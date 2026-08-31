# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.errors import ConfigError, ValidationFailed
from gentoo_install.data import load_catalog, load_timezones
from gentoo_install.model import compat
from gentoo_install.model.config import (
    Bootloader,
    BootloaderConfig,
    DiskConfig,
    DiskMode,
    FirstBoot,
    ImageFormat,
    InitSystem,
    InstallConfig,
    KernelConfig,
    MemoryLaunch,
    MemoryMode,
    PackagesConfig,
    PortageConfig,
    SystemConfig,
)
from gentoo_install.model.device import DeviceGraph
from gentoo_install.model.device import (
    Existing,
    Filesystem,
    LogicalVolume,
    Mountpoint,
    Node,
    Partition,
    PartitionRole,
    PartitionTable,
    VolumeGroup,
)
from gentoo_install.model.size import Size
from gentoo_install.exec.config import load
from gentoo_install.exec.probe import amd64_profiles, profiles_from_eselect
from gentoo_install.model.templates import Choice
from gentoo_install.model.validate import (
    CLOSED_SYSTEM_FIELDS,
    HTTP_SCHEMES,
    PACKAGE_GROUP_FIELDS,
    validate,
    validate_memory_launch,
    zfs_kernel_ceiling,
)
from gentoo_install.model.parse import parse

from .layouts import encrypted_root, config, ext4_on_gpt, i, unlockable_root, zfs_root

_DISK_MODE_FIELD_CASES: tuple[tuple[DiskMode, str], ...] = (
    (DiskMode.PARTITION, "image"),
    (DiskMode.PARTITION, "size"),
    (DiskMode.PARTITION, "wipe"),
    (DiskMode.PARTITION, "source"),
    (DiskMode.PARTITION, "source_format"),
    (DiskMode.PARTITION, "destination"),
    (DiskMode.IN_PLACE, "devices"),
    (DiskMode.IN_PLACE, "root"),
    (DiskMode.IN_PLACE, "simple"),
    (DiskMode.IN_PLACE, "image"),
    (DiskMode.IN_PLACE, "size"),
    (DiskMode.IN_PLACE, "wipe"),
    (DiskMode.IN_PLACE, "source"),
    (DiskMode.IN_PLACE, "source_format"),
    (DiskMode.IN_PLACE, "destination"),
    (DiskMode.IMAGE, "source"),
    (DiskMode.IMAGE, "source_format"),
    (DiskMode.IMAGE, "destination"),
    (DiskMode.DD, "devices"),
    (DiskMode.DD, "root"),
    (DiskMode.DD, "simple"),
    (DiskMode.DD, "image"),
    (DiskMode.DD, "size"),
    (DiskMode.DD, "wipe"),
)


def _mode_config(mode: DiskMode) -> InstallConfig:
    if mode is DiskMode.PARTITION:
        return config(ext4_on_gpt())
    if mode is DiskMode.IMAGE:
        return image_config()
    if mode is DiskMode.DD:
        return dd_config()
    installation = config()
    return replace(
        installation,
        disk=replace(
            installation.disk,
            graph=DeviceGraph.build(()),
            root=i(""),
            mode=DiskMode.IN_PLACE,
        ),
    )


def _with_disk_field(disk: DiskConfig, field: str) -> DiskConfig:
    if field == "devices":
        return replace(disk, graph=DeviceGraph.build(ext4_on_gpt()))
    if field == "root":
        return replace(disk, root=i("mnt-root"))
    if field == "simple":
        return replace(disk, simple=Choice(disk="/dev/disk/by-id/virtio-target"))
    if field == "image":
        return replace(disk, image="/run/target.raw")
    if field == "size":
        return replace(disk, size=Size.parse("20GiB"))
    if field == "wipe":
        return replace(disk, wipe=True)
    if field == "source":
        return replace(disk, source="/run/source.raw")
    if field == "source_format":
        return replace(disk, source_format=ImageFormat.ZSTD)
    if field == "destination":
        return replace(disk, destination="/dev/disk/by-id/target")
    raise AssertionError(f"no value for disk.{field}")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

_SHIPPED_CATALOG = load_catalog()
_SHIPPED_TIMEZONES = load_timezones()


def _validate_with_shipped_values(installation: InstallConfig) -> None:
    validate(
        installation,
        available_timezones=_SHIPPED_TIMEZONES,
        available_package_groups=_SHIPPED_CATALOG,
    )


def image_config() -> InstallConfig:
    installation = config()
    image = "/var/tmp/target.raw"
    graph = DeviceGraph.build(
        replace(node, selector=image) if isinstance(node, Existing) else node
        for node in installation.disk.graph.nodes.values()
    )
    return replace(
        installation,
        disk=replace(
            installation.disk,
            graph=graph,
            mode=DiskMode.IMAGE,
            image=image,
            size=Size.parse("20GiB"),
        ),
    )

def dd_config() -> InstallConfig:
    installation = config()
    return replace(
        installation,
        system=SystemConfig(),
        packages=PackagesConfig(),
        portage=PortageConfig(),
        kernel=KernelConfig(),
        bootloader=BootloaderConfig(),
        disk=replace(
            installation.disk,
            graph=DeviceGraph.build(()),
            root=i(""),
            mode=DiskMode.DD,
            source="/run/image.raw",
            destination="/dev/disk/by-id/virtio-target",
        ),
    )


@pytest.mark.parametrize(("mode", "field"), _DISK_MODE_FIELD_CASES)
def test_every_disk_mode_refuses_other_fields(mode: DiskMode, field: str) -> None:
    installation = _mode_config(mode)

    with pytest.raises(
        ValidationFailed,
        match=rf"disk\.{field} is not allowed in {mode.value} mode",
    ):
        validate(replace(installation, disk=_with_disk_field(installation.disk, field)))


def test_disk_mode_field_cases_cover_the_table() -> None:
    from gentoo_install.model.validate import DISK_MODE_FIELDS

    assert set(DISK_MODE_FIELDS) == set(DiskMode)
    all_fields = set().union(*DISK_MODE_FIELDS.values())
    expected = {
        (mode, field)
        for mode, allowed in DISK_MODE_FIELDS.items()
        for field in all_fields - allowed
    }
    assert set(_DISK_MODE_FIELD_CASES) == expected


def test_every_port_is_judged_against_one_range() -> None:
    """The memory environment, proxy, and remote unlock share one TCP range."""
    import ast
    import inspect

    assert compat.PORTS == range(1, 65536)
    assert compat.port_problem("a port", 1) == ""
    assert compat.port_problem("a port", 65535) == ""
    assert "between 1 and 65535" in compat.port_problem("a port", 0)
    assert "between 1 and 65535" in compat.port_problem("a port", 65536)

    tree = ast.parse(inspect.getsource(compat))
    spelled = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in (65535, 65536)
    ]
    assert len(spelled) == 1, [node.lineno for node in spelled]


def test_a_broken_proxy_is_refused_in_dd_mode_too() -> None:
    """`dd` reads a local path and uses no proxy, so its validation returned
    before the proxy was looked at: a host with no port was refused in every
    other mode and accepted here.
    """
    from gentoo_install.model.config import ProxyConfig, ProxyKind

    broken = replace(
        dd_config(),
        proxy=ProxyConfig(kind=ProxyKind.HTTP, host="proxy.example", port=0),
    )
    with pytest.raises(ValidationFailed, match="proxy port"):
        validate(broken)

    # And a sound one still validates, in this mode as in the others.
    working = replace(
        dd_config(),
        proxy=ProxyConfig(kind=ProxyKind.HTTP, host="proxy.example", port=3128),
    )
    validate(working)


