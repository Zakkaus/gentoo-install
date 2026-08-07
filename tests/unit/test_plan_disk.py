from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from gentoo_install.errors import InvalidLayout
from gentoo_install.model.device import (
    DeviceGraph,
    Filesystem,
    FilesystemType,
    Luks,
    MdRaid,
    Mountpoint,
    Node,
    Partition,
    PartitionRole,
    PartitionTable,
    RaidLevel,
    RaidMetadata,
    Subvolume,
    Swap,
    TableType,
    ZfsPool,
)
from gentoo_install.model.size import Size
from gentoo_install.plan import disk
from gentoo_install.plan.operations import Stage

from .layouts import config, ext4_on_gpt, i, zfs_root
from .recorder import Recorder


def apply_all(nodes: list[Node]) -> Recorder:
    recorder = Recorder()
    for operation in disk.build(config(nodes)):
        operation.apply(recorder)
    return recorder


def test_a_gpt_partition_is_created_with_its_index_type_and_size() -> None:
    recorder = apply_all(ext4_on_gpt())
    created = recorder.argv_starting("sgdisk")
    assert created[0][:3] == ("sgdisk", "--zap-all", "/dev/mapper/disk")
    assert created[1][1:3] == ("--new=1:0:+512MiB", "--typecode=1:ef00")
    assert created[2][1:3] == ("--new=2:0:0", "--typecode=2:8300")


def test_a_bios_boot_partition_gets_the_legacy_bootable_attribute() -> None:
    nodes = ext4_on_gpt()
    nodes.append(
        Partition(
            id=i("bios"), table=i("table"), index=3, role=PartitionRole.BIOS_BOOT, size=Size.parse("2MiB")
        )
    )
    argv = [command for command in apply_all(nodes).argv_starting("sgdisk") if "--new=3" in command[1]]
    assert "--attributes=3:set:2" in argv[0]


def test_an_mbr_partition_is_placed_by_offset_because_parted_needs_one() -> None:
    nodes: list[Node] = [
        node for node in ext4_on_gpt() if not isinstance(node, (PartitionTable, Mountpoint))
    ]
    nodes += [
        PartitionTable(id=i("table"), disk=i("disk"), table=TableType.MBR),
        Mountpoint(id=i("mnt-root"), source=i("rootfs"), path=PurePosixPath("/")),
    ]
    made = apply_all(nodes).argv_starting("parted")
    assert made[0][-2:] == ("mklabel", "msdos")
    assert made[1][-3:] == ("primary", "1MiB", "513MiB")
    assert made[-1][-3:] == ("primary", "513MiB", "100%")


def test_the_kernel_is_asked_to_reread_the_table_before_anything_uses_it() -> None:
    operations = disk.build(config(ext4_on_gpt()))
    reread = [n for n, operation in enumerate(operations) if isinstance(operation, disk.RereadPartitionTable)]
    partitions = [n for n, operation in enumerate(operations) if isinstance(operation, disk.CreatePartition)]
    formats = [n for n, operation in enumerate(operations) if isinstance(operation, disk.MakeFilesystem)]
    assert max(partitions) < min(reread) < min(formats)


def test_every_disk_is_reread_once_rather_than_once_per_partition() -> None:
    operations = disk.build(config(ext4_on_gpt()))
    assert len([o for o in operations if isinstance(o, disk.RereadPartitionTable)]) == 1


def test_luks_is_formatted_with_argon2id_and_opened_from_the_same_key_file() -> None:
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id != i("rootfs")]
    nodes += [
        Luks(id=i("crypt"), backing=i("rootpart"), name="root"),
        Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.EXT4),
    ]
    recorder = apply_all(nodes)
    formatted = recorder.only("cryptsetup", "luksFormat")
    opened = recorder.only("cryptsetup", "open")
    assert "--pbkdf" in formatted and formatted[formatted.index("--pbkdf") + 1] == "argon2id"
    assert "--type" in formatted and "luks2" in formatted
    assert formatted[formatted.index("--key-file") + 1] == opened[opened.index("--key-file") + 1]


def test_an_array_is_created_with_the_metadata_the_layout_asked_for() -> None:
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id not in {i("rootfs"), i("mnt-root")}]
    nodes += [
        Partition(id=i("second"), table=i("table"), index=3, role=PartitionRole.RAID, size=None),
        MdRaid(
            id=i("array"),
            members=(i("rootpart"), i("second")),
            level=RaidLevel.RAID1,
            name="root",
            metadata=RaidMetadata.V1_0,
        ),
        Filesystem(id=i("rootfs"), device=i("array"), kind=FilesystemType.EXT4),
        Mountpoint(id=i("mnt-root"), source=i("rootfs"), path=PurePosixPath("/")),
    ]
    argv = apply_all(nodes).only("mdadm", "--create")
    assert "--metadata=1.0" in argv
    assert "--level=raid1" in argv
    assert "--raid-devices=2" in argv


