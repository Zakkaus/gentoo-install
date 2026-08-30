# SPDX-License-Identifier: GPL-2.0-or-later
"""TOML to `InstallConfig`.

Nothing here touches the machine. A device selector is carried through as the
string the user wrote; `exec/probe.py` resolves it and `validate.py` reports what
is missing. That keeps a configuration file checkable on any machine.
"""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Final, Mapping, Sequence, TypeVar

from decimal import Decimal

from ..errors import ConfigError, InvalidSize
from . import config as model_config
from . import sshkey
from . import templates
from .config import (
    CONFIG_VERSION,
    Binhost,
    BinhostChannel,
    Bootloader,
    BootloaderConfig,
    ConsoleFontSize,
    DiskConfig,
    DiskMode,
    ImageFormat,
    Firmware,
    Firewall,
    InitSystem,
    InstallConfig,
    KernelConfig,
    KernelSource,
    Keywords,
    Logger,
    MirrorConfig,
    GentooZhMirror,
    MirrorRegion,
    FirstBoot,
    Networking,
    Overlay,
    RemoteUnlock,
    Sync,
    PackagesConfig,
    PortageConfig,
    ProxyKind,
    ProxyConfig,
    SystemConfig,
    User,
)
from .device import (
    Share,
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
    ZfsTopology,
)
from .size import Size

#: How a partition asks for whatever the others leave. Spelled out rather
#: than left as an omitted key: a partition with no `size` reads like one
#: whose size was forgotten, and it is a decision.
REST: Final[str] = "rest"

E = TypeVar("E", bound=Enum)


def parse(raw: Mapping[str, Any]) -> InstallConfig:
    version = _int(raw, model_config.CONFIG_VERSION_KEY, "", default=CONFIG_VERSION)
    if version > CONFIG_VERSION:
        raise ConfigError(
            f"config_version {version} is newer than this installer understands "
            f"({CONFIG_VERSION}); upgrade the installer"
        )
    if version < CONFIG_VERSION:
        raise ConfigError(f"config_version {version} has no migration to {CONFIG_VERSION}")

    _reject_unknown(
        raw,
        "",
        {model_config.CONFIG_VERSION_KEY, *model_config.PERSISTED_SECTIONS},
    )
    return InstallConfig(
        config_version=version,
        disk=_disk(_table(raw, "disk", "", required=True), "disk"),
        proxy=_proxy(_table(raw, "proxy", ""), "proxy"),
        system=_system(_table(raw, "system", ""), "system"),
        portage=_portage(_table(raw, "portage", ""), "portage"),
        kernel=_kernel(_table(raw, "kernel", ""), "kernel"),
        bootloader=_bootloader(_table(raw, "bootloader", ""), "bootloader"),
        packages=_packages(_table(raw, "packages", ""), "packages"),
    )


def _proxy(raw: Mapping[str, Any], at: str) -> ProxyConfig:
    _reject_unknown(raw, at, {"kind", "host", "port", "username", "password", "bypass"})
    default = ProxyConfig()
    kind = _enum(raw, "kind", at, ProxyKind, default.kind)
    host = _str(raw, "host", at, default.host).strip()
    port = _int(raw, "port", at, default.port)
    username = _str(raw, "username", at, default.username)
    password = _str(raw, "password", at, default.password)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in password):
        raise ConfigError(f"{_at('password', at)} contains control characters")
    if host and any(char.isspace() for char in host):
        raise ConfigError(f"{_at('host', at)} must not contain spaces")
    if port < 0 or port > 65535:
        raise ConfigError(f"{_at('port', at)} must be between 1 and 65535")
    if host and port == 0:
        raise ConfigError(f"{_at('port', at)} must be between 1 and 65535")
    bypass = _strings(raw, "bypass", at, default.bypass)
    if any(not host.strip() or any(char.isspace() for char in host) for host in bypass):
        raise ConfigError(f"{_at('bypass', at)} must contain non-empty host names")
    if not host and (port or username or password or bypass):
        raise ConfigError(f"{_at('host', at)} is required when proxy fields are set")
    return ProxyConfig(kind=kind, host=host, port=port, username=username, password=password,
                       bypass=tuple(host.strip() for host in bypass))


