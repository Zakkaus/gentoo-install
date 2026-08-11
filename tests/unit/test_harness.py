"""The harness has to report a failed install as a failed run.

It reads the installer's exit code off the result disk rather than from the
process it drove, so nothing else in the run can notice a failure for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.vm.run import verdict


def test_a_failed_installer_fails_the_run() -> None:
    assert verdict({"install.rc": b"1\n"}, None) == 1
    assert verdict({"install.rc": b"0\n"}, None) == 0


def test_a_run_that_collected_no_exit_code_is_not_called_a_failure() -> None:
    """A probe run never runs the installer, so there is nothing to fail."""
    assert verdict({}, None) == 0


def test_a_run_that_installed_and_collected_no_exit_code_is_a_failure() -> None:
    """The same `verdict` serves both modes, so an install whose archive lost
    `install.rc` — or whose installer was killed before writing one — reported
    success on a run that proves nothing finished."""
    assert verdict({}, None, installed=True) == 1
    assert verdict({"install.rc": b"0\n"}, None, installed=True) == 0


def test_every_configuration_the_campaign_names_exists() -> None:
    """A stage naming a fixture that was renamed fails half an hour in, after
    the medium has booted, rather than at the first line."""
    from tests.vm.campaign import STAGES

    root = Path(__file__).resolve().parents[1]
    for stage, runs in STAGES.items():
        for run in runs:
            assert (root / run.config).is_file(), f"{stage}: {run.config}"


def test_the_campaign_covers_every_vm_fixture() -> None:
    """A fixture nobody runs is a path nobody tests, and the point of the
    matrix is that the list cannot quietly fall behind."""
    from tests.vm.campaign import STAGES

    root = Path(__file__).resolve().parents[1]
    named = {Path(run.config).name for runs in STAGES.values() for run in runs}
    available = {path.name for path in (root / "fixtures").glob("*.toml")}
    assert available - named == set(), sorted(available - named)


def test_every_fixture_names_a_disk_the_harness_creates() -> None:
    """Three of them named `virtio-target` with no number, from before the
    harness numbered the serials, so preflight refused them twenty seconds in
    and they had never once installed anything."""
    from gentoo_install.model.device import Existing
    from gentoo_install.exec.config import load

    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "fixtures").glob("*.toml")):
        for disk in load(path).disk.graph.of_type(Existing):
            # `virtio-targetN` is what `tests/vm/qemu.py` gives each target
            # disk as its serial, and udev makes the by-id name from that.
            assert re.fullmatch(
                r"/dev/(disk/by-id/virtio-target\d+|null)", disk.selector
            ), f"{path.name}: {disk.selector}"


def test_every_encrypted_fixture_names_where_its_passphrase_lives() -> None:
    """`stage_passphrases` writes one file per node that names a path. A node
    encrypted without one fails preflight before the disks are touched, which
    is right, and means the fixture could never run."""
    from gentoo_install.model.device import Luks, ZfsPool
    from gentoo_install.exec.config import load

    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "fixtures").glob("*.toml")):
        graph = load(path).disk.graph
        for node in graph.of_type(Luks):
            assert node.passphrase_file, f"{path.name}: {node.id}"
        for pool in graph.of_type(ZfsPool):
            assert not pool.encrypted or pool.passphrase_file, f"{path.name}: {pool.id}"


def test_named_picks_runs_out_of_any_stage() -> None:
    """Testing four configurations again should be this harness with an
    argument, not a shell loop written for one afternoon and thrown away."""
    from tests.vm.campaign import named

    picked = named(["vm-raidz", "ext4-bios"])
    assert [Path(one.config).stem for one in picked] == ["vm-raidz", "ext4-bios"]
    # The firmware travels with the run, so a caller naming a BIOS fixture does
    # not have to remember to say so.
    assert picked[1].firmware == "bios"


def test_naming_a_fixture_that_does_not_exist_says_which_do() -> None:
    """A typo otherwise runs nothing and reports success."""
    import pytest

    from tests.vm.campaign import named

    with pytest.raises(SystemExit) as refused:
        named(["vm-raidz", "vm-typo"])
    assert "vm-typo" in str(refused.value)
    assert "vm-raidz" in str(refused.value)


def test_a_guest_runs_behind_whoever_is_at_the_keyboard() -> None:
    """The workstation is somebody's desktop while a campaign runs. Five guests
    at the default priority make the compositor stutter."""
    from tests.vm.qemu import _YIELDING, Firmware, VmSpec, Vm
    from tests.vm.media import MEDIA

    spec = VmSpec(
        medium=MEDIA["official-minimal"],
        workdir=Path("/tmp"),
        firmware=Firmware.UEFI,
        ssh_port=2222,
    )
    argv = Vm(spec)._argv()
    assert argv[: len(_YIELDING)] == list(_YIELDING)
    assert argv[len(_YIELDING)] == "qemu-system-x86_64"
    # Best-effort at its lowest, not the idle class: idle starves a guest that
    # is extracting a stage3 while something else touches the disk.
    if "ionice" in _YIELDING:
        assert "-c" in _YIELDING and "2" in _YIELDING


def test_the_machine_is_packed_by_weight_rather_than_by_count() -> None:
    """A run that compiles a kernel and one that unpacks binary packages cost
    the machine ten times differently. Counting guests put five compile jobs on
    it at once and then left two of them alone for the last forty minutes."""
    from tests.vm.campaign import CAPACITY, STAGES

    runs = [one for stage in STAGES.values() for one in stage]
    heavy = [one for one in runs if one.weight > 1]
    assert heavy, "nothing is marked as compiling, so the weight does nothing"
    assert all(one.cpus > 5 for one in heavy), "a compile job with the default cores"
    assert all(one.cpus == 0 for one in runs if one.weight == 1)
    # Three compile jobs at once, or six light ones. More than three saturates
    # the host's threads and every one of them takes longer.
    assert CAPACITY // max(one.weight for one in heavy) == 3


def test_a_heavier_run_asks_for_the_cores_on_the_command_line() -> None:
    """The guest derives its MAKEOPTS from the vCPU count, so the weight has to
    reach `run.py` rather than only the scheduler."""
    from tests.vm.campaign import Run

    argv = Run("fixtures/vm-cjk-kernel.toml", weight=2, cpus=10).argv()
    assert "--cpus" in argv and argv[argv.index("--cpus") + 1] == "10"
    assert "--cpus" not in Run("fixtures/vm-binpkg.toml").argv()


def test_every_configuration_the_campaign_runs_reaches_the_serial_port() -> None:
    """The harness reads the installed system over `-serial`, so a kernel that
    was never told about ttyS0 prints to tty0 and the boot check waits out its
    timeout against an empty buffer, ten minutes after a clean install."""
    import tomllib

    from tests.vm.campaign import STAGES

    root = Path(__file__).resolve().parents[1]
    for stage, runs in STAGES.items():
        for run in runs:
            held = tomllib.loads((root / run.config).read_text())
            params = held.get("bootloader", {}).get("kernel_params", [])
            assert any(one.startswith("console=ttyS0") for one in params), (
                f"{stage}: {run.config}"
            )


def test_replacing_an_iso_at_the_same_path_re_extracts_it(tmp_path: Path) -> None:
    """A rolling release keeps its filename, so keying the cache by name left
    the previous kernel in place and the campaign reported a new matrix while
    booting the old medium."""
    import tests.vm.media as media

    extracted: list[Path] = []

    def fake_extract(iso: Path, files: dict[str, Path]) -> None:
        extracted.append(iso)
        for target in files.values():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(iso.read_bytes())

    iso = tmp_path / "rolling.iso"
    iso.write_bytes(b"the first build")
    medium = media.Medium(
        name="rolling",
        iso=iso,
        volume_label="rolling",
        kernel_in_iso="/boot/kernel",
        initrd_in_iso="/boot/initrd",
        root_prompt="# ",
    )
    original_cache, original_extract = media.CACHE, media._extract
    media.CACHE = tmp_path / "cache"
    media._extract = fake_extract
    try:
        kernel, _ = medium.boot_files()
        assert kernel.read_bytes() == b"the first build"
        medium.boot_files()
        assert len(extracted) == 1, "an unchanged ISO was extracted twice"

        iso.write_bytes(b"the second build entirely")
        kernel, _ = medium.boot_files()
        assert len(extracted) == 2, "a replaced ISO was served from the cache"
        assert kernel.read_bytes() == b"the second build entirely"
    finally:
        media.CACHE, media._extract = original_cache, original_extract


def test_bootstrap_names_a_package_for_every_command_preflight_wants() -> None:
    """`package_for` fell through to printing the command itself, so an LVM or
    stage3 install was told to install executable names that are not packages."""
    import subprocess

    from gentoo_install.exec import preflight

    root = Path(__file__).resolve().parents[2]
    source = (root / "bootstrap.sh").read_text()
    start = source.index("package_for()")
    body = source[start : source.index("\n}\n", start) + 3]
    script = body + '\npackage_for "$1" "$2"\n'

    operation_commands = {
        command for commands in preflight.BY_OPERATION.values() for command in commands
    }
    wanted = operation_commands | {
        command
        for group in (
            preflight.ALWAYS,
            preflight.MENU_ONLY,
            *preflight.BY_FEATURE.values(),
            *preflight.EXTRA_FILESYSTEM_COMMANDS.values(),
        )
        for command in group
    }
    stage3_providers = {
        "gentoo": {"tar": "tar", "xz": "xz-utils", "gpg": "gnupg", "gpg-agent": "gnupg"},
        "alpine": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gpg-agent"},
        "debian": {"tar": "tar", "xz": "xz-utils", "gpg": "gnupg", "gpg-agent": "gpg-agent"},
        "ubuntu": {"tar": "tar", "xz": "xz-utils", "gpg": "gnupg", "gpg-agent": "gpg-agent"},
        "arch": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gnupg"},
        "fedora": {
            "tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gnupg2-gpg-agent",
        },
        "rhel": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gnupg2"},
        "centos": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gnupg2"},
        "suse": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gpg2"},
        "opensuse": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gpg2"},
        "opensuse-leap": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gpg2"},
        "opensuse-tumbleweed": {
            "tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gpg2",
        },
    }
    assert all(set(providers) == operation_commands for providers in stage3_providers.values())
    # These are the commands every distribution really names its package after.
    # Alpine splits util-linux into one package per tool, so each of those is
    # its own name there and nowhere else.
    itself = {"cryptsetup", "mdadm", "parted", "btrfs", "tar", "sgdisk", "zfs", "openssl"}
    split_on_alpine = {"mount", "umount", "lsblk", "blkid", "findmnt", "wipefs"}
    # Debian and Ubuntu keep mount and umount in a package of that name.
    named_that = {
        ("mount", "debian"),
        ("umount", "debian"),
        ("mount", "ubuntu"),
        ("umount", "ubuntu"),
    }
    wrong: list[str] = []
    for command in sorted(wanted):
        for family, providers in stage3_providers.items():
            said = subprocess.run(
                ["sh", "-c", script, "_", command, family], capture_output=True, text=True
            ).stdout.strip()
            if command in operation_commands:
                assert said == providers[command], (command, family, said)
                continue
            if command in itself:
                continue
            allowed = (family == "alpine" and command in split_on_alpine) or (
                command,
                family,
            ) in named_that
            if said == command and not allowed:
                wrong.append(f"{command}:{family}")
    assert not wrong, wrong


def test_alpine_gets_stage3_helpers_from_the_preflight_contract() -> None:
    from tests.vm import media

    helpers = {"xz", "gpg-agent"}
    assert helpers <= media._fixture_commands()
    assert helpers <= set(media.ALPINE_PACKAGES)


def test_the_installed_checks_read_the_files_the_plan_writes() -> None:
    """The input-method check read a drop-in after the plan moved to
    `/etc/environment`, so it could not see the file the install had written
    and reported a mismatch on a correct system for every desktop run."""
    from gentoo_install.plan.packages import ENVIRONMENT_FILE
    from tests.vm.run import INSTALLED

    asked = dict(INSTALLED)["inputmethod"]
    missing = [str(one) for one in ENVIRONMENT_FILE.values() if str(one) not in asked]
    assert not missing, (missing, asked)
    # And nothing the plan stopped writing: a path left behind reads as
    # coverage while the check answers with nothing.
    named = {
        word
        for word in asked.replace(";", " ").split()
        if word.startswith("/etc/") and "fcitx5" not in word
    }
    assert named == {str(one) for one in ENVIRONMENT_FILE.values()}, named


def test_a_guest_waits_until_the_machine_has_room_for_it() -> None:
    """A fixed count cannot know what else the machine is doing: one campaign
    ran beside an editor and a test suite and lost sixteen of twenty-four
    guests to earlyoom, which reads as an installer defect in every log."""
    import tests.vm.campaign as campaign

    said: list[str] = []
    answers = iter([1024**3, 2 * 1024**3, campaign.GUEST_BYTES + campaign.HEADROOM_BYTES])
    original = campaign.available_bytes
    campaign.available_bytes = lambda: next(answers)
    try:
        campaign.wait_for_room(said.append, patience=0.0)
    finally:
        campaign.available_bytes = original
    assert said and "waiting for memory" in said[0], said


def test_a_machine_whose_memory_cannot_be_read_is_not_stalled_on() -> None:
    """No measurement is not a reason to wait for ever."""
    import tests.vm.campaign as campaign

    original = campaign.available_bytes
    campaign.available_bytes = lambda: 0
    try:
        campaign.wait_for_room(lambda line: None)
    finally:
        campaign.available_bytes = original


def test_a_passphrase_becomes_the_keys_the_monitor_understands() -> None:
    """GRUB unlocks an encrypted BIOS disk before it reads `grub.cfg`, so its
    prompt is on the VGA console whatever `GRUB_TERMINAL` says and `sendkey`
    is the only way to answer it."""
    from tests.vm.monitor import MonitorError, keys_for

    assert keys_for("install-disk") == [
        "i", "n", "s", "t", "a", "l", "l", "minus", "d", "i", "s", "k",
    ]
    assert keys_for("Ab.1_/") == ["shift-a", "b", "dot", "1", "shift-minus", "slash"]
    # Refused rather than dropped: an unnamed character would be sent as
    # itself and silently ignored, and the guest would wait for ever.
    with pytest.raises(MonitorError):
        keys_for("wide\u4e2d")


def test_the_guest_offers_a_monitor_socket() -> None:
    """Without one there is no way past GRUB's own prompt on a BIOS install
    with an encrypted disk, and the run times out on an empty serial log."""
    from tests.vm.qemu import Firmware, Vm, VmSpec
    from tests.vm.media import MEDIA

    spec = VmSpec(
        medium=MEDIA["official-minimal"], workdir=Path("/tmp"), firmware=Firmware.BIOS
    )
    argv = Vm(spec)._argv()
    assert "-monitor" in argv
    assert "none" not in argv[argv.index("-monitor") + 1]


def test_the_campaign_gives_every_run_the_firmware_its_fixture_installs_for() -> None:
    """A BIOS layout booted with UEFI firmware reaches the EDK2 shell, and the
    run spends forty minutes installing before it fails at `never matched
    'login:'` with `Shell>` in the log."""
    from pathlib import Path

    from gentoo_install.exec.config import load
    from tests.vm.campaign import STAGES

    for runs in STAGES.values():
        for run in runs:
            wanted = load(Path("tests") / run.config).bootloader.firmware.value
            assert run.firmware == wanted, f"{run.config} installs for {wanted}"


def test_a_run_whose_firmware_contradicts_its_fixture_is_refused() -> None:
    """Refused before the medium boots, not after the install."""
    from tests.vm.run import main

    assert main(["--install", "fixtures/vm-bios.toml", "--firmware", "uefi"]) == 1
    assert main(["--install", "fixtures/vm-binpkg.toml", "--firmware", "bios"]) == 1


def test_every_fixture_sets_the_password_the_harness_logs_in_with() -> None:
    """`ext4-bios.toml` carried a placeholder hash, so an install from it
    reached a login prompt and answered `Login incorrect` — a run that had
    partitioned, installed and booted correctly, failed on its last step."""
    import subprocess
    from pathlib import Path

    from gentoo_install.exec.config import load
    from tests.vm.run import INSTALLED_PASSWORD

    # `openssl passwd`, not `crypt`: the module left the standard library in
    # 3.13 and openssl is a tool the installer already requires.
    for fixture in sorted(Path("tests/fixtures").glob("*.toml")):
        hashed = load(fixture).system.root_password_hash
        assert hashed, f"{fixture.name} sets no root password"
        salt = hashed.split("$")[2]
        said = subprocess.run(
            ["openssl", "passwd", "-6", "-salt", salt, INSTALLED_PASSWORD],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert said == hashed, f"{fixture.name} sets a hash that is not {INSTALLED_PASSWORD!r}"


def test_the_boot_check_asks_what_the_fixture_asked_for() -> None:
    """`hostname` and `kernel` carried an empty expectation, and `mounts` only
    looked for `/`, so a guest that came up with the wrong hostname, the wrong
    root filesystem or the wrong locale passed every check and the run was
    recorded `ok`. That is success with the wrong result, which no assertion
    in this repository could see."""
    from pathlib import Path

    from gentoo_install.exec.config import load
    from tests.vm.cluster import _asked_for

    wanted = {
        "vm-xfs": ("xfsbox", "xfs", "systemd", "en_US.UTF-8"),
        "vm-btrfs": ("btrfsbox", "btrfs", "systemd", "zh_TW.UTF-8"),
        "vm-zfs": ("zfsbox", "zfs", "systemd", "en_US.UTF-8"),
        "vm-openrc-desktop": ("openrcdesk", "ext4", "openrc", "en_US.UTF-8"),
    }
    for name, (host, filesystem, init, locale) in wanted.items():
        said = {one: value for one, _, value in _asked_for(load(Path("tests/fixtures") / f"{name}.toml"))}
        assert said["hostname"] == host, name
        assert said["root filesystem"] == filesystem, name
        assert said["init"] == init, name
        assert said["locale"] == f"LANG={locale}", name


def test_every_fixture_has_a_boot_check_that_can_fail() -> None:
    """A fixture whose checks are all empty strings is one the boot pass
    cannot fail, which is how the empty ones went unnoticed."""
    from pathlib import Path

    from gentoo_install.exec.config import load
    from tests.vm.cluster import _asked_for

    for fixture in sorted(Path("tests/fixtures").glob("*.toml")):
        checks = _asked_for(load(fixture))
        empty = [one for one, _, value in checks if not value]
        assert not empty, f"{fixture.name}: {empty}"
        assert len(checks) >= 4, f"{fixture.name}: {checks}"


def test_a_fixture_named_twice_is_refused_before_any_guest_is_built() -> None:
    """Every map in the schedule is keyed by job name, so the second guest
    overwrote the first's bookkeeping: one outcome ended the loop while the
    other was still running, `1/1 passed` was printed for two jobs, and the
    second guest could outlive the process."""
    from tests.vm.cluster import main

    assert main(["vm-lvm", "vm-lvm"]) == 1


def test_a_run_that_collected_fewer_results_than_it_dispatched_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that died without answering left its job with no outcome, and
    the verdict compared passes against the outcomes that arrived rather than
    against the jobs that were asked for: two dispatched, one returned, `1/1
    passed` and exit 0."""
    from typing import Any

    from tests.vm import cluster

    def one_result(jobs: list[Any], *args: Any, **kwargs: Any) -> list[cluster.Outcome]:
        return [
            cluster.Outcome(
                name=jobs[0].name,
                verdict=cluster.Verdict.OK,
                seconds=1.0,
                detail="",
                revision="revision-a",
            )
        ]

    monkeypatch.setattr(cluster, "run", one_result)
    assert cluster.main(["vm-lvm", "vm-xfs"]) == 1
    # And the ordinary case still passes, so this is not failing on everything.
    def both(jobs: list[Any], *args: Any, **kwargs: Any) -> list[cluster.Outcome]:
        return [
            cluster.Outcome(
                name=one.name,
                verdict=cluster.Verdict.OK,
                seconds=1.0,
                detail="",
                revision="revision-a",
            )
            for one in jobs
        ]

    monkeypatch.setattr(cluster, "run", both)
    assert cluster.main(["vm-lvm", "vm-xfs"]) == 0


