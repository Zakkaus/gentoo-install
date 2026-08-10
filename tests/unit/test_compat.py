from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Callable

import pytest

from gentoo_install.model.compat import RULES, Trait, esp_mount, excluded_by, traits_of, violations
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
    RemoteUnlock,
    PortageConfig,
    SystemConfig,
)
from gentoo_install.model.device import (
    Existing,
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


def an_encrypted_esp() -> InstallConfig:
    """A LUKS container between the firmware and the esp. The operator can
    build it a partition at a time, and nothing about it ever boots."""
    from gentoo_install.model.device import Luks

    nodes = [node for node in ext4_on_gpt() if node.id not in (i("espfs"), i("mnt-esp"))]
    nodes += [
        Luks(id=i("espcrypt"), backing=i("esp"), name="esp"),
        Filesystem(id=i("espfs"), device=i("espcrypt"), kind=FilesystemType.VFAT, label="ESP"),
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/efi")),
    ]
    return config(nodes)


def systemd_boot_on_bios() -> InstallConfig:
    return boots(config(), Bootloader.SYSTEMD_BOOT, Firmware.BIOS)


def systemd_boot_with_the_kernel_on_ext4() -> InstallConfig:
    """A separate ext4 /boot, which is the case the loader really cannot read.

    An esp at `/efi` with `/boot` an ordinary directory is bootctl's own
    recommendation and no longer counts: `kernel-install` writes to the esp
    there, and refusing it made systemd-boot unselectable from the default
    layout.
    """
    from gentoo_install.model.device import Partition, PartitionRole
    from gentoo_install.model.size import Size

    nodes = list(ext4_on_gpt())
    top = max(one.index for one in nodes if isinstance(one, Partition))
    nodes += [
        Partition(
            id=i("bootpart"),
            table=i("table"),
            index=top + 1,
            role=PartitionRole.DATA,
            size=Size.parse("1GiB"),
        ),
        Filesystem(id=i("bootfs"), device=i("bootpart"), kind=FilesystemType.EXT4),
        Mountpoint(id=i("mnt-boot"), source=i("bootfs"), path=PurePosixPath("/boot")),
    ]
    return boots(config(nodes), Bootloader.SYSTEMD_BOOT, Firmware.UEFI)


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


def zfsbootmenu_without_a_pool() -> InstallConfig:
    """ZFSBootMenu imports a pool and boots a dataset out of it, so an ext4
    root leaves it with nothing to import and the machine with nothing to
    boot."""
    return replace(
        boots(config(), Bootloader.ZFSBOOTMENU, Firmware.UEFI),
        portage=PortageConfig(
            overlays=(Overlay(name="gentoo-zh", sync_uri="https://example.invalid/overlay.git"),)
        ),
    )


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


def remote_unlock_without_a_key() -> InstallConfig:
    """dracut-crypt-ssh authorises `/root/.ssh/authorized_keys`, so with none
    the initramfs runs a daemon nobody can log into."""
    encrypted = [node for node in ext4_on_gpt() if node.id != i("rootfs")]
    encrypted += [
        Luks(id=i("crypt"), backing=i("rootpart"), name="root"),
        Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.EXT4),
    ]
    return replace(
        config(encrypted),
        kernel=KernelConfig(remote_unlock=RemoteUnlock(enabled=True)),
        system=replace(config().system, authorized_keys=()),
    )


def remote_unlock_of_an_unencrypted_root() -> InstallConfig:
    return replace(
        config(),
        kernel=KernelConfig(remote_unlock=RemoteUnlock(enabled=True)),
        system=replace(config().system, authorized_keys=("ssh-ed25519 AAAA k",)),
    )


def a_system_nothing_can_log_into() -> InstallConfig:
    """No root password, no user and no key: the machine boots and refuses
    every login. `zfs-zbm.toml` was this until a VM run reached its prompt."""
    return replace(config(), system=SystemConfig())


