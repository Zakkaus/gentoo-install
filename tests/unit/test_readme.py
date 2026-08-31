# SPDX-License-Identifier: GPL-2.0-or-later
"""Keep the five concise READMEs and English reference aligned."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[2]

READMES = {
    "README.md": "en",
    "README.zh-TW.md": "zh-TW",
    "README.zh-CN.md": "zh-CN",
    "README.ja.md": "ja",
    "README.ko.md": "ko",
}
REFERENCE = "REFERENCE.md"
SWITCHER = tuple(READMES)
MENU_PICTURES = {
    "README.md": "screenshot-en.png",
    "README.zh-TW.md": "screenshot-zh-TW.png",
    "README.zh-CN.md": "screenshot-zh-CN.png",
    "README.ja.md": "screenshot-ja.png",
    "README.ko.md": "screenshot-ko.png",
}
SECTIONS = {
    "README.md": (
        "Capabilities at a glance",
        "Verification status",
        "Requirements",
        "Safety",
        "Installation",
        "Configuration files",
        "Resuming an interrupted run",
        "Binary packages",
        "Reference",
        "Contributing",
        "License",
    ),
    "README.zh-TW.md": (
        "\u529f\u80fd\u6982\u8981",
        "\u9a57\u8b49\u72c0\u614b",
        "\u9700\u6c42",
        "\u5b89\u5168\u4e8b\u9805",
        "\u5b89\u88dd",
        "\u8a2d\u5b9a\u6a94",
        "\u5f9e\u4e2d\u65b7\u8655\u7e7c\u7e8c",
        "\u4e8c\u9032\u4f4d\u5957\u4ef6",
        "\u53c3\u8003\u8cc7\u6599",
        "\u53c3\u8207\u958b\u767c",
        "\u6388\u6b0a",
    ),
    "README.zh-CN.md": (
        "\u529f\u80fd\u6982\u8981",
        "\u9a8c\u8bc1\u72b6\u6001",
        "\u8981\u6c42",
        "\u5b89\u5168\u4e8b\u9879",
        "\u5b89\u88c5",
        "\u914d\u7f6e\u6587\u4ef6",
        "\u4ece\u4e2d\u65ad\u5904\u7ee7\u7eed",
        "\u4e8c\u8fdb\u5236\u8f6f\u4ef6\u5305",
        "\u53c2\u8003\u8d44\u6599",
        "\u53c2\u4e0e\u5f00\u53d1",
        "\u8bb8\u53ef",
    ),
    "README.ja.md": (
        "\u6a5f\u80fd\u306e\u6982\u8981",
        "\u691c\u8a3c\u72b6\u6cc1",
        "\u8981\u4ef6",
        "\u5b89\u5168\u4e0a\u306e\u6ce8\u610f",
        "\u30a4\u30f3\u30b9\u30c8\u30fc\u30eb",
        "\u8a2d\u5b9a\u30d5\u30a1\u30a4\u30eb",
        "\u4e2d\u65ad\u3057\u305f\u5b9f\u884c\u306e\u518d\u958b",
        "\u30d0\u30a4\u30ca\u30ea\u30d1\u30c3\u30b1\u30fc\u30b8",
        "\u30ea\u30d5\u30a1\u30ec\u30f3\u30b9",
        "\u958b\u767a\u3078\u306e\u53c2\u52a0",
        "\u30e9\u30a4\u30bb\u30f3\u30b9",
    ),
    "README.ko.md": (
        "\uae30\ub2a5 \uac1c\uc694",
        "\uac80\uc99d \uc0c1\ud0dc",
        "\uc694\uad6c \uc0ac\ud56d",
        "\uc548\uc804",
        "\uc124\uce58",
        "\uc124\uc815 \ud30c\uc77c",
        "\uc911\ub2e8\ub41c \uc2e4\ud589 \uc7ac\uac1c",
        "\ubc14\uc774\ub108\ub9ac \ud328\ud0a4\uc9c0",
        "\ucc38\uc870 \ubb38\uc11c",
        "\uae30\uc5ec",
        "\ub77c\uc774\uc120\uc2a4",
    ),
}
FACT_UNITS = (
    "identity",
    "capability-summary",
    "verification-scope",
    "verification-architecture",
    "requirements-runtime",
    "safety-destructive",
    "safety-review-backup",
    "install-download",
    "install-terminal",
    "install-config-workflow",
    "install-root-shell",
    "configuration-reference",
    "resume-limits",
    "binary-packages",
    "reference",
    "contributing",
    "license",
)
REFERENCE_SECTIONS = (
    "Runtime requirements",
    "Command line",
    "Memory environment",
    "In-place conversion",
    "Capabilities",
    "Validation",
    "Configuration files",
    "Binary packages",
    "Exit codes",
)
SECOND_PERSON = {
    "en": re.compile(r"\b(?:you|your|yours)\b", re.IGNORECASE),
    "zh-TW": re.compile(r"[\u4f60\u59b3\u60a8]|\u8acb"),
    "zh-CN": re.compile(r"[\u4f60\u60a8]|\u8bf7"),
    "ja": re.compile(r"\u3042\u306a\u305f|\u304f\u3060\u3055\u3044"),
    "ko": re.compile(r"\ub2f9\uc2e0|\ud558\uc138\uc694|\ud558\uc2ed\uc2dc\uc624"),
}


def document(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def fact_bodies(name: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(
        r"<!-- fact: ([a-z0-9-]+) -->\n\n(.*?)(?=\n\n<!-- fact:|\n\n## |\Z)",
        document(name),
        re.S,
    ):
        found[match.group(1)] = match.group(2).strip()
    return found


def shell_commands(name: str) -> list[str]:
    commands: list[str] = []
    for block in re.findall(r"```sh\n(.*?)```", document(name), re.S):
        for line in block.splitlines():
            command = line.split("#", 1)[0].strip()
            if command:
                commands.append(command)
    return commands


def links(name: str) -> set[str]:
    return set(re.findall(r"\]\(([^)#]+)", document(name)))


def test_every_readme_exists_links_to_all_translations_and_shows_its_menu() -> None:
    for name in READMES:
        first = document(name).splitlines()[0]
        for other in SWITCHER:
            if other == name:
                assert f"({other})" not in first, name
            else:
                assert f"({other})" in first, f"{name} does not link to {other}"

        picture = MENU_PICTURES[name]
        assert (ROOT / picture).is_file(), picture
        assert f"({picture})" in document(name), name
        assert "(cjk-console.png)" not in document(name), name


def test_every_readme_has_the_same_concise_factual_shape() -> None:
    for name, expected in SECTIONS.items():
        assert tuple(re.findall(r"^## (.+)$", document(name), re.M)) == expected, name
        bodies = fact_bodies(name)
        assert tuple(bodies) == FACT_UNITS, name
        assert all(bodies.values()), name


def test_readme_facts_preserve_safety_configuration_and_verification_boundaries() -> None:
    for name in READMES:
        bodies = fact_bodies(name)
        assert "TESTED.md" in bodies["verification-scope"], name
        assert "tests/fixtures/" in bodies["verification-scope"], name
        assert "`0`" in bodies["verification-scope"], name
        # An architecture row without a record is implemented, not verified,
        # and belongs here rather than in the capability list.
        architecture = bodies["verification-architecture"]
        for named in ("amd64", "arm64", "x86", "TESTED.md", "tests/vm/"):
            assert named in architecture, (name, named)
        assert "wipe = true" in bodies["safety-destructive"], name
        assert "dry-run" in bodies["safety-review-backup"], name
        assert "/dev/disk/by-id/" in bodies["safety-review-backup"], name
        assert "my-install.toml" in bodies["install-config-workflow"], name
        assert "--no-shell" in bodies["install-root-shell"], name
        assert "REFERENCE.md#configuration-files" in bodies["configuration-reference"], name
        assert "tests/fixtures/vm-binpkg.toml" in bodies["configuration-reference"], name
        assert "--resume" in bodies["resume-limits"], name
        assert "/run/gentoo-install/install.jsonl" in bodies["resume-limits"], name
        assert "binhost" in bodies["binary-packages"], name
        assert "gentoo-zh" in bodies["binary-packages"], name
        assert "REFERENCE.md" in bodies["reference"], name


def test_configuration_workflow_remains_safe_and_identical_across_locales() -> None:
    english = shell_commands("README.md")
    assert english == [
        "curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz",
        "cd gentoo-install-master",
        "./bootstrap.sh",
        "./bootstrap.sh --config my-install.toml --dry-run",
        "./bootstrap.sh --config my-install.toml",
        "./bootstrap.sh --config my-install.toml --no-shell",
        "./bootstrap.sh --config my-install.toml --resume",
    ]
    for name in READMES:
        assert shell_commands(name) == english, name
        workflow = fact_bodies(name)["install-config-workflow"]
        assert workflow.index("--dry-run") < workflow.index("./bootstrap.sh --config my-install.toml\n")


def test_reference_holds_the_moved_lookup_material() -> None:
    text = document(REFERENCE)
    assert not re.search(r"[\u4e00-\u9fff]", text)
    assert tuple(re.findall(r"^## (.+)$", text, re.M)) == REFERENCE_SECTIONS
    for required in (
        "packages.gentoo.org",
        "--disarm",
        'mode = "in-place"',
        "gentoo_install/model/compat.py",
        "config_version",
        "getuto",
        "`5`",
    ):
        assert required in text, required


def test_all_embedded_toml_examples_parse_and_validate() -> None:
    from gentoo_install.model.parse import parse
    from gentoo_install.model.validate import validate

    for name in (*READMES, REFERENCE):
        blocks = re.findall(r"```toml\n(.*?)```", document(name), re.S)
        if name == REFERENCE:
            assert blocks, "REFERENCE.md carries the published TOML examples"
        for block in blocks:
            validate(parse(tomllib.loads(block)))


def test_every_documented_long_option_exists_in_cli_help() -> None:
    from gentoo_install.cli import parser

    documented = set(re.findall(r"--[a-z][a-z-]*", document(REFERENCE)))
    documented.update(re.findall(r"--[a-z][a-z-]*", document("README.md")))
    cli_help = parser().format_help()
    assert documented
    assert all(option in cli_help for option in documented), documented


def test_every_relative_link_resolves() -> None:
    for name in (*READMES, REFERENCE):
        for target in links(name):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (ROOT / target).exists(), f"{name} links to {target}"


def test_readmes_do_not_address_the_reader_or_repeat_contributor_guidance() -> None:
    assert (ROOT / "CONTRIBUTING.md").is_file()
    for name, locale in READMES.items():
        text = document(name)
        for number, line in enumerate(text.splitlines(), 1):
            assert not SECOND_PERSON[locale].search(line), f"{name}:{number}"
        assert "mypy" not in text, name
        assert "pytest" not in text, name


def test_tested_record_holds_the_detail_the_readmes_do_not_repeat() -> None:
    record = document("TESTED.md")
    for heading in ("## Mode 1", "## Mode 2", "## Mode 3"):
        assert heading in record, heading
    assert "post-boot" in record
    for name in READMES:
        body = fact_bodies(name)["verification-scope"]
        assert "b931ef46" not in body, name
        assert "\n|" not in body, name


def test_the_chinese_readmes_space_code_spans_away_from_han_text() -> None:
    """A code span written flush against a Han character reads as one word.

    `chinese_lint.py` does not see this, so the rule lives here. The check it
    replaces named five literal fragments, four of which described prose the
    compact README no longer carries.
    """
    han = "[\u3400-\u4dbf\u4e00-\u9fff]"
    before = re.compile(han + r"`")
    after = re.compile(r"`" + han)
    for name in ("README.zh-TW.md", "README.zh-CN.md"):
        said = document(name)
        assert not before.search(said), (name, before.search(said))
        assert not after.search(said), (name, after.search(said))


def test_no_chinese_readme_calls_arming_a_boot_by_the_weapon_word() -> None:
    """The Chinese word for arming a weapon is not the word for a one-shot
    boot entry. The prose that used it moved to the English `REFERENCE.md`,
    and this keeps it from coming back."""
    for name in ("README.zh-TW.md", "README.zh-CN.md"):
        said = document(name)
        assert "\u6b66\u88dd" not in said, name
        assert "\u6b66\u88c5" not in said, name


#: How each locale says the run does not write there. The verb, not the word
#: for a disk: every one of these bodies already names a disk in
#: `/dev/disk/by-id/` and in the sentence about selectors, so matching that
#: would pass on the wording this rule exists to refuse. A safety claim is
#: the one place a per-locale table earns its keep, because prose cannot be
#: compared across five languages and the alternative was no check at all.
UNWRITTEN: Final[dict[str, str]] = {
    "README.md": "does not write",
    "README.zh-TW.md": "\u4e0d\u6703\u5beb\u5165",
    "README.zh-CN.md": "\u4e0d\u4f1a\u5199\u5165",
    "README.ja.md": "\u66f8\u304d\u8fbc\u307e\u306a\u3044",
    "README.ko.md": "\uc4f0\uc9c0 \uc54a\ub294",
}


def test_every_readme_says_where_the_backup_has_to_be() -> None:
    """Three of the five said the backup must be off the selected disk and
    two said only `a separate backup`.

    English is the source document and it was one of the weaker two, so the
    weaker wording was the one being translated from. A backup on a second
    partition of the disk being wiped satisfies `separate` and is destroyed:
    `preflight.py` counts a wiped disk as at risk in exactly that sense.

    All five now say the backup is on a disk the run does not write.
    """
    for name in READMES:
        body = fact_bodies(name)["safety-review-backup"]
        assert UNWRITTEN[name] in body, (name, body)
