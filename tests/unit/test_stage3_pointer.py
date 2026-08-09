"""`_newest` reads the pointer file, which every mirror serves identically."""

from __future__ import annotations

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

BASE = "https://mirrors.ustc.edu.cn/gentoo/releases/amd64/autobuilds/current-stage3-amd64-systemd"


def test_reads_the_name_out_of_the_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_read", lambda url: POINTER)
    assert fetch._newest(BASE, "systemd") == "stage3-amd64-systemd-20260802T163058Z.tar.xz"


def test_asks_for_the_pointer_beside_the_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def record(url: str) -> str:
        asked.append(url)
        return POINTER

    monkeypatch.setattr(fetch, "_read", record)
    fetch._newest(BASE, "systemd")
    assert asked == [
        "https://mirrors.ustc.edu.cn/gentoo/releases/amd64/autobuilds"
        "/latest-stage3-amd64-systemd.txt"
    ]


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
    fetch._newest(f"https://distfiles.gentoo.org/x/current-stage3-amd64-{variant}", variant)
    assert asked[0].endswith(f"latest-stage3-amd64-{variant}.txt")
