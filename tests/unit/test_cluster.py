# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from typing import cast

import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest

from gentoo_install.model.config import MirrorRegion, Sync
from tests.vm import cluster

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
from tests.vm.console import ConsoleTimeout, SerialConsole
from tests.vm.proxmox import Api, Node, ProxmoxNotFound, VMID_FIRST, VMID_LAST


class WorkerFailure(Exception):
    pass


class FailedThread:
    def __init__(
        self,
        target: Callable[..., None],
        args: tuple[object, ...],
        daemon: bool,
    ) -> None:
        self._target = target
        self._args = args
        self.daemon = daemon

    def start(self) -> None:
        try:
            self._target(*self._args)
        except WorkerFailure:
            return

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        return None


class FakeApi:
    def __init__(self) -> None:
        self.allocations: list[int] = []
        self.created: dict[int, str] = {}

    def nodes(self) -> list[Node]:
        return [Node("node", 64 * 1024**3, 16, free_cores=16.0)]

    def free_vmid(self, held: frozenset[int] = frozenset()) -> int:
        vmid = next(
            candidate
            for candidate in range(VMID_FIRST, VMID_LAST + 1)
            if candidate not in held
        )
        self.allocations.append(vmid)
        return vmid

    def call(self, method: str, path: str, **form: Any) -> Any:
        if method == "POST" and path.endswith("/qemu"):
            vmid = int(form["vmid"])
            self.created[vmid] = str(form["tags"])
            return "UPID:create"
        vmid = int(path.split("/qemu/", 1)[1].split("/", 1)[0])
        if vmid not in self.created:
            raise ProxmoxNotFound(f"VM {vmid} does not exist")
        if method == "GET" and path.endswith("/config"):
            return {"tags": self.created[vmid]}
        if method == "GET" and path.endswith("/status/current"):
            return {"status": "stopped"}
        if method == "DELETE":
            del self.created[vmid]
            return "UPID:delete"
        raise AssertionError(f"unexpected request: {method} {path}")

    def wait(self, node: str, upid: str, patience: float = 1800.0) -> None:
        return None

    def remove_iso(self, node: str, name: str) -> str:
        return ""


class Threading:
    Thread = FailedThread


def _confined(path: Path) -> Path:
    return path


def _reconcile(api: FakeApi, workdir: Path) -> None:
    return None


def _rewrite(
    jobs: list[cluster.Job],
    into: Path,
    region: MirrorRegion,
    sync: Sync,
    public_key: str = "",
    site: str = "",
) -> Path:
    return into


def _build_driver(path: Path, **kwargs: Any) -> Path:
    return path


def _revision(path: Path) -> str:
    return "revision"


def _remote_name(path: Path) -> str:
    return path.name


def _current_minimal() -> tuple[str, list[str], str]:
    return "install.iso", ["https://example.invalid/install.iso"], "sha512"


def _prepare(*args: object, **kwargs: object) -> None:
    return None


def _worker_raises(*args: object) -> None:
    raise WorkerFailure("worker stopped before answering")


def test_worker_failure_reports_outcome_and_releases_vmid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = FakeApi()

    def api_factory() -> FakeApi:
        return api

    monkeypatch.setattr(cluster, "Api", api_factory)
    monkeypatch.setattr(cluster, "confined", _confined)
    monkeypatch.setattr(cluster, "reconcile", _reconcile)
    monkeypatch.setattr(cluster, "rewrite_fixtures", _rewrite)
    monkeypatch.setattr(cluster, "build_driver", _build_driver)
    monkeypatch.setattr(cluster, "revision_identity", _revision)
    monkeypatch.setattr(cluster, "remote_name", _remote_name)
    monkeypatch.setattr(cluster, "current_minimal", _current_minimal)
    monkeypatch.setattr(cluster, "prepare", _prepare)
    monkeypatch.setattr(cluster, "answer_once", _worker_raises)
    monkeypatch.setattr(cluster, "threading", Threading)
    monkeypatch.setattr(cluster, "WATCH_EVERY", 0.001)
    monkeypatch.setattr(cluster, "POLL_WHILE_QUEUED", 0.001)

    jobs = [
        cluster.Job("first", tmp_path / "first.toml"),
        cluster.Job("second", tmp_path / "second.toml"),
    ]
    outcomes = cluster.run(jobs, tmp_path / "work", limit=1)

    assert [outcome.name for outcome in outcomes] == ["first", "second"]
    assert all(outcome.verdict is cluster.Verdict.ERROR for outcome in outcomes)
    assert all(outcome.vmid == VMID_FIRST for outcome in outcomes)
    assert api.allocations == [VMID_FIRST, VMID_FIRST]
    assert api.created == {}


