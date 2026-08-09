from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Sequence

import pytest

from gentoo_install.errors import NothingToBoot, ValidationFailed

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
from gentoo_install.model.device import (
    DeviceGraph,
    Filesystem,
    FilesystemType,
    Luks,
    MdRaid,
    Mountpoint,
    Node,
    Partition,
    PartitionRole,
    RaidLevel,
    ZfsPool,
)
from gentoo_install.model.size import Size
from gentoo_install.model.parse import load
from gentoo_install.model.validate import validate
from gentoo_install.plan import bootloader, kernel

from .layouts import config, encrypted_root, ext4_on_gpt, i, zfs_root
from .recorder import Recorder


def apply_boot(installation: InstallConfig) -> Recorder:
    recorder = Recorder()
    for operation in bootloader.build(installation):
        operation.apply(recorder)
    return recorder


def apply_kernel(installation: InstallConfig) -> Recorder:
    # The `find` reply stands for a machine where the kernel did land.
    # `test_a_kernel_that_never_reached_boot_stops_the_install` takes it away.
    recorder = Recorder(replies={"find": "/boot/kernel-6.18.41-gentoo-dist-bin\n"})
    for operation in kernel.build(installation):
        operation.apply(recorder)
    return recorder


def test_a_kernel_that_never_reached_boot_stops_the_install() -> None:
    """`kernel-install` reports success when a plugin exits 77, so an initramfs
    dracut refused to build leaves /boot empty and every later step blind."""
    recorder = Recorder(replies={"find": ""})
    with pytest.raises(NothingToBoot):
        for operation in kernel.build(config()):
            operation.apply(recorder)


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


def test_the_patched_kernel_is_a_dist_kernel_and_builds_itself() -> None:
    """`sys-kernel/gentoo-cjk-kernel` inherits kernel-build and PDEPENDs on
    virtual/dist-kernel, so nothing here configures or compiles a tree."""
    patched = replace(
        config(),
        kernel=KernelConfig(source=KernelSource.CJK),
        system=SystemConfig(console_cjk=True, console_font=ConsoleFontSize.SIZE_16X32),
    )
    recorder = apply_kernel(patched)
    assert not recorder.argv_starting("eselect")
    assert not recorder.argv_starting("make")
    described = " ".join(operation.describe() for operation in kernel.build(patched))
    assert "sys-kernel/gentoo-cjk-kernel" in described
    assert any("rebuild the initramfs" in o.describe() for o in kernel.build(patched))


def test_the_patched_kernel_is_keyworded_and_its_cjk_flag_is_left_alone() -> None:
    """It is ~amd64 in gentoo-zh, and its cjk flag is on by default: writing
    the flag when it is wanted would be a line that changes nothing."""
    wanted = replace(
        config(),
        kernel=KernelConfig(source=KernelSource.CJK),
        system=SystemConfig(console_cjk=True, console_font=ConsoleFontSize.SIZE_16X32),
    )
    recorder = apply_kernel(wanted)
    keywords = recorder.files[
        PurePosixPath("/etc/portage/package.accept_keywords/cjk-kernel")
    ]
    assert keywords == "sys-kernel/gentoo-cjk-kernel ~amd64\n"
    assert PurePosixPath("/etc/portage/package.use/cjk-kernel") not in recorder.files
    off = apply_kernel(replace(config(), kernel=KernelConfig(source=KernelSource.CJK)))
    assert (
        off.files[PurePosixPath("/etc/portage/package.use/cjk-kernel")]
        == "sys-kernel/gentoo-cjk-kernel -cjk\n"
    )


def test_a_sources_package_is_refused_rather_than_left_unbuilt() -> None:
    """It would unpack a tree nothing compiles, and the failure would be a
    bootloader pointing at an empty /boot an hour after the disks were written."""
    named = replace(
        config(), kernel=KernelConfig(source=KernelSource.CJK, package="sys-kernel/gentoo-sources")
    )
    with pytest.raises(ValidationFailed, match="source tree"):
        validate(named)


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
    assert "sys-fs/zfs" in described and "dist-kernel" in described
    # 2.4.1 absorbed the module and blocks every older kmod.
    assert "sys-fs/zfs-kmod" not in described


def test_the_patched_kernel_needs_the_flag_too() -> None:
    """It is a dist-kernel like the other two, so `sys-fs/zfs` has the same
    `Kernel not configured` failure without it."""
    patched = replace(config(zfs_root()), kernel=KernelConfig(source=KernelSource.CJK))
    assert any("dist-kernel" in o.describe() for o in kernel.build(patched))


