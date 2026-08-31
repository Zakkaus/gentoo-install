# SPDX-License-Identifier: GPL-2.0-or-later
"""The installed-state contract shared by local and cluster runners."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from gentoo_install.data import load_catalog
from gentoo_install.model import compat
from gentoo_install.model.config import (
    Bootloader,
    DiskMode,
    InitSystem,
    InstallConfig,
    Networking,
)
from gentoo_install.model.device import (
    Filesystem,
    Luks,
    Mountpoint,
    Subvolume,
    ZfsDataset,
    ZfsPool,
    ZfsTopology,
)
from gentoo_install.plan.mounts import ResolvedMount, resolve_mounts
from gentoo_install.plan.packages import ENVIRONMENT_FILE, input_environment
from gentoo_install.model.sshkey import fingerprint
from gentoo_install.plan.system import _network_service as network_service
from gentoo_install.plan.system import key_accounts
from gentoo_install.plan.system import _sshd_service as sshd_service

from .console import DISK_PASSPHRASE


@dataclass(frozen=True)
class InstalledCheck:
    """One command and the output pattern that proves its result."""

    name: str
    command: str
    pattern: str
    # Compound checks must name the relation that prevents a single-line match.
    line_boundary_exception: str | None = None

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


INPUT_METHOD_BINARIES: Final[dict[str, str]] = {
    "fcitx": "fcitx5",
    "ibus": "ibus-daemon",
}


#: The root entry, not any `UUID=` in the file. Every layout writes an esp
#: line by UUID as well, so `UUID=` alone was satisfied by a machine whose
#: root was still named by device. The separator is a tab on every guest read.
ROOT_BY_UUID: Final[str] = r"(?m)^UUID=\S+[ \t]+/[ \t]+\S+[ \t]+[^\n]*$"


def checks(installation: InstallConfig) -> tuple[InstalledCheck, ...]:
    """Derive every installed-state check from the configuration."""
    result = [
        InstalledCheck("os-release", "cat /etc/os-release", r"(?m)^ID=['\"]?gentoo['\"]?$"),
        # A layout is a set of mount lines; one matching line cannot prove all
        # configured targets are present.
        InstalledCheck(
            "mounts",
            "findmnt --noheadings --list --output TARGET,SOURCE,FSTYPE",
            _mount_pattern(installation),
            "requires every configured mount line",
        ),
        InstalledCheck(
            "locale",
            "locale",
            rf"(?m)^LANG={re.escape(installation.system.locale)}$",
        ),
        # Nothing checked timezone until wrong-zone machines passed. libc reads
        # `localtime`, so it and `/etc/timezone` must agree.
        InstalledCheck(
            "timezone",
            "readlink -f /etc/localtime; cat /etc/timezone 2>/dev/null",
            rf"(?m)^(?:/usr/share/zoneinfo/)?{re.escape(installation.system.timezone)}$"
            rf"\n^{re.escape(installation.system.timezone)}$",
        ),
        InstalledCheck("hostname", _hostname_command(installation), _hostname_pattern(installation)),
        # The release has to equal a boot filename, so this relation spans two lines.
        InstalledCheck(
            "kernel",
            "uname -r; find /boot -maxdepth 4 -type f "
            r"\( -name 'vmlinuz*' -o -name 'kernel-*' -o -name linux -o -name '*.conf' \) | sort",
            KERNEL_MATCHES_ITS_RELEASE,
            "joins a running release to a boot path",
        ),
        InstalledCheck(
            "resolver",
            "readlink -f /etc/resolv.conf; test -s /etc/resolv.conf "
            "&& echo RESOLVCONF-OK || echo RESOLVCONF-EMPTY",
            r"(?m)^RESOLVCONF-OK$",
        ),
        InstalledCheck(
            "portage",
            "emerge --info >/dev/null 2>&1 && echo EMERGE-OK || echo EMERGE-FAIL",
            r"(?m)^EMERGE-OK$",
        ),
        InstalledCheck("cpu-flags", CPU_FLAGS_COMMAND, r"(?m)^CPUFLAGS-ALL-KNOWN$"),
        InstalledCheck(
            "init",
            "test -d /run/openrc && echo openrc || { test -d /run/systemd/system "
            "&& echo systemd || echo unknown; }",
            rf"(?m)^{'systemd' if installation.system.init is InitSystem.SYSTEMD else 'openrc'}$",
        ),
    ]
    if installation.system.init is InitSystem.SYSTEMD:
        result.extend(
            [
                InstalledCheck(
                    "units",
                    "systemctl list-unit-files --state=enabled --no-legend --no-pager",
                    r"(?m)^\S+[ \t]+enabled(?:[ \t]+\S+)?$",
                ),
                InstalledCheck(
                    "failed",
                    "test -z \"$(systemctl --failed --no-legend --no-pager)\" "
                    "&& echo NO-FAILED-UNITS",
                    r"(?m)^NO-FAILED-UNITS$",
                ),
            ]
        )
    else:
        result.extend(
            [
                InstalledCheck(
                    "units",
                    "rc-update show default",
                    r"(?m)^[ \t]*\S+[ \t]*\|[ \t]*default$",
                ),
                InstalledCheck(
                    "failed",
                    "test -z \"$(rc-status --crashed)\" && echo NO-FAILED-UNITS",
                    r"(?m)^NO-FAILED-UNITS$",
                ),
            ]
        )
    if installation.system.networking is not Networking.NONE:
        service = re.escape(network_service(installation.system))
        result.append(
            InstalledCheck(
                "network",
                "systemctl list-unit-files --state=enabled --no-legend --no-pager; "
                "rc-update show default",
                rf"(?m)^(?:{service}\.service[ \t]+enabled(?:[ \t]+\S+)?|"
                rf"[ \t]*{service}[ \t]*\|[ \t]*default)$",
            )
        )
    if installation.system.addresses:
        result.extend(
            InstalledCheck(
                f"address {address}",
                "ip -o address show",
                rf"(?m)^\d+:[ \t]+\S+[ \t]+inet6?[ \t]+{re.escape(address)}"
                rf"(?=[ \t/])[^\n]*$",
            )
            for address in installation.system.addresses
        )
        result.extend(
            InstalledCheck(
                f"default route {gateway}",
                "ip -4 route show default; ip -6 route show default",
                rf"(?m)^default[ \t]+via[ \t]+{re.escape(gateway)}[ \t]+[^\n]*$",
            )
            for gateway in installation.system.gateways
        )
        if installation.system.dns and installation.system.init is InitSystem.SYSTEMD:
            result.extend(
                InstalledCheck(
                    f"resolver {resolver}",
                    "resolvectl dns",
                    rf"(?m)^(?=[^\n]*(?<!\S){re.escape(resolver)}(?!\S))[^\n]+$",
                )
                for resolver in installation.system.dns
            )
    for user in installation.system.users:
        # The account the machine holds, not the operation that asked for it:
        # `btrfs-luks` names a user with three groups and a shell, and its
        # console log never mentioned the name.
        shell = re.escape(user.shell) if user.shell else "[^:]*"
        result.append(
            InstalledCheck(
                f"user {user.name}",
                f"getent passwd {user.name}",
                rf"(?m)^{re.escape(user.name)}:[^:]*:\d+:\d+:[^:]*:[^:]*:{shell}$",
            )
        )
        if user.groups:
            # `id -nG` returns one space-delimited line, so every configured
            # group must be a complete token on that same line.
            group_patterns = "".join(
                rf"(?=[^\n]*(?<!\S){re.escape(one)}(?!\S))" for one in user.groups
            )
            result.append(
                InstalledCheck(
                    f"user {user.name} groups",
                    f"id -nG {user.name}",
                    rf"(?m)^{group_patterns}[^\n]+$",
                )
            )
    if installation.system.sshd:
        # Sixteen fixtures ask for sshd and nothing asked the machine for it.
        # `UnitFileState` rather than `is-enabled`: `systemctl` colours its
        # output on a console that is a terminal, and the serial console is
        # one, so `list-unit-files` prints `\x1b[0;4msshd.service`.
        if installation.system.init is InitSystem.SYSTEMD:
            result.append(
                InstalledCheck(
                    "sshd",
                    f"systemctl show --property=UnitFileState --value {sshd_service()}.service",
                    r"(?m)^enabled$",
                )
            )
        else:
            result.append(
                InstalledCheck(
                    "sshd",
                    "rc-update show default",
                    rf"(?m)^[ \t]*{sshd_service()}[ \t]*\|[ \t]*default$",
                )
            )
    if installation.system.authorized_keys and installation.system.sshd:
        # The file that decides who can log in, read back from the machine
        # rather than assumed from the plan. `ssh-keygen -l` computes the
        # fingerprint on the guest, so the answer cannot come from the echo of
        # the question, and `stat` answers with the modes sshd requires:
        # sshd ignores an `authorized_keys` group or world writable, and says
        # so only in its own log.
        for name, home in key_accounts(
            installation.system, installation.kernel.remote_unlock.enabled
        ):
            path = f"{home}/.ssh/authorized_keys"
            required = [
                re.escape(fingerprint(key))
                for key in installation.system.authorized_keys
            ]
            required.append(rf"ACL 600 {re.escape(path)}")
            required.append(rf"ACL 700 {re.escape(home)}/\.ssh")
            required.append(rf"ACL [0-7]{{3}} {re.escape(home)}$")
            result.append(
                InstalledCheck(
                    f"authorized keys {name}",
                    # The home directory as well as the two below it: a
                    # missing `.ssh` and a missing `/home/<user>` are different
                    # failures, and the first message could not tell them apart.
                    f"ssh-keygen -lf {path}; stat -c 'ACL %a %n' {path} {home}/.ssh {home}",
                    "(?s)" + "".join(f"(?=.*{one})" for one in required),
                    line_boundary_exception="requires every fingerprint and both modes",
                )
            )
    datasets = tuple(installation.disk.graph.of_type(ZfsDataset))
    if datasets:
        # Every dataset the configuration declares, read off the machine with
        # the mountpoint it declared. A pool whose child dataset is absent, or
        # mounted somewhere else, produces a system that boots and quietly
        # holds none of what was written into that dataset.
        pools = {
            one.id: one.name for one in installation.disk.graph.of_type(ZfsPool)
        }
        declared = [
            rf"^{re.escape(f'{pools[one.pool]}/{one.name}')}\s"
            for one in datasets
            if one.pool in pools
        ]
        result.append(
            InstalledCheck(
                "datasets",
                "zfs list -H -o name,used,mountpoint",
                "(?ms)" + "".join(f"(?=.*{one})" for one in declared),
                line_boundary_exception="requires one line per configured dataset",
            )
        )
    if installation.system.zram is not None:
        # The device the machine brought up, not the file the installer wrote:
        # `zram-generator` and `zram-init` each read a configuration that can
        # be present on a machine with no zram device at all.
        result.append(
            InstalledCheck(
                "zram",
                "swapon --show=NAME --noheadings; zramctl --noheadings --output NAME",
                r"(?m)^/dev/zram0$",
            )
        )
    # Every display manager, not `greetd` alone: the KDE guest of 2026-09-01
    # asked for none, so `sddm` arrived as a dependency of `plasma-meta`, its
    # unit stayed `disabled` under a default target of `graphical.target`, and
    # the machine drew a text login with the whole desktop installed.
    if installation.packages.display_manager:
        greeter = installation.packages.display_manager
        if installation.system.init is InitSystem.SYSTEMD:
            result.append(
                InstalledCheck(
                    "greeter service",
                    f"systemctl is-enabled {greeter}.service; "
                    "systemctl is-active display-manager.service",
                    r"(?m)^enabled$\n^active$",
                    "requires enabled and active state lines",
                )
            )
        else:
            result.append(
                InstalledCheck(
                    "greeter service",
                    f"rc-update show default; pgrep -x {greeter}",
                    r"(?ms)(?=.*^display-manager\s+\|\s+default$)(?=.*^[1-9][0-9]*$)",
                    "requires default service and process lines",
                )
            )
    if installation.packages.display_manager == "greetd":
        result.append(
            InstalledCheck(
                "greetd config",
                "cat /etc/greetd/config.toml",
                # The refusal is a `command` line running agreety, not the
                # word: greetd 0.10.3 ships the comment "`agreety` is the
                # bundled agetty/login-lookalike" two lines above the command,
                # and the ebuild's patch leaves it there, so a check for the
                # word alone could never pass.
                r"(?ms)\A(?!.*^\s*command\s*=\s*\"agreety)"
                r"(?=.*^command = \"tuigreet .*--sessions /usr/share/wayland-sessions"
                r" --xsessions /usr/share/xsessions\"$)",
                "requires constraints across greetd config lines",
            )
        )
    groups = load_catalog()
    frameworks = {
        groups[name].input_framework
        for name in installation.packages.applications
        if name in groups and groups[name].input_method
    }
    for framework in sorted(one for one in frameworks if one):
        binary = INPUT_METHOD_BINARIES[framework]
        environment = ENVIRONMENT_FILE[installation.system.init]
        wanted = (f"/usr/bin/{binary}", *input_environment(installation, groups))
        pattern = "(?ms)" + "".join(
            rf"(?=.*^{re.escape(line)}$)" for line in wanted
        )
        result.append(
            InstalledCheck(
                "inputmethod",
                f"command -v {binary}; cat {environment}",
                pattern,
                "requires the binary and every environment line",
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
                # The whole line, not a digit anywhere in it: `[1-9]` matches
                # the `2` of `2>/dev/null`, so a reader that kept the echo
                # would accept an image that authenticates nobody.
                r"(?m)^[1-9][0-9]*$",
            )
        )
    if installation.disk.mode is DiskMode.IN_PLACE:
        # No graph to derive from: the layout belongs to the machine that was
        # converted, and the esp and root filesystem are whatever it already
        # had. `fstab` still has to name the root by UUID, which is the part
        # the conversion writes.
        result.append(InstalledCheck("fstab", "cat /etc/fstab", ROOT_BY_UUID))
        return tuple(result)
    graph = installation.disk.graph
    esp = compat.esp_mount(graph)
    if esp is not None:
        # The type as well as the path: the path is the argument `findmnt` was
        # given, so the check said only that something was mounted there,
        # while an esp the firmware cannot read is one that is not vfat.
        result.append(
            InstalledCheck(
                "esp",
                f"findmnt --noheadings --output TARGET,SOURCE,FSTYPE {esp.path}",
                rf"(?m)^{re.escape(str(esp.path))}\s+\S+\s+vfat$",
            )
        )
    root = graph[installation.disk.root]
    source = graph[root.source] if isinstance(root, Mountpoint) else root
    if isinstance(source, ZfsDataset):
        result.append(
            InstalledCheck(
                "root filesystem",
                "findmnt --noheadings --output FSTYPE /",
                r"(?m)^zfs$",
            )
        )
        pool = graph[source.pool]
        if isinstance(pool, ZfsPool) and pool.topology is not ZfsTopology.STRIPE:
            # The vdev line `zpool status` draws, which is the pool's own
            # answer: a raidz that `zpool create` built as a stripe carries
            # the dataset, mounts, boots and passes every other check here.
            result.append(
                InstalledCheck(
                    "pool topology",
                    f"zpool status {pool.name}",
                    rf"(?m)^[ \t]+{pool.topology.value}-\d+[ \t]+[^\n]*$",
                )
            )
    else:
        filesystem = graph[source.filesystem] if isinstance(source, Subvolume) else source
        kind = filesystem.kind.value if isinstance(filesystem, Filesystem) else ""
        result.append(InstalledCheck("root filesystem", "findmnt --noheadings --output FSTYPE /", rf"(?m)^{kind}$"))
    if not isinstance(source, ZfsDataset):
        result.append(InstalledCheck("fstab", "cat /etc/fstab", ROOT_BY_UUID))
    return tuple(result)


def _mount_pattern(installation: InstallConfig) -> str:
    """Require every configured mount, or the root of an unprobed conversion."""
    if installation.disk.layout_is_read_from_the_machine:
        # The initial in-place configuration has no graph, but conversion still needs
        # its root mounted.
        return r"(?m)^/\s+\S+\s+\S+$"
    mounts = resolve_mounts(installation.disk.graph)
    if not mounts:
        # Only unprobed in-place configurations lack mountpoints; no planned mount
        # must pass.
        return r"(?!)"
    return "(?ms)" + "".join(_mount_line_pattern(mount) for mount in mounts)


def _mount_line_pattern(mount: ResolvedMount) -> str:
    """Require one `findmnt` line with the mount's configured filesystem type."""
    if mount.dataset is not None:
        filesystem_type = "zfs"
    elif mount.filesystem_kind is not None:
        filesystem_type = mount.filesystem_kind.value
    else:
        raise ValueError(
            f"resolved mount {mount.path} has neither a filesystem type nor a ZFS dataset"
        )
    return (
        rf"(?=.*^{re.escape(str(mount.path))}\s+\S+\s+"
        rf"{re.escape(filesystem_type)}$)"
    )


