# SPDX-License-Identifier: GPL-2.0-or-later
"""The pieces every screen is built from.

Each one is a loop over key presses that returns an `Answer`: what the operator
chose, or that they went back, or that they cancelled. A widget never touches
the configuration; the caller decides what a choice means, which is what keeps
every screen a pure function of its input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import textwrap
from typing import (
    Callable,
    ClassVar,
    Final,
    Generic,
    Iterable,
    Mapping,
    Protocol,
    Sequence,
    TypeVar,
)

from ..i18n import CUT, clip, truncate, width

#: Re-exported: every widget cuts through `clip`, and the mark it leaves is
#: what tells a cut value from a whole one.
__all__ = ["CUT", "clip"]

V = TypeVar("V")
A = TypeVar("A")
U = TypeVar("U")

#: What asks to leave. ctrl-c is here rather than a signal because raw mode
#: delivers it as a byte, so it is answered rather than obeyed.
#: Ending the run. Escape is not here: it meant "end the install" on one
#: screen and "go back one" on the next inside a single feature, and the key
#: table in `docs/design.md` gives it one meaning below the main menu.
CANCEL: Final[frozenset[str]] = frozenset({"\x03"})

#: Leaving a screen without answering it. Every widget takes all three, and
#: the status line names the arrow, which is the one that works in a field
#: already holding a value.
BACK_KEYS: Final[frozenset[str]] = frozenset({"KEY_LEFT", "\x1b", "\x7f", "KEY_BACKSPACE"})

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


class _Missing:
    pass


_MISSING: Final[_Missing] = _Missing()


@dataclass(frozen=True, init=False)
class Answer(Generic[V]):
    outcome: Outcome
    _value: V | _Missing

    def __init__(self, outcome: Outcome, value: V | _Missing = _MISSING) -> None:
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "_value", value)

    @property
    def value(self) -> V | None:
        return None if isinstance(self._value, _Missing) else self._value

    @property
    def chosen(self) -> bool:
        return self.outcome is Outcome.CHOSE

    def unwrap(self) -> V:
        if isinstance(self._value, _Missing):
            raise ValueError("no value: the operator went back or cancelled")
        return self._value

    def map(self, transform: Callable[[V], U]) -> Answer[U]:
        if not self.chosen:
            return Answer(self.outcome)
        return Answer(Outcome.CHOSE, transform(self.unwrap()))


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


def spread(left: str, right: str, columns: int) -> str:
    """`left` at the margin and `right` at the end of one row that wide.

    `right` is dropped rather than truncated when the two cannot both fit: half
    a count reads as a different count, and half a legend explains a mark the
    reader can no longer see named.
    """
    head = truncate(left, columns)
    room = columns - width(head) - 1
    tail = right if right and width(right) <= room else ""
    return f"{head}{' ' * (columns - width(head) - width(tail))}{tail}"


def band(screen: Screen, line: int, left: str, right: str = "") -> None:
    """One full-width reversed row.

    Reverse video is an attribute rather than a glyph, so a console with no
    colour and no line-drawing font still shows the edge; a box drawn from
    `U+2500` would be a row of question marks on the medium's own font.
    """
    _, columns = screen.size()
    screen.write(line, 0, spread(left, right, columns), highlight=True)


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
    #: Drawn above the first row in a group. Empty preserves the ordinary flat
    #: menu used by the rest of the installer.
    heading: str = ""
    #: Tri-state menus allow one preferred row for each non-empty group.
    preference_group: str = ""


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
class _Menu(Generic[V, A]):
    title: str
    items: Sequence[Item[V]]
    #: Rows already selected, for a menu that takes several answers.
    selected: set[int] = field(default_factory=set)
    #: A subset of selected rows carrying the preferred mark.
    preferred: set[int] = field(default_factory=set)
    #: Font selection needs installed and preferred states; every other menu
    #: keeps the binary behavior unless it opts in.
    tri_state: bool = False
    footer: str = ""
    #: What the marks in the body mean, kept at the end of the footer line so
    #: it does not read as one more key.
    legend: str = ""
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
    current: V | None | _Missing = _MISSING

    _multiple: ClassVar[bool] = False

    def run(self, screen: Screen) -> Answer[A]:
        cursor = self.cursor if 0 <= self.cursor < len(self.items) else self._first_enabled()
        if not self.cursor and not isinstance(self.current, _Missing):
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
            elif pressed == " " and self._multiple:
                self._toggle(cursor)
            elif pressed in ("\n", "KEY_ENTER"):
                answer = self._accept(cursor)
                if answer is not None:
                    return answer
            elif pressed in BACK_KEYS:
                return Answer(Outcome.BACK)
            elif pressed in CANCEL_IN_A_MENU:
                return Answer(Outcome.CANCELLED)

    def _accept(self, cursor: int) -> Answer[A] | None:
        raise NotImplementedError

    def _toggle(self, cursor: int) -> None:
        """Removing a selected row is always allowed; adding one is not.

        A choice can be disabled after it was made -- an application from an
        overlay the operator then removed -- and refusing the keystroke left
        them holding an invalid selection with no way to drop it.
        """
        if self.items[cursor].disabled_because and cursor not in self.selected:
            return
        if not self.tri_state:
            self.selected.symmetric_difference_update({cursor})
            return
        if cursor not in self.selected:
            self.selected.add(cursor)
            return
        if cursor not in self.preferred:
            group = self.items[cursor].preference_group
            if not group:
                self.selected.remove(cursor)
                return
            self.preferred = {
                index
                for index in self.preferred
                if self.items[index].preference_group != group
            }
            self.preferred.add(cursor)
            return
        self.preferred.remove(cursor)
        self.selected.remove(cursor)

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
        screen.write(0, 0, clip(self.title, columns))
        # One line each, under the title. A question whose subject is a list
        # crammed the list into the title, and the title is truncated to the
        # width: the profile a desktop moves to fell off the end of it.
        for offset, one in enumerate(self.preamble):
            screen.write(offset + 1, 2, clip(one, columns - 4))
        above = len(self.preamble)
        room = lines - 4 - above
        displayed = self._display_rows(columns)
        cursor_row = next(
            row for row, (index, _) in enumerate(displayed) if index == cursor
        )
        top = max(0, min(cursor_row - room // 2, len(displayed) - room))
        for row, (index, text) in enumerate(displayed[top : top + room]):
            if index is None:
                screen.write(row + 2 + above, 2, clip(text, columns - 4))
                continue
            item = self.items[index]
            # The marker is the signal and the colour repeats it: a serial
            # console with no colour has to show the same thing, and a legend
            # naming a mark nobody draws describes an interface that does not
            # exist. In the left margin, so the labels stay aligned.
            if item.style is not Style.PLAIN:
                screen.write(row + 2 + above, 0, MARKS[item.style], style=item.style)
            screen.write(
                row + 2 + above,
                2,
                clip(text, columns - 4),
                highlight=index == cursor,
                style=item.style,
            )
        if self.footer or self.legend:
            # Held apart rather than run together: the keys and what the marks
            # mean are two things to read, and one line of `[enter] open
            # [q] cancel * required ~ never opened` reads as neither.
            screen.write(lines - 1, 0, spread(self.footer, self.legend, columns))
        screen.show()

    def _display_rows(self, columns: int) -> list[tuple[int | None, str]]:
        rows: list[tuple[int | None, str]] = []
        heading = ""
        for index, item in enumerate(self.items):
            if item.heading and item.heading != heading:
                heading = item.heading
                rows.append((None, heading))
            mark = self._selection_mark(index)
            text = f"{mark} {item.label}" if self._multiple else item.label
            if item.detail:
                text = f"{text}  {item.detail}"
            if item.disabled_because:
                text = f"{text} - {item.disabled_because}"
            wrapped = textwrap.wrap(text, width=max(1, columns - 4), break_long_words=False) or [""]
            rows.append((index, wrapped[0]))
            rows.extend((None, continuation) for continuation in wrapped[1:])
        return rows

    def _selection_mark(self, index: int) -> str:
        if not self._multiple:
            return ""
        # ASCII brackets remain readable without a graphical console font.
        mark = "x" if index in self.selected else " "
        if self.tri_state and index in self.selected and index not in self.preferred:
            mark = "-"
        return f"[{mark}]"


class Menu(_Menu[V, V]):
    def _accept(self, cursor: int) -> Answer[V] | None:
        if self.items[cursor].disabled_because:
            return None
        return Answer(Outcome.CHOSE, self.items[cursor].value)


class MultipleChoiceMenu(_Menu[V, tuple[V, ...]]):
    _multiple: ClassVar[bool] = True

    def _accept(self, cursor: int) -> Answer[tuple[V, ...]]:
        return Answer(
            Outcome.CHOSE,
            tuple(self.items[index].value for index in sorted(self.selected)),
        )


@dataclass
class TextField:
    title: str
    value: str = ""
    #: Shown instead of the characters typed, for a password.
    masked: bool = False
    footer: str = ""
    #: What the marks in the body mean, kept at the end of the footer line so
    #: it does not read as one more key.
    legend: str = ""
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
        # of them mean something by empty. Left always leaves, because a
        # prefilled field otherwise has no way back at all: the hostname
        # screen answered backspace by deleting and escape by offering to end
        # the run.
        touched = False
        while True:
            self._draw(screen, typed)
            pressed = screen.key()
            if pressed in ("\n", "KEY_ENTER"):
                return Answer(Outcome.CHOSE, "".join(typed))
            if pressed in ("KEY_LEFT", "\x1b"):
                return Answer(Outcome.BACK)
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
        screen.write(0, 0, clip(self.title, columns))
        # Brackets and a caret, drawn highlighted: a bare string at the top of
        # an empty screen does not read as somewhere to type.
        room = columns - 8
        shown = "*" * len(typed) if self.masked else "".join(typed)
        shown = _tail_that_fits(shown, room - 1)
        row = 2
        if self.detail:
            screen.write(row, 0, clip(self.detail, columns))
            row += 2
        # The caret in both states, so an empty field never reads as a full
        # one. A placeholder is a hint about the shape of the answer and is
        # drawn only when there is no `detail` naming the exact string.
        inside = f"{shown}_" if typed else f"_{clip(self.placeholder, room - 1)}"
        screen.write(row, 2, f"[ {inside}{' ' * (room - width(inside))} ]", highlight=True)
        if self.footer or self.legend:
            # Held apart rather than run together: the keys and what the marks
            # mean are two things to read, and one line of `[enter] open
            # [q] cancel * required ~ never opened` reads as neither.
            screen.write(lines - 1, 0, spread(self.footer, self.legend, columns))
        screen.show()


def _tail_that_fits(text: str, room: int) -> str:
    """The end of what was typed, because that is where the caret is.

    `room` is negative on a terminal narrower than the brackets, and dropping
    a character off an empty string never makes it shorter: the loop ends on
    the string rather than on the width, or an 8-column terminal never redraws.
    """
    while text and width(text) > room:
        text = text[1:]
    return text


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
    #: What the marks in the body mean, kept at the end of the footer line so
    #: it does not read as one more key.
    legend: str = ""
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
                legend=self.legend,
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
            legend=self.legend,
            current=self.current,
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        return Answer(Outcome.CHOSE, answer.unwrap())


class Accepts(Enum):
    """What a field takes at a keystroke.

    Rejecting a submitted form tells the operator they were wrong; refusing the
    key tells them before they are. A host name can never hold a space, so the
    space is not typed.
    """

    ANYTHING = "anything"
    #: Host names, bypass lists and user names.
    NO_SPACE = "no space"
    #: Ports. The range stays a rejection: `70000` is five valid keystrokes.
    DIGITS = "digits"

    def holds(self, character: str) -> bool:
        if self is Accepts.NO_SPACE:
            return not character.isspace()
        if self is Accepts.DIGITS:
            return character.isdigit()
        return True


@dataclass
class Field:
    """One line of a `Form`."""

    label: str
    value: str = ""
    #: What this field takes, from the table above.
    accepts: Accepts = Accepts.ANYTHING
    #: Drawn inside the box while it is empty.
    placeholder: str = ""
    #: Drawn as asterisks. A password read over a shoulder is the reason, and
    #: it is why one is typed twice on the same screen rather than once.
    secret: bool = False
    #: A tick rather than a box to type in. `value` is `"x"` when it is on, so
    #: a form still answers with one list of strings.
    toggle: bool = False


@dataclass(frozen=True)
class FormRejected:
    """A submitted form that must be redrawn with an inline explanation."""

    message: str
    corrections: Mapping[int, str] = field(default_factory=dict)


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
    #: What the marks in the body mean, kept at the end of the footer line so
    #: it does not read as one more key.
    legend: str = ""
    done: str = "Done"
    #: Drawn under the title after one of the answers was rejected. Retrying
    #: with the values kept is the point: an
    #: operator who mistyped the second password should not lose the first.
    message: str = ""

    def run(self, screen: Screen) -> Answer[list[str]]:
        return self.run_validated(screen, lambda values: Answer(Outcome.CHOSE, values))

    def run_validated(
        self,
        screen: Screen,
        validator: Callable[[list[str]], Answer[V] | FormRejected],
    ) -> Answer[V]:
        """Retry rejected submissions while retaining the form's entered values."""
        typed = [list(field.value) for field in self.fields]
        message = self.message
        # The last row submits, so it is a row like the others and reachable
        # the same way.
        cursor = 0
        # `TextField`'s rule: backspace deletes while the field under the cursor
        # has content and leaves only from an empty one nobody has edited. The
        # footer offers Back and the form had no way at all to take it.
        touched = False
        while True:
            self._draw(screen, typed, cursor, message)
            pressed = screen.key()
            if pressed in BACKWARD_FIELD:
                cursor = max(0, cursor - 1)
            elif pressed in FORWARD_FIELD:
                cursor = min(len(self.fields), cursor + 1)
            elif pressed in ("\n", "KEY_ENTER"):
                if cursor == len(self.fields):
                    values = ["".join(one) for one in typed]
                    checked = validator(values)
                    if not isinstance(checked, FormRejected):
                        return checked
                    corrected = values.copy()
                    for index, value in checked.corrections.items():
                        if index < 0 or index >= len(corrected):
                            raise ValueError(f"form correction index out of range: {index}")
                        corrected[index] = value
                    typed = [list(value) for value in corrected]
                    message = checked.message
                    cursor = 0
                    touched = False
                    continue
                cursor += 1
            elif pressed == " " and cursor < len(self.fields) and self.fields[cursor].toggle:
                typed[cursor] = [] if typed[cursor] else ["x"]
                touched = True
            elif pressed in ("KEY_LEFT", "\x1b"):
                return Answer(Outcome.BACK)
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
                and self.fields[cursor].accepts.holds(pressed)
            ):
                typed[cursor].append(pressed)
                touched = True

    def _draw(
        self, screen: Screen, typed: list[list[str]], cursor: int, message: str
    ) -> None:
        lines, columns = screen.size()
        screen.clear()
        screen.write(0, 0, clip(self.title, columns))
        offset = 2
        if message:
            screen.write(1, 2, clip(message, columns - 2), style=Style.REQUIRED)
            offset = 3
        widest = max((width(field.label) for field in self.fields), default=0)
        room = max(2, columns - widest - 10)
        widest = min(widest, columns - room - 10)
        for index, field in enumerate(self.fields):
            row = index + offset
            if row >= lines - 1:
                break
            screen.write(row, 2, clip(field.label, widest))
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
            inside = clip(field.placeholder, room) if not shown else shown
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
        if self.footer or self.legend:
            # Held apart rather than run together: the keys and what the marks
            # mean are two things to read, and one line of `[enter] open
            # [q] cancel * required ~ never opened` reads as neither.
            screen.write(lines - 1, 0, spread(self.footer, self.legend, columns))
        screen.show()


