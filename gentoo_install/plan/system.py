"""The target's own settings: locale, time, console, identity, mounts, users.

Two things here are not obvious and cost an unbootable system when missed.
`locale-gen` exits 0 having skipped a locale, so each one is verified against
`locale -a` afterwards. An encrypted `/` or `/usr` needs `x-initrd.attach` in
crypttab, or systemd waits forever for a device the initramfs already attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from ..errors import InvalidLayout, LocaleMissing
from ..model import compat
from ..model.config import ConsoleFontSize, InitSystem, InstallConfig, Networking, SystemConfig, User
from ..model.size import Size
from ..model.device import (
    MdRaid,
    DeviceId,
    Filesystem,
    FilesystemType,
    Luks,
    Mountpoint,
    Subvolume,
    Swap,
    ZfsDataset,
)
from .operations import Context, Operation, Stage
from .portage import Emerge

#: Console fonts `sys-apps/kbd` installs, by the cell size they draw.
CONSOLE_FONTS: Final[dict[ConsoleFontSize, str]] = {
    ConsoleFontSize.SIZE_8X8: "lat2-08",
    ConsoleFontSize.SIZE_8X16: "default8x16",
    ConsoleFontSize.SIZE_16X32: "latarcyrheb-sun32",
}

#: Groups a desktop user needs. `wheel` is what sudo is granted through.
USER_GROUPS: Final[tuple[str, ...]] = ("users", "wheel", "audio", "video", "render", "usb", "input")

ROOT = PurePosixPath("/")


@dataclass(frozen=True, kw_only=True)
class GenerateLocales(Operation):
    """`locale-gen` returns 0 even when it skipped one, and a missing locale
    only shows up later as a system running under C."""

    stage: Stage = Stage.SYSTEM
    locales: tuple[str, ...]

    def describe(self) -> str:
        return f"generate and verify locales {', '.join(self.locales)}"

    def apply(self, context: Context) -> None:
        content = "".join(f"{locale} {_charmap(locale)}\n" for locale in self.locales)
        context.write(PurePosixPath("/etc/locale.gen"), content)
        context.run_in_target(["locale-gen"])
        available = context.run_in_target(["locale", "--all-locales"]).lower().split()
        missing = [
            locale for locale in self.locales if _normalised(locale) not in available
        ]
        if missing:
            for locale in missing:
                name = locale.split(".", 1)[0]
                context.run_in_target(
                    ["localedef", "--inputfile", name, "--charmap", _charmap(locale), locale]
                )
            still = context.run_in_target(["locale", "--all-locales"]).lower().split()
            absent = [locale for locale in self.locales if _normalised(locale) not in still]
            if absent:
                raise LocaleMissing(f"the target has no {', '.join(absent)} after locale-gen and localedef")


@dataclass(frozen=True, kw_only=True)
class SelectLocale(Operation):
    stage: Stage = Stage.SYSTEM
    locale: str

    def describe(self) -> str:
        return f"set the system locale to {self.locale}"

    def apply(self, context: Context) -> None:
        context.write(PurePosixPath("/etc/locale.conf"), f"LANG={self.locale}\n")


@dataclass(frozen=True, kw_only=True)
class SetTimezone(Operation):
    stage: Stage = Stage.SYSTEM
    timezone: str

    def describe(self) -> str:
        return f"set the timezone to {self.timezone}"

    def apply(self, context: Context) -> None:
        context.run_in_target(
            ["ln", "--symbolic", "--force", f"/usr/share/zoneinfo/{self.timezone}", "/etc/localtime"]
        )
        context.write(PurePosixPath("/etc/timezone"), f"{self.timezone}\n")


@dataclass(frozen=True, kw_only=True)
class ConfigureConsole(Operation):
    """Keymap and font. `cn` is not a keymap the console has, so a Chinese
    system still types on `us` and gets its CJK from the font, not the keymap."""

    stage: Stage = Stage.SYSTEM
    keymap: str
    font: str
    init: InitSystem

    def describe(self) -> str:
        return f"set the console keymap to {self.keymap} and its font to {self.font}"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.SYSTEMD:
            context.write(
                PurePosixPath("/etc/vconsole.conf"), f"KEYMAP={self.keymap}\nFONT={self.font}\n"
            )
            return
        context.write(PurePosixPath("/etc/conf.d/keymaps"), f'keymap="{self.keymap}"\n')
        context.write(PurePosixPath("/etc/conf.d/consolefont"), f'consolefont="{self.font}"\n')


@dataclass(frozen=True, kw_only=True)
class SetHostname(Operation):
    stage: Stage = Stage.SYSTEM
    hostname: str
    init: InitSystem

    def describe(self) -> str:
        return f"set the hostname to {self.hostname}"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.SYSTEMD:
            context.write(PurePosixPath("/etc/hostname"), f"{self.hostname}\n")
        else:
            context.write(PurePosixPath("/etc/conf.d/hostname"), f'hostname="{self.hostname}"\n')
        context.write(
            PurePosixPath("/etc/hosts"),
            "127.0.0.1\tlocalhost\n"
            "::1\t\tlocalhost\n"
            f"127.0.1.1\t{self.hostname}.localdomain\t{self.hostname}\n",
        )


@dataclass(frozen=True, kw_only=True)
class WriteMachineId(Operation):
    stage: Stage = Stage.SYSTEM
    init: InitSystem

    def describe(self) -> str:
        return "give the target its own machine id"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.SYSTEMD:
            context.run_in_target(["systemd-machine-id-setup"])
            return
        # `dbus-uuidgen` would need sys-apps/dbus, which a stage3 has not got.
        # The kernel hands out a fresh uuid to anyone who reads this file.
        value = context.run_in_target(["cat", "/proc/sys/kernel/random/uuid"]).strip()
        context.write(PurePosixPath("/etc/machine-id"), value.replace("-", "") + "\n")


@dataclass(frozen=True)
class FstabEntry:
    """A line of fstab. The device is still an id here; `apply` turns it into a
    UUID, because a UUID survives the disk being renumbered and `/dev/sda2` does
    not."""

    device: DeviceId
    path: PurePosixPath
    kind: str
    options: tuple[str, ...]
    dump: int
    check: int


@dataclass(frozen=True, kw_only=True)
class WriteFstab(Operation):
    stage: Stage = Stage.SYSTEM
    entries: tuple[FstabEntry, ...]

    def describe(self) -> str:
        mounts = ", ".join(
            "swap" if entry.kind == "swap" else str(entry.path) for entry in self.entries
        )
        return f"write /etc/fstab with entries for {mounts}"

    def apply(self, context: Context) -> None:
        lines = ["# device\tmountpoint\ttype\toptions\tdump\tpass"]
        for entry in self.entries:
            options = ",".join(entry.options) or "defaults"
            lines.append(
                f"UUID={context.device_uuid(entry.device)}\t{entry.path}\t{entry.kind}\t"
                f"{options}\t{entry.dump}\t{entry.check}"
            )
        context.write(PurePosixPath("/etc/fstab"), "\n".join(lines) + "\n")


@dataclass(frozen=True)
class CrypttabEntry:
    name: str
    backing: DeviceId
    #: systemd waits for a `/` or `/usr` device the initramfs already attached
    #: unless the entry says so. Boot stops at a start job with no limit.
    initrd_attach: bool


@dataclass(frozen=True, kw_only=True)
class WriteCrypttab(Operation):
    stage: Stage = Stage.SYSTEM
    entries: tuple[CrypttabEntry, ...]

    def describe(self) -> str:
        names = ", ".join(entry.name for entry in self.entries)
        return f"write /etc/crypttab for {names}"

    def apply(self, context: Context) -> None:
        lines = []
        for entry in self.entries:
            options = "luks,x-initrd.attach" if entry.initrd_attach else "luks"
            lines.append(f"{entry.name}\tUUID={context.device_uuid(entry.backing)}\tnone\t{options}")
        context.write(PurePosixPath("/etc/crypttab"), "\n".join(lines) + "\n")


@dataclass(frozen=True, kw_only=True)
class CreateUser(Operation):
    stage: Stage = Stage.SYSTEM
    name: str
    groups: tuple[str, ...]
    shell: str
    password_hash: str

    def describe(self) -> str:
        locked = "no password" if not self.password_hash else "a password"
        return f"create user {self.name} in {', '.join(self.groups)} with {locked}"

    def apply(self, context: Context) -> None:
        context.run_in_target(
            [
                "useradd",
                "--create-home",
                "--groups", ",".join(self.groups),
                "--shell", self.shell,
                self.name,
            ]
        )
        _set_password(context, self.name, self.password_hash)


@dataclass(frozen=True, kw_only=True)
class SetRootPassword(Operation):
    stage: Stage = Stage.SYSTEM
    password_hash: str

    def describe(self) -> str:
        return "set the root password" if self.password_hash else "lock the root account"

    def apply(self, context: Context) -> None:
        _set_password(context, "root", self.password_hash)


@dataclass(frozen=True, kw_only=True)
class GrantSudo(Operation):
    stage: Stage = Stage.SYSTEM

    def describe(self) -> str:
        return "let the wheel group run sudo, with a password"

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath("/etc/sudoers.d/10-wheel"), "%wheel ALL=(ALL:ALL) ALL\n", mode=0o440
        )


@dataclass(frozen=True, kw_only=True)
class RequestNetworkUse(Operation):
    """Written in the portage phase, before NetworkManager is merged."""

    stage: Stage = Stage.PORTAGE
    lines: tuple[str, ...]

    def describe(self) -> str:
        return f"ask for {'; '.join(self.lines)}"

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath("/etc/portage/package.use/network"),
            "".join(f"{line}\n" for line in self.lines),
        )


@dataclass(frozen=True, kw_only=True)
class WriteNetworkConfig(Operation):
    """A wired interface with DHCP. systemd-networkd does nothing without a
    `.network` file, so enabling the service alone leaves the system offline.

    NetworkManager needs no file: it manages every unconfigured interface.
    """

    stage: Stage = Stage.SYSTEM
    init: InitSystem
    networking: Networking

    def describe(self) -> str:
        if self.networking in (Networking.NETWORKMANAGER_WPA, Networking.NETWORKMANAGER_IWD):
            return f"leave the interfaces to NetworkManager ({self.networking.value})"
        return "configure the wired interface for DHCP"

    def apply(self, context: Context) -> None:
        if self.networking in (Networking.NETWORKMANAGER_WPA, Networking.NETWORKMANAGER_IWD):
            return
        if self.init is InitSystem.SYSTEMD:
            context.write(
                PurePosixPath("/etc/systemd/network/20-wired.network"),
                "[Match]\nName=en*\nName=eth*\n\n[Network]\nDHCP=yes\n",
            )
            return
        context.write(PurePosixPath("/etc/conf.d/net"), 'config_eth0="dhcp"\n')


@dataclass(frozen=True, kw_only=True)
class EnableSerialGetty(Operation):
    """A login on the serial console.

    systemd starts one by itself when the kernel command line names a serial
    console; openrc does not, and its inittab ships the serial lines commented
    out, so a machine installed for remote use comes up with no way in.
    """

    stage: Stage = Stage.SYSTEM
    port: str
    baud: int

    def describe(self) -> str:
        return f"start a login on {self.port} at {self.baud} baud"

    def apply(self, context: Context) -> None:
        # The id field takes at most four characters, so sysvinit drops a
        # `ttyS0` entry and the console stays silent. Gentoo's example says s0.
        entry = f"s{self.port.removeprefix('ttyS')}"
        context.append(
            PurePosixPath("/etc/inittab"),
            f"\n{entry}:12345:respawn:/sbin/agetty -L {self.baud} {self.port} vt100\n",
        )


#: What provides zram on each init. systemd has a generator that reads one
#: config file; openrc has an init script that reads conf.d.
ZRAM_PACKAGE: Final[dict[InitSystem, str]] = {
    InitSystem.SYSTEMD: "sys-apps/zram-generator",
    InitSystem.OPENRC: "sys-block/zram-init",
}


@dataclass(frozen=True, kw_only=True)
class ConfigureZram(Operation):
    stage: Stage = Stage.SYSTEM
    size: Size
    init: InitSystem

    def describe(self) -> str:
        return f"configure {self.size} of compressed swap in memory"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.SYSTEMD:
            context.write(
                PurePosixPath("/etc/systemd/zram-generator.conf"),
                f"[zram0]\nzram-size = {self.size.bytes // 1024 ** 2}\n"
                "compression-algorithm = zstd\n",
            )
            return
        context.write(
            PurePosixPath("/etc/conf.d/zram-init"),
            "load_on_start=yes\nunload_on_stop=yes\nnum_devices=1\n"
            "type0=swap\n"
            f"size0={self.size.bytes // 1024 ** 2}\n"
            "algo0=zstd\nlabl0=zram-swap\n",
        )


@dataclass(frozen=True, kw_only=True)
class WriteMdadmConf(Operation):
    """The array definition the initramfs assembles from.

    Without it dracut's mdraid module brings the array up under whatever name
    the kernel picks, the root UUID never appears, and boot stops in the
    emergency shell.
    """

    stage: Stage = Stage.SYSTEM

    def describe(self) -> str:
        return "write /etc/mdadm.conf from the arrays this run created"

    def apply(self, context: Context) -> None:
        scanned = context.run(["mdadm", "--detail", "--scan"]).strip()
        if not scanned:
            raise InvalidLayout("mdadm reports no array to record in /etc/mdadm.conf")
        # MAILADDR as well: `mdadm --monitor` exits with an error when it has
        # nobody to alert, leaving a healthy array with a failed unit.
        context.write(PurePosixPath("/etc/mdadm.conf"), f"MAILADDR root\n{scanned}\n")


@dataclass(frozen=True, kw_only=True)
class SetHardwareClock(Operation):
    """What the RTC is taken to hold. Wrong here and the clock is off by the
    timezone offset every boot until something corrects it."""

    stage: Stage = Stage.SYSTEM
    utc: bool
    init: InitSystem

    def describe(self) -> str:
        return f"treat the hardware clock as {'UTC' if self.utc else 'local time'}"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.OPENRC:
            context.write(
                PurePosixPath("/etc/conf.d/hwclock"),
                f'clock="{"UTC" if self.utc else "local"}"\n',
            )
            return
        # systemd reads the third line of /etc/adjtime and nothing else.
        context.write(
            PurePosixPath("/etc/adjtime"),
            f"0.0 0 0.0\n0\n{'UTC' if self.utc else 'LOCAL'}\n",
        )


@dataclass(frozen=True, kw_only=True)
class EnableService(Operation):
    stage: Stage = Stage.SYSTEM
    service: str
    init: InitSystem
    runlevel: str = "default"

    def describe(self) -> str:
        return f"enable {self.service} at boot"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.SYSTEMD:
            context.run_in_target(["systemctl", "enable", self.service])
        else:
            context.run_in_target(["rc-update", "add", self.service, self.runlevel])


def build(config: InstallConfig) -> list[Operation]:
    system = config.system
    operations: list[Operation] = [
        GenerateLocales(locales=system.locales),
        SelectLocale(locale=system.locale),
        SetTimezone(timezone=system.timezone),
        ConfigureConsole(
            keymap=system.keymap, font=CONSOLE_FONTS[system.console_font], init=system.init
        ),
        SetHostname(hostname=system.hostname, init=system.init),
        WriteMachineId(init=system.init),
        WriteFstab(entries=fstab_entries(config)),
        SetHardwareClock(utc=system.hardware_clock_utc, init=system.init),
    ]
    if config.disk.graph.of_type(MdRaid):
        operations.append(WriteMdadmConf())
    if system.zram is not None:
        operations += [
            Emerge(
                stage=Stage.SYSTEM,
                packages=(ZRAM_PACKAGE[system.init],),
                summary="install the compressed swap device",
            ),
            ConfigureZram(size=system.zram, init=system.init),
        ]
        if system.init is InitSystem.OPENRC:
            # systemd needs no unit here: the generator makes one from the file.
            operations.append(
                EnableService(service="zram-init", init=system.init, runlevel="boot")
            )
    crypttab = crypttab_entries(config)
    if crypttab:
        operations.append(WriteCrypttab(entries=crypttab))
    for user in system.users:
        operations.append(
            CreateUser(
                name=user.name,
                groups=_groups_of(user),
                shell=user.shell,
                password_hash=user.password_hash,
            )
        )
    serial = _serial_console(config)
    if serial is not None and system.init is InitSystem.OPENRC:
        operations.append(EnableSerialGetty(port=serial[0], baud=serial[1]))
    operations.append(SetRootPassword(password_hash=system.root_password_hash))
    if any(user.sudo for user in system.users):
        operations += [
            Emerge(stage=Stage.SYSTEM, packages=("app-admin/sudo",), summary="install sudo"),
            GrantSudo(),
        ]
    if system.sshd:
        operations += [
            Emerge(stage=Stage.SYSTEM, packages=("net-misc/openssh",), summary="install sshd"),
            EnableService(service=_sshd_service(system.init), init=system.init),
        ]
    flags = _network_use(system)
    if flags:
        operations.append(RequestNetworkUse(lines=flags))
    network = _network_packages(system)
    if network:
        operations.append(
            Emerge(stage=Stage.SYSTEM, packages=network, summary="install the network tools")
        )
    operations += [
        WriteNetworkConfig(init=system.init, networking=system.networking),
        EnableService(service=_network_service(system), init=system.init),
    ]
    return operations


def fstab_entries(config: InstallConfig) -> tuple[FstabEntry, ...]:
    graph = config.disk.graph
    entries: list[FstabEntry] = []
    for mount in sorted(graph.of_type(Mountpoint), key=lambda node: len(node.path.parts)):
        source = graph[mount.source]
        if isinstance(source, ZfsDataset):
            # A dataset carries its mountpoint property; fstab would fight it.
            continue
        if isinstance(source, Subvolume):
            filesystem = graph[source.filesystem]
            if not isinstance(filesystem, Filesystem):
                continue
            entries.append(
                FstabEntry(
                    device=filesystem.device,
                    path=mount.path,
                    kind=filesystem.kind.value,
                    options=_options(filesystem.kind, mount.options, f"subvol={source.name}"),
                    dump=0,
                    check=_check_order(mount.path),
                )
            )
            continue
        if isinstance(source, Filesystem):
            entries.append(
                FstabEntry(
                    device=source.device,
                    path=mount.path,
                    kind=source.kind.value,
                    options=_options(source.kind, mount.options),
                    dump=0,
                    check=_check_order(mount.path),
                )
            )
    for swap in graph.of_type(Swap):
        entries.append(
            FstabEntry(
                device=swap.device,
                path=PurePosixPath("none"),
                kind="swap",
                options=("sw",),
                dump=0,
                check=0,
            )
        )
    return tuple(entries)


def crypttab_entries(config: InstallConfig) -> tuple[CrypttabEntry, ...]:
    graph = config.disk.graph
    early = {node.id for node in compat.early_containers(config.disk.graph)}
    return tuple(
        CrypttabEntry(name=node.name, backing=node.backing, initrd_attach=node.id in early)
        for node in graph.of_type(Luks)
    )



def _options(kind: FilesystemType, chosen: tuple[str, ...], *extra: str) -> tuple[str, ...]:
    """The defaults for a filesystem, then what the layout asked for.

    An option the layout already sets replaces the default rather than joining
    it: `umask=0077,umask=0077` is what mount reads, and only the last one wins.
    """
    kept: list[str] = []
    named = {option.split("=", 1)[0] for option in (*chosen, *extra)}
    for option in _default_options(kind):
        if option.split("=", 1)[0] not in named:
            kept.append(option)
    return (*kept, *chosen, *extra)


def _default_options(kind: FilesystemType) -> tuple[str, ...]:
    if kind is FilesystemType.VFAT:
        return ("defaults", "umask=0077")
    if kind is FilesystemType.BTRFS:
        return ("defaults", "compress=zstd:1")
    return ("defaults",)


def _check_order(path: PurePosixPath) -> int:
    """fsck order: the root filesystem first, everything else after it."""
    return 1 if path == ROOT else 2


def _groups_of(user: User) -> tuple[str, ...]:
    groups = list(USER_GROUPS if user.sudo else [group for group in USER_GROUPS if group != "wheel"])
    for group in user.groups:
        if group not in groups:
            groups.append(group)
    return tuple(groups)


def _serial_console(config: InstallConfig) -> tuple[str, int] | None:
    """The serial port and speed the kernel command line asks for, if any."""
    for parameter in config.bootloader.kernel_params:
        if not parameter.startswith("console=ttyS"):
            continue
        value = parameter.split("=", 1)[1]
        port, _, rest = value.partition(",")
        digits = "".join(character for character in rest if character.isdigit())
        return port, int(digits) if digits else 115200
    return None


def _sshd_service(init: InitSystem) -> str:
    return "sshd.service" if init is InitSystem.SYSTEMD else "sshd"


def _network_service(system: SystemConfig) -> str:
    if system.networking in (Networking.NETWORKMANAGER_WPA, Networking.NETWORKMANAGER_IWD):
        return "NetworkManager.service" if system.init is InitSystem.SYSTEMD else "NetworkManager"
    return "systemd-networkd.service" if system.init is InitSystem.SYSTEMD else "dhcpcd"


def _network_packages(system: SystemConfig) -> tuple[str, ...]:
    if system.networking is Networking.NETWORKMANAGER_IWD:
        # `iwd` replaces wpa_supplicant as the wifi backend, and it is the flag
        # rather than the package that decides which NetworkManager talks to.
        return ("net-misc/networkmanager", "net-wireless/iwd")
    if system.networking is Networking.NETWORKMANAGER_WPA:
        return ("net-misc/networkmanager", "net-wireless/wpa_supplicant")
    if system.init is InitSystem.SYSTEMD:
        # networkd is part of systemd and does the DHCP itself.
        return ()
    # stage3 carries no netifrc, and openrc's net.* scripts are nothing without it.
    return ("net-misc/netifrc", "net-misc/dhcpcd")


def _network_use(system: SystemConfig) -> tuple[str, ...]:
    """The flag that picks NetworkManager's wifi backend."""
    if system.networking is Networking.NETWORKMANAGER_IWD:
        return ("net-misc/networkmanager iwd",)
    return ()


def _set_password(context: Context, user: str, password_hash: str) -> None:
    if not password_hash:
        context.run_in_target(["passwd", "--lock", user])
        return
    context.run_in_target(["usermod", "--password", password_hash, user])


def _charmap(locale: str) -> str:
    """`zh_TW.UTF-8` names its charmap; `C` and `en_US` do not, and locale.gen
    needs one either way."""
    _, _, charmap = locale.partition(".")
    return charmap or "UTF-8"


def _normalised(locale: str) -> str:
    """`locale -a` prints `zh_CN.utf8` for what locale.gen calls `zh_CN.UTF-8`."""
    return locale.lower().replace("-", "")
