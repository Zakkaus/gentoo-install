# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Iterable

import pytest

from gentoo_install.errors import PreflightFailed
from gentoo_install.exec import preflight
from gentoo_install.exec.probe import Machine, Probe
from gentoo_install.exec.runner import Runner
from gentoo_install.plan.operations import Context, Operation, Stage
from gentoo_install.model.config import DiskMode, InstallConfig
from gentoo_install.model.device import DeviceGraph

from .layouts import config, i


@dataclass(frozen=True, kw_only=True)
class NeedsMediumCommand(Operation):
    stage: Stage = Stage.PREFLIGHT
    host_commands: ClassVar[tuple[str, ...]] = ("medium-tool",)

    def describe(self) -> str:
        return "use the test medium tool"

    def apply(self, context: Context) -> None:
        raise AssertionError("preflight ran an operation")


@dataclass(frozen=True, kw_only=True)
class NeedsTar(NeedsMediumCommand):
    host_commands: ClassVar[tuple[str, ...]] = ("tar",)


def _machine(
    commands: frozenset[str], *, versions: dict[str, str] | None = None
) -> Machine:
    return Machine(
        architecture="x86_64",
        uefi=True,
        root=True,
        memory_bytes=16 * 1024**3,
        commands=commands,
        release_key=True,
        versions=versions if versions is not None else {},
        efi_variables=True,
        efi_bits=64,
    )


def _probe(tmp_path: Path) -> Probe:
    return Probe(runner=Runner(log=lambda line: None), work=tmp_path)


def dd_config() -> InstallConfig:
    installation = config()
    return replace(
        installation,
        disk=replace(
            installation.disk,
            graph=DeviceGraph.build(()),
            root=i(""),
            mode=DiskMode.DD,
            source="/run/prepared.raw",
            destination="/dev/disk/by-id/virtio-target",
        ),
    )


class ImageWriteProbe(Probe):
    def live_medium(self) -> str:
        return "the root filesystem is overlay"

    def image_source_exists(self, source: str) -> bool:
        return True

    def whole_disk(self, selector: str) -> bool:
        return True

    def mounted(self, disk: str, ignoring: str = "") -> bool:
        return False

def test_dd_requires_a_live_or_memory_environment(tmp_path: Path) -> None:
    class Installed(ImageWriteProbe):
        def live_medium(self) -> str:
            return ""

        def memory_environment(self) -> bool:
            return False

    report = preflight.inspect(
        dd_config(),
        _machine(frozenset({"cat", "dd", "lsblk", "swapon"})),
        Installed(runner=Runner(log=lambda line: None), work=tmp_path),
    )
    with pytest.raises(PreflightFailed, match="boot a live or memory environment"):
        report.raise_if_fatal()


def test_dd_accepts_a_live_medium_or_memory_environment(tmp_path: Path) -> None:
    class Live(ImageWriteProbe):
        def live_medium(self) -> str:
            return "the root filesystem is overlay"

    class Memory(ImageWriteProbe):
        def live_medium(self) -> str:
            return ""

        def memory_environment(self) -> bool:
            return True

    machine = _machine(frozenset({"cat", "dd", "lsblk", "swapon"}))
    for probe in (
        Live(runner=Runner(log=lambda line: None), work=tmp_path),
        Memory(runner=Runner(log=lambda line: None), work=tmp_path),
    ):
        report = preflight.inspect(dd_config(), machine, probe)
        assert not report.fatal

def test_dd_refuses_an_absent_source_or_in_use_destination(tmp_path: Path) -> None:
    class AbsentSource(ImageWriteProbe):
        def image_source_exists(self, source: str) -> bool:
            return False

    class Partition(ImageWriteProbe):
        def whole_disk(self, selector: str) -> bool:
            return False

    class Mounted(ImageWriteProbe):
        def mounted(self, disk: str, ignoring: str = "") -> bool:
            return True

    machine = _machine(frozenset({"cat", "dd", "lsblk", "swapon"}))
    for broken, expected in (
        (AbsentSource, "is not a regular file"),
        (Partition, "is not a whole disk"),
        (Mounted, "is mounted or holds active swap"),
    ):
        report = preflight.inspect(
            dd_config(),
            machine,
            broken(runner=Runner(log=lambda line: None), work=tmp_path),
        )
        assert any(expected in problem for problem in report.fatal)




def test_a_plan_missing_a_declared_host_command_is_refused(tmp_path: Path) -> None:
    operation = NeedsMediumCommand()
    report = preflight.inspect(
        config(), _machine(frozenset()), _probe(tmp_path), operations=(operation,)
    )
    with pytest.raises(PreflightFailed) as refused:
        report.raise_if_fatal()
    assert "medium-tool" in str(refused.value)
    assert "NeedsMediumCommand" in str(refused.value)


