# SPDX-License-Identifier: GPL-2.0-or-later
"""Run the whole VM matrix unattended, in the order `docs/vm-campaign.md` sets.

    python3 -m tests.vm.campaign --stage blocking
    python3 -m tests.vm.campaign            # every stage, in order

Every stage runs its own configurations at once; the stages themselves are
ordered, and a failure in the first one stops the rest unless `--keep-going`
says otherwise. How many run at once is decided by `_room_for_a_guest` against
the memory the machine has at that moment, under a ceiling of `GUESTS`;
starting `tests/vm/run.py` by hand instead goes around that admission control,
and four guests started that way left this machine with 788 MiB free.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Sequence

from .expectations import EXPECTATIONS, Expectation
from .media import MISSING_PRECONDITION

WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/runs"
LOGS: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/campaign"

#: The most guests the machine may ever hold, whatever memory says. A ceiling
#: rather than the limit: `_room_for_a_guest` is what usually decides.
GUESTS: Final[int] = 4

#: What one guest costs, measured: `-m 8G` reaches 8.1 GiB resident.
GUEST_BYTES: Final[int] = 9 * 1024**3

#: What has to be left over after the new guest. earlyoom on this machine
#: kills below 6% of 60 GiB, and it prefers `qemu-system`, so the guest is
#: what dies. The margin is above that with room for the desktop to grow.
HEADROOM_BYTES: Final[int] = 8 * 1024**3

#: How long to wait before looking at memory again.
PATIENCE: Final[float] = 30.0


def available_bytes() -> int:
    """`MemAvailable`, which is what earlyoom watches too."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def wait_for_room(log: Callable[[str], None] = print, patience: float = PATIENCE) -> None:
    """Block until another guest fits.

    A fixed count cannot know what else the machine is doing. One campaign ran
    beside an editor and a test suite and lost sixteen of twenty-four guests to
    earlyoom, which reads as an installer defect in every one of those logs.
    An unreadable `/proc/meminfo` means no measurement and no waiting: a
    machine this cannot read is not one it should stall on.
    """
    said = False
    while True:
        free = available_bytes()
        if not free or free >= GUEST_BYTES + HEADROOM_BYTES:
            return
        if not said:
            log(
                f"waiting for memory: {free // 1024**3} GiB available, "
                f"{(GUEST_BYTES + HEADROOM_BYTES) // 1024**3} GiB wanted"
            )
            said = True
        time.sleep(patience)

#: What the machine can carry at once in CPU terms, in the units `Run.weight`
#: counts. Separate from `GUESTS` because the two limits are different
#: resources: a compile saturates its vCPUs without costing more memory than
#: a download does. Flat counting put five compile jobs on at once and left
#: the machine with two of them for the last forty minutes.
CAPACITY: Final[int] = 6

#: Long enough for a desktop install from source. The harness has its own
#: per-step timeouts; this only stops a run that wedged entirely.
TIMEOUT: Final[float] = 5400.0


@dataclass(frozen=True)
class Run:
    """One invocation of `tests.vm.run`."""

    config: str
    medium: str = "official-minimal"
    firmware: str = "uefi"
    #: Kill the installer partway and finish it with `--resume`.
    interrupt: bool = False
    #: Boot what was installed and check it. Always: an install that exits 0
    #: is not an install that boots, and that is the whole question.
    boot: bool = True
    #: vCPUs, and through the guest's own MAKEOPTS how fast it compiles. Left
    #: at zero for a run that installs binary packages: more cores do nothing
    #: for a download, and they would be taken from a run that is compiling.
    cpus: int = 0
    #: What this costs the machine, against `CAPACITY`. Two for a run that
    #: compiles a kernel or a desktop: those saturate their vCPUs for half an
    #: hour, and packing them beside each other makes every one of them slower
    #: without finishing any sooner. One for a run that installs binary
    #: packages, which spends its time on the network and the disk.
    weight: int = 1

    @property
    def name(self) -> str:
        how = "-interrupted" if self.interrupt else ""
        return f"{self.medium}-{self.firmware}-{Path(self.config).stem}{how}"

    @property
    def expectation(self) -> Expectation | None:
        return EXPECTATIONS.get(Path(self.config).stem)

    def argv(self) -> list[str]:
        argv = [
            sys.executable, "-m", "tests.vm.run",
            "--medium", self.medium,
            "--firmware", self.firmware,
            "--install", self.config,
        ]
        if self.cpus:
            argv += ["--cpus", str(self.cpus)]
        if self.interrupt:
            argv.append("--interrupt")
        return argv + ["--and-boot"] if self.boot else argv


