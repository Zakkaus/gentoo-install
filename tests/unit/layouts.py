# SPDX-License-Identifier: GPL-2.0-or-later
"""Configurations the compatibility and validation tests start from.

Each builder returns a configuration that breaks no rule, so a test states what
it changes and nothing else.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from gentoo_install.model.config import (
    SystemConfig,
    Bootloader,
    BootloaderConfig,
    DiskConfig,
    Firmware,
    InstallConfig,
    KernelConfig,
    KernelSource,
)
from gentoo_install.model.device import (
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
    StorageLayout,
    TableType,
    ZfsDataset,
    ZfsPool,
)
from gentoo_install.model.size import Size


def i(name: str) -> DeviceId:
    return DeviceId(name)


def ext4_on_gpt() -> list[Node]:
    """One disk, an esp mounted at /efi, ext4 root. The plainest UEFI install."""
    return [
        Existing(id=i("disk"), selector="/dev/disk/by-id/virtio-target", wipe=True),
        PartitionTable(id=i("table"), disk=i("disk"), table=TableType.GPT),
        Partition(id=i("esp"), table=i("table"), index=1, role=PartitionRole.ESP, size=Size.parse("512MiB")),
        Partition(id=i("rootpart"), table=i("table"), index=2, role=PartitionRole.DATA, size=None),
        Filesystem(id=i("espfs"), device=i("esp"), kind=FilesystemType.VFAT, label="ESP"),
        Filesystem(id=i("rootfs"), device=i("rootpart"), kind=FilesystemType.EXT4),
        Mountpoint(id=i("mnt-root"), source=i("rootfs"), path=PurePosixPath("/")),
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/efi")),
    ]


def zfs_root() -> list[Node]:
    """ZFS root with ZFSBootMenu: the kernel stays in the pool and only the ZBM
    executable goes on the esp, which is how the Live ISO installs it."""
    return [
        Existing(id=i("disk"), selector="/dev/disk/by-id/virtio-target", wipe=True),
        PartitionTable(id=i("table"), disk=i("disk"), table=TableType.GPT),
        Partition(id=i("esp"), table=i("table"), index=1, role=PartitionRole.ESP, size=Size.parse("512MiB")),
        Partition(id=i("poolpart"), table=i("table"), index=2, role=PartitionRole.DATA, size=None),
        Filesystem(id=i("espfs"), device=i("esp"), kind=FilesystemType.VFAT, label="ESP"),
        ZfsPool(id=i("pool"), vdevs=(i("poolpart"),), name="zpcala", encrypted=True),
        ZfsDataset(id=i("ds-root"), pool=i("pool"), name="ROOT/gentoo/root"),
        Mountpoint(id=i("mnt-root"), source=i("ds-root"), path=PurePosixPath("/")),
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/efi")),
    ]


def encrypted_root() -> list[Node]:
    """The same layout with LUKS between the partition and its filesystem, for
    anything that only makes sense when there is a passphrase prompt."""
    return [
        Existing(id=i("disk"), selector="/dev/disk/by-id/virtio-target", wipe=True),
        PartitionTable(id=i("table"), disk=i("disk"), table=TableType.GPT),
        Partition(id=i("esp"), table=i("table"), index=1, role=PartitionRole.ESP, size=Size.parse("512MiB")),
        Partition(id=i("rootpart"), table=i("table"), index=2, role=PartitionRole.DATA, size=None),
        Filesystem(id=i("espfs"), device=i("esp"), kind=FilesystemType.VFAT, label="ESP"),
        Luks(id=i("crypt"), backing=i("rootpart"), name="root", passphrase_file="/run/keys/root"),
        Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.EXT4, label="gentoo"),
        Mountpoint(id=i("mnt-root"), source=i("rootfs"), path=PurePosixPath("/")),
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/efi")),
    ]


def unlockable_root() -> list[Node]:
    """`encrypted_root` with a /boot outside the container.

    GRUB reads its kernel from /boot, so with /boot on the encrypted root it
    asks for the passphrase at the physical console and the initramfs that
    serves the ssh daemon never starts. `vm-unlock` spent three rounds proving
    it: the console held `Enter passphrase for hd0,gpt2` at fifty-one minutes.
    """
    return [
        Existing(id=i("disk"), selector="/dev/disk/by-id/virtio-target", wipe=True),
        PartitionTable(id=i("table"), disk=i("disk"), table=TableType.GPT),
        Partition(id=i("esp"), table=i("table"), index=1, role=PartitionRole.ESP, size=Size.parse("512MiB")),
        Partition(id=i("bootpart"), table=i("table"), index=2, role=PartitionRole.DATA, size=Size.parse("1GiB")),
        Partition(id=i("rootpart"), table=i("table"), index=3, role=PartitionRole.DATA, size=None),
        Filesystem(id=i("espfs"), device=i("esp"), kind=FilesystemType.VFAT, label="ESP"),
        Filesystem(id=i("bootfs"), device=i("bootpart"), kind=FilesystemType.EXT4, label="BOOT"),
        Luks(id=i("crypt"), backing=i("rootpart"), name="root", passphrase_file="/run/keys/root"),
        Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.EXT4, label="gentoo"),
        Mountpoint(id=i("mnt-root"), source=i("rootfs"), path=PurePosixPath("/")),
        Mountpoint(id=i("mnt-boot"), source=i("bootfs"), path=PurePosixPath("/boot")),
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/efi")),
    ]


def config(nodes: list[Node] | None = None) -> InstallConfig:
    return InstallConfig(
        disk=DiskConfig(graph=DeviceGraph.build(nodes or ext4_on_gpt()), root=i("mnt-root")),
        kernel=KernelConfig(source=KernelSource.DIST_SOURCE),
        bootloader=BootloaderConfig(kind=Bootloader.GRUB, firmware=Firmware.UEFI),
        # An empty hash locks root, and `compat` refuses a system nothing can
        # log into. Every layout here is meant to be installable.
        system=SystemConfig(root_password_hash="$6$gentooinst$IR3GrdJ862XljQYDqocr4tKniIRDIT.jQNFzIrHE3U75H6B6YSWZoSYoVd5edSHpqaYBdiNfXHCoIPRVgb9lT/"),
    )


def running_layout() -> StorageLayout:
    """A UEFI machine whose root is a plain filesystem on a partition.

    What `probe.storage_layout()` reads on the guests the conversion fixture
    runs against, so a test can derive a conversion without a machine.
    """
    return StorageLayout(
        root_device="/dev/vda2",
        root_filesystem_type="ext4",
        root_uuid="8f1c0a2e-0000-4000-8000-000000000001",
        root_on_lvm=False,
        root_on_luks=False,
        root_on_mdraid=False,
        root_below_device="/dev/vda",
        boot_device="/dev/vda2",
        boot_filesystem_type="ext4",
        boot_same_filesystem=True,
        esp_device="/dev/vda1",
        esp_mountpoint="/efi",
        uefi=True,
        root_free_bytes=20 * 2**30,
    )
