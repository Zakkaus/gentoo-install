# SPDX-License-Identifier: GPL-2.0-or-later
"""The cluster backend's parsing and its safety guard, with no cluster."""

from __future__ import annotations

import shlex
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import Message
import io
import struct
from threading import Event
import time
import urllib.error
import urllib.response
import urllib.request
from pathlib import Path
from typing import Any, Final, cast

import threading

import pytest

from tests.vm import proxmox
from tests.vm.proxmox import (
    TAG,
    Api,
    CreateConflict,
    ForeignGuest,
    Guest,
    GuestSpec,
    ProxmoxError,
    ProxmoxNotFound,
    ProxmoxTransientError,
    Traffic,
    _RejectRedirect,
    _line_of_linux,
)
from tests.vm.websocket import WebSocket, WebSocketError, _client_frame

#: One editor screen as GRUB drew it on the serial console, captured from the
#: Gentoo minimal ISO. `search` sits between `setparams` and `linux`, which is
#: what made a fixed count edit the wrong line.
GENTOO_EDITOR = (
    b"\x1b[07;03Hsetparams 'Boot LiveCD (kernel: gentoo)'   "
    b"\x1b[08;03H        search --no-floppy --set=root -l Gentoo-amd64-20260712   "
    b"\x1b[09;03H        linux /boot/gentoo dokeymap nodhcp cdroot   "
    b"\x1b[10;03H        initrd /boot/gentoo.igz"
)


def test_the_api_token_accepts_the_cluster_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "01234567-89ab-cdef-0123-456789abcdef"
    token_file = tmp_path / "token"
    token_file.write_text(f" \n{token}\n")
    monkeypatch.setattr(proxmox, "TOKEN_FILE", token_file)

    assert proxmox._secret() == token


def test_the_api_token_accepts_a_non_uuid_header_safe_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "opaque.token_secret=ABC+123"
    token_file = tmp_path / "token"
    token_file.write_text(token)
    monkeypatch.setattr(proxmox, "TOKEN_FILE", token_file)

    assert proxmox._secret() == token


@pytest.mark.parametrize(
    "token",
    [
        "01234567-89ab-cdef\r\n0123-456789abcdef",
        "01234567-89ab-cdef\x000123-456789abcdef",
        "01234567-89ab-cdef\x7f0123-456789abcdef",
        "01234567-89ab-cdef 0123-456789abcdef",
        "01234567-89ab-cdef\t0123-456789abcdef",
    ],
)
def test_the_api_token_rejects_header_breaking_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(token)
    monkeypatch.setattr(proxmox, "TOKEN_FILE", token_file)

    with pytest.raises(ProxmoxError) as raised:
        proxmox._secret()

    assert str(raised.value) == f"the API token has an invalid format at {token_file}"
    assert token not in str(raised.value)


def test_the_api_token_rejects_empty_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(" \n\t")
    monkeypatch.setattr(proxmox, "TOKEN_FILE", token_file)

    with pytest.raises(ProxmoxError) as raised:
        proxmox._secret()

    assert str(raised.value) == f"the API token has an invalid format at {token_file}"


def test_an_invalid_api_token_is_not_disclosed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "01234567-89ab-cdef-0123-456789abcde\nLEAK-CANARY"
    token_file = tmp_path / "token"
    token_file.write_text(token)
    monkeypatch.setattr(proxmox, "TOKEN_FILE", token_file)

    with pytest.raises(ProxmoxError) as raised:
        proxmox._secret()

    assert str(raised.value) == f"the API token has an invalid format at {token_file}"
    assert "LEAK-CANARY" not in str(raised.value)


def test_the_linux_line_is_read_off_the_screen() -> None:
    assert _line_of_linux(GENTOO_EDITOR) == 2


def test_an_entry_with_no_search_line_needs_one_fewer() -> None:
    screen = (
        b"\x1b[07;03Hsetparams 'Install'   "
        b"\x1b[08;03H        linuxefi /images/pxeboot/vmlinuz quiet   "
        b"\x1b[09;03H        initrdefi /images/pxeboot/initrd.img"
    )
    assert _line_of_linux(screen) == 1


def test_a_screen_with_no_grub_entry_is_refused() -> None:
    """Guessing a number here would edit whatever sat on that row, and the
    entry would boot unchanged with its own line corrupted."""
    with pytest.raises(ProxmoxError, match="no GRUB entry"):
        _line_of_linux(b"\x1b[07;03HSeaBIOS   \x1b[08;03HNo bootable device.")


class _Recording(Api):
    """An `Api` that answers from a script instead of the cluster."""

    def __init__(self, answers: dict[str, Any]) -> None:
        super().__init__(host="nowhere.invalid")
        self.answers = answers
        self.asked: list[tuple[str, str]] = []
        self.removed: set[int] = set()

    def call(self, method: str, path: str, **form: Any) -> Any:
        self.asked.append((method, path))
        vmid = int(path.split("/qemu/", 1)[1].split("/", 1)[0]) if "/qemu/" in path else -1
        if method == "GET" and path.endswith("/config") and vmid in self.removed:
            raise ProxmoxNotFound("the guest does not exist")
        if method == "DELETE":
            self.removed.add(vmid)
        for key, value in self.answers.items():
            if key in path:
                return value
        return {}

    def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
        return None


def test_a_guest_without_the_tag_is_not_deleted() -> None:
    """`9002` in the range this harness allocates from is
    `prod-debian-12-server-template`. A VMID is not ownership."""
    api = _Recording({"config": {"name": "prod-debian-12-server-template", "tags": "debian;prod"}})
    guest = Guest(api=api, node="infra-node1", vmid=9002, spec=GuestSpec(name="x", iso="x"))
    with pytest.raises(ProxmoxError, match="refusing to remove"):
        guest.destroy()
    assert not any(method == "DELETE" for method, _ in api.asked)


def test_a_tagged_guest_is_deleted() -> None:
    api = _Recording({"config": {"name": "gi-run", "tags": f"{TAG};gi-one"}})
    guest = Guest(
        api=api,
        node="infra-node4",
        vmid=9300,
        spec=GuestSpec(name="x", iso="x", nonce="gi-one"),
    )
    guest.destroy()
    assert ("DELETE", "/nodes/infra-node4/qemu/9300") in api.asked


def test_ours_needs_the_tag_as_well_as_the_range() -> None:
    api = _Recording(
        {
            "resources": [
                {"node": "infra-node1", "vmid": 9002, "tags": "debian;prod"},
                {"node": "infra-node4", "vmid": 9300, "tags": TAG},
                {"node": "infra-node5", "vmid": 114514, "tags": TAG},
            ]
        }
    )
    assert api.ours() == [("infra-node4", 9300)]


def test_a_vmid_is_allocated_around_everything_on_the_cluster() -> None:
    """Not around the harness's own machines: 9300 free of our guests but held
    by somebody else's would collide."""
    api = _Recording(
        {"resources": [{"node": "n", "vmid": 9300, "tags": "someone-else"}]}
    )
    assert api.free_vmid() == 9301


def test_a_delete_carries_its_parameters_in_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body on DELETE is answered `501 Unexpected content for method`, so the
    two methods that take no body put their parameters in the query string."""
    seen: dict[str, Any] = {}

    class Answer:
        headers: dict[str, str] = {}

        def read(self) -> bytes:
            return b'{"data":"UPID:x"}'

        def __enter__(self) -> Answer:
            return self

        def __exit__(self, *rest: object) -> None:
            return None

    def fake_open(request: Any, timeout: float = 0.0, context: Any = None) -> Any:
        seen["url"] = request.full_url
        seen["body"] = request.data
        return Answer()

    api = Api(host="nowhere.invalid")
    monkeypatch.setattr(api._opener, "open", fake_open)
    api.call("DELETE", "/nodes/n/qemu/9300", purge=1)
    removed = (seen["url"], seen["body"])
    api.call("POST", "/nodes/n/qemu", vmid=9300)
    created = (seen["url"], seen["body"])

    assert removed == ("https://nowhere.invalid/api2/json/nodes/n/qemu/9300?purge=1", None)
    assert created == ("https://nowhere.invalid/api2/json/nodes/n/qemu", b"vmid=9300")


def test_ordinary_api_calls_do_not_reuse_load_balancer_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []

    class Answer(io.BytesIO):
        def __init__(self, cookie: str) -> None:
            super().__init__(b'{"data":{}}')
            self.headers = Message()
            self.headers.add_header("Set-Cookie", cookie)

        def __enter__(self) -> Answer:
            return self

        def __exit__(self, *unused: object) -> None:
            return None

    class Opener:
        def open(
            self, request: urllib.request.Request, timeout: float = 0.0
        ) -> Answer:
            requests.append(request)
            return Answer(f"INGRESSCOOKIE=worker-{len(requests)}; Path=/")

    api = Api(host="nowhere.invalid")
    api._opener = cast(urllib.request.OpenerDirector, Opener())
    monkeypatch.setattr(proxmox, "_secret", lambda: "secret")

    api.call("GET", "/nodes")
    api.call("GET", "/cluster/status")

    assert [request.get_header("Cookie") for request in requests] == [None, None]
    assert not hasattr(api, "affinity")


class _HttpRefusal:
    def __init__(self, code: int, reason: str, body: str) -> None:
        self.code = code
        self.reason = reason
        self.body = body
        self.attempts = 0

    def open(self, request: urllib.request.Request, timeout: float = 0.0) -> Any:
        self.attempts += 1
        raise urllib.error.HTTPError(
            request.full_url,
            self.code,
            self.reason,
            Message(),
            io.BytesIO(self.body.encode()),
        )


def _refusing_api(
    monkeypatch: pytest.MonkeyPatch, code: int, reason: str, body: str
) -> tuple[Api, _HttpRefusal]:
    refusal = _HttpRefusal(code, reason, body)
    api = Api(host="nowhere.invalid")
    api._opener = cast(urllib.request.OpenerDirector, refusal)
    monkeypatch.setattr(proxmox, "_secret", lambda: "secret")
    return api, refusal


@pytest.mark.parametrize(
    ("method", "path", "code"),
    [
        ("GET", "/nodes", 429),
        ("GET", "/nodes", 502),
        ("GET", "/nodes", 503),
        ("GET", "/nodes", 504),
        # The cluster proxy could not reach the node. `infra-node3` answered
        # this while restarting and ended a campaign at its first dispatch.
        ("GET", "/nodes/node/qemu/9306/status", 595),
        ("POST", "/nodes/node/qemu/9300/termproxy", 500),
        ("GET", "/nodes/node/tasks/UPID%3Anode%3Atask/status", 500),
    ],
)
def test_documented_http_failures_are_transient(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, code: int
) -> None:
    body = '{"data":null,"message":"try later"}'
    api, refusal = _refusing_api(monkeypatch, code, "Temporary refusal", body)
    with pytest.raises(ProxmoxTransientError) as raised:
        api.call(method, path)
    said = str(raised.value)
    assert f"{method} {path} answered {code} Temporary refusal" in said
    assert body in said
    assert refusal.attempts == 1


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection reset"),
        TimeoutError("timed out"),
        OSError("network unreachable"),
    ],
)
def test_transport_failures_are_transient(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    class Failing:
        def open(self, request: urllib.request.Request, timeout: float = 0.0) -> Any:
            raise failure

    api = Api(host="nowhere.invalid")
    api._opener = cast(urllib.request.OpenerDirector, Failing())
    monkeypatch.setattr(proxmox, "_secret", lambda: "secret")
    with pytest.raises(ProxmoxTransientError, match="did not answer"):
        api.call("GET", "/nodes")


def test_a_missing_vm_http_500_is_not_found_at_the_api_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "Configuration file 'nodes/node/qemu-server/9300.conf' does not exist"
    body = '{"data":null}'
    api, refusal = _refusing_api(monkeypatch, 500, reason, body)
    with pytest.raises(ProxmoxNotFound) as raised:
        api.call("GET", "/nodes/node/qemu/9300/config")
    said = str(raised.value)
    assert reason in said
    assert body in said
    assert refusal.attempts == 1


def test_a_permanent_http_500_is_not_retried_by_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as clock

    body = '{"data":null,"message":"permission denied by policy"}'
    api, refusal = _refusing_api(monkeypatch, 500, "Internal Server Error", body)
    moments = iter((0.0, 0.0, 0.0, 0.0, 0.0, 2.0))
    monkeypatch.setattr(clock, "monotonic", lambda: next(moments))
    monkeypatch.setattr(clock, "sleep", lambda seconds: None)
    guest = Guest(
        api,
        "node",
        9300,
        GuestSpec(name="x", iso="x", nonce="gi-owned"),
    )
    with pytest.raises(ProxmoxError) as raised:
        guest.destroy(patience=1.0)
    said = str(raised.value)
    assert "500 Internal Server Error" in said
    assert body in said
    assert refusal.attempts == 1


def test_an_api_redirect_cannot_create_a_second_authorized_request() -> None:
    """A 302 to another origin inherited the cluster administrator token."""
    seen: list[tuple[str, str | None]] = []

    class Redirecting(urllib.request.HTTPSHandler):
        def https_open(self, request: urllib.request.Request) -> Any:
            seen.append((request.full_url, request.get_header("Authorization")))
            headers = Message()
            headers.add_header("Location", "https://example.invalid/stolen")
            response = urllib.response.addinfourl(
                io.BytesIO(), headers, request.full_url, code=302
            )
            setattr(response, "msg", "Found")
            return response

    api = Api(host="nowhere.invalid")
    api._opener = urllib.request.build_opener(Redirecting(), _RejectRedirect())
    with pytest.raises(ProxmoxError, match="302"):
        api.call("GET", "/nodes")
    assert len(seen) == 1
    assert seen[0][0].startswith("https://nowhere.invalid/")
    assert seen[0][1] is not None


def test_the_console_channel_frames_input_the_way_proxmox_reads_it() -> None:
    """`0:<bytes>:<data>`. Without the length prefix the proxy discards it."""

    class Fake:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.closed = False

        def send(self, payload: bytes, opcode: int = 0x2) -> None:
            self.sent.append(payload)

        def read(self) -> bytes:
            return b""

        def close(self) -> None:
            self.closed = True

    fake = Fake()
    channel = proxmox.ConsoleChannel(fake)
    channel.sendall(b"uname -r\n")
    assert fake.sent == [b"0:9:uname -r\n"]


def test_a_websocket_write_that_closes_is_reported_at_the_send_call() -> None:
    from tests.vm.console import ConsoleClosed, SerialConsole

    class Closing:
        def __init__(self) -> None:
            self.closed = False
            self.why_closed = ""

        def send(self, payload: bytes, opcode: int = 0x2) -> None:
            self.closed = True
            self.why_closed = "the connection broke: Broken pipe"

        def read(self) -> bytes:
            raise AssertionError("the failed send must not become a read timeout")

        def close(self) -> None:
            self.closed = True

    console = SerialConsole(proxmox.ConsoleChannel(Closing()), io.BytesIO())
    with pytest.raises(ConsoleClosed, match="Broken pipe") as caught:
        console.send("uname -r")
    assert caught.value.write_may_have_reached_guest is True


def test_a_server_frame_is_read_whole_across_two_reads() -> None:
    """One console write can arrive in two packets, and half a frame is not a
    short read to pass upward."""
    payload = b"root@livecd ~ # "
    whole = struct.pack("!BB", 0x81, len(payload)) + payload

    class Split:
        def __init__(self) -> None:
            self.parts = [whole[:3], whole[3:]]

        def recv(self, size: int, /) -> bytes:
            return self.parts.pop(0) if self.parts else b""

        def sendall(self, data: bytes, /) -> None:
            raise AssertionError("this test never writes")

        def close(self) -> None:
            pass

        def settimeout(self, seconds: float | None, /) -> None:
            pass

    socket = WebSocket(Split())
    assert socket.read() == b""
    assert socket.read() == payload


def test_a_client_frame_is_masked() -> None:
    """RFC 6455: a client always masks, and Proxmox drops a frame that is not."""
    frame = _client_frame(b"hi", 0x2)
    assert frame[1] & 0x80, "the mask bit has to be set"
    mask = frame[2:6]
    assert bytes(b ^ mask[n % 4] for n, b in enumerate(frame[6:])) == b"hi"


class _FramedStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.sent: list[bytes] = []
        self.closed = False

    def recv(self, size: int, /) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data: bytes, /) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True

    def settimeout(self, seconds: float | None, /) -> None:
        return None


def _client_payload(frame: bytes) -> tuple[int, bytes]:
    size = frame[1] & 0x7F
    assert size < 126
    mask = frame[2:6]
    payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(frame[6:]))
    return frame[0] & 0x0F, payload


def test_a_fragmented_message_survives_an_interleaved_ping() -> None:
    """The continuation payload was discarded after the first fragment."""
    stream = _FramedStream([b"\x02\x02ab\x89\x01P\x80\x02cd"])
    socket = WebSocket(stream)
    assert socket.read() == b"abcd"
    assert [_client_payload(one) for one in stream.sent] == [(0xA, b"P")]


def test_a_received_close_is_answered_once_and_closes_the_transport() -> None:
    """The peer waited for a close reply while the client left the socket open."""
    payload = struct.pack("!H", 1000)
    stream = _FramedStream([bytes((0x88, len(payload))) + payload])
    socket = WebSocket(stream)
    assert socket.read() == b""
    assert [_client_payload(one) for one in stream.sent] == [(0x8, payload)]
    assert stream.closed and socket.closed


def test_an_oversized_declared_frame_is_refused_before_its_payload_arrives() -> None:
    """A declared 2^40-byte frame otherwise grew the receive buffer without a bound."""
    stream = _FramedStream([b"\x82\x7f" + struct.pack("!Q", 1 << 40)])
    with pytest.raises(WebSocketError, match="declares"):
        WebSocket(stream).read()
    assert stream.closed


def test_the_handshake_accept_must_match_the_key_that_was_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any status line containing 101 was accepted without proving the upgrade."""
    import base64
    import hashlib
    import os
    import socket as socket_module
    import ssl
    from typing import cast

    from tests.vm import websocket

    key_bytes = b"0123456789abcdef"
    key = base64.b64encode(key_bytes)
    accept = base64.b64encode(
        hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
    )

    class Handshake(_FramedStream):
        pass

    class Context:
        def __init__(self, reply: bytes) -> None:
            self.reply = reply
            self.secure: Handshake | None = None

        def wrap_socket(self, raw: Any, server_hostname: str) -> Any:
            self.secure = Handshake([self.reply])
            return self.secure

    class Raw:
        def __init__(self) -> None:
            self.closed = False
            #: What `keep_asking` set, so the double answers the same calls the
            #: real socket does rather than only the ones this test reads.
            self.options: list[tuple[int, int, int]] = []

        def setsockopt(self, level: int, option: int, value: int) -> None:
            self.options.append((level, option, value))

        def close(self) -> None:
            self.closed = True

    prefix = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Protocol: binary\r\nSec-WebSocket-Accept: "
    )
    monkeypatch.setattr(os, "urandom", lambda size: key_bytes)
    monkeypatch.setattr(socket_module, "create_connection", lambda *args, **kwargs: Raw())

    valid = Context(prefix + accept + b"\r\n\r\n")
    assert WebSocket.connect(
        "pve.invalid", "/console", {}, context=cast(ssl.SSLContext, valid)
    ).closed is False

    invalid = Context(prefix + b"wrong\r\n\r\n")
    with pytest.raises(WebSocketError, match="accept"):
        WebSocket.connect(
            "pve.invalid", "/console", {}, context=cast(ssl.SSLContext, invalid)
        )
    assert invalid.secure is not None and invalid.secure.closed


