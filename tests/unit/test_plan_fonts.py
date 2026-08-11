from __future__ import annotations

from dataclasses import replace
from typing import Final
from xml.etree import ElementTree

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.model.config import InstallConfig, Overlay, PackagesConfig
from gentoo_install.plan.build import build as build_plan
from gentoo_install.plan.fonts import (
    CJK_SANS_ORDER,
    CJK_SANS_PREFERENCE,
    CjkFontconfigLocale,
    EnableNotoCjkFontconfig,
    WriteCjkSansPreference,
)

from .layouts import config
from .recorder import Recorder


CJK_RULES: Final[tuple[tuple[str, str, str], ...]] = (
    ("zh_TW.UTF-8", "zh-tw", "Noto Sans CJK TC"),
    ("zh_CN.UTF-8", "zh-cn", "Noto Sans CJK SC"),
    ("zh_HK.UTF-8", "zh-hk", "Noto Sans CJK HK"),
    ("ja_JP.UTF-8", "ja", "Noto Sans CJK JP"),
    ("ko_KR.UTF-8", "ko", "Noto Sans CJK KR"),
)


def zh_tw_desktop() -> InstallConfig:
    installation = config()
    return replace(
        installation,
        system=replace(installation.system, locale="zh_TW.UTF-8"),
        packages=PackagesConfig(
            desktop="plasma", applications=("configure-fonts", "noto-cjk")
        ),
    )


def test_a_zh_tw_desktop_writes_the_regional_sans_rule() -> None:
    operations = [
        one
        for one in build_plan(zh_tw_desktop(), load_catalog())
        if isinstance(one, (EnableNotoCjkFontconfig, WriteCjkSansPreference))
    ]
    recorder = Recorder()
    for operation in operations:
        operation.apply(recorder)

    assert [operation.describe() for operation in operations] == [
        "enable /etc/fonts/conf.d/70-noto-cjk.conf",
        "write /etc/fonts/conf.d/71-gentoo-install-cjk-sans.conf",
    ]
    assert recorder.only("eselect", "fontconfig") == (
        "eselect",
        "fontconfig",
        "enable",
        "70-noto-cjk.conf",
    )
    assert "<string>Noto Sans CJK TC</string>" in recorder.files[CJK_SANS_PREFERENCE]
    # The face the operator's locale asks for leads the alias.
    written = recorder.files[CJK_SANS_PREFERENCE]
    assert written.index("Noto Sans CJK TC") < written.index("Noto Sans CJK SC"), written


def test_a_plan_without_the_font_group_enables_no_fontconfig_file() -> None:
    assert not [
        one
        for one in build_plan(
            replace(config(), system=replace(config().system, locale="zh_CN.UTF-8")),
            load_catalog(),
        )
        if isinstance(one, (EnableNotoCjkFontconfig, WriteCjkSansPreference))
    ]


@pytest.mark.parametrize("locale", ["en_US.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8"])
def test_a_locale_outside_cjk_is_not_changed(locale: str) -> None:
    installation = config()
    selected = replace(
        installation,
        system=replace(installation.system, locale=locale),
        packages=PackagesConfig(applications=("configure-fonts", "noto-cjk")),
    )
    assert not [
        one
        for one in build_plan(selected, load_catalog())
        if isinstance(one, (EnableNotoCjkFontconfig, WriteCjkSansPreference))
    ]


@pytest.mark.parametrize(("locale", "language", "face"), CJK_RULES)
def test_the_written_file_is_a_regional_sans_match(
    locale: str, language: str, face: str
) -> None:
    selected = CjkFontconfigLocale.selected(locale)
    assert selected is not None
    content = WriteCjkSansPreference(locale=selected).content()
    root = ElementTree.fromstring(content)
    assert root.tag == "fontconfig"
    matches = root.findall("match")
    assert len(matches) == 3, content
    sans = next(one for one in matches if one.findtext("./test/string") == "sans-serif")
    preferred = [one.text for one in sans.findall("./edit/string")]
    # The locale's own face leads; the rest still answer the languages they
    # cover, and a `lang` test cannot separate them.
    assert preferred[0] == face, preferred
    assert set(preferred) == set(CJK_SANS_ORDER), preferred
    assert language


