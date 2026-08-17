# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import subprocess

import pytest

from dataclasses import fields, replace
from pathlib import PurePosixPath
from typing import Any, Final, Sequence

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
    PackagesConfig,
    PortageConfig,
    ProxyConfig,
    ProxyKind,
    Sync,
    SystemConfig,
)
from gentoo_install.errors import CommandFailed, ConfigError, ValidationFailed
from gentoo_install.plan import portage
from gentoo_install.plan.build import PORTAGE_PREREQUISITES, build as build_plan
from gentoo_install.plan.operations import CommandOutput, Operation, Stage
from gentoo_install.plan.packages import Group

from .layouts import config
from .recorder import Recorder

MIRROR = "https://distfiles.gentoo.org"
PROFILE = "default/linux/amd64/23.0/systemd"
PROFILE_LIST = f"""Available profile symlink targets:
  [1]   default/linux/amd64/23.0 (stable)
  [2]   {PROFILE} (stable) *
"""


def apply_all(installation: InstallConfig) -> Recorder:
    recorder = Recorder()
    recorder.answering = lambda argv: (
        CommandOutput("", 0)
        if tuple(argv[:3]) == ("portageq", "envvar", "PORTAGE_GPG_KEY_SERVER")
        else None
    )
    recorder.replies["eselect"] = PROFILE_LIST
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


def test_named_portage_fragments_share_one_path_and_writer() -> None:
    operation = portage.WritePortageConfig(
        kind=portage.PortageConfigKind.USE,
        name="example",
        lines=("app-misc/example foo",),
    )
    recorder = Recorder()
    operation.apply(recorder)
    assert operation.path == PurePosixPath("/etc/portage/package.use/example")
    assert recorder.files[operation.path] == "app-misc/example foo\n"


@pytest.mark.parametrize("kind", tuple(portage.PortageConfigKind))
def test_portage_config_kinds_use_the_same_typed_writer(
    kind: portage.PortageConfigKind,
) -> None:
    operation = portage.WritePortageConfig(kind=kind, name="example", lines=("one", "two"))
    recorder = Recorder()

    operation.apply(recorder)

    # The directory is named, not derived from `kind.value`: deriving both
    # sides from the same source cannot fail when a kind names the wrong file.
    directories = {
        portage.PortageConfigKind.USE: "package.use",
        portage.PortageConfigKind.KEYWORDS: "package.accept_keywords",
        portage.PortageConfigKind.LICENSE: "package.license",
        portage.PortageConfigKind.UNMASK: "package.unmask",
    }
    assert set(directories) == set(portage.PortageConfigKind)
    assert len(set(directories.values())) == len(directories)
    assert operation.path == PurePosixPath(f"/etc/portage/{directories[kind]}/example")
    assert recorder.files[operation.path] == "one\ntwo\n"


def test_the_chroot_gets_proc_rbinds_and_a_slave_run() -> None:
    recorder = Recorder()

    portage.MountChrootFilesystems().apply(recorder)

    assert recorder.commands == [
        ("mount", "--types", "proc", "/proc", "/mnt/gentoo/proc"),
        ("mount", "--rbind", "/sys", "/mnt/gentoo/sys"),
        ("mount", "--make-rslave", "/mnt/gentoo/sys"),
        ("mount", "--rbind", "/dev", "/mnt/gentoo/dev"),
        ("mount", "--make-rslave", "/mnt/gentoo/dev"),
        ("mount", "--bind", "/run", "/mnt/gentoo/run"),
        ("mount", "--make-slave", "/mnt/gentoo/run"),
    ]


def test_resolv_conf_is_copied_so_the_chroot_can_resolve_a_mirror() -> None:
    recorder = Recorder()

    portage.SeedResolver().apply(recorder)

    assert recorder.commands == [
        ("install", "--mode=0644", "/etc/resolv.conf", "/mnt/gentoo/etc/resolv.conf")
    ]


def test_chroot_mounts_and_resolver_seed_keep_their_stage_and_order() -> None:
    operations = portage.build(config(), MIRROR)
    mounted = next(
        n for n, operation in enumerate(operations)
        if isinstance(operation, portage.MountChrootFilesystems)
    )
    seeded = next(
        n for n, operation in enumerate(operations)
        if isinstance(operation, portage.SeedResolver)
    )
    assert seeded == mounted + 1
    assert operations[mounted].stage is Stage.CHROOT
    assert operations[seeded].stage is Stage.CHROOT


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


def test_proxy_clients_route_portage_and_keep_credentials_out_of_descriptions() -> None:
    secret = "s3cr3t"
    installation = replace(
        config(),
        proxy=ProxyConfig(
            kind=ProxyKind.HTTPS,
            host="proxy.example",
            port=8443,
            username="operator",
            password=secret,
            bypass=("localhost", "intranet.example"),
        ),
    )
    recorder = apply_all(installation)
    make_conf = recorder.files[PurePosixPath("/etc/portage/make.conf")]
    assert 'http_proxy="https://proxy.example:8443"' in make_conf
    assert 'RSYNC_PROXY="proxy.example:8443"' in make_conf
    assert 'no_proxy="localhost,intranet.example"' in make_conf
    assert secret not in make_conf
    assert secret in recorder.files[PurePosixPath("/etc/wgetrc")]
    assert secret in recorder.files[portage.CURL_PROXY_CONFIG]
    # git talks to the proxy itself, and the file is `0600` like the others.
    assert secret in recorder.files[PurePosixPath("/etc/gitconfig")]
    # dirmngr.conf too, but written after `getuto` rebuilds the directory: the
    # endpoint in the environment carries no credential, so a proxy that wants
    # one answered `keyserver refresh failed: Not authenticated`.
    assert secret in recorder.files[PurePosixPath("/etc/portage/gnupg/dirmngr.conf")]
    assert all(secret not in operation.describe() for operation in portage.build(installation, MIRROR))


def test_socks_proxy_uses_curl_for_portage_and_leaves_wget_and_dirmngr_direct() -> None:
    installation = replace(
        config(), proxy=ProxyConfig(kind=ProxyKind.SOCKS5, host="proxy.example", port=1080, username="installer", password="secret")
    )

    recorder = apply_all(installation)
    make_conf = recorder.files[PurePosixPath("/etc/portage/make.conf")]

    assert 'FETCHCOMMAND="curl ' in make_conf
    assert 'RESUMECOMMAND="curl ' in make_conf
    assert f'FETCHCOMMAND="curl -K {portage.CURL_PROXY_CONFIG}' in make_conf
    assert f'RESUMECOMMAND="curl -K {portage.CURL_PROXY_CONFIG}' in make_conf
    assert "wget" not in make_conf.split('FETCHCOMMAND="', 1)[1].split('"', 1)[0]
    assert "RSYNC_PROXY" not in make_conf
    assert "proxy = \"socks5h://proxy.example:1080\"" in recorder.files[portage.CURL_PROXY_CONFIG]
    assert 'proxy-user = "installer:secret"' in recorder.files[portage.CURL_PROXY_CONFIG]
    assert "installer:secret" not in make_conf
    assert "installer:secret" not in portage.CURL_FETCHCOMMAND
    assert "installer:secret" not in portage.CURL_RESUMECOMMAND
    assert recorder.files[PurePosixPath("/etc/wgetrc")] == "use_proxy = off\n"
    # `getuto` rebuilds /etc/portage/gnupg from a staging directory after this
    # runs, so nothing written there survives; its own dirmngr.conf carries
    # `honor-http-proxy`, and a SOCKS URL in `http_proxy` made the tree's
    # signature check answer `Invalid URI`.
    assert PurePosixPath("/etc/portage/gnupg/dirmngr.conf") not in recorder.files
    assert "http_proxy" not in make_conf
    assert "https_proxy" not in make_conf
    assert "ftp_proxy" not in make_conf
    assert 'all_proxy="socks5h://proxy.example:1080"' in make_conf


def test_http_proxy_keeps_wget_for_portage_and_configures_wget_and_dirmngr() -> None:
    installation = replace(config(), proxy=ProxyConfig(kind=ProxyKind.HTTP, host="proxy.example", port=8080, username="installer", password="secret"))

    recorder = apply_all(installation)
    make_conf = recorder.files[PurePosixPath("/etc/portage/make.conf")]

    assert 'FETCHCOMMAND="wget ' in make_conf
    assert 'RESUMECOMMAND="wget ' in make_conf
    assert "http_proxy = http://proxy.example:8080" in recorder.files[PurePosixPath("/etc/wgetrc")]
    assert 'http_proxy="http://proxy.example:8080"' in make_conf
    written = recorder.files[PurePosixPath("/etc/portage/gnupg/dirmngr.conf")]
    assert "honor-http-proxy" in written
    assert "http-proxy http://installer:secret@proxy.example:8080" in written


def test_no_proxy_leaves_every_client_as_the_distribution_shipped_it() -> None:
    """Writing an empty proxy is not the same as writing nothing.
    `FETCHCOMMAND` reached `make.conf` on a machine with no proxy, and
    `emerge-webrsync` runs it for a snapshot whose URL it supplies itself, so
    wget answered `missing URL` and the install stopped with no tree.
    """
    recorder = apply_all(config())
    make_conf = recorder.files[PurePosixPath("/etc/portage/make.conf")]
    for name in ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy",
                 "FETCHCOMMAND", "RESUMECOMMAND", "RSYNC_PROXY"):
        assert name not in make_conf
    for path in ("/etc/wgetrc", "/etc/gitconfig"):
        assert PurePosixPath(path) not in recorder.files
    assert portage.CURL_PROXY_CONFIG not in recorder.files


