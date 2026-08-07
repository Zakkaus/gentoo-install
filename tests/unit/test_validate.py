from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.errors import ValidationFailed
from gentoo_install.model.device import Mountpoint, Node
from gentoo_install.model.parse import load
from gentoo_install.model.validate import validate

from .layouts import config, ext4_on_gpt, i, zfs_root

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_a_plain_uefi_install_validates() -> None:
    validate(config())


def test_the_shipped_fixture_validates() -> None:
    validate(load(FIXTURES / "btrfs-luks.toml"))


def test_a_root_that_no_device_defines_is_named() -> None:
    broken = replace(config(), disk=replace(config().disk, root=i("absent")))
    with pytest.raises(ValidationFailed, match="'absent', which no device defines"):
        validate(broken)


def test_a_root_that_is_not_a_mountpoint_is_named() -> None:
    broken = replace(config(), disk=replace(config().disk, root=i("rootfs")))
    with pytest.raises(ValidationFailed, match="not a mountpoint"):
        validate(broken)


def test_a_root_mounted_somewhere_else_is_named() -> None:
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id != i("mnt-root")]
    nodes.append(Mountpoint(id=i("mnt-root"), source=i("rootfs"), path=PurePosixPath("/srv")))
    with pytest.raises(ValidationFailed, match="mounted at /srv"):
        validate(config(nodes))


def test_two_devices_on_one_path_are_named() -> None:
    nodes = ext4_on_gpt()
    nodes.append(Mountpoint(id=i("mnt-esp-again"), source=i("espfs"), path=PurePosixPath("/efi")))
    with pytest.raises(ValidationFailed, match="2 devices are mounted at /efi"):
        validate(config(nodes))


def test_a_layout_problem_and_a_broken_rule_are_reported_in_one_message() -> None:
    nodes = zfs_root()
    nodes.append(Mountpoint(id=i("mnt-esp-again"), source=i("espfs"), path=PurePosixPath("/efi")))
    with pytest.raises(ValidationFailed) as caught:
        validate(config(nodes))
    message = str(caught.value)
    assert "2 devices are mounted at /efi" in message
    assert "root on ZFS excludes GRUB" in message
