"""Run the install fixtures on the Proxmox cluster, unattended.

One process, many guests. Each guest is a thread that builds a machine, drives
its console through an install, reads the result back and deletes the machine,
and the scheduler starts the next one the moment a slot frees. Nothing waits
for the whole set: a binpkg fixture finishes in six minutes and a source kernel
takes an hour, so a barrier would hold every finished result hostage to the
slowest, and results that were already earned would be lost if it hung.

Two clocks bound every guest. A watchdog looks at the serial log every ten
minutes and ends a guest whose byte count has not moved across three looks;
a whole run has a hard ceiling on top of that. Activity, not elapsed time, is
what separates a slow mirror from a dead guest.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import queue
import signal
import re
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Final, Protocol, TypeVar, cast

from gentoo_install.model.config import Firmware as BootFirmware
from gentoo_install.model.config import InitSystem, InstallConfig
from gentoo_install.model.device import (
    Existing,
    Filesystem,
    Luks,
    Mountpoint,
    Subvolume,
    ZfsDataset,
    ZfsPool,
)
from gentoo_install.model.config import MirrorRegion, Sync
from gentoo_install.exec.config import load
from gentoo_install.model.serialise import to_toml
from .console import (
    DISK_PASSPHRASE,
    PASSPHRASE_PROMPT,
    PASSWORD_PROMPT,
    ConsoleClosed,
    ConsoleIdle,
    ConsoleTimeout,
    SerialConsole,
)
from .driver import build as build_driver, digest as driver_digest, remote_name
from .monitor import keys_for
from .proxmox import (
    Api,
    CreateConflict,
    Guest,
    GuestSpec,
    Node,
    ProxmoxError,
    VMID_FIRST,
    VMID_LAST,
    Line,
    append_to_cmdline,
    append_to_cmdline_blind,
)
from .results import CONSOLE_CLOSE, ResultError, console_command, read_console
from .workdir import WorkdirError, confined

REPOSITORY: Final[Path] = Path(__file__).resolve().parents[2]
WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/cluster"

#: Outside any one round's work directory: the ISO stays on the node between
#: rounds, so the record of what was uploaded has to outlive the round that
#: uploaded it. A fresh work directory otherwise met its own upload as
#: `already exists on infra-node5 without its signed SHA-512 record`.
MEDIUM_TRUST: Final[Path] = WORKROOT / "medium-trust"

#: Where the guest gathers what the run produced before it is read back.
RESULT_DIR: Final[str] = "/tmp/gentoo-install-results"

#: Where the media come from, in the order they are tried. The cluster is in
#: China, so a node reaches these far faster than an upload from the
#: workstation would, and the bytes never cross the workstation's link.
#:
#: `mirrors.ustc.edu.cn` is not among them, and it is the fastest of the set
#: from these nodes. It answers `403 Forbidden` to Proxmox's downloader, which
#: is wget, while serving the same URL to anything else. Measured again on
#: 2026-08-10: `Wget/1.21.4` gets 403 from USTC and 206 from tuna, nju, zju
#: and hust. The installer reads USTC happily; only this path has to avoid it.
MIRRORS: Final[tuple[str, ...]] = (
    "https://mirrors.tuna.tsinghua.edu.cn/gentoo",
    "https://mirror.nju.edu.cn/gentoo",
    "https://mirrors.zju.edu.cn/gentoo",
    "https://mirrors.hust.edu.cn/gentoo",
    "https://mirrors.bfsu.edu.cn/gentoo",
    # Last, and only if every Chinese mirror refused: the nodes are in China
    # and a gibibyte from here is slow enough to be worth avoiding.
    "https://distfiles.gentoo.org",
)

AUTOBUILDS: Final[str] = "releases/amd64/autobuilds"

#: The pointer file beside the medium, which is Gentoo's own signed output and
#: identical on every mirror. Read rather than hardcoded: `local` storage is
#: per node, so a name that only one node happens to hold is not a default.
MINIMAL_POINTER: Final[str] = "latest-install-amd64-minimal.txt"

#: Filled in from the pointer file when a run starts. The placeholder is what a
#: `Job` carries until then, so a fixture can still name a medium of its own.
DEFAULT_ISO: Final[str] = "minimal"

RELENG_FINGERPRINT: Final[str] = "13EBBDBEDE7A12775DFDB1BABB572E0E2D182910"
RELEASE_KEY: Final[Path] = Path("/usr/share/openpgp-keys/gentoo-release.asc")
RELEASE_KEYRING: Final[str] = "https://qa-reports.gentoo.org/output/service-keys.gpg"

#: Kernel parameters the medium's own GRUB entry lacks. Without the console the
#: kernel says nothing on the serial port and every run reads as hung.
EXTRA_CMDLINE: Final[str] = "console=ttyS0,115200"

#: What a guest is given. Four gibibytes rather than six, so the three nodes
#: with about 6 GiB spare can each take one instead of none: the installer
#: warns below 5 GiB and builds in `/var/tmp` rather than a tmpfs, which is
#: slower and correct. Two cores of a node's four leaves it able to answer the
#: API while a build runs.
GUEST_MEMORY_MIB: Final[int] = 4096
GUEST_CORES: Final[int] = 2
TARGET_GIB: Final[int] = 40

#: What a guest that compiles is given instead. A source kernel or a desktop
#: is an hour of `emerge` where a binary-package fixture is six minutes, and
#: giving both the same two cores left the cluster idle while the queue was
#: deep. A node has four cores, so a heavy guest takes all of them and the
#: node carries one; the memory is what `MAKEOPTS -j4` needs to link.
HEAVY_MEMORY_MIB: Final[int] = 8192
HEAVY_CORES: Final[int] = 4

#: Left free on every node whatever else is scheduled. A node with nothing
#: spare stops answering, and this cluster runs other people's machines.
NODE_HEADROOM_BYTES: Final[int] = 2 * 1024**3

#: How often the watchdog looks, and how many quiet looks end a guest. Ten
#: minutes because a stage3 extract and a kernel build both write progress more
#: often than that, and half an hour of silence is not a slow mirror.
WATCH_EVERY: Final[float] = 600.0
WATCH_STRIKES: Final[int] = 3

#: Between starting one guest and the next. They all reach for the same mirror
#: in their first minute, and twelve starting together each failed the
#: reachability check against a host that was answering.
#:
#: Twenty rather than eight, because the DHCP server on this network runs on a
#: Raspberry Pi acting as the router, and thirteen guests asking for a lease
#: inside one minute is what made it answer some and not others: the same node
#: offered 10.31.0.201 on one attempt and printed `soliciting a DHCP lease`
#: then `timed out` on the next, minutes apart.
STAGGER: Final[float] = 20.0

#: How long the schedule waits before looking at capacity again while jobs are
#: still queued. A node's free memory lags what is actually running on it, so
#: the first look after a guest is deleted can still read it as full: with only
#: the watchdog's interval to fall back on, five free slots sat unused for ten
#: minutes with eighteen jobs waiting.
POLL_WHILE_QUEUED: Final[float] = 20.0

#: The outer bound, reached only by a guest that keeps printing and never
#: finishes. Twelve of one round hit the three-hour version of it with a
#: repository listing still scrolling, so the ceiling is generous and
#: `INSTALL_IDLE` is what actually ends a dead one.
RUN_CEILING: Final[float] = 8 * 3600.0

#: How long an install may print nothing. Unpacking a stage3 and fetching one
#: are the quiet steps, and neither takes twenty minutes; the watchdog reads
#: the hypervisor's byte counters for the same question from outside.
INSTALL_IDLE: Final[float] = 20 * 60.0

#: How long the installed system has to reach a login prompt. A first boot
#: builds the initramfs cache and, on openrc, runs every service in order.
BOOT_PATIENCE: Final[float] = 600.0

#: A cluster with no room must produce a result rather than poll for ever.
CAPACITY_PATIENCE: Final[float] = 120.0

#: A conflict refreshes cluster allocation before another create. The bound
#: prevents a busy VMID range from holding the scheduler indefinitely.
RESERVATION_TRIES: Final[int] = 4


class Verdict(Enum):
    """How one guest ended. `STUCK` is separate from `FAIL` on purpose: a
    failure is read in the log, and a guest that stopped writing is read on
    the screen, so the two are chased differently."""

    OK = "ok"
    FAIL = "FAIL"
    STUCK = "STUCK"
    ERROR = "ERROR"


class Phase(Enum):
    """The operation that produced an outcome."""

    SCHEDULE = "schedule"
    CREATE = "create"
    BOOT_LIVE = "boot-live"
    INSTALL = "install"
    BOOT_INSTALLED = "boot-installed"


class JobStatus(Enum):
    """The mutually exclusive states of one scheduled fixture."""

    WAITING = "waiting"
    RUNNING = "running"
    ANSWERED = "answered"
    COLLECTED = "collected"


@dataclass
class Outcome:
    name: str
    verdict: Verdict
    seconds: float
    detail: str = ""
    log: Path | None = None
    #: Whether the guest is gone from the cluster. False keeps the node's
    #: slot held: the memory is still allocated, and handing it back put the
    #: next guest onto a node that had no room for it.
    removed: bool = True
    phase: Phase = Phase.SCHEDULE
    revision: str = ""
    #: Which VMID the job held. Zero for an outcome raised before one was
    #: taken, which is the only case that reserves nothing.
    vmid: int = 0


@dataclass(frozen=True)
class Job:
    """One fixture and all scheduler state belonging to its run."""

    name: str
    fixture: Path
    iso: str = DEFAULT_ISO
    uefi: bool = True
    disks: int = 1
    #: Read from the fixture rather than listed beside it: what makes a run
    #: long is compiling, and the configuration is what says whether it does.
    heavy: bool = False
    status: JobStatus = JobStatus.WAITING
    vmid: int = 0
    node: str | None = None
    lease: Path | None = None
    log: Path | None = None
    outcome: Outcome | None = None
    collected_at: int | None = None
    thread: threading.Thread | None = None
    execution: Running | None = None

    @property
    def memory_mib(self) -> int:
        return HEAVY_MEMORY_MIB if self.heavy else GUEST_MEMORY_MIB

    @property
    def cores(self) -> int:
        return HEAVY_CORES if self.heavy else GUEST_CORES

    @property
    def collected(self) -> bool:
        return self.status is JobStatus.COLLECTED

    @property
    def running(self) -> bool:
        return self.status is JobStatus.RUNNING

    @property
    def holds_resources(self) -> bool:
        return self.running or self.outcome is not None and not self.outcome.removed

    @property
    def weight(self) -> int:
        return 2 if self.heavy else 1

    def dispatch(
        self,
        node: str,
        vmid: int,
        lease: Path,
        log: Path,
        execution: Running,
    ) -> Job:
        if self.status is not JobStatus.WAITING:
            raise ProxmoxError(f"{self.name} cannot be dispatched from {self.status.value}")
        return replace(
            self,
            status=JobStatus.RUNNING,
            node=node,
            vmid=vmid,
            lease=lease,
            log=log,
            execution=execution,
        )

    def started(self, thread: threading.Thread) -> Job:
        if not self.running or self.thread is not None:
            raise ProxmoxError(f"{self.name} cannot record a worker from {self.status.value}")
        return replace(self, thread=thread)

    def answered(self, outcome: Outcome) -> Job:
        if not self.running or outcome.name != self.name:
            raise ProxmoxError(f"{self.name} cannot accept an outcome from {outcome.name}")
        return replace(
            self,
            status=JobStatus.ANSWERED,
            outcome=replace(
                outcome,
                vmid=outcome.vmid or self.vmid,
                log=outcome.log or self.log,
            ),
        )

    def worker_failed(self, revision: str) -> Job:
        return self.answered(
            Outcome(
                self.name,
                Verdict.ERROR,
                0.0,
                "the worker ended without reporting",
                self.log,
                revision=revision,
                vmid=self.vmid,
            )
        )

    def capacity_failed(self, seconds: float, revision: str) -> Job:
        if self.status is not JobStatus.WAITING:
            raise ProxmoxError(f"{self.name} cannot fail capacity from {self.status.value}")
        return replace(
            self,
            status=JobStatus.ANSWERED,
            outcome=Outcome(
                self.name,
                Verdict.ERROR,
                seconds,
                f"the cluster had no capacity for {seconds:.0f}s",
                phase=Phase.SCHEDULE,
                revision=revision,
            ),
        )

    def collect(self, position: int) -> Job:
        if self.status is not JobStatus.ANSWERED or self.outcome is None:
            raise ProxmoxError(f"{self.name} cannot be collected from {self.status.value}")
        return replace(self, status=JobStatus.COLLECTED, collected_at=position)


#: Below this, over a whole ten-minute look, the guest is not working: a
#: stage3 download that is merely slow still moved several megabytes, and the
#: slowest this network has served one is 40 KiB/s, which is 24 MiB.
QUIET_BYTES: Final[int] = 1024 * 1024


@dataclass
class Watchdog:
    """Whether a guest is still doing anything.

    The console alone is not enough. An install spends minutes downloading a
    stage3 and says nothing at all while it does, so a watchdog reading only
    the serial log ends the guests that are working hardest. The guest's own
    counters are what separate a slow transfer from a dead machine.
    """

    log: Path
    counters: Callable[[], int | None]
    strikes: int = 0
    _seen: int = field(default=0, init=False)
    _moved: int = field(default=0, init=False)
    _counter_before: int = field(default=0, init=False)
    _counter_after: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def moved(self) -> bool:
        with self._lock:
            size = self.log.stat().st_size if self.log.exists() else 0
            talking = size > self._seen
            self._seen = max(self._seen, size)
            return self._observe(talking)

    def _observe(self, talking: bool) -> bool:
        traffic = self.counters()
        if traffic is None:
            if talking:
                self.strikes = 0
            return True
        self._counter_before = self._moved
        self._counter_after = traffic
        working = traffic - self._moved >= QUIET_BYTES
        self._moved = max(self._moved, traffic)
        if talking or working:
            self.strikes = 0
            return True
        self.strikes += 1
        return False

    def idle_reason(self) -> str | None:
        with self._lock:
            if self._observe(talking=False):
                return None
            return (
                "counters were flat "
                f"({self._counter_before} -> {self._counter_after} bytes)"
            )

    @property
    def stuck(self) -> bool:
        return self.strikes >= WATCH_STRIKES


def _download(url: str, target: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=30.0) as answer:
            target.write_bytes(answer.read())
    except OSError as error:
        raise ProxmoxError(f"{url} did not answer: {error}") from error


def _signing_key(status: str) -> str | None:
    for line in status.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[:2] == ["[GNUPG:]", "VALIDSIG"]:
            return fields[-1]
    return None


def verify_release_signature(digests: Path, key: Path, home: Path) -> None:
    """Verify Gentoo's digest signature against the pinned primary key."""
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    imported = subprocess.run(
        ["gpg", "--batch", "--homedir", str(home), "--import", str(key)],
        capture_output=True,
        text=True,
    )
    if imported.returncode != 0:
        raise ProxmoxError(f"the Gentoo release key could not be imported: {imported.stderr[:200]}")
    verified = subprocess.run(
        [
            "gpg",
            "--batch",
            "--homedir",
            str(home),
            "--status-fd",
            "1",
            "--verify",
            str(digests),
        ],
        capture_output=True,
        text=True,
    )
    signed = _signing_key(verified.stdout)
    if verified.returncode != 0 or signed is None:
        raise ProxmoxError(f"the signature on {digests.name} does not verify")
    if signed.upper() != RELENG_FINGERPRINT:
        raise ProxmoxError(
            f"{digests.name} is signed by {signed}, not the pinned {RELENG_FINGERPRINT}"
        )


