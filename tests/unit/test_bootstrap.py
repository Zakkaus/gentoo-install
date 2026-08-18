# SPDX-License-Identifier: GPL-2.0-or-later
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


def run(
    tmp_path: Path, release: str, *arguments: str, path: str
) -> subprocess.CompletedProcess[str]:
    """Drive the launcher with another distribution's /etc/os-release."""
    where = tmp_path / "os-release"
    where.write_text(release)
    return subprocess.run(
        [SHELL, str(LAUNCHER), *arguments],
        cwd=REPOSITORY,
        env={"OS_RELEASE": str(where), "PATH": path},
        capture_output=True,
        text=True,
    )


def output(finished: subprocess.CompletedProcess[str]) -> str:
    return finished.stdout + finished.stderr


@pytest.mark.parametrize("argument", ["--help", "-h"])
@pytest.mark.parametrize("installer_status", [0, 23])
def test_help_bypasses_installation_preflight_and_preserves_status(
    tmp_path: Path, argument: str, installer_status: int
) -> None:
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    trace = tmp_path / "trace"
    python = helpers / "python3"
    python.write_text(
        """#!/bin/sh
printf 'python %s\\n' "$*" >> "$TRACE"
case "$1" in
-c)
    case "$2" in
    *version_info\\[1\\]*) printf '11\\n' ;;
    *version_info\\[0\\]*) printf '3\\n' ;;
    esac
    ;;
--version) printf 'Python 3.11.0\\n' ;;
-m)
    case "$3" in
    --missing-commands) printf 'sgdisk\\n' ;;
    -h | --help) printf 'installer help\\n'; exit "$INSTALLER_STATUS" ;;
    esac
    ;;
esac
"""
    )
    python.chmod(0o755)
    identity = helpers / "id"
    identity.write_text(
        """#!/bin/sh
printf 'id %s\\n' "$*" >> "$TRACE"
printf '1000\\n'
"""
    )
    identity.chmod(0o755)
    release = tmp_path / "os-release"
    release.write_text("ID=gentoo\n")

    finished = subprocess.run(
        [SHELL, str(LAUNCHER), argument],
        cwd=REPOSITORY,
        env={
            "INSTALLER_STATUS": str(installer_status),
            "OS_RELEASE": str(release),
            "PATH": str(helpers),
            "TRACE": str(trace),
        },
        capture_output=True,
        text=True,
    )

    calls = trace.read_text().splitlines()
    assert finished.returncode == installer_status
    assert finished.stdout == "installer help\n"
    assert f"python -m gentoo_install {argument}" in calls
    assert not any("--missing-commands" in call for call in calls)
    assert not any(call.startswith("id ") for call in calls)


@pytest.mark.parametrize("release,manager", FAMILIES)
def test_each_live_system_gets_its_own_package_manager(
    tmp_path: Path, release: str, manager: str
) -> None:
    """With no python on PATH the launcher still has to say how to get one."""
    finished = run(tmp_path, release, path="/nonexistent")
    said = output(finished)
    assert finished.returncode == 1
    assert "needs python 3.11 or newer" in said
    assert f"{manager} python3" in said


def test_a_broken_path_still_reaches_the_message(tmp_path: Path) -> None:
    """`dirname` is on PATH too, so finding the script's own directory must not
    need it: the operator would otherwise get a shell error and no reason."""
    said = output(run(tmp_path, "ID=debian\n", path="/nonexistent"))
    assert "command not found" not in said
    assert "needs python 3.11" in said


def test_a_missing_tool_is_named_as_the_package_of_that_distribution(tmp_path: Path) -> None:
    """The command is the same everywhere and the package is not: `sgdisk`
    comes from gdisk on Debian and from gptfdisk everywhere else."""
    arguments = ("--config", "tests/fixtures/vm-luks.toml")
    lean = only_python(tmp_path)
    debian = output(run(tmp_path, "ID=debian\n", *arguments, path=lean))
    arch = output(run(tmp_path, "ID=arch\n", *arguments, path=lean))
    assert "missing commands:" in debian
    assert "apt-get install -y" in debian and "gdisk" in debian
    assert "pacman" in arch and "gptfdisk" in arch


