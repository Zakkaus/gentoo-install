from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.errors import NothingToBoot

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
from gentoo_install.model.parse import load
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


def test_a_configuration_can_name_a_kernel_package_this_installer_does_not() -> None:
    """gentoo-zh and other overlays ship sources packages of their own, and the
    build steps are the same whichever one it is."""
    named = replace(
        config(),
        kernel=KernelConfig(source=KernelSource.CJK_SOURCE, package="sys-kernel/gentoo-sources"),
    )
    described = " ".join(operation.describe() for operation in kernel.build(named))
    assert "sys-kernel/gentoo-sources" in described
    assert "gentoo-cjk-sources" not in described


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


def test_zfs_is_told_to_build_against_a_dist_kernel() -> None:
    """A dist-kernel leaves no `.config` in /usr/src/linux, and `sys-fs/zfs`
    dies in its setup phase looking for one."""
    described = " ".join(operation.describe() for operation in kernel.build(config(zfs_root())))
    assert "sys-fs/zfs-kmod" in described and "dist-kernel" in described


def test_a_kernel_built_from_source_needs_no_such_flag() -> None:
    """It configured and built the tree itself, so the `.config` is there."""
    patched = replace(config(zfs_root()), kernel=KernelConfig(source=KernelSource.CJK_SOURCE))
    assert not any("dist-kernel" in o.describe() for o in kernel.build(patched))


def test_a_kernel_hook_is_installed_because_a_sources_package_pulls_none_in() -> None:
    """Without it `make install` falls back to the kernel's own script, which
    looks for LILO and leaves /boot with no kernel."""
    described = " ".join(operation.describe() for operation in kernel.build(config()))
    assert "sys-kernel/installkernel" in described


def test_a_bootloader_with_no_menu_entry_is_a_failure_rather_than_a_success() -> None:
    """grub-mkconfig exits 0 having found nothing, and the machine drops back
    to the firmware menu."""
    recorder = Recorder()
    recorder.replies["grep"] = "0\n"
    operation = next(
        operation for operation in bootloader.build(config()) if isinstance(operation, bootloader.InstallGrub)
    )
    with pytest.raises(NothingToBoot):
        operation.apply(recorder)


def test_grub_is_told_the_disk_is_encrypted() -> None:
    """`grub-install` refuses outright when /boot is inside a LUKS container
    and the configuration does not say so."""
    from gentoo_install.model.device import Luks

    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id != i("rootfs")]
    nodes += [
        Luks(id=i("crypt"), backing=i("rootpart"), name="root"),
        Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.EXT4),
    ]
    written = apply_boot(config(nodes)).files[PurePosixPath("/etc/default/grub")]
    assert "GRUB_ENABLE_CRYPTODISK=y" in written
    assert "GRUB_ENABLE_CRYPTODISK" not in apply_boot(config()).files[
        PurePosixPath("/etc/default/grub")
    ]


def test_grub_talks_on_the_serial_line_when_the_cmdline_does() -> None:
    """A machine installed for remote use whose bootloader only draws on VGA
    cannot be recovered over the line it was installed through."""
    from gentoo_install.model.config import BootloaderConfig as Boot

    remote = replace(config(), bootloader=Boot(kernel_params=("console=ttyS0,115200",)))
    written = apply_boot(remote).files[PurePosixPath("/etc/default/grub")]
    assert 'GRUB_TERMINAL_OUTPUT="console serial"' in written
    assert "--unit=0 --speed=115200" in written


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


def zfs_installation() -> InstallConfig:
    from gentoo_install.model.config import Overlay, PortageConfig

    return replace(
        config(zfs_root()),
        bootloader=BootloaderConfig(kind=Bootloader.ZFSBOOTMENU, firmware=Firmware.UEFI),
        portage=PortageConfig(
            overlays=(Overlay(name="gentoo-zh", sync_uri="https://example.invalid/overlay.git"),)
        ),
    )


