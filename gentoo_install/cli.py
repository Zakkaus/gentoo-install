"""Argument parsing and the one place an exception becomes an exit code.

The table is in docs/design.md. Codes 3 and 4 stay apart on purpose: 3 says the
data could not be trusted, 4 says an operation did not finish.
"""

from __future__ import annotations

import argparse
import curses
import locale
import os
import shutil
import sys
import termios
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Final, Sequence

from . import errors
from .errors import GentooInstallError
from .data import load_catalog
from .exec import fetch, preflight, report
from .exec.apply import Machine, already_degraded, apply, completed
from .exec.probe import Probe, with_probed_facts
from .exec.runner import Runner
from .model.size import Size
from .tui import app, screens
from .tui.curses_screen import CursesScreen, too_small
from .i18n import Catalog, tag_for
from .model import mirrors, qr, templates
from .model.config import (
    BootloaderConfig,
    Binhost,
    DiskConfig,
    Firmware,
    InstallConfig,
    MirrorConfig,
    MirrorRegion,
    PortageConfig,
)
from .exec.config import load
from .plan.build import DEFAULT_MIRROR, build, stage3_mirror
from .plan.operations import Context, Operation, Stage
from .plan.portage import variant_of
from .plan.render import render, summarise

#: The country whose mirrors are the ones worth offering. Every other answer,
#: and no answer at all, takes the global list.
CN: Final[str] = "CN"

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
        "--no-shell",
        action="store_true",
        help="unmount and exit without offering a root shell in the new system, which is "
        "what an unattended run wants",
    )
    parsed.add_argument(
        "--skip-preflight",
        action="store_true",
        help="install without checking the machine first, for a harness that knows what it booted",
    )
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    state = RunState(disk_was_written=bool(arguments.resume))
    try:
        _require_root(arguments)
        if _needs_network(arguments):
            # Before any reachability check: an unset clock makes every HTTPS
            # request fail, and the message would name the network instead.
            _check_the_clock()
            if arguments.config is None:
                # The menu reads every version from the package site.
                _require_network()
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
            chosen = _from_menu(arguments)
            if chosen is None:
                print("cancelled", file=sys.stderr)
                return EXIT_ABORTED
            config = chosen
        else:
            config = load(arguments.config)
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
        if _needs_network(arguments):
            _require_mirror(config, arguments.mirror)
        if not arguments.dry_run:
            # Before the plan is derived, because `build` validates: a reused
            # esp on an array carries its metadata version only after the
            # machine has been asked for it. Not on a dry run, which is meant
            # to answer on a machine that has none of the hardware.
            config = with_probed_facts(
                config, Probe(runner=Runner(log=lambda line: None), work=arguments.work)
            )
        operations = build(config, load_catalog(), mirror=arguments.mirror)
        if arguments.dry_run:
            print(render(operations), end="")
            print(summarise(operations))
            return EXIT_OK
        return install(config, operations, arguments, state)
    except errors.DeviceNotFound as error:
        _print_machine_state(state)
        print(f"device: {error}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except errors.ConfigError as error:
        _print_machine_state(state)
        print(f"configuration: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except errors.PreflightFailed as error:
        _print_machine_state(state)
        print(f"preflight: {error}", file=sys.stderr)
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
    """Tell the operator whether a failed run changed the selected disk."""
    if state.disk_was_written:
        print("the selected disk has been written to and may not boot", file=sys.stderr)
    else:
        print("nothing was written to the selected disk", file=sys.stderr)


def install(
    config: InstallConfig,
    operations: tuple[Operation, ...],
    arguments: argparse.Namespace,
    state: RunState,
) -> int:
    """Check the machine, then perform every operation in order."""
    work: Path = arguments.work
    with report.recording(work, arguments.target) as active_report:
        record = active_report.record
        journal = active_report.journal
        runner = Runner(log=record, journal=journal)
        probe = Probe(runner=runner, work=work)
        probe.load()
        if not arguments.skip_preflight:
            preflight_report = preflight.check(config, probe, str(arguments.target))
            for warning in preflight_report.warnings:
                record(f"warning: {warning}")
            preflight_report.raise_if_fatal()
        machine = Machine(
            config=config, runner=runner, probe=probe, work=work, mountpoint=arguments.target
        )
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
        # Reaching the body means the partition stage is about to run. A
        # command that fails can still have changed the disk before exiting.
        state.disk_was_written = state.disk_was_written or any(
            operation.stage is Stage.PARTITION for operation in body
        )
        try:
            apply(body, machine, finished)
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
            )
            _offer_a_shell(arguments, machine, record, stopped)
        except BaseException as error:
            failure = _first_failure(failure, error, record)
        # Before the closing stage: that stage unmounts the target, so a later
        # copy lands on the install medium's tmpfs and vanishes at reboot.
        try:
            report.keep_log(work, arguments.target, record)
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
        counted = journal.counts()
        record(
            f"installed {len(operations)} operations into {arguments.target}; "
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


def _unattended(arguments: argparse.Namespace) -> bool:
    """Whether there is nobody at the keyboard to answer a question."""
    return bool(arguments.no_shell) or not sys.stdin.isatty()


def _draws_wide_characters() -> bool:
    """Whether this terminal's encoding can carry a CJK glyph.

    `LC_CTYPE`, not `LANG`: the codeset is what ncurses reads, and an operator
    who exported `LC_ALL=C` over a Chinese `LANG` has a terminal that cannot
    draw one whatever the environment says it prefers.
    """
    return locale.nl_langinfo(locale.CODESET).upper().replace("-", "") == "UTF8"


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
) -> None:
    """A root shell in the target before it is unmounted.

    Offered after a failure as well as after a success: the operator is the one
    who can tell whether the machine is fixable, and once the target is
    unmounted they would have to mount the whole layout again by hand.
    """
    if _unattended(arguments):
        return
    if not _asked(f"enter a root shell in {arguments.target} before unmounting?"):
        return
    record(f"a root shell was opened in {arguments.target}")
    machine.runner.run(["chroot", str(arguments.target), "/bin/bash", "--login"], check=False)
    record("the shell exited; unmounting")


def _probe_for(arguments: argparse.Namespace) -> Probe:
    """A probe that says nothing: `--missing-commands` writes one command per
    line and the launcher reads every line of it."""
    return Probe(runner=Runner(log=lambda line: None), work=arguments.work)


#: The overlay that carries the patched kernel. Its packages are on no package
#: site, so their versions come from the overlay's own listing.
_OVERLAY_PACKAGES: Final[tuple[str, ...]] = (
    "sys-kernel/gentoo-cjk-kernel-bin",
    "sys-kernel/gentoo-cjk-kernel",
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
    runner.run(["hwclock", "--hctosys", "--utc"], check=False)


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
    has_ipv4, has_ipv6 = probe.address_families()
    # Checked before the first screen: the menu hashes a password with
    # `openssl`, and finding it absent at that point throws away every answer.
    lacking = report.absent(preflight.MENU_ONLY)
    if lacking:
        raise errors.PreflightFailed(f"the menu needs {', '.join(sorted(lacking))}")
    context = screens.Context(
        translate=Catalog(tag_for(override=arguments.lang)),
        ipv4=has_ipv4,
        ipv6=has_ipv6,
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
        load_config=lambda name: load(Path(name)),
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
        display = CursesScreen(window)
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
            return app.run(display, screens.with_language(start, "en"), context)
        # Asked before the menu: the environment says which language the
        # operator reads, not whether this terminal can draw it.
        if not arguments.lang:
            context.translate = Catalog(screens.language_screen(display, context))
            context.tag = context.translate.tag
            chosen = screens.with_language(start, context.tag)
        else:
            chosen = screens.with_language(start, context.translate.tag)
        # Before the menu and after the language: the question has to be
        # readable, and loading a file replaces every answer behind it.
        offered = screens.saved_config_screen(display, chosen, context)
        return app.run(display, offered.unwrap() if offered.chosen else chosen, context)

    # Before `initscr`, which `wrapper` calls. Python sets `LC_CTYPE` from the
    # environment at startup and ncursesw reads that, so this changes nothing
    # on a machine whose environment names a UTF-8 locale; it is here because
    # nothing else guarantees the rest of the categories agree with it.
    locale.setlocale(locale.LC_ALL, "")
    try:
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
            binhost=Binhost(subarch="x86-64-v3" if supports_v3 else "x86-64"),
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
