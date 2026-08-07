"""QEMU process management for the test harness."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Self

from .media import Medium

OVMF_CODE = Path("/usr/share/edk2-ovmf/OVMF_CODE_4M.qcow2")
OVMF_VARS = Path("/usr/share/edk2-ovmf/OVMF_VARS.fd")


class Firmware(Enum):
    UEFI = "uefi"
    BIOS = "bios"


class QemuError(Exception):
    """QEMU could not be started, or the host lacks what it needs."""


@dataclass(frozen=True)
class VmSpec:
    medium: Medium
    workdir: Path
    firmware: Firmware = Firmware.UEFI
    memory: str = "8G"
    cpus: int = 4
    ssh_port: int = 2222
    disks: tuple[Path, ...] = ()


class Vm:
    def __init__(self, spec: VmSpec) -> None:
        self.spec = spec
        self.serial_socket = spec.workdir / "serial.sock"
        self.serial_log = spec.workdir / "serial.log"
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if shutil.which("qemu-system-x86_64") is None:
            raise QemuError("qemu-system-x86_64 is not installed")
        self.spec.workdir.mkdir(parents=True, exist_ok=True)
        self.serial_socket.unlink(missing_ok=True)
        # QEMU's stderr goes to a file, not a pipe: nobody reads the pipe, so a chatty
        # or failing QEMU would fill the buffer and block.
        errors = (self.spec.workdir / "qemu.err").open("wb")
        self._process = subprocess.Popen(self._argv(), stdout=subprocess.DEVNULL, stderr=errors)

    def _argv(self) -> list[str]:
        kernel, initrd = self.spec.medium.boot_files()
        argv = [
            "qemu-system-x86_64",
            "-enable-kvm",
            "-cpu", "host",
            "-machine", "q35",
            "-smp", str(self.spec.cpus),
            "-m", self.spec.memory,
            "-kernel", str(kernel),
            "-initrd", str(initrd),
            "-append", self.spec.medium.cmdline(),
            "-drive", f"file={self.spec.medium.iso},media=cdrom,readonly=on",
            "-netdev", f"user,id=net0,hostfwd=tcp::{self.spec.ssh_port}-:22",
            "-device", "virtio-net-pci,netdev=net0",
            "-display", "none",
            "-monitor", "none",
            "-serial", f"unix:{self.serial_socket},server,nowait",
        ]
        if self.spec.firmware is Firmware.UEFI:
            argv += self._ovmf_args()
        for index, disk in enumerate(self.spec.disks):
            argv += [
                "-drive", f"file={disk},format=raw,if=none,id=disk{index}",
                "-device", f"virtio-blk-pci,drive=disk{index}",
            ]
        return argv

    def _ovmf_args(self) -> list[str]:
        if not OVMF_CODE.is_file() or not OVMF_VARS.is_file():
            raise QemuError(f"OVMF firmware is missing: {OVMF_CODE}, {OVMF_VARS}")
        # NVRAM must be writable and per-run, so boot entries do not leak between tests.
        variables = self.spec.workdir / "OVMF_VARS.fd"
        shutil.copy(OVMF_VARS, variables)
        return [
            "-drive", f"if=pflash,format=qcow2,readonly=on,file={OVMF_CODE}",
            "-drive", f"if=pflash,format=raw,file={variables}",
        ]

    def wait(self, timeout: float) -> int:
        if self._process is None:
            raise QemuError("the VM was never started")
        return self._process.wait(timeout=timeout)

    def kill(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=30)
        self.serial_socket.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.kill()
