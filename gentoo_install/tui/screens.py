# SPDX-License-Identifier: GPL-2.0-or-later
"""One screen per decision, each a function of the configuration so far.

A screen never mutates what it was given: it returns a new `InstallConfig`, and
`app.py` re-validates before moving on. Every option a compatibility rule
excludes is drawn greyed with that rule's own sentence, so the interface and the
validator never disagree about why something cannot be chosen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from itertools import takewhile
from typing import Callable, Final, Sequence

from ..i18n import Catalog
from ..exec import fetch
from ..model import compat
from ..model.config import (
    BASE_PROFILE,
    SystemConfig,
    FirstBoot,
    ConsoleFontSize,
    Bootloader,
    BootloaderConfig,
    DiskConfig,
    DiskMode,
    ImageFormat,
    Firewall,
    InitSystem,
    MirrorRegion,
    InstallConfig,
    KernelConfig,
    KernelSource,
    Keywords,
    Logger,
    Networking,
    PackagesConfig,
    PortageConfig,
    ProxyConfig,
    ProxyKind,
    User,
)
from ..model.device import (
    DeviceGraph,
    DeviceId,
    Partition,
    Swap,
    FilesystemType,
    PartitionRole,
    TableType,
    ZfsPool,
)
from ..plan import automatic as automatic_values
from ..plan.convert import REPLACED_DIRECTORIES, SWAP_CONFIRMATION
from ..plan.fonts import CjkFontconfigLocale
from ..model.compat import KERNEL_PACKAGES
from ..model.size import Size
from ..errors import DeviceNotFound, GentooInstallError
from ..model import atoms, manual, paste, refusals, sshkey
from ..model.templates import Layout, build
from ..plan.packages import Catalog as Groups
from ..plan.packages import FONT_CONFIGURATION_DISABLED, FONT_CONFIGURATION_ENABLED
from ..plan.packages import INPUT_CONFIGURATION_DISABLED, INPUT_CONFIGURATION_ENABLED
from ..plan import system as plan_system
from .packages import (
    _profile_for,
    _record_operator,
    _set_font_configuration,
    _typed_beside_automatic,
    font_configuration_group,
    input_configuration_group,
)
from .partitions import (
    _edit_passphrase,
    _from_layout,
    _zfs_bootloader,
    partitions_screen,
)
from .context import (
    Context,
    ValueKind,
    answers,
    current_menu,
    footer,
    say,
    with_gentoo_zh,
)
from .widgets import (
    Accepts,
    band,
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
    (DiskMode.PARTITION, "partition a disk"),
    (DiskMode.IN_PLACE, "replace the running system"),
    (DiskMode.DD, "write a prepared image"),
)


def _because(refused: refusals.Refusal, translate: Catalog) -> str:
    """The reason in the operator's language, and what on this machine caused
    it after it. The detail is a device path or a command name, which is the
    same word in every language and is not in any catalog."""
    if not refused.detail:
        return translate(refused.reason)
    return f"{translate(refused.reason)} ({refused.detail})"


def install_mode_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Install onto disks, convert the running system, or write an image."""
    translate = context.translate
    refused = context.conversion_refused
    image_write_refused = context.image_write_refused
    preamble = [
        translate("This choice decides whether a new system is built or this one is replaced.")
    ]
    if context.running_system:
        preamble.append(translate("Running system: ") + context.running_system)
    if refused:
        preamble.append(translate("Conversion is not offered: ") + _because(refused, translate))
    if image_write_refused:
        preamble.append(
            translate("Image writing is not offered: ") + _because(image_write_refused, translate)
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
        kept = _answers_this_mode_discards(config)
        if kept and not _agrees_to_discard(screen, context, kept):
            return Answer(Outcome.CHOSE, config)
        # Cleared with the disk keys: this mode writes the image as it is
        # and configures nothing in it, and `validate` refuses a configuration
        # that describes a machine it will not produce. Left standing they
        # would block an install from a menu that no longer shows those rows.
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
                system=SystemConfig(),
                packages=PackagesConfig(),
                portage=PortageConfig(),
                kernel=KernelConfig(),
                bootloader=BootloaderConfig(),
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


def _answers_this_mode_discards(config: InstallConfig) -> int:
    """How many of the operator's answers writing an image would throw away.

    Counted rather than guessed at: the row is the first in the list and one
    keypress on it emptied twenty of them, silently. An agent that pressed it
    went back and forth three times looking for what it had lost.
    """
    return sum(
        1
        for part, blank in (
            ("system", SystemConfig()),
            ("packages", PackagesConfig()),
            ("portage", PortageConfig()),
            ("kernel", KernelConfig()),
            ("bootloader", BootloaderConfig()),
        )
        if getattr(config, part) != blank
    )


def _agrees_to_discard(screen: Screen, context: Context, kept: int) -> bool:
    """Whether the operator wants those answers gone."""
    translate = context.translate
    asked = Confirm(
        **answers(translate),
        title=translate("Writing an image discards the rest of the answers."),
        detail=translate("{count} groups of answers are discarded.").format(count=kept),
        footer=footer(translate),
    ).run(screen)
    return bool(asked.chosen and asked.unwrap())


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
        # The word itself, the way the erase screen shows the disk name: an
        # operator reading `Type the word to confirm.` was left with an empty
        # field and no word anywhere on the screen.
        detail=(
            f"{translate('Replaced: ')}{replaced}. "
            f"{translate('Kept: /home, /root and every other mount.')} "
            f"{translate('Type {word} to confirm.').format(word=SWAP_CONFIRMATION)}"
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
                detail=translate("each partition, and a second disk for a pool or array"),
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
    items: list[Item[tuple[Layout, FilesystemType]]] = [
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
    menu: Menu[tuple[Layout, FilesystemType]] = Menu(
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






def erase_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """The one screen with no default: each name has to be typed.

    Every device `compat.destroyed` names, not `context.choice.disk`: a second
    disk added in manual partitioning was destroyed under a confirmation that
    named the first one.
    """
    translate = context.translate
    original = set(context.confirmed)
    pending = set(original)
    for selector in compat.destroyed_selectors(config):
        if selector in pending:
            continue
        answered = _confirm_one(screen, context, selector, pending)
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
        placeholder=translate("letters, digits and hyphens"),
        footer=footer(translate),
    )
    answer = field.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, hostname=answer.unwrap() or "gentoo")),
    )