#: The left pane is the widest label in the catalog plus a marker and a space,
#: held between these two: narrower cuts an English name, wider leaves the
#: right pane too narrow for a device path and a filesystem beside it.
LEFT_PANE_MINIMUM: Final[int] = 20
LEFT_PANE_MAXIMUM: Final[int] = 34

#: The marker column and the space after it, before every label.
MARKER_ROOM: Final[int] = 2

#: The separator column and the space after it, between the two panes.
PANE_GAP: Final[int] = 2

#: ASCII: the medium's own console font carries no box-drawing set.
SEPARATOR: Final[str] = "|"




def left_pane_width(labels: Iterable[str]) -> int:
    """How wide the left pane stands for this catalog.

    Measured, not a constant: the same rows are 25 cells in English and 30 in
    Japanese, and a pane sized for one of them truncates the other.
    """
    widest = max((width(label) for label in labels), default=0)
    return min(LEFT_PANE_MAXIMUM, max(LEFT_PANE_MINIMUM, widest + MARKER_ROOM))


def right_pane_width(columns: int, left: int) -> int:
    """What is left for the right pane once the separator and its space are."""
    return columns - left - PANE_GAP


@dataclass(frozen=True)
class PaneRow(Generic[V]):
    """One row of the left pane, and what the right pane says about it."""

    label: str
    value: V
    #: Drawn at the right of the left pane, and dropped rather than cut when
    #: the label fills the pane: half a state word names another state.
    state: str = ""
    style: Style = Style.PLAIN
    #: The right pane's lines while the cursor is on this row. The first two
    #: are what a screen too small for two panes shows under the row.
    detail: tuple[str, ...] = ()
    #: Why this row cannot be opened. A row the operator cannot answer is
    #: still drawn and still readable, and the reason heads its right pane.
    disabled_because: str = ""


