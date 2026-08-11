"""Resolve device graph mountpoints for runtime and persistent consumers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from ..errors import InvalidLayout
from ..model.device import (
    DeviceGraph,
    DeviceId,
    Filesystem,
    FilesystemType,
    Mountpoint,
    Subvolume,
    ZfsDataset,
    ZfsPool,
)


@dataclass(frozen=True, kw_only=True)
class ResolvedMount:
    """One mount with its graph references reduced to executable meaning."""

    mountpoint: DeviceId
    path: PurePosixPath
    device: DeviceId | None
    dataset: str | None
    filesystem_kind: FilesystemType | None
    subvolume: str | None
    options: tuple[str, ...]


def resolve_mounts(graph: DeviceGraph) -> tuple[ResolvedMount, ...]:
    """Resolve every mount in deterministic parent-before-child order."""
    mounts = (_resolve_mount(graph, mount) for mount in graph.of_type(Mountpoint))
    return tuple(sorted(mounts, key=lambda mount: (len(mount.path.parts), str(mount.path))))


def _resolve_mount(graph: DeviceGraph, mount: Mountpoint) -> ResolvedMount:
    source = graph[mount.source]
    if isinstance(source, ZfsDataset):
        pool = graph[source.pool]
        if not isinstance(pool, ZfsPool):
            raise InvalidLayout(
                f"dataset {source.id!r} names {source.pool!r}, which is not a ZFS pool"
            )
        return ResolvedMount(
            mountpoint=mount.id,
            path=mount.path,
            device=None,
            dataset=f"{pool.name}/{source.name}",
            filesystem_kind=None,
            subvolume=None,
            options=mount.options,
        )
    if isinstance(source, Subvolume):
        filesystem = graph[source.filesystem]
        if not isinstance(filesystem, Filesystem):
            raise InvalidLayout(
                f"subvolume {source.id!r} names {source.filesystem!r}, which is not a filesystem"
            )
        return ResolvedMount(
            mountpoint=mount.id,
            path=mount.path,
            device=filesystem.device,
            dataset=None,
            filesystem_kind=filesystem.kind,
            subvolume=source.name,
            options=(*mount.options, f"subvol={source.name}"),
        )
    if isinstance(source, Filesystem):
        return ResolvedMount(
            mountpoint=mount.id,
            path=mount.path,
            device=source.device,
            dataset=None,
            filesystem_kind=source.kind,
            subvolume=None,
            options=mount.options,
        )
    raise InvalidLayout(
        f"mountpoint {mount.id!r} names {mount.source!r}, which is not a mountable source"
    )
