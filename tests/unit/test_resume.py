"""Carrying on from where a run stopped.

The question this answers: an install that reached the desktop stage and died
there has already partitioned, installed a bootloader and built a kernel.
Starting again from the beginning throws all of that away.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from gentoo_install.exec.apply import completed
from gentoo_install.log import Journal
from gentoo_install.model.device import DeviceId
from gentoo_install.plan.operations import Operation, Stage

from .recorder import Recorder


@dataclass(frozen=True, kw_only=True)
class Noted(Operation):
    stage: Stage = Stage.PACKAGES
    text: str

    def describe(self) -> str:
        return self.text

    def apply(self, context: object) -> None:  # pragma: no cover - never run here
        raise AssertionError("this test never applies an operation")


def journal_with(tmp_path: Path, entries: list[tuple[int, str, str]]) -> Journal:
    """`position` is what the run recorded, not the order of the lines: that is
    the whole point of the field."""
    journal = Journal(path=tmp_path / "install.jsonl")
    for position, describe, status in entries:
        journal.write(
            "operation",
            stage="packages",
            describe=describe,
            seconds=0.1,
            status=status,
            position=position,
        )
    return Journal(path=tmp_path / "install.jsonl")


def test_only_the_operations_that_finished_are_skipped(tmp_path: Path) -> None:
    journal = journal_with(
        tmp_path,
        [(0, "partition", "done"), (1, "mkfs", "done"), (2, "emerge plasma", "failed")],
    )
    assert completed(journal) == {(0, "partition"), (1, "mkfs")}


def test_position_counts_as_well_as_the_text(tmp_path: Path) -> None:
    """A plan can hold two operations that describe themselves identically, and
    skipping both because one finished steps over work never done."""
    journal = journal_with(tmp_path, [(0, "emerge", "done"), (1, "emerge", "failed")])
    assert completed(journal) == {(0, "emerge")}


def test_a_second_resume_does_not_redo_what_the_first_one_finished(tmp_path: Path) -> None:
    """Counting the lines drifted twice over: the failed attempt consumed a
    number, and a resumed run appends to the same file. After one resume every
    position was wrong, and the next resume re-ran operations that had already
    partitioned the disk."""
    path = tmp_path / "install.jsonl"
    journal = Journal(path=path)

    def note(position: int, describe: str, status: str) -> None:
        journal.write(
            "operation",
            stage="partition",
            describe=describe,
            seconds=0.1,
            status=status,
            position=position,
        )

    for position in range(5):
        note(position, f"op{position}", "done")
    note(5, "op5", "failed")
    assert completed(Journal(path=path)) == {(n, f"op{n}") for n in range(5)}

    # The resumed run picks up at 5 and dies again after 6.
    note(5, "op5", "done")
    note(6, "op6", "done")
    assert completed(Journal(path=path)) == {(n, f"op{n}") for n in range(7)}


def test_an_entry_from_before_positions_were_recorded_is_ignored(tmp_path: Path) -> None:
    """It says nothing reliable about where it sat, and redoing work is safer
    than skipping the wrong operation."""
    path = tmp_path / "install.jsonl"
    Journal(path=path).write(
        "operation", stage="partition", describe="partition", seconds=0.1, status="done"
    )
    assert completed(Journal(path=path)) == frozenset()


def test_a_run_with_no_journal_skips_nothing(tmp_path: Path) -> None:
    assert completed(Journal(path=tmp_path / "absent.jsonl")) == frozenset()
    assert completed(None) == frozenset()


def test_a_half_written_last_line_does_not_stop_the_replay(tmp_path: Path) -> None:
    """A run killed mid-write leaves one broken line, and losing the whole
    journal over it would make resume useless exactly when it is needed."""
    path = tmp_path / "install.jsonl"
    journal = Journal(path=path)
    journal.write(
        "operation",
        stage="partition",
        describe="partition",
        seconds=0.1,
        status="done",
        position=0,
    )
    with path.open("a") as handle:
        handle.write('{"event": "operation", "desc')
    assert completed(Journal(path=path)) == {(0, "partition")}


def test_the_running_installer_says_where_it_is_and_how_long_it_has_taken() -> None:
    """A desktop emerge takes hours, and the screen carried one line per
    operation with no position, no total and no clock."""
    from gentoo_install.exec.apply import _elapsed, apply
    from gentoo_install.exec.probe import Probe
    from gentoo_install.exec.runner import Runner
    from gentoo_install.plan.build import build
    from gentoo_install.data import load_catalog
    from gentoo_install.model.parse import load
    from gentoo_install.exec.apply import Machine

    assert _elapsed(0) == "0:00:00"
    assert _elapsed(3671) == "1:01:11"

    said: list[str] = []
    runner = Runner(log=said.append, dry_run=True)
    config = load(Path("tests/fixtures/ext4-bios.toml"))
    operations = build(config, load_catalog())
    machine = Machine(
        config=config,
        runner=runner,
        probe=Probe(runner=runner, work=Path("/tmp")),
        work=Path("/tmp"),
        mountpoint=Path("/mnt/gentoo"),
    )
    # Every operation is reported as already done, so nothing runs and the
    # counter is all that is under test.
    finished = frozenset(
        (position, one.describe()) for position, one in enumerate(operations)
    )
    apply(operations, machine, finished)
    counted = [line for line in said if line.startswith("[1/")]
    assert counted and f"[1/{len(operations)} 0:00:00]" in counted[0]
    assert any(line.startswith(f"[{len(operations)}/{len(operations)} ") for line in said)


def test_a_resumed_run_mounts_again_rather_than_skipping() -> None:
    """A mount is state of the running machine, not of the disk. Skipping it
    after a reboot unpacks the stage3 into the live medium's own tmpfs until
    the machine runs out of memory, with nothing in the log saying so."""
    from gentoo_install.plan.disk import Mount
    from gentoo_install.plan.operations import Stage

    mount = Mount(
        mountpoint=DeviceId("mnt"),
        source=DeviceId("fs"),
        path=PurePosixPath("/"),
        options=(),
    )
    assert mount.survives_a_reboot is False
    # Everything that writes to the disk stays skippable, or a resumed run
    # partitions a disk it already installed onto.
    from gentoo_install.plan.disk import WipeSignatures

    assert WipeSignatures(device=DeviceId("part"), stage=Stage.PARTITION).survives_a_reboot


def test_mounting_something_already_mounted_is_not_an_error() -> None:
    """It runs again on a resume, so it has to be safe to run twice."""
    from gentoo_install.plan.disk import Mount

    recorder = Recorder()
    recorder.mounts.add("/mnt/gentoo")
    Mount(
        mountpoint=DeviceId("mnt"),
        source=DeviceId("fs"),
        path=PurePosixPath("/"),
        options=(),
    ).apply(recorder)
    assert not any(argv[0] == "mount" for argv in recorder.commands)
