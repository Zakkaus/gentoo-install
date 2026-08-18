# SPDX-License-Identifier: GPL-2.0-or-later
"""One screen per decision, each a function of the configuration so far.

A screen never mutates what it was given: it returns a new `InstallConfig`, and
`app.py` re-validates before moving on. Every option a compatibility rule
excludes is drawn greyed with that rule's own sentence, so the interface and the
validator never disagree about why something cannot be chosen.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from itertools import takewhile
from pathlib import PurePosixPath
from typing import Callable, Final, Generic, Sequence, TypeVar, TypedDict

from ..i18n import Catalog, truncate
from ..exec import fetch
from ..model import compat
from ..model.config import (
    SystemConfig,
    FirstBoot,
    ConsoleFontSize,
    Binhost,
    BinhostChannel,
    Bootloader,
    BootloaderConfig,
    DiskConfig,
    DiskMode,
    ImageFormat,
    Firewall,
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
    ProxyConfig,
    ProxyKind,
    Sync,
    User,
)
from ..model.device import (
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    Luks,
    Mountpoint,
    Partition,
    PartitionTable,
    Swap,
    FilesystemType,
    PartitionRole,
    RaidLevel,
    RaidMetadata,
    TableType,
    ZfsPool,
    ZfsTopology,
)
from ..plan import automatic as automatic_values
from ..plan.convert import REPLACED_DIRECTORIES, SWAP_CONFIRMATION
from ..plan import kernel as plan_kernel
from ..plan.fonts import CJK_SANS_PREFERENCE, CjkFontconfigLocale, FontCategory
from ..model.compat import KERNEL_PACKAGES
from ..plan.portage import community_binhost
from ..plan.operations import Operation
from ..plan.render import counts
from ..model.size import ZERO, Size
from ..errors import ConfigError, DeviceNotFound, GentooInstallError, ValidationFailed
from ..model import atoms, manual, mirrors, paste, qr, sshkey
from ..model.templates import Choice, Layout, build
from ..model.validate import validate
from ..plan.packages import Catalog as Groups
from ..plan.packages import FRAMEWORK_GROUPS
from ..plan.packages import FONT_CONFIGURATION_DISABLED, FONT_CONFIGURATION_ENABLED
from ..plan.packages import INPUT_CONFIGURATION_DISABLED, INPUT_CONFIGURATION_ENABLED
from ..plan.packages import driver_conflict, framework_conflict
from ..plan import system as plan_system
from .context import (
    Answers,
    Context,
    Step,
    ValueKind,
    ValueProvenance,
    ValueSource,
    answers,
    footer,
    say,
    show_address,
)
from .widgets import (
    Accepts,
    band,
    MINIMUM_COLUMNS,
    Answer,
    Confirm,
    Field,
    Form,
    FormRejected,
    Item,
    Menu,
    MultipleChoiceMenu,
    Outcome,
    Screen,
    TextField,
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


#: What each mode does, in the operator's terms rather than the enum's.
INSTALL_MODES: tuple[tuple[DiskMode, str], ...] = (
    (DiskMode.PARTITION, "partition a disk and install onto it"),
    (DiskMode.IN_PLACE, "replace the running system with Gentoo, keeping its disks"),
    (DiskMode.DD, "write a prepared image over a whole disk"),
)


def install_mode_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Install onto disks, convert the running system, or write an image."""
    translate = context.translate
    refused = context.conversion_refused
    image_write_refused = context.image_write_refused
    preamble = [translate("This is the difference between a new system and this one.")]
    if context.running_system:
        preamble.append(translate("Running system: ") + context.running_system)
    if refused:
        preamble.append(translate("Conversion is not offered: ") + translate(refused))
    if image_write_refused:
        preamble.append(
            translate("Image writing is not offered: ") + translate(image_write_refused)
        )
    menu: Menu[DiskMode] = Menu(
        title=translate("Install mode"),
        preamble=tuple(preamble),
        items=[
            Item(label=translate(what), value=mode)
            for mode, what in INSTALL_MODES
            if (mode is not DiskMode.IN_PLACE or not refused)
            and (mode is not DiskMode.DD or not image_write_refused)
        ],
        footer=footer(translate),
        current=config.disk.mode,
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    mode = answer.unwrap()
    if mode is config.disk.mode:
        return Answer(Outcome.CHOSE, config)
    if mode is DiskMode.IN_PLACE:
        agreed = _confirm_the_swap(screen, context)
        if agreed is not None:
            return agreed
    if mode is DiskMode.DD:
        return Answer(
            Outcome.CHOSE,
            replace(
                config,
                disk=replace(
                    config.disk,
                    mode=mode,
                    graph=DeviceGraph.build([]),
                    root=DeviceId(""),
                    image="",
                    size=None,
                    wipe=False,
                ),
            ),
        )
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            disk=replace(
                config.disk,
                mode=mode,
                graph=DeviceGraph.build([]) if mode is DiskMode.IN_PLACE else config.disk.graph,
                root=DeviceId(""),
                source="",
                source_format=ImageFormat.RAW,
                destination="",
            ),
        ),
    )


def image_source_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Choose the file whose bytes are streamed onto the destination disk."""
    translate = context.translate
    source = TextField(
        title=translate("Image source"),
        value=config.disk.source,
        placeholder=translate("/path/to/image.raw"),
        footer=footer(translate),
    ).run(screen)
    if not source.chosen:
        return Answer(source.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, disk=replace(config.disk, source=source.unwrap()))
    )


def image_format_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Choose the decoder that emits the prepared image bytes."""
    translate = context.translate
    source_format = Menu[ImageFormat](
        title=translate("Image format"),
        items=[Item(label=one.value, value=one) for one in ImageFormat],
        footer=footer(translate),
        current=config.disk.source_format,
    ).run(screen)
    if not source_format.chosen:
        return Answer(source_format.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, disk=replace(config.disk, source_format=source_format.unwrap())),
    )


def image_destination_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Choose the whole disk that is overwritten by the image."""
    translate = context.translate
    if not context.disks:
        raise DeviceNotFound("this machine has no disk to install onto")
    destination = Menu[str](
        title=translate("Destination disk"),
        preamble=(translate("The selected disk is overwritten by the image."),),
        items=[
            Item(label=context.shown_as(name), value=name, detail=detail)
            for name, detail in context.disks
        ],
        footer=footer(translate),
        current=config.disk.destination,
    ).run(screen)
    if not destination.chosen:
        return Answer(destination.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, disk=replace(config.disk, destination=destination.unwrap())),
    )


def _confirm_the_swap(screen: Screen, context: Context) -> Answer[InstallConfig] | None:
    """None once the operator has typed the word, an outcome to return if not.

    The swap is irreversible: `/home` and `/root` are outside
    `REPLACED_DIRECTORIES` and survive, but that is not a way back — the old
    `/usr`, `/etc` and `/var` are gone and the staging root is removed. So the
    screen names what goes, names what stays, and asks for a word rather than a
    keypress.
    """
    translate = context.translate
    replaced = ", ".join("/" + name for name in REPLACED_DIRECTORIES)
    question = Confirm(
        **answers(translate),
        title=translate("This replaces the running system and cannot be undone."),
        phrase=SWAP_CONFIRMATION,
        detail=(
            f"{translate('Replaced: ')}{replaced}. "
            f"{translate('Kept: /home, /root and every other mount.')} "
            f"{translate('Type the word to confirm.')}"
        ),
        footer=footer(translate),
    )
    answer = question.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    if answer.unwrap():
        return None
    return Answer(Outcome.BACK)


def disk_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    if not context.disks:
        # Named, and under `GentooInstallError`: `cli.py` turns those into an
        # exit code, and a bare `LookupError` left the menu with a traceback.
        raise DeviceNotFound("this machine has no disk to install onto")
    menu: Menu[str] = Menu(
        title=translate("Disks"),
        preamble=(translate("The selected disk is rewritten as the install target."),),
        items=[
            # Labelled by the kernel name and valued by the selector: the
            # configuration needs the stable one and the operator reads the
            # short one.
            Item(label=context.shown_as(name), value=name, detail=detail)
            for name, detail in context.disks
        ],
        footer=footer(translate),
        current=context.choice.disk,
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    picked = answer.unwrap()
    context.choice = replace(context.choice, disk=picked)
    # `_rebuild` reads the layout rather than the choice when the table was
    # hand-written, so leaving this behind partitioned the disk the operator
    # switched away from. The kept rows name partitions of that disk and go too.
    context.layout = manual.Layout()
    # Cleared with the disk: the operator typed the name of the one they were
    # looking at, and carrying that confirmation to another unblocks the
    # install for a disk nobody agreed to erase.
    context.confirmed.clear()
    context.inspect_disk(picked)
    return Answer(Outcome.CHOSE, _rebuild(config, context))


def layout_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Who lays the disk out, and only then what goes on it.

    One list held both questions: four filesystems and `manual` side by side,
    as if `manual` were a fifth filesystem. It is the other question, and
    asking it first is what makes the automatic path a path rather than a
    default nobody was offered an alternative to.
    """
    translate = context.translate
    by_hand = context.choice.layout is Layout.REUSE
    how = Menu[bool](
        title=translate("How is this disk laid out?"),
        preamble=(translate("Automatic layout replaces the partition table; manual layout controls each partition."),),
        items=[
            Item(
                label=translate("automatic"),
                value=False,
                detail=translate("the installer writes the table"),
            ),
            Item(
                label=translate("manual"),
                value=True,
                detail=translate("one table: keep, format, delete or add each partition"),
            ),
        ],
        footer=footer(translate),
        current=by_hand,
    ).run(screen)
    if not how.chosen:
        return Answer(how.outcome)
    if how.unwrap():
        context.manual = False
        return partitions_screen(screen, config, context)
    return _template_screen(screen, config, context)


