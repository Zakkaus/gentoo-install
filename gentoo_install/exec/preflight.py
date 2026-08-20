# SPDX-License-Identifier: GPL-2.0-or-later
"""Checks that run before anything is written.

Facts come from `probe.py`; nothing here reads the system itself, so the checks
can be run against a described machine. Every failure is collected, because a
user fixing one condition per run learns about the next one a run later.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Final, Iterable, Mapping

from ..errors import DeviceNotFound, PreflightFailed
from ..model import compat
from ..model.config import DiskMode, Firmware, InstallConfig
from ..model.device import (
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    Swap,
    FilesystemType,
    Luks,
    MdRaid,
    Partition,
    PartitionTable,
    TableType,
    VolumeGroup,
    ZfsPool,
)
from ..model.size import DEFAULT_ALIGNMENT, SectorSize, Size
from ..model.validate import root_size_problems
from ..plan.disk import MKFS
from ..plan.operations import Operation
from ..plan import dd
from ..plan.portage import InstallStage3, MountChrootFilesystems, SyncRepository
from .probe import RELEASE_KEY, Machine, Probe
from .runner import write_file

# These are used while preflight inspects disks, before any operation runs.
PREFLIGHT_ONLY: Final[tuple[str, ...]] = ("lsblk", "swapon")

#: The stage3 operation's direct commands and the helpers they launch.
STAGE3_COMMANDS: Final[tuple[str, ...]] = ("tar", "xz", "gpg", "gpg-agent")

#: Commands every install needs, whatever the layout is.
ALWAYS: Final[tuple[str, ...]] = (
    "tar", "gpg", "mount", "umount", "findmnt", "lsblk", "blkid", "chroot", "udevadm", "swapon",
    # Reached through a context method rather than an operation's own list, so
    # the plan runs them on every configuration: `install` mounts the chroot's
    # filesystems, `mkdir` makes every mount point, `sleep` spaces a retry.
    "install", "mkdir", "sleep",
)

#: Commands reached through context methods or helper subprocesses.
BY_OPERATION: Final[dict[type[Operation], tuple[str, ...]]] = {
    InstallStage3: STAGE3_COMMANDS,
    MountChrootFilesystems: ("install",),
    SyncRepository: ("sleep",),
}

#: What the menu needs and a configuration file does not: a file carries
#: `password_hash` already, and the menu computes one with `openssl passwd -6`.
MENU_ONLY: Final[tuple[str, ...]] = ("openssl",)

#: What `Probe.storage_layout` runs to read the machine it is offered to
#: replace. Alpine's busybox has none of them, and without them every field
#: comes back empty, so the conversion is refused for a reason that names the
#: layout rather than the missing package.
LAYOUT_COMMANDS: Final[tuple[str, ...]] = ("findmnt", "lsblk", "blkid")

#: What each part of a layout adds. Derived from the graph, never a second list.
BY_FEATURE: Final[dict[str, tuple[str, ...]]] = {
    # `partprobe` is absent here on purpose: it comes from parted, and
    # `RereadPartitionTable` falls back to `blockdev --rereadpt` without it.
    "gpt": ("sgdisk", "blockdev", "wipefs"),
    # `blockdev` as well: the reread falls back to it, and an MBR layout
    # reaches the same operation a GPT one does.
    "mbr": ("parted", "partprobe", "wipefs", "blockdev"),
    # `dmsetup` as well: `OpenLuks` lists the mapper's own devices before it
    # opens one, and a medium without it stops with the disks partitioned.
    "luks": ("cryptsetup", "dmsetup"),
    "mdraid": ("mdadm",),
    # The binaries the operations invoke, not the multicall name: a medium
    # carrying lvm without its symlinks passes on `lvm` and dies at `pvcreate`
    # with the disks already partitioned.
    "lvm": ("pvcreate", "vgcreate", "lvcreate", "vgchange"),
    "swap": ("mkswap", "swapoff"),
    # `hostid`, not `zgenhostid`: the host reads its own id and the target
    # writes it, so the tool that writes runs inside the chroot.
    "zfs": ("zpool", "zfs", "hostid"),
}

#: btrfs needs its own tool as well as its mkfs, to make the subvolumes.
EXTRA_FILESYSTEM_COMMANDS: Final[dict[FilesystemType, tuple[str, ...]]] = {
    FilesystemType.BTRFS: ("btrfs",),
}

#: Commands whose busybox applet satisfies `which` and then rejects the flags,
#: with what the output has to say and why the applet will not do.
GNU_ONLY: Final[dict[str, tuple[str, str]]] = {
    "tar": ("GNU tar", "stage3 needs the GNU one for xattrs and capabilities"),
    "mount": (
        "util-linux",
        "the chroot needs --rbind and --make-rslave, which the busybox applet "
        "has no option for at all",
    ),
    "blkid": (
        "util-linux",
        "the fstab UUIDs come from --probe --match-tag, and the busybox applet "
        "takes no options at all",
    ),
    "umount": (
        "util-linux",
        "releasing a half-finished run needs --recursive and --lazy, which the "
        "busybox applet does not have",
    ),
    "swapon": (
        "util-linux",
        "the check that refuses to repartition a disk in use reads --show, "
        "which the busybox applet does not have",
    ),
}


@dataclass(frozen=True)
class UnusableCommand:
    """A present command whose implementation cannot perform the install."""

    name: str
    reason: str


@dataclass(frozen=True)
class CommandAssessment:
    """Command presence and implementation facts derived for one caller."""

    missing: frozenset[str]
    unusable: tuple[UnusableCommand, ...]


def assess_commands(
    wanted: Iterable[str],
    present: AbstractSet[str],
    versions: Mapping[str, str],
) -> CommandAssessment:
    """Derive command usability from presence and version facts."""
    names = frozenset(wanted)
    missing = names - present
    unusable: list[UnusableCommand] = []
    for command, (required, reason) in GNU_ONLY.items():
        if command not in names or command in missing:
            continue
        version = versions.get(command)
        if version is None or required in version:
            continue
        if not version:
            problem = (
                f"{command} answered nothing to --version, so it cannot be shown "
                f"to be {required}; {reason}"
            )
        else:
            problem = f"{command} is not {required} ({version[:60]}); {reason}"
        unusable.append(UnusableCommand(name=command, reason=problem))
    return CommandAssessment(missing=missing, unusable=tuple(unusable))


#: `zpool create` refuses anything shorter, and it refuses it after the disk
#: has already been partitioned.
ZFS_PASSPHRASE_MINIMUM: Final[int] = 8

#: Below this, compiling in a tmpfs is what runs the machine out of memory.
TMPFS_MINIMUM: Final[int] = 8 * 1024**3


@dataclass(frozen=True)
class SecretStore:
    """Approved passphrases staged under the run's volatile work directory."""

    work: Path

    def path(self, device: DeviceId) -> Path:
        return self.work / "keys" / "approved" / str(device)

    def stage(self, device: DeviceId, passphrase: str) -> None:
        write_file(self.path(device), passphrase, 0o600)

    def cleanup(self) -> tuple[str, ...]:
        """Remove every staged passphrase, and answer the ones that stayed.

        Raising here replaces the failure that brought the run to the closing
        path — a preflight refusal, or the install's own error — with an
        `unlink` errno, and that is never the more useful of the two. The
        caller reports what stayed, because a passphrase left on disk is not
        something to drop quietly either.
        """
        keys = self.work / "keys" / "approved"
        if not keys.is_dir():
            return ()
        stayed: list[str] = []
        for path in keys.iterdir():
            if not path.is_file():
                continue
            try:
                path.unlink()
            except OSError as error:
                stayed.append(f"{path}: {error}")
        return tuple(stayed)


