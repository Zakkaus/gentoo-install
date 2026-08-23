# SPDX-License-Identifier: GPL-2.0-or-later
"""Random configurations through `validate` and `build`.

`GentooInstallError` is an answer: a refusal the operator can read. Anything
else out of those two is a defect, because `cli.py` is the only place allowed
to turn a failure into an exit code and every other exception reaches the
operator as a traceback with their answers gone.

Seeds rather than a clock: a failure here has to be reproducible by number.
"""

from __future__ import annotations

import random

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.errors import GentooInstallError
from gentoo_install.model import manual, templates
from gentoo_install.model.compat import CJK_KERNELS
from gentoo_install.model.config import (
    Bootloader,
    BootloaderConfig,
    DiskConfig,
    Firmware,
    InitSystem,
    InstallConfig,
    KernelConfig,
    KernelSource,
    Overlay,
    PackagesConfig,
    PortageConfig,
    SystemConfig,
    User,
)
from gentoo_install.model.device import (
    FilesystemType,
    PartitionRole,
    RaidLevel,
    RaidMetadata,
    TableType,
    ZfsDataset,
    ZfsTopology,
)
from gentoo_install.model.size import Size
from gentoo_install.model.validate import validate
from gentoo_install.plan.build import build

DISKS = [f"/dev/disk/by-id/virtio-target{n}" for n in range(4)]
DESKTOPS = ("", "plasma", "gnome", "xfce", "console")
APPLICATIONS = ("fcitx5", "rime", "ibus", "pipewire", "bluetooth", "noto-cjk", "vim")
DRIVERS = ("intel", "amdgpu", "radeon", "nouveau", "nvidia", "virtual-machine")
WRITABLE = (FilesystemType.EXT4, FilesystemType.XFS, FilesystemType.BTRFS, FilesystemType.F2FS)

#: What the panel adds when zfs or a cjk kernel is chosen. Without it every
#: such configuration stops at the overlay rule and the plan is never reached.
GENTOOZH = Overlay(name="gentoo-zh", sync_uri="https://example.invalid/overlay.git")


def _template() -> DiskConfig:
    graph, root = templates.build(
        templates.Choice(
            disk=random.choice(DISKS),
            layout=random.choice(list(templates.Layout)[:3]),
            firmware=random.choice(list(Firmware)),
            filesystem=random.choice(WRITABLE),
            swap=random.choice((None, Size.parse("2GiB"))),
            passphrase_file=random.choice(("", "/run/pass")),
        )
    )
    return DiskConfig(graph=graph, root=root)


def _hand_written() -> DiskConfig:
    """Several disks, an array and a pool: what the templates cannot express."""
    disks = []
    for n in range(random.randint(1, 3)):
        disks.append(
            manual.Disk(
                selector=DISKS[n],
                table=random.choice(list(TableType)),
                slices=[
                    # One esp, on the first disk.
                    manual.Slice(
                        index=1,
                        role=PartitionRole.ESP,
                        size=Size.parse("512MiB"),
                        filesystem=FilesystemType.VFAT,
                        mountpoint="/efi" if n == 0 else "",
                    ),
                    manual.Slice(
                        index=2,
                        role=PartitionRole.DATA,
                        size=None,
                        filesystem=random.choice(WRITABLE),
                        mountpoint="/" if n == 0 else "",
                        passphrase_file=random.choice(("", "/run/pass")),
                    ),
                ],
            )
        )
    graph, root = manual.build(
        manual.Layout(
            disks=disks,
            array=manual.Array(
                level=random.choice(list(RaidLevel)),
                metadata=random.choice(list(RaidMetadata)),
                filesystem=random.choice((FilesystemType.EXT4, FilesystemType.XFS)),
                passphrase_file=random.choice(("", "/run/pass")),
            ),
            pool=random.choice(("rpool", "tank")),
            topology=random.choice(list(ZfsTopology)),
        )
    )
    return DiskConfig(graph=graph, root=root)


def a_configuration() -> InstallConfig:
    disk = random.choice((_template, _hand_written))()
    init = random.choice(list(InitSystem))
    kernel = random.choice(list(KernelSource))
    on_zfs = any(isinstance(one, ZfsDataset) for one in disk.graph.nodes.values())
    cjk = kernel in CJK_KERNELS
    boot = (
        Bootloader.ZFSBOOTMENU
        if on_zfs
        else random.choice((Bootloader.GRUB, Bootloader.SYSTEMD_BOOT))
    )
    return InstallConfig(
        disk=disk,
        system=SystemConfig(
            init=init,
            console_cjk=cjk and random.random() < 0.7,
            zram=random.choice((None, Size.parse("4GiB"))),
            sshd=random.choice((True, False)),
            users=random.choice(((), (User(name="zakk", sudo=True, password_hash="$6$t$x"),))),
            root_password_hash="$6$t$x",
        ),
        portage=PortageConfig(
            profile="default/linux/amd64/23.0"
            + ("/systemd" if init is InitSystem.SYSTEMD else ""),
            build_in_ram=random.choice((None, Size.parse("8GiB"))),
            overlays=(GENTOOZH,) if (on_zfs or cjk) else (),
        ),
        kernel=KernelConfig(source=kernel),
        bootloader=BootloaderConfig(
            kind=boot,
            firmware=Firmware.BIOS
            if boot is Bootloader.GRUB and random.random() < 0.3
            else Firmware.UEFI,
            kernel_params=random.choice(((), ("quiet",))),
        ),
        packages=PackagesConfig(
            desktop=random.choice(DESKTOPS),
            graphics=tuple(random.sample(DRIVERS, random.randint(0, 2))),
            display_manager=random.choice(("", "sddm", "gdm", "lightdm", "greetd")),
            applications=tuple(random.sample(APPLICATIONS, random.randint(0, 3))),
        ),
    )


@pytest.mark.parametrize("seed", range(300))
def test_a_random_configuration_is_planned_or_refused_and_never_crashes(seed: int) -> None:
    """Refusing is `validate`'s job alone.

    Both calls sat in one `try` and any `GentooInstallError` counted as a
    refusal, so a `build()` that raised `InvalidLayout` for every valid ZFS,
    LUKS or mdraid configuration was recorded as the rules working, and the
    ext4 seeds carried the hit count on their own.
    """
    from gentoo_install.plan.packages import driver_conflict, framework_conflict

    random.seed(seed)
    catalog = load_catalog()
    wanted = a_configuration()
    try:
        validate(wanted)
    except GentooInstallError:
        return
    try:
        operations = build(wanted, catalog)
    except GentooInstallError as refused:
        # `build` may refuse two catalog rules `validate` cannot see: they read
        # the package catalog, and the model layer does not. Any other refusal
        # of a configuration that validated is the finding, and one `try`
        # around both calls hid it.
        named = framework_conflict(wanted, catalog) or driver_conflict(wanted, catalog)
        assert named and named == str(refused), refused
        return
    assert operations, "a configuration that validated produced no operations"


def test_the_seeds_reach_the_plan_often_enough_to_mean_anything() -> None:
    """A generator that every rule refuses proves only that the rules refuse.

    Measured rather than assumed: 300 seeds put around a third through, and a
    change that drops it to none would leave the test above passing on nothing.
    """
    catalog = load_catalog()
    planned = 0
    for seed in range(300):
        random.seed(seed)
        wanted = a_configuration()
        try:
            validate(wanted)
            build(wanted, catalog)
        except GentooInstallError:
            continue
        planned += 1
    assert planned >= 60, planned
