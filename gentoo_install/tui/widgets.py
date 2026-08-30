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

#: What the two panes need. A serial console is often exactly this, and below
#: it `TwoPane` draws one pane: the floor for the layout, not for the run.
TWO_PANE_COLUMNS: Final[int] = 80
TWO_PANE_LINES: Final[int] = 24


class Style(Enum):
    """What a row's colour says. Never the only signal: a console with no
    colour still has to show the same thing, so the text says it too."""

    PLAIN = "plain"
    #: Required and still unanswered. The install cannot start.
    REQUIRED = "required"
    #: Optional and never opened, so it is running on a default nobody chose.
    UNTOUCHED = "untouched"
    #: The list while an editor holds the right pane. The operator sees where
    #: they are without the list competing with the screen they are answering.
    DIMMED = "dimmed"


#: One character per style, drawn in the left margin. ASCII: a console with no
#: CJK font and no box-drawing set still shows it. Total over `Style`, because
#: the lookup happens while a screen is being drawn and a missing member raised
#: there rather than anywhere a caller could answer for it.
MARKS: Final[dict[Style, str]] = {
    Style.PLAIN: "",
    Style.REQUIRED: "*",
    Style.UNTOUCHED: "~",
    Style.DIMMED: "",
}


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

    def help(self) -> None:
        """Draw the key page and wait for one key.

        On the screen rather than passed to each widget: the interface builds
        fifty-eight menus, and a page threaded through every one of them is a
        page half of them will be missing.
        """


@dataclass(frozen=True)
class KeyRow:
    """One line of the key page. `keys` is drawn as typed, not translated."""

    keys: str
    does: str


#: Every key a widget answers, in one table, because a key the operator cannot
#: find is a key that does not exist: `KEY_LEFT` went back from every screen
#: for a year and no status line named it.
KEY_HELP: Final[tuple[KeyRow, ...]] = (
    KeyRow("enter  \u2192", "Open the row under the cursor, or accept this screen"),
    KeyRow("\u2191  \u2193", "Move the cursor"),
    KeyRow("j  k", "Move the cursor, in a list; a letter in a field"),
    KeyRow("tab  shift-tab", "Move the cursor, in a list and between fields"),
    KeyRow("space", "Choose a row, where a screen takes several answers"),
    KeyRow("\u2190  backspace  esc", "Back, keeping what this screen already holds"),
    KeyRow("q", "Ask whether to leave, in a list; a letter in a field"),
    KeyRow("ctrl-c", "Ask whether to leave, anywhere"),
    KeyRow("?", "This page, in a list; a letter in a field"),
    KeyRow("page-up  page-down", "Move the cursor one screen"),
    KeyRow("home  end", "Move the cursor to the first or last row"),
    KeyRow("g  G", "First or last row, in a list; a letter in a field"),
    KeyRow("/", "Narrow a list to the rows holding what is typed next"),
    KeyRow("ctrl-u", "Empty the field, which arrives with a value in it"),
)

#: What opens the key page. A letter in a field, like `q`.
HELP_KEY: Final[str] = "?"

#: What empties a field. `ctrl-u` is what a shell's line editor uses, and a
#: printable key cannot do this: every one of them is a character somebody
#: types into one of these fields.
CLEAR_KEY: Final[str] = "\x15"

#: One screen at a time, and the ends. `America` holds 169 timezones, which is
#: eight screens of `j` on a console 24 lines tall.
PAGE_BACKWARD: Final[tuple[str, ...]] = ("KEY_PPAGE",)
PAGE_FORWARD: Final[tuple[str, ...]] = ("KEY_NPAGE",)
FIRST: Final[tuple[str, ...]] = ("KEY_HOME", "g")
LAST: Final[tuple[str, ...]] = ("KEY_END", "G")

#: What narrows a list to the rows holding what was typed.
FILTER_KEY: Final[str] = "/"