@dataclass(frozen=True)
class Report:
    fatal: tuple[str, ...]
    warnings: tuple[str, ...]
    secrets: SecretStore | None = None

    def raise_if_fatal(self) -> None:
        if self.fatal:
            # After the reasons, never instead of them: what the operator has
            # to read first is why this machine was refused.
            stayed = self.secrets.cleanup() if self.secrets is not None else ()
            raise PreflightFailed(
                "this machine cannot run the install:\n  "
                + "\n  ".join([*self.fatal, *(f"a staged passphrase stayed: {one}" for one in stayed)])
            )


def _configured_commands(config: InstallConfig) -> frozenset[str]:
    if config.disk.mode is DiskMode.DD:
        return frozenset((*PREFLIGHT_ONLY, *dd.required_commands(config.disk.source_format)))
    graph = config.disk.graph
    wanted = set(ALWAYS) | set(STAGE3_COMMANDS)
    if config.disk.mode is DiskMode.IMAGE:
        wanted |= {"test", "truncate", "losetup"}
    for table in graph.of_type(PartitionTable):
        wanted |= set(BY_FEATURE["gpt" if table.table is TableType.GPT else "mbr"])
    if graph.of_type(Luks):
        wanted |= set(BY_FEATURE["luks"])
    if graph.of_type(MdRaid):
        wanted |= set(BY_FEATURE["mdraid"])
    if graph.of_type(VolumeGroup):
        wanted |= set(BY_FEATURE["lvm"])
    if graph.of_type(Swap):
        wanted |= set(BY_FEATURE["swap"])
    if graph.of_type(ZfsPool):
        wanted |= set(BY_FEATURE["zfs"])
    for filesystem in graph.of_type(Filesystem):
        if not filesystem.create:
            # A reused filesystem is mounted and never made, so `mkfs` for it
            # is not needed; `blkid` is, and ALWAYS already has it.
            continue
        # Taken from the table the operations themselves use, so a filesystem
        # added there can never be missing here.
        wanted.add(MKFS[filesystem.kind].argv[0])
        wanted |= set(EXTRA_FILESYSTEM_COMMANDS.get(filesystem.kind, ()))
    return frozenset(wanted)