def _archive(files: dict[str, bytes]) -> str:
    import base64 as b64
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(f"./{name}")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return b64.b64encode(buffer.getvalue()).decode()


def test_results_come_back_through_the_console() -> None:
    """The guest's disks are on shared storage the workstation cannot read and
    the API downloads no volume, so the console is the only channel back."""
    from tests.vm.results import CONSOLE_CLOSE, CONSOLE_OPEN, console_command, read_console

    encoded = _archive({"install.log": b"ok\n", "exit.txt": b"0\n"})
    wrapped = "\r\n".join(encoded[at : at + 80] for at in range(0, len(encoded), 80))
    # The shell echoes the command, so the markers appear twice; the terminal
    # wraps the one line base64 wrote, so the payload arrives in pieces.
    said = (
        f"root@livecd ~ # {console_command('/tmp/results')}\r\n"
        f"{CONSOLE_OPEN}\r\n{wrapped}\r\n\r\n{CONSOLE_CLOSE}\r\nroot@livecd ~ # "
    ).encode()
    assert read_console(said) == {"install.log": b"ok\n", "exit.txt": b"0\n"}


def test_a_console_with_no_archive_is_an_error_not_an_empty_result() -> None:
    from tests.vm.results import ResultError, read_console

    with pytest.raises(ResultError, match="no result archive"):
        read_console(b"root@livecd ~ # the install died here")


def test_a_truncated_archive_is_refused() -> None:
    """A guest killed mid-transfer answers half a stream, and half a tar read
    as an empty result would report a passing run."""
    from tests.vm.results import CONSOLE_CLOSE, CONSOLE_OPEN, ResultError, read_console

    encoded = _archive({"install.log": b"ok\n"})
    said = f"{CONSOLE_OPEN}\r\n{encoded[: len(encoded) // 2]}\r\n{CONSOLE_CLOSE}\r\n".encode()
    with pytest.raises(ResultError):
        read_console(said)


def test_the_watchdog_counts_quiet_looks_not_elapsed_time(tmp_path: Path) -> None:
    """A slow mirror and a dead guest take the same wall-clock; only one of
    them is still writing to the console."""
    from tests.vm.cluster import WATCH_STRIKES, Watchdog

    log = tmp_path / "run.log"
    log.write_bytes(b"booting\n")
    watch = Watchdog(log=log, counters=lambda: Traffic(0, 0, 0.0))
    # Read into locals: mypy narrows a property and never widens it again,
    # because it cannot see `moved()` changing what `stuck` answers.
    first = (watch.moved(), watch.stuck)
    quiet = [watch.moved() for _ in range(WATCH_STRIKES)]
    after = watch.stuck
    # One more byte and it is alive again: a build that prints once an hour
    # must not be ended for the silence before it.
    log.write_bytes(b"booting\nunpacking\n")
    revived = (watch.moved(), watch.stuck)

    assert first == (True, False), "the first look sees the whole log"
    assert quiet == [False] * WATCH_STRIKES
    assert after is True
    assert revived == (True, False)


def test_a_silent_guest_that_is_still_downloading_is_left_alone(tmp_path: Path) -> None:
    """An install says nothing on the console while it fetches a stage3, and
    that is the guest doing the most work. Ending it for the silence is how a
    watchdog kills the runs it exists to protect."""
    from tests.vm.cluster import QUIET_BYTES, WATCH_STRIKES, Watchdog

    log = tmp_path / "quiet.log"
    log.write_bytes(b"downloading\n")
    moving = [0]

    def counters() -> Traffic:
        moving[0] += QUIET_BYTES * 2
        return Traffic(moving[0], 0, 0.0)

    watch = Watchdog(log=log, counters=counters)
    looks = [watch.moved() for _ in range(WATCH_STRIKES + 2)]
    assert looks == [True] * (WATCH_STRIKES + 2)
    assert not watch.stuck


def test_a_guest_compiling_in_memory_is_not_read_as_stuck(tmp_path: Path) -> None:
    """`vm-binhost-fallback` was ended at 48.8 minutes with

        the console was silent for 1200s and counters were flat
        (16883546473 -> 16883554254 bytes)

    while it was building grub. A build whose directory is in RAM writes no
    disk and answers no network, so the bytes are the wrong question on their
    own; the guest's share of a core answers it."""
    from tests.vm.cluster import BUSY_CPU, WATCH_STRIKES, Watchdog

    log = tmp_path / "compiling.log"
    log.write_bytes(b"")
    watch = Watchdog(log=log, counters=lambda: Traffic(5_000_000, 0, BUSY_CPU))
    looks = [watch.moved() for _ in range(WATCH_STRIKES + 2)]

    assert looks == [True] * (WATCH_STRIKES + 2), looks
    assert not watch.stuck


def test_a_guest_moving_nothing_at_all_is_stuck(tmp_path: Path) -> None:
    """Console silent and counters flat: not slow, dead."""
    from tests.vm.cluster import WATCH_STRIKES, Watchdog

    log = tmp_path / "dead.log"
    log.write_bytes(b"")
    watch = Watchdog(log=log, counters=lambda: Traffic(5_000_000, 0, 0.0))
    quiet = [watch.moved() for _ in range(WATCH_STRIKES + 1)]
    assert quiet[1:] == [False] * WATCH_STRIKES
    assert watch.stuck


def test_a_stuck_guest_is_stopped_and_not_deleted_by_the_sweep(tmp_path: Path) -> None:
    """Stopping is what wakes the worker blocked on its console; the worker
    then reports and deletes. A sweep that deleted would race it."""
    from tests.vm.cluster import GUEST_MEMORY_MIB, WATCH_STRIKES, Running, Watchdog, _sweep

    stopped: list[str] = []

    class Quiet:
        def stop(self) -> None:
            stopped.append("stopped")

        def destroy(self) -> None:
            raise AssertionError("the sweep must not delete a guest")

    log = tmp_path / "quiet.log"
    log.write_bytes(b"")
    watch = Watchdog(log=log, counters=lambda: Traffic(0, 0, 0.0), strikes=WATCH_STRIKES - 1)
    inflight = {
        "vm-zfs": Running(
            guest=Quiet(),
            watch=watch,
            reservation_bytes=GUEST_MEMORY_MIB * 1024**2,
        )
    }
    _sweep(inflight)
    assert stopped == ["stopped"]


def test_a_kernel_parameter_can_be_typed_through_sendkey() -> None:
    """`console=ttyS0,115200` at a GRUB prompt on a BIOS guest: every
    character needs a qemu key name, and `equal` and `comma` were missing."""
    from tests.vm.monitor import keys_for

    assert keys_for(" console=ttyS0,115200") == [
        "spc", "c", "o", "n", "s", "o", "l", "e", "equal",
        "t", "t", "y", "shift-s", "0", "comma", "1", "1", "5", "2", "0", "0",
    ]


def test_every_printable_ascii_character_has_a_key_name() -> None:
    """A missing name is refused rather than sent as itself and dropped, so
    the table has to cover what a passphrase or a command line can hold."""
    from tests.vm.monitor import keys_for

    printable = "".join(chr(code) for code in range(0x20, 0x7F))
    assert len(keys_for(printable)) == len(printable)


def test_every_dispatched_job_answers_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that dies without answering leaves its name in the running set
    for ever and the schedule never ends. One run sat idle for half an hour
    with an empty cluster and a job still queued, because `WebSocketError` out
    of a dropped console was outside the handled set."""
    import queue as queueing

    from tests.vm import cluster
    from tests.vm.cluster import Job, Outcome, Verdict, answer_once
    from tests.vm.websocket import WebSocketError

    def explode(*rest: object) -> Outcome:
        raise WebSocketError("the connection broke")

    monkeypatch.setattr(cluster, "install_one", explode)
    done: queueing.Queue[Outcome] = queueing.Queue()
    job = Job(name="vm-lvm", fixture=tmp_path / "vm-lvm.toml")
    answer_once(done, Api(host="nowhere.invalid"), "infra-node3", job, "d.iso", tmp_path, {})

    answered = done.get_nowait()
    assert (answered.name, answered.verdict) == ("vm-lvm", Verdict.ERROR)
    assert "WebSocketError" in answered.detail
    assert done.empty(), "one job, one answer"


def test_a_node_offers_a_slot_for_every_guest_it_can_hold() -> None:
    """One slot per node capped a six-node cluster with 51 GiB spare at three
    guests at a time, with twenty jobs queued behind them."""
    from tests.vm import cluster
    from tests.vm.cluster import GUEST_MEMORY_MIB, NODE_HEADROOM_BYTES, free_slots
    from tests.vm.proxmox import Node

    guest = GUEST_MEMORY_MIB * 1024**2

    class Counted(Api):
        def nodes(self) -> list[Node]:
            return [
                Node(name="big", free_bytes=NODE_HEADROOM_BYTES + guest * 3, cores=4, free_cores=64.0),
                Node(name="one", free_bytes=NODE_HEADROOM_BYTES + guest, cores=4, free_cores=64.0),
                Node(name="none", free_bytes=NODE_HEADROOM_BYTES + guest - 1, cores=4, free_cores=64.0),
            ]

    names = [node.name for node in free_slots(Counted(host="nowhere.invalid"))]
    # Counted, not ordered: the order is round robin so that one node does not
    # carry every build, and that rule has a test of its own.
    assert Counter(names) == Counter({"big": 3, "one": 1}), names
    assert "none" not in names, "the headroom is left free on every node"


def test_the_editor_is_reopened_with_escape_not_a_second_e() -> None:
    """A bare second `e` types the letter into the command line. ESC discards
    the edits in the editor and does nothing in the menu, so it is safe in
    both, and one run read `no GRUB entry to edit on this screen` off a
    countdown line eight seconds from booting."""
    from tests.vm.proxmox import _editor_screen

    class Slow:
        """Answers the menu once, then the editor."""

        def __init__(self) -> None:
            self.sent: list[str] = []
            self.screens = [
                b"\x1b[54;01H   The highlighted entry will be executed automatically in 8s.",
                GENTOO_EDITOR,
            ]

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return self.screens.pop(0) if self.screens else b""

        def send(self, line: str) -> None:
            self.sent.append(line)

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            assert pattern == "GNU GRUB", pattern
            return b"GNU GRUB  version 2.14"

        @property
        def closed(self) -> bool:
            return False

        def close(self) -> None:
            pass

    console = Slow()
    screen = _editor_screen(console, 30.0)
    assert b"setparams" in screen
    # The leading ESC halts the countdown: GRUB stops it on the first key it
    # receives, and a run whose `e` arrived before the menu was drawn watched
    # the entry boot ten seconds later.
    assert console.sent == ["e", "\x1b", "e"]


def test_the_editor_is_asked_for_without_waiting_for_the_menu_again() -> None:
    """`hold_the_menu` waits for the menu and stops the countdown. Asking again
    inside `_editor_screen` blocked the whole patience on a header that had
    already gone past, and every press landed after it: nineteen guests of one
    round ended at `GRUB never opened its editor`."""
    from tests.vm.proxmox import _editor_screen

    class Held:
        """The menu is already up and its countdown already stopped."""

        def __init__(self) -> None:
            self.sent: list[str] = []

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return GENTOO_EDITOR if self.sent else b""

        def send(self, line: str) -> None:
            self.sent.append(line)

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            raise AssertionError("hold_the_menu already waited for the menu")

        @property
        def closed(self) -> bool:
            return False

        def close(self) -> None:
            pass

    console = Held()
    assert b"setparams" in _editor_screen(console, 30.0)
    assert console.sent == ["e"], console.sent


def test_a_long_install_is_never_sent_twice_after_a_reconnect() -> None:
    """A console dropped mid-install is reopened and listened to again. Handing
    the command over a second time would start another install on a target the
    first one has half written."""
    from tests.vm.cluster import Reconnecting
    from tests.vm.console import ConsoleClosed

    sent: list[str] = []
    opened: list[int] = []

    class Flaky:
        def __init__(self, drops: int) -> None:
            self.drops = drops

        def send(self, line: str) -> None:
            sent.append(line)

        def send_raw(self, keys: str) -> None:
            sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            if self.drops:
                self.drops -= 1
                raise ConsoleClosed("the guest closed the serial connection")
            return b"MARK_1_DONE"

        def close(self) -> None:
            pass

    def open_console() -> Flaky:
        opened.append(1)
        return Flaky(drops=1 if len(opened) < 3 else 0)

    link = Reconnecting(open_console, tries=4)
    link.wait_for("sh install.sh", timeout=5.0)

    commands = [one for one in sent if "install.sh" in one]
    assert len(commands) == 1, commands
    assert len(opened) == 3, "one open, then one per drop"


def test_wait_for_does_not_resend_after_an_ambiguous_write_failure() -> None:
    from tests.vm.cluster import Reconnecting
    from tests.vm.console import ConsoleClosed

    sent: list[str] = []

    class Console:
        def __init__(self, fail_write: bool) -> None:
            self.fail_write = fail_write

        def send(self, line: str) -> None:
            sent.append(line)
            if self.fail_write:
                raise ConsoleClosed(
                    "the connection broke during the write",
                    write_may_have_reached_guest=True,
                )

        def send_raw(self, keys: str) -> None:
            # The reopen clears whatever half a line the drop left; an
            # interrupt cancels and cannot start a second install, which is
            # the thing this test exists to prevent.
            raw.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            return b"MARK_1_DONE"

        def close(self) -> None:
            pass

    raw: list[str] = []
    consoles = [Console(fail_write=True), Console(fail_write=False)]
    link = Reconnecting(lambda: consoles.pop(0), tries=2)
    link.wait_for("sh install.sh", timeout=5.0)

    commands = [one for one in sent if "install.sh" in one]
    assert len(commands) == 1, commands
    # Nothing raw carries the command either: a second install is what the
    # ambiguous write might already have started.
    assert not [one for one in raw if "install.sh" in one], raw


def test_a_short_command_is_sent_again_after_a_reconnect() -> None:
    """The shell never received it, so waiting for its marker would wait for
    ever."""
    from tests.vm.cluster import Reconnecting
    from tests.vm.console import ConsoleClosed

    sent: list[str] = []

    class Once:
        def __init__(self, drop: bool) -> None:
            self.drop = drop

        def send(self, line: str) -> None:
            sent.append(line)

        def send_raw(self, keys: str) -> None:
            sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            if self.drop:
                raise ConsoleClosed("dropped")
            return b"done"

        def close(self) -> None:
            pass

    opens = [Once(drop=True), Once(drop=False)]
    link = Reconnecting(lambda: opens.pop(0), tries=3)
    link.run("mkdir -p /mnt/driver")
    assert [one for one in sent if "mkdir" in one].__len__() == 2


def test_guests_already_placed_are_subtracted_from_what_a_node_reports() -> None:
    """A guest's memory is allocated lazily, so a node with eleven freshly
    started still reported 13.8 GiB free. Reading that alone dispatched twenty
    guests wanting 120 GiB onto a cluster with 71, on hardware running other
    people's machines."""
    from tests.vm.cluster import GUEST_MEMORY_MIB, NODE_HEADROOM_BYTES, free_slots
    from tests.vm.proxmox import Node

    guest = GUEST_MEMORY_MIB * 1024**2

    class Lagging(Api):
        def nodes(self) -> list[Node]:
            return [Node(name="one", free_bytes=NODE_HEADROOM_BYTES + guest * 3, cores=4, free_cores=64.0)]

    api = Lagging(host="nowhere.invalid")
    assert len(free_slots(api)) == 3
    assert len(free_slots(api, {"one": guest * 2})) == 1
    assert free_slots(api, {"one": guest * 3}) == []
    assert free_slots(api, {"one": guest * 9}) == [], "never negative"


def test_the_archive_is_waited_for_rather_than_timed() -> None:
    """An install log runs to twelve megabytes and the console carries it a
    chunk at a time. A fixed window caught only the shell's echo of the
    command and reported `the console result is not base64`."""
    import inspect

    from tests.vm import cluster

    source = inspect.getsource(cluster.collect)
    assert "expect(CONSOLE_CLOSE" in source
    assert "snapshot(" not in source, "a fixed window cannot tell short from unfinished"


def test_neither_marker_appears_in_the_command_the_shell_echoes() -> None:
    """The shell echoes the line it was given. A reader waiting for the closing
    marker matched the echo and returned before the archive had started, and
    every run failed in ninety seconds with `the console result is not
    base64`."""
    from tests.vm.results import CONSOLE_CLOSE, CONSOLE_OPEN, console_command

    said = console_command("/tmp/results")
    assert CONSOLE_OPEN not in said
    assert CONSOLE_CLOSE not in said


def test_the_command_a_real_shell_runs_produces_a_readable_archive(tmp_path: Path) -> None:
    """Run against a real `sh`, with the echo in front of it the way a console
    carries one: the markers have to survive `printf` and still be found."""
    import subprocess

    from tests.vm.results import LOG_TAIL, console_command, read_console

    (tmp_path / "install.rc").write_bytes(b"0\n")
    (tmp_path / "install.txt").write_bytes(b"installed 53 operations\n")
    command = console_command(str(tmp_path))
    printed = subprocess.run(["sh", "-c", command], capture_output=True).stdout
    said = f"root@livecd ~ # {command}\r\n".encode() + printed
    # A log this size travels whole, and its tail with it: the tail is the
    # fallback for the one fixture whose 80 MB never crossed this channel.
    assert read_console(said) == {
        "install.rc": b"0\n",
        "install.txt": b"installed 53 operations\n",
        LOG_TAIL: b"installed 53 operations\n",
    }


def test_the_installer_does_not_start_before_the_guest_has_a_network() -> None:
    """Reaching a root shell says the medium booted, not that the interface is
    up. Starting there made the installer's own reachability check fail within
    ninety seconds of boot, and the run stopped before the first disk was
    touched."""
    import inspect

    from tests.vm import cluster

    source = inspect.getsource(cluster.install_one)
    ran = source.index("install.sh")
    waited = source.index("wait_for_network")
    assert waited < ran, "the wait has to come before the installer"


