# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.errors import ConfigError, ValidationFailed
from gentoo_install.model.config import (
    DiskMode,
    DiskConfig,
    Bootloader,
    BootloaderConfig,
    Firmware,
    InitSystem,
    Keywords,
    InstallConfig,
    KernelConfig,
    KernelSource,
    Overlay,
    PackagesConfig,
    User,
    PortageConfig,
    SystemConfig,
)
from gentoo_install.model.device import Node, Partition, PartitionRole
from gentoo_install.exec.config import load
from gentoo_install.model.validate import validate
from gentoo_install.plan import disk as plan_disk
from gentoo_install.plan.system import EnableService
from gentoo_install.plan.build import build

#: The overlay every gentoo-zh package needs, and the rule table demands it.
GENTOO_ZH = Overlay(name="gentoo-zh", sync_uri="https://github.com/gentoo-zh/overlay.git")
from gentoo_install.plan.operations import Operation, Stage
from gentoo_install.plan import packages as plan_packages
from gentoo_install.plan.packages import Catalog, Group
from gentoo_install.plan.portage import Emerge
from gentoo_install.plan.render import render, summarise

from .layouts import config, ext4_on_gpt, i, running_layout, zfs_root
from .recorder import Recorder

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CATALOG: Catalog = {"console": Group(name="console", packages=("app-editors/vim",))}


def plan(installation: InstallConfig, catalog: Catalog = CATALOG) -> tuple[Operation, ...]:
    return build(installation, catalog)


def first_index(operations: tuple[Operation, ...], text: str) -> int:
    for index, operation in enumerate(operations):
        if text in operation.describe():
            return index
    raise AssertionError(f"no operation mentions {text!r}")


def test_the_stages_run_in_the_order_the_design_lists() -> None:
    stages = [operation.stage.order for operation in plan(config())]
    assert stages == sorted(stages)


def test_partitions_are_created_in_index_order() -> None:
    nodes: list[Node] = ext4_on_gpt()
    indexes = [
        operation.index
        for operation in plan(config(nodes))
        if isinstance(operation, plan_disk.CreatePartition)
    ]
    assert indexes == sorted(indexes)
    assert len(indexes) == len([node for node in nodes if isinstance(node, Partition)])


def test_a_deeper_mountpoint_is_mounted_after_the_one_it_sits_in() -> None:
    mounted = [
        operation.path
        for operation in plan(config())
        if isinstance(operation, plan_disk.Mount)
    ]
    assert mounted == sorted(mounted, key=lambda path: len(path.parts))
    assert mounted[0] == PurePosixPath("/")


def test_the_kernel_is_installed_before_the_bootloader() -> None:
    operations = plan(config())
    assert first_index(operations, "install the kernel") < first_index(operations, "install the bootloader")


def test_a_service_is_enabled_only_after_its_package_is_merged() -> None:
    desktop = replace(
        config(),
        packages=PackagesConfig(desktop="plasma"),
    )
    catalog: Catalog = {
        "plasma": Group(name="plasma", packages=("kde-plasma/plasma-meta",), services=("sddm.service",))
    }
    operations = plan(desktop, catalog)
    assert first_index(operations, "emerge kde-plasma/plasma-meta") < first_index(operations, "enable sddm")


def test_the_esp_is_formatted_before_it_is_mounted() -> None:
    operations = plan(config())
    assert first_index(operations, "make a vfat filesystem") < first_index(operations, "mount esp")


def test_the_binary_kernel_comes_from_a_binary_host_and_a_patched_one_does_not() -> None:
    binary = plan(replace(config(), kernel=KernelConfig(source=KernelSource.DIST_BIN)))
    patched = plan(
        replace(
            config(),
            kernel=KernelConfig(source=KernelSource.CJK),
            portage=replace(config().portage, overlays=(GENTOO_ZH,)),
        )
    )
    assert "from source" not in [
        operation.describe() for operation in binary if "install the kernel" in operation.describe()
    ][0]
    assert "from source" in [
        operation.describe() for operation in patched if "install the kernel" in operation.describe()
    ][0]


def test_testing_keywords_are_written_before_the_target_is_unmounted() -> None:
    """Both are in the last stage, and the order inside it matters: nothing can
    be written to a filesystem that is no longer mounted."""
    testing = replace(config(), portage=PortageConfig(keywords=Keywords.TESTING))
    operations = plan(testing)
    assert operations[-1].stage is Stage.FINISH
    assert first_index(operations, "ACCEPT_KEYWORDS") < first_index(operations, "unmount everything")


