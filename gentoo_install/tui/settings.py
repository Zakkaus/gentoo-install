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

from ..model.config import (
    Bootloader,
    InitSystem,
    InstallConfig,
    KernelSource,
    Keywords,
    Networking,
)
from ..plan.kernel import CJK_KERNELS, KERNEL_PACKAGES
from ..model import mirrors
from ..model.device import Existing, Luks, MdRaid, PartitionTable, VolumeGroup, ZfsPool
from ..plan import automatic as automatic_values
from . import screens
from .screens import Context, Step, footer
from ..i18n import width
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
    #: What the Install row says when this one is missing. `still needs an
    #: answer` is wrong for a confirmation: there is no field to fill, and the
    #: operator is being asked to agree to something.
    missing: str = "still needs an answer"
    #: Why this row cannot be opened right now, or empty when it can. A row
    #: whose answer the rest of the configuration has already settled is drawn
    #: with the reason rather than opening a screen that changes nothing.
    unavailable: Callable[[InstallConfig, Context], str] = lambda config, context: ""


def settled(setting: Setting, config: InstallConfig, context: Context) -> bool:
    """Whether a required row counts as answered.

    Opened, not merely non-empty: the mirror and the disk both start on a value
    read from this machine, and an install that erases a drive nobody looked at
    is the failure the requirement exists to prevent.
    """
    if setting.value(config, context) == UNSET:
        return False
    return setting.key in context.visited or not setting.required


def style_of(setting: Setting, config: InstallConfig, context: Context) -> Style:
    """Red for a required row with no answer, yellow for an optional row the
    operator has not opened. Colour repeats what the value already says: a
    console without it loses nothing."""
    if setting.required and not settled(setting, config, context):
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
                    detail=shown_value(row, current, context),
                    disabled_because=row.unavailable(current, context)
                    or ("" if row.edit else context.translate("detected")),
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
            if editor is None or rows[chosen].unavailable(current, context):
                continue
            context.visited.add(rows[chosen].key)
            edited = editor(screen, current, context)
            if edited.outcome is Outcome.CANCELLED:
                return Answer(Outcome.CANCELLED, current)
            if edited.chosen:
                current = edited.unwrap()

    return open


#: Room the label, the indent and the `+N` need, so the summary is measured
#: against what is left of the line rather than against the whole width.
_MARGIN: Final[int] = 34

#: Values that say a row holds no answer. A summary of seven rows reads as a
#: string of words with no subject when five of them are one of these, so the
#: group names what is set and says so when nothing is.
QUIET: Final[tuple[str, ...]] = (
    "none", "off", "not used", "nothing is erased", "nothing is unlocked at boot", "default",
    UNSET,
)


def shown_value(setting: Setting, config: InstallConfig, context: Context) -> str:
    """A row's value as the operator reads it.

    `UNSET` is the sentinel `style_of` compares against, so it is translated
    here rather than by each value function. A required row says `required`
    instead: both are drawn red, and `not set` reads as a state that can be
    left alone.
    """
    value = setting.value(config, context)
    if value != UNSET:
        return value
    return context.translate("required" if setting.required else UNSET)


def _summary(rows: tuple[Setting, ...]) -> Callable[[InstallConfig, Context], str]:
    """What a grouped row shows: as many of the values behind it as fit, and
    how many more. Measured against the terminal, because a wide one has room
    for all six and truncating them there says less than it costs."""

    def shown(config: InstallConfig, context: Context) -> str:
        quiet = {context.translate(one) for one in QUIET}
        values = [shown_value(row, config, context) for row in rows]
        said = [one for one in values if one not in quiet]
        if not said:
            return context.translate("nothing set")
        room = max(20, context.columns - _MARGIN)
        taken: list[str] = []
        for value in said:
            rest = len(said) - len(taken) - 1
            if taken and width(", ".join([*taken, value])) + (4 if rest else 0) > room:
                break
            taken.append(value)
        left = len(said) - len(taken)
        joined = ", ".join(taken)
        return f"{joined} +{left}" if left else joined

    return shown


