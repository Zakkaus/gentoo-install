# SPDX-License-Identifier: GPL-2.0-or-later
"""The only module that opens a network connection.

stage3 is verified before it is unpacked and never after: a signature that does
not match is a failed install, not a reason to try another mirror.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import hashlib
import http.client
import json
import socket
import ssl
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from itertools import takewhile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from collections.abc import Iterator, Sequence
from typing import Any, Callable, Final, cast

from ..errors import (
    ArchiveDigestMismatch,
    CommandFailed,
    ConfigError,
    DownloadFailed,
    IntegrityError,
    PreflightFailed,
    UploadFailed,
)
from ..model import paste
from ..model.validate import KernelCeiling
from ..model.config import ProxyConfig
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

_CURRENT_PROXY: contextvars.ContextVar[ProxyConfig | None] = contextvars.ContextVar(
    "gentoo_install_proxy", default=None
)


def configure_proxy(proxy: ProxyConfig | None) -> None:
    """Set the proxy used by fetch entry points that have no explicit proxy."""
    _CURRENT_PROXY.set(proxy)


def _read_for(url: str, proxy: ProxyConfig | None) -> str:
    return _read(url) if proxy is None else _read(url, proxy)


def stage3(
    mirror: str,
    variant: str,
    fingerprint: str,
    work: Path,
    runner: Runner,
    proxy: ProxyConfig | None = None,
    fallbacks: Sequence[str] = (),
) -> Path:
    """Download the newest stage3 of `variant`, verify it, return where it is.

    A `.verified` marker beside the archive lets an interrupted install skip a
    download of several gigabytes. It names the digest it was written for and
    that digest is recomputed here, because an empty marker beside a replaced
    or corrupted archive was an integrity check that verified nothing.

    `fallbacks` are tried in order when the first mirror cannot be reached at
    all, and when it answers with an archive that does not match its own signed
    DIGESTS: both say something about that mirror rather than the release, and
    `vm-convert` ended four minutes into a run because one copy of a stage3 was
    corrupt. A DIGESTS file whose signature does not verify is a different
    thing and stops everything.
    """
    last: DownloadFailed | ArchiveDigestMismatch | None = None
    for one in (mirror, *fallbacks):
        try:
            return _stage3_from(one, variant, fingerprint, work, runner, proxy)
        except DownloadFailed as failed:
            last = failed
            runner.log(f"{one} did not serve the stage3: {failed}")
        except ArchiveDigestMismatch as corrupt:
            last = corrupt
            runner.log(f"{one} served a corrupt stage3: {corrupt}")
    assert last is not None
    raise last


def _stage3_from(
    mirror: str,
    variant: str,
    fingerprint: str,
    work: Path,
    runner: Runner,
    proxy: ProxyConfig | None = None,
) -> Path:
    selected = proxy if proxy is not None else _bootstrap_proxy(work)
    if selected is not None:
        configure_proxy(selected)
    builds = f"{mirror.rstrip('/')}/{STAGE3_PATH}"
    where = _newest(builds, variant, selected)
    name = where.rsplit("/", 1)[-1]
    work.mkdir(parents=True, exist_ok=True)
    archive = work / name
    marker = work / f"{name}.verified"
    if marker.is_file() and archive.is_file() and _marker_matches(marker, archive, fingerprint):
        return archive

    _download(f"{builds}/{where}", archive, runner.log, proxy=selected)
    digests = work / f"{name}.DIGESTS"
    _download(f"{builds}/{where}.DIGESTS", digests, runner.log, proxy=selected)
    _import_release_key(runner, work, selected)
    _verify_signature(digests, fingerprint, runner)
    try:
        _verify_digest(archive, digests)
    except ArchiveDigestMismatch:
        # Removed before the next mirror is asked: the download below skips a
        # file that is already there, so leaving the bad one would make every
        # fallback verify the same corrupt copy.
        archive.unlink(missing_ok=True)
        raise
    marker.write_text(
        f"{MARKER_SCHEMA}\n{name}\n{_sha512(archive)}\n{fingerprint.lower()}\n"
    )
    return archive


def _bootstrap_proxy(work: Path) -> ProxyConfig | None:
    """Read the plan-written proxy before the stage3 has replaced `/etc`."""
    try:
        root = work.parents[2]
        raw = tomllib.loads((root / "etc/gentoo-install/proxy.toml").read_text())
    except (OSError, IndexError, tomllib.TOMLDecodeError):
        return None
    kind = raw.get("kind", "http")
    host = raw.get("host", "")
    port = raw.get("port", 0)
    username = raw.get("username", "")
    password = raw.get("password", "")
    bypass = raw.get("bypass", [])
    if not isinstance(kind, str) or not isinstance(host, str) or not isinstance(port, int) or not isinstance(username, str) or not isinstance(password, str) or not isinstance(bypass, list) or not all(
        isinstance(item, str) for item in bypass
    ):
        return None
    from ..model.config import ProxyKind
    try:
        proxy_kind = ProxyKind(kind)
    except ValueError:
        return None
    return ProxyConfig(kind=proxy_kind, host=host, port=port, username=username, password=password, bypass=tuple(bypass))


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


def rank_mirrors(
    candidates: tuple[str, ...], proxy: ProxyConfig | None = None
) -> tuple[str, ...]:
    """Fastest first, measured. A mirror that fails or times out keeps its place
    at the end rather than disappearing: a slow mirror still installs, and a
    measurement that found nothing must not leave an empty list.
    """
    # Concurrently: the China list is twenty-three sites, and measuring them
    # one after another costs two minutes when most of them time out.
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        times = list(pool.map(lambda mirror: _probe(mirror, proxy), candidates))
    measured = [(time, position, mirror) for position, (time, mirror) in enumerate(zip(times, candidates))]
    return tuple(mirror for _, _, mirror in sorted(measured))


def _probe(mirror: str, proxy: ProxyConfig | None = None) -> float:
    url = f"{mirror.rstrip('/')}/{PROBE_FILE}"
    started = time.monotonic()
    try:
        with _urlopen(_asked(url), proxy, PROBE_TIMEOUT) as response:
            response.read(1 << 16)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return float("inf")
    return time.monotonic() - started


#: A reset connection is not an answer. `why_unreachable` already retries the
#: same way and `_newest` did not, so one `Connection reset by peer` from
#: `distfiles.gentoo.org` ended an install a minute after it started.
READ_TRIES: Final[int] = 4


def _read_patiently(url: str, proxy: ProxyConfig | None = None) -> str:
    last: DownloadFailed | None = None
    for attempt in range(READ_TRIES):
        try:
            return _read(url) if proxy is None else _read(url, proxy)
        except DownloadFailed as error:
            last = error
            if attempt + 1 < READ_TRIES:
                time.sleep(ONLINE_PAUSE * 2**attempt)
    assert last is not None
    raise last


def _newest(
    builds: str, variant: str, proxy: ProxyConfig | None = None
) -> str:
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
    for line in _read_patiently(pointer, proxy).splitlines():
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


def _import_release_key(
    runner: Runner, work: Path, proxy: ProxyConfig | None = None
) -> None:
    """Load the key a stage3 signature is checked against.

    Trust comes from `RELENG_FINGERPRINT`, not from where the key came from: a
    substituted key has a different fingerprint and `_verify_signature` refuses
    it. So an Alpine or Debian medium, which ships no key file, downloads one.
    """
    source = RELEASE_KEY
    if not RELEASE_KEY.is_file():
        source = work / "gentoo-release.gpg"
        work.mkdir(parents=True, exist_ok=True)
        _download(RELEASE_KEYRING, source, proxy=proxy)
    result = runner.run(["gpg", "--quiet", "--import", str(source)], check=False)
    if result.returncode != 0:
        # Named here rather than left to the verification below, which would
        # report a good signature as a bad one because the key never loaded.
        raise PreflightFailed(f"{source} could not be imported: {result.stdout.strip()}")


def text(url: str, proxy: ProxyConfig | None = None) -> str:
    """A short document, such as a public key someone pasted somewhere."""
    return _read_for(paste.raw_url(url), proxy)


def upload(
    body: str, export: paste.Export, proxy: ProxyConfig | None = None
) -> str:
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
        with _urlopen(request, proxy, TIMEOUT) as response:
            answered = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
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


def network_time(proxy: ProxyConfig | None = None) -> float:
    """Seconds since the epoch from a `Date` header, or 0 when unread."""
    try:
        request = _asked(CLOCK_URL, method="HEAD")
        with _urlopen(request, proxy, PROBE_TIMEOUT) as response:
            stamp = response.headers.get("Date", "")
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
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


def egress_country(proxy: ProxyConfig | None = None) -> str:
    """The two-letter country the machine reaches the internet from, or empty.

    Which mirrors are worth offering follows from where the packets come out,
    not from which language the operator reads: a Taiwanese or Singaporean
    machine reading Chinese is not behind the Great Firewall, and a machine in
    China reading English is.
    """
    for url in COUNTRY_URLS:
        try:
            answer = _read_for(url, proxy)
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


def why_unreachable(url: str, proxy: ProxyConfig | None = None) -> str:
    """Empty when the URL answers, and why it did not otherwise.

    The reason is carried out, not discarded. `cannot reach X` sent a run to
    look at the network when the answer was a certificate the medium could not
    verify, and a whole campaign was diagnosed twice over for want of one
    sentence.
    """
    said = ""
    for attempt in range(ONLINE_TRIES):
        try:
            if proxy is None:
                _read(url)
            else:
                _read(url, proxy)
        except DownloadFailed as error:
            said = str(error)
            if attempt + 1 < ONLINE_TRIES:
                time.sleep(ONLINE_PAUSE * 2**attempt)
            continue
        return ""
    return said


def reachable(url: str, proxy: ProxyConfig | None = None) -> bool:
    """Whether a URL answers, tried `ONLINE_TRIES` times.

    Asked of the address the run will actually read rather than of any host: a
    machine behind a portal resolves names and still cannot fetch anything.
    """
    return not (why_unreachable(url) if proxy is None else why_unreachable(url, proxy))


def online(proxy: ProxyConfig | None = None) -> bool:
    """Whether the package site answers. The menu reads every version from it."""
    url = f"{PACKAGES_API}/sys-kernel/gentoo-kernel-bin.json"
    return reachable(url) if proxy is None else reachable(url, proxy)


def why_mirror_unreachable(
    mirror: str, variant: str, proxy: ProxyConfig | None = None
) -> str:
    """Empty when the mirror answers, and why it did not otherwise."""
    url = f"{mirror.rstrip('/')}/{STAGE3_PATH}/latest-stage3-amd64-{variant}.txt"
    return why_unreachable(url) if proxy is None else why_unreachable(url, proxy)


def mirror_online(
    mirror: str, variant: str, proxy: ProxyConfig | None = None
) -> bool:
    """Whether the mirror an install was told to use answers.

    The stage3 comes from here and nothing else has to answer: a run given a
    configuration never reads `packages.gentoo.org`, and requiring it stopped
    five installs on a network where the mirror was reachable and that site
    was not.
    """
    return reachable(
        f"{mirror.rstrip('/')}/{STAGE3_PATH}/latest-stage3-amd64-{variant}.txt", proxy
    )


def package_versions(
    atom: str, proxy: ProxyConfig | None = None
) -> tuple[tuple[str, bool], ...]:
    """Versions of a main-tree package, newest first, each with whether it is
    stable on amd64. Read live: the installing system need not be Gentoo and
    need not carry a repository at all."""
    try:
        document = json.loads(_read_for(f"{PACKAGES_API}/{atom}.json", proxy))
    except (DownloadFailed, ValueError):
        return ()
    found = [
        (str(entry["version"]), "amd64" in entry.get("keywords", []))
        for entry in document.get("versions", [])
        if entry.get("version")
    ]
    return tuple(sorted(found, key=lambda pair: _version_key(pair[0]), reverse=True))


def overlay_versions(
    atom: str, proxy: ProxyConfig | None = None
) -> tuple[tuple[str, bool], ...]:
    """Versions of a gentoo-zh package, from the overlay's own file listing.

    None is stable: the overlay is keyworded `~amd64` throughout, which is what
    `package.accept_keywords` for it says.
    """
    _, _, name = atom.partition("/")
    try:
        listing = json.loads(_read_for(f"{OVERLAY_API}/{atom}", proxy))
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


def zfs_kernel_max(proxy: ProxyConfig | None = None) -> KernelCeiling:
    """The highest kernel `sys-fs/zfs` builds a module for, or unknown.

    `MODULES_KERNEL_MAX` in the newest ebuild. A real ceiling: 2.4.3 stops at
    7.0, so a 7.1 kernel leaves a ZFS root with no module to import the pool.
    """
    versions = (
        package_versions("sys-fs/zfs")
        if proxy is None
        else package_versions("sys-fs/zfs", proxy)
    )
    for version, _ in versions:
        try:
            ebuild = _read_for(f"{GITWEB}/sys-fs/zfs/zfs-{version}.ebuild", proxy)
        except DownloadFailed:
            return KernelCeiling(None)
        for line in ebuild.splitlines():
            if line.startswith("MODULES_KERNEL_MAX="):
                return KernelCeiling(line.split("=", 1)[1].strip().strip("\"'"))
    return KernelCeiling(None)


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


def _bypassed(url: str, proxy: ProxyConfig) -> bool:
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        return False
    lowered = host.lower()
    for raw_item in proxy.bypass:
        item = raw_item.lower().lstrip(".")
        if item == "*":
            return True
        if item.startswith("*."):
            item = item[2:]
        if lowered == item or lowered.endswith(f".{item}"):
            return True
    return False


def _urlopen(
    request: urllib.request.Request,
    proxy: ProxyConfig | None,
    timeout: float,
) -> Any:
    """Open a request through the configured proxy without exposing credentials."""
    selected = proxy if proxy is not None else _CURRENT_PROXY.get()
    if selected is None or not selected.url or _bypassed(request.full_url, selected):
        return urllib.request.urlopen(request, timeout=timeout)
    if selected.url.lower().split(":", 1)[0] in {"socks5", "socks5h"}:
        return _socks_open(request, selected, timeout)
    # ProxyHandler keeps credentials inside the opener and never places them in
    # argv or the process environment.
    handlers = urllib.request.ProxyHandler({"http": selected.url, "https": selected.url})
    opener_handlers: list[Any] = [handlers]
    if selected.username or selected.password:
        passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, selected.url, selected.username, selected.password)
        passwords.add_password(
            None, selected.redacted_url, selected.username, selected.password
        )
        opener_handlers.append(urllib.request.ProxyBasicAuthHandler(passwords))
    return urllib.request.build_opener(*opener_handlers).open(request, timeout=timeout)


def _recv(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while sum(map(len, chunks)) < size:
        chunk = sock.recv(size - sum(map(len, chunks)))
        if not chunk:
            raise OSError("SOCKS5 proxy closed the connection")
        chunks.append(chunk)
    return b"".join(chunks)


def _socks_connect(proxy: ProxyConfig, host: str, port: int, timeout: float | None) -> socket.socket:
    if not proxy.host:
        raise OSError("SOCKS5 proxy has no host")
    sock = socket.create_connection((proxy.host, proxy.port or 1080), timeout)
    username, password = proxy.username.encode(), proxy.password.encode()
    methods = b"\x00\x02" if username or password else b"\x00"
    sock.sendall(b"\x05" + bytes((len(methods),)) + methods)
    answer = _recv(sock, 2)
    if answer[0] != 5 or answer[1] == 255:
        sock.close()
        raise OSError("SOCKS5 proxy rejected authentication")
    if answer[1] == 2:
        if len(username) > 255 or len(password) > 255:
            sock.close()
            raise OSError("SOCKS5 credentials are too long")
        sock.sendall(b"\x01" + bytes((len(username),)) + username + bytes((len(password),)) + password)
        if _recv(sock, 2) != b"\x01\x00":
            sock.close()
            raise OSError("SOCKS5 proxy rejected credentials")
    # SOCKS5 uses proxy-side DNS so intranet names do not need local resolution.
    remote_dns = True
    if remote_dns:
        encoded = host.encode()
        address = b"\x03" + bytes((len(encoded),)) + encoded
    else:
        resolved = cast(str, socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0])
        packed = socket.inet_pton(socket.AF_INET6 if ":" in resolved else socket.AF_INET, resolved)
        address = (b"\x04" if ":" in resolved else b"\x01") + packed
    sock.sendall(b"\x05\x01\x00" + address + port.to_bytes(2, "big"))
    reply = _recv(sock, 4)
    if reply[1] != 0:
        sock.close()
        raise OSError(f"SOCKS5 proxy CONNECT failed ({reply[1]})")
    length = {1: 4, 4: 16}.get(reply[3])
    if length is None:
        length = _recv(sock, 1)[0]
    _recv(sock, length + 2)
    return sock


def _socks_open(request: urllib.request.Request, proxy: ProxyConfig, timeout: float) -> Any:
    parts = urllib.parse.urlsplit(request.full_url)
    if not parts.hostname:
        raise urllib.error.URLError("request has no host")
    if parts.scheme == "https":
        context = ssl.create_default_context()

        class Secure(http.client.HTTPSConnection):
            def connect(self) -> None:
                self.sock = _socks_connect(proxy, self.host, self.port, self.timeout)
                self.sock = context.wrap_socket(self.sock, server_hostname=self.host)

        opener = urllib.request.build_opener(_SocksHTTPSHandler(Secure))
    else:
        class Plain(http.client.HTTPConnection):
            def connect(self) -> None:
                self.sock = _socks_connect(proxy, self.host, self.port, self.timeout)

        opener = urllib.request.build_opener(_SocksHTTPHandler(Plain))
    return opener.open(request, timeout=timeout)


class _SocksHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, connection: type[http.client.HTTPConnection]) -> None:
        super().__init__()
        self._connection = connection

    def http_open(self, request: urllib.request.Request) -> Any:
        return self.do_open(self._connection, request)


class _SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, connection: type[http.client.HTTPSConnection]) -> None:
        super().__init__()
        self._connection = connection

    def https_open(self, request: urllib.request.Request) -> Any:
        return self.do_open(self._connection, request)


#: Errno values that mean the address family this machine tried has no route
#: at all, rather than the host being down. A guest with a ULA address and
#: NAT64 answers these instantly for every global IPv6 destination.
_NO_ROUTE: Final[frozenset[int]] = frozenset(
    {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EAFNOSUPPORT}
)


def _unroutable(error: BaseException) -> bool:
    """Whether the failure is the address family and not the host."""
    reason = getattr(error, "reason", error)
    return isinstance(reason, OSError) and reason.errno in _NO_ROUTE


@contextlib.contextmanager
def _over(family: int) -> Iterator[None]:
    """Resolve names to one address family, for one attempt.

    Python has no per-request address family. A mirror with both records is
    tried over whichever the resolver puts first, and on a network whose other
    family goes nowhere that answers `Network is unreachable` at once: twenty
    cluster runs stopped there while `curl` fetched the same URL from the same
    guest a second earlier.

    Both directions, not only a fall back to IPv4. An IPv6-only machine has no
    IPv4 route at all, and retrying the family that just failed is no retry.
    """
    real = socket.getaddrinfo

    def only_this(
        host: Any, port: Any, ignored: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> Any:
        return real(host, port, family, type, proto, flags)

    setattr(socket, "getaddrinfo", only_this)
    try:
        yield
    finally:
        setattr(socket, "getaddrinfo", real)


#: Tried in turn after a failure that named no route. Both, because the
#: machine may have only one of them and the resolver may have offered the
#: other first.
_FAMILIES: Final[tuple[int, ...]] = (socket.AF_INET, socket.AF_INET6)


def _read(url: str, proxy: ProxyConfig | None = None) -> str:
    try:
        return _read_once(url) if proxy is None else _read_once(url, proxy)
    except DownloadFailed as first:
        if not _unroutable(first.__cause__ or first):
            raise
        last = first
    for family in _FAMILIES:
        try:
            with _over(family):
                return _read_once(url) if proxy is None else _read_once(url, proxy)
        except DownloadFailed as again:
            last = again
    raise last


def _resolver_state(url: str) -> str:
    """What the machine says about this name, when a fetch could not read it.

    Twenty-three mirrors failed with `EAI_AGAIN` in guests whose `/etc/hosts`
    held every one of their names, and no diagnostic outside the failing
    process could tell which of the two was wrong.
    """
    host = urllib.parse.urlsplit(url).hostname
    if host is None:
        return ""
    try:
        nsswitch = Path("/etc/nsswitch.conf").read_text(encoding="utf-8")
    except OSError:
        nsswitch = ""
    order = next(
        (line.strip() for line in nsswitch.splitlines() if line.startswith("hosts:")),
        "no hosts line",
    )
    try:
        in_file = any(
            host in line.split("#", 1)[0].split()
            for line in Path("/etc/hosts").read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        in_file = False
    return (
        f" [{order}; {host} in /etc/hosts: {in_file};"
        f' {_resolvers(Path("/etc/resolv.conf"))}; {_families(host)};'
        f" {_route_to_resolver(Path('/etc/resolv.conf'))};"
        f" {_kernel_routes(Path('/proc/net/route'))}; {_interfaces()};"
        f" {_network_namespace()}]"
    )


def _interfaces() -> str:
    """The interfaces this process can see.

    An empty routing table beside a console that printed a full one is either a
    namespace with nothing in it or an interface that was deconfigured, and the
    interface list is what separates them.
    """
    try:
        return f"interfaces {[name for _, name in socket.if_nameindex()]}"
    except OSError as error:
        return f"interfaces unreadable: {type(error).__name__}"


def _network_namespace() -> str:
    """Whether this process shares the machine's network namespace."""
    try:
        mine = Path("/proc/self/ns/net").readlink()
        theirs = Path("/proc/1/ns/net").readlink()
    except OSError as error:
        return f"namespace unreadable: {type(error).__name__}"
    return f"namespace {'shared' if mine == theirs else f'{mine} not {theirs}'}"


