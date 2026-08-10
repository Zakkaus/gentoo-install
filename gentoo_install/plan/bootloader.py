"""GRUB, systemd-boot and ZFSBootMenu.

A ZFS root gets no GRUB artefacts at all. The Live ISO's Calamares run installs
GRUB and then deletes what it wrote, because its module order cannot be changed;
this installer decides before it writes, so nothing has to be undone.
"""

from __future__ import annotations

import ipaddress

import re

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from ..errors import NothingToBoot
from ..model import compat
from ..model.config import Bootloader, Firmware, InitSystem, InstallConfig, RemoteUnlock
from ..model.device import (
    Existing,
    DeviceId,
    MdRaid,
    Mountpoint,
    Partition,
    PartitionRole,
    ZfsDataset,
    ZfsPool,
)
from .operations import Context, Operation, Stage
from .portage import Emerge

BOOTLOADER_PACKAGES: Final[dict[Bootloader, tuple[str, ...]]] = {
    Bootloader.GRUB: ("sys-boot/grub",),
    Bootloader.SYSTEMD_BOOT: (),
    Bootloader.ZFSBOOTMENU: ("sys-boot/zfsbootmenu",),
}

#: `bootctl` comes from systemd on a systemd system and from systemd-utils on
#: an openrc one, and the two block each other, so the init decides which.
BOOTCTL_PACKAGE: Final[dict[InitSystem, str]] = {
    InitSystem.SYSTEMD: "sys-apps/systemd",
    InitSystem.OPENRC: "sys-apps/systemd-utils",
}

#: Writing an NVRAM entry needs this, and only UEFI has NVRAM to write to.
EFI_PACKAGE: Final[str] = "sys-boot/efibootmgr"

#: Where generate-zbm writes, and the path firmware boots with no NVRAM entry.
#: It names the image after the kernel, so the name is looked up, not assumed.
ZBM_DIRECTORY: Final[str] = "EFI/zbm"
FALLBACK_IMAGE: Final[str] = "EFI/BOOT/BOOTX64.EFI"


@dataclass(frozen=True, kw_only=True)
class InstallGrub(Operation):
    stage: Stage = Stage.BOOTLOADER
    firmware: Firmware
    esp: PurePosixPath | None
    #: Whose disk GRUB is written to on a BIOS machine: the one the root is on.
    boot_device: DeviceId

    def describe(self) -> str:
        where = f"the esp at {self.esp}" if self.esp is not None else "the boot disk"
        return f"install GRUB for {self.firmware.value} on {where}"

    def apply(self, context: Context) -> None:
        if self.firmware is Firmware.UEFI and self.esp is not None:
            efi = ["grub-install", "--target=x86_64-efi", f"--efi-directory={self.esp}"]
            context.run_in_target([*efi, "--bootloader-id=Gentoo"])
            # Also as the removable-media path: firmware that loses its NVRAM
            # entry, and every firmware that never had one, boots only that.
            context.run_in_target([*efi, "--removable"])
        else:
            context.run_in_target(
                ["grub-install", "--target=i386-pc", context.containing_disk(self.boot_device)]
            )
        context.run_in_target(["grub-mkconfig", "--output", "/boot/grub/grub.cfg"])
        # grub-mkconfig exits 0 having found no kernel, and the machine then
        # drops back to the firmware menu with nothing to boot.
        entries = context.run_in_target(
            ["grep", "--count", "^menuentry", "/boot/grub/grub.cfg"], check=False
        ).strip()
        if not entries.isdigit() or int(entries) == 0:
            raise NothingToBoot("grub.cfg has no menu entry; /boot holds no kernel")


