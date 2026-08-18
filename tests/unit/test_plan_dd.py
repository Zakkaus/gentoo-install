# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.model.config import DiskMode, ImageFormat, InstallConfig
from gentoo_install.model.device import DeviceGraph
from gentoo_install.plan import dd
from gentoo_install.plan.build import build
from gentoo_install.plan.operations import Stage
from gentoo_install.plan.render import render

from .layouts import config, i
from .recorder import Recorder


_FORMATS: tuple[tuple[ImageFormat, tuple[str, ...]], ...] = (
    (ImageFormat.RAW, ("cat", "--")),
    (ImageFormat.GZIP, ("gzip", "--decompress", "--stdout", "--")),
    (ImageFormat.XZ, ("xz", "--decompress", "--stdout", "--")),
    (ImageFormat.ZSTD, ("zstd", "--decompress", "--stdout", "--")),
    (ImageFormat.TAR, ("tar", "--extract", "--to-stdout", "--file")),
)


def dd_config(source_format: ImageFormat) -> InstallConfig:
    installation = config()
    return replace(
        installation,
        disk=replace(
            installation.disk,
            graph=DeviceGraph.build(()),
            root=i(""),
            mode=DiskMode.DD,
            source=f"/run/gentoo.raw.{source_format.value}",
            source_format=source_format,
            destination="/dev/disk/by-id/virtio-target",
        ),
    )


@pytest.mark.parametrize(("source_format", "reader"), _FORMATS)
def test_dd_streams_each_supported_source_format(
    source_format: ImageFormat, reader: tuple[str, ...]
) -> None:
    installation = dd_config(source_format)
    operations = build(installation, load_catalog())
    operation = operations[0]

    assert render(operations) == (
        f"[partition]\n  stream the {source_format.value} image "
        f"{installation.disk.source} onto {installation.disk.destination}\n"
    )

    machine = Recorder()
    operation.apply(machine)
    assert machine.pipelines == [
        (
            (*reader, installation.disk.source),
            ("dd", f"of={installation.disk.destination}", "bs=4M", "conv=fsync"),
        )
    ]
    assert not machine.commands


def test_dd_plan_has_no_target_configuration_or_bootloader_operation() -> None:
    operations = build(dd_config(ImageFormat.ZSTD), load_catalog())

    assert [type(operation) for operation in operations] == [dd.WriteImage]
    assert all(operation.stage is not Stage.FINISH for operation in operations)


def test_reader_table_covers_every_image_format() -> None:
    assert set(dd.READERS) == set(ImageFormat)
