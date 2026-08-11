from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.errors import CommandFailed
from gentoo_install.plan import packages as plan_packages
from gentoo_install.plan.packages import build

from .recorder import Recorder

from .layouts import config


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
    assert any(type(operation).__name__ == "ConfigureGnomeInputSources" for operation in operations)
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
