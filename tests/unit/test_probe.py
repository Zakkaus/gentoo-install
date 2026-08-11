from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Sequence

import pytest

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
              "name": "/dev/vda1", "size": 17179869185,
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
    assert partition.size_bytes == 17179869185
    assert partition.filesystem == "ext4"
    assert partition.device_type == "part"
    with pytest.raises(FrozenInstanceError):
        setattr(partition, "size_bytes", 0)
    assert probe.partitions("/dev/vda") == (("/dev/vda1", "16G", "ext4"),)