def _expected_sha512(digests: Path, name: str) -> str:
    rows = digests.read_text().splitlines()
    for index, row in enumerate(rows):
        if row.strip().upper().startswith("# SHA512"):
            for candidate in rows[index + 1 :]:
                fields = candidate.split()
                if len(fields) == 2 and Path(fields[1]).name == name:
                    digest = fields[0].lower()
                    if re.fullmatch(r"[0-9a-f]{128}", digest):
                        return digest
    raise ProxmoxError(f"{digests.name} has no SHA512 line for {name}")


def _medium_name(name: str, sha512: str) -> str:
    return f"{Path(name).stem}-{sha512[:20]}.iso"


def current_minimal() -> tuple[str, tuple[str, ...], str]:
    """The verified current minimal ISO, its mirrors and signed SHA-512."""
    trust = MEDIUM_TRUST
    trust.mkdir(parents=True, exist_ok=True)
    key = RELEASE_KEY
    if not key.is_file():
        key = trust / "service-keys.gpg"
        if not key.is_file():
            _download(RELEASE_KEYRING, key)
    for mirror in MIRRORS:
        pointer = f"{mirror}/{AUTOBUILDS}/{MINIMAL_POINTER}"
        try:
            with urllib.request.urlopen(pointer, timeout=30.0) as answer:
                said = answer.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in said.splitlines():
            first = line.strip().split(" ")[0]
            if first.endswith(".iso"):
                original = first.rsplit("/", 1)[-1]
                digests = trust / f"{original}.DIGESTS"
                last = ""
                for source in MIRRORS:
                    try:
                        _download(f"{source}/{AUTOBUILDS}/{first}.DIGESTS", digests)
                        verify_release_signature(digests, key, trust / "gnupg")
                        sha512 = _expected_sha512(digests, original)
                        return _medium_name(original, sha512), tuple(
                            f"{one}/{AUTOBUILDS}/{first}" for one in MIRRORS
                        ), sha512
                    except ProxmoxError as error:
                        last = str(error)
                raise ProxmoxError(f"no mirror served trusted digests for {original}: {last}")
    raise SystemExit("no mirror named an install medium")


