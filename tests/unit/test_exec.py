from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

import pytest

from gentoo_install.errors import (
    CommandFailed,
    ConfigError,
    DeviceNotFound,
    IntegrityError,
    PreflightFailed,
)
from gentoo_install.exec import apply, fetch, preflight
from gentoo_install.exec.probe import Machine as ProbedMachine
from gentoo_install.exec.probe import Probe
from gentoo_install.exec.runner import Result, Runner, under
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


def test_a_uefi_boot_without_efivarfs_is_fatal(tmp_path: Path) -> None:
    """`/sys/firmware/efi` existing says the kernel booted through EFI; it does
    not say the variables can be written. `efibootmgr --create` is what the
    ZFSBootMenu install runs, and GRUB's own NVRAM entry needs the same."""
    report = preflight.inspect(config(), described(efi_variables=False), probe_of(tmp_path))
    assert any("efivarfs" in reason for reason in report.fatal), report.fatal

    fine = preflight.inspect(config(), described(efi_variables=True), probe_of(tmp_path))
    assert not [one for one in fine.fatal if "efivarfs" in one], fine.fatal


def test_a_bios_configuration_needs_no_firmware_variables(tmp_path: Path) -> None:
    """Nothing writes an EFI variable on a BIOS install, so their absence is
    not a reason to refuse one."""
    on_bios = replace(
        present(), bootloader=BootloaderConfig(kind=Bootloader.GRUB, firmware=Firmware.BIOS)
    )
    report = preflight.inspect(on_bios, described(efi_variables=False), probe_of(tmp_path))
    assert not [one for one in report.fatal if "efivarfs" in one], report.fatal


def test_thirty_two_bit_efi_firmware_is_fatal_for_an_amd64_install(tmp_path: Path) -> None:
    """An x86_64 CPU can boot through 32-bit EFI. Only `/sys/firmware/efi` was
    read, so the install finished and the firmware then refused the amd64 EFI
    executable it was handed."""
    report = preflight.inspect(config(), described(efi_bits=32), probe_of(tmp_path))
    assert any("32-bit EFI" in reason for reason in report.fatal), report.fatal


def test_efi_width_the_kernel_does_not_publish_is_not_a_failure(tmp_path: Path) -> None:
    """`fw_platform_size` arrived in Linux 4.4, and a machine older than that
    is not evidence of a 32-bit platform."""
    report = preflight.inspect(config(), described(efi_bits=0), probe_of(tmp_path))
    assert not [reason for reason in report.fatal if "EFI" in reason], report.fatal


def test_a_bios_configuration_on_a_uefi_boot_is_only_a_warning(tmp_path: Path) -> None:
    on_bios = replace(
        present(), bootloader=BootloaderConfig(kind=Bootloader.GRUB, firmware=Firmware.BIOS)
    )
    report = preflight.inspect(on_bios, described(uefi=True), probe_of(tmp_path))
    assert not [one for one in report.fatal if "boot" in one], report.fatal
    assert report.warnings


def test_little_memory_warns_instead_of_stopping(tmp_path: Path) -> None:
    report = preflight.inspect(present(), described(memory_bytes=2 * 1024**3), probe_of(tmp_path))
    # Only about memory: `present()` points its disk at /dev/null, which
    # reports no size, and an unreadable capacity is fatal on its own.
    assert not [one for one in report.fatal if "memory" in one], report.fatal
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


def test_a_medium_without_the_release_key_fetches_one_rather_than_stopping(tmp_path: Path) -> None:
    """Alpine, Debian, Arch, Fedora and openSUSE ship no
    `/usr/share/openpgp-keys/gentoo-release.asc`, and the installer has to run
    on all of them. The pinned fingerprint is what trust rests on."""
    report = preflight.inspect(present(), described(release_key=False), probe_of(tmp_path))
    assert not any("release key" in reason for reason in report.fatal)
    assert any("release key is fetched" in reason for reason in report.warnings)


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


