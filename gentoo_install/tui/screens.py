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
    Bootloader,
    BootloaderConfig,
    DiskConfig,
    Firmware,
    InitSystem,
    InstallConfig,
    KernelSource,
    Overlay,
    PortageConfig,
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
    return Answer(
        Outcome.CHOSE, replace(config, system=replace(config.system, init=answer.unwrap()[0]))
    )


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


#: Screen order, which is the order of `docs/design.md`.
STEPS: tuple[Step, ...] = (
    disk_screen,
    layout_screen,
    erase_screen,
    system_screen,
    init_screen,
    root_password_screen,
    kernel_screen,
    bootloader_screen,
    packages_screen,
)
