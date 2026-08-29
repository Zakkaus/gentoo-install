# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from typing import Any, cast

import hashlib
import io
import re
import time
import urllib.request
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest

from gentoo_install.model.config import MirrorRegion, Sync
from tests.vm import cluster, driver

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
from tests.vm.console import ConsoleTimeout, SerialConsole, command_done
from tests.vm.proxmox import (
    Api,
    Node,
    ProxmoxError,
    ProxmoxNotFound,
    ProxmoxTransientError,
    Traffic,
    VMID_FIRST,
    VMID_LAST,
)


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
        if path.startswith("/cluster/resources"):
            # The startup orphan report asks; this world has no leftovers.
            return []
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
        cluster.Watchdog(tmp_path / "odd-sized.log", lambda: Traffic(0, 0, 0.0)),
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
    counters: Callable[[], Traffic | None],
    output_at: float | None = None,
) -> tuple[cluster.Reconnecting, cluster.Watchdog]:
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    serial = SerialConsole(TimedChannel(clock, output_at), BytesIO())
    link = cluster.Reconnecting(lambda: serial)
    watch = cluster.Watchdog(
        tmp_path / "install.log",
        counters,
        where="infra-node2",
        load=lambda: 0.99,
        state=lambda: "io-error",
    )
    return link, watch


def test_install_wait_continues_when_silent_guest_moves_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    traffic = [0]

    def counters() -> Traffic:
        traffic[0] += cluster.QUIET_BYTES * 2
        return Traffic(traffic[0], 0, 0.0)

    link, watch = _timed_wait(monkeypatch, tmp_path, clock, counters, output_at=3.0)
    link.wait_for("install", timeout=5.0, idle=2.0, watch=watch)

    assert clock[0] == 3.0
    assert traffic[0] == cluster.QUIET_BYTES * 2


def test_install_wait_names_silent_console_and_flat_counters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    # A guest whose network counter sits at a number and whose disk sits at
    # zero: summed, both shapes read the same and the verdict could not say
    # which of the two had stopped.
    link, watch = _timed_wait(
        monkeypatch, tmp_path, clock, lambda: Traffic(4096, 0, 0.0)
    )
    watch.log.write_bytes(b"output before the idle window\n")

    with pytest.raises(ConsoleTimeout) as raised:
        link.wait_for("install", timeout=5.0, idle=2.0, watch=watch)

    message = str(raised.value)
    # Two poke windows after the idle one: a fresh console is opened and read,
    # then asked. That time is spent only on a guest about to be ended, and it
    # buys the one fact the counters cannot give.
    assert clock[0] == 2.0 + 2 * cluster.POKE_PATIENCE, clock[0]
    assert "console was silent" in message
    # This console answers nothing, which is what a guest that has really
    # stopped looks like -- and it now says so instead of leaving it open.
    # Without the proxy's greeting, because this fake never sends one: that
    # clause is the difference between a console that is gone and a guest
    # that stopped.
    assert "wrote nothing" in message, message
    assert "the proxy greeted it" not in message, message
    assert "counters were flat" in message
    assert "network 4096 -> 4096" in message, message
    assert "disk 0 -> 0" in message, message
    # And where, because the cluster's other tenants are not this campaign's:
    # a guest that goes to cpu 0.00 mid-compile is a different finding on a
    # node at 100% than on an idle one, and the verdict was the only record.
    assert "on infra-node2" in message, message
    # And what the node was doing then: a guest at `cpu 0.00` on a node at 99%
    # is the cluster having no time for it, which is not the same finding as a
    # guest that stopped. Two were ended this way on one day.
    assert "the node itself at 99%" in message, message
    # And what qemu calls it, when that is not `running`: a guest paused by a
    # storage error reads exactly like one that stopped on its own.
    assert "qemu calls the guest io-error" in message, message


def test_a_console_that_answers_when_reopened_says_so_in_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The difference between a guest that stopped and a console that went.

    Five stalls on this cluster read `the console was silent` mid-compile, and
    the counters cannot separate the two: these guests keep
    `/var/tmp/portage` in RAM, so `gi-vm-desktop` was measured compiling
    `fcitx-rime` with both disk counters frozen and `cpu 0.00`. A fresh
    console that prints at once is a guest that never stopped.
    """
    clock = [0.0]
    talking = _Talkative(clock)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    serial = SerialConsole(TimedChannel(clock), BytesIO())
    # The first console is the silent one the wait gave up on; the one the
    # poke opens is the guest still printing.
    opened = [0]

    def _open() -> SerialConsole:
        opened[0] += 1
        return serial if opened[0] == 1 else SerialConsole(talking, BytesIO())

    link = cluster.Reconnecting(_open)
    watch = cluster.Watchdog(
        tmp_path / "install.log", lambda: Traffic(4096, 0, 0.0), where="infra-node2"
    )
    watch.log.write_bytes(b"output before the idle window\n")

    with pytest.raises(ConsoleTimeout) as raised:
        link.wait_for("install", timeout=5.0, idle=2.0, watch=watch)

    message = str(raised.value)
    assert "showed" in message and "at once" in message, message
    assert "still compiling" in message, message
    # And it never had to be asked, so nothing was written to a guest that is
    # working: `vm-luks` lost `grub-install` at operation 56 of 56 that way.
    assert "until it was asked" not in message, message


class _Talkative:
    """A console that prints the moment anything reads it."""

    def __init__(self, clock: list[float], says: bytes = b"[3/24] still compiling\n") -> None:
        self._clock = clock
        self._says = says

    def recv(self, size: int) -> bytes:
        self._clock[0] += 1.0
        return self._says

    def sendall(self, data: bytes) -> None:
        return None

    def close(self) -> None:
        return None

    @property
    def closed(self) -> bool:
        return False


def test_the_proxys_own_greeting_is_not_the_guest_speaking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`zfs-zbm` stalled at 95.5 minutes and the poke answered `a new console
    showed b'OKstarting serial terminal on interface serial0'`, which is what
    Proxmox writes when a session attaches and nothing the guest did. The
    check matched the greeting opening it produced -- the same shape as a
    shell echoing the command that asked it a question."""
    clock = [0.0]
    banner = _Talkative(clock, b"OKstarting serial terminal on interface serial0\r\n")
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    serial = SerialConsole(TimedChannel(clock), BytesIO())
    opened = [0]

    def _open() -> SerialConsole:
        opened[0] += 1
        return serial if opened[0] == 1 else SerialConsole(banner, BytesIO())

    link = cluster.Reconnecting(_open)
    watch = cluster.Watchdog(
        tmp_path / "install.log", lambda: Traffic(4096, 0, 0.0), where="infra-node1"
    )
    watch.log.write_bytes(b"output before the idle window\n")

    with pytest.raises(ConsoleTimeout) as raised:
        link.wait_for("install", timeout=5.0, idle=2.0, watch=watch)

    message = str(raised.value)
    assert "at once" not in message, message
    # And it says the proxy answered, because a console that is gone and a
    # guest that has stopped are the two readings and both look like silence.
    assert "the proxy greeted it" in message, message
    assert "wrote nothing" in message, message


