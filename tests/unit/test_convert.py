# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from gentoo_install.errors import CommandFailed, ConversionFailed
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
    with pytest.raises(ConversionFailed, match=r"the staging directory has no .*/new/boot"):
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

    _pretend_mounts(monkeypatch, root / "var")
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


def _pretend_mounts(monkeypatch: pytest.MonkeyPatch, *paths: Path) -> None:
    """Name what the kernel would list as a mount point in a temporary tree."""
    from gentoo_install.exec import convert as exec_convert

    real = exec_convert._mount_points()
    monkeypatch.setattr(
        exec_convert, "_mount_points", lambda: real | {str(one) for one in paths}
    )


def test_a_bind_mount_within_one_filesystem_is_still_a_mount_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.path.ismount` compares `st_dev` against the parent's, so it answers
    False for a bind mount of a sibling directory while `rename(2)` still
    answers EBUSY for it. Measured under `unshare -rm`: the bind mount is
    False to `ismount` and listed in `/proc/self/mountinfo`.

    Sampled from this machine's own table, so the field the mount point comes
    from is the one the kernel writes:

        36 35 98:0 /mnt1 /mnt2 rw,noatime master:1 - ext3 /dev/root rw
    """
    from gentoo_install.exec import convert as exec_convert

    sample = (
        "36 35 98:0 /mnt1 /mnt2 rw,noatime master:1 - ext3 /dev/root rw\n"
        "37 36 0:32 / /var/a\\040b rw,relatime - tmpfs none rw\n"
        "\n"
    )
    written = tmp_path / "mountinfo"
    written.write_text(sample)
    monkeypatch.setattr(exec_convert, "MOUNTINFO", written)
    assert exec_convert._mount_points() == frozenset({"/mnt2", "/var/a b"})


def test_an_unreadable_mount_table_is_refused_rather_than_read_as_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control for the above: no mount point and no answer at all are
    different, and the caller decides an irreversible step from this one."""
    from gentoo_install.exec import convert as exec_convert

    monkeypatch.setattr(exec_convert, "MOUNTINFO", tmp_path / "absent")
    with pytest.raises(ConversionFailed, match="mount table"):
        exec_convert._mount_points()


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

    _pretend_mounts(monkeypatch, root / "var", root / "var" / "lib")

    with pytest.raises(ConversionFailed, match="holding lib"):
        convert.convert(staging, ("usr", "var"), copy=_copy, root=root)

    assert (root / "usr" / "kept.txt").read_text() == "old", "nothing was moved"


def test_a_deep_mount_inside_a_mounted_directory_is_refused_before_any_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renamed parent keeps a nested mount attached, and cleanup can walk it."""
    root = tmp_path / "root"
    staging = root / "new"
    for name in ("usr", "var"):
        (root / name).mkdir(parents=True)
        (staging / name).mkdir(parents=True)
    protected = root / "var" / "lib" / "docker" / "operator-data"
    protected.parent.mkdir(parents=True)
    protected.write_text("precious")
    (root / "usr" / "kept.txt").write_text("old")

    _pretend_mounts(monkeypatch, root / "var", protected.parent)

    with pytest.raises(ConversionFailed, match=r"holding lib/docker"):
        convert.convert(staging, ("usr", "var"), copy=_copy, root=root)

    assert (root / "usr" / "kept.txt").read_text() == "old", "nothing was moved"
    assert protected.read_text() == "precious", "the mounted tree was touched"
    assert not (root / "var.gentoo-install.old").exists()


def test_a_mount_inside_an_ordinary_directory_is_refused_before_any_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination need not be a mount to contain a mount that rename carries."""
    root = tmp_path / "root"
    staging = root / "new"
    for name in ("usr", "var"):
        (root / name).mkdir(parents=True)
        (staging / name).mkdir(parents=True)
    protected = root / "var" / "log" / "operator-data"
    protected.parent.mkdir()
    protected.write_text("precious")
    (root / "usr" / "kept.txt").write_text("old")

    _pretend_mounts(monkeypatch, protected.parent)

    with pytest.raises(ConversionFailed, match=r"holding log"):
        convert.convert(staging, ("usr", "var"), copy=_copy, root=root)

    assert (root / "usr" / "kept.txt").read_text() == "old", "nothing was moved"
    assert protected.read_text() == "precious", "the mounted tree was touched"
    assert not (root / "var.gentoo-install.old").exists()


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
    root = tmp_path / "root"
    staging = root / "new"
    for name in ("usr", "var"):
        (root / name).mkdir(parents=True)
        (staging / name).mkdir(parents=True)
    (root / "var" / "old-log").mkdir()
    (staging / "var" / "cache").mkdir()
    (staging / "var" / "db").mkdir()
    (staging / "usr" / "new.txt").write_text("new")

    _pretend_mounts(monkeypatch, root / "var")
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


def test_a_failed_cross_device_copy_restores_all_replaced_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    staging = root / "new"
    (root / "usr").mkdir(parents=True)
    (staging / "usr").mkdir(parents=True)
    (root / "usr" / "content").write_text("old usr")
    (staging / "usr" / "content").write_text("new usr")
    (root / "var").mkdir()
    (staging / "var").mkdir()
    (root / "var" / "old-log").write_text("old var")
    (staging / "var" / "cache").mkdir()

    _pretend_mounts(monkeypatch, root / "var")
    real_rename = os.rename

    def cross_device_rename(source: Path | str, destination: Path | str) -> None:
        if Path(source) == staging / "var" / "cache":
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(source, destination)

    copy_failure = CommandFailed("cp --archive ended with exit status 1")

    def failing_copy(source: Path, destination: Path) -> None:
        raise copy_failure

    monkeypatch.setattr("os.rename", cross_device_rename)

    with pytest.raises(ConversionFailed, match="cp --archive") as failure:
        convert.convert(staging, ("usr", "var"), copy=failing_copy, root=root)

    assert (root / "usr" / "content").read_text() == "old usr"
    assert (staging / "usr" / "content").read_text() == "new usr"
    assert (root / "var" / "old-log").read_text() == "old var"
    assert (staging / "var" / "cache").is_dir()
    assert not (root / "var" / "cache").exists()
    assert not (root / "var" / convert.KEPT_ASIDE).exists()
    inner_failure = failure.value.__cause__
    assert isinstance(inner_failure, ConversionFailed)
    assert inner_failure.__cause__ is copy_failure


def test_a_merged_usr_symlink_is_removed_rather_than_left_beside_the_new_one(
    tmp_path: Path,
) -> None:
    """`/bin`, `/sbin`, `/lib` and `/lib64` are symlinks into `usr`, and
    `shutil.rmtree` answers `[Errno None] None` on a symlink rather than
    removing it. run136's converted machine kept all four."""
    root = tmp_path / "root"
    staging = root / "new"
    (root / "usr" / "bin").mkdir(parents=True)
    (staging / "usr").mkdir(parents=True)
    (staging / "bin").mkdir()
    os.symlink("usr/bin", root / "bin")

    convert.convert(staging, ("usr", "bin"), copy=_copy, root=root)

    assert (root / "bin").is_dir() and not (root / "bin").is_symlink()
    left = [one.name for one in root.iterdir() if one.name.endswith(convert.KEPT_ASIDE)]
    assert left == [], left


def test_a_copy_that_writes_part_of_a_tree_then_fails_leaves_none_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cp --archive` can create destination content and then return non-zero.
    The entry is recorded before the copy runs, or the rollback never hears
    about it and restores the original beside the half-written copy."""
    root = tmp_path / "root"
    staging = root / "new"
    (root / "var").mkdir(parents=True)
    (staging / "var").mkdir(parents=True)
    (root / "var" / "old-log").write_text("old var")
    (staging / "var" / "cache").mkdir()

    _pretend_mounts(monkeypatch, root / "var")
    real_rename = os.rename

    def cross_device_rename(source: Path | str, destination: Path | str) -> None:
        if Path(source) == staging / "var" / "cache":
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(source, destination)

    def half_written(source: Path, destination: Path) -> None:
        destination.mkdir(parents=True)
        (destination / "arrived-first").write_text("half of it")
        raise CommandFailed("cp --archive ended with exit status 1")

    monkeypatch.setattr(os, "rename", cross_device_rename)
    with pytest.raises(ConversionFailed):
        convert.convert(staging, ("var",), copy=half_written, root=root)

    assert (root / "var" / "old-log").read_text() == "old var", "the original is back"
    assert not (root / "var" / "cache").exists(), sorted(
        one.name for one in (root / "var").iterdir()
    )


def test_a_directory_that_cannot_be_listed_is_not_reported_as_empty(
    tmp_path: Path,
) -> None:
    """The guard on the irreversible step could not tell safe from unreadable.

    `_mounts_inside` returned `[]` for a directory with nothing mounted below
    it and `[]` for one whose listing raised, and the caller uses that answer
    to decide the rename may proceed. Its own comment says why that matters:
    `rename(2)` answers EBUSY for a mount point, and finding that out halfway
    through the entries is a state with no clean rollback.
    """
    import pytest as _pytest

    from gentoo_install.errors import ConversionFailed
    from gentoo_install.exec.convert import _mounts_inside

    # A directory with nothing mounted below it still answers empty.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a-file").write_text("")
    assert _mounts_inside(plain, frozenset()) == []

    # One that cannot be listed is refused, and the message names it.
    absent = tmp_path / "never-made"
    with _pytest.raises(ConversionFailed) as refused:
        _mounts_inside(absent, frozenset())
    assert str(absent) in str(refused.value), str(refused.value)

    # A file where a directory was expected raises through the same path.
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("")
    with _pytest.raises(ConversionFailed):
        _mounts_inside(not_a_directory, frozenset())


def test_a_copied_entry_is_undone_by_removing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback is told how each entry arrived rather than asking again.

    It used to try the reverse `rename` and read `EXDEV` to decide, so the
    answer depended on the mount still being there. `convert` records the
    error and leaves the directory half replaced, and on `/usr` that is a
    machine that does not boot.
    """
    del monkeypatch

    from gentoo_install.exec.convert import KEPT_ASIDE, Arrival, _restore_contents

    destination = tmp_path / "var"
    staged = tmp_path / "staging" / "var"
    aside = destination / KEPT_ASIDE
    for one in (destination, staged, aside):
        one.mkdir(parents=True)
    # What the machine looks like after `_replace_contents`: the original
    # entry held aside, the staged one copied in, and the staged original
    # still present because a copy does not consume it.
    (aside / "old").write_text("the machine's own")
    # The copy is half written, which is the hazard `_replace_contents` records
    # when it puts the entry on the list before calling `copy`.
    (destination / "new").write_text("from the stage3, half")
    (staged / "new").write_text("from the stage3, whole")

    # `rename` works throughout: the mount the copy crossed is gone by the time
    # the rollback runs, which is exactly when rederiving the answer gives the
    # wrong one. The old code renamed the underlying entry into staging and
    # left the copy behind.
    _restore_contents(destination, staged, [("new", Arrival.COPIED)])

    # The machine's own entry is back and the copy is gone.
    assert (destination / "old").read_text() == "the machine's own"
    assert not (destination / "new").exists()
    # The staged original is untouched, so a later attempt still has it. The
    # rollback used to rename the half-written copy over it.
    assert (staged / "new").read_text() == "from the stage3, whole"
    assert not aside.exists()


