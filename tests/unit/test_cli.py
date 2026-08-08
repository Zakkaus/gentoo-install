from __future__ import annotations

import os
from pathlib import Path

import pytest

from gentoo_install import cli
from gentoo_install.exec import fetch
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


def online(monkeypatch: pytest.MonkeyPatch, answer: bool = True) -> None:
    """No test opens a connection: the answer is given rather than measured."""
    monkeypatch.setattr(fetch, "online", lambda: answer)


def test_the_menu_stops_when_the_machine_cannot_reach_the_package_site(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kernel versions and the ZFS ceiling are read live so the installer runs
    on a medium with no Gentoo repository; offline there is nothing to offer."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    online(monkeypatch, False)
    assert main([]) == EXIT_PREFLIGHT
    assert "needs a network" in capsys.readouterr().err


def test_the_two_offline_answers_are_still_given_without_a_network(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--missing-commands` names packages and a dry run over a file prints a
    plan; both are what somebody runs on a machine that is not the target."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    online(monkeypatch, False)
    assert main(["--missing-commands"]) == EXIT_OK
    assert main(["--config", str(FIXTURES / "ext4-bios.toml"), "--dry-run"]) == EXIT_OK
    assert "needs a network" not in capsys.readouterr().err


def test_no_configuration_without_a_terminal_says_so(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no --config the menu opens, and pytest is not a terminal: that has
    to be an exit code with a sentence, not a curses traceback."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    online(monkeypatch)
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
    online(monkeypatch)
    assert main([]) == EXIT_PREFLIGHT
    assert "run as root" in capsys.readouterr().err
    assert main(["--dry-run", "--config", str(FIXTURES / "vm-binpkg.toml")]) == 0

def test_an_error_with_no_name_of_its_own_still_becomes_an_exit_code(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """This module is the one place an exception becomes an exit code, and one
    that escaped exited 1, which reads as a bad configuration."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def boom(*_: object, **__: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli, "load", boom)
    code = main(["--config", str(FIXTURES / "vm-binpkg.toml")])
    assert code == cli.EXIT_COMMAND
    assert "No space left" in capsys.readouterr().err


def test_an_unexpected_error_is_named_rather_than_traced(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(cli, "load", boom)
    assert main(["--config", str(FIXTURES / "vm-binpkg.toml")]) == cli.EXIT_COMMAND
    assert "unexpected RuntimeError" in capsys.readouterr().err