def test_a_uuid_is_read_from_the_device_and_not_from_the_cache(tmp_path: Path) -> None:
    """blkid's cache still holds the previous filesystem's UUID after a
    reformat, and that UUID would go into fstab and the kernel command line."""
    seen: list[tuple[str, ...]] = []

    class Recording(Runner):
        def run(self, argv, *, check=True, input_text=None, timeout=None):  # type: ignore[no-untyped-def]
            seen.append(tuple(argv))
            return Result(
                argv=tuple(argv), returncode=0, stdout="1234-ABCD", stderr="", seconds=0.0
            )

    probe = Probe(runner=Recording(log=lambda line: None), work=tmp_path)
    assert probe.uuid_of("/dev/vdb2", DeviceId("rootfs")) == "1234-ABCD"
    called = next(argv for argv in seen if argv[0] == "blkid")
    assert "--probe" in called


def test_the_disk_a_mirrored_root_boots_from_is_the_same_on_every_run(tmp_path: Path) -> None:
    """`ancestors_of` is a frozenset and Python randomises string hashing per
    process, so a RAID1 root answered with a different disk each run and
    `grub-install` wrote the bootloader to whichever came out."""
    from gentoo_install.exec.apply import Machine
    from gentoo_install.model.device import MdRaid, RaidLevel
    from gentoo_install.model.parse import load

    layout = load(Path("tests/fixtures/vm-mdraid.toml"))
    array = layout.disk.graph.of_type(MdRaid)[0]
    assert array.level is RaidLevel.RAID1 and len(array.members) > 1

    class Echoing(Probe):
        """The selector back, so the test is about which node was picked and
        not about which device nodes this machine happens to have."""

        def resolve(self, device: DeviceId, selector: str) -> str:
            return selector

    probe = Echoing(runner=runner(tmp_path), work=tmp_path)
    machine = Machine(
        config=layout, runner=runner(tmp_path), probe=probe, work=tmp_path,
        mountpoint=Path("/mnt/gentoo"),
    )
    graph = layout.disk.graph
    first = next(
        graph[one]
        for one in sorted(graph.ancestors_of(array.id))
        if isinstance(graph[one], Existing)
    )
    assert isinstance(first, Existing)
    for _ in range(4):
        assert machine.containing_disk(array.id) == first.selector


def test_every_command_that_has_to_be_the_real_one_is_checked(tmp_path: Path) -> None:
    """A busybox applet satisfies `which` and then rejects the flags, which is
    a failure after the disks are written. One case per entry in the table."""
    busybox = "BusyBox v1.36.1 (2024-01-01 00:00:00 UTC) multi-call binary."
    for command, (wanted, _) in preflight.GNU_ONLY.items():
        applet = described(versions={name: busybox for name in preflight.GNU_ONLY})
        report = preflight.inspect(present(), applet, probe_of(tmp_path))
        assert any(f"{command} is not {wanted}" in reason for reason in report.fatal), command

    real = described(
        versions={
            "tar": "tar (GNU tar) 1.35",
            "mount": "mount from util-linux 2.42.2",
        }
    )
    assert not any("is not" in reason for reason in preflight.inspect(
        present(), real, probe_of(tmp_path)
    ).fatal)


def test_the_commands_whose_implementation_matters_are_the_ones_probed(tmp_path: Path) -> None:
    """`preflight` owns the table and `probe` reads the versions, so a command
    added to `GNU_ONLY` is asked for without a second list to update."""
    asked: list[str] = []

    class Watching(Probe):
        def versions(self, wanted: Iterable[str]) -> dict[str, str]:
            asked.extend(wanted)
            return {}

    preflight.check(present(), Watching(runner=runner(tmp_path), work=tmp_path))
    assert set(asked) == set(preflight.GNU_ONLY)


