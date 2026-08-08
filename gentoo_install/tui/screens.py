"""One screen per decision, each a function of the configuration so far.

A screen never mutates what it was given: it returns a new `InstallConfig`, and
`app.py` re-validates before moving on. Every option a compatibility rule
excludes is drawn greyed with that rule's own sentence, so the interface and the
validator never disagree about why something cannot be chosen.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import takewhile
from typing import Callable, Final, Sequence, TypeVar

from ..i18n import Catalog
from ..model import compat
from ..model.config import (
    ConsoleFontSize,
    Binhost,
    BinhostChannel,
    Bootloader,
    BootloaderConfig,
    DiskConfig,
    Firmware,
    GentooZhMirror,
    InitSystem,
    InstallConfig,
    KernelSource,
    Keywords,
    Logger,
    MirrorConfig,
    MirrorRegion,
    Networking,
    Overlay,
    PackagesConfig,
    PortageConfig,
    Sync,
    User,
)
from ..model.device import FilesystemType, PartitionRole, TableType, ZfsPool
from ..plan.kernel import KERNEL_PACKAGES
from ..plan.portage import community_binhost
from ..model.size import Size
from ..errors import GentooInstallError, ValidationFailed
from ..model import atoms, manual, mirrors, paste, sshkey
from ..model.templates import Choice, Layout, build
from ..model.validate import validate
from ..plan.packages import Catalog as Groups
from ..plan import system as plan_system
from .widgets import (
    Answer,
    Confirm,
    Field,
    Form,
    Item,
    Menu,
    Outcome,
    Screen,
    TextField,
)

#: A screen takes what has been decided and returns it changed.
Step = Callable[[Screen, InstallConfig, "Context"], Answer[InstallConfig]]



class Context:
    """What the screens need besides the configuration itself."""

    def __init__(
        self,
        translate: Catalog,
        disks: Sequence[tuple[str, str]],
        groups: Groups,
        hash_password: Callable[[str], str],
        stage_passphrase: Callable[[str], str] = lambda text: "",
        timezones: Sequence[str] = (),
        firmware: Firmware = Firmware.UEFI,
        cores: int = 1,
        cpu_flags: Sequence[str] = (),
        supports_v3: bool = False,
        inspect_disk: Callable[[str], tuple[tuple[tuple[str, str, str], ...], str]] = (
            lambda disk: ((), "")
        ),
        fetch_text: Callable[[str], str] = lambda url: "",
        kernel_versions: Callable[[str], tuple[tuple[str, bool], ...]] = lambda atom: (),
        keymaps: Callable[[], tuple[tuple[str, str], ...]] = lambda: (),
        zfs_kernel_max: str = "",
    ) -> None:
        self.translate = translate
        #: Selector and a human description, from `exec/probe.py`.
        self.disks = disks
        self.groups = groups
        #: Injected rather than imported: the model layer does no I/O, and
        #: hashing runs `openssl` on the installing system.
        self.hash_password = hash_password
        #: Writes a passphrase into the run's work directory and returns its
        #: path. Injected because the model layer does no I/O.
        self.stage_passphrase = stage_passphrase
        #: Reads a short document over the network, for a key someone pasted
        #: somewhere. Injected because this layer opens no connection.
        self.fetch_text = fetch_text
        #: Versions of a kernel package this machine can see, newest first,
        #: each with whether it is stable on amd64. Empty on a medium with no
        #: repository, which is when the version is typed instead.
        self.kernel_versions = kernel_versions
        #: Every console keymap the machine ships, as (family, name). Empty on
        #: a medium with no keymap tree, which is when the name is typed.
        self.keymaps = keymaps
        #: The highest kernel `sys-fs/zfs` builds a module for, read from its
        #: ebuild. Empty when no repository is visible, which offers every version.
        self.zfs_kernel_max = zfs_kernel_max
        #: Every zone the machine knows, from `exec/probe.py`.
        self.timezones = tuple(timezones)
        #: How this machine booted. The install defaults to the same, because
        #: installing for the other is almost always a mistake.
        self.firmware = firmware
        #: Kept so a disk screen can rebuild the graph when one answer changes,
        #: rather than editing a graph it did not build.
        self.choice = Choice(disk=disks[0][0] if disks else "", firmware=firmware)
        #: Erasing a drive is confirmed by typing its name, and the menu shows
        #: whether that has been done rather than asking again at the end.
        self.erase_confirmed = False
        #: Keys of the rows the operator has opened. An optional row never
        #: opened is running on a default nobody chose, which the menu says in
        #: colour as well as in the value it shows.
        self.visited: set[str] = set()
        #: The hand-written partition table, when the layout is manual.
        self.layout = manual.Layout()
        #: Whether the disk comes from that table rather than a template.
        self.manual = False
        #: What the chosen disk holds now, and how big it is. Both come from
        #: `exec/probe.py` and are shown before anything is erased.
        self.existing: tuple[tuple[str, str, str], ...] = ()
        self.disk_size = ""
        #: The catalog's tag, so the language screen can preselect it.
        self.tag = translate.tag
        #: This machine's core count and instruction set, for the rows that
        #: recommend a value rather than asking for one blind.
        self.cores = cores
        self.cpu_flags = tuple(cpu_flags)
        #: Whether `ld.so` says this CPU runs x86-64-v3 binaries.
        self.supports_v3 = supports_v3
        self._inspect = inspect_disk
        if self.choice.disk:
            self.inspect_disk(self.choice.disk)

    def inspect_disk(self, disk: str) -> None:
        self.existing, self.disk_size = self._inspect(disk)


def answers(translate: Catalog) -> dict[str, str]:
    """The yes and no a `Confirm` shows. Every one of them reads them from the
    catalog, so a translated interface does not answer in English."""
    return {"no": translate("No"), "yes": translate("Yes")}


def footer(translate: Catalog) -> str:
    return "  ".join(
        (
            f"[enter] {translate('Continue')}",
            f"[backspace] {translate('Back')}",
            f"[q] {translate('Cancel')}",
        )
    )


def _rebuild(config: InstallConfig, context: Context) -> InstallConfig:
    """The disk graph, from whichever description the operator is editing.

    Through the template only when that is what the layout row chose. A screen
    that rebuilt from the template regardless replaced a hand-written or reused
    table with a whole-disk graph carrying `wipe`, which is the operator's data
    destroyed by opening an unrelated row.
    """
    graph, root = manual.build(context.layout) if context.manual else build(context.choice)
    return replace(config, disk=DiskConfig(graph=graph, root=root))


def disk_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    if not context.disks:
        raise LookupError("no disk to install onto")
    menu: Menu[str] = Menu(
        title=translate("Disks"),
        items=[Item(label=name, value=name, detail=detail) for name, detail in context.disks],
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    picked = answer.unwrap()[0]
    context.choice = replace(context.choice, disk=picked)
    # `_rebuild` reads the layout rather than the choice when the table was
    # hand-written, so leaving this behind partitioned the disk the operator
    # switched away from. The kept rows name partitions of that disk and go too.
    context.layout.disk = picked
    context.layout.reused = []
    # Cleared with the disk: the operator typed the name of the one they were
    # looking at, and carrying that confirmation to another unblocks the
    # install for a disk nobody agreed to erase.
    context.erase_confirmed = False
    context.inspect_disk(picked)
    return Answer(Outcome.CHOSE, _rebuild(config, context))


def layout_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    items: list[Item[tuple[Layout | None, FilesystemType]]] = [
        Item(label="ext4", value=(Layout.WHOLE_DISK, FilesystemType.EXT4)),
        Item(label="xfs", value=(Layout.WHOLE_DISK, FilesystemType.XFS)),
        Item(
            label="btrfs",
            value=(Layout.WHOLE_DISK_BTRFS, FilesystemType.BTRFS),
            detail=translate("with the @ and @home subvolumes"),
        ),
        Item(
            label="zfs",
            value=(Layout.WHOLE_DISK_ZFS, FilesystemType.EXT4),
            detail=translate("with ZFSBootMenu"),
        ),
        Item(
            label=translate("manual"),
            value=(None, FilesystemType.EXT4),
            detail=translate("choose the partitions yourself"),
        ),
        Item(
            label=translate("reuse"),
            value=(Layout.REUSE, FilesystemType.EXT4),
            detail=translate("keep the partitions already on the disk"),
        ),
    ]
    menu: Menu[tuple[Layout | None, FilesystemType]] = Menu(
        title=translate("Layout"), items=items, footer=footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    layout, filesystem = answer.unwrap()[0]
    context.manual = False
    if layout is Layout.REUSE:
        return reuse_screen(screen, config, context)
    if layout is None:
        context.layout.reused = []
        return partitions_screen(screen, config, context)
    context.manual = False
    context.choice = replace(context.choice, layout=layout, filesystem=filesystem)
    changed = _rebuild(config, context)
    if layout is Layout.WHOLE_DISK_ZFS:
        changed = _zfs_bootloader(screen, changed, context)
    return Answer(Outcome.CHOSE, changed)


def reuse_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """The partitions already on the disk, each given a mount point.

    Nothing is partitioned, so every partition the operator leaves alone keeps
    its data. Formatting one is a choice per row rather than a mode.
    """
    translate = context.translate
    if not context.existing:
        _say(screen, context, translate("This disk holds no partitions."))
        return Answer(Outcome.BACK)
    if [one.selector for one in context.layout.reused] != [
        name for name, _, _ in context.existing
    ]:
        context.layout.reused = [
            manual.Reused(selector=name, filesystem=_known(kind))
            for name, _, kind in context.existing
        ]
    cursor = 0
    while True:
        rows: list[Item[int]] = [
            Item(label=one.describe(), value=index, detail=size)
            for index, (one, size) in enumerate(
                zip(context.layout.reused, [size for _, size, _ in context.existing])
            )
        ]
        rows.append(Item(label=translate("Done"), value=len(context.layout.reused)))
        menu: Menu[int] = Menu(
            title=f"{translate('Reuse partitions')}  {_layout_problem(context, config)}".strip(),
            items=rows,
            footer=footer(translate),
            cursor=cursor,
        )
        answer = menu.run(screen)
        cursor = menu.cursor
        if not answer.chosen:
            return Answer(answer.outcome)
        chosen = answer.unwrap()[0]
        if chosen == len(context.layout.reused):
            context.manual = True
            graph, root = manual.build(context.layout)
            return Answer(Outcome.CHOSE, replace(config, disk=DiskConfig(graph=graph, root=root)))
        edited = _edit_reused(screen, context, context.layout.reused[chosen])
        if edited is not None:
            context.layout.reused[chosen] = edited


def _known(kind: str) -> FilesystemType | None:
    """The type blkid reported, when the model has a member for it. ntfs and
    exfat are mounted and never created, so they have no member and no row."""
    return next((one for one in FilesystemType if one.value == kind), None)


def _edit_reused(
    screen: Screen, context: Context, entry: manual.Reused
) -> manual.Reused | None:
    translate = context.translate
    cursor = 0
    while True:
        rows: list[Item[str]] = [
            Item(
                label=translate("Mount point"),
                value=_MOUNTPOINT,
                detail=entry.mountpoint or translate("not mounted"),
                # A type with no `FilesystemType` member, ntfs and exfat among
                # them, has no fstab line to write, and taking a mount point
                # for one dropped it without saying so.
                disabled_because=(
                    "" if entry.filesystem else translate("name the filesystem first")
                ),
            ),
            Item(
                label=translate("Filesystem"),
                value=_FILESYSTEM,
                detail=entry.filesystem.value if entry.filesystem else translate("unknown"),
            ),
            Item(
                label=translate("Format it"),
                value=_FORMAT,
                detail=translate("yes") if entry.format else translate("no, keep the data"),
            ),
            Item(label=translate("Done"), value=_DONE),
        ]
        menu: Menu[str] = Menu(
            title=entry.selector, items=rows, footer=footer(translate), cursor=cursor
        )
        answer = menu.run(screen)
        cursor = menu.cursor
        if not answer.chosen:
            return None
        field = answer.unwrap()[0]
        if field == _DONE:
            return entry
        if field == _MOUNTPOINT:
            where = TextField(
                title=translate("Mount point"),
                value=entry.mountpoint,
                placeholder=translate("/srv, or empty to leave it unmounted"),
                footer=footer(translate),
            ).run(screen)
            if where.chosen:
                entry = replace(entry, mountpoint=where.unwrap().strip())
        elif field == _FILESYSTEM:
            picked = Menu(
                title=translate("Filesystem"),
                items=[Item(label=one.value, value=one) for one in FilesystemType],
                footer=footer(translate),
            ).run(screen)
            if picked.chosen:
                entry = replace(entry, filesystem=picked.unwrap()[0])
        else:
            asked = Confirm(
                **answers(translate),
                title=translate("Format it, losing what is on it?"), footer=footer(translate)
            ).run(screen)
            if asked.chosen:
                entry = replace(entry, format=asked.unwrap())


def _zfs_bootloader(screen: Screen, config: InstallConfig, context: Context) -> InstallConfig:
    """A ZFS root cannot use GRUB, so this asks which of the two that remain.

    ZFSBootMenu lives in the gentoo-zh overlay and in no other repository, so
    choosing it is also consenting to that overlay. Adding it silently is what
    this replaces.
    """
    translate = context.translate
    answer = Menu(
        title=translate("A ZFS root cannot boot from GRUB. Which bootloader?"),
        items=[
            Item(
                label="ZFSBootMenu",
                value=Bootloader.ZFSBOOTMENU,
                detail=translate("adds the gentoo-zh overlay, the only one that has it"),
            ),
            Item(
                label="systemd-boot",
                value=Bootloader.SYSTEMD_BOOT,
                detail=translate("no overlay, and the esp has to hold the kernel"),
            ),
        ],
        footer=footer(translate),
    ).run(screen)
    if not answer.chosen:
        return config
    kind = answer.unwrap()[0]
    if kind is Bootloader.SYSTEMD_BOOT:
        return replace(config, bootloader=replace(config.bootloader, kind=kind))
    return replace(
        config,
        bootloader=replace(config.bootloader, kind=kind),
        portage=_with_gentoo_zh(config),
    )


def _with_gentoo_zh(config: InstallConfig) -> PortageConfig:
    """The overlay, cloned from the site already chosen for it.

    Read from `model/mirrors.py` and not written here: a literal beside that
    table is a second address to update, and the overlay has moved once
    already. A site the operator has not picked yet answers as upstream.
    """
    if any(overlay.name == "gentoo-zh" for overlay in config.portage.overlays):
        return config.portage
    where = mirrors.gentoozh(config.portage.mirrors.gentoo_zh).git
    added = (*config.portage.overlays, Overlay(name="gentoo-zh", sync_uri=where))
    binhost = config.portage.binhost
    if binhost.community is BinhostChannel.OFF:
        # On with the overlay: the host serves what that overlay builds, and
        # `compat.py` is what keeps the two from being set apart.
        binhost = replace(binhost, community=BinhostChannel.STABLE)
    return replace(config.portage, overlays=added, binhost=binhost)


def erase_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """The one screen with no default: the disk name has to be typed."""
    translate = context.translate
    disk = context.choice.disk
    while True:
        # The selector goes in the field rather than the title: together they
        # are three sentences and a `/dev/disk/by-id/` path, and 80 columns
        # truncated away the one saying what to type.
        question = Confirm(
            **answers(translate),
            title=f"{translate('This erases every partition on the disk.')} "
            f"{translate('Type the disk name to confirm.')}",
            phrase=disk,
            placeholder=disk,
            footer=footer(translate),
        )
        answer = question.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        if answer.unwrap():
            context.erase_confirmed = True
            return Answer(Outcome.CHOSE, config)
        # Said rather than swallowed: a trailing space read as a refusal, and
        # the row went back to unset with nothing explaining why.
        _say(screen, context, translate("That is not the name of this disk."))


def system_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    field = TextField(
        title=translate("Hostname"),
        value=config.system.hostname,
        footer=footer(translate),
    )
    answer = field.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, hostname=answer.unwrap() or "gentoo")),
    )


def init_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    menu: Menu[InitSystem] = Menu(
        title=translate("Init system"),
        items=[Item(label=init.value, value=init) for init in InitSystem],
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    init = answer.unwrap()[0]
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            system=replace(config.system, init=init),
            portage=replace(config.portage, profile=_profile_for(config.portage.profile, init)),
        ),
    )


def _profile_was_chosen(config: InstallConfig, groups: Groups) -> bool:
    """Whether the profile is something other than what the current desktop and
    init imply, which is the only evidence the operator set it by hand."""
    implied = desktop_profiles(groups).get(config.packages.desktop)
    if implied is None:
        return True
    return config.portage.profile != _profile_for(implied, config.system.init)


def _profile_for(profile: str, init: InitSystem) -> str:
    """The profile has to follow the init: a systemd profile has `systemd` as a
    path component, and the two disagreeing builds packages for the other."""
    parts = [part for part in profile.split("/") if part != "systemd"]
    if init is InitSystem.SYSTEMD:
        parts.append("systemd")
    return "/".join(parts)


def root_password_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """The hash goes into the configuration; the plaintext never does."""
    translate = context.translate
    typed = _ask_password(screen, context, translate("Root password"))
    if typed is None:
        return Answer(Outcome.BACK)
    hashed = context.hash_password(typed)
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, root_password_hash=hashed))
    )


def user_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """One normal account with sudo, or none.

    An empty name leaves the system with root only, which is a choice a server
    install makes deliberately.
    """
    translate = context.translate
    named = TextField(
        title=translate("User name, or empty for root only"), footer=footer(translate)
    ).run(screen)
    if not named.chosen:
        return Answer(named.outcome)
    name = named.unwrap().strip()
    if not name:
        return Answer(Outcome.CHOSE, replace(config, system=replace(config.system, users=())))
    # One string with the name in it, not a fragment plus the name: the relation
    # the label exists to state does not survive concatenation in a language
    # that puts the possessive first.
    password = _ask_password(screen, context, translate("Password for {user}").format(user=name))
    if password is None:
        return Answer(Outcome.BACK)
    granted = Confirm(
        **answers(translate),
        title=translate("Give this account sudo?"), footer=footer(translate)
    ).run(screen)
    if not granted.chosen:
        return Answer(granted.outcome)
    user = User(
        name=name,
        # No list here: `plan/system.py:USER_GROUPS` is the one table, and
        # naming `wheel` again put a account that declined sudo back in it.
        sudo=granted.unwrap(),
        password_hash=context.hash_password(password),
    )
    return Answer(Outcome.CHOSE, replace(config, system=replace(config.system, users=(user,))))


#: One row of the mirror screen. The Gentoo rows come first and the overlay
#: rows after, because a row of one repository between two rows of the other
#: reads as though it belonged to it.
_REGION: Final[str] = "region"
_SITE: Final[str] = "site"
_MEASURE: Final[str] = "measure"
_DISTFILES: Final[str] = "distfiles"
_SYNC: Final[str] = "sync"
_BINHOST: Final[str] = "binhost"
_ZH_BINHOST: Final[str] = "zh-binhost"
_ZH_SITE: Final[str] = "zh-site"
_ZH_DISTFILES: Final[str] = "zh-distfiles"
_DONE: Final[str] = "done"

#: How the tree is kept up to date, and what each costs.
SYNC_METHODS: tuple[tuple[Sync, str], ...] = (
    (Sync.GIT, "carries the history a signed sync checks"),
    (Sync.RSYNC, "what emerge --sync has always done"),
    (Sync.WEBRSYNC, "no git, and no history"),
)

#: The gentoo-zh binary package channels.
GENTOOZH_CHANNELS: tuple[tuple[BinhostChannel, str], ...] = (
    (BinhostChannel.OFF, "not used"),
    (BinhostChannel.STABLE, "stable ::gentoo, ~amd64 for the overlay"),
    (BinhostChannel.UNSTABLE, "~amd64 throughout, so fewer packages match a binary host"),
)

#: Only x86-64 and x86-64-v3 carry a useful number of official binary packages;
#: the other subarchitectures are nearly empty.
BINHOSTS: tuple[tuple[tuple[bool, str], str], ...] = (
    ((False, "x86-64"), "hours rather than minutes"),
    ((True, "x86-64"), "works on every amd64 machine"),
    ((True, "x86-64-v3"), "this CPU runs it, and the packages are built for it"),
)

#: The overlays with no mirror of their own, so they are on or off and nothing
#: else. guru publishes from one place only.
PLAIN_OVERLAYS: tuple[tuple[str, str], ...] = (
    ("gig", "https://github.com/gentoo-zh/gig-overlay.git"),
    ("guru", "https://anongit.gentoo.org/git/repo/proj/guru.git"),
)


def mirror_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Where every repository comes from, and which of them are used at all.

    One screen because they are one decision: an overlay selected with no
    mirror behind it, or a mirror chosen for an overlay that is not selected,
    are both states the operator can reach by editing two rows apart.
    """
    translate = context.translate
    current = config
    cursor = 0
    while True:
        menu: Menu[str] = Menu(
            title=translate("Mirrors"),
            items=_mirror_fields(current, translate),
            footer=footer(translate),
            cursor=cursor,
        )
        answer = menu.run(screen)
        cursor = menu.cursor
        if not answer.chosen:
            return Answer(answer.outcome)
        field = answer.unwrap()[0]
        if field == _DONE:
            return Answer(Outcome.CHOSE, current)
        changed = _edit_mirror(screen, context, current, field)
        if changed is not None:
            current = changed


