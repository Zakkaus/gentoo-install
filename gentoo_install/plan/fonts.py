"""Font selection configured for the package groups that provide the faces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Final

from ..model.config import InstallConfig
from . import packages
from .operations import Context, Operation, Stage


class CjkFontconfigLocale(Enum):
    ZH_CN = ("zh_CN", "zh-cn", "Noto Sans CJK SC")
    ZH_TW = ("zh_TW", "zh-tw", "Noto Sans CJK TC")
    ZH_HK = ("zh_HK", "zh-hk", "Noto Sans CJK HK")
    JA_JP = ("ja_JP", "ja", "Noto Sans CJK JP")
    KO_KR = ("ko_KR", "ko", "Noto Sans CJK KR")

    def __init__(self, locale: str, language: str, face: str) -> None:
        self.locale = locale
        self.language = language
        self.face = face

    @classmethod
    def selected(cls, locale: str) -> CjkFontconfigLocale | None:
        locale_name = locale.partition(".")[0]
        return next((candidate for candidate in cls if candidate.locale == locale_name), None)


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

#: Every CJK sans face, so the one the operator's locale asks for leads and the
#: others still answer text in the languages they cover.
CJK_SANS_ORDER: Final[tuple[str, ...]] = (
    "Noto Sans CJK TC",
    "Noto Sans CJK SC",
    "Noto Sans CJK HK",
    "Noto Sans CJK JP",
    "Noto Sans CJK KR",
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

    def describe(self) -> str:
        return f"write {CJK_SANS_PREFERENCE}"

    def apply(self, context: Context) -> None:
        context.write(CJK_SANS_PREFERENCE, self.content())

    def content(self) -> str:
        """The chosen regional face ahead of every other CJK family.

        A `lang` test does not separate the CJK languages: measured on a real
        machine, a rule tested on `zh-tw` also fired for `ja` and `ko`, so the
        alias names the order instead and the file is written only for a system
        whose own locale is Chinese.
        """
        others = tuple(one for one in CJK_SANS_ORDER if one != self.locale.face)
        families = "".join(
            f"      <family>{one}</family>\n" for one in (self.locale.face, *others)
        )
        return (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
            "<fontconfig>\n"
            '  <alias binding="same">\n'
            "    <family>sans-serif</family>\n"
            "    <prefer>\n"
            f"{families}"
            "    </prefer>\n"
            "  </alias>\n"
            "</fontconfig>\n"
        )


def build(config: InstallConfig, catalog: packages.Catalog) -> tuple[Operation, ...]:
    locale = CjkFontconfigLocale.selected(config.system.locale)
    if locale is None:
        return ()
    if not any(group.name == NOTO_CJK_GROUP for group in packages.groups(config, catalog)):
        return ()
    return (EnableNotoCjkFontconfig(), WriteCjkSansPreference(locale=locale))
