# SPDX-License-Identifier: GPL-2.0-or-later
"""Why an install mode is not offered, as text the catalog can hold.

One table, because these reach the operator: an exception message is English
for a log and is built at runtime, so a screen that translates one is asking
the catalog for a key that cannot exist. Every string here is a catalog key,
and the machine-specific part is drawn beside it rather than inside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Refusal:
    """Why a mode is not offered, and what on this machine made it so.

    Two fields rather than one sentence: `reason` is a catalog key and
    `detail` is a device path or a list of commands, which no catalog holds
    and no translator should see.
    """

    reason: str
    detail: str = ""

    def __bool__(self) -> bool:
        return bool(self.reason)

#: A medium with no installed system behind it.
LIVE_MEDIUM: Final[str] = "This medium carries no installed system to replace."

#: The layout commands are missing, or the layout would not read back.
CANNOT_READ_THE_SYSTEM: Final[str] = "The running system cannot be read on this machine."

#: `layout_graph` refused the root: the filesystem is named beside this.
CANNOT_DESCRIBE_THE_ROOT: Final[str] = (
    "This installer cannot describe the filesystem the running system boots from."
)

#: Writing an image over the running root takes the installer with it.
WOULD_OVERWRITE_THE_INSTALLER: Final[str] = (
    "Writing an image over the running system would erase the installer with it."
)

#: The running system was never probed, so nothing is known either way.
SYSTEM_NOT_READ: Final[str] = "The running system was not read."

#: The medium was never asked whether it runs from memory.
MEMORY_NOT_READ: Final[str] = "The medium was not asked whether it runs from memory."

#: Nothing refused: a mode that is offered.
OFFERED: Final[Refusal] = Refusal("")
