# SPDX-License-Identifier: GPL-2.0-or-later
"""Boot an install medium in QEMU and probe it, drive the installer, or hand it
to a human.

    python3 -m tests.vm.run --medium official-minimal
    python3 -m tests.vm.run --medium official-minimal --install tests/fixtures/ext4-bios.toml
    python3 -m tests.vm.run --medium gigos --interactive
"""

from __future__ import annotations

import argparse
import fcntl
import io
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from gentoo_install.model.config import Bootloader, Firmware as BootFirmware, InitSystem, InstallConfig
from gentoo_install.model.device import (
    Existing,
    Filesystem,
    Luks,
    Mountpoint,
    Subvolume,
    ZfsDataset,
    ZfsPool,
)
from gentoo_install.exec.config import load
from gentoo_install.model.serialise import to_toml
from gentoo_install.model.manual import dataset_for

from .console import DISK_PASSPHRASE, PASSPHRASE_PROMPT, PASSWORD_PROMPT, SerialConsole
from .monitor import type_text
from .driver import REPOSITORY, build as build_driver
from .media import MEDIA, Medium
from .qemu import Firmware, Vm, VmSpec
from .results import collect_command, create_disk, read_disk
from .installed import checks, stage_passphrase_commands

WORKROOT = Path.home() / "code/gentoo-install/lab/vm/runs"
#: Big enough for a stage3, a desktop and the swap a fixture may ask for.
TARGET_SIZE = "40G"

#: The password in `fixtures/vm-binpkg.toml`, as plain text. It exists so the
#: harness can log into what it installed; nothing else uses it.
INSTALLED_PASSWORD = "install"

#: How long SeaBIOS and GRUB take to reach the cryptomount prompt. Measured on
#: this machine at about twenty seconds; the extra ten is for a loaded host.
GRUB_PROMPT_SECONDS = 30.0
RESULT_DIR = "/run/vm-result"

#: What the installed system has to show, as (result file, pattern). A check
#: that only collected output would pass on a system that booted into an
#: emergency shell, so each of these decides the exit code.
#: Where a kernel image lives depends on the layout the bootloader wants, so
#: the pattern comes from the configuration in `_from_config`.
EXPECTED: tuple[tuple[str, str], ...] = (
    # A run once left /etc/resolv.conf pointing at a socket that only exists
    # once the system is up, which took DNS away from every emerge after it.
    ("resolver", r"^RESOLVCONF-OK$"),
    ("portage", r"^EMERGE-OK$"),
)

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


#: qemu's user-mode network puts the workstation here, so a fixture that
#: names a proxy on the host names this address.
GATEWAY: Final[str] = "10.0.2.2"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def require_proxy(installation: InstallConfig) -> None:
    """Refuse a run whose proxy is not listening, before anything is built.

    A fixture reaches the workstation at qemu's user-mode gateway, which is
    `127.0.0.1` from here. Without this the install fails at the stage3 fetch
    and reads exactly like a defect in the proxy support it was meant to prove.
    """
    url = installation.proxy.redacted_url
    if not url:
        return
    parsed = urlsplit(url)
    if parsed.hostname != GATEWAY or parsed.port is None:
        return
    with socket.socket() as probe:
        probe.settimeout(5.0)
        if probe.connect_ex(("127.0.0.1", parsed.port)) != 0:
            raise SystemExit(
                f"{url} names this workstation, and nothing is listening on "
                f"port {parsed.port}; start the proxy before the run"
            )


def _target_paths(workdir: Path, installation: InstallConfig) -> tuple[Path, ...]:
    count = max(1, len(installation.disk.graph.of_type(Existing)))
    if count == 1:
        return (workdir / "target.qcow2",)
    return tuple(workdir / f"target{index}.qcow2" for index in range(count))


#: Fixtures whose target disk has to hold a table before the installer runs,
#: and what `parted` is told to put there. A configuration with `create =
#: false` describes a table the operator already has, and every other fixture
#: gets a blank disk, so nothing exercised the editing path at all.
SEEDED: dict[str, tuple[str, ...]] = {
    "mbr-edit": ("mklabel", "msdos", "mkpart", "primary", "ext2", "1MiB", "1025MiB"),
}


