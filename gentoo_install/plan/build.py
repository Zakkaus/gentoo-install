"""One configuration in, one ordered operation list out.

Each module contributes the operations for what it owns; the order across them
is the stage order of `docs/design.md`, applied here rather than trusted to the
order the modules happen to be called in.
"""

from __future__ import annotations

from ..model.config import InstallConfig
from ..model.validate import validate
from . import bootloader, disk, kernel, packages, portage, system
from .operations import Operation

#: The mirror a stage3 is fetched from when the configuration names none.
DEFAULT_MIRROR = "https://distfiles.gentoo.org"


def build(config: InstallConfig, catalog: packages.Catalog, *, mirror: str = DEFAULT_MIRROR) -> tuple[Operation, ...]:
    """Validate, then derive the whole install. Nothing here touches a machine."""
    validate(config)
    operations: list[Operation] = [
        *disk.build(config),
        *portage.build(config, mirror),
        *system.build(config),
        *kernel.build(config),
        *bootloader.build(config),
        *packages.build(config, catalog),
        *portage.finish(config),
    ]
    return tuple(sorted(operations, key=lambda operation: operation.stage.order))