def test_a_guest_at_a_prompt_is_not_reported_as_a_stall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`vm-cjk-kernel` printed `MARK_21_BEGIN`, stopped mid-compiler-line, and
    answered `root@livecd ~ #` when the poke asked at 178.9 minutes.

    The guest never stopped: its command ended and the console stopped
    delivering, so the marker went into a connection nobody was receiving on.
    Counted as a stall it kept row 402 looking like one defect when it is two,
    which is what made it take fifteen rounds.
    """
    clock = [0.0]
    prompt = _Talkative(clock, b"\x1b[?2004l\x1b[?2004hroot@livecd ~ # ")
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    serial = SerialConsole(TimedChannel(clock), BytesIO())
    opened = [0]

    def _open() -> SerialConsole:
        opened[0] += 1
        # Silent when read, talking only after the poke's newline: the shell
        # redraws its prompt when something is typed at it.
        return serial if opened[0] == 1 else SerialConsole(_Quiet(clock, prompt), BytesIO())

    link = cluster.Reconnecting(_open)
    watch = cluster.Watchdog(
        tmp_path / "install.log", lambda: Traffic(4096, 0, 0.0), where="infra-node5"
    )
    watch.log.write_bytes(b"output before the idle window\n")

    with pytest.raises(ConsoleTimeout) as raised:
        link.wait_for("install", timeout=5.0, idle=2.0, watch=watch)

    message = str(raised.value)
    assert "at a shell prompt" in message, message
    assert "the console stopped delivering" in message, message


class _Quiet:
    """Silent until something is written to it, then whatever it was given."""

    def __init__(self, clock: list[float], after: object) -> None:
        self._clock = clock
        self._after = after
        self._written = False

    def recv(self, size: int) -> bytes:
        self._clock[0] += 1.0
        return cast(Any, self._after).recv(size) if self._written else b""

    def sendall(self, data: bytes) -> None:
        self._written = True

    def close(self) -> None:
        return None

    @property
    def closed(self) -> bool:
        return False


def test_run_ceiling_ends_silent_guest_that_keeps_moving_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    readings = [0]

    def counters() -> Traffic:
        readings[0] += 1
        return Traffic(readings[0] * cluster.QUIET_BYTES * 2, 0, 0.0)

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
    import re
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
        assert re.search(init[2], f"{expected}\n"), init
        assert not re.search(init[2], f"{expected}-runtime\n"), init
        if wanted:
            assert expected == wanted, init


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
        watch=cluster.Watchdog(log, lambda: Traffic(0, 0, 0.0)),
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
        counters=(lambda: Traffic(next(counters), 0, 0.0)) if busy else (lambda: Traffic(0, 0, 0.0)),
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
    import ast
    import inspect
    from types import ModuleType
    import textwrap

    from tests.vm import proxmox

    # The class the module catches, not a name that reads like it: a local
    # `class ProxmoxTransientError(Exception)` in `tests/vm/cluster.py` would
    # satisfy every text search here and catch nothing the API raises.
    assert getattr(cluster, "ProxmoxTransientError") is proxmox.ProxmoxTransientError

    # The `try` that wraps the call which creates the guest, found in the tree
    # rather than by where the words sit: a handler placed around something
    # else keeps the same words in the same order.
    tree = ast.parse(textwrap.dedent(inspect.getsource(cluster.run)))
    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and any(
            isinstance(call.func, ast.Name) and call.func.id == "_reserve_job"
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )
        and any(
            isinstance(handler.type, ast.Name)
            and handler.type.id == "ProxmoxTransientError"
            for handler in node.handlers
        )
    ]
    assert len(guarded) == 1, "one guard, on the call that creates the guest"
    handler = next(
        one
        for one in guarded[0].handlers
        if isinstance(one.type, ast.Name) and one.type.id == "ProxmoxTransientError"
    )
    # The calls, not the words: a log line naming both reads the same, and
    # `unreachable.discard(node.name)` leaves the refusing node in the round.
    made = {
        (statement.value.func.value.id, statement.value.func.attr)
        + tuple(ast.unparse(one) for one in statement.value.args)
        for statement in handler.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Attribute)
        and isinstance(statement.value.func.value, ast.Name)
    }
    assert ("unreachable", "add", "node.name") in made, made
    assert ("waiting", "insert", "index", "job") in made, made
    assert isinstance(handler.body[-1], ast.Continue), ast.unparse(handler.body[-1])


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


def test_a_verdict_says_how_far_the_installer_had_reached(tmp_path: Path) -> None:
    """A marker names one of this harness's own commands, not an installer
    operation, and the two were read as the same thing: `btrfs-luks` was
    reported as `never matched \'MARK_24_DONE\'` with the installer at its own
    operation 78 of 90, three from the end of its package set.

    Sampled from a kept cluster log, so the shape is the one `exec/apply.py`
    actually writes.
    """
    log = tmp_path / "a-guest.log"
    log.write_bytes(
        b"| >>> Emerging (1 of 4) sys-apps/portage\r\n"
        b"[76/90 0:27:38] [bootloader] write /etc/default/grub with cmdline\r\n"
        b"[78/90 0:27:55] [packages] install the plasma group: emerge kde-plasma\r\n"
        b"|  * kwin-6.6.6-1.gpkg.tar MD5 SHA1 size ;-) ...           [ ok ]\r\n"
    )
    said = cluster.how_far_it_got(log)
    assert said.startswith("reached [78/90"), said
    assert "install the plasma group" in said, said

    # A guest that printed no operation says nothing rather than naming the
    # last thing a build wrote.
    quiet = tmp_path / "quiet.log"
    quiet.write_bytes(b"| >>> Emerging (1 of 4) sys-apps/portage\r\n")
    assert cluster.how_far_it_got(quiet) == ""
    assert cluster.how_far_it_got(tmp_path / "never-made") == ""


def test_a_run_refused_before_it_started_names_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cli.py` writes `the install stopped: ` only once `install()` is running,
    so a configuration refused before that left the whole verdict at `the
    installer exited b\'1\'`. The reason was in the file the guest handed back.

    The refusal is produced by `cli.main`, not written here, so a renamed
    prefix fails this test rather than going quiet in a verdict.
    """
    from gentoo_install import cli as real_cli
    from gentoo_install.cli import EXIT_CONFIG, main
    from tests.vm.results import LOG_TAIL

    broken = tmp_path / "broken.toml"
    broken.write_text(
        (Path(__file__).resolve().parents[1] / "fixtures" / "vm-xfs.toml")
        .read_text()
        .replace('locale = "en_US.UTF-8"', 'locale = "zh_CH.UTF-8"')
    )
    monkeypatch.setattr(real_cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(real_cli, "_check_the_clock", lambda: None)
    assert main(["--config", str(broken), "--dry-run"]) == EXIT_CONFIG
    printed = capsys.readouterr().err
    assert printed.strip(), "the refusal printed nothing"

    reason = cluster._why_it_stopped({LOG_TAIL: printed.encode()})
    assert "configuration:" in reason, (reason, printed)
    assert "zh_CH.UTF-8" in reason, reason

    # And a build's own output is still not a reason: that decision is held by
    # `test_a_stopped_install_says_why` and this must not overturn it.
    assert cluster._why_it_stopped({LOG_TAIL: b"| >>> Emerging app-i18n/ibus\n"}) == ""


def test_a_layout_that_cannot_fit_this_runners_target_is_refused_here() -> None:
    """Every guest here gets the same `TARGET_GIB`. `vm-shares` asks `40%` of
    the disk for a root that declares `min = "20GiB"`, which is 16178MiB at
    that size, so the installer refuses it — correctly — after the round has
    taken a node and 1.9 minutes.

    The local campaign already names this fixture and its reason, but that is
    a list only the local runner reads. This refusal is derived from
    `TARGET_GIB`, so it follows the size this runner gives.
    """
    import pytest

    with pytest.raises(SystemExit, match="20GiB"):
        cluster.fixtures(["vm-shares"])

    # Negative control: the fixtures that fit are still dispatched, including
    # the one whose layout comes from the machine rather than the file.
    assert [one.name for one in cluster.fixtures(["vm-xfs", "vm-convert"])] == [
        "vm-xfs",
        "vm-convert",
    ]


def test_the_two_runners_agree_on_which_fixtures_a_guest_cannot_run() -> None:
    """A fixture excluded for a reason that holds on both runners has to be
    excluded on both. `vm-shares` was named in the local table and dispatched
    here anyway, which is one rule kept in two places with one of them
    complete."""
    import pytest

    from tests.unit.test_harness import NOT_IN_THE_CAMPAIGN

    both: dict[str, str] = {"vm-shares.toml": "20GiB"}
    assert set(both) <= NOT_IN_THE_CAMPAIGN, sorted(set(both) - NOT_IN_THE_CAMPAIGN)
    for fixture, said in both.items():
        with pytest.raises(SystemExit, match=said):
            cluster.fixtures([fixture.removesuffix(".toml")])


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
    ordinary = code.index('f"the installer exited {code!r}')
    assert expected < ordinary, "the exception has to be read before the general rule"

    # And the two agree on which fixture it is: one fact, two runners.
    from tests.vm import campaign

    failing = {
        Path(one.config).stem
        for stage in campaign.STAGES.values()
        for one in stage
        if one.expectation is not None and one.expectation.must_stop
    }
    assert failing == set(cluster.EXPECTED_TO_FAIL), (failing, cluster.EXPECTED_TO_FAIL)


def test_the_console_carries_the_whole_log_until_it_cannot(tmp_path: Path) -> None:
    """`vm-source-kernel` writes 80 MB of build output, which gzips to 4.5 MiB
    and reaches the reader as one base64 line of about six: it is the only
    fixture that has ever failed while its results were being read. Everything
    else the cluster kept is between 3.9 MB and 25 MB and crossed the console
    before the tail existed, and the whole log is what a defect is found in.

    The shell is run rather than read: it decides between the two on the
    guest, where nothing else can.
    """
    import base64
    import io
    import subprocess
    import tarfile

    from tests.vm.results import (
        CONSOLE_CLOSE,
        CONSOLE_OPEN,
        FULL_LOG_BYTES,
        LOG_TAIL,
        LOG_TAIL_BYTES,
        console_command,
    )

    said = console_command("/tmp/gentoo-install-results")
    assert f"tail -c {LOG_TAIL_BYTES}" in said, said
    assert f"/{LOG_TAIL}" in said, said
    assert str(FULL_LOG_BYTES) in said, said

    def carried(size: int) -> set[str]:
        room = tmp_path / f"results-{size}"
        room.mkdir()
        (room / "install.txt").write_bytes(b"x" * size)
        (room / "install.rc").write_text("0\n")
        (room / "install.jsonl").write_text("{}\n")
        printed = subprocess.run(
            ["sh", "-c", console_command(str(room), limit=1024)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        blob = printed.split(CONSOLE_OPEN)[1].split(CONSOLE_CLOSE)[0]
        packed = base64.b64decode("".join(blob.split()), validate=True)
        with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
            return {Path(one).name for one in archive.getnames() if one != "."}

    assert carried(1024) == {"install.txt", "install.tail", "install.rc", "install.jsonl"}
    # Over the limit the log stays behind and everything the verdict is made
    # of still travels.
    assert carried(1025) == {"install.tail", "install.rc", "install.jsonl"}


def test_a_failed_install_verdict_carries_the_installer_s_own_reason() -> None:
    """`the installer exited b'4'` was the whole verdict, and `vm-greetd`'s
    reason was 294910 lines into `install.txt`. `cli` writes that reason as
    its last line, and the guest hands the file back with everything else.
    """
    from tests.vm.results import LOG_TAIL

    said = (
        "| >>> Installing app-i18n/ibus-1.5.33\n"
        "|  * ERROR: app-i18n/ibus-1.5.33::gentoo failed (compile phase):\n"
        "the install stopped: CommandFailed: emerge ended with exit 1: "
        "keybindingmanager.c:1422:1: error: type defaults to 'int'\n"
    ).encode()
    reason = cluster._why_it_stopped({LOG_TAIL: said})
    assert reason.startswith(": CommandFailed: emerge ended with exit 1"), reason
    assert "keybindingmanager.c" in reason, reason

    # A run whose installer never got that far says nothing rather than
    # guessing from the build output above it.
    assert cluster._why_it_stopped({LOG_TAIL: b"| >>> Emerging app-i18n/ibus\n"}) == ""
    assert cluster._why_it_stopped({}) == ""

    # An archive an older revision made carries the whole log instead.
    assert "CommandFailed" in cluster._why_it_stopped({"install.txt": said})


def test_an_expected_failure_is_the_failure_the_fixture_measures() -> None:
    """`vm-proxy-dead` passes by stopping, and the rule for that was
    `code != b"0"`. A guest whose results never came back hands the caller an
    empty code, which satisfied it; so did any other stop — a syntax error, a
    refused preflight, a failed disk — while the fixture exists to show that
    nothing bypassed a proxy pointed at a dead port.

    Measured on a passing run: `install.rc` held `4` and `Connection refused`
    appeared 26 times in the log it handed back.
    """
    from tests.vm.results import LOG_TAIL

    refused = {LOG_TAIL: b"wget: unable to connect: Connection refused\n"}
    assert cluster._did_not_stop("vm-proxy-dead", b"4", refused) == ""
    assert "wrote no exit code" in cluster._did_not_stop("vm-proxy-dead", b"", refused)
    assert "did not" in cluster._did_not_stop("vm-proxy-dead", b"0", refused)
    # A stop for another reason is not this fixture's result.
    assert "not the b'4'" in cluster._did_not_stop("vm-proxy-dead", b"1", refused)
    other = {LOG_TAIL: b"the install stopped: DeviceNotFound: /dev/disk/by-id/x\n"}
    said = cluster._did_not_stop("vm-proxy-dead", b"4", other)
    assert "Connection refused" in said and "not what this fixture measures" in said
    # The whole log counts as well as the tail: a log under the size limit
    # travels whole and the tail is the fallback.
    whole = {"install.txt": b"wget: unable to connect: Connection refused\n"}
    assert cluster._did_not_stop("vm-proxy-dead", b"4", whole) == ""


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

        def send_raw(self, keys: str) -> None:
            # The protocol has it, so the double does: a reopen clears the
            # line before it asks for a prompt.
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
    assert first.watch.log.name.startswith("vm-greetd-9301-")
    assert second.watch.log.name.startswith("vm-greetd-9302-")


def test_a_guest_qemu_calls_running_adds_nothing_to_the_reason(tmp_path: Path) -> None:
    """`running` is the ordinary answer and saying it would push the counters
    out of a verdict that is cut to length."""
    watch = cluster.Watchdog(
        tmp_path / "install.log",
        lambda: Traffic(0, 0, 0.0),
        where="infra-node4",
        state=lambda: "running",
    )
    reason = watch.idle_reason()
    assert reason is not None
    assert "qemu calls the guest" not in reason, reason


def test_a_node_that_will_not_answer_leaves_the_reason_readable(tmp_path: Path) -> None:
    """The node's load is read once, when the guest is about to be called
    stuck. A cluster that will not answer that request must not take the rest
    of the reason with it.
    """
    def refusing() -> float | None:
        return None

    watch = cluster.Watchdog(
        tmp_path / "install.log", lambda: Traffic(0, 0, 0.0), where="infra-node4", load=refusing
    )
    reason = watch.idle_reason()
    assert reason is not None
    assert "on infra-node4" in reason, reason
    assert "the node itself" not in reason, reason


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

        def send_raw(self, keys: str) -> None:
            # The protocol has it, so the double does.
            return None

        def close(self) -> None:
            return None

    opened: list[Console] = []
    sampled: list[int] = []

    def counters() -> Traffic | None:
        # A guest whose disk keeps growing while its console says nothing.
        sampled.append(len(sampled))
        return Traffic(0, cluster.QUIET_BYTES * 2 * (len(sampled) + 1), 0.0)

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

    still = cluster.Watchdog(log=log, counters=lambda: Traffic(0, 0, 0.0))
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
    # Against what the call site actually gives it, with room for the `ip` and
    # `dmesg` reads that follow. Both numbers are derived from the same
    # `LOOKUP_PATIENCE`, so an absolute ceiling is what keeps the probe from
    # growing: it is read three times per install and once ended every run it
    # was measuring.
    assert worst + 20 < cluster.PROBE_PATIENCE, (worst, cluster.PROBE_PATIENCE)
    assert cluster.PROBE_PATIENCE <= 180.0, cluster.PROBE_PATIENCE
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
    # Nor an interrupt: at a `login:` prompt that is an answer too.
    assert cluster.INTERRUPT not in opened[1].sent, opened[1].sent


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

    # The empty line is the request for a prompt, and it is the whole of it:
    # the interrupt clears a half-written line, so a reopen with no write in
    # flight has nothing to clear. Sent anyway it killed what the guest was
    # running, and four finished installs in one round were recorded as errors.
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


def test_the_silent_console_sentence_is_the_one_the_editor_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two spellings of the same sentence is a retry that never fires. Read off
    the exception the editor raises, not out of its source: the words can sit
    in a comment while the raise says something else."""
    from tests.vm import cluster, proxmox
    from tests.vm.proxmox import ProxmoxError

    monkeypatch.setattr("time.sleep", lambda seconds: None)

    class Console:
        def send_raw(self, keys: str) -> None:
            return None

        def snapshot(self, seconds: float) -> bytes:
            return b"\r\n"

    with pytest.raises(ProxmoxError) as raised:
        proxmox._editor_screen(cast(Any, Console()), 0.05)
    assert cluster.SILENT_EDITOR in str(raised.value)


def test_every_sentence_worth_another_boot_is_one_the_menu_raises() -> None:
    """The retry fired for one sentence and a second recoverable failure went
    past it: `static-ip` was lost at 0.5 minutes to `the entry booted before
    its countdown was held`, having passed at 18.2 minutes the round before.

    Read off what the code raises rather than out of its source, and over the
    whole set rather than the one that was written first.
    """
    from tests.vm import cluster, proxmox
    from tests.vm.proxmox import GrubNotReadable

    class Booting:
        """A menu whose countdown has already expired."""

        def expect(self, pattern: str, timeout: float) -> bytes:
            return b"executed automatically in 0s"

        def send_raw(self, keys: str) -> None:
            return None

        def snapshot(self, seconds: float) -> bytes:
            return b"Booting `Boot LiveCD (kernel: gentoo)'\r\n"

    with pytest.raises(GrubNotReadable) as raised:
        proxmox.hold_the_menu(cast(Any, Booting()), timeout=0.5)
    assert cluster.BOOTED_ITSELF in str(raised.value), raised.value
    # And the set the retry reads is the one both sentences are in.
    assert any(one in str(raised.value) for one in cluster.WORTH_ANOTHER_BOOT)
    assert cluster.SILENT_EDITOR in cluster.WORTH_ANOTHER_BOOT


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


def test_two_rounds_cannot_write_one_guest_log() -> None:
    """A vmid is handed back and reused, so `ext2-9303.log` was written by
    whichever round ran that fixture on 9303 last: run109's copy was gone when
    its revision was asked for, and the revision is the first line of the log.
    The driver CD is content-addressed, so its name is what says which
    installer built the guest."""
    from pathlib import Path as _Path

    from tests.vm.cluster import guest_log
    from tests.vm.driver import NAME_PREFIX

    workdir = _Path("/lab/vm/cluster")
    first = guest_log(workdir, "ext2", 9303, f"{NAME_PREFIX}53d1cb7f02a50952333.iso")
    second = guest_log(workdir, "ext2", 9303, f"{NAME_PREFIX}023374df9cee160062d.iso")
    assert first != second, first
    assert first.name.startswith("ext2-9303-"), first.name
    # The same driver twice is the same log: two guests of one round are told
    # apart by the vmid, which the pool never hands out twice at once.
    assert first == guest_log(workdir, "ext2", 9303, f"{NAME_PREFIX}53d1cb7f02a50952333.iso")
    assert first != guest_log(workdir, "ext2", 9304, f"{NAME_PREFIX}53d1cb7f02a50952333.iso")
    # A name that is not a driver CD still produces a path rather than an
    # empty tag, because the log is where a failure is read.
    assert guest_log(workdir, "ext2", 9303, "local.iso").name == "ext2-9303-driver.log"


def test_a_cluster_conversion_writes_and_reads_the_home_marker() -> None:
    """`/home` surviving is what an in-place conversion promises, and the
    cluster row said the converted machine carried what the conversion asked
    for while nothing had looked at `/home`. The local runner has checked it
    since `#697`; this path had not."""
    import inspect

    from tests.vm import cluster
    from tests.vm.convert import HOME_MARKER_CHECK

    source = inspect.getsource(cluster.convert_and_check)
    # The constant, not a second copy of the path: the local runner writes the
    # same file and reads the same marker.
    written = source.index("HOME_MARKER_PATH")
    converted = source.index("install.sh")
    assert written < converted, source
    assert source.index("ETC_MARKER_PATH") < converted, source
    assert "HOME_MARKER_CHECK, ETC_MARKER_CHECK" in source, source

    # The check itself asks the guest to count, so neither the shell's echo of
    # the command nor `cat`'s own diagnostic can answer it.
    assert "grep -Fc" in HOME_MARKER_CHECK.command
    import re

    assert not re.search(HOME_MARKER_CHECK.pattern, HOME_MARKER_CHECK.command)
    assert re.search(HOME_MARKER_CHECK.pattern, "home=1\n")
    assert not re.search(HOME_MARKER_CHECK.pattern, "home=0\n")

    # `/etc` is the other half of the same promise: replaced, not merged, so
    # the count there has to be zero, and the same counting keeps the echo
    # and the diagnostic out of the answer.
    from tests.vm.convert import ETC_MARKER_CHECK

    assert "grep -Fc" in ETC_MARKER_CHECK.command
    assert not re.search(ETC_MARKER_CHECK.pattern, ETC_MARKER_CHECK.command)
    assert re.search(ETC_MARKER_CHECK.pattern, "etc=0\n")
    assert not re.search(ETC_MARKER_CHECK.pattern, "etc=1\n")
    assert not re.search(ETC_MARKER_CHECK.pattern, "")


def test_the_extra_checks_reach_the_installed_reader() -> None:
    """`boot_and_check` grew a parameter, and a parameter nothing forwards is
    a check that never runs."""
    import ast
    import inspect
    import textwrap

    from tests.vm import cluster

    # The list `boot_and_check` iterates, read from the tree: a window of
    # characters after `_asked_for` moved when a comment was added between
    # the two, and what this holds is that both reach the same reader.
    asked = ast.parse(textwrap.dedent(inspect.getsource(cluster.boot_and_check)))
    names = {
        node.id
        for loop in ast.walk(asked)
        if isinstance(loop, ast.For)
        for node in ast.walk(loop.iter)
        if isinstance(node, ast.Name)
    }
    assert {"_asked_for", "extra"} <= names, sorted(names)


def test_a_binhost_fixture_that_compiled_everything_is_not_a_pass() -> None:
    """The mirror image of `MUST_DEGRADE`. An install that gave the binary
    host up and compiled instead finishes, boots and answers every other
    check, so `vm-binpkg` — the blocking fixture for that path — was green
    whether or not a single package came from the host it names.

    Measured on the journals the cluster had kept: every `vm-binpkg` recorded
    binary packages and none of them degraded, so this rule is red only when
    the path it guards has broken.
    """
    import json

    from gentoo_install.plan.portage import BINARY_PACKAGES
    from tests.vm import cluster

    def journal(*rows: dict[str, object]) -> dict[str, bytes]:
        return {"install.jsonl": "\n".join(json.dumps(one) for one in rows).encode()}

    served = journal(
        {"event": "package", "source": "binary", "atom": "sys-apps/portage-3.0.70"},
        {"event": "package", "source": "compiled", "atom": "dev-vcs/git-2.51.0"},
    )
    assert cluster._binary_packages_missing("vm-binpkg", served) == ""

    compiled = journal({"event": "package", "source": "compiled", "atom": "dev-vcs/git-2.51.0"})
    assert "no package from a binary host" in cluster._binary_packages_missing(
        "vm-binpkg", compiled
    )

    # A degradation is the loud form of the same thing, and its reason is
    # carried: `the host answered 404` and `the key is not trusted` are
    # different problems.
    gave_up = journal(
        {"event": "degraded", "what": BINARY_PACKAGES, "reason": "the host answered 404"},
        {"event": "package", "source": "binary", "atom": "sys-apps/portage-3.0.70"},
    )
    said = cluster._binary_packages_missing("vm-binpkg", gave_up)
    assert "gave up" in said and "404" in said, said

    # No journal is not a pass either: a guest that handed nothing back says
    # nothing about where its packages came from.
    assert "no journal" in cluster._binary_packages_missing("vm-binpkg", {})

    # The control: the rule fires on the table, not on every fixture.
    assert cluster._binary_packages_missing("vm-xfs", compiled) == ""
    assert "vm-binhost-fallback" not in cluster.MUST_USE_A_BINARY_HOST
    assert set(cluster.MUST_USE_A_BINARY_HOST) & set(cluster.MUST_DEGRADE) == set()

    # And it is asked where the verdict is decided: a rule nothing calls is
    # the same as no rule, and `tests/vm/` has no unreachable-code check.
    import inspect

    said = inspect.getsource(cluster.install_one)
    assert "_binary_packages_missing(" in said, said[:200]


def test_a_schedule_says_which_revision_it_measures() -> None:
    """Each guest's log opens with `installer revision:`, and the schedule's
    own output did not. Identifying what run109 measured meant reading a guest
    log, and a later round running the same fixture on the same vmid had
    overwritten it."""
    import inspect

    from tests.vm import cluster

    source = inspect.getsource(cluster.run)
    computed = source.index("revision_identity(driver_path)")
    said = source.index('print(f"installer revision: {revision}"')
    assert computed < said, source[computed : computed + 200]
    # Before the first guest is created, or a schedule that dies placing one
    # says nothing about what it was carrying.
    assert said < source.index("_reserve_job("), source[said : said + 200]


def test_the_summary_names_a_guest_the_run_could_not_remove() -> None:
    """`removed=False` kept `Verdict.OK`, which is right — the install did
    work — and the summary printed `10/10 passed` and nothing else. run114
    left `ext2` on `infra-node4` for hours that way, and the only trace was one
    line thousands of lines earlier."""
    import inspect

    from tests.vm import cluster

    source = inspect.getsource(cluster.main)
    counted = source.index("passed")
    said = source.index("still on the cluster")
    assert counted < said, source[counted : counted + 300]
    # On stderr, where the operator reading a redirected run still sees it.
    assert "file=sys.stderr" in source[said : said + 120], source[said : said + 200]


def test_a_conversion_counts_the_modules_its_bootloader_needs() -> None:
    """`vm-convert` failed on the cluster with `file '/boot/grub/x86_64-efi/
    normal.mod' not found. Entering rescue mode...` while the conversion's own
    log said `Installation finished. No error reported.` twice. A guest in the
    rescue shell answers nothing, so the count is taken before the reboot."""
    import ast
    import inspect
    import re
    import textwrap

    from tests.vm import cluster
    from tests.vm.convert import (
        GRUB_MODULES_CHECK,
        GRUB_PREFIX_CHECK,
        GRUB_PREFIX_REPORT,
        GRUB_READS_ITS_MODULE,
        before_the_reboot,
    )

    # In the function's own body, not inside a branch: a loop the reboot can
    # walk past is a check the failing machine never answers.
    body = ast.parse(textwrap.dedent(inspect.getsource(cluster.convert_and_check))).body[0]
    assert isinstance(body, ast.FunctionDef)
    asked = [
        n
        for n, one in enumerate(body.body)
        if isinstance(one, ast.For)
        and isinstance(one.iter, ast.Call)
        and isinstance(one.iter.func, ast.Name)
        and one.iter.func.id == "before_the_reboot"
    ]
    booted = [
        n
        for n, one in enumerate(body.body)
        if isinstance(one, ast.Return)
        and isinstance(one.value, ast.Call)
        and isinstance(one.value.func, ast.Name)
        and one.value.func.id == "boot_and_check"
    ]
    assert asked and booted and asked[0] < booted[0], ast.dump(body)

    # Derived from the configuration, because every one of these reads a UEFI
    # GRUB installation: a BIOS conversion has no `x86_64-efi` directory and
    # no stub on an esp, so asking it these three fails a working machine.
    from dataclasses import replace

    from gentoo_install.exec.config import load
    from gentoo_install.model.config import Firmware

    conversion = load(FIXTURES / "vm-convert.toml")
    asked_for = before_the_reboot(conversion)
    assert asked_for == (GRUB_MODULES_CHECK, GRUB_READS_ITS_MODULE, GRUB_PREFIX_CHECK)
    on_bios = replace(
        conversion,
        bootloader=replace(conversion.bootloader, firmware=Firmware.BIOS),
    )
    assert before_the_reboot(on_bios) == ()

    # Both answers are counted by the guest, so neither can come from the
    # echo, and neither counts nothing as a pass.
    for check, answer in (
        (GRUB_MODULES_CHECK, "grubmods=214\n"),
        (GRUB_READS_ITS_MODULE, "grubread=182672\n"),
        (
            GRUB_PREFIX_CHECK,
            "grubstub=163840 grubprefix=1 boot=c866ae3a-68e7-4096-9703-dfa17070c3ed"
            " esp=1234-ABCD stub=c866ae3a-68e7-4096-9703-dfa17070c3ed"
            " embedded=(hd0,gpt2)/boot/grub drive=hd0,gpt2 fs=xfs\n",
        ),
    ):
        assert "wc -" in check.command or "grep -ac" in check.command
        assert not re.search(check.pattern, check.command)
        assert re.search(check.pattern, answer)
        assert not re.search(check.pattern, re.sub(r"=\d+", "=0", answer))
        assert not re.search(check.pattern, "")

    # The second reads through GRUB's own driver rather than the kernel's: a
    # module the kernel lists and GRUB cannot follow is the failure this
    # exists for.
    assert "grub-fstest" in GRUB_READS_ITS_MODULE.command

    # The third answers with two counts, because a stub that is not there and
    # a stub naming another filesystem are different defects and one number
    # cannot tell them apart.
    healthy = (
        "grubstub=163840 grubprefix=1 boot=aaaa esp=1234-ABCD stub=aaaa"
        " embedded=(hd0,gpt2)/boot/grub drive=hd0,gpt2 fs=xfs\n"
    )
    assert re.search(GRUB_PREFIX_CHECK.pattern, healthy)
    assert re.search(GRUB_PREFIX_CHECK.pattern, healthy.replace("stub=163840", "stub=0")) is None

    # run145's `vm-xfs` installed, rebooted and was read back: this is what
    # its stub answered. `grubprefix=0` and an empty `stub` are what a machine
    # that boots carries, so a check that refuses them refuses a healthy
    # install, which is how `#828` came to block every UEFI run.
    from_a_machine_that_booted = (
        "grubstub=163840 grubprefix=0 boot=d521c22e-04b8-4387-b297-8bb44bbb7ce7"
        " esp=AF55-B837 stub= embedded=(,gpt2)/boot/grub"
        " drive=(hostdisk//dev/vda,gpt2) fs=xfs\n"
    )
    assert re.search(GRUB_PREFIX_CHECK.pattern, from_a_machine_that_booted)
    assert re.search(GRUB_PREFIX_REPORT.pattern, from_a_machine_that_booted)
    # A stub `grub-install` never wrote is still refused, and so is a
    # `grub-probe` that named no filesystem for `/boot`.
    no_stub = from_a_machine_that_booted.replace("grubstub=163840", "grubstub=0")
    no_boot_uuid = from_a_machine_that_booted.replace(
        "boot=d521c22e-04b8-4387-b297-8bb44bbb7ce7", "boot="
    )
    assert re.search(GRUB_PREFIX_CHECK.pattern, no_stub) is None
    assert re.search(GRUB_PREFIX_CHECK.pattern, no_boot_uuid) is None

    # The workstation measurement that started this — `grub-install` writing a
    # stub carrying `/boot`'s uuid — did not transfer: a guest that boots
    # carries none. Both probes stay in the command as evidence for the next
    # failure; neither decides one.
    assert "grub-probe --target=fs_uuid /boot" in GRUB_PREFIX_CHECK.command
    assert "boot=%s esp=%s stub=%s" in GRUB_PREFIX_CHECK.command
    # The values, not only the format: printf keeps the field and prints it
    # empty when the argument goes, so the two probes are named here.
    assert GRUB_PREFIX_CHECK.command.count("grub-probe --target=fs_uuid /boot") == 2
    assert "grub-probe --target=fs_uuid /efi" in GRUB_PREFIX_CHECK.command

    # `vm-convert` answered `stub=` empty while the stub was 163840 bytes: the
    # reader looked for a 36-character uuid alone, and a vfat esp is named by
    # an eight-digit serial. Both forms are read now, so an empty answer means
    # the stub carries no filesystem at all rather than one this could not see.
    import re as _re

    reader = _re.compile(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}|[0-9A-F]{4}-[0-9A-F]{4}")
    assert reader.search("1234-ABCD"), "a vfat serial is a filesystem name too"
    assert reader.search("c866ae3a-68e7-4096-9703-dfa17070c3ed")
    assert reader.pattern in GRUB_PREFIX_CHECK.command, GRUB_PREFIX_CHECK.command

    # `vm-convert` then answered `stub=` empty on a 163840-byte stub, which
    # says the stub names no filesystem and not which directory it will read.
    # The prefix itself is that answer, and it decides nothing: an empty match
    # must not turn a healthy conversion into a failure.
    assert _re.search(GRUB_PREFIX_CHECK.pattern, healthy.replace("(hd0,gpt2)/boot/grub", ""))
    # `grep -ac ""` counts every line, so a `grub-probe` that fails leaves
    # `grubprefix` large and healthy-looking; `boot=` is what catches it.
    assert (
        _re.search(GRUB_PREFIX_CHECK.pattern, healthy.replace("boot=aaaa", "boot=").replace("grubprefix=1", "grubprefix=772"))
        is None
    )
    # Anchored, because grub-2.14 carries its own build paths: an unanchored
    # reader answered `/var/tmp/portage/…/grub-2.14/grub-core/bus/pci.c` from
    # a stub whose prefix is `(hd0,gpt2)/boot/grub`, measured on 2026-08-20.
    assert r"'^\([^)]+\)/[^ ]*|^/[^ ]*grub$'" in GRUB_PREFIX_CHECK.command
    assert "embedded=%s" in GRUB_PREFIX_CHECK.command

    # run138 answered `embedded=(,gpt2)/boot/grub`: a partition with no drive
    # and no filesystem driver, from a `grub-install` that reported success.
    # The two probes it composes that prefix from are asked here, so the next
    # failure names the one that answered nothing instead of leaving it open.
    assert "grub-probe --target=drive /boot" in GRUB_PREFIX_CHECK.command
    assert "grub-probe --target=fs /boot" in GRUB_PREFIX_CHECK.command
    # Their diagnosis, not only their answer: `grub-probe` writes the reason
    # on stderr, and a field of spaces would break the line into two.
    assert GRUB_PREFIX_CHECK.command.count("2>&1 | tr ' ' '_'") == 2
    assert _re.search(GRUB_PREFIX_CHECK.pattern, healthy.replace("drive=hd0,gpt2", "drive="))
    assert _re.search(GRUB_PREFIX_CHECK.pattern, healthy.replace(" fs=xfs", "")) is None


def test_the_unlock_addresses_go_back_after_the_guests_do() -> None:
    """The pool is shared with every other campaign on this machine and its
    lease is what keeps two of them apart. Releasing before `_abandon_jobs`
    hands a live guest's address to the next campaign to ask."""
    import ast
    import inspect
    import textwrap

    from tests.vm import cluster

    body = ast.parse(textwrap.dedent(inspect.getsource(cluster.run))).body[0]
    assert isinstance(body, ast.FunctionDef)
    closing: list[ast.stmt] = []
    for one in ast.walk(body):
        if isinstance(one, ast.Try) and one.finalbody:
            closing = one.finalbody
    abandoned = [
        n
        for n, statement in enumerate(closing)
        if any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_abandon_jobs"
            for call in ast.walk(statement)
        )
    ]
    released = [
        n
        for n, statement in enumerate(closing)
        if any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "release"
            for call in ast.walk(statement)
        )
    ]
    assert abandoned and released, ast.dump(ast.Module(body=closing, type_ignores=[]))
    assert abandoned[0] < released[0], [abandoned, released]


def test_each_schedule_rewrites_its_fixtures_into_its_own_directory(tmp_path: Path) -> None:
    """Every schedule on this machine shares one work directory, and the
    verdict re-reads the configuration file after the guest is installed.
    `static-ip` was installed with `10.31.0.172/24` from its own reservation,
    a later schedule rewrote the same path with `10.31.0.186/24`, and the
    check then failed the machine for not holding an address nothing had ever
    asked it for."""
    from tests.vm import cluster

    first = cluster._fixture_dir(tmp_path)
    second = cluster._fixture_dir(tmp_path)
    assert first != second, first
    assert first.parent == tmp_path and second.parent == tmp_path

    # And the writer keeps them apart: the same fixture name rewritten by two
    # schedules leaves two files, each holding what its own guest was given.
    for where, address in ((first, "10.31.0.172/24"), (second, "10.31.0.186/24")):
        where.mkdir()
        (where / "static-ip.toml").write_text(f'addresses = ["{address}"]\n')
    assert (first / "static-ip.toml").read_text() != (second / "static-ip.toml").read_text()

    # The scheduler asks for one rather than naming a fixed path.
    import inspect

    source = inspect.getsource(cluster.run)
    assert "_fixture_dir(workdir)" in source, source[:200]
    assert 'workdir / "fixtures"' not in source, source[:200]



def test_a_machine_that_booted_is_asked_what_its_stub_holds() -> None:
    """`#828` refused every UEFI install by assuming what a bootable stub
    carries. Nothing had measured it: the prefix checks run only for a
    conversion. This one reports and judges nothing, so the answer arrives
    without a rule written ahead of it."""
    import re

    from gentoo_install.exec.config import load
    from tests.vm.convert import GRUB_PREFIX_CHECK, GRUB_PREFIX_REPORT, after_the_boot

    uefi = load(Path("tests/fixtures/vm-xfs.toml"))
    assert after_the_boot(uefi) == (GRUB_PREFIX_REPORT,)
    # BIOS asks none of it: the stub and the module directory are UEFI's.
    assert after_the_boot(load(Path("tests/fixtures/ext4-bios.toml"))) == ()

    # The same command as the conversion, so the two answers are comparable.
    assert GRUB_PREFIX_REPORT.command == GRUB_PREFIX_CHECK.command

    # Every field, no value: the failing conversion's own answer passes here,
    # which is the point — it is a measurement and not a refusal.
    failed = (
        "grubstub=163840 grubprefix=0 boot=642038bc esp=99F1-0F9A stub= "
        "embedded=(,gpt2)/boot/grub drive=(hostdisk//dev/vda,gpt2) fs=xfs\n"
    )
    assert re.search(GRUB_PREFIX_REPORT.pattern, failed)
    # And so does the conversion's own check, because run145 measured a
    # machine that booted answering the same six values: only the two uuids,
    # which name that machine's own filesystems, differ.
    assert re.search(GRUB_PREFIX_CHECK.pattern, failed)
    fields = dict(pair.split("=", 1) for pair in failed.split())
    booted_fields = dict(
        pair.split("=", 1)
        for pair in (
            "grubstub=163840 grubprefix=0 boot=d521c22e-04b8-4387-b297-8bb44bbb7ce7"
            " esp=AF55-B837 stub= embedded=(,gpt2)/boot/grub"
            " drive=(hostdisk//dev/vda,gpt2) fs=xfs"
        ).split()
    )
    assert {name for name in fields if fields[name] != booted_fields[name]} == {"boot", "esp"}
    # A line that lost a field is still refused: an absent answer is not one.
    assert re.search(GRUB_PREFIX_REPORT.pattern, failed.replace(" fs=xfs", "")) is None

    # And somebody asks it. A check nothing calls is the shape this repository
    # has shipped before: declared, never read, and read as coverage.
    import ast
    import inspect
    import textwrap

    from tests.vm import cluster as cluster_module

    asked = ast.parse(textwrap.dedent(inspect.getsource(cluster_module.boot_and_check)))
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "after_the_boot"
        for node in ast.walk(asked)
    ), "boot_and_check never asks what the stub holds"


