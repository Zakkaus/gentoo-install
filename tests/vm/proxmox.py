# SPDX-License-Identifier: GPL-2.0-or-later
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
import sys
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

#: A reset is idempotent, so a request that may or may not have arrived is
#: safe to send again: resetting a guest that was already reset does nothing.
#: run63 lost `vm-unlock` at 1.2 minutes to one `<urlopen error timed out>`.
RESET_TRIES: Final[int] = 3
RESET_PAUSE: Final[float] = 5.0
TASK_POLL: Final[float] = 2.0

#: What Proxmox writes as a task's exit status when it finished its work
#: and logged a warning while doing it.
TASK_WARNED: Final[str] = "WARNINGS:"

#: What every driver CD this harness uploads is named after.
DRIVER_PREFIX: Final[str] = "gi-driver-"

#: What Proxmox answers when a delete reaches a guest that never stopped.
STILL_RUNNING: Final[str] = "is running - destroy failed"
CLEANUP_PAUSE: Final[float] = 2.0
CLEANUP_PATIENCE: Final[float] = 300.0
#: How long a stop is retried. Shorter than cleanup: the guest is wanted down
#: so the disk it wrote can be booted, and a run that cannot stop it has
#: nothing left to measure.
STOP_PATIENCE: Final[float] = 120.0


class ProxmoxError(Exception):
    """The cluster refused a call, or a task it accepted did not finish."""


class GrubNotReadable(ProxmoxError):
    """GRUB produced no serial output that identifies a readable menu."""


class ProxmoxNotFound(ProxmoxError):
    """The requested cluster object no longer exists."""


class ForeignGuest(ProxmoxError):
    """That VMID holds a guest this run did not build, so it is not ours to remove."""


class ProxmoxTransientError(ProxmoxError):
    """The call failed in a state the caller may retry."""


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
    return isinstance(error, ProxmoxTransientError)


def _http_exception(method: str, path: str, error: urllib.error.HTTPError) -> ProxmoxError:
    reason = str(error.reason)
    said = error.read().decode("utf-8", "replace").strip()
    message = f"{method} {path} answered {error.code} {reason}: {said}"
    config = re.fullmatch(r"/nodes/[^/]+/qemu/(\d+)/config(?:\?.*)?", path)
    if error.code == 404 or (
        error.code == 500
        and config is not None
        and f"qemu-server/{config.group(1)}.conf' does not exist" in f"{reason}\n{said}"
    ):
        return ProxmoxNotFound(message)
    retryable_500 = error.code == 500 and (
        re.fullmatch(r"/nodes/[^/]+/qemu/\d+/termproxy(?:\?.*)?", path) is not None
        or re.fullmatch(r"/nodes/[^/]+/tasks/[^/]+/status(?:\?.*)?", path) is not None
        # `destroy` stops the guest and deletes it, and the stop can fail on
        # its own: 9302 was left running on a node refusing connections, and
        # this answer ended the only loop that would have stopped it again.
        or (STILL_RUNNING in said and method == "DELETE")
    )
    # 595 is the cluster proxy saying it could not reach the node, not the node
    # saying no: `infra-node3` answered it four times while restarting, and the
    # fourth ended a twenty-two guest campaign at its first dispatch.
    if error.code in (429, 502, 503, 504, 595) or retryable_500:
        return ProxmoxTransientError(message)
    return ProxmoxError(message)


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
        secret = TOKEN_FILE.read_text().strip()
    except OSError as error:
        raise ProxmoxError(f"the API token is not readable at {TOKEN_FILE}: {error}") from error
    if re.fullmatch(r"[\x21-\x7e]+", secret) is None:
        raise ProxmoxError(f"the API token has an invalid format at {TOKEN_FILE}")
    return secret


@dataclass(frozen=True)
class Node:
    name: str
    free_bytes: int
    cores: int
    #: Cores this node is not already using, measured rather than derived: the
    #: cluster runs other people's machines and their load is not in `cores`.
    free_cores: float = 0.0


