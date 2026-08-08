"""A hand-written partition list, turned into the same device graph.

The interface edits a list of `Partition` rows; this builds a `DeviceGraph`
from them. Nothing downstream can tell a manual layout from a template: both
produce the graph a configuration file would have described.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
)
from .size import Size

#: What `sgdisk` needs the esp to be, and what firmware reads.
ESP_FILESYSTEM: Final[FilesystemType] = FilesystemType.VFAT


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

    def describe(self) -> str:
        size = str(self.size) if self.size is not None else "rest"
        kind = self.filesystem.value if self.filesystem is not None else self.role.value
        where = self.mountpoint or "not mounted"
        locked = " luks" if self.passphrase_file else ""
        return f"{self.index}  {size}  {kind}{locked}  {where}"


@dataclass
class Layout:
    """The whole table, plus the disk it is on."""

    disk: str = ""
    table: TableType = TableType.GPT
    slices: list[Slice] = field(default_factory=list)

    def next_index(self) -> int:
        return max((entry.index for entry in self.slices), default=0) + 1


def suggest(disk: str, firmware: Firmware) -> Layout:
    """What the table starts as, so the operator edits rather than types.

    A blank table is a worse starting point than one that already boots: every
    layout needs the same first two entries and only the sizes differ.
    """
    if firmware is Firmware.UEFI:
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
                Slice(
                    index=2,
                    role=PartitionRole.DATA,
                    size=None,
                    filesystem=FilesystemType.EXT4,
                    mountpoint="/",
                    label="gentoo",
                ),
            ],
        )
    return Layout(
        disk=disk,
        table=TableType.MBR,
        slices=[
            Slice(
                index=1,
                role=PartitionRole.DATA,
                size=None,
                filesystem=FilesystemType.EXT4,
                mountpoint="/",
                label="gentoo",
            )
        ],
    )


def build(layout: Layout) -> tuple[DeviceGraph, DeviceId]:
    """The graph and the id of the mount point that is `/`."""
    nodes: list[Node] = [
        Existing(id=DeviceId("disk"), selector=layout.disk, wipe=True),
        PartitionTable(id=DeviceId("table"), disk=DeviceId("disk"), table=layout.table),
    ]
    root = DeviceId("")
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
    return DeviceGraph.build(nodes), root