class _ProxyField(Enum):
    HOST = "host"
    PORT = "port"
    USERNAME = "username"
    PASSWORD = "password"
    BYPASS = "bypass"


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
    proxy_fields = [
        (
            _ProxyField.HOST,
            Field(
                label=translate("Proxy host"),
                value=current.host,
                accepts=Accepts.NO_SPACE,
                placeholder="proxy.example.com",
            ),
        ),
        (
            _ProxyField.PORT,
            Field(
                label=translate("Proxy port"),
                value=str(current.port) if current.port else "",
                accepts=Accepts.DIGITS,
                placeholder="3128",
            ),
        ),
        (
            _ProxyField.USERNAME,
            Field(
                label=translate("Proxy username"),
                value=current.username,
                accepts=Accepts.NO_SPACE,
                placeholder=translate("empty when the proxy needs no login"),
            ),
        ),
        (_ProxyField.PASSWORD, Field(label=translate("Proxy password"), value=current.password, secret=True)),
        (
            _ProxyField.BYPASS,
            Field(
                label=translate("Bypass hosts"),
                value=", ".join(current.bypass),
                accepts=Accepts.NO_SPACE,
                placeholder=translate("comma-separated host names"),
            ),
        ),
    ]
    positions = {name: index for index, (name, _) in enumerate(proxy_fields)}

    def validated(values: list[str]) -> Answer[InstallConfig] | FormRejected:
        host = values[positions[_ProxyField.HOST]].strip()
        port = values[positions[_ProxyField.PORT]].strip()
        username = values[positions[_ProxyField.USERNAME]].strip()
        password = values[positions[_ProxyField.PASSWORD]].strip()
        hosts = values[positions[_ProxyField.BYPASS]].strip()
        if not host and (port or username or password or hosts):
            return FormRejected(translate("Proxy host is required when proxy fields are set"), {0: host})
        if port and (not port.isdecimal() or not 1 <= int(port) <= 65535):
            return FormRejected(translate("Proxy port must be between 1 and 65535"), {1: port})
        bypass = tuple(one for one in (item.strip() for item in hosts.split(",")) if one)
        if any(any(char.isspace() for char in item) for item in bypass):
            return FormRejected(
                translate("Bypass hosts must not contain spaces"),
                {positions[_ProxyField.BYPASS]: hosts},
            )
        selected = replace(config, proxy=ProxyConfig(
            kind=selected_kind, host=host, port=int(port or "0"), username=username,
            password=password, bypass=bypass,
        ))
        fetch.configure_proxy(selected.proxy)
        return Answer(Outcome.CHOSE, selected)

    return Form(
        title=translate("Proxy"),
        fields=[field for _, field in proxy_fields],
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


def _compatibility_violations(config: InstallConfig) -> tuple[compat.Rule, ...]:
    """The rules whose traits are knowable before an in-place layout is read."""
    if config.disk.layout_is_read_from_the_machine:
        return compat.violations_without_a_graph(config)
    return compat.violations(config)


def _common_violations(candidates: Sequence[InstallConfig]) -> set[compat.Rule]:
    """Rules every candidate breaks, which no choice on this screen can fix.

    Reporting them on each row disables the whole screen and points the
    operator at a setting that is not on it.
    """
    broken = [set(_compatibility_violations(one)) for one in candidates]
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
        broken = [one for one in _compatibility_violations(candidate) if one not in shared]
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
    (
        KernelSource.XANMOD,
        "built here with XanMod patches, cjktty for CJK on the console, from gentoo-zh",
    ),
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
    package = config.kernel.package or KERNEL_PACKAGES[config.kernel.source].atom
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
            Item(label=KERNEL_PACKAGES[source].atom, value=source, detail=translate(reason))
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
        return Answer(Outcome.CHOSE, replace(changed, portage=with_gentoo_zh(changed)))
    if config.system.console_cjk:
        # Turned off with the kernel that carried it, and said out loud: the
        # rule would otherwise refuse the install with a message about the
        # kernel, from a row that has no way to clear this.
        say(screen, context, translate("This kernel has no cjktty: console CJK is off."))
        changed = replace(changed, system=replace(changed.system, console_cjk=False))
    return Answer(Outcome.CHOSE, changed)







































































def configuration_groups(groups: Groups) -> tuple[str, ...]:
    return (
        font_configuration_group(groups, FONT_CONFIGURATION_ENABLED),
        font_configuration_group(groups, FONT_CONFIGURATION_DISABLED),
        input_configuration_group(groups, INPUT_CONFIGURATION_ENABLED),
        input_configuration_group(groups, INPUT_CONFIGURATION_DISABLED),
    )



















#: What profiles.desc lists for amd64 beside the base, as suffixes of it. A
#: systemd profile is the same path plus /systemd, which `_profile_for`
#: relies on.
PROFILE_VARIANTS: Final[tuple[str, ...]] = (
    "",
    "systemd",
    "desktop",
    "desktop/systemd",
    "desktop/plasma",
    "desktop/plasma/systemd",
    "desktop/gnome",
    "desktop/gnome/systemd",
    "no-multilib",
    "no-multilib/systemd",
)

#: Built from `BASE_PROFILE`, not written out: the release was spelled ten
#: times here and once there, so moving it was eleven edits and the ten that
#: are missed are silent.
PROFILES: tuple[str, ...] = tuple(
    f"{BASE_PROFILE}/{one}" if one else BASE_PROFILE for one in PROFILE_VARIANTS
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


def _zones_under(area: str, zones: Sequence[str]) -> list[str]:
    """The zones of one area, each named once, in the order the machine gave."""
    seen: dict[str, None] = {}
    for zone in zones:
        if zone.split("/", 1)[0] == area:
            seen[zone] = None
    return list(seen)


def _under(zone: str) -> str:
    """What to label a zone inside its area's list."""
    return zone.split("/", 1)[1] if "/" in zone else zone


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
    while True:
        within = _zones_under(area, zones)
        # An area that is one whole zone has no city to pick: `UTC` is the
        # only row under `UTC`. Checked on every pass, not once before the
        # loop: an operator who opened `Asia`, went back and chose `UTC` ended
        # the run on `list index out of range` and was left at a shell.
        if within == [area]:
            return Answer(
                Outcome.CHOSE, replace(config, system=replace(config.system, timezone=area))
            )
        city: Menu[str] = Menu(
            title=area,
            items=[Item(label=_under(zone), value=zone) for zone in within],
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
            if not area:
                return Answer(
                    Outcome.CHOSE,
                    replace(config, system=replace(config.system, timezone=context.timezone_here)),
                )
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
    """Only the profiles that match the chosen init and stage3 this installer fetches."""
    choices = context.profile_paths or PROFILES
    wanted = [
        profile
        for profile in choices
        if ("systemd" in profile.split("/")) is (config.system.init is InitSystem.SYSTEMD)
        and not compat.unserved_profile_problems(profile)
    ]
    # Without the part every row shares. The whole path is 44 cells and the
    # pane is narrower than that, so a row wrapped onto a second line and the
    # list stopped reading as a list; the prefix is named once above instead.
    shared = f"{BASE_PROFILE}/"
    menu: Menu[str] = Menu(
        title=context.translate("Portage"),
        preamble=(shared,),
        items=[
            Item(label=profile.removeprefix(shared), value=profile)
            for profile in wanted
        ],
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
    current = (
        context.layout.disks[0].table
        if context.manual and context.layout.disks
        else context.choice.table
    )
    menu: Menu[TableType] = Menu(
        title=translate("Partition table"),
        preamble=(translate("The table format controls how firmware and the kernel identify partitions."),),
        items=items,
        current=current,
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
    answer = current_menu(
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
    chosen = current_menu(
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


def console_cjk_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Flipped where it stands: the row reads `in use` or `not used` already."""
    return Answer(
        Outcome.CHOSE,
        replace(config, system=replace(config.system, console_cjk=not config.system.console_cjk)),
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

    Locale, timezone, mirror region, fonts, and input methods follow the
    language. Every one of these stays a row the operator can change.
    """

    locale: str
    timezone: str
    mirror_region: MirrorRegion
    #: True for the languages the cjktty patch is the point of. It pulls in
    #: gentoo-zh, so it is not a default for a language that would not use the
    #: rest of that overlay.
    cjk_console: bool = False
    font_groups: tuple[str, ...] = ()
    input_method_groups: tuple[str, ...] = ()


#: One row per interface language. Keyed by the same tags as the catalogs.
LANGUAGE_DEFAULTS: Final[dict[str, LanguageDefaults]] = {
    "en": LanguageDefaults("en_US.UTF-8", "UTC", MirrorRegion.GLOBAL),
    "zh-CN": LanguageDefaults(
        "zh_CN.UTF-8",
        "Asia/Shanghai",
        MirrorRegion.CN,
        cjk_console=True,
        font_groups=("noto-cjk",),
        input_method_groups=("fcitx5", "rime"),
    ),
    "zh-TW": LanguageDefaults(
        "zh_TW.UTF-8",
        "Asia/Taipei",
        MirrorRegion.GLOBAL,
        cjk_console=True,
        font_groups=("noto-cjk",),
        input_method_groups=("fcitx5", "rime"),
    ),
    # cjktty is what puts Chinese, Japanese and Korean on the console, so all
    # four of those catalogs take the patched kernel and not only the two
    # Chinese ones.
    "ja": LanguageDefaults("ja_JP.UTF-8", "Asia/Tokyo", MirrorRegion.GLOBAL, True),
    "ko": LanguageDefaults("ko_KR.UTF-8", "Asia/Seoul", MirrorRegion.GLOBAL, True),
}


def with_language(config: InstallConfig, tag: str) -> InstallConfig:
    """The configuration as the chosen interface language leaves it."""
    chosen = LANGUAGE_DEFAULTS.get(tag)
    if chosen is None:
        return config
    locales = config.system.locales
    if chosen.locale not in locales:
        locales = (*locales, chosen.locale)
    language_groups = (*chosen.font_groups, *chosen.input_method_groups)
    applications = (
        *config.packages.applications,
        *(group for group in language_groups if group not in config.packages.applications),
    )
    seeded = replace(
        config,
        system=replace(
            config.system,
            locale=chosen.locale,
            timezone=chosen.timezone,
            locales=locales,
            console_cjk=chosen.cjk_console,
        ),
        packages=replace(config.packages, applications=applications),
        portage=replace(
            config.portage,
            mirrors=replace(config.portage.mirrors, region=chosen.mirror_region),
        ),
    )
    if not chosen.cjk_console:
        return seeded
    # The patched kernel is what puts CJK on the console, and it is in gentoo-zh
    # and nowhere else, so the overlay comes with it or the row is unusable.
    return replace(
        seeded,
        kernel=replace(seeded.kernel, source=KernelSource.CJK_BIN),
        portage=with_gentoo_zh(seeded),
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
            detail=(
                context.translate("This language needs a cjktty kernel or a CJK console font.")
                if cjk
                else ""
            ),
        )
        for tag, name, cjk in INTERFACE_LANGUAGES
    ]
    start = next(
        (index for index, (tag, _, _) in enumerate(INTERFACE_LANGUAGES) if tag == context.tag), 0
    )
    menu: Menu[str] = Menu(
        # This screen precedes the language choice, so each script identifies it.
        title="Language / \u8a9e\u8a00 / \uc5b8\uc5b4",
        items=items,
        cursor=start,
        footer=context.translate("[enter] select"),
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
            placeholder="-O2 -pipe -march=native",
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
    address_fields: list[tuple[compat.NetworkField, Field]] = [
        (
            compat.NetworkField.SYSTEM_INTERFACE,
            Field(
                label=translate("Interface"),
                value=system.interface,
                placeholder=translate("enp1s0, or empty for the first wired one"),
            ),
        ),
        (
            compat.NetworkField.SYSTEM_IPV4,
            Field(
                label=translate("IPv4"),
                value=next((one for one in system.addresses if ":" not in one), ""),
                placeholder="192.0.2.10/24",
            ),
        ),
        (
            compat.NetworkField.SYSTEM_IPV4_GATEWAY,
            Field(
                label=translate("IPv4 gateway"),
                value=next((one for one in system.gateways if ":" not in one), ""),
                placeholder="192.0.2.1",
            ),
        ),
        (
            compat.NetworkField.SYSTEM_IPV6,
            Field(
                label=translate("IPv6"),
                value=next((one for one in system.addresses if ":" in one), ""),
                placeholder="2001:db8::2/64",
            ),
        ),
        (
            compat.NetworkField.SYSTEM_IPV6_GATEWAY,
            Field(
                label=translate("IPv6 gateway"),
                value=next((one for one in system.gateways if ":" in one), ""),
                placeholder="fe80::1",
            ),
        ),
        (
            compat.NetworkField.SYSTEM_DNS,
            Field(
                label=translate("DNS"),
                value=" ".join(system.dns),
                placeholder=translate("separated by spaces"),
            ),
        ),
    ]
    positions = {name: index for index, (name, _) in enumerate(address_fields)}

    def validated(values: list[str]) -> Answer[InstallConfig] | FormRejected:
        interface = values[positions[compat.NetworkField.SYSTEM_INTERFACE]].strip()
        four = values[positions[compat.NetworkField.SYSTEM_IPV4]].strip()
        four_gateway = values[positions[compat.NetworkField.SYSTEM_IPV4_GATEWAY]].strip()
        six = values[positions[compat.NetworkField.SYSTEM_IPV6]].strip()
        six_gateway = values[positions[compat.NetworkField.SYSTEM_IPV6_GATEWAY]].strip()
        resolvers = values[positions[compat.NetworkField.SYSTEM_DNS]].strip()
        selected_system = replace(
            system,
            interface=interface,
            addresses=tuple(one for one in (four, six) if one),
            gateways=tuple(one for one in (four_gateway, six_gateway) if one),
            dns=tuple(resolvers.split()),
        )
        problems = compat.system_network_problems(selected_system)
        if problems:
            problem = problems[0]
            return FormRejected(
                problem.describe(translate),
                {positions[problem.field]: values[positions[problem.field]]},
            )
        return Answer(Outcome.CHOSE, replace(config, system=selected_system))

    return Form(
        title=translate("Static address"),
        fields=[field for _, field in address_fields],
        footer=footer(translate),
        done=translate("Done"),
    ).run_validated(screen, validated)


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
    remote_unlock_fields: list[tuple[compat.NetworkField, Field]] = [
        (
            compat.NetworkField.REMOTE_UNLOCK_PORT,
            Field(label=translate("Port"), value=str(unlock.port), placeholder="222"),
        ),
        (
            compat.NetworkField.REMOTE_UNLOCK_ADDRESS,
            Field(
                label=translate("Address"),
                value=unlock.address,
                placeholder=translate("192.0.2.10/24, or empty for DHCP"),
            ),
        ),
        (
            compat.NetworkField.REMOTE_UNLOCK_GATEWAY,
            Field(
                label=translate("Gateway"),
                value=unlock.gateway,
                placeholder=translate("192.0.2.1, needed to answer off this subnet"),
            ),
        ),
        (
            compat.NetworkField.SYSTEM_INTERFACE,
            Field(
                label=translate("Interface"),
                value=unlock.interface,
                placeholder=translate("eth0, or empty for whichever comes up"),
            ),
        ),
    ]
    positions = {name: index for index, (name, _) in enumerate(remote_unlock_fields)}

    def validated(values: list[str]) -> Answer[InstallConfig] | FormRejected:
        port = values[positions[compat.NetworkField.REMOTE_UNLOCK_PORT]].strip()
        address = values[positions[compat.NetworkField.REMOTE_UNLOCK_ADDRESS]].strip()
        gateway = values[positions[compat.NetworkField.REMOTE_UNLOCK_GATEWAY]].strip()
        interface = values[positions[compat.NetworkField.SYSTEM_INTERFACE]].strip()
        problems = compat.remote_unlock_problems(
            enabled=True,
            port=port,
            address=address,
            gateway=gateway,
        )
        if problems:
            problem = problems[0]
            return FormRejected(
                problem.describe(translate),
                {positions[problem.field]: values[positions[problem.field]]},
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
        fields=[field for _, field in remote_unlock_fields],
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
