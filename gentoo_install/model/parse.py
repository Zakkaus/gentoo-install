"""TOML to `InstallConfig`.

Nothing here touches the machine. A device selector is carried through as the
string the user wrote; `exec/probe.py` resolves it and `validate.py` reports what
is missing. That keeps a configuration file checkable on any machine.
"""

from __future__ import annotations

import tomllib
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence, TypeVar

from ..errors import ConfigError
from .config import (
    CONFIG_VERSION,
    Binhost,
    BinhostChannel,
    Bootloader,
    BootloaderConfig,
    ConsoleFontSize,
    DiskConfig,
    Firmware,
    InitSystem,
    InstallConfig,
    KernelConfig,
    KernelSource,
    Keywords,
    MirrorConfig,
    MirrorRegion,
    Overlay,
    PackagesConfig,
    PortageConfig,
    SystemConfig,
    User,
)
from .device import (
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    FilesystemType,
    LogicalVolume,
    Luks,
    MdRaid,
    Mountpoint,
    Node,
    Partition,
    PartitionRole,
    PartitionTable,
    RaidLevel,
    RaidMetadata,
    Subvolume,
    Swap,
    TableType,
    VolumeGroup,
    ZfsDataset,
    ZfsPool,
)
from .size import Size

E = TypeVar("E", bound=Enum)


def load(path: Path) -> InstallConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    except OSError as error:
        raise ConfigError(f"{path}: {error}") from error
    return parse(raw)


def parse(raw: Mapping[str, Any]) -> InstallConfig:
    version = _int(raw, "config_version", "", default=CONFIG_VERSION)
    if version > CONFIG_VERSION:
        raise ConfigError(
            f"config_version {version} is newer than this installer understands "
            f"({CONFIG_VERSION}); upgrade the installer"
        )
    if version < CONFIG_VERSION:
        raise ConfigError(f"config_version {version} has no migration to {CONFIG_VERSION}")

    _reject_unknown(raw, "", {"config_version", "disk", "system", "portage", "kernel", "bootloader", "packages"})
    return InstallConfig(
        config_version=version,
        disk=_disk(_table(raw, "disk", "", required=True), "disk"),
        system=_system(_table(raw, "system", ""), "system"),
        portage=_portage(_table(raw, "portage", ""), "portage"),
        kernel=_kernel(_table(raw, "kernel", ""), "kernel"),
        bootloader=_bootloader(_table(raw, "bootloader", ""), "bootloader"),
        packages=_packages(_table(raw, "packages", ""), "packages"),
    )


def _system(raw: Mapping[str, Any], at: str) -> SystemConfig:
    _reject_unknown(
        raw,
        at,
        {
            "hostname", "timezone", "locales", "locale", "keymap", "console_cjk",
            "console_font", "init", "sshd", "users",
        },
    )
    default = SystemConfig()
    return SystemConfig(
        hostname=_str(raw, "hostname", at, default.hostname),
        timezone=_str(raw, "timezone", at, default.timezone),
        locales=_strings(raw, "locales", at, default.locales),
        locale=_str(raw, "locale", at, default.locale),
        keymap=_str(raw, "keymap", at, default.keymap),
        console_cjk=_bool(raw, "console_cjk", at, default.console_cjk),
        console_font=_enum(raw, "console_font", at, ConsoleFontSize, default.console_font),
        init=_enum(raw, "init", at, InitSystem, default.init),
        sshd=_bool(raw, "sshd", at, default.sshd),
        users=tuple(_user(entry, f"{at}.users[{n}]") for n, entry in enumerate(_tables(raw, "users", at))),
    )


def _user(raw: Mapping[str, Any], at: str) -> User:
    _reject_unknown(raw, at, {"name", "groups", "shell", "sudo"})
    default = User(name="")
    name = _str(raw, "name", at, required=True)
    if not name:
        raise ConfigError(f"{at}.name is empty")
    return User(
        name=name,
        groups=_strings(raw, "groups", at, default.groups),
        shell=_str(raw, "shell", at, default.shell),
        sudo=_bool(raw, "sudo", at, default.sudo),
    )


def _portage(raw: Mapping[str, Any], at: str) -> PortageConfig:
    _reject_unknown(
        raw,
        at,
        {
            "profile", "keywords", "makeopts", "common_flags", "use", "video_cards",
            "accept_license", "mirrors", "binhost", "overlays",
        },
    )
    default = PortageConfig()
    return PortageConfig(
        profile=_str(raw, "profile", at, default.profile),
        keywords=_enum(raw, "keywords", at, Keywords, default.keywords),
        makeopts=_str(raw, "makeopts", at, default.makeopts),
        common_flags=_str(raw, "common_flags", at, default.common_flags),
        use=_strings(raw, "use", at, default.use),
        video_cards=_strings(raw, "video_cards", at, default.video_cards),
        accept_license=_strings(raw, "accept_license", at, default.accept_license),
        mirrors=_mirrors(_table(raw, "mirrors", at), f"{at}.mirrors"),
        binhost=_binhost(_table(raw, "binhost", at), f"{at}.binhost"),
        overlays=tuple(
            _overlay(entry, f"{at}.overlays[{n}]") for n, entry in enumerate(_tables(raw, "overlays", at))
        ),
    )


