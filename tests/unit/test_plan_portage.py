from __future__ import annotations

import pytest

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from gentoo_install.model.config import (
    Binhost,
    BinhostChannel,
    Firmware,
    InitSystem,
    InstallConfig,
    Keywords,
    MirrorConfig,
    MirrorRegion,
    Overlay,
    PortageConfig,
    SystemConfig,
)
from gentoo_install.errors import ConfigError
from gentoo_install.plan import portage
from gentoo_install.plan.operations import Operation

from .layouts import config
from .recorder import Recorder

MIRROR = "https://distfiles.gentoo.org"


def apply_all(installation: InstallConfig) -> Recorder:
    recorder = Recorder()
    for operation in [*portage.build(installation, MIRROR), *portage.finish(installation)]:
        operation.apply(recorder)
    return recorder


def with_portage(**fields: Any) -> InstallConfig:
    return replace(config(), portage=replace(PortageConfig(), **fields))


def test_stage3_is_unpacked_with_xattrs_because_capabilities_would_be_lost() -> None:
    argv = apply_all(config()).only("tar", "--extract")
    assert "--xattrs-include=*.*" in argv
    assert "--numeric-owner" in argv
    assert "--preserve-permissions" in argv


def test_the_chroot_gets_proc_rbinds_and_a_slave_run() -> None:
    recorder = apply_all(config())
    mounts = recorder.argv_starting("mount")
    assert ("mount", "--types", "proc", "/proc", "/mnt/gentoo/proc") in mounts
    assert ("mount", "--rbind", "/sys", "/mnt/gentoo/sys") in mounts
    assert ("mount", "--make-rslave", "/mnt/gentoo/sys") in mounts
    assert ("mount", "--bind", "/run", "/mnt/gentoo/run") in mounts
    assert ("mount", "--make-slave", "/mnt/gentoo/run") in mounts


def test_resolv_conf_is_copied_so_the_chroot_can_resolve_a_mirror() -> None:
    assert apply_all(config()).only(
        "install", "--mode=0644", "/etc/resolv.conf", "/mnt/gentoo/etc/resolv.conf"
    )


def test_efivarfs_comes_with_the_recursive_bind_rather_than_a_second_mount() -> None:
    """Mounting it again would fail on a machine that booted BIOS, and the
    rbind of /sys already carries it."""
    recorder = apply_all(config())
    assert not recorder.argv_starting("mount", "--types", "efivarfs")
    assert ("mount", "--rbind", "/sys", "/mnt/gentoo/sys") in recorder.argv_starting("mount")


def test_make_conf_carries_the_flags_the_configuration_set() -> None:
    installation = with_portage(
        common_flags="-O2 -pipe -march=x86-64-v3",
        makeopts="-j8",
        use=("dracut", "cjk"),
        video_cards=("amdgpu",),
    )
    written = apply_all(installation).files[PurePosixPath("/etc/portage/make.conf")]
    assert 'COMMON_FLAGS="-O2 -pipe -march=x86-64-v3"' in written
    assert 'MAKEOPTS="-j8"' in written
    assert 'USE="dracut cjk"' in written
    assert 'VIDEO_CARDS="amdgpu"' in written


def test_l10n_is_derived_from_the_locales_rather_than_listed_twice() -> None:
    installation = replace(
        config(), system=SystemConfig(locales=("en_US.UTF-8", "zh_CN.UTF-8", "zh_TW.UTF-8"))
    )
    written = apply_all(installation).files[PurePosixPath("/etc/portage/make.conf")]
    assert 'L10N="en-US zh-CN zh-TW"' in written


def test_a_chinese_region_gets_the_chinese_mirrors() -> None:
    installation = with_portage(mirrors=MirrorConfig(region=MirrorRegion.CN))
    written = apply_all(installation).files[PurePosixPath("/etc/portage/make.conf")]
    assert "mirrors.cernet.edu.cn" in written


def test_the_autounmask_files_exist_before_any_emerge_runs() -> None:
    installation = with_portage(binhost=Binhost(official=True, community=BinhostChannel.STABLE))
    operations: list[Operation] = portage.build(installation, MIRROR)
    created = next(
        n for n, operation in enumerate(operations) if "autounmask" in operation.describe()
    )
    merges = [n for n, operation in enumerate(operations) if isinstance(operation, portage.Emerge)]
    assert merges, "the fixture has to merge something for this to mean anything"
    assert created < min(merges)


