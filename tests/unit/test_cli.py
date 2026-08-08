from __future__ import annotations

import os
import shutil
from typing import Sequence, cast
from pathlib import Path

import pytest

from gentoo_install import cli
from gentoo_install.exec import fetch
from gentoo_install.cli import EXIT_CONFIG, EXIT_OK, EXIT_PREFLIGHT, main
from gentoo_install.errors import ConfigError
from gentoo_install.model.parse import load

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


def test_a_terminal_too_small_for_the_interface_says_so_rather_than_drawing() -> None:
    """`too_small` was written and never called, so a 60x20 console got a menu
    with rows off the edge and no message saying why."""
    from gentoo_install.tui.curses_screen import too_small
    from gentoo_install.tui.widgets import MINIMUM_COLUMNS, MINIMUM_LINES

    class Sized:
        def __init__(self, lines: int, columns: int) -> None:
            self.lines, self.columns = lines, columns

        def size(self) -> tuple[int, int]:
            return self.lines, self.columns

    cramped = too_small(Sized(20, 60))  # type: ignore[arg-type]
    assert "60x20" in cramped and f"{MINIMUM_COLUMNS}x{MINIMUM_LINES}" in cramped
    assert too_small(Sized(MINIMUM_LINES, MINIMUM_COLUMNS)) == ""  # type: ignore[arg-type]
    assert "too_small(display)" in Path("gentoo_install/cli.py").read_text()


def test_the_menu_names_openssl_before_it_asks_anything(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fetch.password_hash` shells out to `openssl passwd -6`, and finding it
    absent at the root-password screen throws away every answer before it."""
    from gentoo_install.exec import preflight

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    online(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "openssl" else "/bin/x")
    assert main([]) == EXIT_PREFLIGHT
    assert "the menu needs openssl" in capsys.readouterr().err
    # A file carries its hashes, so an install from one needs none of it.
    from gentoo_install.model.parse import load

    assert "openssl" not in preflight.required_commands(load(FIXTURES / "ext4-bios.toml"))


def _record_commands(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Every argv the runner is handed, with nothing run."""
    from gentoo_install.exec.runner import Result, Runner

    seen: list[tuple[str, ...]] = []

    def remember(self: Runner, argv: Sequence[str], **rest: object) -> Result:
        seen.append(tuple(argv))
        return Result(argv=tuple(argv), returncode=0, stdout="", stderr="", seconds=0.0)

    monkeypatch.setattr(Runner, "run", remember)
    return seen


def test_a_clock_a_year_out_is_corrected_before_the_network_is_blamed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """TLS refuses a certificate that is not yet valid, so an unset RTC failed
    every HTTPS request and the message sent the operator to debug a working
    network."""
    import time

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    online(monkeypatch)
    monkeypatch.setattr(fetch, "network_time", lambda: time.time() + 400 * 86400)
    ran = _record_commands(monkeypatch)
    main([])
    said = capsys.readouterr().err
    assert "clock is more than a day out" in said
    assert any(argv[0] == "hwclock" for argv in ran)


def test_a_clock_that_agrees_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    online(monkeypatch)
    monkeypatch.setattr(fetch, "network_time", lambda: time.time() + 5)
    ran = _record_commands(monkeypatch)
    main([])
    assert not any(argv[0] == "hwclock" for argv in ran)


def test_the_target_is_still_mounted_when_the_shell_is_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offer has to come between the last operation and the unmount: once
    the target is gone the operator would have to mount the layout by hand."""
    import inspect

    source = inspect.getsource(cli.install)
    offered = source.index("_offer_a_shell")
    closing = source.index("apply(closing")
    assert offered < closing
    # And the body runs before either.
    assert source.index("apply(body") < offered


def test_an_unattended_run_is_never_asked_about_a_shell(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-shell` is what the VM harness passes: it drives a serial console,
    where stdin is a terminal and the question would wait forever."""
    import argparse

    from gentoo_install.exec.apply import Machine

    arguments = argparse.Namespace(no_shell=True, target=Path("/mnt/gentoo"))
    said: list[str] = []
    cli._offer_a_shell(arguments, cast(Machine, None), said.append, False)
    assert not said and not capsys.readouterr().out


def test_a_saved_configuration_loads_back_into_the_same_install(tmp_path: Path) -> None:
    """The point of saving is an unattended second run, so the file has to be
    one `--config` accepts, not a record of what was chosen."""
    original = load(FIXTURES / "vm-zfs.toml")
    where = cli._save_config(original, str(tmp_path / "my-install.toml"))
    assert Path(where).read_text().startswith("config_version")
    assert load(Path(where)) == original


def test_a_bare_name_is_saved_where_the_installer_was_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    where = cli._save_config(load(FIXTURES / "vm-zfs.toml"), "my-install.toml")
    assert where == str(tmp_path / "my-install.toml")


def test_a_path_that_cannot_be_written_names_the_path_and_the_reason() -> None:
    with pytest.raises(ConfigError, match="cannot write /proc/nope/my-install.toml"):
        cli._save_config(load(FIXTURES / "vm-zfs.toml"), "/proc/nope/my-install.toml")
