# SPDX-License-Identifier: GPL-2.0-or-later
"""Convert another distribution's cloud image in place, on this machine.

The two conversion rows in `TESTED.md` were produced by commands nobody kept,
so neither could be reproduced. This is the committed way to do it: pick an
image, boot it, hand it the driver CD, and read the machine afterwards.

The pristine download is never written to. Each run gets a qcow2 overlay over
it, larger than the original so that cloud-init grows the root filesystem on
first boot and the conversion has somewhere to unpack a stage3.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from gentoo_install.exec.config import load
from gentoo_install.model.config import InstallConfig

from .console import PASSWORD_PROMPT, SerialConsole
from .driver import FIND_DRIVER, REPOSITORY, build as build_driver
from .media import MediaError
from .installed import InstalledCheck, checks
from .media import MEDIA
from .qemu import Firmware, Vm, VmSpec
from .run import free_port
from .workdir import confined

#: Where `pull.sh` keeps the images. A run reads them and writes none of them.
CLOUD: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/cloud"
WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/converts"

#: What cloud-init is told to set, so the harness can log in over serial. The
#: images ship no password at all and no key the harness holds.
ROOT_PASSWORD: Final[str] = "install"
#: The payload is also part of the path: `expect_command` would otherwise match
#: the shell's echo of `cat`, so the marker check must use `expect_output`.
HOME_MARKER: Final[str] = "gentoo-install-home-survives-conversion-4f9d6e2a"
HOME_MARKER_PATH: Final[Path] = Path("/home") / f".{HOME_MARKER}"
HOME_MARKER_CHECK: Final[InstalledCheck] = InstalledCheck(
    "home marker",
    f"cat {HOME_MARKER_PATH}",
    re.escape(HOME_MARKER),
)

#: Bigger than every image here, so cloud-init's `growpart` and `resizefs`
#: run on first boot. A 5 GiB Fedora root cannot hold a stage3 and a kernel.
OVERLAY_SIZE: Final[str] = "24G"

#: How long the guest is given to finish cloud-init, which grows the
#: filesystem and sets the password before a login is possible.
CLOUD_INIT_PATIENCE: Final[float] = 600.0

#: How long the converted machine is given to reach a login prompt. It boots
#: the kernel the conversion installed, through the bootloader the conversion
#: wrote, on the disk the old distribution was on.
BOOT_PATIENCE: Final[float] = 900.0

#: An in-place conversion compiles nothing by default but does emerge a
#: dist-kernel, and a cloud image starts from an empty Portage cache.
CONVERT_CEILING: Final[float] = 7200.0
CONVERT_IDLE: Final[float] = 1800.0


@dataclass(frozen=True)
class CloudImage:
    """One distribution's cloud image and what it needs before converting."""

    name: str
    filename: str
    #: What the image's own package manager is called, for the line
    #: `bootstrap.sh --missing-commands` prints. Read from the image rather
    #: than guessed: the launcher already knows every one of these.
    installer: str
    #: What `--missing-commands` asks for is turned into this command.
    install: str
    #: The firmware the image can boot. `genericcloud` and Fedora Cloud Base
    #: carry an ESP; the Alpine BIOS variant does not.
    firmware: Firmware
    #: A command whose package is not named after it. Every other name goes to
    #: the package manager as it was printed.
    packages: dict[str, str] = field(default_factory=dict)
    #: What the harness itself reads the machine with, which the installer
    #: never asks for. `efibootmgr` is the whole of it: `read_the_boot_order`
    #: printed `NO-EFIBOOTMGR` on every conversion, and that reading is the
    #: only place a Fedora conversion's `Boot0001 "Fedora"` was ever visible.
    tools: tuple[str, ...] = ("efibootmgr",)

    @property
    def image(self) -> Path:
        return CLOUD / self.filename


