"""The device graph: what the target machine's storage should look like.

Every node carries an id chosen by the configuration, and references its inputs
by id rather than by path. Paths are assigned by the kernel at probe time and
change between boots; ids do not, so a run that stops halfway can resume against
the same graph.

Ordering is not decided here. `plan/disk.py` topologically sorts this graph into
the operation sequence; this module only rejects a graph that cannot be ordered.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Iterable, Mapping, NewType, TypeVar

from ..errors import DeviceCycle, DuplicateDeviceId, UnknownDeviceId
from .size import Size

DeviceId = NewType("DeviceId", str)

#: Any node kind, for the lookups that filter a graph by class.
T = TypeVar("T", bound="Node")


class TableType(Enum):
    GPT = "gpt"
    MBR = "mbr"


class PartitionRole(Enum):
    """What the partition is for. Decides its GPT type code and its flags."""

    ESP = "esp"
    BIOS_BOOT = "bios-boot"
    SWAP = "swap"
    RAID = "raid"
    LVM = "lvm"
    ZFS = "zfs"
    DATA = "data"


class RaidLevel(Enum):
    """Spelled as `mdadm --level` names them, because the parser reads an enum
    from a TOML string and an integer value can never match one."""

    RAID0 = "raid0"
    RAID1 = "raid1"
    RAID5 = "raid5"
    RAID6 = "raid6"

    @property
    def minimum(self) -> int:
        """Members the level needs. `mdadm --create` refuses fewer, and it
        refuses them after the disks have already been partitioned."""
        return {
            RaidLevel.RAID0: 2,
            RaidLevel.RAID1: 2,
            RaidLevel.RAID5: 3,
            RaidLevel.RAID6: 4,
        }[self]


class RaidMetadata(Enum):
    """mdadm superblock format, as `--metadata` spells it."""

    V0_90 = "0.90"
    V1_0 = "1.0"
    V1_1 = "1.1"
    V1_2 = "1.2"

    @property
    def superblock_at_start(self) -> bool:
        """0.90 and 1.0 sit at the end of the member, so firmware that knows
        nothing about mdraid still reads the member as a plain filesystem."""
        return self in (RaidMetadata.V1_1, RaidMetadata.V1_2)


class FilesystemType(Enum):
    EXT2 = "ext2"
    EXT3 = "ext3"
    EXT4 = "ext4"
    BTRFS = "btrfs"
    XFS = "xfs"
    F2FS = "f2fs"
    VFAT = "vfat"


@dataclass(frozen=True)
class Node:
    """A device the installer creates, or an existing one it reuses."""

    id: DeviceId

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        """Ids this node is built from. Empty for a node with no inputs."""
        return ()


@dataclass(frozen=True)
class Existing(Node):
    """A device already present on the machine, named by a selector.

    The selector is resolved by `exec/probe.py`, never here: the model must stay
    parseable on a machine that does not have the device.
    """

    selector: str
    wipe: bool = False
    #: None until probed and empty when no array was found, so compatibility
    #: can distinguish missing evidence from a machine with no superblock.
    mdraid_metadata: str | None = None


@dataclass(frozen=True)
class Extent:
    """A byte range on a disk, `end` inclusive, as `parted` reports one."""

    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True)
class PartitionTable(Node):
    disk: DeviceId
    table: TableType
    #: Whether the table is written from scratch. Off edits the one already on
    #: the disk, which is what lets a partition be kept beside a new one:
    #: `sgdisk --zap-all` would take every entry with it.
    create: bool = True
    #: Entries removed from a table that is not written from scratch. Deleting
    #: is an edit to the table, so it belongs to the table and not to a node of
    #: its own: the partition it names stops existing.
    remove: tuple[int, ...] = ()
    #: Where the disk has no partition now, in bytes, filled in by the probe
    #: before validation. Empty for a table written from scratch, and empty in
    #: a configuration file: the model cannot read a partition table, and
    #: without this an added partition on an edited table was placed at 1MiB
    #: on top of one the operator kept.
    free_extents: tuple[Extent, ...] = ()

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return (self.disk,)


@dataclass(frozen=True)
class Partition(Node):
    table: DeviceId
    index: int
    role: PartitionRole
    #: None means "all remaining space"; only the last partition may use it.
    size: Size | None
    label: str = ""

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return (self.table,)


@dataclass(frozen=True)
class Luks(Node):
    backing: DeviceId
    name: str
    #: A path on the installing system, never the passphrase: this file is
    #: copied into the target and the install log is pasted into bug reports.
    passphrase_file: str = ""

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return (self.backing,)


@dataclass(frozen=True)
class MdRaid(Node):
    members: tuple[DeviceId, ...]
    level: RaidLevel
    name: str
    #: mdadm's own default. An array holding the esp needs 0.90 or 1.0.
    metadata: RaidMetadata = RaidMetadata.V1_2

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return self.members


@dataclass(frozen=True)
class VolumeGroup(Node):
    members: tuple[DeviceId, ...]
    name: str

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return self.members


@dataclass(frozen=True)
class LogicalVolume(Node):
    group: DeviceId
    name: str
    #: None means "all remaining space in the group".
    size: Size | None

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return (self.group,)


class ZfsTopology(Enum):
    """How `zpool create` joins the vdevs.

    A bare list of devices is a stripe: `zpool create p a b` survives losing
    neither. The keyword is what makes it redundant, so the default here is
    the one that is safe to assume for a single device and refused for more.
    """

    STRIPE = "stripe"
    MIRROR = "mirror"
    RAIDZ1 = "raidz1"
    RAIDZ2 = "raidz2"
    RAIDZ3 = "raidz3"

    @property
    def keyword(self) -> str:
        """What goes on the `zpool create` line before the devices. A stripe
        has none: the devices follow the pool name directly."""
        return "" if self is ZfsTopology.STRIPE else self.value

    @property
    def minimum(self) -> int:
        """Devices this topology needs. `zpool create` refuses fewer, after the
        disks have already been partitioned."""
        return {
            ZfsTopology.STRIPE: 1,
            ZfsTopology.MIRROR: 2,
            ZfsTopology.RAIDZ1: 2,
            ZfsTopology.RAIDZ2: 3,
            ZfsTopology.RAIDZ3: 4,
        }[self]


@dataclass(frozen=True)
class ZfsPool(Node):
    vdevs: tuple[DeviceId, ...]
    name: str
    topology: ZfsTopology = ZfsTopology.STRIPE
    encrypted: bool = False
    #: A path, never the passphrase. Same rule as `Luks.passphrase_file`.
    passphrase_file: str = ""

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return self.vdevs


@dataclass(frozen=True)
class ZfsDataset(Node):
    pool: DeviceId
    name: str

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return (self.pool,)


@dataclass(frozen=True)
class Filesystem(Node):
    device: DeviceId
    kind: FilesystemType
    label: str = ""
    #: False for one that is already there: nothing is formatted, the type is
    #: verified against the disk instead, and its data survives the install.
    create: bool = True

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return (self.device,)


@dataclass(frozen=True)
class Subvolume(Node):
    filesystem: DeviceId
    name: str

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return (self.filesystem,)


@dataclass(frozen=True)
class Swap(Node):
    device: DeviceId

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return (self.device,)


@dataclass(frozen=True)
class Mountpoint(Node):
    """Where a filesystem, subvolume or dataset is mounted in the target."""

    source: DeviceId
    path: PurePosixPath
    options: tuple[str, ...] = ()

    @property
    def inputs(self) -> tuple[DeviceId, ...]:
        return (self.source,)


@dataclass(frozen=True, init=False)
class DeviceGraph:
    """A validated graph whose nodes cannot change after construction."""

    nodes: Mapping[DeviceId, Node]

    def __init__(self, nodes: Iterable[Node] | Mapping[DeviceId, Node]) -> None:
        source = nodes.values() if isinstance(nodes, Mapping) else nodes
        by_id: dict[DeviceId, Node] = {}
        for node in source:
            if node.id in by_id:
                raise DuplicateDeviceId(f"two nodes claim the id {node.id!r}")
            by_id[node.id] = node
        for node in by_id.values():
            for parent in node.inputs:
                if parent not in by_id:
                    raise UnknownDeviceId(
                        f"{node.id!r} is built from {parent!r}, which no node defines"
                    )
        _reject_cycles(by_id)
        object.__setattr__(self, "nodes", MappingProxyType(by_id))

    @classmethod
    def build(cls, nodes: Iterable[Node]) -> DeviceGraph:
        return cls(nodes=nodes)

    def __getitem__(self, device: DeviceId) -> Node:
        try:
            return self.nodes[device]
        except KeyError:
            raise UnknownDeviceId(f"no node with id {device!r}") from None

    def inputs_of(self, device: DeviceId) -> tuple[DeviceId, ...]:
        return self[device].inputs

    def consumers_of(self, device: DeviceId) -> tuple[DeviceId, ...]:
        self[device]
        return tuple(
            node.id for node in self.nodes.values() if device in node.inputs
        )

    def ancestors_of(self, device: DeviceId) -> frozenset[DeviceId]:
        """Every id this node is transitively built from, excluding itself."""
        seen: set[DeviceId] = set()
        stack = list(self[device].inputs)
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self[current].inputs)
        return frozenset(seen)

    def of_type(self, kind: type[T]) -> tuple[T, ...]:
        return tuple(node for node in self.nodes.values() if isinstance(node, kind))


class _Mark(Enum):
    OPEN = "open"
    DONE = "done"


def _reject_cycles(nodes: Mapping[DeviceId, Node]) -> None:
    """Depth-first walk with an explicit stack.

    Recursion would be shorter, but a configuration file controls the depth and
    Python's recursion limit is not a diagnostic anyone can act on.
    """
    marks: dict[DeviceId, _Mark] = {}
    for start in nodes:
        if start in marks:
            continue
        stack: list[tuple[DeviceId, bool]] = [(start, False)]
        path: list[DeviceId] = []
        while stack:
            current, leaving = stack.pop()
            if leaving:
                marks[current] = _Mark.DONE
                path.pop()
                continue
            mark = marks.get(current)
            if mark is _Mark.DONE:
                continue
            if mark is _Mark.OPEN:
                cycle = path[path.index(current) :] + [current]
                raise DeviceCycle("device cycle: " + " -> ".join(cycle))
            marks[current] = _Mark.OPEN
            path.append(current)
            stack.append((current, True))
            for parent in nodes[current].inputs:
                stack.append((parent, False))