def required_commands(
    config: InstallConfig, operations: Iterable[Operation] | None = None
) -> frozenset[str]:
    """Executables preflight probes for this configuration and built plan.

    The configuration-only path remains for callers that have not built a
    plan, such as ``--missing-commands``. Installation passes its built plan.
    """
    if operations is None:
        return _configured_commands(config)
    wanted = set(PREFLIGHT_ONLY)
    for operation in operations:
        wanted.update(_operation_commands(operation))
    return frozenset(wanted)


def _operation_commands(operation: Operation) -> frozenset[str]:
    wanted = set(operation.required_host_commands())
    for operation_type, commands in BY_OPERATION.items():
        if isinstance(operation, operation_type):
            wanted.update(commands)
    inner = operation.wrapped
    if inner is not None:
        wanted.update(_operation_commands(inner))
    return frozenset(wanted)


def _command_users(operations: Iterable[Operation]) -> dict[str, tuple[str, ...]]:
    users: dict[str, list[str]] = {}
    for operation in operations:
        name = type(operation).__name__
        for command in _operation_commands(operation):
            named = users.setdefault(command, [])
            if name not in named:
                named.append(name)
    return {command: tuple(names) for command, names in users.items()}


def _disks_at_risk(graph: DeviceGraph) -> list[Existing]:
    """Every device this run destroys the content of.

    The same rule the confirmation row reads, so the two cannot disagree: a
    wiped disk, a table written or edited, and a filesystem created on a
    device the operator kept.
    """
    return list(compat.destroyed(graph))


