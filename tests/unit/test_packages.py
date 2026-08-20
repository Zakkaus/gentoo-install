# SPDX-License-Identifier: GPL-2.0-or-later
import sys

from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import ClassVar

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.errors import CommandFailed, ConfigError
from gentoo_install.model.config import ConsoleFontSize, InstallConfig, Overlay, User
from gentoo_install.plan import packages as plan_packages
from gentoo_install.plan.packages import Group, build
from gentoo_install.plan.portage import Emerge, PortageConfigKind

from .recorder import Recorder

from .layouts import config


def test_build_resolves_the_selected_package_groups_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = load_catalog()
    installation = config()
    wanted = replace(
        installation,
        system=replace(
            installation.system,
            users=(User(name="operator", password_hash="$6$x$y"),),
        ),
        portage=replace(
            installation.portage,
            overlays=(
                Overlay(
                    name="gentoo-zh",
                    sync_uri="https://example.invalid/gentoo-zh.git",
                ),
            ),
        ),
        packages=replace(
            installation.packages,
            desktop="plasma",
            graphics=("nvidia",),
            applications=("rime", "pipewire", "wemeet"),
        ),
    )
    resolve = plan_packages.groups
    resolutions = 0

    def counted_groups(
        config_arg: InstallConfig, catalog_arg: plan_packages.Catalog
    ) -> tuple[plan_packages.Group, ...]:
        nonlocal resolutions
        resolutions += 1
        return resolve(config_arg, catalog_arg)

    monkeypatch.setattr(plan_packages, "groups", counted_groups)

    operations = build(wanted, catalog)

    assert resolutions == 1
    assert any(isinstance(one, plan_packages.WriteInputMethodEnvironment) for one in operations)
    assert any(
        isinstance(one, plan_packages.AddUserToGroups)
        and one.user == "operator"
        and one.groups == ("pipewire",)
        for one in operations
    )
    emerged = {
        one.requester
        for one in operations
        if isinstance(one, Emerge) and one.requester
    }
    assert {"the `nvidia` group", "the `wemeet` group"} <= emerged


def test_console_profile_verifies_the_selected_kbd_font_payload() -> None:
    selected = replace(
        config(),
        system=replace(config().system, console_font=ConsoleFontSize.SIZE_16X32),
        packages=replace(config().packages, desktop="console"),
    )
    operations = build(selected, load_catalog())
    verification = next(
        operation for operation in operations if isinstance(operation, plan_packages.VerifyPackagePaths)
    )
    assert verification.package == "sys-apps/kbd"
    assert verification.paths == (PurePosixPath("/usr/share/consolefonts/latarcyrheb-sun32.psfu.gz"),)

    recorder = PackageRecorder()
    recorder.package_paths["sys-apps/kbd"] = frozenset(verification.paths)
    verification.apply(recorder)


def test_console_profile_rejects_a_kbd_payload_without_the_selected_font() -> None:
    selected = replace(
        config(),
        packages=replace(config().packages, desktop="console"),
    )
    verification = next(
        operation
        for operation in build(selected, load_catalog())
        if isinstance(operation, plan_packages.VerifyPackagePaths)
    )
    with pytest.raises(CommandFailed, match="sys-apps/kbd was expected to provide"):
        verification.apply(PackageRecorder())


def test_input_engines_declare_the_language_they_type() -> None:
    catalog = load_catalog()
    providers = {"fcitx5", "ibus"}
    engines = [
        group
        for group in catalog.values()
        if group.input_method and group.name not in providers
    ]
    assert engines
    assert all(group.input_language for group in engines)


def test_font_groups_declare_a_family_and_category() -> None:
    catalog = load_catalog()
    fonts = [group for group in catalog.values() if group.font_family]
    assert fonts
    assert all(group.font_category for group in fonts)
    assert catalog["wenkai-tc"].font_family == "LXGW WenKai TC"
    assert catalog["source-han-sans"].font_family == "Source Han Sans {source}"
    assert catalog["sarasa-mono"].font_family == "Sarasa Mono SC"


def test_plasma_declares_the_minizip_compatibility_required_by_its_stack() -> None:
    catalog = load_catalog()

    for profile in ("plasma", "plasma-full"):
        assert "sys-libs/minizip-ng compat" in catalog[profile].package_use


