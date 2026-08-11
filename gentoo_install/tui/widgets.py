"""The pieces every screen is built from.

Each one is a loop over key presses that returns an `Answer`: what the operator
chose, or that they went back, or that they cancelled. A widget never touches
the configuration; the caller decides what a choice means, which is what keeps
every screen a pure function of its input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Generic, Protocol, Sequence, TypeVar

from ..i18n import truncate, width

V = TypeVar("V")

#: What asks to leave. ctrl-c is here rather than a signal because raw mode
#: delivers it as a byte, so it is answered rather than obeyed.
CANCEL: Final[frozenset[str]] = frozenset({"\x1b", "\x03"})

#: A menu takes `q` as well; a text field cannot, because `q` is a character
#: someone is entitled to type into a hostname.
CANCEL_IN_A_MENU: Final[frozenset[str]] = CANCEL | {"q"}

#: 80x24 is the floor every screen has to work in, and a serial console is
#: often exactly that.
MINIMUM_COLUMNS = 80
MINIMUM_LINES = 24


class Style(Enum):
    """What a row's colour says. Never the only signal: a console with no
    colour still has to show the same thing, so the text says it too."""

    PLAIN = "plain"
    #: Required and still unanswered. The install cannot start.
    REQUIRED = "required"
    #: Optional and never opened, so it is running on a default nobody chose.
    UNTOUCHED = "untouched"


#: One character per style, drawn in the left margin. ASCII: a console with no
#: CJK font and no box-drawing set still shows it.
MARKS: Final[dict[Style, str]] = {Style.REQUIRED: "*", Style.UNTOUCHED: "~"}


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

    def write(
        self,
        line: int,
        column: int,
        text: str,
        highlight: bool = False,
        style: Style = Style.PLAIN,
    ) -> None: ...

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
    style: Style = Style.PLAIN


#: Tab moves on and shift-tab moves back, alongside the arrows. An operator
#: coming from a browser or an installer with a graphical toolkit reaches for
#: it first, and nothing in these screens uses tab for anything else.
#: `KEY_BTAB` is what ncurses calls shift-tab once keypad mode is on; the raw
#: sequence is what arrives before that.
TAB: Final[str] = "\t"
SHIFT_TAB: Final[tuple[str, ...]] = ("KEY_BTAB", "\x1b[Z")

#: `j` and `k` as well: a serial console over ssh may swallow an arrow, and
#: these are what an operator who reads vi bindings tries.
FORWARD: Final[tuple[str, ...]] = ("KEY_DOWN", "j", TAB)
BACKWARD: Final[tuple[str, ...]] = ("KEY_UP", "k", *SHIFT_TAB)

#: No `j` or `k` in a form: those are characters somebody is typing.
FORWARD_FIELD: Final[tuple[str, ...]] = ("KEY_DOWN", TAB)
BACKWARD_FIELD: Final[tuple[str, ...]] = ("KEY_UP", *SHIFT_TAB)


@dataclass
class Menu(Generic[V]):
    title: str
    items: Sequence[Item[V]]
    #: Rows already selected, for a menu that takes several answers.
    selected: set[int] = field(default_factory=set)
    multiple: bool = False
    footer: str = ""
    #: Lines drawn between the title and the rows. For a question whose
    #: subject is a list: the title is one line and truncated to the width.
    preamble: tuple[str, ...] = ()
    #: Where the highlight starts, and where it was left. A menu re-entered
    #: after editing a row has to come back to that row.
    cursor: int = 0
    #: The value the configuration holds now. Reopening a selector and pressing
    #: enter without navigating has to answer with what was already set, and
    #: without this the first row won: encryption enabled became disabled, and
    #: a second disk became the first.
    current: V | None = None

    def run(self, screen: Screen) -> Answer[list[V]]:
        cursor = self.cursor if 0 <= self.cursor < len(self.items) else self._first_enabled()
        if not self.cursor and self.current is not None:
            here = next(
                (at for at, one in enumerate(self.items) if one.value == self.current), None
            )
            if here is not None:
                cursor = here
        if self.items[cursor].disabled_because:
            cursor = self._first_enabled()
        while True:
            self.cursor = cursor
            self._draw(screen, cursor)
            pressed = screen.key()
            if pressed in BACKWARD:
                cursor = self._step(cursor, -1)
            elif pressed in FORWARD:
                cursor = self._step(cursor, 1)
            elif pressed == " " and self.multiple:
                self._toggle(cursor)
            elif pressed in ("\n", "KEY_ENTER"):
                answer = self._accept(cursor)
                if answer is not None:
                    return answer
            elif pressed in ("KEY_LEFT", "\x7f", "KEY_BACKSPACE"):
                return Answer(Outcome.BACK)
            elif pressed in CANCEL_IN_A_MENU:
                return Answer(Outcome.CANCELLED)

    def _accept(self, cursor: int) -> Answer[list[V]] | None:
        if self.multiple:
            return Answer(Outcome.CHOSE, [self.items[index].value for index in sorted(self.selected)])
        if self.items[cursor].disabled_because:
            return None
        return Answer(Outcome.CHOSE, [self.items[cursor].value])

    def _toggle(self, cursor: int) -> None:
        """Removing a selected row is always allowed; adding one is not.

        A choice can be disabled after it was made -- an application from an
        overlay the operator then removed -- and refusing the keystroke left
        them holding an invalid selection with no way to drop it.
        """
        if self.items[cursor].disabled_because and cursor not in self.selected:
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
            # A selected row is reachable even when disabled, or the operator
            # cannot put the cursor on the choice they have to undo.
            if not self.items[candidate].disabled_because or candidate in self.selected:
                return candidate
        return cursor

    def _draw(self, screen: Screen, cursor: int) -> None:
        lines, columns = screen.size()
        screen.clear()
        screen.write(0, 0, truncate(self.title, columns))
        # One line each, under the title. A question whose subject is a list
        # crammed the list into the title, and the title is truncated to the
        # width: the profile a desktop moves to fell off the end of it.
        for offset, one in enumerate(self.preamble):
            screen.write(offset + 1, 2, truncate(one, columns - 4))
        above = len(self.preamble)
        room = lines - 4 - above
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
            # The marker is the signal and the colour repeats it: a serial
            # console with no colour has to show the same thing, and a legend
            # naming a mark nobody draws describes an interface that does not
            # exist. In the left margin, so the labels stay aligned.
            if item.style is not Style.PLAIN:
                screen.write(row + 2 + above, 0, MARKS[item.style], style=item.style)
            screen.write(
                row + 2 + above,
                2,
                truncate(text, columns - 4),
                highlight=index == cursor,
                style=item.style,
            )
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
    #: Drawn inside the field while it is empty, so a field that takes an
    #: unusual value still says what that value looks like.
    placeholder: str = ""
    #: Drawn on its own line above the field. What has to be typed exactly
    #: belongs here and not in `placeholder`: a placeholder is drawn inside the
    #: box, where it is indistinguishable from a value already entered, and an
    #: operator pressed enter on what looked like a filled field.
    detail: str = ""

    def run(self, screen: Screen) -> Answer[str]:
        typed = list(self.value)
        # Backspace leaves the screen only while the field is untouched: a
        # field that had content could not be cleared otherwise, and several
        # of them mean something by empty.
        touched = False
        while True:
            self._draw(screen, typed)
            pressed = screen.key()
            if pressed in ("\n", "KEY_ENTER"):
                return Answer(Outcome.CHOSE, "".join(typed))
            if pressed in ("\x7f", "KEY_BACKSPACE"):
                if typed:
                    typed.pop()
                    touched = True
                elif not touched:
                    return Answer(Outcome.BACK)
            elif pressed in CANCEL:
                return Answer(Outcome.CANCELLED)
            elif len(pressed) == 1 and pressed.isprintable():
                typed.append(pressed)
                touched = True

    def _draw(self, screen: Screen, typed: list[str]) -> None:
        lines, columns = screen.size()
        screen.clear()
        screen.write(0, 0, truncate(self.title, columns))
        # Brackets and a caret, drawn highlighted: a bare string at the top of
        # an empty screen does not read as somewhere to type.
        room = columns - 8
        shown = "*" * len(typed) if self.masked else "".join(typed)
        while width(shown) > room - 1:
            shown = shown[1:]
        row = 2
        if self.detail:
            screen.write(row, 0, truncate(self.detail, columns))
            row += 2
        # The caret in both states, so an empty field never reads as a full
        # one. A placeholder is a hint about the shape of the answer and is
        # drawn only when there is no `detail` naming the exact string.
        inside = f"{shown}_" if typed else f"_{truncate(self.placeholder, room - 1)}"
        screen.write(row, 2, f"[ {inside}{' ' * (room - width(inside))} ]", highlight=True)
        if self.footer:
            screen.write(lines - 1, 0, truncate(self.footer, columns))
        screen.show()


@dataclass
class Confirm:
    """A question with no default. Anything destructive uses `phrase`, so the
    operator types the disk name rather than pressing enter on a highlight."""

    title: str
    phrase: str = ""
    #: Drawn inside the field, so the phrase to type is visible without the
    #: title having to carry it.
    placeholder: str = ""
    #: Drawn above the field. `phrase` uses this rather than `placeholder`:
    #: the exact string has to be readable while the field still looks empty.
    detail: str = ""
    #: Other spellings of the same answer. A `/dev/disk/by-id/` selector is
    #: sixty characters and its last component names the same disk.
    also: tuple[str, ...] = ()
    footer: str = ""
    #: The two answers, already translated by the caller. Defaulted so a test
    #: needs no catalog, and passed in everywhere the operator will read them.
    no: str = "No"
    yes: str = "Yes"
    #: What the configuration says now, so pressing enter without navigating
    #: keeps it. Without this every yes/no screen answered No.
    current: bool = False

    def run(self, screen: Screen) -> Answer[bool]:
        if self.phrase:
            typed = TextField(
                title=self.title,
                footer=self.footer,
                placeholder=self.placeholder,
                detail=self.detail,
            ).run(screen)
            if not typed.chosen:
                return Answer(typed.outcome)
            return Answer(Outcome.CHOSE, typed.unwrap() in {self.phrase, *self.also})
        menu: Menu[bool] = Menu(
            title=self.title,
            items=[Item(label=self.no, value=False), Item(label=self.yes, value=True)],
            footer=self.footer,
            current=self.current,
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        return Answer(Outcome.CHOSE, answer.unwrap()[0])


@dataclass
class Field:
    """One line of a `Form`."""

    label: str
    value: str = ""
    #: Drawn inside the box while it is empty.
    placeholder: str = ""
    #: Drawn as asterisks. A password read over a shoulder is the reason, and
    #: it is why one is typed twice on the same screen rather than once.
    secret: bool = False
    #: A tick rather than a box to type in. `value` is `"x"` when it is on, so
    #: a form still answers with one list of strings.
    toggle: bool = False


@dataclass
class Form:
    """Several fields on one screen, moved between with the arrow keys.

    One field per screen makes the operator answer six questions without ever
    seeing them together, and a network address is exactly the case where the
    six have to be read as one setting.
    """

    title: str
    fields: list[Field]
    footer: str = ""
    done: str = "Done"
    #: Drawn under the title, for a form the caller re-ran because one of the
    #: answers was wrong. Re-running with the values kept is the point: an
    #: operator who mistyped the second password should not lose the first.
    message: str = ""

    def run(self, screen: Screen) -> Answer[list[str]]:
        typed = [list(field.value) for field in self.fields]
        # The last row submits, so it is a row like the others and reachable
        # the same way.
        cursor = 0
        # `TextField`'s rule: backspace deletes while the field under the cursor
        # has content and leaves only from an empty one nobody has edited. The
        # footer offers Back and the form had no way at all to take it.
        touched = False
        while True:
            self._draw(screen, typed, cursor)
            pressed = screen.key()
            if pressed in BACKWARD_FIELD:
                cursor = max(0, cursor - 1)
            elif pressed in FORWARD_FIELD:
                cursor = min(len(self.fields), cursor + 1)
            elif pressed in ("\n", "KEY_ENTER"):
                if cursor == len(self.fields):
                    return Answer(Outcome.CHOSE, ["".join(one) for one in typed])
                cursor += 1
            elif pressed == " " and cursor < len(self.fields) and self.fields[cursor].toggle:
                typed[cursor] = [] if typed[cursor] else ["x"]
                touched = True
            elif pressed in ("\x7f", "KEY_BACKSPACE"):
                if cursor < len(self.fields) and typed[cursor] and not self.fields[cursor].toggle:
                    typed[cursor].pop()
                    touched = True
                elif not touched:
                    return Answer(Outcome.BACK)
            elif pressed in CANCEL:
                return Answer(Outcome.CANCELLED)
            elif (
                len(pressed) == 1
                and pressed.isprintable()
                and cursor < len(self.fields)
                and not self.fields[cursor].toggle
            ):
                typed[cursor].append(pressed)
                touched = True

    def _draw(self, screen: Screen, typed: list[list[str]], cursor: int) -> None:
        lines, columns = screen.size()
        screen.clear()
        screen.write(0, 0, truncate(self.title, columns))
        offset = 2
        if self.message:
            screen.write(1, 2, truncate(self.message, columns - 2), style=Style.REQUIRED)
            offset = 3
        widest = max((width(field.label) for field in self.fields), default=0)
        room = columns - widest - 10
        for index, field in enumerate(self.fields):
            row = index + offset
            if row >= lines - 1:
                break
            screen.write(row, 2, field.label)
            if field.toggle:
                screen.write(
                    row,
                    widest + 4,
                    f"[{'x' if typed[index] else ' '}]",
                    highlight=index == cursor,
                )
                continue
            shown = "*" * len(typed[index]) if field.secret else "".join(typed[index])
            while width(shown) > room - 1:
                shown = shown[1:]
            inside = truncate(field.placeholder, room) if not shown else shown
            if index == cursor:
                inside = f"{shown}_" if shown else inside
            screen.write(
                row,
                widest + 4,
                f"[ {inside}{' ' * max(0, room - width(inside))} ]",
                highlight=index == cursor,
            )
        end = min(len(self.fields) + offset + 1, lines - 2)
        screen.write(end, 2, self.done, highlight=cursor == len(self.fields))
        if self.footer:
            screen.write(lines - 1, 0, truncate(self.footer, columns))
        screen.show()