def _passphrase_problems(
    config: InstallConfig, probe: Probe, secrets: SecretStore | None = None
) -> list[str]:
    """Read every passphrase file before the first disk is touched.

    `zpool create` rejects a short passphrase only once the pool's vdevs have
    been partitioned, which leaves the disk wiped and the install stopped.

    Through the probe, because everything preflight decides has to come from
    its declared inputs: opening the path here made the same configuration and
    the same probe answer differently on two machines.
    """
    problems: list[str] = []
    graph = config.disk.graph
    encrypted: list[tuple[str, str, int]] = [
        (node.id, node.passphrase_file, 0) for node in graph.of_type(Luks)
    ]
    encrypted += [
        (pool.id, pool.passphrase_file, ZFS_PASSPHRASE_MINIMUM)
        for pool in graph.of_type(ZfsPool)
        if pool.encrypted
    ]
    for device, source, minimum in encrypted:
        if not source:
            problems.append(f"{device} is encrypted but names no passphrase_file")
            continue
        passphrase, unreadable = probe.passphrase(source)
        if unreadable:
            problems.append(f"{device}: {source} cannot be read: {unreadable}")
            continue
        if not passphrase:
            problems.append(f"{device}: {source} is empty")
        elif len(passphrase) < minimum:
            problems.append(
                f"{device}: {source} holds {len(passphrase)} characters and zfs takes at "
                f"least {minimum}"
            )
        elif secrets is not None:
            secrets.stage(DeviceId(device), passphrase)
    return problems


def _capacity_problems(config: InstallConfig, probe: Probe) -> list[str]:
    """Whether each table's fixed partitions fit the disk they are on.

    Checked before anything runs, because `sgdisk --new` refuses the partition
    that does not fit only after `wipefs --all` and `sgdisk --zap-all` have
    already destroyed the table that was there. 512-byte sectors: the GPT tail
    is 33 of them, so assuming the smaller sector reserves the smaller tail and
    the check stays on the permissive side of a 4Kn disk.
    """
    graph = config.disk.graph
    problems: list[str] = []
    supplied_root_sizes: dict[DeviceId, Size] = {}
    for table in graph.of_type(PartitionTable):
        disk = graph[table.disk]
        if not isinstance(disk, Existing):
            continue
        path = ""
        if config.disk.mode is DiskMode.IMAGE:
            if config.disk.size is None:
                continue
            capacity = config.disk.size.bytes
        else:
            try:
                path = probe.resolve(disk.id, disk.selector)
                capacity = probe.disk_bytes(path)
            except DeviceNotFound:
                # Already reported by the loop that resolves every wiped disk.
                continue
        if not capacity:
            # Fatal, not skipped: this table is about to be rewritten, and a
            # capacity the machine would not report is not permission to write
            # it. Skipping let `wipefs` run before `sgdisk` found no room.
            problems.append(
                f"{disk.selector} did not report a size, so the layout on {table.id} "
                "cannot be checked against it"
            )
            continue
        claimed = sum(
            one.size.bytes
            for one in graph.of_type(Partition)
            if one.table == table.id and one.size is not None
        )
        if not table.create and config.disk.mode is not DiskMode.IMAGE:
            # An edited table keeps every partition the configuration does not
            # remove, and their space is claimed as much as a new one's.
            try:
                present = probe.partition_sizes(path)
            except DeviceNotFound:
                problems.append(
                    f"{disk.selector} did not report its partitions, so what {table.id} "
                    "keeps cannot be counted"
                )
                continue
            missing = sorted(set(table.remove) - set(present))
            if missing:
                # Before anything runs, because the removals are one `sgdisk
                # --delete` each and the earlier ones are already committed
                # when a later number is refused: `Partition number 99 out of
                # range!` left a disk whose partition 1 had been deleted.
                problems.append(
                    f"{table.id} removes {missing} from {disk.selector}, "
                    f"which has {sorted(present)}"
                )
            claimed += sum(
                size for number, size in present.items() if number not in table.remove
            )
        usable = Size(capacity)
        if table.table is TableType.GPT:
            usable = usable.gpt_last_usable(SectorSize(512))
        # The first partition starts at the alignment boundary, not at zero.
        usable = Size(max(0, usable.bytes - DEFAULT_ALIGNMENT))
        supplied_root_sizes[table.id] = usable
        if claimed > usable.bytes:
            problems.append(
                f"{disk.selector} holds {Size(capacity)} and {table.id} claims "
                f"{Size(claimed)} in fixed sizes, which does not fit"
            )
    problems += root_size_problems(config, supplied_root_sizes)
    return problems


