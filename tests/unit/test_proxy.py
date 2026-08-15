# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import json
import socket
import urllib.request
from typing import Any
from typing import cast

import pytest

from gentoo_install.exec import fetch
from gentoo_install.exec.runner import Runner
from gentoo_install.model.config import ProxyConfig, ProxyKind


PROXY = ProxyConfig(
    kind=ProxyKind.HTTP,
    host="proxy.example",
    port=8080,
    username="operator",
    password="secret",
    bypass=("localhost", "internal.example"),
)


class Response:
    headers: dict[str, str] = {"Date": "Wed, 01 Jan 2025 00:00:00 GMT"}

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return b"US\n"


def test_http_proxy_opener_keeps_credentials_out_of_environment_and_bypasses_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[Any] = []

    class Opener:
        def open(self, request: object, timeout: float) -> Response:
            opened.append((request, timeout))
            return Response()

    built: list[tuple[object, ...]] = []
    def build(*handlers: Any) -> Opener:
        built.append(handlers)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build)
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Response())

    with fetch._urlopen(fetch._asked("https://public.example/path"), PROXY, 3.0):
        pass
    with fetch._urlopen(fetch._asked("https://internal.example/path"), PROXY, 3.0):
        pass

    assert built
    handler = cast(Any, built[0][0])
    assert handler.proxies["https"] == PROXY.url
    assert len(opened) == 1
    assert "secret" not in str(fetch._CURRENT_PROXY.get())


def test_socks_scheme_controls_local_name_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[bytes] = []

    class Socket:
        def sendall(self, data: bytes) -> None:
            sent.append(data)

        def recv(self, size: int) -> bytes:
            if size == 2:
                return b"\x05\x00"
            return b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50"

        def close(self) -> None:
            return None

    monkeypatch.setattr(socket, "create_connection", lambda address, timeout: Socket())
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 80))],
    )
    fetch._socks_connect(ProxyConfig(kind=ProxyKind.SOCKS5, host="proxy.example", port=1080), "internal.example", 80, 3.0)

    request = sent[-1]
    assert request[3] == 3


def test_runner_redacts_proxy_credentials_in_dry_run_and_result_command() -> None:
    lines: list[str] = []
    runner = Runner(log=lines.append, dry_run=True, proxy=PROXY)
    result = runner.run(["wget", PROXY.url, "https://public.example/file"])
    assert all("secret" not in line for line in lines)
    assert "secret" not in result.command


def test_fetch_paths_accept_the_same_proxy_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ProxyConfig | None] = []
    opened: list[ProxyConfig | None] = []

    def read(url: str, proxy: ProxyConfig | None = None) -> str:
        seen.append(proxy)
        if url.endswith(".json"):
            return json.dumps({"versions": [{"version": "1.0", "keywords": ["amd64"]}]})
        if "/contents/" in url:
            return "[]"
        if "zfs-1.0.ebuild" in url:
            return "MODULES_KERNEL_MAX=7.0\n"
        if "ipinfo" in url:
            return "AU\n"
        return "# pointer\n"

    monkeypatch.setattr(fetch, "_read", read)
    def open_url(request: Any, proxy: ProxyConfig | None, timeout: float) -> Response:
        opened.append(proxy)
        return Response()

    monkeypatch.setattr(fetch, "_urlopen", open_url)
    monkeypatch.setattr(fetch, "ONLINE_PAUSE", 0.0)
    assert fetch.network_time(PROXY) > 0.0
    assert fetch.egress_country(PROXY) == "AU"
    assert fetch.online(PROXY) is True
    assert fetch.mirror_online("https://mirror.example", "systemd", PROXY) is True
    assert fetch.package_versions("app-misc/example", PROXY)
    assert fetch.overlay_versions("app-misc/example", PROXY) == ()
    assert fetch.zfs_kernel_max(PROXY).maximum == "7.0"
    assert seen and all(proxy is PROXY for proxy in seen)
    assert opened == [PROXY]
