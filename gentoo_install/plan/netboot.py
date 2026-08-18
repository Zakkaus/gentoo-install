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
from ..model.config import BootMethod, MemoryLaunch, MemoryMode
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
ALPINE_RELEASES: Final[str] = (
    "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/{}/"
    "latest-releases.yaml"
)
ALPINE_NETBOOT_FLAVOUR: Final[str] = "alpine-netboot"

#: Alpine's own repository, which its netboot init installs the packages from.
#: `alpine_repo=auto` searches for a `.boot_repository` file and finds none on
#: a machine that booted from a local kernel.
ALPINE_REPOSITORY: Final[str] = (
    "https://dl-cdn.alpinelinux.org/alpine/latest-stable/main"
)

#: Where the kernel modules come from. `modloop=` is not read by
#: `initramfs-init` at all: the reader is `/etc/init.d/modloop` in the booted
#: root, which parses `/proc/cmdline` itself and `wget`s an `http://` value
#: (`openrc/modloop.initd:16,78`). Both fields are filled from the netboot
#: archive this run downloaded rather than written in, so an aarch64 machine
#: composes an aarch64 URL and the modloop is the one built beside the kernel
#: that will be running.
ALPINE_MODLOOP: Final[str] = (
    "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/"
    "{architecture}/netboot-{version}/modloop-lts"
)

#: What Alpine names a netboot archive. `netboot/` beside it holds whatever
#: release is newest, so pinning the version keeps `modules/$(uname -r)` inside
#: the modloop matching the kernel taken out of this archive.
ALPINE_ARCHIVE = re.compile(
    r"^alpine-netboot-(?P<version>.+)-(?P<architecture>[^-]+)\.tar\.gz$"
)

#: What the kernel calls a machine and what Gentoo calls the same one. Two
#: ecosystems name the same architecture differently, the way `fma` and `fma3`
#: do: `uname -m` answers `x86_64` while the ISO is published as
#: `install-amd64-…`. Gentoo's own names are the lines of
#: `profiles/arch.list`. A machine outside this table is refused by name
#: rather than sent to a URL composed from a guess.
GENTOO_ARCHITECTURES: Final[dict[str, str]] = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "i686": "x86",
}

#: One directory for everything this arms, on the filesystem the bootloader
#: reads. Named rather than scattered, because disarming has to be able to
#: delete exactly what arming wrote.
PLACE: Final[str] = "gentoo-install-ram"

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
    first one's arming in place, and the next reboot — months later, for
    another reason — enters a memory environment carrying a configuration
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
        _disarm(context, self.target)
        context.run(["rm", "--recursive", "--force", str(self.target.place)], check=False)


def _disarm(context: Context, target: BootTarget) -> None:
    """Clear the one-shot this module sets, whichever way it was set.

    `bootctl set-oneshot ""` and not a named entry: `bootctl(1)` says an empty
    ID unsets the variable, while any other value is another entry to boot
    next. This asked for `auto-reboot-to-firmware-setup`, which is not a
    disarm — it is a machine that reboots into its firmware setup instead.
    """
    if target.method is BootMethod.SYSTEMD_BOOT:
        context.run(["bootctl", "set-oneshot", ""], check=False)
        return
    if target.grub_directory is not None:
        context.run(
            ["grub-editenv", f"{target.grub_directory}/grubenv", "unset", "next_entry"],
            check=False,
        )


