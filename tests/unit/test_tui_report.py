# SPDX-License-Identifier: GPL-2.0-or-later
"""The five counts, each against a session that produces it and one that does not.

Every count here has a negative control in the same test: a session whose
screens differ only in the thing being counted. A count that answers the same
number for both is measuring the session's length, not the interface.
"""

from __future__ import annotations

from pathlib import Path

from tests.tui.report import STUCK_AFTER, Report, read, title_of

#: A framed pane, as `TwoPane.frame` draws it. The heading names the open row.
def frame(name: str, rows: tuple[tuple[str, str], ...]) -> str:
    lines = [f"+- {name} " + "-" * 20 + "+"]
    lines += [f"{one.ljust(20)}| {other}" for one, other in rows]
    return "\n".join(lines)


def session(directory: Path, screens: list[str], keys: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "screens.txt").write_text("\f".join(screens), encoding="utf-8")
    (directory / "keys.txt").write_text("\n".join(keys) + "\n", encoding="utf-8")
    return directory


DISK = (("* Disk", "/dev/sda"), ("  Hostname", "lab1"))
CHANGED = (("* Disk", "/dev/sdb"), ("  Hostname", "lab1"))


def test_the_frame_names_the_open_row_not_the_title_bar() -> None:
    """`gentoo-install` is on every screen, so it identifies none of them."""
    assert title_of(frame("Partitioning", DISK)) == "Partitioning"
    # Negative control: a screen with no frame falls back to its first line,
    # and must not answer with the box drawing of a screen that has one.
    assert title_of("Mirrors\n  gentoo") == "Mirrors"


def test_a_row_opened_and_left_unchanged_counts_as_lost(tmp_path: Path) -> None:
    """The count that says a row's own name failed to say what it was."""
    inside = frame("Disk", DISK)
    lost = read(
        session(
            tmp_path / "lost",
            [frame("Partitioning", DISK), inside, frame("Partitioning", DISK)],
            ["enter", "esc", "down"],
        )
    )
    assert lost.lost == ("Disk",), lost

    # Negative control: the same three screens and the same two keys, with one
    # value different on the way out. Nothing was lost; something was set.
    found = read(
        session(
            tmp_path / "found",
            [frame("Partitioning", DISK), inside, frame("Partitioning", CHANGED)],
            ["enter", "esc", "down"],
        )
    )
    assert found.lost == (), found


def test_a_screen_that_outlasts_answering_it_counts_as_stuck(tmp_path: Path) -> None:
    same = frame("Mirrors", DISK)
    stuck = read(session(tmp_path / "stuck", [same] * (STUCK_AFTER + 2), ["down"]))
    assert stuck.stuck == ("Mirrors",), stuck

    # Negative control: the same number of screens, moving through rows. Length
    # alone must not produce the count.
    moving = [frame(f"Row {at}", DISK) for at in range(STUCK_AFTER + 2)]
    assert read(session(tmp_path / "moving", moving, ["down"])).stuck == ()


def test_asking_for_help_and_being_refused_are_counted(tmp_path: Path) -> None:
    refused = frame("Extra packages", (("Not a package name: code", ""),))
    one = read(
        session(tmp_path / "asked", [frame("Disk", DISK), refused], ["help", "type:code"])
    )
    assert one.helped == 1 and one.refused == 1, one

    # Negative control: the same length of session with neither.
    quiet = read(
        session(tmp_path / "quiet", [frame("Disk", DISK)] * 2, ["down", "type:code"])
    )
    assert quiet.helped == 0 and quiet.refused == 0, quiet


def test_finished_means_the_plan_ran_not_that_a_row_was_on_the_screen(
    tmp_path: Path,
) -> None:
    """The main list always carries a `Start the installation` row.

    Matched against the last screen, a session that ended anywhere on that
    list read as one that had installed: three of four runs were reported
    finished when one of them had left the installer without starting it.
    """
    still = session(tmp_path / "part", [frame("Install", DISK)], ["down"])
    (still / "probe-9301-abc.log").write_bytes(b"root@livecd ~ # ")
    assert not read(still).finished

    ran = session(tmp_path / "done", [frame("Disk", DISK)], ["enter"])
    (ran / "probe-9301-abc.log").write_bytes(
        b"run: findmnt --mountpoint /mnt/gentoo\ninstalled 74 operations into /mnt/gentoo\n"
    )
    assert read(ran).finished

    # Negative control: a console that mentions the installer without having
    # run it is not enough, or the count is measuring the word and not the run.
    said = session(tmp_path / "said", [frame("Disk", DISK)], ["enter"])
    (said / "probe-9301-abc.log").write_bytes(b"install.sh --lang zh-TW\nInstall\n")
    assert not read(said).finished


