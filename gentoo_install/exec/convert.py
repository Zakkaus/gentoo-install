# SPDX-License-Identifier: GPL-2.0-or-later
"""Atomically replace selected live-system directories with staged ones."""

from __future__ import annotations

import errno
import os
import re
import shutil
import sys
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Final, Literal, Sequence

from ..errors import ConversionFailed

#: Copies one tree into a destination the caller names, preserving what a
#: stage3 needs. `cp --archive` preserves xattrs and file capabilities.
Copier = Callable[[Path, Path], None]


WarningReporter = Callable[[str], None]


def _stderr_warning(message: str) -> None:
    print(message, file=sys.stderr)

_PT_INTERP: Final[int] = 3
_LIBRARY_DIRECTORIES: tuple[str, ...] = ("lib", "lib64", "usr/lib", "usr/lib64")


def staged_copier(
    staging: Path, run: Callable[[Sequence[str]], object]
) -> Copier:
    """Return a copier that remains runnable after a mounted `/usr` moves."""
    cp = _staged_executable(staging, staging / "bin" / "cp", "copy tool")
    interpreter = _elf_interpreter(cp)
    command: tuple[str, ...]
    if interpreter is None:
        command = (str(cp),)
    else:
        loader = _staged_executable(
            staging, _interpreter_path(staging, interpreter), "dynamic loader"
        )
        libraries = _staged_library_directories(staging)
        if not libraries:
            raise ConversionFailed(
                f"{cp} is dynamically linked but {staging} has no library directory"
            )
        command = (
            str(loader),
            "--library-path",
            ":".join(str(path) for path in libraries),
            str(cp),
        )

    def copy(source: Path, destination: Path) -> None:
        run(
            (
                *command,
                "--archive",
                "--one-file-system",
                str(source),
                str(destination),
            )
        )

    return copy


def _staged_executable(staging: Path, path: Path, what: str) -> Path:
    """Return an executable after refusing a symlink out of the staging root."""
    resolved = _resolved_in_staging(staging, path, what)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ConversionFailed(f"the staged {what} {path} is not executable")
    return resolved


def _resolved_in_staging(staging: Path, path: Path, what: str) -> Path:
    """Resolve `path`, rejecting an absolute symlink into the running system."""
    try:
        root = staging.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConversionFailed(
            f"the staged {what} {path} could not be read: {error}"
        ) from error
    if root != resolved and root not in resolved.parents:
        raise ConversionFailed(f"the staged {what} {path} resolves outside {staging}")
    return resolved


def _interpreter_path(staging: Path, interpreter: str) -> Path:
    """Return the staged location of an ELF interpreter path."""
    written = PurePosixPath(interpreter)
    if not written.is_absolute() or ".." in written.parts:
        raise ConversionFailed(
            f"the staged copy tool names an invalid interpreter {interpreter!r}"
        )
    return staging.joinpath(*written.parts[1:])


def _staged_library_directories(staging: Path) -> tuple[Path, ...]:
    """Return the distinct standard library directories inside `staging`."""
    found: list[Path] = []
    for name in _LIBRARY_DIRECTORIES:
        candidate = staging / name
        if not candidate.exists() and not candidate.is_symlink():
            continue
        resolved = _resolved_in_staging(staging, candidate, "library directory")
        if not resolved.is_dir():
            raise ConversionFailed(
                f"the staged library directory {candidate} is not a directory"
            )
        if resolved not in found:
            found.append(resolved)
    return tuple(found)


