# SPDX-License-Identifier: GPL-2.0-or-later
"""What each session is asked to build, in the words an operator would use.

Not a configuration file. Every install fixture hands the installer a `.toml`
and the interface is never read; these are sentences, and finding the rows
that answer them is the whole test. A spec that named `disk.filesystem` would
be telling the agent where to look, which is the question being asked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Spec:
    """One machine to build, and how to tell whether it was."""

    number: int
    #: What the operator wants, as they would say it.
    wanted: str
    #: What the installed machine must show for the run to have answered the
    #: spec. Read off the machine after it boots, never from the agent.
    proof: tuple[str, ...]


#: Every password in every spec. One value, because a test that fails on a
#: mistyped password says nothing about the interface.
PASSWORD: Final[str] = "testtest"

SPECS: Final[dict[int, Spec]] = {
    1: Spec(
        number=1,
        wanted=(
            "Install Gentoo onto the whole 40 GiB disk. The machine boots with "
            "UEFI and should use GRUB. Use btrfs for the root filesystem. The "
            "system should come up in Traditional Chinese with a CJK font, in "
            "the Asia/Taipei timezone, and be called lab1. Set the root "
            f"password to {PASSWORD}. Add a user called zakk with the same "
            "password who can use sudo. No desktop environment."
        ),
        proof=(
            "hostname is lab1",
            "the root filesystem is btrfs",
            "the bootloader is grub and the firmware is uefi",
            "/etc/localtime points at Asia/Taipei",
            "zh_TW.UTF-8 is in /etc/locale.gen",
            "zakk exists and is in the wheel group",
        ),
    ),
    2: Spec(
        number=2,
        wanted=(
            "This machine has two disks. Install onto them with the root "
            f"filesystem encrypted, using the passphrase {PASSWORD}. Give "
            "/home a partition of its own. Use ext4. The machine boots with "
            "BIOS, not UEFI. Use OpenRC rather than systemd. The system should "
            "come up in Simplified Chinese, timezone Asia/Shanghai. Set the "
            f"root password to {PASSWORD}."
        ),
        proof=(
            "the root filesystem sits on a LUKS container",
            "/home is a separate filesystem",
            "the root filesystem is ext4",
            "the init system is openrc",
            "zh_CN.UTF-8 is in /etc/locale.gen",
        ),
    ),
    3: Spec(
        number=3,
        wanted=(
            "This machine is already running a system. Replace it with Gentoo "
            "in place, keeping everything under /home. Set the root password "
            f"to {PASSWORD}."
        ),
        proof=(
            "the machine boots into Gentoo",
            "the files that were under /home are still there",
        ),
    ),
    4: Spec(
        number=4,
        wanted=(
            "Move the installer into memory first, then install onto the disk "
            f"from there. Set the root password to {PASSWORD}."
        ),
        proof=(
            "the installer ran from a memory-held medium",
            "the machine boots into Gentoo",
        ),
    ),
}
