# SPDX-License-Identifier: GPL-2.0-or-later
"""The daemon that holds one session's console."""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from tests.tui import session as tui_session
from tests.tui.screen import Screen


class DeadConsole:
    """A console that stops answering, the way a dropped websocket does."""

    def read_available(self, seconds: float) -> bytes:
        raise OSError("the guest closed the serial connection")

    def send_raw(self, keys: str) -> None:
        raise AssertionError("nothing is sent to a console that died")


def test_a_reader_that_dies_says_so_beside_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two readers died mid-install on 2026-08-22 and both guests carried on
    installing. Nothing was written, so a `screens.txt` frozen at 03:14 read
    as a run still in progress; only the cluster's byte counters said
    otherwise."""
    monkeypatch.setattr(tui_session, "SESSIONS", tmp_path)
    session = tui_session.Session(name="dead")
    session.directory.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(session.control))
    listener.listen(1)
    listener.settimeout(0.05)
    try:
        with pytest.raises(OSError):
            tui_session.serve(session, DeadConsole(), None)
    finally:
        listener.close()
    assert session.ended.read_text(encoding="utf-8") == (
        "OSError: the guest closed the serial connection\n"
    )


class OneScreen:
    """A console with something to show and nothing after it."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.reads = 0

    def read_available(self, seconds: float) -> bytes:
        self.reads += 1
        return b"hello" if self.reads == 1 else b""

    def send_raw(self, keys: str) -> None:
        self.sent.append(keys)


def test_a_client_that_hangs_up_does_not_end_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two agents lost their guests to one `BrokenPipeError` raised writing an
    answer nobody was left to read, both while the 38-row font screen was
    settling. The request ends; the session goes on."""
    monkeypatch.setattr(tui_session, "SESSIONS", tmp_path)
    session = tui_session.Session(name="hangup")
    session.directory.mkdir(parents=True)
    session.screens.write_text("", encoding="utf-8")
    console = OneScreen()

    def ask(message: dict[str, str], *, read_the_answer: bool) -> bytes:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # A dead daemon leaves a socket that still accepts and never answers,
        # so without this the control for this test hangs instead of failing.
        client.settimeout(15.0)
        client.connect(str(session.control))
        client.sendall(json.dumps(message).encode() + b"\n")
        if not read_the_answer:
            # What an interrupted `session screen` leaves behind.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
            return b""
        got = client.makefile("rb").readline()
        client.close()
        return got

    gone = threading.Thread(target=ask, args=({"do": "screen"},), kwargs={"read_the_answer": False})
    answers: list[bytes] = []
    served = threading.Thread(
        target=tui_session.serve, args=(session, console, None), daemon=True
    )
    served.start()
    for _ in range(200):
        if session.control.exists():
            break
        time.sleep(0.02)
    gone.start()
    gone.join()
    answers.append(ask({"do": "key", "text": "x"}, read_the_answer=True))
    ask({"do": "stop"}, read_the_answer=True)
    served.join(timeout=10)

    assert not served.is_alive(), "the session outlived its own stop"
    assert json.loads(answers[0].decode()) == {"sent": "yes"}
    assert console.sent == ["x"], console.sent
    assert not session.ended.exists(), session.ended.read_text(encoding="utf-8")


def test_convert_drives_the_installer_inside_an_installed_guest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interface answers that a live medium has no system to replace, so
    the conversion spec is reached through a machine this harness already
    installed rather than through a guest of its own."""
    from tests.vm import cluster

    asked: list[tuple[str, object]] = []
    monkeypatch.setattr(tui_session, "SESSIONS", tmp_path)
    monkeypatch.setattr(
        cluster,
        "tui_conversion",
        lambda api, node, name, vmid, workdir, spec=3: asked.append(("convert", vmid)),
    )
    monkeypatch.setattr(
        cluster,
        "tui_execution",
        lambda api, node, name, spec, workdir, vmid=0: asked.append(("execution", vmid)),
    )
    monkeypatch.setattr(tui_session, "Api", lambda: None, raising=False)
    # `os` itself, not the name the module re-exports: mypy refuses the
    # attribute on a module that does not export it.
    monkeypatch.setattr(os, "fork", lambda: 1)

    tui_session.start("conv", spec=3, node="infra-node3", convert=9300)

    assert asked == [("convert", 9300)], asked


def test_a_conversion_without_a_node_is_refused_before_anything_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guest already exists, so the harness cannot choose where it lives
    the way it does for a guest it is about to create."""
    from tests.vm import cluster

    monkeypatch.setattr(tui_session, "SESSIONS", tmp_path)
    monkeypatch.setattr(
        cluster,
        "tui_conversion",
        lambda *a, **k: pytest.fail("a refused conversion must not reach the cluster"),
    )
    monkeypatch.setattr(tui_session, "Api", lambda: None, raising=False)

    with pytest.raises(tui_session.SessionError, match="--node names where it is"):
        tui_session.start("conv", spec=3, convert=9300)
