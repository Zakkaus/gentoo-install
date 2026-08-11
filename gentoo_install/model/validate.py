"""Checks that need more than one field, run before anything touches a disk.

Every problem is collected and reported together: fixing one rule per run means
one more failed run to learn about the next one.
"""

from __future__ import annotations

import ipaddress

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Collection, Final, Mapping

from ..errors import CommandFailed, ValidationFailed
from . import compat
from .config import InitSystem, InstallConfig
from .device import (
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    MdRaid,
    Mountpoint,
    Node,
    Partition,
    PartitionTable,
    TableType,
    ZfsPool,
    ZfsTopology,
)
from .size import Size

_ROOT = PurePosixPath("/")


@dataclass(frozen=True)
class ProbedProfile:
    """One row from `eselect profile list`."""

    path: str
    stability: str
    current: bool = False


_PROFILE_ROW: Final[re.Pattern[str]] = re.compile(
    r"^\s*\[\d+\]\s+(\S+)\s+\(([^()\s]+)\)(\s+\*)?\s*$"
)
_L10N_TAG: Final[re.Pattern[str]] = re.compile(
    r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?"
    r"(?:-(?:[0-9][a-z0-9]{3}|[a-z][a-z0-9]{4,7}))*$"
)


def parse_profile_list(
    output: str,
    *,
    source: str = "`eselect profile list`",
) -> tuple[ProbedProfile, ...]:
    """Parse the numbered rows emitted by eselect on the repository in use."""
    found: list[ProbedProfile] = []
    for line in output.splitlines():
        if not line.strip() or line.strip() == "Available profile symlink targets:":
            continue
        matched = _PROFILE_ROW.fullmatch(line)
        if matched is None:
            raise CommandFailed(
                f"{source} could not be read: unrecognised profile row {line.strip()!r}"
            )
        found.append(
            ProbedProfile(
                path=matched.group(1),
                stability=matched.group(2),
                current=matched.group(3) is not None,
            )
        )
    if not found:
        raise CommandFailed(f"{source} could not be read: no profile rows were returned")
    return tuple(found)


class _ProfilesNotRead:
    pass


_PROFILES_NOT_READ: Final[_ProfilesNotRead] = _ProfilesNotRead()


def validate(
    config: InstallConfig,
    *,
    available_profiles: Collection[str] | None | _ProfilesNotRead = _PROFILES_NOT_READ,
) -> None:
    problems = [
        *_layout_problems(config),
        *root_size_problems(config),
        *_profile_problems(config),
        *_repository_profile_problems(config.portage.profile, available_profiles),
        *_kernel_package_problems(config),
        *_reuse_problems(config),
        *_pool_problems(config),
        *_array_problems(config),
        *_network_problems(config),
        *_unlock_problems(config),
        *_l10n_problems(config),
        *(rule.describe() for rule in compat.violations(config)),
    ]
    if problems:
        raise ValidationFailed(
            "the configuration does not describe an installable system:\n  " + "\n  ".join(problems)
        )


def _l10n_problems(config: InstallConfig) -> list[str]:
    return [
        f"L10N tag {tag!r} is not a hyphenated BCP 47 language tag"
        for tag in config.portage.l10n
        if _L10N_TAG.fullmatch(tag) is None
    ]


def _pool_problems(config: InstallConfig) -> list[str]:
    """Every pool says how its vdevs are joined, and has enough of them.

    A bare list of devices is a stripe: `zpool create p a b` survives losing
    neither, and an operator who gave two disks almost always meant a mirror.
    The default is only safe for one device, so more than one has to say.
    """
    problems: list[str] = []
    for pool in config.disk.graph.of_type(ZfsPool):
        if len(pool.vdevs) > 1 and pool.topology is ZfsTopology.STRIPE:
            problems.append(
                f"{pool.id} joins {len(pool.vdevs)} devices with no topology, which stripes "
                f"them; name one of {', '.join(one.value for one in ZfsTopology)}"
            )
        if len(pool.vdevs) < pool.topology.minimum:
            problems.append(
                f"{pool.id} is a {pool.topology.value} of {len(pool.vdevs)} devices and needs "
                f"at least {pool.topology.minimum}"
            )
    return problems