def test_a_local_copy_is_sent_and_the_node_is_never_asked_to_fetch(
    tmp_path: Path,
) -> None:
    """The node reaches the Chinese Gentoo mirrors and nothing else.

    Asked for a GitHub release it starts a download task that never
    progresses, and `fetch_iso` waits an hour: one session sat fifteen minutes
    with no output before its own timeout ended it.
    """
    from tests.vm import cluster

    asked: list[str] = []
    sent: list[Path] = []

    class Placing:
        def isos(self, node: str) -> list[str]:
            return []

        def fetch_iso(self, node: str, url: str, name: str, sha512: str) -> None:
            asked.append(url)

        def send_iso(self, node: str, path: Path, name: str, sha512: str) -> None:
            sent.append(path)

        def stale_drivers(self, node: str, keep: str, older_than: float) -> list[str]:
            return []

        def upload_iso(self, node: str, path: Path, name: str) -> str:
            return name

    copy = tmp_path / "medium.iso"
    copy.write_bytes(b"medium")
    driver = tmp_path / "driver.iso"
    driver.write_bytes(b"driver")
    api = cast(Any, Placing())
    cluster.prepare(
        api, "infra-node6", "medium.iso", ("https://github.example/medium.iso",),
        "ab" * 64, tmp_path / "trust", driver, driver.name, local=copy,
    )
    assert sent == [copy] and asked == []

    # Negative control: with no local copy the URLs are what is used, in order.
    cluster.prepare(
        api, "infra-node6", "medium.iso", ("https://one.example/m", "https://two.example/m"),
        "ab" * 64, tmp_path / "trust", driver, driver.name,
    )
    assert asked == ["https://one.example/m"] and sent == [copy]