@pytest.mark.parametrize(
    ("username", "password", "field", "character"),
    (
        ('operator"quote', "", "username", "double quote"),
        (r"operator\slash", "", "username", "backslash"),
        ("operator\ncontrol", "", "username", "control character U+000A"),
        ("", 'p"q', "password", "double quote"),
        ("", r"password\slash", "password", "backslash"),
        ("", "password\ncontrol", "password", "control character U+000A"),
    ),
)
def test_proxy_credentials_reject_curl_config_characters(
    username: str, password: str, field: str, character: str
) -> None:
    from gentoo_install.model.config import ProxyConfig, ProxyKind

    broken = replace(
        config(),
        proxy=ProxyConfig(
            kind=ProxyKind.HTTP,
            host="proxy.example",
            port=3128,
            username=username,
            password=password,
        ),
    )
    with pytest.raises(ValidationFailed, match=rf"{field}.*{re.escape(character)}"):
        validate(broken)


def test_the_profile_probe_reads_current_amd64_paths(tmp_path: Path) -> None:
    desc = tmp_path / "profiles.desc"
    desc.write_text(
        "amd64 default/linux/amd64/24.0 stable\n"
        "amd64 default/linux/amd64/24.0/systemd stable\n"
        "amd64 default/linux/amd64/24.0/x32 dev\n"
        "arm64 default/linux/arm64/24.0 stable\n",
        encoding="utf-8",
    )
    assert amd64_profiles(desc) == (
        "default/linux/amd64/24.0",
        "default/linux/amd64/24.0/systemd",
    )


def test_an_in_place_configuration_validates_without_a_device_graph() -> None:
    installation = replace(config(), disk=replace(config().disk, graph=DeviceGraph.build(()), root=i(""), mode=DiskMode.IN_PLACE))
    validate(installation)


def test_in_place_keeps_the_rules_that_need_no_device_graph() -> None:
    """The graph rules are skipped in this mode because there is no graph, and
    that used to take the configuration's own rules with them: a conversion
    could lock root, name no user and authorise no key, and the machine it
    produced had no way in with an exit code of 0."""
    base = replace(
        config(), disk=replace(config().disk, graph=DeviceGraph.build(()), root=i(""), mode=DiskMode.IN_PLACE)
    )
    locked = replace(
        base, system=replace(base.system, root_password_hash="", users=(), authorized_keys=())
    )
    with pytest.raises(ValidationFailed, match="no user password and no authorised ssh key"):
        validate(locked)
    unlocking = replace(
        base,
        system=replace(base.system, authorized_keys=()),
        kernel=replace(
            base.kernel, remote_unlock=replace(base.kernel.remote_unlock, enabled=True)
        ),
    )
    with pytest.raises(ValidationFailed, match="no authorised ssh key"):
        validate(unlocking)
    # The other direction: a rule that reads the graph must not fire here, or
    # every conversion is refused for a layout it was never given.
    validate(base)


def test_an_in_place_configuration_rejects_zfsbootmenu() -> None:
    base = _mode_config(DiskMode.IN_PLACE)
    installation = replace(
        base,
        bootloader=replace(base.bootloader, kind=Bootloader.ZFSBOOTMENU),
    )
    with pytest.raises(ValidationFailed, match="in-place conversion has no device graph"):
        validate(installation)


def test_in_place_mode_rejects_a_device_graph() -> None:
    installation = replace(config(), disk=replace(config().disk, mode=DiskMode.IN_PLACE))
    with pytest.raises(ValidationFailed, match="disk.devices is not allowed"):
        validate(installation)


def test_in_place_mode_rejects_a_graph_root() -> None:
    installation = replace(config(), disk=replace(config().disk, graph=DeviceGraph.build(()), mode=DiskMode.IN_PLACE))
    with pytest.raises(ValidationFailed, match="disk.root is not allowed"):
        validate(installation)


def test_an_image_configuration_validates() -> None:
    validate(image_config())


def test_a_dd_configuration_validates_without_a_target_layout() -> None:
    validate(dd_config())


def test_dd_mode_requires_an_image_source() -> None:
    installation = dd_config()
    with pytest.raises(ValidationFailed, match="disk.source is required"):
        validate(replace(installation, disk=replace(installation.disk, source="")))


def test_dd_mode_requires_a_destination_disk() -> None:
    installation = dd_config()
    with pytest.raises(ValidationFailed, match="disk.destination is required"):
        validate(replace(installation, disk=replace(installation.disk, destination="")))


def test_dd_mode_requires_a_device_destination() -> None:
    installation = dd_config()
    with pytest.raises(ValidationFailed, match="disk.destination must name a device"):
        validate(replace(installation, disk=replace(installation.disk, destination="target.raw")))


def test_dd_mode_refuses_its_source_as_the_destination() -> None:
    installation = dd_config()
    with pytest.raises(ValidationFailed, match="disk.source and disk.destination must differ"):
        validate(
            replace(
                installation,
                disk=replace(installation.disk, destination=installation.disk.source),
            )
        )




def test_image_mode_requires_an_image_file() -> None:
    installation = image_config()
    with pytest.raises(ValidationFailed, match="disk.image is required"):
        validate(replace(installation, disk=replace(installation.disk, image="")))


def test_image_mode_requires_a_size() -> None:
    installation = image_config()
    with pytest.raises(ValidationFailed, match="disk.size is required"):
        validate(replace(installation, disk=replace(installation.disk, size=None)))


def test_image_mode_refuses_a_physical_disk_selector() -> None:
    installation = image_config()
    graph = DeviceGraph.build(
        replace(node, selector="/dev/disk/by-id/virtio-target")
        if isinstance(node, Existing)
        else node
        for node in installation.disk.graph.nodes.values()
    )
    with pytest.raises(ValidationFailed, match="physical disk"):
        validate(replace(installation, disk=replace(installation.disk, graph=graph)))


def test_image_mode_refuses_a_device_path_as_the_image_file() -> None:
    installation = image_config()
    device = "/dev/vda"
    graph = DeviceGraph.build(
        replace(node, selector=device) if isinstance(node, Existing) else node
        for node in installation.disk.graph.nodes.values()
    )
    with pytest.raises(ValidationFailed, match="disk.image must name a file"):
        validate(
            replace(
                installation,
                disk=replace(installation.disk, graph=graph, image=device),
            )
        )


def test_partition_mode_is_unaffected() -> None:
    validate(config())
def test_a_plain_uefi_install_validates() -> None:
    validate(config())


def test_lowram_refuses_a_layout_that_needs_zfs() -> None:
    with pytest.raises(ValidationFailed) as refused:
        validate_memory_launch(config(zfs_root()), MemoryLaunch(MemoryMode.LOWRAM))
    said = str(refused.value)
    assert "layout needs ZFS" in said
    assert "Alpine netboot kernel has no zfs.ko" in said
    assert "--ram" in said


@pytest.mark.parametrize("port", (0, 65536))
def test_memory_launch_refuses_ssh_ports_outside_the_tcp_range(port: int) -> None:
    with pytest.raises(ValidationFailed, match="--ssh-port must be between 1 and 65535"):
        validate_memory_launch(
            config(), MemoryLaunch(MemoryMode.RAM, ssh_key="ssh-ed25519 key", ssh_port=port)
        )


