# SPDX-License-Identifier: GPL-2.0-or-later
"""Walk the menu in a real terminal, on the medium it ships on.

`tests/unit/` drives the screens through `FakeScreen`, which answers a key and
records the lines a screen asked to draw. That found nothing an operator met:
the timezone screen offering `UTC` alone, the root password row disagreeing
with the Install row, and eight rows that read their first entry back were all
reported from a machine or from reading the code. What `FakeScreen` cannot
show is what curses actually puts on an 80-column console, so this boots the
medium, starts the menu on the serial port and reads the screen back.

    python3 -m tests.vm.tui
    python3 -m tests.vm.tui --lang zh-TW --keep
"""

from __future__ import annotations

import argparse
import codecs
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .console import ConsoleTimeout, SerialConsole
from .driver import build as build_driver, wait_for_driver
from .run import create_target
from .media import MEDIA
from .qemu import Firmware, Vm, VmSpec
from .workdir import WorkdirError, confined

WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/tui"

#: How long the walk waits after a lone escape. ncurses holds one for
#: ESCDELAY, a second by default, in case it starts a sequence.
ESCAPE_SETTLES: Final[float] = 1.5

#: The console the menu is drawn on. Eighty by twenty-four is what the medium
#: gives over a serial port and the smallest the interface supports, so a row
#: that only fits a wider terminal is a defect an operator meets first.
COLUMNS: Final[int] = 80
LINES: Final[int] = 24

#: How long a redraw is collected for. curses writes a screen in many small
#: writes, and reading for less than this catches half of one.
REDRAW_SECONDS: Final[float] = 2.0

_ESCAPE: Final[int] = 0x1B
_CONTROL_STRINGS: Final[frozenset[int]] = frozenset(b"]PX^_")
_MAX_ESCAPE_BYTES: Final[int] = 256