def create_target(path: Path, seed: tuple[str, ...] = ()) -> Path:
    """A disk for the installer to partition, thrown away with the run.

    Seeded through a raw image and converted: `parted` writes to a file, and
    it cannot read a qcow2.
    """
    # The first thing this does is delete the file, and the images an in-place
    # conversion will be given are downloaded once and kept: `lab/vm/cloud/`
    # holds three of them. A run writes only inside its own directory, so a
    # path outside `WORKROOT` is a mistake rather than a target, and it is
    # refused before the unlink rather than diagnosed after it.
    if WORKROOT not in path.resolve().parents:
        raise ValueError(f"{path} is not inside {WORKROOT}, and this deletes what it is given")
    path.unlink(missing_ok=True)
    if not seed:
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(path), TARGET_SIZE],
            check=True,
            capture_output=True,
        )
        return path
    raw = path.with_suffix(".raw")
    raw.unlink(missing_ok=True)
    subprocess.run(
        ["qemu-img", "create", "-f", "raw", str(raw), TARGET_SIZE],
        check=True,
        capture_output=True,
    )
    subprocess.run(["parted", "--script", str(raw), *seed], check=True, capture_output=True)
    subprocess.run(
        ["qemu-img", "convert", "-O", "qcow2", str(raw), str(path)],
        check=True,
        capture_output=True,
    )
    raw.unlink(missing_ok=True)
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


