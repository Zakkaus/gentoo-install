"""The only module in the package that imports `subprocess`.

Every external command goes through here, so there is one place that owns exit
codes, output capture and the log. A caller that wants to run something without
it would also be deciding, on its own, what a failure means.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from ..errors import CommandFailed


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
    dry_run: bool = False
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
            completed = subprocess.run(
                full,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._environment(),
            )
        except FileNotFoundError as error:
            raise CommandFailed(f"{full[0]} is not installed") from error
        except subprocess.TimeoutExpired as error:
            raise CommandFailed(f"{shlex.join(full)} did not finish within {timeout}s") from error
        result = Result(
            argv=full,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            seconds=time.monotonic() - started,
        )
        self.history.append(result)
        if check and result.returncode != 0:
            raise CommandFailed(
                f"{result.command} exited {result.returncode}: {_tail(result.stderr or result.stdout)}"
            )
        return result

    def in_target(self, target: Path) -> Runner:
        """A runner whose commands land inside the target's chroot."""
        return Runner(
            log=self.log,
            dry_run=self.dry_run,
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
