"""Checks that need more than one field, run before anything touches a disk.

Every problem is collected and reported together: fixing one rule per run means
one more failed run to learn about the next one.
"""

from __future__ import annotations

import ipaddress

import re
from collections import Counter
from pathlib import PurePosixPath
from typing import Final

from ..errors import ValidationFailed
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
    ZfsPool,
    ZfsTopology,
)
from .size import Size

_ROOT = PurePosixPath("/")


def validate(config: InstallConfig) -> None:
    problems = [
        *_layout_problems(config),
        *_root_size_problems(config),
        *_profile_problems(config),
        *_kernel_package_problems(config),
        *_reuse_problems(config),
        *_pool_problems(config),
        *_array_problems(config),
        *_network_problems(config),
        *_unlock_problems(config),
        *(rule.describe() for rule in compat.violations(config)),
    ]
    if problems:
        raise ValidationFailed(
            "the configuration does not describe an installable system:\n  " + "\n  ".join(problems)
        )


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


def _root_size_problems(config: InstallConfig) -> list[str]:
    """Checked here because the size is in the configuration: a root too small
    is knowable before anything is partitioned."""
    graph = config.disk.graph
    root = graph.nodes.get(config.disk.root)
    if not isinstance(root, Mountpoint):
        return []
    node = _nearest_sized(graph, root.id)
    if node is None:
        return []
    size = getattr(node, "size")
    return [
        f"{node.id} carries / and is {size}, under the {ROOT_MINIMUM} a stage3, "
        "a kernel and linux-firmware need"
    ] if size < ROOT_MINIMUM else []


def _nearest_sized(graph: DeviceGraph, device: DeviceId) -> Node | None:
    """The first node with a size, walking down from the mount point.

    The nearest one, not every ancestor: a logical volume or an array is as
    large as its members together, and testing each member refused a root that
    is the right size for being built out of small ones.
    """
    seen: set[DeviceId] = set()
    edge = [device]
    while edge:
        following: list[DeviceId] = []
        for current in edge:
            if current in seen or current not in graph.nodes:
                continue
            seen.add(current)
            node = graph[current]
            if isinstance(getattr(node, "size", None), Size):
                return node
            following.extend(node.inputs)
        edge = following
    return None


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

    for path, count in Counter(mount.path for mount in graph.of_type(Mountpoint)).items():
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
    return problems