#: The three stages of `docs/vm-campaign.md`, in order.
STAGES: Final[dict[str, tuple[Run, ...]]] = {
    "blocking": (
        Run("fixtures/vm-binpkg.toml"),
        Run("fixtures/vm-bios.toml", firmware="bios"),
        # The only fixture whose target disk already holds a table; `run.py`
        # seeds it, and the installer keeps partition 1 and adds partition 2.
        Run("fixtures/mbr-edit.toml", firmware="bios"),
        Run("fixtures/vm-desktop.toml", weight=2, cpus=10),
        Run("fixtures/vm-zfs.toml"),
    ),
    "matrix": (
        Run("fixtures/vm-luks.toml"),
        Run("fixtures/vm-lvm.toml"),
        Run("fixtures/vm-mdraid.toml"),
        Run("fixtures/vm-sdboot.toml"),
        # `installkernel[systemd-boot]` needs `bootctl`, and without systemd
        # that is `sys-apps/systemd-utils[boot,kernel-install]`. Every other
        # openrc fixture boots from GRUB and every systemd-boot one runs
        # systemd, so the pair reached a real machine before a test.
        Run("fixtures/openrc-sdboot.toml"),
        Run("fixtures/vm-zfs-encrypted.toml"),
        Run("fixtures/zfs-zbm.toml"),
        Run("fixtures/zbm-unlock.toml"),
        Run("fixtures/btrfs-luks.toml"),
        Run("fixtures/ext4-bios.toml", firmware="bios", weight=2, cpus=10),
        Run("fixtures/vm-cjk-kernel.toml"),
        # The four nothing had ever installed: the only untested filesystem, a
        # desktop on openrc, GNOME at all, and btrfs subvolumes without LUKS
        # wrapped round them.
        Run("fixtures/vm-xfs.toml"),
        Run("fixtures/vm-btrfs.toml"),
        # The two filesystems `compat.py` carries a label rule apiece for and
        # nothing had ever made: `mkfs`, the label, the fstab line and the
        # initramfs support are one path each, and neither had been walked.
        Run("fixtures/ext2.toml", firmware="bios"),
        Run("fixtures/ext3.toml", firmware="bios"),
        # The kernel every other fixture takes as a binary package. Building it
        # is a different path: the configuration, the build, `installkernel`
        # and the initramfs are produced rather than unpacked, and about an
        # hour on four cores, so it gets the weight a desktop gets.
        Run("fixtures/vm-source-kernel.toml", weight=2, cpus=10),
        # A machine that configures its own address instead of asking for one.
        # The model has carried static addressing since the beginning and no
        # fixture set it, so nothing had ever installed a machine that comes up
        # on an address, a gateway and a resolver it was given. `cluster.py`
        # rewrites the address to the one the scheduler reserved.
        Run("fixtures/vm-openrc-desktop.toml", weight=2, cpus=10),
        Run("fixtures/vm-gnome.toml", weight=2, cpus=10),
        # Three more nothing had ever installed: raidz needs a third disk,
        # which no other fixture asks for; zram was set by none of them; and
        # GRUB opening the container itself to read /boot only happens on an
        # encrypted BIOS disk.
        # The in-place conversion: `cluster.py` installs `vm-xfs.toml` first and
        # runs this one against the machine that produced, so the whole path
        # from staging root to swap to bootloader is exercised on a real
        # system rather than a plan. Two installs one after the other, so it is
        # long rather than heavy: the instantaneous cost is one guest's.
        Run("fixtures/vm-convert.toml"),
        Run("fixtures/vm-raidz.toml"),
        Run("fixtures/vm-zram.toml"),
        Run("fixtures/vm-bios-luks.toml", firmware="bios"),
        # sshd with a key that can reach it, and the initramfs daemon that
        # unlocks the root: `remote_unlock` was off in every other fixture.
        # The proxy pointed at a port nothing listens on: a run that reaches
        # the mirror proves something bypassed it, so this one is expected to
        # fail at the stage3 download and its failure is the result.
        Run("fixtures/vm-proxy-dead.toml"),  # see expectations.EXPECTATIONS
        # The binary host it names answers 404, so the run has to give binary
        # packages up and finish from source. `EXPECTATIONS` is what makes the
        # console say so; without it the run is green whether or not the host
        # ever failed.
        Run("fixtures/vm-binhost-fallback.toml"),
        # The direction that matters to an operator on an intranet, and the one
        # nothing covered: the proxy answers and the install completes through
        # it. It needs a SOCKS5 listener on the workstation, so `run.py` refuses
        # the run rather than reporting a proxy defect the fixture cannot show.
        Run("fixtures/vm-proxy.toml"),
        # The same layout through an HTTP proxy: the two kinds take
        # different fetchers, and only SOCKS5 had installed a machine.
        Run("fixtures/vm-proxy-http.toml"),
        Run("fixtures/vm-unlock.toml"),
        # The same configuration again, killed partway and finished with
        # --resume: the one path nothing else reaches.
        Run("fixtures/vm-binpkg.toml", interrupt=True),
        # A mirrored pool across two disks, and the third filesystem member no
        # fixture had ever made.
        Run("fixtures/vm-zfs-mirror.toml"),
        Run("fixtures/vm-f2fs.toml"),
        # xfce behind greetd, and the only fixture with a display manager
        # whose configuration the installer rewrites rather than writes: the
        # `command =` line in the file `gui-libs/greetd` installs.
        Run("fixtures/vm-greetd.toml", weight=2, cpus=10),
    ),
    # One configuration, six media: this stage tests `bootstrap.sh` and
    # preflight, so the shortest fixture is the right one. Booted like every
    # other run, because an install performed from a foreign medium still has
    # to produce a system that starts.
    "media": tuple(
        Run("fixtures/vm-binpkg.toml", medium=one)
        for one in ("gigos", "alpine", "debian", "arch", "fedora", "opensuse")
    ),
}