def test_non_double_memory_reservation_is_admitted_in_exact_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cluster, "HEAVY_MEMORY_MIB", 6144)
    job = cluster.Job("odd-sized", tmp_path / "odd-sized.toml", heavy=True)
    reservation = 6 * 1024**3
    node = Node(
        "node",
        cluster.NODE_HEADROOM_BYTES + reservation,
        16,
        free_cores=16.0,
    )

    class CapacityApi:
        def nodes(self) -> list[Node]:
            return [node]

    slots = cluster.free_slots(cast(Api, CapacityApi()))
    assert slots == [node]
    assert cluster.room_for(node, job)

    class Guest:
        def stop(self) -> None:
            return None

        def destroy(self) -> None:
            return None

    execution = cluster.Running(
        Guest(),
        cluster.Watchdog(tmp_path / "odd-sized.log", lambda: 0),
        job.reservation_bytes,
    )
    dispatched = job.dispatch(
        node.name,
        VMID_FIRST,
        tmp_path / "odd-sized.lease",
        tmp_path / "odd-sized.log",
        execution,
    )
    assert job.reservation_bytes == reservation
    assert execution.reservation_bytes == reservation
    assert cluster._reserved_bytes({job.name: dispatched}) == {node.name: reservation}


class TimedChannel:
    def __init__(self, clock: list[float], output_at: float | None = None) -> None:
        self._clock = clock
        self._output_at = output_at
        self._answered = False

    def recv(self, size: int) -> bytes:
        self._clock[0] += 1.0
        if (
            self._output_at is not None
            and self._clock[0] >= self._output_at
            and not self._answered
        ):
            self._answered = True
            return b"MARK_1_DONE\n"
        return b""

    def sendall(self, data: bytes) -> None:
        return None

    def close(self) -> None:
        return None

    @property
    def closed(self) -> bool:
        return False


def _timed_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock: list[float],
    counters: Callable[[], int | None],
    output_at: float | None = None,
) -> tuple[cluster.Reconnecting, cluster.Watchdog]:
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    serial = SerialConsole(TimedChannel(clock, output_at), BytesIO())
    link = cluster.Reconnecting(lambda: serial)
    watch = cluster.Watchdog(tmp_path / "install.log", counters)
    return link, watch


def test_install_wait_continues_when_silent_guest_moves_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    traffic = [0]

    def counters() -> int:
        traffic[0] += cluster.QUIET_BYTES * 2
        return traffic[0]

    link, watch = _timed_wait(monkeypatch, tmp_path, clock, counters, output_at=3.0)
    link.wait_for("install", timeout=5.0, idle=2.0, watch=watch)

    assert clock[0] == 3.0
    assert traffic[0] == cluster.QUIET_BYTES * 2


def test_install_wait_names_silent_console_and_flat_counters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    link, watch = _timed_wait(monkeypatch, tmp_path, clock, lambda: 0)
    watch.log.write_bytes(b"output before the idle window\n")

    with pytest.raises(ConsoleTimeout) as raised:
        link.wait_for("install", timeout=5.0, idle=2.0, watch=watch)

    message = str(raised.value)
    assert clock[0] == 2.0
    assert "console was silent" in message
    assert "counters were flat" in message
    assert "0 -> 0 bytes" in message


def test_run_ceiling_ends_silent_guest_that_keeps_moving_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    readings = [0]

    def counters() -> int:
        readings[0] += 1
        return readings[0] * cluster.QUIET_BYTES * 2

    link, watch = _timed_wait(monkeypatch, tmp_path, clock, counters)
    with pytest.raises(ConsoleTimeout, match="never matched"):
        link.wait_for("install", timeout=5.0, idle=2.0, watch=watch)

    assert clock[0] == 5.0
    assert readings[0] == 2


def test_the_init_check_asks_the_running_init_not_a_file_listing() -> None:
    """`ls -l /sbin/init` names systemd through its symlink and says nothing on
    OpenRC, where `/sbin/init` is `sys-apps/sysvinit`'s own binary: a guest
    answered `-rwxr-xr-x 1 root root 49064 Nov 22 2025 /sbin/init` and the
    check looked for `openrc` in it. Two fixtures failed every round on that.
    """
    from pathlib import Path

    from gentoo_install.exec.config import load
    from gentoo_install.model.config import InitSystem
    from tests.vm.cluster import _asked_for

    for fixture, wanted in (("vm-lvm", "openrc"), ("vm-binpkg", None)):
        installation = load(Path(f"tests/fixtures/{fixture}.toml"))
        init = next(one for one in _asked_for(installation) if one[0] == "init")
        assert "ls -l /sbin/init" not in init[1], init
        assert "/run/openrc" in init[1], init
        expected = "systemd" if installation.system.init is InitSystem.SYSTEMD else "openrc"
        assert init[2] == expected, init
        if wanted:
            assert init[2] == wanted, init


