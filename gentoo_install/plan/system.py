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

from ..errors import LocaleMissing
from ..model.config import ConsoleFontSize, InitSystem, InstallConfig, User
from ..model.device import (
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
USR = PurePosixPath("/usr")


@dataclass(frozen=True, kw_only=True)
class GenerateLocales(Operation):
    """`locale-gen` returns 0 even when it skipped one, and a missing locale
    only shows up later as a system running under C."""

    stage: Stage = Stage.SYSTEM
    locales: tuple[str, ...]

    def describe(self) -> str:
        return f"generate and verify locales {', '.join(self.locales)}"

    def apply(self, context: Context) -> None:
        content = "".join(f"{locale} {locale.split('.')[-1]}\n" for locale in self.locales)
        context.write(PurePosixPath("/etc/locale.gen"), content)
        context.run_in_target(["locale-gen"])
        available = context.run_in_target(["locale", "--all-locales"]).lower().split()
        missing = [
            locale for locale in self.locales if _normalised(locale) not in available
        ]
        if missing:
            for locale in missing:
                name, _, charset = locale.partition(".")
                context.run_in_target(["localedef", "--inputfile", name, "--charmap", charset, locale])
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
        else:
            context.run_in_target(["dbus-uuidgen", "--ensure=/etc/machine-id"])


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
class WriteNetworkConfig(Operation):
    """A wired interface with DHCP. systemd-networkd does nothing without a
    `.network` file, so enabling the service alone leaves the system offline."""

    stage: Stage = Stage.SYSTEM
    init: InitSystem

    def describe(self) -> str:
        return "configure the wired interface for DHCP"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.SYSTEMD:
            context.write(
                PurePosixPath("/etc/systemd/network/20-wired.network"),
                "[Match]\nName=en*\nName=eth*\n\n[Network]\nDHCP=yes\n",
            )
            return
        context.write(PurePosixPath("/etc/conf.d/net"), 'config_eth0="dhcp"\n')


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
    ]
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
    operations.append(SetRootPassword(password_hash=system.root_password_hash))
    if any(user.sudo for user in system.users):
        operations.append(GrantSudo())
    if system.sshd:
        operations += [
            Emerge(stage=Stage.SYSTEM, packages=("net-misc/openssh",), summary="install sshd"),
            EnableService(service=_sshd_service(system.init), init=system.init),
        ]
    operations += [
        Emerge(
            stage=Stage.SYSTEM,
            packages=_network_packages(system.init),
            summary="install the network tools",
        ),
        WriteNetworkConfig(init=system.init),
        EnableService(service=_network_service(system.init), init=system.init),
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
                    options=(*_default_options(filesystem.kind), *mount.options, f"subvol={source.name}"),
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
                    options=(*_default_options(source.kind), *mount.options),
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
    early = _containers_under(config)
    return tuple(
        CrypttabEntry(name=node.name, backing=node.backing, initrd_attach=node.id in early)
        for node in graph.of_type(Luks)
    )


def _containers_under(config: InstallConfig) -> frozenset[DeviceId]:
    """LUKS containers carrying `/` or `/usr`, which the initramfs opens."""
    graph = config.disk.graph
    found: set[DeviceId] = set()
    for mount in graph.of_type(Mountpoint):
        if mount.path not in (ROOT, USR):
            continue
        found |= {
            node.id for node in graph.of_type(Luks) if node.id in graph.ancestors_of(mount.id)
        }
    return frozenset(found)


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


def _sshd_service(init: InitSystem) -> str:
    return "sshd.service" if init is InitSystem.SYSTEMD else "sshd"


def _network_service(init: InitSystem) -> str:
    return "systemd-networkd.service" if init is InitSystem.SYSTEMD else "dhcpcd"


def _network_packages(init: InitSystem) -> tuple[str, ...]:
    if init is InitSystem.SYSTEMD:
        return ("net-misc/dhcpcd",)
    # stage3 carries no netifrc, and openrc's net.* scripts are nothing without it.
    return ("net-misc/netifrc", "net-misc/dhcpcd")


def _set_password(context: Context, user: str, password_hash: str) -> None:
    if not password_hash:
        context.run_in_target(["passwd", "--lock", user])
        return
    context.run_in_target(["usermod", "--password", password_hash, user])


def _normalised(locale: str) -> str:
    """`locale -a` prints `zh_CN.utf8` for what locale.gen calls `zh_CN.UTF-8`."""
    return locale.lower().replace("-", "")
