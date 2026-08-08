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
    Partition,
    PartitionRole,
    PartitionTable,
    Swap,
    TableType,
    ZfsDataset,
    ZfsPool,
    ZfsTopology,
)
from .size import Size

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


#: Every purpose the manual table offers. RAID and LVM members are absent on
#: purpose: this table builds no array and no volume group, so a member here
#: would be a partition nothing ever assembles.
PURPOSES: Final[tuple[Purpose, ...]] = (
    Purpose("root", "root", PartitionRole.DATA, "/", FilesystemType.EXT4),
    Purpose("esp", "esp", PartitionRole.ESP, "/efi", ESP_FILESYSTEM, chooses_filesystem=False),
    Purpose("boot", "boot", PartitionRole.DATA, "/boot", FilesystemType.EXT4),
    Purpose("home", "home", PartitionRole.DATA, "/home", FilesystemType.EXT4),
    Purpose("var", "var", PartitionRole.DATA, "/var", FilesystemType.EXT4),
    Purpose("swap", "swap", PartitionRole.SWAP, chooses_filesystem=False),
    Purpose("zfs", "zfs pool member", PartitionRole.ZFS, chooses_filesystem=False, asks_mountpoint=True),
    Purpose("bios-boot", "bios-boot", PartitionRole.BIOS_BOOT, chooses_filesystem=False),
    Purpose("other", "other", PartitionRole.DATA, filesystem=FilesystemType.EXT4, asks_mountpoint=True),
)

_OTHER: Final[Purpose] = PURPOSES[-1]


def purpose_for(key: str) -> Purpose:
    """The row of the table with that key."""
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
    #: LUKS between the partition and its filesystem, or encrypts the pool.
    passphrase_file: str = ""
    status: SliceStatus = SliceStatus.CREATE
    #: The path `exec/probe.py` listed, for a row that is already on the disk.
    #: Empty for `create`, which has no device until the plan runs.
    selector: str = ""

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
class Layout:
    """The whole table, plus the disk it is on."""

    disk: str = ""
    table: TableType = TableType.GPT
    slices: list[Slice] = field(default_factory=list)
    def writes_the_table(self) -> bool:
        """Whether anything here changes the partition table.

        A table of nothing but `keep` and `format` rows is never written, so
        every partition on the disk survives; that is what the separate
        "reuse" mode used to be.
        """
        return any(one.status.edits_the_table for one in self.slices)
    #: The pool every slice marked as a pool member joins.
    pool: str = "rpool"
    #: How those members are joined. Only asked once more than one is marked:
    #: a single member has nothing to mirror.
    topology: ZfsTopology = ZfsTopology.STRIPE

    def next_index(self) -> int:
        return max((entry.index for entry in self.slices), default=0) + 1


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
        return Layout(disk=disk, table=TableType.MBR, slices=[root])
    return Layout(
        disk=disk,
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


def dataset_for(mountpoint: str) -> str:
    """What to call the dataset mounted there.

    `/` is the pool's root filesystem and takes the conventional name; anything
    else takes its path, because a dataset name cannot start with a slash.
    """
    return ROOT_DATASET if mountpoint == "/" else mountpoint.strip("/").replace("//", "/")


def _index_of(entry: Slice) -> int:
    """The number the partition has in the table on the disk.

    Read off the selector rather than taken from `index`: the row order in the
    editor is not the entry number, and `sgdisk --delete` addresses the entry.
    """
    trailing = ""
    for character in reversed(entry.selector):
        if not character.isdigit():
            break
        trailing = character + trailing
    return int(trailing) if trailing else entry.index


def build(layout: Layout) -> tuple[DeviceGraph, DeviceId]:
    """The graph and the id of the mount point that is `/`.

    One pass over one table. A row that is already on the disk becomes an
    `Existing` with `wipe` off; a new one becomes a `Partition`; a deleted one
    becomes an entry the table drops. The disk is wiped only when nothing is
    kept, so `sgdisk --zap-all` never takes a partition the operator asked for.
    """
    kept = [one for one in layout.slices if one.status.exists and one.status is not SliceStatus.DELETE]
    removed = tuple(
        _index_of(one) for one in layout.slices if one.status is SliceStatus.DELETE
    )
    fresh = [one for one in layout.slices if one.status is SliceStatus.CREATE]
    nodes: list[Node] = []
    if fresh or removed:
        # Wiped only when there is nothing on the disk worth keeping: the
        # operator who kept a row asked for the table it lives in.
        untouched = bool(kept) or bool(removed)
        nodes += [
            Existing(id=DeviceId("disk"), selector=layout.disk, wipe=not untouched),
            PartitionTable(
                id=DeviceId("disk-table"),
                disk=DeviceId("disk"),
                table=layout.table,
                create=not untouched,
                remove=removed,
            ),
        ]
    root = DeviceId("")
    vdevs: list[DeviceId] = []
    datasets: list[Slice] = []
    pool_passphrase = ""
    for entry in sorted(layout.slices, key=lambda one: one.index):
        if entry.status is SliceStatus.DELETE:
            # Gone from the table above, so it carries nothing downstream.
            continue
        part = DeviceId(f"part{entry.index}")
        if entry.status is SliceStatus.CREATE:
            nodes.append(
                Partition(
                    id=part,
                    table=DeviceId("disk-table"),
                    index=entry.index,
                    role=entry.role,
                    size=entry.size,
                    label=entry.label,
                )
            )
        else:
            nodes.append(Existing(id=part, selector=entry.selector, wipe=False))
        if entry.role is PartitionRole.ZFS:
            # The pool encrypts its own datasets, so a member is never wrapped
            # in LUKS as well.
            vdevs.append(part)
            pool_passphrase = pool_passphrase or entry.passphrase_file
            if entry.mountpoint:
                datasets.append(entry)
            continue
        carrier = part
        if entry.passphrase_file:
            carrier = DeviceId(f"crypt{entry.index}")
            nodes.append(
                Luks(
                    id=carrier,
                    backing=part,
                    name=f"crypt{entry.index}",
                    passphrase_file=entry.passphrase_file,
                )
            )
        if entry.role is PartitionRole.SWAP:
            nodes.append(Swap(id=DeviceId(f"swap{entry.index}"), device=carrier))
            continue
        if entry.filesystem is None:
            continue
        filesystem = DeviceId(f"fs{entry.index}")
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
        mount = DeviceId(f"mnt{entry.index}")
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
    if vdevs:
        nodes.append(
            ZfsPool(
                id=DeviceId("pool"),
                vdevs=tuple(vdevs),
                name=layout.pool,
                topology=layout.topology,
                encrypted=bool(pool_passphrase),
                passphrase_file=pool_passphrase,
            )
        )
        for entry in datasets:
            dataset = DeviceId(f"ds{entry.index}")
            mount = DeviceId(f"mnt{entry.index}")
            nodes += [
                ZfsDataset(id=dataset, pool=DeviceId("pool"), name=dataset_for(entry.mountpoint)),
                Mountpoint(id=mount, source=dataset, path=PurePosixPath(entry.mountpoint)),
            ]
            if entry.mountpoint == "/":
                root = mount
    return DeviceGraph.build(nodes), root
