# SPDX-License-Identifier: GPL-2.0-or-later
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
from typing import Final

DEFAULT_SIZE_MIB = 64
RESULT_BUFFER_BYTES = 64 * 1024 * 1024


class ResultError(Exception):
    """The guest produced no readable result archive."""


def create_disk(path: Path, size_mib: int = DEFAULT_SIZE_MIB) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as image:
        image.truncate(size_mib * 1024 * 1024)
    return path


#: The serial QEMU gives the result disk, and the name udev builds from it.
#: Named rather than numbered: the driver CD is a virtio disk as well, so
#: `/dev/vda` is whichever of them the kernel enumerated first.
RESULT_SERIAL: Final[str] = "result"
RESULT_DEVICE: Final[str] = f"/dev/disk/by-id/virtio-{RESULT_SERIAL}0"


def collect_command(directory: str, device: str = RESULT_DEVICE) -> str:
    return f"tar cf {device} -C {directory} ."


#: Wraps the archive on the console so the reader can find its two ends in a
#: stream that also carries the shell's own echo of the command.
CONSOLE_OPEN = "GI_RESULTS_BEGIN"
CONSOLE_CLOSE = "GI_RESULTS_END"


#: How much of the install log the console carries back. A source-kernel
#: install writes 80 MB of it, which gzips to 4.5 MiB and reaches the reader
#: as one base64 line of about six: `vm-source-kernel` is the only fixture
#: that has ever failed while its results were being read, and it did so
#: twice. The last quarter of a megabyte holds the failure and the tail of
#: whatever emerge was doing, which is what anybody reads.
LOG_TAIL_BYTES: Final[int] = 256 * 1024

#: What the guest writes the tail to, beside the log it came from.
LOG_TAIL: Final[str] = "install.tail"

#: Up to this much of the log travels whole. Measured on the logs the cluster
#: had kept when this was written: 39 fixtures between 3.9 MB and 25 MB, all
#: of which crossed the console before the tail existed, and one
#: `vm-source-kernel` at 80 MB, which never did. The whole log is what a
#: defect is found in — the binary host a configuration had turned off was
#: found by reading every `emerge` line of one — so the tail is the fallback
#: rather than the rule.
FULL_LOG_BYTES: Final[int] = 32 * 1024 * 1024


def console_command(directory: str, limit: int = FULL_LOG_BYTES) -> str:
    """Print the results as one base64 line between two markers.

    A guest on the cluster writes its disks to shared storage the workstation
    cannot read, and the API offers no way to download a volume, so the console
    is the only channel back. Compressed first: an install log is megabytes and
    base64 adds a third again.

    Neither marker appears whole in the command. The shell echoes the line it
    was given, so a reader waiting for the closing marker matched the echo and
    returned before the archive had started: every run failed in ninety seconds
    with `the console result is not base64`.
    """
    stem, opened = CONSOLE_OPEN.rsplit("_", 1)
    closed = CONSOLE_CLOSE.rsplit("_", 1)[1]
    log = f"{directory}/install.txt"
    return (
        f"printf '{stem}_%s\\n' {opened}; "
        f"tail -c {LOG_TAIL_BYTES} {log} > {directory}/{LOG_TAIL} 2>/dev/null || true; "
        f"if [ \"$(wc -c < {log} 2>/dev/null || echo 0)\" -le {limit} ]; "
        "then whole=; else whole=--exclude=./install.txt; fi; "
        f"tar cz $whole -C {directory} . | base64 -w0; "
        f"echo; printf '{stem}_%s\\n' {closed}"
    )


def read_console(said: bytes) -> dict[str, bytes]:
    """The archive out of what the console printed."""
    if len(said) > RESULT_BUFFER_BYTES:
        raise ResultError("the console result exceeds the configured size limit")
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
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                if member.size > RESULT_BUFFER_BYTES - total:
                    raise ResultError("the console result files exceed the configured size limit")
                extracted = archive.extractfile(member)
                if extracted is not None:
                    data = extracted.read()
                    total += len(data)
                    files[member.name.removeprefix("./")] = data
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