def _hostname_command(installation: InstallConfig) -> str:
    """Read the init system's hostname file."""
    return "cat /etc/hostname" if installation.system.init is InitSystem.SYSTEMD else "cat /etc/conf.d/hostname"


def _hostname_pattern(installation: InstallConfig) -> str:
    """Match the init system's hostname representation.

    Both branches answered the bare name, which is a substring of the openrc
    file's `hostname="name"` and of any longer name installed by mistake.
    """
    name = re.escape(installation.system.hostname)
    if installation.system.init is InitSystem.SYSTEMD:
        return rf"(?m)^{name}$"
    return rf'(?m)^hostname="{name}"$'


#: The release `uname -r` printed, and a `/boot` file whose own name carries
#: it. Both branches of the layout rule answered `/boot/`, which any file
#: under `/boot` satisfies while the release the machine is running is read
#: and never compared: `grub` and `systemd-boot` alike passed on a stale
#: kernel. The release is a backreference, so nothing but the machine's own
#: answer can satisfy it. Measured against fourteen guests: `kernel-<release>`
#: (dist-bin), `vmlinuz-<release>` (BIOS), and systemd-boot's
#: `<machine-id>/<release>/linux` all carry it.
KERNEL_MATCHES_ITS_RELEASE: Final[str] = r"(?ms)\A\s*(?P<release>[0-9]\S*)$.*^/boot/\S*(?P=release)"


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
