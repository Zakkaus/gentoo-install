from __future__ import annotations

import socket

import pytest

from tests.vm.console import ConsoleClosed, ConsoleTimeout, SerialConsole, strip_ansi


def make_console(chunks: list[bytes]) -> tuple[SerialConsole, socket.socket]:
    reader, writer = socket.socketpair()
    for chunk in chunks:
        writer.sendall(chunk)
    reader.settimeout(0.1)
    return SerialConsole(reader, open("/dev/null", "wb")), writer


def test_strip_ansi_removes_colour_and_title_sequences() -> None:
    raw = b"\x1b[0;32m ok \x1b[0m\x1b]0;title\x07done"
    assert strip_ansi(raw) == b" ok done"


def test_expect_matches_across_chunk_boundaries() -> None:
    console, writer = make_console([b"livecd ", b"~ #"])
    assert console.expect(r"livecd ~ #", timeout=2.0).endswith(b"livecd ~ #")
    writer.close()


def test_expect_reports_the_tail_on_timeout() -> None:
    console, writer = make_console([b"nothing useful here"])
    with pytest.raises(ConsoleTimeout, match="nothing useful here"):
        console.expect(r"never appears", timeout=0.5)
    writer.close()


def test_send_appends_a_newline() -> None:
    console, writer = make_console([])
    console.send("true")
    assert writer.recv(4096) == b"true\n"
    writer.close()


def test_expect_keeps_output_that_arrived_after_the_match() -> None:
    console, writer = make_console([b"login: root\r\nPassword: "])
    console.expect(r"login:", timeout=2.0)
    assert console.expect(r"Password:", timeout=0.5).endswith(b"Password:")
    writer.close()


def test_closed_socket_is_not_reported_as_a_timeout() -> None:
    console, writer = make_console([])
    writer.close()
    with pytest.raises(ConsoleClosed):
        console.expect(r"anything", timeout=2.0)