def test_a_guest_that_could_not_be_removed_keeps_its_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`destroy()` failing printed a line and the scheduler handed the node's
    slot back anyway. The memory is still allocated, so the next guest went
    onto a node with no room for it and the hypervisor ended it."""
    from tests.vm import cluster

    class Guest:
        def stop(self) -> None:
            return None

        def destroy(self) -> None:
            return None

    def dispatch(name: str) -> cluster.Job:
        job = cluster.Job(name, Path(f"tests/fixtures/{name}.toml"))
        execution = cluster.Running(
            Guest(),
            cluster.Watchdog(Path(f"/nonexistent/{name}.log"), lambda: 0),
            job.reservation_bytes,
        )
        return job.dispatch(
            "infra-node1",
            9300,
            Path(f"/nonexistent/{name}.lease"),
            Path(f"/nonexistent/{name}.log"),
            execution,
        )

    removed = dispatch("vm-lvm")
    retained = dispatch("vm-xfs")
    before = cluster._reserved_bytes({removed.name: removed, retained.name: retained})
    assert retained.execution is not None
    reservation = retained.execution.reservation_bytes
    monkeypatch.setattr(cluster, "GUEST_MEMORY_MIB", 5120)

    removed = removed.answered(
        cluster.Outcome(removed.name, cluster.Verdict.OK, 1.0, removed=True)
    ).collect(0)
    retained = retained.answered(
        cluster.Outcome(retained.name, cluster.Verdict.ERROR, 1.0, removed=False)
    ).collect(1)
    after = cluster._reserved_bytes({removed.name: removed, retained.name: retained})

    assert before["infra-node1"] == 2 * reservation
    assert after == {"infra-node1": reservation}


def test_cleanup_result_does_not_leak_between_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later campaign may run the same job after an earlier cleanup failed.
    Its removed guest must return the slot and VMID."""
    import queue
    from typing import cast

    from tests.vm import cluster
    from tests.vm.proxmox import Api, ProxmoxError

    destroy_fails = iter((True, False))

    class FakeGuest:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.vmid = 9300

        def create(self) -> None:
            raise ProxmoxError("the test stops before installation")

        def transferred(self) -> int:
            return 0

        def destroy(self) -> None:
            if next(destroy_fails):
                raise ProxmoxError("the guest remains")

    monkeypatch.setattr(cluster, "Guest", FakeGuest)
    done: queue.Queue[cluster.Outcome] = queue.Queue()
    inflight: dict[str, cluster.Running] = {}
    job = cluster.Job("vm-lvm", Path("tests/fixtures/vm-lvm.toml"))
    nowhere = cast(Api, object())

    cluster.answer_once(
        done, nowhere, "infra-node1", job, "driver.iso", tmp_path, inflight, 9300
    )
    cluster.answer_once(
        done, nowhere, "infra-node1", job, "driver.iso", tmp_path, inflight, 9301
    )

    left_behind = done.get_nowait()
    removed = done.get_nowait()
    assert (left_behind.vmid, removed.vmid) == (9300, 9301)
    assert not left_behind.removed
    assert removed.removed


