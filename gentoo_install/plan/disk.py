# SPDX-License-Identifier: GPL-2.0-or-later
"""Disks: partition tables, arrays, filesystems and mounts.

The device graph decides the order. Nodes are emitted in topological order so a
node is always built after everything it is built from, and the operations are
then grouped by stage, because every partition has to exist before the first
`mkfs` runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Final, Protocol, cast

from ..errors import CommandFailed, ConfigError, GentooInstallError, InvalidLayout
from ..model.config import DiskMode, InstallConfig
from ..model.device import (
    dataset_name,
    md_path,
    mapper_path,
    DeviceGraph,
    DeviceId,
    Existing,
    Extent,
    Filesystem,
    FilesystemType,
    LogicalVolume,
    Luks,
    MdRaid,
    Mountpoint,
    Node,
    Partition,
    PartitionRole,
    PartitionTable,
    RaidLevel,
    RaidMetadata,
    StorageFacts,
    Subvolume,
    Swap,
    T,
    TableType,
    VolumeGroup,
    ZfsDataset,
    ZfsPool,
    ZfsTopology,
)
from ..model.size import DEFAULT_ALIGNMENT, SectorSize, Size
from .mounts import ResolvedMount, resolve_mounts
from .operations import CommandOutput, Context, Operation, Stage, answered

#: GPT type codes as `sgdisk --typecode` spells them.
TYPE_CODES: Final[dict[PartitionRole, str]] = {
    PartitionRole.ESP: "ef00",
    PartitionRole.BIOS_BOOT: "ef02",
    PartitionRole.SWAP: "8200",
    PartitionRole.RAID: "fd00",
    PartitionRole.LVM: "8e00",
    # Solaris root, which is the type OpenZFS on Linux gives a pool member.
    PartitionRole.ZFS: "bf00",
    PartitionRole.DATA: "8300",
}

#: `mkfs` for each filesystem, with the flags that make the result predictable.
#: Where the stage3 is downloaded, relative to the target root. Named here
#: because `exec/apply.py` writes it and `DiscardStage3` deletes it.
STAGE3_CACHE: Final[str] = "var/cache/gentoo-install"

@dataclass(frozen=True)
class MakeCommand:
    """How one filesystem is made, and how it is told its label.

    One entry rather than two tables keyed the same way: `MakeFilesystem`
    indexes both on the same line, so a filesystem added to one and not the
    other raises `KeyError` while the disk is already partitioned.
    """

    argv: tuple[str, ...]
    label_option: str


#: `-L` everywhere except vfat, which spells its label option `-n`, and f2fs,
#: which spells it `-l`.
MKFS: Final[dict[FilesystemType, MakeCommand]] = {
    FilesystemType.EXT2: MakeCommand(("mkfs.ext2", "-F"), "-L"),
    FilesystemType.EXT3: MakeCommand(("mkfs.ext3", "-F"), "-L"),
    FilesystemType.EXT4: MakeCommand(("mkfs.ext4", "-F"), "-L"),
    FilesystemType.BTRFS: MakeCommand(("mkfs.btrfs", "-f"), "-L"),
    FilesystemType.XFS: MakeCommand(("mkfs.xfs", "-f"), "-L"),
    FilesystemType.F2FS: MakeCommand(("mkfs.f2fs", "-f"), "-l"),
    FilesystemType.VFAT: MakeCommand(("mkfs.vfat", "-F", "32"), "-n"),
}

#: The first partition starts here, which is also the alignment every later one
#: is rounded up to.
FIRST_OFFSET: Final[Size] = Size(DEFAULT_ALIGNMENT)

#: Pool properties, taken from what the Live ISO's Calamares configuration
#: creates, so a pool this installer makes matches one the GUI installer makes.
POOL_OPTIONS: Final[tuple[str, ...]] = (
    "-o", "ashift=12",
    "-o", "autotrim=on",
    "-O", "mountpoint=none",
    "-O", "acltype=posixacl",
    "-O", "relatime=on",
)

#: Dataset properties. `dnodesize=auto` is what makes `xattr=sa` usable: a
#: system attribute needs a dnode large enough to hold it.
DATASET_OPTIONS: Final[tuple[str, ...]] = (
    "-o", "compression=lz4",
    "-o", "atime=off",
    "-o", "xattr=sa",
    "-o", "dnodesize=auto",
)


@dataclass(frozen=True, kw_only=True)
class ReleaseTarget(Operation):
    """Take apart what a previous run of this configuration left behind.

    An install that stopped halfway leaves the target mounted, containers open
    and arrays assembled, and every one of those makes the disk busy so the
    rerun fails at `wipefs`. A missing container is expected on a first run;
    an open one after its close command fails is not.
    """

    stage: Stage = Stage.PARTITION
    #: One command per device, already in the order they have to run: whatever
    #: is built on top comes first. Derived from the graph rather than from a
    #: fixed sequence of kinds, because LVM on LUKS and LUKS on LVM need
    #: opposite orders and both are describable.
    steps: tuple[tuple[str, ...], ...]

    def required_host_commands(self) -> frozenset[str]:
        commands = {"umount", "findmnt", "sleep", *(step[0] for step in self.steps)}
        for argv in self.steps:
            if argv[:2] in {("vgchange", "--activate"), ("cryptsetup", "close")}:
                commands.add("dmsetup")
        return frozenset(commands)

    def describe(self) -> str:
        return "release anything a previous run of this configuration left mounted or open"

    def apply(self, context: Context) -> None:
        self._unmount(context, context.target)
        self._unmount(context, context.target.parent / SCRATCH_MOUNT)
        for argv in self.steps:
            self._close(context, argv)

    def _unmount(self, context: Context, path: PurePosixPath) -> None:
        context.run(["umount", "--recursive", str(path)], check=False)
        if not self._still_mounted(context, path):
            return
        context.run(["umount", "--recursive", "--lazy", str(path)])
        if self._still_mounted(context, path):
            raise CommandFailed(f"{path} remains mounted after lazy unmount")

    def _still_mounted(self, context: Context, path: PurePosixPath) -> bool:
        found = context.run(["findmnt", "--mountpoint", str(path)], check=False)
        if not isinstance(found, CommandOutput):
            raise CommandFailed(f"cannot determine whether {path} is mounted")
        # 1 is `findmnt` saying the path is not a mountpoint; anything else is
        # the probe failing, and reading that as unmounted skipped the lazy
        # unmount and let the wipe run against a target still mounted.
        if found.returncode not in _FINDMNT_ANSWERED:
            raise CommandFailed(
                f"whether {path} is mounted could not be read: {str(found).strip()[:200]}"
            )
        return found.returncode == 0

    def _close(self, context: Context, argv: tuple[str, ...]) -> None:
        last: CommandOutput | None = None
        for attempt in range(RELEASE_TRIES):
            result = context.run(list(argv), check=False)
            if not isinstance(result, CommandOutput):
                raise CommandFailed(f"cannot determine whether {' '.join(argv)} completed")
            if result.returncode == 0 or self._is_released(context, argv):
                return
            last = result
            if attempt + 1 < RELEASE_TRIES:
                context.run(["sleep", f"{RELEASE_PAUSE:g}"])
        assert last is not None
        raise CommandFailed(
            f"{' '.join(argv)} left its device open after {RELEASE_TRIES} attempts: "
            f"{last.strip() or 'no output'}"
        )

    def _is_released(self, context: Context, argv: tuple[str, ...]) -> bool:
        if argv[:2] == ("zpool", "export"):
            listed = self._state(context, ["zpool", "list", "-H", "-o", "name"])
            return argv[2] not in listed.split()
        if argv[:2] == ("vgchange", "--activate"):
            listed = self._state(context, ["dmsetup", "ls"])
            # device-mapper doubles hyphens, so the final hyphen keeps group
            # names that share a prefix distinct.
            prefix = f"{argv[3].replace('-', '--')}-"
            return not any(
                line.split() and line.split()[0].startswith(prefix)
                for line in listed.splitlines()
            )
        if argv[:2] == ("cryptsetup", "close"):
            listed = self._state(context, ["dmsetup", "ls", "--target", "crypt"])
            return not any(line.split() and line.split()[0] == argv[2] for line in listed.splitlines())
        if argv[:2] == ("mdadm", "--stop"):
            listed = self._state(context, ["mdadm", "--detail", "--scan"])
            return argv[2] not in listed
        raise ConfigError(f"no release state check for {' '.join(argv)}")

    def _state(self, context: Context, argv: list[str]) -> CommandOutput:
        result = context.run(argv, check=False)
        if not isinstance(result, CommandOutput) or result.returncode != 0:
            raise CommandFailed(f"cannot determine release state with {' '.join(argv)}")
        return result


class _ImageContext(Protocol):
    def remember_image_device(self, device: DeviceId, path: str) -> None: ...

    def image_device_path(self, device: DeviceId) -> str | None: ...

    def release_image_device(self, device: DeviceId) -> None: ...


@dataclass(frozen=True, kw_only=True)
class CreateImage(Operation):
    """Create a sparse file and attach it as the graph's whole disk."""

    stage: Stage = Stage.PARTITION
    host_commands = ("test", "truncate", "losetup")
    device: DeviceId
    image: str
    size: Size
    wipe: bool

    @property
    def survives_a_reboot(self) -> bool:
        return False

    def describe(self) -> str:
        if self.wipe:
            return (
                f"create a {self.size} sparse image at {self.image}, overwriting an existing image, "
                "and attach it as a loop device"
            )
        return (
            f"create a {self.size} sparse image at {self.image}, refusing to overwrite an existing "
            "image, and attach it as a loop device"
        )

    def apply(self, context: Context) -> None:
        attached = context.run(
            ["losetup", "--associated", "--noheadings", "--output", "NAME", self.image],
            check=False,
        )
        if not isinstance(attached, CommandOutput) or attached.returncode != 0:
            raise CommandFailed(f"cannot determine whether {self.image} is already attached")
        loops = tuple(line.strip() for line in attached.splitlines() if line.strip())
        if len(loops) == 1:
            cast(_ImageContext, context).remember_image_device(self.device, loops[0])
            return
        if len(loops) > 1:
            raise ConfigError(
                f"{self.image} is attached to multiple loop devices: {', '.join(loops)}"
            )
        if not self.wipe:
            exists = context.run(["test", "-e", self.image], check=False)
            if not isinstance(exists, CommandOutput):
                raise CommandFailed(f"cannot determine whether {self.image} already exists")
            if exists.returncode == 0:
                raise ConfigError(f"{self.image} already exists; set disk.wipe = true to overwrite it")
            if exists.returncode != 1:
                raise CommandFailed(f"cannot determine whether {self.image} already exists")
        context.run(["truncate", "--size", str(self.size.bytes), self.image])
        loop = context.run(["losetup", "--find", "--show", "--partscan", self.image]).strip()
        if not loop:
            raise CommandFailed(f"losetup attached {self.image} but returned no loop device")
        cast(_ImageContext, context).remember_image_device(self.device, loop)