def _mirror_fields(config: InstallConfig, translate: Catalog) -> list[Item[str]]:
    portage = config.portage
    chosen = portage.mirrors
    region = chosen.region
    site = chosen.site or mirrors.gentoo_sites(region)[0].key
    used = {overlay.name for overlay in portage.overlays}
    zh = mirrors.gentoozh(chosen.gentoo_zh)
    rows = [
        Item(label=translate("Region"), value=_REGION, detail=region.value),
        Item(
            label=translate("Gentoo mirror"),
            value=_SITE,
            # Unset until it is picked, the same as the row that opens this
            # screen: filling the blank in with the region's first site made
            # the two disagree about whether the question was answered.
            detail=translate(next(one.name for one in mirrors.gentoo_sites(region) if one.key == site))
            if chosen.site
            else translate("not set"),
        ),
        Item(
            label=translate("Gentoo distfiles"),
            value=_DISTFILES,
            detail=translate("in use") if chosen.gentoo_distfiles else translate("not used"),
        ),
        Item(
            label=translate("Measure them"),
            value=_MEASURE,
            detail=translate("yes") if chosen.speed_test else translate("no"),
        ),
        Item(label=translate("Repository sync"), value=_SYNC, detail=portage.sync.value),
        # One row, not one per method: showing a git address under a webrsync
        # choice is the interface contradicting itself.
        Item(
            label=translate("Gentoo tree from"),
            value="",
            detail=_tree_source(portage, region, site, translate),
        ),
        Item(
            label=translate("Gentoo binary packages"),
            value=_BINHOST,
            detail=mirrors.gentoo_binhost(region, site, portage.binhost.subarch)
            if portage.binhost.official
            else translate("not used"),
        ),
        Item(
            label=translate("gentoo-zh"),
            value=_ZH_SITE,
            detail=translate(zh.name) if "gentoo-zh" in used else translate("not used"),
        ),
    ]
    if "gentoo-zh" in used:
        rows += [
            Item(
                label=translate("gentoo-zh distfiles"),
                value=_ZH_DISTFILES,
                # The first of the list, which is the one Portage tries first:
                # all five spelled out do not fit an 80-column row.
                detail=mirrors.gentoozh_distfiles(chosen.gentoo_zh)[0]
                if chosen.gentoo_zh_distfiles
                else translate("not used"),
            ),
            Item(
                label=translate("gentoo-zh binary packages"),
                value=_ZH_BINHOST,
                # The plan's own function, so the row cannot name one path
                # while the install writes another: global `~amd64` forces the
                # unstable one whatever this row was set to.
                detail=community_binhost(portage)
                if portage.binhost.community is not BinhostChannel.OFF
                else translate("not used"),
            ),
        ]
    rows += [
        Item(
            label=name,
            value=name,
            detail=translate("in use") if name in used else translate("not used"),
        )
        for name, _ in PLAIN_OVERLAYS
    ]
    rows.append(Item(label=translate("Done"), value=_DONE))
    return rows


