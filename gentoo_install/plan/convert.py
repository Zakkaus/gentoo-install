# SPDX-License-Identifier: GPL-2.0-or-later
"""The irreversible userland swap in an in-place conversion."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol, Sequence, cast

from ..errors import ConversionFailed, ConversionUnsupported
from ..model.config import DiskConfig, DiskMode
from ..model.device import (
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    FilesystemType,
    Mountpoint,
    StorageLayout,
)
from .operations import Context, Operation, Stage


REPLACED_DIRECTORIES: tuple[str, ...] = (
    "bin",
    "sbin",
    "etc",
    "lib",
    "lib64",
    "usr",
    "var",
)


class _Converter(Protocol):
    def convert(self, staging: Path, names: Sequence[str], *, root: Path = Path("/")) -> None: ...


@dataclass(frozen=True, kw_only=True)
class Staged(Operation):
    """Run an operation against the staging root instead of the target.

    Everything before the swap has to land in `/gentoo-install.new`, because
    the running userland is still the one on disk. One wrapper rather than a
    staging variant of each operation: the planners stay unaware of the mode.
    """

    stage: Stage
    inner: Operation
    staging: PurePosixPath = PurePosixPath("/gentoo-install.new")

    @property
    def wrapped(self) -> Operation | None:
        # Preflight reads this: `--missing-commands` and the check before an
        # install key part of their table on the operation's type, and a
        # wrapper is not an instance of what it wraps.
        return self.inner

    @property
    def releases_the_machine(self) -> bool:
        return self.inner.releases_the_machine

    def describe(self) -> str:
        return f"{self.inner.describe()}, in {self.staging}"

    def apply(self, context: Context) -> None:
        self.inner.apply(_aimed_at(context, self.staging))


def _aimed_at(parent: Context, staging: PurePosixPath) -> Context:
    """The same machine with everything aimed at the staging root.

    `run_in_target` chroots into the machine's own mount point rather than
    `context.target`, so a context that answered only `target` would have run
    every `emerge` in the system being replaced. The live implementation keeps
    that path in one field, so moving it moves all of them; a recorder that
    keeps no such field gets the wrapper, which is enough for one that runs
    nothing.
    """
    if hasattr(parent, "mountpoint"):
        try:
            return cast(Context, replace(cast(Any, parent), mountpoint=Path(str(staging))))
        except TypeError:
            pass
    return cast(Context, _StagingContext(parent=parent, staging=staging))


@dataclass(frozen=True)
class _StagingContext:
    """The install context with `target` moved to the staging root.

    Everything else is forwarded rather than listed: naming the members by
    hand is exactly what drifted, and a conversion stopped at `make.conf`
    with `'_StagingContext' object has no attribute 'read'` after it had
    already downloaded and unpacked a stage3.
    """

    parent: Context
    staging: PurePosixPath

    @property
    def target(self) -> PurePosixPath:
        return self.staging

    def __getattr__(self, name: str) -> object:
        return getattr(self.parent, name)


@dataclass(frozen=True, kw_only=True)
class SwapDirectories(Operation):
    """Atomically replace the selected live-system directories."""

    stage: Stage = Stage.BOOTLOADER
    names: tuple[str, ...] = REPLACED_DIRECTORIES
    staging: PurePosixPath = PurePosixPath("/gentoo-install.new")

    def describe(self) -> str:
        return f"atomically swap {', '.join('/' + name for name in self.names)} from {self.staging}"

    def apply(self, context: Context) -> None:
        module = importlib.import_module("gentoo_install.exec.convert")
        converter = cast(_Converter, module)
        converter.convert(Path(str(self.staging)), self.names)


@dataclass(frozen=True, kw_only=True)
class PrepareStaging(Operation):
    """Create the directory the staged system is built in.

    Nothing else does: the ordinary path gets `/mnt/gentoo` from the mount
    operations, and a conversion has none, so `tar --directory` was the first
    thing to find out.
    """

    # `STAGE3`, not `MOUNT`: creating a directory is not a mount, and a
    # conversion mounts nothing. Sorting is stable, so it stays ahead of the
    # unpack it exists for.
    stage: Stage = Stage.STAGE3
    staging: PurePosixPath = PurePosixPath("/gentoo-install.new")

    def required_host_commands(self) -> frozenset[str]:
        return frozenset({"mkdir", "find"})

    def describe(self) -> str:
        return f"create {self.staging} for the staged system"

    def apply(self, context: Context) -> None:
        context.run(["mkdir", "--parents", str(self.staging)])
        inside = context.run(
            ["find", str(self.staging), "-maxdepth", "1", "-mindepth", "1"], check=False
        ).strip()
        if inside:
            raise ConversionFailed(
                f"{self.staging} is not empty and is left from an earlier attempt: "
                "remove it before converting"
            )


@dataclass(frozen=True, kw_only=True)
class LeaveStaging(Operation):
    """Unmount what the chroot bound under the staging root and remove it.

    `disk.finish` cannot be reused: it unmounts everything under the target,
    and in this mode the target is `/`. What is left otherwise is `/proc` and
    `/sys` bound into a directory nothing will ever enter again, which holds
    the machine at shutdown, and a staging root on the disk for ever.
    """

    stage: Stage = Stage.FINISH
    staging: PurePosixPath = PurePosixPath("/gentoo-install.new")

    def required_host_commands(self) -> frozenset[str]:
        return frozenset({"umount", "findmnt", "rm"})

    def describe(self) -> str:
        return f"unmount and remove {self.staging}"

    def apply(self, context: Context) -> None:
        context.run(["umount", "--recursive", "--lazy", str(self.staging)], check=False)
        listed = context.run(
            ["findmnt", "--noheadings", "--list", "--output", "TARGET"], check=False
        )
        under = f"{self.staging}/"
        if any(
            line.strip() == str(self.staging) or line.strip().startswith(under)
            for line in listed.splitlines()
        ):
            # Said rather than raised: the machine is converted, and `rm` walking
            # into a `/proc` still bound here is not a risk worth taking to
            # tidy up.
            raise ConversionFailed(f"{self.staging} still has something mounted under it")
        context.run(["rm", "--recursive", "--force", str(self.staging)])


class _BootPopulator(Protocol):
    def populate_boot(self, staging: Path, *, root: Path = Path("/")) -> None: ...


@dataclass(frozen=True, kw_only=True)
class PopulateBoot(Operation):
    """Put the staged kernel into the machine's own `/boot`, after the swap.

    `/boot` is not in the swap: it is a separate mount on many machines and
    holds the esp below it on many more, and `rename` refuses both. Without
    this the converted machine keeps the old distribution's kernels and the
    one that was just built stays in the staging root.
    """

    stage: Stage = Stage.BOOTLOADER
    staging: PurePosixPath = PurePosixPath("/gentoo-install.new")

    def describe(self) -> str:
        return f"put the kernel from {self.staging}/boot into /boot and drop the old images"

    def apply(self, context: Context) -> None:
        module = importlib.import_module("gentoo_install.exec.convert")
        cast(_BootPopulator, module).populate_boot(Path(str(self.staging)))


#: The synthetic ids. Fixed rather than derived from the device name so that a
#: plan reads the same whichever disk the machine happens to boot from.
ROOT_DEVICE: Final[DeviceId] = DeviceId("running-root-device")
ROOT_FILESYSTEM: Final[DeviceId] = DeviceId("running-root")
ROOT_MOUNT: Final[DeviceId] = DeviceId("running-root-mount")
BOOT_DEVICE: Final[DeviceId] = DeviceId("running-boot-device")
BOOT_FILESYSTEM: Final[DeviceId] = DeviceId("running-boot")
BOOT_MOUNT: Final[DeviceId] = DeviceId("running-boot-mount")
ESP_DEVICE: Final[DeviceId] = DeviceId("running-esp-device")
ESP_FILESYSTEM: Final[DeviceId] = DeviceId("running-esp")
ESP_MOUNT: Final[DeviceId] = DeviceId("running-esp-mount")


def layout_graph(layout: StorageLayout) -> DiskConfig:
    """Describe the running layout as the device graph the planners read.

    The bootloader, `fstab` and kernel planners all take the root device, the
    esp and the mount options from `config.disk.graph`. A second description
    for the conversion would be the same rule set in two tables, so the facts
    `probe.storage_layout()` read become a graph instead. Every filesystem in
    it carries `create=False`, so no operation derived from it formats
    anything.
    """
    _room_for_both(layout)
    below = _unsupported_layer(layout)
    if below:
        raise ConversionUnsupported(
            f"the running root is below {below}, which the conversion cannot rebuild"
        )
    if not layout.root_device or not layout.root_filesystem_type:
        raise ConversionUnsupported("the running root device could not be read")
    nodes: list[object] = [
        Existing(id=ROOT_DEVICE, selector=layout.root_device),
        Filesystem(
            id=ROOT_FILESYSTEM,
            device=ROOT_DEVICE,
            kind=_filesystem(layout.root_filesystem_type, "root"),
            create=False,
        ),
        Mountpoint(id=ROOT_MOUNT, source=ROOT_FILESYSTEM, path=PurePosixPath("/")),
    ]
    if layout.boot_same_filesystem is False:
        # A separate `/boot` has to be in the new `fstab`: the kernel this
        # conversion puts there is on that filesystem, and a machine that does
        # not mount it comes up with an empty `/boot`.
        if not layout.boot_device or not layout.boot_filesystem_type:
            raise ConversionUnsupported("the running /boot is a separate filesystem that could not be read")
        nodes += [
            Existing(id=BOOT_DEVICE, selector=layout.boot_device),
            Filesystem(
                id=BOOT_FILESYSTEM,
                device=BOOT_DEVICE,
                kind=_filesystem(layout.boot_filesystem_type, "/boot"),
                create=False,
            ),
            Mountpoint(id=BOOT_MOUNT, source=BOOT_FILESYSTEM, path=PurePosixPath("/boot")),
        ]
    if layout.uefi:
        if not layout.esp_device:
            raise ConversionUnsupported("the machine booted through UEFI and has no esp")
        nodes += [
            Existing(id=ESP_DEVICE, selector=layout.esp_device),
            Filesystem(
                id=ESP_FILESYSTEM,
                device=ESP_DEVICE,
                kind=FilesystemType.VFAT,
                create=False,
            ),
            Mountpoint(
                id=ESP_MOUNT,
                source=ESP_FILESYSTEM,
                path=PurePosixPath(layout.esp_mountpoint or "/efi"),
                options=("umask=0077",),
            ),
        ]
    return DiskConfig(
        graph=DeviceGraph(cast(Sequence[Any], nodes)),
        root=ROOT_MOUNT,
        mode=DiskMode.IN_PLACE,
    )


#: What the root filesystem needs free before a conversion starts. The staged
#: system and the running one are on it at the same time: a base Gentoo with a
#: binary kernel is about 4 GiB, the ebuild repository 1.5 GiB, and the
#: binary packages and distfiles fetched on the way another 2 GiB.
CONVERSION_FREE_BYTES: Final[int] = 10 * 2**30


def _room_for_both(layout: StorageLayout) -> None:
    """Stop before anything is written when the root cannot hold both systems.

    An unknown figure is not a small one: `findmnt` not reporting `avail` is a
    reason to carry on, because refusing there would stop machines that are
    fine.
    """
    free = layout.root_free_bytes
    if free is not None and free < CONVERSION_FREE_BYTES:
        raise ConversionUnsupported(
            f"the root filesystem has {free // 2**30} GiB free and a conversion needs "
            f"{CONVERSION_FREE_BYTES // 2**30}: the staged system and the running one "
            "are on it at the same time"
        )


def _unsupported_layer(layout: StorageLayout) -> str:
    """The first stacked layer below the root, empty when there is none."""
    for name, present in (
        ("LUKS", layout.root_on_luks),
        ("LVM", layout.root_on_lvm),
        ("mdraid", layout.root_on_mdraid),
    ):
        if present:
            return name
    return ""


def _filesystem(name: str, where: str) -> FilesystemType:
    try:
        return FilesystemType(name)
    except ValueError as error:
        raise ConversionUnsupported(
            f"the running {where} is on {name}, which this installer cannot describe"
        ) from error
