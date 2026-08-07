"""Checks that need more than one field, run before anything touches a disk.

Every problem is collected and reported together: fixing one rule per run means
one more failed run to learn about the next one.
"""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath

from ..errors import ValidationFailed
from . import compat
from .config import InstallConfig
from .device import Mountpoint

_ROOT = PurePosixPath("/")


def validate(config: InstallConfig) -> None:
    problems = [*_layout_problems(config), *(rule.describe() for rule in compat.violations(config))]
    if problems:
        raise ValidationFailed(
            "the configuration does not describe an installable system:\n  " + "\n  ".join(problems)
        )


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
