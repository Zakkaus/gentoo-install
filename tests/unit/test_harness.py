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
    swap install was told to install packages named `pvcreate` and `mkswap`,
    which no distribution has."""
    import subprocess

    from gentoo_install.exec import preflight

    root = Path(__file__).resolve().parents[2]
    source = (root / "bootstrap.sh").read_text()
    start = source.index("package_for()")
    body = source[start : source.index("\n}\n", start) + 3]
    script = body + '\npackage_for "$1" "$2"\n'

    wanted = {
        command
        for group in (
            preflight.ALWAYS,
            preflight.MENU_ONLY,
            *preflight.BY_FEATURE.values(),
            *preflight.EXTRA_FILESYSTEM_COMMANDS.values(),
        )
        for command in group
    }
    # These are the commands a distribution really does name its package after.
    # Commands a distribution really does name a package after. Alpine splits
    # util-linux into one package per tool, so each of those is its own name
    # there and nowhere else.
    itself = {"cryptsetup", "mdadm", "parted", "btrfs", "tar", "sgdisk", "zfs", "openssl"}
    split_on_alpine = {"mount", "umount", "lsblk", "blkid", "findmnt", "wipefs"}
    # Debian keeps mount and umount in a package of that name.
    named_that = {("mount", "debian"), ("umount", "debian")}
    wrong: list[str] = []
    for command in sorted(wanted - itself):
        for family in ("gentoo", "alpine", "debian", "arch", "fedora", "opensuse"):
            said = subprocess.run(
                ["sh", "-c", script, "_", command, family], capture_output=True, text=True
            ).stdout.strip()
            allowed = (family == "alpine" and command in split_on_alpine) or (
                command,
                family,
            ) in named_that
            if said == command and not allowed:
                wrong.append(f"{command}:{family}")
    assert not wrong, wrong


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
