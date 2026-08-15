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
