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

#: What a kernel needs switched on for the framebuffer console to draw CJK.
#: `FONT_CJK_32x32` is off on purpose: its Kconfig default is on, and the base
#: patch ships an empty glyph table, so it costs 8 MiB of kernel memory for a
#: font with nothing in it.
CJK_CONSOLE_OPTIONS: Final[tuple[tuple[str, bool], ...]] = (
    ("FRAMEBUFFER_CONSOLE", True),
    ("CONSOLE_TRANSLATIONS", True),
    ("FB_EFI", True),
    ("FONT_CJK_16x16", True),
    ("FONT_CJK_32x32", False),
)

#: The tool each dracut module's layer needs in the installed system.
STACK_PACKAGES: Final[dict[str, str]] = {
    "btrfs": "sys-fs/btrfs-progs",
    "crypt": "sys-fs/cryptsetup",
    "lvm": "sys-fs/lvm2",
    "mdraid": "sys-fs/mdadm",
    "zfs": "sys-fs/zfs",
}

#: The tool each filesystem needs, so the target can check and mount it again.
FILESYSTEM_PACKAGES: Final[dict[FilesystemType, str]] = {
    FilesystemType.EXT2: "sys-fs/e2fsprogs",
    FilesystemType.EXT3: "sys-fs/e2fsprogs",
    FilesystemType.EXT4: "sys-fs/e2fsprogs",
    FilesystemType.BTRFS: "sys-fs/btrfs-progs",
    FilesystemType.XFS: "sys-fs/xfsprogs",
    FilesystemType.F2FS: "sys-fs/f2fs-tools",
    FilesystemType.VFAT: "sys-fs/dosfstools",
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
    """Written rather than left to `--autounmask-license`, so the acceptance is
    a file the installed system keeps and not a decision buried in a log."""

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


@dataclass(frozen=True, kw_only=True)
class SelectKernelSource(Operation):
    """A sources package unpacks a tree and installs nothing. Everything after
    this works on whatever `/usr/src/linux` points at."""

    stage: Stage = Stage.KERNEL

    def describe(self) -> str:
        return "point /usr/src/linux at the kernel that was just unpacked"

    def apply(self, context: Context) -> None:
        context.run_in_target(["eselect", "kernel", "set", "1"])


@dataclass(frozen=True, kw_only=True)
class ConfigureKernel(Operation):
    """`defconfig` first, then the options this install needs on top of it.

    `olddefconfig` afterwards answers everything the toggles pulled in, which is
    what keeps the build from stopping at an interactive prompt.
    """

    stage: Stage = Stage.KERNEL
    options: tuple[tuple[str, bool], ...]

    def describe(self) -> str:
        named = ", ".join(f"{'+' if wanted else '-'}{name}" for name, wanted in self.options)
        return f"configure the kernel from defconfig with {named or 'no extra options'}"

    def apply(self, context: Context) -> None:
        source = "/usr/src/linux"
        context.run_in_target(["make", "--directory", source, "defconfig"])
        for name, wanted in self.options:
            context.run_in_target(
                [
                    f"{source}/scripts/config",
                    "--file", f"{source}/.config",
                    "--enable" if wanted else "--disable",
                    name,
                ]
            )
        context.run_in_target(["make", "--directory", source, "olddefconfig"])


@dataclass(frozen=True, kw_only=True)
class BuildKernel(Operation):
    stage: Stage = Stage.KERNEL

    def describe(self) -> str:
        return "build the kernel and its modules, then install both"

    def apply(self, context: Context) -> None:
        source = "/usr/src/linux"
        context.run_in_target(["make", "--directory", source, f"--jobs={context.jobs()}"])
        context.run_in_target(["make", "--directory", source, "modules_install"])
        # `install` runs installkernel, which is what builds the initramfs and
        # copies the image where the bootloader will look for it.
        context.run_in_target(["make", "--directory", source, "install"])


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
            # A patched kernel is on no official binary host, and a sources
            # package has to be compiled in any case.
            binary_packages=config.kernel.source is KernelSource.DIST_BIN,
        )
    )
    if config.kernel.source is KernelSource.CJK_SOURCE:
        # A sources package leaves a tree, not a kernel: without these three the
        # install finishes with a bootloader pointing at nothing.
        operations += [
            SelectKernelSource(),
            ConfigureKernel(options=CJK_CONSOLE_OPTIONS if config.system.console_cjk else ()),
            BuildKernel(),
        ]
    else:
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
        package = STACK_PACKAGES.get(module)
        if package is not None and package not in wanted:
            wanted.append(package)
    for filesystem in graph.of_type(Filesystem):
        package = FILESYSTEM_PACKAGES[filesystem.kind]
        if package not in wanted:
            wanted.append(package)
    return tuple(wanted)
