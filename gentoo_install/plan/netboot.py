# SPDX-License-Identifier: GPL-2.0-or-later
"""Arming one boot into a memory-resident live environment.

This plan runs against the machine the operator is logged into, so its context
is built with `/` as the target rather than a mounted new system. Nothing here
installs anything: it fetches an image, puts a kernel where this machine's own
bootloader reads one, and arms a single boot. The install happens afterwards,
on the ordinary path, inside that environment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from ..errors import DownloadFailed, PreflightFailed
from ..model.architecture import ARCHITECTURES, architecture_of
from ..model.config import BootMethod, MemoryLaunch, MemoryMode, MirrorRegion
from .operations import CommandOutput, Context, Operation, Stage

#: Where the CJK ISO's releases are listed. The published asset name carries
#: the build timestamp, so nothing may pin it: an installer that names one
#: release stops working the week that release is superseded, and the ISO this
#: was first written against (`20260809T143052Z`) was already gone from the
#: index by the time the plan was written.
CJK_RELEASES: Final[str] = (
    "https://api.github.com/repos/gentoo-zh/gentoo-cjk-livecd/releases/latest"
)

#: The same images through CERNET, which answers 302 to whichever member
#: mirror is closest. Measured on 2026-08-18: the index lists one directory
#: per build (`20260813T073053Z/`), each holding the ISO, its `.sha256`, its
#: `DIGESTS` and a `CONTENTS.gz`, and the redirect landed on
#: `mirror.nyist.edu.cn` with the ISO at 989376512 bytes.
#:
#: Asked before the release index, because the machines this path exists for
#: are in China and a gigabyte from GitHub's CDN is the slowest part of the
#: whole run. GitHub answers when the mirror does not, so neither is a single
#: point of failure.
CJK_MIRROR: Final[str] = "https://mirrors.cernet.edu.cn/gentoo-zh/gentoo-cjk-livecd/"

#: What a build directory is called there. Read rather than composed: the
#: name carries a build timestamp and the newest is the greatest of them.
CJK_BUILD = re.compile(r'href="(\d{8}T\d{6}Z)/"')

#: What an asset is called inside one. The ISO and its checksum are matched
#: by suffix so a build that adds a file does not confuse this.
CJK_ASSET = re.compile(r'href="([^"?/]+\.(?:iso|iso\.sha256))"')

#: Alpine publishes version, filename and SHA-256 for every flavour in one
#: document per architecture, so the version is read rather than pinned here
#: and the architecture is substituted rather than written in. Alpine spells
#: it the way the kernel does: `x86_64` and `aarch64` both answer, checked on
#: 2026-08-18, and the netboot flavour is present in each with the same
#: fields.

#: Where each mirror region fetches Alpine from. The China entry is the host
#: `tests/vm/media.py` already installs Alpine packages from, so it is
#: measured rather than chosen.
ALPINE_MIRRORS: Final[dict[MirrorRegion, str]] = {
    MirrorRegion.CN: "https://mirrors.ustc.edu.cn/alpine",
    MirrorRegion.GLOBAL: "https://dl-cdn.alpinelinux.org/alpine",
}
ALPINE_RELEASES: Final[str] = (
    "{base}/latest-stable/releases/{architecture}/latest-releases.yaml"
)
ALPINE_NETBOOT_FLAVOUR: Final[str] = "alpine-netboot"

#: Alpine's own repository, which its netboot init installs the packages from.
#: `alpine_repo=auto` searches for a `.boot_repository` file and finds none on
#: a machine that booted from a local kernel.
ALPINE_REPOSITORY: Final[str] = "{base}/latest-stable/main"

#: Where the kernel modules come from. `modloop=` is not read by
#: `initramfs-init` at all: the reader is `/etc/init.d/modloop` in the booted
#: root, which parses `/proc/cmdline` itself and `wget`s an `http://` value
#: (`openrc/modloop.initd:16,78`). Both fields are filled from the netboot
#: archive this run downloaded rather than written in, so an aarch64 machine
#: composes an aarch64 URL and the modloop is the one built beside the kernel
#: that will be running.
ALPINE_MODLOOP: Final[str] = (
    "{base}/latest-stable/releases/{architecture}/netboot-{version}/modloop-{flavour}"
)

#: Which kernel Alpine builds for each architecture. Not one word everywhere:
#: `alpine-netboot-3.24.1-aarch64.tar.gz` holds `virt` and `rpi` and no `lts`
#: at all, measured 2026-08-29, so an aarch64 run composing `vmlinuz-lts`
#: extracts nothing. `virt` is the one built for a machine with no hardware of
#: its own, which is every cloud instance this arms.
ALPINE_FLAVOUR: Final[dict[str, str]] = {
    "x86_64": "lts",
    "i686": "lts",
    "aarch64": "virt",
}

#: What Alpine names a netboot archive. `netboot/` beside it holds whatever
#: release is newest, so pinning the version keeps `modules/$(uname -r)` inside
#: the modloop matching the kernel taken out of this archive.
ALPINE_ARCHIVE = re.compile(
    r"^alpine-netboot-(?P<version>.+)-(?P<architecture>[^-]+)\.tar\.gz$"
)

#: One directory for everything this arms, on the filesystem the bootloader
#: reads. Named rather than scattered, because disarming has to be able to
#: delete exactly what arming wrote.
PLACE: Final[str] = "gentoo-install-ram"

#: Where the download lands, on the root filesystem rather than the esp. The
#: esp is whatever size that machine was given \u2014 124 MiB on a Debian cloud
#: image \u2014 and the images are 373 MB and about a gigabyte, so the first real
#: `--lowram` run ended at `curl: (23) Failure writing output to destination`
#: with 111 MB written. The firmware only ever reads a kernel and an initramfs
#: from the esp, and those are tens of megabytes.
STAGING: Final[PurePosixPath] = PurePosixPath("/") / PLACE

#: What the decline answers, in the banner's own words. `nothing was changed`
#: was wrong for the same reason the banner no longer says the disk is
#: untouched: the payload and the boot entry are already on the machine.
DECLINED: Final[str] = "no partition table and no filesystem was changed"

#: Written inside the placed directory by `--bypass` alone, so a disarm can
#: tell a default this installer replaced from one the operator chose.
BYPASS_RECORD: Final[str] = "replaced-default"

#: What the entry is called wherever the boot method keeps names.
ENTRY_LABEL: Final[str] = "gentoo-install memory environment"

#: The markers a BIOS GRUB `custom.cfg` entry is written between, so a second
#: arming replaces the first rather than appending beside it.
CUSTOM_BEGIN: Final[str] = "### BEGIN gentoo-install memory environment"
CUSTOM_END: Final[str] = "### END gentoo-install memory environment"

#: What the CJK ISO's own `grub.cfg` passes, read from the ISO on 2026-08-17,
#: plus the three this path has to add. `dodhcp` because the ISO ships
#: `nodhcp` and a memory environment with no network cannot fetch a stage3;
#: `rd.live.ram=1` because the disk holding the ISO is the disk the install
#: erases; `iso-scan/filename=` because GRUB's `loopback` is visible only
#: inside GRUB and the initramfs finds the file itself.
CJK_CMDLINE: Final[tuple[str, ...]] = (
    "dokeymap",
    "dodhcp",
    "rd.live.dir=/",
    "rd.live.squashimg=image.squashfs",
    "cdroot",
    "rd.live.ram=1",
)

#: What the machine this plan runs on booted with. Read through the runner
#: rather than opened here: `plan/` touches no machine of its own.
RUNNING_CMDLINE: Final[str] = "/proc/cmdline"

#: The one `console=` value that means no console. Alpine's
#: `setup_inittab_console` returns before writing a single getty when it is
#: present (`initramfs-init.in:136-140`) and hands `switch_root -c /dev/null`,
#: and `LIVECD_CONSOLE=null` puts the CJK medium's getty on `/dev/null`.
#: Carrying it forward would turn "use what the machine uses" into "give the
#: operator nothing", where writing none at least leaves both media's own
#: detection to run.
SILENT_CONSOLES: Final[frozenset[str]] = frozenset({"console=null"})

#: `check_live_ram` in the ISO's initramfs enters an emergency shell when
#: `MemTotal - image` is under `rd.minmem`, which defaults to 1024 MiB. The
#: `image.squashfs` measured on 2026-08-17 is 824 MiB, so a machine under this
#: does not boot slowly, it stops at a shell nobody is watching.
RAM_FLOOR_MIB: Final[int] = 1900

#: Alpine's netboot kernel carries no `zfs.ko`, so a layout needing ZFS is a
#: refusal rather than an attempt. `model/validate.py` holds that rule; this
#: constant is what the message names.
LOWRAM_FLOOR_MIB: Final[int] = 512


@dataclass(frozen=True)
class BootTarget:
    """What this machine boots with, as `exec/probe.py` answered it.

    Carried rather than probed here: `plan/` derives operations and reads no
    machine. The esp fields are absent on a BIOS machine, and the bootloader
    directory is absent on a UEFI one.

    No disk and partition number: both GRUBs are armed by writing a marked
    entry and `grub-reboot`, so nothing here composes an `efibootmgr
    --create-only` call. Carrying the two facts an unwritten call would need
    reads as though it were written, which is the shape of a promise the code
    does not keep.
    """

    method: BootMethod
    #: What `uname -m` answers here. Both environments are published per
    #: architecture, so this decides which one is fetched rather than being
    #: assumed: `--ram` works on any machine the CJK release publishes an ISO
    #: for, the day it publishes one.
    architecture: str
    esp_mountpoint: str | None = None
    grub_directory: str | None = None
    #: Whether the firmware refuses an unsigned kernel. `None` is unread,
    #: which is a BIOS machine or one whose efivarfs is not mounted.
    secure_boot: bool | None = None
    #: Whether `/boot` is a directory on the root filesystem rather than a
    #: mount of its own. GRUB's paths are relative to the filesystem its
    #: `root` names, so the same file is `/gentoo-install-ram/kernel` when
    #: `/boot` is its own partition and `/boot/gentoo-install-ram/kernel` when
    #: it is not. `None` is unread, and is treated as separate because that is
    #: what every layout this installer produces has.
    boot_on_the_root_filesystem: bool | None = None

    @property
    def grub_prefix(self) -> str:
        """What a path in a GRUB entry has to start with on this machine."""
        return "/boot" if self.boot_on_the_root_filesystem else ""

    @property
    def place(self) -> PurePosixPath:
        """Where the kernel and the initramfs go for this method.

        systemd-boot and a UEFI GRUB read from the esp; a BIOS GRUB reads from
        wherever its own configuration lives, which is `/boot` on every layout
        this installer produces.
        """
        if self.method is BootMethod.BIOS_GRUB:
            return PurePosixPath("/boot") / PLACE
        if self.esp_mountpoint is None:
            raise PreflightFailed(
                "this machine boots by UEFI and no EFI system partition is mounted, "
                "so there is nowhere the firmware would read a kernel from"
            )
        return PurePosixPath(self.esp_mountpoint) / PLACE


@dataclass(frozen=True, kw_only=True)
class RefuseWithoutABootMethod(Operation):
    """Nothing may be downloaded before it is known that it can be booted.

    An arming that cannot be undone is the one failure this path must not
    have, so the refusals come before the fetch rather than after it.
    """

    stage: Stage = Stage.PREFLIGHT
    target: BootTarget

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "check that this machine can be told to boot once, {}", (
            self.target.method.value,
        )

    def apply(self, context: Context) -> None:
        if self.target.method is BootMethod.NONE:
            raise PreflightFailed(
                "no bootloader here can be told to boot once: this needs "
                "systemd-boot, a UEFI GRUB with efibootmgr, or a BIOS GRUB"
            )
        if self.target.secure_boot:
            # Neither image is signed for this machine's own db, so the
            # firmware rejects the kernel and the armed boot goes nowhere.
            # `--bypass` makes that the default, which is a machine that does
            # not boot at all, so this is refused before anything is fetched.
            raise PreflightFailed(
                "this machine enforces Secure Boot and neither memory "
                "environment is signed for it: turn Secure Boot off in the "
                "firmware setup, or install from a medium instead"
            )
        # Reading it forces the layout check in `place` before anything is
        # fetched, so a UEFI machine with no mounted esp stops here.
        self.target.place


@dataclass(frozen=True, kw_only=True)
class RefuseTooLittleMemory(Operation):
    """`--ram` reads the whole squashfs into RAM, and the initramfs stops at an
    emergency shell rather than saying so on a machine that cannot hold it."""

    stage: Stage = Stage.PREFLIGHT
    mode: MemoryMode

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "check this machine has the {} MiB {} needs", (
            str(self.floor),
            f"--{self.mode.value}",
        )

    @property
    def floor(self) -> int:
        return RAM_FLOOR_MIB if self.mode is MemoryMode.RAM else LOWRAM_FLOOR_MIB

    def apply(self, context: Context) -> None:
        said = context.run(["free", "--mebi", "--total"], check=False)
        if isinstance(said, CommandOutput) and said.returncode != 0:
            # Unreadable is not the same as too little, and refusing on a
            # command that did not run is a refusal nobody can act on.
            return
        total = _total_mebibytes(said)
        if total is not None and total < self.floor:
            raise PreflightFailed(
                f"{self.mode.value} needs about {self.floor} MiB and this machine "
                f"has {total}: the live image is read into memory, and under "
                "that the initramfs stops at an emergency shell"
            )


@dataclass(frozen=True, kw_only=True)
class ClearPreviousArming(Operation):
    """Take back whatever an earlier run armed, before this one writes.

    A second run that stops at the download or the unpack otherwise leaves the
    first one's arming in place, and the next reboot \u2014 months later, for
    another reason \u2014 enters a memory environment carrying a configuration
    nobody meant to install any more. `reinstall` clears its own state before
    it fetches for the same reason (`reinstall.sh:4485-4547`, called at 5016).

    The placed directory goes too: a stale image beside the new one makes the
    one this plan looks for ambiguous.
    """

    stage: Stage = Stage.PREFLIGHT
    target: BootTarget

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "take back anything an earlier run armed", ()

    def apply(self, context: Context) -> None:
        _take_back(context, self.target)


def _take_back(context: Context, target: BootTarget) -> None:
    """Clear the one-shot and delete what an arming placed.

    Both operations that undo an arming do exactly this: one runs before a new
    arming writes, the other when the operator changed their mind.
    """
    _disarm(context, target)
    _remove_the_entry(context, target)
    for gone in (target.place, STAGING):
        context.run(["rm", "--recursive", "--force", str(gone)], check=False)


def _remove_the_entry(context: Context, target: BootTarget) -> None:
    """Delete what `WriteMemoryEntry` wrote, which is outside `target.place`.

    Deleting the kernel and leaving the entry gives a menu whose first choice
    stops at `error: file not found` for ever, and on `--bypass` that entry is
    also the default.
    """
    if target.method is BootMethod.SYSTEMD_BOOT:
        entry = PurePosixPath(str(target.esp_mountpoint)) / "loader" / "entries" / f"{PLACE}.conf"
        context.run(["rm", "--force", str(entry)], check=False)
        return
    if target.grub_directory is None:
        return
    custom = PurePosixPath(target.grub_directory) / "custom.cfg"
    written = context.read(custom)
    kept = _without_previous(written)
    if kept != written:
        context.write(custom, kept)


def _disarm(context: Context, target: BootTarget) -> None:
    """Clear the one-shot this module sets, whichever way it was set.

    `bootctl set-oneshot ""` and not a named entry: `bootctl(1)` says an empty
    ID unsets the variable, while any other value is another entry to boot
    next. This asked for `auto-reboot-to-firmware-setup`, which is not a
    disarm \u2014 it is a machine that reboots into its firmware setup instead.
    """
    if target.method is BootMethod.SYSTEMD_BOOT:
        context.run(["bootctl", "set-oneshot", ""], check=False)
        if context.read(target.place / BYPASS_RECORD):
            # An empty ID unsets the variable, `bootctl(1)`. Only on the record
            # `--bypass` left: the machine falls back to `loader.conf`, and one
            # that was never bypassed keeps the default the operator set.
            context.run(["bootctl", "set-default", ""], check=False)
        return
    if target.grub_directory is not None:
        environment = f"{target.grub_directory}/grubenv"
        context.run(["grub-editenv", environment, "unset", "next_entry"], check=False)
        # Only when it names this installer's entry: `--bypass` wrote it with
        # `grub-set-default`, and unsetting it unconditionally would throw away
        # a default the operator chose on a machine that was never bypassed.
        # The whole line, so a failure's message on stdout cannot match it and
        # the exit code does not have to be read here.
        listed = context.run(["grub-editenv", environment, "list"], check=False)
        if f"saved_entry={ENTRY_LABEL}" in str(listed).splitlines():
            context.run(["grub-editenv", environment, "unset", "saved_entry"], check=False)


@dataclass(frozen=True, kw_only=True)
class FetchMemoryImage(Operation):
    """Download the image and check it against the checksum its publisher gives.

    A wrong image is discovered at the next boot, when the machine the
    operator was logged into is no longer answering.
    """

    stage: Stage = Stage.STAGE3
    mode: MemoryMode
    target: BootTarget
    region: MirrorRegion = MirrorRegion.GLOBAL

    def required_host_commands(self) -> frozenset[str]:
        return frozenset({"curl", "sha256sum", "mkdir", "rm"})

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "fetch the {} image and verify its checksum", (self.mode.value,)

    def apply(self, context: Context) -> None:
        place = STAGING
        context.run(["mkdir", "--parents", str(place)])
        name, url, checksum = (
            _cjk_release(context, self.target.architecture)
            if self.mode is MemoryMode.RAM
            else _alpine_release(context, self.target.architecture, self.region)
        )
        image = place / name
        context.run(["curl", "--fail", "--location", "--output", str(image), url])
        said = context.run(["sha256sum", str(image)])
        got = said.split()[0] if said.split() else ""
        if got != checksum:
            context.run(["rm", "--force", str(image)])
            raise DownloadFailed(
                f"{name} hashes to {got or 'nothing'} and its publisher says "
                f"{checksum}, so it was not written where the firmware reads it"
            )


@dataclass(frozen=True, kw_only=True)
class PlaceMemoryKernel(Operation):
    """Take the kernel and the initramfs out of the image.

    Only these two are moved. For `--ram` the ISO itself stays where it was
    downloaded, because `iso-scan/filename` is what the initramfs uses to find
    it and it does not have to fit on the esp.
    """

    stage: Stage = Stage.BOOTLOADER
    mode: MemoryMode
    target: BootTarget

    def required_host_commands(self) -> frozenset[str]:
        # `xorriso` reads the ISO and `tar` the netboot archive; a machine
        # without the one its mode needs refused at the unpack rather than in
        # preflight, an image and several minutes later.
        reader = "xorriso" if self.mode is MemoryMode.RAM else "tar"
        return frozenset({reader, "mkdir", "blkid" if self.mode is MemoryMode.RAM else "ls"})

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "unpack the {} kernel and initramfs into {}", (
            self.mode.value,
            str(self.target.place),
        )

    def apply(self, context: Context) -> None:
        place = self.target.place
        context.run(["mkdir", "--parents", str(place)])
        if self.mode is MemoryMode.RAM:
            image = _only_image(context, STAGING, ".iso")
            for member, name in (("/boot/gentoo", "kernel"), ("/boot/gentoo.igz", "initramfs")):
                context.run(
                    [
                        "xorriso",
                        "-osirrox",
                        "on",
                        "-indev",
                        str(image),
                        "-extract",
                        member,
                        str(place / name),
                    ]
                )
            return
        archive = _only_image(context, STAGING, ".tar.gz")
        flavour = _alpine_flavour(archive)
        for member, name in (
            (f"boot/vmlinuz-{flavour}", "kernel"),
            (f"boot/initramfs-{flavour}", "initramfs"),
        ):
            context.run(
                [
                    "tar",
                    "--extract",
                    # Alpine stores these as uid 1000 and GNU tar restores
                    # ownership as root, which the esp cannot express: the
                    # first run to get this far answered `tar: kernel: Cannot
                    # change ownership to uid 1000, gid 1000: Operation not
                    # permitted` and exited 2 with the kernel half written.
                    "--no-same-owner",
                    "--no-same-permissions",
                    "--file",
                    str(archive),
                    "--directory",
                    str(place),
                    "--transform",
                    f"s|.*|{name}|",
                    member,
                ]
            )



#: Where the payload lands inside both live systems.
PAYLOAD: Final[str] = "/gentoo-install"

#: The credential record is read by `chpasswd` after the live system starts.
ROOT_PASSWORD: Final[str] = "root-password"

#: The one-shot local hook that applies the credential record.
ROOT_PASSWORD_START: Final[str] = "gentoo-install.start"

#: The archive `initramfs-init` unpacks after it has built Alpine's sysroot.
APKOVL: Final[str] = "gentoo-install.apkovl.tar.gz"

#: The marker that keeps Alpine's default boot services on a machine that
#: brings its own apkovl. `initramfs-init` removes it after reading it.
DEFAULT_SERVICES: Final[str] = "/etc/.default_boot_services"

#: The login profile that sources the first screen in each environment. CJK
#: root uses bash; Alpine root uses ash and reads `.profile`.
AUTOSTART: Final[dict[MemoryMode, str]] = {
    MemoryMode.RAM: "/root/.bash_profile",
    MemoryMode.LOWRAM: "/root/.profile",
}


@dataclass(frozen=True, kw_only=True)
class AppendConfiguration(Operation):
    """Put the configuration and the keys inside the initramfs.

    A newc cpio appended to the compressed initramfs is unpacked by the kernel
    without repacking its 55 MiB base image. `--ram` uses the appended dracut
    hook; `--lowram` puts an apkovl in that cpio's root, because Alpine's
    `initramfs-init` unpacks it into the live sysroot without running dracut.
    """

    stage: Stage = Stage.BOOTLOADER
    target: BootTarget
    launch: MemoryLaunch
    #: The operator's configuration, already rendered. Carried rather than
    #: read, because `plan/` does no I/O.
    configuration: str
    #: Where this installer is running from. Its own tree goes in the payload:
    #: 1.4 MiB packed, and it guarantees the memory environment runs the
    #: revision that wrote the configuration rather than whatever a later
    #: download would bring.
    source: str
    keys: tuple[str, ...] = ()
    #: Whether the payload carries credentials that need an ssh daemon.
    access: bool = False

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        # Every file it writes 0600, named: the old text passed the dry-run
        # rule because the word `inside` appeared in `inside the initramfs`,
        # not because it named the root password record.
        if self.launch.root_password:
            return (
                "put the installer and the configuration in the initramfs, with {} key(s)"
                " in authorized_keys and the root password in root-password",
                (str(len(self.keys)),),
            )
        return (
            "put the installer and the configuration in the initramfs, with {} key(s) in authorized_keys",
            (str(len(self.keys)),),
        )

    def required_host_commands(self) -> frozenset[str]:
        commands = {"cpio", "dd", "find", "mkdir", "python3", "rm", "tar"}
        if self.launch.mode is MemoryMode.LOWRAM and self.needs_ssh:
            commands.add("ln")
        return frozenset(commands)

    @property
    def needs_ssh(self) -> bool:
        """Whether the payload carries anything that wants a running sshd.

        `build()` derives `access` from the same three inputs, so this repeats
        it only for an operation built by hand rather than through the plan.
        """
        return (
            self.access
            or bool(self.keys)
            or bool(self.launch.ssh_key)
            or bool(self.launch.root_password)
        )

    def apply(self, context: Context) -> None:
        staging = self.target.place / "payload"
        payload_staging = (
            staging
            if self.launch.mode is MemoryMode.RAM
            else staging / "apkovl"
        )
        inside = payload_staging / PAYLOAD.lstrip("/")
        context.run(["mkdir", "--parents", str(inside)])
        context.write(inside / "config.toml", self.configuration)
        if self.keys:
            key_text = "".join(f"{one}\n" for one in self.keys)
            context.write(inside / "authorized_keys", key_text, mode=0o600)
            if self.launch.mode is MemoryMode.LOWRAM:
                context.write(
                    payload_staging / "root/.ssh/authorized_keys",
                    key_text,
                    mode=0o600,
                )
        if self.launch.root_password:
            password_start = _root_password_start()
            context.write(
                inside / ROOT_PASSWORD,
                _root_password_record(self.launch.root_password),
                mode=0o600,
            )
            context.write(inside / ROOT_PASSWORD_START, password_start, mode=0o755)
            if self.launch.mode is MemoryMode.LOWRAM:
                context.write(
                    payload_staging / "etc/local.d" / ROOT_PASSWORD_START,
                    password_start,
                    mode=0o755,
                )
        if self.launch.mode is MemoryMode.LOWRAM and self.needs_ssh:
            runlevel = payload_staging / "etc/runlevels/default"
            context.run(["mkdir", "--parents", str(runlevel)])
            context.run(
                [
                    "ln",
                    "--symbolic",
                    "--force",
                    "/etc/init.d/sshd",
                    str(runlevel / "sshd"),
                ]
            )
        # `bootstrap.sh` runs the tree beside it, so both go in together.
        context.pipe(
            [
                "tar",
                "--create",
                "--exclude=__pycache__",
                "--directory",
                self.source,
                "gentoo_install",
                "bootstrap.sh",
            ],
            [
                "tar",
                "--extract",
                # The ESP is vfat and has no owners, so restoring them aborts
                # after copying only part of the installer tree.
                "--no-same-owner",
                "--no-same-permissions",
                "--directory",
                str(inside),
            ],
        )
        context.write(inside / "start.sh", _start(self.launch.mode), mode=0o755)
        context.write(inside / "command.sh", _command(), mode=0o755)
        if self.launch.mode is MemoryMode.RAM:
            hooks = staging / "usr/lib/dracut/hooks/pre-pivot"
            context.run(["mkdir", "--parents", str(hooks)])
            context.write(
                hooks / "99-gentoo-install.sh",
                _handover(self.launch.mode),
                mode=0o755,
            )
        else:
            context.write(
                payload_staging / AUTOSTART[self.launch.mode].lstrip("/"),
                _source_start(),
            )
            # The apkovl is unpacked over `/`, so this lands on `PATH`.
            context.write(payload_staging / COMMAND.lstrip("/"), _command(), mode=0o755)
            # `initramfs-init` adds services only when this marker is present.
            # Without it Alpine reaches the preflight with no `/lib/modules`.
            context.write(payload_staging / DEFAULT_SERVICES.lstrip("/"), "")
            apkovl = staging / APKOVL
            context.run(
                [
                    "tar",
                    "--create",
                    "--gzip",
                    "--file",
                    str(apkovl),
                    "--directory",
                    str(payload_staging),
                    ".",
                ]
            )
            context.run(["rm", "--recursive", "--force", str(payload_staging)])
        # `%P` drops the staging prefix, so cpio members land in the initramfs
        # root rather than in a directory the live environment never reads.
        image = self.target.place / "initramfs"
        # The kernel reads a concatenated initramfs segment only from a
        # four-byte boundary; Alpine's `initramfs-lts` measured 3 mod 4.
        context.run(["python3", "-c", _PAD_INITRAMFS, str(image)])
        archive = self.target.place / "payload.cpio"
        context.pipe(
            [
                "find",
                str(staging),
                "-mindepth",
                "1",
                "-printf",
                "%P\\0",
            ],
            [
                "cpio",
                "--create",
                "--format=newc",
                "--null",
                "--directory",
                str(staging),
                "--file",
                str(archive),
            ],
        )
        context.run(
            [
                "dd",
                f"if={archive}",
                f"of={image}",
                "bs=1M",
                "oflag=append",
                "conv=notrunc",
                "status=none",
            ]
        )
        context.run(["rm", "--force", str(archive)])
        context.run(["rm", "--recursive", "--force", str(staging)])


_PAD_INITRAMFS: Final[str] = (
    "import os, sys; "
    "descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND); "
    "padding = -os.fstat(descriptor).st_size % 4; "
    "os.write(descriptor, b'\\0' * padding); "
    "os.close(descriptor)"
)


def _root_password_record(password: str) -> str:
    """The `chpasswd` record that carries a live-environment password."""
    if "\n" in password or "\r" in password:
        raise PreflightFailed("--root-password cannot contain a newline")
    return f"root:{password}\n"


def _root_password_start() -> str:
    """Set the live root password after its boot service has started."""
    return (
        "#!/bin/sh\n"
        f"chpasswd < {PAYLOAD}/{ROOT_PASSWORD} || exit 1\n"
        f"rm --force {PAYLOAD}/{ROOT_PASSWORD}\n"
        "rc-service sshd restart\n"
        'rm --force "$0"\n'
    )


#: Alpine's package for the interpreter the installer runs on.
ALPINE_PYTHON: Final[str] = "python3"

#: Where the kernel exposes the firmware variables a UEFI boot entry is
#: written into, and where the mounted filesystems are listed. Named here so a
#: test can point the script at files of its own rather than at this machine's.
FIRMWARE: Final[str] = "/sys/firmware/efi"
EFIVARS: Final[str] = "/sys/firmware/efi/efivars"
MOUNTS: Final[str] = "/proc/mounts"


def _ready_the_environment() -> str:
    """Make the memory environment able to run the installer at all.

    Alpine's netboot root ships busybox and `apk` and no Python, so the first
    `--lowram` install ended at `this installer needs python 3.11 or newer`.
    `--update-cache` because `alpine_repo=` writes the repository file and
    leaves no index beside it.
    """
    return (
        # Alpine's netboot init mounts no efivarfs, so the preflight refused
        # the first `--lowram` install with `the firmware variables are not
        # readable` on a machine that boots through them.
        f"    if [ -d {FIRMWARE} ] && ! grep -q ' efivarfs ' {MOUNTS}; then\n"
        # Alpine's `linux-lts` ships `efivarfs.ko.gz`, so the type is a module
        # and the mount answered `No such device` without this.
        "        modprobe efivarfs 2>/dev/null || true\n"
        # The mount point is absent on that guest as well, and the failure is
        # printed because the preflight's refusal is the only other evidence.
        f"        mkdir -p {EFIVARS}\n"
        f"        mount -t efivarfs efivarfs {EFIVARS} || "
        "printf 'the firmware variables did not mount\\n'\n"
        "    fi\n"
        "    if ! command -v python3 >/dev/null 2>&1 && command -v apk >/dev/null 2>&1; "
        "then\n"
        "        printf 'installing python3 into the memory environment\\n'\n"
        f"        apk add --update-cache --quiet {ALPINE_PYTHON} || "
        "printf 'python3 could not be installed\\n'\n"
        "    fi\n"
    )


#: The question the delivered profile ends on. `tests/vm/ram.py` waits for
#: this exact text to decide the configuration arrived, so it is defined here
#: and imported there: changing the wording in one place alone made every
#: `--ram` and `--lowram` run time out for a day without anyone noticing.
ASKS_TO_INSTALL: Final[str] = "install now? [yes/no]"

#: An answer outside `yes|y|install`, which the profile reads as a decline.
DECLINES: Final[str] = "no"


#: Where the command lands, because it is on `PATH` in both environments and
#: is not part of either distribution's own package set.
COMMAND: Final[str] = "/usr/local/sbin/gentoo-install"

#: How to bring up wifi, per environment. The CJK ISO carries
#: `net-misc/networkmanager` with its default `+wifi` and `sys-kernel/
#: linux-firmware`; the Alpine netboot initrd's `drivers/net` holds only
#: `ethernet`, `mdio`, `net_failover` and `virtio_net`, no supplicant, and its
#: full module set is in a `modloop` that itself has to be fetched over the
#: network.
WIFI_LINES: Final[dict[MemoryMode, tuple[str, ...]]] = {
    MemoryMode.RAM: (
        "wifi: nmcli device wifi connect <SSID> password <PASSWORD>",
        "\u7121\u7dda\u7db2\u8def\uff1anmcli device wifi connect <SSID> password <\u5bc6\u78bc>",
        "\u65e0\u7ebf\u7f51\u7edc\uff1animcli device wifi connect <SSID> password <\u5bc6\u7801>",
    ),
    MemoryMode.LOWRAM: (
        "wifi is not available here: this environment has no wireless driver",
        "and no supplicant. Connect this machine by cable before installing.",
    ),
}

#: The banner, in the languages that environment can draw. The Alpine netboot
#: bundle has no CJK font, so Chinese there is a row of blanks.
#:
#: Not `the disk has not been touched`: `PlaceMemoryKernel` put a kernel and
#: an initramfs on the boot target and `WriteMemoryEntry` added an entry to
#: it, both before the reboot that reaches this screen. What is still true is
#: the part an operator is deciding about, so that is what it says.
BANNER: Final[dict[MemoryMode, tuple[str, ...]]] = {
    MemoryMode.RAM: (
        "gentoo-install is in memory. Its own kernel and boot entry are on this",
        "machine; no partition table and no filesystem has been changed.",
        "\u5b89\u88dd\u5668\u5df2\u5728\u8a18\u61b6\u9ad4\u4e2d\u3002\u5b83\u7684\u6838\u5fc3\u8207\u958b\u6a5f\u9805\u76ee\u5df2\u5beb\u5165\u9019\u53f0\u6a5f\u5668\uff0c",
        "\u5206\u5272\u8868\u8207\u6a94\u6848\u7cfb\u7d71\u90fd\u9084\u6c92\u6709\u8b8a\u52d5\u3002",
        "\u5b89\u88c5\u5668\u5df2\u5728\u5185\u5b58\u4e2d\u3002\u5b83\u7684\u5185\u6838\u4e0e\u542f\u52a8\u9879\u5df2\u5199\u5165\u8fd9\u53f0\u673a\u5668\uff0c",
        "\u5206\u533a\u8868\u4e0e\u6587\u4ef6\u7cfb\u7edf\u90fd\u8fd8\u6ca1\u6709\u53d8\u52a8\u3002",
    ),
    MemoryMode.LOWRAM: (
        "gentoo-install is in memory. Its own kernel and boot entry are on this",
        "machine; no partition table and no filesystem has been changed.",
    ),
}


def _command() -> str:
    """The installer as a command, so the first screen is not the only way in.

    An operator who answers `shell`, whose connection drops before they
    answer, or who has to bring up wifi first would otherwise have to reboot
    the machine to see the offer again.
    """
    return (
        "#!/bin/sh\n"
        f"cd {PAYLOAD} || exit 1\n"
        + _ready_the_environment()
        + "exec sh ./bootstrap.sh --no-shell --install-missing "
        + f"--config {PAYLOAD}/config.toml \"$@\"\n"
    )


def _start(mode: MemoryMode) -> str:
    """What the operator's login shell runs. It asks before it erases anything.

    Sourced by a login profile rather than executed by a boot service, so it
    must not `exit`: that would end the login shell it is running in and drop
    the operator straight back to a prompt they cannot use. `return` ends a
    sourced file and nothing else.

    Two answers and no timeout: a countdown that ends in a partitioned disk is
    a countdown nobody can lose safely, and this path is reached by rebooting
    a machine whose operator may still be reconnecting to it. Answering `no`
    is not the end of the road either \u2014 the banner names the command that
    starts it again.
    """
    said = "".join(f"printf '%s\\n' '{one}'\n" for one in BANNER[mode])
    wifi = "".join(f"printf '%s\\n' '{one}'\n" for one in WIFI_LINES[mode])
    return (
        "#!/bin/sh\n"
        f"cd {PAYLOAD} || return 0\n"
        "printf '\\n'\n"
        + said
        + "printf '\\n'\n"
        + wifi
        + f"printf '%s\\n' 'start it later with: {COMMAND}'\n"
        "printf '\\n'\n"
        f"printf '%s' '{ASKS_TO_INSTALL} '\n"
        "read answer\n"
        'case "$answer" in\n'
        "yes|y|install)\n"
        # `sh`, not `exec sh`: this is sourced, so exec removes the login shell.
        + f"    sh {COMMAND} ;;\n"
        + f"*) printf '%s\\n' '{DECLINED}; run {COMMAND} when ready' ;;\n"
        "esac\n"
    )


def _source_start() -> str:
    """The profile line that presents the delivered installer."""
    return f"[ -f {PAYLOAD}/start.sh ] && . {PAYLOAD}/start.sh\n"


def _handover(mode: MemoryMode) -> str:
    """What dracut runs to hand the payload to the CJK live system.

    `pre-pivot`, because `$NEWROOT` is mounted by then and writable: with
    `rd.live.ram=1` the squashfs is read-only but `dmsquash-live-root` puts an
    overlay over it, so what is written here survives that boot.
    """
    return (
        "#!/bin/sh\n"
        f'[ -d "$NEWROOT" ] || exit 0\n'
        f'mkdir -p "$NEWROOT{PAYLOAD}" "$NEWROOT/root/.ssh"\n'
        f'cp -a {PAYLOAD}/. "$NEWROOT{PAYLOAD}/" 2>/dev/null || exit 0\n'
        f'if [ -f "$NEWROOT{PAYLOAD}/authorized_keys" ]; then\n'
        f'    cp "$NEWROOT{PAYLOAD}/authorized_keys" "$NEWROOT/root/.ssh/authorized_keys"\n'
        f'    chmod 700 "$NEWROOT/root/.ssh"\n'
        f'    chmod 600 "$NEWROOT/root/.ssh/authorized_keys"\n'
        "fi\n"
        f'if [ -f "$NEWROOT{PAYLOAD}/{ROOT_PASSWORD}" ]; then\n'
        f'    mkdir -p "$NEWROOT/etc/local.d"\n'
        f'    mv "$NEWROOT{PAYLOAD}/{ROOT_PASSWORD_START}" '
        f'"$NEWROOT/etc/local.d/{ROOT_PASSWORD_START}"\n'
        "fi\n"
        # Appended, not written: the file exists on the medium and sources
        # `.bashrc`, and replacing it would take the prompt and the aliases
        # with it.
        # On `PATH` as well as in the payload: answering `no`, or losing the
        # connection before answering, otherwise leaves rebooting as the only
        # way back to the offer.
        f'install -m 755 "$NEWROOT{PAYLOAD}/command.sh" "$NEWROOT{COMMAND}" '
        "2>/dev/null || true\n"
        f'printf \'\\n{_source_start()}\' >> "$NEWROOT{AUTOSTART[mode]}"\n'
    )


@dataclass(frozen=True, kw_only=True)
class DiscardTheArchive(Operation):
    """Delete the netboot archive once everything that reads it has.

    Transport, not payload: the two files are out and `modloop=` names a URL
    rather than this file. It is 373 MB on the root filesystem of a machine
    about to reboot into memory. After the entry, not before it: the entry
    composes that URL from this file's name, and deleting it first ended a run
    with `/gentoo-install-ram holds 0 files ending .tar.gz`.
    """

    stage: Stage = Stage.BOOTLOADER

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "delete the downloaded archive, now that its two files are out", ()

    def apply(self, context: Context) -> None:
        archive = _only_image(context, STAGING, ".tar.gz")
        context.run(["rm", "--force", str(archive)])


@dataclass(frozen=True, kw_only=True)
class WriteMemoryEntry(Operation):
    """Write the entry the armed boot selects, in this machine's own format."""

    stage: Stage = Stage.BOOTLOADER
    mode: MemoryMode
    target: BootTarget
    launch: MemoryLaunch
    region: MirrorRegion = MirrorRegion.GLOBAL
    #: `--bypass`, which the entry itself has to carry on a GRUB machine.
    bypass: bool = False
    #: Whether the payload carries credentials that need an ssh daemon.
    access: bool = False

    @property
    def needs_ssh(self) -> bool:
        """Whether this entry has to ask Alpine to install OpenSSH.

        The keys live on `AppendConfiguration`, so this reads `access`, which
        `build()` derives from them and from both launch credentials.
        """
        return self.access or bool(self.launch.ssh_key) or bool(self.launch.root_password)

    def destinations(self) -> tuple[PurePosixPath, ...]:
        # Only the systemd-boot entry: the GRUB branch delegates to
        # `_write_custom`, and a file another path writes is named by that one.
        if self.target.method is not BootMethod.SYSTEMD_BOOT:
            return ()
        return (
            PurePosixPath(str(self.target.esp_mountpoint))
            / "loader"
            / "entries"
            / f"{PLACE}.conf",
        )

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        written = self.destinations()
        if not written:
            return "write a {} entry for the {} environment", (
                self.target.method.value,
                self.mode.value,
            )
        return "write {}, a {} entry for the {} environment", (
            str(written[0]),
            self.target.method.value,
            self.mode.value,
        )

    def apply(self, context: Context) -> None:
        place = self.target.place
        cmdline = " ".join(self._cmdline(context, place))
        if self.target.method is BootMethod.SYSTEMD_BOOT:
            entry = (
                f"title   {ENTRY_LABEL}\n"
                f"linux   /{PLACE}/kernel\n"
                f"initrd  /{PLACE}/initramfs\n"
                f"options {cmdline}\n"
            )
            (path,) = self.destinations()
            context.write(path, entry)
            return
        # Both GRUBs read their own `custom.cfg`, and a UEFI one is reached
        # by `--bootnext` into GRUB rather than into the entry directly.
        _write_custom(context, self.target, cmdline, place, bypass=self.bypass)

    def _cmdline(self, context: Context, place: PurePosixPath) -> tuple[str, ...]:
        if self.mode is MemoryMode.RAM:
            image = _only_image(context, STAGING, ".iso")
            label = _volume_label(context, image)
            words = [
                f"root=live:CDLABEL={label}",
                *CJK_CMDLINE,
                # The path as it is: `STAGING` is at the root of the root
                # filesystem, and iso-scan mounts each `by-uuid` device and
                # looks for that path inside it.
                f"iso-scan/filename={image}",
                *_inherited_consoles(context),
            ]
            if self.needs_ssh:
                # `autoconfig` starts sshd only for `dosshd`; secrets arrive in the payload.
                words.append("dosshd")
            return tuple(words)
        base = ALPINE_MIRRORS[self.region]
        archive = _only_image(context, STAGING, ".tar.gz")
        words = [
            f"alpine_repo={ALPINE_REPOSITORY.format(base=base)}",
            f"modloop={_alpine_modloop(archive, self.region)}",
            f"apkovl=/{APKOVL}",
            "ip=dhcp",
            *_inherited_consoles(context),
        ]
        if self.needs_ssh:
            # `pkgs=` is a fixed value. The payload supplies credentials and
            # the service link that starts OpenSSH after Alpine installs it.
            words.append("pkgs=openssh")
        return tuple(words)


