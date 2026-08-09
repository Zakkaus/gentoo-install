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
mkdir -p /mnt/driver {UNPACKED}
mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver
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
    staging = output.parent / "driver"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(
        REPOSITORY / "gentoo_install",
        staging / "gentoo_install",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(fixtures or REPOSITORY / "tests" / "fixtures", staging / "fixtures")
    # The launcher is what an operator runs, so the CD carries the same one.
    shutil.copy2(REPOSITORY / "bootstrap.sh", staging / "bootstrap.sh")
    entry = staging / "install.sh"
    entry.write_text(ENTRY)
    entry.chmod(0o755)

    if packed:
        payload = output.parent / PAYLOAD
        packing = subprocess.run(
            ["tar", "czf", str(payload), "-C", str(staging), "."],
            capture_output=True,
            text=True,
        )
        if packing.returncode != 0:
            raise MediaError(f"the driver payload was not packed: {packing.stderr.strip()}")
        shutil.rmtree(staging)
        staging.mkdir(parents=True)
        shutil.move(str(payload), staging / PAYLOAD)
        entry = staging / "install.sh"
        entry.write_text(PACKED_ENTRY)
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
