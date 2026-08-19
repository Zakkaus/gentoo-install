# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import json
from pathlib import Path

from gentoo_install.exec.runner import Runner
from gentoo_install.log import Journal, Source, merged

EMERGE_OUTPUT = """
These are the packages that would be merged, in order:

[binary   R    ] sys-apps/portage-3.0.70::gentoo
[ebuild  N     ] dev-vcs/git-2.51.0::gentoo  USE="curl -cgi"
[binary  N     ] sys-kernel/gentoo-kernel-bin-6.18.43::gentoo
"""


def test_a_binary_and_a_compiled_package_are_told_apart() -> None:
    found = dict(merged(EMERGE_OUTPUT))
    assert found["sys-apps/portage-3.0.70::gentoo"] is Source.BINARY
    assert found["dev-vcs/git-2.51.0::gentoo"] is Source.COMPILED
    assert found["sys-kernel/gentoo-kernel-bin-6.18.43::gentoo"] is Source.BINARY


def test_the_journal_is_one_json_object_per_line(tmp_path: Path) -> None:
    journal = Journal(path=tmp_path / "install.jsonl")
    journal.operation("portage", "sync repository gentoo", 12.5)
    journal.degraded("binhost", "the host answered 404, so the packages are compiled")
    written = [json.loads(line) for line in (tmp_path / "install.jsonl").read_text().splitlines()]
    assert written[0]["event"] == "operation" and written[0]["seconds"] == 12.5
    assert written[1]["event"] == "degraded" and "404" in written[1]["reason"]


def test_a_run_can_be_asked_how_much_it_compiled(tmp_path: Path) -> None:
    journal = Journal(path=tmp_path / "install.jsonl")
    journal.packages(EMERGE_OUTPUT)
    assert journal.counts() == {"binary": 2, "compiled": 1}


def test_every_command_reaches_the_journal_with_its_exit_code(tmp_path: Path) -> None:
    journal = Journal(path=tmp_path / "install.jsonl")
    runner = Runner(log=lambda line: None, journal=journal)
    runner.run(["true"])
    runner.run(["false"], check=False)
    codes = [entry["returncode"] for entry in journal.entries if entry["event"] == "command"]
    assert codes == [0, 1]


def test_a_chrooted_runner_writes_to_the_same_journal(tmp_path: Path) -> None:
    journal = Journal(path=tmp_path / "install.jsonl")
    runner = Runner(log=lambda line: None, journal=journal)
    assert runner.in_target(Path("/mnt/gentoo")).journal is journal


def test_a_journal_carries_what_the_run_was(tmp_path: Path) -> None:
    """`--resume` skips operations by position and description, so a journal
    from another configuration would skip the wrong ones, one from before a
    reboot would skip operations whose result the reboot discarded, and one
    from another installer would skip whatever now sits at those positions.
    The journal had no way to say which run wrote it."""
    from gentoo_install.log import Journal

    journal = Journal(path=tmp_path / "install.jsonl")
    assert journal.identity() is None, "a journal that recorded nothing claims nothing"

    written = {"configuration": "abc123", "session": "a-boot-id", "installer": "def456"}
    journal.started(**written)
    journal.operation(stage="partition", described="do something", seconds=1.0)
    assert journal.identity() == written

    # Read back from the file, not from memory: a resumed run is a new process.
    assert Journal(path=tmp_path / "install.jsonl").identity() == written


def test_an_identity_entry_missing_a_field_is_not_an_identity(tmp_path: Path) -> None:
    """A line written by a future version, or a partial one from a run killed
    mid-write, must not read as an identity that happens to match."""
    import json

    from gentoo_install.log import Journal

    path = tmp_path / "install.jsonl"
    for missing in Journal.IDENTITY:
        entry = {"event": "started"} | {
            name: "held" for name in Journal.IDENTITY if name != missing
        }
        path.write_text(json.dumps(entry) + "\n")
        assert Journal(path=path).identity() is None, missing


def test_a_pretend_is_not_a_merge(tmp_path: Path) -> None:
    """`VerifyPackages` runs `emerge --pretend --quiet` before anything is
    installed and it prints the same lines the merge does. Measured on a
    guest: 31 lines from the pretend and 32 from the merge, and the journal
    held all 63 as installed packages."""
    journal = Journal(path=tmp_path / "install.jsonl")
    runner = Runner(log=lambda line: None, journal=journal)
    runner.run(["sh", "-c", "cat", "emerge", "--pretend", "--quiet"], input_text=EMERGE_OUTPUT)
    assert journal.counts() == {}, journal.counts()
    # The control: the same output from a command that is not a pretend is
    # recorded, so this is a rule about the flag rather than about `sh`.
    runner.run(["sh", "-c", "cat", "emerge", "--verbose"], input_text=EMERGE_OUTPUT)
    assert journal.counts() == {"binary": 2, "compiled": 1}, journal.counts()

