"""A `Screen` that records what was drawn and replays key presses.

Every widget is driven through this, so the interface is tested without a
terminal and a screen that would not fit in 80x24 fails as a unit test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gentoo_install.i18n import width


@dataclass
class FakeScreen:
    keys: list[str] = field(default_factory=list)
    lines: int = 24
    columns: int = 80
    #: Every frame drawn, most recent last, as a list of rendered rows.
    frames: list[list[str]] = field(default_factory=list)
    highlighted: list[str] = field(default_factory=list)
    _current: dict[int, str] = field(default_factory=dict)

    def size(self) -> tuple[int, int]:
        return self.lines, self.columns

    def clear(self) -> None:
        self._current = {}

    def write(self, line: int, column: int, text: str, highlight: bool = False) -> None:
        assert 0 <= line < self.lines, f"row {line} is off a {self.lines}-line screen"
        assert column + width(text) <= self.columns, f"{text!r} runs past column {self.columns}"
        self._current[line] = " " * column + text
        if highlight:
            self.highlighted.append(text)

    def show(self) -> None:
        self.frames.append([self._current.get(row, "") for row in range(self.lines)])

    def key(self) -> str:
        if not self.keys:
            raise AssertionError("the widget asked for a key and the test supplied none")
        return self.keys.pop(0)

    @property
    def last(self) -> str:
        """The most recent frame as one string, for a containment assertion."""
        return "\n".join(self.frames[-1]) if self.frames else ""
