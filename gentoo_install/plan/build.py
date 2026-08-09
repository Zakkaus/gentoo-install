"""One configuration in, one ordered operation list out.

Each module contributes the operations for what it owns; the order across them
is the stage order of `docs/design.md`, applied here rather than trusted to the
order the modules happen to be called in.
"""

from __future__ import annotations

from typing import Final

from ..model import mirrors
from ..model.config import InstallConfig
from ..model.validate import validate
from . import bootloader, disk, kernel, packages, portage, system
from .operations import Operation

#: The mirror a stage3 is fetched from when the configuration names none.
DEFAULT_MIRROR: Final[str] = "https://distfiles.gentoo.org"


def stage3_mirror(config: InstallConfig, fallback: str = DEFAULT_MIRROR) -> str:
    """Where the stage3 comes from.

    The site the operator chose, which is the same one `GENTOO_MIRRORS` gets.
    Only `--mirror` was read, so choosing USTC set the mirror for every later
    fetch and downloaded the several hundred megabytes of the stage3 itself
    from `distfiles.gentoo.org`, which is the slow one from China.

    An empty `site` means the region's first, which is what the field's own
    documentation says and what a configuration that names only a region
    holds. Reading it as `no choice was made` sent every region-only install
    back to `distfiles.gentoo.org`: a run from the cluster in China fetched
    the stage3 there with `cn` selected.
    """
    chosen = config.portage.mirrors.site
    offered = mirrors.gentoo_sites(config.portage.mirrors.region)
    if not chosen:
        return offered[0].distfiles if offered else fallback
    for site in offered:
        if site.key == chosen:
            return site.distfiles
    return fallback


def build(config: InstallConfig, catalog: packages.Catalog, *, mirror: str = DEFAULT_MIRROR) -> tuple[Operation, ...]:
    """Validate, then derive the whole install. Nothing here touches a machine."""
    validate(config)
    operations: list[Operation] = [
        *disk.build(config),
        *portage.build(
            config,
            stage3_mirror(config, mirror),
            packages.required_use(config, catalog),
            packages.required_video_cards(config, catalog),
            packages.required_licenses(config, catalog),
        ),
        *system.build(config),
        *kernel.build(config),
        *bootloader.build(config),
        *packages.build(config, catalog),
        *portage.finish(config),
        # Last of all: the keyword change above still has to reach make.conf in
        # the target, and nothing can be written once it is unmounted.
        *disk.finish(config),
    ]
    return tuple(sorted(operations, key=lambda operation: operation.stage.order))