def test_the_network_wait_gives_up_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unattended schedule cannot hold a slot for a guest whose interface
    never came up."""
    from tests.vm import cluster
    from tests.vm.console import ConsoleTimeout

    asked: list[str] = []

    class Down:
        def send(self, line: str) -> None:
            asked.append(line)

        def send_raw(self, keys: str) -> None:
            asked.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            return b"NETWORK_DOWN"

        def close(self) -> None:
            pass

    monkeypatch.setattr(cluster, "NETWORK_PATIENCE", 0.3)
    monkeypatch.setattr(cluster, "NETWORK_PAUSE", 0.05)
    link = cluster.Reconnecting(Down, tries=1)
    with pytest.raises(ConsoleTimeout, match="reached no mirror") as refused:
        cluster.wait_for_network(link)
    assert asked, "it asked at least once before giving up"

    # What it held, not `no network`: `vm-gnome` gave up with an address, a
    # default route and three resolvers, and every mirror answered `Could not
    # contact DNS servers`. A verdict that says the guest had no network sends
    # the reader to the segment when the fault is name resolution.
    said = str(refused.value)
    assert "address" in said and "route" in said and "resolver" in said, said


def test_the_network_wait_returns_as_soon_as_the_guest_answers() -> None:
    from tests.vm import cluster

    tries: list[str] = []

    class Late:
        def send(self, line: str) -> None:
            tries.append(line)

        def send_raw(self, keys: str) -> None:
            pass

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            return b"NETWORK_DOWN" if len(tries) < 3 else b"NETWORK_UP"

        def close(self) -> None:
            pass

    link = cluster.Reconnecting(Late, tries=1)
    cluster.wait_for_network(link)
    assert len(tries) == 7
    assert sum(1 for line in tries if "NETWORK_%s" in line) == 2
    assert "REACH" in tries[-1]


def test_the_network_probe_is_sent_again_after_a_reconnect() -> None:
    """A fresh console must receive a probe after the previous read closed."""
    from tests.vm import cluster
    from tests.vm.console import ConsoleClosed, ConsoleTimeout

    opened: list["Probe"] = []

    class Probe:
        def __init__(self, drop: bool) -> None:
            self.drop = drop
            self.sent: list[str] = []

        def send(self, line: str) -> None:
            self.sent.append(line)

        def send_raw(self, keys: str) -> None:
            self.send(keys)

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            probed = any(cluster.NETWORK_PROBE in line for line in self.sent)
            if self.drop and probed:
                self.drop = False
                raise ConsoleClosed("termproxy disconnected")
            if not self.sent:
                raise ConsoleTimeout("the reopened console received no probe")
            if "BEGIN" in pattern:
                return b"MARK_2_BEGIN"
            return b"NETWORK_UP\r\nMARK_2_DONE"

        def close(self) -> None:
            pass

    def open_console() -> Probe:
        one = Probe(drop=not opened)
        opened.append(one)
        return one

    link = cluster.Reconnecting(open_console, tries=2)
    cluster.wait_for_network(link)

    probes = [
        line
        for console in opened
        for line in console.sent
        if cluster.NETWORK_PROBE in line
    ]
    assert len(probes) == 2, probes


def test_a_run_is_not_green_until_the_installed_system_answers() -> None:
    """The install finishing is half the question. A machine can reach a login
    prompt with the wrong filesystem mounted, no fstab and the wrong locale,
    and every check before this one would still be green."""
    import inspect

    from tests.vm import cluster

    source = inspect.getsource(cluster.install_one)
    assert "boot_and_check" in source
    checked = source.index("boot_and_check")

    # One exception, and it has to be the only one: a fixture in
    # `EXPECTED_TO_FAIL` stops the install on purpose, so there is no installed
    # system to read and its verdict is decided before that point. Every other
    # `Verdict.OK` comes after the machine answered.
    exception = source.index("EXPECTED_TO_FAIL")
    early = [
        at
        for at in range(len(source))
        if source.startswith("Verdict.OK", at) and at < checked
    ]
    assert len(early) == 1, early
    assert exception < early[0] < checked, (exception, early, checked)
    assert "Verdict.OK" in source[checked:], "and the ordinary verdict is after it"


def test_installed_boot_attaches_before_reset_without_sending_a_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late termproxy has no GRUB scrollback, while a blank line submitted at
    the hidden passphrase prompt is an empty key rather than a harmless probe."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster

    events: list[str] = []

    class Guest:
        def stop(self) -> None:
            events.append("stop")

        def boot_from_disk(self) -> None:
            events.append("boot-from-disk")

        def start(self) -> None:
            events.append("start")

        def reset(self) -> None:
            events.append("reset")

    class Link:
        def reopen(self, *, solicit_prompt: bool = True) -> None:
            events.append(f"reopen:{solicit_prompt}")

    def unlock(*unused: object) -> cluster.UnlockResult:
        events.append("unlock")
        return cluster.UnlockResult(
            cluster.InstalledBootState.WAIT_LOGIN,
            "stop after observing boot order",
        )

    monkeypatch.setattr(cluster, "_unlock", unlock)
    refused = cluster.boot_and_check(
        cast(Any, Guest()),
        cast(Any, Link()),
        Path("unused"),
        load(Path("tests/fixtures/vm-btrfs.toml")),
    )

    assert refused == "stop after observing boot order"
    assert events == [
        "stop",
        "boot-from-disk",
        "start",
        "reopen:False",
        "reset",
        "unlock",
    ]


def test_a_failed_remote_unlock_answers_with_what_the_console_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`no ssh daemon on port 2222 after 180s` was the whole of what a
    two-hour run produced. The unlock is attempted over the network and returns
    before anything reads the console, so a guest that never left GRUB and one
    whose initramfs came up without an address answered identically."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster

    class Guest:
        def stop(self) -> None:
            return None

        def boot_from_disk(self) -> None:
            return None

        def start(self) -> None:
            return None

        def reset(self) -> None:
            return None

    class Console:
        def __init__(self, screen: bytes) -> None:
            self.screen = screen
            self.asked: list[float] = []

        def snapshot(self, seconds: float) -> bytes:
            self.asked.append(seconds)
            return self.screen

    class Link:
        def __init__(self, screen: bytes) -> None:
            self.console = Console(screen)

        def reopen(self, *, solicit_prompt: bool = True) -> None:
            return None

    def refuse(*unused: object, **ignored: object) -> None:
        raise RuntimeError("no ssh daemon on port 2222 after 180s")

    monkeypatch.setattr(cluster, "remote_unlock", refuse)
    installation = load(Path("tests/fixtures/vm-unlock.toml"))
    assert installation.kernel.remote_unlock.enabled, "the fixture under test"

    stuck_at_grub = Link(b"\x1b[2JGNU GRUB  version 2.14\r\nGentoo Linux\r\n")
    refused = cluster.boot_and_check(
        cast(Any, Guest()),
        cast(Any, stuck_at_grub),
        Path("unused"),
        installation,
        remote_key=Path("unused.key"),
    )
    assert "no ssh daemon on port 2222" in refused
    assert "GNU GRUB" in refused, refused
    assert stuck_at_grub.console.asked == [cluster.UNLOCK_SCREEN_PATIENCE]

    # Enough of it to reach the lines before the last prompt. `vm-unlock` came
    # back holding only `Please enter passphrase for disk root`, which says the
    # initramfs is running and nothing about why its network was not; the lines
    # above it are the ones naming the services that started.
    # The marker sits about 2.5 kB before the prompt, which is where the line
    # that names a service that did or did not start would be. At 400 bytes it
    # is cut and the verdict says only that a passphrase was asked for.
    boot = (
        b"[  OK  ] Started Network Configuration.\r\n"
        + (
            b"[  OK  ] Reached target Preparation for Network.\r\n"
            b"         Starting dracut cmdline hook...\r\n"
        ) * 25
        + b"Please enter passphrase for disk root: "
    )
    assert len(boot) - boot.index(b"Started Network Configuration") > 2000, len(boot)
    verbose = Link(boot)
    said = cluster.boot_and_check(
        cast(Any, Guest()),
        cast(Any, verbose),
        Path("unused"),
        installation,
        remote_key=Path("unused.key"),
    )
    assert "Started Network Configuration" in said, said
    assert "passphrase for disk root" in said, said

    # Negative control: a console that produced nothing says so, rather than
    # an empty pair of quotes that reads as a screen that was read and blank.
    silent = Link(b"")
    assert "the console held nothing" in cluster.boot_and_check(
        cast(Any, Guest()),
        cast(Any, silent),
        Path("unused"),
        installation,
        remote_key=Path("unused.key"),
    )


def test_unlock_reconnect_never_solicits_a_shell_prompt() -> None:
    """There is no shell while GRUB owns the console, so a solicitation is an
    empty passphrase and changes the state the reader is trying to observe."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster
    from tests.vm.console import ConsoleClosed

    opened: list["Console"] = []

    class Console:
        def __init__(self, drop: bool) -> None:
            self.drop = drop
            self.sent: list[str] = []

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            if self.drop:
                self.drop = False
                raise ConsoleClosed("termproxy reset with the guest")
            return b"login:"

        def send(self, line: str) -> None:
            self.sent.append(line)

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def close(self) -> None:
            pass

    def open_console() -> Console:
        console = Console(drop=not opened)
        opened.append(console)
        return console

    class Guest:
        def send_keys(self, keys: list[str]) -> None:
            raise AssertionError(f"unexpected VGA passphrase send: {keys!r}")

    installation = load(Path("tests/fixtures/vm-luks.toml"))
    link = cluster.Reconnecting(open_console, tries=2)
    result = cluster._unlock(Guest(), link, installation)
    assert result == cluster.UnlockResult(cluster.InstalledBootState.LOGIN_READY)
    assert len(opened) == 2
    assert opened[1].sent == [], "a GRUB reconnect submitted an empty passphrase"


def test_every_question_asked_inside_names_what_would_fail_it() -> None:
    """A check with nothing to compare against passes on any machine, which is
    the shape a coverage claim hides behind."""
    from pathlib import Path as _Path

    from gentoo_install.exec.config import load
    from tests.vm.cluster import _asked_for

    # The exemption this test used to carry was the defect: `hostname` and
    # `kernel` were skipped for comparing against nothing, and they were the
    # two a guest could get wrong without failing.
    asked = _asked_for(load(_Path("tests/fixtures/vm-xfs.toml")))
    named = {name for name, _, _ in asked}
    assert {"os-release", "fstab", "locale", "hostname", "root filesystem", "init"} <= named
    for name, command, wanted in asked:
        assert command.strip(), name
        assert wanted, f"{name} compares against nothing"


