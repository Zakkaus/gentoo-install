"""The launcher runs before the installer does, on a system we did not build.

Every case here drives `bootstrap.sh` itself rather than a copy of its tables,
because a second copy of the mapping is what would drift.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
LAUNCHER = REPOSITORY / "bootstrap.sh"
#: Absolute, because the cases below hand the child a PATH that has no shell.
SHELL = shutil.which("sh") or "/bin/sh"
PYTHON = shutil.which("python3") or "/usr/bin/python3"

#: What each live system reports, and the manager the launcher has to pick.
FAMILIES = [
    ("ID=debian\n", "apt-get install -y"),
    ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "apt-get install -y"),
    ("ID=manjaro\nID_LIKE=arch\n", "pacman -Sy --needed --noconfirm"),
    ("ID=opensuse-tumbleweed\nID_LIKE=suse\n", "zypper --non-interactive install"),
    ("ID=fedora\n", "dnf install -y"),
    ('ID=rocky\nID_LIKE="rhel centos fedora"\n', "dnf install -y"),
    ("ID=gentoo\n", "emerge --noreplace"),
]


def only_python(tmp_path: Path) -> str:
    """A PATH holding python and nothing else, so the launcher reports every
    other command as missing whatever this machine happens to have."""
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    link = directory / "python3"
    if not link.exists():
        link.symlink_to(PYTHON)
    return str(directory)


def run(tmp_path: Path, release: str, *arguments: str, path: str) -> str:
    """Drive the launcher with another distribution's /etc/os-release."""
    where = tmp_path / "os-release"
    where.write_text(release)
    finished = subprocess.run(
        [SHELL, str(LAUNCHER), *arguments],
        cwd=REPOSITORY,
        env={"OS_RELEASE": str(where), "PATH": path},
        capture_output=True,
        text=True,
    )
    return finished.stdout + finished.stderr


@pytest.mark.parametrize("release,manager", FAMILIES)
def test_each_live_system_gets_its_own_package_manager(
    tmp_path: Path, release: str, manager: str
) -> None:
    """With no python on PATH the launcher still has to say how to get one."""
    said = run(tmp_path, release, path="/nonexistent")
    assert "needs python 3.11 or newer" in said
    assert f"{manager} python3" in said


def test_a_broken_path_still_reaches_the_message(tmp_path: Path) -> None:
    """`dirname` is on PATH too, so finding the script's own directory must not
    need it: the operator would otherwise get a shell error and no reason."""
    said = run(tmp_path, "ID=debian\n", path="/nonexistent")
    assert "command not found" not in said
    assert "needs python 3.11" in said


def test_a_missing_tool_is_named_as_the_package_of_that_distribution(tmp_path: Path) -> None:
    """The command is the same everywhere and the package is not: `sgdisk`
    comes from gdisk on Debian and from gptfdisk everywhere else."""
    arguments = ("--config", "tests/fixtures/vm-luks.toml")
    lean = only_python(tmp_path)
    debian = run(tmp_path, "ID=debian\n", *arguments, path=lean)
    arch = run(tmp_path, "ID=arch\n", *arguments, path=lean)
    assert "missing commands:" in debian
    assert "apt-get install -y" in debian and "gdisk" in debian
    assert "pacman" in arch and "gptfdisk" in arch


def test_a_package_is_never_named_twice(tmp_path: Path) -> None:
    """`btrfs` and `mkfs.btrfs` both come from btrfs-progs, and naming it twice
    reads as though it has to be installed twice."""
    said = run(
        tmp_path,
        "ID=arch\n",
        "--config",
        "tests/fixtures/vm-luks.toml",
        path=only_python(tmp_path),
    )
    listed = [line for line in said.splitlines() if line.startswith("run: ")]
    assert listed, said
    packages = listed[0].removeprefix("run: ").split()
    assert len(packages) == len(set(packages))


def test_a_dry_run_needs_none_of_the_tools(tmp_path: Path) -> None:
    """It performs nothing, and refusing it on a machine without them takes
    away the one way to check a file before reaching the target. Found by
    running the launcher on the Live ISO, which ships no lvm."""
    said = run(
        tmp_path,
        "ID=gentoo\n",
        "--config",
        "tests/fixtures/vm-lvm.toml",
        "--dry-run",
        path=only_python(tmp_path),
    )
    assert "missing commands:" not in said


def test_an_install_still_names_what_is_missing(tmp_path: Path) -> None:
    said = run(
        tmp_path,
        "ID=gentoo\n",
        "--config",
        "tests/fixtures/vm-lvm.toml",
        path=only_python(tmp_path),
    )
    assert "missing commands:" in said


def test_the_launcher_works_from_another_directory(tmp_path: Path) -> None:
    """`python -m gentoo_install` takes `sys.path[0]` from the current
    directory, so a launcher started by absolute path from elsewhere printed
    `No module named gentoo_install`, and the swallowed failure read as a
    machine with every tool already present."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    where = tmp_path / "os-release"
    where.write_text("ID=debian\n")
    finished = subprocess.run(
        [SHELL, str(LAUNCHER), "--config", str(REPOSITORY / "tests/fixtures/vm-luks.toml")],
        cwd=elsewhere,
        env={"OS_RELEASE": str(where), "PATH": only_python(tmp_path)},
        capture_output=True,
        text=True,
    )
    said = finished.stdout + finished.stderr
    assert "No module named" not in said
    assert "missing commands:" in said
    assert "apt-get install -y" in said


def test_fedora_gets_the_package_that_actually_ships_sgdisk(tmp_path: Path) -> None:
    """Fedora, RHEL and CentOS put `sgdisk` in gdisk and have no gptfdisk, so
    `dnf install -y gptfdisk` stopped on Unable to find a match and installed
    none of the other packages on the line either."""
    arguments = ("--config", "tests/fixtures/vm-luks.toml")
    for release in ("ID=fedora\n", 'ID=rocky\nID_LIKE="rhel centos fedora"\n'):
        said = run(tmp_path, release, *arguments, path=only_python(tmp_path))
        assert "gdisk" in said and "gptfdisk" not in said


def test_alpine_names_each_util_linux_tool_on_its_own(tmp_path: Path) -> None:
    """Alpine's `util-linux` package is a 1.5 kB placeholder that installs
    nothing; the tools are one package each, so `apk add util-linux` left the
    same commands missing and the operator looping on the same message."""
    said = run(tmp_path, "ID=alpine\n", "--config", "tests/fixtures/vm-luks.toml",
               path=only_python(tmp_path))
    assert "apk add" in said
    for named in ("lsblk", "findmnt", "blkid"):
        assert named in said, named
    # The placeholder must not be what it asks for.
    assert " util-linux " not in said and not said.rstrip().endswith(" util-linux")


def test_no_distribution_is_told_to_install_a_package_named_chroot(tmp_path: Path) -> None:
    """`chroot` and `hostid` are coreutils everywhere; no distribution ships a
    package under either name."""
    for release in ("ID=alpine\n", "ID=arch\n", "ID=fedora\n", "ID=debian\n"):
        said = run(tmp_path, release, "--config", "tests/fixtures/vm-zfs.toml",
                   path=only_python(tmp_path))
        # The install line only: the line above it lists the missing commands,
        # where the word `chroot` belongs.
        line = next(one for one in said.splitlines() if one.startswith("run: "))
        assert "chroot" not in line, release
        assert "coreutils" in line, release
