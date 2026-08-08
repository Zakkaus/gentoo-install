"""The main menu's rows: what each one is called, shows, and edits.

One table, read by the menu to draw itself and by the menu to dispatch. A row
knows three things: its label, how to render the value it currently holds, and
the screen that changes it. Nothing here draws anything.

The shape is deliberate. `archinstall` and `oddlama-gentoo-install` arrived at
the same one independently: a menu the operator re-enters in any order, not a
wizard that has to be cancelled and restarted to change an early answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final

from ..model.config import Bootloader, InitSystem, InstallConfig, KernelSource, Keywords
from ..plan.kernel import KERNEL_PACKAGES
from ..model import mirrors
from ..model.device import Existing, Luks, MdRaid, PartitionTable, VolumeGroup, ZfsPool
from . import screens
from .screens import Context, Step, footer
from .widgets import Answer, Item, Menu, Outcome, Screen, Style

#: Shown for a row the operator has not visited and that has no usable default.
UNSET: Final[str] = "not set"


@dataclass(frozen=True)
class Setting:
    """One row of the main menu."""

    key: str
    label: str
    #: What the row shows to the right of its label.
    value: Callable[[InstallConfig, Context], str]
    edit: Step | None
    #: A run cannot start until every required row has an answer.
    required: bool = False
    #: The rows behind this one when it opens a group. Held here so which rows
    #: are groups is one fact: `unanswered` walked its own list of three and
    #: missed the fourth, which would have reported `Kernel` for a row inside.
    rows: tuple[Setting, ...] = ()


def style_of(setting: Setting, config: InstallConfig, context: Context) -> Style:
    """Red for a required row with no answer, yellow for an optional row the
    operator has not opened. Colour repeats what the value already says: a
    console without it loses nothing."""
    if setting.required and setting.value(config, context) == UNSET:
        return Style.REQUIRED
    if not setting.required and setting.edit is not None and setting.key not in context.visited:
        return Style.UNTOUCHED
    return Style.PLAIN


def nested(title: str, rows: tuple[Setting, ...]) -> Step:
    """A row that opens a list of rows, drawn the same way the main menu is.

    Six decisions about one subject read as six unrelated rows in a menu of
    thirty; behind one row they read as the subject they belong to. Not a
    wizard: the list is re-enterable and every row shows its value.
    """

    def open(
        screen: Screen, config: InstallConfig, context: Context
    ) -> Answer[InstallConfig]:
        current = config
        cursor = 0
        while True:
            items = [
                Item(
                    label=context.translate(row.label),
                    value=index,
                    detail=row.value(current, context),
                    disabled_because="" if row.edit else context.translate("detected"),
                    style=style_of(row, current, context),
                )
                for index, row in enumerate(rows)
            ]
            items.append(Item(label=context.translate("Done"), value=len(rows)))
            menu: Menu[int] = Menu(
                title=context.translate(title),
                items=items,
                footer=footer(context.translate),
                cursor=cursor,
            )
            answer = menu.run(screen)
            cursor = menu.cursor
            if answer.outcome is Outcome.BACK:
                # Backspace is what this menu's own footer calls Back, so it
                # leaves the group and keeps what was edited inside it.
                return Answer(Outcome.CHOSE, current)
            if not answer.chosen:
                # The edits go with it: the operator who cancels and then says
                # no to leaving gets the group back as they left it.
                return Answer(answer.outcome, current)
            chosen = answer.unwrap()[0]
            if chosen == len(rows):
                return Answer(Outcome.CHOSE, current)
            editor = rows[chosen].edit
            if editor is None:
                continue
            context.visited.add(rows[chosen].key)
            edited = editor(screen, current, context)
            if edited.outcome is Outcome.CANCELLED:
                return Answer(Outcome.CANCELLED, current)
            if edited.chosen:
                current = edited.unwrap()

    return open


def _summary(rows: tuple[Setting, ...], take: int = 2) -> Callable[[InstallConfig, Context], str]:
    """What a grouped row shows: the first values behind it, and how many more.

    Not all of them: six joined by commas runs past 80 columns, and the part
    that gets truncated away is the end of the list.
    """

    def shown(config: InstallConfig, context: Context) -> str:
        first = ", ".join(row.value(config, context) for row in rows[:take])
        rest = len(rows) - take
        return f"{first} +{rest}" if rest > 0 else first

    return shown


def _shown(config: InstallConfig, context: Context) -> str:
    """A row with nothing to edit still has to say what it settled on."""
    return _firmware(config, context)


def _swap(config: InstallConfig, context: Context) -> str:
    from ..model.device import Swap

    if config.system.zram is not None:
        return f"zram {config.system.zram}"
    if config.disk.graph.of_type(Swap):
        return "a partition"
    return "none"


def _logger(config: InstallConfig, context: Context) -> str:
    """systemd needs none, so the row says that rather than naming a package
    the operator would then wonder why they have two of."""
    if config.system.init is InitSystem.SYSTEMD:
        return "journald"
    return config.system.logger.value


def _cron(config: InstallConfig, context: Context) -> str:
    return "cronie" if config.system.cron else context.translate("none")


def _kernel(config: InstallConfig, context: Context) -> str:
    return config.kernel.package or KERNEL_PACKAGES[config.kernel.source]


def _cpu_flags(config: InstallConfig, context: Context) -> str:
    return " ".join(config.portage.cpu_flags) or context.translate("what the profile sets")


def _kernel_version(config: InstallConfig, context: Context) -> str:
    return config.kernel.version or context.translate("not pinned")


def _keywords(config: InstallConfig, context: Context) -> str:
    return "~amd64" if config.portage.keywords is Keywords.TESTING else "amd64"


def _compiler(config: InstallConfig, context: Context) -> str:
    """The three the operator is most likely to have changed. The rest are on
    the screen behind this row."""
    portage = config.portage
    jobs = portage.makeopts or f"-j{context.cores}"
    return f"{jobs}, {portage.common_flags}, {' '.join(portage.accept_license)}"


def _bootloader(config: InstallConfig, context: Context) -> str:
    return f"{config.bootloader.kind.value}, {config.bootloader.firmware.value}"


def _network(config: InstallConfig, context: Context) -> str:
    return config.system.networking.value


def _unlock_keymap(config: InstallConfig, context: Context) -> str:
    chosen = config.system.keymap_initramfs
    return chosen if chosen else f"{config.system.keymap} (same as the console)"


def _address(config: InstallConfig, context: Context) -> str:
    system = config.system
    if not system.addresses:
        return "DHCP"
    where = system.interface or "auto"
    return f"{where}: {', '.join(system.addresses)}"


def _remote_unlock(config: InstallConfig, context: Context) -> str:
    unlock = config.kernel.remote_unlock
    if not unlock.enabled:
        return context.translate("off")
    return f"{unlock.port}, {unlock.address or 'DHCP'}"


def _keys(config: InstallConfig, context: Context) -> str:
    count = len(config.system.authorized_keys)
    return f"{count} authorised" if count else "none"


def _mirror(config: InstallConfig, context: Context) -> str:
    """Unset until a site is picked. Every repository is fetched from here, so
    the region a machine happens to default to is not an answer."""
    chosen = config.portage.mirrors
    if not chosen.site:
        return UNSET
    overlays = [overlay.name for overlay in config.portage.overlays]
    measured = ", measured" if chosen.speed_test else ""
    return f"{chosen.site}{measured}" + (f", {', '.join(overlays)}" if overlays else "")


def _firmware(config: InstallConfig, context: Context) -> str:
    detected = " (detected)" if config.bootloader.firmware is context.firmware else ""
    return f"{config.bootloader.firmware.value}{detected}"


def _drive(config: InstallConfig, context: Context) -> str:
    disks = [node.selector.rsplit("/", 1)[-1] for node in config.disk.graph.of_type(Existing)]
    return ", ".join(disks) if disks else UNSET


def _table(config: InstallConfig, context: Context) -> str:
    tables = {node.table.value for node in config.disk.graph.of_type(PartitionTable)}
    return ", ".join(sorted(tables)) if tables else UNSET


def _layout(config: InstallConfig, context: Context) -> str:
    if context.manual:
        return f"manual, {len(context.layout.slices)} partitions"
    return context.choice.layout.value


def _partitions(config: InstallConfig, context: Context) -> str:
    if not context.manual:
        return "from the layout above"
    return ", ".join(entry.describe().split("  ")[1] for entry in context.layout.slices) or "none"


def _encryption(config: InstallConfig, context: Context) -> str:
    return "on" if context.choice.passphrase_file else "off"


def _root(config: InstallConfig, context: Context) -> str:
    return "set" if config.system.root_password_hash else UNSET


def _user(config: InstallConfig, context: Context) -> str:
    return ", ".join(user.name for user in config.system.users) or "none"


def _graphics(config: InstallConfig, context: Context) -> str:
    return config.packages.graphics or context.translate("none")


def _display_manager(config: InstallConfig, context: Context) -> str:
    return config.packages.display_manager or context.translate("none")


def _applications(config: InstallConfig, context: Context) -> str:
    return ", ".join(config.packages.applications) or "none"


def _root_login(config: InstallConfig, context: Context) -> str:
    return context.translate("allowed" if config.system.sshd_root_login else "refused")


def _sshd(config: InstallConfig, context: Context) -> str:
    if not config.system.sshd:
        return context.translate("no server")
    return context.translate(
        "password login" if config.system.sshd_password_login else "keys only"
    )


def _license(config: InstallConfig, context: Context) -> str:
    return " ".join(config.portage.accept_license)


def _makeopts(config: InstallConfig, context: Context) -> str:
    return config.portage.makeopts or f"-j{context.cores} (this machine)"


def _cflags(config: InstallConfig, context: Context) -> str:
    from ..model.config import PortageConfig

    flags = config.portage.common_flags
    return f"{flags} (stage3 default)" if flags == PortageConfig().common_flags else flags


def _extra(config: InstallConfig, context: Context) -> str:
    return " ".join(config.packages.extra) or "none"


def _erase(config: InstallConfig, context: Context) -> str:
    """Answered already when the layout writes no partition table.

    A reuse layout produces only `Existing(wipe=False)` nodes, so demanding the
    disk name for an erase that will not happen blocked an install that erases
    nothing.
    """
    if not any(node.wipe for node in config.disk.graph.of_type(Existing)):
        return context.translate("nothing is erased")
    return "confirmed" if context.erase_confirmed else UNSET


#: The disk, as one subject. Six rows in a menu of thirty read as six unrelated
#: decisions; behind one row they read as the layout they describe.
DISK: Final[tuple[Setting, ...]] = (
    Setting("disk", "Drive", _drive, screens.disk_screen, required=True),
    Setting("table", "Partition table", _table, screens.table_screen),
    Setting("layout", "Layout", _layout, screens.layout_screen),
    Setting("partitions", "Partitions", _partitions, screens.partitions_screen),
    Setting("encryption", "Encryption", _encryption, screens.encryption_screen),
    Setting("swap", "Swap", _swap, screens.swap_screen),
)

#: How the target builds. Read together, so shown together.
COMPILER: Final[tuple[Setting, ...]] = (
    Setting("makeopts", "Compile jobs", _makeopts, screens.makeopts_screen),
    Setting("cflags", "Compiler flags", _cflags, screens.compile_flags_screen),
    # Read from /proc/cpuinfo, so it is right without being asked; shown
    # because it decides which binary packages match.
    Setting("cpu_flags", "CPU flags", _cpu_flags, None),
    Setting("license", "Licenses", _license, screens.license_screen),
    Setting("keywords", "Package keywords", _keywords, screens.keywords_screen),
)

#: The kernel and the version of it, which is read from a repository rather
#: than held in a table: the list moves every week.
KERNEL: Final[tuple[Setting, ...]] = (
    Setting("source", "Package", _kernel, screens.kernel_screen),
    Setting("version", "Version", _kernel_version, screens.kernel_version_screen),
)

#: Who reaches the machine over the network once it boots.
SSH: Final[tuple[Setting, ...]] = (
    Setting("sshd", "SSH server", _sshd, screens.sshd_screen),
    Setting("rootlogin", "Root login over SSH", _root_login, screens.root_login_screen),
    Setting("keys", "SSH public keys", _keys, screens.authorized_keys_screen),
    Setting("unlock", "Remote unlock", _remote_unlock, screens.remote_unlock_screen),
)

#: The menu, flat and in the order it is drawn. One row per decision: nesting
#: hides a choice behind a heading nobody opens, and `archinstall` reaches the
#: same conclusion.
SETTINGS: Final[tuple[Setting, ...]] = (
    Setting("firmware", "Firmware", _firmware, None),
    Setting("keymap", "Keyboard layout", lambda c, x: c.system.keymap, screens.keymap_screen),
    Setting(
        "console_font",
        "Console font",
        lambda c, x: c.system.console_font.value,
        screens.console_font_screen,
    ),
    Setting("keymap_initramfs", "Keyboard at unlock", _unlock_keymap, screens.initramfs_keymap_screen),
    Setting("locale", "System language", lambda c, x: c.system.locale, screens.locale_screen),
    Setting("timezone", "Timezone", lambda c, x: c.system.timezone, screens.timezone_screen),
    Setting("mirror", "Mirrors", _mirror, screens.mirror_screen, required=True),
    Setting("storage", "Disk", _summary(DISK), nested("Disk", DISK), required=True, rows=DISK),
    Setting("hostname", "Hostname", lambda c, x: c.system.hostname, screens.system_screen),
    Setting("init", "Init system", lambda c, x: c.system.init.value, screens.init_screen),
    Setting("logger", "System logger", _logger, screens.logger_screen),
    Setting("cron", "Cron", _cron, screens.cron_screen),
    Setting("profile", "Profile", lambda c, x: c.portage.profile, screens._profile_screen),
    Setting("compiler", "Compiler", _summary(COMPILER), nested("Compiler", COMPILER), rows=COMPILER),
    Setting("root", "Root password", _root, screens.root_password_screen, required=True),
    Setting("user", "User account", _user, screens.user_screen),
    Setting("kernel", "Kernel", _summary(KERNEL), nested("Kernel", KERNEL), rows=KERNEL),
    Setting("bootloader", "Bootloader", _bootloader, screens.bootloader_screen),
    Setting("desktop", "Desktop", lambda c, x: c.packages.desktop or "none", screens.desktop_screen),
    Setting("graphics", "Graphics", _graphics, screens.graphics_screen),
    Setting("dm", "Display manager", _display_manager, screens.display_manager_screen),
    Setting("packages", "Applications", _applications, screens.packages_screen),
    Setting("extra", "Extra packages", _extra, screens.extra_packages_screen),
    Setting("network", "Network configuration", _network, screens.networking_screen),
    Setting("address", "Address", _address, screens.address_screen),
    Setting("ssh", "SSH", _summary(SSH), nested("SSH", SSH), rows=SSH),
    Setting("erase", "Confirm erasing the drive", _erase, screens.erase_screen, required=True),
)


def unanswered(config: InstallConfig, context: Context) -> tuple[str, ...]:
    """Required rows still showing nothing, which is what blocks the install.

    A grouped row is named by whichever row behind it is missing: `Disk` says
    nothing about which of its six the operator has not reached.
    """
    walked = [one for group in SETTINGS for one in (group.rows or (group,))]
    return tuple(
        setting.label
        for setting in walked
        if setting.required and setting.value(config, context) == UNSET
    )
