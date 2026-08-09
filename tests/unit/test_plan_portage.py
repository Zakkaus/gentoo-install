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
    # The gentoo-zh host specifically: the official one is trusted by getuto,
    # which runs before either of them.
    added = next(
        n
        for n, operation in enumerate(operations)
        if "binary package host gentoo-zh" in operation.describe()
    )
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


def test_a_package_built_here_still_takes_its_dependencies_from_the_host() -> None:
    """`binary_packages=False` means this atom carries a flag the host's build
    lacks, not that the machine has to compile everything under it: turning
    binaries off wholesale pulled gtk+, cups and 21 more into a systemd
    rebuild and died on a circular dependency between docutils and pillow."""
    recorder = Recorder()
    portage.PrepareBinhostTrust().apply(recorder)
    portage.Emerge(
        packages=("sys-apps/systemd",),
        summary="rebuild systemd with the unlock generator",
        binary_packages=False,
    ).apply(recorder)
    emerge = next(argv for argv in recorder.in_target if argv[0] == "emerge")
    assert "--getbinpkg=y" in emerge
    assert "--usepkg=n" not in emerge
    excluded = emerge[emerge.index("--usepkg-exclude") + 1]
    assert "sys-apps/systemd" in excluded.split()
    # The standing exclusions stay: one option, one value, and a second
    # `--usepkg-exclude` would replace this list rather than extend it.
    assert "virtual/*" in excluded.split()


def test_a_degraded_binhost_reaches_the_source_path_at_all() -> None:
    """`FEATURES=getbinpkg` in make.conf outlives `--usepkg=n`, so a host that
    cannot be verified still served every package until both were passed."""
    recorder = Recorder()
    recorder.given_up.add(portage.BINARY_PACKAGES)
    portage.Emerge(packages=("sys-boot/grub",), summary="install the bootloader").apply(recorder)
    emerge = next(argv for argv in recorder.in_target if argv[0] == "emerge")
    assert "--usepkg=n" in emerge and "--getbinpkg=n" in emerge


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


#: Captured from `emerge --pretend --quiet --nodeps` on a synced tree. Invented
#: output is what let a substring test over the whole text stand for years.
NO_SUCH_PACKAGE = (
    '\nemerge: there are no ebuilds to satisfy "app-misc/not-a-real-package".\n'
    "\nemerge: searching for similar names... nothing similar found.\n"
)
LICENCE_REFUSED = (
    '\n!!! All ebuilds that could satisfy "x11-drivers/nvidia-drivers" have been masked.\n'
    "!!! One of the following masked packages is required to complete your request:\n"
    "- x11-drivers/nvidia-drivers-610.57.04::gentoo (masked by: NVIDIA-2025 license(s))\n"
    "A copy of the 'NVIDIA-2025' license is located at "
    "'/var/db/repos/gentoo/licenses/NVIDIA-2025'.\n"
)


def test_a_package_name_that_matches_nothing_stops_before_the_disks_fill() -> None:
    """Asked once the tree is synced: otherwise the run dies at the packages
    stage, hours in and with the disks already written."""
    recorder = Recorder()
    recorder.replies["emerge"] = NO_SUCH_PACKAGE
    with pytest.raises(ConfigError, match="no ebuild matches"):
        portage.VerifyPackages(packages=("app-misc/not-a-real-package",)).apply(recorder)

    portage.VerifyPackages(packages=("app-editors/neovim",)).apply(Recorder())


def test_a_package_the_licence_refuses_is_named_as_that_and_not_as_a_typo() -> None:
    """The two have different answers: one is a name to correct, the other is
    ACCEPT_LICENSE to widen. Reporting both as "no ebuild matches" sends the
    operator hunting for a spelling mistake that is not there."""
    recorder = Recorder()
    recorder.replies["emerge"] = LICENCE_REFUSED
    with pytest.raises(ConfigError, match="ACCEPT_LICENSE refuses"):
        portage.VerifyPackages(packages=("x11-drivers/nvidia-drivers",)).apply(recorder)


def test_the_word_license_in_a_path_is_not_a_licence_refusal() -> None:
    """`license` appears in the licences directory Portage prints, in package
    names, and in `ACCEPT_LICENSE` itself. Matching it anywhere in the output
    reported a missing package as a licence the operator had refused."""
    recorder = Recorder()
    recorder.replies["emerge"] = (
        "\n[ebuild  N     ] app-misc/license-tools-1.0::gentoo\n"
        "A copy of the 'GPL-2' license is located at /usr/portage/licenses/GPL-2.\n"
    )
    portage.VerifyPackages(packages=("app-misc/license-tools",)).apply(recorder)


def test_one_probe_per_package_and_not_two() -> None:
    """It ran `emerge --pretend` twice for every atom that resolved, doubling
    the cost of the check on a long list."""
    recorder = Recorder()
    portage.VerifyPackages(packages=("app-editors/neovim", "app-misc/tmux")).apply(recorder)
    assert len([argv for argv in recorder.in_target if argv[0] == "emerge"]) == 2


