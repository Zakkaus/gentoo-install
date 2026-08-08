"""The hand-written partition table.

Driven through the fake screen like every other widget, so the table can be
edited, validated and turned into a graph without a terminal or a disk.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from gentoo_install.errors import ValidationFailed
from gentoo_install.model import manual
from gentoo_install.model.config import (
    Bootloader,
    BootloaderConfig,
    DiskConfig,
    Firmware,
    InstallConfig,
)
from gentoo_install.model.device import (
    DeviceGraph,
    DeviceId,
    Filesystem,
    FilesystemType,
    Partition,
    Luks,
    Mountpoint,
    PartitionRole,
    Swap,
    ZfsDataset,
    ZfsPool,
)
from gentoo_install.model.size import Size
from gentoo_install.model.validate import validate
from gentoo_install.model.templates import Layout
from gentoo_install.tui import screens
from gentoo_install.tui.widgets import Outcome

from .fake_screen import FakeScreen
from .layouts import config, ext4_on_gpt, i
from .test_tui_app import context


def opened() -> screens.Context:
    at = context()
    at.manual = True
    return at


def index_of(items: list[str], label: str) -> int:
    return items.index(label)


def test_the_table_starts_from_something_that_already_boots() -> None:
    """A blank table is a worse starting point than one that installs: every
    layout needs the same first entries and only the sizes differ."""
    suggested = manual.suggest("/dev/vda", Firmware.UEFI)
    assert [entry.mountpoint for entry in suggested.slices] == ["/efi", "/"]
    graph, root = manual.build(suggested)
    assert root
    validate(config_from(graph, root))


def config_from(graph: DeviceGraph, root: DeviceId) -> InstallConfig:
    return replace(config(), disk=DiskConfig(graph=graph, root=root))


def test_a_partition_can_be_added_and_reaches_the_graph() -> None:
    at = opened()
    at.layout = manual.suggest("/dev/vda", Firmware.UEFI)
    at.layout.slices.append(
        manual.Slice(
            index=3,
            role=PartitionRole.DATA,
            size=Size.parse("20GiB"),
            filesystem=FilesystemType.XFS,
            mountpoint="/home",
        )
    )
    graph, root = manual.build(at.layout)
    mounted = sorted(str(node.path) for node in graph.of_type(Mountpoint))
    assert mounted == ["/", "/efi", "/home"]
    validate(config_from(graph, root))


def test_each_partition_chooses_its_own_filesystem() -> None:
    """`/` on btrfs and `/home` on xfs is a layout people actually want."""
    layout = manual.Layout(disk="/dev/vda", slices=[
        manual.Slice(index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                     filesystem=FilesystemType.VFAT, mountpoint="/efi"),
        manual.Slice(index=2, role=PartitionRole.DATA, size=Size.parse("30GiB"),
                     filesystem=FilesystemType.BTRFS, mountpoint="/"),
        manual.Slice(index=3, role=PartitionRole.DATA, size=None,
                     filesystem=FilesystemType.XFS, mountpoint="/home"),
    ])
    graph, root = manual.build(layout)
    kinds = {node.kind.value for node in graph.of_type(Filesystem)}
    assert kinds == {"vfat", "btrfs", "xfs"}
    validate(config_from(graph, root))


def test_a_table_with_no_root_says_so_rather_than_installing() -> None:
    at = opened()
    at.layout = manual.Layout(disk="/dev/vda", slices=[
        manual.Slice(index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                     filesystem=FilesystemType.VFAT, mountpoint="/efi"),
    ])
    assert "mounted at /" in screens._layout_problem(at, config())


def test_a_root_too_small_is_reported_in_the_table() -> None:
    """The validator's own sentence, so the table and the install row agree."""
    at = opened()
    at.layout = manual.Layout(disk="/dev/vda", slices=[
        manual.Slice(index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                     filesystem=FilesystemType.VFAT, mountpoint="/efi"),
        manual.Slice(index=2, role=PartitionRole.DATA, size=Size.parse("4GiB"),
                     filesystem=FilesystemType.EXT4, mountpoint="/"),
    ])
    assert "under the" in screens._layout_problem(at, config())


