# SPDX-License-Identifier: GPL-2.0-or-later
"""Arming one boot into a memory-resident live environment."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path, PurePosixPath
from collections.abc import Callable
from typing import Final, Sequence

import pytest

from gentoo_install.errors import DownloadFailed, PreflightFailed
from gentoo_install.model.config import BootMethod, MemoryLaunch, MemoryMode
from gentoo_install.plan import netboot
from gentoo_install.plan.operations import CommandOutput, Operation, Stage

from .recorder import Recorder

ESP = "/boot/efi"

#: What `uname -m` answers on the machine most of these cases stand on.
MACHINE = "x86_64"
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
        return netboot.BootTarget(
            method=method, architecture=MACHINE, grub_directory="/boot/grub"
        )
    return netboot.BootTarget(
        method=method,
        architecture=MACHINE,
        esp_mountpoint=ESP,
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


ALPINE_ARCHIVE: Final[str] = "alpine-netboot-3.24.1-x86_64.tar.gz"

#: What a machine reached over a serial port boots with. Taken from the cloud
#: image `tests/vm/convert.py` drives, which is the machine these two modes are
#: armed on in the harness.
RUNNING_CMDLINE: Final[str] = (
    "BOOT_IMAGE=/boot/vmlinuz root=UUID=1e2c ro console=tty0 "
    "console=ttyS0,115200n8 quiet\n"
)


def _answering(
    mode: MemoryMode = MemoryMode.RAM,
    *,
    digest: str = DIGEST,
    archive: str = ALPINE_ARCHIVE,
    cmdline: str = RUNNING_CMDLINE,
) -> Recorder:
    """A machine that answers everything this plan asks it."""

    def answer(argv: Sequence[str]) -> str | None:
        if argv[0] == "cat" and argv[-1] == netboot.RUNNING_CMDLINE:
            return cmdline
        if argv[0] == "curl":
            wanted = argv[-1]
            if wanted == netboot.CJK_RELEASES:
                return CJK_INDEX
            if wanted.endswith("latest-releases.yaml"):
                return ALPINE_INDEX
            if wanted.endswith(".sha256"):
                return f"{DIGEST}  {ISO}\n"
            return ""
        if argv[0] == "sha256sum":
            return f"{digest}  {argv[1]}\n"
        if argv[0] == "ls":
            name = ISO if mode is MemoryMode.RAM else archive
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
            target=_target(BootMethod.NONE)
        ).apply(Recorder())


def test_a_uefi_machine_with_no_mounted_esp_is_refused() -> None:
    """The firmware reads a kernel from the esp and from nowhere else, so an
    unmounted one is a refusal rather than a download that lands in `/`."""
    with pytest.raises(PreflightFailed, match="EFI system partition"):
        netboot.RefuseWithoutABootMethod(
            target=replace(_target(), esp_mountpoint=None)
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
    # On the root filesystem, not the esp: the esp is whatever size that
    # machine was given, 124 MiB on a Debian cloud image, and this file is
    # about a gigabyte.
    assert f"/{netboot.PLACE}/{ISO}" in fetched[0], fetched
    assert ESP not in " ".join(fetched[0]), fetched


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


def test_a_key_alone_starts_the_daemon_it_is_meant_to_authenticate_to() -> None:
    """`/etc/init.d/autoconfig:146,242` schedules sshd for `dosshd` and for
    nothing else, and the medium's default runlevel holds no sshd, so the key
    the initramfs hook copies to `/root/.ssh` reached a machine with nothing
    listening."""
    recorder = _answering()
    netboot.WriteMemoryEntry(
        mode=MemoryMode.RAM,
        target=_target(),
        launch=_launch(ssh_key="https://github.com/zakkaus.keys"),
    ).apply(recorder)

    entry = recorder.files[PurePosixPath(f"{ESP}/loader/entries/{netboot.PLACE}.conf")]
    assert "dosshd" in entry, entry
    assert "passwd=" not in entry, entry


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


def _modloop(entry: str) -> str:
    words = [one for one in entry.split() if one.startswith("modloop=")]
    assert len(words) == 1, entry
    return words[0]


def _lowram_entry(archive: str) -> str:
    recorder = _answering(MemoryMode.LOWRAM, archive=archive)
    netboot.WriteMemoryEntry(
        mode=MemoryMode.LOWRAM, target=_target(), launch=_launch(MemoryMode.LOWRAM)
    ).apply(recorder)
    return recorder.files[PurePosixPath(f"{ESP}/loader/entries/{netboot.PLACE}.conf")]


def test_the_modloop_is_the_one_beside_the_kernel_that_was_downloaded() -> None:
    """`netboot/` holds whatever release is newest, so a machine armed the day
    before an Alpine release would boot a kernel from one version and mount a
    modloop from the next, whose `modules/$(uname -r)` does not exist."""
    assert _modloop(_lowram_entry("alpine-netboot-3.24.1-x86_64.tar.gz")).endswith(
        "/releases/x86_64/netboot-3.24.1/modloop-lts"
    ), _lowram_entry("alpine-netboot-3.24.1-x86_64.tar.gz")


def test_the_modloop_follows_the_machine_rather_than_this_file() -> None:
    """Alpine publishes `netboot-<version>/modloop-lts` under every
    architecture's own directory, checked against `latest-stable/releases/` for
    `x86_64` and `aarch64` on 2026-08-18."""
    word = _modloop(_lowram_entry("alpine-netboot-3.24.1-aarch64.tar.gz"))
    assert "/releases/aarch64/netboot-3.24.1/" in word, word
    assert "x86_64" not in word, word


def _ram_entry(cmdline: str) -> str:
    recorder = _answering(cmdline=cmdline)
    netboot.WriteMemoryEntry(
        mode=MemoryMode.RAM, target=_target(), launch=_launch()
    ).apply(recorder)
    return recorder.files[PurePosixPath(f"{ESP}/loader/entries/{netboot.PLACE}.conf")]


def test_the_entry_carries_the_console_the_machine_already_answers_on() -> None:
    """`fixinittab` on the CJK medium auto-detects `hvc0`, `ttyHV0` and
    `ttyAMA0` only, and comments the medium's own `s0` line out, so an amd64
    machine driven over `ttyS0` gets no getty and shows the operator nothing.
    Read from `/etc/init.d/fixinittab:24-42,104-105` in the ISO's squashfs."""
    entry = _ram_entry(RUNNING_CMDLINE)
    assert "console=tty0" in entry and "console=ttyS0,115200n8" in entry, entry


