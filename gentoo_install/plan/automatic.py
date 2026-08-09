"""What the installer puts on the kernel command line and in USE by itself.

The panel shows these beside what the operator typed. A value the installed
system ends up with is then something they were told about while they could
still change it, instead of something they find in `/etc/default/grub`
afterwards and cannot account for.

Every entry is derived from the function that actually adds the value, never
from a second list: a table that only describes the behaviour goes stale the
first time the behaviour changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..model.config import Bootloader, InstallConfig
from ..model.device import Mountpoint, Subvolume, ZfsDataset, ZfsPool
from .bootloader import initramfs_devices, initramfs_keymap, unlock_parameters
from .packages import Catalog, groups


#: Every reason an `Added` can carry, as English source strings. Held in one
#: place because the panel translates `Added.because` through a variable, which
#: the catalog test cannot see: a reason added without a translation has to
#: fail that test rather than draw an English line in a translated screen.
ROOT: Final[str] = "where the root filesystem is"
ROOT_FROM_GRUB: Final[str] = "grub-mkconfig derives this one, and mounts the root read-only"
ROOT_FROM_ZBM: Final[str] = "ZFSBootMenu boots whichever dataset it is pointed at"
WRITABLE: Final[str] = "the root is mounted writable, not read-only"
SUBVOLUME: Final[str] = "the initramfs mounts the default subvolume without it"
CONTAINER: Final[str] = "one for each container the initramfs opens before the root"
ARRAY: Final[str] = "one for each array the initramfs assembles before the root"
KEYMAP: Final[str] = "so the passphrase prompt uses the keyboard you chose"
UNLOCK: Final[str] = "remote unlock needs the initramfs to have an address"
GROUP_USE: Final[str] = "asked for by a group you chose"
GROUP_CARD: Final[str] = "the graphics driver you chose needs it"

REASONS: Final[tuple[str, ...]] = (
    ROOT, ROOT_FROM_GRUB, ROOT_FROM_ZBM, WRITABLE, SUBVOLUME, CONTAINER, ARRAY, KEYMAP, UNLOCK, GROUP_USE, GROUP_CARD,
)


@dataclass(frozen=True, kw_only=True)
class Added:
    """One value the installer adds without being asked, and why."""

    #: As it will appear in `make.conf` or on the command line. A value only
    #: known once a device exists ends in `=…`, because inventing a UUID here
    #: would read as the one the machine is going to get.
    value: str
    #: English source string for the catalog.
    because: str
    #: The group or device that caused it, shown as it is. An atom, a group
    #: name and a pool name are identifiers, so none of them is translated.
    source: str = ""
    #: Whether the installer writes this one. `grub-mkconfig` composes `root=`
    #: and `ro` itself from the filesystem it probes, so GRUB gets neither from
    #: us; showing them anyway is right, because the entry still carries them.
    written_here: bool = True


def kernel_parameters(config: InstallConfig) -> tuple[Added, ...]:
    """Everything the boot entry carries that the operator did not type.

    Split by bootloader because the three do not divide the work the same way:
    systemd-boot reads a command line we write in full, GRUB composes `root=`
    and `ro` in `10_linux` and takes only the rest from `/etc/default/grub`,
    and ZFSBootMenu finds the dataset itself.
    """
    kind = config.bootloader.kind
    added: list[Added] = []
    if kind is Bootloader.SYSTEMD_BOOT:
        added.append(Added(value=_root_value(config), because=ROOT))
        added.append(Added(value="rw", because=WRITABLE))
        if _rootflags(config):
            added.append(
                Added(
                    value=f"rootflags=subvol={_rootflags(config)}",
                    because=SUBVOLUME,
                    source=_rootflags(config),
                )
            )
    elif kind is Bootloader.GRUB:
        added.append(
            Added(value=_root_value(config), because=ROOT_FROM_GRUB, written_here=False)
        )
    else:
        added.append(
            Added(value=_root_value(config), because=ROOT_FROM_ZBM, written_here=False)
        )
    if kind is not Bootloader.ZFSBOOTMENU:
        containers, arrays = initramfs_devices(config)
        added += [Added(value="rd.luks.uuid=…", because=CONTAINER) for _ in containers]
        added += [Added(value="rd.md.uuid=…", because=ARRAY) for _ in arrays]
        keymap = initramfs_keymap(config)
        if keymap:
            added.append(
                Added(
                    value=f"rd.vconsole.keymap={keymap}", because=KEYMAP, source=keymap
                )
            )
    for parameter in unlock_parameters(config):
        added.append(Added(value=parameter, because=UNLOCK))
    return tuple(added)


def use_flags(config: InstallConfig, catalog: Catalog) -> tuple[Added, ...]:
    """The `USE` a chosen group asks for, attributed to the group that asks.

    `required_use` is what reaches `make.conf`; this walks the same groups in
    the same order so the two cannot disagree.
    """
    added: list[Added] = []
    seen = set(config.portage.use)
    for group in groups(config, catalog):
        for flag in group.use:
            if flag in seen:
                continue
            seen.add(flag)
            added.append(
                Added(value=flag, because=GROUP_USE, source=group.name)
            )
    return tuple(added)


def video_cards(config: InstallConfig, catalog: Catalog) -> tuple[Added, ...]:
    """`VIDEO_CARDS`, which follows the graphics choice rather than a list of
    its own. Shown for the same reason as the rest: it is written for you."""
    added: list[Added] = []
    seen = set(config.portage.video_cards)
    for group in groups(config, catalog):
        for card in group.video_cards:
            if card in seen:
                continue
            seen.add(card)
            added.append(
                Added(value=card, because=GROUP_CARD, source=group.name)
            )
    return tuple(added)


def _root_value(config: InstallConfig) -> str:
    """`root=`, in the form the entry will carry. A dataset names itself, so
    that one is exact; a device is only a UUID once it has been formatted."""
    graph = config.disk.graph
    mount = graph[config.disk.root]
    source = graph[mount.source] if isinstance(mount, Mountpoint) else mount
    if isinstance(source, ZfsDataset):
        pool = graph[source.pool]
        name = pool.name if isinstance(pool, ZfsPool) else ""
        return f"root=ZFS={name}/{source.name}"
    return "root=UUID=…"


def _rootflags(config: InstallConfig) -> str:
    graph = config.disk.graph
    mount = graph[config.disk.root]
    source = graph[mount.source] if isinstance(mount, Mountpoint) else mount
    return source.name if isinstance(source, Subvolume) else ""
