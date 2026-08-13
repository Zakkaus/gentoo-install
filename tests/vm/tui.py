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
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .console import ConsoleTimeout, SerialConsole
from .driver import build as build_driver
from .run import create_target
from .media import MEDIA
from .qemu import Firmware, Vm, VmSpec
from .workdir import WorkdirError, confined

WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/tui"

#: The console the menu is drawn on. Eighty by twenty-four is what the medium
#: gives over a serial port and the smallest the interface supports, so a row
#: that only fits a wider terminal is a defect an operator meets first.
COLUMNS: Final[int] = 80
LINES: Final[int] = 24

#: How long a redraw is collected for. curses writes a screen in many small
#: writes, and reading for less than this catches half of one.
REDRAW_SECONDS: Final[float] = 2.0

_CSI: Final[re.Pattern[str]] = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")
_OTHER_ESCAPE: Final[re.Pattern[str]] = re.compile(r"\x1b[()][A-B]|\x1b[=>]|\x1b.")


def rendered(said: bytes) -> list[str]:
    """The rows a screen holds, replayed into a grid.

    Taking the escape codes out is not enough. curses draws by moving the
    cursor, not by ending lines, so stripping the codes ran the whole screen
    together and the width check reported a 597-cell row that is 24 rows of at
    most 80. The sequences that move or clear are replayed instead, and what is
    measured is where each character actually landed.
    """
    grid: list[list[str]] = [[] for _ in range(LINES)]
    row = column = 0

    def place(character: str) -> None:
        nonlocal row, column
        if not 0 <= row < LINES:
            return
        line = grid[row]
        while len(line) <= column:
            line.append(" ")
        line[column] = character
        column += 1

    text = said.decode("utf-8", "replace")
    at = 0
    while at < len(text):
        found = _CSI.match(text, at)
        if found:
            at = found.end()
            arguments = [one for one in found.group(1).split(";") if one.isdigit()]
            letter = found.group(2)
            if letter == "H":
                # Rows and columns are one-based in the sequence and zero-based
                # here, and an absent argument means the first.
                row = (int(arguments[0]) if arguments else 1) - 1
                column = (int(arguments[1]) if len(arguments) > 1 else 1) - 1
            elif letter == "J":
                grid = [[] for _ in range(LINES)]
                row = column = 0
            elif letter == "K":
                if 0 <= row < LINES:
                    del grid[row][column:]
            elif letter in "ABCD":
                # curses moves a row at a time far more often than it jumps:
                # without these the grid held the previous screen's rows and
                # the width check measured a menu nobody drew.
                step = int(arguments[0]) if arguments else 1
                if letter == "A":
                    row -= step
                elif letter == "B":
                    row += step
                elif letter == "C":
                    column += step
                else:
                    column = max(0, column - step)
            continue
        found = _OTHER_ESCAPE.match(text, at)
        if found:
            at = found.end()
            continue
        character = text[at]
        at += 1
        if character == "\r":
            column = 0
        elif character == "\n":
            row += 1
            column = 0
        elif character == "\b":
            column = max(0, column - 1)
        elif character == "\x07":
            continue
        else:
            place(character)
    return ["".join(line).rstrip() for line in grid]


@dataclass(frozen=True)
class ScreenState:
    """The small part of terminal state the walk needs to make safe choices.

    Keeping replay behind this boundary lets the pending stateful VT work
    replace `rendered()` without spreading terminal parsing into the walk.
    """

    lines: tuple[str, ...]

    @classmethod
    def replay(cls, said: bytes) -> ScreenState:
        return cls(tuple(rendered(said)))

    @property
    def title(self) -> str:
        return self.lines[0] if self.lines else ""

    @property
    def drawn(self) -> bool:
        """Whether this has the geometry every curses menu screen draws."""
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
        """Whether Enter replaced the main menu with a complete row screen."""
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


def _wait_for_menu(console: SerialConsole) -> ScreenState:
    deadline = time.monotonic() + MENU_PATIENCE
    last = ScreenState(())
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        last = ScreenState.replay(console.snapshot(min(REDRAW_SECONDS, remaining)))
        if last.main_menu:
            return last
    raise ConsoleTimeout(
        f"menu was not drawn before timeout; last rendered state was {last.lines!r}"
    )


def _open_menu(console: SerialConsole, lang: str) -> None:
    console.run("mkdir -p /mnt/driver")
    console.run("mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver")
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
    _open_menu(console, lang)
    # Waited for rather than slept through: a walk that sent its first keys on
    # a timer had them land on the shell, and the escape that followed reached
    # the main menu, where escape means leave. The recording then held one
    # screen and the review of it reported nothing.
    _wait_for_menu(console)
    console.send_raw("\x1b[B")
    time.sleep(0.5)
    console.send_raw("\x1b[A")
    time.sleep(0.5)
    for step in range(_ROWS):
        drawn = ScreenState.replay(console.snapshot(REDRAW_SECONDS))
        body = [line for line in drawn.lines if line.strip()]
        if not body:
            seen.note(f"row {step}", "the row drew nothing")
        else:
            seen.screens += 1
            for line in body:
                if cells(line) > COLUMNS:
                    seen.note(f"row {step}", f"{cells(line)} cells: {line!r}")
        # Enter opens the row, then escape leaves it, then down moves on.
        console.send_raw("\r")
        time.sleep(1.0)
        opened = ScreenState.replay(console.snapshot(REDRAW_SECONDS))
        for line in opened.lines:
            if cells(line) > COLUMNS:
                seen.note(f"row {step} opened", f"{cells(line)} cells: {line!r}")
        # Only when the row actually opened. Escape on the main menu leaves the
        # installer, so sending it after a press that opened nothing ends the
        # walk on its first row.
        if opened.opened_from(drawn):
            console.send_raw("\x1b")
            time.sleep(0.5)
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
    target = create_target(workdir / "target.qcow2")
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
