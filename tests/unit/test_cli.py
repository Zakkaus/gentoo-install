from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_install.cli import EXIT_ABORTED, EXIT_CONFIG, EXIT_OK, main

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


def test_installing_for_real_is_not_implemented_and_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--config", str(FIXTURES / "ext4-bios.toml")])
    assert code == EXIT_ABORTED
    assert "untouched" in capsys.readouterr().err


def test_no_configuration_asks_for_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_CONFIG
    assert "--config" in capsys.readouterr().err
