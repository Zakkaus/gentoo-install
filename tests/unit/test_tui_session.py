# SPDX-License-Identifier: GPL-2.0-or-later
"""The daemon that holds one session's console."""

from __future__ import annotations

import socket
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
