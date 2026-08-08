"""The one table of combinations that do not work.

A rule is a pair of traits: when the condition holds and the excluded trait holds
too, the configuration is broken. `validate.py` turns that into an error and the
TUI greys the excluded choice out with the same sentence, so a rule is written
once and read twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

from .config import (
    BinhostChannel,
    Bootloader,
    ConsoleFontSize,
    Firmware,
    InstallConfig,
    KernelSource,
)
from .device import (
    T,
    DeviceGraph,
    DeviceId,
    Filesystem,
    FilesystemType,
    Luks,
    MdRaid,
    Mountpoint,
    Node,
    Partition,
    PartitionRole,
    PartitionTable,
    TableType,
    ZfsDataset,
    ZfsPool,
)

#: Sizes the cjktty patch carries CJK glyphs for. Any other size draws garbage.
CJK_FONT_SIZES = frozenset({ConsoleFontSize.SIZE_8X16, ConsoleFontSize.SIZE_16X32})

_BOOT = PurePosixPath("/boot")
_ROOT = PurePosixPath("/")
_USR = PurePosixPath("/usr")
#: Where an esp is mounted in the target, in the order the installer prefers.
_ESP_PATHS = (PurePosixPath("/efi"), PurePosixPath("/boot"))


class Trait(Enum):
    """Something a configuration is or has. The value is shown to the user."""

    ROOT_ON_ZFS = "root on ZFS"
    LUKS = "LUKS"
    GRUB = "GRUB"
    SYSTEMD_BOOT = "systemd-boot"
    ZFSBOOTMENU = "ZFSBootMenu"
    UEFI_BOOT = "UEFI boot"
    BIOS_BOOT = "BIOS boot"
    NO_MOUNTED_ESP = "no mounted esp"
    KERNEL_OFF_ESP = "kernel and initramfs off the esp"
    ESP_ON_MDRAID = "esp on mdraid"
    ESP_MDRAID_SUPERBLOCK_AT_START = "mdraid metadata 1.1 or 1.2 under the esp"
    GPT_WITHOUT_BIOS_BOOT = "a GPT boot disk with no bios-boot partition"
    CONSOLE_CJK = "CJK on the console"
    COMMUNITY_BINHOST = "the gentoo-zh binary host"
    NO_GENTOOZH_OVERLAY = "no gentoo-zh overlay"
    KERNEL_WITHOUT_CJKTTY = "a kernel that does not carry the cjktty patch"
    CJK_KERNEL = "the patched kernel from gentoo-zh"
    REMOTE_UNLOCK = "unlocking the root over ssh"
    NO_AUTHORIZED_KEY = "no authorised ssh key"
    NO_ENCRYPTED_CONTAINER = "no encrypted container to unlock"
    FONT_WITHOUT_CJK_GLYPHS = "a console font other than 8x16 or 16x32"


@dataclass(frozen=True)
class Rule:
    when: Trait
    excludes: Trait
    reason: str

    def describe(self) -> str:
        return f"{self.when.value} excludes {self.excludes.value}: {self.reason}"


RULES: tuple[Rule, ...] = (
    Rule(
        Trait.ROOT_ON_ZFS,
        Trait.GRUB,
        "GRUB reads only some ZFS feature flags, so a pool that enables a newer "
        "one stops booting",
    ),
    Rule(Trait.ROOT_ON_ZFS, Trait.BIOS_BOOT, "ZFSBootMenu is an EFI executable"),
    Rule(Trait.ROOT_ON_ZFS, Trait.LUKS, "use ZFS native encryption instead"),
    Rule(
        Trait.UEFI_BOOT,
        Trait.NO_MOUNTED_ESP,
        "an EFI executable has to live on a vfat esp mounted in the target",
    ),
    Rule(Trait.SYSTEMD_BOOT, Trait.BIOS_BOOT, "systemd-boot has no BIOS implementation"),
    Rule(Trait.SYSTEMD_BOOT, Trait.KERNEL_OFF_ESP, "it cannot read ext4 or btrfs"),
    Rule(
        Trait.ESP_ON_MDRAID,
        Trait.ESP_MDRAID_SUPERBLOCK_AT_START,
        "firmware reads a member as a plain vfat partition, and only 0.90 and 1.0 "
        "keep the superblock at the end of the member",
    ),
    Rule(Trait.BIOS_BOOT, Trait.GPT_WITHOUT_BIOS_BOOT, "GRUB stage 1.5 needs somewhere to live"),
    Rule(
        Trait.CONSOLE_CJK,
        Trait.KERNEL_WITHOUT_CJKTTY,
        "cjktty patches the kernel VT layer, which no official kernel carries; "
        "sys-kernel/gentoo-cjk-kernel is the one that does",
    ),
    Rule(
        Trait.REMOTE_UNLOCK,
        Trait.NO_AUTHORIZED_KEY,
        "dracut-crypt-ssh authorises /root/.ssh/authorized_keys, so with none "
        "the initramfs runs an ssh daemon nobody can log into",
    ),
    Rule(
        Trait.REMOTE_UNLOCK,
        Trait.NO_ENCRYPTED_CONTAINER,
        "there is no passphrase prompt to reach: the root is not encrypted",
    ),
    Rule(
        Trait.CJK_KERNEL,
        Trait.NO_GENTOOZH_OVERLAY,
        "sys-kernel/gentoo-cjk-kernel is in that overlay and in no other "
        "repository, so the emerge fails once the disks have been partitioned",
    ),
    Rule(
        Trait.CONSOLE_CJK,
        Trait.FONT_WITHOUT_CJK_GLYPHS,
        "cjktty ships CJK glyphs for 8x16 and 16x32 only",
    ),
    Rule(
        Trait.ZFSBOOTMENU,
        Trait.NO_GENTOOZH_OVERLAY,
        "sys-boot/zfsbootmenu is in that overlay and in no other repository, so "
        "the emerge fails once the disks have already been partitioned",
    ),
    Rule(
        Trait.COMMUNITY_BINHOST,
        Trait.NO_GENTOOZH_OVERLAY,
        "the key its packages are signed with comes from that overlay, and "
        "without it nothing from the host verifies",
    ),
)


def traits_of(config: InstallConfig) -> frozenset[Trait]:
    graph = config.disk.graph
    found: set[Trait] = set()

    if _holds(graph, config.disk.root, (ZfsPool, ZfsDataset)):
        found.add(Trait.ROOT_ON_ZFS)
    if graph.of_type(Luks):
        found.add(Trait.LUKS)

    if config.bootloader.kind is Bootloader.GRUB:
        found.add(Trait.GRUB)
    if config.bootloader.kind is Bootloader.SYSTEMD_BOOT:
        found.add(Trait.SYSTEMD_BOOT)
    if config.bootloader.kind is Bootloader.ZFSBOOTMENU:
        found.add(Trait.ZFSBOOTMENU)
    if config.bootloader.firmware is Firmware.UEFI:
        found.add(Trait.UEFI_BOOT)
    else:
        found.add(Trait.BIOS_BOOT)

    if esp_mount(graph) is None:
        found.add(Trait.NO_MOUNTED_ESP)
    boot = _covering_mount(graph, _BOOT)
    if boot is None or not _on_esp(graph, boot.id):
        found.add(Trait.KERNEL_OFF_ESP)

    for array in graph.of_type(MdRaid):
        if not _on_esp(graph, array.id):
            continue
        found.add(Trait.ESP_ON_MDRAID)
        if array.metadata.superblock_at_start:
            found.add(Trait.ESP_MDRAID_SUPERBLOCK_AT_START)

    for table in _nodes_under(graph, config.disk.root, PartitionTable):
        if table.table is TableType.GPT and not _has_bios_boot(graph, table.id):
            found.add(Trait.GPT_WITHOUT_BIOS_BOOT)

    if config.system.console_cjk:
        found.add(Trait.CONSOLE_CJK)
    if config.portage.binhost.community is not BinhostChannel.OFF:
        found.add(Trait.COMMUNITY_BINHOST)
    if not any(overlay.name == "gentoo-zh" for overlay in config.portage.overlays):
        found.add(Trait.NO_GENTOOZH_OVERLAY)
    if config.kernel.remote_unlock.enabled:
        found.add(Trait.REMOTE_UNLOCK)
        if not config.system.authorized_keys:
            found.add(Trait.NO_AUTHORIZED_KEY)
        if not early_containers(graph):
            found.add(Trait.NO_ENCRYPTED_CONTAINER)
    if config.kernel.source is KernelSource.CJK:
        found.add(Trait.CJK_KERNEL)
    else:
        found.add(Trait.KERNEL_WITHOUT_CJKTTY)
    if config.system.console_font not in CJK_FONT_SIZES:
        found.add(Trait.FONT_WITHOUT_CJK_GLYPHS)

    return frozenset(found)


def violations(config: InstallConfig) -> tuple[Rule, ...]:
    present = traits_of(config)
    return tuple(rule for rule in RULES if rule.when in present and rule.excludes in present)


def excluded_by(present: frozenset[Trait]) -> tuple[Rule, ...]:
    """Rules whose condition already holds, so the excluded choice is unavailable."""
    return tuple(rule for rule in RULES if rule.when in present)


def _chain(graph: DeviceGraph, device: DeviceId) -> tuple[Node, ...]:
    """What `device` is built from. Empty for an id no node defines, because
    `validate.py` reports that on its own and a trait cannot say it twice."""
    if device not in graph.nodes:
        return ()
    return tuple(graph[parent] for parent in graph.ancestors_of(device))


def _nodes_under(graph: DeviceGraph, device: DeviceId, kind: type[T]) -> tuple[T, ...]:
    return tuple(node for node in _chain(graph, device) if isinstance(node, kind))


def _holds(graph: DeviceGraph, device: DeviceId, kinds: tuple[type[Node], ...]) -> bool:
    return any(isinstance(node, kinds) for node in _chain(graph, device))


def _on_esp(graph: DeviceGraph, device: DeviceId) -> bool:
    """Whether the mount sits on an esp.

    A partition this install creates says so with its role. A reused one has no
    `Partition` node to ask, so the evidence is the filesystem: firmware reads
    vfat and nothing else, and the flag is already set on the disk.
    """
    if any(
        partition.role is PartitionRole.ESP for partition in _nodes_under(graph, device, Partition)
    ):
        return True
    kept = [one for one in _nodes_under(graph, device, Filesystem) if not one.create]
    return any(one.kind is FilesystemType.VFAT for one in kept)


def esp_mount(graph: DeviceGraph) -> Mountpoint | None:
    for path in _ESP_PATHS:
        for mount in graph.of_type(Mountpoint):
            if mount.path == path and _on_esp(graph, mount.id):
                return mount
    return None


def early_containers(graph: DeviceGraph) -> tuple[Luks, ...]:
    """The LUKS containers carrying `/` or `/usr`.

    The initramfs has to open these before it can mount anything, so they are
    what crypttab marks `x-initrd.attach` and what the kernel command line
    names with `rd.luks.uuid`.
    """
    found: dict[DeviceId, Luks] = {}
    for mount in graph.of_type(Mountpoint):
        if mount.path not in (_ROOT, _USR):
            continue
        ancestors = graph.ancestors_of(mount.id)
        found.update(
            {node.id: node for node in graph.of_type(Luks) if node.id in ancestors}
        )
    return tuple(found.values())


def boot_is_encrypted(graph: DeviceGraph) -> bool:
    """Whether the boot files sit inside a LUKS container.

    GRUB then has to unlock it before it can read them, which it refuses to do
    unless the configuration says so.
    """
    boot = _covering_mount(graph, _BOOT)
    if boot is None:
        return False
    return any(isinstance(graph[parent], Luks) for parent in graph.ancestors_of(boot.id))


def _covering_mount(graph: DeviceGraph, path: PurePosixPath) -> Mountpoint | None:
    """The mount a file at `path` lands on: the deepest one `path` sits under."""
    covering = [mount for mount in graph.of_type(Mountpoint) if path.is_relative_to(mount.path)]
    if not covering:
        return None
    return max(covering, key=lambda mount: len(mount.path.parts))


def _has_bios_boot(graph: DeviceGraph, table: DeviceId) -> bool:
    for child in graph.consumers_of(table):
        node = graph[child]
        if isinstance(node, Partition) and node.role is PartitionRole.BIOS_BOOT:
            return True
    return False