def test_the_kernel_hook_is_installed_before_any_kernel_is() -> None:
    """installkernel is what puts an image and an initramfs in /boot; without
    it grub-mkconfig still exits 0 over an empty directory."""
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
    built = next(index for index, line in enumerate(described) if "need a flag" in line)
    installed = next(index for index, line in enumerate(described) if "install the kernel" in line)
    # The request, then the tool, then the kernel: lvm2 ships `dmsetup`, and
    # the kernel's postinst runs dracut, which dies on a module whose tool is
    # not there yet. lvm2 builds no kernel module, so nothing wants it later.
    assert asked < built < installed
    assert "from source" in described[built]
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


def test_both_command_line_writers_are_told_the_same_devices() -> None:
    """Deriving it twice is how systemd-boot came to omit the arrays GRUB was
    given, so one function answers for both."""
    from dataclasses import replace as _replace

    from gentoo_install.model.config import Bootloader

    raid = load(Path("tests/fixtures/vm-mdraid.toml"))
    grub = next(
        operation
        for operation in bootloader.build(raid)
        if isinstance(operation, bootloader.WriteGrubDefaults)
    )
    entries = _replace(raid, bootloader=_replace(raid.bootloader, kind=Bootloader.SYSTEMD_BOOT))
    bls = next(
        operation
        for operation in kernel.build(entries)
        if isinstance(operation, kernel.WriteKernelCmdline)
    )
    assert grub.arrays == bls.arrays != ()
    assert grub.luks == bls.luks


def test_every_kernel_choice_asks_zfs_to_build_against_a_dist_kernel() -> None:
    """`sys-fs/zfs` reads /usr/src/linux/.config and dies with `Kernel not
    configured`; a dist-kernel leaves no such file, so the flag is what it
    reads instead. Every choice is a dist-kernel, so every choice needs it."""
    from dataclasses import replace as _replace

    from gentoo_install.model.config import KernelSource

    zfs = load(Path("tests/fixtures/vm-zfs.toml"))
    for source in KernelSource:
        chosen = _replace(zfs, kernel=_replace(zfs.kernel, source=source, package=""))
        described = [operation.describe() for operation in kernel.build(chosen)]
        asked = next(
            index for index, line in enumerate(described) if "dist-kernel" in line
        )
        merged = next(
            index
            for index, line in enumerate(described)
            if line.startswith("install ") and "sys-fs/zfs" in line
        )
        assert asked < merged, source



def test_the_unlock_prompt_uses_the_keyboard_that_is_attached() -> None:
    """An encrypted root asks for its passphrase before the console keymap is
    loaded, so a keyboard that is not us cannot type one."""
    from dataclasses import replace as _replace

    encrypted = load(Path("tests/fixtures/vm-luks.toml"))
    french = _replace(encrypted, system=_replace(encrypted.system, keymap="fr"))
    recorder = Recorder()
    for operation in bootloader.build(french):
        if isinstance(operation, bootloader.WriteGrubDefaults):
            operation.apply(recorder)
    assert "rd.vconsole.keymap=fr" in recorder.files[PurePosixPath("/etc/default/grub")]

    # Nothing is said when the keyboard is the default, or when nothing asks.
    plain = Recorder()
    for operation in bootloader.build(encrypted):
        if isinstance(operation, bootloader.WriteGrubDefaults):
            operation.apply(plain)
    assert "rd.vconsole.keymap" not in plain.files[PurePosixPath("/etc/default/grub")]


