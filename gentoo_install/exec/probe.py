"""The only module that reads the machine's state.

Everything else asks this. `preflight.py` in particular holds no `os.path` call
of its own, so its checks can be exercised against a described machine rather
than only against the one running the tests.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ..errors import DeviceNotFound
from ..model.device import DeviceId
from .runner import Runner

EFI_MARKER: Final[Path] = Path("/sys/firmware/efi")
MEMINFO: Final[Path] = Path("/proc/meminfo")


@dataclass(frozen=True)
class Machine:
    """What the installing system is, as far as the installer cares."""

    architecture: str
    uefi: bool
    root: bool
    memory_bytes: int
    commands: frozenset[str]


@dataclass
class Probe:
    """Resolves ids to paths and answers questions about the machine.

    The id to path map is cached in the work directory: a run that stops halfway
    resumes against the same devices even if the kernel renumbered them.
    """

    runner: Runner
    work: Path
    resolved: dict[DeviceId, str] = field(default_factory=dict)
    uuids: dict[DeviceId, str] = field(default_factory=dict)

    def machine(self, wanted: frozenset[str] = frozenset()) -> Machine:
        return Machine(
            architecture=platform.machine(),
            uefi=EFI_MARKER.is_dir(),
            root=os.geteuid() == 0,
            memory_bytes=self._memory(),
            commands=frozenset(name for name in wanted if shutil.which(name) is not None),
        )

    def resolve(self, device: DeviceId, selector: str) -> str:
        """Turn a selector from the configuration into a path that exists now."""
        known = self.resolved.get(device)
        if known is not None:
            return known
        candidate = Path(selector)
        if not candidate.exists():
            raise DeviceNotFound(f"{device}: {selector} is not present on this machine")
        path = str(candidate.resolve())
        self.resolved[device] = path
        self.save()
        return path

    def remember(self, device: DeviceId, path: str) -> None:
        """Record a device the installer created, such as a LUKS mapping."""
        self.resolved[device] = path
        self.save()

    def path_of(self, device: DeviceId) -> str:
        path = self.resolved.get(device)
        if path is None:
            raise DeviceNotFound(f"{device} has no path yet; nothing has created it")
        return path

    def uuid_of(self, device: DeviceId) -> str:
        """`blkid` after a fresh mkfs can miss the new signature, so the value is
        cached the first time it is read and reused afterwards."""
        known = self.uuids.get(device)
        if known is not None:
            return known
        self.runner.run(["udevadm", "settle"], check=False)
        result = self.runner.run(
            ["blkid", "--match-tag", "UUID", "--output", "value", self.path_of(device)]
        )
        uuid = result.stdout.strip()
        if not uuid:
            raise DeviceNotFound(f"{device} has no UUID; was it formatted?")
        self.uuids[device] = uuid
        self.save()
        return uuid

    def disk_of(self, device: DeviceId) -> str:
        """The whole disk a partition sits on, which is what a bootloader wants."""
        path = self.path_of(device)
        result = self.runner.run(["lsblk", "--noheadings", "--output", "PKNAME", path])
        parent = result.stdout.strip().splitlines()
        return f"/dev/{parent[0].strip()}" if parent and parent[0].strip() else path

    def wait_for(self, path: str, seconds: float = 15.0) -> str:
        """Wait for a device node to appear.

        `partprobe` returns before udev has finished creating the nodes, so the
        first operation that wants a new partition would otherwise find nothing.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if Path(path).exists():
                return path
            self.runner.run(["udevadm", "settle"], check=False)
            time.sleep(0.5)
        raise DeviceNotFound(f"{path} did not appear within {seconds:.0f}s")

    def mounted(self, path: Path) -> bool:
        return self.runner.run(["findmnt", "--mountpoint", str(path)], check=False).returncode == 0

    def save(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "devices.json").write_text(
            json.dumps({"paths": self.resolved, "uuids": self.uuids}, indent=2, sort_keys=True)
        )

    def load(self) -> None:
        cache = self.work / "devices.json"
        if not cache.is_file():
            return
        raw = json.loads(cache.read_text())
        self.resolved = {DeviceId(key): value for key, value in raw.get("paths", {}).items()}
        self.uuids = {DeviceId(key): value for key, value in raw.get("uuids", {}).items()}

    def _memory(self) -> int:
        if not MEMINFO.is_file():
            return 0
        for line in MEMINFO.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
        return 0