def test_a_disk_is_named_by_id_without_shelling_out_to_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`find -lname` is a GNU predicate and busybox answers `unrecognized`, so
    Alpine got `/dev/sda` written into the configuration; that name points at a
    different disk as soon as another one is plugged in."""
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "wwn-0x5000c500").symlink_to("../../sda")
    (by_id / "ata-ST8000_WWZ472M1").symlink_to("../../sda")
    (by_id / "ata-OTHER_DISK").symlink_to("../../sdb")
    monkeypatch.setattr(Probe, "BY_ID", by_id)

    probe = probe_of(tmp_path)
    # The wwn is stable and unreadable, so the model name wins.
    assert probe._stable_name("/dev/sda") == str(by_id / "ata-ST8000_WWZ472M1")
    assert probe._stable_name("/dev/sdb") == str(by_id / "ata-OTHER_DISK")
    # A disk with no by-id name keeps its kernel name rather than borrowing one.
    assert probe._stable_name("/dev/sdc") == "/dev/sdc"

    monkeypatch.setattr(Probe, "BY_ID", tmp_path / "absent")
    assert probe._stable_name("/dev/sda") == "/dev/sda"


def test_a_table_bigger_than_its_disk_is_refused_before_the_old_one_is_erased(
    tmp_path: Path,
) -> None:
    """`sgdisk --new` fails only after `wipefs --all` and `sgdisk --zap-all`
    have run, so the check has to be a preflight one: by then the table that
    was on the disk is gone and there is nothing to go back to."""
    from gentoo_install.model.device import Partition, PartitionRole
    from gentoo_install.model.size import Size

    nodes: list[Node] = [
        replace(node, size=Size.parse("40GiB"))
        if isinstance(node, Partition) and node.role is PartitionRole.DATA
        else node
        for node in present().disk.graph.nodes.values()
    ]
    oversized = config([replace(node, selector="/dev/null") if isinstance(node, Existing) else node
                        for node in nodes])

    class Sized(Probe):
        def resolve(self, device: DeviceId, selector: str) -> str:
            return selector

        def disk_bytes(self, disk: str) -> int:
            return 20 * 1024**3

    report = preflight.inspect(oversized, described(), Sized(runner=runner(tmp_path), work=tmp_path))
    assert any("does not fit" in reason for reason in report.fatal)

    # The same table on a disk that holds it raises nothing.
    class Roomy(Sized):
        def disk_bytes(self, disk: str) -> int:
            return 200 * 1024**3

    roomy = preflight.inspect(oversized, described(), Roomy(runner=runner(tmp_path), work=tmp_path))
    assert not any("does not fit" in reason for reason in roomy.fatal)


def test_a_cpu_flag_is_renamed_and_never_swapped_for_another(tmp_path: Path) -> None:
    """`bmi1` was mapped to `avx2`, so an AMD Piledriver, which has the first
    and not the second, got `avx2` in `CPU_FLAGS_X86` and every package built
    for it died on SIGILL. The four below are genuine name differences between
    /proc/cpuinfo and `cpu_flags_x86.desc`."""
    from gentoo_install.exec.probe import CPU_FLAGS

    renamed = {kernel: portage for kernel, portage in CPU_FLAGS.items() if kernel != portage}
    assert renamed == {"fma": "fma3", "pclmulqdq": "pclmul", "sha_ni": "sha", "pni": "sse3"}
    assert CPU_FLAGS["bmi1"] == "bmi1" and CPU_FLAGS["bmi2"] == "bmi2"
    # One kernel name per portage name: two keys sharing a value is a swap.
    values = [portage for portage in CPU_FLAGS.values()]
    assert len(values) == len(set(values))


def test_every_request_names_the_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    """`paste.gentoozh.org` answers 403 to urllib's default agent, so a key
    fetched from a paste failed before every request carried a name."""
    import urllib.request

    from gentoo_install.exec import fetch

    seen: list[str] = []

    class Answer:
        def __enter__(self) -> Answer:
            return self

        def __exit__(self, *unused: object) -> None:
            return None

        def read(self, *unused: object) -> bytes:
            return b"ssh-ed25519 AAAA test@example"

        headers: dict[str, str] = {}

    def opened(request: object, timeout: float = 0.0) -> Answer:
        assert isinstance(request, urllib.request.Request)
        seen.append(request.get_header("User-agent") or "")
        return Answer()

    monkeypatch.setattr(urllib.request, "urlopen", opened)
    fetch.text("https://paste.gentoozh.org/abcdef")
    fetch.network_time()
    assert seen == [fetch.USER_AGENT, fetch.USER_AGENT]


def test_a_medium_with_no_zfs_stops_before_the_disks_are_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer runs off whatever medium is to hand, and most of them have
    no ZFS; `zpool create` finds that out after the disks are partitioned."""
    from gentoo_install.model.parse import load

    pooled = load(Path("tests/fixtures/vm-zfs.toml"))
    probe = probe_of(tmp_path)
    monkeypatch.setattr(Probe, "zfs_support", lambda self: "this live system has no zpool")
    report = preflight.inspect(pooled, described(commands=frozenset()), probe)
    assert any("has no zpool, and this configuration makes a pool" in one for one in report.fatal)


