# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from gentoo_install.model.config import InstallConfig
from gentoo_install.plan import disk as plan_disk
from gentoo_install.plan import portage as plan_portage
from gentoo_install.plan.bootloader import InstallGrub
from gentoo_install.plan.build import build
from gentoo_install.plan.convert import SwapDirectories
from gentoo_install.plan.operations import Stage
from gentoo_install.model.config import DiskConfig, DiskMode
from gentoo_install.model.device import DeviceGraph, DeviceId
from gentoo_install.plan.packages import Catalog, Group

from .layouts import config


CATALOG: Catalog = {"console": Group(name="console", packages=("app-editors/vim",))}


def _in_place() -> InstallConfig:
    """A conversion carries no device graph: the layout comes from the machine,
    and `validate()` refuses one beside the mode."""
    return replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId(""), mode=DiskMode.IN_PLACE),
    )


def test_partition_mode_keeps_the_ordinary_list() -> None:
    ordinary = build(config(), CATALOG)
    explicit = build(replace(config(), disk=replace(config().disk)), CATALOG)
    assert tuple(type(operation) for operation in explicit) == tuple(type(operation) for operation in ordinary)


def test_conversion_operation_describes_and_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[tuple[Path, tuple[str, ...]]] = []

    def convert(staging: Any, names: tuple[str, ...]) -> None:
        called.append((staging, names))

    from gentoo_install.exec import convert as executor

    monkeypatch.setattr(executor, "convert", convert)
    operation = SwapDirectories()
    assert operation.describe()
    operation.apply(SimpleNamespace(target=PurePosixPath("/target")))
    assert called == [(Path("/gentoo-install.new"), operation.names)]
