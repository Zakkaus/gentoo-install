"""Byte counts and sector arithmetic.

Every length in the disk model is a `Size`. Passing raw integers around is how
sector counts and byte counts get confused, and a partition table built from a
confused number is only discovered after `mkfs`.
"""

from __future__ import annotations

from decimal import Decimal

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from ..errors import InvalidSize, UnalignedSize


class Unit(Enum):
    """Suffixes accepted in a size literal.

    SI suffixes are powers of 1000 and IEC suffixes are powers of 1024, matching
    what `sgdisk`, `parted` and the Handbook use. A bare `M` is IEC, because every
    partitioning tool in this project's path treats it that way.
    """

    B = 1
    KB = 1000
    MB = 1000**2
    GB = 1000**3
    TB = 1000**4
    PB = 1000**5
    KIB = 1024
    MIB = 1024**2
    GIB = 1024**3
    TIB = 1024**4
    PIB = 1024**5

    @property
    def suffix(self) -> str:
        return {
            Unit.B: "B",
            Unit.KB: "kB",
            Unit.MB: "MB",
            Unit.GB: "GB",
            Unit.TB: "TB",
            Unit.PB: "PB",
            Unit.KIB: "KiB",
            Unit.MIB: "MiB",
            Unit.GIB: "GiB",
            Unit.TIB: "TiB",
            Unit.PIB: "PiB",
        }[self]


_SUFFIXES: Final[dict[str, Unit]] = {
    "": Unit.B,
    "b": Unit.B,
    "k": Unit.KIB,
    "kb": Unit.KB,
    "kib": Unit.KIB,
    "m": Unit.MIB,
    "mb": Unit.MB,
    "mib": Unit.MIB,
    "g": Unit.GIB,
    "gb": Unit.GB,
    "gib": Unit.GIB,
    "t": Unit.TIB,
    "tb": Unit.TB,
    "tib": Unit.TIB,
    "p": Unit.PIB,
    "pb": Unit.PB,
    "pib": Unit.PIB,
}

_LITERAL: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<suffix>[a-zA-Z]*)\s*$"
)

_IEC_LADDER: Final[tuple[Unit, ...]] = (
    Unit.PIB,
    Unit.TIB,
    Unit.GIB,
    Unit.MIB,
    Unit.KIB,
)

#: Partition starts are aligned to this by default. It is the alignment `sgdisk`
#: and `parted` pick, and it keeps 4K-native and RAID-backed devices aligned too.
DEFAULT_ALIGNMENT: Final[int] = 1024 * 1024

#: A GPT reserves LBA 1 for the header and LBAs 2..33 for the partition array, at
#: both ends of the device. Anything overlapping the trailing copy is unbootable.
GPT_RESERVED_SECTORS: Final[int] = 33


@dataclass(frozen=True, order=True)
class SectorSize:
    """Logical sector size of a block device, in bytes."""

    value: int

    def __post_init__(self) -> None:
        if self.value <= 0 or self.value % 512 != 0:
            raise InvalidSize(f"sector size must be a positive multiple of 512, got {self.value}")

    def __str__(self) -> str:
        return f"{self.value}B"


#: Reported by every device this installer supports today; 4Kn devices report 4096.
SECTOR_512: Final[SectorSize] = SectorSize(512)
SECTOR_4K: Final[SectorSize] = SectorSize(4096)


