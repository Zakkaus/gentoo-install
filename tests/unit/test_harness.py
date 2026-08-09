"""The harness has to report a failed install as a failed run.

It reads the installer's exit code off the result disk rather than from the
process it drove, so nothing else in the run can notice a failure for it.
"""

from __future__ import annotations

import re
from pathlib import Path

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
    from gentoo_install.model.parse import load

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
    from gentoo_install.model.parse import load

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
