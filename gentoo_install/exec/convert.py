# SPDX-License-Identifier: GPL-2.0-or-later
"""Atomically replace selected live-system directories with staged ones."""

from __future__ import annotations

import errno
import os
import shutil
import sys
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

from ..errors import ConversionFailed

#: Copies one tree into a destination the caller names, preserving what a
#: stage3 needs. The plan hands over `cp --archive`: `shutil.copytree` restores
#: neither xattrs nor file capabilities.
Copier = Callable[[Path, Path], None]


#: Where a mounted directory's own entries are moved while it is replaced.
#: Inside the mount, so every move is a rename on one filesystem.
KEPT_ASIDE: str = ".gentoo-install.old"


def _mounts_inside(directory: Path) -> list[str]:
    """Name every entry of `directory` that is itself a mount point."""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return []
    return [one for one in entries if os.path.ismount(directory / one)]


class Arrival(Enum):
    """How an entry reached the destination, because undoing the two differs.

    A renamed entry goes back; a copied one is deleted, and its original is
    still in the staging root.
    """

    COPIED = "copied"
    RENAMED = "renamed"


def _remove(path: Path) -> None:
    """Delete a copy that arrived, on the way back out of a failure."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _replace_contents(destination: Path, staged: Path, copy: Copier) -> None:
    """Swap what is in a mount point, since the mount point cannot be renamed.

    Moving the destination's own entries aside is a `rename(2)` inside the
    mount. Bringing the staged ones in crosses out of the staging root, and a
    directory reaches this function precisely because it is a separate mount:
    Fedora's `/var` is its own btrfs subvolume, so `rename` answered
    `[Errno 18] Invalid cross-device link` on the first entry.
    """
    aside = destination / KEPT_ASIDE
    if os.path.lexists(aside):
        raise ConversionFailed(f"{aside} is left from an earlier attempt")
    os.mkdir(aside)
    moved: list[str] = []
    arrived: list[tuple[str, Arrival]] = []
    try:
        for name in sorted(os.listdir(destination)):
            if name == KEPT_ASIDE:
                continue
            os.rename(destination / name, aside / name)
            moved.append(name)
        for name in sorted(os.listdir(staged)):
            try:
                os.rename(staged / name, destination / name)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                # Recorded before the copy, not after: `cp --archive` can
                # write part of a tree and then fail, and an entry the
                # rollback never hears about leaves that part in place beside
                # the restored original. `_remove` tolerates a path that was
                # never created.
                arrived.append((name, Arrival.COPIED))
                copy(staged / name, destination / name)
                continue
            arrived.append((name, Arrival.RENAMED))
    except Exception as error:
        for name, how in reversed(arrived):
            try:
                if how is Arrival.RENAMED:
                    os.rename(destination / name, staged / name)
                else:
                    _remove(destination / name)
            except OSError as rollback_error:
                error.add_note(f"could not return {name} to the staging root: {rollback_error}")
        for name in reversed(moved):
            try:
                os.rename(aside / name, destination / name)
            except OSError as rollback_error:
                error.add_note(f"could not restore {destination / name}: {rollback_error}")
        try:
            os.rmdir(aside)
        except OSError as rollback_error:
            error.add_note(f"could not remove {aside}: {rollback_error}")
        raise ConversionFailed(
            f"{destination} could not be replaced by content: {error}"
        ) from error


def _restore_contents(destination: Path, staged: Path) -> None:
    """Undo `_replace_contents` when a later directory fails."""
    aside = destination / KEPT_ASIDE
    for name in sorted(os.listdir(destination)):
        if name == KEPT_ASIDE:
            continue
        os.rename(destination / name, staged / name)
    for name in sorted(os.listdir(aside)):
        os.rename(aside / name, destination / name)
    os.rmdir(aside)


def convert(
    staging: Path, names: Sequence[str], *, copy: Copier, root: Path = Path("/")
) -> None:
    """Replace each named directory and remove backups after all swaps finish."""
    try:
        same = os.stat(staging).st_dev == os.stat(root).st_dev
    except OSError as error:
        raise ConversionFailed(f"the staging directory could not be read: {error}") from error
    if not same:
        # Rename cannot cross a filesystem, and copying is exactly what this
        # step exists to avoid: the window would be the whole copy.
        raise ConversionFailed("the staging directory is not on the root filesystem")

    destinations: list[tuple[str, Path, Path, Path, bool, bool]] = []
    for name in names:
        destination = root / name
        staged = staging / name
        old = root / f"{name}.gentoo-install.old"
        if not staged.is_dir():
            raise ConversionFailed(f"the staging directory has no {name}")
        if os.path.lexists(old):
            raise ConversionFailed(f"{old} is left from an earlier attempt")
        if os.path.ismount(destination):
            # Counted before anything moves: `rename(2)` answers EBUSY for a
            # mount point, and finding that out halfway through the entries is
            # a state with no clean rollback.
            nested = _mounts_inside(destination)
            if nested:
                raise ConversionFailed(
                    f"{destination} is a separate mount holding {', '.join(nested)}, "
                    "which rename cannot move"
                )
        # A distribution without one of these is converted, not refused: a
        # merged-usr Debian has no `/lib64` at all, and renaming what is not
        # there fails half way through with the rest already swapped.
        destinations.append(
            (name, destination, staged, old, os.path.lexists(destination), os.path.ismount(destination))
        )
    # Mount points last: replacing one by content is the only step whose
    # rollback touches more than two renames, so nothing else has to be undone
    # after one of them succeeded.
    destinations.sort(key=lambda entry: entry[5])

    swapped: list[tuple[str, Path, Path, Path, bool, bool]] = []
    for entry in destinations:
        name, destination, staged, old, present, mounted = entry
        moved_old = False
        try:
            if mounted:
                _replace_contents(destination, staged, copy)
            else:
                if present:
                    os.rename(destination, old)
                    moved_old = True
                os.rename(staged, destination)
        except Exception as error:
            if moved_old:
                try:
                    os.rename(old, destination)
                except OSError as rollback_error:
                    error.add_note(f"could not restore {name}: {rollback_error}")
            for entry_back in reversed(swapped):
                (
                    swapped_name,
                    swapped_destination,
                    swapped_staged,
                    swapped_old,
                    was_there,
                    was_mounted,
                ) = entry_back
                if was_mounted:
                    try:
                        _restore_contents(swapped_destination, swapped_staged)
                    except OSError as rollback_error:
                        error.add_note(
                            f"could not restore {swapped_name}: {rollback_error}"
                        )
                    continue
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
    for name, destination, _, old, present, mounted in swapped:
        kept = destination / KEPT_ASIDE if mounted else old
        if not mounted and not present:
            continue
        try:
            # `/bin`, `/sbin`, `/lib` and `/lib64` are symlinks into `usr` on a
            # merged-usr system, and `shutil.rmtree` refuses a symlink with
            # `[Errno None] None`, so four of them stayed on every converted
            # machine.
            if kept.is_symlink():
                kept.unlink()
            else:
                shutil.rmtree(kept)
        except OSError as error:
            print(f"{kept} stayed behind: {error}", file=sys.stderr)


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
