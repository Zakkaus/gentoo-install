"""The real terminal, behind the narrow `Screen` the widgets need."""

from __future__ import annotations

import curses
from typing import Protocol, Any

from .widgets import MINIMUM_COLUMNS, MINIMUM_LINES, Style

#: Colour pair per style. A terminal with no colour keeps every pair at 0,
#: which curses renders as the default attributes.
_PAIRS: dict[Style, int] = {Style.PLAIN: 0, Style.REQUIRED: 1, Style.UNTOUCHED: 2}


class CursesScreen:
    def __init__(self, window: Any) -> None:
        self._window = window
        # raw() rather than cbreak(): the interrupt and suspend characters
        # arrive as bytes, so ctrl-c reaches the widgets and is answered with
        # the same question an escape is, instead of ending the run outright.
        curses.raw()
        self._coloured = curses.has_colors()
        if self._coloured:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(_PAIRS[Style.REQUIRED], curses.COLOR_RED, -1)
            curses.init_pair(_PAIRS[Style.UNTOUCHED], curses.COLOR_YELLOW, -1)

    def size(self) -> tuple[int, int]:
        lines, columns = self._window.getmaxyx()
        return int(lines), int(columns)

    def clear(self) -> None:
        self._window.erase()

    def write(
        self,
        line: int,
        column: int,
        text: str,
        highlight: bool = False,
        style: Style = Style.PLAIN,
    ) -> None:
        lines, columns = self.size()
        if not 0 <= line < lines:
            return
        attributes = curses.A_REVERSE if highlight else 0
        if self._coloured and style is not Style.PLAIN:
            attributes |= curses.color_pair(_PAIRS[style])
        try:
            self._window.addstr(line, column, text, attributes)
        except curses.error:
            # Writing the last cell of the last line always raises, and there
            # is nothing to recover: the text is already on screen.
            pass

    def show(self) -> None:
        self._window.refresh()

    def key(self) -> str:
        pressed = str(self._window.getkey())
        if pressed == "KEY_RESIZE":
            # curses keeps the size it started with until it is told to look
            # again, so `size()` kept answering 80x24 after the window grew
            # and every list stayed the height it had when the menu opened.
            # The widgets treat an unknown key as one to redraw on, which is
            # what makes the new size take effect.
            curses.update_lines_cols()
            self._window.erase()
        return pressed


class Sized(Protocol):
    """Anything that can say how big it is. A protocol, so the size check can
    be exercised without a terminal."""

    def size(self) -> tuple[int, int]: ...


def too_small(screen: Sized) -> str:
    lines, columns = screen.size()
    if lines >= MINIMUM_LINES and columns >= MINIMUM_COLUMNS:
        return ""
    return (
        f"the terminal is {columns}x{lines} and the interface needs "
        f"{MINIMUM_COLUMNS}x{MINIMUM_LINES}"
    )
