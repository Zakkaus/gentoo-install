# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from typing import Sequence

from gentoo_install.errors import CommandFailed, ConfigError, InvalidLayout
from gentoo_install.plan.operations import CommandOutput
from gentoo_install.model.config import DiskMode, InstallConfig
from gentoo_install.model.device import (
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
    TableType,
    VolumeGroup,
    ZfsDataset,
    ZfsPool,
)
from gentoo_install.exec.config import load
from gentoo_install.model.size import Size
from gentoo_install.plan import disk, system
from gentoo_install.plan.operations import Stage

from .layouts import config, ext4_on_gpt, i, zfs_root
from .recorder import Recorder


def apply_all(nodes: list[Node]) -> Recorder:
    recorder = Recorder()
    for operation in disk.build(config(nodes)):
        operation.apply(recorder)
    return recorder


def apply_installation(installation: InstallConfig) -> Recorder:
    recorder = Recorder()
    for operation in disk.build(installation):
        operation.apply(recorder)
    return recorder


def image_installation() -> InstallConfig:
    installation = config()
    image = "/var/tmp/target.raw"
    graph = DeviceGraph.build(
        replace(node, selector=image) if isinstance(node, Existing) else node
        for node in installation.disk.graph.nodes.values()
    )
    return replace(
        installation,
        disk=replace(
            installation.disk,
            graph=graph,
            mode=DiskMode.IMAGE,
            image=image,
            size=Size.parse("20GiB"),
            wipe=True,
        ),
    )


def test_an_image_is_attached_before_partitioning_and_detached_after_unmounting() -> None:
    installation = image_installation()
    operations = disk.build(installation)
    assert isinstance(operations[0], disk.CreateImage)
    finished = disk.finish(installation)
    assert isinstance(finished[-1], disk.DetachImage)
    assert isinstance(finished[-2], disk.UnmountTarget)


def test_an_image_refuses_an_existing_file_unless_wipe_is_enabled() -> None:
    image = "/var/tmp/target.raw"
    create = disk.CreateImage(
        device=i("disk"), image=image, size=Size.parse("20GiB"), wipe=False
    )
    recorder = Recorder(existing_paths={image}, replies={"losetup": "/dev/loop7\n"})
    with pytest.raises(ConfigError, match="disk.wipe"):
        create.apply(recorder)
    assert recorder.commands == [("test", "-e", image)]

    wiping = replace(create, wipe=True)
    wiping.apply(recorder)
    assert recorder.commands[-2:] == [
        ("truncate", "--size", str(Size.parse("20GiB").bytes), image),
        ("losetup", "--find", "--show", "--partscan", image),
    ]
    assert recorder.device_path(i("disk")) == "/dev/loop7"
    assert "loop device" in create.describe()

    detach = disk.DetachImage(device=i("disk"), image=image)
    assert detach.releases_the_machine
    detach.apply(recorder)
    assert recorder.commands[-1] == ("losetup", "--detach", "/dev/loop7")
    assert recorder.image_device_path(i("disk")) is None


def test_a_gpt_partition_is_created_with_its_index_type_and_size() -> None:
    recorder = apply_all(ext4_on_gpt())
    created = recorder.argv_starting("sgdisk")
    assert created[0][:3] == ("sgdisk", "--zap-all", "/dev/mapper/disk")
    # `512M`, not `512MiB`: sgdisk has no byte suffix and reads a bare
    # number as sectors, so the readable form is not the one it takes.
    assert created[1][1:3] == ("--new=1:0:+512M", "--typecode=1:ef00")
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


def test_every_filesystem_names_its_label_with_an_option_that_tool_takes() -> None:
    """`MKFS` and `LABEL_OPTION` were two tables keyed the same way and
    indexed on one line, so a filesystem added to one and not the other raised
    `KeyError` with the disk already partitioned. One entry carries both, and
    every entry has to produce a command that names the label.
    """
    from gentoo_install.model.device import FilesystemType
    from gentoo_install.plan.disk import MKFS, MakeFilesystem

    assert set(MKFS) == set(FilesystemType)
    for kind in FilesystemType:
        recorder = Recorder()
        MakeFilesystem(
            filesystem=i("rootfs"), device=i("rootpart"), kind=kind, label="tag"
        ).apply(recorder)
        argv = recorder.only(MKFS[kind].argv[0])
        assert argv[-1] == "/dev/mapper/rootpart", kind
        assert argv[-2] == "tag", kind
        # The option, not an empty string: `["", "tag"]` is what an entry with
        # no label option would hand the tool.
        assert argv[-3].startswith("-") and len(argv[-3]) > 1, kind