@dataclass(frozen=True, kw_only=True)
class ArmOneShot(Operation):
    """Tell this machine to boot the entry once and return to its own.

    One boot, not a new default: a memory environment that does not come up
    has to leave a machine that still boots. `--bypass` is the other operation
    and says what it costs.
    """

    stage: Stage = Stage.BOOTLOADER
    target: BootTarget

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "arm one boot into the memory environment with {}", (
            self.target.method.value,
        )

    def apply(self, context: Context) -> None:
        if self.target.method is BootMethod.SYSTEMD_BOOT:
            context.run(["bootctl", "set-oneshot", f"{PLACE}.conf"])
            return
        context.run(["grub-reboot", ENTRY_LABEL])


@dataclass(frozen=True, kw_only=True)
class ReplaceDefaultBoot(Operation):
    """`--bypass`: make the memory environment the default rather than a guest.

    This is the one path where an environment that does not come up leaves a
    machine that does not boot at all, so it is never a fallback something
    else selects: firmware that drops an `efibootmgr --create-only` write, an
    NVRAM entry lost to a reset rather than a clean shutdown, and a read-only
    `grubenv` are all cases where the one-shot is written and ignored, and the
    operator asks for this having seen that happen.
    """

    stage: Stage = Stage.BOOTLOADER
    target: BootTarget

    def destinations(self) -> tuple[PurePosixPath, ...]:
        return (self.target.place / BYPASS_RECORD,)

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        (record,) = self.destinations()
        return (
            "replace the default boot entry with the memory environment ({}) and "
            "write {}, which is what lets a disarm take the replacement back",
            (self.target.method.value, str(record)),
        )

    def apply(self, context: Context) -> None:
        context.write(self.target.place / BYPASS_RECORD, f"{self.target.method.value}\n")
        if self.target.method is BootMethod.SYSTEMD_BOOT:
            context.run(["bootctl", "set-default", f"{PLACE}.conf"])
            return
        # `grub-set-default` writes `saved_entry`, which the shipped
        # `grub.cfg` reads only when `GRUB_DEFAULT=saved`. Writing the
        # variable is not enough on a machine whose configuration does not,
        # so the entry is also made the first one `custom.cfg` offers.
        context.run(["grub-set-default", ENTRY_LABEL])