def test_a_worker_that_ends_without_reporting_becomes_an_error() -> None:
    """`answer_once` puts an outcome on the queue for everything a Python
    handler can see. A thread that ends any other way left its name in the
    running set for ever and the schedule never finished: one round sat idle
    for half an hour with an empty cluster and a job still queued."""
    import threading

    from tests.vm.cluster import _unanswered

    ended = threading.Thread(target=lambda: None)
    ended.start()
    ended.join()
    assert _unanswered({"vm-lvm": ended}, nothing_queued=True) == ["vm-lvm"]


def test_unknown_telemetry_does_not_make_a_quiet_guest_stuck(tmp_path: Path) -> None:
    """Three failed API reads were treated as three proofs that counters were flat."""
    from tests.vm.cluster import WATCH_STRIKES, Watchdog

    log = tmp_path / "quiet.log"
    log.write_bytes(b"")
    answers: list[int | None] = [None] * WATCH_STRIKES + [10] * (WATCH_STRIKES + 1)
    watch = Watchdog(log, lambda: answers.pop(0))
    unknown = [watch.moved() for _ in range(WATCH_STRIKES)]
    assert unknown == [True] * WATCH_STRIKES
    assert watch.strikes == 0 and not watch.stuck
    [watch.moved() for _ in range(WATCH_STRIKES + 1)]
    assert watch.stuck, "known flat counters must still trip the watchdog"


