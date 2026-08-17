# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import argparse
import os
import shutil
import time
from typing import Sequence, cast
import re
from dataclasses import replace
from pathlib import Path

import pytest

from gentoo_install import cli
from gentoo_install.exec import fetch
from gentoo_install.exec import report
from gentoo_install.exec.probe import Probe as RealProbe
from gentoo_install.exec.runner import Runner
from gentoo_install.cli import EXIT_CONFIG, EXIT_INTEGRITY, EXIT_OK, EXIT_PREFLIGHT, main
from gentoo_install.errors import ConfigError, IntegrityError
from gentoo_install.model.config import DiskConfig, DiskMode
from gentoo_install.model.device import DeviceGraph, DeviceId, StorageFacts, StorageLayout
from gentoo_install.plan.build import DEFAULT_MIRROR
from gentoo_install.exec.config import load

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

    cramped = too_small(Sized(20, 60))
    assert "60x20" in cramped and f"{MINIMUM_COLUMNS}x{MINIMUM_LINES}" in cramped
    assert too_small(Sized(MINIMUM_LINES, MINIMUM_COLUMNS)) == ""
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
    from gentoo_install.exec.config import load

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


def test_a_published_configuration_reaches_the_pastebin_without_its_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One upload, of a body that still parses and carries no crypt hash."""
    from dataclasses import replace as replaced

    from gentoo_install.model import paste
    from gentoo_install.model.config import User
    from gentoo_install.model.serialise import REDACTED

    sent: list[tuple[str, str]] = []

    def uploaded(body: str, export: paste.Export) -> str:
        sent.append((body, export.extension))
        return "https://paste.gentoozh.org/AbCdEf.toml"

    monkeypatch.setattr(fetch, "upload", uploaded)
    config = load(FIXTURES / "ext4-bios.toml")
    config = replaced(
        config,
        system=replaced(
            config.system,
            root_password_hash="$6$salt$secretsecret",
            users=(User(name="zakk", password_hash="$6$salt$anothersecret"),),
        ),
    )
    assert report.publish_config(config) == "https://paste.gentoozh.org/AbCdEf.toml"
    body, extension = sent[0]
    assert extension == "toml"
    assert "secretsecret" not in body and "anothersecret" not in body
    assert body.count(REDACTED) == 2


def test_the_address_is_printed_even_when_no_code_fits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A console with no room for the code still has to say where the paste
    is, because the address is the part that matters."""
    import shutil as shutil_module

    monkeypatch.setattr(
        shutil_module, "get_terminal_size", lambda default=(80, 24): os.terminal_size((20, 24))
    )
    cli.show_the_address("https://paste.gentoozh.org/AbCdEf.log")
    printed = capsys.readouterr().out
    assert printed.strip() == "https://paste.gentoozh.org/AbCdEf.log"


def test_a_code_is_drawn_beside_the_address_when_it_fits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil as shutil_module

    monkeypatch.setattr(
        shutil_module, "get_terminal_size", lambda default=(80, 24): os.terminal_size((100, 40))
    )
    cli.show_the_address("https://paste.gentoozh.org/AbCdEf.log")
    printed = capsys.readouterr().out.splitlines()
    # The code first, the address last: the address is the line that has to
    # survive a console that scrolled.
    assert printed[-1] == "https://paste.gentoozh.org/AbCdEf.log"
    assert any("\u2588" in line for line in printed)


def test_a_clock_that_cannot_be_set_says_so_rather_than_blaming_the_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """busybox has no `hwclock --set`, and every HTTPS request then fails on a
    not-yet-valid certificate while the next message blames the mirror."""
    from gentoo_install.exec.runner import Result

    monkeypatch.setattr(fetch, "network_time", lambda: time.time() + 10 * 24 * 3600)

    def refused(argv: Sequence[str], **rest: object) -> Result:
        return Result(
            argv=tuple(argv), stdout="hwclock: unrecognized option", stderr="", returncode=1,
            seconds=0.0,
        )

    monkeypatch.setattr(Runner, "run", lambda self, argv, **rest: refused(argv, **rest))
    cli._check_the_clock()
    said = capsys.readouterr().err
    assert "the clock could not be set" in said