def test_the_repository_is_configured_before_it_is_synced() -> None:
    recorder = apply_all(config())
    assert PurePosixPath("/etc/portage/repos.conf/gentoo.conf") in recorder.files
    stanza = recorder.files[PurePosixPath("/etc/portage/repos.conf/gentoo.conf")]
    assert "sync-type = git" in stanza
    assert "sync-depth = 1" in stanza
    assert "sync-git-verify-commit-signature = true" in stanza


def test_the_local_copy_goes_before_the_first_sync_or_git_refuses() -> None:
    recorder = apply_all(config())
    removed = recorder.in_target.index(("rm", "--recursive", "--force", "/var/db/repos/gentoo"))
    synced = recorder.in_target.index(("emerge", "--sync", "gentoo"))
    assert removed < synced


def test_the_first_tree_arrives_by_webrsync_because_stage3_has_no_git() -> None:
    operations = portage.build(config(), MIRROR)
    described = [operation.describe() for operation in operations]
    webrsync = next(n for n, text in enumerate(described) if "emerge-webrsync" in text)
    git = next(n for n, text in enumerate(described) if "dev-vcs/git" in text)
    git_sync = next(n for n, text in enumerate(described) if text == "sync repository gentoo")
    assert webrsync < git < git_sync


def test_a_verified_repository_names_the_key_it_verifies_against() -> None:
    stanza = apply_all(config()).files[PurePosixPath("/etc/portage/repos.conf/gentoo.conf")]
    assert "sync-openpgp-key-path = /usr/share/openpgp-keys/gentoo-release.asc" in stanza


def test_an_overlay_is_accepted_only_for_itself() -> None:
    installation = with_portage(
        overlays=(Overlay(name="gentoo-zh", sync_uri="https://example.invalid/overlay.git"),)
    )
    written = apply_all(installation).files
    assert written[PurePosixPath("/etc/portage/package.accept_keywords/gentoo-zh")] == (
        "*/*::gentoo-zh ~amd64\n"
    )


def test_the_binhost_key_is_signed_after_getuto_creates_the_keyring() -> None:
    installation = with_portage(binhost=Binhost(official=True, community=BinhostChannel.STABLE))
    recorder = apply_all(installation)
    commands = [argv[0] if argv[0] != "gpg" else argv[3] for argv in recorder.in_target]
    assert "getuto" in commands
    getuto = commands.index("getuto")
    imported = next(n for n, argv in enumerate(recorder.in_target) if "--import" in argv)
    signed = next(n for n, argv in enumerate(recorder.in_target) if "--lsign-key" in argv)
    assert getuto < imported < signed


def test_the_binhost_is_only_trusted_once_its_key_is() -> None:
    installation = with_portage(binhost=Binhost(official=True, community=BinhostChannel.STABLE))
    operations = portage.build(installation, MIRROR)
    signed = next(n for n, operation in enumerate(operations) if "locally sign" in operation.describe())
    added = next(n for n, operation in enumerate(operations) if "binary package host" in operation.describe())
    assert signed < added
    stanza = apply_all(installation).files[PurePosixPath("/etc/portage/binrepos.conf/gentoo-zh.conf")]
    assert "verify-signature = true" in stanza


def test_a_stable_system_never_writes_global_testing_keywords() -> None:
    assert portage.finish(with_portage(keywords=Keywords.STABLE)) == []
    assert portage.finish(with_portage(keywords=Keywords.TESTING))


def test_the_stage3_variant_follows_the_init_system() -> None:
    systemd = apply_all(config()).commands
    openrc = apply_all(replace(config(), system=SystemConfig(init=InitSystem.OPENRC))).commands
    assert any("systemd" in argv for argv in systemd if argv[0] == "fetch-stage3")
    assert any("openrc" in argv for argv in openrc if argv[0] == "fetch-stage3")