def test_the_target_is_always_unmounted_at_the_end() -> None:
    operations = plan(config())
    assert "unmount everything under the target" in operations[-1].describe()


def test_a_zfs_root_produces_no_grub_operation() -> None:
    zfs = replace(
        config(zfs_root()),
        bootloader=BootloaderConfig(kind=Bootloader.ZFSBOOTMENU, firmware=Firmware.UEFI),
        # ZFSBootMenu lives in this overlay, and the rule table refuses the
        # configuration without it.
        portage=PortageConfig(
            overlays=(Overlay(name="gentoo-zh", sync_uri="https://example.invalid/overlay.git"),)
        ),
    )
    assert not any("GRUB" in operation.describe() for operation in plan(zfs))
    assert any("ZFSBootMenu" in operation.describe() for operation in plan(zfs))


def test_bios_installs_no_efibootmgr() -> None:
    from gentoo_install.model.size import Size

    # A bios-boot partition is a megabyte for GRUB's stage 1.5, and the root
    # is what takes the rest: two partitions with no size describe a disk
    # `sgdisk` refuses, because the first of them runs to the last sector.
    nodes = [
        replace(node, index=3) if isinstance(node, Partition) and node.id == i("rootpart") else node
        for node in ext4_on_gpt()
    ]
    nodes.append(
        Partition(
            id=i("bios"),
            table=i("table"),
            index=2,
            role=PartitionRole.BIOS_BOOT,
            size=Size.parse("2MiB"),
        )
    )
    on_bios = replace(
        config(nodes), bootloader=BootloaderConfig(kind=Bootloader.GRUB, firmware=Firmware.BIOS)
    )
    merged = " ".join(
        operation.describe() for operation in plan(on_bios) if isinstance(operation, Emerge)
    )
    assert "efibootmgr" not in merged


def test_dracut_carries_a_module_for_every_layer_of_the_stack() -> None:
    described = " ".join(operation.describe() for operation in plan(config()))
    assert "tell dracut to carry" not in described  # plain ext4 needs no extra module
    with_luks = " ".join(
        operation.describe() for operation in plan(load(FIXTURES / "btrfs-luks.toml"), load_catalog())
    )
    assert "tell dracut to carry crypt, btrfs" in with_luks


def test_an_openrc_target_gets_netifrc_which_stage3_does_not_carry() -> None:
    base = config()
    openrc = replace(
        base,
        system=replace(base.system, init=InitSystem.OPENRC),
        # The profile follows the init; the validator refuses them disagreeing.
        portage=replace(base.portage, profile="default/linux/amd64/23.0"),
    )
    assert "net-misc/netifrc" in " ".join(operation.describe() for operation in plan(openrc))


def test_a_broken_configuration_never_reaches_an_operation() -> None:
    broken = replace(config(), disk=replace(config().disk, root=i("absent")))
    with pytest.raises(ValidationFailed):
        plan(broken)


def test_an_unknown_package_group_names_what_the_catalog_has() -> None:
    unknown = replace(config(), packages=PackagesConfig(desktop="gnome"))
    with pytest.raises(ConfigError, match="console"):
        plan(unknown)


def test_render_groups_by_stage_and_summarise_counts_them() -> None:
    operations = plan(config())
    text = render(operations)
    assert text.startswith("[partition]\n")
    assert f"{len(operations)} operations" in summarise(operations)


def test_every_operation_describes_itself_in_one_line() -> None:
    for path in sorted(FIXTURES.glob("*.toml")):
        installation = load(path)
        # A conversion derives its whole plan from the running machine, so the
        # fixture alone is not enough to build one.
        layout = running_layout() if installation.disk.mode is DiskMode.IN_PLACE else None
        for operation in build(installation, load_catalog(), layout=layout):
            described = operation.describe()
            assert described and "\n" not in described, f"{type(operation).__name__} in {path.name}"


def test_a_group_from_an_overlay_needs_that_overlay_selected() -> None:
    """Otherwise the first sign is emerge failing an hour into an install that
    has already partitioned the disks."""
    catalog = load_catalog()
    wanted = replace(
        config(), packages=PackagesConfig(applications=("flclash",))
    )
    with pytest.raises(ConfigError, match="gentoo-zh"):
        plan_packages.build(wanted, catalog)

    with_overlay = replace(
        wanted,
        portage=replace(
            wanted.portage,
            overlays=(Overlay(name="gentoo-zh", sync_uri="https://example/overlay.git"),),
        ),
    )
    assert plan_packages.build(with_overlay, catalog)