@pytest.mark.parametrize(
    ("name", "command"),
    [("ext2", "mkfs.ext2"), ("ext3", "mkfs.ext3")],
)
def test_each_legacy_ext_fixture_uses_its_mkfs_command(name: str, command: str) -> None:
    installation = load(Path("tests/fixtures") / f"{name}.toml")
    made = apply_installation(installation).argv_starting(command)
    assert made == ((command, "-F", "-L", "root", "/dev/mapper/rootpart"),)


@pytest.mark.parametrize("name", ["ext2", "ext3"])
def test_each_legacy_ext_fixture_writes_its_fstab_entry(name: str) -> None:
    installation = load(Path("tests/fixtures") / f"{name}.toml")
    recorder = Recorder()
    system.WriteFstab(entries=system.fstab_entries(installation)).apply(recorder)
    lines = recorder.files[PurePosixPath("/etc/fstab")].splitlines()
    assert lines[1] == f"UUID=uuid-of-rootpart\t/\t{name}\tdefaults\t0\t1"


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


def test_a_btrfs_scratch_unmount_failure_stops_the_install() -> None:
    class UnmountFails(Recorder):
        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            if argv[0] == "umount":
                self.commands.append(tuple(argv))
                return CommandOutput("target is busy", 1)
            return super().run(argv, check=check, input_text=input_text)

    operation = disk.CreateSubvolume(
        subvolume=i("sub-root"), device=i("rootfs"), name="@"
    )
    with pytest.raises(CommandFailed, match="umount .*btrfs-top exited 1"):
        operation.apply(UnmountFails())


def test_a_btrfs_scratch_unmount_does_not_replace_creation_failure() -> None:
    class BothFail(Recorder):
        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            if argv[0] == "umount":
                self.commands.append(tuple(argv))
                return CommandOutput("target is busy", 1)
            if argv[0] == "btrfs":
                raise CommandFailed("btrfs subvolume create exited 1: already exists")
            return super().run(argv, check=check, input_text=input_text)

    operation = disk.CreateSubvolume(
        subvolume=i("sub-root"), device=i("rootfs"), name="@"
    )
    with pytest.raises(CommandFailed, match="already exists"):
        operation.apply(BothFail())


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
    """Two different inputs, not the same call twice: comparing
    `topological(graph)` with itself passes for any deterministic
    implementation, including one that has lost its tie-break and creates a
    later partition in the space an earlier one wanted.
    """
    nodes = ext4_on_gpt()
    forwards = [node.id for node in disk.topological(DeviceGraph.build(nodes))]
    backwards = [node.id for node in disk.topological(DeviceGraph.build(list(reversed(nodes))))]
    assert forwards == backwards, (forwards, backwards)

    # And the property the tie-break exists for: `sgdisk --new=N:0:+size`
    # takes what is free when it runs, so the partitions are created in index
    # order. Named so that the index and the id disagree — with the plain
    # fixture both orders agree and sorting by id alone passes.
    disagreeing: list[Node] = [
        Existing(id=i("disk"), selector="/dev/disk/by-id/virtio-target", wipe=True),
        PartitionTable(id=i("table"), disk=i("disk"), table=TableType.GPT),
    ]
    disagreeing += [
        Partition(id=i("aaa"), table=i("table"), index=2, role=PartitionRole.DATA, size=None),
        Partition(
            id=i("zzz"),
            table=i("table"),
            index=1,
            role=PartitionRole.ESP,
            size=Size.parse("512MiB"),
        ),
    ]
    indexes = [
        node.index
        for node in disk.topological(DeviceGraph.build(disagreeing))
        if isinstance(node, Partition)
    ]
    assert indexes == [1, 2], indexes


def test_every_disk_operation_lands_in_a_disk_stage() -> None:
    allowed = {Stage.PARTITION, Stage.ARRAY, Stage.FORMAT, Stage.ZFS, Stage.MOUNT}
    assert {operation.stage for operation in disk.build(config(zfs_root()))} <= allowed


def test_the_table_is_reread_without_parted_installed() -> None:
    """`partprobe` comes from parted, which a gpt-only medium has no other
    reason to carry, and a table nobody rereads leaves no partition nodes."""
    recorder = Recorder(failures={"partprobe"})
    disk.RereadPartitionTable(disk=i("disk")).apply(recorder)
    assert ("blockdev", "--rereadpt", "/dev/mapper/disk") in recorder.commands
    assert recorder.degraded("partprobe")

    plain = Recorder()
    disk.RereadPartitionTable(disk=i("disk")).apply(plain)
    assert not any(argv[0] == "blockdev" for argv in plain.commands)


def test_a_volume_with_no_size_is_created_after_the_sized_ones() -> None:
    """`lvcreate -l 100%FREE` takes what is free when it runs, so creating the
    unsized volume first leaves the sized one with nothing."""
    described = [
        operation.describe()
        for operation in disk.build(load(Path("tests/fixtures/vm-lvm.toml")))
        if "logical volume" in operation.describe()
    ]
    assert len(described) == 2
    assert "16GiB" in described[0]
    assert "the rest of the group" in described[1]