@dataclass
class VTScreen:
    """A bounded terminal screen whose state survives serial-console reads."""

    columns: int = COLUMNS
    lines: int = LINES
    row: int = field(default=0, init=False)
    column: int = field(default=0, init=False)
    saved_cursor: tuple[int, int] = field(default=(0, 0), init=False)
    scrolling_region: tuple[int, int] = field(default=(0, LINES - 1), init=False)
    _grid: list[list[str | None]] = field(init=False, repr=False)
    _escape: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _discarding_control: int | None = field(default=None, init=False, repr=False)
    _discarding_after_escape: bool = field(default=False, init=False, repr=False)
    _discarding_escape: int | None = field(default=None, init=False, repr=False)
    _decoder: codecs.IncrementalDecoder = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.columns <= 0 or self.lines <= 0:
            raise ValueError("a screen must have positive dimensions")
        self._grid = [[] for _ in range(self.lines)]
        self.scrolling_region = (0, self.lines - 1)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    @property
    def cursor(self) -> tuple[int, int]:
        return self.row, self.column

    def rows(self) -> list[str]:
        """Return a stable copy of the rows currently visible."""
        return [
            "".join(cell for cell in line if cell is not None).rstrip()
            for line in self._grid
        ]

    def feed(self, chunk: bytes) -> None:
        """Apply output while retaining only a bounded incomplete control."""
        data = bytes(self._escape) + chunk
        self._escape.clear()
        while True:
            if self._discarding_control is not None:
                end = self._discarded_control_end(data)
                if end is None:
                    return
                data = data[end:]
            elif self._discarding_escape is not None:
                end = self._discarded_escape_end(data)
                if end is None:
                    return
                data = data[end:]
            if not data:
                return

            at = plain = 0
            while at < len(data):
                byte = data[at]
                if byte == _ESCAPE:
                    self._write(data[plain:at])
                    end = self._escape_end(data, at)
                    if end is None:
                        remainder = self._retain_incomplete_escape(data, at)
                        if remainder is None:
                            return
                        data = remainder
                        break
                    self._dispatch_escape(data[at:end])
                    at = plain = end
                elif byte < 0x20 or byte == 0x7F:
                    self._write(data[plain:at])
                    self._dispatch_control(byte)
                    at += 1
                    plain = at
                else:
                    at += 1
            else:
                self._write(data[plain:])
                return

    def _retain_incomplete_escape(self, data: bytes, start: int) -> bytes | None:
        sequence = data[start:]
        if len(sequence) < _MAX_ESCAPE_BYTES:
            self._escape.extend(sequence)
            return None
        prefix = sequence[:_MAX_ESCAPE_BYTES]
        if prefix[1] in _CONTROL_STRINGS:
            self._discarding_control = prefix[1]
            self._discarding_after_escape = prefix[-1] == _ESCAPE
        else:
            self._discarding_escape = prefix[1]
        return sequence[_MAX_ESCAPE_BYTES:]

    def _discarded_escape_end(self, data: bytes) -> int | None:
        kind = self._discarding_escape
        assert kind is not None
        final_first = 0x40 if kind == ord("[") else 0x30
        for at, byte in enumerate(data):
            if byte == _ESCAPE:
                self._discarding_escape = None
                return at
            if final_first <= byte <= 0x7E:
                self._discarding_escape = None
                return at + 1
        return None

    def _discarded_control_end(self, data: bytes) -> int | None:
        kind = self._discarding_control
        assert kind is not None
        at = 0
        if self._discarding_after_escape:
            if not data:
                return None
            self._discarding_after_escape = False
            if data[0] == ord("\\"):
                self._discarding_control = None
                return 1
        while at < len(data):
            if kind == ord("]") and data[at] == 0x07:
                self._discarding_control = None
                return at + 1
            if data[at] == _ESCAPE:
                if at + 1 == len(data):
                    self._discarding_after_escape = True
                    return None
                if data[at + 1] == ord("\\"):
                    self._discarding_control = None
                    return at + 2
            at += 1
        return None

    def _escape_end(self, data: bytes, start: int) -> int | None:
        if start + 1 >= len(data):
            return None
        limit = min(len(data), start + _MAX_ESCAPE_BYTES)
        kind = data[start + 1]
        if kind == ord("["):
            for at in range(start + 2, limit):
                if 0x40 <= data[at] <= 0x7E:
                    return at + 1
            return None
        if kind in _CONTROL_STRINGS:
            for at in range(start + 2, limit):
                if kind == ord("]") and data[at] == 0x07:
                    return at + 1
                if (
                    data[at] == _ESCAPE
                    and at + 1 < limit
                    and data[at + 1] == ord("\\")
                ):
                    return at + 2
            return None
        at = start + 1
        while at < limit and 0x20 <= data[at] <= 0x2F:
            at += 1
        if at < limit and 0x30 <= data[at] <= 0x7E:
            return at + 1
        return None

    def _dispatch_escape(self, sequence: bytes) -> None:
        if sequence.startswith(b"\x1b["):
            body = sequence[2:-1]
            split = 0
            while split < len(body) and 0x30 <= body[split] <= 0x3F:
                split += 1
            parameters = body[:split].decode("ascii")
            intermediates = body[split:].decode("ascii")
            self._dispatch_csi(parameters, intermediates, chr(sequence[-1]))
        elif sequence == b"\x1b7":
            self.saved_cursor = self.cursor
        elif sequence == b"\x1b8":
            self.row, self.column = self.saved_cursor
            self._clamp_cursor()
        elif sequence == b"\x1bD":
            self._index(reset_column=False)
        elif sequence == b"\x1bE":
            self._index(reset_column=True)
        elif sequence == b"\x1bM":
            self._reverse_index()

    def _dispatch_csi(self, parameters: str, intermediates: str, final: str) -> None:
        """Apply the CSI operations emitted by curses on a serial terminal."""
        if intermediates:
            return
        private = parameters.startswith(("<", "=", ">", "?"))
        raw = parameters[1:] if private else parameters
        if private or any(character not in "0123456789;" for character in raw):
            return
        values = [int(value) if value else None for value in raw.split(";")]
        if final in ("H", "f"):
            self.row = self._position(values, 0) - 1
            self.column = self._position(values, 1) - 1
            self._clamp_cursor()
        elif final in ("G", "`"):
            self.column = self._position(values, 0) - 1
            self._clamp_cursor()
        elif final == "d":
            self.row = self._position(values, 0) - 1
            self._clamp_cursor()
        elif final in "ABCD":
            amount = self._amount(values)
            if final == "A":
                self.row -= amount
            elif final == "B":
                self.row += amount
            elif final == "C":
                self.column += amount
            else:
                self.column -= amount
            self._clamp_cursor()
        elif final in "EF":
            amount = self._amount(values)
            self.row += amount if final == "E" else -amount
            self.column = 0
            self._clamp_cursor()
        elif final in "ae":
            amount = self._amount(values)
            if final == "a":
                self.column += amount
            else:
                self.row += amount
            self._clamp_cursor()
        elif final == "J":
            self._erase_display(values[0] or 0)
        elif final == "K":
            self._erase_line(values[0] or 0)
        elif final == "X":
            self._erase_characters(self._amount(values))
        elif final in "ST":
            amount = self._amount(values)
            if final == "S":
                self._scroll_up(amount)
            else:
                self._scroll_down(amount)
        elif final in "LM":
            self._insert_or_delete_lines(self._amount(values), insert=final == "L")
        elif final in "@P":
            self._insert_or_delete_characters(self._amount(values), insert=final == "@")
        elif final == "r":
            self._set_scrolling_region(values)
        elif final == "s":
            self.saved_cursor = self.cursor
        elif final == "u":
            self.row, self.column = self.saved_cursor
            self._clamp_cursor()

    def _position(self, values: list[int | None], index: int) -> int:
        if index >= len(values) or values[index] in (None, 0):
            return 1
        value = values[index]
        assert value is not None
        return value

    def _amount(self, values: list[int | None]) -> int:
        return self._position(values, 0)

    def _clamp_cursor(self) -> None:
        self.row = min(max(0, self.row), self.lines - 1)
        # Keep one extra screen so the width audit still catches overflow.
        self.column = min(max(0, self.column), self.columns * 2 - 1)

    def _write(self, data: bytes) -> None:
        for character in self._decoder.decode(data, final=False):
            self._place(character)

    def _place(self, character: str) -> None:
        line = self._grid[self.row]
        width = cells(character)
        if width <= 0:
            at = min(self.column - 1, len(line) - 1)
            while at >= 0 and line[at] is None:
                at -= 1
            if at >= 0:
                base = line[at]
                assert base is not None
                line[at] = base + character
            return

        limit = self.columns * 2
        stop = min(self.column + width, limit)
        while len(line) < stop:
            line.append(" ")
        for at in range(self.column, stop):
            self._clear_glyph_at(line, at)
        line[self.column] = character
        for at in range(self.column + 1, stop):
            line[at] = None
        self.column = min(self.column + width, limit - 1)

    def _clear_glyph_at(self, line: list[str | None], at: int) -> None:
        if not 0 <= at < len(line):
            return
        start = at
        while start > 0 and line[start] is None:
            start -= 1
        base = line[start]
        if base is None:
            line[at] = " "
            return
        stop = min(start + max(1, cells(base)), len(line))
        for cell in range(start, stop):
            line[cell] = " "

    def _blank_cells(self, line: list[str | None], start: int, stop: int) -> None:
        stop = min(stop, len(line))
        for at in range(start, stop):
            self._clear_glyph_at(line, at)
        for at in range(start, stop):
            line[at] = " "

    def _dispatch_control(self, byte: int) -> None:
        if byte == 0x0D:
            self.column = 0
        elif byte in (0x0A, 0x0B, 0x0C):
            self._index(reset_column=True)
        elif byte == 0x08:
            self.column = max(0, self.column - 1)
        elif byte == 0x09:
            self.column = min((self.column // 8 + 1) * 8, self.columns * 2 - 1)

    def _index(self, reset_column: bool) -> None:
        _, bottom = self.scrolling_region
        if self.row == bottom:
            self._scroll_up(1)
        else:
            self.row = min(self.row + 1, self.lines - 1)
        if reset_column:
            self.column = 0

    def _reverse_index(self) -> None:
        top, _ = self.scrolling_region
        if self.row == top:
            self._scroll_down(1)
        else:
            self.row = max(0, self.row - 1)

    def _erase_display(self, mode: int) -> None:
        if mode == 0:
            self._erase_line(0)
            for row in range(self.row + 1, self.lines):
                self._grid[row].clear()
        elif mode == 1:
            for row in range(self.row):
                self._grid[row].clear()
            self._erase_line(1)
        elif mode in (2, 3):
            for line in self._grid:
                line.clear()

    def _erase_line(self, mode: int) -> None:
        line = self._grid[self.row]
        if mode == 0:
            self._clear_glyph_at(line, self.column)
            del line[self.column :]
        elif mode == 1:
            self._blank_cells(line, 0, self.column + 1)
        elif mode == 2:
            line.clear()

    def _erase_characters(self, amount: int) -> None:
        line = self._grid[self.row]
        self._blank_cells(line, self.column, self.column + amount)

    def _scroll_up(self, amount: int) -> None:
        top, bottom = self.scrolling_region
        count = min(amount, bottom - top + 1)
        region = self._grid[top : bottom + 1]
        self._grid[top : bottom + 1] = region[count:] + [[] for _ in range(count)]

    def _scroll_down(self, amount: int) -> None:
        top, bottom = self.scrolling_region
        count = min(amount, bottom - top + 1)
        region = self._grid[top : bottom + 1]
        self._grid[top : bottom + 1] = [[] for _ in range(count)] + region[:-count]

    def _insert_or_delete_lines(self, amount: int, insert: bool) -> None:
        top, bottom = self.scrolling_region
        if not top <= self.row <= bottom:
            return
        count = min(amount, bottom - self.row + 1)
        region = self._grid[self.row : bottom + 1]
        if insert:
            replacement: list[list[str | None]] = [
                [] for _ in range(count)
            ] + region[:-count]
        else:
            replacement = region[count:] + [[] for _ in range(count)]
        self._grid[self.row : bottom + 1] = replacement

    def _insert_or_delete_characters(self, amount: int, insert: bool) -> None:
        line = self._grid[self.row]
        if self.column >= len(line):
            return
        if insert:
            if line[self.column] is None:
                self._clear_glyph_at(line, self.column)
            line[self.column : self.column] = [" "] * amount
            self._clear_glyph_at(line, self.columns * 2)
            del line[self.columns * 2 :]
        else:
            self._clear_glyph_at(line, self.column)
            self._clear_glyph_at(line, self.column + amount - 1)
            del line[self.column : self.column + amount]

    def _set_scrolling_region(self, values: list[int | None]) -> None:
        top = self._position(values, 0) - 1
        bottom_value = self.lines
        if len(values) > 1 and values[1] not in (None, 0):
            chosen = values[1]
            assert chosen is not None
            bottom_value = chosen
        bottom = bottom_value - 1
        if 0 <= top < bottom < self.lines:
            self.scrolling_region = top, bottom
            self.row = self.column = 0


def rendered(said: bytes) -> list[str]:
    """The rows a screen holds, replayed into a grid.

    Taking the escape codes out is not enough. curses draws by moving the
    cursor, not by ending lines, so stripping the codes ran the whole screen
    together and the width check reported a 597-cell row that is 24 rows of at
    most 80. The sequences that move or clear are replayed instead, and what is
    measured is where each character actually landed.
    """
    screen = VTScreen()
    screen.feed(said)
    return screen.rows()


@dataclass(frozen=True)
class ScreenState:
    """The visible state used by menu readiness and navigation checks."""

    lines: tuple[str, ...]

    @classmethod
    def from_screen(cls, screen: VTScreen) -> ScreenState:
        return cls(tuple(screen.rows()))

    @property
    def title(self) -> str:
        return self.lines[0] if self.lines else ""

    @property
    def drawn(self) -> bool:
        return bool(
            len(self.lines) == LINES
            and self.title
            and any(line.strip() for line in self.lines[1:-1])
            and self.lines[-1].strip()
        )

    @property
    def main_menu(self) -> bool:
        return self.drawn and self.title == "gentoo-install"

    def opened_from(self, previous: ScreenState) -> bool:
        return previous.main_menu and self.drawn and self.title != previous.title


@dataclass
class Finding:
    where: str
    what: str


@dataclass
class Walk:
    """What the walk saw, and what was wrong with it."""

    screens: int = 0
    findings: list[Finding] = field(default_factory=list)

    def note(self, where: str, what: str) -> None:
        self.findings.append(Finding(where=where, what=what))


#: What `width()` counts as two cells. A menu drawn with a wide character on a
#: console with no CJK font still occupies two columns, so the check is on the
#: cells rather than on the glyphs.
def cells(line: str) -> int:
    from gentoo_install.i18n import width

    return width(line)


#: How long the menu is waited for. The interpreter starts, reads the driver
#: archive and probes the disks before it draws anything.
MENU_PATIENCE: Final[float] = 180.0


def _wait_for_menu(
    console: SerialConsole, screen: VTScreen | None = None
) -> ScreenState:
    if screen is None:
        screen = VTScreen()
    deadline = time.monotonic() + MENU_PATIENCE
    last = ScreenState(())
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        screen.feed(console.snapshot(min(REDRAW_SECONDS, remaining)))
        last = ScreenState.from_screen(screen)
        if last.main_menu:
            return last
    raise ConsoleTimeout(
        f"menu was not drawn before timeout; last rendered state was {last.lines!r}"
    )


def _open_menu(console: SerialConsole, lang: str) -> None:
    # Through the shared search, not a device node: the walk hardcoded
    # `/dev/sr1` and the guest answered `Can't open blockdev`, so the tarball
    # was never unpacked and the menu never started.
    wait_for_driver(console)
    console.run("mkdir -p /tmp/driver && tar xzf /mnt/driver/driver.tar.gz -C /tmp/driver")
    console.run(f"stty rows {LINES} cols {COLUMNS}")
    # `TERM` explicitly: a serial getty leaves it unset and curses then refuses
    # to start, which reads as the installer crashing.
    console.send_raw(
        f"cd /tmp/driver && TERM=vt220 LINES={LINES} COLUMNS={COLUMNS} "
        f"python3 -m gentoo_install --lang {lang}\n"
    )


def walk(console: SerialConsole, lang: str) -> Walk:
    """Open every row of the menu and read what the terminal shows."""
    seen = Walk()
    screen = VTScreen()
    _open_menu(console, lang)
    # Waited for rather than slept through: a walk that sent its first keys on
    # a timer had them land on the shell, and the escape that followed reached
    # the main menu, where escape means leave. The recording then held one
    # screen and the review of it reported nothing.
    _wait_for_menu(console, screen)
    console.send_raw("\x1b[B")
    time.sleep(0.5)
    console.send_raw("\x1b[A")
    time.sleep(0.5)
    for step in range(_ROWS):
        screen.feed(console.snapshot(REDRAW_SECONDS))
        drawn = ScreenState.from_screen(screen)
        body = [line for line in drawn.lines if line.strip()]
        if not body:
            seen.note(f"row {step}", "the row drew nothing")
        else:
            seen.screens += 1
            for line in body:
                if cells(line) > COLUMNS:
                    seen.note(f"row {step}", f"{cells(line)} cells: {line!r}")
        # Enter opens the row, backspace leaves it, then down moves on.
        console.send_raw("\r")
        time.sleep(1.0)
        screen.feed(console.snapshot(REDRAW_SECONDS))
        opened = ScreenState.from_screen(screen)
        for line in opened.lines:
            if cells(line) > COLUMNS:
                seen.note(f"row {step} opened", f"{cells(line)} cells: {line!r}")
        # Escape cancels, and cancelling is the only leave that changes
        # nothing: backspace deletes inside a text field, and the walk that
        # used it left the machine's hostname as `gento`.
        if opened.opened_from(drawn):
            console.send_raw("\x1b")
            # Longer than ncurses' ESCDELAY, which is a second by default: a
            # key sent inside it is read as the rest of an escape sequence and
            # the escape never reaches the screen it was meant to leave.
            time.sleep(ESCAPE_SETTLES)
        else:
            seen.note(f"row {step}", "enter opened nothing")
        console.send_raw("\x1b[B")
        time.sleep(0.3)
    return seen


#: How many rows are walked. Read from the menu rather than guessed would need
#: the model here; the panel has fewer than this and the extra presses land on
#: the last row, which is harmless and keeps the walk from ending early.
_ROWS: Final[int] = 20


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--medium", default="official-minimal", choices=sorted(MEDIA))
    parser.add_argument("--lang", default="en", help="the interface language to walk")
    parser.add_argument("--workdir", type=Path, default=WORKROOT)
    parser.add_argument("--keep", action="store_true", help="keep the run directory")
    args = parser.parse_args(argv)

    try:
        root = confined(args.workdir)
    except WorkdirError as error:
        print(error, file=sys.stderr)
        return 1
    try:
        workdir = confined(root / f"{args.medium}-{args.lang}")
    except WorkdirError as error:
        print(error, file=sys.stderr)
        return 1
    workdir.mkdir(parents=True, exist_ok=True)
    medium = MEDIA[args.medium]
    driver_iso = build_driver(workdir / "driver.iso", packed=True)
    # A guest with no disk to install onto never reaches the menu: the two
    # recordings this walk produced returned to the shell before any screen was
    # drawn, and the review of them found nothing because there was nothing.
    target = create_target(workdir / "target.qcow2", root=root)
    spec = VmSpec(
        medium=medium,
        workdir=workdir,
        firmware=Firmware.UEFI,
        memory="2G",
        cpus=2,
        driver_iso=driver_iso,
        targets=(target,),
    )
    with Vm(spec) as machine:
        with SerialConsole.connect(machine.serial_socket, machine.serial_log) as console:
            console.expect(medium.root_prompt, timeout=600.0)
            seen = walk(console, args.lang)

    print(f"{seen.screens} screens drawn, {len(seen.findings)} findings", flush=True)
    for one in seen.findings:
        print(f"  {one.where}: {one.what}", flush=True)
    return 1 if seen.findings else 0


if __name__ == "__main__":
    sys.exit(main())
