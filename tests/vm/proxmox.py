"""Test machines on a Proxmox VE cluster, driven entirely through its API.

The workstation cannot host the campaign: five guests share its memory with an
editor and a test suite, `earlyoom` takes one whenever the total crosses its
threshold, and hourly ZFS snapshots pin every disk image that is deleted. A
green run there is not evidence.

Everything here goes over HTTPS. Port 22 is closed on the cluster, so the
node's serial socket is unreachable as a file and `termproxy` is the console:
it answers with a ticket, and the ticket opens a websocket carrying the same
bytes the local harness reads off a unix socket.

The token is an administrator of a cluster running other people's machines.
Nothing here names a VM it did not create, and every destructive call names one
VMID.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from .console import ConsoleClosed, ConsoleTimeout, SerialConsole
from .monitor import keys_for
from .websocket import Framed, WebSocket, WebSocketError

#: Where the secret lives. Read from the file at every call site; it never
#: reaches a command line, an environment variable, or a log.
TOKEN_FILE: Final[Path] = Path.home() / ".ssh/chris"
TOKEN_ID: Final[str] = "zakk@authentik!gentoo-install"
HOST: Final[str] = "pve.infra.plz.ac"

#: The API answers on 443 behind a proxy, not on Proxmox's own 8006.
PORT: Final[int] = 443

#: The range this harness allocates from. Chosen because nothing on the cluster
#: uses it: 9000, 9002, 9200 and 9201 are somebody else's, and one of them is a
#: production template. A range alone is not proof of ownership, which is what
#: `TAG` is for.
VMID_FIRST: Final[int] = 9300
VMID_LAST: Final[int] = 9399

#: Written onto every guest at creation and required before anything is
#: deleted. `9002` is `prod-debian-12-server-template`: reading a VMID range as
#: ownership would have offered a production machine up for removal.
TAG: Final[str] = "gentoo-install-test"

#: Where disks go. `local` is per node and holds the ISOs; `ceph-pve` is shared,
#: so a guest can be built on whichever node has the memory.
#: How many times a create is attempted when the shared storage lock is held,
#: and how long between them. Thirteen guests built at once contend on
#: `ceph-pve`; the lock is another create's, and it is gone in seconds.
CREATE_TRIES: Final[int] = 4
CREATE_PAUSE: Final[float] = 10.0

DISK_STORAGE: Final[str] = "ceph-pve"
ISO_STORAGE: Final[str] = "local"

#: Long enough for a stage3 to extract over a slow mirror, short enough that a
#: node that stopped answering does not hold the whole campaign.
API_TIMEOUT: Final[float] = 60.0
TASK_POLL: Final[float] = 2.0
CLEANUP_PAUSE: Final[float] = 2.0
CLEANUP_PATIENCE: Final[float] = 300.0


class ProxmoxError(Exception):
    """The cluster refused a call, or a task it accepted did not finish."""


class ProxmoxNotFound(ProxmoxError):
    """The requested cluster object no longer exists."""


class CreateConflict(ProxmoxError):
    """A VMID became occupied before this guest was created."""


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    """API credentials never follow an HTTP redirect."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _transient(error: ProxmoxError) -> bool:
    said = str(error).lower()
    return "did not answer" in said or any(
        f"answered {code}" in said for code in (429, 500, 502, 503, 504)
    )


def _certificates() -> ssl.SSLContext:
    """The system trust store, verified.

    The comment this replaced said the cluster serves a certificate its own CA
    signed. It does not: `pve.infra.plz.ac` is issued by Let's Encrypt and the
    default context verifies it. Turning verification off sent an
    administrator token for a cluster of seventy-nine machines to whatever
    answered on port 443.
    """
    return ssl.create_default_context()


def _secret() -> str:
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError as error:
        raise ProxmoxError(f"the API token is not readable at {TOKEN_FILE}: {error}") from error


@dataclass(frozen=True)
class Node:
    name: str
    free_bytes: int
    cores: int


