# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Sequence

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.exec.config import load
from gentoo_install.model.config import InstallConfig, KernelSource
from gentoo_install.model.hardware import CpuVendor, HardwareFacts
from gentoo_install.plan import bootloader, kernel
from gentoo_install.plan.build import build as build_plan
from gentoo_install.plan.portage import (
    BINPKG_EXCLUDED,
    BINPKG_OPTIONS,
    EMERGE_OPTIONS,
    Emerge,
    InstallMode,
    SourcePolicy,
)
from gentoo_install.errors import ValidationFailed
from gentoo_install.model.validate import KernelCeiling

from .recorder import Recorder
from .layouts import config
from gentoo_install.plan.operations import Stage


def test_physical_intel_gets_microcode_before_its_kernel_merges() -> None:
    """The package needs testing, licence, and USE configuration before it
    enters the kernel transaction."""
    operations = build_plan(
        config(),
        load_catalog(),
        hardware=HardwareFacts(cpu_vendor=CpuVendor.INTEL, virtual_machine=False),
    )
    configured = next(
        operation for operation in operations if isinstance(operation, kernel.ConfigureIntelMicrocode)
    )
    merged = next(
        operation
        for operation in operations
        if isinstance(operation, Emerge) and kernel.INTEL_MICROCODE in operation.packages
    )

    def iuse(argv: Sequence[str]) -> str | None:
        if tuple(argv[:4]) == ("portageq", "metadata", "/", "ebuild") and argv[-1] == "IUSE":
            return "+dist-kernel +initramfs"
        return None

    recorder = Recorder(answering=iuse)
    configured.apply(recorder)

    assert configured.stage is Stage.PORTAGE
    assert operations.index(configured) < operations.index(merged)
    assert merged.packages[-1] == kernel.INTEL_MICROCODE
    assert merged.summary == "install the kernel and Intel microcode"
    assert recorder.files[PurePosixPath("/etc/portage/package.use/intel-microcode")] == (
        f"{kernel.INTEL_MICROCODE} dist-kernel initramfs\n"
    )
    assert recorder.files[PurePosixPath("/etc/portage/package.accept_keywords/intel-microcode")] == (
        f"{kernel.INTEL_MICROCODE} ~amd64\n"
    )
    assert recorder.files[PurePosixPath("/etc/portage/package.license/intel-microcode")] == (
        f"{kernel.INTEL_MICROCODE} intel-ucode\n"
    )


@pytest.mark.parametrize(
    "hardware",
    (
        HardwareFacts(cpu_vendor=CpuVendor.AMD, virtual_machine=False),
        HardwareFacts(cpu_vendor=CpuVendor.INTEL, virtual_machine=True),
        HardwareFacts(cpu_vendor=CpuVendor.INTEL, virtual_machine=None),
        HardwareFacts(),
    ),
)
def test_only_a_confirmed_physical_intel_needs_the_intel_microcode_package(
    hardware: HardwareFacts,
) -> None:
    operations = kernel.build(config(), hardware)

    assert not any(
        isinstance(operation, kernel.ConfigureIntelMicrocode) for operation in operations
    )
    assert not any(
        isinstance(operation, Emerge) and kernel.INTEL_MICROCODE in operation.packages
        for operation in operations
    )


def test_no_dracut_config_this_installer_writes_is_a_file_a_package_owns() -> None:
    """`sys-kernel/dracut-crypt-ssh` installs `/etc/dracut.conf.d/crypt-ssh.conf`,
    and this installer wrote its own file at that exact path. Portage refused to
    replace it and left the package's copy as `._cfg0000_crypt-ssh.conf`, which
    nothing in an install ever applies: `vm-unlock`'s log carries `IMPORTANT:
    config file '/etc/dracut.conf.d/crypt-ssh.conf' needs updating`.

    The prefix is the other half. dracut reads the directory in lexical order,
    so a name sorting before what the package or dracut itself ships loses.
    """
    owned = {"crypt-ssh.conf", "01-gentoo.conf", "10-hostonly.conf", "11-generic.conf"}
    ours = kernel.REMOTE_UNLOCK_CONFIG
    assert ours.parent == PurePosixPath("/etc/dracut.conf.d")
    assert ours.name not in owned, ours
    assert all(ours.name > one for one in owned), ours


