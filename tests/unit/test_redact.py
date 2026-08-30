# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from typing import Final

import pytest

from gentoo_install.errors import ConfigError, DownloadFailed
from gentoo_install.exec import config as config_loader
from gentoo_install.exec import fetch
from gentoo_install.exec.runner import Runner
from gentoo_install.redact import SECRET_QUERY_PARAMETERS, holds_a_secret


QUERY_SECRET_CASES: Final[tuple[str, ...]] = (
    "token",
    "key",
    "password",
    "secret",
    "access_token",
)


def test_query_secret_cases_cover_the_redaction_rule_table() -> None:
    assert frozenset(QUERY_SECRET_CASES) == SECRET_QUERY_PARAMETERS


@pytest.mark.parametrize("parameter", QUERY_SECRET_CASES)
def test_fetch_failure_redacts_each_secret_query_parameter(
    parameter: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = f"{parameter}-credential"
    source = f"https://example.invalid/install.toml?{parameter}={secret}&mode=dry-run"

    def refuse(*args: object, **kwargs: object) -> object:
        raise OSError("network unreachable")

    def resolver_state(url: str) -> str:
        return ""

    monkeypatch.setattr(fetch, "_urlopen", refuse)
    monkeypatch.setattr(fetch, "_resolver_state", resolver_state)

    with pytest.raises(DownloadFailed) as caught:
        fetch.read_text(source, ceiling=1024)

    message = str(caught.value)
    assert secret not in message and parameter in message and "mode=dry-run" in message
    assert not holds_a_secret(message)


def test_config_error_redacts_user_information_and_query_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "https://operator:network-password@example.invalid/install.toml"
        "?token=query-credential"
    )

    def refuse(url: str, *, ceiling: int, password: str = "") -> str:
        raise DownloadFailed(f"{url} could not be read")

    monkeypatch.setattr(fetch, "read_text", refuse)

    with pytest.raises(ConfigError) as caught:
        config_loader.load_source(source)

    message = str(caught.value)
    assert not any(
        secret in message
        for secret in ("operator", "network-password", "query-credential")
    )
    assert not holds_a_secret(message)


def test_runner_redacts_query_credentials_with_the_shared_scrubber() -> None:
    secret = "runner-credential"
    source = f"https://example.invalid/file?access_token={secret}"
    lines: list[str] = []
    result = Runner(log=lines.append, dry_run=True).run(["curl", source])
    shown = "\n".join((*lines, result.command))

    assert secret not in shown
    assert not holds_a_secret(shown)
