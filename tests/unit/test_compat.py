from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Callable

import pytest

from gentoo_install.model.compat import RULES, Trait, excluded_by, traits_of, violations
from gentoo_install.model.config import (
    Binhost,
    BinhostChannel,
    Bootloader,
    BootloaderConfig,
    ConsoleFontSize,
    Firmware,
    InstallConfig,
    KernelConfig,
    KernelSource,
    Overlay,
    PortageConfig,
    SystemConfig,
)
from gentoo_install.model.device import (
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
    TableType,
    ZfsPool,
)
from gentoo_install.model.size import Size

from .layouts import config, ext4_on_gpt, i, zfs_root


def boots(config_: InstallConfig, kind: Bootloader, firmware: Firmware) -> InstallConfig:
    return replace(config_, bootloader=BootloaderConfig(kind=kind, firmware=firmware))


def zfs_on_grub() -> InstallConfig:
    return config(zfs_root())


def zfs_on_bios() -> InstallConfig:
    return boots(config(zfs_root()), Bootloader.ZFSBOOTMENU, Firmware.BIOS)


def zfs_over_luks() -> InstallConfig:
    nodes: list[Node] = [node for node in zfs_root() if node.id != i("pool")]
    nodes += [
        Luks(id=i("crypt"), backing=i("poolpart"), name="crypt"),
        ZfsPool(id=i("pool"), vdevs=(i("crypt"),), name="zpcala"),
    ]
    return boots(config(nodes), Bootloader.ZFSBOOTMENU, Firmware.UEFI)


def uefi_without_an_esp() -> InstallConfig:
    return config([node for node in ext4_on_gpt() if node.id != i("mnt-esp")])


def systemd_boot_on_bios() -> InstallConfig:
    return boots(config(), Bootloader.SYSTEMD_BOOT, Firmware.BIOS)


def systemd_boot_with_the_kernel_on_ext4() -> InstallConfig:
    return boots(config(), Bootloader.SYSTEMD_BOOT, Firmware.UEFI)


def mirrored_esp() -> list[Node]:
    """Two esp members in a mirror, with the metadata version mdadm defaults to."""
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id not in {i("espfs"), i("mnt-esp")}]
    nodes += [
        Partition(id=i("esp2"), table=i("table"), index=3, role=PartitionRole.ESP, size=Size.parse("512MiB")),
        MdRaid(id=i("esp-mirror"), members=(i("esp"), i("esp2")), level=RaidLevel.RAID1, name="esp"),
        Filesystem(id=i("espfs"), device=i("esp-mirror"), kind=FilesystemType.VFAT),
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/efi")),
    ]
    return nodes


def esp_on_a_mirror() -> InstallConfig:
    return config(mirrored_esp())


def bios_on_gpt_without_a_bios_boot_partition() -> InstallConfig:
    return boots(config(), Bootloader.GRUB, Firmware.BIOS)


def cjk_console_on_an_unpatched_kernel() -> InstallConfig:
    return replace(
        config(),
        system=SystemConfig(console_cjk=True),
        kernel=KernelConfig(source=KernelSource.DIST_SOURCE),
    )


def zfsbootmenu_without_its_overlay() -> InstallConfig:
    return boots(config(zfs_root()), Bootloader.ZFSBOOTMENU, Firmware.UEFI)


def community_binhost_without_its_overlay() -> InstallConfig:
    return replace(
        config(),
        portage=PortageConfig(binhost=Binhost(official=True, community=BinhostChannel.STABLE)),
    )


def cjk_console_with_an_8x8_font() -> InstallConfig:
    return replace(
        config(),
        system=SystemConfig(console_cjk=True, console_font=ConsoleFontSize.SIZE_8X8),
        kernel=KernelConfig(source=KernelSource.CJK),
    )


def the_patched_kernel_without_its_overlay() -> InstallConfig:
    return replace(config(), kernel=KernelConfig(source=KernelSource.CJK))


