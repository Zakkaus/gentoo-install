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
from .exec import preflight
from .exec.apply import Machine, apply
from .exec.probe import Probe
from .exec.runner import Runner
from .model.config import InstallConfig
from .model.parse import load
from .plan.build import DEFAULT_MIRROR, build
from .plan.operations import Operation
from .plan.render import render, summarise

#: Everything a run needs to keep: the device map, the staged keys, the log.
WORK = Path("/run/gentoo-install")

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
    parsed.add_argument(
        "--target", type=Path, default=Path("/mnt/gentoo"), help="where to mount the new system"
    )
    parsed.add_argument("--work", type=Path, default=WORK, help="where to keep the run's state")
    parsed.add_argument(
        "--skip-preflight",
        action="store_true",
        help="install without checking the machine first, for a harness that knows what it booted",
    )
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
        return install(load(arguments.config), operations, arguments)
    except errors.DeviceNotFound as error:
        print(f"device: {error}", file=sys.stderr)
        return EXIT_PREFLIGHT
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


def install(config: InstallConfig, operations: tuple[Operation, ...], arguments: argparse.Namespace) -> int:
    """Check the machine, then perform every operation in order."""
    work: Path = arguments.work
    work.mkdir(parents=True, exist_ok=True)
    log = (work / "install.log").open("a")

    def record(line: str) -> None:
        print(line, file=log, flush=True)
        print(line)

    runner = Runner(log=record)
    probe = Probe(runner=runner, work=work)
    probe.load()
    if not arguments.skip_preflight:
        report = preflight.check(config, probe)
        for warning in report.warnings:
            record(f"warning: {warning}")
        report.raise_if_fatal()
    machine = Machine(
        config=config, runner=runner, probe=probe, work=work, mountpoint=arguments.target
    )
    apply(operations, machine)
    record(f"installed {len(operations)} operations into {arguments.target}")
    return EXIT_OK
