"""A hand-written partition list, turned into the same device graph.

The interface edits a list of `Partition` rows; this builds a `DeviceGraph`
from them. Nothing downstream can tell a manual layout from a template: both
produce the graph a configuration file would have described.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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


@dataclass(frozen=True)
class Reused:
    """A partition that already exists and is kept.

    `selector` is the path `exec/probe.py` listed. `format` chooses between
    reusing what is on it and making a new filesystem in place; either way no
    partition table is written, so every other partition on the disk survives.
    """

    selector: str
    mountpoint: str = ""
    filesystem: FilesystemType | None = None
    format: bool = False

    def describe(self) -> str:
        kind = self.filesystem.value if self.filesystem is not None else "unknown"
        what = "format" if self.format else "keep"
        return f"{self.selector}  {kind}  {self.mountpoint or '-'}  {what}"


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

    def describe(self) -> str:
        size = str(self.size) if self.size is not None else "rest"
        kind = self.filesystem.value if self.filesystem is not None else self.role.value
        where = self.mountpoint or "-"
        locked = " luks" if self.passphrase_file else ""
        return f"{self.index}  {size:>9}  {kind:<6}{locked}  {where}"


@dataclass
class Layout:
    """The whole table, plus the disk it is on."""

    disk: str = ""
    table: TableType = TableType.GPT
    slices: list[Slice] = field(default_factory=list)
    #: Partitions kept rather than created. Non-empty means no partition table
    #: is written at all, which is what makes the rest of the disk survive.
    reused: list[Reused] = field(default_factory=list)
    #: The pool every slice marked as a pool member joins.
    pool: str = "rpool"

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


def build_reused(layout: Layout) -> tuple[DeviceGraph, DeviceId]:
    """A graph that partitions nothing.

    Each kept partition becomes an `Existing` node with `wipe` off, so the plan
    emits no `sgdisk` and no `wipefs`, and `Filesystem.create` decides whether
    it is formatted.
    """
    nodes: list[Node] = []
    root = DeviceId("")
    for index, entry in enumerate(layout.reused, start=1):
        device = DeviceId(f"kept{index}")
        nodes.append(Existing(id=device, selector=entry.selector, wipe=False))
        if entry.filesystem is None or not entry.mountpoint:
            continue
        filesystem = DeviceId(f"keptfs{index}")
        mount = DeviceId(f"keptmnt{index}")
        nodes += [
            Filesystem(
                id=filesystem, device=device, kind=entry.filesystem, create=entry.format
            ),
            Mountpoint(
                id=mount,
                source=filesystem,
                path=PurePosixPath(entry.mountpoint),
                options=("umask=0077",) if entry.filesystem is ESP_FILESYSTEM else (),
            ),
        ]
        if entry.mountpoint == "/":
            root = mount
    return DeviceGraph.build(nodes), root


def build(layout: Layout) -> tuple[DeviceGraph, DeviceId]:
    """The graph and the id of the mount point that is `/`."""
    if layout.reused:
        return build_reused(layout)
    nodes: list[Node] = [
        Existing(id=DeviceId("disk"), selector=layout.disk, wipe=True),
        PartitionTable(id=DeviceId("table"), disk=DeviceId("disk"), table=layout.table),
    ]
    root = DeviceId("")
    vdevs: list[DeviceId] = []
    datasets: list[Slice] = []
    pool_passphrase = ""
    for entry in sorted(layout.slices, key=lambda one: one.index):
        part = DeviceId(f"part{entry.index}")
        nodes.append(
            Partition(
                id=part,
                table=DeviceId("table"),
                index=entry.index,
                role=entry.role,
                size=entry.size,
                label=entry.label,
            )
        )
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
            Filesystem(id=filesystem, device=carrier, kind=entry.filesystem, label=entry.label)
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
