# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Callable

import pytest

from gentoo_install.model import compat
from gentoo_install.model.compat import RULES, Trait, esp_mount, excluded_by, traits_of, violations
from gentoo_install.model.config import (
    Binhost,
    BinhostChannel,
    DiskMode,
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
    User,
)
from gentoo_install.model.device import (
    DeviceGraph,
    Existing,
    Filesystem,
    FilesystemType,
    LogicalVolume,
    Luks,
    MdRaid,
    MdraidMetadataState,
    Mountpoint,
    Node,
    Partition,
    PartitionRole,
    PartitionTable,
    RaidLevel,
    RaidMetadata,
    StorageFacts,
    Swap,
    TableType,
    VolumeGroup,
    ZfsDataset,
    ZfsPool,
)
from gentoo_install.exec.config import load
from gentoo_install.model.size import Size

from .layouts import config, ext4_on_gpt, i, zfs_root

VALID_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB+85deBslaLOMFw71dx23wo7fFT76GVcEyQS9IdVvvT "
    "installer@example"
)


def boots(config_: InstallConfig, kind: Bootloader, firmware: Firmware) -> InstallConfig:
    return replace(config_, bootloader=BootloaderConfig(kind=kind, firmware=firmware))


def zfs_on_grub() -> InstallConfig:
    return config(zfs_root())


def a_kernel_on_zfs_under_grub() -> InstallConfig:
    """An ext4 root with `/boot` on its own pool. Every rule was read from the
    root's device chain, so this passed all of them and GRUB then could not
    read the pool the kernel was on."""
    from gentoo_install.model.device import ZfsDataset

    return boots(
        config(
            [
                *ext4_on_gpt(),
                Existing(id=i("bootdisk"), selector="/dev/disk/by-id/virtio-boot", wipe=True),
                ZfsPool(id=i("bpool"), vdevs=(i("bootdisk"),), name="bpool"),
                ZfsDataset(id=i("ds-boot"), pool=i("bpool"), name="BOOT/gentoo"),
                Mountpoint(id=i("mnt-boot"), source=i("ds-boot"), path=PurePosixPath("/boot")),
            ]
        ),
        Bootloader.GRUB,
        Firmware.UEFI,
    )


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


def in_place_zfsbootmenu() -> InstallConfig:
    installation = config()
    return replace(
        installation,
        disk=replace(installation.disk, mode=DiskMode.IN_PLACE),
        bootloader=replace(installation.bootloader, kind=Bootloader.ZFSBOOTMENU),
    )


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


def systemd_boot_with_encrypted_vfat_boot() -> InstallConfig:
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
        Luks(id=i("bootcrypt"), backing=i("bootpart"), name="boot"),
        Filesystem(id=i("bootfs"), device=i("bootcrypt"), kind=FilesystemType.VFAT),
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


def remote_unlock_of_a_native_zfs_root(kind: Bootloader) -> InstallConfig:
    nodes = [
        replace(node, encrypted=True, passphrase_file="/run/keys/pool")
        if isinstance(node, ZfsPool)
        else node
        for node in zfs_root()
    ]
    installation = boots(config(nodes), kind, Firmware.UEFI)
    return replace(
        installation,
        system=replace(installation.system, authorized_keys=(VALID_KEY,)),
        kernel=KernelConfig(remote_unlock=RemoteUnlock(enabled=True)),
        portage=PortageConfig(
            overlays=(
                Overlay(
                    name="gentoo-zh",
                    sync_uri="https://example.invalid/overlay.git",
                ),
            )
        ),
    )


def native_zfs_remote_unlock_with_systemd_boot() -> InstallConfig:
    return remote_unlock_of_a_native_zfs_root(Bootloader.SYSTEMD_BOOT)


def remote_unlock_with_boot_inside_the_container() -> InstallConfig:
    """`vm-unlock` was this. The console held `Enter passphrase for hd0,gpt2`
    fifty-one minutes into the round: GRUB unlocks /boot before it can read a
    kernel, so it asked at the physical console and the initramfs that serves
    the ssh daemon never started."""
    encrypted = [node for node in ext4_on_gpt() if node.id != i("rootfs")]
    encrypted += [
        Luks(id=i("crypt"), backing=i("rootpart"), name="root"),
        Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.EXT4),
    ]
    installation = config(encrypted)
    return replace(
        installation,
        kernel=KernelConfig(remote_unlock=RemoteUnlock(enabled=True)),
        system=replace(installation.system, authorized_keys=(VALID_KEY,)),
    )


def a_system_nothing_can_log_into() -> InstallConfig:
    """No root password, no user and no key: the machine boots and refuses
    every login. `zfs-zbm.toml` was this until a VM run reached its prompt."""
    return replace(config(), system=SystemConfig())


def a_key_no_account_can_use() -> InstallConfig:
    """`plan/system.py` writes every key to `/root/.ssh/authorized_keys` when
    no user has sudo, and sshd is given `PermitRootLogin no` unless root login
    was asked for. A working root password kept this out of `ROOT_LOCKED`, so
    the machine booted with keys nothing would accept."""
    return replace(
        config(),
        system=SystemConfig(
            root_password_hash="$6$salt$" + "x" * 86,
            authorized_keys=("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI zakk@host",),
        ),
    )


def an_esp_mdadm_could_not_read() -> InstallConfig:
    """mdadm asked and unable to answer, which is not the same as answering
    that the device is no array. Reading the two alike accepted an esp on a
    1.2 array, which the firmware cannot read as plain vfat."""
    return config(reused_esp())