def prepare(
    api: Api,
    node: str,
    medium: str,
    urls: tuple[str, ...],
    sha512: str,
    trust: Path,
    driver_path: Path,
    driver: str,
) -> None:
    """Put the medium and the driver CD on one node's `local` storage.

    `local` is per node, not shared: a guest built on a node without the medium
    is refused with `volume 'local:iso/...' does not exist`, which is what the
    first cluster run hit.
    """
    stamp = trust / "remote" / node / f"{medium}.sha512"
    if medium in api.isos(node):
        try:
            recorded = stamp.read_text().strip()
        except OSError:
            recorded = ""
        if recorded != sha512:
            raise ProxmoxError(
                f"{medium} already exists on {node} without its signed SHA-512 record"
            )
    else:
        last = ""
        for url in urls:
            try:
                api.fetch_iso(node, url, medium, sha512)
                break
            except ProxmoxError as error:
                last = str(error)
        else:
            raise ProxmoxError(f"no mirror served {medium} to {node}: {last}")
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(sha512)
    driver_sha256 = driver_digest(driver_path)
    driver_stamp = trust / "remote" / node / f"{driver}.sha256"
    if driver in api.isos(node):
        try:
            recorded_driver = driver_stamp.read_text().strip()
        except OSError:
            recorded_driver = ""
        if recorded_driver != driver_sha256:
            raise ProxmoxError(
                f"{driver} already exists on {node} without its driver SHA-256 record"
            )
    else:
        api.upload_iso(node, driver_path, driver)
        driver_stamp.parent.mkdir(parents=True, exist_ok=True)
        driver_stamp.write_text(driver_sha256)



#: How long a guest is given to reach a mirror. Generous, because the link
#: this cluster gives its guests is not steady: eight mirrors answered nothing
#: for two minutes and the same guests had fetched a stage3 that morning.
#: Waiting costs a slot; giving up costs the whole run and its diagnosis.
NETWORK_PATIENCE: Final[float] = 900.0

#: Between attempts. A guest that has newly reached a shell is still running
#: dhcpcd, and the installer's own reachability check is what fails: five
#: attempts thirty seconds apart against a host that was answering.
NETWORK_PAUSE: Final[float] = 10.0

#: What the guest is asked, and the two answers it can give. Neither answer
#: appears whole in the command: the shell echoes the line it was given, and a
#: reader waiting for `NETWORK_UP` matched that echo and returned on the first
#: pass with no address on the interface at all.
NETWORK_UP: Final[str] = "NETWORK_UP"
#: Printed when every target was tried and none answered. Not `DOWN`, because
#: the loop prints it after `UP` as well and a reader has to tell them apart.
NETWORK_DONE: Final[str] = "NETWORK_DONE"
#: Several mirrors, and any one of them answering is enough. Asking only
#: Gentoo's own said `down` on a network where four Chinese mirrors were
#: answering, and asking only a Chinese one would say the same the other way.
NETWORK_TARGETS: Final[tuple[str, ...]] = tuple(
    f"{one}/{AUTOBUILDS}/latest-stage3-amd64-systemd.txt" for one in MIRRORS
)
NETWORK_PROBE: Final[str] = (
    "for one in "
    + " ".join(NETWORK_TARGETS)
    + "; do curl -sS -o /dev/null --max-time 15 \"$one\" && { "
    "printf 'NETWORK_%s\\n' UP; break; }; done; "
    "printf 'NETWORK_%s\\n' DONE"
)


#: What gets the guest an address on this cluster. Its network carries a ULA
#: IPv6 prefix advertised by something that is not the hypervisor and offers no
#: way out of itself, and the addresses that reach a mirror come from DHCPv4:
#: `dhcpcd -4` answered `leased 10.31.0.230` and `default via 10.31.0.254`, and
#: `curl` then returned 200 where it had timed out for fifteen minutes.
#:
#: Asked for on the first pass, not after half the patience: seventeen of
#: twenty-four guests in one round waited out the whole window for an address
#: nothing was going to hand them. Only when there is no IPv4 default route
#: already, so a medium whose own manager configured one is left alone.
#: The network the guests are on, and the router that serves it. The DHCP
#: server runs on that same Raspberry Pi and answers intermittently under
#: load, so a guest is given an address rather than asking for one.
GUEST_NETWORK: Final[str] = "10.31.0"
GUEST_PREFIX: Final[int] = 24
GUEST_GATEWAY: Final[str] = f"{GUEST_NETWORK}.254"

#: Where the static addresses start. One per VMID, so two guests of one
#: campaign cannot collide and the address says which guest it is.
#:
#: 150 rather than 100: a probe on the segment found 10.31.0.106 through .115
#: answering with locally administered MAC addresses, which are other people's
#: machines on this cluster. Four guests a round took an address one of them
#: already held. From .150 to .199 nothing answered.
GUEST_ADDRESS_BASE: Final[int] = 150

#: Every resolver, in the order glibc tries them. The router's own is first
#: because it resolves names on this network too; the public ones follow so a
#: guest is not stranded when that Pi is busy.
GUEST_RESOLVERS: Final[tuple[str, ...]] = (GUEST_GATEWAY, "1.1.1.1", "8.8.8.8")


#: What a guest outside the cluster does, where no address is reserved for it.
#: Any daemon from an earlier attempt is stopped first: dhcpcd that finds one
#: running prints `sending commands to dhcpcd process` and returns at once.
#: `--noarp`, because the handshake otherwise stalls in `probing address`
#: until dhcpcd gives up.
ASK_FOR_IPV4: Final[str] = (
    "ip -4 route show default | grep -q . || { "
    'for one in /sys/class/net/e*; do dev=$(basename "$one"); ip link set "$dev" up; '
    'dhcpcd -x "$dev" >/dev/null 2>&1; pkill -x dhcpcd >/dev/null 2>&1; '
    'dhcpcd -4 --noarp -w -t 45 "$dev" >/dev/null 2>&1 || true; done; }; '
    "ip -4 route show default | grep -q . || true"
)


def static_address(vmid: int) -> str:
    """The address this guest takes, derived from its VMID.

    Derived rather than allocated: two campaigns picking from a pool both read
    the same entry as free, and the VMID is already unique per guest.
    """
    return f"{GUEST_NETWORK}.{GUEST_ADDRESS_BASE + vmid - VMID_FIRST}"


def configure_statically(address: str) -> str:
    """Give the interface `address`, or the next free one after it.

    This segment carries other people's machines: a probe found 10.31.0.106
    through .115 answering with locally administered MAC addresses, none of
    them ours. Walking forward from the address this guest was assigned keeps
    the collision impossible without depending on the DHCP server, which runs
    on a Raspberry Pi here and answers intermittently.
    """
    network, last = address.rsplit(".", 1)
    ceiling = GUEST_ADDRESS_BASE + (VMID_LAST - VMID_FIRST)
    gateway = GUEST_GATEWAY
    prefix = GUEST_PREFIX
    # `\\n` for printf, not a real newline: the whole thing is one line sent to
    # a serial console, and a literal break there is two commands.
    resolvers = "".join(f"nameserver {one}\\n" for one in GUEST_RESOLVERS)
    return (
        # Nothing at all once there is a default route. Without this guard the
        # second pass probed the address the first pass had taken, `arping -D`
        # answered that something holds it — this guest — and the fallback
        # then tore the working configuration down again.
        "ip -4 route show default | grep -q . || { "
        'for one in /sys/class/net/e*; do dev=$(basename "$one"); ip link set "$dev" up; '
        # The next free address from this one, rather than the DHCP server: an
        # address somebody else holds is a collision with a real machine, and
        # this segment has machines scattered through the range. `arping -D`
        # is a duplicate-address probe: it asks whether anything answers and
        # says so without claiming it.
        f'n={last}; while [ "$n" -le {ceiling} ]; do '
        'if arping -D -c 2 -w 3 -I "$dev" ' + f"{network}.$n" + ' >/dev/null 2>&1; then '
        'ip -4 addr add ' + f"{network}.$n/{prefix}" + ' dev "$dev" 2>/dev/null && break; fi; '
        'n=$((n + 1)); done; '
        f'ip -4 route replace default via {gateway} dev "$dev" 2>/dev/null; '
        "done; }; "
        f"printf '{resolvers}' > /etc/resolv.conf; "
        "ip -4 route show default | grep -q . || true"
    )


#: What the official minimal medium asks before it hands over a shell, and
#: what answers it. Nothing answered: two guests on one round sat at `Load
#: keymap (Enter for default):` while the run spent its patience waiting for a
#: prompt that was one keystroke away.
KEYMAP_QUESTION: Final[str] = r"Load keymap|keymap \(Enter for default\)"