@dataclass(frozen=True, kw_only=True)
class FetchMemoryImage(Operation):
    """Download the image and check it against the checksum its publisher gives.

    A wrong image is discovered at the next boot, when the machine the
    operator was logged into is no longer answering.
    """

    stage: Stage = Stage.STAGE3
    mode: MemoryMode
    target: BootTarget

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "fetch the {} image and verify its checksum", (self.mode.value,)

    def apply(self, context: Context) -> None:
        place = self.target.place
        context.run(["mkdir", "--parents", str(place)])
        name, url, checksum = (
            _cjk_release(context, self.target.architecture)
            if self.mode is MemoryMode.RAM
            else _alpine_release(context, self.target.architecture)
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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "unpack the {} kernel and initramfs into {}", (
            self.mode.value,
            str(self.target.place),
        )

    def apply(self, context: Context) -> None:
        place = self.target.place
        if self.mode is MemoryMode.RAM:
            image = _only_image(context, place, ".iso")
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
        archive = _only_image(context, place, ".tar.gz")
        for member, name in (
            ("boot/vmlinuz-lts", "kernel"),
            ("boot/initramfs-lts", "initramfs"),
        ):
            context.run(
                [
                    "tar",
                    "--extract",
                    "--file",
                    str(archive),
                    "--directory",
                    str(place),
                    "--transform",
                    f"s|.*|{name}|",
                    member,
                ]
            )


#: Where the payload lands inside the initramfs, and where the hook puts it
#: in the live system. One name, because the hook that copies it and the
#: installer that reads it are written apart.
PAYLOAD: Final[str] = "/gentoo-install"

#: Where the first screen is hung, which is the login shell rather than a
#: boot service. OpenRC's `local` runs `local.d/*.start` with
#: `> /dev/null 2>&1` unless `rc_verbose` is set — read from that medium's own
#: `/etc/init.d/local` — and it runs during boot with no controlling
#: terminal, so a question printed there is invisible and the `read` beside it
#: answers itself. `/root/.bash_profile` runs for the medium's console
#: auto-login and for an ssh login, which is where the operator is; root's
#: shell there is `/bin/bash` and the file already exists.
AUTOSTART: Final[str] = "/root/.bash_profile"


@dataclass(frozen=True, kw_only=True)
class AppendConfiguration(Operation):
    """Put the configuration and the keys inside the initramfs.

    A newc cpio appended to the compressed initramfs is unpacked by the kernel
    and its hooks are run by dracut, which was measured on 2026-08-18 by
    booting one: a `cmdline` hook in the appended segment printed its marker at
    3.5 seconds. So a 1.5 KiB segment delivers everything rather than the whole
    55 MiB image being repacked. `lsinitrd` does not list the appended segment,
    so it cannot be used to check this.
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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "put the installer, the configuration and {} key(s) inside the initramfs", (
            str(len(self.keys)),
        )

    def apply(self, context: Context) -> None:
        staging = self.target.place / "payload"
        inside = PurePosixPath(str(staging) + PAYLOAD)
        context.run(["mkdir", "--parents", str(inside)])
        context.write(inside / "config.toml", self.configuration)
        if self.keys:
            context.write(
                inside / "authorized_keys",
                "".join(f"{one}\n" for one in self.keys),
                mode=0o600,
            )
        # `bootstrap.sh` runs the tree beside it, so both go in together.
        context.run(
            [
                "sh",
                "-c",
                f"cd {self.source} && tar --create --exclude=__pycache__ "
                f"gentoo_install bootstrap.sh | tar --extract --directory {inside}",
            ]
        )
        context.write(inside / "start.sh", _start(), mode=0o755)
        hooks = staging / "usr/lib/dracut/hooks/pre-pivot"
        context.run(["mkdir", "--parents", str(hooks)])
        context.write(hooks / "99-gentoo-install.sh", _handover(), mode=0o755)
        # `find | cpio` from inside the staging directory, or every path in the
        # archive carries the staging prefix and lands nowhere the hook looks.
        context.run(
            [
                "sh",
                "-c",
                f"cd {staging} && find . | cpio --create --format=newc "
                f">> {self.target.place / 'initramfs'}",
            ]
        )
        context.run(["rm", "--recursive", "--force", str(staging)])


def _start() -> str:
    """What the operator's login shell runs. It asks before it erases anything.

    Sourced by `.bash_profile` rather than executed by a boot service, so it
    must not `exit`: that would end the login shell it is running in and drop
    the operator straight back to a prompt they cannot use. `return` ends a
    sourced file and nothing else.

    Two answers and no timeout: a countdown that ends in a partitioned disk is
    a countdown nobody can lose safely, and this path is reached by rebooting
    a machine whose operator may still be reconnecting to it.
    """
    return (
        "#!/bin/sh\n"
        f"cd {PAYLOAD} || return 0\n"
        "printf '\\n'\n"
        "printf 'gentoo-install is in memory. The disk has not been touched.\\n'\n"
        "printf '  install  reinstall this machine from the delivered configuration\\n'\n"
        "printf '  shell    leave this and use the live system as a rescue medium\\n'\n"
        "printf 'install or shell> '\n"
        "read answer\n"
        'case "$answer" in\n'
        # `sh`, not `exec sh`: this is sourced, and `exec` would replace the
        # operator's login shell with the installer and leave them with no
        # shell when it ends.
        f"install) sh ./bootstrap.sh --config {PAYLOAD}/config.toml ;;\n"
        "*) printf 'nothing was changed\\n' ;;\n"
        "esac\n"
    )


def _handover() -> str:
    """What runs in the initramfs to hand the payload to the live system.

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
        # Appended, not written: the file exists on the medium and sources
        # `.bashrc`, and replacing it would take the prompt and the aliases
        # with it.
        f'printf \'\\n[ -f {PAYLOAD}/start.sh ] && . {PAYLOAD}/start.sh\\n\' '
        f'>> "$NEWROOT{AUTOSTART}"\n'
    )


