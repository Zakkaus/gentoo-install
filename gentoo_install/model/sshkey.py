# SPDX-License-Identifier: GPL-2.0-or-later
"""One public key, checked before it is the only way into the machine."""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final

from ..errors import ConfigError

class KeyShape(Enum):
    """The structure after the SSH wire type."""

    FIXED_LENGTH = auto()
    RSA = auto()


@dataclass(frozen=True)
class KeyFormat:
    """The fields an accepted public-key type requires."""

    name: str
    field_count: int
    public_material_field: int
    shape: KeyShape
    public_material_size: int | None = None
    curve: bytes | None = None


FIRST_DATA_FIELD: Final[int] = 1
SECOND_DATA_FIELD: Final[int] = 2
RSA_EXPONENT_FIELD: Final[int] = FIRST_DATA_FIELD
RSA_MODULUS_FIELD: Final[int] = SECOND_DATA_FIELD
RSA_MINIMUM_MODULUS_BITS: Final[int] = 1024

# OpenSSH refuses RSA moduli below 1024 bits, so a non-empty field is insufficient.
KEY_FORMATS: Final[tuple[KeyFormat, ...]] = (
    KeyFormat("ssh-ed25519", 2, FIRST_DATA_FIELD, KeyShape.FIXED_LENGTH, 32),
    KeyFormat("ssh-rsa", 3, RSA_MODULUS_FIELD, KeyShape.RSA),
    KeyFormat(
        "ecdsa-sha2-nistp256",
        3,
        SECOND_DATA_FIELD,
        KeyShape.FIXED_LENGTH,
        65,
        b"nistp256",
    ),
    KeyFormat(
        "ecdsa-sha2-nistp384",
        3,
        SECOND_DATA_FIELD,
        KeyShape.FIXED_LENGTH,
        97,
        b"nistp384",
    ),
    KeyFormat(
        "ecdsa-sha2-nistp521",
        3,
        SECOND_DATA_FIELD,
        KeyShape.FIXED_LENGTH,
        133,
        b"nistp521",
    ),
    KeyFormat(
        "sk-ssh-ed25519@openssh.com",
        3,
        FIRST_DATA_FIELD,
        KeyShape.FIXED_LENGTH,
        32,
    ),
    KeyFormat(
        "sk-ecdsa-sha2-nistp256@openssh.com",
        4,
        SECOND_DATA_FIELD,
        KeyShape.FIXED_LENGTH,
        65,
        b"nistp256",
    ),
)
KEY_TYPES: Final[frozenset[str]] = frozenset(key_format.name for key_format in KEY_FORMATS)

# Named in the refusal, so an operator learns what is accepted without the
# message having to quote what they typed.
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
    key_format = _format(kind)
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ConfigError("the key body is not base64") from error
    parts = _wire(raw)
    if parts[0] != kind.encode():
        raise ConfigError("the key body declares a different type from its first field")
    _validate_shape(parts, key_format)
    return " ".join(fields)


def fingerprint(line: str) -> str:
    """The key as `ssh-keygen -l` names it: `SHA256:` and the digest of the
    wire blob, base64 without padding.

    A hash and not the key: a dry-run reaches `install.jsonl` and any paste an
    operator sends on, and the comment field of a key carries a username and a
    hostname.
    """
    blob = line.split()[1]
    digest = hashlib.sha256(base64.b64decode(blob, validate=True)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


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


def _format(kind: str) -> KeyFormat:
    """The key format named by an authorized_keys type."""
    for key_format in KEY_FORMATS:
        if key_format.name == kind:
            return key_format
    raise ConfigError(f"unknown key type; the accepted ones are {ACCEPTED}")


def _validate_shape(parts: list[bytes], key_format: KeyFormat) -> None:
    """Reject complete wire blobs that do not contain this key type's fields."""
    if len(parts) != key_format.field_count:
        raise ConfigError("the key body does not have the fields required for its type")
    if key_format.curve is not None and parts[FIRST_DATA_FIELD] != key_format.curve:
        raise ConfigError("the key body declares a curve different from its type")
    if key_format.shape is KeyShape.RSA:
        _validate_rsa(parts)
        return
    size = key_format.public_material_size
    if size is None or len(parts[key_format.public_material_field]) != size:
        raise ConfigError("the key body has public material with the wrong length")


def _validate_rsa(parts: list[bytes]) -> None:
    """Reject RSA fields that OpenSSH cannot use for authentication."""
    exponent = parts[RSA_EXPONENT_FIELD]
    modulus = parts[RSA_MODULUS_FIELD]
    if not exponent or not modulus:
        raise ConfigError("the RSA public material is incomplete")
    value = int.from_bytes(exponent, "big")
    if value < 3 or value % 2 == 0:
        raise ConfigError("the RSA public exponent is invalid")
    if int.from_bytes(modulus, "big").bit_length() < RSA_MINIMUM_MODULUS_BITS:
        raise ConfigError("the RSA public modulus is shorter than 1024 bits")
