# SPDX-License-Identifier: GPL-2.0-or-later
"""Hardware facts that select target packages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CpuVendor(Enum):
    AMD = "AuthenticAMD"
    INTEL = "GenuineIntel"
    UNKNOWN = ""


@dataclass(frozen=True)
class HardwareFacts:
    """Machine facts read before the operation list is derived."""

    cpu_vendor: CpuVendor = CpuVendor.UNKNOWN
    virtual_machine: bool | None = None

    @property
    def needs_intel_microcode(self) -> bool:
        return self.cpu_vendor is CpuVendor.INTEL and self.virtual_machine is False