def test_an_unattended_run_is_asked_nothing_on_the_way_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The VM harness drives a serial console, where stdin is a terminal, so
    every question sits there for ever; a real run hung on the paste offer for
    twelve minutes before it was killed."""
    import argparse

    asked: list[str] = []

    def question_asked(question: str) -> bool:
        asked.append(question)
        return False

    arguments = argparse.Namespace(no_shell=True, target=tmp_path)
    report.offer_paste(
        tmp_path,
        lambda line: None,
        True,
        cli._unattended(arguments),
        question_asked,
        cli.show_the_address,
    )
    assert asked == []


def test_a_failed_run_only_releases_the_machine(tmp_path: Path) -> None:
    """A run that stopped before the stage3 was unpacked has no target to
    configure, so `chroot ... ln` exited 127 and that error replaced the
    download timeout the operator actually needed to see."""
    from gentoo_install.plan.disk import UnmountTarget
    from gentoo_install.plan.system import LinkResolvConf
    from gentoo_install.model.config import InitSystem

    from .recorder import Recorder

    said: list[str] = []
    recorder = Recorder()
    closing = (LinkResolvConf(init=InitSystem.SYSTEMD), UnmountTarget(pools=()))
    cli._release(closing, recorder, said.append)
    assert any(argv[0] == "umount" for argv in recorder.commands)
    assert not recorder.in_target
    assert said == []


def test_releasing_reports_a_failure_rather_than_raising(tmp_path: Path) -> None:
    """The exception that matters is the one already on its way out."""
    from gentoo_install.plan.disk import UnmountTarget

    from .recorder import Recorder

    said: list[str] = []
    recorder = Recorder(failures={"umount"})
    cli._release((UnmountTarget(pools=()),), recorder, said.append)
    assert said and "warning" in said[0]


def test_only_files_that_could_be_our_configuration_are_offered(tmp_path: Path) -> None:
    """Every `.toml` was offered, so a directory holding a `pyproject.toml`
    answered `the top level has unknown keys: project, tool`.

    The test is whether the file holds a table this configuration has. One of
    ours with a wrong value inside still does, and is offered so its error is
    shown rather than the file being hidden.
    """
    import os

    from gentoo_install.tui.app import SAVE_AS

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n[tool.mypy]\nstrict = true\n')
    (tmp_path / "elsewhere.toml").write_text("[whatever]\nx = 1\n")
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "wrong-value.toml").write_text("config_version = 1\n[system]\nhostname = 5\n")
    # Unreadable, and named the way this installer writes one: the operator
    # hand-edited their own file into a syntax error and needs to be told.
    (tmp_path / SAVE_AS).write_text("[disk\nbroken =\n")
    # Unreadable and not ours, so there is nothing to say about it.
    (tmp_path / "theirs.toml").write_text("[oops\n")

    here = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert report.configs_here(SAVE_AS) == (SAVE_AS, "wrong-value.toml")
    finally:
        os.chdir(here)


def test_persisted_sections_drive_parse_serialise_and_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import fields
    import tomllib

    from gentoo_install.model import config as model_config
    from gentoo_install.model.parse import parse
    from gentoo_install.model.serialise import to_toml

    table_fields = {
        field.name for field in fields(model_config.InstallConfig)
    } - {model_config.CONFIG_VERSION_KEY}
    assert set(model_config.PERSISTED_SECTIONS) == table_fields

    config = load(FIXTURES / "btrfs-luks.toml")
    raw = tomllib.loads(to_toml(config))
    monkeypatch.setattr(
        model_config,
        "PERSISTED_SECTIONS",
        tuple(name for name in model_config.PERSISTED_SECTIONS if name != "system"),
    )

    with pytest.raises(ConfigError, match="system"):
        parse(raw)
    assert "[system]" not in to_toml(config)

    (tmp_path / "system-only.toml").write_text("[system]\nhostname = 'gentoo'\n")
    monkeypatch.chdir(tmp_path)
    assert report.configs_here("my-install.toml") == ()


def test_the_address_handed_over_carries_no_extension() -> None:
    """The extension asks wastebin to highlight the paste. An 8.7 MB install
    log answered 408 after five seconds every time; the same address without
    the extension answered 200, and a small paste highlights fine either way.
    An install log is the large case, so the address a person is handed is the
    one that opens."""
    from gentoo_install.model import paste

    assert paste.page_url("/IOARIoCd1Yo.log") == "https://paste.gentoozh.org/IOARIoCd1Yo"
    assert paste.page_url("IOARIoCd1Yo.toml") == "https://paste.gentoozh.org/IOARIoCd1Yo"
    # Nothing to strip, and no trailing dot left behind.
    assert paste.page_url("/abc") == "https://paste.gentoozh.org/abc"
    # The extension still reaches the server, because it is what the paste is
    # stored with; only the address the person reads drops it.
    assert {one.extension for one in paste.EXPORTS} == {"log", "toml"}


def test_the_log_is_kept_before_the_target_is_unmounted() -> None:
    """`Stage.FINISH` unmounts, so a copy made after it lands on the install
    medium's tmpfs and goes with the reboot.

    Found on a machine this installer had installed: `/var/log/gentoo-install`
    was not there at all, and the copy had reported success.
    """
    import inspect

    from gentoo_install import cli

    source = inspect.getsource(cli.install)
    kept = source.index("report.keep_log(")
    closing = source.index("apply(closing, machine, finished)")
    released = source.index("_release(closing, machine, record)")
    assert kept < closing, "the log is copied after the closing stage unmounts"
    assert kept < released, "the log is copied after the failure path unmounts"


def test_log_preservation_can_run_without_the_cli(tmp_path: Path) -> None:
    """The copy succeeds either way; only the mount says whether the file
    reached the disk or the tmpfs under it."""
    from gentoo_install.exec.report import keep_log

    said: list[str] = []
    (tmp_path / "install.log").write_text("something\n")
    keep_log(tmp_path, tmp_path / "target", said.append)
    assert said and "not mounted" in said[0]
    assert not (tmp_path / "target").exists()


def test_an_exit_that_is_not_a_named_error_still_releases_and_keeps_the_log() -> None:
    """An ENOSPC on the live medium or a Ctrl-C left mounts, arrays and
    imported pools open and the failure log on a tmpfs that goes with the
    reboot, because only `GentooInstallError` was caught."""
    import inspect

    from gentoo_install import cli

    source = inspect.getsource(cli.install)
    assert "except BaseException as error:" in source, "only named errors are caught"
    caught = source.index("except BaseException as error:")
    kept = source.index("report.keep_log(")
    released = source.index("_release(closing, machine, record)")
    raised = source.index("raise failure")
    assert caught < kept < raised, "the log is kept before the exception leaves"
    assert caught < released < raised, "the machine is released before it leaves"


def test_a_release_that_fails_does_not_stop_the_ones_after_it(tmp_path: Path) -> None:
    """The loop caught only `GentooInstallError`, so one release raising
    `OSError` left the containers open, the arrays assembled and the pools
    imported: exactly the state this path exists to undo."""
    from dataclasses import dataclass

    from gentoo_install import cli
    from gentoo_install.plan.operations import Operation, Stage

    ran: list[str] = []

    @dataclass(frozen=True, kw_only=True)
    class Releasing(Operation):
        stage: Stage = Stage.FINISH
        name: str
        raises: BaseException | None = None

        @property
        def releases_the_machine(self) -> bool:
            return True

        def describe(self) -> str:
            return self.name

        def apply(self, context: object) -> None:
            ran.append(self.name)
            if self.raises is not None:
                raise self.raises

    said: list[str] = []
    closing = (
        Releasing(name="close the container", raises=OSError("device busy")),
        Releasing(name="stop the array"),
        Releasing(name="export the pool"),
    )
    from gentoo_install.exec.apply import Machine
    from gentoo_install.exec.probe import Probe
    from gentoo_install.exec.runner import Runner

    from .layouts import config

    runner = Runner(log=lambda line: None)
    machine = Machine(
        config=config(),
        runner=runner,
        probe=Probe(runner=runner, work=tmp_path),
        work=tmp_path,
    )
    cli._release(closing, machine, said.append)
    assert ran == ["close the container", "stop the array", "export the pool"]
    assert any("device busy" in one for one in said), said


def test_a_finish_failure_releases_the_machine_and_keeps_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cleanup operation before the unmount can fail after the target and
    pool exist, so the release operation still has to run on that path."""
    from dataclasses import dataclass

    from gentoo_install.plan.operations import Context, Operation, Stage

    from .layouts import config

    released: list[str] = []

    @dataclass(frozen=True, kw_only=True)
    class Partition(Operation):
        stage: Stage = Stage.PARTITION

        def describe(self) -> str:
            return "begin partitioning"

        def apply(self, context: Context) -> None:
            return

    @dataclass(frozen=True, kw_only=True)
    class FailFinish(Operation):
        stage: Stage = Stage.FINISH

        def describe(self) -> str:
            return "finish the target"

        def apply(self, context: Context) -> None:
            raise IntegrityError("the finish artifact did not verify")

    @dataclass(frozen=True, kw_only=True)
    class FailRelease(Operation):
        stage: Stage = Stage.FINISH

        @property
        def releases_the_machine(self) -> bool:
            return True

        def describe(self) -> str:
            return "close a held resource"

        def apply(self, context: Context) -> None:
            raise OSError("one resource stayed busy")

    @dataclass(frozen=True, kw_only=True)
    class Release(Operation):
        stage: Stage = Stage.FINISH

        @property
        def releases_the_machine(self) -> bool:
            return True

        def describe(self) -> str:
            return "release the machine"

        def apply(self, context: Context) -> None:
            released.append("released")

    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)
    monkeypatch.setattr(cli, "load", lambda path: config())
    monkeypatch.setattr(
        cli, "probe_storage_facts", lambda chosen, probe: StorageFacts()
    )
    monkeypatch.setattr(
        cli,
        "build",
        lambda chosen, catalog, *, mirror, storage_facts, layout: (
            Partition(),
            FailFinish(),
            FailRelease(),
            Release(),
        ),
    )
    monkeypatch.setattr(report, "keep_log", lambda work, target, record: None)

    code = main(
        [
            "--config", str(tmp_path / "install.toml"),
            "--work", str(tmp_path / "work"),
            "--target", str(tmp_path / "target"),
            "--skip-preflight",
            "--no-shell",
        ]
    )

    assert released == ["released"]
    assert code == EXIT_INTEGRITY
    assert "integrity: the finish artifact did not verify" in capsys.readouterr().err


