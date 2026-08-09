"""Run the whole VM matrix unattended, in the order `docs/vm-campaign.md` sets.

    python3 -m tests.vm.campaign --stage blocking
    python3 -m tests.vm.campaign            # every stage, in order

Stage one is sequential: each of its runs would make the ones after it
meaningless if it failed. The rest go four at a time, which is what 60 GiB
holds at the 8 GiB a VM is given.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Sequence

WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/runs"
LOGS: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/campaign"

#: Measured rather than assumed: a guest given 8 GiB sits at about 6 GiB
#: resident, so six of them is 36 GiB of the 60 this machine has. Raising it
#: further would swap, and a swapping guest times out rather than failing.
WORKERS: Final[int] = 6

#: Long enough for a desktop install from source. The harness has its own
#: per-step timeouts; this only stops a run that wedged entirely.
TIMEOUT: Final[float] = 5400.0


@dataclass(frozen=True)
class Run:
    """One invocation of `tests.vm.run`."""

    config: str
    medium: str = "official-minimal"
    firmware: str = "uefi"
    #: Boot what was installed and check it. Off only where the fixture is
    #: there to exercise the installer rather than the system it produces.
    boot: bool = True

    @property
    def name(self) -> str:
        return f"{self.medium}-{self.firmware}-{Path(self.config).stem}"

    def argv(self) -> list[str]:
        argv = [
            sys.executable, "-m", "tests.vm.run",
            "--medium", self.medium,
            "--firmware", self.firmware,
            "--install", self.config,
        ]
        return argv + ["--and-boot"] if self.boot else argv


#: The three stages of `docs/vm-campaign.md`, in order.
STAGES: Final[dict[str, tuple[Run, ...]]] = {
    "blocking": (
        Run("fixtures/vm-binpkg.toml"),
        Run("fixtures/vm-bios.toml", firmware="bios"),
        Run("fixtures/vm-desktop.toml"),
        Run("fixtures/vm-zfs.toml"),
    ),
    "matrix": (
        Run("fixtures/vm-luks.toml"),
        Run("fixtures/vm-lvm.toml"),
        Run("fixtures/vm-mdraid.toml"),
        Run("fixtures/vm-sdboot.toml"),
        Run("fixtures/vm-zfs-encrypted.toml"),
        Run("fixtures/zfs-zbm.toml"),
        Run("fixtures/btrfs-luks.toml"),
        Run("fixtures/ext4-bios.toml", firmware="bios"),
        Run("fixtures/vm-cjk-kernel.toml"),
    ),
    # One configuration, six media: this stage tests `bootstrap.sh` and
    # preflight, not the install, so the shortest fixture is the right one.
    "media": tuple(
        Run("fixtures/vm-binpkg.toml", medium=one, boot=False)
        for one in ("gigos", "alpine", "debian", "arch", "fedora", "opensuse")
    ),
}


@dataclass(frozen=True)
class Outcome:
    run: Run
    returncode: int
    seconds: float
    log: Path

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def perform(run: Run) -> Outcome:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{run.name}.log"
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


def announce(outcome: Outcome) -> None:
    mark = "ok  " if outcome.passed else "FAIL"
    print(f"{mark} {outcome.run.name:52} {outcome.seconds / 60:5.1f}m  {outcome.log}")


def sequential(runs: Sequence[Run]) -> list[Outcome]:
    """Stop at the first failure: the rest would prove nothing."""
    done: list[Outcome] = []
    for run in runs:
        outcome = perform(run)
        announce(outcome)
        done.append(outcome)
        if not outcome.passed:
            break
    return done


def parallel(runs: Sequence[Run]) -> list[Outcome]:
    """Every run to the end: these are independent, and one failure is not a
    reason to leave the other nine unknown."""
    done: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for outcome in pool.map(perform, runs):
            announce(outcome)
            done.append(outcome)
    return done


def main(argv: list[str] | None = None) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=sorted(STAGES), action="append")
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="run the later stages even when a blocking one failed",
    )
    args = parser.parse_args(argv)
    wanted = args.stage or list(STAGES)

    done: list[Outcome] = []
    for stage in wanted:
        print(f"--- {stage} ({len(STAGES[stage])} runs)")
        outcomes = sequential(STAGES[stage]) if stage == "blocking" else parallel(STAGES[stage])
        done += outcomes
        if not args.keep_going and any(not one.passed for one in outcomes) and stage == "blocking":
            print("the blocking stage failed; the rest would prove nothing")
            break

    failed = [one for one in done if not one.passed]
    print(f"\n{len(done) - len(failed)}/{len(done)} passed")
    for one in failed:
        print(f"  {one.run.name}: exit {one.returncode}, {one.log}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
