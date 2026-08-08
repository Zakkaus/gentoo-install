"""The only module that reads the machine's state.

Everything else asks this. `preflight.py` in particular holds no `os.path` call
of its own, so its checks can be exercised against a described machine rather
than only against the one running the tests.
"""

from __future__ import annotations

import json
import os
import platform
import re
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


#: Directories under zoneinfo that are not regions: legacy aliases, and the
#: right and posix trees, which repeat every zone with another leap-second
#: table.
#: What the kernel calls a CPU feature, and what portage calls it. Only the
#: ones `CPU_FLAGS_X86` defines: a name portage does not know is a build
#: failure, not an optimisation. A value differing from its key is a rename and
#: nothing else: mapping one feature onto another wrote `avx2` for a Piledriver
#: that has `bmi1` and no AVX2, and every package built for it died on SIGILL.
CPU_FLAGS: Final[dict[str, str]] = {
    "aes": "aes", "avx": "avx", "avx2": "avx2", "avx512f": "avx512f",
    "avx512bw": "avx512bw", "avx512cd": "avx512cd", "avx512dq": "avx512dq",
    "avx512vl": "avx512vl", "avx512vbmi": "avx512vbmi", "avx512vnni": "avx512vnni",
    "bmi1": "bmi1", "bmi2": "bmi2", "f16c": "f16c", "fma": "fma3",
    "mmx": "mmx", "mmxext": "mmxext",
    "pclmulqdq": "pclmul", "popcnt": "popcnt", "rdrand": "rdrand", "sha_ni": "sha",
    "sse": "sse", "sse2": "sse2", "pni": "sse3", "sse4_1": "sse4_1",
    "sse4_2": "sse4_2", "sse4a": "sse4a", "ssse3": "ssse3",
    "vaes": "vaes", "vpclmulqdq": "vpclmulqdq",
}

