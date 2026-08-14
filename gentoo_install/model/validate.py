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
from .config import InitSystem, InstallConfig, Networking, ProxyKind
from .device import (
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    FilesystemType,
    LogicalVolume,
    MdRaid,
    Mountpoint,
    Node,
    Partition,
    PartitionTable,
    StorageFacts,
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


class KernelCeiling(str):
    """A read kernel ceiling, with unknown kept distinct from no ceiling."""

    maximum: str | None

    def __new__(cls, maximum: str | None) -> KernelCeiling:
        normalised = maximum.strip() if maximum else None
        if normalised is not None and re.fullmatch(r"\d+(?:\.\d+)+", normalised) is None:
            normalised = None
        ceiling = super().__new__(cls, normalised or "unknown")
        ceiling.maximum = normalised
        return ceiling


def zfs_kernel_ceiling(cpv: str, rdepend: str) -> KernelCeiling:
    """Derive the ZFS ceiling from the dependency Portage cached for its ebuild."""
    bounds = re.findall(r"<virtual/dist-kernel-(\d+)\.(\d+)(?=[\s)])", rdepend)
    if len(bounds) != 1:
        raise ValueError(f"Portage metadata for {cpv} has no single ZFS dist-kernel ceiling")
    major, exclusive_minor = (int(part) for part in bounds[0])
    if exclusive_minor == 0:
        raise ValueError(f"Portage metadata for {cpv} has an invalid ZFS dist-kernel ceiling")
    return KernelCeiling(f"{major}.{exclusive_minor - 1}")


def zfs_kernel_version_problem(version: str, ceiling: KernelCeiling) -> str | None:
    """Return the incompatibility between a selected kernel and a ZFS ceiling."""
    if ceiling.maximum is None:
        return "the sys-fs/zfs kernel ceiling could not be read, so this ZFS install cannot establish that its kernel will build the module"
    # An unpinned kernel is Portage's to resolve: `sys-fs/zfs` carries
    # `dist-kernel-cap? ( dist-kernel? ( <virtual/dist-kernel-7.1 ) )`, so the
    # ceiling is enforced by the dependency rather than by a version here.
    if not version:
        return None
    limit = _numeric_version(ceiling.maximum)
    selected = _numeric_version(version)
    if not limit or not selected:
        return f"kernel version {version!r} cannot be compared with the sys-fs/zfs ceiling {ceiling}"
    if selected[: len(limit)] > limit:
        return f"kernel {version} is above the sys-fs/zfs ceiling {ceiling.maximum}, so its ZFS module will not build"
    return None


class _ZfsKernelCeilingNotChecked:
    pass


_ZFS_KERNEL_CEILING_NOT_CHECKED: Final[_ZfsKernelCeilingNotChecked] = (
    _ZfsKernelCeilingNotChecked()
)


@dataclass(frozen=True)
class _IPAddressFact:
    literal: str
    parsed: ipaddress.IPv4Address | ipaddress.IPv6Address | None


@dataclass(frozen=True)
class _IPInterfaceFact:
    literal: str
    parsed: ipaddress.IPv4Interface | ipaddress.IPv6Interface | None


@dataclass(frozen=True)
class _SystemAddressFacts:
    addresses: tuple[_IPInterfaceFact, ...]
    gateways: tuple[_IPAddressFact, ...]
    resolvers: tuple[_IPAddressFact, ...]


@dataclass(frozen=True)
class _RemoteUnlockAddressFacts:
    address: _IPInterfaceFact
    gateway: _IPAddressFact


@dataclass(frozen=True)
class _ConfiguredAddressFacts:
    system: _SystemAddressFacts
    remote_unlock: _RemoteUnlockAddressFacts


def validate(
    config: InstallConfig,
    *,
    storage_facts: StorageFacts | None = None,
    available_profiles: Collection[str] | None | _ProfilesNotRead = _PROFILES_NOT_READ,
    zfs_kernel_max: str | None | _ZfsKernelCeilingNotChecked = (
        _ZFS_KERNEL_CEILING_NOT_CHECKED
    ),
) -> None:
    address_facts = _derive_address_facts(config)
    problems = [
        *_proxy_problems(config),
        *_layout_problems(config),
        *compat.filesystem_label_problems(config),
        *root_size_problems(config),
        *_profile_problems(config),
        *_repository_profile_problems(config.portage.profile, available_profiles),
        *_zfs_kernel_problems(config, zfs_kernel_max),
        *_kernel_package_problems(config),
        *_reuse_problems(config),
        *_pool_problems(config),
        *_array_problems(config),
        *_network_problems(config, address_facts.system),
        *_unlock_problems(config, address_facts.remote_unlock),
        *_locale_problems(config),
        *_l10n_problems(config),
        *compat.binhost_subarch_problems(config),
        *(rule.describe() for rule in compat.violations(config, storage_facts)),
    ]
    if problems:
        raise ValidationFailed(
            "the configuration does not describe an installable system:\n  " + "\n  ".join(problems)
        )


def _proxy_problems(config: InstallConfig) -> list[str]:
    """Check proxy syntax for configurations built without the TOML parser."""
    proxy = config.proxy
    if not proxy.enabled:
        if proxy.bypass:
            return ["proxy bypass hosts require a proxy host"]
        if proxy.port or proxy.username or proxy.password:
            return ["proxy host is required when proxy fields are set"]
        return []
    if proxy.port < 1 or proxy.port > 65535:
        return ["proxy port must be between 1 and 65535"]
    if any(char.isspace() for char in proxy.host):
        return ["proxy host must not contain spaces"]
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in proxy.password):
        return ["proxy password contains control characters"]
    if any(not item.strip() or any(char.isspace() for char in item) for item in proxy.bypass):
        return ["proxy bypass hosts must be non-empty host names"]
    return []