@dataclass(frozen=True)
class Outcome:
    run: Run
    returncode: int
    seconds: float
    log: Path
    error: str | None = None

    @property
    def passed(self) -> bool:
        expected = self.run.expectation
        if self.error is not None:
            return False
        if expected is None:
            return self.returncode == 0
        try:
            said = self.log.read_text(errors="replace")
        except OSError:
            return False
        if expected.marker not in said:
            return False
        if not expected.must_stop:
            return self.returncode == 0
        return self.returncode == expected.runner_returncode


def _log_for(run: Run) -> Path:
    return LOGS / f"{run.name}.log"


def _failed_outcome(run: Run, started: float, error: Exception) -> Outcome:
    return Outcome(
        run,
        1,
        time.monotonic() - started,
        _log_for(run),
        f"{type(error).__name__}: {error}",
    )


def perform(run: Run) -> Outcome:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = _log_for(run)
    started = time.monotonic()
    with log.open("w") as handle:
        finished = subprocess.run(
            run.argv(),
            cwd=Path(__file__).resolve().parents[2],
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=TIMEOUT,
            check=False,
        )
    return Outcome(run, finished.returncode, time.monotonic() - started, log)


#: What the log says when the guest went away rather than the install failing.
#: Reported apart from a real failure: chasing it as a defect wastes the time
#: the campaign exists to save.
HOST_KILLED: Final[str] = "the guest closed the serial connection"

#: What the log says when the same configuration was already being run. Also
#: not a defect: two overlapping campaigns name the same fixture, and reading
#: that as a failed install sent one round chasing a ZFS bug that was a lock.
ALREADY_RUNNING: Final[str] = "another run holds"


def mark_for(outcome: Outcome) -> str:
    if outcome.passed:
        return "ok  "
    if outcome.error is not None:
        return "FAIL"
    if (expected := outcome.run.expectation) is not None and expected.must_stop:
        # A clean exit proves the proxy was bypassed; another failure proved
        # neither the expected path nor a completed installation.
        return "BYPASS" if outcome.returncode == 0 else "FAIL"
    said = outcome.log.read_text(errors="replace")
    if MISSING_PRECONDITION in said:
        # Not a fixture that failed: the workstation is missing an ISO or a
        # listener, and reporting it as FAIL sends a reader to the installer.
        return "SKIP"
    if HOST_KILLED in said:
        return "HOST"
    return "LOCK" if ALREADY_RUNNING in said else "FAIL"


def announce(outcome: Outcome) -> None:
    mark = mark_for(outcome)
    print(f"{mark} {outcome.run.name:52} {outcome.seconds / 60:5.1f}m  {outcome.log}")


