# SPDX-License-Identifier: GPL-2.0-or-later
"""Boot the installer on a guest with one address family and see what it does.

Three networks, one guest each: dual stack, IPv4 only, IPv6 only. The cluster
cannot produce the third — its guests all have a ULA address and NAT64 — so
this runs locally, where qemu's slirp backend can be told which families to
offer.

Three questions per guest, and each is something the installer decides on its
own before it touches a disk:

- egress: what `Probe.address_families()` says the machine has;
- startup: whether `bootstrap.sh --missing-commands` answers at all, which is
  the path a launcher takes before any network is needed;
- mirror: whether the installer's own downloader fetches the stage3 pointer
  file, which is what preflight checks and what the install then reads.

    python3 -m tests.vm.netcheck
    python3 -m tests.vm.netcheck --families ipv6
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .console import SerialConsole
from .driver import build as build_driver
from .workdir import WorkdirError, confined
from .media import MEDIA
from .qemu import Firmware, Vm, VmSpec

WORKROOT: Final[Path] = Path.home() / "code/gentoo-install/lab/vm/netcheck"

#: Read by the installer's own downloader, which is the point: `curl` answering
#: says nothing about what `urllib` does with the same name.
POINTER: Final[str] = (
    "https://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3-amd64-systemd.txt"
)

#: What each network is expected to give the guest, as `Probe` reports it.
EXPECTED: Final[dict[str, tuple[bool, bool]]] = {
    "dual": (True, True),
    "ipv4": (True, False),
    "ipv6": (False, True),
}


@dataclass
class Result:
    families: str
    egress: str = ""
    startup: str = ""
    mirror: str = ""

    @property
    def good(self) -> bool:
        return not (self.egress or self.startup or self.mirror)


#: How long the guest is given to configure the interface. The medium boots
#: with `nodhcp`, so nothing is configured until the live system's own client
#: runs, and asking before that reports a machine with no address at all.
CONFIGURED: Final[float] = 180.0


def _wait_for_an_address(console: SerialConsole, families: str) -> str:
    """Wait until the guest has a global address of the family it was given.

    The interface is brought up first. The medium boots with `nodhcp` and
    leaves it down with `accept_ra = 0`, so nothing is configured until
    something asks, which is what an operator does before installing.
    """
    console.run("ip link set enp0s2 up 2>/dev/null || true")
    console.run("dhcpcd -w -t 20 enp0s2 >/dev/null 2>&1 || true", timeout=90.0)
    wants4, wants6 = EXPECTED[families]
    wanted = "inet " if wants4 else "inet6 "
    deadline = time.monotonic() + CONFIGURED
    said = b""
    while time.monotonic() < deadline:
        said = console.expect_command(
            "ip -oneline address show scope global; echo ADDRESSES", timeout=60.0
        )
        if wanted.encode() in said:
            return ""
        time.sleep(5.0)
    return f"no global {wanted.strip()} address after {CONFIGURED:.0f}s: {said[-200:]!r}"


def _check(console: SerialConsole, families: str, driver: str) -> Result:
    found = Result(families=families)
    waited = _wait_for_an_address(console, families)
    if waited:
        found.egress = waited
        return found
    console.run("mkdir -p /mnt/driver")
    console.run("mountpoint -q /mnt/driver || mount -o ro /dev/sr1 /mnt/driver")
    console.run(f"mkdir -p {driver} && tar xzf /mnt/driver/driver.tar.gz -C {driver}")

    wants4, wants6 = EXPECTED[families]
    said = console.expect_command(
        f"cd {driver} && python3 -c \"from gentoo_install.exec.probe import Probe; "
        "from gentoo_install.exec.runner import Runner; from pathlib import Path; "
        "print('FAMILIES', Probe(runner=Runner(log=lambda l: None), "
        "work=Path('/tmp')).address_families())\"",
        timeout=120.0,
    )
    if f"FAMILIES ({wants4}, {wants6})" not in said.decode("utf-8", "replace"):
        found.egress = f"expected ({wants4}, {wants6}), console said {said[-200:]!r}"

    said = console.expect_command(
        f"sh {driver}/bootstrap.sh --config fixtures/vm-binpkg.toml --missing-commands "
        "> /tmp/missing 2>&1; echo RC=$?",
        timeout=300.0,
    )
    if b"RC=0" not in said:
        found.startup = f"the launcher did not answer: {said[-200:]!r}"

    # The count, not a bare marker: the command carries `MIRROR_BYTES` in its
    # own text and a shell echoes what it was given, so a check for the word
    # alone passed on a machine that fetched nothing.
    said = console.expect_command(
        f"cd {driver} && python3 -c \"from gentoo_install.exec import fetch; "
        f"print('MIRROR_BYTES=%d' % len(fetch._read('{POINTER}')))\" 2>&1",
        timeout=300.0,
    )
    if not re.search(rb"MIRROR_BYTES=[1-9][0-9]*", said):
        found.mirror = f"the pointer file was not fetched: {said[-300:]!r}"
    return found


def run(families: str, workdir: Path) -> Result:
    workdir = confined(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    medium = MEDIA["official-minimal"]
    driver_iso = build_driver(workdir / "driver.iso", packed=True)
    spec = VmSpec(
        medium=medium,
        workdir=workdir / families,
        firmware=Firmware.UEFI,
        memory="4G",
        cpus=2,
        driver_iso=driver_iso,
        families=families,
    )
    with Vm(spec) as machine:
        with SerialConsole.connect(machine.serial_socket, machine.serial_log) as console:
            console.expect(medium.root_prompt, timeout=600.0)
            return _check(console, families, "/tmp/driver")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--families",
        action="append",
        choices=sorted(EXPECTED),
        help="which network to give the guest; repeatable, all three by default",
    )
    parser.add_argument("--workdir", type=Path, default=WORKROOT)
    args = parser.parse_args(argv)

    try:
        workdir = confined(args.workdir)
    except WorkdirError as error:
        print(error, file=sys.stderr)
        return 1

    wanted = args.families or sorted(EXPECTED)
    results: list[Result] = []
    for families in wanted:
        started = time.monotonic()
        print(f"→ {families}", flush=True)
        found = run(families, workdir)
        results.append(found)
        mark = "ok  " if found.good else "FAIL"
        print(f"{mark} {families:5} {(time.monotonic() - started) / 60:4.1f}m", flush=True)
        for what, said in (
            ("egress", found.egress),
            ("startup", found.startup),
            ("mirror", found.mirror),
        ):
            if said:
                print(f"       {what}: {said}", flush=True)
    return 0 if all(one.good for one in results) else 1


if __name__ == "__main__":
    sys.exit(main())