@dataclass(frozen=True, kw_only=True)
class DetachImage(Operation):
    """Detach the loop device attached for an image installation."""

    stage: Stage = Stage.FINISH
    host_commands = ("losetup",)
    device: DeviceId
    image: str

    @property
    def releases_the_machine(self) -> bool:
        return True

    def describe(self) -> str:
        return f"detach the loop device for image {self.image}"

    def apply(self, context: Context) -> None:
        image_context = cast(_ImageContext, context)
        loop = image_context.image_device_path(self.device)
        if loop is None:
            return
        context.run(["losetup", "--detach", loop])
        image_context.release_image_device(self.device)


@dataclass(frozen=True, kw_only=True)
class WipeSignatures(Operation):
    """`mkfs` refuses, or worse silently inherits, when an old superblock is left."""

    stage: Stage = Stage.PARTITION
    host_commands = ("wipefs",)
    device: DeviceId

    def describe(self) -> str:
        return f"wipe existing signatures from {self.device}"

    def apply(self, context: Context) -> None:
        context.run(["wipefs", "--all", context.device_path(self.device)])


@dataclass(frozen=True, kw_only=True)
class CreatePartitionTable(Operation):
    """A fresh table, or the entries removed from the one already there.

    `--zap-all` takes every entry with it, so a table holding a partition the
    operator asked to keep is edited rather than written.
    """

    stage: Stage = Stage.PARTITION
    table: DeviceId
    disk: DeviceId
    kind: TableType
    create: bool = True
    remove: tuple[int, ...] = ()

    def required_host_commands(self) -> frozenset[str]:
        return frozenset(("sgdisk" if self.kind is TableType.GPT else "parted",))

    def describe(self) -> str:
        if self.create:
            return f"create a {self.kind.value} partition table on {self.disk} as {self.table}"
        if not self.remove:
            return f"keep the {self.kind.value} partition table on {self.disk} as {self.table}"
        listed = ", ".join(str(one) for one in self.remove)
        return f"delete partition {listed} from {self.disk}, keeping the rest of its table"

    def apply(self, context: Context) -> None:
        path = context.device_path(self.disk)
        if not self.create:
            for index in self.remove:
                # Each tool against its own table. `sgdisk` reads an msdos
                # label, converts it to GPT in memory and writes that back, so
                # deleting one entry with it takes the other operating system
                # on the disk with it.
                if self.kind is TableType.GPT:
                    context.run(["sgdisk", f"--delete={index}", path])
                else:
                    context.run(["parted", "--script", path, "rm", str(index)])
            return
        if self.kind is TableType.GPT:
            context.run(["sgdisk", "--zap-all", path])
        else:
            context.run(["parted", "--script", path, "mklabel", "msdos"])


