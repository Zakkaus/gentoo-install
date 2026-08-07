"""The only module that opens a network connection.

stage3 is verified before it is unpacked and never after: a signature that does
not match is a failed install, not a reason to try another mirror.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

from ..errors import IntegrityError
from ..model.device import DeviceId
from .runner import Runner

STAGE3_PATH: Final[str] = "releases/amd64/autobuilds"
TIMEOUT: Final[float] = 60.0

#: Where the install medium keeps the release engineering public key.
RELEASE_KEY: Final[Path] = Path("/usr/share/openpgp-keys/gentoo-release.asc")

#: Every Gentoo mirror carries this, and it is small enough that the time is
#: dominated by latency and the first megabytes of throughput.
PROBE_FILE: Final[str] = "distfiles/timestamp.chk"

#: A mirror that has not answered by now is not the one to install from.
PROBE_TIMEOUT: Final[float] = 5.0


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
    measured: list[tuple[float, int, str]] = []
    for position, mirror in enumerate(candidates):
        measured.append((_probe(mirror), position, mirror))
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


def passphrase_for(device: DeviceId) -> str:
    """Where an encryption passphrase comes from.

    Interactive entry belongs to the interface layer; until it exists, a run
    that needs one fails here rather than inventing a key nobody knows.
    """
    raise IntegrityError(
        f"no passphrase is available for {device}; the interface that asks for one is not written"
    )


def _newest(base: str) -> str:
    names: set[str] = {str(name) for name in _ENTRY.findall(_read(f"{base}/"))}
    if not names:
        raise IntegrityError(f"{base} lists no stage3 archive")
    return sorted(names)[-1]


def _import_release_key(runner: Runner) -> None:
    """The medium ships the key; importing it beats fetching one from a
    keyserver, which would decide at run time what to trust."""
    if RELEASE_KEY.is_file():
        runner.run(["gpg", "--quiet", "--import", str(RELEASE_KEY)], check=False)


def _read(url: str) -> str:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
        return str(response.read().decode("utf-8", "replace"))


def _download(url: str, target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response, partial.open("wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)
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
    for line in status.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "[GNUPG:]" and fields[1] in ("VALIDSIG", "EXPKEYSIG"):
            return fields[2]
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