def _kernel_routes(path: Path) -> str:
    """What the kernel's IPv4 table holds, read without running a command.

    A guest whose shell printed `default via 10.31.0.254 dev ens18` a few
    seconds earlier answered `ENETUNREACH` from this process, which separates a
    table that emptied from a socket that cannot use one that is still there.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return "routes unreadable"
    routes = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        routes.append(f"{fields[0]}:{_dotted(fields[1])}/{_dotted(fields[2])}")
    return f"routes {routes}" if routes else "no routes"


def _dotted(word: str) -> str:
    """A little-endian hexadecimal address from `/proc/net/route`."""
    value = int(word, 16)
    return ".".join(str((value >> shift) & 0xFF) for shift in (0, 8, 16, 24))


def _route_to_resolver(path: Path) -> str:
    """Whether this process can reach the first resolver, and from which address.

    `getent` answered five times out of five and the same lookup timed out from
    this process twenty seconds later, which separates a resolver that stopped
    answering from a guest that lost the address it was answering from.
    """
    servers = _resolvers(path)
    if not servers.startswith("nameservers "):
        return "no route measured"
    first = servers.removeprefix("nameservers ").strip("[]'").split("', '")[0]
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((first, 53))
    except OSError as error:
        return f"route to {first}: {type(error).__name__}:{getattr(error, 'errno', '')}"
    else:
        return f"route to {first} from {probe.getsockname()[0]}"
    finally:
        probe.close()


def _families(host: str) -> str:
    """Which address family the C library could still answer for this name.

    A guest answered `getent ahosts` five times out of five and failed the same
    name from this process twenty seconds later. Asking each family separately
    separates a resolver that drops the AAAA from one that answers nothing.
    """
    answers = []
    for name, family in (("v4", socket.AF_INET), ("v6", socket.AF_INET6)):
        try:
            socket.getaddrinfo(host, None, family)
        except OSError as error:
            answers.append(f"{name}={type(error).__name__}:{getattr(error, 'errno', '')}")
        else:
            answers.append(f"{name}=ok")
    return " ".join(answers)


def _resolvers(path: Path) -> str:
    """The servers the C library would have asked, at the moment of the failure.

    A guest reached its mirrors through `curl` and then failed the same names
    from this process minutes later, which only `/etc/resolv.conf` as it stood
    at each moment can separate.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "resolv.conf unreadable"
    servers = [line.split()[1] for line in lines if line.startswith("nameserver ")]
    return f"nameservers {servers}" if servers else "no nameserver line"