def test_preinstall_console_timeout_is_an_error_with_its_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live-medium timeout was recorded as an installer FAIL."""
    from typing import Any

    from tests.vm import cluster
    from tests.vm.console import ConsoleTimeout
    from tests.vm.proxmox import Api

    class FakeGuest:
        def __init__(self, *args: object, **kwargs: object) -> None:
            # `wait_for_network` derives this guest's static address from it.
            self.vmid = 9300

        def create(self) -> None:
            return None

        def start(self) -> None:
            return None

        def reset(self) -> None:
            return None

        def transferred(self) -> int:
            return 0

        def destroy(self) -> None:
            return None

    class Link:
        console: Any = object()

        def run(self, command: str, timeout: float = 120.0) -> None:
            return None

        def wait_for(
            self,
            command: str,
            timeout: float,
            idle: float = 0.0,
            watch: object | None = None,
        ) -> None:
            return None

    class Links:
        @classmethod
        def to(cls, guest: object, log: Path) -> Link:
            return Link()

    def timeout(*args: object, **kwargs: object) -> None:
        raise ConsoleTimeout("no live prompt")

    monkeypatch.setattr(cluster, "Guest", FakeGuest)
    monkeypatch.setattr(cluster, "Reconnecting", Links)
    monkeypatch.setattr(cluster, "append_to_cmdline", timeout)
    job = cluster.Job("vm-lvm", Path("tests/fixtures/vm-lvm.toml"))
    outcome = cluster.install_one(
        Api(host="nowhere.invalid"),
        "node",
        job,
        "driver.iso",
        tmp_path,
        vmid=9300,
        nonce="gi-phase",
        revision="revision-a",
    )
    assert (outcome.verdict, outcome.phase) == (
        cluster.Verdict.ERROR,
        cluster.Phase.BOOT_LIVE,
    )

    monkeypatch.setattr(cluster, "append_to_cmdline", lambda *args: None)
    monkeypatch.setattr(cluster, "reach_prompt", lambda *args: None)
    monkeypatch.setattr(cluster, "wait_for_network", lambda *args: None)
    monkeypatch.setattr(cluster, "stage_passphrases", lambda *args: None)
    monkeypatch.setattr(cluster, "collect", lambda *args: {"install.rc": b"1"})
    outcome = cluster.install_one(
        Api(host="nowhere.invalid"),
        "node",
        job,
        "driver.iso",
        tmp_path,
        vmid=9300,
        nonce="gi-phase",
        revision="revision-a",
    )
    assert (outcome.verdict, outcome.phase) == (
        cluster.Verdict.FAIL,
        cluster.Phase.INSTALL,
    )


def test_driver_identity_changes_with_the_packaged_bytes(tmp_path: Path) -> None:
    """Outcomes named only a commit even when the packaged dirty tree changed."""
    from tests.vm.cluster import revision_identity
    from tests.vm.driver import remote_name

    driver = tmp_path / "driver.iso"
    driver.write_bytes(b"first fixture tree")
    first = revision_identity(driver), remote_name(driver)
    driver.write_bytes(b"second fixture tree")
    second = revision_identity(driver), remote_name(driver)
    assert first != second


def test_a_revisionless_success_is_not_counted_as_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A green outcome with no installer identity proves no revision."""
    from typing import Any

    from tests.vm import cluster

    def revisionless(*args: Any, **kwargs: Any) -> list[cluster.Outcome]:
        return [cluster.Outcome("vm-lvm", cluster.Verdict.OK, 1.0)]

    monkeypatch.setattr(cluster, "run", revisionless)
    assert cluster.main(["vm-lvm"]) == 1

    def dirty(*args: Any, **kwargs: Any) -> list[cluster.Outcome]:
        return [
            cluster.Outcome(
                "vm-lvm",
                cluster.Verdict.OK,
                1.0,
                revision="abc dirty=1 driver-sha256=def",
            )
        ]

    monkeypatch.setattr(cluster, "run", dirty)
    assert cluster.main(["vm-lvm"]) == 1


