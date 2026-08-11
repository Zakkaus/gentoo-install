"""The real terminal, behind the narrow `Screen` the widgets need."""

from __future__ import annotations

import curses
from collections.abc import Callable
from typing import Protocol, Any

from .widgets import MINIMUM_COLUMNS, MINIMUM_LINES, Style

#: Colour pair per style. A terminal with no colour keeps every pair at 0,
#: which curses renders as the default attributes.
_PAIRS: dict[Style, int] = {Style.PLAIN: 0, Style.REQUIRED: 1, Style.UNTOUCHED: 2}


class CursesScreen:
    def __init__(
        self, window: Any, translate: Callable[[str], str] = lambda source: source
    ) -> None:
        self._window = window
        self._translate = translate
        # raw() rather than cbreak(): the interrupt and suspend characters
        # arrive as bytes, so ctrl-c reaches the widgets and is answered with
        # the same question an escape is, instead of ending the run outright.
        curses.raw()
        # A resize can interrupt a blocking read before ncurses queues its key;
        # nonblocking mode would make that case indistinguishable from no input.
        self._window.nodelay(False)
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
        while True:
            pressed = self._read_key()
            if pressed != "KEY_RESIZE" and not too_small(self):
                return pressed
            curses.update_lines_cols()
            self._window.erase()
            if not too_small(self):
                return pressed
            self._show_too_small()
            if pressed in ("\x1b", "\x03"):
                return pressed
            leaving = self._wait_until_usable()
            if leaving:
                return leaving
            return pressed

    def _read_key(self) -> str:
        """Read one character or named function key, retrying signal interruptions."""
        interrupted = False
        while True:
            try:
                pressed = self._window.get_wch()
            except curses.error:
                # SIGWINCH may interrupt the read before ncurses queues KEY_RESIZE.
                if interrupted:
                    raise
                interrupted = True
                continue
            if isinstance(pressed, int):
                return curses.keyname(pressed).decode("ascii", "replace")
            return str(pressed)

    def _wait_until_usable(self) -> str:
        """Keep widgets away from dimensions their layout cannot represent."""
        while too_small(self):
            self._show_too_small()
            pressed = self._read_key()
            if pressed in ("\x1b", "\x03"):
                return pressed
            if pressed == "KEY_RESIZE":
                curses.update_lines_cols()
                self._window.erase()
        return ""

    def _show_too_small(self) -> None:
        """Draw the required size and the two ways out of the wait."""
        self.clear()
        self.write(0, 0, self._translate("Esc leaves; resize the terminal"))
        self.write(2, 0, too_small(self))
        self.show()


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
