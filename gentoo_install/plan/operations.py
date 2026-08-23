# SPDX-License-Identifier: GPL-2.0-or-later
"""What an installation is made of: one named operation per thing that happens.

`describe()` is what a dry run prints and `apply()` is what an install performs.
They are two methods on one object so the preview cannot drift from the run, and
every operation is a named type rather than a command string, so a plan can be
compared, sorted and tested without parsing anything.

`Context` is the seam to `exec/`. The plan layer never imports `exec`, which
keeps it a pure function of the configuration.
"""

from __future__ import annotations

import signal

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
import re
from collections.abc import Callable
from typing import ClassVar, Final, Protocol, Sequence

from ..errors import CommandFailed
from ..model.device import DeviceId
from ..model.validate import KernelCeiling


class Stage(Enum):
    """The install phases of docs/design.md. Declaration order is run order."""

    PREFLIGHT = "preflight"
    PARTITION = "partition"
    ARRAY = "array"
    FORMAT = "format"
    ZFS = "zfs"
    MOUNT = "mount"
    STAGE3 = "stage3"
    CHROOT = "chroot"
    PORTAGE = "portage"
    SYSTEM = "system"
    KERNEL = "kernel"
    BOOTLOADER = "bootloader"
    PACKAGES = "packages"
    FINISH = "finish"

    @property
    def order(self) -> int:
        return list(Stage).index(self)


def ending(returncode: int) -> str:
    """How a command ended, in words a reader can act on.

    `subprocess` answers a negative code for a signal, and `emerge failed with
    exit -13` sent a reader looking for an exit status that does not exist.
    """
    if returncode >= 0:
        return f"exit {returncode}"
    try:
        return f"{signal.Signals(-returncode).name} ({-returncode})"
    except ValueError:
        return f"signal {-returncode}"


#: What a failing command's own error looks like. Portage keeps printing
#: after one, so the last lines of the output are news items rather than the
#: cause: an emerge that stopped at `!!! Couldn't download
#: 'cjktty-font-unifont-15.1.04.patch'. Aborting.` was reported to the
#: operator as `1 news items need reading`.
COMPLAINT: Final[re.Pattern[str]] = re.compile(
    r"(\bERROR|error:|\bfailed\b|Call stack|\bdied\b|\bcannot\b|No such file"
    r"|^!!!|Aborting)",
    re.I | re.M,
)


def worth_reading(text: str, lines: int = 5) -> str:
    """The lines of a failed command worth reading, rarely the last ones."""
    kept = [line.strip() for line in text.splitlines() if line.strip()]
    complaints = [line for line in kept if COMPLAINT.search(line)]
    chosen = complaints[:lines] if complaints else kept[-lines:]
    return " | ".join(chosen) if chosen else "no output"


class CommandOutput(str):
    """Command text with the exit status retained for callers that inspect it."""

    returncode: int

    def __new__(cls, stdout: str, returncode: int) -> CommandOutput:
        output = super().__new__(cls, stdout)
        output.returncode = returncode
        return output

    @property
    def ending(self) -> str:
        return ending(self.returncode)


def answered(output: str, probe: str, *, allowed: tuple[int, ...] = (0,)) -> str:
    """The text a probe answered, or a named failure.

    `check=False` keeps a probe's failure out of the exception path and the
    runner merges stderr into stdout, so the diagnostic arrives where the
    answer belongs: `zfs is not installed` is not `yes`, and the caller that
    compares the two folds a probe that never ran into the branch for a
    negative answer.
    """
    # A Context whose `run` honours the declared `-> str` carries no exit
    # status, so a probe whose ending cannot be read is treated as one failing.
    if not isinstance(output, CommandOutput) or output.returncode not in allowed:
        raise CommandFailed(f"{probe}: {str(output).strip()[:200] or 'no output'}")
    return str(output).strip()


