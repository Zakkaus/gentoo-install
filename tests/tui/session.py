# SPDX-License-Identifier: GPL-2.0-or-later
"""A guest an operator can drive one keystroke at a time.

The install fixtures hand the installer a configuration file, so the whole
interface is skipped: a menu nobody can read still passes every one of them.
This is the other half. A session boots a guest, leaves the installer at its
first screen, and answers three questions from the command line: what is on
the screen, what happens when this key is pressed, and what the run produced.

The console is a websocket to the node, which no second process can inherit,
so `start` leaves a daemon holding it and the other subcommands speak to that
daemon over a unix socket beside the session's own directory.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from tests.tui.screen import Screen


@runtime_checkable
class Held(Protocol):
    """What `serve` needs of the console it holds. A protocol rather than
    `SerialConsole`, so a test can hand it one that dies."""

    def read_available(self, seconds: float) -> bytes: ...

    def send_raw(self, keys: str) -> None: ...

#: How long the console must stay quiet before the page counts as drawn, and
#: how long to keep waiting for that. A redraw at 120x40 arrives in a few
#: chunks a few milliseconds apart; a running install writes continuously and
#: must not hold the answer for ever.
SETTLE_QUIET: Final[float] = 0.25
SETTLE_LIMIT: Final[float] = 5.0

#: Where a session keeps its socket, its log and what it was asked to build.
#: Beside the cluster's own work directory, in the workspace rather than the
#: repository: a session leaves a driver CD and a screen transcript, and a
#: build artifact inside the checkout is one `git status` away from a commit.
SESSIONS: Final[Path] = Path.home() / "code/gentoo-install/lab/tui"

#: What the operator types, as the terminal sends it. Named because an agent
#: writing `\x1b[B` by hand gets it wrong once per session.
#: What each name sends, taken from terminfo rather than written out. ncurses
#: parses against the terminal's own capabilities and `curses.wrapper` turns
#: the keypad on, so the `CSI` forms are not what it is waiting for: every
#: arrow arrived as a bare escape and the widgets read it as Back. The agent
#: driving the first session worked that out and navigated on tab alone.
KEYS: Final[dict[str, str]] = {
    "up": "\x1bOA",
    "down": "\x1bOB",
    "left": "\x1bOD",
    "right": "\x1bOC",
    "enter": "\n",
    "esc": "\x1b",
    "escape": "\x1b",
    "tab": "\t",
    "space": " ",
    "backspace": "\x7f",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "home": "\x1bOH",
    "end": "\x1bOF",
    "clear": "\x15",
    "help": "?",
    "filter": "/",
}

#: The terminal the guest is given. Wide enough for two panes, tall enough
#: that the whole list fits without the agent having to scroll to see it.
from tests.vm.cluster import TUI_COLUMNS as SCREEN_COLUMNS
from tests.vm.cluster import TUI_LINES as SCREEN_LINES

#: How long the screen is allowed to keep changing before it is read. A menu
#: redraws in one frame; a screen that fetches answers after a keystroke may
#: take several, and reading too early shows the operator the previous page.
SETTLE: Final[float] = 0.6


class SessionError(Exception):
    """Anything the operator can act on: a session that is not there, a key
    with no name, a guest that stopped answering."""


@dataclass(frozen=True)
class Session:
    """Where one session keeps its things."""

    name: str

    @property
    def directory(self) -> Path:
        return SESSIONS / self.name

    @property
    def control(self) -> Path:
        return self.directory / "control"

    @property
    def screens(self) -> Path:
        """Every screen answered, separated by a form feed, in order."""
        return self.directory / "screens.txt"

    @property
    def ended(self) -> Path:
        """Why the daemon stopped holding the console.

        Two readers died mid-install and the guests kept going; with nothing
        written, a stale `screens.txt` reads as a run still in progress."""
        return self.directory / "ended.txt"

    @property
    def hangups(self) -> Path:
        """The last request that ended without an answer. A client going away
        is not a failure of the session, and used to end it."""
        return self.directory / "hangups.txt"

    @property
    def transcript(self) -> Path:
        """Every key sent, one per line, so the counts a report is judged on
        are read off the session rather than taken from the agent."""
        return self.directory / "keys.txt"


def keys_from(words: list[str]) -> str:
    """`down down enter` or a literal string, never a raw escape.

    An agent that has to spell `\\x1b[B` spells it wrong, and a run lost to
    that is a run that says nothing about the interface.
    """
    typed: list[str] = []
    for word in words:
        if word in KEYS:
            typed.append(KEYS[word])
        elif word.startswith("type:"):
            typed.append(word[len("type:") :])
        else:
            raise SessionError(
                f"{word} is not a key; use one of {', '.join(sorted(KEYS))} "
                "or type:<text>"
            )
    return "".join(typed)


def ask(
    session: Session, message: dict[str, object], patience: float = 120.0
) -> dict[str, str]:
    """One request to the daemon holding the console."""
    if not session.control.exists():
        raise SessionError(f"no session named {session.name}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(patience)
        channel.connect(str(session.control))
        channel.sendall(json.dumps(message).encode() + b"\n")
        answer = bytearray()
        while not answer.endswith(b"\n"):
            chunk = channel.recv(65536)
            if not chunk:
                break
            answer.extend(chunk)
    if not answer:
        raise SessionError(f"the session {session.name} stopped answering")
    answered: dict[str, str] = json.loads(answer.decode())
    return answered




def start(name: str, spec: int, node: str = "", vmid: int = 0) -> str:
    """Build a guest and leave the installer at its first screen.

    The same path an install fixture takes as far as the driver CD, and then
    the other half: `install.sh` with no `--config`, which is the interface
    this exists to exercise.
    """
    from tests.vm import cluster
    from tests.vm.proxmox import Api

    session = Session(name)
    session.directory.mkdir(parents=True, exist_ok=True)
    session.transcript.write_text("", encoding="utf-8")
    session.screens.write_text("", encoding="utf-8")
    (session.directory / "spec.txt").write_text(str(spec), encoding="utf-8")
    api = Api()
    held = cluster.tui_execution(api, node, name, spec, session.directory, vmid)
    if os.fork() != 0:
        return name
    # The child holds the console; the parent's caller gets the name back and
    # every other subcommand reaches this process through the socket.
    os.setsid()
    console = held.console
    assert isinstance(console, Held), f"{type(console).__name__} cannot be held"
    serve(session, console, held.guest)
    os._exit(0)


def _with_cursor(grid: "Screen") -> str:
    """The grid, and under it the row the cursor is on.

    The interface marks the cursor by inverting that row and nothing else, so
    the grid alone does not carry it: every row reads the same and enter is a
    guess. Named rather than drawn into the grid, because a marker in the
    cells would be a character the operator's own screen does not show.
    """
    rows = grid.highlighted()
    if not rows:
        return grid.text()
    return grid.text() + "\n\ncursor:\n" + "\n".join(rows)


def _settled(grid: "Screen", console: object) -> str:
    """The screen once the guest has stopped drawing on it.

    Read while `ncurses` is repainting, the grid holds a page half of one
    layout and half of the next: a real read at 120x40 showed three rows
    twice and no section headings at all, which is a screen the interface
    never drew and an operator would call broken.
    """
    read = getattr(console, "read_available")
    deadline = time.monotonic() + SETTLE_LIMIT
    while time.monotonic() < deadline:
        arrived = read(SETTLE_QUIET)
        if not arrived:
            break
        grid.feed(arrived)
    return _with_cursor(grid)


def serve(session: Session, console: Held, guest: object) -> None:
    """Hold the console and answer the other subcommands.

    A websocket cannot be handed to a second process, so the process that
    opened it stays and everything else talks to it here.
    """
    grid = Screen(lines=SCREEN_LINES, columns=SCREEN_COLUMNS)
    session.control.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(session.control))
    listener.listen(4)
    listener.settimeout(1.0)
    try:
        _answer_until_stopped(session, console, guest, grid, listener)
    except BaseException as error:
        session.ended.write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
        raise


def _answer_until_stopped(
    session: Session,
    console: Held,
    guest: object,
    grid: Screen,
    listener: socket.socket,
) -> None:
    """The loop `serve` wraps, so one `except` covers every way it can end."""
    while True:
        # Drain first, always: the guest writes whether or not anyone asked,
        # and a screen read without draining is the previous page.
        grid.feed(console.read_available(0.2))
        try:
            channel, _ = listener.accept()
        except TimeoutError:
            continue
        with channel:
            # A client that goes away takes its own request with it and
            # nothing else: two agents lost their guests to one
            # `BrokenPipeError` raised writing an answer nobody was left to
            # read, both while the 38-row font screen was settling.
            try:
                asked = channel.makefile("rb").readline()
                if not asked:
                    continue
                message = json.loads(asked.decode())
                answer: dict[str, str] = {}
                if message["do"] == "screen":
                    shown = _settled(grid, console)
                    # Kept as it is answered, because the counts are computed
                    # from the screens the operator was actually shown: a
                    # report that reads a file nobody writes answers zero for
                    # every one of them.
                    with session.screens.open("a", encoding="utf-8") as log:
                        log.write(shown + "\f")
                    answer = {"screen": shown}
                elif message["do"] == "key":
                    console.send_raw(str(message["text"]))
                    answer = {"sent": "yes"}
                elif message["do"] == "stop":
                    answer = {"stopped": "yes"}
                channel.sendall(json.dumps(answer).encode() + b"\n")
            except (OSError, ValueError) as error:
                # `OSError` covers the hang-ups; `ValueError` covers a request
                # that arrived truncated and will not parse.
                session.hangups.write_text(
                    f"{type(error).__name__}: {error}\n", encoding="utf-8"
                )
                continue
            if message["do"] == "stop":
                break
    listener.close()
    session.control.unlink(missing_ok=True)
    stop = getattr(guest, "destroy", None)
    if stop is not None:
        stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, helped in (
        ("screen", "print what the terminal is showing"),
        ("stop", "delete the guest"),
    ):
        one = commands.add_parser(name, help=helped)
        one.add_argument("session")
    opened = commands.add_parser("start", help="build a guest and open the menu")
    opened.add_argument("session")
    opened.add_argument("--spec", type=int, required=True)
    opened.add_argument("--node", default="")
    opened.add_argument("--vmid", type=int, default=0)
    typed = commands.add_parser("key", help="press keys, in order")
    typed.add_argument("session")
    typed.add_argument("keys", nargs="+")
    commands.add_parser("list", help="every session on this machine")
    arguments = parser.parse_args(argv)

    if arguments.command == "list":
        for found in sorted(SESSIONS.glob("*/control")):
            print(found.parent.name)
        return 0

    session = Session(arguments.session)
    if arguments.command == "start":
        print(start(arguments.session, arguments.spec, arguments.node, arguments.vmid))
        return 0
    if arguments.command == "key":
        sent = keys_from(arguments.keys)
        with session.transcript.open("a", encoding="utf-8") as log:
            log.write(" ".join(arguments.keys) + "\n")
        ask(session, {"do": "key", "text": sent})
        time.sleep(SETTLE)
        print(ask(session, {"do": "screen"})["screen"])
        return 0
    if arguments.command == "screen":
        print(ask(session, {"do": "screen"})["screen"])
        return 0
    if arguments.command == "stop":
        ask(session, {"do": "stop"})
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