def _template_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """What the installer writes when it lays the disk out itself."""
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
            disabled_because=context.zfs_unavailable,
        ),
    ]
    # On the row the configuration already holds, so enter keeps what is set
    # rather than choosing whichever filesystem happens to be listed first.
    here = next(
        (
            index
            for index, item in enumerate(items)
            if item.value[0] is context.choice.layout
            and item.value[1] is context.choice.filesystem
        ),
        0,
    )
    menu: Menu[tuple[Layout | None, FilesystemType]] = Menu(
        title=translate("Layout"),
        preamble=(translate("This choice sets the root filesystem and partition graph."),),
        items=items, footer=footer(translate), cursor=here
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    layout, filesystem = answer.unwrap()
    was_manual, was_choice = context.manual, context.choice
    context.manual = False
    if layout is None:
        return partitions_screen(screen, config, context)
    context.choice = replace(context.choice, layout=layout, filesystem=filesystem)
    changed = _rebuild(config, context)
    if layout is Layout.WHOLE_DISK_ZFS:
        picked = _zfs_bootloader(screen, changed, context)
        if not picked.chosen:
            # The graph was already rebuilt and the choice already written, so
            # both go back before the child's outcome leaves this screen.
            context.manual, context.choice = was_manual, was_choice
            return Answer(picked.outcome)
        changed = picked.unwrap()
    return Answer(Outcome.CHOSE, changed)


def _known(kind: str) -> FilesystemType | None:
    """The type blkid reported, when the model has a member for it. ntfs and
    exfat are mounted and never created, so they have no member and no row."""
    return next((one for one in FilesystemType if one.value == kind), None)


def _zfs_bootloader(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """A ZFS root cannot use GRUB, so this asks which of the two that remain.

    ZFSBootMenu lives in the gentoo-zh overlay and in no other repository, so
    choosing it is also consenting to that overlay. Adding it silently is what
    this replaces.
    """
    translate = context.translate
    answer = Menu(
        title=translate("A ZFS root cannot boot from GRUB. Which bootloader?"),
        preamble=(translate("The bootloader determines how the ZFS root is found at startup."),),
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
        current=config.bootloader.kind,
    ).run(screen)

    def apply(kind: Bootloader) -> InstallConfig:
        if kind is Bootloader.SYSTEMD_BOOT:
            return replace(config, bootloader=replace(config.bootloader, kind=kind))
        return replace(
            config,
            bootloader=replace(config.bootloader, kind=kind),
            portage=_with_gentoo_zh(config),
        )

    return answer.map(apply)


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
    """The one screen with no default: each name has to be typed.

    Every device `compat.destroyed` names, not `context.choice.disk`: a second
    disk added in manual partitioning was destroyed under a confirmation that
    named the first one.
    """
    translate = context.translate
    original = set(context.confirmed)
    pending = set(original)
    for target in compat.destroyed(config.disk.graph):
        if target.selector in pending:
            continue
        answered = _confirm_one(screen, context, target.selector, pending)
        if answered is not None:
            context.confirmed = original
            return answered
    context.confirmed = pending
    return Answer(Outcome.CHOSE, config)


def _confirm_one(
    screen: Screen,
    context: Context,
    disk: str,
    confirmed: set[str],
) -> Answer[InstallConfig] | None:
    """None once this selector is confirmed; an outcome to return otherwise."""
    translate = context.translate
    # Every name this disk answers to: the selector the installer chose, its
    # last component, and the `/dev/sda` an operator reads off `lsblk`.
    accepted = {disk, disk.rsplit("/", 1)[-1], *context.names_for(disk)}
    shown = context.shown_as(disk)
    while True:
        # On its own line, not inside the field: a placeholder is drawn where a
        # value would be, and an operator pressed enter on what looked like a
        # field already filled in.
        question = Confirm(
            **answers(translate),
            title=f"{translate('This erases every partition on the disk.')} "
            f"{translate('Type the disk name to confirm.')}",
            phrase=shown,
            also=tuple(accepted - {shown}),
            detail=shown,
            footer=footer(translate),
        )
        answer = question.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        if answer.unwrap():
            confirmed.add(disk)
            return None
        # Said rather than swallowed: a trailing space read as a refusal, and
        # the row went back to unset with nothing explaining why.
        say(screen, context, translate("That is not the name of this disk."))


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


def proxy_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Set the session proxy before any network-derived setting is read."""
    translate = context.translate
    current = config.proxy

    kind_answer = Menu(
        title=translate("Proxy type"),
        items=[Item(kind.value, kind) for kind in ProxyKind],
        current=current.kind,
        footer=footer(translate),
    ).run(screen)
    if not kind_answer.chosen:
        return Answer(kind_answer.outcome)
    selected_kind = kind_answer.unwrap()

    def validated(values: list[str]) -> Answer[InstallConfig] | FormRejected:
        host, port, username, password, hosts = (one.strip() for one in values)
        if not host and (port or username or password or hosts):
            return FormRejected(translate("Proxy host is required when proxy fields are set"), {0: host})
        if port and (not port.isdecimal() or not 1 <= int(port) <= 65535):
            return FormRejected(translate("Proxy port must be between 1 and 65535"), {1: port})
        bypass = tuple(one for one in (item.strip() for item in hosts.split(",")) if one)
        if any(any(char.isspace() for char in item) for item in bypass):
            return FormRejected(translate("Bypass hosts must not contain spaces"), {1: hosts})
        selected = replace(config, proxy=ProxyConfig(
            kind=selected_kind, host=host, port=int(port or "0"), username=username,
            password=password, bypass=bypass,
        ))
        fetch.configure_proxy(selected.proxy)
        return Answer(Outcome.CHOSE, selected)

    return Form(
        title=translate("Proxy"),
        fields=[
            Field(
                label=translate("Proxy host"),
                value=current.host,
                accepts=Accepts.NO_SPACE,
            ),
            Field(
                label=translate("Proxy port"),
                value=str(current.port) if current.port else "",
                accepts=Accepts.DIGITS,
            ),
            Field(
                label=translate("Proxy username"),
                value=current.username,
                accepts=Accepts.NO_SPACE,
            ),
            Field(label=translate("Proxy password"), value=current.password, secret=True),
            Field(
                label=translate("Bypass hosts"),
                value=", ".join(current.bypass),
                accepts=Accepts.NO_SPACE,
                placeholder=translate("comma-separated host names"),
            ),
        ],
        footer=footer(translate),
        done=translate("Done"),
    ).run_validated(screen, validated)


def init_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    menu: Menu[InitSystem] = Menu(
        title=translate("Init system"),
        preamble=(translate("This selects the service manager and its matching profile."),),
        items=[Item(label=init.value, value=init) for init in InitSystem],
        footer=footer(translate),
        current=config.system.init,
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    init = answer.unwrap()
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
    if not typed.chosen:
        return Answer(typed.outcome)
    hashed = context.hash_password(typed.unwrap())
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, root_password_hash=hashed))
    )


def user_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """One normal account, on one screen.

    Five answers that are read together: three screens in a row meant the
    operator confirmed a password before seeing whether the account gets sudo,
    and a mismatch threw away the name as well. A wrong answer redraws the
    same form with a line saying what was wrong and everything else kept.

    An empty name leaves the system with root only, which is a choice a server
    install makes deliberately.
    """
    translate = context.translate
    existing = config.system.users[0] if config.system.users else None
    def validated(values: list[str]) -> Answer[InstallConfig] | FormRejected:
        name, first, again, sudo, extra = values
        stripped_name = name.strip()
        if not stripped_name:
            return Answer(
                Outcome.CHOSE, replace(config, system=replace(config.system, users=()))
            )
        wrong = _user_problem(stripped_name, first, again, extra, existing, translate)
        if wrong:
            return FormRejected(wrong, {1: "", 2: ""})
        groups, _ = atoms.split_use_flags(extra)
        user = User(
            name=stripped_name,
            # `plan/system.py:USER_GROUPS` is the one table for what every
            # account gets, so `wheel` is not named again here: an account that
            # declined sudo was put back in it.
            groups=groups,
            sudo=bool(sudo),
            password_hash=(
                context.hash_password(first)
                if first
                else (existing.password_hash if existing else "")
            ),
        )
        return Answer(
            Outcome.CHOSE, replace(config, system=replace(config.system, users=(user,)))
        )

    return Form(
        title=translate("User account"),
        fields=[
            Field(
                label=translate("User name"),
                value=existing.name if existing else "",
                placeholder=translate("empty for root only"),
            ),
            Field(label=translate("Password"), secret=True),
            Field(label=translate("Type it again"), secret=True),
            Field(
                label=translate("sudo"),
                toggle=True,
                value="x" if existing and existing.sudo else "",
            ),
            Field(
                label=_groups_label(config, context),
                value=" ".join(existing.groups) if existing else "",
                placeholder=translate("separated by spaces, such as plugdev kvm docker"),
            ),
        ],
        footer=footer(translate),
        done=translate("Done"),
    ).run_validated(screen, validated)


def _groups_label(config: InstallConfig, context: Context) -> str:
    """The row's label, naming what a chosen package already adds.

    On the label rather than in the field: the account is put in them whatever
    is typed here, so an editable box holding them would discard the answer it
    appears to take.
    """
    given = [one.value for one in automatic_values.user_groups(config, context.groups)]
    label = context.translate("Extra groups")
    return f"{label} (+{' '.join(given)})" if given else label


def _user_problem(
    name: str, first: str, again: str, extra: str, existing: User | None, translate: Catalog
) -> str:
    """Why the form cannot be accepted, in the words the operator reads.

    Returned rather than raised: a wrong answer redraws the form, and a screen
    that exits to the menu loses the four answers that were right.
    """
    if not USER_NAME.match(name):
        return translate("A user name is lowercase, starts with a letter, and has no spaces")
    if first != again:
        return translate("The two do not match.")
    if not first and not (existing and existing.password_hash):
        return translate("An account with no password cannot log in")
    _, bad = atoms.split_use_flags(extra)
    if bad:
        return f"{translate('Not a group name')}: {' '.join(bad)}"
    return ""


#: What `useradd` accepts, which is what the account has to match before the
#: install runs rather than after: NAME_REGEX in shadow's login.defs.
USER_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


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
_TABLE: Final[str] = "table"
_DROP: Final[str] = "drop"


T = TypeVar("T")


@dataclass(frozen=True)
class _FieldDescriptor(Generic[T]):
    key: str
    row: Callable[[T, Catalog], Item[str]]
    edit: Callable[[Screen, Context, T], T | None]

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


def _unreachable_here(site: mirrors.Site, context: Context) -> str:
    """Why this machine cannot fetch from a site, or empty.

    An IPv6-only machine reaches four of the mirrors on this list not at all:
    they publish no AAAA record, and one publishes an AAAA and does not answer
    on it. Saying so here is the difference between choosing another and
    finding out when the stage3 does not arrive.
    """
    # Both facts positive: this machine has IPv6 and has no IPv4. A machine
    # whose interface is still coming up reports neither, and refusing on that
    # would hide half the list from an operator who is about to configure it.
    if site.ipv6 or context.ipv4 or not context.ipv6:
        return ""
    return context.translate("this machine has no IPv4 and the mirror has no IPv6")


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
            preamble=(translate("These sources provide the Gentoo tree, distfiles, overlays, and binary packages."),),
            items=_mirror_fields(current, translate),
            footer=footer(translate),
            cursor=cursor,
        )
        answer = menu.run(screen)
        cursor = menu.cursor
        if not answer.chosen:
            return Answer(answer.outcome)
        field = answer.unwrap()
        if field == _DONE:
            return Answer(Outcome.CHOSE, _with_a_site(current))
        changed = _edit_mirror(screen, context, current, field)
        if changed is not None:
            current = changed


def _default_site(region: MirrorRegion, translate: Catalog) -> str:
    """What `_with_a_site` would adopt, drawn before it does."""
    offered = mirrors.gentoo_sites(region)
    return translate(offered[0].name) if offered else translate("none")


def _with_a_site(config: InstallConfig) -> InstallConfig:
    """The region's first mirror, for an operator who opened this screen and
    changed nothing.

    The row is required so that nobody installs from a mirror they never
    looked at, and opening the screen is looking at it. Leaving it unset
    instead made the row say `required` after it had been answered, which is
    the interface calling the operator wrong.
    """
    chosen = config.portage.mirrors
    if chosen.site:
        return config
    offered = mirrors.gentoo_sites(chosen.region)
    if not offered:
        return config
    return replace(
        config, portage=replace(config.portage, mirrors=replace(chosen, site=offered[0].key))
    )


def _mirror_fields(config: InstallConfig, translate: Catalog) -> list[Item[str]]:
    portage = config.portage
    chosen = portage.mirrors
    region = chosen.region
    site = chosen.site or mirrors.gentoo_sites(region)[0].key
    used = {overlay.name for overlay in portage.overlays}
    zh = mirrors.gentoozh(chosen.gentoo_zh)
    rows = [field.row(config, translate) for field in _MIRROR_FIELDS]
    rows += [
        # One row, not one per method: showing a git address under a webrsync
        # choice is the interface contradicting itself.
        Item(
            label=translate("Gentoo repository"),
            value="",
            detail=_tree_source(portage, region, site, translate),
        ),
        next(field.row(config, translate) for field in _ALL_MIRROR_FIELDS if field.key == _BINHOST),
        next(field.row(config, translate) for field in _ALL_MIRROR_FIELDS if field.key == _ZH_SITE),
    ]
    if "gentoo-zh" in used:
        rows += [field.row(config, translate) for field in _ZH_MIRROR_FIELDS]
    rows += [
        field.row(config, translate) for field in _OVERLAY_FIELDS
    ]
    rows.append(Item(label=translate("Done"), value=_DONE))
    return rows


def _site_name(region: MirrorRegion, site: str, translate: Catalog) -> str:
    """The chosen site's name, or `not set` when the region does not carry it.

    The two are set together everywhere, and a bare `next` over a list that
    does not hold it raised `StopIteration` out of the menu instead.
    """
    named = next((one.name for one in mirrors.gentoo_sites(region) if one.key == site), "")
    return translate(named) if named else translate("not set")


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
    if not field:
        # A row that only reports where a service will come from, derived from
        # the two choices above it.
        return None
    descriptor = next((one for one in _ALL_MIRROR_FIELDS if one.key == field), None)
    return descriptor.edit(screen, context, config) if descriptor else None


def _mirror_region_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    return Item(label=translate("Region"), value=_REGION, detail=config.portage.mirrors.region.value)


def _mirror_site_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    chosen = config.portage.mirrors
    site = chosen.site or mirrors.gentoo_sites(chosen.region)[0].key
    detail = _site_name(chosen.region, site, translate) if chosen.site else (
        f"{_default_site(chosen.region, translate)} ({translate('default')})"
    )
    return Item(label=translate("Gentoo mirror"), value=_SITE, detail=detail)


def _mirror_measure_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    return Item(
        label=translate("Measure them"), value=_MEASURE,
        detail=translate("yes") if config.portage.mirrors.speed_test else translate("no"),
    )


def _mirror_sync_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    return Item(label=translate("Repository sync"), value=_SYNC, detail=config.portage.sync.value)


def _edit_mirror_region(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig | None:
    current = config.portage.mirrors
    picked = Menu(title=context.translate("Region"), items=[Item(label=one.value, value=one) for one in MirrorRegion], footer=footer(context.translate), current=current.region).run(screen)
    if not picked.chosen:
        return None
    return replace(config, portage=replace(config.portage, mirrors=replace(current, region=picked.unwrap(), site="")))


def _edit_mirror_site(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig | None:
    current = config.portage.mirrors
    offered = mirrors.gentoo_sites(current.region)
    chosen = Menu(title=context.translate("Gentoo mirror"), items=[Item(label=context.translate(one.name), value=one.key, detail=f"{context.translate(one.area)}  {one.distfiles}", disabled_because=_unreachable_here(one, context)) for one in offered], footer=footer(context.translate), current=current.site).run(screen)
    return replace(config, portage=replace(config.portage, mirrors=replace(current, site=chosen.unwrap()))) if chosen.chosen else None


def _toggle_mirror_distfiles(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig:
    current = config.portage.mirrors
    return replace(config, portage=replace(config.portage, mirrors=replace(current, gentoo_distfiles=not current.gentoo_distfiles)))


def _toggle_mirror_measure(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig:
    current = config.portage.mirrors
    return replace(config, portage=replace(config.portage, mirrors=replace(current, speed_test=not current.speed_test)))


def _edit_mirror_sync(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig | None:
    return _pick(screen, context, config, "Repository sync", list(SYNC_METHODS), config.portage.sync, lambda chosen, value: replace(chosen, portage=replace(chosen.portage, sync=value)))


def _mirror_distfiles_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    return Item(
        label=translate("Gentoo distfiles"),
        value=_DISTFILES,
        detail=translate("in use") if config.portage.mirrors.gentoo_distfiles else translate("not used"),
    )


def _mirror_zh_distfiles_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    chosen = config.portage.mirrors
    return Item(label=translate("gentoo-zh distfiles"), value=_ZH_DISTFILES, detail=mirrors.gentoozh_distfiles(chosen.gentoo_zh)[0] if chosen.gentoo_zh_distfiles else translate("not used"))


def _mirror_zh_binhost_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    return Item(label=translate("gentoo-zh binary packages"), value=_ZH_BINHOST, detail=community_binhost(config.portage) if config.portage.binhost.community is not BinhostChannel.OFF else translate("not used"))


def _mirror_overlay_row(config: InstallConfig, translate: Catalog, name: str) -> Item[str]:
    return Item(label=name, value=name, detail=translate("in use") if name in {one.name for one in config.portage.overlays} else translate("not used"))


def _edit_mirror_overlay(
    screen: Screen, context: Context, config: InstallConfig, name: str
) -> InstallConfig:
    return _toggle_overlay(config, name)


def _overlay_descriptor(name: str) -> _FieldDescriptor[InstallConfig]:
    def row(config: InstallConfig, translate: Catalog) -> Item[str]:
        return _mirror_overlay_row(config, translate, name)

    def edit(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig:
        return _edit_mirror_overlay(screen, context, config, name)

    return _FieldDescriptor(name, row, edit)


_MIRROR_FIELDS: tuple[_FieldDescriptor[InstallConfig], ...] = (
    _FieldDescriptor(_REGION, _mirror_region_row, _edit_mirror_region),
    _FieldDescriptor(_SITE, _mirror_site_row, _edit_mirror_site),
    _FieldDescriptor(_DISTFILES, _mirror_distfiles_row, lambda s, c, x: _toggle_mirror_distfiles(s, c, x)),
    _FieldDescriptor(_MEASURE, _mirror_measure_row, lambda s, c, x: _toggle_mirror_measure(s, c, x)),
    _FieldDescriptor(_SYNC, _mirror_sync_row, _edit_mirror_sync),
)
_ZH_MIRROR_FIELDS: tuple[_FieldDescriptor[InstallConfig], ...] = (
    _FieldDescriptor(_ZH_DISTFILES, _mirror_zh_distfiles_row, lambda s, c, x: replace(x, portage=replace(x.portage, mirrors=replace(x.portage.mirrors, gentoo_zh_distfiles=not x.portage.mirrors.gentoo_zh_distfiles)))),
)
_OVERLAY_FIELDS = tuple(
    _overlay_descriptor(name)
    for name, _ in PLAIN_OVERLAYS
)
_ALL_MIRROR_FIELDS = _MIRROR_FIELDS + (
    _FieldDescriptor(_BINHOST, lambda config, translate: Item(label=translate("Gentoo binary packages"), value=_BINHOST, detail=mirrors.gentoo_binhost(config.portage.mirrors.region, config.portage.mirrors.site, config.portage.binhost.subarch) if config.portage.binhost.official else translate("not used")), lambda s, c, x: _edit_binhost(s, c, x)),
    _FieldDescriptor(_ZH_BINHOST, _mirror_zh_binhost_row, lambda s, c, x: _pick(s, c, x, "gentoo-zh binary packages", list(GENTOOZH_CHANNELS), x.portage.binhost.community, lambda chosen, value: replace(chosen, portage=replace(chosen.portage, binhost=replace(chosen.portage.binhost, community=value))))),
    _FieldDescriptor(_ZH_SITE, lambda config, translate: Item(label=translate("gentoo-zh"), value=_ZH_SITE, detail=translate(mirrors.gentoozh(config.portage.mirrors.gentoo_zh).name) if "gentoo-zh" in {one.name for one in config.portage.overlays} else translate("not used")), lambda s, c, x: _edit_gentoozh(s, c, x)),
    *_ZH_MIRROR_FIELDS,
    *_OVERLAY_FIELDS,
)


V = TypeVar("V")


def _current_menu(
    screen: Screen,
    context: Context,
    title: str,
    items: Sequence[Item[V]],
    current: V,
) -> Answer[V]:
    """A single-choice menu whose current value cannot be omitted."""
    return Menu(
        title=title,
        items=items,
        footer=footer(context.translate),
        current=current,
    ).run(screen)


def _pick(
    screen: Screen,
    context: Context,
    config: InstallConfig,
    title: str,
    offered: list[tuple[V, str]],
    current: V,
    apply: Callable[[InstallConfig, V], InstallConfig],
) -> InstallConfig | None:
    """One value from a short list, each row carrying what it costs."""
    translate = context.translate
    answer = _current_menu(
        screen,
        context,
        translate(title),
        [
            Item(label=str(getattr(value, "value", value)), value=value, detail=translate(reason))
            for value, reason in offered
        ],
        current,
    )
    if not answer.chosen:
        return None
    return apply(config, answer.unwrap())


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
        title=translate("Gentoo binary packages"),
        preamble=(translate("Binary packages reduce compilation; source builds remain the fallback."),),
        items=items,
        footer=footer(translate),
        current=(config.portage.binhost.official, config.portage.binhost.subarch),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return None
    official, subarch = answer.unwrap()
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
    used = any(one.name == "gentoo-zh" for one in config.portage.overlays)
    answer = _current_menu(
        screen,
        context,
        translate("gentoo-zh"),
        items,
        config.portage.mirrors.gentoo_zh if used else None,
    )
    if not answer.chosen:
        return None
    picked = answer.unwrap()
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
    `in use` or `not used` asks what the row has answered."""
    portage = config.portage
    kept = tuple(one for one in portage.overlays if one.name != name)
    if len(kept) == len(portage.overlays):
        uri = next(where for offered, where in PLAIN_OVERLAYS if offered == name)
        kept = (*kept, Overlay(name=name, sync_uri=uri))
    return replace(config, portage=replace(portage, overlays=kept))


def _common_violations(candidates: Sequence[InstallConfig]) -> set[compat.Rule]:
    """Rules every candidate breaks, which no choice on this screen can fix.

    Reporting them on each row disables the whole screen and points the
    operator at a setting that is not on it.
    """
    broken = [set(compat.violations(one)) for one in candidates]
    return set.intersection(*broken) if broken else set()


def bootloader_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Every excluded bootloader is drawn with the rule's own sentence."""
    translate = context.translate
    # Only what this choice introduces: `violations` reports every rule the
    # whole configuration breaks, so one unrelated problem elsewhere greyed out
    # all three rows and named a reason that pointed at another screen.
    shared = _common_violations(
        [replace(config, bootloader=replace(config.bootloader, kind=one)) for one in Bootloader]
    )
    items: list[Item[Bootloader]] = []
    for kind in Bootloader:
        candidate = replace(config, bootloader=replace(config.bootloader, kind=kind))
        broken = [one for one in compat.violations(candidate) if one not in shared]
        items.append(
            Item(
                label=kind.value,
                value=kind,
                disabled_because=translate(broken[0].reason) if broken else "",
            )
        )
    menu: Menu[Bootloader] = Menu(
        title=translate("Bootloader"),
        preamble=(translate("The bootloader installs the files firmware uses to start the kernel."),),
        items=items,
        footer=footer(translate),
        current=config.bootloader.kind,
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, bootloader=replace(config.bootloader, kind=answer.unwrap())),
    )


#: What each kernel choice costs and what it gives. All three are dist-kernels:
#: the package builds and installs itself, so none of them is a source tree the
#: installer would have to configure.
KERNELS: tuple[tuple[KernelSource, str], ...] = (
    (KernelSource.DIST_BIN, "prebuilt"),
    (KernelSource.DIST_SOURCE, "built here"),
    (KernelSource.CJK_BIN, "prebuilt, cjktty for CJK on the console, from gentoo-zh"),
    (KernelSource.CJK, "built here, cjktty for CJK on the console, from gentoo-zh"),
)


def _while_reading(
    screen: Screen, context: Context, package: str
) -> tuple[tuple[str, bool], ...]:
    """Read the versions with the reason for the pause on the screen.

    The lookup is a network request and the interface stopped answering keys
    while it ran, with nothing to say why: on a slow link that reads as a
    program that has died. Drawn before the call and left there, because the
    call is what returns.
    """
    screen.clear()
    band(screen, 0, context.translate("Kernel"), package)
    screen.write(2, 2, context.translate("reading the versions this package offers"))
    screen.show()
    return context.kernel_versions(package)


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
    offered = _within(_while_reading(screen, context, package), ceiling)
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
        Outcome.CHOSE, replace(config, kernel=replace(config.kernel, version=answer.unwrap()))
    )