def test_the_zbm_image_is_the_one_generate_zbm_wrote() -> None:
    """It names the image after the kernel it built from, so the name is looked
    up: assuming `vmlinuz.EFI` left the fallback path empty and the machine
    unbootable."""
    recorder = Recorder()
    recorder.replies["find"] = "/efi/EFI/zbm/kernel.EFI\n"
    for operation in bootloader.build(zfs_installation()):
        operation.apply(recorder)
    copied = recorder.only("install", "-D", "-m0644")
    assert copied[3] == "/efi/EFI/zbm/kernel.EFI"
    entry = recorder.only("efibootmgr", "--create")
    assert "\\EFI\\zbm\\kernel.EFI" in entry


def test_generate_zbm_writing_nothing_is_a_failure() -> None:
    recorder = Recorder()
    recorder.replies["find"] = "\n"
    operation = next(
        o for o in bootloader.build(zfs_installation()) if isinstance(o, bootloader.InstallZfsBootMenu)
    )
    with pytest.raises(NothingToBoot):
        operation.apply(recorder)


def test_zfsbootmenu_sets_bootfs_and_writes_both_efi_paths() -> None:
    zfs = zfs_installation()
    recorder = Recorder()
    recorder.replies["find"] = "/efi/EFI/zbm/kernel.EFI\n"
    for operation in bootloader.build(zfs):
        operation.apply(recorder)
    assert recorder.only("zpool", "set")[2].startswith("bootfs=zpcala/")
    assert recorder.argv_starting("zgenhostid")
    installed = recorder.only("install", "-D", "-m0644")
    assert installed[-1].endswith("EFI/BOOT/BOOTX64.EFI")
    assert "config.yaml" in str(list(recorder.files)[0]) or any(
        "zfsbootmenu" in str(path) for path in recorder.files
    )


def test_the_pool_keeps_its_hostid_or_it_will_not_import() -> None:
    recorder = Recorder()
    recorder.replies["find"] = "/efi/EFI/zbm/kernel.EFI\n"
    for operation in bootloader.build(zfs_installation()):
        operation.apply(recorder)
    assert recorder.argv_starting("zgenhostid")


def test_systemd_boot_gets_boot_entries_and_a_command_line() -> None:
    """`layout=bls` comes from the USE flag, and without /etc/kernel/cmdline
    `90-loaderentry.install` copies the install medium's own command line."""
    sdboot = load(Path("tests/fixtures/vm-sdboot.toml"))
    described = [operation.describe() for operation in kernel.build(sdboot)]
    assert "set sys-kernel/installkernel to dracut systemd systemd-boot" in described
    assert any("/etc/kernel/cmdline" in line for line in described)

    grub = [operation.describe() for operation in kernel.build(config())]
    assert "set sys-kernel/installkernel to dracut" in grub
    assert not any("/etc/kernel/cmdline" in line for line in grub)


def test_the_boot_entry_names_the_root_the_layout_actually_has() -> None:
    written: dict[str, str] = {}
    for fixture, expected in (
        ("tests/fixtures/vm-sdboot.toml", "root=UUID="),
        ("tests/fixtures/btrfs-luks.toml", "rootflags=subvol="),
        ("tests/fixtures/zfs-zbm.toml", "root=ZFS="),
    ):
        installation = load(Path(fixture))
        root, dataset, extra = kernel._root_parameters(installation)
        recorder = Recorder()
        kernel.WriteKernelCmdline(root=root, dataset=dataset, kernel_params=extra).apply(
            recorder
        )
        written[fixture] = recorder.files[PurePosixPath("/etc/kernel/cmdline")]
        assert expected in written[fixture]