def test_remote_unlock_puts_an_address_on_the_command_line() -> None:
    """dracut's network module does nothing without both `rd.neednet=1` and an
    `ip=`, so the initramfs would come up with no address to ssh into.

    The three fields are built into dracut's seven, because an initramfs with
    an address and no gateway answers only its own subnet and one with no
    interface named takes whichever came up first.
    """
    from gentoo_install.model.config import RemoteUnlock

    def parameters(**fields: object) -> tuple[str, ...]:
        unlock = RemoteUnlock(enabled=True, **fields)  # type: ignore[arg-type]
        return bootloader.unlock_parameters(
            replace(config(), kernel=KernelConfig(remote_unlock=unlock))
        )

    assert parameters() == ("rd.neednet=1", "ip=dhcp")
    assert parameters(interface="enp1s0") == ("rd.neednet=1", "ip=enp1s0:dhcp")
    assert parameters(address="192.0.2.10/24", gateway="192.0.2.1", interface="enp1s0") == (
        "rd.neednet=1",
        "ip=192.0.2.10::192.0.2.1:255.255.255.0::enp1s0:none",
    )
    # IPv6 goes in brackets, because the fields and the address share a
    # separator, and takes the prefix itself where IPv4 takes a netmask.
    assert parameters(address="2001:db8::10/64", gateway="2001:db8::1") == (
        "rd.neednet=1",
        "ip=[2001:db8::10]::[2001:db8::1]:64:::none",
    )
    # Unparsable: the field is left empty rather than guessed, because a wrong
    # netmask puts the machine on the wrong subnet without saying so.
    assert parameters(address="192.0.2.10/nonsense") == (
        "rd.neednet=1",
        "ip=192.0.2.10::::::none",
    )

def test_remote_unlock_adds_the_modules_that_answer_on_the_network() -> None:
    """`crypt-ssh` is what dracut-crypt-ssh installs as 60crypt-ssh, and it
    needs `network` beside it or the initramfs has no link."""
    from gentoo_install.model.config import RemoteUnlock

    wanted = replace(config(), kernel=KernelConfig(remote_unlock=RemoteUnlock(enabled=True)))
    modules = kernel.dracut_modules(wanted)
    assert "crypt-ssh" in modules and "network" in modules
    assert "crypt-ssh" not in kernel.dracut_modules(config())


def test_the_unlock_daemon_is_keyworded_and_told_where_to_read_its_keys() -> None:
    """It is ~amd64, and the module reads its configuration at initramfs build
    time, so the file has to exist before dracut runs."""
    from gentoo_install.model.config import RemoteUnlock

    wanted = replace(config(), kernel=KernelConfig(remote_unlock=RemoteUnlock(enabled=True)))
    recorder = apply_kernel(wanted)
    keywords = recorder.files[
        PurePosixPath("/etc/portage/package.accept_keywords/dracut-crypt-ssh")
    ]
    assert keywords == "sys-kernel/dracut-crypt-ssh ~amd64\n"
    written = recorder.files[PurePosixPath("/etc/dracut.conf.d/crypt-ssh.conf")]
    assert 'dropbear_port="222"' in written
    # The `unlock` helper runs cryptsetup and the module does not pull it in.
    assert "/sbin/cryptsetup" in written
    assert 'dropbear_ed25519_key="SYSTEM"' in written


def test_the_systemd_boot_branch_keeps_what_the_shared_prefix_built() -> None:
    """It rebound the list and threw away the emerge above it. bootctl writes
    the boot entry through efivarfs, so efibootmgr is left out deliberately."""
    from gentoo_install.model.config import Bootloader, BootloaderConfig, Firmware

    chosen = replace(
        config(),
        bootloader=BootloaderConfig(kind=Bootloader.SYSTEMD_BOOT, firmware=Firmware.UEFI),
    )
    described = [one.describe() for one in bootloader.build(chosen)]
    assert not any("efibootmgr" in line for line in described)
    assert any("install bootctl" in line for line in described)
    # GRUB still gets it: grub-install needs efibootmgr to write the entry.
    grub = [one.describe() for one in bootloader.build(config())]
    assert any("efibootmgr" in line for line in grub)


def test_which_tool_builds_a_kernel_module_is_one_table() -> None:
    """It was two: a `builds_a_module` flag nobody read and an `OUT_OF_TREE`
    dict beside it. Two tables holding one fact disagree eventually."""
    from gentoo_install.plan.kernel import STACK_PACKAGES, _out_of_tree_modules

    assert not hasattr(kernel, "OUT_OF_TREE")
    declared = {atom for tool in STACK_PACKAGES.values() for atom in tool.modules}
    assert declared == {"sys-fs/zfs"}

    zfs = load(Path("tests/fixtures/vm-zfs.toml"))
    assert set(_out_of_tree_modules(zfs)) == declared
    assert _out_of_tree_modules(config()) == ()


