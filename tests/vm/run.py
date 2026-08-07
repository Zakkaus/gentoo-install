"""Boot an install medium in QEMU and probe it, drive the installer, or hand it
to a human.

    python3 -m tests.vm.run --medium official-minimal
    python3 -m tests.vm.run --medium official-minimal --install tests/fixtures/ext4-bios.toml
    python3 -m tests.vm.run --medium gigos --interactive
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

from gentoo_install.model import compat
from gentoo_install.model.config import InitSystem, InstallConfig
from gentoo_install.model.device import (
    Filesystem,
    Luks,
    Mountpoint,
    Subvolume,
    ZfsDataset,
    ZfsPool,
)
from gentoo_install.model.parse import load

from .console import SerialConsole
from .driver import REPOSITORY, build as build_driver
from .media import MEDIA, Medium
from .qemu import Firmware, Vm, VmSpec
from .results import collect_command, create_disk, read_disk

WORKROOT = Path.home() / "code/gentoo-install/lab/vm/runs"
#: Big enough for a stage3, a desktop and the swap a fixture may ask for.
TARGET_SIZE = "40G"

#: The password in `fixtures/vm-binpkg.toml`, as plain text. It exists so the
#: harness can log into what it installed; nothing else uses it.
INSTALLED_PASSWORD = "install"
RESULT_DIR = "/run/vm-result"

#: What the installed system has to show, as (result file, pattern). A check
#: that only collected output would pass on a system that booted into an
#: emergency shell, so each of these decides the exit code.
EXPECTED = (("kernel", r"^(kernel|vmlinuz)-"),)

#: What a system installed by this installer has to be able to answer.
INSTALLED = (
    ("os-release", "cat /etc/os-release"),
    ("mounts", "findmnt --noheadings --list --output TARGET,SOURCE,FSTYPE"),
    ("fstab", "cat /etc/fstab"),
    ("locale", "locale"),
    ("hostname", "cat /etc/hostname || cat /etc/conf.d/hostname"),
    ("kernel", "uname -r; ls /boot"),
)

#: Asking systemd's questions of an openrc system gets "command not found",
#: which reads as a failure of the install rather than of the check.
BY_INIT: dict[InitSystem, tuple[tuple[str, str], ...]] = {
    InitSystem.SYSTEMD: (
        ("units", "systemctl list-unit-files --state=enabled --no-legend --no-pager"),
        ("failed", "systemctl --failed --no-legend --no-pager"),
    ),
    InitSystem.OPENRC: (
        ("units", "rc-update show default"),
        ("failed", "rc-status --crashed"),
    ),
}

PROBE = (
    ("os-release", "head -3 /etc/os-release; uname -r"),
    ("python", "python3 -V"),
    ("storage", "command -v zfs cryptsetup lvm mdadm mkfs.btrfs mkfs.ext4 sgdisk parted"),
    ("portage", "command -v emerge gcc; ls /var/db/repos 2>/dev/null"),
    ("block", "lsblk -no NAME,SIZE,TYPE"),
)


class RunInProgress(Exception):
    """Another run holds this directory. Its result disk and its serial socket
    would both be taken over, and the first run would fail in a way that looks
    like a failed install."""


def claim(workdir: Path) -> int:
    lock = workdir / "run.lock"
    handle = os.open(lock, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        raise RunInProgress(f"another run holds {lock}") from None
    return handle


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


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


def check_installed(console: SerialConsole, installation: InstallConfig) -> None:
    """Assert against the system that was installed, booted from its own disk."""
    console.run(f"mkdir -p {RESULT_DIR}")
    for name, command in (*INSTALLED, *BY_INIT[installation.system.init]):
        console.run(f"{{ {command} ; }} > {RESULT_DIR}/{name}.txt 2>&1")
    console.run(collect_command(RESULT_DIR))
    console.run("sync")


def stage_passphrases(console: SerialConsole, installation: InstallConfig) -> None:
    """Put the passphrases where the layout says they are.

    An operator does this by hand before an unattended install; the harness
    does it here so an encrypted layout can be tested without a prompt.
    """
    graph = installation.disk.graph
    wanted = [node.passphrase_file for node in graph.of_type(Luks) if node.passphrase_file]
    wanted += [node.passphrase_file for node in graph.of_type(ZfsPool) if node.passphrase_file]
    for source in wanted:
        parent = PurePosixPath(source).parent
        console.run(f"mkdir -p {parent} && chmod 700 {parent}")
        console.run(f"printf '%s' '{INSTALLED_PASSWORD}' > {source}")
        console.run(f"chmod 600 {source}")


def run_installer(console: SerialConsole, config: str, extra: str = "") -> None:
    """Run the installer from the driver CD and keep everything it printed."""
    console.run("mkdir -p /mnt/driver")
    console.run("mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver")
    console.run(f"mkdir -p {RESULT_DIR}")
    # tee, not a redirect: the serial console is the only way to watch a run
    # that takes half an hour, and a redirect makes it silent until it ends.
    console.run(
        f"cd /mnt/driver && python3 -u -m gentoo_install --config {config} {extra} 2>&1 "
        f"| tee {RESULT_DIR}/install.txt; echo ${{PIPESTATUS[0]}} > {RESULT_DIR}/install.rc",
        timeout=3600.0,
    )
    console.run(f"cp /mnt/driver/{config} {RESULT_DIR}/config.toml 2>/dev/null || true")
    # The journal says which packages came from a binary host and which were
    # compiled, which is the question this configuration exists to answer.
    console.run(f"cp /run/gentoo-install/install.jsonl {RESULT_DIR}/ 2>/dev/null || true")
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
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=0,
        help="host port forwarded to the guest's sshd; 0 picks a free one, which is what "
        "keeps two runs from colliding",
    )
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
    parser.add_argument(
        "--boot-installed",
        action="store_true",
        help="boot the disk a previous --install run produced and check the system on it; "
        "takes the same --install argument, which is what names the run",
    )
    parser.add_argument("--interactive", action="store_true", help="hand the VM to a human over SSH")
    parser.add_argument("--keep", action="store_true", help="keep the run directory")
    args = parser.parse_args(argv)

    medium = MEDIA[args.medium]
    # The configuration is part of the name: two runs sharing a directory would
    # share a serial socket, and the second one never connects.
    if args.boot_installed and not args.install:
        print("--boot-installed needs the same --install argument as the run it checks", file=sys.stderr)
        return 1
    variant = Path(args.install).stem if args.install else "probe"
    workdir = WORKROOT / f"{medium.name}-{args.firmware}-{variant}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        claim(workdir)
    except RunInProgress as error:
        print(error, file=sys.stderr)
        return 1
    ssh_port = args.ssh_port or free_port()
    key = ssh_keypair(workdir)
    result_disk = create_disk(workdir / "result.img")
    driver_iso = build_driver(workdir / "driver.iso") if args.install and not args.boot_installed else None
    targets: tuple[Path, ...] = ()
    if args.boot_installed:
        installed = workdir / "target.qcow2"
        if not installed.is_file():
            print(f"{installed} does not exist; run --install first", file=sys.stderr)
            return 1
        targets = (installed,)
    elif args.install:
        targets = (create_target(workdir / "target.qcow2"),)

    spec = VmSpec(
        medium=medium,
        workdir=workdir,
        firmware=Firmware(args.firmware),
        ssh_port=ssh_port,
        disks=(result_disk,),
        driver_iso=driver_iso,
        targets=targets,
        boot_installed=args.boot_installed,
    )

    started = time.monotonic()
    with Vm(spec) as vm:
        with SerialConsole.connect(vm.serial_socket, vm.serial_log) as console:
            if args.boot_installed:
                console.login("root", INSTALLED_PASSWORD, r"# ")
                print(f"[{time.monotonic() - started:5.1f}s] logged into the installed system")
                check_installed(console, load(REPOSITORY / "tests" / args.install))
                power_off(console, vm)
                return report(
                    result_disk, keep=args.keep, assertions=REPOSITORY / "tests" / args.install
                )
            reach_shell(console, medium)
            print(f"[{time.monotonic() - started:5.1f}s] root shell on serial")

            install_key(console, key.with_suffix(".pub").read_text().strip())
            console.run("command -v sshd >/dev/null && (rc-service sshd start || systemctl start sshd) || true")
            probed = ssh(key, ssh_port, "true")
            print(f"[{time.monotonic() - started:5.1f}s] ssh reachable: {probed.returncode == 0}")

            if args.interactive:
                print(f"ssh -p {ssh_port} -i {key} root@127.0.0.1")
                print("the VM stays up until this process is interrupted")
                try:
                    vm.wait(timeout=86400.0)
                except KeyboardInterrupt:
                    return 0
                return 0

            if args.install:
                stage_passphrases(console, load(REPOSITORY / "tests" / args.install))
                run_installer(console, args.install, "--dry-run" if args.dry_run else "")
            else:
                probe(console)
            power_off(console, vm)

    return report(result_disk, keep=args.keep)


def power_off(console: SerialConsole, vm: Vm) -> None:
    """Shut the guest down, reading its console until the process is gone.

    Draining has to continue past the last message worth matching: the guest
    stops writing when its console buffer fills, and a shutdown that cannot
    write does not finish.
    """
    console.send("poweroff")
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        console.drain(2.0)
        try:
            vm.wait(timeout=0.1)
            return
        except subprocess.TimeoutExpired:
            continue
    print("guest did not power off, killing it", file=sys.stderr)


def report(
    result_disk: Path, *, keep: bool, assertions: Path | None = None
) -> int:
    results = read_disk(result_disk)
    for name in sorted(results):
        print(f"--- {name} ---")
        print(results[name].decode("utf-8", "replace").rstrip())
    code = check_expected(results, assertions) if assertions is not None else 0
    if not keep:
        result_disk.unlink(missing_ok=True)
    return code


def _from_config(config: Path) -> list[tuple[str, str]]:
    """What this particular configuration should have produced.

    Derived from the model rather than written out twice: a check that only
    ever matched the first fixture passed on a system that ignored the setting
    and failed on the second one.
    """
    installation = load(config)
    graph = installation.disk.graph
    root = graph[installation.disk.root]
    source = graph[root.source] if isinstance(root, Mountpoint) else root
    # systemd keeps the bare name in /etc/hostname; openrc keeps a shell
    # assignment in /etc/conf.d/hostname, and neither form matches the other.
    name = re.escape(installation.system.hostname)
    if installation.system.init is InitSystem.SYSTEMD:
        expected = [("hostname", f"^{name}$")]
    else:
        expected = [("hostname", f'hostname="{name}"')]
    network = "systemd-networkd" if installation.system.init is InitSystem.SYSTEMD else "dhcpcd"
    expected.append(("units", re.escape(network)))
    esp = compat.esp_mount(graph)
    if esp is not None:
        # A BIOS install has no esp at all, so asking for one would fail on a
        # machine that did exactly what its layout said.
        expected.append(("mounts", rf"^{esp.path}\s+\S+\s+vfat"))
    if isinstance(source, ZfsDataset):
        # A dataset carries its own mountpoint, so fstab has no entry for `/`.
        expected.append(("mounts", r"^/\s+\S+\s+zfs"))
        return expected
    filesystem = graph[source.filesystem] if isinstance(source, Subvolume) else source
    kind = filesystem.kind.value if isinstance(filesystem, Filesystem) else ""
    expected += [("mounts", rf"^/\s+\S+\s+{kind}"), ("fstab", rf"UUID=\S+\s+/\s+{kind}")]
    return expected


def check_expected(results: dict[str, bytes], config: Path) -> int:
    """Turn the collected output into a verdict."""
    missing: list[str] = []
    # From the configuration rather than hardcoded: a second fixture installs a
    # different name, and a check that only ever matched the first one would
    # pass on a system that ignored the setting.
    for name, pattern in [*EXPECTED, *_from_config(config)]:
        text = results.get(f"{name}.txt", b"").decode("utf-8", "replace")
        if re.search(pattern, text, re.MULTILINE) is None:
            missing.append(f"{name}.txt does not match {pattern}")
    failed = results.get("failed.txt", b"").decode("utf-8", "replace").strip()
    if failed:
        missing.append(f"systemd reports failed units: {failed.splitlines()[0]}")
    for problem in missing:
        print(f"FAIL {problem}", file=sys.stderr)
    if missing:
        return 1
    print("the installed system booted, mounted its layout and has no failed unit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