def test_the_console_order_is_the_machines_own() -> None:
    """The last `console=` is what the kernel gives `/dev/console`, and
    `livecd-functions.sh:119-136` takes the last one into `LIVECD_CONSOLE` the
    same way, so reordering them moves the first screen to another device."""
    entry = _ram_entry("root=UUID=1e2c console=ttyS0,115200n8 console=tty0\n")
    options = next(
        line for line in entry.splitlines() if line.startswith("options")
    )
    assert options.index("console=ttyS0") < options.index("console=tty0"), options


def test_the_value_that_means_no_console_is_not_carried_over() -> None:
    """`console=null` is the one value that asks for nothing. Alpine's
    `setup_inittab_console` returns before writing a single getty when it sees
    it (`initramfs-init.in:136-140`) and `switch_root -c /dev/null` follows,
    and `LIVECD_CONSOLE=null` puts the CJK medium's getty on `/dev/null`, so
    carrying it forward is worse than carrying nothing."""
    entry = _ram_entry("root=UUID=1e2c console=null\n")
    assert "console=" not in entry, entry


def test_a_real_console_beside_the_silent_one_still_comes_over() -> None:
    entry = _ram_entry("root=UUID=1e2c console=null console=ttyS0,115200n8\n")
    assert "console=ttyS0,115200n8" in entry, entry
    assert "console=null" not in entry, entry


def test_a_machine_that_names_no_console_is_left_alone() -> None:
    """An ordinary workstation boots without one, and writing `console=ttyS0`
    for it would move its first screen to a port that is not there."""
    entry = _ram_entry("BOOT_IMAGE=/boot/vmlinuz root=UUID=1e2c ro quiet\n")
    assert "console=" not in entry, entry


def test_the_lowram_entry_carries_the_console_too() -> None:
    """Alpine's `initramfs-init` reads `console=` outside `myopts` and passes
    it to `switch_root -c`, so the same argument decides where its own first
    screen lands."""
    recorder = _answering(MemoryMode.LOWRAM, cmdline=RUNNING_CMDLINE)
    netboot.WriteMemoryEntry(
        mode=MemoryMode.LOWRAM, target=_target(), launch=_launch(MemoryMode.LOWRAM)
    ).apply(recorder)

    entry = recorder.files[PurePosixPath(f"{ESP}/loader/entries/{netboot.PLACE}.conf")]
    assert "console=ttyS0,115200n8" in entry, entry


def test_an_archive_named_otherwise_is_refused_rather_than_guessed() -> None:
    """A URL composed from a name that does not parse would be fetched by
    `wget` inside the booted medium, where its failure is a warning on a screen
    nobody is watching and a system with no modules."""
    with pytest.raises(DownloadFailed):
        _lowram_entry("netboot.tar.gz")


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


def _appended(recorder: Recorder, keys: tuple[str, ...] = ()) -> None:
    netboot.AppendConfiguration(
        target=_target(),
        launch=_launch(),
        configuration='[disk]\nmode = "partition"\n',
        source="/opt/gentoo-install",
        keys=keys,
    ).apply(recorder)