def test_a_cached_medium_with_the_wrong_checksum_is_fetched_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated download left behind reads as a medium until it is booted."""
    from tests.vm import cluster

    monkeypatch.setattr(cluster, "MEDIA_CACHE", tmp_path / "media")
    body = b"the whole medium"
    want = hashlib.sha512(body).hexdigest()

    served: list[str] = []

    class Answer(io.BytesIO):
        def __enter__(self) -> Answer:
            return self

        def __exit__(self, *rest: object) -> None:
            return None

    def open_url(url: str, timeout: float = 0.0) -> Answer:
        served.append(url)
        return Answer(body)

    monkeypatch.setattr(urllib.request, "urlopen", open_url)
    stale = tmp_path / "media" / "medium.iso"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"half of it")
    got = cluster.cached_medium("medium.iso", ("https://one.example/m",), want)
    assert got.read_bytes() == body and served == ["https://one.example/m"]

    # Negative control: the same call against the now-correct file downloads
    # nothing, or every session would re-fetch a gigabyte it already has.
    again = cluster.cached_medium("medium.iso", ("https://one.example/m",), want)
    assert again == got and served == ["https://one.example/m"]


class ScriptedShell:
    """A `Reconnecting` double that echoes the command it was given.

    A double that answers only the output hides the defect this test exists
    to catch: `expect_output` reads between two markers precisely because the
    shell repeats the line, and a check written against a silent fake passes
    on a machine that answered nothing.
    """

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def expect_output(self, command: str, timeout: float = 120.0) -> bytes:
        self.asked.append(command)
        # The longest match, not the first: every command mentioning
        # `grub.cfg` matched the key for reading the file itself, so the check
        # for a terminal line was answered with a path and read as `already
        # set`. A loose double hides the branch it is meant to exercise.
        matched = sorted(
            (one for one in self.answers if one in command), key=len, reverse=True
        )
        if matched:
            said = self.answers[matched[0]]
            return f"{command}\n{said}".encode().split(b"\n", 1)[1]
        return b""

    def run(self, command: str, timeout: float = 120.0, *, repeatable: bool = True) -> None:
        self.asked.append(command)


def test_a_zfs_root_is_told_to_speak_through_the_pool() -> None:
    """`gi-s7a` and `gi-s7b` both ran ZFSBootMenu, which is never on screen for
    a menu editor to hold, so the parameters go on the pool it reads."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {"zpool import": "rpool", "zfs list": "rpool/ROOT\nrpool/ROOT/gentoo"}
    )
    route = cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    assert route is cluster.SerialRoute.ZFSBOOTMENU
    assert "zpool import -N -f rpool" in shell.asked
    # On the boot environment, not on `rpool/ROOT`: the installer writes an
    # empty value on the environment itself, and `zfs get -o source` reads
    # that back as `local`, so nothing is inherited from the parent.
    assert [one for one in shell.asked if one.startswith("zfs set")] == [
        "zfs set org.zfsbootmenu:commandline='console=ttyS0,115200' rpool/ROOT/gentoo"
    ], shell.asked
    assert "zpool export rpool" in shell.asked
    # No esp is looked for once the pool answered: mounting one would be a
    # second bootloader's route taken on a machine that has the first.
    assert not [one for one in shell.asked if "blkid" in one]