def test_a_swap_partition_needs_no_filesystem() -> None:
    layout = manual.Layout(disk="/dev/vda", slices=[
        manual.Slice(index=1, role=PartitionRole.SWAP, size=Size.parse("4GiB")),
        manual.Slice(index=2, role=PartitionRole.DATA, size=None,
                     filesystem=FilesystemType.EXT4, mountpoint="/"),
    ])
    graph, _ = manual.build(layout)
    assert len(graph.of_type(Swap)) == 1


def test_the_editor_lists_the_table_and_leaves_on_done() -> None:
    at = opened()
    at.layout = manual.suggest(at.choice.disk, Firmware.UEFI)
    screen = FakeScreen(keys=["KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "\n"], lines=30, columns=90)
    answer = screens.partitions_screen(screen, config(), at)
    assert answer.outcome is Outcome.CHOSE
    drawn = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "/efi" in drawn and "rest" in drawn


def test_the_purpose_menu_names_root() -> None:
    """A GPT type code is not what the operator is deciding: the maintainer
    opened this menu looking for root and found `data`."""
    labels = [one.label for one in manual.PURPOSES]
    assert "root" in labels
    assert {"esp", "boot", "home", "var", "swap"} <= set(labels)


def test_choosing_a_purpose_settles_the_mount_point_and_the_filesystem() -> None:
    """Everything the purpose decides moves together: `swap` has to drop the
    mount point and the filesystem the slice carried as `root`."""
    rooted = manual.Slice(index=1, role=PartitionRole.DATA, size=None,
                          filesystem=FilesystemType.EXT4, mountpoint="/")
    swap = next(one for one in manual.PURPOSES if one.key == "swap")
    changed = screens._apply_purpose(rooted, swap)
    assert changed.role is PartitionRole.SWAP
    assert changed.mountpoint == "" and changed.filesystem is None


def test_a_slice_knows_which_purpose_it_came_from() -> None:
    """Derived from the role and the mount point rather than stored, so the two
    can never disagree with the row the menu highlights."""
    home = manual.Slice(index=3, role=PartitionRole.DATA, size=None,
                        filesystem=FilesystemType.XFS, mountpoint="/home")
    assert manual.purpose_of(home).key == "home"
    odd = replace(home, mountpoint="/srv")
    assert manual.purpose_of(odd).key == "other"


def test_a_manual_table_can_put_the_root_on_zfs() -> None:
    """ZFS is a pool, not a `FilesystemType`, so the manual table reaches it
    through a purpose that makes the partition a pool member."""
    layout = manual.Layout(disk="/dev/vda", pool="rpool", slices=[
        # systemd-boot reads no pool, so the kernel has to sit on the esp and
        # the esp has to be where the kernel is installed.
        manual.Slice(index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                     filesystem=FilesystemType.VFAT, mountpoint="/boot"),
        manual.Slice(index=2, role=PartitionRole.ZFS, size=None, mountpoint="/"),
    ])
    graph, root = manual.build(layout)
    pool = graph.of_type(ZfsPool)[0]
    assert pool.name == "rpool" and not pool.encrypted
    assert [node.name for node in graph.of_type(ZfsDataset)] == ["ROOT/gentoo"]
    assert root
    validate(replace(config_from(graph, root),
                     bootloader=BootloaderConfig(kind=Bootloader.SYSTEMD_BOOT)))


def test_two_pool_members_make_one_pool() -> None:
    layout = manual.Layout(disk="/dev/vda", slices=[
        manual.Slice(index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                     filesystem=FilesystemType.VFAT, mountpoint="/efi"),
        manual.Slice(index=2, role=PartitionRole.ZFS, size=Size.parse("30GiB"), mountpoint="/"),
        manual.Slice(index=3, role=PartitionRole.ZFS, size=None, mountpoint="/home"),
    ])
    graph, _ = manual.build(layout)
    pools = graph.of_type(ZfsPool)
    assert len(pools) == 1 and len(pools[0].vdevs) == 2
    assert sorted(node.name for node in graph.of_type(ZfsDataset)) == ["ROOT/gentoo", "home"]


def test_a_pool_member_is_never_wrapped_in_luks() -> None:
    """The pool encrypts its own datasets; LUKS underneath as well would ask
    for two passphrases to reach one filesystem."""
    layout = manual.Layout(disk="/dev/vda", slices=[
        manual.Slice(index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                     filesystem=FilesystemType.VFAT, mountpoint="/efi"),
        manual.Slice(index=2, role=PartitionRole.ZFS, size=None, mountpoint="/",
                     passphrase_file="/run/keys/tui"),
    ])
    graph, _ = manual.build(layout)
    assert not graph.of_type(Luks)
    assert graph.of_type(ZfsPool)[0].encrypted


def test_every_field_of_a_partition_is_visible_with_its_value() -> None:
    """No answer behind a screen the operator has to guess is there: the
    maintainer could not find where to type a size."""
    at = opened()
    at.layout = manual.suggest("/dev/vda", Firmware.UEFI)
    entry = at.layout.slices[1]
    fields = screens._slice_fields(entry, manual.purpose_of(entry), at.translate)
    shown = {item.label: item.detail for item in fields}
    assert shown["Purpose"] == "root"
    assert shown["Filesystem"] == "ext4"
    assert shown["Mount point"] == "/"
    assert "Encryption" in shown and "Size" in shown and "Label" in shown


def test_a_field_that_does_not_apply_says_why() -> None:
    swap = manual.Slice(index=2, role=PartitionRole.SWAP, size=Size.parse("4GiB"))
    fields = screens._slice_fields(swap, manual.purpose_of(swap), opened().translate)
    filesystem = next(item for item in fields if item.label == "Filesystem")
    assert filesystem.disabled_because


def test_zfs_is_offered_where_a_filesystem_is_chosen() -> None:
    """It is a pool and not a `FilesystemType`, but the operator opening the
    filesystem row is looking for it there, so it is a row there as well."""
    at = opened()
    entry = manual.Slice(index=2, role=PartitionRole.DATA, size=None,
                         filesystem=FilesystemType.EXT4, mountpoint="/")
    screen = FakeScreen(keys=[*["KEY_DOWN"] * len(FilesystemType), "\n"], lines=30)
    changed = screens._edit_field(screen, at, entry, manual.purpose_of(entry), screens._FILESYSTEM)
    assert changed is not None
    assert changed.role is PartitionRole.ZFS
    assert changed.filesystem is None
    assert changed.mountpoint == "/"
    assert "zfs" in screen.last


def test_opening_the_table_after_choosing_zfs_keeps_zfs() -> None:
    """The row used to seed a fresh ext4 table, which discarded the layout the
    operator had just chosen without saying so."""
    at = opened()
    at.choice = replace(at.choice, layout=Layout.WHOLE_DISK_ZFS, disk="/dev/vdb")
    screens.partitions_screen(FakeScreen(keys=["q"]), config(), at)
    root = next(one for one in at.layout.slices if one.mountpoint == "/")
    assert root.role is PartitionRole.ZFS and root.filesystem is None


def test_opening_the_table_after_choosing_xfs_keeps_xfs() -> None:
    at = opened()
    at.choice = replace(at.choice, filesystem=FilesystemType.XFS, disk="/dev/vdb")
    screens.partitions_screen(FakeScreen(keys=["q"]), config(), at)
    root = next(one for one in at.layout.slices if one.mountpoint == "/")
    assert root.filesystem is FilesystemType.XFS


def reused(**fields: object) -> manual.Layout:
    entry = manual.Reused(selector="/dev/vda2", filesystem=FilesystemType.EXT4, mountpoint="/")
    return manual.Layout(disk="/dev/vda", reused=[replace(entry, **fields)])  # type: ignore[arg-type]


def test_reusing_a_partition_creates_no_table_and_no_partition() -> None:
    """The point of the mode: every partition the operator leaves alone keeps
    its data, so nothing may be written to the table."""
    from gentoo_install.model.device import Existing, PartitionTable

    graph, root = manual.build(reused())
    assert not graph.of_type(PartitionTable)
    assert not graph.of_type(Partition)
    kept = graph.of_type(Existing)
    assert [one.selector for one in kept] == ["/dev/vda2"]
    assert not kept[0].wipe
    assert root


def test_keeping_a_filesystem_verifies_it_instead_of_making_one() -> None:
    """Mounting an xfs partition the configuration calls ext4 writes the wrong
    type into fstab, and the machine fails to mount it on the next boot."""
    from gentoo_install.plan import disk as plan_disk

    graph, root = manual.build(reused())
    described = [one.describe() for one in plan_disk.build(config_from(graph, root))]
    assert any("already holds a ext4" in line for line in described)
    assert not any(line.startswith("make a") for line in described)


def test_formatting_a_kept_partition_makes_the_filesystem_and_nothing_else() -> None:
    from gentoo_install.plan import disk as plan_disk

    graph, root = manual.build(reused(format=True))
    described = [one.describe() for one in plan_disk.build(config_from(graph, root))]
    assert any(line.startswith("make a ext4") for line in described)
    assert not any("partition" in line and "sgdisk" in line for line in described)


def test_a_reused_filesystem_on_a_partition_this_run_creates_is_refused() -> None:
    """It describes keeping data that the same plan destroys a few operations
    earlier."""
    from gentoo_install.model.device import Filesystem as Fs

    layout = manual.suggest("/dev/vda", Firmware.UEFI)
    graph, root = manual.build(layout)
    nodes = [
        replace(node, create=False) if isinstance(node, Fs) and node.id == "fs2" else node
        for node in graph.nodes.values()
    ]
    broken = DeviceGraph.build(nodes)
    with pytest.raises(ValidationFailed, match="nothing would be left to reuse"):
        validate(config_from(broken, root))


def test_a_reused_filesystem_on_a_wiped_disk_is_refused() -> None:
    from gentoo_install.model.device import Existing

    graph, root = manual.build(reused())
    nodes = [
        replace(node, wipe=True) if isinstance(node, Existing) else node
        for node in graph.nodes.values()
    ]
    with pytest.raises(ValidationFailed, match="marked to be wiped"):
        validate(config_from(DeviceGraph.build(nodes), root))


def test_a_reused_filesystem_needs_no_mkfs_on_the_medium() -> None:
    """`mkfs.xfs` is absent from the official minimal ISO's smaller variants,
    and a partition that is only mounted never calls it."""
    from gentoo_install.exec import preflight

    graph, root = manual.build(reused(filesystem=FilesystemType.XFS))
    assert "mkfs.xfs" not in preflight.required_commands(config_from(graph, root))
    made, made_root = manual.build(reused(filesystem=FilesystemType.XFS, format=True))
    assert "mkfs.xfs" in preflight.required_commands(config_from(made, made_root))


def test_a_reused_esp_is_an_esp_whether_or_not_it_is_reformatted() -> None:
    """Whether this run runs mkfs.vfat does not change what the partition is,
    and keying on it made ticking `format` refuse every UEFI install."""
    from gentoo_install.model.compat import esp_mount

    for formatting in (False, True):
        layout = manual.Layout(disk="/dev/vda", reused=[
            manual.Reused(selector="/dev/vda1", filesystem=FilesystemType.VFAT,
                          mountpoint="/efi", format=formatting),
            manual.Reused(selector="/dev/vda2", filesystem=FilesystemType.EXT4, mountpoint="/"),
        ])
        graph, root = manual.build(layout)
        assert esp_mount(graph) is not None, formatting
        validate(config_from(graph, root))


def test_a_reused_esp_resolves_to_the_device_the_bootloader_installs_onto() -> None:
    """It has no `Partition` node, and returning None skipped the whole
    bootloader branch without saying anything."""
    from gentoo_install.plan import bootloader

    layout = manual.Layout(disk="/dev/vda", reused=[
        manual.Reused(selector="/dev/vda1", filesystem=FilesystemType.VFAT, mountpoint="/efi"),
        manual.Reused(selector="/dev/vda2", filesystem=FilesystemType.EXT4, mountpoint="/"),
    ])
    graph, root = manual.build(layout)
    installation = config_from(graph, root)
    assert bootloader._esp_partition(installation) == "kept1"
    assert any("install GRUB" in one.describe() for one in bootloader.build(installation))


def test_opening_another_row_does_not_replace_a_reused_table_with_a_wipe() -> None:
    """Every screen rebuilt the disk from the whole-disk template, so opening
    Swap on a reused layout turned it into `wipe the whole disk`."""
    from gentoo_install.model.device import Existing

    at = opened()
    at.layout = manual.Layout(disk="/dev/vda", reused=[
        manual.Reused(selector="/dev/vda1", filesystem=FilesystemType.VFAT, mountpoint="/efi"),
        manual.Reused(selector="/dev/vda2", filesystem=FilesystemType.EXT4, mountpoint="/"),
    ])
    answer = screens.swap_screen(FakeScreen(keys=["\n"]), config(), at)
    graph = answer.unwrap().disk.graph
    assert [one.wipe for one in graph.of_type(Existing)] == [False, False]
    assert not graph.of_type(Partition)


def test_switching_the_disk_moves_a_hand_written_table_with_it() -> None:
    """`_rebuild` reads `context.layout` and not `context.choice` once the table
    is hand-written, so the old disk stayed in the graph and the install
    partitioned the one the operator had just switched away from."""
    at = context()
    at.manual = True
    at.layout.disk = at.choice.disk
    at.layout.reused = [manual.Reused(selector=f"{at.choice.disk}-part1")]
    first = at.choice.disk
    answer = screens.disk_screen(FakeScreen(keys=["KEY_DOWN", "\n"], lines=24), config(), at)
    assert at.choice.disk != first
    assert at.layout.disk == at.choice.disk
    # The kept rows named partitions of the disk that is no longer the target.
    assert at.layout.reused == []
    assert answer.outcome is Outcome.CHOSE


def test_a_reuse_layout_is_not_asked_to_confirm_an_erase_it_will_not_do() -> None:
    """`build_reused` produces only `Existing(wipe=False)`, so demanding the
    disk name blocked an install that writes no partition table at all."""
    from pathlib import PurePosixPath

    from gentoo_install.model.device import Existing
    from gentoo_install.tui import settings

    at = context()
    kept = [
        Existing(id=i("kept1"), selector="/dev/disk/by-id/virtio-target0-part1", wipe=False),
        Filesystem(id=i("keptfs"), device=i("kept1"), kind=FilesystemType.EXT4, create=False),
        Mountpoint(id=i("mnt-root"), source=i("keptfs"), path=PurePosixPath("/")),
    ]
    reused = config(kept)
    assert settings.unanswered(reused, at).count("Confirm erasing the drive") == 0

    # A layout that does erase still has to be confirmed.
    at.erase_confirmed = False
    assert "Confirm erasing the drive" in settings.unanswered(config(), at)


def test_the_encryption_row_reads_the_graph_and_not_the_answer_given_to_it() -> None:
    """`_rebuild` builds a hand-written table from `context.layout` and reads
    none of `context.choice`, so the row said `on` over a graph with no
    container in it and the machine came up unencrypted."""
    from gentoo_install.tui import settings

    at = context()
    at.manual = True
    at.layout.disk = at.choice.disk
    row = next(one for one in settings.DISK if one.key == "encryption")

    # The screen refuses rather than staging a passphrase nothing will use.
    screen = FakeScreen(keys=["\n"], lines=24, columns=100)
    answer = screens.encryption_screen(screen, config(), at)
    assert answer.outcome is Outcome.BACK
    assert not at.choice.passphrase_file
    assert row.value(config(), at) == "off"

    # A graph that does carry a container reads on, whatever the choice holds.
    nodes = [node for node in ext4_on_gpt() if node.id != i("rootfs")]
    nodes += [
        Luks(id=i("crypt"), backing=i("rootpart"), name="root", passphrase_file="/run/keys/x"),
        Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.EXT4),
    ]
    assert row.value(config(nodes), at) == "on"