@dataclass(frozen=True, kw_only=True)
class DisarmMemoryBoot(Operation):
    """Undo an arming, for the operator who changed their mind before rebooting."""

    stage: Stage = Stage.FINISH
    target: BootTarget

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "take back the armed boot and delete what it placed", ()

    def apply(self, context: Context) -> None:
        _take_back(context, self.target)


def build(
    *,
    launch: MemoryLaunch,
    target: BootTarget,
    bypass: bool = False,
    configuration: str = "",
    source: str = "",
    keys: tuple[str, ...] = (),
    region: MirrorRegion = MirrorRegion.GLOBAL,
) -> list[Operation]:
    """Every operation that arms one boot into the memory environment.

    Order is the stage order the rest of the installer sorts by: the refusals
    are `PREFLIGHT` so nothing is downloaded onto a machine that cannot boot
    it, the fetch is `STAGE3`, and placing, writing and arming are
    `BOOTLOADER`.
    """
    arming: Operation = (
        ReplaceDefaultBoot(target=target) if bypass else ArmOneShot(target=target)
    )
    access = bool(keys) or bool(launch.ssh_key) or bool(launch.root_password)
    operations: list[Operation] = [
        RefuseWithoutABootMethod(target=target),
        RefuseTooLittleMemory(mode=launch.mode),
        # After the refusals and before the fetch: a machine this refuses is
        # one whose earlier arming is not this run's to take back.
        ClearPreviousArming(target=target),
        FetchMemoryImage(mode=launch.mode, target=target, region=region),
        PlaceMemoryKernel(mode=launch.mode, target=target),
    ]
    if configuration:
        # After the kernel is placed and before the entry names it: the
        # appended segment goes on the initramfs that operation wrote.
        operations.append(
            AppendConfiguration(
                target=target,
                launch=launch,
                configuration=configuration,
                source=source,
                keys=keys,
                access=access,
            )
        )
    operations.append(
        WriteMemoryEntry(
            mode=launch.mode,
            target=target,
            launch=launch,
            region=region,
            bypass=bypass,
            access=access,
        )
    )
    if launch.mode is MemoryMode.LOWRAM:
        operations.append(DiscardTheArchive())
    operations.append(arming)
    return operations


