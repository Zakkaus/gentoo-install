# SPDX-License-Identifier: GPL-2.0-or-later
"""Take secrets out of text that is about to be logged or published.

`exec/runner.py` scrubs command arguments, fetch errors scrub their source,
and `exec/report.py` refuses to publish a log that still holds a secret.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Final

#: A modular crypt hash: `$id$` and then the rest of it. The identifier is
#: kept and everything after it replaced, so `$6$` still says which scheme
#: produced it. One expression covers `$6$salt$hash`, bcrypt's
#: `$2b$rounds$…`, yescrypt's `$y$j9T$salt$hash` and `$argon2id$v=19$…`,
#: which differ in how many fields follow the identifier.
CRYPT_HASH: Final[re.Pattern[str]] = re.compile(r"\$[0-9A-Za-z-]{1,12}\$\S{10,}")

HIDDEN: Final[str] = "[redacted]"

SECRET_QUERY_PARAMETERS: Final[frozenset[str]] = frozenset(
    ("token", "key", "password", "secret", "access_token")
)
_URL: Final[re.Pattern[str]] = re.compile(r"https?://[^\s'\"`<>]+", re.IGNORECASE)

def _hide(found: re.Match[str]) -> str:
    scheme = found.group(0).split("$")[1]
    return f"${scheme}${HIDDEN}"


def _hide_url(found: re.Match[str]) -> str:
    return _scrub_url(found.group(0))


def _scrub_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return value
    if not parts.scheme or host is None:
        return value
    netloc = parts.netloc
    if parts.username is not None:
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{port}" if port is not None else host
    query = _scrub_query(parts.query)
    if netloc == parts.netloc and query == parts.query:
        return value
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _scrub_query(query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    if not any(name.casefold() in SECRET_QUERY_PARAMETERS for name, _ in pairs):
        return query
    return urllib.parse.urlencode(
        [
            (name, HIDDEN if name.casefold() in SECRET_QUERY_PARAMETERS else value)
            for name, value in pairs
        ]
    )


def scrub(value: str) -> str:
    """`value` with password hashes and URL credentials replaced."""
    return CRYPT_HASH.sub(_hide, _URL.sub(_hide_url, value))


def holds_a_secret(text: str) -> bool:
    """Whether publishing `text` would disclose password material."""
    return scrub(text) != text
