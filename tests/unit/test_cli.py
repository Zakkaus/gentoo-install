# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import argparse
import getpass
import io
import os
import shutil
import sys
import time
from typing import Any, Final, Sequence, cast
import re
from dataclasses import replace
from pathlib import Path

import pytest

from gentoo_install import cli, errors
from gentoo_install.i18n import Catalog, tag_for
from gentoo_install.exec import fetch
from gentoo_install.exec import report
from gentoo_install.exec.probe import BootMethod, Probe as RealProbe
from gentoo_install.exec.runner import Result, Runner
from gentoo_install.cli import EXIT_CONFIG, EXIT_INTEGRITY, EXIT_OK, EXIT_PREFLIGHT, main
from gentoo_install.errors import CommandFailed, ConfigError, IntegrityError
from gentoo_install.plan.operations import CommandOutput
from gentoo_install.model.config import DiskConfig, DiskMode, ImageFormat, MemoryLaunch, MemoryMode
from gentoo_install.model.hardware import CpuVendor, HardwareFacts
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


def test_a_dry_run_refuses_an_unknown_shipped_value_before_network_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configuration = tmp_path / "invalid-locale.toml"
    configuration.write_text(
        (FIXTURES / "btrfs-luks.toml")
        .read_text(encoding="utf-8")
        .replace('locale = "zh_TW.UTF-8"', 'locale = "zh_CH.UTF-8"'),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(
        cli,
        "_check_the_clock",
        lambda: pytest.fail("an invalid configuration checked the network clock"),
    )

    assert main(["--config", str(configuration), "--dry-run"]) == EXIT_CONFIG
    assert "system.locale is 'zh_CH.UTF-8'" in capsys.readouterr().err


def test_dd_dry_run_renders_its_only_operation_without_a_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configuration = tmp_path / "dd.toml"
    configuration.write_text(
        '[disk]\n'
        'mode = "dd"\n'
        'source = "/run/prepared.raw.zst"\n'
        'source_format = "zst"\n'
        'destination = "/dev/disk/by-id/virtio-target"\n'
    )
    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(
        cli, "_check_the_clock", lambda: pytest.fail("dd dry run checked the network clock")
    )

    assert main(["--config", str(configuration), "--dry-run"]) == EXIT_OK
    printed = capsys.readouterr().out
    assert "stream the zst image /run/prepared.raw.zst onto /dev/disk/by-id/virtio-target" in printed
    assert printed.splitlines()[-1] == "1 operations: partition 1"

def test_dd_execution_skips_target_shell_and_log_handover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gentoo_install.plan import dd

    installation = load(FIXTURES / "btrfs-luks.toml")
    installation = replace(
        installation,
        disk=DiskConfig(
            graph=DeviceGraph.build(()),
            root=DeviceId(""),
            mode=DiskMode.DD,
            source="/run/prepared.raw",
            source_format=ImageFormat.RAW,
            destination="/dev/disk/by-id/virtio-target",
        ),
    )
    arguments = argparse.Namespace(
        work=tmp_path / "work",
        target=tmp_path / "target",
        skip_preflight=True,
        resume=False,
        no_shell=False,
        menu=False,
     lang="")
    monkeypatch.setattr(cli, "apply", lambda *args: None)
    monkeypatch.setattr(report, "offer_paste", lambda *args: None)
    monkeypatch.setattr(
        cli, "_offer_a_shell", lambda *args: pytest.fail("dd offered a target shell")
    )
    monkeypatch.setattr(
        report, "keep_log", lambda *args: pytest.fail("dd copied its log into a target")
    )

    assert cli.install(
        installation,
        (dd.WriteImage(
            source=installation.disk.source,
            source_format=installation.disk.source_format,
            destination=installation.disk.destination,
        ),),
        arguments,
        cli.RunState(),
    ) == EXIT_OK

def test_image_write_offer_requires_a_live_or_memory_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = RealProbe(runner=Runner(log=lambda line: None), work=tmp_path)
    monkeypatch.setattr(probe, "live_medium", lambda: "")
    monkeypatch.setattr(probe, "memory_environment", lambda: False)
    from gentoo_install.model import refusals

    assert cli._image_write_offer(probe).reason == refusals.WOULD_OVERWRITE_THE_INSTALLER

    monkeypatch.setattr(probe, "live_medium", lambda: "the root filesystem is overlay")
    assert not cli._image_write_offer(probe)

    monkeypatch.setattr(probe, "live_medium", lambda: "")
    monkeypatch.setattr(probe, "memory_environment", lambda: True)
    assert not cli._image_write_offer(probe)

@pytest.mark.parametrize(
    ("flag", "mode"),
    (("--ram", MemoryMode.RAM), ("--lowram", MemoryMode.LOWRAM)),
)
def test_memory_mode_flags_choose_the_named_environment(flag: str, mode: MemoryMode) -> None:
    arguments = cli.parser().parse_args([flag])
    assert cli._memory_launch(arguments) == MemoryLaunch(mode)
    with_credentials = cli.parser().parse_args(
        [
            flag,
            "--ssh-key",
            "ssh-ed25519 public-key",
            "--ssh-port",
            "2222",
            "--root-password",
            "secret",
        ]
    )
    assert cli._memory_launch(with_credentials) == MemoryLaunch(
        mode,
        ssh_key="ssh-ed25519 public-key",
        ssh_port=2222,
        root_password="secret",
    )


def test_memory_mode_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["--ram", "--lowram"])


def test_memory_key_must_be_a_file_or_ssh_public_key(tmp_path: Path) -> None:
    key = tmp_path / "key.pub"
    key.write_text("public key", encoding="utf-8")
    from_file = cli._memory_launch(
        cli.parser().parse_args(["--ram", "--ssh-key", str(key)])
    )
    assert from_file is not None and from_file.ssh_key == str(key)
    with pytest.raises(ConfigError, match="not a readable file"):
        cli._memory_launch(
            cli.parser().parse_args(["--ram", "--ssh-key", "not-a-public-key"])
        )

    # The four other forms reach the executor without being read here.
    for value in (
        "github:zakkaus",
        "gitlab:zakkaus",
        "https://example.invalid/key.pub",
        "ssh-ed25519 AAAAC3Nz zakk@box",
    ):
        given = cli._memory_launch(cli.parser().parse_args(["--ram", "--ssh-key", value]))
        assert given is not None and given.ssh_key == value, value

    with pytest.raises(ConfigError, match="scheme: ftp"):
        cli._memory_launch(
            cli.parser().parse_args(["--ram", "--ssh-key", "ftp://host/key.pub"])
        )

def test_memory_key_is_resolved_before_the_boot_entry_is_written(tmp_path: Path) -> None:
    """A key source becomes validated key text before the payload is built."""
    public_key = (
        "ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIB+85deBslaLOMFw71dx23wo7fFT76GVcEyQS9IdVvvT "
        "netboot@example"
    )
    key = tmp_path / "operator key.pub"
    key.write_text(f"{public_key}\n", encoding="utf-8")

    keys = cli._memory_ssh_keys(
        MemoryLaunch(MemoryMode.RAM, ssh_key=str(key)),
        Runner(log=lambda line: None),
    )

    assert keys == (public_key,)


@pytest.mark.parametrize("mode", ("--ram", "--lowram"))
def test_memory_modes_require_a_one_shot_boot_entry(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(RealProbe, "boot_method", lambda self: BootMethod.NONE)
    code = main(["--config", str(FIXTURES / "btrfs-luks.toml"), "--dry-run", mode])
    assert code == EXIT_PREFLIGHT
    assert "cannot arm a one-shot boot entry" in capsys.readouterr().err


def test_ram_warns_but_proceeds_for_a_layout_without_zfs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(RealProbe, "boot_method", lambda self: BootMethod.SYSTEMD_BOOT)
    code = main(["--config", str(FIXTURES / "btrfs-luks.toml"), "--dry-run", "--ram"])
    said = capsys.readouterr()
    assert code == EXIT_OK
    assert "warning: --ram is slower for a layout without ZFS" in said.err
    assert "operations:" in said.out


def test_a_dry_run_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preview never invokes an operation's executor."""
    from collections.abc import Callable
    from dataclasses import dataclass

    from gentoo_install.plan.operations import Context, Operation, Stage

    executed: list[Context] = []

    @dataclass(frozen=True, kw_only=True)
    class RecordingOperation(Operation):
        stage: Stage = Stage.PARTITION
        executor: Callable[[Context], None]

        def describe(self) -> str:
            return "record execution"

        def apply(self, context: Context) -> None:
            self.executor(context)

    operation = RecordingOperation(executor=executed.append)
    monkeypatch.setattr(
        cli,
        "build",
        lambda chosen, catalog, *, mirror, storage_facts, layout, supports_v3, hardware: (operation,),
    )
    code = main(["--config", str(FIXTURES / "ext4-bios.toml"), "--dry-run"])
    assert code == EXIT_OK
    assert executed == []


def test_a_dry_run_passes_probed_hardware_to_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gentoo_install.plan.operations import Operation

    expected = HardwareFacts(cpu_vendor=CpuVendor.INTEL, virtual_machine=False)
    planned: list[HardwareFacts] = []

    class HardwareProbe:
        def __init__(self, **_: object) -> None:
            pass

        def hardware(self) -> HardwareFacts:
            return expected

    def build_with_hardware(
        chosen: object,
        catalog: object,
        *,
        mirror: str,
        storage_facts: StorageFacts,
        layout: StorageLayout | None,
        supports_v3: bool | None,
        hardware: HardwareFacts,
    ) -> tuple[Operation, ...]:
        planned.append(hardware)
        return ()

    monkeypatch.setattr(cli, "Probe", HardwareProbe)
    monkeypatch.setattr(cli, "build", build_with_hardware)

    assert main(["--config", str(FIXTURES / "ext4-bios.toml"), "--dry-run"]) == EXIT_OK
    assert planned == [expected]


def test_a_dry_run_names_devices_by_id_rather_than_by_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A separate property from applying nothing: the preview must not have
    gone looking for a node under `/dev` to print."""
    main(["--config", str(FIXTURES / "ext4-bios.toml"), "--dry-run"])
    assert "/dev/" not in capsys.readouterr().out


def test_an_unknown_option_is_a_configuration_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--not-an-option"]) == EXIT_CONFIG
    assert "unrecognized arguments" in capsys.readouterr().err


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


def online(monkeypatch: pytest.MonkeyPatch, answer: bool = True, said: str = "") -> None:
    """No test opens a connection: the answer is given rather than measured.

    Both spellings, because `cli` asks for the reason and everything else asks
    the question: a double that answers one and not the other lets the caller
    change which it uses without a test noticing.
    """
    monkeypatch.setattr(fetch, "online", lambda: answer)
    monkeypatch.setattr(
        fetch, "why_offline", lambda: "" if answer else (said or "HTTP Error 503")
    )
    # The mirror answers with the package site by default: a machine that
    # reaches neither is the offline case, and a test that wants the mirror up
    # while the site is down says so itself.
    monkeypatch.setattr(fetch, "mirror_online", lambda *a, **k: answer)


class TerminalInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def interactive_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give a menu test an interactive standard input."""
    monkeypatch.setattr(sys, "stdin", TerminalInput())


def test_xanmod_versions_come_from_the_gentoo_zh_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fetch,
        "overlay_versions",
        lambda atom: (("7.1.9", False),) if atom == "sys-kernel/xanmod-kernel" else (),
    )
    monkeypatch.setattr(fetch, "package_versions", lambda atom: pytest.fail(atom))
    assert cli._kernel_versions("sys-kernel/xanmod-kernel") == (("7.1.9", False),)