#: The facts a case needs to reach its rule. Most rules read the graph alone;
#: these read what the machine answered about a device already on it.
FACTS_FOR: dict[Callable[[], InstallConfig], StorageFacts] = {
    an_esp_mdadm_could_not_read: StorageFacts(
        mdraid_metadata={i("esp"): MdraidMetadataState.UNAVAILABLE}
    ),
}


CASES: list[tuple[Callable[[], InstallConfig], Trait, Trait]] = [
    (an_esp_mdadm_could_not_read, Trait.UEFI_BOOT, Trait.ESP_MDRAID_UNREADABLE),
    (a_key_no_account_can_use, Trait.AUTHORIZED_KEYS, Trait.NO_ACCOUNT_A_KEY_CAN_REACH),
    (a_system_nothing_can_log_into, Trait.ROOT_LOCKED, Trait.NO_OTHER_LOGIN),
    (zfs_on_grub, Trait.ROOT_ON_ZFS, Trait.GRUB),
    (a_kernel_on_zfs_under_grub, Trait.BOOT_ON_ZFS, Trait.GRUB),
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
    (in_place_zfsbootmenu, Trait.IN_PLACE_CONVERSION, Trait.ZFSBOOTMENU),
    (cjk_console_with_an_8x8_font, Trait.CONSOLE_CJK, Trait.FONT_WITHOUT_CJK_GLYPHS),
    (the_patched_kernel_without_its_overlay, Trait.CJK_KERNEL, Trait.NO_GENTOOZH_OVERLAY),
    (remote_unlock_without_a_key, Trait.REMOTE_UNLOCK, Trait.NO_AUTHORIZED_KEY),
    (remote_unlock_of_an_unencrypted_root, Trait.REMOTE_UNLOCK, Trait.NO_ENCRYPTED_CONTAINER),
    (remote_unlock_with_boot_inside_the_container, Trait.REMOTE_UNLOCK, Trait.GRUB_UNLOCKS_BOOT),
    (
        native_zfs_remote_unlock_with_systemd_boot,
        Trait.REMOTE_UNLOCK,
        Trait.NATIVE_ZFS_SYSTEM_INITRAMFS,
    ),
]

KERNEL_CASES: list[tuple[KernelSource, Trait, Trait]] = [
    (KernelSource.DIST_BIN, Trait.CONSOLE_CJK, Trait.KERNEL_WITHOUT_CJKTTY),
    (KernelSource.DIST_SOURCE, Trait.CONSOLE_CJK, Trait.KERNEL_WITHOUT_CJKTTY),
    (KernelSource.CJK_BIN, Trait.CJK_KERNEL, Trait.NO_GENTOOZH_OVERLAY),
    (KernelSource.CJK, Trait.CJK_KERNEL, Trait.NO_GENTOOZH_OVERLAY),
    (KernelSource.XANMOD, Trait.CJK_KERNEL, Trait.NO_GENTOOZH_OVERLAY),
]


@pytest.mark.parametrize(("source", "when", "excludes"), KERNEL_CASES)
def test_each_kernel_package_breaks_its_declared_compatibility_rule(
    source: KernelSource, when: Trait, excludes: Trait
) -> None:
    installation = replace(
        config(),
        kernel=KernelConfig(source=source),
        system=SystemConfig(console_cjk=when is Trait.CONSOLE_CJK),
    )
    assert (when, excludes) in {
        (rule.when, rule.excludes) for rule in violations(installation)
    }


def test_kernel_cases_cover_the_package_table() -> None:
    assert {source for source, _, _ in KERNEL_CASES} == set(compat.KERNEL_PACKAGES)


@pytest.mark.parametrize(("build", "when", "excludes"), CASES)
def test_each_rule_fires_on_a_configuration_that_breaks_it(
    build: Callable[[], InstallConfig], when: Trait, excludes: Trait
) -> None:
    found = violations(build(), FACTS_FOR.get(build))
    assert (when, excludes) in {(rule.when, rule.excludes) for rule in found}


@pytest.mark.parametrize("name", ["ext2", "ext3"])
def test_each_legacy_ext_fixture_rejects_a_label_over_16_bytes(name: str) -> None:
    installation = load(Path("tests/fixtures") / f"{name}.toml")
    nodes = [
        replace(node, label="x" * 17)
        if isinstance(node, Filesystem) and node.id == i("rootfs")
        else node
        for node in installation.disk.graph.nodes.values()
    ]
    broken = replace(
        installation,
        disk=replace(installation.disk, graph=DeviceGraph.build(nodes)),
    )
    assert compat.filesystem_label_problems(broken) == (
        f"filesystem rootfs has a {name} label of 17 bytes, but {name} labels are limited to 16 bytes",
    )


def test_every_rule_in_the_table_has_a_case_that_breaks_it() -> None:
    assert {(when, excludes) for _, when, excludes in CASES} == {
        (rule.when, rule.excludes) for rule in RULES
    }


def test_a_bootloader_that_reads_the_kernel_off_the_esp_unlocks_remotely() -> None:
    """The rule above is GRUB's alone, and for two different reasons.
    systemd-boot reads the kernel off the esp, which is never in the container.
    ZFSBootMenu keeps the kernel in the pool and does ask for a key, but asks it
    from its own image, which carries `crypt-ssh` and dropbear."""
    encrypted = [node for node in ext4_on_gpt() if node.id != i("rootfs")]
    encrypted += [
        Luks(id=i("crypt"), backing=i("rootpart"), name="root"),
        Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.EXT4),
    ]
    for kind in (Bootloader.SYSTEMD_BOOT, Bootloader.GRUB):
        installation = boots(config(encrypted), kind, Firmware.UEFI)
        installation = replace(
            installation,
            system=replace(installation.system, authorized_keys=(VALID_KEY,)),
            kernel=KernelConfig(remote_unlock=RemoteUnlock(enabled=True)),
        )
        broken = {(rule.when, rule.excludes) for rule in violations(installation)}
        pair = (Trait.REMOTE_UNLOCK, Trait.GRUB_UNLOCKS_BOOT)
        assert (pair in broken) == (kind is Bootloader.GRUB), (kind, broken)