def _mirrors(raw: Mapping[str, Any], at: str) -> MirrorConfig:
    _reject_unknown(raw, at, {"region", "speed_test", "distfiles", "repo_sync_uri"})
    default = MirrorConfig()
    return MirrorConfig(
        region=_enum(raw, "region", at, MirrorRegion, default.region),
        speed_test=_bool(raw, "speed_test", at, default.speed_test),
        distfiles=_strings(raw, "distfiles", at, default.distfiles),
        repo_sync_uri=_str(raw, "repo_sync_uri", at, default.repo_sync_uri),
    )


def _binhost(raw: Mapping[str, Any], at: str) -> Binhost:
    _reject_unknown(raw, at, {"official", "community"})
    default = Binhost()
    return Binhost(
        official=_bool(raw, "official", at, default.official),
        community=_enum(raw, "community", at, BinhostChannel, default.community),
    )


def _overlay(raw: Mapping[str, Any], at: str) -> Overlay:
    _reject_unknown(raw, at, {"name", "sync_uri"})
    return Overlay(name=_str(raw, "name", at, required=True), sync_uri=_str(raw, "sync_uri", at, required=True))


def _kernel(raw: Mapping[str, Any], at: str) -> KernelConfig:
    _reject_unknown(raw, at, {"source", "dracut_modules"})
    default = KernelConfig()
    return KernelConfig(
        source=_enum(raw, "source", at, KernelSource, default.source),
        dracut_modules=_strings(raw, "dracut_modules", at, default.dracut_modules),
    )


def _bootloader(raw: Mapping[str, Any], at: str) -> BootloaderConfig:
    _reject_unknown(raw, at, {"kind", "firmware", "kernel_params"})
    default = BootloaderConfig()
    return BootloaderConfig(
        kind=_enum(raw, "kind", at, Bootloader, default.kind),
        firmware=_enum(raw, "firmware", at, Firmware, default.firmware),
        kernel_params=_strings(raw, "kernel_params", at, default.kernel_params),
    )


def _packages(raw: Mapping[str, Any], at: str) -> PackagesConfig:
    _reject_unknown(raw, at, {"desktop", "applications", "extra"})
    default = PackagesConfig()
    return PackagesConfig(
        desktop=_str(raw, "desktop", at, default.desktop),
        applications=_strings(raw, "applications", at, default.applications),
        extra=_strings(raw, "extra", at, default.extra),
    )


def _disk(raw: Mapping[str, Any], at: str) -> DiskConfig:
    _reject_unknown(raw, at, {"root", "devices"})
    devices = _tables(raw, "devices", at)
    if not devices:
        raise ConfigError(f"{at}.devices is empty; nothing to install onto")
    nodes = [_node(entry, f"{at}.devices[{n}]") for n, entry in enumerate(devices)]
    return DiskConfig(graph=DeviceGraph.build(nodes), root=DeviceId(_str(raw, "root", at, required=True)))


def _node(raw: Mapping[str, Any], at: str) -> Node:
    kind = _str(raw, "kind", at, required=True)
    builder = _NODES.get(kind)
    if builder is None:
        raise ConfigError(f"{at}.kind is {kind!r}; expected one of {', '.join(sorted(_NODES))}")
    return builder(raw, at)


def _existing(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "selector", "wipe"})
    return Existing(id=_id(raw, at), selector=_str(raw, "selector", at, required=True), wipe=_bool(raw, "wipe", at, False))


def _table_node(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "disk", "table"})
    return PartitionTable(id=_id(raw, at), disk=_ref(raw, "disk", at), table=_enum(raw, "table", at, TableType, TableType.GPT))


def _partition(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "table", "index", "role", "size", "label"})
    return Partition(
        id=_id(raw, at),
        table=_ref(raw, "table", at),
        index=_int(raw, "index", at, required=True),
        role=_enum(raw, "role", at, PartitionRole, PartitionRole.DATA),
        size=_size(raw, "size", at),
        label=_str(raw, "label", at, ""),
    )


def _luks(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "backing", "name"})
    return Luks(id=_id(raw, at), backing=_ref(raw, "backing", at), name=_str(raw, "name", at, required=True))


def _raid(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "members", "level", "name", "metadata"})
    return MdRaid(
        id=_id(raw, at),
        members=_refs(raw, "members", at),
        level=_enum(raw, "level", at, RaidLevel, RaidLevel.RAID1),
        name=_str(raw, "name", at, required=True),
        metadata=_enum(raw, "metadata", at, RaidMetadata, RaidMetadata.V1_2),
    )


def _volume_group(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "members", "name"})
    return VolumeGroup(id=_id(raw, at), members=_refs(raw, "members", at), name=_str(raw, "name", at, required=True))


def _logical_volume(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "group", "name", "size"})
    return LogicalVolume(
        id=_id(raw, at), group=_ref(raw, "group", at), name=_str(raw, "name", at, required=True), size=_size(raw, "size", at)
    )


