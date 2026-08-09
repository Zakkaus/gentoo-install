"""The README set is five files that have to move together.

Five translations drift the moment one of them is edited alone, and the drift
is invisible to a reader who reads only one of them.
"""

from __future__ import annotations

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
PICTURES = ("screenshot.png", "cjk-console.png")


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
        for picture in PICTURES:
            assert f"({picture})" in said, f"{name} is missing {picture}"


def test_every_readme_carries_the_same_sections() -> None:
    """The headings are translated, so the count is what can be compared: a
    section added to one file and not the rest is the usual way they diverge."""
    counts = {
        name: sum(1 for line in (ROOT / name).read_text().splitlines() if line.startswith("## "))
        for name in READMES
    }
    assert len(set(counts.values())) == 1, counts


def test_every_configuration_a_readme_prints_can_produce_a_plan() -> None:
    """The published example described a disk with no table, no esp and no
    root mountpoint: it parsed and then failed validation with two errors, so
    a reader who copied it got no plan at all."""
    import re
    import tomllib

    from gentoo_install.model.parse import parse
    from gentoo_install.model.validate import validate

    for name in READMES:
        for block in re.findall(r"```toml\n(.*?)```", (ROOT / name).read_text(), re.S):
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
    import re

    banned = re.compile(r"\b(you|your|yours)\b", re.IGNORECASE)
    for name in READMES:
        for number, line in enumerate((ROOT / name).read_text().splitlines(), 1):
            assert not banned.search(line), f"{name}:{number}"


def test_the_readme_set_holds_no_contributor_instructions() -> None:
    """`CONTRIBUTING.md` holds those, and a second copy goes stale."""
    assert (ROOT / "CONTRIBUTING.md").is_file()
    for name in READMES:
        said = (ROOT / name).read_text()
        assert "mypy" not in said, name
        assert "pytest" not in said, name