def _tree_source(
    portage: PortageConfig, region: MirrorRegion, site: str, translate: Catalog
) -> str:
    """Where the chosen sync method will actually read the tree from."""
    if portage.sync is Sync.GIT:
        return mirrors.gentoo_sync_uri(region, site)
    if portage.sync is Sync.RSYNC:
        return mirrors.gentoo_rsync_uri(region, site)
    return translate("a snapshot from GENTOO_MIRRORS")


def _edit_mirror(
    screen: Screen, context: Context, config: InstallConfig, field: str
) -> InstallConfig | None:
    """The one screen behind a field, or None when nothing changed."""
    translate = context.translate
    portage = config.portage
    current = portage.mirrors
    if not field:
        # A row that only reports where a service will come from, derived from
        # the two choices above it.
        return None
    if field == _REGION:
        picked = Menu(
            title=translate("Region"),
            items=[Item(label=one.value, value=one) for one in MirrorRegion],
            footer=footer(translate),
        ).run(screen)
        if not picked.chosen:
            return None
        # The site belongs to the region, so changing one clears the other.
        mirrored = replace(current, region=picked.unwrap()[0], site="")
        return replace(config, portage=replace(portage, mirrors=mirrored))
    if field == _SITE:
        offered = mirrors.gentoo_sites(current.region)
        chosen = Menu(
            title=translate("Gentoo mirror"),
            items=[
                Item(
                    label=translate(one.name),
                    value=one.key,
                    detail=f"{translate(one.area)}  {one.distfiles}",
                )
                for one in offered
            ],
            footer=footer(translate),
        ).run(screen)
        if not chosen.chosen:
            return None
        return replace(
            config, portage=replace(portage, mirrors=replace(current, site=chosen.unwrap()[0]))
        )
    if field == _DISTFILES:
        return replace(
            config,
            portage=replace(
                portage, mirrors=replace(current, gentoo_distfiles=not current.gentoo_distfiles)
            ),
        )
    if field == _MEASURE:
        return replace(
            config,
            portage=replace(portage, mirrors=replace(current, speed_test=not current.speed_test)),
        )
    if field == _SYNC:
        return _pick(
            screen, context, config, "Repository sync",
            list(SYNC_METHODS),
            lambda chosen_config, value: replace(
                chosen_config, portage=replace(chosen_config.portage, sync=value)
            ),
        )
    if field == _BINHOST:
        return _edit_binhost(screen, context, config)
    if field == _ZH_BINHOST:
        return _pick(
            screen, context, config, "gentoo-zh binary packages",
            list(GENTOOZH_CHANNELS),
            lambda chosen_config, value: replace(
                chosen_config,
                portage=replace(
                    chosen_config.portage,
                    binhost=replace(chosen_config.portage.binhost, community=value),
                ),
            ),
        )
    if field == _ZH_SITE:
        return _edit_gentoozh(screen, context, config)
    if field == _ZH_DISTFILES:
        return replace(
            config,
            portage=replace(
                portage,
                mirrors=replace(current, gentoo_zh_distfiles=not current.gentoo_zh_distfiles),
            ),
        )
    return _toggle_overlay(config, field)


V = TypeVar("V")


def _pick(
    screen: Screen,
    context: Context,
    config: InstallConfig,
    title: str,
    offered: list[tuple[V, str]],
    apply: Callable[[InstallConfig, V], InstallConfig],
) -> InstallConfig | None:
    """One value from a short list, each row carrying what it costs."""
    translate = context.translate
    menu: Menu[V] = Menu(
        title=translate(title),
        items=[
            Item(label=str(getattr(value, "value", value)), value=value, detail=translate(reason))
            for value, reason in offered
        ],
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return None
    return apply(config, answer.unwrap()[0])


def _edit_binhost(
    screen: Screen, context: Context, config: InstallConfig
) -> InstallConfig | None:
    """Off, or which subarchitecture. `ld.so` lists what this CPU would search,
    so a machine that cannot run v3 is told rather than left to meet an illegal
    instruction in the first binary package."""
    translate = context.translate
    items: list[Item[tuple[bool, str]]] = [
        Item(
            label=subarch if official else translate("not used"),
            value=(official, subarch),
            detail=translate(reason) if subarch != "x86-64-v3" or context.supports_v3 else "",
            disabled_because=(
                translate("this CPU cannot run it")
                if subarch.endswith("v3") and not context.supports_v3
                else ""
            ),
        )
        for (official, subarch), reason in BINHOSTS
    ]
    menu: Menu[tuple[bool, str]] = Menu(
        title=translate("Gentoo binary packages"), items=items, footer=footer(translate)
    )
    if context.supports_v3:
        # Recommended by starting on it: it is the faster of the two and this
        # machine has already proved it can run it.
        menu.cursor = 2
    answer = menu.run(screen)
    if not answer.chosen:
        return None
    official, subarch = answer.unwrap()[0]
    portage = config.portage
    return replace(
        config,
        portage=replace(
            portage, binhost=replace(portage.binhost, official=official, subarch=subarch)
        ),
    )


def _edit_gentoozh(
    screen: Screen, context: Context, config: InstallConfig
) -> InstallConfig | None:
    """Whether gentoo-zh is used and where from, which is one question: a
    mirror chosen for an overlay nobody selected changes nothing."""
    translate = context.translate
    items: list[Item[GentooZhMirror | None]] = [Item(label=translate("not used"), value=None)]
    items += [
        Item(
            label=translate(one.name),
            value=GentooZhMirror(one.key),
            # The git address, not the distfiles one: this choice writes
            # `sync-uri`, and upstream serves the two from different hosts.
            detail=f"{translate(one.area)}  {one.git}",
        )
        for one in mirrors.GENTOOZH_SITES
    ]
    answer = Menu(
        title=translate("gentoo-zh"), items=items, footer=footer(translate)
    ).run(screen)
    if not answer.chosen:
        return None
    picked = answer.unwrap()[0]
    portage = config.portage
    if picked is None:
        # What required the overlay goes with it: leaving the community binhost
        # on refuses the install from a row the operator is not looking at.
        kept = tuple(one for one in portage.overlays if one.name != "gentoo-zh")
        return replace(
            config,
            portage=replace(
                portage,
                overlays=kept,
                binhost=replace(portage.binhost, community=BinhostChannel.OFF),
            ),
        )
    # The overlay is cloned from the chosen site, not from upstream: a mirror
    # picked here and ignored by the sync is the choice doing nothing.
    added = _with_gentoo_zh(config)
    overlays = tuple(
        replace(one, sync_uri=mirrors.gentoozh(picked).git) if one.name == "gentoo-zh" else one
        for one in added.overlays
    )
    return replace(
        config,
        portage=replace(
            added, overlays=overlays, mirrors=replace(portage.mirrors, gentoo_zh=picked)
        ),
    )


def _toggle_overlay(config: InstallConfig, name: str) -> InstallConfig:
    """Flipped where it stands. A yes/no screen over a row that already reads
    `in use` or `not used` asks the question the row just answered."""
    portage = config.portage
    kept = tuple(one for one in portage.overlays if one.name != name)
    if len(kept) == len(portage.overlays):
        uri = next(where for offered, where in PLAIN_OVERLAYS if offered == name)
        kept = (*kept, Overlay(name=name, sync_uri=uri))
    return replace(config, portage=replace(portage, overlays=kept))


def bootloader_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Every excluded bootloader is drawn with the rule's own sentence."""
    translate = context.translate
    items: list[Item[Bootloader]] = []
    for kind in Bootloader:
        candidate = replace(config, bootloader=replace(config.bootloader, kind=kind))
        broken = compat.violations(candidate)
        items.append(
            Item(label=kind.value, value=kind, disabled_because=broken[0].reason if broken else "")
        )
    menu: Menu[Bootloader] = Menu(
        title=translate("Bootloader"), items=items, footer=footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, bootloader=replace(config.bootloader, kind=answer.unwrap()[0])),
    )


