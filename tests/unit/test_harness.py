# SPDX-License-Identifier: GPL-2.0-or-later
"""The harness has to report a failed install as a failed run.

It reads the installer's exit code off the result disk rather than from the
process it drove, so nothing else in the run can notice a failure for it.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from gentoo_install.model.config import InstallConfig

import pytest

from tests.vm.console import passphrase_settle
from tests.vm.proxmox import Traffic
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


def test_remote_unlock_failure_is_not_a_console_fallback() -> None:
    assert verdict({"remote-unlock.rc": b"1\n"}, None) == 1


def test_remote_unlock_replaces_fixture_values_without_mutating_fixture() -> None:
    from gentoo_install.exec.config import load
    from tests.vm.run import remote_config

    fixture = Path("tests/fixtures/zbm-unlock.toml")
    before = fixture.read_bytes()
    substituted = remote_config(load(fixture), "ssh-ed25519 AAAA harness")
    assert substituted.system.authorized_keys == ("ssh-ed25519 AAAA harness",)
    assert substituted.kernel.remote_unlock.address == ""
    assert fixture.read_bytes() == before


@pytest.mark.parametrize(
    ("fixture", "command", "proof"),
    [
        ("vm-unlock.toml", "unlock", None),
        (
            "zbm-unlock.toml",
            # The pools first: `zfs load-key -a` succeeds with nothing to do
            # when the pool is not imported, so the proof was the first thing
            # to notice and said only `dataset does not exist`.
            "echo pools=$(zpool list -H -o name | tr '\\n' ',') && zfs load-key -a",
            "zfs get -H -o value keystatus zpcala/ROOT/gentoo/root",
        ),
    ],
)
def test_remote_unlock_command_comes_from_fixture(
    fixture: str, command: str, proof: str | None
) -> None:
    from gentoo_install.exec.config import load
    from tests.vm.run import remote_unlock_commands

    commands = remote_unlock_commands(load(Path("tests/fixtures") / fixture))
    assert commands is not None
    actual_command, actual_proof = commands
    assert actual_command == command
    assert actual_proof == proof


def test_zfs_remote_unlock_proof_changes_when_pool_is_renamed() -> None:
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from gentoo_install.model.device import ZfsPool
    from tests.vm.run import remote_unlock_commands

    installation = load(Path("tests/fixtures/zbm-unlock.toml"))
    pool = installation.disk.graph.of_type(ZfsPool)[0]
    nodes = [
        replace(node, name="renamed") if node.id == pool.id and isinstance(node, ZfsPool) else node
        for node in installation.disk.graph.nodes.values()
    ]
    renamed = replace(installation, disk=replace(installation.disk, graph=type(installation.disk.graph)(nodes)))
    commands = remote_unlock_commands(renamed)
    assert commands is not None
    _, proof = commands
    assert proof is not None and "renamed/" in proof and "zpcala/" not in proof


def test_disabled_remote_unlock_produces_no_command() -> None:
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from tests.vm.run import remote_unlock_commands

    installation = load(Path("tests/fixtures/zfs-zbm.toml"))
    disabled = replace(
        installation,
        kernel=replace(installation.kernel, remote_unlock=replace(installation.kernel.remote_unlock, enabled=False)),
    )
    assert remote_unlock_commands(disabled) is None


def test_unlock_forward_is_only_added_when_remote_unlock_is_requested() -> None:
    from tests.vm.media import MEDIA
    from tests.vm.qemu import Vm, VmSpec

    ordinary = Vm(VmSpec(MEDIA["official-minimal"], Path("/tmp"), ssh_port=2200))._netdev()
    remote = Vm(
        VmSpec(MEDIA["official-minimal"], Path("/tmp"), ssh_port=2200, remote_unlock_port=2201)
    )._netdev()
    assert "hostfwd=tcp::2201-:2222" not in ordinary
    assert "hostfwd=tcp::2201-:2222" in remote


def test_every_configuration_the_campaign_names_exists() -> None:
    """A stage naming a fixture that was renamed fails half an hour in, after
    the medium has booted, rather than at the first line."""
    from tests.vm.campaign import STAGES

    root = Path(__file__).resolve().parents[1]
    for stage, runs in STAGES.items():
        for run in runs:
            assert (root / run.config).is_file(), f"{stage}: {run.config}"


#: Fixtures the local campaign does not run, and why. Named rather than
#: derived from the disk mode: a blanket exemption lets the next unrun fixture
#: in without anybody noticing, which is what this check exists to stop.
NOT_IN_THE_CAMPAIGN: Final[frozenset[str]] = frozenset(
    {
        # A share is a share of the disk, and every guest here is given the
        # same 40 GiB target, so this fixture would measure one capacity and
        # prove nothing about the arithmetic it exists for. What it does prove
        # is checked without a guest: `tests/golden/vm-shares.txt` carries the
        # resolved bytes, and `tests/unit/test_plan_disk.py` runs the same
        # configuration against two very different disks.
        "vm-shares.toml",
        # What the memory environment is asked to install once it comes up.
        # `tests/vm/ram.py` runs it against a cloud image rather than the
        # medium the campaign boots, because the machine under test has to
        # have a bootloader of its own to arm.
        "vm-ram.toml",
        # The only fixture that configures a static address, and the interface
        # it has to pin is the cluster's: a local guest presents `enp0s2` where
        # the cluster presents `ens18`, so `[Match] Name=ens18` matches nothing
        # and networkd applies no address. Measured 2026-08-24 on
        # `2c9728a81d97e`: the address, route and resolver checks all failed
        # while `network` passed, and every recorded pass of this fixture is a
        # cluster run.
        "static-ip.toml",
        # The dd runner generates its sources, streams each one from the driver
        # CD, and reads the target back; neither target can boot as a system.
        "vm-dd-raw.toml",
        "vm-dd-gz.toml",
    }
)


def test_no_local_run_configures_an_address_the_guest_cannot_match() -> None:
    """A static address needs the interface named, and the name is decided by
    the hypervisor's slot layout: `ens18` on the cluster, `enp0s2` under local
    qemu. A fixture pinning one of them is green on that runner and red on the
    other for a reason no check reports as an environment mismatch."""
    from gentoo_install.exec.config import load
    from tests.vm.campaign import STAGES

    scheduled = {Path(run.config).stem for runs in STAGES.values() for run in runs}
    configuring = {
        one.stem
        for one in sorted(Path("tests/fixtures").glob("*.toml"))
        if load(one).system.addresses
    }
    assert configuring, "a fixture with a static address is what this measures"
    assert not configuring & scheduled, sorted(configuring & scheduled)


def test_the_campaign_covers_every_vm_fixture() -> None:
    """A fixture nobody runs is a path nobody tests, and the point of the
    matrix is that the list cannot quietly fall behind."""
    from tests.vm.campaign import STAGES
    from tests.vm.cluster import MUST_DEGRADE
    from tests.vm.expectations import EXPECTATIONS

    root = Path(__file__).resolve().parents[1]
    named = {Path(run.config).name for runs in STAGES.values() for run in runs}
    available = {path.name for path in (root / "fixtures").glob("*.toml")}
    assert available - named == NOT_IN_THE_CAMPAIGN, sorted(
        (available - named) ^ NOT_IN_THE_CAMPAIGN
    )
    # Both runners can now tell its pass from an ordinary install: the cluster
    # reads the `degraded` event, the campaign reads the line it writes.
    assert "vm-binhost-fallback" in MUST_DEGRADE
    assert EXPECTATIONS["vm-binhost-fallback"].marker


def test_every_fixture_names_a_disk_the_harness_creates() -> None:
    """Three of them named `virtio-target` with no number, from before the
    harness numbered the serials, so preflight refused them twenty seconds in
    and they had never once installed anything.

    An image fixture is exempt: its `existing` node names the file the
    installer creates, and `validate()` refuses a `/dev/` selector there."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.config import DiskMode
    from gentoo_install.model.device import Existing

    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "fixtures").glob("*.toml")):
        installation = load(path)
        if installation.disk.mode is DiskMode.IMAGE:
            continue
        for disk in installation.disk.graph.of_type(Existing):
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

    # `boot_installed` keeps the medium's kernel and initramfs out of the
    # argv, so this gate holds whatever the operator has downloaded: the
    # release the pin names is rotated off the mirrors within weeks, and both
    # of these turned red on a machine whose ISO cache had been cleaned.
    spec = VmSpec(
        medium=MEDIA["official-minimal"],
        workdir=Path("/tmp"),
        # BIOS, because this asserts the argv prefix and UEFI would want the
        # machine's OVMF images, which a runner without a hypervisor lacks.
        firmware=Firmware.BIOS,
        ssh_port=2222,
        boot_installed=True,
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


def test_no_run_can_carry_a_weight_of_its_own_again() -> None:
    """Both runners must keep reading `sizing.compiles`, not a second copy.

    They did not: `Run` held a hand-written weight per row, nine fixtures were
    marked light here while the cluster derived heavy, and every one of the
    nine was a layout that compiles a ZFS module. A field is how that comes
    back, so the field set is what this holds.
    """
    import ast
    import dataclasses
    from pathlib import Path

    from tests.vm import campaign, sizing

    written = {field.name for field in dataclasses.fields(campaign.Run)}
    assert not written & {"weight", "cpus", "heavy", "compiles"}, written
    # And the derivation reads the shared rule rather than a local copy: the
    # import is what makes it the same rule, so the import is what is held.
    imported = {
        name.name
        for node in ast.walk(ast.parse(Path(campaign.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module == "sizing"
        for name in node.names
    }
    assert "compiles" in imported, imported
    # The cluster reads the same module, so naming it here is not a second
    # spelling of the rule.
    assert sizing.compiles.__module__ == "tests.vm.sizing"


def test_a_heavier_run_asks_for_the_cores_on_the_command_line() -> None:
    """The guest derives its MAKEOPTS from the vCPU count, so the weight has to
    reach `run.py` rather than only the scheduler."""
    from tests.vm.campaign import COMPILING_CPUS, Run

    # A fixture that compiles, rather than a hand-set weight: the cores are
    # derived from the configuration now, so a run that asks for them has to
    # be one that would.
    heavy = Run("fixtures/vm-desktop.toml")
    assert heavy.compiles, heavy.config
    argv = heavy.argv()
    assert "--cpus" in argv and argv[argv.index("--cpus") + 1] == str(COMPILING_CPUS)

    light = Run("fixtures/vm-binpkg.toml")
    assert not light.compiles, light.config
    assert "--cpus" not in light.argv()


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
        "gentoo": {"tar": "tar", "xz": "xz-utils", "gpg": "gnupg", "gpg-agent": "gnupg", "install": "coreutils", "sleep": "coreutils"},
        "alpine": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gpg-agent", "install": "coreutils", "sleep": "coreutils"},
        "debian": {"tar": "tar", "xz": "xz-utils", "gpg": "gnupg", "gpg-agent": "gpg-agent", "install": "coreutils", "sleep": "coreutils"},
        "ubuntu": {"tar": "tar", "xz": "xz-utils", "gpg": "gnupg", "gpg-agent": "gpg-agent", "install": "coreutils", "sleep": "coreutils"},
        "arch": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gnupg", "install": "coreutils", "sleep": "coreutils"},
        "fedora": {
            "tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gnupg2-gpg-agent", "install": "coreutils", "sleep": "coreutils",
        },
        "rhel": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gnupg2", "install": "coreutils", "sleep": "coreutils"},
        "centos": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gnupg2", "install": "coreutils", "sleep": "coreutils"},
        "suse": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gpg2", "install": "coreutils", "sleep": "coreutils"},
        "opensuse": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gpg2", "install": "coreutils", "sleep": "coreutils"},
        "opensuse-leap": {"tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gpg2", "install": "coreutils", "sleep": "coreutils"},
        "opensuse-tumbleweed": {
            "tar": "tar", "xz": "xz", "gpg": "gnupg", "gpg-agent": "gpg2", "install": "coreutils", "sleep": "coreutils",
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
        # Debian's package is called `dmsetup`; Alpine, Arch and the rpm
        # families call it `device-mapper` and Gentoo puts it in lvm2.
        ("dmsetup", "debian"),
        ("dmsetup", "ubuntu"),
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
    from tests.vm.installed import checks
    from gentoo_install.exec.config import load

    installation = load(Path("tests/fixtures/vm-desktop.toml"))
    asked = next(one.command for one in checks(installation) if one.name == "inputmethod")
    expected = str(ENVIRONMENT_FILE[installation.system.init])
    assert expected in asked
    # And nothing the plan stopped writing: a path left behind reads as
    # coverage while the check answers with nothing.
    named = {
        word
        for word in asked.replace(";", " ").split()
        if word.startswith("/etc/") and "fcitx5" not in word
    }
    assert named == {expected}, named


def test_an_installed_check_matches_the_value_and_not_the_text_around_it() -> None:
    """Four checks accepted text that carried the answer without the machine
    having produced it: `Gentoo` anywhere in `os-release`, `UUID=` anywhere in
    `fstab` when every layout writes one for the esp, `/boot/` for any file at
    all while `uname -r` was read and never compared, and a bare hostname that
    is a substring of openrc's `hostname="name"`.

    The passing text is copied from guests this campaign ran.
    """
    import re

    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    def pattern(fixture: str, name: str) -> str:
        return next(
            one.pattern for one in checks(load(Path(f"tests/fixtures/{fixture}.toml")))
            if one.name == name
        )

    kernel = pattern("vm-binpkg", "kernel")
    assert re.search(kernel, "\n6.18.43-gentoo-dist-bin\n/boot/kernel-6.18.43-gentoo-dist-bin\n")
    assert re.search(
        kernel,
        "\n6.18.43-gentoo-dist-bin\n/boot/3c3b/6.18.43-gentoo-dist-bin/linux\n"
        "/boot/loader/loader.conf\n",
    )
    # The kernel that is installed is not the kernel that booted.
    assert not re.search(kernel, "\n6.18.43-gentoo-dist-bin\n/boot/kernel-6.17.1-gentoo\n")

    fstab = pattern("vm-binpkg", "fstab")
    assert re.search(fstab, "UUID=8979\t/\text4\tdefaults\t0\t1\nUUID=0AB2\t/efi\tvfat\t0\t2\n")
    # The esp by UUID and the root still named by device.
    assert not re.search(fstab, "/dev/vda2\t/\text4\tdefaults\t0\t1\nUUID=0AB2\t/efi\tvfat\t0\t2\n")

    # Nothing asked for the timezone at all until this: a machine installed in
    # the wrong zone passed every check the campaign made.
    zone = pattern("vm-cjk-kernel", "timezone")
    assert re.search(zone, "/usr/share/zoneinfo/UTC\nUTC\n")
    assert not re.search(zone, "/usr/share/zoneinfo/Asia/Taipei\nAsia/Taipei\n")
    assert not re.search(zone, "readlink: /etc/localtime: No such file or directory\n")
    # A different zone that carries this one's name: `UTC-8` and `Etc/GMT+8`
    # are zones of their own, and an unanchored pattern took either for `UTC`.
    assert not re.search(zone, "/usr/share/zoneinfo/UTC-8\nUTC-8\n")
    taipei = pattern("btrfs-luks", "timezone")
    assert re.search(taipei, "/usr/share/zoneinfo/Asia/Taipei\nAsia/Taipei\n")
    assert not re.search(taipei, "/usr/share/zoneinfo/UTC\nUTC\n")

    esp = pattern("vm-binpkg", "esp")
    assert re.search(esp, "/efi /dev/vda1 vfat\n")
    # Mounted there, but not something the firmware can read.
    assert not re.search(esp, "/efi /dev/vda1 ext4\n")
    assert not re.search(esp, "findmnt: /efi: not a mountpoint\n")

    release = pattern("vm-binpkg", "os-release")
    assert re.search(release, "NAME=Gentoo\nID='gentoo'\n")
    assert not re.search(release, "NAME=\"Ubuntu\"\nID=ubuntu\nID_LIKE=Gentoo\n")

    # openrc keeps the name in a shell assignment, systemd in a bare line.
    openrc = pattern("openrc-sdboot", "hostname")
    assert re.search(openrc, 'hostname="openrcsdbox"\n')
    assert not re.search(openrc, 'hostname="openrcsdboxlonger"\n')
    systemd = pattern("vm-binpkg", "hostname")
    assert re.search(systemd, "vmtest\n")
    assert not re.search(systemd, "vmtestlonger\n")


def test_every_installed_check_is_line_bounded_or_names_why_it_is_not() -> None:
    """A bare pattern accepts its token from any longer line, so each check
    either matches one complete line or records the multi-line relation it needs.
    """
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from gentoo_install.model.config import DiskMode, InitSystem
    from tests.vm.installed import checks

    configurations = [
        load(path)
        for path in sorted(Path("tests/fixtures").glob("*.toml"))
        if load(path).disk.mode is not DiskMode.DD
    ]
    greetd = load(Path("tests/fixtures/vm-greetd.toml"))
    configurations.append(replace(greetd, system=replace(greetd.system, init=InitSystem.OPENRC)))
    exceptions = {
        (check.name, check.line_boundary_exception)
        for installation in configurations
        for check in checks(installation)
        if check.line_boundary_exception is not None
    }
    assert exceptions == {
        ("mounts", "requires every configured mount line"),
        ("kernel", "joins a running release to a boot path"),
        ("greeter service", "requires enabled and active state lines"),
        ("greeter service", "requires default service and process lines"),
        ("greetd config", "requires constraints across greetd config lines"),
        ("inputmethod", "requires the binary and every environment line"),
        ("authorized keys root", "requires every fingerprint and both modes"),
        ("datasets", "requires one line per configured dataset"),
        ("authorized keys zakk", "requires every fingerprint and both modes"),
    }
    for installation in configurations:
        for check in checks(installation):
            if check.line_boundary_exception is not None:
                continue
            assert check.pattern.startswith("(?m)^"), check
            assert check.pattern.endswith("$"), check


def test_installed_checks_reject_embedded_or_stale_values() -> None:
    """Each control matches its former pattern, rejects the wrong machine now,
    and preserves a complete matching output.
    """
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    def pattern(installation: InstallConfig, name: str) -> str:
        return next(check.pattern for check in checks(installation) if check.name == name)

    systemd = load(Path("tests/fixtures/vm-binpkg.toml"))
    chinese_locale = replace(systemd, system=replace(systemd.system, locale="zh_CN.UTF-8"))
    static_ip = load(Path("tests/fixtures/static-ip.toml"))
    configured_ip = replace(
        static_ip,
        system=replace(
            static_ip.system,
            addresses=("192.0.2.1",),
            gateways=("192.0.2.1",),
        ),
    )
    cases = (
        (
            "resolver",
            r"RESOLVCONF-OK",
            pattern(systemd, "resolver"),
            "/run/RESOLVCONF-OK-missing\nRESOLVCONF-EMPTY\n",
            "/run/systemd/resolve/stub-resolv.conf\nRESOLVCONF-OK\n",
        ),
        (
            "locale",
            r"LANG=zh_CN.UTF-8",
            pattern(chinese_locale, "locale"),
            "LANG=zh_CN.UTF-8-broken\n",
            "LANG=zh_CN.UTF-8\nLANGUAGE=zh_CN\n",
        ),
        (
            "timezone",
            r"(?m)^(?:/usr/share/zoneinfo/)?UTC$",
            pattern(systemd, "timezone"),
            "/usr/share/zoneinfo/Asia/Tokyo\nUTC\n",
            "/usr/share/zoneinfo/UTC\nUTC\n",
        ),
        (
            "address",
            r"192\.0\.2\.1",
            pattern(configured_ip, "address 192.0.2.1"),
            "2: ens18    inet 192.0.2.10/24 brd 192.0.2.255 scope global ens18\n",
            "2: ens18    inet 192.0.2.1/24 brd 192.0.2.255 scope global ens18\n",
        ),
        (
            "default route",
            r"default via 192\.0\.2\.1",
            pattern(configured_ip, "default route 192.0.2.1"),
            "default via 192.0.2.10 dev ens18 proto static\n",
            "default via 192.0.2.1 dev ens18 proto static\n",
        ),
        (
            "sshd",
            r"(?m)^enabled\b",
            pattern(systemd, "sshd"),
            "enabled-runtime\n",
            "enabled\n",
        ),
    )
    for name, legacy, current, wrong, correct in cases:
        assert re.search(legacy, wrong), f"{name}: legacy pattern missed its control"
        assert not re.search(current, wrong), f"{name}: accepted the wrong machine"
        assert re.search(current, correct), f"{name}: rejected complete output"


def test_an_image_install_is_judged_by_the_image_it_wrote() -> None:
    """The product is a file on the scratch filesystem this runner mounts, and
    nothing here can boot a file on the guest's own disk. `--and-boot` booted
    the target disk, which in this mode carries that scratch filesystem and no
    installed system: the check ended at `UEFI Interactive Shell` with
    `install.rc` holding 0 and the install correct.
    """
    import re

    from gentoo_install.exec.config import load
    from tests.vm import run as runner

    installation = load(Path("tests/fixtures/vm-image.toml"))
    expectation = runner._from_config(load(Path("tests/fixtures/vm-image.toml")))
    assert [name for name, _ in expectation] == ["image"]
    pattern = expectation[0][1]

    # What `lsblk` prints for the loop device the image was attached to.
    assert re.search(pattern, "loop0\nloop0p1 vfat\nloop0p2 ext4\n", re.MULTILINE)
    # A partition table with no root filesystem, and what losetup says when
    # the image was never written.
    assert not re.search(pattern, "loop0\nloop0p1 vfat\n", re.MULTILINE)
    assert not re.search(
        pattern,
        "losetup: /mnt/scratch/target.raw: failed to set up loop device\n",
        re.MULTILINE,
    )

    class Console:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run(self, command: str, timeout: float = 0.0) -> None:
            self.commands.append(command)

    console = Console()
    runner.check_image(cast(Any, console), installation)
    said = " ".join(console.commands)
    assert f"losetup -Pf --show {installation.disk.image}" in said
    assert "losetup -d" in said, "the loop device has to be given back"

    # And an ordinary install asks for nothing: there is no image to read.
    plain = Console()
    runner.check_image(cast(Any, plain), load(Path("tests/fixtures/vm-binpkg.toml")))
    assert plain.commands == []

    # And the verdict really reads it. This is the answer a guest of this
    # campaign gave, copied from `image.txt`.
    written = Path("tests/fixtures/vm-image.toml")
    real = b"loop1   \nloop1p1 vfat\nloop1p2 ext4\n"
    import contextlib
    from io import StringIO

    printed = StringIO()
    with contextlib.redirect_stdout(printed):
        assert runner.check_expected({"image.txt": real, "install.rc": b"0\n"}, load(written)) == 0
    # What was read, not what a different mode reads: nothing booted here.
    assert "the image holds every filesystem" in printed.getvalue(), printed.getvalue()
    assert "booted" not in printed.getvalue(), printed.getvalue()
    # The esp alone: the install stopped before it made the root filesystem.
    assert runner.check_expected({"image.txt": b"loop1\nloop1p1 vfat\n"}, load(written)) != 0
    # And nothing collected at all, which is how the check stayed inert.
    assert runner.check_expected({}, load(written)) != 0

    # And the install phase really asks for that verdict: `report` was called
    # with no assertions there, so `image.txt` was collected, printed, and
    # never compared to anything.
    assert runner._reads_an_image("fixtures/vm-image.toml", False)
    assert not runner._reads_an_image("fixtures/vm-image.toml", True)
    assert not runner._reads_an_image("fixtures/vm-binpkg.toml", False)
    assert not runner._reads_an_image(None, False)

    import inspect

    # `_install_and_check`, which is where the work is: `_perform` is the
    # wrapper that stands a fixture's proxy up around it.
    wiring = inspect.getsource(runner._install_and_check)
    assert "_reads_an_image(args.install, args.dry_run)" in wiring


def test_every_input_framework_has_a_binary_to_ask_for() -> None:
    """The check asks `command -v <binary>`, and the map from framework to
    binary is a second table beside the catalog's own `input_framework`. A
    framework with no entry raises `KeyError` while the checks are derived,
    which ends the run before the machine is asked anything."""
    from gentoo_install.data import load_catalog
    from tests.vm.installed import INPUT_METHOD_BINARIES

    frameworks = {
        group.input_framework
        for group in load_catalog().values()
        if group.input_method and group.input_framework
    }
    assert frameworks, "the catalog names no input framework at all"
    assert frameworks <= set(INPUT_METHOD_BINARIES), frameworks - set(
        INPUT_METHOD_BINARIES
    )


def test_an_openrc_machine_is_asked_about_greetd_the_way_openrc_answers() -> None:
    """`systemctl` is not on that machine, so the systemd wording would answer
    nothing and the check would read as a failed greeter. No fixture installs
    greetd under OpenRC yet, so the configuration is built here."""
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from gentoo_install.model.config import InitSystem
    from tests.vm.installed import checks

    installation = load(Path("tests/fixtures/vm-greetd.toml"))
    under_openrc = replace(
        installation,
        system=replace(installation.system, init=InitSystem.OPENRC),
    )

    asked = next(
        one for one in checks(under_openrc) if one.name == "greeter service"
    )

    assert "systemctl" not in asked.command, asked.command
    assert "rc-update" in asked.command, asked.command
    # A running greeter, not just an enabled one: the pid comes from the
    # machine and cannot be echoed by the command that asks for it.
    assert "pgrep" in asked.command, asked.command
    assert re.search(asked.pattern, "display-manager | default\n1234\n")
    assert not re.search(asked.pattern, "display-manager | default\n")


def test_greetd_service_check_reads_systemd_state() -> None:
    from gentoo_install.exec.config import load
    from tests.vm.installed import InstalledCheck, checks

    installation = load(Path("tests/fixtures/vm-greetd.toml"))
    actual = [one for one in checks(installation) if one.name == "greeter service"]
    assert actual == [
        InstalledCheck(
            "greeter service",
            "systemctl is-enabled greetd.service; systemctl is-active display-manager.service",
            r"(?m)^enabled$\n^active$",
            "requires enabled and active state lines",
        )
    ]
    assert re.search(actual[0].pattern, "enabled\nactive\n")
    assert not re.search(actual[0].pattern, "enabled\ninactive\n")


def test_greetd_config_check_requires_tuigreet_session_directories() -> None:
    from gentoo_install.exec.config import load
    from tests.vm.installed import InstalledCheck, checks

    installation = load(Path("tests/fixtures/vm-greetd.toml"))
    actual = [one for one in checks(installation) if one.name == "greetd config"]
    assert actual == [
        InstalledCheck(
            "greetd config",
            "cat /etc/greetd/config.toml",
            r"(?ms)\A(?!.*^\s*command\s*=\s*\"agreety)"
            r"(?=.*^command = \"tuigreet .*--sessions /usr/share/wayland-sessions"
            r" --xsessions /usr/share/xsessions\"$)",
            "requires constraints across greetd config lines",
        )
    ]
    configured = (
        "[default_session]\n"
        'command = "tuigreet --sessions /usr/share/wayland-sessions'
        ' --xsessions /usr/share/xsessions"\n'
    )
    assert re.search(actual[0].pattern, configured)
    # The comment the ebuild's file carries above the command, which this test
    # used to require a failure for and which no machine is without.
    assert re.search(actual[0].pattern, f"# `agreety` is the bundled\n{configured}")
    assert not re.search(actual[0].pattern, f'command = "agreety --cmd /bin/sh"\n{configured}')
    assert not re.search(actual[0].pattern, 'command = "tuigreet"\n')


def test_ibus_check_reads_its_binary_and_session_environment() -> None:
    from gentoo_install.exec.config import load
    from tests.vm.installed import InstalledCheck, checks

    installation = load(Path("tests/fixtures/vm-greetd.toml"))
    actual = [one for one in checks(installation) if one.name == "inputmethod"]
    assert actual == [
        InstalledCheck(
            "inputmethod",
            "command -v ibus-daemon; cat /etc/environment",
            r"(?ms)(?=.*^/usr/bin/ibus\-daemon$)(?=.*^XMODIFIERS=@im=ibus$)"
            r"(?=.*^GTK_IM_MODULE=ibus$)(?=.*^QT_IM_MODULE=ibus$)",
            "requires the binary and every environment line",
        )
    ]
    configured = (
        "/usr/bin/ibus-daemon\n"
        "XMODIFIERS=@im=ibus\n"
        "GTK_IM_MODULE=ibus\n"
        "QT_IM_MODULE=ibus\n"
    )
    assert re.search(actual[0].pattern, configured)
    assert not re.search(actual[0].pattern, configured.replace("QT_IM_MODULE=ibus\n", ""))


def test_greetd_checks_are_not_requested_without_greetd() -> None:
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    selected = load(Path("tests/fixtures/vm-greetd.toml"))
    installation = replace(
        selected, packages=replace(selected.packages, display_manager="lightdm")
    )
    named = [one.name for one in checks(installation)]
    assert "greetd config" not in named, named
    # The service check is every display manager's, so lightdm keeps it.
    assert "greeter service" in named, named


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
        medium=MEDIA["official-minimal"],
        workdir=Path("/tmp"),
        firmware=Firmware.BIOS,
        boot_installed=True,
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

    from gentoo_install.model.config import DiskMode
    from gentoo_install.exec.config import load
    from tests.vm.run import INSTALLED_PASSWORD

    # `openssl passwd`, not `crypt`: the module left the standard library in
    # 3.13 and openssl is a tool the installer already requires.
    for fixture in sorted(Path("tests/fixtures").glob("*.toml")):
        installation = load(fixture)
        if installation.disk.mode is DiskMode.DD:
            continue
        hashed = installation.system.root_password_hash
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
        assert re.search(said["hostname"], host) or re.search(said["hostname"], f'hostname="{host}"'), name
        assert re.search(said["root filesystem"], filesystem), name
        assert re.search(said["init"], init), name
        assert re.search(said["locale"], f"LANG={locale}"), name


def test_every_fixture_has_a_boot_check_that_can_fail() -> None:
    """A fixture whose checks are all empty strings is one the boot pass
    cannot fail, which is how the empty ones went unnoticed."""
    from pathlib import Path

    from gentoo_install.model.config import DiskMode
    from gentoo_install.exec.config import load
    from tests.vm.cluster import _asked_for

    for fixture in sorted(Path("tests/fixtures").glob("*.toml")):
        installation = load(fixture)
        if installation.disk.mode is DiskMode.DD:
            continue
        checks = _asked_for(installation)
        empty = [one for one, _, value in checks if not value]
        assert not empty, f"{fixture.name}: {empty}"
        assert len(checks) >= 4, f"{fixture.name}: {checks}"


def test_local_and_cluster_use_the_same_installed_contract() -> None:
    """Both transport adapters must derive checks from one specification.

    Except where there is no installed system to ask: `dd` writes an image
    onto a disk and `image` writes one into a file, and the cluster refuses
    an image fixture at dispatch because it has no scratch filesystem to put
    it on. Their local contract is the product, not a booted machine.
    """
    from gentoo_install.model.config import DiskMode
    from tests.vm.cluster import _asked_for
    from tests.vm.installed import checks
    from tests.vm.run import _from_config
    from gentoo_install.exec.config import load

    without_a_machine = {DiskMode.DD, DiskMode.IMAGE}
    covered = {
        load(path).disk.mode
        for path in Path("tests/fixtures").glob("*.toml")
    }
    assert without_a_machine <= covered, "every exempted mode needs a fixture"

    for path in sorted(Path("tests/fixtures").glob("*.toml")):
        installation = load(path)
        if installation.disk.mode in without_a_machine:
            continue
        expected = [(one.name, one.pattern) for one in checks(installation)]
        assert _from_config(load(path)) == expected
        assert [(name, pattern) for name, _, pattern in _asked_for(installation)] == expected


def test_dd_runner_selects_raw_and_gzip_sources() -> None:
    """The end-to-end runner must stream every reader format it says it checks."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.config import DiskMode, ImageFormat
    from tests.vm import dd

    expected = {
        "vm-dd-raw.toml": (ImageFormat.RAW, f"/mnt/driver/fixtures/{dd.RAW_SOURCE_NAME}"),
        "vm-dd-gz.toml": (ImageFormat.GZIP, f"/mnt/driver/fixtures/{dd.GZIP_SOURCE_NAME}"),
    }
    assert {Path(selected.fixture).name for selected in dd.INPUTS} == set(expected)
    for selected in dd.INPUTS:
        name = Path(selected.fixture).name
        source_format, source = expected[name]
        installation = load(Path("tests") / selected.fixture)
        assert installation.disk.mode is DiskMode.DD
        assert installation.disk.source_format is source_format
        assert installation.disk.source == source
        assert installation.disk.destination == "/dev/disk/by-id/virtio-target0"


def test_dd_runner_builds_a_unique_raw_source_and_its_gzip_stream(tmp_path: Path) -> None:
    """A stale blank disk must not be indistinguishable from this run's source."""
    import gzip

    from tests.vm import dd

    sources = dd.build_sources(tmp_path)
    raw = sources.raw.read_bytes()

    assert len(raw) == dd.SOURCE_BYTES
    assert raw.startswith(sources.marker)
    assert raw != b"\0" * dd.SOURCE_BYTES
    with gzip.open(sources.gzip, "rb") as compressed:
        assert compressed.read() == raw
    staged = dd.stage_sources(tmp_path / "driver", sources)
    assert (staged / dd.RAW_SOURCE_NAME).read_bytes() == raw
    with gzip.open(staged / dd.GZIP_SOURCE_NAME, "rb") as compressed:
        assert compressed.read() == raw


def test_dd_runner_reads_the_marker_bounded_installer_status() -> None:
    """A status check must not match the shell echo of its own command."""
    from typing import Any, cast

    from tests.vm import dd

    class Output:
        def __init__(self, status: bytes) -> None:
            self.status = status
            self.commands: list[str] = []

        def expect_output(self, command: str, timeout: float) -> bytes:
            self.commands.append(command)
            return self.status

    successful = Output(b"0\n")
    dd.require_install_success(cast(Any, successful), "raw")
    assert successful.commands == [f"cat {dd.INSTALL_RESULT}"]

    with pytest.raises(RuntimeError, match="gz installer exited"):
        dd.require_install_success(cast(Any, Output(b"1\n")), "gz")


def test_dd_runner_rejects_a_target_byte_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The target comparison fails instead of trusting the dd process status."""
    import subprocess

    from tests.vm import dd

    calls: list[list[str]] = []

    def identical(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Images are identical.\n", stderr="")

    monkeypatch.setattr("tests.vm.dd.subprocess.run", identical)
    dd.target_matches(Path("source.raw"), Path("target.qcow2"), "raw")
    assert calls == [
        [
            "qemu-img",
            "compare",
            "-f",
            "raw",
            "-F",
            "qcow2",
            "source.raw",
            "target.qcow2",
        ]
    ]

    def different(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="byte 512 differs\n", stderr="")

    monkeypatch.setattr("tests.vm.dd.subprocess.run", different)
    with pytest.raises(RuntimeError, match="byte 512 differs"):
        dd.target_matches(Path("source.raw"), Path("target.qcow2"), "raw")


def test_a_single_runner_check_cannot_be_added_without_breaking_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner-local extra check must be visible as a contract mismatch."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster, run
    from tests.vm.installed import checks

    path = Path("tests/fixtures/ext4-bios.toml")
    installation = load(path)
    original = cluster._asked_for
    monkeypatch.setattr(
        cluster,
        "_asked_for",
        lambda config: [*original(config), ("sabotage", "true", "^true$")],
    )
    assert [(name, pattern) for name, pattern in run._from_config(load(path))] != [
        (name, pattern) for name, _, pattern in cluster._asked_for(installation)
    ]


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
            cluster.Watchdog(Path(f"/nonexistent/{name}.log"), lambda: Traffic(0, 0, 0.0)),
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


def test_console_buffer_keeps_the_tail_of_long_output() -> None:
    from io import BytesIO

    from tests.vm.console import ConsoleTimeout, SerialConsole

    class Channel:
        closed = False

        def __init__(self) -> None:
            self.chunks = [b"old output", b"current failure"]

        def recv(self, size: int) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

        def sendall(self, data: bytes) -> None:
            return None

        def close(self) -> None:
            return None

    console = SerialConsole(Channel(), BytesIO(), buffer_limit=len(b"current failure"))
    with pytest.raises(ConsoleTimeout, match="current failure"):
        console.expect(r"never appears", timeout=0.1)


def test_local_and_cluster_consoles_frame_commands_identically() -> None:
    from tests.vm.cluster import Reconnecting
    from tests.vm.console import SerialConsole

    class Channel:
        closed = False

        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def recv(self, size: int) -> bytes:
            return b"MARK_1_DONE\n"

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def close(self) -> None:
            return None

    local_channel = Channel()
    SerialConsole(local_channel, BytesIO()).run("printf output")
    cluster_lines: list[str] = []

    class ClusterConsole:
        def send(self, line: str) -> None:
            cluster_lines.append(line)

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            return b"MARK_1_DONE\n"

    Reconnecting(lambda: cast(Any, ClusterConsole())).run("printf output")
    expected = (
        "printf 'MARK_%s_BEGIN\\n' 1; printf output; "
        "printf 'MARK_%s_DONE\\n' 1"
    )
    assert local_channel.sent == [expected.encode() + b"\n"]
    assert cluster_lines == [expected]


def test_result_archive_rejects_data_above_its_bound() -> None:
    from tests.vm.results import RESULT_BUFFER_BYTES, ResultError, read_console

    said = b"x" * (RESULT_BUFFER_BYTES + 1)
    with pytest.raises(ResultError, match="size limit"):
        read_console(said)


def test_unknown_telemetry_does_not_make_a_quiet_guest_stuck(tmp_path: Path) -> None:
    """Three failed API reads were treated as three proofs that counters were flat."""
    from tests.vm.cluster import WATCH_STRIKES, Watchdog

    log = tmp_path / "quiet.log"
    log.write_bytes(b"")
    answers: list[Traffic | None] = [None] * WATCH_STRIKES + [
        Traffic(10, 0, 0.0)
    ] * (WATCH_STRIKES + 1)
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
            repeatable: bool = False,
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


def test_campaign_driver_paths_are_content_addressed(tmp_path: Path) -> None:
    """Two revisions retain separate images in one campaign work directory."""
    from tests.vm.cluster import retain_driver
    from tests.vm.driver import remote_name

    first = tmp_path / "first.iso"
    second = tmp_path / "second.iso"
    first.write_bytes(b"revision-a")
    second.write_bytes(b"revision-b")

    first_path = retain_driver(tmp_path, first)
    second_path = retain_driver(tmp_path, second)

    assert first_path != second_path
    assert first_path.name == remote_name(first_path)
    assert second_path.name == remote_name(second_path)
    assert first_path.read_bytes() == b"revision-a"
    assert second_path.read_bytes() == b"revision-b"


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


@pytest.mark.lab
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

        def call(self, method: str, path: str, **form: Any) -> Any:
            # The startup orphan report asks; an empty cluster has none.
            return []

        def remove_iso(self, node: str, name: str) -> str:
            return ""

    now = [0.0]
    monkeypatch.setattr(cluster, "Api", lambda: Empty())
    monkeypatch.setattr(
        cluster,
        "rewrite_fixtures",
        lambda jobs, into, region, sync, public_key="", site="", unlock_addresses=None, distfiles="", binhost="": into,
    )

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
            if path.startswith("/cluster/resources"):
                # The startup orphan report asks; this world has no leftovers.
                return []
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


def test_a_vmid_a_live_campaign_reused_leaves_the_lease_behind_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One schedule died holding 9301; a second one started, was given that
    VMID, and the third one refused to remove a guest whose nonce is not its
    own — correctly, and then ended its own run with that exception. A lease
    naming a guest somebody else now holds is a stale lease."""
    from typing import Any

    from tests.vm import cluster
    from tests.vm.proxmox import Api, TAG

    stale = cluster.Lease("node", 9301, "gi-dead-campaign", 10)
    stale_path = cluster._write_lease(tmp_path, stale)

    class Reused(Api):
        def __init__(self) -> None:
            self.deleted: list[int] = []

        def ours(self) -> list[tuple[str, int]]:
            return [("node", 9301)]

        def call(self, method: str, path: str, **form: Any) -> Any:
            if path.endswith("/config"):
                return {"tags": f"{TAG};gi-the-live-campaign"}
            if method == "DELETE":
                self.deleted.append(9301)
                return "UPID:node:delete"
            raise AssertionError((method, path))

    api = Reused()
    monkeypatch.setattr(cluster, "_pid_alive", lambda pid: False)
    cluster.reconcile(api, tmp_path)

    assert api.deleted == [], api.deleted
    assert not stale_path.exists()


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


@pytest.mark.lab
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


def test_scheduler_workers_are_owned_by_the_scheduler() -> None:
    """No guest outlives the process. The worker's own `finally` cannot carry
    that, because a daemon thread wedged on a console is cut short at
    interpreter shutdown, so the closing path removes every guest the schedule
    built rather than only the ones a job still counts as running."""
    from tests.vm import cluster

    class Machine:
        def __init__(self) -> None:
            self.removed = False

        def stop(self) -> None:
            pass

        def destroy(self, patience: float = 0.0) -> None:
            self.removed = True

    finished, wedged = Machine(), Machine()
    jobs = {
        name: cluster.Job(
            name=name,
            fixture=Path(f"{name}.toml"),
            execution=cast(Any, SimpleNamespace(guest=machine)),
            thread=None,
        )
        for name, machine in (("done", finished), ("stuck", wedged))
    }

    cluster._abandon_jobs(jobs)

    assert finished.removed and wedged.removed


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
            self.snapshots = 0

        def run(self, line: str, timeout: float = 0.0) -> str:
            return ""

        # Echoed first, the way a shell answers: `wait_for_driver` reads the
        # value the guest computed and a double that skips the echo hides
        # every check that could match its own question.
        def expect_command(self, command: str, timeout: float = 0.0) -> bytes:
            return f"{command}\r\ndriver=0\r\n".encode()

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            self.snapshots += 1
            if self.snapshots % 2:
                return (
                    b"\x1b[2J\x1b[1;1Hgentoo-install"
                    b"\x1b[3;3Hstorage"
                    b"\x1b[24;1H[enter] Continue"
                )
            # Different redraw bytes, but the same stable screen: Enter did
            # not open the row and Escape must therefore stay unsent.
            return (
                b"\x1b[2Jgentoo-install"
                b"\x1b[2B\r  storage"
                b"\x1b[21B\r[enter] Continue"
            )

        def close(self) -> None:
            pass

    import time as clock

    monkeypatch.setattr(clock, "sleep", lambda seconds: None)
    monkeypatch.setattr(tui, "_ROWS", 3)
    console = Console()
    seen = tui.walk(cast("Any", console), "en")

    assert "\x1b" not in console.sent, console.sent
    assert any("opened nothing" in one.what for one in seen.findings), seen.findings


def test_the_walk_waits_for_a_drawn_menu_after_the_echoed_launch_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch command already contains the program name. It is not a
    screen, so navigation must wait until curses has drawn the menu."""
    from typing import Any, cast

    from tests.vm import tui

    menu = (
        b"\x1b[2J\x1b[1;1Hgentoo-install"
        b"\x1b[3;3Hstorage"
        b"\x1b[24;1H[enter] Continue"
    )
    launch = (
        "cd /tmp/driver && TERM=vt220 LINES=24 COLUMNS=80 "
        "python3 -m gentoo_install --lang en\n"
    )

    class Console:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.snapshots = 0
            self.asked = 0

        def run(self, line: str, timeout: float = 0.0) -> str:
            return ""

        def send_raw(self, keys: str) -> None:
            self.sent.append(keys)

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            self.asked += 1
            return b"python3 -m gentoo_install # gentoo-install\r\n"

        def expect_command(self, command: str, timeout: float = 0.0) -> bytes:
            return f"{command}\r\ndriver=0\r\n".encode()

        def snapshot(self, seconds: float) -> bytes:
            self.snapshots += 1
            if self.snapshots <= 2:
                assert self.sent == [launch], "navigation reached the echoed command"
            if self.snapshots == 1:
                return b"python3 -m gentoo_install # gentoo-install\r\n"
            return menu

        def close(self) -> None:
            pass

    import time as clock

    monkeypatch.setattr(clock, "sleep", lambda seconds: None)
    monkeypatch.setattr(tui, "_ROWS", 1)
    console = Console()
    tui.walk(cast("Any", console), "en")

    assert console.snapshots >= 4
    assert console.asked == 0, "readiness fell back to echoed console text"
    assert console.sent[:3] == [launch, "\x1b[B", "\x1b[A"]


def test_menu_readiness_keeps_one_deadline_and_reports_the_last_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as clock
    from typing import Any, cast

    from tests.vm import tui
    from tests.vm.console import ConsoleTimeout

    class Console:
        def __init__(self) -> None:
            self.windows: list[float] = []

        def snapshot(self, seconds: float) -> bytes:
            self.windows.append(seconds)
            return b"still only the echoed gentoo-install command"

        def close(self) -> None:
            pass

    moments = iter((10.0, 10.0, 12.5, 13.0))
    monkeypatch.setattr(tui, "MENU_PATIENCE", 3.0)
    monkeypatch.setattr(clock, "monotonic", lambda: next(moments))
    console = Console()

    with pytest.raises(ConsoleTimeout, match="last rendered state") as refused:
        tui._wait_for_menu(cast("Any", console))

    assert console.windows == [2.0, 0.5]
    assert "still only the echoed gentoo-install command" in str(refused.value)


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


def test_vt_screen_retains_an_escape_split_between_serial_reads() -> None:
    from tests.vm.tui import VTScreen

    prefix = b"\x1b[2J\x1b[Halpha"
    control = b"\x1b[3;5H"
    suffix = b"X"
    whole = VTScreen()
    whole.feed(prefix + control + suffix)
    split = VTScreen()
    split.feed(prefix + control[:3])
    split.feed(control[3:] + suffix)

    assert split.rows() == whole.rows()
    assert split.cursor == whole.cursor
    assert split.rows()[2] == "    X"


def test_vt_screen_positions_wide_characters_by_terminal_cell_width() -> None:
    from tests.vm.tui import VTScreen

    screen = VTScreen(columns=8, lines=2)
    text = "\\u754c".encode().decode("unicode_escape")
    wide = text.encode()
    screen.feed(wide + b"ab")

    assert screen.rows()[0] == text + "ab"
    assert screen.cursor == (0, 4)


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
    """The fallback exists for a GRUB that writes only to VGA. The official
    minimal ISO answers `starting serial terminal on interface serial0` and can
    be read, and a menu that can be read is never typed at blind.

    What the fallback does changed: editing the menu blind never landed, and a
    screenshot of the guest shows the live system already logged in on the VGA
    console three seconds later. The medium's own auto-login is asked for a
    shell on the serial port instead."""
    from typing import Any, cast

    from tests.vm import cluster

    read: list[str] = []
    monkeypatch.setattr(cluster, "append_to_cmdline", lambda link, extra: read.append("read"))
    monkeypatch.setattr(
        cluster, "open_a_serial_shell_blind", lambda guest, link: read.append("shell")
    )
    cluster._edit_bios_cmdline(cast("Any", object()), cast("Any", object()))
    assert read == ["read"], "a menu that can be read is not typed at blind"

    # A GRUB that says nothing gets the auto-login attempt, from a fresh boot
    # because the unreadable menu counted down while the serial wait ran.
    reset: list[str] = []

    class Guest:
        def reset(self) -> None:
            reset.append("reset")

    class Link:
        def reopen(self, *, solicit_prompt: bool = True) -> None:
            reset.append(f"reopen:{solicit_prompt}")

    from tests.vm.proxmox import GrubNotReadable

    def silent(link: object, extra: str) -> None:
        raise GrubNotReadable("the menu is VGA-only")

    read.clear()
    monkeypatch.setattr(cluster, "append_to_cmdline", silent)
    cluster._edit_bios_cmdline(cast("Any", Guest()), cast("Any", Link()))
    assert read == ["shell"]
    assert reset == ["reset", "reopen:False"]


def test_bios_grub_serial_start_signal_stops_the_countdown() -> None:
    from typing import Any, cast

    from tests.vm import proxmox

    sent: list[str] = []

    class Console:
        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            assert "starting serial terminal on interface serial0" in pattern
            return b"starting serial terminal on interface serial0"

        def send_raw(self, keys: str) -> None:
            sent.append(keys)

        def snapshot(self, seconds: float) -> bytes:
            # A BIOS medium draws nothing to a serial console before the
            # kernel, so there is no countdown to confirm stopped.
            return b""

        def close(self) -> None:
            pass

    proxmox.hold_the_menu(cast(Any, Console()), timeout=1.0)
    assert sent == [proxmox.GRUB_HOLD]


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

    def close(self) -> None:
        pass


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


def test_an_idle_verdict_names_the_counters_before_the_console_tail() -> None:
    """A verdict is cut to `OUTCOME_BYTES`, and `zbm-unlock`'s console tail
    filled all 300 of them, so the round could not say whether the guest's
    counters had moved — which is the whole question a STUCK asks. The reason
    goes first, where truncation cannot reach it."""
    from tests.vm import cluster
    from tests.vm.console import ConsoleIdle, ConsoleTimeout

    sent: list[str] = []
    idle = ConsoleIdle(
        "never matched 'MARK_26_DONE', nothing arrived for 1200s; last output "
        "was " + "b'0.1/src/shared/data-fd-util.c [294/2044] compiling'" * 6
    )
    link = cluster.Reconnecting(lambda: _PatternConsole(idle, sent))
    watch = cluster.Watchdog(log=Path("/nonexistent"), counters=lambda: Traffic(0, 0, 0.0))

    with pytest.raises(ConsoleTimeout) as raised:
        link.wait_for("sh install.sh", timeout=10.0, idle=1200.0, watch=watch)

    said = str(raised.value)[: cluster.OUTCOME_BYTES]
    assert "counters were flat" in said, said
    assert said.startswith("the console was silent for 1200s"), said


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

        def close(self) -> None:
            pass

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
            self._bytes_read = 0
            self.reads = 0

        def _read_once(self) -> None:
            self.reads += 1
            clock.sleep(0.01)
            if not self.quiet:
                chunk = b"dev-libs/one/Manifest\n"
                self._buffer += chunk
                self._bytes_read += len(chunk)

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


def test_console_activity_past_buffer_limit_is_not_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retained tail is diagnostic state, not an activity counter."""
    import time

    from tests.vm.console import ConsoleIdle, ConsoleTimeout, SerialConsole

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    output = b"dev-libs/one/Manifest\n"

    class Printing:
        closed = False

        def __init__(self) -> None:
            self.reads = 0

        def recv(self, size: int) -> bytes:
            self.reads += 1
            clock[0] += 0.01
            return output

        def sendall(self, data: bytes) -> None:
            return None

        def close(self) -> None:
            return None

    guest = Printing()
    limit = 2 * len(output)
    console = SerialConsole(guest, BytesIO(), buffer_limit=limit)
    with pytest.raises(ConsoleTimeout) as timed:
        console.expect("MARK_1_DONE", timeout=1.0, idle=0.2)
    assert not isinstance(timed.value, ConsoleIdle)
    assert guest.reads > 2
    assert len(console._buffer) == limit
    assert clock[0] == pytest.approx(1.0)


def test_a_cn_run_clones_the_overlay_from_a_chinese_mirror(tmp_path: Path) -> None:
    """github never connects from inside a cluster guest, and an overlay clone
    that fails stops the whole install rather than degrading."""
    from gentoo_install.model.config import MirrorRegion, Sync
    from gentoo_install.exec.config import load
    from tests.vm import cluster

    source = Path(__file__).resolve().parents[1] / "fixtures" / "vm-zfs.toml"
    assert "github.com" in load(source).portage.overlays[0].sync_uri
    job = cluster.Job(name="vm-zfs", fixture=source)
    written = cluster.rewrite_fixtures(
        [job], tmp_path / "fixtures", MirrorRegion.CN, Sync.RSYNC
    )
    moved = load(written / source.name)
    overlay = next(one for one in moved.portage.overlays if one.name == "gentoo-zh")
    assert "github.com" not in overlay.sync_uri
    assert overlay.sync_uri.startswith("https://mirrors.cernet.edu.cn/")


def test_a_static_address_is_moved_to_the_one_the_scheduler_reserved(
    tmp_path: Path,
) -> None:
    """A fixture pins one address; the scheduler hands each guest its own. A
    machine installed on the pinned one comes up where nothing expects it, and
    its own checks then read the address it was told to have."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.config import MirrorRegion, Sync
    from tests.vm import cluster

    source = Path(__file__).resolve().parents[1] / "fixtures" / "static-ip.toml"
    pinned = load(source).system.addresses
    assert pinned, "this fixture exists to carry a static address"

    job = cluster.Job(name="static-ip", fixture=source)
    written = cluster.rewrite_fixtures(
        [job],
        tmp_path / "fixtures",
        MirrorRegion.CN,
        Sync.RSYNC,
        unlock_addresses={"static-ip": "10.31.0.207"},
    )
    moved = load(written / source.name).system

    assert moved.addresses == (f"10.31.0.207/{cluster.GUEST_PREFIX}",), moved.addresses
    assert moved.gateways == (cluster.GUEST_GATEWAY,), moved.gateways
    assert moved.dns == cluster.GUEST_RESOLVERS, moved.dns


def test_a_check_whose_name_has_a_space_is_written_to_one_file() -> None:
    """`root filesystem` is a check name, and an unquoted redirection split it
    into two words: bash answered `syntax error near unexpected token` and the
    Alpine run ended there with the install already finished.
    """
    import subprocess
    from typing import cast

    from tests.vm import run as vm_run
    from tests.vm.console import SerialConsole

    class Recording:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run(self, command: str, timeout: float = 120.0) -> None:
            self.commands.append(command)

    from gentoo_install.exec.config import load

    console = Recording()
    installation = load(Path(__file__).resolve().parents[1] / "fixtures" / "vm-btrfs.toml")
    vm_run.check_installed(cast("SerialConsole", console), installation)
    assert any("root filesystem" in one for one in console.commands)
    for command in console.commands:
        assert subprocess.run(
            ["bash", "-n", "-c", command], capture_output=True
        ).returncode == 0, command


def test_a_healthy_init_is_judged_by_the_marker_it_prints() -> None:
    """The shared contract gives `failed` the pattern `NO-FAILED-UNITS`, and a
    second rule here failed the run on any output at all, so the marker that
    means success read as the failure it rules out: a clean Alpine install
    reported `systemd reports failed units: NO-FAILED-UNITS`.
    """
    from tests.vm.installed import checks
    from tests.vm.run import check_expected
    from gentoo_install.exec.config import load

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "vm-btrfs.toml"
    installation = load(fixture)
    # These output forms are copied from the command contracts. Regex syntax is
    # not output, so every bounded check gets an explicit line.
    written = {
        "os-release": b"NAME=Gentoo\nID='gentoo'\n",
        "mounts": (
            b"/       /dev/vda2[/@]      btrfs\n"
            b"/efi    /dev/vda1          vfat\n"
            b"/home   /dev/vda2[/@home]  btrfs\n"
        ),
        "locale": b"LANG=zh_TW.UTF-8\n",
        "timezone": (
            f"/usr/share/zoneinfo/{installation.system.timezone}\n"
            f"{installation.system.timezone}\n"
        ).encode(),
        "hostname": installation.system.hostname.encode() + b"\n",
        "kernel": b"6.18.43-gentoo-dist-bin\n/boot/kernel-6.18.43-gentoo-dist-bin\n",
        "resolver": b"/run/systemd/resolve/stub-resolv.conf\nRESOLVCONF-OK\n",
        "portage": b"EMERGE-OK\n",
        "cpu-flags": b"CPUFLAGS-ALL-KNOWN\n",
        "init": b"systemd\n",
        "units": b"systemd-networkd.service enabled enabled\n",
        "failed": b"NO-FAILED-UNITS\n",
        "network": b"systemd-networkd.service enabled enabled\n",
        "esp": b"/efi /dev/vda1 vfat\n",
        "root filesystem": b"btrfs\n",
        "fstab": b"UUID=ab2e555d\t/\tbtrfs\tdefaults,subvol=@\t0\t1\n",
    }
    checks_for_fixture = checks(installation)
    missing = {one.name for one in checks_for_fixture} - written.keys()
    assert not missing, missing
    healthy = {f"{one.name}.txt": written[one.name] for one in checks_for_fixture}
    assert check_expected(healthy, load(fixture)) == 0
    broken = dict(healthy)
    broken["failed.txt"] = b"cronie.service loaded failed failed\n"
    assert check_expected(broken, load(fixture)) != 0


def test_installed_mount_check_requires_every_configured_target() -> None:
    """A partial btrfs layout must not pass as fully mounted."""
    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    fixture = Path("tests/fixtures/vm-btrfs.toml")
    mount_check = next(
        check for check in checks(load(fixture)) if check.name == "mounts"
    )
    missing_home = (
        "/       /dev/vda2[/@]      btrfs\n"
        "/efi    /dev/vda1          vfat\n"
    )
    complete = missing_home + "/home   /dev/vda2[/@home]  btrfs\n"

    assert re.search(mount_check.pattern, missing_home) is None
    assert re.search(mount_check.pattern.encode(), missing_home.encode()) is None
    assert re.search(mount_check.pattern, complete) is not None
    assert re.search(mount_check.pattern.encode(), complete.encode()) is not None


def test_installed_mount_check_handles_swap_and_unprobed_conversion() -> None:
    """Swap has no target; an unprobed conversion still requires its root."""
    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    bios_installation = load(Path("tests/fixtures/vm-bios.toml"))
    bios_check = next(
        check for check in checks(bios_installation) if check.name == "mounts"
    )
    conversion_check = next(
        check for check in checks(load(Path("tests/fixtures/vm-convert.toml")))
        if check.name == "mounts"
    )

    assert re.search(bios_check.pattern, "/ /dev/vda2 ext4\n") is not None
    assert re.search(conversion_check.pattern, "/ /dev/vda1 xfs\n") is not None
    assert re.search(conversion_check.pattern, "/proc proc proc\n") is None


def test_the_harness_starts_the_proxy_a_fixture_names_on_this_workstation() -> None:
    """`vm-proxy` and `vm-proxy-http` were skipped unless somebody had started
    a proxy by hand, and the cluster cannot run them at all, so the direction
    an operator on an intranet needs — the proxy works and the install
    finishes — had never been measured."""
    import socket
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from tests.vm.run import GATEWAY, free_port, proxy_for

    root = Path(__file__).resolve().parents[1]
    installation = load(root / "fixtures" / "vm-proxy.toml")
    port = free_port()
    named = replace(installation, proxy=replace(installation.proxy, host=GATEWAY, port=port))

    def reachable() -> bool:
        with socket.socket() as probe:
            probe.settimeout(2.0)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    assert not reachable()
    with proxy_for(named):
        assert reachable(), port
    # And taken down again: a run that leaves one behind makes the next run's
    # "already listening" branch true for a proxy nobody is serving.
    assert not reachable()


def test_a_proxy_already_listening_is_left_alone() -> None:
    """The port is in the fixture rather than chosen here so that an operator
    can debug against their own proxy; replacing it would take that away."""
    import socket
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from tests.vm.run import GATEWAY, proxy_for

    root = Path(__file__).resolve().parents[1]
    installation = load(root / "fixtures" / "vm-proxy.toml")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        named = replace(
            installation, proxy=replace(installation.proxy, host=GATEWAY, port=port)
        )
        with proxy_for(named):
            # Still the test's own listener: a second bind on the same port
            # would have raised rather than answered.
            assert listener.getsockname()[1] == port


def test_a_proxy_somewhere_else_starts_nothing_on_this_workstation() -> None:
    """An operator's own intranet proxy is unreachable from here by design,
    and binding its port here would answer for a machine that is not this
    one."""
    import socket
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from tests.vm.run import free_port, proxy_for

    root = Path(__file__).resolve().parents[1]
    installation = load(root / "fixtures" / "vm-proxy.toml")
    # A port nothing holds, so dropping the address check would make this
    # bind. `1080` would not: a proxy run holds that one.
    port = free_port()
    named = replace(installation, proxy=replace(installation.proxy, host="192.0.2.9", port=port))
    with proxy_for(named):
        with socket.socket() as probe:
            probe.settimeout(2.0)
            assert probe.connect_ex(("127.0.0.1", port)) != 0, port


def test_input_method_check_accepts_its_planned_environment() -> None:
    from gentoo_install.data import load_catalog
    from gentoo_install.exec.config import load
    from gentoo_install.plan.packages import input_environment
    from tests.vm.installed import checks

    installation = load(Path("tests/fixtures/vm-desktop.toml"))
    named = [one for one in checks(installation) if one.name == "inputmethod"]
    assert named, "a configuration with an input method has no check for it"

    environment = "\n".join(input_environment(installation, load_catalog()))
    for check in named:
        binary = check.command.removeprefix("command -v ").split(";", maxsplit=1)[0]
        output = f"/usr/bin/{binary}\n{environment}\n"
        assert re.search(check.pattern, output), (check, output)


def test_no_input_method_asks_for_no_environment() -> None:
    """Built here rather than named: this test pointed at `vm-gnome` until that
    fixture was given ibus, and a fixture is free to change what it installs."""
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    chosen = load(Path("tests/fixtures/vm-gnome.toml"))
    installation = replace(
        chosen, packages=replace(chosen.packages, applications=())
    )

    assert [one for one in checks(installation) if one.name == "inputmethod"] == []


def test_a_fixture_meant_to_fail_proves_its_failure_path(tmp_path: Path) -> None:
    """`vm-proxy-dead` succeeds only after its dead proxy refuses the stage3
    download; another non-zero result proves no such thing."""
    from tests.vm.campaign import Outcome, Run, mark_for

    dead = Run("fixtures/vm-proxy-dead.toml")
    log = tmp_path / "proxy-dead.log"
    log.write_text("the stage3 could not be fetched: [Errno 111] Connection refused\n")

    assert Outcome(dead, 1, 1.0, log).passed
    assert not Outcome(dead, 124, 1.0, log).passed

    log.write_text("configuration parse error\n")
    unrelated = Outcome(dead, 1, 1.0, log)
    assert not unrelated.passed
    assert mark_for(unrelated) == "FAIL"
    assert mark_for(Outcome(dead, 0, 1.0, log)) == "BYPASS"

    ordinary = Run("fixtures/vm-btrfs.toml")
    assert Outcome(ordinary, 0, 1.0, log).passed
    assert not Outcome(ordinary, 4, 1.0, log).passed


def test_neither_runner_keeps_its_own_copy_of_an_expectation() -> None:
    """One table, two readers. `Connection refused` was written in three
    places, and the two runners disagreed on the exit code beside it: the
    campaign wanted `1` and the cluster `b"4"`, and nothing compared them."""
    import ast

    from tests.vm.expectations import EXPECTATIONS

    wanted = [one.says for one in EXPECTATIONS.values() if one.says]
    assert wanted, "an empty set of markers would make this assert nothing"

    here = Path(__file__).resolve().parents[1] / "vm"
    for runner in ("campaign.py", "cluster.py", "run.py"):
        tree = ast.parse((here / runner).read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        for says in wanted:
            held = [one for one in literals if says in one]
            assert not held, (runner, says, held)


def test_a_fixture_that_has_to_degrade_is_not_green_when_it_did_not(tmp_path: Path) -> None:
    """`vm-binhost-fallback` finishes and boots whether or not its binary host
    ever failed, so the exit code cannot tell the two apart. The day the host
    answers again this run has to go red rather than quietly stop covering the
    degradation it exists to measure."""
    from gentoo_install.exec.apply import degradation_warning
    from gentoo_install.plan.portage import BINARY_PACKAGES
    from tests.vm.campaign import Outcome, Run

    fallback = Run("fixtures/vm-binhost-fallback.toml")
    log = tmp_path / "fallback.log"

    log.write_text("the install finished\n")
    assert not Outcome(fallback, 0, 1.0, log).passed

    log.write_text(degradation_warning(BINARY_PACKAGES, "the index answered 404") + "\n")
    assert Outcome(fallback, 0, 1.0, log).passed
    assert not Outcome(fallback, 4, 1.0, log).passed


def test_the_campaign_expects_exactly_the_fixtures_that_cannot_finish() -> None:
    """An expectation naming a fixture the campaign never runs is a rule that
    cannot fire, and it reads as coverage."""
    from tests.vm.campaign import STAGES
    from tests.vm.expectations import EXPECTATIONS

    scheduled = {Path(run.config).stem for runs in STAGES.values() for run in runs}

    assert set(EXPECTATIONS) <= scheduled, set(EXPECTATIONS) - scheduled
    assert {name for name, one in EXPECTATIONS.items() if one.must_stop} == {"vm-proxy-dead"}


def test_campaign_collects_an_outcome_from_every_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launch error must not hide completed guests or prevent their summary."""
    import subprocess

    from tests.vm import campaign

    # Real fixture names, because a `Run` reads its own configuration to
    # decide how heavy it is; what each one installs does not matter here.
    runs = [
        campaign.Run("fixtures/vm-xfs.toml"),
        campaign.Run("fixtures/vm-lvm.toml"),
        campaign.Run("fixtures/vm-btrfs.toml"),
    ]
    announced: list[str] = []

    def fake_perform(run: campaign.Run) -> campaign.Outcome:
        if run.config.endswith("vm-xfs.toml"):
            raise subprocess.TimeoutExpired(run.argv(), 1.0)
        if run.config.endswith("vm-lvm.toml"):
            raise OSError("could not launch guest")
        return campaign.Outcome(run, 0, 1.0, tmp_path / "complete.log")

    monkeypatch.setattr(campaign, "LOGS", tmp_path)
    monkeypatch.setattr(campaign, "wait_for_room", lambda: None)
    monkeypatch.setattr(campaign, "perform", fake_perform)
    monkeypatch.setattr(campaign, "announce", lambda outcome: announced.append(outcome.run.name))

    outcomes = campaign.parallel(runs)

    assert sorted(outcome.run.name for outcome in outcomes) == sorted(run.name for run in runs)
    assert sorted(announced) == sorted(run.name for run in runs)
    by_name = {outcome.run.name: outcome for outcome in outcomes}
    assert not by_name[runs[0].name].passed
    assert "TimeoutExpired" in (by_name[runs[0].name].error or "")
    assert not by_name[runs[1].name].passed
    assert "OSError" in (by_name[runs[1].name].error or "")
    assert by_name[runs[2].name].passed
    assert campaign.report(outcomes) == 1


def test_a_hypervisor_that_stops_answering_is_not_a_living_guest(tmp_path: Path) -> None:
    """`vm-desktop` sat for sixty-eight minutes on a node that had stopped
    answering, its console silent and its counters unreadable, and every look
    called it alive. One unanswered request is noise; a run of them is the
    watchdog going blind, and a blind watchdog must end the guest rather than
    wait out the ceiling."""
    from tests.vm.cluster import BLIND_SAMPLES, Watchdog

    log = tmp_path / "guest.log"
    log.write_text("")
    watchdog = Watchdog(log=log, counters=lambda: None)

    for _ in range(BLIND_SAMPLES - 1):
        assert watchdog.moved(), "one unreadable sample alone is not proof"
        assert not watchdog.stuck
    assert not watchdog.moved()

    assert watchdog.stuck
    assert "did not answer" in (watchdog.idle_reason() or "")


def test_a_guest_still_printing_survives_an_unreadable_counter(tmp_path: Path) -> None:
    """The counters go unreadable for a node under load while the guest is
    fine, and the console is the other half of the answer."""
    from tests.vm.cluster import BLIND_SAMPLES, Watchdog

    log = tmp_path / "guest.log"
    log.write_text("")
    watchdog = Watchdog(log=log, counters=lambda: None)

    for step in range(BLIND_SAMPLES * 3):
        log.write_text("x" * (step + 1) * 4096)
        assert watchdog.moved()

    assert not watchdog.stuck
    assert watchdog.idle_reason() is None


def test_reopening_a_console_closes_the_one_it_replaces() -> None:
    """`termproxy` holds the guest's serial chardev until its client goes away.
    `vm-btrfs` reconnected as the installed system switched root, the previous
    session was still draining the stream, and the new one read nothing for the
    rest of the run."""
    from tests.vm.cluster import Reconnecting

    order: list[str] = []

    class Session:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            order.append(f"closed {self.name}")

        def send(self, line: str) -> None:
            pass

    names = iter("abcd")

    def open_console() -> Session:
        name = next(names)
        order.append(f"opened {name}")
        return Session(name)

    link = Reconnecting(cast(Any, open_console))
    link.reopen(solicit_prompt=False)

    assert order == ["opened a", "closed a", "opened b"]


def test_a_console_that_refuses_to_close_does_not_stop_the_reconnect() -> None:
    """The socket is already gone in the case that makes the harness reconnect,
    so closing it is allowed to fail."""
    from tests.vm.cluster import Reconnecting

    opened: list[str] = []

    class Broken:
        def close(self) -> None:
            raise OSError("already gone")

        def send(self, line: str) -> None:
            pass

    def open_console() -> Broken:
        opened.append("open")
        return Broken()

    link = Reconnecting(cast(Any, open_console))
    link.reopen(solicit_prompt=False)

    assert opened == ["open", "open"]
def test_nothing_is_carried_when_it_was_not_asked_for() -> None:
    from tests.vm import cluster

    sent: list[str] = []

    class Link:
        def run(
            self, command: str, timeout: float = 120.0, *, repeatable: bool = True
        ) -> None:
            sent.append(command)

        def expect_output(self, command: str, timeout: float = 180.0) -> bytes:
            sent.append(command)
            return cluster.NETWORK_UP.encode()

    cluster.wait_for_network(cast(Any, Link()), 9300, "10.31.0.150")

    assert not any("/etc/hosts" in one for one in sent)


def test_a_campaign_answers_every_signal_that_asks_it_to_stop() -> None:
    """A campaign started in the background inherits `SIG_IGN` for SIGINT from
    a shell without job control, and CPython keeps an inherited ignore: two
    schedules ignored `kill -INT` entirely and kept seven guests on the cluster
    until SIGTERM reached them."""
    import signal as signals

    from tests.vm.cluster import _leave_on_a_signal

    before = {
        one: signals.getsignal(one)
        for one in (signals.SIGTERM, signals.SIGHUP, signals.SIGINT)
    }
    try:
        _leave_on_a_signal()
        installed = {one: signals.getsignal(one) for one in before}
        # The same function for all three, which SIGINT's inherited handler is
        # not: the interpreter's own raises `KeyboardInterrupt` as well.
        assert len(set(map(id, installed.values()))) == 1, (
            "a signal was left to the handler the campaign inherited"
        )
        for one, now in installed.items():
            with pytest.raises(KeyboardInterrupt, match=f"signal {int(one)}"):
                cast(Any, now)(int(one), None)
    finally:
        for one, was in before.items():
            signals.signal(one, was)


def test_a_worker_cannot_hold_the_interpreter_open_after_the_schedule_ends() -> None:
    """Two schedules ended by SIGTERM removed every guest and then hung at
    interpreter shutdown, joining workers still reading the consoles of guests
    that no longer existed. `_abandon` already destroys what a timed-out worker
    was holding, which is why its docstring says the workers are daemons."""
    import ast

    root = Path(__file__).resolve().parents[1]
    source = (root / "vm" / "cluster.py").read_text()
    daemons = [
        keyword.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Thread"
        for keyword in node.keywords
        if keyword.arg == "daemon"
    ]

    assert daemons, "a worker thread is started without saying whether it is a daemon"
    for value in daemons:
        assert isinstance(value, ast.Constant) and value.value is True


def test_a_name_typed_the_moment_the_prompt_appears_is_offered_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt that comes back means the name did not take, and it is offered
    again. `openrc-sdboot` and `vm-bios` printed their issue three times and
    were failed for a password prompt that was never coming."""
    from tests.vm import cluster

    monkeypatch.setattr(cluster, "AGETTY_FLUSHES_AFTER", 0.0)
    # The name's echo before each answer, because agetty echoes what is typed
    # at a login prompt and the harness reads its own name back to know that
    # the prompt after it is a fresh one.
    said = [
        b"root\r\n",
        b"\r\nopenrcsdbox login: ",
        b"root\r\n",
        b"\r\nopenrcsdbox login: ",
        b"root\r\n",
        b"Password: ",
    ]
    offered: list[str] = []

    class Prompting:
        def respond(self, line: str) -> None:
            offered.append(line)

        def observe(self, pattern: str, timeout: float) -> bytes:
            return said.pop(0)

    assert cluster._name_the_user(cast(Any, Prompting()))
    assert offered == ["root", "root", "root"], "the name is offered again"


def test_a_prompt_that_never_takes_a_name_ends_rather_than_answers_for_ever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.vm import cluster

    monkeypatch.setattr(cluster, "AGETTY_FLUSHES_AFTER", 0.0)
    offered: list[str] = []

    class Deaf:
        def respond(self, line: str) -> None:
            offered.append(line)

        def observe(self, pattern: str, timeout: float) -> bytes:
            return b"\r\nopenrcsdbox login: "

    assert not cluster._name_the_user(cast(Any, Deaf()))
    assert len(offered) == cluster.LOGIN_TRIES


def test_a_console_that_ends_in_the_node_s_own_ssh_names_the_node(tmp_path: Path) -> None:
    """`termproxy` reaches a guest on another node over ssh, and when that ends
    the log carries ssh's own words with no `| ` prefix. Three guests on
    `infra-node3` were failed for a marker that never arrived while their
    installs were running: one had just printed `Installation finished`."""
    from tests.vm.cluster import console_proxy_dropped

    log = tmp_path / "vm-lvm.log"
    log.write_text(
        "| >>> Emerging (1 of 2) sys-kernel/linux-firmware-20260622::gentoo\n"
        "Read from remote host 10.31.0.202: Connection reset by peer\n"
        "Connection to 10.31.0.202 closed.\n"
        "client_loop: send disconnect: Broken pipe\n"
    )

    said = console_proxy_dropped(log)

    assert "10.31.0.202" in said
    assert "console proxy" in said


def test_a_console_refused_by_the_node_names_it_too(tmp_path: Path) -> None:
    from tests.vm.cluster import console_proxy_dropped

    log = tmp_path / "vm-desktop.log"
    log.write_text("ssh: connect to host 10.31.0.202 port 22: Connection refused\n")

    assert "10.31.0.202" in console_proxy_dropped(log)


def test_an_ordinary_console_timeout_blames_no_node(tmp_path: Path) -> None:
    """The guest's own words must not read as the node going away, or a healthy
    node stops taking guests for an install that hung by itself."""
    from tests.vm.cluster import console_proxy_dropped

    log = tmp_path / "vm-zfs.log"
    log.write_text(
        "| >>> Emerging (5 of 9) sys-fs/zfs-2.4.0::gentoo\n"
        "| ssh-keygen: generating new host keys\n"
        "| * Starting sshd ...\n"
    )

    assert console_proxy_dropped(log) == ""


def test_what_the_guest_handed_back_is_written_beside_its_log(tmp_path: Path) -> None:
    """`install.jsonl` says which packages came from a binary host, which were
    compiled and why each degradation happened. It crossed the console and was
    dropped: only `install.rc` was ever read."""
    from tests.vm.cluster import keep_results

    log = tmp_path / "vm-btrfs.log"
    log.write_text("")

    keep_results(
        log,
        {
            "install.rc": b"0\n",
            "install.jsonl": b'{"package":"sys-apps/portage","source":"binhost"}\n',
        },
    )

    assert (tmp_path / "vm-btrfs.install.rc").read_bytes() == b"0\n"
    assert b"binhost" in (tmp_path / "vm-btrfs.install.jsonl").read_bytes()


def test_a_file_that_cannot_be_written_does_not_end_the_run(tmp_path: Path) -> None:
    """The install already happened; a directory that refuses a write is not a
    reason to throw its result away."""
    from tests.vm.cluster import keep_results

    log = tmp_path / "sub" / "vm-btrfs.log"

    keep_results(log, {"install.rc": b"0\n"})


def test_a_lease_older_than_any_run_is_taken_over(tmp_path: Path) -> None:
    """A schedule that is killed never releases what it took. Sixteen rounds
    left a hundred leases behind and the pool was empty from the first
    dispatch of the next: the whole campaign died at `no static address is
    available from 10.31.0.150`."""
    import os
    import time

    from tests.vm.cluster import LEASE_SECONDS, AddressPool

    pool = AddressPool(tmp_path, lambda address: False)
    leases = tmp_path / "addresses"
    leases.mkdir()
    stale = leases / "10.31.0.150"
    stale.touch()
    old = time.time() - LEASE_SECONDS - 60
    os.utime(stale, (old, old))

    assert pool.reserve("10.31.0.150") == "10.31.0.150"


def test_a_lease_of_a_running_guest_is_left_alone(tmp_path: Path) -> None:
    """Six hours is longer than any run, so a guest still installing keeps the
    address it was given."""
    from tests.vm.cluster import AddressPool

    pool = AddressPool(tmp_path, lambda address: False)
    leases = tmp_path / "addresses"
    leases.mkdir()
    (leases / "10.31.0.150").touch()

    assert pool.reserve("10.31.0.150") == "10.31.0.151"


def test_a_lease_outlives_the_age_rule_while_its_schedule_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A campaign runs longer than one guest's ceiling, so the age rule alone
    frees an address whose schedule is still using it. `vm-unlock` was on
    10.31.0.151 when a round that started 92 minutes later was given the same
    address, and its initramfs answered `dracut Warning: Duplicate address
    detected for 10.31.0.151 for interface eth0`.
    """
    import os
    import time

    from tests.vm import cluster
    from tests.vm.cluster import LEASE_SECONDS, AddressPool

    # The pytest process is not a schedule, so the pid that stands for one is
    # named here rather than taken from this process.
    monkeypatch.setattr(cluster, "_scheduler_is_running", lambda pid: pid == 4242)

    pool = AddressPool(tmp_path, lambda address: False)
    leases = tmp_path / "addresses"
    leases.mkdir()
    mine = leases / "10.31.0.150"
    mine.write_text("4242\n")
    old = time.time() - LEASE_SECONDS - 60
    os.utime(mine, (old, old))

    assert pool.reserve("10.31.0.150") == "10.31.0.151"

    # Negative control: the same lease held by a pid that is not a schedule of
    # this harness is nobody's, and the age rule frees it.
    gone = leases / "10.31.0.151"
    gone.write_text("7\n")
    os.utime(gone, (old, old))
    assert pool.reserve("10.31.0.151") == "10.31.0.151"


def test_a_schedule_releases_only_the_leases_it_holds(tmp_path: Path) -> None:
    """`release` unlinked by name, so a schedule that ended freed an address
    another schedule had taken over, and the guest still answering on it met a
    second guest that had just been given it.
    """
    import os

    from tests.vm.cluster import AddressPool

    pool = AddressPool(tmp_path, lambda address: False)
    leases = tmp_path / "addresses"
    leases.mkdir()
    (leases / "10.31.0.150").write_text(f"{os.getpid()}\n")
    (leases / "10.31.0.151").write_text("1\n")

    pool.release("10.31.0.150")
    pool.release("10.31.0.151")

    assert not (leases / "10.31.0.150").exists(), "its own lease goes"
    assert (leases / "10.31.0.151").exists(), "somebody else's stays"


def test_the_guest_is_told_not_to_ask_for_aaaa() -> None:
    """`/etc/hosts` carries IPv4 addresses only, and an `AF_UNSPEC` lookup asks
    DNS for the AAAA regardless. A resolver that does not answer turns that
    into `EAI_AGAIN` for the whole call, so a name that is in the file fails
    anyway: sixteen rounds ended at the stage3 fetch with `hosts: files dns`
    and `in /etc/hosts: True` printed beside the error."""
    from tests.vm.cluster import GUEST_RESOLVER, use_our_resolvers

    written = use_our_resolvers()

    assert "options no-aaaa" in written
    assert f"nameserver {GUEST_RESOLVER}" in written
    assert written.index("options no-aaaa") < written.index("nameserver")


def test_the_unlock_waits_for_the_daemon_before_it_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon comes up inside an initramfs that has to be unpacked first,
    and connecting the moment the reset returns answered `Connection timed out
    during banner exchange` for one that was seconds from starting."""
    from types import SimpleNamespace

    from tests.vm import run as runner

    answers = ["ssh: connect to host: Connection refused", "Permission denied (publickey)."]
    asked: list[int] = []

    def probing(argv: list[str], **rest: object) -> object:
        asked.append(1)
        said = answers[min(len(asked) - 1, len(answers) - 1)]
        return SimpleNamespace(returncode=255, stdout="", stderr=said)

    monkeypatch.setattr("subprocess.run", probing)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    runner.wait_for_unlock_daemon(tmp_path / "key", 2222)

    assert len(asked) == 2, "the first refusal is not the answer"


def test_a_daemon_that_never_answers_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from tests.vm import run as runner

    def refusing(argv: list[str], **rest: object) -> object:
        return SimpleNamespace(returncode=255, stdout="", stderr="Connection refused")

    monkeypatch.setattr("subprocess.run", refusing)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    with pytest.raises(RuntimeError, match="no ssh daemon on port 2222"):
        runner.wait_for_unlock_daemon(tmp_path / "key", 2222, patience=0.01)


def test_the_unlock_itself_waits_first() -> None:
    import inspect

    from tests.vm import run as runner

    code = inspect.getsource(runner.remote_unlock)
    assert code.index("wait_for_unlock_daemon(") < code.index("subprocess.Popen(")


def test_the_local_runner_pins_no_interface_name_either() -> None:
    """`vm-unlock`'s own serial log has `virtio_net virtio0 enp0s2: renamed
    from eth0` at 156.6 seconds — after `systemd-networkd` started and after
    `Reached target Network`. The unit generated from `ip=eth0:dhcp` matched a
    name that no longer existed, so nothing answered on the forwarded port.

    `cluster.rewrite_fixtures` has cleared the interface since #407; this is
    the other runner, which did not, so the two disagreed about the one
    parameter that decides whether the initramfs is reachable.
    """
    from pathlib import Path

    from gentoo_install.exec.config import load
    from gentoo_install.plan.bootloader import unlock_parameters
    from tests.vm.run import remote_config

    for name in ("vm-unlock", "zbm-unlock"):
        fixture = load(Path(f"tests/fixtures/{name}.toml"))
        # The negative control is the input: the fixture names a device, which
        # is a reasonable thing for an operator to write and wrong here.
        assert fixture.kernel.remote_unlock.interface, name

        prepared = remote_config(fixture, "ssh-ed25519 AAAA test")
        assert prepared.kernel.remote_unlock.interface == "", name

        address = next(
            one for one in unlock_parameters(prepared) if one.startswith("ip=")
        )
        assert "eth0" not in address, address


@pytest.mark.lab
def test_create_target_refuses_a_path_outside_the_run_directories(tmp_path: Path) -> None:
    """Its first act is to delete the file it is given. The images an in-place
    conversion will be handed are downloaded once and kept — `lab/vm/cloud/`
    holds three — so a path outside `WORKROOT` is a mistake, and one caught
    after the unlink is caught too late."""
    import pytest

    from tests.vm.run import DEFAULT_TARGET_SIZE, WORKROOT, create_target

    keep = tmp_path / "debian-12-genericcloud-amd64.qcow2"
    keep.write_bytes(b"not really an image, but it stands for one")

    with pytest.raises(ValueError, match="deletes what it is given"):
        create_target(keep, DEFAULT_TARGET_SIZE)
    assert keep.exists(), "the refusal has to come before the unlink"
    assert keep.read_bytes().startswith(b"not really"), "and leave it untouched"

    # Negative control: a path inside a run directory is the ordinary case and
    # still works, or the guard would have stopped every run.
    inside = WORKROOT / "unit-test-create-target" / "target.qcow2"
    inside.parent.mkdir(parents=True, exist_ok=True)
    try:
        made = create_target(inside, DEFAULT_TARGET_SIZE)
        assert made.exists() and made.stat().st_size > 0
    finally:
        inside.unlink(missing_ok=True)
        inside.parent.rmdir()


@pytest.mark.parametrize("seed", [(), ("mklabel", "msdos")])
def test_requested_target_size_reaches_disk_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: tuple[str, ...]
) -> None:
    """A fixture larger than the former fixed size must reach qemu-img."""
    from tests.vm import run as runner

    launched: list[list[str]] = []

    def records(command: list[str], **named: object) -> object:
        launched.append(command)
        return object()

    monkeypatch.setattr(runner, "WORKROOT", tmp_path)
    monkeypatch.setattr("tests.vm.run.subprocess.run", records)
    target = tmp_path / ("seeded" if seed else "blank") / "target.qcow2"
    target.parent.mkdir()

    runner.create_target(target, "96G", seed)

    creation = next(command for command in launched if command[:2] == ["qemu-img", "create"])
    assert creation[-1] == "96G"


def test_local_runner_options_preserve_defaults_and_accept_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--resolver` without an address is an explicit request to keep the medium."""
    from tests.vm import run as runner

    captured: list[object] = []
    medium = SimpleNamespace(name="test", source_stamp=lambda: "test-medium")

    def capture(args: object, received_medium: object, workdir: Path) -> int:
        captured.append(args)
        return 0

    monkeypatch.setattr(runner, "MEDIA", {"test": medium})
    monkeypatch.setattr(runner, "WORKROOT", tmp_path)
    monkeypatch.setattr(runner, "_perform", capture)

    assert runner.main(["--medium", "test"]) == 0
    assert cast(Any, captured[0]).target_size == runner.DEFAULT_TARGET_SIZE
    assert cast(Any, captured[0]).resolver == runner.DEFAULT_RESOLVERS

    assert runner.main(
        ["--medium", "test", "--target-size", "96G", "--resolver", "10.0.2.2"]
    ) == 0
    assert cast(Any, captured[1]).target_size == "96G"
    assert cast(Any, captured[1]).resolver == ["10.0.2.2"]

    assert runner.main(["--medium", "test", "--resolver"]) == 0
    assert cast(Any, captured[2]).resolver == []


def test_requested_resolver_replaces_the_medium_and_none_leaves_it_alone() -> None:
    """A local mirror can run without either public nameserver."""
    from tests.vm import run as runner
    from tests.vm.console import SerialConsole

    class Recording:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run(self, command: str, timeout: float = 120.0) -> None:
            self.commands.append(command)

    requested = Recording()
    runner.pin_resolver(cast(SerialConsole, requested), ("10.0.2.2",))
    assert requested.commands == [
        "printf %s 'nameserver 10.0.2.2\n' > /etc/resolv.conf"
    ]

    unchanged = Recording()
    runner.pin_resolver(cast(SerialConsole, unchanged), ())
    assert unchanged.commands == []


def test_runner_records_the_size_and_effective_resolver() -> None:
    """The result has enough runner inputs to replay an installation."""
    from tests.vm import run as runner
    from tests.vm.console import SerialConsole

    class Recording:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run(self, command: str, timeout: float = 120.0) -> None:
            self.commands.append(command)

    console = Recording()
    runner.record_run_options(cast(SerialConsole, console), "96G", ("10.0.2.2",))
    assert console.commands[0] == f"mkdir -p {runner.RESULT_DIR}"
    assert "target_size=96G" in console.commands[1]
    assert "requested_resolver=10.0.2.2" in console.commands[1]
    assert "cat /etc/resolv.conf" in console.commands[1]

    medium = Recording()
    runner.record_run_options(cast(SerialConsole, medium), "96G", ())
    assert "requested_resolver=medium" in medium.commands[1]


def test_a_failed_remote_unlock_answers_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--boot-installed` powered the machine off and returned the moment the
    remote unlock failed, so `zbm-unlock` never reached a login: every verdict
    it produced said only that nothing answered on a forwarded port. The
    machine is sitting at its own passphrase prompt, and the console gets in,
    so the failure is answered rather than raised.
    """
    from tests.vm import run as harness

    def refuses(*rest: object, **named: object) -> str:
        raise RuntimeError("no ssh daemon on port 56353 after 180s")

    monkeypatch.setattr(harness, "remote_unlock", refuses)
    said = harness.try_remote_unlock(Path("/dev/null"), 1, cast(Any, object()))
    assert "no ssh daemon on port 56353" in said

    # Negative control one: an unlock that worked answers nothing, or the
    # caller would report a failure on every successful run.
    monkeypatch.setattr(harness, "remote_unlock", lambda *rest, **named: "proof")
    assert harness.try_remote_unlock(Path("/dev/null"), 1, cast(Any, object())) == ""

    # Negative control two: only the unlock's own failure is answered. A
    # KeyboardInterrupt or a programming error still leaves the run.
    def breaks(*rest: object, **named: object) -> str:
        raise ValueError("this is not the unlock failing")

    monkeypatch.setattr(harness, "remote_unlock", breaks)
    with pytest.raises(ValueError):
        harness.try_remote_unlock(Path("/dev/null"), 1, cast(Any, object()))


def test_a_zfsbootmenu_unlock_fixture_checks_the_image_that_carries_the_daemon() -> None:
    """ZFSBootMenu unlocks the pool from its own EFI image, not the system
    initramfs, so that file is the only place the ssh daemon can be.
    `zbm-unlock` failed three times reporting nothing but a forwarded port
    that went unanswered, while the machine itself was sound.
    """
    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    unlocking = load(Path("tests/fixtures/zbm-unlock.toml"))
    named = {one.name: one for one in checks(unlocking)}
    assert "zbm unlock key" in named, sorted(named)
    asked = named["zbm unlock key"].command
    assert "/efi/EFI/zbm" in asked
    # The acl or the hook, not the word `dropbear`: the image carries
    # `etc/dropbear/ssh_host_rsa_key`, so counting that word answered 3 for an
    # image with no authorized key and no start hook in it at all.
    assert "authorized_keys" in asked and "dropbear-start" in asked, asked

    # Negative control one: ZFSBootMenu without the unlock has no daemon to
    # look for, and asking for one would fail every ordinary ZBM install.
    plain = load(Path("tests/fixtures/zfs-zbm.toml"))
    assert plain.bootloader.kind is unlocking.bootloader.kind
    assert not plain.kernel.remote_unlock.enabled
    assert "zbm unlock key" not in {one.name for one in checks(plain)}

    # Negative control two: a remote unlock under another bootloader lives in
    # the system initramfs, and that image is not on the esp.
    elsewhere = load(Path("tests/fixtures/vm-unlock.toml"))
    assert elsewhere.kernel.remote_unlock.enabled
    assert "zbm unlock key" not in {one.name for one in checks(elsewhere)}


def test_the_driver_cd_is_found_rather_than_numbered(tmp_path: Path) -> None:
    """`/dev/sr1` was hardcoded in four places. It is the second CD only when
    an install medium is booted: a guest booting from its own disk, which is
    what an in-place conversion of a cloud image does, has the driver as
    `/dev/sr0` and every one of those four mounts failed.

    Run against a fake `mount` on PATH, the one-line form tries each candidate
    in turn, stops at the first that succeeds, and answers with `mountpoint`
    rather than with the last failed mount.
    """
    import subprocess

    from tests.vm.driver import ENTRY, FIND_DRIVER, PACKED_ENTRY

    assert "\n" not in FIND_DRIVER, "a console sends this as one command"
    assert FIND_DRIVER in ENTRY and FIND_DRIVER in PACKED_ENTRY

    fake = tmp_path / "bin"
    fake.mkdir()
    calls = tmp_path / "calls"
    (fake / "mount").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'for one in "$@"; do case "$one" in /dev/sr0) exit 0;; esac; done\n'
        "exit 32\n"
    )
    (fake / "mountpoint").write_text(
        f'#!/bin/sh\n[ -f {tmp_path}/is-mounted ] && exit 0\nexit 1\n'
    )
    (fake / "mkdir").write_text("#!/bin/sh\nexit 0\n")
    for one in fake.iterdir():
        one.chmod(0o755)
    environment = {"PATH": f"{fake}:/usr/bin:/bin"}

    refused = subprocess.run(
        ["sh", "-c", FIND_DRIVER], env=environment, capture_output=True, text=True
    )
    assert refused.returncode != 0, refused
    assert calls.read_text().split("\n")[:3] == [
        "-o ro /dev/disk/by-label/GENTOO-INSTALL /mnt/driver",
        "-o ro /dev/sr1 /mnt/driver",
        "-o ro /dev/sr0 /mnt/driver",
    ], calls.read_text()

    (tmp_path / "is-mounted").touch()
    accepted = subprocess.run(
        ["sh", "-c", FIND_DRIVER], env=environment, capture_output=True, text=True
    )
    assert accepted.returncode == 0, accepted


def test_every_driver_mount_goes_through_the_one_finder() -> None:
    """The denominator: no caller keeps its own device number, so fixing one
    place fixed all of them."""
    from tests.vm.driver import REPOSITORY

    from tests.vm.driver import FIND_DRIVER

    offenders: list[str] = []
    read: list[str] = []
    for name in ("run.py", "cluster.py", "campaign.py"):
        path = REPOSITORY / "tests" / "vm" / name
        read.append(name)
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "/dev/sr" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{name}:{number}: {line.strip()}")
    assert read == ["run.py", "cluster.py", "campaign.py"], read
    assert not offenders, offenders

    # And the one place that does name devices is the finder itself, so the
    # count above is zero because the callers use it rather than because the
    # scan missed them.
    source = (REPOSITORY / "tests" / "vm" / "driver.py").read_text()
    naming = [
        line for line in source.splitlines()
        if "/dev/sr" in line and not line.lstrip().startswith("#")
    ]
    assert len(naming) == 1, naming
    assert "/dev/sr1 /dev/sr0" in FIND_DRIVER


def test_a_conversion_marks_home_and_checks_the_marker_after_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker write and read must both be wired to the conversion path."""
    from gentoo_install.exec.config import load
    from tests.vm import convert

    class Console:
        def __init__(self) -> None:
            self.commands: list[str] = []
            self.asked: list[str] = []
            self.answer = b""

        def run(self, command: str, timeout: float = 0.0) -> None:
            self.commands.append(command)

        def expect_output(self, command: str, timeout: float = 0.0) -> bytes:
            self.asked.append(command)
            return self.answer

        def expect_command(self, command: str, timeout: float = 0.0) -> bytes:
            raise AssertionError("the preservation check used expect_command")

    console = Console()
    convert.convert(cast(Any, console), "fixtures/vm-convert.toml")
    assert console.commands[0] == (
        f"printf '%s\\n' {convert.HOME_MARKER} > {convert.HOME_MARKER_PATH}"
    )

    installation = load(Path("tests/fixtures/vm-convert.toml"))
    assert convert.HOME_MARKER_CHECK in convert.conversion_checks(installation)
    monkeypatch.setattr(
        convert,
        "conversion_checks",
        lambda _: (convert.HOME_MARKER_CHECK,),
    )
    console.answer = b"home=1\n"
    assert convert.check_installed(cast(Any, console), installation) == ""
    assert console.asked == [convert.HOME_MARKER_CHECK.command]

    # `cat` names the file it could not open, so the diagnostic for a missing
    # marker carried the marker: the check passed on a conversion that had
    # thrown /home away. The runner merges stderr into stdout, so that text
    # really does reach the pattern.
    missing = (
        f"cat: {convert.HOME_MARKER_PATH}: No such file or directory\n".encode()
    )
    for answer in (b"", b"home=0\n", missing, f"{convert.HOME_MARKER}\n".encode()):
        console.answer = answer
        assert "home marker" in convert.check_installed(cast(Any, console), installation)


def test_a_conversion_asks_for_a_port_nobody_holds() -> None:
    """Two runs asked for 2222 and the second died with

        qemu-system-x86_64: -netdev user,...,hostfwd=tcp::2222-:22:
        Could not set up host forwarding rule 'tcp::2222-:22'

    which names the rule and not the port, and not what already had it.
    `tests/vm/run.py` has `free_port()` for exactly this; the conversion runner
    took `VmSpec`'s default instead.
    """
    import ast
    import inspect

    from tests.vm import convert

    for where in (convert.main, convert.boot_and_check):
        tree = ast.parse(inspect.getsource(where).lstrip())
        specs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "VmSpec"
        ]
        assert specs, where.__name__
        for spec in specs:
            named = {
                keyword.arg
                for keyword in spec.keywords
                if keyword.arg is not None
            }
            assert "ssh_port" in named, f"{where.__name__} takes the default port"


def test_two_free_ports_in_a_row_differ() -> None:
    """The negative control for the above: a `free_port()` that answered the
    same number twice would satisfy the check and collide anyway."""
    from tests.vm.run import free_port

    seen = {free_port() for _ in range(8)}
    assert len(seen) > 1, seen


def test_a_vm_is_asked_to_quit_before_it_is_killed(tmp_path: Path) -> None:
    """A Fedora conversion wrote a `Gentoo` NVRAM entry and `efibootmgr -v` in
    that guest answered

        BootOrder: 0002,0001,0003,0000,0004
        Boot0002* Gentoo  ...\\EFI\\Gentoo\\grubx64.efi

    with Gentoo first. The next VM in the same work directory booted
    `Boot0001 "Fedora"` and stopped at a missing kernel, and the workdir's
    `OVMF_VARS.fd` held neither name: `kill()` is SIGKILL, so the pflash write
    stayed in the host page cache.
    """
    import socket as socket_module

    from tests.vm.qemu import Vm

    class Process:
        def __init__(self) -> None:
            self.killed = False

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            self.killed = True

    monitor = tmp_path / "monitor.sock"
    listener = socket_module.socket(socket_module.AF_UNIX)
    listener.bind(str(monitor))
    listener.listen(1)
    try:
        vm = Vm.__new__(Vm)
        vm.monitor_socket = monitor
        vm.serial_socket = tmp_path / "serial.sock"
        process = Process()
        vm._process = cast(Any, process)

        vm.shut_down(timeout=5.0)
        client, _ = listener.accept()
        with client:
            assert client.recv(64) == b"quit\n"
    finally:
        listener.close()
    # And the kill still happens, because the monitor is the polite route and
    # not the guaranteed one.
    assert process.killed


def test_a_vm_exit_takes_the_quit_path() -> None:
    """The negative control: `__exit__` calling `kill()` straight is what lost
    the firmware variables, so the path it takes is the contract."""
    import inspect

    from tests.vm.qemu import Vm

    source = inspect.getsource(Vm.__exit__)
    assert "self.shut_down()" in source, source
    assert "self.kill()" not in source, source


def _run_the_cpu_flags_check(tmp_path: Path, flags: str, known: tuple[str, ...]) -> str:
    """Execute the installed-state command against a fake portage tree.

    The command is run rather than read: it is shell, and a test that asserts
    on its text passes for a command that does nothing.
    """
    import os
    import subprocess

    from tests.vm.installed import CPU_FLAGS_COMMAND

    tree = tmp_path / "repo"
    (tree / "profiles/desc").mkdir(parents=True)
    (tree / "profiles/desc/cpu_flags_x86.desc").write_text(
        "".join(f"{one} - Use the {one} instruction set\n" for one in known),
        encoding="utf-8",
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    portageq = binaries / "portageq"
    portageq.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'get_repo_path) printf \'%s\\n\' "{tree}" ;;\n'
        f"envvar) printf '%s\\n' '{flags}' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    portageq.chmod(0o755)
    environment = dict(os.environ, PATH=f"{binaries}:/usr/bin:/bin")
    return subprocess.run(
        ["sh", "-c", CPU_FLAGS_COMMAND],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    ).stdout


def test_a_cpu_flag_portage_does_not_define_is_reported(tmp_path: Path) -> None:
    """`vaes` is a cpuinfo flag with no `CPU_FLAGS_X86` counterpart, and it
    was written into `make.conf` for three weeks. The authority is a file only
    the installed system has, so this is read there and nowhere else."""
    said = _run_the_cpu_flags_check(
        tmp_path, "aes avx2 vaes", ("aes", "avx", "avx2", "fma3")
    )
    assert "CPUFLAGS-UNKNOWN: vaes" in said, said
    assert "CPUFLAGS-ALL-KNOWN" not in said, said


def test_flags_the_tree_defines_pass(tmp_path: Path) -> None:
    said = _run_the_cpu_flags_check(tmp_path, "aes avx2 fma3", ("aes", "avx", "avx2", "fma3"))
    assert "CPUFLAGS-ALL-KNOWN" in said, said
    assert "CPUFLAGS-UNKNOWN" not in said, said


def test_a_prefix_of_a_known_flag_is_not_accepted_as_that_flag(tmp_path: Path) -> None:
    """`grep -q "^$one - "` and not `grep -q "$one"`: `avx` appears inside
    every `avx512*` line, so a substring check accepts a name the tree does
    not define."""
    said = _run_the_cpu_flags_check(tmp_path, "avx512", ("avx", "avx512f"))
    assert "CPUFLAGS-UNKNOWN: avx512" in said, said


def test_the_check_cannot_pass_by_the_tool_being_absent(tmp_path: Path) -> None:
    """The installer does not merge `cpuid2cpuflags`, so a pattern that
    accepted its absence would be a rule that never fires. The verdict comes
    from the tree, and the tool's answer is printed beside it."""
    from tests.vm.installed import CPU_FLAGS_COMMAND, checks
    from tests.unit.layouts import config

    said = _run_the_cpu_flags_check(tmp_path, "aes vaes", ("aes",))
    assert "CPUFLAGS-ALL-KNOWN" not in said, said

    wanted = next(one for one in checks(config()) if one.name == "cpu-flags")
    assert wanted.pattern == r"(?m)^CPUFLAGS-ALL-KNOWN$", wanted
    assert "cpuid2cpuflags" in CPU_FLAGS_COMMAND


def test_a_default_entry_that_moved_is_a_failure_and_a_one_shot_is_not() -> None:
    """The whole promise of this mode is that the default does not change, so
    it is read before and after rather than assumed. `next_entry` is the
    one-shot and appears exactly because the arming worked; `saved_entry` and
    the firmware's `BootOrder` are what must not move."""
    from tests.vm.ram import _default_changed, _one_shot_is_armed

    before = b"saved_entry=Debian\n"
    armed = b"saved_entry=Debian\nnext_entry=gentoo-install memory environment\n"
    assert not _default_changed(before, armed), "the one-shot is the point"
    assert _one_shot_is_armed(armed)
    assert not _one_shot_is_armed(before)

    moved = b"saved_entry=gentoo-install memory environment\n"
    assert _default_changed(before, moved)

    order_before = b"BootOrder: 0001,0002\n"
    order_after = b"BootOrder: 0003,0001,0002\n"
    assert _default_changed(order_before, order_after)


def test_the_memory_run_checks_the_medium_and_the_payload_apart() -> None:
    """A live medium that booted without the configuration is an environment
    the operator has to drive by hand, which is not what was asked for. Two
    claims, two waits, two messages."""
    from typing import Any, cast

    from tests.vm import ram
    from tests.vm.console import ConsoleTimeout

    class Booted:
        """The medium speaks and the payload never does."""

        def __init__(self) -> None:
            self.asked: list[str] = []

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            self.asked.append(pattern)
            if pattern == ram.PAYLOAD_SPEAKS:
                raise ConsoleTimeout("no first screen")
            return b"livecd login: "

    console = Booted()
    said = ram.came_up(cast(Any, console), "ram")
    assert "without its configuration" in said, said
    assert console.asked == [ram.CJK_SPEAKS, ram.PAYLOAD_SPEAKS], console.asked


def test_each_mode_waits_for_its_own_medium() -> None:
    """`--lowram` boots Alpine and `--ram` boots the CJK ISO, and a run that
    waited for the wrong banner would pass on a medium it did not ask for."""
    from typing import Any, cast

    from tests.vm import ram
    from tests.vm.console import ConsoleTimeout

    for mode, wanted in (("ram", ram.CJK_SPEAKS), ("lowram", ram.ALPINE_SPEAKS)):
        asked: list[str] = []

        class Silent:
            def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
                asked.append(pattern)
                raise ConsoleTimeout("nothing")

        assert "never spoke" in ram.came_up(cast(Any, Silent()), mode)
        assert asked == [wanted], (mode, asked)
    assert ram.CJK_SPEAKS != ram.ALPINE_SPEAKS


def test_memory_install_waits_for_completion_after_the_first_screen() -> None:
    """The `install` input is consent, not evidence that its installation ended."""
    from tests.vm import ram

    class Finished:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.waited: list[tuple[str, float, float]] = []

        def send(self, line: str) -> None:
            self.sent.append(line)

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            self.waited.append((pattern, timeout, idle))
            return b""

    console = Finished()
    ram.install_from_memory(cast(Any, console))

    assert console.sent == ["install"]
    # The refusal is waited for beside the completion: the installer returns to
    # the environment's shell and prints nothing more, so a wait for the
    # completion alone spends the whole idle window on a run that has ended.
    assert console.waited == [
        (
            rf"{ram.INSTALL_FINISHED}|{ram.INSTALL_STOPPED}",
            ram.INSTALL_CEILING,
            ram.INSTALL_IDLE,
        ),
        (r"# ", ram.POST_INSTALL_PATIENCE, 0.0),
    ]


def test_broken_entry_removes_its_initramfs_and_proves_that_output() -> None:
    """The one-shot remains armed; only the file its entry loads is removed."""
    from tests.vm import ram

    class Armed:
        def __init__(self, broken: bytes) -> None:
            self.broken = broken
            self.asked: list[str] = []
            self.ran: list[str] = []

        def expect_output(self, command: str, timeout: float = 0.0) -> bytes:
            self.asked.append(command)
            return (
                b"/boot/efi/gentoo-install-ram/initramfs\n"
                if len(self.asked) == 1
                else self.broken
            )

        def run(self, command: str, timeout: float = 0.0) -> None:
            self.ran.append(command)

    broken = Armed(b"INITRAMFS-BROKEN")
    ram.break_armed_environment(cast(Any, broken))
    assert broken.ran == ["rm -- /boot/efi/gentoo-install-ram/initramfs"]
    assert "find /boot /efi" in broken.asked[0]

    with pytest.raises(RuntimeError, match="was not removed"):
        ram.break_armed_environment(cast(Any, Armed(b"INITRAMFS-STILL-PRESENT")))


def test_fallback_requires_the_original_marker_and_os_identity() -> None:
    """A marker alone could come from an environment that mounted the disk."""
    from tests.vm import ram
    from tests.vm.convert import IMAGES

    class Returned:
        def __init__(self, said: bytes) -> None:
            self.said = said
            self.command = ""

        def expect_output(self, command: str, timeout: float = 0.0) -> bytes:
            self.command = command
            return self.said

    chosen = IMAGES["debian"]
    returned = Returned(b"gentoo-install-ram-fallback-system\nID=debian\n")
    ram.require_own_system(cast(Any, returned), chosen)
    assert "cat /var/lib/gentoo-install-ram-fallback-system" in returned.command
    assert 'printf \'ID=%s\\n\' "$ID"' in returned.command

    with pytest.raises(RuntimeError, match="ID=debian"):
        ram.require_own_system(
            cast(Any, Returned(b"gentoo-install-ram-fallback-system\nID=gentoo\n")),
            chosen,
        )


def test_memory_runner_boots_the_target_and_reuses_shared_state_checks() -> None:
    """The memory environment is not evidence; its installed disk is."""
    import inspect

    from tests.vm import ram

    source = inspect.getsource(ram.run_install)
    assert "boot_installed=True" in source, source
    assert "check_installed(console, installation)" in source, source
    assert "report(result, keep=True, assertions=load(configuration))" in source, source


class _CdAppearsLate:
    """A guest whose shell answers before its ATAPI devices are enumerated,
    which a Debian cloud image under KVM did at 7.3 seconds.

    The command is echoed back before its output, because a real shell echoes
    the line it was given and `expect_command` answers with everything up to
    the marker. A fake that answers only the output is what let a check match
    its own question for two revisions.
    """

    def __init__(self, ready_after: int) -> None:
        self.ready_after = ready_after
        self.tries = 0
        #: A double that discards what it was told to do cannot fail when the
        #: caller stops doing it, so every command reaches this list.
        self.ran: list[str] = []

    def run(self, command: str, timeout: float = 0.0) -> None:
        self.ran.append(command)

    def expect_command(self, command: str, timeout: float = 0.0) -> bytes:
        self.tries += 1
        code = 0 if self.tries > self.ready_after else 1
        return f"{command}\r\ndriver={code}\r\n".encode()


def test_the_driver_cd_is_waited_for_rather_than_asked_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sh` exits 2 for a script it cannot open, which reads as the installer
    refusing: the first memory-mode run failed that way with both `/dev/sr0`
    and `/dev/sr1` answering `Can't open blockdev`."""
    from typing import Any, cast

    from tests.vm.driver import wait_for_driver

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    console = _CdAppearsLate(ready_after=3)
    wait_for_driver(cast(Any, console))

    assert console.tries == 4, console.tries
    # The mount itself, not only the probe: the fake used to discard what it
    # was given, so deleting the mount left this test green on a guest that
    # would never have had a driver CD.
    from tests.vm.driver import FIND_DRIVER

    assert console.ran, "the driver CD was never mounted"
    assert all(one == FIND_DRIVER for one in console.ran), console.ran
    assert len(console.ran) == console.tries, (console.ran, console.tries)


def test_a_guest_that_never_sees_the_cd_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded: a CD that is genuinely absent has to end the run rather than
    hold it, and the message carries what the guest answered."""
    from typing import Any, cast

    from tests.vm.driver import DriverNotFound, wait_for_driver

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    console = _CdAppearsLate(ready_after=10**9)
    with pytest.raises(DriverNotFound, match="driver=1"):
        wait_for_driver(cast(Any, console), patience=0.0)


class _EchoingChannel:
    """A shell as a `Channel`: it echoes the line it was given, then answers.

    Every real shell does this, and it is what the check below exists for.
    """

    def __init__(self) -> None:
        self.pending = b""
        self.sent: list[bytes] = []

    def recv(self, size: int) -> bytes:
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)
        line = data.decode().strip()
        token = line.split("' ", 1)[1].split(";", 1)[0]
        self.pending += (
            f"{line}\r\nMARK_{token}_BEGIN\r\nRESOLVCONF-EMPTY\r\n"
            f"MARK_{token}_DONE\r\n"
        ).encode()

    def close(self) -> None:
        return None

    @property
    def closed(self) -> bool:
        return False


def test_a_check_reads_the_output_and_not_the_question(tmp_path: Path) -> None:
    """`checks()` holds commands that name both answers — `echo RESOLVCONF-OK
    || echo RESOLVCONF-EMPTY` — and the conversion runner matched the pattern
    against everything up to the marker, which begins with the shell's echo of
    the command. Every such check passed on any machine."""
    from typing import Any, cast

    from tests.vm.console import SerialConsole

    channel = _EchoingChannel()
    with (tmp_path / "console.log").open("wb") as log:
        console = SerialConsole(cast(Any, channel), log)
        command = (
            "test -s /etc/resolv.conf && echo RESOLVCONF-OK || echo RESOLVCONF-EMPTY"
        )
        said = console.expect_output(command, timeout=5.0)
        whole = console.expect_command(command, timeout=5.0)

    assert b"RESOLVCONF-OK" not in said, said
    assert b"RESOLVCONF-EMPTY" in said, said
    assert b"RESOLVCONF-OK" in whole, "the echo is what the old reading carried"


def test_the_local_reader_hands_back_lines_a_dollar_anchor_can_match(
    tmp_path: Path,
) -> None:
    """`convert.py` matches `installed.py` patterns against what this reader
    answers, and four of them anchor on `$`. A serial line ends `\r\n`, so
    those four could not match a correct machine.
    """
    from tests.vm.console import SerialConsole

    channel = _EchoingChannel()
    with (tmp_path / "console.log").open("wb") as log:
        said = SerialConsole(cast(Any, channel), log).expect_output("true", timeout=5.0)

    assert b"\r" not in said, said
    assert re.search(rb"(?m)^RESOLVCONF-EMPTY$", said), said


@pytest.mark.lab
def test_the_driver_medium_reaches_a_guest_with_no_ata_driver(tmp_path: Path) -> None:
    """The Debian genericcloud kernel builds no ATA or AHCI driver, so a
    `media=cdrom` drive gave the guest no `/dev/sr*` at all: measured inside
    one, `lsblk` listed `vda` and `vdb` and nothing else, and both the
    conversion and the memory-mode runner died in seven seconds with
    `/dev/sr0: Can't open blockdev`."""
    from tests.vm.media import MEDIA
    from tests.vm.qemu import Vm, VmSpec

    spec = VmSpec(
        medium=MEDIA["official-minimal"],
        workdir=tmp_path,
        driver_iso=tmp_path / "driver.iso",
        boot_installed=True,
    )
    argv = Vm(spec)._argv()

    assert "virtio-blk-pci,drive=driver" in argv, argv
    assert not any("media=cdrom" in one and "driver" in one for one in argv), argv


def test_the_driver_medium_is_found_by_its_label_first() -> None:
    """A device node names where this run attached it; the label names what it
    is. The guest above has the medium at `/dev/vda`, and no list of `sr`
    nodes reaches it."""
    from tests.vm.driver import FIND_DRIVER, LABEL

    assert f"/dev/disk/by-label/{LABEL}" in FIND_DRIVER, FIND_DRIVER
    assert FIND_DRIVER.index(LABEL) < FIND_DRIVER.index("/dev/sr"), FIND_DRIVER


class _MissingCommands:
    """A guest whose launcher prints the commands its preflight will refuse
    for: one bare name per line, which is what `--missing-commands` prints."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.asked: list[str] = []

    def run(self, command: str, timeout: float = 0.0) -> None:
        self.ran.append(command)

    #: What the guest still lacks when the verification runs. Empty is a
    #: machine where the package manager did what it was asked.
    absent: tuple[str, ...] = ()
    #: What `--missing-commands` prints, one bare name per line.
    missing: tuple[str, ...] = ("gpg", "gpg-agent")

    def expect_output(self, command: str, timeout: float = 0.0) -> bytes:
        self.asked.append(command)
        if command.startswith("for one in "):
            return "".join(f"absent={one}\r\n" for one in self.absent).encode()
        return "".join(f"{one}\r\n" for one in self.missing).encode()


def test_the_harness_installs_what_it_reads_the_machine_with() -> None:
    """`read_the_boot_order` printed `NO-EFIBOOTMGR` on every conversion,
    because the cloud images have no `efibootmgr` and the installer never asks
    for one — it merges `sys-boot/efibootmgr` into the target instead. That
    reading is the only place a Fedora conversion's surviving `Boot0001
    "Fedora"` was ever visible."""
    from typing import Any, cast

    from tests.vm.convert import IMAGES, install_tools

    console = _MissingCommands()
    install_tools(cast(Any, console), IMAGES["debian"], "fixtures/vm-convert.toml")

    installs = [one for one in console.ran if "apt-get" in one]
    assert len(installs) == 1, console.ran
    assert "efibootmgr" in installs[0], installs[0]


def test_the_missing_commands_are_installed_before_the_conversion() -> None:
    """`--missing-commands` prints bare names. The reading this replaces looked
    for `run: ` and for the package manager's own name, matched neither,
    installed nothing, and let the conversion reach `preflight: these commands
    are missing: gpg, gpg-agent` with exit code 4."""
    from typing import Any, cast

    from tests.vm.convert import IMAGES, install_tools

    console = _MissingCommands()
    install_tools(cast(Any, console), IMAGES["debian"], "fixtures/vm-convert.toml")

    installs = [one for one in console.ran if "apt-get" in one]
    assert len(installs) == 1, console.ran
    assert "gpg gpg-agent" in installs[0], installs[0]


def test_the_checks_come_from_the_config_the_guest_was_handed(tmp_path: Path) -> None:
    """`rewrite_fixtures` moves the mirror, the sync and, for a fixture that
    pins one, the machine's own address. `static-ip` installed on the address
    the scheduler reserved and was failed for not having the one its fixture
    names: `address 10.31.0.150/24: the installed system does not say
    '10\\.31\\.0\\.150/24'`."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.config import MirrorRegion, Sync
    from tests.vm import cluster
    from tests.vm.installed import checks

    source = Path(__file__).resolve().parents[1] / "fixtures" / "static-ip.toml"
    job = cluster.Job(name="static-ip", fixture=source)
    written = cluster.rewrite_fixtures(
        [job],
        tmp_path / "fixtures",
        MirrorRegion.CN,
        Sync.RSYNC,
        unlock_addresses={"static-ip": "10.31.0.207"},
    )
    from dataclasses import replace as _replace

    job = _replace(job, installed_config=written / source.name)

    handed = load(job.installed_config or job.fixture)
    patterns = {check.pattern for check in checks(handed)}
    assert any("10\\.31\\.0\\.207" in one for one in patterns), sorted(patterns)
    assert not any("10\\.31\\.0\\.150" in one for one in patterns), sorted(patterns)


def test_an_editing_fixture_gets_its_table_before_the_installer_runs() -> None:
    """`mbr-edit` describes a table the operator already has. `run.py` writes
    it into the qcow2 before the guest starts; the cluster's disk is made by
    the hypervisor, so nothing wrote one and the installer refused in five
    minutes: `/dev/disk/by-id/virtio-target0 did not report its partitions, so
    what table keeps cannot be counted`."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster
    from tests.vm.run import SEEDED

    assert "mbr-edit" in SEEDED, sorted(SEEDED)
    source = Path(__file__).resolve().parents[1] / "fixtures" / "mbr-edit.toml"
    selector = cluster._first_selector(load(source))

    assert selector == "/dev/disk/by-id/virtio-target0", selector
    # The seed reaches the guest as one `parted` line naming that selector.
    line = f"parted --script {selector} {' '.join(SEEDED['mbr-edit'])}"
    assert "mklabel msdos" in line and "mkpart" in line, line
    assert "/dev/vda" not in line, "the fixture's own selector, not a guessed node"


def test_the_cluster_seeds_only_the_fixtures_that_ask_for_it() -> None:
    """Every other fixture describes a table it creates, and a seeded one
    would leave the installer keeping partitions the configuration never
    mentioned."""
    import inspect

    from tests.vm import cluster

    source = inspect.getsource(cluster)
    assert "SEEDED.get(job.fixture.stem, ())" in source, "one table, read by stem"


def test_a_command_whose_package_is_named_otherwise_is_translated() -> None:
    """`apt-get ... mkfs.vfat` answers `Unable to locate package mkfs.vfat`
    and installs **nothing at all**, including the packages it did recognise.
    The memory-mode arming then refused with `--lowram cannot arm a one-shot
    boot entry on this machine`, whose only missing piece was `efibootmgr`."""
    from typing import Any, cast

    from tests.vm.convert import IMAGES, install_tools

    console = _MissingCommands()
    console.missing = ("gpg", "mkfs.vfat")
    install_tools(cast(Any, console), IMAGES["debian"], "fixtures/vm-ram.toml")

    installs = [one for one in console.ran if "apt-get" in one]
    assert len(installs) == 1, console.ran
    assert "dosfstools" in installs[0], installs[0]
    assert "mkfs.vfat" not in installs[0], installs[0]
    # And the guest is asked for the command, not for the package: `command -v
    # dosfstools` is false on a machine that has `mkfs.vfat`, which is what
    # the package installed.
    asked = [one for one in console.asked if one.startswith("for one in ")]
    assert len(asked) == 1, console.asked
    assert "mkfs.vfat" in asked[0], asked[0]
    assert "dosfstools" not in asked[0], asked[0]


def test_a_package_manager_that_installed_nothing_stops_the_run() -> None:
    """It exits zero for a name it knows and non-zero for one it does not, and
    either way the run reads the same. What the guest has is what decides."""
    from typing import Any, cast

    import pytest as _pytest

    from tests.vm.media import MediaError
    from tests.vm.convert import IMAGES, install_tools

    console = _MissingCommands()
    console.absent = ("efibootmgr", "dosfstools")
    with _pytest.raises(MediaError, match="efibootmgr"):
        install_tools(cast(Any, console), IMAGES["debian"], "fixtures/vm-ram.toml")


def test_a_long_command_does_not_fill_the_whole_verdict() -> None:
    """`vm-zram` ended a round with a mirror probe naming eight URLs, and the
    command alone filled all 300 bytes a verdict is cut to: what the guest was
    doing was written down and why it stopped was not."""
    from tests.vm import cluster
    from tests.vm.console import ConsoleTimeout

    probe = "for one in " + " ".join(
        f"https://mirror{n}.example.edu.cn/gentoo/releases/amd64/autobuilds/"
        "latest-stage3-amd64-systemd.txt"
        for n in range(8)
    )
    with pytest.raises(ConsoleTimeout) as raised:
        with cluster._naming(probe):
            raise ConsoleTimeout("never matched 'MARK_9_DONE', 121s of 120s elapsed")

    said = str(raised.value)[: cluster.OUTCOME_BYTES]
    assert "never matched 'MARK_9_DONE'" in said, said
    assert said.startswith("never matched"), said
    assert len(probe) > cluster.OUTCOME_BYTES, "the case this exists for"


def test_the_boot_order_is_read_with_the_same_tools_both_times() -> None:
    """The cloud images ship no `efibootmgr`, and `install_tools` puts one on
    the guest. A first reading taken before that answered `NO-BOOTORDER` and
    the second `BootOrder: 0003,0000,0004`, so the run reported the default
    entry as moved by a machine that had not moved it."""
    import inspect

    from tests.vm import ram

    source = inspect.getsource(ram.run_install)
    installed = source.index("install_tools(")
    arming = source.index("arm_and_confirm(")
    confirmed = inspect.getsource(ram.arm_and_confirm)
    first = confirmed.index("before = read_the_default_entry(")

    assert installed < arming, "the tools go on before the arming"
    assert confirmed.index("arm(console") > first, "and the reading before the arming"


def test_the_lowram_check_logs_in_where_the_medium_asks_for_one() -> None:
    """Alpine's netboot console asks for a login and the CJK medium logs root
    in by itself. The first screen is in root's profile either way, so the
    ninth `--lowram` run reached `Loading user settings from
    /gentoo-install.apkovl.tar.gz: ok.` and then `(none) login:` and waited
    five minutes at a prompt nobody answered."""
    import inspect

    from tests.vm import ram

    source = inspect.getsource(ram.came_up)
    assert 'console.send("root")' in source, source
    assert source.index('mode == "lowram"') < source.index('console.send("root")')
    assert source.index("PAYLOAD_SPEAKS") > source.index('console.send("root")')


def test_the_mirror_check_cannot_match_its_own_command() -> None:
    """It asked for `print('BYTES', …)` and looked for `BYTES` in a reply that
    begins with the shell's echo of that line, so a machine that fetched
    nothing passed. The count is what the machine produces."""
    import inspect
    import re as _re

    from tests.vm import netcheck

    source = inspect.getsource(netcheck)
    command = _re.search(r"print\('MIRROR_BYTES=%d'[^\n]*", source)
    assert command is not None, source
    assert not _re.search(rb"MIRROR_BYTES=[1-9][0-9]*", command.group(0).encode()), (
        command.group(0)
    )
    assert 'rb"MIRROR_BYTES=[1-9][0-9]*"' in source, "and the check wants a count"


def test_a_run_of_one_half_does_not_claim_the_other() -> None:
    """`--part install` ended with `proved a broken one-shot returns twice`
    and nothing in that run had armed a broken entry. A closing line is a
    claim, and a claim about a guest that was never started is the shape of
    failure this suite exists to catch."""
    from tests.vm import ram

    assert set(ram.RAN) == {"install", "fallback", "bypass", "both"}
    assert "one-shot" not in ram.RAN["install"], ram.RAN["install"]
    assert "installed Gentoo" not in ram.RAN["fallback"], ram.RAN["fallback"]
    # And the whole run still says both, because it did both.
    assert "installed Gentoo" in ram.RAN["both"]
    assert "one-shot" in ram.RAN["both"]


def test_the_result_disk_is_named_and_not_numbered() -> None:
    """An install that had finished ended with

        tar: /dev/vda: Cannot write: Operation not permitted

    because the driver CD is a virtio disk too and took `/dev/vda`, leaving
    the results written at a read-only device. The targets already carry a
    serial for the same reason; the result disk now does as well."""
    from tests.vm.qemu import Firmware, Vm, VmSpec
    from tests.vm.media import MEDIA
    from tests.vm.results import RESULT_DEVICE, RESULT_SERIAL, collect_command

    spec = VmSpec(
        medium=MEDIA["official-minimal"],
        workdir=Path("/tmp"),
        firmware=Firmware.BIOS,
        disks=(Path("/tmp/result.img"),),
        driver_iso=Path("/tmp/driver.iso"),
        boot_installed=True,
    )
    argv = Vm(spec)._argv()

    named = [one for one in argv if one.startswith("virtio-blk-pci,drive=disk0")]
    assert named == [f"virtio-blk-pci,drive=disk0,serial={RESULT_SERIAL}0"], argv
    # And the command writes to the name, not to whichever disk came first.
    assert "/dev/vda" not in collect_command("/run/vm-result")
    assert RESULT_DEVICE in collect_command("/run/vm-result")
def test_bypass_requires_the_default_entry_to_move() -> None:
    """The one-shot's check is `_default_changed` being false; this path's is
    the same reading being true. `--bypass` exists for firmware that drops a
    one-shot, and an arming that left the default alone would look like a pass
    while the machine boots what it always did."""
    from tests.vm import ram

    before = b"saved_entry=Debian\n"
    replaced = b"saved_entry=gentoo-install memory environment\n"
    assert ram._default_changed(before, replaced)
    assert not ram._default_changed(before, before)
    # The closing line says what this half measured and not what the others do.
    assert "default entry" in ram.RAN["bypass"], ram.RAN["bypass"]
    assert "one-shot" not in ram.RAN["bypass"], ram.RAN["bypass"]


def test_the_bypass_runner_asks_for_two_boots_and_names_the_flag() -> None:
    """A single boot is what `--ram` alone already proves. The second boot is
    the difference: a replaced default entry comes up in the environment
    again, and that is why this path leaves an unbootable machine when the
    environment does not come up."""
    import inspect

    from tests.vm import ram

    source = inspect.getsource(ram.run_bypass)
    assert 'for boot in ("first", "second")' in source, source
    assert "arm_bypass" in source, source
    assert "--bypass" in inspect.getsource(ram.arm_bypass)


def test_the_bypass_runner_reaches_a_shell_before_it_sends_a_command() -> None:
    """The delivered screen reads a whole line as its answer, so a marked
    command typed there is consumed: the second `--bypass` reboot ended at
    `never matched 'MARK_14_DONE'` with `nothing was changed` on the console."""
    import inspect

    from tests.vm import ram

    source = inspect.getsource(ram.run_bypass)
    assert "leave_the_first_screen(console)" in source, source
    left = source.index("leave_the_first_screen(console)")
    # Last in the loop body, so the next pass's `reboot` reaches a shell: the
    # reboot is the first statement of the pass that follows it.
    assert source.index("came_up(console, mode)") < left, source
    assert source.index('console.run("reboot"') < left, source
    assert 'for boot in ("first", "second")' in source, source
    answered = inspect.getsource(ram.leave_the_first_screen)
    # What it sends, not the word it used to send: the profile stopped
    # accepting `shell` and no test noticed, because this one held the literal.
    assert "console.send(netboot.DECLINES)" in answered, answered


def test_a_conversion_reads_its_exit_code_and_not_the_echo() -> None:
    """`expect_command` keeps the line the shell echoed, and the tenth command
    carries `MARK_10_BEGIN`, so `b"0" not in code` was false for every exit
    code a guest could have written. A conversion that failed was reported as
    one that worked, and then the boot check ran against a machine nobody had
    converted."""
    import itertools

    from tests.vm.console import SerialConsole, command_begin, command_done

    class Echoing(SerialConsole):
        """A shell: it repeats the line it was given, then answers it."""

        def __init__(self, answer: bytes) -> None:
            self._buffer = b""
            self._bytes_read = 0
            self.answer = answer
            self.sent: list[str] = []
            # From ten, which is where the marker in the echo first carries a
            # zero of its own.
            self._tokens = itertools.count(10)

        def send(self, line: str) -> None:
            self.sent.append(line)
            token = len(self.sent) + 9
            self._buffer += (
                f"[root@beforeconvert ~]# {line}\r\n".encode()
                + f"{command_begin(token)}\r\n".encode()
                + self.answer
                + f"{command_done(token)}\r\n".encode()
            )

        def _read_once(self) -> None:
            return None

    failed = Echoing(b"4\r\n")
    said = failed.expect_output("cat /tmp/gentoo-install-results/install.rc", timeout=1.0)
    assert said.strip() == b"4", said
    assert said.strip() != b"0", "the exit code, not the marker in the echo"

    # And what the old reading saw: `printf 'MARK_%s_BEGIN\n' 10` puts the
    # token in the line the shell repeats, so the echo alone carries a zero
    # and `b"0" in code` was true before the guest said anything.
    echoed = failed.sent[0]
    assert "' 10;" in echoed and "0" in echoed, echoed

    worked = Echoing(b"0\r\n")
    assert worked.expect_output(
        "cat /tmp/gentoo-install-results/install.rc", timeout=1.0
    ).strip() == b"0"

    # And the conversion reads it that way: the fake above proves what
    # `expect_output` answers, not what `convert.py` asks for.
    import ast

    tree = ast.parse(Path("tests/vm/convert.py").read_text())
    read = [
        node.value.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(one, ast.Name) and one.id == "code" for one in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
    ]
    assert read == ["expect_output"], read


def test_no_installed_check_is_ever_read_with_its_own_echo() -> None:
    """Console transports read only framed output, and a bounded pattern must
    not match the shell line that framed it.
    """
    import ast
    import re

    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    echoed = [
        check.name
        for check in checks(load(Path("tests/fixtures/zfs-zbm.toml")))
        if re.search(check.pattern, f"root@livecd ~ # {check.command}\r\n")
    ]
    assert not echoed, f"an installed check matched its shell echo: {echoed}"

    # `cluster.py` and `convert.py` ask over a console; `run.py` redirects each
    # answer into a file, where no echo reaches.
    for module, function in (("cluster", "boot_and_check"), ("convert", "check_installed")):
        source = Path(f"tests/vm/{module}.py").read_text()
        tree = ast.parse(source)
        wanted = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == function
        )
        asked = {
            node.func.attr
            for node in ast.walk(wanted)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("expect_")
        }
        assert "expect_command" not in asked, f"{module}.{function} reads the echo: {asked}"
        assert "expect_output" in asked, f"{module}.{function} asks nothing: {asked}"


def test_the_firmware_and_its_variable_store_are_the_same_build() -> None:
    """A 4 MB `OVMF_CODE` handed the 2 MB `OVMF_VARS.fd` keeps its variables
    in memory and writes none of them back. A Fedora conversion wrote
    `Boot0002 Gentoo` first in `BootOrder`, powered off, and the store on disk
    was byte-identical to the template it was copied from, so the next boot
    scanned the ESP and started `\\EFI\\fedora\\shimx64.efi` instead."""
    from tests.vm.qemu import OVMF_CODE, OVMF_VARS, PFLASH_FORMAT

    def build(path: Path) -> str:
        # `OVMF_CODE_4M.qcow2` and `OVMF_VARS_4M.qcow2` against `OVMF_CODE.fd`
        # and `OVMF_VARS.fd`: whatever follows the role is the build.
        parts = path.stem.split("_")
        return "_".join(parts[2:])

    assert build(OVMF_CODE) == build(OVMF_VARS), (OVMF_CODE.name, OVMF_VARS.name)

    # And qemu is told the format each one is actually in.
    assert PFLASH_FORMAT[OVMF_CODE.suffix] == "qcow2"
    assert PFLASH_FORMAT[OVMF_VARS.suffix] == PFLASH_FORMAT[OVMF_CODE.suffix]


def test_a_lowram_install_failure_carries_the_installers_own_log() -> None:
    """`FAIL the memory environment did not install: … espfs labelled ESP` came
    with `/proc/partitions` holding `vdc1` and `vdc2`, `/dev` holding neither,
    and no way to tell whether `wait_for` had fallen back to `mdev -s` or what
    stood between the node appearing and the formatter finding nothing. The
    installer writes every command it runs to its own log, and the diagnostic
    read everything except that."""
    import inspect

    from gentoo_install.cli import WORK
    from gentoo_install.exec.report import RunFile
    from tests.vm import ram

    asked = inspect.getsource(ram._what_the_disk_holds)
    assert f"{WORK}/{RunFile.LOG.value}" in asked, asked

    # And it reads between the markers, because several of these commands name
    # their own answer.
    assert "expect_output" in asked and "expect_command" not in asked, asked


def test_a_fixture_whose_site_is_the_test_keeps_it() -> None:
    """`vm-binhost-fallback` names `xtom-hk`, whose binary package index
    answers 404, and it is green only when Portage drops that host and
    compiles from source. `--site nju` rewrote it to a working mirror, so the
    guest installed 42 minutes of binary packages and the verdict said the
    degradation path works."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.config import MirrorRegion, Sync
    from tests.vm import cluster

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    kept = cluster.Job(name="vm-binhost-fallback", fixture=fixtures / "vm-binhost-fallback.toml")
    moved = cluster.Job(name="vm-btrfs", fixture=fixtures / "vm-btrfs.toml")
    pinned = load(kept.fixture).portage.mirrors.site
    assert pinned and pinned != "nju", "the fixture pins a site of its own"

    import tempfile

    with tempfile.TemporaryDirectory() as where:
        written = cluster.rewrite_fixtures(
            [kept, moved], Path(where) / "fixtures", MirrorRegion.CN, Sync.WEBRSYNC, site="nju"
        )
        assert load(written / kept.fixture.name).portage.mirrors.site == pinned
        assert load(written / moved.fixture.name).portage.mirrors.site == "nju"


def test_every_fixture_that_keeps_its_site_pins_one() -> None:
    """A name in the table whose fixture does not pin a site keeps nothing and
    reads as protection that is not there."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    for name in cluster.KEEPS_ITS_SITE:
        path = fixtures / f"{name}.toml"
        assert path.is_file(), name
        assert load(path).portage.mirrors.site, name


def test_a_fixture_that_must_degrade_is_failed_when_it_did_not() -> None:
    """`vm-binhost-fallback` was green because the install finished, which it
    does whether or not the host it names ever failed: 42 minutes of binary
    packages downloaded from a working mirror, and the verdict read as proof
    that the degradation path works. The journal records what a run gave up
    on, so the fixture's own premise is now part of its verdict."""
    import json

    from gentoo_install.plan.portage import BINARY_PACKAGES
    from tests.vm import cluster

    degraded = json.dumps({"event": "degraded", "what": BINARY_PACKAGES, "reason": "404"})
    other = json.dumps({"event": "degraded", "what": "something else", "reason": "-"})

    assert cluster._degradation_missing("vm-btrfs", {}) == "", "no promise, no requirement"
    assert cluster._degradation_missing(
        "vm-binhost-fallback", {"install.jsonl": degraded.encode()}
    ) == ""

    for held in ({}, {"install.jsonl": b""}, {"install.jsonl": other.encode()}):
        refused = cluster._degradation_missing("vm-binhost-fallback", held)
        assert refused, held
        assert BINARY_PACKAGES in refused, refused

    # A partial last line is what a run killed mid-write leaves behind, and it
    # must not hide the entry above it.
    partial = degraded.encode() + b'\n{"event": "degr'
    assert cluster._degradation_missing("vm-binhost-fallback", {"install.jsonl": partial}) == ""


def test_every_fixture_that_must_degrade_keeps_the_site_that_makes_it() -> None:
    """A fixture required to degrade is required to reach the host that fails
    it, so `--site` must not move it: the two tables have to agree or the
    requirement is one no run can meet."""
    from tests.vm import cluster

    assert set(cluster.MUST_DEGRADE) <= cluster.KEEPS_ITS_SITE, cluster.MUST_DEGRADE


def test_an_image_install_is_given_a_disk_to_write_its_image_onto() -> None:
    """Nothing mounted anything at the image's path, so it landed on the live
    medium's tmpfs and both runners ran out of the guest's memory. The spare
    target disk was already attached — `_target_paths` asks for one even when
    the configuration has no `existing` node — and nothing used it."""
    from gentoo_install.exec.config import load
    from tests.vm import run as runner

    said: list[str] = []

    class Console:
        def run(self, command: str, timeout: float = 0.0) -> None:
            said.append(command)

    image = load(Path(__file__).resolve().parents[1] / "fixtures" / "vm-image.toml")
    runner.mount_scratch_for_image(cast(Any, Console()), image)

    where = str(Path(image.disk.image).parent)
    assert said == [
        f"mkfs.ext4 -F -L scratch {runner.SCRATCH_DISK}",
        f"mkdir -p {where}",
        f"mount {runner.SCRATCH_DISK} {where}",
    ], said

    # A configuration that installs onto a disk is left alone: formatting the
    # target it is about to partition would destroy the install.
    said.clear()
    ordinary = load(Path(__file__).resolve().parents[1] / "fixtures" / "vm-btrfs.toml")
    runner.mount_scratch_for_image(cast(Any, Console()), ordinary)
    assert said == [], said


def test_the_image_fixture_writes_where_the_runner_mounts() -> None:
    """Two places name the path and they have to agree: the fixture writes the
    image and the runner mounts the disk under it."""
    import inspect

    from gentoo_install.exec.config import load
    from tests.vm import run as runner

    image = load(Path(__file__).resolve().parents[1] / "fixtures" / "vm-image.toml")
    mounted = inspect.getsource(runner.mount_scratch_for_image)
    assert "PurePosixPath(installation.disk.image).parent" in mounted, mounted
    assert image.disk.image.startswith("/mnt/"), image.disk.image


#: `zpool status` on this workstation, whose rpool is a mirror and whose tank
#: is a single device. The indentation is a tab and two spaces, which is why
#: the vdev pattern cannot be anchored on a fixed number of columns.
_ZPOOL_STATUS = (
    "  pool: rpool\n state: ONLINE\nconfig:\n\n"
    "\tNAME                                                STATE     READ WRITE CKSUM\n"
    "\trpool                                               ONLINE       0     0     0\n"
    "\t  mirror-0                                          ONLINE       0     0     0\n"
    "\t    nvme-WD_BLACK_SN850X_1000GB_233204801130-part3  ONLINE       0     0     0\n"
    "\t    nvme-WD_BLACK_SN850X_1000GB_24041D800840-part3  ONLINE       0     0     0\n"
    "\nerrors: No known data errors\n"
)

#: The same command on a pool with no redundancy: the devices hang directly
#: off the pool line and there is no vdev row at all.
_ZPOOL_STATUS_STRIPE = (
    "  pool: tank\n state: ONLINE\nconfig:\n\n"
    "\tNAME                                      STATE     READ WRITE CKSUM\n"
    "\ttank                                      ONLINE       0     0     0\n"
    "\t  ata-ST8000NM017B-2TJ103_WWZ472M1-part2  ONLINE       0     0     0\n"
    "\nerrors: No known data errors\n"
)


def test_a_redundant_pool_is_checked_by_the_vdev_zpool_reports() -> None:
    """`zpool create` builds a stripe from the same devices when the keyword
    is dropped, and that pool carries the dataset, mounts, boots and passes
    every other check here. Only `zpool status` says which one was built."""
    import re

    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    def topology(fixture: str) -> tuple[str, str] | None:
        for one in checks(load(Path(f"tests/fixtures/{fixture}.toml"))):
            if one.name == "pool topology":
                return one.command, one.pattern
        return None

    mirrored = topology("vm-zfs-mirror")
    assert mirrored is not None
    command, pattern = mirrored
    assert command == "zpool status rpool", command
    assert re.search(pattern, _ZPOOL_STATUS)
    # The control, on the same machine's other pool: a stripe has no vdev row,
    # so a pool built without the keyword fails this check.
    assert not re.search(pattern, _ZPOOL_STATUS_STRIPE)

    raidz = topology("vm-raidz")
    assert raidz is not None and raidz[1] != pattern, raidz
    assert not re.search(raidz[1], _ZPOOL_STATUS), "raidz1 is not what a mirror reports"

    # A pool with no redundancy has nothing to check: the rule fires on the
    # topology, not on ZFS.
    assert topology("vm-zfs") is None


def test_zram_is_checked_on_the_device_rather_than_the_file_that_asks_for_one() -> None:
    """`zram-generator.conf` and `/etc/conf.d/zram-init` are what the
    installer writes, and a machine can hold either with no zram device at
    all: the package can be missing and the service can be off."""
    import re

    from dataclasses import replace

    from gentoo_install.exec.config import load
    from gentoo_install.model.config import InstallConfig
    from tests.vm.installed import checks

    def zram(installation: InstallConfig) -> tuple[str, str] | None:
        for one in checks(installation):
            if one.name == "zram":
                return one.command, one.pattern
        return None

    installation = load(Path("tests/fixtures/vm-zram.toml"))
    found = zram(installation)
    assert found is not None
    _, pattern = found
    # `swapon --show=NAME --noheadings` and `zramctl --noheadings --output
    # NAME` on this workstation, which runs zram swap.
    assert re.search(pattern, "/dev/zram0\n/dev/dm-0\n/dev/dm-1\n")
    assert re.search(pattern, "/dev/zram0\n")
    # Controls: a machine swapping to a partition, and one with nothing.
    assert not re.search(pattern, "/dev/dm-0\n/dev/dm-1\n")
    assert not re.search(pattern, "")
    assert zram(replace(installation, system=replace(installation.system, zram=None))) is None


#: `rc-update show default` on `vm-openrc-desktop`, which asks for sshd, and
#: on `openrc-sdboot`, which does not. Both copied from the guests.
_RC_UPDATE_WITH_SSHD = (
    "       NetworkManager | default\n"
    "               cronie | default\n"
    "                 dbus | default\n"
    "      display-manager | default\n"
    "                local | default\n"
    "             netmount | default\n"
    "                 sshd | default\n"
    "             sysklogd | default\n"
)
_RC_UPDATE_WITHOUT_SSHD = (
    "               cronie | default\n"
    "               dhcpcd | default\n"
    "                local | default\n"
    "             netmount | default\n"
    "             sysklogd | default\n"
)


def test_a_machine_asked_for_sshd_is_asked_whether_it_has_one() -> None:
    """Sixteen fixtures set `system.sshd` and no check looked at it, so a
    machine that came up without a daemon passed. `UnitFileState` rather than
    `list-unit-files`: on a serial console `systemctl` colours its output and
    prints `\x1b[0;4msshd.service`, which no pattern anchored at the start of
    a line matches."""
    import re

    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    def sshd(fixture: str) -> tuple[str, str] | None:
        for one in checks(load(Path(f"tests/fixtures/{fixture}.toml"))):
            if one.name == "sshd":
                return one.command, one.pattern
        return None

    openrc = sshd("vm-openrc-desktop")
    assert openrc is not None
    command, pattern = openrc
    assert command == "rc-update show default", command
    assert re.search(pattern, _RC_UPDATE_WITH_SSHD)
    assert not re.search(pattern, _RC_UPDATE_WITHOUT_SSHD)

    systemd = sshd("vm-greetd")
    assert systemd is not None
    command, pattern = systemd
    assert "UnitFileState" in command, command
    # `systemctl show --property=UnitFileState --value sshd.service` on this
    # workstation, which runs one.
    assert re.search(pattern, "enabled\n")
    assert not re.search(pattern, "disabled\n")
    assert not re.search(pattern, "")

    # The rule fires on the configuration: a machine that asked for no daemon
    # is not failed for having none.
    assert sshd("openrc-sdboot") is None


def test_a_declared_user_is_read_back_from_the_machine() -> None:
    """`btrfs-luks` names a user with three groups and a shell, and the
    guest`s console log never mentioned the name: a machine that created no
    account passed. The uid fields are the machine`s own answer, which the
    command cannot supply."""
    import re

    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    def named(fixture: str, name: str) -> str | None:
        for one in checks(load(Path(f"tests/fixtures/{fixture}.toml"))):
            if one.name == name:
                return one.pattern
        return None

    account = named("btrfs-luks", "user zakk")
    assert account is not None
    # `getent passwd zakk` on this workstation, with the shell the fixture
    # asks for in place of the one this machine has.
    assert re.search(account, "zakk:x:1000:1000:Zakk:/home/zakk:/bin/bash\n")
    # The controls: no account at all, and an account with another shell.
    assert not re.search(account, "")
    assert not re.search(account, "zakk:x:1000:1000:Zakk:/home/zakk:/usr/bin/zsh\n")
    # A name that only appears as a group, which `getent passwd` would not
    # print and a looser pattern would have accepted.
    assert not re.search(account, "wheel:x:10:zakk\n")

    groups = named("btrfs-luks", "user zakk groups")
    assert groups is not None
    # `id -nG zakk` on this workstation.
    assert re.search(groups, "zakk wheel audio video usb input kvm render libvirt\n")
    assert not re.search(groups, "zakk wheel audio usb input kvm libvirt\n")

    # A fixture whose user has no group list gets no group check, and one
    # with no user gets neither.
    assert named("zbm-unlock", "user zakk") is not None
    assert named("zbm-unlock", "user zakk groups") is None
    assert named("ext3", "user zakk") is None


def test_the_zfs_unlock_proof_travels_in_the_unlock_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ZFSBootMenu's dropbear lives in the image it boots from, so unlocking
    hands over to the kernel it loads and the daemon goes with it. `zbm-unlock`
    unlocked and its second connection, carrying `zfs get keystatus`, hung
    until the timeout and left `TimeoutExpired` as the whole verdict."""
    from gentoo_install.exec.config import load
    from tests.vm import run as runner

    seen: dict[str, object] = {}
    opened: list[str] = []

    class FakeProcess:
        returncode = 0

        def communicate(
            self, text: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            seen["stdin"] = text
            return ("Enter passphrase:\navailable\n", "")

    def fake_popen(argv: list[str], **_: object) -> FakeProcess:
        seen["argv"] = argv
        return FakeProcess()

    def refuse_a_second_connection(*arguments: object, **_: object) -> None:
        opened.append(str(arguments))
        raise AssertionError("the proof must not open a second connection")

    monkeypatch.setattr(runner, "wait_for_unlock_daemon", lambda *a, **k: None)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(runner, "ssh", refuse_a_second_connection)

    installation = load(Path("tests/fixtures/zbm-unlock.toml"))
    assert runner.remote_unlock(Path("key"), 2222, installation) == "available"
    asked = cast(list[str], seen["argv"])[-1]
    assert "zfs load-key -a" in asked and "keystatus" in asked, asked
    # The control: a second connection would be the race this closes.
    assert not opened, opened


def test_the_menu_walk_makes_its_target_under_its_own_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`create_target` refuses a path outside the directory it confines to, and
    the menu walk keeps its guests under `lab/vm/tui`. Without a root of its
    own `python3 -m tests.vm.tui` raised before it booted anything, so the one
    check of the interface on a real 80-column console could not run."""
    import ast
    import inspect
    import textwrap

    import pytest

    from tests.vm import tui
    from tests.vm.run import DEFAULT_TARGET_SIZE, create_target

    mine = tmp_path / "walk"
    mine.mkdir()
    # `qemu-img` is not on a CI runner, and this test is about the guard, not
    # about making an image: the call is recorded instead of performed.
    launched: list[list[str]] = []

    def records(command: list[str], **named: object) -> object:
        launched.append(command)
        return object()

    monkeypatch.setattr("tests.vm.run.subprocess.run", records)
    made = create_target(mine / "target.qcow2", DEFAULT_TARGET_SIZE, root=tmp_path)
    assert made == mine / "target.qcow2"
    assert any(command[:2] == ["qemu-img", "create"] for command in launched), launched

    # The named root is a boundary, not a switch that turns the guard off.
    outside = tmp_path.parent / "not-the-walk.qcow2"
    outside.write_bytes(b"stands for a downloaded cloud image")
    with pytest.raises(ValueError, match="deletes what it is given"):
        create_target(outside, DEFAULT_TARGET_SIZE, root=mine)
    assert outside.read_bytes().startswith(b"stands for")

    # And the walk asks for it: the default root would refuse its own workdir.
    body = ast.parse(textwrap.dedent(inspect.getsource(tui.main))).body[0]
    assert isinstance(body, ast.FunctionDef)
    asked = [
        call
        for call in ast.walk(body)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "create_target"
    ]
    assert len(asked) == 1, ast.dump(body)
    assert [one.arg for one in asked[0].keywords] == ["root"], ast.dump(asked[0])


def test_only_the_driver_module_names_a_cd_device() -> None:
    """`FIND_DRIVER` tries the label first and two device nodes after it,
    because a medium that boots from a squashfs has no `/dev/sr*` at all. Two
    runners mounted `/dev/sr1` themselves instead, and the menu walk died on
    `Can't open blockdev` with the tarball never unpacked."""
    import re
    from pathlib import Path as _Path

    here = _Path(__file__).resolve().parents[1] / "vm"
    guilty: list[str] = []
    for module in sorted(here.glob("*.py")):
        if module.name == "driver.py":
            continue
        for number, line in enumerate(module.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"mount\b[^\n]*/dev/sr", line):
                guilty.append(f"{module.name}:{number}: {line.strip()}")
    assert not guilty, guilty


def test_the_walk_leaves_a_row_with_the_key_every_widget_answers() -> None:
    """Two leave keys were tried on a real console and both were wrong.
    Escape is Cancel, and `app.run` answers Cancel with the prompt that ends
    the install: the recording held two screens and nineteen rows reported as
    never opening. Backspace deletes inside a field the row arrived with
    filled, and that walk left the machine calling itself `gento`."""
    import inspect

    from tests.vm import tui

    walking = inspect.getsource(tui.walk)
    left = [line.strip() for line in walking.splitlines() if "send_raw" in line]
    assert 'console.send_raw("\\x1b[D")' in left, left
    assert not any(r"\x7f" in line for line in left), left
    assert 'console.send_raw("\\x1b")' not in left, left


def test_a_dd_fixture_names_the_runner_that_can_run_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`tests.vm.dd` generates the source image per run and stages it onto the
    CD. Handed to the ordinary runner the fixture builds a guest, boots it and
    stops at `image source ... is not a regular file` minutes later."""
    from tests.vm import run as runner

    code = runner.main(["--install", "fixtures/vm-dd-raw.toml", "--dry-run"])
    said = capsys.readouterr()
    assert code == 1, said
    assert "tests.vm.dd" in said.err, said

    # The control names a fixture this runner does own, with the firmware
    # deliberately wrong so it is refused by the check below this one rather
    # than booting a machine: the dd message belongs to the dd branch alone.
    code = runner.main(["--install", "fixtures/ext4-bios.toml", "--firmware", "uefi"])
    also = capsys.readouterr()
    assert code == 1, also
    assert "tests.vm.dd" not in also.err, also
    assert "--firmware says uefi" in also.err, also



def test_the_installed_login_answers_the_disk_before_it_answers_the_login() -> None:
    """An encrypted root asks dracut's passphrase prompt before anything
    offers a login. Waiting for `login:` alone spent the whole patience at
    that prompt and reported `s2` as a machine that never booted."""
    import re

    from tests.vm import cluster
    from tests.vm.console import (
        DISK_PASSPHRASE,
        PASSPHRASE_ATTEMPTS,
        ConsoleTimeout,
    )
    from tests.vm.proxmox import ProxmoxError

    class Booting:
        """A console that asks for the passphrase, then offers a login.

        It matches the caller's pattern against what the machine is printing,
        the way the real one does. A double that answers whatever is asked
        cannot fail: the first version of this test stayed green with the
        passphrase alternative taken back out.
        """

        def __init__(self, prompts: int) -> None:
            self.left = prompts
            self.sent: list[str] = []

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            printing = (
                b"Please enter passphrase for disk vda2:"
                if self.left
                else b"localhost login:"
            )
            if re.search(pattern.encode(), printing) is None:
                raise ConsoleTimeout(f"never matched {pattern!r}: {printing!r}")
            if self.left:
                self.left -= 1
            return printing

        def send(self, text: str) -> None:
            self.sent.append(text)

        def send_raw(self, keys: str) -> None:
            raise AssertionError("this path types no raw keys")

        def snapshot(self, seconds: float) -> bytes:
            raise AssertionError("this path reads through expect only")

        def close(self) -> None:
            return None

        @property
        def closed(self) -> bool:
            return False

    for prompts in (0, 1, 3):
        console = Booting(prompts)
        cluster.reach_the_login_past_any_passphrase(console)
        assert console.sent == [DISK_PASSPHRASE] * prompts, console.sent

    # A prompt that never stops is the passphrase being wrong, not a layout
    # with many devices: it is raised, not answered until the ceiling.
    forever = Booting(PASSPHRASE_ATTEMPTS + 1)
    with pytest.raises(ProxmoxError, match="never offered a login"):
        cluster.reach_the_login_past_any_passphrase(forever)
    assert len(forever.sent) == PASSPHRASE_ATTEMPTS, forever.sent


def test_the_pool_scan_is_bounded_and_outlives_its_own_window() -> None:
    """`zpool import` with no `-d` reads a label from every block device.
    `gi-s2` carries two encrypted 40 GiB disks and that scan was still running
    when the default 120s expect gave up, so the LUKS boot check died on the
    scan rather than on its answer."""
    import inspect

    from tests.vm import cluster

    source = inspect.getsource(cluster.make_the_installed_system_speak)
    assert "zpool import" in source, source
    scan = next(
        one
        for one in source.splitlines()
        if "zpool import" in one and not one.lstrip().startswith("#")
    )
    assert "timeout " in scan, scan

    # The window the caller passes has to outlast the bound the shell gets,
    # or the expect gives up while the command is still allowed to run. Read
    # off the call, not restated: `POOL_SCAN_PATIENCE + 60 > POOL_SCAN_PATIENCE`
    # is true of every number and holds nothing.
    called = next(
        one for one in source.splitlines() if "timeout=POOL_SCAN_PATIENCE" in one
    )
    added = re.search(r"timeout=POOL_SCAN_PATIENCE \+ ([0-9.]+)", called)
    assert added is not None, called
    assert float(added.group(1)) > 0, called
    signature = inspect.signature(cluster.Reconnecting.expect_output)
    default = signature.parameters["timeout"].default
    assert cluster.POOL_SCAN_PATIENCE > default, (cluster.POOL_SCAN_PATIENCE, default)


def test_an_installed_check_is_read_between_the_markers() -> None:
    """Commands that emit success markers keep their readers on framed output."""
    import ast
    import re
    from pathlib import Path as _Path

    from tests.vm import installed

    markers = ("RESOLVCONF-OK", "EMERGE-OK", "NO-FAILED-UNITS", "CPUFLAGS-ALL-KNOWN")
    spelled = [
        one.name
        for one in installed.checks(_a_conversion())
        if any(
            marker in one.command and re.search(one.pattern, f"{marker}\n")
            for marker in markers
        )
    ]
    assert {"resolver", "portage", "failed", "cpu-flags"} <= set(spelled), spelled

    root = _Path(__file__).resolve().parents[1] / "vm"
    echoed = []
    for source in (root / "convert.py", root / "cluster.py", root / "run.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name != "expect_command":
                continue
            reads = ast.dump(node)
            if "check" in reads and "command" in reads:
                echoed.append(f"{source.name}:{node.lineno}")
    assert not echoed, f"an installed check read with its own echo: {echoed}"


def _a_conversion() -> "InstallConfig":
    """A configuration whose checks include the ones that spell their answer."""
    from tests.unit.layouts import config

    return config()


def test_the_resolver_file_replaces_the_symlink_and_the_daemon_that_owns_it() -> None:
    """On an installed systemd system `/etc/resolv.conf` points into
    `/run/systemd/resolve/`, so writing through it lasts until the daemon
    rewrites its own file. The eighth conversion's guest reached the gateway
    and `223.5.5.5` and still answered `fail fail fail fail fail` to every
    lookup of `mirrors.ustc.edu.cn`."""
    from tests.vm import cluster

    written = cluster.use_our_resolvers()
    at = written.index("> /etc/resolv.conf")
    before = written[:at]
    assert "rm -f /etc/resolv.conf" in before, written
    assert "systemd-resolved" in before, written
    # The removal comes after the daemon is stopped, or the daemon puts a new
    # symlink back between the two commands.
    assert before.index("systemd-resolved") < before.index("rm -f"), written
    # And every failure is tolerated: the medium runs neither init system's
    # service manager and must not stop on that.
    assert before.count("2>/dev/null") >= 2, written


def test_the_lookup_budget_outlasts_every_resolver_in_the_list() -> None:
    """`RESOLVER_OPTIONS` gives each resolver `attempts` tries of `timeout`
    seconds, so a list whose first entries are down costs that much before the
    working one is reached. At 3 seconds the probe could not outlast one dead
    resolver: the eighth conversion's guest answered `fail fail fail fail fail`
    while it held a route to `10.31.0.254` and `223.5.5.5`, and reading that as
    a broken resolver sent the diagnosis after the wrong thing."""
    from tests.vm import cluster

    settings = dict(
        one.split(":", 1)
        for one in cluster.RESOLVER_OPTIONS.split()
        if ":" in one
    )
    worst = (
        len(cluster.GUEST_RESOLVERS)
        * int(settings["attempts"])
        * int(settings["timeout"])
    )
    assert cluster.LOOKUP_PATIENCE > worst, (cluster.LOOKUP_PATIENCE, worst)


def test_both_runners_answer_the_same_number_of_passphrase_prompts() -> None:
    """The cluster bounded it at four and the local runner at five, so a fifth
    valid dracut prompt passed one and failed the other. The bound is one
    constant in `tests/vm/console.py`, which both already import."""
    import inspect

    from tests.vm import cluster, console, run

    assert inspect.getsource(cluster).count("PASSPHRASE_ATTEMPTS: Final") == 0
    assert inspect.getsource(run).count("PASSPHRASE_ATTEMPTS: Final") == 0
    assert console.PASSPHRASE_ATTEMPTS >= 2
    # And neither loop counts to a literal of its own.
    for module in (cluster, run):
        source = inspect.getsource(module)
        assert "for _ in range(5):" not in source, module.__name__
        assert "for _ in range(4):" not in source, module.__name__


def test_the_unlock_key_count_refuses_a_digit_from_the_command_itself() -> None:
    """`zbm unlock key` counts authorised keys in the EFI image. `[1-9]` was
    an unanchored search, so the `2` of the command's own `2>/dev/null` would
    satisfy it and an image that authenticates nobody would be accepted. The
    test that came with the anchoring never exercised the pattern: an anchored
    pattern is not a substring of its command, so it fell outside that scan."""
    import re

    from dataclasses import replace

    from gentoo_install.model.config import Bootloader, DiskMode
    from tests.vm import installed

    from .layouts import config, ext4_on_gpt

    unlocking = config(ext4_on_gpt())
    unlocking = replace(
        unlocking,
        bootloader=replace(unlocking.bootloader, kind=Bootloader.ZFSBOOTMENU),
        kernel=replace(
            unlocking.kernel,
            remote_unlock=replace(unlocking.kernel.remote_unlock, enabled=True),
        ),
    )
    check = next(
        one for one in installed.checks(unlocking) if one.name == "zbm unlock key"
    )
    matcher = re.compile(check.pattern)
    # What the machine answers when the image carries no key at all.
    assert not matcher.search("0"), check.pattern
    # And what a digit borrowed from the command's own redirection looks like.
    assert not matcher.search(check.command), check.pattern
    # A real count still passes.
    assert matcher.search("2"), check.pattern


def test_building_the_driver_names_the_tree_it_was_built_from(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.vm import driver

    with pytest.raises(Exception):
        driver.build(tmp_path / "nowhere" / "driver.iso", fixtures=tmp_path / "absent")
    said = capsys.readouterr().out
    assert "installer revision:" in said, said
    assert driver.revision() in said, said


def test_one_module_announces_the_revision_and_the_runners_do_not() -> None:
    """A runner that prints its own copy is one the next edit can leave stale.

    `run.py` announced the tree and the other three runners did not, so
    `ram.py`, `dd.py` and `convert.py` each measured a snapshot their own logs
    could not name.
    """
    room = Path(__file__).resolve().parents[2] / "tests" / "vm"
    announcing = {
        one.name for one in sorted(room.glob("*.py"))
        if "installer revision:" in one.read_text()
    }
    # `cluster.py` writes it into each guest's own log rather than the
    # campaign stream, and takes its value from `revision_identity`, which
    # carries the driver digest a local run has no use for.
    assert announcing == {"driver.py", "cluster.py"}, sorted(announcing)
    runners = {"run.py", "ram.py", "dd.py", "convert.py"}
    assert not announcing & runners, sorted(announcing & runners)


def test_the_usage_examples_name_a_path_the_runner_can_load() -> None:
    """`--install` is resolved under `tests/`, and the module's own example
    said `tests/fixtures/...`, which resolves to `tests/tests/fixtures/...`."""
    import re

    from tests.vm import run as runner

    room = Path(__file__).resolve().parents[2] / "tests"
    said = runner.__doc__ or ""
    examples = re.findall(r"--install (\S+)", said)
    assert examples, said
    for one in examples:
        assert (room / one).is_file(), (one, str(room / one))


def test_a_reader_removes_the_colour_systemctl_writes_to_a_terminal() -> None:
    """A serial console is a terminal, so `systemctl` colours its answer and
    `enabled` arrives wrapped in SGR. The bounded patterns anchor on `$`, so a
    cluster run failed its `network` check while the console showed the line."""
    import re

    from tests.vm.console import plain
    from tests.vm.installed import checks

    from .layouts import config, ext4_on_gpt

    said = (
        b"\r\nsystemd-networkd.service                "
        b"\x1b[0;1;32menabled\x1b[0m\x1b[0;1;32m \x1b[0m\x1b[0;1;32menabled \x1b[0m\r\n"
    )
    assert b"\x1b" not in plain(said), plain(said)
    network = [one for one in checks(config(ext4_on_gpt())) if one.name == "network"]
    if network:
        pattern = network[0].pattern.encode()
        assert re.search(pattern, said) is None, "the raw bytes must not match"
        assert re.search(pattern, plain(said)) is not None, plain(said)


def test_the_installed_key_check_rejects_a_file_sshd_would_ignore() -> None:
    """The file that decides who can log in was never read back: the plan said
    it was written and no check opened it. sshd ignores an `authorized_keys`
    that is group or world writable and says so only in its own log, so the
    modes are as much of the answer as the key is."""
    import re

    from gentoo_install.exec.config import load
    from gentoo_install.model.sshkey import fingerprint
    from tests.vm.installed import checks

    installation = load(Path("tests/fixtures/vm-unlock.toml"))
    found = [one for one in checks(installation) if one.name.startswith("authorized keys")]
    assert found, [one.name for one in checks(installation)]

    printed = fingerprint(installation.system.authorized_keys[0])
    good = (
        f"2048 {printed} root@example (RSA)\n"
        "ACL 600 /root/.ssh/authorized_keys\n"
        "ACL 700 /root/.ssh\n"
        "ACL 750 /root\n"
    )
    for check in found:
        assert re.search(check.pattern, good), check.name
        for broken, why in (
            (good.replace("ACL 600", "ACL 644"), "a world-readable file"),
            (good.replace("ACL 700", "ACL 755"), "a world-readable directory"),
            (good.replace(printed[7:20], "Z" * 13), "a different key"),
            ("ssh-keygen: /root/.ssh/authorized_keys: No such file\n", "no file at all"),
            # The two are different failures and the message has to say which.
            (good.replace("ACL 750 /root\n", ""), "no home directory either"),
        ):
            assert not re.search(check.pattern, broken), (check.name, why)


def test_a_run_the_workstation_cannot_start_is_not_reported_as_a_failure(
    tmp_path: Path,
) -> None:
    """Two runs ended at 0.0m on 2026-08-24: `vm-proxy-http` because nothing
    listened on the proxy port, and `vm-binpkg` on the openSUSE medium because
    that ISO is not downloaded here. Both were printed as FAIL, which sends a
    reader to the installer for something the workstation is missing."""
    from tests.vm.campaign import Outcome, Run, mark_for
    from tests.vm.media import MISSING_PRECONDITION

    run = Run("fixtures/vm-proxy-http.toml")
    log = tmp_path / "proxy-http.log"

    # The ISO, because the harness starts the proxy itself now and that
    # message is gone: a sample nothing can produce stops being a sample.
    log.write_text(
        f"{MISSING_PRECONDITION}openSUSE-Leap-15.6-NET-x86_64-Media.iso is not "
        "downloaded here\n"
    )
    assert mark_for(Outcome(run, 1, 0.0, log)) == "SKIP"

    log.write_text("the install stopped: mkfs refused the device\n")
    assert mark_for(Outcome(run, 1, 0.0, log)) == "FAIL"


def test_the_installed_checks_read_the_config_the_run_installed(tmp_path: Path) -> None:
    """A remote-unlock run installs the harness's key, not the operator's.

    `zbm-unlock` failed its authorized-keys check on 2026-08-24 with the file
    present and its modes right: it held `SHA256:9Y3t23If...`, the run's own
    key, while the check derived its fingerprint from the fixture on disk.
    """
    from gentoo_install.exec.config import load
    from gentoo_install.model.sshkey import fingerprint
    from tests.vm.installed import checks
    from tests.vm.run import installed_config

    fixture = Path("tests/fixtures/zbm-unlock.toml")
    key = tmp_path / "id_ed25519"
    key.with_suffix(".pub").write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH8w6sgdmfNXN4gVQnnQ"
        "/5WQiY0oUPrQYr0SPTfnw2ah harness@example\n"
    )

    written = installed_config(fixture, key)
    assert written.system.authorized_keys != load(fixture).system.authorized_keys, written

    patterns = "".join(
        one.pattern for one in checks(written) if one.name.startswith("authorized keys")
    )
    assert patterns, [one.name for one in checks(written)]
    # Escaped, because the check escapes what it puts in its pattern: a
    # fingerprint holds `+` and `/`.
    for absent in load(fixture).system.authorized_keys:
        assert re.escape(fingerprint(absent)) not in patterns, absent
    for present in written.system.authorized_keys:
        assert re.escape(fingerprint(present)) in patterns, present

    # The call site, not only the helper: the defect was one `load()` in the
    # boot branch, and a test that calls `installed_config` itself passes with
    # that line still there.
    import ast

    source = (Path(__file__).resolve().parents[1] / "vm" / "run.py").read_text()
    bound = [
        ast.unparse(node.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(isinstance(one, ast.Name) and one.id == "expected" for one in node.targets)
    ]
    assert bound, "the boot branch no longer binds `expected`"
    for value in bound:
        assert value.startswith("installed_config("), value


def test_a_second_campaign_does_not_write_over_the_first_ones_logs() -> None:
    """The run that found the missing `authorized_keys` on 2026-08-24 had its
    install log replaced by the run sent to confirm it, because both wrote
    `<fixture>.log`. `cluster.py` learned this already and puts the vmid and
    the driver hash in its own names."""
    from tests.vm.campaign import _log_for, Run
    from tests.vm.driver import revision

    named = _log_for(Run("fixtures/vm-luks.toml")).name
    marker = revision().split()[0]
    assert marker in named, (named, marker)
    # `-dirty` survives: a name that drops it reads like a commit.
    if "dirty" in marker:
        assert "dirty" in named, named


def test_the_dataset_check_names_every_dataset_the_configuration_declares() -> None:
    """A pool whose child dataset is absent, or mounted somewhere else, gives
    a machine that boots and holds none of what was written into that dataset.
    `zfs-zbm` lost `/home/zakk` entirely and nothing in the checks read the
    dataset list back."""
    import re

    from gentoo_install.exec.config import load
    from tests.vm.installed import checks

    installation = load(Path("tests/fixtures/zfs-zbm.toml"))
    found = [one for one in checks(installation) if one.name == "datasets"]
    assert found, [one.name for one in checks(installation)]
    check = found[0]

    listed = (
        "zpcala/ROOT\t96K\tnone\n"
        "zpcala/ROOT/gentoo\t2.1G\tnone\n"
        "zpcala/ROOT/gentoo/root\t2.1G\t/\n"
        "zpcala/ROOT/gentoo/home\t96K\t/home\n"
    )
    assert re.search(check.pattern, listed), check.pattern
    without = listed.replace("zpcala/ROOT/gentoo/home\t96K\t/home\n", "")
    assert not re.search(check.pattern, without), check.pattern
    # And not satisfied by the question: the command names no dataset.
    assert not re.search(check.pattern, check.command), check.command


def test_a_failed_remote_unlock_says_the_exit_code_and_not_ssh_noise() -> None:
    """`zbm-unlock` failed on 2026-08-24 with the whole message being
    `Warning: Permanently added '[127.0.0.1]:52151' (ECDSA) to the list of
    known hosts.` — ssh's own noise, kept because the report took the last 300
    characters and there was nothing else. A reader learns from that only that
    ssh ran."""
    from tests.vm.run import _without_ssh_noise

    noise = (
        "Warning: Permanently added '[127.0.0.1]:52151' (ECDSA) to the list of known hosts.\n"
    )
    assert _without_ssh_noise(noise) == ""
    assert _without_ssh_noise(noise + "zfs: no such pool\n") == "zfs: no such pool"
    assert _without_ssh_noise("zfs: no such pool\n") == "zfs: no such pool"


def test_an_untracked_file_does_not_make_a_run_look_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every run said `1 uncommitted files` because a screenshot sat untracked
    in the checkout, while `git describe --dirty` had already reported the
    tree clean. `CLAUDE.md` reads that line to decide whether a result counts,
    so a file the driver cannot contain must not appear in it."""
    import subprocess

    from tests.vm import driver

    def git(*argv: str) -> None:
        subprocess.run(["git", *argv], cwd=tmp_path, check=True, capture_output=True)

    git("init", "--quiet")
    git("config", "user.email", "zakk@gentoozh.org")
    git("config", "user.name", "Zakk")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "tracked.txt").write_text("one\n")
    git("add", "tracked.txt")
    git("commit", "--quiet", "-m", "first")

    monkeypatch.setattr(driver, "REPOSITORY", tmp_path)
    (tmp_path / "screenshot.png").write_bytes(b"")
    assert "uncommitted" not in driver.revision(), driver.revision()
    (tmp_path / "tracked.txt").write_text("two\n")
    assert "1 uncommitted files" in driver.revision(), driver.revision()


def test_only_reaches_every_run_of_a_fixture() -> None:
    """`--only vm-binpkg` reached the openSUSE medium and nothing else: the
    lookup was a dict keyed by the fixture stem, so of the eight runs carrying
    that fixture the last one written won. The ordinary run and the
    `--interrupt` one that exercises `--resume` could not be asked for."""
    from tests.vm.campaign import named

    chosen = named(["vm-binpkg"])
    names = [one.name for one in chosen]
    assert len(names) > 1, names
    assert "official-minimal-uefi-vm-binpkg" in names, names
    assert "official-minimal-uefi-vm-binpkg-interrupted" in names, names
    assert any(one.interrupt for one in chosen), names


def test_an_interrupted_run_does_not_share_a_work_directory() -> None:
    """`--only vm-binpkg` now selects the plain run and the `--interrupt` one,
    and they collided: the work directory took the fixture stem and not the
    suffix `Run.name` carries, so the second reported `LOCK` at 0.0m without
    ever starting."""
    import ast
    import inspect

    from tests.vm import run as runner
    from tests.vm.campaign import Run

    plain = Run("fixtures/vm-binpkg.toml")
    interrupted = Run("fixtures/vm-binpkg.toml", interrupt=True)
    assert plain.name != interrupted.name, plain.name
    assert interrupted.name.endswith(runner.INTERRUPTED_SUFFIX), interrupted.name

    # And the work directory is built from the same constant, so the two
    # names cannot drift apart again.
    source = inspect.getsource(runner.main)
    assigned = [
        ast.unparse(node.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(isinstance(one, ast.Name) and one.id == "workdir" for one in node.targets)
    ]
    assert assigned, "main no longer assigns workdir"
    assert all("INTERRUPTED_SUFFIX" in one for one in assigned), assigned


def test_every_dispatch_form_says_what_it_will_run_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--only vm-zfs-encrypted` printed nothing at all until the run ended an
    hour later: its two siblings each announce their round and that branch was
    written without one. A local round redirected to a file is then a zero-byte
    file for the whole run, which reads exactly like a launch that failed."""
    from tests.vm import campaign

    said: list[str] = []

    def announced(runs: Sequence[campaign.Run]) -> list[campaign.Outcome]:
        said.append(capsys.readouterr().out)
        return []

    monkeypatch.setattr(campaign, "parallel", announced)
    monkeypatch.setattr(campaign, "report", lambda done: 0)
    for argv in (["--only", "vm-xfs"], ["--keep-going"], []):
        assert campaign.main(argv) == 0, argv

    assert len(said) >= 3, said
    for spoken in said:
        assert spoken.strip(), said

def test_both_runners_read_one_answer_for_how_heavy_a_guest_is() -> None:
    """The two runners spend the same resource on the same install and each
    held its own answer: the cluster derived it, the campaign wrote it out per
    run. Comparing them fixture by fixture gave nine disagreements, every one
    the local table calling light what the cluster's rule calls heavy — the
    six ZFS layouts, and the three with no binary host at all."""
    from tests.vm.campaign import STAGES
    from tests.vm.cluster import fixtures as cluster_fixtures

    local = {Path(one.config).stem: one for stage in STAGES.values() for one in stage}
    # Only what the cluster will accept: a fixture it refuses has no answer
    # there to compare against.
    shared = sorted(
        stem
        for stem in local
        if stem not in {"vm-proxy", "vm-proxy-http", "vm-image", "static-ip", "vm-convert"}
    )
    assert len(shared) > 20, shared

    for stem in shared:
        job = cluster_fixtures([stem])[0]
        assert job.heavy == (local[stem].weight > 1), stem
        assert job.heavy == bool(local[stem].cpus), stem


def test_a_conversion_is_dispatched_to_the_runner_that_can_perform_one() -> None:
    """`vm-convert` was run through `tests/vm/run.py`, which boots the
    installation medium against a blank disk. A conversion has to convert a
    machine that is already running, so it met a seeded cloud image whose root
    filesystem holds 3 GiB free and the installer refused with exit 2. The
    refusal was right; the dispatch was not.

    Derived from `DiskMode.IN_PLACE` rather than named, which is how
    `cluster.py` decides the same thing.
    """
    from tests.vm.campaign import STAGES, Run

    conversion = Run("fixtures/vm-convert.toml")
    assert conversion.converts
    assert conversion.argv()[1:3] == ["-m", "tests.vm.convert"]
    # The image, because no installation medium is booted: a name carrying
    # `official-minimal-uefi` would name two things this run does not have.
    assert conversion.name == "fedora-vm-convert", conversion.name

    plain = Run("fixtures/vm-xfs.toml")
    assert not plain.converts
    assert plain.argv()[1:3] == ["-m", "tests.vm.run"]

    # And it is in the campaign again, which is the point of the change.
    named = {Path(one.config).stem for stage in STAGES.values() for one in stage}
    assert "vm-convert" in named


def test_the_proxy_the_harness_runs_carries_a_real_fetch() -> None:
    """A proxy that accepts a connection and forwards nothing looks exactly
    like a working one to `require_proxy`'s old probe, which is what the
    fixtures were waiting on. Both kinds and both directions of the SOCKS
    credential, against a listener this test runs itself."""
    import socket
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from tests.vm.proxy import Credential, serving

    body = b"through the proxy"

    class Answering(BaseHTTPRequestHandler):
        # The name is `http.server`'s, not a choice.
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            return None

    with HTTPServer(("127.0.0.1", 0), Answering) as origin:
        threading.Thread(target=origin.serve_forever, daemon=True).start()
        where = f"http://127.0.0.1:{origin.server_address[1]}/"

        with serving("http", 0) as port:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"})
            )
            with opener.open(where, timeout=10) as answer:
                assert answer.read() == body

        with serving("socks5", 0, Credential("installer", "secret")) as port:
            assert _through_socks(port, "installer", "secret", origin.server_address[1]) == body
            # The refusal, or the credential is decoration.
            assert _through_socks(port, "installer", "wrong", origin.server_address[1]) is None
        origin.shutdown()


def _through_socks(port: int, name: str, secret: str, target: int) -> bytes | None:
    """One SOCKS5 CONNECT to `127.0.0.1:target`, then one HTTP request.

    Hand-written rather than through a library, because the point is what the
    bytes on the wire are: nothing in the standard library speaks SOCKS5.
    """
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        client.sendall(bytes((5, 1, 2)))
        if client.recv(2) != bytes((5, 2)):
            return None
        client.sendall(
            bytes((1, len(name))) + name.encode() + bytes((len(secret),)) + secret.encode()
        )
        if client.recv(2) != bytes((1, 0)):
            return None
        client.sendall(bytes((5, 1, 0, 1)) + socket.inet_aton("127.0.0.1") + target.to_bytes(2, "big"))
        if client.recv(10)[:2] != bytes((5, 0)):
            return None
        client.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        seen = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            seen += chunk
        return seen.partition(b"\r\n\r\n")[2]


def test_the_image_install_is_in_the_campaign_and_is_not_booted() -> None:
    """`vm-image` was in neither runner: the cluster refuses it because it
    cannot give the guest a filesystem to write the image onto, and the
    campaign excluded it because nothing here boots a file. So the only
    installation mode that produces a file rather than a disk was never run
    end to end at all.

    `check_image` reads the partitions and filesystems out of the file, which
    is what a verdict for this mode can honestly assert; booting it is a
    separate gap with its own row.
    """
    from tests.vm.campaign import STAGES

    named = {Path(one.config).stem: one for stage in STAGES.values() for one in stage}
    image = named.get("vm-image")
    assert image is not None, sorted(named)
    assert not image.boot, "the target disk carries the scratch filesystem, not a system"
    assert "--and-boot" not in image.argv(), image.argv()
    # And every other run still boots, or this field becomes a way to make a
    # red run green.
    assert all(one.boot for name, one in named.items() if name != "vm-image")


def test_an_image_run_hands_its_verdict_a_configuration_not_a_path() -> None:
    """Every image run died on `'PosixPath' object has no attribute 'disk'`.
    `check_expected` took an `InstallConfig` from `#1015` onward and this call
    site kept passing the fixture's path; `argparse.Namespace` makes
    `args.install` `Any`, so the expression built from it was `Any` too and
    mypy strict had nothing to say. Nothing in either runner's list carried
    the image mode, so nothing ran into it.
    """
    import ast

    source = Path("tests/vm/run.py").read_text()
    tree = ast.parse(source)
    inside = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_install_and_check"
    )
    assertions = [
        keyword.value
        for node in ast.walk(inside)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "assertions"
    ]
    assert assertions, "the image verdict lost its assertions argument"
    for value in assertions:
        written = ast.unparse(value)
        # A path only ever reaches this argument through `installed_config`,
        # which is what turns it into the configuration the run installs. The
        # other call site passes a name already bound from it.
        if "REPOSITORY" in written:
            assert "installed_config(" in written, written


def test_a_name_prompt_that_comes_back_is_answered_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`vm-zfs-encrypted` failed on an install that had finished: its console
    held two `cryptzfs login:` banners two seconds apart and no `Password:`.
    The name prompt turns the echo off with `TCSAFLUSH`, so a name typed into
    that window is discarded and agetty prints a fresh one. `cluster.py` has
    handled this since `ext3` lost 33.6 minutes to it; the local console did
    the sequence once and gave up."""
    import time

    from tests.vm.console import SerialConsole

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class Agetty:
        """A login that swallows the first name and reprints its prompt."""

        def __init__(self) -> None:
            self.names = 0
            self.queue = [b"\r\nlab login: "]

        def recv(self, size: int) -> bytes:
            clock[0] += 1.0
            return self.queue.pop(0) if self.queue else b""

        def sendall(self, data: bytes) -> None:
            typed = data.decode().strip()
            if typed == "root":
                self.names += 1
                # The first name lands in the flush window and is lost.
                self.queue.append(
                    b"\r\nlab login: " if self.names == 1 else b"\r\nPassword: "
                )
                return
            self.queue.append(b"\r\n# ")

        def close(self) -> None:
            return None

        @property
        def closed(self) -> bool:
            return False

    guest = Agetty()
    SerialConsole(cast(Any, guest), BytesIO()).login("root", "install", r"# ")
    assert guest.names == 2, guest.names


def test_no_run_path_spells_out_the_login_sequence_itself() -> None:
    """The reprinted-name-prompt fix reached `SerialConsole.login()` and
    `vm-zfs-encrypted` failed again in exactly the same way, because
    `unlock_and_login` carried its own copy of the sequence. Six call sites
    waited for `PASSWORD_PROMPT`; hardening one of them changed nothing for
    the fixture that had shown the defect.
    """
    import re

    source = Path("tests/vm/run.py").read_text()
    assert "PASSWORD_PROMPT" not in source, "run.py spells the sequence out again"
    # And it does reach the shared one, by whichever of the two entries suits
    # a caller that has already read the prompt.
    assert re.search(r"console\.(login|answer_login)\(", source), source[:0]


#: Every function that waits for a boot passphrase prompt and answers it. The
#: set is named rather than counted so that a loop removed shows up as
#: loudly as a loop added: a check over whatever it happens to find passes on
#: a file with nothing in it.
PASSPHRASE_LOOPS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("tests/vm/run.py", "unlock_and_login"),
        ("tests/vm/cluster.py", "reach_the_login_past_any_passphrase"),
        ("tests/vm/cluster.py", "_unlock"),
    }
)


def _answers_a_passphrase(function: ast.FunctionDef) -> bool:
    """Whether this function waits for a passphrase prompt and answers it."""
    names = {
        node.id for node in ast.walk(function) if isinstance(node, ast.Name)
    }
    return "PASSPHRASE_PROMPT" in names and "DISK_PASSPHRASE" in names


def test_every_passphrase_loop_reads_the_one_settle_rule() -> None:
    """Three loops on three transports, and the measurement reached one.

    `PROMPT_SETTLE` was measured because `vm-zfs-encrypted` failed locally
    three times running with its answer echoed on its own line and never
    taken. The fix went into `run.py`. The cluster's
    `reach_the_login_past_any_passphrase` still answered the moment it read
    the prompt, and `_unlock` still started its settle from zero, so the
    fixture that showed the defect was measured on a path the fix never
    reached.
    """
    found: set[tuple[str, str]] = set()
    for path in sorted({one for one, _ in PASSPHRASE_LOOPS}):
        tree = ast.parse(Path(path).read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            if not _answers_a_passphrase(function):
                continue
            found.add((path, function.name))
            called = {
                node.func.id
                for node in ast.walk(function)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            assert "passphrase_settle" in called, (
                f"{path}:{function.name} answers a passphrase prompt without "
                "reading the settle rule"
            )
    assert found == PASSPHRASE_LOOPS, found ^ PASSPHRASE_LOOPS


def test_the_settle_grows_with_every_unanswered_prompt() -> None:
    """A fixed wait is a guess about a machine whose load is not ours to
    choose: the three seconds were measured on an idle one, and a prompt
    answered without effect is evidence that this machine needs longer."""
    assert passphrase_settle(0) > 0, passphrase_settle(0)
    assert passphrase_settle(1) > passphrase_settle(0), passphrase_settle(1)
    assert passphrase_settle(4) > passphrase_settle(3), passphrase_settle(4)


def test_a_booted_image_run_is_judged_like_any_installed_machine() -> None:
    """`exit 0` and a layout read through `losetup` were the whole record.

    Nothing had ever booted the file an image install writes, so a mode whose
    entire product is that file was verified by reading it. The install pass
    still reads it; the pass after it boots the same file and is judged by
    what a booted machine leaves behind.
    """
    import contextlib
    from io import StringIO

    from gentoo_install.exec.config import load
    from tests.vm import run as runner

    written = load(Path("tests/fixtures/vm-image.toml"))
    read_back = [name for name, _ in runner._from_config(written)]
    assert read_back == ["image"], read_back
    after_boot = [name for name, _ in runner._from_config(written, booted=True)]
    assert "image" not in after_boot, after_boot
    assert len(after_boot) > 1, after_boot

    # The same results, judged twice: what satisfies the install pass cannot
    # satisfy the boot pass, because the boot pass asks what came up.
    collected = {"image.txt": b"loop1   \nloop1p1 vfat\nloop1p2 ext4\n", "install.rc": b"0\n"}
    printed = StringIO()
    with contextlib.redirect_stdout(printed):
        assert runner.check_expected(collected, written) == 0
    with contextlib.redirect_stdout(StringIO()):
        assert runner.check_expected(collected, written, booted=True) == 1


def test_the_image_comes_out_of_the_guest_without_its_declared_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twenty gibibytes declared and a few written, read back compressed.

    What the file holds is asserted; what it occupies is not. Skipping a run
    of zeros and writing it produce the same bytes, and the difference is the
    allocation -- which ZFS makes on its own, so the assertion that would hold
    it passed with the seek removed and was not a check at all.
    """
    import gzip as gzip_module
    import subprocess as subprocess_module
    import tempfile

    from gentoo_install.exec.config import load
    from tests.vm import run as runner

    written = load(Path("tests/fixtures/vm-image.toml"))
    payload = b"\0" * (runner.IMAGE_BLOCK * 3) + b"gentoo" + b"\0" * 10

    class _Pull:
        def __init__(self, code: int, body: bytes) -> None:
            self.stdout = BytesIO(gzip_module.compress(body))
            self.returncode = code

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"", b"ssh: connect to host port 22: Connection refused"

    def _pull(code: int, body: bytes) -> Any:
        return lambda *args, **rest: _Pull(code, body)

    with tempfile.TemporaryDirectory() as scratch:
        into = Path(scratch) / "image.raw"
        monkeypatch.setattr(subprocess_module, "Popen", _pull(0, payload))
        runner.fetch_image(Path("/dev/null"), 2222, written, into)
        # Byte-exact: the seek advances the file position by exactly what it
        # skipped, and one that does not leaves the payload at the wrong
        # offset and the file short.
        assert into.read_bytes() == payload, into.stat().st_size

        # An ssh that failed is not an image, however much arrived.
        monkeypatch.setattr(subprocess_module, "Popen", _pull(255, payload))
        with pytest.raises(RuntimeError, match="did not come out of the guest"):
            runner.fetch_image(Path("/dev/null"), 2222, written, into)

        # Nor is an empty one: the file exists and holds no install.
        monkeypatch.setattr(subprocess_module, "Popen", _pull(0, b""))
        with pytest.raises(RuntimeError, match="came out empty"):
            runner.fetch_image(Path("/dev/null"), 2222, written, into)


def _ppm(width: int, height: int, ink: dict[tuple[int, int], bytes]) -> bytes:
    """A P6 PPM whose named pixels differ from the background."""
    body = bytearray(b"\x00\x00\x00" * width * height)
    for (column, line), colour in ink.items():
        at = (line * width + column) * 3
        body[at : at + 3] = colour
    return f"P6\n{width} {height}\n255\n".encode() + bytes(body)


def test_the_framebuffer_reader_answers_which_columns_carry_ink(tmp_path: Path) -> None:
    """The serial console carries the bytes the guest wrote, not what the
    console made of them. A CJK kernel built with the wrong font size writes
    the same bytes and draws nothing, and every check this repository has
    passes on it.
    """
    from tests.vm.monitor import TEXT_CELL_HEIGHT, Framebuffer, MonitorError, screendump

    # Two text rows of a narrow screen, with ink in the second row only.
    width, height = 18, TEXT_CELL_HEIGHT * 2
    lit = {(column, TEXT_CELL_HEIGHT + 3): b"\xff\xff\xff" for column in (4, 5, 13)}
    shot = tmp_path / "screen.ppm"
    shot.write_bytes(_ppm(width, height, lit))

    screen = Framebuffer.read(shot)
    assert (screen.width, screen.height) == (width, height)
    assert screen.inked_columns(0) == frozenset()
    assert screen.inked_columns(1) == frozenset({4, 5, 13})
    with pytest.raises(MonitorError, match="past the"):
        screen.inked_columns(2)

    # A truncated dump is refused rather than read as a smaller screen: qemu
    # creates the file and then writes it, so a race reads a valid header over
    # a body that is not there yet.
    short = tmp_path / "short.ppm"
    short.write_bytes(_ppm(width, height, {})[:-30])
    with pytest.raises(MonitorError, match="carries"):
        Framebuffer.read(short)

    # And a file that is not one at all.
    other = tmp_path / "other.ppm"
    other.write_bytes(b"P3\n2 2\n255\n0 0 0 0 0 0 0 0 0 0 0 0\n")
    with pytest.raises(MonitorError, match="not an 8-bit binary PPM"):
        Framebuffer.read(other)

    assert screendump is not None


def test_screendump_reads_a_real_qemu_screen(tmp_path: Path) -> None:
    """Against qemu itself, because the format is the whole question: this
    was written after one run measured `P6\\n720 400\\n255\\n` from a live
    monitor, and a reader built from the documentation would have been
    checked against nothing."""
    import shutil
    import subprocess

    from tests.vm.monitor import TEXT_CELL_HEIGHT, TEXT_CELL_WIDTH, screendump

    qemu = shutil.which("qemu-system-x86_64")
    if qemu is None:
        pytest.skip("no qemu-system-x86_64 on this machine")
    socket_path = tmp_path / "mon.sock"
    guest = subprocess.Popen(
        [
            qemu, "-display", "none", "-m", "128", "-nodefaults", "-vga", "std",
            "-monitor", f"unix:{socket_path},server,nowait",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        screen = screendump(socket_path, tmp_path / "shot.ppm")
    finally:
        guest.terminate()
        guest.wait(timeout=30)
    # Text mode, in the cells the reader assumes: 80 columns and 25 rows.
    assert screen.width == 80 * TEXT_CELL_WIDTH, screen.width
    assert screen.height == 25 * TEXT_CELL_HEIGHT, screen.height
    assert len(screen.pixels) == screen.width * screen.height * 3


def _screen_with(narrow: int, wide: int) -> Any:
    """A framebuffer whose two top rows carry ink out to these pixels."""
    from tests.vm.monitor import TEXT_CELL_HEIGHT, Framebuffer

    width, height = 160, TEXT_CELL_HEIGHT * 3
    body = bytearray(b"\x00\x00\x00" * width * height)
    for row, reach in ((0, narrow), (1, wide)):
        for column in range(reach):
            at = ((row * TEXT_CELL_HEIGHT + 4) * width + column) * 3
            body[at : at + 3] = b"\xff\xff\xff"
    return Framebuffer(width=width, height=height, pixels=bytes(body))


def test_a_wide_glyph_is_measured_against_the_same_screens_narrow_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One build shipped `CONFIG_FONT_CJK_16x16` alone, printed the text and
    drew nothing, and every check in this harness passed. The comparison is
    between two rows of one screen so that nobody has to say how many pixels
    a glyph should have: the cell size is the font's to choose.
    """
    from dataclasses import replace as _replace

    from gentoo_install.exec.config import load
    from tests.vm import run as runner
    cjk = load(Path("tests/fixtures/vm-cjk-kernel.toml"))
    assert cjk.system.console_cjk, "this fixture is the one that asks for it"
    plain = _replace(cjk, system=_replace(cjk.system, console_cjk=False))

    class Console:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run(self, command: str, timeout: float = 0.0) -> bytes:
            self.commands.append(command)
            return b""

    def _answer(screen: Any) -> None:
        monkeypatch.setattr(runner, "screendump", lambda *args, **rest: screen)

    # A console asked for nothing is not touched at all.
    quiet = Console()
    _answer(_screen_with(0, 0))
    runner.check_console_glyphs(cast(Any, quiet), plain, tmp_path / "m", tmp_path / "p")
    assert quiet.commands == [], quiet.commands

    # Two cells per wide character: the pair reaches about twice as far. The
    # cell here is 8 rather than `TEXT_CELL_WIDTH`, because that is what the
    # installed system's framebuffer console actually draws and the check
    # derives the cell from the screen rather than from the constant.
    good = Console()
    _answer(_screen_with(8 * 2, 8 * 4))
    runner.check_console_glyphs(cast(Any, good), cjk, tmp_path / "m", tmp_path / "p")
    assert any("/dev/tty1" in one for one in good.commands), good.commands
    assert any("\\033[2J" in one for one in good.commands), good.commands

    # The fallback: the wide pair drawn in single cells, which is what a
    # kernel with the wrong font size does.
    _answer(_screen_with(8 * 2, 8 * 2))
    with pytest.raises(SystemExit, match="fell back"):
        runner.check_console_glyphs(cast(Any, Console()), cjk, tmp_path / "m", tmp_path / "p")

    # Nothing drawn for the wide pair at all.
    _answer(_screen_with(8 * 2, 0))
    with pytest.raises(SystemExit, match="drew nothing for the wide pair"):
        runner.check_console_glyphs(cast(Any, Console()), cjk, tmp_path / "m", tmp_path / "p")

    # And a screen with no ink anywhere says so rather than reporting the
    # wide pair missing: an empty comparison proves nothing either way.
    _answer(_screen_with(0, 0))
    with pytest.raises(SystemExit, match="cannot say anything"):
        runner.check_console_glyphs(cast(Any, Console()), cjk, tmp_path / "m", tmp_path / "p")

    # A wider cell than either mode measured here, to show the threshold moves
    # with the screen rather than with a constant: a 16-wide cell needs the
    # wide pair out at 48, and 32 is a narrow fallback at that size even
    # though it would have passed as a wide pair on an 8-wide cell.
    _answer(_screen_with(16 * 2, 16 * 4))
    runner.check_console_glyphs(cast(Any, Console()), cjk, tmp_path / "m", tmp_path / "p")
    _answer(_screen_with(16 * 2, 16 * 2))
    with pytest.raises(SystemExit, match="fell back"):
        runner.check_console_glyphs(cast(Any, Console()), cjk, tmp_path / "m", tmp_path / "p")


def test_one_rule_says_how_much_memory_a_guest_needs() -> None:
    """`cluster.py` gave a compiling guest 8192 and `qemu.py` gave every local
    guest a flat `8G`, so the same question had two answers and the local one
    did not distinguish a desktop from a binary-package install at all.
    `vm-gnome` was `Killed` compiling `net-libs/webkit-gtk` at 185 minutes.
    """
    from gentoo_install.exec.config import load
    from tests.vm import cluster, qemu, sizing

    heavy = load(Path("tests/fixtures/vm-gnome.toml"))
    light = load(Path("tests/fixtures/vm-binpkg.toml"))
    assert sizing.compiles(heavy) and not sizing.compiles(light)
    assert sizing.memory_mib(heavy) == sizing.HEAVY_MEMORY_MIB
    assert sizing.memory_mib(light) == sizing.LIGHT_MEMORY_MIB
    # Larger, and the reason is a measurement rather than a ratio: eight was
    # what a compiling guest had when webkit-gtk was killed in one.
    assert sizing.HEAVY_MEMORY_MIB > 8192, sizing.HEAVY_MEMORY_MIB

    # And both runners read that one rule rather than restating it.
    assert cluster.HEAVY_MEMORY_MIB == sizing.HEAVY_MEMORY_MIB
    assert cluster.GUEST_MEMORY_MIB == sizing.LIGHT_MEMORY_MIB
    assert qemu.VmSpec.memory == f"{sizing.LIGHT_MEMORY_MIB}M", qemu.VmSpec.memory
    # The local runner asks the rule for every run that installs something:
    # its default is only for a run with no configuration, such as probing a
    # medium.
    source = Path("tests/vm/run.py").read_text()
    assert "memory_mib(load(" in source, source[:0]
    assert "memory=wanted_memory" in source, source[:0]


def test_a_gap_between_chunks_is_not_the_end_of_the_draw() -> None:
    """One quiet window ended the read in the middle of a drawn row.

    The console is a websocket to a cluster node, so the gap between two
    chunks of one repaint can be longer than a window. Reading then returned
    `-j` for a row the guest had written as `-j1`, which reads as a defect in
    the interface rather than as half a frame, and it repeated across three
    reads because the tear is where the network put it, not where the guest is.
    """
    from tests.tui.session import _settled
    from tests.tui.screen import Screen

    class TornConsole:
        """Delivers one repaint in two chunks with a quiet window between."""

        def __init__(self) -> None:
            self.answers = [b"\x1b[1;1H  -j", b"", b"1", b"", b""]

        def read_available(self, patience: float) -> bytes:
            del patience
            return self.answers.pop(0) if self.answers else b""

    grid = Screen(lines=5, columns=20)
    shown = _settled(grid, TornConsole())
    assert "-j1" in shown, shown


def test_a_console_socket_asks_whether_its_peer_is_still_there() -> None:
    """`read()` answers empty for an idle console and for a dead one alike.

    A half-open connection whose peer vanished without a FIN reads empty for
    ever, which is exactly what an idle guest looks like, and four stalled
    guests were read as the installer hanging. Keepalive makes the kernel
    break the second one, and `read()` already turns that `OSError` into a
    closed console with a reason.

    Measured against a real socket rather than a double: these are the values
    the kernel kept, not the values that were passed.
    """
    import socket

    from tests.vm import websocket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    talking = socket.create_connection(listener.getsockname())
    try:
        assert talking.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 0
        websocket.keep_asking(talking)
        assert talking.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        assert (
            talking.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)
            == websocket.SILENCE_BEFORE_PROBING
        )
        assert (
            talking.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)
            == websocket.BETWEEN_PROBES
        )
        assert (
            talking.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)
            == websocket.PROBES_BEFORE_GIVING_UP
        )
    finally:
        talking.close()
        listener.close()