def test_systemd_boot_is_told_to_speak_through_its_loader_entry() -> None:
    """`gi-s5a` printed systemd-boot's own `Boot in 3s.` and nothing after it;
    the kernel command line lives in the entry file on the esp."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {
            "zpool import": "",
            "blkid": "/dev/vda1",
            "loader/entries": "/mnt/esp/loader/entries/abc-7.1.9.conf",
        }
    )
    route = cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    assert route is cluster.SerialRoute.LOADER_ENTRIES
    assert "mkdir -p /mnt/esp && mount /dev/vda1 /mnt/esp" in shell.asked
    assert [one for one in shell.asked if one.startswith("sed -i '/^options/")] == [
        "sed -i '/^options/ s|$| console=ttyS0,115200|' /mnt/esp/loader/entries/*.conf"
    ]
    assert shell.asked[-1] == "umount /mnt/esp"


def test_a_machine_with_nothing_to_edit_is_left_to_the_menu() -> None:
    """An esp with no loader entries and no partition carrying a `grub.cfg`
    leaves the menu editor as the only way in. The esp is unmounted rather
    than left over the next mount."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {
            "zpool import": "",
            "blkid -t TYPE=vfat": "/dev/vda1",
            "loader/entries": "",
            "blkid -o export": "",
        }
    )
    route = cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    assert route is cluster.SerialRoute.NOTHING_FOUND
    assert not [one for one in shell.asked if one.startswith("sed -i")]
    assert "umount /mnt/esp" in shell.asked
    # Nothing was mounted looking for a config, because nothing was named.
    assert not [one for one in shell.asked if "/mnt/root" in one]