def _elf_interpreter(binary: Path) -> str | None:
    """Read the dynamic loader path from the staged copy tool's ELF header."""
    try:
        contents = binary.read_bytes()
    except OSError as error:
        raise ConversionFailed(
            f"the staged copy tool {binary} could not be read: {error}"
        ) from error
    if len(contents) < 6 or contents[:4] != b"\x7fELF":
        raise ConversionFailed(f"the staged copy tool {binary} is not an ELF executable")
    if contents[5] == 1:
        byteorder: Literal["little", "big"] = "little"
    elif contents[5] == 2:
        byteorder = "big"
    else:
        raise ConversionFailed(
            f"the staged copy tool {binary} has an unknown ELF byte order"
        )
    # ELF32 and ELF64 store the program-header fields at different offsets.
    if contents[4] == 1:
        header_size, table_at, entry_at, count_at = 52, 28, 42, 44
        program_size, offset_at, size_at, number_size = 32, 4, 16, 4
    elif contents[4] == 2:
        header_size, table_at, entry_at, count_at = 64, 32, 54, 56
        program_size, offset_at, size_at, number_size = 56, 8, 32, 8
    else:
        raise ConversionFailed(f"the staged copy tool {binary} has an unknown ELF class")
    if len(contents) < header_size:
        raise ConversionFailed(f"the staged copy tool {binary} has a truncated ELF header")
    table = int.from_bytes(contents[table_at : table_at + number_size], byteorder)
    entry_size = int.from_bytes(contents[entry_at : entry_at + 2], byteorder)
    count = int.from_bytes(contents[count_at : count_at + 2], byteorder)
    if count == 0:
        return None
    if entry_size < program_size or table + entry_size * count > len(contents):
        raise ConversionFailed(f"the staged copy tool {binary} has invalid program headers")
    for index in range(count):
        start = table + index * entry_size
        if int.from_bytes(contents[start : start + 4], byteorder) != _PT_INTERP:
            continue
        offset = int.from_bytes(
            contents[start + offset_at : start + offset_at + number_size], byteorder
        )
        size = int.from_bytes(
            contents[start + size_at : start + size_at + number_size], byteorder
        )
        if offset + size > len(contents):
            raise ConversionFailed(f"the staged copy tool {binary} has an invalid interpreter")
        written = contents[offset : offset + size]
        terminator = written.find(b"\0")
        if terminator <= 0:
            raise ConversionFailed(f"the staged copy tool {binary} has an invalid interpreter")
        try:
            return written[:terminator].decode("ascii")
        except UnicodeDecodeError as error:
            raise ConversionFailed(
                f"the staged copy tool {binary} has a non-ASCII interpreter"
            ) from error
    return None


#: Where a mounted directory's own entries are moved while it is replaced.
#: Inside the mount, so every move is a rename on one filesystem.
KEPT_ASIDE: str = ".gentoo-install.old"

#: The kernel's own mount table, which lists a bind mount `st_dev` cannot see.
MOUNTINFO: Path = Path("/proc/self/mountinfo")


def _mount_points() -> frozenset[str]:
    """Every mount point in this namespace, from the kernel's own list.

    `os.path.ismount` compares a directory's `st_dev` against its parent's, so
    a bind mount within one filesystem reads as an ordinary directory while
    `rename(2)` still answers EBUSY for it. Measured under `unshare -rm`:
    `mount --bind` of a sibling directory is `False` to `ismount` and present
    here.
    """
    try:
        listed = MOUNTINFO.read_text()
    except OSError as error:
        raise ConversionFailed(
            f"the mount table could not be read, so whether a directory this "
            f"conversion replaces is a mount point is unknown: {error}"
        ) from error
    points: set[str] = set()
    for line in listed.splitlines():
        fields = line.split(" ")
        if len(fields) > 4:
            points.add(_unescape(fields[4]))
    return frozenset(points)


def _unescape(field: str) -> str:
    """Undo mountinfo's octal escaping of space, tab, newline and backslash."""
    return re.sub(r"\\([0-7]{3})", lambda found: chr(int(found.group(1), 8)), field)


def _mounts_inside(directory: Path, points: frozenset[str]) -> list[str]:
    """Name every mount point below `directory`, at any depth.

    A directory that cannot be listed is refused rather than reported as
    holding nothing. Moving its contents has no clean rollback after an entry
    has moved.
    """
    try:
        with os.scandir(directory):
            pass
    except OSError as error:
        raise ConversionFailed(
            f"{directory} cannot be listed, so this irreversible replacement "
            f"is refused: {error}"
        ) from error
    directory_text = str(directory)
    prefix = "/" if directory_text == "/" else f"{directory_text}/"
    return sorted(
        point[len(prefix) :]
        for point in points
        if point != directory_text and point.startswith(prefix)
    )


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