def test_a_driver_group_adds_its_video_cards_and_writes_no_file() -> None:
    catalog = load_catalog()
    wanted = replace(config(), packages=PackagesConfig(applications=("nvidia",)))
    assert plan_packages.required_video_cards(wanted, catalog) == ("nvidia",)
    written = [
        operation
        for operation in plan_packages.build(wanted, catalog)
        if isinstance(operation, plan_packages.WriteGroupFile)
    ]
    # Nothing at all. `nvidia.conf` is the ebuild's, and a second drop-in of
    # ours would have set `modeset=1`, which 595.84 removed from that file
    # because the driver now defaults to it.
    assert written == []
    assert plan_packages.required_licenses(wanted, catalog) == (
        "@FREE",
        "@BINARY-REDISTRIBUTABLE",
    )


def test_an_input_method_group_configures_fcitx_for_every_user() -> None:
    """fcitx starts with no engine, so a desktop with the packages installed
    still types latin until someone adds one by hand."""
    catalog = load_catalog()
    wanted = replace(
        config(),
        system=replace(config().system, users=(User(name="zakk"),)),
        packages=PackagesConfig(applications=("fcitx5", "rime")),
    )
    recorder = Recorder()
    for operation in plan_packages.build(wanted, catalog):
        if isinstance(
            operation,
            (plan_packages.WriteInputMethodProfile, plan_packages.WriteInputMethodEnvironment),
        ):
            operation.apply(recorder)
    written = {str(path): text for path, text in recorder.files.items()}
    assert "DefaultIM=keyboard-us" in written["/home/zakk/.config/fcitx5/profile"]
    assert "Name=rime" in written["/etc/skel/.config/fcitx5/profile"]
    assert "luna_pinyin" in written["/home/zakk/.local/share/fcitx5/rime/default.custom.yaml"]
    assert "gtk-im-module=fcitx" in written["/home/zakk/.config/gtk-4.0/settings.ini"]
    assert "XMODIFIERS=@im=fcitx" in written["/etc/environment"]


def test_a_wayland_desktop_leaves_the_module_variables_unset() -> None:
    """Setting them under a Wayland compositor makes the candidate window
    blink, because the compositor already drives fcitx over text-input."""
    catalog = load_catalog()
    wanted = replace(
        config(),
        packages=PackagesConfig(desktop="plasma", applications=("fcitx5", "rime")),
    )
    recorder = Recorder()
    for operation in plan_packages.build(wanted, catalog):
        if isinstance(operation, plan_packages.WriteInputMethodEnvironment):
            operation.apply(recorder)
    written = "".join(recorder.files.values())
    assert "XMODIFIERS=@im=fcitx" in written
    assert "GTK_IM_MODULE" not in written and "QT_IM_MODULE=" not in written
    assert "QT_IM_MODULES=" in written


def test_a_display_manager_is_enabled_the_way_each_init_does_it() -> None:
    """openrc runs every one through a single `display-manager` script that
    reads the name from conf.d; `rc-update add sddm` would fail with the disks
    already written."""
    catalog = load_catalog()
    wanted = replace(config(), packages=PackagesConfig(display_manager="sddm"))
    systemd = [one.describe() for one in plan_packages.build(wanted, catalog)]
    assert "enable sddm at boot" in systemd
    assert not any("display-manager" in line for line in systemd)

    openrc = replace(wanted, system=replace(wanted.system, init=InitSystem.OPENRC))
    described = [one.describe() for one in plan_packages.build(openrc, catalog)]
    assert "enable display-manager in the default runlevel" in described
    assert any("gui-libs/display-manager-init" in line for line in described)


def test_no_group_names_a_systemd_unit_file() -> None:
    """openrc has no `.service`, and the suffix reaches `rc-update` unchanged."""
    for group in load_catalog().values():
        for service in group.services:
            assert not service.endswith(".service"), (group.name, service)


def test_a_zfs_root_is_not_refused_for_an_unrelated_encrypted_partition() -> None:
    """`ROOT_ON_ZFS` is scoped to the root's ancestry and `LUKS` was not, so
    the pair matched two devices that have nothing to do with each other."""
    from gentoo_install.model.compat import Trait, traits_of
    from gentoo_install.model.device import DeviceGraph, Luks, Partition, PartitionRole
    from gentoo_install.model.size import Size

    from .layouts import i, zfs_root

    nodes = [*zfs_root()]
    nodes += [
        Partition(id=i("data"), table=i("table"), index=9, role=PartitionRole.DATA,
                  size=Size.parse("1GiB")),
        Luks(id=i("vault"), backing=i("data"), name="vault"),
    ]
    elsewhere = replace(config(nodes), disk=replace(config(nodes).disk, graph=DeviceGraph.build(nodes)))
    assert Trait.LUKS not in traits_of(elsewhere)


