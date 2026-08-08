"""One screen per decision, each a function of the configuration so far.

A screen never mutates what it was given: it returns a new `InstallConfig`, and
`app.py` re-validates before moving on. Every option a compatibility rule
excludes is drawn greyed with that rule's own sentence, so the interface and the
validator never disagree about why something cannot be chosen.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Final, Sequence

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
    Networking,
    Overlay,
    PortageConfig,
    Sync,
    User,
)
from ..model.device import FilesystemType, PartitionRole, TableType
from ..plan.kernel import KERNEL_PACKAGES
from ..model.size import Size
from ..errors import GentooInstallError, ValidationFailed
from ..model import atoms, manual, paste, sshkey
from ..model.templates import Choice, Layout, build
from ..model.validate import validate
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
    context.inspect_disk(context.choice.disk)
    return Answer(Outcome.CHOSE, _rebuild(config, context.choice))


def layout_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    items: list[Item[tuple[Layout | None, FilesystemType]]] = [
        Item(label="ext4", value=(Layout.WHOLE_DISK, FilesystemType.EXT4)),
        Item(label="xfs", value=(Layout.WHOLE_DISK, FilesystemType.XFS)),
        Item(label="btrfs with @ and @home", value=(Layout.WHOLE_DISK_BTRFS, FilesystemType.BTRFS)),
        Item(label="zfs with ZFSBootMenu", value=(Layout.WHOLE_DISK_ZFS, FilesystemType.EXT4)),
        Item(label="manual: choose the partitions yourself", value=(None, FilesystemType.EXT4)),
    ]
    menu: Menu[tuple[Layout | None, FilesystemType]] = Menu(
        title=translate("Layout"), items=items, footer=_footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    layout, filesystem = answer.unwrap()[0]
    if layout is None:
        context.manual = True
        return partitions_screen(screen, config, context)
    context.manual = False
    context.choice = replace(context.choice, layout=layout, filesystem=filesystem)
    changed = _rebuild(config, context.choice)
    if layout is Layout.WHOLE_DISK_ZFS:
        changed = _zfs_bootloader(screen, changed, context)
    return Answer(Outcome.CHOSE, changed)


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
        footer=_footer(translate),
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
    context.erase_confirmed = answer.unwrap()
    return Answer(Outcome.CHOSE, config)


def system_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    field = TextField(
        title=translate("Hostname"),
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
        title=translate("Init system"),
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
    field = TextField(
        title=translate("Root password"), masked=True, footer=_footer(translate)
    )
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
    named = TextField(
        title=translate("User name, or empty for root only"), footer=_footer(translate)
    ).run(screen)
    if not named.chosen:
        return Answer(named.outcome)
    name = named.unwrap().strip()
    if not name:
        return Answer(Outcome.CHOSE, replace(config, system=replace(config.system, users=())))
    typed = TextField(
        title=translate("Password for") + f" {name}", masked=True, footer=_footer(translate)
    ).run(screen)
    if not typed.chosen:
        return Answer(typed.outcome)
    granted = Confirm(
        title=translate("Give this account sudo?"), footer=_footer(translate)
    ).run(screen)
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
        title=translate("Bootloader"), items=items, footer=_footer(translate)
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
    (KernelSource.DIST_BIN, "prebuilt, minutes"),
    (KernelSource.DIST_SOURCE, "built here, hours"),
    (KernelSource.CJK, "cjktty for CJK on the console, from gentoo-zh"),
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
        footer=_footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()[0]
    changed = replace(config, kernel=replace(config.kernel, source=chosen))
    if chosen is KernelSource.CJK:
        # The package is in gentoo-zh and in no other repository, so choosing
        # it is consenting to that overlay rather than having it added quietly.
        changed = replace(changed, portage=_with_gentoo_zh(changed))
    return Answer(Outcome.CHOSE, changed)


#: The profile each desktop is built against. Verified against
#: profiles.desc: a systemd variant is the same path plus /systemd.
DESKTOP_PROFILES: dict[str, str] = {
    "": "default/linux/amd64/23.0",
    "console": "default/linux/amd64/23.0",
    "plasma": "default/linux/amd64/23.0/desktop/plasma",
    "plasma-full": "default/linux/amd64/23.0/desktop/plasma",
    "gnome": "default/linux/amd64/23.0/desktop/gnome",
    "gnome-full": "default/linux/amd64/23.0/desktop/gnome",
    "xfce": "default/linux/amd64/23.0/desktop",
}


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
        for name in sorted(DESKTOP_PROFILES)
    ]
    menu: Menu[str] = Menu(
        title=translate("Desktop and applications"), items=items, footer=_footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    desktop = answer.unwrap()[0]
    profile = _profile_for(DESKTOP_PROFILES[desktop], config.system.init)
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            packages=replace(config.packages, desktop=desktop),
            portage=replace(config.portage, profile=profile),
        ),
    )


def packages_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    translate = context.translate
    # A desktop is chosen on its own screen, because it also decides the
    # profile; this one offers what can be added beside any of them.
    names = sorted(name for name in context.groups if name not in DESKTOP_PROFILES)
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


#: Taken from profiles.desc for amd64 23.0. A systemd profile is the same path
#: plus /systemd, which `_profile_for` relies on.
#: `zpool create` refuses anything shorter, and LUKS with a short passphrase is
#: not worth offering either.
PASSPHRASE_MINIMUM: Final[int] = 8

#: The overlays this installer knows how to add, and where each syncs from.
OVERLAYS: dict[str, str] = {
    "gentoo-zh": GENTOO_ZH,
    "gig": "https://github.com/gentoo-zh/gig-overlay.git",
}

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
        footer=_footer(translate),
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
        footer=_footer(translate),
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
    wanted = Confirm(
        title=translate("Encrypt the root filesystem?"), footer=_footer(translate)
    ).run(screen)
    if not wanted.chosen:
        return Answer(wanted.outcome)
    if not wanted.unwrap():
        context.choice = replace(context.choice, passphrase_file="")
        return Answer(Outcome.CHOSE, _rebuild(config, context.choice))
    staged = _ask_passphrase(screen, context)
    if not staged:
        return Answer(Outcome.BACK)
    context.choice = replace(context.choice, passphrase_file=staged)
    return Answer(Outcome.CHOSE, _rebuild(config, context.choice))


def _ask_passphrase(screen: Screen, context: Context) -> str:
    """The passphrase typed twice, staged in a file whose path is returned.

    Empty when the operator went back. The configuration holds the path and
    never the passphrase, because it is copied into the target and the install
    log is what people paste into bug reports.
    """
    translate = context.translate
    while True:
        first = TextField(
            title=translate("Passphrase"), masked=True, footer=_footer(translate)
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
            title=translate("Passphrase again"), masked=True, footer=_footer(translate)
        ).run(screen)
        if not again.chosen:
            return ""
        if again.unwrap() != typed:
            _say(screen, context, translate("The two do not match."))
            continue
        return context.stage_passphrase(typed)


def _say(screen: Screen, context: Context, message: str) -> None:
    """One line the operator has to acknowledge, so a rejected entry is not
    silently redrawn as an empty field."""
    Menu(
        title=message,
        items=[Item(label=context.translate("Continue"), value=0)],
        footer=_footer(context.translate),
    ).run(screen)


def swap_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    translate = context.translate
    items: list[Item[str]] = [
        Item(label="none", value=""),
        Item(label="4 GiB partition", value="4GiB"),
        Item(label="8 GiB partition", value="8GiB"),
        Item(label="zram, 4 GiB compressed in memory", value="zram:4GiB"),
    ]
    menu: Menu[str] = Menu(title=translate("Swap"), items=items, footer=_footer(translate))
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
    items: list[Item[tuple[bool, BinhostChannel, str]]] = [
        Item(
            label="official, x86-64",
            value=(True, BinhostChannel.OFF, "x86-64"),
            detail=translate("works on every amd64 machine"),
        ),
        Item(
            label="official, x86-64-v3",
            value=(True, BinhostChannel.OFF, "x86-64-v3"),
            # `ld.so` lists the subarchitectures it would search, so a machine
            # that cannot run these is told rather than left to find out when
            # the first binary package dies with an illegal instruction. The
            # reason replaces the detail: both together overflow 80 columns.
            detail=(
                translate("this CPU runs it, and the packages are built for it")
                if context.supports_v3
                else ""
            ),
            disabled_because=(
                "" if context.supports_v3 else translate("this CPU cannot run it")
            ),
        ),
        Item(
            label="official and gentoo-zh",
            value=(True, BinhostChannel.STABLE, "x86-64"),
            detail=translate("gentoo-zh builds x86-64 only"),
        ),
        Item(
            label="compile everything from source",
            value=(False, BinhostChannel.OFF, "x86-64"),
            detail=translate("hours rather than minutes"),
        ),
    ]
    menu: Menu[tuple[bool, BinhostChannel, str]] = Menu(
        title=translate("Binary packages"), items=items, footer=_footer(translate)
    )
    if context.supports_v3:
        # Recommended by starting on it: it is the faster of the two and this
        # machine has already proved it can run it.
        menu.cursor = 1
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    official, community, subarch = answer.unwrap()[0]
    portage = replace(
        config.portage,
        binhost=Binhost(official=official, community=community, subarch=subarch),
    )
    if community is not BinhostChannel.OFF:
        portage = _with_gentoo_zh(replace(config, portage=portage))
    return Answer(Outcome.CHOSE, replace(config, portage=portage))


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
        footer=_footer(translate),
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
    desktop_screen,
    packages_screen,
    sshd_screen,
    overview_screen,
)


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
        footer=_footer(context.translate),
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
        title=translate("Partition table"), items=items, footer=_footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    context.choice = replace(context.choice, table=answer.unwrap()[0])
    return Answer(Outcome.CHOSE, _rebuild(config, context.choice))


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
        title=translate("Firmware"), items=items, footer=_footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    firmware = answer.unwrap()[0]
    context.choice = replace(context.choice, firmware=firmware)
    changed = _rebuild(config, context.choice)
    return Answer(
        Outcome.CHOSE,
        replace(changed, bootloader=replace(changed.bootloader, firmware=firmware)),
    )


def keymap_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    field = TextField(
        title=context.translate("Keyboard layout"),
        value=config.system.keymap,
        footer=_footer(context.translate),
    )
    answer = field.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, keymap=answer.unwrap() or "us")),
    )


def repositories_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """The overlays, each chosen on its own. One is never added behind the
    operator's back, so selecting ZFSBootMenu ticks gentoo-zh visibly here."""
    translate = context.translate
    present = {overlay.name for overlay in config.portage.overlays}
    items = [
        Item(label=name, value=name, detail=uri) for name, uri in sorted(OVERLAYS.items())
    ]
    menu: Menu[str] = Menu(
        title=translate("Optional repositories"),
        items=items,
        multiple=True,
        selected={index for index, item in enumerate(items) if item.value in present},
        footer=_footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = tuple(
        Overlay(name=name, sync_uri=OVERLAYS[name]) for name in answer.unwrap()
    )
    return Answer(Outcome.CHOSE, replace(config, portage=replace(config.portage, overlays=chosen)))


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
        title=translate("Network configuration"), items=items, footer=_footer(translate)
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
            footer=f"{_layout_problem(context, config)}  {_footer(translate)}".strip(),
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        chosen = answer.unwrap()[0]
        if chosen < 0:
            continue
        if chosen == len(rows) + 1:
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
_LABEL: Final[str] = "label"
_ENCRYPTION: Final[str] = "encryption"
_DELETE: Final[str] = "delete"
_DONE: Final[str] = "done"


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
            footer=_footer(translate),
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
            footer=_footer(translate),
        ).run(screen)
        if not typed.chosen:
            return None
        text = typed.unwrap().strip()
        return replace(entry, size=Size.parse(text) if text else None)
    if field == _PURPOSE:
        picked = Menu(
            title=translate("What is this partition for?"),
            items=[Item(label=one.label, value=one) for one in manual.PURPOSES],
            footer=_footer(translate),
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
            title=translate("Filesystem"), items=items, footer=_footer(translate)
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
            footer=_footer(translate),
        ).run(screen)
        if not where.chosen:
            return None
        return replace(entry, mountpoint=where.unwrap().strip())
    if field == _LABEL:
        named = TextField(
            title=translate("Label"),
            value=entry.label,
            placeholder=translate("gentoo"),
            footer=_footer(translate),
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
        title=(
            translate("Encrypt the pool?")
            if purpose.role is PartitionRole.ZFS
            else translate("Encrypt this partition?")
        ),
        footer=_footer(translate),
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
            footer=_footer(translate),
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


#: One row per interface language. Keyed by the same tags as the catalogs.
LANGUAGE_DEFAULTS: Final[dict[str, LanguageDefaults]] = {
    "en": LanguageDefaults("en_US.UTF-8", "UTC", MirrorRegion.GLOBAL),
    "zh-CN": LanguageDefaults("zh_CN.UTF-8", "Asia/Shanghai", MirrorRegion.CN),
    "zh-TW": LanguageDefaults("zh_TW.UTF-8", "Asia/Taipei", MirrorRegion.GLOBAL),
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
    return replace(
        config,
        system=replace(
            config.system, locale=chosen.locale, timezone=chosen.timezone, locales=locales
        ),
        portage=replace(
            config.portage, mirrors=replace(config.portage.mirrors, region=chosen.mirror)
        ),
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
        title=translate("Licenses to accept"), items=items, footer=_footer(translate)
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
        title=translate("Compile jobs"), items=items, footer=_footer(translate)
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
        title=translate("Compiler flags"), items=items, footer=_footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    chosen = answer.unwrap()[0]
    if not chosen:
        typed = TextField(
            title=translate("Compiler flags"),
            value=config.portage.common_flags,
            footer=_footer(translate),
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
    field = TextField(
        title=translate("Keyboard the initramfs uses, empty to follow the console"),
        value=config.system.keymap_initramfs,
        footer=_footer(translate),
    )
    answer = field.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, keymap_initramfs=answer.unwrap().strip())),
    )


def address_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """DHCP, or an address typed in. A machine on a network with no DHCP comes
    up unreachable otherwise."""
    translate = context.translate
    wanted = Confirm(
        title=translate("Use DHCP?"), footer=_footer(translate)
    ).run(screen)
    if not wanted.chosen:
        return Answer(wanted.outcome)
    if wanted.unwrap():
        return Answer(
            Outcome.CHOSE,
            replace(config, system=replace(config.system, address="", gateway="")),
        )
    address = TextField(
        title=translate("Address with its prefix, such as 192.0.2.10/24"),
        value=config.system.address,
        footer=_footer(translate),
    ).run(screen)
    if not address.chosen:
        return Answer(address.outcome)
    gateway = TextField(
        title=translate("Gateway"),
        value=config.system.gateway,
        footer=_footer(translate),
    ).run(screen)
    if not gateway.chosen:
        return Answer(gateway.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(
            config,
            system=replace(
                config.system,
                address=address.unwrap().strip(),
                gateway=gateway.unwrap().strip(),
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
            Item(label=_key_summary(key), value=-index - 1) for index, key in enumerate(keys)
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
            title=translate("SSH public keys"), items=items, footer=_footer(translate)
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
        footer=_footer(translate),
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
        footer=_footer(translate),
    ).run(screen)
    if not typed.chosen or not typed.unwrap().strip():
        return ""
    return _read_a_key(screen, context, paste.url_for(typed.unwrap()))


def _fetch_a_key(screen: Screen, context: Context) -> str:
    translate = context.translate
    typed = TextField(
        title=translate("URL of a public key"),
        placeholder=translate("https://example.com/id_ed25519.pub"),
        footer=_footer(translate),
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
        footer=_footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, sshd_root_login=answer.unwrap()[0])),
    )


def sync_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """How the ebuild repository is kept up to date after the first sync.

    The first sync is webrsync whichever this is: a stage3 has no `dev-vcs/git`,
    so a git sync cannot be the step that installs it.
    """
    translate = context.translate
    items = [
        Item(
            label=Sync.GIT.value,
            value=Sync.GIT,
            detail=translate("carries the history a signed sync checks"),
        ),
        Item(
            label=Sync.WEBRSYNC.value,
            value=Sync.WEBRSYNC,
            detail=translate("no git, and no history"),
        ),
    ]
    menu: Menu[Sync] = Menu(
        title=translate("Repository sync"), items=items, footer=_footer(translate)
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, portage=replace(config.portage, sync=answer.unwrap()[0]))
    )
