# SPDX-License-Identifier: GPL-2.0-or-later
"""The documents a contributor reads before the code: SECURITY and the code of
conduct. What they claim about the program is checked against the program."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from gentoo_install.model.config import ProxyConfig
from gentoo_install.model.serialise import NOT_FOR_A_PASTE, SECRET

ROOT = Path(__file__).resolve().parents[2]

#: The address every report goes to. One address, so a document that names a
#: different one sends a vulnerability report nobody reads.
CONTACT = "zakk@gentoozh.org"

DOCUMENTS = ("SECURITY.md", "CODE_OF_CONDUCT.md")


@pytest.mark.parametrize("name", DOCUMENTS)
def test_a_report_has_somewhere_to_go(name: str) -> None:
    said = (ROOT / name).read_text(encoding="utf-8")
    assert CONTACT in said, f"{name} names no address to report to"


def test_security_names_what_publishing_actually_removes() -> None:
    """`SECURITY.md` tells an operator which values publishing takes out. The
    list is `serialise.SECRET` plus the two proxy credentials, and a field
    added to one and not the other leaves the document promising a protection
    that is not there.

    The paragraph is found by the redaction it describes rather than by a
    flag name: it was found by `--paste`, which is not an option this
    installer has, so the document and the test agreed on something the
    operator cannot type."""
    said = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    paragraph = next(
        part for part in said.split("\n- ") if "Publishing a\n  configuration" in part
    )

    # Both tables: `NOT_FOR_A_PASTE` was added without a document guard of its
    # own, which is the same gap the redaction itself was closing.
    for name in (*SECRET, *NOT_FOR_A_PASTE):
        assert f"`{name}`" in paragraph, name
    dropped = {"username", "password"}
    assert dropped <= {field.name for field in fields(ProxyConfig)}, "the model moved"
    for name in dropped:
        assert name in paragraph, name


def test_the_issue_form_asks_for_files_that_exist() -> None:
    """The form names where a run's record is. A path or a filename that has
    moved sends every reporter to an empty directory, and the report that
    comes back cannot be reproduced."""
    from gentoo_install.exec.report import LOG_DIRECTORY, RunFile

    form = (ROOT / ".github" / "ISSUE_TEMPLATE" / "an-install-failed.yml").read_text(
        encoding="utf-8"
    )
    assert str(LOG_DIRECTORY) in form, LOG_DIRECTORY
    for held in RunFile:
        assert held.value in form, held.value

    # And what it promises the publish action removes.
    for name in SECRET:
        assert name in form, name


def test_the_issue_form_sends_a_vulnerability_somewhere_else() -> None:
    """An installer that runs as root gets reports that should not be public
    until they are fixed. The link is the only thing standing between a
    reporter and the issue tracker."""
    config = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8")
    assert "SECURITY.md" in config, config


def test_the_reference_documents_name_only_options_the_parser_has() -> None:
    """`SECURITY.md` described `--paste`, and no such option exists.

    Publishing a configuration is a menu action and the log offer is the
    closing question, so an operator reading that line had a flag to type
    that `--help` does not list. Only these two documents describe this
    installer's own command line; `CONTRIBUTING.md` and `TESTED.md` name
    options of `tests/vm/run.py` and of `emerge`.
    """
    import re

    from gentoo_install.cli import parser

    offered = {one for action in parser()._actions for one in action.option_strings}
    offered.add("--help")
    assert len(offered) > 10, sorted(offered)

    unknown: list[str] = []
    for name in ("REFERENCE.md", "SECURITY.md"):
        said = (ROOT / name).read_text(encoding="utf-8")
        for found in re.finditer(r"--[a-z][a-z0-9-]*", said):
            if found.group(0) not in offered:
                unknown.append(f"{name}: {found.group(0)}")
    assert unknown == [], unknown


def test_every_fixture_the_record_names_still_exists() -> None:
    """`TESTED.md` is what `README.md` points at for the verification boundary.

    A row names the fixtures a round exercised. A fixture renamed or removed
    afterwards leaves the row naming a file nobody can read, and the row keeps
    reading as evidence: this is the record, so it has to be checkable against
    the tree rather than only against memory.

    Only names that look like a fixture are checked. A row also holds package
    atoms, option names and command output in backticks, and a rule that took
    every backticked word would be a rule nobody could keep green.
    """
    import re

    said = (ROOT / "TESTED.md").read_text(encoding="utf-8")
    have = {path.stem for path in (ROOT / "tests" / "fixtures").rglob("*.toml")}
    assert len(have) > 20, sorted(have)

    named: set[str] = set()
    for line in said.splitlines():
        if not line.startswith("| `"):
            continue
        for found in re.finditer(r"`([a-z0-9][a-z0-9-]*)`", line):
            word = found.group(1)
            if word.startswith("vm-") or word in have:
                named.add(word)
    assert len(named) > 20, sorted(named)
    assert sorted(named - have) == [], sorted(named - have)