def test_remote_unlock_is_allowed_when_the_pool_itself_is_encrypted() -> None:
    """ZFS native encryption prompts for a passphrase exactly as a container
    does, and `early_containers` cannot see it."""
    from gentoo_install.model.compat import Trait, traits_of
    from gentoo_install.model.config import KernelConfig, RemoteUnlock
    from gentoo_install.model.templates import Choice, Layout, build as build_template

    graph, root = build_template(
        Choice(disk="/dev/vda", layout=Layout.WHOLE_DISK_ZFS, passphrase_file="/run/keys/x")
    )
    encrypted = replace(
        config(),
        disk=DiskConfig(graph=graph, root=root),
        kernel=KernelConfig(remote_unlock=RemoteUnlock(enabled=True)),
        system=replace(config().system, authorized_keys=("ssh-ed25519 AAAA k",)),
    )
    assert Trait.NO_ENCRYPTED_CONTAINER not in traits_of(encrypted)


def test_a_versioned_sources_atom_is_still_refused() -> None:
    """The check matched a bare atom, so a version or a slot walked past a rule
    that exists to stop an unbuildable kernel."""
    from gentoo_install.model.config import KernelConfig

    for atom in (
        "sys-kernel/gentoo-sources",
        "=sys-kernel/gentoo-sources-6.12.16",
        "sys-kernel/gentoo-sources:6.12",
        "=sys-kernel/gentoo-sources-6.12.16-r2",
    ):
        named = replace(config(), kernel=KernelConfig(package=atom))
        with pytest.raises(ValidationFailed, match="source tree"):
            validate(named)
    validate(replace(config(), kernel=KernelConfig(package="=sys-kernel/gentoo-kernel-7.1.7-r1")))


def test_a_desktop_on_openrc_gets_the_session_services_systemd_provides() -> None:
    """elogind's init script says `before xdm`, but openrc only orders services
    that are in a runlevel: without these a Plasma login has no seat, no
    polkit and no suspend."""
    catalog = load_catalog()
    openrc = replace(
        config(),
        packages=PackagesConfig(desktop="plasma", display_manager="sddm"),
        system=replace(config().system, init=InitSystem.OPENRC),
    )
    operations = plan_packages.build(openrc, catalog)
    services = [one.service for one in operations if isinstance(one, EnableService)]
    assert set(services) == {"dbus", "elogind", "display-manager"}

    # Every one of them after the emerge that ships it: `rc-update` refuses a
    # service whose package is absent, and neither dbus nor elogind is in a
    # stage3 or in @system. This is the defect that stopped an openrc install
    # at `rc-update add lvm boot`, in a second place.
    described = [one.describe() for one in operations]
    merged = next(at for at, one in enumerate(described) if "session bus" in one)
    for name in ("dbus", "elogind"):
        enabled = next(at for at, one in enumerate(described) if one.startswith(f"enable {name} "))
        assert merged < enabled, name

    systemd = replace(openrc, system=replace(openrc.system, init=InitSystem.SYSTEMD))
    assert [
        one.service
        for one in plan_packages.build(systemd, catalog)
        if isinstance(one, EnableService)
    ] == ["sddm"]


def test_an_openrc_desktop_gets_dbus_and_elogind_without_a_display_manager() -> None:
    """They were emitted only inside `_display_manager`, so a desktop chosen
    with no manager booted to a console with a desktop it cannot start."""
    from dataclasses import replace as _replace
    from pathlib import Path as _Path

    from gentoo_install.data import load_catalog
    from gentoo_install.model.config import InitSystem
    from gentoo_install.exec.config import load
    from gentoo_install.plan.build import build
    from gentoo_install.plan.packages import SESSION_PACKAGES
    from gentoo_install.plan.system import EnableService

    desktop = load(_Path("tests/fixtures/vm-desktop.toml"))
    assert not desktop.packages.display_manager
    openrc = _replace(
        desktop,
        system=_replace(desktop.system, init=InitSystem.OPENRC),
        portage=_replace(desktop.portage, profile="default/linux/amd64/23.0/desktop/plasma"),
    )
    enabled = {
        one.service for one in build(openrc, load_catalog()) if isinstance(one, EnableService)
    }
    assert {service for _, service, _ in SESSION_PACKAGES} <= enabled

    # No desktop, no session services: systemd needs none of this either.
    console = _replace(openrc, packages=_replace(openrc.packages, desktop="", applications=()))
    plain = {
        one.service for one in build(console, load_catalog()) if isinstance(one, EnableService)
    }
    assert not {service for _, service, _ in SESSION_PACKAGES} & plain


