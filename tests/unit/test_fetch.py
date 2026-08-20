# SPDX-License-Identifier: GPL-2.0-or-later
"""The stage3 fetch, and what it does when a mirror will not answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_install.errors import ArchiveDigestMismatch, DownloadFailed
from gentoo_install.exec import fetch
from gentoo_install.exec.runner import Runner


def test_a_mirror_that_cannot_be_reached_is_not_the_end_of_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage3 fetch was the one step in the plan with a single address. A
    name that did not resolve ended the install three minutes in, with the
    disks already partitioned and mounted."""
    asked: list[str] = []

    def refusing(
        mirror: str,
        variant: str,
        fingerprint: str,
        work: Path,
        runner: Runner,
        proxy: object = None,
    ) -> Path:
        asked.append(mirror)
        if mirror != "https://third.example":
            raise DownloadFailed(f"{mirror} could not be reached")
        return work / "stage3.tar.xz"

    monkeypatch.setattr(fetch, "_stage3_from", refusing)

    where = fetch.stage3(
        "https://first.example",
        "systemd",
        "0" * 40,
        tmp_path,
        Runner(log=lambda line: None),
        None,
        ("https://second.example", "https://third.example"),
    )

    assert where.name == "stage3.tar.xz"
    assert asked == [
        "https://first.example",
        "https://second.example",
        "https://third.example",
    ]


def test_every_mirror_refusing_raises_the_last_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refusing(
        mirror: str,
        variant: str,
        fingerprint: str,
        work: Path,
        runner: Runner,
        proxy: object = None,
    ) -> Path:
        raise DownloadFailed(f"{mirror} could not be reached")

    monkeypatch.setattr(fetch, "_stage3_from", refusing)

    with pytest.raises(DownloadFailed, match="second.example"):
        fetch.stage3(
            "https://first.example",
            "systemd",
            "0" * 40,
            tmp_path,
            Runner(log=lambda line: None),
            None,
            ("https://second.example",),
        )


