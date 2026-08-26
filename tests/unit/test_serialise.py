# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import tomllib
from typing import Iterator
from dataclasses import fields, replace
from pathlib import Path

import pytest

from gentoo_install.exec.config import load
from gentoo_install.model.config import DiskConfig, InstallConfig, Overlay, PortageConfig, ProxyConfig, User
from gentoo_install.model.config import ProxyKind
from gentoo_install.model.device import DeviceGraph, DeviceId, Existing, Node, PartitionTable
from gentoo_install.model.parse import parse
from gentoo_install.model.serialise import KINDS, REDACTED, SECRET, to_toml
from gentoo_install.model import templates
from gentoo_install.model.templates import Layout
from gentoo_install.model.config import Firmware

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _round_trip(config: InstallConfig) -> InstallConfig:
    return parse(tomllib.loads(to_toml(config)))


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.toml")), ids=lambda path: path.name)
def test_every_fixture_survives_a_round_trip(path: Path) -> None:
    config = parse(tomllib.loads(path.read_text()))
    assert _round_trip(config) == config

@pytest.mark.parametrize(
    "layout",
    [one for one in Layout if one is not Layout.REUSE],
    ids=lambda one: one.value,
)
def test_every_template_the_menu_builds_survives_a_round_trip(layout: Layout) -> None:
    """The fixtures above all came from TOML, so they agreed by construction:
    a second parse of what a first parse produced cannot disagree with it.

    What an operator exports is not a fixture. `templates.build` constructs
    the graph in Python, and its `rootpart` carried `share=None` while an
    omitted `size` parsed back as an empty `Share` — the same answer to
    `takes_the_rest` and a different object, so every export-then-import of a
    menu-built configuration compared unequal.
    """
    graph, root = templates.build(
        templates.Choice(
            disk="/dev/disk/by-id/virtio-target0",
            layout=layout,
            firmware=Firmware.UEFI,
        )
    )
    config = InstallConfig(disk=DiskConfig(graph=graph, root=root))
    assert _round_trip(config) == config


def test_a_dd_configuration_survives_a_round_trip() -> None:
    configuration = parse(
        tomllib.loads(
            '[disk]\n'
            'mode = "dd"\n'
            'source = "/run/prepared.raw.zst"\n'
            'source_format = "zst"\n'
            'destination = "/dev/disk/by-id/virtio-target"\n'
        )
    )
    assert _round_trip(configuration) == configuration


def test_runtime_storage_facts_are_not_configuration_fields() -> None:
    assert "mdraid_metadata" not in {field.name for field in fields(Existing)}
    assert "free_extents" not in {field.name for field in fields(PartitionTable)}


@pytest.mark.parametrize("kind", tuple(KINDS), ids=lambda kind: KINDS[kind])
def test_every_node_kind_survives_a_toml_round_trip(kind: type[Node]) -> None:
    for path in sorted(FIXTURES.glob("*.toml")):
        config = parse(tomllib.loads(path.read_text()))
        if any(isinstance(node, kind) for node in config.disk.graph.nodes.values()):
            assert _round_trip(config) == config
            return
    raise AssertionError(f"no fixture carries {kind.__name__}")


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


def test_an_l10n_override_survives_a_round_trip() -> None:
    config = parse(tomllib.loads((FIXTURES / "ext4-bios.toml").read_text()))
    held = replace(config, portage=replace(PortageConfig(), l10n=("en", "zh-TW")))

    written = to_toml(held)

    assert 'l10n = ["en", "zh-TW"]' in written
    assert _round_trip(held).portage.l10n == ("en", "zh-TW")


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


def test_proxy_credentials_round_trip_and_are_removed_from_published_config() -> None:
    path = FIXTURES / "proxy" / "config.toml"
    config = load(path)

    assert _round_trip(config).proxy == config.proxy
    published = to_toml(config, publishing=True)
    assert "secret" not in published
    assert "operator" not in published
    assert 'kind = "socks5"' in published
    assert 'host = "proxy.example"' in published