def read_text(url: str, *, ceiling: int) -> str:
    """Read a small document, refusing a body past `ceiling`.

    One byte past the cap is read on purpose: a body exactly at the cap is
    allowed, and anything longer is refused without the rest of it ever being
    read. A configuration is kilobytes, and a redirect into an ISO is the case
    this exists to stop.
    """
    try:
        with _urlopen(_asked(url), _CURRENT_PROXY.get(), TIMEOUT) as response:
            body = response.read(ceiling + 1)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
        raise DownloadFailed(f"{url} could not be read: {error}{_resolver_state(url)}") from error
    if len(body) > ceiling:
        raise DownloadFailed(f"{url} is larger than {ceiling} bytes")
    return str(body.decode("utf-8", "replace"))


def _read_once(url: str, proxy: ProxyConfig | None = None) -> str:
    try:
        with _urlopen(_asked(url), proxy, TIMEOUT) as response:
            return str(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
        raise DownloadFailed(f"{url} could not be read: {error}{_resolver_state(url)}") from error


#: A stage3 is a quarter of a gigabyte over whatever link the operator has, so
#: one timed-out read part way through is the transient case. `_read_patiently`
#: already covers the pointer file beside it; the archive had no retry at all,
#: and `The read operation timed out` ended an install two minutes in.
DOWNLOAD_TRIES: Final[int] = 3


#: How long a name may stay unresolvable before the install gives up. A
#: resolver that answers intermittently is the common case on the networks this
#: installer is used from, and `Temporary failure in name resolution` says so;
#: six seconds of retry ended installs a resolver would have served a minute
#: later. `SYNC_PAUSE` waits this long for a mirror rewriting its Manifests.
UNRESOLVED_PAUSE: Final[float] = 30.0


def _unresolved(error: BaseException) -> bool:
    """Whether the name did not resolve, rather than the host refusing."""
    reason = getattr(error, "reason", error)
    return isinstance(reason, socket.gaierror) and reason.errno == socket.EAI_AGAIN


def _permanent(error: BaseException) -> bool:
    """Whether trying again cannot help. A missing archive stays missing; a
    timeout, a reset, or `429 Too Many Requests` is a moment rather than an
    answer."""
    return (
        isinstance(error, urllib.error.HTTPError)
        and 400 <= error.code < 500
        and error.code not in {408, 429}
    )


def _download(
    url: str,
    target: Path,
    log: Callable[[str], None] | None = None,
    proxy: ProxyConfig | None = None,
) -> None:
    """Retried with a growing pause, because a stage3 is the longest fetch a
    run makes and a link that dropped one read usually carries the next."""
    last: DownloadFailed | None = None
    for attempt in range(DOWNLOAD_TRIES):
        try:
            _download_over_any_family(url, target, log, proxy)
            return
        except DownloadFailed as error:
            if _permanent(error.__cause__ or error):
                raise
            last = error
            if attempt + 1 < DOWNLOAD_TRIES:
                cause = error.__cause__ or error
                time.sleep(
                    UNRESOLVED_PAUSE if _unresolved(cause) else ONLINE_PAUSE * 2**attempt
                )
    assert last is not None
    raise last


def _download_over_any_family(
    url: str,
    target: Path,
    log: Callable[[str], None] | None = None,
    proxy: ProxyConfig | None = None,
) -> None:
    """Written beside the target and renamed, so an interrupted download never
    leaves a short file that looks complete.

    Falls back to IPv4 for the same reason `_read` does: the stage3 is the
    largest thing this installer fetches, and a mirror reached over an address
    family with no route fails before a byte arrives.
    """
    try:
        _download_once(url, target, log, proxy)
        return
    except DownloadFailed as first:
        if not _unroutable(first.__cause__ or first):
            raise
        last = first
    for family in _FAMILIES:
        try:
            with _over(family):
                _download_once(url, target, log, proxy)
                return
        except DownloadFailed as again:
            last = again
    raise last



#: How long a download may say nothing. The campaign's watchdog ends a guest
#: that has printed nothing for twenty minutes, and a stage3 over a slow mirror
#: takes longer than that to arrive.
PROGRESS_INTERVAL: Final[float] = 30.0


def _content_length(response: object) -> int | None:
    length = getattr(response, "headers", {}).get("Content-Length")
    try:
        return int(length) if length is not None else None
    except ValueError:
        return None


def _progress(name: str, got: int, total: int | None) -> str:
    megabytes = got / (1 << 20)
    if total:
        return f"{name}: {megabytes:.0f} MiB of {total / (1 << 20):.0f} MiB"
    return f"{name}: {megabytes:.0f} MiB"


def _download_once(
    url: str,
    target: Path,
    log: Callable[[str], None] | None = None,
    proxy: ProxyConfig | None = None,
) -> None:
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with _urlopen(_asked(url), proxy, TIMEOUT) as response, partial.open("wb") as handle:
            total = _content_length(response)
            got = 0
            said = time.monotonic()
            while chunk := response.read(1 << 20):
                handle.write(chunk)
                got += len(chunk)
                # A stage3 is the longest silence in a run, and a watchdog
                # reading the console ends the guest doing the most work.
                if log is not None and time.monotonic() - said >= PROGRESS_INTERVAL:
                    said = time.monotonic()
                    log(_progress(target.name, got, total))
        partial.replace(target)
    # `http.client.HTTPException` beside the rest: `IncompleteRead` is one and
    # is not an `OSError`, so a server closing the connection with bytes still
    # promised escaped this handler, left the `.part` file behind and stopped
    # the install instead of reaching the next mirror.
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
        partial.unlink(missing_ok=True)
        raise DownloadFailed(f"{url} could not be fetched: {error}") from error


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
        raise ArchiveDigestMismatch(
            f"{archive.name} has SHA512 {got}, the DIGESTS file says {wanted}"
        )


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
