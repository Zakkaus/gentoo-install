"""The only module in the package that imports `subprocess`.

Every external command goes through here, so there is one place that owns exit
codes, output capture and the log. A caller that wants to run something without
it would also be deciding, on its own, what a failure means.
"""

from __future__ import annotations

import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from ..errors import CommandFailed
from ..log import Journal


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


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
    history: list[Result] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: float | None = 3600.0,
    ) -> Result:
        full = (*self.prefix, *argv)
        if self.dry_run:
            self.log(f"would run: {shlex.join(full)}")
            return Result(argv=full, returncode=0, stdout="", stderr="", seconds=0.0)
        started = time.monotonic()
        self.log(f"run: {shlex.join(full)}")
        try:
            output, returncode = self._stream(full, input_text, timeout)
        except FileNotFoundError as error:
            raise CommandFailed(f"{full[0]} is not installed") from error
        result = Result(
            argv=full,
            returncode=returncode,
            stdout=output,
            stderr="",
            seconds=time.monotonic() - started,
        )
        self.history.append(result)
        if self.journal is not None:
            self.journal.command(result.argv, result.returncode, result.seconds)
            if "emerge" in full:
                self.journal.packages(result.stdout)
        if check and result.returncode != 0:
            raise CommandFailed(
                f"{result.command} exited {result.returncode}: {_tail(result.stderr or result.stdout)}"
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
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self._environment(),
        )
        if input_text is not None and process.stdin is not None:
            process.stdin.write(input_text)
            process.stdin.close()
        # A timer, not a deadline checked per line: a command that hangs without
        # printing anything never reaches a per-line check.
        expired = threading.Event()

        def stop() -> None:
            expired.set()
            process.kill()

        watchdog = threading.Timer(timeout, stop) if timeout is not None else None
        if watchdog is not None:
            watchdog.start()
        lines: list[str] = []
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    lines.append(line)
                    if self.echo:
                        self.log(f"| {line.rstrip()}")
            returncode = process.wait()
        finally:
            if watchdog is not None:
                watchdog.cancel()
        if expired.is_set():
            raise CommandFailed(f"{shlex.join(argv)} did not finish within {timeout}s")
        return "".join(lines), returncode

    def in_target(self, target: Path) -> Runner:
        """A runner whose commands land inside the target's chroot."""
        return Runner(
            log=self.log,
            journal=self.journal,
            dry_run=self.dry_run,
            echo=self.echo,
            prefix=("chroot", str(target)),
            # DONT_MOUNT_BOOT: the target's /boot is already mounted where the
            # layout says, and the kernel's install hook would mount it again.
            environment={**self.environment, "DONT_MOUNT_BOOT": "1"},
            history=self.history,
        )

    def _environment(self) -> dict[str, str] | None:
        if not self.environment:
            return None
        import os

        return {**os.environ, **self.environment}


def _tail(text: str, lines: int = 5) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()][-lines:]
    return " | ".join(kept) if kept else "no output"


def write_file(path: Path, content: str, mode: int = 0o644) -> None:
    """Write a file the installer owns, creating the directories above it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def under(target: Path, path: PurePosixPath) -> Path:
    """A target-absolute path as a path on the installing system."""
    return target / path.relative_to("/") if str(path) != "/" else target