def disarm(*, target: BootTarget) -> list[Operation]:
    """Take back an arming, for the operator who answers no to the reboot.

    A machine left armed reboots into the memory environment the next time
    anything reboots it, which may be months later and for another reason.
    """
    return [DisarmMemoryBoot(target=target)]


def _write_custom(
    context: Context,
    target: BootTarget,
    cmdline: str,
    place: PurePosixPath,
    *,
    bypass: bool = False,
) -> None:
    """A GRUB entry between markers, so arming twice replaces rather than adds."""
    if target.grub_directory is None:
        raise PreflightFailed(
            "this machine boots with GRUB and no GRUB directory was found, "
            "so there is nowhere an entry would be read from"
        )
    custom = PurePosixPath(target.grub_directory) / "custom.cfg"
    # Relative to the filesystem GRUB's `root` names, which is the partition
    # holding `/boot` when that is a mount and the root filesystem when it is
    # a directory. Written the first way on a machine of the second kind, the
    # `search` matches nothing, `root` is left alone, and the entry stops in
    # GRUB with the kernel not found.
    # Where the kernel actually is, as the filesystem holding it names it. A
    # UEFI GRUB reads it off the esp, so the path is relative to the esp and
    # `grub_prefix` \u2014 which describes `/boot` on the root filesystem \u2014 would
    # send `search` after a directory that exists nowhere: the entry then
    # stopped in GRUB with `you need to load the kernel first.`
    at = (
        f"/{PLACE}"
        if target.method is BootMethod.UEFI_GRUB
        else f"{target.grub_prefix}/{PLACE}"
    )
    # Guarded, because GRUB answers a missing file with `error: file \u2026 not
    # found.` and `Press any key to continue...`: a machine armed from far away
    # then waits at a menu nobody is at. `$prefix` carries its own device, so
    # rereading the machine's own configuration works whatever `search` set.
    # `grub-set-default` writes `saved_entry`, which a machine whose
    # configuration says `GRUB_DEFAULT=0` never reads: a `--bypass` guest
    # rebooted straight into `Booting \'Debian GNU/Linux\'` with the installer entry
    # selected in `grubenv`. `custom.cfg` is sourced at the end of `grub.cfg`,
    # after its own `set default`, so this assignment is the one that stands.
    chooses = f"set default='{ENTRY_LABEL}'\n" if bypass else ""
    entry = (
        f"{CUSTOM_BEGIN}\n"
        f"{chooses}"
        # `--unrestricted`, `insmod` and `btrfs_relative_path` are what
        # `bin456789/reinstall` carries for the same job: a GRUB password on a
        # cloud image otherwise asks for one, `all_video` is missing from
        # Fedora's EFI GRUB, and a `/boot` inside a btrfs subvolume resolves
        # every path against the subvolume without the third.
        f"menuentry '{ENTRY_LABEL}' --unrestricted {{\n"
        "    insmod all_video\n"
        "    insmod lvm\n"
        "    set btrfs_relative_path=n\n"
        f"    search --no-floppy --set=root --file {at}/kernel\n"
        f"    if [ -f {at}/kernel -a -f {at}/initramfs ]; then\n"
        f"        linux {at}/kernel {cmdline}\n"
        f"        initrd {at}/initramfs\n"
        f"    else\n"
        f"        configfile $prefix/grub.cfg\n"
        f"    fi\n"
        f"}}\n"
        f"{CUSTOM_END}\n"
    )
    context.write(custom, _without_previous(context.read(custom)) + entry)


