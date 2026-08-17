# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import os
from pathlib import Path

import pytest

from gentoo_install.errors import ConversionFailed
from gentoo_install.exec import convert


def _copy(source: Path, destination: Path) -> None:
    """What the plan hands over as `cp --archive`, in the test's own terms.

    `copytree` here rather than a runner: what these tests hold is the ordering
    and the rollback, and the plan's own test holds that the command really is
    `cp --archive`.
    """
    import shutil

    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


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

    convert.convert(staging, names, copy=_copy, root=root)

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
        convert.convert(staging, ("etc",), copy=_copy, root=root)
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
        convert.convert(staging, names, copy=_copy, root=root)

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
        convert.convert(staging, ("etc",), copy=_copy, root=root)

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

    convert.convert(staging, ("usr", "lib64"), copy=_copy, root=root)

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
        convert.convert(staging, ("lib64", "usr"), copy=_copy, root=root)

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


def test_a_separately_mounted_directory_is_replaced_by_its_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fedora mounts `/var` as its own btrfs subvolume, and `rename(2)` cannot
    replace a mount point. Refusing it kept the whole RPM family out, so the
    entries move instead: every one is a rename on the same filesystem, which
    is what `distro2gentoo`'s `cp -a` is not."""
    root = tmp_path / "root"
    staging = root / "new"
    for name in ("usr", "var"):
        (root / name).mkdir(parents=True)
        (staging / name).mkdir(parents=True)
    (root / "usr" / "kept.txt").write_text("old")
    (root / "var" / "old-log").write_text("from the distribution being replaced")
    (staging / "usr" / "new.txt").write_text("new")
    (staging / "var" / "portage").mkdir()

    real = os.path.ismount
    monkeypatch.setattr(
        "os.path.ismount", lambda path: str(path).endswith("/var") or real(path)
    )
    # The directory itself, not its contents: a rename would put a different
    # directory at that path, which is exactly what a mount point forbids. In a
    # temporary directory nothing is really mounted, so the inode is what tells
    # the two paths apart.
    was = os.stat(root / "var").st_ino
    ordinary = os.stat(root / "usr").st_ino

    convert.convert(staging, ("usr", "var"), copy=_copy, root=root)

    assert os.stat(root / "var").st_ino == was, "the mount point was replaced"
    assert os.stat(root / "usr").st_ino != ordinary, "an ordinary directory is renamed"
    # The mount point itself is untouched, and what is in it is the staged set.
    assert (root / "var" / "portage").is_dir()
    assert not (root / "var" / "old-log").exists(), "replaced, not merged"
    assert not (root / "var" / convert.KEPT_ASIDE).exists(), "the kept set is removed"
    # And the ordinary directory still went by rename.
    assert (root / "usr" / "new.txt").read_text() == "new"
    assert not (root / "usr" / "kept.txt").exists()


def test_a_mount_inside_a_mounted_directory_is_refused_before_any_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control for the above. `rename(2)` answers EBUSY for a mount
    point, so a `/var` with something mounted under it cannot have its entries
    moved, and finding that out halfway through has no clean rollback."""
    root = tmp_path / "root"
    staging = root / "new"
    for name in ("usr", "var"):
        (root / name).mkdir(parents=True)
        (staging / name).mkdir(parents=True)
    (root / "var" / "lib").mkdir()
    (root / "usr" / "kept.txt").write_text("old")

    real = os.path.ismount
    monkeypatch.setattr(
        "os.path.ismount",
        lambda path: str(path).endswith(("/var", "/var/lib")) or real(path),
    )

    with pytest.raises(ConversionFailed, match="holding lib"):
        convert.convert(staging, ("usr", "var"), copy=_copy, root=root)

    assert (root / "usr" / "kept.txt").read_text() == "old", "nothing was moved"


def test_a_mounted_directory_is_filled_by_copy_when_rename_cannot_cross(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read off a Fedora 41 machine, at operation 46 of 48:

        [bootloader] atomically swap /bin, /sbin, /etc, /lib, /lib64, /usr, /var
        the install stopped: ConversionFailed: /var could not be replaced by
        content: [Errno 18] Invalid cross-device link:
        '/gentoo-install.new/var/cache' -> '/var/cache'

    A directory reaches this path precisely because it is a separate mount, and
    Fedora's `/var` is its own btrfs subvolume, so its `st_dev` differs from
    the staging root's and `rename(2)` cannot reach it. Everything before this
    had succeeded: the whole userland was built and the swap was the last step.
    """
    import errno

    root = tmp_path / "root"
    staging = root / "new"
    for name in ("usr", "var"):
        (root / name).mkdir(parents=True)
        (staging / name).mkdir(parents=True)
    (root / "var" / "old-log").mkdir()
    (staging / "var" / "cache").mkdir()
    (staging / "var" / "db").mkdir()
    (staging / "usr" / "new.txt").write_text("new")

    real_mount = os.path.ismount
    monkeypatch.setattr(
        "os.path.ismount", lambda path: str(path).endswith("/var") or real_mount(path)
    )
    real_rename = os.rename
    crossed: list[tuple[str, str]] = []

    def rename(source: Path | str, destination: Path | str) -> None:
        pair = (str(source), str(destination))
        if str(staging / "var") in pair[0]:
            crossed.append(pair)
            raise OSError(errno.EXDEV, "Invalid cross-device link", pair[0], None, pair[1])
        real_rename(pair[0], pair[1])

    copied: list[tuple[str, str]] = []

    def copy(source: Path, destination: Path) -> None:
        copied.append((source.name, destination.name))
        _copy(source, destination)

    monkeypatch.setattr("os.rename", rename)
    convert.convert(staging, ("usr", "var"), copy=copy, root=root)

    assert crossed, "the test did not reproduce a cross-device rename"
    assert copied == [("cache", "cache"), ("db", "db")], copied
    assert (root / "var" / "cache").is_dir() and (root / "var" / "db").is_dir()
    assert not (root / "var" / "old-log").exists(), "replaced, not merged"
    assert not (root / "var" / convert.KEPT_ASIDE).exists(), "the kept set is removed"
    assert (root / "usr" / "new.txt").read_text() == "new", "an ordinary directory renames"