def test_the_prebuilt_patched_kernel_is_the_one_a_chinese_interface_takes() -> None:
    """`sys-kernel/gentoo-cjk-kernel-bin` is in gentoo-zh beside the source
    one: same cjktty patch, same `+cjk` flag, same `virtual/dist-kernel`, and
    nothing to compile on the target."""
    from gentoo_install.model.config import KernelSource
    from gentoo_install.plan.kernel import CJK_KERNELS, KERNEL_PACKAGES

    assert KERNEL_PACKAGES[KernelSource.CJK_BIN] == "sys-kernel/gentoo-cjk-kernel-bin"
    assert set(CJK_KERNELS) == {KernelSource.CJK_BIN, KernelSource.CJK}
    assert set(KERNEL_PACKAGES) == set(KernelSource)

    # Both take the keyword and the flag; neither comes from a binary host.
    for source in CJK_KERNELS:
        chosen = replace(config(), kernel=KernelConfig(source=source))
        described = [one.describe() for one in kernel.build(chosen)]
        assert any("as testing, with cjk" in line for line in described), source
        assert any("from source" in line for line in described), source


def test_the_prebuilt_patched_kernel_sits_beside_the_source_one() -> None:
    """`sys-kernel/gentoo-cjk-kernel-bin` is in gentoo-zh: same cjktty patch,
    same `+cjk` flag, same `virtual/dist-kernel`, nothing to compile."""
    from gentoo_install.plan.kernel import CJK_KERNELS, KERNEL_PACKAGES

    assert KERNEL_PACKAGES[KernelSource.CJK_BIN] == "sys-kernel/gentoo-cjk-kernel-bin"
    assert set(CJK_KERNELS) == {KernelSource.CJK_BIN, KernelSource.CJK}
    # Every choice has a package: a member with none installs nothing at all.
    assert set(KERNEL_PACKAGES) == set(KernelSource)

    for source in CJK_KERNELS:
        described = [
            one.describe()
            for one in kernel.build(replace(config(), kernel=KernelConfig(source=source)))
        ]
        # Keyworded and flagged like the other, and on no official binary host.
        assert any("as testing, with cjk" in line for line in described), source
        assert any("from source" in line for line in described), source


def test_zfsbootmenu_unlocks_once_rather_than_asking_the_initramfs_too() -> None:
    """ZBM unlocks the pool to read the kernel and the initramfs then asks for
    the same passphrase again, because kexec does not carry the loaded key."""
    recorder = Recorder()
    for operation in kernel.build(zfs_installation()):
        if isinstance(operation, kernel.StoreZfsKey):
            operation.apply(recorder)
    key = PurePosixPath("/etc/zfs/zpcala.key")
    # No trailing newline: `zfs load-key` trims one, so the file is read the
    # same either way and this is the form that cannot be wrong.
    assert recorder.files[key] == "a passphrase"
    assert recorder.modes[key] == 0o400
    assert recorder.files[PurePosixPath("/etc/dracut.conf.d/zfs-key.conf")] == (
        'install_items+=" /etc/zfs/zpcala.key "\n'
    )
    # On the installing system: the pool is imported there, and the target has
    # no `zfs` binary until sys-fs/zfs is merged later in the same stage.
    assert ("zfs", "set", "keylocation=file:///etc/zfs/zpcala.key", "zpcala") in recorder.commands
    assert not recorder.in_target


def test_a_separate_boot_partition_keeps_the_prompt() -> None:
    """The key file rides inside the initramfs, so an initramfs on a partition
    outside the pool would publish the passphrase in the clear."""
    nodes = [
        *zfs_root(),
        Partition(
            id=i("bootpart"),
            table=i("table"),
            index=3,
            role=PartitionRole.DATA,
            size=Size.parse("1GiB"),
        ),
        Filesystem(id=i("bootfs"), device=i("bootpart"), kind=FilesystemType.EXT4),
        Mountpoint(id=i("mnt-boot"), source=i("bootfs"), path=PurePosixPath("/boot")),
    ]
    outside = replace(zfs_installation(), disk=replace(
        zfs_installation().disk, graph=DeviceGraph.build(nodes)
    ))
    assert not [one for one in kernel.build(outside) if isinstance(one, kernel.StoreZfsKey)]


def test_a_pool_that_is_not_encrypted_needs_no_key_file() -> None:
    plain = [
        node if not isinstance(node, ZfsPool) else replace(node, encrypted=False)
        for node in zfs_root()
    ]
    open_pool = replace(zfs_installation(), disk=replace(
        zfs_installation().disk, graph=DeviceGraph.build(plain)
    ))
    assert not [one for one in kernel.build(open_pool) if isinstance(one, kernel.StoreZfsKey)]


