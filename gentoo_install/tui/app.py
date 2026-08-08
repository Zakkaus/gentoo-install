"""The screen stack: run each step, and let the operator go back through them.

Every answer is a whole `InstallConfig`, so going back is discarding the last
one rather than undoing an edit. Nothing here writes to a machine; the result is
handed to the same `plan.build` a configuration file goes through.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..model.config import InstallConfig
from .screens import STEPS, Context, Step
from .widgets import Outcome, Screen


@dataclass(frozen=True)
class Finished:
    """What the operator ended with, and whether they meant to."""

    config: InstallConfig | None

    @property
    def cancelled(self) -> bool:
        return self.config is None


def run(
    screen: Screen,
    start: InstallConfig,
    context: Context,
    steps: Sequence[Step] = STEPS,
) -> Finished:
    #: One entry per completed step, so going back restores exactly what that
    #: step was given rather than replaying the ones before it.
    history: list[InstallConfig] = [start]
    index = 0
    while index < len(steps):
        answer = steps[index](screen, history[index], context)
        if answer.outcome is Outcome.CANCELLED:
            return Finished(None)
        if answer.outcome is Outcome.BACK:
            if index == 0:
                return Finished(None)
            index -= 1
            del history[index + 1 :]
            continue
        history.append(answer.unwrap())
        index += 1
    return Finished(history[-1])
