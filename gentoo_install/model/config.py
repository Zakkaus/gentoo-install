"""The whole installation, as data.

`parse.py` builds this from a TOML file and the TUI builds it from answers; both
produce the same object, and `plan.build()` accepts nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from .size import Size
from .device import DeviceGraph, DeviceId

#: `parse.py` refuses a newer file and migrates an older one.
CONFIG_VERSION: Final[int] = 1

#: The scalar written before every table section.
CONFIG_VERSION_KEY: Final[str] = "config_version"

#: Persisted table sections in TOML order. The disk graph stays last.
PERSISTED_SECTIONS: Final[tuple[str, ...]] = (
    "proxy",
    "system",
    "portage",
    "kernel",
    "bootloader",
    "packages",
    "disk",
)


class InitSystem(Enum):
    OPENRC = "openrc"
    SYSTEMD = "systemd"


class ProxyKind(Enum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS5 = "socks5"


@dataclass(frozen=True)
class ProxyConfig:
    """The optional proxy used by installer and target network clients.

    The endpoint fields are persisted separately. ``url`` is derived for tools
    that still require one endpoint string.
    """

    kind: ProxyKind = ProxyKind.HTTP
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    bypass: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    @property
    def over_socks(self) -> bool:
        """Whether the fetchers have to be ones that speak SOCKS.

        `validate.py` names the four schemes an operator may write; this
        divides them, so a caller never carries its own set of scheme names.
        """
        return self.enabled and self.kind is ProxyKind.SOCKS5

    @property
    def over_http(self) -> bool:
        return self.enabled and self.kind is not ProxyKind.SOCKS5

    @property
    def url(self) -> str:
        """The endpoint URL; SOCKS5 uses proxy-side DNS for intranet names."""
        from urllib.parse import quote

        if not self.host:
            return ""
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        userinfo = ""
        if self.username or self.password:
            userinfo = quote(self.username, safe="")
            if self.password:
                userinfo += f":{quote(self.password, safe='')}"
            userinfo += "@"
        scheme = "socks5h" if self.kind is ProxyKind.SOCKS5 else self.kind.value
        return f"{scheme}://{userinfo}{host}:{self.port}"

    @property
    def redacted_url(self) -> str:
        """The derived URL with user information removed for display and logs."""
        if not self.enabled:
            return ""
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        scheme = "socks5h" if self.kind is ProxyKind.SOCKS5 else self.kind.value
        return f"{scheme}://{host}:{self.port}"


class KernelSource(Enum):
    """Every choice is a dist-kernel: the package builds and installs itself.

    The installer configures no kernel of its own, so a `-sources` package
    would unpack a tree nothing ever compiles.
    """

    DIST_BIN = "dist-bin"
    DIST_SOURCE = "dist-source"
    CJK_BIN = "cjk-bin"
    CJK = "cjk"


class Bootloader(Enum):
    GRUB = "grub"
    SYSTEMD_BOOT = "systemd-boot"
    ZFSBOOTMENU = "zfsbootmenu"


class Firmware(Enum):
    UEFI = "uefi"
    BIOS = "bios"


class Keywords(Enum):
    STABLE = "stable"
    TESTING = "testing"


class Networking(Enum):
    """How the installed system brings a link up.

    `BUILTIN` is whatever the init already has: systemd-networkd on systemd,
    netifrc on openrc. The rest install a manager, and the two NetworkManager
    entries differ only in which supplicant drives wifi.
    """

    BUILTIN = "builtin"
    NETWORKMANAGER_WPA = "networkmanager-wpa"
    NETWORKMANAGER_IWD = "networkmanager-iwd"
    NONE = "none"


class Sync(Enum):
    """How the ebuild repository is kept up to date after the first sync.

    The first one is always webrsync whichever this is: a stage3 has no
    `dev-vcs/git`, so a git sync cannot be the step that installs it.
    """

    GIT = "git"
    WEBRSYNC = "webrsync"
    RSYNC = "rsync"


class BinhostChannel(Enum):
    OFF = "off"
    STABLE = "stable"
    UNSTABLE = "unstable"


class MirrorRegion(Enum):
    CN = "cn"
    GLOBAL = "global"


class GentooZhMirror(Enum):
    """Which site gentoo-zh is fetched from. Separate from `MirrorRegion`: the
    two repositories hold different files and do not share a mirror set."""

    UPSTREAM = "upstream"
    CERNET = "cernet"
    NJU = "nju"
    NYIST = "nyist"
    HA = "ha"


class Firewall(Enum):
    """Which packet filter is installed. Nothing is enabled and no rule is
    written: an empty rule set is what both of these ship, and choosing an
    operator's policy for them is not the installer's decision to make."""

    NONE = "none"
    NFTABLES = "nftables"
    IPTABLES = "iptables"


class Logger(Enum):
    """What writes the system log.

    systemd has journald built in, so `NONE` is the answer there. An openrc
    system with `NONE` keeps no log at all, which is a choice and not a
    default: the handbook names three and the installer offers those three.
    """

    NONE = "none"
    SYSKLOGD = "sysklogd"
    SYSLOG_NG = "syslog-ng"
    METALOG = "metalog"


class ConsoleFontSize(Enum):
    """Cell size of the console font. Every size here is one `sys-apps/kbd`
    ships a font for, so the choice never names a font the target lacks."""

    SIZE_8X8 = "8x8"
    SIZE_8X16 = "8x16"
    SIZE_16X32 = "16x32"


@dataclass(frozen=True)
class User:
    name: str
    groups: tuple[str, ...] = ()
    shell: str = "/bin/bash"
    sudo: bool = False
    #: A crypt(3) hash. Empty locks the account; a plaintext password is never
    #: carried in a configuration file.
    password_hash: str = ""


@dataclass(frozen=True)
class FirstBoot:
    """What the installed system runs once, the first time it boots.

    The script behind `url` is fetched while the installer still has a network
    and written into the target, not fetched at first boot: a download that
    fails then leaves a machine half-configured with nobody watching, and the
    operator cannot see beforehand what is about to run as root.
    """

    #: Shell lines, run in order after the fetched script.
    commands: tuple[str, ...] = ()
    #: Where to fetch a script from. Empty for none.
    url: str = ""

    @property
    def wanted(self) -> bool:
        return bool(self.commands or self.url)


@dataclass(frozen=True)
class SystemConfig:
    hostname: str = "gentoo"
    timezone: str = "Asia/Shanghai"
    locales: tuple[str, ...] = ("en_US.UTF-8", "zh_CN.UTF-8", "zh_TW.UTF-8")
    locale: str = "zh_CN.UTF-8"
    #: There is no `cn` keymap; a Chinese locale does not imply one.
    keymap: str = "us"
    #: The keymap the initramfs uses. Empty follows `keymap`. It matters on its
    #: own because an encrypted root asks for its passphrase there, and a
    #: keyboard that is not us cannot type one it was never told about.
    keymap_initramfs: str = ""
    #: The interface to configure. Empty matches `en*` and `eth*`, which is
    #: what a machine with one wired card wants.
    interface: str = ""
    #: Static addresses in CIDR form, either family, such as `192.0.2.10/24`
    #: and `2001:db8::2/64`. Empty is DHCP and router advertisements.
    addresses: tuple[str, ...] = ()
    #: One per family at most. A v6 gateway is often `fe80::1`.
    gateways: tuple[str, ...] = ()
    #: Resolvers. A static address with none of these boots with an address and
    #: no way to resolve a name.
    dns: tuple[str, ...] = ()
    #: Public keys for root and for every sudo user. One list: the operator
    #: authorises a person, not an account.
    authorized_keys: tuple[str, ...] = ()
    #: The local console renders CJK itself, which needs a kernel carrying cjktty.
    console_cjk: bool = False
    console_font: ConsoleFontSize = ConsoleFontSize.SIZE_8X16
    init: InitSystem = InitSystem.SYSTEMD
    #: Compressed swap in memory. None leaves it off; a size is what the device
    #: is given, which is a ceiling and not an allocation.
    zram: Size | None = None
    #: What the RTC holds. Dual-booting Windows is the reason to say false.
    hardware_clock_utc: bool = True
    users: tuple[User, ...] = ()
    #: Empty locks root, which is what a system with a sudo user wants.
    root_password_hash: str = ""
    #: openrc has none until one is installed; systemd needs none.
    logger: Logger = Logger.SYSKLOGD
    #: `sys-process/cronie`, which both inits use. Off leaves no cron at all.
    cron: bool = True
    sshd: bool = False
    #: Whether sshd accepts a password. Off means keys only, which is what the
    #: shipped configuration already does.
    sshd_password_login: bool = False
    #: Whether root may log in over ssh at all. Off by default and off with a
    #: key too: `key_accounts` puts the keys on root anyway when no sudo user
    #: exists, so refusing root here costs a headless machine nothing.
    sshd_root_login: bool = False
    networking: Networking = Networking.BUILTIN
    #: The package only. No service is enabled and no rule is written, so a
    #: machine reachable over ssh stays reachable until its operator says
    #: otherwise.
    firewall: Firewall = Firewall.NONE
    first_boot: FirstBoot = field(default_factory=FirstBoot)


@dataclass(frozen=True)
class Overlay:
    name: str
    sync_uri: str


@dataclass(frozen=True)
class MirrorConfig:
    region: MirrorRegion = MirrorRegion.GLOBAL
    speed_test: bool = False
    #: Replaces the built-in list when non-empty.
    distfiles: tuple[str, ...] = ()
    repo_sync_uri: str = ""
    #: Which site of the region, by its key in `model/mirrors.py`. Empty takes
    #: the region's first.
    site: str = ""
    #: Whether GENTOO_MIRRORS is written at all. Off leaves Portage on its own
    #: built-in list, which is the right answer behind a caching proxy.
    gentoo_distfiles: bool = True
    #: Which gentoo-zh mirror, chosen apart from the region above.
    gentoo_zh: GentooZhMirror = GentooZhMirror.UPSTREAM
    #: Whether its distfiles are appended to GENTOO_MIRRORS. On: no main mirror
    #: carries the overlay's sources, and appending costs nothing when the
    #: overlay is not selected.
    gentoo_zh_distfiles: bool = True


@dataclass(frozen=True)
class Binhost:
    official: bool = True
    #: The official host's subarchitecture. `x86-64-v3` needs AVX2 and the two
    #: are the only ones with a useful number of packages; gentoo-zh builds
    #: `x86-64` only, so this does not touch it.
    subarch: str = "x86-64"
    community: BinhostChannel = BinhostChannel.OFF


@dataclass(frozen=True)
class PortageConfig:
    #: Matches the default init. A systemd profile has `systemd` as a path
    #: component; the two disagreeing leaves packages built for the other.
    profile: str = "default/linux/amd64/23.0/systemd"
    keywords: Keywords = Keywords.STABLE
    #: git by default: it carries the history a `verify-commit` sync checks,
    #: and it is what an ongoing system uses.
    sync: Sync = Sync.GIT
    #: Atoms accepted as testing while the rest of the system stays stable.
    testing_packages: tuple[str, ...] = ()
    makeopts: str = ""
    common_flags: str = "-O2 -pipe"
    use: tuple[str, ...] = ()
    video_cards: tuple[str, ...] = ()
    #: Empty derives L10N from the generated locales.
    l10n: tuple[str, ...] = ()
    #: `INPUT_DEVICES`. libinput is what every current desktop reads; the
    #: profile's own value is replaced outright by make.conf, so an empty
    #: tuple here would leave a machine with no pointer driver.
    input_devices: tuple[str, ...] = ("libinput",)
    accept_license: tuple[str, ...] = ("@FREE",)
    #: Detected from /proc/cpuinfo when the interface fills it in. Empty means
    #: the profile's own value stands.
    cpu_flags: tuple[str, ...] = ()
    #: A tmpfs over /var/tmp/portage of this size, or None to build on disk.
    #: Off by default: a build that outgrows the tmpfs fails on ENOSPC, and how
    #: much a machine can spare is not derivable from how much it has.
    build_in_ram: Size | None = None
    mirrors: MirrorConfig = field(default_factory=MirrorConfig)
    binhost: Binhost = field(default_factory=Binhost)
    overlays: tuple[Overlay, ...] = ()


@dataclass(frozen=True)
class RemoteUnlock:
    """Unlocking an encrypted root over ssh, from the initramfs.

    Off unless `enabled`. The authorised keys are the ones in
    `SystemConfig.authorized_keys`: dracut-crypt-ssh reads
    `/root/.ssh/authorized_keys` by default and that is where they are written,
    so a machine with none has an ssh daemon nobody can log into.
    """

    enabled: bool = False
    #: 222 is the module's own default. Kept off 22 so a client's known_hosts
    #: entry for the running system does not collide with the initramfs one.
    port: int = 222
    #: The address in CIDR, or empty for DHCP. Three fields rather than
    #: dracut's seven-colon `ip=`: an initramfs with an address and no gateway
    #: answers only its own subnet, and one with no interface named picks
    #: whichever came up first.
    address: str = ""
    gateway: str = ""
    interface: str = ""


@dataclass(frozen=True)
class KernelConfig:
    source: KernelSource = KernelSource.DIST_BIN
    #: Overrides the package the source implies, for one this installer does
    #: not name itself, such as a kernel from another overlay.
    package: str = ""
    #: Pins the version. Empty leaves the choice to Portage, which takes the
    #: newest the keywords allow.
    version: str = ""
    #: Added to the ones the disk layout implies.
    dracut_modules: tuple[str, ...] = ()
    remote_unlock: RemoteUnlock = field(default_factory=RemoteUnlock)


@dataclass(frozen=True)
class BootloaderConfig:
    kind: Bootloader = Bootloader.GRUB
    firmware: Firmware = Firmware.UEFI
    kernel_params: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiskConfig:
    graph: DeviceGraph
    #: Named explicitly: a malformed graph can hold two mountpoints for `/`.
    root: DeviceId


@dataclass(frozen=True)
class PackagesConfig:
    #: Name under `data/profiles/`, empty for no desktop.
    desktop: str = ""
    #: Group names under `data/packages/`.
    applications: tuple[str, ...] = ()
    #: The graphics driver groups. More than one because a hybrid machine has
    #: more than one adapter: an AMD laptop with an NVIDIA card needs
    #: `amdgpu radeonsi nvidia` in VIDEO_CARDS, and one group names two of
    #: those. Empty for whatever the kernel picks.
    graphics: tuple[str, ...] = ()
    #: The display manager group, empty for a console login.
    display_manager: str = ""
    #: Merged verbatim after everything else.
    extra: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallConfig:
    disk: DiskConfig
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    portage: PortageConfig = field(default_factory=PortageConfig)
    kernel: KernelConfig = field(default_factory=KernelConfig)
    bootloader: BootloaderConfig = field(default_factory=BootloaderConfig)
    packages: PackagesConfig = field(default_factory=PackagesConfig)
    config_version: int = CONFIG_VERSION
