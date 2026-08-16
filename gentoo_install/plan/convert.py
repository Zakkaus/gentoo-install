# SPDX-License-Identifier: GPL-2.0-or-later
"""The irreversible userland swap in an in-place conversion."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol, Sequence, cast

from ..errors import ConversionUnsupported
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

    def describe(self) -> str:
        return f"{self.inner.describe()}, in {self.staging}"

    def apply(self, context: Context) -> None:
        self.inner.apply(cast(Context, _StagingContext(parent=context, staging=self.staging)))


@dataclass(frozen=True)
class _StagingContext:
    parent: Context
    staging: PurePosixPath

    @property
    def target(self) -> PurePosixPath:
        return self.staging

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> str:
        return self.parent.run(argv, check=check, input_text=input_text)

    def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
        self.parent.write(path, content, mode=mode)

    def fetch_stage3(
        self,
        mirror: str,
        variant: str,
        fingerprint: str,
        fallbacks: Sequence[str] = (),
    ) -> PurePosixPath:
        return self.parent.fetch_stage3(mirror, variant, fingerprint, fallbacks)


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


#: The synthetic ids. Fixed rather than derived from the device name so that a
#: plan reads the same whichever disk the machine happens to boot from.
ROOT_DEVICE: Final[DeviceId] = DeviceId("running-root-device")
ROOT_FILESYSTEM: Final[DeviceId] = DeviceId("running-root")
ROOT_MOUNT: Final[DeviceId] = DeviceId("running-root-mount")
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