def test_a_failed_run_can_be_started_again() -> None:
    """An install that stopped halfway leaves the target mounted, containers
    open and arrays assembled, and every one makes the disk busy at wipefs."""
    released = [
        operation
        for operation in disk.build(load(Path("tests/fixtures/vm-luks.toml")))
        if isinstance(operation, disk.ReleaseTarget)
    ]
    assert len(released) == 1
    recorder = Recorder()
    released[0].apply(recorder)
    ran = [argv[0] for argv in recorder.commands]
    assert ran[0] == "umount"
    assert ("cryptsetup", "close", "root") in recorder.commands


def test_the_teardown_closes_each_device_before_the_one_it_sits_on() -> None:
    """A fixed sequence of kinds gets one nesting wrong, and both occur: a
    volume group on a container and a container on a logical volume."""
    from gentoo_install.plan.disk import _teardown

    base = [
        Existing(id=i("d"), selector="/dev/vda", wipe=True),
        PartitionTable(id=i("t"), disk=i("d"), table=TableType.GPT),
        Partition(id=i("p"), table=i("t"), index=1, role=PartitionRole.LVM, size=None),
    ]
    on_luks = DeviceGraph.build([
        *base,
        Luks(id=i("c"), backing=i("p"), name="crypt"),
        VolumeGroup(id=i("vg"), members=(i("c"),), name="vg"),
        LogicalVolume(id=i("lv"), group=i("vg"), name="root", size=None),
    ])
    assert [one[0] for one in _teardown(on_luks)] == ["vgchange", "cryptsetup"]

    on_lvm = DeviceGraph.build([
        *base,
        VolumeGroup(id=i("vg"), members=(i("p"),), name="vg"),
        LogicalVolume(id=i("lv"), group=i("vg"), name="root", size=None),
        Luks(id=i("c"), backing=i("lv"), name="crypt"),
    ])
    assert [one[0] for one in _teardown(on_lvm)] == ["cryptsetup", "vgchange"]


def test_only_the_boot_environment_is_left_unmounted_at_boot() -> None:
    """`zfs mount -a` and `zfs-mount-generator` skip `canmount=noauto`, so a
    `/home` dataset marked that way came up empty. The values follow the
    dataset array of `calamares-settings-gig`'s `zfs.conf`."""
    from pathlib import PurePosixPath

    from gentoo_install.plan.disk import CreateDataset

    holder = CreateDataset(dataset=i("ds"), name="rpool/ROOT", mountpoint=None)
    root = CreateDataset(dataset=i("ds"), name="rpool/ROOT/gentoo", mountpoint=PurePosixPath("/"))
    home = CreateDataset(
        dataset=i("ds"), name="rpool/ROOT/gentoo/home", mountpoint=PurePosixPath("/home")
    )
    assert (holder.canmount(), root.canmount(), home.canmount()) == ("off", "noauto", "on")

    recorder = Recorder()
    home.apply(recorder)
    assert "canmount=on" in recorder.only("zfs", "create")


def test_the_downloaded_stage3_does_not_ship_with_the_installed_system() -> None:
    """The archive lands on the target because the work directory is a tmpfs,
    so without this the machine keeps a multi-gigabyte tarball, its DIGESTS and
    the marker saying it was verified."""
    from gentoo_install.plan.disk import STAGE3_CACHE, DiscardStage3
    from gentoo_install.plan.operations import Stage

    operations = disk.finish(config())
    discard = next(one for one in operations if isinstance(one, DiscardStage3))
    assert discard.stage is Stage.FINISH
    # Before the unmount: nothing can be written to the target after it.
    assert operations.index(discard) < operations.index(
        next(one for one in operations if isinstance(one, disk.UnmountTarget))
    )
    recorder = Recorder()
    discard.apply(recorder)
    assert recorder.only("rm")[-1] == f"/{STAGE3_CACHE}"


def test_a_pool_of_several_devices_says_how_they_are_joined() -> None:
    """`zpool create p a b` with no keyword stripes them and survives losing
    neither, and an operator who gave two disks almost always meant a mirror.

    The rule is exercised on its own: a ZFS root drags in the bootloader rules
    with it, and those are another table's business.
    """
    from gentoo_install.model.config import InstallConfig
    from gentoo_install.model.device import DeviceId, ZfsPool, ZfsTopology
    from gentoo_install.model.validate import _pool_problems

    def with_pool(vdevs: tuple[DeviceId, ...], topology: ZfsTopology) -> InstallConfig:
        nodes = [
            replace(node, vdevs=vdevs, topology=topology) if isinstance(node, ZfsPool) else node
            for node in zfs_root()
        ]
        return config(nodes)

    one, two = (i("poolpart"),), (i("poolpart"), i("esp"))
    assert _pool_problems(with_pool(one, ZfsTopology.STRIPE)) == []
    assert "no topology" in " ".join(_pool_problems(with_pool(two, ZfsTopology.STRIPE)))
    assert _pool_problems(with_pool(two, ZfsTopology.MIRROR)) == []

    # Each topology carries what it needs, and the check fires before the disks
    # are touched rather than in `zpool create` after they are partitioned.
    assert "at least 3" in " ".join(_pool_problems(with_pool(two, ZfsTopology.RAIDZ2)))