def test_a_selected_non_noto_sans_leads_the_written_alias() -> None:
    installation = zh_tw_desktop()
    selected = replace(
        installation,
        packages=replace(
            installation.packages,
            applications=("configure-fonts", "source-han-sans", "noto-cjk"),
        ),
    )
    operation = next(
        one
        for one in build_plan(selected, load_catalog())
        if isinstance(one, WriteCjkSansPreference)
    )
    root = ElementTree.fromstring(operation.content())
    sans = next(
        match
        for match in root.findall("match")
        if match.findtext("./test/string") == "sans-serif"
    )
    families = [one.text for one in sans.findall("./edit/string")]
    assert families[0] == "Source Han Sans TW"
    assert families.index("Source Han Sans TW") < families.index("Noto Sans CJK TC")


def test_a_latin_sans_cannot_lead_the_cjk_alias() -> None:
    installation = zh_tw_desktop()
    selected = replace(
        installation,
        packages=replace(
            installation.packages,
            applications=("configure-fonts", "open-sans", "noto-cjk"),
        ),
    )
    operation = next(
        one
        for one in build_plan(selected, load_catalog())
        if isinstance(one, WriteCjkSansPreference)
    )
    root = ElementTree.fromstring(operation.content())
    sans = next(
        match
        for match in root.findall("match")
        if match.findtext("./test/string") == "sans-serif"
    )
    families = [one.text for one in sans.findall("./edit/string")]
    assert families[0] == "Noto Sans CJK TC"
    assert "Open Sans" not in families


def test_serif_and_sans_defaults_precede_arphic_families() -> None:
    installation = config()
    selected = replace(
        installation,
        system=replace(installation.system, locale="zh_CN.UTF-8"),
        packages=PackagesConfig(applications=("configure-fonts",)),
    )
    defaults = [
        one
        for one in build_plan(selected, load_catalog())
        if isinstance(one, (EnableNotoCjkFontconfig, WriteCjkSansPreference))
    ]
    assert not any(isinstance(one, EnableNotoCjkFontconfig) for one in defaults)
    default_rule = next(
        one for one in defaults if isinstance(one, WriteCjkSansPreference)
    )
    default_root = ElementTree.fromstring(default_rule.content())
    default_sans = next(
        match
        for match in default_root.findall("match")
        if match.findtext("./test/string") == "sans-serif"
    )
    assert default_sans.findtext("./edit/string") == "Noto Sans CJK SC"

    operation = next(
        one
        for one in build_plan(zh_tw_desktop(), load_catalog())
        if isinstance(one, WriteCjkSansPreference)
    )
    root = ElementTree.fromstring(operation.content())
    matches = {
        match.findtext("./test/string"): match for match in root.findall("match")
    }
    assert matches["sans-serif"].findtext("./edit/string") == "Noto Sans CJK TC"
    assert matches["serif"].findtext("./edit/string") == "Noto Sans CJK TC"
    assert all(edit.get("mode") == "prepend_first" for edit in root.findall("./match/edit"))
    assert "AR PL" not in operation.content()
    installation = zh_tw_desktop()
    selected = replace(
        installation,
        portage=replace(
            installation.portage,
            overlays=(
                Overlay(
                    name="gentoo-zh",
                    sync_uri="https://example.invalid/gentoo-zh.git",
                ),
            ),
        ),
        packages=replace(
            installation.packages,
            applications=("configure-fonts", "wenkai-tc", "noto-cjk"),
        ),
    )
    operation = next(
        one
        for one in build_plan(selected, load_catalog())
        if isinstance(one, WriteCjkSansPreference)
    )
    root = ElementTree.fromstring(operation.content())
    matches = {
        match.findtext("./test/string"): match for match in root.findall("match")
    }
    assert matches["serif"].findtext("./edit/string") == "LXGW WenKai TC"
    assert matches["sans-serif"].findtext("./edit/string") == "Noto Sans CJK TC"
