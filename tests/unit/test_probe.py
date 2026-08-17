# SPDX-License-Identifier: GPL-2.0-or-later
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Sequence

import json
import pytest

from gentoo_install.exec import probe
from gentoo_install.exec.probe import Probe
from gentoo_install.exec.runner import Result, Runner


class DiskListing(Runner):
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        return Result(
            argv=tuple(argv),
            returncode=0,
            stdout="/dev/vda 64G disk Sample Disk\n",
            stderr="",
            seconds=0.0,
        )


class StableDiskProbe(Probe):
    def _stable_name(self, path: str) -> str:
        return "/dev/disk/by-id/virtio-sample"


class PartitionListing(Runner):
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        listing = """{
          "blockdevices": [{
            "name": "/dev/vda", "size": 68719476736,
            "fstype": null, "type": "disk",
            "children": [{
              "name": "/dev/vda1", "partn": 1, "size": 17179869185,
              "fstype": "ext4", "type": "part"
            }]
          }]
        }"""
        return Result(
            argv=tuple(argv),
            returncode=0,
            stdout=listing,
            stderr="",
            seconds=0.0,
        )

class StorageListing(Runner):
    def run(self, argv: Sequence[str], **rest: object) -> Result:
        if argv[0] == "findmnt":
            output = json.dumps({"filesystems": [
                {"target": "/", "source": "/dev/mapper/vg-root", "fstype": "ext4", "avail": 12345},
                {"target": "/boot", "source": "/dev/sda2", "fstype": "ext4"},
                {"target": "/boot/efi", "source": "/dev/sda1", "fstype": "vfat"},
            ]})
        elif argv[0] == "lsblk":
            output = json.dumps({"blockdevices": [
                {"path": "/dev/mapper/vg-root", "type": "lvm", "pkname": "/dev/md0"},
                {"path": "/dev/md0", "type": "md", "pkname": "/dev/cryptroot"},
                {"path": "/dev/cryptroot", "type": "crypt", "pkname": "/dev/sda3"},
                {"path": "/dev/sda3", "type": "part", "fstype": "LVM2_member", "pkname": "/dev/sda"},
                {"path": "/dev/sda1", "type": "part", "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"},
            ]})
        else:
            output = "root-uuid\n"
        return Result(argv=tuple(argv), returncode=0, stdout=output, stderr="", seconds=0.0)


class FailedStorageListing(Runner):
    def run(self, argv: Sequence[str], **rest: object) -> Result:
        return Result(argv=tuple(argv), returncode=1, stdout="not available\n", stderr="", seconds=0.0)


def test_storage_layout_reads_each_storage_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    efi = tmp_path / "efi"
    efi.mkdir()
    monkeypatch.setattr(probe, "EFI_MARKER", efi)

    layout = Probe(runner=StorageListing(log=lambda line: None), work=tmp_path).storage_layout()

    assert layout.root_device == "/dev/mapper/vg-root"
    assert layout.root_filesystem_type == "ext4"
    assert layout.root_uuid == "root-uuid"
    assert layout.root_on_lvm is True
    assert layout.root_on_luks is True
    assert layout.root_on_mdraid is True
    assert layout.root_below_device == "/dev/md0"
    assert layout.boot_device == "/dev/sda2"
    # The conversion writes an fstab from these facts, and a `/boot` on its own
    # partition needs a type as well as a device to be mounted at all.
    assert layout.boot_filesystem_type == "ext4"
    assert layout.boot_same_filesystem is False
    assert layout.esp_device == "/dev/sda1"
    assert layout.esp_mountpoint == "/boot/efi"
    assert layout.uefi is True
    assert layout.root_free_bytes == 12345


