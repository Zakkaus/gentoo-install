"""The operation list as text.

A dry run prints this and a golden test compares it, so what a reviewer reads is
exactly what the run performs.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .operations import Operation, Stage


def render(operations: Sequence[Operation]) -> str:
    lines: list[str] = []
    for stage in Stage:
        in_stage = [operation for operation in operations if operation.stage is stage]
        if not in_stage:
            continue
        lines.append(f"[{stage.value}]")
        lines += [f"  {operation.describe()}" for operation in in_stage]
        lines.append("")
    return "\n".join(lines)


def counts(operations: Iterable[Operation]) -> dict[Stage, int]:
    """How many operations each stage holds, in the order they first appear."""
    counted: dict[Stage, int] = {}
    for operation in operations:
        counted[operation.stage] = counted.get(operation.stage, 0) + 1
    return counted


def summarise(operations: Iterable[Operation]) -> str:
    """The English form, for a log. The menu builds its own from `counts`:
    this layer has no catalog and the whole line was drawn untranslated."""
    counted = counts(operations)
    parts = [f"{stage.value} {count}" for stage, count in counted.items()]
    return f"{sum(counted.values())} operations: {', '.join(parts)}"