#: What each kernel choice costs and what it gives. All three are dist-kernels:
#: the package builds and installs itself, so none of them is a source tree the
#: installer would have to configure.
KERNELS: tuple[tuple[KernelSource, str], ...] = (
    (KernelSource.DIST_BIN, "prebuilt"),
    (KernelSource.DIST_SOURCE, "built here"),
    (KernelSource.CJK, "built here, cjktty for CJK on the console, from gentoo-zh"),
)


def _within(
    offered: tuple[tuple[str, bool], ...], ceiling: str
) -> tuple[tuple[str, bool], ...]:
    """Kernel versions a ZFS root can actually boot.

    A version above `MODULES_KERNEL_MAX` leaves `sys-fs/zfs` with no module and
    the pool unmountable, so it is dropped rather than offered with a warning.
    """
    if not ceiling:
        return offered
    limit = _numeric(ceiling)
    return tuple((version, stable) for version, stable in offered if _numeric(version) <= limit)


def _numeric(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        digits = "".join(takewhile(str.isdigit, piece))
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def kernel_version_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Which version, read from whatever repository this machine can see.

    The list moves every week, so it is read rather than held here. A medium
    that ships no repository has nothing to list, and the version is typed
    instead; either way a testing version is accepted for that atom alone.
    """
    translate = context.translate
    package = config.kernel.package or KERNEL_PACKAGES[config.kernel.source]
    ceiling = context.zfs_kernel_max if config.disk.graph.of_type(ZfsPool) else ""
    offered = _within(context.kernel_versions(package), ceiling)
    if not offered:
        typed = TextField(
            title=f"{package}  {translate('version')}",
            value=config.kernel.version,
            placeholder=translate("empty to leave the version to the keywords"),
            footer=footer(translate),
        ).run(screen)
        if not typed.chosen:
            return Answer(typed.outcome)
        return Answer(
            Outcome.CHOSE, replace(config, kernel=replace(config.kernel, version=typed.unwrap().strip()))
        )
    # No unpinned row under a ceiling: `MODULES_KERNEL_MAX` only warns, so
    # nothing stops the resolver picking a kernel `sys-fs/zfs` will not build
    # against, and the failure lands after the disks are written.
    items: list[Item[str]] = [] if ceiling else [
        Item(
            label=translate("not pinned"),
            value="",
            detail=translate("whatever the keywords allow at install time"),
        )
    ]
    items += [
        Item(
            label=version,
            value=version,
            detail="amd64" if stable else translate("~amd64, accepted for this atom"),
        )
        for version, stable in offered
    ]
    title = package
    if ceiling:
        title = f"{package}  {translate('sys-fs/zfs module ceiling')} {ceiling}"
    menu: Menu[str] = Menu(title=title, items=items, footer=footer(translate))
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, kernel=replace(config.kernel, version=answer.unwrap()[0]))
    )


def logger_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Which system logger, which only openrc needs.

    A stage3 has none, so an openrc install without this keeps no log of its
    own boot. systemd carries journald and the row says so rather than
    offering a second logger for the same lines.
    """
    translate = context.translate
    if config.system.init is InitSystem.SYSTEMD:
        _say(screen, context, translate("systemd logs to journald; no other logger is needed."))
        return Answer(Outcome.BACK)
    menu: Menu[Logger] = Menu(
        title=translate("System logger"),
        items=[
            Item(label=one.value, value=one, detail=translate(choice.reason))
            for one, choice in plan_system.LOGGERS.items()
        ],
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, logger=answer.unwrap()[0]))
    )


def cron_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Flipped where it stands: the row reads `in use` or `not used` already."""
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, cron=not config.system.cron))
    )


def keywords_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """`ACCEPT_KEYWORDS` for the installed system.

    Written after everything is installed either way: opening `~amd64` before
    that drags the whole install into an unmask chain.
    """
    translate = context.translate
    menu: Menu[Keywords] = Menu(
        title=translate("Package keywords"),
        items=[
            # The keyword itself, not the enum name: `amd64` and `~amd64` are
            # what the operator will see in every Portage message afterwards.
            Item(
                label="amd64",
                value=Keywords.STABLE,
                detail=translate("the amd64 stable channel"),
            ),
            Item(
                label="~amd64",
                value=Keywords.TESTING,
                detail=translate(
                    "the ~amd64 unstable channel, so fewer packages match a binary host"
                ),
            ),
        ],
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, portage=replace(config.portage, keywords=answer.unwrap()[0])),
    )


def kernel_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    menu: Menu[KernelSource] = Menu(
        title=translate("Kernel"),
        items=[
            # The package name, not the enum value: `dist-bin` says nothing
            # about which kernel is about to be installed.
            Item(label=KERNEL_PACKAGES[source], value=source, detail=translate(reason))
            for source, reason in KERNELS
        ],
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()[0]
    changed = replace(config, kernel=replace(config.kernel, source=chosen))
    if chosen is KernelSource.CJK:
        # cjk on with it, the mirror of the branch below: `RequestCjkKernel`
        # reads this flag, so choosing the patched kernel and leaving the flag
        # off wrote `-cjk` and compiled the patch out of the package that
        # exists for it.
        changed = replace(changed, system=replace(changed.system, console_cjk=True))
        # The package is in gentoo-zh and in no other repository, so choosing
        # it is consenting to that overlay rather than having it added quietly.
        return Answer(Outcome.CHOSE, replace(changed, portage=_with_gentoo_zh(changed)))
    if config.system.console_cjk:
        # Turned off with the kernel that carried it, and said out loud: the
        # rule would otherwise refuse the install with a message about the
        # kernel, from a row that has no way to clear this.
        _say(screen, context, translate("This kernel has no cjktty: console CJK is off."))
        changed = replace(changed, system=replace(changed.system, console_cjk=False))
    return Answer(Outcome.CHOSE, changed)


#: The profile each desktop is built against. Verified against
#: profiles.desc: a systemd variant is the same path plus /systemd.
#: What a machine with no desktop is built against. Every other answer names
#: its own profile in `data/profiles/<name>.toml`.
BASE_PROFILE: Final[str] = "default/linux/amd64/23.0"


def desktop_profiles(groups: Groups) -> dict[str, str]:
    """The desktops and the profile each is built against, read from the files
    that declare them: a table beside `data/profiles/` meant a desktop added
    there never reached the menu, and one added here installed nothing."""
    found = {"": BASE_PROFILE}
    for name, group in groups.items():
        if group.profile:
            found[name] = group.profile
    return found


def desktop_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """The desktop decides the profile as well as the packages, the same way
    the init system does."""
    translate = context.translate
    detail = {
        "plasma": translate("the session only"),
        "plasma-full": translate("with the KDE application set"),
        "gnome": translate("the session only"),
        "gnome-full": translate("with the GNOME application set"),
    }
    items = [
        Item(label=name or "no desktop", value=name, detail=detail.get(name, ""))
        for name in sorted(desktop_profiles(context.groups))
    ]
    menu: Menu[str] = Menu(
        title=translate("Desktop and applications"), items=items, footer=footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    desktop = answer.unwrap()[0]
    changed = replace(config, packages=replace(config.packages, desktop=desktop))
    if not _profile_was_chosen(config, context.groups):
        # Only while the profile is still the one the last desktop implied.
        # Overwriting it regardless threw away a profile the operator had
        # picked on purpose, such as no-multilib.
        return Answer(
            Outcome.CHOSE,
            replace(
                changed,
                portage=replace(
                    config.portage,
                    profile=_profile_for(
                        desktop_profiles(context.groups)[desktop], config.system.init
                    ),
                ),
            ),
        )
    return Answer(Outcome.CHOSE, changed)


#: The graphics groups, in the order the menu lists them, and what each is
#: for. Kept out of the applications list: a driver is one choice, not a set of
#: things to tick.
GRAPHICS: tuple[tuple[str, str], ...] = (
    ("", "the in-kernel driver, which is all Intel and AMD usually need"),
    ("intel", "i915 and xe, with the firmware they need"),
    ("amdgpu", "GCN 1.2 and newer"),
    ("radeon", "AMD up to Sea Islands"),
    ("nouveau", "the in-kernel NVIDIA driver"),
    ("nvidia", "the proprietary driver, which widens ACCEPT_LICENSE"),
    ("virtual-machine", "virtio-gpu, QXL and the VMware adapter"),
)

#: The display managers. A desktop no longer names one, because which login
#: screen to run is a decision of its own.
DISPLAY_MANAGERS: tuple[tuple[str, str], ...] = (
    ("", "a text console login"),
    ("sddm", "the one Plasma expects"),
    ("gdm", "the one GNOME expects"),
    ("lightdm", "the one Xfce expects"),
    ("greetd", "a console greeter"),
)


def graphics_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Which driver, which is what VIDEO_CARDS and the firmware follow."""
    return _one_group(
        screen,
        config,
        context,
        "Graphics",
        GRAPHICS,
        lambda packages, name: replace(packages, graphics=name),
    )