def test_storage_layout_leaves_facts_absent_when_commands_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "EFI_MARKER", tmp_path / "absent")

    layout = Probe(runner=FailedStorageListing(log=lambda line: None), work=tmp_path).storage_layout()

    assert layout.root_device is None
    assert layout.root_filesystem_type is None
    assert layout.root_uuid is None
    assert layout.root_on_lvm is None
    assert layout.root_on_luks is None
    assert layout.root_on_mdraid is None
    assert layout.root_below_device is None
    assert layout.boot_device is None
    assert layout.boot_same_filesystem is None
    assert layout.esp_device is None
    assert layout.esp_mountpoint is None
    assert layout.uefi is False
    assert layout.root_free_bytes is None


def test_a_disk_probe_keeps_the_kernel_path_that_the_display_tuple_lost(
    tmp_path: Path,
) -> None:
    probe = StableDiskProbe(runner=DiskListing(log=lambda line: None), work=tmp_path)

    disk = probe.probed_disks()[0]

    assert disk.kernel_path == "/dev/vda"
    assert disk.selector == "/dev/disk/by-id/virtio-sample"
    with pytest.raises(FrozenInstanceError):
        setattr(disk, "selector", "/dev/vda")
    assert probe.disks() == (("/dev/disk/by-id/virtio-sample", "64G Sample Disk"),)


def test_a_partition_probe_keeps_the_exact_size_that_the_display_tuple_lost(
    tmp_path: Path,
) -> None:
    probe = Probe(runner=PartitionListing(log=lambda line: None), work=tmp_path)

    partition = probe.probed_partitions("/dev/vda")[0]

    assert partition.kernel_path == "/dev/vda1"
    assert partition.partition_number == 1
    assert partition.size_bytes == 17179869185
    assert partition.filesystem == "ext4"
    assert partition.device_type == "part"
    with pytest.raises(FrozenInstanceError):
        setattr(partition, "size_bytes", 0)
    assert probe.partitions("/dev/vda") == (("/dev/vda1", "16G", "ext4"),)


def test_the_live_medium_is_read_from_the_kernel_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The official minimal ISO boots `root=live:CDLABEL=Gentoo-amd64-20260811
    rd.live.dir=/ rd.live.squashimg=image.squashfs`, which is what says the
    machine is a medium and not somebody's computer."""
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(
        "BOOT_IMAGE=/boot/gentoo dokeymap nodhcp root=live:CDLABEL=Gentoo-amd64-20260811 "
        "rd.live.dir=/ rd.live.squashimg=image.squashfs cdroot\n"
    )
    monkeypatch.setattr(probe, "CMDLINE", cmdline)

    said = probe.Probe(runner=Runner(log=lambda line: None), work=tmp_path).live_medium()

    assert "root=live:" in said


def test_an_overlay_root_is_a_live_medium_without_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "CMDLINE", tmp_path / "absent")

    class Overlaid(Runner):
        def run(self, argv: Sequence[str], **rest: object) -> Result:
            return Result(argv=tuple(argv), returncode=0, stdout="overlay\n", stderr="", seconds=0.0)

    said = probe.Probe(runner=Overlaid(log=lambda line: None), work=tmp_path).live_medium()

    assert "overlay" in said


def test_an_installed_machine_is_not_called_a_live_medium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workstation whose root is `zfs rpool/ROOT/gentoo` must not read as a
    medium, or the warning that names the difference never appears."""
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("BOOT_IMAGE=/vmlinuz root=ZFS=rpool/ROOT/gentoo ro quiet\n")
    monkeypatch.setattr(probe, "CMDLINE", cmdline)

    class Installed(Runner):
        def run(self, argv: Sequence[str], **rest: object) -> Result:
            return Result(argv=tuple(argv), returncode=0, stdout="zfs\n", stderr="", seconds=0.0)

    assert probe.Probe(runner=Installed(log=lambda line: None), work=tmp_path).live_medium() == ""


def test_the_esp_is_the_one_something_is_mounted_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This workstation carries two vfat partitions and the unmounted one comes
    first in `lsblk`. Returning at the first match named a partition nothing
    boots from and left the mount point empty beside it."""
    from gentoo_install.exec.probe import _esp_from_blocks

    esp_type = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    blocks = (
        {"path": "/dev/nvme1n1p1", "parttype": esp_type},
        {"path": "/dev/nvme0n1p1", "parttype": esp_type},
    )
    mounts = ({"source": "/dev/nvme0n1p1", "target": "/boot/efi", "fstype": "vfat"},)

    assert _esp_from_blocks(blocks, mounts) == ("/dev/nvme0n1p1", "/boot/efi")