def test_a_pinned_site_moves_every_address_that_follows_a_mirror(tmp_path: Path) -> None:
    """This cluster is in China. A run that reaches for `distfiles.gentoo.org`
    or `github.com` measures the way out of the country: three rounds died at
    the stage3 fetch, and a node pulling a 3 MiB template from
    `download.proxmox.com` connected, was answered 200, and received nothing
    for fifteen minutes."""
    from gentoo_install.exec.config import load
    from gentoo_install.model import mirrors

    job = cluster.Job("vm-binpkg", FIXTURES / "vm-binpkg.toml")

    cluster.rewrite_fixtures([job], tmp_path, MirrorRegion.CN, Sync.GIT, "", "nju")
    moved = load(tmp_path / "vm-binpkg.toml").portage

    assert moved.mirrors.site == "nju"
    assert moved.mirrors.gentoo_zh.value == "nju"
    assert mirrors.gentoo_binhost(moved.mirrors.region, moved.mirrors.site) == (
        "https://mirrors.nju.edu.cn/gentoo/releases/amd64/binpackages/23.0/x86-64"
    )
    assert mirrors.gentoozh(moved.mirrors.gentoo_zh).git == (
        "https://mirror.nju.edu.cn/git/gentoo-zh.git"
    )


def test_no_site_leaves_the_region_to_choose(tmp_path: Path) -> None:
    from gentoo_install.exec.config import load

    job = cluster.Job("vm-binpkg", FIXTURES / "vm-binpkg.toml")

    cluster.rewrite_fixtures([job], tmp_path, MirrorRegion.CN, Sync.GIT)
    moved = load(tmp_path / "vm-binpkg.toml").portage

    assert moved.mirrors.site == ""
    assert moved.mirrors.gentoo_zh.value == "cernet"


def test_the_reachability_probe_asks_every_resolver_and_changes_nothing() -> None:
    probe = cluster.REACHABILITY_PROBE
    for one in cluster.GUEST_RESOLVERS:
        assert one in probe
    assert "> /etc/resolv.conf" not in probe
    assert "getent ahostsv4" in probe
    assert "getent ahosts mirrors" in probe
    assert "GNU_LIBC_VERSION" in probe
    assert "ip -4 -brief address show" in probe
    assert "ip -4 route show" in probe


def test_the_network_wait_measures_reachability_once_the_probe_answers() -> None:
    link = _AnsweringLink(cluster.NETWORK_UP.encode())
    cluster.wait_for_network(cast(cluster.Reconnecting, link), vmid=9301)
    assert cluster.REACHABILITY_PROBE in link.ran


class _AnsweringLink:
    """A link whose network probe succeeds on the first pass."""

    def __init__(self, answer: bytes) -> None:
        self.answer = answer
        self.ran: list[str] = []

    def expect_output(self, command: str, timeout: float = 0.0) -> bytes:
        self.ran.append(command)
        return self.answer

    def run(self, command: str, timeout: float = 0.0) -> None:
        self.ran.append(command)


def test_the_resolvers_are_written_before_the_first_network_probe() -> None:
    link = _AnsweringLink(cluster.NETWORK_UP.encode())
    cluster.wait_for_network(cast(cluster.Reconnecting, link), vmid=9301)
    written = link.ran.index(cluster.use_our_resolvers())
    probed = next(i for i, one in enumerate(link.ran) if "NETWORK_%s" in one)
    assert written < probed


def test_the_interface_configuration_no_longer_carries_the_resolvers() -> None:
    """Two writers of one file is how the fallback undid the early write."""
    assert "/etc/resolv.conf" not in cluster.configure_statically("10.31.0.150")


def test_the_guest_holds_its_own_address_once_the_probe_answers() -> None:
    """A lease from the segment's DHCP server lapses mid-install: sixty-one
    lookups in round 26 answered `ENETUNREACH` from an installer that had
    reached a mirror over the same interface a minute earlier."""
    link = _AnsweringLink(cluster.NETWORK_UP.encode())
    cluster.wait_for_network(cast(cluster.Reconnecting, link), vmid=9301)
    configured = link.ran.index(cluster.configure_statically(cluster.static_address(9301)))
    probed = next(i for i, one in enumerate(link.ran) if "NETWORK_%s" in one)
    assert probed < configured


def test_the_address_does_not_depend_on_the_default_route_being_absent() -> None:
    """The guard meant a guest that came up on DHCP never got one of its own,
    so it had nothing left when the lease lapsed: sixty-one lookups answered
    `ENETUNREACH` from an installer that had reached a mirror a minute
    earlier."""
    command = cluster.configure_statically("10.31.0.150")
    assert "route show default | grep -q . || {" not in command
    # SIGKILL: a term makes dhcpcd release the lease and deconfigure the
    # interface, which is what emptied the routing table under the installer.
    assert "pkill -KILL -x dhcpcd" in command
    assert "pkill -x dhcpcd" not in command
    assert "addr add 10.31.0.150/24" in command
