"""The hand-written partition table.

Driven through the fake screen like every other widget, so the table can be
edited, validated and turned into a graph without a terminal or a disk.
"""

from __future__ import annotations

from dataclasses import replace

from gentoo_install.model import manual
from gentoo_install.model.config import DiskConfig, InstallConfig
from gentoo_install.model.device import DeviceGraph, DeviceId
from gentoo_install.model.config import Firmware
from gentoo_install.model.device import Filesystem, FilesystemType, Mountpoint, PartitionRole, Swap
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
