"""Kernel, initramfs and the storage modules dracut has to carry.

The dracut modules are derived from the device graph, never listed by hand: a
second list is a list that goes stale, and the cost of it going stale is an
initramfs that cannot find the root filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from ..model.config import Bootloader, InstallConfig, KernelSource
from ..errors import InvalidLayout
from ..model.device import (
    DeviceId,
    Filesystem,
    FilesystemType,
    Luks,
    MdRaid,
    Mountpoint,
    Subvolume,
    VolumeGroup,
    ZfsDataset,
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
    #: systemd-boot reads only what `layout=bls` writes, and that layout comes
    #: from this USE flag. The ebuild's REQUIRED_USE adds `systemd` with it.
    boot_entries: bool = False

    def describe(self) -> str:
        flags = " ".join(self._flags())
        return f"set sys-kernel/installkernel to {flags}"

    def _flags(self) -> tuple[str, ...]:
        return ("dracut", "systemd", "systemd-boot") if self.boot_entries else ("dracut",)

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath("/etc/portage/package.use/installkernel"),
            f"sys-kernel/installkernel {' '.join(self._flags())}\n",
        )


@dataclass(frozen=True, kw_only=True)
class WriteKernelCmdline(Operation):
    """What a bls entry boots with.

    `90-loaderentry.install` falls back to the running `/proc/cmdline` when this
    file is absent, so the installed system would boot with the install
    medium's own command line.
    """

    stage: Stage = Stage.KERNEL
    #: None when the root is a dataset, which names itself rather than a UUID.
    root: DeviceId | None
    dataset: str
    kernel_params: tuple[str, ...]

    def describe(self) -> str:
        named = self.dataset or str(self.root)
        return f"write /etc/kernel/cmdline so the boot entries mount {named}"

    def apply(self, context: Context) -> None:
        if self.root is None:
            where = f"root=ZFS={self.dataset}"
        else:
            where = f"root=UUID={context.device_uuid(self.root)}"
        context.write(
            PurePosixPath("/etc/kernel/cmdline"),
            " ".join((where, "rw", *self.kernel_params)) + "\n",
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
class RequestDistKernelModules(Operation):
    """An out-of-tree module builds against `/usr/src/linux` unless it is told
    the kernel is a dist-kernel, and a dist-kernel leaves no `.config` there:
    `sys-fs/zfs` then dies in its setup phase with "Kernel not configured"."""

    stage: Stage = Stage.KERNEL
    packages: tuple[str, ...]

    def describe(self) -> str:
        return f"tell {', '.join(self.packages)} to build against the dist-kernel"

    def apply(self, context: Context) -> None:
        lines = "".join(f"{package} dist-kernel\n" for package in self.packages)
        context.write(PurePosixPath("/etc/portage/package.use/dist-kernel-modules"), lines)


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
    entries = config.bootloader.kind is Bootloader.SYSTEMD_BOOT
    operations: list[Operation] = [
        ConfigureInstallKernel(boot_entries=entries),
    ]
    if entries:
        root, dataset, extra = _root_parameters(config)
        operations.append(
            WriteKernelCmdline(
                root=root,
                dataset=dataset,
                kernel_params=(*extra, *config.bootloader.kernel_params),
            )
        )
    operations += [
        # A dist-kernel pulls this in; a sources package does not, and without
        # it `make install` falls back to the kernel's own script, which looks
        # for LILO and leaves /boot without a kernel or an initramfs.
        Emerge(
            stage=Stage.KERNEL,
            packages=("sys-kernel/installkernel",),
            summary="install the hook that puts a kernel in /boot",
        ),
    ]
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
    modules = _out_of_tree_modules(config)
    if modules and config.kernel.source is not KernelSource.CJK_SOURCE:
        operations.append(RequestDistKernelModules(packages=modules))
    tools = storage_packages(config)
    if tools:
        operations.append(
            Emerge(stage=Stage.KERNEL, packages=tools, summary="install the storage tools")
        )
    package = config.kernel.package or KERNEL_PACKAGES[config.kernel.source]
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


#: Packages that build a kernel module of their own.
OUT_OF_TREE: Final[dict[str, tuple[str, ...]]] = {
    "zfs": ("sys-fs/zfs", "sys-fs/zfs-kmod"),
}


def _out_of_tree_modules(config: InstallConfig) -> tuple[str, ...]:
    wanted: list[str] = []
    for module in dracut_modules(config):
        wanted += [package for package in OUT_OF_TREE.get(module, ()) if package not in wanted]
    return tuple(wanted)


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


def _root_parameters(config: InstallConfig) -> tuple[DeviceId | None, str, tuple[str, ...]]:
    """What a boot entry needs to find the root: a device, or a dataset name.

    A subvolume root also needs `rootflags`, because the initramfs mounts the
    filesystem's default subvolume otherwise.
    """
    graph = config.disk.graph
    mount = graph[config.disk.root]
    source = graph[mount.source] if isinstance(mount, Mountpoint) else mount
    if isinstance(source, ZfsDataset):
        pool = graph[source.pool]
        name = pool.name if isinstance(pool, ZfsPool) else ""
        return None, f"{name}/{source.name}", ()
    if isinstance(source, Subvolume):
        filesystem = graph[source.filesystem]
        if isinstance(filesystem, Filesystem):
            return filesystem.device, "", (f"rootflags=subvol={source.name}",)
    if isinstance(source, Filesystem):
        return source.device, "", ()
    raise InvalidLayout(f"{config.disk.root} is not something a boot entry can mount")