@dataclass(frozen=True, kw_only=True)
class CreatePartition(Operation):
    stage: Stage = Stage.PARTITION
    partition: DeviceId
    disk: DeviceId
    table_kind: TableType
    index: int
    role: PartitionRole
    #: None means the rest of the disk, which only the last partition may ask for.
    size: Size | None
    label: str
    #: Where this partition starts. Only the MBR path needs it: `sgdisk` places a
    #: partition at the first aligned free sector by itself, `parted` does not.
    start: Size
    #: Where the free extent it was placed in ends, when the table is edited
    #: rather than written. `100%` reaches past a partition the operator asked
    #: to keep, and `parted` then refuses the whole edit.
    limit: Size | None = None

    def required_host_commands(self) -> frozenset[str]:
        return frozenset(("sgdisk" if self.table_kind is TableType.GPT else "parted",))

    def describe(self) -> str:
        if self.size is not None:
            extent = str(self.size)
        elif self.limit is not None:
            # An edited table places the partition in one free gap, so "the
            # rest of the disk" would promise the operator the retained
            # partitions' space as well.
            extent = f"the free space from {self.start} to {self.limit}"
        else:
            extent = "the rest of the disk"
        name = f" labelled {self.label}" if self.label else ""
        placement = f" in the {self.table_kind.value} table"
        if self.table_kind is TableType.MBR:
            placement += f" from {self.start}"
        return (
            f"create partition {self.index} on {self.disk} as {self.partition}{placement}: "
            f"{self.role.value}, {extent}{name}"
        )

    def apply(self, context: Context) -> None:
        path = context.device_path(self.disk)
        if self.table_kind is TableType.GPT:
            end = f"+{self.size.single_letter()}" if self.size is not None else "0"
            argv = [
                "sgdisk",
                f"--new={self.index}:0:{end}",
                f"--typecode={self.index}:{TYPE_CODES[self.role]}",
            ]
            if self.role is PartitionRole.BIOS_BOOT:
                # Attribute 2 marks the partition legacy BIOS bootable, which is
                # what firmware doing a legacy boot from GPT looks for.
                argv.append(f"--attributes={self.index}:set:2")
            if self.label:
                argv.append(f"--change-name={self.index}:{self.label}")
            context.run([*argv, path])
            return
        if self.size is not None:
            end = str(self.start + self.size)
        else:
            end = str(self.limit) if self.limit is not None else "100%"
        context.run(
            ["parted", "--script", "--align", "optimal", path, "mkpart", "primary", str(self.start), end]
        )
        if self.role is PartitionRole.ESP:
            context.run(["parted", "--script", path, "set", str(self.index), "esp", "on"])


@dataclass(frozen=True, kw_only=True)
class RereadPartitionTable(Operation):
    """`sgdisk` returns before the kernel has reread the table, so the partition
    nodes do not exist yet when the next operation asks for one."""

    stage: Stage = Stage.PARTITION
    host_commands = ("blockdev", "udevadm")
    disk: DeviceId

    def describe(self) -> str:
        return f"reread the partition table of {self.disk} and wait for its nodes"

    def apply(self, context: Context) -> None:
        path = context.device_path(self.disk)
        try:
            context.run(["partprobe", path])
        except CommandFailed as error:
            context.degrade("partprobe", f"blockdev rereads the table instead: {error}")
            context.run(["blockdev", "--rereadpt", path])
        # `partprobe` asks the kernel to reread; `partx --update` is what
        # updates the partitions where mdev is the device manager and nothing
        # listens for the event. Undeclared like `partprobe` above and for the
        # same reason: `Probe.wait_for`'s own scan is the fallback without it.
        context.run(["partx", "--update", path], check=False)
        context.run(["udevadm", "settle"])
        context.settle_device_events()


@dataclass(frozen=True, kw_only=True)
class CreateMdRaid(Operation):
    stage: Stage = Stage.ARRAY
    host_commands = ("mdadm",)
    array: DeviceId
    members: tuple[DeviceId, ...]
    level: RaidLevel
    metadata: RaidMetadata
    name: str

    def describe(self) -> str:
        members = ", ".join(self.members)
        return (
            f"create {self.level.value} array {md_path(self.name)} as {self.array} "
            f"from {members}, metadata {self.metadata.value}"
        )

    def apply(self, context: Context) -> None:
        context.run(
            [
                "mdadm",
                "--create",
                md_path(self.name),
                "--run",
                f"--level={self.level.value}",
                f"--metadata={self.metadata.value}",
                f"--raid-devices={len(self.members)}",
                *(context.device_path(member) for member in self.members),
            ]
        )


@dataclass(frozen=True, kw_only=True)
class CreateLuks(Operation):
    stage: Stage = Stage.ARRAY
    host_commands = ("cryptsetup",)
    container: DeviceId
    backing: DeviceId
    name: str

    def describe(self) -> str:
        return f"format {self.backing} as LUKS2 using the key file for {self.container}"

    def apply(self, context: Context) -> None:
        path = context.device_path(self.backing)
        key = str(context.key_file(self.container))
        context.run(
            [
                "cryptsetup", "luksFormat",
                "--type", "luks2",
                "--cipher", "aes-xts-plain64",
                "--hash", "sha512",
                "--pbkdf", "argon2id",
                "--key-size", "512",
                "--batch-mode",
                "--key-file", key,
                path,
            ]
        )


@dataclass(frozen=True, kw_only=True)
class OpenLuks(Operation):
    """Open a container that already exists, or leave an open one alone.

    Separate from `CreateLuks` because the two outlive different things: the
    header is on the disk and a resumed run must not write another, while the
    mapping is process state that a failure cleanup or a reboot takes away.
    Skipping both left `/dev/mapper/root` absent and every later stage
    addressing a target that was never assembled.
    """

    stage: Stage = Stage.ARRAY
    host_commands = ("dmsetup", "cryptsetup")
    container: DeviceId
    backing: DeviceId
    name: str

    def describe(self) -> str:
        return f"open {self.backing} as {mapper_path(self.name)} using the key file for {self.container}"

    @property
    def survives_a_reboot(self) -> bool:
        return False

    def apply(self, context: Context) -> None:
        # `dmsetup ls` exits 0 with `No devices found` when there are none, so
        # its output answers without a second exit code to read.
        listed = context.run(["dmsetup", "ls", "--target", "crypt"])
        if any(line.split() and line.split()[0] == self.name for line in listed.splitlines()):
            return
        context.run(
            [
                "cryptsetup", "open",
                "--type", "luks2",
                "--key-file", str(context.key_file(self.container)),
                context.device_path(self.backing),
                self.name,
            ]
        )


@dataclass(frozen=True, kw_only=True)
class AssembleMdRaid(Operation):
    """Assemble an array that already exists, or leave a running one alone."""

    stage: Stage = Stage.ARRAY
    host_commands = ("mdadm",)
    array: DeviceId
    members: tuple[DeviceId, ...]
    name: str

    def describe(self) -> str:
        return f"assemble {md_path(self.name)} from {', '.join(self.members)}"

    @property
    def survives_a_reboot(self) -> bool:
        return False

    def apply(self, context: Context) -> None:
        listed = context.run(["mdadm", "--detail", "--scan"])
        if md_path(self.name) in listed:
            return
        context.run(
            [
                "mdadm",
                "--assemble",
                md_path(self.name),
                *(context.device_path(member) for member in self.members),
            ]
        )