class Api:
    """One authenticated conversation with the cluster."""

    def __init__(self, host: str = HOST) -> None:
        self.host = host
        self._context = _certificates()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._context), _RejectRedirect()
        )

    def _remember(self, headers: Any) -> str:
        for name, value in headers.items():
            if name.lower() == "set-cookie" and value.startswith("INGRESSCOOKIE="):
                return str(value).split(";", 1)[0]
        return ""

    def call(self, method: str, path: str, **form: Any) -> Any:
        data, _ = self.call_with_affinity(method, path, **form)
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
                return data, remembered
        except urllib.error.HTTPError as error:
            # The reason, not only the body: Proxmox answers `500` with
            # `{"data":null}` and puts what went wrong in the status line.
            raise _http_exception(method, path, error) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProxmoxTransientError(
                f"{method} {path} did not answer: {error}"
            ) from error

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
                exit_status = str(status.get("exitstatus", ""))
                if exit_status.startswith(TASK_WARNED):
                    # Proxmox says `WARNINGS: n` for a task that did its work
                    # and logged something: a `qmdestroy` that could not reach
                    # one disk still removed the guest, and reading it as a
                    # failure printed `the guest was not removed` for three
                    # machines that were gone.
                    print(f"{upid} {exit_status}", file=sys.stderr)
                    return
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
                free_cores=int(one.get("maxcpu", 0)) * (1.0 - float(one.get("cpu", 0.0))),
            )
            for one in self.call("GET", "/nodes")
            if one.get("status") == "online"
        ]
        return sorted(found, key=lambda one: one.free_bytes, reverse=True)

    def node_load(self, name: str) -> float | None:
        """The share of this node's cores in use, or None when it will not say.

        Read when a guest is about to be called stuck, not on every sample: a
        guest at `cpu 0.00` on a node at 99% is the cluster having no time for
        it, and a guest at `cpu 0.00` on an idle node is a guest that stopped.
        """
        try:
            status = self.call("GET", f"/nodes/{name}/status")
        except ProxmoxError:
            return None
        used = status.get("cpu")
        return float(used) if isinstance(used, (int, float)) else None

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

    def stale_drivers(self, node: str, keep: str, older_than: float) -> list[str]:
        """Driver CDs on this node that no run can still be using.

        A schedule removes its own, but one killed outright leaves it, and 149
        of them were counted across six nodes against a 33 GiB store. Age is
        what makes the answer safe: a campaign runs for hours, so a file older
        than a day belongs to nobody, while a name a second campaign uploaded
        this minute is left alone.
        """
        content = self.call("GET", f"/nodes/{node}/storage/{ISO_STORAGE}/content?content=iso")
        now = time.time()
        found = []
        for one in content:
            name = str(one["volid"]).split("/")[-1]
            if name == keep or not name.startswith(DRIVER_PREFIX):
                continue
            if now - float(one.get("ctime", now)) >= older_than:
                found.append(name)
        return sorted(found)

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
        # Retried, because resetting a guest that was already reset does
        # nothing and a request that timed out may well have arrived: run63
        # lost `vm-unlock` at 1.2 minutes to one
        # `POST .../status/reset did not answer: <urlopen error timed out>`.
        last: ProxmoxTransientError | None = None
        for attempt in range(RESET_TRIES):
            try:
                self.api.wait(
                    self.node,
                    self.api.call(
                        "POST", f"/nodes/{self.node}/qemu/{self.vmid}/status/reset"
                    ),
                )
                return
            except ProxmoxTransientError as error:
                last = error
                if attempt + 1 < RESET_TRIES:
                    time.sleep(RESET_PAUSE * (attempt + 1))
        assert last is not None
        raise last

    def qmp_state(self) -> str:
        """What qemu says this guest is doing, or empty when it will not say.

        `running` is the ordinary answer. A guest whose storage returned an
        error is `io-error` and one somebody paused is `paused`: both read as
        `cpu 0.00` with flat counters, which is what a guest that stopped on
        its own reads as too.
        """
        try:
            status = self.api.call(
                "GET", f"/nodes/{self.node}/qemu/{self.vmid}/status/current"
            )
        except ProxmoxError:
            return ""
        said = status.get("qmpstatus")
        return said if isinstance(said, str) else ""

    def transferred(self) -> tuple[int, float] | None:
        """Bytes this guest has received and written, and its CPU share.

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
        moved = int(status.get("netin", 0)) + int(status.get("diskwrite", 0))
        # The share of a core the guest is using, from the same reading. A
        # compile whose build directory is in RAM moves no bytes at all:
        # `vm-binhost-fallback` was ended for 7781 bytes in twenty minutes
        # while it was building grub.
        return moved, float(status.get("cpu", 0.0))

    def running(self) -> bool:
        status = self.api.call("GET", f"/nodes/{self.node}/qemu/{self.vmid}/status/current")
        return str(status.get("status")) == "running"

    def console(self) -> ConsoleChannel:
        return ConsoleChannel.open(self.api, self.node, self.vmid)

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
                    if not _transient(error):
                        raise
                    last = str(error)
                    time.sleep(0.5 * (attempt + 1))
            else:
                raise ProxmoxError(f"{key!r} was not delivered to vm {self.vmid}: {last}")
            time.sleep(self.KEY_PAUSE)

    def stop(self) -> None:
        """Stop the machine this run created, and only that one.

        `destroy` has checked the tag since a VMID was found not to be proof of
        ownership; this did not, and three call sites reach it without going
        through `destroy`. VMIDs are recycled between fixtures inside one
        campaign — 9302 carried three guests in seventy minutes — so a stale
        `Guest` had nothing between it and another run's machine.
        """
        try:
            status = self.api.call(
                "GET", f"/nodes/{self.node}/qemu/{self.vmid}/status/current"
            )
        except ProxmoxNotFound:
            self._booted = False
            return
        if str(status.get("status")) != "running":
            self._booted = False
            return
        if not self._is_ours():
            raise ProxmoxError(
                f"vm {self.vmid} on {self.node} is not this run's machine; not stopping it"
            )
        # Retried, because the cluster proxy answering `502 Bad Gateway` is not
        # the node refusing: one such answer ended `vm-binpkg` after a 48-minute
        # install that had already finished, on the stop that precedes booting
        # the disk it wrote. The status is read again each round, so a stop
        # whose answer was eaten is seen as the guest already being down.
        deadline = time.monotonic() + STOP_PATIENCE
        last = "the guest is still running"
        while True:
            try:
                self.api.wait(
                    self.node,
                    self.api.call("POST", f"/nodes/{self.node}/qemu/{self.vmid}/status/stop"),
                    patience=180.0,
                )
                break
            except ProxmoxNotFound:
                break
            except ProxmoxError as error:
                last = str(error)
                if not _transient(error) or time.monotonic() >= deadline:
                    raise ProxmoxError(
                        f"vm {self.vmid} on {self.node} was not stopped: {last}"
                    ) from error
                time.sleep(min(CLEANUP_PAUSE, max(0.0, deadline - time.monotonic())))
                try:
                    again = self.api.call(
                        "GET", f"/nodes/{self.node}/qemu/{self.vmid}/status/current"
                    )
                except ProxmoxNotFound:
                    break
                except ProxmoxError:
                    continue
                if str(again.get("status")) != "running":
                    break
        self._booted = False

    def _is_ours(self) -> bool:
        """Whether the machine at this VMID still carries this harness's tag.

        A VMID is not proof: the range already held a production template, and
        within one campaign a failed run's VMID is handed straight to the next.
        """
        if not VMID_FIRST <= self.vmid <= VMID_LAST:
            return False
        try:
            config = self.api.call("GET", f"/nodes/{self.node}/qemu/{self.vmid}/config")
        except ProxmoxNotFound:
            return False
        return TAG in str(config.get("tags", "")).split(";")

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
                if not _transient(error):
                    raise
                time.sleep(min(CLEANUP_PAUSE, max(0.0, deadline - time.monotonic())))
                continue
            tags = str(config.get("tags", "")).split(";")
            if TAG not in tags:
                raise ForeignGuest(
                    f"vm {self.vmid} on {self.node} is not tagged {TAG!r}; refusing to remove it"
                )
            if not self.spec.nonce or self.spec.nonce not in tags:
                raise ForeignGuest(
                    f"vm {self.vmid} on {self.node} is not the guest this run built; "
                    "refusing to remove it"
                )
            try:
                self.stop()
            except ProxmoxError as error:
                # A running guest is the one cleanup must still try to delete.
                last = str(error)
            try:
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
                # Every failure of the removal itself, not only the ones the
                # API marks retryable: `qmdestroy` on 9301 ended with `rbd
                # error: rbd: listing images failed`, the guest stayed on the
                # cluster for hours, and the same DELETE with the same
                # parameters removed it. The loop re-reads the config each
                # round and returns the moment the guest is gone, so a retry
                # cannot remove anything twice.
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

        Each retry starts a new ticket exchange. The failure is intermittent
        and follows which backend answered: one backend refused five calls in
        a row while the next succeeded, so a failed exchange is not reused.
        """
        last = ""
        for attempt in range(tries):
            try:
                ticket, affinity = api.call_with_affinity(
                    "POST", f"/nodes/{node}/qemu/{vmid}/termproxy"
                )
            except ProxmoxError as error:
                if not _transient(error):
                    raise
                last = str(error)
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
        try:
            got = self._socket.read()
        except WebSocketError:
            # A frame this reader cannot parse ends the connection the same way
            # a reset does. `read` already closed the socket, so the caller
            # sees a closed console and reopens it; raised, it went past every
            # `except ConsoleClosed` and ended the run.
            return b""
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

    @property
    def why_closed(self) -> str:
        return str(getattr(self._socket, "why_closed", ""))

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
GRUB_COUNTDOWN: Final[str] = (
    r"starting serial terminal on interface serial0|"
    r"highlighted entry|GNU GRUB|Minimal BASH-like"
)