def test_the_menu_stops_when_the_machine_cannot_reach_the_package_site(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kernel versions and the ZFS ceiling are read live so the installer runs
    on a medium with no Gentoo repository; offline there is nothing to offer."""
    interactive_stdin(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    online(monkeypatch, False, said="HTTP Error 404: Not Found")
    assert main([]) == EXIT_PREFLIGHT
    said = capsys.readouterr().err
    # The site's own answer, not a guess about the machine: the url names one
    # atom, so a package rename upstream reads as a broken network.
    assert "neither packages.gentoo.org" in said, said
    assert "HTTP Error 404: Not Found" in said, said


def test_a_mirror_that_answers_is_enough_to_open_the_menu(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version rows are read from the package site and the install from a
    mirror, so the site being unreachable costs the pinned versions and nothing
    else. One guest resolved that site to an IPv6 address alone with no route
    to reach it, and was refused at the first screen with a working mirror one
    row away."""
    interactive_stdin(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    online(monkeypatch, False, said="Errno 101 Network is unreachable")
    monkeypatch.setattr(fetch, "mirror_online", lambda *a, **k: True)

    def reached(*args: object, **keywords: object) -> object:
        raise errors.PreflightFailed("the menu was reached")

    monkeypatch.setattr(cli, "_from_menu", reached)
    assert main([]) == EXIT_PREFLIGHT
    warned = capsys.readouterr().err
    assert "the menu was reached" in warned, warned
    assert "packages.gentoo.org did not answer" in warned, warned
    assert "Errno 101" in warned, warned

    # Negative control: with the mirror unreachable too, the machine still
    # reaches the menu and says what it will not have there, so the warning
    # above is the degradation and not a check that stopped running.
    online(monkeypatch, False, said="Errno 101 Network is unreachable")
    assert main([]) == EXIT_PREFLIGHT
    offline = capsys.readouterr().err
    assert "the menu was reached" in offline, offline
    assert "neither packages.gentoo.org" in offline, offline


def test_the_offline_refusal_happens_once_the_menu_has_answered(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refusing before the menu closed the one option that needs no network.

    `mode = "dd"` streams a prepared image from a local path, and it is chosen
    inside the menu. The refusal did not go away with the early check: it is
    `_require_mirror`, which reads the mirror the answered configuration names
    and which has always exempted that mode.
    """
    interactive_stdin(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    # The third spelling: `online` answers `online` and `why_offline`, and the
    # mirror check asks this one, so a run left with the real function reaches
    # the network from a test.
    monkeypatch.setattr(
        fetch, "why_mirror_unreachable", lambda *a, **k: "Errno 101 Network is unreachable"
    )

    fetching = load(FIXTURES / "btrfs-luks.toml")
    monkeypatch.setattr(cli, "_from_menu", lambda *a, **k: fetching)
    online(monkeypatch, False, said="Errno 101 Network is unreachable")
    assert main(["--dry-run"]) == EXIT_PREFLIGHT
    said = capsys.readouterr().err
    assert "cannot reach" in said, said

    prepared = load(FIXTURES / "vm-dd-raw.toml")
    assert prepared.disk.mode is DiskMode.DD
    monkeypatch.setattr(cli, "_from_menu", lambda *a, **k: prepared)
    online(monkeypatch, False, said="Errno 101 Network is unreachable")
    assert main(["--dry-run", "--work", str(tmp_path / "work")]) == EXIT_OK
    assert "operations:" in capsys.readouterr().out


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


@pytest.mark.parametrize(
    ("arguments", "stdin_isatty"),
    (([], False), (["--no-shell"], True)),
)
def test_an_unattended_menu_needs_a_configuration(
    arguments: list[str],
    stdin_isatty: bool,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither a pipe nor --no-shell may enter the interactive menu."""

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    if stdin_isatty:
        interactive_stdin(monkeypatch)
    else:
        monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)
    monkeypatch.setattr(
        cli,
        "_from_menu",
        lambda *args: pytest.fail("unattended invocation opened the menu"),
    )

    code = main(arguments)

    said = capsys.readouterr().err
    assert code == EXIT_PREFLIGHT
    assert "an unattended run needs --config FILE" in said


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

    monkeypatch.setattr(cli, "load_source", boom)
    code = main(["--config", str(FIXTURES / "vm-binpkg.toml")])
    assert code == cli.EXIT_COMMAND
    assert "No space left" in capsys.readouterr().err


def test_an_unexpected_error_is_named_rather_than_traced(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(cli, "load_source", boom)
    assert main(["--config", str(FIXTURES / "vm-binpkg.toml")]) == cli.EXIT_COMMAND
    assert "unexpected RuntimeError" in capsys.readouterr().err


def test_a_terminal_too_small_for_the_interface_says_so_rather_than_drawing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`too_small` was written and never called, so a 60x20 console got a menu
    with rows off the edge and no message saying why."""
    from gentoo_install.tui.curses_screen import too_small
    from gentoo_install.tui.widgets import MINIMUM_COLUMNS, MINIMUM_LINES

    class Sized:
        def __init__(self, lines: int, columns: int) -> None:
            self.lines, self.columns = lines, columns

        def size(self) -> tuple[int, int]:
            return self.lines, self.columns

    cramped = too_small(Sized(4, 16))
    assert "16x4" in cramped and f"{MINIMUM_COLUMNS}x{MINIMUM_LINES}" in cramped
    assert too_small(Sized(MINIMUM_LINES, MINIMUM_COLUMNS)) == ""

    # 60x20 is served rather than refused: it is under the two-pane floor and
    # over the one this refuses at, so `TwoPane` draws one pane there. The two
    # numbers are separate because a terminal the layout cannot use in full is
    # not a terminal the interface cannot use at all.
    from gentoo_install.tui.widgets import TWO_PANE_COLUMNS, TWO_PANE_LINES

    assert too_small(Sized(20, 60)) == ""
    assert MINIMUM_COLUMNS < TWO_PANE_COLUMNS and MINIMUM_LINES < TWO_PANE_LINES

    # The call and its effect, not the substring: `cramped = too_small(display)`
    # with the `raise` deleted keeps the text in `cli.py` and puts the menu on
    # a console it cannot draw on, which is the defect this test is named after.
    import ast
    import inspect

    from gentoo_install import cli as cli_module

    tree = ast.parse(inspect.getsource(cli_module))
    called = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "too_small"
    ]
    assert len(called) == 1, ast.dump(tree)[:200]
    answer = called[0].targets[0]
    assert isinstance(answer, ast.Name)
    raised = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and node.exc is not None
        and any(
            isinstance(inner, ast.Name) and inner.id == answer.id
            for inner in ast.walk(node.exc)
        )
    ]
    assert raised, f"nothing raises {answer.id}"


def test_the_menu_names_openssl_before_it_asks_anything(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fetch.password_hash` shells out to `openssl passwd -6`, and finding it
    absent at the root-password screen throws away every answer before it."""
    from gentoo_install.exec import preflight

    interactive_stdin(monkeypatch)
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

    interactive_stdin(monkeypatch)
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

    interactive_stdin(monkeypatch)
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

    arguments = argparse.Namespace(no_shell=True, menu=False, target=Path("/mnt/gentoo"), lang="")
    said: list[str] = []
    cli._offer_a_shell(
        arguments,
        cast(Machine, None),
        said.append,
        False,
        Path("/mnt/gentoo"),
        DiskMode.PARTITION,
        Catalog("en"),
    )
    assert not said and not capsys.readouterr().out


def test_the_closing_questions_are_asked_in_the_menu_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both questions come after curses has gone and were English literals, so
    a machine whose every screen had been in Chinese ended by asking
    `enter a root shell in /mnt/gentoo before unmounting? [y/N]`."""
    import argparse
    from types import SimpleNamespace

    from gentoo_install.exec import report
    from gentoo_install.exec.apply import Machine

    asked: list[str] = []

    def say_no(question: str, translate: object = None) -> bool:
        asked.append(question)
        return False

    monkeypatch.setattr(cli, "_unattended", lambda arguments: False)
    monkeypatch.setattr(cli, "_asked", say_no)
    arguments = argparse.Namespace(
        no_shell=False, menu=True, target=Path("/mnt/gentoo"), lang=""
    )
    machine = cast(Machine, SimpleNamespace(runner=None))

    cli._offer_a_shell(
        arguments,
        machine,
        lambda one: None,
        False,
        Path("/mnt/gentoo"),
        DiskMode.PARTITION,
        Catalog("zh-TW"),
    )
    report.offer_paste(
        Path("/tmp"), lambda one: None, False, False, say_no, lambda one: None, Catalog("zh-TW")
    )

    assert len(asked) == 2, asked
    for question in asked:
        assert not question.isascii(), question
    # And the tag the menu recorded is the one those questions are built
    # from: `_from_menu` writes it onto `RunState` and `install` reads it.
    walked = cli.RunState()
    walked.language = "zh-TW"
    assert cli._closing_catalog(walked, arguments).tag == "zh-TW"
    # A `--config` run never opens a screen, so the environment decides.
    assert cli._closing_catalog(cli.RunState(), arguments).tag == tag_for(
        override=arguments.lang
    )


def test_a_conversion_is_offered_a_shell_in_the_root_it_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read off the machine `m7i` converted: the question named `/mnt/gentoo`,
    which the conversion never created, and answering it would have chrooted
    into a directory that is not there."""
    import argparse

    from types import SimpleNamespace

    from gentoo_install.exec.apply import Machine

    from gentoo_install.exec.runner import Result

    class Fake:
        def __init__(self) -> None:
            self.argv: list[list[str]] = []

        # A `Result`, the way the real runner answers: a double returning None
        # hid the caller that reads the exit code for itself.
        def run(self, argv: list[str], check: bool = True) -> Result:
            self.argv.append(argv)
            return Result(argv=tuple(argv), returncode=0, stdout="", stderr="", seconds=0.0)

    asked: list[str] = []
    # The two unattended guards have their own test above; this one is about
    # what the question and the chroot name once it is asked.
    monkeypatch.setattr(cli, "_unattended", lambda arguments: False)
    def say_yes(question: str, translate: object = None) -> bool:
        asked.append(question)
        return True

    monkeypatch.setattr(cli, "_asked", say_yes)
    arguments = argparse.Namespace(no_shell=False, menu=False, target=Path("/mnt/gentoo"), lang="")
    runner = Fake()
    machine = cast(Machine, SimpleNamespace(runner=runner))

    cli._offer_a_shell(
        arguments, machine, lambda one: None, False, Path("/"), DiskMode.IN_PLACE, Catalog("en")
    )
    assert runner.argv == [["chroot", "/", "/bin/bash", "--login"]]
    assert "/mnt/gentoo" not in asked[0], asked
    # An ordinary install still says what it is about to do.
    cli._offer_a_shell(
        arguments,
        machine,
        lambda one: None,
        False,
        Path("/mnt/gentoo"),
        DiskMode.PARTITION,
        Catalog("en"),
    )
    assert "before unmounting" in asked[1], asked
    assert "before unmounting" not in asked[0], asked


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

    arguments = argparse.Namespace(no_shell=True, menu=False, target=tmp_path, lang="")
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


def test_the_log_is_kept_before_the_target_is_unmounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Stage.FINISH` unmounts, so a copy made after it lands on the install
    medium's tmpfs and goes with the reboot.

    Found on a machine this installer had installed: `/var/log/gentoo-install`
    was not there at all, and the copy had reported success.
    """
    import argparse
    from dataclasses import replace

    from gentoo_install import cli
    from gentoo_install.exec import report

    from gentoo_install.data import load_catalog
    from gentoo_install.plan.build import build
    from gentoo_install.plan.operations import Operation
    from gentoo_install.plan.operations import Stage

    from .layouts import config, ext4_on_gpt

    # The order the run takes, not the order the words sit in the source: a
    # closure can keep `report.keep_log(` early in the text and call it after
    # the closing stage, and this test would still pass.
    def order_of(fail: bool) -> list[str]:
        seen: list[str] = []

        class Recording:
            def __init__(self, **fields: object) -> None:
                self.given_up: set[str] = set()
                self.runner = fields["runner"]

        def applied(operations: object, machine: object, *rest: object) -> None:
            stages = {one.stage for one in cast("tuple[Operation, ...]", operations)}
            seen.append("closing" if stages == {Stage.FINISH} else "body")
            if fail and stages != {Stage.FINISH}:
                raise CommandFailed("the body stopped")

        monkeypatch.setattr(cli, "Machine", Recording)
        monkeypatch.setattr(cli, "apply", applied)
        monkeypatch.setattr(report, "keep_log", lambda work, target, record: seen.append("keep"))
        monkeypatch.setattr(cli, "_release", lambda *args: seen.append("release"))
        monkeypatch.setattr(cli, "_offer_a_shell", lambda *args: None)
        monkeypatch.setattr(report, "offer_paste", lambda *args, **kwargs: None)
        arguments = argparse.Namespace(
            work=tmp_path / f"work-{fail}",
            target=tmp_path / "target",
            skip_preflight=True,
            resume=False,
            no_shell=True,
            menu=False,
            dry_run=False,
            lang="",
        )
        try:
            cli.install(replace(config(ext4_on_gpt())), operations, arguments, cli.RunState())
        except CommandFailed:
            pass
        return seen

    operations = tuple(build(config(ext4_on_gpt()), load_catalog()))
    assert {one.stage for one in operations} >= {Stage.FINISH}, "the plan has a closing stage"

    done = order_of(fail=False)
    assert done.index("keep") < done.index("closing"), done
    stopped = order_of(fail=True)
    assert stopped.index("keep") < stopped.index("release"), stopped


def test_the_conversion_hands_the_offer_the_root_it_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`install()` resolves the target once — `/` in place, `--target`
    otherwise — and every other reader takes it from there. The offer took the
    unresolved value, so a conversion proposed `/mnt/gentoo`.
    """
    import argparse
    from dataclasses import replace

    from gentoo_install import cli
    from gentoo_install.data import load_catalog
    from gentoo_install.exec import report
    from gentoo_install.plan.build import build

    from .layouts import config, ext4_on_gpt

    handed: dict[str, tuple[object, ...]] = {}

    class Recording:
        def __init__(self, **fields: object) -> None:
            self.given_up: set[str] = set()
            self.runner = fields["runner"]

    monkeypatch.setattr(cli, "Machine", Recording)
    monkeypatch.setattr(cli, "apply", lambda *args: None)
    monkeypatch.setattr(report, "keep_log", lambda work, target, record: None)
    monkeypatch.setattr(report, "offer_paste", lambda *args, **kwargs: None)

    def offered(mode: DiskMode) -> tuple[object, ...]:
        monkeypatch.setattr(
            cli, "_offer_a_shell", lambda *args: handed.__setitem__(mode.value, args)
        )
        chosen = config(ext4_on_gpt())
        chosen = replace(chosen, disk=replace(chosen.disk, mode=mode))
        arguments = argparse.Namespace(
            work=tmp_path / f"work-{mode.value}",
            target=tmp_path / "target",
            skip_preflight=True,
            resume=False,
            no_shell=True,
            menu=False,
            dry_run=False,
            lang="",
        )
        cli.install(chosen, tuple(build(config(ext4_on_gpt()), load_catalog())), arguments, cli.RunState())
        return handed[mode.value]

    converting = offered(DiskMode.IN_PLACE)
    assert Path("/") in converting, converting
    assert tmp_path / "target" not in converting, converting
    # And an ordinary install is still handed the directory it mounted.
    ordinary = offered(DiskMode.PARTITION)
    assert tmp_path / "target" in ordinary, ordinary


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
    monkeypatch.setattr(cli, "load_source", lambda path: config())
    monkeypatch.setattr(
        cli, "probe_storage_facts", lambda chosen, probe: StorageFacts()
    )
    monkeypatch.setattr(
        cli,
        "build",
        lambda chosen, catalog, *, mirror, storage_facts, layout, supports_v3, hardware: (
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
    monkeypatch.setattr(cli, "load_source", lambda path: config())
    monkeypatch.setattr(
        cli, "probe_storage_facts", lambda chosen, probe: StorageFacts()
    )
    monkeypatch.setattr(
        cli,
        "build",
        lambda chosen, catalog, *, mirror, storage_facts, layout, supports_v3, hardware: (FailPartition(),),
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
    said = capsys.readouterr().err
    assert "this machine's storage has been written to and it may not boot" in said
    assert "the partition stage started" in said, said


def test_a_failure_before_partitioning_says_nothing_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gentoo_install.model.config import InstallConfig

    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)

    def invalid(path: Path) -> InstallConfig:
        raise ConfigError("the configuration is invalid")

    monkeypatch.setattr(cli, "load_source", invalid)
    code = main(["--config", str(tmp_path / "install.toml"), "--dry-run"])

    said = capsys.readouterr().err
    assert code == EXIT_CONFIG
    assert "nothing was written" in said
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
    monkeypatch.setattr(cli, "load_source", lambda path: config())
    monkeypatch.setattr(
        cli, "probe_storage_facts", lambda chosen, probe: StorageFacts()
    )
    monkeypatch.setattr(
        cli,
        "build",
        lambda chosen, catalog, *, mirror, storage_facts, layout, supports_v3, hardware: (
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
    assert "nothing was written" in said
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

        def hardware(self) -> HardwareFacts:
            return HardwareFacts()

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
    monkeypatch.setattr(cli, "load_source", lambda path: converted)
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
    return argparse.Namespace(no_shell=no_shell, menu=False, lang="")


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


def test_an_eof_at_the_conversion_prompt_declines_the_swap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def closed_stdin(*_: object) -> str:
        raise EOFError

    said: list[str] = []
    monkeypatch.setattr(cli, "_unattended", lambda arguments: False)
    monkeypatch.setattr("builtins.input", closed_stdin)

    assert not cli._confirmed_swap(_conversion_arguments(False), said.append)
    assert any("not confirmed" in line for line in said)
    assert "nothing was changed" in capsys.readouterr().err


def test_a_declined_conversion_exits_as_a_user_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .layouts import config

    converted = replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId(""), mode=DiskMode.IN_PLACE),
    )
    monkeypatch.setattr(cli, "_confirmed_swap", lambda arguments, record: False)
    arguments = argparse.Namespace(
        work=tmp_path / "work",
        target=Path("/mnt/gentoo"),
        skip_preflight=True,
        resume=False,
        no_shell=True,
        menu=False,
        dry_run=False,
     lang="")

    assert cli.install(converted, (), arguments, cli.RunState()) == cli.EXIT_ABORTED


def test_an_unattended_conversion_is_not_asked_but_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question here would hold a serial console open for ever, and the mode
    in the configuration file is the authorisation."""

    def refuse(*_: object) -> str:
        raise AssertionError("an unattended run must not be asked")

    monkeypatch.setattr("builtins.input", refuse)
    said: list[str] = []
    assert cli._confirmed_swap(_conversion_arguments(True), said.append)
    assert any("/usr" in one for one in said)


def test_a_conversion_with_nonterminal_stdin_is_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_: object) -> str:
        raise AssertionError("a nonterminal run must not be asked")

    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr("builtins.input", refuse)

    assert cli._confirmed_swap(_conversion_arguments(False), lambda line: None)


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

        def hardware(self) -> HardwareFacts:
            return HardwareFacts()

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
    monkeypatch.setattr(cli, "load_source", lambda path: converted)
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
        menu=False,
        dry_run=False,
     lang="")
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
        menu=False,
        dry_run=False,
     lang="")

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

    # The layout comes back too: the Install row derives a conversion's plan
    # from it before the operator presses it.
    running, refused, layout = cli._conversion_offer(cast(RealProbe, Probe()))
    assert layout is None, layout
    assert running == ""
    # The reason is a catalog key and the commands are the detail beside it:
    # a translated screen cannot hold `findmnt` and the operator still needs it.
    from gentoo_install.model import refusals as reasons

    assert refused.reason == reasons.CANNOT_READ_THE_SYSTEM, refused
    assert "findmnt" in refused.detail and "lsblk" in refused.detail, refused

    # Negative control: with every command present the offer reads the layout,
    # which this double refuses to answer. A guard that fires unconditionally
    # would swallow that and return a refusal instead.
    absent.clear()
    with pytest.raises(AssertionError, match="must not be read"):
        cli._conversion_offer(cast(RealProbe, Probe()))

def test_an_unreadable_fstab_refuses_conversion_before_entries_are_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gentoo_install.exec import probe as probe_module
    from gentoo_install.model import refusals

    class Storage(Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            timeout: float | None = None,
        ) -> Result:
            if argv[0] == "findmnt":
                stdout = (
                    '{"filesystems":[{"target":"/","source":"/dev/vda2",'
                    '"fstype":"ext4","avail":21474836480}]}'
                )
            elif argv[0] == "lsblk":
                stdout = (
                    '{"blockdevices":[{"path":"/dev/vda2","type":"part",'
                    '"pkname":"/dev/vda"},{"path":"/dev/vda","type":"disk"}]}'
                )
            else:
                stdout = "root-uuid\n"
            return Result(argv=tuple(argv), returncode=0, stdout=stdout, stderr="", seconds=0.0)

    def unreadable(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        raise OSError(f"{path}: permission denied")

    monkeypatch.setattr(probe_module, "EFI_MARKER", tmp_path / "no-efi")
    monkeypatch.setattr(Path, "read_text", unreadable)
    monkeypatch.setattr(RealProbe, "live_medium", lambda self: "")
    monkeypatch.setattr(report, "absent", lambda wanted, probe=None: set())

    _, refused, layout = cli._conversion_offer(
        RealProbe(runner=Storage(log=lambda line: None), work=tmp_path)
    )

    assert layout is None
    assert refused.reason == refusals.CANNOT_READ_THE_SYSTEM
    assert "/etc/fstab" in refused.detail

    # A machine with no fstab at all carries nothing, which is an answer: only
    # one this cannot open is a probe that failed.
    def absent(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        raise FileNotFoundError(f"{path}: no such file")

    monkeypatch.setattr(Path, "read_text", absent)
    _, allowed, read = cli._conversion_offer(
        RealProbe(runner=Storage(log=lambda line: None), work=tmp_path)
    )
    assert read is not None, allowed
    assert read.carried_fstab == ()


def test_an_unreadable_bootctl_status_does_not_build_a_grub_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gentoo_install.exec import probe as probe_module

    class Failing(Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            timeout: float | None = None,
        ) -> Result:
            return Result(
                argv=tuple(argv),
                returncode=1,
                stdout="bootctl: failed to read boot loader status\n",
                stderr="",
                seconds=0.0,
            )

    class BootProbe(RealProbe):
        def storage_layout(self) -> StorageLayout:
            return StorageLayout(
                root_device="/dev/vda2",
                root_filesystem_type="ext4",
                root_uuid="root-uuid",
                root_on_lvm=False,
                root_on_luks=False,
                root_on_mdraid=False,
                root_below_device="/dev/vda",
                boot_device="/dev/vda2",
                boot_filesystem_type="ext4",
                boot_same_filesystem=True,
                esp_device="/dev/vda1",
                esp_mountpoint="/boot/efi",
                uefi=True,
                root_free_bytes=20 * 2**30,
            )

    monkeypatch.setattr(probe_module, "_efi_variables", lambda: True)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    with pytest.raises(CommandFailed, match="bootctl status"):
        cli._boot_target(BootProbe(runner=Failing(log=lambda line: None), work=tmp_path))


def test_an_unattended_conversion_still_records_that_ssh_stops() -> None:
    """Measured on Debian 12 and Arch: once `/usr` and `/etc` belong to the new
    system, a new ssh login stops working until the machine reboots, while the
    session that started the run keeps its own mapped binaries. An unattended
    run is not asked anything, so the warning has to be recorded before that
    return or the one run nobody is watching never carries it.
    """
    said: list[str] = []
    unattended = argparse.Namespace(no_shell=True, menu=False, lang="")

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


def test_a_dry_run_takes_its_configuration_from_a_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every mode takes its configuration the same way; what differs between
    installing, converting and writing an image is what the configuration
    says, not where it came from. `load_source` existed with no caller, so
    `--config` still turned a URL into a filename and answered that no such
    file exists.
    """
    from gentoo_install.exec import config as loader

    asked: list[str] = []
    body = (FIXTURES / "ext4-bios.toml").read_text()

    def reading(url: str, *, ceiling: int, password: str = "") -> str:
        asked.append(url)
        return body

    monkeypatch.setattr(fetch, "read_text", reading)
    assert main(["--config", "https://example.invalid/machine.toml", "--dry-run"]) == EXIT_OK
    assert asked == ["https://example.invalid/machine.toml"], asked
    assert "operations:" in capsys.readouterr().out.splitlines()[-1]
    assert loader.looks_like_a_url("https://example.invalid/machine.toml")


def test_a_local_configuration_never_reaches_the_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The negative direction: accepting a URL must not send an ordinary path
    through the fetcher, which would need a network to read a file."""

    def refuse(url: str, *, ceiling: int, password: str = "") -> str:
        raise AssertionError(f"a path was fetched: {url}")

    monkeypatch.setattr(fetch, "read_text", refuse)
    assert main(["--config", str(FIXTURES / "ext4-bios.toml"), "--dry-run"]) == EXIT_OK
    assert "operations:" in capsys.readouterr().out.splitlines()[-1]


def _memory_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A UEFI machine with a mounted esp, so the memory plan can be derived."""
    from gentoo_install.model.device import StorageLayout

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(RealProbe, "boot_method", lambda self: BootMethod.SYSTEMD_BOOT)
    monkeypatch.setattr(
        RealProbe,
        "storage_layout",
        lambda self: StorageLayout(
            root_device="/dev/vda2",
            root_filesystem_type="btrfs",
            root_uuid=None,
            root_on_lvm=None,
            root_on_luks=None,
            root_on_mdraid=None,
            root_below_device=None,
            boot_device=None,
            boot_filesystem_type=None,
            boot_same_filesystem=True,
            esp_device="/dev/vda1",
            esp_mountpoint="/boot/efi",
            uefi=True,
            root_free_bytes=None,
        ),
    )
    monkeypatch.setattr(RealProbe, "disk_of_path", lambda self, path: "/dev/vda")
    monkeypatch.setattr(RealProbe, "partition_number_of_path", lambda self, path: 1)


def test_a_memory_run_prints_the_arming_plan_and_not_the_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What `--ram` arms happens after the reboot. Falling through to the
    install would partition the disk now, from the system being replaced."""
    _memory_machine(monkeypatch)
    code = main(["--config", str(FIXTURES / "btrfs-luks.toml"), "--dry-run", "--ram"])
    said = capsys.readouterr()
    assert code == EXIT_OK
    assert "arm one boot into the memory environment" in said.out, said.out
    assert "partition" not in said.out.lower(), said.out


def test_bypass_prints_the_replacing_operation_instead(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--bypass` is the path for firmware that drops a one-shot write, and it
    is the only one that touches the default entry."""
    _memory_machine(monkeypatch)
    code = main(
        ["--config", str(FIXTURES / "btrfs-luks.toml"), "--dry-run", "--ram", "--bypass"]
    )
    said = capsys.readouterr()
    assert code == EXIT_OK
    assert "replace the default boot entry" in said.out, said.out
    assert "arm one boot" not in said.out, said.out


def test_a_disarm_dry_run_renders_without_applying(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A preview must not clear the boot entry or delete the armed payload."""
    _memory_machine(monkeypatch)
    monkeypatch.setattr(
        cli,
        "apply",
        lambda *args, **keywords: pytest.fail("dry-run disarm called apply"),
    )

    code = main(["--disarm", "--dry-run"])

    said = capsys.readouterr()
    assert code == EXIT_OK, said
    assert "take back the armed boot and delete what it placed" in said.out


def test_disarm_missing_commands_does_not_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A command report must not change the armed boot environment."""
    _memory_machine(monkeypatch)
    monkeypatch.setattr(report, "absent", lambda commands, probe: frozenset({"rm"}))
    monkeypatch.setattr(
        cli,
        "apply",
        lambda *args, **keywords: pytest.fail("disarm command report called apply"),
    )

    code = main(["--disarm", "--missing-commands"])

    said = capsys.readouterr()
    assert code == EXIT_OK, said
    assert said.out == "rm\n"


def test_the_disarm_the_message_names_is_one_the_parser_takes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unattended arming prints ``armed. `reboot` when ready; `--disarm`
    takes it back.`` and the parser did not take that option, so an operator
    who followed it was answered `unrecognized arguments: --disarm`.
    """
    import inspect

    from gentoo_install import cli as command_line

    said = inspect.getsource(command_line._reboot_or_disarm)
    named = [one for one in said.split("`") if one.startswith("--")]
    assert named == ["--disarm"], named

    _memory_machine(monkeypatch)
    ran: list[str] = []
    monkeypatch.setattr(
        command_line, "apply", lambda operations, machine, on_start=None: ran.extend(
            one.describe() for one in operations
        )
    )

    code = main(["--disarm"])
    printed = capsys.readouterr()
    assert code == EXIT_OK, printed
    assert ran == ["take back the armed boot and delete what it placed"], ran


def test_bypass_without_a_memory_mode_is_refused() -> None:
    """It changes what a machine boots by default, so it may not be a flag
    that does nothing when the mode it belongs to was not asked for."""
    with pytest.raises(ConfigError, match="require --ram or --lowram"):
        cli._memory_launch(cli.parser().parse_args(["--bypass"]))


def test_the_boot_target_carries_whether_boot_is_its_own_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRUB's paths are relative to the filesystem its `root` names, so an
    entry written without this fact points at `/gentoo-install-ram/kernel` on
    a machine whose file is at `/boot/gentoo-install-ram/kernel`, and the
    armed boot stops in GRUB. The probe answers it; this holds the wiring."""
    from gentoo_install.model.device import StorageLayout

    for same in (True, False, None):
        monkeypatch.setattr(
            RealProbe,
            "storage_layout",
            lambda self, answer=same: StorageLayout(
                root_device="/dev/vda2",
                root_filesystem_type="ext4",
                root_uuid=None,
                root_on_lvm=None,
                root_on_luks=None,
                root_on_mdraid=None,
                root_below_device=None,
                boot_device=None,
                boot_filesystem_type=None,
                boot_same_filesystem=answer,
                esp_device=None,
                esp_mountpoint=None,
                uefi=False,
                root_free_bytes=None,
            ),
        )
        monkeypatch.setattr(RealProbe, "boot_method", lambda self: BootMethod.BIOS_GRUB)
        probe = RealProbe(runner=Runner(log=lambda line: None), work=Path("/tmp"))
        assert cli._boot_target(probe).boot_on_the_root_filesystem is same, same


def test_the_boot_target_carries_the_machine_this_is_running_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both memory environments are published per architecture, so an arming
    that does not name the machine fetches somebody else's image. `BootTarget`
    keeps no default for it, and this holds the one call site that fills it."""
    monkeypatch.setattr(cli, "architecture", lambda: "aarch64")
    monkeypatch.setattr(RealProbe, "boot_method", lambda self: BootMethod.BIOS_GRUB)
    probe = RealProbe(runner=Runner(log=lambda line: None), work=Path("/tmp"))

    assert cli._boot_target(probe).architecture == "aarch64"


def test_missing_commands_with_a_memory_mode_answers_for_the_arming(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`--ram` reads the ISO with `xorriso`, which no ordinary install needs.
    Asked without the mode, `--missing-commands` answers for a different run
    than the one about to happen, and the third `--ram` attempt fetched a
    gigabyte and stopped at `command: xorriso is not installed`."""
    from gentoo_install import cli

    fixture = Path("tests/fixtures/vm-ram.toml").resolve()
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name, path=None: None)
    monkeypatch.setattr(RealProbe, "boot_method", lambda self: BootMethod.BIOS_GRUB)

    code = cli.main(
        [
            "--config",
            str(fixture),
            "--ram",
            "--missing-commands",
            "--work",
            str(tmp_path),
        ]
    )

    said = capsys.readouterr().out.split()
    assert code == cli.EXIT_OK, said
    assert "xorriso" in said, said
    assert "curl" in said, said


def test_missing_commands_is_answered_before_the_arming_refusals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`--ram cannot arm a one-shot boot entry on this machine` is what a
    machine without `efibootmgr` answers, and `efibootmgr` is one of the
    commands being asked about: the guest was refused instead of told, so it
    installed nothing and the arming failed on the same machine minutes
    later."""
    import shutil as _shutil

    from gentoo_install import cli

    fixture = Path("tests/fixtures/vm-ram.toml").resolve()
    monkeypatch.setattr(_shutil, "which", lambda name, path=None: None)
    monkeypatch.setattr(RealProbe, "boot_method", lambda self: BootMethod.NONE)

    code = cli.main(
        ["--config", str(fixture), "--ram", "--missing-commands", "--work", str(tmp_path)]
    )
    said = capsys.readouterr().out.split()

    assert code == cli.EXIT_OK, said
    assert "xorriso" in said, said


def test_a_resume_refuses_a_journal_from_another_run(tmp_path: Path) -> None:
    """The README documents `--resume` for the same live session, installer
    and configuration because nothing checked any of it: a resume replays
    operations by position and description, so a journal from a different
    configuration skips the wrong ones, one from before a reboot skips
    operations whose result the reboot discarded, and one from a different
    installer skips whatever now sits at those positions."""
    import json

    import pytest as _pytest

    from gentoo_install import cli
    from gentoo_install.errors import ResumeRefused
    from gentoo_install.log import Journal

    said: list[str] = []
    path = tmp_path / "install.jsonl"
    same = {
        "configuration": "digest-of-the-file",
        "session": "the-boot-id",
        "installer": "digest-of-the-tree",
    }

    def journal_of(**changed: str) -> Journal:
        entry = {"event": "started", **same, **changed}
        path.write_text(json.dumps(entry) + "\n")
        return Journal(path=path)

    cli._refuse_a_different_run(journal_of(), same, said.append)
    assert said == []

    with _pytest.raises(ResumeRefused, match="different configuration"):
        cli._refuse_a_different_run(journal_of(configuration="another"), same, said.append)

    with _pytest.raises(ResumeRefused, match="rebooted"):
        cli._refuse_a_different_run(journal_of(session="another"), same, said.append)

    with _pytest.raises(ResumeRefused, match="different installer"):
        cli._refuse_a_different_run(journal_of(installer="another"), same, said.append)

    # A machine whose kernel publishes no boot id: the rest still has to match,
    # and the session cannot be compared either way.
    without = {**same, "session": ""}
    cli._refuse_a_different_run(journal_of(session=""), without, said.append)

    # A journal from a run that predates this carries on as it always did, and
    # says so rather than refusing.
    path.write_text(json.dumps({"event": "operation", "position": 0}) + "\n")
    cli._refuse_a_different_run(Journal(path=path), same, said.append)
    assert said and "records no identity" in said[-1], said


def test_a_resume_checks_the_newest_journal_attempt(tmp_path: Path) -> None:
    from gentoo_install.errors import ResumeRefused
    from gentoo_install.log import Journal

    path = tmp_path / "install.jsonl"
    earlier = {
        "configuration": "first",
        "session": "boot",
        "installer": "tree",
    }
    resumed = {
        "configuration": "second",
        "session": "boot",
        "installer": "tree",
    }
    journal = Journal(path=path)
    journal.started(**earlier)
    Journal(path=path).started(**resumed)

    cli._refuse_a_different_run(Journal(path=path), resumed, lambda line: None)
    with pytest.raises(ResumeRefused, match="different configuration"):
        cli._refuse_a_different_run(Journal(path=path), earlier, lambda line: None)


def test_every_refusal_names_a_field_the_identity_carries() -> None:
    """A refusal for a field the journal does not record is one no run can
    trigger, and a field with no refusal is one no run is refused for."""
    from gentoo_install import cli
    from gentoo_install.log import Journal

    assert set(cli._RESUME_REFUSALS) == set(Journal.IDENTITY)


def test_the_identity_of_a_run_is_what_would_change_its_plan() -> None:
    """Two configurations that differ anywhere have to differ here, or a
    resume accepts a journal written for the other one."""
    from dataclasses import replace as _replace

    from gentoo_install import cli
    from gentoo_install.log import Journal

    from .layouts import config

    first = config()
    identity = cli._run_identity(first)
    assert set(identity) == set(Journal.IDENTITY), identity
    assert len(identity["configuration"]) == 64
    assert len(identity["installer"]) == 64
    assert cli._run_identity(first) == identity, "the same run answers the same"

    other = _replace(first, system=_replace(first.system, hostname="somewhere-else"))
    assert cli._run_identity(other)["configuration"] != identity["configuration"]
    # The other two are the machine's and the installer's, not the file's.
    assert cli._run_identity(other)["session"] == identity["session"]
    assert cli._run_identity(other)["installer"] == identity["installer"]


def test_the_installer_digest_reads_the_source_it_is_running_from() -> None:
    """A digest that ignored a file would call two installers the same, and
    the file most likely to differ is the one somebody edited on the medium."""
    import hashlib
    from pathlib import Path as _Path

    from gentoo_install import cli

    root = _Path(cli.__file__).resolve().parent
    files = sorted(root.rglob("*.py"))
    assert len(files) > 30, files

    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    assert cli.installer_digest() == digest.hexdigest()


def test_a_refused_menu_configuration_returns_to_the_menu(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal reached the operator as a line on a dead terminal.

    One whose partition sizes came to exactly the size of the disk was dropped
    to a shell with twenty rows filled in and no way back to the one that was
    wrong.
    """
    walked: list[str] = []

    def menu(arguments: object, refused: str = "", state: object = None) -> None:
        walked.append(refused)
        if len(walked) == 1:
            raise errors.PreflightFailed("disk1-table claims 40GiB, which does not fit")
        return None

    interactive_stdin(monkeypatch)
    monkeypatch.setattr(cli, "_from_menu", menu)
    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)
    assert cli.main(["--lang", "en"]) == cli.EXIT_ABORTED
    assert walked == ["", "disk1-table claims 40GiB, which does not fit"], walked

    # Negative control: the same refusal from a configuration file has no menu
    # to go back to, so it stays an exit code and a line on stderr.
    del walked[:]
    monkeypatch.setattr(cli, "load_source", _refusing)
    assert cli.main(["--config", "any.toml"]) == cli.EXIT_PREFLIGHT
    assert walked == [], walked
    assert "preflight:" in capsys.readouterr().err


def _refusing(source: object) -> None:
    raise errors.PreflightFailed("a file's configuration is refused the same way")


def test_the_same_refusal_twice_stops_rather_than_asking_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A walk that did not change what was wrong must not be offered again.

    The retry exists so one wrong row does not cost nineteen right ones; it is
    not a reason to ask the same question for ever.
    """
    walked: list[str] = []

    def menu(arguments: object, refused: str = "", state: object = None) -> None:
        walked.append(refused)
        raise errors.PreflightFailed("the same thing is still wrong")

    interactive_stdin(monkeypatch)
    monkeypatch.setattr(cli, "_from_menu", menu)
    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)
    assert cli.main(["--lang", "en"]) == cli.EXIT_PREFLIGHT
    # Twice: once with nothing to say, once with the reason. Not a third time.
    assert walked == ["", "the same thing is still wrong"], walked
    assert "preflight:" in capsys.readouterr().err


def test_a_refused_configuration_from_the_menu_returns_to_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ZFS mirror built with one member ended a session on `no node with
    id ''`, which is not a sentence an operator can act on and not a reason to
    lose the other nineteen rows."""
    walked: list[str] = []

    def menu(arguments: object, refused: str = "", state: object = None) -> None:
        walked.append(refused)
        if len(walked) == 1:
            raise errors.UnknownDeviceId("no node with id ''")
        return None

    interactive_stdin(monkeypatch)
    monkeypatch.setattr(cli, "_from_menu", menu)
    monkeypatch.setattr(cli, "_require_root", lambda arguments: None)
    monkeypatch.setattr(cli, "_needs_network", lambda arguments: False)
    assert cli.main(["--lang", "en"]) == cli.EXIT_ABORTED
    assert walked == ["", "no node with id ''"], walked

    # Negative control: the same refusal from a configuration file has no menu
    # to go back to, so it stays an exit code and a line on stderr.
    del walked[:]
    monkeypatch.setattr(cli, "load_source", _refusing_config)
    assert cli.main(["--config", "any.toml"]) == cli.EXIT_CONFIG
    assert walked == [], walked
    assert "configuration:" in capsys.readouterr().err


def _refusing_config(source: object) -> None:
    raise errors.UnknownDeviceId("a file's configuration is refused the same way")


#: Every named error and the exit code an operator reads from it. `cli.py`
#: ends its chain with a clause for `GentooInstallError` itself, so an error
#: added without a decision would take 4 in silence; this table is where the
#: decision is recorded, and the test below drives the real chain to check it.
EXIT_FOR_ERROR: Final[dict[str, int]] = {
    # 1: the answers are wrong, and nothing has reached a disk.
    "ConfigError": cli.EXIT_CONFIG,
    "DeviceCycle": cli.EXIT_CONFIG,
    "DuplicateDeviceId": cli.EXIT_CONFIG,
    "InvalidLayout": cli.EXIT_CONFIG,
    "InvalidSize": cli.EXIT_CONFIG,
    "UnalignedSize": cli.EXIT_CONFIG,
    "UnknownDeviceId": cli.EXIT_CONFIG,
    "ValidationFailed": cli.EXIT_CONFIG,
    # `--resume` names a journal this tree and session cannot continue, which
    # is the invocation being wrong rather than a command failing.
    "ResumeRefused": cli.EXIT_CONFIG,
    # 2: the machine is not fit to start, and the operator can change that.
    "PreflightFailed": cli.EXIT_PREFLIGHT,
    "ConversionUnsupported": cli.EXIT_PREFLIGHT,
    "DeviceNotFound": cli.EXIT_PREFLIGHT,
    "WorkDirectoryBusy": cli.EXIT_PREFLIGHT,
    # 3: something arrived and cannot be trusted.
    "IntegrityError": cli.EXIT_INTEGRITY,
    "ArchiveDigestMismatch": cli.EXIT_INTEGRITY,
    # 4: something was attempted and did not finish.
    "CommandFailed": cli.EXIT_COMMAND,
    "DownloadFailed": cli.EXIT_COMMAND,
    "ConversionFailed": cli.EXIT_COMMAND,
    "LocaleMissing": cli.EXIT_COMMAND,
    "NothingToBoot": cli.EXIT_COMMAND,
    "TargetEscape": cli.EXIT_COMMAND,
    "UploadFailed": cli.EXIT_COMMAND,
    "GentooInstallError": cli.EXIT_COMMAND,
}


def test_every_named_error_has_an_exit_code_somebody_chose(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Raised through `main` rather than read off the source: the chain's order
    decides which clause catches a subclass, and reading the clauses cannot
    show that."""
    from gentoo_install import cli as command_line
    from gentoo_install import errors

    named = {
        name: value
        for name, value in vars(errors).items()
        if isinstance(value, type) and issubclass(value, errors.GentooInstallError)
    }
    assert set(named) == set(EXIT_FOR_ERROR), set(named) ^ set(EXIT_FOR_ERROR)

    for name, error in sorted(named.items()):
        def raising(*_: object, **__: object) -> None:
            raise error(f"{name} for the exit code test")

        monkeypatch.setattr(command_line, "build", raising)
        code = main(["--config", str(FIXTURES / "btrfs-luks.toml"), "--dry-run"])
        capsys.readouterr()
        assert code == EXIT_FOR_ERROR[name], (name, code, EXIT_FOR_ERROR[name])


def test_a_dry_run_reads_a_capacity_only_when_a_share_needs_one() -> None:
    """A share is a share of the disk, so a dry run that cannot read the
    capacity prints a different number from the one the install writes. A
    layout of absolute sizes needs none, and reading one anyway made every dry
    run depend on evidence it does not use: the first version asked a probe
    double for `disk_bytes` and `ext4-bios` exited 4."""
    import tomllib

    from gentoo_install.model.device import asks_for_a_share
    from gentoo_install.model.parse import parse

    plain = load(FIXTURES / "ext4-bios.toml")
    assert not asks_for_a_share(plain.disk.graph)

    base = (FIXTURES / "vm-xfs.toml").read_text()
    anchor = 'kind = "partition"\nid = "rootpart"\ntable = "table"\nindex = 2\nrole = "data"'
    assert base.count(anchor) == 1
    shared = parse(tomllib.loads(base.replace(anchor, f'{anchor}\nsize = "40%"')))
    assert asks_for_a_share(shared.disk.graph)

    # `rest` is not a share of anything: it is whatever is left, which needs
    # no capacity to describe, so it must not drag a probe in behind it.
    rested = parse(tomllib.loads(base.replace(anchor, f'{anchor}\nsize = "rest"')))
    assert not asks_for_a_share(rested.disk.graph)


def test_a_resume_with_no_journal_refuses_instead_of_partitioning(tmp_path: Path) -> None:
    """`--resume` says it carries on "instead of partitioning the disk again".

    With nothing to resume the code started a fresh journal, left `finished`
    empty and applied the whole operation list -- partitioning included. The
    work directory is a tmpfs, so the ordinary way to arrive here is a reboot,
    which is exactly when an operator believes a resume is what they asked
    for.
    """
    import pytest as _pytest

    from gentoo_install import cli
    from gentoo_install.errors import ResumeRefused

    # A journal with entries carries on, which is the whole point of the flag.
    cli._refuse_a_resume_with_no_journal(tmp_path, True)

    with _pytest.raises(ResumeRefused) as refused:
        cli._refuse_a_resume_with_no_journal(tmp_path / "run", False)
    said = str(refused.value)
    # Both halves, because the operator's next move depends on which is wrong:
    # the flag they passed, and the directory that turned out to be empty.
    assert "--resume" in said, said
    assert str(tmp_path / "run") in said, said
    assert "partition" in said, said


def test_a_console_a_person_drives_is_not_an_unattended_run() -> None:
    """`--no-shell` says the closing root shell is unwanted, not that nobody is
    here, and the driver CD adds it to every run. Both meanings on one flag
    left `tests/tui/session.py` unable to open the menu at all: it answered
    `an unattended run needs --config FILE` on a console an operator drives a
    key at a time."""
    import argparse

    from gentoo_install import cli

    def parsed(*argv: str) -> argparse.Namespace:
        return cli.parser().parse_args(list(argv))

    # What the driver CD hands a session, which is the case that was refused.
    assert not cli._unattended(parsed("--no-shell", "--menu", "--lang", "zh-TW"))
    # What it hands a fixture, which must stay unattended: the closing offer of
    # a root shell would sit on a serial console nobody is reading.
    assert cli._unattended(parsed("--no-shell", "--config", "x.toml"))


def test_a_menu_promised_to_a_pipe_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--menu` asserts a person is driving. A pipe is where that is false, and
    curses would draw to nobody. Refused outside the walk: a preflight refusal
    with no `--config` is carried back into the menu as a reason to answer
    differently, and no answer turns a pipe into a terminal."""
    import io

    from gentoo_install import cli

    class NotATerminal(io.StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", NotATerminal())
    code = cli.main(["--menu"])
    said = capsys.readouterr()
    assert code == cli.EXIT_PREFLIGHT, (code, said)
    assert "--menu" in said.err, said.err


def test_every_session_invocation_asks_for_the_menu() -> None:
    """The refusal above fires on any `install.sh` run with no `--config`, so a
    session invocation that forgets `--menu` cannot open the interface at all.

    The interpolation is resolved before the check: an earlier version matched
    the constant's name, which is present whatever the constant holds, and the
    control that emptied it stayed green.
    """
    import re
    from pathlib import Path

    from tests.vm import cluster

    source = Path(cluster.__file__).read_text(encoding="utf-8")
    invocations = [
        found.replace("{DRIVEN_BY_A_PERSON}", cluster.DRIVEN_BY_A_PERSON)
        for found in re.findall(r'"[^"]*install\.sh ([^"]*)"', source)
    ]
    assert invocations, source[:200]
    driven = [one for one in invocations if "--config" not in one]
    # The two session entry points, so a walk that stops matching them cannot
    # leave this passing with nothing checked.
    assert len(driven) == 2, driven
    for arguments in driven:
        assert "--menu" in arguments, arguments


def test_the_evidence_for_a_live_medium_is_a_token_not_a_sentence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`live_medium()` is carried as a `Refusal.detail`, which is not translated.

    The menu draws `translate(reason) + " (" + detail + ")"`, so a sentence
    here put English inside a translated line: a guest running the interface
    in Traditional Chinese drew a translated refusal ending in `(the kernel
    command line carries rd.live.)`. The reason already says what happened;
    the detail says which marker, and a marker is the same word in every
    language.
    """
    from gentoo_install.exec import probe as probe_module

    class Said:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.returncode = 0

    class Runner:
        def __init__(self, root: str) -> None:
            self.root = root

        def run(self, argv: list[str], check: bool = True) -> Said:
            del argv, check
            return Said(self.root)

    cmdline = tmp_path / "cmdline"
    monkeypatch.setattr(probe_module, "CMDLINE", cmdline)

    cmdline.write_text("BOOT_IMAGE=/vmlinuz rd.live.image quiet")
    from_cmdline = _probe_with(probe_module, Runner(""), tmp_path).live_medium()
    assert from_cmdline == "rd.live.", from_cmdline

    cmdline.write_text("BOOT_IMAGE=/vmlinuz root=/dev/vda2 ro")
    from_root = _probe_with(probe_module, Runner("overlay"), tmp_path).live_medium()
    assert from_root == "overlay", from_root

    # The rule, rather than the two values above: a producer that grows a
    # sentence again is what this is here to refuse.
    for evidence in (from_cmdline, from_root):
        assert " " not in evidence, evidence


def _probe_with(probe_module: object, runner: object, work: Path) -> RealProbe:
    """A `Probe` built the way `cli.py` builds one, with a fake runner."""
    made = getattr(probe_module, "Probe")
    return cast(RealProbe, made(cast(Any, runner), work))


def test_the_kernel_is_quiet_while_the_menu_is_drawn(tmp_path: Path) -> None:
    """On a serial console the kernel and the menu share one screen.

    A guest drew `[ 3915.800938] clocksource: Watchdog remote CPU 1 read ti`
    across a panel, which is unreadable and indistinguishable from a broken
    installer. Only the first field of `/proc/sys/kernel/printk` decides what
    reaches the console, and it is put back afterwards.
    """
    from gentoo_install.exec.console import WHILE_DRAWING, kernel_messages_held

    printk = tmp_path / "printk"
    printk.write_text("7\t4\t1\t7\n")
    with kernel_messages_held(printk):
        assert printk.read_text().split()[0] == WHILE_DRAWING, printk.read_text()
    assert printk.read_text().split()[0] == "7", printk.read_text()

    # Restored when the walk raises, or a failed install leaves the machine
    # silent about everything that follows.
    with pytest.raises(RuntimeError):
        with kernel_messages_held(printk):
            raise RuntimeError("the walk stopped")
    assert printk.read_text().split()[0] == "7", printk.read_text()

    # A machine with no procfs still gets a menu: the file is absent here and
    # the block runs anyway.
    ran = False
    with kernel_messages_held(tmp_path / "absent"):
        ran = True
    assert ran


def test_the_menu_walk_is_wrapped_in_the_quiet(tmp_path: Path) -> None:
    """A helper nothing calls leaves every serial console as noisy as before."""
    import ast
    import inspect
    import textwrap

    from gentoo_install import cli

    source = textwrap.dedent(inspect.getsource(cli._from_menu))
    called = {
        getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }
    assert "kernel_messages_held" in called, sorted(called)


def test_a_mirror_of_the_region_opens_the_menu_when_the_default_one_is_blocked(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine in China reaches USTC and not `distfiles.gentoo.org`, and
    refusing there stopped an install the first mirror row would have run."""
    interactive_stdin(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    online(monkeypatch, False, said="HTTP Error 503")
    monkeypatch.setattr(fetch, "egress_country", lambda *a, **k: "CN")
    monkeypatch.setattr(
        fetch,
        "mirror_online",
        lambda mirror, *a, **k: mirror == "https://mirrors.ustc.edu.cn/gentoo",
    )

    assert cli._a_mirror_that_answers() == "https://mirrors.ustc.edu.cn/gentoo"
    cli._say_what_the_menu_will_not_have()
    assert "mirrors.ustc.edu.cn" in capsys.readouterr().err


def test_no_mirror_answering_says_so_and_still_opens_the_menu(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """It refused here, and `mode = "dd"` is chosen inside the menu it closed.

    That mode streams a prepared image from a local path and fetches nothing,
    so a machine with the image and no network could not reach the one option
    it could have run. What an install does need is the mirror its own
    configuration names, and `_require_mirror` reads that after the menu.
    """
    online(monkeypatch, False, said="HTTP Error 503")
    monkeypatch.setattr(fetch, "egress_country", lambda *a, **k: "CN")
    monkeypatch.setattr(fetch, "mirror_online", lambda *a, **k: False)

    cli._say_what_the_menu_will_not_have()

    said = capsys.readouterr().err
    assert "nor any mirror" in said, said
    assert "HTTP Error 503" in said, said


def test_a_shell_that_never_started_is_not_recorded_as_one_that_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both lines were written around an unchecked `chroot`, so a target whose
    `/bin/bash` is not there recorded a shell that opened and exited."""
    import argparse
    from types import SimpleNamespace

    from gentoo_install.exec.apply import Machine
    from gentoo_install.exec.runner import Result

    interactive_stdin(monkeypatch)
    monkeypatch.setattr(cli, "_asked", lambda question, translate=None: True)

    def run_a_shell(code: int) -> list[str]:
        said: list[str] = []
        runner = SimpleNamespace(
            run=lambda argv, **kwargs: Result(
                argv=tuple(argv), returncode=code, stdout="", stderr="", seconds=0.0
            )
        )
        cli._offer_a_shell(
            argparse.Namespace(no_shell=False, menu=False, target=Path("/mnt/gentoo"), lang=""),
            cast(Machine, SimpleNamespace(runner=runner)),
            said.append,
            False,
            Path("/mnt/gentoo"),
            DiskMode.PARTITION,
            Catalog("en"),
        )
        return said

    refused = run_a_shell(127)
    assert any("no root shell could be started" in one for one in refused), refused
    assert not any("was opened" in one for one in refused), refused

    # A shell the operator ended with a non-zero status still opened.
    ordinary = run_a_shell(1)
    assert any("was opened" in one for one in ordinary), ordinary
    assert any("the shell exited" in one for one in ordinary), ordinary


def test_a_system_clock_that_could_not_follow_the_rtc_is_reported(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `--set` failure is warned about because TLS then refuses every
    mirror. `--hctosys` failing leaves exactly that state, and its result was
    discarded."""
    import time

    from gentoo_install.exec.runner import Result, Runner

    # A stamp two days out, so the correction runs at all.
    monkeypatch.setattr(fetch, "network_time", lambda: time.time() + 172800)

    def running(self: Runner, argv: Sequence[str], **kwargs: object) -> Result:
        failed = "--hctosys" in argv
        return Result(
            argv=tuple(argv),
            returncode=1 if failed else 0,
            stdout="hwclock: cannot set the system clock" if failed else "",
            stderr="",
            seconds=0.0,
        )

    monkeypatch.setattr(Runner, "run", running)
    cli._check_the_clock()
    said = capsys.readouterr().err
    assert "system clock could not be set from the corrected RTC" in said, said


def test_the_flag_help_says_what_each_flag_actually_controls() -> None:
    """`--no-shell` reads as the closing shell alone, and `_unattended` reads
    it for every question; `--menu` said it opens the interface, and `_once`
    opens it only when there is no `--config` to apply. An operator who reads
    the help and passes `--config x --menu` gets no interface."""
    import argparse
    import inspect

    parser = cli.parser()
    help_for = {
        action.option_strings[0]: (action.help or "")
        for action in parser._actions
        if action.option_strings
    }

    # `--no-shell` reaches `_unattended`, so it decides more than the shell.
    assert "_unattended" in inspect.getsource(cli._confirmed_swap)
    assert "no_shell" in inspect.getsource(cli._unattended)
    assert "no question" in help_for["--no-shell"], help_for["--no-shell"]

    # `--menu` opens the interface only without `--config`.
    opening = inspect.getsource(cli._once)
    assert "arguments.config is None" in opening
    assert "without --config" in help_for["--menu"], help_for["--menu"]

    quiet = argparse.Namespace(menu=False, no_shell=True)
    driven = argparse.Namespace(menu=True, no_shell=True)
    assert cli._unattended(quiet)
    assert not cli._unattended(driven)

def test_a_conversion_that_replaced_usr_is_not_told_nothing_was_written() -> None:
    """`operation_started` read `Stage.PARTITION` alone.

    `plan/convert.py` declares `Stage.STAGE3`, `Stage.SYSTEM`,
    `Stage.BOOTLOADER` and `Stage.FINISH` and never partitions, so a
    conversion that failed after replacing `/usr` and `/etc` printed `nothing
    was written to the selected disk`. A run that reuses a layout formats
    without partitioning and read the same way.
    """
    from dataclasses import Field

    from gentoo_install.plan import convert
    from gentoo_install.plan.operations import Operation, Stage

    def _at_stage(stage: Stage) -> Operation:
        class One(Operation):
            def describe(self) -> str:
                return "one"

            def apply(self, context: object) -> None:
                return None

        return One(stage=stage)

    declared = {
        one.__dataclass_fields__["stage"].default
        for one in vars(convert).values()
        if isinstance(one, type)
        and issubclass(one, Operation)
        and one is not Operation
        and isinstance(one.__dataclass_fields__.get("stage"), Field)
        and isinstance(one.__dataclass_fields__["stage"].default, Stage)
    }
    assert declared, "the conversion declares no stage at all"
    assert Stage.PARTITION not in declared, declared

    for stage in declared:
        state = cli.RunState()
        state.operation_started(_at_stage(stage))
        assert state.disk_was_written, stage

    # Preflight is the one stage that writes nothing, and it is what the
    # menu's return path reads to keep nineteen answers the operator gave.
    quiet = cli.RunState()
    quiet.operation_started(_at_stage(Stage.PREFLIGHT))
    assert not quiet.disk_was_written


def test_the_dry_run_help_promises_only_what_a_dry_run_keeps() -> None:
    """It said a dry run exits `without touching anything`, and it can set the RTC.

    A dry run with no configuration opens the menu, which reads versions over
    HTTPS, so `_needs_network` is true and `_check_the_clock` runs before the
    read: a machine more than a day out has `hwclock --set` and
    `hwclock --hctosys` run on it. Both facts are read off the code here, so
    the day either of them changes this stops holding the promise to the
    weaker wording.
    """
    import inspect

    arguments = cli.parser().parse_args(["--dry-run"])
    assert arguments.config is None
    assert cli._needs_network(arguments), "a dry run with no config reads the network"
    assert "hwclock" in inspect.getsource(cli._check_the_clock)

    said = next(
        one.help for one in cli.parser()._actions if "--dry-run" in one.option_strings
    )
    assert said is not None
    assert "anything" not in said, said
    assert "without applying any of them" in said, said


def test_a_translated_question_takes_the_answer_its_reader_would_type() -> None:
    """The question was translated and `y`/`yes` was all it accepted.

    An operator reading the Chinese offer of a root shell had to answer in
    English or lose the mounted target. Each catalog's own affirmative is read
    here rather than written twice, and `y` and `yes` stay accepted in every
    language because the hint printed beside the question names them.
    """
    from gentoo_install.i18n import Catalog

    # The catalog key, as `cli` writes it at the call.
    AFFIRMATIVE = "y|yes"
    assert cli._affirmatives(None) == frozenset({"y", "yes"})

    for tag in ("zh-TW", "zh-CN", "ja", "ko"):
        catalog = Catalog(tag)
        said = catalog(AFFIRMATIVE)
        assert said != AFFIRMATIVE, f"{tag} has no affirmative of its own"
        taken = cli._affirmatives(catalog)
        assert {"y", "yes"} <= taken, (tag, sorted(taken))
        for word in said.split("|"):
            assert word.strip().lower() in taken, (tag, word)


def _recorded(seen: list[list[str]], argv: object) -> CommandOutput:
    """Record the command and answer the way the real runner does."""
    seen.append([str(one) for one in cast(Sequence[str], argv)])
    return CommandOutput("", 0)


def test_the_closing_questions_take_the_answer_they_asked_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Both closing questions reach `_asked`, and only one of them was bound.

    `_offer_a_shell` calls it directly and `offer_paste` is handed it as a
    callback, so a catalog passed to the first alone leaves the second asking
    in Chinese and taking English. Driven through both callers rather than
    through `_asked`, because what is being checked is who passes what.
    """
    import argparse
    import io
    from types import SimpleNamespace

    from gentoo_install.exec import report
    from gentoo_install.exec.apply import Machine
    from gentoo_install.i18n import Catalog
    from gentoo_install.exec import fetch
    from gentoo_install.exec.report import RunFile

    catalog = Catalog("zh-TW")
    yes = catalog("y|yes").split("|")[-1]
    assert not yes.isascii(), yes

    monkeypatch.setattr(cli, "_unattended", lambda arguments: False)
    monkeypatch.setattr(cli, "_forget_what_was_typed", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{yes}\n"))

    ran: list[list[str]] = []
    machine = cast(
        Machine,
        SimpleNamespace(
            runner=SimpleNamespace(
                run=lambda argv, **k: _recorded(ran, argv)
            )
        ),
    )
    cli._offer_a_shell(
        argparse.Namespace(no_shell=False, menu=True, target=tmp_path, lang=""),
        machine,
        lambda one: None,
        False,
        tmp_path,
        DiskMode.PARTITION,
        catalog,
    )
    assert ran and ran[0][0] == "chroot", ran

    # The callback half: the log is published rather than left behind.
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{yes}\n"))
    said: list[str] = []
    (tmp_path / RunFile.LOG.value).write_text("a log\n", encoding="utf-8")
    monkeypatch.setattr(fetch, "upload", lambda *a, **k: "https://example/1")
    report.offer_paste(
        tmp_path,
        said.append,
        False,
        False,
        lambda question: cli._asked(question, catalog),
        lambda one: None,
        catalog,
    )
    assert not any("by hand" in one for one in said), said


def _no_way_in() -> Any:
    """A configuration whose only login is a root hash, with that hash gone."""
    started = load(FIXTURES / "ext4-bios.toml")
    return replace(started, system=replace(started.system, root_password_hash=""))


def _driven(*, no_shell: bool = False, menu: bool = False) -> argparse.Namespace:
    return argparse.Namespace(no_shell=no_shell, menu=menu)


def test_a_configuration_with_no_login_is_left_alone_off_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question added to an unattended run is a way for it to hang, so the
    prompt exists only where somebody can answer it; `validate` still refuses.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        getpass, "getpass", lambda _: pytest.fail("asked with no terminal")
    )
    locked = _no_way_in()
    assert cli._with_a_root_password_if_asked(locked, _driven()) is locked


def test_a_configuration_with_no_login_is_asked_for_one_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One configuration, many machines: `--config` already takes a URL, so the
    password is the only thing that has to differ per machine."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_forget_what_was_typed", lambda: None)
    answers = iter(["hunter2hunter2", "hunter2hunter2"])
    monkeypatch.setattr(getpass, "getpass", lambda _: next(answers))

    filled = cli._with_a_root_password_if_asked(_no_way_in(), _driven())
    assert filled.system.root_password_hash.startswith("$6$")


def test_a_mistyped_confirmation_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_forget_what_was_typed", lambda: None)
    answers = iter(["one", "other", "same", "same"])
    monkeypatch.setattr(getpass, "getpass", lambda _: next(answers))

    filled = cli._with_a_root_password_if_asked(_no_way_in(), _driven())
    assert filled.system.root_password_hash.startswith("$6$")


def test_asking_stops_rather_than_repeating_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """A person mistypes; something answering wrongly forever is what the
    ceiling stops."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_forget_what_was_typed", lambda: None)
    monkeypatch.setattr(getpass, "getpass", lambda _: "")

    with pytest.raises(errors.ValidationFailed, match="no root password"):
        cli._with_a_root_password_if_asked(_no_way_in(), _driven())


def test_an_encrypted_paste_is_not_asked_about_under_no_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same guard as the root password: `--no-shell` is a real terminal
    with nobody at it, so the refusal has to stand instead of a prompt."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        getpass, "getpass", lambda _: pytest.fail("asked under --no-shell")
    )

    def refuse(source: str, password: str | None = None) -> None:
        raise ConfigError("this is an encrypted paste")

    monkeypatch.setattr(cli, "load_source", refuse)
    with pytest.raises(ConfigError, match="encrypted paste"):
        cli._configuration_from("https://paste.example/x", _driven(no_shell=True))


def test_no_shell_on_a_terminal_is_still_unattended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver CD passes `--no-shell` on a real terminal, so a question
    guarded on `isatty` alone would have sat there for the whole run."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        getpass, "getpass", lambda _: pytest.fail("asked under --no-shell")
    )
    locked = _no_way_in()
    assert cli._with_a_root_password_if_asked(locked, _driven(no_shell=True)) is locked


def test_a_configuration_that_can_already_log_in_is_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        getpass, "getpass", lambda _: pytest.fail("asked with a password already set")
    )
    started = load(FIXTURES / "ext4-bios.toml")
    assert cli._with_a_root_password_if_asked(started, _driven()) is started
