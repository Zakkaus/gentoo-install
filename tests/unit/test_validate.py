from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.errors import ValidationFailed
from gentoo_install.model.config import InitSystem, InstallConfig
from gentoo_install.model.device import (
    Mountpoint,
    Node,
    Partition,
    PartitionRole,
    PartitionTable,
)
from gentoo_install.model.size import Size
from gentoo_install.exec.config import load
from gentoo_install.exec.probe import profiles_from_eselect
from gentoo_install.model.validate import validate

from .layouts import encrypted_root, config, ext4_on_gpt, i, zfs_root

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_a_plain_uefi_install_validates() -> None:
    validate(config())


def test_the_shipped_fixture_validates() -> None:
    validate(load(FIXTURES / "btrfs-luks.toml"))


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
                config(encrypted_root()),
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