def test_a_medium_with_no_zfs_stops_nothing_that_makes_no_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Probe, "zfs_support", lambda self: "this live system has no zpool")
    report = preflight.inspect(present(), described(), probe_of(tmp_path))
    assert not any("pool" in one for one in report.fatal)


def test_the_zfs_check_names_the_command_that_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two halves fail separately: no userland at all, and a userland whose
    module will not load."""
    import shutil as shutil_module

    from gentoo_install.exec import probe as probe_module

    probe = probe_of(tmp_path)
    monkeypatch.setattr(shutil_module, "which", lambda name: None)
    assert probe.zfs_support() == "this live system has no zpool or zfs"

    monkeypatch.setattr(shutil_module, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(probe_module.Probe, "ZFS_LOADED", (tmp_path / "absent",))
    assert "cannot load the zfs kernel module" in probe.zfs_support()


def test_a_loaded_module_needs_no_modprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kernel with ZFS built in has no /sys/module entry to find, so either
    marker is enough and neither is asked for twice."""
    import shutil as shutil_module

    from gentoo_install.exec import probe as probe_module

    here = tmp_path / "dev-zfs"
    here.touch()
    monkeypatch.setattr(shutil_module, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(probe_module.Probe, "ZFS_LOADED", (here,))
    assert probe_of(tmp_path).zfs_support() == ""


def test_an_expired_key_is_not_read_as_a_fingerprint() -> None:
    """`EXPKEYSIG` ends in the username, per gpg's DETAILS, so taking its last
    field pins the installer against `<releng@gentoo.org>` and reports a good
    signature as signed by the wrong key. gpg emits VALIDSIG beside it."""
    from gentoo_install.exec.fetch import _signing_key

    status = (
        "[GNUPG:] EXPKEYSIG 534E4209AB49EEE1 Gentoo Linux Release Engineering "
        "(Automated Weekly Release Key) <releng@gentoo.org>\n"
        "[GNUPG:] VALIDSIG 534E4209AB49EEE1 2026-08-08 1 4 0 1 8 00 PRIMARYFPR\n"
    )
    assert _signing_key(status) == "PRIMARYFPR"


def test_a_status_with_no_valid_signature_names_no_key() -> None:
    from gentoo_install.exec.fetch import _signing_key

    assert _signing_key("[GNUPG:] BADSIG 534E4209 Gentoo <releng@gentoo.org>\n") is None


def test_a_disk_whose_state_cannot_be_read_counts_as_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """busybox `swapon` has no `--show`, and answering no to a guard that
    exists to refuse let a run repartition a disk holding an active swap."""
    from gentoo_install.exec.runner import Result

    probe = probe_of(tmp_path)
    monkeypatch.setattr(Path, "is_block_device", lambda self: True)
    monkeypatch.setattr(
        probe.runner,
        "run",
        lambda argv, **rest: Result(
            argv=tuple(argv), stdout="swapon: unknown option", stderr="", returncode=1, seconds=0.0
        ),
    )
    assert probe.mounted("/dev/sdz") is True


def test_a_table_edited_in_place_is_checked_for_mounts_too(tmp_path: Path) -> None:
    """The disk carries `wipe=False` when its table is only edited, so the one
    guard against repartitioning a disk in use skipped exactly the case that
    deletes a partition from a disk holding another operating system."""
    from gentoo_install.exec.preflight import _disks_at_risk
    from gentoo_install.model.device import DeviceGraph, PartitionTable

    from .layouts import ext4_on_gpt, i

    nodes = [
        replace(node, wipe=False) if isinstance(node, Existing) else node
        for node in ext4_on_gpt()
    ]
    nodes = [
        replace(node, create=False, remove=(2,)) if isinstance(node, PartitionTable) else node
        for node in nodes
    ]
    graph = DeviceGraph.build(nodes)
    assert [disk.id for disk in _disks_at_risk(graph)] == [i("disk")]

    # Nothing edits the table: every partition survives and no disk is at risk.
    kept = DeviceGraph.build(
        [
            replace(node, create=False, remove=()) if isinstance(node, PartitionTable) else node
            for node in nodes
        ]
    )
    assert _disks_at_risk(kept) == []


def test_the_bootloader_disk_of_a_reused_partition_is_the_disk(tmp_path: Path) -> None:
    """A reused partition is an `Existing` whose selector names the partition,
    and there is no whole-disk node above it. `grub-install /dev/sda2` writes
    into a partition boot sector or refuses, and the machine does not boot."""
    from gentoo_install.exec.apply import Machine
    from gentoo_install.model.config import DiskConfig
    from gentoo_install.model.device import (
        DeviceGraph,
        DeviceId,
        Filesystem,
        FilesystemType,
        Mountpoint,
    )
    from pathlib import PurePosixPath

    from .layouts import config as base

    nodes = [
        Existing(id=DeviceId("part"), selector="/dev/null", wipe=False),
        Filesystem(
            id=DeviceId("fs"), device=DeviceId("part"), kind=FilesystemType.EXT4, create=False
        ),
        Mountpoint(id=DeviceId("root"), source=DeviceId("fs"), path=PurePosixPath("/")),
    ]
    reused = replace(
        base(), disk=DiskConfig(graph=DeviceGraph.build(nodes), root=DeviceId("root"))
    )
    # A real Probe over a runner that answers `lsblk`, not a monkeypatched
    # `disk_of`: patching the method under test hid that the production path
    # asked for a cached path `resolve()` never writes.
    asked: list[list[str]] = []

    class Answering(Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            timeout: float | None = None,
        ) -> Result:
            asked.append(list(argv))
            said = "sda\n" if argv[0] == "lsblk" and "PKNAME" in argv else ""
            return Result(argv=tuple(argv), returncode=0, stdout=said, stderr="", seconds=0.0)

    answering = Answering(log=lambda line: None)
    probe = Probe(runner=answering, work=tmp_path)
    machine = Machine(config=reused, probe=probe, runner=answering, work=tmp_path)
    assert machine.containing_disk(DeviceId("root")) == "/dev/sda"
    assert any("PKNAME" in argv for argv in asked), asked


def test_a_verification_marker_only_covers_the_bytes_it_was_written_for(tmp_path: Path) -> None:
    """The marker used to be an empty file named after the archive, so replacing
    or corrupting the archive after a verified run let the next one extract it
    unchecked."""
    from gentoo_install.exec import fetch

    archive = tmp_path / "stage3-amd64-systemd-1.tar.xz"
    archive.write_bytes(b"the verified bytes")
    marker = tmp_path / f"{archive.name}.verified"
    key = "13EBBDBEDE7A12775DFDB1BABB572E0E2D182910"
    marker.write_text(
        f"{fetch.MARKER_SCHEMA}\n{archive.name}\n{fetch._sha512(archive)}\n{key.lower()}\n"
    )
    assert fetch._marker_matches(marker, archive, key)

    archive.write_bytes(b"different bytes entirely")
    assert not fetch._marker_matches(marker, archive, key)


def test_an_empty_or_foreign_marker_is_not_evidence(tmp_path: Path) -> None:
    """An empty marker is what an older installer wrote and what anyone can
    copy, and a marker naming another archive or another key covers neither."""
    from gentoo_install.exec import fetch

    archive = tmp_path / "stage3-amd64-systemd-1.tar.xz"
    archive.write_bytes(b"the verified bytes")
    marker = tmp_path / f"{archive.name}.verified"
    key = "13EBBDBEDE7A12775DFDB1BABB572E0E2D182910"
    digest = fetch._sha512(archive)

    for said in (
        "",
        f"{fetch.MARKER_SCHEMA}\nother.tar.xz\n{digest}\n{key.lower()}\n",
        f"{fetch.MARKER_SCHEMA}\n{archive.name}\n{digest}\ndeadbeef\n",
        f"older-schema\n{archive.name}\n{digest}\n{key.lower()}\n",
    ):
        marker.write_text(said)
        assert not fetch._marker_matches(marker, archive, key), said


def test_a_symlink_in_the_target_cannot_reach_the_live_system(tmp_path: Path) -> None:
    """`target / path` is a lexical join, so an absolute symlink under the
    target -- shipped by a stage3 or left on a reused filesystem -- made the
    installer write to the live system as root."""
    from gentoo_install.exec.runner import TargetEscape

    target = tmp_path / "mnt"
    (target / "etc").mkdir(parents=True)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("the live system")
    (target / "etc/example").symlink_to(sentinel)

    probe = probe_of(tmp_path)
    machine = apply.Machine(
        config=config(), probe=probe, runner=probe.runner, work=tmp_path, mountpoint=target
    )
    with pytest.raises(TargetEscape):
        machine.write(PurePosixPath("/etc/example"), "written by the installer")
    assert sentinel.read_text() == "the live system"


def test_a_symlinked_parent_directory_cannot_reach_the_live_system(tmp_path: Path) -> None:
    """The final component is not the only way out: a directory on the way can
    be the symlink."""
    from gentoo_install.exec.runner import TargetEscape

    target = tmp_path / "mnt"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "etc").symlink_to(outside)

    probe = probe_of(tmp_path)
    machine = apply.Machine(
        config=config(), probe=probe, runner=probe.runner, work=tmp_path, mountpoint=target
    )
    with pytest.raises(TargetEscape):
        machine.write(PurePosixPath("/etc/example"), "written by the installer")
    assert not (outside / "example").exists()


