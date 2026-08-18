# SPDX-License-Identifier: GPL-2.0-or-later
"""The installed-state contract shared by local and cluster runners."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from gentoo_install.data import load_catalog
from gentoo_install.model import compat
from gentoo_install.model.config import (
    Bootloader,
    DiskMode,
    InitSystem,
    InstallConfig,
    Networking,
)
from gentoo_install.model.device import Filesystem, Luks, Mountpoint, Subvolume, ZfsDataset, ZfsPool
from gentoo_install.plan.system import _network_service as network_service

from .console import DISK_PASSPHRASE


@dataclass(frozen=True)
class InstalledCheck:
    """One command and the output pattern that proves its result."""

    name: str
    command: str
    pattern: str


#: Read on the installed machine, against the tree that machine has: every
#: `CPU_FLAGS_X86` value portage accepts is one line of
#: `profiles/desc/cpu_flags_x86.desc`, so a name that is not there is a name
#: portage does not know. `vaes` was written for three weeks and is a cpuinfo
#: flag with no portage counterpart at all; nothing in the unit tests could
#: see it, because the authority is a file only the installed system has.
#:
#: The comparison against `cpuid2cpuflags` is printed when the tool is there
#: and is not what the check passes on: the installer does not merge it, so a
#: pattern accepting its absence would be a rule that cannot fail.
CPU_FLAGS_COMMAND: str = (
    "desc=$(portageq get_repo_path / gentoo)/profiles/desc/cpu_flags_x86.desc; "
    "unknown=''; "
    "for one in $(portageq envvar CPU_FLAGS_X86); do "
    'grep -q "^$one - " "$desc" || unknown="$unknown $one"; '
    "done; "
    'if [ -n "$unknown" ]; then printf \'CPUFLAGS-UNKNOWN:%s\\n\' "$unknown"; '
    "else echo CPUFLAGS-ALL-KNOWN; fi; "
    "command -v cpuid2cpuflags >/dev/null 2>&1 && "
    "printf 'CPUFLAGS-TOOL %s\\n' \"$(cpuid2cpuflags)\" || true"
)


def checks(installation: InstallConfig) -> tuple[InstalledCheck, ...]:
    """Derive every installed-state check from the configuration."""
    result = [
        InstalledCheck("os-release", "cat /etc/os-release", "Gentoo"),
        InstalledCheck("mounts", "findmnt --noheadings --list --output TARGET,SOURCE,FSTYPE", "/"),
        InstalledCheck("locale", "locale", f"LANG={installation.system.locale}"),
        InstalledCheck("hostname", _hostname_command(installation), _hostname_pattern(installation)),
        InstalledCheck("kernel", "uname -r; find /boot -maxdepth 4 -type f "
                       r"\( -name 'vmlinuz*' -o -name 'kernel-*' -o -name linux -o -name '*.conf' \) | sort",
                       _kernel_pattern(installation)),
        InstalledCheck("resolver", "readlink -f /etc/resolv.conf; test -s /etc/resolv.conf && echo RESOLVCONF-OK || echo RESOLVCONF-EMPTY", "RESOLVCONF-OK"),
        InstalledCheck("portage", "emerge --info >/dev/null 2>&1 && echo EMERGE-OK || echo EMERGE-FAIL", "EMERGE-OK"),
        InstalledCheck("cpu-flags", CPU_FLAGS_COMMAND, "CPUFLAGS-ALL-KNOWN"),
        InstalledCheck("init", "test -d /run/openrc && echo openrc || { test -d /run/systemd/system && echo systemd || echo unknown; }", "systemd" if installation.system.init is InitSystem.SYSTEMD else "openrc"),
    ]
    if installation.system.init is InitSystem.SYSTEMD:
        result.extend([InstalledCheck("units", "systemctl list-unit-files --state=enabled --no-legend --no-pager", "enabled"), InstalledCheck("failed", "test -z \"$(systemctl --failed --no-legend --no-pager)\" && echo NO-FAILED-UNITS", "NO-FAILED-UNITS")])
    else:
        result.extend([InstalledCheck("units", "rc-update show default", "default"), InstalledCheck("failed", "test -z \"$(rc-status --crashed)\" && echo NO-FAILED-UNITS", "NO-FAILED-UNITS")])
    if installation.system.networking is not Networking.NONE:
        result.append(InstalledCheck("network", "systemctl list-unit-files --state=enabled --no-legend --no-pager; rc-update show default", re.escape(network_service(installation.system))))
    groups = load_catalog()
    frameworks = {
        groups[name].input_framework
        for name in installation.packages.applications
        if name in groups and groups[name].input_method
    }
    for framework in sorted(one for one in frameworks if one):
        # What the environment file actually carries. `DefaultIM=` was asserted
        # here for three weeks and lives in the user's `fcitx5/profile`, so the
        # check could only ever fail; `vm-desktop` was the first fixture to
        # reach it and it did have a working input method.
        result.append(
            InstalledCheck(
                "inputmethod",
                "cat /etc/environment /etc/env.d/90input-method 2>/dev/null",
                re.escape(f"XMODIFIERS=@im={framework}"),
            )
        )
    if (
        installation.bootloader.kind is Bootloader.ZFSBOOTMENU
        and installation.kernel.remote_unlock.enabled
    ):
        # ZFSBootMenu unlocks the pool from its own image, not the system
        # initramfs, so the only place the ssh daemon can live is that EFI
        # file. `zbm-unlock` failed three times saying nothing but that a
        # forwarded port went unanswered.
        # The acl, not the daemon: `grep -ci dropbear` counts the `etc/dropbear`
        # host-key paths and answered 3 on an image that authenticates nobody.
        result.append(
            InstalledCheck(
                "zbm unlock key",
                "lsinitrd /efi/EFI/zbm/*.EFI 2>/dev/null "
                "| grep -cE 'authorized_keys|dropbear-start'",
                r"[1-9]",
            )
        )
    if installation.disk.mode is DiskMode.IN_PLACE:
        # No graph to derive from: the layout belongs to the machine that was
        # converted, and the esp and root filesystem are whatever it already
        # had. `fstab` still has to name the root by UUID, which is the part
        # the conversion writes.
        result.append(InstalledCheck("fstab", "cat /etc/fstab", "UUID="))
        return tuple(result)
    graph = installation.disk.graph
    esp = compat.esp_mount(graph)
    if esp is not None:
        result.append(InstalledCheck("esp", f"findmnt --noheadings --output TARGET,SOURCE,FSTYPE {esp.path}", str(esp.path)))
    root = graph[installation.disk.root]
    source = graph[root.source] if isinstance(root, Mountpoint) else root
    if isinstance(source, ZfsDataset):
        result.append(InstalledCheck("root filesystem", "findmnt --noheadings --output FSTYPE /", "zfs"))
    else:
        filesystem = graph[source.filesystem] if isinstance(source, Subvolume) else source
        kind = filesystem.kind.value if isinstance(filesystem, Filesystem) else ""
        result.append(InstalledCheck("root filesystem", "findmnt --noheadings --output FSTYPE /", kind))
    if not isinstance(source, ZfsDataset):
        result.append(InstalledCheck("fstab", "cat /etc/fstab", "UUID="))
    return tuple(result)


def _hostname_command(installation: InstallConfig) -> str:
    """Read the init system's hostname file."""
    return "cat /etc/hostname" if installation.system.init is InitSystem.SYSTEMD else "cat /etc/conf.d/hostname"


def _hostname_pattern(installation: InstallConfig) -> str:
    """Match the init system's hostname representation."""
    return installation.system.hostname


def _kernel_pattern(installation: InstallConfig) -> str:
    """Match the selected bootloader's kernel layout."""
    if installation.bootloader.kind is Bootloader.SYSTEMD_BOOT:
        return "/boot/"
    return "/boot/"


def stage_passphrase_commands(installation: InstallConfig) -> tuple[str, ...]:
    """Return commands that stage the harness passphrase for encrypted nodes."""
    graph = installation.disk.graph
    paths = [node.passphrase_file for node in graph.of_type(Luks) if node.passphrase_file]
    paths += [node.passphrase_file for node in graph.of_type(ZfsPool) if node.passphrase_file]
    commands: list[str] = []
    for source in paths:
        parent = PurePosixPath(source).parent
        commands.extend((f"mkdir -p {parent} && chmod 700 {parent}", f"printf '%s' '{DISK_PASSPHRASE}' > {source}", f"chmod 600 {source}"))
    return tuple(commands)