def test_the_topology_keyword_goes_before_the_devices() -> None:
    """`zpool create pool mirror a b`. A stripe has no keyword at all: the
    devices follow the pool name directly."""
    from gentoo_install.model.device import ZfsPool, ZfsTopology
    from gentoo_install.plan.disk import CreateZpool

    for topology, expected in (
        (ZfsTopology.STRIPE, None),
        (ZfsTopology.MIRROR, "mirror"),
        (ZfsTopology.RAIDZ1, "raidz1"),
    ):
        recorder = Recorder()
        CreateZpool(
            pool=i("pool"),
            vdevs=(i("a"), i("b")),
            name="rpool",
            topology=topology,
            encrypted=False,
        ).apply(recorder)
        argv = recorder.only("zpool", "create")
        after = argv[argv.index("rpool") + 1 :]
        if expected is None:
            assert after[0].startswith("/dev/"), topology
        else:
            assert after[0] == expected, topology
            assert after[1].startswith("/dev/")
    # Every member of the table has a minimum, so a new one cannot be added
    # without saying how many devices it takes.
    assert {one: one.minimum for one in ZfsTopology}.keys() == set(ZfsTopology)


def test_an_array_has_enough_members_for_the_level_it_names() -> None:
    """`mdadm --create` refuses a raid5 of two, and it refuses it after the
    disks have already been partitioned."""
    from gentoo_install.model.device import MdRaid, RaidLevel
    from gentoo_install.model.validate import _array_problems

    installation = load(Path("tests/fixtures/vm-mdraid.toml"))
    graph = installation.disk.graph

    def at_level(level: RaidLevel) -> list[str]:
        nodes = [
            replace(node, level=level) if isinstance(node, MdRaid) else node
            for node in graph.nodes.values()
        ]
        return _array_problems(replace(installation, disk=replace(
            installation.disk, graph=DeviceGraph.build(nodes)
        )))

    assert at_level(RaidLevel.RAID1) == []
    assert "at least 3" in " ".join(at_level(RaidLevel.RAID5))
    assert "at least 4" in " ".join(at_level(RaidLevel.RAID6))


def test_an_entry_is_deleted_with_the_tool_that_reads_the_table() -> None:
    """`sgdisk` reads an msdos label, converts it to GPT in memory and writes
    that back, so deleting one entry with it takes the other operating system
    on the disk with it."""
    from gentoo_install.model.device import DeviceId, TableType

    for kind, expected in (
        (TableType.GPT, ("sgdisk", "--delete=2")),
        (TableType.MBR, ("parted", "--script")),
    ):
        recorder = Recorder()
        disk.CreatePartitionTable(
            table=DeviceId("table"),
            disk=DeviceId("disk"),
            kind=kind,
            create=False,
            remove=(2,),
        ).apply(recorder)
        assert recorder.commands[0][:2] == expected, kind
        if kind is TableType.MBR:
            assert recorder.commands[0][-2:] == ("rm", "2")


def test_a_swap_partition_makes_preflight_ask_for_mkswap() -> None:
    """`MakeSwap` runs it, and only filesystems contributed to the list, so a
    medium without it passed and died after the disks were partitioned."""
    from gentoo_install.exec import preflight
    from gentoo_install.exec.config import load

    wanted = preflight.required_commands(load(Path("tests/fixtures/vm-bios.toml")))
    assert {"mkswap", "swapoff"} <= wanted


def test_the_lvm_check_names_the_binaries_the_plan_runs() -> None:
    """A medium carrying lvm2 as one multicall binary without its symlinks
    passed on `lvm` and then died at `pvcreate`."""
    from gentoo_install.exec import preflight
    from gentoo_install.exec.config import load

    wanted = preflight.required_commands(load(Path("tests/fixtures/vm-lvm.toml")))
    assert {"pvcreate", "vgcreate", "lvcreate"} <= wanted


#: `findmnt --noheadings --list --output TARGET,SOURCE` as util-linux
#: 2.42.2 prints it, padding and all. The rows are in mount order, which
#: is what decides the answer: the last one at or above the mountpoint is
#: the mount a path lookup reaches.
MOUNT_TABLE_VISIBLE: str = (
    "/                                                 rpool/ROOT/gentoo\n"
    "/mnt/gentoo                                       rpool/ROOT/gentoo\n"
    "/mnt/gentoo/home                                  rpool/ROOT/gentoo/home\n"
)