def _zpool(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "vdevs", "name", "encrypted"})
    return ZfsPool(
        id=_id(raw, at),
        vdevs=_refs(raw, "vdevs", at),
        name=_str(raw, "name", at, required=True),
        encrypted=_bool(raw, "encrypted", at, False),
    )


def _dataset(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "pool", "name"})
    return ZfsDataset(id=_id(raw, at), pool=_ref(raw, "pool", at), name=_str(raw, "name", at, required=True))


def _filesystem(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "device", "type", "label"})
    return Filesystem(
        id=_id(raw, at),
        device=_ref(raw, "device", at),
        kind=_enum(raw, "type", at, FilesystemType, required=True),
        label=_str(raw, "label", at, ""),
    )


def _subvolume(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "filesystem", "name"})
    return Subvolume(id=_id(raw, at), filesystem=_ref(raw, "filesystem", at), name=_str(raw, "name", at, required=True))


def _swap(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "device"})
    return Swap(id=_id(raw, at), device=_ref(raw, "device", at))


def _mountpoint(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "source", "path", "options"})
    path = _str(raw, "path", at, required=True)
    if not path.startswith("/"):
        raise ConfigError(f"{at}.path is {path!r}; a mountpoint must be absolute")
    return Mountpoint(
        id=_id(raw, at),
        source=_ref(raw, "source", at),
        path=PurePosixPath(path),
        options=_strings(raw, "options", at, ()),
    )


_NODES = {
    "existing": _existing,
    "table": _table_node,
    "partition": _partition,
    "luks": _luks,
    "raid": _raid,
    "volume-group": _volume_group,
    "logical-volume": _logical_volume,
    "zpool": _zpool,
    "dataset": _dataset,
    "filesystem": _filesystem,
    "subvolume": _subvolume,
    "swap": _swap,
    "mountpoint": _mountpoint,
}


def _reject_unknown(raw: Mapping[str, Any], at: str, known: set[str]) -> None:
    unknown = sorted(set(raw) - known)
    if unknown:
        where = at or "the top level"
        raise ConfigError(f"{where} has unknown keys: {', '.join(unknown)}")


def _at(key: str, at: str) -> str:
    return f"{at}.{key}" if at else key


def _get(raw: Mapping[str, Any], key: str, at: str, required: bool) -> Any:
    if key in raw:
        return raw[key]
    if required:
        raise ConfigError(f"{_at(key, at)} is required")
    return None


def _str(raw: Mapping[str, Any], key: str, at: str, default: str = "", *, required: bool = False) -> str:
    value = _get(raw, key, at, required)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{_at(key, at)} must be a string, got {type(value).__name__}")
    return value


def _bool(raw: Mapping[str, Any], key: str, at: str, default: bool = False, *, required: bool = False) -> bool:
    value = _get(raw, key, at, required)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{_at(key, at)} must be true or false, got {type(value).__name__}")
    return value


def _int(raw: Mapping[str, Any], key: str, at: str, default: int = 0, *, required: bool = False) -> int:
    value = _get(raw, key, at, required)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{_at(key, at)} must be an integer, got {type(value).__name__}")
    return value


def _strings(raw: Mapping[str, Any], key: str, at: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = _get(raw, key, at, required=False)
    if value is None:
        return default
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{_at(key, at)} must be a list of strings")
    return tuple(value)


def _size(raw: Mapping[str, Any], key: str, at: str) -> Size | None:
    value = _get(raw, key, at, required=False)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{_at(key, at)} must be a size literal such as \"512MiB\"")
    return Size.parse(value)


def _enum(raw: Mapping[str, Any], key: str, at: str, kind: type[E], default: E | None = None, *, required: bool = False) -> E:
    value = _get(raw, key, at, required or default is None)
    if value is None:
        if default is None:
            raise ConfigError(f"{_at(key, at)} is required")
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{_at(key, at)} must be a string")
    for member in kind:
        if member.value == value:
            return member
    allowed = ", ".join(str(member.value) for member in kind)
    raise ConfigError(f"{_at(key, at)} is {value!r}; expected one of {allowed}")


def _table(raw: Mapping[str, Any], key: str, at: str, *, required: bool = False) -> Mapping[str, Any]:
    value = _get(raw, key, at, required)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{_at(key, at)} must be a table")
    return value


def _tables(raw: Mapping[str, Any], key: str, at: str) -> Sequence[Mapping[str, Any]]:
    value = _get(raw, key, at, required=False)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ConfigError(f"{_at(key, at)} must be a list of tables")
    return value


def _id(raw: Mapping[str, Any], at: str) -> DeviceId:
    return DeviceId(_str(raw, "id", at, required=True))


def _ref(raw: Mapping[str, Any], key: str, at: str) -> DeviceId:
    return DeviceId(_str(raw, key, at, required=True))


def _refs(raw: Mapping[str, Any], key: str, at: str) -> tuple[DeviceId, ...]:
    values = _strings(raw, key, at)
    if not values:
        raise ConfigError(f"{_at(key, at)} must list at least one device id")
    return tuple(DeviceId(value) for value in values)
