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


def test_security_says_when_the_staged_passphrase_goes() -> None:
    """The document said the installer does not delete it before the reboot.

    `apply` clears the staged keys in a `finally`, so they are gone when the
    run ends whether it finished or stopped, and before the root shell it
    offers. A security document that understates its own protection is still
    wrong, and the sentence would have stayed wrong if the clean-up were
    removed.
    """
    import ast
    import inspect

    from gentoo_install.exec import apply as exec_apply
    from gentoo_install.exec.preflight import SecretStore
    from gentoo_install.model.device import DeviceId

    said = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    paragraph = next(part for part in said.split("\n- ") if "staged in a file" in part)
    assert "finally" in paragraph, paragraph
    assert "0600" in paragraph, paragraph

    # The path the document names is the one the store writes.
    where = SecretStore(Path("/run/gentoo-install")).path(DeviceId("one"))
    assert str(where.parent) in paragraph, (where, paragraph)

    # The clean-up is in a `finally`, which is what makes the sentence true.
    tree = ast.parse(inspect.getsource(exec_apply.apply))
    cleaned = [
        node
        for outer in ast.walk(tree)
        if isinstance(outer, ast.Try)
        for node in ast.walk(ast.Module(body=outer.finalbody, type_ignores=[]))
        if isinstance(node, ast.Attribute) and node.attr == "cleanup_secrets"
    ]
    assert cleaned, ast.dump(tree)[:200]

def test_the_reference_lists_every_kernel_source_and_the_real_exit_codes() -> None:
    """Two tables in `REFERENCE.md` were written once and not kept.

    The kernel table names four of the five `KernelSource` members: `xanmod`
    is offered on the Kernel screen and merges `sys-kernel/xanmod-kernel`, so
    the reference described a choice the installer has as one it does not.

    The exit-code table gave `2` to an argparse usage error. `cli.py`
    translates the parser's `SystemExit(2)` to `EXIT_CONFIG`, so the process
    exits `1` and `2` is preflight alone. Both are read off the code here
    rather than repeated.
    """
    import subprocess
    import sys

    from gentoo_install.cli import EXIT_CONFIG, EXIT_PREFLIGHT
    from gentoo_install.model.compat import KERNEL_PACKAGES

    said = (ROOT / "REFERENCE.md").read_text(encoding="utf-8")

    assert len(KERNEL_PACKAGES) >= 5, sorted(one.value for one in KERNEL_PACKAGES)
    for source, package in KERNEL_PACKAGES.items():
        assert f"`{source.value}`" in said, source.value
        assert f"`{package.atom}`" in said, package.atom

    # What the parser actually does, not what the table remembers.
    refused = subprocess.run(
        [sys.executable, "-m", "gentoo_install", "--an-option-that-does-not-exist"],
        capture_output=True,
        cwd=ROOT,
    )
    assert refused.returncode == EXIT_CONFIG, refused.returncode
    assert f"| `{EXIT_CONFIG}` | configuration error" in said, said[:0]
    assert f"| `{EXIT_PREFLIGHT}` | preflight failure |" in said


def test_the_memory_mode_section_does_not_claim_more_than_it_records() -> None:
    """Two sentences in one section contradicted each other.

    It opened with `every path in it has a record` and closed with `What has
    no record yet:` naming two of them. `--disarm` is a real option and
    appeared nowhere in the file at all. This is the document `README.md`
    points at for the verification boundary, so an over-claim here is the
    expensive kind.
    """
    from gentoo_install.cli import parser

    said = (ROOT / "TESTED.md").read_text(encoding="utf-8")
    section = said[said.index("## Mode 3") : said.index("## The interface alone")]

    assert "every path in it has a record" not in section, section[:200]
    unrecorded = section[section.index("What has no record yet") :]

    memory = {"--ram", "--lowram", "--bypass", "--disarm"}
    offered = {one for action in parser()._actions for one in action.option_strings}
    assert memory <= offered, sorted(memory - offered)
    for flag in sorted(memory):
        assert f"`{flag}`" in section, flag
    # The one no run has exercised is named where the gaps are named.
    assert "`--disarm`" in unrecorded, unrecorded[:200]


