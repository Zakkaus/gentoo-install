# SPDX-License-Identifier: GPL-2.0-or-later
"""The only module in the package that imports `subprocess`.

Every external command goes through here, so there is one place that owns exit
codes, output capture and the log. A caller that wants to run something without
it would also be deciding, on its own, what a failure means.
"""

from __future__ import annotations

import errno
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from ..errors import CommandFailed
from ..log import Journal
from ..model.config import ProxyConfig
from ..plan.operations import ending


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def command(self) -> str:
        return shlex.join(_display_argv(self.argv))

    @property
    def ending(self) -> str:
        return ending(self.returncode)


@dataclass
class Runner:
    """Runs commands, or pretends to when `dry_run` is set.

    `dry_run` exists for the operations that read state; nothing that changes a
    machine is reached in a dry run at all, because `render()` is used instead
    of `apply()`.
    """

    log: Callable[[str], None] = print
    journal: Journal | None = None
    dry_run: bool = False
    #: Whether a command's own output is logged as it arrives.
    echo: bool = True
    #: Prepended to every command, which is how `run_in_target` chroots.
    prefix: tuple[str, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)
    proxy: ProxyConfig | None = None
    history: list[Result] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        full = (*self.prefix, *argv)
        if self.dry_run:
            self.log(f"would run: {shlex.join(_display_argv(full))}")
            return Result(argv=full, returncode=0, stdout="", stderr="", seconds=0.0)
        started = time.monotonic()
        self.log(f"run: {shlex.join(_display_argv(full))}")
        try:
            output, returncode = self._stream(full, input_text, timeout)
        except FileNotFoundError as error:
            if check:
                raise CommandFailed(f"{full[0]} is not installed") from error
            # `check=False` says a failure is an answer, and a medium without
            # the command is one of the answers: the Arch live image carries
            # no `zpool`, and asking whether a disk belongs to a pool stopped
            # an install that used no ZFS at all.
            self.log(f"| {full[0]} is not installed")
            return Result(
                argv=full,
                returncode=127,
                stdout=f"{full[0]} is not installed",
                stderr="",
                seconds=time.monotonic() - started,
            )
        result = Result(
            argv=full,
            returncode=returncode,
            stdout=output,
            stderr="",
            seconds=time.monotonic() - started,
        )
        self.history.append(result)
        if self.journal is not None:
            self.journal.command(_display_argv(result.argv), result.returncode, result.seconds)
            if "emerge" in full:
                self.journal.packages(result.stdout)
        if check and result.returncode != 0:
            raise CommandFailed(
                f"{result.command} ended with {result.ending}: "
                f"{_tail(result.stderr or result.stdout)}"
            )
        return result

    def _stream(
        self, argv: tuple[str, ...], input_text: str | None, timeout: float | None
    ) -> tuple[str, int]:
        """Read the command's output as it arrives.

        An emerge can run for hours; captured output would show nothing until it
        ended, which is indistinguishable from a hang. stderr is merged in so
        the order of the two streams is the order they happened in.
        """
        expired = threading.Event()
        lines: list[str] = []
        # Its own session: killing `chroot` leaves the emerge inside it running,
        # still holding the target's bind mounts.
        with subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self._environment(),
            start_new_session=True,
        ) as process:

            def stop() -> None:
                expired.set()
                _kill_group(process)

            # A timer, not a deadline checked per line: a command that hangs
            # without printing anything never reaches a per-line check.
            watchdog = threading.Timer(timeout, stop) if timeout is not None else None
            if watchdog is not None:
                watchdog.start()
            try:
                if input_text is not None:
                    # communicate(), not a write before the read loop: input
                    # past the pipe buffer deadlocks against a blocked writer.
                    written, _ = process.communicate(input_text)
                    lines.append(written)
                    if self.echo:
                        self.log(f"| {written.rstrip()}")
                elif process.stdout is not None:
                    for line in process.stdout:
                        lines.append(line)
                        if self.echo:
                            self.log(f"| {line.rstrip()}")
                returncode = process.wait()
            except BaseException:
                _kill_group(process)
                raise
            finally:
                if watchdog is not None:
                    watchdog.cancel()
        # A timer that fired after the command already exited must not turn a
        # clean run into a timeout, so the signal decides, not the flag alone.
        if expired.is_set() and returncode < 0:
            raise CommandFailed(
                f"{shlex.join(_display_argv(argv))} did not finish within {timeout}s"
            )
        return "".join(lines), returncode

    def in_target(self, target: Path) -> Runner:
        """A runner whose commands land inside the target's chroot."""
        return Runner(
            log=self.log,
            journal=self.journal,
            dry_run=self.dry_run,
            echo=self.echo,
            prefix=("chroot", str(target)),
            # The target's /boot is already mounted where the layout says it is.
            environment={**self.environment, "DONT_MOUNT_BOOT": "1"},
            proxy=self.proxy,
            history=self.history,
        )

    def _environment(self) -> dict[str, str] | None:
        values = dict(self.environment)
        # Password-bearing proxy URLs stay in tool configuration, never process environment.
        if self.proxy is not None and self.proxy.enabled and not self.proxy.password:
            values.update(
                {
                    "http_proxy": self.proxy.redacted_url,
                    "https_proxy": self.proxy.redacted_url,
                    "all_proxy": self.proxy.redacted_url,
                    "no_proxy": ",".join(self.proxy.bypass),
                    "HTTP_PROXY": self.proxy.redacted_url,
                    "HTTPS_PROXY": self.proxy.redacted_url,
                    "ALL_PROXY": self.proxy.redacted_url,
                    "NO_PROXY": ",".join(self.proxy.bypass),
                }
            )
        if not values:
            return None
        return {**os.environ, **values}