IMAGES: Final[dict[str, CloudImage]] = {
    one.name: one
    for one in (
        CloudImage(
            "fedora",
            "Fedora-Cloud-Base-Generic-41-1.4.x86_64.qcow2",
            "dnf",
            "dnf install -y --setopt=install_weak_deps=False",
            Firmware.UEFI,
            {"mkfs.vfat": "dosfstools", "sgdisk": "gdisk", "mkfs.btrfs": "btrfs-progs",
             "pvcreate": "lvm2", "partprobe": "parted"},
        ),
        CloudImage(
            "debian",
            "debian-12-genericcloud-amd64.qcow2",
            "apt-get",
            "apt-get update && apt-get install -y --no-install-recommends",
            Firmware.UEFI,
            {"mkfs.vfat": "dosfstools", "sgdisk": "gdisk", "mkfs.btrfs": "btrfs-progs",
             "mkfs.xfs": "xfsprogs", "pvcreate": "lvm2", "partprobe": "parted",
             "zpool": "zfsutils-linux"},
        ),
        CloudImage(
            "arch",
            "Arch-Linux-x86_64-cloudimg.qcow2",
            "pacman",
            "pacman -Sy --noconfirm",
            Firmware.UEFI,
            {"mkfs.vfat": "dosfstools", "sgdisk": "gptfdisk", "mkfs.btrfs": "btrfs-progs",
             "mkfs.xfs": "xfsprogs", "pvcreate": "lvm2", "partprobe": "parted",
             "zpool": "zfs-utils"},
        ),
    )
}


