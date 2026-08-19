# SPDX-License-Identifier: GPL-2.0-or-later
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Final, Sequence

import json
import pytest

from gentoo_install.errors import DeviceNotFound
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


#: The shape `bootctl status` prints, from `src/boot/bootctl-status.c` in
#: systemd v256: `Current Boot Loader:` is the section for the loader that
#: started this machine, and `Product:` is its first line.
BOOTED_BY_SYSTEMD_BOOT_OUTPUT: Final[str] = (
    "System:\n"
    "      Firmware: UEFI 2.70 (EDK II)\n"
    "   Secure Boot: disabled\n"
    "\n"
    "Current Boot Loader:\n"
    "      Product: systemd-boot 257\n"
    "     Features: \u2713 Boot counting\n"
    "          ESP: /dev/disk/by-partuuid/11111111-2222-3333-4444-555555555555\n"
)

#: What the same command prints on a machine systemd-boot did not start, read
#: from this workstation, which boots through ZFSBootMenu: no `Current Boot
#: Loader` section at all, a `Current Stub` one, and the loaders it can see
#: listed under headings of their own.
BOOTED_BY_SOMETHING_ELSE: Final[str] = (
    "System:\n"
    "      Firmware: UEFI 2.90 (American Megatrends 5.26)\n"
    "   Secure Boot: disabled\n"
    "\n"
    "Current Stub:\n"
    "      Product: systemd-stub 256.6\n"
    "\n"
    "Available Boot Loaders on ESP:\n"
    "          ESP: /boot/efi\n"
    "         File: \u2514\u2500/EFI/systemd/systemd-bootx64.efi (systemd-boot 256.6)\n"
    "\n"
    "Boot Loaders Listed in EFI Variables:\n"
    "        Title: ZFSBootMenu (A)\n"
)

#: And what it prints when there is no systemd-boot at all. `log_info` writes
#: it, the runner merges stderr into stdout, and the words are the ones the
#: old test looked for.
NO_SYSTEMD_BOOT_AT_ALL: Final[str] = (
    "System:\n"
    "      Firmware: UEFI 2.70 (EDK II)\n"
    "\n"
    "Available Boot Loaders on ESP:\n"
    "          ESP: /boot/efi\n"
    "systemd-boot not installed in ESP.\n"
    "No default/fallback boot loader installed in ESP.\n"
)


def test_every_proc_path_is_built_from_one_root() -> None:
    """`MEMINFO`, `CMDLINE` and `CPUINFO` each wrote `/proc` out again beside
    the constant that declares it, so a probe pointed at another root would
    have moved some of them and not the others.
    """
    import ast
    import inspect

    assert probe.MEMINFO.parent == probe.PROC
    assert probe.CMDLINE.parent == probe.PROC
    assert probe.CPUINFO.parent == probe.PROC

    tree = ast.parse(inspect.getsource(probe))
    spelled = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/proc")
    ]
    assert len(spelled) == 1, [node.lineno for node in spelled]


def test_only_the_loader_that_booted_the_machine_answers_for_it() -> None:
    """`systemd-boot` anywhere in `bootctl status` was the test, and the
    command prints those words on machines it did not boot: in the esp file
    list, and in `systemd-boot not installed in ESP.` — which the runner
    merges into stdout. Arming such a machine with `bootctl set-oneshot`
    names an entry the loader it actually boots never wrote.
    """
    assert probe.BOOTED_BY_SYSTEMD_BOOT.search(BOOTED_BY_SYSTEMD_BOOT_OUTPUT)
    assert not probe.BOOTED_BY_SYSTEMD_BOOT.search(BOOTED_BY_SOMETHING_ELSE)
    assert not probe.BOOTED_BY_SYSTEMD_BOOT.search(NO_SYSTEMD_BOOT_AT_ALL)
    # The words really are in both, which is why the old test passed on them.
    assert "systemd-boot" in BOOTED_BY_SOMETHING_ELSE
    assert "systemd-boot" in NO_SYSTEMD_BOOT_AT_ALL


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
                stdout=BOOTED_BY_SYSTEMD_BOOT_OUTPUT,
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