#: How long GRUB is given to draw its editor. Each attempt costs an escape,
#: a settle and a snapshot, so thirty seconds bought six presses; `vm-mdraid`
#: lost a run in forty-eight seconds while its node sat at 99.8% CPU and every
#: redraw crawled. The menu itself is already held, so waiting longer races
#: nothing: the countdown is stopped and the guest is not about to boot.
EDITOR_PATIENCE: Final[float] = 120.0


#: How long each press is given to show whether the countdown stopped.
HOLD_CONFIRM: Final[float] = 3.0

#: GRUB is at its menu. The wake-up pattern also matches the hypervisor's own
#: banner, which is not evidence of anything on the guest.
_MENU_DRAWN: Final[re.Pattern[bytes]] = re.compile(
    rb"highlighted entry|GNU GRUB|Minimal BASH-like"
)
_COUNTING: Final[re.Pattern[bytes]] = re.compile(rb"executed automatically in \d+s")
_BOOTING: Final[re.Pattern[bytes]] = re.compile(rb"Booting [`\']")


def hold_the_menu(console: Line, timeout: float = 300.0) -> bytes:
    """Wait for GRUB's menu and stop its countdown, and prove it stopped.

    `starting serial terminal on interface serial0` is the hypervisor's own
    banner, printed before the firmware loads anything, so one press on that
    match lands in the void: `vm-openrc-desktop` counted `2s 1s 0s`, booted
    `Boot LiveCD` unedited with no serial console, and the editor was then
    asked for over the next two and a half minutes on a guest already gone.
    """
    deadline = time.monotonic() + timeout
    try:
        seen = console.expect(GRUB_COUNTDOWN, timeout=timeout)
    except (ConsoleTimeout, ConsoleClosed) as error:
        raise GrubNotReadable(str(error)) from error
    console.send_raw(GRUB_HOLD)
    while time.monotonic() < deadline:
        after = console.snapshot(HOLD_CONFIRM)
        seen += after
        if _BOOTING.search(after):
            raise GrubNotReadable(
                f"the entry booted before its countdown was held: {seen[-400:]!r}"
            )
        if _MENU_DRAWN.search(seen):
            if not _COUNTING.search(after):
                return seen
        elif not after.strip():
            # Nothing drawn and nothing more arriving: a BIOS medium says
            # nothing to a serial console before the kernel, so the banner is
            # the only signal there and one press is all there is to make.
            return seen
        # Either GRUB has not drawn yet or it is still counting: the press
        # before this one reached something that was not GRUB.
        console.send_raw(GRUB_HOLD)
    raise GrubNotReadable(f"GRUB kept counting down: {seen[-400:]!r}")