def test_the_probe_runs_before_the_guest_is_touched() -> None:
    """An interface raised from outside NetworkManager is one it then leaves
    alone: `ip link set up` before the medium had its chance stopped it
    configuring the guest, and `curl` answered `Could not connect to server`
    in ten milliseconds where before it had reached the mirror.

    So the probe goes first and the request follows immediately after it comes
    back empty — not after half the patience, which is what left seventeen of
    twenty-four guests waiting for an address nothing would hand them. The
    request is guarded on there being no IPv4 default route, so a medium whose
    own manager configured one is still left alone.
    """
    import inspect

    from tests.vm import cluster

    code = [
        line
        for line in inspect.getsource(cluster.wait_for_network).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    probe = next(at for at, line in enumerate(code) if "NETWORK_PROBE" in line)
    ask = next(at for at, line in enumerate(code) if "link.run(configure" in line)
    assert probe < ask, "the medium gets its chance before anything is raised"
    assert ask < len(code), "and the address is pinned once the probe answers"
    assert "ip -4 route show default" in cluster.ASK_FOR_IPV4


def test_no_marker_appears_in_the_command_that_prints_it() -> None:
    """The shell echoes the line it was given. A reader waiting for a marker
    matched the echo and returned before the work had started — twice: the
    result archive in ninety seconds, and the network probe on its first pass
    with no address on the interface at all."""
    from tests.vm import cluster
    from tests.vm.console import command_begin, command_done, marked_command
    from tests.vm.results import CONSOLE_CLOSE, CONSOLE_OPEN, console_command

    watched = (
        cluster.NETWORK_UP,
        cluster.NETWORK_DONE,
        CONSOLE_OPEN,
        CONSOLE_CLOSE,
        command_begin(7),
        command_done(7),
    )
    commands = (
        console_command("/tmp/results"),
        cluster.NETWORK_PROBE,
        marked_command("findmnt --output TARGET,SOURCE,FSTYPE", 7),
    )
    for command in commands:
        for marker in watched:
            assert marker not in command, f"{marker} appears whole in {command[:80]!r}"


def test_a_command_answers_with_what_it_printed_and_not_with_its_own_echo() -> None:
    """`findmnt --noheadings --list --output TARGET,SOURCE,FSTYPE` was checked
    for `/`, and the echo of that command carries one. A guest whose `findmnt`
    printed nothing passed the mount check on the echoed line alone.

    Run against a real `sh` with the echo in front of it, the way a console
    carries one.
    """
    import subprocess

    from tests.vm import cluster
    from tests.vm.console import command_begin, command_done, marked_command

    command = marked_command("findmnt --output TARGET,SOURCE,FSTYPE", 7)
    # `sh -x`-free: the echo is what a terminal adds, so it is written here.
    said = subprocess.run(
        ["sh", "-c", f"printf '%s\n' {shlex.quote(command)}; {command}"],
        capture_output=True,
        check=False,
    ).stdout
    answer = said.split(command_begin(7).encode())[-1]
    answer = answer.split(command_done(7).encode())[0]
    assert b"findmnt" not in answer, answer
    assert b"TARGET,SOURCE,FSTYPE" not in answer, answer


def test_the_nodes_are_asked_for_a_medium_in_china_first() -> None:
    """The cluster is in China and a gibibyte from Gentoo's own mirror is slow
    enough to be worth avoiding. `mirrors.ustc.edu.cn` is absent on purpose: it
    answers 403 to wget, which is what Proxmox downloads with."""
    from tests.vm.cluster import MIRRORS

    assert MIRRORS[-1] == "https://distfiles.gentoo.org", "the fallback comes last"
    chinese = [one for one in MIRRORS if one.endswith(".cn/gentoo")]
    assert len(chinese) >= 4, MIRRORS
    assert all(one in MIRRORS[: len(chinese)] for one in chinese), "and they come first"
    assert not any("ustc" in one for one in MIRRORS), "USTC refuses wget"


def test_each_encrypted_boot_path_answers_its_own_number_of_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing answered the prompt, so an encrypted install that had worked
    was failed ten minutes later for not reaching a login it was never going
    to reach unattended.

    One scripted console per encrypted layout, and a BIOS one whose prompt is
    on the VGA console and never reaches the serial port at all.
    """
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from gentoo_install.model.config import Firmware as BootFirmware
    from tests.vm import cluster
    from tests.vm.cluster import Reconnecting
    from tests.vm.console import DISK_PASSPHRASE

    class Scripted:
        """Emit each distinct boot prompt, then hand off the observed login."""

        def __init__(self, prompts: int) -> None:
            self.prompts = prompts
            self.sent: list[str] = []
            self.keys: list[str] = []

        def send(self, line: str) -> None:
            self.sent.append(line)

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            answered = self.sent.count(DISK_PASSPHRASE)
            if answered >= self.prompts:
                return b"gentoo login:"
            if answered == 0:
                return b"Enter passphrase for hd0,gpt2:"
            return b"Please enter passphrase for disk root:"

    class Silent(Scripted):
        """A guest whose keys go through the API, not the serial port."""

        def send_keys(self, keys: list[str]) -> None:
            self.keys.extend(keys)

        def close(self) -> None:
            pass

    for name, firmware, serial_prompts in (
        ("vm-luks", BootFirmware.UEFI, 2),
        ("vm-zfs-encrypted", BootFirmware.UEFI, 1),
        ("vm-bios-luks", BootFirmware.BIOS, 1),
    ):
        installation = load(Path("tests/fixtures") / f"{name}.toml")
        assert installation.bootloader.firmware is firmware, name
        console = Silent(serial_prompts)
        link = Reconnecting(lambda: console, tries=1)
        # No wait for GRUB: the sleep is what the real path spends and this
        # test is not measuring it.
        monkeypatch.setattr(cluster, "GRUB_PROMPT_SECONDS", 0.0)
        result = cluster._unlock(console, link, installation)
        assert result == cluster.UnlockResult(cluster.InstalledBootState.LOGIN_READY), (
            f"{name}: {result}"
        )
        assert console.sent.count(DISK_PASSPHRASE) == serial_prompts, (
            f"{name}: {console.sent}"
        )
        if firmware is BootFirmware.BIOS:
            assert console.keys, f"{name}: nothing was typed at GRUB"
            assert console.keys[-1] == "ret", console.keys

    plain = load(Path("tests/fixtures/vm-binpkg.toml"))
    console = Silent(0)
    assert cluster._unlock(
        console, Reconnecting(lambda: console, tries=1), plain
    ) == cluster.UnlockResult(cluster.InstalledBootState.WAIT_LOGIN)
    assert console.sent == [], "a plain disk was sent a passphrase"


def test_installed_login_uses_the_login_observed_by_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_unlock` consumes through `login:`. Its caller must answer that prompt,
    not wait ten minutes for a second copy which getty does not owe it."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster
    from tests.vm.console import PASSWORD_PROMPT
    from tests.vm.convert import after_the_boot

    events: list[str] = []

    class Guest:
        def stop(self) -> None:
            events.append("stop")

        def boot_from_disk(self) -> None:
            events.append("boot")

        def start(self) -> None:
            events.append("start")

        def reset(self) -> None:
            events.append("reset")

    class Link:
        def reopen(self, *, solicit_prompt: bool = True) -> None:
            events.append(f"reopen:{solicit_prompt}")

        def observe(self, pattern: str, timeout: float, *, solicit: bool = False) -> bytes:
            events.append(f"observe:{pattern}")
            if pattern == PASSWORD_PROMPT:
                return "\u5bc6\u78bc\uff1a".encode()
            return b"root@cryptbox ~ #"

        def respond(self, line: str) -> None:
            events.append(f"respond:{line}")

        def expect_output(self, command: str, timeout: float = 120.0) -> bytes:
            installation = load(Path("tests/fixtures/vm-luks.toml"))
            for _, expected_command, wanted in cluster._asked_for(installation):
                if command == expected_command:
                    return wanted.encode()
            # The prefix report a booted machine is asked. It judges nothing,
            # so any complete line answers it.
            for one in after_the_boot(installation):
                if command == one.command:
                    return (
                        b"grubstub=163840 grubprefix=1 boot=aaaa esp=1234-ABCD stub=aaaa "
                        b"embedded=(hd0,gpt2)/boot/grub drive=hd0,gpt2 fs=ext2\n"
                    )
            raise AssertionError(command)

    monkeypatch.setattr(
        cluster,
        "_unlock",
        lambda *unused: cluster.UnlockResult(cluster.InstalledBootState.LOGIN_READY),
    )
    refused = cluster.boot_and_check(
        cast(Any, Guest()),
        cast(Any, Link()),
        Path("unused"),
        load(Path("tests/fixtures/vm-luks.toml")),
    )

    assert refused == ""
    assert not any(one == "observe:login:" for one in events), events
    assert events.count("respond:root") == 1
    assert events.count(f"respond:{cluster.INSTALLED_PASSWORD}") == 1


@pytest.mark.parametrize("delivery", [None, True])
def test_an_ambiguous_boot_response_is_never_retried(delivery: bool | None) -> None:
    from tests.vm.cluster import Reconnecting
    from tests.vm.console import ConsoleClosed

    opened = 0
    attempts: list[str] = []

    class Ambiguous:
        @property
        def closed(self) -> bool:
            return False

        def send(self, line: str) -> None:
            attempts.append(line)
            raise ConsoleClosed(
                "the connection closed during write",
                write_may_have_reached_guest=delivery,
            )

    def open_console() -> Ambiguous:
        nonlocal opened
        opened += 1
        return Ambiguous()

    link = Reconnecting(cast(Any, open_console), tries=4)
    with pytest.raises(ConsoleClosed):
        link.respond("secret response")
    assert attempts == ["secret response"]
    assert opened == 1, "unknown delivery must not open a channel for a retry"


def test_a_known_undelivered_boot_response_reopens_without_a_blank_line() -> None:
    from tests.vm.cluster import Reconnecting
    from tests.vm.console import ConsoleClosed

    opened: list["Channel"] = []

    class Channel:
        def __init__(self, closed: bool) -> None:
            self.is_closed = closed
            self.sent: list[str] = []

        @property
        def closed(self) -> bool:
            return self.is_closed

        def send(self, line: str) -> None:
            if self.is_closed:
                raise ConsoleClosed("closed", write_may_have_reached_guest=False)
            self.sent.append(line)

        def close(self) -> None:
            pass

    def open_console() -> Channel:
        channel = Channel(closed=not opened)
        opened.append(channel)
        return channel

    link = Reconnecting(cast(Any, open_console), tries=2)
    link.respond("owned response")
    assert len(opened) == 2
    assert opened[0].sent == []
    assert opened[1].sent == ["owned response"]


def test_a_connection_reset_is_a_dropped_console_and_not_a_dead_run() -> None:
    """`WebSocketError` went past every `except ConsoleClosed`, so a TCP reset
    ended two cluster guests at zero minutes with their installs running and
    the schedule recorded `ERROR` for a machine that was fine.

    Driven through the real `Framed.read`, so the transport decides.
    """
    from tests.vm.websocket import WebSocket

    class Resetting:
        """A socket that answers one frame and then resets."""

        def __init__(self) -> None:
            self.reads = 0

        def recv(self, size: int) -> bytes:
            self.reads += 1
            if self.reads == 1:
                # One unmasked text frame carrying `hello`.
                return b"\x81\x05hello"
            raise ConnectionResetError(104, "Connection reset by peer")

        def sendall(self, data: bytes) -> None:
            return None

        def close(self) -> None:
            return None

        def settimeout(self, seconds: float | None) -> None:
            return None

    sock = Resetting()
    framed = WebSocket(sock)
    assert framed.read() == b"hello"
    # Read into locals: mypy narrows a property it has seen tested, and the
    # whole point here is that this one changes.
    before = framed.closed
    assert framed.read() == b""
    after, why = framed.closed, framed.why_closed
    assert (before, after) == (False, True), "a reset has to look like a closed connection"
    assert "reset" in why.lower(), why


def test_a_guest_that_compiles_is_given_a_whole_node() -> None:
    """Every guest got two cores and four gibibytes, so an hour of `emerge`
    ran with the same share as a six-minute binary-package install and the
    cluster sat idle beside a deep queue. What makes a run long is compiling,
    and the configuration is what says whether it does."""
    from tests.vm.cluster import GUEST_CORES, GUEST_MEMORY_MIB, HEAVY_CORES, HEAVY_MEMORY_MIB
    from tests.vm.cluster import fixtures as cluster_fixtures

    weights = {one.name: one for one in cluster_fixtures(
        ["vm-binpkg", "vm-xfs", "vm-zfs", "vm-desktop", "vm-gnome", "ext4-bios"]
    )}
    # `vm-zfs` stood here as a light guest until its own `install.jsonl` was
    # read: a ZFS root compiles its module, ZFSBootMenu and systemd whatever
    # the kernel and the binary host say.
    for name in ("vm-desktop", "vm-gnome", "ext4-bios", "vm-zfs"):
        job = weights[name]
        assert job.heavy, name
        assert (job.cores, job.memory_mib) == (HEAVY_CORES, HEAVY_MEMORY_MIB), name
    for name in ("vm-binpkg", "vm-xfs"):
        job = weights[name]
        assert not job.heavy, name
        assert (job.cores, job.memory_mib) == (GUEST_CORES, GUEST_MEMORY_MIB), name


def test_a_node_with_one_light_slot_left_is_not_given_a_heavy_guest() -> None:
    """A heavy guest asks for more memory, so a slot list built from the light
    size does not answer for it.

    Both sizes come from `sizing.py` rather than from a ratio spelled here: it
    said "twice" and stayed true only while heavy was 8192 against 4096, which
    stopped being so when `vm-gnome` was `Killed` compiling webkit-gtk.
    """
    from tests.vm.cluster import (
        GUEST_MEMORY_MIB,
        HEAVY_MEMORY_MIB,
        NODE_HEADROOM_BYTES,
        room_for,
    )
    from tests.vm.cluster import fixtures as cluster_fixtures
    from tests.vm.proxmox import Node

    light, heavy = cluster_fixtures(["vm-binpkg", "vm-desktop"])
    one_slot = Node(
        name="infra-node1",
        free_bytes=NODE_HEADROOM_BYTES + GUEST_MEMORY_MIB * 1024**2,
        cores=4,
            free_cores=64.0,
    )
    assert room_for(one_slot, light)
    assert not room_for(one_slot, heavy)

    two_slots = Node(
        name="infra-node2",
        free_bytes=NODE_HEADROOM_BYTES + HEAVY_MEMORY_MIB * 1024**2,
        cores=4,
            free_cores=64.0,
    )
    assert room_for(two_slots, heavy)
    assert HEAVY_MEMORY_MIB > GUEST_MEMORY_MIB, (HEAVY_MEMORY_MIB, GUEST_MEMORY_MIB)


def test_a_broken_pipe_is_a_dropped_console_and_not_a_dead_run() -> None:
    """The read side was fixed and the write side was not, so three guests
    were recorded `ERROR ... [Errno 32] Broken pipe` at sixteen minutes with
    their installs running."""
    from tests.vm.websocket import WebSocket

    class Broken:
        def recv(self, size: int) -> bytes:
            return b""

        def sendall(self, data: bytes) -> None:
            raise BrokenPipeError(32, "Broken pipe")

        def close(self) -> None:
            return None

        def settimeout(self, seconds: float | None) -> None:
            return None

    framed = WebSocket(Broken())
    before = framed.closed
    framed.send(b"hello")
    after, why = framed.closed, framed.why_closed
    assert (before, after) == (False, True), "a broken pipe has to look like a closed connection"
    assert "pipe" in why.lower(), why
    # A second write on a closed connection is not an error either.
    framed.send(b"again")


def test_a_command_is_delivered_after_the_console_was_dropped() -> None:
    """A write to a dropped connection is discarded rather than raised, which
    is what the transport should do — and it left the command unsent:
    `wait_for_network` put its probe into a closed socket and then waited
    fifteen minutes for output that was never going to come. Eight guests
    failed that way in one round."""
    from tests.vm.cluster import Reconnecting

    opened: list["Dropping"] = []

    class Dropping:
        """Closed until it is opened again, like a console after a reset."""

        def __init__(self, alive: bool) -> None:
            self.alive = alive
            self.sent: list[str] = []

        def send(self, line: str) -> None:
            if self.alive:
                self.sent.append(line)

        def send_raw(self, keys: str) -> None:
            self.send(keys)

        def snapshot(self, seconds: float) -> bytes:
            return b""

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return not self.alive

        def close(self) -> None:
            pass

    def open_console() -> Dropping:
        one = Dropping(alive=len(opened) > 0)
        opened.append(one)
        return one

    link = Reconnecting(open_console, tries=4)
    link.send("probe me")
    assert len(opened) == 2, "the closed console has to be replaced before the write"
    # The command and nothing else. This asserted `["", "probe me"]` while a
    # reopen before a write also asked for a prompt, and that empty line is
    # what agetty counted as a login attempt: `vm-lvm` and `openrc-sdboot`
    # failed in three rounds with the password arriving one prompt late.
    assert opened[1].sent == ["probe me"]
    assert opened[0].sent == [], "nothing goes into the dropped one"


def test_removing_a_guest_needs_the_range_the_tag_and_this_run_s_own_mark() -> None:
    """The tag is ours to write, so it is not evidence on its own: a machine
    outside the range that carries it would have been deleted, and two
    campaigns that picked the same free VMID in the same second could each
    delete the other's guest.

    This token administers a cluster running other people's work.
    """
    from typing import Any

    from tests.vm.proxmox import TAG, VMID_FIRST, Api, Guest, GuestSpec, ProxmoxError

    class Answering(Api):
        def __init__(self, tags: str) -> None:
            self.tags = tags
            self.deleted: list[int] = []

        def call(self, method: str, path: str, **form: Any) -> Any:
            if method == "GET" and path.endswith("/config"):
                if self.deleted:
                    raise ProxmoxNotFound("the guest does not exist")
                return {"tags": self.tags}
            if method == "GET" and path.endswith("/status/current"):
                return {"status": "stopped"}
            if method == "DELETE":
                self.deleted.append(int(path.rsplit("/", 1)[1]))
            return "UPID:x"

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            return None

    def guest(api: Api, vmid: int, nonce: str) -> Guest:
        return Guest(
            api=api,
            node="infra-node1",
            vmid=vmid,
            spec=GuestSpec(name="x", iso="x", nonce=nonce),
        )

    # Outside the range, however it is tagged.
    api = Answering(f"{TAG};gi-abc")
    with pytest.raises(ProxmoxError, match="outside"):
        guest(api, 9002, "gi-abc").destroy()
    assert api.deleted == []

    # In range and tagged, but built by another run.
    api = Answering(f"{TAG};gi-someone-else")
    with pytest.raises(ProxmoxError, match="not the guest this run built"):
        guest(api, VMID_FIRST, "gi-abc").destroy()
    assert api.deleted == []

    # In range, tagged, and ours.
    api = Answering(f"{TAG};gi-abc")
    guest(api, VMID_FIRST, "gi-abc").destroy()
    assert api.deleted == [VMID_FIRST]


def test_every_cluster_guest_is_built_with_a_mark_of_its_own() -> None:
    """A guest with no nonce falls back to the tag alone, which is the state
    this rule exists to leave behind."""
    import inspect

    from tests.vm import cluster

    source = inspect.getsource(cluster.install_one)
    assert "nonce=" in source, "the campaign has to mark the guests it builds"


def test_a_create_held_off_by_the_storage_lock_is_tried_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thirteen guests built at once contend on `ceph-pve`, and one round lost
    a fixture to `cfs-lock 'storage-ceph-pve' error: got lock request
    timeout`. The lock belongs to another create and is gone in seconds; a
    create that fails for any other reason still stops the run."""
    from typing import Any

    from tests.vm import proxmox
    from tests.vm.proxmox import Api, Guest, GuestSpec, ProxmoxError

    class Contended(Api):
        def __init__(self, refusals: int, message: str) -> None:
            self.refusals = refusals
            self.message = message
            self.attempts = 0

        def call(self, method: str, path: str, **form: Any) -> Any:
            if method == "POST" and path.endswith("/qemu"):
                self.attempts += 1
            return "UPID:x"

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            if self.attempts <= self.refusals:
                raise ProxmoxError(self.message)

    lock = "ended with \"unable to create VM 9313 - cfs-lock 'storage-ceph-pve' error: got lock request timeout\""
    monkeypatch.setattr(proxmox, "CREATE_PAUSE", 0.0)
    api = Contended(refusals=2, message=lock)
    Guest(api=api, node="infra-node1", vmid=9300, spec=GuestSpec(name="x", iso="x")).create()
    assert api.attempts == 3, api.attempts

    # Anything else stops at once: a full storage is not a lock.
    other = Contended(refusals=1, message="ended with 'no space left on device'")
    with pytest.raises(ProxmoxError, match="no space"):
        Guest(api=other, node="infra-node1", vmid=9300, spec=GuestSpec(name="x", iso="x")).create()
    assert other.attempts == 1, other.attempts


def test_a_create_conflict_never_cleans_up_the_conflicting_vmid() -> None:
    """Cleanup used the requested VMID even though create proved another guest owned it."""

    class Conflicting(Api):
        def __init__(self) -> None:
            self.asked: list[tuple[str, str]] = []

        def call(self, method: str, path: str, **form: Any) -> Any:
            self.asked.append((method, path))
            raise ProxmoxError("VM 9300 already exists on node 'other'")

    api = Conflicting()
    guest = Guest(
        api,
        "infra-node1",
        9300,
        GuestSpec(name="x", iso="x", nonce="gi-conflict"),
    )
    with pytest.raises(CreateConflict):
        guest.create()
    guest.destroy()
    assert api.asked == [("POST", "/nodes/infra-node1/qemu")]


def test_two_campaigns_reserve_distinct_vmids_before_dispatch(tmp_path: Path) -> None:
    """Both campaigns first read 9300 as free; only its creator dispatches it."""
    import threading

    from tests.vm.cluster import Job, _read_lease, _reserve_job

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    owners: dict[int, str] = {}
    attempted: dict[str, list[int]] = {"first": [], "second": []}

    class Campaign(Api):
        def __init__(self, name: str) -> None:
            self.name = name
            self.probes = 0
            self.controls: list[str] = []

        def free_vmid(self, held: frozenset[int] = frozenset()) -> int:
            with lock:
                candidate = next(
                    vmid
                    for vmid in range(9300, 9400)
                    if vmid not in owners and vmid not in held
                )
                self.probes += 1
                first_probe = self.probes == 1
            if first_probe:
                barrier.wait()
            return candidate

        def call(self, method: str, path: str, **form: Any) -> Any:
            if method == "POST" and path.endswith("/qemu"):
                vmid = int(form["vmid"])
                attempted[self.name].append(vmid)
                with lock:
                    if vmid in owners:
                        raise ProxmoxError(f"VM {vmid} already exists")
                    owners[vmid] = self.name
                return f"UPID:{self.name}"
            self.controls.append(f"{method} {path}")
            raise AssertionError(f"unexpected cluster operation: {method} {path}")

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            return None

    jobs: dict[str, Job] = {}

    def reserve(name: str) -> None:
        api = Campaign(name)
        jobs[name] = _reserve_job(
            api,
            "infra-node1",
            Job(name=name, fixture=tmp_path / f"{name}.toml", iso="minimal.iso"),
            "driver.iso",
            tmp_path / name,
            frozenset(),
        )
        assert api.controls == []

    threads = [threading.Thread(target=reserve, args=(name,)) for name in attempted]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert set(jobs) == set(attempted)
    assert {job.vmid for job in jobs.values()} == {9300, 9301}
    winner = owners[9300]
    loser = next(name for name in attempted if name != winner)
    assert attempted[winner] == [9300]
    assert attempted[loser] == [9300, 9301]
    assert jobs[winner].vmid == 9300
    assert jobs[loser].vmid == 9301
    for job in jobs.values():
        assert job.execution is not None
        guest = cast(Guest, job.execution.guest)
        assert job.execution.created
        assert (job.node, job.vmid) == (guest.node, guest.vmid)
        assert job.lease is not None
        lease = _read_lease(job.lease)
        assert lease is not None
        assert (lease.node, lease.vmid, lease.nonce) == (
            guest.node,
            guest.vmid,
            guest.spec.nonce,
        )


def test_a_reserved_guest_is_not_created_twice_and_is_still_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.vm.cluster import Job, Running, Watchdog, install_one

    class Recording(Api):
        def __init__(self) -> None:
            self.creates = 0

        def call(self, method: str, path: str, **form: Any) -> Any:
            if method == "POST" and path.endswith("/qemu"):
                self.creates += 1
                return "UPID:created"
            raise AssertionError(f"unexpected cluster operation: {method} {path}")

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            return None

    api = Recording()
    guest = Guest(
        api,
        "infra-node1",
        9300,
        GuestSpec(name="reserved", iso="minimal.iso", nonce="gi-reserved"),
    )
    guest.create()
    removed: list[int] = []

    def fail_start() -> None:
        raise ProxmoxError("start failed")

    def destroy() -> None:
        removed.append(guest.vmid)

    monkeypatch.setattr(guest, "start", fail_start)
    monkeypatch.setattr(guest, "destroy", destroy)
    log = tmp_path / "reserved.log"
    job = Job(name="reserved", fixture=tmp_path / "reserved.toml", iso="minimal.iso")
    execution = Running(
        guest,
        Watchdog(log=log, counters=lambda: Traffic(0, 0, 0.0)),
        job.reservation_bytes,
        created=True,
    )
    outcome = install_one(
        api,
        guest.node,
        job,
        "driver.iso",
        tmp_path,
        execution=execution,
    )

    assert outcome.detail == "start failed"
    assert api.creates == 1
    assert removed == [9300]


def test_an_ordinary_reservation_failure_is_immediate_and_writes_no_lease(
    tmp_path: Path,
) -> None:
    from tests.vm.cluster import Job, _reserve_job

    class Full(Api):
        def __init__(self) -> None:
            self.creates = 0

        def free_vmid(self, held: frozenset[int] = frozenset()) -> int:
            return 9300

        def call(self, method: str, path: str, **form: Any) -> Any:
            self.creates += 1
            raise ProxmoxError("no space left on device")

    api = Full()
    with pytest.raises(ProxmoxError, match="no space"):
        _reserve_job(
            api,
            "infra-node1",
            Job(name="full", fixture=tmp_path / "full.toml", iso="minimal.iso"),
            "driver.iso",
            tmp_path,
            frozenset(),
        )

    assert api.creates == 1
    assert not (tmp_path / "leases").exists()


def test_reservation_bookkeeping_failure_removes_the_created_guest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.vm import cluster
    from tests.vm.cluster import Job, _reserve_job

    class Recording(Api):
        def __init__(self) -> None:
            self.creates = 0

        def free_vmid(self, held: frozenset[int] = frozenset()) -> int:
            return 9300

        def call(self, method: str, path: str, **form: Any) -> Any:
            self.creates += 1
            return "UPID:created"

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            return None

    removed: list[int] = []
    monkeypatch.setattr(
        Guest, "destroy", lambda guest: removed.append(guest.vmid)
    )

    def fail_lease(workdir: Path, lease: object) -> Path:
        raise OSError("lease storage is read-only")

    monkeypatch.setattr(cluster, "_write_lease", fail_lease)
    api = Recording()
    with pytest.raises(OSError, match="read-only"):
        _reserve_job(
            api,
            "infra-node1",
            Job(name="lease", fixture=tmp_path / "lease.toml", iso="minimal.iso"),
            "driver.iso",
            tmp_path,
            frozenset(),
        )

    assert api.creates == 1
    assert removed == [9300]


def test_create_conflict_reservations_refresh_until_the_attempt_bound(
    tmp_path: Path,
) -> None:
    from tests.vm.cluster import RESERVATION_TRIES, Job, _reserve_job

    class Conflicting(Api):
        def __init__(self) -> None:
            self.candidates: list[int] = []

        def free_vmid(self, held: frozenset[int] = frozenset()) -> int:
            return next(vmid for vmid in range(9300, 9400) if vmid not in held)

        def call(self, method: str, path: str, **form: Any) -> Any:
            self.candidates.append(int(form["vmid"]))
            raise ProxmoxError(f"VM {form['vmid']} already exists")

    api = Conflicting()
    with pytest.raises(CreateConflict):
        _reserve_job(
            api,
            "infra-node1",
            Job(name="busy", fixture=tmp_path / "busy.toml", iso="minimal.iso"),
            "driver.iso",
            tmp_path,
            frozenset(),
        )

    assert api.candidates == list(range(9300, 9300 + RESERVATION_TRIES))
    assert not (tmp_path / "leases").exists()


def test_transient_task_status_failures_do_not_abort_a_completed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two temporary 502 responses discarded a task that later reported OK."""
    answers: list[Any] = [
        ProxmoxTransientError("GET status answered 502 Bad Gateway"),
        ProxmoxTransientError("GET status answered 502 Bad Gateway"),
        {"status": "stopped", "exitstatus": "OK"},
    ]

    class Tasks(Api):
        def __init__(self) -> None:
            pass

        def call(self, method: str, path: str, **form: Any) -> Any:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    import time as clock

    monkeypatch.setattr(clock, "sleep", lambda seconds: None)
    Tasks().wait("wrong-node", "UPID:right-node:task", patience=10.0)
    assert not answers


def test_install_media_download_carries_the_signed_sha512() -> None:
    """A replaced ISO was accepted because the node received no checksum."""

    class Downloads(Api):
        def __init__(self) -> None:
            self.form: dict[str, Any] = {}

        def stale_drivers(self, node: str, keep: str, older_than: float) -> list[str]:
            return []

        def isos(self, node: str) -> list[str]:
            return []

        def call(self, method: str, path: str, **form: Any) -> Any:
            self.form = form
            return "UPID:node:download"

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            return None

    api = Downloads()
    sha512 = "a" * 128
    api.fetch_iso("node", "https://mirror.invalid/install.iso", "install-a.iso", sha512)
    assert api.form["checksum"] == sha512
    assert api.form["checksum-algorithm"] == "sha512"


def test_install_media_cache_name_contains_its_signed_digest() -> None:
    """Two different images with one upstream filename otherwise reused one remote ISO."""
    from tests.vm.cluster import _medium_name

    first = _medium_name("install-amd64-minimal.iso", "a" * 128)
    second = _medium_name("install-amd64-minimal.iso", "b" * 128)
    assert first != second
    assert first.endswith(".iso") and second.endswith(".iso")


def test_an_existing_install_iso_needs_its_matching_verification_record(
    tmp_path: Path,
) -> None:
    """A same-name remote ISO is replaced when its record is missing."""
    from tests.vm.cluster import prepare
    from tests.vm.driver import digest as driver_digest

    class Existing(Api):
        def __init__(self) -> None:
            self.removed: list[str] = []
            self.fetched: list[str] = []

        def stale_drivers(self, node: str, keep: str, older_than: float) -> list[str]:
            return []

        def isos(self, node: str) -> list[str]:
            return ["minimal-a.iso", "driver.iso"]

        def upload_iso(self, node: str, path: Path, name: str) -> str:
            return name

        def remove_iso(self, node: str, name: str) -> str:
            self.removed.append(name)
            return ""

        def fetch_iso(self, node: str, url: str, filename: str, sha512: str) -> None:
            self.fetched.append(filename)

    api = Existing()
    driver = tmp_path / "driver.iso"
    driver.write_bytes(b"driver")
    prepare(
        api,
        "node",
        "minimal-a.iso",
        ("https://mirror.invalid/install.iso",),
        "a" * 128,
        tmp_path / "trust",
        driver,
        "driver.iso",
    )
    assert api.removed == ["minimal-a.iso", "driver.iso"]
    assert api.fetched == ["minimal-a.iso"]


def test_an_existing_install_iso_replaces_a_mismatched_record(tmp_path: Path) -> None:
    from tests.vm.cluster import prepare
    from tests.vm.driver import digest as driver_digest

    class Existing(Api):
        def __init__(self) -> None:
            self.removed: list[str] = []
            self.fetched: list[str] = []

        def stale_drivers(self, node: str, keep: str, older_than: float) -> list[str]:
            return []

        def isos(self, node: str) -> list[str]:
            return ["minimal-a.iso", "driver.iso"]

        def upload_iso(self, node: str, path: Path, name: str) -> str:
            return name

        def remove_iso(self, node: str, name: str) -> str:
            self.removed.append(name)
            return ""

        def fetch_iso(self, node: str, url: str, filename: str, sha512: str) -> None:
            self.fetched.append(filename)

    driver = tmp_path / "driver.iso"
    driver.write_bytes(b"driver")
    trust = tmp_path / "trust" / "remote" / "node"
    trust.mkdir(parents=True)
    (trust / "minimal-a.iso.sha512").write_text("b" * 128)
    (trust / "driver.iso.sha256").write_text(driver_digest(driver))
    api = Existing()
    prepare(
        api,
        "node",
        "minimal-a.iso",
        ("https://mirror.invalid/install.iso",),
        "a" * 128,
        tmp_path / "trust",
        driver,
        "driver.iso",
    )
    assert api.removed == ["minimal-a.iso"]
    assert api.fetched == ["minimal-a.iso"]


def test_iso_listing_without_checksum_does_not_mark_recorded_media_corrupt(
    tmp_path: Path,
) -> None:
    from tests.vm.cluster import prepare
    from tests.vm.driver import digest as driver_digest

    listing = [
        {
            "volid": "local:iso/install-a.iso",
            "format": "iso",
            "content": "iso",
            "size": 123,
            "ctime": 456,
        },
        {
            "volid": "local:iso/driver.iso",
            "format": "iso",
            "content": "iso",
            "size": 3,
            "ctime": 456,
        },
    ]

    class Listing(Api):
        def call(self, method: str, path: str, **form: Any) -> Any:
            return listing

        def remove_iso(self, node: str, name: str) -> str:
            raise AssertionError("recorded media should not be removed")

        def fetch_iso(self, node: str, url: str, filename: str, sha512: str) -> None:
            raise AssertionError("recorded media should not be fetched")

        def upload_iso(self, node: str, path: Path, name: str) -> str:
            raise AssertionError("recorded media should not be uploaded")

    driver = tmp_path / "driver.iso"
    driver.write_bytes(b"drv")
    stamps = tmp_path / "trust" / "remote" / "node"
    stamps.mkdir(parents=True)
    (stamps / "install-a.iso.sha512").write_text("a" * 128)
    (stamps / "driver.iso.sha256").write_text(driver_digest(driver))
    prepare(
        Listing(),
        "node",
        "install-a.iso",
        ("https://mirror.invalid/install.iso",),
        "a" * 128,
        tmp_path / "trust",
        driver,
        "driver.iso",
    )


def test_an_invalid_install_media_digest_signature_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsigned checksum would let a mirror replace both ISO and digest."""
    import subprocess

    from tests.vm import cluster

    digests = tmp_path / "install.iso.DIGESTS"
    key = tmp_path / "release.gpg"
    digests.write_text(f"# SHA512 HASH\n{'a' * 128}  install.iso\n")
    key.write_bytes(b"key")
    answers = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "gpg: good-looking text without status", ""),
        ]
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: next(answers))
    with pytest.raises(ProxmoxError, match="does not verify"):
        cluster.verify_release_signature(digests, key, tmp_path / "gnupg")


