from __future__ import annotations

import tomllib
from dataclasses import fields, replace
from pathlib import Path

import pytest

from gentoo_install.model.config import InstallConfig, Overlay, User
from gentoo_install.model.device import DeviceGraph, DeviceId, Existing, PartitionTable
from gentoo_install.model.parse import _NODES, parse
from gentoo_install.model.serialise import KINDS, REDACTED, SECRET, to_toml

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _round_trip(config: InstallConfig) -> InstallConfig:
    return parse(tomllib.loads(to_toml(config)))


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.toml")), ids=lambda path: path.name)
def test_every_fixture_survives_a_round_trip(path: Path) -> None:
    config = parse(tomllib.loads(path.read_text()))
    assert _round_trip(config) == config


def test_probed_facts_are_not_written_as_configuration() -> None:
    config = parse(tomllib.loads((FIXTURES / "vm-mdraid.toml").read_text()))
    nodes = [
        replace(node, mdraid_metadata="1.0") if isinstance(node, Existing) else node
        for node in config.disk.graph.nodes.values()
    ]
    probed = replace(config, disk=replace(config.disk, graph=DeviceGraph.build(nodes)))
    assert _round_trip(probed) == config


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


def test_a_published_configuration_carries_no_password_hash() -> None:
    """The pastebin is a public address and a crypt hash is what an offline
    attack starts from."""
    config = parse(tomllib.loads((FIXTURES / "ext4-bios.toml").read_text()))
    config = replace(
        config,
        system=replace(
            config.system,
            root_password_hash="$6$rootsalt$rootrootroot",
            users=(User(name="zakk", password_hash="$6$usersalt$useruseruser"),),
        ),
    )
    published = to_toml(config, publishing=True)
    assert "rootrootroot" not in published
    assert "useruseruser" not in published
    assert published.count(REDACTED) == 2


def test_a_published_configuration_still_parses() -> None:
    """It is offered so someone can attach it to an issue and be told to try
    it, so it has to be a file `--config` accepts."""
    config = parse(tomllib.loads((FIXTURES / "vm-desktop.toml").read_text()))
    again = parse(tomllib.loads(to_toml(config, publishing=True)))
    assert again.system.hostname == config.system.hostname
    assert again.system.root_password_hash == REDACTED


def test_every_secret_field_the_model_has_is_in_the_table() -> None:
    """A field added with `password_hash` in its name and not in `SECRET` is a
    hash this would publish."""
    from gentoo_install.model.config import SystemConfig

    named = {
        field.name
        for holder in (SystemConfig, User)
        for field in fields(holder)
        if "password" in field.name and field.name.endswith("hash")
    }
    assert named == SECRET


def test_a_size_is_written_as_a_literal_and_not_as_a_table() -> None:
    """`Size` is a frozen dataclass, so the writer recursed into it and
    produced `[system.zram]`, which the parser then refused. A saved
    configuration holding zram or a build tmpfs could not be loaded back."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.size import Size

    started = load(Path(__file__).resolve().parents[1] / "fixtures" / "vm-binpkg.toml")
    held = replace(
        started,
        system=replace(started.system, zram=Size.parse("2GiB")),
        portage=replace(started.portage, build_in_ram=Size.parse("8GiB")),
    )
    written = to_toml(held)
    assert 'zram = "2GiB"' in written
    assert 'build_in_ram = "8GiB"' in written
    assert "[system.zram]" not in written
    assert parse(tomllib.loads(written)) == held


@pytest.mark.parametrize(
    "said",
    [
        "first\nsecond",
        "tab\there",
        "ret\rurn",
        "bell\x07",
        "del\x7f",
        'quote"and\\slash',
        "form\ffeed",
        "back\bspace",
    ],
)
def test_a_control_character_survives_the_round_trip(said: str) -> None:
    """The encoder escaped only backslash and quote, so a value carrying a
    newline produced TOML that `tomllib` refused to read back."""
    from .layouts import config

    held = replace(config(), system=replace(config().system, hostname=said))
    assert _round_trip(held).system.hostname == said