def _zfs_kernel_problems(
    config: InstallConfig,
    ceiling: str | None | _ZfsKernelCeilingNotChecked,
) -> list[str]:
    """Refuse a ZFS root unless its selected kernel fits a read ceiling."""
    if isinstance(ceiling, _ZfsKernelCeilingNotChecked):
        return []
    if not config.disk.graph.of_type(ZfsPool):
        return []
    maximum = ceiling.maximum if isinstance(ceiling, KernelCeiling) else ceiling
    if maximum is None or not maximum.strip():
        return [zfs_kernel_version_problem(config.kernel.version, KernelCeiling(None)) or ""]
    problem = zfs_kernel_version_problem(config.kernel.version, KernelCeiling(maximum))
    return [problem] if problem is not None else []


def _numeric_version(version: str) -> tuple[int, ...]:
    """Numeric prefix components used by kernel and module version metadata."""
    found: list[int] = []
    for component in version.split("."):
        matched = re.match(r"\d+", component)
        if matched is None:
            break
        found.append(int(matched.group()))
    return tuple(found)


def _l10n_problems(config: InstallConfig) -> list[str]:
    return [
        f"L10N tag {tag!r} is not a hyphenated BCP 47 language tag"
        for tag in config.portage.l10n
        if _L10N_TAG.fullmatch(tag) is None
    ]


def _locale_problems(config: InstallConfig) -> list[str]:
    selected = config.system.locale
    if selected and selected not in config.system.locales:
        return [f"system.locale is {selected!r}, which system.locales must include"]
    return []


def _pool_problems(config: InstallConfig) -> list[str]:
    """Every pool says how its vdevs are joined, and has enough of them.

    A bare list of devices is a stripe: `zpool create p a b` survives losing
    neither, and an operator who gave two disks almost always meant a mirror.
    The default is only safe for one device, so more than one has to say.
    """
    problems: list[str] = []
    for pool in config.disk.graph.of_type(ZfsPool):
        if pool.passphrase_file and not pool.encrypted:
            problems.append(
                f"{pool.id} sets passphrase_file but encrypted is false, so the pool "
                "would be created as plaintext"
            )
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


def _parse_ip_address(literal: str) -> _IPAddressFact:
    try:
        parsed = ipaddress.ip_address(literal)
    except ValueError:
        parsed = None
    return _IPAddressFact(literal, parsed)


def _parse_ip_interface(literal: str) -> _IPInterfaceFact:
    try:
        parsed = ipaddress.ip_interface(literal)
    except ValueError:
        parsed = None
    return _IPInterfaceFact(literal, parsed)


def _derive_system_address_facts(config: InstallConfig) -> _SystemAddressFacts:
    system = config.system
    return _SystemAddressFacts(
        addresses=tuple(_parse_ip_interface(one) for one in system.addresses),
        gateways=tuple(_parse_ip_address(one) for one in system.gateways),
        resolvers=tuple(_parse_ip_address(one) for one in system.dns),
    )