CASES: list[tuple[Callable[[], InstallConfig], Trait, Trait]] = [
    (zfs_on_grub, Trait.ROOT_ON_ZFS, Trait.GRUB),
    (zfs_on_bios, Trait.ROOT_ON_ZFS, Trait.BIOS_BOOT),
    (zfs_over_luks, Trait.ROOT_ON_ZFS, Trait.LUKS),
    (uefi_without_an_esp, Trait.UEFI_BOOT, Trait.NO_MOUNTED_ESP),
    (systemd_boot_on_bios, Trait.SYSTEMD_BOOT, Trait.BIOS_BOOT),
    (systemd_boot_with_the_kernel_on_ext4, Trait.SYSTEMD_BOOT, Trait.KERNEL_OFF_ESP),
    (esp_on_a_mirror, Trait.ESP_ON_MDRAID, Trait.ESP_MDRAID_SUPERBLOCK_AT_START),
    (bios_on_gpt_without_a_bios_boot_partition, Trait.BIOS_BOOT, Trait.GPT_WITHOUT_BIOS_BOOT),
    (cjk_console_on_an_unpatched_kernel, Trait.CONSOLE_CJK, Trait.KERNEL_WITHOUT_CJKTTY),
    (community_binhost_without_its_overlay, Trait.COMMUNITY_BINHOST, Trait.NO_GENTOOZH_OVERLAY),
    (zfsbootmenu_without_its_overlay, Trait.ZFSBOOTMENU, Trait.NO_GENTOOZH_OVERLAY),
    (cjk_console_with_an_8x8_font, Trait.CONSOLE_CJK, Trait.FONT_WITHOUT_CJK_GLYPHS),
    (the_patched_kernel_without_its_overlay, Trait.CJK_KERNEL, Trait.NO_GENTOOZH_OVERLAY),
]


@pytest.mark.parametrize(("build", "when", "excludes"), CASES)
def test_each_rule_fires_on_a_configuration_that_breaks_it(
    build: Callable[[], InstallConfig], when: Trait, excludes: Trait
) -> None:
    assert (when, excludes) in {(rule.when, rule.excludes) for rule in violations(build())}


def test_every_rule_in_the_table_has_a_case_that_breaks_it() -> None:
    assert {(when, excludes) for _, when, excludes in CASES} == {
        (rule.when, rule.excludes) for rule in RULES
    }


def test_a_plain_uefi_install_breaks_no_rule() -> None:
    assert violations(config()) == ()


def test_zfs_with_zfsbootmenu_and_the_kernel_in_the_pool_breaks_no_rule() -> None:
    installable = replace(
        boots(config(zfs_root()), Bootloader.ZFSBOOTMENU, Firmware.UEFI),
        portage=PortageConfig(
            overlays=(Overlay(name="gentoo-zh", sync_uri="https://example.invalid/overlay.git"),)
        ),
    )
    assert violations(installable) == ()


def test_a_mirrored_esp_with_metadata_1_0_breaks_no_rule() -> None:
    nodes = [
        replace(node, metadata=RaidMetadata.V1_0) if isinstance(node, MdRaid) else node
        for node in mirrored_esp()
    ]
    assert violations(config(nodes)) == ()


def test_two_broken_rules_are_both_reported() -> None:
    both = boots(config(zfs_root()), Bootloader.GRUB, Firmware.BIOS)
    assert {rule.excludes for rule in violations(both)} >= {Trait.GRUB, Trait.BIOS_BOOT}


def test_the_reason_reads_as_one_sentence_the_interface_can_show() -> None:
    described = violations(zfs_on_grub())[0].describe()
    assert described.startswith("root on ZFS excludes GRUB: ")
    assert "feature flags" in described


def test_the_interface_learns_what_a_choice_excludes_before_it_is_made() -> None:
    excluded = {rule.excludes for rule in excluded_by(frozenset({Trait.ROOT_ON_ZFS}))}
    assert excluded == {Trait.GRUB, Trait.BIOS_BOOT, Trait.LUKS}


def test_an_esp_mounted_at_boot_puts_the_kernel_on_the_esp() -> None:
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id != i("mnt-esp")]
    nodes.append(Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/boot")))
    assert Trait.KERNEL_OFF_ESP not in traits_of(config(nodes))


def test_a_gpt_boot_disk_with_a_bios_boot_partition_satisfies_bios() -> None:
    nodes = ext4_on_gpt()
    nodes.append(
        Partition(id=i("bios"), table=i("table"), index=3, role=PartitionRole.BIOS_BOOT, size=Size.parse("2MiB"))
    )
    assert Trait.GPT_WITHOUT_BIOS_BOOT not in traits_of(config(nodes))


def test_an_mbr_boot_disk_needs_no_bios_boot_partition() -> None:
    nodes = [
        replace(node, table=TableType.MBR) if isinstance(node, PartitionTable) else node
        for node in ext4_on_gpt()
    ]
    assert Trait.GPT_WITHOUT_BIOS_BOOT not in traits_of(config(nodes))


def test_a_root_id_no_node_defines_yields_no_trait_rather_than_raising() -> None:
    broken = replace(config(), disk=replace(config().disk, root=i("absent")))
    assert Trait.ROOT_ON_ZFS not in traits_of(broken)
