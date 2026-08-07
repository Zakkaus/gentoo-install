from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.errors import CommandFailed, DeviceNotFound, IntegrityError, PreflightFailed
from gentoo_install.exec import fetch, preflight
from gentoo_install.exec.probe import Machine as ProbedMachine
from gentoo_install.exec.probe import Probe
from gentoo_install.exec.runner import Runner, under
from gentoo_install.model.config import Bootloader, BootloaderConfig, Firmware
from gentoo_install.model.device import DeviceId

from .layouts import config, ext4_on_gpt, i


def runner(tmp_path: Path) -> Runner:
    return Runner(log=lambda line: None)


def described(**fields: object) -> ProbedMachine:
    base = {
        "architecture": "x86_64",
        "uefi": True,
        "root": True,
        "memory_bytes": 16 * 1024**3,
        "commands": frozenset(preflight.required_commands(config())),
    }
    base.update(fields)
    return ProbedMachine(**base)  # type: ignore[arg-type]


def probe_of(tmp_path: Path, *, disk: str | None = "/dev/null") -> Probe:
    """A probe that has already resolved the layout's disk, so a check about
    firmware or memory is not answered by a missing device on the test host."""
    probe = Probe(runner=runner(tmp_path), work=tmp_path)
    if disk is not None:
        probe.remember(DeviceId("disk"), disk)
    return probe


def test_a_command_that_fails_raises_with_its_output(tmp_path: Path) -> None:
    with pytest.raises(CommandFailed, match="exited 3"):
        runner(tmp_path).run(["sh", "-c", "echo trouble >&2; exit 3"])


def test_a_command_that_is_not_installed_says_so(tmp_path: Path) -> None:
    with pytest.raises(CommandFailed, match="not installed"):
        runner(tmp_path).run(["definitely-not-a-command-on-this-machine"])


def test_a_command_that_hangs_without_printing_is_still_killed(tmp_path: Path) -> None:
    """The timeout is a watchdog rather than a check between output lines: a
    silent hang never reaches a per-line check."""
    with pytest.raises(CommandFailed, match="did not finish"):
        runner(tmp_path).run(["sleep", "30"], timeout=1.0)


def test_output_arrives_line_by_line_rather_than_at_the_end(tmp_path: Path) -> None:
    seen: list[str] = []
    Runner(log=seen.append).run(["sh", "-c", "echo first; echo second"])
    assert [line for line in seen if line.startswith("| ")] == ["| first", "| second"]


def test_a_failure_can_be_asked_for_rather_than_raised(tmp_path: Path) -> None:
    assert runner(tmp_path).run(["false"], check=False).returncode == 1


def test_a_dry_run_runs_nothing(tmp_path: Path) -> None:
    lines: list[str] = []
    quiet = Runner(log=lines.append, dry_run=True)
    marker = tmp_path / "written"
    quiet.run(["touch", str(marker)])
    assert not marker.exists()
    assert lines and lines[0].startswith("would run:")


def test_commands_in_the_target_are_chrooted_and_keep_boot_unmounted() -> None:
    target = Runner(log=lambda line: None).in_target(Path("/mnt/gentoo"))
    assert target.prefix == ("chroot", "/mnt/gentoo")
    assert target.environment["DONT_MOUNT_BOOT"] == "1"


def test_a_target_path_becomes_a_host_path() -> None:
    assert under(Path("/mnt/gentoo"), PurePosixPath("/etc/fstab")) == Path("/mnt/gentoo/etc/fstab")
    assert under(Path("/mnt/gentoo"), PurePosixPath("/")) == Path("/mnt/gentoo")


def test_a_selector_that_is_absent_names_the_device_rather_than_guessing(tmp_path: Path) -> None:
    with pytest.raises(DeviceNotFound, match="disk"):
        probe_of(tmp_path, disk=None).resolve(DeviceId("disk"), "/dev/definitely-absent")


def test_a_resolved_device_survives_a_restart_of_the_installer(tmp_path: Path) -> None:
    first = probe_of(tmp_path, disk=None)
    first.remember(DeviceId("disk"), "/dev/vda")
    second = probe_of(tmp_path, disk=None)
    second.load()
    assert second.path_of(DeviceId("disk")) == "/dev/vda"


def test_the_required_commands_come_from_the_layout() -> None:
    plain = preflight.required_commands(config())
    assert "mkfs.ext4" in plain and "sgdisk" in plain
    assert "cryptsetup" not in plain and "zpool" not in plain

    from gentoo_install.model.parse import load

    encrypted = preflight.required_commands(load(Path("tests/fixtures/btrfs-luks.toml")))
    assert {"cryptsetup", "mkfs.btrfs", "btrfs"} <= encrypted


