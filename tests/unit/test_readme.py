# SPDX-License-Identifier: GPL-2.0-or-later
"""The README set is five files that have to move together.

Five translations drift the moment one of them is edited alone, and the drift
is invisible to a reader who reads only one of them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Every translation, and the language each one is written in.
READMES = {
    "README.md": "en",
    "README.zh-TW.md": "zh-TW",
    "README.zh-CN.md": "zh-CN",
    "README.ja.md": "ja",
    "README.ko.md": "ko",
}

#: What the top of each file has to offer, so a reader who lands on any one of
#: them can reach the other four.
SWITCHER = tuple(READMES)

#: Both pictures, in both files, at the top. A README that lost one of them
#: still reads as complete.
#: One menu capture per locale, so a reader sees the interface in the
#: language the file they opened is written in.
PICTURES = (
    "screenshot-en.png",
    "screenshot-zh-TW.png",
    "screenshot-zh-CN.png",
    "screenshot-ja.png",
    "screenshot-ko.png",
    "cjk-console.png",
)

#: Which capture each file shows, so the interface is in the language the
#: reader chose by opening that file.
MENU_PICTURES = {
    "README.md": "screenshot-en.png",
    "README.zh-TW.md": "screenshot-zh-TW.png",
    "README.zh-CN.md": "screenshot-zh-CN.png",
    "README.ja.md": "screenshot-ja.png",
    "README.ko.md": "screenshot-ko.png",
}

SECTIONS = {
    "README.md": (
        "Capabilities", "Verification status", "Requirements", "Safety", "Installation",
        "Resuming an interrupted run", "Configuration files", "Binary packages", "Exit codes",
        "Questions", "Contributing", "License",
    ),
    "README.zh-TW.md": (
        "\u529f\u80fd", "\u9a57\u8b49\u72c0\u614b", "\u9700\u6c42", "\u5b89\u5168\u4e8b\u9805",
        "\u5b89\u88dd", "\u5f9e\u4e2d\u65b7\u8655\u7e7c\u7e8c", "\u8a2d\u5b9a\u6a94",
        "\u4e8c\u9032\u4f4d\u5957\u4ef6", "\u9000\u51fa\u78bc", "\u5e38\u898b\u554f\u984c", "\u53c3\u8207\u958b\u767c",
        "\u6388\u6b0a",
    ),
    "README.zh-CN.md": (
        "\u529f\u80fd", "\u9a8c\u8bc1\u72b6\u6001", "\u8981\u6c42", "\u5b89\u5168\u4e8b\u9879",
        "\u5b89\u88c5", "\u4ece\u4e2d\u65ad\u5904\u7ee7\u7eed", "\u914d\u7f6e\u6587\u4ef6",
        "\u4e8c\u8fdb\u5236\u8f6f\u4ef6\u5305", "\u9000\u51fa\u7801", "\u5e38\u89c1\u95ee\u9898", "\u53c2\u4e0e\u5f00\u53d1",
        "\u8bb8\u53ef",
    ),
    "README.ja.md": (
        "\u6a5f\u80fd", "\u691c\u8a3c\u72b6\u6cc1", "\u8981\u4ef6", "\u5b89\u5168\u4e0a\u306e\u6ce8\u610f",
        "\u30a4\u30f3\u30b9\u30c8\u30fc\u30eb", "\u4e2d\u65ad\u3057\u305f\u5b9f\u884c\u306e\u518d\u958b",
        "\u8a2d\u5b9a\u30d5\u30a1\u30a4\u30eb", "\u30d0\u30a4\u30ca\u30ea\u30d1\u30c3\u30b1\u30fc\u30b8",
        "\u7d42\u4e86\u30b3\u30fc\u30c9", "\u3088\u304f\u3042\u308b\u8cea\u554f",
        "\u958b\u767a\u3078\u306e\u53c2\u52a0",
        "\u30e9\u30a4\u30bb\u30f3\u30b9",
    ),
    "README.ko.md": (
        "\uae30\ub2a5", "\uac80\uc99d \uc0c1\ud0dc", "\uc694\uad6c \uc0ac\ud56d", "\uc548\uc804",
        "\uc124\uce58", "\uc911\ub2e8\ub41c \uc2e4\ud589 \uc7ac\uac1c", "\uc124\uc815 \ud30c\uc77c",
        "\ubc14\uc774\ub108\ub9ac \ud328\ud0a4\uc9c0", "\uc885\ub8cc \ucf54\ub4dc",
        "\uc790\uc8fc \ubb3b\ub294 \uc9c8\ubb38", "\uae30\uc5ec",
        "\ub77c\uc774\uc120\uc2a4",
    ),
}

FACT_UNITS = (
    "identity", "capability-scope", "storage-device-graph", "zram-system",
    "in-place-conversion", "prepared-image", "boot-system", "remote-unlock",
    "desktop-language", "portage", "proxy",
    "memory-environment", "memory-environment-access",
    "plan-records", "verification-scope",
    "requirements-runtime",
    "requirements-version-sources", "requirements-network-filter", "requirements-bootstrap",
    "safety-destructive", "safety-review-backup", "install-download", "install-terminal",
    "install-config-workflow", "install-root-shell", "resume-behavior", "resume-limits",
    "config-model", "config-fixtures", "config-dry-run", "binary-packages", "exit-codes",
    "faq-customisation", "contributing", "license",
)

SECOND_PERSON = {
    "en": re.compile(r"\b(?:you|your|yours)\b", re.IGNORECASE),
    "zh-TW": re.compile(r"[\u4f60\u59b3\u60a8]|\u8acb"),
    "zh-CN": re.compile(r"[\u4f60\u60a8]|\u8bf7"),
    "ja": re.compile(r"\u3042\u306a\u305f|\u304f\u3060\u3055\u3044"),
    "ko": re.compile(r"\ub2f9\uc2e0|\ud558\uc138\uc694|\ud558\uc2ed\uc2dc\uc624"),
}


def readme(name: str) -> str:
    return (ROOT / name).read_text()


def fact_bodies(name: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(
        r"<!-- fact: ([a-z0-9-]+) -->\n\n(.*?)(?=\n\n<!-- fact:|\n\n## |\Z)",
        readme(name),
        re.S,
    ):
        found[match.group(1)] = match.group(2).strip()
    return found


def test_every_readme_exists_and_links_to_all_the_others() -> None:
    for name in READMES:
        first = (ROOT / name).read_text().splitlines()[0]
        for other in SWITCHER:
            if other == name:
                continue
            assert f"({other})" in first, f"{name} does not link to {other}"


def test_no_readme_links_to_itself() -> None:
    """A self-link reads as another translation and goes nowhere."""
    for name in READMES:
        first = (ROOT / name).read_text().splitlines()[0]
        assert f"({name})" not in first, name


def test_both_pictures_are_in_every_readme_and_on_disk() -> None:
    for picture in PICTURES:
        assert (ROOT / picture).is_file(), picture
    for name in READMES:
        said = (ROOT / name).read_text()
        # The menu capture is the one in this file's own language: a reader
        # who opened the Korean file is shown the Korean interface.
        assert f"({MENU_PICTURES[name]})" in said, f"{name} is missing its own menu capture"
        assert "(cjk-console.png)" in said, f"{name} is missing cjk-console.png"


def test_every_readme_carries_the_complete_ordered_sections() -> None:
    for name, expected in SECTIONS.items():
        assert tuple(re.findall(r"^## (.+)$", readme(name), re.M)) == expected, name


def test_every_readme_carries_the_same_nonempty_factual_units() -> None:
    for name in READMES:
        bodies = fact_bodies(name)
        assert tuple(bodies) == FACT_UNITS, name
        assert all(bodies.values()), name


def test_reviewed_cross_locale_claims_stay_attached_to_their_factual_units() -> None:
    common = {
        # The records themselves live in `TESTED.md`, which is one file rather
        # than five: what every README has to carry is the boundary and the
        # pointer, or a reader takes an implemented path for a tested one.
        "verification-scope": ("TESTED.md", "tests/fixtures/", "ext4", "LUKS2"),
        "requirements-version-sources": (
            "packages.gentoo.org", "api.github.com/repos/gentoo-zh/overlay/contents",
            "gitweb.gentoo.org",
        ),
        "portage": ("zh-TW", "zh-CN", "ja", "ko", "en", "gentoo-zh"),
        # Each of these is a name or a number a reader acts on, so a
        # translation that drops one leaves that locale unable to use the
        # path the other four document.
        "storage-device-graph": ("ZFS", "stripe", "mirror", "raidz1", "raidz2", "raidz3"),
        "prepared-image": (
            "disk.image", "disk.size", "disk.source", "disk.destination",
            "`raw`", "`gz`", "`xz`", "`zst`", "`tar`",
        ),
        "boot-system": ("GRUB", "systemd-boot", "ZFSBootMenu"),
        "remote-unlock": (
            "[kernel.remote_unlock]", "222", "dracut-crypt-ssh",
            "system.authorized_keys", "ZFSBootMenu",
        ),
        "desktop-language": ("greetd", "tuigreet"),
        "exit-codes": ("argparse", "bootstrap.sh", "`1`", "`2`"),
    }
    backups = {
        "README.md": "separate backup",
        "README.zh-TW.md": "\u53e6\u6709\u5099\u4efd",
        "README.zh-CN.md": "\u72ec\u7acb\u4e8e\u6240\u9009\u78c1\u76d8\u7684\u5907\u4efd",
        "README.ja.md": "\u9078\u629e\u3057\u305f\u30c7\u30a3\u30b9\u30af\u3068\u306f\u5225\u306e\u30d0\u30c3\u30af\u30a2\u30c3\u30d7",
        "README.ko.md": "\uc120\ud0dd\ud55c \ub514\uc2a4\ud06c\uc640 \ubd84\ub9ac\ub41c \uc704\uce58\uc5d0 \ubcc4\ub3c4 \ubc31\uc5c5",
    }
    install_alternatives = {
        "README.md": "one of the two",
        "README.zh-TW.md": "\u64c7\u4e00",
        "README.zh-CN.md": "\u62e9\u4e00",
        "README.ja.md": "\u3044\u305a\u308c\u304b\u4e00\u65b9",
        "README.ko.md": "\ud558\ub098\ub9cc",
    }
    # The three the installer now compares, in each locale's own words. The
    # sentence used to name what was *not* checked; it names what is.
    resume_identity = {
        "README.md": "same installer and the same configuration file",
        "README.zh-TW.md": "\u540c\u4e00\u500b\u5b89\u88dd\u5668\u8207\u540c\u4e00\u4efd\u8a2d\u5b9a\u6a94",
        "README.zh-CN.md": "\u540c\u4e00\u4e2a\u5b89\u88c5\u7a0b\u5e8f\u4e0e\u540c\u4e00\u4efd\u914d\u7f6e\u6587\u4ef6",
        "README.ja.md": "\u540c\u3058\u30a4\u30f3\u30b9\u30c8\u30fc\u30e9\u30fc\u3001\u540c\u3058\u8a2d\u5b9a\u30d5\u30a1\u30a4\u30eb",
        "README.ko.md": "\ub3d9\uc77c\ud55c \uc124\uce58 \ud504\ub85c\uadf8\ub7a8, \ub3d9\uc77c\ud55c \uc124\uc815 \ud30c\uc77c",
    }
    #: Every locale says the installer stops rather than only documenting it.
    resume_boot_id = dict.fromkeys(resume_identity, "boot id")
    resume_digest_limit = {
        "README.md": "shared helper or constant",
        "README.zh-TW.md": "\u5171\u7528\u8f14\u52a9\u51fd\u5f0f\u6216\u5e38\u6578",
        "README.zh-CN.md": "\u5171\u7528\u8f85\u52a9\u51fd\u6570\u6216\u5e38\u91cf",
        "README.ja.md": "\u5171\u6709\u306e\u30d8\u30eb\u30d1\u30fc\u3084\u5b9a\u6570",
        "README.ko.md": "\uacf5\uc6a9 \ud5ec\ud37c\ub098 \uc0c1\uc218",
    }
    for name in READMES:
        bodies = fact_bodies(name)
        for unit, tokens in common.items():
            assert all(token in bodies[unit] for token in tokens), (name, unit)
        assert backups[name] in bodies["safety-review-backup"], name
        assert install_alternatives[name] in bodies["install-config-workflow"], name
        assert resume_identity[name] in bodies["resume-limits"], name
        assert resume_boot_id[name] in bodies["resume-limits"], name
        assert resume_digest_limit[name] in bodies["resume-limits"], name
        assert "zram" not in bodies["storage-device-graph"], name
        assert "zram" in bodies["zram-system"], name


def test_the_record_file_holds_what_the_readmes_stopped_carrying() -> None:
    """Five copies of a record set drift; one does not. What the READMEs keep
    is the boundary and the pointer, and `TESTED.md` keeps the rows."""
    from pathlib import Path

    record = Path("TESTED.md").read_text(encoding="utf-8")
    for revision in (
        "b931ef46fc15ed50385f70467f2bfb0a8d1fd154",
        "7cf09c2f9d9c",
        "bcc090fab621",
        "71e751cf14a1",
        "0827931289d0",
    ):
        assert revision in record, revision
    # A section for each mode, including the one with nothing in it yet: an
    # absent section reads as an oversight, an empty one as a fact.
    for heading in ("## Mode 1", "## Mode 2", "## Mode 3"):
        assert heading in record, heading
    # Every path in mode 3 has a record, and what is still missing is named
    # where the rows are: a section carrying only records reads as a finished
    # mode, and one carrying only gaps reads as a plan.
    assert "install or shell>" in record, "the record says what came up"
    assert "read it back byte-for-byte" in record, "and what was written"
    assert (
        "a machine that goes on to install Gentoo from inside the environment"
        in " ".join(record.split())
    ), "the boundary of mode 3 is stated where its rows are"

    # And every README points at it rather than repeating it.
    for name in READMES:
        body = fact_bodies(name)["verification-scope"]
        assert "TESTED.md" in body, name
        assert "b931ef46" not in body, f"{name} still carries a record"


def test_proxy_is_documented_in_every_readme_and_the_example_parses() -> None:
    from dataclasses import fields
    import tomllib

    from gentoo_install.model.config import InstallConfig
    from gentoo_install.model.parse import parse
    from gentoo_install.model.validate import validate

    proxy_implemented = any(field.name == "proxy" for field in fields(InstallConfig))
    for name in READMES:
        body = fact_bodies(name)["proxy"]
        assert "socks5h" in body and "bypass" in body and "dry-run" in body, name
        examples = re.findall(r"```toml\n(.*?)```", readme(name), re.S)
        example = next(example for example in examples if "[proxy]" in example)
        raw = tomllib.loads(example)
        assert raw["proxy"]["kind"] == "socks5"
        assert raw["proxy"]["host"] == "proxy.example"
        if proxy_implemented:
            validate(parse(raw))


def test_translated_closed_lists_have_no_open_ended_marker() -> None:
    banned = {
        "README.zh-CN.md": ("\u7b49", "\u5305\u62ec"),
        "README.ja.md": ("\u306a\u3069",),
        "README.ko.md": ("\ub4f1\uc758", "\ube44\ub86f\ud55c"),
    }
    for name, markers in banned.items():
        bodies = fact_bodies(name)
        scope = bodies["storage-device-graph"] + bodies["requirements-bootstrap"]
        assert all(marker not in scope for marker in markers), name


def test_simplified_chinese_keeps_required_inline_spacing() -> None:
    banned = (
        "\u5305\u542b \u5b50\u5377 \u7684",
        "Xfce \uff0c",
        "\u65e5\u5fd7`install.log`",
        "\u4f7f\u7528`--lang",
        "\u4f7f\u7528`--no-shell`",
    )
    said = readme("README.zh-CN.md")
    assert all(fragment not in said for fragment in banned)


def test_every_configuration_a_readme_prints_can_produce_a_plan() -> None:
    """The published example described a disk with no table, no esp and no
    root mountpoint: it parsed and then failed validation with two errors, so
    a reader who copied it got no plan at all."""
    import re
    import tomllib

    from gentoo_install.model.parse import parse
    from gentoo_install.model.validate import validate

    from dataclasses import fields
    from gentoo_install.model.config import InstallConfig

    proxy_implemented = any(field.name == "proxy" for field in fields(InstallConfig))
    for name in READMES:
        for block in re.findall(r"```toml\n(.*?)```", (ROOT / name).read_text(), re.S):
            if not proxy_implemented and "[proxy]" in block:
                continue
            validate(parse(tomllib.loads(block)))


def test_every_path_a_readme_links_to_exists() -> None:
    """A schema reference nobody can open is worse than none."""
    import re

    for name in READMES:
        said = (ROOT / name).read_text()
        for target in re.findall(r"\]\(([^)#]+)\)", said):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (ROOT / target).exists(), f"{name} links to {target}"


def test_the_shell_commands_are_the_same_in_every_translation() -> None:
    """A translated comment is fine; a translated command is a different
    instruction, and one locale losing `--dry-run` is a destructive run."""
    import re

    def commands(name: str) -> list[str]:
        found: list[str] = []
        for block in re.findall(r"```sh\n(.*?)```", (ROOT / name).read_text(), re.S):
            for line in block.splitlines():
                bare = line.split("#", 1)[0].strip()
                if bare:
                    found.append(bare)
        return found

    English = commands("README.md")
    assert English, "the English README prints no commands"
    for name in READMES:
        assert commands(name) == English, name


def test_no_readme_addresses_the_reader_in_the_second_person() -> None:
    """The whole set is written about the installer and the operator, and one
    locale slipping into instructions to `you` reads as a different document."""
    for name, locale in READMES.items():
        for number, line in enumerate(readme(name).splitlines(), 1):
            assert not SECOND_PERSON[locale].search(line), f"{name}:{number}"


def test_every_documented_long_option_exists_in_cli_help() -> None:
    from gentoo_install.cli import parser

    documented = set(re.findall(r"--[a-z][a-z-]*", readme("README.md")))
    cli_help = parser().format_help()
    assert documented
    assert all(option in cli_help for option in documented), documented


def test_the_readme_set_holds_no_contributor_instructions() -> None:
    """`CONTRIBUTING.md` holds those, and a second copy goes stale."""
    assert (ROOT / "CONTRIBUTING.md").is_file()
    for name in READMES:
        said = (ROOT / name).read_text()
        assert "mypy" not in said, name
        assert "pytest" not in said, name


def test_the_readme_names_everything_publishing_removes() -> None:
    """The README said the menu replaces `only` those two values and that the
    other values remain. `to_toml(publishing=True)` also drops the proxy
    username and password, and drops them rather than replacing them — so the
    sentence understated the protection and mis-stated its mechanism, on the
    one paragraph a reader consults before putting a configuration on a public
    address."""
    from dataclasses import fields

    from gentoo_install.model.config import ProxyConfig
    from gentoo_install.model.serialise import REDACTED, SECRET

    english = (ROOT / "README.md").read_text(encoding="utf-8")
    said = next(line for line in english.splitlines() if REDACTED in line)

    for name in SECRET:
        assert f"`{name}`" in said, name
    # And the keys that are omitted rather than replaced, by their real names.
    dropped = {"username", "password"}
    assert dropped <= {field.name for field in fields(ProxyConfig)}, "the model moved"
    for name in dropped:
        assert f"`{name}`" in said, name

    # `only` was the word that made it wrong; it must not come back beside the
    # two hashes it used to qualify.
    assert "replaces only" not in said, said


def test_every_fixture_a_record_used_is_named_in_it() -> None:
    """A row that says `dd`, raw and gz` does not say which configuration ran,
    and a reader cannot check the claim against the tree. Six of the forty
    fixtures were used by a record that never named them.

    Only the fixtures a record exists for: one that has never run is absent
    from `TESTED.md` on purpose, and this must not turn into a rule that every
    fixture has a record.
    """
    import re

    recorded = set(re.findall(r"`([a-z0-9][a-z0-9-]+)`", (ROOT / "TESTED.md").read_text()))
    fixtures = {one.stem for one in (ROOT / "tests" / "fixtures").glob("*.toml")}

    # Named anywhere in the file, which includes a row that records why a
    # fixture failed: this rule is about a record a reader can trace back to a
    # configuration, not about a fixture having passed. These two appear
    # nowhere at all.
    without_a_record = {"vm-source-kernel"}
    assert without_a_record <= fixtures, without_a_record
    assert fixtures - recorded == without_a_record, sorted(fixtures - recorded)