def test_l10n_is_derived_from_the_locales_rather_than_listed_twice() -> None:
    installation = replace(
        config(), system=SystemConfig(locales=("en_US.UTF-8", "zh_CN.UTF-8", "zh_TW.UTF-8"))
    )
    written = apply_all(installation).files[PurePosixPath("/etc/portage/make.conf")]
    assert 'L10N="en-US zh-CN zh-TW"' in written


def test_an_l10n_override_reaches_make_conf_without_changing_the_locales() -> None:
    locales = ("en_US.UTF-8", "zh_TW.UTF-8")
    installation = replace(
        config(),
        system=SystemConfig(locales=locales),
        portage=replace(PortageConfig(), l10n=("en", "qaa", "zh-TW")),
    )

    written = apply_all(installation).files[PurePosixPath("/etc/portage/make.conf")]

    assert 'L10N="en qaa zh-TW"' in written
    assert installation.system.locales == locales


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


def test_a_custom_rsync_uri_is_written_to_the_main_repository() -> None:
    sync_uri = "rsync://mirror.example.invalid/gentoo-portage"
    installation = with_portage(
        sync=Sync.RSYNC,
        mirrors=MirrorConfig(repo_sync_uri=sync_uri),
    )

    stanza = apply_all(installation).files[PurePosixPath("/etc/portage/repos.conf/gentoo.conf")]

    assert stanza == f"""[gentoo]
location = /var/db/repos/gentoo
sync-type = rsync
sync-uri = {sync_uri}
auto-sync = yes
"""


def test_an_empty_rsync_uri_uses_the_default_rsync_repository() -> None:
    installation = with_portage(sync=Sync.RSYNC, mirrors=MirrorConfig())

    stanza = apply_all(installation).files[PurePosixPath("/etc/portage/repos.conf/gentoo.conf")]

    assert "sync-type = rsync" in stanza
    assert "sync-uri = rsync://rsync.gentoo.org/gentoo-portage" in stanza


def test_a_custom_git_uri_is_written_to_the_main_repository() -> None:
    sync_uri = "https://git.example.invalid/gentoo.git"
    installation = with_portage(
        sync=Sync.GIT,
        mirrors=MirrorConfig(repo_sync_uri=sync_uri),
    )

    stanza = apply_all(installation).files[PurePosixPath("/etc/portage/repos.conf/gentoo.conf")]

    assert stanza == f"""[gentoo]
location = /var/db/repos/gentoo
sync-type = git
sync-uri = {sync_uri}
sync-depth = 1
auto-sync = yes
sync-git-verify-commit-signature = true
sync-openpgp-key-path = /usr/share/openpgp-keys/gentoo-release.asc
"""


def test_webrsync_is_persisted_without_a_repository_uri() -> None:
    installation = with_portage(
        sync=Sync.WEBRSYNC,
        mirrors=MirrorConfig(repo_sync_uri="https://unused.example.invalid/gentoo.git"),
    )
    operations = portage.build(installation, MIRROR)

    assert sum(isinstance(operation, portage.WebrsyncRepository) for operation in operations) == 1
    stanza = apply_all(installation).files[PurePosixPath("/etc/portage/repos.conf/gentoo.conf")]
    assert stanza == """[gentoo]
location = /var/db/repos/gentoo
sync-type = webrsync
auto-sync = yes
sync-webrsync-verify-signature = true
sync-openpgp-key-path = /usr/share/openpgp-keys/gentoo-release.asc
"""


def test_the_local_copy_goes_before_the_first_sync_or_git_refuses() -> None:
    recorder = apply_all(config())
    removed = recorder.in_target.index(("rm", "--recursive", "--force", "/var/db/repos/gentoo"))
    synced = next(n for n, one in enumerate(recorder.in_target) if "--sync" in one)
    assert removed < synced


def test_the_first_tree_arrives_by_webrsync_because_stage3_has_no_git() -> None:
    operations = portage.build(config(), MIRROR)
    described = [operation.describe() for operation in operations]
    webrsync = next(n for n, text in enumerate(described) if "emerge-webrsync" in text)
    profile = next(n for n, operation in enumerate(operations) if isinstance(operation, portage.SelectProfile))
    git = next(n for n, text in enumerate(described) if "dev-vcs/git" in text)
    git_sync = next(n for n, text in enumerate(described) if text == "sync repository gentoo")
    assert webrsync < profile < git < git_sync


def test_a_profile_absent_from_the_target_tree_stops_before_it_is_set() -> None:
    recorder = Recorder()
    recorder.answering = lambda argv: CommandOutput(PROFILE_LIST, 0) if argv[-1] == "list" else None

    with pytest.raises(ValidationFailed, match=r"configured profile 'absent'.*eselect profile list"):
        portage.SelectProfile(profile="absent").apply(recorder)

    assert recorder.in_target == [("eselect", "profile", "list")]


def test_a_profile_present_in_the_target_tree_is_set() -> None:
    recorder = Recorder()
    recorder.answering = lambda argv: CommandOutput(PROFILE_LIST, 0) if argv[-1] == "list" else None

    portage.SelectProfile(profile=PROFILE).apply(recorder)

    assert recorder.in_target == [
        ("eselect", "profile", "list"),
        ("eselect", "profile", "set", PROFILE),
    ]


def test_an_unreadable_target_profile_list_is_reported_as_unreadable() -> None:
    recorder = Recorder()
    recorder.answering = lambda argv: CommandOutput("eselect failed", 1)

    with pytest.raises(CommandFailed, match=r"profile list.*target.*could not be read"):
        portage.SelectProfile(profile=PROFILE).apply(recorder)

    assert recorder.in_target == [("eselect", "profile", "list")]


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
    """A build-all policy means this atom carries a flag the host's build
    lacks, not that the machine has to compile everything under it: turning
    binaries off wholesale pulled gtk+, cups and 21 more into a systemd
    rebuild and died on a circular dependency between docutils and pillow."""
    recorder = Recorder()
    portage.PrepareBinhostTrust().apply(recorder)
    portage.Emerge(
        packages=("sys-apps/systemd",),
        summary="rebuild systemd with the unlock generator",
        source=portage.SourcePolicy.build_all(),
    ).apply(recorder)
    emerge = next(argv for argv in recorder.in_target if argv[0] == "emerge")
    assert "--getbinpkg=y" in emerge
    assert "--usepkg=n" not in emerge
    excluded = emerge[emerge.index("--usepkg-exclude") + 1]
    assert "sys-apps/systemd" in excluded.split()
    # The standing exclusions stay: one option, one value, and a second
    # `--usepkg-exclude` would replace this list rather than extend it.
    assert "virtual/*" in excluded.split()


@pytest.mark.parametrize(
    ("policy", "built", "description"),
    (
        (
            portage.SourcePolicy.binaries_allowed(),
            (),
            "install packages: emerge app-editors/nano app-misc/tmux",
        ),
        (
            portage.SourcePolicy.build_all(),
            ("app-editors/nano", "app-misc/tmux"),
            "install packages: emerge app-editors/nano app-misc/tmux, from source",
        ),
        (
            portage.SourcePolicy.build_subset(("app-misc/tmux",)),
            ("app-misc/tmux",),
            (
                "install packages: emerge app-editors/nano app-misc/tmux, "
                "building app-misc/tmux here"
            ),
        ),
    ),
    ids=("binaries-allowed", "build-all", "build-subset"),
)
def test_source_policies_render_their_commands_and_descriptions(
    policy: portage.SourcePolicy,
    built: tuple[str, ...],
    description: str,
) -> None:
    operation = portage.Emerge(
        packages=("app-editors/nano", "app-misc/tmux"),
        summary="install packages",
        source=policy,
    )
    recorder = Recorder()

    operation.apply(recorder)

    emerge = recorder.only("emerge")
    excluded = emerge[emerge.index("--usepkg-exclude") + 1]
    expected = portage.BINPKG_EXCLUDED
    if built:
        expected += " " + " ".join(portage._unversioned(atom) for atom in built)
    assert excluded == expected
    assert operation.describe() == description


@pytest.mark.parametrize(
    ("packages", "subset"),
    (
        (("app-editors/nano",), ()),
        (("app-editors/nano",), ("app-misc/tmux",)),
        (("app-editors/nano",), ("app-editors/nano", "app-misc/tmux")),
    ),
    ids=("empty", "outside", "mixed"),
)
def test_invalid_source_subsets_are_rejected(
    packages: tuple[str, ...], subset: tuple[str, ...]
) -> None:
    if not subset:
        with pytest.raises(ValueError, match="source subset cannot be empty"):
            portage.SourcePolicy.build_subset(subset)
        return
    with pytest.raises(ValueError, match="source subset contains atoms outside packages"):
        portage.Emerge(
            packages=packages,
            summary="install packages",
            source=portage.SourcePolicy.build_subset(subset),
        )


def test_an_emerge_has_one_typed_install_mode() -> None:
    operation = portage.Emerge(
        packages=("app-editors/nano",),
        summary="install the editor",
    )

    assert operation.mode is portage.InstallMode.NORMAL
    assert {"oneshot", "noreplace"}.isdisjoint(field.name for field in fields(portage.Emerge))


@pytest.mark.parametrize(
    ("mode", "mode_options"),
    (
        (portage.InstallMode.NORMAL, ()),
        (portage.InstallMode.ONESHOT, ("--oneshot",)),
        (portage.InstallMode.NOREPLACE, ("--noreplace",)),
    ),
    ids=("normal", "oneshot", "noreplace"),
)
def test_install_modes_map_to_exact_emerge_argv(
    mode: portage.InstallMode, mode_options: tuple[str, ...]
) -> None:
    recorder = Recorder()
    portage.Emerge(
        packages=("app-editors/nano",),
        summary="install the editor",
        mode=mode,
    ).apply(recorder)
    assert recorder.only("emerge") == (
        "emerge",
        *portage.EMERGE_OPTIONS,
        *mode_options,
        *portage.BINPKG_OPTIONS,
        "--",
        "app-editors/nano",
    )


