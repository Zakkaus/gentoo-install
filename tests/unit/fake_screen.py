# SPDX-License-Identifier: GPL-2.0-or-later
"""A `Screen` that records what was drawn and replays key presses.

Every widget is driven through this, so the interface is tested without a
terminal and a screen that would not fit in 80x24 fails as a unit test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gentoo_install.i18n import width
from gentoo_install.tui.widgets import Style


@dataclass
class FakeScreen:
    keys: list[str] = field(default_factory=list)
    lines: int = 24
    columns: int = 80
    #: Every frame drawn, most recent last, as a list of rendered rows.
    frames: list[list[str]] = field(default_factory=list)
    highlighted: list[str] = field(default_factory=list)
    #: Every styled row drawn, so a test can assert what the colour says
    #: without a terminal that has colour.
    styled: list[tuple[Style, str]] = field(default_factory=list)
    #: How many times a widget asked for the key page.
    helped: int = 0
    #: One entry per cell, so a wide character owns two of them and the
    #: second is empty.
    _grid: dict[int, list[str]] = field(default_factory=dict)

    def size(self) -> tuple[int, int]:
        return self.lines, self.columns

    def clear(self) -> None:
        self._grid = {}

    def write(
        self,
        line: int,
        column: int,
        text: str,
        highlight: bool = False,
        style: Style = Style.PLAIN,
    ) -> None:
        """Merged into the row at that column, the way curses does it.

        Replacing the row would hide a widget writing two things over each
        other, which is exactly what a screen made of columns can get wrong.
        """
        assert 0 <= line < self.lines, f"row {line} is off a {self.lines}-line screen"
        assert column + width(text) <= self.columns, f"{text!r} runs past column {self.columns}"
        # A grid of cells, not a string of characters: curses places at a
        # column, so a wide label takes two cells and the field beside it
        # still starts where the widget asked. Composing by character index
        # pushed every later write right, and the composed row measured wider
        # than the screen while every write into it was inside it.
        cells = self._grid.setdefault(line, [])
        while len(cells) < column:
            cells.append(" ")
        # A wide character the span covers only one cell of loses both of its
        # cells, and only at the two edges: inside the span every cell is rewritten.
        end = column + width(text)
        if 0 < column < len(cells) and cells[column] == "":
            cells[column - 1] = " "
            cells[column] = " "
        if end < len(cells) and cells[end] == "":
            cells[end] = " "
        at = column
        for character in text:
            step = width(character)
            if step == 0:
                # A combining mark owns no cell, so it joins the base character
                # in the cell that holds it rather than displacing a column.
                base = at - 1
                while base > 0 and cells[base] == "":
                    base -= 1
                if base >= 0:
                    cells[base] += character
                continue
            while len(cells) < at + step:
                cells.append(" ")
            cells[at] = character
            for tail in range(1, step):
                cells[at + tail] = ""
            at += step
        if highlight:
            self.highlighted.append(text)
        if style is not Style.PLAIN:
            self.styled.append((style, text))

    def drawn(self, line: int) -> str:
        """The row as it stands, before `show` records the frame."""
        return "".join(self._grid.get(line, []))

    def show(self) -> None:
        self.frames.append(
            ["".join(self._grid.get(row, [])) for row in range(self.lines)]
        )

    def help(self) -> None:
        """What the real screen draws, recorded: a test asserts the page was
        asked for without a terminal to draw it on."""
        self.helped += 1

    def key(self) -> str:
        if not self.keys:
            raise AssertionError("the widget asked for a key and the test supplied none")
        return self.keys.pop(0)

    @property
    def last(self) -> str:
        """The most recent frame as one string, for a containment assertion."""
        return "\n".join(self.frames[-1]) if self.frames else ""