#: The same table after `zfs create` mounted the dataset and the root went
#: over it. Measured under `unshare --mount` on 2026-08-21: the kernel keeps
#: the covered row, `findmnt --target` still answers with its source, and
#: `ls` on the path answers `No such file or directory`.
MOUNT_TABLE_HIDDEN: str = (
    "/                                                 rpool/ROOT/gentoo\n"
    "/mnt/gentoo/home                                  zpcala/ROOT/gentoo/home\n"
    "/mnt/gentoo                                       zpcala/ROOT/gentoo/root\n"
)


def test_a_dataset_already_mounted_is_left_alone() -> None:
    """`zfs create` mounts a dataset the moment it is given a mountpoint, so
    `zfs mount` on it answers `filesystem already mounted` and stops the
    install. zfs-zbm reached that on `zpcala/ROOT/gentoo/home`."""
    from gentoo_install.plan.disk import MountZfsDataset

    where = PurePosixPath("/home")
    told = MountZfsDataset(mountpoint=i("mnt-home"), name="rpool/ROOT/gentoo/home", path=where)

    # Asked of ZFS, not of the path: the mountpoint property carries the
    # target prefix during an install, so `/home` names a directory on the
    # installing system and says nothing about the dataset.
    fresh = Recorder()
    fresh.replies["zfs"] = "no\n"
    told.apply(fresh)
    assert ("zfs", "mount", "rpool/ROOT/gentoo/home") in fresh.commands

    # The root was mounted before the dataset, so the dataset's own row is the
    # last one at or above the mountpoint and a lookup there reaches it.
    already = Recorder()
    already.replies["zfs"] = "yes\n"
    already.replies["findmnt"] = MOUNT_TABLE_VISIBLE
    told.apply(already)
    assert not any(one[:2] in {("zfs", "mount"), ("zfs", "unmount")} for one in already.commands)


def test_a_zfs_child_hidden_by_the_root_is_remounted_after_it() -> None:
    nodes = zfs_root()
    nodes += [
        ZfsDataset(id=i("ds-home"), pool=i("pool"), name="ROOT/gentoo/home"),
        Mountpoint(id=i("mnt-home"), source=i("ds-home"), path=PurePosixPath("/home")),
    ]
    operations = disk.build(config(nodes))
    root = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, disk.MountZfsDataset) and operation.path == PurePosixPath("/")
    )
    home = next(
        (index, operation)
        for index, operation in enumerate(operations)
        if isinstance(operation, disk.MountZfsDataset)
        and operation.path == PurePosixPath("/home")
    )
    assert root < home[0]

    # The dataset was mounted at create time and the root went over it in this
    # stage, so its own row is still in the table and the root's row follows.
    hidden = Recorder(replies={"zfs": "yes\n", "findmnt": MOUNT_TABLE_HIDDEN})
    home[1].apply(hidden)
    assert (
        "findmnt",
        "--noheadings",
        "--list",
        "--output",
        "TARGET,SOURCE",
    ) in hidden.commands
    assert hidden.argv_starting("zfs", "unmount") == (
        ("zfs", "unmount", "zpcala/ROOT/gentoo/home"),
    )
    assert hidden.argv_starting("zfs", "mount") == (
        ("zfs", "mount", "zpcala/ROOT/gentoo/home"),
    )


#: What `parted --machine --script <disk> unit B print free` printed for a
#: 4 GiB MBR image holding one 1 GiB partition at 1MiB, captured on this
#: machine. Kept whole: the free lines repeat the number `1`, so the marker in
#: the fifth field is what they have to be read by.
PARTED_PRINT_FREE: str = """BYT;
/home/zakk/.cache/mbrtest/disk.img:4294967296B:file:512:512:msdos::;
1:16384B:1048575B:1032192B:free;
1:1048576B:1074790399B:1073741824B:::;
1:1074790400B:4294967295B:3220176896B:free;
"""


def _edited_mbr(size: Size | None = None) -> DeviceGraph:
    """One retained partition on the disk, one added by the configuration."""
    from pathlib import PurePosixPath

    return DeviceGraph.build(
        [
            Existing(id=DeviceId("d"), selector="/dev/null", wipe=False),
            PartitionTable(
                id=DeviceId("t"),
                disk=DeviceId("d"),
                table=TableType.MBR,
                create=False,
            ),
            Partition(
                id=DeviceId("new"),
                table=DeviceId("t"),
                index=2,
                role=PartitionRole.DATA,
                size=size if size is not None else Size(1024**3),
            ),
            Filesystem(id=DeviceId("fs"), device=DeviceId("new"), kind=FilesystemType.EXT4),
            Mountpoint(id=DeviceId("m"), source=DeviceId("fs"), path=PurePosixPath("/")),
        ]
    )


