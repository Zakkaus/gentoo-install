"""Checks that run before anything is written.

Facts come from `probe.py`; nothing here reads the system itself, so the checks
can be run against a described machine. Every failure is collected, because a
user fixing one condition per run learns about the next one a run later.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..errors import DeviceNotFound, PreflightFailed
from ..model.config import Firmware, InstallConfig
from ..model.device import (
    Existing,
    Filesystem,
    FilesystemType,
    Luks,
    MdRaid,
    PartitionTable,
    TableType,
    VolumeGroup,
    ZfsPool,
)
from .probe import Machine, Probe

#: Commands every install needs, whatever the layout is.
ALWAYS: Final[tuple[str, ...]] = ("tar", "gpg", "mount", "umount", "findmnt", "lsblk", "blkid", "chroot")

#: What each part of a layout adds. Derived from the graph, never a second list.
BY_FEATURE: Final[dict[str, tuple[str, ...]]] = {
    "gpt": ("sgdisk", "partprobe", "wipefs"),
    "mbr": ("parted", "partprobe", "wipefs"),
    "luks": ("cryptsetup",),
    "mdraid": ("mdadm",),
    "lvm": ("lvm",),
    "zfs": ("zpool", "zfs", "zgenhostid"),
    FilesystemType.EXT4.value: ("mkfs.ext4",),
    FilesystemType.EXT3.value: ("mkfs.ext3",),
    FilesystemType.EXT2.value: ("mkfs.ext2",),
    FilesystemType.BTRFS.value: ("mkfs.btrfs", "btrfs"),
    FilesystemType.XFS.value: ("mkfs.xfs",),
    FilesystemType.F2FS.value: ("mkfs.f2fs",),
    FilesystemType.VFAT.value: ("mkfs.vfat",),
}

#: Below this, compiling in a tmpfs is what runs the machine out of memory.
TMPFS_MINIMUM: Final[int] = 8 * 1024**3


@dataclass(frozen=True)
class Report:
    fatal: tuple[str, ...]
    warnings: tuple[str, ...]

    def raise_if_fatal(self) -> None:
        if self.fatal:
            raise PreflightFailed(
                "this machine cannot run the install:\n  " + "\n  ".join(self.fatal)
            )


def required_commands(config: InstallConfig) -> frozenset[str]:
    graph = config.disk.graph
    wanted = set(ALWAYS)
    for table in graph.of_type(PartitionTable):
        wanted |= set(BY_FEATURE["gpt" if table.table is TableType.GPT else "mbr"])
    if graph.of_type(Luks):
        wanted |= set(BY_FEATURE["luks"])
    if graph.of_type(MdRaid):
        wanted |= set(BY_FEATURE["mdraid"])
    if graph.of_type(VolumeGroup):
        wanted |= set(BY_FEATURE["lvm"])
    if graph.of_type(ZfsPool):
        wanted |= set(BY_FEATURE["zfs"])
    for filesystem in graph.of_type(Filesystem):
        wanted |= set(BY_FEATURE.get(filesystem.kind.value, ()))
    return frozenset(wanted)


def check(config: InstallConfig, probe: Probe) -> Report:
    wanted = required_commands(config)
    machine = probe.machine(wanted)
    return inspect(config, machine, probe)


def inspect(config: InstallConfig, machine: Machine, probe: Probe) -> Report:
    fatal: list[str] = []
    warnings: list[str] = []

    if not machine.root:
        fatal.append("the installer has to run as root")
    if machine.architecture != "x86_64":
        fatal.append(f"this build installs amd64 and the machine reports {machine.architecture}")

    wants_uefi = config.bootloader.firmware is Firmware.UEFI
    if wants_uefi and not machine.uefi:
        fatal.append("the configuration boots by UEFI and this machine booted by BIOS")
    if not wants_uefi and machine.uefi:
        warnings.append("the configuration boots by BIOS on a machine that booted by UEFI")

    missing = sorted(required_commands(config) - machine.commands)
    if missing:
        fatal.append(f"these commands are missing: {', '.join(missing)}")

    for disk in config.disk.graph.of_type(Existing):
        if not disk.wipe:
            continue
        try:
            path = probe.resolve(disk.id, disk.selector)
        except DeviceNotFound as error:
            fatal.append(str(error))
            continue
        if probe.mounted(Path(path)):
            fatal.append(f"{path} is mounted; the installer will not repartition a disk in use")

    if machine.memory_bytes and machine.memory_bytes < TMPFS_MINIMUM:
        warnings.append(
            f"{machine.memory_bytes // 1024**3} GiB of memory: build in /var/tmp rather than a tmpfs"
        )
    return Report(fatal=tuple(fatal), warnings=tuple(warnings))