@dataclass(frozen=True, kw_only=True)
class WriteGrubDefaults(Operation):
    stage: Stage = Stage.BOOTLOADER
    kernel_params: tuple[str, ...]
    #: `/boot` inside a LUKS container. `grub-install` refuses outright without
    #: this, saying so in as many words.
    cryptodisk: bool
    serial: tuple[str, int] | None
    #: Gentoo's dracut sets `hostonly_cmdline="no"` and detects the chroot's own
    #: root, so only `rd.luks.uuid` tells the initramfs what to open.
    luks: tuple[DeviceId, ...] = ()
    #: Arrays the initramfs assembles. /etc/mdadm.conf alone is not enough:
    #: dracut assembles nothing without `rd.md.uuid` on the command line.
    arrays: tuple[DeviceId, ...] = ()
    #: The initramfs keymap. An encrypted root asks for its passphrase there,
    #: and dracut is built with hostonly_cmdline="no", so the command line is
    #: the only thing that says which keyboard is attached.
    keymap: str = ""

    def describe(self) -> str:
        extra = []
        if self.cryptodisk:
            extra.append("cryptodisk enabled")
        if self.serial is not None:
            extra.append(f"its menu on {self.serial[0]}")
        listed = f", {' and '.join(extra)}" if extra else ""
        return f"write /etc/default/grub with cmdline {' '.join(self.kernel_params) or 'empty'}{listed}"

    def apply(self, context: Context) -> None:
        # `GRUB_CMDLINE_LINUX` reaches every entry and `_DEFAULT` only the
        # default one, so what the initramfs needs to find the root at all goes
        # in the first: a recovery entry with no `rd.luks.uuid` waits for a
        # device that never appears. Recovery is disabled below, which made the
        # split invisible rather than unnecessary.
        needed = (
            *luks_parameters(context, self.luks),
            *array_parameters(context, self.arrays),
            *keymap_parameters(self.keymap),
        )
        lines = [
            f'GRUB_CMDLINE_LINUX="{" ".join(needed)}"',
            f'GRUB_CMDLINE_LINUX_DEFAULT="{" ".join(self.kernel_params)}"',
            "GRUB_TIMEOUT=5",
            "GRUB_DISABLE_RECOVERY=true",
        ]
        if self.cryptodisk:
            lines.append("GRUB_ENABLE_CRYPTODISK=y")
        if self.serial is not None:
            port, baud = self.serial
            unit = port.removeprefix("ttyS")
            # Both: serial alone leaves a machine with a monitor dark until
            # the kernel starts.
            lines += [
                'GRUB_TERMINAL_INPUT="console serial"',
                'GRUB_TERMINAL_OUTPUT="console serial"',
                f'GRUB_SERIAL_COMMAND="serial --unit={unit} --speed={baud}"',
            ]
        context.write(PurePosixPath("/etc/default/grub"), "\n".join(lines) + "\n")


@dataclass(frozen=True, kw_only=True)
class RequestBootctl(Operation):
    """The `boot` flag is what provides `bootctl` and the EFI stub, and both
    packages that can provide them keep it behind that flag.

    Written in the portage phase, not the bootloader phase:
    `installkernel[systemd-boot]` depends on this same flag and is merged with
    the kernel, several phases earlier.
    """

    stage: Stage = Stage.PORTAGE
    package: str

    def describe(self) -> str:
        return f"ask for {self.package}[boot], which is what provides bootctl"

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath("/etc/portage/package.use/systemd-boot"), f"{self.package} boot\n"
        )


@dataclass(frozen=True, kw_only=True)
class InstallSystemdBoot(Operation):
    stage: Stage = Stage.BOOTLOADER
    esp: PurePosixPath

    def describe(self) -> str:
        return f"install systemd-boot on the esp at {self.esp}"

    def apply(self, context: Context) -> None:
        context.run_in_target(["bootctl", f"--esp-path={self.esp}", "install"])


@dataclass(frozen=True, kw_only=True)
class GenerateHostId(Operation):
    """The pool records the hostid it was created under, which is the installing
    system's. `zgenhostid -f` alone writes a fresh random one, so the value is
    read from the installing system and written into the target."""

    #: The kernel stage, not the bootloader one: dracut's zfs module copies
    #: `/etc/hostid` into the initramfs, so writing it after `RebuildInitramfs`
    #: left the image without it and the pool imported under a hostid the
    #: target does not have.
    stage: Stage = Stage.KERNEL

    def describe(self) -> str:
        return "copy the installing system's hostid into the target so the pool imports"

    def apply(self, context: Context) -> None:
        hostid = context.run(["hostid"]).strip()
        context.run_in_target(["zgenhostid", "-f", hostid])


