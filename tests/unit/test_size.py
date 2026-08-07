from __future__ import annotations

import pytest

from gentoo_install.errors import InvalidSize, UnalignedSize
from gentoo_install.model.size import (
    DEFAULT_ALIGNMENT,
    GPT_RESERVED_SECTORS,
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
    assert device.gpt_last_usable(SECTOR_512) == device - Size(GPT_RESERVED_SECTORS * 512)
    assert device.gpt_last_usable(SECTOR_4K) == device - Size(GPT_RESERVED_SECTORS * 4096)


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