def _swap(config: InstallConfig, context: Context) -> str:
    from ..model.device import Swap

    return "a partition" if config.disk.graph.of_type(Swap) else context.translate("none")


def _zram(config: InstallConfig, context: Context) -> str:
    size = config.system.zram
    return size.single_letter() if size is not None else context.translate("off")


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


def _build_in_ram(config: InstallConfig, context: Context) -> str:
    size = config.portage.build_in_ram
    return f"tmpfs {size.single_letter()}" if size is not None else context.translate("off")


def _first_boot(config: InstallConfig, context: Context) -> str:
    wanted = config.system.first_boot
    if not wanted.wanted:
        return context.translate("none")
    parts = []
    if wanted.url:
        parts.append(wanted.url)
    if wanted.commands:
        parts.append(f"{len(wanted.commands)} {context.translate('commands')}")
    return ", ".join(parts)


def _cmdline(config: InstallConfig, context: Context) -> str:
    """What was typed, and how many parameters the layout adds to it. The count
    is the point: it says the line is longer than this row without listing a
    UUID nobody can read at a glance."""
    added = len(automatic_values.kernel_parameters(config))
    typed = " ".join(config.bootloader.kernel_params)
    if not typed:
        return f"{added} {context.translate('from the layout')}"
    return f"{typed} +{added}"


def _use(config: InstallConfig, context: Context) -> str:
    added = len(automatic_values.use_flags(config, context.groups))
    typed = " ".join(config.portage.use)
    if not typed:
        return f"{added} {context.translate('from the groups you chose')}" if added else UNSET
    return f"{typed} +{added}" if added else typed


def _network(config: InstallConfig, context: Context) -> str:
    return config.system.networking.value


def _unlock_keymap(config: InstallConfig, context: Context) -> str:
    """Nothing is typed at unlock unless something is unlocked.

    The prompt comes from the initramfs, so a layout with no container and no
    remote unlock never shows one and the row was reporting a keyboard for a
    prompt that will not appear.
    """
    graph = config.disk.graph
    if not graph.of_type(Luks) and not config.kernel.remote_unlock.enabled:
        return context.translate("nothing is unlocked at boot")
    chosen = config.system.keymap_initramfs
    return chosen if chosen else f"{config.system.keymap} (same as the console)"


def _address(config: InstallConfig, context: Context) -> str:
    """What the machine will come up with, not what was typed here.

    Only the init's own manager reads these fields, so a static address under
    NetworkManager was drawn as though it were in effect and nothing wrote it.
    """
    system = config.system
    if system.networking is Networking.NONE:
        return context.translate("no networking")
    if system.networking is not Networking.BUILTIN:
        return str(system.networking.value)
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
    return f"{count} authorised" if count else context.translate("none")


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
    """The value alone. The row cannot be edited and already draws the reason
    beside it, so a `(detected)` suffix said the same word twice."""
    return str(config.bootloader.firmware.value)


def _drive(config: InstallConfig, context: Context) -> str:
    disks = [node.selector.rsplit("/", 1)[-1] for node in config.disk.graph.of_type(Existing)]
    return ", ".join(disks) if disks else UNSET


def _table(config: InstallConfig, context: Context) -> str:
    """What the graph holds, or what a reuse layout does instead.

    `build_reused` writes no partition table at all, so the row read `not set`
    for ever and every answer given to it looked like it had not taken.
    """
    if not context.layout.writes_the_table() and context.layout.slices:
        return context.translate("the one already on the disk")
    tables = {node.table.value for node in config.disk.graph.of_type(PartitionTable)}
    return ", ".join(sorted(tables)) if tables else UNSET


def _reuse_writes_no_table(config: InstallConfig, context: Context) -> str:
    """A table nobody edits is never written, so its type is whatever the disk
    already carries."""
    if context.layout.slices and not context.layout.writes_the_table():
        return context.translate("a reused layout writes no table")
    return ""


