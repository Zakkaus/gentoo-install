"""Argument parsing and the one place an exception becomes an exit code.

The table is in docs/design.md. Codes 3 and 4 stay apart on purpose: 3 says the
data could not be trusted, 4 says an operation did not finish.
"""

from __future__ import annotations

import argparse
import curses
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Final, Iterable, Sequence

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
from .model.config import DiskConfig, Firmware, InstallConfig, PortageConfig
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
        _require_root(arguments)
        if _needs_network(arguments):
            _require_network()
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
    except OSError as error:
        # The exec layer writes files and reads /proc, and ENOSPC on the target
        # is a command that did not finish, not a configuration mistake.
        print(f"system: {error}", file=sys.stderr)
        return EXIT_COMMAND
    except Exception as error:
        # Last, and deliberately wide: this module is the one place an exception
        # becomes an exit code, and one that escapes exits 1, which means
        # "bad configuration" to anything reading the code.
        print(f"unexpected {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_COMMAND


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


#: The overlay that carries the patched kernel. Its packages are on no package
#: site, so their versions come from the overlay's own listing.
_OVERLAY_PACKAGES: Final[tuple[str, ...]] = ("sys-kernel/gentoo-cjk-kernel",)


def _kernel_versions(atom: str) -> tuple[tuple[str, bool], ...]:
    if atom in _OVERLAY_PACKAGES:
        return fetch.overlay_versions(atom)
    return fetch.package_versions(atom)


def _needs_network(arguments: argparse.Namespace) -> bool:
    """Everything but the two answers a machine can give offline.

    `--missing-commands` lists what is absent and `--config --dry-run` prints a
    plan; the menu reads every version live and an install fetches a stage3.
    """
    if arguments.missing_commands:
        return False
    return not (arguments.dry_run and arguments.config is not None)


def _require_network() -> None:
    """Stop at startup rather than halfway through the install.

    Every version the menu offers is read live, so that the installer runs on
    Alpine or Debian as well as on a Gentoo medium, and no install of any kind
    finishes without fetching a stage3.
    """
    if not fetch.online():
        raise errors.PreflightFailed(
            "this machine cannot reach packages.gentoo.org; the installer needs a network"
        )


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
        fetch_text=fetch.text,
        kernel_versions=_kernel_versions,
        zfs_kernel_max=fetch.zfs_kernel_max(),
        cores=probe.cores(),
        cpu_flags=probe.cpu_flags(),
        supports_v3=probe.supports_v3(),
    )
    if not context.disks:
        raise errors.DeviceNotFound("this machine reports no disk to install onto")
    if not sys.stdout.isatty():
        # Checked before curses starts: initialising it writes escape codes to
        # the pipe before it discovers there is no terminal.
        raise errors.PreflightFailed("the menu needs a terminal; pass --config FILE")
    start = _blank(context.disks[0][0], context.cores, context.cpu_flags)

    def walk(window: object) -> app.Finished:
        display = CursesScreen(window)
        # Asked before the menu: the environment says which language the
        # operator reads, not whether this terminal can draw it.
        if not arguments.lang:
            context.translate = Catalog(screens.language_screen(display, context))
            context.tag = context.translate.tag
            chosen = screens.with_language(start, context.tag)
        else:
            chosen = screens.with_language(start, context.translate.tag)
        return app.run(display, chosen, context)

    try:
        finished = curses.wrapper(walk)
    except curses.error as error:
        raise errors.PreflightFailed(
            f"the menu needs a terminal and this is not one ({error}); pass --config FILE"
        ) from error
    return finished.config


def _blank(disk: str, cores: int, cpu_flags: tuple[str, ...]) -> InstallConfig:
    """What the menu starts from.

    MAKEOPTS and CPU_FLAGS_X86 are filled in from this machine: both are right
    for almost every install, and leaving them empty means the operator has to
    know their own instruction set to get an ordinary build.
    """
    graph, root = templates.build(templates.Choice(disk=disk))
    return InstallConfig(
        disk=DiskConfig(graph=graph, root=root),
        portage=PortageConfig(makeopts=f"-j{cores}", cpu_flags=cpu_flags),
    )


def _require_root(arguments: argparse.Namespace) -> None:
    """Refuse before the menu rather than at the first write.

    Every path but a dry run partitions disks and stages keys under /run, and a
    menu answered as an ordinary user dies on EPERM with the answers thrown away.
    """
    if arguments.dry_run or arguments.missing_commands or os.geteuid() == 0:
        return
    raise errors.PreflightFailed("run as root")


def _stage_passphrase(passphrase: str, work: Path) -> str:
    """Write a passphrase where the disk operations read it from.

    Under the work directory, which is a tmpfs on an install medium, so the
    passphrase never reaches a disk this run wrote.
    """
    where = work / "keys" / "tui"
    where.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_file(where, passphrase, 0o600)
    return str(where)
