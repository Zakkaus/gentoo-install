# SPDX-License-Identifier: GPL-2.0-or-later
"""Carrying on from where a run stopped.

The question this answers: an install that reached the desktop stage and died
there has already partitioned, installed a bootloader and built a kernel.
Starting again from the beginning throws all of that away.
"""

from __future__ import annotations

import pytest

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
    the whole point of the field. The middle value is the operation's identity,
    which is what a resumed run matches against."""
    journal = Journal(path=tmp_path / "install.jsonl")
    for position, said, status in entries:
        journal.write(
            "operation",
            stage="packages",
            describe=said,
            identity=said,
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

    def note(position: int, said: str, status: str) -> None:
        journal.write(
            "operation",
            stage="partition",
            describe=said,
            identity=said,
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


def test_a_new_run_does_not_inherit_an_earlier_runs_checkpoints(tmp_path: Path) -> None:
    path = tmp_path / "install.jsonl"
    earlier = Journal(path=path)
    earlier.started(configuration="first", session="boot", installer="tree")
    earlier.write(
        "operation",
        stage="partition",
        describe="partition",
        identity="partition",
        seconds=0.1,
        status="done",
        position=0,
    )

    Journal(path=path).started(configuration="second", session="boot", installer="tree")

    assert completed(Journal(path=path)) == frozenset()


def test_a_resume_keeps_its_earlier_checkpoints(tmp_path: Path) -> None:
    path = tmp_path / "install.jsonl"
    initial = Journal(path=path)
    initial.started(configuration="config", session="boot", installer="tree")
    initial.write(
        "operation",
        stage="partition",
        describe="partition",
        identity="partition",
        seconds=0.1,
        status="done",
        position=0,
    )

    resumed = Journal(path=path)
    assert resumed.resume()
    resumed.write(
        "operation",
        stage="partition",
        describe="format",
        identity="format",
        seconds=0.1,
        status="done",
        position=1,
    )

    assert completed(Journal(path=path)) == {(0, "partition"), (1, "format")}


def test_an_entry_from_before_positions_were_recorded_is_ignored(tmp_path: Path) -> None:
    """It says nothing reliable about where it sat, and redoing work is safer
    than skipping the wrong operation."""
    path = tmp_path / "install.jsonl"
    Journal(path=path).write(
        "operation",
        stage="partition",
        describe="partition",
        identity="partition",
        seconds=0.1,
        status="done",
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
        identity="partition",
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
    from gentoo_install.exec.config import load
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
    from gentoo_install.exec.apply import identity

    finished = frozenset(
        (position, identity(one)) for position, one in enumerate(operations)
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


def test_a_changed_implementation_is_not_skipped_by_an_old_journal() -> None:
    """Commit `57f5ad3` changed `ConfigureInstallKernel` from writing
    `/etc/kernel/install.conf` to writing a drop-in and left its `describe()`
    alone, so a journal from before it let a resumed run skip the correction.
    Identity covers the class's source for exactly that."""
    from gentoo_install.exec.apply import identity

    @dataclass(frozen=True, kw_only=True)
    class Before(Operation):
        stage: Stage = Stage.KERNEL
        where: str = "/etc/kernel/install.conf"

        def describe(self) -> str:
            return "set the boot root"

        def apply(self, context: object) -> None:
            raise AssertionError("never run here")

    @dataclass(frozen=True, kw_only=True)
    class After(Operation):
        stage: Stage = Stage.KERNEL
        where: str = "/etc/kernel/install.conf"

        def describe(self) -> str:
            return "set the boot root"

        def apply(self, context: object) -> None:
            # The drop-in, not the file that shadows installkernel's own.
            raise AssertionError("never run here")

    old, new = Before(), After()
    assert old.describe() == new.describe()
    assert identity(old) != identity(new)


def test_a_changed_payload_is_not_skipped_either() -> None:
    """Two operations of the same class whose fields differ are different work,
    however similar their descriptions read."""
    from gentoo_install.exec.apply import identity

    assert identity(Noted(text="emerge")) != identity(Noted(text="emerge plasma"))
    assert identity(Noted(text="emerge")) == identity(Noted(text="emerge"))