def test_a_package_is_never_named_twice(tmp_path: Path) -> None:
    """`btrfs` and `mkfs.btrfs` both come from btrfs-progs, and naming it twice
    reads as though it has to be installed twice."""
    said = output(run(
        tmp_path,
        "ID=arch\n",
        "--config",
        "tests/fixtures/vm-luks.toml",
        path=only_python(tmp_path),
    ))
    listed = [line for line in said.splitlines() if line.startswith("run: ")]
    assert listed, said
    packages = listed[0].removeprefix("run: ").split()
    assert len(packages) == len(set(packages))


def test_a_dry_run_needs_none_of_the_tools(tmp_path: Path) -> None:
    """It performs nothing, and refusing it on a machine without them takes
    away the one way to check a file before reaching the target. Found by
    running the launcher on the Live ISO, which ships no lvm."""
    finished = run(
        tmp_path,
        "ID=gentoo\n",
        "--config",
        "tests/fixtures/vm-lvm.toml",
        "--dry-run",
        path=only_python(tmp_path),
    )
    said = output(finished)
    assert finished.returncode == 0
    assert "missing commands:" not in said


def test_an_install_still_names_what_is_missing(tmp_path: Path) -> None:
    finished = run(
        tmp_path,
        "ID=gentoo\n",
        "--config",
        "tests/fixtures/vm-lvm.toml",
        path=only_python(tmp_path),
    )
    said = output(finished)
    assert finished.returncode == 1
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
        said = output(run(tmp_path, release, *arguments, path=only_python(tmp_path)))
        assert "gdisk" in said and "gptfdisk" not in said


def test_alpine_names_each_util_linux_tool_on_its_own(tmp_path: Path) -> None:
    """Alpine's `util-linux` package is a 1.5 kB placeholder that installs
    nothing; the tools are one package each, so `apk add util-linux` left the
    same commands missing and the operator looping on the same message."""
    said = output(
        run(
            tmp_path,
            "ID=alpine\n",
            "--config",
            "tests/fixtures/vm-luks.toml",
            path=only_python(tmp_path),
        )
    )
    assert "apk add" in said
    for named in ("lsblk", "findmnt", "blkid"):
        assert named in said, named
    # The placeholder must not be what it asks for.
    assert " util-linux " not in said and not said.rstrip().endswith(" util-linux")


def test_no_distribution_is_told_to_install_a_package_named_chroot(tmp_path: Path) -> None:
    """`chroot` and `hostid` are coreutils everywhere; no distribution ships a
    package under either name."""
    for release in ("ID=alpine\n", "ID=arch\n", "ID=fedora\n", "ID=debian\n"):
        said = output(
            run(
                tmp_path,
                release,
                "--config",
                "tests/fixtures/vm-zfs.toml",
                path=only_python(tmp_path),
            )
        )
        # The install line only: the line above it lists the missing commands,
        # where the word `chroot` belongs.
        line = next(one for one in said.splitlines() if one.startswith("run: "))
        assert "chroot" not in line, release
        assert "coreutils" in line, release


#: The provider of each storage command per distribution family, from the
#: distributions' own file lists. The splits are not guessable: Debian keeps
#: `swapoff` in `mount`, Fedora keeps both swap tools in `util-linux-core`,
#: and Alpine keeps them in `util-linux-misc`.
PROVIDERS = {
    "debian": {"lvm": "lvm2", "mkswap": "util-linux", "swapoff": "mount"},
    "ubuntu": {"lvm": "lvm2", "mkswap": "util-linux", "swapoff": "mount"},
    "arch": {"lvm": "lvm2", "mkswap": "util-linux", "swapoff": "util-linux"},
    "opensuse": {"lvm": "lvm2", "mkswap": "util-linux", "swapoff": "util-linux"},
    "fedora": {"lvm": "lvm2", "mkswap": "util-linux-core", "swapoff": "util-linux-core"},
    "rhel": {"lvm": "lvm2", "mkswap": "util-linux", "swapoff": "util-linux"},
    "centos": {"lvm": "lvm2", "mkswap": "util-linux", "swapoff": "util-linux"},
    "gentoo": {"lvm": "lvm2", "mkswap": "util-linux", "swapoff": "util-linux"},
    "alpine": {"lvm": "lvm2", "mkswap": "util-linux-misc", "swapoff": "util-linux-misc"},
}


