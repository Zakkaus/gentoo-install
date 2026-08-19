# SPDX-License-Identifier: GPL-2.0-or-later
"""Runs an operation list against a machine.

This is the `Context` the plan layer declares. Everything it does goes through
`runner.py` or `probe.py`, so there is no second path to the disks.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import time
from dataclasses import astuple, dataclass, field, is_dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from ..errors import CommandFailed, GentooInstallError, InvalidLayout, TargetEscape
from ..model.config import InstallConfig
from ..model.validate import KernelCeiling
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
from ..plan.operations import CommandOutput, Operation
from . import fetch, packages
from .preflight import SecretStore
from .probe import Probe
from .runner import Runner, open_in_target, under


@dataclass
class Machine:
    """The live implementation of `plan.operations.Context`."""

    config: InstallConfig
    runner: Runner
    probe: Probe
    work: Path
    mountpoint: Path = Path("/mnt/gentoo")
    keys: dict[DeviceId, PurePosixPath] = field(default_factory=dict)
    secrets: SecretStore | None = None
    given_up: set[str] = field(default_factory=set)
    image_devices: dict[DeviceId, str] = field(default_factory=dict)

    @property
    def target(self) -> PurePosixPath:
        return PurePosixPath(self.mountpoint)

    def run(
        self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
    ) -> str:
        result = self.runner.run(argv, check=check, input_text=input_text)
        return CommandOutput(result.stdout, result.returncode)

    def pipe(self, producer: Sequence[str], consumer: Sequence[str]) -> None:
        self.runner.pipe(producer, consumer)

    def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> str:
        result = self.runner.in_target(self.mountpoint).run(argv, check=check)
        return CommandOutput(result.stdout, result.returncode)

    def installed_package_paths(self, package: str) -> frozenset[PurePosixPath]:
        return packages.installed_package_paths(self.mountpoint, package)

    def installed_command_help(self, package: str, command: PurePosixPath) -> str:
        result = self.runner.in_target(self.mountpoint).run([str(command), "--help"], check=False)
        if result.returncode != 0:
            raise CommandFailed(
                f"cannot verify options installed by {package}: "
                f"{command} --help exited {result.returncode}"
            )
        return result.stdout

    def target_is_directory(self, path: PurePosixPath) -> bool:
        return packages.target_is_directory(self.mountpoint, path)

    def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
        # Beside it and renamed over, never in place: a run cut short between
        # the truncate and the write leaves a half-written `fstab` or
        # `make.conf`, and `--resume` reads it as the operator's own.
        beside = path.with_name(f".{path.name}.gentoo-install")
        # The mode is set at open time: writing first and narrowing afterwards
        # leaves a secret readable for the interval in between.
        handle = open_in_target(
            self.mountpoint, beside, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode, parents=True
        )
        with os.fdopen(handle, "w") as opened:
            opened.write(content)
            opened.flush()
            os.fsync(opened.fileno())
        written = under(self.mountpoint, beside)
        os.chmod(written, mode, follow_symlinks=False)
        destination = under(self.mountpoint, path)
        # `os.replace` takes the symlink's place rather than following it, so
        # the refusal `open_in_target` gives the other paths has to be made
        # here as well: a stage3 that ships `/etc/mtab` as a link out of the
        # target must not have it quietly replaced either.
        if os.path.islink(destination):
            os.unlink(written)
            raise TargetEscape(f"{path} in the target is a symlink")
        os.replace(written, destination)

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
            return self.image_devices.get(device) or self.probe.resolve(device, node.selector)
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

    def remember_image_device(self, device: DeviceId, path: str) -> None:
        self.image_devices[device] = path

    def image_device_path(self, device: DeviceId) -> str | None:
        return self.image_devices.get(device)

    def release_image_device(self, device: DeviceId) -> None:
        self.image_devices.pop(device, None)

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
        try:
            return self._secret_path(device).read_text()
        except OSError as error:
            raise InvalidLayout(f"approved passphrase for {device} cannot be read: {error}") from error

    def _secret_path(self, device: DeviceId) -> Path:
        return (self.secrets or SecretStore(self.work)).path(device)

    def cleanup_secrets(self) -> None:
        for stayed in (self.secrets or SecretStore(self.work)).cleanup():
            self.runner.log(f"WARNING: a staged passphrase stayed: {stayed}")

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
        path = self._secret_path(device)
        if not path.is_file():
            raise InvalidLayout(f"approved passphrase for {device} was not staged")
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
        # Through the proxy, like the stage3 below: on a machine whose only
        # route out is the proxy every candidate times out instead, and the
        # ranking degrades to the order it was given.
        ranked = fetch.rank_mirrors(candidates, self.config.proxy)
        self.runner.log(f"mirrors, fastest first: {', '.join(ranked)}")
        return ranked

    def fetch_text(self, url: str) -> str:
        self.runner.log(f"fetching {url}")
        return fetch.text(url, self.config.proxy)

    def fetch_stage3(
        self,
        mirror: str,
        variant: str,
        fingerprint: str,
        fallbacks: Sequence[str] = (),
    ) -> PurePosixPath:
        """Downloaded onto the target, not into the work directory: that is a
        tmpfs on an install medium, and a stage3 there costs the memory the
        emerge is about to need."""
        return PurePosixPath(
            fetch.stage3(
                mirror,
                variant,
                fingerprint,
                self.mountpoint / STAGE3_CACHE,
                self.runner,
                self.config.proxy,
                fallbacks,
            )
        )

    def zfs_kernel_max(self) -> KernelCeiling:
        """Read the selected target tree's cached ZFS dependency ceiling."""
        return self.probe.zfs_kernel_max(self.mountpoint)


