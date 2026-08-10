"""What an installation is made of: one named operation per thing that happens.

`describe()` is what a dry run prints and `apply()` is what an install performs.
They are two methods on one object so the preview cannot drift from the run, and
every operation is a named type rather than a command string, so a plan can be
compared, sorted and tested without parsing anything.

`Context` is the seam to `exec/`. The plan layer never imports `exec`, which
keeps it a pure function of the configuration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Protocol, Sequence

from ..model.device import DeviceId


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


class CommandOutput(str):
    """Command text with the exit status retained for callers that inspect it."""

    returncode: int

    def __new__(cls, stdout: str, returncode: int) -> CommandOutput:
        output = super().__new__(cls, stdout)
        output.returncode = returncode
        return output


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

    def fetch_stage3(self, mirror: str, variant: str, fingerprint: str) -> PurePosixPath:
        """Download the newest stage3 of `variant`, check its signature against
        `fingerprint` and its digest, and return where it was written."""


@dataclass(frozen=True, kw_only=True)
class Operation(ABC):
    #: Which phase this runs in. A subclass fixes it with a default; the few
    #: operations that serve several phases take it from the caller.
    stage: Stage

    @abstractmethod
    def describe(self) -> str:
        """One line, present tense, naming the concrete arguments."""

    @abstractmethod
    def apply(self, context: Context) -> None: ...

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
