from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.errors import (
    CommandFailed,
    ConfigError,
    DeviceNotFound,
    IntegrityError,
    PreflightFailed,
)
from gentoo_install.exec import fetch, preflight
from gentoo_install.exec.probe import Machine as ProbedMachine
from gentoo_install.exec.probe import Probe
from gentoo_install.exec.runner import Runner, under
from gentoo_install.model.config import Bootloader, BootloaderConfig, Firmware, InstallConfig
from gentoo_install.model.device import DeviceId, Existing, Node

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
        "release_key": True,
    }
    base.update(fields)
    return ProbedMachine(**base)  # type: ignore[arg-type]


def probe_of(tmp_path: Path) -> Probe:
    return Probe(runner=runner(tmp_path), work=tmp_path)


def present() -> InstallConfig:
    """The layout with its disk pointed at something every machine has, so a
    check about firmware or memory is not answered by a missing device."""
    nodes: list[Node] = [
        replace(node, selector="/dev/null") if isinstance(node, Existing) else node
        for node in ext4_on_gpt()
    ]
    return config(nodes)


def test_a_command_that_fails_raises_with_its_output(tmp_path: Path) -> None:
    with pytest.raises(CommandFailed, match="exited 3"):
        runner(tmp_path).run(["sh", "-c", "echo trouble >&2; exit 3"])


def test_a_failure_names_the_error_rather_than_the_last_thing_printed(tmp_path: Path) -> None:
    """Portage keeps printing after an error, so the tail of the output is news
    items and the cause is further up."""
    script = (
        "echo 'ERROR: sys-fs/zfs failed (setup phase): Kernel not configured';"
        "echo ' * IMPORTANT: 22 news items need reading';"
        "echo ' * Use eselect news read';"
        "echo ' * Regenerating GNU info directory index';"
        "echo ' * Processed 109 info files';"
        "echo ' * nothing to see here'; exit 1"
    )
    with pytest.raises(CommandFailed, match="Kernel not configured"):
        runner(tmp_path).run(["sh", "-c", script])


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
        probe_of(tmp_path).resolve(DeviceId("disk"), "/dev/definitely-absent")


def test_a_resolved_device_survives_a_restart_of_the_installer(tmp_path: Path) -> None:
    first = probe_of(tmp_path)
    first.remember(DeviceId("disk"), "/dev/vda")
    second = probe_of(tmp_path)
    second.load()
    assert second.path_of(DeviceId("disk")) == "/dev/vda"


def test_the_required_commands_come_from_the_layout() -> None:
    plain = preflight.required_commands(config())
    assert "mkfs.ext4" in plain and "sgdisk" in plain
    assert "cryptsetup" not in plain and "zpool" not in plain


def test_every_filesystem_the_installer_can_make_has_a_required_command() -> None:
    """The list is derived from the table the operations use, so a filesystem
    added there cannot be silently absent here."""
    from gentoo_install.model.device import FilesystemType
    from gentoo_install.plan.disk import MKFS

    assert set(MKFS) == set(FilesystemType)

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
        present(), bootloader=BootloaderConfig(kind=Bootloader.GRUB, firmware=Firmware.BIOS)
    )
    report = preflight.inspect(on_bios, described(uefi=True), probe_of(tmp_path))
    assert not report.fatal
    assert report.warnings


def test_little_memory_warns_instead_of_stopping(tmp_path: Path) -> None:
    report = preflight.inspect(present(), described(memory_bytes=2 * 1024**3), probe_of(tmp_path))
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


def test_a_device_that_is_not_a_block_device_is_not_reported_as_mounted(tmp_path: Path) -> None:
    """`lsblk` writes its complaint to stderr, which the runner merges into
    stdout; taking that as output would make every disk look mounted."""
    assert probe_of(tmp_path).mounted("/dev/null") is False


def test_a_signature_from_the_wrong_key_is_refused(tmp_path: Path) -> None:
    """A run that verified against whatever key signed the file would verify
    nothing, so the fingerprint is compared even when gpg is happy."""

    class Signed(Runner):
        def run(self, argv, *, check=True, input_text=None, timeout=3600.0):  # type: ignore[no-untyped-def]
            from gentoo_install.exec.runner import Result

            return Result(
                argv=tuple(argv),
                returncode=0,
                stdout="[GNUPG:] VALIDSIG SUBKEY 2026-08-08 1 4 0 1 8 00 DEADBEEF\n",
                stderr="",
                seconds=0.0,
            )

    with pytest.raises(IntegrityError, match="not the pinned"):
        fetch._verify_signature(tmp_path / "x.DIGESTS", "ABC123", Signed(log=lambda line: None))


