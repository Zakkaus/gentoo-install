# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import os
from pathlib import Path

import pytest

from gentoo_install.errors import ConversionFailed
from gentoo_install.exec import convert


def _directory(path: Path, content: str) -> None:
    path.mkdir()
    (path / "content").write_text(content)


def test_swapping_several_directories_removes_backups(tmp_path: Path) -> None:
    root = tmp_path / "root"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    names = ("bin", "etc", "usr")
    for name in names:
        _directory(root / name, f"old {name}")
        _directory(staging / name, f"new {name}")

    convert.convert(staging, names, root=root)

    for name in names:
        assert (root / name / "content").read_text() == f"new {name}"
        assert not (root / f"{name}.gentoo-install.old").exists()


def test_different_filesystem_is_refused_before_any_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    _directory(root / "etc", "old")
    _directory(staging / "etc", "new")
    real_stat = os.stat
    root_device = real_stat(root).st_dev
    staging_device = root_device + 1

    def fake_stat(path: os.PathLike[str] | str) -> os.stat_result:
        result = real_stat(path)
        if Path(path) == root:
            values = list(result)
            values[2] = staging_device
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", fake_stat)

    with pytest.raises(ConversionFailed, match="not on the root filesystem"):
        convert.convert(staging, ("etc",), root=root)
    assert (root / "etc" / "content").read_text() == "old"
    assert (staging / "etc" / "content").read_text() == "new"


def test_rename_failure_restores_previous_swaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    names = ("one", "two", "three")
    for name in names:
        _directory(root / name, f"old {name}")
        _directory(staging / name, f"new {name}")
    real_rename = os.rename
    calls = 0

    def failing_rename(source: os.PathLike[str] | str, destination: os.PathLike[str] | str) -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise OSError("injected rename failure")
        real_rename(source, destination)

    monkeypatch.setattr(os, "rename", failing_rename)

    # Wrapped, not re-raised: `errors.py` owns what leaves this package, and
    # the message still carries what the kernel said.
    with pytest.raises(ConversionFailed, match="injected rename failure"):
        convert.convert(staging, names, root=root)

    for name in names:
        assert (root / name / "content").read_text() == f"old {name}"
        assert not (root / f"{name}.gentoo-install.old").exists()
        if name != "three":
            assert (staging / name / "content").read_text() == f"new {name}"


def test_existing_backup_is_refused_before_any_rename(tmp_path: Path) -> None:
    root = tmp_path / "root"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    _directory(root / "etc", "old")
    _directory(staging / "etc", "new")
    _directory(root / "etc.gentoo-install.old", "previous")

    with pytest.raises(ConversionFailed, match="left from an earlier attempt"):
        convert.convert(staging, ("etc",), root=root)

    assert (root / "etc" / "content").read_text() == "old"
    assert (root / "etc.gentoo-install.old" / "content").read_text() == "previous"
    assert (staging / "etc" / "content").read_text() == "new"


def test_a_directory_the_machine_lacks_is_moved_into_place(tmp_path: Path) -> None:
    """A merged-usr Debian has no `/lib64` at all, and renaming what is not
    there failed half way through with the rest already swapped."""
    root = tmp_path / "root"
    staging = tmp_path / "root" / "new"
    (root / "usr").mkdir(parents=True)
    (root / "usr" / "old.txt").write_text("old")
    (staging / "usr").mkdir(parents=True)
    (staging / "usr" / "new.txt").write_text("new")
    (staging / "lib64").mkdir()
    (staging / "lib64" / "new.txt").write_text("new")

    convert.convert(staging, ("usr", "lib64"), root=root)

    assert (root / "usr" / "new.txt").read_text() == "new"
    assert (root / "lib64" / "new.txt").read_text() == "new"
    assert not (root / "lib64.gentoo-install.old").exists()
    assert not (root / "usr.gentoo-install.old").exists()


def test_a_rollback_takes_back_a_directory_the_machine_lacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rolling back a name that had no backup means putting it back in the
    staging root, not restoring a `.old` that was never made."""
    root = tmp_path / "root"
    staging = tmp_path / "root" / "new"
    (root / "usr").mkdir(parents=True)
    (staging / "lib64").mkdir(parents=True)
    (staging / "usr").mkdir()

    real = os.rename

    def failing(source: str, target: str) -> None:
        if str(target).endswith("usr.gentoo-install.old"):
            raise OSError(5, "input/output error")
        real(source, target)

    monkeypatch.setattr("os.rename", failing)
    with pytest.raises(ConversionFailed, match="usr could not be swapped"):
        convert.convert(staging, ("lib64", "usr"), root=root)

    assert (staging / "lib64").is_dir(), "the staged one has to go back"
    assert not (root / "lib64").exists()
    assert (root / "usr").is_dir()


def test_the_staged_kernel_is_put_into_the_machines_boot(tmp_path: Path) -> None:
    """`/boot` is not in the swap, so without this the machine keeps the old
    distribution's kernels and the one just built stays in the staging root."""
    root = tmp_path / "root"
    staging = root / "new"
    (root / "boot").mkdir(parents=True)
    (root / "boot" / "vmlinuz-6.1.0-debian").write_text("old")
    (root / "boot" / "initrd.img-6.1.0-debian").write_text("old")
    (root / "boot" / "grub").mkdir()
    (root / "boot" / "grub" / "grub.cfg").write_text("old menu")
    (staging / "boot").mkdir(parents=True)
    (staging / "boot" / "vmlinuz-6.18.43-gentoo").write_text("new")

    convert.populate_boot(staging, root=root)

    assert (root / "boot" / "vmlinuz-6.18.43-gentoo").read_text() == "new"
    assert not (root / "boot" / "vmlinuz-6.1.0-debian").exists()
    assert not (root / "boot" / "initrd.img-6.1.0-debian").exists()
    # Not a kernel and a directory: `grub` and an esp mounted below `/boot`
    # both have to survive.
    assert (root / "boot" / "grub" / "grub.cfg").read_text() == "old menu"


def test_a_boot_directory_the_machine_lacks_is_created(tmp_path: Path) -> None:
    root = tmp_path / "root"
    staging = root / "new"
    (staging / "boot").mkdir(parents=True)
    (staging / "boot" / "vmlinuz-6.18.43-gentoo").write_text("new")
    root.mkdir(exist_ok=True)

    convert.populate_boot(staging, root=root)

    assert (root / "boot" / "vmlinuz-6.18.43-gentoo").read_text() == "new"


def test_a_staging_root_without_a_kernel_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "new").mkdir(parents=True)
    with pytest.raises(ConversionFailed, match="no "):
        convert.populate_boot(root / "new", root=root)


def test_a_file_that_is_not_a_kernel_image_is_left_alone(tmp_path: Path) -> None:
    """The rule names kernels, not everything: a machine's own `memtest86+` or
    a signed shim under `/boot` is not this installer's to delete."""
    root = tmp_path / "root"
    staging = root / "new"
    (root / "boot").mkdir(parents=True)
    (root / "boot" / "memtest86+.bin").write_text("keep")
    (staging / "boot").mkdir(parents=True)
    (staging / "boot" / "vmlinuz-6.18.43-gentoo").write_text("new")

    convert.populate_boot(staging, root=root)

    assert (root / "boot" / "memtest86+.bin").read_text() == "keep"