def test_the_command_line_names_the_container_the_initramfs_opens() -> None:
    """Gentoo's dracut sets `hostonly_cmdline="no"`, and its detection runs in
    the chroot where the root is the installer's own, so nothing else says it."""
    encrypted = load(Path("tests/fixtures/vm-luks.toml"))
    recorder = Recorder()
    for operation in bootloader.build(encrypted):
        if isinstance(operation, bootloader.WriteGrubDefaults):
            operation.apply(recorder)
    written = recorder.files[PurePosixPath("/etc/default/grub")]
    assert "rd.luks.uuid=" in written

    plain = Recorder()
    for operation in bootloader.build(load(Path("tests/fixtures/ext4-bios.toml"))):
        if isinstance(operation, bootloader.WriteGrubDefaults):
            operation.apply(plain)
    assert "rd.luks.uuid" not in plain.files[PurePosixPath("/etc/default/grub")]


def test_zfsbootmenu_talks_where_the_install_did() -> None:
    """`org.zfsbootmenu:commandline` is the command line of the system ZBM
    boots, not of ZBM, so an encrypted pool prompts on a console nobody reads."""
    operations = bootloader.build(load(Path("tests/fixtures/vm-zfs-encrypted.toml")))
    installed = next(
        operation
        for operation in operations
        if isinstance(operation, bootloader.InstallZfsBootMenu)
    )
    written = installed._config()
    assert "console=ttyS0,115200" in written and "console=tty1" in written


def test_a_systemd_initramfs_gets_the_generator_that_unlocks_the_root() -> None:
    """`cryptsetup` is in systemd's IUSE without a `+`, so a stage3 systemd has
    no generator and the initramfs waits for a device it never opens."""
    described = [
        operation.describe() for operation in kernel.build(load(Path("tests/fixtures/vm-luks.toml")))
    ]
    asked = described.index("ask for sys-apps/systemd[cryptsetup], which provides the unlock generator")
    rebuilt = described.index("rebuild systemd with the unlock generator: emerge sys-apps/systemd, from source")
    built = described.index(
        "rebuild the initramfs from sys-kernel/gentoo-kernel-bin with the modules written above"
    )
    assert asked < rebuilt < built

    plain = [operation.describe() for operation in kernel.build(load(Path("tests/fixtures/vm-sdboot.toml")))]
    assert not any("unlock generator" in line for line in plain)


def test_a_stack_tool_gets_the_use_flag_dracut_needs() -> None:
    """`lvm` has no `+` in sys-fs/lvm2's IUSE, so the default build has
    device-mapper and no LVM tools, and dracut cannot build its lvm module."""
    described = [
        operation.describe() for operation in kernel.build(load(Path("tests/fixtures/vm-lvm.toml")))
    ]
    asked = next(index for index, line in enumerate(described) if "sys-fs/lvm2[lvm]" in line)
    built = next(index for index, line in enumerate(described) if "from source" in line)
    installed = next(index for index, line in enumerate(described) if "install the kernel" in line)
    assert asked < built < installed
    # The binary host builds the default USE, so this one cannot come from it.
    assert "sys-fs/lvm2" not in next(
        line for line in described if line.startswith("install the storage tools:")
    )

    plain = [operation.describe() for operation in kernel.build(config())]
    assert not any("package.use/storage" in line or "[lvm]" in line for line in plain)


def test_the_command_line_names_the_array_the_initramfs_assembles() -> None:
    """/etc/mdadm.conf alone is not enough: without `rd.md.uuid` dracut brings
    up no array and boot stops in the emergency shell."""
    recorder = Recorder()
    for operation in bootloader.build(load(Path("tests/fixtures/vm-mdraid.toml"))):
        if isinstance(operation, bootloader.WriteGrubDefaults):
            operation.apply(recorder)
    assert "rd.md.uuid=1111:2222:3333:4444" in recorder.files[PurePosixPath("/etc/default/grub")]

    plain = Recorder()
    for operation in bootloader.build(load(Path("tests/fixtures/ext4-bios.toml"))):
        if isinstance(operation, bootloader.WriteGrubDefaults):
            operation.apply(plain)
    assert "rd.md.uuid" not in plain.files[PurePosixPath("/etc/default/grub")]