@dataclass(frozen=True, kw_only=True)
class ActivateVolumeGroup(Operation):
    """Bring the group's volumes up. `vgchange` on an active group is a no-op."""

    stage: Stage = Stage.ARRAY
    host_commands = ("vgchange",)
    group: DeviceId
    name: str

    def describe(self) -> str:
        return f"activate volume group {self.name}"

    @property
    def survives_a_reboot(self) -> bool:
        return False

    def apply(self, context: Context) -> None:
        context.run(["vgchange", "--activate", "y", self.name])


@dataclass(frozen=True, kw_only=True)
class CreateVolumeGroup(Operation):
    stage: Stage = Stage.ARRAY
    host_commands = ("pvcreate", "vgcreate")
    group: DeviceId
    members: tuple[DeviceId, ...]
    name: str

    def describe(self) -> str:
        return f"create volume group {self.name} as {self.group} from {', '.join(self.members)}"

    def apply(self, context: Context) -> None:
        paths = [context.device_path(member) for member in self.members]
        context.run(["pvcreate", "--force", *paths])
        context.run(["vgcreate", self.name, *paths])


@dataclass(frozen=True, kw_only=True)
class CreateLogicalVolume(Operation):
    stage: Stage = Stage.ARRAY
    host_commands = ("lvcreate",)
    volume: DeviceId
    group: str
    name: str
    size: Size | None

    def describe(self) -> str:
        extent = str(self.size) if self.size is not None else "the rest of the group"
        return f"create logical volume {self.group}/{self.name} as {self.volume}: {extent}"

    def apply(self, context: Context) -> None:
        extent = (
            ["--size", self.size.single_letter()]
            if self.size is not None
            else ["--extents", "100%FREE"]
        )
        context.run(["lvcreate", "--yes", *extent, "--name", self.name, self.group])


@dataclass(frozen=True, kw_only=True)
class MakeFilesystem(Operation):
    stage: Stage = Stage.FORMAT
    filesystem: DeviceId
    device: DeviceId
    kind: FilesystemType
    label: str

    def required_host_commands(self) -> frozenset[str]:
        return frozenset((MKFS[self.kind].argv[0],))

    def describe(self) -> str:
        name = f" labelled {self.label}" if self.label else ""
        return f"make a {self.kind.value} filesystem on {self.device} as {self.filesystem}{name}"

    def apply(self, context: Context) -> None:
        argv = list(MKFS[self.kind].argv)
        if self.label:
            argv += [MKFS[self.kind].label_option, self.label]
        context.run([*argv, context.device_path(self.device)])


@dataclass(frozen=True, kw_only=True)
class VerifyFilesystem(Operation):
    """The type already on the device, checked against what was declared.

    `blkid --probe` rather than the cache: the cache holds whatever was there
    before this run, which for a reused partition is the answer this
    check must not trust.
    """

    stage: Stage = Stage.FORMAT
    filesystem: DeviceId
    device: DeviceId
    kind: FilesystemType

    def describe(self) -> str:
        return f"check {self.device} already holds a {self.kind.value} filesystem"

    def apply(self, context: Context) -> None:
        found = context.filesystem_type(self.device)
        if found != self.kind.value:
            raise InvalidLayout(
                f"{self.device} holds {found or 'no filesystem'}, and the configuration "
                f"reuses it as {self.kind.value}"
            )


#: Where a btrfs top level is mounted while its subvolumes are created. Beside
#: the target rather than under it: the layout is mounted on the target itself.
SCRATCH_MOUNT: Final[str] = "btrfs-top"

def _unmount_scratch(
    context: Context, scratch: PurePosixPath, failure: GentooInstallError | None
) -> None:
    # A stale top-level mount makes the next run fail at `wipefs` with a busy
    # device rather than naming the holder that needs cleanup.
    result: str | None = None
    try:
        result = context.run(["umount", str(scratch)], check=False)
    except CommandFailed:
        if failure is None:
            raise
    if failure is None and isinstance(result, CommandOutput) and result.returncode != 0:
        raise CommandFailed(
            f"umount {scratch} exited {result.returncode}: {result.strip() or 'no output'}"
        )


@dataclass(frozen=True, kw_only=True)
class VerifySubvolume(Operation):
    """A subvolume the configuration reuses rather than makes.

    A conversion mounts the running machine's own layout, so `@` is already
    there and `btrfs subvolume create` answers `target path already exists`.
    Checked rather than skipped: a name that is not there is a layout this
    plan cannot mount, and finding that out at the mount is too late.
    """

    stage: Stage = Stage.FORMAT
    host_commands = ("mkdir", "mount", "btrfs", "umount")
    subvolume: DeviceId
    device: DeviceId
    name: str

    def describe(self) -> str:
        return f"check {self.device} already holds the btrfs subvolume {self.name}"

    def apply(self, context: Context) -> None:
        scratch = context.target.parent / SCRATCH_MOUNT
        path = context.device_path(self.device)
        context.run(["mkdir", "--parents", str(scratch)])
        failure: GentooInstallError | None = None
        try:
            context.run(["mount", "--types", "btrfs", path, str(scratch)])
            # `btrfs subvolume list`, not a directory test: a plain directory of
            # the same name is not a subvolume and mounting `subvol=` on it fails
            # at the mount rather than here.
            listed = context.run(["btrfs", "subvolume", "list", str(scratch)], check=False)
            wanted = self.name.lstrip("/")
            if not any(
                line.rsplit(" path ", 1)[-1].strip() == wanted for line in listed.splitlines()
            ):
                raise InvalidLayout(
                    f"{self.device} has no btrfs subvolume {self.name}, and the "
                    "configuration reuses it"
                )
        except GentooInstallError as error:
            failure = error
            raise
        finally:
            _unmount_scratch(context, scratch, failure)


@dataclass(frozen=True, kw_only=True)
class CreateSubvolume(Operation):
    """btrfs subvolumes live inside the filesystem, so the top level has to be
    mounted to create one and unmounted again before the layout is mounted."""

    stage: Stage = Stage.FORMAT
    host_commands = ("mkdir", "mount", "btrfs", "umount")
    subvolume: DeviceId
    device: DeviceId
    name: str

    def describe(self) -> str:
        return f"create btrfs subvolume {self.name} on {self.device} as {self.subvolume}"

    def apply(self, context: Context) -> None:
        scratch = context.target.parent / SCRATCH_MOUNT
        path = context.device_path(self.device)
        context.run(["mkdir", "--parents", str(scratch)])
        failure: GentooInstallError | None = None
        try:
            context.run(["mount", "--types", "btrfs", path, str(scratch)])
            context.run(["btrfs", "subvolume", "create", str(scratch / self.name)])
        except GentooInstallError as error:
            failure = error
            raise
        finally:
            _unmount_scratch(context, scratch, failure)


@dataclass(frozen=True, kw_only=True)
class MakeSwap(Operation):
    """The target's fstab enables this swap, not the installer: swap in use on
    the installing system keeps the device busy."""

    stage: Stage = Stage.FORMAT
    host_commands = ("swapoff", "mkswap")
    swap: DeviceId
    device: DeviceId

    def describe(self) -> str:
        return f"make swap on {self.device} as {self.swap}"

    def apply(self, context: Context) -> None:
        path = context.device_path(self.device)
        # The install medium may have activated an existing swap signature on
        # this partition, and `mkswap` refuses while it is in use.
        context.run(["swapoff", path], check=False)
        context.run(["mkswap", path])


