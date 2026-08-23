# SPDX-License-Identifier: GPL-2.0-or-later
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
    BASE_PROFILE,
    Bootloader,
    DiskMode,
    Firewall,
    InitSystem,
    InstallConfig,
    Keywords,
    Networking,
)
from ..model.compat import CJK_KERNELS, KERNEL_PACKAGES
from ..model import compat, mirrors
from ..model.device import (
    DeviceGraph,
    DeviceId,
    Existing,
    Luks,
    Mountpoint,
    Partition,
    PartitionTable,
    ZfsPool,
)
from ..plan import automatic as automatic_values
from . import screens
from .mirror import mirror_screen
from .packages import (
    cjk_font_groups,
    cjk_fonts_screen,
    desktop_screen,
    display_manager_screen,
    extra_packages_screen,
    graphics_screen,
    input_method_groups,
    input_method_screen,
    packages_screen,
    preferred_font_groups,
    use_flags_screen,
    video_cards_screen,
)
from .partitions import partitions_screen
from .context import Context, Step, ValueKind, ValueSource, footer
from ..i18n import width
from .widgets import Answer, Item, Menu, Outcome, Screen, Style, fit

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
    #: One sentence saying what this row decides, drawn in the right pane
    #: above the values. A bare `/dev/sda` does not say what it is for, and
    #: the operator should not have to open a screen to find out.
    describes: str = ""
    #: The heading this row sits under, so twenty-four rows read as an order
    #: to work through rather than one list.
    section: str = ""
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
    #: Whether this row starts on a value read from this machine. Such a row
    #: has to be opened before it counts as answered: an install that erases a
    #: drive nobody looked at is what the requirement exists to prevent. A row
    #: with no detected default needs no visit — its value can only have come
    #: from the operator, and counting it as missing made the Install row say
    #: `root password: still needs an answer` beside a row reading `set`.
    detected: bool = False
    #: Why this row cannot be opened right now, or empty when it can. A row
    #: whose answer the rest of the configuration has already settled is drawn
    #: with the reason rather than opening a screen that changes nothing.
    unavailable: Callable[[InstallConfig, Context], str] = lambda config, context: ""


def settled(setting: Setting, config: InstallConfig, context: Context) -> bool:
    """Whether a required row counts as answered.

    A row with a detected default has to be opened as well as filled: the
    mirror and the disk both start on a value read from this machine, and an
    install that erases a drive nobody looked at is the failure the
    requirement exists to prevent.

    Every other row counts as answered when it has a value. The root password
    has no detected default, so requiring a visit made the Install row say it
    still needed an answer beside a row that read `set`.
    """
    if setting.value(config, context) == UNSET:
        return False
    if not setting.required or not setting.detected:
        return True
    return setting.key in context.visited


def style_of(setting: Setting, config: InstallConfig, context: Context) -> Style:
    """Red for a required row with no answer, yellow for an optional row the
    operator has not opened. Colour repeats what the value already says: a
    console without it loses nothing."""
    # A row the configuration refuses is not an answer anybody owes: a
    # conversion takes its layout from the running machine, and `Drive` was
    # drawn red and read `required` beside a screen that would not open.
    if setting.unavailable(config, context):
        return Style.PLAIN
    if setting.required and not settled(setting, config, context):
        return Style.REQUIRED
    if not setting.required and setting.edit is not None and setting.key not in context.visited:
        return Style.UNTOUCHED
    return Style.PLAIN


def nested(
    title: str,
    rows: tuple[Setting, ...],
    preamble: Callable[[InstallConfig, Context], tuple[str, ...]] = (
        lambda config, context: ()
    ),
) -> Step:
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
                preamble=preamble(current, context),
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
            chosen = answer.unwrap()
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


#: Values that say a row holds no answer. A summary of seven rows reads as a
#: string of words with no subject when five of them are one of these, so the
#: group names what is set and says so when nothing is.
QUIET: Final[tuple[str, ...]] = (
    "none", "off", "not used", "nothing is erased", "nothing is unlocked at boot", "default",
    UNSET,
)