def test_the_memory_example_carries_the_configuration_arming_needs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The example armed nothing and the sentence beside it said so wrongly.

    It read `bootstrap.sh --ram --ssh-key ... --root-password ...` with no
    `--config`, under a bullet saying there is no interface on this path. A
    memory mode arms whatever configuration it was given, so without one the
    run either opens the menu or refuses.
    """
    import os
    import shlex

    from gentoo_install.cli import EXIT_PREFLIGHT, main, parser

    said = (ROOT / "REFERENCE.md").read_text(encoding="utf-8")
    block = said[said.index("```sh", said.index("## Memory environment")) :]
    printed = block[block.index("\n") + 1 : block.index("```", 3)]
    armed = next(line for line in printed.splitlines() if "--ram" in line)

    written = shlex.split(armed)
    assert written[0] == "./bootstrap.sh", written
    arguments = parser().parse_args(written[1:])
    assert arguments.config is not None, armed

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    without = [one for one in written[1:] if one not in ("--config", arguments.config)]
    assert main([*without, "--no-shell"]) == EXIT_PREFLIGHT
    assert "an unattended run needs --config FILE" in capsys.readouterr().err


def test_the_wifi_sentence_is_marked_as_unrecorded_while_it_is() -> None:
    """`REFERENCE.md` stated a wireless install as something that happens.

    Nothing in the Mode 3 records has a wireless adapter, so the sentence was
    the design speaking. The two documents are held together here: while the
    records name no `nmcli`, the reference says the gap is a gap and
    `TESTED.md` lists it among what has no record.
    """
    reference = (ROOT / "REFERENCE.md").read_text(encoding="utf-8")
    tested = (ROOT / "TESTED.md").read_text(encoding="utf-8")

    section = tested[tested.index("## Mode 3") : tested.index("## The interface alone")]
    gaps = section.index("What has no record yet")
    recorded, unrecorded = section[:gaps], section[gaps:]
    if "nmcli" in recorded:
        return
    assert "wifi" in unrecorded, unrecorded[:400]

    said = next(line for line in reference.splitlines() if "nmcli" in line)
    assert "No run in `TESTED.md`" in said, said


def test_every_derived_file_names_its_source_where_a_reader_arrives() -> None:
    """`CREDITS.md` promised a statement at the top of each derived file.

    `tests/vm/cluster.py` carried its shadow provenance beside the constant,
    3800 lines down, where nobody opening the file sees it. The ledger's own
    reason is that a reader of the file cannot be assumed to have read the
    ledger, so the rows are read here and the files checked against them.
    """
    said = (ROOT / "CREDITS.md").read_text(encoding="utf-8")
    table = said[said.index("| File here |") : said.index("## Projects read")]
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in table.splitlines()
        if line.startswith("| `")
    ]
    assert rows, table

    for here, project, *_ in rows:
        path = ROOT / here.strip("`")
        assert path.exists(), here
        opening = "\n".join(path.read_text(encoding="utf-8").splitlines()[:12])
        name = project.split("[")[-1].split("]")[0] if "[" in project else project
        assert name in opening, f"{here} does not name {name} at its top"


def test_contributing_does_not_forbid_what_it_later_permits() -> None:
    """Its first paragraph said reference code must not be copied.

    Eighty lines down it sets out when code may be taken and what crediting
    it costs, and `CREDITS.md` records one such derivation. A contributor who
    reads only the opening turns down reuse the project allows, so the
    opening sends them to the section that decides it.
    """
    said = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    opening = said[: said.index("## Architecture")]

    assert "## Derived code" in said, said[:200]
    assert "[Derived code](#derived-code)" in opening, opening
