# SPDX-License-Identifier: GPL-2.0-or-later
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Sequence

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
