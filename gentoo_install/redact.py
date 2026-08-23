# SPDX-License-Identifier: GPL-2.0-or-later
"""Take crypt hashes out of text that is about to be logged or published.

Two callers, and both are needed: `exec/runner.py` scrubs the argv of every
command, and `exec/report.py` refuses to publish a log that still holds one,
because a log carries command output as well as command lines.
"""

from __future__ import annotations

import re
from typing import Final

#: A modular crypt hash: `$id$` and then the rest of it. The identifier is
#: kept and everything after it replaced, so `$6$` still says which scheme
#: produced it. One expression covers `$6$salt$hash`, bcrypt's
#: `$2b$rounds$…`, yescrypt's `$y$j9T$salt$hash` and `$argon2id$v=19$…`,
#: which differ in how many fields follow the identifier.
CRYPT_HASH: Final[re.Pattern[str]] = re.compile(r"\$[0-9A-Za-z-]{1,12}\$\S{10,}")

HIDDEN: Final[str] = "[redacted]"


def _hide(found: re.Match[str]) -> str:
    scheme = found.group(0).split("$")[1]
    return f"${scheme}${HIDDEN}"


def scrub(value: str) -> str:
    """`value` with every crypt hash in it replaced by its scheme."""
    return CRYPT_HASH.sub(_hide, value)


def holds_a_secret(text: str) -> bool:
    """Whether publishing `text` would hand out offline-cracking material."""
    return CRYPT_HASH.search(text) is not None
