# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from typing import cast

import pytest

from gentoo_install.errors import InvalidSize, UnalignedSize
from gentoo_install.model.size import (
    DEFAULT_ALIGNMENT,
    SECTOR_512,
    SECTOR_4K,
    SectorSize,
    Size,
    Unit,
)

MIB = 1024 * 1024
GIB = 1024 * MIB


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("0", 0),
        ("4096", 4096),
        ("512MiB", 512 * MIB),
        ("512M", 512 * MIB),
        ("512MB", 512 * 1000 * 1000),
        ("20G", 20 * GIB),
        ("1.5GiB", 1536 * MIB),
        ("  8 GiB ", 8 * GIB),
        ("1TB", 1000**4),
    ],
)
def test_parse_accepts_si_and_iec_literals(literal: str, expected: int) -> None:
    assert Size.parse(literal).bytes == expected


@pytest.mark.parametrize("literal", ["", "MiB", "-1", "1.5B", "8 quatloos", "1,5GiB", "0x10"])
def test_parse_rejects_malformed_literals(literal: str) -> None:
    with pytest.raises(InvalidSize):
        Size.parse(literal)


def test_a_bare_m_suffix_is_binary_because_partitioning_tools_read_it_that_way() -> None:
    assert Size.parse("100M") == Size.parse("100MiB")
    assert Size.parse("100M") != Size.parse("100MB")


def test_negative_sizes_are_rejected_at_construction() -> None:
    with pytest.raises(InvalidSize):
        Size(-1)


@pytest.mark.parametrize("value", [True, cast(int, 1.5), cast(int, float("nan"))])
def test_sizes_are_whole_byte_counts_at_runtime(value: int) -> None:
    with pytest.raises(InvalidSize, match="whole number of bytes"):
        Size(value)


def test_arithmetic_keeps_the_type_and_refuses_to_go_negative() -> None:
    assert Size.parse("1GiB") + Size.parse("1GiB") == Size.parse("2GiB")
    assert Size.parse("1GiB") - Size.parse("512MiB") == Size.parse("512MiB")
    assert Size.parse("512MiB") * 2 == Size.parse("1GiB")
    assert 2 * Size.parse("512MiB") == Size.parse("1GiB")
    assert Size.parse("1GiB") // 4 == Size.parse("256MiB")
    with pytest.raises(InvalidSize):
        Size.parse("1MiB") - Size.parse("2MiB")
    with pytest.raises(InvalidSize):
        Size.parse("1MiB") // 0


def test_sizes_order_by_bytes() -> None:
    assert Size.parse("1GiB") > Size.parse("1GB")
    assert sorted([Size.parse("1GiB"), Size.parse("1MiB")]) == [
        Size.parse("1MiB"),
        Size.parse("1GiB"),
    ]


def test_alignment_rounds_in_the_named_direction() -> None:
    unaligned = Size.parse("1MiB") + Size(1)
    assert not unaligned.is_aligned()
    assert unaligned.align_up() == Size.parse("2MiB")
    assert unaligned.align_down() == Size.parse("1MiB")
    assert Size.parse("2MiB").align_up() == Size.parse("2MiB")
    assert DEFAULT_ALIGNMENT == MIB


def test_alignment_of_zero_or_less_is_rejected() -> None:
    with pytest.raises(InvalidSize):
        Size.parse("1MiB").align_up(0)
    with pytest.raises(InvalidSize):
        Size.parse("1MiB").is_aligned(-4096)


def test_sector_conversion_round_trips_and_refuses_partial_sectors() -> None:
    assert Size.from_sectors(2048, SECTOR_512) == Size.parse("1MiB")
    assert Size.parse("1MiB").sectors(SECTOR_512) == 2048
    assert Size.parse("1MiB").sectors(SECTOR_4K) == 256
    with pytest.raises(UnalignedSize):
        Size(513).sectors(SECTOR_512)


def test_sector_size_must_be_a_positive_multiple_of_512() -> None:
    assert SectorSize(4096).value == 4096
    for bad in (0, -512, 500):
        with pytest.raises(InvalidSize):
            SectorSize(bad)


def test_gpt_tail_is_reserved_on_both_sector_sizes() -> None:
    device = Size.parse("1GiB")
    assert device.gpt_last_usable(SECTOR_512) == device - Size(33 * 512)
    assert device.gpt_last_usable(SECTOR_4K) == device - Size(5 * 4096)


def test_a_partition_ending_inside_the_backup_gpt_does_not_fit() -> None:
    device = Size.parse("1GiB")
    last = device.gpt_last_usable(SECTOR_512)
    assert last.fits_in_gpt(device, SECTOR_512)
    assert not (last + Size(1)).fits_in_gpt(device, SECTOR_512)
    assert not device.fits_in_gpt(device, SECTOR_512)


def test_a_device_smaller_than_the_gpt_itself_is_rejected() -> None:
    with pytest.raises(InvalidSize):
        Size(1024).gpt_last_usable(SECTOR_4K)


def test_of_rejects_a_fraction_that_is_not_a_whole_number_of_bytes() -> None:
    assert Size.of(1.5, Unit.KIB) == Size(1536)
    with pytest.raises(InvalidSize):
        Size.of(1.5, Unit.B)


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (Size(0), "0B"),
        (Size(1), "1B"),
        (Size(1023), "1023B"),
        (Size.parse("1KiB"), "1KiB"),
        (Size.parse("512MiB"), "512MiB"),
        (Size.parse("2GiB"), "2GiB"),
        (Size.parse("1MB"), "1000000B"),
    ],
)
def test_str_uses_the_largest_unit_that_stays_exact(size: Size, text: str) -> None:
    assert str(size) == text


def test_a_size_for_sgdisk_and_lvm_carries_a_one_letter_unit() -> None:
    """`lvcreate --size` accepts only `b s k m g t p e`, so `512MiB` is
    rejected; `sgdisk --new` has no byte suffix at all and reads a bare number
    as sectors."""
    assert Size.parse("512MiB").single_letter() == "512M"
    assert Size.parse("20GiB").single_letter() == "20G"
    assert Size.parse("4KiB").single_letter() == "4K"


def test_a_size_that_is_not_a_whole_kib_rounds_up_rather_than_saying_bytes() -> None:
    """`sgdisk --new=1:0:+1500000000B` asked for 1.5 billion sectors and failed
    on a disk two thousand times smaller. Neither tool can express the
    remainder, so the next whole KiB is the honest answer."""
    rounded = Size.parse("1500000000B").single_letter()
    assert rounded == "1464844K"
    assert Size.parse(rounded).bytes >= 1500000000
    assert Size.parse(rounded).bytes - 1500000000 < 1024


def test_the_readable_form_still_uses_the_full_iec_unit() -> None:
    """`parted` takes `MiB`, and so does a person reading the plan."""
    assert str(Size.parse("512MiB")) == "512MiB"


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("4.1MB", 4_100_000),
        ("1.1GB", 1_100_000_000),
        ("1.0001MB", 1_000_100),
        ("2.5GiB", 2_684_354_560),
        ("0.5MiB", 524_288),
    ],
)
def test_a_decimal_size_is_exact(literal: str, expected: int) -> None:
    """`float` multiplication left a fractional artifact, so `4.1MB` -- exactly
    4,100,000 bytes -- was rejected as not a whole number of them."""
    assert Size.parse(literal).bytes == expected


def test_a_size_that_really_is_fractional_is_still_refused() -> None:
    """Half a byte is not a size, and exact arithmetic must not make it one."""
    with pytest.raises(InvalidSize):
        Size.parse("1.5B")