def test_the_payload_is_appended_to_the_initramfs_rather_than_repacked() -> None:
    """A newc cpio appended to the compressed initramfs is unpacked by the
    kernel and its hooks are run by dracut: measured on 2026-08-18 by booting
    one, where a hook in the appended segment printed at 3.5 seconds. So 1.5
    KiB of cpio delivers everything and the 55 MiB image is never rewritten.
    """
    recorder = _answering()
    _appended(recorder)

    appended = [
        one for one in recorder.commands if one[0] == "sh" and "cpio --create" in one[-1]
    ]
    assert len(appended) == 1, recorder.commands
    # Appended, not overwritten: `>` would leave an initramfs holding only the
    # payload and a machine with no way to mount its root.
    assert ">>" in appended[0][-1] and ">> " in appended[0][-1], appended
    assert f"{netboot.PLACE}/initramfs" in appended[0][-1], appended


def test_the_archive_is_made_from_inside_the_staging_directory() -> None:
    """`find` from anywhere else writes the staging prefix into every path, so
    the payload unpacks to a directory the hook does not look in."""
    recorder = _answering()
    _appended(recorder)

    made = next(one for one in recorder.commands if "cpio --create" in one[-1])
    assert made[-1].startswith("cd ") and "&& find . |" in made[-1], made


def test_the_installer_travels_with_its_own_configuration() -> None:
    """The configuration was written by this revision, so the environment runs
    this revision rather than whatever a later download would bring."""
    recorder = _answering()
    _appended(recorder)

    carried = next(
        one for one in recorder.commands if one[0] == "sh" and "tar --create" in one[-1]
    )
    assert "cd /opt/gentoo-install" in carried[-1], carried
    assert "gentoo_install bootstrap.sh" in carried[-1], carried
    assert "__pycache__" in carried[-1], carried


def test_a_key_is_written_where_sshd_reads_it_and_not_world_readable() -> None:
    recorder = _answering()
    _appended(recorder, keys=("ssh-ed25519 AAAA zakk@box",))

    payload = PurePosixPath(f"{ESP}/{netboot.PLACE}/payload{netboot.PAYLOAD}")
    assert recorder.files[payload / "authorized_keys"].endswith("zakk@box\n")
    assert recorder.modes[payload / "authorized_keys"] == 0o600
    hook = recorder.files[
        PurePosixPath(f"{ESP}/{netboot.PLACE}/payload/usr/lib/dracut/hooks/pre-pivot/99-gentoo-install.sh")
    ]
    assert "/root/.ssh/authorized_keys" in hook, hook
    assert "chmod 600" in hook, hook


def test_the_live_system_asks_before_it_erases_anything() -> None:
    """`docs/design.md` settles this: the first screen offers two things and
    neither happens on a timer. A countdown that ends in a partitioned disk is
    one nobody can lose safely."""
    recorder = _answering()
    _appended(recorder)

    start = recorder.files[
        PurePosixPath(f"{ESP}/{netboot.PLACE}/payload{netboot.PAYLOAD}/start.sh")
    ]
    assert "read answer" in start, start
    assert "install)" in start and "bootstrap.sh --config" in start, start
    # No timeout anywhere: `read -t` and `sleep` are both ways to answer for
    # the operator, and the answer they would give erases a disk.
    assert "read -t" not in start and "sleep" not in start, start


def test_no_configuration_appends_nothing() -> None:
    """`--ram` with no configuration is the rescue path, and a payload with an
    empty `config.toml` would offer to install from it."""
    plan = netboot.build(launch=_launch(), target=_target())
    assert not any(isinstance(one, netboot.AppendConfiguration) for one in plan)

    carrying = netboot.build(
        launch=_launch(), target=_target(), configuration="x = 1\n", source="/opt/x"
    )
    at = next(
        n for n, one in enumerate(carrying) if isinstance(one, netboot.AppendConfiguration)
    )
    placed = next(
        n for n, one in enumerate(carrying) if isinstance(one, netboot.PlaceMemoryKernel)
    )
    entry = next(
        n for n, one in enumerate(carrying) if isinstance(one, netboot.WriteMemoryEntry)
    )
    # After the initramfs is placed and before the entry names it.
    assert placed < at < entry, [type(one).__name__ for one in carrying]


def _custom(recorder: Recorder) -> str:
    return recorder.files[PurePosixPath("/boot/grub/custom.cfg")]


