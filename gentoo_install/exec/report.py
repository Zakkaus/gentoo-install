# SPDX-License-Identifier: GPL-2.0-or-later
"""Persist and publish the artifacts produced by an install run."""

from __future__ import annotations

import os
import fcntl
import shutil
import sys
import stat
import tomllib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Final
from pathlib import Path, PurePosixPath

from .. import errors
from ..errors import GentooInstallError, TargetEscape, WorkDirectoryBusy
from ..log import Journal
from ..model import config as model_config
from ..model import paste
from ..model.config import InstallConfig
from ..model.serialise import to_toml
from ..redact import holds_a_secret
from . import fetch, preflight
from .probe import Probe
from .runner import open_in_target, write_file


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


#: Held for the whole of a run. Two invocations sharing a `--work` both pass
#: preflight, then partition the same disks and append to one journal, and a
#: later `--resume` reads only the attempt whose `started` it happened to see
#: last.
LOCK_FILE: Final[str] = "install.lock"


@contextmanager
def recording(work: Path, target: Path) -> Iterator[ActiveReport]:
    """Open the files that record a run and close them when it finishes."""
    work.mkdir(parents=True, exist_ok=True)
    lock = (work / LOCK_FILE).open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        lock.close()
        raise WorkDirectoryBusy(
            f"another install is using {work}; wait for it or pass a different --work"
        ) from error
    log = (work / RunFile.LOG.value).open("a")

    def record(line: str) -> None:
        print(line, file=log, flush=True)
        print(line, flush=True)

    try:
        yield ActiveReport(
            work=work,
            target=target,
            record=record,
            journal=Journal(path=work / RunFile.JOURNAL.value),
        )
    finally:
        # Not a `with`: this close runs while an install's own exception is
        # unwinding, and the work directory is a tmpfs an install can fill.
        # An errno from the last flush would replace the reason the install
        # stopped, which is the one thing the operator needs.
        try:
            log.close()
        except OSError as error:
            print(f"WARNING: the run log could not be closed: {error}", file=sys.stderr)
        # Closing the descriptor releases the lock; the file stays so a reader
        # of the work directory can see what it is for.
        lock.close()


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
    translate: Callable[[str], str] = lambda source: source,
) -> None:
    """Offer to publish this run's log and display the resulting address."""
    if unattended:
        return
    # Two calls, not one conditional inside `translate`: the catalog check
    # reads the literals out of the source and a conditional hides both.
    outcome = translate("the install stopped") if stopped else translate("the install finished")
    if not ask(
        translate("{outcome}. send the log to {host}, which is public?").format(
            outcome=outcome, host=paste.HOST
        )
    ):
        record(f"the log to publish by hand is {work / RunFile.LOG.value}")
        return
    source = work / RunFile.LOG.value
    try:
        body = source.read_text()
    except OSError as error:
        record(f"warning: {source} could not be read: {error}")
        return
    if holds_a_secret(body):
        # A second guard, because the first one only reaches command lines: a
        # log carries what commands printed as well as how they were called.
        record(f"the log holds a password hash and was not sent to {paste.HOST}")
        record(f"the log to publish by hand is {source}")
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
