# SPDX-License-Identifier: GPL-2.0-or-later
"""Arming one boot into a memory-resident live environment."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from collections.abc import Callable
from typing import Sequence

import pytest

from gentoo_install.errors import DownloadFailed, PreflightFailed
from gentoo_install.model.config import BootMethod, MemoryLaunch, MemoryMode
from gentoo_install.plan import netboot
from gentoo_install.plan.operations import CommandOutput, Operation, Stage

from .recorder import Recorder

ESP = "/boot/efi"
ISO = "install-amd64-cjk-minimal-20260813T073053Z.iso"

#: One real answer from the release index, trimmed to the fields this reads.
#: Taken from `api.github.com/repos/gentoo-zh/gentoo-cjk-livecd/releases/latest`
#: on 2026-08-17, where the newest tag was `20260813T073053Z`.
CJK_INDEX = json.dumps(
    {
        "tag_name": "20260813T073053Z",
        "assets": [
            {"name": f"{ISO}.CONTENTS.gz", "browser_download_url": "https://host/c.gz"},
            {"name": ISO, "browser_download_url": f"https://host/{ISO}"},
            {"name": f"{ISO}.sha256", "browser_download_url": f"https://host/{ISO}.sha256"},
        ],
    }
)

DIGEST = "e3" * 32

#: One real record shape from `latest-releases.yaml`, read on 2026-08-18.
ALPINE_INDEX = """---
-
  title: "Mini root filesystem"
  flavor: alpine-minirootfs
  file: alpine-minirootfs-3.24.1-x86_64.tar.gz
  sha256: 41f73e3cf5fa919b8aa5ca6b30dc48f0da2720776d7423e2a7748211456fe081
-
  title: "Netboot"
  flavor: alpine-netboot
  file: alpine-netboot-3.24.1-x86_64.tar.gz
  sha256: 9a7769ea8fa1737b1b49d82f1bdd53d0a17338d6d3b7cfc6f2c3ec5158596d8b
-
  title: "Standard"
  flavor: alpine-standard
  file: alpine-standard-3.24.1-x86_64.iso
  sha256: 0000000000000000000000000000000000000000000000000000000000000000
