"""GRUB, systemd-boot and ZFSBootMenu.

A ZFS root gets no GRUB artefacts at all. The Live ISO's Calamares run installs
GRUB and then deletes what it wrote, because its module order cannot be changed;
this installer decides before it writes, so nothing has to be undone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from ..model.config import Bootloader, Firmware, InstallConfig
from ..model.device import Mountpoint, ZfsDataset, ZfsPool
from .operations import Context, Operation, Stage
from .portage import Emerge

BOOTLOADER_PACKAGES: Final[dict[Bootloader, tuple[str, ...]]] = {
    Bootloader.GRUB: ("sys-boot/grub",),
    Bootloader.SYSTEMD_BOOT: ("sys-apps/systemd-utils",),
    Bootloader.ZFSBOOTMENU: ("sys-boot/zfsbootmenu",),
}

#: Writing an NVRAM entry needs this, and only UEFI has NVRAM to write to.
EFI_PACKAGE: Final[str] = "sys-boot/efibootmgr"

#: Where ZFSBootMenu's EFI executable is installed under the esp, and the
#: fallback path firmware boots when no NVRAM entry survives.
ZBM_IMAGE: Final[str] = "EFI/zbm/vmlinuz.EFI"
FALLBACK_IMAGE: Final[str] = "EFI/BOOT/BOOTX64.EFI"


@dataclass(frozen=True, kw_only=True)
class InstallGrub(Operation):
    stage: Stage = Stage.BOOTLOADER
    firmware: Firmware
    esp: PurePosixPath | None

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
            context.run_in_target(["grub-install", "--target=i386-pc", context.boot_disk()])
        context.run_in_target(["grub-mkconfig", "--output", "/boot/grub/grub.cfg"])


@dataclass(frozen=True, kw_only=True)
class WriteGrubDefaults(Operation):
    stage: Stage = Stage.BOOTLOADER
    kernel_params: tuple[str, ...]

    def describe(self) -> str:
        return f"write /etc/default/grub with cmdline {' '.join(self.kernel_params) or 'empty'}"

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath("/etc/default/grub"),
            f'GRUB_CMDLINE_LINUX_DEFAULT="{" ".join(self.kernel_params)}"\n'
            "GRUB_TIMEOUT=5\n"
            "GRUB_DISABLE_RECOVERY=true\n",
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
    """The pool records the hostid it was created under. The target's initramfs
    needs the same one or the pool is not imported at boot."""

    stage: Stage = Stage.BOOTLOADER

    def describe(self) -> str:
        return "write /etc/hostid so the pool imports on the target"

    def apply(self, context: Context) -> None:
        context.run_in_target(["zgenhostid", "-f"])


@dataclass(frozen=True, kw_only=True)
class InstallZfsBootMenu(Operation):
    """No `zpool.cache`: the boot path is hostid plus an import scan, which
    survives the pool being seen under a different device path."""

    stage: Stage = Stage.BOOTLOADER
    pool: str
    dataset: str
    esp: PurePosixPath
    kernel_params: tuple[str, ...]

    def describe(self) -> str:
        return f"build ZFSBootMenu into {self.esp}/{ZBM_IMAGE} and boot {self.dataset} from it"

    def apply(self, context: Context) -> None:
        context.run_in_target(["zpool", "set", f"bootfs={self.dataset}", self.pool])
        context.run_in_target(
            [
                "zfs", "set",
                f"org.zfsbootmenu:commandline={' '.join(self.kernel_params)}",
                self.dataset,
            ]
        )
        context.write(
            PurePosixPath("/etc/zfsbootmenu/config.yaml"),
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
            "  Stub: /usr/lib/systemd/boot/efi/linuxx64.efi.stub\n",
        )
        context.run_in_target(["generate-zbm"])
        context.run_in_target(
            ["install", "-D", "-m0644", f"{self.esp}/{ZBM_IMAGE}", f"{self.esp}/{FALLBACK_IMAGE}"]
        )
        context.run_in_target(
            [
                "efibootmgr",
                "--create",
                "--disk", context.boot_disk(),
                "--label", "ZFSBootMenu",
                "--loader", "\\EFI\\zbm\\vmlinuz.EFI",
            ]
        )


def build(config: InstallConfig) -> list[Operation]:
    kind = config.bootloader.kind
    esp = _esp_path(config)
    packages = BOOTLOADER_PACKAGES[kind]
    if config.bootloader.firmware is Firmware.UEFI:
        packages = (*packages, EFI_PACKAGE)
    operations: list[Operation] = [
        Emerge(stage=Stage.BOOTLOADER, packages=packages, summary="install the bootloader")
    ]
    if kind is Bootloader.GRUB:
        operations += [
            WriteGrubDefaults(kernel_params=config.bootloader.kernel_params),
            InstallGrub(firmware=config.bootloader.firmware, esp=esp),
        ]
    elif kind is Bootloader.SYSTEMD_BOOT and esp is not None:
        operations.append(InstallSystemdBoot(esp=esp))
    elif kind is Bootloader.ZFSBOOTMENU and esp is not None:
        pool = _pool_name(config)
        operations += [
            GenerateHostId(),
            InstallZfsBootMenu(
                pool=pool,
                dataset=_root_dataset(config, pool),
                esp=esp,
                kernel_params=config.bootloader.kernel_params,
            ),
        ]
    return operations


def _esp_path(config: InstallConfig) -> PurePosixPath | None:
    graph = config.disk.graph
    for path in (PurePosixPath("/efi"), PurePosixPath("/boot")):
        for mount in graph.of_type(Mountpoint):
            if mount.path == path:
                return path
    return None


def _pool_name(config: InstallConfig) -> str:
    pools = config.disk.graph.of_type(ZfsPool)
    return pools[0].name if pools else ""


def _root_dataset(config: InstallConfig, pool: str) -> str:
    graph = config.disk.graph
    root = graph.nodes.get(config.disk.root)
    if isinstance(root, Mountpoint):
        source = graph.nodes.get(root.source)
        if isinstance(source, ZfsDataset):
            return f"{pool}/{source.name}"
    return pool