#: What the build needs outside the tmpfs: the compiler, the sources it has
#: open, and the page cache under them.
RAM_HEADROOM: Final[int] = 2 * 1024**3


def _memory_problems(config: InstallConfig, machine: Machine) -> list[str]:
    """A tmpfs the machine cannot spare is a failure this can see coming.

    The size is the operator's, so it is comparable before anything runs: a
    build that outgrows its tmpfs fails on ENOSPC after hours of compiling.
    """
    wanted = config.portage.build_in_ram
    if wanted is None or not machine.memory_bytes:
        return []
    held = Size(machine.memory_bytes)
    if wanted.bytes >= machine.memory_bytes:
        return [
            f"the build tmpfs is {wanted} and this machine has {held}, "
            "so nothing would be left to build with"
        ]
    if machine.memory_bytes - wanted.bytes < RAM_HEADROOM:
        return [
            f"the build tmpfs is {wanted} of {held}, leaving "
            f"{Size(machine.memory_bytes - wanted.bytes)} for the compiler and its sources"
        ]
    return []


def _dd_problems(config: InstallConfig, probe: Probe) -> list[str]:
    """Safety checks for a write that replaces a whole disk at once."""
    problems: list[str] = []
    medium = probe.live_medium()
    if not medium and not probe.memory_environment():
        problems.append(
            "dd mode overwrites a whole disk; boot a live or memory environment first"
        )
    if not probe.image_source_exists(config.disk.source):
        problems.append(f"image source {config.disk.source!r} is not a regular file")
    destination = config.disk.destination
    if not probe.whole_disk(destination):
        problems.append(f"dd destination {destination!r} is not a whole disk")
    elif probe.mounted(destination):
        problems.append(f"dd destination {destination!r} is mounted or holds active swap")
    return problems


def check(
    config: InstallConfig,
    probe: Probe,
    target: str = "/mnt/gentoo",
    *,
    operations: Iterable[Operation] | None = None,
) -> Report:
    plan = tuple(operations) if operations is not None else None
    wanted = required_commands(config, plan)
    machine = probe.machine(wanted, judged=set(GNU_ONLY) & wanted)
    return inspect(config, machine, probe, target, operations=plan)