#: How long the medium is given to reach a shell, question or no question.
PROMPT_PATIENCE: Final[float] = 900.0


def reach_prompt(link: Reconnecting, patience: float = PROMPT_PATIENCE) -> None:
    """Wait for a root prompt, answering the medium's questions on the way."""
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        said = link.expect(
            rf"livecd .*#|localhost .*#|{KEYMAP_QUESTION}",
            timeout=max(30.0, deadline - time.monotonic()),
        )
        if not re.search(KEYMAP_QUESTION.encode(), said):
            return  # The prompt matched, which is what this waits for.
        # The default is what every fixture wants, and the question waits for
        # a key rather than answering itself.
        link.send("")
    raise ConsoleTimeout(f"the medium did not reach a shell in {patience:.0f}s")


def wait_for_network(link: Reconnecting, vmid: int = 0) -> None:
    """Configure the guest's interface, then wait until it can reach a mirror.

    The medium boots with `nodhcp` and leaves the link unconfigured, so
    something has to configure it, which is what an operator does before
    installing. The probe goes first in case the medium's own manager got
    there.

    `vmid` gives the guest its own address. Zero falls back to asking the DHCP
    server, which is what a run outside the cluster does.
    """
    deadline = time.monotonic() + NETWORK_PATIENCE
    configure = configure_statically(static_address(vmid)) if vmid else ASK_FOR_IPV4
    asked = False
    while time.monotonic() < deadline:
        link.send(NETWORK_PROBE)
        said = link.expect(rf"{NETWORK_UP}|{NETWORK_DONE}", timeout=180.0)
        if NETWORK_UP.encode() in said:
            return
        # Every pass, not once: the fallback asks a DHCP server that answers
        # intermittently, and the static path is cheap to repeat.
        link.run(configure, timeout=120.0)
        asked = True
        time.sleep(NETWORK_PAUSE)
    # What the guest actually had, into the log this run leaves behind. Eight
    # cluster guests failed here on one round and the log held nothing but
    # `Could not connect to server`, so nothing said whether the medium never
    # configured an interface or the cluster gave it no route.
    for question in (
        "ip -oneline address show",
        "ip -4 route show default; ip -6 route show default",
        "cat /etc/resolv.conf",
    ):
        link.run(question, timeout=60.0)
    raise ConsoleTimeout(f"the guest had no network after {NETWORK_PATIENCE:.0f}s")


def stage_passphrases(link: Reconnecting, installation: InstallConfig) -> None:
    """Put the passphrases where the layout says they are.

    An operator does this by hand before an unattended install. Without it an
    encrypted layout stops at `pool: /run/gentoo-install-keys/pool cannot be
    read`, which is the installer refusing correctly and the harness not having
    done its part.
    """
    graph = installation.disk.graph
    wanted = [node.passphrase_file for node in graph.of_type(Luks) if node.passphrase_file]
    wanted += [node.passphrase_file for node in graph.of_type(ZfsPool) if node.passphrase_file]
    for source in wanted:
        parent = PurePosixPath(source).parent
        link.run(f"mkdir -p {parent} && chmod 700 {parent}")
        link.run(f"printf '%s' '{DISK_PASSPHRASE}' > {source}")
        link.run(f"chmod 600 {source}")


def rewrite_fixtures(
    jobs: list[Job], into: Path, region: MirrorRegion, sync: Sync
) -> Path:
    """Write each fixture out again with its mirror region and sync replaced.

    Through the parser and the writer, not a text substitution: a fixture that
    cannot survive the round trip is a defect worth failing on here rather than
    an hour into an install.

    Both defaults come from what this network measured, not from what a
    Chinese cluster is assumed to look like. Its guests have a ULA address and
    reach the world through NAT64, and from inside one:

    - `github.com` never connects, so the default `git` sync cannot be used
      and `rsync` is what the fixtures run with here;
    - `mirrors.tuna.tsinghua.edu.cn` connects but transfers at 128 KiB/s,
      which is an hour for one stage3;
    - `distfiles.gentoo.org`, a CDN, is the fast one, so `global` is the
      region that finishes.

    The node itself has none of these limits: it fetched a 1 GiB ISO from
    tuna in the same run. Only the guest network is this shape.
    """
    into.mkdir(parents=True, exist_ok=True)
    for job in jobs:
        config = load(job.fixture)
        moved = replace(
            config,
            portage=replace(
                config.portage,
                sync=sync,
                mirrors=replace(config.portage.mirrors, region=region),
            ),
        )
        (into / job.fixture.name).write_text(to_toml(moved))
    return into


@dataclass(frozen=True)
class Lease:
    node: str
    vmid: int
    nonce: str
    pid: int


def _lease_path(workdir: Path, lease: Lease) -> Path:
    return workdir / "leases" / f"{lease.vmid}-{lease.nonce}.json"


def _write_lease(workdir: Path, lease: Lease) -> Path:
    path = _lease_path(workdir, lease)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(lease.__dict__, sort_keys=True))
    temporary.replace(path)
    return path


def _read_lease(path: Path) -> Lease | None:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return None
        node = raw.get("node")
        vmid = raw.get("vmid")
        nonce = raw.get("nonce")
        pid = raw.get("pid")
        if not isinstance(node, str) or not isinstance(vmid, int):
            return None
        if not isinstance(nonce, str) or not nonce or not isinstance(pid, int):
            return None
        return Lease(node, vmid, nonce, pid)
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reconcile(api: Api, workdir: Path) -> None:
    """Remove expired guests only when a local lease supplies their nonce."""
    owned = set(api.ours())
    directory = workdir / "leases"
    for path in sorted(directory.glob("*.json")):
        lease = _read_lease(path)
        if lease is None or _pid_alive(lease.pid):
            continue
        if (lease.node, lease.vmid) not in owned:
            continue
        guest = Guest(
            api,
            lease.node,
            lease.vmid,
            GuestSpec(name="expired-lease", iso="", nonce=lease.nonce),
        )
        guest.destroy()
        path.unlink()


def revision_identity(driver: Path) -> str:
    """Git state and exact driver bytes represented by a campaign outcome."""

    def ask(argv: list[str]) -> str:
        try:
            result = subprocess.run(
                argv, cwd=REPOSITORY, capture_output=True, text=True
            )
        except OSError:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    described = ask(["git", "describe", "--always", "--dirty"]) or "unknown"
    changed = len(ask(["git", "status", "--short"]).splitlines())
    return f"{described} dirty={changed} driver-sha256={driver_digest(driver)}"


def trusted_revision(identity: str) -> bool:
    """Whether an outcome identifies a clean tree or an explicit test identity."""
    dirty = re.search(r"(?:^| )dirty=(\d+)(?: |$)", identity)
    return bool(identity) and (dirty is None or dirty.group(1) == "0")


