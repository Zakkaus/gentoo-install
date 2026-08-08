from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.errors import ValidationFailed
from gentoo_install.model.config import InitSystem
from gentoo_install.model.device import Mountpoint, Node, Partition, PartitionRole
from gentoo_install.model.size import Size
from gentoo_install.model.parse import load
from gentoo_install.model.validate import validate

from .layouts import config, ext4_on_gpt, i, zfs_root

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_a_plain_uefi_install_validates() -> None:
    validate(config())


def test_the_shipped_fixture_validates() -> None:
    validate(load(FIXTURES / "btrfs-luks.toml"))


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


def test_two_devices_on_one_path_are_named() -> None:
    nodes = ext4_on_gpt()
    nodes.append(Mountpoint(id=i("mnt-esp-again"), source=i("espfs"), path=PurePosixPath("/efi")))
    with pytest.raises(ValidationFailed, match="2 devices are mounted at /efi"):
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
