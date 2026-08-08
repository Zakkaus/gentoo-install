"""The QR encoder against `qrencode`, module for module.

The fixtures were produced by `qrencode -l M -m 0 -t ASCII`, one at the byte
ceiling of every version this module draws. A code that differs from a real
encoder anywhere does not scan, and nothing about the output says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_install.model import qr

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "qr"


def golden(path: Path) -> tuple[str, qr.Matrix]:
    text, *rows = path.read_text().splitlines()
    return text, tuple(tuple(cell == "#" for cell in row) for row in rows)


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.txt")), ids=lambda path: path.stem)
def test_the_matrix_is_the_one_qrencode_produces(path: Path) -> None:
    text, expected = golden(path)
    assert qr.encode(text) == expected


def test_every_version_the_table_holds_has_a_fixture() -> None:
    """A version with no fixture is a row of `BLOCKS` nothing checks, and a
    mistyped row there produces a code that is wrong and still looks like one."""
    covered = {(len(golden(path)[1]) - 17) // 4 for path in FIXTURES.glob("*.txt")}
    assert covered == set(qr.BLOCKS)


@pytest.mark.parametrize("version", sorted(qr.BLOCKS))
def test_each_row_multiplies_out_to_the_version_total(version: int) -> None:
    """The check that catches a mistyped block table without an encoder to
    compare against: data plus correction, times blocks, is the version's own
    codeword count."""
    totals = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172}
    data, correction, blocks = qr.BLOCKS[version]
    assert (data + correction) * blocks == totals[version]


def test_text_too_long_for_the_largest_version_is_refused() -> None:
    with pytest.raises(qr.TooLongForAQrCode, match="version 6"):
        qr.encode("z" * (qr.capacity(6) + 1))


def test_the_drawing_is_square_and_has_its_quiet_zone() -> None:
    """A code with no margin does not scan, and two cells per module is what
    makes a terminal cell square enough for a camera."""
    lines = qr.render(qr.encode("https://paste.gentoozh.org/abc"))
    assert len(lines) == 29 + 8
    assert all(len(line) == (29 + 8) * 2 for line in lines)
    assert lines[0].strip() == ""
    assert lines[-1].strip() == ""


def test_the_drawing_can_be_asked_for_plain_text() -> None:
    """A console with no block glyph still gets something, and the caller
    prints the address either way."""
    lines = qr.render(qr.encode("g"), quiet=1, dark="#", light=".")
    assert set("".join(lines)) == {"#", "."}


def mask_of(matrix: qr.Matrix) -> int:
    """Which mask a symbol declares, read back out of its format information."""
    read = (
        [matrix[8][at] for at in range(6)]
        + [matrix[8][7], matrix[8][8], matrix[7][8]]
        + [matrix[14 - at][8] for at in range(9, 15)]
    )
    bits = "".join("1" if one else "0" for one in read)
    return next(
        one
        for one in range(8)
        if "".join("1" if bit else "0" for bit in qr._format_bits(one)) == bits
    )


def test_the_fixtures_exercise_every_mask_but_the_one_nothing_selects() -> None:
    """Mask 1 blanks every other row, which is the worst score a symbol can
    have for run length, so no input reaches it: four thousand addresses were
    tried. The other seven are each checked against a real encoder above."""
    exercised = {mask_of(golden(path)[1]) for path in FIXTURES.glob("*.txt")}
    assert exercised == {0, 2, 3, 4, 5, 6, 7}


def test_a_mask_is_chosen_by_the_lowest_penalty() -> None:
    """The rules are libqrencode's, so the choice matches it; a symbol under
    any of the eight still scans, which is why this is a preference and not a
    correctness rule."""
    text = "https://paste.gentoozh.org/d16L41P1-xb.log"
    version = 3
    reserved = qr._reserve(version)
    drawn = qr._place(qr._codewords(text.encode(), version), version, reserved)
    scored = [qr._penalty(qr._masked(drawn, reserved, one, version)) for one in range(8)]
    assert mask_of(qr.encode(text)) == min(range(8), key=lambda one: scored[one])


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.txt")), ids=lambda path: path.stem)
def test_the_half_height_drawing_still_holds_every_module(path: Path) -> None:
    """Two rows of modules per line halves the width, and a pairing that drops
    or shifts a row produces a picture that looks like a code and is not one."""
    text, expected = golden(path)
    quiet = 4
    lines = qr.halved(qr.encode(text), quiet=quiet)
    back = [[False] * len(lines[0]) for _ in range(len(lines) * 2)]
    for row, line in enumerate(lines):
        for column, cell in enumerate(line):
            top, bottom = next(pair for pair, glyph in qr.HALVES.items() if glyph == cell)
            back[row * 2][column] = top
            back[row * 2 + 1][column] = bottom
    inner = [row[quiet : len(expected) + quiet] for row in back[quiet : len(expected) + quiet]]
    assert [tuple(row) for row in inner] == [tuple(row) for row in expected]
