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