def display_manager_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    return _one_group(
        screen,
        config,
        context,
        "Display manager",
        DISPLAY_MANAGERS,
        lambda packages, name: replace(packages, display_manager=name),
    )


def _one_group(
    screen: Screen,
    config: InstallConfig,
    context: Context,
    title: str,
    offered: tuple[tuple[str, str], ...],
    apply: Callable[[PackagesConfig, str], PackagesConfig],
) -> Answer[InstallConfig]:
    """A row that holds one group name, drawn from a table of them."""
    translate = context.translate
    menu: Menu[str] = Menu(
        title=translate(title),
        items=[
            Item(label=name or translate("none"), value=name, detail=translate(reason))
            for name, reason in offered
        ],
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, packages=apply(config.packages, answer.unwrap()[0]))
    )


def packages_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    translate = context.translate
    # A desktop is chosen on its own screen, because it also decides the
    # profile; this one offers what can be added beside any of them.
    # A desktop, a driver and a display manager are each one choice of their
    # own, so none of them is a row to tick here.
    elsewhere = (
        set(desktop_profiles(context.groups))
        | {name for name, _ in GRAPHICS}
        | {name for name, _ in DISPLAY_MANAGERS}
    )
    names = sorted(name for name in context.groups if name not in elsewhere)
    items = [
        Item(label=name, value=name, detail=" ".join(context.groups[name].packages))
        for name in names
    ]
    chosen_already = set(config.packages.applications)
    menu: Menu[str] = Menu(
        title=translate("Applications"),
        items=items,
        multiple=True,
        selected={index for index, item in enumerate(items) if item.value in chosen_already},
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()
    return Answer(
        Outcome.CHOSE,
        replace(config, packages=replace(config.packages, applications=tuple(chosen))),
    )


#: `zpool create` refuses anything shorter, and LUKS with a short passphrase is
#: not worth offering either.
PASSPHRASE_MINIMUM: Final[int] = 8

#: Taken from profiles.desc for amd64 23.0. A systemd profile is the same path
#: plus /systemd, which `_profile_for` relies on.
PROFILES: tuple[str, ...] = (
    "default/linux/amd64/23.0",
    "default/linux/amd64/23.0/systemd",
    "default/linux/amd64/23.0/desktop",
    "default/linux/amd64/23.0/desktop/systemd",
    "default/linux/amd64/23.0/desktop/plasma",
    "default/linux/amd64/23.0/desktop/plasma/systemd",
    "default/linux/amd64/23.0/desktop/gnome",
    "default/linux/amd64/23.0/desktop/gnome/systemd",
    "default/linux/amd64/23.0/no-multilib",
    "default/linux/amd64/23.0/no-multilib/systemd",
)

#: Offered as a list rather than free text: a mistyped locale is only found
#: when `locale -a` fails, which is after the stage3 is unpacked.
LOCALES: tuple[tuple[str, str], ...] = (
    ("zh_TW.UTF-8", "Chinese (Traditional)"),
    ("zh_CN.UTF-8", "Chinese (Simplified)"),
    ("en_US.UTF-8", "English"),
    ("ja_JP.UTF-8", "Japanese"),
    ("ko_KR.UTF-8", "Korean"),
)

#: The zones this installer is aimed at, with UTC for a server.
TIMEZONES: tuple[str, ...] = (
    "Asia/Shanghai",
    "Asia/Taipei",
    "Asia/Hong_Kong",
    "Asia/Tokyo",
    "Europe/London",
    "America/New_York",
    "UTC",
)


def locale_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    menu: Menu[str] = Menu(
        title=translate("System language"),
        items=[Item(label=f"{name}  {label}", value=name) for name, label in LOCALES],
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()[0]
    # Every offered locale is generated whichever one is selected: switching
    # afterwards then needs no regeneration.
    generated = tuple(name for name, _ in LOCALES)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, locale=chosen, locales=generated)),
    )


def timezone_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Every zone the machine knows, area first.

    Six hundred rows do not fit a console, and a hand-picked shortlist is not a
    timezone chooser.
    """
    translate = context.translate
    zones = context.timezones or TIMEZONES
    areas = []
    for zone in zones:
        area = zone.split("/", 1)[0]
        if area not in areas:
            areas.append(area)
    chosen_area: Menu[str] = Menu(
        title=translate("Timezone"),
        items=[Item(label=area, value=area) for area in areas],
        footer=footer(translate),
    )
    picked = chosen_area.run(screen)
    if not picked.chosen:
        return Answer(picked.outcome)
    area = picked.unwrap()[0]
    within = [zone for zone in zones if zone.split("/", 1)[0] == area]
    if within == [area]:
        return Answer(
            Outcome.CHOSE, replace(config, system=replace(config.system, timezone=area))
        )
    city: Menu[str] = Menu(
        title=area,
        items=[Item(label=zone.split("/", 1)[1], value=zone) for zone in within],
        footer=footer(translate),
    )
    answer = city.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, timezone=answer.unwrap()[0]))
    )


def encryption_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Whether the root filesystem is encrypted, and the passphrase if it is."""
    translate = context.translate
    if context.manual:
        # `_rebuild` builds from `context.layout` here and reads none of
        # `context.choice`, so staging a passphrase left the row reading `on`
        # over a graph with no container in it at all.
        _say(screen, context, translate("A hand-written table is encrypted per partition."))
        return Answer(Outcome.BACK)
    wanted = Confirm(
        **answers(translate),
        title=translate("Encrypt the root filesystem?"), footer=footer(translate)
    ).run(screen)
    if not wanted.chosen:
        return Answer(wanted.outcome)
    if not wanted.unwrap():
        context.choice = replace(context.choice, passphrase_file="")
        return Answer(Outcome.CHOSE, _rebuild(config, context))
    staged = _ask_passphrase(screen, context)
    if not staged:
        return Answer(Outcome.BACK)
    context.choice = replace(context.choice, passphrase_file=staged)
    return Answer(Outcome.CHOSE, _rebuild(config, context))


def _ask_passphrase(screen: Screen, context: Context) -> str:
    """The passphrase typed twice, staged in a file whose path is returned.

    Empty when the operator went back. The configuration holds the path and
    never the passphrase, because it is copied into the target and the install
    log is what people paste into bug reports.
    """
    translate = context.translate
    while True:
        first = TextField(
            title=translate("Passphrase"), masked=True, footer=footer(translate)
        ).run(screen)
        if not first.chosen:
            return ""
        typed = first.unwrap()
        if len(typed) < PASSPHRASE_MINIMUM:
            # Checked here, not at preflight: zfs refuses a short passphrase
            # only once the disks have been partitioned.
            _say(screen, context, translate("The passphrase is too short."))
            continue
        again = TextField(
            title=translate("Passphrase again"), masked=True, footer=footer(translate)
        ).run(screen)
        if not again.chosen:
            return ""
        if again.unwrap() != typed:
            _say(screen, context, translate("The two do not match."))
            continue
        return context.stage_passphrase(typed)


def _ask_password(screen: Screen, context: Context, title: str) -> str | None:
    """A password typed twice, or None when the operator went back.

    Twice for the same reason the passphrase is: the field is masked, and a
    password with a typo in it is found out at the first login of a machine
    that has already been installed.
    """
    translate = context.translate
    while True:
        first = TextField(title=title, masked=True, footer=footer(translate)).run(screen)
        if not first.chosen:
            return None
        typed = first.unwrap()
        again = TextField(
            title=translate("Type it again"), masked=True, footer=footer(translate)
        ).run(screen)
        if not again.chosen:
            return None
        if again.unwrap() == typed:
            return typed
        _say(screen, context, translate("The two do not match."))


def _say(screen: Screen, context: Context, message: str) -> None:
    """One line the operator has to acknowledge, so a rejected entry is not
    silently redrawn as an empty field."""
    Menu(
        title=message,
        items=[Item(label=context.translate("Continue"), value=0)],
        footer=footer(context.translate),
    ).run(screen)


def swap_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    items: list[Item[str]] = [
        Item(label=translate("none"), value=""),
        Item(label="4GiB", value="4GiB", detail=translate("a partition")),
        Item(label="8GiB", value="8GiB", detail=translate("a partition")),
        Item(
            label="zram 4GiB",
            value="zram:4GiB",
            detail=translate("compressed in memory, no partition"),
        ),
    ]
    menu: Menu[str] = Menu(title=translate("Swap"), items=items, footer=footer(translate))
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()[0]
    # One exclusive choice, so each row clears the other kind. Setting only its
    # own left an operator who tried both with a swap partition and zram.
    zram = Size.parse(chosen.removeprefix("zram:")) if chosen.startswith("zram:") else None
    partition = Size.parse(chosen) if chosen and not chosen.startswith("zram:") else None
    context.choice = replace(context.choice, swap=partition)
    changed = _rebuild(config, context)
    return Answer(Outcome.CHOSE, replace(changed, system=replace(changed.system, zram=zram)))


