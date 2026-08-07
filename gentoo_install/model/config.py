"""The whole installation, as data.

`parse.py` builds this from a TOML file and the TUI builds it from answers; both
produce the same object, and `plan.build()` accepts nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .device import DeviceGraph, DeviceId

#: `parse.py` refuses a newer file and migrates an older one.
CONFIG_VERSION = 1


class InitSystem(Enum):
    OPENRC = "openrc"
    SYSTEMD = "systemd"


class KernelSource(Enum):
    DIST_BIN = "dist-bin"
    DIST_SOURCE = "dist-source"
    CJK_SOURCE = "cjk-source"


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


class BinhostChannel(Enum):
    OFF = "off"
    STABLE = "stable"
    UNSTABLE = "unstable"


class MirrorRegion(Enum):
    CN = "cn"
    GLOBAL = "global"


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
class SystemConfig:
    hostname: str = "gentoo"
    timezone: str = "Asia/Shanghai"
    locales: tuple[str, ...] = ("en_US.UTF-8", "zh_CN.UTF-8", "zh_TW.UTF-8")
    locale: str = "zh_CN.UTF-8"
    #: There is no `cn` keymap; a Chinese locale does not imply one.
    keymap: str = "us"
    #: The local console renders CJK itself, which needs a kernel carrying cjktty.
    console_cjk: bool = False
    console_font: ConsoleFontSize = ConsoleFontSize.SIZE_8X16
    init: InitSystem = InitSystem.SYSTEMD
    users: tuple[User, ...] = ()
    #: Empty locks root, which is what a system with a sudo user wants.
    root_password_hash: str = ""
    sshd: bool = False


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


@dataclass(frozen=True)
class Binhost:
    official: bool = True
    community: BinhostChannel = BinhostChannel.OFF


@dataclass(frozen=True)
class PortageConfig:
    profile: str = "default/linux/amd64/23.0"
    keywords: Keywords = Keywords.STABLE
    makeopts: str = ""
    common_flags: str = "-O2 -pipe"
    use: tuple[str, ...] = ()
    video_cards: tuple[str, ...] = ()
    accept_license: tuple[str, ...] = ("@FREE",)
    mirrors: MirrorConfig = field(default_factory=MirrorConfig)
    binhost: Binhost = field(default_factory=Binhost)
    overlays: tuple[Overlay, ...] = ()


@dataclass(frozen=True)
class KernelConfig:
    source: KernelSource = KernelSource.DIST_BIN
    #: Overrides the package the source implies, for a sources package this
    #: installer does not name itself, such as one from another overlay.
    package: str = ""
    #: Added to the ones the disk layout implies.
    dracut_modules: tuple[str, ...] = ()


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
    #: Merged verbatim after everything else.
    extra: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallConfig:
    disk: DiskConfig
    system: SystemConfig = field(default_factory=SystemConfig)
    portage: PortageConfig = field(default_factory=PortageConfig)
    kernel: KernelConfig = field(default_factory=KernelConfig)
    bootloader: BootloaderConfig = field(default_factory=BootloaderConfig)
    packages: PackagesConfig = field(default_factory=PackagesConfig)
    config_version: int = CONFIG_VERSION
