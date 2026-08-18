# SPDX-License-Identifier: GPL-2.0-or-later
"""Stream a prepared disk image onto a whole disk."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..model.config import ImageFormat, InstallConfig
from .operations import Context, Operation, Stage


#: One reader per supported source encoding. Appending the source path keeps it
#: out of a shell command, so spaces and metacharacters remain data.
READERS: Final[dict[ImageFormat, tuple[str, ...]]] = {
    ImageFormat.RAW: ("cat", "--"),
    ImageFormat.GZIP: ("gzip", "--decompress", "--stdout", "--"),
    ImageFormat.XZ: ("xz", "--decompress", "--stdout", "--"),
    ImageFormat.ZSTD: ("zstd", "--decompress", "--stdout", "--"),
    ImageFormat.TAR: ("tar", "--extract", "--to-stdout", "--file"),
}


def reader(source: str, source_format: ImageFormat) -> tuple[str, ...]:
    """The command that emits unencoded image bytes to standard output."""
    return (*READERS[source_format], source)


def required_commands(source_format: ImageFormat) -> frozenset[str]:
    """The reader and sink needed for one source format."""
    return frozenset((READERS[source_format][0], "dd"))

@dataclass(frozen=True, kw_only=True)
class WriteImage(Operation):
    """Write the image stream and leave its layout and bootloader intact."""

    stage: Stage = Stage.PARTITION
    source: str
    source_format: ImageFormat
    destination: str

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "stream the {} image {} onto {}", (
            self.source_format.value,
            self.source,
            self.destination,
        )

    def apply(self, context: Context) -> None:
        context.pipe(
            reader(self.source, self.source_format),
            ("dd", f"of={self.destination}", "bs=4M", "conv=fsync"),
        )

    def required_host_commands(self) -> frozenset[str]:
        return required_commands(self.source_format)


def build(config: InstallConfig) -> tuple[Operation, ...]:
    """The complete dd plan: one streamed write and no target configuration."""
    return (
        WriteImage(
            source=config.disk.source,
            source_format=config.disk.source_format,
            destination=config.disk.destination,
        ),
    )