def sshd_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Off, keys only, or password login. Three rows rather than two questions:
    whether a password is accepted is the decision, not a detail of the first."""
    translate = context.translate
    menu: Menu[tuple[bool, bool]] = Menu(
        title=translate("Start an SSH server at boot?"),
        items=[
            Item(label=translate("no server"), value=(False, False)),
            Item(
                label=translate("keys only"),
                value=(True, False),
                detail=translate("no password is accepted"),
            ),
            Item(
                label=translate("password login"),
                value=(True, True),
                detail=translate("root included"),
            ),
        ],
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    running, password = answer.unwrap()[0]
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            system=replace(config.system, sshd=running, sshd_password_login=password),
        ),
    )


def overview_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Everything that is about to happen, then one confirmation.

    The list is the operation sequence itself, so the screen cannot describe
    something the installer will not do.
    """
    from ..plan.build import build as plan_build
    from ..plan.render import summarise

    translate = context.translate
    operations = plan_build(config, context.groups)
    lines = [operation.describe() for operation in operations]
    items = [Item(label=line, value=index) for index, line in enumerate(lines)]
    menu: Menu[int] = Menu(
        title=f"{translate('Overview')}: {summarise(operations)}",
        items=items,
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    question = Confirm(
        **answers(translate), title=translate("Install"), footer=footer(translate)
    )
    confirmed = question.run(screen)
    if not confirmed.chosen:
        return Answer(confirmed.outcome)
    return Answer(Outcome.CHOSE, config) if confirmed.unwrap() else Answer(Outcome.BACK)


def _profile_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Only the profiles that match the chosen init, because the validator
    refuses the other half and the operator should not be offered them."""
    wanted = [
        profile
        for profile in PROFILES
        if ("systemd" in profile.split("/")) is (config.system.init is InitSystem.SYSTEMD)
    ]
    menu: Menu[str] = Menu(
        title=context.translate("Portage"),
        items=[Item(label=profile, value=profile) for profile in wanted],
        footer=footer(context.translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, portage=replace(config.portage, profile=answer.unwrap()[0]))
    )


def table_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """GPT or MBR. UEFI needs GPT in practice, and the compatibility table
    refuses BIOS booting a GPT disk with no bios-boot partition."""
    translate = context.translate
    items = [Item(label=table.value, value=table) for table in TableType]
    menu: Menu[TableType] = Menu(
        title=translate("Partition table"), items=items, footer=footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    context.choice = replace(context.choice, table=answer.unwrap()[0])
    return Answer(Outcome.CHOSE, _rebuild(config, context))


def firmware_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Detected rather than asked first: the machine already booted one way,
    and installing for the other is almost always a mistake."""
    translate = context.translate
    items = [
        Item(
            label=firmware.value,
            value=firmware,
            detail="this machine booted this way" if firmware is context.firmware else "",
        )
        for firmware in Firmware
    ]
    menu: Menu[Firmware] = Menu(
        title=translate("Firmware"), items=items, footer=footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    firmware = answer.unwrap()[0]
    context.choice = replace(context.choice, firmware=firmware)
    changed = _rebuild(config, context)
    return Answer(
        Outcome.CHOSE,
        replace(changed, bootloader=replace(changed.bootloader, firmware=firmware)),
    )


def keymap_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """The keymaps this machine ships, family first.

    Two hundred rows do not fit a console, and a typed name that `kbd` has no
    file for loads nothing and says so only at the next boot.
    """
    translate = context.translate
    picked = _pick_keymap(screen, context, translate("Keyboard layout"), config.system.keymap)
    if picked is None:
        return Answer(Outcome.BACK)
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, keymap=picked or "us"))
    )


def _pick_keymap(
    screen: Screen, context: Context, title: str, current: str, empty: str = ""
) -> str | None:
    """One keymap, or None when the operator went back. `empty` names the row
    that stands for no answer, and is absent when there is no such answer."""
    translate = context.translate
    offered = context.keymaps()
    if not offered:
        # A medium that ships no keymap tree has nothing to list, and a list
        # nobody can populate is worse than a field.
        typed = TextField(title=title, value=current, footer=footer(translate)).run(screen)
        return typed.unwrap() if typed.chosen else None
    families: list[Item[str]] = []
    if empty:
        families.append(Item(label=empty, value=""))
    families += [
        Item(label=family, value=family) for family in sorted({one for one, _ in offered})
    ]
    answer = Menu(title=title, items=families, footer=footer(translate)).run(screen)
    if not answer.chosen:
        return None
    family = answer.unwrap()[0]
    if not family:
        return ""
    within = [name for one, name in offered if one == family]
    chosen = Menu(
        title=f"{title}  {family}",
        items=[Item(label=name, value=name) for name in within],
        footer=footer(translate),
    ).run(screen)
    return chosen.unwrap()[0] if chosen.chosen else None


def console_font_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Cell size of the console font. A rule in `compat.py` refuses the 8x8 one
    beside console CJK, so the excluded size is drawn with its own reason
    rather than being offered and then rejected by the validator."""
    items: list[Item[ConsoleFontSize]] = []
    for size in ConsoleFontSize:
        candidate = replace(config, system=replace(config.system, console_font=size))
        broken = compat.violations(candidate)
        items.append(
            Item(
                label=size.value,
                value=size,
                disabled_because=broken[0].reason if broken else "",
            )
        )
    answer = Menu(
        title=context.translate("Console font"), items=items, footer=footer(context.translate)
    ).run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, console_font=answer.unwrap()[0])),
    )


def networking_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """How the installed system brings a link up. The two NetworkManager rows
    differ only in which supplicant drives wifi."""
    translate = context.translate
    builtin = "systemd-networkd" if config.system.init is InitSystem.SYSTEMD else "netifrc"
    detail = {
        Networking.BUILTIN: builtin,
        Networking.NETWORKMANAGER_WPA: "wpa_supplicant for wifi",
        Networking.NETWORKMANAGER_IWD: "iwd for wifi",
        Networking.NONE: "configure it yourself after the install",
    }
    items = [
        Item(label=choice.value, value=choice, detail=detail[choice]) for choice in Networking
    ]
    menu: Menu[Networking] = Menu(
        title=translate("Network configuration"), items=items, footer=footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, networking=answer.unwrap()[0])),
    )


def partitions_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """The partition table, edited row by row.

    Every change rebuilds the graph and runs the validator, so a table that
    cannot be installed says why here rather than at the first `mkfs`.
    """
    translate = context.translate
    if context.layout.disk != context.choice.disk:
        # Seeded from the template that was chosen, not from a fixed default:
        # opening this row after picking zfs used to show an ext4 root and
        # discard the choice.
        context.layout = manual.suggest(
            context.choice.disk, context.choice.firmware, _template_filesystem(context.choice)
        )
    while True:
        rows = sorted(context.layout.slices, key=lambda one: one.index)
        items: list[Item[int]] = list(_existing(context))
        items += [Item(label=entry.describe(), value=index) for index, entry in enumerate(rows)]
        items.append(Item(label=translate("Add a partition"), value=len(rows)))
        items.append(Item(label=translate("Done"), value=len(rows) + 1))
        menu: Menu[int] = Menu(
            title=f"{translate('Partitions')}  {_capacity(context)}",
            items=items,
            footer=f"{_layout_problem(context, config)}  {footer(translate)}".strip(),
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        chosen = answer.unwrap()[0]
        if chosen < 0:
            continue
        if chosen == len(rows) + 1:
            # Marked here rather than by whoever opened this screen: the row can
            # be reached from the menu as well as from the layout row, and a
            # flag set before the editor answers describes a table that may
            # never have been produced.
            context.manual = True
            return Answer(Outcome.CHOSE, _from_layout(config, context))
        if chosen == len(rows):
            added = _edit_slice(screen, context, None)
            if added is not None:
                context.layout.slices.append(added)
            continue
        edited = _edit_slice(screen, context, rows[chosen])
        context.layout.slices.remove(rows[chosen])
        if edited is not None:
            context.layout.slices.append(edited)


def _template_filesystem(choice: Choice) -> FilesystemType | None:
    """What the root of the chosen template carries. None for ZFS, whose root
    is a dataset on a pool and not a filesystem on a partition."""
    return None if choice.layout is Layout.WHOLE_DISK_ZFS else choice.filesystem


def _capacity(context: Context) -> str:
    """The disk's size and what the table has already claimed, because a size
    is guesswork without them."""
    total = context.disk_size
    if not total:
        return ""
    claimed = sum(
        entry.size.bytes for entry in context.layout.slices if entry.size is not None
    )
    rest = any(entry.size is None for entry in context.layout.slices)
    used = Size(claimed)
    return f"{total} total, {used} claimed{', rest to one partition' if rest else ''}"


def _existing(context: Context) -> tuple[Item[int], ...]:
    """What is on the disk now, drawn above the table and not selectable."""
    return tuple(
        Item(
            label=f"{name}  {size}  {kind or 'no filesystem'}",
            value=-1,
            disabled_because=context.translate("on the disk now, will be erased"),
        )
        for name, size, kind in context.existing
    )


def _layout_problem(context: Context, config: InstallConfig) -> str:
    """What the validator says about the table as it stands."""
    try:
        graph, root = manual.build(context.layout)
    except GentooInstallError as error:
        return str(error)
    if not root:
        return context.translate("no partition is mounted at /")
    try:
        validate(replace(config, disk=DiskConfig(graph=graph, root=root)))
    except ValidationFailed as error:
        return str(error).splitlines()[-1].strip()
    return ""


def _from_layout(config: InstallConfig, context: Context) -> InstallConfig:
    graph, root = manual.build(context.layout)
    return replace(config, disk=DiskConfig(graph=graph, root=root))


#: One row of the slice editor. Every field is visible with its value, so no
#: answer is hidden behind a screen the operator has to reach to discover.
_SIZE: Final[str] = "size"
_PURPOSE: Final[str] = "purpose"
_FILESYSTEM: Final[str] = "filesystem"
_MOUNTPOINT: Final[str] = "mountpoint"
_FORMAT: Final[str] = "format"
_LABEL: Final[str] = "label"
_ENCRYPTION: Final[str] = "encryption"
_DELETE: Final[str] = "delete"


def _edit_slice(
    screen: Screen, context: Context, current: manual.Slice | None
) -> manual.Slice | None:
    """One partition as a list of fields, or None to delete it."""
    translate = context.translate
    entry = current or manual.Slice(
        index=context.layout.next_index(),
        role=PartitionRole.DATA,
        size=None,
        filesystem=FilesystemType.EXT4,
        mountpoint="",
    )
    cursor = 0
    while True:
        purpose = manual.purpose_of(entry)
        menu: Menu[str] = Menu(
            title=f"{translate('Partition')} {entry.index}",
            items=_slice_fields(entry, purpose, translate),
            footer=footer(translate),
            cursor=cursor,
        )
        answer = menu.run(screen)
        cursor = menu.cursor
        if not answer.chosen:
            return current
        field = answer.unwrap()[0]
        if field == _DONE:
            return entry
        if field == _DELETE:
            return None
        changed = _edit_field(screen, context, entry, purpose, field)
        if changed is not None:
            entry = changed


def _slice_fields(
    entry: manual.Slice, purpose: manual.Purpose, translate: Catalog
) -> list[Item[str]]:
    """Every field with its value, and why one that does not apply cannot be
    opened."""
    no_filesystem = translate("this purpose fixes the filesystem")
    return [
        Item(label=translate("Size"), value=_SIZE, detail=_size_of(entry, translate)),
        Item(label=translate("Purpose"), value=_PURPOSE, detail=purpose.label),
        Item(
            label=translate("Filesystem"),
            value=_FILESYSTEM,
            detail=entry.filesystem.value if entry.filesystem else "-",
            disabled_because="" if purpose.chooses_filesystem else no_filesystem,
        ),
        Item(
            label=translate("Mount point"),
            value=_MOUNTPOINT,
            detail=entry.mountpoint or "-",
            disabled_because=(
                "" if purpose.asks_mountpoint else translate("this purpose fixes the mount point")
            ),
        ),
        Item(label=translate("Label"), value=_LABEL, detail=entry.label or "-"),
        Item(
            label=translate("Encryption"),
            value=_ENCRYPTION,
            detail=translate("on") if entry.passphrase_file else translate("off"),
        ),
        Item(label=translate("Delete this partition"), value=_DELETE),
        Item(label=translate("Done"), value=_DONE),
    ]


def _size_of(entry: manual.Slice, translate: Catalog) -> str:
    return str(entry.size) if entry.size is not None else translate("the remaining space")


def _edit_field(
    screen: Screen,
    context: Context,
    entry: manual.Slice,
    purpose: manual.Purpose,
    field: str,
) -> manual.Slice | None:
    """The one screen behind a field, or None when the operator went back."""
    translate = context.translate
    if field == _SIZE:
        typed = TextField(
            title=translate("Size"),
            value="" if entry.size is None else str(entry.size),
            placeholder=translate("512MiB, 20GiB, or empty for the remaining space"),
            footer=footer(translate),
        ).run(screen)
        if not typed.chosen:
            return None
        text = typed.unwrap().strip()
        return replace(entry, size=Size.parse(text) if text else None)
    if field == _PURPOSE:
        picked = Menu(
            title=translate("What is this partition for?"),
            items=[Item(label=one.label, value=one) for one in manual.PURPOSES],
            footer=footer(translate),
        ).run(screen)
        if not picked.chosen:
            return None
        return _apply_purpose(entry, picked.unwrap()[0])
    if field == _FILESYSTEM:
        # zfs is listed here as well as under the purpose, because that is
        # where anyone choosing a filesystem looks for it. It is a pool, so
        # picking it changes what the partition is rather than how it is
        # formatted.
        items: list[Item[FilesystemType | None]] = [
            Item(label=one.value, value=one) for one in FilesystemType
        ]
        items.append(
            Item(label="zfs", value=None, detail=translate("a pool member, not a filesystem"))
        )
        answered = Menu(
            title=translate("Filesystem"), items=items, footer=footer(translate)
        ).run(screen)
        if not answered.chosen:
            return None
        kind = answered.unwrap()[0]
        if kind is None:
            return _apply_purpose(entry, manual.purpose_for("zfs"))
        return replace(entry, filesystem=kind)
    if field == _MOUNTPOINT:
        where = TextField(
            title=translate("Mount point"),
            value=entry.mountpoint,
            placeholder=translate("/srv, or empty to leave it unmounted"),
            footer=footer(translate),
        ).run(screen)
        if not where.chosen:
            return None
        return replace(entry, mountpoint=where.unwrap().strip())
    if field == _LABEL:
        named = TextField(
            title=translate("Label"),
            value=entry.label,
            placeholder=translate("gentoo"),
            footer=footer(translate),
        ).run(screen)
        if not named.chosen:
            return None
        return replace(entry, label=named.unwrap().strip())
    return _edit_slice_encryption(screen, context, entry, purpose)


def _apply_purpose(entry: manual.Slice, purpose: manual.Purpose) -> manual.Slice:
    """Everything the purpose decides, in one place: picking `swap` has to drop
    the filesystem and the mount point it had as `root`."""
    return replace(
        entry,
        role=purpose.role,
        filesystem=entry.filesystem if purpose.chooses_filesystem else purpose.filesystem,
        mountpoint=entry.mountpoint if purpose.asks_mountpoint else purpose.mountpoint,
    )


def _edit_slice_encryption(
    screen: Screen, context: Context, entry: manual.Slice, purpose: manual.Purpose
) -> manual.Slice | None:
    translate = context.translate
    turned = Confirm(
        **answers(translate),
        title=(
            translate("Encrypt the pool?")
            if purpose.role is PartitionRole.ZFS
            else translate("Encrypt this partition?")
        ),
        footer=footer(translate),
    ).run(screen)
    if not turned.chosen:
        return None
    if not turned.unwrap():
        return replace(entry, passphrase_file="")
    staged = _ask_passphrase(screen, context)
    if not staged:
        return None
    return replace(entry, passphrase_file=staged)


def extra_packages_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Any atom the operator wants, typed in.

    Only the syntax is checked here. Whether it resolves is a question for the
    target's repositories, which `VerifyPackages` asks once the tree is synced;
    the live medium often carries no repository at all.
    """
    translate = context.translate
    while True:
        typed = TextField(
            title=translate("Packages to install, separated by spaces"),
            value=" ".join(config.packages.extra),
            footer=footer(translate),
        ).run(screen)
        if not typed.chosen:
            return Answer(typed.outcome)
        good, bad = atoms.split(typed.unwrap())
        if bad:
            _say(screen, context, f"{translate('Not a package name')}: {' '.join(bad)}")
            continue
        return Answer(
            Outcome.CHOSE, replace(config, packages=replace(config.packages, extra=good))
        )