def shown_value(
    setting: Setting,
    config: InstallConfig,
    context: Context,
    room: int | None = None,
    *,
    within_refused: bool = False,
) -> str:
    """A row's value as the operator reads it.

    `UNSET` is the sentinel `style_of` compares against, so it is translated
    here rather than by each value function. A required row says `required`
    instead: both are drawn red, and `not set` reads as a state that can be
    left alone.
    """
    value = setting.value(config, context)
    if value == UNSET:
        # The group's refusal counts as this row's: a row behind a screen that
        # will not open cannot be answered either, and the right pane went on
        # reading `required` beside a row the operator could not reach.
        refused = within_refused or bool(setting.unavailable(config, context))
        value = context.translate(
            "required" if setting.required and not refused else UNSET
        )
    if room is None:
        return value
    # `required` is fitted too: the widest label in the catalog leaves its own
    # row no room at all, and a word wider than the row is dropped whole.
    return fit(value.split(", "), room)


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
        # Whole: `shown_value` fits it to the room its own row has, which the
        # terminal's width is not.
        return ", ".join(said)

    return shown


def _swap(config: InstallConfig, context: Context) -> str:
    from ..model.device import Swap

    if config.disk.graph.of_type(Swap):
        return context.translate("a partition")
    return context.translate("none")


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
    # Nothing when no version is pinned: the row already names the package,
    # and `not pinned` is a word about the absence of an answer rather than an
    # answer, which `QUIET` then has to filter back out of every summary.
    return config.kernel.version


def _keywords(config: InstallConfig, context: Context) -> str:
    return "~amd64" if config.portage.keywords is Keywords.TESTING else "amd64"


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
        parts.append(context.translate("Commands: {count}").format(count=len(wanted.commands)))
    return ", ".join(parts)


def _cmdline(config: InstallConfig, context: Context) -> str:
    """What was typed, and how many parameters the layout adds to it. The count
    is the point: it says the line is longer than this row without listing a
    UUID nobody can read at a glance."""
    added = len(automatic_values.kernel_parameters(config))
    typed = " ".join(config.bootloader.kernel_params)
    if not typed:
        return context.translate("{count} kernel parameters added").format(count=added)
    return f"{typed} +{added}"


def _use(config: InstallConfig, context: Context) -> str:
    added = len(automatic_values.use_flags(config, context.groups))
    typed = " ".join(config.portage.use)
    if not typed:
        return f"{added} {context.translate('from the groups you chose')}" if added else UNSET
    return f"{typed} +{added}" if added else typed


def _network(config: InstallConfig, context: Context) -> str:
    # The NetworkManager values name a package and stay as they are. `builtin`
    # describes what the init already has, and untranslated it read as an
    # implementation value in a Chinese interface.
    if config.system.networking is Networking.BUILTIN:
        # The key is the English row, so it stays the word Portage and the
        # init systems use; the catalogs carry the description.
        return context.translate("builtin")
    named: str = config.system.networking.value
    return named


def _firewall(config: InstallConfig, context: Context) -> str:
    name: str = config.system.firewall.value
    return context.translate("none") if config.system.firewall is Firewall.NONE else name


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
    if chosen:
        return chosen
    same = context.translate("same as the console")
    return f"{config.system.keymap} ({same})"


def _address(config: InstallConfig, context: Context) -> str:
    """What the machine will come up with.

    Every manager, not only the built-in one: `WriteNetworkConfig` writes a
    mode-0600 NetworkManager keyfile from the same fields, so answering with
    the manager's name hid an address the install was about to configure. The
    row beside this one carries the manager, so this one never repeats it.
    """
    system = config.system
    if system.networking is Networking.NONE:
        return context.translate("no networking")
    if not system.addresses:
        return "DHCP"
    where = system.interface or "auto"
    said = f"{where}: {', '.join(system.addresses)}"
    if system.gateways:
        said += f"  {context.translate('via')} {', '.join(system.gateways)}"
    if system.dns:
        said += f"  DNS {', '.join(system.dns)}"
    return said


def _remote_unlock(config: InstallConfig, context: Context) -> str:
    unlock = config.kernel.remote_unlock
    if not unlock.enabled:
        return context.translate("off")
    return f"{unlock.port}, {unlock.address or 'DHCP'}"


