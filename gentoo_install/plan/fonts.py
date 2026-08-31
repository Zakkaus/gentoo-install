# SPDX-License-Identifier: GPL-2.0-or-later
"""Font selection configured for the package groups that provide the faces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Final

from ..errors import ConfigError
from ..model.config import InstallConfig
from . import packages
from .operations import Context, Operation, Stage


class CjkFontconfigLocale(Enum):
    # No fontconfig language tag: the file this writes carries no `lang`
    # test, because every CJK face covers several of them and the order is
    # what separates them. One was stored here and never read.
    ZH_CN = ("zh_CN", "SC", "CN")
    ZH_TW = ("zh_TW", "TC", "TW")
    ZH_HK = ("zh_HK", "HK", "HK")
    JA_JP = ("ja_JP", "JP", "JP")
    KO_KR = ("ko_KR", "KR", "KR")

    def __init__(self, locale: str, noto: str, source: str) -> None:
        self.locale = locale
        self.noto = noto
        self.source = source
        self.face = f"Noto Sans CJK {noto}"

    @classmethod
    def selected(cls, locale: str) -> CjkFontconfigLocale | None:
        locale_name = locale.partition(".")[0]
        return next((candidate for candidate in cls if candidate.locale == locale_name), None)

    def resolve(self, family: str) -> str:
        try:
            return family.format(noto=self.noto, source=self.source)
        except KeyError as error:
            raise ConfigError(f"unknown regional font family field {error}") from error


class FontCategory(Enum):
    SANS = ("sans", "Sans", "sans-serif")
    SERIF = ("serif", "Serif", "serif")
    KAI = ("kai", "Kai", "serif")
    MONOSPACE = ("monospace", "Monospace", "monospace")

    def __init__(self, catalog: str, heading: str, generic: str) -> None:
        self.catalog = catalog
        self.heading = heading
        self.generic = generic

    @classmethod
    def selected(cls, category: str) -> FontCategory:
        found = next((candidate for candidate in cls if candidate.catalog == category), None)
        if found is None:
            raise ConfigError(f"unknown font category {category!r}")
        return found


@dataclass(frozen=True)
class FontPreference:
    category: FontCategory
    families: tuple[str, ...]


NOTO_CJK_GROUP: Final[str] = "noto-cjk"
NOTO_CJK_AVAILABLE: Final[PurePosixPath] = PurePosixPath(
    "/etc/fonts/conf.avail/70-noto-cjk.conf"
)
NOTO_CJK_ENABLED: Final[PurePosixPath] = PurePosixPath(
    "/etc/fonts/conf.d/70-noto-cjk.conf"
)
CJK_SANS_PREFERENCE: Final[PurePosixPath] = PurePosixPath(
    "/etc/fonts/conf.d/71-gentoo-install-cjk-sans.conf"
)
CJK_SANS_ORDER: Final[tuple[str, ...]] = tuple(
    f"Noto Sans CJK {locale.noto}" for locale in CjkFontconfigLocale
)


@dataclass(frozen=True, kw_only=True)
class EnableNotoCjkFontconfig(Operation):
    stage: Stage = Stage.PACKAGES

    def describe(self) -> str:
        return f"enable {NOTO_CJK_ENABLED}"

    def apply(self, context: Context) -> None:
        context.run_in_target(
            ["eselect", "fontconfig", "enable", NOTO_CJK_AVAILABLE.name]
        )


@dataclass(frozen=True, kw_only=True)
class WriteCjkSansPreference(Operation):
    stage: Stage = Stage.PACKAGES
    locale: CjkFontconfigLocale
    preferences: tuple[FontPreference, ...] = ()

    def describe(self) -> str:
        return f"write {CJK_SANS_PREFERENCE}"

    def apply(self, context: Context) -> None:
        context.write(CJK_SANS_PREFERENCE, self.content())

    def content(self) -> str:
        """Place catalog-selected faces ahead of distribution fallback rules."""
        regional_noto = _regional_families("Noto Sans CJK {noto}", self.locale)
        selected = tuple(
            (preference.category.generic, preference.families)
            for preference in self.preferences
        )
        matches = "".join(
            _match(generic, _unique((*_for_generic(selected, generic), *regional_noto)))
            for generic in ("sans-serif", "serif", "monospace")
        )
        return (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
            "<fontconfig>\n"
            f"{matches}"
            "</fontconfig>\n"
        )


def _match(generic: str, families: tuple[str, ...]) -> str:
    values = "".join(f"      <string>{family}</string>\n" for family in families)
    return (
        '  <match target="pattern">\n'
        '    <test name="family" qual="any">\n'
        f"      <string>{generic}</string>\n"
        "    </test>\n"
        '    <edit name="family" mode="prepend_first" binding="strong">\n'
        f"{values}"
        "    </edit>\n"
        "  </match>\n"
    )


def _for_generic(
    selected: tuple[tuple[str, tuple[str, ...]], ...], generic: str
) -> tuple[str, ...]:
    return tuple(
        family
        for selected_generic, families in selected
        if selected_generic == generic
        for family in families
    )


def _regional_families(
    template: str, locale: CjkFontconfigLocale
) -> tuple[str, ...]:
    if "{" not in template:
        return (template,)
    ordered = (locale, *(candidate for candidate in CjkFontconfigLocale if candidate is not locale))
    return _unique(tuple(candidate.resolve(template) for candidate in ordered))


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return tuple(unique)


def build(config: InstallConfig, catalog: packages.Catalog) -> tuple[Operation, ...]:
    # The decisions are checked before the locale, not after: what this file
    # writes is a CJK font preference and an `en_US.UTF-8` machine needs none,
    # but a group that declines and a group that accepts is a configuration
    # nobody can satisfy whatever the locale is, and refusing it only for some
    # locales is a rule that fires by accident.
    chosen = packages.groups(config, catalog)
    decisions = {
        group.font_configuration for group in chosen if group.font_configuration
    }
    unknown = decisions - packages.FONT_CONFIGURATION_STATES
    if unknown:
        raise ConfigError(
            f"unknown font configuration decision: {', '.join(sorted(unknown))}"
        )
    if len(decisions) > 1:
        raise ConfigError("font configuration was both accepted and declined")
    locale = CjkFontconfigLocale.selected(config.system.locale)
    if locale is None:
        return ()
    if packages.FONT_CONFIGURATION_DISABLED in decisions:
        return ()
    if (
        packages.FONT_CONFIGURATION_ENABLED not in decisions
        and not any(group.font_family for group in chosen)
    ):
        return ()
    preferences = tuple(
        FontPreference(
            category=FontCategory.selected(group.font_category),
            families=_regional_families(group.font_family, locale),
        )
        for group in chosen
        if group.font_family and group.font_cjk
    )
    operations: tuple[Operation, ...] = (
        WriteCjkSansPreference(locale=locale, preferences=preferences),
    )
    if any(group.name == NOTO_CJK_GROUP for group in chosen):
        operations = (EnableNotoCjkFontconfig(), *operations)
    return operations