def test_the_pin_names_the_primary_key_rather_than_the_subkey_that_signed(tmp_path: Path) -> None:
    """Gentoo signs with a subkey. Comparing VALIDSIG's second field rejects a
    signature that is good, which is how the pinned value was found to name the
    primary key."""

    class Subkey(Runner):
        def run(self, argv, *, check=True, input_text=None, timeout=None):  # type: ignore[no-untyped-def]
            from gentoo_install.exec.runner import Result

            return Result(
                argv=tuple(argv),
                returncode=0,
                stdout="[GNUPG:] VALIDSIG 534E4209 2026-08-08 1 4 0 1 8 00 PRIMARYFPR\n",
                stderr="",
                seconds=0.0,
            )

    fetch._verify_signature(tmp_path / "x.DIGESTS", "primaryfpr", Subkey(log=lambda line: None))


def test_a_fingerprint_that_only_appears_in_the_output_is_not_a_signature(tmp_path: Path) -> None:
    """The archive name comes from the mirror, so hex inside it must not be
    mistaken for the key that signed the file."""

    class Mentioned(Runner):
        def run(self, argv, *, check=True, input_text=None, timeout=None):  # type: ignore[no-untyped-def]
            from gentoo_install.exec.runner import Result

            return Result(
                argv=tuple(argv),
                returncode=0,
                stdout="gpg: Good signature from stage3-amd64-ABC123.tar.xz\n",
                stderr="",
                seconds=0.0,
            )

    with pytest.raises(IntegrityError, match="does not verify"):
        fetch._verify_signature(tmp_path / "x.DIGESTS", "ABC123", Mentioned(log=lambda line: None))


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
    """A configuration error, not an integrity one: exit code 3 says the data
    could not be trusted, and a missing passphrase is not that."""
    with pytest.raises(ConfigError, match="names no passphrase_file"):
        fetch.passphrase_for(DeviceId("crypt"), "")


def test_a_passphrase_comes_from_the_file_the_layout_names(tmp_path: Path) -> None:
    """A path, never the passphrase: the configuration is copied into the
    target and the log is what people paste into bug reports."""
    source = tmp_path / "key"
    source.write_text("open sesame\n")
    assert fetch.passphrase_for(DeviceId("crypt"), str(source)) == "open sesame"


def test_an_empty_or_missing_passphrase_file_is_named(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("")
    with pytest.raises(ConfigError, match="is empty"):
        fetch.passphrase_for(DeviceId("crypt"), str(empty))
    with pytest.raises(ConfigError, match="cannot be read"):
        fetch.passphrase_for(DeviceId("crypt"), str(tmp_path / "absent"))


def test_a_medium_without_the_release_key_is_stopped_before_the_download(tmp_path: Path) -> None:
    report = preflight.inspect(present(), described(release_key=False), probe_of(tmp_path))
    assert any("signature cannot be checked" in reason for reason in report.fatal)


def test_a_short_zfs_passphrase_is_caught_before_the_disk_is_touched(tmp_path: Path) -> None:
    """`zpool create` rejects it only after the vdevs have been partitioned,
    which leaves the disk wiped and the install stopped."""
    from gentoo_install.model.device import ZfsPool

    from .layouts import zfs_root

    def with_key(source: str) -> InstallConfig:
        nodes = [
            replace(node, passphrase_file=source) if isinstance(node, ZfsPool) else node
            for node in zfs_root()
        ]
        return replace(
            config(nodes),
            bootloader=BootloaderConfig(kind=Bootloader.ZFSBOOTMENU, firmware=Firmware.UEFI),
        )

    key = tmp_path / "key"
    key.write_text("1234567")
    problems = preflight._passphrase_problems(with_key(str(key)))
    assert len(problems) == 1 and "at least 8" in problems[0]

    key.write_text("12345678")
    assert preflight._passphrase_problems(with_key(str(key))) == []
    assert "cannot be read" in preflight._passphrase_problems(with_key(str(tmp_path / "gone")))[0]
    assert "names no passphrase_file" in preflight._passphrase_problems(with_key(""))[0]
