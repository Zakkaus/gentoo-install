"""Runs an operation list against a machine.

This is the `Context` the plan layer declares. Everything it does goes through
`runner.py` or `probe.py`, so there is no second path to the disks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Sequence

from ..errors import InvalidLayout
from ..model.config import InstallConfig
from ..model.device import (
    DeviceId,
    Existing,
    LogicalVolume,
    Luks,
    MdRaid,
    Partition,
    PartitionTable,
    VolumeGroup,
)
from ..plan.operations import Operation
from . import fetch
from .probe import Probe
from .runner import Runner, under, write_file


@dataclass
class Machine:
    """The live implementation of `plan.operations.Context`."""

    config: InstallConfig
    runner: Runner
    probe: Probe
    work: Path
    mountpoint: Path = Path("/mnt/gentoo")
    keys: dict[DeviceId, PurePosixPath] = field(default_factory=dict)

    @property
    def target(self) -> PurePosixPath:
        return PurePosixPath(self.mountpoint)

    def run(self, argv: Sequence[str]) -> str:
        return self.runner.run(argv).stdout

    def run_in_target(self, argv: Sequence[str]) -> str:
        return self.runner.in_target(self.mountpoint).run(argv).stdout

    def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
        write_file(under(self.mountpoint, path), content, mode)

    def append(self, path: PurePosixPath, content: str) -> None:
        where = under(self.mountpoint, path)
        where.parent.mkdir(parents=True, exist_ok=True)
        with where.open("a") as handle:
            handle.write(content)

    def device_path(self, device: DeviceId) -> str:
        """An id becomes a path here and nowhere else.

        A node the configuration only names is resolved through its selector; a
        node the installer creates has a path only its creating operation knows,
        so those are derived from the node itself.
        """
        node = self.config.disk.graph[device]
        if isinstance(node, Existing):
            return self.probe.resolve(device, node.selector)
        if isinstance(node, Partition):
            return self._partition_path(node)
        if isinstance(node, Luks):
            return f"/dev/mapper/{node.name}"
        if isinstance(node, MdRaid):
            return f"/dev/md/{node.name}"
        if isinstance(node, LogicalVolume):
            group = self.config.disk.graph[node.group]
            if isinstance(group, VolumeGroup):
                return f"/dev/{group.name}/{node.name}"
        return self.probe.path_of(device)

    def _partition_path(self, node: Partition) -> str:
        """`/dev/vdb` plus index 2 is `/dev/vdb2`, but `/dev/nvme0n1` plus 2 is
        `/dev/nvme0n1p2`: a name ending in a digit takes the `p`."""
        table = self.config.disk.graph[node.table]
        if not isinstance(table, PartitionTable):
            raise InvalidLayout(f"{node.id} names {node.table}, which is not a partition table")
        disk = self.device_path(table.disk)
        separator = "p" if disk[-1].isdigit() else ""
        path = self.probe.wait_for(f"{disk}{separator}{node.index}")
        self.probe.remember(node.id, path)
        return path

    def key_file(self, device: DeviceId) -> PurePosixPath:
        """Staged under the work directory, which is a tmpfs on the install
        medium, so the passphrase never reaches a disk the installer wrote."""
        known = self.keys.get(device)
        if known is not None:
            return known
        path = self.work / "keys" / str(device)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_file():
            path.write_text(fetch.passphrase_for(device))
            path.chmod(0o600)
        staged = PurePosixPath(path)
        self.keys[device] = staged
        return staged

    def boot_disk(self) -> str:
        graph = self.config.disk.graph
        for node in graph.of_type(Existing):
            return self.probe.resolve(node.id, node.selector)
        raise InvalidLayout("the layout names no disk to install a bootloader on")

    def device_uuid(self, device: DeviceId) -> str:
        return self.probe.uuid_of(device)

    def fetch_stage3(self, mirror: str, variant: str, fingerprint: str) -> PurePosixPath:
        return PurePosixPath(fetch.stage3(mirror, variant, fingerprint, self.work, self.runner))


def apply(operations: Sequence[Operation], machine: Machine) -> None:
    """Perform each operation in order, stopping at the first failure.

    Nothing is retried and nothing is skipped: a disk operation that failed
    leaves a state the next one cannot assume anything about.
    """
    for operation in operations:
        machine.runner.log(f"[{operation.stage.value}] {operation.describe()}")
        operation.apply(machine)