def _system(raw: Mapping[str, Any], at: str) -> SystemConfig:
    _reject_unknown(
        raw,
        at,
        {
            "hostname", "timezone", "locales", "locale", "keymap", "console_cjk",
            "console_font", "init", "logger", "cron", "sshd", "sshd_password_login",
            "users",
            "root_password_hash",
            "zram", "hardware_clock_utc", "networking", "firewall", "keymap_initramfs",
            "first_boot",
            "interface", "addresses", "gateways", "dns", "authorized_keys",
            "sshd_root_login",
        },
    )
    default = SystemConfig()
    return SystemConfig(
        hostname=_str(raw, "hostname", at, default.hostname),
        timezone=_str(raw, "timezone", at, default.timezone),
        locales=_strings(raw, "locales", at, default.locales),
        locale=_str(raw, "locale", at, default.locale),
        keymap=_str(raw, "keymap", at, default.keymap),
        keymap_initramfs=_str(raw, "keymap_initramfs", at, default.keymap_initramfs),
        interface=_str(raw, "interface", at, default.interface),
        addresses=_strings(raw, "addresses", at, default.addresses),
        gateways=_strings(raw, "gateways", at, default.gateways),
        dns=_strings(raw, "dns", at, default.dns),
        authorized_keys=tuple(
            sshkey.check(key)
            for key in _strings(raw, "authorized_keys", at, default.authorized_keys)
        ),
        console_cjk=_bool(raw, "console_cjk", at, default.console_cjk),
        console_font=_enum(raw, "console_font", at, ConsoleFontSize, default.console_font),
        init=_enum(raw, "init", at, InitSystem, default.init),
        root_password_hash=_str(raw, "root_password_hash", at, default.root_password_hash),
        logger=_enum(raw, "logger", at, Logger, default.logger),
        cron=_bool(raw, "cron", at, default.cron),
        sshd=_bool(raw, "sshd", at, default.sshd),
        sshd_password_login=_bool(
            raw, "sshd_password_login", at, default.sshd_password_login
        ),
        sshd_root_login=_bool(raw, "sshd_root_login", at, default.sshd_root_login),
        networking=_enum(raw, "networking", at, Networking, default.networking),
        firewall=_enum(raw, "firewall", at, Firewall, default.firewall),
        first_boot=_first_boot(_table(raw, "first_boot", at), f"{at}.first_boot"),
        zram=_size(raw, "zram", at),
        hardware_clock_utc=_bool(raw, "hardware_clock_utc", at, default.hardware_clock_utc),
        users=tuple(_user(entry, f"{at}.users[{n}]") for n, entry in enumerate(_tables(raw, "users", at))),
    )


def _user(raw: Mapping[str, Any], at: str) -> User:
    _reject_unknown(raw, at, {"name", "groups", "shell", "sudo", "password_hash"})
    default = User(name="")
    name = _str(raw, "name", at, required=True)
    if not name:
        raise ConfigError(f"{at}.name is empty")
    return User(
        name=name,
        groups=_strings(raw, "groups", at, default.groups),
        shell=_str(raw, "shell", at, default.shell),
        sudo=_bool(raw, "sudo", at, default.sudo),
        password_hash=_str(raw, "password_hash", at, default.password_hash),
    )


def _portage(raw: Mapping[str, Any], at: str) -> PortageConfig:
    _reject_unknown(
        raw,
        at,
        {
            "profile", "keywords", "sync", "testing_packages", "makeopts",
            "common_flags", "use", "video_cards", "l10n", "accept_license",
            "cpu_flags", "build_in_ram", "input_devices", "mirrors", "binhost",
            "overlays", "repositories",
        },
    )
    default = PortageConfig()
    return PortageConfig(
        profile=_str(raw, "profile", at, default.profile),
        keywords=_enum(raw, "keywords", at, Keywords, default.keywords),
        sync=_enum(raw, "sync", at, Sync, default.sync),
        testing_packages=_strings(raw, "testing_packages", at, default.testing_packages),
        repositories=_strings(raw, "repositories", at, default.repositories),
        makeopts=_str(raw, "makeopts", at, default.makeopts),
        common_flags=_str(raw, "common_flags", at, default.common_flags),
        use=_strings(raw, "use", at, default.use),
        video_cards=_strings(raw, "video_cards", at, default.video_cards),
        l10n=_strings(raw, "l10n", at, default.l10n),
        input_devices=_strings(raw, "input_devices", at, default.input_devices),
        accept_license=_strings(raw, "accept_license", at, default.accept_license),
        cpu_flags=_strings(raw, "cpu_flags", at, default.cpu_flags),
        build_in_ram=_size(raw, "build_in_ram", at),
        mirrors=_mirrors(_table(raw, "mirrors", at), f"{at}.mirrors"),
        binhost=_binhost(_table(raw, "binhost", at), f"{at}.binhost"),
        overlays=tuple(
            _overlay(entry, f"{at}.overlays[{n}]") for n, entry in enumerate(_tables(raw, "overlays", at))
        ),
    )