def append_to_cmdline(console: Line, extra: str, timeout: float = EDITOR_PATIENCE) -> None:
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
    try:
        screen = _editor_screen(console, timeout)
        down = _line_of_linux(screen)
    except ProxmoxError as error:
        raise GrubNotReadable(str(error)) from error
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


#: What the medium's menu waits, and what `BIOS_MENU_DELAY` has to stay under.
#: Read from the ISO's own `grub.cfg`, which sets `timeout=10`.
BIOS_MENU_TIMEOUT: Final[float] = 10.0

#: When to press `e`, counted from the guest being started, paired with the
#: line to move to. Nothing on the serial port says when the menu appeared —
#: not SeaBIOS, not `Welcome to GRUB!`, nothing until the kernel speaks, and
#: every BIOS log in `lab/` holds the guest's boot and no marker before it — so
#: the moment is sampled rather than waited for.
#:
#: A sample at `d` lands when the menu appeared at some `t` with `d - 10 < t <=
#: d`, because GRUB counts `BIOS_MENU_TIMEOUT` down from `t` and a key before
#: `t` goes nowhere. Each attempt is its own boot, so the samples cover the
#: union of those windows: spacing them under `BIOS_MENU_TIMEOUT` leaves no gap
#: between the earliest and the latest.
#:
#: The samples used to stop at nine seconds, which covered a menu up at nine
#: and nothing later, and no run has ever printed the line this function prints
#: when an edit lands. Nodes measured between 80% and 100% busy all night, and
#: `vm-mdraid`'s readable UEFI menu needed more than thirty seconds there, so
#: a menu appearing after ten is the case to cover rather than the one to rule
#: out.
#:
#: Not later than that, though. `diskread` dates GRUB's countdown on a *cold*
#: start — the kernel load steps by tens of megabytes at 33.4s on an idle node
#: and 59.4s at 98% cpu — and moving the samples out to meet it made things
#: worse, because every attempt here follows a `reset` of a guest that has
#: already booted once, where the medium is in the host's cache and
#: `Welcome to GRUB!` arrives 1.9s later. The two clocks are not the same one.
BIOS_ATTEMPTS: Final[tuple[tuple[float, int], ...]] = (
    (6.0, 2),
    (9.0, 2),
    (13.0, 2),
    (17.0, 2),
    (3.0, 2),
    (6.0, 1),
    (13.0, 3),
)

