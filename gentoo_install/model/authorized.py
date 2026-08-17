# SPDX-License-Identifier: GPL-2.0-or-later
"""Where an `--ssh-key` value comes from, and what a fetched one has to be."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ..errors import ConfigError
from . import sshkey


class KeySourceKind(Enum):
    """How the value is obtained. `github:` and `gitlab:` are URLs once they
    have been expanded, so they are not separate kinds: what differs is the
    address, and the fetch is the same."""

    LITERAL = "literal"
    PATH = "path"
    URL = "url"


@dataclass(frozen=True)
class KeySource:
    """A source, resolved no further than a string. `model/` does no I/O, so
    reading the path or fetching the URL is the `exec/` layer's work."""

    kind: KeySourceKind
    value: str


#: The shorthands and what each expands to. One table, so a service added here
#: is a service the classifier answers.
SHORTHANDS: dict[str, str] = {
    "github": "https://github.com/{}.keys",
    "gitlab": "https://gitlab.com/{}.keys",
}

#: A URL scheme this classifier accepts. `http` is here because the operator
#: asked for it and `reinstall` takes it; nothing about the transport is
#: checked, so a key fetched over it is only as trustworthy as the network
#: between the machine and that host.
SCHEMES: tuple[str, ...] = ("https://", "http://")

_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


def classify(value: str) -> KeySource:
    """What kind of source this is, without reading or fetching anything."""
    candidate = value.strip()
    if not candidate:
        raise ConfigError("the --ssh-key source is empty")

    for service, template in SHORTHANDS.items():
        if not candidate.startswith(f"{service}:"):
            continue
        user = candidate[len(service) + 1 :]
        if not user or "/" in user or any(one.isspace() for one in user):
            raise ConfigError(f"the {service}: shorthand has no valid username")
        return KeySource(KeySourceKind.URL, template.format(user))

    for scheme in SCHEMES:
        if not candidate.startswith(scheme):
            continue
        if len(candidate) == len(scheme):
            raise ConfigError(f"the key URL {candidate} has no host")
        if any(one.isspace() for one in candidate):
            raise ConfigError("the key URL contains whitespace")
        return KeySource(KeySourceKind.URL, candidate)

    if candidate.split(None, 1)[0] in sshkey.KEY_TYPES:
        return KeySource(KeySourceKind.LITERAL, candidate)
    # Before the path: `ftp://host/key` is a scheme this cannot fetch, and
    # treating it as a filename reports a missing file instead of saying so.
    named = _SCHEME.match(candidate)
    if named is not None:
        raise ConfigError(f"unknown --ssh-key source scheme: {named.group()[:-1]}")
    return KeySource(KeySourceKind.PATH, candidate)


def keys_in(text: str) -> tuple[str, ...]:
    """Every key in a fetched or read value, each checked by `sshkey.check`.

    `https://github.com/<user>.keys` answers one key per line, and a host that
    is down answers an HTML page: the structural check is what tells them
    apart, because a page that mentions `ssh-rsa` contains the word and not
    the key.
    """
    if text.lstrip().startswith("-----BEGIN"):
        raise ConfigError("that is a private key; --ssh-key takes the public one")
    keys = tuple(sshkey.check(line) for line in text.splitlines() if line.strip())
    if not keys:
        raise ConfigError("the key source held no key")
    return keys
