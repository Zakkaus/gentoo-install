"""A QR code for a short URL, drawn on the console.

Byte mode, error correction level M, versions 1 to 6. That is every code this
installer needs: the longest thing it shows is a pastebin address. Levels other
than M and versions above 6 are left out rather than written untested, and
version 7 is where the format gains a version-information block this has no
reason to carry.

Written here because the only alternative is a third-party package, and the
installer runs off a medium that has none. The output is checked against
`qrencode` in `tests/unit/test_qr.py`, module for module.
"""

from __future__ import annotations

from typing import Final

from ..errors import ConfigError

#: Data codewords, error-correction codewords per block, and blocks, for level
#: M. Each row multiplies out to the version's total codeword count, which is
#: the check that catches a mistyped row.
BLOCKS: Final[dict[int, tuple[int, int, int]]] = {
    1: (16, 10, 1),
    2: (28, 16, 1),
    3: (44, 26, 1),
    4: (32, 18, 2),
    5: (43, 24, 2),
    6: (27, 16, 4),
}

#: Where the alignment patterns are centred. Version 1 has none.
ALIGNMENT: Final[dict[int, tuple[int, ...]]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
}

#: Bits of padding after the last codeword. Version 1 needs none.
REMAINDER: Final[dict[int, int]] = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7}

#: Level M as the format information spells it.
LEVEL_BITS: Final[int] = 0b00

#: The two bytes the standard pads with, alternating, after the terminator.
PADDING: Final[tuple[int, int]] = (0xEC, 0x11)

_GENERATOR: Final[int] = 0x11D
_FORMAT_GENERATOR: Final[int] = 0b101_0011_0111
_FORMAT_MASK: Final[int] = 0b101_0100_0001_0010

Matrix = tuple[tuple[bool, ...], ...]


class TooLongForAQrCode(ConfigError):
    """The text does not fit the largest code this module draws."""


def capacity(version: int) -> int:
    """Bytes that fit, after the mode and the length that precede them."""
    data, _, blocks = BLOCKS[version]
    return (data * blocks * 8 - 12) // 8


def encode(text: str) -> Matrix:
    """The module grid, without the quiet zone around it."""
    payload = text.encode()
    version = _smallest(len(payload))
    codewords = _codewords(payload, version)
    reserved = _reserve(version)
    drawn = _place(codewords, version, reserved)
    mask = min(range(8), key=lambda one: _penalty(_masked(drawn, reserved, one, version)))
    return _masked(drawn, reserved, mask, version)


def render(matrix: Matrix, quiet: int = 4, dark: str = "██", light: str = "  ") -> list[str]:
    """The grid as lines of text.

    Two cells per module, because a terminal cell is about half as wide as it
    is tall and a scanner needs the code square. The full block is what fills a
    cell; a `#` leaves gaps a camera reads as light.
    """
    width = len(matrix) + quiet * 2
    blank = [light * width]
    return (
        blank * quiet
        + [light * quiet + "".join(dark if cell else light for cell in row) + light * quiet
           for row in matrix]
        + blank * quiet
    )


#: One character per two rows of modules: full, top only, bottom only, neither.
#: The pairing is what makes the code square on a terminal cell that is about
#: twice as tall as it is wide, at half the width of one character per module.
HALVES: Final[dict[tuple[bool, bool], str]] = {
    (True, True): "\u2588",
    (True, False): "\u2580",
    (False, True): "\u2584",
    (False, False): " ",
}


def halved(matrix: Matrix, quiet: int = 4) -> list[str]:
    """The grid as half-height lines, quiet zone included.

    The caller sets the colours. Drawn in a terminal's own foreground these are
    light modules on a dark screen, which is the code inverted; most scanners
    read that and some do not, so `cli.py` forces black on white.
    """
    width = len(matrix) + quiet * 2
    blank = (False,) * width
    padded = [blank] * quiet
    padded += [(False,) * quiet + row + (False,) * quiet for row in matrix]
    padded += [blank] * quiet
    if len(padded) % 2:
        padded.append(blank)
    return [
        "".join(HALVES[(top, bottom)] for top, bottom in zip(padded[at], padded[at + 1]))
        for at in range(0, len(padded), 2)
    ]


def _smallest(length: int) -> int:
    for version in sorted(BLOCKS):
        if length <= capacity(version):
            return version
    raise TooLongForAQrCode(
        f"{length} bytes does not fit a version {max(BLOCKS)} code, which holds "
        f"{capacity(max(BLOCKS))}"
    )


def _codewords(payload: bytes, version: int) -> list[int]:
    """The data and its error correction, interleaved as the standard orders."""
    data, ec_count, blocks = BLOCKS[version]
    bits = _bitstream(payload, data * blocks)
    grouped = [bits[index * data : (index + 1) * data] for index in range(blocks)]
    corrections = [_remainder(one, ec_count) for one in grouped]
    out: list[int] = []
    for column in range(data):
        out += [one[column] for one in grouped]
    for column in range(ec_count):
        out += [one[column] for one in corrections]
    return out