def _keys(config: InstallConfig, context: Context) -> str:
    count = len(config.system.authorized_keys)
    if not count:
        return context.translate("none")
    # A template through the catalog, so the number stays a number and the
    # word around it is translated. The whole string was English before, in
    # the middle of a translated menu.
    return context.translate("{count} authorised").format(count=count)


def _mirror(config: InstallConfig, context: Context) -> str:
    """Unset until a site is picked. Every repository is fetched from here, so
    the region a machine happens to default to is not an answer."""
    chosen = config.portage.mirrors
    if not chosen.site:
        return UNSET
    overlays = [overlay.name for overlay in config.portage.overlays]
    measured = f", {context.translate('measured')}" if chosen.speed_test else ""
    return f"{chosen.site}{measured}" + (f", {', '.join(overlays)}" if overlays else "")


def _proxy(config: InstallConfig, context: Context) -> str:
    """Show the proxy endpoint without exposing URL credentials."""
    proxy = config.proxy
    if not proxy.enabled:
        return context.translate("off")
    value = proxy.redacted_url
    if proxy.bypass:
        value += f"  {context.translate('{count} hosts bypass').format(count=len(proxy.bypass))}"
    return value


def _firmware(config: InstallConfig, context: Context) -> str:
    """The value alone. The row cannot be edited and already draws the reason
    beside it, so a `(detected)` suffix said the same word twice."""
    return str(config.bootloader.firmware.value)


def _drive(config: InstallConfig, context: Context) -> str:
    """The kernel name, not the selector: the configuration keeps the stable
    `/dev/disk/by-id/` one and nobody reads sixty characters of it."""
    disks = [context.shown_as(node.selector) for node in config.disk.graph.of_type(Existing)]
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
        return context.translate("manual, {count} partitions").format(
            count=len(context.layout.slices)
        )
    if context.choice.layout.value == "whole-disk":
        layout = context.translate("whole-disk")
    elif context.choice.layout.value == "whole-disk-btrfs":
        layout = context.translate("whole-disk-btrfs")
    elif context.choice.layout.value == "whole-disk-zfs":
        layout = context.translate("whole-disk-zfs")
    else:
        layout = context.translate("reuse")
    if context.choice.layout.value.startswith("whole-disk"):
        return f"{layout} ({context.translate('erases the disk')})"
    return layout


def _template_writes_the_table(config: InstallConfig, context: Context) -> str:
    """A whole-disk template has no table to hand-write.

    Opening this row over one switched the layout to manual without saying so,
    and the editor then listed the disk's contents as about to be erased, which
    is not what the operator had chosen a template for.
    """
    if context.manual:
        return ""
    # Named for what it is rather than for a row: the row it used to point at
    # was renamed when the layout question was split in two.
    return context.translate("written by the template")


def _partitions(config: InstallConfig, context: Context) -> str:
    if not context.manual:
        return _written_table(config, context)
    # The fields, not a slice of `describe()`: that string pads its first
    # column to ten characters, so splitting it on two spaces answered with
    # the padding and the row read as a bare comma.
    return ", ".join(
        f"{one.mountpoint or one.role.value} {one.size or context.translate('the rest')}"
        for one in context.layout.slices
    ) or context.translate("none")


def _written_table(config: InstallConfig, context: Context) -> str:
    """The table the template produces, read off the graph it built.

    The row said `default`, which names nothing: an operator asking what a
    template does to their disk was told that it is the default one. The
    partitions are already in the graph by the time this row is drawn.
    """
    graph = config.disk.graph
    written: list[str] = []
    for partition in sorted(graph.of_type(Partition), key=lambda one: one.index):
        # Every mount point that ends up on this partition, not the one
        # directly above it: a btrfs root reaches it through a subvolume and an
        # encrypted one through a container, and both answered with the
        # filesystem's own name where a path belongs.
        paths = sorted(
            str(one.path)
            for one in graph.of_type(Mountpoint)
            if _rests_on(graph, one.id, partition.id)
        )
        size = str(partition.size) if partition.size else context.translate("the rest")
        written.append(f"{' '.join(paths) or partition.role.value} {size}")
    return ", ".join(written) or context.translate("none")


