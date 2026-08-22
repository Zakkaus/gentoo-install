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

#: Named in the refusal, so an operator learns what is accepted without the
#: message having to quote what they typed.
ACCEPTED: Final[str] = ", ".join(sorted(KEY_TYPES))


def check(line: str) -> str:
    """The key with its whitespace normalised, or a named error.

    A key that reached the target truncated is discovered at the first login
    attempt, which is the moment the console is no longer available.
    """
    # No message quotes what was read. This is the field a password or a
    # private key is pasted into by mistake, and every error here reaches the
    # log, `install.jsonl` and the paste an operator sends to somebody else.
    fields = line.split()
    if len(fields) < 2:
        raise ConfigError("not a public key: a key is a type and a body")
    kind, blob = fields[0], fields[1]
    if kind not in KEY_TYPES:
        raise ConfigError(f"unknown key type; the accepted ones are {ACCEPTED}")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ConfigError("the key body is not base64") from error
    parts = _wire(raw)
    if len(parts) < 2 or parts[0] != kind.encode():
        raise ConfigError("the key body declares a different type from its first field")
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