def test_cleanup_retries_unknown_stop_and_delete_until_the_guest_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost stop reply and one failed delete left a running guest behind."""

    class Uncertain(Api):
        def __init__(self) -> None:
            self.running = True
            self.absent = False
            self.stops = 0
            self.deletes = 0

        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/config"):
                if self.absent:
                    raise ProxmoxNotFound("the guest does not exist")
                return {"tags": f"{TAG};gi-owned"}
            if path.endswith("/status/current"):
                return {"status": "running" if self.running else "stopped"}
            if path.endswith("/status/stop"):
                self.stops += 1
                self.running = False
                return "UPID:node:stop"
            if method == "DELETE":
                self.deletes += 1
                if self.deletes == 1:
                    raise ProxmoxTransientError("DELETE answered 503 storage busy")
                self.absent = True
                return "UPID:node:delete"
            raise AssertionError((method, path))

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            if upid.endswith(":stop") and self.stops == 1:
                raise ProxmoxError("task status did not answer")

    import time as clock

    monkeypatch.setattr(clock, "sleep", lambda seconds: None)
    api = Uncertain()
    Guest(
        api,
        "node",
        9300,
        GuestSpec(name="x", iso="x", nonce="gi-owned"),
    ).destroy(patience=10.0)
    assert api.stops == 1
    assert api.deletes == 2
    assert api.absent


def test_stopping_an_absent_guest_is_idempotent() -> None:
    class Absent(Api):
        def call(self, method: str, path: str, **form: Any) -> Any:
            raise ProxmoxNotFound("the guest does not exist")

    guest = Guest(Absent(), "node", 9300, GuestSpec(name="x", iso="x"))
    guest._booted = True
    guest.stop()
    assert not guest._booted


def test_stopping_a_stopped_guest_is_idempotent() -> None:
    class Stopped(Api):
        def call(self, method: str, path: str, **form: Any) -> Any:
            assert method == "GET"
            return {"status": "stopped"}

    guest = Guest(Stopped(), "node", 9300, GuestSpec(name="x", iso="x"))
    guest._booted = True
    guest.stop()
    assert not guest._booted


def test_a_status_failure_does_not_mark_the_guest_stopped() -> None:
    class Refusing(Api):
        def call(self, method: str, path: str, **form: Any) -> Any:
            raise ProxmoxError("status failed")

    guest = Guest(Refusing(), "node", 9300, GuestSpec(name="x", iso="x", nonce="gi-owned"))
    guest._booted = True
    with pytest.raises(ProxmoxError, match="status failed"):
        guest.stop()
    assert guest._booted


def test_a_stop_task_failure_does_not_mark_the_guest_stopped() -> None:
    class Refusing(Api):
        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/status/current"):
                return {"status": "running"}
            # The ownership check reads this before anything is stopped.
            if path.endswith("/config"):
                return {"tags": f"{TAG};gi-owned"}
            return "UPID:node:stop"

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            raise ProxmoxError("stop task failed")

    guest = Guest(Refusing(), "node", 9300, GuestSpec(name="x", iso="x", nonce="gi-owned"))
    guest._booted = True
    with pytest.raises(ProxmoxError, match="stop task failed"):
        guest.stop(patience=0.0)
    assert guest._booted


def test_a_refused_stop_is_reported_before_the_guest_is_destroyed() -> None:
    @dataclass
    class Refusing(Api):
        absent: bool = False

        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/config"):
                if self.absent:
                    raise ProxmoxNotFound("the guest does not exist")
                return {"tags": f"{TAG};gi-owned"}
            if path.endswith("/status/current"):
                return {"status": "running"}
            if path.endswith("/status/stop"):
                raise ProxmoxError("the stop was refused")
            if method == "DELETE":
                self.absent = True
                return "UPID:node:delete"
            raise AssertionError((method, path))

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            if upid.endswith(":delete"):
                raise ProxmoxNotFound("the guest does not exist")
            return None

    api = Refusing()
    guest = Guest(
        api,
        "node",
        9300,
        GuestSpec(name="x", iso="x", nonce="gi-owned"),
    )
    guest._booted = True
    with pytest.raises(ProxmoxError, match="the stop was refused"):
        guest.stop()
    assert guest._booted
    guest.destroy(patience=1.0)

    assert api.absent


def test_interleaved_consoles_use_the_cookie_from_their_own_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket B replaces simulated shared state before ticket A connects."""
    captured: dict[int, str] = {}
    first_ticket = Event()
    second_ticket = Event()

    class Tickets(Api):
        def __init__(self) -> None:
            self.host = "pve.invalid"

        def call_with_affinity(self, method: str, path: str, **form: Any) -> tuple[Any, str]:
            vmid = int(path.split("/qemu/", 1)[1].split("/", 1)[0])
            cookie = f"INGRESSCOOKIE=worker-{vmid}"
            if vmid == 9300:
                setattr(self, "affinity", cookie)
                first_ticket.set()
                assert second_ticket.wait(timeout=5.0)
            else:
                assert first_ticket.wait(timeout=5.0)
                setattr(self, "affinity", cookie)
                second_ticket.set()
            ticket = {"port": vmid, "ticket": f"ticket-{vmid}", "user": "root@pam"}
            return ticket, cookie

    class Framed:
        closed = False

        def settimeout(self, seconds: float | None) -> None:
            return None

        def send(self, payload: bytes, opcode: int = 0x2) -> None:
            return None

        def read(self) -> bytes:
            return b""

        def close(self) -> None:
            return None

    def connect(
        host: str,
        path: str,
        headers: dict[str, str],
        **kwargs: Any,
    ) -> Framed:
        vmid = int(path.split("/qemu/", 1)[1].split("/", 1)[0])
        captured[vmid] = headers.get("Cookie", "")
        return Framed()

    monkeypatch.setattr(WebSocket, "connect", connect)
    monkeypatch.setattr(proxmox, "_secret", lambda: "secret")
    api = Tickets()
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(proxmox.ConsoleChannel.open, api, "node", 9300, 1)
        second = workers.submit(proxmox.ConsoleChannel.open, api, "node", 9301, 1)
        first.result()
        second.result()

    assert captured == {
        9300: "INGRESSCOOKIE=worker-9300",
        9301: "INGRESSCOOKIE=worker-9301",
    }


def test_a_guest_is_asked_for_an_address_on_the_first_pass() -> None:
    """This cluster's guest network carries a ULA IPv6 prefix advertised by
    something that is not the hypervisor and offers no way out of itself. The
    addresses that reach a mirror come from DHCPv4, and nothing asked for one
    until half the patience had gone — and then only when NetworkManager was
    absent. Seventeen of twenty-four guests in one round waited out the whole
    fifteen minutes for an address nothing was going to hand them.

    Measured: `dhcpcd -4 ens18` answered `leased 10.31.0.230` with `default
    via 10.31.0.254`, and `curl` returned 200 immediately after.
    """
    import inspect

    from tests.vm import cluster

    assert "dhcpcd -4" in cluster.ASK_FOR_IPV4, cluster.ASK_FOR_IPV4
    # Only when there is none: a medium whose own manager configured a route
    # is left alone, which is what stopped the ones that do have a manager.
    assert "ip -4 route show default" in cluster.ASK_FOR_IPV4
    assert "NetworkManager" not in cluster.ASK_FOR_IPV4, "the manager is not the question"

    source = inspect.getsource(cluster.wait_for_network)
    assert "NETWORK_PATIENCE / 2" not in source, "the request is not delayed any more"
    assert "ASK_FOR_IPV4" in source


def test_the_keymap_question_is_answered_rather_than_waited_out() -> None:
    """The official minimal medium asks `Load keymap (Enter for default):` and
    waits for a key. Nothing answered it: two guests on one round sat there
    while the run spent its patience waiting for a prompt that was one
    keystroke away."""
    from tests.vm.cluster import Reconnecting, reach_prompt

    class Asking:
        """Asks once, then gives a prompt."""

        def __init__(self) -> None:
            self.sent: list[str] = []
            self.asked = False

        def send(self, line: str) -> None:
            self.sent.append(line)

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return b""

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            if not self.asked:
                self.asked = True
                return b"Load keymap (Enter for default): "
            return b"livecd ~ # "

        @property
        def closed(self) -> bool:
            return False

        def close(self) -> None:
            pass

    console = Asking()
    reach_prompt(Reconnecting(lambda: console, tries=1), patience=30.0)
    assert console.sent == [""], console.sent

    class Ready:
        """Gives a prompt straight away, and must not be sent anything."""

        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, line: str) -> None:
            self.sent.append(line)

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return b""

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            return b"livecd ~ # "

        @property
        def closed(self) -> bool:
            return False

        def close(self) -> None:
            pass

    quiet = Ready()
    reach_prompt(Reconnecting(lambda: quiet, tries=1), patience=30.0)
    assert quiet.sent == [], quiet.sent


def test_slots_are_offered_one_node_at_a_time() -> None:
    """A node's whole share came before the next node was touched, so five
    guests went onto `infra-node5` and left the other five idle: one node
    carried every build, and its four cores and the shared storage lock were
    contended by all of them."""
    from typing import Any

    from tests.vm.cluster import GUEST_MEMORY_MIB, NODE_HEADROOM_BYTES, free_slots
    from tests.vm.proxmox import Api, Node

    def node(name: str, guests: int) -> Node:
        return Node(
            name=name,
            free_bytes=NODE_HEADROOM_BYTES + guests * GUEST_MEMORY_MIB * 1024**2,
            cores=4,
            free_cores=64.0,
        )

    class Cluster(Api):
        def __init__(self) -> None:
            pass

        def nodes(self) -> list[Node]:
            return [node("a", 3), node("b", 1), node("c", 2)]

    order = [one.name for one in free_slots(Cluster())]
    assert order == ["a", "b", "c", "a", "c", "a"], order


