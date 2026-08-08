from __future__ import annotations

from dataclasses import replace

import pytest

from gentoo_install.model.config import (
    Bootloader,
    BootloaderConfig,
    DiskConfig,
    Firmware,
    InstallConfig,
    KernelConfig,
    Overlay,
    PortageConfig,
)
from gentoo_install.model.device import FilesystemType, Luks, Swap, ZfsPool
from gentoo_install.model.size import Size
from gentoo_install.model.templates import Choice, Layout, build
from gentoo_install.model.validate import validate

#: Every template, with the bootloader each one has to be paired with.
CASES = [
    (Choice(disk="/dev/vda"), Bootloader.GRUB, Firmware.UEFI),
    (Choice(disk="/dev/vda", firmware=Firmware.BIOS), Bootloader.GRUB, Firmware.BIOS),
    (Choice(disk="/dev/vda", layout=Layout.WHOLE_DISK_BTRFS), Bootloader.GRUB, Firmware.UEFI),
    (
        Choice(disk="/dev/vda", layout=Layout.WHOLE_DISK_ZFS),
        Bootloader.ZFSBOOTMENU,
        Firmware.UEFI,
    ),
    (
        Choice(disk="/dev/vda", filesystem=FilesystemType.XFS, swap=Size.parse("4GiB")),
        Bootloader.GRUB,
        Firmware.UEFI,
    ),
]


def configured(choice: Choice, kind: Bootloader, firmware: Firmware) -> InstallConfig:
    graph, root = build(choice)
    portage = PortageConfig()
    if kind is Bootloader.ZFSBOOTMENU:
        # sys-boot/zfsbootmenu is in that overlay and in no other repository,
        # which the compatibility table states and this honours.
        portage = replace(
            portage,
            overlays=(Overlay(name="gentoo-zh", sync_uri="https://example.invalid/overlay.git"),),
        )
    return InstallConfig(
        disk=DiskConfig(graph=graph, root=root),
        portage=portage,
        kernel=KernelConfig(),
        bootloader=BootloaderConfig(kind=kind, firmware=firmware),
    )


@pytest.mark.parametrize("choice,kind,firmware", CASES)
def test_every_template_produces_a_configuration_that_validates(
    choice: Choice, kind: Bootloader, firmware: Firmware
) -> None:
    """A template that needs a hand edit before it installs is not a template."""
    validate(configured(choice, kind, firmware))


def test_a_passphrase_file_encrypts_the_layout_it_is_given() -> None:
    """The same field means LUKS under a filesystem and native encryption on a
    pool, because the two are never stacked."""
    plain, _ = build(Choice(disk="/dev/vda"))
    assert not plain.of_type(Luks)

    encrypted, _ = build(Choice(disk="/dev/vda", passphrase_file="/run/keys/root"))
    assert [node.passphrase_file for node in encrypted.of_type(Luks)] == ["/run/keys/root"]

    pool, _ = build(
        Choice(disk="/dev/vda", layout=Layout.WHOLE_DISK_ZFS, passphrase_file="/run/keys/pool")
    )
    assert not pool.of_type(Luks)
    assert [node.encrypted for node in pool.of_type(ZfsPool)] == [True]


def test_swap_is_a_partition_only_when_it_was_asked_for() -> None:
    without, _ = build(Choice(disk="/dev/vda"))
    assert not without.of_type(Swap)
    with_swap, _ = build(Choice(disk="/dev/vda", swap=Size.parse("2GiB")))
    assert len(with_swap.of_type(Swap)) == 1


def test_bios_gets_a_table_that_needs_no_bios_boot_partition() -> None:
    """GPT would need one for GRUB's stage 1.5; MBR uses the gap after the
    table, so the template avoids a partition nobody asked about."""
    validate(configured(Choice(disk="/dev/vda", firmware=Firmware.BIOS), Bootloader.GRUB, Firmware.BIOS))


def test_the_btrfs_template_matches_the_calamares_subvolume_list() -> None:
    """Feature parity with the GUI installer is the bar: `mount.conf` of
    `calamares-settings-gig` lists `/@`, `/@home`, `/@cache` and `/@log`, and a
    system installed either way has to keep its churn in the same places."""
    from pathlib import PurePosixPath

    from gentoo_install.model.device import Mountpoint, Subvolume
    from gentoo_install.model.templates import SUBVOLUMES, build

    graph, _ = build(Choice(disk="/dev/vda", layout=Layout.WHOLE_DISK_BTRFS))
    assert {node.name for node in graph.of_type(Subvolume)} == {"@", "@home", "@cache", "@log"}
    mounted = {
        node.path: node.options
        for node in graph.of_type(Mountpoint)
        if isinstance(graph[node.source], Subvolume)
    }
    assert set(mounted) == {PurePosixPath(where) for _, where in SUBVOLUMES}
    assert all(options == ("compress=zstd:1",) for options in mounted.values())