def logger_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Which system logger, which only openrc needs.

    A stage3 has none, so an openrc install without this keeps no log of its
    own boot. systemd carries journald and the row says so rather than
    offering a second logger for the same lines.
    """
    translate = context.translate
    if config.system.init is InitSystem.SYSTEMD:
        say(screen, context, translate("systemd logs to journald; no other logger is needed."))
        return Answer(Outcome.BACK)
    menu: Menu[Logger] = Menu(
        title=translate("System logger"),
        preamble=(translate("This service records messages from OpenRC after boot."),),
        items=[
            Item(label=one.value, value=one, detail=translate(choice.reason))
            for one, choice in plan_system.LOGGERS.items()
        ],
        footer=footer(translate),
        current=config.system.logger,
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, logger=answer.unwrap()))
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
        preamble=(translate("The keyword policy controls which versions Portage may accept."),),
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
                    "the ~amd64 testing channel, so fewer packages are available from a binhost"
                ),
            ),
        ],
        footer=footer(translate),
        current=config.portage.keywords,
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, portage=replace(config.portage, keywords=answer.unwrap())),
    )


def kernel_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    menu: Menu[KernelSource] = Menu(
        title=translate("Kernel"),
        preamble=(translate("The kernel provides the boot environment and CJK console support."),),
        items=[
            # The package name, not the enum value: `dist-bin` says nothing
            # about which kernel is about to be installed.
            Item(label=KERNEL_PACKAGES[source], value=source, detail=translate(reason))
            for source, reason in KERNELS
        ],
        footer=footer(translate),
        current=config.kernel.source,
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()
    changed = replace(config, kernel=replace(config.kernel, source=chosen))
    if chosen in compat.CJK_KERNELS:
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
        say(screen, context, translate("This kernel has no cjktty: console CJK is off."))
        changed = replace(changed, system=replace(changed.system, console_cjk=False))
    return Answer(Outcome.CHOSE, changed)


#: What a machine with no desktop is built against. Every other answer names
#: its own profile in `data/profiles/<name>.toml`.
BASE_PROFILE: Final[str] = "default/linux/amd64/23.0"


def desktop_profiles(groups: Groups, profile_paths: Sequence[str] = ()) -> dict[str, str]:
    """The desktops and the profile each is built against, read from the files
    that declare them: a table beside `data/profiles/` meant a desktop added
    there never reached the menu, and one added here installed nothing."""
    release = next(
        (path.split("/")[3] for path in profile_paths if path.startswith("default/linux/amd64/")),
        BASE_PROFILE.split("/")[3],
    )
    prefix = f"default/linux/amd64/{release}"
    found = {"": prefix}
    for name, group in groups.items():
        if group.profile:
            found[name] = group.profile.replace(BASE_PROFILE, prefix, 1)
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
        Item(
            label=translate("no desktop") if not name else name,
            value=name,
            # Plasma and GNOME both bring `wayland`, and the operator reads
            # that here rather than after choosing.
            detail=detail.get(name, "")
            + _adds(config, context, lambda packages, one: replace(packages, desktop=one), name),
        )
        for name in sorted(desktop_profiles(context.groups, context.profile_paths))
    ]
    menu: Menu[str] = Menu(
        title=translate("Desktop and applications"),
        preamble=(translate("The desktop selects its profile and proposes login and network services."),),
        items=items,
        current=config.packages.desktop,
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    desktop = answer.unwrap()
    changed = replace(config, packages=replace(config.packages, desktop=desktop))
    if desktop != config.packages.desktop:
        changed = _set_input_configuration(changed, context.groups, None)
    if not _profile_was_chosen(config, context.groups):
        # Only while the profile is still the one the last desktop implied.
        # Overwriting it regardless threw away a profile the operator had
        # picked on purpose, such as no-multilib.
        changed = replace(
            changed,
            portage=replace(
                config.portage,
                profile=_profile_for(
                    desktop_profiles(context.groups, context.profile_paths)[desktop], config.system.init
                ),
            ),
        )
    manager = config.packages.display_manager
    was_proposed = _has_derived(context, ValueKind.DISPLAY_MANAGER, manager)
    kept_manager = (
        manager
        if manager and not was_proposed and desktop != config.packages.desktop
        else ""
    )
    changed = _desktop_proposes(changed, config, context, desktop)
    login = LOGIN_SCREEN.get(desktop, "")
    effects = derive_effects(config, changed, context, kept_manager)
    answered = settle(
        screen,
        context,
        config,
        changed,
        kept_display_manager=kept_manager,
    )
    if answered.chosen:
        _record_derived(context, answered.unwrap(), effects)
    return answered


#: What each desktop's own login screen is. Proposed rather than fixed: the
#: row stays editable, and pulling the login screen out of the desktop profile
#: was right — leaving it empty was not, because the row is then required and
#: red on a menu whose operator has already said which desktop they want.
LOGIN_SCREEN: Final[dict[str, str]] = {
    "plasma": "sddm",
    "plasma-full": "sddm",
    "gnome": "gdm",
    "gnome-full": "gdm",
    "xfce": "lightdm",
}


@dataclass(frozen=True)
class Effects:
    """Derived values changed by one TUI choice."""

    use_flags: tuple[str, ...] = ()
    video_cards: tuple[str, ...] = ()
    user_groups: tuple[str, ...] = ()
    profile: str = ""
    display_manager_changed: bool = False
    networking_changed: bool = False
    kept_display_manager: str = ""
    withdrawn_use: tuple[str, ...] = ()
    withdrawn_video_cards: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        """Whether the choice has a value to confirm."""
        return bool(
            self.use_flags
            or self.video_cards
            or self.user_groups
            or self.profile
            or self.display_manager_changed
            or self.networking_changed
            or self.kept_display_manager
        )

    @property
    def editable_row(self) -> Callable[..., Answer[InstallConfig]] | None:
        """The row containing values that can be inspected after consent."""
        if self.use_flags:
            return use_flags_screen
        if self.video_cards:
            return video_cards_screen
        return None


def derive_effects(
    before: InstallConfig,
    after: InstallConfig,
    context: Context,
    kept_display_manager: str = "",
) -> Effects:
    """Derive every value one choice adds, replaces, or withdraws."""
    return Effects(
        use_flags=_new(automatic_values.use_flags, before, after, context.groups),
        video_cards=_new(automatic_values.video_cards, before, after, context.groups),
        user_groups=_new(automatic_values.user_groups, before, after, context.groups),
        profile=after.portage.profile if after.portage.profile != before.portage.profile else "",
        display_manager_changed=(
            after.packages.display_manager != before.packages.display_manager
        ),
        networking_changed=after.system.networking is not before.system.networking,
        kept_display_manager=kept_display_manager,
        withdrawn_use=tuple(
            one.value
            for one in context.provenance
            if one.kind is ValueKind.USE_FLAG and one.source is ValueSource.DERIVED
        ),
        withdrawn_video_cards=tuple(
            one.value
            for one in context.provenance
            if one.kind is ValueKind.VIDEO_CARD and one.source is ValueSource.DERIVED
        ),
    )


def apply_effects(after: InstallConfig, effects: Effects) -> InstallConfig:
    """Pin additions while removing only values derived by the replaced choice."""
    kept_use = tuple(one for one in after.portage.use if one not in effects.withdrawn_use)
    kept_cards = tuple(
        one for one in after.portage.video_cards if one not in effects.withdrawn_video_cards
    )
    return replace(
        after,
        portage=replace(
            after.portage,
            use=(*kept_use, *effects.use_flags),
            video_cards=(*kept_cards, *effects.video_cards),
        ),
    )


def _desktop_proposes(
    changed: InstallConfig, before: InstallConfig, context: Context, desktop: str
) -> InstallConfig:
    """The login screen and the network manager a desktop implies.

    Only over what the operator has not set: a login screen they picked stands,
    and so does a network manager. The Plasma and GNOME profiles already carry
    `USE=networkmanager`, and nothing moved `system.networking` with it, so the
    installed desktop had the settings panel and not the service behind it.
    """
    if (
        _has_derived(context, ValueKind.DISPLAY_MANAGER, before.packages.display_manager)
    ):
        changed = replace(
            changed,
            packages=replace(changed.packages, display_manager=""),
        )
    if not desktop:
        return changed
    login = LOGIN_SCREEN.get(desktop, "")
    if login and not changed.packages.display_manager and login in context.groups:
        changed = replace(
            changed, packages=replace(changed.packages, display_manager=login)
        )
    if not _has_operator(context, ValueKind.NETWORKING, before.system.networking.value):
        changed = replace(
            changed,
            system=replace(changed.system, networking=Networking.NETWORKMANAGER_WPA),
        )
    return changed


def settle(
    screen: Screen,
    context: Context,
    before: InstallConfig,
    after: InstallConfig,
    kept_display_manager: str = "",
) -> Answer[InstallConfig]:
    """Confirm what a choice changes outside its own row, then write it down.

    Choosing a driver or a desktop moves `VIDEO_CARDS`, `USE` and the profile.
    Deriving those silently at build time meant the operator met them for the
    first time in the installed system, so they are listed here and, once
    confirmed, put into the configuration as the operator's own values. Pinned
    rather than left derived: a value in `portage.use` survives a later change
    to the desktop, and `automatic.use_flags` stops reporting a flag that is
    already there, so nothing appears twice.

    Declining cancels the choice. The flags are not optional extras that come
    with it; they are what makes it work.
    """
    effects = derive_effects(before, after, context, kept_display_manager)
    if not effects.has_changes:
        return Answer(Outcome.CHOSE, after)
    translate = context.translate
    lines = []
    if effects.video_cards:
        lines.append(f"VIDEO_CARDS: {' '.join(effects.video_cards)}")
    if effects.use_flags:
        lines.append(f"USE: {' '.join(effects.use_flags)}")
    if effects.display_manager_changed:
        lines.append(
            f"{translate('Display manager')}: {after.packages.display_manager}"
        )
    elif effects.kept_display_manager:
        lines.append(
            f"{translate('Display manager')}: {effects.kept_display_manager} "
            f"({translate('kept')})"
        )
    if effects.networking_changed:
        lines.append(f"{translate('Network')}: {after.system.networking.value}")
    if effects.user_groups:
        # Not pinned into the account: `AddUserToGroups` derives them from the
        # same catalog at build time, so the account is put in them whether or
        # not one exists when the driver is chosen.
        lines.append(f"{translate('Extra groups')}: {' '.join(effects.user_groups)}")
    if effects.profile:
        lines.append(f"{translate('Profile')}: {effects.profile}")
    # A third answer only when there is something to open. The row is derived
    # from what actually changed: with a profile and no flags it offered `Yes,
    # and open VIDEO_CARDS`, which edits a value this choice did not touch.
    where = effects.editable_row
    named = translate("USE flags") if effects.use_flags else translate("VIDEO_CARDS")
    # Yes first and preselected. Declining cancels the desktop the operator
    # just chose, and these values are what makes it work rather than extras
    # that come with it, so the cursor starting on `No` offered the refusal as
    # the default answer to a question the choice itself had already implied.
    items = [
        Item(label=translate("Yes"), value="yes"),
        Item(label=translate("No"), value="no"),
    ]
    if where is not None:
        # The row the values land on is somewhere else in the menu, and an
        # operator who wants to look at them now would otherwise have to leave,
        # find it, and remember what they were checking.
        items.append(
            Item(
                label=f"{translate('Yes, and open')} {named}",
                value="open",
                detail=translate("editable"),
            )
        )
    asked: Menu[str] = Menu(
        title=translate("This choice also sets"),
        preamble=tuple(lines),
        items=items,
        footer=footer(translate),
        current="yes",
    )
    answered = asked.run(screen)
    if not answered.chosen or answered.unwrap() == "no":
        return Answer(Outcome.BACK, before)
    # Pinned without what the choice being replaced had derived. Adding to
    # `portage.use` and never taking anything out meant switching from Plasma
    # to GNOME left `qt6` and `sddm` behind and read as
    # `wayland qt6 networkmanager sddm gnome gtk`. What the operator typed is
    # not derivable and stays; what the last choice derived goes with it.
    pinned = apply_effects(after, effects)
    if answered.unwrap() == "open" and where is not None:
        opened = where(screen, pinned, context)
        if opened.chosen:
            return Answer(Outcome.CHOSE, opened.unwrap())
        if opened.outcome is Outcome.CANCELLED:
            # Cancelling reaches the application's leave confirmation. Reading
            # it as `keep what was pinned` committed a desktop and a profile
            # the operator had just refused.
            return Answer(Outcome.CANCELLED)
    return Answer(Outcome.CHOSE, pinned)


def _has_derived(context: Context, kind: ValueKind, value: str) -> bool:
    """Whether this value came from the current automatic proposal."""
    return ValueProvenance(kind, value, ValueSource.DERIVED) in context.provenance


def _has_operator(context: Context, kind: ValueKind, value: str) -> bool:
    """Whether the operator selected this value in its own row."""
    return ValueProvenance(kind, value, ValueSource.OPERATOR) in context.provenance


def _record_derived(context: Context, after: InstallConfig, effects: Effects) -> None:
    """Replace the previous choice's derived values with the new choice's."""
    context.provenance = {
        one for one in context.provenance if one.source is not ValueSource.DERIVED
    }
    context.provenance.update(
        ValueProvenance(ValueKind.USE_FLAG, value, ValueSource.DERIVED)
        for value in effects.use_flags
    )
    context.provenance.update(
        ValueProvenance(ValueKind.VIDEO_CARD, value, ValueSource.DERIVED)
        for value in effects.video_cards
    )
    if effects.networking_changed:
        context.provenance.add(
            ValueProvenance(
                ValueKind.NETWORKING,
                after.system.networking.value,
                ValueSource.DERIVED,
            )
        )
    if effects.display_manager_changed:
        context.provenance.add(
            ValueProvenance(
                ValueKind.DISPLAY_MANAGER,
                after.packages.display_manager,
                ValueSource.DERIVED,
            )
        )


def _record_operator(context: Context, kind: ValueKind, values: Sequence[str]) -> None:
    """Mark values edited in their own row as operator choices."""
    kept = {
        one
        for one in context.provenance
        if one.kind is not kind or one.source is not ValueSource.DERIVED
    }
    context.provenance = kept | {
        ValueProvenance(kind, value, ValueSource.OPERATOR) for value in values
    }


def _new(
    derive: Callable[[InstallConfig, Groups], tuple[automatic_values.Added, ...]],
    before: InstallConfig,
    after: InstallConfig,
    groups: Groups,
) -> tuple[str, ...]:
    """What the change added, in order, without what was already there.

    Only what the same function said before. Unioning `portage.use` as well
    crossed two namespaces: `pipewire` is both a USE flag and the group its
    ebuild asks the account to join, so pinning the flag hid the group. Each
    `derive` already drops what the operator typed.
    """
    had = {one.value for one in derive(before, groups)}
    return tuple(one.value for one in derive(after, groups) if one.value not in had)


#: The graphics groups, in the order the menu lists them, and what each is
#: for. Kept out of the applications list: a driver is one choice, not a set of
#: things to tick.
GRAPHICS: tuple[tuple[str, str], ...] = (
    ("", "no driver package: i915, amdgpu, radeon and nouveau are in the kernel"),
    ("intel", "i915 and xe, with the firmware they need"),
    ("amdgpu", "GCN 1.2 and newer"),
    ("radeon", "AMD up to Sea Islands"),
    ("nouveau", "the in-kernel NVIDIA driver"),
    ("nvidia", "proprietary, widens ACCEPT_LICENSE, and blacklists nouveau itself"),
    ("virtual-machine", "virtio-gpu, QXL and VMware, with fbdev left as the fallback"),
)

#: The display managers. A desktop no longer names one, because which login
#: screen to run is a decision of its own.
DISPLAY_MANAGERS: tuple[tuple[str, str], ...] = (
    ("", "a text console login"),
    ("sddm", "the one Plasma expects"),
    ("gdm", "the one GNOME expects, and it installs gnome-shell whatever you run"),
    ("lightdm", "the one Xfce expects"),
    ("greetd", "a console greeter"),
)


def graphics_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Which drivers, which is what VIDEO_CARDS and the firmware follow.

    More than one can be ticked: a hybrid machine has more than one adapter,
    and an AMD laptop with an NVIDIA card needs `amdgpu radeonsi nvidia`. Each
    row says what it adds, so the line the ticks build is readable before any
    of them is pressed.
    """
    translate = context.translate
    named = tuple((name, reason) for name, reason in GRAPHICS if name)
    items = [
        Item(
            label=name,
            value=name,
            detail=translate(reason)
            + _adds(config, context, lambda packages, one: replace(packages, graphics=(one,)), name),
        )
        for name, reason in named
    ]
    ticked = set(config.packages.graphics)
    while True:
        menu: MultipleChoiceMenu[str] = MultipleChoiceMenu(
            title=translate("Graphics"),
            preamble=(translate("The drivers set VIDEO_CARDS and install required firmware or packages."),),
            items=items,
            selected={index for index, item in enumerate(items) if item.value in ticked},
            footer=footer(translate),
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        chosen = replace(
            config, packages=replace(config.packages, graphics=tuple(answer.unwrap()))
        )
        # Checked here rather than at the Install row: the conflict is between
        # two ticks on this screen and this is where either can be unticked.
        clash = driver_conflict(chosen, context.groups)
        if not clash:
            return settle(screen, context, config, chosen)
        ticked = set(answer.unwrap())
        say(screen, context, translate(clash))


def display_manager_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    # A manager with no desktop installs a login screen that has no session to
    # start, so every entry but `none` says to pick a desktop first.
    without = "" if config.packages.desktop else context.translate("choose a desktop first")
    chosen = _one_group(
        screen,
        config,
        context,
        "Display manager",
        DISPLAY_MANAGERS,
        lambda packages, name: replace(packages, display_manager=name),
        current=config.packages.display_manager,
        unavailable=lambda name: without if name else "",
        say_what_it_adds=True,
    )
    if not chosen.chosen:
        return chosen
    answered = settle(screen, context, config, chosen.unwrap())
    if answered.chosen:
        _record_operator(
            context,
            ValueKind.DISPLAY_MANAGER,
            (answered.unwrap().packages.display_manager,),
        )
    return answered


def _needs_an_overlay(
    wanted: Sequence[str], have: set[str], translate: Catalog
) -> str:
    """Why a group cannot be ticked: its packages are in no repository this
    configuration adds. Said here rather than at the Install row, because this
    is the screen the tick is on."""
    missing = [name for name in wanted if name not in have]
    if not missing:
        return ""
    return f"{translate('needs the overlay')} {', '.join(missing)}"


def _one_group(
    screen: Screen,
    config: InstallConfig,
    context: Context,
    title: str,
    offered: tuple[tuple[str, str], ...],
    apply: Callable[[PackagesConfig, str], PackagesConfig],
    current: str,
    unavailable: Callable[[str], str] = lambda name: "",
    say_what_it_adds: bool = False,
) -> Answer[InstallConfig]:
    """A row that holds one group name, drawn from a table of them."""
    translate = context.translate
    answer = _current_menu(
        screen,
        context,
        translate(title),
        [
            Item(
                label=name or translate("none"),
                value=name,
                detail=translate(reason) + _adds(config, context, apply, name)
                if say_what_it_adds
                else translate(reason),
                disabled_because=unavailable(name),
            )
            for name, reason in offered
        ],
        current,
    )
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, packages=apply(config.packages, answer.unwrap()))
    )