def _replace_contents(
    destination: Path, staged: Path, copy: Copier
) -> list[tuple[str, Arrival]]:
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
    return arrived


def _restore_contents(
    destination: Path, staged: Path, arrived: Sequence[tuple[str, Arrival]]
) -> None:
    """Undo `_replace_contents` when a later directory fails.

    Told how each entry arrived rather than asking again: rederiving it meant
    trying the reverse `rename` and reading `EXDEV`, and a mount that has gone
    since answers that rename with success, which moves the underlying entry
    into staging and leaves the copy on the mount. A copy leaves the staged
    original in place, so removing the copy is its undo.
    """
    aside = destination / KEPT_ASIDE
    failure: OSError | None = None

    def remember(error: OSError, detail: str) -> None:
        nonlocal failure
        if failure is None:
            failure = error
        failure.add_note(detail)

    for name, how in reversed(arrived):
        try:
            if how is Arrival.RENAMED:
                os.rename(destination / name, staged / name)
            else:
                _remove(destination / name)
        except OSError as error:
            remember(error, f"could not return {name} to the staging root: {error}")
    try:
        original_names = sorted(os.listdir(aside))
    except OSError as error:
        remember(error, f"could not list {aside}: {error}")
    else:
        for name in original_names:
            try:
                os.rename(aside / name, destination / name)
            except OSError as error:
                remember(error, f"could not restore {destination / name}: {error}")
    try:
        os.rmdir(aside)
    except OSError as error:
        remember(error, f"could not remove {aside}: {error}")
    if failure is not None:
        raise failure


def convert(
    staging: Path,
    names: Sequence[str],
    *,
    copy: Copier,
    root: Path = Path("/"),
    warn: WarningReporter = _stderr_warning,
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

    points = _mount_points()
    destinations: list[tuple[str, Path, Path, Path, bool, bool]] = []
    for name in names:
        destination = root / name
        staged = staging / name
        old = root / f"{name}.gentoo-install.old"
        if not staged.is_dir():
            raise ConversionFailed(f"the staging directory has no {name}")
        if os.path.lexists(old):
            raise ConversionFailed(f"{old} is left from an earlier attempt")
        # Classify this destination from the snapshot that checked descendants;
        # a later read could choose an unchecked replacement path.
        mounted = str(destination) in points
        present = os.path.lexists(destination)
        if present and not destination.is_symlink():
            # Checked before anything moves: cleanup must never traverse a
            # mount that a renamed tree carried into its backup.
            nested = _mounts_inside(destination, points)
            if nested:
                raise ConversionFailed(
                    f"{destination} is holding {', '.join(nested)} mount points, "
                    "which must not be moved aside"
                )
        # A distribution without one of these is converted, not refused: a
        # merged-usr Debian has no `/lib64` at all, and renaming what is not
        # there fails half way through with the rest already swapped.
        destinations.append((name, destination, staged, old, present, mounted))
    # Mount points first: their copies run the staged `cp` at its path inside
    # `staging`, and renaming the staged `usr` into place removes that path.
    destinations.sort(key=lambda entry: not entry[5])

    swapped: list[tuple[str, Path, Path, Path, bool, bool]] = []
    #: How each entry of a replaced mount point arrived, so the rollback is
    #: told rather than asking the filesystem a second time.
    arrivals: dict[str, list[tuple[str, Arrival]]] = {}
    for entry in destinations:
        name, destination, staged, old, present, mounted = entry
        moved_old = False
        try:
            if mounted:
                arrivals[name] = _replace_contents(destination, staged, copy)
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
                        _restore_contents(
                            swapped_destination,
                            swapped_staged,
                            arrivals.get(swapped_name, []),
                        )
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
            warn(f"{kept} stayed behind: {error}")


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


def populate_boot(
    staging: Path,
    *,
    root: Path = Path("/"),
    warn: WarningReporter = _stderr_warning,
) -> None:
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
            warn(f"{entry} stayed behind: {error}")
