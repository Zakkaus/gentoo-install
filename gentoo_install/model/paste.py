"""The project's pastebin, which is where a key or a config gets copied from."""

from __future__ import annotations

import urllib.parse
from typing import Final

HOST: Final[str] = "paste.gentoozh.org"
BASE: Final[str] = f"https://{HOST}"

#: Hosts that serve the text at /raw/<id> and an HTML page at the address a
#: browser shows. Only this one: any other URL is fetched as it was given.
RAW_PATH_HOSTS: Final[frozenset[str]] = frozenset({HOST})


def url_for(identifier: str) -> str:
    """The address of a paste from its identifier alone."""
    return f"{BASE}/raw/{identifier.strip().strip('/')}"


def raw_url(url: str) -> str:
    """The address that serves the text rather than the page around it."""
    parts = urllib.parse.urlsplit(url)
    if parts.netloc in RAW_PATH_HOSTS and not parts.path.startswith("/raw/"):
        return urllib.parse.urlunsplit(parts._replace(path=f"/raw{parts.path}"))
    return url
