"""The pieces every screen is built from.

Each one is a loop over key presses that returns an `Answer`: what the operator
chose, or that they went back, or that they cancelled. A widget never touches
the configuration; the caller decides what a choice means, which is what keeps
every screen a pure function of its input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Protocol, Sequence, TypeVar

from ..i18n import truncate, width

V = TypeVar("V")

#: 80x24 is the floor every screen has to work in, and a serial console is
#: often exactly that.
MINIMUM_COLUMNS = 80
MINIMUM_LINES = 24


class Outcome(Enum):
    """Three states, not two: going back is not the same as cancelling, and
    neither is the same as choosing."""

    CHOSE = "chose"
    BACK = "back"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Answer(Generic[V]):
    outcome: Outcome
    value: V | None = None

    @property
    def chosen(self) -> bool:
        return self.outcome is Outcome.CHOSE

    def unwrap(self) -> V:
        if self.value is None:
            raise ValueError("no value: the operator went back or cancelled")
        return self.value


class Screen(Protocol):
    """What a widget needs from a terminal.

    Narrow on purpose: curses implements it, and so does the fake the tests
    drive, so every widget is exercised without a terminal.
    """

    def size(self) -> tuple[int, int]:
        """Lines and columns."""

    def clear(self) -> None: ...

    def write(self, line: int, column: int, text: str, highlight: bool = False) -> None: ...

    def show(self) -> None:
        """Flush what was written."""

    def key(self) -> str:
        """Block until a key press and return its name."""


@dataclass(frozen=True)
class Item(Generic[V]):
    """One row of a menu."""

    label: str
    value: V
    #: Why the row cannot be chosen, taken from `model/compat.py` so the
    #: interface and the validator give the same reason.
    disabled_because: str = ""
    detail: str = ""


@dataclass
class Menu(Generic[V]):
    title: str
    items: Sequence[Item[V]]
    #: Rows already selected, for a menu that takes several answers.
    selected: set[int] = field(default_factory=set)
    multiple: bool = False
    footer: str = ""

    def run(self, screen: Screen) -> Answer[list[V]]:
        cursor = self._first_enabled()
        while True:
            self._draw(screen, cursor)
            pressed = screen.key()
            if pressed in ("KEY_UP", "k"):
                cursor = self._step(cursor, -1)
            elif pressed in ("KEY_DOWN", "j"):
                cursor = self._step(cursor, 1)
            elif pressed == " " and self.multiple:
                self._toggle(cursor)
            elif pressed in ("\n", "KEY_ENTER"):
                answer = self._accept(cursor)
                if answer is not None:
                    return answer
            elif pressed in ("KEY_LEFT", "\x7f", "KEY_BACKSPACE"):
                return Answer(Outcome.BACK)
            elif pressed in ("q", "\x1b"):
                return Answer(Outcome.CANCELLED)

    def _accept(self, cursor: int) -> Answer[list[V]] | None:
        if self.multiple:
            return Answer(Outcome.CHOSE, [self.items[index].value for index in sorted(self.selected)])
        if self.items[cursor].disabled_because:
            return None
        return Answer(Outcome.CHOSE, [self.items[cursor].value])

    def _toggle(self, cursor: int) -> None:
        if self.items[cursor].disabled_because:
            return
        self.selected.symmetric_difference_update({cursor})

    def _first_enabled(self) -> int:
        for index, item in enumerate(self.items):
            if not item.disabled_because:
                return index
        return 0

    def _step(self, cursor: int, by: int) -> int:
        """Skip disabled rows, and stop rather than wrap: wrapping past the end
        of a long list loses the operator's place."""
        candidate = cursor
        while 0 <= candidate + by < len(self.items):
            candidate += by
            if not self.items[candidate].disabled_because:
                return candidate
        return cursor

    def _draw(self, screen: Screen, cursor: int) -> None:
        lines, columns = screen.size()
        screen.clear()
        screen.write(0, 0, truncate(self.title, columns))
        room = lines - 4
        top = max(0, min(cursor - room // 2, len(self.items) - room))
        for row, index in enumerate(range(top, min(top + room, len(self.items)))):
            item = self.items[index]
            mark = " "
            if self.multiple:
                # Brackets, not a glyph: a console with no CJK font and no
                # box-drawing set still shows the state.
                mark = "x" if index in self.selected else " "
                mark = f"[{mark}]"
            text = f"{mark} {item.label}" if self.multiple else item.label
            if item.detail:
                text = f"{text}  {item.detail}"
            if item.disabled_because:
                # After the value, not instead of it: a row that cannot be
                # chosen still has to show what it settled on.
                text = f"{text} - {item.disabled_because}"
            screen.write(row + 2, 2, truncate(text, columns - 4), highlight=index == cursor)
        if self.footer:
            screen.write(lines - 1, 0, truncate(self.footer, columns))
        screen.show()


@dataclass
class TextField:
    title: str
    value: str = ""
    #: Shown instead of the characters typed, for a password.
    masked: bool = False
    footer: str = ""

    def run(self, screen: Screen) -> Answer[str]:
        typed = list(self.value)
        while True:
            self._draw(screen, typed)
            pressed = screen.key()
            if pressed in ("\n", "KEY_ENTER"):
                return Answer(Outcome.CHOSE, "".join(typed))
            if pressed in ("\x7f", "KEY_BACKSPACE"):
                if typed:
                    typed.pop()
                else:
                    return Answer(Outcome.BACK)
            elif pressed == "\x1b":
                return Answer(Outcome.CANCELLED)
            elif len(pressed) == 1 and pressed.isprintable():
                typed.append(pressed)

    def _draw(self, screen: Screen, typed: list[str]) -> None:
        lines, columns = screen.size()
        screen.clear()
        screen.write(0, 0, truncate(self.title, columns))
        shown = "*" * len(typed) if self.masked else "".join(typed)
        # Keep the end visible: the operator is looking at what they just typed.
        room = columns - 4
        while width(shown) > room:
            shown = shown[1:]
        screen.write(2, 2, shown)
        if self.footer:
            screen.write(lines - 1, 0, truncate(self.footer, columns))
        screen.show()


@dataclass
class Confirm:
    """A question with no default. Anything destructive uses `phrase`, so the
    operator types the disk name rather than pressing enter on a highlight."""

    title: str
    phrase: str = ""
    footer: str = ""

    def run(self, screen: Screen) -> Answer[bool]:
        if self.phrase:
            typed = TextField(title=self.title, footer=self.footer).run(screen)
            if not typed.chosen:
                return Answer(typed.outcome)
            return Answer(Outcome.CHOSE, typed.unwrap() == self.phrase)
        menu: Menu[bool] = Menu(
            title=self.title,
            items=[Item(label="No", value=False), Item(label="Yes", value=True)],
            footer=self.footer,
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        return Answer(Outcome.CHOSE, answer.unwrap()[0])
