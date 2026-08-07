"""Boot an install medium in QEMU and probe it, drive the installer, or hand it
to a human.

    python3 -m tests.vm.run --medium official-minimal
    python3 -m tests.vm.run --medium official-minimal --install tests/fixtures/ext4-bios.toml
    python3 -m tests.vm.run --medium gigos --interactive
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from .console import SerialConsole
from .driver import build as build_driver
from .media import MEDIA, Medium
from .qemu import Firmware, Vm, VmSpec
from .results import collect_command, create_disk, read_disk

WORKROOT = Path.home() / "code/gentoo-install/lab/vm/runs"
#: Big enough for a stage3, a desktop and the swap a fixture may ask for.
TARGET_SIZE = "40G"
RESULT_DIR = "/run/vm-result"

PROBE = (
    ("os-release", "head -3 /etc/os-release; uname -r"),
    ("python", "python3 -V"),
    ("storage", "command -v zfs cryptsetup lvm mdadm mkfs.btrfs mkfs.ext4 sgdisk parted"),
    ("portage", "command -v emerge gcc; ls /var/db/repos 2>/dev/null"),
    ("block", "lsblk -no NAME,SIZE,TYPE"),
)


def create_target(path: Path) -> Path:
    """A blank disk for the installer to partition, thrown away with the run."""
    path.unlink(missing_ok=True)
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(path), TARGET_SIZE],
        check=True,
        capture_output=True,
    )
    return path


def ssh_keypair(workdir: Path) -> Path:
    key = workdir / "id_ed25519"
    if not key.is_file():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key), "-C", "vm-test"],
            check=True,
        )
    return key


def install_key(console: SerialConsole, public_key: str) -> None:
    console.run("mkdir -p /root/.ssh && chmod 700 /root/.ssh")
    console.run(f"printf '%s\\n' '{public_key}' > /root/.ssh/authorized_keys")
    console.run("chmod 600 /root/.ssh/authorized_keys")


def ssh(key: Path, port: int, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ssh", "-p", str(port), "-i", str(key),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "root@127.0.0.1", command,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def reach_shell(console: SerialConsole, medium: Medium) -> None:
    if medium.login_user is None:
        console.expect(medium.root_prompt, timeout=300.0)
    else:
        console.login(medium.login_user, medium.login_password, medium.root_prompt)


def run_installer(console: SerialConsole, config: str, extra: str = "") -> None:
    """Run the installer from the driver CD and keep everything it printed."""
    console.run("mkdir -p /mnt/driver")
    console.run("mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver")
    console.run(f"mkdir -p {RESULT_DIR}")
    console.run(
        f"cd /mnt/driver && python3 -m gentoo_install --config {config} {extra} "
        f"> {RESULT_DIR}/install.txt 2>&1; echo $? > {RESULT_DIR}/install.rc",
        timeout=1800.0,
    )
    console.run(f"cp /mnt/driver/{config} {RESULT_DIR}/config.toml 2>/dev/null || true")
    console.run(collect_command(RESULT_DIR))
    console.run("sync")


def probe(console: SerialConsole) -> None:
    console.run(f"mkdir -p {RESULT_DIR}")
    for name, command in PROBE:
        console.run(f"{{ {command} ; }} > {RESULT_DIR}/{name}.txt 2>&1")
    console.run(collect_command(RESULT_DIR))
    console.run("sync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--medium", choices=sorted(MEDIA), default="official-minimal")
    parser.add_argument("--firmware", choices=[f.value for f in Firmware], default="uefi")
    parser.add_argument("--ssh-port", type=int, default=2222)
    parser.add_argument(
        "--install",
        help="run the installer from the driver CD against this configuration, "
        "as a path inside the CD such as fixtures/ext4-bios.toml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --install, print the operations instead of performing them",
    )
    parser.add_argument("--interactive", action="store_true", help="hand the VM to a human over SSH")
    parser.add_argument("--keep", action="store_true", help="keep the run directory")
    args = parser.parse_args(argv)

    medium = MEDIA[args.medium]
    workdir = WORKROOT / f"{medium.name}-{args.firmware}"
    workdir.mkdir(parents=True, exist_ok=True)
    key = ssh_keypair(workdir)
    result_disk = create_disk(workdir / "result.img")
    driver_iso = build_driver(workdir / "driver.iso") if args.install else None
    targets = (create_target(workdir / "target.qcow2"),) if args.install else ()

    spec = VmSpec(
        medium=medium,
        workdir=workdir,
        firmware=Firmware(args.firmware),
        ssh_port=args.ssh_port,
        disks=(result_disk,),
        driver_iso=driver_iso,
        targets=targets,
    )

    started = time.monotonic()
    with Vm(spec) as vm:
        with SerialConsole.connect(vm.serial_socket, vm.serial_log) as console:
            reach_shell(console, medium)
            print(f"[{time.monotonic() - started:5.1f}s] root shell on serial")

            install_key(console, key.with_suffix(".pub").read_text().strip())
            console.run("command -v sshd >/dev/null && (rc-service sshd start || systemctl start sshd) || true")
            probed = ssh(key, args.ssh_port, "true")
            print(f"[{time.monotonic() - started:5.1f}s] ssh reachable: {probed.returncode == 0}")

            if args.interactive:
                print(f"ssh -p {args.ssh_port} -i {key} root@127.0.0.1")
                print("the VM stays up until this process is interrupted")
                try:
                    vm.wait(timeout=86400.0)
                except KeyboardInterrupt:
                    return 0
                return 0

            if args.install:
                run_installer(console, args.install, "--dry-run" if args.dry_run else "")
            else:
                probe(console)
            console.send("poweroff")
            try:
                vm.wait(timeout=120.0)
            except subprocess.TimeoutExpired:
                print("guest did not power off, killing it", file=sys.stderr)

    results = read_disk(result_disk)
    for name in sorted(results):
        print(f"--- {name} ---")
        print(results[name].decode("utf-8", "replace").rstrip())
    if not args.keep:
        result_disk.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