def test_a_failure_after_partitioning_says_the_disk_may_not_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dataclasses import dataclass

    from gentoo_install.plan.operations import Context, Operation, Stage

    from .layouts import config

    @dataclass(frozen=True, kw_only=True)
    class FailPartition(Operation):
        stage: Stage = Stage.PARTITION

        def describe(self) -> str:
            return "write the partition table"

        def apply(self, context: Context) -> None:
            raise IntegrityError("partitioning stopped")

    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)
    monkeypatch.setattr(cli, "load", lambda path: config())
    monkeypatch.setattr(
        cli, "probe_storage_facts", lambda chosen, probe: StorageFacts()
    )
    monkeypatch.setattr(
        cli,
        "build",
        lambda chosen, catalog, *, mirror, storage_facts, layout: (FailPartition(),),
    )
    monkeypatch.setattr(report, "keep_log", lambda work, target, record: None)

    code = main(
        [
            "--config", str(tmp_path / "install.toml"),
            "--work", str(tmp_path / "work"),
            "--target", str(tmp_path / "target"),
            "--skip-preflight",
            "--no-shell",
        ]
    )

    assert code == EXIT_INTEGRITY
    assert "the selected disk has been written to and may not boot" in capsys.readouterr().err


def test_a_failure_before_partitioning_says_nothing_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gentoo_install.model.config import InstallConfig

    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)

    def invalid(path: Path) -> InstallConfig:
        raise ConfigError("the configuration is invalid")

    monkeypatch.setattr(cli, "load", invalid)
    code = main(["--config", str(tmp_path / "install.toml"), "--dry-run"])

    said = capsys.readouterr().err
    assert code == EXIT_CONFIG
    assert "nothing was written to the selected disk" in said
    assert "may not boot" not in said


