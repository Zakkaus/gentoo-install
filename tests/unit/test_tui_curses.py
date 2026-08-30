# SPDX-License-Identifier: GPL-2.0-or-later
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
import fcntl
import json
import os
import pty
import selectors
import signal
import struct
import sys
import termios
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

#: How long the child may say nothing at all before it counts as stuck. The
#: bound is on silence, not on elapsed time: a machine running guests and a
#: second pytest makes the child slower, not quieter, and a wall-clock budget
#: turns that load into a red gate nobody can reproduce alone.
RESIZE_STALL = 15.0
#: The backstop for a child that keeps drawing for ever, which silence cannot
#: catch.
RESIZE_CAP = 60.0
RESIZE_INTERVAL = 0.3
RESIZE_START_INTERVAL = 1.0
#: How long the child must say nothing before its redraw counts as finished.
#: The first byte is not the last: `curses` answers a `SIGWINCH` with a whole
#: screen, and a key sent after the first chunk of it landed mid-redraw. This
#: test failed once in a full suite and passed alone and on the next run,
#: which is worse than failing.
RESIZE_QUIET = 0.35

ResizeAction = str | tuple[int, int]


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
        # Everything, including SystemExit and KeyboardInterrupt: this is a
        # forked child whose only way to report is the pipe below, and an
        # exception escaping here would leave the parent reading an empty one.
        except BaseException as error:
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


