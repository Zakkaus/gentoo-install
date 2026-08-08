"""The only module that opens a network connection.

stage3 is verified before it is unpacked and never after: a signature that does
not match is a failed install, not a reason to try another mirror.
"""

from __future__ import annotations

import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
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
)
from ..model import paste
from ..model.device import DeviceId
from .probe import RELEASE_KEY
from .runner import Runner

STAGE3_PATH: Final[str] = "releases/amd64/autobuilds"
TIMEOUT: Final[float] = 60.0

#: Every Gentoo mirror carries this, and it is small enough that the time is
#: dominated by latency and the first megabytes of throughput.
PROBE_FILE: Final[str] = "distfiles/timestamp.chk"

#: A mirror that has not answered by now is not the one to install from.
PROBE_TIMEOUT: Final[float] = 5.0

#: Enough that a long mirror list is measured in one timeout rather than in
#: one per site.
PROBE_WORKERS: Final[int] = 8


#: The mirror's index is HTML; this is the link to a tarball in it.
_ENTRY = re.compile(r'href="(stage3-amd64-[\w.\-]+\.tar\.xz)"')


def stage3(mirror: str, variant: str, fingerprint: str, work: Path, runner: Runner) -> Path:
    """Download the newest stage3 of `variant`, verify it, return where it is.

    A `.verified` marker beside the archive means a previous run already checked
    it, so an interrupted install does not download several gigabytes again.
    """
    base = f"{mirror.rstrip('/')}/{STAGE3_PATH}/current-stage3-amd64-{variant}"
    name = _newest(base)
    work.mkdir(parents=True, exist_ok=True)
    archive = work / name
    if (work / f"{name}.verified").is_file() and archive.is_file():
        return archive

    _download(f"{base}/{name}", archive)
    digests = work / f"{name}.DIGESTS"
    _download(f"{base}/{name}.DIGESTS", digests)
    _import_release_key(runner)
    _verify_signature(digests, fingerprint, runner)
    _verify_digest(archive, digests)
    (work / f"{name}.verified").write_text("")
    return archive


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
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as response:
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


def _newest(base: str) -> str:
    names: set[str] = {str(name) for name in _ENTRY.findall(_read(f"{base}/"))}
    if not names:
        # DownloadFailed, not IntegrityError: the index arrived and held
        # nothing, which is a mirror mid-sync rather than data to distrust.
        raise DownloadFailed(f"{base} lists no stage3 archive")
    return sorted(names)[-1]


def _import_release_key(runner: Runner) -> None:
    """The medium ships the key; importing it beats fetching one from a
    keyserver, which would decide at run time what to trust.

    `preflight.py` is what checks the file exists, so a machine without it is
    stopped before the download rather than after it.
    """
    result = runner.run(["gpg", "--quiet", "--import", str(RELEASE_KEY)], check=False)
    if result.returncode != 0:
        # Named here rather than left to the verification below, which would
        # report a good signature as a bad one because the key never loaded.
        raise PreflightFailed(f"{RELEASE_KEY} could not be imported: {result.stdout.strip()}")


def text(url: str) -> str:
    """A short document, such as a public key someone pasted somewhere."""
    return _read(paste.raw_url(url))


def _read(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return str(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DownloadFailed(f"{url} could not be read: {error}") from error


def _download(url: str, target: Path) -> None:
    """Written beside the target and renamed, so an interrupted download never
    leaves a short file that looks complete."""
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response, partial.open("wb") as handle:
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
    `EXPKEYSIG` is accepted because Gentoo's release key expires and is
    extended; a revoked or bad signature is not.
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

    `VALIDSIG` reports the subkey that made the signature in its second field
    and the primary key in its last. Gentoo signs with a subkey, so comparing
    the second field rejects a signature that is perfectly good.
    """
    for line in status.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "[GNUPG:]" and fields[1] in ("VALIDSIG", "EXPKEYSIG"):
            return fields[-1]
    return None


def _verify_digest(archive: Path, digests: Path) -> None:
    wanted = _expected_sha512(digests, archive.name)
    got = hashlib.sha512(archive.read_bytes()).hexdigest()
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