def test_a_partition_added_to_an_edited_mbr_goes_after_the_ones_on_the_disk() -> None:
    """A retained partition is not a model node, so summing the model's own
    gave 1MiB and `parted` answered `the closest location we can manage is
    1048kB to 1048kB` — after the removals in the same plan had already been
    committed."""
    from gentoo_install.exec.probe import Probe
    from gentoo_install.exec.runner import Result, Runner
    from gentoo_install.plan.disk import _start_of

    class Answering(Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            timeout: float | None = None,
        ) -> Result:
            said = PARTED_PRINT_FREE if argv[0] == "parted" else ""
            return Result(argv=tuple(argv), returncode=0, stdout=said, stderr="", seconds=0.0)

    read = Probe(runner=Answering(log=lambda line: None), work=Path("/tmp"))
    free = read.free_extents("/dev/null")
    assert free == (Extent(start=16384, end=1048575), Extent(start=1074790400, end=4294967295)), free

    graph = _edited_mbr()
    facts = StorageFacts(free_extents={DeviceId("t"): free})
    added = next(one for one in graph.of_type(Partition) if one.id == DeviceId("new"))
    start = _start_of(graph, added, facts)
    # The retained partition ends at 1074790399, so anything at or below it
    # overlaps what the operator kept.
    assert start.bytes > 1074790399, start
    assert start.is_aligned(), start


def test_a_partition_that_fits_no_free_extent_is_refused_before_anything_runs() -> None:
    """Refused while the table is unchanged: the removals of the same plan run
    first and stay committed when a later creation fails."""
    from gentoo_install.errors import InvalidLayout
    from gentoo_install.plan.disk import _start_of

    graph = _edited_mbr(size=Size(4 * 1024**3))
    facts = StorageFacts(
        free_extents={DeviceId("t"): (Extent(start=1048576, end=2097151),)}
    )
    added = next(one for one in graph.of_type(Partition) if one.id == DeviceId("new"))
    with pytest.raises(InvalidLayout):
        _start_of(graph, added, facts)


def test_a_table_written_from_scratch_still_starts_at_the_first_offset() -> None:
    """A new table needs no runtime extents; sgdisk finds GPT space itself."""
    from gentoo_install.plan.disk import FIRST_OFFSET, _start_of

    graph = DeviceGraph.build(ext4_on_gpt())
    first = next(
        one for one in graph.of_type(Partition) if one.index == 1
    )
    assert _start_of(graph, first, StorageFacts()) == FIRST_OFFSET


def test_the_whole_plan_passes_one_facts_value_to_validation_and_disk_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gentoo_install.data import load_catalog
    from gentoo_install.model.config import InstallConfig
    from gentoo_install.plan import build as plan_build
    from gentoo_install.plan import disk as plan_disk
    from gentoo_install.plan.operations import Operation

    seen: list[StorageFacts] = []

    def validated(
        installation: InstallConfig,
        *,
        storage_facts: StorageFacts,
        supports_v3: bool | None = None,
    ) -> None:
        seen.append(storage_facts)

    def disk_operations(
        installation: InstallConfig, storage_facts: StorageFacts
    ) -> list[Operation]:
        seen.append(storage_facts)
        return []

    monkeypatch.setattr(plan_build, "validate", validated)
    monkeypatch.setattr(plan_disk, "build", disk_operations)
    facts = StorageFacts()

    plan_build.build(config(ext4_on_gpt()), load_catalog(), storage_facts=facts)

    assert len(seen) == 2
    assert seen[0] is facts and seen[1] is facts


