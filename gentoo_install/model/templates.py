"""Whole-disk layouts, as functions that return a device graph.

A template is not a mode the rest of the installer knows about: it produces the
same `DeviceGraph` a hand-written configuration would, and nothing downstream
can tell the difference. `oddlama-gentoo-install` made its templates macros that
set global variables, which is why its mount points are fixed at three.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Final

from ..errors import InvalidLayout
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
    Subvolume,
    Swap,
    TableType,
    ZfsDataset,
    ZfsPool,
)
from .size import Size

#: Big enough for several kernels and their initramfs images, which is what
#: fills an esp that later turns out to be too small.
ESP_SIZE: Final[Size] = Size.parse("1GiB")

#: `btrfsSubvolumes` of `calamares-settings-gig`'s `mount.conf`, so a system
#: installed either way keeps its snapshots and its churn in the same places.
SUBVOLUMES: Final[tuple[tuple[str, str], ...]] = (
    ("@", "/"),
    ("@home", "/home"),
    ("@cache", "/var/cache"),
    ("@log", "/var/log"),
)


class Layout(Enum):
    WHOLE_DISK = "whole-disk"
    WHOLE_DISK_BTRFS = "whole-disk-btrfs"
    WHOLE_DISK_ZFS = "whole-disk-zfs"
    #: Not a template: it creates nothing. `templates.build` refuses it, and
    #: `manual.build_reused` is what turns the operator's table into a graph.
    REUSE = "reuse"


@dataclass(frozen=True)
class Choice:
    """What the interface asks for, and a hand-written file can set too."""

    disk: str
    layout: Layout = Layout.WHOLE_DISK
    firmware: Firmware = Firmware.UEFI
    #: None follows the firmware: GPT for UEFI, MBR for BIOS.
    table: TableType | None = None
    filesystem: FilesystemType = FilesystemType.EXT4
    swap: Size | None = None
    #: A path on the installing system, never the passphrase. Empty means the
    #: layout is not encrypted.
    passphrase_file: str = ""
    pool: str = "rpool"


def build(choice: Choice) -> tuple[DeviceGraph, DeviceId]:
    """The graph and the id of the mount point that is `/`."""
    if choice.layout is Layout.REUSE:
        # Refused rather than approximated: reuse names partitions that already
        # exist, and a template has none of them to name.
        raise InvalidLayout(
            "the reuse layout describes existing partitions, so it is built from the "
            "operator's table and not from a template"
        )
    # MBR for BIOS rather than GPT: GPT would need a bios-boot partition for
    # GRUB's stage 1.5, and MBR needs nothing but the gap after the table.
    nodes: list[Node] = [
        Existing(id=DeviceId("disk"), selector=choice.disk, wipe=True),
        PartitionTable(
            id=DeviceId("table"),
            disk=DeviceId("disk"),
            table=choice.table
            or (TableType.GPT if choice.firmware is Firmware.UEFI else TableType.MBR),
        ),
    ]
    index = 1
    if choice.firmware is Firmware.UEFI:
        nodes += [
            Partition(
                id=DeviceId("esp"),
                table=DeviceId("table"),
                index=index,
                role=PartitionRole.ESP,
                size=ESP_SIZE,
            ),
            Filesystem(id=DeviceId("espfs"), device=DeviceId("esp"), kind=FilesystemType.VFAT, label="ESP"),
            Mountpoint(
                id=DeviceId("mnt-esp"),
                source=DeviceId("espfs"),
                path=PurePosixPath("/efi"),
                options=("umask=0077",),
            ),
        ]
        index += 1

    if choice.swap is not None:
        nodes += [
            Partition(
                id=DeviceId("swappart"),
                table=DeviceId("table"),
                index=index,
                role=PartitionRole.SWAP,
                size=choice.swap,
            ),
            Swap(id=DeviceId("swap"), device=DeviceId("swappart")),
        ]
        index += 1

    root_partition = DeviceId("rootpart")
    nodes.append(
        Partition(
            id=root_partition,
            table=DeviceId("table"),
            index=index,
            role=PartitionRole.DATA,
            size=None,
        )
    )
    carrier = root_partition
    if choice.passphrase_file and choice.layout is not Layout.WHOLE_DISK_ZFS:
        nodes.append(
            Luks(
                id=DeviceId("crypt"),
                backing=root_partition,
                name="root",
                passphrase_file=choice.passphrase_file,
            )
        )
        carrier = DeviceId("crypt")

    if choice.layout is Layout.WHOLE_DISK_ZFS:
        nodes += [
            ZfsPool(
                id=DeviceId("pool"),
                vdevs=(root_partition,),
                name=choice.pool,
                encrypted=bool(choice.passphrase_file),
                passphrase_file=choice.passphrase_file,
            ),
            ZfsDataset(id=DeviceId("ds-root"), pool=DeviceId("pool"), name="ROOT/gentoo"),
            Mountpoint(id=DeviceId("mnt-root"), source=DeviceId("ds-root"), path=PurePosixPath("/")),
        ]
        return DeviceGraph.build(nodes), DeviceId("mnt-root")

    if choice.layout is Layout.WHOLE_DISK_BTRFS:
        nodes.append(
            Filesystem(id=DeviceId("rootfs"), device=carrier, kind=FilesystemType.BTRFS, label="gentoo")
        )
        for name, where in SUBVOLUMES:
            key = name.removeprefix("@") or "root"
            nodes += [
                Subvolume(id=DeviceId(f"sub-{key}"), filesystem=DeviceId("rootfs"), name=name),
                Mountpoint(
                    id=DeviceId(f"mnt-{key}"),
                    source=DeviceId(f"sub-{key}"),
                    path=PurePosixPath(where),
                    options=("compress=zstd:1",),
                ),
            ]
        return DeviceGraph.build(nodes), DeviceId("mnt-root")

    nodes += [
        Filesystem(id=DeviceId("rootfs"), device=carrier, kind=choice.filesystem, label="gentoo"),
        Mountpoint(id=DeviceId("mnt-root"), source=DeviceId("rootfs"), path=PurePosixPath("/")),
    ]
    return DeviceGraph.build(nodes), DeviceId("mnt-root")