def test_a_plain_uefi_install_breaks_no_rule() -> None:
    assert violations(config()) == ()


def test_cjk_compatibility_uses_the_effective_kernel_package() -> None:
    custom = replace(
        config(),
        system=replace(config().system, console_cjk=True),
        kernel=KernelConfig(
            source=KernelSource.DIST_BIN,
            package="sys-kernel/gentoo-cjk-kernel-bin",
        ),
    )
    present = traits_of(custom)
    assert Trait.CJK_KERNEL in present
    assert Trait.KERNEL_WITHOUT_CJKTTY not in present


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
    described = next(
        rule.describe()
        for rule in violations(a_kernel_on_zfs_under_grub())
        if rule.when is Trait.BOOT_ON_ZFS and rule.excludes is Trait.GRUB
    )
    assert described == (
        "/boot on ZFS excludes GRUB: the kernel is on a pool created with today's "
        "feature flags, which GRUB cannot read; use a pool created with compatibility=grub2"
    )

    bios = next(
        rule.describe()
        for rule in violations(bios_on_gpt_without_a_bios_boot_partition())
        if rule.excludes is Trait.GPT_WITHOUT_BIOS_BOOT
    )
    assert bios.endswith("GRUB on a BIOS GPT disk needs a BIOS Boot Partition")



def test_the_interface_learns_what_a_choice_excludes_before_it_is_made() -> None:
    excluded = {rule.excludes for rule in excluded_by(frozenset({Trait.ROOT_ON_ZFS}))}
    assert excluded == {Trait.GRUB, Trait.BIOS_BOOT, Trait.LUKS}