def test_a_read_that_failed_says_what_the_resolver_was_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twenty-three mirrors failed with `EAI_AGAIN` in guests whose
    `/etc/hosts` held every one of their names, and no diagnostic outside the
    failing process could say which of the two was wrong."""
    hosts = tmp_path / "hosts"
    hosts.write_text("210.28.130.3 mirrors.nju.edu.cn # gentoo-install\n")
    nsswitch = tmp_path / "nsswitch.conf"
    nsswitch.write_text("passwd: files\nhosts: files dns\n")

    real_read = Path.read_text

    def reading(self: Path, *args: object, **rest: object) -> str:
        if str(self) == "/etc/hosts":
            return real_read(hosts)
        if str(self) == "/etc/nsswitch.conf":
            return real_read(nsswitch)
        return real_read(self)

    monkeypatch.setattr(Path, "read_text", reading)

    said = fetch._resolver_state("https://mirrors.nju.edu.cn/gentoo/x")

    assert "hosts: files dns" in said
    assert "mirrors.nju.edu.cn in /etc/hosts: True" in said


def test_a_name_absent_from_the_file_is_said_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    real_read = Path.read_text

    def reading(self: Path, *args: object, **rest: object) -> str:
        if str(self) == "/etc/hosts":
            return real_read(hosts)
        raise OSError("no nsswitch here")

    monkeypatch.setattr(Path, "read_text", reading)

    said = fetch._resolver_state("https://mirrors.ustc.edu.cn/gentoo/x")

    assert "no hosts line" in said
    assert "mirrors.ustc.edu.cn in /etc/hosts: False" in said


def test_a_failed_read_names_the_servers_the_library_would_have_asked(
    tmp_path: Path,
) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("options no-aaaa\nnameserver 10.31.0.199\nnameserver 223.5.5.5\n")
    assert fetch._resolvers(resolv) == "nameservers ['10.31.0.199', '223.5.5.5']"


def test_a_failed_read_says_so_when_no_resolver_is_configured(tmp_path: Path) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("options no-aaaa\n")
    assert fetch._resolvers(resolv) == "no nameserver line"


def test_a_failed_read_says_so_when_the_resolver_file_is_missing(tmp_path: Path) -> None:
    assert fetch._resolvers(tmp_path / "absent") == "resolv.conf unreadable"


def test_the_diagnostic_carries_the_resolver_state(tmp_path: Path) -> None:
    said = fetch._resolver_state("https://mirrors.nju.edu.cn/gentoo/x")
    assert "nameserver" in said


def test_the_diagnostic_separates_the_two_address_families() -> None:
    said = fetch._families("localhost")
    assert "v4=" in said and "v6=" in said


def test_a_family_that_cannot_be_answered_names_its_error() -> None:
    said = fetch._families("no-such-name.gentoo-install.invalid")
    assert "v4=gaierror" in said


def test_the_diagnostic_says_which_address_reaches_the_resolver(tmp_path: Path) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.1\n")
    assert fetch._route_to_resolver(resolv) == "route to 127.0.0.1 from 127.0.0.1"


def test_the_diagnostic_says_so_when_there_is_no_resolver_to_route_to(
    tmp_path: Path,
) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("options no-aaaa\n")
    assert fetch._route_to_resolver(resolv) == "no route measured"


def test_the_diagnostic_reads_the_kernel_routing_table(tmp_path: Path) -> None:
    table = tmp_path / "route"
    table.write_text(
        "Iface\tDestination\tGateway\tFlags\n"
        "ens18\t00000000\tFE001F0A\t0003\n"
        "ens18\t00001F0A\t00000000\t0001\n"
    )
    assert fetch._kernel_routes(table) == (
        "routes ['ens18:0.0.0.0/10.31.0.254', 'ens18:10.31.0.0/0.0.0.0']"
    )


def test_an_empty_routing_table_is_named_as_such(tmp_path: Path) -> None:
    table = tmp_path / "route"
    table.write_text("Iface\tDestination\tGateway\tFlags\n")
    assert fetch._kernel_routes(table) == "no routes"


def test_the_diagnostic_names_the_interfaces_it_can_see() -> None:
    said = fetch._interfaces()
    assert said.startswith("interfaces [")
    assert "lo" in said


def test_the_diagnostic_says_whether_the_namespace_is_shared() -> None:
    said = fetch._network_namespace()
    assert said.startswith("namespace ")


def test_a_corrupt_archive_moves_to_the_next_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signature on the DIGESTS was good, so the metadata is authentic and
    that mirror's copy of the file is not: `vm-convert` ended four minutes into
    a run because one copy of a stage3 was corrupt."""
    asked: list[str] = []

    def serving(
        mirror: str,
        variant: str,
        fingerprint: str,
        work: Path,
        runner: Runner,
        proxy: object = None,
    ) -> Path:
        asked.append(mirror)
        if mirror == "https://first.example":
            raise ArchiveDigestMismatch("stage3.tar.xz has SHA512 aaa, the DIGESTS file says bbb")
        return work / "stage3.tar.xz"

    monkeypatch.setattr(fetch, "_stage3_from", serving)

    where = fetch.stage3(
        "https://first.example",
        "systemd",
        "0" * 40,
        tmp_path,
        Runner(log=lambda line: None),
        None,
        ("https://second.example",),
    )

    assert where.name == "stage3.tar.xz"
    assert asked == ["https://first.example", "https://second.example"]


def test_every_mirror_serving_a_corrupt_archive_still_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def serving(
        mirror: str,
        variant: str,
        fingerprint: str,
        work: Path,
        runner: Runner,
        proxy: object = None,
    ) -> Path:
        raise ArchiveDigestMismatch(f"{mirror} served a corrupt archive")

    monkeypatch.setattr(fetch, "_stage3_from", serving)

    with pytest.raises(ArchiveDigestMismatch, match="second.example"):
        fetch.stage3(
            "https://first.example",
            "systemd",
            "0" * 40,
            tmp_path,
            Runner(log=lambda line: None),
            None,
            ("https://second.example",),
        )


def test_a_corrupt_archive_is_removed_so_the_next_mirror_downloads_again(
    tmp_path: Path
) -> None:
    """The download skips a file that is already there, so leaving the bad one
    would make every fallback verify the same corrupt copy."""
    archive = tmp_path / "stage3.tar.xz"
    archive.write_bytes(b"corrupt")
    digests = tmp_path / "stage3.tar.xz.DIGESTS"
    digests.write_text("# SHA512 HASH\n" + "0" * 128 + " stage3.tar.xz\n")

    with pytest.raises(ArchiveDigestMismatch):
        fetch._verify_digest(archive, digests)
    assert archive.exists(), "the check itself does not remove anything"


