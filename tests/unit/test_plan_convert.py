# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from gentoo_install.model.config import InstallConfig
from gentoo_install.plan import disk as plan_disk
from gentoo_install.plan import portage as plan_portage
from gentoo_install.plan.bootloader import InstallGrub
from gentoo_install.plan.build import build
from gentoo_install.plan.convert import SwapDirectories
from gentoo_install.plan.operations import Stage
from gentoo_install.model.config import DiskConfig, DiskMode
from gentoo_install.errors import ConversionUnsupported
from gentoo_install.model.device import (
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    Mountpoint,
    StorageLayout,
)
from gentoo_install.plan import convert
from gentoo_install.plan.packages import Catalog, Group

from .layouts import config


CATALOG: Catalog = {"console": Group(name="console", packages=("app-editors/vim",))}


def _in_place() -> InstallConfig:
    """A conversion carries no device graph: the layout comes from the machine,
    and `validate()` refuses one beside the mode."""
    return replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId(""), mode=DiskMode.IN_PLACE),
    )


def test_partition_mode_keeps_the_ordinary_list() -> None:
    ordinary = build(config(), CATALOG)
    explicit = build(replace(config(), disk=replace(config().disk)), CATALOG)
    assert tuple(type(operation) for operation in explicit) == tuple(type(operation) for operation in ordinary)


def test_conversion_operation_describes_and_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[tuple[Path, tuple[str, ...]]] = []

    def convert(staging: Any, names: tuple[str, ...]) -> None:
        called.append((staging, names))

    from gentoo_install.exec import convert as executor

    monkeypatch.setattr(executor, "convert", convert)
    operation = SwapDirectories()
    assert operation.describe()
    operation.apply(SimpleNamespace(target=PurePosixPath("/target")))
    assert called == [(Path("/gentoo-install.new"), operation.names)]


def _layout(
    *,
    root_device: str | None = "/dev/vda2",
    root_filesystem_type: str | None = "xfs",
    root_on_lvm: bool = False,
    root_on_luks: bool = False,
    root_on_mdraid: bool = False,
    esp_device: str | None = "/dev/vda1",
    esp_mountpoint: str | None = "/boot/efi",
    uefi: bool = True,
) -> StorageLayout:
    """A UEFI machine whose root is a plain filesystem on a partition."""
    return StorageLayout(
        root_device=root_device,
        root_filesystem_type=root_filesystem_type,
        root_uuid="8f1c0a2e-0000-4000-8000-000000000001",
        root_on_lvm=root_on_lvm,
        root_on_luks=root_on_luks,
        root_on_mdraid=root_on_mdraid,
        root_below_device="/dev/vda",
        boot_device="/dev/vda2",
        boot_same_filesystem=True,
        esp_device=esp_device,
        esp_mountpoint=esp_mountpoint,
        uefi=uefi,
        root_free_bytes=20 * 2**30,
    )


def test_the_running_layout_becomes_a_graph_that_formats_nothing() -> None:
    disk = convert.layout_graph(_layout())
    filesystems = [
        node for node in disk.graph.nodes.values() if isinstance(node, Filesystem)
    ]
    assert filesystems, "the graph has to describe the filesystems already there"
    assert all(not one.create for one in filesystems), filesystems
    assert disk.mode is DiskMode.IN_PLACE


def test_the_graph_names_the_devices_the_probe_read() -> None:
    disk = convert.layout_graph(_layout())
    selectors = {
        node.selector for node in disk.graph.nodes.values() if isinstance(node, Existing)
    }
    assert selectors == {"/dev/vda2", "/dev/vda1"}
    root = disk.graph[disk.root]
    assert isinstance(root, Mountpoint)
    assert root.path == PurePosixPath("/")


def test_the_esp_keeps_the_mountpoint_the_machine_already_uses() -> None:
    disk = convert.layout_graph(_layout(esp_mountpoint="/efi"))
    mounts = {
        node.path: node.options
        for node in disk.graph.nodes.values()
        if isinstance(node, Mountpoint)
    }
    assert mounts[PurePosixPath("/efi")] == ("umask=0077",)


def test_a_machine_without_uefi_gets_no_esp() -> None:
    disk = convert.layout_graph(_layout(uefi=False, esp_device=None, esp_mountpoint=None))
    assert not [
        node for node in disk.graph.nodes.values() if isinstance(node, Mountpoint)
        if node.path != PurePosixPath("/")
    ]


def test_uefi_without_an_esp_is_refused() -> None:
    with pytest.raises(ConversionUnsupported, match="no esp"):
        convert.layout_graph(_layout(esp_device=None))


def test_a_root_below_luks_is_refused_by_name() -> None:
    """One `Existing` node cannot describe a stack, and a conversion that
    guessed would rewrite a bootloader for a root it cannot unlock."""
    with pytest.raises(ConversionUnsupported, match="LUKS"):
        convert.layout_graph(_layout(root_on_luks=True))


def test_a_root_below_lvm_is_refused_by_name() -> None:
    with pytest.raises(ConversionUnsupported, match="LVM"):
        convert.layout_graph(_layout(root_on_lvm=True))


def test_a_root_below_mdraid_is_refused_by_name() -> None:
    with pytest.raises(ConversionUnsupported, match="mdraid"):
        convert.layout_graph(_layout(root_on_mdraid=True))


def test_a_filesystem_the_model_has_no_member_for_is_refused() -> None:
    with pytest.raises(ConversionUnsupported, match="reiserfs"):
        convert.layout_graph(_layout(root_filesystem_type="reiserfs"))


def test_a_root_device_that_could_not_be_read_is_refused() -> None:
    with pytest.raises(ConversionUnsupported, match="could not be read"):
        convert.layout_graph(_layout(root_device=None))
