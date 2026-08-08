"""One screen per decision, each a function of the configuration so far.

A screen never mutates what it was given: it returns a new `InstallConfig`, and
`app.py` re-validates before moving on. Every option a compatibility rule
excludes is drawn greyed with that rule's own sentence, so the interface and the
validator never disagree about why something cannot be chosen.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Sequence

from ..i18n import Catalog
from ..model import compat
from ..model.config import (
    Binhost,
    BinhostChannel,
    Bootloader,
    BootloaderConfig,
    DiskConfig,
    Firmware,
    InitSystem,
    InstallConfig,
    KernelSource,
    MirrorRegion,
    Overlay,
    PortageConfig,
    User,
)
from ..model.device import FilesystemType
from ..model.size import Size
from ..model.templates import Choice, Layout, build
from ..plan.packages import Catalog as Groups
from .widgets import Answer, Confirm, Item, Menu, Outcome, Screen, TextField

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
    ) -> None:
        self.translate = translate
        #: Selector and a human description, from `exec/probe.py`.
        self.disks = disks
        self.groups = groups
        #: Injected rather than imported: the model layer does no I/O, and
        #: hashing runs `openssl` on the installing system.
        self.hash_password = hash_password
        #: Kept so the disk screen can rebuild the graph when one answer
        #: changes, rather than editing a graph it did not build.
        self.choice = Choice(disk=disks[0][0] if disks else "")


def _footer(translate: Catalog) -> str:
    return "  ".join(
        (
            f"[enter] {translate('Continue')}",
            f"[backspace] {translate('Back')}",
            f"[q] {translate('Cancel')}",
        )
    )


def _rebuild(config: InstallConfig, choice: Choice) -> InstallConfig:
    graph, root = build(choice)
    return replace(config, disk=DiskConfig(graph=graph, root=root))


def disk_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    if not context.disks:
        raise LookupError("no disk to install onto")
    menu: Menu[str] = Menu(
        title=translate("Disks"),
        items=[Item(label=name, value=name, detail=detail) for name, detail in context.disks],
        footer=_footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    context.choice = replace(context.choice, disk=answer.unwrap()[0])
    return Answer(Outcome.CHOSE, _rebuild(config, context.choice))


def layout_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    items = [
        Item(label="ext4", value=(Layout.WHOLE_DISK, FilesystemType.EXT4)),
        Item(label="xfs", value=(Layout.WHOLE_DISK, FilesystemType.XFS)),
        Item(label="btrfs with @ and @home", value=(Layout.WHOLE_DISK_BTRFS, FilesystemType.BTRFS)),
        Item(label="zfs with ZFSBootMenu", value=(Layout.WHOLE_DISK_ZFS, FilesystemType.EXT4)),
    ]
    menu: Menu[tuple[Layout, FilesystemType]] = Menu(
        title=translate("Disks"), items=items, footer=_footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    layout, filesystem = answer.unwrap()[0]
    context.choice = replace(context.choice, layout=layout, filesystem=filesystem)
    changed = _rebuild(config, context.choice)
    if layout is Layout.WHOLE_DISK_ZFS:
        # The only repository carrying sys-boot/zfsbootmenu, which the
        # compatibility table states and the validator would otherwise reject.
        changed = replace(
            changed,
            bootloader=replace(changed.bootloader, kind=Bootloader.ZFSBOOTMENU),
            portage=_with_gentoo_zh(changed),
        )
    return Answer(Outcome.CHOSE, changed)


def _with_gentoo_zh(config: InstallConfig) -> PortageConfig:
    if any(overlay.name == "gentoo-zh" for overlay in config.portage.overlays):
        return config.portage
    added = (*config.portage.overlays, Overlay(name="gentoo-zh", sync_uri=GENTOO_ZH))
    return replace(config.portage, overlays=added)


GENTOO_ZH = "https://github.com/gentoo-zh/overlay.git"


def erase_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """The one screen with no default: the disk name has to be typed."""
    translate = context.translate
    disk = context.choice.disk
    question = Confirm(
        title=f"{translate('This erases every partition on the disk.')} {disk}. "
        f"{translate('Type the disk name to confirm.')}",
        phrase=disk,
        footer=_footer(translate),
    )
    answer = question.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    if not answer.unwrap():
        return Answer(Outcome.BACK)
    return Answer(Outcome.CHOSE, config)


def system_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    field = TextField(
        title=translate("Target system"),
        value=config.system.hostname,
        footer=_footer(translate),
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
        title=translate("Target system"),
        items=[Item(label=init.value, value=init) for init in InitSystem],
        footer=_footer(translate),
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
    field = TextField(title=translate("Users"), masked=True, footer=_footer(translate))
    answer = field.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    hashed = context.hash_password(answer.unwrap())
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, root_password_hash=hashed))
    )


def user_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """One normal account with sudo, or none.

    An empty name leaves the system with root only, which is a choice a server
    install makes deliberately.
    """
    translate = context.translate
    named = TextField(title=translate("Users"), footer=_footer(translate)).run(screen)
    if not named.chosen:
        return Answer(named.outcome)
    name = named.unwrap().strip()
    if not name:
        return Answer(Outcome.CHOSE, replace(config, system=replace(config.system, users=())))
    typed = TextField(title=translate("Users"), masked=True, footer=_footer(translate)).run(screen)
    if not typed.chosen:
        return Answer(typed.outcome)
    granted = Confirm(title=translate("Users"), footer=_footer(translate)).run(screen)
    if not granted.chosen:
        return Answer(granted.outcome)
    user = User(
        name=name,
        groups=("wheel", "audio", "video", "usb"),
        sudo=granted.unwrap(),
        password_hash=context.hash_password(typed.unwrap()),
    )
    return Answer(Outcome.CHOSE, replace(config, system=replace(config.system, users=(user,))))


def mirror_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Which region's mirrors, and whether to measure them.

    Measuring costs a minute and saves hours on a slow link, so it is a
    question rather than a default.
    """
    translate = context.translate
    items: list[Item[tuple[MirrorRegion, bool]]] = [
        Item(label="official mirrors", value=(MirrorRegion.GLOBAL, False)),
        Item(label="mirrors in China", value=(MirrorRegion.CN, False)),
        Item(label="mirrors in China, fastest first", value=(MirrorRegion.CN, True)),
    ]
    menu: Menu[tuple[MirrorRegion, bool]] = Menu(
        title=translate("Portage"), items=items, footer=_footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    region, measure = answer.unwrap()[0]
    mirrors = replace(config.portage.mirrors, region=region, speed_test=measure)
    return Answer(Outcome.CHOSE, replace(config, portage=replace(config.portage, mirrors=mirrors)))


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
        title=translate("Kernel and bootloader"), items=items, footer=_footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, bootloader=replace(config.bootloader, kind=answer.unwrap()[0])),
    )


