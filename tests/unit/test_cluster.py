# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from typing import Any, cast

import re
import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest

from gentoo_install.model.config import MirrorRegion, Sync
from tests.vm import cluster

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
from tests.vm.console import ConsoleTimeout, SerialConsole, command_done
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
    unlock_addresses: object = None,
    distfiles: str = "",
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
    # The occupancy probe shells out to `arping` against a segment this machine
    # is not on, so without this the test answers whatever the host's network
    # happens to say and the whole suite was run with it deselected.
    monkeypatch.setattr(cluster, "_address_is_taken", lambda address: False)

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
        cluster.Watchdog(tmp_path / "odd-sized.log", lambda: (0, 0.0)),
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
    counters: Callable[[], tuple[int, float] | None],
    output_at: float | None = None,
) -> tuple[cluster.Reconnecting, cluster.Watchdog]:
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    serial = SerialConsole(TimedChannel(clock, output_at), BytesIO())
    link = cluster.Reconnecting(lambda: serial)
    watch = cluster.Watchdog(tmp_path / "install.log", counters, where="infra-node2")
    return link, watch


def test_install_wait_continues_when_silent_guest_moves_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    traffic = [0]

    def counters() -> tuple[int, float]:
        traffic[0] += cluster.QUIET_BYTES * 2
        return traffic[0], 0.0

    link, watch = _timed_wait(monkeypatch, tmp_path, clock, counters, output_at=3.0)
    link.wait_for("install", timeout=5.0, idle=2.0, watch=watch)

    assert clock[0] == 3.0
    assert traffic[0] == cluster.QUIET_BYTES * 2


def test_install_wait_names_silent_console_and_flat_counters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    link, watch = _timed_wait(monkeypatch, tmp_path, clock, lambda: (0, 0.0))
    watch.log.write_bytes(b"output before the idle window\n")

    with pytest.raises(ConsoleTimeout) as raised:
        link.wait_for("install", timeout=5.0, idle=2.0, watch=watch)

    message = str(raised.value)
    assert clock[0] == 2.0
    assert "console was silent" in message
    assert "counters were flat" in message
    assert "0 -> 0 bytes" in message
    # And where, because the cluster's other tenants are not this campaign's:
    # a guest that goes to cpu 0.00 mid-compile is a different finding on a
    # node at 100% than on an idle one, and the verdict was the only record.
    assert "on infra-node2" in message, message


def test_run_ceiling_ends_silent_guest_that_keeps_moving_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    readings = [0]

    def counters() -> tuple[int, float]:
        readings[0] += 1
        return readings[0] * cluster.QUIET_BYTES * 2, 0.0

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
    assert "ip -brief link show" in probe
    assert "dmesg | tail" in probe


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

    def run(
        self, command: str, timeout: float = 0.0, *, repeatable: bool = True
    ) -> None:
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


def test_the_network_is_measured_once_more_after_the_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The post-install probe has to precede collection of the guest results."""
    events: list[str] = []
    repeatability: dict[str, bool] = {}

    class Guest:
        vmid = 9301

        def start(self) -> None:
            return None

        def reset(self) -> None:
            return None

        def destroy(self) -> None:
            return None

    class Link:
        console = object()

        def run(
            self, command: str, timeout: float = 0.0, *, repeatable: bool = True
        ) -> None:
            events.append(command)
            repeatability[command] = repeatable

        def wait_for(
            self, command: str, timeout: float, idle: float, watch: cluster.Watchdog
        ) -> None:
            events.append(command)

    def collect(guest: object, link: object, log: Path) -> dict[str, bytes]:
        events.append("collect")
        return {"install.rc": b"1"}

    guest = Guest()
    log = tmp_path / "network.log"
    job = cluster.Job("network", FIXTURES / "mbr-edit.toml")
    held = cluster.Running(
        guest=cast(Any, guest),
        watch=cluster.Watchdog(log, lambda: (0, 0.0)),
        reservation_bytes=0,
        created=True,
    )
    link = Link()
    monkeypatch.setattr(cluster.Reconnecting, "to", lambda guest, log: link)
    monkeypatch.setattr(cluster, "reach_prompt", lambda link: None)
    monkeypatch.setattr(cluster, "append_to_cmdline", lambda link, extra: None)
    monkeypatch.setattr(cluster, "wait_for_network", lambda link, vmid, address: None)
    monkeypatch.setattr(cluster, "stage_passphrases", lambda link, config: None)
    monkeypatch.setattr(cluster, "collect", collect)
    outcome = cluster.install_one(
        cast(Api, object()), "node", job, "driver.iso", tmp_path, execution=held
    )

    installed = next(index for index, event in enumerate(events) if "install.sh --config" in event)
    probes = [index for index, event in enumerate(events) if event == cluster.REACHABILITY_PROBE]
    assert outcome.verdict is cluster.Verdict.FAIL
    assert len(probes) == 2
    assert probes[0] < installed < probes[1] < events.index("collect")
    partition = next(one for one in events if one.startswith("parted --script"))
    assert not repeatability[partition]


def test_the_keeper_puts_the_address_back_and_counts_it() -> None:
    """Round 32 measured the interface up with its address immediately before
    the installer and up with none after it, with nothing in the kernel log and
    no network command in the installer's own run list."""
    import subprocess

    written = cluster.keep_the_address("10.31.0.152")
    for command in written:
        assert subprocess.run(["bash", "-n", "-c", command], capture_output=True).returncode == 0
        assert len(command) < cluster.CONSOLE_LINE_BYTES, command
    assert any("10.31.0.152/24" in one for one in written)
    assert " &" in written[-1], "the keeper outlives the command that starts it"
    # The console wrapper appends `; printf MARK...` to whatever it is given.
    composed = subprocess.run(["bash", "-n", "-c", f"{written[-1]}; true"], capture_output=True)
    assert composed.returncode == 0, written[-1]
    assert any(cluster.KEEPER_LOG in one for one in written)
    assert cluster.KEEPER_LOG in cluster.REACHABILITY_PROBE


def test_the_keeper_starts_once_the_guest_has_its_address() -> None:
    link = _AnsweringLink(cluster.NETWORK_UP.encode())
    cluster.wait_for_network(cast(cluster.Reconnecting, link), vmid=9301)
    configured = link.ran.index(cluster.configure_statically(cluster.static_address(9301)))
    kept = link.ran.index(cluster.keep_the_address(cluster.static_address(9301))[-1])
    assert configured < kept


def test_a_console_that_keeps_closing_gives_up_instead_of_burning_the_ceiling() -> None:
    """A node whose proxy is refusing grants a session and closes it at once,
    so every reopen succeeds and nothing ever raises: two guests sat at step 11
    for forty minutes with their installs running and no way to read them."""
    from tests.vm.console import ConsoleClosed

    opened: list[int] = []

    class Closing:
        def send(self, line: str) -> None:
            pass

        def send_raw(self, keys: str) -> None:
            pass

        def snapshot(self, seconds: float) -> bytes:
            return b""

        @property
        def closed(self) -> bool:
            return False

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            raise ConsoleClosed("termproxy disconnected")

        def close(self) -> None:
            pass

    def open_console() -> Closing:
        opened.append(1)
        return Closing()

    link = cluster.Reconnecting(open_console, tries=1000)
    with pytest.raises(ConsoleClosed, match="kept closing"):
        link.expect("never", timeout=600.0)
    assert len(opened) <= cluster.REOPEN_CEILING + 2, opened


def test_a_conversion_job_installs_a_machine_before_it_converts_one() -> None:
    """`mode = "in-place"` carries no device graph, so there is nothing for the
    scheduler to create. The job installs an ordinary fixture first and runs
    the conversion against what that produced."""
    job = cluster.fixtures(["vm-convert"])[0]
    assert job.name == "vm-convert"
    assert job.fixture.name == cluster.CONVERSION_BASE
    assert job.convert_to is not None and job.convert_to.name == "vm-convert.toml"


def test_an_ordinary_job_converts_nothing() -> None:
    job = cluster.fixtures(["vm-xfs"])[0]
    assert job.convert_to is None


def test_the_conversion_base_is_a_layout_the_conversion_accepts() -> None:
    """A root below LUKS, LVM or mdraid is refused by name, so a base with one
    would fail every run for a reason that has nothing to do with the swap."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.device import Luks, LogicalVolume, MdRaid

    base = load(cluster.REPOSITORY / "tests" / "fixtures" / cluster.CONVERSION_BASE)
    for stacked in (Luks, LogicalVolume, MdRaid):
        assert not base.disk.graph.of_type(stacked), stacked.__name__


def test_the_converted_machine_is_read_back_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """A converted machine that reaches a login prompt with the old hostname
    booted the system it was replacing, so the same reader has to run again."""
    import inspect

    code = inspect.getsource(cluster.convert_and_check)
    assert "boot_and_check(" in code
    assert "install.sh --config fixtures/" in code
    assert "wait_for_network(" in code, "the installed system has to reach a mirror too"


class _Screen:
    """What `Reconnecting.console` offers a verdict that has to say what the
    guest was showing. The real one is a live console; a test supplies the
    tail it wants to see in the message."""

    def __init__(self, trailing: bytes = b"") -> None:
        self.trailing = trailing

    def snapshot(self, seconds: float) -> bytes:
        return self.trailing


class _LoginConsole:
    """A serial line that behaves like `login` on the guests.

    It echoes whatever arrives before the prompt has turned the echo off, which
    is what made `vm-lvm` answer `Login incorrect` after a complete install.
    """

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.sent: list[str] = []
        self.answers: list[bytes] = []
        self.console = _Screen(b"lvmbox login: ")

    def respond(self, line: str) -> None:
        self.sent.append(line)
        if line == "root":
            self.answers.append(b"Password: ")
        elif self.refusals > 0:
            self.refusals -= 1
            self.answers.append(b"Login incorrect\r\nlogin: ")
        else:
            self.answers.append(b"lvmbox ~ # ")

    def observe(self, pattern: str, timeout: float = 0.0) -> bytes:
        return self.answers.pop(0) if self.answers else b""


def test_a_refused_password_is_offered_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """`login` writes `Password:` and then turns the echo off; a password sent
    inside that window is echoed and read as an empty one."""
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    console = _LoginConsole(refusals=1)
    assert cluster._log_in(cast(cluster.Reconnecting, console), "install") == ""
    assert console.sent.count("install") == 2, console.sent


def test_a_login_that_is_refused_every_time_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    console = _LoginConsole(refusals=99)
    said = cluster._log_in(cast(cluster.Reconnecting, console), "install")
    assert "refused every login" in said


class _EchoingConsole:
    """`login` before it has turned the echo off: the password comes back on
    the console and is read as an empty one.

    Taken from `openrc-sdboot`'s log, where `Password: ` was followed by
    `install` on its own line and then `Login incorrect`, against `vm-lvm`'s
    working exchange where nothing appeared between the two.
    """

    def __init__(self, echoes: int) -> None:
        self.echoes = echoes
        self.sent: list[str] = []
        self.settles: list[float] = []
        self.answers: list[bytes] = []
        self.console = _Screen(b"openrcsdbox login: ")

    def respond(self, line: str) -> None:
        self.sent.append(line)
        if line == "root":
            self.answers.append(b"Password: ")
        elif self.echoes > 0:
            self.echoes -= 1
            self.answers.append(f"{line}\r\n\r\nLogin incorrect\r\nlogin: ".encode())
        else:
            self.answers.append(b"openrcsdbox ~ # ")

    def observe(self, pattern: str, timeout: float = 0.0) -> bytes:
        return self.answers.pop(0) if self.answers else b""


def test_a_password_the_console_echoed_is_not_counted_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`openrc-sdboot` lost this race twice. `login` gives up after three
    attempts of its own, so a race counted against the harness's own budget
    ends the login before a clean attempt is ever made."""
    settles: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: settles.append(seconds))
    console = _EchoingConsole(echoes=3)
    assert cluster._log_in(cast(cluster.Reconnecting, console), "install") == ""
    assert console.sent.count("install") == 4, console.sent

    # Every sleep, unfiltered: the settle's second step is `AGETTY_FLUSHES_AFTER`
    # itself now that the first is zero, and a filter by value drops it.
    assert settles == [
        cluster.PASSWORD_ECHO_OFF_AFTER + n * cluster.PASSWORD_ECHO_BACKOFF
        for n in range(4)
    ], settles

    # Negative control: a refusal with no echo is the installed system saying
    # no, and it must end after the ordinary budget rather than be absorbed.
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    silent = _LoginConsole(refusals=99)
    assert "refused every login" in cluster._log_in(
        cast(cluster.Reconnecting, silent), "install"
    )
    assert silent.sent.count("install") == cluster.LOGIN_TRIES, silent.sent

    # Negative control: an echo that never stops still ends, so a guest whose
    # console echoes everything cannot hold the schedule open.
    forever = _EchoingConsole(echoes=999)
    assert "refused every login" in cluster._log_in(
        cast(cluster.Reconnecting, forever), "install"
    )
    assert (
        forever.sent.count("install")
        == cluster.LOGIN_TRIES + cluster.PASSWORD_ECHO_CATCHES
    ), forever.sent