def test_negative_cluster_limit_is_refused_before_building() -> None:
    """A negative limit sliced every slot away and waited forever."""
    from tests.vm.cluster import main

    with pytest.raises(SystemExit):
        main(["vm-lvm", "--limit", "-1"])


def test_zero_cluster_capacity_returns_an_error_after_a_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nonempty waiting queue with no running workers blocked on an empty queue forever."""
    from typing import Any
    import time as clock

    from tests.vm import cluster, workdir

    lab = tmp_path / "lab"
    run_dir = lab / "cluster"
    lab.mkdir()
    monkeypatch.setattr(workdir, "LAB_ROOT", lab)

    class Empty:
        def ours(self) -> list[tuple[str, int]]:
            return []

        def nodes(self) -> list[Any]:
            return []

        def remove_iso(self, node: str, name: str) -> str:
            return ""

    now = [0.0]
    monkeypatch.setattr(cluster, "Api", lambda: Empty())
    monkeypatch.setattr(cluster, "rewrite_fixtures", lambda jobs, into, region, sync: into)

    def build(path: Path, **kwargs: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"driver")
        return path

    monkeypatch.setattr(cluster, "build_driver", build)
    monkeypatch.setattr(
        cluster,
        "current_minimal",
        lambda: ("minimal-deadbeef.iso", ("https://invalid/iso",), "0" * 128),
    )
    monkeypatch.setattr(clock, "monotonic", lambda: now[0])
    monkeypatch.setattr(clock, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    outcome = cluster.run(
        [cluster.Job("vm-lvm", Path("tests/fixtures/vm-lvm.toml"))], run_dir
    )
    assert len(outcome) == 1
    assert outcome[0].verdict is cluster.Verdict.ERROR
    assert outcome[0].phase is cluster.Phase.SCHEDULE
    assert now[0] == cluster.CAPACITY_PATIENCE


def test_the_record_of_an_uploaded_medium_outlives_the_round_that_uploaded_it() -> None:
    """The ISO stays on the node between rounds, so a per-round work directory
    met its own upload as `already exists without its signed SHA-512 record`
    and the round could not start."""
    from tests.vm import cluster

    import inspect

    assert cluster.MEDIUM_TRUST.parent == cluster.WORKROOT
    # Deriving it from a round's work directory is what the parameter allowed.
    assert not inspect.signature(cluster.current_minimal).parameters
    assert "trust" not in inspect.signature(cluster.prepare).parameters or (
        list(inspect.signature(cluster.prepare).parameters)[5] == "trust"
    )


def test_reconcile_removes_only_an_expired_locally_leased_guest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unused cluster listing could neither recover an orphan nor distinguish campaigns."""
    from typing import Any

    from tests.vm import cluster
    from tests.vm.proxmox import ProxmoxNotFound, TAG

    expired = cluster.Lease("node", 9300, "gi-expired", 10)
    live = cluster.Lease("node", 9302, "gi-live", 20)
    expired_path = cluster._write_lease(tmp_path, expired)
    live_path = cluster._write_lease(tmp_path, live)
    cluster._write_lease(tmp_path, cluster.Lease("node", 9002, "gi-outside", 10))

    from tests.vm.proxmox import Api

    class Leased(Api):
        def __init__(self) -> None:
            self.absent = False
            self.deleted: list[int] = []

        def ours(self) -> list[tuple[str, int]]:
            return [("node", 9300), ("node", 9301), ("node", 9302)]

        def call(self, method: str, path: str, **form: Any) -> Any:
            vmid = int(path.split("/qemu/", 1)[1].split("/", 1)[0])
            if path.endswith("/config"):
                if self.absent:
                    raise ProxmoxNotFound("gone")
                return {"tags": f"{TAG};gi-expired"}
            if path.endswith("/status/current"):
                return {"status": "stopped"}
            if method == "DELETE":
                self.deleted.append(vmid)
                self.absent = True
                return "UPID:node:delete"
            raise AssertionError((method, path))

        def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
            return None

    api = Leased()
    monkeypatch.setattr(cluster, "_pid_alive", lambda pid: pid == 20)
    cluster.reconcile(api, tmp_path)
    assert api.deleted == [9300]
    assert not expired_path.exists()
    assert live_path.exists()
    assert not any(vmid == 9301 for vmid in api.deleted)