def _derive_remote_unlock_address_facts(
    config: InstallConfig,
) -> _RemoteUnlockAddressFacts:
    unlock = config.kernel.remote_unlock
    return _RemoteUnlockAddressFacts(
        address=(
            _parse_ip_interface(unlock.address)
            if unlock.address
            else _IPInterfaceFact("", None)
        ),
        gateway=(
            _parse_ip_address(unlock.gateway)
            if unlock.gateway
            else _IPAddressFact("", None)
        ),
    )


def _derive_address_facts(config: InstallConfig) -> _ConfiguredAddressFacts:
    return _ConfiguredAddressFacts(
        system=_derive_system_address_facts(config),
        remote_unlock=_derive_remote_unlock_address_facts(config),
    )


def _network_problems(
    config: InstallConfig,
    facts: _SystemAddressFacts | None = None,
) -> list[str]:
    """A static address that reaches nothing is not a configured network.

    Both checks fire only when an address was given: DHCP and router
    advertisements supply the gateway and the resolvers themselves.
    """
    system = config.system
    if facts is None:
        facts = _derive_system_address_facts(config)
    problems: list[str] = []
    for address in facts.addresses:
        if address.parsed is None:
            problems.append(f"{address.literal!r} is not an address with a prefix length")
    for named, where in (("gateway", facts.gateways), ("resolver", facts.resolvers)):
        for one in where:
            if one.parsed is None:
                problems.append(f"{one.literal!r} is not an address, so it is not a {named}")
    if (
        system.addresses
        and system.init is InitSystem.OPENRC
        and system.networking is Networking.BUILTIN
        and not system.interface
    ):
        problems.append(
            "system.interface must name the OpenRC interface when builtin networking uses "
            "static addresses because netifrc has no wildcard match"
        )
    if problems or not system.addresses:
        return problems
    if not system.dns:
        problems.append(
            "the addresses are static and no resolver is named, so the installed system "
            "has an address and no way to resolve a name"
        )
    for address in facts.addresses:
        if address.parsed is not None and not any(
            gateway.parsed is not None
            and gateway.parsed.version == address.parsed.version
            for gateway in facts.gateways
        ):
            problems.append(
                f"{address.literal} has no gateway of its own family, so it reaches nothing off "
                "its own subnet"
            )
    return problems


def _unlock_problems(
    config: InstallConfig,
    facts: _RemoteUnlockAddressFacts | None = None,
) -> list[str]:
    """The initramfs ssh daemon's port, checked here so the menu and a
    configuration file share the rule. `dropbear_port` took any integer, and
    the initramfs then failed to start with the disks already encrypted."""
    unlock = config.kernel.remote_unlock
    if facts is None:
        facts = _derive_remote_unlock_address_facts(config)
    if not unlock.enabled:
        return []
    problems: list[str] = []
    if not 1 <= unlock.port <= 65535:
        problems.append(f"the remote unlock port {unlock.port} is not between 1 and 65535")
    for named, fact in (("address", facts.address), ("gateway", facts.gateway)):
        if fact.literal and fact.parsed is None:
            problems.append(
                f"the remote unlock {named} {fact.literal!r} is not an address"
            )
    here = facts.address.parsed
    there = facts.gateway.parsed
    if here is not None and there is not None and here.version != there.version:
        # Both go into one dracut `ip=` stanza, so an IPv4 client with an IPv6
        # gateway is a static interface that routes nowhere and the machine
        # waiting for its passphrase can only be reached from the console.
        problems.append(
            f"the remote unlock address {unlock.address} is IPv{here.version} and its gateway "
            f"{unlock.gateway} is IPv{there.version}, so the initramfs has no route"
        )
    return problems


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
    elif _root_is_vfat(graph, root):
        problems.append(
            "the root filesystem is vfat, which cannot represent the Unix ownership, "
            "modes, or symlinks required by stage3"
        )

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
    problems += _logical_volume_size_problems(graph)
    return problems


def _root_is_vfat(graph: DeviceGraph, root: Mountpoint) -> bool:
    for parent in graph.ancestors_of(root.id):
        node = graph[parent]
        if isinstance(node, Filesystem) and node.kind is FilesystemType.VFAT:
            return True
    return False


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


def _logical_volume_size_problems(graph: DeviceGraph) -> list[str]:
    return [
        f"logical volume {volume.id} is {volume.size}"
        for volume in graph.of_type(LogicalVolume)
        if volume.size is not None and volume.size.bytes == 0
    ]


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