def _mirrors(raw: Mapping[str, Any], at: str) -> MirrorConfig:
    _reject_unknown(
        raw,
        at,
        {
            "region", "speed_test", "distfiles", "repo_sync_uri", "site",
            "gentoo_distfiles",
            "gentoo_zh", "gentoo_zh_distfiles",
        },
    )
    default = MirrorConfig()
    return MirrorConfig(
        region=_enum(raw, "region", at, MirrorRegion, default.region),
        speed_test=_bool(raw, "speed_test", at, default.speed_test),
        distfiles=_strings(raw, "distfiles", at, default.distfiles),
        repo_sync_uri=_str(raw, "repo_sync_uri", at, default.repo_sync_uri),
        site=_str(raw, "site", at, default.site),
        gentoo_distfiles=_bool(raw, "gentoo_distfiles", at, default.gentoo_distfiles),
        gentoo_zh=_enum(raw, "gentoo_zh", at, GentooZhMirror, default.gentoo_zh),
        gentoo_zh_distfiles=_bool(
            raw, "gentoo_zh_distfiles", at, default.gentoo_zh_distfiles
        ),
    )


def _remote_unlock(raw: Mapping[str, Any], at: str) -> RemoteUnlock:
    _reject_unknown(raw, at, {"enabled", "port", "address", "gateway", "interface"})
    default = RemoteUnlock()
    return RemoteUnlock(
        enabled=_bool(raw, "enabled", at, default.enabled),
        port=_int(raw, "port", at, default.port),
        address=_str(raw, "address", at, default.address),
        gateway=_str(raw, "gateway", at, default.gateway),
        interface=_str(raw, "interface", at, default.interface),
    )


def _binhost(raw: Mapping[str, Any], at: str) -> Binhost:
    _reject_unknown(raw, at, {"official", "community", "subarch", "url"})
    default = Binhost()
    return Binhost(
        official=_bool(raw, "official", at, default.official),
        community=_enum(raw, "community", at, BinhostChannel, default.community),
        subarch=_str(raw, "subarch", at, default.subarch),
        url=_str(raw, "url", at, default.url),
    )


def _overlay(raw: Mapping[str, Any], at: str) -> Overlay:
    _reject_unknown(raw, at, {"name", "sync_uri"})
    return Overlay(name=_str(raw, "name", at, required=True), sync_uri=_str(raw, "sync_uri", at, required=True))


