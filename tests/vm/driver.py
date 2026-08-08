"""Package the working tree into a second CD the guest can run.

The installer is not on the install medium, so every run builds this from the
tree as it is right now. Copying files into the guest over the serial console
would work and takes minutes; an ISO takes a second and cannot go stale.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .media import MediaError

REPOSITORY = Path(__file__).resolve().parents[2]
LABEL = "GENTOO-INSTALL"

#: What the guest runs to put the installer on its PYTHONPATH. `/dev/sr1` is
#: mounted read-only, so nothing is unpacked and nothing is written back.
ENTRY = """#!/bin/sh
# Mount the driver CD and run the installer from it.
set -e
mkdir -p /mnt/driver
mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver
cd /mnt/driver
exec python3 -m gentoo_install "$@"
"""


def build(output: Path) -> Path:
    """Write an ISO holding the installer package and return its path."""
    if shutil.which("xorriso") is None:
        raise MediaError("xorriso is not installed, so the driver CD cannot be built")
    staging = output.parent / "driver"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(
        REPOSITORY / "gentoo_install",
        staging / "gentoo_install",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("fixtures",):
        shutil.copytree(REPOSITORY / "tests" / name, staging / name)
    # The launcher is what an operator runs, so the CD carries the same one.
    shutil.copy2(REPOSITORY / "bootstrap.sh", staging / "bootstrap.sh")
    entry = staging / "install.sh"
    entry.write_text(ENTRY)
    entry.chmod(0o755)

    output.unlink(missing_ok=True)
    result = subprocess.run(
        [
            "xorriso", "-as", "mkisofs",
            "-volid", LABEL,
            "-output", str(output),
            "-quiet",
            str(staging),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MediaError(f"xorriso failed: {result.stderr.strip()}")
    shutil.rmtree(staging)
    return output
