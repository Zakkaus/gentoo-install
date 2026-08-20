# SPDX-License-Identifier: GPL-2.0-or-later
"""`_newest` reads the pointer file, which every mirror serves identically."""

from __future__ import annotations

import time

import pytest

from gentoo_install.errors import DownloadFailed
from gentoo_install.exec import fetch

#: Captured from `mirrors.ustc.edu.cn` on 2026-08-09. Every mirror serves this
#: file byte for byte, because it is Gentoo's own signed output rather than
#: something the mirror generates.
POINTER = """\
-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA256

# Latest as of Sun, 09 Aug 2026 15:00:00 +0000
# ts=1786287600
20260802T163058Z/stage3-amd64-systemd-20260802T163058Z.tar.xz 511865372
-----BEGIN PGP SIGNATURE-----

iQFPBAEBCAA5FiEEU05CCatJ7uHBnZYWLERpXbn2BD0FAmp4ljAbFIAAAAAABAAO
=LvLJ
-----END PGP SIGNATURE-----
"""

BASE = "https://mirrors.ustc.edu.cn/gentoo/releases/amd64/autobuilds"


def test_reads_the_dated_path_out_of_the_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The path, not the bare name: `current-stage3-amd64-*` is a symlink that
    moves when a build is published, and downloading through it answered `404
    Not Found` for an archive the pointer had just named."""
    monkeypatch.setattr(fetch, "_read", lambda url: POINTER)
    assert fetch._newest(BASE, "systemd") == (
        "20260802T163058Z/stage3-amd64-systemd-20260802T163058Z.tar.xz"
    )


def test_asks_for_the_pointer_beside_the_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def record(url: str) -> str:
        asked.append(url)
        return POINTER

    monkeypatch.setattr(fetch, "_read", record)
    fetch._newest(BASE, "systemd")
    assert asked == [f"{BASE}/latest-stage3-amd64-systemd.txt"]


def test_an_index_page_is_not_what_it_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """USTC links each file by an absolute path, so the pattern that matched
    Gentoo's own directory listing found nothing there. Reading the pointer
    file instead is what makes a Chinese mirror usable at all."""
    listing = (
        '<a href="/gentoo/releases/amd64/autobuilds/current-stage3-amd64-systemd'
        '/stage3-amd64-systemd-20260802T163058Z.tar.xz">stage3</a>'
    )
    monkeypatch.setattr(fetch, "_read", lambda url: listing)
    with pytest.raises(DownloadFailed, match="names no stage3 archive"):
        fetch._newest(BASE, "systemd")


def test_a_mirror_mid_sync_is_a_download_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_read", lambda url: "# Latest as of never\n")
    with pytest.raises(DownloadFailed, match="names no stage3 archive"):
        fetch._newest(BASE, "systemd")


@pytest.mark.parametrize("variant", ["systemd", "openrc", "hardened-systemd"])
def test_the_variant_picks_the_pointer(monkeypatch: pytest.MonkeyPatch, variant: str) -> None:
    asked: list[str] = []

    def record(url: str) -> str:
        asked.append(url)
        return POINTER.replace("systemd", variant)

    monkeypatch.setattr(fetch, "_read", record)
    found = fetch._newest("https://distfiles.gentoo.org/releases/amd64/autobuilds", variant)
    assert asked[0].endswith(f"latest-stage3-amd64-{variant}.txt")
    assert found == f"20260802T163058Z/stage3-amd64-{variant}-20260802T163058Z.tar.xz"


def test_the_pause_between_attempts_grows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed two seconds was not enough spread: twelve guests starting
    together each failed every attempt inside ninety seconds against a host
    that was answering."""
    slept: list[float] = []
    monkeypatch.setattr(fetch, "_read", lambda url: (_ for _ in ()).throw(DownloadFailed("no")))
    monkeypatch.setattr(time, "sleep", slept.append)
    assert fetch.reachable("https://example.invalid/x") is False
    assert slept == [2.0, 4.0, 8.0, 16.0], slept