def _bitstream(payload: bytes, total: int) -> list[int]:
    """Mode, length, the bytes themselves, then the padding, as codewords."""
    bits = [0, 1, 0, 0]
    bits += _as_bits(len(payload), 8)
    for byte in payload:
        bits += _as_bits(byte, 8)
    bits += [0] * min(4, total * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    out = [int("".join(str(one) for one in bits[at : at + 8]), 2) for at in range(0, len(bits), 8)]
    while len(out) < total:
        out.append(PADDING[(len(out) - len(bits) // 8) % 2])
    return out


def _as_bits(value: int, width: int) -> list[int]:
    return [(value >> shift) & 1 for shift in range(width - 1, -1, -1)]


def _remainder(data: list[int], count: int) -> list[int]:
    """The Reed-Solomon remainder, over GF(256) with the QR primitive."""
    out = list(data) + [0] * count
    generator = _generator(count)
    for at in range(len(data)):
        lead = out[at]
        if not lead:
            continue
        for offset, coefficient in enumerate(generator):
            out[at + offset] ^= _multiply(coefficient, lead)
    return out[len(data) :]


_EXP: Final[list[int]] = [0] * 512
_LOG: Final[list[int]] = [0] * 256


def _build_tables() -> None:
    value = 1
    for power in range(255):
        _EXP[power] = value
        _LOG[value] = power
        value <<= 1
        if value & 0x100:
            value ^= _GENERATOR
    for power in range(255, 512):
        _EXP[power] = _EXP[power - 255]


_build_tables()


def _multiply(left: int, right: int) -> int:
    if not left or not right:
        return 0
    return _EXP[_LOG[left] + _LOG[right]]


def _generator(count: int) -> list[int]:
    out = [1]
    for power in range(count):
        out = _times(out, [1, _EXP[power]])
    return out


def _times(left: list[int], right: list[int]) -> list[int]:
    out = [0] * (len(left) + len(right) - 1)
    for index, one in enumerate(left):
        for offset, other in enumerate(right):
            out[index + offset] ^= _multiply(one, other)
    return out


def _size(version: int) -> int:
    return version * 4 + 17


def _reserve(version: int) -> list[list[bool]]:
    """Which cells the function patterns own, so the data skips them."""
    size = _size(version)
    taken = [[False] * size for _ in range(size)]
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        for row in range(-1, 8):
            for column in range(-1, 8):
                if 0 <= top + row < size and 0 <= left + column < size:
                    taken[top + row][left + column] = True
    for centre in ALIGNMENT[version]:
        for other in ALIGNMENT[version]:
            if _is_a_finder(centre, other, size):
                continue
            for row in range(-2, 3):
                for column in range(-2, 3):
                    taken[centre + row][other + column] = True
    for at in range(size):
        taken[6][at] = True
        taken[at][6] = True
    for at in range(9):
        taken[8][at] = True
        taken[at][8] = True
    for at in range(8):
        taken[8][size - 1 - at] = True
        taken[size - 1 - at][8] = True
    return taken


def _is_a_finder(row: int, column: int, size: int) -> bool:
    """The three corners already carry a finder, so no alignment goes there."""
    return (
        (row == 6 and column == 6)
        or (row == 6 and column == size - 7)
        or (row == size - 7 and column == 6)
    )


def _patterns(version: int) -> list[list[bool]]:
    """Everything that is not data: finders, alignment, timing, dark module."""
    size = _size(version)
    grid = [[False] * size for _ in range(size)]
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        for row in range(7):
            for column in range(7):
                edge = row in (0, 6) or column in (0, 6)
                middle = 2 <= row <= 4 and 2 <= column <= 4
                grid[top + row][left + column] = edge or middle
    for centre in ALIGNMENT[version]:
        for other in ALIGNMENT[version]:
            if _is_a_finder(centre, other, size):
                continue
            for row in range(-2, 3):
                for column in range(-2, 3):
                    ring = max(abs(row), abs(column))
                    grid[centre + row][other + column] = ring != 1
    for at in range(8, size - 8):
        grid[6][at] = at % 2 == 0
        grid[at][6] = at % 2 == 0
    grid[size - 8][8] = True
    return grid


def _place(codewords: list[int], version: int, taken: list[list[bool]]) -> list[list[bool]]:
    """The zigzag: two columns at a time, right to left, alternating direction."""
    size = _size(version)
    grid = _patterns(version)
    bits = [bit for word in codewords for bit in _as_bits(word, 8)]
    bits += [0] * REMAINDER[version]
    supply = iter(bits)
    column = size - 1
    upward = True
    while column > 0:
        if column == 6:
            # The vertical timing pattern is a column of its own, not a pair.
            column -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for offset in (0, 1):
                at = column - offset
                if taken[row][at]:
                    continue
                grid[row][at] = next(supply, 0) == 1
        column -= 2
        upward = not upward
    return grid


def _masked(grid: list[list[bool]], taken: list[list[bool]], mask: int, version: int) -> Matrix:
    size = _size(version)
    out = [list(row) for row in grid]
    for row in range(size):
        for column in range(size):
            if not taken[row][column] and _mask_bit(mask, row, column):
                out[row][column] = not out[row][column]
    _write_format(out, mask, size)
    return tuple(tuple(row) for row in out)


def _mask_bit(mask: int, row: int, column: int) -> bool:
    if mask == 0:
        return (row + column) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return column % 3 == 0
    if mask == 3:
        return (row + column) % 3 == 0
    if mask == 4:
        return (row // 2 + column // 3) % 2 == 0
    if mask == 5:
        return (row * column) % 2 + (row * column) % 3 == 0
    if mask == 6:
        return ((row * column) % 2 + (row * column) % 3) % 2 == 0
    return ((row + column) % 2 + (row * column) % 3) % 2 == 0


def _write_format(grid: list[list[bool]], mask: int, size: int) -> None:
    bits = _format_bits(mask)
    # The two copies run in opposite directions: the corner one starts at the
    # most significant bit, the split one at the least.
    for at in range(15):
        bit = bits[at]
        if at < 6:
            grid[8][at] = bit
        elif at == 6:
            grid[8][7] = bit
        elif at == 7:
            grid[8][8] = bit
        elif at == 8:
            grid[7][8] = bit
        else:
            grid[14 - at][8] = bit
    # The second copy runs up column 8 for seven bits, then along row 8. Seven,
    # not eight: the module below that run is the dark one, which is not
    # format information and never changes.
    for at in range(15):
        if at < 7:
            grid[size - 1 - at][8] = bits[at]
        else:
            grid[8][size - 15 + at] = bits[at]


def _format_bits(mask: int) -> list[bool]:
    value = (LEVEL_BITS << 3) | mask
    remainder = value << 10
    for shift in range(4, -1, -1):
        if remainder & (1 << (shift + 10)):
            remainder ^= _FORMAT_GENERATOR << shift
    return [bool((((value << 10) | remainder) ^ _FORMAT_MASK) >> shift & 1) for shift in range(14, -1, -1)]


def _penalty(matrix: Matrix) -> int:
    """The four rules the standard scores a mask by, lowest total winning.

    Written to match `libqrencode`, because that is what the fixtures come
    from: run lengths per line for rules one and three, every 2x2 block for
    rule two, and a rounded dark ratio for rule four. Any of the eight masks
    produces a scannable code, so this decides which one, not whether it works.
    """
    return _runs_and_finders(matrix) + _squares(matrix) + _balance(matrix)


def _runs_and_finders(matrix: Matrix) -> int:
    total = 0
    for line in (*matrix, *(tuple(one) for one in zip(*matrix))):
        total += _line_penalty(_run_lengths(line))
    return total


def _run_lengths(line: tuple[bool, ...]) -> list[int]:
    """Run lengths, with a sentinel first when the line starts dark.

    The sentinel is what puts every dark run at an odd index, which is how the
    finder-like rule below finds the middle of a 1:1:3:1:1 without checking
    colours again.
    """
    runs = [-1, 1] if line[0] else [1]
    for at in range(1, len(line)):
        if line[at] == line[at - 1]:
            runs[-1] += 1
        else:
            runs.append(1)
    return runs


def _line_penalty(runs: list[int]) -> int:
    total = 0
    for at, run in enumerate(runs):
        if run >= 5:
            total += 3 + run - 5
        if at % 2 == 0 or not 3 <= at < len(runs) - 2 or run % 3:
            continue
        unit = run // 3
        if [runs[at - 2], runs[at - 1], runs[at + 1], runs[at + 2]] != [unit] * 4:
            continue
        before = at == 3 or runs[at - 3] >= 4 * unit
        after = at + 4 >= len(runs) or runs[at + 3] >= 4 * unit
        if before or after:
            total += 40
    return total


def _squares(matrix: Matrix) -> int:
    total = 0
    for row in range(len(matrix) - 1):
        for column in range(len(matrix) - 1):
            block = {
                matrix[row][column],
                matrix[row][column + 1],
                matrix[row + 1][column],
                matrix[row + 1][column + 1],
            }
            total += 3 if len(block) == 1 else 0
    return total


#: The two shapes rule three looks for, in both directions.
_FINDER_LIKE: Final[tuple[tuple[bool, ...], ...]] = (
    (True, False, True, True, True, False, True, False, False, False, False),
    (False, False, False, False, True, False, True, True, True, False, True),
)


def _finders(matrix: Matrix) -> int:
    total = 0
    for line in (*matrix, *zip(*matrix)):
        for at in range(len(line) - 10):
            window = tuple(line[at : at + 11])
            total += 40 if window in _FINDER_LIKE else 0
    return total


def _balance(matrix: Matrix) -> int:
    """How far the proportion of dark modules is from half, in steps of 5%.

    The ratio is rounded rather than truncated, which is what `libqrencode`
    does and what decides the mask on a symbol sitting near a 5% boundary.
    """
    cells = len(matrix) ** 2
    dark = sum(1 for row in matrix for cell in row if cell)
    ratio = (200 * dark + cells) // cells // 2
    return abs(ratio - 50) // 5 * 10
