# SPDX-License-Identifier: GPL-2.0-or-later
"""The disk screens: what a layout is made of, and how it is edited by hand.

Fifty-one definitions reachable from `partitions_screen` and from nothing
else. `screens.py` reaches four of them — the screen itself, `_from_layout`,
`_zfs_bootloader` and `_edit_passphrase` — and this module reaches nothing in
`screens.py`, which is what makes the boundary one way. The closure was
computed rather than guessed: an earlier attempt cut by subject matter and
found edges running both ways.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Final, Sequence

from ..i18n import CUT, Catalog, clip, width
from ..model import manual
from ..model.config import (
    Bootloader,
    DiskConfig,
    Firmware,
    InstallConfig,
)
from ..model.device import (
    Filesystem,
    FilesystemType,
    Partition,
    PartitionRole,
    RaidLevel,
    RaidMetadata,
    TableType,
    ZfsTopology,
)
from ..model.size import SectorSize, Size
from ..model.templates import Choice, Layout, build
from ..model.validate import validate
from ..errors import GentooInstallError, InvalidSize, ValidationFailed
from .context import (
    Context,
    GENTOO_ZH,
    ValueKind,
    answers,
    DONE,
    DROP,
    FieldDescriptor,
    PASSPHRASE_MINIMUM,
    TABLE,
    current_menu,
    footer,
    forget_derived,
    mark_derived,
    say,
    was_derived,
    with_gentoo_zh,
    without_gentoo_zh,
)
from .widgets import (
    Answer,
    Confirm,
    Item,
    Menu,
    Outcome,
    Screen,
    TextField,
    TextFieldRejected,
)

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
            if not was_derived(context, ValueKind.OVERLAY, GENTOO_ZH):
                # An overlay the operator selected on the Mirrors screen is
                # theirs, and this choice does not reach across and take it.
                return replace(config, bootloader=replace(config.bootloader, kind=kind))
            forget_derived(context, ValueKind.OVERLAY)
            return replace(
                config,
                bootloader=replace(config.bootloader, kind=kind),
                portage=without_gentoo_zh(config),
            )
        if not any(one.name == GENTOO_ZH for one in config.portage.overlays):
            mark_derived(context, ValueKind.OVERLAY, GENTOO_ZH)
        return replace(
            config,
            bootloader=replace(config.bootloader, kind=kind),
            portage=with_gentoo_zh(config),
        )

    # BACK whichever key was pressed. Nothing under this module is the main
    # menu, and CANCELLED is what `app.py` answers with the quit prompt.
    return answer.map(apply) if answer.chosen else Answer(Outcome.BACK)


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
        return Answer(Outcome.BACK)
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
            return Answer(Outcome.BACK)
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
            return Answer(Outcome.BACK)
        if again.unwrap() != typed:
            say(screen, context, translate("The two do not match."))
            continue
        return Answer(Outcome.CHOSE, context.stage_passphrase(typed))


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
            footer=_status_line(
                _layout_problem(context, config), footer(translate), screen.size()[1]
            ),
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
            # `esc` is Back everywhere but the main menu, and this is not it.
            # Answering CANCELLED made the main loop ask whether to end the
            # install, one screen after the same key had meant one step back.
            return Answer(Outcome.BACK)
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
                    # Back to the editor whichever key was pressed: the table
                    # the operator drew is still there, and a ZFS root with
                    # GRUB is what committing this would have written.
                    continue
                built = picked.unwrap()
            return Answer(Outcome.CHOSE, built)
        _act_on(screen, context, row)


#: Cells between the keys and the validator's message on the status line.
_GAP: Final[int] = 2


def _status_line(problem: str, keys: str, columns: int) -> str:
    """The keys first, the validator's message in whatever room is left.

    `spread` cuts the footer from the right, so a message written ahead of the
    keys took them off the row exactly when the table was wrong and Back was
    the key the operator needed.
    """
    if not problem:
        return keys
    room = columns - width(keys) - _GAP
    if room <= width(CUT):
        return keys
    return f"{keys}{' ' * _GAP}{clip(problem, room)}"


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
            Item(label=translate("Partition table"), value=TABLE, detail=disk.table.value),
            *(
                [Item(label=translate("Take this disk off the table"), value=DROP)]
                if position > 0
                else []
            ),
            Item(label=translate("Done"), value=DONE),
        ]
        answer = Menu(
            title=context.shown_as(disk.selector), items=items, footer=footer(translate)
        ).run(screen)
        if not answer.chosen or answer.unwrap() == DONE:
            return
        if answer.unwrap() == DROP:
            context.layout.disks.pop(position)
            return
        picked = current_menu(
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
        if not answer.chosen or answer.unwrap() == DONE:
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
        picked = current_menu(
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
        chosen = current_menu(
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
        kind = current_menu(
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
) -> FieldDescriptor[tuple[manual.Array, int]]:
    def row(value: tuple[manual.Array, int], translate: Catalog) -> Item[str]:
        shown = translate(label)
        if key == _NAME:
            shown = translate("Name")
        return Item(label=shown, value=key, detail=detail(value[0], translate))

    def edit(screen: Screen, context: Context, value: tuple[manual.Array, int]) -> tuple[manual.Array, int]:
        _edit_array_field_legacy(screen, context, key, value[1])
        return value

    return FieldDescriptor(
        key,
        row,
        edit,
    )


_ARRAY_FIELDS: tuple[FieldDescriptor[tuple[manual.Array, int]], ...] = (
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
    FieldDescriptor(
        DONE,
        lambda _, translate: Item(label=translate("Done"), value=DONE),
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
    answer = current_menu(
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


def _too_big_for_the_disk(context: Context) -> str:
    """Sizes that come to more than the disk can hand out.

    Checked while they are typed. Left to the check that runs before the disk
    is written, an operator was told nothing for twenty rows and then refused
    at the confirmation: `/efi 1GiB + / 35GiB + swap 4GiB` on a 40 GiB disk
    is over by the trailing GPT copy and the first alignment boundary.
    """
    translate = context.translate
    for disk in context.layout.disks:
        total = context.contents(disk.selector)[1]
        fresh = [one for one in disk.slices if one.status is manual.SliceStatus.CREATE]
        claimed = sum(one.size.bytes for one in fresh if one.size is not None)
        if not total or not claimed:
            continue
        try:
            capacity = Size.parse(total)
        except GentooInstallError:
            continue
        usable = capacity.usable_for_partitions(
            disk.table is TableType.GPT, SectorSize(512)
        )
        if claimed > usable.bytes:
            return translate(
                "{disk} can hand out {usable} and these sizes come to {claimed}"
            ).format(disk=disk.selector, usable=usable, claimed=Size(claimed))
    return ""


def _layout_problem(context: Context, config: InstallConfig) -> str:
    """What the validator says about the table as it stands."""
    over = _too_big_for_the_disk(context)
    if over:
        return over
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
    opened_with = current or manual.Slice(
        index=disk.next_index(),
        role=PartitionRole.DATA,
        size=None,
        filesystem=FilesystemType.EXT4,
        mountpoint="",
    )
    entry = opened_with
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
            if current is None and entry == opened_with:
                # Nothing was filled in, so there is nothing to keep and the
                # row the operator opened by mistake is not added.
                return None
            return _kept_on_back(screen, context, entry, opened_with)
        field = answer.unwrap()
        if field == DONE:
            return entry
        if field == _DELETE:
            return None
        changed = _edit_field(screen, context, entry, purpose, field)
        if changed is not None:
            entry = changed


@dataclass(frozen=True)
class _SliceRule:
    """One field the table cannot hold a value for, and what to say about it."""

    #: The row of `_slice_field_items` the message names.
    field: str
    refuses: Callable[[manual.Slice], bool]
    revert: Callable[[manual.Slice, manual.Slice], manual.Slice]
    said: Callable[[Catalog], str]


def _mountpoint_refused(entry: manual.Slice) -> bool:
    # The two conditions `validate` reads off the built graph, where the
    # message names a node rather than the field the operator typed into.
    return bool(entry.mountpoint) and (
        not entry.mountpoint.startswith("/") or ".." in entry.mountpoint.split("/")
    )


def _no_size_said(translate: Catalog) -> str:
    # A literal inside `translate(...)` rather than a field of the rule: the
    # catalog check reads these out of the source, and a string it cannot see
    # ships as English in the middle of a translated screen.
    return (
        f"{translate('Size')}: "
        f"{translate('a partition of no size leaves nothing to format')}"
    )


def _mountpoint_said(translate: Catalog) -> str:
    return (
        f"{translate('Mount point')}: "
        f"{translate('a mount point has to be an absolute path inside the target')}"
    )


_SLICE_RULES: Final[tuple[_SliceRule, ...]] = (
    _SliceRule(
        _SIZE,
        lambda entry: entry.size is not None and entry.size.bytes <= 0,
        lambda entry, opened: replace(entry, size=opened.size),
        _no_size_said,
    ),
    _SliceRule(
        _MOUNTPOINT,
        _mountpoint_refused,
        lambda entry, opened: replace(entry, mountpoint=opened.mountpoint),
        _mountpoint_said,
    ),
)


def _kept_on_back(
    screen: Screen, context: Context, entry: manual.Slice, opened_with: manual.Slice
) -> manual.Slice:
    """Back writes the edited fields back; a field the table cannot hold goes
    back to what it held, named with the reason it was refused."""
    kept = entry
    for rule in _SLICE_RULES:
        if not rule.refuses(kept):
            continue
        kept = rule.revert(kept, opened_with)
        say(screen, context, rule.said(context.translate))
    return kept


def _slice_fields(
    entry: manual.Slice, purpose: manual.Purpose, translate: Catalog
) -> list[Item[str]]:
    """Every row the editor shows, which is the one table that lists them.

    Filtered through `_SLICE_FIELDS` before, and that table holds the fields
    an operator edits rather than the rows a menu draws: `Done` has no field
    to edit, so it was dropped, and the editor's only way out became the
    cancel that answers with the slice it opened on. Every size, filesystem,
    mount point and label typed into it was thrown away.
    """
    return _slice_field_items(entry, purpose, translate)


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
                    disabled_because=_encryption_refused(purpose, translate),
                )
            ]
            # Neither member kind: the pool and the array each carry their
            # own LUKS, and `manual.build` drops a member's passphrase for
            # that reason. A RAID member still offered the row, so a table
            # marked `luks` produced an unencrypted array.
            if purpose.role not in (PartitionRole.ZFS, PartitionRole.RAID)
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
        Item(label=translate("Done"), value=DONE),
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
        chosen_status = current_menu(
            screen,
            context,
            translate("What happens to it"),
            [
                Item(
                    label=translate(one.value),
                    value=one,
                    detail=translate(manual.STATUS_REASONS[one]),
                    disabled_because=translate(
                        manual.kept_row_refused(replace(entry, status=one))
                    ),
                )
                for one in offered
            ],
            entry.status,
        )
        if not chosen_status.chosen:
            return None
        return replace(entry, status=chosen_status.unwrap())
    if field == _SIZE:
        def parse_size(text: str) -> Answer[Size | None] | TextFieldRejected:
            literal = text.strip()
            if not literal:
                return Answer(Outcome.CHOSE, None)
            try:
                return Answer(Outcome.CHOSE, Size.parse(literal))
            except InvalidSize:
                return TextFieldRejected(
                    translate("{value} is not a valid size").format(value=text), text
                )

        typed = TextField(
            title=translate("Size"),
            value="" if entry.size is None else str(entry.size),
            placeholder=translate("512MiB, 20GiB, or empty for the remaining space"),
            footer=footer(translate),
        ).run_validated(screen, parse_size)
        if not typed.chosen:
            return None
        return replace(entry, size=typed.unwrap())
    if field == _PURPOSE:
        picked = current_menu(
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
        answered = current_menu(
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


def _slice_descriptor(key: str) -> FieldDescriptor[tuple[manual.Slice, manual.Purpose]]:
    def row(value: tuple[manual.Slice, manual.Purpose], translate: Catalog) -> Item[str]:
        return next(item for item in _slice_field_items(*value, translate) if item.value == key)

    def edit(
        screen: Screen, context: Context, value: tuple[manual.Slice, manual.Purpose]
    ) -> tuple[manual.Slice, manual.Purpose] | None:
        changed = _edit_field_legacy(screen, context, value[0], value[1], key)
        return (changed, value[1]) if changed is not None else None

    return FieldDescriptor(key, row, edit)


_SLICE_FIELDS: tuple[FieldDescriptor[tuple[manual.Slice, manual.Purpose]], ...] = tuple(
    _slice_descriptor(key)
    for key in (_SIZE, _PURPOSE, _FILESYSTEM, _MOUNTPOINT, _LABEL, _ENCRYPTION, _STATUS, _DELETE)
)


def _encryption_refused(purpose: manual.Purpose, translate: Catalog) -> str:
    """Why this purpose takes no encryption row, or empty when it takes one.

    A `Purpose` carries no encryption of its own, so this is the only place
    that decides it. `_apply_purpose` used to clear the passphrase on the way
    into `zfs` instead, which took an encrypted root away from an operator who
    passed through that purpose and came back.
    """
    if purpose.role is PartitionRole.ESP:
        # Firmware reads the esp itself and cannot open a container, so an
        # encrypted esp never boots.
        return translate("firmware cannot open a container to read the esp")
    if purpose.role is PartitionRole.ZFS:
        # `manual.build` reads the pool's own passphrase and puts no LUKS under
        # a vdev, so a value here would be shown and then ignored.
        return translate("the pool's own encryption covers its members")
    return ""


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
    return _edit_passphrase(
        screen,
        context,
        entry.passphrase_file,
        translate("Encrypt this partition?"),
    ).map(lambda staged_path: replace(entry, passphrase_file=staged_path)).value