@dataclass(frozen=True, kw_only=True)
class CreateZpool(Operation):
    stage: Stage = Stage.ZFS
    host_commands = ("zpool",)
    pool: DeviceId
    vdevs: tuple[DeviceId, ...]
    name: str
    topology: ZfsTopology
    encrypted: bool

    def describe(self) -> str:
        how = " with native encryption" if self.encrypted else ""
        joined = ", ".join(self.vdevs)
        return (
            f"create zpool {self.name} as {self.pool} from {joined} "
            f"as a {self.topology.value}{how}"
        )

    def apply(self, context: Context) -> None:
        options = [*POOL_OPTIONS]
        if self.encrypted:
            options += ["-O", "encryption=on", "-O", "keyformat=passphrase", "-O", "keylocation=prompt"]
        # `keylocation=prompt` means the passphrase is read on stdin here and
        # asked for at boot, which is what ZFSBootMenu prompts for.
        context.run(
            [
                "zpool", "create", "-f",
                *options,
                "-R", str(context.target),
                self.name,
                # Before the devices, and absent for a stripe: `zpool create p
                # a b` with no keyword joins them end to end and survives
                # losing neither.
                *([self.topology.keyword] if self.topology.keyword else []),
                *(context.device_path(vdev) for vdev in self.vdevs),
            ],
            input_text=context.passphrase(self.pool) if self.encrypted else None,
        )


@dataclass(frozen=True, kw_only=True)
class ImportZpool(Operation):
    """Import a pool that already exists, or leave an imported one alone.

    The failure cleanup exports the pool, so a resumed run that skipped
    `CreateZpool` had no pool at all and every dataset mount below it
    addressed the live medium's own filesystem.
    """

    stage: Stage = Stage.ZFS
    host_commands = ("zpool",)
    pool: DeviceId
    name: str

    def describe(self) -> str:
        return f"import zpool {self.name} with the installation target as its alternate root"

    @property
    def survives_a_reboot(self) -> bool:
        return False

    def apply(self, context: Context) -> None:
        # Listed rather than imported blindly: `zpool import` on a pool that
        # is already imported fails, and its failure is not an answer here.
        listed = context.run(["zpool", "list", "-H", "-o", "name"])
        if self.name in listed.split():
            return
        context.run(["zpool", "import", "-N", "-f", "-R", str(context.target), self.name])


def canmount_for(mountpoint: PurePosixPath | None) -> str:
    """What a dataset's `canmount` is once the install is finished.

    `zfs mount -a` and `zfs-mount-generator` both skip `noauto`, which is
    wanted for the boot environment and wrong for everything else: a `/home`
    dataset marked `noauto` came up empty. The values are the ones
    `calamares-settings-gig`'s `zfs.conf` gives its dataset array.
    """
    if mountpoint is None:
        return "off"
    return "noauto" if mountpoint == PurePosixPath("/") else "on"


def _canmount_at_creation(mountpoint: PurePosixPath | None) -> str:
    """What it is created with, which is never `on`.

    `zfs create -o canmount=on` mounts the dataset immediately, and the pool's
    altroot puts a `/home` dataset under the target before the root dataset is
    mounted over it: the writes then land in the root dataset and the machine
    boots with an empty `/home`.
    """
    return "off" if mountpoint is None else "noauto"


@dataclass(frozen=True, kw_only=True)
class CreateDataset(Operation):
    stage: Stage = Stage.ZFS
    host_commands = ("zfs",)
    dataset: DeviceId
    name: str
    #: Where the dataset is mounted, or None when it only holds other datasets.
    mountpoint: PurePosixPath | None

    def describe(self) -> str:
        where = str(self.mountpoint) if self.mountpoint is not None else "none"
        return f"create dataset {self.name} as {self.dataset}, mountpoint {where}"

    def canmount(self) -> str:
        return canmount_for(self.mountpoint)

    def apply(self, context: Context) -> None:
        where = str(self.mountpoint) if self.mountpoint is not None else "none"
        # `noauto` whatever it ends up as: `zfs create -o canmount=on` mounts
        # the dataset there and then, and the pool's altroot puts `/home`
        # under the target before the root dataset is mounted over it. The
        # mount stage sets the real value once it has mounted this in order.
        # -p: a dataset three levels down needs its parents, and the layout
        # names only the leaves it mounts.
        context.run(
            [
                "zfs", "create", "-p",
                *DATASET_OPTIONS,
                "-o", f"mountpoint={where}",
                "-o", f"canmount={_canmount_at_creation(self.mountpoint)}",
                self.name,
            ]
        )


@dataclass(frozen=True, kw_only=True)
class Mount(Operation):
    stage: Stage = Stage.MOUNT
    host_commands = ("mkdir", "findmnt", "mount")
    mountpoint: DeviceId
    source: DeviceId
    path: PurePosixPath
    options: tuple[str, ...]

    def describe(self) -> str:
        options = f" with {','.join(self.options)}" if self.options else ""
        return f"mount {self.source} at {self.path}{options}"

    @property
    def survives_a_reboot(self) -> bool:
        return False

    def apply(self, context: Context) -> None:
        where = _under(context.target, self.path)
        context.run(["mkdir", "--parents", str(where)])
        if context.is_mounted(str(where)):
            return
        argv = ["mount"]
        if self.options:
            argv += ["--options", ",".join(self.options)]
        context.run([*argv, context.device_path(self.source), str(where)])


@dataclass(frozen=True, kw_only=True)
class MountZfsDataset(Operation):
    """A dataset carries its own mountpoint property, so it is mounted by name."""

    stage: Stage = Stage.MOUNT
    host_commands = ("zfs", "findmnt")
    mountpoint: DeviceId
    name: str
    path: PurePosixPath

    @property
    def survives_a_reboot(self) -> bool:
        return False

    def describe(self) -> str:
        return f"mount dataset {self.name} at {self.path}"

    def apply(self, context: Context) -> None:
        mounted = answered(
            context.run(
                ["zfs", "get", "-H", "-o", "value", "mounted", self.name],
                check=False,
            ),
            f"whether {self.name} is mounted could not be read",
        )
        if mounted == "yes":
            # A parent mounted later can hide this dataset while ZFS still
            # reports it mounted, so what a lookup answers decides readiness.
            # `findmnt --target` answers with the covered mount's own source,
            # measured on util-linux 2.42.2, and that is the answer this wants:
            # ZFS refuses to unmount a dataset whose mount is covered, so a
            # dataset mounted where it belongs is left alone either way.
            # Exit 1 means the path does not resolve, which is a mountpoint
            # this run has not created yet rather than a probe that failed.
            visible = answered(
                context.run(
                    [
                        "findmnt",
                        "--noheadings",
                        "--output",
                        "SOURCE",
                        "--target",
                        str(_under(context.target, self.path)),
                    ],
                    check=False,
                ),
                f"what is mounted at {self.path} could not be read",
                allowed=(0, 1),
            )
            if visible == self.name:
                return
            context.run(["zfs", "unmount", self.name])
        context.run(["zfs", "mount", self.name])
        # After the mount, not at creation: `canmount=on` is what makes
        # `zfs mount -a` bring this dataset up at boot, and setting it here
        # keeps `zfs create` from mounting it before the root is in place.
        wanted = canmount_for(self.path)
        if wanted != "noauto":
            context.run(["zfs", "set", f"canmount={wanted}", self.name])

