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
    DeviceGraph,
    Existing,
    Filesystem,
    Swap,
    FilesystemType,
    Luks,
    MdRaid,
    Partition,
    PartitionTable,
    TableType,
    VolumeGroup,
    ZfsPool,
)
from ..model.size import DEFAULT_ALIGNMENT, SectorSize, Size
from ..plan.disk import MKFS
from .probe import RELEASE_KEY, Machine, Probe

#: Commands every install needs, whatever the layout is.
ALWAYS: Final[tuple[str, ...]] = (
    "tar", "gpg", "mount", "umount", "findmnt", "lsblk", "blkid", "chroot", "udevadm", "swapon",
)

#: What the menu needs and a configuration file does not: a file carries
#: `password_hash` already, and the menu computes one with `openssl passwd -6`.
MENU_ONLY: Final[tuple[str, ...]] = ("openssl",)

#: What each part of a layout adds. Derived from the graph, never a second list.
BY_FEATURE: Final[dict[str, tuple[str, ...]]] = {
    # `partprobe` is absent here on purpose: it comes from parted, and
    # `RereadPartitionTable` falls back to `blockdev --rereadpt` without it.
    "gpt": ("sgdisk", "blockdev", "wipefs"),
    "mbr": ("parted", "partprobe", "wipefs"),
    "luks": ("cryptsetup",),
    "mdraid": ("mdadm",),
    # The binaries the operations invoke, not the multicall name: a medium
    # carrying lvm without its symlinks passes on `lvm` and dies at `pvcreate`
    # with the disks already partitioned.
    "lvm": ("pvcreate", "vgcreate", "lvcreate"),
    "swap": ("mkswap", "swapoff"),
    # `hostid`, not `zgenhostid`: the host reads its own id and the target
    # writes it, so the tool that writes runs inside the chroot.
    "zfs": ("zpool", "zfs", "hostid"),
}

#: btrfs needs its own tool as well as its mkfs, to make the subvolumes.
EXTRA_FILESYSTEM_COMMANDS: Final[dict[FilesystemType, tuple[str, ...]]] = {
    FilesystemType.BTRFS: ("btrfs",),
}

#: Commands whose busybox applet satisfies `which` and then rejects the flags,
#: with what the output has to say and why the applet will not do.
GNU_ONLY: Final[dict[str, tuple[str, str]]] = {
    "tar": ("GNU tar", "stage3 needs the GNU one for xattrs and capabilities"),
    "mount": (
        "util-linux",
        "the chroot needs --rbind and --make-rslave, which the busybox applet "
        "has no option for at all",
    ),
    "blkid": (
        "util-linux",
        "the fstab UUIDs come from --probe --match-tag, and the busybox applet "
        "takes no options at all",
    ),
    "umount": (
        "util-linux",
        "releasing a half-finished run needs --recursive and --lazy, which the "
        "busybox applet does not have",
    ),
    "swapon": (
        "util-linux",
        "the check that refuses to repartition a disk in use reads --show, "
        "which the busybox applet does not have",
    ),
}

#: `zpool create` refuses anything shorter, and it refuses it after the disk
#: has already been partitioned.
ZFS_PASSPHRASE_MINIMUM: Final[int] = 8

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
    if graph.of_type(Swap):
        wanted |= set(BY_FEATURE["swap"])
    if graph.of_type(ZfsPool):
        wanted |= set(BY_FEATURE["zfs"])
    for filesystem in graph.of_type(Filesystem):
        if not filesystem.create:
            # A reused filesystem is mounted and never made, so `mkfs` for it
            # is not needed; `blkid` is, and ALWAYS already has it.
            continue
        # Taken from the table the operations themselves use, so a filesystem
        # added there can never be missing here.
        wanted.add(MKFS[filesystem.kind][0])
        wanted |= set(EXTRA_FILESYSTEM_COMMANDS.get(filesystem.kind, ()))
    return frozenset(wanted)


def _disks_at_risk(graph: DeviceGraph) -> list[Existing]:
    """Every disk this run writes a partition table on.

    Not `wipe` alone: a table edited in place carries `wipe=False`, and
    deleting an entry from a disk whose other partition is mounted is exactly
    the case the mount check exists for.
    """
    edited = {
        table.disk
        for table in graph.of_type(PartitionTable)
        if table.create or table.remove
    }
    return [
        disk
        for disk in graph.of_type(Existing)
        if disk.wipe or disk.id in edited
    ]


def _busybox_problems(machine: Machine) -> list[str]:
    """Named here rather than discovered when the flag is rejected, which is
    after the disks are partitioned and the archive is downloaded."""
    problems: list[str] = []
    for command, (wanted, reason) in GNU_ONLY.items():
        version = machine.versions.get(command)
        if version is None or wanted in version:
            continue
        problems.append(
            f"{command} is not {wanted} ({version.splitlines()[0][:60]}); {reason}"
        )
    return problems