def test_preflight_collects_every_reason_rather_than_the_first(tmp_path: Path) -> None:
    report = preflight.inspect(
        config(),
        described(root=False, architecture="aarch64", commands=frozenset()),
        probe_of(tmp_path),
    )
    assert len(report.fatal) >= 3
    with pytest.raises(PreflightFailed, match="run as root"):
        report.raise_if_fatal()


def test_a_uefi_configuration_on_a_bios_boot_is_fatal(tmp_path: Path) -> None:
    report = preflight.inspect(config(), described(uefi=False), probe_of(tmp_path))
    assert any("booted by BIOS" in reason for reason in report.fatal)


def test_a_bios_configuration_on_a_uefi_boot_is_only_a_warning(tmp_path: Path) -> None:
    on_bios = replace(
        config(), bootloader=BootloaderConfig(kind=Bootloader.GRUB, firmware=Firmware.BIOS)
    )
    report = preflight.inspect(on_bios, described(uefi=True), probe_of(tmp_path))
    assert not report.fatal
    assert report.warnings


def test_little_memory_warns_instead_of_stopping(tmp_path: Path) -> None:
    report = preflight.inspect(config(), described(memory_bytes=2 * 1024**3), probe_of(tmp_path))
    assert not report.fatal
    assert any("tmpfs" in warning for warning in report.warnings)


def test_a_digests_file_without_the_archive_is_an_integrity_error(tmp_path: Path) -> None:
    digests = tmp_path / "stage3.tar.xz.DIGESTS"
    digests.write_text("# SHA512 HASH\nabc  stage3-other.tar.xz\n")
    with pytest.raises(IntegrityError, match="no SHA512 line"):
        fetch._expected_sha512(digests, "stage3-amd64-systemd-1.tar.xz")


def test_a_digest_that_does_not_match_stops_the_install(tmp_path: Path) -> None:
    archive = tmp_path / "stage3-amd64-systemd-1.tar.xz"
    archive.write_bytes(b"not really a stage3")
    digests = tmp_path / "stage3-amd64-systemd-1.tar.xz.DIGESTS"
    digests.write_text(f"# SHA512 HASH\n{'0' * 128}  {archive.name}\n")
    with pytest.raises(IntegrityError, match="SHA512"):
        fetch._verify_digest(archive, digests)


def test_a_signature_from_the_wrong_key_is_refused(tmp_path: Path) -> None:
    """A run that verified against whatever key signed the file would verify
    nothing, so the fingerprint is compared even when gpg is happy."""

    class Signed(Runner):
        def run(self, argv, *, check=True, input_text=None, timeout=3600.0):  # type: ignore[no-untyped-def]
            from gentoo_install.exec.runner import Result

            return Result(argv=tuple(argv), returncode=0, stdout="VALIDSIG DEADBEEF", stderr="", seconds=0.0)

    with pytest.raises(IntegrityError, match="not the pinned"):
        fetch._verify_signature(tmp_path / "x.DIGESTS", "ABC123", Signed(log=lambda line: None))


def test_a_mirror_that_never_answers_goes_last_rather_than_disappearing() -> None:
    """An empty mirror list is worse than a slow mirror: Portage with no mirror
    at all cannot fetch anything."""
    candidates = ("https://mirror.invalid.example./", "https://other.invalid.example./")
    ranked = fetch.rank_mirrors(candidates)
    assert set(ranked) == set(candidates)
    assert len(ranked) == len(candidates)


def test_the_measured_order_is_used_only_when_the_configuration_asks() -> None:
    from .layouts import config as layout
    from .recorder import Recorder
    from gentoo_install.model.config import MirrorConfig, PortageConfig
    from gentoo_install.plan import portage as plan_portage

    measured = Recorder()
    for operation in plan_portage.build(
        replace(layout(), portage=PortageConfig(mirrors=MirrorConfig(speed_test=True))),
        "https://distfiles.gentoo.org",
    ):
        if isinstance(operation, plan_portage.WriteMakeConf):
            operation.apply(measured)
    assert measured.argv_starting("rank-mirrors")

    plain = Recorder()
    for operation in plan_portage.build(layout(), "https://distfiles.gentoo.org"):
        if isinstance(operation, plan_portage.WriteMakeConf):
            operation.apply(plain)
    assert not plain.argv_starting("rank-mirrors")


def test_no_passphrase_is_invented_when_none_was_supplied() -> None:
    with pytest.raises(IntegrityError, match="no passphrase"):
        fetch.passphrase_for(DeviceId("crypt"))
