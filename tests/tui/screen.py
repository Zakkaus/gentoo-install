# SPDX-License-Identifier: GPL-2.0-or-later
"""What a terminal would be showing, rebuilt from what was written to it.

An agent driving the interface has to read the screen, not the byte stream:
`ncurses` repaints by moving the cursor and overwriting cells, so the same
line appears many times in the stream and only the last write is on screen.

Only what `ncurses` emits is understood, which is a small part of the
standard: absolute cursor moves, the three erases, and the attributes. An
escape this does not know is dropped rather than drawn, because a stray `[`
in the middle of a menu row is worse than a missing colour.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from typing import Final

from gentoo_install.i18n import width

#: `CSI ... final-byte`. The parameters are digits and semicolons; the final
#: byte is anything in `@` to `~`, not a letter. Insert-character is `@`, and
#: matching letters alone left it unrecognised in the middle of a row.
CSI: Final[re.Pattern[str]] = re.compile(r"\x1b\[([0-9;?]*)([@-~])")

#: Two-character escapes with no parameters, and the ones that take a string
#: terminator. Neither draws anything.
SHORT: Final[re.Pattern[str]] = re.compile(r"\x1b[()][0-9A-B]|\x1b[=>]|\x1b\][^\x07]*\x07")


#: The longest escape this reads, so a tail shorter than it may still grow
#: into one. `\x1b]...\x07` can be longer, and is matched by its terminator.
LONGEST: Final[int] = 12


def _unfinished(text: str, at: int) -> bool:
    """Whether the escape at `at` could still be completed by the next chunk."""
    tail = text[at:]
    if tail.startswith("\x1b]"):
        return "\x07" not in tail
    return len(tail) < LONGEST


@dataclass
class Screen:
    """A grid of cells and a cursor, filled by `feed`."""

    lines: int = 40
    columns: int = 120
    _rows: list[list[str]] = field(default_factory=list)
    _reversed: list[list[bool]] = field(default_factory=list)
    line: int = 0
    column: int = 0
    _reverse: bool = False
    _decoder: codecs.IncrementalDecoder | None = None
    #: An escape sequence the last chunk ended in the middle of.
    _pending: str = ""

    def __post_init__(self) -> None:
        self._rows = [[" "] * self.columns for _ in range(self.lines)]
        self._reversed = [[False] * self.columns for _ in range(self.lines)]
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def text(self) -> str:
        """Every row, trailing spaces removed, as one string."""
        return "\n".join("".join(row).rstrip() for row in self._rows)

    def banded(self) -> list[str]:
        """The rows drawn in reverse video, which is what a heading is."""
        return [
            "".join(row).strip()
            for row, marks in zip(self._rows, self._reversed)
            if all(marks) and "".join(row).strip()
        ]

    def feed(self, data: bytes) -> None:
        """Take a chunk of the console, which may end anywhere.

        A read boundary falls wherever the network put it, so decoding each
        chunk on its own turns one split character into three replacement
        marks, and one split escape into a letter drawn into a row. Both are
        carried over instead: the decoder keeps the bytes and `_pending` the
        text of an escape that has not ended yet.
        """
        assert self._decoder is not None
        text = self._pending + self._decoder.decode(data)
        self._pending = ""
        at = 0
        while at < len(text):
            character = text[at]
            if character == "\x1b":
                found = CSI.match(text, at) or SHORT.match(text, at)
                if found:
                    if found.re is CSI:
                        self._control(found.group(1), found.group(2))
                    at = found.end()
                    continue
                if _unfinished(text, at):
                    self._pending = text[at:]
                    return
                # An escape this does not know: drop the escape alone, or the
                # letter after it would be drawn into the middle of a row.
                at += 1
                continue
            if character == "\r":
                self.column = 0
            elif character == "\n":
                self.line = min(self.lines - 1, self.line + 1)
                self.column = 0
            elif character == "\b":
                self.column = max(0, self.column - 1)
            elif character >= " ":
                self._put(character)
            at += 1

    def _put(self, character: str) -> None:
        # A wide character owns two cells and the second is empty, the way the
        # terminal itself lays it out: counting it as one put every column
        # after a Chinese label one cell to the left.
        cells = max(1, width(character))
        if 0 <= self.line < self.lines and 0 <= self.column < self.columns:
            self._rows[self.line][self.column] = character
            self._reversed[self.line][self.column] = self._reverse
            for offset in range(1, cells):
                if self.column + offset < self.columns:
                    self._rows[self.line][self.column + offset] = ""
                    self._reversed[self.line][self.column + offset] = self._reverse
        self.column += cells

    def _control(self, parameters: str, final: str) -> None:
        numbers = [int(one) for one in parameters.split(";") if one.isdigit()]
        first = numbers[0] if numbers else 0
        if final == "H":
            self.line = max(0, (numbers[0] if numbers else 1) - 1)
            self.column = max(0, (numbers[1] if len(numbers) > 1 else 1) - 1)
        elif final in "ABCD":
            step = first or 1
            if final == "A":
                self.line = max(0, self.line - step)
            elif final == "B":
                self.line = min(self.lines - 1, self.line + step)
            elif final == "C":
                self.column = min(self.columns - 1, self.column + step)
            else:
                self.column = max(0, self.column - step)
        elif final == "G":
            self.column = max(0, first - 1)
        elif final == "d":
            # ncurses moves down a column with this rather than a full `H`,
            # 23 times in one menu draw. Ignored, every write after it landed
            # a row too high and the pane read as two layouts at once.
            self.line = max(0, min(self.lines - 1, (first or 1) - 1))
        elif final == "@":
            self._insert(first or 1)
        elif final == "J":
            self._erase_display(first)
        elif final == "K":
            self._erase_line(first)
        elif final == "X":
            for offset in range(first or 1):
                if self.column + offset < self.columns:
                    self._rows[self.line][self.column + offset] = " "
        elif final == "m":
            for one in numbers or [0]:
                if one == 0:
                    self._reverse = False
                elif one == 7:
                    self._reverse = True
                elif one == 27:
                    self._reverse = False

    def _insert(self, count: int) -> None:
        """Push the rest of the row right, which is how a value is corrected."""
        row = self._rows[self.line]
        marks = self._reversed[self.line]
        keep = self.columns - self.column - count
        if keep <= 0:
            self._blank(self.line, self.column, self.columns)
            return
        row[self.column + count :] = row[self.column : self.column + keep]
        marks[self.column + count :] = marks[self.column : self.column + keep]
        self._blank(self.line, self.column, self.column + count)

    def _erase_display(self, kind: int) -> None:
        if kind == 2:
            for line in range(self.lines):
                self._blank(line, 0, self.columns)
            return
        if kind == 0:
            self._blank(self.line, self.column, self.columns)
            for line in range(self.line + 1, self.lines):
                self._blank(line, 0, self.columns)
        else:
            for line in range(self.line):
                self._blank(line, 0, self.columns)
            self._blank(self.line, 0, self.column + 1)

    def _erase_line(self, kind: int) -> None:
        if kind == 1:
            self._blank(self.line, 0, self.column + 1)
        elif kind == 2:
            self._blank(self.line, 0, self.columns)
        else:
            self._blank(self.line, self.column, self.columns)

    def _blank(self, line: int, start: int, end: int) -> None:
        for column in range(start, min(end, self.columns)):
            self._rows[line][column] = " "
            self._reversed[line][column] = False