def test_every_storage_command_names_its_real_provider() -> None:
    """All five reached the fallback and were printed as package names, so an
    LVM or swap install was told to install `pvcreate` and `mkswap`."""
    import subprocess
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "bootstrap.sh").read_text()
    start = source.index("package_for()")
    script = source[start : source.index("\n}\n", start) + 3] + '\npackage_for "$1" "$2"\n'

    for family, wanted in PROVIDERS.items():
        for command in ("pvcreate", "vgcreate", "lvcreate"):
            said = subprocess.run(
                ["sh", "-c", script, "_", command, family], capture_output=True, text=True
            ).stdout.strip()
            assert said == wanted["lvm"], (command, family, said)
        for command in ("mkswap", "swapoff"):
            said = subprocess.run(
                ["sh", "-c", script, "_", command, family], capture_output=True, text=True
            ).stdout.strip()
            assert said == wanted[command], (command, family, said)


def test_arch_is_not_told_to_install_a_package_it_cannot_reach() -> None:
    """`zfs-utils` is in archzfs, a third-party repository a stock live image
    has not configured, so `pacman -S zfs-utils` answers `target not found`.
    Naming it reads as an instruction that works."""
    import subprocess
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "bootstrap.sh").read_text()
    start = source.index("package_for()")
    script = source[start : source.index("\n}\n", start) + 3] + '\npackage_for "$1" "$2"\n'

    for command in ("zpool", "zfs"):
        said = subprocess.run(
            ["sh", "-c", script, "_", command, "arch"], capture_output=True, text=True
        ).stdout.strip()
        assert said == "", (command, said)


def test_arch_is_told_what_it_cannot_supply_instead_of_a_failing_command(
    tmp_path: Path,
) -> None:
    """On a stock Arch image with nothing but python on PATH, the launcher
    names every missing command. The zfs pair has no official package, so the
    line for them says so rather than putting them in a `pacman` command that
    answers `target not found`."""
    said = output(run(
        tmp_path,
        "ID=arch\n",
        "--config",
        "tests/fixtures/vm-zfs.toml",
        path=only_python(tmp_path),
    ))
    assert "missing commands:" in said
    assert "this system has no package for:" in said
    for command in ("zpool", "zfs"):
        assert command in said.split("this system has no package for:")[1].splitlines()[0]
    installs = [line for line in said.splitlines() if line.startswith("run: pacman")]
    assert not any("zfs-utils" in line for line in installs), installs


def test_install_missing_runs_the_command_it_prints_and_checks_again(
    tmp_path: Path,
) -> None:
    """The memory environment has no second screen to ask at: the operator
    typed `install` once, and Alpine's netboot root arrives without `sgdisk`,
    `blkid` or an interpreter. Without the flag the launcher still only prints
    the command, because a live system is somebody else's machine."""
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "python3").symlink_to(PYTHON)
    installed = tmp_path / "installed"
    # An `apk` that reports what it was asked for and, from then on, makes the
    # installer answer that nothing is missing.
    (helpers / "apk").write_text(
        # A redirection, not `touch`: the PATH this case hands the launcher
        # holds python and this script and nothing else.
        f"#!/bin/sh\nprintf 'APK %s\\n' \"$*\"\n: > {installed}\n", encoding="utf-8"
    )
    (helpers / "apk").chmod(0o755)
    # The preflight's own answer, so the case does not depend on which tools
    # this workstation happens to have.
    module = tmp_path / "fake"
    (module / "gentoo_install").mkdir(parents=True)
    (module / "gentoo_install" / "__init__.py").write_text("", encoding="utf-8")
    (module / "gentoo_install" / "__main__.py").write_text(
        "import os, sys\n"
        f"print('' if os.path.exists({str(installed)!r}) else 'sgdisk blkid')\n",
        encoding="utf-8",
    )
    where = tmp_path / "os-release"
    where.write_text("ID=alpine\n")

    def launch(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [SHELL, str(LAUNCHER), *arguments],
            cwd=str(module),
            env={
                "OS_RELEASE": str(where),
                "PATH": str(helpers),
                "PYTHONPATH": str(module),
            },
            capture_output=True,
            text=True,
        )

    printed = launch("--config", "x.toml")
    assert printed.returncode == 1, output(printed)
    assert "run: apk add --update-cache sgdisk blkid" in output(printed), output(printed)
    assert "APK" not in output(printed), output(printed)
    assert not installed.exists()

    ran = launch("--install-missing", "--config", "x.toml")
    said = output(ran)
    assert "APK add --update-cache sgdisk blkid" in said, said
    assert installed.exists()
    # The flag is consumed here: the installer's own parser has no such option.
    assert "--install-missing" not in said.replace("run: ", ""), said