def test_a_body_failure_before_partitioning_says_nothing_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dataclasses import dataclass

    from gentoo_install.plan.operations import Context, Operation, Stage

    from .layouts import config

    partitioned: list[bool] = []

    @dataclass(frozen=True, kw_only=True)
    class FailBeforePartition(Operation):
        stage: Stage = Stage.PREFLIGHT

        def describe(self) -> str:
            return "prepare the install"

        def apply(self, context: Context) -> None:
            raise IntegrityError("preparation stopped")

    @dataclass(frozen=True, kw_only=True)
    class Partition(Operation):
        stage: Stage = Stage.PARTITION

        def describe(self) -> str:
            return "write the partition table"

        def apply(self, context: Context) -> None:
            partitioned.append(True)

    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)
    monkeypatch.setattr(cli, "load", lambda path: config())
    monkeypatch.setattr(
        cli, "probe_storage_facts", lambda chosen, probe: StorageFacts()
    )
    monkeypatch.setattr(
        cli,
        "build",
        lambda chosen, catalog, *, mirror, storage_facts, layout: (
            FailBeforePartition(),
            Partition(),
        ),
    )
    monkeypatch.setattr(report, "keep_log", lambda work, target, record: None)

    code = main(
        [
            "--config", str(tmp_path / "install.toml"),
            "--work", str(tmp_path / "work"),
            "--target", str(tmp_path / "target"),
            "--skip-preflight",
            "--no-shell",
        ]
    )

    said = capsys.readouterr().err
    assert partitioned == []
    assert code == EXIT_INTEGRITY
    assert "nothing was written to the selected disk" in said
    assert "may not boot" not in said