def test_the_unlock_operation_describes_the_keyword_it_writes() -> None:
    """`apply` writes the `dracut-crypt-ssh` keyword in both branches, and the
    ZFSBootMenu branch's `describe` named only the dracut omission file. A dry
    run has to say every file a real run puts on the disk.
    """
    from gentoo_install.plan.portage import PortageConfigKind

    for fixture in ("vm-unlock", "zbm-unlock"):
        installation = load(Path("tests/fixtures") / f"{fixture}.toml")
        operation = next(
            one
            for one in kernel.build(installation)
            if isinstance(one, kernel.ConfigureRemoteUnlock)
        )
        recorder = Recorder()
        operation.apply(recorder)
        keyworded = [
            path
            for path in recorder.files
            if PortageConfigKind.KEYWORDS.value in str(path)
        ]
        assert keyworded, f"{fixture}: nothing wrote the keyword"
        assert kernel.REMOTE_UNLOCK_PACKAGE in operation.describe(), (
            fixture,
            operation.describe(),
        )
    # And the two branches really are different, so this covers both.
    assert (
        next(
            one for one in kernel.build(load(Path("tests/fixtures/zbm-unlock.toml")))
            if isinstance(one, kernel.ConfigureRemoteUnlock)
        ).system_initramfs
        is False
    )


def test_grub_remote_unlock_keeps_the_system_dracut_path() -> None:
    installation = load(Path("tests/fixtures/vm-unlock.toml"))
    operation = next(
        one
        for one in kernel.build(installation)
        if isinstance(one, kernel.ConfigureRemoteUnlock)
    )
    recorder = Recorder()
    operation.apply(recorder)

    written = recorder.files[kernel.REMOTE_UNLOCK_CONFIG]
    assert 'dropbear_port="2222"' in written
    assert 'dropbear_rsa_key="SYSTEM"' in written
    assert 'install_items+=" /sbin/cryptsetup "' in written
    assert kernel.dracut_modules(installation)[-2:] == ("crypt-ssh", "network-legacy")
    assert not any(
        isinstance(one, bootloader.ConfigureZfsBootMenuRemoteAccess)
        for one in bootloader.build(installation)
    )
    unlock_packages = next(
        one.packages
        for one in kernel.build(installation)
        if isinstance(one, Emerge) and kernel.REMOTE_UNLOCK_PACKAGE in one.packages
    )
    # The legacy network tools come with it: the system image omits systemd
    # too, so `network-legacy` needs them and dracut refuses `crypt-ssh`
    # without it.
    assert unlock_packages == (kernel.REMOTE_UNLOCK_PACKAGE, *kernel.LEGACY_NETWORK_PACKAGES)


def test_zfsbootmenu_installs_network_legacy_executables() -> None:
    """The legacy network tools are the unlock's prerequisite, so the fixture
    that carries the unlock is the one that asks for them."""
    installation = load(Path("tests/fixtures/zbm-unlock.toml"))
    operations = kernel.build(installation)
    unlock_packages = next(
        one.packages
        for one in operations
        if isinstance(one, Emerge) and kernel.REMOTE_UNLOCK_PACKAGE in one.packages
    )
    assert unlock_packages == (
        kernel.REMOTE_UNLOCK_PACKAGE,
        *kernel.LEGACY_NETWORK_PACKAGES,
    )

    request = next(
        one
        for one in operations
        if isinstance(one, kernel.RequestLegacyNetworkTools)
    )
    recorder = Recorder()
    request.apply(recorder)
    assert recorder.files[
        PurePosixPath("/etc/portage/package.use/legacy-network")
    ] == "net-misc/dhcp client -server\nnet-misc/iputils arping\n"


def test_systemd_unlock_rebuild_uses_exact_oneshot_argv() -> None:
    installation = load(Path("tests/fixtures/vm-unlock.toml"))
    rebuild = next(
        one
        for one in kernel.build(installation)
        if isinstance(one, Emerge) and one.summary == "rebuild systemd with the unlock generator"
    )

    assert rebuild.mode is InstallMode.ONESHOT
    recorder = Recorder()
    rebuild.apply(recorder)
    assert recorder.only("emerge") == (
        "emerge",
        *EMERGE_OPTIONS,
        "--oneshot",
        *BINPKG_OPTIONS[:-1],
        f"{BINPKG_EXCLUDED} sys-apps/systemd",
        "--",
        "sys-apps/systemd",
    )


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