def seed(workdir: Path) -> Path:
    """A NoCloud seed ISO setting the root password and nothing else.

    cloud-init finds it by the `cidata` label on any attached block device, so
    it is handed over as a plain disk rather than a second CD: the driver CD
    is already the only one and `FIND_DRIVER` looks for it there.
    """
    if shutil.which("xorriso") is None:
        raise RuntimeError("xorriso is not installed, so the seed cannot be built")
    source = workdir / "seed"
    source.mkdir(parents=True, exist_ok=True)
    (source / "user-data").write_text(
        "#cloud-config\n"
        "disable_root: false\n"
        "ssh_pwauth: true\n"
        "growpart:\n"
        "  mode: auto\n"
        "  devices: ['/']\n"
        "resize_rootfs: true\n"
        "chpasswd:\n"
        "  expire: false\n"
        "  list: |\n"
        f"    root:{ROOT_PASSWORD}\n"
    )
    (source / "meta-data").write_text(
        "instance-id: gentoo-install-convert\nlocal-hostname: beforeconvert\n"
    )
    output = workdir / "cidata.iso"
    subprocess.run(
        [
            "xorriso", "-as", "mkisofs",
            "-quiet",
            "-output", str(output),
            "-volid", "cidata",
            "-joliet", "-rock",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    return output


def overlay(image: Path, workdir: Path) -> Path:
    """A writable copy of the image that leaves the download alone.

    A backing file rather than a copy: the images are half a gigabyte each and
    a run throws its overlay away.
    """
    if not image.is_file():
        raise FileNotFoundError(f"{image} is not there; run lab/vm/cloud/pull.sh")
    where = workdir / "root.qcow2"
    where.unlink(missing_ok=True)
    subprocess.run(
        [
            "qemu-img", "create", "-f", "qcow2",
            "-F", "qcow2", "-b", str(image.resolve()),
            str(where), OVERLAY_SIZE,
        ],
        check=True,
        capture_output=True,
    )
    return where


def reach_root(console: SerialConsole, chosen: CloudImage) -> None:
    """Log in as root once cloud-init has set the password.

    The wait is on cloud-init rather than on the login prompt: agetty offers
    one before the password exists, and each rejected attempt is one of its
    five tries.
    """
    console.expect(r"login:", timeout=CLOUD_INIT_PATIENCE)
    console.send("root")
    console.expect(PASSWORD_PROMPT, timeout=60.0)
    console.send(ROOT_PASSWORD)
    console.expect(r"#|\$", timeout=120.0)
    # `--wait` rather than a sleep: growpart and resizefs run in the same pass
    # that set the password, and a conversion started before they finish has a
    # root filesystem the size of the download.
    console.run("cloud-init status --wait || true", timeout=CLOUD_INIT_PATIENCE)
    console.run("df -h / && lsblk || true", timeout=120.0)


def install_tools(console: SerialConsole, chosen: CloudImage, config: str) -> None:
    """Install what the launcher says is missing, the way an operator would."""
    console.run(FIND_DRIVER, timeout=180.0)
    said = console.expect_output(
        f"sh /mnt/driver/install.sh --config {config} --missing-commands || true",
        timeout=300.0,
    ).decode("utf-8", "replace")
    # One bare command name per line, which is what `--missing-commands`
    # prints: an earlier reading looked for `run: ` and for the installer's own
    # name, found neither, installed nothing, and let the conversion reach a
    # preflight that refused with `these commands are missing: gpg, gpg-agent`.
    wanted = [
        chosen.packages.get(name, name)
        for name in (line.strip() for line in said.splitlines())
        if name and " " not in name and not name.startswith("MARK_")
    ]
    commands = [name for name in wanted]
    wanted += list(chosen.tools)
    if wanted:
        console.run(f"{chosen.install} {' '.join(sorted(set(wanted)))}", timeout=1800.0)
    # Checked afterwards, because a package manager given one name it does not
    # know installs nothing at all: `apt-get ... efibootmgr gpg gpg-agent
    # mkfs.vfat` answered `Unable to locate package mkfs.vfat` and left the
    # machine without any of them, and the arming then refused for a boot
    # method whose only missing piece was `efibootmgr`.
    asked = sorted(set(commands) | set(chosen.tools))
    if not asked:
        return
    said = console.expect_output(
        "for one in " + " ".join(asked) + "; do "
        'command -v "$one" >/dev/null || printf "absent=%s\\n" "$one"; done',
        timeout=300.0,
    ).decode("utf-8", "replace")
    absent = [line.split("=", 1)[1] for line in said.split() if line.startswith("absent=")]
    if absent:
        raise MediaError(
            f"{chosen.install.split()[0]} left {', '.join(absent)} missing, "
            "so the run would refuse for a command nobody installed"
        )

def write_home_marker(console: SerialConsole) -> None:
    """Write the conversion's unique sentinel outside the replaced tree."""
    console.run(
        f"printf '%s\\n' {HOME_MARKER} > {HOME_MARKER_PATH}",
        timeout=60.0,
    )


def conversion_checks(installation: InstallConfig) -> tuple[InstalledCheck, ...]:
    """Add the in-place preservation check to the installed-state contract."""
    return (*checks(installation), HOME_MARKER_CHECK)


def check_installed(console: SerialConsole, installation: InstallConfig) -> str:
    """Run installed-state checks and return the failures, if any."""
    failed: list[str] = []
    for check in conversion_checks(installation):
        said = console.expect_output(check.command, timeout=180.0)
        text = said.decode("utf-8", "replace")
        if not re.search(check.pattern, text, re.MULTILINE):
            failed.append(f"{check.name} does not match {check.pattern!r}: {text[-200:]!r}")
    return "; ".join(failed)


def convert(console: SerialConsole, config: str) -> None:
    """Run the installer in place and keep everything it printed."""
    write_home_marker(console)
    console.run("mkdir -p /tmp/gentoo-install-results", timeout=60.0)
    console.run(
        f"{{ sh /mnt/driver/install.sh --config {config}; echo $? "
        "> /tmp/gentoo-install-results/install.rc; } 2>&1 "
        "| tee /tmp/gentoo-install-results/install.txt",
        timeout=CONVERT_CEILING,
    )

def boot_and_check(root: Path, workdir: Path, chosen: CloudImage, config: str) -> str:
    """Boot what the conversion produced and read it. Answer "" when it holds.

    A conversion that exits zero has replaced a userland; whether the machine
    boots the new kernel through the new bootloader is a different question,
    and it is the one `TESTED.md` counts. The old distribution's hostname is
    what tells a machine that booted the system it was replacing from one that
    worked, so the fixture sets a different one.
    """
    installation = load(REPOSITORY / "tests" / config)
    spec = VmSpec(
        medium=MEDIA["official-minimal"],
        workdir=workdir,
        firmware=chosen.firmware,
        memory="4G",
        cpus=2,
        ssh_port=free_port(),
        targets=(root,),
        boot_installed=True,
    )
    with Vm(spec) as vm:
        console = SerialConsole.connect(vm.serial_socket, workdir / "boot.log")
        console.expect(r"login:", timeout=BOOT_PATIENCE)
        console.send("root")
        console.expect(PASSWORD_PROMPT, timeout=120.0)
        console.send(ROOT_PASSWORD)
        console.expect(r"#|\$", timeout=180.0)
        return check_installed(console, installation)


def read_the_boot_order(console: SerialConsole) -> str:
    """What the firmware will choose next, read before anything reboots.

    A Fedora conversion exited zero, wrote `\\EFI\\BOOT\\BOOTX64.EFI` and a
    `Gentoo` entry with `Installation finished. No error reported.`, and the
    machine then started `Boot0001 "Fedora"` and stopped at
    `file '/vmlinuz-6.11.4-301.fc41.x86_64' not found`. Whether the new entry
    exists, and where it sits in `BootOrder`, is the difference between those
    two and cannot be inferred from `grub-install` saying nothing.
    """
    said = console.expect_output(
        "efibootmgr -v 2>&1 || echo NO-EFIBOOTMGR", timeout=120.0
    ).decode("utf-8", "replace")
    print("--- firmware boot entries ---", flush=True)
    print(said.strip(), flush=True)
    return said


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", choices=sorted(IMAGES), default="fedora")
    parser.add_argument(
        "--config",
        default="fixtures/vm-convert.toml",
        help="the conversion configuration on the driver CD",
    )
    parser.add_argument("--memory", default="6G")
    parser.add_argument("--cpus", type=int, default=6)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the overlay behind, which is the only copy of what went wrong",
    )
    arguments = parser.parse_args(argv)
    chosen = IMAGES[arguments.image]

    workdir = confined(WORKROOT / f"{chosen.name}-{int(time.time())}")
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
        # A free one rather than the default: two conversions, or a conversion
        # beside a local install, both asked for 2222 and the second died with
        # `Could not set up host forwarding rule` and no mention of the port.
        ssh_port=free_port(),
        driver_iso=driver,
        targets=(root,),
        disks=(cidata,),
        boot_installed=True,
    )
    started = time.monotonic()
    with Vm(spec) as vm:
        console = SerialConsole.connect(vm.serial_socket, vm.serial_log)
        reach_root(console, chosen)
        print(f"[{time.monotonic() - started:5.1f}s] root shell on serial", flush=True)
        install_tools(console, chosen, arguments.config)
        convert(console, arguments.config)
        code = console.expect_command(
            "cat /tmp/gentoo-install-results/install.rc", timeout=60.0
        )
        read_the_boot_order(console)
        print(f"[{time.monotonic() - started:5.1f}s] conversion exited {code!r}", flush=True)
    if b"0" not in code:
        if not arguments.keep:
            root.unlink(missing_ok=True)
        return 1
    refused = boot_and_check(root, workdir, chosen, arguments.config)
    if refused:
        print(f"FAIL {refused}", flush=True)
    else:
        print(
            f"[{time.monotonic() - started:5.1f}s] the converted machine booted "
            "and holds what the conversion asked for",
            flush=True,
        )
    if not arguments.keep:
        root.unlink(missing_ok=True)
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