def kernel_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    menu: Menu[KernelSource] = Menu(
        title=translate("Kernel and bootloader"),
        items=[Item(label=source.value, value=source) for source in KernelSource],
        footer=_footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, kernel=replace(config.kernel, source=answer.unwrap()[0]))
    )


def packages_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    translate = context.translate
    names = sorted(context.groups)
    menu: Menu[str] = Menu(
        title=translate("Desktop and applications"),
        items=[Item(label=name, value=name) for name in names],
        multiple=True,
        footer=_footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()
    return Answer(
        Outcome.CHOSE,
        replace(config, packages=replace(config.packages, applications=tuple(chosen))),
    )


#: Offered as a list rather than free text: a mistyped locale is only found
#: when `locale -a` fails, which is after the stage3 is unpacked.
LOCALES: tuple[tuple[str, str], ...] = (
    ("zh_TW.UTF-8", "Chinese (Traditional)"),
    ("zh_CN.UTF-8", "Chinese (Simplified)"),
    ("en_US.UTF-8", "English"),
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
        title=translate("Target system"),
        items=[Item(label=f"{name}  {label}", value=name) for name, label in LOCALES],
        footer=_footer(translate),
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
    translate = context.translate
    menu: Menu[str] = Menu(
        title=translate("Target system"),
        items=[Item(label=zone, value=zone) for zone in TIMEZONES],
        footer=_footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, timezone=answer.unwrap()[0]))
    )


def swap_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    items: list[Item[str]] = [
        Item(label="none", value=""),
        Item(label="4 GiB partition", value="4GiB"),
        Item(label="8 GiB partition", value="8GiB"),
        Item(label="zram, 4 GiB compressed in memory", value="zram:4GiB"),
    ]
    menu: Menu[str] = Menu(title=translate("Disks"), items=items, footer=_footer(translate))
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()[0]
    if chosen.startswith("zram:"):
        return Answer(
            Outcome.CHOSE,
            replace(config, system=replace(config.system, zram=Size.parse(chosen.removeprefix("zram:")))),
        )
    context.choice = replace(context.choice, swap=Size.parse(chosen) if chosen else None)
    return Answer(Outcome.CHOSE, _rebuild(config, context.choice))


def binhost_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Binary packages shorten an install from hours to minutes, and the cost
    of each choice is on the screen rather than discovered while compiling."""
    translate = context.translate
    items: list[Item[tuple[bool, BinhostChannel]]] = [
        Item(label="official binary packages", value=(True, BinhostChannel.OFF)),
        Item(
            label="official and gentoo-zh binary packages",
            value=(True, BinhostChannel.STABLE),
        ),
        Item(label="compile everything from source", value=(False, BinhostChannel.OFF)),
    ]
    menu: Menu[tuple[bool, BinhostChannel]] = Menu(
        title=translate("Portage"), items=items, footer=_footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    official, community = answer.unwrap()[0]
    portage = replace(config.portage, binhost=Binhost(official=official, community=community))
    if community is not BinhostChannel.OFF:
        portage = _with_gentoo_zh(replace(config, portage=portage))
    return Answer(Outcome.CHOSE, replace(config, portage=portage))


def sshd_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    question = Confirm(title=translate("Network and SSH"), footer=_footer(translate))
    answer = question.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, sshd=answer.unwrap()))
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
        footer=_footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    question = Confirm(title=translate("Install"), footer=_footer(translate))
    confirmed = question.run(screen)
    if not confirmed.chosen:
        return Answer(confirmed.outcome)
    return Answer(Outcome.CHOSE, config) if confirmed.unwrap() else Answer(Outcome.BACK)


#: Screen order, which is the order of `docs/design.md`.
STEPS: tuple[Step, ...] = (
    disk_screen,
    layout_screen,
    swap_screen,
    erase_screen,
    locale_screen,
    timezone_screen,
    system_screen,
    init_screen,
    root_password_screen,
    user_screen,
    mirror_screen,
    binhost_screen,
    kernel_screen,
    bootloader_screen,
    packages_screen,
    sshd_screen,
    overview_screen,
)
