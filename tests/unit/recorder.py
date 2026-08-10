"""A `Context` that records instead of doing.

`apply()` is the half of an operation a golden file cannot see. Recording the
argv it would run is how the flags get asserted without a disk.
"""

from __future__ import annotations

from gentoo_install.errors import CommandFailed, DownloadFailed
from gentoo_install.plan.operations import CommandOutput
from collections.abc import Callable
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
    #: The mode each file was written with. A keyfile NetworkManager refuses
    #: for being world-readable is a mode the plan has to be able to assert.
    modes: dict[PurePosixPath, int] = field(default_factory=dict)
    stdin: list[str] = field(default_factory=list)
    #: What `run_in_target` returns, keyed by the first word of the command.
    replies: dict[str, str] = field(default_factory=dict)
    #: Commands whose first word is here raise instead of returning.
    failures: set[str] = field(default_factory=set)
    given_up: set[str] = field(default_factory=set)
    #: Consulted before the replies table, and injected rather than assigned
    #: over the bound method: five tests replaced `run_in_target` itself, which
    #: mypy accepted only under `# type: ignore[method-assign]`. Answering
    #: `None` falls through to the ordinary behaviour.
    answering: Callable[[Sequence[str]], str | None] | None = None

    def run(
        self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
    ) -> str:
        self.commands.append(tuple(argv))
        if input_text is not None:
            self.stdin.append(input_text)
        if argv[0] in self.failures:
            raise CommandFailed(f"{argv[0]} exited 1")
        # A CommandOutput, the way the real runner answers: a double returning
        # a bare str hides every caller that reads the exit code for itself.
        return CommandOutput(self.replies.get(argv[0], ""), 0)

    def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> str:
        """`check=False` returns the output and raises nothing, the way the real
        one does: a double that raises anyway hides every caller that reads an
        exit code for itself."""
        self.in_target.append(tuple(argv))
        if self.answering is not None:
            said = self.answering(argv)
            if said is not None:
                return said
        if argv[0] in self.failures:
            if check:
                raise CommandFailed(f"{argv[0]} exited 1")
            return self.replies.get(argv[0], "")
        return self.replies.get(argv[0], "1")

    def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
        self.files[path] = content
        self.modes[path] = mode

    def read(self, path: PurePosixPath) -> str:
        return self.files.get(path, "")

    def append(self, path: PurePosixPath, content: str) -> None:
        self.files[path] = self.files.get(path, "") + content

    def device_path(self, device: DeviceId) -> str:
        return f"/dev/mapper/{device}"

    #: Directories the double reports as already mounted, for the resume path.
    mounts: set[str] = field(default_factory=set)

    def is_mounted(self, path: str) -> bool:
        return path in self.mounts

    def passphrase(self, device: DeviceId) -> str:
        return "a passphrase"

    def key_file(self, device: DeviceId) -> PurePosixPath:
        return PurePosixPath(f"/run/gentoo-install/keys/{device}")

    def containing_disk(self, device: DeviceId) -> str:
        return "/dev/vda"

    def partition_index(self, device: DeviceId) -> int:
        return 1

    def array_uuid(self, device: DeviceId) -> str:
        return self.replies.get("mdadm-uuid", "1111:2222:3333:4444")

    def degrade(self, what: str, reason: str) -> None:
        self.given_up.add(what)

    def degraded(self, what: str) -> bool:
        return what in self.given_up

    def jobs(self) -> int:
        return 4

    def device_uuid(self, device: DeviceId) -> str:
        return f"uuid-of-{device}"

    def filesystem_type(self, device: DeviceId) -> str:
        """What a reused device is said to hold. Set per test through
        `replies`, keyed by the id, so a mismatch can be exercised."""
        return self.replies.get(f"type-of-{device}", "")

    def rank_mirrors(self, candidates: tuple[str, ...]) -> tuple[str, ...]:
        self.commands.append(("rank-mirrors", *candidates))
        return tuple(reversed(candidates))

    #: What `fetch_text` answers, by URL. A URL nobody staged raises, so a
    #: test cannot pass by accident on an empty script.
    pages: dict[str, str] = field(default_factory=dict)

    def fetch_text(self, url: str) -> str:
        self.commands.append(("fetch-text", url))
        if url not in self.pages:
            raise DownloadFailed(f"nothing staged for {url}")
        return self.pages[url]

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
