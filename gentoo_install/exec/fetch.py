"""The only module that opens a network connection.

stage3 is verified before it is unpacked and never after: a signature that does
not match is a failed install, not a reason to try another mirror.
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from itertools import takewhile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

from ..errors import (
    CommandFailed,
    ConfigError,
    DownloadFailed,
    IntegrityError,
    PreflightFailed,
    UploadFailed,
)
from ..model import paste
from ..model.device import DeviceId
from .probe import RELEASE_KEY
from .runner import Runner

STAGE3_PATH: Final[str] = "releases/amd64/autobuilds"
TIMEOUT: Final[float] = 60.0

#: Sent on every request. `paste.gentoozh.org` answers 403 to the agent urllib
#: sends by default, so a key fetched from a paste failed before this existed;
#: naming the installer also puts something readable in a mirror's log.
USER_AGENT: Final[str] = "gentoo-install"

#: Every Gentoo mirror carries this, and it is small enough that the time is
#: dominated by latency and the first megabytes of throughput.
PROBE_FILE: Final[str] = "distfiles/timestamp.chk"

#: A mirror that has not answered by now is not the one to install from.
PROBE_TIMEOUT: Final[float] = 5.0

#: Enough that a long mirror list is measured in one timeout rather than in
#: one per site.
PROBE_WORKERS: Final[int] = 8


#: The marker's format. A marker written by an older installer says nothing
#: about the bytes beside it, so it is refused rather than trusted.
MARKER_SCHEMA: Final[str] = "gentoo-install-stage3-1"


def stage3(mirror: str, variant: str, fingerprint: str, work: Path, runner: Runner) -> Path:
    """Download the newest stage3 of `variant`, verify it, return where it is.

    A `.verified` marker beside the archive lets an interrupted install skip a
    download of several gigabytes. It names the digest it was written for and
    that digest is recomputed here, because an empty marker beside a replaced
    or corrupted archive was an integrity check that verified nothing.
    """
    builds = f"{mirror.rstrip('/')}/{STAGE3_PATH}"
    where = _newest(builds, variant)
    name = where.rsplit("/", 1)[-1]
    work.mkdir(parents=True, exist_ok=True)
    archive = work / name
    marker = work / f"{name}.verified"
    if marker.is_file() and archive.is_file() and _marker_matches(marker, archive, fingerprint):
        return archive

    _download(f"{builds}/{where}", archive)
    digests = work / f"{name}.DIGESTS"
    _download(f"{builds}/{where}.DIGESTS", digests)
    _import_release_key(runner, work)
    _verify_signature(digests, fingerprint, runner)
    _verify_digest(archive, digests)
    marker.write_text(
        f"{MARKER_SCHEMA}\n{name}\n{_sha512(archive)}\n{fingerprint.lower()}\n"
    )
    return archive


def _marker_matches(marker: Path, archive: Path, fingerprint: str) -> bool:
    """Whether the marker was written for exactly these bytes and this key."""
    said = marker.read_text().splitlines()
    if len(said) != 4 or said[0] != MARKER_SCHEMA:
        return False
    schema, name, digest, key = said
    return name == archive.name and key == fingerprint.lower() and digest == _sha512(archive)


def _sha512(path: Path) -> str:
    """Streamed: a stage3 is a quarter of a gigabyte and the live medium's root
    is a tmpfs, so reading it whole costs memory the install still needs."""
    reader = hashlib.sha512()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            reader.update(block)
    return reader.hexdigest()


def rank_mirrors(candidates: tuple[str, ...]) -> tuple[str, ...]:
    """Fastest first, measured. A mirror that fails or times out keeps its place
    at the end rather than disappearing: a slow mirror still installs, and a
    measurement that found nothing must not leave an empty list.
    """
    # Concurrently: the China list is twenty-three sites, and measuring them
    # one after another costs two minutes when most of them time out.
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        times = list(pool.map(_probe, candidates))
    measured = [(time, position, mirror) for position, (time, mirror) in enumerate(zip(times, candidates))]
    return tuple(mirror for _, _, mirror in sorted(measured))


def _probe(mirror: str) -> float:
    url = f"{mirror.rstrip('/')}/{PROBE_FILE}"
    started = time.monotonic()
    try:
        with urllib.request.urlopen(_asked(url), timeout=PROBE_TIMEOUT) as response:
            response.read(1 << 16)
    except (urllib.error.URLError, TimeoutError, OSError):
        return float("inf")
    return time.monotonic() - started


def passphrase_for(device: DeviceId, source: str) -> str:
    """Read an encryption passphrase from the file the layout names.

    A path, never the passphrase itself: the configuration is copied into the
    target and the log is the file people paste into bug reports.
    """
    if not source:
        raise ConfigError(
            f"{device} is encrypted but names no passphrase_file, and nothing else asks for one"
        )
    path = Path(source)
    try:
        passphrase = path.read_text().strip("\n")
    except OSError as error:
        raise ConfigError(f"{device}: {source} cannot be read: {error}") from error
    if not passphrase:
        raise ConfigError(f"{device}: {source} is empty")
    return passphrase


def _newest(builds: str, variant: str) -> str:
    """The current archive's path under `releases/amd64/autobuilds`.

    `latest-stage3-amd64-<variant>.txt`, not the directory index: an index is
    a mirror's own HTML and every mirror writes it differently. USTC links
    each file by an absolute path, so the pattern that matched Gentoo's own
    page found nothing there and every install from a Chinese mirror stopped
    with `lists no stage3 archive`.

    The path, not the bare name. Each entry is `<timestamp>/<name> <size>`,
    and the dated directory is where the file stays; `current-stage3-amd64-*`
    is a symlink that moves when a build is published, and downloading through
    it answered `404 Not Found` for an archive the pointer had just named.

    The signature around the entries is not checked here: the DIGESTS file is,
    and it is what decides whether the bytes are the right ones.
    """
    pointer = f"{builds}/latest-stage3-amd64-{variant}.txt"
    paths: list[str] = []
    for line in _read(pointer).splitlines():
        said = line.strip()
        if not said or said.startswith(("#", "-----", "Hash:")):
            continue
        first = said.split()[0]
        if first.endswith(".tar.xz"):
            paths.append(first)
    if not paths:
        # DownloadFailed, not IntegrityError: the file arrived and named
        # nothing, which is a mirror mid-sync rather than data to distrust.
        raise DownloadFailed(f"{pointer} names no stage3 archive")
    return sorted(paths)[-1]


#: Gentoo's own keyring, which carries the release signing key. Fetched when
#: the medium ships no key file, which is every medium that is not Gentoo's.
RELEASE_KEYRING: Final[str] = "https://qa-reports.gentoo.org/output/service-keys.gpg"


def _import_release_key(runner: Runner, work: Path) -> None:
    """Load the key a stage3 signature is checked against.

    Trust comes from `RELENG_FINGERPRINT`, not from where the key came from: a
    substituted key has a different fingerprint and `_verify_signature` refuses
    it. So an Alpine or Debian medium, which ships no key file, downloads one.
    """
    source = RELEASE_KEY
    if not RELEASE_KEY.is_file():
        source = work / "gentoo-release.gpg"
        work.mkdir(parents=True, exist_ok=True)
        _download(RELEASE_KEYRING, source)
    result = runner.run(["gpg", "--quiet", "--import", str(source)], check=False)
    if result.returncode != 0:
        # Named here rather than left to the verification below, which would
        # report a good signature as a bad one because the key never loaded.
        raise PreflightFailed(f"{source} could not be imported: {result.stdout.strip()}")


def text(url: str) -> str:
    """A short document, such as a public key someone pasted somewhere."""
    return _read(paste.raw_url(url))


def upload(body: str, export: paste.Export) -> str:
    """Create a paste and return the address of the page that shows it.

    Offered after a run has already finished or failed, so every failure here
    is reported and none of them changes the outcome of the install.
    """
    request = _asked(
        f"{paste.BASE}/",
        data=paste.payload(body, export),
        method="POST",
        **{"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            answered = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UploadFailed(f"{paste.HOST} did not take the paste: {error}") from error
    except ValueError as error:
        raise UploadFailed(f"{paste.HOST} answered something that is not JSON") from error
    path = answered.get("path") if isinstance(answered, dict) else None
    if not isinstance(path, str) or not path:
        raise UploadFailed(f"{paste.HOST} answered no path for the new paste")
    return paste.page_url(path)


#: Version and keyword data for one package of the main tree.
PACKAGES_API: Final[str] = "https://packages.gentoo.org/packages"

#: One file of the main tree, by path. Used for what the API does not carry.
GITWEB: Final[str] = "https://gitweb.gentoo.org/repo/gentoo.git/plain"

#: The gentoo-zh overlay's file listing. Its packages are on no package site.
OVERLAY_API: Final[str] = "https://api.github.com/repos/gentoo-zh/overlay/contents"


#: Read over plain HTTP on purpose: a clock far enough out makes every TLS
#: certificate look not-yet-valid, so HTTPS fails before the time can be read.
CLOCK_URL: Final[str] = "http://distfiles.gentoo.org/"

#: Beyond this the certificates start being refused, so it is worth saying.
CLOCK_TOLERANCE: Final[float] = 24 * 3600.0


def network_time() -> float:
    """Seconds since the epoch from a `Date` header, or 0 when unread."""
    try:
        request = _asked(CLOCK_URL, method="HEAD")
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            stamp = response.headers.get("Date", "")
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0.0
    try:
        return parsedate_to_datetime(stamp).timestamp()
    except (TypeError, ValueError):
        return 0.0


#: Where the machine's egress country is read from, in order. Two, because the
#: first is not always reachable from the network whose answer matters most.
COUNTRY_URLS: Final[tuple[str, ...]] = (
    "https://ipinfo.io/country",
    "https://www.cloudflare.com/cdn-cgi/trace",
)


def egress_country() -> str:
    """The two-letter country the machine reaches the internet from, or empty.

    Which mirrors are worth offering follows from where the packets come out,
    not from which language the operator reads: a Taiwanese or Singaporean
    machine reading Chinese is not behind the Great Firewall, and a machine in
    China reading English is.
    """
    for url in COUNTRY_URLS:
        try:
            answer = _read(url)
        except DownloadFailed:
            continue
        for line in answer.splitlines():
            if line.startswith("loc="):
                return line[4:].strip().upper()
        stripped = answer.strip().upper()
        if len(stripped) == 2 and stripped.isalpha():
            return stripped
    return ""


#: How many times the address is asked before the machine is called offline,
#: and the first pause between attempts, which doubles. The check guards the
#: whole run, so a single lost packet must not decide it, and a fixed two
#: seconds was not enough spread: twelve guests starting together each failed
#: three attempts inside ninety seconds against a host that was answering.
#: Five attempts back off 2, 4, 8 and 16 seconds, half a minute in all, which
#: is nothing beside the install it is guarding.
ONLINE_TRIES: Final[int] = 5
ONLINE_PAUSE: Final[float] = 2.0


def reachable(url: str) -> bool:
    """Whether a URL answers, tried `ONLINE_TRIES` times.

    Asked of the address the run will actually read rather than of any host: a
    machine behind a portal resolves names and still cannot fetch anything.
    """
    for attempt in range(ONLINE_TRIES):
        try:
            _read(url)
        except DownloadFailed:
            if attempt + 1 < ONLINE_TRIES:
                time.sleep(ONLINE_PAUSE * 2**attempt)
            continue
        return True
    return False


def online() -> bool:
    """Whether the package site answers. The menu reads every version from it."""
    return reachable(f"{PACKAGES_API}/sys-kernel/gentoo-kernel-bin.json")


def mirror_online(mirror: str, variant: str) -> bool:
    """Whether the mirror an install was told to use answers.

    The stage3 comes from here and nothing else has to answer: a run given a
    configuration never reads `packages.gentoo.org`, and requiring it stopped
    five installs on a network where the mirror was reachable and that site
    was not.
    """
    return reachable(
        f"{mirror.rstrip('/')}/{STAGE3_PATH}/latest-stage3-amd64-{variant}.txt"
    )


def package_versions(atom: str) -> tuple[tuple[str, bool], ...]:
    """Versions of a main-tree package, newest first, each with whether it is
    stable on amd64. Read live: the installing system need not be Gentoo and
    need not carry a repository at all."""
    try:
        document = json.loads(_read(f"{PACKAGES_API}/{atom}.json"))
    except (DownloadFailed, ValueError):
        return ()
    found = [
        (str(entry["version"]), "amd64" in entry.get("keywords", []))
        for entry in document.get("versions", [])
        if entry.get("version")
    ]
    return tuple(sorted(found, key=lambda pair: _version_key(pair[0]), reverse=True))


def overlay_versions(atom: str) -> tuple[tuple[str, bool], ...]:
    """Versions of a gentoo-zh package, from the overlay's own file listing.

    None is stable: the overlay is keyworded `~amd64` throughout, which is what
    `package.accept_keywords` for it says.
    """
    _, _, name = atom.partition("/")
    try:
        listing = json.loads(_read(f"{OVERLAY_API}/{atom}"))
    except (DownloadFailed, ValueError):
        return ()
    if not isinstance(listing, list):
        return ()
    versions = [
        str(entry["name"])[len(name) + 1 : -len(".ebuild")]
        for entry in listing
        if isinstance(entry, dict) and str(entry.get("name", "")).endswith(".ebuild")
    ]
    named = [version for version in versions if version and version != "9999"]
    return tuple((version, False) for version in sorted(named, key=_version_key, reverse=True))


def zfs_kernel_max() -> str:
    """The highest kernel `sys-fs/zfs` builds a module for, or empty if unread.

    `MODULES_KERNEL_MAX` in the newest ebuild. A real ceiling: 2.4.3 stops at
    7.0, so a 7.1 kernel leaves a ZFS root with no module to import the pool.
    """
    for version, _ in package_versions("sys-fs/zfs"):
        try:
            ebuild = _read(f"{GITWEB}/sys-fs/zfs/zfs-{version}.ebuild")
        except DownloadFailed:
            return ""
        for line in ebuild.splitlines():
            if line.startswith("MODULES_KERNEL_MAX="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric components, so 6.18.43 sorts above 6.6.148."""
    parts: list[int] = []
    for piece in version.replace("-", ".").replace("_", ".").split("."):
        digits = "".join(takewhile(str.isdigit, piece))
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _asked(url: str, *, data: bytes | None = None, method: str = "GET", **headers: str) -> urllib.request.Request:
    """Every request this module makes, so none of them goes out unnamed."""
    return urllib.request.Request(
        url, data=data, method=method, headers={"User-Agent": USER_AGENT, **headers}
    )