@lru_cache(maxsize=None)
def _implementation(kind: type[Operation]) -> str:
    """A digest of the class that will perform the operation.

    Position and description are not identity. Commit `57f5ad3` changed
    `ConfigureInstallKernel` from writing `/etc/kernel/install.conf` to writing
    a drop-in, and left its `describe()` alone: a journal from before it let a
    resumed run skip the corrected implementation. The source is what changed,
    so the source is what the identity has to cover.
    """
    try:
        source = inspect.getsource(kind)
    except (OSError, TypeError):
        # Nothing to read is not evidence that nothing changed, so the name
        # alone identifies it and the fields below still carry the payload.
        source = ""
    return hashlib.sha256(f"{kind.__qualname__}\n{source}".encode()).hexdigest()[:16]


def identity(operation: Operation) -> str:
    """What has to match for a journal entry to describe this operation.

    Hashed rather than recorded: an operation's fields name key files and
    device selectors, and the journal is copied into the installed system.
    """
    fields = repr(astuple(operation)) if is_dataclass(operation) else repr(operation)
    together = f"{_implementation(type(operation))}\n{fields}"
    return hashlib.sha256(together.encode()).hexdigest()[:16]


def already_degraded(journal: Journal | None) -> set[str]:
    """What a previous run gave up on, so a resume gives up on it too.

    Without this a resumed run rebuilt an empty `given_up`: the operation that
    recorded an unusable binary host had already completed and was skipped, so
    the next `Emerge` asked the host for packages the earlier run had declared
    untrusted, and the record of where each package came from changed across
    the resume boundary.
    """
    if journal is None:
        return set()
    return {
        str(entry["what"])
        for entry in journal.replay()
        if entry.get("event") == "degraded" and entry.get("what")
    }


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
            done.add((position, str(entry.get("identity", ""))))
    return frozenset(done)


def apply(
    operations: Sequence[Operation],
    machine: Machine,
    finished: frozenset[tuple[int, str]] = frozenset(),
    on_start: Callable[[Operation], None] | None = None,
) -> None:
    """Perform each operation in order, stopping at the first failure.

    Nothing is retried: a disk operation that failed leaves a state the next
    one cannot assume anything about. `finished` names what an earlier run
    completed, so a resumed run does not partition a disk it already installed
    onto. `on_start` runs before the operation because a failed command may
    still have changed the machine.
    """
    total = len(operations)
    opened = time.monotonic()
    try:
        for position, operation in enumerate(operations):
            counted = f"[{position + 1}/{total} {_elapsed(time.monotonic() - opened)}]"
            if operation.survives_a_reboot and (position, identity(operation)) in finished:
                machine.runner.log(
                    f"{counted} [{operation.stage.value}] done earlier: {operation.describe()}"
                )
                continue
            if on_start is not None:
                on_start(operation)
            machine.runner.log(f"{counted} [{operation.stage.value}] {operation.describe()}")
            started = time.monotonic()
            try:
                operation.apply(machine)
            except GentooInstallError:
                _record(machine, operation, started, "failed", position)
                raise
            _record(machine, operation, started, "done", position)
    finally:
        machine.cleanup_secrets()


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
        identity=identity(operation),
        seconds=round(time.monotonic() - started, 3),
        status=status,
        position=position,
    )
