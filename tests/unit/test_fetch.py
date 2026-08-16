# SPDX-License-Identifier: GPL-2.0-or-later
"""The stage3 fetch, and what it does when a mirror will not answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_install.errors import DownloadFailed
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
