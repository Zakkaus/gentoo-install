# SPDX-License-Identifier: GPL-2.0-or-later
"""Arm one boot into a memory environment on a running machine, and read it.

The other runners install onto a disk or convert a running system. This one
verifies the step before either: `--ram` and `--lowram` place a kernel where
the machine's own bootloader reads one, arm a single boot, and leave the
default entry alone. Nothing else in the suite reboots a machine into
something this installer put there, which is why `TESTED.md` has no row for
it.

A cloud image is the machine under test, the same one `convert.py` uses: it
has a bootloader, a disk layout and a distribution that is not Gentoo, so a
console that comes up Gentoo is the memory environment and nothing else.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Final

from .console import ConsoleClosed, ConsoleTimeout, SerialConsole
from .convert import IMAGES, CloudImage, install_tools, overlay, reach_root, seed
from .driver import build as build_driver, wait_for_driver
from .media import MEDIA
from .qemu import Vm, VmSpec
from .run import free_port
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

#: How long that screen is given after the medium speaks. The payload is
#: copied by a `pre-pivot` hook and started by the medium's own `local`
#: service, so it follows the login prompt rather than racing it.
PAYLOAD_PATIENCE: Final[float] = 300.0


def arm(console: SerialConsole, config: str, mode: str) -> None:
    """Run the installer in its memory mode and keep everything it printed."""
    console.run("mkdir -p /tmp/gentoo-install-results", timeout=60.0)
    console.run(
        f"{{ sh /mnt/driver/install.sh --config {config} --{mode}; echo $? "
        "> /tmp/gentoo-install-results/arm.rc; } 2>&1 "
        "| tee /tmp/gentoo-install-results/arm.txt",
        timeout=ARM_CEILING,
    )


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
    try:
        console.expect(PAYLOAD_SPEAKS, timeout=PAYLOAD_PATIENCE)
    except (ConsoleTimeout, ConsoleClosed) as error:
        return f"the environment booted without its configuration: {error}"[:600]
    return ""


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
    arguments = parser.parse_args(argv)
    chosen: CloudImage = IMAGES[arguments.image]

    workdir = confined(WORKROOT / f"{arguments.mode}-{int(time.time())}")
    workdir.mkdir(parents=True)
    print(f"work directory: {workdir}", flush=True)

    driver = build_driver(workdir / "driver.iso")
    root = overlay(chosen.image, workdir)
    cidata = seed(workdir)

    spec = VmSpec(
        medium=MEDIA["official-minimal"],
        workdir=workdir,
        firmware=chosen.firmware,
        memory=arguments.memory,
        cpus=arguments.cpus,
        ssh_port=free_port(),
        driver_iso=driver,
        targets=(root,),
        disks=(cidata,),
        boot_installed=True,
    )
    started = time.monotonic()
    refused = ""
    with Vm(spec) as vm:
        console = SerialConsole.connect(vm.serial_socket, vm.serial_log)
        reach_root(console, chosen)
        print(f"[{time.monotonic() - started:5.1f}s] root shell on serial", flush=True)
        before = read_the_default_entry(console)
        # Before anything asks the CD for a file: the guest's shell answers
        # before its ATAPI devices are enumerated, and `sh` exits 2 for a
        # script it cannot open, which reads as the installer refusing.
        wait_for_driver(console)
        install_tools(console, chosen, arguments.config)
        arm(console, arguments.config, arguments.mode)
        code = console.expect_command(
            "cat /tmp/gentoo-install-results/arm.rc", timeout=60.0
        )
        after = read_the_default_entry(console)
        print(f"[{time.monotonic() - started:5.1f}s] arming exited {code!r}", flush=True)
        if b"0" not in code:
            refused = f"the arming exited {code!r}"
        elif _default_changed(before, after):
            refused = (
                "the default boot entry changed, which this mode promises not to do: "
                f"{before!r} then {after!r}"
            )[:600]
        else:
            console.run("reboot", timeout=60.0)
            refused = came_up(console, arguments.mode)
    if refused:
        print(f"FAIL {refused}", flush=True)
    else:
        print(
            f"[{time.monotonic() - started:5.1f}s] the machine rebooted into the "
            "memory environment and it holds the delivered configuration",
            flush=True,
        )
    if not arguments.keep:
        root.unlink(missing_ok=True)
    return 1 if refused else 0


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