def test_the_console_connection_is_the_one_that_gets_keepalive() -> None:
    """`connect` must call it, not only export it: a helper nothing reaches
    leaves every real console exactly as ambiguous as before."""
    import ast
    import inspect

    from tests.vm import websocket

    import textwrap

    source = textwrap.dedent(inspect.getsource(websocket.WebSocket.connect))
    called = {
        getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }
    assert "keep_asking" in called, sorted(called)


def test_a_stalled_schedule_can_be_asked_where_its_threads_are() -> None:
    """A campaign that stalls says what it failed at and nothing about why.

    On 2026-09-01 it answered `GET /nodes did not answer` for twenty minutes
    while a fresh process made the same call in three seconds and `free_slots`
    reported two free slots. `kill -USR1` now writes every thread's stack and
    the schedule carries on, so the next stall is evidence rather than a
    guess. Run rather than read: whether a handler survives the other three
    this function installs is not visible in the source.
    """
    import subprocess
    import sys

    program = (
        "import os, signal, sys, threading, time;"
        "sys.path.insert(0, '.');"
        "from tests.vm.cluster import _leave_on_a_signal;"
        "_leave_on_a_signal();"
        "threading.Thread(target=time.sleep, args=(30,), daemon=True).start();"
        "os.kill(os.getpid(), signal.SIGUSR1);"
        "time.sleep(0.5);"
        "print('STILL-RUNNING')"
    )
    said = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=60
    )
    assert "STILL-RUNNING" in said.stdout, said.stdout
    assert "Thread 0x" in said.stderr or "Current thread" in said.stderr, said.stderr