def test_a_bios_grub_machine_is_told_to_speak_through_its_config() -> None:
    """A BIOS GRUB draws its menu on the VGA console, so the menu editor has
    nothing to hold: `gi-s2a` answered zero bytes where `gi-t1b` answered a
    whole boot. The kernel still writes wherever `console=` sends it."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {
            "zpool import": "",
            "blkid -t TYPE=vfat": "",
            "blkid -o export": "/dev/vda1:ext4",
            "grub/grub.cfg": "/mnt/root/boot/grub/grub.cfg",
        }
    )
    route = cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    assert route is cluster.SerialRoute.GRUB_CONFIG
    assert [one for one in shell.asked if one.startswith("sed -i '/^[[:space:]]*linux/")] == [
        "sed -i '/^[[:space:]]*linux/ s|$| console=ttyS0,115200|' /mnt/root/boot/grub/grub.cfg"
    ]
    assert "umount /mnt/root" in shell.asked


def test_a_luks_root_is_unlocked_before_its_config_is_read() -> None:
    """Spec 2's root is a LUKS container, and nothing under it can be mounted
    until it is opened. Every spec uses one password, so the harness knows it
    without being told."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {
            "zpool import": "",
            "blkid -t TYPE=vfat": "",
            "blkid -o export": "/dev/vda1:crypto_LUKS",
            "grub/grub.cfg": "/mnt/root/boot/grub/grub.cfg",
            "GRUB_ENABLE_CRYPTODISK": "0",
        }
    )
    route = cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    assert route is cluster.SerialRoute.GRUB_CONFIG
    assert (
        "printf '%s' testtest | cryptsetup open /dev/vda1 speak" in shell.asked
    ), shell.asked
    assert any("mount /dev/mapper/speak /mnt/root" in one for one in shell.asked), shell.asked
    assert "cryptsetup close speak" in shell.asked


def test_the_menu_editor_is_asked_for_only_where_nothing_else_was_written() -> None:
    """Asked unconditionally it waits 120 seconds twice for a menu that never
    comes: `gi-u7` boots ZFSBootMenu and `gi-u5` draws systemd-boot's own menu,
    and the 90 KB that guest sent were that menu redrawing."""
    from tests.vm import cluster

    held: list[str] = []

    def editor(guest: object, link: object) -> None:
        held.append("asked")

    original = cluster._edit_uefi_cmdline
    cluster._edit_uefi_cmdline = cast(Any, editor)
    try:
        for route, expected in (
            (cluster.SerialRoute.ZFSBOOTMENU, False),
            (cluster.SerialRoute.LOADER_ENTRIES, False),
            (cluster.SerialRoute.GRUB_CONFIG, False),
            (cluster.SerialRoute.NOTHING_FOUND, True),
        ):
            held.clear()
            answered = cluster.edit_the_menu_if_that_is_the_only_route(
                cast(Any, None), cast(Any, None), route
            )
            assert answered is expected, route
            assert bool(held) is expected, route
    finally:
        cluster._edit_uefi_cmdline = original


def test_an_openrc_root_gets_a_getty_on_the_serial_line() -> None:
    """`lab8` printed its whole boot and then nothing: systemd spawns a getty
    on whatever `console=` names, and OpenRC's `/etc/inittab` only starts one
    on tty1, so the check that reads the machine back had nowhere to log in."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {
            "zpool import": "",
            "blkid -t TYPE=vfat": "",
            "blkid -o export": "/dev/vda1:ext4",
            "grub/grub.cfg": "/mnt/root/boot/grub/grub.cfg",
            "grep -c '^[^#]*ttyS0'": "0",
        }
    )
    route = cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    assert route is cluster.SerialRoute.GRUB_CONFIG
    written = [one for one in shell.asked if "inittab" in one and "printf" in one]
    assert written, shell.asked
    assert cluster.SERIAL_GETTY in written[0]
    # Written before the root is unmounted, or it lands on the live medium.
    assert shell.asked.index(written[0]) < shell.asked.index("umount /mnt/root")


def test_a_root_that_already_has_a_serial_getty_is_left_alone() -> None:
    """A second line would give the machine two agettys on one device, and
    they take turns losing the terminal."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {
            "zpool import": "",
            "blkid -t TYPE=vfat": "",
            "blkid -o export": "/dev/vda1:ext4",
            "grub/grub.cfg": "/mnt/root/boot/grub/grub.cfg",
            "grep -c '^[^#]*ttyS0'": "1",
        }
    )
    cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    assert not [one for one in shell.asked if "inittab" in one and "printf" in one]


def test_grub_is_moved_onto_the_serial_line_as_well() -> None:
    """`GRUB_ENABLE_CRYPTODISK=y` makes GRUB read the passphrase itself, before
    the kernel's `console=` can matter, and a BIOS GRUB draws that prompt on
    the VGA console. `gi-w2` sent 49 bytes and stopped there."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {
            "zpool import": "",
            "blkid -t TYPE=vfat": "",
            "blkid -o export": "/dev/vda1:crypto_LUKS",
            "grub/grub.cfg": "/mnt/root/boot/grub/grub.cfg",
            "grep -c '^terminal_output.*serial'": "0",
            "grep -c '^[^#]*ttyS0'": "0",
        }
    )
    cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    written = [one for one in shell.asked if one.startswith("sed -i '1i")]
    assert written, shell.asked
    for line in cluster.GRUB_SERIAL_LINES:
        assert line in written[0], line
    # Before the kernel line is what matters: GRUB acts on a command where it
    # reads it, and the passphrase prompt is the first thing the file leads to.
    assert written[0].count("1i") == 1


def test_a_grub_config_already_on_serial_is_left_alone() -> None:
    """The installer writes these itself when `kernel_params` names a serial
    console, and a second `terminal_output` would override the first."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {
            "zpool import": "",
            "blkid -t TYPE=vfat": "",
            "blkid -o export": "/dev/vda1:ext4",
            "grub/grub.cfg": "/mnt/root/boot/grub/grub.cfg",
            "grep -c '^terminal_output.*serial'": "1",
            "grep -c '^[^#]*ttyS0'": "1",
        }
    )
    cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    assert not [one for one in shell.asked if one.startswith("sed -i '1i")]


def test_a_gfxterm_only_config_is_still_moved_onto_the_serial_line() -> None:
    """`00_header` writes `terminal_output gfxterm` on every machine that has
    the graphics modules, so a check for the command alone answered 1 on
    `gi-w2`, skipped the edit, and left the guest at an invisible passphrase
    prompt for a second round. The check has to name `serial`."""
    from tests.vm import cluster

    def grub_cfg(command: str) -> str:
        # What the machine computes, not what the test wants: the count comes
        # from a real `grep` over a file that has gfxterm and nothing else.
        lines = ["terminal_output gfxterm", "menuentry 'Gentoo' {", "  linux /boot/vmlinuz"]
        pattern = command.split("'")[1]
        assert pattern == "^terminal_output.*serial", pattern
        import re as _re

        return str(sum(1 for one in lines if _re.search(pattern, one)))

    asked: list[str] = []

    class Shell(ScriptedShell):
        def expect_output(self, command: str, timeout: float = 120.0) -> bytes:
            if "terminal_output" in command:
                asked.append(command)
                return grub_cfg(command).encode()
            return super().expect_output(command, timeout)

    shell = Shell(
        {
            "zpool import": "",
            "blkid -t TYPE=vfat": "",
            "blkid -o export": "/dev/vda1:ext4",
            "grub/grub.cfg": "/mnt/root/boot/grub/grub.cfg",
            "grep -c '^[^#]*ttyS0'": "1",
        }
    )
    cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    assert asked, "the terminal was never checked"
    assert [one for one in shell.asked if one.startswith("sed -i '1i")], shell.asked


def test_a_grep_that_counts_none_is_read_as_none() -> None:
    """`grep -c` exits 1 when it counts nothing, so `grep -c … || echo 0` ran
    both halves and the console answered two lines. Compared against `"0"`
    that read as a match, and `gi-w2` lost two rounds to an edit that was
    skipped every time."""
    from tests.vm import cluster

    class TwoZeroes:
        """What the shell really answered: the count and the fallback."""

        def __init__(self) -> None:
            self.asked: list[str] = []

        def expect_output(self, command: str, timeout: float = 120.0) -> bytes:
            self.asked.append(command)
            # A pipeline takes grep's exit status out of play, so one line.
            return b"0\n" if "| head -1" in command else b"0\n0\n"

        def run(self, command: str, timeout: float = 120.0, *, repeatable: bool = True) -> None:
            self.asked.append(command)

    shell = TwoZeroes()
    assert cluster._counted(cast(Any, shell), "ttyS0", "/mnt/root/etc/inittab") == 0
    assert "| head -1" in shell.asked[0], shell.asked


