# SPDX-License-Identifier: GPL-2.0-or-later
"""What every screen needs and no screen owns.

`settings.py`, `overview.py` and `app.py` all reach for `Context`, the footer
and the one-line acknowledgement, and each import of them from `screens.py`
made a screen module the place a caller went for a shared type. The import
direction is one way: this module knows nothing about a screen.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Final, Generic, Sequence, TypedDict, TypeVar

from ..errors import ConfigError, GentooInstallError
from ..exec.preflight import ZFS_PASSPHRASE_MINIMUM
from ..i18n import Catalog, clip
from ..model import manual, mirrors, qr, refusals
from ..model.config import (
    BinhostChannel,
    Firmware,
    InstallConfig,
    Overlay,
    PortageConfig,
)
from ..model.device import (
    Existing,
    Filesystem,
    FilesystemType,
    Luks,
    Mountpoint,
    Partition,
    PartitionTable,
    Swap,
    ZfsPool,
)
from ..model.size import ZERO, Size
from ..model.templates import Choice, Layout
from ..plan.packages import Catalog as Groups
from .widgets import Answer, Item, Menu, Screen

#: A screen takes what has been decided and returns it changed.
Step = Callable[[Screen, InstallConfig, "Context"], Answer[InstallConfig]]


class ValueKind(Enum):
    """A configuration value whose source must survive a later choice."""

    USE_FLAG = "use flag"
    VIDEO_CARD = "video card"
    NETWORKING = "networking"
    DISPLAY_MANAGER = "display manager"


class ValueSource(Enum):
    """Whether the operator or a TUI choice supplied a value."""

    OPERATOR = "operator"
    DERIVED = "derived"


@dataclass(frozen=True)
class ValueProvenance:
    """The source of one value in the current TUI session."""

    kind: ValueKind
    value: str
    source: ValueSource


def _cannot_load(name: str) -> InstallConfig:
    raise ConfigError(f"this installer was built with no way to read {name}")


class Context:
    """What the screens need besides the configuration itself."""

    columns: int
    visited: set[str]

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
        memory: Size = ZERO,
        cpu_flags: Sequence[str] = (),
        supports_v3: bool = False,
        inspect_disk: Callable[[str], tuple[tuple[tuple[str, str, str], ...], str]] = (
            lambda disk: ((), "")
        ),
        #: Every spelling of one device, for the erase confirmation. The
        #: default answers with the selector alone: only the machine knows
        #: which kernel name it resolves to today.
        names_for: Callable[[str], tuple[str, ...]] = lambda selector: (selector,),
        fetch_text: Callable[[str], str] = lambda url: "",
        kernel_versions: Callable[[str], tuple[tuple[str, bool], ...]] = lambda atom: (),
        keymaps: Callable[[], tuple[tuple[str, str], ...]] = lambda: (),
        timezone_here: str = "",
        zfs_kernel_max: str = "",
        save_config: Callable[[InstallConfig, str], str] = lambda config, name: "",
        publish_config: Callable[[InstallConfig], str] = lambda config: "",
        #: Configuration files sitting where the installer was started, and how
        #: to read one. An operator who saved their answers and rebooted should
        #: not have to retype them or remember the `--config` flag.
        configs_here: Sequence[str] = (),
        load_config: Callable[[str], InstallConfig] | None = None,
        zfs_unavailable: str = "",
        #: Whether this machine has a routable address of each family. A
        #: mirror with no AAAA record cannot be reached from an IPv6-only
        #: machine, and the menu says so rather than letting the operator find
        #: out when the stage3 does not arrive. Both true by default: a
        #: machine that cannot be read refuses nothing.
        ipv4: bool = True,
        ipv6: bool = True,
        profile_paths: Sequence[str] = (),
        #: What the running system is, in one line, and why it cannot be
        #: converted in place. The description is shown so the operator can see
        #: which machine was read; the refusal is empty when a conversion is
        #: possible. Both come from `exec/probe.py` and `plan/convert.py`,
        #: because this layer reads no machine. A machine that could not be
        #: read refuses the conversion rather than offering one blind.
        running_system: str = "",
        conversion_refused: refusals.Refusal = refusals.Refusal(refusals.SYSTEM_NOT_READ),
        image_write_refused: refusals.Refusal = refusals.Refusal(refusals.MEMORY_NOT_READ),
    ) -> None:
        self.translate = translate
        self.running_system = running_system
        self.conversion_refused = conversion_refused
        self.image_write_refused = image_write_refused
        self.ipv4 = ipv4
        self.ipv6 = ipv6
        self.profile_paths = tuple(profile_paths)
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
        #: Writes the configuration under the given name and returns the path.
        #: Injected because this layer opens no file.
        self.save_config = save_config
        #: Sends the configuration to the pastebin and returns the address.
        #: Injected because this layer opens no connection.
        self.publish_config = publish_config
        self.configs_here = tuple(configs_here)
        #: Defaulted to a refusal rather than to a blank configuration: a
        #: double that silently answers with defaults would let the screen
        #: report a load that never happened.
        self.load_config: Callable[[str], InstallConfig] = load_config or _cannot_load
        #: Why this live system cannot make a pool, or empty when it can. Every
        #: row that would produce one is drawn with this as its reason, because
        #: the medium the installer runs from is often not a Gentoo one.
        self.zfs_unavailable = zfs_unavailable
        #: Every console keymap the machine ships, as (family, name). Empty on
        #: a medium with no keymap tree, which is when the name is typed.
        self.keymaps = keymaps
        #: The highest kernel `sys-fs/zfs` builds a module for, read from its
        #: ebuild. Empty when no repository is visible, which offers every version.
        self.zfs_kernel_max = zfs_kernel_max
        #: The zone the installing system is on, from its `/etc/localtime`. A
        #: live medium sets that from the firmware clock and its own default,
        #: so it is the one guess about where the machine is that costs nothing.
        self.timezone_here = timezone_here
        #: Every zone the machine knows, from `exec/probe.py`.
        self.timezones = tuple(timezones)
        #: How this machine booted. The install defaults to the same, because
        #: installing for the other is almost always a mistake.
        self.firmware = firmware
        #: Kept so a disk screen can rebuild the graph when one answer changes,
        #: rather than editing a graph it did not build.
        self.choice = Choice(disk=disks[0][0] if disks else "", firmware=firmware)
        #: Selectors whose destruction the operator confirmed by typing the
        #: name. A set rather than one flag: a layout can destroy content on
        #: more than one device, and a single flag confirmed for the first
        #: disk authorised a second one the prompt never named.
        self.confirmed: set[str] = set()
        self.provenance: set[ValueProvenance] = set()
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
        self.memory = memory
        self.cpu_flags = tuple(cpu_flags)
        #: Whether `ld.so` says this CPU runs x86-64-v3 binaries.
        self.supports_v3 = supports_v3
        self._inspect = inspect_disk
        #: Every spelling of one device, for the erase confirmation.
        self.names_for = names_for
        self._inspected: dict[str, tuple[tuple[tuple[str, str, str], ...], str]] = {}
        if self.choice.disk:
            self.inspect_disk(self.choice.disk)

    def hydrate_disk(self, config: InstallConfig) -> None:
        """Align the editable disk choice with a loaded configuration graph."""
        disks = config.disk.graph.of_type(Existing)
        if not disks:
            return
        disk = disks[0].selector
        table = next(
            (one.table for one in config.disk.graph.of_type(PartitionTable) if one.disk in config.disk.graph.nodes),
            None,
        )
        root = config.disk.graph[config.disk.root]
        root_device = root.source if isinstance(root, Mountpoint) else None
        root_fs = FilesystemType.EXT4
        for filesystem in config.disk.graph.of_type(Filesystem):
            if root_device == filesystem.id or (
                root_device is not None and filesystem.id in config.disk.graph.ancestors_of(root_device)
            ):
                root_fs = filesystem.kind
                break
        layout = Layout.WHOLE_DISK
        if config.disk.graph.of_type(ZfsPool):
            layout = Layout.WHOLE_DISK_ZFS
        elif root_fs is FilesystemType.BTRFS:
            layout = Layout.WHOLE_DISK_BTRFS
        swap = next(
            (
                partition.size
                for one in config.disk.graph.of_type(Swap)
                if isinstance((partition := config.disk.graph[one.device]), Partition)
            ),
            None,
        )
        encrypted = next(
            (one.passphrase_file for one in config.disk.graph.of_type(Luks)),
            next((one.passphrase_file for one in config.disk.graph.of_type(ZfsPool)), ""),
        )
        self.choice = Choice(
            disk=disk,
            layout=layout,
            firmware=self.firmware,
            table=table,
            filesystem=root_fs,
            swap=swap,
            passphrase_file=encrypted,
        )
        self.manual = False
        self.layout = manual.Layout()
        self.confirmed.clear()
        self.inspect_disk(disk)

    def shown_as(self, selector: str) -> str:
        """What to call a device on screen.

        The configuration keeps the `/dev/disk/by-id/` selector, because a
        kernel name is assigned at probe time and one saved today installs
        somewhere else after the next reboot. Nobody reads sixty characters of
        it: the screen says `/dev/sda`, which is what `lsblk` says too.
        """
        paths = [
            one
            for one in self.names_for(selector)
            if one.startswith("/dev/") and one != selector
        ]
        return min(paths, key=len) if paths else selector.rsplit("/", 1)[-1]

    def inspect_disk(self, disk: str) -> None:
        self.existing, self.disk_size = self.contents(disk)

    def contents(self, disk: str) -> tuple[tuple[tuple[str, str, str], ...], str]:
        """What that disk holds and how big it is, asked once per disk.

        Cached because the partition screen redraws after every edit and a
        manual table may span several disks; `lsblk` per disk per keystroke is
        a visible pause on a machine with a dozen of them.
        """
        known = self._inspected.get(disk)
        if known is None:
            known = self._inspect(disk)
            self._inspected[disk] = known
        return known


class Answers(TypedDict):
    no: str
    yes: str


def answers(translate: Catalog) -> Answers:
    """The yes and no a `Confirm` shows. Every one of them reads them from the
    catalog, so a translated interface does not answer in English."""
    return {"no": translate("No"), "yes": translate("Yes")}


def footer(translate: Catalog, enter: str = "Continue") -> str:
    """What each key does here. `enter` names the action of this screen: the
    overview said `Continue` on the screen where enter starts the install."""
    return "  ".join(
        (
            f"[enter] {translate(enter)}",
            # Left as well as backspace: left is the only Back that works in
            # every widget, and a field with a value in it takes backspace as
            # a deletion, so an operator reading this footer alone had no way
            # back out of the hostname screen.
            f"[\u2190] {translate('Back')}",
            f"[backspace] {translate('Back')}",
            # Back, not Cancel: escape steps back one screen at every depth
            # below the main menu, where it asks whether to end the run. It
            # said `Cancel` while doing that, and `[q] Cancel` before it,
            # which a field takes as the letter.
            f"[esc] {translate('Back')}",
            # The page names the six this line has no room for. A key nothing
            # writes down is a key nobody finds: `j`, `k`, `tab`, `shift-tab`
            # and `q` all worked here with no status line naming one of them.
            f"[?] {translate('Keys')}",
        )
    )



def say(screen: Screen, context: Context, message: str) -> Answer[None]:
    """One line the operator has to acknowledge, so a rejected entry is not
    silently redrawn as an empty field."""
    answer = Menu(
        title=message,
        items=[Item(label=context.translate("Continue"), value=0)],
        footer=footer(context.translate),
    ).run(screen)
    return Answer(answer.outcome)



def show_address(screen: Screen, context: Context, url: str) -> None:
    """The address as a code and as text, on the screen the operator is on.

    The machine showing this has no browser and no way to copy a line off its
    console. Drawn here rather than after curses exits, because the operator
    asked for it from the overview and is going back to the overview.
    """
    lines, columns = screen.size()
    drawn = []
    try:
        drawn = qr.halved(qr.encode(url))
    except GentooInstallError:
        # Too long for the versions the encoder covers. The address still is
        # the answer, and it is printed either way.
        drawn = []
    if drawn and (len(drawn[0]) > columns or len(drawn) + 4 > lines):
        drawn = []
    screen.clear()
    screen.write(0, 0, clip(url, columns))
    for index, line in enumerate(drawn):
        screen.write(index + 2, 0, line)
    screen.write(min(len(drawn) + 3, lines - 1), 0, context.translate("Continue"))
    screen.show()
    screen.key()


T = TypeVar("T")


@dataclass(frozen=True)
class FieldDescriptor(Generic[T]):
    key: str
    row: Callable[[T, Catalog], Item[str]]
    edit: Callable[[Screen, Context, T], T | None]


V = TypeVar("V")


def current_menu(
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


def pick(
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
    answer = current_menu(
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


DONE: Final[str] = "done"
TABLE: Final[str] = "table"
DROP: Final[str] = "drop"


def with_gentoo_zh(config: InstallConfig) -> PortageConfig:
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


#: What the preflight refuses, read from there rather than written again: the
#: two carried the same 8 and the same reason, and raising one would have left
#: the menu accepting a passphrase the install stops on once the disk is
#: already partitioned.
PASSPHRASE_MINIMUM: Final[int] = ZFS_PASSPHRASE_MINIMUM