def test_an_esp_mounted_at_boot_puts_the_kernel_on_the_esp() -> None:
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id != i("mnt-esp")]
    nodes.append(Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/boot")))
    assert Trait.KERNEL_OFF_ESP not in traits_of(config(nodes))


def test_systemd_boot_cannot_read_an_encrypted_separate_boot() -> None:
    broken = systemd_boot_with_encrypted_vfat_boot()
    assert (Trait.SYSTEMD_BOOT, Trait.KERNEL_OFF_ESP) in {
        (rule.when, rule.excludes) for rule in violations(broken)
    }


@pytest.mark.parametrize("kind", [Bootloader.GRUB, Bootloader.SYSTEMD_BOOT])
def test_system_initramfs_remote_unlock_cannot_open_native_zfs(
    kind: Bootloader,
) -> None:
    from gentoo_install.errors import ValidationFailed
    from gentoo_install.model.validate import validate

    with pytest.raises(ValidationFailed, match="native ZFS"):
        validate(remote_unlock_of_a_native_zfs_root(kind))


def test_zfsbootmenu_native_zfs_remote_unlock_is_not_refused() -> None:
    from pathlib import Path

    from gentoo_install.exec.config import load
    from gentoo_install.model.validate import validate

    installation = load(Path("tests/fixtures/zfs-zbm.toml"))
    validate(installation)


@pytest.mark.parametrize(
    ("sshd", "root_login"),
    [
        pytest.param(False, True, id="sshd-disabled"),
        pytest.param(True, False, id="root-login-disabled"),
    ],
)
def test_an_authorized_key_needs_a_daemon_and_an_account_that_can_use_it(
    sshd: bool, root_login: bool
) -> None:
    installation = replace(
        config(),
        system=replace(
            config().system,
            root_password_hash="",
            users=(),
            authorized_keys=(VALID_KEY,),
            sshd=sshd,
            sshd_root_login=root_login,
        ),
    )
    assert (Trait.ROOT_LOCKED, Trait.NO_OTHER_LOGIN) in {
        (rule.when, rule.excludes) for rule in violations(installation)
    }


@pytest.mark.parametrize(
    "password_hash",
    [
        # The shape of a hash and a digest field `crypt(3)` never produces:
        # SHA-512 is 86 characters, SHA-256 is 43 and MD5 is 22, measured with
        # `openssl passwd` on one salt. A configuration whose only login
        # carried one of these installed a machine nothing could log in to.
        "$6$salt$x",
        "$6$salt$" + "a" * 85,
        "$5$salt$" + "a" * 42,
        "$1$salt$" + "a" * 21,
    ],
)
def test_a_hash_shaped_value_that_crypt_cannot_produce_locks_the_account(
    password_hash: str,
) -> None:
    installation = replace(
        config(),
        system=replace(config().system, root_password_hash=password_hash, users=()),
    )
    assert (Trait.ROOT_LOCKED, Trait.NO_OTHER_LOGIN) in {
        (rule.when, rule.excludes) for rule in violations(installation)
    }


@pytest.mark.parametrize(
    "password_hash",
    [
        # Produced by `openssl passwd` on this machine, so the lengths are the
        # ones `crypt(3)` writes rather than the ones this test assumes.
        "$6$abcdefgh$D7W7qyozKBT.t6FD3DVYHvADbIO0eSyI4.p20LaEUjro8PqTGYYo"
        "/EQcuNjhFbzo9Yg5ir1KIEqFY/yJpgFph0",
        "$5$abcdefgh$sI6g8tHKrJ6RX7w.YoN9B7a/5wK5jYEOMmMnt3tPl0/",
        "$1$abcdefgh$znAnv9M.XU2pRYfmSs46h/",
        # yescrypt has no single digest length, so it keeps the shape check
        # alone rather than a length guessed at.
        "$y$j9T$MnwtsK.oCEIMSXUvI08eF/$Y0Xn1KAWaEQNyEIQrICLKXpsLLBEGYq4M0EF2NTXVK5",
    ],
)
def test_a_hash_crypt_really_produced_is_a_login(password_hash: str) -> None:
    installation = replace(
        config(),
        system=replace(config().system, root_password_hash=password_hash, users=()),
    )
    assert (Trait.ROOT_LOCKED, Trait.NO_OTHER_LOGIN) not in {
        (rule.when, rule.excludes) for rule in violations(installation)
    }


@pytest.mark.parametrize("password_hash", ["!", "*", "not-a-hash"])
def test_a_nonempty_root_password_value_must_be_an_authenticating_hash(
    password_hash: str,
) -> None:
    installation = replace(
        config(),
        system=replace(config().system, root_password_hash=password_hash, users=()),
    )
    assert (Trait.ROOT_LOCKED, Trait.NO_OTHER_LOGIN) in {
        (rule.when, rule.excludes) for rule in violations(installation)
    }


def test_a_nonempty_user_password_value_must_be_an_authenticating_hash() -> None:
    installation = replace(
        config(),
        system=replace(
            config().system,
            root_password_hash="",
            users=(User(name="operator", password_hash="!"),),
        ),
    )
    assert (Trait.ROOT_LOCKED, Trait.NO_OTHER_LOGIN) in {
        (rule.when, rule.excludes) for rule in violations(installation)
    }


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
    """Formatting a retained partition must block both the preflight and the
    main-menu confirmation until the operator names it."""
    from gentoo_install.exec import preflight
    from gentoo_install.tui import settings
    from tests.unit.test_tui_app import context

    installation = config(
        [
            Existing(id=i("part"), selector="/dev/sda2", wipe=False),
            Filesystem(
                id=i("fs"), device=i("part"), kind=FilesystemType.EXT4, create=True
            ),
            Mountpoint(id=i("root"), source=i("fs"), path=PurePosixPath("/")),
        ]
    )
    assert [one.selector for one in preflight._disks_at_risk(installation.disk.graph)] == [
        "/dev/sda2"
    ]
    at = context()
    assert settings._erase(installation, at) == settings.UNSET
    at.confirmed = {"/dev/sda2"}
    assert settings._erase(installation, at) == at.translate("confirmed")


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


def reused_esp() -> list[Node]:
    """An esp on an array already assembled on the machine.

    `Existing` and nothing else: runtime facts, not the graph, describe its
    superblock.
    """
    nodes: list[Node] = [
        node
        for node in ext4_on_gpt()
        if node.id not in {i("espfs"), i("mnt-esp"), i("esp")}
    ]
    nodes += [
        Existing(id=i("esp"), selector="/dev/md0"),
        Filesystem(id=i("espfs"), device=i("esp"), kind=FilesystemType.VFAT),
        Mountpoint(id=i("mnt-esp"), source=i("espfs"), path=PurePosixPath("/efi")),
    ]
    return nodes


def test_a_reused_array_under_the_esp_meets_the_firmware_rule() -> None:
    """An esp the plan creates carries an `MdRaid` node; a reused one is
    `Existing` and carries nothing, so a RAID1 esp with metadata 1.1 or 1.2
    met no rule at all although firmware cannot read a member whose superblock
    sits at the start."""
    installation = config(reused_esp())
    at_start = traits_of(
        installation,
        StorageFacts(mdraid_metadata={i("esp"): RaidMetadata.V1_2}),
    )
    at_end = traits_of(
        installation,
        StorageFacts(mdraid_metadata={i("esp"): RaidMetadata.V1_0}),
    )
    absent = traits_of(
        installation,
        StorageFacts(mdraid_metadata={i("esp"): MdraidMetadataState.ABSENT}),
    )
    not_probed = traits_of(installation, StorageFacts())
    unavailable = traits_of(
        installation,
        StorageFacts(mdraid_metadata={i("esp"): MdraidMetadataState.UNAVAILABLE}),
    )

    assert Trait.ESP_ON_MDRAID in at_start
    assert Trait.ESP_MDRAID_SUPERBLOCK_AT_START in at_start
    assert Trait.ESP_ON_MDRAID in at_end
    assert Trait.ESP_MDRAID_SUPERBLOCK_AT_START not in at_end
    assert Trait.ESP_ON_MDRAID not in absent
    # Nobody asked, so nothing is claimed; mdadm asked and could not say, which
    # is the answer a rule has to refuse.
    assert Trait.ESP_ON_MDRAID not in not_probed
    assert Trait.ESP_MDRAID_UNREADABLE not in not_probed
    assert Trait.ESP_MDRAID_UNREADABLE in unavailable


def test_a_mirror_with_no_ipv6_is_refused_on_an_ipv6_only_machine() -> None:
    """Four of the mirrors publish no AAAA record and one publishes an AAAA it
    does not answer on. An IPv6-only machine reaches none of them, and finding
    that out when the stage3 does not arrive is an hour lost."""

    from gentoo_install.data import load_catalog
    from gentoo_install.i18n import Catalog
    from gentoo_install.model import mirrors
    from gentoo_install.tui.context import Context
    from gentoo_install.tui.mirror import _unreachable_here

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


def _under(*nodes: Node) -> list[Node]:
    """One retained partition, whatever is layered on it, and a root mount."""
    return [Existing(id=i("part"), selector="/dev/sda2", wipe=False), *nodes]


#: One layout per entry in `compat._writes_over`, each destroying `/dev/sda2`
#: without setting `wipe` and without rewriting a table.
OVER_A_KEPT_PARTITION: dict[str, list[Node]] = {
    "filesystem": _under(
        Filesystem(id=i("fs"), device=i("part"), kind=FilesystemType.EXT4, create=True),
        Mountpoint(id=i("root"), source=i("fs"), path=PurePosixPath("/")),
    ),
    "luks": _under(
        Luks(id=i("crypt"), backing=i("part"), name="root", passphrase_file="/run/key"),
        Filesystem(id=i("fs"), device=i("crypt"), kind=FilesystemType.EXT4, create=True),
        Mountpoint(id=i("root"), source=i("fs"), path=PurePosixPath("/")),
    ),
    "swap": _under(Swap(id=i("swap"), device=i("part"))),
    "mdraid": _under(
        MdRaid(id=i("md"), members=(i("part"),), level=RaidLevel.RAID1, name="md0"),
        Filesystem(id=i("fs"), device=i("md"), kind=FilesystemType.EXT4, create=True),
        Mountpoint(id=i("root"), source=i("fs"), path=PurePosixPath("/")),
    ),
    "lvm": _under(
        VolumeGroup(id=i("vg"), members=(i("part"),), name="vg0"),
        LogicalVolume(id=i("lv"), group=i("vg"), name="root", size=None),
        Filesystem(id=i("fs"), device=i("lv"), kind=FilesystemType.EXT4, create=True),
        Mountpoint(id=i("root"), source=i("fs"), path=PurePosixPath("/")),
    ),
    "zfs": _under(
        ZfsPool(id=i("pool"), vdevs=(i("part"),), name="rpool"),
        ZfsDataset(id=i("ds"), pool=i("pool"), name="rpool/ROOT"),
        Mountpoint(id=i("root"), source=i("ds"), path=PurePosixPath("/")),
    ),
}


@pytest.mark.parametrize("layout", sorted(OVER_A_KEPT_PARTITION), ids=lambda one: one)
def test_every_way_of_writing_over_a_kept_device_names_it(layout: str) -> None:
    """`Existing -> Luks -> Filesystem` named nothing: the filesystem's own
    device was `Luks`, not the partition, so the erase confirmation called an
    encrypted reuse harmless and preflight never asked whether it was mounted
    while `luksFormat` overwrote it. Each entry that puts a signature on a
    device the operator kept has to name that device."""
    named = compat.destroyed(DeviceGraph.build(OVER_A_KEPT_PARTITION[layout]))
    assert [one.selector for one in named] == ["/dev/sda2"]


def test_every_write_case_leaves_the_erase_row_unanswered() -> None:
    """Each write over a retained partition needs an explicit confirmation."""
    from gentoo_install.tui import settings
    from tests.unit.test_tui_app import context

    for name, nodes in OVER_A_KEPT_PARTITION.items():
        at = context()
        installation = config(nodes)
        assert settings._erase(installation, at) == settings.UNSET, name
        at.confirmed = {"/dev/sda2"}
        assert settings._erase(installation, at) == at.translate("confirmed"), name


def test_writing_over_a_partition_this_plan_creates_does_not_condemn_the_disk() -> None:
    """The walk stops at a `Partition`: a filesystem on a partition the plan
    creates is the table's business, and climbing to the disk would tell the
    operator a whole drive is erased when one partition is being formatted."""
    kept = replace(
        config(ext4_on_gpt()),
        disk=replace(
            config(ext4_on_gpt()).disk,
            graph=DeviceGraph.build(
                [
                    replace(node, wipe=False)
                    if isinstance(node, Existing)
                    else replace(node, create=False, remove=())
                    if isinstance(node, PartitionTable)
                    else node
                    for node in ext4_on_gpt()
                ]
            ),
        ),
    )
    assert compat.destroyed(kept.disk.graph) == ()


def test_the_kernel_package_table_is_the_only_one() -> None:
    """`model/compat.py` and `plan/kernel.py` each held a complete copy, and a
    package renamed in one of them would have left `traits_of` deciding
    `CJK_KERNEL` from a name the installer no longer merges."""
    import ast
    from pathlib import Path

    from gentoo_install.model import compat
    from gentoo_install.model.config import KernelSource

    assert set(compat.KERNEL_PACKAGES) == set(KernelSource), "every choice has a package"
    xanmod = compat.KERNEL_PACKAGES[KernelSource.XANMOD]
    assert xanmod.atom == "sys-kernel/xanmod-kernel"
    assert xanmod.applies_cjktty

    # Derived rather than listed: the cjk set has to move when the table does.
    assert compat._CJK_KERNEL_PACKAGES == {
        compat.KERNEL_PACKAGES[source].atom for source in compat.CJK_KERNELS
    }

    # No second table anywhere: a dict whose keys are every KernelSource member
    # is a copy of this one. Matched on the member names rather than on the
    # enum's identifier, because `import KernelSource as _K` would otherwise
    # walk straight past — the first version of this check did.
    members = {source.name for source in KernelSource}
    root = Path(compat.__file__).resolve().parent.parent
    copies: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == Path(compat.__file__).resolve():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            named = {key.attr for key in node.keys if isinstance(key, ast.Attribute)}
            if named == members:
                copies.append(f"{path.relative_to(root)}:{node.lineno}")
    assert copies == [], copies


def test_every_way_of_unlocking_remotely_is_decided_and_none_is_decided_twice() -> None:
    """The nine combinations of root and bootloader, each with remote unlock
    asked for. Written out because the rules were added one at a time and the
    reason an operator is shown has to be the reason that applies: an
    unencrypted pool must not be refused for native encryption, and a native
    pool under ZFSBootMenu must not be refused at all.
    """
    from gentoo_install.model.device import ZfsPool

    def luks() -> list[Node]:
        nodes = [node for node in ext4_on_gpt() if node.id != i("rootfs")]
        return nodes + [
            Luks(id=i("crypt"), backing=i("rootpart"), name="root"),
            Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.EXT4),
        ]

    def pool(encrypted: bool) -> list[Node]:
        return [
            replace(
                node,
                encrypted=encrypted,
                passphrase_file="/run/keys/pool" if encrypted else "",
            )
            if isinstance(node, ZfsPool)
            else node
            for node in zfs_root()
        ]

    overlay = PortageConfig(
        overlays=(Overlay(name="gentoo-zh", sync_uri="https://example.invalid/o.git"),)
    )

    def refusals(nodes: list[Node], kind: Bootloader) -> set[Trait]:
        installation = boots(config(nodes), kind, Firmware.UEFI)
        installation = replace(
            installation,
            system=replace(installation.system, authorized_keys=(VALID_KEY,)),
            kernel=KernelConfig(remote_unlock=RemoteUnlock(enabled=True)),
            portage=overlay,
        )
        return {
            rule.excludes
            for rule in violations(installation)
            if rule.when is Trait.REMOTE_UNLOCK
        }

    expected: dict[tuple[str, Bootloader], set[Trait]] = {
        ("luks", Bootloader.GRUB): {Trait.GRUB_UNLOCKS_BOOT},
        ("luks", Bootloader.SYSTEMD_BOOT): set(),
        ("native", Bootloader.GRUB): {Trait.NATIVE_ZFS_SYSTEM_INITRAMFS},
        ("native", Bootloader.SYSTEMD_BOOT): {Trait.NATIVE_ZFS_SYSTEM_INITRAMFS},
        ("native", Bootloader.ZFSBOOTMENU): set(),
        ("plain", Bootloader.GRUB): {Trait.NO_ENCRYPTED_CONTAINER},
        ("plain", Bootloader.SYSTEMD_BOOT): {Trait.NO_ENCRYPTED_CONTAINER},
        ("plain", Bootloader.ZFSBOOTMENU): {Trait.NO_ENCRYPTED_CONTAINER},
    }
    layouts = {"luks": luks(), "native": pool(True), "plain": pool(False)}
    for (name, kind), wanted in expected.items():
        assert refusals(layouts[name], kind) == wanted, (name, kind)


