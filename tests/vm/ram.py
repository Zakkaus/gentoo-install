# SPDX-License-Identifier: GPL-2.0-or-later
"""Install through a memory environment, then prove an armed failure falls back.

The cloud image under test has its own bootloader, disk layout and non-Gentoo
system. The runner first boots the delivered `--ram` or `--lowram` environment,
answers its first screen with `install`, and boots the resulting disk through
the shared installed-state contract. A separate fresh image arms one boot,
removes the entry's initramfs, and proves the original system boots twice.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

from gentoo_install.exec.config import load
from gentoo_install.model.config import InstallConfig
from gentoo_install.plan.netboot import ENTRY_LABEL

from .console import ConsoleClosed, ConsoleTimeout, SerialConsole
from .convert import IMAGES, CloudImage, install_tools, overlay, reach_root, seed
from .driver import REPOSITORY, build as build_driver, wait_for_driver
from .media import MEDIA, MediaError
from .qemu import Vm, VmSpec
from .results import ResultError, create_disk
from .run import check_installed, free_port, power_off, report, unlock_and_login
from .workdir import confined

WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/ram"

#: How long the arming is given. It downloads an image of about a gigabyte
#: from the publisher, which is the whole of the wait; the rest is a `tar` and
#: three small writes.
ARM_CEILING: Final[float] = 3600.0

#: How long the memory environment is given to come up after the reboot. It
#: reads the image into RAM before it starts, which the console reports.
MEMORY_BOOT_PATIENCE: Final[float] = 900.0

#: What the CJK medium prints once it is running. `livecd login:` and not the
#: kernel banner: the banner appears for the machine's own kernel too, and
#: this has to tell one boot from the other.
CJK_SPEAKS: Final[str] = r"livecd login:|Gentoo Linux Minimal Installation CD"

#: What the Alpine netboot medium prints instead.
ALPINE_SPEAKS: Final[str] = r"Welcome to Alpine Linux|localhost login:"

#: What the delivered payload's own first screen prints. It is the proof the
#: configuration arrived, not merely that a live medium booted.
PAYLOAD_SPEAKS: Final[str] = r"install or shell>"

#: How long the login prompt is given after the medium's banner.
LOGIN_PATIENCE: Final[float] = 300.0

#: How long that screen is given after the medium speaks. The payload is
#: copied by a `pre-pivot` hook and started by the medium's own `local`
#: service, so it follows the login prompt rather than racing it.
PAYLOAD_PATIENCE: Final[float] = 300.0

#: A pre-arming file that only the cloud system owns.
FALLBACK_MARKER: Final[str] = "gentoo-install-ram-fallback-system"
FALLBACK_MARKER_PATH: Final[str] = f"/var/lib/{FALLBACK_MARKER}"
#: The normal completion record emitted only after every install operation ends.
INSTALL_FINISHED: Final[str] = r"installed [0-9]+ operations into /mnt/gentoo;"
#: What `cli.py` prints instead when an operation raised.
INSTALL_STOPPED: Final[str] = r"the install stopped:"
#: Cluster guests kept making progress after three hours, so use its eight-hour
#: ceiling; its twenty-minute quiet window rejects a stopped serial console.
INSTALL_CEILING: Final[float] = 8 * 3600.0
INSTALL_IDLE: Final[float] = 20 * 60.0
POST_INSTALL_PATIENCE: Final[float] = 600.0
#: A deleted initramfs prevents this entry from reaching its first screen.
BROKEN_BOOT_PATIENCE: Final[float] = 180.0


def arm(console: SerialConsole, config: str, mode: str) -> None:
    """Run the installer in its memory mode and keep everything it printed."""
    console.run("mkdir -p /tmp/gentoo-install-results", timeout=60.0)
    console.run(
        f"sh /mnt/driver/install.sh --config {config} --{mode} 2>&1 "
        "| tee /tmp/gentoo-install-results/arm.txt",
        timeout=ARM_CEILING,
    )


def arm_bypass(console: SerialConsole, config: str, mode: str) -> bytes:
    """Replace the default entry, and require it to have moved.

    The one-shot's promise is the opposite of this one: `--bypass` exists for
    firmware that drops a one-shot, and it is the only path where an
    environment that does not come up leaves a machine that does not boot.
    """
    before = read_the_default_entry(console)
    console.run("mkdir -p /tmp/gentoo-install-results", timeout=60.0)
    console.run(
        f"sh /mnt/driver/install.sh --config {config} --{mode} --bypass 2>&1 "
        "| tee /tmp/gentoo-install-results/arm.txt",
        timeout=ARM_CEILING,
    )
    after = read_the_default_entry(console)
    if not _default_changed(before, after):
        raise RuntimeError(
            "the default boot entry did not move, which is the whole of what "
            f"--bypass does: {before!r} then {after!r}"
        )
    return after


def arm_and_confirm(console: SerialConsole, config: str, mode: str) -> bytes:
    """Arm one boot and reject a missing one-shot or a moved default entry."""
    before = read_the_default_entry(console)
    arm(console, config, mode)
    after = read_the_default_entry(console)
    if not _one_shot_is_armed(after):
        raise RuntimeError(f"GRUB recorded no one-shot entry: {after!r}")
    if _default_changed(before, after):
        raise RuntimeError(
            "the default boot entry changed, which this mode promises not to do: "
            f"{before!r} then {after!r}"
        )
    return after


def _one_shot_is_armed(said: bytes) -> bool:
    """Whether GRUB recorded the entry that its next boot will select."""
    return any(
        line.strip() == f"next_entry={ENTRY_LABEL}"
        for line in said.decode("utf-8", "replace").splitlines()
    )


def break_armed_environment(console: SerialConsole) -> None:
    """Remove the entry's initramfs without changing its one-shot state.

    The entry loads this exact file. Removing it after arming makes that selected
    entry unable to start, while leaving GRUB's `next_entry` for the reboot.
    """
    said = console.expect_output(
        "find /boot /efi -type f -path '*/gentoo-install-ram/initramfs' -print "
        "2>/dev/null",
        timeout=60.0,
    )
    paths = tuple(line for line in said.decode("utf-8", "replace").splitlines() if line)
    if len(paths) != 1:
        raise RuntimeError(f"the armed entry has {len(paths)} initramfs files: {paths!r}")
    initramfs = shlex.quote(paths[0])
    console.run(f"rm -- {initramfs}", timeout=60.0)
    broken = console.expect_output(
        f"if test ! -e {initramfs}; then printf INITRAMFS-BROKEN; "
        "else printf INITRAMFS-STILL-PRESENT; fi",
        timeout=60.0,
    ).strip()
    if broken != b"INITRAMFS-BROKEN":
        raise RuntimeError(f"the armed initramfs was not removed: {broken!r}")


def memory_did_not_start(console: SerialConsole) -> None:
    """Require the entry with its missing initramfs to miss the delivered screen."""
    try:
        console.expect(PAYLOAD_SPEAKS, timeout=BROKEN_BOOT_PATIENCE)
    except (ConsoleClosed, ConsoleTimeout):
        return
    raise RuntimeError("the entry with its initramfs removed reached the delivered screen")


def mark_own_system(console: SerialConsole) -> None:
    """Write a marker on the cloud disk before its one-shot boot is armed."""
    console.run(
        f"printf '%s\\n' {FALLBACK_MARKER} > {FALLBACK_MARKER_PATH}",
        timeout=60.0,
    )


def require_own_system(console: SerialConsole, chosen: CloudImage) -> None:
    """Read the cloud marker and its own os-release identity between markers."""
    said = console.expect_output(
        f"cat {FALLBACK_MARKER_PATH}; . /etc/os-release; printf 'ID=%s\\n' \"$ID\"",
        timeout=60.0,
    )
    wanted = (FALLBACK_MARKER.encode(), f"ID={chosen.name}".encode())
    missing = tuple(one.decode() for one in wanted if one not in said)
    if missing:
        raise RuntimeError(
            "the machine did not return to its own system: missing "
            f"{', '.join(missing)} from {said!r}"
        )


def install_from_memory(console: SerialConsole) -> None:
    """Choose installation once, then wait for its recorded completion.

    The installer's own refusal is matched beside the completion: it returns
    to the environment's shell and prints nothing more, so waiting only for
    the completion spends the whole idle window on a run that already ended.
    """
    console.send("install")
    said = console.expect(
        rf"{INSTALL_FINISHED}|{INSTALL_STOPPED}",
        timeout=INSTALL_CEILING,
        idle=INSTALL_IDLE,
    )
    if re.search(INSTALL_STOPPED.encode(), said):
        console.expect(r"# ", timeout=POST_INSTALL_PATIENCE)
        raise RuntimeError(
            f"the memory environment did not install: "
            f"{said.decode('utf-8', 'replace')[-300:]}; {_what_the_disk_holds(console)}"
        )
    console.expect(r"# ", timeout=POST_INSTALL_PATIENCE)


def _what_the_disk_holds(console: SerialConsole) -> str:
    """What the machine says about the disk the install stopped on.

    Read between the markers rather than after the command, because the shell
    echoes the line it was given and a reader of that echo learns nothing.
    """
    answers = []
    for command in (
        "cat /proc/partitions",
        "grep -c . /proc/mounts; grep vd /proc/mounts",
        "ls -l /dev/vd*",
        "for one in /proc/[0-9]*/comm; do cat \"$one\"; done | sort -u | tr '\\n' ' '",
        "command -v mdev; echo mdev=$?",
        # The installer's own log, which names every command it ran: whether
        # `wait_for` fell back to `mdev -s`, and what stood between the node
        # appearing and the formatter finding nothing, are in it and in no
        # file either runner keeps.
        "tail -25 /run/gentoo-install/install.log",
        "stat -c '%n %F' /dev/vdc /dev/vdc1 /dev/vdc2 2>&1",
        "dmesg | tail -20",
    ):
        try:
            said = console.expect_output(command, timeout=60.0)
        except (ConsoleClosed, ConsoleTimeout) as error:
            said = f"unanswered: {error}".encode()
        answers.append(f"{command}: {said.decode('utf-8', 'replace').strip()}")
    return " | ".join(answers)


def read_the_default_entry(console: SerialConsole) -> bytes:
    """What the machine boots by default, before and after the arming.

    The promise of this path is that it does not change, so it is read rather
    than assumed: an arming that quietly became the default is the failure
    `--bypass` exists to make explicit.
    """
    return console.expect_output(
        "grub-editenv list 2>/dev/null; efibootmgr 2>/dev/null | grep -i bootorder "
        "|| echo NO-BOOTORDER",
        timeout=60.0,
    )


def came_up(console: SerialConsole, mode: str) -> str:
    """Whether the memory environment answered. Empty when it did.

    The medium and the payload are two separate claims and both are checked:
    a live medium that booted without the configuration is an environment the
    operator has to drive by hand, which is not what was asked for.
    """
    speaks = CJK_SPEAKS if mode == "ram" else ALPINE_SPEAKS
    try:
        console.expect(speaks, timeout=MEMORY_BOOT_PATIENCE)
    except (ConsoleTimeout, ConsoleClosed) as error:
        return f"the memory environment never spoke: {error}"[:600]
    if mode == "lowram":
        # Alpine's netboot console asks for a login and the CJK medium logs
        # root in by itself. The first screen is in root's profile either way,
        # so this console has to log in for the same reason the operator's ssh
        # session does. Root has no password in a netboot Alpine.
        try:
            console.expect(r"login:", timeout=LOGIN_PATIENCE)
            console.send("root")
        except (ConsoleTimeout, ConsoleClosed) as error:
            return f"the environment asked for no login: {error}"[:600]
    try:
        console.expect(PAYLOAD_SPEAKS, timeout=PAYLOAD_PATIENCE)
    except (ConsoleTimeout, ConsoleClosed) as error:
        return f"the environment booted without its configuration: {error}"[:600]
    return ""


def leave_the_first_screen(console: SerialConsole) -> None:
    """Answer the delivered screen so the console is at a shell again.

    `run()` sends a marked command, and typed at `install or shell>` the whole
    line is read as the answer: the screen printed `nothing was changed` and
    the marker never came back, so the second reboot of a `--bypass` run
    failed at `never matched 'MARK_14_DONE'`.
    """
    console.send("shell")
    console.expect(r"# ", timeout=POST_INSTALL_PATIENCE)


def run_install(
    chosen: CloudImage,
    config: str,
    driver: Path,
    workdir: Path,
    *,
    mode: str,
    memory: str,
    cpus: int,
    keep: bool,
) -> None:
    """Install from the delivered screen and boot the target through shared checks."""
    configuration = REPOSITORY / "tests" / config
    installation: InstallConfig = load(configuration)
    workdir.mkdir(parents=True, exist_ok=True)
    root = overlay(chosen.image, workdir)
    cidata = seed(workdir)
    result = create_disk(workdir / "result.img")
    verified = False
    started = time.monotonic()
    try:
        first = VmSpec(
            medium=MEDIA["official-minimal"],
            workdir=workdir,
            firmware=chosen.firmware,
            memory=memory,
            cpus=cpus,
            ssh_port=free_port(),
            driver_iso=driver,
            targets=(root,),
            disks=(cidata,),
            boot_installed=True,
        )
        with Vm(first) as vm:
            console = SerialConsole.connect(vm.serial_socket, vm.serial_log)
            reach_root(console, chosen)
            print(f"[{time.monotonic() - started:5.1f}s] root shell on serial", flush=True)
            wait_for_driver(console)
            install_tools(console, chosen, config, mode)
            arm_and_confirm(console, config, mode)
            console.run("reboot", timeout=60.0)
            refused = came_up(console, mode)
            if refused:
                raise RuntimeError(refused)
            install_from_memory(console)
            power_off(console, vm)

        installed = VmSpec(
            medium=MEDIA["official-minimal"],
            workdir=workdir,
            firmware=chosen.firmware,
            memory=memory,
            cpus=cpus,
            ssh_port=free_port(),
            disks=(result,),
            targets=(root,),
            boot_installed=True,
        )
        with Vm(installed) as vm:
            console = SerialConsole.connect(vm.serial_socket, workdir / "installed.log")
            method = unlock_and_login(console, installation)
            print(
                f"[{time.monotonic() - started:5.1f}s] logged into the installed system "
                f"({method})",
                flush=True,
            )
            check_installed(console, installation)
            power_off(console, vm)
        if report(result, keep=True, assertions=configuration) != 0:
            raise RuntimeError("the installed system failed its shared state checks")
        verified = True
        print(
            f"[{time.monotonic() - started:5.1f}s] memory install booted its disk "
            "and passed the shared installed-state checks",
            flush=True,
        )
    finally:
        if verified and not keep:
            root.unlink(missing_ok=True)
            result.unlink(missing_ok=True)


def run_fallback(
    chosen: CloudImage,
    config: str,
    driver: Path,
    workdir: Path,
    *,
    mode: str,
    memory: str,
    cpus: int,
    keep: bool,
) -> None:
    """Break one selected entry, then boot the original disk twice."""
    workdir.mkdir(parents=True, exist_ok=True)
    root = overlay(chosen.image, workdir)
    cidata = seed(workdir)
    verified = False
    started = time.monotonic()
    try:
        armed = VmSpec(
            medium=MEDIA["official-minimal"],
            workdir=workdir,
            firmware=chosen.firmware,
            memory=memory,
            cpus=cpus,
            ssh_port=free_port(),
            driver_iso=driver,
            targets=(root,),
            disks=(cidata,),
            boot_installed=True,
        )
        with Vm(armed) as vm:
            console = SerialConsole.connect(vm.serial_socket, vm.serial_log)
            reach_root(console, chosen)
            mark_own_system(console)
            wait_for_driver(console)
            install_tools(console, chosen, config, mode)
            arm_and_confirm(console, config, mode)
            break_armed_environment(console)
            console.run("reboot", timeout=60.0)
            memory_did_not_start(console)

        returned = VmSpec(
            medium=MEDIA["official-minimal"],
            workdir=workdir,
            firmware=chosen.firmware,
            memory=memory,
            cpus=cpus,
            ssh_port=free_port(),
            targets=(root,),
            boot_installed=True,
        )
        with Vm(returned) as vm:
            console = SerialConsole.connect(vm.serial_socket, workdir / "fallback.log")
            reach_root(console, chosen)
            require_own_system(console, chosen)
            if _one_shot_is_armed(read_the_default_entry(console)):
                raise RuntimeError("the failed entry was still armed after its boot")
            print(
                f"[{time.monotonic() - started:5.1f}s] the failed one-shot returned "
                "to the cloud system",
                flush=True,
            )
            console.run("reboot", timeout=60.0)
            reach_root(console, chosen)
            require_own_system(console, chosen)
            if _one_shot_is_armed(read_the_default_entry(console)):
                raise RuntimeError("the second boot unexpectedly re-armed the failed entry")
            power_off(console, vm)
        verified = True
        print(
            f"[{time.monotonic() - started:5.1f}s] the second reboot still reached "
            "the cloud system",
            flush=True,
        )
    finally:
        if verified and not keep:
            root.unlink(missing_ok=True)


#: What the closing line says, per half. A run of one half claimed both: a
#: `--part install` run ended with `proved a broken one-shot returns twice`
#: and nothing had armed a broken one.
RAN: Final[dict[str, str]] = {
    "install": "memory mode installed Gentoo from the environment it delivered",
    "fallback": "memory mode proved a broken one-shot returns to its own system twice",
    "bypass": (
        "memory mode replaced the default entry and came up in the environment twice"
    ),
    "both": (
        "memory mode installed Gentoo and proved a broken one-shot returns twice"
    ),
}


def _what_ran(part: str) -> str:
    return RAN[part]


def run_bypass(
    chosen: CloudImage,
    config: str,
    driver: Path,
    workdir: Path,
    *,
    mode: str,
    memory: str,
    cpus: int,
    keep: bool,
) -> None:
    """Replace the default entry, and require both boots to reach the environment.

    One boot is what `--ram` alone proves. The second is what separates this
    path from it: a replaced default entry comes up in the environment again,
    which is why this is the one path an environment that does not come up
    leaves unbootable.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    root = overlay(chosen.image, workdir)
    cidata = seed(workdir)
    verified = False
    started = time.monotonic()
    try:
        spec = VmSpec(
            medium=MEDIA["official-minimal"],
            workdir=workdir,
            firmware=chosen.firmware,
            memory=memory,
            cpus=cpus,
            ssh_port=free_port(),
            driver_iso=driver,
            targets=(root,),
            disks=(cidata,),
            boot_installed=True,
        )
        with Vm(spec) as vm:
            console = SerialConsole.connect(vm.serial_socket, vm.serial_log)
            reach_root(console, chosen)
            wait_for_driver(console)
            install_tools(console, chosen, config, mode)
            arm_bypass(console, config, mode)
            for boot in ("first", "second"):
                console.run("reboot", timeout=60.0)
                refused = came_up(console, mode)
                if refused:
                    raise RuntimeError(f"the {boot} boot after --bypass: {refused}")
                # The environment is at its own first screen, and the next
                # command has to reach a shell rather than that prompt.
                leave_the_first_screen(console)
                print(
                    f"[{time.monotonic() - started:5.1f}s] the {boot} boot came up "
                    "in the memory environment",
                    flush=True,
                )
        verified = True
    finally:
        if verified and not keep:
            root.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", choices=sorted(IMAGES), default="debian")
    parser.add_argument("--mode", choices=("ram", "lowram"), default="lowram")
    parser.add_argument(
        "--config",
        default="fixtures/vm-ram.toml",
        help="what the memory environment is asked to install",
    )
    parser.add_argument("--memory", default="4G")
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--keep", action="store_true")
    # The two halves are an hour apart, and the fallback is the one a change to
    # the boot entry has to be measured against.
    parser.add_argument(
        "--part", choices=("both", "install", "fallback", "bypass"), default="both"
    )
    arguments = parser.parse_args(argv)
    chosen: CloudImage = IMAGES[arguments.image]

    workdir = confined(WORKROOT / f"{arguments.mode}-{int(time.time())}")
    workdir.mkdir(parents=True)
    print(f"work directory: {workdir}", flush=True)
    try:
        driver = build_driver(workdir / "driver.iso")
        if arguments.part in ("both", "install"):
            run_install(
                chosen,
                arguments.config,
                driver,
                workdir / "install",
                mode=arguments.mode,
                memory=arguments.memory,
                cpus=arguments.cpus,
                keep=arguments.keep,
            )
        if arguments.part == "bypass":
            run_bypass(
                chosen,
                arguments.config,
                driver,
                workdir / "bypass",
                mode=arguments.mode,
                memory=arguments.memory,
                cpus=arguments.cpus,
                keep=arguments.keep,
            )
        if arguments.part in ("both", "fallback"):
            run_fallback(
                chosen,
                arguments.config,
                driver,
                workdir / "fallback",
                mode=arguments.mode,
                memory=arguments.memory,
                cpus=arguments.cpus,
                keep=arguments.keep,
            )
    except (
        ConsoleClosed,
        ConsoleTimeout,
        MediaError,
        OSError,
        ResultError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"FAIL {error}", file=sys.stderr, flush=True)
        return 1
    print(_what_ran(arguments.part), flush=True)
    return 0


def _default_changed(before: bytes, after: bytes) -> bool:
    """Whether the default entry moved, ignoring the one-shot beside it.

    `grub-editenv list` prints `next_entry` once armed, which is the whole
    point; `saved_entry` and the firmware's `BootOrder` are what must not
    move.
    """
    return _kept(before) != _kept(after)


def _kept(said: bytes) -> tuple[str, ...]:
    text = said.decode("utf-8", "replace")
    return tuple(
        sorted(
            line.strip()
            for line in text.splitlines()
            if re.match(r"\s*(saved_entry=|BootOrder:)", line)
        )
    )


if __name__ == "__main__":
    sys.exit(main())