def test_the_menu_starts_from_the_firmware_the_machine_booted() -> None:
    """`_blank` took `BootloaderConfig`'s default, so a machine that booted
    BIOS opened the menu on a row reading `uefi - detected` and a template
    holding a GPT and an esp its firmware cannot read. The detection reached
    only `context.firmware`."""
    from gentoo_install.cli import _blank
    from gentoo_install.model.config import Firmware
    from gentoo_install.model.device import Mountpoint, PartitionTable, TableType
    from gentoo_install.model import compat

    for firmware, table, esp in (
        (Firmware.UEFI, TableType.GPT, True),
        (Firmware.BIOS, TableType.MBR, False),
    ):
        started = _blank("/dev/disk/by-id/example", 4, (), firmware=firmware)
        assert started.bootloader.firmware is firmware
        tables = [one.table for one in started.disk.graph.of_type(PartitionTable)]
        assert tables == [table], (firmware, tables)
        mounted = {str(one.path) for one in started.disk.graph.of_type(Mountpoint)}
        assert ("/efi" in mounted) is esp, (firmware, mounted)
        # Only the storage rules: the menu is what asks for a root password,
        # so the blank configuration is deliberately not installable yet.
        broken = [
            one
            for one in compat.violations(started)
            if one.when is not compat.Trait.ROOT_LOCKED
        ]
        assert not broken, broken


def test_a_busybox_applet_counts_as_a_missing_command(tmp_path: Path) -> None:
    """busybox provides `tar` without `--xattrs` and `mount` without
    `--rbind`. Reporting them as installed left the launcher with no package
    to offer and the preflight refusing the run after the disks were already
    partitioned."""
    from gentoo_install.exec.probe import Probe
    from gentoo_install.exec.runner import Result, Runner

    class Busybox(Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            timeout: float | None = None,
        ) -> Result:
            said = f"{argv[0]} (BusyBox v1.36.1) multi-call binary"
            return Result(argv=tuple(argv), returncode=0, stdout=said, stderr="", seconds=0.0)

    probe = Probe(runner=Busybox(log=lambda line: None), work=tmp_path)
    # Only commands actually on this PATH can be judged; the rest are absent
    # either way and say nothing about the implementation check.
    present = [name for name in ("tar", "mount", "blkid") if shutil.which(name)]
    said = report.absent(present, probe)
    assert set(present) <= said, (present, said)
    # Without a probe the old answer stands: on PATH is enough.
    assert not report.absent(present)