def test_the_loader_and_not_a_flag_list_decides_the_v3_binhost() -> None:
    """`ld.so --help` lists the subarchitectures it would search, and
    `docs/design.md` names it as the source in two places. Deriving the same
    fact a second time from `/proc/cpuinfo` refused the v3 host on every
    machine that qualifies for it: the psABI names the requirement `FMA` and
    `CPU_FLAGS_X86` spells that instruction set `fma3`, so the requirement was
    a name no configuration can hold and the test covering it wrote `fma` too.
    """
    from gentoo_install.model.compat import binhost_subarch_problems

    base = config()
    v3 = replace(
        base,
        portage=replace(
            base.portage,
            # Deliberately empty: the flag list is no longer what decides, so
            # a machine whose loader says yes is not refused for lacking one.
            cpu_flags=(),
            binhost=replace(base.portage.binhost, official=True, subarch="x86-64-v3"),
        ),
    )
    assert binhost_subarch_problems(v3, True) == ()
    refused = binhost_subarch_problems(v3, False)
    assert refused and "ld.so --help" in refused[0], refused
    # Unread is not the same as unsupported: a dry run reads no machine, and
    # refusing there would refuse a configuration written for another one.
    assert binhost_subarch_problems(v3, None) == ()


def test_a_subarch_no_official_host_publishes_is_refused() -> None:
    """A host that does not exist answers 404 an hour into the install."""
    from gentoo_install.model.compat import binhost_subarch_problems

    base = config()
    invented = replace(
        base,
        portage=replace(
            base.portage,
            binhost=replace(base.portage.binhost, official=True, subarch="x86-64-v9"),
        ),
    )
    assert binhost_subarch_problems(invented, True), "an unknown subarch has to be refused"


