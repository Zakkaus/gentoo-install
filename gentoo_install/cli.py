# SPDX-License-Identifier: GPL-2.0-or-later
"""Argument parsing and the one place an exception becomes an exit code.

The table is in docs/design.md. Codes 3 and 4 stay apart on purpose: 3 says the
data could not be trusted, 4 says an operation did not finish.
"""

from __future__ import annotations

import argparse
import curses
import hashlib
import locale
import os
import shutil
import getpass
import sys
import termios
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Final, Mapping, Sequence

from . import errors
from .errors import GentooInstallError, ResumeRefused
from .data import load_catalog, load_timezones
from .exec import fetch, preflight, report
from .exec.apply import Machine, already_degraded, apply, completed
from .exec import probe as probe_module
from .exec.probe import (
    GRUB_DIRECTORIES,
    BootMethod,
    architecture,
    Probe,
    probe_capacities,
    probe_storage_facts,
    secure_boot,
)
from .exec.runner import Runner
from .log import Journal
from .model.device import (
    DeviceGraph,
    DeviceId,
    StorageFacts,
    StorageLayout,
    ZfsPool,
    asks_for_a_share,
)
from .model.hardware import HardwareFacts
from .model.size import Size
from .tui import app, screens
from .tui import context as tui_context
from .tui.curses_screen import CursesScreen, too_small
from .i18n import Catalog, tag_for
from .exec.console import kernel_messages_held
from .model import mirrors, qr, refusals, templates
from .model.architecture import official_subarch
from .model.config import (
    BootloaderConfig,
    Binhost,
    DiskConfig,
    DiskMode,
    Firmware,
    InstallConfig,
    MemoryLaunch,
    MemoryMode,
    MirrorConfig,
    MirrorRegion,
    PortageConfig,
)
from .exec.config import load_source
from .model import authorized
from .model.serialise import to_toml
from .model.validate import validate, validate_memory_launch
from .plan import convert, netboot
from .plan.convert import SWAP_CONFIRMATION
from .plan.build import (
    DEFAULT_MIRROR,
    DEFAULT_VARIANT,
    build,
    running_config,
    stage3_mirror,
)
from .plan.operations import Context, Operation, Stage
from .plan.portage import variant_of
from .plan.render import render, summarise

#: The country whose mirrors are the ones worth offering. Every other answer,
#: and no answer at all, takes the global list.
CN: Final[str] = "CN"

#: What `chroot` answers for its own failure rather than the command's, from
#: its `--help`: 125 for chroot itself, 126 for a command that cannot be
#: invoked, 127 for one that is not there.
CHROOT_COULD_NOT_START: Final[tuple[int, ...]] = (125, 126, 127)

#: What an operator has to know before a conversion starts, because after it
#: there is no way to tell them.
SESSION_IS_THE_LIFELINE: Final[str] = (
    "Keep this session open: once the directories are replaced, a new ssh "
    "login will not work until the machine reboots."
)

#: Everything a run needs to keep: the device map, the staged keys, the log.
WORK = Path("/run/gentoo-install")

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_PREFLIGHT = 2
EXIT_INTEGRITY = 3
EXIT_COMMAND = 4
EXIT_ABORTED = 5


@dataclass
class RunState:
    """Machine state that has to survive until the exit message is printed."""

    disk_was_written: bool = False
    #: The interface language the menu was walked in. The closing questions
    #: are printed after curses has gone, and they were English literals on a
    #: machine whose every screen had been in Chinese.
    language: str = ""

    #: Which stage the last started operation belonged to, so the closing
    #: message can name the evidence for what it claims.
    stage_reached: Stage | None = None

    def operation_started(self, operation: Operation) -> None:
        self.stage_reached = operation.stage
        # Every stage after preflight either writes to a device or into a
        # filesystem mounted from one. Reading `Stage.PARTITION` alone told an
        # operator whose in-place conversion had already replaced /usr that
        # nothing was written: the conversion never partitions, and neither
        # does a run that reuses a layout and only formats.
        if operation.stage is not Stage.PREFLIGHT:
            self.disk_was_written = True


def parser() -> argparse.ArgumentParser:
    parsed = argparse.ArgumentParser(
        prog="gentoo-install", description="Install Gentoo from a configuration file or a menu."
    )
    parsed.add_argument(
        "--config",
        help="install from this file or URL instead of the menu",
    )
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
        "--no-shell",
        action="store_true",
        # Not only the shell: `_unattended` reads this flag, so it also
        # answers every closing question, and `mode = "in-place"` in a
        # configuration file is the authorisation the conversion asks for.
        help="answer no question and offer no root shell, which is what an unattended "
        "run wants; --menu says a person is here after all",
    )
    parsed.add_argument(
        "--menu",
        action="store_true",
        # The help said `open the interface`, and `_once` opens it only when
        # there is no `--config` to apply. With one, this flag says who is at
        # the keyboard and nothing more.
        help="a person is driving this console, so keep the questions --no-shell "
        "would suppress; without --config it also opens the interface. Requires a "
        "terminal on stdin",
    )
    parsed.add_argument(
        "--skip-preflight",
        action="store_true",
        help="install without checking the machine first, for a harness that knows what it booted",
    )
    memory = parsed.add_mutually_exclusive_group()
    memory.add_argument(
        "--ram",
        dest="memory_mode",
        action="store_const",
        const=MemoryMode.RAM,
        help="boot the whole Gentoo CJK ISO in memory",
    )
    memory.add_argument(
        "--lowram",
        dest="memory_mode",
        action="store_const",
        const=MemoryMode.LOWRAM,
        help="boot the Alpine netboot environment in memory",
    )
    parsed.add_argument(
        "--ssh-key",
        default="",
        help=(
            "the public key the memory environment authorises: a key itself "
            "(`ssh-ed25519 ...`, `ssh-rsa ...`, `ecdsa-sha2-nistp256/384/521 ...`), "
            "a file, an https:// or http:// URL, or github:/gitlab: and a username"
        ),
    )
    parsed.add_argument(
        "--ssh-port",
        type=int,
        help="the port where the memory environment's sshd listens",
    )
    parsed.add_argument(
        "--root-password",
        default="",
        help="the root password for the memory environment",
    )
    parsed.add_argument(
        "--bypass",
        action="store_true",
        help="replace the default boot entry instead of arming one boot",
    )
    parsed.add_argument(
        "--disarm",
        action="store_true",
        help="take back an armed boot and delete what it placed",
    )
    return parsed