def _layout(config: InstallConfig, context: Context) -> str:
    if context.manual:
        return f"manual, {len(context.layout.slices)} partitions"
    return context.choice.layout.value


def _template_writes_the_table(config: InstallConfig, context: Context) -> str:
    """A whole-disk template has no table to hand-write.

    Opening this row over one switched the layout to manual without saying so,
    and the editor then listed the disk's contents as about to be erased, which
    is not what the operator had chosen a template for.
    """
    if context.manual:
        return ""
    return context.translate("the layout row writes this table")


def _partitions(config: InstallConfig, context: Context) -> str:
    if not context.manual:
        return context.translate("default")
    return ", ".join(
        entry.describe().split("  ")[1] for entry in context.layout.slices
    ) or context.translate("none")


def _encryption(config: InstallConfig, context: Context) -> str:
    """Read from the graph and not from `context.choice`: a hand-written table
    is built from `context.layout`, so the choice said `on` over a layout that
    carried no container and the machine came up unencrypted."""
    graph = config.disk.graph
    if graph.of_type(Luks) or any(pool.passphrase_file for pool in graph.of_type(ZfsPool)):
        return context.translate("on")
    return context.translate("off")


def _root(config: InstallConfig, context: Context) -> str:
    """`set`, `locked`, or the sentinel that blocks the install.

    Locked is an answer, not a missing one: an empty hash locks the account,
    which is what a system with a sudo user wants. Without this the row asked
    for ever, because the screen cannot produce a hash for no password.
    """
    if config.system.root_password_hash:
        return context.translate("set")
    if any(user.sudo for user in config.system.users):
        return context.translate("locked, a sudo user logs in")
    return UNSET


def _user(config: InstallConfig, context: Context) -> str:
    return ", ".join(user.name for user in config.system.users) or context.translate("none")


def _graphics(config: InstallConfig, context: Context) -> str:
    """The driver, then what it makes `VIDEO_CARDS`. The second half is the
    part that decides which Mesa drivers get built, and it is not derivable
    from the first: `radeon` alone would drop Sea Islands."""
    chosen = " ".join(config.packages.graphics) or context.translate("none")
    cards = [one.value for one in automatic_values.video_cards(config, context.groups)]
    return f"{chosen} ({' '.join(cards)})" if cards else chosen


def _video_cards(config: InstallConfig, context: Context) -> str:
    every = [
        *config.portage.video_cards,
        *(one.value for one in automatic_values.video_cards(config, context.groups)),
    ]
    return " ".join(every) or context.translate("none")


def _input_devices(config: InstallConfig, context: Context) -> str:
    """`default` rather than `libinput` while it is untouched, so the group
    above does not summarise itself as `Desktop environment  libinput` when
    nothing in it has been chosen."""
    from ..model.config import PortageConfig

    if config.portage.input_devices == PortageConfig().input_devices:
        return context.translate("default")
    return " ".join(config.portage.input_devices) or context.translate("none")


def _display_manager(config: InstallConfig, context: Context) -> str:
    return config.packages.display_manager or context.translate("none")


def _applications(config: InstallConfig, context: Context) -> str:
    return ", ".join(config.packages.applications) or context.translate("none")


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
    return " ".join(config.packages.extra) or context.translate("none")


def _erase(config: InstallConfig, context: Context) -> str:
    """Answered already when the layout writes no partition table.

    A reuse layout produces only `Existing(wipe=False)` nodes, so demanding the
    disk name for an erase that will not happen blocked an install that erases
    nothing.
    """
    if not any(node.wipe for node in config.disk.graph.of_type(Existing)):
        return context.translate("nothing is erased")
    return context.translate("confirmed") if context.erase_confirmed else UNSET


def _cjk_kernel_only(config: InstallConfig, context: Context) -> str:
    """A font with CJK glyphs draws nothing without the patch that lets the
    console show them, so the size is a choice only under that kernel."""
    if config.kernel.source not in CJK_KERNELS:
        return context.translate("only the cjk kernel draws CJK on the console")
    return ""