@dataclass(frozen=True, kw_only=True)
class DiscardStage3(Operation):
    """The archive is downloaded onto the target because the work directory is
    a tmpfs, so the installed system otherwise keeps a multi-gigabyte tarball,
    its DIGESTS and the marker that says it was verified."""

    stage: Stage = Stage.FINISH

    def describe(self) -> str:
        return f"delete the downloaded stage3 from /{STAGE3_CACHE}"

    def apply(self, context: Context) -> None:
        context.run_in_target(["rm", "--recursive", "--force", f"/{STAGE3_CACHE}"])


#: How much of the tool's own words a busy-pool failure carries. The
#: verdict that reports it is truncated, and a pool's status is longer
#: than a console line.
HOLDER_BYTES: Final[int] = 400

RELEASE_TRIES: Final[int] = 6
RELEASE_PAUSE: Final[float] = 5.0


@dataclass(frozen=True, kw_only=True)
class UnmountTarget(Operation):
    """Leaving the target mounted keeps `/dev` and `/proc` bound into it, and
    the installing system then hangs at shutdown waiting for them."""

    stage: Stage = Stage.FINISH
    pools: tuple[str, ...]

    def required_host_commands(self) -> frozenset[str]:
        commands = {"umount", "findmnt"}
        if self.pools:
            commands.update(("zfs", "zpool", "sleep"))
        return frozenset(commands)

    @property
    def releases_the_machine(self) -> bool:
        return True

    def describe(self) -> str:
        exported = f" and export {', '.join(self.pools)}" if self.pools else ""
        return f"unmount everything under the target{exported}"

    def apply(self, context: Context) -> None:
        # Plain first, lazy only if that fails. A lazy unmount detaches the
        # tree and leaves the datasets mounted as far as the kernel is
        # concerned, so `zpool export` reads `pool is busy` for as long as the
        # references last and `-f` does not clear it either: Gig-OS failed
        # every attempt including the forced one.
        context.run(["umount", "--recursive", str(context.target)], check=False)
        # Judged by what is still mounted, not by the exit code: a recursive
        # unmount that cleared everything but one already-gone submount still
        # exits 1, and the lazy fallback then met an empty target and raised
        # `umount: /mnt/gentoo: not mounted` on a finished install.
        if self._still_mounted(context):
            context.run(["umount", "--recursive", "--lazy", str(context.target)])
        for pool in self.pools:
            self._unmount_datasets(context, pool)
            self._export(context, pool)

    def _still_mounted(self, context: Context) -> bool:
        found = context.run(
            ["findmnt", "--mountpoint", str(context.target)], check=False
        )
        if not isinstance(found, CommandOutput):
            raise CommandFailed(
                f"whether {context.target} is mounted could not be read"
            )
        if found.returncode not in _FINDMNT_ANSWERED:
            raise CommandFailed(
                f"whether {context.target} is mounted could not be read: "
                f"{str(found).strip()[:200]}"
            )
        return found.returncode == 0

    def _unmount_datasets(self, context: Context, pool: str) -> None:
        """Unmount the pool's own datasets, deepest first.

        Unmounting the target tree is not enough. A live environment that
        imported the pool itself mounts each dataset at its `mountpoint`
        property rather than under the altroot, so those mounts are outside
        `/mnt/gentoo` and `zpool export` reads `pool is busy` with the target
        already clear: Gig-OS failed there where gentoo-cjk did not.
        """
        listed = context.run(
            ["zfs", "list", "-H", "-o", "name,mounted", "-r", pool], check=False
        )
        if isinstance(listed, CommandOutput) and listed.returncode != 0:
            return
        names = [one.split("\t", 1)[0].strip() for one in str(listed).splitlines() if one.strip()]
        for name in sorted(names, key=lambda one: one.count("/"), reverse=True):
            context.run(["zfs", "unmount", name], check=False)

    def _mounted_datasets(self, context: Context, pool: str) -> bool:
        listed = context.run(
            ["zfs", "list", "-H", "-o", "name,mounted", "-r", pool], check=False
        )
        if not isinstance(listed, CommandOutput) or listed.returncode != 0:
            return False
        return any(
            separator and state.strip() == "yes"
            for line in str(listed).splitlines()
            for _, separator, state in [line.partition("\t")]
        )

    def _export(self, context: Context, pool: str) -> None:
        """Export the pool, forcing only a holder this operation can name.

        `umount --lazy` detaches the tree and returns before the last
        reference is dropped, so the export that follows reads `pool is busy`
        on a guest whose install had otherwise finished. Those references go
        within seconds, which is what the retries are for. A pool still busy
        with nothing of its own mounted is held by something else — `zed` in
        a live environment is one — and forcing there hides it, so it is
        reported instead. An unexported pool needs `zpool import -f` on the
        next boot, so the failure has to say which case it is.
        """
        last: CommandFailed | None = None
        for attempt in range(RELEASE_TRIES):
            try:
                context.run(["zpool", "export", pool])
                return
            except CommandFailed as failed:
                last = failed
                if attempt + 1 < RELEASE_TRIES:
                    context.run(["sleep", f"{RELEASE_PAUSE:g}"])
        assert last is not None
        if self._mounted_datasets(context, pool):
            context.run(["zpool", "export", "-f", pool])
            return
        raise CommandFailed(
            f"{pool} remains busy after dataset unmount; holder is unknown. "
            f"`zpool export` said {str(last).strip()[:HOLDER_BYTES]!r}, and the pool "
            f"reads {self._holders(context, pool)}"
        ) from last

    def _holders(self, context: Context, pool: str) -> str:
        """What the machine says about a pool it will not let go of.

        `holder is unknown` was the whole message, so an operator was told the
        export failed and nothing else: the run that hit it on `vm-zfs` left
        no way to tell `zed` from a stray mount from a device still open.
        Read rather than guessed, and `check=False` throughout because this
        runs while an error is already being raised.
        """
        status = context.run(["zpool", "status", "-v", pool], check=False)
        return str(status).strip()[:HOLDER_BYTES] or "nothing at all"


def finish(config: InstallConfig) -> list[Operation]:
    operations: list[Operation] = [
        # Before the unmount, which is the last chance to write to the target.
        DiscardStage3(),
        UnmountTarget(pools=tuple(pool.name for pool in config.disk.graph.of_type(ZfsPool))),
    ]
    if config.disk.mode is DiskMode.IMAGE:
        operations.append(DetachImage(device=_image_device(config), image=config.disk.image))
    return operations