def test_the_password_waits_for_the_echo_to_be_turned_off() -> None:
    import inspect

    code = inspect.getsource(cluster._log_in)
    assert "PASSWORD_ECHO_OFF_AFTER" in code
    assert code.index("PASSWORD_ECHO_OFF_AFTER") < code.index("link.respond(password)")


class _Stoppable:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def destroy(self) -> None:
        self.stopped = True


def _held(log: Path, moved: bool, stuck: bool, busy: bool = False) -> object:
    """`busy` gives the guest moving byte counters, which is a guest whose disk
    is working while its console has gone."""
    counters = iter(range(0, 10**9, cluster.QUIET_BYTES * 2))
    watch = cluster.Watchdog(
        log=log,
        counters=(lambda: (next(counters), 0.0)) if busy else (lambda: (0, 0.0)),
    )
    # One pass so the watchdog has seen the log: the first call always reports
    # growth, from nothing to whatever is there.
    watch.moved()
    watch.strikes = cluster.WATCH_STRIKES if stuck else 0
    return cluster.Running(guest=cast(Any, _Stoppable()), watch=watch, reservation_bytes=0)


def test_a_quiet_guest_whose_node_dropped_the_console_is_ended_at_once(
    tmp_path: Path,
) -> None:
    """A node whose proxy is refusing leaves the socket open and silent, so
    nothing raises and the guest's own counters still say it is working."""
    log = tmp_path / "vm-zfs.log"
    log.write_text(
        "installing\nRead from remote host 10.31.0.202: Connection reset by peer\n"
        "Connection to 10.31.0.202 closed.\n"
    )
    held = _held(log, moved=False, stuck=False)
    cluster._sweep({"vm-zfs": cast(Any, held)})
    assert cast(Any, held).guest.stopped


def test_a_quiet_guest_with_a_healthy_console_is_left_running(tmp_path: Path) -> None:
    log = tmp_path / "vm-xfs.log"
    log.write_text("emerging sys-kernel/gentoo-kernel\n")
    held = _held(log, moved=False, stuck=False)
    cluster._sweep({"vm-xfs": cast(Any, held)})
    assert not cast(Any, held).guest.stopped


def test_both_fixtures_of_a_conversion_job_reach_the_driver_cd(tmp_path: Path) -> None:
    """A conversion job carries two: the ordinary install it runs first and the
    in-place fixture it runs against the result. Writing only the first left
    the CD without the second and the guest answered `no such file`."""
    from gentoo_install.model.config import MirrorRegion, Sync

    jobs = cluster.fixtures(["vm-convert"])
    written = cluster.rewrite_fixtures(
        jobs, tmp_path / "fixtures", MirrorRegion.CN, Sync.GIT, site="nju"
    )
    names = {path.name for path in written.iterdir()}
    assert names == {cluster.CONVERSION_BASE, "vm-convert.toml"}, names


def test_the_conversion_keeps_its_own_copy_of_both_logs() -> None:
    """A conversion run has two installs and their result files have the same
    names, so writing the second over the first left only one."""
    import inspect

    code = inspect.getsource(cluster.convert_and_check)
    assert 'f"convert.{name}"' in code


def test_a_busy_guest_whose_console_went_is_ended_too(tmp_path: Path) -> None:
    """A node whose proxy is refusing leaves the socket open and silent while
    the disk stays busy, so the watchdog is right that the guest is alive and
    the run still cannot be read: `btrfs-luks` sat that way for sixteen
    minutes."""
    log = tmp_path / "btrfs-luks.log"
    log.write_text(
        "emerging\nRead from remote host 10.31.0.202: Connection reset by peer\n"
    )
    held = _held(log, moved=False, stuck=False, busy=True)
    assert cast(Any, held).watch.moved() is True, "the counters say it is working"
    cluster._sweep({"btrfs-luks": cast(Any, held)})
    assert cast(Any, held).guest.stopped


def test_a_busy_guest_with_a_healthy_console_is_left_running(tmp_path: Path) -> None:
    log = tmp_path / "vm-xfs.log"
    log.write_text("emerging sys-kernel/gentoo-kernel\n")
    held = _held(log, moved=False, stuck=False, busy=True)
    cluster._sweep({"vm-xfs": cast(Any, held)})
    assert not cast(Any, held).guest.stopped


def test_the_initramfs_is_given_the_address_the_guest_will_have(tmp_path: Path) -> None:
    """`vm-unlock` pinned `192.0.2.10`, a documentation address. On the cluster
    the guest was on 10.31.0.155 and the unlock answered `No route to host`
    after ninety minutes of installing."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.config import MirrorRegion, Sync

    jobs = cluster.fixtures(["vm-unlock"])
    written = cluster.rewrite_fixtures(
        jobs,
        tmp_path / "fixtures",
        MirrorRegion.CN,
        Sync.GIT,
        "",
        "nju",
        {"vm-unlock": "10.31.0.155"},
    )
    rewritten = load(written / "vm-unlock.toml")
    assert rewritten.kernel.remote_unlock.address == f"10.31.0.155/{cluster.GUEST_PREFIX}"


def test_the_initramfs_gateway_is_on_the_subnet_its_address_is_on(
    tmp_path: Path,
) -> None:
    """The address was rewritten to the guest's own and the gateway beside it
    was left at `192.0.2.1`, so the initramfs came up with a default route
    through a host that is not on its subnet. `zbm-unlock` reached the unlock
    twice, at 93.6 and 62.2 minutes, and answered `No route to host` both
    times; the install itself had finished."""
    import ipaddress

    from gentoo_install.exec.config import load
    from gentoo_install.model.config import MirrorRegion, Sync
    from gentoo_install.plan.bootloader import unlock_parameters

    for name in ("vm-unlock", "zbm-unlock"):
        jobs = cluster.fixtures([name])
        # The negative control is the input: the fixtures ship a documentation
        # address and a documentation gateway, so neither assertion below is
        # true of what is read off disk.
        before = load(jobs[0].fixture).kernel.remote_unlock
        assert before.gateway != cluster.GUEST_GATEWAY, before.gateway
        assert ipaddress.ip_address(before.gateway) not in ipaddress.ip_network(
            f"{cluster.GUEST_NETWORK}.0/{cluster.GUEST_PREFIX}"
        ), before.gateway

        written = cluster.rewrite_fixtures(
            jobs,
            tmp_path / name,
            MirrorRegion.CN,
            Sync.GIT,
            "",
            "nju",
            {name: "10.31.0.155"},
        )
        unlock = load(written / f"{name}.toml").kernel.remote_unlock
        assert unlock.gateway == cluster.GUEST_GATEWAY, unlock.gateway

        # The property, not the constant: whatever the pool hands out, the
        # gateway has to be reachable from the address without a route.
        address = ipaddress.ip_interface(unlock.address)
        assert ipaddress.ip_address(unlock.gateway) in address.network, unlock

        # And what the initramfs is actually handed carries it.
        parameters = unlock_parameters(load(written / f"{name}.toml"))
        fields = next(one for one in parameters if one.startswith("ip=")).removeprefix(
            "ip="
        ).split(":")
        assert fields[2] == cluster.GUEST_GATEWAY, fields


def test_the_initramfs_is_not_pinned_to_an_interface_the_guest_does_not_have(
    tmp_path: Path,
) -> None:
    """`vm-unlock` pinned `eth0` and every cluster guest's only NIC is `ens18`
    under predictable naming. dracut's `ip=` names the device in its sixth
    field, so the address was configured on nothing and `rd.neednet=1` waited
    for a link that never arrived: two hours of installing answered `no ssh
    daemon on port 2222`."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.config import MirrorRegion, Sync
    from gentoo_install.plan.bootloader import unlock_parameters

    jobs = cluster.fixtures(["vm-unlock"])
    # The fixture is what an operator on real hardware may well write, and the
    # rewrite is what makes it true here, so the negative control is the input.
    assert load(jobs[0].fixture).kernel.remote_unlock.interface == "eth0"

    written = cluster.rewrite_fixtures(
        jobs,
        tmp_path / "fixtures",
        MirrorRegion.CN,
        Sync.GIT,
        "",
        "nju",
        {"vm-unlock": "10.31.0.155"},
    )
    rewritten = load(written / "vm-unlock.toml")
    assert rewritten.kernel.remote_unlock.interface == ""

    # Not only the field: the parameter the initramfs is handed carries no
    # device name, which is what lets dracut use whichever NIC came up.
    parameters = unlock_parameters(rewritten)
    address = next(one for one in parameters if one.startswith("ip="))
    fields = address.removeprefix("ip=").split(":")
    assert fields[0] == "10.31.0.155", fields
    assert fields[5] == "", fields


