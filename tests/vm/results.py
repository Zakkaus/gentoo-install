"""Result transfer from guest to host.

The guest writes a tar stream straight onto a raw virtio disk and the host reads it
back with `tarfile`. Nothing has to parse console output, and neither side needs a
filesystem tool or root.
"""

from __future__ import annotations

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