def _is_readable_file(name: str) -> bool:
    try:
        with Path(name).open("rb"):
            return True
    except OSError:
        return False


def _memory_launch(arguments: argparse.Namespace) -> MemoryLaunch | None:
    mode = arguments.memory_mode
    if mode is None:
        if (
            arguments.ssh_key
            or arguments.ssh_port is not None
            or arguments.root_password
            or arguments.bypass
        ):
            raise errors.ConfigError(
                "--ssh-key, --ssh-port, --root-password and --bypass require "
                "--ram or --lowram"
            )
        return None
    if arguments.ssh_key:
        # Classified here so `github:zakkaus` and a URL are accepted, and a
        # path is checked while the operator is still at the keyboard: a
        # filename that is a typo otherwise fails after the reboot, in the
        # environment they can no longer log in to.
        source = authorized.classify(arguments.ssh_key)
        if source.kind is authorized.KeySourceKind.PATH and not _is_readable_file(
            source.value
        ):
            raise errors.ConfigError(
                f"--ssh-key {source.value} is not a readable file, an ssh- "
                "prefixed public key, a URL, or github:/gitlab: and a username"
            )
    return MemoryLaunch(
        mode=mode,
        ssh_key=arguments.ssh_key,
        ssh_port=arguments.ssh_port,
        root_password=arguments.root_password,
    )


def _memory_ssh_keys(launch: MemoryLaunch, runner: Runner) -> tuple[str, ...]:
    """Read the key source before a boot entry can make the machine reboot."""
    if not launch.ssh_key:
        return ()
    source = authorized.classify(launch.ssh_key)
    if source.kind is authorized.KeySourceKind.LITERAL:
        text = source.value
    elif source.kind is authorized.KeySourceKind.PATH:
        text = runner.run(["cat", "--", source.value]).stdout
    else:
        text = runner.run(["curl", "--fail", "--location", source.value]).stdout
    return authorized.keys_in(text)

def _validate_memory_launch(
    config: InstallConfig, launch: MemoryLaunch, probe: Probe
) -> None:
    validate_memory_launch(config, launch)
    if probe.boot_method() is BootMethod.NONE:
        raise errors.PreflightFailed(
            f"--{launch.mode.value} cannot arm a one-shot boot entry on this machine"
        )
    if launch.mode is MemoryMode.RAM and not config.disk.graph.of_type(ZfsPool):
        print(
            "warning: --ram is slower for a layout without ZFS; --lowram uses the smaller Alpine netboot",
            file=sys.stderr,
        )


def _boot_target(probe: Probe) -> netboot.BootTarget:
    """What `plan/netboot.py` needs about this machine's own bootloader."""
    layout = probe.storage_layout()
    return netboot.BootTarget(
        method=probe.boot_method(),
        esp_mountpoint=layout.esp_mountpoint,
        grub_directory=next(
            (str(one) for one in GRUB_DIRECTORIES if one.is_dir()), None
        ),
        boot_on_the_root_filesystem=layout.boot_same_filesystem,
        secure_boot=secure_boot(),
        architecture=architecture(),
    )


def _disarm_memory_environment(arguments: argparse.Namespace) -> int:
    """Take back an arming from a later invocation.

    The message an unattended arming prints names this, and for a while the
    parser did not: an operator who followed it was answered `unrecognized
    arguments: --disarm`. The target is read from the machine, the same way
    the arming read it, so nothing has to have been kept from that run.
    """
    _require_root(arguments)
    probe = _probe_for(arguments)
    operations = netboot.disarm(target=_boot_target(probe))
    if arguments.missing_commands:
        wanted = set(preflight.ALWAYS)
        for operation in operations:
            wanted |= operation.required_host_commands()
        print("\n".join(sorted(report.absent(frozenset(wanted), probe))))
        return EXIT_OK
    if arguments.dry_run:
        print(render(operations), end="")
        print(summarise(operations))
        return EXIT_OK
    # A configuration with nothing in it: disarming reads the machine and
    # writes to the machine, and every field of an install's configuration is
    # about a target this run does not touch.
    machine = Machine(
        config=InstallConfig(disk=DiskConfig(graph=DeviceGraph.build(()), root=DeviceId(""))),
        runner=probe.runner,
        probe=probe,
        work=arguments.work,
        mountpoint=Path("/"),
    )
    apply(operations, machine, on_start=lambda one: print(one.describe()))
    return EXIT_OK


def _arm_memory_environment(
    config: InstallConfig, launch: MemoryLaunch, arguments: argparse.Namespace
) -> int:
    """Place the environment and arm one boot into it, then ask about rebooting.

    This path returns instead of installing: what it arms happens after the
    reboot, and running the install now would install onto the disk the
    operator asked to be erased from a live environment.
    """
    probe = Probe(runner=Runner(log=lambda line: None), work=arguments.work)
    target = _boot_target(probe)
    keys = tuple(config.system.authorized_keys)
    if not arguments.missing_commands:
        keys += _memory_ssh_keys(launch, probe.runner)
    operations = netboot.build(
        launch=launch,
        target=target,
        bypass=arguments.bypass,
        # Rendered rather than the file the operator passed: a run from the
        # menu has no file, and the environment must install what was chosen.
        configuration=to_toml(config),
        # Where this installer is, so the payload carries the revision that
        # wrote that configuration.
        source=str(Path(__file__).resolve().parent.parent),
        keys=keys,
        region=config.portage.mirrors.region,
    )
    if arguments.missing_commands:
        # The arming's own commands, not an ordinary install's: `--ram` reads
        # the ISO with `xorriso`, which no install needs and which the guest
        # therefore never had. Asked without the mode, this answers for a
        # different run than the one about to happen.
        wanted = set(preflight.ALWAYS)
        for operation in operations:
            wanted |= operation.required_host_commands()
        print("\n".join(sorted(report.absent(frozenset(wanted), probe))))
        return EXIT_OK
    if arguments.dry_run:
        print(render(operations), end="")
        print(summarise(operations))
        return EXIT_OK
    machine = Machine(
        config=config,
        runner=probe.runner,
        probe=probe,
        work=arguments.work,
        # `/`, not a mounted new system: everything this places goes on the
        # machine the operator is logged into.
        mountpoint=Path("/"),
    )
    apply(operations, machine, on_start=lambda one: print(one.describe()))
    return _reboot_or_disarm(target, arguments, machine)


