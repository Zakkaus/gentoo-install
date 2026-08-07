from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath

from gentoo_install.model.config import (
    Bootloader,
    BootloaderConfig,
    ConsoleFontSize,
    Firmware,
    InstallConfig,
    KernelConfig,
    KernelSource,
    SystemConfig,
)
from gentoo_install.model.device import Filesystem, FilesystemType, Luks, MdRaid, Node, RaidLevel
from gentoo_install.plan import bootloader, kernel

from .layouts import config, ext4_on_gpt, i, zfs_root
from .recorder import Recorder


def apply_boot(installation: InstallConfig) -> Recorder:
    recorder = Recorder()
    for operation in bootloader.build(installation):
        operation.apply(recorder)
    return recorder


def apply_kernel(installation: InstallConfig) -> Recorder:
    recorder = Recorder()
    for operation in kernel.build(installation):
        operation.apply(recorder)
    return recorder


def test_dracut_carries_a_module_for_each_layer_of_the_stack() -> None:
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id != i("rootfs")]
    nodes += [
        MdRaid(id=i("array"), members=(i("rootpart"),), level=RaidLevel.RAID1, name="root"),
        Luks(id=i("crypt"), backing=i("array"), name="root"),
        Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.BTRFS),
    ]
    assert kernel.dracut_modules(config(nodes)) == ("mdraid", "crypt", "btrfs")


def test_a_plain_layout_asks_dracut_for_nothing_extra() -> None:
    assert kernel.dracut_modules(config()) == ()
    assert not apply_kernel(config()).files.get(
        PurePosixPath("/etc/dracut.conf.d/10-gentoo-install.conf")
    )


def test_the_storage_tools_follow_the_filesystems_in_use() -> None:
    assert set(kernel.storage_packages(config())) == {"sys-fs/dosfstools", "sys-fs/e2fsprogs"}
    assert "sys-fs/zfs" in kernel.storage_packages(config(zfs_root()))


def test_installkernel_is_told_about_dracut_before_the_kernel_is_merged() -> None:
    operations = kernel.build(config())
    told = next(n for n, o in enumerate(operations) if "installkernel" in o.describe())
    merged = next(n for n, o in enumerate(operations) if "install the kernel" in o.describe())
    assert told < merged


def test_the_firmware_licence_is_accepted_before_firmware_is_merged() -> None:
    recorder = apply_kernel(config())
    accepted = recorder.files[PurePosixPath("/etc/portage/package.license/linux-firmware")]
    assert "linux-fw-redistributable no-source-code" in accepted


def test_a_source_kernel_is_configured_and_built_rather_than_only_unpacked() -> None:
    """A sources package leaves a tree in /usr/src and installs no kernel, so
    without these the install ends with a bootloader pointing at nothing."""
    patched = replace(
        config(),
        kernel=KernelConfig(source=KernelSource.CJK_SOURCE),
        system=SystemConfig(console_cjk=True, console_font=ConsoleFontSize.SIZE_16X32),
    )
    recorder = apply_kernel(patched)
    assert ("eselect", "kernel", "set", "1") in recorder.in_target
    assert recorder.argv_starting("make", "--directory", "/usr/src/linux", "defconfig")
    assert recorder.argv_starting("make", "--directory", "/usr/src/linux", "install")
    toggles = [argv for argv in recorder.in_target if argv[0].endswith("scripts/config")]
    assert any("FONT_CJK_16x16" in argv and "--enable" in argv for argv in toggles)
    assert any("FONT_CJK_32x32" in argv and "--disable" in argv for argv in toggles)


def test_a_kernel_built_from_source_asks_for_no_dist_kernel_initramfs() -> None:
    patched = replace(config(), kernel=KernelConfig(source=KernelSource.CJK_SOURCE))
    assert not any("rebuild the initramfs" in o.describe() for o in kernel.build(patched))
    binary = replace(config(), kernel=KernelConfig(source=KernelSource.DIST_BIN))
    assert any("rebuild the initramfs" in o.describe() for o in kernel.build(binary))


def test_bootctl_comes_from_the_package_the_init_system_allows() -> None:
    """systemd provides bootctl on a systemd system and systemd-utils on an
    openrc one; merging the wrong one hits a blocker."""
    from gentoo_install.model.config import InitSystem, SystemConfig

    for init, package in (
        (InitSystem.SYSTEMD, "sys-apps/systemd"),
        (InitSystem.OPENRC, "sys-apps/systemd-utils"),
    ):
        installation = replace(
            config(),
            system=SystemConfig(init=init),
            bootloader=BootloaderConfig(kind=Bootloader.SYSTEMD_BOOT, firmware=Firmware.UEFI),
        )
        described = " ".join(operation.describe() for operation in bootloader.build(installation))
        assert package in described


def test_grub_is_installed_to_the_removable_path_as_well() -> None:
    """Firmware with no NVRAM entry, and firmware that lost it, boots only the
    removable path. The installed system has to come up either way."""
    installed = apply_boot(config()).argv_starting("grub-install")
    assert any("--bootloader-id=Gentoo" in argv for argv in installed)
    assert any("--removable" in argv for argv in installed)


def test_a_bios_install_writes_no_efi_artefact() -> None:
    on_bios = replace(
        config(), bootloader=BootloaderConfig(kind=Bootloader.GRUB, firmware=Firmware.BIOS)
    )
    argv = apply_boot(on_bios).only("grub-install", "--target=i386-pc")
    assert argv[-1] == "/dev/vda"


def test_zfsbootmenu_sets_bootfs_and_writes_both_efi_paths() -> None:
    zfs = replace(
        config(zfs_root()),
        bootloader=BootloaderConfig(kind=Bootloader.ZFSBOOTMENU, firmware=Firmware.UEFI),
    )
    recorder = apply_boot(zfs)
    assert recorder.only("zpool", "set")[2].startswith("bootfs=zpcala/")
    assert recorder.argv_starting("zgenhostid")
    installed = recorder.only("install", "-D", "-m0644")
    assert installed[-1].endswith("EFI/BOOT/BOOTX64.EFI")
    assert "config.yaml" in str(list(recorder.files)[0]) or any(
        "zfsbootmenu" in str(path) for path in recorder.files
    )


def test_the_pool_keeps_its_hostid_or_it_will_not_import() -> None:
    zfs = replace(
        config(zfs_root()),
        bootloader=BootloaderConfig(kind=Bootloader.ZFSBOOTMENU, firmware=Firmware.UEFI),
    )
    assert apply_boot(zfs).argv_starting("zgenhostid")