#: What the kernel prints once `console=ttyS0` is on its command line, and the
#: proof that the edit landed on the right line.
KERNEL_SPEAKS: Final[str] = r"Linux version|Command line:|\[    0\.000000\]"


#: What the live medium's own auto-login shell is asked to start, so the
#: serial port carries a root shell without the kernel command line being
#: touched. `--autologin root` because the medium scrambles the root password,
#: and `&` because this is typed into a shell that has to stay usable.
#: Short because every character is one API request with a pause after it:
#: 61 characters measured 68s per attempt on `infra-node1`, which is a fifth of
#: the deadline spent typing. `-a` is `--autologin`, `-L` is the medium's own
#: commented `s0` line, and `agetty` defaults TERM to `vt100` on a serial line.
SERIAL_GETTY: Final[str] = "setsid agetty -a root -L 115200 ttyS0&"

#: What that shell prints once it is on the serial port. `root@` and not `#`:
#: the medium's prompt carries the user and the host, and `#` alone matches
#: half the boot messages before it.
SERIAL_SHELL_SPEAKS: Final[str] = r"root@[^\s]+"

#: How often the line is typed again, and for how long altogether. Not a
#: ladder of fixed delays: every timing guess this replaces failed on a loaded
#: cluster and `vm-bios` passed only in a round where it was the one guest.
#: An idle node reaches the auto-login in about fifteen seconds, measured by
#: screenshotting a SeaBIOS guest through the QEMU monitor; a node running
#: twelve of them takes as long as it takes, and no number written here is
#: right for both. So the line goes in again every interval until the port
#: answers or the deadline passes, and the deadline is the only guess left.
AUTOLOGIN_INTERVAL: Final[float] = 20.0