@dataclass
class TwoPane(Generic[V]):
    """The settings list beside the current value of the row under the cursor.

    Focus never leaves the left pane. Two panes that both take the cursor need
    a third key to move between them, and on a serial console another key is
    another state the operator cannot see.
    """

    title: str
    rows: Sequence[PaneRow[V]]
    #: Right-aligned on the title row, answered against total.
    counter: str = ""
    footer: str = ""
    #: What the marks mean, at the end of the status line so it does not read
    #: as one more key.
    legend: str = ""
    #: Where the cursor was left. Reopening after editing a row comes back to
    #: that row.
    cursor: int = 0

    def run(self, screen: Screen) -> Answer[V]:
        cursor = self.cursor if 0 <= self.cursor < len(self.rows) else 0
        while True:
            self.cursor = cursor
            self._draw(screen, cursor)
            pressed = screen.key()
            # `j` and `k` as well as the arrows, because every other menu in
            # this interface takes them and a serial console often has no
            # arrow keys at all.
            if pressed in ("KEY_UP", "k", "KEY_BTAB", "\x1b[Z"):
                cursor = max(0, cursor - 1)
            elif pressed in ("KEY_DOWN", "j", "\t"):
                cursor = min(max(0, len(self.rows) - 1), cursor + 1)
            elif pressed in ("\n", "KEY_ENTER", "KEY_RIGHT"):
                # A disabled row answers nothing rather than opening a screen
                # that would refuse: its reason is already in the right pane.
                if self.rows and not self.rows[cursor].disabled_because:
                    return Answer(Outcome.CHOSE, self.rows[cursor].value)
            elif pressed in ("KEY_LEFT", "\x1b", "q"):
                # All three answer Back and what Back means is the caller's:
                # the main menu asks whether to end the run. `q` is here and
                # nowhere deeper, because a text field reads it as a letter.
                return Answer(Outcome.BACK)
            elif pressed == "\x03":
                return Answer(Outcome.CANCELLED)

    def _draw(self, screen: Screen, cursor: int) -> None:
        lines, columns = screen.size()
        screen.clear()
        screen.write(0, 0, spread(self.title, self.counter, columns))
        if columns < MINIMUM_COLUMNS or lines < MINIMUM_LINES:
            self._draw_one_pane(screen, cursor, lines, columns)
        else:
            self._draw_two_panes(screen, cursor, lines, columns)
        if lines > 1 and (self.footer or self.legend):
            screen.write(lines - 1, 0, spread(self.footer, self.legend, columns))
        screen.show()

    def _draw_two_panes(self, screen: Screen, cursor: int, lines: int, columns: int) -> None:
        left = left_pane_width(row.label for row in self.rows)
        right = right_pane_width(columns, left)
        room = lines - 3
        for line in range(2, lines - 1):
            screen.write(line, left, SEPARATOR)
        top = _window(cursor, room, len(self.rows))
        for offset, index in enumerate(range(top, min(len(self.rows), top + room))):
            self._draw_row(
                screen, offset + 2, index, index == cursor, MARKER_ROOM, left - MARKER_ROOM
            )
        # The reason heads the pane: a row that cannot be opened has to say
        # why where the operator is already looking.
        here = self.rows[cursor] if self.rows else None
        detail: tuple[str, ...] = ()
        if here is not None:
            reason = (here.disabled_because,) if here.disabled_because else ()
            detail = (*reason, *here.detail)
        for offset, line_text in enumerate(detail[:room]):
            screen.write(offset + 2, left + PANE_GAP, clip(line_text, right))

    def _draw_one_pane(self, screen: Screen, cursor: int, lines: int, columns: int) -> None:
        """One pane, and the row under the cursor carries two of its lines.

        The floor is a measured boundary rather than best effort: below it the
        right pane cannot hold a value, so it stops existing.
        """
        room = lines - 3
        if room <= 0:
            return
        entries: list[tuple[int | None, str]] = []
        for index, row in enumerate(self.rows):
            entries.append((index, ""))
            if index == cursor:
                below = (
                    (row.disabled_because, *row.detail)
                    if row.disabled_because
                    else row.detail
                )
                entries.extend((None, line) for line in below[:2])
        here = next((at for at, (which, _) in enumerate(entries) if which == cursor), 0)
        top = _window(here, room, len(entries))
        for offset, (which, text) in enumerate(entries[top : top + room]):
            if which is None:
                screen.write(offset + 2, 4, clip(text, columns - 4))
                continue
            self._draw_row(
                screen, offset + 2, which, which == cursor, MARKER_ROOM, columns - MARKER_ROOM
            )

    def _draw_row(
        self, screen: Screen, line: int, index: int, focused: bool, column: int, room: int
    ) -> None:
        row = self.rows[index]
        if row.style is not Style.PLAIN:
            # The marker is the signal and the colour repeats it: a monochrome
            # serial console has to show the same thing.
            screen.write(line, 0, MARKS[row.style], style=row.style)
        screen.write(
            line,
            column,
            spread(clip(row.label, room), row.state, room),
            highlight=focused,
            style=row.style,
        )


def _window(cursor: int, room: int, total: int) -> int:
    """The first row drawn, so the cursor stays on screen without wrapping."""
    if room <= 0 or total <= room:
        return 0
    return max(0, min(cursor - room // 2, total - room))