def test_a_separate_boot_partition_gets_a_path_relative_to_itself() -> None:
    """GRUB's paths are relative to the filesystem its `root` names, which is
    the `/boot` partition when there is one."""
    recorder = _answering()
    target = replace(_target(BootMethod.BIOS_GRUB), boot_on_the_root_filesystem=False)
    netboot.WriteMemoryEntry(mode=MemoryMode.RAM, target=target, launch=_launch()).apply(
        recorder
    )

    written = _custom(recorder)
    assert f"linux /{netboot.PLACE}/kernel" in written, written
    assert "/boot/gentoo-install-ram" not in written, written


def test_boot_on_the_root_filesystem_gets_the_boot_prefix() -> None:
    """The same file is `/boot/gentoo-install-ram/kernel` to GRUB when `/boot`
    is a directory rather than a mount. Written the other way, the `search`
    matches nothing, `root` is left alone, and the entry stops in GRUB with
    the kernel not found — the machine still boots its own system, and the
    armed one never runs."""
    recorder = _answering()
    target = replace(_target(BootMethod.BIOS_GRUB), boot_on_the_root_filesystem=True)
    netboot.WriteMemoryEntry(mode=MemoryMode.RAM, target=target, launch=_launch()).apply(
        recorder
    )

    written = _custom(recorder)
    for line in ("search", "linux", "initrd"):
        named = next(one for one in written.splitlines() if one.strip().startswith(line))
        assert f"/boot/{netboot.PLACE}/" in named, named


def test_the_three_lines_of_an_entry_never_disagree_about_the_path() -> None:
    """`search` sets `root` by finding that exact path, so a `linux` line
    naming another one is an entry that finds a filesystem and then asks it
    for a file it does not have."""
    for on_root in (True, False, None):
        recorder = _answering()
        target = replace(
            _target(BootMethod.BIOS_GRUB), boot_on_the_root_filesystem=on_root
        )
        netboot.WriteMemoryEntry(
            mode=MemoryMode.RAM, target=target, launch=_launch()
        ).apply(recorder)

        written = _custom(recorder)
        searched = next(
            one.split()[-1] for one in written.splitlines() if "search" in one
        )
        loaded = next(
            one.split()[1] for one in written.splitlines() if one.strip().startswith("linux")
        )
        assert searched == loaded, (on_root, searched, loaded)


def test_an_unread_boot_layout_is_treated_as_a_separate_partition() -> None:
    """Every layout this installer produces has one, so that is the answer
    when nothing read the machine."""
    assert _target(BootMethod.BIOS_GRUB).grub_prefix == ""


def test_a_machine_enforcing_secure_boot_is_refused_before_anything_is_fetched() -> None:
    """Neither image is signed for this machine's own db, so the firmware
    rejects the kernel and the armed boot goes nowhere. With `--bypass` that
    entry is the default, which is a machine that does not boot at all."""
    refusing = replace(_target(BootMethod.BIOS_GRUB), secure_boot=True)
    with pytest.raises(PreflightFailed, match="Secure Boot"):
        netboot.RefuseWithoutABootMethod(target=refusing).apply(Recorder())


def test_secure_boot_off_or_unread_is_not_a_refusal() -> None:
    """A BIOS machine has no such variable, and refusing on a fact nobody
    could read is a refusal nobody can act on."""
    for state in (False, None):
        netboot.RefuseWithoutABootMethod(
            target=replace(_target(BootMethod.BIOS_GRUB), secure_boot=state)
        ).apply(Recorder())


def test_the_refusal_comes_before_the_fetch_in_the_plan() -> None:
    """An arming half done is the failure this path must not have, so a
    machine that cannot boot what would be fetched is refused first."""
    plan = netboot.build(launch=_launch(), target=_target())
    refusal = next(
        n for n, one in enumerate(plan) if isinstance(one, netboot.RefuseWithoutABootMethod)
    )
    fetch = next(n for n, one in enumerate(plan) if isinstance(one, netboot.FetchMemoryImage))
    assert refusal < fetch, [type(one).__name__ for one in plan]


def test_an_earlier_arming_is_taken_back_before_this_run_writes() -> None:
    """A second run that stops at the download otherwise leaves the first
    one's arming in place, and the next reboot — months later, for another
    reason — enters a memory environment carrying a configuration nobody
    meant to install any more."""
    plan = netboot.build(launch=_launch(), target=_target())
    cleared = next(
        n for n, one in enumerate(plan) if isinstance(one, netboot.ClearPreviousArming)
    )
    refused = next(
        n for n, one in enumerate(plan) if isinstance(one, netboot.RefuseWithoutABootMethod)
    )
    fetch = next(n for n, one in enumerate(plan) if isinstance(one, netboot.FetchMemoryImage))
    # After the refusals: a machine this refuses is one whose earlier arming
    # is not this run's to take back.
    assert refused < cleared < fetch, [type(one).__name__ for one in plan]