def build(
    config: InstallConfig, storage_facts: StorageFacts | None = None
) -> list[Operation]:
    graph = config.disk.graph
    facts = storage_facts if storage_facts is not None else StorageFacts()
    operations: list[Operation] = []
    if config.disk.mode is DiskMode.IMAGE:
        image_size = config.disk.size
        if image_size is None:
            raise InvalidLayout("image mode has no image size")
        operations.append(
            CreateImage(
                device=_image_device(config),
                image=config.disk.image,
                size=image_size,
                wipe=config.disk.wipe,
            )
        )
    operations.append(ReleaseTarget(steps=_teardown(graph)))
    mounts: list[Operation] = []
    for node in topological(graph):
        for operation in _operations_for(graph, node, facts):
            (mounts if operation.stage is Stage.MOUNT else operations).append(operation)
    mounts += [_mount_operation(mount) for mount in resolve_mounts(graph)]
    for disk in _disks_with_partitions(graph):
        operations.append(RereadPartitionTable(disk=disk))
    operations += mounts
    # Every partition exists before the first mkfs, so the stage decides here
    # too, not only once the whole plan is assembled.
    return sorted(operations, key=lambda operation: operation.stage.order)


def _image_device(config: InstallConfig) -> DeviceId:
    devices = tuple(
        device.id
        for device in config.disk.graph.of_type(Existing)
        if device.selector == config.disk.image
    )
    if len(devices) != 1:
        raise InvalidLayout("image mode needs exactly one Existing device selecting disk.image")
    return devices[0]


def topological(graph: DeviceGraph) -> tuple[Node, ...]:
    """Nodes ordered so each one follows everything it is built from.

    Ties are broken by partition index first and by id second, so the same graph
    always produces the same plan, and `sgdisk --new=N:0:+size` never places a
    later partition in the space an earlier one was going to take.
    """
    ready: dict[DeviceId, Node] = {}
    remaining = dict(graph.nodes)
    while remaining:
        available = sorted(
            (node for node in remaining.values() if all(parent in ready for parent in node.inputs)),
            key=_order_key,
        )
        if not available:
            raise InvalidLayout(
                "these devices cannot be ordered: " + ", ".join(sorted(remaining))
            )
        for node in available:
            ready[node.id] = node
            del remaining[node.id]
    return tuple(ready.values())


def _order_key(node: Node) -> tuple[int, int, str]:
    """Partition index first, then whether the node takes the remaining space.

    `sgdisk --new=N:0:+size` and `lvcreate -l 100%FREE` both take what is free
    when they run, so a node with no size has to be created after every sized
    one that shares its container.
    """
    index = node.index if isinstance(node, Partition) else 0
    takes_the_rest = isinstance(node, (Partition, LogicalVolume)) and node.size is None
    return (index, 1 if takes_the_rest else 0, node.id)


#: What closes each kind of device, by the node that describes it.
_CLOSE: Final[dict[type[Node], Callable[[Node], tuple[str, ...]]]] = {
    ZfsPool: lambda node: ("zpool", "export", cast(ZfsPool, node).name),
    VolumeGroup: lambda node: ("vgchange", "--activate", "n", cast(VolumeGroup, node).name),
    Luks: lambda node: ("cryptsetup", "close", cast(Luks, node).name),
    MdRaid: lambda node: ("mdadm", "--stop", cast(MdRaid, node).device_path),
}


def _teardown(graph: DeviceGraph) -> tuple[tuple[str, ...], ...]:
    """Close each device before the one it is built on.

    The build order reversed, which is the only order that suits every nesting:
    a volume group on a LUKS container and a container on a logical volume both
    occur, and a fixed sequence of kinds gets one of them wrong.
    """
    steps: list[tuple[str, ...]] = []
    for node in reversed(topological(graph)):
        close = _CLOSE.get(type(node))
        if close is not None:
            steps.append(close(node))
    return tuple(steps)


def _disks_with_partitions(graph: DeviceGraph) -> tuple[DeviceId, ...]:
    """Sorted, because nothing orders one disk's probe against another's and
    graph order made the plan depend on how the devices happen to be written
    in the configuration file."""
    disks: set[DeviceId] = set()
    for partition in graph.of_type(Partition):
        table = graph[partition.table]
        if isinstance(table, PartitionTable):
            disks.add(table.disk)
    return tuple(sorted(disks))


def _operations_for(
    graph: DeviceGraph, node: Node, storage_facts: StorageFacts
) -> list[Operation]:
    if isinstance(node, Existing):
        return [WipeSignatures(device=node.id)] if node.wipe else []
    if isinstance(node, PartitionTable):
        return [
            CreatePartitionTable(
                table=node.id,
                disk=node.disk,
                kind=node.table,
                create=node.create,
                remove=node.remove,
            )
        ]
    if isinstance(node, Partition):
        table = _expect(graph, node.table, PartitionTable)
        return [
            CreatePartition(
                partition=node.id,
                disk=table.disk,
                table_kind=table.table,
                index=node.index,
                role=node.role,
                size=resolve_share(graph, node, storage_facts),
                label=node.label,
                start=_start_of(graph, node, storage_facts),
                limit=_extent_end_of(graph, node, storage_facts),
            ),
        ]
    if isinstance(node, MdRaid):
        return [
            CreateMdRaid(
                array=node.id,
                members=node.members,
                level=node.level,
                metadata=node.metadata,
                name=node.name,
            ),
            AssembleMdRaid(array=node.id, members=node.members, name=node.name),
        ]
    if isinstance(node, Luks):
        return [
            CreateLuks(container=node.id, backing=node.backing, name=node.name),
            OpenLuks(container=node.id, backing=node.backing, name=node.name),
        ]
    if isinstance(node, VolumeGroup):
        return [
            CreateVolumeGroup(group=node.id, members=node.members, name=node.name),
            ActivateVolumeGroup(group=node.id, name=node.name),
        ]
    if isinstance(node, LogicalVolume):
        group = _expect(graph, node.group, VolumeGroup)
        return [
            CreateLogicalVolume(volume=node.id, group=group.name, name=node.name, size=node.size)
        ]
    if isinstance(node, Filesystem):
        if not node.create:
            # Verified rather than made: mounting an xfs partition that the
            # configuration calls ext4 writes the wrong type into fstab, and
            # the machine then fails to mount it on the next boot.
            return [VerifyFilesystem(filesystem=node.id, device=node.device, kind=node.kind)]
        return [
            MakeFilesystem(
                filesystem=node.id, device=node.device, kind=node.kind, label=node.label
            )
        ]
    if isinstance(node, Subvolume):
        filesystem = _expect(graph, node.filesystem, Filesystem)
        if not node.create:
            return [
                VerifySubvolume(subvolume=node.id, device=filesystem.device, name=node.name)
            ]
        return [CreateSubvolume(subvolume=node.id, device=filesystem.device, name=node.name)]
    if isinstance(node, Swap):
        return [MakeSwap(swap=node.id, device=node.device)]
    if isinstance(node, ZfsPool):
        return [
            CreateZpool(
                pool=node.id,
                vdevs=node.vdevs,
                name=node.name,
                topology=node.topology,
                encrypted=node.encrypted,
            ),
            ImportZpool(pool=node.id, name=node.name),
        ]
    if isinstance(node, ZfsDataset):
        pool = _expect(graph, node.pool, ZfsPool)
        return [
            CreateDataset(
                dataset=node.id,
                name=dataset_name(pool, node),
                mountpoint=_dataset_mountpoint(graph, node.id),
            )
        ]
    return []