def test_a_configured_install_checks_its_mirror_and_not_the_package_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`packages.gentoo.org` is what the menu reads. Requiring it stopped five
    installs on a network where the chosen mirror answered and that site did
    not."""
    asked: list[str] = []

    def why(url: str) -> str:
        asked.append(url)
        return ""

    monkeypatch.setattr(fetch, "why_unreachable", why)
    monkeypatch.setattr(cli, "_check_the_clock", lambda: None)
    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    code = main(["--config", "tests/fixtures/btrfs-luks.toml", "--dry-run"])
    assert code == EXIT_OK
    assert not asked, "a dry run reads nothing at all"

    code = main(["--config", "tests/fixtures/btrfs-luks.toml", "--missing-commands"])
    assert code == EXIT_OK
    assert not asked


def test_the_mirror_check_names_the_mirror_it_could_not_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gentoo_install import errors
    from gentoo_install.exec.config import load

    monkeypatch.setattr(
        fetch, "why_unreachable", lambda url: "certificate verify failed: unable to get issuer"
    )
    config = load(Path("tests/fixtures/btrfs-luks.toml"))
    # The host is read from the configuration rather than written here: the
    # region's first mirror changes, and a test naming one of them fails for
    # a reason that has nothing to do with the rule it holds.
    from gentoo_install.model.mirrors import gentoo_distfiles

    host = gentoo_distfiles(config.portage.mirrors.region)[0].split("//", 1)[1].split("/", 1)[0]
    with pytest.raises(errors.PreflightFailed, match=re.escape(host)) as raised:
        cli._require_mirror(config, DEFAULT_MIRROR)
    # The reason is carried out, not discarded: `cannot reach X` sent a run to
    # look at the network when the answer was a certificate the medium could
    # not verify.
    assert "certificate verify failed" in str(raised.value)


def test_a_question_is_asked_after_the_earlier_keys_are_thrown_away(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The menu is curses and the key that left it is still in the terminal's
    input queue, so `readline` returned it at once and both questions after a
    failed install were answered by keystrokes aimed at the screen before
    them."""
    import io
    import sys as system

    from gentoo_install import cli

    flushed: list[str] = []
    monkeypatch.setattr(cli, "_forget_what_was_typed", lambda: flushed.append("flushed"))

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(system, "stdin", Terminal("y\n"))
    assert cli._asked("well?") is True
    assert flushed == ["flushed"], "the leftover keys go before the question"
    assert "well?" in capsys.readouterr().out