def test_each_rime_schema_is_a_group_the_operator_can_tick() -> None:
    """docs/design.md names five separately: three were bundled into `rime`
    and two shipped in no file at all, so wubi86, cangjie5 and jyut6ping3 had
    no row and no config key."""
    from dataclasses import replace as _replace
    from pathlib import Path as _Path

    from gentoo_install.data import load_catalog
    from gentoo_install.exec.config import load
    from gentoo_install.plan.build import build

    catalog = load_catalog()
    named = {schema for group in catalog.values() for schema in group.schemas}
    assert {"luna_pinyin", "bopomofo", "wubi86", "cangjie5", "jyut6ping3"} <= named
    # One schema per group, so ticking one cannot drag in another.
    for name, group in catalog.items():
        assert len(group.schemas) <= 1, name

    desktop = load(_Path("tests/fixtures/vm-desktop.toml"))
    picked = _replace(
        desktop,
        packages=_replace(desktop.packages, applications=("fcitx5", "rime", "rime-cangjie")),
    )
    written = " ".join(one.describe() for one in build(picked, catalog))
    assert "luna_pinyin cangjie5" in written
    assert "bopomofo" not in written


def test_every_engine_the_operator_picked_is_in_the_fcitx_profile() -> None:
    """It wrote the first one and installed the rest, so a desktop that asked
    for Chinese and Japanese typed Chinese and had no way to reach mozc."""
    from dataclasses import replace as _replace
    from pathlib import Path as _Path

    from gentoo_install.data import load_catalog
    from gentoo_install.exec.config import load
    from gentoo_install.plan.build import build
    from gentoo_install.plan.packages import WriteInputMethodProfile

    desktop = load(_Path("tests/fixtures/vm-desktop.toml"))
    picked = _replace(
        desktop,
        packages=_replace(desktop.packages, applications=("fcitx5", "rime", "mozc", "hangul")),
    )
    written = next(
        one for one in build(picked, load_catalog()) if isinstance(one, WriteInputMethodProfile)
    )
    assert written.engines == ("rime", "mozc", "hangul")

    recorder = Recorder()
    written.apply(recorder)
    profile = recorder.files[PurePosixPath("/etc/skel/.config/fcitx5/profile")]
    # The keyboard stays first and stays the default, so a password field does
    # not start composing.
    assert "DefaultIM=keyboard-us" in profile
    for index, name in enumerate(("keyboard-us", "rime", "mozc", "hangul")):
        assert f"[Groups/0/Items/{index}]\nName={name}\n" in profile


def applied(wanted: InstallConfig, *kinds: type) -> Recorder:
    recorder = Recorder()
    for operation in plan_packages.build(wanted, load_catalog()):
        if isinstance(operation, kinds):
            operation.apply(recorder)
    return recorder


def test_a_plasma_wayland_session_starts_the_input_method_from_kwin() -> None:
    """The Virtual keyboard KCM writes this same key. Without it fcitx runs as
    an autostart entry, which does not speak input-method-v2 and integrates
    incorrectly under KWin."""
    wanted = replace(
        config(), packages=PackagesConfig(desktop="plasma", applications=("fcitx5", "rime"))
    )
    written = applied(wanted, plan_packages.ConfigureKwinInputMethod).files
    kwinrc = written[plan_packages.KWIN_DEFAULTS]
    assert "InputMethod=/usr/share/applications/fcitx5-wayland-launcher.desktop" in kwinrc
    assert "VirtualKeyboardEnabled=true" in kwinrc


def test_a_desktop_that_is_not_wayland_writes_no_kwin_default() -> None:
    wanted = replace(
        config(), packages=PackagesConfig(desktop="xfce", applications=("fcitx5", "rime"))
    )
    assert not applied(wanted, plan_packages.ConfigureKwinInputMethod).files


def test_a_session_with_no_input_method_writes_no_kwin_default() -> None:
    """The key is about reaching an engine, so a plain Plasma install has no
    reason to carry it."""
    wanted = replace(config(), packages=PackagesConfig(desktop="plasma"))
    assert not applied(wanted, plan_packages.ConfigureKwinInputMethod).files


def test_chromium_gets_the_flag_that_lets_it_reach_the_input_method() -> None:
    """Without it a candidate window never appears in Chromium under Wayland."""
    wanted = replace(
        config(),
        packages=PackagesConfig(desktop="plasma", applications=("fcitx5", "rime", "chromium")),
    )
    recorder = applied(wanted, plan_packages.AppendWaylandFlags)
    written = {str(path): text for path, text in recorder.files.items()}
    assert "--enable-wayland-ime" in written["/etc/chromium/default"]
    # The launcher picks the ozone platform from XDG_SESSION_TYPE, so forcing
    # it here would break the X11 session on the same install.
    assert "ozone-platform" not in written["/etc/chromium/default"]


