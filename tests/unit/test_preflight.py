from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Iterable

import pytest

from gentoo_install.errors import PreflightFailed
from gentoo_install.exec import preflight
from gentoo_install.exec.probe import Machine, Probe
from gentoo_install.exec.runner import Runner
from gentoo_install.plan.operations import Context, Operation, Stage

from .layouts import config


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
