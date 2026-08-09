"""Runs an operation list against a machine.

This is the `Context` the plan layer declares. Everything it does goes through
`runner.py` or `probe.py`, so there is no second path to the disks.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Sequence

from ..errors import GentooInstallError, InvalidLayout
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
    ZfsPool,
)
from ..log import Journal
from ..plan.disk import STAGE3_CACHE
from ..plan.operations import Operation
from . import fetch
from .probe import Probe
from .runner import Runner, open_in_target, under, write_file


@dataclass
class Machine:
    """The live implementation of `plan.operations.Context`."""

    config: InstallConfig
    runner: Runner
    probe: Probe
    work: Path
    mountpoint: Path = Path("/mnt/gentoo")
    keys: dict[DeviceId, PurePosixPath] = field(default_factory=dict)
    given_up: set[str] = field(default_factory=set)

    @property
    def target(self) -> PurePosixPath:
        return PurePosixPath(self.mountpoint)

    def run(
        self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
    ) -> str:
        return self.runner.run(argv, check=check, input_text=input_text).stdout

    def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> str:
        return self.runner.in_target(self.mountpoint).run(argv, check=check).stdout

    def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
        # The mode is set at open time: writing first and narrowing afterwards
        # leaves a secret readable for the interval in between.
        handle = open_in_target(
            self.mountpoint, path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode, parents=True
        )
        with os.fdopen(handle, "w") as opened:
            opened.write(content)
        os.chmod(under(self.mountpoint, path), mode, follow_symlinks=False)

    def read(self, path: PurePosixPath) -> str:
        """Empty for a file that is not there, which is the normal case before
        the stage3 is unpacked. Any other failure is raised: swallowing it made
        `merge` replace the stage3's make.conf instead of editing it."""
        try:
            handle = open_in_target(self.mountpoint, path, os.O_RDONLY)
        except (FileNotFoundError, NotADirectoryError):
            return ""
        with os.fdopen(handle, "r") as opened:
            return opened.read()

    def append(self, path: PurePosixPath, content: str) -> None:
        handle = open_in_target(
            self.mountpoint, path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, parents=True
        )
        with os.fdopen(handle, "a") as opened:
            opened.write(content)

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

    def passphrase(self, device: DeviceId) -> str:
        """The passphrase itself, for a command that reads one on stdin."""
        node = self.config.disk.graph[device]
        source = node.passphrase_file if isinstance(node, (Luks, ZfsPool)) else ""
        return fetch.passphrase_for(device, source)

    def key_file(self, device: DeviceId) -> PurePosixPath:
        """Where the passphrase is staged, as a path on the installing system.

        Under the work directory, which is a tmpfs on an install medium, so the
        passphrase never reaches a disk the installer wrote. A file left by an
        earlier run is rewritten rather than reused: a changed passphrase would
        otherwise be silently ignored.
        """
        known = self.keys.get(device)
        if known is not None:
            return known
        node = self.config.disk.graph[device]
        source = node.passphrase_file if isinstance(node, (Luks, ZfsPool)) else ""
        path = self.work / "keys" / str(device)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_file(path, fetch.passphrase_for(device, source), 0o600)
        staged = PurePosixPath(path)
        self.keys[device] = staged
        return staged

    def array_uuid(self, device: DeviceId) -> str:
        exported = self.runner.run(["mdadm", "--detail", "--export", self.device_path(device)])
        for line in exported.stdout.splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "MD_UUID" and value.strip():
                return value.strip()
        raise InvalidLayout(f"mdadm reports no MD_UUID for {device}")

    def degrade(self, what: str, reason: str) -> None:
        self.given_up.add(what)
        self.runner.log(f"WARNING: {what} is unavailable, so {reason}")
        if self.runner.journal is not None:
            self.runner.journal.degraded(what, reason)

    def degraded(self, what: str) -> bool:
        return what in self.given_up

    def is_mounted(self, path: str) -> bool:
        return self.runner.run(["findmnt", "--mountpoint", path], check=False).returncode == 0

    def containing_disk(self, device: DeviceId) -> str:
        """The whole disk a device sits on, which is what a bootloader wants.

        Derived from the graph rather than from whichever disk the graph happens
        to yield first: a layout with a second disk would otherwise have its
        bootloader written to the wrong one. Sorted for the same reason
        `bootloader.py` sorts: `ancestors_of` is a frozenset, so a mirrored root
        answered with a different disk on every run.
        """
        graph = self.config.disk.graph
        tables = {table.disk for table in graph.of_type(PartitionTable)}
        for parent in (device, *sorted(graph.ancestors_of(device))):
            node = graph[parent]
            if not isinstance(node, Existing):
                continue
            path = self.probe.resolve(node.id, node.selector)
            if node.id in tables:
                return path
            # A reused partition is an `Existing` too, and its selector names
            # the partition. `grub-install /dev/sda2` writes into a partition
            # boot sector or refuses outright, so the parent decides.
            return self.probe.disk_of_path(path)
        raise InvalidLayout(f"nothing under {device} is a disk this installer can boot from")

    def partition_index(self, device: DeviceId) -> int:
        node = self.config.disk.graph[device]
        if isinstance(node, Partition):
            return node.index
        # A reused partition carries no index: the operator named a device, so
        # the number comes from the machine. Refusing here left ZFSBootMenu on
        # an existing esp failing with `is not a partition`.
        if isinstance(node, Existing):
            return self.probe.partition_number_of_path(
                self.probe.resolve(node.id, node.selector)
            )
        raise InvalidLayout(f"{device} is not a partition, so it has no number")

    def jobs(self) -> int:
        return os.cpu_count() or 1

    def device_uuid(self, device: DeviceId) -> str:
        return self.probe.uuid_of(self.device_path(device), device)

    def filesystem_type(self, device: DeviceId) -> str:
        return self.probe.filesystem_type_of(self.device_path(device))

    def rank_mirrors(self, candidates: tuple[str, ...]) -> tuple[str, ...]:
        ranked = fetch.rank_mirrors(candidates)
        self.runner.log(f"mirrors, fastest first: {', '.join(ranked)}")
        return ranked

    def fetch_text(self, url: str) -> str:
        self.runner.log(f"fetching {url}")
        return fetch.text(url)

    def fetch_stage3(self, mirror: str, variant: str, fingerprint: str) -> PurePosixPath:
        """Downloaded onto the target, not into the work directory: that is a
        tmpfs on an install medium, and a stage3 there costs the memory the
        emerge is about to need."""
        return PurePosixPath(
            fetch.stage3(mirror, variant, fingerprint, self.mountpoint / STAGE3_CACHE, self.runner)
        )