#: What one attempt costs before the port is looked at at all. `send_keys` is
#: one API request per character with `KEY_PAUSE` after each, and the
#: 38-character line measured 48.7s, 57.2s and 51.2s on `infra-node5`; the
#: 61-character line it replaced measured 66s to 70s on `infra-node1`.
AUTOLOGIN_TYPING: Final[float] = 60.0

#: Twelve attempts at what one of them actually costs. A guest built for the
#: question answered on its third try on an idle node; the 360s this replaces
#: bought five on a loaded one, and every BIOS fixture in run71 spent all five
#: without reaching a shell. The old arithmetic divided the deadline by the
#: interval alone, which counts the watching and not the typing.
AUTOLOGIN_DEADLINE: Final[float] = 12 * (AUTOLOGIN_TYPING + AUTOLOGIN_INTERVAL)


def open_a_serial_shell_blind(
    guest: Guest, link: "Reopenable", patience: float = AUTOLOGIN_DEADLINE
) -> None:
    """Type into the medium's auto-login shell until the serial port answers.

    The blind GRUB edit this replaces never landed. A screenshot of the guest
    explains why: the menu is gone in about three seconds and the live system
    is already logged in on the VGA console, so the keys arrived at a shell
    rather than at GRUB — `bash: cserial: command not found` is what the
    screen held. Nothing about the kernel command line has to change: the
    medium logs root in by itself, and one line moves a getty onto the port.

    Readable from the first attempt, unlike what it replaces: the shell
    answers on the serial port or it does not, and the console says which.
    """
    console = link.console
    last = ""
    typed = 0
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        typed += 1
        # A bare newline first: the auto-login prints its welcome and leaves
        # the cursor after a prompt, and a line typed into a console that has
        # not finished drawing loses its first characters.
        guest.send_keys(["ret"])
        guest.send_keys(keys_for(SERIAL_GETTY))
        guest.send_keys(["ret"])
        # The keys go through the API, so nothing crosses the console while
        # they are typed and the node closes an idle session. Measured on a
        # guest of its own: 68s of typing, then `ConsoleClosed` at 0.0s of
        # watching, four attempts out of four, with the guest silent
        # throughout. The console that is watched is opened after the typing.
        #
        # Asking for a prompt, not merely opening: `agetty --autologin` prints
        # one prompt when the shell starts and never prints again, so a
        # session opened after that shows nothing for ever. Fourteen attempts
        # over sixteen minutes on `ext4-bios` matched nothing at all with the
        # session opened silently.
        link.reopen(solicit_prompt=True)
        console = link.console
        try:
            console.expect(SERIAL_SHELL_SPEAKS, timeout=AUTOLOGIN_INTERVAL)
            print(f"the serial shell answered on attempt {typed}", flush=True)
            return
        except ConsoleTimeout as error:
            # Typed into a medium that has not reached its shell yet, which is
            # what a loaded node looks like. The line is harmless there and
            # goes in again rather than the guest being reset.
            last = str(error)[:200]
        except ConsoleClosed as error:
            last = str(error)[:200]
            link.reopen(solicit_prompt=False)
            console = link.console
    raise ProxmoxError(
        f"no serial shell in {patience:.0f}s and {typed} attempts at the "
        f"medium's auto-login: {last}"
    )


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
    for attempt, (delay, down) in enumerate(BIOS_ATTEMPTS):
        # Timed, not read: this guest's GRUB draws on VGA and says nothing on
        # the serial port, so there is no menu to wait for.
        time.sleep(delay)
        guest.send_keys(["e"])
        time.sleep(2.0)
        guest.send_keys(["ctrl-n"] * down + ["ctrl-e"])
        time.sleep(1.0)
        guest.send_keys(keys_for(f" {extra}"))
        time.sleep(1.0)
        guest.send_keys(["ctrl-x"])
        try:
            console.expect(KERNEL_SPEAKS, timeout=patience)
            print(f"the blind GRUB edit landed at {delay:.0f}s, {down} down", flush=True)
            return
        except (ConsoleTimeout, ConsoleClosed) as error:
            last = str(error)[:200]
        if attempt + 1 < len(BIOS_ATTEMPTS):
            # A reset drops the console with it, so the next attempt reads a
            # new one: the first guest to take a second attempt reported `the
            # guest closed the serial connection` a minute in.
            guest.reset()
            link.reopen(solicit_prompt=False)
            console = link.console
    raise ProxmoxError(
        f"the kernel never spoke after {len(BIOS_ATTEMPTS)} blind GRUB edits: {last}"
    )


