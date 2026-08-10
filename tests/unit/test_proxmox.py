"""The cluster backend's parsing and its safety guard, with no cluster."""

from __future__ import annotations

import struct
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from tests.vm import proxmox
from tests.vm.proxmox import TAG, Api, Guest, GuestSpec, ProxmoxError, _line_of_linux
from tests.vm.websocket import WebSocket, _client_frame

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

    def call(self, method: str, path: str, **form: Any) -> Any:
        self.asked.append((method, path))
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
    api = _Recording({"config": {"name": "gi-run", "tags": TAG}})
    guest = Guest(api=api, node="infra-node4", vmid=9300, spec=GuestSpec(name="x", iso="x"))
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

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    api = Api(host="nowhere.invalid")
    api.call("DELETE", "/nodes/n/qemu/9300", purge=1)
    removed = (seen["url"], seen["body"])
    api.call("POST", "/nodes/n/qemu", vmid=9300)
    created = (seen["url"], seen["body"])

    assert removed == ("https://nowhere.invalid/api2/json/nodes/n/qemu/9300?purge=1", None)
    assert created == ("https://nowhere.invalid/api2/json/nodes/n/qemu", b"vmid=9300")


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
    assert names == ["big", "big", "big", "one"]
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
            raise AssertionError("the editor is read by snapshot, not by expect")

    console = Slow()
    screen = _editor_screen(console, 30.0)
    assert b"setparams" in screen
    assert console.sent == ["e", "\x1b", "e"]


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

        def expect(self, pattern: str, timeout: float) -> bytes:
            if self.drop:
                raise ConsoleClosed("dropped")
            return b"done"

    opens = [Once(drop=True), Once(drop=False)]
    link = Reconnecting(lambda: opens.pop(0), tries=3)
    link.run("mkdir -p /mnt/driver")
    assert [one for one in sent if "mkdir" in one].__len__() == 2
