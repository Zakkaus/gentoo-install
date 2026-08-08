from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from gentoo_install.errors import ConfigError
from gentoo_install.i18n import LOCALES, Catalog, tag_for, truncate, width


def shipped(tag: str) -> dict[str, str]:
    """The catalog as data, so no CJK literal has to live in the test tree."""
    raw = tomllib.loads((LOCALES / f"{tag}.toml").read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in raw["strings"].items()}


def test_the_first_variable_that_is_set_decides() -> None:
    """`locale(7)` order, and a variable set to C is a request for the
    untranslated interface rather than an absence."""
    assert tag_for({"LANG": "zh_TW.UTF-8"}) == "zh-TW"
    assert tag_for({"LC_MESSAGES": "zh_CN.UTF-8", "LANG": "en_US.UTF-8"}) == "zh-CN"
    assert tag_for({"LC_ALL": "C", "LANG": "zh_TW.UTF-8"}) == "en"
    assert tag_for({}) == "en"


@pytest.mark.parametrize(
    "given,expected",
    [
        ("zh-cn", "zh-CN"),
        ("ZH_tw", "zh-TW"),
        ("zh_HK", "zh-TW"),
        ("zh_SG", "zh-CN"),
        ("en_GB", "en"),
        ("de_DE.UTF-8", "de"),
    ],
)
def test_a_tag_is_normalised_however_it_was_written(given: str, expected: str) -> None:
    assert tag_for({}, override=given) == expected


def test_chinese_with_no_catalog_falls_back_to_english() -> None:
    """Guessing between the two scripts is worse than English, which the
    operator can at least read."""
    assert tag_for({"LANG": "zh_MO.UTF-8"}) == "en"


def test_a_missing_string_falls_back_to_its_source() -> None:
    catalog = Catalog("zh-TW")
    # Compared against the catalog file rather than a literal here, because no
    # CJK belongs in the test tree.
    assert catalog("Disks") == shipped("zh-TW")["Disks"]
    assert catalog("a string nobody translated") == "a string nobody translated"


def test_a_language_with_no_catalog_reads_as_english(tmp_path: Path) -> None:
    assert Catalog("de", tmp_path)("Disks") == "Disks"


def test_a_broken_catalog_is_named_rather_than_ignored(tmp_path: Path) -> None:
    (tmp_path / "xx.toml").write_text("[strings]\nDisks = 3\n")
    with pytest.raises(ConfigError, match="Disks"):
        Catalog("xx", tmp_path)("Disks")


def test_every_shipped_catalog_translates_the_same_keys() -> None:
    """A key in one catalog and not the other is a screen that changes language
    halfway down."""
    tags = sorted(path.stem for path in LOCALES.glob("*.toml"))
    assert tags == ["zh-CN", "zh-TW"]
    first, second = (shipped(tag) for tag in tags)
    assert set(first) == set(second)
    for tag in tags:
        catalog = Catalog(tag)
        for key, value in shipped(tag).items():
            assert value and catalog(key) == value, key


#: A wide character and a combining mark, by codepoint: no CJK literal belongs
#: in the test tree, and these are what the width rule exists for.
WIDE = "\u78c1\u789f"
COMBINING = "e\u0301"


def test_width_counts_cells_and_not_characters() -> None:
    """A CJK character takes two columns and a combining mark takes none, so
    `len` lays the screen out wrongly for exactly the users this is for."""
    assert width("Disks") == 5
    assert width(WIDE) == 4
    assert width(f"{WIDE} Disks") == 10
    assert width(COMBINING) == 1


def test_truncate_never_cuts_a_character_in_half() -> None:
    assert truncate(WIDE + WIDE, 5) == WIDE
    assert truncate("Disks", 99) == "Disks"
    assert width(truncate(WIDE + WIDE, 5)) <= 5
