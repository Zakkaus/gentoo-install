from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from gentoo_install.errors import DeviceCycle, DuplicateDeviceId, UnknownDeviceId
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
    Subvolume,
    TableType,
)
from gentoo_install.model.size import Size


def i(name: str) -> DeviceId:
    return DeviceId(name)


def btrfs_on_luks() -> list[Node]:
    """The layout the second golden fixture will use: one disk, ESP, LUKS, btrfs."""
    return [
        Existing(id=i("disk"), selector="/dev/disk/by-id/virtio-target", wipe=True),
        PartitionTable(id=i("table"), disk=i("disk"), table=TableType.GPT),
        Partition(
            id=i("esp"),
            table=i("table"),
            index=1,
            role=PartitionRole.ESP,
            size=Size.parse("512MiB"),
        ),
        Partition(id=i("cryptroot"), table=i("table"), index=2, role=PartitionRole.DATA, size=None),
        Filesystem(id=i("espfs"), device=i("esp"), kind=FilesystemType.VFAT, label="ESP"),
        Luks(id=i("root-luks"), backing=i("cryptroot"), name="root"),
        Filesystem(id=i("rootfs"), device=i("root-luks"), kind=FilesystemType.BTRFS),
        Subvolume(id=i("sub-root"), filesystem=i("rootfs"), name="@"),
        Subvolume(id=i("sub-home"), filesystem=i("rootfs"), name="@home"),
        Mountpoint(id=i("mnt-root"), source=i("sub-root"), path=PurePosixPath("/")),
        Mountpoint(id=i("mnt-home"), source=i("sub-home"), path=PurePosixPath("/home")),
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/efi")),
    ]


def test_a_realistic_stack_builds() -> None:
    graph = DeviceGraph.build(btrfs_on_luks())
    assert len(graph.nodes) == 12
    assert graph.inputs_of(i("rootfs")) == (i("root-luks"),)
    assert set(graph.consumers_of(i("rootfs"))) == {i("sub-root"), i("sub-home")}
    assert graph.inputs_of(i("disk")) == ()


def test_of_type_selects_by_class() -> None:
    graph = DeviceGraph.build(btrfs_on_luks())
    assert {node.name for node in graph.of_type(Subvolume)} == {"@", "@home"}
    assert len(graph.of_type(Mountpoint)) == 3
    assert graph.of_type(Luks)[0].name == "root"


def test_duplicate_ids_are_rejected() -> None:
    nodes = btrfs_on_luks()
    nodes.append(Existing(id=i("disk"), selector="/dev/sdb"))
    with pytest.raises(DuplicateDeviceId, match="disk"):
        DeviceGraph.build(nodes)


def test_a_reference_to_a_missing_node_is_rejected() -> None:
    nodes = [
        Existing(id=i("disk"), selector="/dev/sda"),
        PartitionTable(id=i("table"), disk=i("nowhere"), table=TableType.GPT),
    ]
    with pytest.raises(UnknownDeviceId, match="nowhere"):
        DeviceGraph.build(nodes)


def test_a_node_that_feeds_itself_is_a_cycle() -> None:
    with pytest.raises(DeviceCycle, match="loop"):
        DeviceGraph.build([Luks(id=i("loop"), backing=i("loop"), name="loop")])


def test_a_two_node_cycle_is_reported_with_its_path() -> None:
    nodes = [
        Luks(id=i("a"), backing=i("b"), name="a"),
        Luks(id=i("b"), backing=i("a"), name="b"),
    ]
    with pytest.raises(DeviceCycle) as caught:
        DeviceGraph.build(nodes)
    assert "->" in str(caught.value)


def test_a_cycle_behind_a_long_chain_is_still_found() -> None:
    nodes: list[Node] = [Existing(id=i("disk"), selector="/dev/sda")]
    nodes += [Luks(id=i(f"n{n}"), backing=i(f"n{n + 1}"), name=f"n{n}") for n in range(50)]
    nodes.append(Luks(id=i("n50"), backing=i("n0"), name="n50"))
    with pytest.raises(DeviceCycle):
        DeviceGraph.build(nodes)


def test_deep_chains_do_not_hit_the_recursion_limit() -> None:
    depth = 3000
    nodes: list[Node] = [Existing(id=i("base"), selector="/dev/sda")]
    nodes += [
        Luks(id=i(f"n{n}"), backing=i(f"n{n - 1}") if n else i("base"), name=f"n{n}")
        for n in range(depth)
    ]
    graph = DeviceGraph.build(nodes)
    assert len(graph.nodes) == depth + 1


def test_lookup_of_an_unknown_id_raises_rather_than_returning_none() -> None:
    graph = DeviceGraph.build(btrfs_on_luks())
    with pytest.raises(UnknownDeviceId):
        graph[i("absent")]
    with pytest.raises(UnknownDeviceId):
        graph.consumers_of(i("absent"))


def test_nodes_are_frozen_so_a_validated_graph_cannot_be_edited() -> None:
    graph = DeviceGraph.build(btrfs_on_luks())
    with pytest.raises(AttributeError):
        setattr(graph[i("rootfs")], "id", i("other"))