def _kernel(raw: Mapping[str, Any], at: str) -> KernelConfig:
    _reject_unknown(
        raw, at, {"source", "package", "version", "dracut_modules", "remote_unlock"}
    )
    default = KernelConfig()
    return KernelConfig(
        source=_enum(raw, "source", at, KernelSource, default.source),
        package=_str(raw, "package", at, default.package),
        version=_str(raw, "version", at, default.version),
        dracut_modules=_strings(raw, "dracut_modules", at, default.dracut_modules),
        remote_unlock=_remote_unlock(
            _table(raw, "remote_unlock", at), f"{at}.remote_unlock"
        ),
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
    _reject_unknown(raw, at, {"desktop", "applications", "extra", "graphics", "display_manager"})
    default = PackagesConfig()
    return PackagesConfig(
        desktop=_str(raw, "desktop", at, default.desktop),
        applications=_strings(raw, "applications", at, default.applications),
        extra=_strings(raw, "extra", at, default.extra),
        graphics=_strings(raw, "graphics", at, default.graphics),
        display_manager=_str(raw, "display_manager", at, default.display_manager),
    )

def _disk(raw: Mapping[str, Any], at: str) -> DiskConfig:
    _reject_unknown(
        raw,
        at,
        {
            "root",
            "devices",
            "simple",
            "mode",
            "image",
            "size",
            "wipe",
            "source",
            "source_format",
            "destination",
        },
    )
    mode = _enum(raw, "mode", at, DiskMode, DiskMode.PARTITION)
    simple = raw.get("simple")
    if simple is not None:
        # Refused rather than merged: with both written, one of them decides
        # and nothing on the page says which.
        if raw.get("devices"):
            raise ConfigError(
                f"{at} carries both `simple` and `devices`; a layout is written one way "
                "or the other"
            )
        if raw.get("root"):
            raise ConfigError(f"{at}.root is derived from `simple` and is not written beside it")
        chosen = _choice(simple, f"{at}.simple")
        graph, root = templates.build(chosen)
        return DiskConfig(
            graph=graph,
            root=root,
            mode=mode,
            simple=chosen,
            image=_str(raw, "image", at),
            size=_size(raw, "size", at),
            wipe=_bool(raw, "wipe", at, False),
            source=_str(raw, "source", at),
            source_format=_enum(raw, "source_format", at, ImageFormat, ImageFormat.RAW),
            destination=_str(raw, "destination", at),
        )
    devices = _tables(raw, "devices", at)
    nodes = [_node(entry, f"{at}.devices[{n}]") for n, entry in enumerate(devices)]
    if mode in (DiskMode.PARTITION, DiskMode.IMAGE) and not nodes:
        raise ConfigError(f"{at}.devices is empty; nothing to install onto")
    return DiskConfig(
        graph=DeviceGraph.build(nodes),
        root=DeviceId(_str(raw, "root", at)),
        mode=mode,
        image=_str(raw, "image", at),
        size=_size(raw, "size", at),
        wipe=_bool(raw, "wipe", at, False),
        source=_str(raw, "source", at),
        source_format=_enum(raw, "source_format", at, ImageFormat, ImageFormat.RAW),
        destination=_str(raw, "destination", at),
    )

def _choice(raw: Any, at: str) -> templates.Choice:
    """The hand-written form of a whole-disk layout.

    Every field is `templates.Choice`'s own, so nothing here decides what a
    value means: `templates.build` is the one expansion and this only reads.
    """
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{at} is not a table")
    _reject_unknown(
        raw,
        at,
        {"disk", "layout", "firmware", "table", "filesystem", "swap", "passphrase_file", "pool"},
    )
    layout = _enum(raw, "layout", at, templates.Layout, templates.Layout.WHOLE_DISK)
    if layout is templates.Layout.REUSE:
        raise ConfigError(
            f"{at}.layout is 'reuse', which names partitions that already exist; "
            "write those as `devices`"
        )
    return templates.Choice(
        disk=_str(raw, "disk", at, required=True),
        layout=layout,
        firmware=_enum(raw, "firmware", at, Firmware, Firmware.UEFI),
        table=_enum(raw, "table", at, TableType, None) if raw.get("table") else None,
        filesystem=_enum(raw, "filesystem", at, FilesystemType, FilesystemType.XFS),
        swap=_size(raw, "swap", at),
        passphrase_file=_str(raw, "passphrase_file", at),
        pool=_str(raw, "pool", at) or "rpool",
    )


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
    _reject_unknown(raw, at, {"kind", "id", "disk", "table", "create", "remove"})
    return PartitionTable(
        id=_id(raw, at),
        disk=_ref(raw, "disk", at),
        table=_enum(raw, "table", at, TableType, TableType.GPT),
        create=_bool(raw, "create", at, True),
        remove=_indexes(raw, "remove", at),
    )


def _indexes(raw: Mapping[str, Any], key: str, at: str) -> tuple[int, ...]:
    """Entry numbers to remove from a table that is not written from scratch.

    `bool` is an `int` in Python, so `remove = [true]` passed the type check and
    deleted partition 1. Numbering starts at 1 and a repeat asks twice for the
    same deletion, so both are refused rather than carried to `sgdisk`.
    """
    value = raw.get(key, [])
    wrong = f"{at}.{key} must be a list of partition numbers, each 1 or greater and named once"
    if not isinstance(value, list):
        raise ConfigError(wrong)
    for one in value:
        if isinstance(one, bool) or not isinstance(one, int) or one < 1:
            raise ConfigError(wrong)
    if len(set(value)) != len(value):
        raise ConfigError(wrong)
    return tuple(int(one) for one in value)


def _partition(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(
        raw, at, {"kind", "id", "table", "index", "role", "size", "label", "min", "max"}
    )
    absolute, share = _extent(raw, at)
    return Partition(
        id=_id(raw, at),
        table=_ref(raw, "table", at),
        index=_int(raw, "index", at, required=True),
        role=_enum(raw, "role", at, PartitionRole, PartitionRole.DATA),
        size=absolute,
        label=_str(raw, "label", at, ""),
        share=share,
    )


def _extent(raw: Mapping[str, Any], at: str) -> tuple[Size | None, Share | None]:
    """How much of the table this partition asks for.

    Three spellings and one field: `"20G"` is an absolute size, `"40%"` is a
    share of what the absolute ones leave, and `"rest"` is whatever remains.
    An omitted `size` still means `rest`, because every configuration written
    before this existed says it that way.

    `min` and `max` bound a share and are refused beside an absolute size:
    one extent described by three numbers is one nobody can read back.
    """
    written = _get(raw, "size", at, required=False)
    lowest = _size(raw, "min", at)
    highest = _size(raw, "max", at)
    bounded = lowest is not None or highest is not None
    if written is not None and not isinstance(written, str):
        raise ConfigError(
            f'{_at("size", at)} must be a size literal such as "512MiB", a share such '
            f'as "40%", or "rest"'
        )
    if written is not None and not written.endswith("%") and written != REST:
        if bounded:
            raise ConfigError(
                f"{_at('size', at)} is an absolute size, so `min` and `max` say nothing "
                "it does not already say; bound a share or a rest instead"
            )
        return Size.parse(written), None
    if written is None and not bounded:
        # Nothing written is `share=None`, which is what `templates.build`
        # constructs for the same partition. Answering `Share()` instead made
        # every template fail export-then-import equality: the two mean the
        # same to `takes_the_rest` and compare unequal.
        return None, None
    percent: Decimal | None = None
    if written is not None and written.endswith("%"):
        try:
            percent = Decimal(written[:-1])
        except ArithmeticError as error:
            raise ConfigError(f"{_at('size', at)}: {written!r} is not a share") from error
    try:
        return None, Share(percent=percent, minimum=lowest, maximum=highest)
    except InvalidSize as error:
        raise ConfigError(f"{_at('size', at)}: {error}") from error


def _luks(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "backing", "name", "passphrase_file"})
    return Luks(
        id=_id(raw, at),
        backing=_ref(raw, "backing", at),
        name=_str(raw, "name", at, required=True),
        passphrase_file=_str(raw, "passphrase_file", at, ""),
    )


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
    _reject_unknown(
        raw, at, {"kind", "id", "vdevs", "name", "topology", "encrypted", "passphrase_file"}
    )
    passphrase_file = _str(raw, "passphrase_file", at, "")
    return ZfsPool(
        id=_id(raw, at),
        vdevs=_refs(raw, "vdevs", at),
        name=_str(raw, "name", at, required=True),
        topology=_enum(raw, "topology", at, ZfsTopology, ZfsTopology.STRIPE),
        encrypted=_bool(raw, "encrypted", at, False) or bool(passphrase_file),
        passphrase_file=passphrase_file,
    )


def _dataset(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "pool", "name"})
    return ZfsDataset(id=_id(raw, at), pool=_ref(raw, "pool", at), name=_str(raw, "name", at, required=True))


