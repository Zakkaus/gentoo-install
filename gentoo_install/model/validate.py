# SPDX-License-Identifier: GPL-2.0-or-later
"""Checks that need more than one field, run before anything touches a disk.

Every problem is collected and reported together: fixing one rule per run means
one more failed run to learn about the next one.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, fields
from pathlib import PurePosixPath
from typing import Collection, Final, Mapping
from urllib.parse import urlparse

from ..errors import CommandFailed, ValidationFailed
from . import compat
from .config import (
    BootloaderConfig,
    DiskConfig,
    DiskMode,
    InitSystem,
    InstallConfig,
    KernelConfig,
    MemoryLaunch,
    MemoryMode,
    PackagesConfig,
    PortageConfig,
    SystemConfig,
)
from .device import (
    takes_the_rest,
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    FilesystemType,
    LogicalVolume,
    MdRaid,
    Mountpoint,
    Node,
    Partition,
    PartitionTable,
    StorageFacts,
    TableType,
    ZfsPool,
    ZfsTopology,
)
from .size import Size

_ROOT = PurePosixPath("/")


@dataclass(frozen=True)
class ProbedProfile:
    """One row from `eselect profile list`."""

    path: str
    stability: str
    current: bool = False


_PROFILE_ROW: Final[re.Pattern[str]] = re.compile(
    r"^\s*\[\d+\]\s+(\S+)\s+\(([^()\s]+)\)(\s+\*)?\s*$"
)
_L10N_TAG: Final[re.Pattern[str]] = re.compile(
    r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?"
    r"(?:-(?:[0-9][a-z0-9]{3}|[a-z][a-z0-9]{4,7}))*$"
)

#: The target's locale is not checked here. `docs/plan.md` settles that the
#: installed system's language is a separate choice from the interface's, and
#: the set `locale-gen` can produce belongs to the target rather than to this
#: installer; `plan/system.GenerateLocales` reads `locale --all-locales` after
#: generating and raises `LocaleMissing` for anything absent, which is the
#: check that can actually see the answer.

#: Closed system values checked by this module. Keymaps are not here: the
#: target's kbd version owns that namespace, not the installer.
CLOSED_SYSTEM_FIELDS: Final[frozenset[str]] = frozenset(
    {"timezone", "hostname", "first_boot.url"}
)

#: `extra` is intentionally absent: it is a sequence of Portage atoms, not
#: package-group names from this installer's catalog.
PACKAGE_GROUP_FIELDS: Final[tuple[str, ...]] = (
    "desktop", "applications", "graphics", "display_manager"
)

HTTP_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_HOSTNAME_LABEL: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)
_HOSTNAME_MAXIMUM: Final[int] = 253

#: `[disk]` keys valid after `mode` selects the operation. `mode` itself
#: selects this table, so it is deliberately not a member of any row.
DISK_MODE_FIELDS: Final[Mapping[DiskMode, frozenset[str]]] = {
    DiskMode.PARTITION: frozenset({"devices", "root", "simple"}),
    DiskMode.IN_PLACE: frozenset(),
    DiskMode.IMAGE: frozenset({"devices", "root", "simple", "image", "size", "wipe"}),
    DiskMode.DD: frozenset({"source", "source_format", "destination"}),
}


def _configured_disk_fields(disk: DiskConfig) -> frozenset[str]:
    """The TOML keys represented by the disk configuration."""
    if disk.simple is not None:
        structural = frozenset({"simple"})
    else:
        structural = frozenset(
            name
            for name, present in (
                ("devices", bool(disk.graph.nodes)),
                ("root", bool(disk.root)),
            )
            if present
        )
    values = {
        field.name
        for field in fields(disk)
        if field.name not in {"graph", "root", "simple", "mode"}
        and getattr(disk, field.name) != field.default
    }
    return structural | frozenset(values)


def parse_profile_list(
    output: str,
    *,
    source: str = "`eselect profile list`",
) -> tuple[ProbedProfile, ...]:
    """Parse the numbered rows emitted by eselect on the repository in use."""
    found: list[ProbedProfile] = []
    for line in output.splitlines():
        if not line.strip() or line.strip() == "Available profile symlink targets:":
            continue
        matched = _PROFILE_ROW.fullmatch(line)
        if matched is None:
            raise CommandFailed(
                f"{source} could not be read: unrecognised profile row {line.strip()!r}"
            )
        found.append(
            ProbedProfile(
                path=matched.group(1),
                stability=matched.group(2),
                current=matched.group(3) is not None,
            )
        )
    if not found:
        raise CommandFailed(f"{source} could not be read: no profile rows were returned")
    return tuple(found)


class _ProfilesNotRead:
    pass


_PROFILES_NOT_READ: Final[_ProfilesNotRead] = _ProfilesNotRead()


class _ShippedValuesNotRead:
    pass


_SHIPPED_VALUES_NOT_READ: Final[_ShippedValuesNotRead] = _ShippedValuesNotRead()


class KernelCeiling(str):
    """A read kernel ceiling, with unknown kept distinct from no ceiling."""

    maximum: str | None

    def __new__(cls, maximum: str | None) -> KernelCeiling:
        normalised = maximum.strip() if maximum else None
        if normalised is not None and re.fullmatch(r"\d+(?:\.\d+)+", normalised) is None:
            normalised = None
        ceiling = super().__new__(cls, normalised or "unknown")
        ceiling.maximum = normalised
        return ceiling


def zfs_kernel_ceiling(cpv: str, rdepend: str) -> KernelCeiling:
    """Derive the ZFS ceiling from the dependency Portage cached for its ebuild."""
    bounds = re.findall(r"<virtual/dist-kernel-(\d+)\.(\d+)(?=[\s)])", rdepend)
    if len(bounds) != 1:
        raise ValueError(f"Portage metadata for {cpv} has no single ZFS dist-kernel ceiling")
    major, exclusive_minor = (int(part) for part in bounds[0])
    if exclusive_minor == 0:
        raise ValueError(f"Portage metadata for {cpv} has an invalid ZFS dist-kernel ceiling")
    return KernelCeiling(f"{major}.{exclusive_minor - 1}")


def zfs_kernel_version_problem(version: str, ceiling: KernelCeiling) -> str | None:
    """Return the incompatibility between a selected kernel and a ZFS ceiling."""
    if ceiling.maximum is None:
        return "the sys-fs/zfs kernel ceiling could not be read, so this ZFS install cannot establish that its kernel will build the module"
    # An unpinned kernel is Portage's to resolve: `sys-fs/zfs` carries
    # `dist-kernel-cap? ( dist-kernel? ( <virtual/dist-kernel-7.1 ) )`, so the
    # ceiling is enforced by the dependency rather than by a version here.
    if not version:
        return None
    limit = _numeric_version(ceiling.maximum)
    selected = _numeric_version(version)
    if not limit or not selected:
        return f"kernel version {version!r} cannot be compared with the sys-fs/zfs ceiling {ceiling}"
    if selected[: len(limit)] > limit:
        return f"kernel {version} is above the sys-fs/zfs ceiling {ceiling.maximum}, so its ZFS module will not build"
    return None


class _ZfsKernelCeilingNotChecked:
    pass


_ZFS_KERNEL_CEILING_NOT_CHECKED: Final[_ZfsKernelCeilingNotChecked] = (
    _ZfsKernelCeilingNotChecked()
)

def validate(
    config: InstallConfig,
    *,
    storage_facts: StorageFacts | None = None,
    available_profiles: Collection[str] | None | _ProfilesNotRead = _PROFILES_NOT_READ,
    available_timezones: Collection[str] | _ShippedValuesNotRead = _SHIPPED_VALUES_NOT_READ,
    available_package_groups: Collection[str]
    | _ShippedValuesNotRead = _SHIPPED_VALUES_NOT_READ,
    zfs_kernel_max: str | None | _ZfsKernelCeilingNotChecked = (
        _ZFS_KERNEL_CEILING_NOT_CHECKED
    ),
    supports_v3: bool | None = None,
) -> None:
    if config.disk.mode is DiskMode.DD:
        # The proxy with it: `dd` reads a local path and never uses one, but a
        # host with no port is a configuration nobody can satisfy, and
        # refusing it in every mode except this one is a rule that fires by
        # accident.
        problems = [
            *_disk_mode_problems(config),
            *_proxy_problems(config),
            *_ignored_by_dd(config),
        ]
        if problems:
            raise ValidationFailed(
                "the configuration does not describe an installable system:\n  "
                + "\n  ".join(problems)
            )
        return
    graph_problems: list[str] = []
    if config.disk.mode in (DiskMode.PARTITION, DiskMode.IMAGE):
        graph_problems = [
            *_layout_problems(config),
            *compat.filesystem_label_problems(config),
            *root_size_problems(config),
            *_zfs_kernel_problems(config, zfs_kernel_max),
            *_reuse_problems(config),
            *_zapped_disk_problems(config),
            *_pool_problems(config),
            *_array_problems(config),
            *(rule.describe() for rule in compat.violations(config, storage_facts)),
        ]
    without_a_graph: list[str] = []
    if config.disk.layout_is_read_from_the_machine:
        without_a_graph = [
            rule.describe() for rule in compat.violations_without_a_graph(config)
        ]
    problems = [
        *_disk_mode_problems(config),
        *_proxy_problems(config),
        *graph_problems,
        *without_a_graph,
        *_profile_problems(config),
        *compat.unserved_profile_problems(config.portage.profile),
        *_repository_profile_problems(config.portage.profile, available_profiles),
        *_kernel_package_problems(config),
        *(problem.describe() for problem in compat.system_network_problems(config.system)),
        *(
            problem.describe()
            for problem in compat.remote_unlock_problems(
                enabled=config.kernel.remote_unlock.enabled,
                port=config.kernel.remote_unlock.port,
                address=config.kernel.remote_unlock.address,
                gateway=config.kernel.remote_unlock.gateway,
            )
        ),
        *_locale_problems(config),
        *_system_value_problems(config, available_timezones),
        *_package_group_problems(config, available_package_groups),
        *_l10n_problems(config),
        *_repository_name_problems(config),
        *compat.binhost_subarch_problems(config, supports_v3),
        *compat.mirror_site_problems(config),
    ]
    if problems:
        raise ValidationFailed(
            "the configuration does not describe an installable system:\n  " + "\n  ".join(problems)
        )

def validate_memory_launch(config: InstallConfig, launch: MemoryLaunch) -> None:
    """Refuse a memory environment that cannot do what was asked of it.

    A payload key or password authenticates to sshd during the memory boot.
    `--ssh-port` applies to the installed system, so it needs one of those
    credentials for the initial daemon on port 22.
    """
    problems: list[str] = []
    if launch.mode is MemoryMode.LOWRAM and config.disk.graph.of_type(ZfsPool):
        problems.append(
            "the layout needs ZFS, the Alpine netboot kernel has no zfs.ko, "
            "and --ram is the mode whose Gentoo CJK ISO carries it"
        )
    if launch.ssh_port is not None and (
        refused := compat.port_problem("--ssh-port", launch.ssh_port)
    ):
        problems.append(refused)
    if "\n" in launch.root_password or "\r" in launch.root_password:
        problems.append("--root-password cannot contain a newline")
    if launch.ssh_port is not None and not (launch.ssh_key or launch.root_password):
        problems.append(
            "--ssh-port takes effect only after the installer writes it, so "
            "--ssh-key or --root-password is needed for the initial sshd"
        )
    if problems:
        raise ValidationFailed(
            "the memory environment cannot start:\n  " + "\n  ".join(problems)
        )


def _ignored_by_dd(config: InstallConfig) -> list[str]:
    """Sections a dd run cannot act on.

    Its whole plan is one `WriteImage`, so a configuration naming a hostname,
    a user and a desktop was accepted and produced an image copy: the operator
    described a machine and got none of it. Compared against the defaults,
    because a file that omits a section still carries one.
    """
    described = (
        ("system", config.system, SystemConfig()),
        ("packages", config.packages, PackagesConfig()),
        ("portage", config.portage, PortageConfig()),
        ("kernel", config.kernel, KernelConfig()),
        ("bootloader", config.bootloader, BootloaderConfig()),
    )
    return [
        f"[{name}] is not allowed in dd mode: the image is written as it is and "
        "nothing in it is configured"
        for name, given, default in described
        if given != default
    ]


def _disk_mode_problems(config: InstallConfig) -> list[str]:
    disk = config.disk
    problems = [
        f"disk.{field} is not allowed in {disk.mode.value} mode"
        for field in sorted(_configured_disk_fields(disk) - DISK_MODE_FIELDS[disk.mode])
    ]
    if disk.mode is DiskMode.PARTITION:
        return problems
    if disk.mode is DiskMode.IMAGE:
        if not disk.image:
            problems.append("disk.image is required in image mode")
        elif disk.image.startswith("/dev/"):
            problems.append("disk.image must name a file rather than a physical disk")
        if disk.size is None:
            problems.append("disk.size is required in image mode")
        for device in disk.graph.of_type(Existing):
            if device.selector.startswith("/dev/"):
                problems.append(
                    f"disk.devices {device.id} selects a physical disk in image mode"
                )
            elif device.selector != disk.image:
                problems.append(
                    f"disk.devices {device.id} selects {device.selector!r} in image mode; "
                    "use disk.image rather than a physical disk"
                )
        return problems
    if disk.mode is DiskMode.DD:
        if not disk.source:
            problems.append("disk.source is required in dd mode")
        if not disk.destination:
            problems.append("disk.destination is required in dd mode")
        elif not disk.destination.startswith("/dev/"):
            problems.append("disk.destination must name a device under /dev")
        if disk.source and disk.source == disk.destination:
            problems.append("disk.source and disk.destination must differ")
        return problems
    return problems


def _proxy_problems(config: InstallConfig) -> list[str]:
    """Check proxy syntax for configurations built without the TOML parser."""
    proxy = config.proxy
    if not proxy.enabled:
        if proxy.bypass:
            return ["proxy bypass hosts require a proxy host"]
        if proxy.port or proxy.username or proxy.password:
            return ["proxy host is required when proxy fields are set"]
        return []
    if refused := compat.port_problem("proxy port", proxy.port):
        return [refused]
    if any(char.isspace() for char in proxy.host):
        return ["proxy host must not contain spaces"]
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in proxy.password):
        return ["proxy password contains control characters"]
    if any(not item.strip() or any(char.isspace() for char in item) for item in proxy.bypass):
        return ["proxy bypass hosts must be non-empty host names"]
    return []


def _zfs_kernel_problems(
    config: InstallConfig,
    ceiling: str | None | _ZfsKernelCeilingNotChecked,
) -> list[str]:
    """Refuse a ZFS root unless its selected kernel fits a read ceiling."""
    if isinstance(ceiling, _ZfsKernelCeilingNotChecked):
        return []
    if not config.disk.graph.of_type(ZfsPool):
        return []
    maximum = ceiling.maximum if isinstance(ceiling, KernelCeiling) else ceiling
    if maximum is None or not maximum.strip():
        return [zfs_kernel_version_problem(config.kernel.version, KernelCeiling(None)) or ""]
    problem = zfs_kernel_version_problem(config.kernel.version, KernelCeiling(maximum))
    return [problem] if problem is not None else []


def _numeric_version(version: str) -> tuple[int, ...]:
    """Numeric prefix components used by kernel and module version metadata."""
    found: list[int] = []
    for component in version.split("."):
        matched = re.match(r"\d+", component)
        if matched is None:
            break
        found.append(int(matched.group()))
    return tuple(found)


#: What `eselect repository` accepts as a name, and what `repos.conf` accepts
#: as a section. Checked here because `eselect` prints one line and exits 0
#: for a name it does not know: the install continues and the overlay is not
#: there.
_REPOSITORY_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_+.-]*")


def _repository_name_problems(config: InstallConfig) -> list[str]:
    problems = []
    shipped = {overlay.name for overlay in config.portage.overlays} | {"gentoo"}
    for name in config.portage.repositories:
        if _REPOSITORY_NAME.fullmatch(name) is None:
            problems.append(f"{name!r} is not a repository name")
        elif name in shipped:
            # Two `repos.conf` sections of the same name, and the later
            # sync-uri replaces the earlier one.
            problems.append(f"{name} is already configured with its own sync-uri")
    return problems


def _l10n_problems(config: InstallConfig) -> list[str]:
    return [
        f"L10N tag {tag!r} is not a hyphenated BCP 47 language tag"
        for tag in config.portage.l10n
        if _L10N_TAG.fullmatch(tag) is None
    ]


def _locale_problems(config: InstallConfig) -> list[str]:
    system = config.system
    problems: list[str] = []
    if system.locale not in system.locales:
        problems.append(f"system.locale is {system.locale!r}, which system.locales must include")
    return problems


def _system_value_problems(
    config: InstallConfig,
    available_timezones: Collection[str] | _ShippedValuesNotRead,
) -> list[str]:
    system = config.system
    problems: list[str] = []
    hostname = system.hostname
    if len(hostname) > _HOSTNAME_MAXIMUM or any(
        _HOSTNAME_LABEL.fullmatch(label) is None for label in hostname.split(".")
    ):
        problems.append(
            f"system.hostname is {hostname!r}, which is not an RFC 1123 hostname"
        )
    url = system.first_boot.url
    if url:
        try:
            parsed = urlparse(url)
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme not in HTTP_SCHEMES
            or not parsed.netloc
            or any(character.isspace() for character in url)
        ):
            problems.append(
                f"system.first_boot.url is {url!r}; expected an HTTP(S) URL with a host"
            )
    if isinstance(available_timezones, _ShippedValuesNotRead):
        return problems
    if system.timezone not in available_timezones:
        problems.append(
            f"system.timezone is {system.timezone!r}, which is not in data/timezones.txt"
        )
    return problems


def _package_group_problems(
    config: InstallConfig,
    available_package_groups: Collection[str] | _ShippedValuesNotRead,
) -> list[str]:
    if isinstance(available_package_groups, _ShippedValuesNotRead):
        return []
    packages = config.packages
    selections = (
        (packages.desktop,),
        packages.applications,
        packages.graphics,
        (packages.display_manager,),
    )
    return [
        f"packages.{field} is {name!r}, which is not a group in data/profiles or "
        "data/packages"
        for field, names in zip(PACKAGE_GROUP_FIELDS, selections)
        for name in names
        if name and name not in available_package_groups
    ]


def _pool_problems(config: InstallConfig) -> list[str]:
    """Every pool says how its vdevs are joined, and has enough of them.

    A bare list of devices is a stripe: `zpool create p a b` survives losing
    neither, and an operator who gave two disks almost always meant a mirror.
    The default is only safe for one device, so more than one has to say.
    """
    problems: list[str] = []
    for pool in config.disk.graph.of_type(ZfsPool):
        if pool.passphrase_file and not pool.encrypted:
            problems.append(
                f"{pool.id} sets passphrase_file but encrypted is false, so the pool "
                "would be created as plaintext"
            )
        if len(pool.vdevs) > 1 and pool.topology is ZfsTopology.STRIPE:
            problems.append(
                f"{pool.id} joins {len(pool.vdevs)} devices with no topology, which stripes "
                f"them; name one of {', '.join(one.value for one in ZfsTopology)}"
            )
        if len(pool.vdevs) < pool.topology.minimum:
            problems.append(
                f"{pool.id} is a {pool.topology.value} of {len(pool.vdevs)} devices and needs "
                f"at least {pool.topology.minimum}"
            )
    return problems


def _array_problems(config: InstallConfig) -> list[str]:
    """Every array has enough members for the level it names."""
    return [
        f"{array.id} is a {array.level.value} of {len(array.members)} devices and needs "
        f"at least {array.level.minimum}"
        for array in config.disk.graph.of_type(MdRaid)
        if len(array.members) < array.level.minimum
    ]




def _reuse_problems(config: InstallConfig) -> list[str]:
    """A filesystem the installer does not create has to sit on a device the
    installer does not create either.

    Reusing one on a partition this run makes, or on a disk this run wipes,
    describes keeping data that the same plan destroys a few operations earlier.
    """
    graph = config.disk.graph
    problems: list[str] = []
    for filesystem in graph.of_type(Filesystem):
        if filesystem.create:
            continue
        under = graph[filesystem.device]
        if not isinstance(under, Existing):
            problems.append(
                f"{filesystem.id} reuses the filesystem on {filesystem.device}, which this "
                f"install creates: nothing would be left to reuse"
            )
            continue
        if under.wipe:
            problems.append(
                f"{filesystem.id} reuses the filesystem on {under.id}, which is marked to be "
                f"wiped"
            )
    return problems


def _zapped_disk_problems(config: InstallConfig) -> list[str]:
    """A table this run writes has to sit on a disk this run wipes.

    `CreatePartitionTable` runs `sgdisk --zap-all` for `create`, which takes
    every entry with it. A configuration saying `wipe = false` on the disk and
    `create = true` on its table says both keep this disk and erase its table,
    and the erase is what happens: the operator reads one thing and the
    installer does the other, on the one operation that cannot be undone.
    """
    graph = config.disk.graph
    problems: list[str] = []
    for table in graph.of_type(PartitionTable):
        if not table.create:
            continue
        disk = graph[table.disk]
        if isinstance(disk, Existing) and not disk.wipe:
            problems.append(
                f"{table.id} is created on {disk.id}, which is marked not to be wiped: "
                f"creating a table runs `sgdisk --zap-all` and takes every partition "
                f"on it, so set `wipe = true` to say so or `create = false` to keep them"
            )
    return problems


def _package_name(atom: str) -> str:
    """`=cat/pkg-1.2:3` reduced to `cat/pkg`.

    Checked on the name, not the atom: a version or a slot otherwise walks past
    a rule that exists to stop an unbuildable kernel.
    """
    bare = atom.lstrip("=<>~!").split(":", 1)[0]
    bare = re.sub(r"-r\d+$", "", bare)
    return re.sub(r"-\d[\w.]*$", "", bare)


def _kernel_package_problems(config: InstallConfig) -> list[str]:
    """The installer configures and compiles no kernel of its own, so a
    `-sources` package would unpack a tree that nothing ever builds and leave
    the bootloader pointing at a `/boot` with no kernel in it."""
    package = _package_name(config.kernel.package)
    if package and package.endswith("-sources"):
        return [
            f"{package} installs a source tree and no kernel; name a dist-kernel "
            "such as sys-kernel/gentoo-kernel"
        ]
    return []


#: Measured: an install into 8 GiB runs out during linux-firmware, an hour
#: after the disks were written.
ROOT_MINIMUM: Final[Size] = Size.parse("12GiB")


def root_size_problems(
    config: InstallConfig,
    supplied_sizes: Mapping[DeviceId, Size] | None = None,
) -> list[str]:
    """Reject undersized declared or supplied sizes beneath the root.

    The nearest declared size describes the root itself. Supplied backing sizes
    remain constraints even when a nearer logical device declares its size.
    """
    graph = config.disk.graph
    root = graph.nodes.get(config.disk.root)
    if not isinstance(root, Mountpoint):
        return []
    return [
        f"{node.id} carries / and is {size}, under the {ROOT_MINIMUM} a stage3, "
        "a kernel and linux-firmware need"
        for node, size in _root_sizes(graph, root.id, supplied_sizes or {})
        if size < ROOT_MINIMUM
    ]


def _root_sizes(
    graph: DeviceGraph,
    device: DeviceId,
    supplied_sizes: Mapping[DeviceId, Size],
) -> list[tuple[Node, Size]]:
    """The nearest declared size and supplied sizes on the root path.

    Declared member sizes are excluded because an array may be large enough
    when each member is not. A supplied physical limit still constrains it.
    """
    seen: set[DeviceId] = set()
    edge = [device]
    declared_size: tuple[Node, Size] | None = None
    supplied: list[tuple[Node, Size]] = []
    while edge:
        following: list[DeviceId] = []
        for current in edge:
            if current in seen or current not in graph.nodes:
                continue
            seen.add(current)
            node = graph[current]
            supplied_size = supplied_sizes.get(current)
            if supplied_size is not None:
                supplied.append((node, supplied_size))
            elif declared_size is None:
                node_size = getattr(node, "size", None)
                if isinstance(node_size, Size):
                    declared_size = node, node_size
            following.extend(node.inputs)
        edge = following
    return ([declared_size] if declared_size is not None else []) + supplied


def _profile_problems(config: InstallConfig) -> list[str]:
    """The profile decides what the system is built against, so one that does
    not match the init leaves a system whose packages expect the other."""
    profile = config.portage.profile
    systemd_profile = "systemd" in profile.split("/")
    wants_systemd = config.system.init is InitSystem.SYSTEMD
    if systemd_profile == wants_systemd:
        return []
    wanted = "one ending in /systemd" if wants_systemd else "one without /systemd"
    return [f"init is {config.system.init.value} and the profile is {profile}; use {wanted}"]




def _repository_profile_problems(
    profile: str,
    available_profiles: Collection[str] | None | _ProfilesNotRead,
) -> list[str]:
    """The chosen profile must exist in the repository used by the target."""
    if isinstance(available_profiles, _ProfilesNotRead):
        return []
    if available_profiles is None:
        return [
            f"configured profile {profile!r} cannot be resolved because the target repository "
            "list from `eselect profile list` could not be read"
        ]
    if profile not in available_profiles:
        return [
            f"configured profile {profile!r} is not in the target repository list returned by "
            "`eselect profile list`"
        ]
    return []


def validate_profile(profile: str, available_profiles: Collection[str] | None) -> None:
    """Apply the repository-membership rule to one configured profile."""
    problems = _repository_profile_problems(profile, available_profiles)
    if problems:
        raise ValidationFailed(problems[0])


def _layout_problems(config: InstallConfig) -> list[str]:
    graph = config.disk.graph
    problems: list[str] = []

    root = graph.nodes.get(config.disk.root)
    if root is None:
        problems.append(f"disk.root is {config.disk.root!r}, which no device defines")
    elif not isinstance(root, Mountpoint):
        problems.append(f"disk.root is {config.disk.root!r}, which is not a mountpoint")
    elif root.path != _ROOT:
        problems.append(f"disk.root is mounted at {root.path}, not at /")
    elif _root_is_vfat(graph, root):
        problems.append(
            "the root filesystem is vfat, which cannot represent the Unix ownership, "
            "modes, or symlinks required by stage3"
        )

    mountpoints = graph.of_type(Mountpoint)
    for mount in mountpoints:
        if not mount.path.is_absolute() or ".." in mount.path.parts:
            problems.append(
                f"mountpoint {mount.id} uses {mount.path}, which does not stay inside the target"
            )

    for path, count in Counter(mount.path for mount in mountpoints).items():
        if count > 1:
            problems.append(f"{count} devices are mounted at {path}")

    problems += _partition_index_problems(graph)
    problems += _logical_volume_size_problems(graph)
    return problems


def _root_is_vfat(graph: DeviceGraph, root: Mountpoint) -> bool:
    for parent in graph.ancestors_of(root.id):
        node = graph[parent]
        if isinstance(node, Filesystem) and node.kind is FilesystemType.VFAT:
            return True
    return False


def _partition_index_problems(graph: DeviceGraph) -> list[str]:
    """Indexes that `sgdisk` accepts and the executor cannot then find.

    Both are refused here because `CreatePartitionTable` has already run
    `sgdisk --zap-all` by the time either shows: index 0 means *allocate one*
    to `sgdisk`, which answers success having made partition 1, and the
    executor then waits for a node ending in 0 that the kernel cannot expose.
    A repeated index fails the second `--new` with exit 4 and leaves half a
    table on a disk whose own table is already gone.
    """
    problems: list[str] = []
    for table in graph.of_type(PartitionTable):
        indexes = [
            node.index for node in graph.of_type(Partition) if node.table == table.id
        ]
        for index in sorted(one for one in set(indexes) if one < 1):
            problems.append(f"partition index {index} on {table.id} is below 1")
        for index, count in sorted(Counter(indexes).items()):
            if count > 1:
                problems.append(f"{count} partitions on {table.id} take index {index}")
        problems += _mbr_index_problems(table, indexes)
        problems += _partition_size_problems(graph, table)
    return problems


def _partition_size_problems(graph: DeviceGraph, table: PartitionTable) -> list[str]:
    """Sizes the table cannot hold, found before `sgdisk --zap-all` runs.

    A partition with no size takes what is left, so only the last one may have
    none: an unsized partition 1 took the disk to its last usable sector and
    `--new=2:0:+8M` then exited 4, with the operator's table already gone.
    Zero is the same shape of failure one step earlier — `+0K` is refused —
    and `Size(0)` stays a legal value everywhere else.
    """
    problems: list[str] = []
    partitions = sorted(
        (one for one in graph.of_type(Partition) if one.table == table.id),
        key=lambda node: node.index,
    )
    for one in partitions:
        if one.size is not None and one.size.bytes <= 0:
            problems.append(f"partition {one.id} on {table.id} is {one.size}")
    unsized = [one for one in partitions if takes_the_rest(one)]
    if len(unsized) > 1:
        problems.append(
            f"{len(unsized)} partitions on {table.id} take the rest of the disk; "
            "only the last one can"
        )
    elif unsized and partitions and unsized[0].index != partitions[-1].index:
        problems.append(
            f"partition {unsized[0].id} takes the rest of {table.id} and is not "
            "the last one, so nothing after it has room"
        )
    return problems


def _logical_volume_size_problems(graph: DeviceGraph) -> list[str]:
    problems = [
        f"logical volume {volume.id} is {volume.size}"
        for volume in graph.of_type(LogicalVolume)
        if volume.size is not None and volume.size.bytes == 0
    ]
    # The same rule partitions have, and for the same reason: `lvcreate
    # --extents 100%FREE` gives the group's whole remainder to whichever
    # volume asks first, so a second one asking is a group with no room and an
    # install that stops with the disk partitioned and the group already made.
    taking_the_rest: dict[str, list[str]] = {}
    for volume in graph.of_type(LogicalVolume):
        if volume.size is None:
            taking_the_rest.setdefault(str(volume.group), []).append(str(volume.id))
    problems += [
        f"volume group {group} has {len(volumes)} logical volumes taking the rest of it "
        f"({', '.join(sorted(volumes))}), and only one can"
        for group, volumes in sorted(taking_the_rest.items())
        if len(volumes) > 1
    ]
    return problems


def _mbr_index_problems(table: PartitionTable, indexes: list[int]) -> list[str]:
    """Indexes `parted` will not honour on a table written from scratch.

    `parted mkpart` takes no partition number: it assigns the lowest free one.
    A new MBR table whose configuration asks for index 3 alone therefore gets
    partition 1, `parted` reports success, and the executor waits for a node
    ending in 3 that the kernel cannot expose — with the table and partition 1
    already written. GPT is not affected: `sgdisk --new=N:...` is given the
    number.
    """
    if table.table is not TableType.MBR or not table.create or not indexes:
        return []
    wanted = sorted(indexes)
    if wanted != list(range(1, len(wanted) + 1)):
        return [
            f"{table.id} is a new mbr table and asks for indexes {wanted}; "
            "parted assigns the lowest free number, so they have to be 1 upwards"
        ]
    return []
