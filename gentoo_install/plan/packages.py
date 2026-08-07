"""Desktop profiles and the applications chosen separately from them.

A profile is data, not code: `data/profiles/*.toml` names packages, services and
the repositories they come from, and an application group has the same shape. So
a user can have an input method without a desktop, or a desktop without one.

The catalog is read by `data.py` and passed in, because this layer stays a pure
function of its arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..errors import ConfigError
from ..model.config import InstallConfig
from .operations import Operation, Stage
from .portage import Emerge
from .system import EnableService


@dataclass(frozen=True)
class Group:
    """One profile or application group, exactly as its TOML file declares it."""

    name: str
    packages: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    use: tuple[str, ...] = ()
    #: Repositories the packages come from. Selecting the group is what asks for
    #: them; an overlay is never added behind the user's back.
    repositories: tuple[str, ...] = ()


Catalog = Mapping[str, Group]


def build(config: InstallConfig, catalog: Catalog) -> list[Operation]:
    operations: list[Operation] = []
    for group in groups(config, catalog):
        if group.packages:
            operations.append(
                Emerge(
                    stage=Stage.PACKAGES,
                    packages=group.packages,
                    summary=f"install the {group.name} group",
                )
            )
        for service in group.services:
            # In this stage, not the system one: the unit does not exist until
            # the package that ships it is merged.
            operations.append(
                EnableService(stage=Stage.PACKAGES, service=service, init=config.system.init)
            )
    if config.packages.extra:
        operations.append(
            Emerge(
                stage=Stage.PACKAGES,
                packages=config.packages.extra,
                summary="install the extra packages",
            )
        )
    return operations


def groups(config: InstallConfig, catalog: Catalog) -> tuple[Group, ...]:
    names = [config.packages.desktop, *config.packages.applications]
    found: list[Group] = []
    for name in names:
        if not name:
            continue
        group = catalog.get(name)
        if group is None:
            raise ConfigError(f"no package group named {name!r}; the catalog has {_known(catalog)}")
        found.append(group)
    return tuple(found)


def required_repositories(config: InstallConfig, catalog: Catalog) -> tuple[str, ...]:
    """What the selected groups need, so the interface can say so before the
    user commits instead of failing at emerge time."""
    wanted: list[str] = []
    for group in groups(config, catalog):
        for repository in group.repositories:
            if repository not in wanted:
                wanted.append(repository)
    return tuple(wanted)


def required_use(config: InstallConfig, catalog: Catalog) -> tuple[str, ...]:
    wanted: list[str] = []
    for group in groups(config, catalog):
        for flag in group.use:
            if flag not in wanted:
                wanted.append(flag)
    return tuple(wanted)


def _known(catalog: Catalog) -> str:
    return ", ".join(sorted(catalog)) or "nothing"