def test_a_bios_cryptodisk_root_says_the_prompt_comes_first() -> None:
    """`GRUB_ENABLE_CRYPTODISK=y` with `/boot` inside the encrypted root means
    the core image asks for the passphrase before it can read `grub.cfg` at
    all: `gi-w2` has the serial lines on lines 1 to 3 and `cryptomount` on
    line 92, and it stayed quiet through four rounds. Everything is still
    written, and the caller is told why the console says nothing."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {
            "zpool import": "",
            "blkid -t TYPE=vfat": "",
            "blkid -o export": "/dev/vdb1:crypto_LUKS",
            "grub/grub.cfg": "/mnt/root/boot/grub/grub.cfg",
            "grep -c '^terminal_output.*serial'": "0",
            "grep -c '^[^#]*ttyS0'": "0",
            "GRUB_ENABLE_CRYPTODISK": "1",
        }
    )
    route = cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    assert route is cluster.SerialRoute.CRYPTODISK_CORE
    # The edits still happen: they work once the passphrase is answered.
    assert [one for one in shell.asked if one.startswith("sed -i '1i")], shell.asked
    assert [one for one in shell.asked if "inittab" in one and "printf" in one], shell.asked
    # Asked while the root is mounted, or the answer cannot be read at all.
    crypt = next(one for one in shell.asked if "GRUB_ENABLE_CRYPTODISK" in one)
    assert shell.asked.index(crypt) < shell.asked.index("umount /mnt/root")


def test_a_reopened_console_clears_whatever_the_shell_was_holding() -> None:
    """A drop in the middle of a `send` leaves the shell holding half a line,
    and an unclosed quote turns every command after it into continuation text:
    `gi-x1` answered `> ` to three `zpool import` retries in a row and nothing
    else. The interrupt goes first, before the prompt is solicited, or the
    empty line only lengthens the quote."""
    from tests.vm import cluster
    from tests.vm.console import ConsoleClosed

    sent: list[str] = []

    class Console:
        closed = False

        def send(self, line: str) -> None:
            sent.append(f"send:{line}")

        def send_raw(self, keys: str) -> None:
            sent.append(f"raw:{keys}")

        def close(self) -> None:
            sent.append("close")

    class Dropping(Console):
        closed = False

        def send(self, line: str) -> None:
            sent.append(f"send:{line}")
            raise ConsoleClosed("dropped mid-send")

    made: list[object] = []

    def open_console() -> object:
        one = Dropping() if not made else Console()
        made.append(one)
        return one

    link = cluster.Reconnecting(cast(Any, open_console))
    sent.clear()
    # A write that drops is what leaves the half line, and what the interrupt
    # is for. Reopened after one, the interrupt goes first or the empty line
    # only lengthens the quote.
    try:
        link.send("zpool import")
    except ConsoleClosed:
        pass
    link.reopen()

    assert sent.index(f"raw:{cluster.INTERRUPT}") < sent.index("send:"), sent


def test_no_interrupt_reaches_a_console_that_may_be_at_a_login_prompt() -> None:
    """`solicit_prompt=False` is used where the console may be at `login:`, a
    GRUB menu or a passphrase prompt, and anything sent there is an answer to
    it: `vm-lvm` and `openrc-sdboot` failed three rounds to one empty line at
    a login prompt. The interrupt clears a shell and belongs only where one is
    known to be there."""
    from tests.vm import cluster

    sent: list[str] = []

    class Console:
        def send(self, line: str) -> None:
            sent.append(f"send:{line}")

        def send_raw(self, keys: str) -> None:
            sent.append(f"raw:{keys}")

        def close(self) -> None:
            sent.append("close")

    link = cluster.Reconnecting(lambda: cast(Any, Console()))
    sent.clear()
    link.reopen(solicit_prompt=False)

    assert f"raw:{cluster.INTERRUPT}" not in sent, sent
    assert not [one for one in sent if one.startswith("send:")], sent


def test_a_commented_getty_does_not_count_as_a_login() -> None:
    """Gentoo's `/etc/inittab` ships the serial entries with a `#` in front,
    which `EnableSerialGetty` already records. Counting every mention of the
    port, the harness found one and added none: `lab8` reached runlevel 3 with
    every service `[ ok ]` and no way to log in."""
    from tests.vm import cluster

    shell = ScriptedShell(
        {
            "blkid": "/dev/vda1:ext4",
            "ls /mnt/root/boot/grub/grub.cfg": "/mnt/root/boot/grub/grub.cfg",
            # What the count answers on a stock inittab: the line is there and
            # it is commented out.
            "^[^#]*ttyS0": "0",
            "^GRUB_ENABLE_CRYPTODISK=y": "0",
            "^terminal_output.*serial": "0",
            "^serial --unit": "0",
            "^terminal_input.*serial": "0",
        }
    )
    cluster.make_the_installed_system_speak(cast(Any, shell), "console=ttyS0,115200")

    written = [one for one in shell.asked if "inittab" in one and "printf" in one]
    assert written, shell.asked

    # The control for this rule is
    # `test_a_root_that_already_has_a_serial_getty_is_left_alone`, which feeds
    # the same check an uncommented entry and asserts nothing is appended.


def test_the_probe_records_what_the_lookups_were_asked_with() -> None:
    """A converted guest answered `fail` to five lookups while it held a route
    to the gateway and to `223.5.5.5`, and neither the resolver list nor the
    order glibc consults was anywhere in the log. `nsswitch.conf` matters as
    much as `resolv.conf`: a `hosts:` line that returns before `dns` makes a
    correct resolver file irrelevant, and three rounds were spent guessing
    which of the two it was."""
    probe = cluster.REACHABILITY_PROBE
    assert "/etc/resolv.conf" in probe, probe
    assert "/etc/nsswitch.conf" in probe, probe
    # Read, not only named: a probe that mentions the file without printing it
    # leaves the reader exactly where they were.
    assert "RESOLVCONF " in probe and "NSSWITCH " in probe, probe
    # Absent is an answer too. A guest with no resolver file at all is the
    # case the lookups cannot distinguish from a resolver that says nothing.
    assert probe.count("printf 'absent'") >= 2, probe


def test_a_campaign_names_the_guests_an_earlier_one_left_behind() -> None:
    """Eight stopped guests held 202 GiB of a 234 GiB pool on 2026-08-24,
    left by campaigns whose process was killed before their cleanup ran. The
    next campaign has to say so: nothing else in the run's output distinguishes
    a pool that is nearly full from one that is filling up."""

    class Listing:
        def __init__(self, answer: object) -> None:
            self.answer = answer

        def call(self, method: str, path: str, **form: object) -> object:
            assert method == "GET" and path.startswith("/cluster/resources")
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    left = [
        {"vmid": 9301, "node": "infra-node3", "name": "gi-s1", "status": "stopped",
         "maxdisk": 40 * 2**30},
        {"vmid": 9306, "node": "infra-node6", "name": "gi-s8", "status": "stopped",
         "maxdisk": 40 * 2**30},
    ]
    running = {"vmid": 9303, "node": "infra-node1", "name": "gi-static-ip",
               "status": "running", "maxdisk": 40 * 2**30}
    # Deliberate infrastructure, and stopped: a leftover sweep that names it
    # sends somebody to delete the thing the resolvers fall back to.
    resolver = {"vmid": 9390, "node": "infra-node4", "name": "gi-resolver",
                "status": "stopped", "maxdisk": 2 * 2**30}
    others = {"vmid": 120, "node": "infra-node2", "name": "somebody-elses",
              "status": "stopped", "maxdisk": 100 * 2**30}

    said = cluster.orphan_report(cast(Any, Listing([*left, running, resolver, others])))
    assert len(said) == 1, said
    assert "2 stopped guest" in said[0] and "80 GiB" in said[0], said[0]
    assert "9301 on infra-node3" in said[0] and "9306 on infra-node6" in said[0], said[0]
    for absent in ("gi-static-ip", "9303", "9390", "120"):
        assert absent not in said[0], (absent, said[0])

    assert cluster.orphan_report(cast(Any, Listing([running, resolver]))) == []
    assert cluster.orphan_report(cast(Any, Listing(ProxmoxError("no answer")))) == []


def test_a_transient_failure_while_tidying_does_not_end_the_campaign(
    tmp_path: Path,
) -> None:
    """A campaign on 2026-08-24 ended with no verdicts at all: an SSL handshake
    failed inside `stale_drivers`, which only removes old driver CDs, and the
    error came out of the scheduler. Six guests had been built and deleted and
    nothing was said about any of them."""
    from tests.vm.cluster import place_driver

    class Tidying:
        def __init__(self) -> None:
            self.placed: list[str] = []

        def stale_drivers(self, node: str, keep: str, older_than: float) -> list[str]:
            raise ProxmoxTransientError("GET /nodes/n/storage/local/content did not answer")

        def isos(self, node: str) -> list[str]:
            return ["driver.iso"]

        def remove_iso(self, node: str, name: str) -> str:
            return ""

        def upload_iso(self, node: str, path: Path, name: str) -> str:
            self.placed.append(name)
            return ""

    api = Tidying()
    trust = tmp_path / "trust"
    driver = tmp_path / "driver.iso"
    driver.write_bytes(b"cd")

    # The point is that this returns rather than raising.
    place_driver(cast(Any, api), "infra-node2", trust, driver, "driver.iso")


def test_both_runners_read_one_git_state() -> None:
    """`driver.revision()` and `cluster.revision_identity()` each ran their own
    `git status`, and fixing the untracked-file count in one left the other
    reporting `dirty=1` for a screenshot."""
    import ast
    import inspect
    from types import ModuleType

    def asks_git_status(module: ModuleType) -> int:
        """`git status` argv lists, not every `"status"` in the file: the
        orphan report reads a `status` key out of the API's answer, and a
        check that counted that would fail for a reason it does not mean."""
        tree = ast.parse(inspect.getsource(module))
        found = 0
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            words = [
                one.value
                for one in node.elts
                if isinstance(one, ast.Constant) and isinstance(one.value, str)
            ]
            if words[:2] == ["git", "status"]:
                found += 1
        return found

    # One derivation, in the module that owns it.
    assert asks_git_status(driver) == 1, "driver.py no longer asks git once"
    assert asks_git_status(cluster) == 0, "cluster.py asks git for itself again"
    assert "git_state" in inspect.getsource(cluster.revision_identity)


def test_the_encrypted_boot_verdict_says_how_long_and_what_was_on_the_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`vm-zfs-encrypted` was failed after 66 minutes with `the encrypted disk
    asked for nothing and booted nowhere: never matched '<the pattern>'` and
    nothing else: the verdict was cut to 200 bytes from the front, and
    `ConsoleTimeout` leads with the pattern, which is 200 bytes on its own. So
    the one fact it carried is the one already in the source, and whether the
    guest had waited its whole ceiling or died at second twelve could not be
    told from the verdict at all."""
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class Booting:
        """A guest that reaches dracut and then stops, escapes and all."""

        def __init__(self) -> None:
            self._said = False

        def recv(self, size: int) -> bytes:
            clock[0] += 1.0
            if self._said:
                return b""
            self._said = True
            return (
                b"[\x1b[0;32m  OK  \x1b[0m] Reached target \x1b[0;1;39mZFS pool "
                b"import target\x1b[0m\r\n         Starting \x1b[0;1;39mdracut "
                b"pre-mount hook\x1b[0m...\r\n"
            )

        def sendall(self, data: bytes) -> None:
            return None

        def close(self) -> None:
            return None

        @property
        def closed(self) -> bool:
            return False

    serial = SerialConsole(cast(Any, Booting()), BytesIO())

    class Watching:
        def __init__(self) -> None:
            self.console = serial

        def observe(self, pattern: str, timeout: float, *, solicit: bool = False) -> bytes:
            return serial.expect(pattern, timeout)

        def respond(self, line: str) -> None:
            return None

    from gentoo_install.exec.config import load

    installation = load(FIXTURES / "vm-zfs-encrypted.toml")
    result = cluster._unlock(cast(Any, _Keyboard()), cast(Any, Watching()), installation)

    assert result.refused, result
    # The screen and the wait, neither of which the reader can get from the
    # source. `dracut pre-mount` appears in no pattern the wait was built from.
    assert "dracut pre-mount hook" in result.refused, result.refused
    assert f"{cluster.BOOT_PATIENCE:.0f}s" in result.refused, result.refused
    # And not the pattern, which is what the truncation used to keep.
    assert "Please enter passphrase" not in result.refused, result.refused