def inspect(
    config: InstallConfig,
    machine: Machine,
    probe: Probe,
    target: str = "/mnt/gentoo",
    *,
    operations: Iterable[Operation] | None = None,
) -> Report:
    plan = tuple(operations) if operations is not None else None
    wanted = required_commands(config, plan)
    config_target = target
    fatal: list[str] = []
    warnings: list[str] = []

    if not machine.root:
        fatal.append("the installer has to run as root")
    # The row rather than the literal: `uname -m` and Gentoo spell the same
    # architecture differently, and the message has to name the one the
    # configuration targets rather than the one this file was written for.
    installs = compat.DEFAULT_ARCHITECTURE
    if config.disk.mode is not DiskMode.DD and machine.architecture != installs.kernel_name:
        fatal.append(
            f"this build installs {installs.gentoo_name} and the machine reports "
            f"{machine.architecture}"
        )
    targets_machine_firmware = config.disk.mode in (DiskMode.PARTITION, DiskMode.IN_PLACE)
    wants_uefi = config.bootloader.firmware is Firmware.UEFI
    if targets_machine_firmware and wants_uefi and not machine.uefi:
        fatal.append(f"the configuration boots by UEFI and this machine booted by BIOS")
    if targets_machine_firmware and not wants_uefi and machine.uefi:
        warnings.append("the configuration boots by BIOS on a machine that booted by UEFI")
    if targets_machine_firmware and wants_uefi and not machine.efi_variables:
        # Fatal: `efibootmgr --create` is what the ZFSBootMenu install runs,
        # and GRUB's `--bootloader-id` entry needs the same. The operator can
        # mount it, so the message says which command.
        fatal.append(
            "the firmware variables are not readable: mount efivarfs with "
            "`mount -t efivarfs efivarfs /sys/firmware/efi/efivars` before installing"
        )
    if targets_machine_firmware and wants_uefi and machine.efi_bits == 32:
        # Fatal rather than a warning: the install would finish and the
        # firmware then refuse the amd64 executable it was handed.
        fatal.append(
            "this machine booted through 32-bit EFI firmware, which cannot load "
            "the amd64 EFI executables an amd64 install writes"
        )

    commands = assess_commands(wanted, machine.commands, machine.versions)
    missing = sorted(commands.missing)
    users = _command_users(plan) if plan is not None else {}
    unnamed = [command for command in missing if command not in users]
    if unnamed:
        fatal.append(f"these commands are missing: {', '.join(unnamed)}")
    for command in missing:
        if command in users:
            fatal.append(
                f"{command} is missing; required by {', '.join(users[command])}"
            )
    fatal += [problem.reason for problem in commands.unusable]
    if config.disk.mode is DiskMode.DD:
        fatal.extend(_dd_problems(config, probe))
        return Report(fatal=tuple(fatal), warnings=tuple(warnings))

    # Said rather than refused: installing from a running system onto a second
    # disk is a real thing to do, and the guard that matters is below — a disk
    # with anything mounted on it is not repartitioned. What was missing is
    # that nothing named the difference before the disk screen.
    if config.disk.mode is DiskMode.IN_PLACE:
        medium = probe.live_medium()
        if medium:
            fatal.append(
                "in-place conversion replaces the running userland, and this is a live "
                f"medium ({medium}): install onto a disk instead"
            )

    if config.disk.mode is not DiskMode.IMAGE:
        if _disks_at_risk(config.disk.graph) and not probe.live_medium():
            where = probe.root_source()
            warnings.append(
                "this does not look like a live medium"
                + (f" ({where} is mounted at /)" if where else "")
                + "; the disks below belong to the machine you are running on"
            )

        for disk in _disks_at_risk(config.disk.graph):
            try:
                path = probe.resolve(disk.id, disk.selector)
            except DeviceNotFound as error:
                fatal.append(str(error))
                continue
            if probe.mounted(path, ignoring=str(config_target)):
                fatal.append(
                    f"{path} is mounted; the installer will not repartition a disk in use"
                )

    if config.disk.graph.of_type(ZfsPool):
        # The commands alone are not enough: a medium can carry the userland
        # and no module, and `zpool create` finds that out after the disks are
        # already partitioned.
        unusable = probe.zfs_support()
        if unusable:
            fatal.append(f"{unusable}, and this configuration makes a pool")

    secrets = SecretStore(probe.work)
    fatal += _passphrase_problems(config, probe, secrets)
    fatal += _capacity_problems(config, probe)

    if not machine.release_key:
        # Not fatal: every medium that is not Gentoo's ships no key file, and
        # `fetch` downloads one whose fingerprint still has to match the pin.
        warnings.append(
            f"{RELEASE_KEY} is absent, so the release key is fetched before the stage3 is verified"
        )

    fatal.extend(_memory_problems(config, machine))
    if (
        config.portage.build_in_ram is not None
        and machine.memory_bytes
        and machine.memory_bytes < TMPFS_MINIMUM
    ):
        # Only when a tmpfs was asked for. The warning fired on every machine
        # under 8 GiB, including the ones building on disk, where how much
        # memory there is decides nothing.
        warnings.append(
            f"{Size(machine.memory_bytes)} of memory: build in /var/tmp rather than a tmpfs"
        )
    return Report(fatal=tuple(fatal), warnings=tuple(warnings), secrets=secrets)
