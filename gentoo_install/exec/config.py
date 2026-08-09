"""Reading a configuration off this machine.

The model parses a mapping and nothing else. Opening a path is I/O, so it
lives here: with `load` in `model/parse.py` the declared `model -> plan ->
exec` boundary was false, and a model test could not cover configuration
loading without a real file on the host.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ..errors import ConfigError
from ..model.config import InstallConfig
from ..model.parse import parse


def load(path: Path) -> InstallConfig:
    """Read a TOML file and parse it, or say which file and why not."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    except OSError as error:
        raise ConfigError(f"{path}: {error}") from error
    return parse(raw)