@dataclass(frozen=True, kw_only=True)
class InstallZfsBootMenu(Operation):
    """No `zpool.cache`: the boot path is hostid plus an import scan, which
    survives the pool being seen under a different device path."""

    stage: Stage = Stage.BOOTLOADER
    pool: str
    dataset: str
    esp: PurePosixPath
    #: The esp partition itself, so the boot entry names the right one rather
    #: than defaulting to partition 1.
    esp_device: DeviceId
    kernel_params: tuple[str, ...]
    #: `org.zfsbootmenu:commandline` is the command line of the system ZBM
    #: boots, not of ZBM itself, which is what prompts for a passphrase.
    serial: tuple[str, int] | None

    def describe(self) -> str:
        return f"build ZFSBootMenu into {self.esp}/{ZBM_DIRECTORY} and boot {self.dataset} from it"

    def _config(self) -> str:
        kernel = ""
        if self.serial is not None:
            port, baud = self.serial
            # Both consoles, and the serial one last: /dev/console follows the
            # last `console=`, and a machine with a monitor still shows a menu.
            kernel = (
                "Kernel:\n"
                f'  CommandLine: "ro loglevel=4 console=tty1 console={port},{baud}"\n'
            )
        return (
            "Global:\n"
            "  ManageImages: true\n"
            f"  BootMountPoint: {self.esp}\n"
            "  InitCPIO: false\n"
            "Components:\n"
            "  Enabled: false\n"
            "EFI:\n"
            f"  ImageDir: {self.esp}/EFI/zbm\n"
            "  Versions: false\n"
            "  Enabled: true\n"
            "  Stub: /usr/lib/systemd/boot/efi/linuxx64.efi.stub\n"
            f"{kernel}"
        )

    def apply(self, context: Context) -> None:
        context.run_in_target(["zpool", "set", f"bootfs={self.dataset}", self.pool])
        context.run_in_target(
            [
                "zfs", "set",
                f"org.zfsbootmenu:commandline={' '.join(self.kernel_params)}",
                self.dataset,
            ]
        )
        context.write(PurePosixPath("/etc/zfsbootmenu/config.yaml"), self._config())
        context.run_in_target(["generate-zbm"])
        image = self._image(context)
        context.run_in_target(["install", "-D", "-m0644", image, f"{self.esp}/{FALLBACK_IMAGE}"])
        context.run_in_target(
            [
                "efibootmgr",
                "--create",
                "--disk", context.containing_disk(self.esp_device),
                "--part", str(context.partition_index(self.esp_device)),
                "--label", "ZFSBootMenu",
                "--loader", _windows_path(image, self.esp),
            ]
        )

    def _image(self, context: Context) -> str:
        """Whatever generate-zbm wrote. It names the image after the kernel
        it built from, so `vmlinuz.EFI` is only one of the names it can have."""
        listing = context.run_in_target(
            ["find", f"{self.esp}/{ZBM_DIRECTORY}", "-name", "*.EFI"], check=False
        )
        found = sorted(
            line.strip() for line in listing.splitlines() if line.strip().endswith(".EFI")
        )
        if not found:
            raise NothingToBoot(f"generate-zbm wrote no EFI image under {self.esp}/{ZBM_DIRECTORY}")
        return found[0]