CASES: list[tuple[Callable[[], InstallConfig], Trait, Trait]] = [
    (a_system_nothing_can_log_into, Trait.ROOT_LOCKED, Trait.NO_OTHER_LOGIN),
    (zfs_on_grub, Trait.ROOT_ON_ZFS, Trait.GRUB),
    (zfs_on_bios, Trait.ROOT_ON_ZFS, Trait.BIOS_BOOT),
    (zfs_over_luks, Trait.ROOT_ON_ZFS, Trait.LUKS),
    (uefi_without_an_esp, Trait.UEFI_BOOT, Trait.NO_MOUNTED_ESP),
    (an_encrypted_esp, Trait.UEFI_BOOT, Trait.ESP_ENCRYPTED),
    (systemd_boot_on_bios, Trait.SYSTEMD_BOOT, Trait.BIOS_BOOT),
    (systemd_boot_with_the_kernel_on_ext4, Trait.SYSTEMD_BOOT, Trait.KERNEL_OFF_ESP),
    (esp_on_a_mirror, Trait.ESP_ON_MDRAID, Trait.ESP_MDRAID_SUPERBLOCK_AT_START),
    (bios_on_gpt_without_a_bios_boot_partition, Trait.BIOS_BOOT, Trait.GPT_WITHOUT_BIOS_BOOT),
    (cjk_console_on_an_unpatched_kernel, Trait.CONSOLE_CJK, Trait.KERNEL_WITHOUT_CJKTTY),
    (community_binhost_without_its_overlay, Trait.COMMUNITY_BINHOST, Trait.NO_GENTOOZH_OVERLAY),
    (zfsbootmenu_without_its_overlay, Trait.ZFSBOOTMENU, Trait.NO_GENTOOZH_OVERLAY),
    (zfsbootmenu_without_a_pool, Trait.ZFSBOOTMENU, Trait.NO_ZFS_ROOT),
    (cjk_console_with_an_8x8_font, Trait.CONSOLE_CJK, Trait.FONT_WITHOUT_CJK_GLYPHS),
    (the_patched_kernel_without_its_overlay, Trait.CJK_KERNEL, Trait.NO_GENTOOZH_OVERLAY),
    (remote_unlock_without_a_key, Trait.REMOTE_UNLOCK, Trait.NO_AUTHORIZED_KEY),
    (remote_unlock_of_an_unencrypted_root, Trait.REMOTE_UNLOCK, Trait.NO_ENCRYPTED_CONTAINER),
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


def test_an_esp_at_boot_efi_is_a_layout_this_installer_accepts() -> None:
    """`calamares-settings-gig` mounts it there, and the GUI installer's
    layouts are the parity bar. Only `/efi` and `/boot` were accepted, so a
    layout the operator can build a partition at a time was refused with
    `an EFI executable has to live on a vfat esp mounted in the target`."""
    nodes = [node for node in ext4_on_gpt() if node.id != i("mnt-esp")]
    nodes.append(
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/boot/efi"))
    )
    there = config(nodes)
    assert violations(there) == ()
    mount = esp_mount(there.disk.graph)
    assert mount is not None and str(mount.path) == "/boot/efi"


def test_the_bootloader_is_installed_where_the_esp_is_actually_mounted() -> None:
    """Accepting the path is not the same as using it: `grub-install` takes an
    `--efi-directory`, and a hardcoded one writes the executable somewhere the
    firmware does not read."""
    from tests.unit.recorder import Recorder

    from gentoo_install.plan import bootloader as plan_bootloader

    nodes = [node for node in ext4_on_gpt() if node.id != i("mnt-esp")]
    nodes.append(
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/boot/efi"))
    )
    recorder = Recorder()
    for operation in plan_bootloader.build(config(nodes)):
        operation.apply(recorder)
    installed = [one for one in recorder.in_target if one and one[0] == "grub-install"]
    assert installed, recorder.in_target
    assert all("--efi-directory=/boot/efi" in one for one in installed), installed


def test_formatting_a_partition_the_operator_kept_counts_as_destruction() -> None:
    """`Existing.wipe` is false and no table is rewritten, so both the erase
    confirmation and the in-use check read a format-only reuse layout as
    harmless while `mkfs` runs over the operator's data."""
    from pathlib import PurePosixPath

    from gentoo_install.model import compat
    from gentoo_install.model.device import (
        DeviceGraph,
        DeviceId,
        Existing,
        Filesystem,
        FilesystemType,
        Mountpoint,
    )

    kept = [
        Existing(id=DeviceId("part"), selector="/dev/sda2", wipe=False),
        Filesystem(
            id=DeviceId("fs"), device=DeviceId("part"), kind=FilesystemType.EXT4, create=False
        ),
        Mountpoint(id=DeviceId("root"), source=DeviceId("fs"), path=PurePosixPath("/")),
    ]
    assert not compat.destroyed(DeviceGraph.build(kept))

    formatted = [
        Existing(id=DeviceId("part"), selector="/dev/sda2", wipe=False),
        Filesystem(
            id=DeviceId("fs"), device=DeviceId("part"), kind=FilesystemType.EXT4, create=True
        ),
        Mountpoint(id=DeviceId("root"), source=DeviceId("fs"), path=PurePosixPath("/")),
    ]
    named = compat.destroyed(DeviceGraph.build(formatted))
    assert [one.selector for one in named] == ["/dev/sda2"]


def test_the_erase_row_and_the_in_use_check_read_the_same_rule() -> None:
    """Two lists of destructive targets drift, and the one that drifted was the
    preflight copy: it named the disk only when a table was written."""
    import inspect

    from gentoo_install.exec import preflight
    from gentoo_install.tui import settings

    for source in (inspect.getsource(preflight._disks_at_risk), inspect.getsource(settings._erase)):
        assert "compat.destroyed(" in source, source


def test_an_encrypted_boot_beside_a_plain_esp_is_a_working_layout() -> None:
    """`/boot` is one of the paths an esp can take, so an unrelated encrypted
    `/boot` beside a plain vfat esp at `/efi` was refused as an encrypted esp
    while the firmware could read the esp perfectly well."""
    from gentoo_install.model import compat
    from gentoo_install.model.device import Luks, Partition, PartitionRole
    from gentoo_install.model.size import Size

    base = list(ext4_on_gpt())
    top = max(one.index for one in base if isinstance(one, Partition))
    beside = base + [
        Partition(
            id=i("bootpart"),
            table=i("table"),
            index=top + 1,
            role=PartitionRole.DATA,
            size=Size.parse("1GiB"),
        ),
        Luks(id=i("bootcrypt"), backing=i("bootpart"), name="boot"),
        Filesystem(id=i("bootfs"), device=i("bootcrypt"), kind=FilesystemType.EXT4),
        Mountpoint(id=i("mnt-boot"), source=i("bootfs"), path=PurePosixPath("/boot")),
    ]
    assert Trait.ESP_ENCRYPTED not in compat.traits_of(config(beside))
    # And the real case still fires.
    assert Trait.ESP_ENCRYPTED in compat.traits_of(an_encrypted_esp())


def test_systemd_boot_is_installable_on_the_layout_the_installer_offers() -> None:
    """bootctl(1): "/efi/, /boot/, and /boot/efi/ are checked in turn. It is
    recommended to mount the ESP to /efi/." The rule demanded the esp at
    `/boot`, so the loader could not be chosen from the default layout at all
    and the row's own reason told the operator to do something the menu does
    not offer."""
    from gentoo_install.model.validate import validate

    validate(boots(config(), Bootloader.SYSTEMD_BOOT, Firmware.UEFI))


def test_a_separate_vfat_boot_is_still_readable() -> None:
    """systemd-boot has a vfat driver, so an XBOOTLDR partition it can read is
    not the failing case."""
    from gentoo_install.model import compat
    from gentoo_install.model.device import Partition, PartitionRole
    from gentoo_install.model.size import Size

    nodes = list(ext4_on_gpt())
    top = max(one.index for one in nodes if isinstance(one, Partition))
    nodes += [
        Partition(
            id=i("bootpart"),
            table=i("table"),
            index=top + 1,
            role=PartitionRole.DATA,
            size=Size.parse("1GiB"),
        ),
        Filesystem(id=i("bootfs"), device=i("bootpart"), kind=FilesystemType.VFAT),
        Mountpoint(id=i("mnt-boot"), source=i("bootfs"), path=PurePosixPath("/boot")),
    ]
    assert Trait.KERNEL_OFF_ESP not in compat.traits_of(
        boots(config(nodes), Bootloader.SYSTEMD_BOOT, Firmware.UEFI)
    )


def reused_esp(metadata: str) -> list[Node]:
    """An esp on an array already assembled on the machine.

    `Existing` and nothing else: the model cannot read a superblock, so the
    version is injected by the probe before validation.
    """
    nodes: list[Node] = [
        node
        for node in ext4_on_gpt()
        if node.id not in {i("espfs"), i("mnt-esp"), i("esp")}
    ]
    nodes += [
        Existing(id=i("esp"), selector="/dev/md0", mdraid_metadata=metadata),
        Filesystem(id=i("espfs"), device=i("esp"), kind=FilesystemType.VFAT),
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/efi")),
    ]
    return nodes


def test_a_reused_array_under_the_esp_meets_the_firmware_rule() -> None:
    """An esp the plan creates carries an `MdRaid` node; a reused one is
    `Existing` and carries nothing, so a RAID1 esp with metadata 1.1 or 1.2
    met no rule at all although firmware cannot read a member whose superblock
    sits at the start."""
    at_start = traits_of(config(reused_esp("1.2")))
    at_end = traits_of(config(reused_esp("1.0")))

    assert Trait.ESP_ON_MDRAID in at_start
    assert Trait.ESP_MDRAID_SUPERBLOCK_AT_START in at_start
    assert Trait.ESP_ON_MDRAID in at_end
    assert Trait.ESP_MDRAID_SUPERBLOCK_AT_START not in at_end


def test_a_mirror_with_no_ipv6_is_refused_on_an_ipv6_only_machine() -> None:
    """Four of the mirrors publish no AAAA record and one publishes an AAAA it
    does not answer on. An IPv6-only machine reaches none of them, and finding
    that out when the stage3 does not arrive is an hour lost."""
    from dataclasses import replace as _replace

    from gentoo_install.data import load_catalog
    from gentoo_install.i18n import Catalog
    from gentoo_install.model import mirrors
    from gentoo_install.tui.screens import Context, _unreachable_here

    def machine(ipv4: bool) -> Context:
        return Context(
            translate=Catalog("en"),
            disks=(),
            groups=load_catalog(),
            hash_password=lambda text: "",
            ipv4=ipv4,
            ipv6=True,
        )

    without_v6 = [one for one in mirrors.GENTOO_SITES if not one.ipv6]
    assert {one.key for one in without_v6} == {
        "aliyun",
        "netease",
        "rackspace-hk",
        "aditsu-hk",
        "nchc-tw",
    }
    for site in without_v6:
        assert _unreachable_here(site, machine(ipv4=False)), site.key
        assert not _unreachable_here(site, machine(ipv4=True)), site.key
    for site in mirrors.GENTOO_SITES:
        if site.ipv6:
            assert not _unreachable_here(site, machine(ipv4=False)), site.key