#: The disk, as one subject. Six rows in a menu of thirty read as six unrelated
#: decisions; behind one row they read as the layout they describe.
DISK: Final[tuple[Setting, ...]] = (
    Setting("disk", "Drive", _drive, screens.disk_screen, required=True),
    Setting(
        "table", "Partition table", _table, screens.table_screen,
        unavailable=_reuse_writes_no_table,
    ),
    Setting("layout", "Layout", _layout, screens.layout_screen),
    Setting(
        "partitions", "Partitions", _partitions, screens.partitions_screen,
        unavailable=_template_writes_the_table,
    ),
    Setting("encryption", "Encryption", _encryption, screens.encryption_screen),
    Setting(
        "keymap_initramfs", "Keyboard at unlock", _unlock_keymap, screens.initramfs_keymap_screen
    ),
    Setting("swap", "Swap", _swap, screens.swap_screen),
    Setting("zram", "zram", _zram, screens.zram_screen),
)

#: How the target builds. Read together, so shown together.
COMPILER: Final[tuple[Setting, ...]] = (
    Setting("makeopts", "Compile jobs", _makeopts, screens.makeopts_screen),
    Setting("cflags", "Compiler flags", _cflags, screens.compile_flags_screen),
    # Read from /proc/cpuinfo, so it is right without being asked; shown
    # because it decides which binary packages match.
    Setting("cpu_flags", "CPU flags", _cpu_flags, screens.cpu_flags_screen),
    Setting("license", "Licenses", _license, screens.license_screen),
    Setting("keywords", "Package keywords", _keywords, screens.keywords_screen),
    Setting("use", "USE flags", _use, screens.use_flags_screen),
    Setting("ram", "Build in RAM", _build_in_ram, screens.build_in_ram_screen),
)

#: The bootloader and what it puts on the command line. One row was enough
#: while the parameters were derived from the disk alone.
BOOT: Final[tuple[Setting, ...]] = (
    Setting("kind", "Bootloader", _bootloader, screens.bootloader_screen),
    Setting("cmdline", "Kernel command line", _cmdline, screens.kernel_cmdline_screen),
)

#: The kernel and the version of it, which is read from a repository rather
#: than held in a table: the list moves every week.
KERNEL: Final[tuple[Setting, ...]] = (
    Setting("source", "Package", _kernel, screens.kernel_screen),
    Setting(
        "console_font",
        "Console font",
        lambda c, x: c.system.console_font.value,
        screens.console_font_screen,
        unavailable=_cjk_kernel_only,
    ),
    Setting("version", "Version", _kernel_version, screens.kernel_version_screen),
)

#: Who reaches the machine over the network once it boots.
SSH: Final[tuple[Setting, ...]] = (
    Setting("sshd", "SSH server", _sshd, screens.sshd_screen),
    Setting("rootlogin", "Root login over SSH", _root_login, screens.root_login_screen),
    Setting("keys", "SSH public keys", _keys, screens.authorized_keys_screen),
    Setting("unlock", "Remote unlock", _remote_unlock, screens.remote_unlock_screen),
)

def _journald_only(config: InstallConfig, context: Context) -> str:
    """systemd logs to journald and merges no other logger, so the row has
    nothing to offer until the init is openrc."""
    if config.system.init is InitSystem.SYSTEMD:
        return context.translate("systemd logs to journald")
    return ""


#: The init system and what it brings with it. The logger is openrc's question
#: alone; cron is `sys-process/cronie` on both, so it stays a choice on both.
INIT: Final[tuple[Setting, ...]] = (
    Setting("init", "Init system", lambda c, x: c.system.init.value, screens.init_screen),
    Setting("logger", "System logger", _logger, screens.logger_screen, unavailable=_journald_only),
    Setting("cron", "Cron", _cron, screens.cron_screen),
)