def test_a_fixture_that_does_not_unlock_remotely_keeps_its_addresses(
    tmp_path: Path,
) -> None:
    from gentoo_install.exec.config import load
    from gentoo_install.model.config import MirrorRegion, Sync

    jobs = cluster.fixtures(["vm-xfs"])
    written = cluster.rewrite_fixtures(
        jobs,
        tmp_path / "rewrite-check",
        MirrorRegion.CN,
        Sync.GIT,
        "",
        "nju",
        {"vm-xfs": "10.31.0.155"},
    )
    assert load(written / "vm-xfs.toml").kernel.remote_unlock.address == ""
    # Outside the repository: this wrote `lab/.rewrite-check` into the working
    # tree, so every full test run left it untracked and `run.py` stamped the
    # next campaign `1 uncommitted files`, which makes the measurement worthless.
    assert Path(cluster.REPOSITORY) not in written.parents, written


def test_a_node_that_refuses_a_guest_does_not_end_the_campaign() -> None:
    """`POST /nodes/infra-node3/qemu` answered `595 Connection refused` and the
    exception left the dispatch loop, so the closing path removed five guests
    that were installing."""
    import inspect

    from tests.vm.proxmox import ProxmoxTransientError

    code = inspect.getsource(cluster.run)
    caught = code.index("except ProxmoxTransientError as refused:")
    reserved = code.index("_reserve_job(")
    assert reserved < caught, "the guard has to sit on the call that creates the guest"
    after = code[caught : caught + 900]
    assert "unreachable.add(node.name)" in after
    assert "waiting.insert(index, job)" in after
    assert "continue" in after
    assert ProxmoxTransientError.__name__ in code


def test_the_resolvers_are_written_again_once_the_client_is_dead() -> None:
    """dhcpcd is left running through the probe, and a lease taken in that
    window rewrote the file: `vm-zfs-mirror` reached the installer with two
    addresses, two default routes and `nameserver 10.31.0.252`."""
    link = _AnsweringLink(cluster.NETWORK_UP.encode())
    cluster.wait_for_network(cast(cluster.Reconnecting, link), vmid=9301)
    written = [i for i, one in enumerate(link.ran) if one == cluster.use_our_resolvers()]
    killed = link.ran.index(cluster.configure_statically(cluster.static_address(9301)))
    assert len(written) == 2, link.ran
    assert written[0] < killed < written[1]


def test_a_round_can_be_pointed_at_one_address(tmp_path: Path) -> None:
    """A cache on the guests' own segment is an address with no name to
    resolve, which is the class of failure the first thirty rounds died of."""
    from gentoo_install.exec.config import load
    from gentoo_install.model.config import MirrorRegion, Sync
    from gentoo_install.plan.build import stage3_mirror

    jobs = cluster.fixtures(["vm-xfs"])
    written = cluster.rewrite_fixtures(
        jobs,
        tmp_path / "fixtures",
        MirrorRegion.CN,
        Sync.WEBRSYNC,
        "",
        "nju",
        None,
        "http://10.31.0.2/gentoo",
    )
    rewritten = load(written / "vm-xfs.toml")
    assert rewritten.portage.mirrors.distfiles == ("http://10.31.0.2/gentoo",)
    # The stage3 has to follow it, or the run downloads the slow half anyway.
    assert stage3_mirror(rewritten) == "http://10.31.0.2/gentoo"


def test_a_round_without_that_address_keeps_the_table(tmp_path: Path) -> None:
    from gentoo_install.exec.config import load
    from gentoo_install.model.config import MirrorRegion, Sync

    jobs = cluster.fixtures(["vm-xfs"])
    written = cluster.rewrite_fixtures(
        jobs, tmp_path / "plain", MirrorRegion.CN, Sync.GIT, "", "nju"
    )
    assert load(written / "vm-xfs.toml").portage.mirrors.distfiles == ()


def test_the_flag_reaches_the_run() -> None:
    import inspect

    code = inspect.getsource(cluster.main)
    assert '"--skip-node"' in code
    assert "args.skip_node" in code


def test_the_prompt_fallback_accepts_any_hostname() -> None:
    """After a reconnect the shell being back at a prompt proves the command
    returned. The pattern named the live medium, so a guest that had rebooted
    into the system it installed never matched it: `vm-convert` waited
    seventy-three minutes on `xfsbox`."""
    import re

    pattern = re.compile(cluster._ANY_ROOT_PROMPT)
    for host in ("livecd", "xfsbox", "convertedbox", "gentoo-test.local"):
        assert pattern.search(f"root@{host} ~ # "), host
    assert not pattern.search("root@xfsbox /mnt # "), "only the home prompt"


def test_the_fallback_is_used_only_after_a_reconnect() -> None:
    """Before one, the marker is the only proof: a prompt in ordinary output
    would end the wait while the command was still running."""
    import inspect

    code = inspect.getsource(cluster.Reconnecting.wait_for)
    guarded = code.index("if after_reconnect:")
    used = code.index("_ANY_ROOT_PROMPT")
    assert guarded < used


def test_a_node_known_to_drop_its_console_takes_no_guests() -> None:
    """66 of the 70 console-proxy drops recorded under `lab/` name one node,
    and each one throws away up to an hour of install. The comment claimed the
    list was seeded; the code only ever held what `--skip-node` passed, so
    every round paid to learn it again."""
    import inspect

    code = inspect.getsource(cluster.run)
    seeded = code.index("KNOWN_BAD_NODES")
    filtered = code.index("if node.name not in unreachable")
    assert seeded < filtered, "the seed has to be in place before the first placement"

    # The seed is a default and not a rule: a node named to `--allow-node`
    # comes back, or a node repaired between rounds could never be used again.
    lines = [one.strip() for one in code.splitlines() if "unreachable: set[str]" in one]
    assert len(lines) == 1, lines
    assert "allow_nodes" in lines[0], lines[0]
    assert "skip_nodes" in lines[0], lines[0]

    # And the flag reaches it: an option nothing reads is worse than none.
    entry = inspect.getsource(cluster.main)
    assert "--allow-node" in entry
    assert "args.allow_node," in entry


def test_the_seed_and_the_flags_compose_the_way_the_call_site_reads() -> None:
    """The one line asserted above, evaluated. Against a seed of its own, not
    against `KNOWN_BAD_NODES`: the composition is the rule, and what the seed
    happens to hold is data that changes when a node is measured again."""
    seed = {"infra-node8"}
    node = "infra-node8"

    def unreachable(skip: tuple[str, ...], allow: tuple[str, ...]) -> set[str]:
        return (seed | set(skip)) - set(allow)

    assert unreachable((), ()) == seed
    assert unreachable(("infra-node9",), ()) == seed | {"infra-node9"}
    assert node not in unreachable((), (node,))
    # Negative control: allowing an unrelated node does not clear the seed.
    assert node in unreachable((), ("infra-node9",))


def test_a_fixture_that_needs_user_mode_networking_is_refused_here() -> None:
    """`vm-proxy` and `vm-proxy-http` reach `10.0.2.2`, which is the machine
    running qemu as its user-mode network presents it. A cluster guest is
    bridged and has no such host, so every fetch times out: the two of them
    spent 116.0 and 112.8 minutes proving it in one round."""
    import pytest

    for name in ("vm-proxy", "vm-proxy-http"):
        with pytest.raises(SystemExit, match="user-mode network"):
            cluster.fixtures([name])

    # Negative control: a dead proxy at 127.0.0.1 is dead on any machine, so it
    # is a real cluster fixture and must not be swept up by the guard.
    assert cluster.fixtures(["vm-proxy-dead"])[0].name == "vm-proxy-dead"
    # And so is every fixture that names no proxy at all.
    assert cluster.fixtures(["vm-xfs", "zfs-zbm"])


