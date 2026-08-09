"""The cluster backend's parsing and its safety guard, with no cluster."""

from __future__ import annotations

import struct
import urllib.request
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