def _array_problems(config: InstallConfig) -> list[str]:
    """Every array has enough members for the level it names."""
    return [
        f"{array.id} is a {array.level.value} of {len(array.members)} devices and needs "
        f"at least {array.level.minimum}"
        for array in config.disk.graph.of_type(MdRaid)
        if len(array.members) < array.level.minimum
    ]


def _network_problems(config: InstallConfig) -> list[str]:
    """A static address that reaches nothing is not a configured network.

    Both checks fire only when an address was given: DHCP and router
    advertisements supply the gateway and the resolvers themselves.
    """
    system = config.system
    problems: list[str] = []
    # Read before anything else uses them: `_family_of` answers 0 for a string
    # that is not an address at all, and every check below then skipped it, so
    # `not-an-address` and `192.0.2.10/99` reached dracut's `ip=` parameter.
    for address in system.addresses:
        try:
            ipaddress.ip_interface(address)
        except ValueError:
            problems.append(f"{address!r} is not an address with a prefix length")
    for named, where in (("gateway", system.gateways), ("resolver", system.dns)):
        for one in where:
            try:
                ipaddress.ip_address(one)
            except ValueError:
                problems.append(f"{one!r} is not an address, so it is not a {named}")
    if problems or not system.addresses:
        return problems
    if not system.dns:
        problems.append(
            "the addresses are static and no resolver is named, so the installed system "
            "has an address and no way to resolve a name"
        )
    for address in system.addresses:
        family = _family_of(address)
        if family and not any(_family_of(one) == family for one in system.gateways):
            problems.append(
                f"{address} has no gateway of its own family, so it reaches nothing off "
                "its own subnet"
            )
    return problems


def _unlock_problems(config: InstallConfig) -> list[str]:
    """The initramfs ssh daemon's port, checked here so the menu and a
    configuration file share the rule. `dropbear_port` took any integer, and
    the initramfs then failed to start with the disks already encrypted."""
    unlock = config.kernel.remote_unlock
    if not unlock.enabled:
        return []
    problems: list[str] = []
    if not 1 <= unlock.port <= 65535:
        problems.append(f"the remote unlock port {unlock.port} is not between 1 and 65535")
    for named, value in (("address", unlock.address), ("gateway", unlock.gateway)):
        if not value:
            continue
        try:
            ipaddress.ip_interface(value) if named == "address" else ipaddress.ip_address(value)
        except ValueError:
            problems.append(f"the remote unlock {named} {value!r} is not an address")
    here, there = _family_of(unlock.address), _family_of(unlock.gateway)
    if here and there and here != there:
        # Both go into one dracut `ip=` stanza, so an IPv4 client with an IPv6
        # gateway is a static interface that routes nowhere and the machine
        # waiting for its passphrase can only be reached from the console.
        problems.append(
            f"the remote unlock address {unlock.address} is IPv{here} and its gateway "
            f"{unlock.gateway} is IPv{there}, so the initramfs has no route"
        )
    return problems


def _family_of(address: str) -> int:
    """4, 6, or 0 for something that is neither."""
    try:
        return ipaddress.ip_address(address.split("/")[0]).version
    except ValueError:
        return 0


def _reuse_problems(config: InstallConfig) -> list[str]:
    """A filesystem the installer does not create has to sit on a device the
    installer does not create either.

    Reusing one on a partition this run makes, or on a disk this run wipes,
    describes keeping data that the same plan destroys a few operations earlier.
    """
    graph = config.disk.graph
    problems: list[str] = []
    for filesystem in graph.of_type(Filesystem):
        if filesystem.create:
            continue
        under = graph[filesystem.device]
        if not isinstance(under, Existing):
            problems.append(
                f"{filesystem.id} reuses the filesystem on {filesystem.device}, which this "
                f"install creates: nothing would be left to reuse"
            )
            continue
        if under.wipe:
            problems.append(
                f"{filesystem.id} reuses the filesystem on {under.id}, which is marked to be "
                f"wiped"
            )
    return problems