def test_a_resume_gives_up_on_what_the_first_run_gave_up_on(tmp_path: Path) -> None:
    """The operation that recorded an unusable binary host has already
    completed and is skipped, so a resumed run rebuilt an empty `given_up` and
    the next `Emerge` asked that host for packages the earlier run had
    declared untrusted. The record of where each package came from then
    changed across the resume boundary."""
    from gentoo_install.exec.apply import already_degraded
    from gentoo_install.log import Journal

    journal = Journal(tmp_path / "install.jsonl")
    journal.degraded("binary packages", "the host answered no signature")
    journal.write("operation", position=0, description="merge", status="done")

    assert already_degraded(journal) == {"binary packages"}
    assert already_degraded(None) == set()


def test_a_new_run_does_not_inherit_an_earlier_runs_degradation(tmp_path: Path) -> None:
    from gentoo_install.exec.apply import already_degraded

    path = tmp_path / "install.jsonl"
    earlier = Journal(path=path)
    earlier.started(configuration="first", session="boot", installer="tree")
    earlier.degraded("binary packages", "the host answered no signature")

    Journal(path=path).started(configuration="second", session="boot", installer="tree")

    assert already_degraded(Journal(path=path)) == set()


def test_an_emerge_after_a_resume_still_builds_from_source(tmp_path: Path) -> None:
    """The whole point of restoring it: `BINPKG_OPTIONS` on a host the first
    run refused would fetch exactly what it refused."""
    from gentoo_install.exec.apply import already_degraded
    from gentoo_install.log import Journal
    from gentoo_install.plan.portage import BINARY_PACKAGES, Emerge
    from tests.unit.recorder import Recorder

    journal = Journal(tmp_path / "install.jsonl")
    journal.degraded(BINARY_PACKAGES, "the key was never signed")

    recorder = Recorder()
    for what in already_degraded(journal):
        recorder.given_up.add(what)
    Emerge(packages=("app-editors/nano",), summary="editor").apply(recorder)
    merged = " ".join(" ".join(one) for one in recorder.in_target)
    assert "--usepkg=n" in merged and "--getbinpkg=n" in merged, merged


#: Every fixture with storage a reboot takes down, and the operation that has
#: to put it back. One table, so a storage kind cannot be added with a
#: creation step and no activation step.
REESTABLISHED: tuple[tuple[str, str], ...] = (
    ("vm-luks", "OpenLuks"),
    ("vm-mdraid", "AssembleMdRaid"),
    ("vm-lvm", "ActivateVolumeGroup"),
    ("vm-zfs", "ImportZpool"),
)


@pytest.mark.parametrize(("fixture", "operation"), REESTABLISHED, ids=lambda one: str(one))
def test_a_resumed_run_re_establishes_what_a_reboot_takes_away(
    fixture: str, operation: str
) -> None:
    """The failure cleanup unmounts the target and exports the pool, so a
    resume that skipped the creation had no container, no array, no volume
    group and no pool: `/mnt/gentoo` was the live medium's own directory and
    every chroot command ran against the installing system.

    The creation stays skippable — it wrote a header the disk still has — and
    the activation beside it does not.
    """
    from pathlib import Path

    from gentoo_install.data import load_catalog
    from gentoo_install.exec.config import load
    from gentoo_install.plan import build as plan

    operations = plan.build(load(Path("tests/fixtures") / f"{fixture}.toml"), load_catalog())
    by_name = {type(one).__name__: one for one in operations}
    assert operation in by_name, sorted(by_name)
    assert by_name[operation].survives_a_reboot is False


def test_a_resume_remounts_the_chroot_but_keeps_the_resolver_seed_checkpoint() -> None:
    """Cleanup removes the chroot mounts, while the copied resolver remains on
    disk until the final system-stage replacement. A reboot checkpoint must
    therefore rerun only the transient half of chroot preparation."""
    from gentoo_install.plan import portage
    from tests.unit.layouts import config

    operations = portage.build(config(), mirror="https://distfiles.gentoo.org")
    by_name = {type(one).__name__: one for one in operations}
    assert by_name["MountChrootFilesystems"].survives_a_reboot is False
    assert by_name["SeedResolver"].survives_a_reboot is True


def test_every_kind_of_storage_that_needs_activation_has_a_fixture_here() -> None:
    """An activation operation with no fixture in the table above is one
    nothing proves, and it reads as covered."""
    from gentoo_install.plan import disk as plan_disk

    named = {name for _, name in REESTABLISHED}
    defined = {
        name
        for name in dir(plan_disk)
        if name.startswith(("Open", "Assemble", "Activate", "Import"))
        and isinstance(getattr(plan_disk, name), type)
    }
    assert defined - named == set(), defined - named