def test_a_key_authenticates_to_the_memory_environment_without_a_password() -> None:
    validate_memory_launch(
        config(),
        MemoryLaunch(MemoryMode.RAM, ssh_key="ssh-ed25519 AAAA"),
    )


def test_an_ssh_port_needs_a_payload_credential() -> None:
    with pytest.raises(ValidationFailed, match="--ssh-key or --root-password"):
        validate_memory_launch(config(), MemoryLaunch(MemoryMode.RAM, ssh_port=2222))
    validate_memory_launch(
        config(),
        MemoryLaunch(MemoryMode.RAM, ssh_key="ssh-ed25519 AAAA", ssh_port=2222),
    )


def test_a_payload_password_can_contain_whitespace() -> None:
    validate_memory_launch(config(), MemoryLaunch(MemoryMode.RAM, root_password="summer meadow"))


@pytest.mark.parametrize("password", ("first\nsecond", "first\rsecond"))
def test_a_payload_password_rejects_record_separators(password: str) -> None:
    with pytest.raises(ValidationFailed, match="cannot contain a newline"):
        validate_memory_launch(config(), MemoryLaunch(MemoryMode.RAM, root_password=password))


@pytest.mark.parametrize(
    "path",
    sorted(FIXTURES.rglob("*.toml")),
    ids=lambda path: str(path.relative_to(FIXTURES)),
)
def test_every_shipped_fixture_validates_closed_values(path: Path) -> None:
    _validate_with_shipped_values(load(path))


_CLOSED_SYSTEM_FIELD_CASES: tuple[str, ...] = (
    "timezone",
    "hostname",
    "first_boot.url",
)


def test_closed_system_value_cases_cover_the_rule_table() -> None:
    assert set(_CLOSED_SYSTEM_FIELD_CASES) == CLOSED_SYSTEM_FIELDS


@pytest.mark.parametrize("field", _CLOSED_SYSTEM_FIELD_CASES)
def test_a_closed_system_value_outside_its_namespace_is_refused(field: str) -> None:
    installation = config()
    system = installation.system
    if field == "timezone":
        system = replace(system, timezone="Mars/Olympus")
    elif field == "hostname":
        system = replace(system, hostname="bad_host")
    elif field == "first_boot.url":
        system = replace(system, first_boot=FirstBoot(url="file:///run/once.sh"))
    else:
        raise AssertionError(f"no invalid value for system.{field}")
    with pytest.raises(ValidationFailed, match=rf"system\.{field}"):
        _validate_with_shipped_values(replace(installation, system=system))


@pytest.mark.parametrize(
    "locale", ("en_US.UTF-8", "zh_TW.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8", "pt_BR.UTF-8")
)
def test_a_system_locale_outside_the_interface_languages_validates(locale: str) -> None:
    """The installed system's language is a separate choice from the
    interface's, and the set `locale-gen` can produce belongs to the target:
    `GenerateLocales` reads `locale --all-locales` and raises there."""
    installation = config()
    _validate_with_shipped_values(
        replace(
            installation,
            system=replace(installation.system, locale=locale, locales=(locale,)),
        )
    )


@pytest.mark.parametrize("scheme", sorted(HTTP_SCHEMES))
def test_each_supported_first_boot_url_scheme_validates(scheme: str) -> None:
    installation = config()
    _validate_with_shipped_values(
        replace(
            installation,
            system=replace(
                installation.system,
                first_boot=FirstBoot(url=f"{scheme}://example.test/once.sh"),
            ),
        )
    )


def test_hostname_uses_rfc_1123_label_and_length_boundaries() -> None:
    hostname = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))
    assert len(hostname) == 253
    installation = config()
    _validate_with_shipped_values(
        replace(
            installation,
            system=replace(installation.system, hostname=hostname),
        )
    )
    with pytest.raises(ValidationFailed, match="system.hostname"):
        _validate_with_shipped_values(
            replace(
                installation,
                system=replace(installation.system, hostname=f"{hostname}a"),
            )
        )


_PACKAGE_GROUP_CASES: tuple[str, ...] = (
    "desktop",
    "applications",
    "graphics",
    "display_manager",
)


def test_package_group_cases_cover_the_rule_table() -> None:
    assert _PACKAGE_GROUP_CASES == PACKAGE_GROUP_FIELDS


@pytest.mark.parametrize("field", _PACKAGE_GROUP_CASES)
def test_an_unknown_package_group_is_refused_and_named(field: str) -> None:
    unknown = "not-a-shipped-group"
    packages = PackagesConfig()
    if field == "desktop":
        packages = replace(packages, desktop=unknown)
    elif field == "applications":
        packages = replace(packages, applications=(unknown,))
    elif field == "graphics":
        packages = replace(packages, graphics=(unknown,))
    elif field == "display_manager":
        packages = replace(packages, display_manager=unknown)
    else:
        raise AssertionError(f"no package selector for {field}")
    with pytest.raises(ValidationFailed, match=rf"packages\.{field} is '{unknown}'"):
        _validate_with_shipped_values(replace(config(), packages=packages))


def test_extra_package_atoms_remain_open() -> None:
    _validate_with_shipped_values(
        replace(config(), packages=PackagesConfig(extra=("app-editors/neovim",)))
    )


def test_keymap_names_remain_open() -> None:
    installation = config()
    _validate_with_shipped_values(
        replace(
            installation,
            system=replace(
                installation.system,
                keymap="target-console-keymap",
                keymap_initramfs="target-unlock-keymap",
            ),
        )
    )


def test_a_selected_locale_absent_from_system_locales_is_refused_and_named() -> None:
    installation = replace(
        config(),
        system=replace(
            config().system,
            locales=("en_US.UTF-8", "zh_CN.UTF-8"),
            locale="zh_TW.UTF-8",
        ),
    )

    with pytest.raises(
        ValidationFailed,
        match=r"system\.locale is 'zh_TW\.UTF-8'.*system\.locales",
    ):
        validate(installation)


def test_a_selected_locale_present_in_system_locales_validates() -> None:
    installation = replace(
        config(),
        system=replace(
            config().system,
            locales=("en_US.UTF-8", "zh_TW.UTF-8"),
            locale="zh_TW.UTF-8",
        ),
    )

    validate(installation)


def test_a_selected_locale_with_no_generated_locales_is_refused() -> None:
    installation = replace(
        config(),
        system=replace(config().system, locales=(), locale="en_US.UTF-8"),
    )

    with pytest.raises(ValidationFailed, match=r"system\.locales"):
        validate(installation)


def test_an_l10n_tag_not_shaped_like_one_is_refused_and_named() -> None:
    installation = replace(
        config(), portage=replace(config().portage, l10n=("zh_TW",))
    )

    with pytest.raises(ValidationFailed, match=r"L10N tag 'zh_TW'"):
        validate(installation)


def test_a_root_that_no_device_defines_is_named() -> None:
    broken = replace(config(), disk=replace(config().disk, root=i("absent")))
    with pytest.raises(ValidationFailed, match="'absent', which no device defines"):
        validate(broken)


def test_a_root_that_is_not_a_mountpoint_is_named() -> None:
    broken = replace(config(), disk=replace(config().disk, root=i("rootfs")))
    with pytest.raises(ValidationFailed, match="not a mountpoint"):
        validate(broken)


def test_a_root_mounted_somewhere_else_is_named() -> None:
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id != i("mnt-root")]
    nodes.append(Mountpoint(id=i("mnt-root"), source=i("rootfs"), path=PurePosixPath("/srv")))
    with pytest.raises(ValidationFailed, match="mounted at /srv"):
        validate(config(nodes))