def _rests_on(graph: DeviceGraph, above: DeviceId, below: DeviceId) -> bool:
    seen: set[DeviceId] = set()
    frontier = [above]
    while frontier:
        current = frontier.pop()
        if current in seen or current not in graph.nodes:
            continue
        seen.add(current)
        if current == below:
            return True
        frontier.extend(graph[current].inputs)
    return False


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
    chosen = config.packages.display_manager
    if chosen and any(
        one.kind is ValueKind.DISPLAY_MANAGER
        and one.value == chosen
        and one.source is ValueSource.DERIVED
        for one in context.provenance
    ):
        return f"{chosen} ({context.translate('proposed')})"
    return chosen or context.translate("none")


def _applications(config: InstallConfig, context: Context) -> str:
    language_groups = frozenset(
        (
            *input_method_groups(context.groups),
            *cjk_font_groups(context.groups),
            *screens.configuration_groups(context.groups),
        )
    )
    applications = [
        name for name in config.packages.applications if name not in language_groups
    ]
    return ", ".join(applications) or context.translate("none")


def _other_locales(config: InstallConfig, context: Context) -> str:
    return ", ".join(
        locale for locale in config.system.locales if locale != config.system.locale
    ) or context.translate("none")


def _input_method(config: InstallConfig, context: Context) -> str:
    selected = [
        context.groups[name].label or name
        for name in config.packages.applications
        if name in input_method_groups(context.groups)
    ]
    return ", ".join(context.translate(name) for name in selected) or context.translate("none")


def _cjk_fonts(config: InstallConfig, context: Context) -> str:
    offered = cjk_font_groups(context.groups)
    selected = [name for name in config.packages.applications if name in offered]
    if not selected:
        return context.translate("none")
    preferred = set(preferred_font_groups(config, context.groups))
    shown = []
    for name in selected:
        label = context.translate(context.groups[name].label or name)
        shown.append(
            context.translate("{font} (preferred)").format(font=label)
            if name in preferred
            else label
        )
    return ", ".join(shown)


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
    if config.portage.makeopts:
        return config.portage.makeopts
    return context.translate("stage3 default")


def _cflags(config: InstallConfig, context: Context) -> str:
    from ..model.config import PortageConfig

    flags = config.portage.common_flags
    if flags != PortageConfig().common_flags:
        return flags
    return context.translate("{flags} (stage3 default)").format(flags=flags)


def _extra(config: InstallConfig, context: Context) -> str:
    return " ".join(config.packages.extra) or context.translate("none")


def _erase(config: InstallConfig, context: Context) -> str:
    """Answered already when the layout destroys nothing the operator had.

    `compat.destroyed` and not `Existing.wipe`: formatting a partition the
    operator kept is `mkfs` over their data with no disk-level wipe and no
    rewritten table, and this row read that layout as harmless.
    """
    targets = set(compat.destroyed_selectors(config))
    if not targets:
        return context.translate("nothing is erased")
    # Every one of them: a layout can destroy content on more than one device,
    # and confirming the first authorised a second the prompt never named.
    return context.translate("confirmed") if targets <= context.confirmed else UNSET


def _cjk_kernel_only(config: InstallConfig, context: Context) -> str:
    """A font with CJK glyphs draws nothing without the patch that lets the
    console show them, so the size is a choice only under that kernel."""
    if config.system.console_cjk and config.kernel.source not in CJK_KERNELS:
        return context.translate("only the cjk kernel draws CJK on the console")
    return ""


def _mode(config: InstallConfig, context: Context) -> str:
    for mode, what in screens.INSTALL_MODES:
        if mode is config.disk.mode:
            return context.translate(what)
    return config.disk.mode.value


def _image_source(config: InstallConfig, context: Context) -> str:
    return config.disk.source or UNSET


def _image_format(config: InstallConfig, context: Context) -> str:
    return config.disk.source_format.value


def _image_destination(config: InstallConfig, context: Context) -> str:
    return config.disk.destination or UNSET


def _the_conversion_writes_no_layout(config: InstallConfig, context: Context) -> str:
    """A conversion has no device graph to edit: it is derived from the machine
    it runs on, and `validate()` refuses one written by hand. The rows stay
    visible and say why rather than disappearing, or an operator who switched
    mode by accident sees a menu that lost a row and no reason."""
    if config.disk.mode is DiskMode.IN_PLACE:
        return context.translate("the conversion takes the layout from the running system")
    return ""