def test_the_prompt_after_a_refusal_is_read_before_the_name_is_offered_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Login incorrect` matches before the prompt that follows it, so that
    prompt stayed in the buffer. `_name_the_user` read it, called it a login
    prompt, and offered the name again — and the second name landed in the
    password field. `vm-lvm` failed at 55.6 minutes with `root` echoed under
    `Password:`, which is what a name in that field looks like.
    """
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    class Agetty:
        """A login that writes its prompt and its refusal as separate reads,
        which is what leaves one of them behind."""

        def __init__(self, refusals: int) -> None:
            self.refusals = refusals
            self.sent: list[str] = []
            self.pending: list[bytes] = []
            self.console = _Screen(b"lvmbox login: ")
            #: What the guest is waiting for. A name sent here is echoed; a
            #: password is not, which is how the two are told apart below.
            self.wants = "name"
            self.into_the_password_field: list[str] = []

        def respond(self, line: str) -> None:
            self.sent.append(line)
            if self.wants == "name":
                self.wants = "password"
                # Echoed, because agetty echoes a name: the harness reads its
                # own name back to know the prompt after it is a fresh one.
                self.pending.append(line.encode() + b"\r\n")
                self.pending.append(b"Password: ")
                return
            if line != "install":
                self.into_the_password_field.append(line)
            self.wants = "name"
            if self.refusals > 0 and line != "install":
                self.refusals -= 1
                self.pending.append(b"\r\nLogin incorrect\r\n")
                self.pending.append(b"lvmbox login: ")
                return
            if self.refusals > 0:
                self.refusals -= 1
                self.pending.append(b"\r\nLogin incorrect\r\n")
                self.pending.append(b"lvmbox login: ")
                return
            self.pending.append(b"lvmbox ~ # ")

        def observe(self, pattern: str, timeout: float = 0.0) -> bytes:
            if not self.pending:
                raise ConsoleTimeout("nothing more")
            return self.pending.pop(0)

    console = Agetty(refusals=2)
    assert cluster._log_in(cast(cluster.Reconnecting, console), "install") == ""
    # Nothing but the password ever reached the password field.
    assert console.into_the_password_field == [], console.sent
    assert console.sent.count("root") == 3, console.sent

    # Negative control: a login that never comes back still ends rather than
    # waiting on a prompt that is not coming.
    stubborn = Agetty(refusals=99)
    assert "refused" in cluster._log_in(cast(cluster.Reconnecting, stubborn), "install")


def test_a_fixture_that_passes_by_failing_is_not_recorded_as_a_failure() -> None:
    """`vm-proxy-dead` points every fetch at a port nothing listens on, so
    `the installer exited 4` is the result it exists to produce. The cluster
    had no notion of that — `campaign.py` did — so it was the one fixture that
    could never be green, and a readiness tally counted it as unverified."""
    import inspect

    assert cluster.EXPECTED_TO_FAIL, "an empty set is the defect this replaced"

    code = inspect.getsource(cluster.install_one)
    expected = code.index("EXPECTED_TO_FAIL")
    ordinary = code.index('f"the installer exited {code!r}"')
    assert expected < ordinary, "the exception has to be read before the general rule"

    # And the two agree on which fixture it is: one fact, two runners.
    from tests.vm import campaign

    failing = {
        Path(one.config).stem
        for stage in campaign.STAGES.values()
        for one in stage
        if one.expect_failure
    }
    assert failing == set(cluster.EXPECTED_TO_FAIL), (failing, cluster.EXPECTED_TO_FAIL)


def test_an_expected_failure_with_no_exit_code_is_not_a_pass() -> None:
    """`vm-proxy-dead` passes by stopping, and the rule for that was
    `code != b"0"`. A guest whose results never came back hands the caller an
    empty code, which satisfies it: the one fixture whose verdict is a failure
    was green whenever the collection failed.
    """
    assert cluster._did_not_stop(b"4") == ""
    assert "wrote no exit code" in cluster._did_not_stop(b"")
    assert "did not" in cluster._did_not_stop(b"0")


def test_a_refused_login_says_what_the_guest_was_showing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`never matched '#|\\$|Login incorrect'` names the pattern that missed and
    not what the guest was showing instead, which is how three rounds went on
    `vm-lvm` guessing at a phase slip. The unlock's verdict carried nothing
    either until #407, and every round after that answered a different layer
    because the screen was in the message."""
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    console = _LoginConsole(refusals=99)
    # The symptom `vm-lvm` showed: the password typed into the name field.
    console.console = _Screen(b"lvmbox login: install\r\nPassword: ")
    said = cluster._log_in(cast(cluster.Reconnecting, console), "install")

    assert "refused every login" in said
    assert "login: install" in said, said

    # Negative control: what the screen holds is what reaches the verdict, so
    # a different screen gives a different message. Without the snapshot both
    # of these read identically — which is what the old verdict did for every
    # failure, whatever the guest was showing.
    other = _LoginConsole(refusals=99)
    other.console = _Screen(b"Maximum number of tries exceeded (5)")
    theirs = cluster._log_in(cast(cluster.Reconnecting, other), "install")
    assert "Maximum number of tries exceeded" in theirs, theirs
    assert theirs != said, "two different screens gave the same verdict"


def test_the_login_wait_asks_again_after_the_console_is_reopened() -> None:
    """run54 compared against run51 byte for byte at the same point: run51 has
    the kernel's own first line straight after `Loading initial ramdisk ...`,
    and run54 has the reconnect banner there instead and nothing after it. The
    guest had booted; the console was reopened past the prompt.

    `agetty` writes `login:` once, so a wait that never writes to the guest
    spends the whole of `BOOT_PATIENCE` on a healthy machine.
    """
    from tests.vm.console import ConsoleClosed

    class Console:
        def __init__(self, opened: int) -> None:
            self.opened = opened
            self.asked = False

        def expect(self, pattern: str, timeout: float) -> bytes:
            if self.opened == 1:
                raise ConsoleClosed("the guest closed the serial connection")
            # The second console is the reopened one, and the prompt has
            # already gone by: only a keystroke brings it back.
            if not self.asked:
                raise ConsoleClosed("still nothing")
            return b"gentoo login:"

        def send(self, line: str) -> None:
            self.asked = True

        def close(self) -> None:
            return None

    opened: list[Console] = []

    def open_console() -> Any:
        opened.append(Console(len(opened) + 1))
        return opened[-1]

    link = cluster.Reconnecting(open_console, tries=4)
    assert b"login:" in link.observe(r"login:", timeout=30.0, solicit=True)

    # Negative control: the default is still the silent wait, because an empty
    # line at a login prompt spends one of agetty's attempts. `boot_and_check`
    # takes that default and treats a missed prompt as a note rather than a
    # verdict, since typing a name brings the prompt back for free.
    opened.clear()
    silent = cluster.Reconnecting(open_console, tries=4)
    with pytest.raises(ConsoleClosed):
        silent.observe(r"login:", timeout=30.0)


def test_the_boot_check_does_not_spend_an_agetty_attempt_on_the_prompt() -> None:
    """`vm-lvm` passed run54 in 21.6 minutes with one reconnect and no solicit,
    and failed run56 in 74.4 minutes with two reconnects and

        Maximum number of tries exceeded (5)
        lvmbox login: install

    An empty line at a login prompt is one of agetty's attempts. The wait keeps
    the silent default, and a prompt that went by is carried into the verdict
    rather than ending the run, because `_name_the_user` types a name and waits
    for either prompt.
    """
    import inspect

    source = inspect.getsource(cluster.boot_and_check)
    waited = [
        line
        for line in source.splitlines()
        if "observe(" in line and "LOGIN_PROMPTS" in line
    ]
    assert waited, source
    assert not any("solicit" in line for line in waited), waited
    # And the miss is not a verdict on its own: nothing returns straight out of
    # that except, or a machine whose one prompt went by is failed for it.
    after = source.split("except (ConsoleTimeout, ConsoleClosed) as error:")[1]
    head = after.split("try:")[0]
    assert "return" not in head, head


def test_infra_node3_takes_guests_again() -> None:
    """That node was named in `KNOWN_BAD_NODES` for 66 of the 70 recorded
    console-proxy drops. Named, it stopped producing the evidence that would
    clear it, so it was measured on 2026-08-17 with `--allow-node`:

        ok  vm-binpkg  68.4m   2 console sessions
        ok  vm-xfs     84.6m   2 console sessions

    Both green, and `vm-xfs` failed the same day on a node that was never
    excluded. One reconnect each is fewer than `vm-binpkg` took there.
    """
    assert "infra-node3" not in cluster.KNOWN_BAD_NODES

    # Negative control one: the seam still excludes what is named to it, or
    # nothing would be left to turn a bad node off with.
    excluded = (set(cluster.KNOWN_BAD_NODES) | {"infra-node3"}) - set(())
    assert "infra-node3" in excluded

    # Negative control two: the help text reads without a name in it, since it
    # interpolated the set and would otherwise end mid-sentence.
    import inspect

    entry = inspect.getsource(cluster.main)
    assert "none are named" in entry, entry[:0] or "the empty case is unhandled"


def test_a_timed_out_marker_names_the_command_it_was_waiting_on() -> None:
    """`vm-convert` ended a 156-minute run with

        never matched 'MARK_63_DONE'; last output was b'...grub-core/lib...'

    A token is not a command, and reading which of sixty-three it was took the
    source. The screen said GRUB was compiling; the message did not say which
    step was waiting on it.
    """
    from tests.vm.console import ConsoleIdle, ConsoleTimeout

    class Console:
        def __init__(self, error: Exception) -> None:
            self.error = error

        def send(self, line: str) -> None:
            return None

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            raise self.error

    link = cluster.Reconnecting(
        lambda: cast(Any, Console(ConsoleTimeout("never matched 'MARK_63_DONE'"))), tries=1
    )
    with pytest.raises(ConsoleTimeout, match="emerge --sync") as caught:
        link.run("emerge --sync gentoo")
    assert "MARK_63_DONE" in str(caught.value), "the token is kept as well"

    # Negative control one: the idle failure keeps its own type, or `wait_for`
    # stops consulting the watchdog and every long build ends as a timeout.
    idle_link = cluster.Reconnecting(
        lambda: cast(Any, Console(ConsoleIdle("never matched 'MARK_9_DONE'"))), tries=1
    )
    with pytest.raises(ConsoleIdle):
        idle_link.run("emerge --sync gentoo")

    # Negative control two: a console that answers is not wrapped at all.
    class Answering(Console):
        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            return b"done"

    cluster.Reconnecting(
        lambda: cast(Any, Answering(ConsoleTimeout("unused"))), tries=1
    ).run("true")



class _MarkerConsole:
    def __init__(self, tail: bytes, completes: bool) -> None:
        self.tail = tail
        self.completes = completes
        self.sent: list[str] = []
        self.echoed = bytearray()
        self.patterns: list[str] = []

    def send(self, line: str) -> None:
        self.sent.append(line)
        self.echoed.extend(line.encode() + b"\r\n")

    def send_raw(self, keys: str) -> None:
        self.echoed.extend(keys.encode())

    def snapshot(self, seconds: float) -> bytes:
        return bytes(self.echoed)

    @property
    def closed(self) -> bool:
        return False

    def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
        self.patterns.append(pattern)
        if self.completes:
            matched = re.search(r"MARK_(\d+)_DONE", pattern)
            assert matched is not None
            self.echoed.extend(f"MARK_{matched.group(1)}_DONE\n".encode())
            return bytes(self.echoed)
        raise ConsoleTimeout(
            f"never matched {pattern!r}; last output was {bytes(self.echoed) + self.tail!r}"
        )

    def close(self) -> None:
        return None


def _marked_token(line: str) -> str:
    matched = re.search(r"MARK_%s_BEGIN\\n' (\d+)", line)
    assert matched is not None
    return matched.group(1)


def test_a_lost_marker_is_resent_with_a_fresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class ExpiringMarkerConsole(_MarkerConsole):
        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            if self.tail:
                clock[0] = 2.0
            return super().expect(pattern, timeout, idle)

    opened: list[_MarkerConsole] = []

    def open_console() -> _MarkerConsole:
        console = ExpiringMarkerConsole(
            cluster._SERIAL_TERMINAL_STARTED.encode() if not opened else b"",
            completes=bool(opened),
        )
        opened.append(console)
        return console

    failure: ConsoleTimeout | None = None
    try:
        cluster.Reconnecting(open_console, tries=2).run("true", timeout=1.0)
    except ConsoleTimeout as error:
        failure = error

    assert failure is None, failure
    assert [_marked_token(one.sent[-1]) for one in opened] == ["1", "2"]
    assert opened[0].echoed.startswith(opened[0].sent[0].encode())
    assert opened[1].sent[0] == ""
    # Either marker ends it: the retry waits for every token this call has
    # sent, because the guest may have printed the first one during the
    # reconnect. The fake answers with the first it finds in the pattern.
    assert opened[1].echoed.endswith(b"MARK_1_DONE\n") or opened[1].echoed.endswith(
        b"MARK_2_DONE\n"
    )


def test_two_rounds_carrying_one_fixture_keep_separate_logs(tmp_path: Path) -> None:
    """Every schedule writes into one directory, so two rounds carrying the
    same fixture wrote the same file at the same time and each verdict pointed
    at a log holding both. `vm-greetd` was in two rounds at once.
    """

    class Free:
        def __init__(self) -> None:
            self.given = 9300

        def free_vmid(self, held: frozenset[int] = frozenset()) -> int:
            self.given += 1
            return self.given

    job = cluster.fixtures(["vm-greetd"])[0]
    api = Free()
    first = cluster._execution(
        cast(Any, api), "infra-node1", job, "driver.iso", tmp_path, 0, "nonce-one"
    )
    second = cluster._execution(
        cast(Any, api), "infra-node2", job, "driver.iso", tmp_path, 0, "nonce-two"
    )

    assert first.watch.log != second.watch.log
    assert first.watch.log.name == "vm-greetd-9301.log"
    assert second.watch.log.name == "vm-greetd-9302.log"


def test_a_marker_that_arrives_after_the_retry_began_still_ends_the_command() -> None:
    """The guest prints the marker just after the reader gave up on it, so the
    retry was waiting for a marker that had already gone past. Six attempts
    each lost the one before, and `vm-source-kernel` threw away 6.5 hours of
    finished install at the last command of the run, twice.
    """
    opened: list[_MarkerConsole] = []

    class LateConsole(_MarkerConsole):
        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            self.patterns.append(pattern)
            if not self.completes:
                raise ConsoleTimeout(
                    f"never matched {pattern!r}; last output was "
                    f"{bytes(self.echoed) + self.tail!r}"
                )
            # The first token's marker, which the guest printed while the
            # session was being replaced. Nothing answers for the second.
            if "MARK_1_DONE" not in pattern:
                raise ConsoleTimeout(f"never matched {pattern!r}")
            self.echoed.extend(b"MARK_1_DONE\n")
            return bytes(self.echoed)

    def open_console() -> _MarkerConsole:
        console = LateConsole(
            cluster._SERIAL_TERMINAL_STARTED.encode() if not opened else b"",
            completes=bool(opened),
        )
        opened.append(console)
        return console

    cluster.Reconnecting(open_console, tries=2).run("true", timeout=60.0)

    assert len(opened) == 2, opened
    assert opened[1].echoed.endswith(b"MARK_1_DONE\n")
    # And the retry did send again: a command the guest never received has to
    # be given a second chance as well.
    assert _marked_token(opened[1].sent[-1]) == "2"


def test_a_lost_marker_retry_is_bounded_and_keeps_the_console_tail() -> None:
    opened: list[_MarkerConsole] = []

    def open_console() -> _MarkerConsole:
        console = _MarkerConsole(cluster._SERIAL_TERMINAL_STARTED.encode(), completes=False)
        opened.append(console)
        return console

    failure: ConsoleTimeout | None = None
    try:
        cluster.Reconnecting(open_console, tries=3).run("true")
    except ConsoleTimeout as error:
        failure = error

    assert failure is not None
    assert len(opened) == 3, opened
    assert cluster._SERIAL_TERMINAL_STARTED in str(failure)
    assert [_marked_token(one.sent[-1]) for one in opened] == ["1", "2", "3"]


def test_a_nonrepeatable_marker_loss_is_not_resent() -> None:
    opened: list[_MarkerConsole] = []

    def open_console() -> _MarkerConsole:
        console = _MarkerConsole(cluster._SERIAL_TERMINAL_STARTED.encode(), completes=False)
        opened.append(console)
        return console

    failure: ConsoleTimeout | None = None
    try:
        cluster.Reconnecting(open_console, tries=3).run(
            "parted --script /dev/vda mklabel gpt", repeatable=False
        )
    except ConsoleTimeout as error:
        failure = error

    assert failure is not None
    assert len(opened) == 1, opened
    assert len(opened[0].sent) == 1


def test_a_capacity_read_that_did_not_answer_does_not_end_the_schedule() -> None:
    """run58 lost seven fixtures to one line:

        ProxmoxTransientError: GET /nodes did not answer:
        Remote end closed connection without response

    Every guest was still installing. The dispatch already treats a transient
    refusal as the node's problem and puts the job back; the capacity read
    beside it had no such guard, so the exception left the loop and the closing
    path removed the guests.
    """
    import inspect

    code = inspect.getsource(cluster.run)
    slots = code.index("free_slots(api")
    guarded = code.index("except ProxmoxTransientError", 0, slots + 400)
    assert guarded > slots, "the capacity read is inside the guard"
    # It answers with no slots rather than swallowing the failure silently:
    # a schedule that can never place anything ends on `capacity_since`.
    after = code[guarded:guarded + 600]
    assert "slots = []" in after, after
    assert "capacity_since" in code, "the timeout that ends a hopeless schedule"

    # Negative control: the dispatch's own guard is a different one and stays,
    # or a node that refuses a guest would take every following job too.
    assert code.count("except ProxmoxTransientError") >= 2, code.count(
        "except ProxmoxTransientError"
    )


def test_a_dropped_console_does_not_end_a_guest_that_is_still_working(
    tmp_path: Path,
) -> None:
    """`vm-gnome` was at `[26/539]` compiling git when its websocket answered
    `[Errno 104] Connection reset by peer`, and the verdict threw away 167
    minutes. A silent console already asks the hypervisor's counters through
    `ConsoleIdle`; a broken one did not ask at all, so the two ways a console
    can stop carrying bytes had opposite answers about the same live guest.
    """
    from tests.vm.console import ConsoleClosed

    class Console:
        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            raise ConsoleClosed("the connection broke: [Errno 104] Connection reset by peer")

        def send(self, line: str) -> None:
            return None

        def close(self) -> None:
            return None

    opened: list[Console] = []
    sampled: list[int] = []

    def counters() -> tuple[int, float] | None:
        # A guest whose disk keeps growing while its console says nothing.
        sampled.append(len(sampled))
        return cluster.QUIET_BYTES * 2 * (len(sampled) + 1), 0.0

    log = tmp_path / "serial.log"
    log.write_bytes(b"")
    watch = cluster.Watchdog(log=log, counters=counters)

    def open_console() -> Any:
        opened.append(Console())
        return opened[-1]

    link = cluster.Reconnecting(open_console, tries=2)
    with pytest.raises(ConsoleClosed):
        link.wait_for("emerge --ask=n @world", timeout=0.0, idle=1.0, watch=watch)

    # Asked once per grant, and the last decision short-circuits on the count
    # rather than taking a sample it will not use.
    assert len(sampled) == cluster.RECONNECT_GRANTS, sampled
    # It reconnected, and it stopped: a run that never gives up holds a node
    # for the rest of the schedule.
    assert 1 < len(opened) <= cluster.REOPEN_CEILING, len(opened)
    # The reopen ceiling is what a grant spends, so a ceiling below the grants
    # ends a working guest before its last one: `vm-gnome` spent all four of
    # its grants compiling and was ERRORed at 156.5 minutes.
    assert cluster.REOPEN_CEILING >= cluster.RECONNECT_GRANTS * 2


def test_a_dropped_console_still_ends_a_guest_that_moves_nothing(tmp_path: Path) -> None:
    """The negative direction. Without it the change reads as "reconnect for
    ever", which is how a dead guest holds a node until the schedule ends.
    """
    from tests.vm.console import ConsoleClosed

    class Console:
        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            raise ConsoleClosed("the connection broke: [Errno 104] Connection reset by peer")

        def send(self, line: str) -> None:
            return None

        def close(self) -> None:
            return None

    log = tmp_path / "serial.log"
    log.write_bytes(b"")
    opened: list[Console] = []

    def open_console() -> Any:
        opened.append(Console())
        return opened[-1]

    still = cluster.Watchdog(log=log, counters=lambda: (0, 0.0))
    link = cluster.Reconnecting(open_console, tries=2)
    with pytest.raises(ConsoleClosed):
        link.wait_for("emerge --ask=n @world", timeout=0.0, idle=1.0, watch=still)
    assert len(opened) == 1, len(opened)

    # And with no watchdog at all, which is what `run` and `expect_output` use.
    opened.clear()
    quiet = cluster.Reconnecting(open_console, tries=2)
    with pytest.raises(ConsoleClosed):
        quiet.wait_for("emerge --ask=n @world", timeout=0.0, idle=1.0)
    assert len(opened) == 1, len(opened)


def test_the_probe_after_the_install_cannot_fail_the_run() -> None:
    """`vm-btrfs` and `vm-mdraid` were both recorded `ERROR` in run60 at 96 and
    81 minutes with

        installed 54 operations into /mnt/gentoo; 0 packages from a binary host
        installed 58 operations into /mnt/gentoo; 0 packages from a binary host

    already in their logs. The install had finished and written its exit code,
    and what timed out was the reachability probe that runs after it, whose
    `getent` calls block on a resolver that has stopped answering. That probe
    answers a question about a failure; it is not one.
    """
    from tests.vm.console import ConsoleTimeout

    asked: list[str] = []

    class Timing:
        def run(self, command: str, timeout: float = 120.0) -> None:
            asked.append(command)
            raise ConsoleTimeout("never matched a marker")

    cluster._note_the_probe(cast(Any, Timing()), "closing")
    assert asked == [cluster.REACHABILITY_PROBE], asked


def test_the_closing_probe_is_still_asked_when_it_can_answer() -> None:
    """Swallowing the failure must not mean skipping the probe, or the
    diagnosis it exists for goes with it."""
    answered: list[str] = []

    class Answering:
        def run(self, command: str, timeout: float = 120.0) -> None:
            answered.append(command)

    cluster._note_the_probe(cast(Any, Answering()), "closing")
    assert answered == [cluster.REACHABILITY_PROBE], answered


def test_a_truncated_verdict_still_says_which_bound_was_reached() -> None:
    """`vm-unlock` was recorded in run59 as

        '{ sh /mnt/driver/install.sh --config fixtures/vm-unlock.toml; ... }':
        never matched 'MARK_27_DONE|root@[A-Za-z0-9._-]+ ~ # '; last output was
        b'msoft-float -fno-omit-frame-pointer -fno-dwarf2-cfi-asm -m

    and that is the whole verdict: `VERDICT_BYTES` cut it there. Whether the
    guest went quiet or ran out of ceiling decides whether the next question is
    about the installer or about the schedule, and the message said neither
    because the reason came after the screen.
    """
    from tests.vm.console import ConsoleIdle, ConsoleTimeout, SerialConsole

    class Silent:
        closed = False

        def recv(self, size: int) -> bytes:
            return b""

        def sendall(self, data: bytes) -> None:
            return None

        def close(self) -> None:
            return None

    def console(log: Path) -> SerialConsole:
        return SerialConsole(cast(Any, Silent()), log.open("wb"))

    import tempfile

    with tempfile.TemporaryDirectory() as where:
        quiet = console(Path(where) / "a.log")
        quiet._buffer = b"x" * 4000
        with pytest.raises(ConsoleIdle) as idled:
            quiet.expect("never-here", timeout=30.0, idle=0.2)
        spent = console(Path(where) / "b.log")
        with pytest.raises(ConsoleTimeout) as ceilinged:
            spent.expect("never-here", timeout=0.2)

    for raised, wanted in ((idled, "nothing arrived for"), (ceilinged, "elapsed")):
        said = str(raised.value)
        assert wanted in said, said
        # Before the screen, so a verdict cut to VERDICT_BYTES keeps it.
        assert said.index(wanted) < said.index("last output was"), said
        assert said.index(wanted) < cluster.OUTCOME_BYTES, said


def test_the_probe_costs_less_than_its_own_budget() -> None:
    """Five guests of run61 were ended at three minutes with

        REACH 10.31.0.199=down 10.31.0.254=up 223.5.5.5=up
        LOOKUPS_V4 ok fail fail fail fail
        LOOKUPS_ANY ok fail

    as the last line of the log and no install started. `getent` waits for its
    own resolver timeout, and ten of those cost more than the 120s the probe is
    given, so a diagnostic ended every run it was measuring.
    """
    import subprocess

    probe = cluster.REACHABILITY_PROBE
    # Every lookup is bounded, wherever it sits.
    lookups = probe.count("getent ")
    assert lookups >= 2, lookups
    assert probe.count(f"timeout {cluster.LOOKUP_PATIENCE} getent ") == lookups, probe
    # Two of them are inside a five-iteration loop, so the worst case counts
    # those five times and the rest once.
    looped = probe.count("for i in 1 2 3 4 5; do")
    assert looped == 2, looped
    worst = (
        looped * 5 * cluster.LOOKUP_PATIENCE
        + (lookups - looped) * cluster.LOOKUP_PATIENCE
        + 3 * 2
        + cluster.KEYSERVER_PATIENCE
    )
    # Against the 120s the three call sites give it, with room for the `ip`
    # and `dmesg` reads that follow.
    assert worst < 90, worst
    assert subprocess.run(["bash", "-n", "-c", probe], capture_output=True).returncode == 0


def test_no_reachability_probe_is_ever_the_verdict() -> None:
    """The probe is read three times and none of them decides anything: the
    network is what `wait_for_network` waits for, and this only records what
    the guest saw at that moment.
    """
    import inspect

    for where in (cluster.install_one, cluster.answer_once, cluster.wait_for_network):
        source = inspect.getsource(where)
        assert "link.run(REACHABILITY_PROBE" not in source, where.__name__
    # And the one place that does run it swallows a timeout, which the tests
    # above hold.
    assert "link.run(REACHABILITY_PROBE" in inspect.getsource(cluster._note_the_probe)


def test_a_dead_first_resolver_costs_one_second_and_not_ten() -> None:
    """`resolv.conf(5)` on this machine: `timeout:n` is "the amount of time the
    resolver will wait for a response from a remote name server before retrying
    the query via a different name server", default `RES_TIMEOUT`, and
    `attempts:n` defaults to 2. So a first nameserver that has stopped
    answering costs ten seconds on every lookup.

    Five guests of run61 had `REACH 10.31.0.199=down` with the gateway and
    `223.5.5.5` both answering ping, `LOOKUPS_V4 ok fail fail fail fail`, and
    no install started; two more failed at `OpenPGP keyring refresh failed`,
    which is the same lookups from inside `emerge-webrsync`.
    """
    written = cluster.use_our_resolvers()

    assert f"options no-aaaa {cluster.RESOLVER_OPTIONS}" in written, written
    assert "timeout:1" in cluster.RESOLVER_OPTIONS, cluster.RESOLVER_OPTIONS
    # Every resolver is still offered, in order: the point is what a dead one
    # costs, not dropping it.
    for one in cluster.GUEST_RESOLVERS:
        assert f"nameserver {one}" in written, one
    # The whole worst case now fits the probe's budget, which it did not before.
    attempts = int(cluster.RESOLVER_OPTIONS.split("attempts:")[1])
    seconds = int(cluster.RESOLVER_OPTIONS.split("timeout:")[1].split()[0])
    assert seconds * attempts <= cluster.LOOKUP_PATIENCE, cluster.RESOLVER_OPTIONS


class _PartlyEchoingConsole:
    """`login` turning the echo off partway through the password.

    Taken from `openrc-sdboot` in run65 and `vm-lvm` in run64: the console
    held a fragment of the password rather than the whole of it, so the
    whole-word check read the attempt as the installed system refusing a
    password that was in fact never delivered whole.
    """

    def __init__(self, echoes: int, keep: int = 1) -> None:
        self.echoes = echoes
        #: How many characters `login` swallowed before the echo stopped.
        self.keep = keep
        self.sent: list[str] = []
        self.answers: list[bytes] = []
        self.console = _Screen(b"openrcsdbox login: ")

    def respond(self, line: str) -> None:
        self.sent.append(line)
        if line == "root":
            self.answers.append(b"Password: ")
        elif self.echoes > 0:
            self.echoes -= 1
            fragment = line[self.keep :]
            self.answers.append(f"{fragment}\r\n\r\nLogin incorrect\r\nlogin: ".encode())
        else:
            self.answers.append(b"openrcsdbox ~ # ")

    def observe(self, pattern: str, timeout: float = 0.0) -> bytes:
        return self.answers.pop(0) if self.answers else b""


def test_a_password_the_console_echoed_part_of_is_not_counted_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`login` turns the echo off between two characters, so what comes back
    is a fragment. Counting that as the installed system's refusal spends the
    harness's whole budget on a race, and `login` gives up after three of its
    own: `openrc-sdboot` was failed at 41.3 minutes for an install that had
    completed."""
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    # One more race than the harness has catches for, so a fragment counted as
    # a refusal spends the whole budget and the login ends with no clean
    # attempt ever made. Three would leave one, and the test would pass
    # whether the fragment was seen or not.
    races = cluster.PASSWORD_ECHO_CATCHES + 1
    for swallowed in (1, 2, 5):
        console = _PartlyEchoingConsole(echoes=races, keep=swallowed)
        assert cluster._log_in(cast(cluster.Reconnecting, console), "install") == "", (
            swallowed,
            console.sent,
        )
        assert console.sent.count("install") == races + 1, (swallowed, console.sent)


class _LoginWithItsOwnLimit:
    """`login` as it behaves after three wrong passwords: it prints its own
    limit instead of a refusal and exits, and agetty starts another one.

    Taken from `ext3` in run78, where the harness waited its whole 120 seconds
    for a `Login incorrect` that `login` had already decided not to print.
    Only `observe` patterns that the stream actually holds are answered, so a
    wait for a word the guest never says fails here as it does on a guest.
    """

    ISSUE = b"\r\nThis is plain\r\n\r\nplain login: "

    #: The three lines `login` writes, per locale, as codepoints: shadow's own
    #: `po/zh_TW.po` and `po/zh_CN.po` for the two Chinese ones.
    WORDING: dict[str, tuple[str, str, str, str]] = {
        "C": (
            "Password: ",
            "Login incorrect",
            "Maximum number of tries exceeded (3)",
            "login: ",
        ),
        "zh_TW": (
            "\u5bc6\u78bc\uff1a",
            "\u767b\u5165\u932f\u8aa4",
            "\u5df2\u78b0\u5230\u6700\u5927\u5617\u8a66\u6b21\u6578 (3)",
            "\u4f7f\u7528\u8005\uff1a",
        ),
        "zh_CN": (
            "\u5bc6\u7801\uff1a",
            "\u767b\u5f55\u9519\u8bef",
            "\u5df2\u7ecf\u8d85\u8fc7\u6700\u5927\u5c1d\u8bd5\u6b21\u6570 (3)",
            "\u7528\u6237\u540d\uff1a",
        ),
    }

    def __init__(self, gives_up: int, locale: str = "C") -> None:
        #: How many times `login` spends its three tries before the password
        #: is finally taken.
        self.gives_up = gives_up
        self.password_prompt, self.refusal, self.gave_up, self.name_prompt = (
            one.encode() for one in self.WORDING[locale]
        )
        self.tries = 0
        #: How many waits gave up. A prompt the guest printed in its own
        #: locale costs the whole re-prompt patience when the harness knows
        #: only the English one.
        self.timeouts = 0
        self.sent: list[str] = []
        self.held = b"plain login: "
        self.console = _Screen(b"plain login: ")

    def respond(self, line: str) -> None:
        self.sent.append(line)
        if line == "root":
            self.held += b"root\r\n" + self.password_prompt
            return
        if self.gives_up == 0:
            self.held += b"\r\nplain ~ # "
            return
        self.tries += 1
        if self.tries < 3:
            # `login` asks again itself, in its own locale; agetty and its
            # English issue only come back after `login` has given up.
            self.held += (
                b"\r\n\r\n" + self.refusal + b"\r\nplain " + self.name_prompt
            )
            return
        self.tries = 0
        self.gives_up -= 1
        self.held += b"\r\n" + self.gave_up + b"\r\n" + self.ISSUE

    def observe(
        self, pattern: str, timeout: float = 0.0, *, solicit: bool = False
    ) -> bytes:
        found = re.search(pattern.encode(), self.held)
        if not found:
            self.timeouts += 1
            raise ConsoleTimeout(f"never matched {pattern!r}")
        said, self.held = self.held[: found.end()], self.held[found.end() :]
        return said


def test_login_spending_its_own_three_tries_is_answered_not_waited_out() -> None:
    """`login` prints `Maximum number of tries exceeded` in place of the third
    refusal and exits. A harness that knows only `Login incorrect` waits its
    whole 120 seconds beside an agetty that is already asking for a name:
    `ext3` was failed at 40.7 minutes for an install that had completed."""
    console = _LoginWithItsOwnLimit(gives_up=1)

    assert cluster._log_in(cast(cluster.Reconnecting, console), "install") == "", (
        console.sent
    )
    # Three passwords into `login`'s limit and a fourth into the one agetty
    # started afterwards, which is the attempt the fix exists to make.
    assert console.sent.count("install") == 4, console.sent


def test_a_login_that_never_takes_the_password_still_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direction that has to keep working: a guest whose `login` gives up
    for ever is a machine nobody can log into, and answering its prompt again
    without bound holds the schedule open."""
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    console = _LoginWithItsOwnLimit(gives_up=99)

    assert "refused every login" in cluster._log_in(
        cast(cluster.Reconnecting, console), "install"
    )
    assert console.sent.count("install") <= cluster.LOGIN_TRIES * 3, console.sent


def test_a_login_speaking_the_installed_locale_is_read_the_same_way() -> None:
    """shadow's `login` is translated, so a fixture installing a Chinese
    locale is refused in Chinese. `vm-unlock` was failed at 64.0 minutes with
    the refusal and the name prompt both on its console in `zh_TW`."""
    for locale in ("zh_TW", "zh_CN"):
        console = _LoginWithItsOwnLimit(gives_up=1, locale=locale)

        assert cluster._log_in(cast(cluster.Reconnecting, console), "install") == "", (
            locale,
            console.sent,
        )
        assert console.sent.count("install") == 4, (locale, console.sent)
        # `login` asks for the name again in its own locale, so a harness that
        # knows only agetty's English one waits out its re-prompt patience on
        # every refusal before typing anything.
        assert console.timeouts == 0, (locale, console.timeouts)


def test_a_refusal_holding_none_of_the_password_still_ends_the_login() -> None:
    """The direction that has to keep working: a genuinely wrong password
    echoes nothing, and absorbing that would hold the schedule open on a
    machine nobody can log into."""
    assert not cluster._echoed_back(b"Login incorrect\r\nlogin: ", "install")
    # The refusal and the prompt carry letters of their own, and searching
    # them makes every refusal read as this harness's own race.
    assert not cluster._echoed_back(b"Login incorrect\r\nlogin: ", "loginx")
    assert cluster._echoed_back(b"nstall\r\nLogin incorrect", "install")
    assert cluster._echoed_back(b"inst\r\nLogin incorrect", "install")


def test_a_verdict_stays_on_one_line_however_many_the_guest_used() -> None:
    """`zbm-unlock`'s verdict ran to three lines in run65 because the remote
    command's output reached it whole. The first line held only ssh's
    known-hosts warning, so a reader taking one line per verdict read the
    failure as a bare warning and the two lines carrying `Key load error:
    Failed to open key material file` as unrelated records."""
    detail = (
        "remote unlock failed: remote unlock failed: Warning: Permanently "
        "added '[10.31.0.150]:2222' (ECDSA) to the list of known hosts.\n"
        "0 / 1 key(s) successfully loaded\n"
        "Key load error: Failed to open key material file: No such file or directory"
    )
    folded = cluster._one_line(detail)

    assert "\n" not in folded, folded
    assert "Key load error" in folded, folded
    assert "0 / 1 key(s)" in folded, folded
    # Joined visibly, or two sentences run together into a third that was
    # never printed.
    assert folded.count(" | ") == 2, folded


def test_folding_drops_the_blank_lines_a_console_leaves() -> None:
    assert cluster._one_line("first\n\n  \nsecond\n") == "first | second"


def test_a_detail_with_no_newline_is_unchanged() -> None:
    """Most verdicts are one line already, and rewriting them would change
    every log this campaign has produced."""
    assert cluster._one_line("the installer exited b'4'") == "the installer exited b'4'"


class _DroppedOnce:
    """A console that has been closed and records what is written to it."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = True

    def send(self, line: str) -> None:
        self.sent.append(line)

    def send_raw(self, keys: str) -> None:
        self.sent.append(keys)

    def close(self) -> None:
        self.closed = True


def test_a_reopen_before_a_write_sends_no_empty_line() -> None:
    """The line the caller is about to write is the request for a prompt, and
    the empty one a solicit adds is a line the guest answers first. At a
    `login:` prompt agetty takes it as an attempt and reprints its banner, so
    every send after it answers the prompt before the one it was written for:
    `vm-lvm` and `openrc-sdboot` failed that way in three rounds."""
    opened: list[_DroppedOnce] = []

    def open_console() -> _DroppedOnce:
        made = _DroppedOnce()
        made.closed = not opened  # the first is closed, the reopened one is not
        opened.append(made)
        return made

    link = cluster.Reconnecting(cast("Any", open_console))
    link.send("root")

    assert len(opened) == 2, opened
    assert opened[1].sent == ["root"], opened[1].sent
    assert "" not in opened[1].sent, opened[1].sent


def test_a_reopen_that_is_waiting_for_a_shell_still_asks_for_one() -> None:
    """The other direction: a console reopened while nothing is being written
    shows nothing until it is asked, and the waits below look for text."""
    opened: list[_DroppedOnce] = []

    def open_console() -> _DroppedOnce:
        made = _DroppedOnce()
        made.closed = False
        opened.append(made)
        return made

    link = cluster.Reconnecting(cast("Any", open_console))
    link.reopen()

    assert opened[-1].sent == [""], opened[-1].sent


def test_the_first_password_is_sent_the_moment_the_prompt_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tests/vm/run.py` sends it at once and passes: `vm-lvm` installs and
    logs in locally on the same fixture and the same installer revision, and
    failed on the cluster in four rounds where a second went first. `login`
    turns the echo off before it writes the prompt, so there is no window to
    wait out; the growth stays for the console that proves otherwise by
    echoing."""
    settles: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: settles.append(seconds))
    console = _LoginConsole(refusals=0)

    assert cluster._log_in(cast(cluster.Reconnecting, console), "install") == ""
    assert settles == [0.0], settles
    assert cluster.PASSWORD_ECHO_OFF_AFTER == 0.0, cluster.PASSWORD_ECHO_OFF_AFTER


def test_the_prompt_a_medium_started_shell_gives_is_a_root_prompt() -> None:
    """`vm-bios` opened a root shell on the serial port and then waited
    fifteen minutes for a prompt it was already looking at: the console's last
    output was ` ~ # `, and the pattern wanted the medium's own hostname in
    front of it. A shell started by the medium's auto-login carries none,
    because nothing has set one in that environment yet."""
    import re

    for prompt in (b"livecd ~ # ", b"localhost ~ # ", b" ~ # ", b"root@livecd ~ # "):
        assert re.search(cluster.ROOT_PROMPT.encode(), prompt), prompt


def test_a_line_that_is_not_a_prompt_is_not_taken_for_one() -> None:
    """The negative direction: `#` alone appears in half the boot messages,
    which is why the pattern asks for the tilde and the space around it."""
    import re

    for other in (
        b"[    0.000000] Command line: console=ttyS0",
        b"#### partitioning",
        b"Load keymap",
    ):
        assert not re.search(cluster.ROOT_PROMPT.encode(), other), other


class _RefusesUntilTheSettleGrows:
    """`login` on a guest that loses the race without echoing anything.

    `ext3` was refused three times with nothing on the console between
    `Password:` and `Login incorrect`: part of the password reached `login`
    and the rest did not, so there was no echo for the harness to notice and
    the settle it retried with was the settle that had just failed.
    """

    def __init__(self, needs: float) -> None:
        self.needs = needs
        self.settled = 0.0
        self.sent: list[str] = []
        self.answers: list[bytes] = []
        self.console = _Screen(b"plain login: ")

    def slept(self, seconds: float) -> None:
        self.settled += seconds

    def respond(self, line: str) -> None:
        self.sent.append(line)
        if line == "root":
            self.answers.append(b"Password: ")
        elif self.settled >= self.needs:
            self.answers.append(b"plain ~ # ")
        else:
            self.answers.append(b"Login incorrect\r\nlogin: ")

    def observe(self, pattern: str, timeout: float = 0.0) -> bytes:
        return self.answers.pop(0) if self.answers else b""


def test_a_refusal_with_no_echo_still_grows_the_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = _RefusesUntilTheSettleGrows(needs=cluster.PASSWORD_ECHO_BACKOFF)
    monkeypatch.setattr("time.sleep", console.slept)

    assert cluster._log_in(cast(cluster.Reconnecting, console), "install") == ""
    assert console.sent.count("install") == 2, console.sent
    assert console.settled >= cluster.PASSWORD_ECHO_BACKOFF, console.settled


def test_a_command_answer_carries_no_carriage_returns() -> None:
    """A serial line ends every line `\\r\\n`, so a check anchored with `$`
    matches nothing: `btrfs-luks` was failed at 130.3 minutes for an
    `inputmethod` check whose three lines were on its console, and `vm-greetd`
    at 59.2 for a service that was enabled and active."""
    import re

    token = 7

    class Echoing:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, line: str) -> None:
            self.sent.append(line)

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            if "BEGIN" in pattern:
                return b"MARK_7_BEGIN\r\n"
            return (
                b"/usr/bin/fcitx5\r\nXMODIFIERS=@im=fcitx\r\n"
                + command_done(token).encode()
            )

        def close(self) -> None:
            return None

    link = cluster.Reconnecting(lambda: cast(Any, Echoing()))
    said = link.expect_output("command -v fcitx5; cat /etc/environment")

    assert b"\r" not in said, said
    assert re.search(rb"(?m)^XMODIFIERS=@im=fcitx$", said), said


def test_the_passphrase_prompt_gets_a_growing_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ZFSBootMenu answered `Key load error: Incorrect key provided for
    'zpcala'` twice and dropped `zbm-unlock` into its emergency shell after an
    install that had finished. The passphrase prompt turns the echo off with
    `TCSAFLUSH`, which discards what was typed before it — the same race the
    installed system's login lost, and the same answer: wait longer each time
    rather than repeat the wait that just failed."""
    import inspect

    waited: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: waited.append(seconds))

    class Asking:
        console = _Screen(b"")

        def __init__(self) -> None:
            self.said: list[str] = []

        def observe(self, pattern: str, timeout: float, *, solicit: bool = False) -> bytes:
            return b"Enter passphrase for 'zpcala': "

        def respond(self, line: str) -> None:
            self.said.append(line)

    link = Asking()
    from gentoo_install.exec.config import load

    installation = load(FIXTURES / "zbm-unlock.toml")
    result = cluster._unlock(cast(Any, _Keyboard()), cast(Any, link), installation)

    assert result.refused, result
    from tests.vm.console import DISK_PASSPHRASE

    assert link.said == [DISK_PASSPHRASE] * cluster.UNLOCK_TRIES, link.said
    # Growing, not repeated: the second answer waits longer than the first.
    assert len(waited) >= cluster.UNLOCK_TRIES, waited
    typed = waited[-cluster.UNLOCK_TRIES:]
    assert typed == sorted(typed) and typed[-1] > typed[0], waited


