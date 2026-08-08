"""The real terminal, behind the narrow `Screen` the widgets need."""

from __future__ import annotations

import curses
from typing import Any

from .widgets import MINIMUM_COLUMNS, MINIMUM_LINES


class CursesScreen:
    def __init__(self, window: Any) -> None:
        self._window = window

    def size(self) -> tuple[int, int]:
        lines, columns = self._window.getmaxyx()
        return int(lines), int(columns)

    def clear(self) -> None:
        self._window.erase()

    def write(self, line: int, column: int, text: str, highlight: bool = False) -> None:
        lines, columns = self.size()
        if not 0 <= line < lines:
            return
        try:
            self._window.addstr(line, column, text, curses.A_REVERSE if highlight else 0)
        except curses.error:
            # Writing the last cell of the last line always raises, and there
            # is nothing to recover: the text is already on screen.
            pass

    def show(self) -> None:
        self._window.refresh()

    def key(self) -> str:
        return str(self._window.getkey())


def too_small(screen: CursesScreen) -> str:
    lines, columns = screen.size()
    if lines >= MINIMUM_LINES and columns >= MINIMUM_COLUMNS:
        return ""
    return (
        f"the terminal is {columns}x{lines} and the interface needs "
        f"{MINIMUM_COLUMNS}x{MINIMUM_LINES}"
    )
