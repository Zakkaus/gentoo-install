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


class Context(Protocol):
    """Everything an operation is allowed to do to a machine.

    `exec/apply.py` implements it against the real runner and `exec/fake.py`
    records the calls instead, so one operation list drives both.
    """

    @property
    def target(self) -> PurePosixPath:
        """Where the new system is mounted, `/mnt/gentoo` in an ordinary run."""

    def run(self, argv: Sequence[str]) -> str:
        """Run a command on the installing system and return its stdout."""

    def run_in_target(self, argv: Sequence[str]) -> str:
        """Run a command inside the target's chroot and return its stdout."""

    def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
        """Write a file in the target, `path` being absolute inside the target."""

    def append(self, path: PurePosixPath, content: str) -> None:
        """Append to a file in the target, creating it when absent."""

    def device_path(self, device: DeviceId) -> str:
        """Resolve a device id to the path it currently has, such as `/dev/sda1`."""

    def key_file(self, device: DeviceId) -> PurePosixPath:
        """Where the passphrase of an encrypted device is staged for this run."""

    def boot_disk(self) -> str:
        """The whole disk a bootloader installs to, such as `/dev/sda`."""

    def device_uuid(self, device: DeviceId) -> str:
        """The UUID of a formatted device, for fstab and crypttab."""

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