def _reboot_or_disarm(
    target: netboot.BootTarget, arguments: argparse.Namespace, machine: Machine
) -> int:
    """Ask, or say what is armed and leave it to the operator.

    A question on a run nobody is watching is a run that waits for ever, so an
    unattended one is told what was armed and returns rather than rebooting a
    machine on its own.
    """
    if _unattended(arguments):
        print("armed. `reboot` when ready; `--disarm` takes it back.")
        return EXIT_OK
    print("The memory environment is armed for the next boot.")
    print("Type reboot to restart into it, anything else to take it back.")
    if input("> ").strip() == "reboot":
        machine.run(["reboot"])
        return EXIT_OK
    apply(netboot.disarm(target=target), machine, on_start=lambda one: print(one.describe()))
    return EXIT_ABORTED


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = parser().parse_args(argv)
    except SystemExit as error:
        if error.code == 2:
            return EXIT_CONFIG
        raise
    if arguments.menu and not sys.stdin.isatty():
        # Outside the walk, because a preflight refusal with no `--config` is
        # carried back into the menu as a reason to answer differently, and no
        # answer makes a pipe into a terminal.
        print(
            "preflight: --menu says a person is driving this console, but "
            "stdin is not a terminal",
            file=sys.stderr,
        )
        return EXIT_PREFLIGHT
    state = RunState(disk_was_written=bool(arguments.resume))
    # Why the previous walk's answers were turned down, carried back into the
    # menu. A refusal that reaches the operator as a line on a dead terminal
    # costs them every other answer: one whose partition sizes came to exactly
    # the size of the disk was dropped to a shell with twenty rows filled in.
    refused = ""
    while True:
        outcome = _once(arguments, state, refused)
        if isinstance(outcome, int):
            return outcome
        if outcome == refused:
            # The same reason twice means the walk did not change what was
            # wrong, and offering the menu again would ask the operator the
            # same question for ever.
            _print_machine_state(state)
            print(f"preflight: {outcome}", file=sys.stderr)
            return EXIT_PREFLIGHT
        refused = outcome


