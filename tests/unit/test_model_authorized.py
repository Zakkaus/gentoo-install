# SPDX-License-Identifier: GPL-2.0-or-later
"""Where an `--ssh-key` value comes from, and what a fetched one has to be."""

from __future__ import annotations

import base64

import pytest

from gentoo_install.errors import ConfigError
from gentoo_install.model import sshkey
from gentoo_install.model.authorized import (
    SCHEMES,
    SHORTHANDS,
    KeySource,
    KeySourceKind,
    classify,
    keys_in,
)


def _key(kind: str = "ssh-ed25519", comment: str = "zakk@box") -> str:
    """A structurally valid key: the body's first length-prefixed field has to
    repeat the type name, so a made-up base64 blob is rejected."""
    body = len(kind).to_bytes(4, "big") + kind.encode() + (32).to_bytes(4, "big") + b"k" * 32
    return f"{kind} {base64.b64encode(body).decode()} {comment}"


CASES: tuple[tuple[str, KeySource], ...] = (
    (_key(), KeySource(KeySourceKind.LITERAL, _key())),
    ("~/.ssh/id_ed25519.pub", KeySource(KeySourceKind.PATH, "~/.ssh/id_ed25519.pub")),
    ("/etc/keys/one.pub", KeySource(KeySourceKind.PATH, "/etc/keys/one.pub")),
    (
        "https://example.invalid/key.pub",
        KeySource(KeySourceKind.URL, "https://example.invalid/key.pub"),
    ),
    (
        "http://example.invalid/key.pub",
        KeySource(KeySourceKind.URL, "http://example.invalid/key.pub"),
    ),
    ("github:zakkaus", KeySource(KeySourceKind.URL, "https://github.com/zakkaus.keys")),
    ("gitlab:zakkaus", KeySource(KeySourceKind.URL, "https://gitlab.com/zakkaus.keys")),
    # A drive letter, not a scheme: the operator copying a command line from
    # Windows is told the file is not here, which is true, rather than that
    # `C:` is a transport this installer cannot fetch.
    (
        r"C:\Users\zakk\.ssh\id_ed25519.pub",
        KeySource(KeySourceKind.PATH, r"C:\Users\zakk\.ssh\id_ed25519.pub"),
    ),
    (_key("ssh-rsa"), KeySource(KeySourceKind.LITERAL, _key("ssh-rsa"))),
    (
        _key("ecdsa-sha2-nistp521"),
        KeySource(KeySourceKind.LITERAL, _key("ecdsa-sha2-nistp521")),
    ),
)


@pytest.mark.parametrize(("value", "expected"), CASES)
def test_each_source_form_is_classified(value: str, expected: KeySource) -> None:
    assert classify(value) == expected


def test_the_cases_cover_every_kind_and_every_shorthand() -> None:
    """A form added to the tables without a case here fails, or a table entry
    that is never exercised reads as coverage and is not."""
    assert {one.kind for _, one in CASES} == set(KeySourceKind)
    for service in SHORTHANDS:
        assert any(value.startswith(f"{service}:") for value, _ in CASES), service
    for scheme in SCHEMES:
        assert any(value.startswith(scheme) for value, _ in CASES), scheme

    # Every key type the checker accepts is a form somebody may paste, and
    # `--help` names three of them: those three are classified here.
    from gentoo_install.model import sshkey

    for kind in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp521"):
        assert kind in sshkey.KEY_TYPES, kind
        assert any(value.startswith(f"{kind} ") for value, _ in CASES), kind


def test_the_help_names_every_source_the_classifier_takes() -> None:
    """The option took a URL and `github:` from the day it was written and
    named neither, so an operator reading `--help` had no way to know: the
    form was asked about as though it did not exist, and it had always
    worked."""
    import inspect

    from gentoo_install import cli

    source = inspect.getsource(cli)
    said = source[source.index('"--ssh-key"') : source.index('"--ssh-port"')]
    for named in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp", "http", "github:", "gitlab:"):
        assert named in said, (named, said)


@pytest.mark.parametrize("value", ("", "   ", "\n"))
def test_an_empty_source_is_named(value: str) -> None:
    with pytest.raises(ConfigError, match="empty"):
        classify(value)


@pytest.mark.parametrize("value", ("github:", "gitlab:", "github:two names", "github:a/b"))
def test_a_shorthand_without_a_usable_username_is_named(value: str) -> None:
    with pytest.raises(ConfigError, match="username"):
        classify(value)


def test_a_scheme_that_cannot_be_fetched_is_named_rather_than_read_as_a_path() -> None:
    """`ftp://host/key.pub` as a filename reports a missing file, which sends
    the operator looking for a directory instead of at the scheme."""
    with pytest.raises(ConfigError, match="scheme: ftp"):
        classify("ftp://example.invalid/key.pub")


@pytest.mark.parametrize("value", ("https://", "http://"))
def test_a_url_with_no_host_is_named(value: str) -> None:
    with pytest.raises(ConfigError, match="no host"):
        classify(value)


def test_a_private_key_is_refused_by_name() -> None:
    """The operator pastes the wrong half of the pair, and `authorized_keys`
    would take the line as a malformed key rather than say what happened."""
    with pytest.raises(ConfigError, match="private key"):
        keys_in("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA==\n")


def test_every_key_a_dot_keys_answer_holds_is_returned() -> None:
    """`https://github.com/<user>.keys` answers one key per line, and taking
    only the first locks the operator out of their other machines."""
    answer = f"{_key(comment='one')}\n\n{_key('ssh-rsa', comment='two')}\n"
    assert keys_in(answer) == (_key(comment="one"), _key("ssh-rsa", comment="two"))


def test_an_html_error_page_that_mentions_a_key_type_is_not_a_key() -> None:
    """A host that is down answers a page, and `ssh-rsa` appearing in its text
    is what a substring check accepts."""
    page = (
        "<!DOCTYPE html>\n<html><head><title>404 Not Found</title></head>\n"
        "<body><p>No user has an ssh-rsa key at this address.</p></body></html>\n"
    )
    with pytest.raises(ConfigError):
        keys_in(page)


def test_a_source_that_held_nothing_is_named() -> None:
    """An empty answer is a fetch that succeeded and returned no key, which
    reads as success everywhere else."""
    with pytest.raises(ConfigError, match="held no key"):
        keys_in("\n  \n")


def test_the_key_check_is_the_one_in_sshkey() -> None:
    """One rule set: `parse.py` and the TUI already validate a key with
    `sshkey.check`, and a second copy here would drift from it."""
    truncated = _key().rsplit(" ", 1)[0][:-4] + " zakk"
    with pytest.raises(ConfigError):
        sshkey.check(truncated)
    with pytest.raises(ConfigError):
        keys_in(truncated)