def _package_name(atom: str) -> str:
    """`=cat/pkg-1.2:3` reduced to `cat/pkg`.

    Checked on the name, not the atom: a version or a slot otherwise walks past
    a rule that exists to stop an unbuildable kernel.
    """
    bare = atom.lstrip("=<>~!").split(":", 1)[0]
    bare = re.sub(r"-r\d+$", "", bare)
    return re.sub(r"-\d[\w.]*$", "", bare)


def _kernel_package_problems(config: InstallConfig) -> list[str]:
    """The installer configures and compiles no kernel of its own, so a
    `-sources` package would unpack a tree that nothing ever builds and leave
    the bootloader pointing at a `/boot` with no kernel in it."""
    package = _package_name(config.kernel.package)
    if package and package.endswith("-sources"):
        return [
            f"{package} installs a source tree and no kernel; name a dist-kernel "
            "such as sys-kernel/gentoo-kernel"
        ]
    return []


#: Measured: an install into 8 GiB runs out during linux-firmware, an hour
#: after the disks were written.
ROOT_MINIMUM: Final[Size] = Size.parse("12GiB")


def root_size_problems(
    config: InstallConfig,
    supplied_sizes: Mapping[DeviceId, Size] | None = None,
) -> list[str]:
    """Reject undersized declared or supplied sizes beneath the root.

    The nearest declared size describes the root itself. Supplied backing sizes
    remain constraints even when a nearer logical device declares its size.
    """
    graph = config.disk.graph
    root = graph.nodes.get(config.disk.root)
    if not isinstance(root, Mountpoint):
        return []
    return [
        f"{node.id} carries / and is {size}, under the {ROOT_MINIMUM} a stage3, "
        "a kernel and linux-firmware need"
        for node, size in _root_sizes(graph, root.id, supplied_sizes or {})
        if size < ROOT_MINIMUM
    ]


def _root_sizes(
    graph: DeviceGraph,
    device: DeviceId,
    supplied_sizes: Mapping[DeviceId, Size],
) -> list[tuple[Node, Size]]:
    """The nearest declared size and supplied sizes on the root path.

    Declared member sizes are excluded because an array may be large enough
    when each member is not. A supplied physical limit still constrains it.
    """
    seen: set[DeviceId] = set()
    edge = [device]
    declared_size: tuple[Node, Size] | None = None
    supplied: list[tuple[Node, Size]] = []
    while edge:
        following: list[DeviceId] = []
        for current in edge:
            if current in seen or current not in graph.nodes:
                continue
            seen.add(current)
            node = graph[current]
            supplied_size = supplied_sizes.get(current)
            if supplied_size is not None:
                supplied.append((node, supplied_size))
            elif declared_size is None:
                node_size = getattr(node, "size", None)
                if isinstance(node_size, Size):
                    declared_size = node, node_size
            following.extend(node.inputs)
        edge = following
    return ([declared_size] if declared_size is not None else []) + supplied


def _profile_problems(config: InstallConfig) -> list[str]:
    """The profile decides what the system is built against, so one that does
    not match the init leaves a system whose packages expect the other."""
    profile = config.portage.profile
    systemd_profile = "systemd" in profile.split("/")
    wants_systemd = config.system.init is InitSystem.SYSTEMD
    if systemd_profile == wants_systemd:
        return []
    wanted = "one ending in /systemd" if wants_systemd else "one without /systemd"
    return [f"init is {config.system.init.value} and the profile is {profile}; use {wanted}"]


def _repository_profile_problems(
    profile: str,
    available_profiles: Collection[str] | None | _ProfilesNotRead,
) -> list[str]:
    """The chosen profile must exist in the repository used by the target."""
    if isinstance(available_profiles, _ProfilesNotRead):
        return []
    if available_profiles is None:
        return [
            f"configured profile {profile!r} cannot be resolved because the target repository "
            "list from `eselect profile list` could not be read"
        ]
    if profile not in available_profiles:
        return [
            f"configured profile {profile!r} is not in the target repository list returned by "
            "`eselect profile list`"
        ]
    return []


def validate_profile(profile: str, available_profiles: Collection[str] | None) -> None:
    """Apply the repository-membership rule to one configured profile."""
    problems = _repository_profile_problems(profile, available_profiles)
    if problems:
        raise ValidationFailed(problems[0])