def test_the_dhcp_request_skips_the_arp_probe_and_waits_long_enough() -> None:
    """The server offers an address within a second and the handshake then
    stalls in `probing address 10.31.0.201/24` until dhcpcd gives up, so a
    guest that had been offered a lease still came up with nothing. Measured
    on two nodes: `-w -t 25` timed out on both, `--noarp -w -t 90` leased
    10.31.0.203 and 10.31.0.201 with `default via 10.31.0.254`.

    The interface is named, too: a bare `dhcpcd -4 -w` returned at once when
    one was already running, so the wait was never taken.
    """
    from tests.vm.cluster import ASK_FOR_IPV4

    assert "--noarp" in ASK_FOR_IPV4, ASK_FOR_IPV4
    assert "-t 45" in ASK_FOR_IPV4, ASK_FOR_IPV4
    assert '"$dev"' in ASK_FOR_IPV4, "the interface has to be named"
    assert "ip -4 route show default" in ASK_FOR_IPV4, "and only when there is none"


def test_the_guest_asks_for_an_address_on_every_pass() -> None:
    """This network's DHCP server runs on a Raspberry Pi that also routes, and
    it answers intermittently: the same node offered 10.31.0.201 on one
    attempt and printed `soliciting a DHCP lease` then `timed out` on the
    next, minutes apart. Asking once meant a guest that hit a quiet moment
    spent its whole window with no address.

    A daemon left by an earlier attempt is stopped first, because dhcpcd that
    finds one running prints `sending commands to dhcpcd process` and returns
    at once — which is why the log showed the marker pair with no delay.
    """
    import inspect

    from tests.vm import cluster

    assert "dhcpcd -x" in cluster.ASK_FOR_IPV4, cluster.ASK_FOR_IPV4
    assert "--noarp" in cluster.ASK_FOR_IPV4
    source = inspect.getsource(cluster.wait_for_network)
    assert "if not asked:" not in source, "the request is no longer a one-shot"
    assert source.count("ASK_FOR_IPV4") == 1
    # And the burst is spread: thirteen leases inside one minute is what the
    # server could not answer.
    assert cluster.STAGGER >= 20.0, cluster.STAGGER


def test_a_guest_is_given_an_address_rather_than_asking_for_one() -> None:
    """The DHCP server on this network runs on a Raspberry Pi that also routes
    and answers intermittently under load: the same node offered 10.31.0.201
    on one attempt and printed `soliciting a DHCP lease` then `timed out` on
    the next. Thirteen guests asking at once is what it could not serve.

    Each guest takes an address derived from its VMID instead, after checking
    that nothing on the segment already answers to it. Measured on a guest:
    10.31.0.108 with `default via 10.31.0.254`, and the mirror answered 200.
    """
    from tests.vm.cluster import (
        GUEST_GATEWAY,
        GUEST_RESOLVERS,
        configure_statically,
        static_address,
        use_our_resolvers,
    )
    from tests.vm.proxmox import VMID_FIRST, VMID_LAST

    assert static_address(VMID_FIRST) == "10.31.0.150"
    assert static_address(VMID_LAST) == "10.31.0.249"
    # One address per guest, and no two the same.
    every = {static_address(one) for one in range(VMID_FIRST, VMID_LAST + 1)}
    assert len(every) == VMID_LAST - VMID_FIRST + 1

    command = configure_statically("10.31.0.113")
    assert "\n" not in command, "one line: this goes to a serial console"
    assert "arping -D" not in command
    assert "addr add 10.31.0.113/24" in command
    assert f"via {GUEST_GATEWAY}" in command
    resolvers = use_our_resolvers()
    assert "\n" not in resolvers, "one line: this goes to a serial console"
    for one in GUEST_RESOLVERS:
        assert f"nameserver {one}" in resolvers


def test_two_workers_reserve_different_static_addresses(tmp_path: Path) -> None:
    """The interleaving is forced rather than hoped for: the occupancy probe
    holds the first caller inside the critical section long enough for the
    second to reach it, so an unlocked pool hands both the same address and
    the assertion fails. Racing two plain threads passed either way.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    from tests.vm.cluster import AddressPool

    entered = threading.Event()

    def probe(address: str) -> bool:
        if not entered.is_set():
            entered.set()
            time.sleep(0.3)
        return False

    pool = AddressPool(tmp_path, probe)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(pool.reserve, "10.31.0.150")
        entered.wait(timeout=5)
        second = workers.submit(pool.reserve, "10.31.0.150")
        addresses = [first.result(timeout=30), second.result(timeout=30)]

    assert set(addresses) == {"10.31.0.150", "10.31.0.151"}


def test_a_guest_leaves_its_own_address_alone_on_the_next_pass() -> None:
    """The request runs on every pass, so the second one probed the address
    the first had taken: `arping -D` answered that something holds it — this
    guest — and the DHCP branch then tore the working configuration down.
    Four guests spent their whole window doing that to themselves.

    Measured from a probe on the segment: 10.31.0.101, .105, .106, .110, .111
    and .120 all answered as taken, and every one of them was a guest of the
    round that was running.
    """
    import subprocess

    from tests.vm.cluster import configure_statically

    command = configure_statically("10.31.0.113")
    assert subprocess.run(["bash", "-n", "-c", command], capture_output=True).returncode == 0
    # No branch that can tear a working configuration down, and every step
    # idempotent, because this now runs on a guest that already has a route.
    # A DHCP client is killed outright, never asked to release: a release
    # deconfigures the interface, which is what emptied the routing table.
    assert "pkill -KILL" in command
    assert "dhcpcd -" not in command and "--release" not in command
    assert command.count("addr add") == 1
    assert "|| true" in command
    # `exit` would end the login shell this runs in, not just the command.
    assert "exit 0" not in command


def test_the_address_range_avoids_the_machines_already_on_the_segment() -> None:
    """A probe found 10.31.0.106 through .115 answering with locally
    administered MAC addresses — other people's machines on this cluster — so
    four guests a round took an address one of them already held. From .150
    upward nothing answered.

    Measured on a guest afterwards: it took 10.31.0.162, running the same
    command twice left it there, and the mirror answered 200.
    """
    from tests.vm.cluster import GUEST_ADDRESS_BASE, configure_statically, static_address
    from tests.vm.proxmox import VMID_FIRST

    assert GUEST_ADDRESS_BASE >= 150, GUEST_ADDRESS_BASE
    command = configure_statically(static_address(VMID_FIRST + 5))
    assert "addr add 10.31.0.155/24" in command, command


def test_a_request_that_started_no_task_is_named_rather_than_crashed() -> None:
    """The API answers an accepted request with its task id, and a `data: null`
    where one belongs is a request that never started. Splitting it raised
    `AttributeError: 'NoneType' object has no attribute 'split'`, and the
    worker reported that instead of the failure behind it."""
    from tests.vm.proxmox import Api, ProxmoxError

    api = Api.__new__(Api)
    with pytest.raises(ProxmoxError) as raised:
        api.wait("infra-node4", "")
    assert "did not start" in str(raised.value)


def test_the_cluster_certificate_is_verified() -> None:
    """An administrator token for seventy-nine machines went to whatever
    answered on port 443. The comment justifying that said the cluster serves a
    certificate its own CA signed; it is issued by Let's Encrypt and the
    default context verifies it."""
    import ssl

    from tests.vm.proxmox import _certificates

    context = _certificates()
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_an_error_carried_in_a_two_hundred_is_not_thrown_away() -> None:
    """This API reports `invalid bootorder: device 'virtio0' does not exist` as
    HTTP 200 with `data: null` and the reason in `message`. Reading only `data`
    turned that into `answered without a task id` on four installs that had
    finished and collected their results. A success answers `{"data": null}`
    with no message at all."""
    import io
    import json

    from tests.vm.proxmox import Api, ProxmoxError

    class Answer(io.BytesIO):
        headers: dict[str, str] = {}

        def __enter__(self) -> "Answer":
            return self

        def __exit__(self, *unused: object) -> None:
            return None

    class Opener:
        """What `Api` calls `_opener`: only `open` is ever reached."""

        def __init__(self, body: dict[str, object]) -> None:
            self.body = body

        def open(self, request: object, timeout: float = 0.0) -> Answer:
            return Answer(json.dumps(self.body).encode())

    def answering(body: dict[str, object]) -> Any:
        return Opener(body)

    api = Api.__new__(Api)
    api.host = "pve.invalid"

    api._opener = answering({"data": None, "message": "invalid bootorder\n"})
    with pytest.raises(ProxmoxError) as raised:
        api.call("PUT", "/nodes/n/qemu/9300/config", boot="order=virtio0")
    assert "invalid bootorder" in str(raised.value)

    # A config change applied then and there says nothing, and that is success.
    api._opener = answering({"data": None})
    assert api.call("PUT", "/nodes/n/qemu/9300/config", description="x") is None


def test_the_blind_edit_leaves_no_gap_in_when_the_menu_could_appear() -> None:
    """A sample at `d` lands only when the menu appeared at some `t` with
    `d - BIOS_MENU_TIMEOUT < t <= d`, so the samples cover a union of windows
    and a gap between two of them is a menu time no attempt can catch.

    The samples used to be capped below `BIOS_MENU_TIMEOUT`, which covered a
    menu up within ten seconds and nothing after: no run has ever printed the
    line `append_to_cmdline_blind` prints when an edit lands, and every BIOS
    fixture ended every round at `the kernel never spoke`.
    """
    from tests.vm.proxmox import BIOS_ATTEMPTS, BIOS_MENU_TIMEOUT

    delays = sorted({delay for delay, _ in BIOS_ATTEMPTS})
    # The earliest sample covers a menu that is up at once.
    assert delays[0] <= BIOS_MENU_TIMEOUT, delays
    # No gap: consecutive samples closer together than the countdown.
    for earlier, later in zip(delays, delays[1:]):
        assert later - earlier < BIOS_MENU_TIMEOUT, (earlier, later)
    # And the cover reaches past a loaded node's menu. `vm-mdraid` needed more
    # than thirty seconds to open a readable UEFI editor on such a node, so
    # stopping at nine is what made every BIOS fixture unreachable.
    assert delays[-1] >= 15.0, delays
    assert len({down for _, down in BIOS_ATTEMPTS}) > 1, "one line is one guess"

    # Bounded: each attempt costs its delay, the keystrokes and a wait for the
    # kernel, and a schedule cannot give one guest the whole round.
    import inspect

    from tests.vm import proxmox

    patience = inspect.signature(proxmox.append_to_cmdline_blind).parameters["patience"]
    worst = sum(delay + 4.0 + float(patience.default) for delay, _ in BIOS_ATTEMPTS)
    assert worst < 900.0, worst


def test_a_refusal_from_the_node_itself_is_not_transient() -> None:
    """`403` is an answer, and retrying it wastes the window a real failure
    needs."""
    import io
    import urllib.error

    from tests.vm.proxmox import ProxmoxError, ProxmoxTransientError, _http_exception

    error = urllib.error.HTTPError(
        "https://pve.invalid/api2/json/nodes/infra-node3/qemu/9306/status",
        403,
        "Permission check failed",
        Message(),
        io.BytesIO(b""),
    )
    classified = _http_exception("GET", "/nodes/infra-node3/qemu/9306/status", error)

    assert isinstance(classified, ProxmoxError)
    assert not isinstance(classified, ProxmoxTransientError)


def test_a_stop_refuses_a_machine_that_is_not_this_runs() -> None:
    """`destroy` has checked the tag since a VMID was found not to be proof of
    ownership; `stop` had no check at all, and three call sites reach it without
    going through `destroy`. VMIDs are recycled between fixtures inside one
    campaign: 9302 carried three guests in seventy minutes."""
    stopped: list[str] = []

    class Someone(Api):
        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/status/current"):
                return {"status": "running"}
            if path.endswith("/config"):
                return {"tags": "somebody-else"}
            stopped.append(path)
            return "UPID:node:stop"

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            return None

    guest = Guest(Someone(), "node", 9302, GuestSpec(name="x", iso="x"))
    guest._booted = True
    with pytest.raises(ProxmoxError, match="not this run's machine"):
        guest.stop()

    assert stopped == []
    assert guest._booted


def test_a_stop_still_stops_this_runs_machine() -> None:
    stopped: list[str] = []

    class Ours(Api):
        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/status/current"):
                return {"status": "running"}
            if path.endswith("/config"):
                return {"tags": f"other;{TAG};gi-owned"}
            stopped.append(path)
            return "UPID:node:stop"

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            return None

    guest = Guest(Ours(), "node", 9302, GuestSpec(name="x", iso="x", nonce="gi-owned"))
    guest._booted = True
    guest.stop()

    assert stopped == ["/nodes/node/qemu/9302/status/stop"]
    assert not guest._booted


def test_a_task_that_finished_with_warnings_finished(capsys: pytest.CaptureFixture[str]) -> None:
    """Proxmox writes `WARNINGS: n` for a task that did its work and logged
    something on the way. Three `qmdestroy` tasks ended that way and the
    schedule printed `the guest was not removed` for machines that were gone."""
    from tests.vm.proxmox import Api

    class Answering(Api):
        def __init__(self) -> None:
            pass

        def call(self, method: str, path: str, **fields: object) -> dict[str, object]:
            return {"status": "stopped", "exitstatus": "WARNINGS: 2"}

    Answering().wait("infra-node6", "UPID:infra-node6:0:0:0:qmdestroy:9300:zakk@pve:")

    assert "WARNINGS: 2" in capsys.readouterr().err


def test_a_task_that_failed_is_still_a_failure() -> None:
    from tests.vm.proxmox import Api, ProxmoxError

    class Refusing(Api):
        def __init__(self) -> None:
            pass

        def call(self, method: str, path: str, **fields: object) -> dict[str, object]:
            return {"status": "stopped", "exitstatus": "unable to open disk image"}

    with pytest.raises(ProxmoxError, match="unable to open disk image"):
        Refusing().wait("infra-node6", "UPID:infra-node6:0:0:0:qmdestroy:9300:zakk@pve:")


def test_a_node_out_of_cores_offers_no_slot_however_much_memory_it_has() -> None:
    """`infra-node1` sat at 100% of its four cores with 7 GiB free while the
    schedule counted memory alone, and the cluster proxy answered 595 for the
    node beside it. Cores are measured rather than derived: the cluster runs
    other people's machines and their load is not in `cores`."""
    from tests.vm.cluster import GUEST_MEMORY_MIB, NODE_HEADROOM_BYTES, free_slots
    from tests.vm.proxmox import Api, Node

    guest = GUEST_MEMORY_MIB * 1024**2

    class Saturated(Api):
        def __init__(self) -> None:
            pass

        def nodes(self) -> list[Node]:
            return [
                Node(
                    name="busy",
                    free_bytes=NODE_HEADROOM_BYTES + guest * 4,
                    cores=4,
                    free_cores=0.2,
                ),
                Node(
                    name="idle",
                    free_bytes=NODE_HEADROOM_BYTES + guest * 4,
                    cores=4,
                    free_cores=3.2,
                ),
            ]

    offered = [node.name for node in free_slots(Saturated())]

    assert "busy" not in offered
    assert offered == ["idle", "idle"], "two guests on a node with 3.2 cores free"


def test_cores_this_schedule_has_placed_are_subtracted_too() -> None:
    """A guest's load takes minutes to show in what the node reports, the same
    lag the memory reservation exists for."""
    from tests.vm.cluster import GUEST_MEMORY_MIB, NODE_HEADROOM_BYTES, free_slots
    from tests.vm.proxmox import Api, Node

    guest = GUEST_MEMORY_MIB * 1024**2

    class Idle(Api):
        def __init__(self) -> None:
            pass

        def nodes(self) -> list[Node]:
            return [
                Node(
                    name="one",
                    free_bytes=NODE_HEADROOM_BYTES + guest * 8,
                    cores=8,
                    free_cores=7.0,
                )
            ]

    # One per guest, not one per vCPU: a node with 7 cores free carries six
    # after the headroom, and each guest already placed there takes one.
    assert len(free_slots(Idle())) == 6
    assert len(free_slots(Idle(), None, {"one": 4})) == 2
    assert free_slots(Idle(), None, {"one": 6}) == []


def test_a_driver_cd_old_enough_to_belong_to_nobody_is_named() -> None:
    """A schedule removes its own driver CD, but one killed outright leaves it:
    149 were counted across six nodes against a 33 GiB store. Age is what makes
    the answer safe, because a campaign runs for hours and a file uploaded this
    minute may be another campaign's."""
    from tests.vm.proxmox import Api

    day = 24 * 60 * 60.0
    now = time.time()

    class Stored(Api):
        def __init__(self) -> None:
            pass

        def call(self, method: str, path: str, **fields: object) -> Any:
            return [
                {"volid": "local:iso/gi-driver-thisrun.iso", "ctime": now - 5 * day},
                {"volid": "local:iso/gi-driver-abandoned.iso", "ctime": now - 2 * day},
                {"volid": "local:iso/gi-driver-someone-elses.iso", "ctime": now - 60},
                {"volid": "local:iso/install-amd64-minimal.iso", "ctime": now - 30 * day},
                {"volid": "local:iso/harvester-v1.5.0-amd64.iso", "ctime": now - 30 * day},
            ]

    stale = Stored().stale_drivers("infra-node6", "gi-driver-thisrun.iso", day)

    assert stale == ["gi-driver-abandoned.iso"]


def test_a_frame_the_reader_cannot_parse_closes_the_console() -> None:
    """`_protocol_error` raises `WebSocketError`, which is not a `ConsoleClosed`
    and not an `OSError`: it went past every reconnect handler. A corrupt frame
    is one more way for a connection to end, and the reader above reopens a
    closed one."""
    from tests.vm.proxmox import ConsoleChannel
    from tests.vm.websocket import WebSocketError

    class Corrupt:
        def __init__(self) -> None:
            self.closed = False

        def read(self) -> bytes:
            self.closed = True
            raise WebSocketError("the server sent an invalid websocket control frame")

        def send(self, data: bytes, opcode: int = 2) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    socket = Corrupt()
    channel = ConsoleChannel(cast(Any, socket))

    assert channel.recv(4096) == b""
    assert channel.closed, "the reader reopens a closed console"


def test_a_delete_that_found_the_guest_running_is_tried_again() -> None:
    """`destroy` stops the guest and then deletes it, and the stop can fail on
    its own: 9302 sat on a node refusing connections, the delete answered
    `VM 9302 is running - destroy failed`, and that ended the only loop that
    would have stopped it again. It held its memory until the schedule closed."""
    from tests.vm.proxmox import ProxmoxTransientError, _http_exception

    error = urllib.error.HTTPError(
        "https://pve/api2/json/nodes/infra-node3/qemu/9302",
        500,
        "VM 9302 is running - destroy failed",
        Message(),
        io.BytesIO(b'{"message":"VM 9302 is running - destroy failed\\n","data":null}'),
    )

    answered = _http_exception("DELETE", "/nodes/infra-node3/qemu/9302?purge=1", error)

    assert isinstance(answered, ProxmoxTransientError)


