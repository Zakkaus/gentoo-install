from __future__ import annotations

import pytest

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
    assert "crypt-ssh" in zbm, zbm
    assert 'dropbear_port="2201"' in zbm
    # `generate-zbm` exited 1 with "Module 'crypt-ssh' depends on module
    # 'network', which can't be installed": dracut's `40network` resolves to
    # `systemd-networkd`, which needs the `systemd` module this image lacks.
    # Naming `network-legacy` makes `40network` pick it, per its own
    # `depends()`, which prefers an implementation already included.
    assert "network-legacy" in zbm, zbm
    assert (
        'omit_dracutmodules+=" systemd-networkd systemd-battery-check "' in zbm
    ), zbm
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
    # One per host key type, and the type list is what the format allows: see
    # test_every_zfsbootmenu_host_key_can_be_written_in_the_format_asked_for.
    assert len(recorder.argv_starting("ssh-keygen")) == len(bootloader.ZBM_HOST_KEY_TYPES)

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


def test_every_zfsbootmenu_host_key_can_be_written_in_the_format_asked_for() -> None:
    """`ssh-keygen -m PEM` refuses ed25519 with "Saving key failed: invalid
    format", so the install stopped at exit 4 in the middle of writing
    ZFSBootMenu's remote access. Measured with `ssh-keygen` itself; RSA and
    ECDSA both write `-----BEGIN RSA PRIVATE KEY-----` and
    `-----BEGIN EC PRIVATE KEY-----`.

    ZFSBootMenu's own documentation names `dropbear_rsa_key` and
    `dropbear_ecdsa_key` and no third one.
    """
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from gentoo_install.plan import bootloader

    assert "ed25519" not in bootloader.ZBM_HOST_KEY_TYPES, bootloader.ZBM_HOST_KEY_TYPES
    keygen = shutil.which("ssh-keygen")
    if keygen is None:  # pragma: no cover - the medium always carries it
        pytest.skip("ssh-keygen is not on this machine")
    with tempfile.TemporaryDirectory() as where:
        for keytype in bootloader.ZBM_HOST_KEY_TYPES:
            target = Path(where) / f"ssh_host_{keytype}_key"
            done = subprocess.run(
                [keygen, "-q", "-N", "", "-m", "PEM", "-t", keytype, "-f", str(target)],
                capture_output=True,
            )
            assert target.is_file(), (keytype, done.stdout, done.stderr)
            assert target.read_bytes().startswith(b"-----BEGIN "), keytype
