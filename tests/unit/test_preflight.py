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


def test_a_bios_machine_is_told_why_a_uefi_configuration_refuses(tmp_path: Path) -> None:
    """`[bootloader] firmware` defaults to UEFI in the parser, while the menu
    takes it from the machine. A configuration file that leaves the key out,
    run on a BIOS machine, therefore installs for UEFI — and the refusal told
    the operator to mount efivarfs, which a BIOS machine does not have."""
    from gentoo_install.exec.probe import Machine as ProbedMachine

    class Bios(Probe):
        def machine(
            self, wanted: frozenset[str] = frozenset(), judged: Iterable[str] = ()
        ) -> ProbedMachine:
            return replace(
                super().machine(wanted, judged), uefi=False, efi_variables=False
            )

    report = preflight.check(
        config(), Bios(runner=Runner(log=lambda line: None), work=tmp_path), operations=()
    )

    assert any("booted by BIOS" in one for one in report.fatal), report.fatal
    assert not any("efivarfs" in one for one in report.fatal), report.fatal


def test_a_conversion_names_the_home_directories_it_will_orphan(tmp_path: Path) -> None:
    """`/home` is kept and `/etc` is replaced, so the files stay and the
    accounts that own them do not: the machine boots and is wrong in a way
    nothing in the run reports."""
    from gentoo_install.exec import preflight as checking

    class WithHomes(Probe):
        def home_accounts(self) -> tuple[tuple[str, int, str], ...]:
            return (("/home/zakk", 1000, "zakk"), ("/home/olduser", 1001, "olduser"))

        def live_medium(self) -> str:
            return ""

    from gentoo_install.model.config import User

    started = config()
    recreated = replace(
        started,
        system=replace(started.system, users=(User(name="zakk", password_hash="x"),)),
    )
    said = checking._orphaned_home_directories(
        recreated, WithHomes(runner=Runner(log=lambda line: None), work=tmp_path)
    )
    assert len(said) == 2, said
    # Both branches, because a recreated account is the case that reads as
    # fine and is not: `useradd` picks its own uid.
    assert any("/home/zakk" in one and "same uid" in one for one in said), said
    assert any("/home/olduser" in one and "does not create" in one for one in said), said


def test_a_system_account_owning_a_home_directory_is_not_reported(tmp_path: Path) -> None:
    """A uid below the first one `useradd` hands out is a service account, and
    naming it would bury the two lines that matter."""
    from gentoo_install.exec import preflight as checking

    class WithService(Probe):
        def home_accounts(self) -> tuple[tuple[str, int, str], ...]:
            return (("/home/postgres", 70, "postgres"),)

    started = config()
    assert (
        checking._orphaned_home_directories(
            started, WithService(runner=Runner(log=lambda line: None), work=tmp_path)
        )
        == []
    )


class _WithNetwork(Probe):
    """A machine holding one address on the interface its default route uses."""

    routes: ClassVar[tuple[str, ...]] = (
        "default via 192.0.2.1 dev eth0 proto static src 192.0.2.10",
    )
    addresses: ClassVar[tuple[tuple[str, str, bool], ...]] = (
        ("eth0", "192.0.2.10/24", False),
        ("virbr0", "192.168.122.1/24", False),
    )

    def default_routes(self) -> tuple[str, ...]:
        return self.routes

    def current_addresses(self) -> tuple[tuple[str, str, bool], ...]:
        return self.addresses


def test_a_conversion_says_what_the_machine_reaches_the_network_with(
    tmp_path: Path,
) -> None:
    """`/etc` holds the interface configuration of the distribution being
    replaced, so a machine whose address was configured there does not come
    back. Nothing in the run said so."""
    from gentoo_install.exec import preflight as checking

    said = checking._replaced_network(
        config(), _WithNetwork(runner=Runner(log=lambda line: None), work=tmp_path)
    )
    assert len(said) == 1, said
    assert "192.0.2.10/24 on eth0" in said[0], said
    assert "asks a DHCP server" in said[0], said
    # Only the interface the default route leaves by: a bridge or a tunnel
    # address is not what the operator is connected through.
    assert "192.168.122.1" not in said[0], said


def test_a_conversion_says_when_the_configured_address_is_a_different_one(
    tmp_path: Path,
) -> None:
    from gentoo_install.exec import preflight as checking

    started = config()
    elsewhere = replace(
        started, system=replace(started.system, addresses=("198.51.100.5/24",))
    )
    said = checking._replaced_network(
        elsewhere, _WithNetwork(runner=Runner(log=lambda line: None), work=tmp_path)
    )
    assert "none of which this machine holds now" in said[0], said


def test_a_machine_with_no_default_route_is_not_reported(tmp_path: Path) -> None:
    """Nothing to compare against, and a line naming no route reads as though
    the machine had lost one."""
    from gentoo_install.exec import preflight as checking

    class NoRoute(_WithNetwork):
        routes: ClassVar[tuple[str, ...]] = ()

    assert (
        checking._replaced_network(
            config(), NoRoute(runner=Runner(log=lambda line: None), work=tmp_path)
        )
        == []
    )
