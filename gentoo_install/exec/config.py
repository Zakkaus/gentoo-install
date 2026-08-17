# SPDX-License-Identifier: GPL-2.0-or-later
"""Reading a configuration off this machine.

The model parses a mapping and nothing else. Opening a path is I/O, so it
lives here: with `load` in `model/parse.py` the declared `model -> plan ->
exec` boundary was false, and a model test could not cover configuration
loading without a real file on the host.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ..errors import ConfigError, GentooInstallError
from ..model.config import InstallConfig
from ..model.parse import parse


#: What a configuration read over the network may weigh. A configuration is a
#: few kilobytes of TOML, and a body past this is a mistake or a redirect into
#: something else, not a file worth parsing.
URL_CEILING: int = 1 << 20

#: What makes a source a URL rather than a path. `file://` is deliberately not
#: here: a path is how a local file is given, and two ways to say one thing is
#: the class of bug this file exists to avoid.
URL_SCHEMES: tuple[str, ...] = ("http://", "https://")


def looks_like_a_url(source: str) -> bool:
    """Whether this source is fetched rather than opened."""
    return source.startswith(URL_SCHEMES)


def load(path: Path) -> InstallConfig:
    """Read a TOML file and parse it, or say which file and why not."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    except OSError as error:
        raise ConfigError(f"{path}: {error}") from error
    return parse(raw)


def load_source(source: str) -> InstallConfig:
    """Read a configuration from a path or from a URL.

    One entry point for both, because every mode takes its configuration the
    same way: what differs between installing, converting and writing an image
    is what the configuration says, not where it came from.
    """
    if not looks_like_a_url(source):
        return load(Path(source))
    from . import fetch

    try:
        body = fetch.read_text(source, ceiling=URL_CEILING)
    except GentooInstallError as error:
        raise ConfigError(f"{source}: {error}") from error
    try:
        raw = tomllib.loads(body)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{source}: {error}") from error
    return parse(raw)