#: The desktop, as one subject. Which session, which driver and which login
#: screen are one decision made three times, not three unrelated rows.
DESKTOP: Final[tuple[Setting, ...]] = (
    Setting(
        "desktop", "Desktop", lambda c, x: c.packages.desktop or x.translate("none"),
        screens.desktop_screen,
    ),
    Setting("graphics", "Graphics", _graphics, screens.graphics_screen),
    Setting("cards", "VIDEO_CARDS", _video_cards, screens.video_cards_screen),
    Setting("input", "INPUT_DEVICES", _input_devices, screens.input_devices_screen),
    Setting("dm", "Display manager", _display_manager, screens.display_manager_screen),
)

#: How the machine comes up on the network. The address is only read by some of
#: the managers, so the two rows have to be read together.
NETWORK: Final[tuple[Setting, ...]] = (
    Setting("network", "Network configuration", _network, screens.networking_screen),
    Setting("address", "Address", _address, screens.address_screen),
)

#: The menu, flat and in the order it is drawn. One row per decision.
SETTINGS: Final[tuple[Setting, ...]] = (
    Setting("firmware", "Firmware", _firmware, None),
    Setting("keymap", "Keyboard layout", lambda c, x: c.system.keymap, screens.keymap_screen),
    Setting("locale", "System language", lambda c, x: c.system.locale, screens.locale_screen),
    Setting("timezone", "Timezone", lambda c, x: c.system.timezone, screens.timezone_screen),
    Setting("mirror", "Mirrors", _mirror, screens.mirror_screen, required=True),
    Setting("storage", "Disk", _summary(DISK), nested("Disk", DISK), required=True, rows=DISK),
    Setting("hostname", "Hostname", lambda c, x: c.system.hostname, screens.system_screen),
    Setting("firstboot", "Run once at first boot", _first_boot, screens.first_boot_screen),
    Setting("system", "Init system", _summary(INIT), nested("Init system", INIT), rows=INIT),
    Setting("profile", "Profile", lambda c, x: c.portage.profile, screens._profile_screen),
    Setting(
        "compiler",
        "Compiler",
        _summary(COMPILER),
        nested("Compiler", COMPILER),
        required=True,
        rows=COMPILER,
    ),
    Setting("root", "Root password", _root, screens.root_password_screen, required=True),
    Setting("user", "User account", _user, screens.user_screen),
    Setting("kernel", "Kernel", _summary(KERNEL), nested("Kernel", KERNEL), rows=KERNEL),
    Setting("bootloader", "Bootloader", _summary(BOOT), nested("Bootloader", BOOT), rows=BOOT),
    Setting("environment", "Desktop environment", _summary(DESKTOP), nested("Desktop environment", DESKTOP), rows=DESKTOP),
    Setting("packages", "Applications", _applications, screens.packages_screen),
    Setting("extra", "Extra packages", _extra, screens.extra_packages_screen),
    Setting("networking", "Network", _summary(NETWORK), nested("Network", NETWORK), rows=NETWORK),
    Setting("ssh", "SSH", _summary(SSH), nested("SSH", SSH), rows=SSH),
    Setting(
        "erase",
        "Confirm erasing the drive",
        _erase,
        screens.erase_screen,
        required=True,
        missing="not confirmed",
    ),
)


def unanswered(config: InstallConfig, context: Context) -> tuple[Setting, ...]:
    """Required rows still showing nothing, which is what blocks the install.

    A grouped row is named by whichever row behind it is missing: `Disk` says
    nothing about which of its six the operator has not reached. The group
    itself is walked too, because a group can be required without any one row
    behind it being: `Compiler` has a usable value for every row and still has
    to be looked at.
    """
    named: list[Setting] = []
    for group in SETTINGS:
        behind = [
            row for row in group.rows if row.required and not settled(row, config, context)
        ]
        if any(row.required for row in group.rows):
            # The rows carry the requirement, so the group is not named as
            # well: `Disk, Drive` reads as two missing answers and is one.
            named += behind
        elif group.required and not settled(group, config, context):
            named.append(group)
    return tuple(named)