class Context(Protocol):
    """Everything an operation is allowed to do to a machine.

    `exec/apply.py` implements it against the real runner and `exec/fake.py`
    records the calls instead, so one operation list drives both.
    """

    @property
    def target(self) -> PurePosixPath:
        """Where the new system is mounted, `/mnt/gentoo` in an ordinary run."""

    def run(
        self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
    ) -> str:
        """Run a command on the installing system and return its stdout.

        `check=False` for a command whose failure is an answer rather than a
        fault, such as deactivating swap that was never active.
        """

    def pipe(self, producer: Sequence[str], consumer: Sequence[str]) -> None:
        """Stream `producer` into `consumer` and fail when either command fails."""

    def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> str:
        """Run a command inside the target's chroot and return its stdout."""

    def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
        """Write a file in the target, `path` being absolute inside the target."""

    def read(self, path: PurePosixPath) -> str:
        """A file in the target, or an empty string when it is not there."""

    def append(self, path: PurePosixPath, content: str) -> None:
        """Append to a file in the target, creating it when absent."""

    def device_path(self, device: DeviceId) -> str:
        """Resolve a device id to the path it currently has, such as `/dev/sda1`."""

    def key_file(self, device: DeviceId) -> PurePosixPath:
        """Where the passphrase of an encrypted device is staged for this run.

        A path on the installing system, not inside the target: it is passed to
        `cryptsetup`, which runs outside the chroot.
        """

    def swap_directories(
        self,
        staging: PurePosixPath,
        names: Sequence[str],
        copy: Callable[[Path, Path], None],
    ) -> None:
        """Replace each named directory of the running system from `staging`.

        Here rather than imported: `plan/convert.py` reached
        `gentoo_install.exec.convert` through `importlib` for as long as this
        seam has existed, and the guard that forbids that edge walks `import`
        statements, which never see one.
        """

    def populate_boot(self, staging: PurePosixPath) -> None:
        """Fill the staged `/boot` from the running system's own."""

    def containing_disk(self, device: DeviceId) -> str:
        """The whole disk a device sits on, such as `/dev/sda`."""

    def partition_index(self, device: DeviceId) -> int:
        """A partition's number, which is what `efibootmgr --part` wants."""

    def jobs(self) -> int:
        """How many compile jobs this machine should run at once."""

    def device_uuid(self, device: DeviceId) -> str:
        """The UUID of a formatted device, for fstab and crypttab."""

    def is_mounted(self, path: str) -> bool:
        """Whether anything is mounted at that directory in the target.

        Asked by a mount that a resumed run reaches for the second time: the
        answer is state of the running machine, not of the disk.
        """
        return False

    def filesystem_type(self, device: DeviceId) -> str:
        """What is on the device now, as `blkid` names it. Empty for nothing."""

    def rank_mirrors(self, candidates: tuple[str, ...]) -> tuple[str, ...]:
        """Measure each candidate and return them fastest first. A candidate
        that times out goes last rather than removing itself."""

    def passphrase(self, device: DeviceId) -> str:
        """The passphrase for an encrypted device, for a command that wants it
        on stdin rather than in a file."""

    def array_uuid(self, device: DeviceId) -> str:
        """The mdadm UUID of an assembled array, which is not the UUID of the
        filesystem on it and is what `rd.md.uuid` names."""

    def degrade(self, what: str, reason: str) -> None:
        """Record that `what` failed and the install continued without it.

        A binary host whose trust could not be set up degrades to compiling
        rather than stopping a run that has already written the disks.
        """

    def degraded(self, what: str) -> bool:
        """Whether `what` was given up on earlier in this run."""

    def fetch_text(self, url: str) -> str:
        """Read a URL while the installer still has a network.

        A first-boot script is fetched here rather than by the machine that is
        about to run it: a download that fails at first boot leaves it
        half-configured with nobody watching.
        """

    def fetch_stage3(
        self,
        mirror: str,
        variant: str,
        fingerprint: str,
        fallbacks: Sequence[str] = (),
    ) -> PurePosixPath:
        """Download the newest stage3 of `variant`, check its signature against
        `fingerprint` and its digest, and return where it was written.

        `fallbacks` are tried in order when a mirror cannot be reached at all."""

    def zfs_kernel_max(self) -> KernelCeiling:
        """Read the selected target tree's authoritative ZFS kernel ceiling."""


@dataclass(frozen=True, kw_only=True)
class Operation(ABC):
    #: Executables this operation runs on the installing system. Commands run
    #: inside the target are supplied by the target and do not belong here.
    host_commands: ClassVar[tuple[str, ...]] = ()

    #: Which phase this runs in. A subclass fixes it with a default; the few
    #: operations that serve several phases take it from the caller.
    stage: Stage

    def describe_parts(self) -> tuple[str, tuple[str, ...]] | None:
        """The source template and rendered values for a translatable description."""
        return None

    def describe(self) -> str:
        """One line, present tense, naming the concrete arguments."""
        parts = self.describe_parts()
        if parts is None:
            raise NotImplementedError(f"{type(self).__name__} must define describe()")
        template, values = parts
        return template.format(*values)

    @abstractmethod
    def apply(self, context: Context) -> None: ...

    def required_host_commands(self) -> frozenset[str]:
        """Executables the live medium must provide for this operation."""
        return frozenset(self.host_commands)

    @property
    def wrapped(self) -> "Operation | None":
        """The operation this one stands in for, when it stands in for one.

        Preflight keys part of its command table on the operation's type, and
        a wrapper is not an instance of what it wraps: without this a staged
        conversion reported that it needed no `tar`.
        """
        return None

    @property
    def releases_the_machine(self) -> bool:
        """Whether this still runs once the install has already failed.

        The closing stage has to unmount either way. The rest of it configures
        the installed system, and a run that stopped before the stage3 was
        unpacked has no installed system to configure: those operations then
        fail inside an empty chroot and their error replaces the real one.
        """
        return False

    @property
    def survives_a_reboot(self) -> bool:
        """Whether finishing this once means a resumed run may skip it.

        A partition table and a filesystem are on the disk. A mount is not: a
        run that stopped, was rebooted and resumed would skip every mount and
        unpack the stage3 into the live medium's own tmpfs until the machine
        ran out of memory, with nothing in the log saying the target was never
        mounted.
        """
        return True
