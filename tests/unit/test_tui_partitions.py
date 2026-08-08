"""The hand-written partition table.

Driven through the fake screen like every other widget, so the table can be
edited, validated and turned into a graph without a terminal or a disk.
"""

from __future__ import annotations

from dataclasses import replace

from gentoo_install.model import manual
from gentoo_install.model.config import Bootloader, BootloaderConfig, DiskConfig, InstallConfig
from gentoo_install.model.device import DeviceGraph, DeviceId
from gentoo_install.model.config import Firmware
from gentoo_install.model.device import (
    Filesystem,
    FilesystemType,
    Luks,
    Mountpoint,
    PartitionRole,
    Swap,
    ZfsDataset,
    ZfsPool,
)
from gentoo_install.model.size import Size
from gentoo_install.model.validate import validate
from gentoo_install.tui import screens
from gentoo_install.tui.widgets import Outcome

from .fake_screen import FakeScreen
from .layouts import config
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
