# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.errors import ConfigError, ValidationFailed
from gentoo_install.model import compat
from gentoo_install.model.config import (
    DiskMode,
    InitSystem,
    InstallConfig,
    MemoryLaunch,
    MemoryMode,
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
from gentoo_install.model.validate import validate, validate_memory_launch, zfs_kernel_ceiling
from gentoo_install.model.parse import parse

from .layouts import encrypted_root, config, ext4_on_gpt, i, unlockable_root, zfs_root

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


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
        disk=replace(
            installation.disk,
            graph=DeviceGraph.build(()),
            root=i(""),
            mode=DiskMode.DD,
            source="/run/image.raw",
            destination="/dev/disk/by-id/virtio-target",
        ),
    )


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


def test_dd_mode_refuses_target_layout_fields() -> None:
    installation = dd_config()
    with pytest.raises(ValidationFailed) as refused:
        validate(
            replace(
                installation,
                disk=replace(
                    installation.disk,
                    graph=DeviceGraph.build(ext4_on_gpt()),
                    root=i("mnt-root"),
                    image="/run/target.raw",
                    size=Size.parse("20GiB"),
                    wipe=True,
                ),
            )
        )
    said = str(refused.value)
    for field in ("disk.devices", "disk.root", "disk.image", "disk.size", "disk.wipe"):
        assert f"{field} is not allowed in dd mode" in said


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


def test_a_key_or_a_port_needs_a_password_as_well() -> None:
    """`catalyst/livecd/files/README.txt` lines 96-98: the LiveCD command line
    takes `dosshd` and `passwd=foo`, and `dosshd` requires the password because
    it scrambles the existing one. None of its 35 options names a key or a
    port, so both of those take effect only once the installer has written
    them, and sshd is already listening on 22 with the command line's password
    by then. Asking for a key with no password leaves that window shut.
    """
    for launch in (
        MemoryLaunch(MemoryMode.RAM, ssh_key="ssh-ed25519 AAAA"),
        MemoryLaunch(MemoryMode.RAM, ssh_port=2222),
    ):
        with pytest.raises(ValidationFailed, match="--root-password is needed as well"):
            validate_memory_launch(config(), launch)


def test_a_password_on_its_own_is_the_environment_s_own_mechanism() -> None:
    """The negative direction, and the case a rule written from expectation
    rather than from the ISO refused: `--root-password` alone is exactly what
    `dosshd` documents, so refusing it would refuse the documented way in."""
    validate_memory_launch(config(), MemoryLaunch(MemoryMode.RAM, root_password="secret"))
    validate_memory_launch(
        config(),
        MemoryLaunch(MemoryMode.RAM, ssh_key="ssh-ed25519 AAAA", root_password="secret"),
    )


def test_the_shipped_fixture_validates() -> None:
    validate(load(FIXTURES / "btrfs-luks.toml"))


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
    """The machine boots with an address and cannot resolve a name, which is
    indistinguishable from a broken network to whoever gets it."""
    from gentoo_install.model.validate import _network_problems

    wanted = replace(
        config(), system=replace(config().system, addresses=("192.0.2.10/24",), gateways=("192.0.2.1",))
    )
    assert any("resolve a name" in one for one in _network_problems(wanted))
    answered = replace(wanted, system=replace(wanted.system, dns=("192.0.2.1",)))
    assert _network_problems(answered) == []


def test_a_static_address_needs_a_gateway_of_its_own_family() -> None:
    """A v6 address with only a v4 gateway reaches nothing off its subnet, and
    the reverse is the more common mistake."""
    from gentoo_install.model.validate import _network_problems

    system = replace(
        config().system,
        addresses=("192.0.2.10/24", "2001:db8::2/64"),
        gateways=("192.0.2.1",),
        dns=("192.0.2.1",),
    )
    problems = _network_problems(replace(config(), system=system))
    assert len(problems) == 1
    assert "2001:db8::2/64" in problems[0]

    both = replace(system, gateways=("192.0.2.1", "fe80::1"))
    assert _network_problems(replace(config(), system=both)) == []


def test_dhcp_needs_neither_a_gateway_nor_a_resolver() -> None:
    """They come from the lease, so demanding them would refuse the default."""
    from gentoo_install.model.validate import _network_problems

    assert _network_problems(config()) == []


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


def test_a_profile_whose_stage3_is_not_fetched_is_refused() -> None:
    """`variant_of` maps a profile to a published stage3 and its table holds
    `/no-multilib` and `/desktop`. A musl profile misses it and gets the plain
    tarball, which is glibc: two C libraries in one system, and `eselect
    profile set` repairs none of it. The profile list comes from the machine's
    own repository, so these are choices an operator can make.
    """
    from dataclasses import replace

    from gentoo_install.model import compat
    from gentoo_install.plan.portage import variant_of

    base = config()
    for segment in compat.UNSERVED_PROFILES:
        broken = replace(
            base,
            portage=replace(base.portage, profile=f"default/linux/amd64/23.0/{segment}"),
            system=replace(base.system, init=InitSystem.OPENRC),
        )
        with pytest.raises(ValidationFailed, match="needs its own stage3"):
            validate(broken)

        # The reason it has to be refused rather than mapped: the variant this
        # installer would fetch does not name the profile at all.
        assert segment not in variant_of(broken), (segment, variant_of(broken))

    # Negative control: the profiles the table does serve are not refused, and
    # each one changes the tarball that is fetched.
    for segment, expected in (("no-multilib", "nomultilib"), ("desktop", "desktop")):
        served = replace(
            base,
            portage=replace(base.portage, profile=f"default/linux/amd64/23.0/{segment}"),
            system=replace(base.system, init=InitSystem.OPENRC),
        )
        validate(served)
        assert variant_of(served) == f"{expected}-openrc", variant_of(served)
