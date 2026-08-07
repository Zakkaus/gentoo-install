"""Reads the tables shipped beside the code: desktop profiles and app groups.

The plan layer is a pure function of its arguments, so the catalog is read here
once and handed to it. `cli.py` is the only caller.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final

from .errors import ConfigError
from .plan.packages import Catalog, Group

DATA: Final[Path] = Path(__file__).resolve().parent / "data"

KEYS: Final[frozenset[str]] = frozenset({"packages", "services", "use", "repositories"})


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
        packages=_strings(raw, "packages", path),
        services=_strings(raw, "services", path),
        use=_strings(raw, "use", path),
        repositories=_strings(raw, "repositories", path),
    )


def _strings(raw: dict[str, object], key: str, path: Path) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{path}: {key} must be a list of strings")
    return tuple(str(item) for item in value)