@dataclass(frozen=True, kw_only=True)
class WriteMemoryEntry(Operation):
    """Write the entry the armed boot selects, in this machine's own format."""

    stage: Stage = Stage.BOOTLOADER
    mode: MemoryMode
    target: BootTarget
    launch: MemoryLaunch

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "write a {} entry for the {} environment", (
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
            context.write(
                PurePosixPath(str(self.target.esp_mountpoint))
                / "loader"
                / "entries"
                / f"{PLACE}.conf",
                entry,
            )
            return
        # Both GRUBs read their own `custom.cfg`, and a UEFI one is reached
        # by `--bootnext` into GRUB rather than into the entry directly.
        _write_custom(context, self.target, cmdline, place)

    def _cmdline(self, context: Context, place: PurePosixPath) -> tuple[str, ...]:
        if self.mode is MemoryMode.RAM:
            image = _only_image(context, place, ".iso")
            label = _volume_label(context, image)
            words = [
                f"root=live:CDLABEL={label}",
                *CJK_CMDLINE,
                f"iso-scan/filename={_relative_to_mount(self.target, image)}",
                *_inherited_consoles(context),
            ]
            if self.launch.ssh_key or self.launch.root_password:
                # `/etc/init.d/autoconfig:146,242` schedules sshd for `dosshd`
                # and for nothing else, and the medium's default runlevel has
                # no sshd in it. A key copied to `/root/.ssh` by the initramfs
                # hook reaches a machine with no daemon listening without this.
                words.append("dosshd")
            if self.launch.root_password:
                # The medium scrambles root's password when it starts sshd
                # without one (`autoconfig:551-555`), so a password login needs
                # this and a key login does not.
                words.append(f"passwd={self.launch.root_password}")
            return tuple(words)
        words = [
            f"alpine_repo={ALPINE_REPOSITORY}",
            f"modloop={_alpine_modloop(_only_image(context, place, '.tar.gz'))}",
            "ip=dhcp",
            *_inherited_consoles(context),
        ]
        if self.launch.ssh_key:
            # Alpine's netboot init installs openssh and enables sshd when
            # this is set; where the key text lands is the booted system's
            # `firstboot`, which is why the installer writes it as well.
            words.append(f"ssh_key={self.launch.ssh_key}")
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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "replace the default boot entry with the memory environment ({})", (
            self.target.method.value,
        )

    def apply(self, context: Context) -> None:
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
        _disarm(context, self.target)
        context.run(["rm", "--recursive", "--force", str(self.target.place)], check=False)


def build(
    *,
    launch: MemoryLaunch,
    target: BootTarget,
    bypass: bool = False,
    configuration: str = "",
    source: str = "",
    keys: tuple[str, ...] = (),
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
    operations: list[Operation] = [
        RefuseWithoutABootMethod(target=target),
        RefuseTooLittleMemory(mode=launch.mode),
        # After the refusals and before the fetch: a machine this refuses is
        # one whose earlier arming is not this run's to take back.
        ClearPreviousArming(target=target),
        FetchMemoryImage(mode=launch.mode, target=target),
        PlaceMemoryKernel(mode=launch.mode, target=target),
    ]
    if configuration:
        # After the kernel is placed and before the entry names it: the
        # appended segment goes on the initramfs that operation just wrote.
        operations.append(
            AppendConfiguration(
                target=target,
                launch=launch,
                configuration=configuration,
                source=source,
                keys=keys,
            )
        )
    operations += [
        WriteMemoryEntry(mode=launch.mode, target=target, launch=launch),
        arming,
    ]
    return operations


def disarm(*, target: BootTarget) -> list[Operation]:
    """Take back an arming, for the operator who answers no to the reboot.

    A machine left armed reboots into the memory environment the next time
    anything reboots it, which may be months later and for another reason.
    """
    return [DisarmMemoryBoot(target=target)]


def _write_custom(
    context: Context, target: BootTarget, cmdline: str, place: PurePosixPath
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
    at = f"{target.grub_prefix}/{PLACE}"
    entry = (
        f"{CUSTOM_BEGIN}\n"
        f"menuentry '{ENTRY_LABEL}' {{\n"
        f"    search --no-floppy --set=root --file {at}/kernel\n"
        f"    linux {at}/kernel {cmdline}\n"
        f"    initrd {at}/initramfs\n"
        f"}}\n"
        f"{CUSTOM_END}\n"
    )
    context.write(custom, _without_previous(context.read(custom)) + entry)


def _without_previous(text: str) -> str:
    """The file with any earlier entry of ours removed, markers included."""
    if CUSTOM_BEGIN not in text:
        return text
    before, _, rest = text.partition(CUSTOM_BEGIN)
    _, _, after = rest.partition(CUSTOM_END)
    return before + after.lstrip("\n")


def _relative_to_mount(target: BootTarget, image: PurePosixPath) -> str:
    """`iso-scan/filename` is relative to the filesystem the file is on.

    iso-scan mounts each `/dev/disk/by-uuid/*` and looks for that path inside
    it, so an absolute path that includes the mount point finds nothing.
    """
    if target.method is BootMethod.BIOS_GRUB or target.esp_mountpoint is None:
        return f"/{image.relative_to('/boot')}" if _under(image, "/boot") else str(image)
    return f"/{image.relative_to(target.esp_mountpoint)}"


def _under(path: PurePosixPath, parent: str) -> bool:
    return str(path).startswith(parent.rstrip("/") + "/")


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
    return tuple(word for word in said.split() if word.startswith("console="))


def _alpine_modloop(archive: PurePosixPath) -> str:
    """The modloop belonging to the netboot archive this run downloaded."""
    named = ALPINE_ARCHIVE.match(archive.name)
    if named is None:
        raise DownloadFailed(
            f"{archive.name} is not named as an Alpine netboot archive, so the "
            "modloop its kernel needs cannot be composed from it"
        )
    return ALPINE_MODLOOP.format(**named.groupdict())


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
    named = GENTOO_ARCHITECTURES.get(machine)
    if named is None:
        raise DownloadFailed(
            f"{machine} is not an architecture Gentoo names, so no ISO can be "
            f"chosen for it: {', '.join(sorted(GENTOO_ARCHITECTURES))} are"
        )
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


def _alpine_release(context: Context, machine: str) -> tuple[str, str, str]:
    """The newest Alpine netboot bundle: its name, its URL and its SHA-256.

    `latest-releases.yaml` is read as records separated by `-` at the start of
    a line rather than with a YAML parser, because no YAML parser is in the
    standard library and this file's shape is two levels deep.
    """
    index = ALPINE_RELEASES.format(machine)
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
    """A `.sha256` file is `<hex>  <name>`, and taking the whole line compares
    a hash against a hash and a filename."""
    fields = digest.split()
    if not fields or len(fields[0]) != 64:
        raise DownloadFailed(f"the checksum published beside {name} is not a SHA-256")
    return fields[0]