def _display_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Redact URL user information before a command reaches logs or journals."""
    shown: list[str] = []
    for value in argv:
        try:
            parts = urllib.parse.urlsplit(value)
        except ValueError:
            shown.append(value)
            continue
        if parts.scheme and parts.hostname and parts.username is not None:
            host = parts.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parts.port is not None:
                host = f"{host}:{parts.port}"
            value = urllib.parse.urlunsplit(
                (parts.scheme, host, parts.path, parts.query, parts.fragment)
            )
        shown.append(value)
    return tuple(shown)


#: What a failing command's own error looks like. Portage keeps printing after
#: one, so the last lines of the output are news items rather than the cause.
_COMPLAINT = re.compile(r"\b(ERROR|error:|failed|Call stack|died|cannot|No such file)", re.I)


def _tail(text: str, lines: int = 5) -> str:
    """The lines worth reading, which are rarely the last ones."""
    kept = [line.strip() for line in text.splitlines() if line.strip()]
    complaints = [line for line in kept if _COMPLAINT.search(line)]
    chosen = complaints[:lines] if complaints else kept[-lines:]
    return " | ".join(chosen) if chosen else "no output"


def write_file(path: Path, content: str, mode: int = 0o644) -> None:
    """Write a file the installer owns, creating the directories above it.

    The mode is set before the content is written: writing first and narrowing
    afterwards leaves a secret readable for the interval in between.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    # `os.open` applies its mode only when it creates the file, so an existing
    # 0644 file stays readable through the write without this.
    os.fchmod(handle, mode)
    with os.fdopen(handle, "w") as opened:
        opened.write(content)


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Kill the whole session, not the direct child."""
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


class TargetEscape(Exception):
    """A path inside the target resolved to something outside it."""


def open_in_target(
    target: Path, path: PurePosixPath, flags: int, mode: int = 0o644, *, parents: bool = False
) -> int:
    """Open a target-absolute path with every component rooted in the target.

    `target / path` is a lexical join, so one absolute symlink under the target
    -- shipped by a stage3, or left on a filesystem the operator reused -- makes
    the installer write to the live system as root. Each component is opened
    with `O_NOFOLLOW` against its parent's descriptor instead, so a symlink
    anywhere on the way is refused rather than followed.
    """
    parts = path.relative_to("/").parts
    if not parts:
        raise TargetEscape(f"{path} names the target itself, not a file in it")
    handle = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts[:-1]:
            if parents:
                try:
                    os.mkdir(name, 0o755, dir_fd=handle)
                except FileExistsError:
                    pass
            try:
                step = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle
                )
            except OSError as error:
                raise TargetEscape(f"{path}: {name} is not a directory in the target") from error
            os.close(handle)
            handle = step
        try:
            return os.open(parts[-1], flags | os.O_NOFOLLOW, mode, dir_fd=handle)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK):
                raise TargetEscape(f"{path} is a symlink in the target") from error
            raise
    finally:
        os.close(handle)


def under(target: Path, path: PurePosixPath) -> Path:
    """A target-absolute path as a path on the installing system."""
    return target / path.relative_to("/") if str(path) != "/" else target