def _without_previous(text: str) -> str:
    """The file with any earlier entry of this installer removed, markers included."""
    if CUSTOM_BEGIN not in text:
        return text
    before, _, rest = text.partition(CUSTOM_BEGIN)
    if CUSTOM_END not in rest:
        # `partition` answers an empty tail for a marker that is not there, so
        # an interrupted write took everything after the opening marker with
        # it, including entries the operator wrote.
        raise PreflightFailed(
            f"{CUSTOM_BEGIN} in the GRUB custom configuration has no "
            f"{CUSTOM_END} after it; remove the partial entry by hand"
        )
    _, _, after = rest.partition(CUSTOM_END)
    return before + after.lstrip("\n")


def _only_image(context: Context, place: PurePosixPath, suffix: str) -> PurePosixPath:
    """The one file of that kind in the directory this plan owns."""
    said = context.run(["ls", "--almost-all", str(place)], check=False)
    names = [one for one in said.split() if one.endswith(suffix)]
    if len(names) != 1:
        raise DownloadFailed(
            f"{place} holds {len(names)} files ending {suffix} and this needs one"
        )
    return place / names[0]


def _inherited_consoles(context: Context) -> tuple[str, ...]:
    """The `console=` words this machine is running with, in their own order.

    Written by neither environment and guessed by neither: the machine boots
    today, so what it boots with is the evidence of which console answers
    there. `fixinittab` on the CJK medium auto-detects `hvc0`, `ttyHV0` and
    `ttyAMA0` only, and comments the medium's own `s0` line out, so an amd64
    machine reached over `ttyS0` gets no getty at all unless `console=` names
    it; on arm the auto-detect covers it, which is why one architecture would
    have hidden this from the other.
    """
    said = context.run(["cat", RUNNING_CMDLINE], check=False)
    if isinstance(said, CommandOutput) and said.returncode != 0:
        return ()
    return tuple(
        word
        for word in said.split()
        if word.startswith("console=") and word not in SILENT_CONSOLES
    )



