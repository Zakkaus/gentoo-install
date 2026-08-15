# SPDX-License-Identifier: GPL-2.0-or-later
"""Every fixture's plan, compared against a file in version control.

A diff here is the point: any change to what an install does shows up as a
reviewable change to these files in the same commit.

Regenerate with `python3 -m tests.golden.regenerate` after reading the diff.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.exec.config import load
from gentoo_install.plan.build import build
from gentoo_install.plan.render import render

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"


def plan_text(name: str) -> str:
    return render(build(load(FIXTURES / f"{name}.toml"), load_catalog()))


def fixtures() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.toml"))


@pytest.mark.parametrize("name", fixtures())
def test_the_plan_matches_its_golden_file(name: str) -> None:
    expected = HERE / f"{name}.txt"
    assert expected.exists(), f"no golden file for {name}; run tests.golden.regenerate"
    assert plan_text(name) == expected.read_text()


def test_every_fixture_has_a_golden_file() -> None:
    assert {path.stem for path in HERE.glob("*.txt")} == set(fixtures())


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
    catalog = load_catalog()
    assert render(build(backwards, catalog)) == render(build(installation, catalog))