#: The disk, as one subject. Six rows in a menu of thirty read as six unrelated
#: decisions; behind one row they read as the layout they describe.
DISK: Final[tuple[Setting, ...]] = (
    Setting("disk", "Drive", _drive, screens.disk_screen, required=True, detected=True),
    Setting(
        "table", "Partition table", _table, screens.table_screen,
        unavailable=_reuse_writes_no_table,
    ),
    Setting("layout", "Layout", _layout, screens.layout_screen),
    Setting(
        "partitions", "Partitions", _partitions, partitions_screen,
        unavailable=_template_writes_the_table,
    ),
    Setting("encryption", "Encryption", _encryption, screens.encryption_screen),
    Setting(
        "keymap_initramfs", "Keyboard at unlock", _unlock_keymap, screens.initramfs_keymap_screen
    ),
    Setting("swap", "Swap", _swap, screens.swap_screen),
    Setting("zram", "zram", _zram, screens.zram_screen),
)

IMAGE_WRITE: Final[tuple[Setting, ...]] = (
    Setting("image_source", "Image source", _image_source, screens.image_source_screen, required=True),
    Setting("image_format", "Image format", _image_format, screens.image_format_screen),
    Setting(
        "image_destination",
        "Destination disk",
        _image_destination,
        screens.image_destination_screen,
        required=True,
    ),
)

#: How the target builds. Read together, so shown together.
COMPILER: Final[tuple[Setting, ...]] = (
    Setting("makeopts", "Compile jobs", _makeopts, screens.makeopts_screen),
    Setting("cflags", "Compiler flags", _cflags, screens.compile_flags_screen),
    # Read from /proc/cpuinfo, so it is right without being asked; shown
    # because it decides which binary packages match.
    Setting("cpu_flags", "CPU flags", _cpu_flags, screens.cpu_flags_screen),
    Setting("keywords", "Package keywords", _keywords, screens.keywords_screen),
    Setting("use", "USE flags", _use, use_flags_screen),
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


LANGUAGE: Final[tuple[Setting, ...]] = (
    Setting("locale", "System language", lambda c, x: c.system.locale, screens.locale_screen),
    Setting("locales", "Other locales", _other_locales, screens.additional_locales_screen),
    Setting("fonts", "Fonts", _cjk_fonts, cjk_fonts_screen),
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
        desktop_screen,
    ),
    Setting(
        "input_method",
        "Input method",
        _input_method,
        input_method_screen,
    ),
    Setting("graphics", "Graphics", _graphics, graphics_screen),
    Setting("cards", "VIDEO_CARDS", _video_cards, video_cards_screen),
    Setting("input", "INPUT_DEVICES", _input_devices, screens.input_devices_screen),
    Setting("dm", "Display manager", _display_manager, display_manager_screen),
)

#: How the machine comes up on the network. The address is only read by some of
#: the managers, so the two rows have to be read together.
NETWORK: Final[tuple[Setting, ...]] = (
    Setting("network", "Network configuration", _network, screens.networking_screen),
    Setting("address", "Address", _address, screens.address_screen),
    Setting("firewall", "Firewall", _firewall, screens.firewall_screen),
)



def _only_one_mode(config: InstallConfig, context: Context) -> str:
    """A screen offering one choice is not a choice.

    Both other modes are refused on most machines — a running system on a
    filesystem the installer cannot describe, and an image write that would
    erase the installer — and the menu still opened a list with a single row
    in it. The reasons belong beside the row, where they are read without
    pressing enter on nothing.
    """
    if not context.conversion_refused or not context.image_write_refused:
        return ""
    return context.translate("The other modes are not available on this machine.")


INSTALL_MODE: Final[Setting] = Setting(
        'mode',
        'Install mode',
        _mode,
        screens.install_mode_screen,
        required=True,
        detected=True,
        describes="Whether a new system is built on a disk or the running system is replaced.",
        section="Partitioning",
        unavailable=_only_one_mode,
    )