def test_only_zfsbootmenu_moves_the_key_off_the_prompt() -> None:
    """Under any other bootloader the initramfs prompt is the only prompt, and
    a key file beside it would take the passphrase out of the boot path."""
    grub = replace(
        zfs_installation(),
        bootloader=BootloaderConfig(kind=Bootloader.GRUB, firmware=Firmware.UEFI),
    )
    assert not [one for one in kernel.build(grub) if isinstance(one, kernel.StoreZfsKey)]


def test_the_kernel_is_merged_before_anything_that_builds_a_module_for_it() -> None:
    """`sys-fs/zfs[dist-kernel]` depends on `virtual/dist-kernel`. Merged while
    the chosen kernel is still masked, Portage satisfies that virtual with a
    second kernel and builds zfs.ko for the one that will not boot."""
    described = [one.describe() for one in kernel.build(zfs_installation())]
    kernel_at = next(at for at, one in enumerate(described) if one.startswith("install the kernel"))
    module = next(at for at, one in enumerate(described) if "build against the dist-kernel" in one)
    built = next(at for at, one in enumerate(described) if "build a module for this kernel" in one)
    initramfs = next(at for at, one in enumerate(described) if one.startswith("rebuild the initramfs"))
    # sys-fs/zfs builds a kernel module, so it waits for the kernel; the dracut
    # module list is written after it too, or the kernel's own postinst asks
    # for a zfs module whose userland is not installed yet.
    listed = next(at for at, one in enumerate(described) if one == "tell dracut to carry zfs")
    assert kernel_at < listed < module < built < initramfs


def test_the_zfs_key_is_set_from_the_installing_system() -> None:
    """A stage3 has no `zfs` binary until sys-fs/zfs is merged, which happens
    later in the same stage; the pool is imported here in any case."""
    recorder = Recorder()
    for operation in kernel.build(zfs_installation()):
        if isinstance(operation, kernel.StoreZfsKey):
            operation.apply(recorder)
    assert recorder.commands and not recorder.in_target


def test_a_tool_that_builds_no_module_is_installed_before_the_kernel() -> None:
    """The kernel's postinst runs dracut, and dracut dies on a module whose
    tool is absent: `dmsetup: command not found`, then `Module 'lvm' cannot be
    installed`, then `Kernel install failed`. Caught by a VM run."""
    described = [
        one.describe() for one in kernel.build(load(Path("tests/fixtures/vm-luks.toml")))
    ]
    tool = next(at for at, one in enumerate(described) if "sys-fs/cryptsetup" in one)
    installed = next(at for at, one in enumerate(described) if one.startswith("install the kernel"))
    assert tool < installed


def test_a_kernel_with_no_modules_is_deleted_and_nothing_else_is() -> None:
    """`sys-fs/zfs` reinstalls the initramfs from its own postinst under a
    version it reads off /usr/src/linux, which is `-gentoo-dist` where the
    prebuilt kernel is `-gentoo-dist-bin`, so a second image appears under a
    name that cannot boot. generate-zbm then refuses every kernel it sees."""
    recorder = Recorder()
    recorder.replies["ls"] = ""
    kept = "6.18.41-gentoo-dist-bin"
    stray = "6.18.41-gentoo-dist"

    def listing(argv: Sequence[str], **rest: object) -> str:
        wanted = list(argv)
        if wanted[0] == "file":
            # Every image here holds the prebuilt kernel; the stray one carries
            # the wrong name, which is the whole defect.
            return f"Linux kernel x86 boot executable bzImage, version {kept} (builder@host)"
        if wanted[-1] == "/lib/modules":
            return f"{kept}\n"
        return "\n".join(
            (
                f"kernel-{kept}",
                f"initramfs-{kept}.img",
                f"System.map-{kept}",
                f"kernel-{stray}",
                f"initramfs-{stray}.img",
                "grub",
                "efi",
                "amd-uc.img",
            )
        )

    recorder.run_in_target = listing  # type: ignore[method-assign]
    removed: list[tuple[str, ...]] = []
    real = listing

    def watched(argv: Sequence[str], **rest: object) -> str:
        wanted = tuple(str(one) for one in argv)
        if wanted[0] == "rm":
            removed.append(wanted)
            return ""
        return real(argv, **rest)

    recorder.run_in_target = watched  # type: ignore[method-assign]
    kernel.RemoveUnbootableKernels().apply(recorder)
    assert [one[-1] for one in removed] == [
        f"/boot/kernel-{stray}",
        f"/boot/initramfs-{stray}.img",
    ]


