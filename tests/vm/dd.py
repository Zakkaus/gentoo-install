# SPDX-License-Identifier: GPL-2.0-or-later
"""Write generated raw and gzip images from a live medium, then compare bytes.

    python3 -m tests.vm.dd
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .console import ConsoleClosed, ConsoleTimeout, SerialConsole
from .driver import REPOSITORY, build as build_driver, wait_for_driver
from .media import MEDIA
from .qemu import Firmware, Vm, VmSpec
from .run import power_off, reach_shell, run_installer
from .workdir import confined

WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/dd"
SOURCE_BYTES: Final[int] = 4 * 1024 * 1024
SOURCE_BLOCK_BYTES: Final[int] = 64 * 1024
RAW_SOURCE_NAME: Final[str] = "dd-source.raw"
GZIP_SOURCE_NAME: Final[str] = "dd-source.raw.gz"
INSTALL_RESULT: Final[str] = "/run/vm-result/install.rc"


@dataclass(frozen=True)
class SourceImages:
    """The generated source bytes and their compressed representation."""

    raw: Path
    gzip: Path
    marker: bytes


@dataclass(frozen=True)
class Input:
    """One reader format and the fixture that selects it."""

    name: str
    fixture: str


INPUTS: Final[tuple[Input, ...]] = (
    Input(name="raw", fixture="fixtures/vm-dd-raw.toml"),
    Input(name="gz", fixture="fixtures/vm-dd-gz.toml"),
)


def _source_block(marker: bytes, index: int) -> bytes:
    """One non-zero block derived from this run's marker."""
    digest = hashlib.sha256(marker + index.to_bytes(4, "big")).digest()
    repeats, remainder = divmod(SOURCE_BLOCK_BYTES, len(digest))
    return digest * repeats + digest[:remainder]


def build_sources(workdir: Path) -> SourceImages:
    """Build a small image whose unique marker makes a stale target impossible."""
    raw = workdir / RAW_SOURCE_NAME
    compressed = workdir / GZIP_SOURCE_NAME
    marker = f"GENTOO-INSTALL-DD-{uuid.uuid4().hex}\n".encode()
    with raw.open("wb") as output:
        for index in range(SOURCE_BYTES // SOURCE_BLOCK_BYTES):
            block = _source_block(marker, index)
            if index == 0:
                block = marker + block[len(marker) :]
            output.write(block)
    with raw.open("rb") as source, gzip.open(compressed, "wb") as output:
        shutil.copyfileobj(source, output)
    return SourceImages(raw=raw, gzip=compressed, marker=marker)


def stage_sources(workdir: Path, sources: SourceImages) -> Path:
    """Add the generated images beside the fixtures the driver CD receives."""
    fixtures = workdir / "fixtures"
    shutil.copytree(REPOSITORY / "tests" / "fixtures", fixtures)
    shutil.copy2(sources.raw, fixtures / RAW_SOURCE_NAME)
    shutil.copy2(sources.gzip, fixtures / GZIP_SOURCE_NAME)
    return fixtures


def create_target(path: Path) -> Path:
    """Create a target whose virtual size exactly equals the source image."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    result = subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(path), str(SOURCE_BYTES)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"qemu-img could not create {path}: {result.stderr.strip()}")
    return path


def target_matches(source: Path, target: Path, name: str) -> None:
    """Read a qcow2 target through QEMU and reject any byte mismatch."""
    result = subprocess.run(
        ["qemu-img", "compare", "-f", "raw", "-F", "qcow2", str(source), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        raise RuntimeError(f"{name} target differs from its source: {detail or 'qemu-img compare failed'}")


def require_install_success(console: SerialConsole, name: str) -> None:
    """Read the installer status between console markers, never from its echo."""
    status = console.expect_output(f"cat {INSTALL_RESULT}", timeout=60.0).strip()
    if status != b"0":
        raise RuntimeError(f"{name} installer exited {status!r}")


def run_input(
    selected: Input, sources: SourceImages, driver: Path, workdir: Path, *, keep: bool
) -> None:
    """Run one reader against a fresh target and compare it after power-off."""
    target = create_target(workdir / "target.qcow2")
    matched = False
    try:
        medium = MEDIA["official-minimal"]
        spec = VmSpec(
            medium=medium,
            workdir=workdir,
            firmware=Firmware.UEFI,
            driver_iso=driver,
            targets=(target,),
        )
        started = time.monotonic()
        with Vm(spec) as vm:
            with SerialConsole.connect(vm.serial_socket, vm.serial_log) as console:
                reach_shell(console, medium, ())
                wait_for_driver(console)
                run_installer(console, selected.fixture)
                require_install_success(console, selected.name)
                power_off(console, vm)
        target_matches(sources.raw, target, selected.name)
        matched = True
        print(
            f"[{time.monotonic() - started:5.1f}s] {selected.name} target byte-for-byte "
            "matches its source",
            flush=True,
        )
    finally:
        if matched and not keep:
            target.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Run both streamed source formats on the official minimal live medium."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep verified target disks")
    arguments = parser.parse_args(argv)

    workdir = confined(WORKROOT / str(time.time_ns()))
    workdir.mkdir(parents=True)
    print(f"work directory: {workdir}", flush=True)
    try:
        sources = build_sources(workdir)
        fixtures = stage_sources(workdir, sources)
        driver = build_driver(workdir / "driver.iso", fixtures=fixtures)
        for selected in INPUTS:
            run_input(selected, sources, driver, workdir / selected.name, keep=arguments.keep)
    except (ConsoleClosed, ConsoleTimeout, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"FAIL {error}", file=sys.stderr, flush=True)
        return 1
    print("dd mode wrote and read back raw and gz sources byte-for-byte", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