class Line(Protocol):
    """What driving a guest needs of a console: keys in, text out.

    A protocol rather than `SerialConsole`, because a cluster run drives a
    console that reopens itself when the cluster drops it, and because a test
    can then script one.
    """

    def send(self, line: str) -> None: ...

    def send_raw(self, keys: str) -> None: ...

    def snapshot(self, seconds: float) -> bytes: ...

    def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes: ...

    def close(self) -> None: ...

    @property
    def closed(self) -> bool: ...


class Reopenable(Protocol):
    """A console that can be replaced with another one to the same guest. A
    reset drops the console with it, and the next attempt needs a live one."""

    console: Line

    def reopen(self, *, solicit_prompt: bool = True) -> None: ...


#: How long an escape is left alone before the next key, so the two are not
#: read as one sequence.
ESCAPE_SETTLES: Final[float] = 2.0

#: Below this, the console said nothing rather than saying the wrong thing. A
#: menu redraw is hundreds of bytes of escape sequences, so anything under a
#: line is silence.
SILENT_CONSOLE_BYTES: Final[int] = 16


#: What GRUB calls the line that names the kernel, whichever medium drew it.
_KERNEL_COMMANDS: Final[tuple[bytes, ...]] = (b"linux", b"linuxefi", b"linux16")


def _drawn_rows(screen: bytes) -> dict[int, bytes]:
    """Every row GRUB placed text on, the last write to each winning."""
    rows: dict[int, bytes] = {}
    for row, text in _PLACED.findall(screen):
        said = text.strip()
        if said:
            rows[int(row)] = said
    return rows


def _kernel_rows(rows: dict[int, bytes]) -> list[int]:
    return [at for at, said in rows.items() if said.split(b" ", 1)[0] in _KERNEL_COMMANDS]


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
    everything = b""
    console.send_raw("e")
    while time.monotonic() < deadline:
        everything += console.snapshot(3.0)
        # Both lines, because both are what the caller needs: `btrfs-luks` was
        # ended at 0.3 minutes on a screen holding `setparams` and the entry's
        # `search` line, with the `linux` line still to come.
        if b"setparams" in everything and _kernel_rows(_drawn_rows(everything)):
            return everything
        if b"setparams" in everything:
            # The editor is open and the rest of the entry is still arriving.
            # ESC here would discard it and go back to the menu.
            continue
        # ESC first, and only then `e` again: in the editor it discards the
        # edits and returns to the menu, and in the menu it does nothing. A
        # bare second `e` would type the letter into the command line. The gap
        # keeps them two keys rather than one escape sequence.
        console.send_raw("\x1b")
        time.sleep(ESCAPE_SETTLES)
        console.send_raw("e")
    # The whole read, not the last snapshot: the last one is empty on a guest
    # that booted while the loop was still pressing, which says nothing.
    # A silent console and a stubborn GRUB need different answers, so they get
    # different sentences: `openrc-sdboot` reported two bytes as GRUB's fault.
    if len(everything.strip()) < SILENT_CONSOLE_BYTES:
        raise ProxmoxError(
            f"the console delivered {len(everything)} bytes while the editor was "
            f"asked for over {timeout:.0f}s: {everything[-400:]!r}"
        )
    raise ProxmoxError(f"GRUB never opened its editor: {everything[-400:]!r}")


def _line_of_linux(screen: bytes) -> int:
    """How many Ctrl-N from where the cursor opens to the `linux` line.

    The editor opens with the cursor on `setparams`, so the answer is the
    difference between the two rows GRUB drew them on. A screen holding
    neither is a medium whose bootloader is not GRUB; guessing a number there
    would edit whatever happened to sit on that row.
    """
    rows = _drawn_rows(screen)
    top = [at for at, said in rows.items() if said.startswith(b"setparams")]
    body = _kernel_rows(rows)
    if not top or not body:
        raise ProxmoxError(f"no GRUB entry to edit on this screen: {screen[-400:]!r}")
    return min(body) - min(top)