class Api:
    """One authenticated conversation with the cluster."""

    def __init__(self, host: str = HOST) -> None:
        self.host = host
        self._context = _certificates()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._context), _RejectRedirect()
        )
        #: The cluster sits behind a load balancer that spreads requests over
        #: every node. A `termproxy` ticket is valid on the node that issued
        #: it, so a websocket landing anywhere else is answered `502 Bad
        #: Gateway`. Holding the balancer's own affinity cookie pins both
        #: halves to one backend.
        self.affinity: str = ""

    def _remember(self, headers: Any) -> str:
        for name, value in headers.items():
            if name.lower() == "set-cookie" and value.startswith("INGRESSCOOKIE="):
                return str(value).split(";", 1)[0]
        return ""

    def call(self, method: str, path: str, **form: Any) -> Any:
        data, affinity = self.call_with_affinity(method, path, **form)
        if affinity:
            self.affinity = affinity
        return data

    def call_with_affinity(self, method: str, path: str, **form: Any) -> tuple[Any, str]:
        # A body on DELETE is answered `501 Unexpected content for method`, so
        # the two methods that take no body carry their parameters in the URL.
        body: bytes | None = None
        if form and method in ("GET", "DELETE"):
            joiner = "&" if "?" in path else "?"
            path = f"{path}{joiner}{urllib.parse.urlencode(form)}"
        elif form:
            body = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(
            f"https://{self.host}/api2/json{path}", data=body, method=method
        )
        request.add_header("Authorization", f"PVEAPIToken={TOKEN_ID}={_secret()}")
        affinity = self.affinity
        if affinity:
            request.add_header("Cookie", affinity)
        try:
            with self._opener.open(request, timeout=API_TIMEOUT) as answer:
                remembered = self._remember(answer.headers)
                said = json.load(answer)
                data = said.get("data")
                reason = str(said.get("message", "")).strip()
                # 200 with `data: null` and a `message` is how this API reports
                # `invalid bootorder: device 'virtio0' does not exist`, and
                # reading only `data` threw that away: four finished installs
                # were reported as a request that never started. A success
                # answers `{"data": null}` with no message at all.
                if data is None and reason:
                    raise ProxmoxError(f"{method} {path} answered {reason}")
                return data, remembered or affinity
        except urllib.error.HTTPError as error:
            # The reason, not only the body: Proxmox answers `500` with
            # `{"data":null}` and puts what went wrong in the status line.
            said = error.read().decode("utf-8", "replace").strip()[:300]
            exception = ProxmoxNotFound if error.code == 404 else ProxmoxError
            raise exception(
                f"{method} {path} answered {error.code} {error.reason}: {said}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProxmoxError(f"{method} {path} did not answer: {error}") from error

    def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
        """Block until a task finishes, and raise unless it finished cleanly.

        The node comes out of the UPID rather than from the caller: a task
        started through the load balancer runs on whichever backend answered,
        and asking the wrong node for its status is a 500 on a task that is
        running perfectly well.
        """
        if not upid:
            # The API answers an accepted request with the task id, and a
            # `data: null` where one belongs is a request that did not start.
            # Splitting it raised `AttributeError: 'NoneType'`, which the
            # worker reported instead of the failure that caused it.
            raise ProxmoxError(f"{node} answered without a task id; the request did not start")
        parts = upid.split(":")
        if len(parts) > 1 and parts[0] == "UPID" and parts[1]:
            node = parts[1]
        quoted = urllib.parse.quote(upid, safe="")
        deadline = time.monotonic() + patience
        while time.monotonic() < deadline:
            try:
                status = self.call("GET", f"/nodes/{node}/tasks/{quoted}/status")
            except ProxmoxError as error:
                if not _transient(error):
                    raise
                time.sleep(min(TASK_POLL, max(0.0, deadline - time.monotonic())))
                continue
            if status.get("status") == "stopped":
                exit_status = status.get("exitstatus", "")
                if exit_status != "OK":
                    raise ProxmoxError(f"{upid} ended with {exit_status!r}")
                return
            time.sleep(min(TASK_POLL, max(0.0, deadline - time.monotonic())))
        raise ProxmoxError(f"{upid} did not finish within {patience:.0f}s")

    def nodes(self) -> list[Node]:
        found = [
            Node(
                name=one["node"],
                free_bytes=int(one.get("maxmem", 0)) - int(one.get("mem", 0)),
                cores=int(one.get("maxcpu", 0)),
            )
            for one in self.call("GET", "/nodes")
            if one.get("status") == "online"
        ]
        return sorted(found, key=lambda one: one.free_bytes, reverse=True)

    def ours(self) -> list[tuple[str, int]]:
        """Every machine this harness created, as `(node, vmid)`.

        Both conditions, not either: the tag says the harness made it and the
        range says the harness would have. Read from the cluster rather than
        from a local list, because a run killed between creating a guest and
        recording it would otherwise leave it holding a node's memory for ever.
        """
        return sorted(
            (one["node"], int(one["vmid"]))
            for one in self.call("GET", "/cluster/resources?type=vm")
            if VMID_FIRST <= int(one["vmid"]) <= VMID_LAST
            and TAG in str(one.get("tags", "")).split(";")
        )

    def taken(self) -> set[int]:
        """Every VMID on the cluster. Allocation reads all of them, not only
        the ones this harness owns."""
        return {int(one["vmid"]) for one in self.call("GET", "/cluster/resources?type=vm")}

    def free_vmid(self, held: frozenset[int] = frozenset()) -> int:
        used = self.taken() | held
        for candidate in range(VMID_FIRST, VMID_LAST + 1):
            if candidate not in used:
                return candidate
        raise ProxmoxError(f"every VMID from {VMID_FIRST} to {VMID_LAST} is in use")

    def isos(self, node: str) -> list[str]:
        content = self.call("GET", f"/nodes/{node}/storage/{ISO_STORAGE}/content?content=iso")
        return sorted(str(one["volid"]).split("/")[-1] for one in content)

    def upload_iso(self, node: str, path: Path, name: str) -> str:
        """Put a file on a node's `local` storage and answer its name there.

        `iso` because that is what the endpoint takes: `snippets` is refused
        with `value 'snippets' does not have a value in the enumeration 'iso,
        vztmpl, import'`. The driver CD really is an ISO; nothing else here
        pretends to be one.
        """
        boundary = "----" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="content"\r\n\r\niso\r\n'
        ).encode()
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="filename"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        body += path.read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            f"https://{self.host}/api2/json/nodes/{node}/storage/{ISO_STORAGE}/upload",
            data=body,
            method="POST",
        )
        request.add_header("Authorization", f"PVEAPIToken={TOKEN_ID}={_secret()}")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        if self.affinity:
            request.add_header("Cookie", self.affinity)
        try:
            with self._opener.open(request, timeout=600.0) as answer:
                upid = json.load(answer).get("data")
        except urllib.error.HTTPError as error:
            said = error.read().decode("utf-8", "replace").strip()[:300]
            raise ProxmoxError(f"{name} was not uploaded: {error.code} {said}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProxmoxError(f"{name} was not uploaded: {error}") from error
        if isinstance(upid, str) and upid.startswith("UPID"):
            self.wait(node, upid, patience=600.0)
        return name

    def remove_iso(self, node: str, name: str) -> str:
        """Drop an uploaded file, and answer why if it stayed.

        A driver CD per run fills a 33 GiB store, so a failure here matters,
        but it must not fail a run whose install already finished. The reason
        is returned rather than swallowed: the first one left a file behind
        and said nothing at all.
        """
        try:
            self.call(
                "DELETE", f"/nodes/{node}/storage/{ISO_STORAGE}/content/{ISO_STORAGE}:iso/{name}"
            )
        except ProxmoxError as error:
            return str(error)
        return ""

    def fetch_iso(self, node: str, url: str, filename: str, sha512: str) -> None:
        """Have the node download an install medium itself.

        The cluster is in China and the workstation is not, so the node reaches
        a Chinese mirror far faster than an upload from here would, and the
        bytes never cross the workstation's link at all.
        """
        if filename in self.isos(node):
            return
        upid = self.call(
            "POST",
            f"/nodes/{node}/storage/{ISO_STORAGE}/download-url",
            url=url,
            filename=filename,
            content="iso",
            checksum=sha512,
            **{"checksum-algorithm": "sha512"},
        )
        self.wait(node, upid, patience=3600.0)


@dataclass
class GuestSpec:
    """What to build. Sizes are what the API wants: MiB for memory, GiB for a disk."""

    name: str
    iso: str
    memory_mib: int = 8192
    cores: int = 4
    #: One entry per target disk, in the order the fixture names them.
    target_gib: tuple[int, ...] = (40,)
    uefi: bool = True
    #: A second CD, already uploaded, carrying the installer built from the tree.
    driver_iso: str = ""
    #: Boot the installed disk instead of the medium. The medium stays attached
    #: so a failed boot can be looked at without rebuilding the guest.
    boot_installed: bool = False
    #: Written beside the tag and required before this guest is removed. Two
    #: campaigns can pick the same free VMID in the same second, and the tag
    #: alone then let each of them delete the other's machine.
    nonce: str = ""


@dataclass
class Guest:
    """A machine on the cluster, and the only thing allowed to delete it."""

    api: Api
    node: str
    vmid: int
    spec: GuestSpec
    _booted: bool = field(default=False, init=False)
    _create_conflict: bool = field(default=False, init=False)

    def create(self) -> None:
        options: dict[str, Any] = {
            "vmid": self.vmid,
            "name": self.spec.name[:63],
            "memory": self.spec.memory_mib,
            "cores": self.spec.cores,
            "sockets": 1,
            "cpu": "host",
            "ostype": "l26",
            "machine": "q35",
            "scsihw": "virtio-scsi-single",
            "serial0": "socket",
            # Under OVMF the serial port is the whole display, and the firmware
            # and GRUB both write to it. SeaBIOS hands over to a GRUB that wants
            # a framebuffer, and with no VGA device to find it stopped writing
            # anywhere at all: the log ended at `Welcome to GRUB!` and the
            # console was dropped. A BIOS guest gets a real VGA to draw on and
            # keeps the serial port for the kernel.
            "vga": "serial0" if self.spec.uefi else "std",
            "net0": "virtio,bridge=vmbr0",
            "onboot": 0,
            # Free page reporting hands memory back that a guest filled with
            # page cache. Without it four guests hold their whole allocation.
            "balloon": self.spec.memory_mib,
            "agent": 0,
            "tags": f"{TAG};{self.spec.nonce}" if self.spec.nonce else TAG,
        }
        if self.spec.uefi:
            options["bios"] = "ovmf"
            # Boot entries an install writes live here, and without a volume
            # OVMF keeps nothing: the second boot would find no entry.
            options["efidisk0"] = f"{DISK_STORAGE}:1,efitype=4m,pre-enrolled-keys=0"
        for index, size in enumerate(self.spec.target_gib):
            # `serial=` is what puts the disk under `/dev/disk/by-id/`, and
            # the fixtures name their targets there. Without it preflight
            # answers `virtio-target0 is not present on this machine`.
            options[f"virtio{index}"] = (
                f"{DISK_STORAGE}:{size},discard=on,serial=target{index}"
            )
        options["ide2"] = f"{ISO_STORAGE}:iso/{self.spec.iso},media=cdrom"
        if self.spec.driver_iso:
            options["ide3"] = f"{ISO_STORAGE}:iso/{self.spec.driver_iso},media=cdrom"
        options["boot"] = "order=" + ("virtio0" if self.spec.boot_installed else "ide2")
        # Retried on a storage lock: thirteen guests built at once contend on
        # `ceph-pve`, and one round lost a fixture to `cfs-lock
        # 'storage-ceph-pve' error: got lock request timeout`. The lock is
        # held by another create, not by anything wrong with this one.
        last: ProxmoxError | None = None
        for attempt in range(CREATE_TRIES):
            try:
                self.api.wait(
                    self.node, self.api.call("POST", f"/nodes/{self.node}/qemu", **options)
                )
                return
            except ProxmoxError as error:
                if "already exists" in str(error).lower():
                    self._create_conflict = True
                    raise CreateConflict(str(error)) from error
                if "lock request timeout" not in str(error):
                    raise
                last = error
                time.sleep(CREATE_PAUSE * (attempt + 1))
        assert last is not None
        raise last

    def start(self) -> None:
        upid = self.api.call("POST", f"/nodes/{self.node}/qemu/{self.vmid}/status/start")
        self._booted = True
        self.api.wait(self.node, upid)

    def boot_from_disk(self) -> None:
        """Point the firmware at the installed disk and forget the medium.

        The install is only half the question: the other half is whether the
        machine it produced comes up and carries the settings that were asked
        for. `order=virtio0` is what makes the firmware try the target rather
        than the CD it was built with.
        """
        upid = self.api.call(
            "PUT", f"/nodes/{self.node}/qemu/{self.vmid}/config", boot="order=virtio0"
        )
        # A config change the API applies then and there answers `data: null`;
        # only a deferred one comes back as a task. Waiting on the empty answer
        # ended four installs that had already finished and collected their
        # results.
        if upid:
            self.api.wait(self.node, upid)

    def reset(self) -> None:
        """Boot the firmware again with somebody already reading.

        `termproxy` forwards only what arrives after it attaches, and attaching
        takes about five seconds: OVMF and GRUB had both finished by then, the
        guest sat at a prompt on a console with no VGA device to show it, and
        the run read an empty serial port for five minutes. Resetting once the
        console is up puts the whole boot on the wire.
        """
        self.api.wait(
            self.node, self.api.call("POST", f"/nodes/{self.node}/qemu/{self.vmid}/status/reset")
        )

    def transferred(self) -> int | None:
        """Bytes this guest has received and written since it started.

        What the watchdog reads when the console is silent: an install
        downloading a stage3 prints nothing for minutes, and ending it for
        that would end the guests doing the most work.
        """
        try:
            status = self.api.call("GET", f"/nodes/{self.node}/qemu/{self.vmid}/status/current")
        except ProxmoxError:
            # One unanswered request is not evidence about the guest, and a
            # watchdog that raises here stops the whole schedule.
            return None
        return int(status.get("netin", 0)) + int(status.get("diskwrite", 0))

    def running(self) -> bool:
        status = self.api.call("GET", f"/nodes/{self.node}/qemu/{self.vmid}/status/current")
        return str(status.get("status")) == "running"

    def console(self) -> ConsoleChannel:
        return ConsoleChannel.open(self.api, self.node, self.vmid)

    def screenshot(self, into: Path) -> Path | None:
        """What is on the VGA console. A BIOS guest asking GRUB's own
        passphrase prompt writes nothing to the serial port, so a run that
        looks hung is diagnosed here or not at all."""
        try:
            raw = self.api.call("GET", f"/nodes/{self.node}/qemu/{self.vmid}/screenshot")
        except ProxmoxError:
            return None
        if not isinstance(raw, str):
            return None
        into.write_text(raw)
        return into

    #: Between keystrokes, and how many times one is repeated. Every key is a
    #: separate request over a new TLS connection, and twenty of them in a row
    #: were answered `Remote end closed connection without response`; a dropped
    #: key silently corrupts a command line nothing can read back.
    KEY_PAUSE: Final[float] = 0.12
    KEY_TRIES: Final[int] = 4

    def send_keys(self, keys: list[str]) -> None:
        for key in keys:
            last = ""
            for attempt in range(self.KEY_TRIES):
                try:
                    self.api.call(
                        "PUT", f"/nodes/{self.node}/qemu/{self.vmid}/sendkey", key=key
                    )
                    break
                except ProxmoxError as error:
                    last = str(error)
                    self.api.affinity = ""
                    time.sleep(0.5 * (attempt + 1))
            else:
                raise ProxmoxError(f"{key!r} was not delivered to vm {self.vmid}: {last}")
            time.sleep(self.KEY_PAUSE)

    def stop(self) -> None:
        try:
            status = self.api.call(
                "GET", f"/nodes/{self.node}/qemu/{self.vmid}/status/current"
            )
            if str(status.get("status")) != "running":
                self._booted = False
                return
            self.api.wait(
                self.node,
                self.api.call("POST", f"/nodes/{self.node}/qemu/{self.vmid}/status/stop"),
                patience=180.0,
            )
        except ProxmoxNotFound:
            self._booted = False
            return
        except ProxmoxError:
            # Already down, or the task record expired. Either way `destroy`
            # is what has to happen next, and it must not be skipped.
            pass
        self._booted = False

    def destroy(self, patience: float = CLEANUP_PATIENCE) -> None:
        """Remove the machine and its disks.

        The tag is checked first. This token administers a cluster running
        other people's work, and a VMID is not proof of ownership: the range
        this harness allocates from already held a production template.
        """
        if self._create_conflict:
            return
        if not VMID_FIRST <= self.vmid <= VMID_LAST:
            # The tag is ours to write, so a machine outside the range that
            # carries it is not evidence: the range is the second guard, and
            # `9002` in this cluster is `prod-debian-12-server-template`.
            raise ProxmoxError(
                f"vm {self.vmid} is outside {VMID_FIRST}-{VMID_LAST}; refusing to remove it"
            )
        deadline = time.monotonic() + patience
        last = "the guest still exists"
        while time.monotonic() <= deadline:
            try:
                config = self.api.call(
                    "GET", f"/nodes/{self.node}/qemu/{self.vmid}/config"
                )
            except ProxmoxNotFound:
                return
            except ProxmoxError as error:
                last = str(error)
                # A guest whose config file is gone has been removed. The API
                # reports that as 500 rather than 404, which `_transient` reads
                # as retry-worthy, so the loop otherwise spent its whole
                # patience re-asking about a guest that no longer existed and
                # then reported the slot as still held.
                if f"qemu-server/{self.vmid}.conf' does not exist" in last:
                    return
                if not _transient(error):
                    raise
                time.sleep(min(CLEANUP_PAUSE, max(0.0, deadline - time.monotonic())))
                continue
            tags = str(config.get("tags", "")).split(";")
            if TAG not in tags:
                raise ProxmoxError(
                    f"vm {self.vmid} on {self.node} is not tagged {TAG!r}; refusing to remove it"
                )
            if not self.spec.nonce or self.spec.nonce not in tags:
                raise ProxmoxError(
                    f"vm {self.vmid} on {self.node} is not the guest this run built; "
                    "refusing to remove it"
                )
            try:
                self.stop()
                self.api.wait(
                    self.node,
                    self.api.call(
                        "DELETE",
                        f"/nodes/{self.node}/qemu/{self.vmid}",
                        **{"destroy-unreferenced-disks": 1, "purge": 1},
                    ),
                    patience=max(0.0, deadline - time.monotonic()),
                )
            except ProxmoxNotFound:
                return
            except ProxmoxError as error:
                last = str(error)
            time.sleep(min(CLEANUP_PAUSE, max(0.0, deadline - time.monotonic())))
        raise ProxmoxError(f"vm {self.vmid} on {self.node} was not removed: {last}")


class ConsoleChannel:
    """The serial console as a `console.Channel`.

    Proxmox frames what it carries: `0:<bytes>:<data>` is input, a bare `2` is
    the keepalive its proxy expects, and what comes back is the console itself
    with no framing of its own.
    """

    #: Proxmox's own terminal proxy drops a connection that says nothing.
    KEEPALIVE: Final[float] = 20.0

    def __init__(self, socket: Framed) -> None:
        self._socket = socket
        self._last_said = time.monotonic()

    @classmethod
    def open(cls, api: Api, node: str, vmid: int, tries: int = 8) -> ConsoleChannel:
        """A ticket, then the websocket it opens.

        `termproxy` refuses a guest that is not running yet and answers 500
        after its own five-second wait, so this retries rather than failing a
        run that was one second early.

        Each retry drops the balancer's affinity cookie first. The failure is
        intermittent and follows which backend the cookie pinned: the same call
        answered 500 five times running on one backend and succeeded first try
        on the next, so retrying without moving would just wait.
        """
        last = ""
        for attempt in range(tries):
            try:
                ticket, affinity = api.call_with_affinity(
                    "POST", f"/nodes/{node}/qemu/{vmid}/termproxy"
                )
            except ProxmoxError as error:
                last = str(error)
                api.affinity = ""
                time.sleep(2.0 * (attempt + 1))
                continue
            path = (
                f"/api2/json/nodes/{node}/qemu/{vmid}/vncwebsocket"
                f"?port={ticket['port']}"
                f"&vncticket={urllib.parse.quote(ticket['ticket'], safe='')}"
            )
            headers = {"Authorization": f"PVEAPIToken={TOKEN_ID}={_secret()}"}
            if affinity:
                headers["Cookie"] = affinity
            try:
                socket = WebSocket.connect(
                    api.host, path, headers, port=PORT, context=_certificates()
                )
            except WebSocketError as error:
                last = str(error)
                api.affinity = ""
                time.sleep(2.0 * (attempt + 1))
                continue
            socket.settimeout(1.0)
            # The proxy authenticates the websocket separately from the
            # request: without this line it accepts the upgrade and then says
            # nothing at all.
            socket.send(f"{ticket['user']}:{ticket['ticket']}\n".encode())
            return cls(socket)
        raise ProxmoxError(f"no console for vm {vmid} on {node}: {last}")

    def recv(self, size: int) -> bytes:
        got = self._socket.read()
        now = time.monotonic()
        if not got and now - self._last_said > self.KEEPALIVE:
            self._socket.send(b"2")
            self._last_said = now
        return got

    def sendall(self, data: bytes) -> None:
        self._socket.send(f"0:{len(data)}:".encode() + data)
        self._last_said = time.monotonic()

    @property
    def closed(self) -> bool:
        return self._socket.closed

    def close(self) -> None:
        self._socket.close()


#: What GRUB's editor answers to, as single control characters. Arrow keys are
#: multi-byte escape sequences whose terminfo GRUB has to agree about; these
#: are one byte each and GRUB reads them the same on every terminal.
GRUB_NEXT_LINE: Final[str] = "\x0e"
GRUB_END_OF_LINE: Final[str] = "\x05"
GRUB_BOOT: Final[str] = "\x18"


#: Down then up: the highlight ends where it started, and GRUB stops its
#: countdown on any key at all.
GRUB_HOLD: Final[str] = "\x0e\x10"

#: GRUB's own countdown line, and the only text of its menu that reaches a
#: serial console: the Gentoo medium sets a graphical theme, so on serial GRUB
#: draws the entries by cursor position and prints nothing else worth matching.
#: Matching an entry title instead matched `Booting \`Boot LiveCD'`, which is
#: printed after the countdown has already run out.
GRUB_COUNTDOWN: Final[str] = r"highlighted entry|GNU GRUB|Minimal BASH-like"


def hold_the_menu(console: Line, timeout: float = 300.0) -> bytes:
    """Wait for GRUB's menu and stop its countdown.

    Waiting first, rather than pressing early: a key sent before the menu is
    drawn is consumed the moment it appears, the countdown never prints, and
    there is then nothing to match on.
    """
    seen = console.expect(GRUB_COUNTDOWN, timeout=timeout)
    console.send_raw(GRUB_HOLD)
    return seen


def append_to_cmdline(console: Line, extra: str, timeout: float = 30.0) -> None:
    """Add kernel parameters to the highlighted GRUB entry and boot it.

    The medium's own entry writes to the firmware console and says nothing more
    once Linux takes over, so a run that boots correctly looks hung after
    `Booting`. The local harness passes `-kernel` and `-append` straight to
    qemu; the cluster answers 500 to `args` for anything but root, so the
    parameters go in the way an operator would add them.

    Which line to edit is counted off the editor's own screen rather than
    assumed: the first medium tried had `search` above `linux`, one Ctrl-N
    landed on it, and the entry booted unchanged with a broken search line.
    """
    hold_the_menu(console)
    screen = _editor_screen(console, timeout)
    down = _line_of_linux(screen)
    console.send_raw(GRUB_NEXT_LINE * down + GRUB_END_OF_LINE)
    time.sleep(0.5)
    console.send_raw(f" {extra}")
    time.sleep(0.5)
    console.send_raw(GRUB_BOOT)


#: How GRUB places each line it draws: `ESC[<row>;<column>H` and then the text.
#: Read rather than counted, because the entries differ between media: the
#: Gentoo medium has `search` above `linux` and one Ctrl-N landed on it, which
#: booted the entry unedited with a broken search line and no serial output.
_PLACED: Final[re.Pattern[bytes]] = re.compile(rb"\x1b\[(\d+);\d+H([^\x1b]*)")


#: How long a SeaBIOS guest takes from reset to a GRUB menu waiting for a key.
#: Nothing announces it: the firmware and GRUB both draw on the VGA this guest
#: has, and the serial port stays silent until the kernel is told to use it.
#: The medium's menu waits ten seconds, so this has to land inside that.
BIOS_MENU_DELAY: Final[float] = 12.0

#: Which line below `setparams` the keys move to, tried in this order. The
#: Gentoo minimal ISO puts `search` between `setparams` and `linux`, so two is
#: right for it; a medium that lacks it needs one. Nothing can be read to
#: decide, so each is tried and checked.
BIOS_DOWN: Final[tuple[int, ...]] = (2, 1, 3)

#: What the kernel prints once `console=ttyS0` is on its command line, and the
#: proof that the edit landed on the right line.
KERNEL_SPEAKS: Final[str] = r"Linux version|Command line:|\[    0\.000000\]"


def append_to_cmdline_blind(
    guest: Guest, link: "Reopenable", extra: str, patience: float = 60.0
) -> None:
    """The same edit on a guest whose GRUB writes only to VGA.

    Under OVMF, GRUB inherits the firmware's serial console and the menu can be
    read. Under SeaBIOS it clears the terminal and switches to its own
    framebuffer, so the serial log stops at `Welcome to GRUB!` and the whole
    install reads as hung. The keys go through the API instead.

    Nothing can be read back, so the check is the kernel itself: with
    `console=ttyS0` on the command line it has to speak. A count that landed on
    the wrong line leaves it silent, and the guest is reset and tried again.
    """
    console = link.console
    last = ""
    for attempt, down in enumerate(BIOS_DOWN):
        # Timed, not read: this guest's GRUB draws on VGA and says nothing on
        # the serial port, so there is no menu to wait for.
        time.sleep(BIOS_MENU_DELAY)
        guest.send_keys(["e"])
        time.sleep(2.0)
        guest.send_keys(["ctrl-n"] * down + ["ctrl-e"])
        time.sleep(1.0)
        guest.send_keys(keys_for(f" {extra}"))
        time.sleep(1.0)
        guest.send_keys(["ctrl-x"])
        try:
            console.expect(KERNEL_SPEAKS, timeout=patience)
            return
        except (ConsoleTimeout, ConsoleClosed) as error:
            last = str(error)[:200]
        if attempt + 1 < len(BIOS_DOWN):
            # A reset drops the console with it, so the next attempt reads a
            # new one: the first guest to take a second attempt reported `the
            # guest closed the serial connection` a minute in.
            guest.reset()
            link.reopen()
            console = link.console
    raise ProxmoxError(f"the kernel never spoke after editing GRUB blind: {last}")


class Line(Protocol):
    """What driving a guest needs of a console: keys in, text out.

    A protocol rather than `SerialConsole`, because a cluster run drives a
    console that reopens itself when the cluster drops it, and because a test
    can then script one.
    """

    def send(self, line: str) -> None: ...

    def send_raw(self, keys: str) -> None: ...

    def snapshot(self, seconds: float) -> bytes: ...

    def expect(self, pattern: str, timeout: float) -> bytes: ...

    @property
    def closed(self) -> bool: ...


class Reopenable(Protocol):
    """A console that can be replaced with another one to the same guest. A
    reset drops the console with it, and the next attempt needs a live one."""

    console: Line

    def reopen(self) -> None: ...


#: How long an escape is left alone before the next key, so the two are not
#: read as one sequence.
ESCAPE_SETTLES: Final[float] = 2.0


def _editor_screen(console: Line, timeout: float) -> bytes:
    """Press `e` until GRUB draws the entry, and answer with that screen.

    One press is not enough. The menu redraws itself when its countdown is
    stopped, and a snapshot taken into that redraw holds the menu rather than
    the editor: a run read `no GRUB entry to edit on this screen` off a
    countdown line eight seconds from booting.
    """
    # No wait for the menu here: `hold_the_menu` did that and stopped the
    # countdown, and asking again blocked the whole patience on a header that
    # had already gone past, so every press landed after it. Nineteen guests of
    # one round ended at `GRUB never opened its editor` that way.
    deadline = time.monotonic() + timeout
    seen = b""
    everything = b""
    console.send_raw("e")
    while time.monotonic() < deadline:
        seen = console.snapshot(3.0)
        everything += seen
        if b"setparams" in seen:
            return seen
        # ESC first, and only then `e` again: in the editor it discards the
        # edits and returns to the menu, and in the menu it does nothing. A
        # bare second `e` would type the letter into the command line. The gap
        # keeps them two keys rather than one escape sequence.
        console.send_raw("\x1b")
        time.sleep(ESCAPE_SETTLES)
        console.send_raw("e")
    # The whole read, not the last snapshot: the last one is empty on a guest
    # that booted while the loop was still pressing, which says nothing.
    raise ProxmoxError(f"GRUB never opened its editor: {everything[-400:]!r}")


def _line_of_linux(screen: bytes) -> int:
    """How many Ctrl-N from where the cursor opens to the `linux` line.

    The editor opens with the cursor on `setparams`, so the answer is the
    difference between the two rows GRUB drew them on. A screen holding
    neither is a medium whose bootloader is not GRUB; guessing a number there
    would edit whatever happened to sit on that row.
    """
    rows: dict[int, bytes] = {}
    for row, text in _PLACED.findall(screen):
        said = text.strip()
        if said:
            rows[int(row)] = said
    top = [at for at, said in rows.items() if said.startswith(b"setparams")]
    body = [
        at
        for at, said in rows.items()
        if said.split(b" ", 1)[0] in (b"linux", b"linuxefi", b"linux16")
    ]
    if not top or not body:
        raise ProxmoxError(f"no GRUB entry to edit on this screen: {screen[-400:]!r}")
    return min(body) - min(top)