def test_an_image_whose_name_disagrees_with_its_contents_is_deleted() -> None:
    """The case `generate-zbm` names: `sys-fs/zfs` leaves both a wrongly named
    image and a matching `/lib/modules` entry, so the modules test alone passes
    and the kernel is still refused with `ignoring inconsistent versions`."""
    recorder = Recorder()
    named = "6.18.41-gentoo-dist"
    inside = "6.18.41-gentoo-dist-bin"
    removed: list[str] = []

    def answering(argv: Sequence[str], **rest: object) -> str:
        wanted = [str(one) for one in argv]
        if wanted[0] == "rm":
            removed.append(wanted[-1])
            return ""
        if wanted[0] == "file":
            return f"Linux kernel x86 boot executable bzImage, version {inside} (b@h)"
        if wanted[-1] == "/lib/modules":
            # zfs built its module against the wrong version, so this passes.
            return f"{named}\n{inside}\n"
        return f"kernel-{named}\n"

    recorder.run_in_target = answering  # type: ignore[method-assign]
    kernel.RemoveUnbootableKernels().apply(recorder)
    assert removed == [f"/boot/kernel-{named}"]


def test_an_unreadable_image_is_left_alone() -> None:
    """`file` answering nothing is not evidence the image is wrong, and it may
    be the only kernel there is."""
    recorder = Recorder()
    version = "6.18.41-gentoo-dist-bin"
    calls: list[str] = []

    def answering(argv: Sequence[str], **rest: object) -> str:
        wanted = [str(one) for one in argv]
        calls.append(wanted[0])
        if wanted[0] == "file":
            return ""
        if wanted[-1] == "/lib/modules":
            return f"{version}\n"
        return f"kernel-{version}\n"

    recorder.run_in_target = answering  # type: ignore[method-assign]
    kernel.RemoveUnbootableKernels().apply(recorder)
    assert "rm" not in calls


def test_nothing_is_deleted_when_no_modules_directory_can_be_read() -> None:
    """Deleting on no evidence is worse than leaving an image that may be the
    only one there is."""
    recorder = Recorder()
    calls: list[tuple[str, ...]] = []

    def answering(argv: Sequence[str], **rest: object) -> str:
        wanted = tuple(str(one) for one in argv)
        calls.append(wanted)
        return "kernel-6.18.41-gentoo-dist\n" if wanted[-1] == "/boot" else ""

    recorder.run_in_target = answering  # type: ignore[method-assign]
    kernel.RemoveUnbootableKernels().apply(recorder)
    assert not any(one[0] == "rm" for one in calls)


def test_the_initramfs_parameters_reach_every_grub_entry() -> None:
    """`GRUB_CMDLINE_LINUX_DEFAULT` reaches only the default entry. A recovery
    entry built without `rd.luks.uuid` waits for a device that never appears,
    so what the initramfs needs to find the root goes in `GRUB_CMDLINE_LINUX`
    and the operator's own parameters go in the other."""
    installation = replace(
        config(encrypted_root()),
        bootloader=BootloaderConfig(kind=Bootloader.GRUB, kernel_params=("quiet",)),
    )
    grub = apply_boot(installation).files[PurePosixPath("/etc/default/grub")]
    every = next(
        line for line in grub.splitlines() if line.startswith("GRUB_CMDLINE_LINUX=")
    )
    default = next(
        line for line in grub.splitlines() if line.startswith("GRUB_CMDLINE_LINUX_DEFAULT=")
    )
    assert "rd.luks.uuid=" in every
    assert "quiet" in default and "rd.luks.uuid=" not in default