def test_desktop_profiles_preserve_their_composed_values() -> None:
    catalog = load_catalog()

    assert {
        name: catalog[name]
        for name in ("plasma", "plasma-full", "gnome", "gnome-full")
    } == {
        "plasma": Group(
            name="plasma",
            packages=(
                "kde-plasma/plasma-meta",
                "x11-base/xorg-server",
                "kde-apps/konsole",
                "kde-apps/dolphin",
            ),
            use=("wayland", "qt6", "networkmanager"),
            profile="default/linux/amd64/23.0/desktop/plasma",
            package_use=(
                "app-i18n/fcitx-configtool kcm",
                "sys-libs/minizip-ng compat",
                "kde-plasma/kwin lock",
                "kde-plasma/kwin-x11 lock",
            ),
            input_method_launcher=(
                "/usr/share/applications/fcitx5-wayland-launcher.desktop"
            ),
            wayland=True,
        ),
        "plasma-full": Group(
            name="plasma-full",
            packages=(
                "kde-plasma/plasma-meta",
                "kde-apps/kde-apps-meta",
                "x11-base/xorg-server",
            ),
            use=("wayland", "qt6", "networkmanager"),
            profile="default/linux/amd64/23.0/desktop/plasma",
            package_use=(
                "app-i18n/fcitx-configtool kcm",
                "sys-libs/minizip-ng compat",
                "kde-plasma/kwin lock",
                "kde-plasma/kwin-x11 lock",
            ),
            input_method_launcher=(
                "/usr/share/applications/fcitx5-wayland-launcher.desktop"
            ),
            wayland=True,
        ),
        "gnome": Group(
            name="gnome",
            packages=("gnome-base/gnome-light", "x11-base/xorg-server"),
            use=("wayland", "gnome", "networkmanager", "gtk"),
            profile="default/linux/amd64/23.0/desktop/gnome",
            wayland=True,
        ),
        "gnome-full": Group(
            name="gnome-full",
            packages=("gnome-base/gnome", "x11-base/xorg-server"),
            use=("wayland", "gnome", "networkmanager", "gtk"),
            profile="default/linux/amd64/23.0/desktop/gnome",
            wayland=True,
        ),
    }


