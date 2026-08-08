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
    assert tags == ["ja", "ko", "zh-CN", "zh-TW"]
    keys = [set(shipped(tag)) for tag in tags]
    assert all(other == keys[0] for other in keys[1:])
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


def in_a_table() -> set[str]:
    """Reasons the screens keep in a table and translate where the row is drawn.

    Read from the tables rather than listed again here: a second list is one
    more thing to forget when a row is added.
    """
    from gentoo_install.model.mirrors import GENTOO_SITES, GENTOOZH_SITES
    from gentoo_install.plan.system import LOGGERS
    from gentoo_install.tui.screens import (
        BINHOSTS,
        DISPLAY_MANAGERS,
        GENTOOZH_CHANNELS,
        GRAPHICS,
        KERNELS,
        LICENSES,
        SYNC_METHODS,
    )

    tables = (
        KERNELS, LICENSES, GRAPHICS, DISPLAY_MANAGERS,
        SYNC_METHODS, GENTOOZH_CHANNELS, BINHOSTS,
    )
    found = {reason for table in tables for _, reason in table}
    # The logger table lives in `plan/` because it also names the package and
    # the service; the menu reads the same rows rather than keeping a copy.
    found |= {choice.reason for choice in LOGGERS.values()}
    # Each status is drawn by its own value, and its sentence lives beside the
    # enum so a status added without one cannot reach a menu.
    from gentoo_install.model.manual import STATUS_REASONS

    found |= {one.value for one in STATUS_REASONS}
    found |= set(STATUS_REASONS.values())
    # A mirror is drawn by its own name and where it is, both translated: a
    # Chinese interface listing "Nanjing University" reads half-finished.
    for site in (*GENTOO_SITES, *GENTOOZH_SITES):
        found.add(site.name)
        found.add(site.area)
    return found


def displayed() -> set[str]:
    """Every source string the interface passes through the catalog."""
    import ast

    from gentoo_install import tui

    found = in_a_table()
    for module in Path(tui.__file__).parent.parent.rglob("*.py"):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            called = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if called == "translate" and node.args and isinstance(node.args[0], ast.Constant):
                found.add(str(node.args[0].value))
            if called == "Setting" and len(node.args) > 1:
                if isinstance(node.args[1], ast.Constant):
                    found.add(str(node.args[1].value))
    return found


def test_the_catalogs_hold_every_string_the_interface_shows() -> None:
    """A string added without its translation reads as English in the middle of
    a translated screen, and a stale key hides a screen nobody updated."""
    shown = displayed()
    keys = set(shipped("zh-TW"))
    assert not shown - keys, sorted(shown - keys)
    assert not keys - shown, sorted(keys - shown)


def _shown(value: object) -> list[str]:
    """The strings an expression can end up drawing.

    Only the result branches: `subarch.endswith("v3")` decides which branch is
    taken and never reaches the screen, so a walk of the whole expression
    reports the condition as untranslated text.
    """
    import ast

    if isinstance(value, ast.Constant):
        return [str(value.value)] if value.value else []
    if isinstance(value, ast.IfExp):
        return _shown(value.body) + _shown(value.orelse)
    if isinstance(value, ast.BoolOp):
        return [name for one in value.values for name in _shown(one)]
    # A call, an attribute or a name: `translate(...)`, a rule's own `reason`,
    # or a variable, none of which is a literal in the source.
    return []


def test_no_row_is_greyed_out_with_a_string_the_catalog_never_saw() -> None:
    """`disabled_because` is drawn beside the label, so an English literal
    there put one English line in the middle of a translated menu. It is
    either empty, a catalog lookup, or a rule's own reason from `compat.py`."""
    import ast

    from gentoo_install import tui

    for module in Path(tui.__file__).parent.rglob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "disabled_because":
                continue
            literal = _shown(node.value)
            assert not literal, f"{module.name}: disabled_because={literal} is not translated"
