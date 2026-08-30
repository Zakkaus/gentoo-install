# SPDX-License-Identifier: GPL-2.0-or-later
"""The project's pastebin: where a key is copied from, and where a failed
install's log is sent so an issue can point at it.

The service is `matze/wastebin`. `POST /` takes JSON and answers with the path
of the new paste; `GET /raw/<id>` is the text and `GET /<id>` the page around
it. `extension` is what picks the highlighter, and its value is a filename
extension from wastebin's own list rather than a MIME type.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Final

HOST: Final[str] = "paste.gentoozh.org"
BASE: Final[str] = f"https://{HOST}"

#: Hosts that serve the text at /raw/<id> and an HTML page at the address a
#: browser shows. Only this one: any other URL is fetched as it was given.
RAW_PATH_HOSTS: Final[frozenset[str]] = frozenset({HOST})

#: How long a paste lives. A week, because the address goes into an issue and
#: the person who reads it is not the person who filed it.
EXPIRES: Final[int] = 7 * 24 * 3600


@dataclass(frozen=True)
class Export:
    """One thing the installer can send, and how the server should show it."""

    key: str
    #: Shown as the paste's own heading.
    title: str
    #: wastebin's `extension`. A name from its list, such as `log` or `toml`.
    extension: str
    #: What the row says it is for, in the menu that offers it.
    summary: str


#: Everything the installer offers to send. One row per thing, because the
#: highlighter belongs beside what it highlights and not in the screen.
EXPORTS: Final[tuple[Export, ...]] = (
    Export(
        key="log",
        title="gentoo-install log",
        extension="log",
        summary="every operation this run attempted, and what it answered",
    ),
    Export(
        key="config",
        title="gentoo-install configuration",
        extension="toml",
        summary="the answers this run was given, as a file --config reads",
    ),
)


def export_for(key: str) -> Export:
    return next(one for one in EXPORTS if one.key == key)


#: The header wastebin reads the password from on `GET /raw/:id`, named in its
#: own README. A request without it, or with the wrong one, answers 200 and the
#: HTML form rather than a status a caller can test.
PASSWORD_HEADER: Final[str] = "wastebin-password"


def payload(
    text: str, export: Export, expires: int = EXPIRES, password: str = ""
) -> bytes:
    """The body of the POST that creates a paste.

    With a password the server encrypts the entry: wastebin's README says
    ChaCha20Poly1305 with an argon2 hashed password, so the text is unreadable
    to the host as well as to anyone who guesses the address.
    """
    body: dict[str, object] = {
        "text": text,
        "extension": export.extension,
        "title": export.title,
        "expires": expires,
    }
    if password:
        body["password"] = password
    return json.dumps(body).encode()


def looks_like_the_password_form(body: str) -> bool:
    """Whether this is the form wastebin answers instead of the text.

    Measured on 2026-08-31: a `GET /raw/:id` for an encrypted paste with no
    password, or the wrong one, answers 200 with an HTML page. A caller that
    only reads the status hands that page to its parser.
    """
    return body.lstrip()[:9].lower() == "<!doctype"


def page_url(path: str) -> str:
    """The address a person opens, from the path the server answered with.

    Without the extension the server put on it. `POST /` answers
    `/<id>.<ext>`, and that address asks wastebin to highlight the paste: an
    8.7 MB install log answered 408 after five seconds every time, while the
    same address without the extension answered 200. A small paste highlights
    fine, so the size is what decides it and an install log is the large case.

    The extension still goes in the request, because it is what the paste is
    stored with. Only the address a person is handed drops it.
    """
    return f"{BASE}/{path.lstrip('/').rsplit('.', 1)[0]}"


def url_for(identifier: str) -> str:
    """The address of a paste from its identifier, or from a whole URL.

    Both, because the operator reads an address off a screen and types back
    whichever part of it they think is being asked for.
    """
    typed = identifier.strip()
    if "//" in typed:
        return raw_url(typed)
    return f"{BASE}/raw/{typed.strip('/')}"


def raw_url(url: str) -> str:
    """The address that serves the text rather than the page around it."""
    parts = urllib.parse.urlsplit(url)
    if parts.netloc in RAW_PATH_HOSTS and not parts.path.startswith("/raw/"):
        return urllib.parse.urlunsplit(parts._replace(path=f"/raw{parts.path}"))
    return url