def test_the_licence_choice_is_not_undone_by_autounmask() -> None:
    """`--autounmask-license=y` with `--write` and `--continue` writes the
    acceptance itself, which makes @FREE a suggestion rather than a refusal."""
    assert "--autounmask-license=y" not in portage.EMERGE_OPTIONS
    assert "--autounmask-use=y" in portage.EMERGE_OPTIONS


def test_the_stage3_make_conf_is_edited_and_not_replaced() -> None:
    """It ships comments and a CHOST nobody should be rewriting, and its
    COMMON_FLAGS is what Gentoo built the binary packages against."""
    stage3 = (
        "# These settings were set by the catalyst build script.\n"
        'COMMON_FLAGS="-O2 -pipe"\n'
        'CFLAGS="${COMMON_FLAGS}"\n'
        'CHOST="x86_64-pc-linux-gnu"\n'
    )
    merged = portage.merge(stage3, [("MAKEOPTS", "-j8"), ("CPU_FLAGS_X86", "aes")])
    assert "# These settings were set by the catalyst build script." in merged
    assert 'CHOST="x86_64-pc-linux-gnu"' in merged
    assert 'COMMON_FLAGS="-O2 -pipe"' in merged
    assert 'MAKEOPTS="-j8"' in merged


def test_a_key_the_file_already_has_is_replaced_in_place() -> None:
    merged = portage.merge('MAKEOPTS="-j1"\n', [("MAKEOPTS", "-j8")])
    assert merged.count("MAKEOPTS") == 1
    assert 'MAKEOPTS="-j8"' in merged


def test_common_flags_are_left_alone_unless_they_were_changed() -> None:
    """The default is the stage3's own value, so writing it would only add a
    line that says what the file already said."""
    keys = [key for key, _ in portage.make_conf(config())]
    assert "COMMON_FLAGS" not in keys
    changed = replace(config(), portage=replace(config().portage, common_flags="-O3 -pipe"))
    assert "COMMON_FLAGS" in [key for key, _ in portage.make_conf(changed)]


def test_global_testing_takes_the_unstable_binary_host_whatever_the_row_says() -> None:
    """The stable set is built against the main tree's `amd64`, so Portage
    refuses every package of it once `~amd64` is in force; the two rows were
    settable apart and the install fetched from a path with nothing usable."""
    from gentoo_install.model.config import BinhostChannel, GentooZhMirror, Keywords
    from gentoo_install.plan.portage import community_binhost

    base = replace(
        config().portage,
        mirrors=replace(config().portage.mirrors, gentoo_zh=GentooZhMirror.CERNET),
    )
    stable = replace(
        base, keywords=Keywords.STABLE,
        binhost=replace(base.binhost, community=BinhostChannel.STABLE),
    )
    assert community_binhost(stable).endswith("/gentoo-zh/binpkgs/x86-64")

    for channel in (BinhostChannel.STABLE, BinhostChannel.UNSTABLE):
        testing = replace(
            base, keywords=Keywords.TESTING,
            binhost=replace(base.binhost, community=channel),
        )
        assert community_binhost(testing).endswith("/gentoo-zh/unstable/binpkgs/x86-64"), channel


def test_the_menu_starts_on_this_machine_s_subarchitecture_and_a_mirror() -> None:
    """`x86-64-v3` needs AVX2, so the row is offered only where it runs and
    chosen where it does; the mirror row is required and started unanswered."""
    from gentoo_install.cli import _blank
    from gentoo_install.model.config import MirrorRegion
    from gentoo_install.model.mirrors import gentoo_sites

    assert _blank("/dev/vda", 4, (), supports_v3=True).portage.binhost.subarch == "x86-64-v3"
    plain = _blank("/dev/vda", 4, (), supports_v3=False)
    assert plain.portage.binhost.subarch == "x86-64"
    assert plain.portage.mirrors.site == gentoo_sites(MirrorRegion.GLOBAL)[0].key

    # The region follows where the packets come out, not the language.
    inside = _blank("/dev/vda", 4, (), country="CN")
    assert inside.portage.mirrors.region is MirrorRegion.CN
    assert inside.portage.mirrors.site == gentoo_sites(MirrorRegion.CN)[0].key
    for outside in ("TW", "SG", "US", ""):
        assert _blank("/dev/vda", 4, (), country=outside).portage.mirrors.region is (
            MirrorRegion.GLOBAL
        ), outside


def test_the_keyring_exists_before_the_first_binary_package_is_fetched() -> None:
    """`make.conf` carries `FEATURES=getbinpkg`, so the first emerge already
    fetches binary packages. With `getuto` after it, a profile carrying
    `binpkg-request-signature` refuses the merge, and one without it installs a
    package nothing verified."""
    from gentoo_install.data import load_catalog
    from gentoo_install.plan.build import build as build_plan

    described = [one.describe() for one in build_plan(config(), load_catalog())]
    keyring = next(n for n, one in enumerate(described) if "getuto" in one)
    host = next(n for n, one in enumerate(described) if "binary package host" in one)
    first = next(n for n, one in enumerate(described) if one.startswith("install git"))
    written = next(n for n, one in enumerate(described) if "make.conf" in one)
    assert written < keyring < first, described[written : first + 1]
    assert host < first, described[host : first + 1]
