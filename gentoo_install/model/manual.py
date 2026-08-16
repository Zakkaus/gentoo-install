# SPDX-License-Identifier: GPL-2.0-or-later
"""A hand-written partition list, turned into the same device graph.

The interface edits a list of `Partition` rows; this builds a `DeviceGraph`
from them. Nothing downstream can tell a manual layout from a template: both
produce the graph a configuration file would have described.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import PurePosixPath
from typing import Final

from .config import Firmware
from .device import (
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    FilesystemType,
    Luks,
    Mountpoint,
    Node,
    MdRaid,
    Partition,
    PartitionRole,
    PartitionTable,
    RaidLevel,
    RaidMetadata,
    Swap,
    TableType,
    ZfsDataset,
    ZfsPool,
    ZfsTopology,
)
from .size import Size

#: The id of the one array a hand-written table can build, and of its mount.
ARRAY: Final[str] = "array"
ARRAY_MOUNT: Final[str] = "array-mnt"

#: What `sgdisk` needs the esp to be, and what firmware reads.
ESP_FILESYSTEM: Final[FilesystemType] = FilesystemType.VFAT

#: The dataset a pool's root filesystem lives on, as the OpenZFS root-on-Linux
#: guide names it.
ROOT_DATASET: Final[str] = "ROOT/gentoo"


@dataclass(frozen=True)
class Purpose:
    """One row of the purpose menu, and everything that follows from it.

    The operator picks what the partition is for, not a GPT type code: the role,
    the mount point and whether a filesystem applies are all derived here.
    """

    key: str
    label: str
    role: PartitionRole
    #: Where it mounts. Empty for a purpose that mounts nothing, and for the
    #: one purpose that asks.
    mountpoint: str = ""
    filesystem: FilesystemType | None = None
    #: A purpose whose filesystem is fixed by the firmware or the pool takes no
    #: filesystem menu.
    chooses_filesystem: bool = True
    #: Only `other` asks, because every other purpose already knows.
    asks_mountpoint: bool = False


#: Every purpose the manual table offers. LVM members are absent on purpose:
#: this table builds no volume group, so a member here would be a partition
#: nothing ever assembles.
PURPOSES: Final[tuple[Purpose, ...]] = (
    Purpose("root", "root", PartitionRole.DATA, "/", FilesystemType.EXT4),
    Purpose("esp", "esp", PartitionRole.ESP, "/efi", ESP_FILESYSTEM, chooses_filesystem=False),
    Purpose("boot", "boot", PartitionRole.DATA, "/boot", FilesystemType.EXT4),
    Purpose("home", "home", PartitionRole.DATA, "/home", FilesystemType.EXT4),
    Purpose("var", "var", PartitionRole.DATA, "/var", FilesystemType.EXT4),
    Purpose("swap", "swap", PartitionRole.SWAP, chooses_filesystem=False),
    Purpose("zfs", "zfs pool member", PartitionRole.ZFS, chooses_filesystem=False, asks_mountpoint=True),
    Purpose("raid", "raid array member", PartitionRole.RAID, chooses_filesystem=False),
    Purpose("bios-boot", "bios-boot", PartitionRole.BIOS_BOOT, chooses_filesystem=False),
    Purpose("other", "other", PartitionRole.DATA, filesystem=FilesystemType.EXT4, asks_mountpoint=True),
)

_OTHER: Final[Purpose] = PURPOSES[-1]


def purpose_for(key: str) -> Purpose:
    return next(one for one in PURPOSES if one.key == key)


def purpose_of(entry: Slice) -> Purpose:
    """Which row of the menu a slice came from.

    Derived rather than stored: the role and the mount point already say it, and
    a stored copy is one more thing that can disagree with them.
    """
    for candidate in PURPOSES:
        if candidate.role is not entry.role:
            continue
        if candidate.asks_mountpoint or candidate.mountpoint == entry.mountpoint:
            return candidate
    return _OTHER


class SliceStatus(Enum):
    """What happens to one row of the table.

    Taken from `archinstall`'s `ModificationStatus`. Two exclusive modes could
    not say "keep the Windows partition, reformat the root, delete the rest,
    add a swap", and that is the ordinary case rather than an exotic one.
    """

    #: Already there and untouched. Mounted if it names a mount point.
    KEEP = "keep"
    #: Already there, kept in the table, given a new filesystem.
    FORMAT = "format"
    #: Already there, removed from the table. Its data goes with it.
    DELETE = "delete"
    #: Not there yet.
    CREATE = "create"

    @property
    def exists(self) -> bool:
        return self is not SliceStatus.CREATE

    @property
    def edits_the_table(self) -> bool:
        """Whether this row changes the partition table itself. A table nobody
        edits is never written, and every partition on the disk survives."""
        return self in (SliceStatus.DELETE, SliceStatus.CREATE)


#: What each status does, as the row says it. Beside the enum rather than in
#: the screen: a status added here without a sentence is a menu row that reads
#: as its own key.
STATUS_REASONS: Final[dict[SliceStatus, str]] = {
    SliceStatus.KEEP: "left alone, data and all",
    SliceStatus.FORMAT: "kept in the table, given a new filesystem",
    SliceStatus.DELETE: "removed from the table, data and all",
    SliceStatus.CREATE: "not on the disk yet",
}


@dataclass(frozen=True)
class Slice:
    """One row of the partition table the operator is editing."""

    index: int
    role: PartitionRole
    #: None means the rest of the disk. Only one slice may leave it unset.
    size: Size | None
    #: None for a slice that carries no filesystem, such as bios-boot.
    filesystem: FilesystemType | None = None
    mountpoint: str = ""
    label: str = ""
    #: A path on the installing system, never the passphrase. Non-empty puts
    #: LUKS between the partition and its filesystem.
    passphrase_file: str = ""
    status: SliceStatus = SliceStatus.CREATE
    #: The path `exec/probe.py` listed, for a row that is already on the disk.
    #: Empty for `create`, which has no device until the plan runs.
    selector: str = ""
    #: Partition-table number supplied by the block-device probe. New rows use
    #: `index`; existing rows must retain the number the machine reported.
    partition_number: int | None = None

    def describe(self) -> str:
        # A row already on the disk has whatever size it has; only a new one is
        # given the rest of the free space.
        size = str(self.size) if self.size is not None else ("" if self.status.exists else "rest")
        kind = self.filesystem.value if self.filesystem is not None else self.role.value
        where = self.mountpoint or "-"
        locked = " luks" if self.passphrase_file else ""
        named = self.selector.rsplit("/", 1)[-1] if self.selector else str(self.index)
        return f"{named:<10} {size:>9}  {kind:<6}{locked}  {where:<12} {self.status.value}"


@dataclass
class Disk:
    """One disk and the table being edited on it.

    A list of these rather than one disk, because `archinstall`'s
    `select_devices()` hands `_manual_partitioning` a list and one
    `DeviceModification` per disk: the esp on one drive and the root on
    another, or a mirror across two, cannot be said with a single table.
    """

    selector: str = ""
    table: TableType = TableType.GPT
    slices: list[Slice] = field(default_factory=list)

    def writes_the_table(self) -> bool:
        """Whether anything here changes this disk's partition table.

        A table of nothing but `keep` and `format` rows is never written, so
        every partition on the disk survives; that is what the separate
        "reuse" mode used to be.
        """
        return any(one.status.edits_the_table for one in self.slices)

    def next_index(self) -> int:
        return max((entry.index for entry in self.slices), default=0) + 1


@dataclass
class Array:
    """What the rows marked as array members are assembled into.

    One array, as there is one pool: a second would make every row say which
    one it joins, and this table exists to be read at a glance.
    """

    name: str = "md0"
    level: RaidLevel = RaidLevel.RAID1
    #: An array holding the esp needs the superblock at the end, so the
    #: firmware reads the member as a plain vfat partition.
    metadata: RaidMetadata = RaidMetadata.V1_2
    filesystem: FilesystemType = FilesystemType.EXT4
    mountpoint: str = "/"
    label: str = ""
    #: A path on the installing system, never the passphrase. Non-empty puts
    #: LUKS between the array and its filesystem.
    passphrase_file: str = ""


@dataclass
class Layout:
    """Every disk the operator is partitioning, and the pool they may share."""

    disks: list[Disk] = field(default_factory=list)
    #: What the rows marked as array members are assembled into.
    array: Array = field(default_factory=Array)
    #: The pool every slice marked as a pool member joins, on any disk.
    pool: str = "rpool"
    #: A path on the installing system, never the passphrase. Non-empty
    #: encrypts the pool.
    passphrase_file: str = ""
    #: How those members are joined. Only asked once more than one is marked:
    #: a single member has nothing to mirror.
    topology: ZfsTopology = ZfsTopology.STRIPE

    @property
    def slices(self) -> list[Slice]:
        """Every row across every disk, for a caller that only counts them."""
        return [entry for disk in self.disks for entry in disk.slices]

    def writes_the_table(self) -> bool:
        return any(disk.writes_the_table() for disk in self.disks)

    def holds(self, selector: str) -> bool:
        return any(disk.selector == selector for disk in self.disks)


def suggest(
    disk: str,
    firmware: Firmware,
    filesystem: FilesystemType | None = FilesystemType.EXT4,
) -> Layout:
    """What the table starts as, so the operator edits rather than types.

    A blank table is a worse starting point than one that already boots: every
    layout needs the same first entries and only the sizes differ. `filesystem`
    is None for a root on ZFS, which is a pool member and carries none.
    """
    root = Slice(
        index=1,
        role=PartitionRole.ZFS if filesystem is None else PartitionRole.DATA,
        size=None,
        filesystem=filesystem,
        mountpoint="/",
        label="gentoo",
    )
    if firmware is not Firmware.UEFI:
        return Layout(disks=[Disk(selector=disk, table=TableType.MBR, slices=[root])])
    return Layout(
        disks=[
            Disk(
                selector=disk,
                table=TableType.GPT,
                slices=[
                    Slice(
                        index=1,
                        role=PartitionRole.ESP,
                        size=Size.parse("1GiB"),
                        filesystem=ESP_FILESYSTEM,
                        mountpoint="/efi",
                        label="ESP",
                    ),
                    replace(root, index=2),
                ],
            )
        ]
    )


def dataset_for(mountpoint: str) -> str:
    """What to call the dataset mounted there.

    `/` is the pool's root filesystem and takes the conventional name; anything
    else takes its path, because a dataset name cannot start with a slash.
    """
    return ROOT_DATASET if mountpoint == "/" else mountpoint.strip("/").replace("//", "/")


def _index_of(entry: Slice) -> int:
    """The number the partition has in the table on the disk.

    Use the probe fact rather than parsing a kernel name: `/dev/nvme0n1p2` and
    `/dev/mapper/...` do not have a reliable suffix to interpret.
    """
    return entry.partition_number if entry.partition_number is not None else entry.index


def _has_existing_slices(slices: list[Slice]) -> bool:
    """Whether the disk still has any partition entry from before this run."""
    return any(entry.status.exists for entry in slices)


def _table_nodes(disk: Disk, prefix: str) -> list[Node]:
    """The disk and its table, or nothing when no row edits the table.

    The disk is wiped only when nothing on it is kept, so `sgdisk --zap-all`
    never takes a partition the operator asked to keep.
    """
    if not disk.writes_the_table():
        return []
    has_existing_slices = _has_existing_slices(disk.slices)
    return [
        Existing(id=DeviceId(prefix), selector=disk.selector, wipe=not has_existing_slices),
        PartitionTable(
            id=DeviceId(f"{prefix}-table"),
            disk=DeviceId(prefix),
            table=disk.table,
            create=not has_existing_slices,
            remove=tuple(
                _index_of(one) for one in disk.slices if one.status is SliceStatus.DELETE
            ),
        ),
    ]


def _array_nodes(array: Array, members: tuple[DeviceId, ...]) -> list[Node]:
    """The array, whatever encrypts it, its filesystem and its mount point."""
    nodes: list[Node] = [
        MdRaid(
            id=DeviceId(ARRAY),
            members=members,
            level=array.level,
            name=array.name,
            metadata=array.metadata,
        )
    ]
    carrier = DeviceId(ARRAY)
    if array.passphrase_file:
        carrier = DeviceId(f"{ARRAY}-crypt")
        nodes.append(
            Luks(
                id=carrier,
                backing=DeviceId(ARRAY),
                name=array.name,
                passphrase_file=array.passphrase_file,
            )
        )
    filesystem = DeviceId(f"{ARRAY}-fs")
    nodes.append(
        Filesystem(id=filesystem, device=carrier, kind=array.filesystem, label=array.label)
    )
    if array.mountpoint:
        nodes.append(
            Mountpoint(
                id=DeviceId(ARRAY_MOUNT),
                source=filesystem,
                path=PurePosixPath(array.mountpoint),
                options=("umask=0077",) if array.filesystem is ESP_FILESYSTEM else (),
            )
        )
    return nodes


def _numbered(slices: list[Slice]) -> list[Slice]:
    """The slices in the order the disk will number them.

    The one with no size takes what is left, so it has to be last whatever
    index the operator gave it: `suggest()` starts the root at 1 and an added
    partition therefore sat behind a partition that runs to the last sector,
    which `sgdisk` refuses after the table has been written.
    """
    kept = [one for one in slices if one.status is not SliceStatus.DELETE]
    if any(one.status is not SliceStatus.CREATE for one in kept):
        # An edited table: every number here is one the disk already gave out,
        # and moving a kept partition's number renames somebody's filesystem.
        return sorted(slices, key=lambda one: one.index)
    ordered = sorted(
        sorted(kept, key=lambda one: one.index), key=lambda one: one.size is None
    )
    renumbered: list[Slice] = []
    for position, entry in enumerate(ordered, start=1):
        renumbered.append(entry if entry.index == position else replace(entry, index=position))
    return renumbered + [one for one in slices if one.status is SliceStatus.DELETE]


def build(layout: Layout) -> tuple[DeviceGraph, DeviceId]:
    """The graph and the id of the mount point that is `/`.

    One pass over every disk's table. A row already on the disk becomes an
    `Existing` with `wipe` off; a new one becomes a `Partition`; a deleted one
    becomes an entry the table drops. Node ids carry the disk they came from,
    because two disks both have a partition 1.
    """
    nodes: list[Node] = []
    root = DeviceId("")
    vdevs: list[DeviceId] = []
    members: list[DeviceId] = []
    #: Pool members with a mount point, each with the id prefix of its disk.
    datasets: list[tuple[str, Slice]] = []
    for position, disk in enumerate(layout.disks, start=1):
        prefix = f"disk{position}"
        nodes += _table_nodes(disk, prefix)
        for entry in _numbered(disk.slices):
            if entry.status is SliceStatus.DELETE:
                # Gone from the table above, so it carries nothing downstream.
                continue
            part = DeviceId(f"{prefix}-part{entry.index}")
            if entry.status is SliceStatus.CREATE:
                nodes.append(
                    Partition(
                        id=part,
                        table=DeviceId(f"{prefix}-table"),
                        index=entry.index,
                        role=entry.role,
                        size=entry.size,
                        label=entry.label,
                    )
                )
            else:
                nodes.append(Existing(id=part, selector=entry.selector, wipe=False))
            if entry.role is PartitionRole.ZFS:
                # The pool encrypts its own datasets, so a member is never
                # wrapped in LUKS as well.
                vdevs.append(part)
                if entry.mountpoint:
                    datasets.append((prefix, entry))
                continue
            if entry.role is PartitionRole.RAID:
                # The array carries the filesystem and any LUKS, so a member
                # carries neither.
                members.append(part)
                continue
            carrier = part
            if entry.passphrase_file:
                carrier = DeviceId(f"{prefix}-crypt{entry.index}")
                nodes.append(
                    Luks(
                        id=carrier,
                        backing=part,
                        name=f"crypt{position}-{entry.index}",
                        passphrase_file=entry.passphrase_file,
                    )
                )
            if entry.role is PartitionRole.SWAP:
                nodes.append(Swap(id=DeviceId(f"{prefix}-swap{entry.index}"), device=carrier))
                continue
            if entry.filesystem is None:
                continue
            filesystem = DeviceId(f"{prefix}-fs{entry.index}")
            nodes.append(
                Filesystem(
                    id=filesystem,
                    device=carrier,
                    kind=entry.filesystem,
                    label=entry.label,
                    # `keep` mounts what is there; everything else makes one.
                    create=entry.status is not SliceStatus.KEEP,
                )
            )
            if not entry.mountpoint:
                continue
            mount = DeviceId(f"{prefix}-mnt{entry.index}")
            nodes.append(
                Mountpoint(
                    id=mount,
                    source=filesystem,
                    path=PurePosixPath(entry.mountpoint),
                    options=("umask=0077",) if entry.filesystem is ESP_FILESYSTEM else (),
                )
            )
            if entry.mountpoint == "/":
                root = mount
    if members:
        nodes += _array_nodes(layout.array, tuple(members))
        if layout.array.mountpoint == "/":
            root = DeviceId(ARRAY_MOUNT)
    if vdevs:
        nodes.append(
            ZfsPool(
                id=DeviceId("pool"),
                vdevs=tuple(vdevs),
                name=layout.pool,
                topology=layout.topology,
                encrypted=bool(layout.passphrase_file),
                passphrase_file=layout.passphrase_file,
            )
        )
        for prefix, entry in datasets:
            dataset = DeviceId(f"{prefix}-ds{entry.index}")
            mount = DeviceId(f"{prefix}-mnt{entry.index}")
            nodes += [
                ZfsDataset(id=dataset, pool=DeviceId("pool"), name=dataset_for(entry.mountpoint)),
                Mountpoint(id=mount, source=dataset, path=PurePosixPath(entry.mountpoint)),
            ]
            if entry.mountpoint == "/":
                root = mount
    return DeviceGraph.build(nodes), root