def _adds(
    config: InstallConfig,
    context: Context,
    apply: Callable[[PackagesConfig, str], PackagesConfig],
    name: str,
) -> str:
    """What choosing this row would put in `make.conf`, on the row itself.

    `settle` asks the same question after the choice, and the panel lists the
    values on the USE row afterwards. This is the third place, and the earliest
    one: an operator comparing `nouveau` against `nvidia` reads what each costs
    without having to pick one to find out.
    """
    would = replace(config, packages=apply(config.packages, name))
    effects = derive_effects(config, would, context)
    added = (*effects.video_cards, *effects.use_flags)
    return f" (+{' '.join(added)})" if added else ""


FRAMEWORK_LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("fcitx", "Fcitx 5"),
    ("ibus", "IBus"),
)


def input_method_groups(groups: Groups) -> tuple[str, ...]:
    """Framework and engine groups, classified by their catalog metadata."""
    return tuple(sorted(name for name, group in groups.items() if group.input_framework))


def input_framework_groups(groups: Groups) -> tuple[str, ...]:
    """Framework providers from the plan layer's canonical registry."""
    return tuple(
        sorted(name for name in FRAMEWORK_GROUPS.values() if name in groups)
    )


def input_engine_groups(groups: Groups, framework: str) -> tuple[str, ...]:
    """Selectable engines belonging to one framework."""
    providers = set(input_framework_groups(groups))
    return tuple(
        sorted(
            name
            for name, group in groups.items()
            if group.input_framework == framework
            and group.input_method
            and name not in providers
        )
    )