def test_an_esp_nothing_is_mounted_from_is_still_named(tmp_path: Path) -> None:
    """A machine that has not mounted its esp still has one, and the install
    has to know which partition it is."""
    from gentoo_install.exec.probe import _esp_from_blocks

    esp_type = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    blocks = ({"path": "/dev/sda1", "parttype": esp_type},)

    assert _esp_from_blocks(blocks, ()) == ("/dev/sda1", None)


class SubvolumeListing(Runner):
    """Verbatim shapes from `findmnt` on btrfs, read on 2026-08-17: a
    subvolume mount answers `/dev/vda3[/probe-test]` in its source and
    `subvol=/probe-test` in its options, while the top level answers a plain
    device and `subvol=/`."""

    def run(self, argv: Sequence[str], **rest: object) -> Result:
        if argv[0] == "findmnt":
            output = json.dumps({"filesystems": [
                {
                    "target": "/",
                    "source": "/dev/vda3[/@]",
                    "fstype": "btrfs",
                    "avail": 38334017536,
                    "options": "rw,relatime,compress=zstd:1,subvolid=256,subvol=/@",
                },
            ]})
        elif argv[0] == "lsblk":
            output = json.dumps({"blockdevices": [
                {"path": "/dev/vda3", "type": "part", "fstype": "btrfs",
                 "uuid": "root-uuid", "pkname": "/dev/vda"},
            ]})
        else:
            output = "root-uuid\n"
        return Result(argv=tuple(argv), returncode=0, stdout=output, stderr="", seconds=0.0)


def test_a_subvolume_root_names_its_device_and_its_subvolume(tmp_path: Path) -> None:
    """`/dev/vda3[/@]` is not a block device. Left whole it matches nothing in
    `lsblk`, so the uuid and the disk below both come back empty and the
    conversion builds a graph around a selector no device carries."""
    found = probe.Probe(runner=SubvolumeListing(log=lambda line: None), work=tmp_path)
    layout = found.storage_layout()

    assert layout.root_device == "/dev/vda3"
    # Kept as `findmnt` wrote it, leading slash and all.
    assert layout.root_subvolume == "/@"
    # The point of splitting it: everything keyed by the device now resolves.
    assert layout.root_uuid == "root-uuid"
    assert layout.root_below_device == "/dev/vda"


def test_a_top_level_btrfs_root_is_not_called_a_subvolume(tmp_path: Path) -> None:
    """Negative control. Arch's cloud image answers `subvol=/` with a plain
    source, and refusing that would refuse every ordinary btrfs machine."""

    class TopLevel(SubvolumeListing):
        def run(self, argv: Sequence[str], **rest: object) -> Result:
            answer = super().run(argv, **rest)
            if argv[0] != "findmnt":
                return answer
            return Result(
                argv=answer.argv,
                returncode=0,
                stdout=answer.stdout.replace("/dev/vda3[/@]", "/dev/vda3").replace(
                    "subvol=/@", "subvol=/"
                ),
                stderr="",
                seconds=0.0,
            )

    layout = probe.Probe(runner=TopLevel(log=lambda line: None), work=tmp_path).storage_layout()
    assert layout.root_device == "/dev/vda3"
    assert layout.root_subvolume is None


