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
import queue
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Final, Protocol

from gentoo_install.model.config import InstallConfig
from gentoo_install.model.device import Existing, Luks, ZfsPool
from gentoo_install.model.config import MirrorRegion, Sync
from gentoo_install.exec.config import load
from gentoo_install.model.serialise import to_toml
from .console import ConsoleClosed, ConsoleTimeout, SerialConsole
from .driver import build as build_driver
from .proxmox import (
    Api,
    Guest,
    GuestSpec,
    Node,
    ProxmoxError,
    Line,
    append_to_cmdline,
    append_to_cmdline_blind,
)
from .results import CONSOLE_CLOSE, ResultError, console_command, read_console

REPOSITORY: Final[Path] = Path(__file__).resolve().parents[2]
WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/cluster"

#: Where the guest gathers what the run produced before it is read back.
RESULT_DIR: Final[str] = "/tmp/gentoo-install-results"

#: Where the media come from, in the order they are tried. The cluster is in
#: China, so a node reaches these far faster than an upload from the
#: workstation would, and the bytes never cross the workstation's link.
#:
#: `mirrors.ustc.edu.cn` is not among them: it answers `403 Forbidden` to
#: Proxmox's downloader, which is wget, while serving the same URL to anything
#: else. The installer reads it happily; only this path has to avoid it.
MIRRORS: Final[tuple[str, ...]] = (
    "https://mirrors.tuna.tsinghua.edu.cn/gentoo",
    "https://mirror.nju.edu.cn/gentoo",
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
STAGGER: Final[float] = 8.0

#: How long the schedule waits before looking at capacity again while jobs are
#: still queued. A node's free memory lags what is actually running on it, so
#: the first look after a guest is deleted can still read it as full: with only
#: the watchdog's interval to fall back on, five free slots sat unused for ten
#: minutes with eighteen jobs waiting.
POLL_WHILE_QUEUED: Final[float] = 20.0

#: Nothing this harness runs takes three hours, and a run that does is holding
#: a node's memory rather than testing anything.
RUN_CEILING: Final[float] = 3 * 3600.0


class Verdict(Enum):
    """How one guest ended. `STUCK` is separate from `FAIL` on purpose: a
    failure is read in the log, and a guest that stopped writing is read on
    the screen, so the two are chased differently."""

    OK = "ok"
    FAIL = "FAIL"
    STUCK = "STUCK"
    ERROR = "ERROR"


@dataclass
class Outcome:
    name: str
    verdict: Verdict
    seconds: float
    detail: str = ""
    log: Path | None = None


@dataclass
class Job:
    """One fixture to install and check."""

    name: str
    fixture: Path
    iso: str = DEFAULT_ISO
    uefi: bool = True
    disks: int = 1


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
    counters: Callable[[], int]
    strikes: int = 0
    _seen: int = field(default=0, init=False)
    _moved: int = field(default=0, init=False)

    def moved(self) -> bool:
        size = self.log.stat().st_size if self.log.exists() else 0
        traffic = self.counters()
        talking = size > self._seen
        working = traffic - self._moved >= QUIET_BYTES
        self._seen = max(self._seen, size)
        self._moved = max(self._moved, traffic)
        if talking or working:
            self.strikes = 0
            return True
        self.strikes += 1
        return False

    @property
    def stuck(self) -> bool:
        return self.strikes >= WATCH_STRIKES


def current_minimal() -> tuple[str, tuple[str, ...]]:
    """The current minimal ISO's name and every URL that serves it."""
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
                return first.rsplit("/", 1)[-1], tuple(
                    f"{one}/{AUTOBUILDS}/{first}" for one in MIRRORS
                )
    raise SystemExit("no mirror named an install medium")


def prepare(
    api: Api, node: str, medium: str, urls: tuple[str, ...], driver_path: Path, driver: str
) -> None:
    """Put the medium and the driver CD on one node's `local` storage.

    `local` is per node, not shared: a guest built on a node without the medium
    is refused with `volume 'local:iso/...' does not exist`, which is what the
    first cluster run hit.
    """
    if medium not in api.isos(node):
        last = ""
        for url in urls:
            try:
                api.fetch_iso(node, url, medium)
                break
            except ProxmoxError as error:
                last = str(error)
        else:
            raise ProxmoxError(f"no mirror served {medium} to {node}: {last}")
    if driver not in api.isos(node):
        api.upload_iso(node, driver_path, driver)


#: The disk passphrase these runs use. Not a root password: zfs takes at least
#: eight characters, and a real install does not reuse one for the other.
DISK_PASSPHRASE: Final[str] = "install-disk"


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
    slots: list[Node] = []
    for node in api.nodes():
        room = node.free_bytes - NODE_HEADROOM_BYTES - held.get(node.name, 0) * need
        slots += [node] * max(0, int(room // need))
    return slots


class Stoppable(Protocol):
    """All the sweep needs of a guest. Stopping is what wakes the worker that
    is blocked reading its console; deleting is the worker's own job, and a
    sweep that deleted would race it."""

    def stop(self) -> None: ...


@dataclass
class Running:
    """A guest in flight, and what the watchdog needs to judge it."""

    guest: Stoppable
    watch: Watchdog


def install_one(
    api: Api,
    node: str,
    job: Job,
    driver: str,
    workdir: Path,
    inflight: dict[str, Running] | None = None,
    vmid: int = 0,
) -> Outcome:
    """Build a guest, install into it, read the result, delete the guest."""
    started = time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    log = workdir / f"{job.name}.log"
    guest = Guest(
        api=api,
        node=node,
        vmid=vmid or api.free_vmid(),
        spec=GuestSpec(
            name=f"gi-{job.name}"[:63],
            iso=job.iso,
            memory_mib=GUEST_MEMORY_MIB,
            cores=GUEST_CORES,
            target_gib=tuple(TARGET_GIB for _ in range(job.disks)),
            uefi=job.uefi,
            driver_iso=driver,
        ),
    )
    watch = Watchdog(log=log, counters=lambda: guest.transferred())
    if inflight is not None:
        inflight[job.name] = Running(guest=guest, watch=watch)
    try:
        guest.create()
        guest.start()
        log.write_bytes(b"")
        link = Reconnecting.to(guest, log)
        console = link.console
        # Reset with the console attached: termproxy forwards only what arrives
        # after it, and the firmware is finished before it gets there.
        guest.reset()
        if job.uefi:
            append_to_cmdline(link, EXTRA_CMDLINE)
        else:
            # SeaBIOS hands over to a GRUB that writes only to VGA, so the
            # serial log stops at `Welcome to GRUB!` and there is no menu to
            # read. The keys go through the API and the kernel appearing on
            # the console is what says the edit landed.
            append_to_cmdline_blind(guest, link, EXTRA_CMDLINE)
        link.expect(r"livecd .*#|localhost .*#", timeout=900.0)
        # The guest's own resolver is left alone. A local run pins one because
        # slirp reads the host's `/etc/resolv.conf` once at startup; the
        # cluster hands out a real configuration, and it is IPv6 with DNS64.
        # Writing IPv4 resolvers over it left every mirror unreachable:
        # `Failed to connect to mirrors.tuna.tsinghua.edu.cn:443 after 111 ms`.
        stage_passphrases(link, load(job.fixture))
        link.run("mkdir -p /mnt/driver")
        link.run("mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver")
        link.run(f"mkdir -p {RESULT_DIR}")
        # tee, not a redirect: the serial console is the only way to watch a
        # run that takes half an hour, and it is what the watchdog reads to
        # tell a slow mirror from a dead guest.
        # `wait_for`, not `run`: a console dropped mid-install is reopened and
        # listened to again, never handed the command a second time.
        link.wait_for(
            f"{{ sh /mnt/driver/install.sh --config fixtures/{job.fixture.name}; "
            f"echo $? > {RESULT_DIR}/install.rc; }} 2>&1 | tee {RESULT_DIR}/install.txt",
            timeout=RUN_CEILING,
        )
        files = collect(guest, link, log)
        code = files.get("install.rc", b"").strip()
        if code != b"0":
            return Outcome(job.name, Verdict.FAIL, time.monotonic() - started,
                           f"the installer exited {code!r}", log)
        return Outcome(job.name, Verdict.OK, time.monotonic() - started, log=log)
    except (ConsoleTimeout, ConsoleClosed) as error:
        verdict = Verdict.STUCK if watch.stuck else Verdict.FAIL
        return Outcome(job.name, verdict, time.monotonic() - started, str(error)[:300], log)
    except (ProxmoxError, ResultError, OSError) as error:
        return Outcome(job.name, Verdict.ERROR, time.monotonic() - started, str(error)[:300], log)
    finally:
        if inflight is not None:
            inflight.pop(job.name, None)
        try:
            guest.destroy()
        except ProxmoxError as error:
            # Said rather than raised: an undeleted guest holds a node's memory
            # and the operator has to know, but the result already exists.
            print(f"{job.name}: the guest was not removed: {error}", file=sys.stderr)


def answer_once(
    done: "queue.Queue[Outcome]",
    api: Api,
    node: str,
    job: Job,
    driver: str,
    workdir: Path,
    inflight: dict[str, Running],
    vmid: int = 0,
) -> None:
    """Run one job and put exactly one outcome on the queue, whatever happens.

    A worker that dies without answering leaves its name in the running set
    for ever and the schedule never ends: a `WebSocketError` out of a dropped
    console was outside the handled set, and a run sat idle for half an hour
    with an empty cluster and a job still queued.
    """
    try:
        done.put(install_one(api, node, job, driver, workdir, inflight, vmid))
    except BaseException as error:
        done.put(
            Outcome(job.name, Verdict.ERROR, 0.0, f"{type(error).__name__}: {error}"[:300])
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
    api = Api()
    workdir.mkdir(parents=True, exist_ok=True)
    # Packed: the ingress refuses the 1.4 MiB loose-file CD with `413`.
    driver_path = build_driver(
        workdir / "driver.iso",
        packed=True,
        fixtures=rewrite_fixtures(jobs, workdir / "fixtures", region, sync),
    )
    driver = f"gi-driver-{stamp}.iso"
    medium, urls = current_minimal()
    prepared: set[str] = set()
    done: queue.Queue[Outcome] = queue.Queue()
    waiting = list(jobs)
    running: dict[str, threading.Thread] = {}
    inflight: dict[str, Running] = {}
    #: Which node each running job was put on, so its slot is given back when
    #: the job answers.
    where: dict[str, str] = {}
    finished: list[Outcome] = []
    #: Handed out here, not in the worker. Two threads asking the cluster at
    #: the same moment both read 9304 as free and the second was refused with
    #: `VM 9304 already exists on node 'infra-node5'`.
    handed: set[int] = set()
    placed: Counter[str] = Counter()
    swept = time.monotonic()

    try:
        while waiting or running:
            slots = free_slots(api, placed)
            if limit:
                slots = slots[: max(0, limit - len(running))]
            while waiting and slots:
                node = slots.pop(0)
                if node.name not in prepared:
                    prepare(api, node.name, medium, urls, driver_path, driver)
                    prepared.add(node.name)
                job = waiting.pop(0)
                placed[node.name] += 1
                where[job.name] = node.name
                if job.iso == DEFAULT_ISO:
                    job = replace(job, iso=medium)
                vmid = api.free_vmid(frozenset(handed))
                handed.add(vmid)
                thread = threading.Thread(
                    target=answer_once,
                    args=(done, api, node.name, job, driver, workdir, inflight, vmid),
                    daemon=True,
                )
                running[job.name] = thread
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
                if time.monotonic() - swept >= WATCH_EVERY:
                    _sweep(inflight)
                    swept = time.monotonic()
                continue
            finished.append(outcome)
            running.pop(outcome.name, None)
            gone = where.pop(outcome.name, "")
            if gone:
                placed[gone] -= 1
            print(
                f"{outcome.verdict.value:6} {outcome.name:34} {outcome.seconds / 60:5.1f}m "
                f"{outcome.detail}",
                flush=True,
            )
    finally:
        for node_name in prepared:
            said = api.remove_iso(node_name, driver)
            if said:
                print(f"{driver} stayed on {node_name}: {said}", file=sys.stderr)
    return finished


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

    def expect(self, pattern: str, timeout: float) -> bytes:
        for attempt in range(self._tries):
            try:
                return self.console.expect(pattern, timeout)
            except ConsoleClosed:
                if attempt + 1 == self._tries:
                    raise
                self.reopen()
        raise ConsoleClosed("the console could not be reopened")

    def run(self, command: str, timeout: float = 120.0) -> None:
        for attempt in range(self._tries):
            token = next(self._marks)
            self.console.send(f"{command}; echo MARK_{token}_DONE")
            try:
                self.console.expect(rf"MARK_{token}_DONE", timeout)
                return
            except ConsoleClosed:
                if attempt + 1 == self._tries:
                    raise
                self.reopen()

    def wait_for(self, command: str, timeout: float) -> None:
        """Send a command once and wait for it however long it takes.

        Reconnecting does not re-send it: an install that is already running
        would be started a second time on a target it has half written.
        """
        token = next(self._marks)
        self.console.send(f"{command}; echo MARK_{token}_DONE")
        for attempt in range(self._tries):
            try:
                self.console.expect(rf"MARK_{token}_DONE", timeout)
                return
            except ConsoleClosed:
                if attempt + 1 == self._tries:
                    raise
                self.reopen()

    def send(self, line: str) -> None:
        self.console.send(line)

    def send_raw(self, keys: str) -> None:
        self.console.send_raw(keys)

    def snapshot(self, seconds: float) -> bytes:
        return self.console.snapshot(seconds)


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


def _sweep(inflight: dict[str, Running]) -> None:
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
            )
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", nargs="+", help="fixture names, without .toml")
    parser.add_argument("--limit", type=int, default=0, help="how many guests at once")
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

    outcomes = run(
        fixtures(args.fixtures),
        args.workdir,
        args.limit,
        int(time.time()),
        MirrorRegion(args.region),
        Sync(args.sync),
    )
    passed = [one for one in outcomes if one.verdict is Verdict.OK]
    print(f"\n{len(passed)}/{len(outcomes)} passed")
    for one in outcomes:
        if one.verdict is not Verdict.OK:
            print(f"  {one.verdict.value} {one.name}: {one.detail} ({one.log})")
    return 0 if len(passed) == len(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