def test_a_filesystem_is_made_with_the_label_option_its_tool_uses() -> None:
    recorder = apply_all(ext4_on_gpt())
    vfat = recorder.only("mkfs.vfat")
    assert vfat[1:4] == ("-F", "32", "-n")
    assert "ESP" in vfat


def test_swap_is_made_and_then_left_alone() -> None:
    nodes = ext4_on_gpt()
    nodes.append(Swap(id=i("swap"), device=i("rootpart")))
    recorder = apply_all(nodes)
    assert recorder.argv_starting("mkswap")
    assert recorder.argv_starting("swapoff")
    assert not recorder.argv_starting("swapon")


def test_a_subvolume_is_created_through_a_scratch_mount_of_the_top_level() -> None:
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id not in {i("rootfs"), i("mnt-root")}]
    nodes += [
        Filesystem(id=i("rootfs"), device=i("rootpart"), kind=FilesystemType.BTRFS),
        Subvolume(id=i("sub-root"), filesystem=i("rootfs"), name="@"),
        Mountpoint(id=i("mnt-root"), source=i("sub-root"), path=PurePosixPath("/")),
    ]
    recorder = apply_all(nodes)
    assert recorder.only("btrfs", "subvolume", "create")[-1].endswith("/btrfs-top/@")
    assert recorder.argv_starting("umount")


def test_a_subvolume_mount_carries_both_its_options_and_its_subvolume() -> None:
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id not in {i("rootfs"), i("mnt-root")}]
    nodes += [
        Filesystem(id=i("rootfs"), device=i("rootpart"), kind=FilesystemType.BTRFS),
        Subvolume(id=i("sub-root"), filesystem=i("rootfs"), name="@"),
        Mountpoint(
            id=i("mnt-root"),
            source=i("sub-root"),
            path=PurePosixPath("/"),
            options=("compress=zstd:1",),
        ),
    ]
    argv = apply_all(nodes).only("mount", "--options")
    assert argv[2] == "compress=zstd:1,subvol=@"


def test_a_pool_is_created_under_the_target_with_encryption_only_when_asked() -> None:
    encrypted = apply_all(zfs_root()).only("zpool", "create")
    assert "-R" in encrypted and "/mnt/gentoo" in encrypted
    assert "encryption=on" in encrypted
    plain = [
        replace(node, encrypted=False) if isinstance(node, ZfsPool) else node for node in zfs_root()
    ]
    assert "encryption=on" not in apply_all(plain).only("zpool", "create")


def test_an_encrypted_pool_gets_its_passphrase_on_stdin() -> None:
    """`keylocation=prompt` reads it here and asks again at boot, which is what
    ZFSBootMenu prompts for."""
    recorder = Recorder()
    for operation in disk.build(config(zfs_root())):
        operation.apply(recorder)
    assert recorder.stdin == ["a passphrase"]

    plain = [replace(node, encrypted=False) if isinstance(node, ZfsPool) else node for node in zfs_root()]
    quiet = Recorder()
    for operation in disk.build(config(plain)):
        operation.apply(quiet)
    assert quiet.stdin == []


def test_a_dataset_is_created_with_its_parents() -> None:
    argv = apply_all(zfs_root()).argv_starting("zfs", "create")
    assert all("-p" in command for command in argv)


def test_a_reference_to_the_wrong_kind_of_node_is_named_rather_than_asserted() -> None:
    nodes: list[Node] = [
        node for node in ext4_on_gpt() if node.id not in {i("mnt-root"), i("rootfs")}
    ]
    nodes.append(Subvolume(id=i("sub"), filesystem=i("rootpart"), name="@"))
    with pytest.raises(InvalidLayout, match="filesystem"):
        disk.build(config(nodes))


def test_the_topological_order_is_the_same_every_time() -> None:
    graph = DeviceGraph.build(ext4_on_gpt())
    assert [node.id for node in disk.topological(graph)] == [
        node.id for node in disk.topological(graph)
    ]


def test_every_disk_operation_lands_in_a_disk_stage() -> None:
    allowed = {Stage.PARTITION, Stage.ARRAY, Stage.FORMAT, Stage.ZFS, Stage.MOUNT}
    assert {operation.stage for operation in disk.build(config(zfs_root()))} <= allowed