def test_systemd_boot_is_asked_about_before_the_efi_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine with systemd-boot on its esp also has writable efivars, so
    reading the variables first answers `uefi-grub` for a machine GRUB does not
    manage and arms the wrong one-shot: `bootctl set-oneshot` is what that
    machine understands, and `efibootmgr --bootnext` names an entry GRUB never
    wrote.
    """
    monkeypatch.setattr(probe, "_efi_variables", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    asked: list[tuple[str, ...]] = []

    class Bootctl(Runner):
        def run(self, argv: Sequence[str], **rest: object) -> Result:
            asked.append(tuple(argv))
            return Result(
                argv=tuple(argv),
                returncode=0,
                stdout="System:\n     Firmware: UEFI 2.70\n Boot Loader: systemd-boot 257\n",
                stderr="",
                seconds=0.0,
            )

    method = probe.Probe(runner=Bootctl(log=lambda line: None), work=tmp_path).boot_method()

    assert method is probe.BootMethod.SYSTEMD_BOOT
    assert asked and asked[0][0] == "bootctl", asked


def test_a_uefi_machine_without_systemd_boot_uses_efibootmgr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_efi_variables", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    class NoBootctl(Runner):
        def run(self, argv: Sequence[str], **rest: object) -> Result:
            return Result(argv=tuple(argv), returncode=1, stdout="", stderr="", seconds=0.0)

    method = probe.Probe(runner=NoBootctl(log=lambda line: None), work=tmp_path).boot_method()

    assert method is probe.BootMethod.UEFI_GRUB


def test_a_bios_machine_is_found_by_its_grub_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both spellings: Fedora and openSUSE use `grub2`, Debian and Arch use
    `grub`, and a machine with neither has no way to be armed."""
    # The shipped value first, because the patch below replaces it and a check
    # that only reads the patched one cannot notice a spelling going missing.
    assert {one.name for one in probe.GRUB_DIRECTORIES} == {"grub", "grub2"}, (
        probe.GRUB_DIRECTORIES
    )

    monkeypatch.setattr(probe, "_efi_variables", lambda: False)
    absent = tmp_path / "absent"
    monkeypatch.setattr(probe, "GRUB_DIRECTORIES", (absent, tmp_path / "grub2"))

    quiet = Runner(log=lambda line: None)
    assert probe.Probe(runner=quiet, work=tmp_path).boot_method() is probe.BootMethod.NONE

    (tmp_path / "grub2").mkdir()
    assert probe.Probe(runner=quiet, work=tmp_path).boot_method() is probe.BootMethod.BIOS_GRUB


#: Every `CPU_FLAGS_X86` value Gentoo defines, read from
#: `profiles/desc/cpu_flags_x86.desc` on 2026-08-18. Held here rather than
#: read from `/var/db/repos` so the check runs where no repository is mounted,
#: and so a value that quietly leaves the table is a failing test rather than
#: a silently narrower one.
PORTAGE_CPU_FLAGS: frozenset[str] = frozenset(
    """
    3dnow 3dnowext aes amx_bf16 amx_int8 amx_tile avx avx2 avx512_4fmaps
    avx512_4vnniw avx512_bf16 avx512_bitalg avx512_fp16 avx512_vbmi2
    avx512_vnni avx512_vp2intersect avx512_vpopcntdq avx512bw avx512cd
    avx512dq avx512er avx512f avx512ifma avx512pf avx512vbmi avx512vl
    avx_vnni bmi1 bmi2 f16c fma3 fma4 mmx mmxext padlock pclmul popcnt rdrand
    sha sse sse2 sse3 sse4_1 sse4_2 sse4a ssse3 vpclmulqdq xop
    """.split()
)

#: The six the description file records as spelt differently in cpuinfo, as
#: `<flag> - ... ([<cpuinfo>] in cpuinfo)`. `sha` is the seventh rename and is
#: not annotated there; it is `sha_ni` in the kernel's own header.
DOCUMENTED_RENAMES: dict[str, str] = {
    "fma": "fma3",
    "phe": "padlock",
    "pclmulqdq": "pclmul",
    "pni": "sse3",
    "popcnt": "popcnt",
    "sha_ni": "sha",
}