def build(config: InstallConfig) -> list[Operation]:
    kind = config.bootloader.kind
    mount = compat.esp_mount(config.disk.graph)
    esp = mount.path if mount is not None else None
    esp_device = _esp_partition(config)
    packages = BOOTLOADER_PACKAGES[kind]
    if config.bootloader.firmware is Firmware.UEFI and kind is not Bootloader.SYSTEMD_BOOT:
        # GRUB only: `bootctl install` writes the boot entry through efivarfs
        # itself, so systemd-boot needs no efibootmgr.
        packages = (*packages, EFI_PACKAGE)
    operations: list[Operation] = []
    if packages:
        operations.append(
            Emerge(stage=Stage.BOOTLOADER, packages=packages, summary="install the bootloader")
        )
    if kind is Bootloader.GRUB:
        operations += [
            WriteGrubDefaults(
                kernel_params=(*unlock_parameters(config), *config.bootloader.kernel_params),
                cryptodisk=compat.boot_is_encrypted(config.disk.graph),
                serial=_serial_console(config),
                luks=initramfs_devices(config)[0],
                arrays=initramfs_devices(config)[1],
                keymap=initramfs_keymap(config),
            ),
            InstallGrub(
                firmware=config.bootloader.firmware, esp=esp, boot_device=config.disk.root
            ),
        ]
    elif kind is Bootloader.SYSTEMD_BOOT and esp is not None:
        provider = BOOTCTL_PACKAGE[config.system.init]
        operations += [
            RequestBootctl(package=provider),
            # `--noreplace`: `sys-kernel/installkernel[systemd-boot]` RDEPENDs
            # `sys-apps/systemd[boot(-)]`, so the kernel stage has already
            # pulled it in and a plain atom here is `[ebuild R]`. One run spent
            # 132 seconds rebuilding it with the flags it already had. The
            # operation stays, because nothing else guarantees `bootctl` when
            # the provider is `systemd-utils`.
            Emerge(
                stage=Stage.BOOTLOADER,
                packages=(provider,),
                summary="install bootctl",
                only_if_absent=True,
            ),
            InstallSystemdBoot(esp=esp),
        ]
    elif kind is Bootloader.ZFSBOOTMENU and esp is not None and esp_device is not None:
        pool, dataset = _root_pool_and_dataset(config)
        operations += [
            # Without the stub systemd ships behind its `boot` flag,
            # generate-zbm writes loose components and no bootable image.
            RequestBootctl(package=BOOTCTL_PACKAGE[config.system.init]),
            Emerge(
                stage=Stage.BOOTLOADER,
                packages=(BOOTCTL_PACKAGE[config.system.init],),
                summary="install the EFI stub generate-zbm builds around",
            ),
            InstallZfsBootMenu(
                pool=pool,
                dataset=dataset,
                esp=esp,
                esp_device=esp_device,
                kernel_params=(*unlock_parameters(config), *config.bootloader.kernel_params),
                serial=_serial_console(config),
            ),
        ]
    return operations


def luks_parameters(context: Context, devices: tuple[DeviceId, ...]) -> tuple[str, ...]:
    """`rd.luks.uuid` for each container the initramfs has to open."""
    return tuple(f"rd.luks.uuid={context.device_uuid(device)}" for device in devices)


def keymap_parameters(keymap: str) -> tuple[str, ...]:
    """`rd.vconsole.keymap`, so the passphrase prompt uses the right keyboard."""
    return (f"rd.vconsole.keymap={keymap}",) if keymap else ()


def unlock_parameters(config: InstallConfig) -> tuple[str, ...]:
    """What the initramfs needs to have an address before the root is unlocked.

    `rd.neednet=1` brings the link up without a network root, and `ip=` is what
    configures it; dracut's network module does nothing without both.
    """
    unlock = config.kernel.remote_unlock
    if not unlock.enabled:
        return ()
    return ("rd.neednet=1", f"ip={_ip_parameter(unlock)}")


def _ip_parameter(unlock: RemoteUnlock) -> str:
    """dracut's `ip=`, built from the three fields the operator answered.

    Seven colon-separated fields: client, peer, gateway, netmask, hostname,
    interface, autoconf. The netmask is dotted there, so a CIDR prefix is
    converted; an address alone reaches nothing off its own subnet.
    """
    if not unlock.address:
        return f"{unlock.interface}:dhcp" if unlock.interface else "dhcp"
    address, _, prefix = unlock.address.partition("/")
    netmask = _netmask(address, prefix)
    client, gateway = _bracketed(address), _bracketed(unlock.gateway)
    return f"{client}::{gateway}:{netmask}::{unlock.interface}:none"


def _bracketed(address: str) -> str:
    """An IPv6 literal in square brackets. The fields are colon-separated and
    so is the address, so an unbracketed one makes the whole parameter
    unreadable."""
    return f"[{address}]" if ":" in address else address