def input_engine_sections(
    groups: Groups, framework: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Engine groups under the language each catalog entry declares."""
    names = input_engine_groups(groups, framework)
    languages = sorted({groups[name].input_language for name in names})
    return tuple(
        (
            language,
            tuple(name for name in names if groups[name].input_language == language),
        )
        for language in languages
        if language
    )


def _selected_input_framework(config: InstallConfig, groups: Groups) -> str:
    frameworks = set(input_framework_groups(groups))
    selected = [
        name for name in config.packages.applications if name in frameworks
    ]
    if len(selected) > 1:
        raise ConfigError("more than one input framework is selected")
    return selected[0] if selected else ""


def select_input_framework(
    config: InstallConfig, groups: Groups, framework_group: str
) -> InstallConfig:
    """Choose one framework and discard every earlier framework and engine."""
    frameworks = set(input_framework_groups(groups))
    if framework_group and framework_group not in frameworks:
        raise ConfigError(f"{framework_group!r} is not an input framework group")
    current = _selected_input_framework(config, groups)
    if current == framework_group:
        return config
    input_groups = set(input_method_groups(groups))
    kept = tuple(
        name for name in config.packages.applications if name not in input_groups
    )
    chosen = (framework_group,) if framework_group else ()
    return replace(
        config,
        packages=replace(config.packages, applications=(*kept, *chosen)),
    )


def select_input_engines(
    config: InstallConfig, groups: Groups, engine_groups: Sequence[str]
) -> InstallConfig:
    """Select engines only when their framework is already selected."""
    framework_group = _selected_input_framework(config, groups)
    if engine_groups and not framework_group:
        raise ConfigError("an input engine needs its framework selected first")
    framework = groups[framework_group].input_framework if framework_group else ""
    offered = set(input_engine_groups(groups, framework))
    wrong = [name for name in engine_groups if name not in offered]
    if wrong:
        raise ConfigError(
            f"the {', '.join(wrong)} engine groups do not belong to {framework}"
        )
    every_engine = {
        name
        for name in input_method_groups(groups)
        if name not in input_framework_groups(groups)
    }
    kept = tuple(
        name for name in config.packages.applications if name not in every_engine
    )
    return replace(
        config,
        packages=replace(config.packages, applications=(*kept, *engine_groups)),
    )


def cjk_font_groups(groups: Groups) -> tuple[str, ...]:
    """Font groups classified by declared family metadata."""
    return tuple(sorted(name for name, group in groups.items() if group.font_family))


def font_sections(groups: Groups) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Font groups in the category order that also owns generic aliases."""
    return tuple(
        (
            category.heading,
            tuple(
                name
                for name in cjk_font_groups(groups)
                if groups[name].font_category == category.catalog
            ),
        )
        for category in FontCategory
        if any(
            groups[name].font_category == category.catalog
            for name in cjk_font_groups(groups)
        )
    )


def select_cjk_fonts(
    config: InstallConfig,
    groups: Groups,
    chosen: Sequence[str],
    preferred: Sequence[str],
) -> InstallConfig:
    """Store each generic's preferred font before other installed fonts."""
    offered = set(cjk_font_groups(groups))
    wrong = [name for name in chosen if name not in offered]
    if wrong:
        raise ConfigError(f"the {', '.join(wrong)} groups do not provide CJK fonts")
    missing = [name for name in preferred if name not in chosen]
    if missing:
        raise ConfigError("a preferred font must also be installed")
    unsupported = [name for name in preferred if not groups[name].font_cjk]
    if unsupported:
        raise ConfigError("a preferred CJK font must provide CJK glyphs")
    generics = [
        FontCategory.selected(groups[name].font_category).generic for name in preferred
    ]
    if len(generics) != len(set(generics)):
        raise ConfigError("only one font may be preferred for each generic")
    fonts = set(cjk_font_groups(groups))
    kept = tuple(
        name for name in config.packages.applications if name not in fonts
    )
    ordered = (*preferred, *(name for name in chosen if name not in preferred))
    return replace(
        config,
        packages=replace(config.packages, applications=(*kept, *ordered)),
    )


def preferred_font_groups(config: InstallConfig, groups: Groups) -> tuple[str, ...]:
    """The first selected font for each generic owns its preferred mark."""
    selected = [
        name for name in config.packages.applications if name in cjk_font_groups(groups)
    ]
    found: list[str] = []
    generics: set[str] = set()
    for name in selected:
        if not groups[name].font_cjk:
            continue
        generic = FontCategory.selected(groups[name].font_category).generic
        if generic not in generics:
            generics.add(generic)
            found.append(name)
    return tuple(found)


def font_configuration_group(groups: Groups, decision: str) -> str:
    names = [
        name for name, group in groups.items() if group.font_configuration == decision
    ]
    if len(names) != 1:
        raise ConfigError(
            f"the catalog must declare exactly one {decision} font configuration group"
        )
    return names[0]


def input_configuration_group(groups: Groups, decision: str) -> str:
    names = [
        name
        for name, group in groups.items()
        if group.input_configuration == decision
    ]
    if len(names) != 1:
        raise ConfigError(
            f"the catalog must declare exactly one {decision} input configuration group"
        )
    return names[0]


def configuration_groups(groups: Groups) -> tuple[str, ...]:
    return (
        font_configuration_group(groups, FONT_CONFIGURATION_ENABLED),
        font_configuration_group(groups, FONT_CONFIGURATION_DISABLED),
        input_configuration_group(groups, INPUT_CONFIGURATION_ENABLED),
        input_configuration_group(groups, INPUT_CONFIGURATION_DISABLED),
    )


def _set_font_configuration(
    config: InstallConfig, groups: Groups, enabled: bool | None
) -> InstallConfig:
    accepted = font_configuration_group(groups, FONT_CONFIGURATION_ENABLED)
    declined = font_configuration_group(groups, FONT_CONFIGURATION_DISABLED)
    decisions = {accepted, declined}
    kept = tuple(
        name for name in config.packages.applications if name not in decisions
    )
    chosen = () if enabled is None else ((accepted,) if enabled else (declined,))
    return replace(
        config,
        packages=replace(config.packages, applications=(*kept, *chosen)),
    )


def _set_input_configuration(
    config: InstallConfig, groups: Groups, enabled: bool | None
) -> InstallConfig:
    accepted = input_configuration_group(groups, INPUT_CONFIGURATION_ENABLED)
    declined = input_configuration_group(groups, INPUT_CONFIGURATION_DISABLED)
    decisions = {accepted, declined}
    kept = tuple(
        name for name in config.packages.applications if name not in decisions
    )
    chosen = () if enabled is None else ((accepted,) if enabled else (declined,))
    return replace(
        config,
        packages=replace(config.packages, applications=(*kept, *chosen)),
    )


def _consent_screen(
    screen: Screen,
    config: InstallConfig,
    context: Context,
    title: str,
    summary: str,
) -> Answer[InstallConfig]:
    translate = context.translate
    declined = font_configuration_group(
        context.groups, FONT_CONFIGURATION_DISABLED
    )
    answer = Menu(
        title=translate(title),
        preamble=(summary,),
        items=[
            Item(label=translate("Yes"), value="yes"),
            Item(label=translate("No"), value="no"),
        ],
        current=("no" if declined in config.packages.applications else "yes"),
        footer=footer(translate),
    ).run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    edited = _set_font_configuration(
        config, context.groups, answer.unwrap() == "yes"
    )
    return Answer(Outcome.CHOSE, edited)


def _input_consent_screen(
    screen: Screen,
    config: InstallConfig,
    context: Context,
    summary: str,
) -> Answer[InstallConfig]:
    translate = context.translate
    declined = input_configuration_group(
        context.groups, INPUT_CONFIGURATION_DISABLED
    )
    answer = Menu(
        title=translate("Input method configuration"),
        preamble=(summary,),
        items=[
            Item(label=translate("Yes"), value="yes"),
            Item(label=translate("No"), value="no"),
        ],
        current=(
            "no" if declined in config.packages.applications else "yes"
        ),
        footer=footer(translate),
    ).run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    edited = _set_input_configuration(
        config, context.groups, answer.unwrap() == "yes"
    )
    return Answer(Outcome.CHOSE, edited)


def _input_configuration_summary(
    config: InstallConfig, groups: Groups, framework: str, translate: Catalog
) -> str:
    selected = [
        groups[name]
        for name in config.packages.applications
        if name in groups and groups[name].input_method
    ]
    engines = ", ".join(group.input_method for group in selected)
    subject = engines or translate("the selected framework")
    desktop = groups.get(config.packages.desktop)
    if (
        framework == "fcitx"
        and engines
        and desktop is not None
        and desktop.input_method_launcher
    ):
        return translate(
            "Write /etc/xdg/kwinrc and the Fcitx profile for: {engines}."
        ).format(
            engines=subject
        )
    sources = ", ".join(group.input_source for group in selected if group.input_source)
    if framework == "ibus" and config.packages.desktop == "gnome" and sources:
        return translate(
            "Write /etc/dconf/db/local.d/00-gentoo-install-input-sources with "
            "IBus engines: {engines}."
        ).format(engines=sources)
    if framework == "fcitx":
        return translate(
            "Write the Fcitx profile and input environment for: {engines}."
        ).format(engines=subject)
    return translate("Write the IBus input environment.")


def input_method_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    translate = context.translate
    frameworks = input_framework_groups(context.groups)
    labels: dict[str, str] = dict(FRAMEWORK_LABELS)
    framework_items = [Item(label=translate("none"), value="")]
    framework_items += [
        Item(
            label=translate(labels.get(context.groups[name].input_framework, name)),
            value=name,
        )
        for name in frameworks
    ]
    framework_answer = Menu(
        title=translate("Input framework"),
        items=framework_items,
        current=_selected_input_framework(config, context.groups),
        footer=footer(translate),
    ).run(screen)
    if not framework_answer.chosen:
        return Answer(framework_answer.outcome)
    with_framework = select_input_framework(
        config, context.groups, framework_answer.unwrap()
    )
    framework_group = _selected_input_framework(with_framework, context.groups)
    if not framework_group:
        without_configuration = _set_input_configuration(
            with_framework, context.groups, None
        )
        return settle(screen, context, config, without_configuration)
    framework = context.groups[framework_group].input_framework
    sections = input_engine_sections(context.groups, framework)
    names = tuple(name for _, section in sections for name in section)
    have = {overlay.name for overlay in config.portage.overlays}
    items = [
        Item(
            label=translate(context.groups[name].label or name),
            value=name,
            heading=translate(language) if offset == 0 else "",
            disabled_because=_needs_an_overlay(
                context.groups[name].repositories, have, translate
            ),
        )
        for language, section in sections
        for offset, name in enumerate(section)
    ]
    selected = set(with_framework.packages.applications) & set(names)
    engine_answer = MultipleChoiceMenu(
        title=translate("Input engines"),
        items=items,
        selected={
            index for index, item in enumerate(items) if item.value in selected
        },
        footer=footer(translate),
    ).run(screen)
    if not engine_answer.chosen:
        return Answer(engine_answer.outcome)
    edited = select_input_engines(
        with_framework, context.groups, tuple(engine_answer.unwrap())
    )
    summary = _input_configuration_summary(
        edited, context.groups, framework, translate
    )
    consented = _input_consent_screen(screen, edited, context, summary)
    if not consented.chosen:
        return Answer(consented.outcome)
    return settle(screen, context, config, consented.unwrap())


def cjk_fonts_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    translate = context.translate
    sections = font_sections(context.groups)
    names = tuple(name for _, section in sections for name in section)
    have = {overlay.name for overlay in config.portage.overlays}
    items = [
        Item(
            label=translate(context.groups[name].label or name),
            value=name,
            heading=translate(heading) if offset == 0 else "",
            preference_group=(
                FontCategory.selected(context.groups[name].font_category).generic
                if context.groups[name].font_cjk
                else ""
            ),
            disabled_because=_needs_an_overlay(
                context.groups[name].repositories, have, translate
            ),
        )
        for heading, section in sections
        for offset, name in enumerate(section)
    ]
    selected = [name for name in config.packages.applications if name in names]
    preferred = set(preferred_font_groups(config, context.groups))
    font_menu = MultipleChoiceMenu(
        title=translate("Fonts"),
        items=items,
        tri_state=True,
        selected={
            index for index, item in enumerate(items) if item.value in selected
        },
        preferred={
            index for index, item in enumerate(items) if item.value in preferred
        },
        footer="  ".join(
            (
                f"[-] {translate('installed')}",
                f"[x] {translate('installed and preferred')}",
                translate("one preferred per generic"),
                footer(translate),
            )
        ),
    )
    font_answer = font_menu.run(screen)
    if not font_answer.chosen:
        return Answer(font_answer.outcome)
    chosen = tuple(font_answer.unwrap())
    chosen_preferred = tuple(
        items[index].value for index in sorted(font_menu.preferred)
    )
    edited = select_cjk_fonts(config, context.groups, chosen, chosen_preferred)
    locale = CjkFontconfigLocale.selected(edited.system.locale)
    if not chosen or locale is None:
        without_configuration = _set_font_configuration(
            edited, context.groups, None
        )
        return settle(screen, context, config, without_configuration)
    preferred_sans = next(
        (
            name
            for name in chosen_preferred
            if FontCategory.selected(context.groups[name].font_category).generic
            == "sans-serif"
        ),
        "",
    )
    leading_sans = (
        locale.resolve(context.groups[preferred_sans].font_family)
        if preferred_sans
        else locale.face
    )
    summary = translate("Write {path}; {face} will lead sans-serif.").format(
        path=CJK_SANS_PREFERENCE, face=leading_sans
    )
    consented = _consent_screen(
        screen,
        edited,
        context,
        "Font configuration",
        summary,
    )
    if not consented.chosen:
        return Answer(consented.outcome)
    return settle(screen, context, config, consented.unwrap())


def packages_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    translate = context.translate
    # A desktop, a driver and a display manager each decide something else as
    # well, so each has its own screen and none is a row to tick here.
    elsewhere = (
        set(desktop_profiles(context.groups, context.profile_paths))
        | {name for name, _ in GRAPHICS}
        | {name for name, _ in DISPLAY_MANAGERS}
        | set(input_method_groups(context.groups))
        | set(cjk_font_groups(context.groups))
    )
    names = sorted(name for name in context.groups if name not in elsewhere)
    have = {overlay.name for overlay in config.portage.overlays}
    items = [
        Item(
            label=name,
            value=name,
            detail=" ".join(context.groups[name].packages)
            + _adds(
                config,
                context,
                lambda packages, one: replace(
                    packages, applications=(*packages.applications, one)
                ),
                name,
            ),
            disabled_because=_needs_an_overlay(context.groups[name].repositories, have, translate),
        )
        for name in names
    ]
    chosen_already = set(config.packages.applications)
    while True:
        menu: MultipleChoiceMenu[str] = MultipleChoiceMenu(
            title=translate("Applications"),
            preamble=(translate("These packages supplement the desktop selection."),),
            items=items,
            selected={index for index, item in enumerate(items) if item.value in chosen_already},
            footer=footer(translate),
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        chosen = answer.unwrap()
        kept = tuple(name for name in config.packages.applications if name in elsewhere)
        edited = replace(
            config,
            packages=replace(config.packages, applications=(*kept, *chosen)),
        )
        # Checked here rather than at the Install row: the conflict is between
        # two ticks on this screen and this is where either can be unticked.
        clash = framework_conflict(edited, context.groups)
        if not clash:
            return settle(screen, context, config, edited)
        chosen_already = set(chosen)
        say(screen, context, clash)


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
#: What the screen offers when the machine's own `/usr/share/zoneinfo` cannot
#: be read. Every zone `LANGUAGE_DEFAULTS` picks is here: a `ko` interface
#: pre-filled `Asia/Seoul` and this list could not show it.
TIMEZONES: tuple[str, ...] = (
    "Asia/Shanghai",
    "Asia/Taipei",
    "Asia/Hong_Kong",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Europe/London",
    "America/New_York",
    "UTC",
)


def locale_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    menu: Menu[str] = Menu(
        title=translate("System language"),
        items=[
            Item(label=f"{name}  {translate(label)}", value=name) for name, label in LOCALES
        ],
        footer=footer(translate),
        current=config.system.locale,
        preamble=(translate("The installed system starts in this locale."),),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()
    generated = config.system.locales
    if chosen not in generated:
        generated = (*generated, chosen)
    changed = replace(config, system=replace(config.system, locale=chosen, locales=generated))
    if CjkFontconfigLocale.selected(chosen) is None:
        changed = _set_font_configuration(changed, context.groups, None)
    return Answer(Outcome.CHOSE, changed)


def additional_locales_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Locales generated in addition to the one used for `LANG`."""
    translate = context.translate
    items = [
        Item(label=f"{name}  {translate(label)}", value=name)
        for name, label in LOCALES
        if name != config.system.locale
    ]
    selected = {
        index for index, item in enumerate(items) if item.value in config.system.locales
    }
    answer = MultipleChoiceMenu(
        title=translate("Other locales"),
        items=items,
        selected=selected,
        footer=footer(translate),
        preamble=(translate("The system language is always generated."),),
    ).run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()
    chosen_set = frozenset(chosen)
    generated = tuple(
        locale
        for locale in config.system.locales
        if locale == config.system.locale or locale in chosen_set
    )
    generated += tuple(locale for locale in chosen if locale not in generated)
    if config.system.locale not in generated:
        generated = (config.system.locale, *generated)
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            system=replace(
                config.system,
                locales=generated,
            ),
        ),
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
    items = [Item(label=area, value=area) for area in areas]
    if context.timezone_here:
        # First, because it is usually right: the medium took it from the
        # firmware clock, and the operator is standing next to the machine.
        items.insert(
            0,
            Item(
                label=translate("follow the BIOS"),
                value="",
                detail=context.timezone_here,
            ),
        )
    chosen_area: Menu[str] = Menu(
        title=translate("Timezone"),
        preamble=(translate("The installed system uses this zone for local time and schedules."),),
        items=items,
        footer=footer(translate),
        # The BIOS row when the medium read one and the configuration still
        # holds the built-in default: the operator is standing next to the
        # machine and that guess is usually right. Anything the operator or a
        # configuration file actually chose wins over it.
        current=(
            ""
            if context.timezone_here and config.system.timezone == SystemConfig().timezone
            else config.system.timezone.split("/", 1)[0]
        ),
    )
    picked = chosen_area.run(screen)
    if not picked.chosen:
        return Answer(picked.outcome)
    area = picked.unwrap()
    if not area:
        return Answer(
            Outcome.CHOSE,
            replace(config, system=replace(config.system, timezone=context.timezone_here)),
        )
    within = [zone for zone in zones if zone.split("/", 1)[0] == area]
    if within == [area]:
        return Answer(
            Outcome.CHOSE, replace(config, system=replace(config.system, timezone=area))
        )
    while True:
        city: Menu[str] = Menu(
            title=area,
            items=[Item(label=zone.split("/", 1)[1], value=zone) for zone in within],
            footer=footer(translate),
            current=config.system.timezone,
        )
        answer = city.run(screen)
        if answer.chosen:
            return Answer(
                Outcome.CHOSE, replace(config, system=replace(config.system, timezone=answer.unwrap()))
            )
        if answer.outcome is Outcome.BACK:
            picked = chosen_area.run(screen)
            if not picked.chosen:
                return Answer(picked.outcome)
            area = picked.unwrap()
            within = [zone for zone in zones if zone.split("/", 1)[0] == area]
            continue
        return Answer(answer.outcome)


def encryption_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Whether the root filesystem is encrypted, and the passphrase if it is."""
    translate = context.translate
    if context.manual:
        # `_rebuild` builds from `context.layout` here and reads none of
        # `context.choice`, so staging a passphrase left the row reading `on`
        # over a graph with no container in it at all.
        say(screen, context, translate("Encryption is a field of each partition, under Partitions."))
        return Answer(Outcome.BACK)
    edited = _edit_passphrase(
        screen,
        context,
        context.choice.passphrase_file,
        translate("Encrypt the root filesystem?"),
    )
    if edited.chosen:
        context.choice = replace(context.choice, passphrase_file=edited.unwrap())
    def rebuilt(_: str) -> InstallConfig:
        changed = _rebuild(config, context)
        if not context.choice.passphrase_file:
            changed = replace(
                changed,
                kernel=replace(
                    changed.kernel,
                    remote_unlock=replace(changed.kernel.remote_unlock, enabled=False),
                ),
            )
        return changed

    return edited.map(rebuilt)


def _edit_passphrase(
    screen: Screen, context: Context, staged_path: str, title: str
) -> Answer[str]:
    """Enable, disable, or replace one staged encryption passphrase."""
    translate = context.translate
    enabled = Confirm(
        **answers(translate),
        title=title,
        footer=footer(translate),
        current=bool(staged_path),
    ).run(screen)
    if not enabled.chosen:
        return Answer(enabled.outcome)
    if not enabled.unwrap():
        return Answer(Outcome.CHOSE, "")
    return _ask_passphrase(screen, context)


def _ask_passphrase(screen: Screen, context: Context) -> Answer[str]:
    """The passphrase typed twice, staged in a file whose path is returned.

    The configuration holds the path and never the passphrase, because it is
    copied into the target and the install log is what people paste into bug
    reports.
    """
    translate = context.translate
    hint = translate("At least {count} characters.").format(count=PASSPHRASE_MINIMUM)
    while True:
        first = TextField(
            title=translate("Passphrase"),
            masked=True,
            detail=hint,
            footer=footer(translate),
        ).run(screen)
        if not first.chosen:
            return Answer(first.outcome)
        typed = first.unwrap()
        if len(typed) < PASSPHRASE_MINIMUM:
            # Checked here, not at preflight: zfs refuses a short passphrase
            # only once the disks have been partitioned.
            say(screen, context, translate("The passphrase is too short."))
            continue
        again = TextField(
            title=translate("Passphrase again"),
            masked=True,
            detail=hint,
            footer=footer(translate),
        ).run(screen)
        if not again.chosen:
            return Answer(again.outcome)
        if again.unwrap() != typed:
            say(screen, context, translate("The two do not match."))
            continue
        return Answer(Outcome.CHOSE, context.stage_passphrase(typed))


def _ask_password(screen: Screen, context: Context, title: str) -> Answer[str]:
    """A password typed twice, or the outcome that left the prompt.

    Twice for the same reason the passphrase is: the field is masked, and a
    password with a typo in it is found out at the first login of a machine
    that has already been installed.
    """
    translate = context.translate
    # One form, not two screens in a row. Two screens have nothing for the
    # arrow keys to move between, so enter is the only way forward, and a typo
    # in the second one threw the first away as well. `Field.secret` exists for
    # this and the account form already uses it.
    def validated(values: list[str]) -> Answer[str] | FormRejected:
        first, again = values
        if not first and not again:
            # Nothing typed is leaving, not an empty password: the row stays
            # required and says so, and enter through the whole form no longer
            # asks the same question for ever.
            return Answer(Outcome.BACK)
        if first and first == again:
            return Answer(Outcome.CHOSE, first)
        # The form comes back with what was typed: only the mismatched second
        # field is cleared, so a long password is not retyped from nothing.
        return FormRejected(translate("The two do not match."), {1: ""})

    return Form(
        title=title,
        fields=[
            Field(label=translate("Password"), secret=True),
            Field(label=translate("Type it again"), secret=True),
        ],
        footer=footer(translate),
    ).run_validated(screen, validated)


def swap_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    items: list[Item[str]] = [
        Item(label=translate("none"), value=""),
        Item(label="4GiB", value="4GiB", detail=translate("a partition")),
        Item(label="8GiB", value="8GiB", detail=translate("a partition")),
    ]
    menu: Menu[str] = Menu(
        title=translate("Swap"),
        preamble=(translate("A swap partition relieves memory pressure and can support hibernation."),),
        items=items,
        current=str(context.choice.swap) if context.choice.swap else "",
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()
    context.choice = replace(context.choice, swap=Size.parse(chosen) if chosen else None)
    if context.manual:
        for disk in context.layout.disks:
            disk.slices[:] = [
                replace(entry, status=manual.SliceStatus.DELETE)
                if entry.role is PartitionRole.SWAP and not chosen
                else entry
                for entry in disk.slices
            ]
        if chosen and not any(entry.role is PartitionRole.SWAP for entry in context.layout.slices):
            disk = context.layout.disks[0]
            disk.slices.append(
                manual.Slice(
                    index=disk.next_index(),
                    role=PartitionRole.SWAP,
                    size=Size.parse(chosen),
                )
            )
        return Answer(Outcome.CHOSE, _from_layout(config, context))
    return Answer(Outcome.CHOSE, _rebuild(config, context))


def zram_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Compressed swap in memory, off unless it is asked for.

    Its own row rather than a fourth entry under Swap: the two are not
    alternatives. A machine can hold a swap partition for hibernation and zram
    for the pressure it meets while running, and offering them as one list said
    otherwise.

    The sizes are shares of this machine's memory. zram is compressed, so a
    quarter of RAM holds more than a quarter of RAM.
    """
    translate = context.translate
    items: list[Item[Size | None]] = [
        Item(label=translate("off"), value=None, detail=translate("no zram device")),
    ]
    for name, divisor in RAM_SHARES:
        share = Size(context.memory.bytes // divisor)
        if share.bytes:
            items.append(
                Item(
                    label=share.single_letter(),
                    value=share,
                    detail=f"{translate(name)} {translate('of this machine')}",
                )
            )
    if len(items) == 1:
        # Only `off` is left, which is not a question. This machine reported no
        # memory, so there is no share of it to offer.
        say(screen, context, translate("this machine reports no memory to share"))
        return Answer(Outcome.BACK)
    menu: Menu[Size | None] = Menu(
        title=translate("zram"),
        preamble=(translate("zram creates compressed swap in memory without disk space."),),
        items=items,
        current=config.system.zram,
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, zram=answer.unwrap()))
    )


#: The fractions of this machine's memory offered as a build tmpfs. Not fixed
#: sizes: 16GiB is most of a 24GiB laptop and a quarter of this workstation,
#: and the useful question is how much of the machine to give up.
RAM_SHARES: tuple[tuple[str, int], ...] = (("a quarter", 4), ("half", 2))

#: Under this, the tmpfs is not worth offering: a Chromium or Rust build fills
#: it and fails on ENOSPC after an hour, which is worse than building on disk.
LEAST_RAM: Final[Size] = Size(8 * 1024**3)


def build_in_ram_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """A tmpfs over /var/tmp/portage, off unless it is asked for.

    Off by default because the failure is bad and late: a build that outgrows
    the tmpfs dies on ENOSPC, and how much a machine can spare is not derivable
    from how much it has. The sizes offered are shares of this machine's own
    memory rather than fixed numbers.
    """
    translate = context.translate
    items: list[Item[Size | None]] = [
        Item(label=translate("off"), value=None, detail=translate("build on disk")),
    ]
    for name, divisor in RAM_SHARES:
        share = Size(context.memory.bytes // divisor)
        if share >= LEAST_RAM:
            items.append(
                Item(
                    label=share.single_letter(),
                    value=share,
                    detail=f"{translate(name)} {translate('of this machine')}",
                )
            )
    if len(items) == 1:
        say(screen, context, translate("this machine has too little memory to build in it"))
        return Answer(Outcome.BACK)
    menu: Menu[Size | None] = Menu(
        title=translate("Build in RAM"),
        preamble=(translate("Portage builds in memory; its contents disappear after reboot."),),
        items=items,
        current=config.portage.build_in_ram,
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, portage=replace(config.portage, build_in_ram=answer.unwrap())),
    )


def first_boot_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """A script and some commands, run once the first time the system boots.

    The script is fetched during the install, not at first boot: a download
    that fails then leaves a machine half-configured with nobody watching, and
    the operator cannot read beforehand what is about to run as root.
    """
    translate = context.translate
    wanted = config.system.first_boot
    def validated(values: list[str]) -> Answer[InstallConfig] | FormRejected:
        url, typed = (one.strip() for one in values)
        if url and not url.startswith(("http://", "https://")):
            return FormRejected(
                translate("An address starts with http:// or https://"), {0: url, 1: typed}
            )
        commands = tuple(one.strip() for one in typed.split(";") if one.strip())
        return Answer(
            Outcome.CHOSE,
            replace(
                config,
                system=replace(config.system, first_boot=FirstBoot(commands=commands, url=url)),
            ),
        )

    return Form(
        title=translate("Run once at first boot"),
        fields=[
            Field(
                label=translate("Script address"),
                value=wanted.url,
                placeholder=translate("https://example.com/setup.sh, or empty for none"),
            ),
            Field(
                label=translate("Commands"),
                value=" ; ".join(wanted.commands),
                placeholder=translate("separated by ; and run in order"),
            ),
        ],
        footer=footer(translate),
        done=translate("Done"),
    ).run_validated(screen, validated)


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
                # No detail: the one it had said `root is a row of its own`,
                # which describes this menu rather than what the option does.
                # `sshd_root_login` is that row and starts off, so
                # `PermitRootLogin no` is written whichever of these is picked.
                label=translate("password login"),
                value=(True, True),
            ),
        ],
        footer=footer(translate),
        current=(config.system.sshd, config.system.sshd_password_login),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    running, password = answer.unwrap()
    if running and config.system.firewall is Firewall.NONE:
        acknowledged = say(
            screen,
            context,
            translate(
                "An SSH server answers the whole network. A firewall is worth "
                "installing with it; the Firewall row under Network does that and "
                "writes no rules, so the policy stays the operator's."
            ),
        )
        if not acknowledged.chosen:
            return Answer(acknowledged.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            system=replace(config.system, sshd=running, sshd_password_login=password),
        ),
    )


def saved_config_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Offer the configurations sitting where the installer was started.

    Asked rather than loaded: a file called `my-install.toml` next to the
    installer is very likely the operator's own answers from before a reboot,
    and as likely to be someone else's example. Nothing is offered
    when the directory holds none, so the usual run is unchanged.
    """
    translate = context.translate
    if not context.configs_here:
        return Answer(Outcome.CHOSE, config)
    items: list[Item[str]] = [
        Item(label=translate("Start from scratch"), value="")
    ]
    items += [Item(label=name, value=name) for name in context.configs_here]
    while True:
        menu: Menu[str] = Menu(
            title=translate("A saved configuration is here. Load it?"),
        preamble=(translate("Loading a saved file replaces the current answers."),),
            items=items,
            footer=footer(translate),
        )
        answer = menu.run(screen)
        if not answer.chosen or not answer.unwrap():
            return Answer(Outcome.CHOSE, config)
        try:
            loaded = context.load_config(answer.unwrap())
            context.hydrate_disk(loaded)
            fetch.configure_proxy(loaded.proxy)
            return Answer(Outcome.CHOSE, loaded)
        except GentooInstallError as error:
            # Back to the list rather than out of the installer: the file being
            # unreadable says nothing about the other one beside it.
            say(screen, context, str(error).splitlines()[-1].strip())


def _profile_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Only the profiles that match the chosen init, because the validator
    refuses the other half and the operator should not be offered them."""
    choices = context.profile_paths or PROFILES
    wanted = [
        profile
        for profile in choices
        if ("systemd" in profile.split("/")) is (config.system.init is InitSystem.SYSTEMD)
    ]
    menu: Menu[str] = Menu(
        title=context.translate("Portage"),
        items=[Item(label=profile, value=profile) for profile in wanted],
        footer=footer(context.translate),
        current=config.portage.profile,
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, portage=replace(config.portage, profile=answer.unwrap()))
    )


def table_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """GPT or MBR. UEFI needs GPT in practice, and the compatibility table
    refuses BIOS booting a GPT disk with no bios-boot partition."""
    translate = context.translate
    items = [Item(label=table.value, value=table) for table in TableType]
    menu: Menu[TableType] = Menu(
        title=translate("Partition table"),
        preamble=(translate("The table format controls how firmware and the kernel identify partitions."),),
        items=items,
        current=context.choice.table,
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    context.choice = replace(context.choice, table=answer.unwrap())
    if context.manual and context.layout.disks:
        context.layout.disks[0].table = answer.unwrap()
        return Answer(Outcome.CHOSE, _from_layout(config, context))
    return Answer(Outcome.CHOSE, _rebuild(config, context))


def keymap_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """The keymaps this machine ships, family first.

    Two hundred rows do not fit a console, and a typed name that `kbd` has no
    file for loads nothing and says so only at the next boot.
    """
    translate = context.translate
    picked = _pick_keymap(screen, context, translate("Keyboard layout"), config.system.keymap)
    if not picked.chosen:
        return Answer(picked.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, keymap=picked.unwrap() or "us")),
    )


def _pick_keymap(
    screen: Screen, context: Context, title: str, current: str, empty: str = ""
) -> Answer[str]:
    """One keymap, or the outcome that left its prompt.

    `empty` names the row that stands for no answer and is absent when there
    is no such answer.
    """
    translate = context.translate
    offered = context.keymaps()
    if not offered:
        # A medium that ships no keymap tree has nothing to list, and a list
        # nobody can populate is worse than a field.
        typed = TextField(title=title, value=current, footer=footer(translate)).run(screen)
        return Answer(Outcome.CHOSE, typed.unwrap()) if typed.chosen else Answer(typed.outcome)
    families: list[Item[str]] = []
    if empty:
        families.append(Item(label=empty, value=""))
    families += [
        Item(label=family, value=family) for family in sorted({one for one, _ in offered})
    ]
    family_current = next(
        (family for family, name in offered if name == current),
        "",
    )
    answer = _current_menu(
        screen,
        context,
        title,
        families,
        family_current,
    )
    if not answer.chosen:
        return Answer(answer.outcome)
    family = answer.unwrap()
    if not family:
        return Answer(Outcome.CHOSE, "")
    within = [name for one, name in offered if one == family]
    chosen = _current_menu(
        screen,
        context,
        f"{title}  {family}",
        [Item(label=name, value=name) for name in within],
        current,
    )
    return (
        Answer(Outcome.CHOSE, chosen.unwrap())
        if chosen.chosen
        else Answer(chosen.outcome)
    )


def console_font_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Cell size of the console font. A rule in `compat.py` refuses the 8x8 one
    beside console CJK, so the excluded size is drawn with its own reason
    rather than being offered and then rejected by the validator."""
    shared = _common_violations(
        [replace(config, system=replace(config.system, console_font=one)) for one in ConsoleFontSize]
    )
    items: list[Item[ConsoleFontSize]] = []
    for size in ConsoleFontSize:
        candidate = replace(config, system=replace(config.system, console_font=size))
        broken = [one for one in compat.violations(candidate) if one not in shared]
        items.append(
            Item(
                label=size.value,
                value=size,
                disabled_because=context.translate(broken[0].reason) if broken else "",
            )
        )
    answer = Menu(
        title=context.translate("Console font"),
        preamble=(context.translate("The console font controls glyph sizes before the desktop starts."),),
        items=items,
        footer=footer(context.translate),
        current=config.system.console_font,
    ).run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, console_font=answer.unwrap())),
    )


def cpu_flags_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """This machine's flags, or the baseline the profile sets.

    The detected list builds for the CPU in front of the operator and for no
    other, which is wrong for an image, for a disk moved to another machine,
    and for anything the binary host built against the baseline.
    """
    translate = context.translate
    detected = tuple(context.cpu_flags)
    items: list[Item[tuple[str, ...]]] = [
        Item(
            label=" ".join(detected) or translate("none detected"),
            value=detected,
            detail=translate("this machine"),
        ),
        Item(
            label=translate("baseline"),
            value=(),
            detail=translate("what the profile sets, and what a binary host builds"),
        ),
    ]
    answer = Menu(
        title=translate("CPU flags"),
        preamble=(translate("These flags select the instruction set used to compile packages."),),
        items=items,
        footer=footer(translate),
        current=config.portage.cpu_flags,
    ).run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, portage=replace(config.portage, cpu_flags=answer.unwrap())),
    )