def _alpine_modloop(
    archive: PurePosixPath, region: MirrorRegion = MirrorRegion.GLOBAL
) -> str:
    """The modloop belonging to the netboot archive this run downloaded."""
    named = ALPINE_ARCHIVE.match(archive.name)
    if named is None:
        raise DownloadFailed(
            f"{archive.name} is not named as an Alpine netboot archive, so the "
            "modloop its kernel needs cannot be composed from it"
        )
    return ALPINE_MODLOOP.format(
        base=ALPINE_MIRRORS[region],
        flavour=_alpine_flavour(archive),
        **named.groupdict(),
    )


def _alpine_flavour(archive: PurePosixPath) -> str:
    """Which kernel to take out of this netboot archive.

    The architecture is in the archive's own name, so the flavour is derived
    from what was downloaded rather than from what the machine says it is.
    """
    named = ALPINE_ARCHIVE.match(archive.name)
    if named is None:
        raise DownloadFailed(
            f"{archive.name} is not named as an Alpine netboot archive, so the "
            "kernel inside it cannot be named either"
        )
    architecture = named.group("architecture")
    flavour = ALPINE_FLAVOUR.get(architecture)
    if flavour is None:
        known = ", ".join(sorted(ALPINE_FLAVOUR))
        raise DownloadFailed(
            f"Alpine builds no known netboot kernel for {architecture}; "
            f"the architectures this can arm are {known}"
        )
    return flavour


