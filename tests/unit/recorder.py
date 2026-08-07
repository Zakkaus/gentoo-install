"""A `Context` that records instead of doing.

`apply()` is the half of an operation a golden file cannot see. Recording the
argv it would run is how the flags get asserted without a disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Sequence

from gentoo_install.model.device import DeviceId


@dataclass
class Recorder:
    target: PurePosixPath = PurePosixPath("/mnt/gentoo")
    commands: list[tuple[str, ...]] = field(default_factory=list)
    in_target: list[tuple[str, ...]] = field(default_factory=list)
    files: dict[PurePosixPath, str] = field(default_factory=dict)
    #: What `run_in_target` returns, keyed by the first word of the command.
    replies: dict[str, str] = field(default_factory=dict)

    def run(self, argv: Sequence[str], *, check: bool = True) -> str:
        self.commands.append(tuple(argv))
        return self.replies.get(argv[0], "")

    def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> str:
        self.in_target.append(tuple(argv))
        return self.replies.get(argv[0], "")

    def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
        self.files[path] = content

    def append(self, path: PurePosixPath, content: str) -> None:
        self.files[path] = self.files.get(path, "") + content

    def device_path(self, device: DeviceId) -> str:
        return f"/dev/mapper/{device}"

    def key_file(self, device: DeviceId) -> PurePosixPath:
        return PurePosixPath(f"/run/gentoo-install/keys/{device}")

    def containing_disk(self, device: DeviceId) -> str:
        return "/dev/vda"

    def partition_index(self, device: DeviceId) -> int:
        return 1

    def jobs(self) -> int:
        return 4

    def device_uuid(self, device: DeviceId) -> str:
        return f"uuid-of-{device}"

    def rank_mirrors(self, candidates: tuple[str, ...]) -> tuple[str, ...]:
        self.commands.append(("rank-mirrors", *candidates))
        return tuple(reversed(candidates))

    def fetch_stage3(self, mirror: str, variant: str, fingerprint: str) -> PurePosixPath:
        self.commands.append(("fetch-stage3", mirror, variant, fingerprint))
        return PurePosixPath("/var/cache/gentoo-install/stage3.tar.xz")

    def argv_starting(self, *prefix: str) -> tuple[tuple[str, ...], ...]:
        both = [*self.commands, *self.in_target]
        return tuple(argv for argv in both if argv[: len(prefix)] == prefix)

    def only(self, *prefix: str) -> tuple[str, ...]:
        found = self.argv_starting(*prefix)
        if len(found) != 1:
            raise AssertionError(f"expected one {' '.join(prefix)!r}, recorded {len(found)}")
        return found[0]
