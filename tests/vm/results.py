"""Result transfer from guest to host.

The guest writes a tar stream straight onto a raw virtio disk and the host reads it
back with `tarfile`. Nothing has to parse console output, and neither side needs a
filesystem tool or root.
"""

from __future__ import annotations

import base64
import binascii
import io
import tarfile
from pathlib import Path

DEFAULT_SIZE_MIB = 64


class ResultError(Exception):
    """The guest produced no readable result archive."""


def create_disk(path: Path, size_mib: int = DEFAULT_SIZE_MIB) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as image:
        image.truncate(size_mib * 1024 * 1024)
    return path


def collect_command(directory: str, device: str = "/dev/vda") -> str:
    return f"tar cf {device} -C {directory} ."


#: Wraps the archive on the console so the reader can find its two ends in a
#: stream that also carries the shell's own echo of the command.
CONSOLE_OPEN = "GI_RESULTS_BEGIN"
CONSOLE_CLOSE = "GI_RESULTS_END"


def console_command(directory: str) -> str:
    """Print the results as one base64 line between two markers.

    A guest on the cluster writes its disks to shared storage the workstation
    cannot read, and the API offers no way to download a volume, so the console
    is the only channel back. Compressed first: an install log is megabytes and
    base64 adds a third again.
    """
    return (
        f"echo {CONSOLE_OPEN}; tar cz -C {directory} . | base64 -w0; "
        f"echo; echo {CONSOLE_CLOSE}"
    )


def read_console(said: bytes) -> dict[str, bytes]:
    """The archive out of what the console printed."""
    opened = said.rfind(CONSOLE_OPEN.encode())
    closed = said.rfind(CONSOLE_CLOSE.encode())
    if opened < 0 or closed < opened:
        raise ResultError("the console carried no result archive")
    # Everything between the markers with the line breaks the terminal added
    # removed: base64 -w0 writes one line, and the console wraps it.
    encoded = b"".join(said[opened:closed].split()[1:])
    try:
        packed = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ResultError(f"the console result is not base64: {error}") from error
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                if extracted is not None:
                    files[member.name.removeprefix("./")] = extracted.read()
    except (tarfile.TarError, EOFError) as error:
        raise ResultError(f"the console result is not a tar archive: {error}") from error
    if not files:
        raise ResultError("the console result archive is empty")
    return files


def read_disk(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    try:
        with path.open("rb") as image, tarfile.open(fileobj=image, mode="r|") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                files[member.name.removeprefix("./")] = extracted.read()
    except tarfile.TarError as error:
        raise ResultError(f"{path} holds no tar archive: {error}") from error
    if not files:
        raise ResultError(f"{path} holds an empty tar archive")
    return files
