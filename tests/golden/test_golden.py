# SPDX-License-Identifier: GPL-2.0-or-later
"""Every fixture's plan, compared against a file in version control.

A diff here is the point: any change to what an install does shows up as a
reviewable change to these files in the same commit.

Regenerate with `python3 -m tests.golden.regenerate` after reading the diff.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.exec.config import load
from gentoo_install.model.config import (
    BootMethod,
    DiskMode,
    InstallConfig,
    MemoryLaunch,
    MemoryMode,
    MirrorRegion,
)
from gentoo_install.plan import netboot
from gentoo_install.plan.build import build
from gentoo_install.plan.render import render

from ..unit.layouts import running_layout

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"


@dataclass(frozen=True)
class MemoryBuildFixture:
    """Named arguments for one memory boot arming plan."""

    name: str
    launch: MemoryLaunch
    target: netboot.BootTarget
    bypass: bool
    configuration: str
    source: str
    keys: tuple[str, ...]
    region: MirrorRegion


@dataclass(frozen=True)
class MemoryDisarmFixture:
    """Named arguments for one memory boot disarming plan."""

    name: str
    target: netboot.BootTarget


MemoryFixture = MemoryBuildFixture | MemoryDisarmFixture

SYSTEMD_BOOT_TARGET: Final = netboot.BootTarget(
    method=BootMethod.SYSTEMD_BOOT,
    architecture="x86_64",
    esp_mountpoint="/boot/efi",
)

MEMORY_FIXTURES: Final[tuple[MemoryFixture, ...]] = (
    MemoryBuildFixture(
        name="memory-ram",
        launch=MemoryLaunch(mode=MemoryMode.RAM),
        target=SYSTEMD_BOOT_TARGET,
        bypass=False,
        configuration="",
        source="",
        keys=(),
        region=MirrorRegion.GLOBAL,
    ),
    MemoryBuildFixture(
        name="memory-lowram",
        launch=MemoryLaunch(mode=MemoryMode.LOWRAM),
        target=SYSTEMD_BOOT_TARGET,
        bypass=False,
        configuration="",
        source="",
        keys=(),
        region=MirrorRegion.GLOBAL,
    ),
    MemoryBuildFixture(
        name="memory-bypass",
        launch=MemoryLaunch(mode=MemoryMode.RAM),
        target=SYSTEMD_BOOT_TARGET,
        bypass=True,
        configuration="",
        source="",
        keys=(),
        region=MirrorRegion.GLOBAL,
    ),
    MemoryDisarmFixture(
        name="memory-disarm",
        target=SYSTEMD_BOOT_TARGET,
    ),
)


def plan_of(installation: InstallConfig) -> str:
    """An in-place configuration carries no device graph, so its plan is
    derived from a machine. The fixed layout stands in for one, which is what
    gives the conversion a golden file at all: without it `build` raises and
    the fixture had none while every other fixture had one."""
    layout = running_layout() if installation.disk.mode is DiskMode.IN_PLACE else None
    return render(build(installation, load_catalog(), layout=layout))


def plan_text(name: str) -> str:
    return plan_of(load(FIXTURES / f"{name}.toml"))


def fixtures() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.toml"))


def memory_fixture(name: str) -> MemoryFixture | None:
    """The explicitly named memory-plan fixture, if one has that name."""
    return next((fixture for fixture in MEMORY_FIXTURES if fixture.name == name), None)


def memory_plan_text(fixture: MemoryFixture) -> str:
    """Render one memory plan from the exact arguments its CLI path receives."""
    if isinstance(fixture, MemoryDisarmFixture):
        return render(netboot.disarm(target=fixture.target))
    return render(
        netboot.build(
            launch=fixture.launch,
            target=fixture.target,
            bypass=fixture.bypass,
            configuration=fixture.configuration,
            source=fixture.source,
            keys=fixture.keys,
            region=fixture.region,
        )
    )


def golden_names() -> list[str]:
    """Every ordinary-install and memory-plan golden fixture name."""
    return sorted([*fixtures(), *(fixture.name for fixture in MEMORY_FIXTURES)])


def golden_text(name: str) -> str:
    """Render either the TOML-backed plan or an explicitly named memory plan."""
    fixture = memory_fixture(name)
    return memory_plan_text(fixture) if fixture is not None else plan_text(name)


@pytest.mark.parametrize("name", golden_names())
def test_the_plan_matches_its_golden_file(name: str) -> None:
    expected = HERE / f"{name}.txt"
    assert expected.exists(), f"no golden file for {name}; run tests.golden.regenerate"
    assert golden_text(name) == expected.read_text()


def test_every_fixture_has_a_golden_file() -> None:
    assert {path.stem for path in HERE.glob("*.txt")} == set(golden_names())


def test_memory_golden_fixtures_cover_each_mode() -> None:
    assert {fixture.name for fixture in MEMORY_FIXTURES} == {
        "memory-ram",
        "memory-lowram",
        "memory-bypass",
        "memory-disarm",
    }


@pytest.mark.parametrize("name", fixtures())
def test_the_order_devices_are_written_in_does_not_change_the_plan(name: str) -> None:
    """Two equivalent inputs, not one input twice: comparing `plan_text(name)`
    with itself passes for any deterministic implementation, including one
    that returns an empty plan, and it holds nothing that could differ.

    The device list's order is what an operator changes by editing a
    configuration file, and the plan is derived from the graph rather than
    from that order, so reversing it has to answer the same text.
    """
    from dataclasses import replace

    from gentoo_install.model.device import DeviceGraph

    installation = load(FIXTURES / f"{name}.toml")
    backwards = replace(
        installation,
        disk=replace(
            installation.disk,
            graph=DeviceGraph.build(list(reversed(list(installation.disk.graph.nodes.values())))),
        ),
    )
    assert plan_of(backwards) == plan_of(installation)
