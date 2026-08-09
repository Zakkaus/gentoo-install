"""The menu under a real terminal, not the fake screen.

`FakeScreen` proves what the widgets decide; it proves nothing about curses.
`CursesScreen` had no test at all, so `raw()`, the colour pairs, the write that
always raises on the last cell and `getkey` were only ever exercised by a
person sitting in front of the installer.

The terminal here is a pty, which is what curses needs and what a serial
console gives it. Everything runs in a child process: `curses.initscr` changes
the state of the terminal it is handed, and pytest's own is not ours to change.
"""

from __future__ import annotations

import curses
import json
import os
import pty
import selectors
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]

#: Big enough for the menu. `too_small` refuses anything under this, and the
#: point here is to drive the menu rather than that refusal.
SIZE = (40, 110)

#: Long enough for any of these to finish, short enough that a screen waiting
#: for a key that was never sent is reported rather than hanging the suite.
DEADLINE = 45.0

#: Between keys. Long enough for the screen to redraw and ask for the next one,
#: short enough that a hundred of them still finish inside the deadline.
KEY_INTERVAL = 0.08


def drive(keys: str, source: str) -> dict[str, Any]:
    """Run `source` against a pty, feeding it `keys`, and return what it printed.

    The child writes one JSON line to a pipe: a pty carries the drawing, and
    reading a verdict back out of curses output is reading a picture.
    """
    read_end, write_end = os.pipe()
    child, terminal = pty.fork()
    if child == 0:  # pragma: no cover - the child never returns
        os.close(read_end)
        os.environ["TERM"] = "xterm"
        os.environ["LINES"], os.environ["COLUMNS"] = str(SIZE[0]), str(SIZE[1])
        sys.path.insert(0, str(REPOSITORY))
        answer: dict[str, Any] = {}
        try:
            exec(compile(source, "<driver>", "exec"), {"answer": answer, "keys": keys})
        except BaseException as error:  # noqa: BLE001 - reported, then the child dies
            answer = {"error": f"{type(error).__name__}: {error}"}
        finally:
            with os.fdopen(write_end, "w") as handle:
                handle.write(json.dumps(answer))
        os._exit(0)

    os.close(write_end)
    # One key at a time, and only once the child has drawn something. Writing
    # them all up front raced the terminal's own modes: ctrl-c arrived while
    # the line discipline still had ISIG and killed the child instead of
    # reaching the widgets, and an escape sequence arrived before ncurses had
    # turned keypad mode on. The drawing is drained as it comes, or the pty
    # buffer fills and the child blocks writing to it.
    selector = selectors.DefaultSelector()
    selector.register(terminal, selectors.EVENT_READ)
    pending = list(keys)
    drawn = False
    deadline = time.monotonic() + DEADLINE
    timed_out = False
    while True:
        if drawn and pending:
            os.write(terminal, pending.pop(0).encode())
            time.sleep(KEY_INTERVAL)
        if time.monotonic() > deadline:
            # A screen that wants a key nobody sent waits for ever. Killed and
            # reported, because an unattended suite that hangs tells nobody
            # which test it was.
            timed_out = True
            os.kill(child, signal.SIGKILL)
            break
        if not selector.select(timeout=0.2):
            if os.waitpid(child, os.WNOHANG)[0]:
                break
            continue
        try:
            if not os.read(terminal, 65536):
                break
            drawn = True
        except OSError:
            break
    os.waitpid(child, 0)
    with os.fdopen(read_end) as handle:
        printed = handle.read()
    selector.close()
    os.close(terminal)
    if timed_out:
        return {"error": f"the screen was still waiting for a key after {DEADLINE}s"}
    return dict(json.loads(printed)) if printed else {}


#: Walk to the bottom of the menu and leave. The exact rows do not matter: what
#: is under test is that a real terminal draws them and hands keys back.
WALK = r"""
import curses

from gentoo_install.tui.curses_screen import CursesScreen, too_small
from gentoo_install.tui.widgets import Item, Menu, Style


def main(window):
    screen = CursesScreen(window)
    answer["size"] = list(screen.size())
    answer["too_small"] = too_small(screen)
    items = [
        Item(label="first", value=1, detail="a", style=Style.REQUIRED),
        Item(label="second", value=2, detail="b", style=Style.UNTOUCHED),
        Item(label="third", value=3, disabled_because="not here"),
        Item(label="fourth", value=4),
    ]
    chosen = Menu(title="under a real terminal", items=items, footer="[q]").run(screen)
    answer["chosen"] = chosen.unwrap() if chosen.chosen else None
    answer["outcome"] = chosen.outcome.value


curses.wrapper(main)
"""


def test_the_menu_runs_under_a_real_terminal() -> None:
    """Down twice and enter: the disabled row is stepped over, so the answer is
    the fourth. Under curses, not under the double that stands in for it.

    `j` rather than the down arrow: an arrow is an escape sequence, and ncurses
    only translates one after it has put the terminal into keypad mode, which
    is after these bytes are already in the buffer. The menu takes both.
    """
    result = drive("jj\r", WALK)
    assert result.get("error") is None, result.get("error")
    assert result["size"] == list(SIZE)
    assert result["too_small"] == ""
    assert result["chosen"] == [4]


