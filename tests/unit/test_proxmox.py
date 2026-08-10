"""The cluster backend's parsing and its safety guard, with no cluster."""

from __future__ import annotations

import shlex
from collections import Counter
from email.message import Message
import io
import struct
import urllib.error
import urllib.response
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from tests.vm import proxmox
from tests.vm.proxmox import (
    TAG,
    Api,
    CreateConflict,
    Guest,
    GuestSpec,
    ProxmoxError,
    ProxmoxNotFound,
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
    watch = Watchdog(log=log, counters=lambda: 0)
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

    def counters() -> int:
        moving[0] += QUIET_BYTES * 2
        return moving[0]

    watch = Watchdog(log=log, counters=counters)
    looks = [watch.moved() for _ in range(WATCH_STRIKES + 2)]
    assert looks == [True] * (WATCH_STRIKES + 2)
    assert not watch.stuck


def test_a_guest_moving_nothing_at_all_is_stuck(tmp_path: Path) -> None:
    """Console silent and counters flat: not slow, dead."""
    from tests.vm.cluster import WATCH_STRIKES, Watchdog

    log = tmp_path / "dead.log"
    log.write_bytes(b"")
    watch = Watchdog(log=log, counters=lambda: 5_000_000)
    quiet = [watch.moved() for _ in range(WATCH_STRIKES + 1)]
    assert quiet[1:] == [False] * WATCH_STRIKES
    assert watch.stuck


def test_a_stuck_guest_is_stopped_and_not_deleted_by_the_sweep(tmp_path: Path) -> None:
    """Stopping is what wakes the worker blocked on its console; the worker
    then reports and deletes. A sweep that deleted would race it."""
    from tests.vm.cluster import WATCH_STRIKES, Running, Watchdog, _sweep

    stopped: list[str] = []

    class Quiet:
        def stop(self) -> None:
            stopped.append("stopped")

        def destroy(self) -> None:
            raise AssertionError("the sweep must not delete a guest")

    log = tmp_path / "quiet.log"
    log.write_bytes(b"")
    watch = Watchdog(log=log, counters=lambda: 0, strikes=WATCH_STRIKES - 1)
    inflight = {"vm-zfs": Running(guest=Quiet(), watch=watch)}
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
                Node(name="big", free_bytes=NODE_HEADROOM_BYTES + guest * 3, cores=4),
                Node(name="one", free_bytes=NODE_HEADROOM_BYTES + guest, cores=4),
                Node(name="none", free_bytes=NODE_HEADROOM_BYTES + guest - 1, cores=4),
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

        def expect(self, pattern: str, timeout: float) -> bytes:
            assert pattern == "GNU GRUB", pattern
            return b"GNU GRUB  version 2.14"

        @property
        def closed(self) -> bool:
            return False

    console = Slow()
    screen = _editor_screen(console, 30.0)
    assert b"setparams" in screen
    # The leading ESC halts the countdown: GRUB stops it on the first key it
    # receives, and a run whose `e` arrived before the menu was drawn watched
    # the entry boot ten seconds later.
    assert console.sent == ["\x1b", "e", "\x1b", "e"]


def test_the_countdown_is_halted_before_the_first_e_is_sent() -> None:
    """A guest whose menu is never waited for boots the default entry: the
    press that would have stopped the countdown arrived before the menu."""
    from tests.vm.proxmox import _editor_screen

    class Menu:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.asked = False

        def send_raw(self, keys: str) -> None:
            # Everything before the menu is drawn is discarded, which is what
            # the guest's firmware does with it.
            if self.asked:
                self.sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            return GENTOO_EDITOR if self.sent else b""

        def send(self, line: str) -> None:
            self.sent.append(line)

        def expect(self, pattern: str, timeout: float) -> bytes:
            self.asked = True
            return b"GNU GRUB  version 2.14"

        @property
        def closed(self) -> bool:
            return False

    console = Menu()
    assert b"setparams" in _editor_screen(console, 30.0)
    assert console.sent[0] == "\x1b", console.sent


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

        def expect(self, pattern: str, timeout: float) -> bytes:
            if self.drops:
                self.drops -= 1
                raise ConsoleClosed("the guest closed the serial connection")
            return b"MARK_1_DONE"

    def open_console() -> Flaky:
        opened.append(1)
        return Flaky(drops=1 if len(opened) < 3 else 0)

    link = Reconnecting(open_console, tries=4)
    link.wait_for("sh install.sh", timeout=5.0)

    commands = [one for one in sent if "install.sh" in one]
    assert len(commands) == 1, commands
    assert len(opened) == 3, "one open, then one per drop"


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

        def expect(self, pattern: str, timeout: float) -> bytes:
            if self.drop:
                raise ConsoleClosed("dropped")
            return b"done"

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
            return [Node(name="one", free_bytes=NODE_HEADROOM_BYTES + guest * 3, cores=4)]

    api = Lagging(host="nowhere.invalid")
    assert len(free_slots(api)) == 3
    assert len(free_slots(api, {"one": 2})) == 1
    assert free_slots(api, {"one": 3}) == []
    assert free_slots(api, {"one": 9}) == [], "never negative"


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

    from tests.vm.results import console_command, read_console

    (tmp_path / "install.rc").write_bytes(b"0\n")
    (tmp_path / "install.txt").write_bytes(b"installed 53 operations\n")
    command = console_command(str(tmp_path))
    printed = subprocess.run(["sh", "-c", command], capture_output=True).stdout
    said = f"root@livecd ~ # {command}\r\n".encode() + printed
    assert read_console(said) == {
        "install.rc": b"0\n",
        "install.txt": b"installed 53 operations\n",
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

        def expect(self, pattern: str, timeout: float) -> bytes:
            return b"NETWORK_DOWN"

    monkeypatch.setattr(cluster, "NETWORK_PATIENCE", 0.3)
    monkeypatch.setattr(cluster, "NETWORK_PAUSE", 0.05)
    link = cluster.Reconnecting(Down, tries=1)
    with pytest.raises(ConsoleTimeout, match="no network"):
        cluster.wait_for_network(link)
    assert asked, "it asked at least once before giving up"


def test_the_network_wait_returns_as_soon_as_the_guest_answers() -> None:
    from tests.vm import cluster

    tries: list[int] = []

    class Late:
        def send(self, line: str) -> None:
            tries.append(1)

        def send_raw(self, keys: str) -> None:
            pass

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def expect(self, pattern: str, timeout: float) -> bytes:
            return b"NETWORK_DOWN" if len(tries) < 3 else b"NETWORK_UP"

    link = cluster.Reconnecting(Late, tries=1)
    cluster.wait_for_network(link)
    assert len(tries) == 3


def test_a_run_is_not_green_until_the_installed_system_answers() -> None:
    """The install finishing is half the question. A machine can reach a login
    prompt with the wrong filesystem mounted, no fstab and the wrong locale,
    and every check before this one would still be green."""
    import inspect

    from tests.vm import cluster

    source = inspect.getsource(cluster.install_one)
    assert "boot_and_check" in source
    ok = source.index("Verdict.OK")
    checked = source.index("boot_and_check")
    assert checked < ok, "the verdict cannot be OK before the system was read"


def test_every_question_asked_inside_names_what_would_fail_it() -> None:
    """A check with nothing to compare against passes on any machine, which is
    the shape a coverage claim hides behind."""
    from pathlib import Path as _Path

    from gentoo_install.exec.config import load
    from tests.vm.cluster import INSIDE, _asked_for

    # The exemption this test used to carry was the defect: `hostname` and
    # `kernel` were skipped for comparing against nothing, and they were the
    # two a guest could get wrong without failing.
    asked = _asked_for(load(_Path("tests/fixtures/vm-xfs.toml")))
    named = {name for name, _, _ in (*INSIDE, *asked)}
    assert {"os-release", "fstab", "locale", "hostname", "root filesystem", "init"} <= named
    for name, command, wanted in (*INSIDE, *asked):
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
    assert "ip -4 route show default" in cluster.ASK_FOR_IPV4


def test_no_marker_appears_in_the_command_that_prints_it() -> None:
    """The shell echoes the line it was given. A reader waiting for a marker
    matched the echo and returned before the work had started — twice: the
    result archive in ninety seconds, and the network probe on its first pass
    with no address on the interface at all."""
    from tests.vm import cluster
    from tests.vm.results import CONSOLE_CLOSE, CONSOLE_OPEN, console_command

    watched = (
        cluster.NETWORK_UP,
        cluster.NETWORK_DONE,
        CONSOLE_OPEN,
        CONSOLE_CLOSE,
        cluster._begin(7),
        cluster._done(7),
    )
    commands = (
        console_command("/tmp/results"),
        cluster.NETWORK_PROBE,
        cluster._marked("findmnt --output TARGET,SOURCE,FSTYPE", 7),
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

    command = cluster._marked("findmnt --output TARGET,SOURCE,FSTYPE", 7)
    # `sh -x`-free: the echo is what a terminal adds, so it is written here.
    said = subprocess.run(
        ["sh", "-c", f"printf '%s\n' {shlex.quote(command)}; {command}"],
        capture_output=True,
        check=False,
    ).stdout
    answer = said.split(cluster._begin(7).encode())[-1]
    answer = answer.split(cluster._done(7).encode())[0]
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


def test_an_encrypted_disk_is_unlocked_before_a_login_is_waited_for(
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
        """Answers with a passphrase prompt until one is sent, then `login:`."""

        def __init__(self) -> None:
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

        def expect(self, pattern: str, timeout: float) -> bytes:
            if DISK_PASSPHRASE in self.sent:
                return b"gentoo login:"
            return b"Enter passphrase for hd0,gpt2:"

    class Silent(Scripted):
        """A guest whose keys go through the API, not the serial port."""

        def send_keys(self, keys: list[str]) -> None:
            self.keys.extend(keys)

    for name, firmware in (
        ("vm-luks", BootFirmware.UEFI),
        ("vm-zfs-encrypted", BootFirmware.UEFI),
        ("vm-bios-luks", BootFirmware.BIOS),
    ):
        installation = load(Path("tests/fixtures") / f"{name}.toml")
        assert installation.bootloader.firmware is firmware, name
        console = Silent()
        link = Reconnecting(lambda: console, tries=1)
        # No wait for GRUB: the sleep is what the real path spends and this
        # test is not measuring it.
        monkeypatch.setattr(cluster, "GRUB_PROMPT_SECONDS", 0.0)
        said = cluster._unlock(console, link, installation)
        assert said == "", f"{name}: {said}"
        assert console.sent.count(DISK_PASSPHRASE) == 1, f"{name}: {console.sent}"
        if firmware is BootFirmware.BIOS:
            assert console.keys, f"{name}: nothing was typed at GRUB"
            assert console.keys[-1] == "ret", console.keys

    plain = load(Path("tests/fixtures/vm-binpkg.toml"))
    console = Silent()
    assert cluster._unlock(console, Reconnecting(lambda: console, tries=1), plain) == ""
    assert console.sent == [], "a plain disk was sent a passphrase"


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
        ["vm-binpkg", "vm-zfs", "vm-desktop", "vm-gnome", "ext4-bios"]
    )}
    for name in ("vm-desktop", "vm-gnome", "ext4-bios"):
        job = weights[name]
        assert job.heavy, name
        assert (job.cores, job.memory_mib) == (HEAVY_CORES, HEAVY_MEMORY_MIB), name
    for name in ("vm-binpkg", "vm-zfs"):
        job = weights[name]
        assert not job.heavy, name
        assert (job.cores, job.memory_mib) == (GUEST_CORES, GUEST_MEMORY_MIB), name


def test_a_node_with_one_light_slot_left_is_not_given_a_heavy_guest() -> None:
    """A heavy guest asks for twice the memory, so a slot list built from the
    light size does not answer for it."""
    from tests.vm.cluster import GUEST_MEMORY_MIB, NODE_HEADROOM_BYTES, room_for
    from tests.vm.cluster import fixtures as cluster_fixtures
    from tests.vm.proxmox import Node

    light, heavy = cluster_fixtures(["vm-binpkg", "vm-desktop"])
    one_slot = Node(
        name="infra-node1",
        free_bytes=NODE_HEADROOM_BYTES + GUEST_MEMORY_MIB * 1024**2,
        cores=4,
    )
    assert room_for(one_slot, light)
    assert not room_for(one_slot, heavy)

    two_slots = Node(
        name="infra-node2",
        free_bytes=NODE_HEADROOM_BYTES + 2 * GUEST_MEMORY_MIB * 1024**2,
        cores=4,
    )
    assert room_for(two_slots, heavy)


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

        def expect(self, pattern: str, timeout: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return not self.alive

    def open_console() -> Dropping:
        one = Dropping(alive=len(opened) > 0)
        opened.append(one)
        return one

    link = Reconnecting(open_console, tries=4)
    link.send("probe me")
    assert len(opened) == 2, "the closed console has to be replaced before the write"
    # `reopen` sends an empty line first, to make the shell draw a prompt.
    assert opened[1].sent == ["", "probe me"]
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


def test_transient_task_status_failures_do_not_abort_a_completed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two temporary 502 responses discarded a task that later reported OK."""
    answers: list[Any] = [
        ProxmoxError("GET status answered 502 Bad Gateway"),
        ProxmoxError("GET status answered 502 Bad Gateway"),
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
    """A same-name remote ISO was reused without evidence that its bytes were checked."""
    from tests.vm.cluster import prepare
    from tests.vm.driver import digest as driver_digest

    class Existing(Api):
        def __init__(self) -> None:
            pass

        def isos(self, node: str) -> list[str]:
            return ["minimal-a.iso", "driver.iso"]

        def upload_iso(self, node: str, path: Path, name: str) -> str:
            return name

    api = Existing()
    driver = tmp_path / "driver.iso"
    driver.write_bytes(b"driver")
    with pytest.raises(ProxmoxError, match="signed SHA-512 record"):
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

    stamp = tmp_path / "trust" / "remote" / "node" / "minimal-a.iso.sha512"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("a" * 128)
    with pytest.raises(ProxmoxError, match="driver SHA-256 record"):
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
    driver_stamp = tmp_path / "trust" / "remote" / "node" / "driver.iso.sha256"
    driver_stamp.write_text(driver_digest(driver))
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
                    raise ProxmoxError("DELETE answered 503 storage busy")
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


def test_a_console_uses_the_cookie_returned_with_its_own_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another worker replaced the shared affinity cookie between ticket and connect."""
    captured: dict[str, str] = {}

    class Tickets(Api):
        def __init__(self) -> None:
            self.host = "pve.invalid"
            self.affinity = "cookie-before"

        def call_with_affinity(self, method: str, path: str, **form: Any) -> tuple[Any, str]:
            self.affinity = "INGRESSCOOKIE=worker-b"
            return {"port": 1, "ticket": "ticket-a", "user": "root@pam"}, "INGRESSCOOKIE=worker-a"

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
        captured.update(headers)
        return Framed()

    monkeypatch.setattr(WebSocket, "connect", connect)
    monkeypatch.setattr(proxmox, "_secret", lambda: "secret")
    proxmox.ConsoleChannel.open(Tickets(), "node", 9300, tries=1)
    assert captured["Cookie"] == "INGRESSCOOKIE=worker-a"


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

        def expect(self, pattern: str, timeout: float) -> bytes:
            if not self.asked:
                self.asked = True
                return b"Load keymap (Enter for default): "
            return b"livecd ~ # "

        @property
        def closed(self) -> bool:
            return False

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

        def expect(self, pattern: str, timeout: float) -> bytes:
            return b"livecd ~ # "

        @property
        def closed(self) -> bool:
            return False

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
    )
    from tests.vm.proxmox import VMID_FIRST, VMID_LAST

    assert static_address(VMID_FIRST) == "10.31.0.150"
    assert static_address(VMID_LAST) == "10.31.0.249"
    # One address per guest, and no two the same.
    every = {static_address(one) for one in range(VMID_FIRST, VMID_LAST + 1)}
    assert len(every) == VMID_LAST - VMID_FIRST + 1

    command = configure_statically("10.31.0.113")
    assert "\n" not in command, "one line: this goes to a serial console"
    # Probed before it is taken: an address somebody else holds is a collision
    # with a real machine, so that guest asks the DHCP server instead.
    assert "arping -D" in command
    assert command.index("arping -D") < command.index("addr add")
    # Walks forward instead of asking the DHCP server: this segment carries
    # other people's machines and that server answers intermittently.
    assert "n=$((n + 1))" in command, command
    assert f"via {GUEST_GATEWAY}" in command
    for one in GUEST_RESOLVERS:
        assert f"nameserver {one}" in command


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
    # The guard comes first, and nothing else runs when a route is already there.
    assert command.startswith("ip -4 route show default | grep -q . || {"), command[:80]
    assert command.index("arping") > command.index("|| {")
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
    assert "n=155;" in command, command
    # Bounded: the walk stops rather than running off the end of the network.
    assert "-le 249" in command, command
