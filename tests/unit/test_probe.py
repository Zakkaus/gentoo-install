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
