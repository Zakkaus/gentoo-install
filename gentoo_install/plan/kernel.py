"""Kernel, initramfs and the storage modules dracut has to carry.

The dracut modules are derived from the device graph, never listed by hand: a
second list is a list that goes stale, and the cost of it going stale is an
initramfs that cannot find the root filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from ..model.config import Bootloader, InitSystem, InstallConfig, KernelSource
from ..errors import ConfigError, InvalidLayout, NothingToBoot
from ..model import compat
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
from .bootloader import (
    array_parameters,
    initramfs_devices,
    keymap_parameters,
    luks_parameters,
)
from .bootloader import GenerateHostId
from .bootloader import initramfs_keymap as bootloader_keymap
from .bootloader import unlock_parameters
from .operations import Context, Operation, Stage
from .portage import (
    Emerge,
    InstallMode,
    PortageConfigKind,
    SourcePolicy,
    WritePortageConfig,
)
from .bootloader import VerifyPackageUse

#: The package that provides each kernel choice.
KERNEL_PACKAGES: Final[dict[KernelSource, str]] = {
    KernelSource.DIST_BIN: "sys-kernel/gentoo-kernel-bin",
    KernelSource.DIST_SOURCE: "sys-kernel/gentoo-kernel",
    KernelSource.CJK_BIN: "sys-kernel/gentoo-cjk-kernel-bin",
    KernelSource.CJK: "sys-kernel/gentoo-cjk-kernel",
}

#: Filesystems whose driver dracut only includes when asked.
FILESYSTEM_MODULES: Final[dict[FilesystemType, str]] = {FilesystemType.BTRFS: "btrfs"}

#: Early unlocking over ssh. It is `~amd64`, and its RDEPEND brings dropbear
#: and one of the network managers dracut's network module can drive.
REMOTE_UNLOCK_PACKAGE: Final[str] = "sys-kernel/dracut-crypt-ssh"

#: Executables required by dracut's network-legacy module. ZFSBootMenu omits
#: systemd, so its remote-unlock image cannot use systemd-networkd instead.
ZBM_LEGACY_NETWORK_PACKAGES: Final[tuple[str, ...]] = (
    "sys-apps/iproute2",
    "net-misc/dhcp",
    "net-misc/iputils",
)

#: Modules an initramfs needs to answer on the network before the root is
#: unlocked. `crypt-ssh` is the module dracut-crypt-ssh installs as 60crypt-ssh.
REMOTE_UNLOCK_MODULES: Final[tuple[str, ...]] = ("crypt-ssh", "network")

#: The two packages that carry the cjktty patch, prebuilt and from source.
#: Both take the `cjk` flag and both are keyworded `~amd64` in gentoo-zh.
CJK_KERNELS: Final[tuple[KernelSource, ...]] = (KernelSource.CJK_BIN, KernelSource.CJK)

INSTALLKERNEL_STATE: Final[str] = "/var/lib/misc/installkernel"
KERNEL_IMAGES: Final[str] = "/boot"

#: One directory per kernel that can actually load a driver.
MODULE_DIRECTORY: Final[str] = "/lib/modules"

#: The cjk USE flag of the patched kernels, which merges the
#: patch's own `cjk.config`. It is on by default, so only turning it off has
#: to be written.
CJK_USE: Final[str] = "cjk"


@dataclass(frozen=True)
class StackTool:
    """What one dracut module needs installed in the target."""

    atom: str
    #: Flags the default build does not carry: without `sys-fs/lvm2[lvm]`
    #: dracut cannot build its lvm module.
    use: tuple[str, ...] = ()
    #: Atoms this tool builds a kernel module from. They read
    #: `/usr/src/linux/.config`, which a dist-kernel does not leave, so each
    #: one has to be told to build against the package instead.
    modules: tuple[str, ...] = ()


#: The tool each dracut module needs in the target.
STACK_PACKAGES: Final[dict[str, StackTool]] = {
    "btrfs": StackTool("sys-fs/btrfs-progs"),
    "crypt": StackTool("sys-fs/cryptsetup"),
    "lvm": StackTool("sys-fs/lvm2", use=("lvm",)),
    "mdraid": StackTool("sys-fs/mdadm"),
    # Not sys-fs/zfs-kmod: `sys-fs/zfs-2.4.1` absorbed the module and blocks
    # every older kmod, so naming it merges a package the tree has retired.
    "zfs": StackTool("sys-fs/zfs", modules=("sys-fs/zfs",)),
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
        where = f", installing into {self.boot_root}" if self.boot_root else ""
        return f"set sys-kernel/installkernel to {flags}{where}"

    def _flags(self) -> tuple[str, ...]:
        return ("dracut", "systemd", "systemd-boot") if self.boot_entries else ("dracut",)

    #: ZFSBootMenu reads the kernel out of the boot environment's own `/boot`,
    #: which is inside the pool. `kernel-install` otherwise picks the mounted
    #: esp as `$BOOT` and writes `/efi/<entry-token>/<version>/`, so the pool
    #: has no kernel and generate-zbm answers `Unable to find latest kernel`.
    boot_root: str = ""

    def apply(self, context: Context) -> None:
        VerifyPackageUse(atom="sys-kernel/installkernel", flags=self._flags()).apply(context)
        # Only this package's flags. What `bootctl` comes from is
        # `RequestBootctl`'s to say, and writing it here as well put one
        # package's USE in two files that neither named the other.
        WritePortageConfig(
            kind=PortageConfigKind.USE,
            name="installkernel",
            lines=(f"sys-kernel/installkernel {' '.join(self._flags())}",),
        ).apply(context)
        if self.boot_root:
            # A drop-in, never `/etc/kernel/install.conf`. kernel-install(8):
            # "The first of the files that is found will be used", so writing
            # the main file shadowed the one `sys-kernel/installkernel` ships
            # and took `layout=compat` and `initrd_generator=dracut` with it.
            # The next kernel merge then died on `No initrd_generator=`.
            context.write(
                PurePosixPath("/etc/kernel/install.conf.d/50-gentoo-install.conf"),
                f"BOOT_ROOT={self.boot_root}\n",
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
    luks: tuple[DeviceId, ...] = ()
    arrays: tuple[DeviceId, ...] = ()
    keymap: str = ""

    def describe(self) -> str:
        named = self.dataset or str(self.root)
        return f"write /etc/kernel/cmdline so the boot entries mount {named}"

    def apply(self, context: Context) -> None:
        if self.root is None:
            where = f"root=ZFS={self.dataset}"
        else:
            where = f"root=UUID={context.device_uuid(self.root)}"
        told = (
            *luks_parameters(context, self.luks),
            *array_parameters(context, self.arrays),
            *keymap_parameters(self.keymap),
        )
        context.write(
            PurePosixPath("/etc/kernel/cmdline"),
            " ".join((where, "rw", *told, *self.kernel_params)) + "\n",
        )


@dataclass(frozen=True, kw_only=True)
class RequestSystemdCryptsetup(Operation):
    """`cryptsetup` is in systemd's IUSE without a `+` and no profile turns it
    on, so a stage3 systemd ships no `systemd-cryptsetup-generator`. A systemd
    initramfs then never builds an unlock unit and boot waits for a device that
    only appears once the container is open.
    """

    stage: Stage = Stage.PORTAGE

    def describe(self) -> str:
        return "ask for sys-apps/systemd[cryptsetup], which provides the unlock generator"

    def apply(self, context: Context) -> None:
        VerifyPackageUse(atom="sys-apps/systemd", flags=("cryptsetup",)).apply(context)
        WritePortageConfig(
            kind=PortageConfigKind.USE,
            name="cryptsetup",
            lines=("sys-apps/systemd cryptsetup",),
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class RequestStorageUse(Operation):
    """Written in the portage phase, before anything merges these packages."""

    stage: Stage = Stage.PORTAGE
    entries: tuple[tuple[str, tuple[str, ...]], ...]

    def describe(self) -> str:
        listed = ", ".join(f"{atom}[{','.join(flags)}]" for atom, flags in self.entries)
        return f"ask for {listed}, which dracut needs and the default build lacks"

    def apply(self, context: Context) -> None:
        for atom, flags in self.entries:
            VerifyPackageUse(atom=atom, flags=flags).apply(context)
        WritePortageConfig(
            kind=PortageConfigKind.USE,
            name="storage",
            lines=tuple(f"{atom} {' '.join(flags)}" for atom, flags in self.entries),
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class RequestZfsBootMenuNetworkTools(Operation):
    """Enable the two optional executables network-legacy checks for."""

    stage: Stage = Stage.PORTAGE

    def describe(self) -> str:
        return "ask for dhclient and arping, which ZFSBootMenu networking requires"

    def apply(self, context: Context) -> None:
        VerifyPackageUse(atom="net-misc/dhcp", flags=("client", "-server")).apply(context)
        VerifyPackageUse(atom="net-misc/iputils", flags=("arping",)).apply(context)
        WritePortageConfig(
            kind=PortageConfigKind.USE,
            name="zfsbootmenu-network",
            lines=("net-misc/dhcp client -server", "net-misc/iputils arping"),
        ).apply(context)


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
class StoreZfsKey(Operation):
    """Move the pool off a boot-time prompt and onto a key file the target
    initramfs carries, so the passphrase is asked for once.

    ZFSBootMenu overrides a `file://` keylocation its own image cannot read and
    prompts instead, and the file exists only in the target initramfs, which is
    inside the pool that key unlocks.
    """

    stage: Stage = Stage.KERNEL
    pool: DeviceId
    name: str

    @property
    def key(self) -> PurePosixPath:
        return PurePosixPath(f"/etc/zfs/{self.name}.key")

    def describe(self) -> str:
        return f"put the {self.name} key in {self.key} so only ZFSBootMenu asks for it"

    def apply(self, context: Context) -> None:
        # No trailing newline: `zfs load-key` trims one when it is there, so a
        # file written without one is read the same either way.
        context.write(self.key, context.passphrase(self.pool), mode=0o400)
        context.write(
            PurePosixPath("/etc/dracut.conf.d/zfs-key.conf"),
            f'install_items+=" {self.key} "\n',
        )
        # On the installing system, not in the target: the pool is imported
        # here, and a stage3 has no `zfs` binary until sys-fs/zfs is merged
        # several operations later.
        context.run(["zfs", "set", f"keylocation=file://{self.key}", self.name])


@dataclass(frozen=True, kw_only=True)
class AcceptFirmwareLicence(Operation):
    """Written rather than left to `--autounmask-license`, so the acceptance is
    a file the installed system keeps and not a decision buried in a log."""

    stage: Stage = Stage.KERNEL

    def describe(self) -> str:
        return "accept the linux-firmware licence"

    def apply(self, context: Context) -> None:
        WritePortageConfig(
            kind=PortageConfigKind.LICENSE,
            name="linux-firmware",
            lines=("sys-kernel/linux-firmware linux-fw-redistributable no-source-code",),
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class ConfigureRemoteUnlock(Operation):
    """Keyword dracut-crypt-ssh and configure the system initramfs when used.

    It is `~amd64`, so the keyword is accepted for that atom alone. `dropbear`
    comes in through its RDEPEND. ZFSBootMenu uses the package in its own image
    and explicitly omits the module from the boot environment's initramfs.
    """

    stage: Stage = Stage.KERNEL
    port: int
    system_initramfs: bool = True

    def describe(self) -> str:
        if not self.system_initramfs:
            return "write /etc/dracut.conf.d/crypt-ssh.conf to omit ssh from the system initramfs"
        return f"configure remote unlock over ssh on port {self.port}"

    def apply(self, context: Context) -> None:
        WritePortageConfig(
            kind=PortageConfigKind.KEYWORDS,
            name="dracut-crypt-ssh",
            lines=(f"{REMOTE_UNLOCK_PACKAGE} ~amd64",),
        ).apply(context)
        if not self.system_initramfs:
            context.write(
                PurePosixPath("/etc/dracut.conf.d/crypt-ssh.conf"),
                'omit_dracutmodules+=" crypt-ssh "\n',
            )
            return
        lines = [
            f'dropbear_port="{self.port}"',
            # SYSTEM converts the target's own host key, so a client that has
            # already trusted this machine does not see a new one at unlock.
            'dropbear_rsa_key="SYSTEM"',
            'dropbear_ecdsa_key="SYSTEM"',
            'dropbear_ed25519_key="SYSTEM"',
            # The `unlock` helper runs cryptsetup, which the module does not
            # pull in by itself.
            'install_items+=" /sbin/cryptsetup "',
        ]
        context.write(
            PurePosixPath("/etc/dracut.conf.d/crypt-ssh.conf"),
            "".join(f"{line}\n" for line in lines),
        )


#: What `gentoo-cjk-kernel` and its `-bin` twin PDEPEND on:
#: `=virtual/dist-kernel-${PV}-r100`. The `-r100` revision exists only in
#: gentoo-zh, and gentoo-zh masked its own `virtual/dist-kernel` on 2026-08-09
#: because it is incompatible with `::gentoo`'s. So the cjk kernels now need
#: the mask lifted, and `::gentoo`'s unrevisioned copy cannot stand in.
CJK_MASKED: Final[str] = "virtual/dist-kernel"


@dataclass(frozen=True, kw_only=True)
class UnmaskCjkDistKernel(Operation):
    """Lift gentoo-zh's mask on the virtual the cjk kernel depends on.

    Written before the kernel merges. Without it the emerge stops on a masked
    package with the disks already partitioned, which is the failure every
    other keyword and licence operation here exists to move earlier.
    """

    stage: Stage = Stage.KERNEL

    def describe(self) -> str:
        return f"unmask {CJK_MASKED}, which the cjk kernel depends on by revision"

    def apply(self, context: Context) -> None:
        WritePortageConfig(
            kind=PortageConfigKind.UNMASK,
            name="cjk-kernel",
            lines=(CJK_MASKED,),
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class AcceptKernelVersion(Operation):
    """A pinned version that is not stable on amd64.

    Most kernel versions are `~amd64` for their first weeks, so pinning one is
    normally pinning a testing version; without this the emerge stops on a
    masked package after the disks are written.
    """

    stage: Stage = Stage.KERNEL
    package: str
    version: str

    def describe(self) -> str:
        return f"accept {self.package}-{self.version} as testing"

    def apply(self, context: Context) -> None:
        WritePortageConfig(
            kind=PortageConfigKind.KEYWORDS,
            name="kernel-version",
            lines=(f"={self.package}-{self.version} ~amd64",),
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class RequestCjkKernel(Operation):
    """Keyword and USE for the patched dist-kernel.

    It is `~amd64` in gentoo-zh, and its `cjk` flag is what merges the patch's
    own `cjk.config`; the flag is on by default, so only refusing it is written.
    """

    stage: Stage = Stage.KERNEL
    package: str
    cjk: bool

    def describe(self) -> str:
        return f"accept {self.package} as testing, with cjk {'on' if self.cjk else 'off'}"

    def apply(self, context: Context) -> None:
        WritePortageConfig(
            kind=PortageConfigKind.KEYWORDS,
            name="cjk-kernel",
            lines=(f"{self.package} ~amd64",),
        ).apply(context)
        if not self.cjk:
            VerifyPackageUse(atom=self.package, flags=("-cjk",)).apply(context)
            WritePortageConfig(
                kind=PortageConfigKind.USE,
                name="cjk-kernel",
                lines=(f"{self.package} -{CJK_USE}",),
            ).apply(context)


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


def _installkernel_payload(context: Context) -> tuple[str, ...]:
    output = context.read(PurePosixPath(INSTALLKERNEL_STATE))
    if not output.strip():
        raise NothingToBoot(f"{INSTALLKERNEL_STATE} has no installkernel payload")
    fields = output.splitlines()[-1].split("\t")
    if len(fields) < 11 or not fields[2] or not fields[7] or not fields[8] or not fields[9]:
        raise ConfigError(f"{INSTALLKERNEL_STATE} has an invalid installkernel payload")
    return tuple(fields)


@dataclass(frozen=True, kw_only=True)
class RequireKernelImage(Operation):
    """Stop here if no kernel image was written, rather than at the bootloader.

    `kernel-install` runs its plugins in order and treats exit status 77 as
    success, stopping the rest: dracut refusing to build -- "root is zfs, but
    the zfs module is missing" -- leaves `emerge --config` returning 0 with no
    image copied and nothing in the log that reads as a failure.

    The installkernel payload identifies the image path; no filename table is
    needed for a layout the package may change.
    """

    stage: Stage = Stage.KERNEL
    def describe(self) -> str:
        return "check the installkernel payload names existing kernel files"

    def apply(self, context: Context) -> None:
        fields = _installkernel_payload(context)
        root, image, initramfs = fields[7], fields[8], fields[9]
        for path in (f"{root}/{image}", f"{root}/{initramfs}"):
            found = context.run_in_target(["test", "-s", path], check=False)
            if not getattr(found, "returncode", 1) == 0:
                raise NothingToBoot(f"installkernel payload names missing image {path}")


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
        for package in self.packages:
            VerifyPackageUse(atom=package, flags=("dist-kernel",)).apply(context)
        WritePortageConfig(
            kind=PortageConfigKind.USE,
            name="dist-kernel-modules",
            lines=tuple(f"{package} dist-kernel" for package in self.packages),
        ).apply(context)


def build(config: InstallConfig) -> list[Operation]:
    graph = config.disk.graph
    modules = dracut_modules(config)
    entries = config.bootloader.kind is Bootloader.SYSTEMD_BOOT
    operations: list[Operation] = [
        ConfigureInstallKernel(
            boot_entries=entries,
            boot_root="/boot" if config.bootloader.kind is Bootloader.ZFSBOOTMENU else "",
        ),
    ]
    if entries:
        root, dataset, extra = _root_parameters(config)
        operations.append(
            WriteKernelCmdline(
                root=root,
                dataset=dataset,
                luks=initramfs_devices(config)[0],
                arrays=initramfs_devices(config)[1],
                keymap=bootloader_keymap(config),
                kernel_params=(
                    *extra,
                    *unlock_parameters(config),
                    *config.bootloader.kernel_params,
                ),
            )
        )
    operations += [
        Emerge(
            stage=Stage.KERNEL,
            packages=("sys-kernel/installkernel",),
            summary="install the hook that puts a kernel in /boot",
        ),
    ]
    if graph.of_type(Luks) and config.system.init is InitSystem.SYSTEMD:
        operations += [
            RequestSystemdCryptsetup(),
            # From source: the binary host builds the default USE, and a binary
            # package without the flag would be installed over this one.
            Emerge(
                stage=Stage.KERNEL,
                packages=("sys-apps/systemd",),
                summary="rebuild systemd with the unlock generator",
                mode=InstallMode.ONESHOT,
                source=SourcePolicy.build_all(),
            ),
        ]
    operations += [
        AcceptFirmwareLicence(),
        Emerge(
            stage=Stage.KERNEL,
            packages=("sys-kernel/dracut", "sys-kernel/linux-firmware"),
            summary="install the initramfs builder and firmware",
        ),
    ]
    flagged = storage_use(modules)
    if flagged:
        operations.insert(0, RequestStorageUse(entries=flagged))
    package = config.kernel.package or KERNEL_PACKAGES[config.kernel.source]
    version = config.kernel.version
    # `=atom-version` rather than a range: the operator chose one version off a
    # list this machine read, so anything else is not what they picked.
    atom = f"={package}-{version}" if version else package
    if version:
        operations.append(AcceptKernelVersion(package=package, version=version))
    if config.kernel.remote_unlock.enabled:
        unlock = config.kernel.remote_unlock
        system_initramfs = config.bootloader.kind is not Bootloader.ZFSBOOTMENU
        remote_packages: tuple[str, ...] = (REMOTE_UNLOCK_PACKAGE,)
        if not system_initramfs:
            operations.append(RequestZfsBootMenuNetworkTools())
            remote_packages += ZBM_LEGACY_NETWORK_PACKAGES
        operations += [
            ConfigureRemoteUnlock(port=unlock.port, system_initramfs=system_initramfs),
            Emerge(
                stage=Stage.KERNEL,
                packages=remote_packages,
                summary=(
                    "install the initramfs ssh daemon"
                    if system_initramfs
                    else "install ZFSBootMenu ssh support"
                ),
            ),
        ]
    if config.kernel.source in CJK_KERNELS:
        operations.append(RequestCjkKernel(package=package, cjk=config.system.console_cjk))
        operations.append(UnmaskCjkDistKernel())
    # Two orderings pull opposite ways, and both are real. The userland the
    # initramfs embeds has to exist before the kernel is merged, because the
    # kernel's own postinst runs dracut and it dies on a module whose tool is
    # missing: `dmsetup: command not found` then `Module 'lvm' cannot be
    # installed`. A package that builds a *kernel module* has to come after,
    # or it builds against whichever kernel Portage picks for
    # `virtual/dist-kernel` rather than the one being installed.
    #
    # So the tools split by `StackTool.modules`, and the dracut module list is
    # written after the kernel: the postinst run then asks for nothing that is
    # not there yet, and `RebuildInitramfs` at the end builds the real one.
    building = set(_module_builders(modules))
    tools = tuple(one for one in storage_packages(config, modules) if one not in building)
    flagged_now = tuple((one, use) for one, use in flagged if one not in building)
    plain = tuple(one for one in tools if one not in {atom for atom, _ in flagged_now})
    if plain:
        operations.append(
            Emerge(stage=Stage.KERNEL, packages=plain, summary="install the storage tools")
        )
    if flagged_now:
        # From source: the binary host builds the default USE, and a binary
        # package without the flag would be installed over the request above.
        operations.append(
            Emerge(
                stage=Stage.KERNEL,
                packages=tuple(one for one, _ in flagged_now),
                summary="install the storage tools that need a flag the default build lacks",
                source=SourcePolicy.build_all(),
            )
        )
    out_of_tree = _out_of_tree_modules(modules)
    if out_of_tree:
        operations.append(RequestDistKernelModules(packages=out_of_tree))
    # The two prebuilt ones only: a source package has to be compiled in any
    # case, and the patched pair is on no official binary host.
    prebuilt = config.kernel.source is KernelSource.DIST_BIN
    if out_of_tree:
        # One emerge, because `sys-fs/zfs[dist-kernel-cap]` caps
        # `virtual/dist-kernel` and resolving the two separately installed a
        # kernel above the cap first: Portage then pulled a second one into
        # its own slot, and `emerge --config <package>` had two to choose
        # between and stopped with "Please use a specific atom".
        operations.append(
            Emerge(
                stage=Stage.KERNEL,
                packages=(atom, *sorted(building)),
                summary="install the kernel and the tools that build a module for it",
                source=(
                    SourcePolicy.build_subset(tuple(sorted(building)))
                    if prebuilt
                    else SourcePolicy.build_all()
                ),
            )
        )
    else:
        operations.append(
            Emerge(
                stage=Stage.KERNEL,
                packages=(atom,),
                summary="install the kernel",
                source=(
                    SourcePolicy.binaries_allowed()
                    if prebuilt
                    else SourcePolicy.build_all()
                ),
            )
        )
    if modules:
        operations.append(WriteDracutModules(modules=modules))
    pool = _pool_the_initramfs_may_carry(config)
    if pool is not None:
        operations.append(StoreZfsKey(pool=pool.id, name=pool.name))
    # Delete first, then rebuild. The misnamed image `sys-fs/zfs` leaves is
    # often the only one in /boot, so deleting it last left generate-zbm with
    # `Unable to find latest kernel`. `emerge --config` reinstalls the image
    # under the name the package itself carries, which is the correct one.
    # Any pool, not only a root one: a data pool created under the installing
    # system's hostid asks for a forced import on the target without this, and
    # the bootloader stage was too late for a root pool because the initramfs
    # is built here.
    if graph.of_type(ZfsPool):
        operations.append(GenerateHostId())
    operations.append(RebuildInitramfs(package=package))
    operations.append(RequireKernelImage())
    return operations


def _module_builders(modules: tuple[str, ...]) -> tuple[str, ...]:
    """Tools that build a kernel module, so they wait for the kernel."""
    wanted: list[str] = []
    for module in modules:
        tool = STACK_PACKAGES.get(module)
        if tool is not None and tool.modules and tool.atom not in wanted:
            wanted.append(tool.atom)
    return tuple(wanted)


def _pool_the_initramfs_may_carry(config: InstallConfig) -> ZfsPool | None:
    """The encrypted pool whose key can go into the initramfs without leaking.

    ZFSBootMenu only, because it is the one bootloader that unlocks the pool
    itself; without it the initramfs prompt is the only prompt and a key file
    beside it would remove the passphrase from the boot path entirely.
    """
    if config.bootloader.kind is not Bootloader.ZFSBOOTMENU:
        return None
    graph = config.disk.graph
    pools = [pool for pool in graph.of_type(ZfsPool) if pool.encrypted]
    if len(pools) != 1 or not compat.boot_is_inside(graph, pools[0].id):
        return None
    return pools[0]


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
    if (
        config.kernel.remote_unlock.enabled
        and config.bootloader.kind is not Bootloader.ZFSBOOTMENU
    ):
        modules += [one for one in REMOTE_UNLOCK_MODULES if one not in modules]
    for extra in config.kernel.dracut_modules:
        if extra not in modules:
            modules.append(extra)
    return tuple(modules)


def _out_of_tree_modules(
    modules: InstallConfig | tuple[str, ...],
) -> tuple[str, ...]:
    """Read from STACK_PACKAGES, not from a list beside it: which tool builds a
    kernel module is one fact, and two tables holding it disagree eventually."""
    if isinstance(modules, InstallConfig):
        modules = dracut_modules(modules)
    wanted: list[str] = []
    for module in modules:
        tool = STACK_PACKAGES.get(module)
        if tool is None:
            continue
        wanted += [atom for atom in tool.modules if atom not in wanted]
    return tuple(wanted)


def storage_use(
    modules: InstallConfig | tuple[str, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Stack packages that need a USE flag the default build does not carry."""
    if isinstance(modules, InstallConfig):
        modules = dracut_modules(modules)
    wanted: list[tuple[str, tuple[str, ...]]] = []
    for module in modules:
        tool = STACK_PACKAGES.get(module)
        if tool is not None and tool.use and (tool.atom, tool.use) not in wanted:
            wanted.append((tool.atom, tool.use))
    return tuple(wanted)


def storage_packages(
    config: InstallConfig, modules: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    """Whatever the layout uses, the target needs the tools to mount it again."""
    graph = config.disk.graph
    if modules is None:
        modules = dracut_modules(config)
    wanted: list[str] = []
    for module in modules:
        tool = STACK_PACKAGES.get(module)
        if tool is not None and tool.atom not in wanted:
            wanted.append(tool.atom)
    # Sorted, unlike the dracut modules above whose order is a dependency
    # order: these come from a set of filesystem kinds and reading them in
    # graph order made the plan depend on how the devices happen to be written
    # in the configuration file.
    for package in sorted(
        {FILESYSTEM_PACKAGES[one.kind] for one in graph.of_type(Filesystem)}
    ):
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
