"""The one table of combinations that do not work.

A rule is a pair of traits: when the condition holds and the excluded trait holds
too, the configuration is broken. `validate.py` turns that into an error and the
TUI greys the excluded choice out with the same sentence, so a rule is written
once and read twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
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
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    FilesystemType,
    Luks,
    MdRaid,
    Mountpoint,
    Node,
    Partition,
    PartitionRole,
    PartitionTable,
    RaidMetadata,
    StorageFacts,
    Swap,
    T,
    TableType,
    VolumeGroup,
    ZfsDataset,
    ZfsPool,
)

#: Sizes the cjktty patch carries CJK glyphs for. Any other size draws garbage.
#:
#: One, not two. `sys-kernel/gentoo-cjk-kernel` writes
#: `# CONFIG_FONT_CJK_32x32 is not set` and says why: the 32x32 font the patch
#: ships is empty. A 16x32 Latin font pairs with that one, so choosing it with
#: CJK on gives a console with no CJK glyphs, which is what the option exists
#: to provide.
CJK_FONT_SIZES = frozenset({ConsoleFontSize.SIZE_8X16})


class FilesystemLabelUnit(Enum):
    BYTES = "bytes"
    CHARACTERS = "characters"

    def measure(self, label: str) -> int:
        return len(label.encode()) if self is FilesystemLabelUnit.BYTES else len(label)


@dataclass(frozen=True)
class FilesystemLabelRule:
    kind: FilesystemType
    maximum: int
    unit: FilesystemLabelUnit

    def problem(self, filesystem: Filesystem) -> str | None:
        length = self.unit.measure(filesystem.label)
        if not filesystem.create or length <= self.maximum:
            return None
        return (
            f"filesystem {filesystem.id} has a {filesystem.kind.value} label of {length} "
            f"{self.unit.value}, but {filesystem.kind.value} labels are limited to "
            f"{self.maximum} {self.unit.value}"
        )


FILESYSTEM_LABEL_RULES: tuple[FilesystemLabelRule, ...] = (
    FilesystemLabelRule(FilesystemType.EXT2, 16, FilesystemLabelUnit.BYTES),
    FilesystemLabelRule(FilesystemType.EXT3, 16, FilesystemLabelUnit.BYTES),
    FilesystemLabelRule(FilesystemType.EXT4, 16, FilesystemLabelUnit.BYTES),
    # The CJK probe fails during CP850 conversion before length is checked, so
    # this entry represents only mkfs.fat's independent character-count limit.
    FilesystemLabelRule(FilesystemType.VFAT, 11, FilesystemLabelUnit.CHARACTERS),
)


def filesystem_label_problems(config: InstallConfig) -> tuple[str, ...]:
    rules = {rule.kind: rule for rule in FILESYSTEM_LABEL_RULES}
    return tuple(
        problem
        for filesystem in config.disk.graph.of_type(Filesystem)
        if filesystem.kind in rules
        if (problem := rules[filesystem.kind].problem(filesystem)) is not None
    )


_BOOT = PurePosixPath("/boot")
_ROOT = PurePosixPath("/")
_USR = PurePosixPath("/usr")
#: Where an esp is mounted in the target, in the order the installer prefers.
#: Where an esp is mounted, in the order the handbook and the installers use.
#: `/boot/efi` is what `calamares-settings-gig` mounts, so a layout the GUI
#: installer produces has to be one this validator accepts.
_ESP_PATHS = (
    PurePosixPath("/efi"),
    PurePosixPath("/boot"),
    PurePosixPath("/boot/efi"),
)


class Trait(Enum):
    """Something a configuration is or has. The value is shown to the user."""

    ROOT_ON_ZFS = "root on ZFS"
    BOOT_ON_ZFS = "/boot on ZFS"
    NO_ZFS_ROOT = "a root that is not a ZFS dataset"
    LUKS = "LUKS"
    GRUB = "GRUB"
    SYSTEMD_BOOT = "systemd-boot"
    ZFSBOOTMENU = "ZFSBootMenu"
    UEFI_BOOT = "UEFI boot"
    BIOS_BOOT = "BIOS boot"
    NO_MOUNTED_ESP = "no mounted esp"
    ESP_ENCRYPTED = "an encrypted esp"
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
    NATIVE_ZFS_SYSTEM_INITRAMFS = "ZFS native encryption with GRUB or systemd-boot"
    FONT_WITHOUT_CJK_GLYPHS = "a console font other than 8x16"
    ROOT_LOCKED = "a root password hash that cannot authenticate"
    NO_OTHER_LOGIN = "no user password and no authorised ssh key usable by sshd"


@dataclass(frozen=True)
class Rule:
    when: Trait
    excludes: Trait
    reason: str

    def describe(self, translate: Callable[[str], str] = str) -> str:
        """The whole sentence, every part of it translatable.

        The trait names were not catalog keys, so a Chinese interface drew
        `root on ZFS excludes BIOS boot:` in English in front of a translated
        reason. `str` as the default keeps the English for a log.
        """
        return "{}: {}".format(
            translate("{when} excludes {excludes}").format(
                when=translate(self.when.value), excludes=translate(self.excludes.value)
            ),
            translate(self.reason),
        )


RULES: tuple[Rule, ...] = (
    Rule(
        Trait.ROOT_LOCKED,
        Trait.NO_OTHER_LOGIN,
        "the root password hash is empty, locked, or malformed, and no user "
        "password or usable ssh key can log into the installed system",
    ),
    Rule(
        Trait.ROOT_ON_ZFS,
        Trait.GRUB,
        "GRUB reads only some ZFS feature flags, so a pool that enables a newer "
        "one stops booting",
    ),
    Rule(
        Trait.BOOT_ON_ZFS,
        Trait.GRUB,
        "the kernel is on a pool created with today's feature flags, and GRUB "
        "reads none of them: only a pool made with compatibility=grub2 is",
    ),
    Rule(Trait.ROOT_ON_ZFS, Trait.BIOS_BOOT, "ZFSBootMenu is an EFI executable"),
    Rule(Trait.ROOT_ON_ZFS, Trait.LUKS, "use ZFS native encryption instead"),
    Rule(
        Trait.ZFSBOOTMENU,
        Trait.NO_ZFS_ROOT,
        "it imports a pool and boots a dataset from it, so it needs ZFS",
    ),
    Rule(
        Trait.UEFI_BOOT,
        Trait.NO_MOUNTED_ESP,
        "an EFI executable has to live on a vfat esp mounted in the target",
    ),
    Rule(
        Trait.UEFI_BOOT,
        Trait.ESP_ENCRYPTED,
        "firmware reads the esp itself and cannot open a LUKS container, so an "
        "encrypted one never boots",
    ),
    Rule(Trait.SYSTEMD_BOOT, Trait.BIOS_BOOT, "systemd-boot has no BIOS implementation"),
    Rule(
        Trait.SYSTEMD_BOOT,
        Trait.KERNEL_OFF_ESP,
        "the separate /boot is encrypted or not vfat, so systemd-boot cannot "
        "read its kernels or entries",
    ),
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
        Trait.REMOTE_UNLOCK,
        Trait.NATIVE_ZFS_SYSTEM_INITRAMFS,
        "GRUB and systemd-boot put the ssh helper in the system initramfs, where "
        "it calls cryptsetup and cannot load a native ZFS key",
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
        "the cjk kernel builds CONFIG_FONT_CJK_16x16 alone, which pairs with the 8x16 font, because the 32x32 font the patch ships is empty",
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


def traits_of(
    config: InstallConfig, storage_facts: StorageFacts | None = None
) -> frozenset[Trait]:
    graph = config.disk.graph
    facts = storage_facts if storage_facts is not None else StorageFacts()
    found: set[Trait] = set()

    if not _password_can_authenticate(config.system.root_password_hash):
        found.add(Trait.ROOT_LOCKED)
        named = any(_password_can_authenticate(one.password_hash) for one in config.system.users)
        key_account = config.system.sshd and bool(config.system.authorized_keys) and (
            config.system.sshd_root_login or any(one.sudo for one in config.system.users)
        )
        if not named and not key_account:
            found.add(Trait.NO_OTHER_LOGIN)

    if _holds(graph, config.disk.root, (ZfsPool, ZfsDataset)):
        found.add(Trait.ROOT_ON_ZFS)
    else:
        found.add(Trait.NO_ZFS_ROOT)
    # Read from whatever covers /boot rather than from the root: a layout with
    # an ext4 root and the kernel on a ZFS dataset passed every rule, and GRUB
    # cannot read a pool made with today's feature flags either way.
    boot = _covering_mount(graph, _BOOT)
    if boot is not None and _holds(graph, boot.id, (ZfsPool, ZfsDataset)):
        found.add(Trait.BOOT_ON_ZFS)
    if any(isinstance(node, Luks) for node in _chain(graph, config.disk.root)):
        # Scoped to the root, like ROOT_ON_ZFS: the rules that name LUKS are
        # about what carries `/`, and a graph-wide test paired a ZFS root with
        # an encrypted partition that has nothing to do with it.
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
    if _encrypted_esp(graph):
        found.add(Trait.ESP_ENCRYPTED)
    # Where `kernel-install` writes, which is `$BOOT_ROOT`: the XBOOTLDR
    # partition at /boot when there is one, and the esp otherwise. bootctl(1):
    # "/efi/, /boot/, and /boot/efi/ are checked in turn. It is recommended to
    # mount the ESP to /efi/". So an esp at /efi with /boot an ordinary
    # directory on the root is the recommended layout, and refusing it made
    # systemd-boot unselectable from the default one. A separate /boot is only
    # a problem when the loader cannot read it: it has a vfat driver and no
    # other.
    boot = _covering_mount(graph, _BOOT)
    esp = esp_mount(graph)
    separate = boot is not None and boot.path == _BOOT and (esp is None or boot.id != esp.id)
    inaccessible = separate and boot is not None and (
        not _is_vfat(graph, boot.id)
        or any(isinstance(node, Luks) for node in _chain(graph, boot.id))
    )
    if esp is None or inaccessible:
        found.add(Trait.KERNEL_OFF_ESP)

    for array in graph.of_type(MdRaid):
        if not _on_esp(graph, array.id):
            continue
        found.add(Trait.ESP_ON_MDRAID)
        if array.metadata.superblock_at_start:
            found.add(Trait.ESP_MDRAID_SUPERBLOCK_AT_START)
    # A reused array is `Existing` and carries no `MdRaid` node, so the loop
    # above never sees it, and `_on_esp` cannot answer for it either: that
    # reads what a device is built from, and a reused array is built from
    # nothing this model knows. Runtime facts identify the assembled array.
    mounted = esp_mount(graph)
    beneath = {node.id for node in _chain(graph, mounted.id)} if mounted else set()
    for reused in graph.of_type(Existing):
        metadata = facts.metadata_for(reused.id)
        if not isinstance(metadata, RaidMetadata) or reused.id not in beneath:
            continue
        found.add(Trait.ESP_ON_MDRAID)
        if metadata.superblock_at_start:
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
        if not early_containers(graph) and not _encrypted_pool(graph, config.disk.root):
            found.add(Trait.NO_ENCRYPTED_CONTAINER)
        if (
            config.bootloader.kind is not Bootloader.ZFSBOOTMENU
            and _encrypted_pool(graph, config.disk.root)
        ):
            found.add(Trait.NATIVE_ZFS_SYSTEM_INITRAMFS)
    if config.kernel.source in (KernelSource.CJK_BIN, KernelSource.CJK):
        found.add(Trait.CJK_KERNEL)
    else:
        found.add(Trait.KERNEL_WITHOUT_CJKTTY)
    if config.system.console_font not in CJK_FONT_SIZES:
        found.add(Trait.FONT_WITHOUT_CJK_GLYPHS)

    return frozenset(found)


_MODULAR_CRYPT = re.compile(
    r"^\$(?:1|2[abxy]|5|6|y|gy|7)\$(?:[^:$\n]+\$)+[^:$\n]+$"
)
_DES_CRYPT = re.compile(r"^[./0-9A-Za-z]{13}$")


def _password_can_authenticate(password_hash: str) -> bool:
    """Whether the value has a supported crypt(3) hash shape."""
    return bool(_MODULAR_CRYPT.fullmatch(password_hash) or _DES_CRYPT.fullmatch(password_hash))


def violations(
    config: InstallConfig, storage_facts: StorageFacts | None = None
) -> tuple[Rule, ...]:
    present = traits_of(config, storage_facts)
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


def _is_vfat(graph: DeviceGraph, device: DeviceId) -> bool:
    """Whether the mount's filesystem is one systemd-boot can read."""
    return any(
        one.kind is FilesystemType.VFAT for one in _nodes_under(graph, device, Filesystem)
    )


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
    if any(isinstance(node, Partition) for node in _chain(graph, device)):
        return False
    # No partition to ask, so the filesystem answers. Not `create`: whether
    # this run formats the esp does not change what the partition is, and
    # keying on it made ticking `format` turn an esp into something else.
    return any(
        one.kind is FilesystemType.VFAT for one in _nodes_under(graph, device, Filesystem)
    )