def test_a_delete_refused_for_another_reason_is_not_retried() -> None:
    from tests.vm.proxmox import ProxmoxError, ProxmoxTransientError, _http_exception

    error = urllib.error.HTTPError(
        "https://pve/api2/json/nodes/infra-node3/qemu/9302",
        500,
        "storage 'ceph-pve' is not online",
        Message(),
        io.BytesIO(b'{"message":"storage is not online","data":null}'),
    )

    answered = _http_exception("DELETE", "/nodes/infra-node3/qemu/9302?purge=1", error)

    assert isinstance(answered, ProxmoxError)
    assert not isinstance(answered, ProxmoxTransientError)


def test_a_heavy_guest_still_fits_a_four_core_node() -> None:
    """Every node in this cluster has four cores and a heavy guest asks for
    four vCPUs. Requiring those to be free meant `btrfs-luks`, `vm-desktop`,
    `vm-gnome`, `vm-openrc-desktop` and `zfs-zbm` could never be placed at all:
    a round dispatched three light fixtures and stalled."""
    from tests.vm.cluster import HEAVY_MEMORY_MIB, Job, NODE_HEADROOM_BYTES, room_for
    from tests.vm.proxmox import Node

    node = Node(
        name="infra-node4",
        free_bytes=NODE_HEADROOM_BYTES + HEAVY_MEMORY_MIB * 1024**2,
        cores=4,
        free_cores=3.5,
    )
    heavy = Job(name="vm-desktop", fixture=Path("vm-desktop.toml"), heavy=True)

    assert heavy.cores == 4, "a heavy guest does ask for four"
    assert room_for(node, heavy)
    assert not room_for(node, heavy, None, {"infra-node4": 3}), "three guests already there"


def test_the_editor_is_given_ten_presses_before_it_is_called_a_failure() -> None:
    """Each attempt costs an escape, a settle and a snapshot. Thirty seconds
    bought six presses, and `vm-mdraid` lost a run in forty-eight seconds while
    its node sat at 99.8% CPU. The menu is already held, so waiting longer
    races nothing."""
    import inspect

    from tests.vm import proxmox

    one_press = proxmox.ESCAPE_SETTLES + 3.0
    assert proxmox.EDITOR_PATIENCE >= 10 * one_press, proxmox.EDITOR_PATIENCE

    signature = inspect.signature(proxmox.append_to_cmdline)
    assert signature.parameters["timeout"].default == proxmox.EDITOR_PATIENCE


def test_the_cluster_serves_no_screenshot_so_none_is_offered() -> None:
    """`Guest.screenshot` read `GET /nodes/{node}/qemu/{vmid}/screenshot` and
    answered `None` on any `ProxmoxError`. Proxmox VE 9.2.10 answers that path
    `501 Method 'GET …/screenshot' not implemented`, so it could only ever have
    answered `None` — a diagnostic that reports "no screen" for ever, on the
    one path that has no other diagnostic.

    Nothing called it. It is named here so that a reader who wants the VGA
    console for the BIOS fixtures learns the endpoint does not exist rather
    than adding a caller and reading `None`.

    Three checks, because none of them covers the others: the attribute catches
    a caller that kept the name, the `screenshot` literal catches a method
    called anything else that asks for the path, and `screendump` catches the
    QEMU monitor route `POST /nodes/{node}/qemu/{vmid}/monitor`, which takes
    the same picture and spells neither of the first two. That route is the one
    the comment above `AUTOLOGIN_INTERVAL` says a screen was measured with.
    """
    import ast

    from tests.vm import cluster, proxmox

    assert not hasattr(Guest, "screenshot")

    # Docstrings are excluded because two of them explain why the path is
    # absent, and `AsyncFunctionDef` keeps that true for a coroutine added later.
    for module in (proxmox, cluster):
        tree = ast.parse(Path(module.__file__ or "").read_text())
        prose = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        asked = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and ("screenshot" in node.value or "screendump" in node.value)
            and id(node) not in prose
        ]
        assert asked == [], (module.__name__, asked)


def test_a_boot_that_never_prompts_says_what_the_console_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`the installed system did not reach a login prompt: never matched
    'login:'; last output was b'OKstarting serial terminal on interface
    serial0'` is the verdict `vm-sdboot` and `vm-convert` came back with, and
    it says the guest was silent and nothing about what it was doing. For the
    conversion it cannot even be told apart from a failure in the conversion
    stage, which runs after this point."""
    from tests.vm import cluster
    from tests.vm.console import ConsoleTimeout

    class Guest:
        def stop(self) -> None:
            return None

        def boot_from_disk(self) -> None:
            return None

        def start(self) -> None:
            return None

        def reset(self) -> None:
            return None

    class Silent:
        def __init__(self, screen: bytes) -> None:
            self.console = type(
                "Screen", (), {"snapshot": lambda _self, seconds: screen}
            )()

        def reopen(self, *, solicit_prompt: bool = True) -> None:
            return None

        def observe(self, pattern: str, timeout: float, *, solicit: bool = False) -> bytes:
            raise ConsoleTimeout("never matched 'login:'")

        def respond(self, line: str) -> None:
            return None

    from gentoo_install.exec.config import load

    installation = load(Path("tests/fixtures/vm-xfs.toml"))
    assert not installation.kernel.remote_unlock.enabled, "no unlock in the way"

    held = b"[  OK  ] Reached target Multi-User System.\r\nA start job is running"
    said = cluster.boot_and_check(
        cast(Any, Guest()), cast(Any, Silent(held)), Path("unused"), installation
    )
    # Carried, not returned: a prompt that went by during a reconnect is not a
    # verdict on its own, because typing a name brings it back. The screen still
    # reaches the verdict the login exchange then produces.
    assert "did not reach a login prompt" in said
    assert "start job is running" in said, said

    # Negative control: a different screen gives a different verdict. Without
    # the snapshot both read the same, whatever the guest was showing.
    other = cluster.boot_and_check(
        cast(Any, Guest()),
        cast(Any, Silent(b"Kernel panic - not syncing")),
        Path("unused"),
        installation,
    )
    assert "Kernel panic" in other, other
    assert other != said


def test_a_silent_console_is_not_reported_as_a_stubborn_grub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`openrc-sdboot` came back with `GRUB never opened its editor: b'\\r\\n'`
    after two minutes of pressing `e`: two bytes for the whole wait. That is a
    console that delivered nothing, not a menu that refused to open an editor,
    and the two need different answers — one is a node, the other is a
    keystroke count."""
    from tests.vm import proxmox
    from tests.vm.proxmox import ProxmoxError

    monkeypatch.setattr("time.sleep", lambda seconds: None)

    class Console:
        def __init__(self, screen: bytes) -> None:
            self.screen = screen

        def send_raw(self, keys: str) -> None:
            return None

        def snapshot(self, seconds: float) -> bytes:
            return self.screen

    with pytest.raises(ProxmoxError, match="the console delivered"):
        proxmox._editor_screen(cast(Any, Console(b"\r\n")), 0.2)

    # Negative control: a console that drew a menu and still opened no editor
    # keeps the sentence about GRUB, because that is a different failure.
    menu = b"\x1b[2J\x1b[01;01HGNU GRUB  version 2.14\r\nGentoo Linux\r\n" * 4
    with pytest.raises(ProxmoxError, match="GRUB never opened its editor"):
        proxmox._editor_screen(cast(Any, Console(menu)), 0.2)

    # And the editor is the editor, so neither fires. Positioned the way GRUB
    # draws it, copied from `vm-sdboot`'s console.
    assert b"setparams" in proxmox._editor_screen(cast(Any, Console(EDITOR_SCREEN)), 0.2)


def test_the_editor_screen_waits_for_the_line_it_is_going_to_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRUB draws the entry line by line. `btrfs-luks` was ended at 0.3
    minutes with `no GRUB entry to edit on this screen` on a screen holding
    `setparams` and the entry's `search` line, the `linux` line still to come:
    the reader returned the moment it saw `setparams`.
    """
    from tests.vm import proxmox

    monkeypatch.setattr("time.sleep", lambda seconds: None)

    class Piecewise:
        """A console that delivers the entry the way one arrived."""

        def __init__(self, pieces: list[bytes]) -> None:
            self.pieces = pieces
            self.keys: list[str] = []

        def send_raw(self, keys: str) -> None:
            self.keys.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return self.pieces.pop(0) if self.pieces else b""

    console = Piecewise([EDITOR_HEAD, EDITOR_TAIL])
    screen = proxmox._editor_screen(cast(Any, console), 5.0)

    assert b"setparams" in screen and b"linux /boot/gentoo" in screen, screen
    # And it did not press ESC between the two, which discards the edit and
    # returns to the menu.
    assert console.keys == ["e"], console.keys
    # The caller can count the rows it needs.
    assert proxmox._line_of_linux(screen) == 3


#: The GRUB editor as `vm-sdboot` drew it: `setparams` on row 5, the entry's
#: `search` on row 7 and the kernel on row 8, each placed with `ESC[row;colH`.
EDITOR_HEAD: Final[bytes] = (
    b"\x1b[05;03Hsetparams 'Boot LiveCD (kernel: gentoo)'"
    b"\x1b[07;03Hsearch --no-floppy --set=root -l Gentoo-amd64-20260816"
)
EDITOR_TAIL: Final[bytes] = (
    b"\x1b[08;03Hlinux /boot/gentoo dokeymap nodhcp root=live:CDLABEL=Gentoo-amd64-20260816"
    b"\x1b[09;03Hinitrd /boot/gentoo.igz"
)
EDITOR_SCREEN: Final[bytes] = EDITOR_HEAD + EDITOR_TAIL


#: What `run60/vm-openrc-desktop.log` held, in the order it arrived. The first
#: line is the hypervisor's, not the guest's.
BANNER: Final[bytes] = b"OKstarting serial terminal on interface serial0\r\n"
FIRMWARE: Final[bytes] = b'BdsDxe: loading Boot0002 "UEFI QEMU DVD-ROM QM00003 "\r\n'
MENU: Final[bytes] = b"GNU GRUB  version 2.14\r\n   Press enter to boot the selected OS\r\n"
COUNTS: Final[tuple[bytes, ...]] = tuple(
    b"   The highlighted entry will be executed automatically in %ds." % n
    for n in (2, 1, 0)
)
BOOTED: Final[bytes] = b"  Booting `Boot LiveCD (kernel: gentoo)'\n\r\n\r"


def test_the_hold_is_pressed_until_grub_stops_counting() -> None:
    """`GRUB_COUNTDOWN` also matches `starting serial terminal on interface
    serial0`, which the hypervisor prints before the firmware loads anything.
    One press there landed in the void, `vm-openrc-desktop` counted `2s 1s 0s`
    and booted `Boot LiveCD` unedited with no serial console, and the editor
    was asked for over the next two and a half minutes on a guest already gone.
    """
    from tests.vm.proxmox import hold_the_menu

    class Console:
        """GRUB draws two snapshots after the banner and counts until a press
        arrives while its menu is up."""

        def __init__(self) -> None:
            self.sent: list[str] = []
            self.frames = [FIRMWARE, MENU + COUNTS[0], COUNTS[1], b""]
            self.held_at: int | None = None

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            return BANNER

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)
            # Only a press that arrives while the menu is drawn stops it.
            if self.held_at is None and len(self.sent) >= 3:
                self.held_at = len(self.sent)

        def snapshot(self, seconds: float) -> bytes:
            if self.held_at is not None:
                return b""
            return self.frames.pop(0) if self.frames else b""

        def send(self, line: str) -> None:
            self.sent.append(line)

        @property
        def closed(self) -> bool:
            return False

        def close(self) -> None:
            pass

    console = Console()
    seen = hold_the_menu(console, timeout=30.0)

    assert console.sent == ["\x0e\x10"] * 3, console.sent
    assert b"GNU GRUB" in seen


def test_an_entry_that_booted_unedited_is_said_at_once() -> None:
    """Two and a half minutes were spent asking for an editor after the guest
    had booted. The boot line is on the same screen, so it is a verdict rather
    than a wait.
    """
    from tests.vm.proxmox import GrubNotReadable, hold_the_menu

    class Console:
        def __init__(self) -> None:
            self.frames = [MENU + COUNTS[0], COUNTS[2] + BOOTED]
            self.sent: list[str] = []

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            return BANNER

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return self.frames.pop(0) if self.frames else b""

        def send(self, line: str) -> None:
            self.sent.append(line)

        @property
        def closed(self) -> bool:
            return False

        def close(self) -> None:
            pass

    with pytest.raises(GrubNotReadable, match="booted before its countdown"):
        hold_the_menu(Console(), timeout=30.0)


def test_a_reset_that_timed_out_is_sent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """run63 lost `vm-unlock` at 1.2 minutes to

        POST /nodes/infra-node3/qemu/9304/status/reset did not answer:
        <urlopen error timed out>

    A reset is idempotent: resetting a guest that was already reset does
    nothing, and a request that timed out may well have arrived. So the one
    call that cannot be made worse by repeating it was the one not repeated.
    """
    from tests.vm.proxmox import Guest, GuestSpec, ProxmoxTransientError

    monkeypatch.setattr("tests.vm.proxmox.time.sleep", lambda seconds: None)
    tries: list[str] = []

    class Timing:
        def call(self, method: str, path: str, **form: object) -> str:
            tries.append(path)
            if len(tries) < 3:
                raise ProxmoxTransientError(f"{method} {path} did not answer: timed out")
            return "UPID:done"

        def wait(self, node: str, upid: str, patience: float = 0.0) -> None:
            return None

    guest = Guest(
        api=cast(Any, Timing()),
        node="infra-node3",
        vmid=9304,
        spec=GuestSpec(name="vm-unlock", iso="x"),
    )
    guest.reset()

    assert len(tries) == 3, tries
    assert all(one.endswith("/status/reset") for one in tries), tries


def test_a_reset_that_never_answers_still_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative direction: retrying for ever would hold a node until the
    schedule ended, which is what the bounded count is for."""
    from tests.vm.proxmox import Guest, GuestSpec, ProxmoxTransientError, RESET_TRIES

    monkeypatch.setattr("tests.vm.proxmox.time.sleep", lambda seconds: None)
    tries: list[str] = []

    class Never:
        def call(self, method: str, path: str, **form: object) -> str:
            tries.append(path)
            raise ProxmoxTransientError(f"{method} {path} did not answer: timed out")

        def wait(self, node: str, upid: str, patience: float = 0.0) -> None:
            return None

    guest = Guest(
        api=cast(Any, Never()),
        node="infra-node3",
        vmid=9304,
        spec=GuestSpec(name="vm-unlock", iso="x"),
    )
    with pytest.raises(ProxmoxTransientError):
        guest.reset()

    assert len(tries) == RESET_TRIES, tries


class _AutoLoginGuest:
    """A SeaBIOS guest that answers on the serial port only once the line has
    been typed into the auto-login shell the VGA console already holds."""

    def __init__(self, answers_after: int = 1) -> None:
        self.answers_after = answers_after
        self.typed: list[str] = []
        self.resets = 0
        #: How many times the whole line went in. Counting a character would
        #: count the line's own letters: `setsid agetty` holds four `s`.
        self.lines = 0

    def send_keys(self, keys: list[str]) -> None:
        self.typed.extend(keys)
        if len(keys) > 1:
            self.lines += 1

    def reset(self) -> None:
        self.resets += 1


class _SerialAfterTyping:
    """A port that answers once the line has been typed `needed` times, which
    is what a medium still booting looks like."""

    def __init__(self, guest: _AutoLoginGuest, needed: int) -> None:
        self.guest = guest
        self.needed = needed
        self.console = self

    def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
        from tests.vm.console import ConsoleTimeout

        if self.guest.lines >= self.needed:
            return b"\r\nroot@livecd ~ # "
        raise ConsoleTimeout("nothing on the serial port")

    def reopen(self, *, solicit_prompt: bool = True) -> None:
        return None


def test_the_serial_shell_is_asked_for_by_typing_into_the_auto_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blind GRUB edit this replaces never landed. A screenshot through
    the QEMU monitor shows why: three seconds after the menu the live system
    is logged in on the VGA console, so the keys reached a shell and the
    screen held `bash: cserial: command not found`."""
    from typing import Any, cast

    from tests.vm import proxmox

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    guest = _AutoLoginGuest()
    link = _SerialAfterTyping(guest, needed=1)
    proxmox.open_a_serial_shell_blind(cast("Any", guest), cast("Any", link))

    typed = "".join(one for one in guest.typed if len(one) == 1)
    assert "agetty" in "".join(guest.typed) or "agetty" in typed, guest.typed
    # The command line is not touched: nothing types `console=` anywhere.
    assert "console=" not in "".join(guest.typed), guest.typed
    assert guest.resets == 0, "a first attempt that answers resets nothing"


def test_a_medium_that_is_slower_is_typed_at_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ladder of fixed delays: every timing guess this replaces failed on a
    loaded cluster, and `vm-bios` passed only in a round where it was the one
    guest. The line goes in again until the port answers, and the medium is
    never reset for being slow."""
    from typing import Any, cast

    from tests.vm import proxmox

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    guest = _AutoLoginGuest()
    link = _SerialAfterTyping(guest, needed=3)
    proxmox.open_a_serial_shell_blind(cast("Any", guest), cast("Any", link))

    assert guest.resets == 0, "a slow medium is typed at again, not reset"
    assert guest.lines == 3, guest.lines


def test_a_medium_that_never_answers_stops_rather_than_holding_the_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any, cast

    from tests.vm import proxmox
    from tests.vm.proxmox import ProxmoxError

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    guest = _AutoLoginGuest()
    # A port that never answers, however many times the line goes in.
    link = _SerialAfterTyping(guest, needed=10**9)
    with pytest.raises(ProxmoxError, match="no serial shell"):
        proxmox.open_a_serial_shell_blind(
            cast("Any", guest), cast("Any", link), patience=0.5
        )

    assert guest.typed, "it has to have tried"