def _mount_operation(mount: ResolvedMount) -> Operation:
    if mount.dataset is not None:
        return MountZfsDataset(
            mountpoint=mount.mountpoint, name=mount.dataset, path=mount.path
        )
    if mount.device is None:
        raise InvalidLayout(f"mountpoint {mount.mountpoint!r} has no resolved source")
    return Mount(
        mountpoint=mount.mountpoint,
        source=mount.device,
        path=mount.path,
        options=mount.options,
    )


def _dataset_mountpoint(graph: DeviceGraph, dataset: DeviceId) -> PurePosixPath | None:
    for mount in graph.of_type(Mountpoint):
        if mount.source == dataset:
            return mount.path
    return None


def resolve_share(
    graph: DeviceGraph, partition: Partition, facts: StorageFacts
) -> Size | None:
    """The bytes this partition asks for, with any share worked out.

    A share is a share of what the table can hand out after every absolute
    size on it, so `40%` beside `60%` beside an esp and a swap is a layout
    that fits. `None` still means the rest, which is what `sgdisk` is told.

    Resolved here rather than at `apply()` so that `describe()` prints the
    number the run will use: a `--dry-run` that says `40%` and an install that
    writes 96 GiB are two different answers to one question.

    The usable space comes from `Size.usable_for_partitions`, the same
    function `exec/preflight.py` checks fixed sizes against. Its own docstring
    says why: answered separately, an operator was told their sizes fitted and
    the install refused them an hour later.
    """
    if partition.share is None:
        return partition.size
    if partition.share.percent is None:
        return _bounded(None, partition)
    table = _expect(graph, partition.table, PartitionTable)
    base = share_base(graph, table, facts)
    if base is None:
        raise InvalidLayout(
            f"{partition.id} asks for {partition.share.percent}% of "
            f"{table.disk}, and this machine did not report that disk's size"
        )
    wanted = Size(int(base.bytes * partition.share.percent / 100)).align_down()
    return _bounded(wanted, partition)


def share_base(
    graph: DeviceGraph, table: PartitionTable, facts: StorageFacts
) -> Size | None:
    """What a share on this table is a share of, or `None` when unknown.

    Every absolute size comes off first. A share is then what is left, which
    is how an operator describes it: the esp and the swap take their fixed
    sizes and the rest is split.
    """
    capacity = facts.capacities.get(table.disk)
    if capacity is None:
        return None
    usable = capacity.usable_for_partitions(
        table.table is TableType.GPT, SectorSize(512)
    )
    fixed = sum(
        one.size.bytes
        for one in graph.of_type(Partition)
        if one.table == table.id and one.size is not None
    )
    return Size(max(0, usable.bytes - fixed))


def _bounded(wanted: Size | None, partition: Partition) -> Size | None:
    """A share clamped by its own bounds, or refused when it cannot be met.

    Never quietly raised to the minimum: the space that would come from is
    another partition's, and moving it silently is how a layout stops adding
    up without anybody being told.
    """
    share = partition.share
    assert share is not None
    if wanted is None:
        return None
    if share.minimum is not None and wanted < share.minimum:
        raise InvalidLayout(
            f"{partition.id} works out to {wanted}, below the {share.minimum} it asks for"
        )
    if share.maximum is not None and share.maximum < wanted:
        return share.maximum
    return wanted


def _start_of(
    graph: DeviceGraph, partition: Partition, storage_facts: StorageFacts
) -> Size:
    """Where an MBR partition begins.

    On a table written from scratch, after every partition with a lower index.
    A partition with no size takes the rest of the disk, so anything after it
    has no start to compute; `validate.py` rejects that layout.

    On a table the operator edits, after the partitions the disk already has:
    those are not model nodes and summing the model's own gave 1MiB, which
    `parted` refused with `the closest location we can manage is 1048kB` after
    the removals in the same plan had already been committed.
    """
    table = graph[partition.table]
    if isinstance(table, PartitionTable) and table.id in storage_facts.free_extents:
        return _into_free_space(
            graph, table, partition, storage_facts.free_extents[table.id]
        )
    start = FIRST_OFFSET
    for sibling in sorted(graph.of_type(Partition), key=lambda node: node.index):
        if sibling.table != partition.table or sibling.index >= partition.index:
            continue
        if sibling.size is None:
            return start
        start = (start + sibling.size).align_up()
    return start


def _extent_end_of(
    graph: DeviceGraph,
    partition: Partition,
    storage_facts: StorageFacts | None,
) -> Size | None:
    """Where the gap this partition was placed in ends, or None for a fresh
    table where the rest of the disk is the answer.

    An unsized partition in an edited table would otherwise be written as
    `100%`, which reaches past every partition the operator asked to keep.
    """
    if partition.size is not None or storage_facts is None:
        return None
    table = _expect(graph, partition.table, PartitionTable)
    if table.create:
        return None
    extents = storage_facts.free_extents.get(table.id, ())
    start = _start_of(graph, partition, storage_facts)
    for extent in extents:
        if extent.start <= start.bytes <= extent.end:
            return Size(extent.end + 1).align_down()
    return None


def _into_free_space(
    graph: DeviceGraph,
    table: PartitionTable,
    partition: Partition,
    free_extents: tuple[Extent, ...],
) -> Size:
    """The first free extent with room for this partition and the ones before it.

    Every added partition of one table is placed in extent order, so two of
    them do not both take the start of the same gap.
    """
    added = [
        one
        for one in sorted(graph.of_type(Partition), key=lambda node: node.index)
        if one.table == table.id
    ]
    cursor = {extent.start: Size(extent.start).align_up() for extent in free_extents}
    for one in added:
        for extent in free_extents:
            at = cursor[extent.start]
            wanted = one.size.bytes if one.size is not None else 0
            if at.bytes + wanted > extent.end + 1:
                continue
            if one.id == partition.id:
                return at
            cursor[extent.start] = (at + one.size).align_up() if one.size else at
            break
        else:
            if one.id == partition.id:
                raise InvalidLayout(
                    f"{partition.id} does not fit any free extent of {table.id}"
                )
    raise InvalidLayout(f"{partition.id} does not fit any free extent of {table.id}")


def _expect(graph: DeviceGraph, device: DeviceId, kind: type[T]) -> T:
    node = graph[device]
    if not isinstance(node, kind):
        raise InvalidLayout(
            f"{device!r} is a {type(node).__name__.lower()} where a {kind.__name__.lower()} is required"
        )
    return node


#: `findmnt` answers 0 when the path is a mountpoint and 1 when it is not.
#: Every other code is the probe failing, which is a different answer: the
#: rule is written out at `MountFilesystem`, and the two cleanup readers had
#: it as `returncode == 0`.
_FINDMNT_ANSWERED: Final[tuple[int, ...]] = (0, 1)


def _under(target: PurePosixPath, path: PurePosixPath) -> PurePosixPath:
    return target / path.relative_to("/") if path != PurePosixPath("/") else target