def test_the_subarch_table_holds_what_the_menu_offers() -> None:
    """One table: the screen that offers a subarchitecture and the check that
    validates it read the same set, or the menu offers what validation
    refuses."""
    from gentoo_install.model.compat import BINHOST_SUBARCHS
    from gentoo_install.tui.mirror import BINHOSTS

    offered = {subarch for (_, subarch), _ in BINHOSTS}
    assert offered <= BINHOST_SUBARCHS, offered - BINHOST_SUBARCHS


#: Traits `FROM_THE_DEVICE_GRAPH` names that no fixture produces, so emptying
#: a graph cannot show them moving. Each needs a layout the fixtures do not
#: have: an encrypted esp, an esp on mdraid, and a native-encrypted pool under
#: a loader that is not ZFSBootMenu.
_UNOBSERVABLE_IN_A_FIXTURE = frozenset(
    {
        Trait.ESP_ENCRYPTED,
        Trait.ESP_ON_MDRAID,
        Trait.ESP_MDRAID_SUPERBLOCK_AT_START,
        Trait.NATIVE_ZFS_SYSTEM_INITRAMFS,
    }
)


def test_the_graph_trait_table_is_what_emptying_a_graph_changes() -> None:
    """`violations_without_a_graph` trusts this table to say which traits an
    in-place conversion cannot be judged by. A trait missing from it refuses
    every conversion; a trait wrongly in it silently drops a rule that holds
    in every mode."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.compat import FROM_THE_DEVICE_GRAPH

    moved: set[Trait] = set()
    for path in sorted((Path(__file__).resolve().parents[1] / "fixtures").glob("*.toml")):
        installation = load(path)
        emptied = replace(
            installation,
            disk=replace(installation.disk, graph=DeviceGraph.build(()), root=i("")),
        )
        moved |= traits_of(installation, None) ^ traits_of(emptied, None)
    assert moved <= FROM_THE_DEVICE_GRAPH, sorted(one.name for one in moved - FROM_THE_DEVICE_GRAPH)
    assert FROM_THE_DEVICE_GRAPH - moved == _UNOBSERVABLE_IN_A_FIXTURE, sorted(
        one.name for one in (FROM_THE_DEVICE_GRAPH - moved) ^ _UNOBSERVABLE_IN_A_FIXTURE
    )


def test_a_configuration_rule_survives_the_loss_of_the_graph() -> None:
    """The rules kept for a conversion are exactly the ones neither of whose
    traits is read from a layout."""
    from gentoo_install.model.compat import FROM_THE_DEVICE_GRAPH, violations_without_a_graph

    locked = replace(
        config(),
        system=replace(config().system, root_password_hash="", users=(), authorized_keys=()),
    )
    kept = violations_without_a_graph(locked)
    assert any(
        rule.when is Trait.ROOT_LOCKED and rule.excludes is Trait.NO_OTHER_LOGIN for rule in kept
    ), [rule.describe() for rule in kept]
    for rule in kept:
        assert not {rule.when, rule.excludes} & FROM_THE_DEVICE_GRAPH, rule.describe()


def test_a_conversion_is_not_refused_for_an_esp_it_has_not_read_yet() -> None:
    """`plan/convert.layout_graph` builds the esp from the running machine, and
    the graph the menu holds is empty until then. Asked for one anyway, every
    UEFI machine was refused with `uefi boot` against `no mounted esp` and no
    row could answer it."""
    from dataclasses import replace

    from gentoo_install.model import compat
    from gentoo_install.model.config import DiskMode
    from gentoo_install.model.device import DeviceGraph, DeviceId

    built = config()
    converted = replace(
        built,
        disk=replace(
            built.disk, mode=DiskMode.IN_PLACE, graph=DeviceGraph.build([]), root=DeviceId("")
        ),
    )
    assert compat.Trait.NO_MOUNTED_ESP not in compat.traits_of(converted)
    assert not compat.violations(converted)

    # Negative control: the same empty graph on a partition install is still
    # refused, so the rule above is not "never ask for an esp".
    partitioned = replace(built, disk=replace(built.disk, graph=DeviceGraph.build([])))
    assert compat.Trait.NO_MOUNTED_ESP in compat.traits_of(partitioned)


def test_a_mirror_site_the_region_does_not_offer_is_refused() -> None:
    """`mirrors.gentoo_binhost()` resolves a site key with
    `next((one for one in sites if one.key == preferred), sites[0])`, so a key
    the region lacks is replaced by the region's first site and nothing says
    so. `vm-binhost-fallback` names `xtom-hk` because its binary package index
    answers 404 and the run is green only if Portage drops that host; rewritten
    to `global`, where there is no `xtom-hk`, it resolved to
    `distfiles.gentoo.org` and installed from a working binary host."""
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from gentoo_install.model import compat, mirrors
    from gentoo_install.model.config import MirrorRegion

    config = load(Path("tests/fixtures/vm-binhost-fallback.toml"))
    assert config.portage.mirrors.site == "xtom-hk", config.portage.mirrors
    assert not compat.mirror_site_problems(config)

    moved = replace(
        config,
        portage=replace(
            config.portage,
            mirrors=replace(config.portage.mirrors, region=MirrorRegion.GLOBAL),
        ),
    )
    problems = compat.mirror_site_problems(moved)
    assert problems and "xtom-hk" in problems[0], problems

    # The silent substitution the rule exists to catch, so the case is not
    # arguing against a mechanism that was never there.
    assert mirrors.gentoo_binhost(MirrorRegion.GLOBAL, "xtom-hk") != mirrors.gentoo_binhost(
        MirrorRegion.CN, "xtom-hk"
    )
    assert "xtom" not in mirrors.gentoo_binhost(MirrorRegion.GLOBAL, "xtom-hk")


def test_a_fixture_that_keeps_its_site_keeps_its_region(tmp_path: Path) -> None:
    """The two are one choice: a key is looked up inside its region, so moving
    the region alone is the same silent substitution by another route.

    Read off the file the harness writes rather than off its source, because
    what the guest installs from is the file.
    """
    from gentoo_install.exec.config import load
    from gentoo_install.model import compat
    from gentoo_install.model.config import MirrorRegion, Sync
    from tests.vm import cluster

    kept, moved = cluster.fixtures(["vm-binhost-fallback", "vm-xfs"])
    written = cluster.rewrite_fixtures(
        [kept, moved], tmp_path, MirrorRegion.GLOBAL, Sync.WEBRSYNC, site="osuosl"
    )

    pinned = load(written / kept.fixture.name).portage.mirrors
    assert (pinned.region, pinned.site) == (MirrorRegion.CN, "xtom-hk"), pinned
    assert not compat.mirror_site_problems(load(written / kept.fixture.name))

    ordinary = load(written / moved.fixture.name).portage.mirrors
    assert (ordinary.region, ordinary.site) == (MirrorRegion.GLOBAL, "osuosl"), ordinary


def test_seeding_a_language_drops_a_mirror_site_its_region_lacks() -> None:
    """The Mirrors screen already clears the site when the region changes;
    the language seeding replaced the region and left the site behind. An
    operator who picks `tuna` and then chooses an English profile would fetch
    everything from `distfiles.gentoo.org` with nothing said."""
    from dataclasses import replace

    from gentoo_install.model import compat
    from gentoo_install.model.config import MirrorRegion
    from gentoo_install.tui import screens
    from tests.unit.layouts import config as a_config

    chinese = replace(
        a_config(),
        portage=replace(
            a_config().portage,
            mirrors=replace(
                a_config().portage.mirrors, region=MirrorRegion.CN, site="tuna"
            ),
        ),
    )
    assert not compat.mirror_site_problems(chinese)

    # The seeding that moves the region. Whichever language leaves the region
    # where it was keeps the site, which is the other half of the rule.
    for tag in sorted(screens.LANGUAGE_DEFAULTS):
        seeded = screens.with_language(chinese, tag)
        assert not compat.mirror_site_problems(seeded), (tag, seeded.portage.mirrors)
        kept = seeded.portage.mirrors.region is MirrorRegion.CN
        assert bool(seeded.portage.mirrors.site) == kept, (tag, seeded.portage.mirrors)


def test_a_cjktty_kernel_is_refused_where_gentoo_zh_builds_none() -> None:
    """The three cjktty packages are `~amd64` in gentoo-zh.

    An aarch64 machine could select one from the menu and reach `emerge` with
    the disk already partitioned, asking that overlay for an atom it carries
    for another architecture. Keyed on `applies_cjktty`, the trait the table
    already holds, so a fourth cjktty package is covered the day it is added.
    """
    from dataclasses import replace

    from gentoo_install.model import compat
    from gentoo_install.model.architecture import AMD64, architecture_of
    from gentoo_install.model.config import KernelSource

    arm = architecture_of("aarch64")
    assert arm is not None

    carries = [
        source
        for source, package in compat.KERNEL_PACKAGES.items()
        if package.applies_cjktty
    ]
    assert carries, compat.KERNEL_PACKAGES

    base = config()
    for source in carries:
        chosen = replace(base, kernel=replace(base.kernel, source=source))
        refused = compat.cjk_kernel_problems(chosen, arm)
        assert refused, (source, refused)
        assert compat.KERNEL_PACKAGES[source].atom in refused[0], refused
        # And the same choice on amd64 is not refused.
        assert compat.cjk_kernel_problems(chosen, AMD64) == (), source

    # Every kernel the table offers is covered: the ones without the patch are
    # accepted on both rows rather than left undecided.
    for source in KernelSource:
        chosen = replace(base, kernel=replace(base.kernel, source=source))
        for row in (AMD64, arm):
            answered = compat.cjk_kernel_problems(chosen, row)
            expected = bool(
                compat.KERNEL_PACKAGES[source].applies_cjktty and row is arm
            )
            assert bool(answered) == expected, (source, row.kernel_name, answered)


def test_validate_reads_the_cjktty_rule() -> None:
    """A rule nothing calls refuses nothing, so `validate` names this one.

    The overlay is enabled here: without it a different rule speaks first,
    and a test that accepted any refusal would pass while this one was never
    reached.
    """
    from dataclasses import replace

    import pytest as _pytest

    from gentoo_install.errors import ValidationFailed
    from gentoo_install.model import compat, validate
    from gentoo_install.model.architecture import architecture_of
    from gentoo_install.model.config import KernelSource, Overlay

    base = config()
    cjk = replace(
        base,
        kernel=replace(base.kernel, source=KernelSource.CJK_BIN),
        portage=replace(
            base.portage,
            overlays=(
                Overlay(name="gentoo-zh", sync_uri="https://example.invalid/overlay.git"),
            ),
        ),
    )
    arm = architecture_of("aarch64")
    assert arm is not None

    # On the row the overlay builds for, this configuration is installable.
    validate.validate(cjk)

    with _pytest.MonkeyPatch.context() as patched:
        patched.setattr(compat, "DEFAULT_ARCHITECTURE", arm)
        with _pytest.raises(ValidationFailed) as refused:
            validate.validate(cjk)
    assert "cjktty" in str(refused.value), str(refused.value)


def test_every_kernel_package_trait_has_a_reader() -> None:
    """A column nothing reads is state implying a behaviour nothing implements.

    `cjk_use_defaults_on` recorded whether an ebuild turns its CJK USE flags
    on by itself. `RequestCjkKernel` writes `cjk` or `-cjk` explicitly for
    every patched kernel, so the ebuild's own default decided nothing, and
    only two tests naming the field kept it alive. The five rows carried a
    trait whose value could not change an install.
    """
    import ast
    from dataclasses import fields
    from pathlib import Path

    # The whole package, `compat.py` included: `applies_cjktty` is read by
    # `cjk_kernels()` beside the table, and `CJK_KERNELS` is what `plan/`
    # takes. A reader in the module that owns the rule is still a reader.
    root = Path(__file__).resolve().parents[2] / "gentoo_install"
    read: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                read.add(node.attr)

    traits = [one.name for one in fields(compat.KernelPackage)]
    assert traits, "the table has no column at all"
    assert [one for one in traits if one not in read] == [], sorted(
        one for one in traits if one not in read
    )