def free_slots(api: Api, placed: Mapping[str, int] | None = None) -> list[Node]:
    """One entry per guest the cluster can still hold, most free node first.

    A node appears as many times as it has room for, not once: returning one
    slot per node capped a six-node cluster with 51 GiB spare at three guests
    at a time, with the queue twenty deep.

    `placed` is what this schedule has already put on each node, and it is
    subtracted from what the node reports. A guest's memory is allocated
    lazily, so a node with eleven of them freshly started still reported 13.8
    GiB free: reading that alone dispatched twenty guests wanting 120 GiB onto
    a cluster with 71, on hardware that is running other people's machines.
    """
    need = GUEST_MEMORY_MIB * 1024**2
    held = placed or {}
    per_node: list[list[Node]] = []
    for node in api.nodes():
        room = node.free_bytes - NODE_HEADROOM_BYTES - held.get(node.name, 0) * need
        per_node.append([node] * max(0, int(room // need)))
    # One from each node in turn, rather than a node's whole share before the
    # next one is touched: five guests went onto `infra-node5` and left the
    # other five idle, so one node carried every build while the shared
    # storage lock and its four cores were contended by all of them.
    slots: list[Node] = []
    for index in range(max((len(one) for one in per_node), default=0)):
        for one in per_node:
            if index < len(one):
                slots.append(one[index])
    return slots


def room_for(node: Node, job: Job, placed: Mapping[str, int] | None = None) -> bool:
    """Whether this node can carry this job now.

    A heavy guest asks for twice the memory, so a slot list built from the
    light size does not answer for it: eight of them were dispatched onto
    nodes with room for four and the last four were killed by the hypervisor.
    """
    held = (placed or {}).get(node.name, 0) * GUEST_MEMORY_MIB * 1024**2
    room = node.free_bytes - NODE_HEADROOM_BYTES - held
    return room >= job.memory_mib * 1024**2


class Stoppable(Protocol):
    """What the sweep and the closing path need of a guest.

    Stopping is what wakes the worker blocked reading its console. During the
    schedule deleting is the worker's own job and a sweep that deleted would
    race it; once the closing path has joined the workers there is nobody left
    to race, and a guest still held there is one nothing else will remove.
    """

    def stop(self) -> None: ...

    def destroy(self) -> None: ...


@dataclass
class Running:
    """A guest in flight, and what the watchdog needs to judge it."""

    guest: Stoppable
    watch: Watchdog
    created: bool = False


def _execution(
    api: Api,
    node: str,
    job: Job,
    driver: str,
    workdir: Path,
    vmid: int,
    nonce: str,
) -> Running:
    log = workdir / f"{job.name}.log"
    guest = Guest(
        api=api,
        node=node,
        vmid=vmid or api.free_vmid(),
        spec=GuestSpec(
            name=f"gi-{job.name}"[:63],
            iso=job.iso,
            memory_mib=job.memory_mib,
            cores=job.cores,
            target_gib=tuple(TARGET_GIB for _ in range(job.disks)),
            uefi=job.uefi,
            driver_iso=driver,
            nonce=nonce or f"gi-{uuid.uuid4().hex[:12]}",
        ),
    )
    return Running(guest, Watchdog(log=log, counters=lambda: guest.transferred()))


def _reserve_job(
    api: Api,
    node: str,
    job: Job,
    driver: str,
    workdir: Path,
    held_vmids: frozenset[int],
) -> Job:
    """Create a cluster guest before recording its lease and dispatch."""
    excluded = set(held_vmids)
    nonce = f"gi-{uuid.uuid4().hex[:12]}"
    conflict: CreateConflict | None = None
    for _ in range(RESERVATION_TRIES):
        vmid = api.free_vmid(frozenset(excluded))
        execution = _execution(api, node, job, driver, workdir, vmid, nonce)
        guest = cast(Guest, execution.guest)
        try:
            guest.create()
        except CreateConflict as error:
            excluded.add(vmid)
            conflict = error
            continue
        execution.created = True
        lease: Path | None = None
        try:
            lease = _write_lease(
                workdir, Lease(guest.node, guest.vmid, guest.spec.nonce, os.getpid())
            )
            return job.dispatch(
                guest.node,
                guest.vmid,
                lease,
                execution.watch.log,
                execution,
            )
        except Exception as error:
            if lease is not None:
                lease.unlink(missing_ok=True)
            try:
                guest.destroy()
            except ProxmoxError as cleanup:
                raise ProxmoxError(
                    f"reservation bookkeeping failed: {error}; "
                    f"VM {guest.vmid} was not removed: {cleanup}"
                ) from error
            raise
    assert conflict is not None
    raise conflict


def install_one(
    api: Api,
    node: str,
    job: Job,
    driver: str,
    workdir: Path,
    inflight: dict[str, Running] | None = None,
    vmid: int = 0,
    nonce: str = "",
    revision: str = "",
    execution: Running | None = None,
) -> Outcome:
    """Build a guest, install into it, read the result, delete the guest."""
    started = time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    held = execution or _execution(
        api, node, job, driver, workdir, vmid=vmid, nonce=nonce
    )
    guest = cast(Guest, held.guest)
    log = held.watch.log
    phase = Phase.CREATE
    watch = held.watch
    outcome: Outcome | None = None
    if inflight is not None:
        inflight[job.name] = held
    try:
        if not held.created:
            guest.create()
        guest.start()
        phase = Phase.BOOT_LIVE
        log.write_text(f"installer revision: {revision}\n")
        link = Reconnecting.to(guest, log)
        console = link.console
        # Reset with the console attached: termproxy forwards only what arrives
        # after it, and the firmware is finished before it gets there.
        guest.reset()
        if job.uefi:
            append_to_cmdline(link, EXTRA_CMDLINE)
        else:
            _edit_bios_cmdline(guest, link)
        reach_prompt(link)
        # The guest's own resolver is left alone. A local run pins one because
        # slirp reads the host's `/etc/resolv.conf` once at startup; the
        # cluster hands out a real configuration, and it is IPv6 with DNS64.
        # Writing IPv4 resolvers over it left every mirror unreachable:
        # `Failed to connect to mirrors.tuna.tsinghua.edu.cn:443 after 111 ms`.
        wait_for_network(link, guest.vmid)
        stage_passphrases(link, load(job.fixture))
        link.run("mkdir -p /mnt/driver")
        link.run("mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver")
        link.run(f"mkdir -p {RESULT_DIR}")
        # tee, not a redirect: the serial console is the only way to watch a
        # run that takes half an hour, and it is what the watchdog reads to
        # tell a slow mirror from a dead guest.
        # `wait_for`, not `run`: a console dropped mid-install is reopened and
        # listened to again, never handed the command a second time.
        phase = Phase.INSTALL
        link.wait_for(
            f"{{ sh /mnt/driver/install.sh --config fixtures/{job.fixture.name}; "
            f"echo $? > {RESULT_DIR}/install.rc; }} 2>&1 | tee {RESULT_DIR}/install.txt",
            timeout=RUN_CEILING,
            idle=INSTALL_IDLE,
            watch=watch,
        )
        files = collect(guest, link, log)
        code = files.get("install.rc", b"").strip()
        if code != b"0":
            outcome = Outcome(
                job.name,
                Verdict.FAIL,
                time.monotonic() - started,
                f"the installer exited {code!r}",
                log,
                phase=phase,
                revision=revision,
            )
            return outcome
        # The install finishing is half the question. The other half is whether
        # the machine it produced comes up and carries what was asked for, and
        # nothing in the log above can answer that.
        phase = Phase.BOOT_INSTALLED
        wrong = boot_and_check(guest, link, log, load(job.fixture))
        if wrong:
            outcome = Outcome(
                job.name,
                Verdict.FAIL,
                time.monotonic() - started,
                wrong,
                log,
                phase=phase,
                revision=revision,
            )
            return outcome
        outcome = Outcome(
            job.name,
            Verdict.OK,
            time.monotonic() - started,
            log=log,
            phase=phase,
            revision=revision,
        )
        return outcome
    except (ConsoleTimeout, ConsoleClosed) as error:
        verdict = Verdict.STUCK if watch.stuck else Verdict.ERROR
        outcome = Outcome(
            job.name,
            verdict,
            time.monotonic() - started,
            str(error)[:300],
            log,
            phase=phase,
            revision=revision,
        )
        return outcome
    except (ProxmoxError, ResultError, OSError) as error:
        outcome = Outcome(
            job.name,
            Verdict.ERROR,
            time.monotonic() - started,
            str(error)[:300],
            log,
            phase=phase,
            revision=revision,
        )
        return outcome
    finally:
        if inflight is not None:
            inflight.pop(job.name, None)
        try:
            guest.destroy()
        except ProxmoxError as error:
            # Said rather than raised: an undeleted guest holds a node's memory
            # and the operator has to know, but the result already exists. The
            # slot stays held below, because the memory does.
            print(f"{job.name}: the guest was not removed: {error}", file=sys.stderr)
            if outcome is not None:
                outcome.removed = False


def answer_once(
    done: "queue.Queue[Outcome]",
    api: Api,
    node: str,
    job: Job,
    driver: str,
    workdir: Path,
    inflight: dict[str, Running] | None,
    vmid: int = 0,
    nonce: str = "",
    revision: str = "",
    execution: Running | None = None,
) -> None:
    """Run one job and put exactly one outcome on the queue, whatever happens.

    A worker that dies without answering leaves its name in the running set
    for ever and the schedule never ends: a `WebSocketError` out of a dropped
    console was outside the handled set, and a run sat idle for half an hour
    with an empty cluster and a job still queued.
    """
    try:
        outcome = install_one(
            api,
            node,
            job,
            driver,
            workdir,
            inflight,
            vmid,
            nonce,
            revision,
            execution,
        )
        outcome = replace(outcome, vmid=outcome.vmid or vmid)
        done.put(outcome)
    except Exception as error:
        done.put(
            Outcome(
                job.name,
                Verdict.ERROR,
                0.0,
                f"{type(error).__name__}: {error}"[:300],
                removed=False,
                revision=revision,
                vmid=vmid,
            )
        )


def run(
    jobs: list[Job],
    workdir: Path,
    limit: int = 0,
    stamp: int = 0,
    region: MirrorRegion = MirrorRegion.GLOBAL,
    sync: Sync = Sync.RSYNC,
) -> list[Outcome]:
    """Every job, collected one at a time as each finishes.

    `limit` caps how many run at once; zero asks the cluster what fits.
    """
    # A repeated name would replace the only record carrying that job's state.
    repeated = sorted({one.name for one in jobs if [j.name for j in jobs].count(one.name) > 1})
    if repeated:
        raise ProxmoxError(f"named more than once: {repeated}")
    workdir = confined(workdir)
    api = Api()
    workdir.mkdir(parents=True, exist_ok=True)
    reconcile(api, workdir)
    # Packed: the ingress refuses the 1.4 MiB loose-file CD with `413`.
    driver_path = build_driver(
        workdir / "driver.iso",
        packed=True,
        fixtures=rewrite_fixtures(jobs, workdir / "fixtures", region, sync),
    )
    revision = revision_identity(driver_path)
    driver = remote_name(driver_path)
    medium, urls, medium_sha512 = current_minimal()
    prepared: set[str] = set()
    done: queue.Queue[Outcome] = queue.Queue()
    scheduled = {job.name: job for job in jobs}
    collected = 0
    swept = time.monotonic()
    capacity_since: float | None = None

    try:
        while not all(job.collected for job in scheduled.values()):
            waiting = [job for job in scheduled.values() if job.status is JobStatus.WAITING]
            running = [job for job in scheduled.values() if job.running]
            placed = Counter(
                job.node
                for job in scheduled.values()
                if job.holds_resources and job.node is not None
                for _ in range(job.weight)
            )
            slots = free_slots(api, placed)
            if limit:
                slots = slots[: max(0, limit - len(running))]
            if waiting and not running and not slots:
                now = time.monotonic()
                if capacity_since is None:
                    capacity_since = now
                waited = now - capacity_since
                if waited >= CAPACITY_PATIENCE:
                    for job in waiting:
                        scheduled[job.name] = job.capacity_failed(waited, revision).collect(
                            collected
                        )
                        collected += 1
                    continue
                time.sleep(min(POLL_WHILE_QUEUED, CAPACITY_PATIENCE - waited))
                continue
            capacity_since = None
            while waiting and slots:
                # The first job this node has room for, not the first job:
                # a heavy guest wants twice the memory, and taking it from a
                # node with one light slot left is how the hypervisor came to
                # kill it. A light job behind it still goes now.
                index = next(
                    (at for at, one in enumerate(waiting) if room_for(slots[0], one, placed)),
                    None,
                )
                if index is None:
                    break
                node = slots.pop(0)
                if node.name not in prepared:
                    prepare(
                        api,
                        node.name,
                        medium,
                        urls,
                        medium_sha512,
                        MEDIUM_TRUST,
                        driver_path,
                        driver,
                    )
                    prepared.add(node.name)
                job = waiting.pop(index)
                if job.iso == DEFAULT_ISO:
                    job = replace(job, iso=medium)
                held_vmids = frozenset(
                    one.vmid
                    for one in scheduled.values()
                    if one.holds_resources and one.vmid
                )
                job = _reserve_job(
                    api,
                    node.name,
                    job,
                    driver,
                    workdir,
                    held_vmids,
                )
                execution = job.execution
                if execution is None:
                    raise ProxmoxError(f"{job.name} has no reserved guest")
                guest = cast(Guest, execution.guest)
                vmid = guest.vmid
                nonce = guest.spec.nonce
                thread = threading.Thread(
                    target=answer_once,
                    args=(
                        done,
                        api,
                        node.name,
                        job,
                        driver,
                        workdir,
                        None,
                        vmid,
                        nonce,
                        revision,
                        execution,
                    ),
                    daemon=True,
                )
                scheduled[job.name] = job.started(thread)
                placed[node.name] += job.weight
                thread.start()
                print(f"→ {job.name} on {node.name} ({len(waiting)} waiting)", flush=True)
                if waiting and slots:
                    time.sleep(STAGGER)
            try:
                # Collected one at a time, never as a set: a fixture that takes
                # an hour must not hold back one that took six minutes. Waiting
                # jobs shorten the wait, because capacity frees between looks.
                outcome = done.get(timeout=POLL_WHILE_QUEUED if waiting else WATCH_EVERY)
            except queue.Empty:
                unanswered = _unanswered_jobs(scheduled, done.empty())
                for job in unanswered:
                    failed = job.worker_failed(revision)
                    if failed.outcome is None:
                        raise ProxmoxError(f"{job.name} failed without an outcome")
                    if job.execution is None:
                        failed.outcome.removed = False
                    else:
                        try:
                            job.execution.guest.destroy()
                        except ProxmoxError as error:
                            failed.outcome.removed = False
                            failed.outcome.detail += f"; the guest was not removed: {error}"
                    done.put(failed.outcome)
                if unanswered:
                    continue
                if time.monotonic() - swept >= WATCH_EVERY:
                    _sweep_jobs(scheduled)
                    swept = time.monotonic()
                continue
            job = scheduled[outcome.name].answered(outcome)
            if outcome.removed and job.lease is not None:
                job.lease.unlink(missing_ok=True)
            job = job.collect(collected)
            collected += 1
            scheduled[job.name] = job
            if job.node is not None and not outcome.removed:
                print(
                    f"  {outcome.name} still holds a slot on {job.node}",
                    file=sys.stderr,
                )
            print(
                f"{outcome.verdict.value:6} {outcome.name:34} {outcome.seconds / 60:5.1f}m "
                f"{outcome.detail}",
                flush=True,
            )
    finally:
        _abandon_jobs(scheduled)
        for node_name in prepared:
            said = api.remove_iso(node_name, driver)
            if said:
                print(f"{driver} stayed on {node_name}: {said}", file=sys.stderr)
    ordered = sorted(
        scheduled.values(),
        key=lambda job: job.collected_at if job.collected_at is not None else len(scheduled),
    )
    outcomes: list[Outcome] = []
    for job in ordered:
        if job.outcome is None:
            raise ProxmoxError(f"{job.name} was collected without an outcome")
        outcomes.append(job.outcome)
    return outcomes


#: How many times the results are asked for again on a fresh console.
COLLECT_TRIES: Final[int] = 3

#: How long the archive has to arrive. An install log runs to twelve megabytes
#: and compresses to about one, which is another third again as base64, and
#: the console carries it a chunk at a time. A fixed window of three minutes
#: caught only the shell's echo of the command and reported `the console
#: result is not base64`; the end marker is what says it is finished.
COLLECT_PATIENCE: Final[float] = 900.0

#: How many times a dropped console is reopened before a run is given up on.
RECONNECT_TRIES: Final[int] = 4


#: What says a command started and what says it finished. Neither appears
#: whole in the line the console is given: the shell echoes that line back,
#: and a reader waiting for the end marker matched the echo and returned
#: before the command had run. `printf` assembles them in the guest instead.
_BEGIN_TEXT: Final[str] = "MARK_{token}_BEGIN"
_DONE_TEXT: Final[str] = "MARK_{token}_DONE"


_Result = TypeVar("_Result")


def _marked(command: str, token: int) -> str:
    return (
        f"printf 'MARK_%s_BEGIN\\n' {token}; {command}; printf 'MARK_%s_DONE\\n' {token}"
    )


def _begin(token: int) -> str:
    return _BEGIN_TEXT.format(token=token)


def _done(token: int) -> str:
    return _DONE_TEXT.format(token=token)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


class Reconnecting:
    """A console that opens another one when the cluster drops it.

    A `termproxy` session does not survive an install, and under a full
    schedule it does not always survive a boot: three guests reported `the
    guest closed the serial connection` a minute in, with the kernel visibly
    running in what the log had already captured. The guest is fine; only the
    connection to it is gone.

    `run` re-sends its command after a reconnect, because the shell never
    received it. `wait_for` does not: the command is already running in the
    guest, and sending it again would start a second install.
    """

    def __init__(self, open_console: Callable[[], Line], tries: int = RECONNECT_TRIES) -> None:
        self._open = open_console
        self._tries = tries
        self._marks = itertools.count(1)
        self.console: Line = open_console()

    @classmethod
    def to(cls, guest: Guest, log: Path, tries: int = RECONNECT_TRIES) -> Reconnecting:
        return cls(lambda: SerialConsole(guest.console(), log.open("ab")), tries)

    def reopen(self) -> None:
        self.console = self._open()
        # The reopened console shows nothing until the shell is asked for a
        # prompt, and every wait below is looking for text.
        self.console.send("")

    def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
        return self._with_reconnect(
            timeout, lambda deadline: self.console.expect(pattern, _remaining(deadline))
        )

    def run(self, command: str, timeout: float = 120.0) -> None:
        def run_once(deadline: float) -> None:
            token = next(self._marks)
            self.console.send(_marked(command, token))
            self.console.expect(_done(token), _remaining(deadline))

        self._with_reconnect(timeout, run_once)

    def wait_for(
        self,
        command: str,
        timeout: float,
        idle: float = 0.0,
        watch: Watchdog | None = None,
    ) -> None:
        """Send a command once and wait for it however long it takes.

        Reconnecting does not re-send it: an install that is already running
        would be started a second time on a target it has half written.

        `idle` is measured from the last byte the guest sent. An install that
        prints for three hours is working and a single ceiling ends it anyway.
        """
        token = next(self._marks)
        sent = False

        def wait_once(deadline: float) -> None:
            nonlocal sent
            if not sent:
                sent = True
                self.console.send(_marked(command, token))
            while True:
                try:
                    self.console.expect(_done(token), _remaining(deadline), idle=idle)
                    return
                except ConsoleIdle as error:
                    if watch is None:
                        raise
                    reason = watch.idle_reason()
                    if reason is None:
                        continue
                    raise ConsoleTimeout(
                        f"{error}; console was silent for {idle:.0f}s and {reason}"
                    ) from error

        self._with_reconnect(timeout, wait_once)

    def expect_output(self, command: str, timeout: float = 120.0) -> bytes:
        """Run a command and answer with what it printed, and nothing else.

        Between the two markers, not up to the last one: the shell echoes the
        line it was given, so what came back began with the command itself.
        `findmnt --output TARGET,SOURCE,FSTYPE` was checked for `/` and the
        echo of that command carries one, so the check passed on a guest whose
        `findmnt` printed nothing at all.
        """
        def collect_once(deadline: float) -> bytes:
            token = next(self._marks)
            self.console.send(_marked(command, token))
            self.console.expect(_begin(token), _remaining(deadline))
            said = self.console.expect(_done(token), _remaining(deadline))
            return said.split(_DONE_TEXT.format(token=token).encode())[0]

        return self._with_reconnect(timeout, collect_once)

    def _with_reconnect(
        self, timeout: float, operation: Callable[[float], _Result]
    ) -> _Result:
        deadline = time.monotonic() + timeout
        closed: ConsoleClosed | None = None
        for attempt in range(self._tries):
            if closed is not None and _remaining(deadline) <= 0.0:
                raise closed
            try:
                return operation(deadline)
            except ConsoleClosed as error:
                closed = error
                if attempt + 1 == self._tries or _remaining(deadline) <= 0.0:
                    raise
                self.reopen()
        raise ConsoleClosed("the console could not be reopened")

    def send(self, line: str) -> None:
        self._reopen_if_closed()
        self.console.send(line)

    def send_raw(self, keys: str) -> None:
        self._reopen_if_closed()
        self.console.send_raw(keys)

    def _reopen_if_closed(self) -> None:
        """A write to a dropped connection is silently discarded.

        That is what the transport should do rather than raise, and it left
        the command undelivered: `wait_for_network` sent its probe into a
        closed socket and then waited fifteen minutes for output that was
        never going to come. Eight guests failed that way in one round.
        """
        if not self.console.closed:
            return
        for attempt in range(self._tries):
            try:
                self.reopen()
                return
            except (ConsoleClosed, OSError):
                if attempt + 1 == self._tries:
                    raise

    def snapshot(self, seconds: float) -> bytes:
        return self.console.snapshot(seconds)

    @property
    def closed(self) -> bool:
        return self.console.closed


#: The password `tests/fixtures/*.toml` set on the installed system. It exists
#: so the harness can log into what it built; nothing else uses it.
INSTALLED_PASSWORD: Final[str] = "install"

#: What the installed system is asked about, and what the answer has to hold.
#: One table, so a check cannot be added without saying what would fail it.
INSIDE: Final[tuple[tuple[str, str, str], ...]] = (
    ("os-release", "cat /etc/os-release", "Gentoo"),
    ("fstab", "cat /etc/fstab", "UUID="),
)


def _asked_for(installation: InstallConfig) -> list[tuple[str, str, str]]:
    """What this particular configuration should have produced.

    Derived from the model rather than written out once for every fixture:
    `hostname` and `kernel` carried an empty expectation, so a guest with the
    wrong hostname, the wrong root filesystem or the wrong locale passed every
    check, and a run that installed the wrong system was recorded as `ok`.
    """
    graph = installation.disk.graph
    root = graph.nodes.get(installation.disk.root)
    source = graph.nodes.get(root.source) if isinstance(root, Mountpoint) else None
    checks = [
        ("locale", "locale", f"LANG={installation.system.locale}"),
        ("hostname", "hostname", installation.system.hostname),
    ]
    if isinstance(source, Filesystem):
        # The type as `findmnt` names it, on the row for `/`: an xfs root that
        # came up as ext4 is an install that went wrong quietly.
        checks.append(
            (
                "root filesystem",
                "findmnt --noheadings --output FSTYPE /",
                source.kind.value,
            )
        )
    elif isinstance(source, Subvolume):
        checks.append(("root filesystem", "findmnt --noheadings --output FSTYPE /", "btrfs"))
    elif isinstance(source, ZfsDataset):
        checks.append(("root filesystem", "findmnt --noheadings --output FSTYPE /", "zfs"))
    checks.append(
        (
            "init",
            # Not `ls -l /sbin/init`: on OpenRC that is sysvinit's own binary,
            # a regular file whose listing never carries the word, so the check
            # could not pass. Both inits leave a directory under /run.
            "test -d /run/openrc && echo openrc || "
            "{ test -d /run/systemd/system && echo systemd || echo unknown; }",
            "systemd" if installation.system.init is InitSystem.SYSTEMD else "openrc",
        )
    )
    return checks


#: How long SeaBIOS and GRUB take to reach the cryptomount prompt. Nothing on
#: the serial port marks it, so the passphrase is typed after this, and a
#: wrong guess costs one retry at the next prompt, which is waited for.
GRUB_PROMPT_SECONDS: Final[float] = 30.0

#: How many prompts are answered before the passphrase is called wrong. A
#: wrong one brings the prompt back and each one re-arms the timeout, so an
#: unbounded loop would never fail.
UNLOCK_TRIES: Final[int] = 5


class Typeable(Protocol):
    """All the unlock step needs of a guest: GRUB's prompt never reaches the
    serial port, so the passphrase goes to the screen as keystrokes."""

    def send_keys(self, keys: list[str]) -> None: ...


def _unlock(guest: Typeable, link: Reconnecting, installation: InstallConfig) -> str:
    """Answer every passphrase prompt on the way to a login.

    Nothing did, so an encrypted install that had worked was failed ten
    minutes later for not reaching a login prompt it was never going to reach
    unattended.

    GRUB unlocks an encrypted BIOS disk before it reads `grub.cfg`, so that
    prompt is on the VGA console whatever `GRUB_TERMINAL` says: it is typed
    through the API's `sendkey` rather than waited for.
    """
    graph = installation.disk.graph
    encrypted = bool(graph.of_type(Luks)) or any(
        pool.encrypted for pool in graph.of_type(ZfsPool)
    )
    if not encrypted:
        return ""
    if installation.bootloader.firmware is BootFirmware.BIOS:
        time.sleep(GRUB_PROMPT_SECONDS)
        guest.send_keys([*keys_for(DISK_PASSPHRASE), "ret"])
    for _ in range(UNLOCK_TRIES):
        try:
            said = link.expect(rf"{PASSPHRASE_PROMPT}|login:", timeout=BOOT_PATIENCE)
        except (ConsoleTimeout, ConsoleClosed) as error:
            return f"the encrypted disk asked for nothing and booted nowhere: {error}"[:200]
        if b"login:" in said:
            return ""
        link.send(DISK_PASSPHRASE)
    return "the disk kept asking for a passphrase; it is not the one installed"


def boot_and_check(
    guest: Guest, link: Reconnecting, log: Path, installation: InstallConfig
) -> str:
    """Boot the newly written disk and read the system back.

    Answers an empty string when everything asked for is there, and what is
    wrong otherwise. Booting is not the test: a machine can reach a login
    prompt with the wrong filesystem mounted, no fstab and the wrong locale,
    and every check above this one would still be green.
    """
    guest.stop()
    guest.boot_from_disk()
    guest.start()
    link.reopen()
    refused = _unlock(guest, link, installation)
    if refused:
        return refused
    try:
        link.expect(r"login:", timeout=BOOT_PATIENCE)
    except (ConsoleTimeout, ConsoleClosed) as error:
        return f"the installed system did not reach a login prompt: {error}"[:200]
    link.send("root")
    link.expect(PASSWORD_PROMPT, timeout=120.0)
    link.send(INSTALLED_PASSWORD)
    try:
        link.expect(r"#|\$", timeout=120.0)
    except (ConsoleTimeout, ConsoleClosed) as error:
        return f"root could not log into the installed system: {error}"[:200]

    for name, command, wanted in (*INSIDE, *_asked_for(installation)):
        said = link.expect_output(command, timeout=120.0)
        if wanted and wanted.encode() not in said:
            return f"{name}: the installed system does not say {wanted!r}"
    return ""


def collect(guest: Guest, link: "Reconnecting", log: Path) -> dict[str, bytes]:
    """Read the result archive back, reopening the console if it has gone.

    A `termproxy` session does not survive an install: one that ran 36 minutes
    was dropped in the second after the installer printed `installed 53
    operations`, and the run was recorded as a failure although everything it
    was testing had worked. The guest is still up and the archive is still on
    it, so the answer is another console, not another install.
    """
    last: Exception | None = None
    for attempt in range(COLLECT_TRIES):
        try:
            link.run(f"cp /run/gentoo-install/install.jsonl {RESULT_DIR}/ 2>/dev/null || true")
            link.send(console_command(RESULT_DIR))
            # Waited for, not timed: the marker is what says the archive is
            # whole, and `expect` answers with everything read up to it.
            return read_console(link.expect(CONSOLE_CLOSE, timeout=COLLECT_PATIENCE))
        except (ConsoleClosed, ConsoleTimeout, ResultError) as error:
            last = error
            if attempt + 1 == COLLECT_TRIES:
                break
            link.reopen()
    raise ResultError(f"the results could not be read back: {last}")


#: How long the closing path waits for a worker to notice its guest was
#: stopped. The worker is blocked reading a console, and closing that console
#: is what wakes it, so this covers the read timeout and not an install.
ABANDON_PATIENCE: Final[float] = 120.0


def _abandon(
    inflight: dict[str, Running], running: dict[str, threading.Thread]
) -> None:
    """Stop and remove every guest still running when the schedule ends.

    The workers stay daemon threads, so one wedged on a console cannot hold the
    interpreter open; that makes this the only path that reclaims their guests.
    Without it a scheduler that raised in `free_slots`, `prepare` or the
    bookkeeping left its guests running and holding memory the cluster's own
    machines need, and nothing ever removed them.
    """
    held = list(inflight.items())
    if not held:
        return
    for name, one in held:
        print(f"! ending {name}: the schedule is closing", file=sys.stderr)
        try:
            one.guest.stop()
        except ProxmoxError as error:
            print(f"  {name} was not stopped: {error}", file=sys.stderr)
    # Joined rather than deleted here: each worker removes its own guest in its
    # own `finally`, and stopping the guest is what lets it get there. Deleting
    # from this thread would race the worker still holding the console.
    deadline = time.monotonic() + ABANDON_PATIENCE
    for thread in running.values():
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    # After the join, not before: a worker that was going to remove its own
    # guest has had its chance, and what is left is what nothing else will
    # remove. Reporting it and walking away left guests running on the cluster.
    for name, one in list(inflight.items()):
        try:
            one.guest.destroy()
            print(f"  {name} removed by the closing path", file=sys.stderr)
        except ProxmoxError as error:
            print(f"  {name} outlived the schedule: {error}", file=sys.stderr)


def _abandon_jobs(scheduled: Mapping[str, Job]) -> None:
    inflight = {
        job.name: job.execution
        for job in scheduled.values()
        if job.running and job.execution is not None
    }
    running = {
        job.name: job.thread
        for job in scheduled.values()
        if job.running and job.thread is not None
    }
    _abandon(inflight, running)


def _edit_bios_cmdline(guest: Guest, link: "Reconnecting") -> None:
    """Read the menu when GRUB speaks on the serial port, and type blind if not.

    SeaBIOS hands over to a GRUB that on some media writes only to VGA, which
    is why the blind path exists at all. The official minimal ISO answers
    `starting serial terminal on interface serial0` and can be read, and typing
    at it blind landed on no line: `vm-bios`, `vm-bios-luks` and `ext4-bios`
    all ended at `the kernel never spoke after editing GRUB blind` with that
    line in the log.
    """
    try:
        append_to_cmdline(link, EXTRA_CMDLINE)
        return
    except (ProxmoxError, ConsoleTimeout, ConsoleClosed):
        pass
    # The half-finished edit goes with the reset, so the blind attempt starts
    # from a menu nobody has touched.
    guest.reset()
    link.reopen()
    append_to_cmdline_blind(guest, link, EXTRA_CMDLINE)


def _unanswered(
    running: Mapping[str, threading.Thread], nothing_queued: bool
) -> list[str]:
    """The workers that ended without putting an outcome on the queue.

    A dead thread is not evidence on its own: a worker that answered and then
    exited looks exactly the same, and the schedule reported `vm-xfs` twice,
    once with the error it raised and once as never reporting. An outcome
    already waiting is that answer, so nothing is declared until the queue has
    been drained.
    """
    if not nothing_queued:
        return []
    return [name for name, thread in running.items() if not thread.is_alive()]


def _unanswered_jobs(
    scheduled: Mapping[str, Job], nothing_queued: bool
) -> list[Job]:
    running = {
        job.name: job.thread
        for job in scheduled.values()
        if job.running and job.thread is not None
    }
    names = _unanswered(running, nothing_queued)
    return [scheduled[name] for name in names]


def _sweep(inflight: Mapping[str, Running]) -> None:
    """End every guest whose serial log has stopped growing.

    Stopping the guest is what reaches the worker: it is blocked reading a
    console, and closing that console is the only thing that wakes it. The
    thread then reports `STUCK`, which is not `FAIL`: nothing was read in the
    log, so the next question is what was on the screen.
    """
    for name, held in list(inflight.items()):
        if held.watch.moved():
            continue
        if not held.watch.stuck:
            print(
                f"… {name} quiet for {held.watch.strikes * WATCH_EVERY / 60:.0f}m",
                flush=True,
            )
            continue
        print(f"! {name} stopped writing; ending it", flush=True)
        try:
            held.guest.stop()
        except ProxmoxError as error:
            print(f"{name}: the stuck guest would not stop: {error}", file=sys.stderr)


def _sweep_jobs(scheduled: Mapping[str, Job]) -> None:
    inflight = {
        job.name: job.execution
        for job in scheduled.values()
        if job.running and job.execution is not None
    }
    _sweep(inflight)


def _compiles(config: InstallConfig) -> bool:
    """Whether this configuration spends an hour in `emerge` rather than six
    minutes: a kernel built from source, a desktop, or no binary host at all.
    """
    if config.kernel.source.value.endswith("-bin"):
        return bool(config.packages.desktop) or not config.portage.binhost.official
    return True


def fixtures(names: list[str]) -> list[Job]:
    found: list[Job] = []
    for name in names:
        path = REPOSITORY / "tests" / "fixtures" / f"{name}.toml"
        if not path.is_file():
            raise SystemExit(f"no fixture named {name} at {path}")
        config: InstallConfig = load(path)
        found.append(
            Job(
                name=name,
                fixture=path,
                uefi=config.bootloader.firmware.value != "bios",
                disks=max(1, len(config.disk.graph.of_type(Existing))),
                heavy=_compiles(config),
            )
        )
    return found


def _leave_on_a_signal() -> None:
    """Turn SIGTERM into the exception SIGINT already raises.

    Python's default handler for SIGTERM ends the process without unwinding,
    so no `finally` runs: `kill` on the scheduler left eight guests on the
    cluster with the closing path that removes them never reached.
    """

    def raised(number: int, frame: object) -> None:
        raise KeyboardInterrupt(f"signal {number}")

    signal.signal(signal.SIGTERM, raised)
    signal.signal(signal.SIGHUP, raised)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", nargs="+", help="fixture names, without .toml")
    def nonnegative(value: str) -> int:
        limit = int(value)
        if limit < 0:
            raise argparse.ArgumentTypeError("--limit cannot be negative")
        return limit

    parser.add_argument("--limit", type=nonnegative, default=0, help="how many guests at once")
    parser.add_argument("--workdir", type=Path, default=WORKROOT)
    parser.add_argument(
        "--region",
        choices=[one.value for one in MirrorRegion],
        default=MirrorRegion.GLOBAL.value,
        help="which mirror region every fixture is rewritten to use",
    )
    parser.add_argument(
        "--sync",
        choices=[one.value for one in Sync],
        default=Sync.RSYNC.value,
        help="how every fixture syncs the tree; git needs github, which this network lacks",
    )
    args = parser.parse_args(argv)

    _leave_on_a_signal()
    try:
        workdir = confined(args.workdir)
    except WorkdirError as error:
        print(error, file=sys.stderr)
        return 1

    repeated = sorted({one for one in args.fixtures if args.fixtures.count(one) > 1})
    if repeated:
        # Named here as well so the operator reads it before the cluster is
        # asked anything; `run` refuses it too, for every other caller.
        print(f"named more than once: {repeated}", file=sys.stderr)
        return 1

    jobs = fixtures(args.fixtures)
    outcomes = run(
        jobs,
        workdir,
        args.limit,
        int(time.time()),
        MirrorRegion(args.region),
        Sync(args.sync),
    )
    passed = [
        one
        for one in outcomes
        if one.verdict is Verdict.OK and trusted_revision(one.revision)
    ]
    print(f"\n{len(passed)}/{len(jobs)} passed")
    for one in outcomes:
        if one.verdict is not Verdict.OK or not trusted_revision(one.revision):
            print(f"  {one.verdict.value} {one.name}: {one.detail} ({one.log})")
    # Against what was asked for, not against what came back: a worker that
    # died without answering left its job with no outcome at all, and a run
    # that collected fewer results than it dispatched still exited 0.
    missing = sorted({one.name for one in jobs} - {one.name for one in outcomes})
    for name in missing:
        print(f"  no result {name}", file=sys.stderr)
    return 0 if len(passed) == len(jobs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