def _passphrase_problems(config: InstallConfig) -> list[str]:
    """Read every passphrase file before the first disk is touched.

    `zpool create` rejects a short passphrase only once the pool's vdevs have
    been partitioned, which leaves the disk wiped and the install stopped.
    """
    problems: list[str] = []
    graph = config.disk.graph
    encrypted: list[tuple[str, str, int]] = [
        (node.id, node.passphrase_file, 0) for node in graph.of_type(Luks)
    ]
    encrypted += [
        (pool.id, pool.passphrase_file, ZFS_PASSPHRASE_MINIMUM)
        for pool in graph.of_type(ZfsPool)
        if pool.encrypted
    ]
    for device, source, minimum in encrypted:
        if not source:
            problems.append(f"{device} is encrypted but names no passphrase_file")
            continue
        try:
            passphrase = Path(source).read_text().strip("\n")
        except OSError as error:
            problems.append(f"{device}: {source} cannot be read: {error}")
            continue
        if not passphrase:
            problems.append(f"{device}: {source} is empty")
        elif len(passphrase) < minimum:
            problems.append(
                f"{device}: {source} holds {len(passphrase)} characters and zfs takes at "
                f"least {minimum}"
            )
    return problems


def _capacity_problems(config: InstallConfig, probe: Probe) -> list[str]:
    """Whether each table's fixed partitions fit the disk they are on.

    Checked before anything runs, because `sgdisk --new` refuses the partition
    that does not fit only after `wipefs --all` and `sgdisk --zap-all` have
    already destroyed the table that was there. 512-byte sectors: the GPT tail
    is 33 of them, so assuming the smaller sector reserves the smaller tail and
    the check stays on the permissive side of a 4Kn disk.
    """
    graph = config.disk.graph
    problems: list[str] = []
    for table in graph.of_type(PartitionTable):
        disk = graph[table.disk]
        if not isinstance(disk, Existing):
            continue
        try:
            path = probe.resolve(disk.id, disk.selector)
            capacity = probe.disk_bytes(path)
        except DeviceNotFound:
            # Already reported by the loop that resolves every wiped disk.
            continue
        if not capacity:
            # Fatal, not skipped: this table is about to be rewritten, and a
            # capacity the machine would not report is not permission to write
            # it. Skipping let `wipefs` run before `sgdisk` found no room.
            problems.append(
                f"{disk.selector} did not report a size, so the layout on {table.id} "
                "cannot be checked against it"
            )
            continue
        claimed = sum(
            one.size.bytes
            for one in graph.of_type(Partition)
            if one.table == table.id and one.size is not None
        )
        if not table.create:
            # An edited table keeps every partition the configuration does not
            # remove, and their space is claimed as much as a new one's.
            try:
                present = probe.partition_sizes(path)
            except DeviceNotFound:
                problems.append(
                    f"{disk.selector} did not report its partitions, so what {table.id} "
                    "keeps cannot be counted"
                )
                continue
            claimed += sum(
                size for number, size in present.items() if number not in table.remove
            )
        usable = Size(capacity)
        if table.table is TableType.GPT:
            usable = usable.gpt_last_usable(SectorSize(512))
        # The first partition starts at the alignment boundary, not at zero.
        usable = Size(max(0, usable.bytes - DEFAULT_ALIGNMENT))
        if claimed > usable.bytes:
            problems.append(
                f"{disk.selector} holds {Size(capacity)} and {table.id} claims "
                f"{Size(claimed)} in fixed sizes, which does not fit"
            )
    return problems


def check(config: InstallConfig, probe: Probe, target: str = "/mnt/gentoo") -> Report:
    wanted = required_commands(config)
    machine = probe.machine(wanted, judged=GNU_ONLY)
    return inspect(config, machine, probe, target)


def inspect(
    config: InstallConfig, machine: Machine, probe: Probe, target: str = "/mnt/gentoo"
) -> Report:
    config_target = target
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
    if wants_uefi and machine.efi_bits == 32:
        # Fatal rather than a warning: the install would finish and the
        # firmware would then refuse the amd64 executable it was handed.
        fatal.append(
            "this machine booted through 32-bit EFI firmware, which cannot load "
            "the amd64 EFI executables an amd64 install writes"
        )

    missing = sorted(required_commands(config) - machine.commands)
    if missing:
        fatal.append(f"these commands are missing: {', '.join(missing)}")
    fatal += _busybox_problems(machine)

    for disk in _disks_at_risk(config.disk.graph):
        try:
            path = probe.resolve(disk.id, disk.selector)
        except DeviceNotFound as error:
            fatal.append(str(error))
            continue
        if probe.mounted(path, ignoring=str(config_target)):
            fatal.append(f"{path} is mounted; the installer will not repartition a disk in use")

    if config.disk.graph.of_type(ZfsPool):
        # The commands alone are not enough: a medium can carry the userland
        # and no module, and `zpool create` finds that out after the disks are
        # already partitioned.
        unusable = probe.zfs_support()
        if unusable:
            fatal.append(f"{unusable}, and this configuration makes a pool")

    fatal += _passphrase_problems(config)
    fatal += _capacity_problems(config, probe)

    if not machine.release_key:
        # Not fatal: every medium that is not Gentoo's ships no key file, and
        # `fetch` downloads one whose fingerprint still has to match the pin.
        warnings.append(
            f"{RELEASE_KEY} is absent, so the release key is fetched before the stage3 is verified"
        )

    if machine.memory_bytes and machine.memory_bytes < TMPFS_MINIMUM:
        warnings.append(
            f"{machine.memory_bytes // 1024**3} GiB of memory: build in /var/tmp rather than a tmpfs"
        )
    return Report(fatal=tuple(fatal), warnings=tuple(warnings))