def test_workdirs_are_confined_before_any_vm_artifact_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repository paths, traversal and an escaping symlink reached mkdir and rmtree."""
    from tests.vm import cluster, netcheck, tui, workdir

    lab = tmp_path / "lab"
    inside = lab / "inside"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    escape = lab / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    inward = lab / "inward"
    inward.symlink_to(inside, target_is_directory=True)
    monkeypatch.setattr(workdir, "LAB_ROOT", lab)

    assert workdir.confined(inward) == inside.resolve()
    for path in (outside, escape, lab / ".." / "outside", Path.cwd()):
        with pytest.raises(workdir.WorkdirError):
            workdir.confined(path)

    monkeypatch.setattr(cluster, "Api", lambda: pytest.fail("cluster API contacted"))
    with pytest.raises(workdir.WorkdirError):
        cluster.run([], escape)
    with pytest.raises(workdir.WorkdirError):
        netcheck.run("ipv4", escape)
    assert tui.main(["--workdir", str(escape)]) == 1
    assert tui.main(
        ["--workdir", str(inside), "--lang", "../../../../../../outside"]
    ) == 1


def test_parallel_driver_builds_do_not_share_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each build deleted the other build's fixed `driver` staging directory."""
    from concurrent.futures import ThreadPoolExecutor
    from typing import Any
    import shutil
    import threading

    from tests.vm import driver

    first_fixtures = tmp_path / "first-fixtures"
    second_fixtures = tmp_path / "second-fixtures"
    first_fixtures.mkdir()
    second_fixtures.mkdir()
    (first_fixtures / "one.toml").write_text("one = true\n")
    (second_fixtures / "two.toml").write_text("two = true\n")
    barrier = threading.Barrier(2)
    original = shutil.copytree

    def synchronized(source: Path, target: Path, *args: Any, **kwargs: Any) -> Any:
        result = original(source, target, *args, **kwargs)
        if Path(source).name == "gentoo_install":
            barrier.wait(timeout=10.0)
        return result

    monkeypatch.setattr(shutil, "copytree", synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(driver.build, tmp_path / "first.iso", True, first_fixtures),
            pool.submit(driver.build, tmp_path / "second.iso", True, second_fixtures),
        ]
        built = [future.result(timeout=30.0) for future in futures]
    assert all(path.is_file() for path in built)
    assert driver.remote_name(built[0]) != driver.remote_name(built[1])


def test_a_medium_that_lacks_a_command_installs_it_before_the_installer_runs() -> None:
    """`bootstrap.sh` prints the package manager line and stops rather than
    running it, which is what an operator wants: the command has to be read
    before it is run. Every Debian run therefore ended at `missing commands:
    mkfs.vfat sgdisk`, exit 1, before anything was attempted.

    The medium declares what it lacks, and `reach_shell` installs it.
    """
    import inspect

    from tests.vm import run as vm_run
    from tests.vm.media import MEDIA

    assert MEDIA["debian"].prepare, "13.6.0 ships without mkfs.vfat and sgdisk"
    assert any("dosfstools" in one for one in MEDIA["debian"].prepare)
    assert any("gdisk" in one for one in MEDIA["debian"].prepare)
    assert MEDIA["fedora"].prepare, "43 ships without sgdisk"
    assert any("gdisk" in one for one in MEDIA["fedora"].prepare)
    # A medium that carries everything is sent nothing.
    assert MEDIA["official-minimal"].prepare == ()

    source = inspect.getsource(vm_run.reach_shell)
    assert "medium.prepare" in source


def test_a_schedule_that_ends_early_stops_the_guests_it_left_running() -> None:
    """The workers are daemon threads, so a scheduler that raised in
    `free_slots`, `prepare` or its own bookkeeping left its guests running and
    holding memory the cluster's own machines need."""
    import threading

    from tests.vm import cluster

    class Guest:
        def __init__(self) -> None:
            self.stopped = False
            self.removed = False

        def stop(self) -> None:
            self.stopped = True

        def destroy(self) -> None:
            self.removed = True

    quiet = cluster.Watchdog(log=Path("/nonexistent"), counters=lambda: None)
    guests = {name: Guest() for name in ("vm-lvm", "vm-xfs")}
    inflight = {
        name: cluster.Running(
            guest=guest,
            watch=quiet,
            reservation_bytes=cluster.GUEST_MEMORY_MIB * 1024**2,
        )
        for name, guest in guests.items()
    }

    joined: list[str] = []

    class Worker(threading.Thread):
        def __init__(self, name: str) -> None:
            super().__init__(daemon=True)
            self.label = name

        def join(self, timeout: float | None = None) -> None:
            joined.append(self.label)
            inflight.pop(self.label, None)

    running: dict[str, threading.Thread] = {name: Worker(name) for name in guests}
    cluster._abandon(inflight, running)

    assert all(guest.stopped for guest in guests.values()), guests
    assert sorted(joined) == ["vm-lvm", "vm-xfs"]
    assert not inflight, "each worker removes its own guest once the console closes"

    # A worker that never got there leaves its guest held, and after the join
    # there is nobody left to race: reporting it and walking away left guests
    # running on a cluster the harness does not own.
    wedged = Guest()
    held = {
        "vm-zfs": cluster.Running(
            guest=wedged,
            watch=quiet,
            reservation_bytes=cluster.GUEST_MEMORY_MIB * 1024**2,
        )
    }

    class Stuck(threading.Thread):
        def join(self, timeout: float | None = None) -> None:
            return

    cluster._abandon(held, {"vm-zfs": Stuck(daemon=True)})
    assert wedged.stopped and wedged.removed, "the closing path removes it"


def test_a_removed_guest_gives_its_vmid_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """`handed` only ever grew, so a campaign of more than a hundred jobs
    stopped at `no free vmid` with the whole range unoccupied. The outcome has
    to carry the VMID for the scheduler to know which one to release."""
    import queue
    from typing import cast

    from tests.vm import cluster

    monkeypatch.setattr(
        cluster,
        "install_one",
        lambda *args, **kwargs: cluster.Outcome("vm-lvm", cluster.Verdict.OK, 60.0),
    )
    done: queue.Queue[cluster.Outcome] = queue.Queue()
    from tests.vm.proxmox import Api

    nowhere = cast(Api, object())
    cluster.answer_once(
        done,
        nowhere,
        "infra-node1",
        cluster.Job("vm-lvm", Path("tests/fixtures/vm-lvm.toml")),
        "gi-driver.iso",
        Path("/nonexistent"),
        {},
        9307,
    )
    outcome = done.get_nowait()
    assert outcome.vmid == 9307, "the scheduler releases by VMID and cannot guess one"
    assert outcome.removed

    # A worker that died still names its VMID, or the range leaks one per crash.
    monkeypatch.setattr(
        cluster, "install_one", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("x"))
    )
    cluster.answer_once(
        done,
        nowhere,
        "infra-node1",
        cluster.Job("vm-xfs", Path("tests/fixtures/vm-xfs.toml")),
        "gi-driver.iso",
        Path("/nonexistent"),
        {},
        9308,
    )
    died = done.get_nowait()
    assert died.vmid == 9308 and not died.removed