def test_vfat_cannot_be_the_root_filesystem() -> None:
    from gentoo_install.model.device import FilesystemType

    nodes = [
        replace(node, kind=FilesystemType.VFAT)
        if isinstance(node, Filesystem) and node.id == i("rootfs")
        else node
        for node in ext4_on_gpt()
    ]
    with pytest.raises(ValidationFailed, match="root filesystem is vfat"):
        validate(config(nodes))


@pytest.mark.parametrize(
    "rule",
    [
        pytest.param(rule, id=rule.kind.value)
        for rule in compat.FILESYSTEM_LABEL_RULES
        if rule.unit is compat.FilesystemLabelUnit.BYTES
    ],
)
def test_an_ext_filesystem_label_over_16_bytes_is_refused(
    rule: compat.FilesystemLabelRule,
) -> None:
    nodes = [
        replace(node, kind=rule.kind, label="x" * (rule.maximum + 1))
        if isinstance(node, Filesystem) and node.id == i("rootfs")
        else node
        for node in ext4_on_gpt()
    ]
    with pytest.raises(
        ValidationFailed,
        match=rf"{rule.kind.value}.*17 bytes.*limited to 16 bytes",
    ):
        validate(config(nodes))


def test_a_vfat_label_over_11_characters_is_refused() -> None:
    nodes = [
        replace(node, label="x" * 12)
        if isinstance(node, Filesystem) and node.id == i("espfs")
        else node
        for node in ext4_on_gpt()
    ]
    with pytest.raises(
        ValidationFailed,
        match=r"vfat.*12 characters.*limited to 11 characters",
    ):
        validate(config(nodes))


def test_the_vfat_limit_measures_characters_rather_than_utf8_bytes() -> None:
    label = "\u4e2d\u6587\u6807\u7b7e"
    rule = next(
        rule
        for rule in compat.FILESYSTEM_LABEL_RULES
        if rule.kind.value == "vfat"
    )
    assert len(label) == 4
    assert len(label.encode()) == 12
    assert rule.unit.measure(label) == 4


def test_a_malformed_authorized_key_is_refused_while_parsing() -> None:
    raw = tomllib.loads((FIXTURES / "ext4-bios.toml").read_text())
    raw["system"]["authorized_keys"] = ["not-a-key"]

    with pytest.raises(ConfigError, match="not a public key"):
        parse(raw)


def test_a_zfs_passphrase_file_implies_native_encryption() -> None:
    from gentoo_install.model.device import ZfsPool
    from gentoo_install.plan import disk as plan_disk

    raw = tomllib.loads((FIXTURES / "vm-zfs.toml").read_text())
    pool = next(node for node in raw["disk"]["devices"] if node["kind"] == "zpool")
    pool["passphrase_file"] = "/run/keys/pool"
    assert "encrypted" not in pool

    installation = parse(raw)
    parsed_pool = installation.disk.graph.of_type(ZfsPool)[0]
    operation = next(
        one for one in plan_disk.build(installation) if isinstance(one, plan_disk.CreateZpool)
    )
    assert parsed_pool.encrypted is True
    assert operation.encrypted is True


def test_an_inconsistent_direct_zfs_model_is_refused() -> None:
    from gentoo_install.model.device import Existing, ZfsPool

    nodes = [
        *ext4_on_gpt(),
        Existing(id=i("pooldisk"), selector="/dev/disk/by-id/pool", wipe=True),
        ZfsPool(
            id=i("pool"),
            vdevs=(i("pooldisk"),),
            name="rpool",
            encrypted=False,
            passphrase_file="/run/keys/pool",
        ),
    ]
    with pytest.raises(ValidationFailed, match="passphrase_file"):
        validate(config(nodes))


def test_two_devices_on_one_path_are_named() -> None:
    nodes = ext4_on_gpt()
    nodes.append(Mountpoint(id=i("mnt-esp-again"), source=i("espfs"), path=PurePosixPath("/efi")))
    with pytest.raises(ValidationFailed, match="2 devices are mounted at /efi"):
        validate(config(nodes))


def test_a_mountpoint_cannot_escape_the_installation_target() -> None:
    nodes = [
        replace(node, path=PurePosixPath("/../outside"))
        if isinstance(node, Mountpoint) and node.id == i("mnt-esp")
        else node
        for node in ext4_on_gpt()
    ]
    with pytest.raises(ValidationFailed, match=r"mountpoint mnt-esp uses /\.\./outside"):
        validate(config(nodes))


def test_a_layout_problem_and_a_broken_rule_are_reported_in_one_message() -> None:
    nodes = zfs_root()
    nodes.append(Mountpoint(id=i("mnt-esp-again"), source=i("espfs"), path=PurePosixPath("/efi")))
    with pytest.raises(ValidationFailed) as caught:
        validate(config(nodes))
    message = str(caught.value)
    assert "2 devices are mounted at /efi" in message
    assert "root on ZFS excludes GRUB" in message


def test_a_root_too_small_is_refused_before_anything_is_written() -> None:
    """Measured: an install into 8 GiB runs out during linux-firmware, an hour
    after the disks were partitioned."""
    nodes = [
        node
        for node in ext4_on_gpt()
        if not isinstance(node, Partition) or node.role is not PartitionRole.DATA
    ]
    nodes.append(
        Partition(
            id=i("rootpart"),
            table=i("table"),
            index=2,
            role=PartitionRole.DATA,
            size=Size.parse("8GiB"),
        )
    )
    with pytest.raises(ValidationFailed, match="under the"):
        validate(config(nodes))


def test_a_root_with_room_passes() -> None:
    nodes = [
        node
        for node in ext4_on_gpt()
        if not isinstance(node, Partition) or node.role is not PartitionRole.DATA
    ]
    nodes.append(
        Partition(
            id=i("rootpart"),
            table=i("table"),
            index=2,
            role=PartitionRole.DATA,
            size=Size.parse("30GiB"),
        )
    )
    validate(config(nodes))


def test_an_official_v3_binhost_is_refused_when_the_loader_says_no() -> None:
    """`ld.so --help` decides, not a flag list: it is what `docs/design.md`
    names and what the menu greys the row out on."""
    base = config()
    selected = replace(
        base,
        portage=replace(
            base.portage,
            binhost=replace(base.portage.binhost, subarch="x86-64-v3"),
        ),
    )

    with pytest.raises(ValidationFailed, match="ld.so --help"):
        validate(selected, supports_v3=False)


def test_an_official_v3_binhost_passes_when_the_loader_says_yes() -> None:
    base = config()
    selected = replace(
        base,
        portage=replace(
            base.portage,
            binhost=replace(base.portage.binhost, subarch="x86-64-v3"),
        ),
    )

    validate(selected, supports_v3=True)


def test_a_configuration_validated_without_a_machine_is_not_refused_v3() -> None:
    """A dry run reads no machine. Refusing there refuses a configuration
    written for another one, which is what `--dry-run` is for."""
    base = config()
    validate(
        replace(
            base,
            portage=replace(
                base.portage,
                binhost=replace(base.portage.binhost, subarch="x86-64-v3"),
            ),
        )
    )


