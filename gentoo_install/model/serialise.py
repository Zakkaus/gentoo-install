"""An `InstallConfig` written back out as the TOML `parse.py` reads.

Driven by the dataclass fields, not by a second list of key names: the parser
already rejects a key the model has no field for, so deriving the writer from
the same fields is what keeps the two from drifting. A key holding the field's
default is left out, because a file full of defaults hides the answers that
were actually given.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Final

from . import config as model_config
from .config import InstallConfig, ProxyConfig
from .device import (
    Existing,
    Filesystem,
    LogicalVolume,
    Luks,
    MdRaid,
    Mountpoint,
    Node,
    Partition,
    PartitionTable,
    Subvolume,
    Swap,
    VolumeGroup,
    ZfsDataset,
    ZfsPool,
)
from .size import Size

#: The `kind` each node is written as, mirroring `parse._NODES`. A node class
#: absent here has no way into a file, so the round-trip test names them all.
KINDS: Final[dict[type[Node], str]] = {
    Existing: "existing",
    PartitionTable: "table",
    Partition: "partition",
    Luks: "luks",
    MdRaid: "raid",
    VolumeGroup: "volume-group",
    LogicalVolume: "logical-volume",
    ZfsPool: "zpool",
    ZfsDataset: "dataset",
    Filesystem: "filesystem",
    Subvolume: "subvolume",
    Swap: "swap",
    Mountpoint: "mountpoint",
}

#: Where a field name and its key in the file differ. `Filesystem.kind` is the
#: one case: the node discriminator already claims `kind`.
RENAMED: Final[dict[tuple[type[Node], str], str]] = {(Filesystem, "kind"): "type"}

#: Fields whose value is replaced when the configuration is published. A crypt
#: hash is not the password, but it is what an offline attack starts from, and
#: the pastebin is a public address.
SECRET: Final[frozenset[str]] = frozenset({"password_hash", "root_password_hash"})

#: What stands in for a secret. Not a valid hash, so a file edited from a
#: published one locks the account rather than setting a password nobody knows.
REDACTED: Final[str] = "removed-before-publishing"

def to_toml(config: InstallConfig, *, publishing: bool = False) -> str:
    """The configuration as a file that parses back into the same object.

    `publishing` replaces every password hash, for the copy that goes to a
    pastebin. The result still parses; it just installs no password.
    """
    lines = [f"{model_config.CONFIG_VERSION_KEY} = {config.config_version}"]
    for name in model_config.PERSISTED_SECTIONS[:-1]:
        lines += _section(name, getattr(config, name), publishing=publishing)
    lines += _disk(config)
    return "\n".join(lines).rstrip("\n") + "\n"


def _section(name: str, value: object, prefix: str = "", *, publishing: bool = False) -> list[str]:
    """One `[table]` and its keys, then a `[table.child]` for each nested one."""
    path = f"{prefix}{name}"
    keys: list[str] = []
    nested: list[str] = []
    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError(f"{path} is not a dataclass instance")
    for field in fields(value):
        held = getattr(value, field.name)
        # `Size` is a frozen dataclass and a scalar in the file, so recursing
        # into it wrote `[system.zram]` and the parser refused its own output.
        if is_dataclass(held) and not isinstance(held, type) and not isinstance(held, Size):
            nested += _section(field.name, held, f"{path}.", publishing=publishing)
            continue
        if held is None or held == field.default or held == _empty(field.default_factory):
            continue
        if _tables(held):
            nested += _array_of_tables(f"{path}.{field.name}", held, publishing=publishing)
            continue
        if publishing and field.name in SECRET:
            keys.append(f"{field.name} = {_value(REDACTED)}")
            continue
        if publishing and isinstance(value, ProxyConfig) and field.name == "url":
            held = value.redacted_url
        keys.append(f"{field.name} = {_value(held)}")
    if not keys and not nested:
        return []
    return (["", f"[{path}]", *keys] if keys else []) + nested


def _tables(held: object) -> bool:
    """Whether a value is a sequence of dataclasses, which TOML writes as `[[ ]]`."""
    return isinstance(held, tuple) and bool(held) and is_dataclass(held[0])


def _array_of_tables(path: str, held: tuple[Any, ...], *, publishing: bool = False) -> list[str]:
    lines: list[str] = []
    for one in held:
        lines += ["", f"[[{path}]]"]
        for field in fields(one):
            inner = getattr(one, field.name)
            if inner is None or inner == field.default or inner == _empty(field.default_factory):
                continue
            if publishing and field.name in SECRET:
                lines.append(f"{field.name} = {_value(REDACTED)}")
                continue
            lines.append(f"{field.name} = {_value(inner)}")
    return lines


def _empty(factory: object) -> object:
    """What a default_factory produces, or a sentinel when there is none."""
    if not callable(factory):
        return _NOTHING
    return factory()


class _Nothing:
    """Never equal to a field value, so a field with no factory is written."""


_NOTHING: Final[_Nothing] = _Nothing()


def _disk(config: InstallConfig) -> list[str]:
    lines = ["", "[disk]", f'root = "{config.disk.root}"']
    for node in config.disk.graph.nodes.values():
        kind = KINDS.get(type(node))
        if kind is None:
            raise KeyError(f"{type(node).__name__} has no kind to write it as")
        lines += ["", "[[disk.devices]]", f'kind = "{kind}"']
        for field in fields(node):
            held = getattr(node, field.name)
            if held is None:
                continue
            if field.name != "id" and (
                held == field.default or held == _empty(field.default_factory)
            ):
                continue
            named = RENAMED.get((type(node), field.name), field.name)
            lines.append(f"{named} = {_value(held)}")
    return lines


def _value(held: Any) -> str:
    if isinstance(held, bool):
        return "true" if held else "false"
    if isinstance(held, Enum):
        return f'"{held.value}"'
    if isinstance(held, (Size, PurePosixPath)):
        return f'"{held}"'
    if isinstance(held, int):
        return str(held)
    if isinstance(held, (tuple, list)):
        return "[" + ", ".join(_value(one) for one in held) + "]"
    return _quoted(str(held))


#: The escapes a TOML basic string defines by name. Everything else below
#: U+0020 takes the \\uXXXX form: a bare control character makes the file
#: unparsable, and a hostname carrying a newline produced one.
_ESCAPES: Final[dict[str, str]] = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _quoted(said: str) -> str:
    out = ['"']
    for char in said:
        named = _ESCAPES.get(char)
        if named is not None:
            out.append(named)
        elif char < "\u0020" or char == "\u007f":
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)
