from dataclasses import replace

from gentoo_install.data import load_catalog
from gentoo_install.plan.packages import build

from .layouts import config


def test_input_engines_declare_the_language_they_type() -> None:
    catalog = load_catalog()
    providers = {"fcitx5", "ibus"}
    engines = [
        group
        for group in catalog.values()
        if group.input_method and group.name not in providers
    ]
    assert engines
    assert all(group.input_language for group in engines)


def test_font_groups_declare_a_family_and_category() -> None:
    catalog = load_catalog()
    fonts = [group for group in catalog.values() if group.font_family]
    assert fonts
    assert all(group.font_category for group in fonts)
    assert catalog["wenkai-tc"].font_family == "LXGW WenKai TC"
    assert catalog["source-han-sans"].font_family == "Source Han Sans {source}"
    assert catalog["sarasa-mono"].font_family == "Sarasa Mono SC"


def test_ibus_has_declared_chinese_engines() -> None:
    catalog = load_catalog()
    chinese = {
        group.name
        for group in catalog.values()
        if group.input_framework == "ibus" and group.input_language == "Chinese"
    }
    assert {
        "ibus-pinyin",
        "ibus-bopomofo",
        "ibus-cangjie",
        "ibus-chinese-tables",
        "ibus-rime",
    } <= chinese
    assert catalog["ibus-pinyin"].packages == ("app-i18n/ibus-libpinyin",)
    installation = config()
    selected = replace(
        installation,
        packages=replace(
            installation.packages,
            desktop="gnome",
            applications=("ibus", "ibus-pinyin", "configure-input"),
        ),
    )
    operations = build(selected, load_catalog())
    assert any(type(operation).__name__ == "ConfigureGnomeInputSources" for operation in operations)
    selected = replace(
        installation,
        packages=replace(
            installation.packages,
            desktop="gnome",
            applications=(
                "ibus",
                "ibus-pinyin",
                "decline-input-configuration",
            ),
        ),
    )
    descriptions = tuple(operation.describe() for operation in build(selected, load_catalog()))
    assert not any("input" in description and "environment" in description for description in descriptions)
    assert not any("dconf" in description for description in descriptions)
