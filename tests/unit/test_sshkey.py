# SPDX-License-Identifier: GPL-2.0-or-later
"""A public key is checked before it becomes the only way into the machine."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from gentoo_install.errors import ConfigError
from gentoo_install.model.paste import raw_url, url_for
from gentoo_install.model import sshkey

TYPES = ("ed25519", "rsa", "ecdsa")


def generated(tmp_path: Path, kind: str) -> str:
    where = tmp_path / kind
    subprocess.run(
        ["ssh-keygen", "-q", "-t", kind, "-N", "", "-f", str(where)], check=True
    )
    return where.with_suffix(".pub").read_text().strip()


@pytest.mark.parametrize("kind", TYPES)
def test_a_key_ssh_keygen_made_is_accepted(tmp_path: Path, kind: str) -> None:
    """Checked against real output rather than against a reading of the
    format: every type here came out of ssh-keygen on this machine."""
    line = generated(tmp_path, kind)
    assert sshkey.check(line) == line


def test_a_key_truncated_in_the_middle_is_refused(tmp_path: Path) -> None:
    """The failure this exists for: a paste that lost its tail still has a
    valid header, so the type check alone accepts it."""
    kind, blob, comment = generated(tmp_path, "ed25519").split()
    with pytest.raises(ConfigError, match="truncated"):
        sshkey.check(f"{kind} {blob[:-4]} {comment}")


def test_a_comment_is_optional_and_extra_whitespace_is_normalised(tmp_path: Path) -> None:
    kind, blob, _ = generated(tmp_path, "ed25519").split()
    assert sshkey.check(f"  {kind}   {blob}  ") == f"{kind} {blob}"


def test_a_body_declaring_another_type_is_refused(tmp_path: Path) -> None:
    """`ssh-rsa` in front of an ed25519 body is what a hand-edited file looks
    like, and sshd ignores the line rather than saying so."""
    _, blob, comment = generated(tmp_path, "ed25519").split()
    with pytest.raises(ConfigError, match="declares a different type"):
        sshkey.check(f"ssh-rsa {blob} {comment}")


@pytest.mark.parametrize(
    "line",
    ["", "ssh-ed25519", "ssh-foo AAAA", "ssh-ed25519 not+base64!!", "-----BEGIN"],
)
def test_what_is_not_a_key_is_named_rather_than_written(line: str) -> None:
    with pytest.raises(ConfigError):
        sshkey.check(line)


def test_a_private_key_is_refused(tmp_path: Path) -> None:
    """Pointing at the wrong file of the pair is the common mistake, and the
    private half must never reach an authorized_keys file."""
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "k")], check=True
    )
    private = (tmp_path / "k").read_text().splitlines()[0]
    with pytest.raises(ConfigError):
        sshkey.check(private)


def test_only_the_gentoozh_paste_gets_a_raw_path() -> None:
    """That host serves an HTML page at the address a browser shows; every
    other address is used as it was given."""
    assert raw_url("https://paste.gentoozh.org/vQTZ") == "https://paste.gentoozh.org/raw/vQTZ"
    assert raw_url("https://paste.gentoozh.org/raw/vQTZ") == "https://paste.gentoozh.org/raw/vQTZ"
    assert raw_url("https://example.com/id_ed25519.pub") == "https://example.com/id_ed25519.pub"
    assert raw_url("https://github.com/zakkaus.keys") == "https://github.com/zakkaus.keys"
    assert url_for("hjq+353Jzfk") == "https://paste.gentoozh.org/raw/hjq+353Jzfk"


def test_a_key_that_decodes_but_holds_one_field_is_refused() -> None:
    """A body with only its type and no key material: base64 is fine and the
    type matches, so nothing before the field count catches it."""
    body = base64.b64encode(
        len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519"
    ).decode()
    with pytest.raises(ConfigError, match="declares a different type"):
        sshkey.check(f"ssh-ed25519 {body}")


def test_a_refused_key_is_not_quoted_back() -> None:
    """This is the field a password or a private key is pasted into by mistake,
    and every error here reaches the log, `install.jsonl` and the paste an
    operator sends to somebody else."""
    import pytest

    from gentoo_install.errors import ConfigError
    from gentoo_install.model import sshkey

    secret = "correct-horse-battery-staple"
    for line in (secret, f"{secret} body", f"ssh-ed25519 {secret}"):
        with pytest.raises(ConfigError) as raised:
            sshkey.check(line)
        assert secret not in str(raised.value), str(raised.value)

    # Negative control: a refusal still says which types are accepted, so the
    # rule above cannot be met by a message that carries nothing at all.
    with pytest.raises(ConfigError, match="ssh-ed25519"):
        sshkey.check("ssh-rsa2 AAAA")