_NOT_A_REGION: Final[frozenset[str]] = frozenset({"right", "posix", "SystemV", "Etc"})


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

    def machine(
        self, wanted: frozenset[str] = frozenset(), judged: Iterable[str] = ()
    ) -> Machine:
        """`judged` names the commands whose implementation matters; the table
        of what each one has to be lives in `preflight.py`."""
        return Machine(
            architecture=platform.machine(),
            uefi=EFI_MARKER.is_dir(),
            root=os.geteuid() == 0,
            memory_bytes=self._memory(),
            commands=frozenset(name for name in wanted if shutil.which(name) is not None),
            release_key=RELEASE_KEY.is_file(),
            versions=self.versions(judged),
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

    def filesystem_type_of(self, path: str) -> str:
        """What is on the device now, as `blkid` names it. Empty for nothing.

        `--probe` for the same reason `uuid_of` uses it: the cache answers with
        whatever was there before this run.
        """
        self.runner.run(["udevadm", "settle"], check=False)
        result = self.runner.run(
            ["blkid", "--probe", "--match-tag", "TYPE", "--output", "value", path], check=False
        )
        # The runner merges stderr into stdout, so a failure has to be read from
        # the exit code: `not a block device` on stdout would read as a type.
        return result.stdout.strip() if result.returncode == 0 else ""

    def uuid_of(self, path: str, device: DeviceId) -> str:
        """Read a formatted device's UUID.

        `--probe` reads the device rather than blkid's cache, which still holds
        the previous filesystem's UUID after a reformat; that UUID would go
        into fstab and the kernel command line.
        """
        self.runner.run(["udevadm", "settle"], check=False)
        result = self.runner.run(
            ["blkid", "--probe", "--match-tag", "UUID", "--output", "value", path], check=False
        )
        # The exit code before the text: the runner merges stderr into stdout,
        # so `not a block device` would go into fstab as a UUID.
        uuid = result.stdout.strip() if result.returncode == 0 else ""
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
        for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
            fields = line.split(maxsplit=3)
            if len(fields) < 3 or fields[2] != "disk":
                continue
            path, size = fields[0], fields[1]
            if path.startswith(self.NOT_A_TARGET):
                continue
            model = fields[3] if len(fields) > 3 else ""
            found.append((self._stable_name(path), f"{size} {model}".strip()))
        return tuple(found)

    #: Where udev keeps the names that survive the kernel renumbering disks.
    BY_ID: ClassVar[Path] = Path("/dev/disk/by-id")

    def _stable_name(self, path: str) -> str:
        """Read rather than shelled out to `find -lname`: that predicate is
        GNU's, and busybox answers `unrecognized: -lname` on Alpine, which left
        the configuration holding a `/dev/sda` that names a different disk once
        another one is plugged in."""
        wanted = path.rsplit("/", 1)[-1]
        names: list[str] = []
        try:
            for entry in self.BY_ID.iterdir():
                if not entry.is_symlink():
                    continue
                if Path(os.readlink(entry)).name == wanted:
                    names.append(str(entry))
        except OSError:
            return path
        names.sort()
        # Prefer a by-id name that is not a wwn: a wwn is stable but says
        # nothing a person can recognise.
        for name in names:
            if "/wwn-" not in name:
                return name
        return names[0] if names else path

    def timezones(self) -> tuple[str, ...]:
        """Every zone this machine knows, as `Area/City`.

        Read from the tree rather than a list in the source: a hand-picked
        selection is not a timezone chooser, and the names change.
        """
        root = Path("/usr/share/zoneinfo")
        if not root.is_dir():
            return ()
        # zone1970.tab lists the canonical zones, one per line, and reading it
        # is both faster and closer to what the operator expects than walking a
        # tree full of aliases.
        for table in ("zone1970.tab", "zone.tab"):
            listed = self._zone_table(root / table)
            if listed:
                return ("UTC", *listed)
        found: list[str] = []
        for area in sorted(root.iterdir()):
            # Only the region directories: the top level also holds files like
            # `UTC`, symlinks like `Japan`, and data like `posixrules`.
            if not area.is_dir() or area.name in _NOT_A_REGION:
                continue
            for city in sorted(area.rglob("*")):
                if city.is_file():
                    found.append(str(city.relative_to(root)))
        return ("UTC", *found)

    def cpu_flags(self) -> tuple[str, ...]:
        """`CPU_FLAGS_X86` for this machine, from /proc/cpuinfo.

        Read here rather than run through `cpuid2cpuflags`: that is
        app-portage/cpuid2cpuflags, which no install medium carries, and the
        flag names it prints are a fixed mapping of the ones the kernel already
        reports.
        """
        try:
            text = Path("/proc/cpuinfo").read_text()
        except OSError:
            return ()
        reported: set[str] = set()
        for line in text.splitlines():
            name, _, value = line.partition(":")
            if name.strip() == "flags":
                reported.update(value.split())
                break
        found = [portage for kernel, portage in CPU_FLAGS.items() if kernel in reported]
        return tuple(sorted(set(found)))

    def supports_v3(self) -> bool:
        """Whether this CPU runs `x86-64-v3` binaries.

        `ld.so --help` lists the subarchitectures it would search, which is the
        loader's own answer rather than a guess from a flag list, and it is
        what decides whether that binary host is worth offering.
        """
        for loader in ("/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-x86-64.so.2"):
            if not Path(loader).exists():
                continue
            listed = self.runner.run([loader, "--help"], check=False)
            if listed.returncode != 0:
                continue
            return any(
                line.strip().startswith("x86-64-v3 (supported")
                for line in listed.stdout.splitlines()
            )
        return False

    def cores(self) -> int:
        return os.cpu_count() or 1

    def partitions(self, disk: str) -> tuple[tuple[str, str, str], ...]:
        """What is on a disk now, as name, size and filesystem.

        Shown before the table is edited: an operator about to erase a disk has
        to see what is on it, and sizes are guesswork without the total.
        """
        listed = self.runner.run(
            ["lsblk", "--noheadings", "--paths", "--output", "NAME,SIZE,FSTYPE", disk],
            check=False,
        )
        if listed.returncode != 0:
            return ()
        found: list[tuple[str, str, str]] = []
        for line in listed.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2:
                found.append((fields[0].lstrip("`|-\u2500\u2514\u251c "), fields[1], fields[2] if len(fields) > 2 else ""))
        return tuple(found)

    def disk_size(self, disk: str) -> str:
        listed = self.runner.run(
            ["lsblk", "--noheadings", "--nodeps", "--output", "SIZE", disk], check=False
        )
        return listed.stdout.strip() if listed.returncode == 0 else ""

    def disk_bytes(self, disk: str) -> int:
        """Capacity in bytes, or 0 when the device cannot be read.

        `disk_size` answers `128G`, which is rounded and cannot be compared
        against a partition table whose sizes are exact.
        """
        listed = self.runner.run(
            ["lsblk", "--bytes", "--noheadings", "--nodeps", "--output", "SIZE", disk],
            check=False,
        )
        if listed.returncode != 0:
            return 0
        text = listed.stdout.strip().splitlines()
        return int(text[0]) if text and text[0].strip().isdigit() else 0

    def _zone_table(self, path: Path) -> tuple[str, ...]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ()
        found: list[str] = []
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 3 and "/" in fields[2] and fields[2] not in found:
                found.append(fields[2])
        return tuple(sorted(found))

    def mounted(self, disk: str, ignoring: str = "") -> bool:
        """Whether a disk, or any partition on it, is in use.

        `findmnt --mountpoint` answers about a directory, so asking it about
        `/dev/sda` always said no and the guard against repartitioning a disk in
        use could never fire.

        `ignoring` is the install target: a run that stopped halfway leaves the
        disk mounted there, and refusing to start again over the previous
        attempt's own leftovers is what makes a failed install unrepeatable.
        """
        # The runner merges stderr into stdout, so the exit code decides first:
        # `lsblk: not a block device` would otherwise read as a mountpoint.
        listed = self.runner.run(
            ["lsblk", "--noheadings", "--output", "MOUNTPOINT", disk], check=False
        )
        if listed.returncode != 0:
            return False
        elsewhere = [
            where
            for where in (line.strip() for line in listed.stdout.splitlines())
            if where and not (ignoring and (where == ignoring or where.startswith(f"{ignoring}/")))
        ]
        if elsewhere:
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