def test_nothing_is_flushed_on_a_terminal_this_run_does_not_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tcflush` raises on a stdin that is not a terminal rather than
    answering, and a run reading its configuration from a pipe still has to
    reach its own closing path."""
    import io
    import sys as system

    from gentoo_install import cli

    monkeypatch.setattr(system, "stdin", io.StringIO(""))
    cli._forget_what_was_typed()


def test_a_conversion_reads_the_running_layout_even_for_a_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A conversion derives its whole plan from the machine it is running on,
    so a dry run that skipped the probe would have nothing to print."""
    read: list[str] = []

    class Reading:
        def __init__(self, **_: object) -> None:
            pass

        def storage_layout(self) -> StorageLayout:
            read.append("layout")
            return StorageLayout(
                root_device="/dev/vda2",
                root_filesystem_type="ext4",
                root_uuid="8f1c0a2e-0000-4000-8000-000000000001",
                root_on_lvm=False,
                root_on_luks=False,
                root_on_mdraid=False,
                root_below_device="/dev/vda",
                boot_device="/dev/vda2",
                boot_filesystem_type="ext4",
                boot_same_filesystem=True,
                esp_device="/dev/vda1",
                esp_mountpoint="/efi",
                uefi=True,
                root_free_bytes=20 * 2**30,
            )

    from .layouts import config

    converted = replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId(""), mode=DiskMode.IN_PLACE),
    )
    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)
    monkeypatch.setattr(cli, "load", lambda path: converted)
    monkeypatch.setattr(cli, "Probe", Reading)

    code = main(
        [
            "--config", str(tmp_path / "install.toml"),
            "--work", str(tmp_path / "work"),
            "--dry-run",
        ]
    )

    assert code == EXIT_OK
    assert read == ["layout"], "the layout is what a conversion plan is derived from"
    assert "swap" in capsys.readouterr().out


def _conversion_arguments(no_shell: bool) -> argparse.Namespace:
    return argparse.Namespace(no_shell=no_shell)


def test_the_swap_is_confirmed_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one step with no second attempt. A wrong answer stops the run."""
    said: list[str] = []
    monkeypatch.setattr(cli, "_unattended", lambda arguments: False)
    monkeypatch.setattr("builtins.input", lambda *_: "convert")
    assert cli._confirmed_swap(_conversion_arguments(False), said.append)
    printed = capsys.readouterr().out
    assert "/usr" in printed and "/home" in printed

    monkeypatch.setattr("builtins.input", lambda *_: "yes")
    assert not cli._confirmed_swap(_conversion_arguments(False), said.append)
    assert any("not confirmed" in one for one in said)


def test_an_unattended_conversion_is_not_asked_but_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question here would hold a serial console open for ever, and the mode
    in the configuration file is the authorisation."""

    def refuse(*_: object) -> str:
        raise AssertionError("an unattended run must not be asked")

    monkeypatch.setattr("builtins.input", refuse)
    monkeypatch.setattr(cli, "_unattended", lambda arguments: True)
    said: list[str] = []
    assert cli._confirmed_swap(_conversion_arguments(True), said.append)
    assert any("/usr" in one for one in said)


def test_a_layout_that_cannot_be_converted_exits_as_a_preflight_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing ran, so code 4 would say a command failed. The machine is one
    this installer declines to convert, which is what code 2 says."""
    from dataclasses import replace

    from .layouts import config

    class OnZfs:
        def __init__(self, **_: object) -> None:
            pass

        def storage_layout(self) -> StorageLayout:
            return StorageLayout(
                root_device="rpool/ROOT/gentoo",
                root_filesystem_type="zfs",
                root_uuid=None,
                root_on_lvm=False,
                root_on_luks=False,
                root_on_mdraid=False,
                root_below_device=None,
                boot_device=None,
                boot_filesystem_type=None,
                boot_same_filesystem=True,
                esp_device="/dev/nvme0n1p1",
                esp_mountpoint="/boot/efi",
                uefi=True,
                root_free_bytes=20 * 2**30,
            )

    converted = replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId(""), mode=DiskMode.IN_PLACE),
    )
    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)
    monkeypatch.setattr(cli, "load", lambda path: converted)
    monkeypatch.setattr(cli, "Probe", OnZfs)

    code = main(
        [
            "--config", str(tmp_path / "install.toml"),
            "--work", str(tmp_path / "work"),
            "--dry-run",
        ]
    )

    assert code == EXIT_PREFLIGHT
    assert "zfs" in capsys.readouterr().err


def test_a_conversion_acts_on_the_running_root_not_the_mount_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every operation after the swap acts on `/`. Left at `--target` they
    would chroot into a directory the machine does not have."""
    from dataclasses import replace

    from .layouts import config

    seen: list[Path] = []

    class Recording:
        def __init__(self, **fields: object) -> None:
            seen.append(cast(Path, fields["mountpoint"]))
            self.given_up: set[str] = set()
            self.runner = fields["runner"]

    converted = replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId(""), mode=DiskMode.IN_PLACE),
    )
    monkeypatch.setattr(cli, "Machine", Recording)
    monkeypatch.setattr(cli, "apply", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_confirmed_swap", lambda arguments, record: True)
    monkeypatch.setattr(report, "keep_log", lambda work, target, record: None)
    arguments = argparse.Namespace(
        work=tmp_path / "work",
        target=Path("/mnt/gentoo"),
        skip_preflight=True,
        resume=False,
        no_shell=True,
        dry_run=False,
    )
    cli.install(converted, (), arguments, cli.RunState())
    assert seen == [Path("/")], seen


def test_the_machine_gets_the_derived_configuration_for_a_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check that refuses a mounted disk has to see the empty graph a
    conversion was given; everything that resolves a `DeviceId` has to see the
    graph read from the machine."""
    from dataclasses import replace

    from .layouts import config

    seen: list[object] = []

    class Recording:
        def __init__(self, **fields: object) -> None:
            seen.append(fields["config"])
            self.given_up: set[str] = set()
            self.runner = fields["runner"]

    converted = replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId(""), mode=DiskMode.IN_PLACE),
    )
    running = replace(converted, disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId("x")))
    monkeypatch.setattr(cli, "Machine", Recording)
    monkeypatch.setattr(cli, "apply", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_confirmed_swap", lambda arguments, record: True)
    monkeypatch.setattr(report, "keep_log", lambda work, target, record: None)
    arguments = argparse.Namespace(
        work=tmp_path / "work",
        target=Path("/mnt/gentoo"),
        skip_preflight=True,
        resume=False,
        no_shell=True,
        dry_run=False,
    )

    cli.install(converted, (), arguments, cli.RunState(), running)

    assert seen == [running], "the machine takes the derived one"


def test_the_conversion_offer_names_the_command_the_reading_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured on an Alpine 3.21 cloud image over ssh: busybox ships no
    `findmnt` and no `lsblk`, so `storage_layout()` came back with every field
    `None` and the conversion was refused with `the running root device could
    not be read`. The machine was readable; the package was missing, and the
    refusal has to say which one so the operator can install it."""

    class Probe:
        def live_medium(self) -> str:
            return ""

        def storage_layout(self) -> StorageLayout:
            raise AssertionError("the layout must not be read without its commands")

    absent: set[str] = {"findmnt", "lsblk"}
    monkeypatch.setattr(report, "absent", lambda wanted, probe=None: set(absent))

    running, refused = cli._conversion_offer(cast(RealProbe, Probe()))
    assert running == ""
    assert "findmnt" in refused and "lsblk" in refused, refused

    # Negative control: with every command present the offer reads the layout,
    # which this double refuses to answer. A guard that fires unconditionally
    # would swallow that and return a refusal instead.
    absent.clear()
    with pytest.raises(AssertionError, match="must not be read"):
        cli._conversion_offer(cast(RealProbe, Probe()))


def test_an_unattended_conversion_still_records_that_ssh_stops() -> None:
    """Measured on Debian 12 and Arch: once `/usr` and `/etc` belong to the new
    system, a new ssh login stops working until the machine reboots, while the
    session that started the run keeps its own mapped binaries. An unattended
    run is not asked anything, so the warning has to be recorded before that
    return or the one run nobody is watching never carries it.
    """
    said: list[str] = []
    unattended = argparse.Namespace(no_shell=True)

    assert cli._confirmed_swap(unattended, said.append)
    assert any(cli.SESSION_IS_THE_LIFELINE in line for line in said), said
    assert any("in-place conversion replaces" in line for line in said), said

    # Negative control: the sentence has to name ssh and the reboot, or it
    # tells the operator nothing they can act on.
    assert "ssh" in cli.SESSION_IS_THE_LIFELINE
    assert "reboot" in cli.SESSION_IS_THE_LIFELINE


def test_a_symlinked_log_directory_in_the_target_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy ran as root through a lexical `target / "var/log/..."` join and
    a plain `shutil.copy2`, so a `var/log/gentoo-install` symlink in the target
    wrote the run's log outside it. A conversion replaces a running system's
    userland, where `/var/log` holds whatever that distribution left there.
    """
    from gentoo_install.exec.report import keep_log

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "install.log").write_text("the run\n")

    target = tmp_path / "target"
    (target / "var/log").mkdir(parents=True)
    # A directory, not a file: `mkdir(exist_ok=True)` refuses a symlink to a
    # file by itself, so only this shape reaches `copy2` and writes outside.
    (target / "var/log/gentoo-install").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(Path, "is_mount", lambda self: self == target)
    said: list[str] = []
    keep_log(tmp_path, target, said.append)

    assert list(outside.iterdir()) == [], list(outside.iterdir())
    assert said and "could not be copied" in said[0], said


def test_a_log_directory_that_is_a_real_directory_still_receives_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control for the refusal above: the ordinary target, where
    nothing on the way is a symlink, still gets both run files."""
    import stat

    from gentoo_install.exec.report import keep_log

    (tmp_path / "install.log").write_text("the run\n")
    (tmp_path / "install.log").chmod(0o600)
    (tmp_path / "install.jsonl").write_text('{"one": 1}\n')

    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(Path, "is_mount", lambda self: self == target)
    said: list[str] = []
    keep_log(tmp_path, target, said.append)

    kept = target / "var/log/gentoo-install"
    assert (kept / "install.log").read_text() == "the run\n"
    assert (kept / "install.jsonl").read_text() == '{"one": 1}\n'
    assert stat.S_IMODE((kept / "install.log").stat().st_mode) == 0o600
    assert said and "the log of this run is in" in said[0], said
