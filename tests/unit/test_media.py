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


def test_every_medium_names_a_volume_label_the_iso_actually_carries() -> None:
    """`GENTOO_CJK` carried `Gentoo-CJK-amd64-20260809` while its file name
    said `20260820`: the label is date-stamped by the build, so repinning the
    ISO and leaving the label behind gives a medium that boots and is then
    refused for being the wrong one. Read out of the file rather than assumed,
    and skipped for a medium whose ISO is not downloaded — that is the
    workstation missing a file, not a defect in the table."""
    from tests.vm.media import MEDIA

    checked = 0
    for medium in MEDIA.values():
        if not medium.volume_label or not medium.iso.is_file():
            continue
        with medium.iso.open("rb") as image:
            # The primary volume descriptor starts at sector 16 and its
            # volume identifier is 32 bytes at offset 40 inside it.
            image.seek(32768 + 40)
            carried = image.read(32).decode("ascii", "replace").rstrip()
        assert carried == medium.volume_label, (medium.name, carried, medium.volume_label)
        checked += 1
    if not checked:
        pytest.skip("no medium's ISO is downloaded here")


def test_every_command_a_fixture_needs_has_an_alpine_package_named_for_it() -> None:
    """`apk add` takes the whole list or none of it. `ALPINE_PACKAGE_FOR_COMMAND`
    fell back to the command's own name, so `cat`, `dd`, `mkdir`, `sleep`,
    `test`, `install`, `truncate`, `dmsetup` and `vgchange` were handed to apk
    as package names; it answered `cat (no such package)` and refused the
    transaction, python3 included. The run then failed two steps later with
    `this installer needs python 3.11 or newer; found: none`, which names
    neither apk nor the table.
    """
    from tests.vm.media import ALPINE_PACKAGE_FOR_COMMAND, _alpine_packages, _fixture_commands

    commands = _fixture_commands()
    assert commands, "no fixture asked for a command"
    unnamed = sorted(one for one in commands if one not in ALPINE_PACKAGE_FOR_COMMAND)
    assert not unnamed, unnamed

    # And an unlisted one is refused rather than passed through, or the table
    # goes stale again the next time a fixture needs a new tool.
    from tests.vm.media import MediaError

    with pytest.raises(MediaError, match="no Alpine package is named"):
        _alpine_packages([*commands, "a-command-nothing-provides"])
