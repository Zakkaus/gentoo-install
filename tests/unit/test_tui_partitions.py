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
    TableType,
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


def one_disk(
    slices: list[manual.Slice],
    disk: str = "/dev/vda",
    table: TableType = TableType.GPT,
    pool: str = "rpool",
) -> manual.Layout:
    """One disk carrying these rows, which is what most of these tests need."""
    return manual.Layout(
        disks=[manual.Disk(selector=disk, table=table, slices=list(slices))], pool=pool
    )


def to_row(at: screens.Context, label: str) -> list[str]:
    """The keys that reach the partition-screen row with that label.

    Counted rather than written out: the screen grew a line per disk, and every
    test that had counted its own KEY_DOWNs then landed a row short.
    """
    labels = [item.label for item in screens._partition_rows(at)]
    return ["KEY_DOWN"] * labels.index(label) + ["\n"]


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
    at.layout.disks[0].slices.append(
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
    layout = one_disk(slices=[
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
    at.layout = one_disk(slices=[
        manual.Slice(index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                     filesystem=FilesystemType.VFAT, mountpoint="/efi"),
    ])
    assert "mounted at /" in screens._layout_problem(at, config())


def test_a_root_too_small_is_reported_in_the_table() -> None:
    """The validator's own sentence, so the table and the install row agree."""
    at = opened()
    at.layout = one_disk(slices=[
        manual.Slice(index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                     filesystem=FilesystemType.VFAT, mountpoint="/efi"),
        manual.Slice(index=2, role=PartitionRole.DATA, size=Size.parse("4GiB"),
                     filesystem=FilesystemType.EXT4, mountpoint="/"),
    ])
    assert "under the" in screens._layout_problem(at, config())


def test_a_swap_partition_needs_no_filesystem() -> None:
    layout = one_disk(slices=[
        manual.Slice(index=1, role=PartitionRole.SWAP, size=Size.parse("4GiB")),
        manual.Slice(index=2, role=PartitionRole.DATA, size=None,
                     filesystem=FilesystemType.EXT4, mountpoint="/"),
    ])
    graph, _ = manual.build(layout)
    assert len(graph.of_type(Swap)) == 1


def test_the_editor_lists_the_table_and_leaves_on_done() -> None:
    at = opened()
    at.layout = manual.suggest(at.choice.disk, Firmware.UEFI)
    screen = FakeScreen(keys=to_row(at, "Done"), lines=30, columns=90)
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
    layout = one_disk(pool="rpool", slices=[
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
    layout = one_disk(slices=[
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
    layout = one_disk(slices=[
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


def kept(
    selector: str,
    filesystem: FilesystemType,
    mountpoint: str,
    status: manual.SliceStatus = manual.SliceStatus.KEEP,
    index: int = 0,
) -> manual.Slice:
    """One row already on the disk, for a table that writes no partitions."""
    return manual.Slice(
        index=index or int(selector[-1]),
        role=PartitionRole.DATA,
        size=None,
        filesystem=filesystem,
        mountpoint=mountpoint,
        status=status,
        selector=selector,
    )


def reused(
    *,
    filesystem: FilesystemType | None = FilesystemType.EXT4,
    mountpoint: str = "/",
    status: manual.SliceStatus = manual.SliceStatus.KEEP,
) -> manual.Layout:
    """A table of one row that is already on the disk. `keep` mounts what is
    there; `format` makes a new filesystem in the same partition."""
    return one_disk(
        slices=[
            manual.Slice(
                index=2,
                role=PartitionRole.DATA,
                size=None,
                filesystem=filesystem,
                mountpoint=mountpoint,
                status=status,
                selector="/dev/vda2",
            )
        ],
    )


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

    graph, root = manual.build(reused(status=manual.SliceStatus.FORMAT))
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
        replace(node, create=False) if isinstance(node, Fs) and node.id == "disk1-fs2" else node
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
    made, made_root = manual.build(reused(filesystem=FilesystemType.XFS, status=manual.SliceStatus.FORMAT))
    assert "mkfs.xfs" in preflight.required_commands(config_from(made, made_root))


def test_a_reused_esp_is_an_esp_whether_or_not_it_is_reformatted() -> None:
    """Whether this run runs mkfs.vfat does not change what the partition is,
    and keying on it made ticking `format` refuse every UEFI install."""
    from gentoo_install.model.compat import esp_mount

    for formatting in (False, True):
        wanted = (
            manual.SliceStatus.FORMAT if formatting else manual.SliceStatus.KEEP
        )
        layout = one_disk(slices=[
            kept("/dev/vda1", FilesystemType.VFAT, "/efi", wanted),
            kept("/dev/vda2", FilesystemType.EXT4, "/"),
        ])
        graph, root = manual.build(layout)
        assert esp_mount(graph) is not None, formatting
        validate(config_from(graph, root))


def test_a_reused_esp_resolves_to_the_device_the_bootloader_installs_onto() -> None:
    """It has no `Partition` node, and returning None skipped the whole
    bootloader branch without saying anything."""
    from gentoo_install.plan import bootloader

    layout = one_disk(slices=[
        kept("/dev/vda1", FilesystemType.VFAT, "/efi"),
        kept("/dev/vda2", FilesystemType.EXT4, "/"),
    ])
    graph, root = manual.build(layout)
    installation = config_from(graph, root)
    assert bootloader._esp_partition(installation) == "disk1-part1"
    assert any("install GRUB" in one.describe() for one in bootloader.build(installation))


def test_opening_another_row_does_not_replace_a_reused_table_with_a_wipe() -> None:
    """Every screen rebuilt the disk from the whole-disk template, so opening
    Swap on a reused layout turned it into `wipe the whole disk`."""
    from gentoo_install.model.device import Existing

    at = opened()
    at.layout = one_disk(slices=[
        kept("/dev/vda1", FilesystemType.VFAT, "/efi"),
        kept("/dev/vda2", FilesystemType.EXT4, "/"),
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
    at.layout = one_disk(
        [kept(f"{at.choice.disk}-part1", FilesystemType.EXT4, "/", index=1)],
        disk=at.choice.disk,
    )
    first = at.choice.disk
    answer = screens.disk_screen(FakeScreen(keys=["KEY_DOWN", "\n"], lines=24), config(), at)
    assert at.choice.disk != first
    # The rows named partitions of the disk that is no longer the target.
    assert at.layout.disks == []
    assert answer.outcome is Outcome.CHOSE


def test_a_reuse_layout_is_not_asked_to_confirm_an_erase_it_will_not_do() -> None:
    """`build_reused` produces only `Existing(wipe=False)`, so demanding the
    disk name blocked an install that writes no partition table at all."""
    from pathlib import PurePosixPath

    from gentoo_install.model.device import Existing
    from gentoo_install.tui import settings

    at = context()
    at.visited.add("erase")
    kept = [
        Existing(id=i("part1"), selector="/dev/disk/by-id/virtio-target0-part1", wipe=False),
        Filesystem(id=i("keptfs"), device=i("part1"), kind=FilesystemType.EXT4, create=False),
        Mountpoint(id=i("mnt-root"), source=i("keptfs"), path=PurePosixPath("/")),
    ]
    reused = config(kept)
    assert "Confirm erasing the drive" not in [
        one.label for one in settings.unanswered(reused, at)
    ]

    # A layout that does erase still has to be confirmed.
    at.confirmed.clear()
    assert "Confirm erasing the drive" in [
        one.label for one in settings.unanswered(config(), at)
    ]


def test_the_encryption_row_reads_the_graph_and_not_the_answer_given_to_it() -> None:
    """`_rebuild` builds a hand-written table from `context.layout` and reads
    none of `context.choice`, so the row said `on` over a graph with no
    container in it and the machine came up unencrypted."""
    from gentoo_install.tui import settings

    at = context()
    at.manual = True
    at.layout = one_disk([], disk=at.choice.disk)
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


def test_the_partitions_row_opens_only_where_there_is_a_table_to_edit() -> None:
    """A whole-disk template writes the table itself, so opening the editor
    over one switched the layout to manual without saying so."""
    from gentoo_install.tui import settings

    at = context()
    at.existing = (("/dev/vda1", "1G", "vfat"), ("/dev/vda2", "29G", "ext4"))
    row = next(one for one in settings.DISK if one.key == "partitions")
    assert row.unavailable(config(), at)

    at.manual = True
    assert not row.unavailable(config(), at)


def test_the_table_opens_on_what_is_already_on_the_disk() -> None:
    """An operator who opens this over a machine with data on it should see
    that data, not a proposal that erases it."""
    at = context()
    at.existing = (("/dev/vda1", "1G", "vfat"), ("/dev/vda2", "29G", "ext4"))
    at.manual = True
    at.layout = manual.Layout()
    screen = FakeScreen(keys=["q"], lines=24, columns=100)
    screens.partitions_screen(screen, config(), at)

    assert [one.selector for one in at.layout.slices] == ["/dev/vda1", "/dev/vda2"]
    assert all(one.status is manual.SliceStatus.KEEP for one in at.layout.slices)
    # Nothing edits the table, so nothing writes one.
    assert not at.layout.writes_the_table()
    drawn = "\n".join(screen.frames[0])
    assert "vda1" in drawn and "keep" in drawn


def test_a_table_nobody_edits_says_what_it_does_with_the_partition_table() -> None:
    """A table of nothing but kept rows writes none, so the row read `not set`
    for ever and every answer given to it looked like it had not taken."""
    from gentoo_install.tui import settings

    at = context()
    row = next(one for one in settings.DISK if one.key == "table")
    assert row.value(config(), at) == "gpt"
    assert not row.unavailable(config(), at)

    at.layout = one_disk([kept("/dev/vda1", FilesystemType.EXT4, "/")])
    assert row.value(config(), at) != settings.UNSET
    assert row.unavailable(config(), at)


def test_the_pool_topology_row_appears_once_there_is_something_to_join() -> None:
    """A pool of one has nothing to mirror, so the row cannot be opened. It is
    still drawn, with the purpose to set: hidden, mirroring was unreachable to
    anyone who did not already know it was there."""
    from gentoo_install.model.device import ZfsTopology

    at = context()
    at.manual = True

    def slices(members: int) -> list[manual.Slice]:
        made = [
            manual.Slice(
                index=1, role=PartitionRole.ESP, size=None,
                filesystem=FilesystemType.VFAT, mountpoint="/efi",
            )
        ]
        made += [
            manual.Slice(
                index=n + 2, role=PartitionRole.ZFS, size=None,
                filesystem=None, mountpoint="/" if n == 0 else "",
            )
            for n in range(members)
        ]
        return made

    at.layout = one_disk(slices(1), disk=at.choice.disk)
    single = FakeScreen(keys=["q"], lines=24, columns=110)
    screens.partitions_screen(single, config(), at)
    drawn = "\n".join(single.frames[0])
    assert "Pool topology" in drawn
    assert "zfs pool member purpose" in drawn

    at.layout = one_disk(slices(2), disk=at.choice.disk)
    pair = FakeScreen(keys=["q"], lines=24, columns=110)
    screens.partitions_screen(pair, config(), at)
    opened = "\n".join(pair.frames[0])
    assert "Pool topology" in opened
    assert "zfs pool member purpose" not in opened


def test_a_topology_this_many_devices_cannot_make_says_how_many_it_needs() -> None:
    """Drawn with the count rather than left out: a row that is simply absent
    reads as a topology this installer does not support."""
    from gentoo_install.model.device import ZfsTopology

    at = context()
    screen = FakeScreen(keys=["q"], lines=20, columns=100)
    screens._pool_topology(screen, at, 2)
    drawn = "\n".join(screen.frames[0])
    for one in ZfsTopology:
        assert one.value in drawn, one
    assert "raidz2 - needs at least 3" in drawn
    assert "raidz3 - needs at least 4" in drawn

    # Enough devices, and no row carries a count.
    roomy = FakeScreen(keys=["q"], lines=20, columns=100)
    screens._pool_topology(roomy, at, 4)
    assert "needs at least" not in "\n".join(roomy.frames[0])


def test_one_table_keeps_deletes_and_creates_in_the_same_pass() -> None:
    """Two exclusive modes could not say "keep the Windows partition, delete
    the old root, add a new one", which is the ordinary case. The disk is not
    wiped and the table is not rewritten: `sgdisk --zap-all` would take the
    kept entries with it."""
    from gentoo_install.model.device import Existing, PartitionTable
    from gentoo_install.plan import disk as plan_disk

    status = manual.SliceStatus
    layout = one_disk(slices=[
        kept("/dev/vda1", FilesystemType.VFAT, "/efi"),
        kept("/dev/vda2", FilesystemType.EXT4, "", status.DELETE),
        manual.Slice(
            index=3, role=PartitionRole.DATA, size=None,
            filesystem=FilesystemType.EXT4, mountpoint="/",
        ),
    ])
    graph, root = manual.build(layout)

    disk = next(one for one in graph.of_type(Existing) if one.selector == "/dev/vda")
    assert disk.wipe is False
    table = graph.of_type(PartitionTable)[0]
    assert table.create is False
    assert table.remove == (2,)

    described = [one.describe() for one in plan_disk.build(config_from(graph, root))]
    assert not any("wipe existing signatures" in line for line in described)
    assert any("delete partition 2" in line for line in described)
    assert any("create partition 3" in line for line in described)
    # The kept esp is checked, not remade.
    assert any("already holds a vfat" in line for line in described)


def test_a_table_of_new_rows_alone_still_wipes_the_disk() -> None:
    """Nothing on the disk is being kept, so the fresh table is the honest
    thing to write and `--zap-all` takes nothing the operator asked for."""
    from gentoo_install.model.device import Existing, PartitionTable

    graph, _ = manual.build(manual.suggest("/dev/vda", Firmware.UEFI))
    assert graph.of_type(Existing)[0].wipe is True
    assert graph.of_type(PartitionTable)[0].create is True


def test_a_table_nobody_edits_writes_no_table_at_all() -> None:
    """Every partition on the disk survives, which is what the separate reuse
    mode used to be and is now one case of the same table."""
    from gentoo_install.model.device import PartitionTable

    graph, _ = manual.build(reused())
    assert graph.of_type(PartitionTable) == ()


def test_the_entry_number_comes_off_the_selector_and_not_the_row() -> None:
    """`sgdisk --delete` addresses the entry in the table, and the row order in
    the editor is not that number."""
    layout = one_disk(slices=[
        manual.Slice(
            index=1, role=PartitionRole.DATA, size=None, filesystem=None,
            status=manual.SliceStatus.DELETE, selector="/dev/nvme0n1p7",
        )
    ])
    from gentoo_install.model.device import PartitionTable

    graph, _ = manual.build(layout)
    assert graph.of_type(PartitionTable)[0].remove == (7,)


def two_disks() -> manual.Layout:
    """The esp on one drive and the root on another, which is the layout a
    machine with a small fast disk and a large slow one wants."""
    return manual.Layout(
        disks=[
            manual.Disk(
                selector="/dev/vda",
                slices=[
                    manual.Slice(
                        index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                        filesystem=FilesystemType.VFAT, mountpoint="/efi",
                    )
                ],
            ),
            manual.Disk(
                selector="/dev/vdb",
                slices=[
                    manual.Slice(
                        index=1, role=PartitionRole.DATA, size=None,
                        filesystem=FilesystemType.EXT4, mountpoint="/",
                    )
                ],
            ),
        ]
    )


def test_a_table_can_span_more_than_one_disk() -> None:
    from gentoo_install.model.device import Existing, PartitionTable

    graph, root = manual.build(two_disks())
    assert sorted(node.selector for node in graph.of_type(Existing)) == ["/dev/vda", "/dev/vdb"]
    assert len(graph.of_type(PartitionTable)) == 2
    validate(config_from(graph, root))


def test_partition_one_of_each_disk_gets_its_own_id() -> None:
    """Both disks have a partition 1, and one id for the two of them dropped a
    node out of the graph without a word."""
    graph, _ = manual.build(two_disks())
    assert {"disk1-part1", "disk2-part1"} <= set(graph.nodes)


def test_one_disk_can_be_rewritten_while_the_other_is_left_alone() -> None:
    """The reason the table is per disk: the second drive holds data and only
    the first is being repartitioned."""
    from gentoo_install.model.device import PartitionTable

    layout = two_disks()
    layout.disks[1].slices = [kept("/dev/vdb1", FilesystemType.EXT4, "/")]
    graph, root = manual.build(layout)
    tables = graph.of_type(PartitionTable)
    assert [node.disk for node in tables] == ["disk1"]
    validate(config_from(graph, root))


def test_the_screen_lists_every_disk_with_its_rows_under_it() -> None:
    at = opened()
    at.layout = two_disks()
    at.choice = replace(at.choice, disk="/dev/vda")
    screen = FakeScreen(keys=["q"], lines=30, columns=100)
    screens.partitions_screen(screen, config(), at)
    drawn = "\n".join(screen.frames[0])
    assert "vda" in drawn and "vdb" in drawn
    assert drawn.index("vda") < drawn.index("/efi") < drawn.index("vdb")


def test_a_second_disk_can_be_added_from_the_table() -> None:
    at = opened()
    at.layout = manual.suggest(at.choice.disk, Firmware.UEFI)
    keys = [*to_row(at, "Add a disk"), "\n"]
    screens.partitions_screen(FakeScreen(keys=[*keys, "q"], lines=30, columns=100), config(), at)
    assert [one.selector for one in at.layout.disks] == [one[0] for one in at.disks]


def test_a_disk_already_in_the_table_is_not_offered_again() -> None:
    at = opened()
    at.layout = manual.Layout(
        disks=[manual.Disk(selector=one[0]) for one in at.disks]
    )
    assert not screens._unused_disks(at)
    labels = [item.label for item in screens._partition_rows(at)]
    assert "Add a disk" not in labels


def test_the_first_disk_cannot_be_taken_off_the_table() -> None:
    """It is the one the disk row chose, and a table with no disk has nothing
    to install onto."""
    at = opened()
    at.layout = two_disks()
    first = FakeScreen(keys=["q"], lines=24, columns=90)
    screens._edit_disk(first, at, 0)
    assert "Take this disk off the table" not in "\n".join(first.frames[0])

    second = FakeScreen(keys=["KEY_DOWN", "\n"], lines=24, columns=90)
    screens._edit_disk(second, at, 1)
    assert [one.selector for one in at.layout.disks] == ["/dev/vda"]


def mirrored() -> manual.Layout:
    """One partition on each of two disks, joined into a mirrored root."""
    layout = manual.Layout(
        disks=[
            manual.Disk(
                selector=f"/dev/vd{letter}",
                slices=[
                    manual.Slice(
                        index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                        filesystem=FilesystemType.VFAT,
                        mountpoint="/efi" if letter == "a" else "",
                    ),
                    manual.Slice(index=2, role=PartitionRole.RAID, size=None, filesystem=None),
                ],
            )
            for letter in ("a", "b")
        ]
    )
    layout.array.mountpoint = "/"
    return layout


def test_a_mirrored_root_can_be_written_by_hand() -> None:
    from gentoo_install.model.device import MdRaid, RaidLevel

    graph, root = manual.build(mirrored())
    array = graph.of_type(MdRaid)[0]
    assert array.level is RaidLevel.RAID1
    assert set(array.members) == {"disk1-part2", "disk2-part2"}
    mounted = graph[root]
    assert isinstance(mounted, Mountpoint) and str(mounted.path) == "/"
    validate(config_from(graph, root))


def test_an_array_member_carries_no_filesystem_of_its_own() -> None:
    """The array carries the filesystem, so a member with one is a partition
    formatted and then overwritten by `mdadm --create`."""
    from gentoo_install.model.device import Filesystem as Fs

    graph, _ = manual.build(mirrored())
    on_members = [
        node for node in graph.of_type(Fs) if node.device in ("disk1-part2", "disk2-part2")
    ]
    assert on_members == []


def test_an_encrypted_array_puts_luks_between_it_and_the_filesystem() -> None:
    from gentoo_install.model.device import Filesystem as Fs
    from gentoo_install.model.device import Luks

    layout = mirrored()
    layout.array.passphrase_file = "/run/keys/array"
    graph, root = manual.build(layout)
    container = graph.of_type(Luks)[0]
    assert container.backing == "array"
    assert graph.of_type(Fs)[-1].device == container.id
    validate(config_from(graph, root))


def test_the_array_row_says_what_to_set_before_it_can_be_opened() -> None:
    """Absent until a partition carried the purpose, an array was something an
    operator could only find by already knowing about it."""
    at = opened()
    at.choice = replace(at.choice, disk="/dev/vda")
    at.layout = manual.suggest("/dev/vda", Firmware.UEFI)
    shut = next(one for one in screens._partition_rows(at) if one.label == "RAID array")
    assert "raid array member purpose" in shut.disabled_because

    at.layout = mirrored()
    open_now = next(one for one in screens._partition_rows(at) if one.label == "RAID array")
    assert open_now.disabled_because == ""
    assert open_now.detail


def test_a_level_the_members_cannot_make_is_shown_with_what_it_needs() -> None:
    """`mdadm --create` refuses it after the disks are partitioned, so the
    menu says so before they are."""
    at = opened()
    at.layout = mirrored()
    screen = FakeScreen(keys=["KEY_DOWN", "\n", "q", "q"], lines=24, columns=90)
    screens._edit_array(screen, at)
    drawn = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "raid5 - needs at least 3" in drawn
    assert "raid6 - needs at least 4" in drawn


def test_the_raid_purpose_is_offered_beside_the_pool_member() -> None:
    keys = [one.key for one in manual.PURPOSES]
    assert "raid" in keys and "zfs" in keys


def without_zfs() -> screens.Context:
    at = opened()
    at.zfs_unavailable = "this live system has no zpool"
    return at


def test_a_medium_with_no_zfs_offers_no_zfs_layout() -> None:
    """Alpine and Debian live images carry none, and the installer is meant to
    run from them; the row says why rather than failing at zpool create."""
    at = without_zfs()
    # `automatic` first: the filesystem list is the screen after it.
    screen = FakeScreen(keys=["\n", "q"], lines=24, columns=100)
    screens.layout_screen(screen, config(), at)
    drawn = "\n".join(screen.frames[1])
    assert "zfs  with ZFSBootMenu - this live system has no zpool" in drawn


def test_a_medium_with_no_zfs_offers_no_pool_member_purpose() -> None:
    at = without_zfs()
    entry = manual.Slice(index=1, role=PartitionRole.DATA, size=None,
                         filesystem=FilesystemType.EXT4, mountpoint="/")
    screen = FakeScreen(keys=["q"], lines=24, columns=100)
    screens._edit_field(screen, at, entry, manual.purpose_of(entry), screens._PURPOSE)
    drawn = "\n".join(screen.frames[0])
    assert "zfs pool member - this live system has no zpool" in drawn


def test_a_medium_with_no_zfs_offers_no_zfs_in_the_filesystem_menu() -> None:
    """It is listed there because that is where anyone choosing a filesystem
    looks for it, so that is where the reason has to appear too."""
    at = without_zfs()
    entry = manual.Slice(index=1, role=PartitionRole.DATA, size=None,
                         filesystem=FilesystemType.EXT4, mountpoint="/")
    screen = FakeScreen(keys=["q"], lines=24, columns=100)
    screens._edit_field(screen, at, entry, manual.purpose_of(entry), screens._FILESYSTEM)
    drawn = "\n".join(screen.frames[0])
    assert "this live system has no zpool" in drawn


def test_a_medium_with_zfs_offers_all_three() -> None:
    at = opened()
    for screen, call in (
        (FakeScreen(keys=["q"], lines=24, columns=100), "layout"),
        (FakeScreen(keys=["q"], lines=24, columns=100), screens._PURPOSE),
        (FakeScreen(keys=["q"], lines=24, columns=100), screens._FILESYSTEM),
    ):
        if call == "layout":
            screens.layout_screen(screen, config(), at)
        else:
            entry = manual.Slice(index=1, role=PartitionRole.DATA, size=None,
                                 filesystem=FilesystemType.EXT4, mountpoint="/")
            screens._edit_field(screen, at, entry, manual.purpose_of(entry), call)
        assert "live system" not in "\n".join(screen.frames[0])


def test_a_hand_built_zfs_root_is_asked_which_bootloader() -> None:
    """The template path asks; the manual one did not, so no overlay was added
    and every row of the bootloader screen was greyed with nothing to pick."""
    at = opened()
    at.choice = replace(at.choice, disk="/dev/vda")
    at.layout = manual.Layout(
        disks=[
            manual.Disk(
                selector="/dev/vda",
                slices=[
                    manual.Slice(
                        index=1, role=PartitionRole.ESP, size=Size.parse("1GiB"),
                        filesystem=FilesystemType.VFAT, mountpoint="/efi",
                    ),
                    manual.Slice(
                        index=2, role=PartitionRole.ZFS, size=None,
                        filesystem=None, mountpoint="/",
                    ),
                ],
            )
        ]
    )
    done = [item.label for item in screens._partition_rows(at)].index("Done")
    keys = ["KEY_DOWN"] * done + ["\n", "\n"]
    screen = FakeScreen(keys=keys, lines=30, columns=110)
    answer = screens.partitions_screen(screen, config(), at)
    assert answer.chosen
    assert answer.unwrap().bootloader.kind is Bootloader.ZFSBOOTMENU
    assert [one.name for one in answer.unwrap().portage.overlays] == ["gentoo-zh"]


def test_the_layout_row_opens_on_what_is_already_set() -> None:
    """Enter on this screen keeps the filesystem the configuration holds. It
    opened on whichever row was listed first, so an operator who pressed enter
    twice changed a setting they were looking at."""
    from gentoo_install.model.device import Filesystem, FilesystemType
    from gentoo_install.tui import screens

    at = context()
    kept = screens.layout_screen(FakeScreen(keys=["\n", "\n"], lines=26), config(), at).unwrap()
    chosen = [
        one.kind
        for one in kept.disk.graph.of_type(Filesystem)
        if one.kind is not FilesystemType.VFAT
    ]
    assert chosen == [at.choice.filesystem]


def test_raid_and_the_pool_topology_are_visible_before_they_are_reachable() -> None:
    """Both rows appeared only once a partition happened to carry the right
    purpose, so neither feature could be found by anyone who did not already
    know it was there. Drawn always, with the reason when they cannot open."""
    from gentoo_install.tui import screens

    at = context()
    at.manual = True
    drawn = FakeScreen(keys=["q"], lines=30, columns=118)
    screens.partitions_screen(drawn, config(), at)
    assert "RAID array" in drawn.last
    assert "raid array member purpose" in drawn.last
    assert "Pool topology" in drawn.last
    assert "zfs pool member purpose" in drawn.last


def test_a_partition_added_after_the_root_does_not_leave_it_in_the_middle() -> None:
    """`suggest()` starts the root at index 1 with no size, so it takes the
    rest of the disk. An operator adding a partition after it produced a table
    whose partition 1 runs to the last sector and whose partition 3 has
    nowhere to go, which `sgdisk` refuses once the table is written.

    The one with no size is numbered last, whatever index it was given.
    """
    from gentoo_install.model.device import Partition

    layout = manual.suggest("/dev/vda", Firmware.UEFI)
    layout.disks[0].slices.append(
        manual.Slice(
            index=3,
            role=PartitionRole.DATA,
            size=Size.parse("20GiB"),
            filesystem=FilesystemType.XFS,
            mountpoint="/home",
        )
    )
    graph, root = manual.build(layout)
    numbered = sorted(graph.of_type(Partition), key=lambda one: one.index)
    assert [one.size is None for one in numbered] == [False, False, True], [
        (one.index, str(one.size)) for one in numbered
    ]
    validate(config_from(graph, root))


def test_an_edited_table_keeps_the_numbers_the_disk_gave_out() -> None:
    """Renumbering is for a table written from scratch. A kept partition's
    number is the disk's, and moving it renames somebody's filesystem."""
    from gentoo_install.model.device import Partition

    status = manual.SliceStatus
    layout = one_disk(slices=[
        kept("/dev/vda1", FilesystemType.VFAT, "/efi"),
        kept("/dev/vda2", FilesystemType.EXT4, "", status.DELETE),
        manual.Slice(
            index=3, role=PartitionRole.DATA, size=None,
            filesystem=FilesystemType.EXT4, mountpoint="/",
        ),
    ])
    graph, _ = manual.build(layout)
    assert [one.index for one in graph.of_type(Partition)] == [3]


def test_who_lays_the_disk_out_is_asked_before_what_goes_on_it() -> None:
    """One list held both questions: four filesystems and `manual` side by
    side, as if `manual` were a fifth filesystem. It is the other question,
    and a list that mixes them offers the automatic path as a default nobody
    was shown an alternative to."""
    from gentoo_install.tui import screens

    at = context()
    first = FakeScreen(keys=["q"], lines=24, columns=100)
    screens.layout_screen(first, config(), at)
    drawn = "\n".join(first.frames[0])
    assert "automatic" in drawn and "manual" in drawn
    for filesystem in ("ext4", "xfs", "btrfs"):
        assert filesystem not in drawn, f"{filesystem} belongs to the next question"