#: The menu, flat and in the order it is drawn. One row per decision.
SETTINGS: Final[tuple[Setting, ...]] = (
    Setting(
        'firmware',
        'Firmware',
        _firmware,
        None,
        describes="How this machine booted, which decides the bootloader the install can use.",
        section="Build",
    ),
    Setting(
        'proxy',
        'Proxy',
        _proxy,
        screens.proxy_screen,
        describes="The proxy every fetch goes through, for a network with no direct connection.",
        section="Sources",
    ),
    Setting(
        'keymap',
        'Keyboard layout',
        lambda c, x: c.system.keymap,
        screens.keymap_screen,
        describes="The console keyboard layout of the installed system.",
        section="System",
    ),
    Setting(
        'language',
        'Language and fonts',
        _summary(LANGUAGE),
        nested('Language and fonts', LANGUAGE),
        rows=LANGUAGE,
        describes="The system locale, the fonts it needs, and the input method for typing CJK.",
        section="System",
    ),
    Setting(
        'timezone',
        'Timezone',
        lambda c, x: c.system.timezone,
        screens.timezone_screen,
        describes="The zone the installed system keeps local time and schedules in.",
        section="System",
    ),
    Setting(
        'mirror',
        'Mirrors',
        _mirror,
        mirror_screen,
        required=True,
        detected=True,
        describes="Where the tree, distfiles, overlays and binary packages are fetched from.",
        section="Sources",
    ),
    Setting(
        "license",
        "Licenses",
        _license,
        screens.license_screen,
        describes=(
            "Which licences Portage may merge: free software only, firmware as well,"
            " or every licence including proprietary software."
        ),
        section="Build",
    ),
    # Before the Disk row, because it decides whether that row applies at all.
    INSTALL_MODE,
    Setting(
        'storage',
        'Disk',
        # The drives alone. A summary of the whole group answered `/dev/sda,
        # gpt, whole-disk (erases the disk), /efi 1GiB, / the rest`, which is
        # the screen behind this row read out on the row itself.
        _drive,
        nested('Disk', DISK),
        required=True,
        rows=DISK,
        detected=True,
        unavailable=_the_conversion_writes_no_layout,
        describes="The disk to install onto, how it is partitioned, and what each partition holds.",
        section="Partitioning",
    ),
    Setting(
        'hostname',
        'Hostname',
        lambda c, x: c.system.hostname,
        screens.system_screen,
        describes="The name the installed system answers to on the network.",
        section="System",
    ),
    Setting(
        'firstboot',
        'Run once at first boot',
        _first_boot,
        screens.first_boot_screen,
        describes="A command the installed system runs once, the first time it starts.",
        section="Software",
    ),
    Setting(
        'system',
        'Init system',
        _summary(INIT),
        nested('Init system', INIT),
        rows=INIT,
        describes="The service manager, the logger beside it, and the profile that matches them.",
        section="System",
    ),
    Setting(
        'profile',
        'Profile',
        # Without the `default/linux/amd64/<release>/` every profile in the
        # list shares. The last component alone is not enough, because
        # `desktop/systemd` and `systemd` both end in the same word; the whole
        # path is too long for the pane, and a value that does not fit is
        # dropped rather than cut, so `no-multilib/systemd` left the row blank.
        lambda c, x: c.portage.profile.removeprefix(f"{BASE_PROFILE}/"),
        screens._profile_screen,
        describes="The Portage profile, which sets the default USE flags and package set.",
        section="System",
    ),
    Setting(
        'compiler',
        'make.conf',
        _summary(COMPILER),
        nested('Compiler', COMPILER),
        required=True,
        rows=COMPILER,
        detected=True,
        describes="How packages are built: parallel jobs, optimisation flags and keyword policy.",
        section="Build",
    ),
    Setting(
        'root',
        'Root password',
        _root,
        screens.root_password_screen,
        required=True,
        describes="The password for the root account of the installed system.",
        section="Accounts",
    ),
    Setting(
        'user',
        'User account',
        _user,
        screens.user_screen,
        describes="An account created beside root, and the groups it belongs to.",
        section="Accounts",
    ),
    Setting(
        'kernel',
        'Kernel',
        _summary(KERNEL),
        nested('Kernel', KERNEL),
        rows=KERNEL,
        describes="Which kernel package is merged, and the console font it is built with.",
        section="Build",
    ),
    Setting(
        'bootloader',
        'Bootloader',
        _summary(BOOT),
        nested('Bootloader', BOOT),
        rows=BOOT,
        describes="What the firmware starts, and the parameters passed to the kernel.",
        section="Build",
    ),
    Setting(
        'environment',
        'Desktop environment',
        _summary(DESKTOP),
        nested('Desktop environment', DESKTOP),
        rows=DESKTOP,
        describes="The desktop, the display manager that starts it, and the graphics drivers.",
        section="Software",
    ),
    Setting(
        'packages',
        'Applications',
        _applications,
        packages_screen,
        describes="Applications merged after the system is installed.",
        section="Software",
    ),
    Setting(
        'extra',
        'Extra packages',
        _extra,
        extra_packages_screen,
        describes="Any further package atoms to merge, given by name.",
        section="Software",
    ),
    Setting(
        'networking',
        'Network',
        _summary(NETWORK),
        nested('Network', NETWORK),
        rows=NETWORK,
        describes="How the installed system configures its network at startup.",
        section="System",
    ),
    Setting(
        'ssh',
        'SSH',
        _summary(SSH),
        nested('SSH', SSH),
        rows=SSH,
        describes="Whether the installed system starts an SSH server, and what it accepts.",
        section="Accounts",
    ),
    Setting(
        'erase',
        'Confirm erasing the drive',
        _erase,
        screens.erase_screen,
        required=True,
        missing='not confirmed',
        describes="Agreement that the selected disk is erased, which cannot be undone.",
        section="Partitioning",
    ),
)