def drive_resizes(
    actions: list[ResizeAction], source: str, stale: tuple[str, str] | None = None
) -> tuple[dict[str, Any], bytes]:
    """Drive keys and kernel window-size changes through a real pty.

    Both descriptors are nonblocking, and a child that misses the deadline is
    killed before the test returns.
    """
    read_end, write_end = os.pipe()
    child, terminal = pty.fork()
    if child == 0:  # pragma: no cover - the child never returns
        os.close(read_end)
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", *SIZE, 0, 0))
        os.set_inheritable(write_end, True)
        environment = dict(os.environ)
        environment["TERM"] = "xterm"
        # Set rather than cleared for one test, which needs them stale to see
        # the defect; the rest are handed a clean environment.
        if stale is None:
            environment.pop("LINES", None)
            environment.pop("COLUMNS", None)
        else:
            environment["LINES"], environment["COLUMNS"] = stale
        driver = f"""
import json
import os
import sys

sys.path.insert(0, {str(REPOSITORY)!r})
answer = {{}}
try:
    exec(compile({source!r}, "<resize-driver>", "exec"), {{"answer": answer}})
except BaseException as error:
    answer = {{"error": f"{{type(error).__name__}}: {{error}}"}}
with os.fdopen({write_end}, "w") as handle:
    handle.write(json.dumps(answer))
"""
        os.execve(sys.executable, [sys.executable, "-c", driver], environment)

    os.close(write_end)
    os.set_blocking(read_end, False)
    selector = selectors.DefaultSelector()
    selector.register(terminal, selectors.EVENT_READ)
    selector.register(read_end, selectors.EVENT_READ)
    pending = list(actions)
    drawing = bytearray()
    printed = bytearray()
    cap = time.monotonic() + RESIZE_CAP
    last_progress = time.monotonic()
    next_action = cap
    waiting_for_resize_draw = False
    started = False
    timed_out = False
    reaped = False
    try:
        while True:
            for ready, _ in selector.select(timeout=0.05):
                try:
                    chunk = os.read(ready.fd, 65536)
                except (BlockingIOError, OSError):
                    chunk = b""
                if chunk:
                    last_progress = time.monotonic()
                if ready.fd == terminal:
                    drawing.extend(chunk)
                    if waiting_for_resize_draw and chunk:
                        # Quiet, not the first byte: the redraw is finished
                        # when the child stops writing, and every chunk pushes
                        # the moment out again.
                        next_action = time.monotonic() + RESIZE_QUIET
                    elif drawing and next_action == cap:
                        delay = RESIZE_INTERVAL if started else RESIZE_START_INTERVAL
                        next_action = time.monotonic() + delay
                else:
                    printed.extend(chunk)
            now = time.monotonic()
            if pending and now >= next_action:
                waiting_for_resize_draw = False
                action = pending.pop(0)
                started = True
                if isinstance(action, str):
                    os.write(terminal, action.encode())
                else:
                    fcntl.ioctl(
                        terminal,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", *action, 0, 0),
                    )
                    # Explicit signaling keeps the test independent of whether
                    # this ioctl implementation signals the pty process group.
                    os.kill(child, signal.SIGWINCH)
                    waiting_for_resize_draw = True
                last_progress = now
                next_action = cap if waiting_for_resize_draw else now + RESIZE_INTERVAL
            waited, _ = os.waitpid(child, os.WNOHANG)
            if waited:
                reaped = True
                break
            if now - last_progress >= RESIZE_STALL or now >= cap:
                timed_out = True
                os.kill(child, signal.SIGKILL)
                break
        if not reaped:
            os.waitpid(child, 0)
        while True:
            try:
                chunk = os.read(read_end, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            printed.extend(chunk)
    finally:
        selector.close()
        os.close(read_end)
        os.close(terminal)
    if timed_out:
        return (
            {
                "error": (
                    f"the screen drew nothing for {RESIZE_STALL}s; "
                    f"{len(pending)} actions remain after {len(drawing)} output bytes; "
                    f"small screen drawn: {b'interface needs' in drawing}"
                )
            },
            bytes(drawing),
        )
    result = dict(json.loads(printed.decode())) if printed else {}
    return result, bytes(drawing)


def test_a_too_small_screen_uses_the_catalog() -> None:
    """The constrained terminal message follows the interface language."""
    from gentoo_install.i18n import Catalog
    from gentoo_install.tui.curses_screen import too_small
    from gentoo_install.tui.widgets import MINIMUM_COLUMNS, MINIMUM_LINES

    class Sized:
        def size(self) -> tuple[int, int]:
            return 4, 16

    translate = Catalog("zh-TW")
    assert too_small(Sized(), translate) == translate(
        "The terminal is {columns}x{lines} and the interface needs "
        "{minimum_columns}x{minimum_lines}"
    ).format(
        columns=16,
        lines=4,
        minimum_columns=MINIMUM_COLUMNS,
        minimum_lines=MINIMUM_LINES,
    )

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
    assert result["chosen"] == 4


@pytest.mark.parametrize(
    ("key", "outcome"),
    [("q", "cancelled"), ("\x1b", "back"), ("\x03", "cancelled")],
    ids=["q", "escape", "ctrl-c"],
)
def test_a_key_that_leaves_reaches_the_widgets_as_an_answer(key: str, outcome: str) -> None:
    """`raw()` is what makes ctrl-c a byte rather than a signal, so the menu
    answers it the way it answers an escape instead of the run ending.

    A harmless `k` first: `raw()` runs in `CursesScreen.__init__`, and a ctrl-c
    that arrives between `initscr` and that call is still a signal. A person
    cannot press a key that early; this driver can.
    """
    result = drive(f"k{key}", WALK)
    assert result.get("error") is None, result.get("error")
    assert result["chosen"] is None
    # Escape answers Back below the main menu and the other two end the run,
    # which is the key table in `docs/design.md` reaching a real terminal.
    assert result["outcome"] == outcome


#: The whole menu, from the configuration `cli.py` starts it with, driven to the
#: point where it produces something the planner accepts.
INSTALL = r"""
import curses
from pathlib import Path

from gentoo_install.data import load_catalog
from gentoo_install.i18n import Catalog
from gentoo_install.exec.config import load
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
    context = app.MainMenuContext(screens.Context(
        translate=Catalog("en"),
        disks=[("/dev/disk/by-id/virtio-target0", "40 GiB")],
        groups=load_catalog(),
        hash_password=lambda password: "$6$test$" + str(len(password)).ljust(86, "a"),
        timezones=("UTC",),
    ))
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
    answered = Form(title="account", fields=fields, done="Done", footer="[q]", message="try again").run(screen)
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


RESIZE_FIELD = r"""
import curses

from gentoo_install.tui.curses_screen import CursesScreen
from gentoo_install.tui.widgets import TextField


def main(window: object) -> None:
    field = TextField(title="hostname", footer="[Esc] Leave")
    answered = field.run(CursesScreen(window))
    answer["outcome"] = answered.outcome.value
    answer["value"] = answered.unwrap() if answered.chosen else None


curses.wrapper(main)
"""


def test_a_text_field_survives_a_terminal_that_shrinks_and_grows() -> None:
    """A five-column redraw used to loop while trimming an empty value.

    The value typed before the shrink remains when the terminal grows again.
    """
    actions: list[ResizeAction] = [*"abc", (10, 5), SIZE, "d", "\n"]
    result, _ = drive_resizes(actions, RESIZE_FIELD)
    assert result.get("error") is None, result.get("error")
    assert result == {"outcome": "chose", "value": "abcd"}


RESIZE_FORM = r"""
import curses

from gentoo_install.tui.curses_screen import CursesScreen
from gentoo_install.tui.widgets import Field, Form


def main(window: object) -> None:
    form = Form(title="network", fields=[Field(label="address")], done="Done", footer="[Esc] Leave")
    translated = "Escape after translation"
    answered = form.run(CursesScreen(window, lambda source: translated))
    answer["outcome"] = answered.outcome.value


curses.wrapper(main)
"""


def test_a_form_that_no_longer_fits_says_how_to_leave() -> None:
    # Five lines is under the floor the interface refuses at, and sixty
    # columns keeps the message on one row: a wrapped one matches no
    # substring. 60x10 is over the floor now and is drawn rather than refused.
    result, drawing = drive_resizes([(5, 60), "\x1b"], RESIZE_FORM)
    assert result.get("error") is None, result.get("error")
    # Back, not cancelled: a terminal that shrank below the floor still lets
    # escape leave the screen, and leaving is what the message offers.
    assert result["outcome"] == "back"
    assert drawing.count(b"Escape after translation") >= 2


#: What a terminal does with a wide glyph, asked of ncurses rather than of the
#: width table: the two must agree or the text after it lands in the wrong cell.
WIDE = r"""
import curses
import locale

from gentoo_install.i18n import width

answer["codeset"] = locale.nl_langinfo(locale.CODESET)


def walk(window):
    window.addstr(0, 0, "\u7e7c\u7e8c")
    window.addstr(0, width("\u7e7c\u7e8c"), "|end")
    window.refresh()
    answer["row"] = window.instr(0, 0, 12).decode("utf-8", "replace").rstrip()


curses.wrapper(walk)
"""


def test_a_terminal_that_cannot_draw_a_wide_glyph_is_recognised() -> None:
    """ncurses reads `LC_CTYPE`. Where the codeset is not UTF-8 it writes a
    wide character as its bytes and advances one cell for each, so the widths
    this code computes stay right and the text after them lands elsewhere: the
    installer's footer is three segments joined by two spaces, and the second
    landed on top of the first. The menu offers English there rather than a
    screen made of wreckage."""
    from gentoo_install.cli import _draws_wide_characters

    result = drive("", WIDE)
    assert result.get("error") is None, result.get("error")
    utf8 = result["codeset"].upper().replace("-", "") == "UTF8"
    assert _draws_wide_characters() == utf8
    if utf8:
        assert result["row"] == "\u7e7c\u7e8c|end", result["row"]
    else:
        # Two cells of nothing where the glyph belongs, which is the state the
        # check exists to refuse.
        assert "\u7e7c" not in result["row"], result["row"]


#: What `cli.py` does before `initscr`, asked of ncurses itself.
STALE_SIZE = r"""
import curses
import os
import sys

def walk(window):
    answer["before"] = window.getmaxyx()


answer["environment"] = [os.environ.get("LINES"), os.environ.get("COLUMNS")]
for named in ("LINES", "COLUMNS"):
    os.environ.pop(named, None)
curses.wrapper(walk)
"""


def test_a_stale_size_in_the_environment_does_not_shrink_the_interface() -> None:
    """ncurses prefers `LINES` and `COLUMNS` over the terminal's own size, so a
    stale pair drew the whole interface into a corner and left the rest of the
    screen holding what was there before. Every other test pops them, which is
    why nothing here could see it.
    """
    result, _ = drive_resizes([], STALE_SIZE, stale=("24", "40"))
    assert result.get("error") is None, result.get("error")
    # The child really did start with them set, or the case is not exercised.
    assert result["environment"] == ["24", "40"]
    assert tuple(result["before"]) == SIZE, (result["before"], SIZE)


def test_the_entry_point_clears_the_size_the_environment_claims() -> None:
    """The call and its effect: reading the terminal correctly in a test that
    pops them itself proves nothing about `cli.py`."""
    import ast
    import inspect

    from gentoo_install import cli

    tree = ast.parse(inspect.getsource(cli))
    popped = {
        str(node.args[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "pop"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    for named in ("LINES", "COLUMNS"):
        assert named in popped, sorted(popped)


HELP_SOURCE = '''
import curses
from gentoo_install.tui.curses_screen import CursesScreen


def main(window):
    screen = CursesScreen(window)
    screen.clear()
    screen.write(0, 0, "MARKER-LEFT")
    screen.write(3, 60, "MARKER-RIGHT")
    screen.show()
    screen.help()
    answer["left"] = window.instr(0, 0, 11).decode()
    answer["right"] = window.instr(3, 60, 12).decode()


curses.wrapper(main)
'''


def test_the_key_page_leaves_the_interface_where_it_was() -> None:
    """Drawn over the interface, the page has to be cleared by whoever redraws.

    The widget that opened it redraws only its own pane, so the operator was
    left looking at the key table with one pane of the interface on top of it.
    Read off a guest: an agent pressed `?` inside an opened row and the screen
    held both.
    """
    answered = drive("x", HELP_SOURCE)
    assert answered.get("error") is None, answered
    assert answered["left"] == "MARKER-LEFT", answered
    assert answered["right"] == "MARKER-RIGHT", answered

def test_every_key_the_session_sends_is_the_one_terminfo_names() -> None:
    """A sequence ncurses cannot parse arrives as a bare escape.

    `CSI H` and `CSI F` are what a terminal sends with the keypad off, and
    `curses.wrapper` turns it on: sent anyway, End reached the widgets as
    escape and the interface offered to leave the run instead of moving the
    cursor. Checked against terminfo rather than a pty, because the pty
    helper writes one byte at a time and ncurses times out inside a sequence.
    """
    from tests.tui.session import KEYS

    curses.setupterm(term="xterm", fd=sys.stdout.fileno())
    for name, capability in (
        ("up", "kcuu1"),
        ("down", "kcud1"),
        ("left", "kcub1"),
        ("right", "kcuf1"),
        ("home", "khome"),
        ("end", "kend"),
        ("pageup", "kpp"),
        ("pagedown", "knp"),
        ("backspace", "kbs"),
    ):
        wanted = curses.tigetstr(capability)
        assert wanted is not None, capability
        assert KEYS[name] == wanted.decode(), (name, KEYS[name], wanted)


    # Negative control: the form this replaced is not what terminfo names, so
    # the check above is comparing against the terminal and not against
    # whatever the table happens to hold.
    assert curses.tigetstr("khome") != b"\x1b[H"
    assert curses.tigetstr("kcud1") != b"\x1b[B"


RESIZE_PANE = r"""
import curses

from gentoo_install.tui.curses_screen import CursesScreen
from gentoo_install.tui.widgets import PaneRow, TwoPane


def main(window: object) -> None:
    screen = CursesScreen(window)
    rows = [
        PaneRow(label=f"row {at}", value=at, state=f"value {at}", detail=("what it is",))
        for at in range(6)
    ]
    chosen = TwoPane(title="gentoo-install", rows=rows).run(screen)
    lines, columns = screen.size()
    answer["outcome"] = chosen.outcome.value
    answer["size"] = [lines, columns]
    answer["drawn"] = window.instr(0, 0, 30).decode().rstrip()


curses.wrapper(main)
"""


def test_the_two_pane_list_redraws_at_the_size_the_terminal_became() -> None:
    """A resize must leave the frame drawn for the new size, not the old one.

    The whole interface is this widget, so a resize that leaves it drawn for
    the previous dimensions is the interface breaking rather than one screen.
    """
    grown = (46, 130)
    actions: list[ResizeAction] = [(30, 100), grown, "\n"]
    result, _ = drive_resizes(actions, RESIZE_PANE)
    assert result.get("error") is None, result.get("error")
    assert result["size"] == list(grown), result
    assert result["drawn"].strip().startswith("gentoo-install"), result
    assert result["outcome"] == "chose", result


RESIZE_REGION = r"""
import curses

from gentoo_install.tui.curses_screen import CursesScreen
from gentoo_install.tui.widgets import Item, Menu, PaneRow, TwoPane


def main(window: object) -> None:
    screen = CursesScreen(window)
    rows = [PaneRow(label=f"row {at}", value=at, state="value") for at in range(6)]
    pane = TwoPane(title="gentoo-install", rows=rows)
    inside = pane.frame(screen, 0, dimmed=True)
    chosen = Menu(
        title="Portage",
        items=[Item(label=f"choice {at}", value=at) for at in range(4)],
    ).run(inside if inside is not None else screen)
    lines, columns = screen.size()
    answer["outcome"] = chosen.outcome.value
    answer["size"] = [lines, columns]
    answer["region"] = list(inside.size()) if inside is not None else None
    answer["left"] = window.instr(3, 0, 12).decode().rstrip()
    answer["title"] = window.instr(0, 0, 20).decode().strip()


curses.wrapper(main)
"""


def test_a_row_opened_before_a_resize_draws_inside_the_new_frame() -> None:
    """The rectangle an editor draws into is measured when the row is opened.

    Resize while it is open and the editor keeps writing into the rectangle
    the old size gave it: the frame around it moved and the screen holds two
    layouts. The whole interface is one opened row most of the time.
    """
    grown = (46, 130)
    actions: list[ResizeAction] = [(30, 100), grown, "\n"]
    result, _ = drive_resizes(actions, RESIZE_REGION)
    assert result.get("error") is None, result.get("error")
    assert result["size"] == list(grown), result
    lines, columns = result["region"]
    assert columns < grown[1], "the region is not narrower than the screen"
    assert lines <= grown[0], result
    # The region has to follow the screen it is cut from, or the editor draws
    # into a rectangle the frame no longer occupies.
    assert lines >= grown[0] - 5, result
    # And the frame itself has to be back on the screen: the editor redraws
    # itself on a resize and nothing else did, so the list beside it and the
    # box around it were gone.
    assert result["left"].strip().startswith("row"), result
    assert result["title"].startswith("gentoo-install"), result


def test_an_accepted_resize_does_not_wipe_the_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Erasing here is what made the frame disappear intermittently.

    `Region` redraws the frame when the screen's size differs from the one it
    last drew at, and any `size()` call updates that. A resize that erased the
    window and then found the size already recorded left the frame wiped and
    nothing to put it back: `test_a_row_opened_before_a_resize_draws_inside_
    the_new_frame` failed twice in fifteen runs, and once in twenty with the
    driver's quiet window raised to three seconds, so it was not the harness
    waiting too little. The caller redraws on `KEY_RESIZE` and `TwoPane.frame`
    clears the screen itself, so the erase was never what removed stale
    content.
    """
    import curses as curses_module

    from gentoo_install.tui.curses_screen import CursesScreen

    # Only the module-level calls the constructor makes outside a terminal.
    # The window itself is the fake below, so what is under test -- whether
    # `key` erases -- is answered by the real code.
    monkeypatch.setattr(curses_module, "update_lines_cols", lambda: None)
    monkeypatch.setattr(curses_module, "raw", lambda: None)
    monkeypatch.setattr(curses_module, "has_colors", lambda: False)
    monkeypatch.setattr(curses_module, "keyname", lambda key: b"KEY_RESIZE")

    class Window:
        def __init__(self, size: tuple[int, int], keys: list[int | str]) -> None:
            self._size = size
            self._keys = keys
            self.erased = 0

        def get_wch(self) -> int | str:
            return self._keys.pop(0)

        def getmaxyx(self) -> tuple[int, int]:
            return self._size

        def erase(self) -> None:
            self.erased += 1

        def addstr(self, *args: Any, **rest: Any) -> None:
            return None

        def refresh(self) -> None:
            return None

        def clrtoeol(self) -> None:
            return None

        def nodelay(self, flag: bool) -> None:
            return None

    resize = curses_module.KEY_RESIZE
    roomy = Window((40, 120), [resize])
    assert CursesScreen(roomy).key() == "KEY_RESIZE"
    assert roomy.erased == 0, "an accepted resize wiped the frame off the screen"

    # A resize that leaves the terminal unusable still erases: the message it
    # puts up needs the screen to itself, and no frame survives that size.
    cramped = Window((4, 20), [resize, "\x1b"])
    assert CursesScreen(cramped).key() == "\x1b"
    assert cramped.erased >= 1, "the too-small screen was drawn over the old layout"