def _filesystem(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "device", "type", "label", "create"})
    return Filesystem(
        id=_id(raw, at),
        device=_ref(raw, "device", at),
        kind=_enum(raw, "type", at, FilesystemType, required=True),
        label=_str(raw, "label", at, ""),
        create=_bool(raw, "create", at, True),
    )


def _subvolume(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "filesystem", "name", "create"})
    return Subvolume(
        id=_id(raw, at),
        filesystem=_ref(raw, "filesystem", at),
        name=_str(raw, "name", at, required=True),
        create=_bool(raw, "create", at, True),
    )


def _swap(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "device"})
    return Swap(id=_id(raw, at), device=_ref(raw, "device", at))


def _mountpoint(raw: Mapping[str, Any], at: str) -> Node:
    _reject_unknown(raw, at, {"kind", "id", "source", "path", "options"})
    path = _str(raw, "path", at, required=True)
    if not path.startswith("/"):
        raise ConfigError(f"{at}.path is {path!r}; a mountpoint must be absolute")
    # Absolute is not inside: `/../outside` reaches `/mnt/outside` once it is
    # joined to the target, and the plan would `mkdir` and `mount` there.
    if ".." in PurePosixPath(path).parts:
        raise ConfigError(
            f"{at}.path is {path!r}; a mountpoint cannot climb out of the target with `..`"
        )
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


def _first_boot(raw: Mapping[str, Any], at: str) -> FirstBoot:
    _reject_unknown(raw, at, {"commands", "url"})
    default = FirstBoot()
    url = _str(raw, "url", at, default.url).strip()
    if url and not url.startswith(("http://", "https://")):
        raise ConfigError(f"{_at('url', at)} must be an http or https address")
    return FirstBoot(commands=_strings(raw, "commands", at, default.commands), url=url)


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
