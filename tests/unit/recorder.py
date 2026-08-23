# SPDX-License-Identifier: GPL-2.0-or-later
"""A `Context` that records instead of doing.

`apply()` is the half of an operation a golden file cannot see. Recording the
argv it would run is how the flags get asserted without a disk.
"""

from __future__ import annotations

from gentoo_install.errors import CommandFailed, DownloadFailed
from gentoo_install.plan.operations import CommandOutput
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Sequence

from gentoo_install.model.device import DeviceId
from gentoo_install.model.validate import KernelCeiling


FAKE_PORTAGE_TRUST_KEY: Final[str] = "0D83BD4123FE15FE2F7D30E457D1F2DF9E5D8B84"

def _answered(text: str, returncode: int) -> CommandOutput:
    """A reply already carrying an exit code keeps it; a bare string takes the
    caller's. `replies` is typed for `str`, and `CommandOutput` is one, so a
    test that configures a failure would otherwise have it rebuilt as success.
    """
    return text if isinstance(text, CommandOutput) else CommandOutput(text, returncode)


@dataclass
class Recorder:
    target: PurePosixPath = PurePosixPath("/mnt/gentoo")
    commands: list[tuple[str, ...]] = field(default_factory=list)
    pipelines: list[tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)
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
    locally_signed: set[str] = field(default_factory=set)
    given_up: set[str] = field(default_factory=set)
    #: What the conversion seam was asked to do, rather than doing it.
    swapped: list[tuple[PurePosixPath, tuple[str, ...]]] = field(default_factory=list)
    populated: list[PurePosixPath] = field(default_factory=list)
    #: Why each one was given up. The reason is what `install.jsonl` records
    #: and what an operator reads, so a double that drops it cannot hold it.
    degradations: dict[str, str] = field(default_factory=dict)
    #: Consulted before the replies table, and injected rather than assigned
    #: over the bound method: five tests replaced `run_in_target` itself, which
    #: mypy accepted only under `# type: ignore[method-assign]`. Answering
    #: `None` falls through to the ordinary behaviour.
    answering: Callable[[Sequence[str]], str | None] | None = None
    zfs_ceiling: KernelCeiling = field(default_factory=lambda: KernelCeiling(None))
    image_devices: dict[DeviceId, str] = field(default_factory=dict)
    existing_paths: set[str] = field(default_factory=set)
    device_event_settles: int = 0

    def run(
        self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
    ) -> CommandOutput:
        self.commands.append(tuple(argv))
        if input_text is not None:
            self.stdin.append(input_text)
        if argv[0] in self.failures:
            raise CommandFailed(f"{argv[0]} exited 1")
        if argv[:2] == ["test", "-e"]:
            return CommandOutput("", 0 if argv[2] in self.existing_paths else 1)
        if argv[:2] == ["findmnt", "--mountpoint"]:
            mounted = argv[2] in self.mounts
            return CommandOutput(f"{argv[2]}\n" if mounted else "", 0 if mounted else 1)
        if self.answering is not None:
            # Both methods, or a hook covering one of them leaves the other
            # answering an empty string that every caller reads as success.
            said = self.answering(argv)
            if said is not None:
                return _answered(said, 0)
        # A CommandOutput, the way the real runner answers: a double returning
        # a bare str hides every caller that reads the exit code for itself.
        return _answered(self.replies.get(argv[0], ""), 0)

    def pipe(self, producer: Sequence[str], consumer: Sequence[str]) -> None:
        self.pipelines.append((tuple(producer), tuple(consumer)))

    def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> CommandOutput:
        """`check=False` returns the output and raises nothing, the way the real
        one does: a double that raises anyway hides every caller that reads an
        exit code for itself.

        Every branch answers a `CommandOutput`, so a caller that degrades or
        retries on a non-zero code is exercised rather than handed a bare `str`
        whose truth value reads as success.
        """
        self.in_target.append(tuple(argv))
        if self.answering is not None:
            said = self.answering(argv)
            if said is not None:
                return _answered(said, 0)
        if argv[0] in self.failures:
            if check:
                raise CommandFailed(f"{argv[0]} exited 1")
            return _answered(self.replies.get(argv[0], ""), 1)
        if argv[0] == "gpg" and "--lsign-key" in argv:
            self.locally_signed.add(argv[-1].upper())
        if argv[0] == "gpg" and "--with-colons" in argv and "--list-sigs" in argv:
            target = argv[-1].upper()
            if target in self.locally_signed:
                return CommandOutput(
                    f"sig:::1:0000000000000000:0::::Portage:10l::{FAKE_PORTAGE_TRUST_KEY}:\n",
                    0,
                )
            return CommandOutput("", 0)
        if argv[:3] == ["portageq", "best_visible", "/"]:
            return CommandOutput(f"{argv[-1]}-1\n", 0)
        if argv[:4] == ["portageq", "metadata", "/", "ebuild"]:
            return CommandOutput("+dracut systemd systemd-boot boot kernel-install cryptsetup "
                                 "client server arping dist-kernel cjk elogind\n\n", 0)
        if argv[0] == "test":
            return _answered(
                self.replies.get("test", ""), 1 if self.replies.get("test") else 0
            )
        if argv[0] == "qlist":
            return CommandOutput("/boot/kernel-6.18.41-gentoo-dist-bin\n/boot/initramfs-6.18.41-gentoo-dist-bin.img\n", 0)
        return _answered(self.replies.get(argv[0], "1"), 0)

    def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
        self.files[path] = content
        self.modes[path] = mode

    def read(self, path: PurePosixPath) -> str:
        if path in self.files:
            return self.files[path]
        if path == PurePosixPath("/var/lib/misc/installkernel"):
            return "date\tsystemd\t6.18.41-gentoo-dist-bin\t/usr/lib/kernel\tcompat\tdracut\tnone\t/boot\tkernel-6.18.41-gentoo-dist-bin\tinitramfs-6.18.41-gentoo-dist-bin.img\tnotset\n"
        if path == PurePosixPath("/etc/portage/gnupg/mykeyid"):
            return f"{FAKE_PORTAGE_TRUST_KEY}\n"
        return self.files.get(path, "")

    def append(self, path: PurePosixPath, content: str) -> None:
        self.files[path] = self.files.get(path, "") + content

    def device_path(self, device: DeviceId) -> str:
        return self.image_devices.get(device, f"/dev/mapper/{device}")


    def settle_device_events(self) -> None:
        self.device_event_settles += 1
    def remember_image_device(self, device: DeviceId, path: str) -> None:
        self.image_devices[device] = path

    def image_device_path(self, device: DeviceId) -> str | None:
        return self.image_devices.get(device)

    def release_image_device(self, device: DeviceId) -> None:
        self.image_devices.pop(device, None)

    #: Directories the double reports as already mounted, for the resume path.
    mounts: set[str] = field(default_factory=set)

    def is_mounted(self, path: str) -> bool:
        return path in self.mounts

    def passphrase(self, device: DeviceId) -> str:
        return "a passphrase"

    def key_file(self, device: DeviceId) -> PurePosixPath:
        return PurePosixPath(f"/run/gentoo-install/keys/{device}")

    def swap_directories(
        self,
        staging: PurePosixPath,
        names: Sequence[str],
        copy: Callable[[Path, Path], None],
    ) -> None:
        # Recorded rather than performed: the real one renames the running
        # system's directories, and a unit test must not.
        self.swapped.append((staging, tuple(names)))

    def populate_boot(self, staging: PurePosixPath) -> None:
        self.populated.append(staging)

    def containing_disk(self, device: DeviceId) -> str:
        return "/dev/vda"

    def partition_index(self, device: DeviceId) -> int:
        return 1

    def array_uuid(self, device: DeviceId) -> str:
        return self.replies.get("mdadm-uuid", "1111:2222:3333:4444")

    def degrade(self, what: str, reason: str) -> None:
        self.given_up.add(what)
        self.degradations[what] = reason

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

    def fetch_stage3(
        self,
        mirror: str,
        variant: str,
        fingerprint: str,
        fallbacks: Sequence[str] = (),
    ) -> PurePosixPath:
        self.commands.append(("fetch-stage3", mirror, variant, fingerprint, *fallbacks))
        return PurePosixPath("/var/cache/gentoo-install/stage3.tar.xz")

    def zfs_kernel_max(self) -> KernelCeiling:
        return self.zfs_ceiling

    def argv_starting(self, *prefix: str) -> tuple[tuple[str, ...], ...]:
        both = [*self.commands, *self.in_target]
        return tuple(argv for argv in both if argv[: len(prefix)] == prefix)

    def only(self, *prefix: str) -> tuple[str, ...]:
        found = self.argv_starting(*prefix)
        if len(found) != 1:
            raise AssertionError(f"expected one {' '.join(prefix)!r}, recorded {len(found)}")
        return found[0]