def test_zfs_kernel_check_reads_target_metadata_and_refuses_unknown() -> None:
    installation = load(Path("tests/fixtures/zfs-zbm.toml"))
    operation = kernel.VerifyZfsKernelCompatibility(version="7.1.2")
    recorder = Recorder(zfs_ceiling=KernelCeiling("7.0"))
    with pytest.raises(ValidationFailed, match="above the sys-fs/zfs ceiling 7.0"):
        operation.apply(recorder)

    unknown = Recorder()
    with pytest.raises(ValidationFailed, match="kernel ceiling could not be read"):
        kernel.VerifyZfsKernelCompatibility(version="6.12.1").apply(unknown)


def test_the_unlock_initramfs_omits_systemd_so_its_queue_runs_first() -> None:
    """`crypt-ssh` starts dropbear from a dracut initqueue hook. A systemd
    initramfs asks for the passphrase before it reaches that queue: a guest's
    own serial log finishes `dracut cmdline hook` and `dracut pre-udev hook`,
    then prints `Please enter passphrase for disk root`, and never starts
    `dracut-initqueue` at all — so nothing was ever listening on the port.

    ZFSBootMenu's image omits systemd for the same reason, and that is the only
    remote-unlock image this installer has ever built successfully.
    """
    installation = load(Path("tests/fixtures/vm-unlock.toml"))
    operation = next(
        one
        for one in kernel.build(installation)
        if isinstance(one, kernel.ConfigureRemoteUnlock)
    )
    recorder = Recorder()
    operation.apply(recorder)
    written = recorder.files[kernel.REMOTE_UNLOCK_CONFIG]

    assert "omit_dracutmodules" in written, written
    for module in ("systemd", "systemd-networkd"):
        assert module in written, (module, written)

    # And the network module has to be the legacy one: with systemd omitted
    # there is no `systemd-networkd` for dracut's `40network` to pick.
    modules = kernel.dracut_modules(installation)
    assert "network-legacy" in modules, modules
    assert "network" not in modules, modules

    # The legacy network module refuses to install without dhclient and
    # arping, and dracut then refuses `crypt-ssh` with it: `Module 'crypt-ssh'
    # cannot be installed` stopped an install at the kernel's own postinst
    # when the modules were changed and the packages were not.
    unlock_packages = next(
        one.packages
        for one in kernel.build(installation)
        if isinstance(one, Emerge) and kernel.REMOTE_UNLOCK_PACKAGE in one.packages
    )
    for package in kernel.LEGACY_NETWORK_PACKAGES:
        assert package in unlock_packages, (package, unlock_packages)
    assert any(
        isinstance(one, kernel.RequestLegacyNetworkTools)
        for one in kernel.build(installation)
    )

    # Negative control: the fixture that does not unlock remotely keeps the
    # ordinary systemd initramfs, so this is not a change to every install.
    plain = load(Path("tests/fixtures/vm-luks.toml"))
    assert not plain.kernel.remote_unlock.enabled, "the control has to be a plain one"
    assert "network-legacy" not in kernel.dracut_modules(plain)
    assert not [
        one for one in kernel.build(plain) if isinstance(one, kernel.ConfigureRemoteUnlock)
    ]
    assert not [
        one for one in kernel.build(plain) if isinstance(one, kernel.RequestLegacyNetworkTools)
    ]


def test_the_firmware_licence_description_names_every_token_it_writes() -> None:
    """`no-source-code` is not the same statement as redistribution.

    The description said `accept the linux-firmware licence` while `apply`
    wrote both tokens, so a dry-run read by someone deciding whether to
    install a blob nobody can rebuild showed the weaker half alone. The
    default `ACCEPT_LICENSE` is `@FREE`, which makes this an exception to the
    policy rather than an instance of it, and the description says so.
    """
    from gentoo_install.model.config import PortageConfig
    from gentoo_install.plan.kernel import AcceptFirmwareLicence

    operation = AcceptFirmwareLicence()
    written = Recorder()
    operation.apply(written)

    lines = [
        text
        for path, text in written.files.items()
        if path.name == "linux-firmware"
    ]
    assert lines, written.files
    tokens = [one for one in lines[0].split() if one.startswith(("linux-fw", "no-source"))]
    assert tokens, lines[0]

    said = operation.describe()
    for token in tokens:
        assert token in said, (token, said)
    # And it is an exception, not the policy: the default is @FREE.
    assert "@FREE" in PortageConfig().accept_license, PortageConfig().accept_license
    assert "ACCEPT_LICENSE" in said, said