def destroyed(graph: DeviceGraph) -> tuple[Existing, ...]:
    """Every device the operator already had whose content this plan destroys.

    Three ways, and only the first was counted. A wiped disk is obvious. A
    table written from scratch or with an entry removed loses what those
    entries described. And a filesystem created on an `Existing` device is
    `mkfs` over whatever was there: the disk-level `wipe` is false, no table
    is rewritten, and both the confirmation row and the in-use check read the
    layout as harmless.
    """
    tables = {
        table.disk
        for table in graph.of_type(PartitionTable)
        if table.create or table.remove
    }
    written: set[DeviceId] = set()
    for node in graph.nodes.values():
        for device in _writes_over(node):
            written |= _existing_beneath(graph, device)
    return tuple(
        disk
        for disk in graph.of_type(Existing)
        if disk.wipe or disk.id in tables or disk.id in written
    )


def _writes_over(node: Node) -> tuple[DeviceId, ...]:
    """What this node puts a new signature on, or nothing.

    One place, so an operation that destroys content cannot be added without
    the confirmation and the in-use check seeing it.
    """
    if isinstance(node, Filesystem) and node.create:
        return (node.device,)
    if isinstance(node, Luks):
        # `CreateLuks` runs `luksFormat`, which overwrites the header and the
        # data that was there.
        return (node.backing,)
    if isinstance(node, Swap):
        # `mkswap` writes a signature over whatever was there.
        return (node.device,)
    if isinstance(node, (MdRaid, VolumeGroup)):
        return tuple(node.members)
    if isinstance(node, ZfsPool):
        return tuple(node.vdevs)
    return ()


