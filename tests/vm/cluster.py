# SPDX-License-Identifier: GPL-2.0-or-later
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
import fcntl
import hashlib
import ipaddress
import itertools
import json
import os
import queue
import shutil
import signal
import re
import socket
import subprocess
import sys
import threading
import time
import uuid

from gentoo_install.plan.netboot import CJK_RELEASES
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePath
from collections import Counter
from collections.abc import Callable, Mapping
from types import MappingProxyType
from contextlib import contextmanager
from typing import Final, Iterator, Protocol, TypeVar, cast, Sequence

from gentoo_install.model.config import Firmware as BootFirmware
from gentoo_install.model.config import (
    DiskMode,
    GentooZhMirror,
    InstallConfig,
)
from gentoo_install.model.device import (
    Existing,
    Luks,
    ZfsPool,
)
from gentoo_install.model.config import MirrorRegion, Sync
from gentoo_install.model import mirrors
from gentoo_install.exec.config import load
from gentoo_install.model.serialise import to_toml
from gentoo_install.plan.portage import BINARY_PACKAGES, KEY_SERVER

from .console import (
    DISK_PASSPHRASE,
    PASSPHRASE_PROMPT,
    PASSWORD_PROMPT,
    ConsoleClosed,
    ConsoleIdle,
    ConsoleTimeout,
    SerialConsole,
    command_begin,
    command_done,
    marked_command,
)
from .driver import (
    FIND_DRIVER,
    NAME_PREFIX as DRIVER_PREFIX,
    build as build_driver,
    digest as driver_digest,
    remote_name,
)
from .monitor import keys_for
from .proxmox import (
    Api,
    CreateConflict,
    ForeignGuest,
    Guest,
    GuestSpec,
    GrubNotReadable,
    Node,
    ProxmoxError,
    ProxmoxTransientError,
    VMID_FIRST,
    VMID_LAST,
    Line,
    append_to_cmdline,
    open_a_serial_shell_blind,
)
from .results import (
    CONSOLE_CLOSE,
    LOG_TAIL,
    RESULT_BUFFER_BYTES,
    ResultError,
    console_command,
    read_console,
)
from .workdir import WorkdirError, confined
from .convert import (
    ETC_MARKER,
    ETC_MARKER_CHECK,
    ETC_MARKER_PATH,
    HOME_MARKER,
    HOME_MARKER_CHECK,
    HOME_MARKER_PATH,
    after_the_boot,
    before_the_reboot,
)
from .installed import InstalledCheck, checks, stage_passphrase_commands
from .run import SEEDED, remote_config, remote_unlock, ssh_keypair

REPOSITORY: Final[Path] = Path(__file__).resolve().parents[2]
WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/cluster"
ADDRESS_POOL_ROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/address-pool"

#: Outside any one round's work directory: the ISO stays on the node between
#: rounds, so the record of what was uploaded has to outlive the round that
#: uploaded it. A fresh work directory otherwise met its own upload as
#: `already exists on infra-node5 without its signed SHA-512 record`.
MEDIUM_TRUST: Final[Path] = WORKROOT / "medium-trust"

#: The workstation's own copy of a medium the cluster cannot download, kept
#: outside a round for the same reason as the trust record above.
MEDIA_CACHE: Final[Path] = WORKROOT / "media"

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

#: Cores left to the node itself. `pveproxy` answers 595 through the
#: cluster proxy when a node is saturated, and a node carrying four
#: two-core guests on four cores has nothing left to answer with.
NODE_HEADROOM_CORES: Final[float] = 1.0

#: How long a driver CD on a node is left alone. Longer than any run,
#: so a campaign never removes one another campaign is booting from.
DRIVER_KEPT_SECONDS: Final[float] = 24 * 60 * 60.0

#: How often the watchdog looks, and how many quiet looks end a guest. Ten
#: minutes because a stage3 extract and a kernel build both write progress more
#: often than that, and half an hour of silence is not a slow mirror.
WATCH_EVERY: Final[float] = 600.0
WATCH_STRIKES: Final[int] = 3
#: Samples the hypervisor may refuse before its silence is the guest's.
BLIND_SAMPLES: Final[int] = WATCH_STRIKES * 2

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

#: How much of an exception reaches the schedule's one-line outcome. Three
#: call sites each wrote `300`, and `VERDICT_BYTES` is twelve times that, so a
#: reader of one number could not tell what a verdict had room for.
OUTCOME_BYTES: Final[int] = 300

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

#: How long to wait instead when the room is held by this harness's own
#: guests. That room is provably coming back — every one of them ends — and a
#: single-fixture round dispatched beside a full campaign was failed at 121
#: seconds with ten of ours still installing. Bounded rather than endless: a
#: guest nothing ever collects would otherwise hold a round for ever.
CAPACITY_PATIENCE_SHARED: Final[float] = 60 * 60.0


def _capacity_patience(api: Api) -> float:
    """How long a round waits for room, by who is holding it."""
    try:
        return CAPACITY_PATIENCE_SHARED if api.ours() else CAPACITY_PATIENCE
    except ProxmoxError:
        # The cluster could not say. The shorter bound is the safe answer: it
        # produces a verdict rather than waiting on an unknown.
        return CAPACITY_PATIENCE

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
    CONVERT = "convert"


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
    #: Whether the node's own console proxy went away rather than the guest.
    #: The node gets no more guests this campaign.
    proxy_dropped: bool = False


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
    remote_unlock: bool = False
    #: The in-place fixture to run once the installed system is up. Set for a
    #: conversion job, whose `fixture` is the ordinary install it converts.
    convert_to: Path | None = None
    #: Where `rewrite_fixtures` put the copy the guest is handed. The checks
    #: come from that copy and not from `fixture`: the rewrite moves the
    #: mirror, the sync and, for a fixture that pins one, the machine's own
    #: address, so a check derived from the original asserts what the guest
    #: was never asked to install.
    installed_config: Path | None = None
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
    def reservation_bytes(self) -> int:
        return self.memory_mib * 1024**2

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
        if execution.reservation_bytes != self.reservation_bytes:
            raise ProxmoxError(f"{self.name} execution has the wrong memory reservation")
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

#: The share of a core a working guest holds. A hung one reads 0.00 and a
#: guest at a prompt is near it; `vm-binhost-fallback` was compiling grub at
#: this and above while its counters moved 7781 bytes in twenty minutes.
BUSY_CPU: Final[float] = 0.10


#: What a guest still moving bytes buys back after its console dropped. The
#: transport outage is not the guest's silence: `vm-gnome` was compiling git
#: when one `Connection reset by peer` ended 167 minutes of work.
RECONNECT_GRANT: Final[float] = 900.0
#: At most this many, so a console that never comes back still ends the run.
#: Sixteen rather than four: every grant is given only to a guest whose own
#: counters or CPU say it is working, and `vm-gnome` spent all four of them
#: still compiling and was ERRORed at 156.5 minutes with the console mid-line.
RECONNECT_GRANTS: Final[int] = 16


@dataclass
class Watchdog:
    """Whether a guest is still doing anything.

    The console alone is not enough. An install spends minutes downloading a
    stage3 and says nothing at all while it does, so a watchdog reading only
    the serial log ends the guests that are working hardest. The guest's own
    counters are what separate a slow transfer from a dead machine.
    """

    log: Path
    counters: Callable[[], tuple[int, float] | None]
    #: Where the guest is. A verdict that says only `counters were flat` is a
    #: guess about a cluster whose other tenants this campaign does not
    #: control: `static-ip` went to cpu 0.00 mid-compile and the node it was
    #: on had to be read back out of the dispatch line to say anything at all.
    where: str = ""
    #: What the node itself was doing, read once when the guest is about to be
    #: called stuck. Two guests were ended mid-compile at `cpu 0.00` on nodes
    #: this campaign does not own, and the verdict could not tell a guest that
    #: stopped from a node with no time to give it.
    load: Callable[[], float | None] | None = None
    #: What qemu says the guest is doing, read at the same moment. A guest
    #: paused by a storage error reads exactly like one that stopped on its
    #: own: flat counters and `cpu 0.00`.
    state: Callable[[], str] | None = None
    strikes: int = 0
    _seen: int = field(default=0, init=False)
    _moved: int = field(default=0, init=False)
    _counter_before: int = field(default=0, init=False)
    _counter_after: int = field(default=0, init=False)
    _cpu: float = field(default=0.0, init=False)
    #: Consecutive samples the hypervisor would not answer.
    _blind: int = field(default=0, init=False)
    #: Whether the last sample saw nothing new in the log.
    _silent: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def moved(self) -> bool:
        with self._lock:
            size = self.log.stat().st_size if self.log.exists() else 0
            talking = size > self._seen
            self._silent = not talking
            self._seen = max(self._seen, size)
            return self._observe(talking)

    @property
    def silent(self) -> bool:
        """Whether the last sample saw nothing new in the log.

        Separate from `moved`, which is also true for a guest whose console has
        gone while its disk is still busy: that is the one shape where the log
        has to be read for the node's own words.
        """
        return self._silent

    def _observe(self, talking: bool) -> bool:
        traffic = self.counters()
        if traffic is None:
            # One unanswered request is not evidence about the guest. A run of
            # them is: `vm-desktop` sat on a node that had stopped answering
            # for sixty-eight minutes with the console silent and the counters
            # unreadable, and the watchdog called that alive every time.
            if talking:
                self.strikes = 0
                self._blind = 0
                return True
            self._blind += 1
            return self._blind < BLIND_SAMPLES
        self._blind = 0
        moved, self._cpu = traffic
        self._counter_before = self._moved
        self._counter_after = moved
        # Either signal is enough. A compile whose build directory is in RAM
        # writes nothing and answers no network, and `vm-binhost-fallback` was
        # ended for that at 48.8 minutes with grub half built.
        working = moved - self._moved >= QUIET_BYTES or self._cpu >= BUSY_CPU
        self._moved = max(self._moved, moved)
        if talking or working:
            self.strikes = 0
            return True
        self.strikes += 1
        return False

    def idle_reason(self) -> str | None:
        with self._lock:
            if self._observe(talking=False):
                return None
            if self._blind:
                minutes = self._blind * WATCH_EVERY / 60
                return (
                    f"the hypervisor did not answer for {minutes:.0f}m "
                    "and the console said nothing"
                )
            node = f" on {self.where}" if self.where else ""
            busy = self.load() if self.load is not None else None
            said = "" if busy is None else f", the node itself at {busy * 100:.0f}%"
            qemu = self.state() if self.state is not None else ""
            if qemu and qemu != "running":
                said += f", and qemu calls the guest {qemu}"
            return (
                f"counters were flat{node} "
                f"({self._counter_before} -> {self._counter_after} bytes, "
                f"cpu {self._cpu:.2f}){said}"
            )

    @property
    def stuck(self) -> bool:
        return self.strikes >= WATCH_STRIKES or self._blind >= BLIND_SAMPLES


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


def current_cjk() -> tuple[str, tuple[str, ...], str]:
    """The current CJK live CD, its download and its published SHA-512.

    Read from the release index rather than pinned: the asset name carries
    the build timestamp, and the one this was first written against was gone
    from the index within the week.
    """
    with urllib.request.urlopen(CJK_RELEASES, timeout=30.0) as answer:
        release = json.loads(answer.read().decode("utf-8", "replace"))
    assets = {one["name"]: one["browser_download_url"] for one in release["assets"]}
    image = next((name for name in assets if name.endswith(".iso")), "")
    if not image:
        raise RuntimeError(f"no iso among {sorted(assets)} in {release.get('tag_name')}")
    digest = ""
    for name, url in assets.items():
        if name == f"{image}.sha256" or name.endswith("DIGESTS"):
            with urllib.request.urlopen(url, timeout=30.0) as answer:
                said = answer.read().decode("utf-8", "replace")
            for line in said.splitlines():
                if image in line or len(line.split()) == 2:
                    digest = line.split()[0]
                    break
        if digest:
            break
    return image, (assets[image],), digest



def cached_medium(medium: str, urls: tuple[str, ...], sha512: str) -> Path:
    """The workstation's own copy, downloaded once and kept.

    The cluster reaches the Chinese Gentoo mirrors and nothing else: GitHub,
    where the CJK live CD is published, answers a node with a closed
    connection, so `fetch_iso` waits an hour for a task that never starts.
    The workstation does reach it, and `send_iso` puts the file on the node
    over the cluster's own certificate.
    """
    kept = MEDIA_CACHE / medium
    if kept.is_file() and _sha512(kept) == sha512:
        return kept
    kept.parent.mkdir(parents=True, exist_ok=True)
    partial = kept.with_suffix(kept.suffix + ".part")
    last = ""
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=60.0) as answer, partial.open("wb") as out:
                shutil.copyfileobj(answer, out)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = str(error)
            continue
        if _sha512(partial) != sha512:
            last = f"{url} served {medium} with the wrong checksum"
            continue
        partial.replace(kept)
        return kept
    partial.unlink(missing_ok=True)
    raise ProxmoxError(f"no source served {medium} to the workstation: {last}")


def _sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


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


def _place(
    api: Api,
    node: str,
    medium: str,
    urls: tuple[str, ...],
    sha512: str,
    local: Path | None,
) -> None:
    """Get one medium onto a node, by whichever route can reach it.

    A local copy is sent rather than fetched: the node reaches the Chinese
    Gentoo mirrors and nothing else, so asking it for a GitHub release starts
    a download task that never progresses and `fetch_iso` waits an hour.
    """
    if local is not None:
        api.send_iso(node, local, medium, sha512)
        return
    last = ""
    for url in urls:
        try:
            api.fetch_iso(node, url, medium, sha512)
            return
        except ProxmoxError as error:
            last = str(error)
    raise ProxmoxError(f"no mirror served {medium} to {node}: {last}")


