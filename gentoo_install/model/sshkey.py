# SPDX-License-Identifier: GPL-2.0-or-later
"""One public key, checked before it is the only way into the machine."""

from __future__ import annotations

import base64
import binascii
from typing import Final

from ..errors import ConfigError

#: The key types OpenSSH 10 accepts in an `authorized_keys` file. `ssh-dss` is
#: absent because OpenSSH removed it.
KEY_TYPES: Final[frozenset[str]] = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)


def check(line: str) -> str:
    """The key with its whitespace normalised, or a named error.

    A key that reached the target truncated is discovered at the first login
    attempt, which is the moment the console is no longer available.
    """
    fields = line.split()
    if len(fields) < 2:
        raise ConfigError(f"not a public key: {line[:40]!r}")
    kind, blob = fields[0], fields[1]
    if kind not in KEY_TYPES:
        raise ConfigError(f"unknown key type: {kind}")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ConfigError(f"the key body is not base64: {error}") from error
    parts = _wire(raw)
    if len(parts) < 2 or parts[0] != kind.encode():
        raise ConfigError(f"the key body does not declare {kind}")
    return " ".join(fields)


def _wire(raw: bytes) -> list[bytes]:
    """The body split into its length-prefixed strings.

    Walking the whole blob is what catches a paste truncated at the end: the
    header still reads, and only the last length runs past what arrived.
    """
    parts: list[bytes] = []
    at = 0
    while at < len(raw):
        if at + 4 > len(raw):
            raise ConfigError("the key body ends inside a length; it is truncated")
        size = int.from_bytes(raw[at : at + 4], "big")
        at += 4
        if at + size > len(raw):
            raise ConfigError("the key body ends inside a field; it is truncated")
        parts.append(raw[at : at + size])
        at += size
    return parts