def _layout_problems(config: InstallConfig) -> list[str]:
    graph = config.disk.graph
    problems: list[str] = []

    root = graph.nodes.get(config.disk.root)
    if root is None:
        problems.append(f"disk.root is {config.disk.root!r}, which no device defines")
    elif not isinstance(root, Mountpoint):
        problems.append(f"disk.root is {config.disk.root!r}, which is not a mountpoint")
    elif root.path != _ROOT:
        problems.append(f"disk.root is mounted at {root.path}, not at /")

    mountpoints = graph.of_type(Mountpoint)
    for mount in mountpoints:
        if not mount.path.is_absolute() or ".." in mount.path.parts:
            problems.append(
                f"mountpoint {mount.id} uses {mount.path}, which does not stay inside the target"
            )

    for path, count in Counter(mount.path for mount in mountpoints).items():
        if count > 1:
            problems.append(f"{count} devices are mounted at {path}")

    problems += _partition_index_problems(graph)
    return problems


def _partition_index_problems(graph: DeviceGraph) -> list[str]:
    """Indexes that `sgdisk` accepts and the executor cannot then find.

    Both are refused here because `CreatePartitionTable` has already run
    `sgdisk --zap-all` by the time either shows: index 0 means *allocate one*
    to `sgdisk`, which answers success having made partition 1, and the
    executor then waits for a node ending in 0 that the kernel cannot expose.
    A repeated index fails the second `--new` with exit 4 and leaves half a
    table on a disk whose own table is already gone.
    """
    problems: list[str] = []
    for table in graph.of_type(PartitionTable):
        indexes = [
            node.index for node in graph.of_type(Partition) if node.table == table.id
        ]
        for index in sorted(one for one in set(indexes) if one < 1):
            problems.append(f"partition index {index} on {table.id} is below 1")
        for index, count in sorted(Counter(indexes).items()):
            if count > 1:
                problems.append(f"{count} partitions on {table.id} take index {index}")
        problems += _mbr_index_problems(table, indexes)
        problems += _partition_size_problems(graph, table)
    return problems


def _partition_size_problems(graph: DeviceGraph, table: PartitionTable) -> list[str]:
    """Sizes the table cannot hold, found before `sgdisk --zap-all` runs.

    A partition with no size takes what is left, so only the last one may have
    none: an unsized partition 1 took the disk to its last usable sector and
    `--new=2:0:+8M` then exited 4, with the operator's table already gone.
    Zero is the same shape of failure one step earlier — `+0K` is refused —
    and `Size(0)` stays a legal value everywhere else.
    """
    problems: list[str] = []
    partitions = sorted(
        (one for one in graph.of_type(Partition) if one.table == table.id),
        key=lambda node: node.index,
    )
    for one in partitions:
        if one.size is not None and one.size.bytes <= 0:
            problems.append(f"partition {one.id} on {table.id} is {one.size}")
    unsized = [one for one in partitions if one.size is None]
    if len(unsized) > 1:
        problems.append(
            f"{len(unsized)} partitions on {table.id} take the rest of the disk; "
            "only the last one can"
        )
    elif unsized and partitions and unsized[0].index != partitions[-1].index:
        problems.append(
            f"partition {unsized[0].id} takes the rest of {table.id} and is not "
            "the last one, so nothing after it has room"
        )
    return problems


def _mbr_index_problems(table: PartitionTable, indexes: list[int]) -> list[str]:
    """Indexes `parted` will not honour on a table written from scratch.

    `parted mkpart` takes no partition number: it assigns the lowest free one.
    A new MBR table whose configuration asks for index 3 alone therefore gets
    partition 1, `parted` reports success, and the executor waits for a node
    ending in 3 that the kernel cannot expose — with the table and partition 1
    already written. GPT is not affected: `sgdisk --new=N:...` is given the
    number.
    """
    if table.table is not TableType.MBR or not table.create or not indexes:
        return []
    wanted = sorted(indexes)
    if wanted != list(range(1, len(wanted) + 1)):
        return [
            f"{table.id} is a new mbr table and asks for indexes {wanted}; "
            "parted assigns the lowest free number, so they have to be 1 upwards"
        ]
    return []