def completed(journal: Journal | None) -> frozenset[tuple[int, str]]:
    """Operations a previous run finished, as position and description.

    The position is read from the entry, not counted while replaying. Counting
    drifted twice over: a failed attempt consumed a number, and a resumed run
    appends to the same file, so after one resume every position was wrong and
    the next resume re-ran operations that had already partitioned the disk.
    Position as well as text, because a plan can hold two operations whose
    descriptions match.
    """
    if journal is None:
        return frozenset()
    done: set[tuple[int, str]] = set()
    for entry in journal.replay():
        if entry.get("event") != "operation" or entry.get("status") != "done":
            continue
        position = entry.get("position")
        # An entry from before positions were recorded says nothing reliable
        # about where it sat, and redoing work is safer than skipping it.
        if isinstance(position, int):
            done.add((position, str(entry.get("describe", ""))))
    return frozenset(done)


def apply(
    operations: Sequence[Operation],
    machine: Machine,
    finished: frozenset[tuple[int, str]] = frozenset(),
) -> None:
    """Perform each operation in order, stopping at the first failure.

    Nothing is retried: a disk operation that failed leaves a state the next
    one cannot assume anything about. `finished` names what an earlier run
    completed, so a resumed run does not partition a disk it already installed
    onto.
    """
    total = len(operations)
    opened = time.monotonic()
    for position, operation in enumerate(operations):
        counted = f"[{position + 1}/{total} {_elapsed(time.monotonic() - opened)}]"
        if operation.survives_a_reboot and (position, operation.describe()) in finished:
            machine.runner.log(
                f"{counted} [{operation.stage.value}] done earlier: {operation.describe()}"
            )
            continue
        machine.runner.log(f"{counted} [{operation.stage.value}] {operation.describe()}")
        started = time.monotonic()
        try:
            operation.apply(machine)
        except GentooInstallError:
            _record(machine, operation, started, "failed", position)
            raise
        _record(machine, operation, started, "done", position)


def _elapsed(seconds: float) -> str:
    """How long the run has been going. A desktop emerge takes hours and the
    only clock on the screen was the operator's own."""
    whole = int(seconds)
    return f"{whole // 3600:d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def _record(
    machine: Machine, operation: Operation, started: float, status: str, position: int
) -> None:
    if machine.runner.journal is None:
        return
    machine.runner.journal.write(
        "operation",
        stage=operation.stage.value,
        describe=operation.describe(),
        seconds=round(time.monotonic() - started, 3),
        status=status,
        position=position,
    )