@pytest.mark.parametrize(
    "policy",
    (
        portage.SourcePolicy.binaries_allowed(),
        portage.SourcePolicy.build_all(),
        portage.SourcePolicy.build_subset(("sys-boot/grub",)),
    ),
    ids=("binaries-allowed", "build-all", "build-subset"),
)
def test_a_degraded_binhost_reaches_the_source_path_at_all(
    policy: portage.SourcePolicy,
) -> None:
    """`FEATURES=getbinpkg` in make.conf outlives `--usepkg=n`, so a host that
    cannot be verified still served every package until both were passed."""
    recorder = Recorder()
    recorder.given_up.add(portage.BINARY_PACKAGES)
    portage.Emerge(
        packages=("sys-boot/grub",),
        summary="install the bootloader",
        source=policy,
    ).apply(recorder)
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
    assert PurePosixPath("/etc/portage/binrepos.conf/gentoobinhost.conf") in written

    portage.Emerge(packages=("sys-boot/grub",), summary="install the bootloader").apply(recorder)
    assert "--getbinpkg=y" in next(argv for argv in recorder.in_target if argv[0] == "emerge")


def test_a_selected_binary_fetch_failure_retries_from_source() -> None:
    recorder = Recorder()
    recorder.answering = lambda argv: (
        CommandOutput("Fetching Binary failed: mirror refused the connection", 1)
        if argv[0] == "emerge" and "--usepkg=n" not in argv
        else CommandOutput("[ebuild] app-editors/nano-8", 0)
    )

    portage.Emerge(
        packages=("app-editors/nano",), summary="install the editor"
    ).apply(recorder)

    emerges = [argv for argv in recorder.in_target if argv[0] == "emerge"]
    # Binaries, binaries again because one dropped handshake is not the host
    # being gone, then source.
    assert len(emerges) == 3
    assert "--getbinpkg=n" in emerges[-1] and "--usepkg=n" in emerges[-1]
    assert "--getbinpkg=n" not in emerges[1]
    assert recorder.degraded(portage.BINARY_PACKAGES)


def test_an_unrelated_emerge_failure_is_not_misclassified_as_a_binhost_failure() -> None:
    recorder = Recorder()
    recorder.answering = lambda argv: CommandOutput("compile failed", 1)

    with pytest.raises(CommandFailed, match="emerge ended with exit 1"):
        portage.Emerge(
            packages=("app-editors/nano",), summary="install the editor"
        ).apply(recorder)

    assert not recorder.degraded(portage.BINARY_PACKAGES)


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
PACKAGE_MASKED = (
    '\n!!! All ebuilds that could satisfy "app-text/catdoc" have been masked.\n'
    "!!! One of the following masked packages is required to complete your request:\n"
    "- app-text/catdoc-0.95-r1::gentoo (masked by: package.mask)\n"
)


def test_an_absent_catalog_atom_stops_the_run_and_names_its_group() -> None:
    wanted = replace(
        config(),
        packages=PackagesConfig(applications=("plasma",)),
    )
    operations = build_plan(
        wanted,
        {"plasma": Group(name="plasma", packages=("kde-plasma/foo",))},
    )
    check = next(one for one in operations if isinstance(one, portage.VerifyPackages))
    recorder = Recorder()

    def answer(argv: Sequence[str]) -> str | None:
        if argv[0] == "emerge":
            return CommandOutput(NO_SUCH_PACKAGE, 1)
        if argv[:4] == ["portageq", "pquery", "--no-version", "--no-filters"]:
            return CommandOutput("" if argv[-1] == "kde-plasma/foo" else f"{argv[-1]}\n", 0)
        if argv[:3] == ["portageq", "best_visible", "/"]:
            return CommandOutput(f"{argv[-1]}-1\n", 0)
        return None

    recorder.answering = answer
    with pytest.raises(
        ConfigError,
        match=r"the `plasma` group asks for `kde-plasma/foo`, which the target's repositories do not carry",
    ):
        check.apply(recorder)


def test_every_requested_atom_is_resolved_in_one_pass_and_the_run_continues() -> None:
    wanted = replace(
        config(),
        packages=PackagesConfig(applications=("plasma",)),
    )
    operations = build_plan(
        wanted,
        {"plasma": Group(name="plasma", packages=("kde-plasma/plasma-meta",))},
    )
    check = next(one for one in operations if isinstance(one, portage.VerifyPackages))
    recorder = Recorder(replies={"emerge": CommandOutput("", 0)})

    check.apply(recorder)

    pretend = recorder.only("emerge", "--pretend", "--quiet")
    atoms = pretend[pretend.index("--") + 1 :]
    assert "kde-plasma/plasma-meta" in atoms
    assert "sys-kernel/gentoo-kernel" in atoms
    assert "sys-boot/grub" in atoms
    assert "dev-vcs/git" not in atoms
    assert len(atoms) == len(set(atoms))
    assert len(recorder.argv_starting("emerge")) == 1
    assert not recorder.argv_starting("portageq")


def test_the_resolution_check_follows_repository_bootstrap_and_precedes_requested_merges() -> None:
    wanted = replace(
        config(),
        portage=replace(
            config().portage,
            overlays=(Overlay(name="local", sync_uri="https://example.invalid/local.git"),),
        ),
        packages=PackagesConfig(applications=("plasma",)),
    )
    operations = build_plan(
        wanted,
        {"plasma": Group(name="plasma", packages=("kde-plasma/plasma-meta",))},
    )
    check = next(n for n, one in enumerate(operations) if isinstance(one, portage.VerifyPackages))
    bootstrap = [
        n
        for n, one in enumerate(operations)
        if isinstance(one, portage.Emerge) and one.repository_bootstrap
    ]
    requested = [
        n
        for n, one in enumerate(operations)
        if isinstance(one, portage.Emerge) and not one.repository_bootstrap
    ]

    assert bootstrap and requested
    assert max(bootstrap) < check < min(requested)
    assert next(
        n for n, one in enumerate(operations) if isinstance(one, portage.AcceptOverlayKeywords)
    ) < check
    assert all(
        n < check
        for n, one in enumerate(operations)
        if isinstance(one, PORTAGE_PREREQUISITES)
    )


def test_a_package_name_that_matches_nothing_stops_before_the_disks_fill() -> None:
    """Asked once the tree is synced: otherwise the run dies at the packages
    stage, hours in and with the disks already written."""
    recorder = Recorder(
        replies={"emerge": CommandOutput(NO_SUCH_PACKAGE, 1), "portageq": CommandOutput("", 0)}
    )
    check = portage.VerifyPackages(
        requests=(
            portage.PackageRequest(
                atom="app-misc/not-a-real-package", requesters=("the extra packages group",)
            ),
        )
    )
    with pytest.raises(ConfigError, match="repositories do not carry"):
        check.apply(recorder)


def test_a_package_the_licence_refuses_is_named_as_that_and_not_as_a_typo() -> None:
    """The two have different answers: one is a name to correct, the other is
    ACCEPT_LICENSE to widen. Reporting both as "no ebuild matches" sends the
    operator hunting for a spelling mistake that is not there."""
    recorder = Recorder()

    def answer(argv: Sequence[str]) -> str | None:
        if argv[0] == "emerge":
            return CommandOutput(LICENCE_REFUSED, 1)
        if argv[1] == "pquery":
            return CommandOutput("x11-drivers/nvidia-drivers\n", 0)
        return CommandOutput("", 1)

    recorder.answering = answer
    check = portage.VerifyPackages(
        requests=(
            portage.PackageRequest(
                atom="x11-drivers/nvidia-drivers", requesters=("the `nvidia` group",)
            ),
        )
    )
    with pytest.raises(ConfigError, match="configuration masks") as caught:
        check.apply(recorder)
    assert "repositories do not carry" not in str(caught.value)


#: What Portage printed when the official binary host timed out during
#: `emerge --pretend`, verbatim from `vm-gnome`'s log.
BINHOST_TIMED_OUT: Final[str] = (
    "!!! [gentoo] Error fetching binhost package info from "
    "'https://mirrors.nju.edu.cn/gentoo/releases/amd64/binpackages/23.0/x86-64'\n"
    "!!! [gentoo] <urlopen error timed out>\n"
    "[ebuild  N    ] dev-perl/Error-0.170.300-r1\n"
)


def test_a_binary_host_that_cannot_be_read_degrades_the_resolve_to_source() -> None:
    """`vm-gnome` stopped six minutes in, before anything was merged, with
    every requested package present and visible: `--pretend` fails on an index
    it cannot fetch. Source is the guaranteed path, so the host is dropped and
    the install continues."""
    recorder = Recorder()
    attempts: list[tuple[str, ...]] = []

    def answer(argv: Sequence[str]) -> str | None:
        if argv[0] != "emerge":
            return CommandOutput("", 0)
        attempts.append(tuple(argv))
        # Off only when the resolve stops asking the host for anything.
        if "--getbinpkg=n" in argv:
            return CommandOutput("[ebuild  N    ] gnome-base/gnome-3.56\n", 0)
        return CommandOutput(BINHOST_TIMED_OUT, 1)

    recorder.answering = answer
    check = portage.VerifyPackages(
        requests=(
            portage.PackageRequest(atom="gnome-base/gnome", requesters=("the `gnome` group",)),
        )
    )
    check.apply(recorder)

    assert recorder.degraded(portage.BINARY_PACKAGES)
    # One retry with binaries still on before the whole install is committed to
    # compiling, the same as `Emerge` makes.
    assert [("--getbinpkg=n" in one) for one in attempts] == [False, False, True], attempts

    # Negative control: the same unreadable index when binaries are already
    # gone is a real rejection, not another degradation.
    already = Recorder()
    already.given_up.add(portage.BINARY_PACKAGES)
    tried: list[tuple[str, ...]] = []

    def refuse(argv: Sequence[str]) -> str | None:
        if argv[0] == "emerge":
            tried.append(tuple(argv))
            return CommandOutput(BINHOST_TIMED_OUT, 1)
        if argv[1] == "pquery":
            return CommandOutput("gnome-base/gnome\n", 0)
        return CommandOutput("gnome-base/gnome-3.56\n", 0)

    already.answering = refuse
    with pytest.raises(ConfigError, match="Error fetching binhost package info"):
        check.apply(already)
    assert len(tried) == 1, tried