def ssh(
    key: Path, port: int, command: str, *, host: str = "127.0.0.1"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ssh", "-p", str(port), "-i", str(key),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            f"root@{host}", command,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def push_config(key: Path, port: int, path: str, contents: str) -> None:
    """Push the substituted configuration through the live SSH channel."""
    result = subprocess.run(
        [
            "ssh", "-p", str(port), "-i", str(key),
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "root@127.0.0.1",
            f"cat > {path}",
        ], input=contents, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"the live SSH configuration push failed: {result.stderr.strip()}")


#: How long the initramfs is given to reach its ssh daemon. The guest has to
#: POST, load a bootloader and unpack an initramfs first, and connecting the
#: moment the reset returns answered `Connection timed out during banner
#: exchange` for a daemon that was seconds from starting.
DAEMON_PATIENCE: Final[float] = 180.0
DAEMON_PAUSE: Final[float] = 3.0


def wait_for_unlock_daemon(
    key: Path, port: int, *, host: str = "127.0.0.1", patience: float = DAEMON_PATIENCE
) -> None:
    """Block until the initramfs answers on `port`, or say it never did.

    The banner is the whole question: exit 255 with `Permission denied` is a
    daemon that is up and does not trust this key yet, and a refused or silent
    port is one that is not.
    """
    deadline = time.monotonic() + patience
    last = ""
    while time.monotonic() < deadline:
        probe = subprocess.run(
            [
                "ssh", "-p", str(port), "-i", str(key),
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                f"root@{host}", "true",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        said = f"{probe.stdout}{probe.stderr}"
        if probe.returncode == 0 or "Permission denied" in said:
            return
        last = said.strip()[-200:]
        time.sleep(DAEMON_PAUSE)
    raise RuntimeError(f"no ssh daemon on port {port} after {patience:.0f}s: {last}")


def try_remote_unlock(key: Path, port: int, installation: InstallConfig) -> str:
    """Answer the empty string when the unlock worked, or why it did not."""
    try:
        remote_unlock(key, port, installation)
    except RuntimeError as error:
        return str(error)
    return ""


def remote_unlock(
    key: Path,
    port: int,
    installation: InstallConfig,
    *,
    host: str = "127.0.0.1",
    timeout: float = 300.0,
) -> str:
    """Unlock the configured root and verify the mechanism's own result."""
    commands = remote_unlock_commands(installation)
    if commands is None:
        raise RuntimeError("remote unlock is disabled")
    command, proof = commands
    # Before the unlock, not racing it: the daemon comes up inside an initramfs
    # that has to be unpacked first.
    wait_for_unlock_daemon(key, port, host=host)
    process = subprocess.Popen(
        [
            "ssh", "-p", str(port), "-i", str(key),
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"root@{host}",
            command,
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = process.communicate(f"{DISK_PASSPHRASE}\n", timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        raise RuntimeError(f"remote unlock timed out after {timeout:.0f}s") from error
    if process.returncode != 0:
        raise RuntimeError(f"remote unlock failed: {output.strip()[-300:]}")
    if proof is None:
        return "unlocked"
    checked = ssh(key, port, proof, host=host)
    status = checked.stdout.strip()
    if checked.returncode != 0 or status != "available":
        raise RuntimeError(f"remote ZFS unlock reported keystatus {status!r}")
    return status


def remote_unlock_commands(installation: InstallConfig) -> tuple[str, str | None] | None:
    """Return the remote client command and its configuration-derived proof."""
    if not installation.kernel.remote_unlock.enabled:
        return None
    if installation.bootloader.kind is Bootloader.ZFSBOOTMENU:
        root = next(
            mount.source
            for mount in installation.disk.graph.of_type(Mountpoint)
            if str(mount.path) == "/"
        )
        dataset = installation.disk.graph[root]
        if not isinstance(dataset, ZfsDataset) or not dataset.name.startswith(dataset_for("/")):
            raise RuntimeError("ZFSBootMenu root is not a configured root dataset")
        pool = installation.disk.graph[dataset.pool]
        if not isinstance(pool, ZfsPool):
            raise RuntimeError("ZFSBootMenu root dataset has no configured pool")
        full_name = f"{pool.name}/{dataset.name}"
        return "zfs load-key -a", f"zfs get -H -o value keystatus {full_name}"
    return "unlock", None


def remote_config(installation: InstallConfig, public_key: str) -> InstallConfig:
    """Replace fixture-only remote credentials with values owned by this run.

    The interface goes with the address, for the reason `cluster.rewrite_fixtures`
    clears it: `vm-unlock` names `eth0`, and udev renames that device. This
    guest's own serial log has `virtio_net virtio0 enp0s2: renamed from eth0`
    at 156.6 seconds, after `systemd-networkd` had started and after `Reached
    target Network` — so the unit generated from `ip=eth0:dhcp` was matching a
    name that no longer existed, and nothing answered on the forwarded port.
    """
    unlock = installation.kernel.remote_unlock
    if not unlock.enabled:
        return installation
    return replace(
        installation,
        system=replace(installation.system, authorized_keys=(public_key,)),
        kernel=replace(
            installation.kernel,
            remote_unlock=replace(unlock, address="", interface=""),
        ),
    )


def reach_shell(console: SerialConsole, medium: Medium) -> None:
    if medium.login_user is None:
        console.expect(medium.root_prompt, timeout=300.0)
    else:
        console.login(medium.login_user, medium.login_password, medium.root_prompt)
    if medium.become_root:
        console.send(medium.become_root)
        console.expect(r"root@", timeout=60.0)
    pin_resolver(console)
    for command in medium.prepare:
        # `bootstrap.sh` prints the package manager line and stops rather than
        # running it, which is what an operator wants and what left every
        # Debian run at `missing commands: mkfs.vfat sgdisk`.
        console.run(command, timeout=600.0)


def pin_resolver(console: SerialConsole) -> None:
    """Point the guest at a fixed resolver rather than at qemu's forwarder.

    Slirp reads the host's `/etc/resolv.conf` once at startup, so a host that
    changes resolver mid-run strands an install that is half an hour in with
    `Temporary failure in name resolution`.
    """
    console.run("printf 'nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n' > /etc/resolv.conf")



def unlock_and_login(
    console: SerialConsole,
    installation: InstallConfig,
    monitor: Path | None = None,
    remote_unlocked: bool = False,
) -> str:
    """Answer every passphrase prompt on the way to a login.

    `monitor` is qemu's monitor socket. GRUB unlocks an encrypted BIOS disk
    before it reads `grub.cfg`, so that prompt is on the VGA console whatever
    `GRUB_TERMINAL` says and the serial port stays silent: proved with a
    screendump reading `Enter passphrase for hd0,msdos2` beside an empty
    serial log. Typing it through the monitor is what gets past GRUB; every
    prompt after it, dracut's included, is on the serial port.
    """
    graph = installation.disk.graph
    encrypted = bool(graph.of_type(Luks)) or any(
        pool.encrypted for pool in graph.of_type(ZfsPool)
    )
    if not encrypted:
        console.login("root", INSTALLED_PASSWORD, r"# ")
        return "console"
    if remote_unlocked:
        console.expect(r"login:", timeout=300.0)
        console.send("root")
        console.expect(PASSWORD_PROMPT, timeout=60.0)
        console.send(INSTALLED_PASSWORD)
        console.expect(r"# ", timeout=60.0)
        return "remote"
    # Bounded: a wrong passphrase makes the prompt come back, and each one
    # re-arms the timeout, so an unbounded loop would never fail.
    if monitor is not None and installation.bootloader.firmware is BootFirmware.BIOS:
        # Blind, because there is nothing to wait for: GRUB's prompt never
        # reaches this console. A wrong guess costs one retry at the next
        # prompt, which is on the serial port and is waited for below.
        time.sleep(GRUB_PROMPT_SECONDS)
        type_text(monitor, DISK_PASSPHRASE)
    for _ in range(5):
        seen = console.expect(rf"{PASSPHRASE_PROMPT}|login:", timeout=300.0)
        if b"login:" in seen:
            console.send("root")
            console.expect(PASSWORD_PROMPT, timeout=60.0)
            console.send(INSTALLED_PASSWORD)
            console.expect(r"# ", timeout=60.0)
            return "console"
        console.send(DISK_PASSPHRASE)
    raise SystemExit("the disk kept asking for a passphrase; it is not the one installed")


def check_installed(console: SerialConsole, installation: InstallConfig) -> None:
    """Assert against the system that was installed, booted from its own disk."""
    console.run(f"mkdir -p {RESULT_DIR}")
    for check in checks(installation):
        console.run(f"{{ {check.command} ; }} > {shlex.quote(f'{RESULT_DIR}/{check.name}.txt')} 2>&1")
    console.run(collect_command(RESULT_DIR))
    console.run("sync")


def stage_passphrases(console: SerialConsole, installation: InstallConfig) -> None:
    """Put the passphrases where the layout says they are.

    An operator does this by hand before an unattended install; the harness
    does it here so an encrypted layout can be tested without a prompt.
    """
    for command in stage_passphrase_commands(installation):
        console.run(command)


def interrupt_and_resume(console: SerialConsole, config: str) -> None:
    """Kill a run partway, then finish it with `--resume`.

    The one path nothing exercised. A resumed run skips what an earlier one
    recorded as done, so anything recorded that did not survive -- a mount, an
    open container -- is a stage3 unpacked into the live medium's own memory.

    Killed at the stage3 rather than at a fixed time: it is the first operation
    that takes long enough to interrupt reliably, and everything before it is
    exactly the destructive part a resumed run must not repeat.
    """
    console.run("mkdir -p /mnt/driver")
    console.run("mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver")
    console.run(f"mkdir -p {RESULT_DIR}")
    # In the background, with a watcher that kills it once the log says the
    # partitioning is behind us. `pkill -f` on the module name: bootstrap.sh
    # execs python, so the shell's own pid is not what is running by then.
    console.run(
        f"cd /mnt/driver && (sh ./bootstrap.sh --no-shell --config {config} "
        f"> {RESULT_DIR}/first.txt 2>&1 &) ; sleep 1"
    )
    console.run(
        f"for _ in $(seq 1 240); do "
        f"grep -q 'stage3' {RESULT_DIR}/first.txt && break; sleep 1; done; "
        f"sleep 3; pkill -f gentoo_install; sleep 2; echo killed",
        timeout=400.0,
    )
    console.run(f"grep -c . {RESULT_DIR}/first.txt || true")
    # The resumed run has to reach the end on its own.
    console.run(
        f"cd /mnt/driver && {{ sh ./bootstrap.sh --no-shell --resume --config {config}; "
        f"echo $? > {RESULT_DIR}/install.rc; }} 2>&1 | tee {RESULT_DIR}/install.txt",
        timeout=3600.0,
    )
    console.run(f"cp /mnt/driver/{config} {RESULT_DIR}/config.toml 2>/dev/null || true")
    console.run(f"cp /run/gentoo-install/install.jsonl {RESULT_DIR}/ 2>/dev/null || true")
    # What the resumed run skipped, which is the whole question.
    console.run(
        f"grep -c 'done earlier' {RESULT_DIR}/install.txt > {RESULT_DIR}/skipped.txt || "
        f"echo 0 > {RESULT_DIR}/skipped.txt"
    )
    console.run(collect_command(RESULT_DIR))


def run_installer(console: SerialConsole, config: str, extra: str = "") -> None:
    """Run the installer from the driver CD and keep everything it printed."""
    console.run("mkdir -p /mnt/driver")
    console.run("mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver")
    console.run(f"mkdir -p {RESULT_DIR}")
    # tee, not a redirect: the serial console is the only way to watch a run
    # that takes half an hour, and a redirect makes it silent until it ends.
    # Through the launcher, not python directly: that is the entry point an
    # operator uses, so every run exercises it.
    # The exit code is written inside the pipeline's first stage rather than
    # read from PIPESTATUS afterwards: that is a bash array, and a live system
    # running busybox ash answers `bad substitution` and never finishes.
    # --no-shell: this is a serial console, so stdin is a terminal, and every
    # question the installer asks on the way out sits there for ever. It offers
    # a root shell and a paste of the log, and a run that fails reaches both.
    console.run(
        f"cd /mnt/driver && {{ sh ./bootstrap.sh --no-shell --config {config} {extra}; "
        f"echo $? > {RESULT_DIR}/install.rc; }} 2>&1 | tee {RESULT_DIR}/install.txt",
        timeout=3600.0,
    )
    console.run(f"cp /mnt/driver/{config} {RESULT_DIR}/config.toml 2>/dev/null || true")
    # The journal says which packages came from a binary host and which were
    # compiled, which is the question this configuration exists to answer.
    console.run(f"cp /run/gentoo-install/install.jsonl {RESULT_DIR}/ 2>/dev/null || true")
    console.run(collect_command(RESULT_DIR))
    console.run("sync")


def install_remote_config(
    console: SerialConsole,
    key: Path,
    ssh_port: int,
    installation: InstallConfig,
    config: str,
) -> str:
    """Publish run-owned remote-unlock values and return the bootstrap path."""
    if not installation.kernel.remote_unlock.enabled:
        return config
    public_key = key.with_suffix(".pub").read_text().strip()
    substituted = remote_config(installation, public_key)
    remote_path = f"/tmp/{Path(config).name}"
    push_config(key, ssh_port, remote_path, to_toml(substituted))
    return remote_path


def probe(console: SerialConsole) -> None:
    console.run(f"mkdir -p {RESULT_DIR}")
    for name, command in PROBE:
        console.run(f"{{ {command} ; }} > {shlex.quote(f'{RESULT_DIR}/{name}.txt')} 2>&1")
    console.run(collect_command(RESULT_DIR))
    console.run("sync")


def main(argv: list[str] | None = None) -> int:
    # A run takes half an hour and every print is buffered when stdout is a
    # file, so a campaign showed nothing at all until it finished.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
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
    parser.add_argument(
        "--and-boot",
        action="store_true",
        help="after a successful --install, boot the disk it produced and check it; "
        "one invocation instead of two, which is what an unattended campaign needs",
    )
    parser.add_argument(
        "--interrupt",
        action="store_true",
        help="kill the installer partway and finish it with --resume, which is the "
        "one path nothing else exercises",
    )
    parser.add_argument("--interactive", action="store_true", help="hand the VM to a human over SSH")
    parser.add_argument(
        "--cpus",
        type=int,
        help="vCPUs for the guest, which is what its MAKEOPTS is derived from",
    )
    parser.add_argument("--keep", action="store_true", help="keep the run directory")
    args = parser.parse_args(argv)

    if args.and_boot:
        # The install first, then the same arguments again with the boot check.
        # Recursion rather than a loop: `main` owns the lock, the workdir and
        # the port, and both halves need their own.
        installed = main([one for one in (argv or sys.argv[1:]) if one != "--and-boot"])
        if installed != 0:
            return installed
        return main(
            [one for one in (argv or sys.argv[1:]) if one != "--and-boot"] + ["--boot-installed"]
        )

    medium = MEDIA[args.medium]
    # The configuration is part of the name: two runs sharing a directory would
    # share a serial socket, and the second one never connects.
    if args.boot_installed and not args.install:
        print("--boot-installed needs the same --install argument as the run it checks", file=sys.stderr)
        return 1
    if args.install:
        wanted = load(REPOSITORY / "tests" / args.install).bootloader.firmware.value
        if wanted != args.firmware:
            # Refused here rather than after the install: a BIOS layout booted
            # with UEFI firmware reaches the EDK2 shell, and the run spends
            # forty minutes installing before it fails at `never matched
            # 'login:'` with a `Shell>` prompt in the log.
            print(
                f"{args.install} installs for {wanted} and --firmware says {args.firmware}",
                file=sys.stderr,
            )
            return 1
    variant = Path(args.install).stem if args.install else "probe"
    workdir = WORKROOT / f"{medium.name}-{args.firmware}-{variant}"
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"installer revision: {_revision()}", flush=True)
    # The medium too: a rolling release keeps its filename, so a result
    # that names only the medium does not say which build it booted.
    print(f"medium: {medium.name} {medium.source_stamp()[:16]}", flush=True)
    try:
        held = claim(workdir)
    except RunInProgress as error:
        print(error, file=sys.stderr)
        return 1
    try:
        return _perform(args, medium, workdir)
    finally:
        # Released before returning, not at exit: `--and-boot` runs both halves
        # in one process, and the second could not claim a lock the first was
        # still holding.
        os.close(held)


def _perform(args: argparse.Namespace, medium: Medium, workdir: Path) -> int:
    ssh_port = args.ssh_port or free_port()
    key = ssh_keypair(workdir)
    requested = load(REPOSITORY / "tests" / args.install) if args.install else None
    if requested is not None:
        require_proxy(requested)
    remote_port = (
        free_port() if requested is not None and requested.kernel.remote_unlock.enabled else None
    )
    result_disk = create_disk(workdir / "result.img")
    driver_iso = build_driver(workdir / "driver.iso") if args.install and not args.boot_installed else None
    targets: tuple[Path, ...] = ()
    if args.install:
        # One disk per `existing` node, because the fixture names them
        # `virtio-target0`, `virtio-target1` and qemu numbers the serials the
        # same way; an mdraid layout needs more than one.
        wanted = _target_paths(workdir, load(REPOSITORY / "tests" / args.install))
        if args.boot_installed:
            missing = [path for path in wanted if not path.is_file()]
            if missing:
                print(f"{missing[0]} does not exist; run --install first", file=sys.stderr)
                return 1
            targets = wanted
        else:
            seed = SEEDED.get(Path(args.install).stem if args.install else "", ())
            targets = tuple(create_target(path, seed) for path in wanted)

    spec = VmSpec(
        medium=medium,
        workdir=workdir,
        **({"cpus": args.cpus} if args.cpus else {}),
        firmware=Firmware(args.firmware),
        ssh_port=ssh_port,
        remote_unlock_port=remote_port,
        disks=(result_disk,),
        driver_iso=driver_iso,
        targets=targets,
        boot_installed=args.boot_installed,
    )

    started = time.monotonic()
    with Vm(spec) as vm:
        with SerialConsole.connect(vm.serial_socket, vm.serial_log) as console:
            if args.boot_installed:
                expected = load(REPOSITORY / "tests" / args.install)
                unlocked_remotely = False
                unlock_failed = ""
                if expected.kernel.remote_unlock.enabled:
                    if remote_port is None:
                        raise RuntimeError("remote unlock is enabled without a forwarded port")
                    unlock_failed = try_remote_unlock(key, remote_port, expected)
                    if unlock_failed:
                        # Not a power-off: the machine is sitting at its own
                        # passphrase prompt, the console gets in, and a verdict
                        # that carries what the machine held beats one that
                        # carries only the port nobody answered on.
                        print(f"FAIL remote unlock: {unlock_failed}", file=sys.stderr)
                    else:
                        unlocked_remotely = True
                        print("installed root unlocked by remote SSH session")
                method = unlock_and_login(
                    console, expected, vm.monitor_socket, remote_unlocked=unlocked_remotely
                )
                print(f"[{time.monotonic() - started:5.1f}s] logged into the installed system ({method})")
                check_installed(console, expected)
                power_off(console, vm)
                code = report(
                    result_disk, keep=args.keep, assertions=REPOSITORY / "tests" / args.install
                )
                if unlock_failed:
                    # The subject of the fixture is the unlock, so a machine
                    # that only came up by console has still failed it.
                    code = code or 1
                if code == 0:
                    # The boot check is the last reader of the target disk, and
                    # one campaign left 80 GiB of them behind. A failed run
                    # keeps its disk: that is the only copy of what went wrong.
                    _discard(targets, keep=args.keep)
                return code
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

            if args.install and args.interrupt:
                stage_passphrases(console, load(REPOSITORY / "tests" / args.install))
                interrupt_and_resume(console, args.install)
            elif args.install:
                installation = load(REPOSITORY / "tests" / args.install)
                config = install_remote_config(console, key, ssh_port, installation, args.install)
                stage_passphrases(console, load(REPOSITORY / "tests" / args.install))
                run_installer(console, config, "--dry-run" if args.dry_run else "")
            else:
                probe(console)
            power_off(console, vm)

    return report(result_disk, keep=args.keep, installed=bool(args.install and not args.dry_run))


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


def _revision() -> str:
    """What the driver CD is about to be built from.

    A campaign that ran while its own tree was being committed to measured
    twelve different snapshots and could name none of them, so every run says
    this before it boots anything. `dirty` means the result proves nothing
    about any commit.
    """
    def ask(command: list[str]) -> str:
        try:
            done = subprocess.run(command, cwd=REPOSITORY, capture_output=True, text=True)
        except OSError:
            return ""
        return done.stdout if done.returncode == 0 else ""

    described = ask(["git", "describe", "--always", "--dirty"]).strip()
    if not described:
        return "unknown, not a git checkout"
    uncommitted = len(ask(["git", "status", "--short"]).splitlines())
    return f"{described} ({uncommitted} uncommitted files)" if uncommitted else described


def _discard(targets: Sequence[Path], *, keep: bool) -> None:
    """Drop the disks the guest was installed onto once nothing reads them."""
    if keep:
        return
    for path in targets:
        path.unlink(missing_ok=True)


def report(
    result_disk: Path, *, keep: bool, assertions: Path | None = None, installed: bool = False
) -> int:
    results = read_disk(result_disk)
    for name in sorted(results):
        print(f"--- {name} ---")
        print(results[name].decode("utf-8", "replace").rstrip())
    code = verdict(results, assertions, installed=installed)
    if not keep:
        result_disk.unlink(missing_ok=True)
    return code


def _from_config(config: Path) -> list[tuple[str, str]]:
    """Return the shared contract in the local result format."""
    installation = load(config)
    return [(check.name, check.pattern) for check in checks(installation)]


def verdict(
    results: dict[str, bytes], assertions: Path | None, *, installed: bool = False
) -> int:
    """Turn everything the run left behind into one exit code."""
    code = check_expected(results, assertions) if assertions is not None else 0
    installer = results.get("install.rc", b"").decode("utf-8", "replace").strip()
    remote = results.get("remote-unlock.rc", b"").decode("utf-8", "replace").strip()
    if remote not in ("", "0"):
        print(f"FAIL remote unlock exited {remote}", file=sys.stderr)
        code = 1
    # The installer's own exit code, which the collected output does not carry:
    # a run that stopped at the bootloader printed its failure and exited 0.
    if installer not in ("", "0"):
        print(f"FAIL the installer exited {installer}", file=sys.stderr)
        code = 1
    if installed and installer == "":
        # An absent code is an answer on a run that installed: the installer
        # was killed before it wrote one, or the archive lost it. Either way
        # nothing here says the install finished, and reporting success is how
        # a run that never completed became a green verdict.
        print("FAIL the run installed and collected no install.rc", file=sys.stderr)
        code = 1
    return code


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
    skipped = results.get("skipped.txt", b"").decode("utf-8", "replace").strip()
    if skipped and skipped.isdigit() and int(skipped) == 0:
        # An interrupted run that resumed and repeated everything partitioned a
        # disk it had already installed onto, which is what --resume exists to
        # avoid; it passing quietly is worse than it failing.
        missing.append("the resumed run skipped nothing an earlier one had finished")
    for problem in missing:
        print(f"FAIL {problem}", file=sys.stderr)
    if missing:
        return 1
    print("the installed system booted, mounted its layout and has no failed unit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
