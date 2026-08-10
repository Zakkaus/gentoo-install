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

from .console import SerialConsole
from .driver import build as build_driver
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

_ANSI: Final[re.Pattern[bytes]] = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-B]|\x1b[=>]")


def rendered(said: bytes) -> list[str]:
    """The lines a screen left, with the escape codes taken out."""
    text = _ANSI.sub(b"", said).decode("utf-8", "replace")
    return [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]


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
    # The shell echoes the launch line and it is longer than the console is
    # wide, so it is read and discarded before the first screen is measured.
    time.sleep(8.0)
    console.snapshot(REDRAW_SECONDS)
    console.send_raw("\x1b[B")
    time.sleep(0.5)
    console.send_raw("\x1b[A")
    time.sleep(0.5)
    for step in range(_ROWS):
        drawn = rendered(console.snapshot(REDRAW_SECONDS))
        body = [line for line in drawn if line.strip()]
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
        opened = rendered(console.snapshot(REDRAW_SECONDS))
        for line in opened:
            if cells(line) > COLUMNS:
                seen.note(f"row {step} opened", f"{cells(line)} cells: {line!r}")
        console.send_raw("\x1b")
        time.sleep(0.5)
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
    spec = VmSpec(
        medium=medium,
        workdir=workdir,
        firmware=Firmware.UEFI,
        memory="2G",
        cpus=2,
        driver_iso=driver_iso,
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