def test_a_pool_still_busy_after_the_lazy_unmount_is_exported_on_a_later_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`umount --lazy` returns before the tree is detached, so the export that
    follows it read `cannot export 'rpool': pool is busy` on a guest whose
    install had otherwise finished."""
    from gentoo_install.errors import CommandFailed
    from gentoo_install.plan.operations import CommandOutput

    class Busy(Recorder):
        refusals: int = 2
        attempts: int = 0

        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            if tuple(argv[:2]) == ("zpool", "export"):
                self.attempts += 1
                if self.refusals:
                    self.refusals -= 1
                    raise CommandFailed("zpool export rpool exited 1: pool is busy")
            return super().run(argv, check=check, input_text=input_text)

    monkeypatch.setattr(disk, "EXPORT_PAUSE", 0.0)
    operation = disk.UnmountTarget(pools=("rpool",))

    # Plain before lazy: a lazy unmount leaves the datasets mounted as far as
    # the kernel is concerned and `zpool export` then reads `pool is busy` for
    # as long as the references last.
    class Clean(Busy):
        """`findmnt` finds nothing: the plain unmount cleared the target."""

        mounted: bool = False

        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            if argv[0] == "findmnt":
                return CommandOutput("/mnt/gentoo" if self.mounted else "", 0 if self.mounted else 1)
            if tuple(argv[:2]) == ("zfs", "list"):
                return CommandOutput("rpool\nrpool/ROOT\nrpool/ROOT/gentoo\nrpool/home\n", 0)
            return super().run(argv, check=check, input_text=input_text)

    ordered = Clean()
    ordered.refusals = 0
    operation.apply(ordered)
    unmounts = [one for one in ordered.commands if one and one[0] == "umount"]
    assert unmounts == [("umount", "--recursive", "/mnt/gentoo")], unmounts

    # The lazy fallback runs on what is still mounted, not on an exit code: a
    # recursive unmount that cleared everything but one already-gone submount
    # still exits 1, and the fallback then raised `not mounted` on a finished
    # install.
    held = Clean()
    held.mounted = True
    held.refusals = 0
    operation.apply(held)
    assert [one for one in held.commands if one and one[0] == "umount"] == [
        ("umount", "--recursive", "/mnt/gentoo"),
        ("umount", "--recursive", "--lazy", "/mnt/gentoo"),
    ]

    recorder = Clean()
    operation.apply(recorder)
    assert recorder.attempts == 3, recorder.attempts
    assert recorder.argv_starting("sleep") == (("sleep", "0"), ("sleep", "0"))

    # The pool's own datasets are unmounted first, deepest before shallowest: a
    # live environment that imported the pool mounts them at their `mountpoint`
    # property, which is outside the target tree the unmount above cleared.
    unmounted = [one[2] for one in recorder.commands if tuple(one[:2]) == ("zfs", "unmount")]
    assert unmounted == ["rpool/ROOT/gentoo", "rpool/ROOT", "rpool/home", "rpool"] or (
        unmounted[0] == "rpool/ROOT/gentoo" and unmounted[-1] == "rpool"
    ), unmounted

    # A non-dataset holder is not released by the installer, so export stops.
    zed = Clean()
    zed.refusals = disk.EXPORT_TRIES
    with pytest.raises(CommandFailed, match="holder is unknown"):
        operation.apply(zed)

    class MountedDataset(Clean):
        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            if tuple(argv[:2]) == ("zfs", "list"):
                return CommandOutput("rpool\tyes\n", 0)
            return super().run(argv, check=check, input_text=input_text)

    mounted = MountedDataset()
    mounted.refusals = disk.EXPORT_TRIES
    operation.apply(mounted)
    assert mounted.commands[-1] == ("zpool", "export", "-f", "rpool")

    # A pool that refuses every plain attempt stops the install.
    stubborn = Clean()
    stubborn.refusals = disk.EXPORT_TRIES
    with pytest.raises(CommandFailed):
        operation.apply(stubborn)


def test_a_pool_busy_with_nothing_mounted_is_named_rather_than_forced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forcing an export hides whatever holds the pool. A dataset of its own
    still mounted is a holder this operation can name, so that one is forced;
    anything else — `zed` in a live environment is one — is reported.
    """
    from gentoo_install.errors import CommandFailed
    from gentoo_install.plan.operations import CommandOutput

    class Stuck(Recorder):
        mounted_state = "no"

        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            if tuple(argv[:2]) == ("zpool", "export") and "-f" not in argv:
                raise CommandFailed("zpool export rpool exited 1: pool is busy")
            if tuple(argv[:2]) == ("zfs", "list"):
                return CommandOutput(f"rpool\t{self.mounted_state}\n", 0)
            return super().run(argv, check=check, input_text=input_text)

    monkeypatch.setattr(disk, "EXPORT_PAUSE", 0.0)
    operation = disk.UnmountTarget(pools=("rpool",))

    held = Stuck()
    held.mounted_state = "yes"
    operation.apply(held)
    assert ["zpool", "export", "-f", "rpool"] in [list(one) for one in held.commands]

    unknown = Stuck()
    unknown.mounted_state = "no"
    with pytest.raises(CommandFailed, match="holder is unknown"):
        operation.apply(unknown)
    assert not any("-f" in one for one in unknown.commands)


def _edited_mbr_taking_the_rest() -> DeviceGraph:
    """The same edited table, with the added partition asking for the rest.

    `_edited_mbr` substitutes a size for `None`, so the case this is about
    cannot be expressed through it.
    """
    from pathlib import PurePosixPath

    return DeviceGraph.build(
        [
            Existing(id=DeviceId("d"), selector="/dev/null", wipe=False),
            PartitionTable(
                id=DeviceId("t"),
                disk=DeviceId("d"),
                table=TableType.MBR,
                create=False,
            ),
            Partition(
                id=DeviceId("new"),
                table=DeviceId("t"),
                index=2,
                role=PartitionRole.DATA,
                size=None,
            ),
            Filesystem(id=DeviceId("fs"), device=DeviceId("new"), kind=FilesystemType.EXT4),
            Mountpoint(id=DeviceId("m"), source=DeviceId("fs"), path=PurePosixPath("/")),
        ]
    )