def _volume_label(context: Context, image: PurePosixPath) -> str:
    """`root=live:CDLABEL=` has to be the label the ISO actually carries.

    Read from the file rather than composed from its name: the two agree today
    and a release that changes one without the other boots to a dracut shell
    saying it cannot find the live image.
    """
    said = context.run(
        ["blkid", "--probe", "--match-tag", "LABEL", "--output", "value", str(image)]
    )
    label = said.strip().splitlines()
    if not label or not label[0].strip():
        raise DownloadFailed(f"{image} carries no volume label to boot by")
    return label[0].strip()


def _total_mebibytes(said: str) -> int | None:
    """`free --mebi --total`'s `Total:` row, which is memory plus swap.

    The `Mem:` row is what matters and is the first: swap is not where a live
    image is read into.
    """
    for line in said.splitlines():
        if line.startswith("Mem:"):
            fields = line.split()
            if len(fields) > 1 and fields[1].isdigit():
                return int(fields[1])
    return None


def _cjk_release(context: Context, machine: str) -> tuple[str, str, str]:
    """The newest CJK ISO for this machine: its name, its URL and its SHA-256.

    Chosen by the name the release publishes rather than by a list here, so an
    architecture the project starts building for works without this file
    changing.
    """
    row = architecture_of(machine)
    if row is None:
        known = ", ".join(sorted(one.kernel_name for one in ARCHITECTURES))
        raise DownloadFailed(
            f"{machine} is not an architecture Gentoo names, so no ISO can be "
            f"chosen for it: {known} are"
        )
    named = row.gentoo_name
    from_mirror = _cjk_from_mirror(context, named)
    if from_mirror is not None:
        return from_mirror
    said = context.run(["curl", "--fail", "--location", CJK_RELEASES])
    try:
        release = json.loads(said)
    except json.JSONDecodeError as error:
        raise DownloadFailed(f"the CJK release index is not JSON: {error}") from error
    assets = {
        str(one.get("name", "")): str(one.get("browser_download_url", ""))
        for one in release.get("assets", [])
    }
    iso = next(
        (one for one in assets if one.endswith(".iso") and f"-{named}-" in one), ""
    )
    if not iso:
        raise DownloadFailed(
            f"the newest CJK release publishes no {named} ISO, so --ram cannot "
            f"run on this machine; --lowram is published for it"
        )
    if not assets.get(f"{iso}.sha256"):
        raise DownloadFailed(f"{iso} is published with no companion .sha256")
    digest = context.run(["curl", "--fail", "--location", assets[f"{iso}.sha256"]])
    return iso, assets[iso], _first_word(digest, iso)