class _Keyboard:
    def send_keys(self, keys: list[str]) -> None:
        return None


def test_a_stale_password_prompt_does_not_take_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ext3` was failed at 33.6 minutes with

        Maximum number of tries exceeded install\r\r\nPassword:

    on its console: `login` had given up, agetty printed a fresh name prompt
    and swallowed the name typed into its flush window, and the `Password:`
    the attempt before had left in the buffer read as an answer — so the
    password went into the name field. The name's own echo tells them apart."""
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    class Agetty:
        """A guest at a name prompt, with one prompt left over from before.

        The first name lands in agetty's flush window and is discarded, which
        is the state the harness cannot see and the echo reports.
        """

        console = _Screen(b"")

        def __init__(self) -> None:
            self.sent: list[str] = []
            self.pending: list[bytes] = [b"Password: "]
            self.wants = "name"
            self.flushed = False
            self.names: list[str] = []

        def respond(self, line: str) -> None:
            self.sent.append(line)
            if self.wants == "name":
                if not self.flushed:
                    self.flushed = True
                    return
                self.names.append(line)
                self.pending.append(line.encode() + b"\r\n")
                self.pending.append(b"Password: ")
                self.wants = "password"
                return
            self.wants = "name"
            if self.names[-1:] == [cluster.NAME] and line == "install":
                self.pending.append(b"\r\nplain ~ # ")
                return
            self.pending.append(b"\r\nLogin incorrect\r\nplain login: ")

        def observe(self, pattern: str, timeout: float = 0.0, *, solicit: bool = False) -> bytes:
            import re as _re

            while self.pending:
                said = self.pending.pop(0)
                if _re.search(pattern.encode(), said):
                    return said
            raise ConsoleTimeout(f"never matched {pattern!r}")

    console = Agetty()

    assert cluster._log_in(cast(cluster.Reconnecting, console), "install") == "", (
        console.sent
    )
    # The password reached the password field and nothing else did: the name
    # was offered twice because the first one was flushed.
    assert console.names == [cluster.NAME], console.names
    assert console.sent == [cluster.NAME, cluster.NAME, "install"], console.sent


def test_a_verdict_with_no_log_does_not_print_a_path_that_is_not_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ERROR vm-image: the cluster had no capacity for 121s (None)` — the
    parenthetical is where every other line puts a file to open, and a job
    refused before a guest existed has none."""
    from pathlib import Path as _Path

    from tests.vm import cluster

    refused = cluster.Outcome(
        name="vm-image",
        verdict=cluster.Verdict.ERROR,
        seconds=121.0,
        detail="the cluster had no capacity for 121s",
    )
    kept = cluster.Outcome(
        name="vm-btrfs",
        verdict=cluster.Verdict.FAIL,
        seconds=1.0,
        detail="the installer exited b'4'",
        log=_Path("/somewhere/vm-btrfs.log"),
    )

    for one in (refused, kept):
        where = f" ({one.log})" if one.log is not None else ""
        print(f"  {one.verdict.value} {one.name}: {one.detail}{where}")
    said = capsys.readouterr().out.splitlines()

    assert said[0].endswith("the cluster had no capacity for 121s"), said[0]
    assert "None" not in said[0], said[0]
    assert said[1].endswith("(/somewhere/vm-btrfs.log)"), said[1]

    # And `main` prints it that way rather than the two agreeing by accident.
    import inspect

    printed = inspect.getsource(cluster.main)
    assert 'f" ({one.log})" if one.log is not None else ""' in printed, printed


def test_an_image_fixture_is_refused_where_nothing_mounts_a_disk_for_it() -> None:
    """`vm-image` asked for a 20 GiB sparse file on a path that is tmpfs on
    the minimal ISO. The stage3 going into it filled the guest's memory:
    `EXT4-fs (loop1p2): failed to convert unwritten extents to written extents
    -- potential data loss! (error -5)`, then `No space left on device`, and
    no room left to write an exit code — so the verdict was `the installer
    exited b\'\'` after two minutes. `tests/vm/run.py` makes a filesystem on
    the spare target disk and mounts it; this runner does not."""
    import pytest as _pytest

    from tests.vm import cluster

    with _pytest.raises(SystemExit, match="only tests/vm/run.py mounts a disk"):
        cluster.fixtures(["vm-image"])

    # And a fixture that installs onto a disk is dispatched as it always was.
    assert [one.name for one in cluster.fixtures(["vm-btrfs"])] == ["vm-btrfs"]


def test_the_refusal_reads_the_mode_rather_than_the_fixture_name() -> None:
    """Named, the rule protects one fixture; read from the configuration, it
    protects the next one somebody writes — including one whose image path
    looks perfectly reasonable, because the path is not what is missing."""
    from gentoo_install.exec.config import load
    from tests.vm import cluster

    image = load(Path(__file__).resolve().parents[1] / "fixtures" / "vm-image.toml")
    assert cluster._needs_a_scratch_filesystem(image) == image.disk.image
    assert image.disk.image.startswith("/mnt/"), "the path is fine; the mount is what is absent"

    ordinary = load(Path(__file__).resolve().parents[1] / "fixtures" / "vm-btrfs.toml")
    assert cluster._needs_a_scratch_filesystem(ordinary) == ""


def test_a_uefi_guest_whose_console_said_nothing_is_booted_once_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`vm-xfs` ended at 2.2 minutes with `the console delivered 0 bytes while
    the editor was asked for over 120s`, having passed the same fixture in the
    round before: the log holds `starting serial terminal on interface
    serial0` and nothing after it. The BIOS path has had a second boot since
    the beginning; the UEFI path had none."""
    from tests.vm import cluster
    from tests.vm.proxmox import GrubNotReadable

    events: list[str] = []
    attempts = {"n": 0}

    def editor(link: object, extra: str) -> None:
        attempts["n"] += 1
        events.append(f"edit:{attempts['n']}")
        if attempts["n"] == 1:
            raise GrubNotReadable(
                "the console delivered 0 bytes while the editor was asked for over 120s: b''"
            )

    class Guest:
        def reset(self) -> None:
            events.append("reset")

    class Link:
        def reopen(self, *, solicit_prompt: bool = True) -> None:
            events.append(f"reopen:{solicit_prompt}")

    monkeypatch.setattr(cluster, "append_to_cmdline", editor)
    # Caught rather than allowed to escape: a retry that was removed would end
    # this test on the exception instead of on the list it is about.
    try:
        cluster._edit_uefi_cmdline(cast(Any, Guest()), cast(Any, Link()))
    except GrubNotReadable as escaped:
        events.append(f"raised:{escaped}")
    assert events == ["edit:1", "reset", "reopen:False", "edit:2"], events