def test_a_profile_that_disagrees_with_the_init_is_refused() -> None:
    """The profile decides what packages are built against, so one that does
    not match leaves a system whose packages expect the other init."""
    base = config()
    with pytest.raises(ValidationFailed, match="without /systemd"):
        validate(replace(base, system=replace(base.system, init=InitSystem.OPENRC)))

    openrc = replace(
        base,
        system=replace(base.system, init=InitSystem.OPENRC),
        portage=replace(base.portage, profile="default/linux/amd64/23.0/desktop"),
    )
    validate(openrc)

    with pytest.raises(ValidationFailed, match="ending in /systemd"):
        validate(replace(openrc, system=replace(openrc.system, init=InitSystem.SYSTEMD)))


def test_a_configured_profile_absent_from_eselect_is_refused() -> None:
    installation = config()

    with pytest.raises(ValidationFailed) as refused:
        validate(
            installation,
            available_profiles=("default/linux/amd64/24.0/systemd",),
        )

    message = str(refused.value)
    assert installation.portage.profile in message
    assert "eselect profile list" in message


def test_a_configured_profile_present_in_eselect_passes() -> None:
    installation = config()

    validate(installation, available_profiles=(installation.portage.profile,))


def test_an_unreadable_eselect_profile_list_is_reported() -> None:
    installation = config()

    with pytest.raises(ValidationFailed) as refused:
        validate(installation, available_profiles=None)

    message = str(refused.value)
    assert installation.portage.profile in message
    assert "could not be read" in message
    assert "eselect profile list" in message


def test_the_profile_parser_keeps_the_observed_eselect_markers() -> None:
    profiles = profiles_from_eselect(
        """Available profile symlink targets:
  [8]   default/linux/amd64/23.0/desktop/plasma/systemd (stable) *
  [15]  default/linux/amd64/23.0/no-multilib/prefix (exp)
  [40]  default/linux/amd64/23.0/x32 (dev)
"""
    )

    assert tuple((one.path, one.stability, one.current) for one in profiles) == (
        ("default/linux/amd64/23.0/desktop/plasma/systemd", "stable", True),
        ("default/linux/amd64/23.0/no-multilib/prefix", "exp", False),
        ("default/linux/amd64/23.0/x32", "dev", False),
    )
    with pytest.raises(FrozenInstanceError):
        setattr(profiles[0], "path", "default/linux/amd64/24.0")


def test_a_static_address_with_no_resolver_is_refused() -> None:
    """The machine boots with an address and cannot resolve a name."""
    wanted = replace(
        config(), system=replace(config().system, addresses=("192.0.2.10/24",), gateways=("192.0.2.1",))
    )
    assert compat.system_network_problems(wanted.system)[0].field is compat.NetworkField.SYSTEM_DNS
    answered = replace(wanted, system=replace(wanted.system, dns=("192.0.2.1",)))
    assert compat.system_network_problems(answered.system) == ()


def test_a_static_address_needs_a_gateway_of_its_own_family() -> None:
    """A v6 address with only a v4 gateway reaches nothing off its subnet."""
    system = replace(
        config().system,
        addresses=("192.0.2.10/24", "2001:db8::2/64"),
        gateways=("192.0.2.1",),
        dns=("192.0.2.1",),
    )
    problems = compat.system_network_problems(system)
    assert len(problems) == 1
    assert problems[0].field is compat.NetworkField.SYSTEM_IPV6_GATEWAY
    assert "2001:db8::2/64" in problems[0].describe()

    both = replace(system, gateways=("192.0.2.1", "fe80::1"))
    assert compat.system_network_problems(both) == ()


def test_dhcp_needs_neither_a_gateway_nor_a_resolver() -> None:
    """They come from the lease, so demanding them would refuse the default."""
    assert compat.system_network_problems(config().system) == ()


def test_openrc_builtin_static_networking_requires_an_interface() -> None:
    from gentoo_install.model.config import Networking

    installation = replace(
        config(),
        portage=replace(
            config().portage, profile="default/linux/amd64/23.0/desktop"
        ),
        system=replace(
            config().system,
            init=InitSystem.OPENRC,
            networking=Networking.BUILTIN,
            addresses=("192.0.2.10/24",),
            gateways=("192.0.2.1",),
            dns=("192.0.2.1",),
        ),
    )

    with pytest.raises(ValidationFailed, match=r"system\.interface.*OpenRC.*static"):
        validate(installation)


@pytest.mark.parametrize("address", ["192.0.2.10/99", "not-an-address", "999.1.1.1/24"])
def test_a_static_address_that_is_not_an_address_is_refused(address: str) -> None:
    """`_family_of` answered 0 for anything unparsable and every check then
    skipped it, so the string reached dracut's `ip=` parameter as written."""
    from gentoo_install.model.config import Networking

    installation = replace(
        config(),
        system=replace(
            config().system,
            networking=Networking.BUILTIN,
            interface="eth0",
            addresses=(address,),
            gateways=("192.0.2.1",),
            dns=("192.0.2.1",),
        ),
    )
    with pytest.raises(ValidationFailed):
        validate(installation)


