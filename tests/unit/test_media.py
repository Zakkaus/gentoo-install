# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.vm import media


def test_replacing_an_iso_with_preserved_metadata_re_extracts_its_boot_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restored ISO may retain metadata while carrying a different kernel."""
    extracted: list[Path] = []

    def extract(iso: Path, files: dict[str, Path]) -> None:
        extracted.append(iso)
        for target in files.values():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(iso.read_bytes())

    first = b"first-build"
    second = b"secondbuild"
    # The premise: only content differs. An edit that changed the length would
    # test the easy path and still pass.
    assert len(first) == len(second)
    iso = tmp_path / "rolling.iso"
    iso.write_bytes(first)
    medium = media.Medium(
        name="rolling",
        iso=iso,
        volume_label="rolling",
        kernel_in_iso="/boot/kernel",
        initrd_in_iso="/boot/initrd",
        root_prompt="# ",
    )
    monkeypatch.setattr(media, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(media, "_extract", extract)

    kernel, initrd = medium.boot_files()
    assert (kernel.read_bytes(), initrd.read_bytes()) == (first, first)
    original = iso.stat()
    iso.write_bytes(second)
    os.utime(iso, ns=(original.st_atime_ns, original.st_mtime_ns))

    kernel, initrd = medium.boot_files()
    assert len(extracted) == 2, "a changed ISO was served from the cache"
    assert (kernel.read_bytes(), initrd.read_bytes()) == (second, second)