def test_an_ordinary_file_in_the_target_is_still_written_and_read(tmp_path: Path) -> None:
    target = tmp_path / "mnt"
    target.mkdir()
    probe = probe_of(tmp_path)
    machine = apply.Machine(
        config=config(), probe=probe, runner=probe.runner, work=tmp_path, mountpoint=target
    )
    machine.write(PurePosixPath("/etc/portage/make.conf"), 'USE="x"\n', mode=0o600)
    written = target / "etc/portage/make.conf"
    assert written.read_text() == 'USE="x"\n'
    assert written.stat().st_mode & 0o777 == 0o600
    machine.append(PurePosixPath("/etc/portage/make.conf"), 'FEATURES="y"\n')
    assert machine.read(PurePosixPath("/etc/portage/make.conf")).endswith('FEATURES="y"\n')
    assert machine.read(PurePosixPath("/etc/nothing-here")) == ""


def test_a_reused_partition_gets_its_number_from_the_machine(tmp_path: Path) -> None:
    """A reused esp carries no index: the operator named a device, not a
    number. `partition_index` refused it, so ZFSBootMenu on an existing esp
    failed with `is not a partition`."""
    from gentoo_install.exec.apply import Machine
    from gentoo_install.model.config import DiskConfig
    from gentoo_install.model.device import DeviceGraph, DeviceId, Filesystem, FilesystemType, Mountpoint

    from .layouts import config as base

    nodes = [
        Existing(id=DeviceId("part"), selector="/dev/null", wipe=False),
        Filesystem(
            id=DeviceId("fs"), device=DeviceId("part"), kind=FilesystemType.VFAT, create=False
        ),
        Mountpoint(id=DeviceId("root"), source=DeviceId("fs"), path=PurePosixPath("/")),
    ]
    reused = replace(
        base(), disk=DiskConfig(graph=DeviceGraph.build(nodes), root=DeviceId("root"))
    )

    class Answering(Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            timeout: float | None = None,
        ) -> Result:
            said = "3\n" if argv[0] == "lsblk" and "PARTN" in argv else ""
            return Result(argv=tuple(argv), returncode=0, stdout=said, stderr="", seconds=0.0)

    answering = Answering(log=lambda line: None)
    machine = Machine(
        config=reused,
        probe=Probe(runner=answering, work=tmp_path),
        runner=answering,
        work=tmp_path,
    )
    assert machine.partition_index(DeviceId("part")) == 3


