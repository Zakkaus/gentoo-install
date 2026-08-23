# SPDX-License-Identifier: GPL-2.0-or-later
"""What each fixture's run has to produce, for both runners.

The two runners read different evidence for the same fact: `campaign.py` sees
`tests/vm/run.py`'s exit code and the console, `cluster.py` reads the
installer's own exit code and `install.jsonl`. Holding the answer in each of
them let `vm-proxy-dead` carry `1` in one table and `b"4"` in another, put a
third copy of `Connection refused` in the unit tests, and left the local runner
with no notion of a fixture that has to degrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

from gentoo_install.exec.apply import degradation_warning
from gentoo_install.plan.portage import BINARY_PACKAGES


@dataclass(frozen=True)
class Expectation:
    """One fixture's required result."""

    #: What `install.jsonl` must record the run giving up, empty when the run
    #: has to finish with nothing degraded.
    degrades: str = ""
    #: Text the console must carry, for a fixture whose evidence is not a
    #: degradation. A run whose log lacks it did not reach the path the
    #: fixture exists to measure.
    says: str = ""
    #: What `tests/vm/run.py` exits when the install stops as intended, or
    #: `None` when the fixture has to install and boot.
    runner_returncode: int | None = None
    #: What `cli.py` recorded as its own exit code, as the cluster collects it.
    installer_returncode: bytes = b""

    @property
    def must_stop(self) -> bool:
        return self.runner_returncode is not None

    @property
    def marker(self) -> str:
        """What a console reader looks for. Derived from `degrades` rather than
        written beside it, so the wording cannot drift from what `degrade()`
        writes."""
        if self.degrades:
            return degradation_warning(self.degrades, "")
        return self.says


EXPECTATIONS: Final[Mapping[str, Expectation]] = MappingProxyType(
    {
        # The proxy points at a port nothing listens on, so a run that reaches
        # the mirror proves something bypassed it.
        "vm-proxy-dead": Expectation(
            says="Connection refused", runner_returncode=1, installer_returncode=b"4"
        ),
        # The fixture's binary host answered 404 for its index on 2026-08-18.
        # The day it answers again this run stops covering the degradation and
        # becomes an ordinary binary package install, which no exit code shows.
        "vm-binhost-fallback": Expectation(degrades=BINARY_PACKAGES),
    }
)
