from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

from gentoo_install.exec.config import load
from gentoo_install.model.config import InstallConfig, RemoteUnlock
from gentoo_install.plan import bootloader, kernel

from .recorder import Recorder


def zfsbootmenu_remote_installation() -> InstallConfig:
    installation = load(Path("tests/fixtures/vm-zfs-encrypted.toml"))
    return replace(
        installation,
        system=replace(
            installation.system,
            authorized_keys=("ssh-ed25519 operator-key remote@example",),
        ),
        kernel=replace(
            installation.kernel,
            remote_unlock=RemoteUnlock(
                enabled=True,
                port=2201,
                address="192.0.2.10/24",
                gateway="192.0.2.1",
                interface="eth0",
            ),
        ),
    )


def test_zfsbootmenu_carries_remote_access_in_its_own_image() -> None:
    installation = zfsbootmenu_remote_installation()
    boot_operations = bootloader.build(installation)
    configured = next(
        operation
        for operation in boot_operations
        if isinstance(operation, bootloader.ConfigureZfsBootMenuRemoteAccess)
    )
    generated = next(
        index
        for index, operation in enumerate(boot_operations)
        if isinstance(operation, bootloader.InstallZfsBootMenu)
    )
    installed = boot_operations[generated]
    assert isinstance(installed, bootloader.InstallZfsBootMenu)
    assert "rd.neednet=1" not in installed.kernel_params
    assert not any(parameter.startswith("ip=") for parameter in installed.kernel_params)
    assert boot_operations.index(configured) < generated
    assert "/etc/zfsbootmenu/dracut.conf.d/dropbear.conf" in configured.describe()
    assert "/etc/dropbear" in configured.describe()

    recorder = Recorder()
    configured.apply(recorder)
    zbm = recorder.files[PurePosixPath("/etc/zfsbootmenu/dracut.conf.d/dropbear.conf")]
    assert 'add_dracutmodules+=" crypt-ssh "' in zbm
    assert 'dropbear_port="2201"' in zbm
    assert "dropbear_rsa_key=/etc/dropbear/ssh_host_rsa_key" in zbm
    assert "dropbear_ecdsa_key=/etc/dropbear/ssh_host_ecdsa_key" in zbm
    assert "dropbear_acl=/etc/dropbear/root_key" in zbm
    assert recorder.files[PurePosixPath("/etc/dropbear/root_key")] == (
        "ssh-ed25519 operator-key remote@example\n"
    )
    assert recorder.modes[PurePosixPath("/etc/dropbear/root_key")] == 0o600
    assert recorder.files[PurePosixPath("/etc/cmdline.d/dracut-network.conf")] == (
        "ip=192.0.2.10::192.0.2.1:255.255.255.0::eth0:none rd.neednet=1\n"
    )
    assert len(recorder.argv_starting("ssh-keygen")) == 3

    kernel_recorder = Recorder()
    for operation in kernel.build(installation):
        if isinstance(operation, kernel.ConfigureRemoteUnlock):
            operation.apply(kernel_recorder)
    assert kernel_recorder.files[PurePosixPath("/etc/dracut.conf.d/crypt-ssh.conf")] == (
        'omit_dracutmodules+=" crypt-ssh "\n'
    )
    assert "crypt-ssh" not in kernel.dracut_modules(installation)
    assert "network" not in kernel.dracut_modules(installation)


def test_a_systemd_boot_machine_shows_its_menu() -> None:
    """`bootctl install` writes no `loader.conf`, and systemd-boot's documented
    default for `timeout` is 0: "no menu is shown and the default entry will be
    booted immediately". An encrypted openrc machine went from firmware
    straight to the passphrase prompt, with no way to pick an older kernel.
    """
    installation = load(Path("tests/fixtures/openrc-sdboot.toml"))
    operations = bootloader.build(installation)
    shown = [one for one in operations if isinstance(one, bootloader.ShowTheBootMenu)]
    assert len(shown) == 1, [one.describe() for one in operations]
    assert shown[0].seconds > 0, shown[0]

    recorder = Recorder()
    shown[0].apply(recorder)
    written = recorder.files[shown[0].esp / "loader" / "loader.conf"]
    assert f"timeout {shown[0].seconds}" in written, written


def test_a_grub_machine_writes_no_loader_conf() -> None:
    """The menu timeout for GRUB is `GRUB_TIMEOUT` in `/etc/default/grub`, and
    a `loader.conf` beside it would be read by nothing."""
    installation = load(Path("tests/fixtures/vm-btrfs.toml"))
    operations = bootloader.build(installation)
    assert not [one for one in operations if isinstance(one, bootloader.ShowTheBootMenu)]