"""


def _target(method: BootMethod = BootMethod.SYSTEMD_BOOT) -> netboot.BootTarget:
    if method is BootMethod.BIOS_GRUB:
        return netboot.BootTarget(method=method, grub_directory="/boot/grub")
    return netboot.BootTarget(
        method=method,
        esp_mountpoint=ESP,
        esp_device="/dev/nvme0n1p1",
        esp_disk="/dev/nvme0n1",
        esp_partition=1,
        grub_directory="/boot/grub",
    )


def _launch(
    mode: MemoryMode = MemoryMode.RAM,
    *,
    ssh_key: str = "",
    ssh_port: int | None = None,
    root_password: str = "",
) -> MemoryLaunch:
    return MemoryLaunch(
        mode=mode, ssh_key=ssh_key, ssh_port=ssh_port, root_password=root_password
    )


def _answering(mode: MemoryMode = MemoryMode.RAM, *, digest: str = DIGEST) -> Recorder:
    """A machine that answers everything this plan asks it."""

    def answer(argv: Sequence[str]) -> str | None:
        if argv[0] == "curl":
            wanted = argv[-1]
            if wanted == netboot.CJK_RELEASES:
                return CJK_INDEX
            if wanted == netboot.ALPINE_RELEASES:
                return ALPINE_INDEX
            if wanted.endswith(".sha256"):
                return f"{DIGEST}  {ISO}\n"
            return ""
        if argv[0] == "sha256sum":
            return f"{digest}  {argv[1]}\n"
        if argv[0] == "ls":
            name = ISO if mode is MemoryMode.RAM else "alpine-netboot-3.24.1-x86_64.tar.gz"
            return f"{name}\nkernel\ninitramfs\n"
        if argv[0] == "blkid":
            return "Gentoo-CJK-amd64-20260813\n"
        if argv[0] == "free":
            return "               total        used\nMem:            7900        1200\nTotal:         15000        1200\n"
        return None

    recorder = Recorder()
    recorder.answering = answer
    return recorder


def _apply(operations: list[Operation], recorder: Recorder) -> None:
    for operation in operations:
        operation.apply(recorder)


def _run(recorder: Recorder, first: str) -> list[tuple[str, ...]]:
    return [one for one in recorder.commands if one and one[0] == first]


def test_every_operation_describes_itself_without_touching_anything() -> None:
    """`render()` prints `describe()` and applies nothing, so a description
    that reads the machine is a dry run that is not one."""
    plan = netboot.build(launch=_launch(), target=_target())
    empty = Recorder()
    for operation in plan:
        assert operation.describe()
    assert empty.commands == []
    assert empty.files == {}


def test_the_plan_refuses_and_checks_before_it_fetches() -> None:
    """Nothing may be downloaded onto a machine that cannot be told to boot
    it: an arming half done is the failure this path must not have."""
    plan = netboot.build(launch=_launch(), target=_target())
    stages = [one.stage for one in plan]
    assert stages == sorted(stages, key=lambda one: one.order), stages
    assert stages[0] is Stage.PREFLIGHT
    fetch = next(n for n, one in enumerate(plan) if isinstance(one, netboot.FetchMemoryImage))
    assert all(one.stage is Stage.PREFLIGHT for one in plan[:fetch]), plan[:fetch]


def test_a_machine_with_no_boot_method_is_refused() -> None:
    with pytest.raises(PreflightFailed, match="boot once"):
        netboot.RefuseWithoutABootMethod(
            target=netboot.BootTarget(method=BootMethod.NONE)
        ).apply(Recorder())


def test_a_uefi_machine_with_no_mounted_esp_is_refused() -> None:
    """The firmware reads a kernel from the esp and from nowhere else, so an
    unmounted one is a refusal rather than a download that lands in `/`."""
    with pytest.raises(PreflightFailed, match="EFI system partition"):
        netboot.RefuseWithoutABootMethod(
            target=netboot.BootTarget(method=BootMethod.SYSTEMD_BOOT)
        ).apply(Recorder())


def test_a_machine_under_the_measured_floor_is_refused() -> None:
    """`check_live_ram` enters an emergency shell rather than reporting this,
    and an emergency shell on a machine nobody is watching is a machine that
    never comes back."""
    recorder = _answering()
    recorder.answering = lambda argv: (
        "               total        used\nMem:             900         100\n"
        if argv[0] == "free"
        else None
    )
    with pytest.raises(PreflightFailed, match="emergency shell"):
        netboot.RefuseTooLittleMemory(mode=MemoryMode.RAM).apply(recorder)


def test_a_memory_reading_that_failed_is_not_a_refusal() -> None:
    """A refusal built on a command that did not run is one nobody can act on."""
    recorder = Recorder()
    recorder.answering = lambda argv: CommandOutput("", 1) if argv[0] == "free" else None
    netboot.RefuseTooLittleMemory(mode=MemoryMode.RAM).apply(recorder)


def test_the_floors_differ_because_the_two_images_do() -> None:
    """`--ram` reads an 824 MiB squashfs into memory and `--lowram` does not,
    which is the whole reason there are two modes."""
    assert netboot.RefuseTooLittleMemory(mode=MemoryMode.RAM).floor > (
        netboot.RefuseTooLittleMemory(mode=MemoryMode.LOWRAM).floor
    )


def test_the_iso_is_taken_from_the_newest_release_rather_than_a_pinned_name() -> None:
    """The published asset carries a build timestamp, and the release this was
    first written against was already gone from the index a week later."""
    recorder = _answering()
    netboot.FetchMemoryImage(mode=MemoryMode.RAM, target=_target()).apply(recorder)

    fetched = [one for one in _run(recorder, "curl") if "--output" in one]
    assert len(fetched) == 1, fetched
    assert fetched[0][-1] == f"https://host/{ISO}", fetched
    assert f"{ESP}/{netboot.PLACE}/{ISO}" in fetched[0], fetched


def test_an_image_whose_checksum_does_not_match_is_deleted_and_named() -> None:
    """A wrong image is otherwise discovered at the next boot, when the machine
    the operator was logged into is no longer answering."""
    recorder = _answering(digest="00" * 32)
    with pytest.raises(DownloadFailed, match="publisher says"):
        netboot.FetchMemoryImage(mode=MemoryMode.RAM, target=_target()).apply(recorder)
    assert any(one[0] == "rm" for one in recorder.commands), recorder.commands


def test_the_alpine_bundle_is_read_out_of_the_published_index() -> None:
    """`latest-releases.yaml` gives the version, the filename and the SHA-256,
    so none of the three is written into this installer."""
    recorder = _answering(MemoryMode.LOWRAM, digest="9a7769ea8fa1737b1b49d82f1bdd53d0a17338d6d3b7cfc6f2c3ec5158596d8b")
    netboot.FetchMemoryImage(mode=MemoryMode.LOWRAM, target=_target()).apply(recorder)

    fetched = [one for one in _run(recorder, "curl") if "--output" in one]
    assert fetched[0][-1].endswith("alpine-netboot-3.24.1-x86_64.tar.gz"), fetched


def test_the_ram_cmdline_adds_dodhcp_and_finds_the_iso_by_its_own_scanner() -> None:
    """The ISO ships `nodhcp`, so a copied line comes up with no network and
    cannot fetch a stage3. GRUB's `loopback` is visible only inside GRUB, so
    the initramfs finds the file with `iso-scan/filename` instead."""
    recorder = _answering()
    netboot.WriteMemoryEntry(
        mode=MemoryMode.RAM, target=_target(), launch=_launch()
    ).apply(recorder)

    entry = recorder.files[PurePosixPath(f"{ESP}/loader/entries/{netboot.PLACE}.conf")]
    assert "dodhcp" in entry, entry
    assert "nodhcp" not in entry, entry
    assert "rd.live.ram=1" in entry, entry
    assert f"iso-scan/filename=/{netboot.PLACE}/{ISO}" in entry, entry
    assert "root=live:CDLABEL=Gentoo-CJK-amd64-20260813" in entry, entry


def test_the_label_is_read_from_the_image_rather_than_from_its_name() -> None:
    """A release that changes one without the other boots to a dracut shell
    saying it cannot find the live image."""
    recorder = _answering()
    recorder.answering = _relabelled(recorder.answering, "Gentoo-CJK-amd64-SOMETHING")
    netboot.WriteMemoryEntry(
        mode=MemoryMode.RAM, target=_target(), launch=_launch()
    ).apply(recorder)

    entry = recorder.files[PurePosixPath(f"{ESP}/loader/entries/{netboot.PLACE}.conf")]
    assert "CDLABEL=Gentoo-CJK-amd64-SOMETHING" in entry, entry


def test_an_image_with_no_label_stops_rather_than_booting_to_a_dracut_shell() -> None:
    recorder = _answering()
    recorder.answering = _relabelled(recorder.answering, "")
    with pytest.raises(DownloadFailed, match="volume label"):
        netboot.WriteMemoryEntry(
            mode=MemoryMode.RAM, target=_target(), launch=_launch()
        ).apply(recorder)


def test_a_root_password_brings_sshd_with_it_because_dosshd_scrambles_the_old_one() -> None:
    """`dosshd` is documented as requiring `passwd=`: without one the machine
    comes up with sshd running and nothing that can authenticate to it."""
    recorder = _answering()
    netboot.WriteMemoryEntry(
        mode=MemoryMode.RAM, target=_target(), launch=_launch(root_password="hunter2")
    ).apply(recorder)

    entry = recorder.files[PurePosixPath(f"{ESP}/loader/entries/{netboot.PLACE}.conf")]
    assert "dosshd" in entry and "passwd=hunter2" in entry, entry


def test_no_root_password_writes_neither_half() -> None:
    recorder = _answering()
    netboot.WriteMemoryEntry(
        mode=MemoryMode.RAM, target=_target(), launch=_launch()
    ).apply(recorder)

    entry = recorder.files[PurePosixPath(f"{ESP}/loader/entries/{netboot.PLACE}.conf")]
    assert "dosshd" not in entry and "passwd=" not in entry, entry


def test_the_lowram_cmdline_names_the_repository_alpine_fetches_modloop_from() -> None:
    """`alpine_repo=auto` searches for a `.boot_repository` file and finds none
    on a machine that booted from a kernel placed on its own disk."""
    recorder = _answering(MemoryMode.LOWRAM)
    netboot.WriteMemoryEntry(
        mode=MemoryMode.LOWRAM, target=_target(), launch=_launch(MemoryMode.LOWRAM)
    ).apply(recorder)

    entry = recorder.files[PurePosixPath(f"{ESP}/loader/entries/{netboot.PLACE}.conf")]
    assert f"alpine_repo={netboot.ALPINE_REPOSITORY}" in entry, entry
    assert "modloop=" in entry and "ip=dhcp" in entry, entry


def test_a_key_given_for_lowram_reaches_alpines_own_option() -> None:
    """Alpine's netboot init installs openssh and enables sshd when `ssh_key`
    is set, which is the only ssh mechanism either medium takes on a cmdline."""
    recorder = _answering(MemoryMode.LOWRAM)
    netboot.WriteMemoryEntry(
        mode=MemoryMode.LOWRAM,
        target=_target(),
        launch=_launch(MemoryMode.LOWRAM, ssh_key="https://github.com/zakkaus.keys"),
    ).apply(recorder)

    entry = recorder.files[PurePosixPath(f"{ESP}/loader/entries/{netboot.PLACE}.conf")]
    assert "ssh_key=https://github.com/zakkaus.keys" in entry, entry


def test_a_bios_machine_gets_a_marked_custom_entry_and_grub_reboot() -> None:
    recorder = _answering()
    target = _target(BootMethod.BIOS_GRUB)
    _apply(
        [
            netboot.WriteMemoryEntry(mode=MemoryMode.RAM, target=target, launch=_launch()),
            netboot.ArmOneShot(target=target),
        ],
        recorder,
    )

    custom = recorder.files[PurePosixPath("/boot/grub/custom.cfg")]
    assert custom.startswith(netboot.CUSTOM_BEGIN), custom
    assert custom.rstrip().endswith(netboot.CUSTOM_END), custom
    assert ("grub-reboot", netboot.ENTRY_LABEL) in recorder.commands, recorder.commands


def test_arming_twice_replaces_the_entry_rather_than_adding_beside_it() -> None:
    """A `custom.cfg` that grows one entry per arming boots whichever GRUB
    picks, which is not the one just written."""
    recorder = _answering()
    target = _target(BootMethod.BIOS_GRUB)
    for _ in range(3):
        netboot.WriteMemoryEntry(
            mode=MemoryMode.RAM, target=target, launch=_launch()
        ).apply(recorder)

    custom = recorder.files[PurePosixPath("/boot/grub/custom.cfg")]
    assert custom.count(netboot.CUSTOM_BEGIN) == 1, custom
    assert custom.count("menuentry") == 1, custom


def test_an_unrelated_custom_entry_is_kept() -> None:
    """The operator's own entries are in that file, and taking them out is a
    machine that stops offering something it used to."""
    recorder = _answering()
    target = _target(BootMethod.BIOS_GRUB)
    recorder.files[PurePosixPath("/boot/grub/custom.cfg")] = "menuentry 'memtest' {}\n"
    netboot.WriteMemoryEntry(
        mode=MemoryMode.RAM, target=target, launch=_launch()
    ).apply(recorder)

    custom = recorder.files[PurePosixPath("/boot/grub/custom.cfg")]
    assert "memtest" in custom, custom


def test_systemd_boot_is_armed_for_one_boot_and_not_made_the_default() -> None:
    """A memory environment that does not come up has to leave a machine that
    still boots, so the default entry is never touched without `--bypass`."""
    recorder = _answering()
    netboot.ArmOneShot(target=_target()).apply(recorder)

    assert ("bootctl", "set-oneshot", f"{netboot.PLACE}.conf") in recorder.commands
    assert not any("set-default" in one for one in recorder.commands), recorder.commands


def test_bypass_replaces_the_default_and_is_never_what_build_picks_by_itself() -> None:
    """`--bypass` is the one path where an environment that does not come up
    leaves a machine that does not boot at all, so nothing selects it for the
    operator."""
    assert any(
        isinstance(one, netboot.ArmOneShot)
        for one in netboot.build(launch=_launch(), target=_target())
    )
    replacing = netboot.build(launch=_launch(), target=_target(), bypass=True)
    assert any(isinstance(one, netboot.ReplaceDefaultBoot) for one in replacing)
    assert not any(isinstance(one, netboot.ArmOneShot) for one in replacing)

    recorder = _answering()
    netboot.ReplaceDefaultBoot(target=_target()).apply(recorder)
    assert ("bootctl", "set-default", f"{netboot.PLACE}.conf") in recorder.commands


def test_disarming_takes_back_the_arming_and_deletes_what_it_placed() -> None:
    """A machine left armed boots into the memory environment the next time
    anything reboots it, which may be months later and for another reason."""
    recorder = _answering()
    _apply(netboot.disarm(target=_target(BootMethod.BIOS_GRUB)), recorder)

    assert any(one[0] == "grub-editenv" and "next_entry" in one for one in recorder.commands)
    assert any(one[0] == "rm" and "/boot/gentoo-install-ram" in one for one in recorder.commands)


def test_every_boot_method_that_can_be_armed_has_a_branch() -> None:
    """A method added to the enum without one here writes nothing and arms
    nothing, and says so only after the reboot."""
    for method in BootMethod:
        if method is BootMethod.NONE:
            continue
        recorder = _answering()
        target = _target(method)
        _apply(
            [
                netboot.WriteMemoryEntry(mode=MemoryMode.RAM, target=target, launch=_launch()),
                netboot.ArmOneShot(target=target),
            ],
            recorder,
        )
        assert recorder.files, method
        assert recorder.commands, method


def _relabelled(
    previous: Callable[[Sequence[str]], str | None] | None, label: str
) -> Callable[[Sequence[str]], str | None]:
    """The same answers with a different volume label."""

    def answer(argv: Sequence[str]) -> str | None:
        if argv[0] == "blkid":
            return f"{label}\n"
        return previous(argv) if previous is not None else None

    return answer