class _ClosesWhileNothingCrossesIt:
    """A node's serial session as it behaves during the typing.

    The keys go through the API, so no byte crosses the console for the whole
    minute the line takes. Measured on a guest built for the question: the
    session was gone at 0.0s of watching on four attempts out of four, with
    the guest silent the whole time and the log holding only the node's own
    `starting serial terminal on interface serial0`.
    """

    def __init__(self, guest: "_AutoLoginGuest") -> None:
        self.guest = guest
        self.console = self
        self.opened_at_lines = 0
        self.reopens = 0
        self.solicited: list[bool] = []

    def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
        from tests.vm.console import ConsoleClosed, ConsoleTimeout

        if self.opened_at_lines < self.guest.lines:
            raise ConsoleClosed(
                "the guest closed the serial connection",
                write_may_have_reached_guest=False,
            )
        if self.guest.lines >= 1:
            return b"\r\nroot@livecd ~ # "
        raise ConsoleTimeout("nothing on the serial port")

    def reopen(self, *, solicit_prompt: bool = True) -> None:
        self.reopens += 1
        self.solicited.append(solicit_prompt)
        self.opened_at_lines = self.guest.lines


def test_the_console_watched_is_opened_after_the_line_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing one line is 38 characters of one API request each, which took 68
    seconds on `infra-node1`, and the node closes a session that carries
    nothing for that long. Watching the session that was open before the
    typing is watching a session that is already gone."""
    from typing import Any, cast

    from tests.vm import proxmox

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    guest = _AutoLoginGuest()
    link = _ClosesWhileNothingCrossesIt(guest)

    proxmox.open_a_serial_shell_blind(cast("Any", guest), cast("Any", link))

    assert link.reopens == 1, link.reopens
    assert guest.lines == 1, guest.lines
    # `agetty --autologin` prints one prompt as the shell starts and never
    # prints again, so a session opened silently after the typing shows
    # nothing for ever: `ext4-bios` matched nothing across fourteen attempts
    # and sixteen minutes that way.
    assert link.solicited == [True], link.solicited


def test_the_typed_line_stays_short_because_every_character_is_a_request() -> None:
    """`KEY_PAUSE` alone is 0.12s per character before the request itself, so
    the length of this line is a fifth of the deadline. Measured: 61 characters
    took 68s of a 360s budget, leaving five attempts of which none watched a
    live console."""
    from tests.vm.proxmox import SERIAL_GETTY

    assert len(SERIAL_GETTY) <= 40, SERIAL_GETTY
    assert "agetty" in SERIAL_GETTY and "ttyS0" in SERIAL_GETTY, SERIAL_GETTY
    assert "115200" in SERIAL_GETTY, "the baud has to match the medium's port"
    assert SERIAL_GETTY.rstrip().endswith("&"), "it cannot hold the shell"


def test_the_deadline_outlasts_a_loaded_node_and_the_interval_does_not() -> None:
    """An idle node reaches the auto-login in about fifteen seconds, measured
    by screenshotting a SeaBIOS guest through the QEMU monitor. A node running
    twelve of them takes longer than any number written here, so the interval
    is short enough to keep trying and the deadline is the only guess."""
    from tests.vm.proxmox import (
        AUTOLOGIN_DEADLINE,
        AUTOLOGIN_INTERVAL,
        AUTOLOGIN_TYPING,
    )

    assert AUTOLOGIN_INTERVAL <= 30.0, AUTOLOGIN_INTERVAL
    # Against what an attempt costs, not against the interval alone: typing is
    # most of it, so the old reading predicted eighteen attempts where five
    # happened.
    attempts = AUTOLOGIN_DEADLINE / (AUTOLOGIN_TYPING + AUTOLOGIN_INTERVAL)
    assert attempts >= 10, f"{attempts:.0f} attempts is too few"
    assert AUTOLOGIN_TYPING >= 48.7, "measured on infra-node5, the fastest of three"


def test_the_installed_checks_come_from_one_table() -> None:
    """`cluster.py` carried its own `INSIDE` pair for `os-release` and
    `fstab`, which `installed.checks()` already derives from the
    configuration. Two tables for one rule set is what lets the answers
    diverge; the shared contract is the one the runners read."""
    from pathlib import Path as _Path

    from gentoo_install.exec.config import load
    from tests.vm import cluster
    from tests.vm.installed import checks

    assert not hasattr(cluster, "INSIDE")
    named = {check.name for check in checks(load(_Path("tests/fixtures/vm-xfs.toml")))}
    assert {"os-release", "fstab"} <= named, named


def test_a_failed_installed_check_answers_with_what_the_machine_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`greetd config: the installed system does not say '(?ms)...'` was the
    whole of a 49-minute verdict. The same sentence covers a file never
    written, a file written and still naming `agreety`, and no file at all,
    and the round after it could only guess which."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster

    installation = load(Path("tests/fixtures/vm-greetd.toml"))
    installed = cluster._asked_for(installation)
    greetd_name, greetd_command, _ = next(
        check for check in installed if check[0] == "greetd config"
    )
    answers = {command: wanted.encode() for _, command, wanted in installed}
    held = b"[default_session]\r\ncommand = \"agreety --cmd /bin/sh\"\r\nuser = \"greetd\"\r\n"
    answers[greetd_command] = held
    asked: list[str] = []

    class Guest:
        def stop(self) -> None:
            return None

        def boot_from_disk(self) -> None:
            return None

        def start(self) -> None:
            return None

        def reset(self) -> None:
            return None

    class Link:
        def reopen(self, *, solicit_prompt: bool = True) -> None:
            return None

        def observe(self, *unused: object, **ignored: object) -> bytes:
            return b""

        def expect_output(self, command: str, timeout: float = 0.0) -> bytes:
            asked.append(command)
            return answers[command]

    def unlocked(*unused: object) -> cluster.UnlockResult:
        return cluster.UnlockResult(cluster.InstalledBootState.LOGIN_READY, "")

    monkeypatch.setattr(cluster, "_unlock", unlocked)
    monkeypatch.setattr(cluster, "_log_in", lambda *unused: "")

    refused = cluster.boot_and_check(
        cast(Any, Guest()),
        cast(Any, Link()),
        Path("unused"),
        installation,
    )

    assert asked[-1] == greetd_command, asked
    assert refused.startswith(f"{greetd_name}: the installed system does not say"), refused
    assert "agreety" in refused, "the answer, not only the pattern"


def test_a_remote_unlock_that_delivered_a_wrong_passphrase_is_not_a_login_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`zbm-unlock` failed at 93.0 minutes with `the installed system asked
    for a name and kept asking`. Its console says otherwise: the passphrase
    reached dropbear, `dracut-pre-mount` answered `Key load error: Incorrect
    key provided for 'zpcala'` three times, `/sysroot` never mounted and the
    guest sat in emergency mode. The ssh session proves delivery, not that the
    key was right."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster
    from tests.vm.console import ConsoleTimeout

    class Guest:
        def stop(self) -> None:
            return None

        def boot_from_disk(self) -> None:
            return None

        def start(self) -> None:
            return None

        def reset(self) -> None:
            return None

    held = (
        b"[   60.242509] dracut-pre-mount[622]: Key load error: "
        b"Incorrect key provided for 'zpcala'.\r\n"
        b"[FAILED] Failed to mount /sysroot.\r\n"
        b"Entering emergency mode. Exit the shell to continue.\r\n"
    )

    class Link:
        """A console holding that screen and nothing else.

        It answers the rest of the login protocol too, so removing the check
        under test fails an assertion rather than an attribute lookup.
        """

        def reopen(self, *, solicit_prompt: bool = True) -> None:
            return None

        def observe(self, pattern: str, timeout: float = 0.0, **ignored: object) -> bytes:
            import re as _re

            if _re.search(pattern.encode(), held):
                return held
            raise ConsoleTimeout("nothing like that on the screen")

        def respond(self, line: str) -> None:
            return None

        def run(self, command: str, **ignored: object) -> None:
            return None

        def expect_output(self, command: str, timeout: float = 0.0) -> bytes:
            return b""

    monkeypatch.setattr(cluster, "remote_unlock", lambda *unused, **ignored: None)
    installation = load(Path("tests/fixtures/zbm-unlock.toml"))
    assert installation.kernel.remote_unlock.enabled, "the fixture under test"

    refused = cluster.boot_and_check(
        cast(Any, Guest()),
        cast(Any, Link()),
        Path("unused"),
        installation,
        remote_key=Path("unused.key"),
    )

    assert "did not mount the root" in refused, refused
    assert "Key load error" in refused, "the screen, not only the conclusion"
    assert "asked for a name" not in refused, refused


def test_a_remote_unlock_that_worked_carries_on_to_the_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check must not turn every remote unlock into a failure: a console
    that answers with a login prompt carries on as it always did, and the
    layouts whose initramfs really was unlocked over ssh see exactly that."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster

    class Guest:
        def stop(self) -> None:
            return None

        def boot_from_disk(self) -> None:
            return None

        def start(self) -> None:
            return None

        def reset(self) -> None:
            return None

    class Link:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def reopen(self, *, solicit_prompt: bool = True) -> None:
            return None

        def observe(self, pattern: str, timeout: float = 0.0, **ignored: object) -> bytes:
            self.asked.append(pattern)
            return b"\r\nunlockbox login: "

        def respond(self, line: str) -> None:
            return None

        def run(self, command: str, **ignored: object) -> None:
            return None

        def expect_output(self, command: str, timeout: float = 0.0) -> bytes:
            return b""

    monkeypatch.setattr(cluster, "remote_unlock", lambda *unused, **ignored: None)
    monkeypatch.setattr(cluster, "_log_in", lambda *unused: "")
    monkeypatch.setattr(cluster, "_asked_for", lambda installation: ())

    link = Link()
    refused = cluster.boot_and_check(
        cast(Any, Guest()),
        cast(Any, link),
        Path("unused"),
        load(Path("tests/fixtures/zbm-unlock.toml")),
        remote_key=Path("unused.key"),
    )

    assert refused == "", refused
    # And the wait it did covers every prompt the machine might show, so a
    # console that asks again is answered rather than waited out.
    # `re.escape` puts a backslash before the space, so the pattern is matched
    # by what it is built from rather than by its literal text.
    assert any("emergency" in one for one in link.asked), link.asked
    assert any("passphrase" in one for one in link.asked), link.asked


def test_a_zfsbootmenu_machine_is_answered_when_its_own_initramfs_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ZFSBootMenu carries the ssh daemon in its own image, so `zfs load-key`
    over that session unlocks the pool ZBM reads and nothing else: the system
    initramfs it boots asks again on the console. The harness took the ssh
    session as the end of the passphrase and never answered."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster
    from tests.vm.console import DISK_PASSPHRASE

    class Guest:
        def stop(self) -> None:
            return None

        def boot_from_disk(self) -> None:
            return None

        def start(self) -> None:
            return None

        def reset(self) -> None:
            return None

    class Link:
        """Asks for a passphrase once, then shows a login prompt."""

        def __init__(self) -> None:
            self.answered: list[str] = []
            self.looks = 0

        def reopen(self, *, solicit_prompt: bool = True) -> None:
            return None

        def observe(self, pattern: str, timeout: float = 0.0, **ignored: object) -> bytes:
            self.looks += 1
            if self.looks == 1:
                return b"Encrypted ZFS password for zpcala/ROOT/gentoo/root "
            return b"\r\nzbmunlockbox login: "

        def respond(self, line: str) -> None:
            self.answered.append(line)

        def run(self, command: str, **ignored: object) -> None:
            return None

        def expect_output(self, command: str, timeout: float = 0.0) -> bytes:
            return b""

    monkeypatch.setattr(cluster, "remote_unlock", lambda *unused, **ignored: None)
    monkeypatch.setattr(cluster, "_log_in", lambda *unused: "")
    monkeypatch.setattr(cluster, "_asked_for", lambda installation: ())
    monkeypatch.setattr(cluster, "PASSWORD_ECHO_OFF_AFTER", 0.0)
    monkeypatch.setattr(cluster, "PASSWORD_ECHO_BACKOFF", 0.0)

    link = Link()
    refused = cluster.boot_and_check(
        cast(Any, Guest()),
        cast(Any, link),
        Path("unused"),
        load(Path("tests/fixtures/zbm-unlock.toml")),
        remote_key=Path("unused.key"),
    )

    assert refused == "", refused
    assert link.answered == [DISK_PASSPHRASE], link.answered


def test_a_destroy_whose_task_failed_is_tried_again() -> None:
    """Measured on the cluster: `qmdestroy` on 9301 ended with `rbd error:
    rbd: listing images failed: (2) No such file or directory`, the run said
    `the guest was not removed`, and the guest sat there for hours until the
    same DELETE with the same parameters removed it. A task failure is not
    evidence that the guest cannot be removed."""

    @dataclass
    class Flaky(Api):
        attempts: int = 0
        absent: bool = False

        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/config"):
                if self.absent:
                    raise ProxmoxNotFound("the guest does not exist")
                return {"tags": f"{TAG};gi-owned"}
            if path.endswith("/status/current"):
                return {"status": "stopped"}
            if method == "DELETE":
                self.attempts += 1
                return "UPID:node:qmdestroy"
            raise AssertionError((method, path))

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            if self.attempts < 2:
                raise ProxmoxError(
                    "UPID:node:qmdestroy ended with 'rbd error: rbd: listing images failed'"
                )
            self.absent = True

    api = Flaky()
    guest = Guest(api, "node", 9300, GuestSpec(name="x", iso="x", nonce="gi-owned"))
    guest.destroy(patience=30.0)
    assert api.attempts == 2, api.attempts
    assert api.absent

    # The control: a guest that never goes away is still refused, and the last
    # thing the cluster said is what the message carries.
    @dataclass
    class Refusing(Flaky):
        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            raise ProxmoxError("UPID:node:qmdestroy ended with 'rbd error: no such image'")

    stubborn = Guest(
        Refusing(), "node", 9300, GuestSpec(name="x", iso="x", nonce="gi-owned")
    )
    with pytest.raises(ProxmoxError, match="no such image"):
        stubborn.destroy(patience=0.5)

    # And a guest this run did not build is refused before any of that: a
    # retry loop must not become a way to remove somebody else's machine.
    @dataclass
    class Foreign(Flaky):
        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/config"):
                return {"tags": f"{TAG};gi-another-run"}
            raise AssertionError((method, path))

    with pytest.raises(ForeignGuest):
        Guest(Foreign(), "node", 9300, GuestSpec(name="x", iso="x", nonce="gi-owned")).destroy(
            patience=1.0
        )


def test_a_stop_the_gateway_ate_is_sent_again() -> None:
    """run118 lost `vm-binpkg` after 48 minutes: the install had finished and
    `POST .../status/stop` answered `502 Bad Gateway`, which `_http_exception`
    already classifies as transient. `destroy` retried such answers and this
    did not, so one gateway hiccup threw away a completed install."""

    @dataclass
    class Flaky(Api):
        attempts: int = 0
        running: bool = True

        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/status/current"):
                return {"status": "running" if self.running else "stopped"}
            if path.endswith("/config"):
                return {"tags": f"{TAG};gi-owned"}
            if path.endswith("/status/stop"):
                self.attempts += 1
                if self.attempts == 1:
                    raise ProxmoxTransientError(
                        "POST /nodes/n/qemu/9300/status/stop answered 502 Bad Gateway"
                    )
                self.running = False
                return "UPID:node:qmstop"
            raise AssertionError((method, path))

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            return None

    api = Flaky()
    guest = Guest(api, "node", 9300, GuestSpec(name="x", iso="x", nonce="gi-owned"))
    guest._booted = True
    guest.stop()
    assert api.attempts == 2, api.attempts
    assert not guest._booted

    # The control: an answer that is not transient still ends the run, and the
    # message carries what the cluster said.
    @dataclass
    class Refusing(Flaky):
        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/status/stop"):
                raise ProxmoxError("POST /nodes/n/qemu/9300/status/stop answered 403 Forbidden")
            return super().call(method, path, **form)

    stubborn = Guest(Refusing(), "node", 9300, GuestSpec(name="x", iso="x", nonce="gi-owned"))
    stubborn._booted = True
    with pytest.raises(ProxmoxError, match="403 Forbidden"):
        stubborn.stop()


def test_a_guest_replaced_between_the_two_reads_is_not_deleted() -> None:
    """`destroy` checks the tag and the nonce, then stops, then deletes. A
    VMID is handed back and reused inside one campaign, so the guest can be
    replaced between those reads; the stop notices and `destroy` swallowed it
    and deleted anyway. Measured by hand on 9311 the same day: a `DELETE` sent
    at a vmid that had changed owner was refused only because Proxmox will not
    destroy a running guest."""

    @dataclass
    class Replaced(Api):
        reads: int = 0
        deleted: bool = False

        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/config"):
                self.reads += 1
                # Ours on the first read, somebody else's by the second.
                tags = f"{TAG};gi-owned" if self.reads == 1 else f"{TAG};gi-another-run"
                return {"tags": tags}
            if path.endswith("/status/current"):
                return {"status": "running"}
            if method == "DELETE":
                self.deleted = True
                return "UPID:node:qmdestroy"
            raise AssertionError((method, path))

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            return None

    api = Replaced()
    guest = Guest(api, "node", 9300, GuestSpec(name="x", iso="x", nonce="gi-owned"))
    with pytest.raises(ForeignGuest, match="not this run's machine"):
        guest.destroy(patience=5.0)
    assert not api.deleted, "the guest that replaced ours was deleted"



def test_the_checksum_fields_precede_the_file_in_the_upload_body() -> None:
    """The endpoint reads the form in order and ignores a checksum after it.

    Sent the other way round the upload still succeeds, and the medium lands
    unverified: the failure is a wrong file that every later check trusts.
    """
    from tests.vm.proxmox import _multipart_head

    head = _multipart_head("Xbound", "medium.iso", "ab" * 64).decode()
    checksum = head.index('name="checksum"')
    filename = head.index('name="filename"')
    assert checksum < filename, head

    # Negative control: the algorithm has to be there too, or PVE reads the
    # 128 characters as the default and refuses the upload it already took.
    assert 'name="checksum-algorithm"' in head and "sha512" in head
    assert head.count("--Xbound") == 4, head


def test_the_boot_order_names_every_target_disk() -> None:
    """`gi-s7a` mirrored its pool across `vda1` and `vdb2` and put the esp on
    `vdb1`. `order=virtio0` pointed the firmware at a disk that is entirely a
    pool member, so it fell through to the medium still attached and the run
    read a live shell back as the installed system."""
    asked: list[dict[str, object]] = []

    class Recording:
        def call(self, method: str, path: str, **form: object) -> object:
            asked.append({"method": method, "path": path, **form})
            return None

    for disks, expected in (((40,), "order=virtio0"), ((40, 40), "order=virtio0;virtio1")):
        asked.clear()
        guest = Guest(
            api=cast(Api, Recording()),
            node="n",
            vmid=9300,
            spec=GuestSpec(name="gi-test", iso="x.iso", target_gib=disks),
        )
        guest.boot_from_disk()
        assert [one["boot"] for one in asked] == [expected], disks
