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
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from gentoo_install.data import load_catalog
from gentoo_install.model import compat
from gentoo_install.model.config import Bootloader, InitSystem, InstallConfig
from gentoo_install.model.device import (
    Existing,
    Filesystem,
    Luks,
    Mountpoint,
    Subvolume,
    ZfsDataset,
    ZfsPool,
)
from gentoo_install.model.config import Networking
from gentoo_install.model.parse import load
from gentoo_install.plan.system import _network_service as network_service

from .console import PASSWORD_PROMPT, SerialConsole
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

#: The disk passphrase, which is not the root password: zfs takes at least
#: eight characters, and a real install does not reuse one for the other.
DISK_PASSPHRASE = "install-disk"
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

#: What a system installed by this installer has to be able to answer.
INSTALLED = (
    ("os-release", "cat /etc/os-release"),
    ("mounts", "findmnt --noheadings --list --output TARGET,SOURCE,FSTYPE"),
    ("fstab", "cat /etc/fstab"),
    ("locale", "locale"),
    ("hostname", "cat /etc/hostname || cat /etc/conf.d/hostname"),
    (
        "inputmethod",
        # `/etc/environment` is where the systemd plan writes: it appends there
        # rather than replacing a drop-in, and this check still read the
        # drop-in, so it could not see the file the install had written.
        "cat /etc/skel/.config/fcitx5/profile 2>/dev/null; "
        "cat /etc/environment /etc/env.d/90input-method 2>/dev/null",
    ),
    (
        "kernel",
        "uname -r; find /boot -maxdepth 4 -type f "
        r"\( -name 'vmlinuz*' -o -name 'kernel-*' -o -name linux -o -name '*.conf' \) | sort",
    ),
    # `test -s` rather than a name lookup: a dangling symlink is the defect
    # this catches, and live DNS in a guest is not something to make a verdict
    # depend on. The lookup is collected beside it for the report.
    (
        "resolver",
        "readlink -f /etc/resolv.conf; "
        "test -s /etc/resolv.conf && echo RESOLVCONF-OK || echo RESOLVCONF-EMPTY; "
        # Up to thirty seconds: dhcpcd writes the file when its lease arrives,
        # and asking the moment the login prompt appears asks too early.
        "for _ in $(seq 1 15); do getent hosts gentoo.org >/dev/null 2>&1 && break; sleep 2; done; "
        "getent hosts gentoo.org >/dev/null 2>&1 && echo RESOLVES || echo NORESOLVE; "
        "sed -n '1,4p' /etc/resolv.conf",
    ),
    # Needs no network, and fails outright on a profile the tree cannot read:
    # the first thing to break if a profile is changed and @world never rebuilt.
    ("portage", "emerge --info >/dev/null 2>&1 && echo EMERGE-OK || echo EMERGE-FAIL"),
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


def _target_paths(workdir: Path, installation: InstallConfig) -> tuple[Path, ...]:
    count = max(1, len(installation.disk.graph.of_type(Existing)))
    if count == 1:
        return (workdir / "target.qcow2",)
    return tuple(workdir / f"target{index}.qcow2" for index in range(count))


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
    if medium.become_root:
        console.send(medium.become_root)
        console.expect(r"root@", timeout=60.0)
    pin_resolver(console)


def pin_resolver(console: SerialConsole) -> None:
    """Point the guest at a fixed resolver rather than at qemu's forwarder.

    Slirp reads the host's `/etc/resolv.conf` once at startup, so a host that
    changes resolver mid-run strands an install that is half an hour in with
    `Temporary failure in name resolution`.
    """
    console.run("printf 'nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n' > /etc/resolv.conf")


#: What GRUB and the initramfs say when they want a passphrase. GRUB asks
#: because /boot is inside the container, the initramfs asks to open the root:
#: two prompts for one passphrase unless a keyfile is embedded.
PASSPHRASE_PROMPT = r"[Ee]nter passphrase|Please enter passphrase|password for"


def unlock_and_login(console: SerialConsole, installation: InstallConfig) -> None:
    """Answer every passphrase prompt on the way to a login."""
    graph = installation.disk.graph
    encrypted = bool(graph.of_type(Luks)) or any(
        pool.encrypted for pool in graph.of_type(ZfsPool)
    )
    if not encrypted:
        console.login("root", INSTALLED_PASSWORD, r"# ")
        return
    # Bounded: a wrong passphrase makes the prompt come back, and each one
    # re-arms the timeout, so an unbounded loop would never fail.
    for _ in range(5):
        seen = console.expect(rf"{PASSPHRASE_PROMPT}|login:", timeout=300.0)
        if b"login:" in seen:
            console.send("root")
            console.expect(PASSWORD_PROMPT, timeout=60.0)
            console.send(INSTALLED_PASSWORD)
            console.expect(r"# ", timeout=60.0)
            return
        console.send(DISK_PASSPHRASE)
    raise SystemExit("the disk kept asking for a passphrase; it is not the one installed")


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
        console.run(f"printf '%s' '{DISK_PASSPHRASE}' > {source}")
        console.run(f"chmod 600 {source}")


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


def probe(console: SerialConsole) -> None:
    console.run(f"mkdir -p {RESULT_DIR}")
    for name, command in PROBE:
        console.run(f"{{ {command} ; }} > {RESULT_DIR}/{name}.txt 2>&1")
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
            targets = tuple(create_target(path) for path in wanted)

    spec = VmSpec(
        medium=medium,
        workdir=workdir,
        **({"cpus": args.cpus} if args.cpus else {}),
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
                expected = load(REPOSITORY / "tests" / args.install)
                unlock_and_login(console, expected)
                print(f"[{time.monotonic() - started:5.1f}s] logged into the installed system")
                check_installed(console, expected)
                power_off(console, vm)
                code = report(
                    result_disk, keep=args.keep, assertions=REPOSITORY / "tests" / args.install
                )
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
    result_disk: Path, *, keep: bool, assertions: Path | None = None
) -> int:
    results = read_disk(result_disk)
    for name in sorted(results):
        print(f"--- {name} ---")
        print(results[name].decode("utf-8", "replace").rstrip())
    code = verdict(results, assertions)
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
    # From the same function the plan enables, not guessed from the init: a
    # desktop brings NetworkManager and the init's own manager stays off, so
    # asserting `systemd-networkd` failed a system that was correct.
    if installation.system.networking is not Networking.NONE:
        expected.append(("units", re.escape(network_service(installation.system))))
    if installation.bootloader.kind is Bootloader.SYSTEMD_BOOT:
        # bls: /boot/<entry-token>/<version>/linux, with an entry naming it.
        expected += [
            ("kernel", r"^/boot/[^/]+/[^/]+/linux$"),
            ("kernel", r"^/boot/loader/entries/.+\.conf$"),
        ]
    else:
        expected.append(("kernel", r"^/boot/(kernel|vmlinuz)-"))
    # The Chinese environment is what this installer exists for, so a run that
    # asked for an input method has to show it reached the installed system.
    groups = load_catalog()
    if any(groups[name].input_method for name in installation.packages.applications if name in groups):
        expected += [("inputmethod", r"^DefaultIM="), ("inputmethod", r"XMODIFIERS=@im=fcitx")]
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


def verdict(results: dict[str, bytes], assertions: Path | None) -> int:
    """Turn everything the run left behind into one exit code."""
    code = check_expected(results, assertions) if assertions is not None else 0
    installer = results.get("install.rc", b"").decode("utf-8", "replace").strip()
    # The installer's own exit code, which the collected output does not carry:
    # a run that stopped at the bootloader printed its failure and exited 0.
    if installer not in ("", "0"):
        print(f"FAIL the installer exited {installer}", file=sys.stderr)
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
    failed = results.get("failed.txt", b"").decode("utf-8", "replace").strip()
    if failed:
        missing.append(f"systemd reports failed units: {failed.splitlines()[0]}")
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
