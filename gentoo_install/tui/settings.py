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

from ..model.config import Bootloader, InstallConfig, KernelSource
from ..model.device import Existing, Luks, MdRaid, VolumeGroup, ZfsPool
from . import screens
from .screens import Context, Step

#: Shown for a row the operator has not visited and that has no usable default.
UNSET: Final[str] = "not set"


@dataclass(frozen=True)
class Setting:
    """One row of the main menu."""

    key: str
    label: str
    #: What the row shows to the right of its label.
    value: Callable[[InstallConfig, Context], str]
    edit: Step
    #: A run cannot start until every required row has an answer.
    required: bool = False


def _disk(config: InstallConfig, context: Context) -> str:
    graph = config.disk.graph
    disks = [node.selector.rsplit("/", 1)[-1] for node in graph.of_type(Existing)]
    if not disks:
        return UNSET
    layers = []
    if graph.of_type(Luks):
        layers.append("luks")
    if graph.of_type(MdRaid):
        layers.append("mdraid")
    if graph.of_type(VolumeGroup):
        layers.append("lvm")
    if graph.of_type(ZfsPool):
        layers.append("zfs")
    described = ", ".join(disks)
    return f"{described} ({' '.join(layers)})" if layers else described


def _swap(config: InstallConfig, context: Context) -> str:
    from ..model.device import Swap

    if config.system.zram is not None:
        return f"zram {config.system.zram}"
    if config.disk.graph.of_type(Swap):
        return "a partition"
    return "none"


def _system(config: InstallConfig, context: Context) -> str:
    system = config.system
    return f"{system.hostname}, {system.locale}, {system.timezone}, {system.init.value}"


def _users(config: InstallConfig, context: Context) -> str:
    root = "root has a password" if config.system.root_password_hash else "root is locked"
    named = ", ".join(user.name for user in config.system.users)
    return f"{root}; {named}" if named else root


def _portage(config: InstallConfig, context: Context) -> str:
    portage = config.portage
    measured = ", measured" if portage.mirrors.speed_test else ""
    return f"{portage.profile}, {portage.mirrors.region.value} mirrors{measured}"


def _binhost(config: InstallConfig, context: Context) -> str:
    binhost = config.portage.binhost
    chosen = []
    if binhost.official:
        chosen.append("official")
    if binhost.community.value != "off":
        chosen.append(f"gentoo-zh {binhost.community.value}")
    return ", ".join(chosen) if chosen else "off, compile everything"


def _kernel(config: InstallConfig, context: Context) -> str:
    return config.kernel.package or config.kernel.source.value


def _bootloader(config: InstallConfig, context: Context) -> str:
    return f"{config.bootloader.kind.value}, {config.bootloader.firmware.value}"


def _packages(config: InstallConfig, context: Context) -> str:
    desktop = config.packages.desktop or "no desktop"
    applications = ", ".join(config.packages.applications)
    return f"{desktop}; {applications}" if applications else desktop


def _network(config: InstallConfig, context: Context) -> str:
    return "sshd enabled" if config.system.sshd else "sshd off"


#: The menu, in the order it is drawn.
SETTINGS: Final[tuple[Setting, ...]] = (
    Setting("disk", "Disks", _disk, screens.disk_menu, required=True),
    Setting("swap", "Swap", _swap, screens.swap_screen),
    Setting("system", "Target system", _system, screens.system_menu, required=True),
    Setting("users", "Users", _users, screens.users_menu, required=True),
    Setting("portage", "Portage", _portage, screens.portage_menu),
    Setting("binhost", "Binary packages", _binhost, screens.binhost_screen),
    Setting("kernel", "Kernel", _kernel, screens.kernel_screen),
    Setting("bootloader", "Bootloader", _bootloader, screens.bootloader_screen),
    Setting("packages", "Desktop and applications", _packages, screens.packages_menu),
    Setting("network", "Network and SSH", _network, screens.sshd_screen),
)


def unanswered(config: InstallConfig, context: Context) -> tuple[str, ...]:
    """Required rows still showing nothing, which is what blocks the install."""
    return tuple(
        setting.label
        for setting in SETTINGS
        if setting.required and setting.value(config, context) == UNSET
    )
