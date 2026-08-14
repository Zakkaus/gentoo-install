"""The target's own settings: locale, time, console, identity, mounts, users."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Final, Mapping
from urllib.parse import urlsplit, urlunsplit

from ..errors import InvalidLayout, LocaleMissing
from ..model import compat
from ..model.config import (
    ConsoleFontSize,
    Firewall,
    InitSystem,
    InstallConfig,
    Logger,
    Networking,
    ProxyConfig,
    SystemConfig,
    User,
)
from ..model.size import Size
from ..model.device import (
    MdRaid,
    Node,
    DeviceId,
    FilesystemType,
    Luks,
    Swap,
    VolumeGroup,
    ZfsPool,
)
from .bootloader import serial_console
from .mounts import resolve_mounts
from .operations import Context, Operation, Stage
from .portage import Emerge, PortageConfigKind, WritePortageConfig

#: Console fonts `sys-apps/kbd` installs, by the cell size they draw.
CONSOLE_FONTS: Final[dict[ConsoleFontSize, str]] = {
    ConsoleFontSize.SIZE_8X8: "lat2-08",
    ConsoleFontSize.SIZE_8X16: "default8x16",
    ConsoleFontSize.SIZE_16X32: "latarcyrheb-sun32",
}

#: Groups a desktop user needs. `wheel` is what sudo is granted through.
USER_GROUPS: Final[tuple[str, ...]] = ("users", "wheel", "audio", "video", "render", "usb", "input")

ROOT = PurePosixPath("/")


class NetworkConfig(Enum):
    NONE = "none"
    NETWORKD = "networkd"
    NETIFRC = "netifrc"
    NETWORKMANAGER = "networkmanager"


class NetworkService(Enum):
    NETWORKD = "systemd-networkd"
    SYSTEMD_RESOLVED = "systemd-resolved"
    DHCPCD = "dhcpcd"
    NETWORKMANAGER = "NetworkManager"
    NETIFRC_INTERFACE = "netifrc-interface"


@dataclass(frozen=True, kw_only=True)
class WriteProxyEnvironment(Operation):
    """Keep the selected route available to clients after the first boot."""

    stage: Stage = Stage.SYSTEM
    proxy: ProxyConfig

    def describe(self) -> str:
        route = self.proxy.redacted_url if self.proxy.enabled else "direct connection"
        return f"keep proxy environment for {route} in the installed system"

    def apply(self, context: Context) -> None:
        endpoint = _proxy_endpoint(self.proxy)
        bypass = ",".join(self.proxy.bypass)
        values = {
            "http_proxy": endpoint,
            "https_proxy": endpoint,
            "ftp_proxy": endpoint,
            "all_proxy": endpoint,
            "no_proxy": bypass,
        }
        environment = "".join(
            f"{key}={json.dumps(value)}\n{key.upper()}={json.dumps(value)}\n"
            for key, value in values.items()
        )
        context.write(PurePosixPath("/etc/environment"), environment)
        profile = "".join(
            f"export {key}={shlex.quote(value)}\n" for key, value in values.items()
        )
        context.write(PurePosixPath("/etc/profile.d/gentoo-install-proxy.sh"), profile)


@dataclass(frozen=True)
class NetworkInitRequirements:
    packages: tuple[str, ...]
    services: tuple[NetworkService, ...]
    config: NetworkConfig
    static_services: tuple[NetworkService, ...] | None = None
    link_resolv_conf: bool = False

    def services_for(self, *, static: bool) -> tuple[NetworkService, ...]:
        if static and self.static_services is not None:
            return self.static_services
        return self.services


@dataclass(frozen=True)
class NetworkBackend:
    systemd: NetworkInitRequirements
    openrc: NetworkInitRequirements
    use: tuple[str, ...] = ()

    def for_init(self, init: InitSystem) -> NetworkInitRequirements:
        return self.systemd if init is InitSystem.SYSTEMD else self.openrc


#: Kept together so a backend cannot change its package without its service.
NETWORK_BACKENDS: Final[Mapping[Networking, NetworkBackend]] = MappingProxyType(
    {
        Networking.BUILTIN: NetworkBackend(
            systemd=NetworkInitRequirements(
                packages=(),
                services=(NetworkService.NETWORKD, NetworkService.SYSTEMD_RESOLVED),
                config=NetworkConfig.NETWORKD,
                link_resolv_conf=True,
            ),
            openrc=NetworkInitRequirements(
                packages=("net-misc/netifrc", "net-misc/dhcpcd"),
                services=(NetworkService.DHCPCD,),
                static_services=(NetworkService.NETIFRC_INTERFACE,),
                config=NetworkConfig.NETIFRC,
            ),
        ),
        Networking.NETWORKMANAGER_WPA: NetworkBackend(
            systemd=NetworkInitRequirements(
                packages=("net-misc/networkmanager", "net-wireless/wpa_supplicant"),
                services=(NetworkService.NETWORKMANAGER,),
                config=NetworkConfig.NETWORKMANAGER,
            ),
            openrc=NetworkInitRequirements(
                packages=("net-misc/networkmanager", "net-wireless/wpa_supplicant"),
                services=(NetworkService.NETWORKMANAGER,),
                config=NetworkConfig.NETWORKMANAGER,
            ),
        ),
        Networking.NETWORKMANAGER_IWD: NetworkBackend(
            systemd=NetworkInitRequirements(
                packages=("net-misc/networkmanager", "net-wireless/iwd"),
                services=(NetworkService.NETWORKMANAGER,),
                config=NetworkConfig.NETWORKMANAGER,
            ),
            openrc=NetworkInitRequirements(
                packages=("net-misc/networkmanager", "net-wireless/iwd"),
                services=(NetworkService.NETWORKMANAGER,),
                config=NetworkConfig.NETWORKMANAGER,
            ),
            use=("net-misc/networkmanager iwd",),
        ),
        Networking.NONE: NetworkBackend(
            systemd=NetworkInitRequirements(
                packages=(), services=(), config=NetworkConfig.NONE
            ),
            openrc=NetworkInitRequirements(
                packages=(), services=(), config=NetworkConfig.NONE
            ),
        ),
    }
)


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
    """Where `LANG` lives differs by init.

    openrc reads none of `/etc/locale.conf`: that is systemd's file. It takes
    `LANG` from `/etc/env.d/02locale`, which `env-update` compiles into
    `/etc/profile.env`, so writing only the systemd file booted openrc under C.
    """

    stage: Stage = Stage.SYSTEM
    locale: str
    init: InitSystem

    def describe(self) -> str:
        return f"set the system locale to {self.locale}"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.SYSTEMD:
            context.write(PurePosixPath("/etc/locale.conf"), f"LANG={self.locale}\n")
            return
        context.write(PurePosixPath("/etc/env.d/02locale"), f'LANG="{self.locale}"\n')
        context.run_in_target(["env-update"])


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


#: Where portage builds. `PORTAGE_TMPDIR` is the parent, and portage makes
#: `portage/` under it, so the tmpfs goes on the directory the build actually
#: fills rather than on all of /var/tmp.
PORTAGE_TMPDIR: Final[PurePosixPath] = PurePosixPath("/var/tmp/portage")


@dataclass(frozen=True, kw_only=True)
class FstabEntry:
    """A line of fstab. The device is still an id here; `apply` turns it into a
    UUID, because a UUID survives the disk being renumbered and `/dev/sda2` does
    not."""

    path: PurePosixPath
    kind: str
    options: tuple[str, ...]
    dump: int
    check: int
    device: DeviceId | None = None
    #: The first field written as it is, for a filesystem that has no device to
    #: take a UUID from. `tmpfs` is the whole source of a tmpfs line.
    source: str = ""


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
            where = (
                entry.source
                if entry.device is None
                else f"UUID={context.device_uuid(entry.device)}"
            )
            lines.append(
                f"{where}\t{_fstab_path(entry.path)}\t{entry.kind}\t"
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
        WritePortageConfig(
            kind=PortageConfigKind.USE,
            name="network",
            lines=self.lines,
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class LinkResolvConf(Operation):
    """Point the target's resolver at the one that will run on it.

    `PrepareChroot` copies the installing system's `/etc/resolv.conf` in so the
    emerges can resolve, and nothing takes it out again: the installed machine
    booted with the live medium's nameservers. `systemd-networkd` publishes
    what it learns only through `systemd-resolved`, so the DNS the operator
    typed reached no program at all.

    Last of all, because the link points at a socket `systemd-resolved` only
    creates once the installed system boots. Written any earlier it takes the
    copied resolver away from every emerge that follows, and the install dies
    on `Temporary failure in name resolution`.
    """

    stage: Stage = Stage.FINISH
    init: InitSystem

    def describe(self) -> str:
        return "point /etc/resolv.conf at systemd-resolved rather than the install medium's"

    def apply(self, context: Context) -> None:
        context.run_in_target(
            [
                "ln", "--symbolic", "--force", "--no-target-directory",
                "../run/systemd/resolve/stub-resolv.conf",
                "/etc/resolv.conf",
            ]
        )


@dataclass(frozen=True, kw_only=True)
class WriteNetworkConfig(Operation):
    """A wired interface, from whichever manager the configuration named.

    systemd-networkd does nothing without a `.network` file, so enabling the
    service alone leaves the system offline. NetworkManager needs no file to
    do DHCP, and needs one to do anything else: a static address given to it
    with no profile written was dropped and the machine came up on DHCP.
    """

    stage: Stage = Stage.SYSTEM
    init: InitSystem
    networking: Networking
    #: Empty matches `en*` and `eth*` on systemd. Static netifrc configurations
    #: are validated to require a name because netifrc has no wildcard.
    interface: str = ""
    #: CIDR, either family. Empty is DHCP and router advertisements.
    addresses: tuple[str, ...] = ()
    gateways: tuple[str, ...] = ()
    dns: tuple[str, ...] = ()

    def describe(self) -> str:
        config = NETWORK_BACKENDS[self.networking].for_init(self.init).config
        if config is NetworkConfig.NONE:
            return "leave the network unconfigured"
        where = self.interface or "the wired interface"
        if config is NetworkConfig.NETWORKMANAGER:
            if not self.addresses:
                return f"leave the interfaces to NetworkManager ({self.networking.value})"
            return f"write a NetworkManager profile for {where} as {', '.join(self.addresses)}"
        if self.addresses:
            return f"configure {where} as {', '.join(self.addresses)}"
        return f"configure {where} for DHCP"

    def apply(self, context: Context) -> None:
        config = NETWORK_BACKENDS[self.networking].for_init(self.init).config
        if config is NetworkConfig.NONE:
            # The operator brings the link up by hand.
            return
        if config is NetworkConfig.NETWORKMANAGER:
            if not self.addresses:
                # NetworkManager does DHCP on every unconfigured interface, so
                # a file saying so is a file that changes nothing.
                return
            # 0600 or NetworkManager refuses to read it, and says so only in
            # its own log while the machine sits with no address.
            context.write(NM_PROFILE, self._networkmanager(), mode=0o600)
            return
        if config is NetworkConfig.NETWORKD:
            context.write(
                PurePosixPath("/etc/systemd/network/20-wired.network"), self._networkd()
            )
            return
        if self.addresses and not self.interface:
            return
        context.write(PurePosixPath("/etc/conf.d/net"), self._netifrc())

    def _networkmanager(self) -> str:
        """A keyfile connection. The gateway rides on the first address of its
        own family, which is the form `nm-settings-keyfile` documents."""
        lines = [
            "[connection]\nid=wired\ntype=ethernet\nautoconnect=true\n",
        ]
        if self.interface:
            lines.append(f"interface-name={self.interface}\n")
        for family, wanted in (("ipv4", self._of(4)), ("ipv6", self._of(6))):
            lines.append(f"\n[{family}]\n")
            if not wanted:
                # `auto` and not `disabled`: a v4-only answer must not switch
                # v6 off, and a v6-only one must not switch v4 off.
                lines.append("method=auto\n")
                continue
            lines.append("method=manual\n")
            gateway = next((one for one in self.gateways if _family(one) == family[-1]), "")
            for index, address in enumerate(wanted, start=1):
                joined = f"{address},{gateway}" if index == 1 and gateway else address
                lines.append(f"address{index}={joined}\n")
            servers = [one for one in self.dns if _family(one) == family[-1]]
            if servers:
                lines.append("dns=" + ";".join(servers) + ";\n")
        return "".join(lines)

    def _of(self, family: int) -> tuple[str, ...]:
        return tuple(one for one in self.addresses if _family(one) == str(family))

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
        if not self.addresses:
            name = self.interface or "eth0"
            return f'config_{name}="dhcp"\n'
        name = self.interface
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
    arrays: tuple[MdRaid, ...]

    def describe(self) -> str:
        paths = ", ".join(f"/dev/md/{array.name}" for array in self.arrays)
        return f"write /etc/mdadm.conf for {paths}"

    def apply(self, context: Context) -> None:
        definitions = "".join(
            f"ARRAY /dev/md/{array.name} metadata={array.metadata.value} "
            f"UUID={context.array_uuid(array.id)}\n"
            for array in self.arrays
        )
        # MAILADDR as well: `mdadm --monitor` exits with an error when it has
        # nobody to alert, leaving a healthy array with a failed unit.
        context.write(PurePosixPath("/etc/mdadm.conf"), f"MAILADDR root\n{definitions}")


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


#: Where the first-boot script goes. Under `/usr/local`, which is the tree the
#: distribution never writes to, so nothing Portage installs collides with it.
FIRST_BOOT_SCRIPT: Final[PurePosixPath] = PurePosixPath(
    "/usr/local/sbin/gentoo-install-firstboot"
)

#: openrc runs every `/etc/local.d/*.start` through the `local` service, which
#: is in the default runlevel already. systemd has no equivalent, so it gets a
#: unit of its own.
FIRST_BOOT_OPENRC: Final[PurePosixPath] = PurePosixPath(
    "/etc/local.d/gentoo-install-firstboot.start"
)
FIRST_BOOT_UNIT: Final[PurePosixPath] = PurePosixPath(
    "/etc/systemd/system/gentoo-install-firstboot.service"
)


@dataclass(frozen=True, kw_only=True)
class WriteFirstBoot(Operation):
    """The script the installed system runs once, and what starts it.

    The remote part is fetched here, while the installer still has a network
    and the operator is still watching. Fetching at first boot instead puts a
    download nobody sees between a machine and being configured.

    Ordered before the bootloader so a failed download stops the run while the
    target is still mounted and the message still reaches the operator.
    """

    stage: Stage = Stage.SYSTEM
    commands: tuple[str, ...]
    url: str
    init: InitSystem

    def describe(self) -> str:
        parts = []
        if self.url:
            parts.append(f"a script from {self.url}")
        if self.commands:
            parts.append(f"{len(self.commands)} commands")
        return f"run {' and '.join(parts)} once, the first time the system boots"

    def apply(self, context: Context) -> None:
        fetched = context.fetch_text(self.url) if self.url else ""
        # `set -e` and a log: this runs with nobody attached, and a step that
        # failed silently is indistinguishable from one that never ran.
        lines = [
            "#!/bin/sh",
            "set -e",
            "exec >>/var/log/gentoo-install-firstboot.log 2>&1",
            "echo \"first boot: $(date -Is)\"",
        ]
        if fetched:
            lines += ["", fetched.rstrip("\n"), ""]
        lines += list(self.commands)
        # Last, and only on success: a script that removes itself before the
        # commands run leaves no way to see what a failure was.
        lines.append(f"rm -f {self._starter()}")
        context.write(FIRST_BOOT_SCRIPT, "\n".join(lines) + "\n", mode=0o700)
        if self.init is InitSystem.SYSTEMD:
            context.write(
                FIRST_BOOT_UNIT,
                "[Unit]\n"
                "Description=gentoo-install first boot\n"
                f"ConditionPathExists={FIRST_BOOT_SCRIPT}\n"
                "After=network-online.target\n"
                "Wants=network-online.target\n"
                "\n[Service]\n"
                "Type=oneshot\n"
                f"ExecStart={FIRST_BOOT_SCRIPT}\n"
                "\n[Install]\n"
                "WantedBy=multi-user.target\n",
            )
        else:
            context.write(
                FIRST_BOOT_OPENRC,
                f"#!/bin/sh\n[ -x {FIRST_BOOT_SCRIPT} ] && {FIRST_BOOT_SCRIPT}\n",
                mode=0o755,
            )

    def _starter(self) -> PurePosixPath:
        """What the script deletes so it does not run again. The starter, not
        itself: the log beside it names what ran, and a second boot finding no
        starter is how `ConditionPathExists` and the openrc guard both stop."""
        return FIRST_BOOT_SCRIPT


@dataclass(frozen=True, kw_only=True)
class GenerateHostKeys(Operation):
    """Create the target's ssh host keys while the installer can still see it.

    `net-misc/openssh` makes none at merge time; sshd makes them the first time
    it starts, which is after this install has ended. `dracut-crypt-ssh` reads
    them at initramfs build time to convert into dropbear's format, so with
    none the remote-unlock daemon comes up with a key the operator's client has
    never seen and refuses the host.
    """

    stage: Stage = Stage.SYSTEM
    remote_unlock: bool = False

    def describe(self) -> str:
        wanted = " so the initramfs and sshd present the same host" if self.remote_unlock else ""
        return f"generate the ssh host keys{wanted}"

    def apply(self, context: Context) -> None:
        # `-A` makes only what is missing, so a resumed run leaves the keys a
        # previous pass created and the client's known_hosts stays right.
        context.run_in_target(["ssh-keygen", "-A"])


@dataclass(frozen=True, kw_only=True)
class EnableService(Operation):
    """The service by name, without a suffix: openrc has no `.service`, and a
    unit name handed to `rc-update` fails with the disks already written."""

    stage: Stage = Stage.SYSTEM
    service: str
    init: InitSystem
    runlevel: str = "default"

    def describe(self) -> str:
        if self.init is InitSystem.SYSTEMD:
            return f"enable {self.service} at boot"
        return f"enable {self.service} in the {self.runlevel} runlevel"

    def apply(self, context: Context) -> None:
        if self.init is InitSystem.SYSTEMD:
            # A name that already carries its unit suffix is passed through:
            # `zfs.target.service` is not a unit and `systemctl enable` fails.
            unit = self.service if "." in self.service else f"{self.service}.service"
            context.run_in_target(["systemctl", "enable", unit])
        else:
            context.run_in_target(["rc-update", "add", self.service, self.runlevel])


def build(config: InstallConfig) -> list[Operation]:
    system = config.system
    operations: list[Operation] = [
        WriteProxyEnvironment(proxy=config.proxy),
        GenerateLocales(locales=system.locales),
        SelectLocale(locale=system.locale, init=system.init),
        SetTimezone(timezone=system.timezone),
        ConfigureConsole(
            keymap=system.keymap, font=CONSOLE_FONTS[system.console_font], init=system.init
        ),
        SetHostname(hostname=system.hostname, init=system.init),
        WriteMachineId(init=system.init),
        WriteFstab(entries=fstab_entries(config)),
        SetHardwareClock(utc=system.hardware_clock_utc, init=system.init),
    ]
    if system.first_boot.wanted:
        operations.append(
            WriteFirstBoot(
                commands=system.first_boot.commands,
                url=system.first_boot.url,
                init=system.init,
            )
        )
        if system.init is InitSystem.SYSTEMD:
            operations.append(
                EnableService(service="gentoo-install-firstboot", init=system.init)
            )
        else:
            # `local` runs every /etc/local.d/*.start. It is in the default
            # runlevel on a stage3 already; adding it again is what makes that
            # true rather than assumed.
            operations.append(EnableService(service="local", init=system.init))
    arrays = config.disk.graph.of_type(MdRaid)
    if arrays:
        operations.append(WriteMdadmConf(arrays=arrays))
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
            operations.append(
                EnableService(
                    stage=STORAGE_SERVICE_STAGE,
                    service="dmcrypt",
                    init=system.init,
                    runlevel="boot",
                )
            )
    for user in system.users:
        operations.append(
            CreateUser(
                name=user.name,
                groups=_groups_of(user),
                shell=user.shell,
                password_hash=user.password_hash,
            )
        )
    unlocking = config.kernel.remote_unlock.enabled
    if system.authorized_keys:
        # After the users: the file lands in a home directory and is chowned to
        # an account, and both have to exist first.
        operations.append(
            WriteAuthorizedKeys(
                keys=system.authorized_keys, accounts=key_accounts(system, unlocking)
            )
        )
    serial = serial_console(config)
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
    if system.sshd or unlocking:
        # The host keys are an initramfs prerequisite, not an sshd feature:
        # `dropbear_*_key="SYSTEM"` converts the target's own keys, and with
        # sshd switched off there were none to convert and the machine stayed
        # locked. The package comes with them and no service is enabled.
        if not system.sshd:
            operations.append(
                Emerge(
                    stage=Stage.SYSTEM,
                    packages=("net-misc/openssh",),
                    summary="install the host keys the initramfs unlock daemon converts",
                )
            )
        # Before Stage.KERNEL, where dracut runs: dracut-crypt-ssh converts
        # these into dropbear's format and there is nothing to convert if
        # sshd has not been started yet, which it has not.
        operations.append(GenerateHostKeys(remote_unlock=unlocking))
    backend = NETWORK_BACKENDS[system.networking]
    requirements = backend.for_init(system.init)
    if backend.use:
        operations.append(RequestNetworkUse(lines=backend.use))
    if requirements.packages:
        operations.append(
            Emerge(
                stage=Stage.SYSTEM,
                packages=requirements.packages,
                summary="install the network tools",
            )
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
    for network_service in requirements.services_for(static=bool(system.addresses)):
        if network_service is NetworkService.NETIFRC_INTERFACE:
            # dhcpcd would DHCP over the static address, while netifrc needs an
            # interface service before it reads /etc/conf.d/net.
            if system.interface:
                operations.append(LinkNetifrcService(interface=system.interface))
        else:
            operations.append(EnableService(service=network_service.value, init=system.init))
    if requirements.link_resolv_conf:
        operations.append(LinkResolvConf(init=system.init))
    operations += _logging(system)
    operations += _firewall(system)
    if system.init is not InitSystem.SYSTEMD:
        # openrc assembles the stack with a service per kind. The root comes up
        # from the initramfs either way; anything else needs these.
        operations += [
            EnableService(
                stage=STORAGE_SERVICE_STAGE, service=service, init=system.init, runlevel="boot"
            )
            for kind, service in OPENRC_STORAGE
            if config.disk.graph.of_type(kind)
        ]
    operations += _zfs_services(config)
    return operations


def _proxy_endpoint(proxy: ProxyConfig) -> str:
    """The proxy URL without user information, for process environments."""
    if not proxy.url:
        return ""
    parts = urlsplit(proxy.url)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, "", "", ""))


def _zfs_services(config: InstallConfig) -> list[Operation]:
    """A pool imports and its datasets mount only if something does it.

    The initramfs brings up the root dataset and nothing else, so `/home` on its
    own dataset came up empty on a system that had never enabled a ZFS service.
    """
    pools = config.disk.graph.of_type(ZfsPool)
    if not pools:
        return []
    init = config.system.init
    operations: list[Operation] = [
        EnableService(stage=STORAGE_SERVICE_STAGE, service=service, init=init, runlevel=runlevel)
        for service, runlevel in ZFS_SERVICES[init]
    ]
    if init is InitSystem.OPENRC and any(pool.passphrase_file for pool in pools):
        operations.insert(
            1,
            EnableService(
                stage=STORAGE_SERVICE_STAGE,
                service=ZFS_KEY_SERVICE,
                init=init,
                runlevel="boot",
            ),
        )
    return operations


def fstab_entries(config: InstallConfig) -> tuple[FstabEntry, ...]:
    graph = config.disk.graph
    entries: list[FstabEntry] = []
    for mount in resolve_mounts(graph):
        if mount.dataset is not None:
            # A dataset carries its mountpoint property; fstab would fight it.
            continue
        if mount.device is None or mount.filesystem_kind is None:
            raise InvalidLayout(f"mountpoint {mount.mountpoint!r} has no resolved filesystem")
        entries.append(
            FstabEntry(
                device=mount.device,
                path=mount.path,
                kind=mount.filesystem_kind.value,
                options=_options(mount.filesystem_kind, mount.options),
                dump=0,
                check=_check_order(mount.path),
            )
        )
    if config.portage.build_in_ram is not None:
        # Exactly the line a Gentoo desktop carries for this: portage is 250:250
        # and needs to write into it, and nothing under it is ever executed or
        # a device node.
        entries.append(
            FstabEntry(
                source="tmpfs",
                path=PORTAGE_TMPDIR,
                kind="tmpfs",
                options=(
                    f"size={config.portage.build_in_ram.single_letter()}",
                    "uid=250",
                    "gid=250",
                    "mode=0775",
                    "noatime",
                    "nodev",
                    "nosuid",
                ),
                dump=0,
                check=0,
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


def _fstab_path(path: PurePosixPath) -> str:
    return str(path).replace("\t", r"\011").replace(" ", r"\040")


def _groups_of(user: User) -> tuple[str, ...]:
    groups = list(USER_GROUPS if user.sudo else [group for group in USER_GROUPS if group != "wheel"])
    for group in user.groups:
        if group not in groups:
            groups.append(group)
    return tuple(groups)


def key_accounts(system: SystemConfig, unlocking: bool = False) -> tuple[tuple[str, str], ...]:
    """Which accounts get the authorised keys, and their home directories.

    Every sudo user, plus root unless sshd refuses root and a sudo user exists
    to log in instead: a key that reaches no account leaves a headless machine
    with no way in.

    `unlocking` always adds root: dracut-crypt-ssh reads
    `/root/.ssh/authorized_keys` and nothing else, so a machine whose only key
    went to a sudo user stayed locked before that account existed. The booted
    system keeps `PermitRootLogin no` either way.
    """
    accounts = [(user.name, f"/home/{user.name}") for user in system.users if user.sudo]
    if system.sshd_root_login or not accounts or unlocking:
        accounts.insert(0, ("root", "/root"))
    return tuple(accounts)


def _sshd_service() -> str:
    """Both inits call it sshd. A parameter that changes nothing reads as
    though one of them might."""
    return "sshd"


#: What each logger is called as a package and as a service. systemd needs
#: none: journald is part of it.
@dataclass(frozen=True)
class LoggerChoice:
    """One logger: what to merge, what to enable, and what the row says."""

    package: str
    service: str
    #: The source string the menu shows, so a logger cannot be offered without
    #: a package or merged without a row.
    reason: str


LOGGERS: Final[dict[Logger, LoggerChoice]] = {
    Logger.NONE: LoggerChoice("", "", "no system log at all"),
    Logger.SYSKLOGD: LoggerChoice(
        "app-admin/sysklogd", "sysklogd", "what the handbook installs"
    ),
    Logger.SYSLOG_NG: LoggerChoice(
        "app-admin/syslog-ng", "syslog-ng", "filters and remote destinations"
    ),
    Logger.METALOG: LoggerChoice("app-admin/metalog", "metalog", "smaller, no remote logging"),
}

#: Where NetworkManager reads a keyfile connection from. One file, because
#: the installer configures one wired interface and nothing else.
NM_PROFILE: Final[PurePosixPath] = PurePosixPath(
    "/etc/NetworkManager/system-connections/wired.nmconnection"
)


def _family(address: str) -> str:
    """`6` for anything holding a colon, `4` otherwise. Enough to sort an
    address list: the two families never share a separator."""
    return "6" if ":" in address else "4"


#: openrc brings up a storage stack with a service per kind; systemd has
#: generators that need none. Service names read from each ebuild's newinitd.
OPENRC_STORAGE: Final[tuple[tuple[type[Node], str], ...]] = (
    (VolumeGroup, "lvm"),
    (MdRaid, "mdraid"),
)

#: `sys-fs/lvm2`, `sys-fs/mdadm` and `sys-fs/cryptsetup` are merged with the
#: kernel stack, and `rc-update` refuses a service whose package is absent.
STORAGE_SERVICE_STAGE: Final[Stage] = Stage.PACKAGES

#: What each init needs enabled before a pool imports and its datasets mount.
#: `-scan` rather than `-cache`: the install bakes no `zpool.cache`, and the
#: cache service with no cache to read imports nothing. `zfs-mount.service` is
#: `WantedBy=zfs.target`, so the target has to be enabled as well.
ZFS_SERVICES: Final[dict[InitSystem, tuple[tuple[str, str], ...]]] = {
    InitSystem.SYSTEMD: (
        ("zfs-import-scan.service", "default"),
        ("zfs-mount.service", "default"),
        ("zfs.target", "default"),
    ),
    InitSystem.OPENRC: (("zfs-import", "boot"), ("zfs-mount", "boot")),
}

#: openrc unlocks a dataset the initramfs did not, between import and mount.
#: systemd has no equivalent to enable: `zfs-mount-generator` writes a unit per
#: pool at boot.
ZFS_KEY_SERVICE: Final[str] = "zfs-load-key"


#: The package each choice merges. Both ship an empty rule set and neither
#: service is enabled here, so nothing about the machine's reachability
#: changes: the operator writes the policy after the install.
FIREWALLS: Final[dict[Firewall, str]] = {
    Firewall.NONE: "",
    Firewall.NFTABLES: "net-firewall/nftables",
    Firewall.IPTABLES: "net-firewall/iptables",
}


def _firewall(system: SystemConfig) -> list[Operation]:
    """Merge the packet filter and stop there.

    No `EnableService` and no rule file. A rule set written by the installer
    is a security policy nobody asked for, and one that closes port 22 on a
    machine reached only over ssh needs a console to undo.
    """
    package = FIREWALLS[system.firewall]
    if not package:
        return []
    return [
        Emerge(stage=Stage.SYSTEM, packages=(package,), summary="install the firewall package")
    ]


def _logging(system: SystemConfig) -> list[Operation]:
    """A logger and a cron daemon, which openrc has neither of after a stage3.

    systemd carries journald, so a logger there would be a second one writing
    the same lines; `cronie` is the same package on both.
    """
    operations: list[Operation] = []
    named = LOGGERS[system.logger]
    if named.package and system.init is not InitSystem.SYSTEMD:
        operations += [
            Emerge(
                stage=Stage.SYSTEM, packages=(named.package,), summary="install the system logger"
            ),
            EnableService(service=named.service, init=system.init),
        ]
    if system.cron:
        operations += [
            Emerge(
                stage=Stage.SYSTEM, packages=("sys-process/cronie",), summary="install cron"
            ),
            EnableService(service="cronie", init=system.init),
        ]
    return operations


def _network_service(system: SystemConfig) -> str:
    """The VM runner reports this name without building the whole plan."""
    services = NETWORK_BACKENDS[system.networking].for_init(system.init).services
    return services[0].value if services else ""


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
