from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.exec.config import load
from gentoo_install.model.config import InstallConfig, KernelSource
from gentoo_install.plan import bootloader, kernel
from gentoo_install.plan.portage import Emerge, SourcePolicy

from .recorder import Recorder


def test_grub_remote_unlock_keeps_the_system_dracut_path() -> None:
    installation = load(Path("tests/fixtures/vm-unlock.toml"))
    operation = next(
        one
        for one in kernel.build(installation)
        if isinstance(one, kernel.ConfigureRemoteUnlock)
    )
    recorder = Recorder()
    operation.apply(recorder)

    written = recorder.files[PurePosixPath("/etc/dracut.conf.d/crypt-ssh.conf")]
    assert 'dropbear_port="2222"' in written
    assert 'dropbear_rsa_key="SYSTEM"' in written
    assert 'install_items+=" /sbin/cryptsetup "' in written
    assert kernel.dracut_modules(installation)[-2:] == ("crypt-ssh", "network")
    assert not any(
        isinstance(one, bootloader.ConfigureZfsBootMenuRemoteAccess)
        for one in bootloader.build(installation)
    )
    unlock_packages = next(
        one.packages
        for one in kernel.build(installation)
        if isinstance(one, Emerge) and kernel.REMOTE_UNLOCK_PACKAGE in one.packages
    )
    assert unlock_packages == (kernel.REMOTE_UNLOCK_PACKAGE,)


def test_zfsbootmenu_installs_network_legacy_executables() -> None:
    installation = load(Path("tests/fixtures/zfs-zbm.toml"))
    operations = kernel.build(installation)
    unlock_packages = next(
        one.packages
        for one in operations
        if isinstance(one, Emerge) and kernel.REMOTE_UNLOCK_PACKAGE in one.packages
    )
    assert unlock_packages == (
        kernel.REMOTE_UNLOCK_PACKAGE,
        *kernel.ZBM_LEGACY_NETWORK_PACKAGES,
    )

    request = next(
        one
        for one in operations
        if isinstance(one, kernel.RequestZfsBootMenuNetworkTools)
    )
    recorder = Recorder()
    request.apply(recorder)
    assert recorder.files[
        PurePosixPath("/etc/portage/package.use/zfsbootmenu-network")
    ] == "net-misc/dhcp client -server\nnet-misc/iputils arping\n"


def test_build_derives_stack_once_for_prebuilt_zfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = load(Path("tests/fixtures/zfs-zbm.toml"))
    installation = replace(
        installation,
        kernel=replace(installation.kernel, source=KernelSource.DIST_BIN),
    )
    derivations = 0
    derive = kernel.dracut_modules

    def counted(config: InstallConfig) -> tuple[str, ...]:
        nonlocal derivations
        derivations += 1
        return derive(config)

    monkeypatch.setattr(kernel, "dracut_modules", counted)

    operations = kernel.build(installation)

    assert derivations == 1
    kernel_and_modules = next(
        one
        for one in operations
        if isinstance(one, Emerge)
        and one.summary == "install the kernel and the tools that build a module for it"
    )
    assert kernel_and_modules.packages == (
        "sys-kernel/gentoo-kernel-bin",
        "sys-fs/zfs",
    )
    assert kernel_and_modules.source == SourcePolicy.build_subset(("sys-fs/zfs",))