def test_a_uefi_guest_whose_grub_was_readable_is_not_booted_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry is for silence. A GRUB that drew something and still could
    not be edited is a different failure and must not cost a second boot."""
    from tests.vm import cluster
    from tests.vm.proxmox import GrubNotReadable

    events: list[str] = []

    def editor(link: object, extra: str) -> None:
        events.append("edit")
        raise GrubNotReadable("GRUB never opened its editor: b'\\x1b[2JBoot LiveCD'")

    class Guest:
        def reset(self) -> None:
            events.append("reset")

    class Link:
        def reopen(self, *, solicit_prompt: bool = True) -> None:
            events.append("reopen")

    monkeypatch.setattr(cluster, "append_to_cmdline", editor)
    with pytest.raises(GrubNotReadable, match="never opened"):
        cluster._edit_uefi_cmdline(cast(Any, Guest()), cast(Any, Link()))
    assert events == ["edit"], events


def test_the_silent_console_sentence_is_the_one_the_editor_writes() -> None:
    """Two spellings of the same sentence is a retry that never fires."""
    import inspect

    from tests.vm import cluster, proxmox

    assert cluster.SILENT_EDITOR in inspect.getsource(proxmox._editor_screen)


def test_a_reconnect_grant_never_shortens_the_ceiling() -> None:
    """`vm-desktop` was ended at `899s of 898s elapsed` with
    `dev-qt/qtsensors` still compiling, six hours into a run whose ceiling had
    hours left: a console drop bought a fifteen-minute grant, and assigning it
    replaced the remaining eight-hour ceiling with those fifteen minutes. The
    grant is for a ceiling that ran out, not a discount on one that has not."""
    import time as clock

    from tests.vm import cluster
    from tests.vm.console import ConsoleClosed

    class Watch:
        def moved(self) -> bool:
            return True

        def idle_reason(self) -> str | None:
            return None

    deadlines: list[float] = []
    drops = {"n": 0}

    def operation(deadline: float) -> str:
        deadlines.append(deadline)
        drops["n"] += 1
        if drops["n"] == 1:
            raise ConsoleClosed("the console dropped")
        return "finished"

    class Link(cluster.Reconnecting):
        def __init__(self) -> None:
            self._tries = 1

        def reopen(self, *, solicit_prompt: bool = True) -> None:
            return None

    started = clock.monotonic()
    link = Link()
    assert link._with_reconnect(cluster.RUN_CEILING, operation, watch=cast(Any, Watch())) == (
        "finished"
    )

    assert len(deadlines) == 2, deadlines
    # The second attempt keeps what the ceiling had left rather than taking
    # the grant in its place.
    assert deadlines[1] >= deadlines[0], deadlines
    assert deadlines[1] - started > cluster.RECONNECT_GRANT * 2, deadlines


def test_a_grant_still_buys_time_when_the_ceiling_has_run_out() -> None:
    """The other direction: a drop after the ceiling is exhausted is what the
    grant was written for, and it has to keep working."""
    import time as clock

    from tests.vm import cluster
    from tests.vm.console import ConsoleClosed

    class Watch:
        def moved(self) -> bool:
            return True

        def idle_reason(self) -> str | None:
            return None

    deadlines: list[float] = []
    drops = {"n": 0}

    def operation(deadline: float) -> str:
        deadlines.append(deadline)
        drops["n"] += 1
        if drops["n"] == 1:
            raise ConsoleClosed("the console dropped")
        return "finished"

    class Link(cluster.Reconnecting):
        def __init__(self) -> None:
            self._tries = 1

        def reopen(self, *, solicit_prompt: bool = True) -> None:
            return None

    started = clock.monotonic()
    assert Link()._with_reconnect(0.0, operation, watch=cast(Any, Watch())) == "finished"

    assert len(deadlines) == 2, deadlines
    assert deadlines[1] - started >= cluster.RECONNECT_GRANT - 1.0, deadlines


def test_an_address_lease_outlives_the_run_that_holds_it() -> None:
    """The lease was six hours against an eight-hour ceiling, and `vm-desktop`
    ran 6.3. A guest that outlives its lease keeps its address while another
    campaign reserves it, and the second guest's initramfs answers `dracut
    Warning: Duplicate address detected for 10.31.0.151 for interface eth0` —
    which is how `zbm-unlock` lost 65 minutes to `no ssh daemon on port
    2222`."""
    from tests.vm import cluster

    assert cluster.LEASE_SECONDS > cluster.RUN_CEILING, (
        cluster.LEASE_SECONDS,
        cluster.RUN_CEILING,
    )
    # And by enough for the boots either side of the install and the checks
    # between them, rather than by a second.
    assert cluster.LEASE_SECONDS - cluster.RUN_CEILING >= cluster.LEASE_MARGIN


def test_a_lease_older_than_any_run_is_taken_over(tmp_path: Path) -> None:
    """The other half: a schedule that was killed never released what it took,
    and sixteen rounds once left a hundred leases behind. A lease nothing can
    still be holding has to be reusable, or the pool empties."""
    import os
    import time as clock

    from tests.vm import cluster

    pool = cluster.AddressPool(tmp_path, lambda address: False)
    first = pool.reserve("10.31.0.150")
    assert first == "10.31.0.150"

    # A second reservation steps over the live lease.
    assert pool.reserve("10.31.0.150") == "10.31.0.151"

    # Aged past any possible run, the first is handed out again.
    lease = tmp_path / "addresses" / first
    stale = clock.time() - cluster.LEASE_SECONDS - 1.0
    os.utime(lease, (stale, stale))
    assert pool.reserve("10.31.0.150") == first


def test_a_round_waits_longer_when_our_own_guests_hold_the_room() -> None:
    """`vm-image` was failed with `the cluster had no capacity for 121s` while
    ten guests of this harness were still installing. That room is provably
    coming back; room held by somebody else's machines is not, and a cluster
    that cannot answer at all is not either."""
    from tests.vm import cluster
    from tests.vm.proxmox import ProxmoxError

    class Busy:
        def ours(self) -> list[tuple[str, int]]:
            return [("infra-node4", 9300), ("infra-node1", 9303)]

    class Empty:
        def ours(self) -> list[tuple[str, int]]:
            return []

    class Silent:
        def ours(self) -> list[tuple[str, int]]:
            raise ProxmoxError("GET /cluster/resources did not answer")

    assert cluster._capacity_patience(cast(Any, Busy())) == cluster.CAPACITY_PATIENCE_SHARED
    assert cluster._capacity_patience(cast(Any, Empty())) == cluster.CAPACITY_PATIENCE
    assert cluster._capacity_patience(cast(Any, Silent())) == cluster.CAPACITY_PATIENCE

    # And the longer wait is bounded, so a guest nothing collects cannot hold
    # a round for ever.
    assert cluster.CAPACITY_PATIENCE_SHARED > cluster.CAPACITY_PATIENCE
    assert cluster.CAPACITY_PATIENCE_SHARED <= cluster.RUN_CEILING