def test_a_rollback_still_raises_for_anything_but_a_crossing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `EXDEV` means the entry arrived by copy; a busy one is a failure."""
    import errno
    import os

    from gentoo_install.exec.convert import KEPT_ASIDE, Arrival, _restore_contents

    destination = tmp_path / "var"
    staged = tmp_path / "staging" / "var"
    (destination / KEPT_ASIDE).mkdir(parents=True)
    staged.mkdir(parents=True)
    (destination / "new").write_text("")

    def busy(source: "str | Path", target: "str | Path") -> None:
        del source, target
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(os, "rename", busy)
    with pytest.raises(OSError) as raised:
        _restore_contents(destination, staged, [("new", Arrival.RENAMED)])
    assert raised.value.errno == errno.EBUSY


def test_the_mount_state_is_read_once_for_each_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two reads decided two different things about the same directory.

    The first chose whether nested mounts had to be counted; the second, five
    lines later, chose which replacement and rollback the entry gets. A mount
    appearing between them skipped the check and still took the mounted path,
    which is the path the check exists to make safe.
    """
    import os

    from gentoo_install.exec import convert as exec_convert

    root = tmp_path / "root"
    staging = tmp_path / "root" / "gentoo-install.new"
    for name in ("usr", "var"):
        (root / name).mkdir(parents=True)
        (staging / name).mkdir(parents=True)

    reads: list[int] = []
    real_points = exec_convert._mount_points

    def counting() -> frozenset[str]:
        reads.append(1)
        return real_points()

    monkeypatch.setattr(exec_convert, "_mount_points", counting)
    exec_convert.convert(
        staging, ("usr", "var"), copy=lambda source, target: None, root=root
    )

    assert len(reads) == 1, reads


def test_every_field_of_a_cloud_image_row_is_read_somewhere() -> None:
    """`installer` named each image's package manager and nothing read it.

    Its comment said it was for the line `bootstrap.sh --missing-commands`
    prints; that reading was replaced because it did not work — the note above
    `install_tools` records that looking for the installer's own name found
    nothing and installed nothing — and the column outlived it. A row of a
    table is state that implies a behaviour, so each of its fields has to have
    a reader.
    """
    import ast
    from dataclasses import fields
    from pathlib import Path

    from tests.vm.convert import CloudImage

    root = Path(__file__).resolve().parents[2] / "tests" / "vm"
    read: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                read.add(node.attr)

    columns = [one.name for one in fields(CloudImage)]
    assert columns, "the table has no column at all"
    assert [one for one in columns if one not in read] == [], sorted(
        one for one in columns if one not in read
    )