def test_a_rejection_that_is_not_the_binary_host_is_never_degraded() -> None:
    """The negative control the degradation rests on: a masked package answers
    with a rejection whatever the binary host is doing, because dropping the
    host would not make it installable and would compile the whole system."""
    recorder = Recorder()

    def answer(argv: Sequence[str]) -> str | None:
        if argv[0] == "emerge":
            return CommandOutput(PACKAGE_MASKED, 1)
        if argv[1] == "pquery":
            return CommandOutput("app-text/catdoc\n", 0)
        return CommandOutput("", 1)

    recorder.answering = answer
    check = portage.VerifyPackages(
        requests=(
            portage.PackageRequest(atom="app-text/catdoc", requesters=("the `catdoc` group",)),
        )
    )
    with pytest.raises(ConfigError, match="configuration masks"):
        check.apply(recorder)
    assert not recorder.degraded(portage.BINARY_PACKAGES)


def test_an_unclassified_nonzero_package_probe_is_rejected() -> None:
    recorder = Recorder()

    def answer(argv: Sequence[str]) -> str | None:
        if argv[0] == "emerge":
            return CommandOutput(PACKAGE_MASKED, 1)
        if argv[1] == "pquery":
            return CommandOutput("app-text/catdoc\n", 0)
        return CommandOutput("app-text/catdoc-0.95-r1\n", 0)

    recorder.answering = answer
    check = portage.VerifyPackages(
        requests=(
            portage.PackageRequest(atom="app-text/catdoc", requesters=("the `catdoc` group",)),
        )
    )
    with pytest.raises(
        ConfigError, match=r"requested by the `catdoc` group: All ebuilds.*masked"
    ):
        check.apply(recorder)


def test_the_word_license_in_a_path_is_not_a_licence_refusal() -> None:
    """`license` appears in the licences directory Portage prints, in package
    names, and in `ACCEPT_LICENSE` itself. Matching it anywhere in the output
    reported a missing package as a licence the operator had refused."""
    recorder = Recorder()
    recorder.replies["emerge"] = CommandOutput(
        "\n[ebuild  N     ] app-misc/license-tools-1.0::gentoo\n"
        "A copy of the 'GPL-2' license is located at /usr/portage/licenses/GPL-2.\n",
        0,
    )
    portage.VerifyPackages(
        requests=(
            portage.PackageRequest(
                atom="app-misc/license-tools", requesters=("the `tools` group",)
            ),
        )
    ).apply(recorder)


def test_one_probe_per_package_and_not_two() -> None:
    """The package set is one dependency-aware question, not one per atom."""
    recorder = Recorder(replies={"emerge": CommandOutput("", 0)})
    portage.VerifyPackages(
        requests=(
            portage.PackageRequest(atom="app-editors/neovim", requesters=("the editor group",)),
            portage.PackageRequest(atom="app-misc/tmux", requesters=("the console group",)),
        )
    ).apply(recorder)
    assert len([argv for argv in recorder.in_target if argv[0] == "emerge"]) == 1


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