def test_the_flag_is_not_added_twice_to_a_file_that_already_has_it() -> None:
    wanted = replace(
        config(),
        packages=PackagesConfig(desktop="plasma", applications=("fcitx5", "rime", "chromium")),
    )
    recorder = Recorder()
    recorder.files[PurePosixPath("/etc/chromium/default")] = (
        'CHROMIUM_FLAGS="${CHROMIUM_FLAGS} --enable-wayland-ime"\n'
    )
    for operation in plan_packages.build(wanted, load_catalog()):
        if isinstance(operation, plan_packages.AppendWaylandFlags):
            operation.apply(recorder)
    assert recorder.files[PurePosixPath("/etc/chromium/default")].count("wayland-ime") == 1


def test_chromium_on_an_x11_desktop_gets_no_wayland_flag() -> None:
    wanted = replace(
        config(),
        packages=PackagesConfig(desktop="xfce", applications=("fcitx5", "rime", "chromium")),
    )
    assert not applied(wanted, plan_packages.AppendWaylandFlags).files


def test_two_input_frameworks_are_refused_rather_than_installed() -> None:
    """Both provide the Gtk and Qt modules and both claim XMODIFIERS, so the
    loser is installed and unreachable."""
    from gentoo_install.errors import ValidationFailed

    wanted = replace(config(), packages=PackagesConfig(applications=("rime", "ibus")))
    assert "pick one" in plan_packages.framework_conflict(wanted, load_catalog())
    with pytest.raises(ValidationFailed, match="two input method frameworks"):
        plan_packages.build(wanted, load_catalog())


def test_one_framework_with_several_engines_is_fine() -> None:
    """Chinese and Japanese together is the ordinary case."""
    wanted = replace(config(), packages=PackagesConfig(applications=("rime", "anthy", "hangul")))
    assert plan_packages.framework_conflict(wanted, load_catalog()) == ""


def test_ibus_gets_its_own_environment_and_no_fcitx_profile() -> None:
    """It is a different framework: writing an fcitx profile for it would
    configure something that is not installed."""
    wanted = replace(config(), packages=PackagesConfig(applications=("ibus",)))
    recorder = applied(
        wanted,
        plan_packages.WriteInputMethodEnvironment,
        plan_packages.WriteInputMethodProfile,
    )
    written = {str(path): text for path, text in recorder.files.items()}
    assert "XMODIFIERS=@im=ibus" in written["/etc/environment"]
    assert "GTK_IM_MODULE=ibus" in written["/etc/environment"]
    assert not any("fcitx5/profile" in name for name in written)


def test_the_framework_group_itself_declares_its_framework() -> None:
    """`fcitx5` carries the toolkit modules and no engine, so a table that
    counted only engine groups saw one framework and let both be installed:
    two sets of Gtk and Qt modules, and XMODIFIERS pointing at the loser."""
    wanted = replace(config(), packages=PackagesConfig(applications=("fcitx5", "ibus")))
    assert "pick one" in plan_packages.framework_conflict(wanted, load_catalog())


def test_every_engine_group_says_which_framework_it_belongs_to() -> None:
    """A group with an engine and no framework would be classified as fcitx by
    default, which is right today and silent when it stops being."""
    catalog = load_catalog()
    named = {name for name, group in catalog.items() if group.input_method}
    assert named
    assert all(catalog[name].input_framework for name in named)


def test_the_nvidia_module_is_built_against_the_dist_kernel() -> None:
    """`dist-kernel` is off by default in linux-mod-r1's IUSE, so without it
    the module is built once and the next kernel upgrade boots to a console
    with no driver. The same reason `sys-fs/zfs` carries the flag."""
    catalog = load_catalog()
    assert catalog["nvidia"].package_use == ("x11-drivers/nvidia-drivers dist-kernel",)
    wanted = replace(config(), packages=PackagesConfig(graphics=("nvidia",)))
    # The whole plan, so the stage sort applies: the request is written in the
    # portage phase and the merge happens in the packages one.
    described = [one.describe() for one in build(wanted, catalog)]
    asked = next(at for at, one in enumerate(described) if "dist-kernel" in one)
    merged = next(at for at, one in enumerate(described) if "nvidia-drivers" in one and "emerge" in one)
    assert asked < merged