def test_one_lost_request_does_not_declare_the_machine_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This check guards the whole run. A link that answered five runs in a
    row timed out on the sixth, and the install stopped before it started."""
    asked: list[int] = []

    def flaky(url: str) -> str:
        asked.append(1)
        if len(asked) < fetch.ONLINE_TRIES:
            raise DownloadFailed("timed out")
        return "{}"

    monkeypatch.setattr(fetch, "_read", flaky)
    monkeypatch.setattr(fetch, "ONLINE_PAUSE", 0.0)
    assert fetch.online() is True
    assert len(asked) == fetch.ONLINE_TRIES


def test_a_machine_with_no_network_is_still_reported_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying must not turn a real absence of network into a slow yes."""
    asked: list[int] = []

    def never(url: str) -> str:
        asked.append(1)
        raise DownloadFailed("no route to host")

    monkeypatch.setattr(fetch, "_read", never)
    monkeypatch.setattr(fetch, "ONLINE_PAUSE", 0.0)
    assert fetch.online() is False
    assert len(asked) == fetch.ONLINE_TRIES


def test_a_family_with_no_route_is_retried_over_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guest with a ULA address and NAT64 answers `Network is unreachable`
    at once for every global IPv6 destination. Twenty cluster runs stopped
    there while `curl` fetched the same URL from the same guest a second
    earlier, because it falls back and urllib does not."""
    import errno
    import socket
    import urllib.error

    tried: list[int] = []

    def unreachable_then_fine(url: str) -> str:
        tried.append(socket.getaddrinfo("x", 0)[0][0] if False else len(tried))
        if len(tried) == 1:
            raise DownloadFailed("unreachable") from urllib.error.URLError(
                OSError(errno.ENETUNREACH, "Network is unreachable")
            )
        return "the pointer file"

    monkeypatch.setattr(fetch, "_read_once", unreachable_then_fine)
    assert fetch._read("https://example.invalid/x") == "the pointer file"
    assert len(tried) == 2, "the second attempt is the one over IPv4"


def test_an_ordinary_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 is the same over either family, and asking twice hides it."""
    import email.message
    import urllib.error

    tried: list[int] = []

    def missing(url: str) -> str:
        tried.append(1)
        raise DownloadFailed("404") from urllib.error.HTTPError(
            url, 404, "gone", email.message.Message(), None
        )

    monkeypatch.setattr(fetch, "_read_once", missing)
    with pytest.raises(DownloadFailed):
        fetch._read("https://example.invalid/x")
    assert len(tried) == 1


def test_one_family_at_a_time_inside_the_fallback() -> None:
    """The fallback is what makes the next attempt different; if it resolved
    the same addresses it would fail the same way. Both directions, because an
    IPv6-only machine has no IPv4 route and retrying IPv4 is no retry."""
    import socket

    for family in (socket.AF_INET, socket.AF_INET6):
        with fetch._over(family):
            found = {one[0] for one in socket.getaddrinfo("localhost", 80)}
        assert found == {family}
    # Restored afterwards, or every later request in this process is pinned.
    assert socket.getaddrinfo("localhost", 80, socket.AF_INET6, socket.SOCK_STREAM)
    assert socket.getaddrinfo("localhost", 80, socket.AF_INET, socket.SOCK_STREAM)


def test_both_families_are_tried_after_a_route_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An IPv6-only machine is the case a fall back to IPv4 alone cannot
    serve: the family that failed is the only one it has."""
    import errno
    import socket
    import urllib.error

    seen: list[int] = []
    real = socket.getaddrinfo

    def watching(host: object, port: object, family: int = 0, *rest: object) -> object:
        seen.append(family)
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    def failing(url: str) -> str:
        try:
            socket.getaddrinfo("example.invalid", 443)
        except OSError:
            pass
        raise DownloadFailed("unreachable") from urllib.error.URLError(
            OSError(errno.ENETUNREACH, "Network is unreachable")
        )

    monkeypatch.setattr(socket, "getaddrinfo", watching)
    monkeypatch.setattr(fetch, "_read_once", failing)
    with pytest.raises(DownloadFailed):
        fetch._read("https://example.invalid/x")
    assert socket.AF_INET in seen and socket.AF_INET6 in seen, seen
    assert socket.getaddrinfo is watching, "the resolver is put back each time"
    monkeypatch.setattr(socket, "getaddrinfo", real)