def test_gnu_tar_is_still_a_capability_requirement(tmp_path: Path) -> None:
    operation = NeedsTar()
    report = preflight.inspect(
        config(),
        _machine(
            frozenset({"tar"}),
            versions={"tar": "BusyBox v1.36.1 multi-call binary."},
        ),
        _probe(tmp_path),
        operations=(operation,),
    )
    assert any("tar is not GNU tar" in problem for problem in report.fatal)


def test_a_new_operation_command_is_probed_without_editing_preflight(tmp_path: Path) -> None:
    asked: list[frozenset[str]] = []

    class Watching(Probe):
        def machine(
            self, wanted: frozenset[str] = frozenset(), judged: Iterable[str] = ()
        ) -> Machine:
            asked.append(wanted)
            return _machine(wanted)

    operation = NeedsMediumCommand()
    preflight.check(
        config(),
        Watching(runner=Runner(log=lambda line: None), work=tmp_path),
        operations=(operation,),
    )
    assert asked and "medium-tool" in asked[0]


def test_an_install_from_a_machine_that_is_not_a_medium_says_so(tmp_path: Path) -> None:
    """Nothing named the difference before the disk screen: the installer
    assumed it was running from a medium. Said rather than refused, because
    installing from a running system onto a second disk is a real thing to do
    and the guard that matters is the one on a mounted disk."""

    class Installed(Probe):
        def live_medium(self) -> str:
            return ""

        def root_source(self) -> str:
            return "zfs rpool/ROOT/gentoo"

    report = preflight.check(
        config(),
        Installed(runner=Runner(log=lambda line: None), work=tmp_path),
        operations=(),
    )

    assert any("does not look like a live medium" in one for one in report.warnings)
    assert any("rpool/ROOT/gentoo" in one for one in report.warnings)
    assert not any("live medium" in one for one in report.fatal), "said, not refused"


def test_an_install_from_a_medium_says_nothing_about_it(tmp_path: Path) -> None:
    class OnAMedium(Probe):
        def live_medium(self) -> str:
            return "the kernel command line carries root=live:"

    report = preflight.check(
        config(),
        OnAMedium(runner=Runner(log=lambda line: None), work=tmp_path),
        operations=(),
    )

    assert not any("live medium" in one for one in report.warnings)


def test_a_conversion_from_a_live_medium_is_refused(tmp_path: Path) -> None:
    """The conversion replaces the running userland. On a live medium that
    userland is a squashfs in RAM, so the swap would rename directories the
    machine loses at the next boot and touch nothing that survives."""
    from dataclasses import replace

    from gentoo_install.model.config import DiskConfig, DiskMode
    from gentoo_install.model.device import DeviceGraph, DeviceId

    class OnAMedium(Probe):
        def live_medium(self) -> str:
            return "the kernel command line carries root=live:"

    converted = replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId(""), mode=DiskMode.IN_PLACE),
    )
    report = preflight.check(
        converted,
        OnAMedium(runner=Runner(log=lambda line: None), work=tmp_path),
        operations=(),
    )

    assert any("in-place conversion" in one for one in report.fatal)
    assert any("root=live:" in one for one in report.fatal)


def test_a_conversion_from_a_running_system_is_allowed(tmp_path: Path) -> None:
    from dataclasses import replace

    from gentoo_install.model.config import DiskConfig, DiskMode
    from gentoo_install.model.device import DeviceGraph, DeviceId

    class Installed(Probe):
        def live_medium(self) -> str:
            return ""

        def root_source(self) -> str:
            return "/dev/vda2"

    converted = replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId(""), mode=DiskMode.IN_PLACE),
    )
    report = preflight.check(
        converted,
        Installed(runner=Runner(log=lambda line: None), work=tmp_path),
        operations=(),
    )

    assert not any("in-place conversion" in one for one in report.fatal)


def test_a_reused_whole_device_root_is_measured_too(tmp_path: Path) -> None:
    """The capacity loop starts at every `PartitionTable`, so a root that is a
    whole reused device — no table above it — supplied nothing and the size
    check had nothing to check. An install into 8 GiB runs out during
    linux-firmware, an hour after the disks were written."""
    from gentoo_install.model.config import DiskConfig
    from gentoo_install.model.device import (
        Existing,
        Filesystem,
        FilesystemType,
        Mountpoint,
        Node,
    )
    from pathlib import PurePosixPath

    nodes: list[Node] = [
        Existing(id=i("root-device"), selector="/dev/sdb", wipe=False),
        Filesystem(
            id=i("rootfs"),
            device=i("root-device"),
            kind=FilesystemType.EXT4,
            create=False,
        ),
        Mountpoint(id=i("mnt-root"), source=i("rootfs"), path=PurePosixPath("/")),
    ]
    reused = replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build(nodes), root=i("mnt-root")),
    )

    class EightGiB(Probe):
        def resolve(self, device: object, selector: str) -> str:
            return selector

        def disk_bytes(self, path: str) -> int:
            return 8 * 2**30

    report = preflight.check(
        reused, EightGiB(runner=Runner(log=lambda line: None), work=tmp_path), operations=()
    )

    assert any("carries / and is" in one for one in report.fatal), report.fatal