def _cjk_from_mirror(context: Context, named: str) -> tuple[str, str, str] | None:
    """The newest build on the mirror, or `None` when it does not answer.

    `None` rather than an exception: a mirror that is down is not a reason to
    stop, and the release index is asked next.
    """
    index = context.run(["curl", "--fail", "--location", CJK_MIRROR], check=False)
    if isinstance(index, CommandOutput) and index.returncode != 0:
        return None
    builds = CJK_BUILD.findall(index)
    if not builds:
        return None
    inside = f"{CJK_MIRROR.rstrip('/')}/{max(builds)}/"
    listed = context.run(["curl", "--fail", "--location", inside], check=False)
    if isinstance(listed, CommandOutput) and listed.returncode != 0:
        return None
    names = CJK_ASSET.findall(listed)
    iso = next((one for one in names if one.endswith(".iso") and f"-{named}-" in one), "")
    if not iso or f"{iso}.sha256" not in names:
        return None
    digest = context.run(["curl", "--fail", "--location", f"{inside}{iso}.sha256"])
    return iso, f"{inside}{iso}", _first_word(digest, iso)


def _alpine_release(
    context: Context, machine: str, region: MirrorRegion = MirrorRegion.GLOBAL
) -> tuple[str, str, str]:
    """The newest Alpine netboot bundle: its name, its URL and its SHA-256.

    `latest-releases.yaml` is read as records separated by `-` at the start of
    a line rather than with a YAML parser, because no YAML parser is in the
    standard library and this file's shape is two levels deep.
    """
    index = ALPINE_RELEASES.format(base=ALPINE_MIRRORS[region], architecture=machine)
    said = context.run(["curl", "--fail", "--location", index])
    for record in said.split("\n-\n"):
        fields = dict(_yaml_pairs(record))
        if fields.get("flavor") != ALPINE_NETBOOT_FLAVOUR:
            continue
        name, digest = fields.get("file", ""), fields.get("sha256", "")
        if not name or not digest:
            break
        base = index.rsplit("/", 1)[0]
        return name, f"{base}/{name}", digest
    raise DownloadFailed(
        f"{index} names no {ALPINE_NETBOOT_FLAVOUR} with a sha256"
    )


def _yaml_pairs(record: str) -> list[tuple[str, str]]:
    return [
        (key.strip(), value.strip().strip('"'))
        for key, _, value in (line.partition(":") for line in record.splitlines())
        if key.strip() and value.strip() and not key.startswith(("#", "-"))
    ]


def _first_word(digest: str, name: str) -> str:
    """The hash out of a `.sha256` file, which is `<hex>  <name>` per line.

    Not the first word of the file: the CJK release publishes `# SHA256 HASH`
    above the hash, and the first `--ram` run to reach the download refused
    with `the checksum published beside \u2026 is not a SHA-256` because it had
    read that comment. The line naming this file is taken when there is one,
    since a file listing several is answered by the wrong hash otherwise.
    """
    lines = [line.split() for line in digest.splitlines()]
    named = [one for one in lines if len(one) >= 2 and one[1].lstrip("*") == name]
    for fields in named or lines:
        if fields and len(fields[0]) == 64:
            return fields[0]
    raise DownloadFailed(f"the checksum published beside {name} is not a SHA-256")
