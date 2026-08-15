# SPDX-License-Identifier: GPL-2.0-or-later
"""The irreversible userland swap in an in-place conversion."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, Sequence, cast

from .operations import Context, Operation, Stage
from .portage import InstallStage3


REPLACED_DIRECTORIES: tuple[str, ...] = (
    "bin",
    "sbin",
    "etc",
    "lib",
    "lib64",
    "usr",
    "var",
)


class _Converter(Protocol):
    def convert(self, staging: Path, names: Sequence[str], *, root: Path = Path("/")) -> None: ...


@dataclass(frozen=True, kw_only=True)
class InstallStage3InStaging(Operation):
    """Reuse stage3 fetching while unpacking into the conversion staging root."""

    stage: Stage = Stage.STAGE3
    source: InstallStage3
    staging: PurePosixPath = PurePosixPath("/gentoo-install.new")

    def describe(self) -> str:
        return self.source.describe().replace("into the target", f"into {self.staging}")

    def apply(self, context: Context) -> None:
        self.source.apply(cast(Context, _StagingContext(parent=context, staging=self.staging)))


@dataclass(frozen=True)
class _StagingContext:
    parent: Context
    staging: PurePosixPath

    @property
    def target(self) -> PurePosixPath:
        return self.staging

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> str:
        return self.parent.run(argv, check=check, input_text=input_text)

    def write(self, path: PurePosixPath, content: str, *, mode: int = 0o644) -> None:
        self.parent.write(path, content, mode=mode)

    def fetch_stage3(
        self,
        mirror: str,
        variant: str,
        fingerprint: str,
        fallbacks: Sequence[str] = (),
    ) -> PurePosixPath:
        return self.parent.fetch_stage3(mirror, variant, fingerprint, fallbacks)


@dataclass(frozen=True, kw_only=True)
class SwapDirectories(Operation):
    """Atomically replace the selected live-system directories."""

    stage: Stage = Stage.BOOTLOADER
    names: tuple[str, ...] = REPLACED_DIRECTORIES
    staging: PurePosixPath = PurePosixPath("/gentoo-install.new")

    def describe(self) -> str:
        return f"atomically swap {', '.join('/' + name for name in self.names)} from {self.staging}"

    def apply(self, context: Context) -> None:
        module = importlib.import_module("gentoo_install.exec.convert")
        converter = cast(_Converter, module)
        converter.convert(Path(str(self.staging)), self.names)
