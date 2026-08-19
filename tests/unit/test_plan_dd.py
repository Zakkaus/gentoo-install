# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.model.config import (
    BootloaderConfig,
    DiskMode,
    ImageFormat,
    InstallConfig,
    KernelConfig,
    PackagesConfig,
    PortageConfig,
    SystemConfig,
)
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
    # Only the disk section: this mode writes the image as it is, and
    # `validate` refuses a configuration describing a machine it will not
    # produce.
    installation = config()
    return replace(
        installation,
        system=SystemConfig(),
        packages=PackagesConfig(),
        portage=PortageConfig(),
        kernel=KernelConfig(),
        bootloader=BootloaderConfig(),
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


def test_a_dd_configuration_cannot_describe_the_machine_it_will_not_produce() -> None:
    """The plan above drops everything but the write, which is why a section
    it drops must not be accepted: an operator who named a hostname, a user
    and a desktop got an image copy and no word about the rest."""
    from gentoo_install.errors import ValidationFailed
    from gentoo_install.model.config import User

    installation = dd_config(ImageFormat.RAW)
    named = replace(
        installation, system=replace(installation.system, hostname="expected-this")
    )
    with pytest.raises(ValidationFailed, match=r"\[system\] is not allowed in dd mode"):
        build(named, load_catalog())
    with_user = replace(
        installation,
        system=replace(
            installation.system, users=(User(name="zakk", password_hash="$6$x$" + "a" * 40),)
        ),
    )
    with pytest.raises(ValidationFailed, match=r"\[system\] is not allowed in dd mode"):
        build(with_user, load_catalog())


def test_reader_table_covers_every_image_format() -> None:
    assert set(dd.READERS) == set(ImageFormat)