def test_only_nvidia_widens_accept_license() -> None:
    """@BINARY-REDISTRIBUTABLE holds every NVIDIA EULA, so an Intel or AMD
    machine declaring it pre-accepts a licence its operator never saw. The
    firmware every install merges has its own per-package acceptance."""
    catalog = load_catalog()
    for name in ("intel", "amdgpu", "radeon", "nouveau", "virtual-machine"):
        assert catalog[name].accept_license == (), name
    assert catalog["nvidia"].accept_license == ("@BINARY-REDISTRIBUTABLE",)


def test_every_group_whose_package_is_proprietary_accepts_its_licence() -> None:
    """A real install stopped at

        ConfigError: the `wechat` group asks for
        `net-im/wechat-universal-bwrap`, which the target's configuration
        masks; the `wemeet` group asks for `net-im/wemeet`, which the
        target's configuration masks

    The refusal was right and the catalog was wrong: both are proprietary and
    neither said so, so `ACCEPT_LICENSE` stayed at the profile's default and
    `portageq best_visible` answered nothing. Each licence below is the
    `LICENSE=` line of that package's ebuild in `gentoo-zh`, read on
    2026-08-19; `flclash` is `GPL-3` and needs nothing."""
    catalog = load_catalog()
    wanted = {
        "wechat": ("all-rights-reserved",),
        "wemeet": ("wemeet_license",),
        "dingtalk": ("all-rights-reserved",),
        "tencent-qq": ("Tencent-QQ",),
    }
    for name, licences in wanted.items():
        assert catalog[name].accept_license == licences, name
    assert catalog["flclash"].accept_license == ()


def test_the_older_amd_group_does_not_suppress_r300() -> None:
    """mesa enables r300 and r600 under the `radeon` umbrella only when
    neither is named explicitly, so `radeon r600` leaves an r300 card on
    llvmpipe."""
    cards = load_catalog()["radeon"].video_cards
    assert "r600" not in cards
    assert set(cards) == {"radeon", "radeonsi"}


def test_the_fcitx_profile_names_an_xkb_layout_and_not_a_console_keymap() -> None:
    """`keyboard-de-latin1` is not an entry fcitx has, so the group's first
    item was invalid and a German desktop typed latin anyway. The default `us`
    is where the two namespaces happen to agree, which hid it."""
    from gentoo_install.plan.packages import xkb_layout

    assert xkb_layout("de-latin1") == "de"
    assert xkb_layout("us") == "us"


@pytest.mark.parametrize("keymap,expected", sorted(plan_packages.XKB_RENAMED.items()))
def test_every_renamed_keymap_differs_from_what_the_prefix_rule_gives(
    keymap: str, expected: str
) -> None:
    """The table exists for the names where the prefix is not the layout; a row
    whose prefix already equals the layout is a row that never fires."""
    from gentoo_install.plan.packages import xkb_layout

    assert xkb_layout(keymap) == expected
    assert keymap != expected


@pytest.mark.parametrize("keymap,expected", sorted(plan_packages.XKB_FAMILIES.items()))
def test_every_family_keymap_carries_no_country_of_its_own(keymap: str, expected: str) -> None:
    """These are layout families rather than country names, so the prefix rule
    has nothing to work with and would fall back to `us`."""
    from gentoo_install.plan.packages import xkb_layout

    assert xkb_layout(keymap) == expected
    assert len(keymap) != 2


def test_a_keymap_the_tables_do_not_know_falls_back_rather_than_inventing() -> None:
    """An invalid layout makes fcitx ignore the group; `us` is what every
    keymap produced before the tables existed."""
    from gentoo_install.plan.packages import XKB_DEFAULT, xkb_layout

    assert xkb_layout("wobble9") == XKB_DEFAULT
    assert xkb_layout("") == XKB_DEFAULT


def test_a_display_manager_asks_for_the_seat_flag_its_init_provides() -> None:
    """lightdm and gdm carry `^^ ( elogind systemd )` with neither on by
    default, so a profile that sets neither refuses the merge; requesting the
    wrong one is worse, because it is use-masked on that profile."""
    catalog = load_catalog()
    for init, flag in ((InitSystem.OPENRC, "elogind"), (InitSystem.SYSTEMD, "systemd")):
        wanted = replace(
            config(),
            system=replace(config().system, init=init),
            packages=PackagesConfig(desktop="xfce", display_manager="lightdm"),
        )
        lines = [
            one.lines
            for one in plan_packages.build(wanted, catalog)
            if isinstance(one, plan_packages.WriteGroupUse) and one.group == "lightdm"
        ]
        # The PAM stack takes the same flag: without it the session lightdm
        # starts registers with no seat.
        assert lines == [(f"x11-misc/lightdm {flag}", f"sys-auth/pambase {flag}")], init