@dataclass
class Region:
    """A rectangle of another screen, behind the same protocol.

    What keeps the frame from disappearing when a row is opened: an editor
    screen is called with one of these and draws inside the right pane without
    a line of its own changing. `clear` erases only the rectangle, so the list
    beside it stays on screen.
    """

    screen: Screen
    line: int
    column: int
    lines: int
    columns: int
    #: How the rectangle is cut from the screen, asked again on every write so
    #: a terminal that changes size takes the rectangle with it. Measured once
    #: and kept, an editor went on writing into the rectangle the old size gave
    #: it while the frame around it was redrawn for the new one, and the screen
    #: held half of each layout.
    cut: Callable[[int, int], tuple[int, int, int, int] | None] | None = None
    #: Draws the frame around this rectangle again. The editor inside redraws
    #: itself when the terminal changes size; nothing else did, so the list
    #: beside it and the box around it were gone and the screen held one pane
    #: of an interface.
    redraw: Callable[[], None] | None = None
    _seen: tuple[int, int] = (0, 0)

    def _rectangle(self) -> tuple[int, int, int, int] | None:
        if self.cut is None:
            return self.line, self.column, self.lines, self.columns
        size = self.screen.size()
        if size != self._seen:
            # Before the arithmetic, so the rectangle answered is the one the
            # frame drew rather than the one it is about to replace.
            self._seen = size
            if self.redraw is not None:
                self.redraw()
        return self.cut(*size)

    def size(self) -> tuple[int, int]:
        rectangle = self._rectangle()
        if rectangle is None:
            return 0, 0
        _, _, lines, columns = rectangle
        return lines, columns

    def clear(self) -> None:
        rectangle = self._rectangle()
        if rectangle is None:
            return
        line, column, lines, columns = rectangle
        for offset in range(lines):
            self.screen.write(line + offset, column, " " * columns)

    def write(
        self,
        line: int,
        column: int,
        text: str,
        highlight: bool = False,
        style: Style = Style.PLAIN,
    ) -> None:
        rectangle = self._rectangle()
        if rectangle is None:
            return
        top, left, lines, columns = rectangle
        if not 0 <= line < lines or column >= columns:
            return
        self.screen.write(
            top + line,
            left + column,
            clip(text, columns - column),
            highlight=highlight,
            style=style,
        )

    def show(self) -> None:
        self.screen.show()

    def key(self) -> str:
        return self.screen.key()

    def help(self) -> None:
        self.screen.help()