@dataclass(frozen=True, order=True)
class Size:
    """A non-negative number of bytes."""

    bytes: int

    def __post_init__(self) -> None:
        if self.bytes < 0:
            raise InvalidSize(f"size cannot be negative, got {self.bytes}")

    @classmethod
    def of(cls, amount: Decimal | int | float, unit: Unit) -> Size:
        """`Decimal`, not `float`: 4.1 MB is exactly 4,100,000 bytes, and
        binary multiplication left a fractional artifact that the whole-byte
        check then rejected."""
        scaled = Decimal(str(amount)) * unit.value
        if scaled != scaled.to_integral_value():
            raise InvalidSize(f"{amount}{unit.suffix} is not a whole number of bytes")
        return cls(int(scaled))

    @classmethod
    def parse(cls, literal: str) -> Size:
        """Read a size literal such as `512MiB`, `20G`, `1.5TB` or `4096`."""
        match = _LITERAL.match(literal)
        if match is None:
            raise InvalidSize(f"{literal!r} is not a size literal")
        suffix = match["suffix"].lower()
        unit = _SUFFIXES.get(suffix)
        if unit is None:
            raise InvalidSize(f"{literal!r} uses an unknown unit {match['suffix']!r}")
        return cls.of(Decimal(match["number"]), unit)

    @classmethod
    def from_sectors(cls, count: int, sector: SectorSize) -> Size:
        if count < 0:
            raise InvalidSize(f"sector count cannot be negative, got {count}")
        return cls(count * sector.value)

    def sectors(self, sector: SectorSize) -> int:
        """Whole sectors covered. Raises when the size is not a sector multiple."""
        if self.bytes % sector.value != 0:
            raise UnalignedSize(f"{self} is not a multiple of {sector}")
        return self.bytes // sector.value

    def is_aligned(self, alignment: int = DEFAULT_ALIGNMENT) -> bool:
        if alignment <= 0:
            raise InvalidSize(f"alignment must be positive, got {alignment}")
        return self.bytes % alignment == 0

    def align_up(self, alignment: int = DEFAULT_ALIGNMENT) -> Size:
        if alignment <= 0:
            raise InvalidSize(f"alignment must be positive, got {alignment}")
        remainder = self.bytes % alignment
        return self if remainder == 0 else Size(self.bytes + alignment - remainder)

    def align_down(self, alignment: int = DEFAULT_ALIGNMENT) -> Size:
        if alignment <= 0:
            raise InvalidSize(f"alignment must be positive, got {alignment}")
        return Size(self.bytes - self.bytes % alignment)

    def gpt_last_usable(self, sector: SectorSize) -> Size:
        """Offset one past the last byte a partition may occupy on this device."""
        reserved = Size.from_sectors(GPT_RESERVED_SECTORS, sector)
        if self.bytes < reserved.bytes:
            raise InvalidSize(f"{self} is too small to hold a GPT")
        return Size(self.bytes - reserved.bytes)

    def fits_in_gpt(self, device: Size, sector: SectorSize) -> bool:
        """Whether a partition ending here clears the trailing GPT copy."""
        return self <= device.gpt_last_usable(sector)

    def __add__(self, other: Size) -> Size:
        return Size(self.bytes + other.bytes)

    def __sub__(self, other: Size) -> Size:
        if other.bytes > self.bytes:
            raise InvalidSize(f"{self} - {other} would be negative")
        return Size(self.bytes - other.bytes)

    def __mul__(self, factor: int) -> Size:
        if factor < 0:
            raise InvalidSize(f"cannot multiply {self} by {factor}")
        return Size(self.bytes * factor)

    __rmul__ = __mul__

    def __floordiv__(self, divisor: int) -> Size:
        if divisor <= 0:
            raise InvalidSize(f"cannot divide {self} by {divisor}")
        return Size(self.bytes // divisor)

    def __str__(self) -> str:
        """Largest IEC unit that keeps the value exact, otherwise bytes.

        For reading and for `parted`, which takes `MiB`. `sgdisk` and
        `lvcreate` do not: use `single_letter()` for those.
        """
        for unit in _IEC_LADDER:
            if self.bytes >= unit.value and self.bytes % unit.value == 0:
                return f"{self.bytes // unit.value}{unit.suffix}"
        return f"{self.bytes}B"

    def single_letter(self) -> str:
        """The size with a one-letter unit, which is what two tools require.

        `lvcreate --size` accepts only `b s k m g t p e`, so `512MiB` is
        rejected outright. `sgdisk --new` is worse: it reads a bare number as
        *sectors* and has no byte suffix, so `+1500000000B` asked for a
        partition of 1.5 billion sectors and failed on a disk 2000 times
        smaller. A value that is not a whole KiB is rounded up to one, because
        neither tool can express the remainder.
        """
        for unit in _IEC_LADDER:
            if self.bytes >= unit.value and self.bytes % unit.value == 0:
                return f"{self.bytes // unit.value}{unit.suffix[0]}"
        return f"{-(-self.bytes // Unit.KIB.value)}K"


ZERO: Final[Size] = Size(0)
