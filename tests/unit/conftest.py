# SPDX-License-Identifier: GPL-2.0-or-later
"""What every unit test needs from this machine, and nothing more.

The Proxmox client reads an API token from a path outside the tree, so three
tests that build a request were green here and red on a runner that has no
such file. A test that is about the token points `TOKEN_FILE` at its own, and
a monkeypatch inside a test wins over this one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.vm import proxmox


@pytest.fixture(scope="session")
def _token_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # Outside each test's own `tmp_path`: one test archives that directory and
    # compares what came back, and a token written there is an extra member.
    token = tmp_path_factory.mktemp("api") / "token"
    token.write_text("01234567-89ab-cdef-0123-456789abcdef\n")
    return token


@pytest.fixture(autouse=True)
def _api_token(_token_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxmox, "TOKEN_FILE", _token_file)