def spread(left: str, right: str, columns: int) -> str:
    """`left` at the margin and `right` at the end of one row that wide.

    `right` is dropped rather than truncated when the two cannot both fit: half
    a count reads as a different count, and half a legend explains a mark the
    reader can no longer see named. `left` is cut with a mark, because a title
    that lost its end and one that never had it read the same.
    """
    head = clip(left, columns)
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
    #: What `/` is narrowing the list to. `None` when no filter is open, which
    #: is a different state from an open filter holding nothing typed yet.
    _query: str | None = None

    _multiple: ClassVar[bool] = False

    def run(self, screen: Screen) -> Answer[A]:
        cursor = self.cursor if 0 <= self.cursor < len(self.items) else self._first_enabled()
        if not self.cursor and not isinstance(self.current, _Missing):
            here = next(
                (at for at, one in enumerate(self.items) if one.value == self.current), None
            )
            if here is not None:
                cursor = here
        if 0 <= cursor < len(self.items) and self.items[cursor].disabled_because:
            cursor = self._first_enabled()
        while True:
            self.cursor = cursor
            self._draw(screen, cursor)
            pressed = screen.key()
            if self._query is not None and self._typed(pressed):
                cursor = self._after_typing(cursor)
                continue
            if pressed == FILTER_KEY and self._query is None:
                self._query = ""
                continue
            if pressed in BACKWARD:
                cursor = self._step(cursor, -1)
            elif pressed in FORWARD:
                cursor = self._step(cursor, 1)
            elif pressed in PAGE_BACKWARD:
                cursor = self._page(cursor, -1, screen)
            elif pressed in PAGE_FORWARD:
                cursor = self._page(cursor, 1, screen)
            elif pressed in FIRST:
                cursor = self._first_enabled()
            elif pressed in LAST:
                cursor = self._last_enabled()
            elif pressed == HELP_KEY:
                screen.help()
            elif pressed == " " and self._multiple:
                self._toggle(cursor)
            elif pressed in ("\n", "KEY_ENTER") or (
                pressed == "KEY_RIGHT" and not self._multiple
            ):
                # Right chooses here as it opens a row in the list, because
                # left goes back from both. It is left out where several rows
                # are chosen at once: there right would accept the screen on
                # the way to a row the operator meant to mark.
                if self._shown(cursor):
                    answer = self._accept(cursor)
                    if answer is not None:
                        return answer
            elif pressed in BACK_KEYS:
                return Answer(Outcome.BACK)
            elif pressed in CANCEL_IN_A_MENU:
                return Answer(Outcome.CANCELLED)

    def _typed(self, pressed: str) -> bool:
        """Whether this key belongs to the filter rather than to the list.

        Arrows and the page keys keep navigating while a filter is open, so
        the operator narrows and moves without leaving what they typed; `j`
        and `k` are letters here, which is why they are not tested first.
        """
        if self._query is None:
            return False
        if pressed == "\x1b":
            self._query = None
            return True
        if pressed in ("\x7f", "KEY_BACKSPACE"):
            self._query = self._query[:-1] if self._query else None
            return True
        if len(pressed) == 1 and pressed.isprintable():
            self._query += pressed
            return True
        return False

    def _after_typing(self, cursor: int) -> int:
        """Keep the cursor on a row the filter still shows."""
        if self._shown(cursor):
            return cursor
        return self._first_enabled()

    def _shown(self, index: int) -> bool:
        """Case-insensitive, on the label alone: a row is found by the name the
        operator reads, not by the reason printed beside it."""
        if not 0 <= index < len(self.items):
            return False
        if not self._query:
            return True
        return self._query.lower() in self.items[index].label.lower()

    def _accept(self, cursor: int) -> Answer[A] | None:
        raise NotImplementedError

    def _toggle(self, cursor: int) -> None:
        """Removing a selected row is always allowed; adding one is not.

        A choice can be disabled after it was made -- an application from an
        overlay the operator then removed -- and refusing the keystroke left
        them holding an invalid selection with no way to drop it.
        """
        if not self._shown(cursor):
            return
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
            if self._shown(index) and not item.disabled_because:
                return index
        return next(
            (index for index in range(len(self.items)) if self._shown(index)),
            -1,
        )

    def _last_enabled(self) -> int:
        for index in range(len(self.items) - 1, -1, -1):
            if not self._shown(index):
                continue
            if not self.items[index].disabled_because or index in self.selected:
                return index
        return next(
            (
                index
                for index in range(len(self.items) - 1, -1, -1)
                if self._shown(index)
            ),
            -1,
        )

    def _page(self, cursor: int, by: int, screen: Screen) -> int:
        """One screen, measured in the display rows it actually draws."""
        lines, columns = screen.size()
        room = max(1, lines - 4 - len(self.preamble))
        positions: dict[int, int] = {}
        for row, (index, _) in enumerate(self._display_rows(columns)):
            if index is not None:
                positions[index] = row
        current = positions.get(cursor)
        if current is None:
            return cursor
        target = current + by * room
        moved = cursor
        while True:
            candidate = self._step(moved, by)
            if candidate == moved:
                return moved
            candidate_row = positions.get(candidate)
            if candidate_row is None:
                return moved
            if by > 0 and candidate_row > target and moved != cursor:
                return moved
            if by < 0 and candidate_row < target and moved != cursor:
                return moved
            moved = candidate

    def _step(self, cursor: int, by: int) -> int:
        """Skip disabled rows, and stop rather than wrap: wrapping past the end
        of a long list loses the operator's place."""
        candidate = cursor
        while 0 <= candidate + by < len(self.items):
            candidate += by
            # A selected row is reachable even when disabled, or the operator
            # cannot put the cursor on the choice they have to undo.
            if not self._shown(candidate):
                continue
            if not self.items[candidate].disabled_because or candidate in self.selected:
                return candidate
        return cursor

    def _draw(self, screen: Screen, cursor: int) -> None:
        lines, columns = screen.size()
        screen.clear()
        # One line each, under the title. A question whose subject is a list
        # crammed the list into the title, and the title is truncated to the
        # width: the profile a desktop moves to fell off the end of it.
        for offset, one in enumerate(self.preamble):
            screen.write(offset + 1, 2, clip(one, columns - 4))
        above = len(self.preamble)
        room = lines - 4 - above
        displayed = self._display_rows(columns)
        # A filter can hide every row, and then there is no cursor to keep on
        # screen: an empty list with a count of nothing is the answer, not a
        # traceback out of `next`.
        cursor_row = next(
            (row for row, (index, _) in enumerate(displayed) if index == cursor), 0
        )
        top = max(0, min(cursor_row - room // 2, len(displayed) - room))
        # On the title row, and only when a row is off the screen: a list that
        # scrolls with nothing to say so reads as the whole list, and the
        # profile screen was read as offering thirteen of its fourteen.
        counted = (
            f"{cursor_row + 1}/{len(displayed)}" if len(displayed) > room else ""
        )
        screen.write(0, 0, spread(clip(self.title, columns), counted, columns))
        for row, (index, text) in enumerate(displayed[top : top + room]):
            if index is None:
                screen.write(row + 2 + above, 2, clip(text, columns - 4))
                continue
            item = self.items[index]
            # The marker is the signal and the colour repeats it: a serial
            # console with no colour has to show the same thing, and a legend
            # naming a mark nobody draws describes an interface that does not
            # exist. In the left margin, so the labels stay aligned.
            mark = MARKS[item.style]
            if mark:
                screen.write(row + 2 + above, 0, mark, style=item.style)
            screen.write(
                row + 2 + above,
                2,
                clip(text, columns - 4),
                highlight=index == cursor,
                style=item.style,
            )
        if self._query is not None:
            # The filter replaces the keys while it is open: what was typed
            # and how many rows are left are what the operator needs, and a
            # count they cannot see makes an empty list read as a broken menu.
            left = sum(1 for index in range(len(self.items)) if self._shown(index))
            screen.write(
                lines - 1, 0, spread(f"{FILTER_KEY}{self._query}", str(left), columns)
            )
        elif self.footer or self.legend:
            # Held apart rather than run together: the keys and what the marks
            # mean are two things to read, and one line of `[enter] open
            # [q] cancel * required ~ never opened` reads as neither.
            screen.write(lines - 1, 0, spread(self.footer, self.legend, columns))
        screen.show()

    def _display_rows(self, columns: int) -> list[tuple[int | None, str]]:
        rows: list[tuple[int | None, str]] = []
        heading = ""
        for index, item in enumerate(self.items):
            if not self._shown(index):
                continue
            if item.heading and item.heading != heading:
                heading = item.heading
                rows.append((None, heading))
            mark = self._selection_mark(index)
            text = f"{mark} {item.label}" if self._multiple else item.label
            if item.detail:
                text = f"{text}  {item.detail}"
            if item.disabled_because:
                text = f"{text} - {item.disabled_because}"
            wrapped = wrap_to_cells(text, max(1, columns - 4))
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
        if not self._shown(cursor) or self.items[cursor].disabled_because:
            return None
        return Answer(Outcome.CHOSE, self.items[cursor].value)


class MultipleChoiceMenu(_Menu[V, tuple[V, ...]]):
    _multiple: ClassVar[bool] = True

    def _accept(self, cursor: int) -> Answer[tuple[V, ...]] | None:
        if not self._shown(cursor):
            return None
        return Answer(
            Outcome.CHOSE,
            tuple(
                self.items[index].value
                for index in sorted(self.selected)
                if not self.items[index].disabled_because
            ),
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
        return self.run_validated(screen, lambda value: Answer(Outcome.CHOSE, value))

    def run_validated(
        self,
        screen: Screen,
        validator: Callable[[str], Answer[V] | TextFieldRejected],
    ) -> Answer[V]:
        """Retry rejected entries while restoring the submitted text."""
        typed = list(self.value)
        message = ""
        # Backspace leaves the screen only while the field is untouched: a
        # field that had content could not be cleared otherwise, and several
        # of them mean something by empty. Left always leaves, because a
        # prefilled field otherwise has no way back at all: the hostname
        # screen answered backspace by deleting and escape by offering to end
        # the run.
        touched = False
        while True:
            self._draw(screen, typed, message)
            pressed = screen.key()
            if pressed in ("\n", "KEY_ENTER"):
                checked = validator("".join(typed))
                if not isinstance(checked, TextFieldRejected):
                    return checked
                typed = list(checked.correction)
                message = checked.message
                touched = False
            elif pressed in ("KEY_LEFT", "\x1b"):
                return Answer(Outcome.BACK)
            elif pressed == CLEAR_KEY:
                # Every field here arrives with a value in it, and replacing
                # one meant counting its characters and pressing backspace
                # that many times: an operator asked for a 512 MiB partition
                # and the field answered `1GiB512MiB`.
                typed.clear()
                touched = True
            elif pressed in ("\x7f", "KEY_BACKSPACE"):
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

    def _draw(self, screen: Screen, typed: list[str], message: str = "") -> None:
        lines, columns = screen.size()
        screen.clear()
        screen.write(0, 0, clip(self.title, columns))
        row = 2
        if message:
            screen.write(1, 2, clip(message, columns - 2), style=Style.REQUIRED)
            row = 3
        # Brackets and a caret, drawn highlighted: a bare string at the top of
        # an empty screen does not read as somewhere to type.
        room = columns - 8
        shown = "*" * len(typed) if self.masked else "".join(typed)
        shown = _tail_that_fits(shown, room - 1)
        if self.detail:
            # Wrapped, not clipped: the exact string to type is the last thing
            # in this line and the first thing a clip removes. An operator
            # whose screen cut the line short typed the row's own name instead.
            # Bounded so the field and the footer keep their rows: a detail
            # long enough to fill the screen would otherwise push the box off
            # the bottom and leave nowhere to type.
            for one in wrap_to_cells(self.detail, columns)[: max(0, lines - row - 2)]:
                screen.write(row, 0, one)
                row += 1
            if row < lines - 2:
                row += 1
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


@dataclass(frozen=True)
class TextFieldRejected:
    """A submitted text field that must be redrawn with an explanation."""

    message: str
    correction: str


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
    #: No default: the one form that took it drew `Done` in the middle of a
    #: Traditional Chinese screen, and five others passed the catalog's word.
    done: str
    footer: str = ""
    #: What the marks in the body mean, kept at the end of the footer line so
    #: it does not read as one more key.
    legend: str = ""
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
        # Backspace leaves only an empty field nobody has edited, so clearing
        # a field never exits the form.
        touched = [False] * len(self.fields)
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
                    touched = [False] * len(self.fields)
                    continue
                cursor += 1
            elif pressed == " " and cursor < len(self.fields) and self.fields[cursor].toggle:
                typed[cursor] = [] if typed[cursor] else ["x"]
                touched[cursor] = True
            elif pressed == CLEAR_KEY:
                if cursor < len(self.fields) and not self.fields[cursor].toggle:
                    typed[cursor].clear()
                    touched[cursor] = True
            elif pressed in ("KEY_LEFT", "\x1b"):
                return Answer(Outcome.BACK)
            elif pressed in ("\x7f", "KEY_BACKSPACE"):
                if cursor == len(self.fields):
                    return Answer(Outcome.BACK)
                if typed[cursor] and not self.fields[cursor].toggle:
                    typed[cursor].pop()
                    touched[cursor] = True
                elif not touched[cursor]:
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
                touched[cursor] = True

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


#: The left pane is the widest row in the catalog plus a marker and a space,
#: never narrower than this and never past half the terminal: the ceiling is
#: the width itself now, so a wide screen stops cutting values.
LEFT_PANE_MINIMUM: Final[int] = 20

#: The marker column and the space after it, before every label.
MARKER_ROOM: Final[int] = 2

#: Below this the interface draws nothing and says so. One pane needs a label
#: it does not have to cut, which is what `LEFT_PANE_MINIMUM` stands for, and
#: a title, the cursor's row, the two summary lines under it and a footer.
#: Measured against every widget on 2026-08-21: the narrowest any of them can
#: still put its content on is 7x5, so this floor is above all of them.
MINIMUM_COLUMNS: Final[int] = LEFT_PANE_MINIMUM
MINIMUM_LINES: Final[int] = 6

#: The separator column and the space after it, between the two panes.
PANE_GAP: Final[int] = 2

#: ASCII: the medium's own console font carries no box-drawing set.
SEPARATOR: Final[str] = "|"




def left_pane_width(rows: Iterable[tuple[str, str]], columns: int = TWO_PANE_COLUMNS) -> int:
    """How wide the left pane stands for this catalog on this terminal.

    Measured from the label **and** the value: the same rows are 25 cells in
    English and 30 in Japanese, and sizing from the label alone left the value
    a few cells, which `spread` then dropped whole. Half the terminal is the
    ceiling, so a wide screen stops cutting values and a narrow one still
    leaves the right pane something to stand in.
    """
    # The first value only, plus room for the count of the rest: one grouped
    # row answering `/dev/sda, gpt, whole-disk (erases the disk), /efi 1GiB,
    # / the rest` widened the pane for every row, and its tail is what the
    # right pane and the screen behind it are for.
    # The value is drawn in a column of its own two cells after the widest
    # label, so the pane needs the widest label and the widest value, not the
    # widest row: those are different rows, and adding them per row sized the
    # pane for a short label beside a long value and cut both.
    pairs = list(rows)
    label = max((width(one) for one, _ in pairs), default=0)
    value = max((width(state.split(", ")[0]) for _, state in pairs), default=0)
    wanted = MARKER_ROOM + label + 2 + value
    return min(max(LEFT_PANE_MINIMUM, wanted), max(LEFT_PANE_MINIMUM, columns // 2))


def wrap_to_cells(text: str, room: int) -> list[str]:
    """Broken on cells and after a separator, never through a value.

    `textwrap` counts a wide character as one, so a Chinese line folds at the
    wrong column; and a break inside `zh_TW.UTF-8` names no locale at all.
    """
    if room <= 0:
        return []
    if not text:
        return [""]
    lines: list[str] = []
    rest = text
    while rest:
        if width(rest) <= room:
            lines.append(rest)
            break
        cut = len(truncate(rest, room))
        at = max(rest.rfind(", ", 0, cut + 1), rest.rfind(" ", 0, cut + 1))
        if at > 0:
            cut = at + 1
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return lines


def fit(parts: list[str], room: int) -> str:
    """As many of the values as the room holds, and no count of the rest.

    The count was noise: `grub, uefi +1` on a pane with room for two values
    spends four cells saying that a third exists, and the pane beside it and
    the screen behind the row both carry the whole answer already.
    """
    said = [one for one in parts if one]
    taken: list[str] = []
    for value in said:
        if width(", ".join([*taken, value])) > room:
            break
        taken.append(value)
    if taken:
        return ", ".join(taken)
    return clip(said[0], room) if said else ""


def section_rule(name: str, room: int) -> str:
    """The heading centred in a band that fills the pane.

    Spaces rather than dashes: the band is drawn in reverse video, and a row
    of dashes reversed reads as stripes rather than as a divider. A console
    with no colour still shows the attribute.
    """
    if room <= 0:
        return ""
    name = clip(name, max(0, room - 2))
    rest = room - width(name)
    if rest <= 0:
        return name
    before = rest // 2
    return " " * before + name + " " * (rest - before)


def right_pane_width(columns: int, left: int) -> int:
    """What is left for the right pane once the separator and its space are."""
    return columns - left - PANE_GAP


def reason_lines(reason: str) -> tuple[str, ...]:
    """A refusal laid out one part to a line.

    Wrapped instead, `Install mode, Drive, Mirrors: still needs an answer`
    breaks wherever the pane happens to end and the operator reads `still
    needs an` on one line. The parts are what the reason is made of, so the
    line breaks go where they already are.
    """
    if not reason:
        return ()
    head, separator, tail = reason.partition(": ")
    parts = [one.strip() for one in head.split(",") if one.strip()]
    return (*parts, tail.strip()) if separator else tuple(parts)


@dataclass(frozen=True)
class PaneRow(Generic[V]):
    """One row of the left pane, and what the right pane says about it."""

    label: str
    value: V
    #: The heading this row sits under. Drawn once above the first row of each
    #: section, so a list of twenty-four rows reads as an order to work
    #: through rather than one lump.
    section: str = ""
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
            elif pressed in PAGE_BACKWARD:
                cursor = max(0, cursor - self._page(screen))
            elif pressed in PAGE_FORWARD:
                cursor = min(max(0, len(self.rows) - 1), cursor + self._page(screen))
            elif pressed in FIRST:
                cursor = 0
            elif pressed in LAST:
                cursor = max(0, len(self.rows) - 1)
            elif pressed == HELP_KEY:
                screen.help()
            elif pressed in ("\n", "KEY_ENTER", "KEY_RIGHT"):
                # A disabled row answers nothing rather than opening a screen
                # that would refuse: its reason is already in the right pane.
                if self.rows and not self.rows[cursor].disabled_because:
                    return Answer(Outcome.CHOSE, self.rows[cursor].value)
            elif pressed in ("KEY_LEFT", "\x1b", "\x7f", "KEY_BACKSPACE"):
                # Back, which at the top of the interface is nowhere to go.
                # Held apart from `q` because they mean different things: an
                # agent read the key page's `esc  Back` and pressed it seven
                # times, and one of those presses offered to end the run.
                return Answer(Outcome.BACK)
            elif pressed == "q":
                # Leaving, which is what the status line names it. Here and
                # nowhere deeper, because a text field reads it as a letter.
                return Answer(Outcome.CANCELLED)
            elif pressed == "\x03":
                return Answer(Outcome.CANCELLED)

    @staticmethod
    def _draw_box(screen: Screen, column: int, width_of: int, lines: int, title: str) -> None:
        """An ASCII frame with the row's name in its top edge.

        ASCII and not `U+250C`: the medium's own console font carries no
        box-drawing set, and a frame of question marks is worse than none.
        """
        if width_of < 4:
            return
        named = clip(f" {title} ", max(0, width_of - 4)) if title else ""
        # Three, not two: the corner at each end and the dash between them.
        # Counting two put one cell past the pane and the closing corner was
        # trimmed away, so the box had a top edge that never closed.
        top = f"+-{named}" + "-" * max(0, width_of - 3 - width(named)) + "+"
        screen.write(2, column, clip(top, width_of))
        for line in range(3, lines - 2):
            screen.write(line, column, "|")
            screen.write(line, column + width_of - 1, "|")
        screen.write(lines - 2, column, "+" + "-" * max(0, width_of - 2) + "+")

    @staticmethod
    def _page(screen: Screen) -> int:
        """The rows one screen holds, which is what both layouts draw between
        the title row and the status line."""
        lines, _ = screen.size()
        return max(1, lines - 3)

    def _draw(self, screen: Screen, cursor: int) -> None:
        region = self.frame(screen, cursor, dimmed=False)
        if region is not None:
            # The reason heads the pane: a row that cannot be opened has to say
            # why where the operator is already looking.
            here = self.rows[cursor] if self.rows else None
            detail: tuple[str, ...] = ()
            if here is not None:
                detail = (*reason_lines(here.disabled_because), *here.detail)
            wrapped: list[str] = []
            for line_text in detail:
                # Wrapped here and not by the caller: only the pane knows how
                # wide it ended up, and a width guessed before the draw folded
                # the text at a column that had nothing to do with the pane.
                wrapped.extend(wrap_to_cells(line_text, region.columns))
            for offset, line_text in enumerate(wrapped[: region.lines]):
                region.write(offset, 0, line_text)
        screen.show()

    def _frame_only(self, screen: Screen, cursor: int, *, dimmed: bool) -> None:
        """Draw the frame without answering with a rectangle.

        Named so the redraw a resize needs cannot build a second `Region`: one
        of those would carry its own callback and the two would take turns
        drawing each other.
        """
        self.frame(screen, cursor, dimmed=dimmed)

    def frame(self, screen: Screen, cursor: int, *, dimmed: bool) -> Region | None:
        """Draw the list, the separator and the status line, and answer with
        the rectangle beside them.

        Split out so an editor can run inside that rectangle: the frame stays
        on screen with the row it opened still marked, rather than the whole
        interface being replaced by whatever screen the row leads to.
        """
        lines, columns = screen.size()
        screen.clear()
        # A bar across the width, like the section bands under it and the
        # status line at the foot: a title floating at the margin with a
        # counter at the far edge read as two loose words.
        screen.write(0, 0, spread(f" {self.title}", f"{self.counter} ", columns), highlight=True)
        if columns < TWO_PANE_COLUMNS or lines < TWO_PANE_LINES:
            self._draw_one_pane(screen, cursor, lines, columns)
            if lines > 1 and (self.footer or self.legend):
                screen.write(lines - 1, 0, spread(self.footer, self.legend, columns))
            return None
        left = left_pane_width(((row.label, row.state) for row in self.rows), columns)
        right = right_pane_width(columns, left)
        room = lines - 3
        # A box rather than one rule: a single column of `|` reads as an edge
        # of the list, and the pane beside it as loose text. `archinstall`
        # frames its own preview the same way.
        # Only while the row is closed. Opened, the screen inside the box
        # writes its own title on the first line, and the box was saying the
        # same word one cell above it.
        named = "" if dimmed else (self.rows[cursor].label if self.rows else "")
        # The frame's left edge stands in the column the single rule used to,
        # so the list keeps its width and no column is spent twice.
        self._draw_box(screen, left, columns - left, lines, named)
        # One entry per drawn line: a heading takes a line of its own, so the
        # window has to count them or the last rows fall off the bottom.
        entries: list[int | None] = []
        heading = ""
        for index, row in enumerate(self.rows):
            if row.section and row.section != heading:
                heading = row.section
                entries.append(None)
            entries.append(index)
        here = entries.index(cursor) if cursor in entries else 0
        top = _window(here, room, len(entries))
        headings = {at: row.section for at, row in enumerate(self.rows)}
        for offset, entry in enumerate(entries[top : top + room]):
            if entry is None:
                # Found by walking forward: the heading belongs to the row
                # under it, and the entry itself carries no index.
                after = next(
                    (one for one in entries[top + offset + 1 :] if one is not None), None
                )
                if after is not None:
                    screen.write(
                        offset + 2, 0, section_rule(headings[after], left), highlight=not dimmed
                    )
                continue
            self._draw_row(
                screen,
                offset + 2,
                entry,
                entry == cursor,
                MARKER_ROOM,
                left - MARKER_ROOM,
                dimmed=dimmed,
            )
        if lines > 1 and (self.footer or self.legend):
            screen.write(lines - 1, 0, spread(self.footer, self.legend, columns))
        # The same arithmetic, handed over rather than its result: the pane is
        # redrawn at the new size after a resize and the rectangle has to move
        # with it.
        return Region(
            screen,
            3,
            left + 2,
            room - 2,
            max(1, columns - left - 4),
            cut=self._pane,
            redraw=lambda: self._frame_only(screen, cursor, dimmed=dimmed),
            _seen=(lines, columns),
        )

    def _pane(self, lines: int, columns: int) -> tuple[int, int, int, int] | None:
        """Where the right pane sits on a screen this size."""
        if columns < TWO_PANE_COLUMNS or lines < TWO_PANE_LINES:
            return None
        left = left_pane_width(((row.label, row.state) for row in self.rows), columns)
        return 3, left + 2, max(1, lines - 3 - 2), max(1, columns - left - 4)

    def _draw_one_pane(self, screen: Screen, cursor: int, lines: int, columns: int) -> None:
        """One pane, and the row under the cursor carries two of its lines.

        The floor is a measured boundary rather than best effort: below it the
        right pane cannot hold a value, so it stops existing.
        """
        here_row = self.rows[cursor] if self.rows else None
        below: tuple[str, ...] = ()
        if here_row is not None:
            below = (*reason_lines(here_row.disabled_because), *here_row.detail)
        # A band of its own above the status line rather than lines pushed in
        # between the rows: inserting them moved every row under the cursor,
        # so walking the list made the interface jump and hid the next row.
        said = [one for one in below[:2] if one]
        room = lines - 3 - (len(said) + 1 if said else 0)
        if room <= 0:
            return
        # Three fields, because a band and a row are drawn differently and
        # telling them apart by their first character broke the moment the
        # band was filled with spaces rather than dashes.
        entries: list[tuple[int | None, str, bool]] = []
        heading = ""
        for index, row in enumerate(self.rows):
            # One pane needs the bands more than two do, not less: the whole
            # list is one column and nothing else separates one group of rows
            # from the next.
            if row.section and row.section != heading:
                heading = row.section
                entries.append((None, section_rule(row.section, columns), True))
            entries.append((index, "", False))
        # Title, blank, the rows, the rule, the said lines, the status line.
        for offset, line_text in enumerate(said):
            screen.write(lines - 1 - len(said) + offset, 2, clip(line_text, columns - 4))
        if said:
            screen.write(lines - 2 - len(said), 0, "-" * columns)
        here = next((at for at, (which, _, _) in enumerate(entries) if which == cursor), 0)
        top = _window(here, room, len(entries))
        for offset, (which, text, band) in enumerate(entries[top : top + room]):
            if which is None:
                screen.write(offset + 2, 0, clip(text, columns), highlight=band)
                continue
            self._draw_row(
                screen, offset + 2, which, which == cursor, MARKER_ROOM, columns - MARKER_ROOM
            )

    def _draw_row(
        self,
        screen: Screen,
        line: int,
        index: int,
        focused: bool,
        column: int,
        room: int,
        *,
        dimmed: bool = False,
    ) -> None:
        row = self.rows[index]
        style = Style.DIMMED if dimmed else row.style
        if row.style is not Style.PLAIN:
            # The marker is the signal and the colour repeats it: a monochrome
            # serial console has to show the same thing.
            screen.write(line, 0, MARKS[row.style], style=style)
        # The value in a column of its own two cells after the widest label,
        # rather than pushed to the far edge: a pane sized for the longest row
        # left every short one with a hand's width of blank between the two.
        at = min(self._value_column(), max(1, room - 1))
        label = clip(row.label, max(1, at - 1))
        # Fitted here, against the room this row actually has: the `+N` and
        # the cut both belong to the draw, or a window opened small keeps
        # every value cut after it grows.
        state = fit(row.state.split(", "), max(0, room - at))
        drawn = f"{label}{' ' * max(1, at - width(label))}{state}"
        # Padded by cells and not by characters: `str.ljust` counts a wide
        # character as one, so a Chinese label padded that way runs past the
        # separator and erases it.
        screen.write(
            line,
            column,
            drawn + " " * max(0, room - width(drawn)),
            # The row stays marked while its editor holds the pane, so the
            # operator can see which one they are answering.
            highlight=focused,
            style=style,
        )

    def _value_column(self) -> int:
        """Two cells after the widest label in the list."""
        return max((width(row.label) for row in self.rows), default=0) + 2


def _window(cursor: int, room: int, total: int) -> int:
    """The first row drawn, so the cursor stays on screen without wrapping."""
    if room <= 0 or total <= room:
        return 0
    return max(0, min(cursor - room // 2, total - room))