def test_proxy_with_every_field_round_trips() -> None:
    config = parse(tomllib.loads((FIXTURES / "proxy" / "config.toml").read_text()))
    assert config.proxy == ProxyConfig(
        kind=ProxyKind.SOCKS5,
        host="proxy.example",
        port=1080,
        username="operator",
        password="secret",
        bypass=("localhost", "intranet.example"),
    )
    assert _round_trip(config) == config


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.toml")), ids=lambda path: path.name)
def test_a_published_configuration_still_parses(path: Path) -> None:
    """It is offered so someone can attach it to an issue and be told to try
    it, so it has to be a file `--config` accepts.

    Every fixture, not one: the published copy was checked against
    `vm-desktop.toml` alone, and the field that leaks is the field some other
    layout is the only one to set.
    """
    config = parse(tomllib.loads(path.read_text()))
    published = to_toml(config, publishing=True)
    again = parse(tomllib.loads(published))
    assert again.system.hostname == config.system.hostname

    held = tomllib.loads(published)

    def leaves(value: object, at: str = "") -> Iterator[tuple[str, object]]:
        if isinstance(value, dict):
            for key, held_value in value.items():
                yield from leaves(held_value, f"{at}.{key}" if at else key)
        elif isinstance(value, list):
            for index, held_value in enumerate(value):
                yield from leaves(held_value, f"{at}[{index}]")
        else:
            yield at, value

    # Keyed on the name a hash has, not on `SECRET`: a check that reads the
    # same table the code reads goes quiet the moment an entry leaves it.
    for where, value in leaves(held):
        name = where.split(".")[-1].split("[")[0]
        if name.endswith("password_hash"):
            assert value == REDACTED, where
        if where.startswith("portage.proxy.") and name in {"username", "password"}:
            raise AssertionError(f"{where} survived publishing")


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


def test_a_simple_disk_is_written_back_as_the_template() -> None:
    """Sixty lines of graph become the eight the operator typed, and reading
    them again expands to the same graph through the same function."""
    import tomllib

    from gentoo_install.model import templates
    from gentoo_install.model.config import DiskConfig
    from gentoo_install.model.device import FilesystemType
    from gentoo_install.model.parse import parse
    from gentoo_install.model.size import Size

    chosen = templates.Choice(
        disk="/dev/disk/by-id/virtio-target0",
        filesystem=FilesystemType.EXT4,
        swap=Size.parse("2GiB"),
    )
    graph, root = templates.build(chosen)
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    config = replace(
        parse(tomllib.loads((fixtures / "btrfs-luks.toml").read_text())),
        disk=DiskConfig(graph=graph, root=root, simple=chosen),
    )

    written = to_toml(config)
    assert "[disk.simple]" in written
    assert "[[disk.devices]]" not in written
    # Only what differs from the default: an omitted key reads as whatever this
    # installer does on its own.
    assert "pool" not in written
    assert 'firmware = "uefi"' not in written

    again = parse(tomllib.loads(written))
    assert again.disk.simple == chosen
    assert again.disk.graph == graph

    # Negative control: a configuration that carries no template still writes
    # its graph, so the rule above is not "never write devices".
    plain = replace(config, disk=DiskConfig(graph=graph, root=root))
    assert "[[disk.devices]]" in to_toml(plain)


def test_every_node_field_the_writer_emits_is_a_key_its_parser_accepts() -> None:
    """`Subvolume.create = false` was written and then refused on reload, so a
    conversion's own layout could not be saved and read back. The writer emits
    every non-default field, so the parser has to name every one of them."""
    import ast
    import inspect
    from dataclasses import fields

    from gentoo_install.model import parse as parse_module
    from gentoo_install.model.serialise import KINDS, RENAMED, SPELLED_AS

    source = ast.parse(inspect.getsource(parse_module))
    accepted: dict[str, set[str]] = {}
    for node in ast.walk(source):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_reject_unknown"
                and len(call.args) == 3
                and isinstance(call.args[2], ast.Set)
            ):
                accepted[node.name] = {
                    one.value
                    for one in call.args[2].elts
                    if isinstance(one, ast.Constant) and isinstance(one.value, str)
                }
    assert accepted, "no parser named its accepted keys"

    builders = {kind: builder.__name__ for kind, builder in parse_module._NODES.items()}
    assert set(builders) == set(KINDS.values()), (
        sorted(set(builders) ^ set(KINDS.values()))
    )
    for held, kind in KINDS.items():
        keys = accepted.get(builders[kind])
        assert keys is not None, (kind, builders[kind])
        written: set[str] = set()
        for one in fields(held):
            written |= SPELLED_AS.get(
                (held, one.name), frozenset({RENAMED.get((held, one.name), one.name)})
            )
        assert written <= keys, (kind, sorted(written - keys))
    # And every declared expansion is for a field that exists, so a renamed
    # field leaves a stale entry that quietly excuses nothing.
    for (held, name), spelled in SPELLED_AS.items():
        assert name in {one.name for one in fields(held)}, (held.__name__, name)
        assert spelled, (held.__name__, name)