def prepare(
    api: Api,
    node: str,
    medium: str,
    urls: tuple[str, ...],
    sha512: str,
    trust: Path,
    driver_path: Path,
    driver: str,
    local: Path | None = None,
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
            reason = api.remove_iso(node, medium)
            if reason:
                raise ProxmoxError(f"{medium} could not be replaced on {node}: {reason}")
            _place(api, node, medium, urls, sha512, local)
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(sha512)
    else:
        _place(api, node, medium, urls, sha512, local)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(sha512)
    for stale in api.stale_drivers(node, driver, DRIVER_KEPT_SECONDS):
        reason = api.remove_iso(node, stale)
        if reason:
            print(f"{stale} stayed on {node}: {reason}", file=sys.stderr)
    driver_sha256 = driver_digest(driver_path)
    driver_stamp = trust / "remote" / node / f"{driver}.sha256"
    if driver in api.isos(node):
        try:
            recorded_driver = driver_stamp.read_text().strip()
        except OSError:
            recorded_driver = ""
        if recorded_driver != driver_sha256:
            reason = api.remove_iso(node, driver)
            if reason:
                raise ProxmoxError(f"{driver} could not be replaced on {node}: {reason}")
            api.upload_iso(node, driver_path, driver)
            driver_stamp.parent.mkdir(parents=True, exist_ok=True)
            driver_stamp.write_text(driver_sha256)
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
#: A resolver of ours on the guest segment, asked before anything else. The
#: segment's own answers arrive intermittently — a guest resolved a mirror at
#: one step and failed on the next five seconds later — and `1.1.1.1` and
#: `8.8.8.8` are outside China, where this cluster is. `gi-resolver` is an
#: Alpine container at this address running dnsmasq over Chinese forwarders.
GUEST_RESOLVER: Final[str] = "10.31.0.199"

GUEST_RESOLVERS: Final[tuple[str, ...]] = (GUEST_RESOLVER, GUEST_GATEWAY, "223.5.5.5")


#: What the keyserver connect is given. Longer than a lookup because a TLS
#: handshake is not a name: measured 1.3s from this workstation.
KEYSERVER_PATIENCE: Final[int] = 5

#: The host `WebrsyncRepository` hands gemato, derived from the installer's own
#: constant rather than repeated: the probe asks about a name, not a URL.
KEY_SERVER_HOST: Final[str] = urllib.parse.urlparse(KEY_SERVER).hostname or KEY_SERVER

#: `resolv.conf(5)`: the defaults are `timeout:5 attempts:2`, so a first
#: nameserver that has stopped answering costs ten seconds on every lookup
#: before the second one is asked. Five guests of run61 had
#: `REACH 10.31.0.199=down` with the gateway and `223.5.5.5` both answering,
#: and their installs never started.
RESOLVER_OPTIONS: Final[str] = "timeout:1 attempts:2"

#: What each lookup in the probe is given. A resolver that has stopped
#: answering makes `getent` wait for its own timeout, and ten of them cost more
#: than the probe's whole budget: five guests of run61 were ended at three
#: minutes with `LOOKUPS_ANY ok fail` as the last line and no install started.
LOOKUP_PATIENCE: Final[int] = 3

#: Asked once the network is up, because every round so far has failed at the
#: stage3 while `/etc/resolv.conf` named a resolver nobody had proved the guest
#: could reach. Read-only: it rewrites nothing, so a run cannot be changed by
#: being measured.
REACHABILITY_PROBE: Final[str] = (
    "printf 'REACH '; for one in " + " ".join(GUEST_RESOLVERS) + "; do "
    'ping -c1 -W2 "$one" >/dev/null 2>&1 '
    "&& printf '%s=up ' \"$one\" || printf '%s=down ' \"$one\"; done; "
    "printf '\\nLOOKUPS_V4 '; for i in 1 2 3 4 5; do "
    f"timeout {LOOKUP_PATIENCE} getent ahostsv4 mirrors.ustc.edu.cn >/dev/null 2>&1 "
    "&& printf 'ok ' || printf 'fail '; done; "
    # `ahosts` and not only `ahostsv4`: the installer asks `AF_UNSPEC`, which
    # queries the AAAA as well, and `options no-aaaa` needs glibc 2.36. A
    # resolver that answers the A and drops the AAAA fails the whole call.
    "printf '\\nLOOKUPS_ANY '; for i in 1 2 3 4 5; do "
    f"timeout {LOOKUP_PATIENCE} getent ahosts mirrors.ustc.edu.cn >/dev/null 2>&1 "
    "&& printf 'ok ' || printf 'fail '; done; "
    # The keyserver by name and by family, and a connect rather than a ping:
    # nine guests of run62 stopped at `gpg: keyserver refresh failed: Try again
    # later` with every mirror reachable, and that message says nothing about
    # which of the name, the family or the route was missing. From this
    # workstation the same host answers `HTTP 200` in 1.3s over IPv4.
    f"printf '\\nKEYSERVER_V4 '; timeout {LOOKUP_PATIENCE} getent ahostsv4 "
    f"{KEY_SERVER_HOST} 2>/dev/null | head -1 || printf 'unresolved'; "
    f"printf '\\nKEYSERVER_ANY '; timeout {LOOKUP_PATIENCE} getent ahosts "
    f"{KEY_SERVER_HOST} 2>/dev/null | head -1 || printf 'unresolved'; "
    "printf '\\nKEYSERVER_TCP '; "
    f"timeout {KEYSERVER_PATIENCE} bash -c '</dev/tcp/{KEY_SERVER_HOST}/443' 2>/dev/null "
    "&& printf 'open' || printf 'refused'; "
    "printf '\\nLIBC '; getconf GNU_LIBC_VERSION; "
    # The state itself, not only whether it worked: sixty-one lookups answered
    # `ENETUNREACH` from a guest that had passed this probe a minute earlier,
    # and only the addresses and routes at each moment say what went.
    "printf 'ADDRS '; ip -4 -brief address show | tr '\\n' ';'; "
    "printf '\\nROUTES '; ip -4 route show | tr '\\n' ';'; "
    # The links regardless of address, and the kernel's own account: round 31
    # measured `ens18` with an address, then the installer four seconds later
    # with no routes, then no `ens18` at all. Only `dmesg` says what removed it.
    "printf '\\nLINKS '; ip -brief link show | tr '\\n' ';'; "
    "printf '\\nDMESG '; dmesg | tail -6 | tr '\\n' ';'; "
    "printf '\\nKEEPER '; wc -l < /tmp/gentoo-install-address-keeper.log "
    "2>/dev/null || printf 'none'; "
    "printf '\\n'"
)



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


#: How much of one command line to send at a time. One line carrying every
#: address keeper is four hundred characters, which is more than a tty edits
#: in canonical mode without dropping the rest: every guest of round 33 died
#: on `MARK_6_DONE` with the loop on one line.
CONSOLE_LINE_BYTES: Final[int] = 200

def configure_statically(address: str) -> str:
    """Give the interface the address reserved by the scheduler.

    This segment carries other people's machines: a probe found 10.31.0.106
    through .115 answering with locally administered MAC addresses, none of
    them ours. Walking forward from the address this guest was assigned keeps
    the collision impossible without depending on the DHCP server, which runs
    on a Raspberry Pi here and answers intermittently.
    """
    network, last = address.rsplit(".", 1)
    gateway = GUEST_GATEWAY
    prefix = GUEST_PREFIX
    # `\\n` for printf, not a real newline: the whole thing is one line sent to
    # a serial console, and a literal break there is two commands.
    # `no-aaaa` as well as the servers: `/etc/hosts` carries IPv4 addresses
    # only, and an `AF_UNSPEC` lookup still asks DNS for the AAAA. A resolver
    # that does not answer turns that into `EAI_AGAIN` for the whole call, so
    # a name sitting in the file fails anyway — which is what ended sixteen
    # rounds at the stage3 fetch.
    return (
        # SIGKILL, not SIGTERM: dhcpcd answers a term by releasing the lease
        # and deconfiguring the interface, and it does that after the command
        # returns. Round 29 measured a full routing table on the console and
        # `no routes` from the installer four seconds later.
        "pkill -KILL -x dhcpcd 2>/dev/null; pkill -KILL -x dhclient 2>/dev/null; "
        "pkill -KILL -x udhcpc 2>/dev/null; "
        # Unconditional, not only when the default route is missing: the guard
        # meant a guest that came up on DHCP never received an address of its
        # own, so it had nothing left once the lease went.
        'for one in /sys/class/net/e*; do dev=$(basename "$one"); ip link set "$dev" up; '
        f'ip -4 addr add {network}.{last}/{prefix} dev "$dev" 2>/dev/null || true; '
        f'ip -4 route replace default via {gateway} dev "$dev" 2>/dev/null || true; '
        "done; "
        # The client is dead, so nothing will put these back if they go: report
        # what is there rather than a bare success, and the log carries it.
        "ip -4 route show default | grep -q . || true"
    )


def use_our_resolvers() -> str:
    """Point the guest at the resolver the cluster runs, whatever DHCP said.

    The segment's DHCP server hands out a resolver on a Raspberry Pi that
    answers intermittently. A guest whose first network probe succeeded kept
    that resolver for the whole install, which is why one guest in a round
    fetched its stage3 and the rest failed every mirror.
    """
    # `\\n` for printf, not a real newline: the whole thing is one line sent to
    # a serial console, and a literal break there is two commands.
    # `no-aaaa` as well as the servers: an `AF_UNSPEC` lookup asks for the AAAA
    # too, and a resolver that does not answer turns that into `EAI_AGAIN` for
    # the whole call even when the A record arrived.
    resolvers = f"options no-aaaa {RESOLVER_OPTIONS}\\n" + "".join(
        f"nameserver {one}\\n" for one in GUEST_RESOLVERS
    )
    return f"printf '{resolvers}' > /etc/resolv.conf"


#: Where the keeper records each time it had to put the address back. The
#: count is the evidence: a keeper that never fires means something else took
#: the network, and one that fires every five seconds names the interval.
KEEPER_LOG: Final[str] = "/tmp/gentoo-install-address-keeper.log"

#: The keeper itself, written a line at a time because a console line over
#: `CONSOLE_LINE_BYTES` comes back mangled.
KEEPER_SCRIPT: Final[str] = "/tmp/gentoo-install-address-keeper.sh"


def keep_the_address(address: str) -> list[str]:
    """Put the address back whenever something takes it away.

    Round 32 measured `ens18` up with `10.31.0.152/24` immediately before the
    installer and the same interface up with no address after it, with nothing
    in the kernel log and no network command in the installer's own run list.
    Rather than name the service that flushes it, this reasserts the
    configuration and counts how often it had to.

    A script rather than one command: the whole loop is four hundred bytes and
    a console line over `CONSOLE_LINE_BYTES` is mangled, which cost round 33.
    """
    lines = (
        "while :; do",
        'for one in /sys/class/net/e*; do dev=$(basename "$one")',
        f'ip -4 addr show dev "$dev" | grep -q {address}/{GUEST_PREFIX} && continue',
        'ip link set "$dev" up',
        f'ip -4 addr add {address}/{GUEST_PREFIX} dev "$dev" 2>/dev/null',
        f'ip -4 route replace default via {GUEST_GATEWAY} dev "$dev" 2>/dev/null',
        f"echo readded >> {KEEPER_LOG}",
        "done",
        "sleep 5",
        "done",
    )
    written = [f"rm -f {KEEPER_SCRIPT} {KEEPER_LOG}"]
    written += [f"printf '%s\\n' '{line}' >> {KEEPER_SCRIPT}" for line in lines]
    # Braced: the console wrapper appends `; printf MARK...`, and a bare `&`
    # followed by `;` is a syntax error, which ended every guest of round 34.
    written.append(f"{{ nohup sh {KEEPER_SCRIPT} >/dev/null 2>&1 & }}")
    return written


def _address_is_taken(address: str) -> bool:
    """Check occupancy before the scheduler reserves an address."""
    result = subprocess.run(
        ["arping", "-D", "-c", "2", "-w", "3", address],
        capture_output=True,
        check=False,
    )
    return result.returncode != 0


#: What the official minimal medium asks before it hands over a shell, and
#: what answers it. Nothing answered: two guests on one round sat at `Load
#: keymap (Enter for default):` while the run spent its patience waiting for a
#: prompt that was one keystroke away.
KEYMAP_QUESTION: Final[str] = r"Load keymap|keymap \(Enter for default\)"

#: How long the medium is given to reach a shell, question or no question.
PROMPT_PATIENCE: Final[float] = 900.0


#: What a root prompt looks like on a medium this drives. The first two are
#: the hostname the medium sets; the third is the prompt a shell started by
#: the medium's own auto-login gives, which carries no hostname because
#: nothing has set one in that environment yet. `vm-bios` reached exactly
#: that and waited fifteen minutes for a prompt it was already looking at:
#: the console's last output was ` ~ # `.
ROOT_PROMPT: Final[str] = r"livecd .*#|localhost .*#|~ #"


def reach_prompt(link: Reconnecting, patience: float = PROMPT_PATIENCE) -> None:
    """Wait for a root prompt, answering the medium's questions on the way."""
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        said = link.expect(
            rf"{ROOT_PROMPT}|{KEYMAP_QUESTION}",
            timeout=max(30.0, deadline - time.monotonic()),
        )
        if not re.search(KEYMAP_QUESTION.encode(), said):
            return  # The prompt matched, which is what this waits for.
        # The default is what every fixture wants, and the question waits for
        # a key rather than answering itself.
        link.send("")
    raise ConsoleTimeout(f"the medium did not reach a shell in {patience:.0f}s")


def wait_for_network(
    link: Reconnecting, vmid: int = 0, address: str = ""
) -> None:
    """Configure the guest's interface, then wait until it can reach a mirror.

    The medium boots with `nodhcp` and leaves the link unconfigured, so
    something has to configure it, which is what an operator does before
    installing. The probe goes first in case the medium's own manager got
    there.

    `vmid` gives the guest its own address. Zero falls back to asking the DHCP
    server, which is what a run outside the cluster does.
    """
    deadline = time.monotonic() + NETWORK_PATIENCE
    configure = (
        configure_statically(address or static_address(vmid))
        if vmid
        else ASK_FOR_IPV4
    )
    # Before the probe, not with the configuration that only runs when the
    # probe fails: the segment's resolver answers the probe and stops answering
    # twenty steps later, so a guest that reached a mirror once was left
    # depending on it for the rest of the install.
    # Before the probe: a probe that succeeds returns without ever running the
    # fallback, so a resolver written only in the fallback reaches the guests
    # that needed it least.
    link.run(use_our_resolvers(), timeout=120.0)
    asked = False
    while time.monotonic() < deadline:
        said = link.expect_output(NETWORK_PROBE, timeout=180.0)
        if NETWORK_UP.encode() in said:
            # After the probe, not before it: an interface raised from outside
            # the medium's own manager is one it then leaves alone. After it
            # succeeds, though, the guest still has to hold the address for the
            # rest of the install, and a lapsed DHCP lease took it away
            # mid-fetch — sixty-one lookups answered `ENETUNREACH`.
            link.run(configure, timeout=120.0)
            # Again, once the DHCP client is dead: it is left running through
            # the probe, and a lease taken in that window rewrote the file.
            # `vm-zfs-mirror` reached the installer with two addresses, two
            # default routes and `nameserver 10.31.0.252`, and every mirror
            # failed to resolve.
            link.run(use_our_resolvers(), timeout=120.0)
            if vmid:
                for command in keep_the_address(address or static_address(vmid)):
                    link.run(command, timeout=120.0, repeatable=False)
            _note_the_probe(link, "network-up")
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
    # Read, not only logged: `vm-gnome` gave up with `no network` while it
    # held 10.31.0.154, a default route and our three resolvers, and every
    # mirror answered `Could not contact DNS servers`. Which of the three was
    # missing is what sends a reader to the segment or to the medium.
    held = {
        "address": link.expect_output("ip -oneline -4 address show scope global", 60.0),
        "route": link.expect_output("ip -4 route show default", 60.0),
        "resolver": link.expect_output("cat /etc/resolv.conf", 60.0),
    }
    missing = [name for name, said in held.items() if not said.strip()]
    had = ", ".join(name for name in held if name not in missing) or "nothing"
    raise ConsoleTimeout(
        f"the guest reached no mirror in {NETWORK_PATIENCE:.0f}s; it had {had}"
        + (f" and no {', '.join(missing)}" if missing else "")
    )


def stage_passphrases(link: Reconnecting, installation: InstallConfig) -> None:
    """Put the passphrases where the layout says they are.

    An operator does this by hand before an unattended install. Without it an
    encrypted layout stops at `pool: /run/gentoo-install-keys/pool cannot be
    read`, which is the installer refusing correctly and the harness not having
    done its part.
    """
    for command in stage_passphrase_commands(installation):
        link.run(command)


def _fixture_dir(workdir: Path) -> Path:
    """Where this schedule writes the configurations its guests are given.

    Its own directory, because every schedule shares `workdir` and the verdict
    re-reads the file after the guest is installed: `static-ip` was installed
    with `10.31.0.172/24`, a later schedule rewrote the same file with its own
    reservation, and the check then failed the machine for not holding
    `10.31.0.186/24` — an address nothing had ever asked it for.
    """
    return workdir / f"fixtures-{uuid.uuid4().hex[:12]}"


def rewrite_fixtures(
    jobs: list[Job],
    into: Path,
    region: MirrorRegion,
    sync: Sync,
    public_key: str = "",
    site: str = "",
    unlock_addresses: Mapping[str, str] | None = None,
    distfiles: str = "",
) -> Path:
    """Write each fixture out again with its mirror region, site and sync.

    Through the parser and the writer, not a text substitution: a fixture that
    cannot survive the round trip is a defect worth failing on here rather than
    an hour into an install.

    `site` names one mirror rather than letting the region pick its first, and
    it moves everything that follows a mirror: the ebuild repository, the
    distfiles list, the official binary packages and, when that key also names
    a gentoo-zh site, the overlay. This cluster is in China and paid for by
    cheap power there, so a run that reaches for `distfiles.gentoo.org` or
    `github.com` is a run measuring the way out of the country rather than the
    installer.
    """
    into.mkdir(parents=True, exist_ok=True)
    # A conversion job carries two: the ordinary install it runs first and the
    # in-place fixture it runs against the result. Writing only `fixture` left
    # the CD without the second one, and the guest answered `no such file`.
    wanted = [
        (job, path)
        for job in jobs
        for path in (job.fixture, job.convert_to)
        if path is not None
    ]
    for job, source in wanted:
        config = load(source)
        # The overlay moves with the region. A `cn` run that still cloned
        # gentoo-zh from github stopped the install after two hundred seconds
        # of `Could not connect to server`, while every other fetch in the same
        # guest came from a Chinese mirror and worked.
        chosen = (
            GentooZhMirror(site)
            if site in {one.value for one in GentooZhMirror}
            else mirrors.gentoozh_for(region)
        )
        where = mirrors.gentoozh(chosen).git
        moved = replace(
            remote_config(config, public_key) if public_key else config,
            portage=replace(
                config.portage,
                sync=sync,
                mirrors=replace(
                    config.portage.mirrors,
                    region=region,
                    site=config.portage.mirrors.site if job.name in KEEPS_ITS_SITE else site,
                    gentoo_zh=chosen,
                    # A replaced list, when one is given: a cache on the
                    # guests' own segment is an address with no name to
                    # resolve, which is the whole class of failure the first
                    # thirty rounds died of.
                    distfiles=(distfiles,) if distfiles else config.portage.mirrors.distfiles,
                ),
                overlays=tuple(
                    replace(overlay, sync_uri=where)
                    if overlay.name == "gentoo-zh"
                    else overlay
                    for overlay in config.portage.overlays
                ),
            ),
        )
        # The address the initramfs will answer on, which is the one the
        # scheduler reserved for this guest: a fixture that pins a
        # documentation address is one the harness can never reach.
        given = (unlock_addresses or {}).get(job.name, "")
        if given and moved.system.addresses:
            # The installed system's own address, for the same reason: a
            # fixture pinning one the scheduler did not reserve produces a
            # machine on somebody else's address, and its own checks then read
            # the address it was told to have rather than the one it has.
            moved = replace(
                moved,
                system=replace(
                    moved.system,
                    addresses=(f"{given}/{GUEST_PREFIX}",),
                    gateways=(GUEST_GATEWAY,),
                    dns=GUEST_RESOLVERS,
                ),
            )
        if given and moved.kernel.remote_unlock.enabled:
            moved = replace(
                moved,
                kernel=replace(
                    moved.kernel,
                    remote_unlock=replace(
                        moved.kernel.remote_unlock,
                        address=f"{given}/{GUEST_PREFIX}",
                        # The gateway with it: the fixtures carry a TEST-NET-1
                        # one beside their TEST-NET-1 address, and rewriting
                        # only the address leaves the initramfs with a default
                        # route through a host that is not on its subnet, so
                        # nothing off it answers.
                        gateway=GUEST_GATEWAY,
                        # Cleared for the same reason the address is rewritten:
                        # a cluster guest's only NIC is `ens18` under
                        # predictable naming, and `vm-unlock` pinned `eth0`.
                        # dracut's `ip=` names the device in its sixth field,
                        # so a name no device carries configures nothing and
                        # `rd.neednet=1` waits for a link that never arrives.
                        interface="",
                    ),
                ),
            )
        (into / source.name).write_text(to_toml(moved))
    return into


def _requests_remote_unlock(path: Path) -> bool:
    """Read the typed configuration so schema changes fail at the boundary."""
    return load(path).kernel.remote_unlock.enabled


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
        try:
            guest.destroy()
        except ForeignGuest:
            # A second campaign reused that VMID after this lease's process
            # died, so the lease is stale and the guest belongs to that run.
            pass
        path.unlink()


def _first_selector(installation: InstallConfig) -> str:
    """The disk an editing fixture starts from, named as the fixture names it.

    From the configuration rather than from `/dev/vda`: the fixture chooses a
    `by-id` path so the guest renumbering its disks cannot move the target,
    and seeding a different device would leave the installer refusing the one
    it was given.
    """
    for node in installation.disk.graph.of_type(Existing):
        if node.selector:
            return node.selector
    raise ProxmoxError(f"{installation.disk.mode.value} names no disk to seed")


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


def retain_driver(workdir: Path, built: Path) -> Path:
    """Move a completed driver to its content-derived campaign path."""
    target = workdir / remote_name(built)
    if built != target:
        built.replace(target)
    return target


def trusted_revision(identity: str) -> bool:
    """Whether an outcome identifies a clean tree or an explicit test identity."""
    dirty = re.search(r"(?:^| )dirty=(\d+)(?: |$)", identity)
    return bool(identity) and (dirty is None or dirty.group(1) == "0")


def free_slots(
    api: Api,
    placed: Mapping[str, int] | None = None,
    cores_placed: Mapping[str, int] | None = None,
) -> list[Node]:
    """One entry per guest the cluster can still hold, most free node first.

    A node appears as many times as it has room for, not once: returning one
    slot per node capped a six-node cluster with 51 GiB spare at three guests
    at a time, with the queue twenty deep.

    `placed` is the bytes this schedule has already put on each node, and it
    is subtracted from what the node reports. A guest's memory is allocated
    lazily, so a node with eleven of them freshly started still reported 13.8
    GiB free: reading that alone dispatched twenty guests wanting 120 GiB onto
    a cluster with 71, on hardware that is running other people's machines.
    """
    need = GUEST_MEMORY_MIB * 1024**2
    held = placed or {}
    cores_held = cores_placed or {}
    per_node: list[list[Node]] = []
    for node in api.nodes():
        room = node.free_bytes - NODE_HEADROOM_BYTES - held.get(node.name, 0)
        by_memory = max(0, int(room // need))
        # A node that is out of cores is out of slots whatever its memory says:
        # `infra-node1` sat at 100% of four cores with 7 GiB free, and the
        # cluster proxy answered 595 for the node next to it. One guest per
        # free core, not one per its vCPU count: cpu time is shared rather than
        # handed out, and requiring a heavy guest's four to be free left five
        # fixtures unschedulable on a four-core node.
        spare = node.free_cores - NODE_HEADROOM_CORES - cores_held.get(node.name, 0)
        by_cores = max(0, int(spare))
        per_node.append([node] * min(by_memory, by_cores))
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


def room_for(
    node: Node,
    job: Job,
    placed: Mapping[str, int] | None = None,
    cores_placed: Mapping[str, int] | None = None,
) -> bool:
    """Whether this node can carry this job now.

    A slot list built from the light size does not answer for a larger job:
    eight heavy guests were dispatched onto nodes with room for four and the
    last four were killed by the hypervisor.
    """
    held = (placed or {}).get(node.name, 0)
    room = node.free_bytes - NODE_HEADROOM_BYTES - held
    spare = (
        node.free_cores - NODE_HEADROOM_CORES - (cores_placed or {}).get(node.name, 0)
    )
    return room >= job.reservation_bytes and spare >= 1.0


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
    reservation_bytes: int
    address: str = ""
    created: bool = False
    #: Held open by a session so the operator's keys reach the same terminal
    #: the screen was read from.
    console: object | None = None


#: What a guest needs beyond its install: the boots either side of it and the
#: installed-state checks between them.
LEASE_MARGIN: Final[float] = 60 * 60.0

#: How long an address lease is honoured. Derived from the ceiling rather than
#: written beside it: at six hours against an eight-hour ceiling a guest could
#: outlive its own lease, another campaign then reserved the same address, and
#: the second guest's initramfs answered `dracut Warning: Duplicate address
#: detected for 10.31.0.151 for interface eth0` and never brought up dropbear.
#: `vm-desktop` really did run 6.3 hours the day this was found.
LEASE_SECONDS: Final[float] = RUN_CEILING + LEASE_MARGIN


#: What a running schedule's command line contains, so that a live pid can be
#: told from a recycled one. A lease is honoured for as long as the process
#: that took it is still that process.
SCHEDULER_COMMAND: Final[str] = "tests.vm.cluster"


def _scheduler_is_running(pid: int) -> bool:
    """Whether that pid is still a schedule of this harness."""
    try:
        said = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return SCHEDULER_COMMAND.encode() in said


class AddressPool:
    """Reserve static addresses under one host-wide scheduler lock."""

    def __init__(self, root: Path, probe: Callable[[str], bool]) -> None:
        self._root = root
        self._probe = probe
        self._lock_path = root / "address.lock"
        self._leases = root / "addresses"

    def _owner(self, lease: Path) -> int:
        """The pid that reserved this address, or 0 when it names nobody."""
        try:
            said = lease.read_text().strip()
        except OSError:
            return 0
        return int(said) if said.isdigit() else 0

    def reserve(self, preferred: str) -> str:
        self._root.mkdir(parents=True, exist_ok=True)
        self._leases.mkdir(exist_ok=True)
        with self._lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # A lease belongs to a schedule for as long as that schedule runs,
            # however long that is: a campaign outlives its own ceiling, and
            # `vm-unlock` lost 10.31.0.151 to a round that started 92 minutes
            # after it. The age rule stays for a schedule that was killed:
            # sixteen rounds once left a hundred leases and the pool was empty
            # from the next dispatch.
            now = time.time()
            held = {
                path.name
                for path in self._leases.iterdir()
                if _scheduler_is_running(self._owner(path))
                or now - path.stat().st_mtime < LEASE_SECONDS
            }
            network, last = preferred.rsplit(".", 1)
            ceiling = GUEST_ADDRESS_BASE + (VMID_LAST - VMID_FIRST)
            for number in range(int(last), ceiling + 1):
                address = f"{network}.{number}"
                if address in held or self._probe(address):
                    continue
                # The name of the holder, not an empty file: `release` unlinks
                # by name, so a schedule that ended freed an address another
                # schedule had taken over, and the guest still answering on it
                # met a second guest that had just been given it.
                (self._leases / address).write_text(f"{os.getpid()}\n")
                return address
        raise ProxmoxError(f"no static address is available from {preferred}")

    def release(self, address: str) -> None:
        with self._lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            lease = self._leases / address
            if self._owner(lease) in (0, os.getpid()):
                lease.unlink(missing_ok=True)


def guest_log(workdir: Path, fixture: str, vmid: int, driver: str) -> Path:
    """Where one guest's console is kept.

    The vmid, because every schedule shares this directory and two rounds
    carrying one fixture wrote the same file at the same time: `vm-greetd` was
    in two rounds at once on the day that was found. The driver as well,
    because a vmid is reused between rounds: a later round running the same
    fixture on the same vmid overwrote the earlier log, and run109's `ext2`
    record was gone when its revision was asked for.
    """
    return workdir / f"{fixture}-{vmid}-{_driver_tag(driver)}.log"


def _driver_tag(driver: str) -> str:
    """The content-addressed part of a driver CD's name, which is what says
    the guest was built by one installer rather than another."""
    stem = PurePath(driver).stem
    tag = stem.rsplit("-", 1)[-1] if stem.startswith(DRIVER_PREFIX) else ""
    return tag[:10] or "driver"


def _execution(
    api: Api,
    node: str,
    job: Job,
    driver: str,
    workdir: Path,
    vmid: int,
    nonce: str,
    address: str = "",
) -> Running:
    chosen = vmid or api.free_vmid()
    log = guest_log(workdir, job.name, chosen, driver)
    guest = Guest(
        api=api,
        node=node,
        vmid=chosen,
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
    return Running(
        guest,
        Watchdog(
            log=log,
            counters=lambda: guest.transferred(),
            where=node,
            load=lambda: api.node_load(node),
            state=lambda: guest.qmp_state(),
        ),
        job.reservation_bytes,
        address,
    )


def _reserved_bytes(scheduled: Mapping[str, Job]) -> Counter[str]:
    held: Counter[str] = Counter()
    for job in scheduled.values():
        if not job.holds_resources:
            continue
        if job.node is None or job.execution is None:
            raise ProxmoxError(f"{job.name} holds resources without an execution")
        held[job.node] += job.execution.reservation_bytes
    return held


def _reserved_cores(scheduled: Mapping[str, Job]) -> Counter[str]:
    held: Counter[str] = Counter()
    for job in scheduled.values():
        if not job.holds_resources:
            continue
        if job.node is None:
            raise ProxmoxError(f"{job.name} holds resources without a node")
        # One per guest, matching what `free_slots` subtracts: a guest's
        # sustained load is not its vCPU count, and counting that made a heavy
        # job need more cores than any node in this cluster has.
        held[job.node] += 1
    return held


def _reserve_job(
    api: Api,
    node: str,
    job: Job,
    driver: str,
    workdir: Path,
    held_vmids: frozenset[int],
    address: str = "",
) -> Job:
    """Create a cluster guest before recording its lease and dispatch."""
    excluded = set(held_vmids)
    nonce = f"gi-{uuid.uuid4().hex[:12]}"
    conflict: CreateConflict | None = None
    for _ in range(RESERVATION_TRIES):
        vmid = api.free_vmid(frozenset(excluded))
        execution = _execution(api, node, job, driver, workdir, vmid, nonce, address)
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


def _note_the_probe(link: Reconnecting, when: str) -> None:
    """Measure the network, and never make the measurement the verdict.

    `vm-btrfs` and `vm-mdraid` were recorded `ERROR` in run60 at 96 and 81
    minutes with `installed 54 operations` and `installed 58 operations`
    already in their logs, and five guests of run61 at three minutes with no
    install started at all. Both were the probe's own `getent` calls waiting
    on a resolver that had stopped answering.
    """
    try:
        link.run(REACHABILITY_PROBE, timeout=120.0)
    except (ConsoleTimeout, ConsoleClosed) as error:
        print(f"the {when} reachability probe did not answer: {error}", flush=True)


#: What each session's guest is built as. The spec is a sentence the agent
#: is given; the shape here is only what the machine needs to exist, and it
#: is deliberately the same for all four so a difference in the report is a
#: difference in the interface rather than in the hardware.
#: The grid the session reads and the guest draws on. One number, so the two
#: cannot drift: a serial line carries no size and ncurses guesses 80x24.
TUI_LINES: Final[int] = 40
TUI_COLUMNS: Final[int] = 120

#: Disks, firmware, and whether the guest needs the CJK medium. The official
#: minimal ISO carries no CJK font, so a Chinese session on it reads as a
#: screen of empty boxes and says nothing about the wording. `gentoo-zh`'s
#: own live CD carries cjktty and the fonts.
#: Disks, UEFI, and whether the medium has to carry a CJK font. One entry per
#: spec, because the machine a spec describes is part of the spec: asking for
#: an MBR table on a UEFI guest is asking the operator to answer an impossible
#: question, and the run would measure that rather than the interface.
#: Specs that describe a machine already running something. A blank guest
#: cannot answer them: the interface itself says so on its first screen —
#: `conversion is not offered: this medium has no installed system to
#: replace` — and an agent sent at one spends its whole run finding that out.
TUI_NEEDS_A_SYSTEM: Final[frozenset[int]] = frozenset({3})

#: Specs the interface has no row for. `--ram` and `--lowram` arm a one-shot
#: boot entry and reboot into a memory-held environment, and they exist only
#: as command-line options: an agent sent at spec 4 found `Build in RAM`,
#: which is Portage's build directory and a different thing, was refused for
#: too little memory, and reported being stuck. Held apart from
#: `TUI_NEEDS_A_SYSTEM` because the guest is not what is missing.
TUI_HAS_NO_ROW: Final[frozenset[int]] = frozenset({4})

TUI_GUESTS: Final[dict[int, tuple[int, bool, bool]]] = {
    1: (1, True, True),
    2: (2, False, True),
    3: (1, True, True),
    4: (1, True, True),
    5: (1, True, True),
    6: (1, True, True),
    7: (2, True, True),
    8: (1, False, False),
    9: (1, True, True),
}


def tui_job(name: str, spec: int) -> Job:
    """A job that carries no fixture, because there is nothing to hand over.

    Every other job names a configuration file. This one names none: what the
    installer is asked to build is typed into it by the operator.
    """
    if spec not in TUI_GUESTS:
        raise ValueError(f"no spec {spec}; there are {sorted(TUI_GUESTS)}")
    disks, uefi, _cjk = TUI_GUESTS[spec]
    return Job(name=name, fixture=Path(), disks=disks, uefi=uefi)


def tui_execution(
    api: Api, node: str, name: str, spec: int, workdir: Path, vmid: int = 0
) -> Running:
    """A guest with the installer at its first screen and nothing answered.

    Every fixture hands the installer a configuration file, so the interface
    is never exercised: a menu nobody can read passes all forty of them. This
    is the other half, and it takes the same path as far as the driver CD.
    """
    # Most free node first, the same order the schedule uses. The node is
    # chosen here rather than by the caller so two sessions started at once
    # cannot both take the last slot on one node.
    if spec in TUI_HAS_NO_ROW:
        raise ValueError(
            f"spec {spec} names a memory-held launch, which is --ram and "
            "--lowram on the command line and has no row in the interface; "
            "an agent sent at it can only report being stuck"
        )
    if spec in TUI_NEEDS_A_SYSTEM:
        raise ValueError(
            f"spec {spec} replaces a running system, so it needs a guest that "
            "already boots one; a fresh guest can only be told that conversion "
            "is not offered"
        )
    chosen = node or free_slots(api)[0].name
    # The medium and the driver CD are per node, and `local` is not shared:
    # a session that skipped this asked the node for `iso/minimal`, which is
    # the name before it is uploaded and not a volume the node has.
    # `build` names the ISO it writes, so this is a file and not a directory
    # to write into; `retain_driver` then moves it to its content-derived name.
    print(f"{name}: building the driver CD", flush=True)
    written = workdir / "driver.iso"
    driver_path = retain_driver(workdir, build_driver(written, packed=True))
    # The CJK live CD for a session, because the interface is Chinese and the
    # official minimal ISO has no font for it: every label would read as a row
    # of empty boxes and the run would say nothing about the wording.
    cjk = TUI_GUESTS[spec][2]
    medium, urls, medium_sha512 = current_cjk() if cjk else current_minimal()
    print(f"{name}: placing {medium} on {chosen}", flush=True)
    prepare(
        api, chosen, medium, urls, medium_sha512, MEDIUM_TRUST,
        driver_path, driver_path.name,
        local=cached_medium(medium, urls, medium_sha512) if cjk else None,
    )
    job = replace(tui_job(name, spec), iso=medium)
    held = _execution(
        api, chosen, job, driver_path.name, workdir, vmid=vmid, nonce=uuid.uuid4().hex
    )
    guest = cast(Guest, held.guest)
    print(f"{name}: booting {guest.vmid} on {chosen}", flush=True)
    guest.create()
    guest.start()
    link = Reconnecting.to(guest, held.watch.log)
    guest.reset()
    if job.uefi:
        _edit_uefi_cmdline(guest, link)
    else:
        _edit_bios_cmdline(guest, link)
    print(f"{name}: waiting for the live medium", flush=True)
    reach_prompt(link)
    wait_for_network(link, guest.vmid, held.address)
    link.run(FIND_DRIVER)
    link.run(f"mkdir -p {RESULT_DIR}")
    # Both ends have to agree on the size or the interface draws for one grid
    # and is read on another: a serial line reports nothing, so ncurses fell
    # back to 80x24 while the screen was rebuilt at 120x40 and every row past
    # column 80 was read from the wrong cells.
    link.run(f"stty rows {TUI_LINES} cols {TUI_COLUMNS}")
    # No `--config`: the menu is the subject.
    link.console.send(
        f"TERM=xterm LINES={TUI_LINES} COLUMNS={TUI_COLUMNS} "
        "sh /mnt/driver/install.sh --lang zh-TW"
    )
    held.console = link.console
    return held


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
    remote_key: Path | None = None,
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
            _edit_uefi_cmdline(guest, link)
        else:
            _edit_bios_cmdline(guest, link)
        reach_prompt(link)
        # The guest's own resolver is left alone. A local run pins one because
        # slirp reads the host's `/etc/resolv.conf` once at startup; the
        # cluster hands out a real configuration, and it is IPv6 with DNS64.
        # Writing IPv4 resolvers over it left every mirror unreachable:
        # `Failed to connect to mirrors.tuna.tsinghua.edu.cn:443 after 111 ms`.
        wait_for_network(link, guest.vmid, held.address)
        stage_passphrases(link, load(job.installed_config or job.fixture))
        link.run(FIND_DRIVER)
        link.run(f"mkdir -p {RESULT_DIR}")
        # tee, not a redirect: the serial console is the only way to watch a
        # run that takes half an hour, and it is what the watchdog reads to
        # tell a slow mirror from a dead guest.
        # `wait_for`, not `run`: a console dropped mid-install is reopened and
        # listened to again, never handed the command a second time.
        # Again, immediately before the installer: the first measurement is
        # taken a minute earlier and passed on every guest of round 27, whose
        # installers then found no route at all. This one splits that window.
        # The table an editing fixture starts from. `run.py` writes it into the
        # qcow2 before the guest starts; a cluster disk is made by the
        # hypervisor, so the medium's own `parted` writes it here instead.
        # Without it `mbr-edit` refuses in five minutes with
        # `did not report its partitions, so what table keeps cannot be counted`.
        seeded = SEEDED.get(job.fixture.stem, ())
        if seeded:
            selector = _first_selector(load(job.installed_config or job.fixture))
            link.run(
                f"parted --script {selector} {' '.join(seeded)}",
                timeout=180.0,
                repeatable=False,
            )
        _note_the_probe(link, "pre-install")
        phase = Phase.INSTALL
        link.wait_for(
            f"{{ sh /mnt/driver/install.sh --config fixtures/{job.fixture.name}; "
            f"echo $? > {RESULT_DIR}/install.rc; }} 2>&1 | tee {RESULT_DIR}/install.txt",
            timeout=RUN_CEILING,
            idle=INSTALL_IDLE,
            watch=watch,
        )
        # A third time, after the install: a console that still has its routes
        # when the installer saw none puts the loss in the installer's own
        # view of the machine rather than in the machine.
        _note_the_probe(link, "closing")
        files = collect(guest, link, log)
        keep_results(log, files)
        code = files.get("install.rc", b"").strip()
        if job.name in EXPECTED_TO_FAIL:
            # The install stopping is the whole answer here, so there is no
            # installed system to boot afterwards and the run ends on this line.
            why = _did_not_stop(job.name, code, files)
            return Outcome(
                job.name,
                Verdict.FAIL if why else Verdict.OK,
                time.monotonic() - started,
                why,
                log,
                phase=phase,
                revision=revision,
            )
        missing = (
            _degradation_missing(job.name, files) or _binary_packages_missing(job.name, files)
            if code == b"0"
            else ""
        )
        if missing:
            return Outcome(
                job.name,
                Verdict.FAIL,
                time.monotonic() - started,
                missing,
                log,
                phase=phase,
                revision=revision,
            )
        if code != b"0":
            outcome = Outcome(
                job.name,
                Verdict.FAIL,
                time.monotonic() - started,
                (
                    f"the installer exited {code!r}{_why_it_stopped(files)}"
                    if code
                    else "the installer wrote no exit code, so it died before its "
                    "last line: read the log"
                ),
                log,
                phase=phase,
                revision=revision,
            )
            return outcome
        # The install finishing is half the question. The other half is whether
        # the machine it produced comes up and carries what was asked for, and
        # nothing in the log above can answer that.
        phase = Phase.BOOT_INSTALLED
        wrong = boot_and_check(
            guest,
            link,
            log,
            load(job.installed_config or job.fixture),
            remote_key,
            held.address,
        )
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
        if job.convert_to is not None:
            phase = Phase.CONVERT
            wrong = convert_and_check(guest, link, log, job.convert_to, held.address, watch)
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
        dropped = console_proxy_dropped(log)
        outcome = Outcome(
            job.name,
            verdict,
            time.monotonic() - started,
            dropped or str(error)[:OUTCOME_BYTES],
            log,
            phase=phase,
            revision=revision,
            proxy_dropped=bool(dropped),
        )
        return outcome
    except (ProxmoxError, ResultError, OSError) as error:
        outcome = Outcome(
            job.name,
            Verdict.ERROR,
            time.monotonic() - started,
            str(error)[:OUTCOME_BYTES],
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
    remote_key: Path | None = None,
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
            remote_key,
        )
        outcome = replace(outcome, vmid=outcome.vmid or vmid)
        done.put(outcome)
    except Exception as error:
        done.put(
            Outcome(
                job.name,
                Verdict.ERROR,
                0.0,
                f"{type(error).__name__}: {error}"[:OUTCOME_BYTES],
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
    site: str = "",
    distfiles: str = "",
    skip_nodes: Sequence[str] = (),
    allow_nodes: Sequence[str] = (),
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
    addresses = AddressPool(ADDRESS_POOL_ROOT, _address_is_taken)
    remote_key = ssh_keypair(workdir) if any(job.remote_unlock for job in jobs) else None
    public_key = remote_key.with_suffix(".pub").read_text().strip() if remote_key else ""
    # Packed: the ingress refuses the 1.4 MiB loose-file CD with `413`.
    staging = workdir / f".driver-{uuid.uuid4().hex}.iso"
    fixture_dir = _fixture_dir(workdir)
    # Reserved before the CD is written, not at dispatch: a fixture that
    # unlocks over SSH pins the initramfs address, and the harness has to ssh
    # to that address. `vm-unlock` carried `192.0.2.10`, which is a
    # documentation address; on the cluster the guest was on 10.31.0.155 and
    # the unlock answered `No route to host` after ninety minutes.
    unlock_addresses = {
        job.name: addresses.reserve(f"{GUEST_NETWORK}.{GUEST_ADDRESS_BASE}")
        for job in jobs
    }
    rewritten = rewrite_fixtures(
        jobs, fixture_dir, region, sync, public_key, site, unlock_addresses, distfiles
    )
    # Rebuilt rather than mutated: `Job` is frozen so that a scheduler state
    # change is always a new object.
    jobs = [replace(job, installed_config=rewritten / job.fixture.name) for job in jobs]
    built_path = build_driver(staging, packed=True, fixtures=rewritten)
    driver_path = retain_driver(workdir, built_path)
    driver = driver_path.name
    revision = revision_identity(driver_path)
    # Said once here as well as in every guest's log: a schedule's own output
    # is the file a reader opens first, and identifying which revision run109
    # measured meant reading a guest log that a later round had overwritten.
    print(f"installer revision: {revision}", flush=True)
    medium, urls, medium_sha512 = current_minimal()
    prepared: set[str] = set()
    # A node whose console proxy went away takes no more guests: three runs on
    # `infra-node3` were lost to the same dropped ssh while their installs
    # were going perfectly well. The seed is what makes that cost nothing: the
    # automatic detection has to lose a guest to each bad node before it acts.
    unreachable: set[str] = (set(KNOWN_BAD_NODES) | set(skip_nodes)) - set(allow_nodes)
    done: queue.Queue[Outcome] = queue.Queue()
    scheduled = {job.name: job for job in jobs}
    collected = 0
    swept = time.monotonic()
    capacity_since: float | None = None

    try:
        while not all(job.collected for job in scheduled.values()):
            waiting = [job for job in scheduled.values() if job.status is JobStatus.WAITING]
            running = [job for job in scheduled.values() if job.running]
            placed = _reserved_bytes(scheduled)
            cores_placed = _reserved_cores(scheduled)
            try:
                slots = [
                    node
                    for node in free_slots(api, placed, cores_placed)
                    if node.name not in unreachable
                ]
            except ProxmoxTransientError as error:
                # No slots this round, not the end of the schedule: run58 lost
                # seven fixtures to one `GET /nodes did not answer: Remote end
                # closed connection without response`, with every guest still
                # installing. `capacity_since` below is what ends a schedule
                # that genuinely has nowhere to run.
                print(f"the cluster's capacity could not be read: {error}", flush=True)
                slots = []
            if limit:
                slots = slots[: max(0, limit - len(running))]
            if waiting and not running and not slots:
                now = time.monotonic()
                if capacity_since is None:
                    capacity_since = now
                waited = now - capacity_since
                patience = _capacity_patience(api)
                if waited >= patience:
                    for job in waiting:
                        scheduled[job.name] = job.capacity_failed(waited, revision).collect(
                            collected
                        )
                        collected += 1
                    continue
                time.sleep(min(POLL_WHILE_QUEUED, patience - waited))
                continue
            capacity_since = None
            while waiting and slots:
                # The first job this node has room for, not the first job:
                # a heavy guest wants twice the memory, and taking it from a
                # node with one light slot left is how the hypervisor came to
                # kill it. A light job behind it still goes now.
                index = next(
                    (
                        at
                        for at, one in enumerate(waiting)
                        if room_for(slots[0], one, placed, cores_placed)
                    ),
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
                address = unlock_addresses[job.name]
                try:
                    job = _reserve_job(
                        api,
                        node.name,
                        job,
                        driver,
                        workdir,
                        held_vmids,
                        address,
                    )
                except ProxmoxTransientError as refused:
                    # The node, not the job: `POST /nodes/infra-node3/qemu`
                    # answered `595 Connection refused` and the exception left
                    # the loop, so the closing path removed five guests that
                    # were installing. That node takes no more of them and the
                    # job goes back to the queue.
                    unreachable.add(node.name)
                    waiting.insert(index, job)
                    print(
                        f"! {node.name} refused a guest ({refused}); "
                        f"{job.name} goes back to the queue",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
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
                        remote_key,
                    ),
                    # A worker reading the console of a guest the closing path
                    # already removed never returns, and a schedule ended by a
                    # signal then hangs at interpreter shutdown joining it.
                    daemon=True,
                )
                scheduled[job.name] = job.started(thread)
                placed[node.name] += execution.reservation_bytes
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
            if outcome.proxy_dropped and job.node is not None:
                unreachable.add(job.node)
                print(f"  {job.node} takes no more guests this campaign", file=sys.stderr)
            print(
                f"{outcome.verdict.value:6} {outcome.name:34} {outcome.seconds / 60:5.1f}m "
                f"{_one_line(outcome.detail)}",
                flush=True,
            )
    finally:
        _abandon_jobs(scheduled)
        # After the guests are gone, not before: the pool is shared with every
        # other campaign on this machine, and an address handed out again while
        # a guest still answers on it puts two machines on one address.
        for one in unlock_addresses.values():
            addresses.release(one)
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
#: A root shell's prompt, whatever the machine calls itself. The live medium
#: says `livecd`, a machine this installer produced says its own hostname, and
#: a converted one says a third: `vm-convert` waited seventy-three minutes for
#: `root@livecd ~ # ` on a guest that had rebooted into `xfsbox`.
_ANY_ROOT_PROMPT: Final[str] = r"root@[A-Za-z0-9._-]+ ~ # "

#: What Proxmox prints when a console session starts. Seen in the middle of a
#: marked command in run78, whose `MARK_25_DONE` never arrived.
_SERIAL_TERMINAL_STARTED: Final[str] = "starting serial terminal on interface serial0"


def _marker_lost(error: ConsoleTimeout) -> bool:
    """Whether a console session started under the command that timed out."""
    return _SERIAL_TERMINAL_STARTED in str(error)


_Result = TypeVar("_Result")


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


#: Fixtures whose install is supposed to stop. `vm-proxy-dead` points every
#: fetch at a port nothing listens on, so `the installer exited 4` is the
#: result it exists to produce; recording that as a failure made it the only
#: fixture that could never be green, and it was counted as unverified for
#: weeks. `campaign.py` knew this and the cluster did not.
#: Mapped to the exit code and the words the machine has to produce, because
#: "stopped" is not the claim: `vm-proxy-dead` exists to show that nothing
#: bypassed the proxy, and a syntax error, a refused preflight or a failed
#: disk stops the install just as non-zero. Measured on a passing run:
#: `install.rc` held `4` and `Connection refused` appeared 26 times.
EXPECTED_TO_FAIL: Final[Mapping[str, tuple[bytes, str]]] = MappingProxyType(
    {"vm-proxy-dead": (b"4", "Connection refused")}
)


#: What `cli.py` prints as its last line when an install fails. The verdict
#: carried the exit code alone, and reading `vm-greetd`'s reason out of
#: `install.txt` meant 294910 lines of build output first.
INSTALL_STOPPED: Final[str] = "the install stopped: "

#: How much of that line the verdict keeps.
REASON_BYTES: Final[int] = 400


def _why_it_stopped(files: Mapping[str, bytes]) -> str:
    """The installer's own last word, or empty when it did not say one."""
    # The tail the console carries back, and the whole log for an archive an
    # older revision made: the guest stopped shipping 80 MB of build output
    # through a channel that had dropped on it twice.
    carried = files.get(LOG_TAIL) or files.get("install.txt", b"")
    said = carried.decode("utf-8", "replace")
    for line in reversed(said.splitlines()):
        found = line.find(INSTALL_STOPPED)
        if found >= 0:
            return f": {line[found + len(INSTALL_STOPPED):].strip()[:REASON_BYTES]}"
    return ""


def _did_not_stop(name: str, code: bytes, files: Mapping[str, bytes]) -> str:
    """Why an expected-to-fail fixture is not a pass, or empty when it is.

    An absent code is not a stop. `code != b"0"` was the whole rule, and a
    guest whose results never came back leaves it empty, so a run that
    collected nothing read as the failure the fixture exists to produce. The
    code and the reason, because the local runner has checked both since it
    was written and the cluster checked neither.
    """
    wanted, marker = EXPECTED_TO_FAIL[name]
    if not code:
        return "the install was supposed to stop and wrote no exit code"
    if code == b"0":
        return "the install was supposed to stop and did not"
    if code != wanted:
        return (
            f"the install stopped with {code!r}, not the {wanted!r} this fixture "
            "exists to produce"
        )
    said = files.get(LOG_TAIL, b"") + files.get("install.txt", b"")
    if marker.encode() not in said:
        return (
            f"the install stopped without {marker!r}, so what stopped it is not "
            "what this fixture measures"
        )
    return ""

#: Fixtures whose mirror site is the thing under test, so `--site` must not
#: move it. `vm-binhost-fallback` names `xtom-hk`, whose binary package index
#: answers 404, and the run is green only if Portage drops that host and
#: compiles from source. Rewritten to a working site it installed from a
#: binhost in 42 minutes and proved nothing.
KEEPS_ITS_SITE: Final[frozenset[str]] = frozenset({"vm-binhost-fallback"})

#: Fixtures whose whole point is that the install gives something up and
#: carries on, mapped to what `install.jsonl` must record it giving up. Without
#: this the fixture is green when the install merely finished, which it does
#: whether or not the host it names ever failed.
MUST_DEGRADE: Final[Mapping[str, str]] = MappingProxyType(
    {"vm-binhost-fallback": BINARY_PACKAGES}
)


#: Fixtures whose point is that the binary host served them. The mirror image
#: of `MUST_DEGRADE`: a binhost install that quietly compiled everything after
#: a degradation finishes, boots and passes every other check, and `vm-binpkg`
#: is the blocking fixture for that path. `install.jsonl` carries one entry
#: per package with where it came from, and `#746` stopped `emerge --pretend`
#: from being counted as a merge.
MUST_USE_A_BINARY_HOST: Final[frozenset[str]] = frozenset({"vm-binpkg"})


def _binary_packages_missing(name: str, files: Mapping[str, bytes]) -> str:
    """Empty when this fixture's journal records packages from a binary host."""
    if name not in MUST_USE_A_BINARY_HOST:
        return ""
    journal = files.get("install.jsonl")
    if journal is None:
        return f"{name} has to install from a binary host and the guest handed back no journal"
    binary = 0
    for line in journal.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") == "degraded" and entry.get("what") == BINARY_PACKAGES:
            return (
                f"{name} gave up on {BINARY_PACKAGES!r} and compiled instead, "
                f"which is what it exists to measure not happening: {entry.get('reason', '')}"
            )[:REASON_BYTES]
        if entry.get("event") == "package" and entry.get("source") == "binary":
            binary += 1
    if not binary:
        return f"{name} finished with no package from a binary host"
    return ""


def _degradation_missing(name: str, files: Mapping[str, bytes]) -> str:
    """Empty when the journal records what this fixture had to give up."""
    wanted = MUST_DEGRADE.get(name)
    if wanted is None:
        return ""
    journal = files.get("install.jsonl")
    if journal is None:
        return f"{name} has to degrade {wanted!r} and the guest handed back no journal"
    for line in journal.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # A run killed mid-write leaves a partial last line.
            continue
        if entry.get("event") == "degraded" and entry.get("what") == wanted:
            return ""
    return f"{name} finished without degrading {wanted!r}, which is what it exists to measure"

#: Nodes that take no guests until `--allow-node` names them. Empty because
#: `infra-node3`, which 66 of the 70 recorded console-proxy drops named, took
#: two fixtures to green on 2026-08-17 with one reconnect each -- fewer than
#: the same fixtures on nodes that were never excluded. A named node stops
#: producing the evidence that would clear it, and the automatic detection
#: still costs one guest if a node goes bad again.
KNOWN_BAD_NODES: Final[frozenset[str]] = frozenset()

#: How many times one guest's console may be reopened before the node itself
#: is called the problem. A healthy run reconnects a handful of times over an
#: hour; a node whose proxy is refusing grants and closes a session every time,
#: which raises nothing and burns the whole run ceiling. Derived from the
#: grants, because a ceiling below what the grants can spend ends a working
#: guest before its last grant: `vm-gnome` was ERRORed mid-compile at 156.5
#: minutes.
REOPEN_CEILING: Final[int] = RECONNECT_GRANTS * RECONNECT_TRIES


#: How much of a command a verdict carries. `vm-zram` ended a round with a
#: mirror probe naming eight URLs, and the command alone filled all 300 bytes
#: of the verdict: the reason it failed was never written down.
COMMAND_IN_A_VERDICT: Final[int] = 80


def _shortened(command: str) -> str:
    if len(command) <= COMMAND_IN_A_VERDICT:
        return command
    return f"{command[:COMMAND_IN_A_VERDICT]}… ({len(command)} chars)"


@contextmanager
def _naming(command: str) -> Iterator[None]:
    """Put the command into a marker's timeout, which otherwise names a token.

    `vm-convert` ended a 156-minute run with `never matched 'MARK_63_DONE'`,
    and reading which of sixty-three commands that was took the source. Enough
    of it to recognise it, and no more: the reason has to fit as well.
    """
    try:
        yield
    except ConsoleIdle as error:
        raise ConsoleIdle(f"{error} while running {_shortened(command)!r}") from error
    except ConsoleTimeout as error:
        raise ConsoleTimeout(f"{error} while running {_shortened(command)!r}") from error


class Reconnecting:
    """A console that opens another one when the cluster drops it.

    A `termproxy` session does not survive an install, and under a full
    schedule it does not always survive a boot: three guests reported `the
    guest closed the serial connection` a minute in, with the kernel visibly
    running in what the log had already captured. The guest is fine; only the
    connection to it is gone.

    `run` resends repeatable commands; `wait_for` does not, because it could
    start a second install.
    """

    def __init__(self, open_console: Callable[[], Line], tries: int = RECONNECT_TRIES) -> None:
        self._open = open_console
        self._tries = tries
        self._marks = itertools.count(1)
        self._reopens = 0
        self.console: Line = open_console()

    @classmethod
    def to(cls, guest: Guest, log: Path, tries: int = RECONNECT_TRIES) -> Reconnecting:
        return cls(lambda: SerialConsole(guest.console(), log.open("ab")), tries)

    def reopen(self, *, solicit_prompt: bool = True) -> None:
        self._reopens += 1
        if self._reopens > REOPEN_CEILING:
            # A node whose proxy is refusing grants a session and closes it at
            # once, so every reopen succeeds and nothing ever raises: two guests
            # on `infra-node3` sat at step 11 for forty minutes with their
            # installs running and no way to read them.
            raise ConsoleClosed(
                f"the console was reopened {self._reopens} times and kept closing",
                write_may_have_reached_guest=False,
            )
        # Before the new one, not after: `termproxy` holds the guest's serial
        # chardev until its client leaves, and the second session reads nothing.
        try:
            self.console.close()
        except OSError:
            pass
        self.console = self._open()
        if solicit_prompt:
            # The reopened console shows nothing until the shell is asked for
            # a prompt, and ordinary shell waits below are looking for text.
            self.console.send("")

    def close(self) -> None:
        self.console.close()

    def set_buffer_limit(self, limit: int) -> None:
        cast(SerialConsole, self.console).set_buffer_limit(limit)

    def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
        return self._with_reconnect(
            timeout,
            lambda deadline: self.console.expect(pattern, _remaining(deadline)),
        )

    def observe(self, pattern: str, timeout: float, *, solicit: bool = False) -> bytes:
        """Wait through a reconnect without writing to the guest.

        Boot prompts own the console input field. An empty line there is a
        password attempt, not a harmless request for another shell prompt.
        `solicit` is for the wait where that does not hold: `login:` is printed
        once, so a console reopened past it never sees the prompt again.
        """
        return self._with_reconnect(
            timeout,
            lambda deadline: self.console.expect(pattern, _remaining(deadline)),
            solicit_on_reconnect=solicit,
        )

    def run(self, command: str, timeout: float = 120.0, *, repeatable: bool = True) -> None:
        """Send a marked command and wait for its marker.

        `repeatable` is false for a command that must not run twice: `parted`
        would write a second partition beside the first, and re-sending it
        after a console session started under it is worse than ending the run.
        """

        tokens: list[int] = []

        def run_once(deadline: float) -> None:
            tokens.append(next(self._marks))
            self.console.send(marked_command(command, tokens[-1]))
            # Any marker this call has sent, not only the newest: a session
            # that starts under the command delivers the marker just after
            # the reader gave up on it, so every retry waited for a marker
            # the guest had already printed. `vm-source-kernel` lost 6.5
            # hours of install to this twice, on the last command of the run.
            with _naming(command):
                self.console.expect(
                    "|".join(command_done(one) for one in tokens), _remaining(deadline)
                )

        self._with_reconnect(
            timeout,
            run_once,
            retry_closed=repeatable,
            retry_timeout=_marker_lost if repeatable else None,
        )

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

        The original console requires the done marker because its buffered
        command echo starts at a shell prompt. After a reconnect, `reopen`
        requests a fresh prompt, which also proves that the command returned.

        `idle` is measured from the last byte the guest sent. An install that
        prints for three hours is working and a single ceiling ends it anyway.
        """
        token = next(self._marks)
        sent = False

        def wait_once(deadline: float) -> None:
            nonlocal sent
            after_reconnect = sent
            if not sent:
                sent = True
                self.console.send(marked_command(command, token))
            completion = command_done(token)
            if after_reconnect:
                completion = rf"{completion}|{_ANY_ROOT_PROMPT}"
            while True:
                try:
                    with _naming(command):
                        self.console.expect(completion, _remaining(deadline), idle=idle)
                    return
                except ConsoleIdle as error:
                    if watch is None:
                        raise
                    reason = watch.idle_reason()
                    if reason is None:
                        continue
                    # The reason first and the console tail after it: a
                    # verdict is cut to `OUTCOME_BYTES`, and the console tail
                    # alone filled all 300 of `zbm-unlock`'s, so the round
                    # could not say whether its counters had moved.
                    raise ConsoleTimeout(
                        f"the console was silent for {idle:.0f}s and {reason}: {error}"
                    ) from error

        self._with_reconnect(timeout, wait_once, watch=watch)

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
            self.console.send(marked_command(command, token))
            with _naming(command):
                self.console.expect(command_begin(token), _remaining(deadline))
                said = self.console.expect(command_done(token), _remaining(deadline))
            # Without the carriage returns: a serial line ends every one of
            # them `\r\n`, so a pattern anchored with `$` matches nothing at
            # all. `btrfs-luks` was failed at 130.3 minutes for an
            # `inputmethod` check whose three lines were on the console.
            return said.split(command_done(token).encode())[0].replace(b"\r", b"")

        return self._with_reconnect(
            timeout, collect_once, retry_timeout=_marker_lost
        )

    def _with_reconnect(
        self,
        timeout: float,
        operation: Callable[[float], _Result],
        *,
        retry_closed: bool = True,
        retry_timeout: Callable[[ConsoleTimeout], bool] | None = None,
        solicit_on_reconnect: bool = True,
        watch: Watchdog | None = None,
    ) -> _Result:
        """Run `operation`, reopening the console under it while time is left.

        A `watch` makes the deadline conditional: the console going is not the
        guest going, and `vm-gnome` lost 167 minutes of compiling to one
        `Connection reset by peer` because the two were treated as the same
        thing. A silent console already asks the counters through
        `ConsoleIdle`; this asks them on the other failure as well.
        """
        deadline = time.monotonic() + timeout
        granted = 0
        attempt = 0
        while True:
            try:
                return operation(deadline)
            except ConsoleClosed:
                if not retry_closed:
                    raise
                attempt += 1
                if attempt >= self._tries or _remaining(deadline) <= 0.0:
                    # `moved()` is asked once, and only here: it takes a sample
                    # the scheduler's sweep also reads.
                    if watch is None or granted >= RECONNECT_GRANTS or not watch.moved():
                        raise
                    granted += 1
                    attempt = 0
                    # Never shorter than what was left: the grant exists to
                    # buy time past an exhausted ceiling, and assigning it
                    # replaced eight hours with fifteen minutes. `vm-desktop`
                    # was ended at `899s of 898s elapsed` with
                    # `dev-qt/qtsensors` compiling on the console, six hours
                    # into a run whose ceiling had hours left.
                    deadline = max(deadline, time.monotonic() + RECONNECT_GRANT)
                    print(
                        f"… the console dropped and the guest is still working, "
                        f"{RECONNECT_GRANT / 60:.0f}m more",
                        flush=True,
                    )
                self.reopen(solicit_prompt=solicit_on_reconnect)
            except ConsoleTimeout as error:
                if retry_timeout is None or not retry_timeout(error):
                    raise
                attempt += 1
                if attempt >= self._tries:
                    raise
                deadline = time.monotonic() + timeout
                self.reopen(solicit_prompt=solicit_on_reconnect)

    def send(self, line: str) -> None:
        self._reopen_if_closed()
        self.console.send(line)

    def respond(self, line: str) -> None:
        """Answer an observed boot prompt once.

        A response is retried only when the transport proves that no byte
        reached the guest. Unknown or partial delivery cannot be made safe by
        sending a password or username again.
        """
        closed: ConsoleClosed | None = None
        for attempt in range(self._tries):
            try:
                if self.console.closed:
                    raise ConsoleClosed(
                        "the console was already closed",
                        write_may_have_reached_guest=False,
                    )
                self.console.send(line)
                return
            except ConsoleClosed as error:
                closed = error
                if error.write_may_have_reached_guest is not False:
                    raise
                if attempt + 1 == self._tries:
                    raise
                self.reopen(solicit_prompt=False)
        assert closed is not None
        raise closed

    def send_raw(self, keys: str) -> None:
        self._reopen_if_closed()
        self.console.send_raw(keys)

    def _reopen_if_closed(self) -> None:
        """A write to a dropped connection is silently discarded.

        That is what the transport should do rather than raise, and it left
        the command undelivered: `wait_for_network` sent its probe into a
        closed socket and then waited fifteen minutes for output that was
        never going to come. Eight guests failed that way in one round.

        Unsolicited, because the caller is about to write: the line it sends
        is the request for a prompt, and the empty one a solicit adds is a
        line the guest answers first. At a `login:` prompt agetty takes it as
        an attempt and reprints its banner, and every send after that answers
        the prompt before the one it was written for. `vm-lvm` and
        `openrc-sdboot` failed that way in run64, run65 and run66: the console
        held `lvmbox login: ` with an empty line under it, then `root`,
        `Password:`, an empty password, and `install` echoed at the next
        `login:`.
        """
        if not self.console.closed:
            return
        for attempt in range(self._tries):
            try:
                self.reopen(solicit_prompt=False)
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


def _asked_for(installation: InstallConfig) -> list[tuple[str, str, str]]:
    """What this particular configuration should have produced.

    Derived from the model rather than written out once for every fixture:
    `hostname` and `kernel` carried an empty expectation, so a guest with the
    wrong hostname, the wrong root filesystem or the wrong locale passed every
    check, and a run that installed the wrong system was recorded as `ok`.
    """
    return [(check.name, check.command, check.pattern) for check in checks(installation)]


#: What agetty sleeps before it flushes the serial line and starts reading, so
#: a name sent the moment `login:` appears is discarded. Its own value plus a
#: margin, because the sleep starts when the prompt is written.
AGETTY_FLUSHES_AFTER: Final[float] = 2.0

#: How many times the user name is offered. An empty line brings the prompt
#: back, so this must end rather than answer for ever.
LOGIN_TRIES: Final[int] = 4


#: Who the installed system is logged in as. Its echo is what tells a fresh
#: password prompt from one the attempt before left in the buffer.
NAME: Final[str] = "root"


def _name_the_user(link: Reconnecting) -> bool:
    """Give the login prompt a name, and say whether it took one.

    agetty sleeps a second after writing the prompt and then flushes whatever
    arrived, so a name typed at once is thrown away and the empty line that
    follows it brings the prompt back. `openrc-sdboot` and `vm-bios` printed
    their issue three times and were failed for a password prompt that was
    never coming.
    """
    for attempt in range(LOGIN_TRIES):
        # Nothing before the first name. `tests/vm/run.py` types it the moment
        # the prompt is read and passes: `vm-lvm` installs and logs in locally
        # on the same fixture and the same revision, and failed on the cluster
        # in four rounds where a blind two seconds went first. The caller has
        # already waited for `login:`, so the prompt is there.
        if attempt:
            # It came back, so that name did not take. This is the wait agetty
            # needs, and it belongs where the console has proved it.
            time.sleep(AGETTY_FLUSHES_AFTER)
        link.respond(NAME)
        try:
            # The echo of the name first: the prompt that follows it is the
            # one this name produced. `ext3` was failed for a `Password:` left
            # in the buffer by the attempt before, which read as an answer and
            # sent the password into the next login prompt's name field.
            link.observe(rf"{NAME}", timeout=60.0)
            said = link.observe(
                rf"{PASSWORD_PROMPT}|{'|'.join(LOGIN_PROMPTS)}", timeout=60.0
            )
        except ConsoleTimeout:
            continue
        if not any(one.encode() in said for one in LOGIN_PROMPTS):
            return True
    return False


#: What is waited after `Password:` before the password is sent. Zero: the
#: implementation that passes sends it the moment the prompt is read, and
#: `vm-lvm` failed on the cluster in four rounds where it was sent a second
#: later. `login` turns the echo off before it writes the prompt, so there is
#: no window to wait out; the growth below is kept for the console that proves
#: otherwise by echoing.
PASSWORD_ECHO_OFF_AFTER: Final[float] = 0.0

#: Added to the settle each time the console proves it lost that race, and how
#: many of those are absorbed. A fixed wait is a guess about a machine whose
#: load is not ours to choose: `openrc-sdboot` lost the same second twice on a
#: node that spent the run between 13% and 85% busy, and `login` gives up after
#: three attempts of its own, so a race that repeats burns the whole login.
PASSWORD_ECHO_BACKOFF: Final[float] = 2.0
PASSWORD_ECHO_CATCHES: Final[int] = 3

#: What a verdict may carry. The console excerpt above is the reason it is not
#: the 200 the other verdicts use: an excerpt cut to fit says less than none,
#: because it reads as the whole screen.
VERDICT_BYTES: Final[int] = 3600

#: How much of a failed check's answer the verdict carries. The tail rather
#: than the head: a `cat` of a configuration file ends with the lines the
#: pattern was looking for.
ANSWER_BYTES: Final[int] = 1200

#: How long the prompt that follows a refusal is waited for before the next
#: attempt. `login` writes it at once, so this only has to outlast a loaded
#: node rather than anything the guest is doing.
REPROMPT_PATIENCE: Final[float] = 30.0


def _one_line(detail: str) -> str:
    """The detail with its newlines folded, so a verdict is one line.

    A remote command's output reaches the verdict whole, and `zbm-unlock`'s
    ran to three lines: the first held only ssh's known-hosts warning, and the
    two that carried `Key load error: Failed to open key material file` read as
    unrelated records. A reader looking for one verdict per line finds the
    wrong one.
    """
    return " | ".join(one.strip() for one in detail.splitlines() if one.strip())


def _seen_since(link: Reconnecting, said: bytes) -> str:
    """What the console holds, for a verdict that would otherwise be a guess.

    The unlock's verdict carried nothing until `#407` and three rounds were
    spent guessing at it; each round after that answered a different layer
    because the screen was in the failure. A refused login is the same shape:
    the pattern that missed is named and what the guest was showing is not.
    """
    trailing = link.console.snapshot(UNLOCK_SCREEN_PATIENCE)
    held = (said + trailing)[-UNLOCK_SCREEN_BYTES:]
    return repr(held) if held else "nothing"


#: How much of the password has to come back before the attempt is read as
#: this harness losing the echo race rather than the system refusing a
#: password. Two characters: `login` turns the echo off partway through, so
#: what reaches the console is a fragment. `openrc-sdboot` was failed for a
#: refusal whose console held `nstall` under `Password:`, which the whole-word
#: check did not see, and the fragment then went to `login` as the password.
ECHO_FRAGMENT: Final[int] = 2

#: What `login` writes when it will not take a password, in each locale a
#: fixture installs. shadow's `login` is translated, so `vm-unlock` was refused
#: in Chinese and the harness spent its whole budget waiting for the English
#: wording. Taken from shadow's own `po/zh_TW.po` and `po/zh_CN.po`; codepoints
#: rather than literals, because no CJK belongs under `tests/`.
REFUSALS: Final[tuple[bytes, ...]] = (
    b"Login incorrect",
    "\u767b\u5165\u932f\u8aa4".encode(),
    "\u767b\u5f55\u9519\u8bef".encode(),
)

#: What `login` prints instead of a refusal when its own three tries are gone.
#: `ext3` was failed twice for a `Login incorrect` that was never coming: the
#: harness waited its whole 120 seconds while agetty reprinted the prompt
#: beside it, ready for another name.
GAVE_UPS: Final[tuple[bytes, ...]] = (
    b"Maximum number of tries exceeded",
    "\u5df2\u78b0\u5230\u6700\u5927\u5617\u8a66\u6b21\u6578".encode(),
    "\u5df2\u7ecf\u8d85\u8fc7\u6700\u5927\u5c1d\u8bd5\u6b21\u6570".encode(),
)

#: The name prompt, which `login` translates and agetty does not: a guest
#: shows agetty's English one first and `login`'s own after a refusal.
LOGIN_PROMPTS: Final[tuple[str, ...]] = (
    "login:",
    "\u4f7f\u7528\u8005\uff1a",
    "\u7528\u6237\u540d\uff1a",
)


#: What the initramfs says when it could not mount the root it was asked for.
#: The passphrase reaching dropbear is not the passphrase being right: a
#: `zbm-unlock` run handed one over ssh, `dracut-pre-mount` answered `Key load
#: error: Incorrect key provided for 'zpcala'` three times, and the guest sat
#: in emergency mode while the harness typed a login name at it for 93
#: minutes and blamed the login.
INITRAMFS_GAVE_UP: Final[tuple[str, ...]] = (
    "Entering emergency mode",
    "Failed to mount /sysroot",
    "Key load error",
)

#: Enough of the screen to carry the refusal and the prompt above it.
INITRAMFS_SCREEN_BYTES: Final[int] = 600


def _any_of(words: tuple[bytes, ...]) -> str:
    """One pattern matching any of these, for a console wait."""
    return "|".join(re.escape(one.decode()) for one in words)


def _held(said: bytes, words: tuple[bytes, ...]) -> bytes:
    """Whichever of these the console holds, or empty."""
    for one in words:
        if one in said:
            return one
    return b""


def _echoed_back(said: bytes, password: str) -> bool:
    """Whether any of the password reached the console.

    Every fragment of it, not the whole word: the echo is turned off between
    two characters, so which part arrives depends on where that landed. Only
    what came before `Login incorrect` is searched, because the refusal and
    the prompt that follows it carry letters of their own: `Login incorrect`
    holds `in`, and so does `install`.
    """
    echo = said.partition(_held(said, REFUSALS))[0]
    return any(
        password[at : at + width].encode() in echo
        for width in range(len(password), ECHO_FRAGMENT - 1, -1)
        for at in range(len(password) - width + 1)
    )


def _log_in(link: Reconnecting, password: str) -> str:
    """Log root in, and say what is wrong when it does not happen.

    Empty on success. A password that comes back on the console was read as an
    empty one, so that attempt is this harness losing a race rather than the
    installed system refusing a password: it does not count, and the settle
    grows. A refusal with no echo is counted, so a genuinely wrong password
    still ends.
    """
    settle = PASSWORD_ECHO_OFF_AFTER
    refusals = 0
    echoed = 0
    while refusals < LOGIN_TRIES:
        if not _name_the_user(link):
            return "the installed system asked for a name and kept asking"
        time.sleep(settle)
        link.respond(password)
        try:
            said = link.observe(
                rf"#|\$|{_any_of(REFUSALS)}|{_any_of(GAVE_UPS)}", timeout=120.0
            )
        except ConsoleTimeout as error:
            return (
                f"root could not log into the installed system: {error}; "
                f"the console held {_seen_since(link, b'')}"
            )[:VERDICT_BYTES]
        if _held(said, GAVE_UPS):
            # `login` has spent its own three tries and exited; agetty starts
            # another one, so the name goes in again rather than the run
            # waiting for a refusal that cannot arrive.
            refusals += 1
            settle += PASSWORD_ECHO_BACKOFF
            try:
                link.observe(
                    rf"{'|'.join(LOGIN_PROMPTS)}",
                    timeout=REPROMPT_PATIENCE,
                    solicit=True,
                )
            except ConsoleTimeout:
                pass
            continue
        if not _held(said, REFUSALS):
            return ""
        if _echoed_back(said, password) and echoed < PASSWORD_ECHO_CATCHES:
            echoed += 1
            settle += PASSWORD_ECHO_BACKOFF
            continue
        refusals += 1
        # The settle grows on a plain refusal as well, not only on one whose
        # echo the console showed: `ext3` was refused three times with nothing
        # echoed at all, which is the same race losing quietly — some of the
        # password reached `login` and the rest did not. Repeating the settle
        # that produced a refusal can only produce another.
        settle += PASSWORD_ECHO_BACKOFF
        # `Login incorrect` matches before the prompt that follows it, so the
        # re-prompt stays in the buffer. `_name_the_user` then reads that one,
        # calls it a login prompt, and offers the name a second time — and the
        # second one lands in the password field. `vm-lvm` failed at 55.6
        # minutes with `root` echoed under `Password:` for exactly that.
        try:
            link.observe(rf"{'|'.join(LOGIN_PROMPTS)}", timeout=REPROMPT_PATIENCE)
        except ConsoleTimeout:
            pass
    return (
        "the installed system refused every login; "
        f"the console held {_seen_since(link, said)}"
    )[:VERDICT_BYTES]


#: How long SeaBIOS and GRUB take to reach the cryptomount prompt. Nothing on
#: the serial port marks it, so the passphrase is typed after this, and a
#: wrong guess costs one retry at the next prompt, which is waited for.
GRUB_PROMPT_SECONDS: Final[float] = 30.0

#: How many prompts are answered before the passphrase is called wrong. A
#: wrong one brings the prompt back and each one re-arms the timeout, so an
#: unbounded loop would never fail.
UNLOCK_TRIES: Final[int] = 5

#: What a failed remote unlock reads off the console before answering, and how
#: much of it the verdict carries. The guest is already at whatever prompt it
#: reached, so this is a read of a screen that has stopped changing rather than
#: a wait for anything.
UNLOCK_SCREEN_PATIENCE: Final[float] = 5.0

#: How much of that screen the verdict carries. Four hundred bytes held the
#: passphrase prompt and stopped mid-escape-sequence, which answered "the
#: initramfs is running" and nothing about why its network was not: the lines
#: before it are the ones that say whether `systemd-networkd` started. Console
#: output is escape-heavy, so this buys fewer lines than its size suggests.
UNLOCK_SCREEN_BYTES: Final[int] = 3000


class Typeable(Protocol):
    """All the unlock step needs of a guest: GRUB's prompt never reaches the
    serial port, so the passphrase goes to the screen as keystrokes."""

    def send_keys(self, keys: list[str]) -> None: ...


class InstalledBootState(Enum):
    """The prompt whose input field owns the next installed-boot response."""

    WAIT_LOGIN = "wait-login"
    LOGIN_READY = "login-ready"


@dataclass(frozen=True)
class UnlockResult:
    state: InstalledBootState
    refused: str = ""


def _unlock(
    guest: Typeable,
    link: Reconnecting,
    installation: InstallConfig,
    remotely_unlocked: bool = False,
) -> UnlockResult:
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
        return UnlockResult(InstalledBootState.WAIT_LOGIN)
    # A remote unlock is not the end of the passphrase for every layout.
    # ZFSBootMenu carries the ssh daemon in its own image, so `zfs load-key`
    # over that session unlocks the pool ZBM is reading and nothing else: the
    # system initramfs it boots asks again on the console. `zbm-unlock` was
    # marked unlocked, its `dracut-pre-mount` answered `Key load error:
    # Incorrect key provided for 'zpcala'` three times, and the harness typed
    # a login name at emergency mode for 93 minutes. The loop below answers
    # whichever prompt actually appears, so a machine that needs nothing more
    # reads its login prompt on the first look and returns just as it did.
    if not remotely_unlocked and installation.bootloader.firmware is BootFirmware.BIOS:
        time.sleep(GRUB_PROMPT_SECONDS)
        guest.send_keys([*keys_for(DISK_PASSPHRASE), "ret"])
    settle = PASSWORD_ECHO_OFF_AFTER
    for _ in range(UNLOCK_TRIES):
        try:
            said = link.observe(
                "|".join(
                    (
                        PASSPHRASE_PROMPT,
                        *LOGIN_PROMPTS,
                        *(re.escape(one) for one in INITRAMFS_GAVE_UP),
                    )
                ),
                timeout=BOOT_PATIENCE,
            )
        except (ConsoleTimeout, ConsoleClosed) as error:
            return UnlockResult(
                InstalledBootState.WAIT_LOGIN,
                f"the encrypted disk asked for nothing and booted nowhere: {error}"[:200],
            )
        if any(one.encode() in said for one in LOGIN_PROMPTS):
            return UnlockResult(InstalledBootState.LOGIN_READY)
        if any(one.encode() in said for one in INITRAMFS_GAVE_UP):
            return UnlockResult(
                InstalledBootState.WAIT_LOGIN,
                (
                    "the initramfs did not mount the root; the console held "
                    f"{said[-INITRAMFS_SCREEN_BYTES:]!r}"
                )[:VERDICT_BYTES],
            )
        # The same race the installed system's login lost: the prompt turns
        # the echo off with `TCSAFLUSH`, which discards whatever was typed
        # before it. `zbm-unlock` answered twice and ZFSBootMenu said `Key
        # load error: Incorrect key provided` both times.
        time.sleep(settle)
        settle += PASSWORD_ECHO_BACKOFF
        try:
            link.respond(DISK_PASSPHRASE)
        except ConsoleClosed as error:
            return UnlockResult(
                InstalledBootState.WAIT_LOGIN,
                f"the disk passphrase delivery is unknown: {error}"[:200],
            )
    return UnlockResult(
        InstalledBootState.WAIT_LOGIN,
        "the disk kept asking for a passphrase; it is not the one installed",
    )


#: The ordinary install a conversion job runs first. Plain by necessity: the
#: conversion refuses a root below LUKS, LVM or mdraid by name.
CONVERSION_BASE: Final[str] = "vm-xfs.toml"


def convert_and_check(
    guest: Guest,
    link: Reconnecting,
    log: Path,
    fixture: Path,
    address: str,
    watch: "Watchdog",
) -> str:
    """Convert the machine this run just installed, then read it back.

    The driver CD is still attached, so the installer that produced this system
    is the one that converts it. Answers an empty string when the converted
    machine carries what the conversion asked for.
    """
    installation = load(fixture)
    wait_for_network(link, guest.vmid, address)
    # Before the conversion, one sentinel on each side of the swap: `/home` is
    # the path an operator has data on and has to survive, and `/etc` is
    # replaced rather than merged, so a file put there has to be gone. The
    # cluster row said the converted machine carried what the conversion asked
    # for without anything having looked at either.
    link.run(f"printf '%s\\n' {HOME_MARKER} > {HOME_MARKER_PATH}")
    link.run(f"printf '%s\\n' {ETC_MARKER} > {ETC_MARKER_PATH}")
    link.run(FIND_DRIVER)
    link.run(f"mkdir -p {RESULT_DIR}")
    link.wait_for(
        f"{{ sh /mnt/driver/install.sh --config fixtures/{fixture.name}; "
        f"echo $? > {RESULT_DIR}/install.rc; }} 2>&1 | tee {RESULT_DIR}/install.txt",
        timeout=RUN_CEILING,
        idle=INSTALL_IDLE,
        watch=watch,
    )
    files = collect(guest, link, log)
    # Prefixed: the names are the same as the install that produced this
    # machine, and writing them again left only the conversion's log beside a
    # run that has two.
    keep_results(log, {f"convert.{name}": content for name, content in files.items()})
    code = files.get("install.rc", b"").strip()
    if code != b"0":
        return f"the conversion exited {code!r}"
    # Asked before the reboot: a guest that drops to `grub rescue>` for a
    # missing module answers nothing afterwards.
    for check in before_the_reboot(installation):
        said = link.expect_output(check.command, timeout=120.0)
        if re.search(check.pattern.encode(), said) is None:
            return (
                f"the conversion left a bootloader that cannot start: {check.name} "
                f"answered {said[-ANSWER_BYTES:]!r}"
            )[:VERDICT_BYTES]
    # The same reader as the first install: a converted machine that reaches a
    # login prompt with the old hostname booted the system it was replacing.
    return boot_and_check(
        guest, link, log, installation, extra=(HOME_MARKER_CHECK, ETC_MARKER_CHECK)
    )


def boot_and_check(
    guest: Guest,
    link: Reconnecting,
    log: Path,
    installation: InstallConfig,
    remote_key: Path | None = None,
    remote_host: str = "127.0.0.1",
    extra: Sequence[InstalledCheck] = (),
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
    # termproxy has no scrollback. Attach first, then reset so this connection
    # sees the GRUB prompt from its beginning. Sending the usual empty line
    # here submitted an empty GRUB passphrase before the reader was ready.
    link.reopen(solicit_prompt=False)
    guest.reset()
    remotely_unlocked = False
    unlock = installation.kernel.remote_unlock
    if unlock.enabled:
        if remote_key is None:
            return "remote unlock is enabled but the harness has no key"
        try:
            remote_unlock(remote_key, unlock.port, installation, host=remote_host)
        except RuntimeError as error:
            # The unlock is attempted over the network and returns before
            # anything reads the console, so `no ssh daemon on port 2222` was
            # the whole of what a two-hour run produced: a guest that never
            # left GRUB and one whose initramfs came up without an address read
            # identically. The screen tells them apart.
            screen = link.console.snapshot(UNLOCK_SCREEN_PATIENCE)
            held = repr(screen[-UNLOCK_SCREEN_BYTES:]) if screen else "nothing"
            return f"remote unlock failed: {error}; the console held {held}"[:VERDICT_BYTES]
        remotely_unlocked = True
        print("installed root unlocked by remote SSH session", flush=True)
    unlocked = _unlock(guest, link, installation, remotely_unlocked)
    if unlocked.refused:
        return unlocked.refused
    missed_the_prompt = ""
    if unlocked.state is InstalledBootState.WAIT_LOGIN:
        try:
            # Unsolicited: the empty line a reconnect used to send is counted by
            # agetty as one of its attempts, and `vm-lvm` came back with
            # `Maximum number of tries exceeded (5)` after two reconnects where
            # the same fixture passed with one and no solicit.
            link.observe(rf"{'|'.join(LOGIN_PROMPTS)}", timeout=BOOT_PATIENCE)
        except (ConsoleTimeout, ConsoleClosed) as error:
            # Not fatal: agetty prints `login:` once, so a console reopened past
            # it never sees the prompt again, while `_name_the_user` types a
            # name and waits for either prompt and gets in anyway. Kept for the
            # verdict, which otherwise says nothing about what the guest showed.
            missed_the_prompt = (
                f"the installed system did not reach a login prompt: {error}; "
                f"the console held {_seen_since(link, b'')}"
            )[:VERDICT_BYTES]
    try:
        refused = _log_in(link, INSTALLED_PASSWORD)
    except ConsoleClosed as error:
        return f"installed login response delivery is unknown: {error}"[:200]
    if refused:
        return f"{missed_the_prompt}; {refused}"[:VERDICT_BYTES] if missed_the_prompt else refused

    for name, command, wanted in [
        *_asked_for(installation),
        # What a stub on a machine that booted holds, asked of every UEFI
        # GRUB install: the conversion is the only path that ever asked, and
        # a rule written without the healthy answer refused all of them.
        *((one.name, one.command, one.pattern) for one in after_the_boot(installation)),
        *((one.name, one.command, one.pattern) for one in extra),
    ]:
        said = link.expect_output(command, timeout=120.0)
        if said != wanted.encode() and re.search(wanted.encode(), said) is None:
            # With the answer, not only the pattern: `greetd config: the
            # installed system does not say '(?ms)...'` is the same verdict
            # whether the file was never written, was written and still names
            # `agreety`, or does not exist at all.
            return (
                f"{name}: the installed system does not say {wanted!r}; "
                f"it said {said[-ANSWER_BYTES:]!r}"
            )[:VERDICT_BYTES]
    return ""


def keep_results(log: Path, files: Mapping[str, bytes]) -> None:
    """Write beside the log what the guest handed back.

    Only `install.rc` was read and the rest was dropped, `install.jsonl` with
    it: the record of which packages came from a binary host, which were
    compiled and why each degradation happened existed in the guest, crossed
    the console, and was never written anywhere.
    """
    for name, content in files.items():
        beside = log.with_name(f"{log.stem}.{Path(name).name}")
        try:
            beside.write_bytes(content)
        except OSError as error:
            print(f"{beside} was not written: {error}", file=sys.stderr)


def collect(guest: Guest, link: "Reconnecting", log: Path) -> dict[str, bytes]:
    """Read the result archive back, reopening the console if it has gone.

    A `termproxy` session does not survive an install: one that ran 36 minutes
    was dropped in the second after the installer printed `installed 53
    operations`, and the run was recorded as a failure although everything it
    was testing had worked. The guest is still up and the archive is still on
    it, so the answer is another console, not another install.
    """
    last: Exception | None = None
    link.set_buffer_limit(RESULT_BUFFER_BYTES)
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
            link.set_buffer_limit(RESULT_BUFFER_BYTES)
    raise ResultError(f"the results could not be read back: {last}")


#: How long the closing path waits for a worker to notice its guest was
#: stopped. The worker is blocked reading a console, and closing that console
#: is what wakes it, so this covers the read timeout and not an install.
ABANDON_PATIENCE: Final[float] = 120.0


#: What `termproxy` leaves in the console when it reached the guest's node
#: over ssh and that connection went away. It names the node, not the guest:
#: `10.31.0.200` to `.205` are the six nodes, and guests start at `.150`.
PROXY_DROPPED: Final[re.Pattern[str]] = re.compile(
    r"(?:Read from remote host (?P<host>\S+?): Connection reset"
    r"|ssh: connect to host (?P<refused>\S+) port 22"
    r"|client_loop: send disconnect)"
)

#: How much of the log's tail carries the reason a console went away.
PROXY_TAIL_BYTES: Final[int] = 4096


def console_proxy_dropped(log: Path) -> str:
    """Say so when the node's console proxy went away rather than the guest.

    A console that reaches another node runs over node-to-node ssh, and when
    that ends the guest's log carries ssh's own words with no `| ` prefix.
    Three guests on `infra-node3` were failed for a marker that never arrived
    while their installs were running perfectly well.
    """
    try:
        with log.open("rb") as reading:
            reading.seek(0, 2)
            reading.seek(max(0, reading.tell() - PROXY_TAIL_BYTES))
            tail = reading.read().decode("utf-8", "replace")
    except OSError:
        return ""
    found = PROXY_DROPPED.search(tail)
    if found is None:
        return ""
    host = found.group("host") or found.group("refused") or "the node"
    return f"the node's console proxy went away: {found.group(0)} ({host})"


def _abandon(
    inflight: dict[str, Running], running: dict[str, threading.Thread]
) -> None:
    """Stop and remove every guest still running when the schedule ends.

    The workers are daemon threads, so one wedged on a console cannot hold the
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
    # A timed-out worker no longer owns cleanup: destroy establishes the
    # terminal guest state before the scheduler releases its reservation.
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
    # The workers are daemons, so what guarantees no guest outlives the process
    # is here rather than in a thread: every guest this schedule built, not only
    # the ones a job still counts as running. `destroy` checks the tag and the
    # nonce, and returns on a guest that is already gone.
    for name, job in scheduled.items():
        if job.execution is None:
            continue
        try:
            job.execution.guest.destroy()
        except ProxmoxError as error:
            print(f"  {name} outlived the schedule: {error}", file=sys.stderr)


#: What each bootloader needs before it will say anything on the serial line.
#: `append_to_cmdline` holds GRUB's menu and types into it; the other two are
#: never on screen at all, so the parameters go into what they read.
class SerialRoute(Enum):
    """Where the serial parameters were written, for the report to name."""

    GRUB_MENU = "grub-menu"
    LOADER_ENTRIES = "loader-entries"
    ZFSBOOTMENU = "zfsbootmenu"
    GRUB_CONFIG = "grub-config"
    NOTHING_FOUND = "nothing-found"


def make_the_installed_system_speak(
    link: "Reconnecting", extra: str = EXTRA_CMDLINE
) -> SerialRoute:
    """Put the serial parameters where the installed bootloader will read them.

    Run on the medium, before `boot_from_disk`. Four installs were checked with
    GRUB's menu editor and all four answered `GRUB never opened its editor`:
    two were running ZFSBootMenu, one systemd-boot, and the BIOS one writes its
    menu to the VGA console. GRUB stays with the editor because holding the
    menu is the only way to reach an entry that is already written.
    """
    pool = link.expect_output("zpool import 2>&1 | sed -n 's/^ *pool: //p' | head -1").strip()
    if pool:
        name = pool.decode(errors="replace")
        # `-N` leaves the datasets unmounted: the property is on the pool, and
        # mounting a root over the live one is what the export then fights.
        link.run(f"zpool import -N -f {name}")
        # On each boot environment, not on the dataset above them: the
        # installer writes an empty `org.zfsbootmenu:commandline` on
        # `<pool>/ROOT/<name>` itself, and `zfs get -o source` reads that back
        # as `local`, so a value set on the parent is never inherited. Read on
        # `gi-u7`, where the parent carried the parameters and the guest was
        # silent through three boots.
        for environment in _boot_environments(link, name):
            link.run(f"zfs set org.zfsbootmenu:commandline='{extra}' {environment}")
        link.run(f"zpool export {name}")
        return SerialRoute.ZFSBOOTMENU
    esp = link.expect_output(
        "blkid -t TYPE=vfat -o device 2>/dev/null | head -1"
    ).strip()
    if esp:
        where = esp.decode(errors="replace")
        link.run(f"mkdir -p /mnt/esp && mount {where} /mnt/esp")
        entries = link.expect_output(
            "ls /mnt/esp/loader/entries/*.conf 2>/dev/null | head -1"
        ).strip()
        if entries:
            # Appended to the line rather than added as a second `options`:
            # systemd-boot reads only the first one and the rest are ignored.
            link.run(
                "sed -i '/^options/ s|$| " + extra + "|' /mnt/esp/loader/entries/*.conf"
            )
            link.run("umount /mnt/esp")
            return SerialRoute.LOADER_ENTRIES
        link.run("umount /mnt/esp")
    if _grub_config_edited(link, extra):
        return SerialRoute.GRUB_CONFIG
    return SerialRoute.NOTHING_FOUND


def _boot_environments(link: "Reconnecting", pool: str) -> list[str]:
    """Every dataset directly under `<pool>/ROOT`, which is what ZFSBootMenu
    offers and reads its properties from."""
    said = link.expect_output(f"zfs list -H -o name -d 1 -r {pool}/ROOT 2>/dev/null")
    named = [one.strip() for one in said.decode(errors="replace").split("\n") if one.strip()]
    return [one for one in named if one != f"{pool}/ROOT"]


def _grub_config_edited(link: "Reconnecting", extra: str) -> bool:
    """Put the parameters in `grub.cfg` itself, for a menu nobody can type into.

    A BIOS GRUB draws its menu on the VGA console, so `append_to_cmdline` has
    nothing to hold: `gi-s2a` answered zero bytes where `gi-t1b` answered a
    whole boot. The kernel still writes wherever `console=` sends it, and that
    comes from the `linux` lines the installer already generated.
    """
    for named in _rootish_devices(link):
        device, _, kind = named.rpartition(":")
        if not device:
            continue
        opened = device
        if kind == "crypto_LUKS":
            opened = "/dev/mapper/speak"
            link.run(f"printf '%s' {TUI_PASSWORD} | cryptsetup open {device} speak")
        for options in ("", "-o subvol=@"):
            where = f"{options} {opened}".strip()
            link.run(f"mkdir -p /mnt/root && mount {where} /mnt/root 2>/dev/null; true")
            found = link.expect_output(
                "ls /mnt/root/boot/grub/grub.cfg 2>/dev/null | head -1"
            ).strip()
            if found:
                # Every `linux` line, because GRUB writes one per kernel and
                # the operator boots whichever the menu defaults to.
                link.run(
                    "sed -i '/^[[:space:]]*linux/ s|$| " + extra + "|' "
                    "/mnt/root/boot/grub/grub.cfg"
                )
                link.run("umount /mnt/root")
                if opened != device:
                    link.run("cryptsetup close speak")
                return True
            link.run("umount /mnt/root 2>/dev/null; true")
        if opened != device:
            link.run("cryptsetup close speak")
    return False


#: Every spec uses one password, so the harness knows it without being told.
TUI_PASSWORD: Final[str] = "testtest"


def _rootish_devices(link: "Reconnecting") -> list[str]:
    """Partitions that could hold a root, newest-looking first.

    `blkid` names the type as well, because a LUKS container has to be
    unlocked before anything can be mounted from it and the two cases cannot
    be told apart from the device name.
    """
    said = link.expect_output(
        "blkid -o export 2>/dev/null | awk -F= '/^DEVNAME=/{d=$2} /^TYPE=/{print d\":\"$2}' "
        "| grep -Ev ':(vfat|swap|iso9660|squashfs|zfs_member)$'"
    )
    return [one for one in said.decode(errors="replace").split("\n") if one.strip()]


def edit_the_menu_if_that_is_the_only_route(
    guest: Guest, link: "Reconnecting", route: SerialRoute
) -> bool:
    """Hold the bootloader's menu only where nothing else could be written.

    Asked unconditionally, the editor waits 120 seconds twice for a menu that
    will never come: `gi-u7` boots ZFSBootMenu and `gi-u5` draws systemd-boot's
    own menu, and the 90 KB that guest sent were that menu redrawing. Answers
    whether the menu was edited.
    """
    if route is not SerialRoute.NOTHING_FOUND:
        return False
    _edit_uefi_cmdline(guest, link)
    return True


def _edit_uefi_cmdline(guest: Guest, link: "Reconnecting") -> None:
    """Add the serial parameters to the medium's entry, once more if the
    console said nothing at all.

    A silent console is not a stubborn GRUB. `vm-xfs` ended at 2.2 minutes
    with `the console delivered 0 bytes while the editor was asked for over
    120s`, having passed the same fixture in the round before: the termproxy
    attached, the log holds `starting serial terminal on interface serial0`
    and then nothing. The BIOS path has had a second boot since the beginning
    and this one had none.
    """
    try:
        append_to_cmdline(link, EXTRA_CMDLINE)
        return
    except GrubNotReadable as error:
        if SILENT_EDITOR not in str(error):
            raise
        print(f"the console said nothing to the editor, booting once more: {error}", flush=True)
    guest.reset()
    link.reopen(solicit_prompt=False)
    append_to_cmdline(link, EXTRA_CMDLINE)


#: What `_editor_screen` says when the console delivered nothing rather than
#: the wrong thing. Matched rather than re-derived, so the two sentences
#: cannot drift apart into a retry that never fires.
SILENT_EDITOR: Final[str] = "the console delivered"


def _edit_bios_cmdline(guest: Guest, link: "Reconnecting") -> None:
    """Read the menu when GRUB speaks on the serial port, and otherwise ask
    the medium's own auto-login for a shell on that port.

    SeaBIOS hands over to a GRUB that on some media writes only to VGA. Typing
    at that menu blind never landed: `vm-bios`, `vm-bios-luks` and `ext4-bios`
    ended at `the kernel never spoke` in every round that tried it, and
    run66's two took fourteen minutes each to do so. A screenshot taken
    through the QEMU monitor says why — three seconds after the menu the live
    system is logged in on the VGA console, so the keys reached a shell and
    `bash: cserial: command not found` is what the screen held.

    So the command line is left alone. The medium logs root in by itself, and
    one typed line moves a getty onto the serial port, which is readable from
    the first attempt.
    """
    try:
        append_to_cmdline(link, EXTRA_CMDLINE)
        return
    except GrubNotReadable:
        pass
    # The unreadable menu counted down while the serial wait ran, so the guest
    # is already past it; the attempt below is timed from a fresh boot.
    guest.reset()
    link.reopen(solicit_prompt=False)
    open_a_serial_shell_blind(guest, link)


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
        working = held.watch.moved()
        # Asked whenever the log stopped growing, not only when the guest also
        # looks idle: a node whose proxy is refusing leaves the socket open and
        # silent while the disk stays busy, so `moved` is true and this is the
        # only shape that names it. Read from the log's tail, which a console
        # that recovered scrolls past on its own.
        dropped = console_proxy_dropped(held.watch.log) if held.watch.silent else ""
        if working and not dropped:
            continue
        if not held.watch.stuck and not dropped:
            print(
                f"… {name} quiet for {held.watch.strikes * WATCH_EVERY / 60:.0f}m",
                flush=True,
            )
            continue
        print(f"! {name} {dropped or 'stopped writing'}; ending it", flush=True)
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


#: qemu's user-mode network, where `10.0.2.2` is the machine running qemu and
#: `10.0.2.3` is its resolver. A cluster guest is bridged and has neither, so a
#: fixture naming one of them can only run under `tests/vm/run.py`. Dispatching
#: `vm-proxy` and `vm-proxy-http` here anyway cost 116.0 and 112.8 minutes,
#: both spent timing out on a proxy that was never going to answer.
USER_MODE_NETWORK: Final[str] = "10.0.2.0/24"


#: An image-mode install needs a filesystem to write its image onto, and on a
#: live medium every writable path is RAM. `tests/vm/run.py` makes one on the
#: spare target disk and mounts it; this runner does not, and `vm-image` died
#: at 2.0 minutes with `EXT4-fs … error -5` and `No space left on device`,
#: having filled the guest's own memory.
def _needs_a_scratch_filesystem(config: InstallConfig) -> str:
    """The image path this runner cannot provide a disk for, or empty."""
    return config.disk.image if config.disk.mode is DiskMode.IMAGE else ""


def _needs_user_mode_networking(config: InstallConfig) -> str:
    """The address that only the local hypervisor provides, or empty."""
    if not config.proxy.host:
        return ""
    network = ipaddress.ip_network(USER_MODE_NETWORK)
    try:
        host = ipaddress.ip_address(config.proxy.host)
    except ValueError:
        return ""
    return config.proxy.host if host in network else ""


def fixtures(names: list[str]) -> list[Job]:
    found: list[Job] = []
    for name in names:
        path = REPOSITORY / "tests" / "fixtures" / f"{name}.toml"
        if not path.is_file():
            raise SystemExit(f"no fixture named {name} at {path}")
        config: InstallConfig = load(path)
        needs_scratch = _needs_a_scratch_filesystem(config)
        if needs_scratch:
            raise SystemExit(
                f"{name} writes its image to {needs_scratch}, and only "
                "tests/vm/run.py mounts a disk there: on this runner the path "
                "is the medium's own tmpfs and the guest fills its memory"
            )
        local_only = _needs_user_mode_networking(config)
        if local_only:
            raise SystemExit(
                f"{name} reaches {local_only}, which only qemu's user-mode network "
                "provides: run it with tests/vm/run.py, not on the cluster"
            )
        convert_to: Path | None = None
        if config.disk.mode is DiskMode.IN_PLACE:
            # A conversion needs a machine to convert, so the job installs one
            # first and this fixture runs on what that produced.
            convert_to = path
            path = REPOSITORY / "tests" / "fixtures" / CONVERSION_BASE
            config = load(path)
        found.append(
            Job(
                name=name,
                fixture=path,
                uefi=config.bootloader.firmware.value != "bios",
                disks=max(1, len(config.disk.graph.of_type(Existing))),
                heavy=_compiles(config),
                remote_unlock=_requests_remote_unlock(path),
                convert_to=convert_to,
            )
        )
    return found


def _leave_on_a_signal() -> None:
    """Unwind on a signal so the closing path runs.

    Python's default handler for SIGTERM ends the process without unwinding,
    so no `finally` runs: `kill` on the scheduler left eight guests on the
    cluster with the closing path that removes them never reached.

    SIGINT is claimed too. A shell without job control hands a background
    campaign `SIG_IGN` for it, CPython keeps an inherited ignore, and
    `kill -INT` on the schedule then does nothing at all.
    """

    def raised(number: int, frame: object) -> None:
        raise KeyboardInterrupt(f"signal {number}")

    signal.signal(signal.SIGTERM, raised)
    signal.signal(signal.SIGHUP, raised)
    signal.signal(signal.SIGINT, raised)


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
        "--site",
        default="",
        help="pin every mirror to one site by its key in model/mirrors.py",
    )
    parser.add_argument(
        "--skip-node",
        action="append",
        default=[],
        metavar="NAME",
        help="take no guests on this node, before it has cost a guest to learn",
    )
    parser.add_argument(
        "--allow-node",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "use a node named in KNOWN_BAD_NODES anyway: "
            f"{' '.join(sorted(KNOWN_BAD_NODES)) or 'none are named'}"
        ),
    )
    parser.add_argument(
        "--distfiles",
        default="",
        help=(
            "replace GENTOO_MIRRORS with this one address, which the stage3 and "
            "emerge-webrsync then follow; an address needs no resolver"
        ),
    )
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
        args.site,
        args.distfiles,
        args.skip_node,
        args.allow_node,
    )
    passed = [
        one
        for one in outcomes
        if one.verdict is Verdict.OK and trusted_revision(one.revision)
    ]
    print(f"\n{len(passed)}/{len(jobs)} passed")
    # A guest that could not be removed does not make its install a failure,
    # but a summary that says only `10/10 passed` hides a machine still
    # holding a node's memory: run114 left `ext2` on `infra-node4` for hours
    # after `qmdestroy` answered `rbd error: listing images failed`.
    for one in outcomes:
        if not one.removed:
            print(f"  still on the cluster: {one.name}", file=sys.stderr)
    for one in outcomes:
        if one.verdict is not Verdict.OK or not trusted_revision(one.revision):
            # A job refused before a guest existed has no log, and `(None)`
            # in the summary reads as a path that could be opened.
            where = f" ({one.log})" if one.log is not None else ""
            print(f"  {one.verdict.value} {one.name}: {one.detail}{where}")
    # Against what was asked for, not against what came back: a worker that
    # died without answering left its job with no outcome at all, and a run
    # that collected fewer results than it dispatched still exited 0.
    missing = sorted({one.name for one in jobs} - {one.name for one in outcomes})
    for name in missing:
        print(f"  no result {name}", file=sys.stderr)
    return 0 if len(passed) == len(jobs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
