"""The harness has to report a failed install as a failed run.

It reads the installer's exit code off the result disk rather than from the
process it drove, so nothing else in the run can notice a failure for it.
"""

from __future__ import annotations

from pathlib import Path

from tests.vm.run import verdict


def test_a_failed_installer_fails_the_run() -> None:
    assert verdict({"install.rc": b"1\n"}, None) == 1
    assert verdict({"install.rc": b"0\n"}, None) == 0


def test_a_run_that_collected_no_exit_code_is_not_called_a_failure() -> None:
    """A probe run never runs the installer, so there is nothing to fail."""
    assert verdict({}, None) == 0


def test_every_configuration_the_campaign_names_exists() -> None:
    """A stage naming a fixture that was renamed fails half an hour in, after
    the medium has booted, rather than at the first line."""
    from tests.vm.campaign import STAGES

    root = Path(__file__).resolve().parents[1]
    for stage, runs in STAGES.items():
        for run in runs:
            assert (root / run.config).is_file(), f"{stage}: {run.config}"


def test_the_campaign_covers_every_vm_fixture() -> None:
    """A fixture nobody runs is a path nobody tests, and the point of the
    matrix is that the list cannot quietly fall behind."""
    from tests.vm.campaign import STAGES

    root = Path(__file__).resolve().parents[1]
    named = {Path(run.config).name for runs in STAGES.values() for run in runs}
    available = {path.name for path in (root / "fixtures").glob("*.toml")}
    assert available - named == set(), sorted(available - named)