def test_a_disk_holding_an_imported_pool_is_in_use(tmp_path: Path) -> None:
    """A `zfs_member` partition carries no block-device mountpoint even while
    its datasets provide `/` and `/home`, so every mountpoint column read blank
    and preflight would authorise repartitioning the running system's disk."""

    class Answering(Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            timeout: float | None = None,
        ) -> Result:
            if argv[0] == "zpool":
                said = (
                    "rpool\t912G\t753G\t159G\t-\t-\t53%\t82%\t1.00x\tONLINE\t-\n"
                    "\tmirror-0\t912G\t-\t-\t-\t-\t-\t-\t-\tONLINE\t-\n"
                    f"\t{tmp_path}/disk1p3\t915G\t-\t-\t-\t-\t-\t-\t-\tONLINE\t-\n"
                )
            else:
                said = ""
            return Result(argv=tuple(argv), returncode=0, stdout=said, stderr="", seconds=0.0)

    probe = Probe(runner=Answering(log=lambda line: None), work=tmp_path)
    assert probe._in_an_imported_pool(f"{tmp_path}/disk1")
    assert not probe._in_an_imported_pool(f"{tmp_path}/disk2")


def test_a_machine_with_no_zpool_command_is_not_called_busy(tmp_path: Path) -> None:
    """`zpool` is absent on most live media, and that is not evidence."""

    class Failing(Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            timeout: float | None = None,
        ) -> Result:
            return Result(
                argv=tuple(argv), returncode=127, stdout="zpool: not found", stderr="", seconds=0.0
            )

    probe = Probe(runner=Failing(log=lambda line: None), work=tmp_path)
    assert not probe._in_an_imported_pool("/dev/sda")