def test_an_unsized_partition_in_an_edited_table_stops_at_its_gap() -> None:
    """`100%` reaches to the end of the disk, past every partition the operator
    asked to keep, and `parted` refuses the whole edit. The planner already
    knows which gap the partition was placed in, so it carries where that gap
    ends."""
    from gentoo_install.plan.disk import _extent_end_of

    graph = _edited_mbr_taking_the_rest()
    gap = Extent(start=1074790400, end=4294967295)
    facts = StorageFacts(free_extents={DeviceId("t"): (gap,)})
    added = next(one for one in graph.of_type(Partition) if one.id == DeviceId("new"))

    limit = _extent_end_of(graph, added, facts)

    assert limit is not None, "an edited table bounds an unsized partition"
    assert limit.bytes <= gap.end + 1
    assert limit.bytes > gap.start


def test_an_unsized_partition_on_a_fresh_table_still_takes_the_rest() -> None:
    """A table this run writes has nothing beyond it to protect."""
    from gentoo_install.plan.disk import _extent_end_of

    graph = DeviceGraph.build(
        [
            replace(node, create=True) if isinstance(node, PartitionTable) else node
            for node in _edited_mbr_taking_the_rest().nodes.values()
        ]
    )
    added = next(one for one in graph.of_type(Partition) if one.id == DeviceId("new"))

    assert _extent_end_of(graph, added, StorageFacts()) is None


def test_a_partition_placed_in_a_gap_does_not_promise_the_rest_of_the_disk() -> None:
    """An edited table places an unsized partition inside one free extent, and
    `parted` is given that extent's end rather than `100%`. `--dry-run` said
    "the rest of the disk", which is the retained partitions' space as well —
    the exact claim the operator opened an edited table to avoid."""
    from gentoo_install.model.device import PartitionRole, TableType

    def placed(limit: Size | None) -> disk.CreatePartition:
        return disk.CreatePartition(
            partition=DeviceId("part"),
            disk=DeviceId("disk"),
            table_kind=TableType.MBR,
            index=2,
            role=PartitionRole.DATA,
            size=None,
            label="",
            start=Size(2 * 2**30),
            limit=limit,
        )

    bounded = placed(Size(10 * 2**30))
    assert "the rest of the disk" not in bounded.describe(), bounded.describe()
    assert str(Size(10 * 2**30)) in bounded.describe(), bounded.describe()

    # Negative control: a fresh table really does take the rest, and saying so
    # is right there.
    fresh = placed(None)
    assert "the rest of the disk" in fresh.describe(), fresh.describe()

    # Not a claim about the text alone: what parted is told has to match.
    recorder = Recorder()
    bounded.apply(recorder)
    ends = [one[-1] for one in recorder.commands if one[0] == "parted" and "mkpart" in one]
    assert ends == [str(Size(10 * 2**30))], recorder.commands


def test_a_zfs_probe_that_did_not_run_is_not_read_as_an_answer() -> None:
    """`check=False` keeps the failure out of the exception path and the runner
    merges stderr into stdout, so `zfs is not installed` arrived where `yes` or
    `no` belongs: the dataset was treated as unmounted, and the second probe
    failing the same way took the branch that unmounts it."""
    from gentoo_install.plan.disk import MountZfsDataset

    told = MountZfsDataset(
        mountpoint=i("mnt-home"), name="rpool/ROOT/gentoo/home", path=PurePosixPath("/home")
    )

    broken = Recorder()
    broken.replies["zfs"] = CommandOutput("zfs is not installed", 127)
    with pytest.raises(CommandFailed, match="whether rpool/ROOT/gentoo/home is mounted"):
        told.apply(broken)
    assert ("zfs", "mount", "rpool/ROOT/gentoo/home") not in broken.commands

    # The mount probe answers, and the one that reads what is at the path does
    # not: unmounting on that would take down a dataset nobody established was
    # hidden.
    half = Recorder()
    half.replies["zfs"] = "yes\n"
    half.replies["findmnt"] = CommandOutput("findmnt is not installed", 127)
    with pytest.raises(CommandFailed, match="what is mounted at /home"):
        told.apply(half)
    assert ("zfs", "unmount", "rpool/ROOT/gentoo/home") not in half.commands

    # Listing the whole table exits 0 whenever findmnt ran, so exit 1 is a
    # probe that did not run rather than a path carrying no mount.
    empty = Recorder()
    empty.replies["zfs"] = "yes\n"
    empty.replies["findmnt"] = CommandOutput("", 1)
    with pytest.raises(CommandFailed, match="what is mounted at /home"):
        told.apply(empty)
    assert ("zfs", "unmount", "rpool/ROOT/gentoo/home") not in empty.commands