#: One `/proc/cpuinfo` flags line and the `cpuid2cpuflags` answer for the same
#: CPU, taken on 2026-08-18 by booting the Gentoo CJK ISO under QEMU once per
#: model. The ISO carries `app-portage/cpuid2cpuflags`, so both halves come
#: from the same guest and the pair is evidence rather than a transcription.
#:
#: Five models, chosen for what they disagree about: `qemu64` has almost
#: nothing, `Nehalem` is an Intel that reports no `mmxext` and is given one
#: through `sse`, `Opteron_G3` is an AMD that reports `sse4a`, and `EPYC` and
#: `Skylake-Client` carry `fma3` and the `bmi` pair. The workstation this was
#: developed on is a sixth, with the AVX-512 family.
CPU_SAMPLES: dict[str, tuple[str, str]] = {
    "EPYC": (
        (
            "3dnowprefetch abm adx aes apic arat avx avx2 bmi1 bmi2 clflush clflushopt "
            "cmov cpuid cr8_legacy cx16 cx8 de extd_apicid f16c fma fpu fsgsbase fxsr "
            "fxsr_opt hypervisor lahf_lm lm mca mce misalignsse mmx mmxext movbe msr mtrr "
            "nopl nx osvw pae pat pclmulqdq pdpe1gb pge pni popcnt pse pse36 rdrand "
            "rdseed rdtscp rep_good sep sha_ni smap smep sse sse2 sse4_1 sse4_2 sse4a "
            "ssse3 syscall topoext tsc tsc_known_freq vme vmmcall x2apic xgetbv1 xsave "
            "xsavec xsaveopt xtopology"
        ),
        (
            "aes avx avx2 bmi1 bmi2 f16c fma3 mmx mmxext pclmul popcnt rdrand sha sse "
            "sse2 sse3 sse4_1 sse4_2 sse4a ssse3"
        ),
    ),
    "Opteron_G3": (
        (
            "3dnowprefetch abm apic clflush cmov cpuid cx16 cx8 de extd_apicid fpu fxsr "
            "hypervisor lahf_lm lm mca mce misalignsse mmx msr mtrr nopl nx pae pat pge "
            "pni popcnt pse pse36 rdtscp rep_good sep sse sse2 sse4a syscall tsc "
            "tsc_known_freq vme vmmcall x2apic"
        ),
        (
            "mmx mmxext popcnt sse sse2 sse3 sse4a"
        ),
    ),
    "Skylake-Client": (
        (
            "3dnowprefetch abm adx aes apic arat avx avx2 bmi1 bmi2 clflush cmov cpuid "
            "cx16 cx8 de erms f16c fma fpu fsgsbase fxsr hypervisor invpcid lahf_lm lm "
            "mca mce mmx movbe msr mtrr nopl nx pae pat pclmulqdq pge pni popcnt pse "
            "pse36 rdrand rdseed rdtscp sep smap smep sse sse2 sse4_1 sse4_2 ssse3 "
            "syscall tsc tsc_deadline_timer tsc_known_freq vme vmmcall x2apic xgetbv1 "
            "xsave xsavec xsaveopt xtopology"
        ),
        (
            "aes avx avx2 bmi1 bmi2 f16c fma3 mmx mmxext pclmul popcnt rdrand sse sse2 "
            "sse3 sse4_1 sse4_2 ssse3"
        ),
    ),
    "nehalem": (
        (
            "3dnowprefetch apic clflush cmov cpuid cx16 cx8 de fpu fxsr hypervisor "
            "lahf_lm lm mca mce mmx msr mtrr nopl nx pae pat pge pni popcnt pse pse36 sep "
            "sse sse2 sse4_1 sse4_2 ssse3 syscall tsc tsc_known_freq vme vmmcall x2apic "
            "xtopology"
        ),
        (
            "mmx mmxext popcnt sse sse2 sse3 sse4_1 sse4_2 ssse3"
        ),
    ),
    "qemu64": (
        (
            "3dnowprefetch apic clflush cmov cpuid cx16 cx8 de extd_apicid fpu fxsr "
            "hypervisor lahf_lm lm mca mce mmx msr mtrr nopl nx pae pat pge pni pse pse36 "
            "rep_good sep sse sse2 syscall tsc tsc_known_freq vmmcall x2apic xtopology"
        ),
        (
            "mmx mmxext sse sse2 sse3"
        ),
    ),
}