def test_a_binhost_that_cannot_be_trusted_compiles_instead_of_stopping() -> None:
    """The disks are written by the time trust is set up, so a keyring that
    failed degrades to source rather than ending the run."""
    recorder = Recorder(failures={"getuto"})
    portage.PrepareBinhostTrust().apply(recorder)
    assert recorder.degraded(portage.BINARY_PACKAGES)

    portage.TrustBinhostKey(
        binhost="gentoo-zh", fingerprint="0" * 40, key_path=PurePosixPath("/usr/share/key.asc")
    ).apply(recorder)
    assert ("gpg", "--homedir", "/etc/portage/gnupg", "--import", "/usr/share/key.asc") not in (
        recorder.in_target
    )

    portage.ConfigureBinhost(name="gentoo-zh", sync_uri="https://example/", verify=True).apply(
        recorder
    )
    assert PurePosixPath("/etc/portage/binrepos.conf/gentoo-zh.conf") not in recorder.files

    portage.Emerge(packages=("sys-boot/grub",), summary="install the bootloader").apply(recorder)
    emerge = next(argv for argv in recorder.in_target if argv[0] == "emerge")
    assert "--usepkg=n" in emerge and "--getbinpkg=y" not in emerge


def test_a_binhost_that_can_be_trusted_is_used() -> None:
    recorder = Recorder()
    portage.PrepareBinhostTrust().apply(recorder)
    portage.Emerge(packages=("sys-boot/grub",), summary="install the bootloader").apply(recorder)
    emerge = next(argv for argv in recorder.in_target if argv[0] == "emerge")
    assert "--getbinpkg=y" in emerge and "--usepkg=n" not in emerge


def test_a_failed_community_key_leaves_the_official_host_alone() -> None:
    """The official host's key comes from `getuto`, so one community key that
    could not be signed says nothing about it."""
    recorder = Recorder(failures={"gpg"})
    portage.TrustBinhostKey(
        binhost="gentoo-zh", fingerprint="0" * 40, key_path=PurePosixPath("/usr/share/key.asc")
    ).apply(recorder)
    assert not recorder.degraded(portage.BINARY_PACKAGES)

    portage.ConfigureBinhost(name="gentoo-zh", sync_uri="https://example/", verify=True).apply(
        recorder
    )
    portage.ConfigureBinhost(name="gentoo", sync_uri="https://official/", verify=True).apply(
        recorder
    )
    written = set(recorder.files)
    assert PurePosixPath("/etc/portage/binrepos.conf/gentoo-zh.conf") not in written
    assert PurePosixPath("/etc/portage/binrepos.conf/gentoo.conf") in written

    portage.Emerge(packages=("sys-boot/grub",), summary="install the bootloader").apply(recorder)
    assert "--getbinpkg=y" in next(argv for argv in recorder.in_target if argv[0] == "emerge")


def test_named_atoms_are_accepted_without_opening_the_whole_system() -> None:
    """The third scope: the rest of the system keeps the guarantee stable
    carries, so the binary host still matches."""
    wanted = replace(
        config(),
        portage=replace(config().portage, testing_packages=("app-editors/neovim", "app-misc/tmux")),
    )
    recorder = Recorder()
    for operation in portage.build(wanted, "https://distfiles.gentoo.org"):
        if isinstance(operation, portage.AcceptTestingPackages):
            operation.apply(recorder)
    written = recorder.files[PurePosixPath("/etc/portage/package.accept_keywords/user")]
    assert written == "app-editors/neovim ~amd64\napp-misc/tmux ~amd64\n"
    assert not any(
        isinstance(operation, portage.AcceptTestingGlobally)
        for operation in portage.build(wanted, "https://distfiles.gentoo.org")
    )


def test_a_package_name_that_matches_nothing_stops_before_the_disks_fill() -> None:
    """Asked once the tree is synced: otherwise the run dies at the packages
    stage, hours in and with the disks already written."""
    recorder = Recorder(failures={"emerge"})
    check = portage.VerifyPackages(packages=("app-editors/neovim", "not/real"))
    with pytest.raises(ConfigError, match="no ebuild matches"):
        check.apply(recorder)

    portage.VerifyPackages(packages=("app-editors/neovim",)).apply(Recorder())
