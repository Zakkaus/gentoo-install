from __future__ import annotations

import os
from pathlib import Path

import pytest

from gentoo_install.cli import EXIT_CONFIG, EXIT_OK, EXIT_PREFLIGHT, main

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_a_dry_run_prints_every_stage_and_exits_clean(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--config", str(FIXTURES / "btrfs-luks.toml"), "--dry-run"])
    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert "[partition]" in printed and "[bootloader]" in printed
    assert "operations:" in printed.splitlines()[-1]


def test_a_dry_run_touches_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """The only proof available at this layer: no operation was applied, and the
    text names devices by id rather than by a path it went looking for."""
    main(["--config", str(FIXTURES / "ext4-bios.toml"), "--dry-run"])
    printed = capsys.readouterr().out
    assert "/dev/" not in printed


def test_a_missing_file_is_a_configuration_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--config", str(FIXTURES / "nothing-here.toml"), "--dry-run"])
    assert code == EXIT_CONFIG
    assert "nothing-here" in capsys.readouterr().err


def test_a_broken_rule_is_a_configuration_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text(
        (FIXTURES / "zfs-zbm.toml").read_text().replace('kind = "zfsbootmenu"', 'kind = "grub"')
    )
    code = main(["--config", str(broken), "--dry-run"])
    assert code == EXIT_CONFIG
    assert "root on ZFS excludes GRUB" in capsys.readouterr().err


def test_an_install_stops_at_preflight_rather_than_touching_a_disk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run as an ordinary user against a machine that has none of the fixture's
    devices: the run has to end in the preflight report, not part way through."""
    code = main(
        [
            "--config", str(FIXTURES / "ext4-bios.toml"),
            "--work", str(tmp_path / "work"),
            "--target", str(tmp_path / "target"),
        ]
    )
    assert code == EXIT_PREFLIGHT
    printed = capsys.readouterr().err
    assert "run as root" in printed or "not present" in printed


def test_no_configuration_without_a_terminal_says_so(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no --config the menu opens, and pytest is not a terminal: that has
    to be an exit code with a sentence, not a curses traceback."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    code = main([])
    said = capsys.readouterr().err
    assert code == EXIT_PREFLIGHT
    assert "pass --config FILE" in said


def test_the_menu_does_not_open_for_an_ordinary_user(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twenty answers thrown away by an EPERM in the middle of the run is the
    failure this replaces: nothing but a dry run works without root."""
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert main([]) == EXIT_PREFLIGHT
    assert "run as root" in capsys.readouterr().err
    assert main(["--dry-run", "--config", str(FIXTURES / "vm-binpkg.toml")]) == 0