def networking_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """How the installed system brings a link up. The two NetworkManager rows
    differ only in which supplicant drives wifi."""
    translate = context.translate
    builtin = translate("systemd-networkd") if config.system.init is InitSystem.SYSTEMD else translate("netifrc")
    detail = {
        Networking.BUILTIN: translate(builtin),
        Networking.NETWORKMANAGER_WPA: translate("wpa_supplicant for wifi"),
        Networking.NETWORKMANAGER_IWD: translate("iwd for wifi"),
        Networking.NONE: translate("configure it yourself after the install"),
    }
    items = [
        Item(label=translate(choice.value), value=choice, detail=detail[choice]) for choice in Networking
    ]
    menu: Menu[Networking] = Menu(
        title=translate("Network configuration"),
        preamble=(translate("This selects the service that brings interfaces up."),),
        items=items,
        footer=footer(translate),
        current=config.system.networking,
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()
    _record_operator(context, ValueKind.NETWORKING, (chosen.value,))
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, networking=chosen)),
    )


def firewall_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Which packet filter to merge. The package and nothing else: no service
    is enabled and no rule is written, so the choice changes what is installed
    and not what the machine answers."""
    translate = context.translate
    # Each call written out, not `translate(detail[choice])`: the catalog test
    # reads literal arguments, and a lookup by variable ships untranslated.
    detail = {
        Firewall.NONE: translate("no packet filter is installed"),
        Firewall.IPTABLES: translate("the older one, for a rule set that already exists"),
        Firewall.NFTABLES: translate("one table for both families, and what a new rule set uses"),
    }
    items = [Item(label=choice.value, value=choice, detail=detail[choice]) for choice in Firewall]
    menu: Menu[Firewall] = Menu(
        title=translate("Firewall"),
        preamble=(translate("This installs a packet filter but does not write its policy."),),
        items=items,
        footer=footer(translate),
        current=config.system.firewall,
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()
    if chosen is not Firewall.NONE:
        acknowledged = say(
            screen,
            context,
            translate(
                "Both ship an empty rule set, and the installer writes none: a "
                "policy it chose could drop port 22, and a machine reached only "
                "over SSH would then need a console to be reached at all."
            ),
        )
        if not acknowledged.chosen:
            return Answer(acknowledged.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, firewall=chosen))
    )


class _RowKind(Enum):
    """What one line of the partition screen stands for."""

    DISK = "disk"
    SLICE = "slice"
    ADD_PARTITION = "add-partition"
    ADD_DISK = "add-disk"
    TOPOLOGY = "topology"
    POOL_ENCRYPTION = "pool-encryption"
    ARRAY = "array"
    DONE = "done"


@dataclass(frozen=True)
class _Row:
    """A line of the partition screen, and what it points at.

    `disk` is a position in `Layout.disks` and `entry` a position in that
    disk's rows; both are -1 on a line that points at neither.
    """

    kind: _RowKind
    disk: int = -1
    entry: int = -1


def partitions_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Every disk being partitioned, and under each one its table, row by row.

    Every change rebuilds the graph and runs the validator, so a table that
    cannot be installed says why here rather than at the first `mkfs`.
    """
    translate = context.translate
    if not context.layout.holds(context.choice.disk) or not context.layout.slices:
        # Seeded from what is on the disk when there is anything, and from the
        # template that was chosen when there is not: opening this row after
        # picking zfs used to show an ext4 root and discard the choice.
        context.layout = _seed(context)
    saved_layout = deepcopy(context.layout)
    saved_manual = context.manual
    cursor = 0
    while True:
        items = _partition_rows(context)
        menu: Menu[_Row] = Menu(
            title=_partitions_title(context),
            preamble=(translate("This editor controls which partitions are kept, formatted, mounted, or erased."),),
            items=items,
            cursor=cursor,
            footer=f"{_layout_problem(context, config)}  {footer(translate)}".strip(),
        )
        answer = menu.run(screen)
        cursor = menu.cursor
        if not answer.chosen:
            added = [
                deepcopy(disk)
                for disk in context.layout.disks
                if not saved_layout.holds(disk.selector)
            ]
            context.layout = saved_layout
            context.layout.disks.extend(added)
            context.manual = saved_manual
            return Answer(answer.outcome)
        row = answer.unwrap()
        if row.kind is _RowKind.DONE:
            # Marked here rather than by whoever opened this screen: the row can
            # be reached from the menu as well as from the layout row, and a
            # flag set before the editor answers describes a table that may
            # never have been produced.
            context.manual = True
            built = _from_layout(config, context)
            if context.layout.disks and _pool_members(context):
                # The same question the template path asks: a ZFS root cannot
                # boot from GRUB, and ZFSBootMenu needs the overlay adding.
                # Without this every bootloader row was greyed and the table
                # had no way out.
                picked = _zfs_bootloader(screen, built, context)
                if not picked.chosen:
                    if picked.outcome is Outcome.CANCELLED:
                        return Answer(picked.outcome)
                    # Back to the editor rather than out of it: the table the
                    # operator drew is still there, and a ZFS root with GRUB
                    # is what committing this would have written.
                    continue
                built = picked.unwrap()
            return Answer(Outcome.CHOSE, built)
        _act_on(screen, context, row)


def _partitions_title(context: Context) -> str:
    translate = context.translate
    if context.layout.writes_the_table():
        return translate("A new partition table")
    return translate("Partitions")