def test_a_disk_that_reports_no_size_stops_the_run(tmp_path: Path) -> None:
    """`disk_bytes` answers 0 for a nonzero exit, empty output or nonnumeric
    text, and the check used to skip on that: `wipefs` and `sgdisk --zap-all`
    then ran before anything discovered the layout did not fit."""
    report = preflight.inspect(present(), described(), probe_of(tmp_path))
    assert any("did not report a size" in one for one in report.fatal), report.fatal


def test_an_edited_table_counts_what_it_keeps(tmp_path: Path) -> None:
    """`claimed` summed only the new partitions, so a 4 GiB addition beside a
    retained 18 GiB partition fitted a 20 GiB disk on paper."""
    from gentoo_install.exec import preflight as check
    from gentoo_install.model.config import DiskConfig
    from gentoo_install.model.device import (
        DeviceGraph,
        DeviceId,
        Partition,
        PartitionRole,
        PartitionTable,
        TableType,
    )
    from gentoo_install.model.size import Size

    twenty = 20 * 1024**3
    nodes = [
        Existing(id=DeviceId("disk"), selector="/dev/null", wipe=False),
        PartitionTable(
            id=DeviceId("table"), disk=DeviceId("disk"), table=TableType.GPT, create=False
        ),
        Partition(
            id=DeviceId("new"),
            table=DeviceId("table"),
            index=2,
            role=PartitionRole.DATA,
            size=Size(4 * 1024**3),
        ),
    ]
    installation = replace(
        config(), disk=DiskConfig(graph=DeviceGraph.build(nodes), root=DeviceId("new"))
    )

    class Answering(Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            timeout: float | None = None,
        ) -> Result:
            said = ""
            if argv[0] == "lsblk" and "SIZE" in argv and "PARTN,SIZE" not in argv:
                said = f"{twenty}\n"
            elif argv[0] == "lsblk" and "PARTN,SIZE" in argv:
                said = f"1 {18 * 1024**3}\n"
            return Result(argv=tuple(argv), returncode=0, stdout=said, stderr="", seconds=0.0)

    probe = Probe(runner=Answering(log=lambda line: None), work=tmp_path)
    problems = check._capacity_problems(installation, probe)
    assert problems, "18 GiB kept plus 4 GiB new does not fit 20 GiB"


def test_a_command_that_answers_nothing_is_named_rather_than_crashing(tmp_path: Path) -> None:
    """`versions()` discarded the exit status, so a command present on PATH
    that exits nonzero with no output reached `splitlines()[0]` and raised
    IndexError where a named preflight problem belongs."""
    said = preflight._busybox_problems(described(versions={"tar": ""}))
    assert any("answered nothing" in one for one in said), said

    wrong = preflight._busybox_problems(described(versions={"tar": "tar (busybox) 1.36"}))
    assert any("is not GNU tar" in one or "is not" in one for one in wrong), wrong


def test_a_version_probe_records_only_a_successful_reply(tmp_path: Path) -> None:
    class Answering(Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            timeout: float | None = None,
        ) -> Result:
            failed = argv[0] == "sh"
            return Result(
                argv=tuple(argv),
                returncode=1 if failed else 0,
                stdout="" if failed else "GNU coreutils 9.5\nmore\n",
                stderr="",
                seconds=0.0,
            )

    probe = Probe(runner=Answering(log=lambda line: None), work=tmp_path)
    said = probe.versions(["sh", "cat"])
    assert said["sh"] == ""
    assert said["cat"] == "GNU coreutils 9.5"