def _read(url: str) -> str:
    try:
        with urllib.request.urlopen(_asked(url), timeout=TIMEOUT) as response:
            return str(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DownloadFailed(f"{url} could not be read: {error}") from error


def _download(url: str, target: Path) -> None:
    """Written beside the target and renamed, so an interrupted download never
    leaves a short file that looks complete."""
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(_asked(url), timeout=TIMEOUT) as response, partial.open("wb") as handle:
            while chunk := response.read(1 << 20):
                handle.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        partial.unlink(missing_ok=True)
        raise DownloadFailed(f"{url} could not be fetched: {error}") from error
    partial.replace(target)


def _verify_signature(digests: Path, fingerprint: str, runner: Runner) -> None:
    """Compare the fingerprint gpg reports, not the text it printed.

    A substring test over everything gpg wrote would also match the hex in a
    file name the mirror chose, and the archive name comes from the mirror.
    An expired key is refused with everything else gpg refuses: it exits
    non-zero for one, and a pin that no longer verifies is not a thing to work
    around silently.
    """
    result = runner.run(["gpg", "--status-fd", "1", "--verify", str(digests)], check=False)
    signed = _signing_key(result.stdout)
    if result.returncode != 0 or signed is None:
        raise IntegrityError(f"the signature on {digests.name} does not verify")
    if signed.upper() != fingerprint.upper():
        raise IntegrityError(
            f"{digests.name} is signed by {signed}, not the pinned {fingerprint}"
        )


def _signing_key(status: str) -> str | None:
    """The primary key's fingerprint, which is what a pin names.

    `VALIDSIG` only. Its last field is the primary key and its second the
    subkey that signed; Gentoo signs with a subkey, so comparing the second
    rejects a signature that is perfectly good. `EXPKEYSIG` ends in the
    *username*, per gpg's own DETAILS, so reading a fingerprint out of it
    yields `<releng@gentoo.org>` and accuses a good signature of being wrong.
    gpg emits both lines for one signature, so nothing is lost by ignoring it.
    """
    for line in status.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "[GNUPG:]" and fields[1] == "VALIDSIG":
            return fields[-1]
    return None


def _verify_digest(archive: Path, digests: Path) -> None:
    wanted = _expected_sha512(digests, archive.name)
    got = _sha512(archive)
    if got != wanted:
        raise IntegrityError(f"{archive.name} has SHA512 {got}, the DIGESTS file says {wanted}")


def _expected_sha512(digests: Path, name: str) -> str:
    lines = digests.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.strip().upper().startswith("# SHA512"):
            for candidate in lines[index + 1 :]:
                parts = candidate.split()
                if len(parts) == 2 and Path(parts[1]).name == name:
                    return parts[0].lower()
    raise IntegrityError(f"{digests.name} has no SHA512 line for {name}")


def password_hash(password: str, runner: Runner) -> str:
    """A crypt(3) SHA-512 hash of `password`.

    `openssl passwd -6`, because Python removed the `crypt` module in 3.13 and
    a hand-rolled implementation of a password format is not worth the risk.
    The password reaches openssl on stdin, so it is never in a command line
    that ps or the journal would show.
    """
    if not password:
        return ""
    result = runner.run(["openssl", "passwd", "-6", "-stdin"], input_text=f"{password}\n")
    hashed = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not hashed.startswith("$6$"):
        raise CommandFailed(f"openssl produced no sha512 hash: {result.stdout[:80]!r}")
    return hashed