def _partition_rows(context: Context) -> list[Item[_Row]]:
    """One line per disk, its rows indented under it, then what can be added."""
    translate = context.translate
    items: list[Item[_Row]] = []
    for position, disk in enumerate(context.layout.disks):
        items.append(
            Item(
                label=context.shown_as(disk.selector),
                value=_Row(_RowKind.DISK, position),
                detail=f"{disk.table.value}  {_capacity(context, disk)}",
            )
        )
        for index, entry in enumerate(sorted(disk.slices, key=lambda one: one.index)):
            # Two spaces rather than a box-drawing character: the console this
            # runs on may have neither a CJK font nor a line-drawing set.
            items.append(
                Item(label=f"  {entry.describe()}", value=_Row(_RowKind.SLICE, position, index))
            )
        items.append(
            Item(
                label=f"  {translate('Add a partition')}",
                value=_Row(_RowKind.ADD_PARTITION, position),
            )
        )
    if _unused_disks(context):
        items.append(Item(label=translate("Add a disk"), value=_Row(_RowKind.ADD_DISK)))
    # Both rows are drawn whether or not they can be opened. Hidden until a
    # partition happened to carry the right purpose, the two features were
    # unreachable to anyone who did not already know they existed.
    pool = len(_pool_members(context))
    items.append(
        Item(
            label=translate("Pool topology"),
            value=_Row(_RowKind.TOPOLOGY),
            detail=context.layout.topology.value if pool > 1 else "",
            disabled_because=(
                "" if pool > 1 else translate("give two partitions the zfs pool member purpose")
            ),
        )
    )
    if pool:
        items.append(
            Item(
                label=f"ZFS {translate('Encryption')}",
                value=_Row(_RowKind.POOL_ENCRYPTION),
                detail=translate("on") if context.layout.passphrase_file else translate("off"),
            )
        )
    array = len(_array_members(context))
    items.append(
        Item(
            label=translate("RAID array"),
            value=_Row(_RowKind.ARRAY),
            detail=_array_summary(context) if array else "",
            disabled_because=(
                "" if array else translate("give a partition the raid array member purpose")
            ),
        )
    )
    items.append(Item(label=translate("Done"), value=_Row(_RowKind.DONE)))
    return items


def _array_summary(context: Context) -> str:
    array = context.layout.array
    where = array.mountpoint or "-"
    return f"{array.name}, {array.level.value}, {array.filesystem.value}, {where}"


def _act_on(screen: Screen, context: Context, row: _Row) -> None:
    """Everything the screen does but leave, so the loop above stays readable."""
    if row.kind is _RowKind.TOPOLOGY:
        picked = _pool_topology(screen, context, len(_pool_members(context)))
        if picked is not None:
            context.layout.topology = picked
        return
    if row.kind is _RowKind.POOL_ENCRYPTION:
        _edit_pool_encryption(screen, context)
        return
    if row.kind is _RowKind.ARRAY:
        _edit_array(screen, context)
        return
    if row.kind is _RowKind.ADD_DISK:
        added = _pick_another_disk(screen, context)
        if added is not None:
            held, _ = context.contents(added)
            context.layout.disks.append(_seeded_disk(added, held))
        return
    disk = context.layout.disks[row.disk]
    if row.kind is _RowKind.DISK:
        _edit_disk(screen, context, row.disk)
        return
    if row.kind is _RowKind.ADD_PARTITION:
        fresh = _edit_slice(screen, context, disk, None)
        if fresh is not None:
            disk.slices.append(fresh)
        return
    rows = sorted(disk.slices, key=lambda one: one.index)
    edited = _edit_slice(screen, context, disk, rows[row.entry])
    disk.slices.remove(rows[row.entry])
    if edited is not None:
        disk.slices.append(edited)


def _array_members(context: Context) -> list[manual.Slice]:
    return [one for one in context.layout.slices if one.role is PartitionRole.RAID]


def _pool_members(context: Context) -> list[manual.Slice]:
    return [one for one in context.layout.slices if one.role is PartitionRole.ZFS]


def _unused_disks(context: Context) -> list[tuple[str, str]]:
    """Disks this machine has that the table does not already cover."""
    return [one for one in context.disks if not context.layout.holds(one[0])]


def _pick_another_disk(screen: Screen, context: Context) -> str | None:
    translate = context.translate
    answer = Menu(
        title=translate("Add a disk"),
        items=[
            Item(label=name, value=name, detail=detail) for name, detail in _unused_disks(context)
        ],
        footer=footer(translate),
    ).run(screen)
    return answer.unwrap() if answer.chosen else None


def _edit_disk(screen: Screen, context: Context, position: int) -> None:
    """What the disk itself carries: its table type, and whether it stays.

    The first disk cannot be dropped here; it is the one the disk row chose,
    and a table with no disk at all has nothing to install onto.
    """
    translate = context.translate
    disk = context.layout.disks[position]
    while True:
        items: list[Item[str]] = [
            Item(label=translate("Partition table"), value=_TABLE, detail=disk.table.value),
            *(
                [Item(label=translate("Take this disk off the table"), value=_DROP)]
                if position > 0
                else []
            ),
            Item(label=translate("Done"), value=_DONE),
        ]
        answer = Menu(
            title=context.shown_as(disk.selector), items=items, footer=footer(translate)
        ).run(screen)
        if not answer.chosen or answer.unwrap() == _DONE:
            return
        if answer.unwrap() == _DROP:
            context.layout.disks.pop(position)
            return
        picked = _current_menu(
            screen,
            context,
            translate("Partition table"),
            [Item(label=one.value, value=one) for one in TableType],
            disk.table,
        )
        if picked.chosen:
            disk.table = picked.unwrap()


def _template_filesystem(choice: Choice) -> FilesystemType | None:
    """What the root of the chosen template carries. None for ZFS, whose root
    is a dataset on a pool and not a filesystem on a partition."""
    return None if choice.layout is Layout.WHOLE_DISK_ZFS else choice.filesystem


def _capacity(context: Context, disk: manual.Disk) -> str:
    """The disk's size and what its table has already claimed, because a size
    is guesswork without them."""
    translate = context.translate
    total = context.contents(disk.selector)[1]
    if not total:
        return ""
    fresh = [one for one in disk.slices if one.status is manual.SliceStatus.CREATE]
    claimed = sum(entry.size.bytes for entry in fresh if entry.size is not None)
    rest = any(entry.size is None for entry in fresh)
    used = Size(claimed)
    return translate("{total} total, {used} claimed{rest}").format(
        total=total, used=used, rest=translate(", rest to one partition") if rest else ""
    )


def _seed(context: Context) -> manual.Layout:
    """The table the editor opens on.

    Every partition already on the disk, kept: an operator who opens this over
    a machine with data on it should see that data, not a proposal that erases
    it. An empty disk has nothing to list, so it gets the template's proposal.
    """
    if not context.existing:
        return manual.suggest(
            context.choice.disk, context.choice.firmware, _template_filesystem(context.choice)
        )
    return manual.Layout(
        disks=[
            _seeded_disk(
                context.choice.disk,
                context.existing,
                context.choice.table or TableType.GPT,
            )
        ]
    )


def _seeded_disk(
    selector: str, held: Sequence[tuple[str, str, str]], table: TableType = TableType.GPT
) -> manual.Disk:
    """A disk with what the machine says is already on it, kept.

    Every disk, not only the first: a second one was appended empty, so a disk
    with partitions was drawn as blank and the rows added beside them rewrote
    its table.
    """
    disk = manual.Disk(selector=selector, table=table)
    for index, (where, _, kind) in enumerate(held, start=1):
        disk.slices.append(
            manual.Slice(
                index=index,
                role=PartitionRole.DATA,
                size=None,
                filesystem=_known(kind),
                status=manual.SliceStatus.KEEP,
                selector=where,
            )
        )
    return disk


_LEVEL: Final[str] = "level"
_METADATA: Final[str] = "metadata"
_NAME: Final[str] = "name"
_FILESYSTEM: Final[str] = "filesystem"
_MOUNTPOINT: Final[str] = "mountpoint"
_LABEL: Final[str] = "label"
_ENCRYPTION: Final[str] = "encryption"


def _edit_array(screen: Screen, context: Context) -> None:
    """The array the member rows are assembled into, as a list of fields.

    The same shape as a partition's field list, because it answers the same
    kind of questions: what it is called, what goes on it and where.
    """
    translate = context.translate
    members = len(_array_members(context))
    while True:
        array = context.layout.array
        items = [field.row((array, members), translate) for field in _ARRAY_FIELDS]
        answer = Menu(
            title=f"{translate('RAID array')}  {members} {translate('members')}",
            items=items,
            footer=footer(translate),
        ).run(screen)
        if not answer.chosen or answer.unwrap() == _DONE:
            return
        _edit_array_field(screen, context, answer.unwrap(), members)


def _edit_array_field(screen: Screen, context: Context, field: str, members: int) -> None:
    descriptor = next((one for one in _ARRAY_FIELDS if one.key == field), None)
    if descriptor is not None:
        descriptor.edit(screen, context, (context.layout.array, members))
    return


def _edit_array_field_legacy(screen: Screen, context: Context, field: str, members: int) -> None:
    translate = context.translate
    array = context.layout.array
    if field == _LEVEL:
        picked = _current_menu(
            screen,
            context,
            translate("RAID level"),
            [
                Item(
                    label=one.value,
                    value=one,
                    disabled_because=""
                    if members >= one.minimum
                    else translate("needs at least {count}").format(count=one.minimum),
                )
                for one in RaidLevel
            ],
            array.level,
        )
        if picked.chosen:
            array.level = picked.unwrap()
        return
    if field == _METADATA:
        chosen = _current_menu(
            screen,
            context,
            translate("Superblock"),
            [
                Item(
                    label=one.value,
                    value=one,
                    detail=""
                    if one.superblock_at_start
                    else translate("at the end, so firmware reads the member"),
                )
                for one in RaidMetadata
            ],
            array.metadata,
        )
        if chosen.chosen:
            array.metadata = chosen.unwrap()
        return
    if field == _FILESYSTEM:
        kind = _current_menu(
            screen,
            context,
            translate("Filesystem"),
            [Item(label=one.value, value=one) for one in FilesystemType],
            array.filesystem,
        )
        if kind.chosen:
            array.filesystem = kind.unwrap()
        return
    if field == _ENCRYPTION:
        edited = _edit_passphrase(
            screen,
            context,
            array.passphrase_file,
            translate("Encrypt this array?"),
        )
        if edited.chosen:
            array.passphrase_file = edited.unwrap()
        return
    titles = {_NAME: "Name", _MOUNTPOINT: "Mount point", _LABEL: "Label"}
    values = {_NAME: array.name, _MOUNTPOINT: array.mountpoint, _LABEL: array.label}
    typed = TextField(
        title=translate(titles[field]), value=values[field], footer=footer(translate)
    ).run(screen)
    if not typed.chosen:
        return
    text = typed.unwrap().strip()
    if field == _NAME:
        array.name = text or array.name
    elif field == _MOUNTPOINT:
        array.mountpoint = text
    else:
        array.label = text


def _array_descriptor(
    key: str, label: str, detail: Callable[[manual.Array, Catalog], str]
) -> _FieldDescriptor[tuple[manual.Array, int]]:
    def row(value: tuple[manual.Array, int], translate: Catalog) -> Item[str]:
        shown = translate(label)
        if key == _NAME:
            shown = translate("Name")
        return Item(label=shown, value=key, detail=detail(value[0], translate))

    def edit(screen: Screen, context: Context, value: tuple[manual.Array, int]) -> tuple[manual.Array, int]:
        _edit_array_field_legacy(screen, context, key, value[1])
        return value

    return _FieldDescriptor(
        key,
        row,
        edit,
    )


_ARRAY_FIELDS: tuple[_FieldDescriptor[tuple[manual.Array, int]], ...] = (
    _array_descriptor(_NAME, "Name", lambda array, _: array.name),
    _array_descriptor(_LEVEL, "RAID level", lambda array, _: array.level.value),
    _array_descriptor(_METADATA, "Superblock", lambda array, _: array.metadata.value),
    _array_descriptor(_FILESYSTEM, "Filesystem", lambda array, _: array.filesystem.value),
    _array_descriptor(_MOUNTPOINT, "Mount point", lambda array, _: array.mountpoint or "-"),
    _array_descriptor(_LABEL, "Label", lambda array, _: array.label or "-"),
    _array_descriptor(
        _ENCRYPTION,
        "Encryption",
        lambda array, translate: translate("on") if array.passphrase_file else translate("off"),
    ),
    _FieldDescriptor(
        _DONE,
        lambda _, translate: Item(label=translate("Done"), value=_DONE),
        lambda _, __, value: value,
    ),
)
def _pool_topology(
    screen: Screen, context: Context, members: int
) -> ZfsTopology | None:
    """How the pool members are joined, with the ones this many cannot make
    drawn with the count they need rather than left out."""
    translate = context.translate
    items = [
        Item(
            label=one.value,
            value=one,
            detail=translate("no redundancy") if one is ZfsTopology.STRIPE else "",
            disabled_because=""
            if members >= one.minimum
            else translate("needs at least {count}").format(count=one.minimum),
        )
        for one in ZfsTopology
    ]
    answer = _current_menu(
        screen,
        context,
        translate("Pool topology"),
        items,
        context.layout.topology,
    )
    return answer.unwrap() if answer.chosen else None


def _edit_pool_encryption(screen: Screen, context: Context) -> Answer[str]:
    edited = _edit_passphrase(
        screen,
        context,
        context.layout.passphrase_file,
        context.translate("Encrypt the pool?"),
    )
    if edited.chosen:
        context.layout.passphrase_file = edited.unwrap()
    return edited


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
_DELETE: Final[str] = "delete"
_STATUS: Final[str] = "status"