def test_clearing_unsets_the_one_shot_rather_than_pointing_it_elsewhere() -> None:
    """`bootctl(1)`: an empty ID unsets the variable, and any other value is
    another entry to boot next. This asked for
    `auto-reboot-to-firmware-setup`, which is not a disarm but a machine that
    reboots into its firmware setup."""
    recorder = _answering()
    netboot.ClearPreviousArming(target=_target()).apply(recorder)

    asked = [one for one in recorder.commands if one[0] == "bootctl"]
    assert asked == [("bootctl", "set-oneshot", "")], asked
    assert not any("firmware-setup" in " ".join(one) for one in recorder.commands)


def test_clearing_a_grub_machine_unsets_next_entry() -> None:
    recorder = _answering()
    netboot.ClearPreviousArming(target=_target(BootMethod.BIOS_GRUB)).apply(recorder)

    assert (
        "grub-editenv",
        "/boot/grub/grubenv",
        "unset",
        "next_entry",
    ) in recorder.commands, recorder.commands


def test_the_placed_directory_goes_with_the_arming() -> None:
    """A stale image beside the new one makes the one this plan looks for
    ambiguous, and `_only_image` then refuses a download that succeeded."""
    recorder = _answering()
    netboot.ClearPreviousArming(target=_target()).apply(recorder)

    assert any(
        one[0] == "rm" and str(_target().place) in one for one in recorder.commands
    ), recorder.commands


def test_disarming_and_clearing_ask_for_the_same_thing() -> None:
    """One way of taking an arming back, or the two drift and the operator
    who answers no is left with a machine armed differently from one whose
    run failed."""
    declined = _answering()
    _apply(netboot.disarm(target=_target()), declined)
    cleared = _answering()
    netboot.ClearPreviousArming(target=_target()).apply(cleared)

    assert declined.commands == cleared.commands, (declined.commands, cleared.commands)


#: The architectures Gentoo names, read from `profiles/arch.list` on this
#: machine on 2026-08-18. Held here so the check runs where no repository is
#: mounted.
GENTOO_ARCH_NAMES: frozenset[str] = frozenset({"amd64", "arm64", "x86"})


def test_every_architecture_this_maps_is_one_gentoo_names() -> None:
    """`uname -m` answers `x86_64` and the ISO is published as
    `install-amd64-…`: two ecosystems naming the same machine, the way `fma`
    and `fma3` do. A name Gentoo does not use finds no asset and the failure
    reads as a missing release."""
    assert set(netboot.GENTOO_ARCHITECTURES.values()) <= GENTOO_ARCH_NAMES, sorted(
        set(netboot.GENTOO_ARCHITECTURES.values()) - GENTOO_ARCH_NAMES
    )


def test_the_alpine_index_is_asked_for_this_machine() -> None:
    """Alpine publishes one document per architecture and spells it the way
    the kernel does; `x86_64` and `aarch64` both answer with a netboot flavour
    and a sha256, checked on 2026-08-18."""
    for machine in ("x86_64", "aarch64"):
        assert netboot.ALPINE_RELEASES.format(machine).endswith(
            f"releases/{machine}/latest-releases.yaml"
        ), machine


def test_the_iso_is_chosen_by_the_name_the_release_publishes() -> None:
    """So an architecture the project starts building for works the day it
    does, without this file changing."""
    recorder = _answering()
    netboot.FetchMemoryImage(
        mode=MemoryMode.RAM,
        target=replace(_target(), architecture="x86_64"),
    ).apply(recorder)

    fetched = [one for one in _run(recorder, "curl") if "--output" in one]
    assert fetched[0][-1].endswith(ISO), fetched


def test_an_architecture_the_release_has_no_iso_for_is_named() -> None:
    """`--ram` on a machine the CJK project does not build for is a refusal
    that says which mode is published for it, rather than a 404 an hour in."""
    recorder = _answering()
    with pytest.raises(DownloadFailed, match="arm64 ISO"):
        netboot.FetchMemoryImage(
            mode=MemoryMode.RAM,
            target=replace(_target(), architecture="aarch64"),
        ).apply(recorder)


def test_an_architecture_gentoo_does_not_name_is_refused_by_name() -> None:
    recorder = _answering()
    with pytest.raises(DownloadFailed, match="not an architecture Gentoo names"):
        netboot.FetchMemoryImage(
            mode=MemoryMode.RAM,
            target=replace(_target(), architecture="riscv64"),
        ).apply(recorder)


def test_no_address_carries_an_architecture_of_its_own() -> None:
    """The one place a machine's name is written is the table. A copy inside
    an address is what has to be found and changed the day arm is tested, and
    the one that is missed sends that machine to another machine's image."""
    assert "{}" in netboot.ALPINE_RELEASES, netboot.ALPINE_RELEASES
    for address in (
        netboot.ALPINE_RELEASES,
        netboot.ALPINE_REPOSITORY,
        netboot.CJK_RELEASES,
    ):
        for named in ("x86_64", "amd64", "aarch64", "arm64", "i686", "x86"):
            assert named not in address, (address, named)


