"""QEMU process management for the test harness."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from .media import Medium

OVMF_CODE = Path("/usr/share/edk2-ovmf/OVMF_CODE_4M.qcow2")
OVMF_VARS = Path("/usr/share/edk2-ovmf/OVMF_VARS.fd")


class Firmware(Enum):
    UEFI = "uefi"
    BIOS = "bios"


#: What runs the guest behind whatever the person at the keyboard is doing.
#: Empty when `nice` or `ionice` is absent: a missing scheduler tool is not a
#: reason to refuse to test.
_YIELDING: Final[tuple[str, ...]] = tuple(
    part
    for tool, arguments in (("nice", ("-n", "10")), ("ionice", ("-c", "2", "-n", "7")))
    if shutil.which(tool)
    for part in (tool, *arguments)
)


class QemuError(Exception):
    """QEMU could not be started, or the host lacks what it needs."""


@dataclass(frozen=True)
class VmSpec:
    medium: Medium
    workdir: Path
    firmware: Firmware = Firmware.UEFI
    memory: str = "8G"
    #: MAKEOPTS in the guest is derived from this, so it decides how fast the
    #: packages a fixture compiles are built. Five leaves 32 threads covering
    #: six guests at once.
    cpus: int = 5
    ssh_port: int = 2222
    remote_unlock_port: int | None = None
    disks: tuple[Path, ...] = ()
    #: Built from the working tree each run and mounted as the second CD.
    driver_iso: Path | None = None
    #: Target disks the installer may partition.
    targets: tuple[Path, ...] = ()
    #: Boot what is on the target disk instead of the install medium. This is
    #: the only check that an install produced a system that boots.
    boot_installed: bool = False
    #: Which address families the guest is given: `dual`, `ipv4` or `ipv6`.
    #: An installer that only ever ran dual-stack has never been asked what it
    #: does when a mirror's other record is the only one it can use.
    families: str = "dual"


class Vm:
    def __init__(self, spec: VmSpec) -> None:
        self.spec = spec
        self.serial_socket = spec.workdir / "serial.sock"
        self.monitor_socket = spec.workdir / "monitor.sock"
        self.serial_log = spec.workdir / "serial.log"
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if shutil.which("qemu-system-x86_64") is None:
            raise QemuError("qemu-system-x86_64 is not installed")
        self.spec.workdir.mkdir(parents=True, exist_ok=True)
        self.serial_socket.unlink(missing_ok=True)
        self.monitor_socket.unlink(missing_ok=True)
        # QEMU's stderr goes to a file, not a pipe: nobody reads the pipe, so a chatty
        # or failing QEMU would fill the buffer and block.
        errors = (self.spec.workdir / "qemu.err").open("wb")
        self._process = subprocess.Popen(self._argv(), stdout=subprocess.DEVNULL, stderr=errors)

    def _argv(self) -> list[str]:
        # The workstation is somebody's desktop while this runs. `nice` keeps
        # five guests from making the compositor stutter, and the best-effort
        # I/O class at its lowest priority does the same for the disk without
        # the idle class's habit of starving a guest that is extracting a
        # stage3. Neither slows a run that has the machine to itself.
        argv = [
            *_YIELDING,
            "qemu-system-x86_64",
            "-enable-kvm",
            "-cpu", "host",
            "-machine", "q35",
            "-smp", str(self.spec.cpus),
            "-m", self.spec.memory,
            "-netdev", self._netdev(),
            "-device", "virtio-net-pci,netdev=net0",
            # A guest fills its whole allocation with page cache and never
            # hands it back, so four 8 GiB guests held 32 GiB of mostly cache
            # and the fifth was killed. Free page reporting returns what the
            # guest is not using; a kernel without it ignores the device.
            "-device", "virtio-balloon,free-page-reporting=on",
            "-display", "none",
            # A monitor socket, not `none`: GRUB's own passphrase prompt for an
            # encrypted BIOS disk happens before it reads grub.cfg, so it is on
            # the VGA console whatever `GRUB_TERMINAL` says, and `sendkey` is
            # the only way in. Proved by screendump: `Enter passphrase for
            # hd0,msdos2` with an empty serial log.
            "-monitor", f"unix:{self.monitor_socket},server,nowait",
            "-serial", f"unix:{self.serial_socket},server,nowait",
        ]
        if not self.spec.boot_installed:
            kernel, initrd = self.spec.medium.boot_files()
            argv += [
                "-kernel", str(kernel),
                "-initrd", str(initrd),
                "-append", self.spec.medium.cmdline(),
                "-drive", f"file={self.spec.medium.iso},media=cdrom,readonly=on",
            ]
        if self.spec.firmware is Firmware.UEFI:
            argv += self._ovmf_args()
        if self.spec.driver_iso is not None:
            argv += ["-drive", f"file={self.spec.driver_iso},media=cdrom,readonly=on"]
        for index, disk in enumerate(self.spec.disks):
            argv += [
                "-drive", f"file={disk},format=raw,if=none,id=disk{index}",
                "-device", f"virtio-blk-pci,drive=disk{index}",
            ]
        for index, target in enumerate(self.spec.targets):
            # A stable serial gives the configuration a selector that survives
            # the guest renumbering its disks. `bootindex` is what makes the
            # firmware try this disk first: SeaBIOS boots whichever drive comes
            # first otherwise, and that is the harness's own result disk.
            device = f"virtio-blk-pci,drive=target{index},serial=target{index}"
            if self.spec.boot_installed and index == 0:
                device += ",bootindex=0"
            argv += [
                "-drive", f"file={target},format=qcow2,if=none,id=target{index}",
                "-device", device,
            ]
        return argv

    def _netdev(self) -> str:
        """The slirp backend, with only the families this run is testing.

        `hostfwd` is IPv4, so a guest given no IPv4 gets no forward either and
        is reached over the serial console alone. That is what an IPv6-only
        machine looks like, and pretending otherwise would test nothing.
        """
        wants4 = self.spec.families in ("dual", "ipv4")
        wants6 = self.spec.families in ("dual", "ipv6")
        parts = [
            "user",
            "id=net0",
            f"ipv4={'on' if wants4 else 'off'}",
            f"ipv6={'on' if wants6 else 'off'}",
        ]
        if wants6:
            # A unique local prefix, not slirp's default `fec0::/64`. Linux
            # gives a site-local address `scope site`, so `ip address show
            # scope global` reports nothing and a guest that can reach the
            # world looks like one with no network. A ULA behind NAT66 is
            # also what the real IPv6-only machines look like, including the
            # cluster's own guests.
            parts.append("ipv6-net=fd00:5:5::/64")
        if wants4:
            parts.append(f"hostfwd=tcp::{self.spec.ssh_port}-:22")
            if self.spec.remote_unlock_port is not None:
                parts.append(f"hostfwd=tcp::{self.spec.remote_unlock_port}-:2222")
        return ",".join(parts)

    def _ovmf_args(self) -> list[str]:
        if not OVMF_CODE.is_file() or not OVMF_VARS.is_file():
            raise QemuError(f"OVMF firmware is missing: {OVMF_CODE}, {OVMF_VARS}")
        # NVRAM must be writable and per-run, so boot entries do not leak between
        # tests. Booting the installed system keeps the file the install wrote,
        # which is where its boot entry lives.
        variables = self.spec.workdir / "OVMF_VARS.fd"
        if not (self.spec.boot_installed and variables.is_file()):
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
        self.monitor_socket.unlink(missing_ok=True)

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
