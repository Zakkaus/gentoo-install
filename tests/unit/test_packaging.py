# SPDX-License-Identifier: GPL-2.0-or-later
"""What the Live ISO installs, checked against what the installer says it is."""

from __future__ import annotations

import configparser
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "packaging" / "launcher" / "gentoo-install"
ENTRY = ROOT / "packaging" / "launcher" / "gentoo-install.desktop"
EBUILD = ROOT / "packaging" / "gig" / "app-admin" / "gentoo-install" / "gentoo-install-9999.ebuild"


def test_the_command_the_banner_names_is_the_command_the_medium_has() -> None:
    """`plan/netboot.py` prints where the installer can be started from, and the
    Live ISO has to put it there. Two places naming the path is one place too
    many for them to agree by themselves."""
    from gentoo_install.plan.netboot import COMMAND

    desktop = configparser.ConfigParser(interpolation=None)
    desktop.read_string(ENTRY.read_text())
    assert desktop["Desktop Entry"]["Exec"] == COMMAND

    # The ebuild puts the launcher in the directory that path names.
    assert f"exeinto {COMMAND.rsplit('/', 1)[0]}" in EBUILD.read_text()

    # Negative control: the path is not the empty string, so the comparison
    # above cannot be satisfied by two things that were both never set.
    assert COMMAND.startswith("/") and COMMAND.endswith("gentoo-install")


def test_the_live_launcher_opens_the_menu() -> None:
    """The memory environment's launcher carries `--config`, because the file
    arrives with it. On a live desktop the operator wants the menu, and a
    launcher that answered from a file would install without asking."""
    # The line that runs, not the comment above it.
    running = [
        line for line in LAUNCHER.read_text().splitlines() if line.startswith("exec ")
    ]
    assert running == ['exec python3 -m gentoo_install "$@"'], running
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR

    # Negative control: the memory launcher does carry one, so the rule above
    # is about this file rather than about the installer having no such option.
    from gentoo_install.plan.netboot import _command

    assert "--config" in _command()


def test_the_desktop_entry_says_it_is_the_text_installer() -> None:
    """It appears beside the Calamares entry on the same desktop, and two rows
    both reading `Install System` tell the operator nothing about which is
    which."""
    desktop = configparser.ConfigParser(interpolation=None)
    desktop.read_string(ENTRY.read_text())
    section = desktop["Desktop Entry"]

    assert section["Type"] == "Application"
    assert section["Terminal"] == "true"
    assert "text" in section["Name"].lower()
    # Every language the interface offers, so no locale falls back to a name
    # that does not say which installer it is.
    for tag in ("zh_CN", "zh_TW", "ja", "ko"):
        assert f"Name[{tag}]" in section, tag
