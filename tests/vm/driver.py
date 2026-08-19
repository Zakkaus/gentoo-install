# SPDX-License-Identifier: GPL-2.0-or-later
"""Package the working tree into a second CD the guest can run.

The installer is not on the install medium, so every run builds this from the
tree as it is right now. Copying files into the guest over the serial console
would work and takes minutes; an ISO takes a second and cannot go stale.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import hashlib
import time
from pathlib import Path
from typing import Final, Protocol

from .media import MediaError

REPOSITORY = Path(__file__).resolve().parents[2]
LABEL = "GENTOO-INSTALL"

#: How the guest finds the driver CD. It is the second one when an install
#: medium is booted and the only one when a guest boots from its own disk.
#: One line, because a caller sends it down a serial console as one command,
#: and the exit status is `mountpoint`'s answer, not the last failed mount's.
#: The label first and the device nodes after it: the medium is found by what
#: it is rather than by where this run happened to attach it. Measured on the
#: Debian genericcloud image, which builds no ATA or AHCI driver at all and so
#: has no `/dev/sr*` for a CD to appear at.
FIND_DRIVER = (
    "mkdir -p /mnt/driver; mountpoint -q /mnt/driver || "
    f"for candidate in /dev/disk/by-label/{LABEL} /dev/sr1 /dev/sr0; do "
    'mount -o ro "$candidate" /mnt/driver 2>/dev/null && break; done; '
    "mountpoint -q /mnt/driver"
)

class DriverNotFound(RuntimeError):
    """The guest never saw the CD the harness attached."""


class Console(Protocol):
    """The two console calls this needs, so either runner's console fits."""

    def run(self, command: str, timeout: float = ...) -> None: ...

    def expect_command(self, command: str, timeout: float = ...) -> bytes: ...


#: How long the driver CD is waited for, and how often it is tried again. The
#: guest's ATAPI devices are not enumerated when its shell first answers: a
#: Debian cloud image under KVM reached a root prompt at 7.3 seconds and both
#: `/dev/sr0` and `/dev/sr1` answered `Can't open blockdev`, so the installer
#: was started with `sh /mnt/driver/install.sh` against nothing and `sh` exited
#: 2 for a file it could not open.
DRIVER_PATIENCE: Final[float] = 120.0
DRIVER_RETRY: Final[float] = 3.0


def wait_for_driver(console: Console, patience: float = DRIVER_PATIENCE) -> None:
    """Mount the driver CD, waiting for the guest to notice it has one."""
    deadline = time.monotonic() + patience
    while True:
        console.run(FIND_DRIVER, timeout=180.0)
        # `expect_command` answers with everything up to the marker, and the
        # shell echoes the line it was given, so a command naming both answers
        # carries both in its own echo. The value is computed, never typed.
        said = console.expect_command(
            "mountpoint -q /mnt/driver; echo driver=$?", timeout=60.0
        )
        if b"driver=0" in said:
            return
        if time.monotonic() >= deadline:
            raise DriverNotFound(
                f"no driver CD at /mnt/driver after {patience:.0f}s; the guest "
                f"answered {said!r}"
            )
        time.sleep(DRIVER_RETRY)


#: What the guest runs to put the installer on its PYTHONPATH.
ENTRY = f"""#!/bin/sh
# Mount the driver CD and run the installer from it.
set -e
{FIND_DRIVER}
cd /mnt/driver
# --no-shell: the harness drives a serial console, where stdin is a terminal,
# and the offer of a root shell would sit there waiting for an answer.
exec python3 -m gentoo_install --no-shell "$@"
"""

#: The name of the compressed payload on a packed CD, and where it is unpacked.
PAYLOAD = "driver.tar.gz"
UNPACKED = "/tmp/gentoo-install-driver"

#: The launcher on a packed CD. Uncompressed the tree is 1.6 MiB and the ISO
#: around it 1.4 MiB, which the cluster's ingress refuses with `413 Request
#: Entity Too Large`; compressed it is 236 KiB and the ISO fits under the
#: limit. Unpacked to tmpfs rather than run from the CD, because the installer
#: writes nothing there but Python still wants somewhere to put bytecode.
PACKED_ENTRY = f"""#!/bin/sh
set -e
mkdir -p {UNPACKED}
{FIND_DRIVER}
tar xzf /mnt/driver/{PAYLOAD} -C {UNPACKED}
cd {UNPACKED}
# Through the launcher, because that is the entry point an operator uses and
# every run should exercise it.
exec sh ./bootstrap.sh --no-shell "$@"
"""


def build(output: Path, packed: bool = False, fixtures: Path | None = None) -> Path:
    """Write an ISO holding the installer package and return its path.

    `packed` puts the tree in a tarball instead of loose files. A local run
    mounts the CD directly; a run on the cluster has to upload it through an
    ingress that refuses anything near a megabyte.

    `fixtures` replaces the configurations the CD carries, which is how the
    cluster runs the same set against a different mirror region.
    """
    if shutil.which("xorriso") is None:
        raise MediaError("xorriso is not installed, so the driver CD cannot be built")
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".driver-", dir=output.parent))
    source = scratch / "source"
    image = source
    try:
        shutil.copytree(
            REPOSITORY / "gentoo_install",
            source / "gentoo_install",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copytree(fixtures or REPOSITORY / "tests" / "fixtures", source / "fixtures")
        # The launcher is what an operator runs, so the CD carries the same one.
        shutil.copy2(REPOSITORY / "bootstrap.sh", source / "bootstrap.sh")
        entry = source / "install.sh"
        entry.write_text(ENTRY)
        entry.chmod(0o755)

        if packed:
            image = scratch / "image"
            image.mkdir()
            payload = image / PAYLOAD
            packing = subprocess.run(
                ["tar", "czf", str(payload), "-C", str(source), "."],
                capture_output=True,
                text=True,
            )
            if packing.returncode != 0:
                raise MediaError(f"the driver payload was not packed: {packing.stderr.strip()}")
            entry = image / "install.sh"
            entry.write_text(PACKED_ENTRY)
            entry.chmod(0o755)

        output.unlink(missing_ok=True)
        result = subprocess.run(
            [
                "xorriso", "-as", "mkisofs",
                "-volid", LABEL,
                "-output", str(output),
                "-quiet",
                str(image),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise MediaError(f"xorriso failed: {result.stderr.strip()}")
        return output
    finally:
        shutil.rmtree(scratch)


def digest(path: Path) -> str:
    """SHA-256 identity of a completed driver image."""
    reader = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            reader.update(block)
    return reader.hexdigest()


#: What every driver CD's name begins with, so a reader can tell one from the
#: node's other ISOs and a caller can recover the digest from the name.
NAME_PREFIX: Final[str] = "gi-driver-"


def remote_name(path: Path) -> str:
    """Content-addressed name used on node-local ISO storage."""
    return f"{NAME_PREFIX}{digest(path)[:20]}.iso"