def _edit_slice(
    screen: Screen, context: Context, disk: manual.Disk, current: manual.Slice | None
) -> manual.Slice | None:
    """One partition as a list of fields, or None to delete it."""
    translate = context.translate
    entry = current or manual.Slice(
        index=disk.next_index(),
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
        field = answer.unwrap()
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
    return [
        descriptor.row((entry, purpose), translate)
        for descriptor in _SLICE_FIELDS
        if descriptor.key in {item.value for item in _slice_field_items(entry, purpose, translate)}
    ]


def _slice_field_items(
    entry: manual.Slice, purpose: manual.Purpose, translate: Catalog
) -> list[Item[str]]:
    """Every field with its value, and why one that does not apply cannot be
    opened."""
    no_filesystem = translate("this purpose fixes the filesystem")
    purpose_labels = {
        "root": translate("root"),
        "esp": translate("esp"),
        "boot": translate("boot"),
        "home": translate("home"),
        "var": translate("var"),
        "swap": translate("swap"),
        "zfs pool member": translate("zfs pool member"),
        "raid array member": translate("raid array member"),
        "bios-boot": translate("bios-boot"),
        "other": translate("other"),
    }
    return [
        Item(label=translate("Size"), value=_SIZE, detail=_size_of(entry, translate)),
        Item(label=translate("Purpose"), value=_PURPOSE, detail=purpose_labels[purpose.label]),
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
        *(
            [
                Item(
                    label=translate("Encryption"),
                    value=_ENCRYPTION,
                    detail=translate("on") if entry.passphrase_file else translate("off"),
                    # Firmware reads the esp itself and cannot open a container,
                    # so an encrypted esp never boots.
                    disabled_because=(
                        translate("firmware cannot open a container to read the esp")
                        if purpose.role is PartitionRole.ESP
                        else ""
                    ),
                )
            ]
            if purpose.role is not PartitionRole.ZFS
            else []
        ),
        Item(
            label=translate("What happens to it"),
            value=_STATUS,
            detail=translate(entry.status.value),
        ),
        # Only a row this table invented: one already on the disk is removed by
        # answering `delete`, which is an edit to the table and not to the list.
        *(
            [Item(label=translate("Take this row off the table"), value=_DELETE)]
            if entry.status is manual.SliceStatus.CREATE
            else []
        ),
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
    descriptor = next((one for one in _SLICE_FIELDS if one.key == field), None)
    if descriptor is None:
        return None
    changed = descriptor.edit(screen, context, (entry, purpose))
    return changed[0] if changed is not None else None


def _edit_field_legacy(
    screen: Screen,
    context: Context,
    entry: manual.Slice,
    purpose: manual.Purpose,
    field: str,
) -> manual.Slice | None:
    """The one screen behind a field, or None when the operator went back."""
    translate = context.translate
    if field == _STATUS:
        offered = (
            [manual.SliceStatus.CREATE]
            if entry.status is manual.SliceStatus.CREATE
            else [
                manual.SliceStatus.KEEP,
                manual.SliceStatus.FORMAT,
                manual.SliceStatus.DELETE,
            ]
        )
        chosen_status = _current_menu(
            screen,
            context,
            translate("What happens to it"),
            [
                Item(label=translate(one.value), value=one, detail=translate(manual.STATUS_REASONS[one]))
                for one in offered
            ],
            entry.status,
        )
        if not chosen_status.chosen:
            return None
        return replace(entry, status=chosen_status.unwrap())
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
        picked = _current_menu(
            screen,
            context,
            translate("What is this partition for?"),
            [
                Item(
                    label=one.label,
                    value=one,
                    disabled_because=(
                        context.zfs_unavailable if one.role is PartitionRole.ZFS else ""
                    ),
                )
                for one in manual.PURPOSES
            ],
            purpose,
        )
        if not picked.chosen:
            return None
        return _apply_purpose(entry, picked.unwrap())
    if field == _FILESYSTEM:
        # zfs is listed here as well as under the purpose, because that is
        # where anyone choosing a filesystem looks for it. It is a pool, so
        # picking it changes what the partition is rather than how it is
        # formatted.
        items: list[Item[FilesystemType | None]] = [
            Item(label=one.value, value=one) for one in FilesystemType
        ]
        items.append(
            Item(
                label="zfs",
                value=None,
                detail=translate("a pool member, not a filesystem"),
                disabled_because=context.zfs_unavailable,
            )
        )
        answered = _current_menu(
            screen,
            context,
            translate("Filesystem"),
            items,
            entry.filesystem,
        )
        if not answered.chosen:
            return None
        kind = answered.unwrap()
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


def _slice_descriptor(key: str) -> _FieldDescriptor[tuple[manual.Slice, manual.Purpose]]:
    def row(value: tuple[manual.Slice, manual.Purpose], translate: Catalog) -> Item[str]:
        return next(item for item in _slice_field_items(*value, translate) if item.value == key)

    def edit(
        screen: Screen, context: Context, value: tuple[manual.Slice, manual.Purpose]
    ) -> tuple[manual.Slice, manual.Purpose] | None:
        changed = _edit_field_legacy(screen, context, value[0], value[1], key)
        return (changed, value[1]) if changed is not None else None

    return _FieldDescriptor(key, row, edit)


_SLICE_FIELDS: tuple[_FieldDescriptor[tuple[manual.Slice, manual.Purpose]], ...] = tuple(
    _slice_descriptor(key)
    for key in (_SIZE, _PURPOSE, _FILESYSTEM, _MOUNTPOINT, _LABEL, _ENCRYPTION, _STATUS, _DELETE)
)


def _apply_purpose(entry: manual.Slice, purpose: manual.Purpose) -> manual.Slice:
    """Everything the purpose decides, in one place: picking `swap` has to drop
    the filesystem and the mount point it had as `root`."""
    return replace(
        entry,
        role=purpose.role,
        filesystem=entry.filesystem if purpose.chooses_filesystem else purpose.filesystem,
        mountpoint=entry.mountpoint if purpose.asks_mountpoint else purpose.mountpoint,
        passphrase_file=("" if purpose.role is PartitionRole.ZFS else entry.passphrase_file),
    )


def _edit_slice_encryption(
    screen: Screen, context: Context, entry: manual.Slice, purpose: manual.Purpose
) -> manual.Slice | None:
    translate = context.translate
    return _edit_passphrase(
        screen,
        context,
        entry.passphrase_file,
        translate("Encrypt this partition?"),
    ).map(lambda staged_path: replace(entry, passphrase_file=staged_path)).value


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
            informed = say(screen, context, f"{translate('Not a package name')}: {' '.join(bad)}")
            if not informed.chosen:
                return Answer(informed.outcome)
            continue
        return Answer(
            Outcome.CHOSE, replace(config, packages=replace(config.packages, extra=good))
        )


def _typed_beside_automatic(
    screen: Screen,
    context: Context,
    *,
    title: str,
    prompt: str,
    typed: tuple[str, ...],
    automatic: tuple[automatic_values.Added, ...],
    accepts: Callable[[str], tuple[tuple[str, ...], tuple[str, ...]]],
    rejected: str,
) -> Answer[tuple[str, ...]]:
    """One editable list, and under it what the installer adds by itself.

    The automatic rows are drawn disabled with their reason: an operator who
    can see that `root=` and `rd.luks.uuid=` are already handled stops adding a
    second copy of them, and one who cannot see it finds them in the installed
    system with nothing to attribute them to.
    """
    translate = context.translate
    while True:
        items: list[Item[str]] = [
            Item(
                label=prompt,
                value="edit",
                detail=" ".join(typed) or translate("none"),
            )
        ]
        items += [
            Item(
                label=one.value,
                value="",
                disabled_because=translate("added for you"),
                detail=(
                    f"{translate(one.because)} ({one.source})"
                    if one.source
                    else translate(one.because)
                ),
            )
            for one in automatic
        ]
        menu: Menu[str] = Menu(title=title, items=items, footer=footer(translate))
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        entered = TextField(
            title=prompt,
            value=" ".join(typed),
            footer=footer(translate),
        ).run(screen)
        if not entered.chosen:
            return Answer(entered.outcome)
        good, bad = accepts(entered.unwrap())
        if bad:
            informed = say(screen, context, f"{rejected}: {' '.join(bad)}")
            if not informed.chosen:
                return Answer(informed.outcome)
            continue
        return Answer(Outcome.CHOSE, good)


def kernel_cmdline_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Parameters appended to every boot entry, beside the ones derived from
    the disk layout.

    All three bootloaders read the same list: GRUB writes it into
    `GRUB_CMDLINE_LINUX_DEFAULT`, systemd-boot into `/etc/kernel/cmdline`, and
    ZFSBootMenu into the pool's `org.zfsbootmenu:commandline`. Nothing here
    depends on which one is installed.
    """
    answer = _typed_beside_automatic(
        screen,
        context,
        title=context.translate("Kernel command line"),
        prompt=context.translate("Parameters to append, separated by spaces"),
        typed=config.bootloader.kernel_params,
        automatic=automatic_values.kernel_parameters(config),
        accepts=atoms.split_kernel_parameters,
        rejected=context.translate(
            "A kernel parameter cannot contain a quote, a backslash or $"
        ),
    )
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, bootloader=replace(config.bootloader, kernel_params=answer.unwrap())),
    )


def use_flags_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """`USE` in `make.conf`, beside what the chosen groups already ask for.

    A flag typed here is appended after the profile's own, so `-foo` turns off
    something the profile set. The groups' flags are listed rather than merged
    into the field: an operator who deletes `wayland` from a field that was
    pre-filled with it has silently overridden their desktop.
    """
    answer = _typed_beside_automatic(
        screen,
        context,
        title=context.translate("USE flags"),
        prompt=context.translate("Flags to add, separated by spaces"),
        typed=config.portage.use,
        automatic=automatic_values.use_flags(config, context.groups),
        accepts=atoms.split_use_flags,
        rejected=context.translate("Not a USE flag"),
    )
    if not answer.chosen:
        return Answer(answer.outcome)
    values = answer.unwrap()
    _record_operator(context, ValueKind.USE_FLAG, values)
    return Answer(
        Outcome.CHOSE, replace(config, portage=replace(config.portage, use=values))
    )


def video_cards_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """`VIDEO_CARDS` on top of what the driver choice contributes.

    A hybrid machine needs more than one: an AMD laptop with an NVIDIA card
    carries `amdgpu radeonsi nvidia`, and the driver row offers one group. The
    values are USE_EXPAND flags, so they are checked as flags.
    """
    answer = _typed_beside_automatic(
        screen,
        context,
        title=context.translate("VIDEO_CARDS"),
        prompt=context.translate("Values to add, separated by spaces"),
        typed=config.portage.video_cards,
        automatic=automatic_values.video_cards(config, context.groups),
        accepts=atoms.split_use_flags,
        rejected=context.translate("Not a VIDEO_CARDS value"),
    )
    if not answer.chosen:
        return Answer(answer.outcome)
    values = answer.unwrap()
    _record_operator(context, ValueKind.VIDEO_CARD, values)
    return Answer(
        Outcome.CHOSE,
        replace(config, portage=replace(config.portage, video_cards=values)),
    )


def input_devices_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """`INPUT_DEVICES`, which make.conf replaces outright rather than adds to.

    Emptying it leaves a machine with no pointer driver, so `libinput` is the
    default rather than something the operator has to know to type.
    """
    answer = _typed_beside_automatic(
        screen,
        context,
        title=context.translate("INPUT_DEVICES"),
        prompt=context.translate("Values to add, separated by spaces"),
        typed=config.portage.input_devices,
        automatic=(),
        accepts=atoms.split_use_flags,
        rejected=context.translate("Not an INPUT_DEVICES value"),
    )
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, portage=replace(config.portage, input_devices=answer.unwrap())),
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

    Locale and timezone follow the language; the mirror region does not, and is
    read from where the machine reaches the internet. Every one of these stays
    a row the operator can change.
    """

    locale: str
    timezone: str
    #: True for the languages the cjktty patch is the point of. It pulls in
    #: gentoo-zh, so it is not a default for a language that would not use the
    #: rest of that overlay.
    cjk_console: bool = False


#: One row per interface language. Keyed by the same tags as the catalogs.
LANGUAGE_DEFAULTS: Final[dict[str, LanguageDefaults]] = {
    "en": LanguageDefaults("en_US.UTF-8", "UTC"),
    "zh-CN": LanguageDefaults("zh_CN.UTF-8", "Asia/Shanghai", True),
    "zh-TW": LanguageDefaults("zh_TW.UTF-8", "Asia/Taipei", True),
    # cjktty is what puts Chinese, Japanese and Korean on the console, so all
    # four of those catalogs take the patched kernel and not only the two
    # Chinese ones.
    "ja": LanguageDefaults("ja_JP.UTF-8", "Asia/Tokyo", True),
    "ko": LanguageDefaults("ko_KR.UTF-8", "Asia/Seoul", True),
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
    )
    if not chosen.cjk_console:
        return seeded
    # The patched kernel is what puts CJK on the console, and it is in gentoo-zh
    # and nowhere else, so the overlay comes with it or the row is unusable.
    return replace(
        seeded,
        kernel=replace(seeded.kernel, source=KernelSource.CJK_BIN),
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
    return answer.unwrap() if answer.chosen else context.tag


#: What each license set allows, in the order the menu offers them.
LICENSES: tuple[tuple[str, str], ...] = (
    ("@FREE", "free software and free documentation only"),
    ("@FREE @BINARY-REDISTRIBUTABLE", "also firmware and other redistributable binaries"),
    ("*", "every license"),
)


#: What the profile accepts when nothing widens it, and what the button below
#: puts back.
DEFAULT_LICENSES: Final[tuple[str, ...]] = ("@FREE",)


def accept_every_license_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """`ACCEPT_LICENSE="*"` from the main menu, or the profile's own default.

    The Licenses row is two levels down under Compiler, and an operator
    installing WeChat or an NVIDIA driver meets the question as a refusal
    instead: `net-im/wemeet` masked, the install over. Two answers rather than
    a toggle, because every other row keeps its value when it is opened and
    accepted again.
    """
    translate = context.translate
    every = ("*",)
    items = [
        Item(
            label="*",
            value=" ".join(every),
            detail=translate("every license, including proprietary ones"),
        ),
        Item(
            label=" ".join(DEFAULT_LICENSES),
            value=" ".join(DEFAULT_LICENSES),
            detail=translate("free software and free documentation only"),
        ),
    ]
    menu: Menu[str] = Menu(
        title=translate("Accept every license"),
        items=items,
        current=" ".join(config.portage.accept_license),
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            portage=replace(config.portage, accept_license=tuple(answer.unwrap().split())),
        ),
    )


def license_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """`ACCEPT_LICENSE`. The default refuses anything that is not free, and a
    machine that needs firmware will not install until this is widened."""
    translate = context.translate
    items = [
        Item(label=value, value=value, detail=translate(reason)) for value, reason in LICENSES
    ]
    menu: Menu[str] = Menu(
        title=translate("Licenses to accept"),
        items=items,
        current=" ".join(config.portage.accept_license),
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            portage=replace(config.portage, accept_license=tuple(answer.unwrap().split())),
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
        title=translate("Compile jobs"),
        items=items,
        footer=footer(translate),
        # Empty means follow the machine, which is the first row. Passing the
        # empty string matched no row, so reopening this screen and accepting
        # pinned a number the operator had deliberately left unpinned.
        current=config.portage.makeopts or f"-j{cores}",
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    picked = answer.unwrap()
    # The machine's own count is stored as nothing at all, which is what
    # "follow this machine" means: a configuration saved with `-j32` builds
    # with 32 jobs on a laptop with four cores.
    jobs = "" if picked == f"-j{cores}" else picked
    return Answer(
        Outcome.CHOSE, replace(config, portage=replace(config.portage, makeopts=jobs))
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
        title=translate("Compiler flags"),
        items=items,
        current=config.portage.common_flags,
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()
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
    if not picked.chosen:
        return Answer(picked.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, keymap_initramfs=picked.unwrap())),
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
        **answers(translate),
        title=translate("Use DHCP?"),
        footer=footer(translate),
        current=not system.addresses,
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
        chosen = answer.unwrap()
        if chosen < 0:
            keys.pop(-chosen - 1)
            continue
        if chosen == 3:
            changed_keys = tuple(keys)
            if not changed_keys:
                config = replace(
                    config,
                    kernel=replace(
                        config.kernel,
                        remote_unlock=replace(config.kernel.remote_unlock, enabled=False),
                    ),
                )
            return Answer(
                Outcome.CHOSE,
                replace(config, system=replace(config.system, authorized_keys=changed_keys)),
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
        say(screen, context, str(error))
        return ""
    # A paste holding several keys is the normal case for one person's file,
    # and taking the first line silently would drop the rest.
    for line in body.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            return _checked_key(screen, context, line)
    say(screen, context, translate("that address returned no key"))
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
        say(screen, context, str(error))
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
    # Refused here rather than at the Install row: both rules live in
    # `compat.py`, and this is the screen the answer is given on. Asked of a
    # candidate with it enabled, because the rules fire on that trait.
    candidate = replace(
        config, kernel=replace(config.kernel, remote_unlock=replace(unlock, enabled=True))
    )
    blocking = compat.violations(candidate)
    if blocking and not unlock.enabled:
        say(screen, context, context.translate(blocking[0].reason))
        return Answer(Outcome.BACK)
    asked = Confirm(
        **answers(translate),
        title=translate("Unlock the root over SSH from the initramfs?"),
        footer=footer(translate),
        current=config.kernel.remote_unlock.enabled,
    ).run(screen)
    if not asked.chosen:
        return Answer(asked.outcome)
    if not asked.unwrap():
        return Answer(
            Outcome.CHOSE,
            replace(config, kernel=replace(config.kernel, remote_unlock=replace(unlock, enabled=False))),
        )
    def validated(values: list[str]) -> Answer[InstallConfig] | FormRejected:
        port, address, gateway, interface = (one.strip() for one in values)
        if not port.isdigit():
            return FormRejected(
                translate("The port has to be a number."),
                {0: port, 1: address, 2: gateway, 3: interface},
            )
        return Answer(
            Outcome.CHOSE,
            replace(
                config,
                kernel=replace(
                    config.kernel,
                    remote_unlock=replace(
                        unlock,
                        enabled=True,
                        port=int(port),
                        address=address,
                        gateway=gateway,
                        interface=interface,
                    ),
                ),
            ),
        )

    return Form(
        title=translate("Remote unlock"),
        fields=[
            Field(label=translate("Port"), value=str(unlock.port), placeholder="222"),
            Field(
                label=translate("Address"),
                value=unlock.address,
                placeholder=translate("192.0.2.10/24, or empty for DHCP"),
            ),
            Field(
                label=translate("Gateway"),
                value=unlock.gateway,
                placeholder=translate("192.0.2.1, needed to answer off this subnet"),
            ),
            Field(
                label=translate("Interface"),
                value=unlock.interface,
                placeholder=translate("eth0, or empty for whichever comes up"),
            ),
        ],
        footer=footer(translate),
        done=translate("Done"),
    ).run_validated(screen, validated)


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
        # Without this the cursor starts on `allowed`, so reopening the row
        # and pressing enter widened root's access over ssh on a machine that
        # had refused it.
        current=config.system.sshd_root_login,
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, sshd_root_login=answer.unwrap())),
    )