def test_no_cpu_flag_is_a_name_portage_does_not_define() -> None:
    """A value outside `CPU_FLAGS_X86` reaches `make.conf` and is a build
    failure rather than an optimisation. This table held `vaes`, which is a
    cpuinfo flag with no portage counterpart at all."""
    from gentoo_install.exec.probe import CPU_FLAGS, IMPLIED_CPU_FLAGS

    produced = set(CPU_FLAGS.values()) | set(IMPLIED_CPU_FLAGS.values())
    assert produced <= PORTAGE_CPU_FLAGS, sorted(produced - PORTAGE_CPU_FLAGS)


def test_every_flag_portage_defines_can_be_produced() -> None:
    """A flag with no cpuinfo name is one no machine ever gets: `avx512_vnni`
    was written `avx512vnni`, so a CPU that has it was never given it."""
    from gentoo_install.exec.probe import CPU_FLAGS, IMPLIED_CPU_FLAGS

    produced = set(CPU_FLAGS.values()) | set(IMPLIED_CPU_FLAGS.values())
    assert PORTAGE_CPU_FLAGS <= produced, sorted(PORTAGE_CPU_FLAGS - produced)


def test_each_documented_rename_is_the_one_the_table_uses() -> None:
    """The description file records the cpuinfo name in brackets. Writing the
    requirement the other way round is what refused the v3 binary host on
    every machine that qualifies for it."""
    from gentoo_install.exec.probe import CPU_FLAGS

    for kernel, portage in DOCUMENTED_RENAMES.items():
        assert CPU_FLAGS.get(kernel) == portage, (kernel, CPU_FLAGS.get(kernel))


class WithoutTheTool(Runner):
    """A machine with no `cpuid2cpuflags`, which is an in-place conversion of
    somebody else's distribution rather than a run from a medium."""

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        return Result(
            argv=tuple(argv), returncode=127, stdout="", stderr="", seconds=0.0
        )


class WithTheTool(Runner):
    """`cpuid2cpuflags` as this workstation printed it on 2026-08-18."""

    ANSWER = (
        "CPU_FLAGS_X86: aes avx avx2 avx512_bf16 avx512_vnni avx512f mmx "
        "mmxext pclmul popcnt sha sse sse2 sse3 sse4_1 sse4_2 ssse3\n"
    )

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        return Result(
            argv=tuple(argv), returncode=0, stdout=self.ANSWER, stderr="", seconds=0.0
        )


def test_the_tool_the_media_ship_is_asked_first() -> None:
    """`app-portage/cpuid2cpuflags` is line 56 of `releng`'s
    `installcd-stage1.spec` and is in the Gig-OS Live ISO's `world`, so both
    media have it. It is versioned with the tree, so a flag added to
    `CPU_FLAGS_X86` after this was written still reaches `make.conf`."""
    flags = probe.Probe(runner=WithTheTool(log=lambda line: None), work=Path("/")).cpu_flags()

    assert flags == tuple(sorted(WithTheTool.ANSWER.partition(":")[2].split())), flags


