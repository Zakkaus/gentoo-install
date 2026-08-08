"""Checks that need more than one field, run before anything touches a disk.

Every problem is collected and reported together: fixing one rule per run means
one more failed run to learn about the next one.
"""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath
from typing import Final

from ..errors import ValidationFailed
from . import compat
from .config import InitSystem, InstallConfig
from .device import Mountpoint
from .size import Size

_ROOT = PurePosixPath("/")


def validate(config: InstallConfig) -> None:
    problems = [
        *_layout_problems(config),
        *_root_size_problems(config),
        *_profile_problems(config),
        *(rule.describe() for rule in compat.violations(config)),
    ]
    if problems:
        raise ValidationFailed(
            "the configuration does not describe an installable system:\n  " + "\n  ".join(problems)
        )


#: A stage3, a kernel build and linux-firmware together, with room for the
#: portage tree. Measured: an install into 8 GiB runs out during
#: sys-kernel/linux-firmware, an hour after the disks were written.
ROOT_MINIMUM: Final[Size] = Size.parse("12GiB")


def _root_size_problems(config: InstallConfig) -> list[str]:
    """Checked here because the size is in the configuration: a root too small
    is knowable before anything is partitioned."""
    graph = config.disk.graph
    root = graph.nodes.get(config.disk.root)
    if not isinstance(root, Mountpoint):
        return []
    for node in (root, *(graph[parent] for parent in graph.ancestors_of(root.id))):
        size = getattr(node, "size", None)
        if isinstance(size, Size) and size < ROOT_MINIMUM:
            return [
                f"{node.id} carries / and is {size}, under the {ROOT_MINIMUM} a stage3, "
                "a kernel and linux-firmware need"
            ]
    return []


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

    for path, count in Counter(mount.path for mount in graph.of_type(Mountpoint)).items():
        if count > 1:
            problems.append(f"{count} devices are mounted at {path}")

    return problems
