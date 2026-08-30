# SPDX-License-Identifier: GPL-2.0-or-later
"""What the Live ISO installs, checked against what the installer says it is."""

from __future__ import annotations

import configparser
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "packaging" / "launcher" / "gentoo-install"
ENTRY = ROOT / "packaging" / "launcher" / "gentoo-install.desktop"
PACKAGE = ROOT / "packaging" / "gig" / "app-admin" / "gentoo-install"
EBUILD = PACKAGE / "gentoo-install-9999.ebuild"


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
    assert running == ['exec "$here/../libexec/gentoo-install/bootstrap.sh" "$@"'], running
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR

    # Negative control: the memory launcher does carry one, so the rule above
    # is about this file rather than about the installer having no such option.
    from gentoo_install.plan.netboot import _command

    assert "--config" in _command()


def test_installed_launcher_stays_on_bootstrap_path(tmp_path: Path) -> None:
    """The installed launcher must keep both bootstrap checks and the caller's
    working directory."""
    prefix = tmp_path / "prefix"
    sbin = prefix / "sbin"
    libexec = prefix / "libexec" / "gentoo-install"
    sbin.mkdir(parents=True)
    libexec.mkdir(parents=True)
    installed_launcher = sbin / "gentoo-install"
    installed_bootstrap = libexec / "bootstrap.sh"
    shutil.copy2(LAUNCHER, installed_launcher)
    shutil.copy2(ROOT / "bootstrap.sh", installed_bootstrap)
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    python = shutil.which("python3") or "/usr/bin/python3"
    (helpers / "python3").symlink_to(python)
    release = tmp_path / "os-release"
    release.write_text("ID=alpine\n")
    operator = tmp_path / "operator"
    module = operator / "gentoo_install"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__main__.py").write_text(
        "from pathlib import Path\nprint(Path.cwd())\n", encoding="utf-8"
    )

    def launch(command: str | Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(command), "--dry-run"],
            cwd=operator,
            env={"OS_RELEASE": str(release), "PATH": f"{sbin}:{helpers}"},
            capture_output=True,
            text=True,
        )

    direct = launch(installed_bootstrap)
    assert direct.returncode == 0, direct.stderr
    assert direct.stdout == f"{operator}\n"

    launched = launch("gentoo-install")
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout == f"{operator}\n"
    assert "live system: alpine" in launched.stderr

    recipe = EBUILD.read_text()
    assert "exeinto /usr/local/libexec/gentoo-install" in recipe
    assert "doexe bootstrap.sh" in recipe


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


def released_ebuild() -> Path:
    """The one ebuild that names a tag rather than the branch."""
    found = [
        one
        for one in sorted(PACKAGE.glob("gentoo-install-*.ebuild"))
        if one != EBUILD
    ]
    assert len(found) == 1, [one.name for one in found]
    return found[0]


def test_the_released_ebuild_carries_the_version_pyproject_declares() -> None:
    """Two files name the version and an operator merges the ebuild, so a bump
    that moves one of them installs a tree that says it is something else."""
    import tomllib

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert released_ebuild().name == f"gentoo-install-{declared}.ebuild"
    assert f"/tags/v${{PV}}.tar.gz" in released_ebuild().read_text()


def test_both_ebuilds_install_the_same_tree() -> None:
    """The live ebuild is what the ISO builds from and the released one is what
    an operator merges. A file added to one and not the other ships a medium
    whose installer is not the installer that was released."""
    def installed(path: Path) -> str:
        text = path.read_text()
        return text[text.index("src_install() {") :]

    assert installed(released_ebuild()) == installed(EBUILD)


def test_only_the_released_ebuild_is_keyworded() -> None:
    """`git-r3` follows the branch, so a keyword on the live ebuild would offer
    an operator whatever `master` held that minute. The released one is
    `~amd64` and not stable, because `TESTED.md` records the boundary and
    nothing in it covers a second architecture."""
    assert 'KEYWORDS=""' in EBUILD.read_text()
    assert 'KEYWORDS="~amd64"' in released_ebuild().read_text()
    assert "git-r3" not in released_ebuild().read_text()