def test_sse_alone_still_gives_mmxext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback, for a conversion where the tool is absent. `mmxext` is
    `[sse] in cpuinfo`: AMD prints it and Intel does not, supporting it
    through SSE, and `cpuid2cpuflags` emits it for both."""
    written = tmp_path / "cpuinfo"
    written.write_text("flags\t: fpu mmx sse sse2 pni\n", encoding="utf-8")
    monkeypatch.setattr(probe, "CPUINFO", written)
    reading = probe.Probe(runner=WithoutTheTool(log=lambda line: None), work=tmp_path)
    flags = reading.cpu_flags()

    assert "mmxext" in flags, flags
    assert "sse3" in flags, flags
    assert "fpu" not in flags, flags


#: One machine's `/proc/cpuinfo` flags and what `cpuid2cpuflags` answered for
#: it, both read on 2026-08-18 from the workstation this is developed on. A
#: pair, because the fallback is only correct if it agrees with the tool on
#: the same CPU, and that agreement is what no assertion about the table
#: alone can show.
SAMPLE_CPUINFO_FLAGS: str = (
    "3dnowprefetch abm adx aes amd_lbr_pmc_freeze amd_lbr_v2 aperfmperf apic arat "
    "avic avx avx2 avx512_bf16 avx512_bitalg avx512_vbmi2 avx512_vnni "
    "avx512_vp2intersect avx512_vpopcntdq avx512bw avx512cd avx512dq avx512f "
    "avx512ifma avx512vbmi avx512vl avx_vnni bmi1 bmi2 bpext bus_lock_detect cat_l3 "
    "cdp_l3 clflush clflushopt clwb clzero cmov cmp_legacy constant_tsc cpb cppc "
    "cpuid cpuid_fault cqm cqm_llc cqm_mbm_local cqm_mbm_total cqm_occup_llc "
    "cr8_legacy cx16 cx8 de decodeassists erms extapic extd_apicid f16c flush_l1d "
    "flushbyasid fma fpu fsgsbase fsrm fxsr fxsr_opt gfni ht hw_pstate ibpb ibrs "
    "ibrs_enhanced ibs invpcid irperf lahf_lm lbrv lm mba mca mce misalignsse mmx "
    "mmxext monitor movbe movdir64b movdiri msr mtrr mwaitx nonstop_tsc nopl npt "
    "nrip_save nx ospke osvw overflow_recov pae pat pausefilter pclmulqdq pdpe1gb "
    "perfctr_core perfctr_llc perfctr_nb perfmon_v2 pfthreshold pge pku pni popcnt "
    "pse pse36 rapl rdpid rdpru rdrand rdseed rdt_a rdtscp rep_good sep sha_ni "
    "skinit smap smca smep ssbd sse sse2 sse4_1 sse4_2 sse4a ssse3 stibp succor svm "
    "svm_lock syscall tce topoext tsc tsc_adjust tsc_scale umip user_shstk "
    "v_spec_ctrl v_vmsave_vmload vaes vgif vmcb_clean vme vmmcall vnmi vpclmulqdq "
    "wbnoinvd wdt x2avic xgetbv1 xsave xsavec xsaveerptr xsaveopt xsaves xtopology "
)

SAMPLE_CPUID2CPUFLAGS: str = (
    "CPU_FLAGS_X86: aes avx avx2 avx512_bf16 avx512_bitalg avx512_vbmi2 avx512_vnni avx512_vp2intersect avx512_vpopcntdq avx512bw avx512cd avx512dq avx512f avx512ifma avx512vbmi avx512vl avx_vnni bmi1 bmi2 f16c fma3 mmx mmxext pclmul popcnt rdrand sha sse sse2 sse3 sse4_1 sse4_2 sse4a ssse3 vpclmulqdq"
)


def test_the_fallback_agrees_with_the_tool_on_the_same_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine with no `cpuid2cpuflags` is an in-place conversion of another
    distribution, and it gets `CPU_FLAGS_X86` from the table instead. The
    table is only right if it answers what the tool would have: this pair was
    35 flags on both sides with nothing on either difference."""
    written = tmp_path / "cpuinfo"
    written.write_text(f"flags\t: {SAMPLE_CPUINFO_FLAGS}\n", encoding="utf-8")
    monkeypatch.setattr(probe, "CPUINFO", written)
    reading = probe.Probe(runner=WithoutTheTool(log=lambda line: None), work=tmp_path)

    expected = tuple(sorted(SAMPLE_CPUID2CPUFLAGS.partition(":")[2].split()))
    assert reading.cpu_flags() == expected, set(expected) ^ set(reading.cpu_flags())
