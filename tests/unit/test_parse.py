from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from gentoo_install.errors import ConfigError, DeviceCycle, InvalidSize, UnknownDeviceId
from gentoo_install.model.config import (
    BinhostChannel,
    Bootloader,
    ConsoleFontSize,
    InitSystem,
    KernelSource,
    Keywords,
    MirrorRegion,
)
from gentoo_install.model.device import (
    DeviceId,
    FilesystemType,
    Luks,
    MdRaid,
    Mountpoint,
    RaidLevel,
    RaidMetadata,
    Subvolume,
)
from gentoo_install.model.parse import load, parse
from gentoo_install.model.size import Size

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fixture() -> dict[str, Any]:
    return tomllib.loads((FIXTURES / "btrfs-luks.toml").read_text())


def test_the_fixture_parses_into_every_section() -> None:
    config = load(FIXTURES / "btrfs-luks.toml")

    assert config.config_version == 1
    assert config.system.hostname == "gig"
    assert config.system.locale == "zh_TW.UTF-8"
    assert config.system.init is InitSystem.SYSTEMD
    assert config.system.users[0].name == "zakk"
    assert config.system.users[0].sudo is True

    assert config.portage.keywords is Keywords.STABLE
    assert config.portage.mirrors.region is MirrorRegion.CN
    assert config.portage.mirrors.speed_test is True
    assert config.portage.binhost.community is BinhostChannel.STABLE
    assert config.portage.overlays[0].name == "gentoo-zh"

    assert config.kernel.source is KernelSource.CJK_SOURCE
    assert config.bootloader.kind is Bootloader.GRUB
    assert config.packages.desktop == "plasma"

    assert config.disk.root == DeviceId("mnt-root")
    assert len(config.disk.graph.nodes) == 12


def test_the_device_graph_keeps_its_types_and_sizes() -> None:
    config = load(FIXTURES / "btrfs-luks.toml")
    graph = config.disk.graph

    esp = graph[DeviceId("esp")]
    assert getattr(esp, "size") == Size.parse("512MiB")
    assert getattr(graph[DeviceId("cryptroot")], "size") is None
    assert getattr(graph[DeviceId("rootfs")], "kind") is FilesystemType.BTRFS
    assert {node.name for node in graph.of_type(Subvolume)} == {"@", "@home"}
    assert graph.of_type(Luks)[0].name == "root"
    root = next(node for node in graph.of_type(Mountpoint) if str(node.path) == "/")
    assert root.options == ("compress=zstd:1",)


def test_parsing_needs_no_hardware() -> None:
    raw = fixture()
    raw["disk"]["devices"][0]["selector"] = "/dev/disk/by-id/does-not-exist-anywhere"
    assert parse(raw).disk.graph.nodes[DeviceId("disk")]


def test_defaults_apply_when_a_section_is_absent() -> None:
    raw = fixture()
    for section in ("system", "portage", "kernel", "bootloader", "packages"):
        del raw[section]
    config = parse(raw)
    assert config.system.hostname == "gentoo"
    assert config.portage.binhost.official is True
    assert config.bootloader.kind is Bootloader.GRUB


def test_a_newer_config_version_is_refused_with_an_actionable_message() -> None:
    raw = fixture()
    raw["config_version"] = 99
    with pytest.raises(ConfigError, match="upgrade the installer"):
        parse(raw)


def test_an_older_config_version_says_there_is_no_migration() -> None:
    raw = fixture()
    raw["config_version"] = 0
    with pytest.raises(ConfigError, match="migration"):
        parse(raw)


@pytest.mark.parametrize(
    ("section", "key"),
    [("system", "hostnam"), ("portage", "keyword"), ("kernel", "sources"), ("bootloader", "type")],
)
def test_a_misspelled_key_is_named_rather_than_ignored(section: str, key: str) -> None:
    raw = fixture()
    raw[section][key] = "whatever"
    with pytest.raises(ConfigError, match=key):
        parse(raw)


