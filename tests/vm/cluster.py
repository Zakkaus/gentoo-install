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
import queue
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Final, Protocol

from gentoo_install.model.config import InstallConfig
from gentoo_install.model.device import Existing
from gentoo_install.model.config import MirrorRegion
from gentoo_install.model.parse import load
from gentoo_install.model.serialise import to_toml
from .console import ConsoleClosed, ConsoleTimeout, SerialConsole
from .driver import build as build_driver
from .proxmox import Api, Guest, GuestSpec, Node, ProxmoxError, append_to_cmdline
from .results import ResultError, console_command, read_console

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

#: What a guest is given. Four cores is a node's whole complement, so three
#: leaves the node able to answer the API while a build runs.
GUEST_MEMORY_MIB: Final[int] = 6144
GUEST_CORES: Final[int] = 3
TARGET_GIB: Final[int] = 40

#: Left free on every node. A node with nothing spare stops answering, and the
#: cluster runs other people's machines.
NODE_HEADROOM_BYTES: Final[int] = 4 * 1024**3

#: How often the watchdog looks, and how many quiet looks end a guest. Ten
#: minutes because a stage3 extract and a kernel build both write progress more
#: often than that, and half an hour of silence is not a slow mirror.
WATCH_EVERY: Final[float] = 600.0
WATCH_STRIKES: Final[int] = 3

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


@dataclass
class Watchdog:
    """Whether a guest is still producing output."""

    log: Path
    strikes: int = 0
    _seen: int = field(default=0, init=False)

    def moved(self) -> bool:
        size = self.log.stat().st_size if self.log.exists() else 0
        if size > self._seen:
            self._seen = size
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


def rewrite_fixtures(jobs: list[Job], into: Path, region: MirrorRegion) -> Path:
    """Write each fixture out again with its mirror region replaced.

    Through the parser and the writer, not a text substitution: a fixture that
    cannot survive the round trip is a defect worth failing on here rather than
    an hour into an install.

    The cluster is in China, and a `global` fixture syncs the tree from
    `github.com/gentoo-mirror/gentoo.git`, which answered `SSL_read:
    unexpected eof while reading` there. Running the set against `cn` is both
    what works and what the operators of that network actually use.
    """
    into.mkdir(parents=True, exist_ok=True)
    for job in jobs:
        config = load(job.fixture)
        moved = replace(
            config, portage=replace(config.portage, mirrors=replace(config.portage.mirrors, region=region))
        )
        (into / job.fixture.name).write_text(to_toml(moved))
    return into


def free_slots(api: Api) -> list[Node]:
    """Nodes with room for one more guest, most free first."""
    need = GUEST_MEMORY_MIB * 1024**2 + NODE_HEADROOM_BYTES
    return [one for one in api.nodes() if one.free_bytes >= need]


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
) -> Outcome:
    """Build a guest, install into it, read the result, delete the guest."""
    started = time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    log = workdir / f"{job.name}.log"
    guest = Guest(
        api=api,
        node=node,
        vmid=api.free_vmid(),
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
    watch = Watchdog(log=log)
    if inflight is not None:
        inflight[job.name] = Running(guest=guest, watch=watch)
    try:
        guest.create()
        guest.start()
        console = SerialConsole(guest.console(), log.open("wb"))
        # Reset with the console attached: termproxy forwards only what arrives
        # after it, and the firmware is finished before it gets there.
        guest.reset()
        append_to_cmdline(console, EXTRA_CMDLINE)
        console.expect(r"livecd .*#|localhost .*#", timeout=900.0)
        # The guest's own resolver is left alone. A local run pins one because
        # slirp reads the host's `/etc/resolv.conf` once at startup; the
        # cluster hands out a real configuration, and it is IPv6 with DNS64.
        # Writing IPv4 resolvers over it left every mirror unreachable:
        # `Failed to connect to mirrors.tuna.tsinghua.edu.cn:443 after 111 ms`.
        console.run("mkdir -p /mnt/driver")
        console.run("mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver")
        console.run(f"mkdir -p {RESULT_DIR}")
        # tee, not a redirect: the serial console is the only way to watch a
        # run that takes half an hour, and it is what the watchdog reads to
        # tell a slow mirror from a dead guest.
        console.run(
            f"{{ sh /mnt/driver/install.sh --config fixtures/{job.fixture.name}; "
            f"echo $? > {RESULT_DIR}/install.rc; }} 2>&1 | tee {RESULT_DIR}/install.txt",
            timeout=RUN_CEILING,
        )
        console.run(f"cp /run/gentoo-install/install.jsonl {RESULT_DIR}/ 2>/dev/null || true")
        console.send(console_command(RESULT_DIR))
        said = console.snapshot(120.0)
        files = read_console(said)
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


def run(
    jobs: list[Job],
    workdir: Path,
    limit: int = 0,
    stamp: int = 0,
    region: MirrorRegion = MirrorRegion.CN,
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
        fixtures=rewrite_fixtures(jobs, workdir / "fixtures", region),
    )
    driver = f"gi-driver-{stamp}.iso"
    medium, urls = current_minimal()
    prepared: set[str] = set()
    done: queue.Queue[Outcome] = queue.Queue()
    waiting = list(jobs)
    running: dict[str, threading.Thread] = {}
    inflight: dict[str, Running] = {}
    finished: list[Outcome] = []

    def one(node: str, job: Job) -> None:
        done.put(install_one(api, node, job, driver, workdir, inflight))

    try:
        while waiting or running:
            slots = free_slots(api)
            if limit:
                slots = slots[: max(0, limit - len(running))]
            while waiting and slots:
                node = slots.pop(0)
                if node.name not in prepared:
                    prepare(api, node.name, medium, urls, driver_path, driver)
                    prepared.add(node.name)
                job = waiting.pop(0)
                if job.iso == DEFAULT_ISO:
                    job = replace(job, iso=medium)
                thread = threading.Thread(target=one, args=(node.name, job), daemon=True)
                running[job.name] = thread
                thread.start()
                print(f"→ {job.name} on {node.name} ({len(waiting)} waiting)", flush=True)
            try:
                # Collected one at a time, never as a set: a fixture that takes
                # an hour must not hold back one that took six minutes.
                outcome = done.get(timeout=WATCH_EVERY)
            except queue.Empty:
                _sweep(inflight)
                continue
            finished.append(outcome)
            running.pop(outcome.name, None)
            print(
                f"{outcome.verdict.value:6} {outcome.name:34} {outcome.seconds / 60:5.1f}m "
                f"{outcome.detail}",
                flush=True,
            )
    finally:
        for name in prepared:
            api.remove_iso(name, driver)
    return finished


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
        default=MirrorRegion.CN.value,
        help="which mirror region every fixture is rewritten to use",
    )
    args = parser.parse_args(argv)

    outcomes = run(
        fixtures(args.fixtures),
        args.workdir,
        args.limit,
        int(time.time()),
        MirrorRegion(args.region),
    )
    passed = [one for one in outcomes if one.verdict is Verdict.OK]
    print(f"\n{len(passed)}/{len(outcomes)} passed")
    for one in outcomes:
        if one.verdict is not Verdict.OK:
            print(f"  {one.verdict.value} {one.name}: {one.detail} ({one.log})")
    return 0 if len(passed) == len(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
