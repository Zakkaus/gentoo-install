# SPDX-License-Identifier: GPL-2.0-or-later
"""Persist and publish the artifacts produced by an install run."""

from __future__ import annotations

import os
import shutil
import stat
import tomllib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Final
from pathlib import Path, PurePosixPath

from .. import errors
from ..errors import GentooInstallError
from ..log import Journal
from ..model import config as model_config
from ..model import paste
from ..model.config import InstallConfig
from ..model.serialise import to_toml
from . import fetch, preflight
from .probe import Probe
from .runner import TargetEscape, open_in_target, write_file


class RunFile(Enum):
    """Files that form the durable record of a run."""

    LOG = "install.log"
    JOURNAL = "install.jsonl"


class PasteKind(Enum):
    """Paste exports created by report workflows."""

    LOG = "log"
    CONFIG = "config"


@dataclass(frozen=True)
class ActiveReport:
    """The open log and journal used while an install is running."""

    work: Path
    target: Path
    record: Callable[[str], None]
    journal: Journal


@contextmanager
def recording(work: Path, target: Path) -> Iterator[ActiveReport]:
    """Open the files that record a run and close them when it finishes."""
    work.mkdir(parents=True, exist_ok=True)
    with (work / RunFile.LOG.value).open("a") as log:

        def record(line: str) -> None:
            print(line, file=log, flush=True)
            print(line, flush=True)

        yield ActiveReport(
            work=work,
            target=target,
            record=record,
            journal=Journal(path=work / RunFile.JOURNAL.value),
        )


#: Target-absolute, so `open_in_target` refuses a symlink on the way. A
#: conversion replaces a running system's userland, where `/var/log` is
#: whatever that distribution left there.
LOG_DIRECTORY: Final[PurePosixPath] = PurePosixPath("/var/log/gentoo-install")


def keep_log(work: Path, target: Path, record: Callable[[str], None]) -> None:
    """Copy the run's log onto the installed system."""
    if not target.is_mount():
        # A successful copy to an unmounted path lands on the live medium.
        record(f"warning: {target} is not mounted, so the log was not kept there")
        return
    kept = target / "var/log/gentoo-install"
    try:
        for name in RunFile:
            source = work / name.value
            if not source.is_file():
                continue
            handle = open_in_target(
                target,
                LOG_DIRECTORY / name.value,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                stat.S_IMODE(source.stat().st_mode),
                parents=True,
            )
            with os.fdopen(handle, "wb") as opened:
                opened.write(source.read_bytes())
    except (OSError, TargetEscape) as error:
        record(f"warning: the log could not be copied to {kept}: {error}")
        return
    # The target is about to be unmounted, while the work copy lasts to reboot.
    record(f"the log of this run is in {kept}, and until the next reboot in {work}")


def offer_paste(
    work: Path,
    record: Callable[[str], None],
    stopped: bool,
    unattended: bool,
    ask: Callable[[str], bool],
    show_address: Callable[[str], None],
) -> None:
    """Offer to publish this run's log and display the resulting address."""
    if unattended:
        return
    outcome = "the install stopped" if stopped else "the install finished"
    if not ask(f"{outcome}. send the log to {paste.HOST}, which is public?"):
        record(f"the log to publish by hand is {work / RunFile.LOG.value}")
        return
    source = work / RunFile.LOG.value
    try:
        body = source.read_text()
    except OSError as error:
        record(f"warning: {source} could not be read: {error}")
        return
    try:
        url = fetch.upload(body, paste.export_for(PasteKind.LOG.value))
    except GentooInstallError as error:
        record(f"warning: {error}")
        return
    record(f"the log of this run is at {url}")
    show_address(url)


def publish_config(config: InstallConfig) -> str:
    """Publish a configuration with password hashes redacted."""
    return fetch.upload(
        to_toml(config, publishing=True), paste.export_for(PasteKind.CONFIG.value)
    )


def configs_here(save_as: str) -> tuple[str, ...]:
    """Find likely installer configurations in the current directory."""
    found: list[str] = []
    try:
        candidates = sorted(one for one in Path.cwd().glob("*.toml") if one.is_file())
    except OSError:
        return ()
    for path in candidates:
        try:
            held = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            # A malformed file with the installer's save name remains visible.
            if path.name == save_as:
                found.append(path.name)
            continue
        if set(held).intersection(model_config.PERSISTED_SECTIONS):
            found.append(path.name)
    return tuple(found)


def save_config(config: InstallConfig, where: Path) -> str:
    """Write a configuration to an already-expanded path."""
    try:
        write_file(where, to_toml(config))
    except OSError as error:
        raise errors.ConfigError(f"cannot write {where}: {error.strerror}") from error
    return str(where)


def stage_passphrase(passphrase: str, work: Path) -> str:
    """Write a passphrase under the run's volatile work directory."""
    where = work / "keys" / "tui"
    where.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_file(where, passphrase, 0o600)
    return str(where)


def absent(wanted: Iterable[str], probe: Probe | None = None) -> set[str]:
    """Find required commands absent from the machine or provided by BusyBox."""
    names = list(wanted)
    present = frozenset(command for command in names if shutil.which(command) is not None)
    judged = set(preflight.GNU_ONLY) & present if probe is not None else set()
    versions = probe.versions(judged) if probe is not None else {}
    assessment = preflight.assess_commands(names, present, versions)
    return set(assessment.missing) | {problem.name for problem in assessment.unusable}
