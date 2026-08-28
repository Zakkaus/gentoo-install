# SPDX-License-Identifier: GPL-2.0-or-later
"""Which machine an install targets, under each of the names something spells it.

Its own module rather than part of `compat.py`: `mirrors.py` composes a URL
from the row and `compat.py` reads `mirrors.py`, so holding the row in
`compat.py` is a cycle.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Final

@dataclass(frozen=True, kw_only=True)
class Architecture:
    """One target architecture, under each of the names something spells it.

    Ecosystems name the same architecture differently, the way `fma` and
    `fma3` do: `uname -m` answers `x86_64` and Gentoo's `profiles/arch.list`
    says `amd64`. Holding the pair together is what lets a site compare a row
    instead of a literal. Keyword-only, because both fields are strings that
    read the same way round, so a swapped row would pass every gate.
    """

    #: What `uname -m` answers on a machine of this kind.
    kernel_name: str
    #: The line of `profiles/arch.list`, which is also the keyword.
    gentoo_name: str
    #: `grub-install --target`. GRUB spells the same machine a third way.
    grub_target: str
    #: Which `CPU_FLAGS_*` `make.conf` takes. Writing the x86 one on an arm64
    #: machine sets a variable no ebuild reads.
    cpu_flags_variable: str
    #: The directory the official binary host keeps this machine's packages
    #: in, under `releases/<gentoo_name>/binpackages/<release>/`. A fourth
    #: spelling: amd64's is `x86-64` and arm64's is `arm64`.
    binhost_subarch: str


#: The row every published path targets today: the stage3, the profile and the
#: official binary host are all fetched for it. Named so that the default is
#: this row rather than whichever one the table happens to list first.
AMD64: Final[Architecture] = Architecture(
    kernel_name="x86_64",
    gentoo_name="amd64",
    grub_target="x86_64-efi",
    cpu_flags_variable="CPU_FLAGS_X86",
    binhost_subarch="x86-64",
)

#: Every architecture this installer has a name for. A machine outside it is
#: refused by name rather than sent to a URL composed from a guess.
ARCHITECTURES: Final[tuple[Architecture, ...]] = (
    AMD64,
    Architecture(
        kernel_name="aarch64",
        gentoo_name="arm64",
        grub_target="arm64-efi",
        cpu_flags_variable="CPU_FLAGS_ARM",
        binhost_subarch="arm64",
    ),
    Architecture(
        kernel_name="i686",
        gentoo_name="x86",
        grub_target="i386-efi",
        cpu_flags_variable="CPU_FLAGS_X86",
        binhost_subarch="i686",
    ),
)

def _machine_row() -> Architecture:
    """The row for the machine this installer is running on.

    Every published path is composed for one architecture -- the stage3, the
    profile, the binary host, `grub-install --target` -- and the installer
    installs for the machine it runs on. Naming that machine here is what the
    constant always meant; it was written as `AMD64` because that was the only
    machine anyone had run it on.

    An architecture with no row falls back to amd64 rather than composing a
    URL from a guess: `preflight` then refuses by name, which is a message an
    operator can act on.
    """
    running = platform.machine()
    for row in ARCHITECTURES:
        if row.kernel_name == running:
            return row
    return AMD64


#: What an installation targets. Read from the machine rather than written in,
#: because a cross-architecture install is not a thing this installer does.
DEFAULT_ARCHITECTURE: Final[Architecture] = _machine_row()


def architecture_of(kernel_name: str) -> Architecture | None:
    """The row a machine reporting `kernel_name` belongs to, if there is one."""
    for row in ARCHITECTURES:
        if row.kernel_name == kernel_name:
            return row
    return None