def test_every_raid_level_can_be_written_in_the_file() -> None:
    for level in RaidLevel:
        raw = fixture()
        raw["disk"]["devices"].append(
            {"kind": "raid", "id": "md", "members": ["esp"], "level": level.value, "name": "md"}
        )
        assert parse(raw).disk.graph.of_type(MdRaid)[0].level is level


def test_an_array_can_name_the_metadata_version_an_esp_member_needs() -> None:
    raw = fixture()
    raw["disk"]["devices"].append(
        {"kind": "raid", "id": "md", "members": ["esp"], "name": "md", "metadata": "1.0"}
    )
    assert parse(raw).disk.graph.of_type(MdRaid)[0].metadata is RaidMetadata.V1_0


def test_an_array_defaults_to_the_metadata_version_mdadm_picks() -> None:
    raw = fixture()
    raw["disk"]["devices"].append({"kind": "raid", "id": "md", "members": ["esp"], "name": "md"})
    assert parse(raw).disk.graph.of_type(MdRaid)[0].metadata is RaidMetadata.V1_2


def test_the_console_font_and_cjk_switch_are_read_from_the_file() -> None:
    raw = fixture()
    raw["system"]["console_cjk"] = True
    raw["system"]["console_font"] = "16x32"
    config = parse(raw)
    assert config.system.console_cjk is True
    assert config.system.console_font is ConsoleFontSize.SIZE_16X32


def test_an_unknown_top_level_key_is_rejected() -> None:
    raw = fixture()
    raw["diskk"] = {}
    with pytest.raises(ConfigError, match="diskk"):
        parse(raw)


def test_an_unknown_enum_value_lists_what_is_allowed() -> None:
    raw = fixture()
    raw["bootloader"]["kind"] = "lilo"
    with pytest.raises(ConfigError) as caught:
        parse(raw)
    assert "grub" in str(caught.value) and "systemd-boot" in str(caught.value)


def test_a_wrong_type_names_the_key_and_the_type() -> None:
    raw = fixture()
    raw["system"]["hostname"] = 42
    with pytest.raises(ConfigError, match="system.hostname must be a string"):
        parse(raw)


def test_a_missing_required_key_is_reported_with_its_path() -> None:
    raw = fixture()
    del raw["disk"]["devices"][5]["name"]
    with pytest.raises(ConfigError, match=r"disk\.devices\[5\]\.name is required"):
        parse(raw)


def test_an_unknown_node_kind_lists_the_known_ones() -> None:
    raw = fixture()
    raw["disk"]["devices"][0]["kind"] = "floppy"
    with pytest.raises(ConfigError) as caught:
        parse(raw)
    assert "partition" in str(caught.value) and "zpool" in str(caught.value)


def test_a_dangling_device_reference_is_rejected() -> None:
    raw = fixture()
    raw["disk"]["devices"][1]["disk"] = "nowhere"
    with pytest.raises(UnknownDeviceId, match="nowhere"):
        parse(raw)


def test_a_cycle_in_the_devices_is_rejected() -> None:
    raw = fixture()
    raw["disk"]["devices"][5]["backing"] = "rootfs"
    with pytest.raises(DeviceCycle):
        parse(raw)


def test_a_malformed_size_is_rejected_where_it_is_written() -> None:
    raw = fixture()
    raw["disk"]["devices"][2]["size"] = "512 megabytes"
    with pytest.raises(InvalidSize):
        parse(raw)


def test_a_relative_mountpoint_is_rejected() -> None:
    raw = fixture()
    raw["disk"]["devices"][9]["path"] = "efi"
    with pytest.raises(ConfigError, match="must be absolute"):
        parse(raw)


def test_an_empty_device_list_is_rejected() -> None:
    raw = fixture()
    raw["disk"]["devices"] = []
    with pytest.raises(ConfigError, match="nothing to install onto"):
        parse(raw)


def test_a_broken_file_reports_the_path() -> None:
    with pytest.raises(ConfigError, match="no-such-file"):
        load(FIXTURES / "no-such-file.toml")