#: What each interface language is called in itself, and what it needs to be
#: readable. A console with no CJK font draws the last three as blank cells.
INTERFACE_LANGUAGES: tuple[tuple[str, str, bool], ...] = (
    ("en", "English", False),
    ("zh-TW", "\u6b63\u9ad4\u4e2d\u6587", True),
    ("zh-CN", "\u7b80\u4f53\u4e2d\u6587", True),
    ("ja", "\u65e5\u672c\u8a9e", True),
    ("ko", "\ud55c\uad6d\uc5b4", True),
)


@dataclass(frozen=True)
class LanguageDefaults:
    """What picking an interface language pre-fills.

    Someone reading Traditional Chinese is in Taipei rather than Shanghai and wants
    `zh_TW.UTF-8`, and the CN mirrors are the wrong side of a border for them.
    Every one of these stays a row the operator can change.
    """

    locale: str
    timezone: str
    mirror: MirrorRegion
    #: True for the languages the cjktty patch is the point of. It pulls in
    #: gentoo-zh, so it is not a default for a language that would not use the
    #: rest of that overlay.
    cjk_console: bool = False


#: One row per interface language. Keyed by the same tags as the catalogs.
LANGUAGE_DEFAULTS: Final[dict[str, LanguageDefaults]] = {
    "en": LanguageDefaults("en_US.UTF-8", "UTC", MirrorRegion.GLOBAL),
    "zh-CN": LanguageDefaults("zh_CN.UTF-8", "Asia/Shanghai", MirrorRegion.CN, True),
    "zh-TW": LanguageDefaults("zh_TW.UTF-8", "Asia/Taipei", MirrorRegion.GLOBAL, True),
    "ja": LanguageDefaults("ja_JP.UTF-8", "Asia/Tokyo", MirrorRegion.GLOBAL),
    "ko": LanguageDefaults("ko_KR.UTF-8", "Asia/Seoul", MirrorRegion.GLOBAL),
}


def with_language(config: InstallConfig, tag: str) -> InstallConfig:
    """The configuration as the chosen interface language leaves it."""
    chosen = LANGUAGE_DEFAULTS.get(tag)
    if chosen is None:
        return config
    locales = config.system.locales
    if chosen.locale not in locales:
        locales = (*locales, chosen.locale)
    seeded = replace(
        config,
        system=replace(
            config.system,
            locale=chosen.locale,
            timezone=chosen.timezone,
            locales=locales,
            console_cjk=chosen.cjk_console,
        ),
        portage=replace(
            config.portage, mirrors=replace(config.portage.mirrors, region=chosen.mirror)
        ),
    )
    if not chosen.cjk_console:
        return seeded
    # The patched kernel is what puts CJK on the console, and it is in gentoo-zh
    # and nowhere else, so the overlay comes with it or the row is unusable.
    return replace(
        seeded,
        kernel=replace(seeded.kernel, source=KernelSource.CJK),
        portage=_with_gentoo_zh(seeded),
    )


def language_screen(screen: Screen, context: Context) -> str:
    """Asked once, before the menu.

    The environment says which language the operator reads; it does not say
    whether this terminal can draw it. So the CJK entries carry the warning and
    English stays first, reachable even when every other row is blank squares.
    """
    items = [
        Item(
            label=f"{name}  ({tag})",
            value=tag,
            detail="needs a cjktty kernel or a terminal with CJK fonts" if cjk else "",
        )
        for tag, name, cjk in INTERFACE_LANGUAGES
    ]
    start = next(
        (index for index, (tag, _, _) in enumerate(INTERFACE_LANGUAGES) if tag == context.tag), 0
    )
    menu: Menu[str] = Menu(
        title="Language / \u8a9e\u8a00 / \uc5b8\uc5b4",
        items=items,
        cursor=start,
        footer="[enter] select",
    )
    answer = menu.run(screen)
    return answer.unwrap()[0] if answer.chosen else context.tag


#: What each license set allows, in the order the menu offers them.
LICENSES: tuple[tuple[str, str], ...] = (
    ("@FREE", "free software and free documentation only"),
    ("@FREE @BINARY-REDISTRIBUTABLE", "also firmware and other redistributable binaries"),
    ("*", "every license"),
)


def license_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """`ACCEPT_LICENSE`. The default refuses anything that is not free, and a
    machine that needs firmware will not install until this is widened."""
    translate = context.translate
    items = [
        Item(label=value, value=value, detail=translate(reason)) for value, reason in LICENSES
    ]
    menu: Menu[str] = Menu(
        title=translate("Licenses to accept"), items=items, footer=footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            portage=replace(config.portage, accept_license=tuple(answer.unwrap()[0].split())),
        ),
    )


