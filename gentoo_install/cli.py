"""Argument parsing and the one place an exception becomes an exit code.

The table is in docs/design.md. Codes 3 and 4 stay apart on purpose: 3 says the
data could not be trusted, 4 says an operation did not finish.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import errors
from .data import load_catalog
from .model.parse import load
from .plan.build import DEFAULT_MIRROR, build
from .plan.render import render, summarise

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_PREFLIGHT = 2
EXIT_INTEGRITY = 3
EXIT_COMMAND = 4
EXIT_ABORTED = 5


def parser() -> argparse.ArgumentParser:
    parsed = argparse.ArgumentParser(
        prog="gentoo-install", description="Install Gentoo from a configuration file or a menu."
    )
    parsed.add_argument("--config", type=Path, help="install from this file instead of the menu")
    parsed.add_argument(
        "--dry-run",
        action="store_true",
        help="print the operations the configuration produces and exit without touching anything",
    )
    parsed.add_argument("--mirror", default=DEFAULT_MIRROR, help="where to fetch stage3 from")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.config is None:
            print("the menu is not written yet; pass --config FILE", file=sys.stderr)
            return EXIT_CONFIG
        operations = build(load(arguments.config), load_catalog(), mirror=arguments.mirror)
        if arguments.dry_run:
            print(render(operations), end="")
            print(summarise(operations))
            return EXIT_OK
        print("only --dry-run is implemented; the disks are untouched", file=sys.stderr)
        return EXIT_ABORTED
    except errors.ConfigError as error:
        print(f"configuration: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except errors.PreflightFailed as error:
        print(f"preflight: {error}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except errors.IntegrityError as error:
        print(f"integrity: {error}", file=sys.stderr)
        return EXIT_INTEGRITY
    except errors.CommandFailed as error:
        print(f"command: {error}", file=sys.stderr)
        return EXIT_COMMAND
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return EXIT_ABORTED
