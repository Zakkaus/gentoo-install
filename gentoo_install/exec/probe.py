"""The only module that reads the machine's state.

Everything else asks this. `preflight.py` in particular holds no `os.path` call
of its own, so its checks can be exercised against a described machine rather
than only against the one running the tests.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar, Final, Iterable, Mapping

from ..errors import CommandFailed, DeviceNotFound
from ..model.config import InstallConfig
from ..model.device import DeviceGraph, DeviceId, Existing, Extent, PartitionTable
from ..model.validate import ProbedProfile, parse_profile_list
from .runner import Runner

EFI_MARKER: Final[Path] = Path("/sys/firmware/efi")

#: Published since Linux 4.4. Absent on an older kernel, which is why an
#: unreadable value is not treated as a failure.
EFI_WIDTH: Final[Path] = EFI_MARKER / "fw_platform_size"

#: Where the firmware's boot entries are. `/sys/firmware/efi` existing says
#: the kernel booted through EFI; it does not say the variables can be
#: written, and `efibootmgr --create` is what ZFSBootMenu's install runs.
EFI_VARIABLES: Final[Path] = EFI_MARKER / "efivars"


def _efi_variables() -> bool:
    """Whether efivarfs is mounted and populated.

    Non-empty, not merely present: the mount point exists as an empty
    directory when efivarfs was never mounted, and the install then reaches
    `efibootmgr` and fails with the disks already written.
    """
    try:
        return any(EFI_VARIABLES.iterdir())
    except OSError:
        return False


#: What a machine gives itself when DHCP finds no server. `ip` reports it as
#: `scope global`, so counting it as an IPv4 network told an IPv6-only guest
#: it had one: 169.254.0.0/16 reaches the link and nothing beyond it.
_LINK_LOCAL_V4: Final[str] = "169.254."
_ULA_V6: Final[ipaddress.IPv6Network] = ipaddress.IPv6Network("fc00::/7")


def _link_local_v4(line: str) -> bool:
    after = line.split(" inet ", 1)[1] if " inet " in line else ""
    return after.strip().startswith(_LINK_LOCAL_V4)


def _ipv6_address(line: str) -> ipaddress.IPv6Address | None:
    fields = line.split()
    try:
        address = ipaddress.ip_interface(fields[fields.index("inet6") + 1]).ip
    except (ValueError, IndexError):
        return None
    return address if isinstance(address, ipaddress.IPv6Address) else None


def _display_block_size(size: int) -> str:
    for unit, suffix in (
        (1024**5, "P"),
        (1024**4, "T"),
        (1024**3, "G"),
        (1024**2, "M"),
        (1024, "K"),
    ):
        if size >= unit:
            value = size / unit
            return f"{value:.1f}".removesuffix(".0") + suffix
    return f"{size}B"


@dataclass(frozen=True)
class ProbedDisk:
    """Facts read for a whole disk offered as an install target."""

    kernel_path: str
    selector: str
    size: str
    model: str


@dataclass(frozen=True)
class ProbedPartition:
    """Facts read for a device nested below a whole disk."""

    kernel_path: str
    size_bytes: int
    filesystem: str
    device_type: str


def _lsblk_children(value: object) -> tuple[ProbedPartition, ...]:
    if not isinstance(value, Mapping):
        return ()
    children = value.get("children")
    if not isinstance(children, list):
        return ()
    found: list[ProbedPartition] = []
    for child in children:
        if not isinstance(child, Mapping):
            continue
        name, size = child.get("name"), child.get("size")
        filesystem, device_type = child.get("fstype"), child.get("type")
        if isinstance(name, str) and isinstance(size, int) and not isinstance(size, bool):
            found.append(
                ProbedPartition(
                    kernel_path=name,
                    size_bytes=size,
                    filesystem=filesystem if isinstance(filesystem, str) else "",
                    device_type=device_type if isinstance(device_type, str) else "",
                )
            )
        found.extend(_lsblk_children(child))
    return tuple(found)


def _under(path: str, root: str) -> bool:
    """Whether `path` is `root` or sits inside it."""
    return path == root or path.startswith(f"{root.rstrip('/')}/")


def _partition_of(node: str, disk: str) -> bool:
    """Whether `node` is `disk` or one of its partitions.

    A prefix test alone reads `/dev/sdaa` as a partition of `/dev/sda`, so the
    remainder has to be a partition suffix: a number, or `p` and a number for
    the nvme and mmc spelling.
    """
    if node == disk:
        return True
    rest = node[len(disk):] if node.startswith(disk) else ""
    return rest.removeprefix("p").isdigit() if rest else False


def _efi_bits() -> int:
    try:
        return int(EFI_WIDTH.read_text().strip())
    except (OSError, ValueError):
        return 0
#: Where both install media keep the release engineering public key.
RELEASE_KEY: Final[Path] = Path("/usr/share/openpgp-keys/gentoo-release.asc")
MEMINFO: Final[Path] = Path("/proc/meminfo")


def profiles_from_eselect(output: str) -> tuple[ProbedProfile, ...]:
    """Parse profile output already read from the machine that owns the tree."""
    return parse_profile_list(output)


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
    #: Whether the firmware's variables can be read and written.
    efi_variables: bool = False
    #: The width of the EFI firmware, from `fw_platform_size`. Zero when the
    #: machine did not boot by EFI or the kernel is too old to publish it: 32
    #: is the case that matters, because amd64 EFI executables do not load on
    #: it and the install would finish and never boot.
    efi_bits: int = 0


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

#: Directories under zoneinfo that are not regions: legacy aliases, and the
#: right and posix trees, which repeat every zone with another leap-second
#: table.
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
        """What each command answers to `--version`, first line only.

        Empty for a command that is on PATH and answered nothing or exited
        nonzero. The exit code was discarded, so an empty reply reached
        `_busybox_problems` and `splitlines()[0]` raised `IndexError` instead
        of naming a command whose implementation could not be checked.
        """
        found: dict[str, str] = {}
        for command in wanted:
            if shutil.which(command) is None:
                continue
            said = self.runner.run([command, "--version"], check=False)
            lines = said.stdout.splitlines() if said.returncode == 0 else []
            found[command] = lines[0] if lines else ""
        return found

    def machine(
        self, wanted: frozenset[str] = frozenset(), judged: Iterable[str] = ()
    ) -> Machine:
        """`judged` names the commands whose implementation matters; the table
        of what each one has to be lives in `preflight.py`."""
        return Machine(
            architecture=platform.machine(),
            uefi=EFI_MARKER.is_dir(),
            efi_bits=_efi_bits(),
            efi_variables=_efi_variables(),
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
        return self.disk_of_path(self.path_of(device))

    def disk_of_path(self, path: str) -> str:
        """The same answer for a path already in hand.

        `resolve()` deliberately caches nothing, so a reused partition has no
        entry for `path_of` to find and asking by id raised `DeviceNotFound`
        at bootloader installation.
        """
        result = self.runner.run(["lsblk", "--noheadings", "--output", "PKNAME", path])
        parent = result.stdout.strip().splitlines()
        return f"/dev/{parent[0].strip()}" if parent and parent[0].strip() else path

    def partition_number_of_path(self, path: str) -> int:
        """Which entry in its table this partition is.

        Asked of the machine because a reused partition carries no index in the
        configuration: the operator named a device, not a number.
        """
        result = self.runner.run(["lsblk", "--noheadings", "--output", "PARTN", path])
        said = result.stdout.strip().splitlines()
        number = said[0].strip() if said else ""
        if not number.isdigit():
            raise DeviceNotFound(f"lsblk gave no partition number for {path}")
        return int(number)

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

    def probed_disks(self) -> tuple[ProbedDisk, ...]:
        """Facts for the whole disks that can be install targets.

        Named by `/dev/disk/by-id/` where one exists: a kernel name is assigned
        at probe time and a configuration saved with one installs elsewhere on
        the next boot.
        """
        listed = self.runner.run(
            ["lsblk", "--noheadings", "--nodeps", "--paths", "--output", "NAME,SIZE,TYPE,MODEL"],
            check=False,
        )
        found: list[ProbedDisk] = []
        for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
            fields = line.split(maxsplit=3)
            if len(fields) < 3 or fields[2] != "disk":
                continue
            path, size = fields[0], fields[1]
            if path.startswith(self.NOT_A_TARGET):
                continue
            model = fields[3] if len(fields) > 3 else ""
            found.append(
                ProbedDisk(
                    kernel_path=path,
                    selector=self._stable_name(path),
                    size=size,
                    model=model,
                )
            )
        return tuple(found)

    def disks(self) -> tuple[tuple[str, str], ...]:
        """Display rows retained for callers that have not moved to facts."""
        return tuple(
            (disk.selector, f"{disk.size} {disk.model}".strip())
            for disk in self.probed_disks()
        )

    #: Where udev keeps the names that survive the kernel renumbering disks.
    BY_ID: ClassVar[Path] = Path("/dev/disk/by-id")

    def address_families(self) -> tuple[bool, bool]:
        """Whether this machine has a routable IPv4 and a routable IPv6.

        A global address, not a loopback or a link-local one: a host with only
        `fe80::` reaches nothing, and a host with only a ULA reaches nothing
        outside its own NAT64. Read so the menu can refuse a mirror this
        machine cannot fetch from rather than let the operator discover it
        when the stage3 does not arrive.
        """
        listed = self.runner.run(
            ["ip", "-oneline", "address", "show", "scope", "global"], check=False
        )
        if listed.returncode != 0:
            # Unreadable is not the same as absent, and a caller that refuses
            # a mirror on this must not act on a command that did not run.
            return True, True
        # Reported as found. A guest whose interface is still coming up has
        # neither, and answering `both` there was a probe that could not be
        # wrong: an IPv6-only machine looked exactly like a dual-stack one.
        has4 = any(
            " inet " in line and not _link_local_v4(line) for line in listed.stdout.splitlines()
        )
        addresses6 = (_ipv6_address(line) for line in listed.stdout.splitlines())
        has6 = any(address is not None and address not in _ULA_V6 for address in addresses6)
        return has4, has6

    def mdraid_metadata(self, selector: str) -> str:
        """The metadata version an array already on the machine carries.

        Empty when the device is not an array. The model cannot read a
        superblock, so a reused RAID1 esp met no compatibility rule at all
        until this was injected before validation, although firmware cannot
        read a member whose superblock sits at the start.
        """
        try:
            said = self.runner.run(["mdadm", "--detail", "--export", selector], check=False)
        except (CommandFailed, OSError):
            # A medium without mdadm says nothing about a superblock, and that
            # is not a reason to stop: the array rule simply does not fire.
            return ""
        if said.returncode != 0:
            return ""
        for line in said.stdout.splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "MD_METADATA" and value.strip():
                return value.strip()
        return ""

    def free_extents(self, disk: str) -> tuple[Extent, ...]:
        """Where the disk has no partition now, in bytes.

        `parted --machine ... unit B print free` prints one line per extent,
        occupied and free alike, and marks the free ones in its filesystem
        field. The number field repeats on those lines and means nothing, so
        the marker is what they are read by.
        """
        listed = self.runner.run(
            ["parted", "--machine", "--script", disk, "unit", "B", "print", "free"],
            check=False,
        )
        if listed.returncode != 0:
            raise DeviceNotFound(f"parted could not read the table of {disk}")
        found: list[Extent] = []
        for line in listed.stdout.splitlines():
            fields = line.strip().rstrip(";").split(":")
            if len(fields) < 5 or fields[4] != "free":
                continue
            try:
                start, end = int(fields[1].rstrip("B")), int(fields[2].rstrip("B"))
            except ValueError:
                continue
            found.append(Extent(start=start, end=end))
        return tuple(found)

    def passphrase(self, source: str) -> tuple[str, str]:
        """What a passphrase file holds, and why it could not be read.

        Exactly one of the two is empty. Asked of the probe rather than opened
        where it is judged: preflight is meant to be reproducible from its
        declared inputs, and reading a host path directly made the same
        configuration and the same probe answer differently on two machines.
        """
        try:
            return Path(source).read_text().strip("\n"), ""
        except OSError as error:
            return "", str(error)

    def names_for(self, selector: str) -> tuple[str, ...]:
        """Every spelling of one device the operator might reasonably type.

        The selector, its last component, and the kernel name it resolves to
        with its own last component. The installer renames a disk to its
        `/dev/disk/by-id/` form, and an operator looking at `lsblk` types
        `/dev/sda`, which the erase confirmation refused.
        """
        found = {selector, selector.rsplit("/", 1)[-1]}
        try:
            real = Path(selector).resolve()
        except OSError:
            return tuple(sorted(found))
        found |= {str(real), real.name}
        return tuple(sorted(found))

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

    #: What the running system's `/etc/localtime` points at. A live medium
    #: sets it from the firmware clock and its own default, so it is the one
    #: guess about where the machine is that costs nothing to make.
    LOCALTIME: ClassVar[Path] = Path("/etc/localtime")

    def timezone_here(self) -> str:
        """The zone the installing system is on, or empty when it is not a link.

        Read from the symlink and not from `timedatectl`: a live medium need
        not carry systemd, and the link is what every distribution writes.
        """
        try:
            target = self.LOCALTIME.resolve()
        except OSError:
            return ""
        parts = target.parts
        if "zoneinfo" not in parts:
            return ""
        named = "/".join(parts[parts.index("zoneinfo") + 1 :])
        return named if named in self.timezones() else ""

    def timezones(self) -> tuple[str, ...]:
        """Every zone the installed system will know, as `Area/City`.

        Read from this machine's tree when it has one, because that is the
        freshest list. A medium without `/usr/share/zoneinfo` falls back to
        the copy this installer carries: the choice belongs to the target,
        which gets `sys-libs/timezone-data`, and a live medium that shipped
        no zone data left the menu offering `UTC` and nothing else.
        """
        root = Path("/usr/share/zoneinfo")
        if not root.is_dir():
            return _carried_timezones()
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
        # An empty tree is the case that produced the report: the directory
        # exists, both tables are missing and there is nothing under it, so
        # every branch above answered nothing and the menu offered `UTC` alone.
        return ("UTC", *found) if found else _carried_timezones()

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

    #: What the kernel side of ZFS shows up as once it is loaded. Either is
    #: enough: a module built into the kernel has no directory under /sys/module.
    ZFS_LOADED: ClassVar[tuple[Path, ...]] = (Path("/dev/zfs"), Path("/sys/module/zfs"))

    def zfs_support(self) -> str:
        """Why this live system cannot make a pool, or empty when it can.

        Asked at startup because the installer runs off whatever medium is to
        hand — Alpine, Debian, a Fedora live image — and most of them carry no
        ZFS at all. Finding that out at `zpool create`, after the disks are
        partitioned, is the alternative.
        """
        missing = [name for name in ("zpool", "zfs") if shutil.which(name) is None]
        if missing:
            return f"this live system has no {' or '.join(missing)}"
        if any(path.exists() for path in self.ZFS_LOADED):
            return ""
        loaded = self.runner.run(["modprobe", "zfs"], check=False)
        if loaded.returncode != 0 or not any(path.exists() for path in self.ZFS_LOADED):
            return "this live system cannot load the zfs kernel module"
        return ""

    def cores(self) -> int:
        return os.cpu_count() or 1

    def probed_partitions(self, disk: str) -> tuple[ProbedPartition, ...]:
        """Facts for the devices nested below a disk.

        Shown before the table is edited: an operator about to erase a disk has
        to see what is on it, and sizes are guesswork without the total.
        """
        listed = self.runner.run(
            [
                "lsblk",
                "--json",
                "--bytes",
                "--paths",
                "--output",
                "NAME,SIZE,FSTYPE,TYPE",
                disk,
            ],
            check=False,
        )
        if listed.returncode != 0:
            return ()
        try:
            document: object = json.loads(listed.stdout)
        except json.JSONDecodeError:
            return ()
        if not isinstance(document, Mapping):
            return ()
        roots = document.get("blockdevices")
        if not isinstance(roots, list):
            return ()
        return tuple(row for root in roots for row in _lsblk_children(root))

    def partitions(self, disk: str) -> tuple[tuple[str, str, str], ...]:
        """Display rows retained for callers that have not moved to facts."""
        return tuple(
            (
                partition.kernel_path,
                _display_block_size(partition.size_bytes),
                partition.filesystem,
            )
            for partition in self.probed_partitions(disk)
        )

    def disk_size(self, disk: str) -> str:
        listed = self.runner.run(
            ["lsblk", "--noheadings", "--nodeps", "--output", "SIZE", disk], check=False
        )
        return listed.stdout.strip() if listed.returncode == 0 else ""

    #: Where `sys-apps/kbd` keeps its keymaps. Debian and Arch put the same
    #: tree under `kbd/`, and only the PC families matter on amd64.
    #: Where each distribution puts the console keymaps. Gentoo, Arch and
    #: openSUSE use the first two; Fedora moved the tree under /usr/lib and
    #: dropped the architecture level; Alpine's kbd-legacy adds one.
    KEYMAPS: ClassVar[tuple[Path, ...]] = (
        Path("/usr/share/keymaps/i386"),
        Path("/usr/share/kbd/keymaps/i386"),
        Path("/usr/share/keymaps/legacy/i386"),
        Path("/usr/lib/kbd/keymaps/legacy/i386"),
        Path("/usr/lib/kbd/keymaps/xkb"),
    )

    #: Debian's console-data names them `.kmap.gz`, everyone else `.map.gz`.
    KEYMAP_SUFFIXES: ClassVar[tuple[str, ...]] = (".map.gz", ".kmap.gz")

    def keymaps(self) -> tuple[tuple[str, str], ...]:
        """Every console keymap this machine ships, as (family, name).

        Read rather than listed here: the set differs by distribution and by
        `kbd` version, and a name the target has no file for loads nothing.
        """
        found: dict[str, str] = {}
        for base in self.KEYMAPS:
            try:
                families = sorted(one for one in base.iterdir() if one.is_dir())
            except OSError:
                continue
            for family in families:
                if family.name == "include":
                    continue
                for suffix in self.KEYMAP_SUFFIXES:
                    for keymap in sorted(family.glob(f"*{suffix}")):
                        found.setdefault(keymap.name[: -len(suffix)], family.name)
            # Fedora's xkb tree is flat, with no family directory at all.
            for suffix in self.KEYMAP_SUFFIXES:
                for keymap in sorted(base.glob(f"*{suffix}")):
                    found.setdefault(keymap.name[: -len(suffix)], base.name)
        return tuple((family, name) for name, family in sorted(found.items()))

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

    def partition_sizes(self, disk: str) -> dict[int, int]:
        """Every partition on the disk now, by number, in bytes.

        A table the operator edits rather than rewrites keeps partitions the
        configuration never names, and their space is claimed just as much as
        a new partition's.
        """
        listed = self.runner.run(
            ["lsblk", "--bytes", "--noheadings", "--output", "PARTN,SIZE", disk], check=False
        )
        if listed.returncode != 0:
            raise DeviceNotFound(f"lsblk could not read the partitions of {disk}")
        found: dict[int, int] = {}
        for line in listed.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
                found[int(fields[0])] = int(fields[1])
        return found

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
        # Nothing can be mounted from something that is not a disk, and the
        # tests point this at /dev/null. Every other failure below answers yes.
        if not Path(disk).is_block_device():
            return False
        # The runner merges stderr into stdout, so the exit code decides first:
        # `lsblk: not a block device` would otherwise read as a mountpoint. A
        # command that fails answers yes: this guard exists to refuse a
        # destructive operation, and a machine it cannot read is one it cannot
        # clear. busybox `swapon` has no `--show`, and answering no there let a
        # run repartition a disk holding an active swap.
        # `MOUNTPOINTS`, not `MOUNTPOINT`: the singular column shows one of
        # them, so a filesystem mounted both under the install target and
        # somewhere else reported only the target and read as free.
        listed = self.runner.run(
            ["lsblk", "--noheadings", "--output", "MOUNTPOINTS", disk], check=False
        )
        if listed.returncode != 0:
            return True
        elsewhere = [
            where
            for where in (line.strip() for line in listed.stdout.splitlines())
            if where and not (ignoring and _under(where, ignoring))
        ]
        if elsewhere:
            return True
        if self._in_an_imported_pool(disk, ignoring):
            return True
        swap = self.runner.run(["swapon", "--noheadings", "--show=NAME"], check=False)
        if swap.returncode != 0:
            return True
        return any(_partition_of(line.strip(), disk) for line in swap.stdout.splitlines())

    def _in_an_imported_pool(self, disk: str, ignoring: str = "") -> bool:
        """Whether any partition of `disk` is a vdev of an imported ZFS pool
        that is not this install's own.

        A `zfs_member` partition carries no block-device mountpoint even while
        its datasets provide `/` and `/home`, so every mountpoint column reads
        blank and the disk looks free.

        `ignoring` is the install target. A run that stopped halfway leaves its
        own pool imported with the target as its altroot, and reading that as
        somebody else's disk made the next attempt impossible: `zpool list`
        answered `rpool ... ONLINE /mnt/gentoo` and preflight refused the disk
        the operator was installing onto.
        """
        listed = self.runner.run(["zpool", "list", "-v", "-H", "-P"], check=False)
        if listed.returncode != 0:
            # No pools and no `zpool` are the same answer here: nothing this
            # command can tell us. `zpool` missing is the common case on a
            # medium without ZFS, and it is not evidence that a disk is busy.
            return False
        whole = str(Path(disk).resolve())
        ours = False
        for line in listed.stdout.splitlines():
            if not line.startswith(("\t", " ")):
                # A pool line. Its last field is the altroot, and `-` when the
                # pool was imported without one.
                fields = line.split()
                where = fields[-1] if fields else "-"
                ours = bool(ignoring) and where != "-" and _under(where, ignoring)
                continue
            if ours:
                continue
            for field in line.split():
                if not field.startswith("/"):
                    continue
                vdev = Path(field)
                real = str(vdev.resolve() if vdev.exists() else vdev)
                if _partition_of(real, whole):
                    return True
        return False

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


#: The zone names this installer carries, for a medium whose own tree is
#: absent or empty. Generated from `zone1970.tab`; the list moves once or
#: twice a year and the target's `sys-libs/timezone-data` is what has to
#: accept the name, not the medium.
_CARRIED: Final[Path] = Path(__file__).resolve().parent.parent / "data" / "timezones.txt"


def _carried_timezones() -> tuple[str, ...]:
    try:
        said = _CARRIED.read_text(encoding="utf-8")
    except OSError:
        return ("UTC",)
    return ("UTC", *(line for line in said.split() if "/" in line))


def with_probed_facts(config: InstallConfig, probe: Probe) -> InstallConfig:
    """Fill in what only the machine can say, before anything validates it.

    The model stays parseable without the hardware, so a reused device carries
    a selector and nothing else. Its mdraid metadata is read here: a reused
    RAID1 esp with a superblock at the start met no compatibility rule at all
    without it, and firmware cannot read such a member.
    """
    graph = config.disk.graph
    reused = [one for one in graph.of_type(Existing) if one.mdraid_metadata is None]
    known = {one.id: probe.mdraid_metadata(one.selector) for one in reused}
    free = _free_extents(graph, probe)
    if not known and not free:
        return config
    nodes = [
        replace(one, mdraid_metadata=known[one.id])
        if isinstance(one, Existing) and one.id in known
        else replace(one, free_extents=free[one.id])
        if isinstance(one, PartitionTable) and one.id in free
        else one
        for one in graph.nodes.values()
    ]
    return replace(config, disk=replace(config.disk, graph=DeviceGraph.build(nodes)))


def _free_extents(graph: DeviceGraph, probe: Probe) -> dict[DeviceId, tuple[Extent, ...]]:
    """Where each edited table has room, read from the disk it is on.

    Only tables the plan does not write from scratch: a new table's whole disk
    is free, and `sgdisk --new=N:0` finds the room itself on GPT. An MBR
    partition is given an explicit start, computed from the model's own
    partitions alone, so an added one landed at 1MiB on top of a retained one.
    """
    found: dict[DeviceId, tuple[Extent, ...]] = {}
    for table in graph.of_type(PartitionTable):
        if table.create or table.free_extents:
            continue
        disk = graph.nodes.get(table.disk)
        if not isinstance(disk, Existing):
            continue
        try:
            found[table.id] = probe.free_extents(probe.resolve(disk.id, disk.selector))
        except DeviceNotFound:
            continue
    return found
