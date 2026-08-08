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
from typing import ClassVar, Final, Iterable, Mapping

from ..errors import DeviceNotFound
from ..model.device import DeviceId
from .runner import Runner

EFI_MARKER: Final[Path] = Path("/sys/firmware/efi")
#: Where both install media keep the release engineering public key.
RELEASE_KEY: Final[Path] = Path("/usr/share/openpgp-keys/gentoo-release.asc")
MEMINFO: Final[Path] = Path("/proc/meminfo")


@dataclass(frozen=True)
class Machine:
    """What the installing system is, as far as the installer cares."""

    architecture: str
    uefi: bool
    root: bool
    memory_bytes: int
    commands: frozenset[str]
    #: Whether the medium ships the key a stage3 signature is checked against.
    release_key: bool
    #: What `--version` said, for the commands whose implementation matters.
    #: A busybox applet satisfies `which` and then rejects the flags.
    versions: Mapping[str, str] = field(default_factory=dict)


@dataclass
class Probe:
    """Resolves ids to paths and answers questions about the machine.

    The id to path map is cached in the work directory: a run that stops halfway
    resumes against the same devices even if the kernel renumbered them.
    """

    runner: Runner
    work: Path
    resolved: dict[DeviceId, str] = field(default_factory=dict)

    def versions(self, wanted: Iterable[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for command in wanted:
            if shutil.which(command) is None:
                continue
            found[command] = self.runner.run([command, "--version"], check=False).stdout
        return found

    def machine(self, wanted: frozenset[str] = frozenset()) -> Machine:
        return Machine(
            architecture=platform.machine(),
            uefi=EFI_MARKER.is_dir(),
            root=os.geteuid() == 0,
            memory_bytes=self._memory(),
            commands=frozenset(name for name in wanted if shutil.which(name) is not None),
            release_key=RELEASE_KEY.is_file(),
            versions=self.versions(self.check_versions_of),
        )

    def resolve(self, device: DeviceId, selector: str) -> str:
        """Turn a selector from the configuration into a path that exists now.

        The selector is resolved every time rather than cached: a
        `/dev/disk/by-id/...` name survives the kernel renumbering its disks and
        the `/dev/sda` it points at today does not.
        """
        candidate = Path(selector)
        if not candidate.exists():
            raise DeviceNotFound(f"{device}: {selector} is not present on this machine")
        return str(candidate.resolve())

    def remember(self, device: DeviceId, path: str) -> None:
        """Record a device the installer created, such as a LUKS mapping."""
        self.resolved[device] = path
        self.save()

    def path_of(self, device: DeviceId) -> str:
        path = self.resolved.get(device)
        if path is None:
            raise DeviceNotFound(f"{device} has no path yet; nothing has created it")
        return path

    def uuid_of(self, path: str, device: DeviceId) -> str:
        """Read a formatted device's UUID. Never cached: a device formatted a
        second time keeps the first UUID in a cache, and fstab then names a
        filesystem that no longer exists."""
        self.runner.run(["udevadm", "settle"], check=False)
        result = self.runner.run(
            ["blkid", "--match-tag", "UUID", "--output", "value", path], check=False
        )
        uuid = result.stdout.strip()
        if not uuid:
            raise DeviceNotFound(f"{device} has no UUID; was it formatted?")
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

    #: `lsblk` calls these TYPE=disk and none of them is an install target:
    #: compressed swap, a loopback of the live image, a ramdisk.
    NOT_A_TARGET: ClassVar[tuple[str, ...]] = ("/dev/zram", "/dev/loop", "/dev/ram")

    #: Commands whose implementation `preflight` has to judge. Set by the
    #: caller so the table of what GNU means stays in one module.
    check_versions_of: ClassVar[tuple[str, ...]] = ("tar",)

    def disks(self) -> tuple[tuple[str, str], ...]:
        """Whole disks the interface can offer, as a selector and a size.

        Named by `/dev/disk/by-id/` where one exists: a kernel name is assigned
        at probe time and a configuration saved with one installs elsewhere on
        the next boot.
        """
        listed = self.runner.run(
            ["lsblk", "--noheadings", "--nodeps", "--paths", "--output", "NAME,SIZE,TYPE,MODEL"],
            check=False,
        )
        found: list[tuple[str, str]] = []
        for line in listed.stdout.splitlines():
            fields = line.split(maxsplit=3)
            if len(fields) < 3 or fields[2] != "disk":
                continue
            path, size = fields[0], fields[1]
            if path.startswith(self.NOT_A_TARGET):
                continue
            model = fields[3] if len(fields) > 3 else ""
            found.append((self._stable_name(path), f"{size} {model}".strip()))
        return tuple(found)

    def _stable_name(self, path: str) -> str:
        links = self.runner.run(["find", "/dev/disk/by-id", "-lname", f"*/{path.rsplit('/', 1)[-1]}"], check=False)
        names = sorted(line.strip() for line in links.stdout.splitlines() if line.strip())
        # Prefer a by-id name that is not a wwn: a wwn is stable but says
        # nothing a person can recognise.
        for name in names:
            if "/wwn-" not in name:
                return name
        return names[0] if names else path

    def mounted(self, disk: str) -> bool:
        """Whether a disk, or any partition on it, is in use.

        `findmnt --mountpoint` answers about a directory, so asking it about
        `/dev/sda` always said no and the guard against repartitioning a disk in
        use could never fire.
        """
        # The runner merges stderr into stdout, so the exit code decides first:
        # `lsblk: not a block device` would otherwise read as a mountpoint.
        listed = self.runner.run(
            ["lsblk", "--noheadings", "--output", "MOUNTPOINT", disk], check=False
        )
        if listed.returncode != 0:
            return False
        if any(line.strip() for line in listed.stdout.splitlines()):
            return True
        swap = self.runner.run(["swapon", "--noheadings", "--show=NAME"], check=False)
        if swap.returncode != 0:
            return False
        return any(line.strip().startswith(disk) for line in swap.stdout.splitlines())

    def save(self) -> None:
        """Written beside the target and renamed over it, so a run that dies
        mid-write leaves the previous cache rather than half of a new one."""
        self.work.mkdir(parents=True, exist_ok=True)
        cache = self.work / "devices.json"
        partial = cache.with_suffix(".json.part")
        partial.write_text(json.dumps({"paths": self.resolved}, indent=2, sort_keys=True))
        partial.replace(cache)

    def load(self) -> None:
        cache = self.work / "devices.json"
        if not cache.is_file():
            return
        try:
            raw = json.loads(cache.read_text())
        except json.JSONDecodeError:
            self.runner.log(f"{cache} is not readable JSON; starting with an empty device map")
            return
        self.resolved = {DeviceId(key): value for key, value in raw.get("paths", {}).items()}

    def _memory(self) -> int:
        if not MEMINFO.is_file():
            return 0
        for line in MEMINFO.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
        return 0