def parallel(runs: Sequence[Run]) -> list[Outcome]:
    """Every run to the end. They are independent, and one failure is not a
    reason to leave the rest unknown: the point of a campaign is to learn
    everything one pass can teach before anything is changed.

    Announced as each finishes rather than in the order they were submitted.
    `pool.map` yields in submission order, so a run that failed in one minute
    stayed unreported behind one that takes forty, and nobody could start on
    it until the whole batch was done.
    """
    room = Semaphore(CAPACITY)
    seats = Semaphore(GUESTS)

    def carried(one: Run) -> Outcome:
        seats.acquire()
        # Counted rather than assumed: an exception between two acquisitions
        # would otherwise release a unit the worker never held.
        held = 0
        try:
            # After the seat and before the run: the ceiling bounds how many can
            # ever be waiting, and this decides whether one more fits right now.
            wait_for_room()
            for _ in range(one.weight):
                room.acquire()
                held += 1
            return perform(one)
        finally:
            for _ in range(held):
                room.release()
            seats.release()

    # Heaviest first. Started last, a run that compiles a kernel is the only
    # thing left on the machine for its final half hour.
    ordered = sorted(runs, key=lambda one: -one.weight)
    done: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=len(ordered) or 1) as pool:
        waiting = {
            pool.submit(carried, one): (one, time.monotonic())
            for one in ordered
        }
        for finished in as_completed(waiting):
            try:
                outcome = finished.result()
            except Exception as error:
                run, started = waiting[finished]
                outcome = _failed_outcome(run, started, error)
            announce(outcome)
            done.append(outcome)
    return done


def named(wanted: Sequence[str]) -> list[Run]:
    """The runs whose fixture carries one of these names.

    So that testing four configurations again is this harness with an argument
    rather than a shell loop written for one afternoon and thrown away.
    """
    by_name = {Path(one.config).stem: one for runs in STAGES.values() for one in runs}
    missing = [one for one in wanted if one not in by_name]
    if missing:
        raise SystemExit(f"no fixture named {', '.join(missing)}; have {', '.join(sorted(by_name))}")
    return [by_name[one] for one in wanted]


def main(argv: list[str] | None = None) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=sorted(STAGES), action="append")
    parser.add_argument(
        "--only",
        action="append",
        metavar="FIXTURE",
        help="run just these, by fixture name, from whichever stage holds them",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="run the later stages even when a blocking one failed",
    )
    args = parser.parse_args(argv)
    done: list[Outcome] = []
    if args.only:
        done = list(parallel(named(args.only)))
    elif args.keep_going:
        # One pool, not one stage at a time. The stage barrier exists so a
        # failed blocking stage can stop the rest, and `--keep-going` has
        # already said not to: waiting for it left three of four seats idle
        # for the half hour the desktop build takes.
        wanted = args.stage or list(STAGES)
        print(f"--- {', '.join(wanted)} ({sum(len(STAGES[one]) for one in wanted)} runs)")
        done = list(parallel([run for stage in wanted for run in STAGES[stage]]))
    else:
        for stage in args.stage or list(STAGES):
            print(f"--- {stage} ({len(STAGES[stage])} runs)")
            outcomes = parallel(STAGES[stage])
            done += outcomes
            if any(not one.passed for one in outcomes) and stage == "blocking":
                print("the blocking stage failed; the rest would prove nothing")
                break
    return report(done)


def report(done: Sequence[Outcome]) -> int:
    """Print the summary and leave a copy on disk. An unattended run is
    launched into a pipe nobody keeps, and the per-run logs alone do not say
    which configurations were in the round."""
    failed = [one for one in done if not one.passed]
    killed = [one for one in failed if mark_for(one) == "HOST"]
    locked = [one for one in failed if mark_for(one) == "LOCK"]
    if locked:
        print(
            f"\n{len(locked)} run(s) never started: another campaign held the same "
            "configuration, so nothing about them was tested"
        )
    if killed:
        print(
            f"\n{len(killed)} run(s) lost the guest rather than failing an install; "
            "that is this machine running out of memory, not a defect"
        )
    print(f"\n{len(done) - len(failed)}/{len(done)} passed")
    for one in failed:
        detail = f", {one.error}" if one.error is not None else ""
        print(f"  {one.run.name}: exit {one.returncode}{detail}, {one.log}")
    lines = [f"{mark_for(one)} {one.run.name}" for one in done]
    lines.append(f"{len(done) - len(failed)}/{len(done)} passed")
    (LOGS / "summary.txt").write_text("\n".join(lines) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
