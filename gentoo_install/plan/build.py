# SPDX-License-Identifier: GPL-2.0-or-later
"""One configuration in, one ordered operation list out.

Each module contributes the operations for what it owns; the order across them
is the stage order of `docs/design.md`, applied here rather than trusted to the
order the modules happen to be called in.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final, Sequence

from ..model import mirrors
from ..errors import ConversionUnsupported
from ..model.config import DiskMode, InstallConfig
from ..model.device import StorageFacts, StorageLayout
from ..model.validate import validate
from . import bootloader, convert, dd, disk, fonts, kernel, packages, portage, system
from .operations import Operation, Stage

#: The mirror a stage3 is fetched from when the configuration names none.
DEFAULT_MIRROR: Final[str] = "https://distfiles.gentoo.org"

#: What `variant_of` returns for the default profile and init system, and the
#: only variant a reachability check can name before the operator has answered
#: anything. Every mirror publishes it.
DEFAULT_VARIANT: Final[str] = "systemd"

# These operations alter the resolver's inputs, so the complete-set check has
# to see them before their package merges run in later stages.
PORTAGE_PREREQUISITES: Final[tuple[type[Operation], ...]] = (
    kernel.ConfigureInstallKernel,
    kernel.AcceptFirmwareLicence,
    kernel.ConfigureRemoteUnlock,
    kernel.UnmaskCjkDistKernel,
    kernel.AcceptKernelVersion,
    kernel.RequestCjkKernel,
    kernel.RequestDistKernelModules,
    kernel.VerifyZfsKernelCompatibility,
)


def stage3_mirror(config: InstallConfig, fallback: str = DEFAULT_MIRROR) -> str:
    """Where the stage3 comes from.

    The site the operator chose, which is the same one `GENTOO_MIRRORS` gets,
    unless that site carries no `releases/`: the region's first is used then,
    for the reason given at the line that does it.
    Only `--mirror` was read, so choosing USTC set the mirror for every later
    fetch and downloaded the several hundred megabytes of the stage3 itself
    from `distfiles.gentoo.org`, which is the slow one from China.

    An empty `site` means the region's first, which is what the field's own
    documentation says and what a configuration that names only a region
    holds. Reading it as `no choice was made` sent every region-only install
    back to `distfiles.gentoo.org`: a run from the cluster in China fetched
    the stage3 there with `cn` selected.

    Only sites that carry `releases/` are considered, whichever was chosen: an
    install told to use one that does not stopped with no stage3 at all.
    """
    # A replaced list is the whole answer, stage3 included: an operator who
    # points Portage at a caching mirror on the segment and still downloads
    # several hundred megabytes of stage3 from the public one has the slow half
    # of both. `emerge-webrsync` already reads snapshots from this list.
    replaced = config.portage.mirrors.distfiles
    if replaced:
        return replaced[0]
    chosen = config.portage.mirrors.site
    offered = [
        site for site in mirrors.gentoo_sites(config.portage.mirrors.region) if site.releases
    ]
    if not chosen:
        return offered[0].distfiles if offered else fallback
    for site in offered:
        if site.key == chosen:
            return site.distfiles
    # The site was chosen for its distfiles and does not carry `releases/`.
    # Falling back is the only way the stage3 arrives at all: xTom answers 404
    # for the pointer file and `ftp.twaren.net` answers 403 to this downloader.
    return offered[0].distfiles if offered else fallback


def running_config(config: InstallConfig, layout: StorageLayout | None) -> InstallConfig:
    """The configuration the operations act on, which is not always the one given.

    A conversion carries no device graph: the layout belongs to the machine and
    the graph is derived from it. Whatever resolves a `DeviceId` at apply time
    has to see that derived graph, and the `Machine` was handed the operator's
    own configuration — `write /etc/fstab` stopped twenty-four operations in
    with `no node with id 'running-root-device'`.
    """
    if config.disk.mode is not DiskMode.IN_PLACE:
        return config
    if layout is None:
        raise ConversionUnsupported("the running layout was not read")
    return replace(config, disk=convert.layout_graph(layout))


def build(
    config: InstallConfig,
    catalog: packages.Catalog,
    *,
    mirror: str = DEFAULT_MIRROR,
    storage_facts: StorageFacts | None = None,
    layout: StorageLayout | None = None,
    supports_v3: bool | None = None,
) -> tuple[Operation, ...]:
    """Validate, then derive the whole install. Nothing here touches a machine."""
    facts = storage_facts if storage_facts is not None else StorageFacts()
    if config.disk.mode is DiskMode.IN_PLACE:
        return _in_place(config, catalog, mirror, layout, supports_v3)
    validate(config, storage_facts=facts, supports_v3=supports_v3)
    if config.disk.mode is DiskMode.DD:
        return dd.build(config)
    chosen = packages.groups(config, catalog)
    operations: list[Operation] = [
        *disk.build(config, facts),
        *portage.build(
            config,
            stage3_mirror(config, mirror),
            packages._required_use(chosen),
            packages._required_video_cards(config, chosen),
        ),
        *system.build(config),
        *kernel.build(config),
        *bootloader.build(config),
        *packages._build(config, catalog, chosen),
        *fonts.build(config, catalog),
        *portage.finish(config),
        # Last of all: the keyword change above still has to reach make.conf in
        # the target, and nothing can be written once it is unmounted.
        *disk.finish(config),
    ]
    operations = [_in_portage_phase(operation) for operation in operations]
    ordered = sorted(operations, key=lambda operation: operation.stage.order)
    requests = _package_requests(ordered)
    if requests:
        after_configuration = max(
            index for index, operation in enumerate(ordered) if operation.stage is Stage.PORTAGE
        ) + 1
        ordered.insert(after_configuration, portage.VerifyPackages(requests=requests))
    return tuple(_told_about_the_binary_host(one, config) for one in ordered)


def _told_about_the_binary_host(operation: Operation, config: InstallConfig) -> Operation:
    """Every merge learns whether this run has a binary host.

    Set here rather than at each of the twenty-seven places an `Emerge` is
    built: a construction site that forgot it would fetch from a host the
    configuration turned off, which is the defect this exists to close.
    """
    if portage.uses_binhost(config.portage) or not isinstance(
        operation, (portage.Emerge, portage.VerifyPackages)
    ):
        return operation
    return replace(operation, binary_host=False)


#: The only bootloader operations that cannot be done before the swap: they
#: write to the esp or the boot sector of the machine as it will boot, and
#: `/boot` and the esp are not part of the swap. Emerging the package and
#: writing `/etc/default/grub` are ordinary staged work, and leaving them in
#: the irreversible window made it minutes long for no reason.
AFTER_THE_SWAP: Final[tuple[type[Operation], ...]] = (
    bootloader.InstallGrub,
    bootloader.InstallSystemdBoot,
    bootloader.ShowTheBootMenu,
)


def _in_place(
    config: InstallConfig,
    catalog: packages.Catalog,
    mirror: str,
    layout: StorageLayout | None,
    supports_v3: bool | None = None,
) -> tuple[Operation, ...]:
    """Derive the conversion of a running system, in the order it must run.

    Not the stage order the rest of the installer sorts by: everything that
    writes has to land in the staging root first, the swap comes after all of
    it, and the bootloader after the swap, because it writes to the root the
    machine will boot from and that is the old one until the swap happens.
    """
    if layout is None:
        raise ConversionUnsupported("the running layout was not read")
    # The operator's own configuration first, because that is where the rule
    # against writing a device graph beside the mode applies.
    validate(config, storage_facts=StorageFacts(), supports_v3=supports_v3)
    derived = running_config(config, layout)
    # And the derived one as an ordinary layout: it describes concrete devices
    # now, so the compatibility table has something to check.
    validate(
        replace(derived, disk=replace(derived.disk, mode=DiskMode.PARTITION)),
        storage_facts=StorageFacts(),
        supports_v3=supports_v3,
    )
    chosen = packages.groups(derived, catalog)
    boot = bootloader.build(derived)
    after_the_swap = tuple(one for one in boot if isinstance(one, AFTER_THE_SWAP))
    staged: list[Operation] = [
        convert.PrepareStaging(),
        *portage.build(
            derived,
            stage3_mirror(derived, mirror),
            packages._required_use(chosen),
            packages._required_video_cards(derived, chosen),
        ),
        *system.build(derived),
        # After `WriteFstab`, which is in the same stage and earlier in this
        # list: the file has to exist before its other mounts are appended.
        convert.CarryFstabEntries(lines=layout.carried_fstab),
        *kernel.build(derived),
        *(one for one in boot if not isinstance(one, AFTER_THE_SWAP)),
        *packages._build(derived, catalog, chosen),
        *fonts.build(derived, catalog),
        *portage.finish(derived),
    ]
    ordered = sorted(
        (_in_portage_phase(operation) for operation in staged),
        key=lambda operation: operation.stage.order,
    )
    requests = _package_requests(ordered)
    if requests:
        after_configuration = max(
            index for index, operation in enumerate(ordered) if operation.stage is Stage.PORTAGE
        ) + 1
        ordered.insert(after_configuration, portage.VerifyPackages(requests=requests))
    # `cli.py` runs every `Stage.FINISH` operation after the body, so those run
    # after the swap and the staging root they would be pointed at no longer has
    # a userland: a Fedora conversion stopped at `chroot /gentoo-install.new ln`
    # with `failed to run command 'ln': No such file or directory`, one
    # operation from the end and with the machine already converted. They are
    # placed here in the order they run rather than staged where they were.
    closing = tuple(one for one in ordered if one.stage is Stage.FINISH)
    return (
        *(
            convert.Staged(stage=operation.stage, inner=operation)
            for operation in ordered
            if operation.stage is not Stage.FINISH
        ),
        convert.SwapDirectories(),
        # Between the swap and the bootloader: `grub-mkconfig` reads `/boot`,
        # and until this runs what is there belongs to the old distribution.
        convert.PopulateBoot(),
        *after_the_swap,
        *closing,
        convert.LeaveStaging(),
        # Last, after the staging root is gone: everything above it wrote into
        # a filesystem that stays mounted, and nothing else will flush it.
        convert.FlushToDisk(),
    )


def _in_portage_phase(operation: Operation) -> Operation:
    if isinstance(operation, PORTAGE_PREREQUISITES):
        return replace(operation, stage=Stage.PORTAGE)
    return operation


def _package_requests(operations: Sequence[Operation]) -> tuple[portage.PackageRequest, ...]:
    requesters: dict[str, list[str]] = {}
    for operation in operations:
        if not isinstance(operation, portage.Emerge) or operation.repository_bootstrap:
            continue
        for atom in operation.packages:
            named = requesters.setdefault(atom, [])
            if operation.package_requester not in named:
                named.append(operation.package_requester)
    return tuple(
        portage.PackageRequest(atom=atom, requesters=tuple(named))
        for atom, named in requesters.items()
    )
