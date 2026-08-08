"""Argument parsing and the one place an exception becomes an exit code.

The table is in docs/design.md. Codes 3 and 4 stay apart on purpose: 3 says the
data could not be trusted, 4 says an operation did not finish.
"""

from __future__ import annotations

import argparse
import curses
import shutil
import sys
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import errors
from .data import load_catalog
from .exec import fetch, preflight
from .exec.apply import Machine, apply, completed
from .exec.probe import Probe
from .exec.runner import Runner, write_file
from .log import Journal
from .tui import app, screens
from .tui.curses_screen import CursesScreen
from .i18n import Catalog, tag_for
from .model import templates
from .model.config import DiskConfig, Firmware, InstallConfig
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
        "--lang",
        default="",
        help="interface language, overriding what LC_ALL, LC_MESSAGES and LANG say",
    )
    parsed.add_argument(
        "--target", type=Path, default=Path("/mnt/gentoo"), help="where to mount the new system"
    )
    parsed.add_argument("--work", type=Path, default=WORK, help="where to keep the run's state")
    parsed.add_argument(
        "--missing-commands",
        action="store_true",
        help="list the commands this layout needs and this machine lacks, one per line, "
        "which is what bootstrap.sh turns into a package list",
    )
    parsed.add_argument(
        "--resume",
        action="store_true",
        help="carry on from where a previous run stopped, skipping the operations its "
        "journal records as done, instead of partitioning the disk again",
    )
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
            if arguments.missing_commands:
                # Nothing to derive a layout from, so answer for the commands
                # every install needs whatever it is about to do.
                print("\n".join(sorted(_absent(preflight.ALWAYS))))
                return EXIT_OK
            chosen = _from_menu(arguments)
            if chosen is None:
                print("cancelled", file=sys.stderr)
                return EXIT_ABORTED
            config = chosen
        else:
            config = load(arguments.config)
        if arguments.missing_commands:
            print("\n".join(sorted(_absent(preflight.required_commands(config)))))
            return EXIT_OK
        operations = build(config, load_catalog(), mirror=arguments.mirror)
        if arguments.dry_run:
            print(render(operations), end="")
            print(summarise(operations))
            return EXIT_OK
        return install(config, operations, arguments)
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
    except errors.DownloadFailed as error:
        print(f"download: {error}", file=sys.stderr)
        return EXIT_COMMAND
    except errors.CommandFailed as error:
        print(f"command: {error}", file=sys.stderr)
        return EXIT_COMMAND
    except errors.GentooInstallError as error:
        # A named error with no clause of its own still gets its exit code from
        # here, rather than escaping as a traceback that exits 1.
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_COMMAND
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return EXIT_ABORTED


def _keep_the_log(work: Path, target: Path, record: Callable[[str], None]) -> None:
    """Copy the run's log onto the installed system.

    The work directory is a tmpfs on an install medium, so a reboot takes the
    log of the run that failed with it, which is the one anybody would want.
    """
    kept = target / "var/log/gentoo-install"
    try:
        kept.mkdir(parents=True, exist_ok=True)
        for name in ("install.log", "install.jsonl"):
            source = work / name
            if source.is_file():
                shutil.copy2(source, kept / name)
    except OSError as error:
        record(f"warning: the log could not be copied to {kept}: {error}")
        return
    record(f"the log of this run is in {kept}")


def install(config: InstallConfig, operations: tuple[Operation, ...], arguments: argparse.Namespace) -> int:
    """Check the machine, then perform every operation in order."""
    work: Path = arguments.work
    work.mkdir(parents=True, exist_ok=True)
    with (work / "install.log").open("a") as log:

        def record(line: str) -> None:
            print(line, file=log, flush=True)
            print(line, flush=True)

        journal = Journal(path=work / "install.jsonl")
        runner = Runner(log=record, journal=journal)
        probe = Probe(runner=runner, work=work)
        probe.load()
        if not arguments.skip_preflight:
            report = preflight.check(config, probe, str(arguments.target))
            for warning in report.warnings:
                record(f"warning: {warning}")
            report.raise_if_fatal()
        machine = Machine(
            config=config, runner=runner, probe=probe, work=work, mountpoint=arguments.target
        )
        finished = completed(journal) if arguments.resume else frozenset()
        if finished:
            record(f"resuming: {len(finished)} operations were finished by an earlier run")
        try:
            apply(operations, machine, finished)
        finally:
            # In `finally`: the log of a run that failed is the one worth
            # keeping, and it is the one a reboot would otherwise destroy.
            _keep_the_log(work, arguments.target, record)
        counted = journal.counts()
        record(
            f"installed {len(operations)} operations into {arguments.target}; "
            f"{counted.get('binary', 0)} packages from a binary host, "
            f"{counted.get('compiled', 0)} compiled"
        )
    return EXIT_OK


def _absent(wanted: Iterable[str]) -> set[str]:
    return {command for command in wanted if shutil.which(command) is None}


def _from_menu(arguments: argparse.Namespace) -> InstallConfig | None:
    """Walk the screens and return what the operator built, or None."""
    runner = Runner(log=lambda line: None)
    probe = Probe(runner=runner, work=arguments.work)
    context = screens.Context(
        translate=Catalog(tag_for(override=arguments.lang)),
        disks=probe.disks(),
        groups=load_catalog(),
        hash_password=lambda password: fetch.password_hash(password, runner),
        stage_passphrase=lambda text: _stage_passphrase(text, arguments.work),
        timezones=probe.timezones(),
        firmware=Firmware.UEFI if probe.machine().uefi else Firmware.BIOS,
        inspect_disk=lambda disk: (probe.partitions(disk), probe.disk_size(disk)),
    )
    if not context.disks:
        raise errors.DeviceNotFound("this machine reports no disk to install onto")
    if not sys.stdout.isatty():
        # Checked before curses starts: initialising it writes escape codes to
        # the pipe before it discovers there is no terminal.
        raise errors.PreflightFailed("the menu needs a terminal; pass --config FILE")
    start = _blank(context.disks[0][0])
    try:
        finished = curses.wrapper(lambda window: app.run(CursesScreen(window), start, context))
    except curses.error as error:
        raise errors.PreflightFailed(
            f"the menu needs a terminal and this is not one ({error}); pass --config FILE"
        ) from error
    return finished.config


def _blank(disk: str) -> InstallConfig:
    """What the first screen starts from: a layout the operator will replace."""
    graph, root = templates.build(templates.Choice(disk=disk))
    return InstallConfig(disk=DiskConfig(graph=graph, root=root))


def _stage_passphrase(passphrase: str, work: Path) -> str:
    """Write a passphrase where the disk operations read it from.

    Under the work directory, which is a tmpfs on an install medium, so the
    passphrase never reaches a disk this run wrote.
    """
    where = work / "keys" / "tui"
    where.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_file(where, passphrase, 0o600)
    return str(where)
