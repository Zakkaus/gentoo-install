# SPDX-License-Identifier: GPL-2.0-or-later
"""How much of a machine a configuration will want, for both runners.

The two runners spend the same resource on the same install and each held its
own answer: `cluster.py` derived it from the configuration and `campaign.py`
wrote it out per run in a table. Comparing them fixture by fixture on
2026-08-25 gave nine disagreements and every one in the same direction, the
local table calling light what the cluster's rule calls heavy — the six ZFS
layouts, which build a module, and `ext2`, `ext3` and `btrfs-luks`, which have
no binary host at all.
"""

from __future__ import annotations

from typing import Final

from gentoo_install.model.config import InstallConfig
from gentoo_install.model.device import ZfsPool


def compiles(config: InstallConfig) -> bool:
    """Whether this configuration spends an hour in `emerge` rather than six
    minutes: a kernel built from source, a desktop, no binary host at all, or
    a ZFS root.

    A binary kernel does not spare a ZFS layout: `sys-fs/zfs` builds a module
    against whatever kernel was installed and no binary host carries one, and
    ZFSBootMenu is in `gentoo-zh` alone. `vm-zfs-encrypted` reads as light by
    every other test here and compiled nineteen packages, systemd among them,
    on the two cores a light guest is given.
    """
    if config.disk.graph.of_type(ZfsPool):
        return True
    if config.kernel.source.value.endswith("-bin"):
        return bool(config.packages.desktop) or not config.portage.binhost.official
    return True


#: What a guest that does not compile is given. Four gibibytes rather than
#: six, so the three nodes with about 6 GiB spare can each take one instead of
#: none: the installer warns below 5 GiB and builds in `/var/tmp` rather than
#: a tmpfs, which is slower and correct.
LIGHT_MEMORY_MIB: Final[int] = 4096

#: What a guest that compiles is given. Twelve rather than eight: the comment
#: this replaced said eight "is what `MAKEOPTS -j4` needs to link", and
#: `vm-gnome` was `Killed` compiling `net-libs/webkit-gtk` at 185 minutes with
#: exactly that. Two gibibytes a job is the figure the handbook gives, and it
#: was not enough for that one.
HEAVY_MEMORY_MIB: Final[int] = 12288


def memory_mib(config: InstallConfig) -> int:
    """How much memory this configuration's guest needs.

    Here rather than in either runner: `cluster.py` gave a compiling guest
    8192 and `qemu.py` gave every local guest a flat `8G`, so one rule had two
    spellings and neither was tied to a measurement that held.
    """
    return HEAVY_MEMORY_MIB if compiles(config) else LIGHT_MEMORY_MIB
