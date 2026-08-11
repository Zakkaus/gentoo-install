from __future__ import annotations

import socket

import pytest

from tests.vm.console import (
    ConsoleClosed,
    ConsoleTimeout,
    SerialConsole,
    _Socket,
    strip_ansi,
)


def make_console(chunks: list[bytes]) -> tuple[SerialConsole, socket.socket]:
    reader, writer = socket.socketpair()
    for chunk in chunks:
        writer.sendall(chunk)
    reader.settimeout(0.1)
    return SerialConsole(_Socket(reader), open("/dev/null", "wb")), writer


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


@pytest.mark.parametrize(("method", "value"), [("send", "true"), ("send_raw", "x")])
def test_a_local_write_error_is_reported_at_the_send_call(method: str, value: str) -> None:
    class Broken:
        closed = False

        def recv(self, size: int) -> bytes:
            return b""

        def sendall(self, data: bytes) -> None:
            raise BrokenPipeError(32, "Broken pipe")

        def close(self) -> None:
            return None

    console = SerialConsole(Broken(), open("/dev/null", "wb"))
    with pytest.raises(ConsoleClosed, match="Broken pipe") as caught:
        getattr(console, method)(value)
    assert caught.value.write_may_have_reached_guest is True


def test_a_write_to_an_already_closed_channel_is_locally_rejected() -> None:
    class Closed:
        closed = True

        def recv(self, size: int) -> bytes:
            return b""

        def sendall(self, data: bytes) -> None:
            raise AssertionError("a closed channel must reject before writing")

        def close(self) -> None:
            return None

    console = SerialConsole(Closed(), open("/dev/null", "wb"))
    with pytest.raises(ConsoleClosed) as caught:
        console.send("true")
    assert caught.value.write_may_have_reached_guest is False


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