def test_the_cjk_kernel_lifts_the_mask_its_dependency_carries() -> None:
    """`gentoo-cjk-kernel` PDEPENDs on `=virtual/dist-kernel-${PV}-r100`. That
    revision exists only in gentoo-zh, and gentoo-zh masked its own
    `virtual/dist-kernel` because it is incompatible with `::gentoo`'s, whose
    copy carries no `-r100`. Without the unmask the emerge stops on a masked
    package with the disks already partitioned."""
    from gentoo_install.model.config import KernelSource
    from gentoo_install.plan.kernel import UnmaskCjkDistKernel

    for source, wanted in (
        (KernelSource.CJK_BIN, True),
        (KernelSource.CJK, True),
        (KernelSource.DIST_BIN, False),
    ):
        installation = replace(
            config(), kernel=replace(KernelConfig(), source=source)
        )
        built = kernel.build(installation)
        lifted = [one for one in built if isinstance(one, UnmaskCjkDistKernel)]
        assert bool(lifted) is wanted, source
        if not lifted:
            continue
        merged = next(
            n for n, one in enumerate(built) if "install the kernel" in one.describe()
        )
        assert built.index(lifted[0]) < merged, "the mask is lifted after the merge"
        recorder = Recorder()
        lifted[0].apply(recorder)
        written = recorder.files[PurePosixPath("/etc/portage/package.unmask/cjk-kernel")]
        assert written.strip() == "virtual/dist-kernel"


def test_the_stray_kernel_is_deleted_before_the_package_reinstalls_one() -> None:
    """Deleting last left `/boot` empty: the misnamed image `sys-fs/zfs`
    leaves is often the only one there, and generate-zbm then answers
    `Unable to find latest kernel`. `emerge --config` puts the image back
    under the name the package carries, so the removal has to come first."""
    from gentoo_install.plan.kernel import RebuildInitramfs, RemoveUnbootableKernels

    built = kernel.build(config(zfs_root()))
    removal = next(
        n for n, one in enumerate(built) if isinstance(one, RemoveUnbootableKernels)
    )
    rebuild = next(n for n, one in enumerate(built) if isinstance(one, RebuildInitramfs))
    assert removal < rebuild, [type(one).__name__ for one in built[removal - 1 : rebuild + 1]]


def test_a_zfs_root_keeps_its_kernel_in_the_pool() -> None:
    """ZFSBootMenu reads the kernel out of the boot environment's own `/boot`.
    `kernel-install` otherwise takes the mounted esp as `$BOOT` and writes
    `/efi/<entry-token>/<version>/`, so the pool has no kernel and generate-zbm
    answers `Unable to find latest kernel`.

    A drop-in, never the main file: kernel-install(8) says "the first of the
    files that is found will be used", so `/etc/kernel/install.conf` shadowed
    the one `sys-kernel/installkernel` ships and the next kernel merge died on
    `No initrd_generator=`.
    """
    from gentoo_install.plan.kernel import ConfigureInstallKernel

    for installation, wanted in (
        (
            replace(
                config(zfs_root()),
                bootloader=BootloaderConfig(
                    kind=Bootloader.ZFSBOOTMENU, firmware=Firmware.UEFI
                ),
            ),
            "/boot",
        ),
        (config(ext4_on_gpt()), ""),
    ):
        told = next(
            one for one in kernel.build(installation) if isinstance(one, ConfigureInstallKernel)
        )
        assert told.boot_root == wanted, installation.bootloader.kind
        recorder = Recorder()
        told.apply(recorder)
        drop_in = PurePosixPath("/etc/kernel/install.conf.d/50-gentoo-install.conf")
        assert (recorder.files.get(drop_in) == "BOOT_ROOT=/boot\n") is bool(wanted)
        # The file installkernel ships carries layout= and initrd_generator=,
        # and the first one found wins rather than the two merging.
        assert PurePosixPath("/etc/kernel/install.conf") not in recorder.files


def test_bootctl_is_not_rebuilt_for_flags_it_already_has() -> None:
    """`sys-kernel/installkernel[systemd-boot]` RDEPENDs
    `sys-apps/systemd[boot(-)]`, so the kernel stage has already pulled it in
    and a plain atom at the bootloader stage is `[ebuild R]`. One passing run
    spent 132 seconds rebuilding it with the flags it had.

    The operation stays: nothing else guarantees `bootctl` when the provider is
    `sys-apps/systemd-utils` on openrc.
    """
    from gentoo_install.plan.portage import Emerge

    installation = replace(
        config(),
        bootloader=BootloaderConfig(kind=Bootloader.SYSTEMD_BOOT, firmware=Firmware.UEFI),
    )
    merge = next(
        one
        for one in bootloader.build(installation)
        if isinstance(one, Emerge) and "bootctl" in one.summary
    )
    assert merge.only_if_absent
    recorder = Recorder()
    merge.apply(recorder)
    assert any("--noreplace" in one for one in recorder.in_target[0])