def test_the_boot_target_carries_no_fact_nothing_reads() -> None:
    """`esp_disk` and `esp_partition` were computed on every run and read by
    nothing: both GRUBs are armed with `custom.cfg` and `grub-reboot`, so no
    `efibootmgr --create-only` call is composed anywhere. Carrying the two
    facts such a call would need reads as though it were written.

    `test_no_definition_in_the_package_is_unreachable` does not see a dataclass
    field, so this is what holds it."""
    import dataclasses
    import inspect

    source = inspect.getsource(netboot)
    for field in dataclasses.fields(netboot.BootTarget):
        # The declaration itself is one mention; a field nothing reads has
        # exactly that one.
        assert source.count(field.name) > 1, field.name


#: What the CERNET index and one build directory answered on 2026-08-18,
#: trimmed to the lines these two patterns read. The mirror answers 302 to
#: whichever member is closest, so what a machine sees is this after the
#: redirect.
MIRROR_INDEX = (
    '<a href="/">Home</a><a href="../">../</a>\n'
    '<a href="20260810T183054Z/">20260810T183054Z/</a>  10-Aug-2026 18:35  -\n'
    '<a href="20260813T073053Z/">20260813T073053Z/</a>  13-Aug-2026 07:35  -\n'
    '<a href="mailto:cips@nyist.edu.cn">contact</a>\n'
)
MIRROR_BUILD = (
    '<a href="../">../</a>\n'
    '<a href="install-amd64-cjk-minimal-20260813T073053Z.iso">…</a> 943M\n'
    '<a href="install-amd64-cjk-minimal-20260813T073053Z.iso.CONTENTS.gz">…</a>\n'
    '<a href="install-amd64-cjk-minimal-20260813T073053Z.iso.DIGESTS">…</a>\n'
    '<a href="install-amd64-cjk-minimal-20260813T073053Z.iso.sha256">…</a>\n'
)
MIRROR_ISO = "install-amd64-cjk-minimal-20260813T073053Z.iso"


def _mirroring(*, answers: bool = True, digest: str = DIGEST) -> Recorder:
    def answer(argv: Sequence[str]) -> str | None:
        if argv[0] != "curl":
            if argv[0] == "sha256sum":
                return f"{digest}  {argv[1]}\n"
            if argv[0] == "ls":
                return f"{MIRROR_ISO}\nkernel\ninitramfs\n"
            if argv[0] == "blkid":
                return "Gentoo-CJK-amd64-20260813\n"
            return None
        wanted = argv[-1]
        if wanted.startswith(netboot.CJK_MIRROR):
            if not answers:
                return CommandOutput("", 7)
            if wanted.endswith(".sha256"):
                return f"{DIGEST}  {MIRROR_ISO}\n"
            if wanted.rstrip("/").endswith("Z"):
                # Each build lists its own name, so a run that took the wrong
                # directory fetches a URL carrying the wrong timestamp.
                build = wanted.rstrip("/").rsplit("/", 1)[1]
                return MIRROR_BUILD.replace("20260813T073053Z", build)
            return MIRROR_INDEX
        if wanted == netboot.CJK_RELEASES:
            return CJK_INDEX
        if wanted.endswith(".sha256"):
            return f"{DIGEST}  {ISO}\n"
        return ""

    recorder = Recorder()
    recorder.answering = answer
    return recorder


def test_the_iso_comes_from_the_mirror_before_the_release_index() -> None:
    """The machines this path exists for are in China, and a gigabyte from
    GitHub's CDN is the slowest part of the whole run. CERNET answers 302 to
    whichever member mirror is closest."""
    recorder = _mirroring()
    netboot.FetchMemoryImage(mode=MemoryMode.RAM, target=_target()).apply(recorder)

    fetched = [one for one in _run(recorder, "curl") if "--output" in one]
    assert fetched[0][-1].startswith(netboot.CJK_MIRROR), fetched
    assert fetched[0][-1].endswith(MIRROR_ISO), fetched
    # The newest build, not the first the index lists.
    assert "/20260813T073053Z/" in fetched[0][-1], fetched
    # The release index is not asked at all when the mirror answered.
    assert not any(netboot.CJK_RELEASES in one for one in recorder.commands)


def test_the_newest_build_is_taken_rather_than_the_first() -> None:
    """The index lists every build it keeps, oldest first, and the newest is
    the greatest timestamp rather than the last line."""
    builds = netboot.CJK_BUILD.findall(MIRROR_INDEX)
    assert builds == ["20260810T183054Z", "20260813T073053Z"], builds
    assert max(builds) == "20260813T073053Z"