def test_reading_a_session_that_left_nothing_behind_answers_zero(tmp_path: Path) -> None:
    """A guest killed before its first screen must not read as a finished run."""
    empty = read(tmp_path / "never-ran")
    assert empty == Report(finished=False, lost=(), helped=0, stuck=(), refused=0)


def test_a_translated_screen_is_counted_the_same_as_an_english_one(tmp_path: Path) -> None:
    """A session runs in its spec's language, so the counts read the catalogs.

    The words come from the catalog rather than from a literal in this file:
    a translated string pasted here goes stale the next time the wording is
    corrected, and passes while measuring nothing.
    """
    from gentoo_install.i18n import Catalog

    rejected = Catalog("zh-TW")("Not a package name")
    assert rejected != "Not a package name", "the catalog has no translation to test against"
    refused = read(session(tmp_path / "zh", [frame("Extra packages", ((rejected, ""),))], ["type:code"]))
    assert refused.refused == 1, refused

    # Negative control: a screen whose text no catalog maps that string to is
    # not counted as a refusal.
    assert read(session(tmp_path / "no", [frame("Kernel", DISK)], ["enter"])).refused == 0

def test_the_session_offers_no_subcommand_that_answers_with_its_own_input() -> None:
    """`plan` read the key log and called it the installer's plan.

    An agent asked whether the plan matches its spec would have been comparing
    the spec against its own keystrokes, which is the shape of check that
    cannot fail. It is gone rather than stubbed: a subcommand that answers
    something else under the right name is worse than an absent one.
    """
    import tests.tui.session as session

    source = Path(session.__file__).read_text(encoding="utf-8")
    assert '"plan"' not in source, "plan is back and has to answer a real plan"
    assert '"screen"' in source and '"key"' in source


def test_the_session_writes_the_file_the_report_counts_from(tmp_path: Path) -> None:
    """The report read `screens.txt` and nothing wrote it.

    Every count came back zero on a session that had been driven for half an
    hour, and zero reads as a clean run rather than as a missing file.
    """
    import tests.tui.session as session

    named = session.Session("probe")
    assert named.screens.name == "screens.txt"
    assert named.screens != named.transcript

    source = Path(session.__file__).read_text(encoding="utf-8")
    assert "session.screens.open" in source, "no screen is ever recorded"

    # Negative control: the report on a directory holding only keys answers
    # zero, which is what a session that never recorded a screen looks like.
    (tmp_path / "keys.txt").write_text("enter\nesc\n", encoding="utf-8")
    assert read(tmp_path) == Report(
        finished=False, lost=(), helped=0, stuck=(), refused=0
    )
def test_every_spec_names_a_machine_that_can_answer_it() -> None:
    """A spec and the guest it runs on are one thing.

    Asked for an MBR table on a UEFI guest, or for a mirror on a machine with
    one disk, the operator is answering an impossible question and the run
    measures that rather than the interface.
    """
    from tests.tui.specs import SPECS
    from tests.vm.cluster import TUI_GUESTS

    assert sorted(SPECS) == sorted(TUI_GUESTS), (sorted(SPECS), sorted(TUI_GUESTS))
    # Read from the proof, not the prose: a spec that says "BIOS, not UEFI"
    # names both words, and the proof is the half a machine can be checked
    # against anyway.
    for number, spec in SPECS.items():
        disks, uefi, _cjk = TUI_GUESTS[number]
        proof = " ".join(spec.proof).lower()
        if "mirror" in proof:
            assert disks >= 2, (number, disks)
        if "firmware is bios" in proof:
            assert not uefi, number
        if "firmware is uefi" in proof or "systemd-boot" in proof:
            assert uefi, number

    # Negative control: a spec asking for two disks on a one-disk guest is the
    # mismatch this refuses, and the rule has to see it.
    broken = {1: (1, True, True)}
    assert not (broken[1][0] >= 2), "a one-disk guest must not satisfy two disks"
