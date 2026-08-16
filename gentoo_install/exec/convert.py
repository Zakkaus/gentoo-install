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

    destinations: list[tuple[str, Path, Path, Path, bool]] = []
    for name in names:
        destination = root / name
        staged = staging / name
        old = root / f"{name}.gentoo-install.old"
        if not staged.is_dir():
            raise ConversionFailed(f"the staging directory has no {name}")
        if os.path.lexists(old):
            raise ConversionFailed(f"{old} is left from an earlier attempt")
        # A distribution without one of these is converted, not refused: a
        # merged-usr Debian has no `/lib64` at all, and renaming what is not
        # there fails half way through with the rest already swapped.
        destinations.append((name, destination, staged, old, os.path.lexists(destination)))

    swapped: list[tuple[str, Path, Path, Path, bool]] = []
    for entry in destinations:
        name, destination, staged, old, present = entry
        moved_old = False
        try:
            if present:
                os.rename(destination, old)
                moved_old = True
            os.rename(staged, destination)
        except OSError as error:
            if moved_old:
                try:
                    os.rename(old, destination)
                except OSError as rollback_error:
                    error.add_note(f"could not restore {name}: {rollback_error}")
            for entry_back in reversed(swapped):
                swapped_name, swapped_destination, swapped_staged, swapped_old, was_there = (
                    entry_back
                )
                try:
                    os.rename(swapped_destination, swapped_staged)
                except OSError as rollback_error:
                    error.add_note(
                        f"could not restore {swapped_name}: {rollback_error}"
                    )
                if not was_there:
                    continue
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
    for name, _, _, old, present in swapped:
        if not present:
            continue
        try:
            shutil.rmtree(old)
        except OSError as error:
            print(f"{old} stayed behind: {error}", file=sys.stderr)


#: What a distribution names a kernel, an initramfs and the two files that go
#: with them. Files only: `/boot/grub` and the esp mounted under `/boot` are
#: directories and are left exactly as they are.
KERNEL_FILES: tuple[str, ...] = (
    "vmlinuz",
    "vmlinux",
    "initrd",
    "initramfs",
    "config-",
    "System.map-",
)


def populate_boot(staging: Path, *, root: Path = Path("/")) -> None:
    """Put the staged kernel into the machine's own `/boot`.

    Copied rather than renamed, and not part of the swap: `/boot` is a separate
    mount on many machines and holds the esp as a mount below it on many more,
    and `rename` refuses both. What it costs is a copy of a few tens of
    megabytes, inside the irreversible window but at its end.

    The old distribution's kernels are removed once the staged ones are in,
    because their modules left with `/lib`, so a menu entry for one is an entry
    that cannot boot.
    """
    source = staging / "boot"
    destination = root / "boot"
    if not source.is_dir():
        raise ConversionFailed(f"the staging directory has no {source}")
    destination.mkdir(parents=True, exist_ok=True)
    carried: set[str] = set()
    for entry in sorted(source.iterdir()):
        target = destination / entry.name
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(entry, target, dirs_exist_ok=True, symlinks=True)
            else:
                if target.exists() or target.is_symlink():
                    target.unlink()
                shutil.copy2(entry, target, follow_symlinks=False)
        except OSError as error:
            raise ConversionFailed(f"{entry.name} could not be put in {destination}: {error}") from error
        carried.add(entry.name)
    for entry in sorted(destination.iterdir()):
        if entry.name in carried or entry.is_dir():
            continue
        if not any(entry.name.startswith(one) for one in KERNEL_FILES):
            continue
        try:
            entry.unlink()
        except OSError as error:
            # Said rather than raised: the machine is already converted, and a
            # stale image left behind is a menu entry, not a broken system.
            print(f"{entry} stayed behind: {error}", file=sys.stderr)
