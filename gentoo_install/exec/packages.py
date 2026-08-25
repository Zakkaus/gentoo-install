# SPDX-License-Identifier: GPL-2.0-or-later
"""Read package payload contracts from an installed Gentoo target."""

from __future__ import annotations

import os
import stat
import re

from pathlib import Path, PurePosixPath

from ..errors import CommandFailed, TargetEscape
from .runner import open_in_target


_ATOM = re.compile(r"^[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+$")


def parse_contents(content: str) -> frozenset[PurePosixPath]:
    """Return paths from the VDB CONTENTS format emitted by Portage."""
    paths: set[PurePosixPath] = set()
    for line_number, line in enumerate(content.splitlines(), start=1):
        kind, separator, fields = line.partition(" ")
        if not separator:
            raise CommandFailed(f"VDB CONTENTS line {line_number} has no path")
        if kind == "dir":
            raw_path = fields
        elif kind == "obj":
            parts = fields.rsplit(maxsplit=2)
            if len(parts) != 3:
                raise CommandFailed(f"VDB CONTENTS object line {line_number} is malformed")
            raw_path = parts[0]
        elif kind == "sym":
            target = fields.rsplit(maxsplit=1)
            if len(target) != 2:
                raise CommandFailed(f"VDB CONTENTS symlink line {line_number} is malformed")
            raw_path, arrow, _ = target[0].partition(" -> ")
            if not arrow:
                raise CommandFailed(f"VDB CONTENTS symlink line {line_number} has no target")
        else:
            raise CommandFailed(f"VDB CONTENTS line {line_number} has unknown type {kind!r}")
        path = PurePosixPath(raw_path)
        if not path.is_absolute():
            raise CommandFailed(f"VDB CONTENTS line {line_number} has a relative path")
        paths.add(path)
    return frozenset(paths)


def installed_package_paths(target: Path, package: str) -> frozenset[PurePosixPath]:
    """Read every installed version of an atom from the target's VDB."""
    atom = package.split(":", 1)[0]
    if not _ATOM.fullmatch(atom):
        raise CommandFailed(f"cannot inspect installed files for invalid package atom {package!r}")
    category, name = atom.split("/", 1)
    category_path = PurePosixPath(f"/var/db/pkg/{category}")
    try:
        category_handle = open_in_target(target, category_path, os.O_RDONLY | os.O_DIRECTORY)
    except (FileNotFoundError, NotADirectoryError):
        return frozenset()
    except (OSError, TargetEscape) as error:
        raise CommandFailed(f"cannot read the installed package database for {package}") from error

    try:
        versions = os.listdir(category_handle)
    except OSError as error:
        raise CommandFailed(f"cannot list installed versions of {package}") from error
    finally:
        os.close(category_handle)

    prefix = f"{name}-"
    matched = [
        version
        for version in versions
        if version.startswith(prefix)
        and version[len(prefix) : len(prefix) + 1].isdigit()
    ]
    paths: set[PurePosixPath] = set()
    for version in matched:
        contents_path = category_path / version / "CONTENTS"
        try:
            handle = open_in_target(target, contents_path, os.O_RDONLY)
            with os.fdopen(handle, "r") as contents:
                paths.update(parse_contents(contents.read()))
        except (OSError, TargetEscape) as error:
            raise CommandFailed(f"cannot read {contents_path} for {package}") from error
    return frozenset(paths)


def _is_absent(error: TargetEscape) -> bool:
    """Whether this escape is a component that is not there.

    `open_in_target` opens each component itself, so a directory missing on
    the way arrives as `TargetEscape`, and a probe that re-raised it answered
    `cannot inspect` for a package that is not installed at all -- where the
    caller has the message that names the reason. `ENOENT` only: measured on
    this kernel, `O_NOFOLLOW | O_DIRECTORY` answers `ENOTDIR` for a symlink
    out of the target and `ENOENT` for a missing name, so taking both would
    turn the escape this function exists to catch into a quiet `False`.
    """
    return isinstance(error.__cause__, FileNotFoundError)


def target_is_file(target: Path, path: PurePosixPath) -> bool:
    """Whether an absolute target path is an existing regular file."""
    try:
        handle = open_in_target(target, path, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return False
    except TargetEscape as error:
        if _is_absent(error):
            return False
        raise CommandFailed(f"cannot inspect target file {path}") from error
    except OSError as error:
        raise CommandFailed(f"cannot inspect target file {path}") from error
    try:
        return stat.S_ISREG(os.fstat(handle).st_mode)
    finally:
        os.close(handle)


def target_is_directory(target: Path, path: PurePosixPath) -> bool:
    """Whether an absolute target path is an existing directory."""
    try:
        handle = open_in_target(target, path, os.O_RDONLY | os.O_DIRECTORY)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except TargetEscape as error:
        if _is_absent(error):
            return False
        raise CommandFailed(f"cannot inspect target directory {path}") from error
    except OSError as error:
        raise CommandFailed(f"cannot inspect target directory {path}") from error
    os.close(handle)
    return True
