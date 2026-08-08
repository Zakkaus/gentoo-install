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
from ..model.device import Existing, Luks, MdRaid, PartitionTable, VolumeGroup, ZfsPool
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
    edit: Step | None
    #: A run cannot start until every required row has an answer.
    required: bool = False


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


def _network(config: InstallConfig, context: Context) -> str:
    return config.system.networking.value


def _mirror(config: InstallConfig, context: Context) -> str:
    mirrors = config.portage.mirrors
    return f"{mirrors.region.value}, measured" if mirrors.speed_test else mirrors.region.value


def _repositories(config: InstallConfig, context: Context) -> str:
    return ", ".join(overlay.name for overlay in config.portage.overlays) or "none"


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


def _applications(config: InstallConfig, context: Context) -> str:
    return ", ".join(config.packages.applications) or "none"


def _license(config: InstallConfig, context: Context) -> str:
    return " ".join(config.portage.accept_license)


def _makeopts(config: InstallConfig, context: Context) -> str:
    return config.portage.makeopts or f"-j{context.cores} (this machine)"


def _extra(config: InstallConfig, context: Context) -> str:
    return " ".join(config.packages.extra) or "none"


def _erase(config: InstallConfig, context: Context) -> str:
    return "confirmed" if context.erase_confirmed else UNSET


#: The menu, flat and in the order it is drawn. One row per decision: nesting
#: hides a choice behind a heading nobody opens, and `archinstall` reaches the
#: same conclusion.
SETTINGS: Final[tuple[Setting, ...]] = (
    # Detected and shown, never chosen: the UEFI and BIOS paths differ and
    # installing for the one the machine did not boot is a mistake.
    Setting("firmware", "Firmware", _firmware, None),
    Setting("keymap", "Keyboard layout", lambda c, x: c.system.keymap, screens.keymap_screen),
    Setting("locale", "System language", lambda c, x: c.system.locale, screens.locale_screen),
    Setting("timezone", "Timezone", lambda c, x: c.system.timezone, screens.timezone_screen),
    Setting("mirror", "Mirror region", _mirror, screens.mirror_screen),
    Setting("repositories", "Optional repositories", _repositories, screens.repositories_screen),
    Setting("disk", "Drive", _drive, screens.disk_screen, required=True),
    Setting("table", "Partition table", _table, screens.table_screen),
    Setting("layout", "Layout", _layout, screens.layout_screen),
    Setting("partitions", "Partitions", _partitions, screens.partitions_screen),
    Setting("encryption", "Encryption", _encryption, screens.encryption_screen),
    Setting("swap", "Swap", _swap, screens.swap_screen),
    Setting("hostname", "Hostname", lambda c, x: c.system.hostname, screens.system_screen),
    Setting("init", "Init system", lambda c, x: c.system.init.value, screens.init_screen),
    Setting("profile", "Profile", lambda c, x: c.portage.profile, screens._profile_screen),
    Setting("license", "Licenses", _license, screens.license_screen),
    Setting("makeopts", "Compile jobs", _makeopts, screens.makeopts_screen),
    Setting("root", "Root password", _root, screens.root_password_screen, required=True),
    Setting("user", "User account", _user, screens.user_screen),
    Setting("kernel", "Kernel", _kernel, screens.kernel_screen),
    Setting("bootloader", "Bootloader", _bootloader, screens.bootloader_screen),
    Setting("binhost", "Binary packages", _binhost, screens.binhost_screen),
    Setting("desktop", "Desktop", lambda c, x: c.packages.desktop or "none", screens.desktop_screen),
    Setting("packages", "Package groups", _applications, screens.packages_screen),
    Setting("extra", "Extra packages", _extra, screens.extra_packages_screen),
    Setting("network", "Network configuration", _network, screens.networking_screen),
    Setting("sshd", "SSH server", lambda c, x: "on" if c.system.sshd else "off", screens.sshd_screen),
    Setting("erase", "Confirm erasing the drive", _erase, screens.erase_screen, required=True),
)


def unanswered(config: InstallConfig, context: Context) -> tuple[str, ...]:
    """Required rows still showing nothing, which is what blocks the install."""
    return tuple(
        setting.label
        for setting in SETTINGS
        if setting.required and setting.value(config, context) == UNSET
    )
