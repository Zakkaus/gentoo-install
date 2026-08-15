# SPDX-License-Identifier: GPL-2.0-or-later
"""Atomically replace selected live-system directories with staged ones."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

from ..errors import ConversionFailed


def convert(staging: Path, names: Sequence[str], *, root: Path = Path("/")) -> None:
    """Replace each named directory and remove backups after all swaps finish."""
    try:
        same = os.stat(staging).st_dev == os.stat(root).st_dev
    except OSError as error:
        raise ConversionFailed(f"the staging directory could not be read: {error}") from error
    if not same:
        # Rename cannot cross a filesystem, and copying is exactly what this
        # step exists to avoid: the window would be the whole copy.
        raise ConversionFailed("the staging directory is not on the root filesystem")

    destinations: list[tuple[str, Path, Path, Path]] = []
    for name in names:
        destination = root / name
        staged = staging / name
        old = root / f"{name}.gentoo-install.old"
        if not staged.is_dir():
            raise ConversionFailed(f"the staging directory has no {name}")
        if os.path.lexists(old):
            raise ConversionFailed(f"{old} is left from an earlier attempt")
        destinations.append((name, destination, staged, old))

    swapped: list[tuple[str, Path, Path, Path]] = []
    for entry in destinations:
        name, destination, staged, old = entry
        moved_old = False
        try:
            os.rename(destination, old)
            moved_old = True
            os.rename(staged, destination)
        except OSError as error:
            if moved_old:
                try:
                    os.rename(old, destination)
                except OSError as rollback_error:
                    error.add_note(f"could not restore {name}: {rollback_error}")
            for swapped_name, swapped_destination, swapped_staged, swapped_old in reversed(swapped):
                try:
                    os.rename(swapped_destination, swapped_staged)
                except OSError as rollback_error:
                    error.add_note(
                        f"could not restore {swapped_name}: {rollback_error}"
                    )
                try:
                    os.rename(swapped_old, swapped_destination)
                except OSError as rollback_error:
                    error.add_note(
                        f"could not restore {swapped_name}: {rollback_error}"
                    )
            raise ConversionFailed(f"{name} could not be swapped: {error}") from error
        swapped.append(entry)

    # Said rather than raised: every name is already swapped by now, so the
    # machine is converted and a directory left behind is not a failure of it.
    for name, _, _, old in swapped:
        try:
            shutil.rmtree(old)
        except OSError as error:
            print(f"{old} stayed behind: {error}", file=sys.stderr)