def _once(arguments: argparse.Namespace, state: RunState, refused: str) -> int | str:
    """One attempt. A string is why the menu should be walked again."""
    try:
        if arguments.disarm:
            # Before everything else: the machine is armed and the operator
            # wants it not to be, which needs no configuration beyond the one
            # they already have and no network at all.
            return _disarm_memory_environment(arguments)
        launch = _memory_launch(arguments)
        _require_root(arguments)
        if arguments.config is None:
            if arguments.missing_commands:
                # Nothing to derive a layout from, so answer for the commands
                # every install needs whatever it is about to do.
                print(
                    "\n".join(
                        sorted(
                            report.absent(
                                (*preflight.ALWAYS, *preflight.MENU_ONLY),
                                _probe_for(arguments),
                            )
                        )
                    )
                )
                return EXIT_OK
            if _unattended(arguments):
                raise errors.PreflightFailed("an unattended run needs --config FILE")
            if _needs_network(arguments):
                # Before any reachability check: an unset clock makes every HTTPS
                # request fail, and the message would name the network instead.
                _check_the_clock()
                # The menu reads every version from the package site.
                _require_network()
            chosen = _from_menu(arguments, refused, state)
            if chosen is None:
                print("cancelled", file=sys.stderr)
                return EXIT_ABORTED
            config = chosen
        else:
            config = _configuration_from(arguments.config)
        if not arguments.missing_commands:
            catalog = load_catalog()
            validate(
                config,
                available_timezones=load_timezones(),
                available_package_groups=catalog,
                declared_desktop_profiles={
                    name: group.profile for name, group in catalog.items() if group.profile
                },
            )
        if launch is not None:
            if arguments.config is not None and _needs_network(arguments):
                _check_the_clock()
            # The refusals come after the question about commands, because
            # one of them is `--ram cannot arm a one-shot boot entry on this
            # machine`, which is what a machine without `efibootmgr` answers —
            # and `efibootmgr` is one of the commands being asked about.
            if not arguments.missing_commands:
                _validate_memory_launch(config, launch, _probe_for(arguments))
            return _arm_memory_environment(config, launch, arguments)
        if arguments.missing_commands:
            print(
                "\n".join(
                    sorted(
                        report.absent(
                            preflight.required_commands(config), _probe_for(arguments)
                        )
                    )
                )
            )
            return EXIT_OK
        needs_network = config.disk.mode is not DiskMode.DD and _needs_network(arguments)
        if needs_network:
            _check_the_clock()
            _require_mirror(config, arguments.mirror)
        storage_facts = StorageFacts()
        loader_v3: bool | None = None
        layout: StorageLayout | None = None
        hardware = HardwareFacts()
        if config.disk.mode is not DiskMode.DD:
            reading = Probe(runner=Runner(log=lambda line: None), work=arguments.work)
            hardware = reading.hardware()
        if config.disk.layout_is_read_from_the_machine:
            # Even for a dry run: a conversion's whole plan is derived from the
            # running machine, so without this there is nothing to print.
            layout = Probe(
                runner=Runner(log=lambda line: None), work=arguments.work
            ).storage_layout()
        if arguments.dry_run and asks_for_a_share(config.disk.graph):
            # A capacity even for a dry run, and only when a share needs one:
            # a share is a share of the disk, so without it the printed plan
            # cannot say what `40%` is, and a plan that prints a different
            # number from the one the install writes is the defect this whole
            # feature exists to avoid. Gated on the share rather than on the
            # mode, so a configuration that asks for nothing reads nothing.
            storage_facts = StorageFacts(
                capacities=probe_capacities(config.disk.graph, reading)
            )
        if not arguments.dry_run and config.disk.mode is not DiskMode.DD:
            # The heavier evidence — mdraid metadata, free extents — is only
            # required before a real install.
            storage_facts = probe_storage_facts(config, reading)
            # `ld.so --help`, the loader's own answer about this machine.
            loader_v3 = reading.supports_v3()
        operations = build(
            config,
            catalog,
            mirror=arguments.mirror,
            storage_facts=storage_facts,
            layout=layout,
            supports_v3=loader_v3,
            hardware=hardware,
        )
        if arguments.dry_run:
            print(render(operations), end="")
            print(summarise(operations))
            return EXIT_OK
        # The derived one for the machine, the operator's own for preflight:
        # the check that refuses a mounted disk has to see the empty graph a
        # conversion was given, and everything that resolves a `DeviceId` has
        # to see the graph read from the machine.
        return install(config, operations, arguments, state, running_config(config, layout))
    except errors.DeviceNotFound as error:
        _print_machine_state(state)
        print(f"device: {error}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except errors.WorkDirectoryBusy as error:
        # Preflight, not a command failure: nothing has run, and the operator
        # can act on it by waiting or choosing another `--work`.
        print(f"work directory: {error}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except errors.ResumeRefused as error:
        # Before the machine state is printed: nothing of this run has touched
        # the disks, and the state to report is the earlier run's.
        print(f"resume: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except errors.ConfigError as error:
        # Back to the menu for the same reason a refused preflight goes back:
        # the answers came from there and none of them has reached a disk. A
        # ZFS mirror built with one member ended a session on `no node with
        # id ''`, which is not a sentence an operator can act on and not a
        # reason to lose the other nineteen rows.
        if arguments.config is None and not state.disk_was_written:
            return str(error)
        _print_machine_state(state)
        print(f"configuration: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except errors.PreflightFailed as error:
        # Back to the menu when that is where the answers came from and none
        # of them has reached a disk: the operator has one thing to change and
        # nineteen to keep.
        if arguments.config is None and not state.disk_was_written:
            return str(error)
        _print_machine_state(state)
        print(f"preflight: {error}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except errors.ConversionUnsupported as error:
        # Not a command failure: nothing ran. The machine is one this installer
        # declines to convert, which is what code 2 says.
        _print_machine_state(state)
        print(f"conversion: {error}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except errors.IntegrityError as error:
        _print_machine_state(state)
        print(f"integrity: {error}", file=sys.stderr)
        return EXIT_INTEGRITY
    except errors.DownloadFailed as error:
        _print_machine_state(state)
        print(f"download: {error}", file=sys.stderr)
        return EXIT_COMMAND
    except errors.CommandFailed as error:
        _print_machine_state(state)
        print(f"command: {error}", file=sys.stderr)
        return EXIT_COMMAND
    except errors.GentooInstallError as error:
        # A named error with no clause of its own still gets its exit code from
        # here, rather than escaping as a traceback that exits 1.
        _print_machine_state(state)
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_COMMAND
    except KeyboardInterrupt:
        _print_machine_state(state)
        print("aborted", file=sys.stderr)
        return EXIT_ABORTED
    except OSError as error:
        # The exec layer writes files and reads /proc, and ENOSPC on the target
        # is a command that did not finish, not a configuration mistake.
        _print_machine_state(state)
        print(f"system: {error}", file=sys.stderr)
        return EXIT_COMMAND
    except Exception as error:
        # Last, and deliberately wide: this module is the one place an exception
        # becomes an exit code, and one that escapes exits 1, which means
        # "bad configuration" to anything reading the code.
        _print_machine_state(state)
        print(f"unexpected {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_COMMAND


def _print_machine_state(state: RunState) -> None:
    """Tell the operator whether a failed run changed this machine's storage.

    The stage is named because the claim rests on it: an operator reading
    `may not boot` decides between rebooting and rescuing, and the two
    answers differ by which stage the run had reached.
    """
    if not state.disk_was_written:
        print("nothing was written: the run stopped before any operation ran", file=sys.stderr)
        return
    if state.stage_reached is None:
        print("this machine's storage has been written to and it may not boot", file=sys.stderr)
        return
    print(
        f"the {state.stage_reached.value} stage started, so this machine's storage "
        "has been written to and it may not boot",
        file=sys.stderr,
    )


def install(
    config: InstallConfig,
    operations: tuple[Operation, ...],
    arguments: argparse.Namespace,
    state: RunState,
    running: InstallConfig | None = None,
) -> int:
    """Check the machine, then perform every operation in order."""
    work: Path = arguments.work
    translate = _closing_catalog(state, arguments)
    # A conversion replaces the running userland, so every operation after the
    # swap acts on `/`. Left at `--target` they would chroot into a directory
    # the machine does not have.
    target: Path = (
        Path("/") if config.disk.mode is DiskMode.IN_PLACE else arguments.target
    )
    with report.recording(work, target) as active_report:
        record = active_report.record
        journal = active_report.journal
        runner = Runner(log=record, journal=journal)
        probe = Probe(runner=runner, work=work)
        probe.load()
        if not arguments.skip_preflight:
            preflight_report = preflight.check(
                config, probe, str(target), operations=operations
            )
            for warning in preflight_report.warnings:
                record(f"warning: {warning}")
            preflight_report.raise_if_fatal()
        if config.disk.mode is DiskMode.IN_PLACE and not _confirmed_swap(arguments, record):
            return EXIT_ABORTED
        machine = Machine(
            config=running if running is not None else config,
            runner=runner,
            probe=probe,
            work=work,
            mountpoint=target,
        )
        identity = _run_identity(config)
        if arguments.resume:
            resuming = journal.resume()
            _refuse_a_different_run(journal, identity, record)
            _refuse_a_resume_with_no_journal(arguments.work, resuming)
        else:
            journal.started(**identity)
        finished = completed(journal) if arguments.resume else frozenset()
        if arguments.resume:
            # Replayed before anything runs. The operation that recorded an
            # unusable binary host has already completed and is skipped, so
            # without this the next `Emerge` asked that host for packages the
            # earlier run had declared untrusted.
            for what in sorted(already_degraded(journal)):
                machine.given_up.add(what)
                record(f"resuming: {what} was already unavailable")
        if finished:
            record(f"resuming: {len(finished)} operations were finished by an earlier run")
        # The closing stage unmounts, so the target has to still be mounted
        # when the operator is offered a shell in it.
        closing = tuple(one for one in operations if one.stage is Stage.FINISH)
        body = tuple(one for one in operations if one.stage is not Stage.FINISH)
        failure: BaseException | None = None
        try:
            apply(body, machine, finished, state.operation_started)
        except BaseException as error:
            failure = _first_failure(failure, error, record)
        stopped = failure is not None
        try:
            report.offer_paste(
                work,
                record,
                stopped,
                _unattended(arguments),
                _asked,
                show_the_address,
                translate,
            )
            if config.disk.mode is not DiskMode.DD:
                _offer_a_shell(
                    arguments, machine, record, stopped, target, config.disk.mode, translate
                )
        except BaseException as error:
            failure = _first_failure(failure, error, record)
        if config.disk.mode is not DiskMode.DD:
            # Before the closing stage: that stage unmounts the target, so a later
            # copy lands on the install medium's tmpfs and vanishes at reboot.
            try:
                report.keep_log(work, target, record)
            except BaseException as error:
                failure = _first_failure(failure, error, record)
        if failure is None:
            try:
                apply(closing, machine, finished)
            except BaseException as error:
                failure = _first_failure(failure, error, record)
        if failure is not None:
            # Only release operations run after a failure. Configuring an
            # incomplete target can obscure the error that stopped the run.
            _release(closing, machine, record)
            raise failure
        if config.disk.mode is DiskMode.DD:
            record(f"wrote the prepared image to {config.disk.destination}")
        else:
            counted = journal.counts()
            record(
                f"installed {len(operations)} operations into {target}; "
                f"{counted.get('binary', 0)} packages from a binary host, "
                f"{counted.get('compiled', 0)} compiled"
            )
    return EXIT_OK


def _first_failure(
    first: BaseException | None,
    current: BaseException,
    record: Callable[[str], None],
) -> BaseException:
    """Keep the error that caused shutdown while recording later failures."""
    if first is None:
        record(f"the install stopped: {type(current).__name__}: {current}")
        return current
    record(f"warning: while handling that failure: {type(current).__name__}: {current}")
    return first


def _release(
    closing: tuple[Operation, ...], machine: Context, record: Callable[[str], None]
) -> None:
    """Unmount and let go, whatever else went wrong.

    Each on its own, and none of them fatal: the install has already failed and
    the exception that matters is the one being carried out of here.
    """
    for operation in closing:
        if not operation.releases_the_machine:
            continue
        try:
            operation.apply(machine)
        except BaseException as error:
            # Release is best-effort because a prior failure owns the exit
            # category, but later release operations still have work to do.
            record(f"warning: {operation.describe()}: {error}")


def _confirmed_swap(
    arguments: argparse.Namespace, record: Callable[[str], None]
) -> bool:
    """Name what the conversion replaces and what it leaves, then ask once.

    Unattended runs are not asked: `mode = "in-place"` in a configuration file
    is the authorisation, and a question here would hold a serial console open
    for ever. The list is recorded either way, so a log says what was replaced.
    """
    replaced = ", ".join("/" + name for name in convert.REPLACED_DIRECTORIES)
    record(f"in-place conversion replaces {replaced} and leaves everything else")
    # Recorded before the unattended return, because an unattended run is the
    # one nobody is watching: measured on Debian 12 and Arch, a new ssh login
    # stops working once `/usr` and `/etc` belong to the new system, while the
    # session that started the run keeps its own mapped binaries.
    record(SESSION_IS_THE_LIFELINE)
    if _unattended(arguments):
        return True
    print(f"This replaces {replaced} on the running system.")
    print("Everything else, including /home and /root, is left alone.")
    print(SESSION_IS_THE_LIFELINE)
    print(f"Type {SWAP_CONFIRMATION} to continue, anything else to stop.")
    try:
        answer = input("> ").strip()
    except EOFError:
        answer = ""
    if answer == SWAP_CONFIRMATION:
        return True
    record("in-place conversion was not confirmed")
    print("nothing was changed", file=sys.stderr)
    return False


def _unattended(arguments: argparse.Namespace) -> bool:
    """Whether there is nobody at the keyboard to answer a question.

    `--no-shell` says the closing offer of a root shell is unwanted, not that
    nobody is here, and the driver CD adds it to every run including the one an
    operator drives a key at a time. `--menu` is how that side says otherwise.
    """
    if arguments.menu:
        return False
    return bool(arguments.no_shell) or not sys.stdin.isatty()


def _draws_wide_characters() -> bool:
    """Whether this terminal's encoding can carry a CJK glyph.

    `LC_CTYPE`, not `LANG`: the codeset is what ncurses reads, and an operator
    who exported `LC_ALL=C` over a Chinese `LANG` has a terminal that cannot
    draw one whatever the environment says it prefers.

    Ncurses and libc know a glyph's cell width, but cannot inspect the font in
    the terminal emulator; writing it and reading the cursor repeats that width.
    """
    return locale.nl_langinfo(locale.CODESET).upper().replace("-", "") == "UTF8"


def _closing_catalog(state: RunState, arguments: argparse.Namespace) -> Catalog:
    """What the questions after the menu are written in.

    The menu's own tag when there was a menu, and what the environment says
    otherwise: `--config` never opens a screen and has no tag to carry.
    """
    return Catalog(state.language or tag_for(override=arguments.lang))


def _configuration_from(source: str) -> InstallConfig:
    """The configuration, asking for a password when the paste needs one.

    Asked rather than taken from the command line: a password there reaches
    the shell's history and every `ps` on the machine. An unattended run has
    no terminal to ask, so the refusal stands and names what is missing.
    """
    try:
        return load_source(source)
    except errors.ConfigError as refused:
        if "encrypted paste" not in str(refused) or not sys.stdin.isatty():
            raise
    _forget_what_was_typed()
    return load_source(source, getpass.getpass("password for this paste: "))


def _asked(question: str) -> bool:
    """Ask, after throwing away what was typed before the question existed.

    The menu is curses and the key that left it is still in the terminal's
    input queue when this runs, so `readline` returned it at once: both
    questions after a failed install were answered by keystrokes aimed at the
    screen before them, and the operator watched two offers go past.
    """
    _forget_what_was_typed()
    print(f"{question} [y/N] ", end="")
    sys.stdout.flush()
    try:
        return sys.stdin.readline().strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _forget_what_was_typed() -> None:
    """Discard input that arrived before the question was printed.

    Nothing to discard on a terminal this run does not own, and `tcflush`
    raises there rather than answering.
    """
    if not sys.stdin.isatty():
        return
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (termios.error, OSError, ValueError):
        # A console that cannot be flushed still gets its question asked; the
        # worst case is the one this replaces.
        return


#: Black on white, so the code reads as a code. A terminal's own colours are
#: light on dark, which draws every symbol inverted; most scanners cope and
#: some do not, and there is no way to find out which one is pointed at it.
_INVERTED: Final[str] = "\x1b[30;47m"
_PLAIN: Final[str] = "\x1b[0m"


def show_the_address(url: str) -> None:
    """A code for the address, then the address.

    The machine showing this is the one with no browser and no way to copy a
    line off its console. The code goes first so the address is the line left
    on screen, and the address is printed whether or not the code fits.
    """
    for line in _code_for(url):
        print(line)
    print(url)


def _code_for(url: str) -> list[str]:
    """The code as lines, or nothing when it does not fit the terminal."""
    try:
        drawn = qr.halved(qr.encode(url))
    except GentooInstallError:
        return []
    columns, lines = shutil.get_terminal_size((80, 24))
    if len(drawn[0]) > columns or len(drawn) + 2 > lines:
        return []
    if not sys.stdout.isatty():
        return drawn
    return [f"{_INVERTED}{line}{_PLAIN}" for line in drawn]


def _offer_a_shell(
    arguments: argparse.Namespace,
    machine: Machine,
    record: Callable[[str], None],
    stopped: bool,
    target: Path,
    mode: DiskMode,
    translate: Catalog,
) -> None:
    """A root shell in the target before it is unmounted.

    Offered after a failure as well as after a success: the operator is the one
    who can tell whether the machine is fixable, and once the target is
    unmounted they would have to mount the whole layout again by hand.

    `target`, not `arguments.target`: a conversion's target is the running `/`
    and it never created `/mnt/gentoo`, so the unresolved value offered a
    chroot into a directory the machine does not have.
    """
    if _unattended(arguments):
        return
    question = (
        translate("enter a root shell in the converted system at {target}?")
        if mode is DiskMode.IN_PLACE
        else translate("enter a root shell in {target} before unmounting?")
    ).format(target=target)
    if not _asked(question):
        return
    opened = machine.runner.run(["chroot", str(target), "/bin/bash", "--login"], check=False)
    if opened.returncode in CHROOT_COULD_NOT_START:
        # Recorded before the run, the log said a shell had opened and exited
        # on a target where `chroot` never started one.
        record(f"no root shell could be started in {target}: exit {opened.returncode}")
        return
    record(f"a root shell was opened in {target}")
    record("the shell exited" if mode is DiskMode.IN_PLACE else "the shell exited; unmounting")


def _probe_for(arguments: argparse.Namespace) -> Probe:
    """A probe that says nothing: `--missing-commands` writes one command per
    line and the launcher reads every line of it."""
    return Probe(runner=Runner(log=lambda line: None), work=arguments.work)


#: The overlay that carries the patched kernel. Its packages are on no package
#: site, so their versions come from the overlay's own listing.
_OVERLAY_PACKAGES: Final[tuple[str, ...]] = (
    "sys-kernel/gentoo-cjk-kernel-bin",
    "sys-kernel/gentoo-cjk-kernel",
    "sys-kernel/xanmod-kernel",
)


def _kernel_versions(atom: str) -> tuple[tuple[str, bool], ...]:
    if atom in _OVERLAY_PACKAGES:
        return fetch.overlay_versions(atom)
    return fetch.package_versions(atom)


def _require_mirror(config: InstallConfig, fallback: str) -> None:
    """The address this install will fetch its stage3 from, and no other.

    `packages.gentoo.org` is what the menu reads; an install given a
    configuration never touches it. Requiring it stopped five installs on a
    network where the chosen mirror answered and that site did not.
    """
    mirror = stage3_mirror(config, fallback)
    said = fetch.why_mirror_unreachable(mirror, variant_of(config))
    if said:
        raise errors.PreflightFailed(
            f"this machine cannot reach {mirror}; the install fetches its stage3 there: {said}"
        )


def _needs_network(arguments: argparse.Namespace) -> bool:
    """Everything but the two answers a machine can give offline.

    `--missing-commands` lists what is absent and `--config --dry-run` prints a
    plan; the menu reads every version live and an install fetches a stage3.
    """
    if arguments.missing_commands:
        return False
    return not (arguments.dry_run and arguments.config is not None)


def _check_the_clock() -> None:
    """A clock far enough out makes every certificate look not-yet-valid.

    Read over plain HTTP and set with `hwclock`, because a live medium carries
    neither chrony nor ntpd. Reported and corrected rather than refused: the
    machine is otherwise fine and the operator has nothing to fix by hand.
    """
    stamp = fetch.network_time()
    if not stamp or abs(stamp - time.time()) < fetch.CLOCK_TOLERANCE:
        return
    when = datetime.fromtimestamp(stamp, tz=timezone.utc)
    print(
        f"warning: this machine's clock is more than a day out; setting it to {when:%F %T} UTC",
        file=sys.stderr,
    )
    runner = Runner(log=lambda line: None)
    setting = runner.run(
        ["hwclock", "--utc", "--set", "--date", when.strftime("%Y-%m-%d %H:%M:%S")], check=False
    )
    if setting.returncode != 0:
        # Said rather than swallowed: busybox has no `hwclock --set`, and every
        # HTTPS request then fails on a not-yet-valid certificate while the
        # next message blames the network.
        print(
            "warning: the clock could not be set; TLS may refuse every mirror "
            f"until it is corrected by hand ({setting.stdout.strip()[:80]})",
            file=sys.stderr,
        )
        return
    applied = runner.run(["hwclock", "--hctosys", "--utc"], check=False)
    if applied.returncode != 0:
        # The same warning as the line above, for the same reason: the RTC now
        # holds the right time and the system clock does not, so TLS refuses
        # every mirror and the next message blames the network.
        print(
            "warning: the system clock could not be set from the corrected RTC; "
            f"TLS may refuse every mirror ({applied.stdout.strip()[:80]})",
            file=sys.stderr,
        )


def _require_network() -> None:
    """Stop at startup rather than halfway through, but only when nothing answers.

    The version rows are read from `packages.gentoo.org` and the install is
    read from a mirror, so the package site being unreachable costs the pinned
    versions and nothing else. `mirror_online` already carries the same lesson
    from the configuration path, where requiring that site stopped five runs on
    a network the mirror answered on; one cluster guest then resolved it to an
    IPv6 address alone with no route to reach that, and was refused at the
    first screen with a working mirror one row away.
    """
    refused = fetch.why_offline()
    if not refused:
        return
    answering = _a_mirror_that_answers()
    if answering:
        print(
            "warning: packages.gentoo.org did not answer, so the version rows "
            f"offer only what the keywords allow ({refused}); {answering} answers",
            file=sys.stderr,
        )
        return
    raise errors.PreflightFailed(
        "this machine reaches neither packages.gentoo.org nor any mirror this "
        f"installer offers, so no install can fetch a stage3: {refused}"
    )


def _a_mirror_that_answers() -> str:
    """The name of the first mirror that serves a stage3, or an empty string.

    Every site of the region, not `DEFAULT_MIRROR` alone: a machine in China
    that cannot reach `distfiles.gentoo.org` was refused at the first screen
    with USTC one row away, which is the same refusal the comment above records
    for `packages.gentoo.org`.
    """
    if fetch.mirror_online(DEFAULT_MIRROR, DEFAULT_VARIANT):
        return DEFAULT_MIRROR
    for site in mirrors.gentoo_sites(_region(fetch.egress_country())):
        if site.releases and fetch.mirror_online(site.distfiles, DEFAULT_VARIANT):
            return site.distfiles
    return ""


def _conversion_offer(
    probe: Probe,
) -> tuple[str, refusals.Refusal, StorageLayout | None]:
    """What the running system is, and why it cannot be converted in place.

    The refusal is what the menu shows instead of the option. Everything that
    would stop a conversion is asked here rather than after the operator has
    answered twenty screens: a live medium has no system to replace,
    `layout_graph` refuses a root this installer cannot describe, and a machine
    that cannot be read at all is refused rather than offered one blind.
    """
    medium = probe.live_medium()
    if medium:
        return "", refusals.Refusal(refusals.LIVE_MEDIUM, medium), None
    # Measured on an Alpine cloud image: without these the layout reads back
    # empty and the refusal blamed the root device rather than the package.
    lacking = report.absent(preflight.LAYOUT_COMMANDS, probe)
    if lacking:
        return (
            "",
            refusals.Refusal(refusals.CANNOT_READ_THE_SYSTEM, ", ".join(sorted(lacking))),
            None,
        )
    try:
        layout = probe.storage_layout()
    except GentooInstallError as error:
        # The message is the detail: English beside the translated reason, and
        # it names the device the reading failed on.
        return "", refusals.Refusal(refusals.CANNOT_READ_THE_SYSTEM, str(error)), None
    described = f"{layout.root_device} on {layout.root_filesystem_type}"
    try:
        convert.layout_graph(layout)
    except GentooInstallError:
        # The exception says which filesystem, in English, for the log. The
        # menu already draws `Running system:` beside this, so the reason it
        # shows is a catalog key rather than the message.
        return (
            described,
            refusals.Refusal(refusals.CANNOT_DESCRIBE_THE_ROOT, layout.root_filesystem_type or ""),
            None,
        )
    return described, refusals.OFFERED, layout

def _image_write_offer(probe: Probe) -> refusals.Refusal:
    """Why the whole-disk image writer must stay unavailable on this machine."""
    if probe.live_medium() or probe.memory_environment():
        return refusals.OFFERED
    return refusals.Refusal(refusals.WOULD_OVERWRITE_THE_INSTALLER)



def installer_digest() -> str:
    """A digest of the installer's own source, so a resume can tell one
    installer from another.

    Read rather than declared: the package carries no version, and a live
    medium is where a source tree is most likely to have been edited between
    the run that stopped and the run that carries it on.
    """
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _run_identity(config: InstallConfig) -> dict[str, str]:
    """What this run is: its configuration, its live session, its installer."""
    return {
        "configuration": hashlib.sha256(to_toml(config).encode("utf-8")).hexdigest(),
        "session": probe_module.session_id(),
        "installer": installer_digest(),
    }


#: What each mismatch means, in the operator's terms. A resume carries on from
#: operations recorded by position and text, so every one of these makes the
#: recorded positions describe something other than the plan about to run.
_RESUME_REFUSALS: Final[dict[str, str]] = {
    "configuration": (
        "this journal was written for a different configuration; start a new "
        "run or pass the configuration the first one used"
    ),
    "session": (
        "this journal was written before the machine rebooted, and a resumed "
        "run needs the state the first one left in memory"
    ),
    "installer": (
        "this journal was written by a different installer, which may number "
        "its operations differently; start a new run"
    ),
}


def _refuse_a_different_run(
    journal: Journal, identity: Mapping[str, str], record: Callable[[str], None]
) -> None:
    """Refuse a journal another run wrote.

    A journal with no identity is from a run that predates this and is resumed
    the way it always was, with a line saying so.
    """
    held = journal.identity()
    if held is None:
        record("resuming: the journal records no identity, so it is not checked")
        return
    for name, refusal in _RESUME_REFUSALS.items():
        now, recorded = identity.get(name, ""), held.get(name, "")
        # An empty value on either side is a machine that cannot answer, and
        # the boot id is the one that can be missing; comparing against it
        # would refuse every resume there.
        if now and recorded and now != recorded:
            raise ResumeRefused(refusal)

def _refuse_a_resume_with_no_journal(work: Path, resuming: bool) -> None:
    """A resume with nothing to resume is a full install, so it is refused.

    `--resume` says it carries on "instead of partitioning the disk again",
    and with no journal to read the run started a fresh one, skipped nothing
    and applied the whole list. The work directory is a tmpfs, so the ordinary
    way to arrive here is a reboot -- which is exactly when an operator
    believes a resume is what they asked for.
    """
    if resuming:
        return
    raise ResumeRefused(
        f"--resume was given and {work} holds no journal to resume: the work "
        "directory does not survive a reboot, so this would partition the disk "
        "again rather than carry on. Run without --resume to install from the "
        "beginning."
    )


def _from_menu(
    arguments: argparse.Namespace, refused: str = "", state: "RunState | None" = None
) -> InstallConfig | None:
    """Walk the screens and return what the operator built, or None.

    `refused` is why an earlier walk's answers were turned down. Shown before
    the first screen rather than printed and left on a dead terminal: an
    operator whose partition sizes did not fit the disk was dropped back to a
    shell with every other answer thrown away.
    """
    runner = Runner(log=lambda line: None)
    probe = Probe(runner=runner, work=arguments.work)
    has_ipv4, has_ipv6 = probe.address_families()
    # Checked before the first screen: the menu hashes a password with
    # `openssl`, and finding it absent at that point throws away every answer.
    lacking = report.absent(preflight.MENU_ONLY)
    if lacking:
        raise errors.PreflightFailed(f"the menu needs {', '.join(sorted(lacking))}")
    running_system, conversion_refused, running_layout = _conversion_offer(probe)
    image_write_refused = _image_write_offer(probe)
    context = tui_context.Context(
        translate=Catalog(tag_for(override=arguments.lang)),
        ipv4=has_ipv4,
        ipv6=has_ipv6,
        profile_paths=probe.amd64_profiles(),
        disks=probe.disks(),
        names_for=probe.names_for,
        groups=load_catalog(),
        hash_password=lambda password: fetch.password_hash(password, runner),
        stage_passphrase=lambda text: report.stage_passphrase(text, arguments.work),
        timezones=probe.timezones(),
        firmware=Firmware.UEFI if probe.machine().uefi else Firmware.BIOS,
        inspect_disk=lambda disk: (probe.partitions(disk), probe.disk_size(disk)),
        fetch_text=fetch.text,
        kernel_versions=_kernel_versions,
        keymaps=probe.keymaps,
        timezone_here=probe.timezone_here(),
        zfs_kernel_max=fetch.zfs_kernel_max(),
        cores=probe.cores(),
        memory=Size(probe.machine().memory_bytes),
        cpu_flags=probe.cpu_flags(),
        supports_v3=probe.supports_v3(),
        save_config=_save_config,
        publish_config=report.publish_config,
        zfs_unavailable=probe.zfs_support(),
        configs_here=report.configs_here(app.SAVE_AS),
        running_system=running_system,
        running_layout=running_layout,
        conversion_refused=conversion_refused,
        image_write_refused=image_write_refused,
        load_config=load_source,
    )
    if not context.disks:
        raise errors.DeviceNotFound("this machine reports no disk to install onto")
    if not sys.stdout.isatty():
        # Checked before curses starts: initialising it writes escape codes to
        # the pipe before it discovers there is no terminal.
        raise errors.PreflightFailed("the menu needs a terminal; pass --config FILE")
    start = _blank(
        context.disks[0][0],
        context.cores,
        context.cpu_flags,
        context.supports_v3,
        fetch.egress_country(),
        context.firmware,
    )

    def walk(window: object) -> app.Finished:
        display = CursesScreen(window, lambda source: context.translate(source))
        cramped = too_small(display)
        if cramped:
            raise errors.PreflightFailed(cramped)
        # A terminal whose encoding is not UTF-8 draws a wide character as its
        # bytes, and ncurses advances one cell for each: the widths this code
        # computes stay right and the text lands somewhere else, so a footer of
        # three CJK segments overwrites itself. English is offered instead of a
        # menu made of wreckage.
        if not _draws_wide_characters():
            context.translate = Catalog("en")
            context.tag = "en"
            if state is not None:
                state.language = "en"
            return app.run(display, screens.with_language(start, "en"), context)
        # Asked before the menu: the environment says which language the
        # operator reads, not whether this terminal can draw it.
        if not arguments.lang:
            context.translate = Catalog(screens.language_screen(display, context))
            context.tag = context.translate.tag
            chosen = screens.with_language(start, context.tag)
        else:
            chosen = screens.with_language(start, context.translate.tag)
        if state is not None:
            state.language = context.translate.tag
        if refused:
            # After the language, so it is readable, and before the saved
            # configuration question, so an operator who is about to load a
            # file has already been told why the last attempt was turned down.
            tui_context.say(display, context, refused)
        # Before the menu and after the language: the question has to be
        # readable, and loading a file replaces every answer behind it.
        offered = screens.saved_config_screen(display, chosen, context)
        return app.run(display, offered.unwrap() if offered.chosen else chosen, context)

    # Before `initscr`, which `wrapper` calls. Python sets `LC_CTYPE` from the
    # environment at startup and ncursesw reads that, so this changes nothing
    # on a machine whose environment names a UTF-8 locale; it is here because
    # nothing else guarantees the rest of the categories agree with it.
    locale.setlocale(locale.LC_ALL, "")
    # ncurses prefers `LINES` and `COLUMNS` over the terminal's own size, so a
    # stale pair drew the whole interface into a corner and left the rest of
    # the screen holding whatever was there before. Measured 2026-08-21: a
    # 40x100 terminal reads as 24x40 with them set.
    os.environ.pop("LINES", None)
    os.environ.pop("COLUMNS", None)
    try:
        # The kernel shares this screen on a serial console, and a message
        # lands in the middle of a panel: held for the walk, restored after.
        with kernel_messages_held():
            finished = curses.wrapper(walk)
    except curses.error as error:
        raise errors.PreflightFailed(
            f"the menu needs a terminal and this is not one ({error}); pass --config FILE"
        ) from error
    if finished.saved:
        print(f"wrote {finished.saved}")
    if finished.published:
        show_the_address(finished.published)
    return finished.config


def _region(country: str) -> MirrorRegion:
    """Which mirror list to offer, from where the packets come out.

    Not from the interface language: a Taiwanese or Singaporean machine reading
    Chinese is not behind the Great Firewall, and one in China reading English
    is. An unread country takes the global list, which reaches everywhere.
    """
    return MirrorRegion.CN if country == CN else MirrorRegion.GLOBAL


def _blank(
    disk: str,
    cores: int,
    cpu_flags: tuple[str, ...],
    supports_v3: bool = False,
    country: str = "",
    firmware: Firmware = Firmware.UEFI,
) -> InstallConfig:
    """What the menu starts from.

    MAKEOPTS, CPU_FLAGS_X86 and the binary host's subarchitecture are filled in
    from this machine: all three are right for almost every install, and
    leaving them empty means the operator has to know their own instruction set
    to get an ordinary build. The mirror is the official one rather than
    nothing, so the row that blocks the install starts answered.
    """
    # The firmware this machine booted through, not the default: the row said
    # `uefi - detected` on a machine that booted BIOS, and the template built
    # an esp and a GPT for a firmware that cannot read either.
    graph, root = templates.build(templates.Choice(disk=disk, firmware=firmware))
    region = _region(country)
    return InstallConfig(
        disk=DiskConfig(graph=graph, root=root),
        bootloader=BootloaderConfig(firmware=firmware),
        portage=PortageConfig(
            makeopts=f"-j{cores}",
            cpu_flags=cpu_flags,
            binhost=Binhost(subarch=official_subarch(supports_v3)),
            mirrors=MirrorConfig(region=region, site=mirrors.gentoo_sites(region)[0].key),
        ),
    )


def _require_root(arguments: argparse.Namespace) -> None:
    """Refuse before the menu rather than at the first write.

    Every path but a dry run partitions disks and stages keys under /run, and a
    menu answered as an ordinary user dies on EPERM with the answers thrown away.
    """
    if arguments.dry_run or arguments.missing_commands or os.geteuid() == 0:
        return
    raise errors.PreflightFailed("run as root")


def _save_config(config: InstallConfig, name: str) -> str:
    """Write the menu's answers where the operator started the installer.

    The working directory, not the work directory: the latter is a tmpfs on an
    install medium and the point of saving is to still have the file after the
    reboot.
    """
    where = Path(name).expanduser()
    if not where.is_absolute():
        where = Path.cwd() / where
    return report.save_config(config, where)
