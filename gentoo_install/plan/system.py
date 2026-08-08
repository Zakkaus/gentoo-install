"""The target's own settings: locale, time, console, identity, mounts, users."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from ..errors import InvalidLayout, LocaleMissing
from ..model import compat
from ..model.config import (
    ConsoleFontSize,
    InitSystem,
    InstallConfig,
    Logger,
    Networking,
    SystemConfig,
    User,
)
from ..model.size import Size
from ..model.device import (
    MdRaid,
    Node,
    DeviceId,
    Filesystem,
    FilesystemType,
    Luks,
    Mountpoint,
    Subvolume,
    Swap,
    VolumeGroup,
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
    """Where each init reads its containers from.

    openrc reads none of `/etc/crypttab`: `sys-fs/cryptsetup` ships a `dmcrypt`
    service that reads `/etc/conf.d/dmcrypt`, so writing crypttab there left a
    container nobody opened at boot.
    """

    stage: Stage = Stage.SYSTEM
    entries: tuple[CrypttabEntry, ...]
    init: InitSystem

    def describe(self) -> str:
        names = ", ".join(entry.name for entry in self.entries)
        where = "/etc/crypttab" if self.init is InitSystem.SYSTEMD else "/etc/conf.d/dmcrypt"
        return f"write {where} for {names}"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.SYSTEMD:
            lines = []
            for entry in self.entries:
                options = "luks,x-initrd.attach" if entry.initrd_attach else "luks"
                lines.append(
                    f"{entry.name}\tUUID={context.device_uuid(entry.backing)}\tnone\t{options}"
                )
            context.write(PurePosixPath("/etc/crypttab"), "\n".join(lines) + "\n")
            return
        # Each target starts a section, which is what the init script's own
        # parser assumes; the root is already open, so it is left out.
        sections = [
            f"target={entry.name}\nsource='UUID={context.device_uuid(entry.backing)}'"
            for entry in self.entries
            if not entry.initrd_attach
        ]
        context.write(
            PurePosixPath("/etc/conf.d/dmcrypt"), "\n\n".join(sections) + "\n" if sections else ""
        )


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
    #: Empty matches `en*` and `eth*` on systemd. netifrc has no wildcard, so
    #: an empty name there falls back to `eth0`.
    interface: str = ""
    #: CIDR, either family. Empty is DHCP and router advertisements.
    addresses: tuple[str, ...] = ()
    gateways: tuple[str, ...] = ()
    dns: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.networking is Networking.NONE:
            return "leave the network unconfigured"
        if self.networking is not Networking.BUILTIN:
            return f"leave the interfaces to NetworkManager ({self.networking.value})"
        where = self.interface or "the wired interface"
        if self.addresses:
            return f"configure {where} as {', '.join(self.addresses)}"
        return f"configure {where} for DHCP"

    def apply(self, context: Context) -> None:
        if self.networking is not Networking.BUILTIN:
            # NetworkManager manages every unconfigured interface itself, and
            # NONE means the operator brings the link up by hand. Writing a
            # DHCP file for either is configuring what they asked not to be.
            return
        if self.init is InitSystem.SYSTEMD:
            context.write(
                PurePosixPath("/etc/systemd/network/20-wired.network"), self._networkd()
            )
            return
        context.write(PurePosixPath("/etc/conf.d/net"), self._netifrc())

    def _networkd(self) -> str:
        match = f"Name={self.interface}\n" if self.interface else "Name=en*\nName=eth*\n"
        lines = [f"[Match]\n{match}", "[Network]\n"]
        if self.addresses:
            lines += [f"Address={one}\n" for one in self.addresses]
            lines += [f"Gateway={one}\n" for one in self.gateways]
        else:
            # Router advertisements as well: that is how most v6 networks hand
            # out a prefix, and DHCP=yes does not ask for one.
            lines += ["DHCP=yes\n", "IPv6AcceptRA=yes\n"]
        lines += [f"DNS={one}\n" for one in self.dns]
        return "".join(lines)

    def _netifrc(self) -> str:
        name = self.interface or "eth0"
        if not self.addresses:
            return f'config_{name}="dhcp"\n'
        lines = [f'config_{name}="{" ".join(self.addresses)}"\n']
        # `default via` for v4 and `default via` for v6 both go in `routes_`;
        # netifrc reads the family from the address.
        if self.gateways:
            routes = "\n".join(f"default via {one}" for one in self.gateways)
            lines.append(f'routes_{name}="{routes}"\n')
        if self.dns:
            lines.append(f'dns_servers_{name}="{" ".join(self.dns)}"\n')
        return "".join(lines)


@dataclass(frozen=True, kw_only=True)
class LinkNetifrcService(Operation):
    """netifrc runs one service per interface, each a symlink to `net.lo`.

    Without it `/etc/conf.d/net` is a file nobody reads, and an openrc machine
    given a static address comes up with none.
    """

    stage: Stage = Stage.SYSTEM
    interface: str

    def describe(self) -> str:
        return f"enable net.{self.interface} so netifrc applies the static address"

    def apply(self, context: Context) -> None:
        service = f"net.{self.interface}"
        context.run_in_target(
            ["ln", "--symbolic", "--force", "net.lo", f"/etc/init.d/{service}"]
        )
        context.run_in_target(["rc-update", "add", service, "default"])


@dataclass(frozen=True, kw_only=True)
class WriteAuthorizedKeys(Operation):
    """Keys the named accounts may log in with.

    A headless install with no key and no console is reachable only by taking
    the disk out, so this is written before the first boot rather than after.
    """

    stage: Stage = Stage.SYSTEM
    keys: tuple[str, ...]
    #: Account name and home directory. root is included unless sshd refuses
    #: root, in which case a key there would authorise a login that cannot
    #: happen.
    accounts: tuple[tuple[str, str], ...]

    def describe(self) -> str:
        who = ", ".join(name for name, _ in self.accounts)
        return f"authorise {len(self.keys)} ssh key(s) for {who}"

    def apply(self, context: Context) -> None:
        body = "".join(f"{key}\n" for key in self.keys)
        for name, home in self.accounts:
            context.write(PurePosixPath(f"{home}/.ssh/authorized_keys"), body, mode=0o600)
            context.run_in_target(["chmod", "700", f"{home}/.ssh"])
            if name != "root":
                context.run_in_target(["chown", "-R", f"{name}:{name}", f"{home}/.ssh"])


@dataclass(frozen=True, kw_only=True)
class WriteSshdConfig(Operation):
    """Whether sshd accepts a password, as a drop-in.

    `50-` so it sorts before the `9999999gentoo-pam.conf` the ebuild installs
    with `PasswordAuthentication no`: sshd takes the first value it reads for a
    keyword, so a later file cannot turn password login back on.
    """

    stage: Stage = Stage.SYSTEM
    password_login: bool
    root_login: bool

    def describe(self) -> str:
        password = "on" if self.password_login else "off"
        return f"ssh password login: {password}, root: {'on' if self.root_login else 'off'}"

    def apply(self, context: Context) -> None:
        answer = "yes" if self.password_login else "no"
        if not self.root_login:
            root = "no"
        else:
            root = "yes" if self.password_login else "prohibit-password"
        # PAM answers the password prompt through keyboard-interactive, so
        # PasswordAuthentication alone leaves it refused.
        lines = [
            f"PasswordAuthentication {answer}",
            f"KbdInteractiveAuthentication {answer}",
            f"PermitRootLogin {root}",
        ]
        context.write(
            PurePosixPath("/etc/ssh/sshd_config.d/50-gentoo-install.conf"),
            "".join(f"{line}\n" for line in lines),
            mode=0o600,
        )


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
    """The service by name, without a suffix: openrc has no `.service`, and a
    unit name handed to `rc-update` fails with the disks already written."""

    stage: Stage = Stage.SYSTEM
    service: str
    init: InitSystem
    runlevel: str = "default"

    def describe(self) -> str:
        return f"enable {self.service} at boot"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.SYSTEMD:
            context.run_in_target(["systemctl", "enable", f"{self.service}.service"])
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
        operations.append(WriteCrypttab(entries=crypttab, init=system.init))
        if system.init is InitSystem.OPENRC and any(
            not entry.initrd_attach for entry in crypttab
        ):
            operations.append(EnableService(service="dmcrypt", init=system.init, runlevel="boot"))
    for user in system.users:
        operations.append(
            CreateUser(
                name=user.name,
                groups=_groups_of(user),
                shell=user.shell,
                password_hash=user.password_hash,
            )
        )
    if system.authorized_keys:
        # After the users: the file lands in a home directory and is chowned to
        # an account, and both have to exist first.
        operations.append(
            WriteAuthorizedKeys(keys=system.authorized_keys, accounts=key_accounts(system))
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
            WriteSshdConfig(
                password_login=system.sshd_password_login,
                root_login=system.sshd_root_login,
            ),
            EnableService(service=_sshd_service(), init=system.init),
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
        WriteNetworkConfig(
            init=system.init,
            networking=system.networking,
            interface=system.interface,
            addresses=system.addresses,
            gateways=system.gateways,
            dns=system.dns,
        ),
    ]
    if system.networking is Networking.NONE:
        # Nothing enabled: an operator who chose no networking gets none, not
        # DHCP from whichever service the init happens to ship.
        pass
    elif _needs_netifrc(system):
        # Not dhcpcd: it would DHCP over the static address, and nothing reads
        # /etc/conf.d/net unless the per-interface service is in a runlevel.
        operations.append(LinkNetifrcService(interface=system.interface or "eth0"))
    else:
        operations.append(
            EnableService(service=_network_service(system), init=system.init)
        )
    operations += _logging(system)
    if system.init is not InitSystem.SYSTEMD:
        # openrc assembles the stack with a service per kind. The root comes up
        # from the initramfs either way; anything else needs these.
        operations += [
            EnableService(service=service, init=system.init, runlevel="boot")
            for kind, service in OPENRC_STORAGE
            if config.disk.graph.of_type(kind)
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


def key_accounts(system: SystemConfig) -> tuple[tuple[str, str], ...]:
    """Which accounts get the authorised keys, and their home directories.

    Every sudo user, plus root unless sshd refuses root and a sudo user exists
    to log in instead: a key that reaches no account leaves a headless machine
    with no way in.
    """
    accounts = [(user.name, f"/home/{user.name}") for user in system.users if user.sudo]
    if system.sshd_root_login or not accounts:
        accounts.insert(0, ("root", "/root"))
    return tuple(accounts)


def _sshd_service() -> str:
    """Both inits call it sshd. A parameter that changes nothing reads as
    though one of them might."""
    return "sshd"


#: What each logger is called as a package and as a service. systemd needs
#: none: journald is part of it.
LOGGERS: Final[dict[Logger, tuple[str, str]]] = {
    Logger.SYSKLOGD: ("app-admin/sysklogd", "sysklogd"),
    Logger.SYSLOG_NG: ("app-admin/syslog-ng", "syslog-ng"),
    Logger.METALOG: ("app-admin/metalog", "metalog"),
}

#: openrc brings up a storage stack with a service per kind; systemd has
#: generators that need none. Service names read from each ebuild's newinitd.
OPENRC_STORAGE: Final[tuple[tuple[type[Node], str], ...]] = (
    (VolumeGroup, "lvm"),
    (MdRaid, "mdraid"),
)


def _logging(system: SystemConfig) -> list[Operation]:
    """A logger and a cron daemon, which openrc has neither of after a stage3.

    systemd carries journald, so a logger there would be a second one writing
    the same lines; `cronie` is the same package on both.
    """
    operations: list[Operation] = []
    named = LOGGERS.get(system.logger)
    if named is not None and system.init is not InitSystem.SYSTEMD:
        atom, service = named
        operations += [
            Emerge(stage=Stage.SYSTEM, packages=(atom,), summary="install the system logger"),
            EnableService(service=service, init=system.init),
        ]
    if system.cron:
        operations += [
            Emerge(
                stage=Stage.SYSTEM, packages=("sys-process/cronie",), summary="install cron"
            ),
            EnableService(service="cronie", init=system.init),
        ]
    return operations


def _needs_netifrc(system: SystemConfig) -> bool:
    """openrc with a static address. `dhcpcd` manages every interface itself,
    so it is enough for DHCP and wrong for anything else."""
    return (
        system.init is InitSystem.OPENRC
        and system.networking is Networking.BUILTIN
        and bool(system.addresses)
    )


def _network_service(system: SystemConfig) -> str:
    """The name both inits use where there is one; the built-in manager is a
    different program on each, so only that case differs."""
    if system.networking in (Networking.NETWORKMANAGER_WPA, Networking.NETWORKMANAGER_IWD):
        return "NetworkManager"
    return "systemd-networkd" if system.init is InitSystem.SYSTEMD else "dhcpcd"


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