def _record_address_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[str]]:
    interface_calls: list[str] = []
    address_calls: list[str] = []
    parse_interface = ipaddress.ip_interface
    parse_address = ipaddress.ip_address

    def counted_interface(literal: str) -> ipaddress.IPv4Interface | ipaddress.IPv6Interface:
        interface_calls.append(literal)
        return parse_interface(literal)

    def counted_address(literal: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        address_calls.append(literal)
        return parse_address(literal)

    monkeypatch.setattr(ipaddress, "ip_interface", counted_interface)
    monkeypatch.setattr(ipaddress, "ip_address", counted_address)
    return interface_calls, address_calls


def test_validate_parses_each_configured_network_value_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gentoo_install.model.config import RemoteUnlock

    interface_calls, address_calls = _record_address_parses(monkeypatch)
    base = config(unlockable_root())
    installation = replace(
        base,
        system=replace(
            base.system,
            addresses=("192.0.2.10/24", "2001:db8::10/64"),
            gateways=("192.0.2.1", "2001:db8::1"),
            dns=("192.0.2.53", "2001:db8::53"),
            authorized_keys=("ssh-ed25519 AAAA test",),
        ),
        kernel=replace(
            base.kernel,
            remote_unlock=RemoteUnlock(
                enabled=True,
                address="198.51.100.10/24",
                gateway="198.51.100.1",
                interface="eth0",
            ),
        ),
    )

    validate(installation)

    assert interface_calls == [
        "192.0.2.10/24",
        "2001:db8::10/64",
        "198.51.100.10/24",
    ]
    assert address_calls == [
        "192.0.2.1",
        "2001:db8::1",
        "192.0.2.53",
        "2001:db8::53",
        "198.51.100.1",
    ]


def test_validate_does_not_reparse_malformed_remote_unlock_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gentoo_install.model.config import RemoteUnlock

    interface_calls, address_calls = _record_address_parses(monkeypatch)
    base = config(unlockable_root())
    installation = replace(
        base,
        system=replace(
            base.system,
            authorized_keys=("ssh-ed25519 AAAA test",),
        ),
        kernel=replace(
            base.kernel,
            remote_unlock=RemoteUnlock(
                enabled=True,
                address="not-an-interface",
                gateway="not-an-address",
                interface="eth0",
            ),
        ),
    )

    with pytest.raises(ValidationFailed):
        validate(installation)

    assert interface_calls == ["not-an-interface"]
    assert address_calls == ["not-an-address"]


def test_disabled_remote_unlock_addresses_remain_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gentoo_install.model.config import RemoteUnlock

    interface_calls, address_calls = _record_address_parses(monkeypatch)
    base = config()
    installation = replace(
        base,
        kernel=replace(
            base.kernel,
            remote_unlock=RemoteUnlock(
                enabled=False,
                address="not-an-interface",
                gateway="not-an-address",
            ),
        ),
    )

    validate(installation)

    assert interface_calls == ["not-an-interface"]
    assert address_calls == ["not-an-address"]


@pytest.mark.parametrize("port", [0, -1, 65536, 99999])
def test_a_remote_unlock_port_outside_the_range_is_refused(port: int) -> None:
    """`dropbear_port` took any integer, and the initramfs then failed to start
    with the disks already encrypted."""
    from gentoo_install.model.config import KernelConfig, RemoteUnlock

    installation = replace(
        config(encrypted_root()),
        system=replace(config().system, authorized_keys=("ssh-ed25519 AAAA test",)),
        kernel=replace(
            KernelConfig(),
            remote_unlock=RemoteUnlock(enabled=True, port=port, interface="eth0"),
        ),
    )
    with pytest.raises(ValidationFailed):
        validate(installation)


def _with_indexes(nodes: list[Node], indexes: dict[str, int]) -> InstallConfig:
    edited = [
        replace(node, index=indexes[str(node.id)])
        if isinstance(node, Partition) and str(node.id) in indexes
        else node
        for node in nodes
    ]
    return config(edited)


@pytest.mark.parametrize(
    "indexes",
    [
        pytest.param({"esp": 0}, id="zero"),
        pytest.param({"esp": -1}, id="negative"),
        pytest.param({"esp": 2}, id="duplicate"),
    ],
)
def test_a_partition_index_sgdisk_cannot_honour_is_refused(indexes: dict[str, int]) -> None:
    """`CreatePartitionTable` runs `sgdisk --zap-all` first, so both of these
    are found with the operator's table already gone.

    Zero means *allocate one* to `sgdisk`: it answers success having made
    partition 1, and the executor then waits for a node ending in 0 that the
    kernel cannot expose. A repeated index fails the second `--new` with exit
    4 and leaves half a table behind.
    """
    with pytest.raises(ValidationFailed):
        validate(_with_indexes(ext4_on_gpt(), indexes))


def test_the_same_index_on_two_tables_is_a_working_layout() -> None:
    """Every disk numbers its own partitions, so two tables both holding a
    partition 1 is what two disks look like, and the check is per table."""
    from gentoo_install.model.device import (
        Existing,
        Filesystem,
        FilesystemType,
        PartitionTable,
        TableType,
    )

    second = [
        Existing(id=i("disk2"), selector="/dev/disk/by-id/virtio-home", wipe=True),
        PartitionTable(id=i("table2"), disk=i("disk2"), table=TableType.GPT),
        Partition(id=i("homepart"), table=i("table2"), index=1, role=PartitionRole.DATA, size=None),
        Filesystem(id=i("homefs"), device=i("homepart"), kind=FilesystemType.EXT4),
        Mountpoint(id=i("mnt-home"), source=i("homefs"), path=PurePosixPath("/home")),
    ]
    validate(config([*ext4_on_gpt(), *second]))
@pytest.mark.parametrize(
    ("address", "gateway"),
    [
        pytest.param("192.0.2.10/24", "2001:db8::1", id="v4-address-v6-gateway"),
        pytest.param("2001:db8::10/64", "192.0.2.1", id="v6-address-v4-gateway"),
    ],
)
def test_a_remote_unlock_gateway_of_another_family_is_refused(
    address: str, gateway: str
) -> None:
    """Both go into one dracut `ip=` stanza, so the initramfs is configured
    with a client of one family and a gateway of the other and routes nowhere.
    The machine waiting for its passphrase is then reachable only from the
    console, which is the one thing remote unlock exists to avoid."""
    from gentoo_install.model.config import KernelConfig, RemoteUnlock

    installation = replace(
        config(encrypted_root()),
        system=replace(config().system, authorized_keys=("ssh-ed25519 AAAA test",)),
        kernel=replace(
            KernelConfig(),
            remote_unlock=RemoteUnlock(
                enabled=True, port=222, interface="eth0", address=address, gateway=gateway
            ),
        ),
    )
    with pytest.raises(ValidationFailed):
        validate(installation)


def test_a_remote_unlock_pair_of_one_family_is_a_working_configuration() -> None:
    """The fixture ships an IPv4 pair, and an IPv6 pair is the same shape."""
    from gentoo_install.model.config import KernelConfig, RemoteUnlock

    for address, gateway in (("192.0.2.10/24", "192.0.2.1"), ("2001:db8::10/64", "2001:db8::1")):
        validate(
            replace(
                config(unlockable_root()),
                system=replace(config().system, authorized_keys=("ssh-ed25519 AAAA test",)),
                kernel=replace(
                    KernelConfig(),
                    remote_unlock=RemoteUnlock(
                        enabled=True,
                        port=222,
                        interface="eth0",
                        address=address,
                        gateway=gateway,
                    ),
                ),
            )
        )


def test_a_new_mbr_table_whose_indexes_parted_cannot_honour_is_refused() -> None:
    """`parted mkpart` takes no partition number and assigns the lowest free
    one, so a new MBR table asking for index 3 alone gets partition 1: parted
    reports success and the executor waits for a node ending in 3 that the
    kernel cannot expose, with the table and partition 1 already written.

    Measured: `parted --script --align optimal <image> mkpart primary 1MiB
    65MiB` on a fresh msdos label produced `1:1.00MiB:65.0MiB`.
    """
    from gentoo_install.model.device import TableType

    nodes = [
        node
        for node in ext4_on_gpt()
        if not isinstance(node, Partition) or node.id != i("esp")
    ]
    nodes = [
        replace(node, table=TableType.MBR)
        if isinstance(node, PartitionTable)
        else replace(node, index=3)
        if isinstance(node, Partition)
        else node
        for node in nodes
    ]
    nodes = [node for node in nodes if not isinstance(node, Mountpoint) or node.path != PurePosixPath("/efi")]
    nodes = [node for node in nodes if node.id != i("espfs")]
    with pytest.raises(ValidationFailed) as refused:
        validate(config(nodes))
    # Named, not merely refused: stripping the esp to reach an MBR layout
    # gives this configuration other problems, and a test that only asks for
    # `ValidationFailed` passes with the index rule removed.
    assert "parted assigns the lowest free number" in str(refused.value), refused.value


def test_an_mbr_table_numbered_from_one_is_a_working_layout() -> None:
    """`ext4-bios.toml` is exactly this, and it installs."""
    from pathlib import Path as _Path

    from gentoo_install.exec.config import load as _load

    validate(_load(_Path("tests/fixtures/ext4-bios.toml")))


@pytest.mark.parametrize(
    ("what", "edited", "says"),
    [
        pytest.param(
            "unsized-not-last",
            {"esp": None, "rootpart": Size.parse("20GiB")},
            "takes the rest of",
            id="unsized-not-last",
        ),
        pytest.param("zero", {"esp": Size(0)}, "is 0B", id="zero"),
    ],
)
def test_a_partition_size_the_table_cannot_hold_is_refused(
    what: str, edited: dict[str, Size | None], says: str
) -> None:
    """Both are found after `sgdisk --zap-all` has taken the operator's table.

    An unsized partition takes what is left, so an unsized partition 1 runs to
    the last usable sector and `--new=2:0:+8M` exits 4. A zero-sized one is
    refused one step earlier, as `+0K`.
    """
    nodes = [
        replace(node, size=edited[str(node.id)])
        if isinstance(node, Partition) and str(node.id) in edited
        else node
        for node in ext4_on_gpt()
    ]
    with pytest.raises(ValidationFailed) as refused:
        validate(config(nodes))
    assert says in str(refused.value), refused.value


def test_the_last_partition_may_still_take_the_rest_of_the_disk() -> None:
    """Every shipped layout does this, and it is the point of the rule that
    only the last one may."""
    validate(config(ext4_on_gpt()))


def _with_non_root_logical_volume(size: Size) -> InstallConfig:
    return config(
        [
            *ext4_on_gpt(),
            Existing(id=i("data-pv"), selector="/dev/disk/by-id/data", wipe=True),
            VolumeGroup(id=i("data-vg"), members=(i("data-pv"),), name="data"),
            LogicalVolume(
                id=i("cache-lv"), group=i("data-vg"), name="cache", size=size
            ),
        ]
    )


def test_two_logical_volumes_cannot_both_take_the_rest_of_a_group() -> None:
    """`lvcreate --extents 100%FREE` gives the remainder to whichever volume
    asks first, so the second one fails with the disk already partitioned and
    the group already made. Partitions have had this rule from the start.
    """
    nodes = [
        *ext4_on_gpt(),
        Existing(id=i("data-pv"), selector="/dev/disk/by-id/data", wipe=True),
        VolumeGroup(id=i("data-vg"), members=(i("data-pv"),), name="data"),
        LogicalVolume(id=i("cache-lv"), group=i("data-vg"), name="cache", size=None),
        LogicalVolume(id=i("spool-lv"), group=i("data-vg"), name="spool", size=None),
    ]
    with pytest.raises(ValidationFailed, match="only one can") as refused:
        validate(config(nodes))
    assert "cache-lv" in str(refused.value) and "spool-lv" in str(refused.value)

    # One of them may, which is what every shipped layout does.
    validate(config(nodes[:-1]))

    # And two groups each with one are two separate remainders.
    beside = [
        *nodes[:-1],
        Existing(id=i("spool-pv"), selector="/dev/disk/by-id/spool", wipe=True),
        VolumeGroup(id=i("spool-vg"), members=(i("spool-pv"),), name="spool"),
        LogicalVolume(id=i("spool-lv"), group=i("spool-vg"), name="spool", size=None),
    ]
    validate(config(beside))


def test_a_zero_sized_non_root_logical_volume_is_refused_and_named() -> None:
    with pytest.raises(ValidationFailed, match="logical volume cache-lv is 0B"):
        validate(_with_non_root_logical_volume(Size(0)))


def test_a_positive_non_root_logical_volume_validates() -> None:
    validate(_with_non_root_logical_volume(Size.parse("1GiB")))


from gentoo_install.model.config import Bootloader, InitSystem, InstallConfig, Overlay
def test_an_unknown_zfs_kernel_ceiling_refuses_the_run() -> None:
    with pytest.raises(ValidationFailed, match="sys-fs/zfs kernel ceiling could not be read"):
        validate(config(zfs_root()), zfs_kernel_max=None)


def test_a_zfs_kernel_ceiling_refuses_above_and_accepts_below() -> None:
    base = config(zfs_root())
    installation = replace(
        base,
        bootloader=replace(base.bootloader, kind=Bootloader.ZFSBOOTMENU),
        portage=replace(
            base.portage,
            overlays=(
                Overlay(
                    name="gentoo-zh",
                    sync_uri="https://example.invalid/gentoo-zh.git",
                ),
            ),
        ),
    )
    above = replace(
        installation,
        kernel=replace(installation.kernel, version="7.1.2"),
    )
    below = replace(
        installation,
        kernel=replace(installation.kernel, version="6.12.58"),
    )
    same_minor = replace(
        installation,
        kernel=replace(installation.kernel, version="7.0.1"),
    )

    with pytest.raises(ValidationFailed, match="7.1.2 is above the sys-fs/zfs ceiling 7.0"):
        validate(above, zfs_kernel_max="7.0")
    validate(below, zfs_kernel_max="7.0")
    validate(same_minor, zfs_kernel_max="7.0")


def test_zfs_ceiling_is_derived_from_portage_rdepend() -> None:
    assert zfs_kernel_ceiling(
        "sys-fs/zfs-2.4.3", "dist-kernel-cap? ( dist-kernel? ( <virtual/dist-kernel-7.1 ) )"
    ).maximum == "7.0"


def test_an_unpinned_kernel_is_left_to_portage() -> None:
    """`sys-fs/zfs` carries `dist-kernel-cap? ( dist-kernel? (
    <virtual/dist-kernel-7.1 ) )`, so an unpinned kernel is bounded by the
    dependency. Refusing one stopped every `dist-bin` ZFS install at step 28.
    """
    from gentoo_install.model.validate import KernelCeiling, zfs_kernel_version_problem

    assert zfs_kernel_version_problem("", KernelCeiling(maximum="7.0")) is None
    assert zfs_kernel_version_problem("6.18.41", KernelCeiling(maximum="7.0")) is None
    assert zfs_kernel_version_problem("7.1.0", KernelCeiling(maximum="7.0")) is not None


_UNSERVED_PROFILE_CASES: tuple[str, ...] = (
    "musl",
    "hardened",
    "llvm",
    "systemd-hardened",
)


@pytest.mark.parametrize("segment", _UNSERVED_PROFILE_CASES)
def test_a_profile_whose_stage3_is_not_fetched_is_refused(segment: str) -> None:
    """A profile's stage3 has to match its C library and toolchain."""
    from gentoo_install.plan.portage import variant_of

    base = config()
    broken = replace(
        base,
        portage=replace(base.portage, profile=f"default/linux/amd64/23.0/{segment}"),
        system=replace(base.system, init=InitSystem.OPENRC),
    )
    with pytest.raises(ValidationFailed, match="needs its own stage3"):
        validate(broken)
    assert segment not in variant_of(broken), (segment, variant_of(broken))


def test_unserved_profile_cases_cover_the_compatibility_table() -> None:
    assert set(_UNSERVED_PROFILE_CASES) == set(compat.UNSERVED_PROFILES)


@pytest.mark.parametrize(
    ("segment", "expected"),
    (("no-multilib", "nomultilib"), ("desktop", "desktop")),
)
def test_a_profile_with_a_served_stage3_is_accepted(segment: str, expected: str) -> None:
    from gentoo_install.plan.portage import variant_of

    base = config()
    served = replace(
        base,
        portage=replace(base.portage, profile=f"default/linux/amd64/23.0/{segment}"),
        system=replace(base.system, init=InitSystem.OPENRC),
    )
    validate(served)
    assert variant_of(served) == f"{expected}-openrc", variant_of(served)


def test_a_repository_name_is_checked_before_it_reaches_eselect() -> None:
    """`eselect repository` prints one line and exits 0 for a name it does not
    know, so the install carries on and the overlay is not there. The name is
    refused here instead, before a disk has been written."""
    from dataclasses import replace

    installation = config()
    for refused in ("not a name", "-leading-dash", "", "science/extra"):
        asked = replace(
            installation,
            portage=replace(installation.portage, repositories=(refused,)),
        )
        with pytest.raises(ValidationFailed):
            validate(asked)

    # A name this installer already ships is refused too: two `repos.conf`
    # sections of one name, and the later sync-uri replaces the earlier.
    shipped = installation.portage.overlays[0].name if installation.portage.overlays else "gentoo"
    with pytest.raises(ValidationFailed):
        validate(
            replace(
                installation,
                portage=replace(installation.portage, repositories=(shipped,)),
            )
        )

    # Negative control: a name that is neither malformed nor already shipped
    # passes, so the rule is not refusing everything.
    validate(
        replace(
            installation, portage=replace(installation.portage, repositories=("science",))
        )
    )


def test_a_table_this_run_writes_needs_a_disk_this_run_wipes() -> None:
    """`CreatePartitionTable` runs `sgdisk --zap-all` for `create`, and a
    configuration could say `wipe = false` on the disk and `create = true` on
    its table.

    Both were accepted, and the erase is what happened: the operator read
    `wipe = false` and the installer took every partition on the disk. That is
    the one operation with no way back, so it is refused rather than warned
    about.
    """
    import tomllib
    from pathlib import Path as _Path

    from gentoo_install.errors import GentooInstallError
    from gentoo_install.model.parse import parse

    raw = _Path("tests/fixtures/vm-binpkg.toml").read_text()
    data = tomllib.loads(raw.replace("wipe = true", "wipe = false"))
    for device in data["disk"]["devices"]:
        if device.get("kind") == "table":
            device["create"] = True
    kept = parse(data)

    with pytest.raises(GentooInstallError) as refused:
        validate(kept)
    assert "zap-all" in str(refused.value), refused.value
    assert "wipe = true" in str(refused.value), refused.value

    # And the two coherent pairs are still accepted: wipe and create together,
    # and keep and edit together, which is what the manual editor builds.
    both = tomllib.loads(raw)
    for device in both["disk"]["devices"]:
        if device.get("kind") == "table":
            device["create"] = True
    validate(parse(both))

    neither = tomllib.loads(raw.replace("wipe = true", "wipe = false"))
    for device in neither["disk"]["devices"]:
        if device.get("kind") == "table":
            device["create"] = False
    validate(parse(neither))


@pytest.mark.parametrize(
    "version, refused",
    [
        ("7.1.7", False),
        ("7.1.7-r2", False),
        ("6.12.47", False),
        ("7.1.7-r2:0", True),
        ("latest", True),
        ("7.1.7::gentoo", True),
    ],
)
def test_a_pinned_kernel_version_has_to_be_one_portage_can_be_given(
    version: str, refused: bool
) -> None:
    """`plan/kernel.py` builds `={package}-{version}` and `plan/portage.py`
    trims the version back off to name the package for `--usepkg-exclude`,
    which takes package names and slot atoms only. Anything after the version
    survives that trim and emerge answers `Invalid Atom(s)` at operation 20 of
    60, with the disks already written.
    """
    from gentoo_install.model.config import KernelConfig, KernelSource

    config = replace(
        parse(tomllib.loads((FIXTURES / "ext4-bios.toml").read_text())),
        kernel=KernelConfig(source=KernelSource.DIST_BIN, version=version),
    )
    if not refused:
        validate(config)
        return
    with pytest.raises(ValidationFailed, match="is not a version portage"):
        validate(config)


@pytest.mark.parametrize(
    "package",
    ["sys-kernel/gentoo-sources", "=sys-kernel/gentoo-sources-6.12*"],
)
def test_a_sources_package_is_refused_in_every_atom_form(package: str) -> None:
    """The trim read `-\\d[\\w.]*$`, and `=cat/pkg-6.12*` ends in an asterisk, so
    the name kept its version and the `-sources` refusal did not fire. Portage
    takes that form, and the installer builds no kernel of its own, so the
    machine finished with a source tree and an empty `/boot`."""
    from gentoo_install.model.config import KernelConfig, KernelSource

    config = replace(
        parse(tomllib.loads((FIXTURES / "ext4-bios.toml").read_text())),
        kernel=KernelConfig(source=KernelSource.DIST_BIN, package=package),
    )
    with pytest.raises(ValidationFailed, match="installs a source tree"):
        validate(config)


def test_the_cjk_refusal_reads_the_package_the_install_will_merge() -> None:
    """`cjk_kernel_problems` keyed on `kernel.source` while the trait beside it
    keyed on `kernel.package or ...`. An override naming a cjktty atom took the
    architecture refusal past the rule, and `emerge` then stopped with the disk
    already partitioned."""
    from gentoo_install.model.architecture import ARCHITECTURES, AMD64
    from gentoo_install.model.config import KernelConfig, KernelSource

    elsewhere = next(one for one in ARCHITECTURES if one.gentoo_name != AMD64.gentoo_name)
    config = replace(
        parse(tomllib.loads((FIXTURES / "ext4-bios.toml").read_text())),
        kernel=KernelConfig(
            source=KernelSource.DIST_BIN, package="sys-kernel/gentoo-cjk-kernel-bin"
        ),
    )

    assert compat.cjk_kernel_problems(config, elsewhere), "the override names a cjk kernel"
    assert not compat.cjk_kernel_problems(config, AMD64)


@pytest.mark.parametrize(
    "label, refused",
    [
        ("GENTOO ESP", False),
        ("esp_1", False),
        ("A-B", False),
        ("\u4e00", True),
        ("CAF\u00c9", True),
        ("A.B", True),
        ("A+B", True),
    ],
)
def test_a_vfat_label_is_refused_unless_mkfs_can_write_it(
    label: str, refused: bool
) -> None:
    """Measured against dosfstools 4.2 on 2026-08-31, one throwaway image per
    codepoint. `mkfs.vfat -n` stops at the CP850 conversion for a CJK label and
    answers `Labels with characters below 0x20 are not allowed` for every other
    non-ASCII one, and the length rule alone let both through to a formatting
    stage that runs after the disks are partitioned.
    """
    nodes = [
        replace(node, label=label)
        if isinstance(node, Filesystem) and node.id == i("espfs")
        else node
        for node in ext4_on_gpt()
    ]
    if not refused:
        validate(config(nodes))
        return
    with pytest.raises(ValidationFailed, match="which mkfs cannot write into one"):
        validate(config(nodes))


def test_a_desktop_paired_with_another_desktops_profile_is_refused() -> None:
    """Nothing refused `desktop = "plasma"` on `.../desktop/systemd`.

    That pairing is in `tests/fixtures/btrfs-luks.toml` and it stalled three
    cluster rounds at `install the plasma group`: the profile is valid, it
    matches the init and the repository has it, so every existing rule passed
    it. `data/profiles/base/plasma.toml` declares the profile its packages are
    built against, and that is what the config has to name.
    """
    from gentoo_install import data

    catalog = data.load_catalog()
    declared = {name: group.profile for name, group in catalog.items() if group.profile}
    assert declared, "no shipped group declares a profile"

    base = load(Path("tests/fixtures/vm-desktop.toml"))
    assert base.packages.desktop in declared, base.packages.desktop

    # The one the fixture was written with, which is the desktop's own.
    validate(base, declared_desktop_profiles=declared)

    generic = replace(
        base,
        portage=replace(base.portage, profile="default/linux/amd64/23.0/desktop/systemd"),
    )
    with pytest.raises(ValidationFailed) as refused:
        validate(generic, declared_desktop_profiles=declared)
    assert "is built against" in str(refused.value), str(refused.value)

    # Without the mapping the rule is silent, because the shipped names are
    # read at the entry point and a caller that has not read them cannot
    # decide: the TUI validates a graph mid-edit with nothing injected.
    validate(generic)
