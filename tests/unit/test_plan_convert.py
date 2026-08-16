# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gentoo_install.model.config import InstallConfig
from gentoo_install.plan import disk as plan_disk
from gentoo_install.plan import portage as plan_portage
from gentoo_install.plan.bootloader import InstallGrub
from gentoo_install.plan.build import build
from gentoo_install.plan.convert import SwapDirectories
from gentoo_install.plan.operations import Stage
from gentoo_install.model.config import DiskConfig, DiskMode
from gentoo_install.errors import ConversionFailed, ConversionUnsupported
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
    root_free_bytes: int | None = 20 * 2**30,
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
        root_free_bytes=root_free_bytes,
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


def test_the_conversion_stages_everything_then_swaps_then_writes_the_bootloader() -> None:
    """The stage order the rest of the installer sorts by puts the bootloader
    before packages. A conversion cannot: the bootloader writes to the root the
    machine will boot from, and that is the old one until the swap happens."""
    operations = build(_in_place(), CATALOG, layout=_layout())
    swapped = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, SwapDirectories)
    )
    assert all(isinstance(one, convert.Staged) for one in operations[:swapped])
    assert not any(isinstance(one, convert.Staged) for one in operations[swapped + 1 :])
    assert operations[swapped + 1 :], "the bootloader has to follow the swap"


def test_nothing_before_the_swap_writes_outside_the_staging_root() -> None:
    operations = build(_in_place(), CATALOG, layout=_layout())
    staged = [one for one in operations if isinstance(one, convert.Staged)]
    assert staged
    assert all(str(one.staging) == "/gentoo-install.new" for one in staged)
    assert all("/gentoo-install.new" in one.describe() for one in staged)


def test_a_conversion_without_a_probed_layout_is_refused() -> None:
    with pytest.raises(ConversionUnsupported, match="was not read"):
        build(_in_place(), CATALOG)


def test_the_conversion_formats_nothing_and_mounts_nothing() -> None:
    """The machine is already running on these filesystems."""
    operations = build(_in_place(), CATALOG, layout=_layout())
    stages = {
        (one.inner.stage if isinstance(one, convert.Staged) else one.stage)
        for one in operations
    }
    assert Stage.FORMAT not in stages
    assert Stage.PARTITION not in stages
    assert Stage.MOUNT not in stages


def test_the_kernel_reaches_boot_between_the_swap_and_the_bootloader() -> None:
    """`grub-mkconfig` reads `/boot`, and until this runs what is there belongs
    to the old distribution."""
    operations = build(_in_place(), CATALOG, layout=_layout())
    swapped = next(
        index for index, one in enumerate(operations) if isinstance(one, SwapDirectories)
    )
    populated = next(
        index for index, one in enumerate(operations)
        if isinstance(one, convert.PopulateBoot)
    )
    grub = next(
        index for index, one in enumerate(operations)
        if type(one).__name__ == "InstallGrub"
    )
    assert swapped < populated < grub


def test_boot_is_not_one_of_the_directories_that_are_renamed() -> None:
    """A rename refuses a mount point and a directory holding one, and `/boot`
    is one on many machines and holds the esp on many more."""
    assert "boot" not in convert.REPLACED_DIRECTORIES


class _RunningContext:
    """A context that records commands and answers `findmnt` from a list."""

    def __init__(self, mounted: tuple[str, ...] = ()) -> None:
        self.mounted = mounted
        self.ran: list[list[str]] = []

    target = PurePosixPath("/")

    answers: dict[str, str] = {}

    def run(self, argv: list[str], *, check: bool = True, input_text: str | None = None) -> str:
        self.ran.append(argv)
        if argv[0] == "findmnt":
            return "\n".join(self.mounted)
        return self.answers.get(argv[0], "")


def test_the_staging_root_is_unmounted_and_removed() -> None:
    context = _RunningContext(mounted=("/", "/boot"))
    convert.LeaveStaging().apply(cast(Any, context))
    assert ["umount", "--recursive", "--lazy", "/gentoo-install.new"] in context.ran
    assert ["rm", "--recursive", "--force", "/gentoo-install.new"] in context.ran


def test_a_staging_root_with_something_still_mounted_is_left_alone() -> None:
    """`rm` walking into a `/proc` still bound there is not a risk worth taking
    to tidy up a machine that is already converted."""
    context = _RunningContext(mounted=("/", "/gentoo-install.new/proc"))
    with pytest.raises(ConversionFailed, match="still has something mounted"):
        convert.LeaveStaging().apply(cast(Any, context))
    assert not any(argv[0] == "rm" for argv in context.ran)


def test_the_conversion_ends_by_leaving_no_staging_root() -> None:
    operations = build(_in_place(), CATALOG, layout=_layout())
    assert isinstance(operations[-1], convert.LeaveStaging)


def test_only_the_esp_and_boot_sector_writes_wait_for_the_swap() -> None:
    """Emerging the bootloader and writing `/etc/default/grub` are ordinary
    staged work, and leaving them in the irreversible window made it minutes
    long for no reason."""
    from gentoo_install.plan.bootloader import InstallGrub

    operations = build(_in_place(), CATALOG, layout=_layout())
    swapped = next(
        index for index, one in enumerate(operations) if isinstance(one, SwapDirectories)
    )
    before = operations[:swapped]
    after = operations[swapped:]
    assert any(
        isinstance(one, convert.Staged) and type(one.inner).__name__ == "Emerge"
        and "bootloader" in one.inner.describe()
        for one in before
    ), "the bootloader package is emerged before the swap"
    assert any(
        isinstance(one, convert.Staged) and type(one.inner).__name__ == "WriteGrubDefaults"
        for one in before
    )
    assert any(isinstance(one, InstallGrub) for one in after)
    assert not any(isinstance(one, InstallGrub) for one in before)


def test_a_root_with_no_room_for_both_systems_is_refused() -> None:
    """The staged system and the running one are on it at the same time, and
    running out half way leaves a staging root and nothing converted."""
    with pytest.raises(ConversionUnsupported, match="4 GiB free"):
        convert.layout_graph(_layout(root_free_bytes=4 * 2**30))


def test_a_root_with_room_is_accepted() -> None:
    convert.layout_graph(_layout(root_free_bytes=convert.CONVERSION_FREE_BYTES))


def test_an_unknown_amount_of_room_is_not_a_small_one() -> None:
    """`findmnt` not reporting `avail` is a reason to carry on: refusing there
    would stop machines that are fine."""
    convert.layout_graph(_layout(root_free_bytes=None))


def test_the_staging_root_is_created_before_anything_is_written() -> None:
    """The ordinary path gets `/mnt/gentoo` from the mount operations, and a
    conversion has none, so `tar --directory` was the first thing to find out."""
    operations = build(_in_place(), CATALOG, layout=_layout())
    first = operations[0]
    assert isinstance(first, convert.Staged)
    assert isinstance(first.inner, convert.PrepareStaging)


def test_a_staging_root_left_from_an_earlier_attempt_is_refused() -> None:
    context = _RunningContext()
    context.answers = {"find": "/gentoo-install.new/usr"}
    with pytest.raises(ConversionFailed, match="earlier attempt"):
        convert.PrepareStaging().apply(cast(Any, context))


def test_an_empty_staging_root_is_accepted() -> None:
    context = _RunningContext()
    convert.PrepareStaging().apply(cast(Any, context))
    assert ["mkdir", "--parents", "/gentoo-install.new"] in context.ran
