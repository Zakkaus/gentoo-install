from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from gentoo_install.model.config import InstallConfig, Overlay, User
from gentoo_install.model.device import DeviceGraph, DeviceId, PartitionTable
from gentoo_install.model.parse import _NODES, parse
from gentoo_install.model.serialise import KINDS, to_toml

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _round_trip(config: InstallConfig) -> InstallConfig:
    return parse(tomllib.loads(to_toml(config)))


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.toml")), ids=lambda path: path.name)
def test_every_fixture_survives_a_round_trip(path: Path) -> None:
    config = parse(tomllib.loads(path.read_text()))
    assert _round_trip(config) == config


def test_every_node_kind_can_be_written() -> None:
    assert set(KINDS.values()) == set(_NODES)


def test_a_table_edited_in_place_survives() -> None:
    config = parse(tomllib.loads((FIXTURES / "ext4-bios.toml").read_text()))
    node = next(one for one in config.disk.graph.nodes.values() if isinstance(one, PartitionTable))
    edited = replace(
        config,
        disk=replace(
            config.disk,
            graph=DeviceGraph.build(
                replace(one, create=False, remove=(2, 3)) if one is node else one
                for one in config.disk.graph.nodes.values()
            ),
        ),
    )
    written = _round_trip(edited)
    table = written.disk.graph.nodes[DeviceId(node.id)]
    assert isinstance(table, PartitionTable)
    assert (table.create, table.remove) == (False, (2, 3))


def test_users_and_overlays_are_written_as_tables() -> None:
    config = parse(tomllib.loads((FIXTURES / "ext4-bios.toml").read_text()))
    config = replace(
        config,
        system=replace(config.system, users=(User(name="zakk", groups=("wheel",), sudo=True),)),
        portage=replace(
            config.portage,
            overlays=(Overlay(name="gig", sync_uri="https://github.com/gentoozh/gig.git"),),
        ),
    )
    assert _round_trip(config) == config


def test_a_default_is_left_out_of_the_file() -> None:
    config = parse(tomllib.loads((FIXTURES / "ext4-bios.toml").read_text()))
    plain = replace(config, system=replace(config.system, hostname="gentoo"))
    assert "hostname" not in to_toml(plain)
