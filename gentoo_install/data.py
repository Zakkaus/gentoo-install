"""Reads the tables shipped beside the code: desktop profiles and app groups.

The plan layer is a pure function of its arguments, so the catalog is read here
once and handed to it. `cli.py` is the only caller.
"""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath
from typing import Final

from .errors import ConfigError
from .plan.packages import Catalog, Group, GroupFile

DATA: Final[Path] = Path(__file__).resolve().parent / "data"

KEYS: Final[frozenset[str]] = frozenset(
    {
        "packages", "services", "use", "repositories", "video_cards", "files",
        "input_method", "schemas", "wayland", "package_use", "accept_license",
        "display_manager", "profile", "input_framework", "input_method_launcher",
        "wayland_files", "user_groups", "user_services", "accept_keywords",
        "systemd_services", "label", "input_language", "font_family",
        "font_category", "font_cjk", "font_configuration", "input_configuration",
        "input_source",
    }
)


def load_catalog(root: Path | None = None) -> Catalog:
    base = root if root is not None else DATA
    catalog: dict[str, Group] = {}
    for directory in (base / "profiles", base / "packages"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            group = _load(path)
            if group.name in catalog:
                raise ConfigError(f"two files declare the group {group.name!r}")
            catalog[group.name] = group
    return catalog


def _load(path: Path) -> Group:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"{path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    unknown = sorted(set(raw) - KEYS)
    if unknown:
        raise ConfigError(f"{path} has unknown keys: {', '.join(unknown)}")
    return Group(
        name=path.stem,
        label=_text(raw, "label", path),
        packages=_strings(raw, "packages", path),
        services=_strings(raw, "services", path),
        systemd_services=_strings(raw, "systemd_services", path),
        use=_strings(raw, "use", path),
        repositories=_strings(raw, "repositories", path),
        video_cards=_strings(raw, "video_cards", path),
        accept_license=_strings(raw, "accept_license", path),
        display_manager=_text(raw, "display_manager", path),
        profile=_text(raw, "profile", path),
        files=_files(raw, path),
        input_method=_text(raw, "input_method", path),
        input_framework=_text(raw, "input_framework", path),
        input_language=_text(raw, "input_language", path),
        input_source=_text(raw, "input_source", path),
        font_family=_text(raw, "font_family", path),
        font_category=_text(raw, "font_category", path),
        font_cjk=_flag(raw, "font_cjk", path),
        font_configuration=_text(raw, "font_configuration", path),
        input_configuration=_text(raw, "input_configuration", path),
        input_method_launcher=_text(raw, "input_method_launcher", path),
        wayland_files=_files(raw, path, key="wayland_files"),
        schemas=_strings(raw, "schemas", path),
        wayland=_flag(raw, "wayland", path),
        package_use=_strings(raw, "package_use", path),
        accept_keywords=_strings(raw, "accept_keywords", path),
        user_groups=_strings(raw, "user_groups", path),
        user_services=_strings(raw, "user_services", path),
    )


def _files(raw: dict[str, object], path: Path, key: str = "files") -> tuple[GroupFile, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"{path}: {key} must be a list of tables")
    found: list[GroupFile] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"path", "content"}:
            raise ConfigError(f"{path}: each {key} entry needs exactly a path and a content")
        where, content = entry["path"], entry["content"]
        if not isinstance(where, str) or not isinstance(content, str):
            raise ConfigError(f"{path}: {key} path and content must be strings")
        found.append(GroupFile(path=PurePosixPath(where), content=content))
    return tuple(found)


def _text(raw: dict[str, object], key: str, path: Path) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"{path}: {key} must be a string")
    return value


def _flag(raw: dict[str, object], key: str, path: Path) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"{path}: {key} must be true or false")
    return value


def _strings(raw: dict[str, object], key: str, path: Path) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{path}: {key} must be a list of strings")
    return tuple(str(item) for item in value)