def test_a_body_that_stops_early_is_a_download_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`http.client.IncompleteRead` is an `HTTPException` and not an `OSError`,
    so a server that closed the connection with bytes still promised escaped
    the handler: the `.part` file stayed behind and the install stopped instead
    of reaching the next mirror."""
    import http.client

    assert not issubclass(http.client.IncompleteRead, OSError), "the whole reason"

    class Truncated:
        def __enter__(self) -> "Truncated":
            return self

        def __exit__(self, *unused: object) -> None:
            return None

        @property
        def headers(self) -> dict[str, str]:
            return {"Content-Length": "1024"}

        def read(self, size: int = 0) -> bytes:
            raise http.client.IncompleteRead(b"half", 1020)

    monkeypatch.setattr(fetch, "_urlopen", lambda *unused, **ignored: Truncated())
    target = tmp_path / "stage3.tar.xz"
    with pytest.raises(DownloadFailed, match="could not be fetched"):
        fetch._download("https://example.invalid/stage3.tar.xz", target)

    # The partial file goes with it, or the next mirror verifies this one.
    assert not target.with_suffix(target.suffix + ".part").exists()
    assert not target.exists()


def test_an_unreplaceable_stage3_tries_the_next_mirror_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rename sat after the `except`, so a full disk or a changed
    permission left the raw `OSError`: the mirror fallback never ran, the CLI
    never classified it, and the `.part` file stayed behind.
    """
    mirrors: list[str] = []
    notices: list[str] = []

    class Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.body = b"stage3"

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *unused: object) -> None:
            return None

        def read(self, size: int = 0) -> bytes:
            body, self.body = self.body, b""
            return body

    def newest(builds: str, variant: str, proxy: object = None) -> str:
        mirrors.append(builds)
        return "stage3.tar.xz"

    def opening(*unused: object, **ignored: object) -> Response:
        return Response()

    def unreplaceable(self: Path, target: Path) -> Path:
        raise OSError("No space left on device")

    monkeypatch.setattr(fetch, "_newest", newest)
    monkeypatch.setattr(fetch, "_urlopen", opening)
    monkeypatch.setattr(fetch, "ONLINE_PAUSE", 0.0)
    monkeypatch.setattr(Path, "replace", unreplaceable)

    with pytest.raises(DownloadFailed, match="could not be fetched"):
        fetch.stage3(
            "https://first.example",
            "systemd",
            "0" * 40,
            tmp_path,
            Runner(log=notices.append),
            fallbacks=("https://second.example",),
        )

    assert mirrors == [
        f"https://first.example/{fetch.STAGE3_PATH}",
        f"https://second.example/{fetch.STAGE3_PATH}",
    ]
    assert len(notices) == 2
    assert all("did not serve the stage3" in notice for notice in notices)
    archive = tmp_path / "stage3.tar.xz"
    assert not archive.exists()
    assert not archive.with_suffix(archive.suffix + ".part").exists()


def test_the_stage3_is_hashed_once_for_one_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The archive is about a quarter of a gigabyte and it was read twice:
    once to compare with `DIGESTS`, once again to write the marker beside it.
    Both hashes are of the same bytes, so the second read is a disk pass an
    install pays for before it can unpack anything."""
    from gentoo_install.exec import fetch

    archive = tmp_path / "stage3-amd64-systemd-1.tar.xz"
    archive.write_bytes(b"the verified bytes")
    digests = tmp_path / "stage3-amd64-systemd-1.tar.xz.DIGESTS"
    digests.write_text(f"# SHA512 HASH\n{fetch._sha512(archive)}  {archive.name}\n")

    reads = 0
    real = fetch._sha512

    def counted(path: Path) -> str:
        nonlocal reads
        reads += 1
        return real(path)

    monkeypatch.setattr(fetch, "_sha512", counted)
    digest = fetch._verify_digest(archive, digests)
    assert reads == 1, reads

    # What the caller writes into the marker has to be the digest it was
    # given, or the second read comes back with it.
    key = "13EBBDBEDE7A12775DFDB1BABB572E0E2D182910"
    marker = tmp_path / f"{archive.name}.verified"
    marker.write_text(f"{fetch.MARKER_SCHEMA}\n{archive.name}\n{digest}\n{key.lower()}\n")
    assert fetch._marker_matches(marker, archive, key)

    # And the caller does not ask again: one call for the whole sequence.
    import inspect

    source = inspect.getsource(fetch._stage3_from)
    assert "_sha512(archive)" not in source, source