def test_a_zfs_root_is_a_heavy_guest_whatever_its_kernel_says() -> None:
    """`vm-zfs-encrypted` stalled twice mid-compile on the two cores a light
    guest is given, and its own `install.jsonl` says why: nineteen packages
    compiled, among them `sys-fs/zfs`, `sys-boot/zfsbootmenu`, its `fzf`,
    `kexec-tools` and `mbuffer`, and `sys-apps/systemd`. A binary kernel does
    not spare a ZFS layout, because the module is built against whatever
    kernel was installed.

    The three assertions before the verdict are the point: every other test
    for a heavy guest says these are light, so a fixture that later gains a
    desktop stops covering this case and says so.
    """
    from gentoo_install.exec.config import load
    from tests.vm.sizing import compiles

    for name in ("vm-zfs", "vm-zfs-encrypted", "vm-zfs-mirror", "vm-raidz"):
        config = load(FIXTURES / f"{name}.toml")
        assert config.kernel.source.value.endswith("-bin"), name
        assert not config.packages.desktop, name
        assert config.portage.binhost.official, name
        assert compiles(config), name

    # And not everything: a plain binary-package install stays light, or the
    # rule buys nothing and the cluster runs one guest at a time.
    assert not compiles(load(FIXTURES / "vm-xfs.toml"))


def test_a_watchdog_verdict_tells_an_unanswered_state_from_a_running_one(
    tmp_path: Path,
) -> None:
    """`vm-raidz` stalled 1200s with `cpu 0.00` on a node at 0% and the verdict
    carried no word about qemu at all. Only a non-empty answer was reported, so
    a guest qemu had paused on an `io-error` and a hypervisor that did not
    answer produced the same silence — and the two want opposite next steps."""

    def flat() -> Traffic:
        return Traffic(0, 0, 0.0)

    def verdict(state: str) -> str:
        watch = cluster.Watchdog(
            tmp_path / "install.log",
            flat,
            where="infra-node1",
            load=lambda: 0.0,
            state=lambda: state,
        )
        said = None
        for _ in range(cluster.WATCH_STRIKES + 2):
            said = watch.idle_reason()
        assert said is not None, state
        return said

    assert "qemu calls the guest io-error" in verdict("io-error")
    assert "the hypervisor would not say" in verdict("")
    # And a guest that is genuinely running says nothing extra: a clause on
    # every verdict is a clause nobody reads.
    assert "qemu calls the guest" not in verdict("running")


def test_the_cluster_limit_counts_weight_rather_than_guests() -> None:
    """Four guests at `--limit 4` meant four compiling ones, and `vm-raidz`
    and `vm-cjk-kernel` both stalled mid-`emerge` with their nodes at 0%. The
    same two finished at a limit of two: 64.5m and 20.0m. Memory and cores are
    reserved per node already; this is the whole-cluster ceiling those cannot
    express, and `campaign.py` has charged its own machine this way all along.
    """
    from tests.vm.campaign import COMPILING_WEIGHT as LOCAL_WEIGHT
    from tests.vm.cluster import COMPILING_WEIGHT, fixtures

    heavy, light = fixtures(["vm-raidz", "vm-binpkg"])
    assert heavy.heavy and not light.heavy, (heavy.name, light.name)
    assert (heavy.weight, light.weight) == (COMPILING_WEIGHT, 1)
    # One number, both runners: a second table is how the two came to
    # disagree about nine fixtures in the first place.
    assert COMPILING_WEIGHT == LOCAL_WEIGHT

    # And the schedule really spends it that way, read off the same
    # arithmetic the dispatch loop uses.
    from tests.vm.cluster import slots_within
    from tests.vm.proxmox import Node

    free = [Node(name=f"infra-node{n}", free_bytes=0, cores=4, free_cores=4.0) for n in range(6)]
    # Four units is four binary-package guests or two compiling ones, and the
    # budget is spent over the jobs about to start as well as the running
    # ones: charging only the running ones sent four compiling guests out on
    # the first pass, which is eight units against a ceiling of four.
    assert len(slots_within(4, list(free), [], [light] * 6)) == 4
    assert len(slots_within(4, list(free), [], [heavy] * 6)) == 2
    # One compiling guest already out there leaves one more, not three.
    assert len(slots_within(4, list(free), [heavy], [heavy] * 6)) == 1
    assert len(slots_within(4, list(free), [light], [light] * 6)) == 3
    assert slots_within(4, list(free), [heavy, heavy], [heavy] * 6) == []
    # Zero is "ask the cluster what fits", not "nothing fits".
    assert len(slots_within(0, list(free), [heavy, heavy], [heavy] * 6)) == len(free)


def test_the_run_ceiling_says_so_rather_than_reporting_its_last_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`btrfs-luks` was ended at 481.4 minutes with C++ still compiling on its
    console and the verdict read `never matched 'MARK_26_DONE', 162s of 162s
    elapsed`. `expect` reports the window it was handed, and the last window
    before an eight-hour deadline is a few seconds, so a guest that ran out of
    budget while working reads exactly like a step that hung."""
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class Printing:
        """A guest that keeps printing and never finishes."""

        def recv(self, size: int) -> bytes:
            clock[0] += 1.0
            return b"compiling something\n"

        def sendall(self, data: bytes) -> None:
            return None

        def close(self) -> None:
            return None

        @property
        def closed(self) -> bool:
            return False

    serial = SerialConsole(cast(Any, Printing()), BytesIO())
    link = cluster.Reconnecting(lambda: serial)
    with pytest.raises(ConsoleTimeout) as ended:
        link.wait_for("install", timeout=30.0, idle=20.0)

    said = str(ended.value)
    assert "ceiling ended it with the console still printing" in said, said
    # And what it was printing, so the reader can tell working from wedged.
    assert "compiling something" in said, said


def test_a_key_already_loaded_is_not_the_initramfs_giving_up() -> None:
    """`zbm-unlock` was failed at 142.6 minutes with `Key load error: Key
    already loaded for 'zpcala'`.

    ZFSBootMenu says that when the key is in place, which is exactly what a
    successful remote SSH unlock leaves behind — so the fixture whose subject
    is the remote unlock was failed for performing it. The wrong-key message
    the codebase already quotes elsewhere is `Key load error: Incorrect key
    provided`, and that one still counts.
    """
    already = b"Enter passphrase for 'zpcala':\r\nKey load error: Key already loaded for 'zpcala'"
    assert not cluster.initramfs_gave_up(already), already

    wrong = b"Key load error: Incorrect key provided for 'zpcala'"
    assert cluster.initramfs_gave_up(wrong), wrong
    for other in (b"Entering emergency mode", b"Failed to mount /sysroot"):
        assert cluster.initramfs_gave_up(other), other
    assert not cluster.initramfs_gave_up(b"[    3.1] mounting the root")


def test_the_resolver_setup_reaches_nsswitch_as_well_as_the_file() -> None:
    """Writing `/etc/resolv.conf` is not enough on an installed systemd guest.

    Measured on the converted guest: the file held all three servers, two of
    them answered pings, and `getent hosts distfiles.gentoo.org` still
    answered 2. `systemd-resolved` was active again -- stopping a
    socket-activated daemon does not keep it stopped -- and the guest's
    `hosts: mymachines resolve [!UNAVAIL=return] files myhostname dns` returns
    whatever `resolve` says, so the `dns` module that reads the file was never
    reached. Rewriting the line to `files dns` made the same lookup answer 0.

    The whole install after it went by name: the conversion stopped at
    operation 26 with `<urlopen error [Errno -2] Name or service not known>`.
    """
    from tests.vm.cluster import GUEST_RESOLVERS, use_our_resolvers

    command = use_our_resolvers()
    for one in GUEST_RESOLVERS:
        assert f"nameserver {one}" in command, command
    assert "stop systemd-resolved" in command, command
    # The half that was missing, and the reason it is not enough to stop the
    # daemon: an active `resolve` module answers before the file is read.
    assert "nsswitch.conf" in command, command
    assert "hosts: files dns" in command, command
    # After the file, not before: the sed and the printf both touch what the
    # next lookup reads, and a resolver written after the switch was rewritten
    # would still be the one in force.
    assert command.index("resolv.conf") < command.index("nsswitch.conf"), command
