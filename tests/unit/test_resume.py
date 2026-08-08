"""Carrying on from where a run stopped.

The question this answers: an install that reached the desktop stage and died
there has already partitioned, installed a bootloader and built a kernel.
Starting again from the beginning throws all of that away.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gentoo_install.exec.apply import completed
from gentoo_install.log import Journal
from gentoo_install.plan.operations import Operation, Stage


@dataclass(frozen=True, kw_only=True)
class Noted(Operation):
    stage: Stage = Stage.PACKAGES
    text: str

    def describe(self) -> str:
        return self.text

    def apply(self, context: object) -> None:  # pragma: no cover - never run here
        raise AssertionError("this test never applies an operation")


def journal_with(tmp_path: Path, entries: list[tuple[str, str]]) -> Journal:
    journal = Journal(path=tmp_path / "install.jsonl")
    for describe, status in entries:
        journal.write("operation", stage="packages", describe=describe, seconds=0.1, status=status)
    return Journal(path=tmp_path / "install.jsonl")


def test_only_the_operations_that_finished_are_skipped(tmp_path: Path) -> None:
    journal = journal_with(
        tmp_path, [("partition", "done"), ("mkfs", "done"), ("emerge plasma", "failed")]
    )
    assert completed(journal) == {(0, "partition"), (1, "mkfs")}


def test_position_counts_as_well_as_the_text(tmp_path: Path) -> None:
    """A plan can hold two operations that describe themselves identically, and
    skipping both because one finished steps over work never done."""
    journal = journal_with(tmp_path, [("emerge", "done"), ("emerge", "failed")])
    assert completed(journal) == {(0, "emerge")}


def test_a_run_with_no_journal_skips_nothing(tmp_path: Path) -> None:
    assert completed(Journal(path=tmp_path / "absent.jsonl")) == frozenset()
    assert completed(None) == frozenset()


def test_a_half_written_last_line_does_not_stop_the_replay(tmp_path: Path) -> None:
    """A run killed mid-write leaves one broken line, and losing the whole
    journal over it would make resume useless exactly when it is needed."""
    path = tmp_path / "install.jsonl"
    journal = Journal(path=path)
    journal.write("operation", stage="partition", describe="partition", seconds=0.1, status="done")
    with path.open("a") as handle:
        handle.write('{"event": "operation", "desc')
    assert completed(Journal(path=path)) == {(0, "partition")}