def makeopts_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """How many compile jobs. The machine's core count is first and preselected,
    because it is right for almost every install and wrong only when memory is
    short: each job can want a gigabyte."""
    translate = context.translate
    cores = context.cores
    offered = [cores, max(1, cores // 2), 1]
    items = [
        Item(
            label=f"-j{jobs}",
            value=f"-j{jobs}",
            detail=translate("this machine's core count") if jobs == cores else "",
        )
        for jobs in dict.fromkeys(offered)
    ]
    menu: Menu[str] = Menu(
        title=translate("Compile jobs"), items=items, footer=footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, portage=replace(config.portage, makeopts=answer.unwrap()[0])),
    )


def compile_flags_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """`COMMON_FLAGS`, which CFLAGS and the rest follow.

    Leaving the stage3's value is first because it is what Gentoo built the
    binary packages against: `-march=native` makes every one of them a miss,
    and an install that was ten minutes becomes hours of compiling.
    """
    translate = context.translate
    stock = PortageConfig().common_flags
    items: list[Item[str]] = [
        Item(
            label=f"{stock}  ({translate('what the stage3 already has')})",
            value=stock,
            detail=translate("binary packages match"),
        ),
        Item(
            label="-O2 -pipe -march=native",
            value="-O2 -pipe -march=native",
            detail=translate("built for this CPU, and no binary package matches"),
        ),
        Item(
            label="-O3 -pipe -march=native",
            value="-O3 -pipe -march=native",
            detail=translate("built for this CPU, and no binary package matches"),
        ),
        Item(label=translate("Type them"), value=""),
    ]
    menu: Menu[str] = Menu(
        title=translate("Compiler flags"), items=items, footer=footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()[0]
    if not chosen:
        typed = TextField(
            title=translate("Compiler flags"),
            value=config.portage.common_flags,
            footer=footer(translate),
        ).run(screen)
        if not typed.chosen:
            return Answer(typed.outcome)
        chosen = typed.unwrap().strip() or stock
    return Answer(
        Outcome.CHOSE, replace(config, portage=replace(config.portage, common_flags=chosen))
    )


def initramfs_keymap_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """The keyboard the passphrase prompt uses.

    Its own row because it is the one that locks people out: an encrypted root
    asks before the console keymap is loaded, and a keyboard that is not us
    cannot type a passphrase it was never told about.
    """
    translate = context.translate
    picked = _pick_keymap(
        screen,
        context,
        translate("Keyboard the initramfs uses"),
        config.system.keymap_initramfs,
        empty=translate("the same as the console"),
    )
    if picked is None:
        return Answer(Outcome.BACK)
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, keymap_initramfs=picked))
    )


def address_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """DHCP, or every static field on one page.

    A machine on a network with no DHCP comes up unreachable, and one given an
    address with no resolver comes up unable to look anything up. Both families
    are here because a v6-only network is not a special case any more.
    """
    translate = context.translate
    system = config.system
    wanted = Confirm(
        **answers(translate), title=translate("Use DHCP?"), footer=footer(translate)
    ).run(screen)
    if not wanted.chosen:
        return Answer(wanted.outcome)
    if wanted.unwrap():
        return Answer(
            Outcome.CHOSE,
            replace(
                config,
                system=replace(system, addresses=(), gateways=(), dns=()),
            ),
        )
    form = Form(
        title=translate("Static address"),
        fields=[
            Field(
                label=translate("Interface"),
                value=system.interface,
                placeholder=translate("enp1s0, or empty for the first wired one"),
            ),
            Field(
                label=translate("IPv4"),
                value=next((one for one in system.addresses if ":" not in one), ""),
                placeholder="192.0.2.10/24",
            ),
            Field(
                label=translate("IPv4 gateway"),
                value=next((one for one in system.gateways if ":" not in one), ""),
                placeholder="192.0.2.1",
            ),
            Field(
                label=translate("IPv6"),
                value=next((one for one in system.addresses if ":" in one), ""),
                placeholder="2001:db8::2/64",
            ),
            Field(
                label=translate("IPv6 gateway"),
                value=next((one for one in system.gateways if ":" in one), ""),
                placeholder="fe80::1",
            ),
            Field(
                label=translate("DNS"),
                value=" ".join(system.dns),
                placeholder=translate("separated by spaces"),
            ),
        ],
        footer=footer(translate),
        done=translate("Done"),
    )
    answer = form.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    interface, four, four_gateway, six, six_gateway, resolvers = (
        one.strip() for one in answer.unwrap()
    )
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            system=replace(
                system,
                interface=interface,
                addresses=tuple(one for one in (four, six) if one),
                gateways=tuple(one for one in (four_gateway, six_gateway) if one),
                dns=tuple(resolvers.split()),
            ),
        ),
    )


def authorized_keys_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Public keys, typed or fetched, checked before they are the only way in.

    Not read from the live system: the operator installing over ssh is not
    necessarily the person whose key belongs on the new machine.
    """
    translate = context.translate
    keys = list(config.system.authorized_keys)
    while True:
        items: list[Item[int]] = [
            Item(
                label=_key_summary(key),
                value=-index - 1,
                # Said rather than confirmed: enter is the only thing this row
                # does, and a key fetched from a URL is gone without a word.
                detail=translate("enter removes it"),
            )
            for index, key in enumerate(keys)
        ]
        items += [
            Item(label=translate("Type a key"), value=0),
            Item(
                label=translate("Fetch from a paste"),
                value=1,
                detail=translate("the identifier alone"),
            ),
            Item(label=translate("Fetch from a URL"), value=2),
            Item(label=translate("Done"), value=3),
        ]
        menu: Menu[int] = Menu(
            title=translate("SSH public keys"), items=items, footer=footer(translate)
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        chosen = answer.unwrap()[0]
        if chosen < 0:
            keys.pop(-chosen - 1)
            continue
        if chosen == 3:
            return Answer(
                Outcome.CHOSE,
                replace(config, system=replace(config.system, authorized_keys=tuple(keys))),
            )
        added = _ADD_A_KEY[chosen](screen, context)
        if added:
            keys.append(added)


def _key_summary(key: str) -> str:
    """Type and comment. The body is 68 columns of base64 that tells the
    operator nothing about which key this is."""
    fields = key.split()
    comment = fields[2] if len(fields) > 2 else ""
    return f"{fields[0]} {comment}".strip()


def _type_a_key(screen: Screen, context: Context) -> str:
    translate = context.translate
    typed = TextField(
        title=translate("Public key"),
        placeholder=translate("ssh-ed25519 AAAA... name@host"),
        footer=footer(translate),
    ).run(screen)
    if not typed.chosen:
        return ""
    return _checked_key(screen, context, typed.unwrap())


def _paste_a_key(screen: Screen, context: Context) -> str:
    """The identifier alone, because the whole address is tedious to copy onto
    a console by hand and the host part never changes."""
    translate = context.translate
    typed = TextField(
        title=f"{paste.BASE}/",
        placeholder=translate("the identifier, such as hjq+353Jzfk"),
        footer=footer(translate),
    ).run(screen)
    if not typed.chosen or not typed.unwrap().strip():
        return ""
    return _read_a_key(screen, context, paste.url_for(typed.unwrap()))


def _fetch_a_key(screen: Screen, context: Context) -> str:
    translate = context.translate
    typed = TextField(
        title=translate("URL of a public key"),
        placeholder=translate("https://example.com/id_ed25519.pub"),
        footer=footer(translate),
    ).run(screen)
    if not typed.chosen or not typed.unwrap().strip():
        return ""
    return _read_a_key(screen, context, typed.unwrap().strip())


def _read_a_key(screen: Screen, context: Context, url: str) -> str:
    translate = context.translate
    try:
        body = context.fetch_text(url)
    except GentooInstallError as error:
        _say(screen, context, str(error))
        return ""
    # A paste holding several keys is the normal case for one person's file,
    # and taking the first line silently would drop the rest.
    for line in body.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            return _checked_key(screen, context, line)
    _say(screen, context, translate("that address returned no key"))
    return ""


#: The rows that add a key, in the order the menu lists them.
_ADD_A_KEY: Final[tuple[Callable[[Screen, "Context"], str], ...]] = (
    lambda screen, context: _type_a_key(screen, context),
    lambda screen, context: _paste_a_key(screen, context),
    lambda screen, context: _fetch_a_key(screen, context),
)


def _checked_key(screen: Screen, context: Context, line: str) -> str:
    """A key that reached the target truncated is discovered at the first login
    attempt, by which time the console is gone."""
    try:
        return sshkey.check(line.strip())
    except GentooInstallError as error:
        _say(screen, context, str(error))
        return ""


def remote_unlock_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Unlocking the root over ssh, from the initramfs.

    The authorised keys are the same ones: dracut-crypt-ssh reads
    `/root/.ssh/authorized_keys`, which is where they are written, so this is
    refused without one rather than installed as a daemon nobody can reach.
    """
    translate = context.translate
    unlock = config.kernel.remote_unlock
    asked = Confirm(
        **answers(translate),
        title=translate("Unlock the root over SSH from the initramfs?"),
        footer=footer(translate),
    ).run(screen)
    if not asked.chosen:
        return Answer(asked.outcome)
    if not asked.unwrap():
        return Answer(
            Outcome.CHOSE,
            replace(config, kernel=replace(config.kernel, remote_unlock=replace(unlock, enabled=False))),
        )
    typed = (str(unlock.port), unlock.address)
    while True:
        form = Form(
            title=translate("Remote unlock"),
            fields=[
                Field(label=translate("Port"), value=typed[0], placeholder="222"),
                Field(
                    label=translate("Address"),
                    value=typed[1],
                    placeholder=translate("empty for DHCP, or dracut ip= form"),
                ),
            ],
            footer=footer(translate),
            done=translate("Done"),
        )
        answer = form.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        port, address = (one.strip() for one in answer.unwrap())
        if port.isdigit():
            break
        # Reopened with what was typed: dropping out of the form took the
        # address with it and the operator retyped both to fix one.
        typed = (port, address)
        _say(screen, context, translate("The port has to be a number."))
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            kernel=replace(
                config.kernel,
                remote_unlock=replace(unlock, enabled=True, port=int(port), address=address),
            ),
        ),
    )


def root_login_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Whether sshd lets root in at all, which is separate from whether a
    password is accepted: a key is enough to reach root without one."""
    translate = context.translate
    menu: Menu[bool] = Menu(
        title=translate("Let root log in over SSH?"),
        items=[
            Item(
                label=translate("allowed"),
                value=True,
                detail=translate("the authorised keys are written for root too"),
            ),
            Item(
                label=translate("refused"),
                value=False,
                detail=translate("reach root through a sudo user"),
            ),
        ],
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, sshd_root_login=answer.unwrap()[0])),
    )
