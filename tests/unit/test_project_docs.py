# SPDX-License-Identifier: GPL-2.0-or-later
"""The documents a contributor reads before the code: SECURITY and the code of
conduct. What they claim about the program is checked against the program."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from gentoo_install.model.config import ProxyConfig
from gentoo_install.model.serialise import SECRET

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
    """`SECURITY.md` tells an operator which values `--paste` takes out. The
    list is `serialise.SECRET` plus the two proxy credentials, and a field
    added to one and not the other leaves the document promising a protection
    that is not there."""
    said = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    paragraph = next(part for part in said.split("\n- ") if "--paste" in part)

    for name in SECRET:
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
