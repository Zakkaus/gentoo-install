from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from gentoo_install.model.config import MirrorRegion, Sync
from tests.vm import cluster
from tests.vm.proxmox import Node, VMID_FIRST, VMID_LAST


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

    def nodes(self) -> list[Node]:
        return [Node("node", 64 * 1024**3, 16)]

    def free_vmid(self, held: frozenset[int] = frozenset()) -> int:
        vmid = next(
            candidate
            for candidate in range(VMID_FIRST, VMID_LAST + 1)
            if candidate not in held
        )
        self.allocations.append(vmid)
        return vmid

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