DD_SETTINGS: Final[tuple[Setting, ...]] = (
    INSTALL_MODE,
    Setting(
        "image_write",
        "Write image",
        _summary(IMAGE_WRITE),
        nested("Write image", IMAGE_WRITE),
        required=True,
        rows=IMAGE_WRITE,
        section="Partitioning",
    ),
    # The same row every other destructive path carries. `dd` writes a whole
    # disk and this table did not hold it, so the one mode that always
    # destroys was the one mode the menu never asked about.
    Setting(
        'erase',
        'Confirm erasing the drive',
        _erase,
        screens.erase_screen,
        required=True,
        missing='not confirmed',
        describes="Agreement that the selected disk is erased, which cannot be undone.",
        section="Partitioning",
    ),
)


#: The order the sections are worked through, which is the order an install
#: actually happens in: what it is written to, then what the machine is, then
#: the system, how it is built, who reaches it, and what it runs.
SECTION_ORDER: Final[tuple[str, ...]] = (
    "Partitioning",
    "System",
    "Sources",
    "Build",
    "Accounts",
    "Software",
)


def in_section_order(table: tuple[Setting, ...]) -> tuple[Setting, ...]:
    """Stable within a section, so the order inside one stays as written."""
    return tuple(
        sorted(table, key=lambda one: SECTION_ORDER.index(one.section)
               if one.section in SECTION_ORDER else len(SECTION_ORDER))
    )


def settings_for(config: InstallConfig) -> tuple[Setting, ...]:
    """The settings relevant to the installation mode being configured."""
    return in_section_order(DD_SETTINGS if config.disk.mode is DiskMode.DD else SETTINGS)


def unanswered(config: InstallConfig, context: Context) -> tuple[Setting, ...]:
    """Required rows still showing nothing, which is what blocks the install.

    A grouped row is named by whichever row behind it is missing: `Disk` says
    nothing about which of its six the operator has not reached. The group
    itself is walked too, because a group can be required without any one row
    behind it being: `Compiler` has a usable value for every row and still has
    to be looked at.
    """
    named: list[Setting] = []
    for group in settings_for(config):
        # A row behind a group the configuration refuses has no screen to open
        # and no value to gain: a conversion left `Drive` required with the
        # screen behind it refusing to open, and the install could never start
        # from the interface. The group itself is left alone, because a group
        # that cannot be opened still shows the value it holds.
        refused = bool(group.unavailable(config, context))
        behind = [
            row
            for row in group.rows
            if row.required
            and not refused
            and not row.unavailable(config, context)
            and not settled(row, config, context)
        ]
        if any(row.required for row in group.rows):
            # The rows carry the requirement, so the group is not named as
            # well: `Disk, Drive` reads as two missing answers and is one.
            named += behind
        elif group.required and not settled(group, config, context):
            named.append(group)
    return tuple(named)
