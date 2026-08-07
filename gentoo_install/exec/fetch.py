"""The only module that opens a network connection.

stage3 is verified before it is unpacked and never after: a signature that does
not match is a failed install, not a reason to try another mirror.
"""

from __future__ import annotations

import hashlib
import re
import urllib.request
from pathlib import Path
from typing import Final

from ..errors import IntegrityError
from ..model.device import DeviceId
from .runner import Runner

STAGE3_PATH: Final[str] = "releases/amd64/autobuilds"
TIMEOUT: Final[float] = 60.0

#: The index lists every build; this is the tarball line in it.
_ENTRY = re.compile(r"^(?P<name>stage3-amd64-[\w.\-]+\.tar\.xz)\s+(?P<size>\d+)", re.MULTILINE)


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
    _verify_signature(digests, fingerprint, runner)
    _verify_digest(archive, digests)
    (work / f"{name}.verified").write_text("")
    return archive


def passphrase_for(device: DeviceId) -> str:
    """Where an encryption passphrase comes from.

    Interactive entry belongs to the interface layer; until it exists, a run
    that needs one fails here rather than inventing a key nobody knows.
    """
    raise IntegrityError(
        f"no passphrase is available for {device}; the interface that asks for one is not written"
    )


def _newest(base: str) -> str:
    listing = _read(f"{base}/")
    names = sorted({found.group("name") for found in _ENTRY.finditer(listing)})
    if not names:
        names = sorted(set(re.findall(r'href="(stage3-amd64-[\w.\-]+\.tar\.xz)"', listing)))
    if not names:
        raise IntegrityError(f"{base} lists no stage3 archive")
    return names[-1]


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
    result = runner.run(["gpg", "--status-fd", "1", "--verify", str(digests)], check=False)
    if result.returncode != 0:
        raise IntegrityError(f"the signature on {digests.name} does not verify")
    if fingerprint.upper() not in result.stdout.upper().replace(" ", ""):
        raise IntegrityError(
            f"{digests.name} is signed by a key that is not the pinned {fingerprint}"
        )


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