@pytest.mark.parametrize("key", ["q", "\x1b", "\x03"], ids=["q", "escape", "ctrl-c"])
def test_cancelling_reaches_the_widgets_as_an_answer(key: str) -> None:
    """`raw()` is what makes ctrl-c a byte rather than a signal, so the menu
    answers it the way it answers an escape instead of the run ending.

    A harmless `k` first: `raw()` runs in `CursesScreen.__init__`, and a ctrl-c
    that arrives between `initscr` and that call is still a signal. A person
    cannot press a key that early; this driver can.
    """
    result = drive(f"k{key}", WALK)
    assert result.get("error") is None, result.get("error")
    assert result["chosen"] is None
    assert result["outcome"] == "cancelled"


#: The whole menu, from the configuration `cli.py` starts it with, driven to the
#: point where it produces something the planner accepts.
INSTALL = r"""
import curses
from pathlib import Path

from gentoo_install.data import load_catalog
from gentoo_install.i18n import Catalog
from gentoo_install.model.parse import load
from gentoo_install.plan.build import build
from gentoo_install.model import mirrors
from gentoo_install.model import compat
from gentoo_install.tui import app, screens
from gentoo_install.tui.curses_screen import CursesScreen
from gentoo_install.tui.settings import SETTINGS
from dataclasses import replace

REPOSITORY = Path(r"{repository}")


def main(window):
    started = load(REPOSITORY / "tests/fixtures/vm-binpkg.toml")
    # A site as well as a region, which is what `cli._blank` fills in and a
    # fixture does not carry.
    started = replace(
        started,
        portage=replace(
            started.portage,
            mirrors=replace(
                started.portage.mirrors,
                site=mirrors.gentoo_sites(started.portage.mirrors.region)[0].key,
            ),
        ),
    )
    context = screens.Context(
        translate=Catalog("en"),
        disks=[("/dev/disk/by-id/virtio-target0", "40 GiB")],
        groups=load_catalog(),
        hash_password=lambda password: "$6$test$" + str(len(password)),
        timezones=("UTC",),
    )
    # What an operator does by opening each row and typing the disk name. The
    # per-screen behaviour is covered against the fake screen; what is under
    # test here is the whole loop on a real terminal.
    context.confirmed = {one.selector for one in compat.destroyed(started.disk.graph)}
    context.visited.update(one.key for one in SETTINGS)
    for one in SETTINGS:
        context.visited.update(row.key for row in (one.rows or ()))
    answer["blocked"] = app._blocked(started, context)
    finished = app.run(CursesScreen(window), started, context)
    answer["cancelled"] = finished.cancelled
    if finished.config is not None:
        answer["operations"] = len(build(finished.config, load_catalog()))


curses.wrapper(main)
"""


def test_the_whole_menu_produces_a_plan_under_curses() -> None:
    """Straight to Install and confirm. The configuration it starts from is a
    complete one, so nothing is unanswered and the row is live: this is the
    path an operator takes when the defaults already suit them."""
    source = INSTALL.replace("{repository}", str(REPOSITORY))
    # Past every setting to the Install row, enter to open the overview, enter
    # to accept it, then down and enter on the confirmation, whose first answer
    # is No. The menu stops at the last row rather than wrapping, so more
    # presses than there are rows is safe.
    result = drive("j" * 40 + "\r" + "\r" + "j\r", source)
    assert result.get("error") is None, result.get("error")
    assert result.get("blocked") == "", result
    assert result.get("cancelled") is False, result
    assert result["operations"] > 20


#: The account form, which is where `Field.secret` and `Field.toggle` are
#: drawn. `FakeScreen` proves what the widget decides; it proves nothing about
#: what curses puts on the terminal.
ACCOUNT = r"""
import curses

from gentoo_install.tui.widgets import Field, Form


def main(window):
    from gentoo_install.tui.curses_screen import CursesScreen

    screen = CursesScreen(window)
    fields = [
        Field(label="name", value=""),
        Field(label="password", secret=True),
        Field(label="sudo", toggle=True),
    ]
    answered = Form(title="account", fields=fields, footer="[q]", message="try again").run(screen)
    answer["values"] = answered.unwrap() if answered.chosen else None
    answer["outcome"] = answered.outcome.value


curses.wrapper(main)
"""


def test_the_account_form_runs_under_a_real_terminal() -> None:
    """A masked field and a tick, drawn by curses rather than by the double.

    The toggle takes a space, which every other widget treats as a character,
    so the two share a key and only a real run shows which one gets it.
    """
    # Enter moves to the next field and submits only from Done, so the last
    # key is what accepts the form.
    result = drive("zakk\nsecret\n \n\n", ACCOUNT)
    assert result.get("error") is None, result.get("error")
    assert result["values"] == ["zakk", "secret", "x"]


def test_a_form_message_does_not_push_the_done_row_off_the_screen() -> None:
    """The message is drawn above the fields, so everything below it moves down
    by one. A form that put Done past the last line could not be submitted."""
    result = drive("\n\n\n\n", ACCOUNT)
    assert result.get("error") is None, result.get("error")
    assert result["values"] == ["", "", ""]