def test_a_mirror_that_does_not_answer_falls_back_to_the_release_index() -> None:
    """A mirror that is down is not a reason to stop: neither route is a
    single point of failure."""
    recorder = _mirroring(answers=False)
    netboot.FetchMemoryImage(mode=MemoryMode.RAM, target=_target()).apply(recorder)

    fetched = [one for one in _run(recorder, "curl") if "--output" in one]
    assert fetched[0][-1] == f"https://host/{ISO}", fetched


def test_only_the_iso_and_its_checksum_are_read_from_a_build() -> None:
    """`DIGESTS` and `CONTENTS.gz` are in the same directory, and a pattern
    that took any href would offer one of them as the image."""
    names = netboot.CJK_ASSET.findall(MIRROR_BUILD)
    assert names == [MIRROR_ISO, f"{MIRROR_ISO}.sha256"], names


def _payload_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The generated first screen, written where a shell can be told to run it.

    The script is generated by the real function with only its own directory
    constant moved, so what runs here is what the machine runs.
    """
    where = tmp_path / "gentoo-install"
    where.mkdir()
    monkeypatch.setattr(netboot, "PAYLOAD", str(where))
    (where / "start.sh").write_text(netboot._start(), encoding="utf-8")
    (where / "config.toml").write_text("x = 1\n", encoding="utf-8")
    (where / "bootstrap.sh").write_text(
        "#!/bin/sh\nprintf 'BOOTSTRAP %s\\n' \"$*\"\n", encoding="utf-8"
    )
    (where / "bootstrap.sh").chmod(0o755)
    return where


def _sourced(where: Path, answer: str) -> str:
    """Run the first screen the way `.bash_profile` does, and say what came out."""
    import subprocess

    return subprocess.run(
        ["bash", "-c", f". {where}/start.sh; echo LOGIN-SHELL-ALIVE"],
        input=f"{answer}\n",
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def test_the_first_screen_asks_and_leaves_the_login_shell_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is sourced by `.bash_profile`, not run by a service, so `exit` would
    end the operator's own shell and `exec` would replace it with the
    installer and leave them with none when it finishes."""
    where = _payload_at(tmp_path, monkeypatch)

    said = _sourced(where, "install")
    assert "install or shell>" in said, said
    assert "BOOTSTRAP" in said and "--config" in said, said
    assert "LOGIN-SHELL-ALIVE" in said, said


def test_any_other_answer_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two answers and no timeout, and everything that is not the install is
    the rescue shell."""
    where = _payload_at(tmp_path, monkeypatch)

    for answer in ("shell", "", "no", "INSTALL"):
        said = _sourced(where, answer)
        assert "nothing was changed" in said, (answer, said)
        assert "BOOTSTRAP" not in said, (answer, said)
        assert "LOGIN-SHELL-ALIVE" in said, (answer, said)


def test_the_first_screen_is_hung_on_the_login_shell_not_a_boot_service() -> None:
    """OpenRC's `local` runs `local.d/*.start` with `> /dev/null 2>&1` unless
    `rc_verbose` is set — read from the CJK medium's own `/etc/init.d/local` —
    and it runs during boot with no controlling terminal. A question printed
    there is invisible and the `read` beside it answers itself."""
    assert netboot.AUTOSTART == "/root/.bash_profile", netboot.AUTOSTART
    assert "local.d" not in netboot._handover(), netboot._handover()
    # Appended: the file exists on the medium and sources `.bashrc`.
    assert f'>> "$NEWROOT{netboot.AUTOSTART}"' in netboot._handover()


def test_a_missing_payload_does_not_end_the_operators_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.bash_profile` sources this, so `exit` there ends the login shell and
    drops the operator out of the session they just reconnected to. The
    payload is missing exactly when something went wrong and they most need a
    shell."""
    where = _payload_at(tmp_path, monkeypatch)
    script = where / "start.sh"
    kept = script.read_text(encoding="utf-8")
    for one in where.iterdir():
        if one != script:
            one.unlink()
    # The directory the script changes into is gone; the script itself is
    # still where `.bash_profile` sources it from.
    monkeypatch.setattr(netboot, "PAYLOAD", str(tmp_path / "absent"))
    script.write_text(kept.replace(str(where), str(tmp_path / "absent")), encoding="utf-8")

    said = _sourced(where, "install")
    assert "LOGIN-SHELL-ALIVE" in said, said
    assert "BOOTSTRAP" not in said, said