def test_the_greeter_is_not_given_a_flag_it_does_not_have() -> None:
    """`x11-misc/lightdm-gtk-greeter` has no seat flag, and a package.use line
    naming one is warned about and ignored."""
    catalog = load_catalog()
    wanted = replace(
        config(), packages=PackagesConfig(desktop="xfce", display_manager="lightdm")
    )
    written = "".join(
        " ".join(one.lines)
        for one in plan_packages.build(wanted, catalog)
        if isinstance(one, plan_packages.WriteGroupUse) and one.group == "lightdm"
    )
    assert "greeter" not in written


def test_the_input_method_variables_reach_a_session_a_display_manager_starts() -> None:
    """`environment.d(5)` under APPLICABILITY: those files reach what
    `systemd --user` starts. A session sddm, lightdm or greetd launches is a
    `systemd.scope`, so `XMODIFIERS` never arrived and the candidate window
    never appeared. `pam_env` reads `/etc/environment` at every PAM login."""
    from gentoo_install.model.config import InitSystem
    from gentoo_install.plan.packages import ENVIRONMENT_FILE, WriteInputMethodEnvironment

    assert str(ENVIRONMENT_FILE[InitSystem.SYSTEMD]) == "/etc/environment"
    recorder = Recorder()
    WriteInputMethodEnvironment(
        init=InitSystem.SYSTEMD, framework="fcitx", session=plan_packages.Session.X11
    ).apply(recorder)
    written = {str(path): text for path, text in recorder.files.items()}
    assert "XMODIFIERS=@im=fcitx" in written["/etc/environment"]


def test_the_anthy_group_names_no_backend_of_its_own() -> None:
    """`fcitx-anthy-5.1.10` depends on `app-i18n/anthy-unicode` and 5.1.8 and
    older on `app-i18n/anthy`. Naming either one here merges a second Japanese
    input backend that the fcitx actually installed does not address."""
    from gentoo_install.data import load_catalog

    group = load_catalog()["anthy"]
    assert group.packages == ("app-i18n/fcitx-anthy",)
    assert not any(name.endswith(("/anthy", "/anthy-unicode")) for name in group.packages)


def test_every_recorder_answer_carries_an_exit_code() -> None:
    """The real `Context.run_in_target` answers a `CommandOutput`, and three
    of the double's branches answered a bare `str`: a failing command under
    `check=False`, an injected `answering`, and the fallback. A caller that
    degrades or retries on a non-zero code was therefore handed an object with
    no `returncode`, and a non-empty string reads as success.
    """
    from gentoo_install.plan.operations import CommandOutput

    recorder = Recorder(replies={"grep": "found"}, failures={"lsblk"})
    answers = [
        recorder.run(["grep", "one"]),
        recorder.run(["unconfigured"]),
        recorder.run_in_target(["grep", "one"]),
        recorder.run_in_target(["unconfigured"]),
        recorder.run_in_target(["lsblk"], check=False),
        recorder.run_in_target(["portageq", "best_visible", "/", "sys-apps/portage"]),
    ]
    assert all(isinstance(one, CommandOutput) for one in answers), [
        type(one).__name__ for one in answers
    ]

    injected = Recorder(answering=lambda argv: "said" if argv[0] == "hostnamectl" else None)
    said = injected.run_in_target(["hostnamectl"])
    assert isinstance(said, CommandOutput), type(said).__name__
    assert said == "said"


def test_a_failing_command_under_no_check_answers_a_non_zero_code() -> None:
    """`check=False` is the path a caller takes when it means to read the code
    itself, so the double answering `0` there makes the failure invisible.
    """
    recorder = Recorder(failures={"findmnt"})

    answered = recorder.run_in_target(["findmnt", "/mnt/gentoo"], check=False)
    assert answered.returncode != 0, answered.returncode
    assert recorder.run_in_target(["ls"]).returncode == 0


def test_a_configured_exit_code_is_not_rebuilt_as_success() -> None:
    """`replies` is typed for `str` and `CommandOutput` is one, so wrapping
    every reply discarded the code a test had configured. `VerifyPackages`
    reads exactly that code to tell a missing ebuild from a working search.
    """
    from gentoo_install.plan.operations import CommandOutput

    recorder = Recorder(replies={"emerge": CommandOutput("no ebuild matches", 1)})

    assert recorder.run_in_target(["emerge", "--pretend"]).returncode == 1
    assert recorder.run(["emerge", "--pretend"]).returncode == 1