def test_profile_base_rejects_an_overlapping_variant_field(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    bases = profiles / "base"
    bases.mkdir(parents=True)
    (bases / "desktop.toml").write_text('use = ["base"]\n', encoding="utf-8")
    (profiles / "variant.toml").write_text(
        'base = "base/desktop.toml"\nuse = ["variant"]\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=r"base and variant both define: use"):
        load_catalog(tmp_path)


def test_profile_base_cycle_is_rejected(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "first.toml").write_text('base = "second.toml"\n', encoding="utf-8")
    (profiles / "second.toml").write_text('base = "first.toml"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="base profiles form a cycle"):
        load_catalog(tmp_path)


def test_xfce_declares_the_dbusmenu_flag_its_panel_requires() -> None:
    """`emerge --pretend` on a cluster run of `vm-openrc-desktop` answered:

        # required by xfce-base/xfce4-panel-4.20.7::gentoo[dbusmenu]
        # required by xfce-base/thunar-4.20.8::gentoo[trash-panel-plugin]
        # required by xfce-base/thunar-volman-4.20.0::gentoo
        # required by xfce-base/xfce4-meta-4.20::gentoo
        >=dev-libs/libdbusmenu-16.04.0-r4 gtk3

    The rejection is of the whole group, so nothing of the desktop is merged.
    """
    catalog = load_catalog()

    assert "dev-libs/libdbusmenu gtk3" in catalog["xfce"].package_use


def test_flclash_declares_the_dbusmenu_flag_its_indicator_requires() -> None:
    """A second group needs the same flag for a different reason, so it is
    declared twice rather than once somewhere neither group names.
    `run59/btrfs-luks.log`, `emerge --pretend` at operation 20:

        # required by dev-libs/libayatana-appindicator-0.5.94::gentoo
        # required by net-proxy/flclash-bin-0.8.95::gentoo-zh
        # required by net-proxy/flclash-bin (argument)
        >=dev-libs/libdbusmenu-16.04.0-r4 gtk3
    """
    catalog = load_catalog()

    assert "dev-libs/libdbusmenu gtk3" in catalog["flclash"].package_use


def test_plasma_declares_the_screen_lock_its_session_requires() -> None:
    """From the same run:

        # required by kde-plasma/plasma-meta-6.6.6::gentoo
        >=kde-plasma/kwin-6.6.6 lock
        # required by kde-plasma/plasma-meta-6.6.6::gentoo[X]
        >=kde-plasma/kwin-x11-6.6.6 lock

    Both variants need it, so it belongs to the base rather than to either.
    """
    catalog = load_catalog()

    for profile in ("plasma", "plasma-full"):
        assert "kde-plasma/kwin lock" in catalog[profile].package_use, profile
        assert "kde-plasma/kwin-x11 lock" in catalog[profile].package_use, profile


def test_ibus_has_declared_chinese_engines() -> None:
    catalog = load_catalog()
    chinese = {
        group.name
        for group in catalog.values()
        if group.input_framework == "ibus" and group.input_language == "Chinese"
    }
    assert {
        "ibus-pinyin",
        "ibus-bopomofo",
        "ibus-cangjie",
        "ibus-chinese-tables",
        "ibus-rime",
    } <= chinese
    assert catalog["ibus-pinyin"].packages == ("app-i18n/ibus-libpinyin",)
    installation = config()
    selected = replace(
        installation,
        packages=replace(
            installation.packages,
            desktop="gnome",
            applications=("ibus", "ibus-pinyin", "configure-input"),
        ),
    )
    operations = build(selected, load_catalog())
    configured = [
        one for one in operations if type(one).__name__ == "ConfigureGnomeInputSources"
    ]
    assert configured
    # Every file it writes, in what a dry run prints: the profile is what
    # makes dconf read the database at all, and naming only the database
    # left a real run writing a file nobody had been told about.
    from gentoo_install.plan.packages import DCONF_PROFILE, GNOME_INPUT_SOURCES

    recorder = Recorder()
    configured[0].apply(recorder)
    described = configured[0].describe()
    assert set(recorder.files) == {DCONF_PROFILE, GNOME_INPUT_SOURCES}, recorder.files
    for path in recorder.files:
        assert str(path) in described, (path, described)
    selected = replace(
        installation,
        packages=replace(
            installation.packages,
            desktop="gnome",
            applications=(
                "ibus",
                "ibus-pinyin",
                "decline-input-configuration",
            ),
        ),
    )
    descriptions = tuple(operation.describe() for operation in build(selected, load_catalog()))
    assert not any("input" in description and "environment" in description for description in descriptions)
    assert not any("dconf" in description for description in descriptions)


class PackageRecorder(Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.package_paths: dict[str, frozenset[PurePosixPath]] = {}
        self.help: dict[str, str] = {}
        self.directories: set[PurePosixPath] = set()

    def installed_package_paths(self, package: str) -> frozenset[PurePosixPath]:
        return self.package_paths.get(package, frozenset())

    def installed_command_help(self, package: str, command: PurePosixPath) -> str:
        return self.help.get(str(command), "")

    def target_is_directory(self, path: PurePosixPath) -> bool:
        return path in self.directories


def test_greetd_update_preserves_package_owned_defaults() -> None:
    recorder = PackageRecorder()
    path = PurePosixPath("/etc/greetd/config.toml")
    recorder.package_paths["gui-libs/greetd"] = frozenset({path})
    recorder.files[path] = (
        "[terminal]\n"
        "vt = \"next\"\n"
        "switch = false\n"
        "\n"
        "[default_session]\n"
        'command = "agreety --cmd $SHELL"\n'
        'user = "greeter-from-package"\n'
    )

    plan_packages.UpdateGreetdConfig(command="tuigreet --time").apply(recorder)

    written = recorder.files[path]
    assert 'command = "tuigreet --time"' in written
    assert 'vt = "next"' in written
    assert "switch = false" in written
    assert 'user = "greeter-from-package"' in written


def test_openrc_display_manager_update_preserves_package_defaults() -> None:
    recorder = PackageRecorder()
    path = PurePosixPath("/etc/conf.d/display-manager")
    recorder.package_paths["gui-libs/display-manager-init"] = frozenset({path})
    recorder.files[path] = (
        "# Keep the package's VT policy.\n"
        "CHECKVT=7\n"
        'DISPLAYMANAGER="xdm"\n'
    )

    plan_packages.UpdatePackageShellAssignment(
        group="lightdm",
        package="gui-libs/display-manager-init",
        path=path,
        key="DISPLAYMANAGER",
        value="lightdm",
    ).apply(recorder)

    assert recorder.files[path] == (
        "# Keep the package's VT policy.\n"
        "CHECKVT=7\n"
        'DISPLAYMANAGER="lightdm"\n'
    )


TUIGREET_HELP = """\
Usage: tuigreet [OPTIONS]

Options:
    -s, --sessions DIRS
    -x, --xsessions DIRS
    -t, --time
    -r, --remember
        --remember-session
"""


@pytest.mark.parametrize(
    ("paths", "help_text", "missing"),
    (
        (frozenset(), TUIGREET_HELP, "/usr/bin/tuigreet"),
        (
            frozenset({PurePosixPath("/usr/bin/tuigreet")}),
            TUIGREET_HELP.replace("        --remember-session\n", ""),
            "--remember-session",
        ),
    ),
)
def test_missing_tuigreet_contract_stops_with_the_package(
    paths: frozenset[PurePosixPath], help_text: str, missing: str
) -> None:
    recorder = PackageRecorder()
    recorder.package_paths["gui-apps/tuigreet"] = paths
    recorder.help["/usr/bin/tuigreet"] = help_text
    operation = plan_packages.VerifyCommandOptions(
        package="gui-apps/tuigreet",
        command=PurePosixPath("/usr/bin/tuigreet"),
        options=("--time", "--remember", "--remember-session", "--sessions", "--xsessions"),
    )

    with pytest.raises(CommandFailed, match="gui-apps/tuigreet") as stopped:
        operation.apply(recorder)

    assert missing in str(stopped.value)


def test_vdb_contents_parser_keeps_paths_with_spaces() -> None:
    from gentoo_install.exec.packages import parse_contents

    sample = (
        "dir /usr/share/example sessions\n"
        "obj /usr/share/example sessions/plasma.desktop deadbeef 1786090234\n"
        "sym /usr/bin/example greeter -> ../libexec/example greeter 1786090234\n"
    )

    assert parse_contents(sample) == frozenset(
        {
            PurePosixPath("/usr/share/example sessions"),
            PurePosixPath("/usr/share/example sessions/plasma.desktop"),
            PurePosixPath("/usr/bin/example greeter"),
        }
    )


def test_chromium_help_need_not_list_internal_feature_switches() -> None:
    group = load_catalog()["chromium"]
    wanted = group.wayland_files[0]
    recorder = PackageRecorder()
    recorder.package_paths["www-client/chromium-common"] = frozenset({wanted.path})
    recorder.package_paths["www-client/chromium"] = frozenset(
        {PurePosixPath("/usr/bin/chromium")}
    )
    recorder.help["/usr/bin/chromium"] = "Usage: chromium [options]\n"

    for operation in plan_packages._wayland_file_operations(group, wanted):
        operation.apply(recorder)

    assert recorder.files[wanted.path] == f"{wanted.content}\n"


def test_missing_fcitx_launcher_stops_before_kwin_write() -> None:
    installation = config()
    selected = replace(
        installation,
        packages=replace(
            installation.packages,
            desktop="plasma",
            applications=("fcitx5", "rime"),
        ),
    )
    operations = plan_packages.build(selected, load_catalog())
    write_position = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, plan_packages.ConfigureKwinInputMethod)
    )
    verification = operations[write_position - 1]
    launcher = PurePosixPath("/usr/share/applications/fcitx5-wayland-launcher.desktop")

    assert isinstance(verification, plan_packages.VerifyPackagePaths)
    assert verification.package == "app-i18n/fcitx"
    assert verification.paths == (launcher,)
    with pytest.raises(CommandFailed, match="app-i18n/fcitx") as stopped:
        verification.apply(PackageRecorder())

    assert str(launcher) in str(stopped.value)


def test_selected_package_contracts_are_verified_before_writes() -> None:
    installation = config()
    selected = replace(
        installation,
        packages=replace(
            installation.packages,
            desktop="plasma",
            display_manager="greetd",
            applications=("fcitx5", "rime", "chromium"),
        ),
    )

    operations = plan_packages.build(selected, load_catalog())
    paths = [one for one in operations if isinstance(one, plan_packages.VerifyPackagePaths)]
    commands = [one for one in operations if isinstance(one, plan_packages.VerifyCommandOptions)]
    directories = [
        one for one in operations if isinstance(one, plan_packages.VerifySessionDirectories)
    ]

    assert any(
        one.package == "gui-libs/greetd"
        and PurePosixPath("/etc/greetd/config.toml") in one.paths
        for one in paths
    )
    assert any(
        one.package == "www-client/chromium-common"
        and PurePosixPath("/etc/chromium/default") in one.paths
        for one in paths
    )
    assert any(
        one.package == "www-client/chromium"
        and PurePosixPath("/usr/bin/chromium") in one.paths
        for one in paths
    )
    assert any(
        one.package == "app-i18n/fcitx"
        and PurePosixPath("/usr/share/applications/fcitx5-wayland-launcher.desktop")
        in one.paths
        for one in paths
    )
    assert {one.package for one in commands} == {"gui-apps/tuigreet"}
    assert directories
    assert directories[0].package == "kde-plasma/plasma-meta"


def _fragment_classes() -> list[type[plan_packages.WriteForGroup]]:
    """Every group fragment class, so a fourth is covered the day it is written.

    Bound in its own module, which a class a test defines and throws away is
    not: a refused declaration stays in `__subclasses__` until it is collected.
    """
    found: list[type[plan_packages.WriteForGroup]] = []
    pending = list(plan_packages.WriteForGroup.__subclasses__())
    while pending:
        one = pending.pop()
        pending += one.__subclasses__()
        if getattr(sys.modules[one.__module__], one.__name__, None) is one:
            found.append(one)
    return found


def test_one_group_fragment_writes_one_describe() -> None:
    """Three classes carried the same `group` and `describe` word for word, and
    an AST scan of the tree found two of them identical. The shared half lives
    in one place, and each subclass names only what differs."""
    base = plan_packages.WriteForGroup
    kinds = {
        plan_packages.WriteGroupKeywords: ("accept", "package.accept_keywords"),
        plan_packages.WriteGroupLicense: ("accept", "package.license"),
        plan_packages.WriteGroupUse: ("ask for", "package.use"),
    }
    found = _fragment_classes()
    assert set(kinds) <= set(found), found
    for cls, (verb, directory) in kinds.items():
        assert (cls.verb, cls.directory.value) == (verb, directory), cls
    for cls in found:
        assert issubclass(cls, base), cls
        # The shared half is inherited: an override here is the duplication
        # coming back, and nothing else would notice.
        assert cls.describe is base.describe, cls
        assert vars(cls).get("group") is None, cls
        written = cls(group="chat", lines=("one", "two"))
        assert written.describe() == f"{cls.verb} one; two for the chat group", cls
        assert str(written.path) == f"/etc/portage/{cls.directory.value}/chat", cls
    # Two classes writing one directory collide on the file a group is named
    # after, so each fragment class owns its own.
    assert len({cls.directory for cls in found}) == len(found), found


def test_a_group_fragment_declaring_half_of_the_pair_is_refused_at_import() -> None:
    """`WriteForGroup.__init__` reads both ClassVars, so a subclass that
    declares one used to build a plan and raise `AttributeError` in
    `Stage.PORTAGE`, an hour after the disks were partitioned."""
    with pytest.raises(ConfigError, match="WriteGroupUnmask declares no verb"):

        class WriteGroupUnmask(plan_packages.WriteForGroup):
            directory: ClassVar[PortageConfigKind] = PortageConfigKind.UNMASK

    with pytest.raises(ConfigError, match="WriteGroupSpoken declares no directory"):

        class WriteGroupSpoken(plan_packages.WriteForGroup):
            verb: ClassVar[str] = "unmask"

    with pytest.raises(ConfigError, match="WriteForGroup declares no directory, verb"):
        plan_packages.WriteForGroup(group="chat", lines=("one",))
