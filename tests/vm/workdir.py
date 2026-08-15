# SPDX-License-Identifier: GPL-2.0-or-later
"""Confinement for VM harness artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Final

LAB_ROOT: Final[Path] = Path.home() / "code/gentoo-install/lab"


class WorkdirError(ValueError):
    """A requested work directory resolves outside the VM lab."""


def confined(path: Path) -> Path:
    root = LAB_ROOT.resolve()
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise WorkdirError(f"{path} resolves outside {root}")
    return resolved
