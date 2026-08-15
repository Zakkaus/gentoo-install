# SPDX-License-Identifier: GPL-2.0-or-later
"""Two logs: one to read, one to query.

`install.log` is the running commentary. `install.jsonl` is one JSON object per
line, which is what answers "why did this take four hours" afterwards: every
command with its exit code and duration, and every package with where it came
from. Portage prints `[binary]` or `[ebuild]` per package and exits 0 either
way, so a binary host that quietly went missing is invisible without this.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

#: How emerge announces each package it is about to merge.
_MERGE_LINE = re.compile(r"^\[(?P<kind>binary|ebuild)[^\]]*\]\s+(?P<atom>\S+)", re.MULTILINE)


class Source(Enum):
    BINARY = "binary"
    COMPILED = "compiled"


@dataclass
class Journal:
    """Append-only record of what a run did."""

    path: Path
    #: Kept in memory as well, so a caller can assert on a run without parsing.
    entries: list[dict[str, Any]] = field(default_factory=list)

    def write(self, kind: str, **fields: Any) -> None:
        entry: dict[str, Any] = {"event": kind, **fields}
        self.entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def command(self, argv: tuple[str, ...], returncode: int, seconds: float) -> None:
        self.write("command", argv=list(argv), returncode=returncode, seconds=round(seconds, 3))

    def operation(self, stage: str, described: str, seconds: float) -> None:
        self.write("operation", stage=stage, describe=described, seconds=round(seconds, 3))

    def packages(self, output: str) -> None:
        for atom, source in merged(output):
            self.write("package", atom=atom, source=source.value)

    def degraded(self, what: str, reason: str) -> None:
        """A path the install had to give up on, and why. Always recorded: a
        binary host that fell back to compiling is the difference between a
        ten minute install and a four hour one."""
        self.write("degraded", what=what, reason=reason)

    def replay(self) -> Iterator[dict[str, Any]]:
        """Every entry a previous run wrote, in order. A line that does not
        parse is skipped: a run killed mid-write leaves a partial last line."""
        try:
            text = self.path.read_text()
        except OSError:
            return
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                yield entry

    def counts(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for entry in self.entries:
            if entry["event"] == "package":
                found[entry["source"]] = found.get(entry["source"], 0) + 1
        return found


def merged(output: str) -> Iterator[tuple[str, Source]]:
    """Every package emerge said it was merging, and where it came from."""
    for found in _MERGE_LINE.finditer(output):
        kind = Source.BINARY if found.group("kind") == "binary" else Source.COMPILED
        yield found.group("atom"), kind