def test_the_walk_does_not_escape_out_of_a_row_that_never_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escape on the main menu leaves the installer. A walk that pressed it
    after a press which opened nothing ended on its first row, and the review
    of that recording reported no findings because there was one screen."""
    from typing import Any, cast

    from tests.vm import tui

    class Console:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.asked = 0

        def run(self, line: str, timeout: float = 0.0) -> str:
            return ""

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            self.asked += 1
            return b"gentoo-install"

        def snapshot(self, seconds: float) -> bytes:
            # The same screen every time: the row never opens.
            return b"gentoo-install\r\nstorage\r\n"

    import time as clock

    monkeypatch.setattr(clock, "sleep", lambda seconds: None)
    monkeypatch.setattr(tui, "_ROWS", 3)
    console = Console()
    seen = tui.walk(cast("Any", console), "en")

    assert console.asked == 1, "the menu is waited for, not slept through"
    assert "\x1b" not in console.sent, console.sent
    assert any("opened nothing" in one.what for one in seen.findings), seen.findings


def test_a_screen_is_replayed_into_a_grid_rather_than_flattened() -> None:
    """curses draws by moving the cursor, not by ending lines. Stripping the
    escape codes ran the whole screen together and the width check reported a
    597-cell row that is 24 rows of at most 80."""
    from tests.vm.tui import cells, rendered

    screen = (
        b"\x1b[2J\x1b[1;1Hgentoo-install"
        b"\x1b[3;3Hkeyboard  us"
        b"\x1b[4;3Hlocale  zh_TW.UTF-8"
    )
    lines = rendered(screen)
    assert lines[0] == "gentoo-install"
    assert lines[2] == "  keyboard  us"
    assert lines[3] == "  locale  zh_TW.UTF-8"
    assert max(cells(one) for one in lines) <= 80

    # A row written past the edge is the finding this walk exists to make, and
    # flattening hid it among rows that were never that wide.
    wide = b"\x1b[2J\x1b[1;1H" + b"x" * 96 + b"\x1b[2;1Hshort"
    drawn = rendered(wide)
    assert cells(drawn[0]) == 96
    assert drawn[1] == "short"

    # Erase to end of line clears what a previous screen left behind.
    stale = b"\x1b[2J\x1b[1;1Hlonger text here\x1b[1;5H\x1b[Kx"
    assert rendered(stale)[0] == "longx"

    # curses moves a row at a time far more often than it jumps. Without the
    # relative moves the grid held the previous screen's rows, and a real
    # recording showed `Mirrors` and `Disk` on the disk submenu.
    moved = (
        b"\x1b[2J\x1b[1;1HDisk\r\x1b[2Bfirst\x1b[K\r\x1b[1Bsecond\x1b[K"
        b"\r\x1b[1B\x1b[Kthird\x1b[2Aover"
    )
    drawn = rendered(moved)
    assert drawn[0] == "Disk"
    # Moving up keeps the column, which is why `over` lands after `first`.
    assert drawn[2] == "firstover", drawn[2]
    assert drawn[3] == "second"
    assert drawn[4] == "third"


def test_a_worker_that_answered_and_exited_is_not_reported_twice() -> None:
    """The schedule reported `vm-xfs` twice: once with the error it raised,
    once as a worker that never reported."""
    import threading

    from tests.vm.cluster import _unanswered

    ended = threading.Thread(target=lambda: None)
    ended.start()
    ended.join()
    running = {"vm-xfs": ended}

    assert _unanswered(running, nothing_queued=False) == []
    # Nothing on the queue and a dead worker is the case this covers: the name
    # would otherwise stay in `running` and the schedule never end.
    assert _unanswered(running, nothing_queued=True) == ["vm-xfs"]

    alive = threading.Event()
    working = threading.Thread(target=alive.wait)
    working.start()
    try:
        assert _unanswered({"vm-lvm": working}, nothing_queued=True) == []
    finally:
        alive.set()
        working.join()


def test_no_fixture_asks_for_more_jobs_than_a_test_guest_has() -> None:
    """`btrfs-luks.toml` carried `-j32 -l32`, written for this workstation's
    thread count. In a guest with five processors `ninja -l32 -j32` thrashed
    and poppler died in its compile phase, which reads as an installer failure
    and is not one."""
    import re
    import tomllib

    from tests.vm.qemu import VmSpec

    allowed = 2 * VmSpec.cpus
    for fixture in sorted(Path("tests/fixtures").glob("*.toml")):
        settings = tomllib.loads(fixture.read_text())
        makeopts = str(settings.get("portage", {}).get("makeopts", ""))
        for jobs in re.findall(r"-[jl](\d+)", makeopts):
            assert int(jobs) <= allowed, f"{fixture.name} asks for {makeopts}"


def test_a_bios_guest_whose_grub_speaks_is_read_before_it_is_typed_at_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blind path exists for a GRUB that writes only to VGA. The official
    minimal ISO answers `starting serial terminal on interface serial0` and can
    be read; typing at it blind landed on no line and three fixtures ended at
    `the kernel never spoke after editing GRUB blind`."""
    from typing import Any, cast

    from tests.vm import cluster

    read: list[str] = []
    monkeypatch.setattr(cluster, "append_to_cmdline", lambda link, extra: read.append("read"))
    monkeypatch.setattr(
        cluster, "append_to_cmdline_blind", lambda guest, link, extra: read.append("blind")
    )
    cluster._edit_bios_cmdline(cast("Any", object()), cast("Any", object()))
    assert read == ["read"], "a menu that can be read is not typed at blind"

    # A GRUB that says nothing still gets the blind attempt, from a menu the
    # failed one did not touch.
    reset: list[str] = []

    class Guest:
        def reset(self) -> None:
            reset.append("reset")

    class Link:
        def reopen(self) -> None:
            reset.append("reopen")

    from tests.vm.console import ConsoleTimeout

    def silent(link: object, extra: str) -> None:
        raise ConsoleTimeout("never matched")

    read.clear()
    monkeypatch.setattr(cluster, "append_to_cmdline", silent)
    cluster._edit_bios_cmdline(cast("Any", Guest()), cast("Any", Link()))
    assert read == ["blind"]
    assert reset == ["reset", "reopen"]


