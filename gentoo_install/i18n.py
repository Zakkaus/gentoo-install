"""Interface language: which one, and the strings for it.

Source strings are English and are the keys. A tag with no catalog, or a key a
catalog does not carry, falls back to the source string, so a half-translated
catalog degrades one string at a time instead of failing.
"""

from __future__ import annotations

import os
import tomllib
import unicodedata
from pathlib import Path
from typing import Final, Mapping

from .errors import ConfigError

LOCALES: Final[Path] = Path(__file__).resolve().parent / "data" / "locale"

#: The environment names a locale; the catalog is named by language tag. Both
#: forms of Chinese are listed because neither is a fallback for the other.
_ALIASES: Final[dict[str, str]] = {
    "zh_TW": "zh-TW",
    "zh_HK": "zh-TW",
    "zh_CN": "zh-CN",
    "zh_SG": "zh-CN",
    "en_US": "en",
    "en_GB": "en",
    "ja_JP": "ja",
    "ko_KR": "ko",
}

#: Read in this order, which is what `locale(7)` says overrides what.
_ENVIRONMENT: Final[tuple[str, ...]] = ("LC_ALL", "LC_MESSAGES", "LANG")


def tag_for(environment: Mapping[str, str] | None = None, override: str = "") -> str:
    """Which catalog to load. `override` is `--lang` and wins outright."""
    if override:
        return _normalise(override)
    source = os.environ if environment is None else environment
    for name in _ENVIRONMENT:
        value = source.get(name, "")
        if not value:
            continue
        # The first one that is set decides, even when it is C: LC_ALL=C is a
        # deliberate request for the untranslated interface, not an absence.
        return "en" if value in ("C", "POSIX") else _normalise(value)
    return "en"


def _normalise(value: str) -> str:
    """`zh_TW.UTF-8` and `zh-tw` both name the same catalog."""
    bare = value.split(".", 1)[0].split("@", 1)[0].replace("-", "_")
    language, _, region = bare.partition("_")
    # `zh-cn`, `zh_CN` and `ZH_cn` all name one catalog; the tag itself is
    # lowercase language and uppercase region.
    bare = f"{language.lower()}_{region.upper()}" if region else language.lower()
    if bare in _ALIASES:
        return _ALIASES[bare]
    language, _, region = bare.partition("_")
    if language == "zh":
        # No catalog for an unlisted Chinese region: guessing between the two
        # scripts is worse than English, which the operator can at least read.
        return _ALIASES.get(f"zh_{region}", "en")
    return language or "en"


class Catalog:
    """One language's strings, loaded the first time one is asked for."""

    def __init__(self, tag: str, root: Path | None = None) -> None:
        self.tag = tag
        self._root = root if root is not None else LOCALES
        self._strings: dict[str, str] | None = None

    def __call__(self, source: str) -> str:
        if self._strings is None:
            self._strings = self._load()
        return self._strings.get(source, source)

    def _load(self) -> dict[str, str]:
        path = self._root / f"{self.tag}.toml"
        if not path.is_file():
            return {}
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"{path}: {error}") from error
        strings = raw.get("strings", {})
        if not isinstance(strings, dict):
            raise ConfigError(f"{path}: [strings] must be a table")
        for key, value in strings.items():
            if not isinstance(value, str):
                raise ConfigError(f"{path}: {key} is not a string")
        return {str(key): str(value) for key, value in strings.items()}


def width(text: str) -> int:
    """Terminal cells `text` occupies.

    A CJK character takes two cells and a combining mark takes none, so `len`
    is wrong for every string this interface displays.
    """
    total = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        total += 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
    return total


def truncate(text: str, cells: int) -> str:
    """Cut to at most `cells` columns, never mid-character."""
    if width(text) <= cells:
        return text
    kept: list[str] = []
    used = 0
    for character in text:
        step = width(character)
        if used + step > cells:
            break
        kept.append(character)
        used += step
    return "".join(kept)