def test_the_image_is_not_written_where_the_firmware_reads() -> None:
    """The first real `--lowram` run ended at `curl: (23) Failure writing
    output to destination` with 111 MB of a 373 MB archive written: the esp on
    a Debian cloud image is 124 MiB, and it is whatever size that machine was
    given. The firmware only ever reads a kernel and an initramfs from it."""
    recorder = _answering(
        MemoryMode.LOWRAM,
        digest="9a7769ea8fa1737b1b49d82f1bdd53d0a17338d6d3b7cfc6f2c3ec5158596d8b",
    )
    netboot.FetchMemoryImage(mode=MemoryMode.LOWRAM, target=_target()).apply(recorder)

    written = [one for one in _run(recorder, "curl") if "--output" in one]
    assert len(written) == 1, written
    output = written[0][written[0].index("--output") + 1]
    assert output.startswith(f"/{netboot.PLACE}/"), output
    assert not output.startswith(ESP), output


def test_the_kernel_still_lands_where_the_firmware_reads() -> None:
    """The other half of the same rule: two files of tens of megabytes go to
    the esp, because that is the only filesystem the firmware reads."""
    recorder = _answering(MemoryMode.LOWRAM)
    netboot.PlaceMemoryKernel(mode=MemoryMode.LOWRAM, target=_target()).apply(recorder)

    unpacked = [one for one in _run(recorder, "tar") if "--directory" in one]
    assert len(unpacked) == 2, unpacked
    for argv in unpacked:
        assert argv[argv.index("--directory") + 1] == f"{ESP}/{netboot.PLACE}", argv


def test_the_lowram_archive_is_deleted_after_the_entry_has_read_its_name() -> None:
    """`modloop=` names a URL rather than this file, so nothing reads it again
    once the entry is written, and it is 373 MB on the root filesystem of a
    machine about to reboot into memory. Deleting it any earlier ended a run
    with `/gentoo-install-ram holds 0 files ending .tar.gz`: the entry composes
    that URL from this file's name."""
    operations = netboot.build(
        launch=_launch(MemoryMode.LOWRAM), target=_target(), configuration="x"
    )
    kinds = [type(one).__name__ for one in operations]

    assert "DiscardTheArchive" in kinds, kinds
    assert kinds.index("WriteMemoryEntry") < kinds.index("DiscardTheArchive"), kinds

    recorder = _answering(MemoryMode.LOWRAM)
    netboot.DiscardTheArchive().apply(recorder)
    removed = [one for one in _run(recorder, "rm") if any(".tar.gz" in a for a in one)]
    assert len(removed) == 1, recorder.commands


def test_the_ram_iso_is_never_discarded() -> None:
    """`iso-scan/filename` names it, so the machine reads it at boot."""
    operations = netboot.build(
        launch=_launch(MemoryMode.RAM), target=_target(), configuration="x"
    )

    assert "DiscardTheArchive" not in [type(one).__name__ for one in operations]


def test_the_ram_image_stays_where_iso_scan_will_look_for_it() -> None:
    """`iso-scan/filename` is why the ISO does not have to be on the esp: it
    mounts each `by-uuid` device and looks for that path inside it."""
    entry = _ram_entry(RUNNING_CMDLINE)
    word = next(one for one in entry.split() if one.startswith("iso-scan/filename="))

    assert word.endswith(f"/{netboot.PLACE}/{ISO}"), word
    assert ESP not in word, word
    removed = [one for one in entry.split() if one.startswith("rm")]
    assert not removed, "the ISO is read at boot and is not deleted here"


def test_the_kernel_is_unpacked_without_the_ownership_it_was_stored_with() -> None:
    """Alpine stores these as uid 1000, GNU tar restores ownership when it runs
    as root, and the esp is vfat and cannot express it: the first run to reach
    this step answered `tar: kernel: Cannot change ownership to uid 1000, gid
    1000: Operation not permitted` and exited 2."""
    recorder = _answering(MemoryMode.LOWRAM)
    netboot.PlaceMemoryKernel(mode=MemoryMode.LOWRAM, target=_target()).apply(recorder)

    unpacked = [one for one in _run(recorder, "tar") if "--extract" in one]
    assert len(unpacked) == 2, unpacked
    for argv in unpacked:
        assert "--no-same-owner" in argv, argv
        assert "--no-same-permissions" in argv, argv


def test_the_payload_copy_restores_no_ownership_either() -> None:
    """The tree goes onto the esp beside the kernel, and vfat has no owners:
    the copy ended `tar: gentoo_install/cli.py: Cannot change ownership to uid
    1000, gid 1000: Operation not permitted` with the payload half written."""
    recorder = _answering()
    netboot.AppendConfiguration(
        target=_target(),
        launch=_launch(),
        configuration="x",
        source="/mnt/driver",
    ).apply(recorder)

    piped = [one for one in recorder.commands if any("tar --create" in a for a in one)]
    assert len(piped) == 1, recorder.commands
    line = " ".join(piped[0])
    assert "--no-same-owner" in line, line
    assert "--no-same-permissions" in line, line