def test_make_conf_values_survive_being_sourced_by_portage() -> None:
    installation = replace(
        config(),
        proxy=ProxyConfig(kind=ProxyKind.SOCKS5, host="proxy.example", port=1080, username="user", password="pass"),
        portage=replace(
            config().portage,
            makeopts='-j8 "quoted" $expanded \\literal',
        ),
    )
    written = apply_all(installation).files[PurePosixPath("/etc/portage/make.conf")]
    result = subprocess.run(
        ["bash", "-c", "source /dev/stdin; printf '%s' \"$MAKEOPTS\""],
        input=written,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout == '-j8 "quoted" $expanded \\literal'
    assert '\\"' in written
    assert "\\$" in written
    assert "\\\\" in written


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


def test_the_stage3_comes_from_the_mirror_the_operator_chose() -> None:
    """Only `--mirror` was read, so choosing USTC set the mirror for every
    later fetch and still downloaded the stage3 itself, several hundred
    megabytes of it, from `distfiles.gentoo.org`."""
    from dataclasses import replace

    from gentoo_install.data import load_catalog
    from gentoo_install.model.config import MirrorConfig, MirrorRegion
    from gentoo_install.plan.build import DEFAULT_MIRROR, build, stage3_mirror

    from .layouts import config

    chosen = replace(
        config(),
        portage=replace(
            config().portage, mirrors=MirrorConfig(region=MirrorRegion.CN, site="ustc")
        ),
    )
    assert stage3_mirror(chosen) == "https://mirrors.ustc.edu.cn/gentoo"
    described = " ".join(one.describe() for one in build(chosen, load_catalog()))
    assert "mirrors.ustc.edu.cn" in described
    assert "distfiles.gentoo.org" not in described

    # No site chosen: the region's first, which is what an empty `site` means.
    # Reading it as `no choice was made` sent every configuration that named
    # only a region back to Gentoo's own mirror, and a run from China fetched
    # the stage3 there with `cn` selected.
    region_only = replace(
        config(),
        portage=replace(config().portage, mirrors=MirrorConfig(region=MirrorRegion.CN)),
    )
    assert stage3_mirror(region_only) == "https://mirrors.ustc.edu.cn/gentoo"

    # Nothing chosen at all is still the official one, which is the region's
    # first as well as what the flag defaults to.
    assert stage3_mirror(config()) == DEFAULT_MIRROR


def test_a_mirror_without_releases_is_never_the_stage3_source() -> None:
    """`mirror.xtom.com.hk` answers 200 for `releases/amd64/autobuilds/` and
    404 for the pointer file inside it, and `ftp.twaren.net` answers 403 to
    this installer's downloader while serving a browser. An install told to
    use either one stopped with no stage3 at all."""
    from dataclasses import replace

    from gentoo_install.model import mirrors
    from gentoo_install.model.config import MirrorConfig, MirrorRegion
    from gentoo_install.plan.build import stage3_mirror

    from .layouts import config

    for key in ("xtom-hk", "nchc-tw"):
        site = next(one for one in mirrors.GENTOO_SITES if one.key == key)
        assert not site.releases, f"{key} is recorded as carrying releases"
        chosen = replace(
            config(),
            portage=replace(
                config().portage,
                mirrors=MirrorConfig(region=MirrorRegion.CN, site=key),
            ),
        )
        assert stage3_mirror(chosen) != site.distfiles
        assert stage3_mirror(chosen).startswith("https://"), key


def test_every_offered_region_resolves_a_stage3_source() -> None:
    """A region whose sites all lacked `releases/` would fall through to a
    fallback nobody chose."""
    from gentoo_install.model import mirrors
    from gentoo_install.model.config import MirrorRegion

    for region in MirrorRegion:
        carrying = [one for one in mirrors.gentoo_sites(region) if one.releases]
        assert carrying, region


def test_a_mirror_rewriting_its_manifests_is_retried_rather_than_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three of eight cluster guests stopped in the same minute with `Manifest
    mismatch for gui-apps/Manifest.gz __size__: expected: 5745, have: 5746`.
    Portage quarantined an incomplete snapshot and refused, which is right;
    stopping an install that had already partitioned the disks is not.
    """
    from typing import Sequence

    from gentoo_install.errors import CommandFailed
    from gentoo_install.plan import portage as plan_portage

    from .recorder import Recorder

    class Flaky(Recorder):
        """Refuses the first `refusals` syncs the way a mirror mid-update does."""

        refusals: int = 2
        attempts: int = 0

        def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> CommandOutput:
            if "--sync" in argv:
                self.attempts += 1
                if self.refusals:
                    self.refusals -= 1
                    raise CommandFailed("emerge --sync gentoo exited 1: Manifest mismatch")
            return super().run_in_target(argv, check=check)

    operation = plan_portage.SyncRepository(
        name="gentoo", location=PurePosixPath("/var/db/repos/gentoo")
    )
    monkeypatch.setattr(plan_portage, "SYNC_PAUSE", 0.0)
    recorder = Flaky()
    operation.apply(recorder)
    assert recorder.attempts == 3, recorder.attempts
    assert recorder.argv_starting("sleep") == (("sleep", "0"), ("sleep", "0"))
    synced = [one for one in recorder.in_target if "--sync" in one]
    assert len(synced) == 1, "the one that worked"

    # A mirror that queues rather than refuses sends nothing for longer than
    # portage's own 180, and rsync was killed while still in USTC's queue.
    assert tuple(synced[0]) == (
        "env",
        f"RSYNC_TIMEOUT={plan_portage.RSYNC_TIMEOUT}",
        "emerge",
        "--sync",
        "gentoo",
    )
    assert plan_portage.RSYNC_TIMEOUT > 180

    # Not walked around: a mismatch that keeps coming back stops the install.
    stubborn = Flaky()
    stubborn.refusals = plan_portage.SYNC_TRIES
    with pytest.raises(CommandFailed):
        operation.apply(stubborn)


#: What a cluster guest's git printed on 2026-08-16 when the overlay mirror was
#: not reachable, and what portage printed when a mirror was mid-update. The
#: two are told apart by their text, so both are held here verbatim.
_UNREACHABLE_SAYS: Final[str] = (
    "chroot /mnt/gentoo env RSYNC_TIMEOUT=900 emerge --sync gentoo-zh exited 1: "
    "fatal: unable to access 'https://mirror.nju.edu.cn/git/gentoo-zh.git/': "
    "Failed to connect to mirror.nju.edu.cn:443 after 2052 ms: "
    "Could not connect to server | !!! git clone error in /var/db/repos/gentoo-zh"
)
_MID_UPDATE_SAYS: Final[str] = (
    "emerge --sync gentoo exited 1: Manifest mismatch for gui-apps/Manifest.gz "
    "__size__: expected: 5745, have: 5746"
)


def test_a_site_that_cannot_be_reached_is_told_apart_from_one_mid_update() -> None:
    from gentoo_install.plan import portage as plan_portage

    assert plan_portage.site_unreachable(_UNREACHABLE_SAYS)
    # The negative control the whole fallback rests on: a mirror that answered
    # and served an incomplete snapshot must be retried where it is, because
    # every other mirror is equally likely to be mid-update.
    assert not plan_portage.site_unreachable(_MID_UPDATE_SAYS)


def test_an_unreachable_overlay_site_moves_to_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`btrfs-luks` and `vm-zfs-mirror` both stopped fifteen minutes into an
    install because the one overlay mirror they were pointed at refused the
    connection, while four other sites carry the same repository."""
    from typing import Sequence

    from gentoo_install.errors import CommandFailed
    from gentoo_install.plan import portage as plan_portage

    from .recorder import Recorder

    class Unreachable(Recorder):
        """Refuses every sync until the repository points at `reachable`."""

        reachable: str = "https://mirrors.ha.edu.cn/gentoo-zh.git"
        pointed: str = "https://mirror.nju.edu.cn/git/gentoo-zh.git"
        attempts: int = 0

        def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> CommandOutput:
            if "--sync" in argv:
                self.attempts += 1
                if self.pointed != self.reachable:
                    raise CommandFailed(_UNREACHABLE_SAYS)
            return super().run_in_target(argv, check=check)

        def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
            for line in content.splitlines():
                if line.startswith("sync-uri = "):
                    self.pointed = line.removeprefix("sync-uri = ")
            super().write(path, content, mode=mode)

    location = PurePosixPath("/var/db/repos/gentoo-zh")
    alternates = tuple(
        plan_portage.ConfigureRepository(
            name="gentoo-zh", location=location, sync_uri=uri, verify_commits=False
        )
        for uri in (
            "https://mirrors.cernet.edu.cn/gentoo-zh.git",
            "https://mirrors.ha.edu.cn/gentoo-zh.git",
        )
    )
    operation = plan_portage.SyncRepository(
        name="gentoo-zh", location=location, alternates=alternates
    )
    monkeypatch.setattr(plan_portage, "SYNC_PAUSE", 0.0)

    recorder = Unreachable()
    operation.apply(recorder)
    assert recorder.pointed == recorder.reachable
    # One attempt per site and no more: a host that refused the connection is
    # not waited on, so the three sites cost three attempts rather than nine.
    assert recorder.attempts == 3, recorder.attempts
    assert recorder.argv_starting("sleep") == ()

    # The directory is cleared before each retry, or git refuses to clone into
    # what the failed attempt left behind.
    cleared = [one for one in recorder.in_target if one[0] == "rm"]
    assert len(cleared) == 3, cleared

    # Negative control: with no alternates the same failure stops the install.
    alone = plan_portage.SyncRepository(name="gentoo-zh", location=location)
    with pytest.raises(CommandFailed):
        alone.apply(Unreachable())

    # Negative control: a mid-update mirror is retried where it is and never
    # spends an alternate, however many alternates it has.
    class MidUpdate(Recorder):
        attempts: int = 0
        pointed_at: list[str] = []

        def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> CommandOutput:
            if "--sync" in argv:
                self.attempts += 1
                raise CommandFailed(_MID_UPDATE_SAYS)
            return super().run_in_target(argv, check=check)

    stubborn = MidUpdate()
    with pytest.raises(CommandFailed):
        operation.apply(stubborn)
    assert stubborn.attempts == plan_portage.SYNC_TRIES, stubborn.attempts


def test_the_overlay_fallback_names_sites_the_mirror_table_carries() -> None:
    """The alternates are the other sites in the one table, never the chosen
    one again, and `gig` has none because it is published from one host."""
    from gentoo_install.model import mirrors
    from gentoo_install.model.config import GentooZhMirror

    for chosen in GentooZhMirror:
        uris = mirrors.overlay_sync_uris("gentoo-zh", chosen)
        assert len(uris) == len(set(uris)), uris
        assert uris[0] == mirrors.gentoozh(chosen).git
        assert set(uris) == {site.git for site in mirrors.GENTOOZH_SITES if site.git}
        assert mirrors.overlay_sync_uris("gig", chosen) == ()
def test_the_stage3_matches_the_profile_and_not_only_the_init_system() -> None:
    """`eselect profile set` is all the installer runs, and a profile switch
    removes nothing: a no-multilib profile on a multilib stage3 keeps every
    32-bit ABI and package the tarball came with, so what the option calls a
    complete 64-bit environment is not one.

    Every profile the menu offers, and the tarball Gentoo publishes for it.
    """
    from dataclasses import replace as replaced

    from gentoo_install.model.config import InitSystem
    from gentoo_install.plan.portage import variant_of
    from gentoo_install.tui.screens import PROFILES

    wanted = {
        "default/linux/amd64/23.0": "openrc",
        "default/linux/amd64/23.0/systemd": "systemd",
        "default/linux/amd64/23.0/desktop": "desktop-openrc",
        "default/linux/amd64/23.0/desktop/systemd": "desktop-systemd",
        "default/linux/amd64/23.0/desktop/plasma": "desktop-openrc",
        "default/linux/amd64/23.0/desktop/plasma/systemd": "desktop-systemd",
        "default/linux/amd64/23.0/desktop/gnome": "desktop-openrc",
        "default/linux/amd64/23.0/desktop/gnome/systemd": "desktop-systemd",
        "default/linux/amd64/23.0/no-multilib": "nomultilib-openrc",
        "default/linux/amd64/23.0/no-multilib/systemd": "nomultilib-systemd",
    }
    assert set(wanted) == set(PROFILES), "a profile was added with no stage3 named for it"

    base = config()
    for profile, variant in wanted.items():
        init = InitSystem.SYSTEMD if profile.endswith("systemd") else InitSystem.OPENRC
        installation = replaced(
            base,
            portage=replaced(base.portage, profile=profile),
            system=replaced(base.system, init=init),
        )
        assert variant_of(installation) == variant, profile


def test_the_first_snapshot_uses_portages_keyserver_policy() -> None:
    from gentoo_install.plan.operations import CommandOutput
    from gentoo_install.plan import portage as plan_portage
    from .recorder import Recorder

    def answer(argv: Sequence[str]) -> str | None:
        if tuple(argv[:3]) == ("portageq", "envvar", "PORTAGE_GPG_KEY_SERVER"):
            return CommandOutput("hkps://keys.gentoo.org\n", 0)
        return None

    recorder = Recorder(answering=answer)
    plan_portage.WebrsyncRepository().apply(recorder)
    assert recorder.in_target[-1] == (
        "env", "PORTAGE_GPG_KEY_SERVER=hkps://keys.gentoo.org", "emerge-webrsync"
    )


def test_an_empty_keyserver_policy_still_names_one() -> None:
    """gemato tries WKD and falls back to a keyserver. A stage3 configures
    none, so a WKD that does not answer ended the install at `No keyserver
    available` with the snapshot already downloaded."""
    from gentoo_install.plan.operations import CommandOutput
    from gentoo_install.plan import portage as plan_portage
    from .recorder import Recorder

    def answer(argv: Sequence[str]) -> str | None:
        if tuple(argv[:3]) == ("portageq", "envvar", "PORTAGE_GPG_KEY_SERVER"):
            return CommandOutput("\n", 0)
        return None

    recorder = Recorder(answering=answer)
    plan_portage.WebrsyncRepository().apply(recorder)
    assert recorder.in_target[-1] == (
        "env", f"PORTAGE_GPG_KEY_SERVER={plan_portage.KEY_SERVER}", "emerge-webrsync"
    )


def test_an_unreadable_keyserver_policy_falls_back_to_gentoos_own() -> None:
    """`portageq` cannot answer before this operation has fetched a snapshot:
    `repos.conf` names `/var/db/repos/gentoo` and nothing has created it, so it
    exits 1. Treating that as a fault stopped every install at step 21 of 51,
    and treating it as `no keyserver` stopped every one of them at gemato.
    """
    from gentoo_install.plan.operations import CommandOutput
    from gentoo_install.plan import portage as plan_portage
    from .recorder import Recorder

    def answer(argv: Sequence[str]) -> str | None:
        if tuple(argv[:3]) == ("portageq", "envvar", "PORTAGE_GPG_KEY_SERVER"):
            return CommandOutput(
                "!!! Invalid Repository Location (not a dir): '/var/db/repos/gentoo'\n", 1
            )
        return None

    recorder = Recorder(answering=answer)
    plan_portage.WebrsyncRepository().apply(recorder)
    assert recorder.in_target[-1] == (
        "env", f"PORTAGE_GPG_KEY_SERVER={plan_portage.KEY_SERVER}", "emerge-webrsync"
    )


def test_a_pinned_package_reaches_usepkg_exclude_without_its_version() -> None:
    """`--usepkg-exclude` takes package names and slot atoms only. A pinned
    kernel reached it as `=sys-kernel/gentoo-cjk-kernel-bin-7.1.7` and emerge
    answered `Invalid Atom(s) in --usepkg-exclude parameter`, which stopped an
    install with the disks already written. Measured against the real emerge:
    `cat/pkg` and `cat/pkg:0` are accepted and anything carrying a version is
    not."""
    from gentoo_install.plan.portage import Emerge, SourcePolicy, _unversioned

    assert _unversioned("=sys-kernel/gentoo-cjk-kernel-bin-7.1.7") == (
        "sys-kernel/gentoo-cjk-kernel-bin"
    )
    assert _unversioned("=sys-kernel/gentoo-kernel-bin-6.18.43-r2") == (
        "sys-kernel/gentoo-kernel-bin"
    )
    # Already a plain name, and one with a digit in it that is not a version.
    assert _unversioned("sys-fs/zfs") == "sys-fs/zfs"
    assert _unversioned("app-i18n/fcitx5") == "app-i18n/fcitx5"

    recorder = Recorder()
    Emerge(
        summary="install the kernel",
        packages=("=sys-kernel/gentoo-cjk-kernel-bin-7.1.7",),
        source=SourcePolicy.build_subset(
            ("=sys-kernel/gentoo-cjk-kernel-bin-7.1.7",)
        ),
    ).apply(recorder)
    excluded = next(
        one[one.index("--usepkg-exclude") + 1]
        for one in recorder.in_target
        if "--usepkg-exclude" in one
    )
    assert "7.1.7" not in excluded, excluded
    assert "sys-kernel/gentoo-cjk-kernel-bin" in excluded


def test_an_rsync_install_does_not_fetch_the_tree_twice() -> None:
    """`emerge-webrsync` places a signed snapshot, and `SyncRepository` deletes
    it and fetches the same tree again over rsync. Twelve guests behind one
    address did that at once and `rsync.gentoo.org` answered `access denied to
    gentoo-portage from UNKNOWN`, which its own MOTD warns about. The
    repository is still configured, so the installed system syncs later.
    """
    from pathlib import Path

    from gentoo_install.exec.config import load
    from gentoo_install.model.config import Sync
    from gentoo_install.plan import portage as plan_portage

    installation = load(Path("tests/fixtures/vm-btrfs.toml"))
    over_rsync = replace(
        installation,
        portage=replace(installation.portage, sync=Sync.RSYNC, overlays=()),
    )
    operations = plan_portage.build(over_rsync, "https://distfiles.gentoo.org")
    synced = [
        one
        for one in operations
        if isinstance(one, plan_portage.SyncRepository) and one.name == "gentoo"
    ]
    assert not synced, [one.describe() for one in operations]
    pointed = [one for one in operations if isinstance(one, plan_portage.ConfigureRepository)]
    assert [one for one in pointed if one.name == "gentoo"], [one.describe() for one in operations]


def test_unpacking_a_stage3_says_it_is_still_unpacking() -> None:
    """A stage3 takes minutes to unpack and `tar` says nothing while it does.
    `vm-f2fs` was ended at 29 minutes with its last line being the extraction,
    which is the same fault the download had: the watchdog reads the console,
    and the console was silent.
    """
    from gentoo_install.plan import portage as plan_portage

    from .recorder import Recorder

    recorder = Recorder()
    plan_portage.InstallStage3(
        mirror="https://distfiles.gentoo.org", variant="systemd"
    ).apply(recorder)
    ran = next(one for one in recorder.commands if one and one[0] == "tar")
    assert f"--checkpoint={plan_portage.UNPACK_CHECKPOINT}" in ran, ran
    assert "--checkpoint-action=echo" in ran, ran


def test_an_awkward_make_conf_value_survives_the_whole_plan() -> None:
    """`FETCHCOMMAND` carries both a quote and a `$`, and writing it plainly
    ended the value at the first quote, so `emerge-webrsync` ran a wget with no
    URL and no tree ever arrived."""
    from gentoo_install.plan.portage import FETCHCOMMAND, RESUMECOMMAND, merge

    written = merge("", (("FETCHCOMMAND", FETCHCOMMAND), ("RESUMECOMMAND", RESUMECOMMAND)))
    for line in written.splitlines():
        if not line.startswith(("FETCHCOMMAND=", "RESUMECOMMAND=")):
            continue
        key, _, value = line.partition("=")
        # bash reads the file, so bash decides what the value is.
        read_back = subprocess.run(
            ["bash", "-c", f'{line}\nprintf %s "${{{key}}}"'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert read_back == (FETCHCOMMAND if key == "FETCHCOMMAND" else RESUMECOMMAND)
        assert "${URI}" in read_back


def test_a_proxy_with_a_private_file_turns_userfetch_off() -> None:
    """Portage's own default drops the fetch to the `portage` user, which
    cannot read the `0600` file holding the password: curl answered `cannot
    read config from /etc/gentoo-install/curl-proxy.conf` for every distfile
    and the install stopped at `linux-firmware`."""
    installation = replace(
        config(), proxy=ProxyConfig(kind=ProxyKind.SOCKS5, host="proxy.example", port=1080, username="installer", password="secret")
    )

    make_conf = apply_all(installation).files[PurePosixPath("/etc/portage/make.conf")]
    features = [line for line in make_conf.splitlines() if line.startswith("FEATURES=")]

    # One line: two would each be a whole assignment and bash keeps the last.
    assert len(features) == 1
    assert "-userfetch" in features[0]


def test_no_proxy_leaves_userfetch_alone() -> None:
    """It is a Portage default and this installer has no opinion on it; the
    only reason to name it is a file the fetching user cannot read."""
    make_conf = apply_all(config()).files[PurePosixPath("/etc/portage/make.conf")]

    assert "userfetch" not in make_conf


def test_the_keyring_proxy_is_written_after_getuto_rebuilds_the_directory() -> None:
    """`getuto` rebuilds `/etc/portage/gnupg` from a staging copy, so a proxy
    line written before it is discarded. Its own file carries
    `honor-http-proxy` and the endpoint in the environment has no credentials,
    so the tree's signature check answered `Not authenticated`."""
    from gentoo_install.plan import portage

    installation = replace(
        config(), proxy=ProxyConfig(kind=ProxyKind.HTTP, host="proxy.example", port=8080,
                                    username="operator", password="secret")
    )
    recorder = apply_all(installation)
    written = recorder.files[PurePosixPath("/etc/portage/gnupg/dirmngr.conf")]

    assert "honor-http-proxy" in written
    assert "secret" in written
    # The same operation runs getuto and writes the file, so nothing can be
    # ordered between them.
    running = [
        one for one in portage.build(installation, MIRROR)
        if isinstance(one, portage.PrepareBinhostTrust)
    ]
    assert len(running) == 1
    assert running[0].proxy.enabled


def test_a_socks_proxy_writes_no_keyring_proxy() -> None:
    """dirmngr has no SOCKS support at all, and `http-proxy socks5h://...`
    answers `Invalid URI`."""
    installation = replace(
        config(), proxy=ProxyConfig(kind=ProxyKind.SOCKS5, host="proxy.example", port=1080)
    )

    assert PurePosixPath("/etc/portage/gnupg/dirmngr.conf") not in apply_all(installation).files


def test_git_reaches_the_proxy_with_the_password_it_demands() -> None:
    """`/etc/gitconfig` is `0600`, the same protection the curl configuration
    gets. Without the credential the proxy answered
    `cannot complete SOCKS5 connection to github.com. (2)` and the tree sync
    stopped."""
    installation = replace(
        config(),
        proxy=ProxyConfig(kind=ProxyKind.SOCKS5, host="proxy.example", port=1080,
                          username="operator", password="secret"),
    )

    assert "secret" in apply_all(installation).files[PurePosixPath("/etc/gitconfig")]


def test_the_flag_variables_still_follow_common_flags() -> None:
    """The stage3 writes `CFLAGS="${COMMON_FLAGS}"` and this installer keeps
    that idiom. Escaping the `$` handed gcc the literal `${COMMON_FLAGS}` as a
    filename, and every source build stopped at `linker input file not found`.
    """
    import subprocess

    from gentoo_install.plan.portage import FETCHCOMMAND, merge

    written = merge(
        "",
        (
            ("COMMON_FLAGS", "-O2 -pipe"),
            ("CFLAGS", "${COMMON_FLAGS}"),
            ("CXXFLAGS", "${COMMON_FLAGS}"),
            ("FETCHCOMMAND", FETCHCOMMAND),
        ),
    )
    read_back = subprocess.run(
        [
            "bash",
            "-c",
            'source /dev/stdin; printf "%s\\n%s\\n%s" "$CFLAGS" "$CXXFLAGS" "$FETCHCOMMAND"',
        ],
        input=written,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.split("\n")

    # Expanded, because bash is what expands them when Portage sources the file.
    assert read_back[0] == "-O2 -pipe"
    assert read_back[1] == "-O2 -pipe"
    # Not expanded: the fetcher substitutes these, long after the file is read.
    assert "${URI}" in read_back[2]


def test_the_first_snapshot_is_tried_again_like_every_later_sync() -> None:
    """It downloads the whole tree, looks the signing key up over WKD and
    refreshes it from a keyserver, and gemato lets a `ReadTimeout` in the WKD
    lookup out rather than falling back. The later syncs have had three
    attempts since a mirror rewrote its Manifests mid-sync; this one had none,
    and `openrc-sdboot` ended a cluster round with the tree never fetched."""
    from gentoo_install.errors import CommandFailed
    from gentoo_install.plan import portage as plan_portage
    from .recorder import Recorder

    tries = {"n": 0}

    def answer(argv: Sequence[str]) -> str | None:
        if "emerge-webrsync" in argv:
            tries["n"] += 1
            if tries["n"] < plan_portage.SYNC_TRIES:
                raise CommandFailed("emerge-webrsync exited 1: requests.exceptions.ReadTimeout")
        return None

    recorder = Recorder(answering=answer)
    plan_portage.WebrsyncRepository().apply(recorder)

    assert tries["n"] == plan_portage.SYNC_TRIES
    assert any("sleep" in one for one in recorder.commands)


def test_a_snapshot_that_never_arrives_stops_the_install() -> None:
    """Bounded, like the later syncs: a mirror that is down has to stop the
    install rather than be walked around."""
    from gentoo_install.errors import CommandFailed
    from gentoo_install.plan import portage as plan_portage
    from .recorder import Recorder

    tries = {"n": 0}

    def answer(argv: Sequence[str]) -> str | None:
        if "emerge-webrsync" in argv:
            tries["n"] += 1
            raise CommandFailed("emerge-webrsync exited 1")
        return None

    recorder = Recorder(answering=answer)
    with pytest.raises(CommandFailed):
        plan_portage.WebrsyncRepository().apply(recorder)

    # `KEYRING_TRIES`, not `SYNC_TRIES`: the first sync outlasts a keyserver a
    # whole cluster round is asking at once, and how long it waits between
    # attempts is what the failure decides.
    assert tries["n"] == plan_portage.KEYRING_TRIES


#: What `btrfs-luks` printed when the binary host dropped one TLS handshake
#: seventy-five minutes into an install. Portage says none of the markers that
#: name a binary failure; what names it is the line that started the package.
SSL_DROPPED = """>>> Emerging binary (89 of 336) app-crypt/libmd-1.2.0::gentoo
>>> Installing (89 of 336) app-crypt/libmd-1.2.0::gentoo
>>> Emerging binary (90 of 336) x11-misc/appmenu-gtk-module-25.04::gentoo
--2026-08-15 19:45:25--  https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64/\
x11-misc/appmenu-gtk-module/appmenu-gtk-module-25.04-4.gpkg.tar
Resolving distfiles.gentoo.org... 143.244.51.245, 89.187.187.11
Connecting to distfiles.gentoo.org|143.244.51.245|:443... connected.
Unable to establish SSL connection.

>>> Failed to emerge x11-misc/appmenu-gtk-module-25.04, Log file:
"""


def test_a_binary_whose_download_broke_retries_from_source() -> None:
    """One dropped TLS handshake to the binary host ended a seventy-five minute
    install with exit 4. Portage prints wget's own words and none of the
    markers that name a binary failure, so the line that started the package is
    what says it was one."""
    recorder = Recorder()
    recorder.answering = lambda argv: (
        CommandOutput(SSL_DROPPED, 1)
        if argv[0] == "emerge" and "--usepkg=n" not in argv
        else CommandOutput("[ebuild] x11-misc/appmenu-gtk-module-25.04", 0)
    )

    portage.Emerge(
        packages=("kde-plasma/plasma-meta",), summary="install the plasma group"
    ).apply(recorder)

    emerges = [argv for argv in recorder.in_target if argv[0] == "emerge"]
    # Binaries, binaries again because one dropped handshake is not the host
    # being gone, then source.
    assert len(emerges) == 3
    assert "--getbinpkg=n" in emerges[-1] and "--usepkg=n" in emerges[-1]
    assert "--getbinpkg=n" not in emerges[1]
    assert recorder.degraded(portage.BINARY_PACKAGES)


def test_a_source_build_that_failed_is_not_called_a_binary_failure() -> None:
    """The same `Failed to emerge` line, from a package Portage was compiling.
    Retrying that from source would repeat the build that already failed."""
    recorder = Recorder()
    compiled = SSL_DROPPED.replace(
        ">>> Emerging binary (90 of 336) x11-misc/appmenu-gtk-module-25.04::gentoo",
        ">>> Emerging (90 of 336) x11-misc/appmenu-gtk-module-25.04::gentoo",
    )
    recorder.answering = lambda argv: CommandOutput(compiled, 1)

    with pytest.raises(CommandFailed):
        portage.Emerge(
            packages=("kde-plasma/plasma-meta",), summary="install the plasma group"
        ).apply(recorder)

    assert not recorder.degraded(portage.BINARY_PACKAGES)


def test_a_binary_that_downloads_on_the_second_try_compiles_nothing() -> None:
    """A dropped TLS handshake is not the binary host being gone. Degrading on
    the first one rebuilds a whole group from source: `libtommath` failed that
    way inside the plasma group, which downloads in seconds and compiles in
    hours."""
    recorder = Recorder()
    tries: list[int] = []

    def answering(argv: Sequence[str]) -> CommandOutput:
        if argv[0] != "emerge":
            return CommandOutput("", 0)
        tries.append(1)
        return CommandOutput(SSL_DROPPED, 1) if len(tries) == 1 else CommandOutput("", 0)

    recorder.answering = answering

    portage.Emerge(
        packages=("kde-plasma/plasma-meta",), summary="install the plasma group"
    ).apply(recorder)

    emerges = [argv for argv in recorder.in_target if argv[0] == "emerge"]
    assert len(emerges) == 2
    assert all("--getbinpkg=n" not in argv for argv in emerges)
    assert not recorder.degraded(portage.BINARY_PACKAGES)


#: Verbatim from `run54/vm-desktop.install.txt`, which ended at exit 4.
BACKGROUND_FETCH = """>>> Verifying ebuild manifests

>>> Running pre-merge checks for net-wireless/wireless-regdb-20251007
 * Fetching in the background:
 * /var/cache/binhost/gentoo/net-wireless/wireless-regdb/\
wireless-regdb-20251007-1.gpkg.tar.partial
 * To view fetch progress, run in another terminal:
 * tail -f /var/log/emerge-fetch.log

>>> Failed to emerge net-wireless/wireless-regdb-20251007
"""


def test_a_binary_fetched_in_the_background_retries_from_source() -> None:
    """`vm-desktop` in run54 stopped at exit 4 after twelve minutes. A package
    whose binary Portage fetches in the background never prints `Emerging
    binary`, and its failure line carries no comma, so neither half of the
    existing rule matched. The only line naming it a binary is the
    `.gpkg.tar.partial` path.
    """
    recorder = Recorder()
    recorder.answering = lambda argv: (
        CommandOutput(BACKGROUND_FETCH, 1)
        if argv[0] == "emerge" and "--usepkg=n" not in argv
        else CommandOutput("[ebuild] net-wireless/wireless-regdb-20251007", 0)
    )

    portage.Emerge(
        packages=("kde-plasma/plasma-meta",), summary="install the plasma group"
    ).apply(recorder)

    emerges = [argv for argv in recorder.in_target if argv[0] == "emerge"]
    assert "--getbinpkg=n" in emerges[-1] and "--usepkg=n" in emerges[-1]
    assert recorder.degraded(portage.BINARY_PACKAGES)


def test_a_background_fetch_of_a_source_build_is_not_a_binary_failure() -> None:
    """Negative control. The same shape without the `.gpkg.tar` line is a
    package Portage was compiling, and retrying it from source repeats the
    build that already failed."""
    recorder = Recorder()
    compiled = "\n".join(
        line for line in BACKGROUND_FETCH.splitlines() if ".gpkg.tar" not in line
    )
    recorder.answering = lambda argv: CommandOutput(compiled, 1)

    with pytest.raises(CommandFailed):
        portage.Emerge(
            packages=("kde-plasma/plasma-meta",), summary="install the plasma group"
        ).apply(recorder)

    assert not recorder.degraded(portage.BINARY_PACKAGES)


def test_a_gpkg_for_one_package_does_not_excuse_another_failing() -> None:
    """Negative control. The binary that was fetched and the package that
    failed have to be the same one, or every source failure after any binary
    download would be retried from source for nothing."""
    recorder = Recorder()
    other = BACKGROUND_FETCH.replace(
        ">>> Failed to emerge net-wireless/wireless-regdb-20251007",
        ">>> Failed to emerge sys-apps/portage-3.0.69",
    )
    recorder.answering = lambda argv: CommandOutput(other, 1)

    with pytest.raises(CommandFailed):
        portage.Emerge(
            packages=("kde-plasma/plasma-meta",), summary="install the plasma group"
        ).apply(recorder)

    assert not recorder.degraded(portage.BINARY_PACKAGES)


def test_a_command_killed_by_a_signal_names_the_signal() -> None:
    """`vm-xfs` lost 36.9 minutes to `emerge failed with exit -13`, which sent
    a reader looking for an exit status that does not exist. `subprocess`
    answers a negative code for a signal, and 13 is SIGPIPE.
    """
    from gentoo_install.plan.operations import CommandOutput, ending

    assert ending(-13) == "SIGPIPE (13)"
    assert CommandOutput("", -9).ending == "SIGKILL (9)"

    # Negative control one: an ordinary status is still an exit status, or
    # every failing command would read as though something killed it.
    assert ending(1) == "exit 1"
    assert ending(0) == "exit 0"
    assert CommandOutput("", 4).ending == "exit 4"

    # Negative control two: a number with no signal behind it says so rather
    # than raising, because the message is what a failing run carries out.
    assert ending(-999) == "signal 999"


def test_a_sync_that_ran_out_of_sites_says_how_many_it_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`vm-cjk-kernel` ended run58 with

        emerge --sync gentoo-zh ended with exit 1: fatal: unable to access
        'https://mirrors.ha.edu.cn/git/gentoo-zh.git/': Failed to connect

    which reads as one unlucky mirror. The log held five hosts, every one of
    them refused, which is a guest with no route rather than a bad mirror.
    """
    from gentoo_install.plan import portage as plan_portage

    from .recorder import Recorder

    location = PurePosixPath("/var/db/repos/gentoo-zh")
    alternates = tuple(
        plan_portage.ConfigureRepository(
            name="gentoo-zh", location=location, sync_uri=uri, verify_commits=False
        )
        for uri in (
            "https://mirrors.cernet.edu.cn/gentoo-zh.git",
            "https://mirrors.ha.edu.cn/gentoo-zh.git",
        )
    )

    class NeverReachable(Recorder):
        def run_in_target(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            self.in_target.append(tuple(argv))
            if "--sync" in argv:
                raise CommandFailed(
                    "emerge --sync gentoo-zh ended with exit 1: fatal: unable to access "
                    "'https://mirrors.ha.edu.cn/git/gentoo-zh.git/': Could not connect to server"
                )
            return CommandOutput("", 0)

    monkeypatch.setattr(plan_portage, "SYNC_PAUSE", 0.0)
    with pytest.raises(CommandFailed, match="none of the 3 sites for gentoo-zh") as caught:
        plan_portage.SyncRepository(
            name="gentoo-zh", location=location, alternates=alternates
        ).apply(NeverReachable())
    # The last host's own words are kept: the count says how wide the failure
    # was and the message says what the failure looked like.
    assert "Could not connect to server" in str(caught.value)

    # Negative control: a sync that succeeds on an alternate raises nothing, so
    # the count is not reported for a run that recovered.
    class SecondSiteAnswers(Recorder):
        tries = 0

        def run_in_target(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            self.in_target.append(tuple(argv))
            if "--sync" in argv:
                type(self).tries += 1
                if type(self).tries == 1:
                    raise CommandFailed("could not connect to server")
            return CommandOutput("", 0)

    plan_portage.SyncRepository(
        name="gentoo-zh", location=location, alternates=alternates
    ).apply(SecondSiteAnswers())


def test_an_emerge_the_binary_host_killed_falls_back_to_source() -> None:
    """`run56/vm-xfs.log`, the install's first emerge:

        2641: !!! [gentoo] Error fetching binhost package info from
              'https://mirrors.nju.edu.cn/gentoo/releases/amd64/...'
        2642: !!! [gentoo] <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]>
        2718: [34/51] [system] install sshd: emerge net-misc/openssh
        2720: the install stopped: CommandFailed: emerge failed with exit -13

    Portage's stdout is a pipe and therefore block-buffered, so the signal
    lost every line it had printed and `_binpkg_failure` had no text to read.
    A binary host that kills emerge is a binary host failure, and the policy
    for one is a warning and a source build, not a stopped install.
    """
    import signal

    recorder = Recorder()
    tries: list[int] = []

    def answering(argv: Sequence[str]) -> CommandOutput | None:
        if argv[0] != "emerge":
            return None
        if "--usepkg=n" in argv:
            return CommandOutput("[ebuild] net-misc/openssh-10.4", 0)
        tries.append(len(tries))
        return CommandOutput("", -signal.SIGPIPE)

    recorder.answering = answering
    portage.Emerge(packages=("net-misc/openssh",), summary="install sshd").apply(recorder)

    emerges = [argv for argv in recorder.in_target if argv[0] == "emerge"]
    # Binaries, binaries again because one dropped handshake is not the host
    # being gone, then source.
    assert len(emerges) == 3, emerges
    assert "--getbinpkg=n" in emerges[-1] and "--usepkg=n" in emerges[-1]
    assert recorder.degraded(portage.BINARY_PACKAGES)
    assert "SIGPIPE" in recorder.degradations[portage.BINARY_PACKAGES]


def test_a_signalled_emerge_that_printed_something_is_not_blamed_on_the_host() -> None:
    """The negative control. A build killed by the out-of-memory killer prints
    plenty first, and calling that a binhost failure would rebuild it from
    source and be killed the same way an hour later.
    """
    import signal

    recorder = Recorder()
    recorder.answering = lambda argv: (
        CommandOutput(">>> Compiling source in /var/tmp/portage/...", -signal.SIGKILL)
        if argv[0] == "emerge"
        else None
    )

    with pytest.raises(CommandFailed, match="SIGKILL"):
        portage.Emerge(packages=("sys-devel/gcc",), summary="install gcc").apply(recorder)
    assert not recorder.degraded(portage.BINARY_PACKAGES)


def test_a_keyserver_the_whole_round_is_asking_gets_longer_than_a_mirror() -> None:
    """run62 dispatched ten guests and nine stopped inside ten minutes at

        OpenPGP keyring refresh failed: | gpg: keyserver refresh failed:
        Try again later

    while `vm-f2fs` reached the same step at 49.6 minutes and passed. So the
    keyserver was not unreachable, it was being asked by a whole round at once,
    and three tries over ninety seconds is not enough to outlast that. A
    Manifest mismatch is the opposite case and still gets the short wait.
    """
    refused = Recorder()

    def refusing(argv: Sequence[str]) -> CommandOutput | None:
        if argv[-1] != "emerge-webrsync":
            return None
        raise CommandFailed(
            "emerge-webrsync ended with exit 1: [  ERROR] OpenPGP keyring "
            "refresh failed: | gpg: keyserver refresh failed: Try again later"
        )

    refused.answering = refusing

    with pytest.raises(CommandFailed):
        portage.WebrsyncRepository().apply(refused)

    slept = [argv for argv in refused.commands if argv and argv[0] == "sleep"]
    assert len(slept) == portage.KEYRING_TRIES - 1, slept
    assert [float(one[1]) for one in slept] == [
        portage.KEYRING_PAUSE * (n + 1) for n in range(portage.KEYRING_TRIES - 1)
    ], slept
    # Twelve minutes rather than ninety seconds, which is what outlasts a round.
    assert sum(float(one[1]) for one in slept) > 8 * 60


def test_a_manifest_mismatch_still_gets_the_short_wait() -> None:
    """The negative direction: a mirror rewriting its Manifests answers within
    a minute, and waiting twelve for it turns a transient into a stall."""
    mismatch = Recorder()

    def mismatching(argv: Sequence[str]) -> CommandOutput | None:
        if argv[-1] != "emerge-webrsync":
            return None
        raise CommandFailed(
            "emerge-webrsync ended with exit 1: Manifest mismatch for "
            "gui-apps/Manifest.gz __size__: expected: 5745, have: 5746"
        )

    mismatch.answering = mismatching

    with pytest.raises(CommandFailed):
        portage.WebrsyncRepository().apply(mismatch)

    slept = [float(argv[1]) for argv in mismatch.commands if argv and argv[0] == "sleep"]
    assert slept == [
        portage.SYNC_PAUSE * (n + 1) for n in range(portage.KEYRING_TRIES - 1)
    ], slept
    assert sum(slept) < 5 * 60, slept


def test_a_refused_keyring_refresh_moves_to_another_keyserver() -> None:
    """Waiting cannot leave a window one host is refusing for. Measured
    2026-08-17: the cluster's guests read `KEYSERVER_TCP refused` for
    140.211.166.190 while this workstation received the key from it, and eight
    of nine run63 guests died after all four attempts asked that one host."""
    from gentoo_install.errors import CommandFailed
    from gentoo_install.plan.operations import CommandOutput
    from gentoo_install.plan import portage as plan_portage
    from .recorder import Recorder

    asked: list[str] = []

    def answer(argv: Sequence[str]) -> str | None:
        if tuple(argv[:3]) == ("portageq", "envvar", "PORTAGE_GPG_KEY_SERVER"):
            return CommandOutput("\n", 0)
        if "emerge-webrsync" in argv:
            asked.append(next(one for one in argv if one.startswith("PORTAGE_GPG_KEY")))
            raise CommandFailed(
                "emerge-webrsync ended with exit 1: [  ERROR] OpenPGP keyring "
                "refresh failed: | gpg: keyserver refresh failed: Try again later"
            )
        return None

    recorder = Recorder(answering=answer)
    with pytest.raises(CommandFailed):
        plan_portage.WebrsyncRepository().apply(recorder)

    assert asked[0] == f"PORTAGE_GPG_KEY_SERVER={plan_portage.KEY_SERVER}"
    assert len(set(asked)) > 1, asked
    # Every host in the table is reached before the attempts run out, or a
    # fallback that is never asked reads as coverage and is not.
    assert {one.partition("=")[2] for one in asked} == set(plan_portage.KEY_SERVERS)


def test_a_manifest_mismatch_keeps_asking_the_same_keyserver() -> None:
    """The rotation answers a refused keyserver. A mirror rewriting its
    Manifests is a different failure, and changing the keyserver for it would
    make the next attempt fetch a key it had no reason to refetch."""
    from gentoo_install.errors import CommandFailed
    from gentoo_install.plan.operations import CommandOutput
    from gentoo_install.plan import portage as plan_portage
    from .recorder import Recorder

    asked: list[str] = []

    def answer(argv: Sequence[str]) -> str | None:
        if tuple(argv[:3]) == ("portageq", "envvar", "PORTAGE_GPG_KEY_SERVER"):
            return CommandOutput("\n", 0)
        if "emerge-webrsync" in argv:
            asked.append(next(one for one in argv if one.startswith("PORTAGE_GPG_KEY")))
            raise CommandFailed("emerge-webrsync exited 1: Manifest mismatch")
        return None

    recorder = Recorder(answering=answer)
    with pytest.raises(CommandFailed):
        plan_portage.WebrsyncRepository().apply(recorder)

    assert len(set(asked)) == 1, asked


def test_the_keyserver_policy_the_stage3_names_is_not_asked_twice() -> None:
    """A stage3 or an operator that names a server has chosen it, so it leads
    the rotation and does not also appear later in it."""
    from gentoo_install.plan import portage as plan_portage

    assert plan_portage._key_servers(plan_portage.KEY_SERVER)[0] == plan_portage.KEY_SERVER
    for chosen in (plan_portage.KEY_SERVER, "hkps://keys.example.invalid"):
        rotation = plan_portage._key_servers(chosen)
        assert len(rotation) == len(set(rotation)), rotation
        assert rotation.count(chosen) == 1, rotation
        assert set(plan_portage.KEY_SERVERS) <= set(rotation), rotation
