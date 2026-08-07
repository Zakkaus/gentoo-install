"""Kernel, initramfs and the storage modules dracut has to carry.

The dracut modules are derived from the device graph, never listed by hand: a
second list is a list that goes stale, and the cost of it going stale is an
initramfs that cannot find the root filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from ..model.config import InstallConfig, KernelSource
from ..model.device import (
    Filesystem,
    FilesystemType,
    Luks,
    MdRaid,
    VolumeGroup,
    ZfsPool,
)
from .operations import Context, Operation, Stage
from .portage import Emerge

#: The package that provides each kernel choice.
KERNEL_PACKAGES: Final[dict[KernelSource, str]] = {
    KernelSource.DIST_BIN: "sys-kernel/gentoo-kernel-bin",
    KernelSource.DIST_SOURCE: "sys-kernel/gentoo-kernel",
    KernelSource.CJK_SOURCE: "sys-kernel/gentoo-cjk-sources",
}

#: Filesystems whose driver dracut only includes when asked.
FILESYSTEM_MODULES: Final[dict[FilesystemType, str]] = {FilesystemType.BTRFS: "btrfs"}

#: Userspace tools the target needs for each layer of its storage stack.
STORAGE_PACKAGES: Final[dict[str, str]] = {
    "btrfs": "sys-fs/btrfs-progs",
    "crypt": "sys-fs/cryptsetup",
    "lvm": "sys-fs/lvm2",
    "mdraid": "sys-fs/mdadm",
    "zfs": "sys-fs/zfs",
    "xfs": "sys-fs/xfsprogs",
    "f2fs": "sys-fs/f2fs-tools",
    "vfat": "sys-fs/dosfstools",
    "ext": "sys-fs/e2fsprogs",
}


@dataclass(frozen=True, kw_only=True)
class ConfigureInstallKernel(Operation):
    """`sys-kernel/installkernel[dracut]` has to be set before the kernel is
    merged: the kernel's own install hook is what builds the initramfs."""

    stage: Stage = Stage.KERNEL

    def describe(self) -> str:
        return "set sys-kernel/installkernel to use dracut"

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath("/etc/portage/package.use/installkernel"),
            "sys-kernel/installkernel dracut\n",
        )


@dataclass(frozen=True, kw_only=True)
class WriteDracutModules(Operation):
    stage: Stage = Stage.KERNEL
    modules: tuple[str, ...]

    def describe(self) -> str:
        return f"tell dracut to carry {', '.join(self.modules)}"

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath("/etc/dracut.conf.d/10-gentoo-install.conf"),
            f'add_dracutmodules+=" {" ".join(self.modules)} "\n',
        )


@dataclass(frozen=True, kw_only=True)
class AcceptFirmwareLicence(Operation):
    """`linux-firmware` stops at an interactive licence prompt otherwise, and an
    unattended install waits there until it is killed."""

    stage: Stage = Stage.KERNEL

    def describe(self) -> str:
        return "accept the linux-firmware licence"

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath("/etc/portage/package.license/linux-firmware"),
            "sys-kernel/linux-firmware linux-fw-redistributable no-source-code\n",
        )


@dataclass(frozen=True, kw_only=True)
class RebuildInitramfs(Operation):
    """The kernel was merged before the dracut configuration was complete for
    any package that pulls one in, so the image is built again from it."""

    stage: Stage = Stage.KERNEL
    package: str

    def describe(self) -> str:
        return f"rebuild the initramfs from {self.package} with the modules written above"

    def apply(self, context: Context) -> None:
        context.run_in_target(["emerge", "--config", self.package])


def build(config: InstallConfig) -> list[Operation]:
    modules = dracut_modules(config)
    operations: list[Operation] = [ConfigureInstallKernel()]
    if modules:
        operations.append(WriteDracutModules(modules=modules))
    operations += [
        AcceptFirmwareLicence(),
        Emerge(
            stage=Stage.KERNEL,
            packages=("sys-kernel/dracut", "sys-kernel/linux-firmware"),
            summary="install the initramfs builder and firmware",
        ),
    ]
    tools = storage_packages(config)
    if tools:
        operations.append(
            Emerge(stage=Stage.KERNEL, packages=tools, summary="install the storage tools")
        )
    package = KERNEL_PACKAGES[config.kernel.source]
    operations.append(
        Emerge(
            stage=Stage.KERNEL,
            packages=(package,),
            summary="install the kernel",
            # A patched kernel exists on no binary host but ours, and a sources
            # package has to be compiled in any case.
            binary_packages=config.kernel.source is KernelSource.DIST_BIN,
        )
    )
    if config.kernel.source is not KernelSource.CJK_SOURCE:
        operations.append(RebuildInitramfs(package=package))
    return operations


def dracut_modules(config: InstallConfig) -> tuple[str, ...]:
    graph = config.disk.graph
    modules: list[str] = []
    if graph.of_type(MdRaid):
        modules.append("mdraid")
    if graph.of_type(Luks):
        modules.append("crypt")
    if graph.of_type(VolumeGroup):
        modules.append("lvm")
    if graph.of_type(ZfsPool):
        modules.append("zfs")
    for filesystem in graph.of_type(Filesystem):
        module = FILESYSTEM_MODULES.get(filesystem.kind)
        if module is not None and module not in modules:
            modules.append(module)
    for extra in config.kernel.dracut_modules:
        if extra not in modules:
            modules.append(extra)
    return tuple(modules)


def storage_packages(config: InstallConfig) -> tuple[str, ...]:
    """Whatever the layout uses, the target needs the tools to mount it again."""
    graph = config.disk.graph
    wanted: list[str] = []
    for module in dracut_modules(config):
        package = STORAGE_PACKAGES.get(module)
        if package is not None and package not in wanted:
            wanted.append(package)
    for filesystem in graph.of_type(Filesystem):
        key = "ext" if filesystem.kind.value.startswith("ext") else filesystem.kind.value
        package = STORAGE_PACKAGES.get(key)
        if package is not None and package not in wanted:
            wanted.append(package)
    return tuple(wanted)