def test_run_refuses_a_repeated_job_name_before_it_touches_the_cluster() -> None:
    """Every map in the scheduler is keyed by name, so a repeated one had the
    second guest overwrite the first's bookkeeping, one result end the loop
    while the other was still running, and `1/1 passed` printed for two jobs.
    The check lived in `main`, which left every other caller able to do it."""
    from tests.vm import cluster
    from tests.vm.proxmox import ProxmoxError

    twice = [
        cluster.Job("vm-lvm", Path("tests/fixtures/vm-lvm.toml")),
        cluster.Job("vm-lvm", Path("tests/fixtures/vm-lvm.toml")),
    ]
    with pytest.raises(ProxmoxError) as raised:
        cluster.run(twice, Path("/nonexistent"))
    assert "named more than once" in str(raised.value)
    assert "vm-lvm" in str(raised.value)


def test_a_term_signal_reaches_the_closing_path() -> None:
    """Python ends the process on SIGTERM without unwinding, so no `finally`
    runs: `kill` on the scheduler left eight guests on the cluster with the
    path that removes them never reached."""
    import signal as signals

    from tests.vm import cluster

    before = signals.getsignal(signals.SIGTERM)
    try:
        cluster._leave_on_a_signal()
        handler = signals.getsignal(signals.SIGTERM)
        assert callable(handler), handler
        with pytest.raises(KeyboardInterrupt):
            handler(signals.SIGTERM, None)
    finally:
        signals.signal(signals.SIGTERM, before)


class _PatternConsole:
    def __init__(self, output: bytes | BaseException, sent: list[str]) -> None:
        self.output = output
        self.sent = sent

    def send(self, line: str) -> None:
        self.sent.append(line)

    def send_raw(self, keys: str) -> None:
        pass

    def snapshot(self, seconds: float) -> bytes:
        return b""

    @property
    def closed(self) -> bool:
        return False

    def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
        from tests.vm.console import ConsoleTimeout

        if isinstance(self.output, BaseException):
            raise self.output
        found = re.search(pattern.encode(), self.output)
        if found is None:
            raise ConsoleTimeout(f"never matched {pattern!r}")
        return self.output[: found.end()]


def test_wait_for_accepts_a_live_prompt_after_reconnect() -> None:
    from tests.vm import cluster
    from tests.vm.console import ConsoleClosed

    sent: list[str] = []
    consoles = iter(
        [
            _PatternConsole(ConsoleClosed("termproxy disconnected"), sent),
            _PatternConsole(b"root@livecd ~ # ", sent),
        ]
    )
    link = cluster.Reconnecting(lambda: next(consoles))

    link.wait_for("install --config vm-raidz.toml", timeout=10.0)

    assert len([line for line in sent if line]) == 1
    assert sent[-1] == "", "reopen asks the live shell to draw a fresh prompt"


def test_wait_for_does_not_accept_the_original_command_echo() -> None:
    from tests.vm import cluster
    from tests.vm.console import ConsoleTimeout

    sent: list[str] = []
    echoed = (
        b"root@livecd ~ # printf 'MARK_%s_BEGIN\\n' 1; "
        b"install --config vm-raidz.toml; printf 'MARK_%s_DONE\\n' 1\r\n"
    )
    link = cluster.Reconnecting(lambda: _PatternConsole(echoed, sent))

    with pytest.raises(ConsoleTimeout, match="never matched"):
        link.wait_for("install --config vm-raidz.toml", timeout=10.0)


def test_wait_for_still_accepts_the_done_marker() -> None:
    from tests.vm import cluster

    sent: list[str] = []
    link = cluster.Reconnecting(
        lambda: _PatternConsole(b"install output\r\nMARK_1_DONE\r\n", sent)
    )

    link.wait_for("install --config vm-raidz.toml", timeout=10.0)

    assert len(sent) == 1


def test_reconnects_share_the_callers_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped console cannot turn one bounded wait into one per connection."""
    import time as clock

    from tests.vm import cluster
    from tests.vm.console import ConsoleClosed, ConsoleTimeout

    now = [0.0]
    opened = 0

    class Missing:
        def __init__(self, drops: bool) -> None:
            self.drops = drops

        def send(self, line: str) -> None:
            pass

        def send_raw(self, keys: str) -> None:
            pass

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            if self.drops:
                now[0] += min(4.0, timeout)
                raise ConsoleClosed("the guest closed the serial connection")
            now[0] += timeout
            raise ConsoleTimeout(f"never matched {pattern!r}")

    def open_console() -> Missing:
        nonlocal opened
        opened += 1
        return Missing(drops=opened < 3)

    monkeypatch.setattr(clock, "monotonic", lambda: now[0])
    link = cluster.Reconnecting(open_console, tries=3)
    with pytest.raises(ConsoleTimeout, match="never matched"):
        link.expect("installation complete", timeout=10.0)

    assert opened == 3, "both dropped connections are reopened"
    assert now[0] == pytest.approx(10.0)


def test_a_console_that_keeps_printing_is_not_ended_by_the_ceiling() -> None:
    """An install that prints for three hours is working; one that prints
    nothing for twenty minutes is not, and a single ceiling cannot tell them
    apart. Twelve guests of one round were ended at three hours with a
    repository listing still scrolling past."""
    import time as clock

    from tests.vm.console import ConsoleTimeout, SerialConsole

    class Printing(SerialConsole):
        """Prints a line on every read and never the marker."""

        quiet: bool = False

        def __init__(self) -> None:
            self._buffer = b""
            self.reads = 0

        def _read_once(self) -> None:
            self.reads += 1
            clock.sleep(0.01)
            if not self.quiet:
                self._buffer += b"dev-libs/one/Manifest\n"

    # Short enough to run, and the same shape: the idle window is a fifth of
    # the ceiling, and output arrives well inside it.
    chatty = Printing()
    started = clock.monotonic()
    with pytest.raises(ConsoleTimeout):
        chatty.expect("MARK_1_DONE", timeout=1.0, idle=0.2)
    assert clock.monotonic() - started >= 0.9, "output kept it alive to the ceiling"

    quiet = Printing()
    quiet.quiet = True
    started = clock.monotonic()
    with pytest.raises(ConsoleTimeout):
        quiet.expect("MARK_1_DONE", timeout=1.0, idle=0.2)
    waited = clock.monotonic() - started
    assert waited < 0.6, f"silence ended it at {waited:.2f}s, not at the ceiling"