def _netmask(address: str, prefix: str) -> str:
    """Dotted for IPv4, the prefix itself for IPv6, empty when unparsable.

    Left empty rather than guessed: dracut takes an empty field as `unset`,
    and a wrong netmask silently puts the machine on the wrong subnet.
    """
    if not prefix:
        return ""
    try:
        parsed = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
    except ValueError:
        return ""
    if isinstance(parsed, ipaddress.IPv6Network):
        return str(parsed.prefixlen)
    return str(parsed.netmask)


def initramfs_devices(config: InstallConfig) -> tuple[tuple[DeviceId, ...], tuple[DeviceId, ...]]:
    """The containers and arrays the initramfs has to be told about.

    One function for both command line writers: deriving it twice is how
    systemd-boot came to omit the arrays that GRUB was given.
    """
    graph = config.disk.graph
    containers = tuple(node.backing for node in compat.early_containers(graph))
    arrays = tuple(node.id for node in graph.of_type(MdRaid))
    return containers, arrays


def array_parameters(context: Context, devices: tuple[DeviceId, ...]) -> tuple[str, ...]:
    """`rd.md.uuid` for each array the initramfs has to assemble."""
    return tuple(f"rd.md.uuid={context.array_uuid(device)}" for device in devices)


def initramfs_keymap(config: InstallConfig) -> str:
    """Only when an encrypted device asks for a passphrase before the console
    keymap is loaded, and only when it differs from the default."""
    wanted = config.system.keymap_initramfs or config.system.keymap
    if wanted == "us" or not compat.early_containers(config.disk.graph):
        return ""
    return wanted


def _serial_console(config: InstallConfig) -> tuple[str, int] | None:
    for parameter in config.bootloader.kernel_params:
        if not parameter.startswith("console=ttyS"):
            continue
        port, _, rest = parameter.split("=", 1)[1].partition(",")
        # The leading run only: `115200n8` names the speed and then the frame
        # format, and taking every digit made that 1152008 baud.
        speed = re.match(r"\d+", rest)
        return port, int(speed.group()) if speed else 115200
    return None


def _windows_path(image: str, esp: PurePosixPath) -> str:
    """`efibootmgr --loader` wants the path within the esp, backslashed."""
    return "\\" + str(PurePosixPath(image).relative_to(esp)).replace("/", "\\")


def _esp_partition(config: InstallConfig) -> DeviceId | None:
    """The esp partition itself, found through the mount `compat` already
    identifies, so the two do not disagree about which mount is the esp."""
    graph = config.disk.graph
    mount = compat.esp_mount(graph)
    if mount is None:
        return None
    # Sorted, because `ancestors_of` returns a frozenset and a mirrored esp
    # would otherwise give a different plan on every run.
    for parent in sorted(graph.ancestors_of(mount.id)):
        node = graph[parent]
        if isinstance(node, Partition) and node.role is PartitionRole.ESP:
            return node.id
    # A reused esp has no `Partition` node: it is the existing device the
    # filesystem sits on, which is what efibootmgr and grub-install want.
    for parent in sorted(graph.ancestors_of(mount.id)):
        if isinstance(graph[parent], Existing):
            return graph[parent].id
    return None


def _root_pool_and_dataset(config: InstallConfig) -> tuple[str, str]:
    """The pool `/` is on and the dataset that holds it, from one graph edge.

    Both were read separately, and the pool was whichever `ZfsPool` came first
    in the graph: a layout with a data pool beside the root pool had
    `bootfs=tank/ROOT/gentoo` set on `tank`, a dataset no pool has, and the
    install stopped at the bootloader with everything else already written.
    Reordering two equivalent `disk.devices` entries changed the answer.
    """
    graph = config.disk.graph
    root = graph.nodes.get(config.disk.root)
    if not isinstance(root, Mountpoint):
        return "", ""
    dataset = graph.nodes.get(root.source)
    if not isinstance(dataset, ZfsDataset):
        return "", ""
    pool = graph.nodes.get(dataset.pool)
    if not isinstance(pool, ZfsPool):
        return "", ""
    return pool.name, f"{pool.name}/{dataset.name}"
