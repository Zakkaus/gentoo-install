from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.errors import ConfigError, ValidationFailed
from gentoo_install.model.config import (
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
from gentoo_install.model.parse import load
from gentoo_install.plan import disk as plan_disk
from gentoo_install.plan.build import build
from gentoo_install.plan.operations import Operation, Stage
from gentoo_install.plan import packages as plan_packages
from gentoo_install.plan.packages import Catalog, Group
from gentoo_install.plan.portage import Emerge
from gentoo_install.plan.render import render, summarise

from .layouts import config, ext4_on_gpt, i, zfs_root
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
    patched = plan(replace(config(), kernel=KernelConfig(source=KernelSource.CJK_SOURCE)))
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
    nodes = ext4_on_gpt()
    nodes.append(
        Partition(
            id=i("bios"),
            table=i("table"),
            index=3,
            role=PartitionRole.BIOS_BOOT,
            size=None,
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
        system=SystemConfig(init=InitSystem.OPENRC),
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
        for operation in build(load(path), load_catalog()):
            described = operation.describe()
            assert described and "\n" not in described, f"{type(operation).__name__} in {path.name}"


def test_a_group_from_an_overlay_needs_that_overlay_selected() -> None:
    """Otherwise the first sign is emerge failing an hour into an install that
    has already partitioned the disks."""
    catalog = load_catalog()
    wanted = replace(
        config(), packages=PackagesConfig(applications=("rime-ice",))
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


def test_a_driver_group_adds_its_video_cards_and_its_drop_in() -> None:
    catalog = load_catalog()
    wanted = replace(config(), packages=PackagesConfig(applications=("nvidia",)))
    assert plan_packages.required_video_cards(wanted, catalog) == ("nvidia",)
    written = [
        operation
        for operation in plan_packages.build(wanted, catalog)
        if isinstance(operation, plan_packages.WriteGroupFile)
    ]
    assert [str(entry.file.path) for entry in written] == ["/etc/modprobe.d/nvidia.conf"]


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
    assert "XMODIFIERS=@im=fcitx" in written["/etc/environment.d/90-input-method.conf"]


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