def _existing_beneath(graph: DeviceGraph, device: DeviceId) -> set[DeviceId]:
    """The devices the operator already had that sit under `device`.

    Followed rather than tested one level down: an encrypted `format` of a
    retained partition is `Existing -> Luks -> Filesystem`, and reading only
    the filesystem's own device found `Luks` and reported nothing destroyed.
    The erase row then said the layout was harmless and preflight never asked
    whether the partition was mounted, while `luksFormat` overwrote it.
    """
    found: set[DeviceId] = set()
    seen: set[DeviceId] = set()
    edge = [device]
    while edge:
        at = edge.pop()
        if at in seen or at not in graph.nodes:
            continue
        seen.add(at)
        node = graph[at]
        if isinstance(node, Existing):
            found.add(node.id)
            continue
        if isinstance(node, Partition):
            # Stop here. What happens to a partition's content is the table's
            # question, and climbing through it to the disk would say the
            # whole drive is erased when one partition is being formatted.
            continue
        edge.extend(node.inputs)
    return found


def esp_mount(graph: DeviceGraph) -> Mountpoint | None:
    for path in _ESP_PATHS:
        for mount in graph.of_type(Mountpoint):
            if mount.path == path and _on_esp(graph, mount.id):
                return mount
    return None


def _encrypted_esp(graph: DeviceGraph) -> bool:
    """Whether a container sits between the firmware and the esp.

    The mount that is the esp, not any mount at an esp path: a plain vfat esp
    at `/efi` beside an unrelated encrypted `/boot` is a working layout and
    scanning every esp path refused it. The scan stays as the fallback for a
    layout with no esp mount at all, where saying the esp is missing would
    send the operator looking for a partition that is there.
    """
    chosen = esp_mount(graph)
    if chosen is not None:
        return any(isinstance(node, Luks) for node in _chain(graph, chosen.id))
    return any(
        mount.path in _ESP_PATHS
        and any(isinstance(node, Luks) for node in _chain(graph, mount.id))
        for mount in graph.of_type(Mountpoint)
    )


def _encrypted_pool(graph: DeviceGraph, root: DeviceId) -> bool:
    """Whether the root is a dataset of a pool with native encryption, which
    prompts for a passphrase exactly as a LUKS container does."""
    return any(
        isinstance(node, ZfsPool) and node.encrypted for node in _chain(graph, root)
    )


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


def boot_is_inside(graph: DeviceGraph, device: DeviceId) -> bool:
    """Whether the kernel and initramfs land on something built from `device`.

    A key file baked into the initramfs is only protected if the initramfs is
    inside the thing that key unlocks; a separate /boot partition puts it in
    the clear.
    """
    boot = _covering_mount(graph, _BOOT)
    if boot is None:
        return False
    return device in graph.ancestors_of(boot.id)


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