@pytest.mark.parametrize("model", sorted(CPU_SAMPLES))
def test_the_fallback_answers_what_the_tool_answers_on_that_cpu(
    model: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table is only right if it agrees with `cpuid2cpuflags` on the same
    CPU, and one machine cannot show that: `mmxext` is absent from Intel's
    cpuinfo and present in the tool's answer, and `sse4a` appears on AMD only.
    """
    cpuinfo, expected = CPU_SAMPLES[model]
    written = tmp_path / "cpuinfo"
    written.write_text(f"flags\t: {cpuinfo}\n", encoding="utf-8")
    monkeypatch.setattr(probe, "CPUINFO", written)
    reading = probe.Probe(runner=WithoutTheTool(log=lambda line: None), work=tmp_path)

    assert reading.cpu_flags() == tuple(expected.split()), (
        model,
        set(expected.split()) ^ set(reading.cpu_flags()),
    )


def test_the_samples_cover_the_cases_one_machine_cannot_show() -> None:
    """A sample set that agrees about everything proves only that the tests
    ran. These four disagreements are why five machines were booted."""
    answers = {name: set(truth.split()) for name, (_, truth) in CPU_SAMPLES.items()}
    assert any("sse4a" in one for one in answers.values()), "no AMD sample"
    assert any("fma3" in one for one in answers.values()), "nothing with FMA3"
    assert any(len(one) < 6 for one in answers.values()), "nothing minimal"
    # The implied flag, on a CPU whose own cpuinfo does not name it.
    intel = {
        name
        for name, (cpuinfo, truth) in CPU_SAMPLES.items()
        if "mmxext" in truth.split() and "mmxext" not in cpuinfo.split()
    }
    assert intel, "no sample exercises mmxext being implied by sse"


#: What the kernel prints in `/proc/cpuinfo` for each feature this maps, read
#: from `arch/x86/include/asm/cpufeatures.h` of the running 6.18 kernel on
#: 2026-08-18. Held here because thirteen of the flags portage defines are on
#: no CPU any sample was taken from — `3dnow`, `fma4`, `xop`, `padlock`, the
#: `amx_*` set and four AVX-512 members — so for those this file is the only
#: evidence that the name on the left of the table is one a machine will ever
#: report. A key that is not a printed name is a branch that can never fire.
KERNEL_PRINTS: frozenset[str] = frozenset(
    (
    "3dnow 3dnowext aes amx_bf16 amx_int8 amx_tile avx avx2 avx512_4fmaps "
    "avx512_4vnniw avx512_bf16 avx512_bitalg avx512_fp16 avx512_vbmi2 "
    "avx512_vnni avx512_vp2intersect avx512_vpopcntdq avx512bw avx512cd "
    "avx512dq avx512er avx512f avx512ifma avx512pf avx512vbmi avx512vl avx_vnni "
    "bmi1 bmi2 f16c fma fma4 mmx mmxext pclmulqdq phe pni popcnt rdrand sha_ni "
    "sse sse2 sse4_1 sse4_2 sse4a ssse3 vpclmulqdq xop"
    ).split()
)


def test_every_cpuinfo_name_is_one_the_kernel_prints() -> None:
    """`sha` and `sha_ni` are the shape of this mistake: portage's flag is
    `sha` and the kernel prints `sha_ni`, so a table keyed by the portage name
    would match nothing on any machine and say nothing about it."""
    from gentoo_install.exec.probe import CPU_FLAGS, IMPLIED_CPU_FLAGS

    keys = set(CPU_FLAGS) | set(IMPLIED_CPU_FLAGS)
    assert keys <= KERNEL_PRINTS, sorted(keys - KERNEL_PRINTS)


def test_the_uncovered_flags_are_the_ones_this_file_answers_for() -> None:
    """Naming the gap, so it is a boundary rather than an omission: these are
    the flags no sampled CPU has, and the kernel header is what holds their
    spelling."""
    covered: set[str] = set()
    for _, truth in CPU_SAMPLES.values():
        covered |= set(truth.split())
    unmeasured = PORTAGE_CPU_FLAGS - covered
    assert unmeasured, "every flag is measured; this test has nothing left to say"
    from gentoo_install.exec.probe import CPU_FLAGS

    for portage in sorted(unmeasured):
        kernel = next(k for k, v in CPU_FLAGS.items() if v == portage)
        assert kernel in KERNEL_PRINTS, (portage, kernel)


def test_secure_boot_is_read_from_the_variable_rather_than_bootctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine without systemd has no `bootctl`. The variable is the four
    attribute bytes and then the value, so the state is its last byte: read on
    this workstation it is `6 0 0 0 0`, and `bootctl status` on the same
    machine prints `Secure Boot: disabled`."""
    written = tmp_path / "SecureBoot"
    for content, wanted in (
        (bytes([6, 0, 0, 0, 0]), False),
        (bytes([6, 0, 0, 0, 1]), True),
    ):
        written.write_bytes(content)
        monkeypatch.setattr(probe, "SECURE_BOOT", written)
        assert probe.secure_boot() is wanted, content


def test_a_machine_with_no_such_variable_answers_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BIOS machine, or one whose efivarfs is not mounted. Unread is not
    `disabled`: a refusal built on it would be one nobody can act on."""
    monkeypatch.setattr(probe, "SECURE_BOOT", tmp_path / "absent")
    assert probe.secure_boot() is None


def _fake_proc(tmp_path: Path, *names: str) -> Path:
    """A `/proc` holding one process per name, and one entry that is not one."""
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    (proc / "self").mkdir(exist_ok=True)
    for number, name in enumerate(names, start=100):
        entry = proc / str(number)
        entry.mkdir(exist_ok=True)
        (entry / "comm").write_text(f"{name}\n")
    return proc


class Asking(Runner):
    """A runner that records what was asked and answers nothing."""

    def __init__(self) -> None:
        super().__init__(log=lambda line: None)
        self.asked: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], **rest: object) -> Result:
        self.asked.append(tuple(argv))
        return Result(argv=tuple(argv), returncode=0, stdout="", stderr="", seconds=0.0)


def test_a_node_is_scanned_for_where_mdev_is_the_device_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alpine's netboot root runs mdev, so `udevadm settle` returns at once
    with no daemon behind it. The `--lowram` install stopped at `/dev/vdc2 did
    not appear within 15s` on a machine whose `/proc/partitions` held `vdc1`
    and `vdc2` and whose `/dev` held neither."""
    monkeypatch.setattr(probe, "PROC", _fake_proc(tmp_path, "busybox"))
    asking = Asking()
    reader = Probe(runner=asking, work=tmp_path)

    with pytest.raises(DeviceNotFound):
        reader.wait_for(str(tmp_path / "vdc2"), seconds=0.6)

    assert ("mdev", "-s") in asking.asked, asking.asked
    assert ("udevadm", "settle") in asking.asked, asking.asked


def test_a_machine_running_udev_is_not_sent_to_mdev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: `mdev -s` on a udev machine rebuilds `/dev` from
    mdev's own rules while udevd is writing it."""
    monkeypatch.setattr(probe, "PROC", _fake_proc(tmp_path, "udevd"))
    asking = Asking()
    reader = Probe(runner=asking, work=tmp_path)

    with pytest.raises(DeviceNotFound):
        reader.wait_for(str(tmp_path / "vdc2"), seconds=0.6)

    assert ("mdev", "-s") not in asking.asked, asking.asked
    assert ("udevadm", "settle") in asking.asked, asking.asked
