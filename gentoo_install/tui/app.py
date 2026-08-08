"""The main menu: every setting, its current value, and what edits it.

Not a wizard. The operator opens rows in any order and as often as they like,
and starts the install from the last row when nothing required is missing.
`archinstall` and `oddlama-gentoo-install` each arrived at this shape on their
own, because installing is a task people change their minds during.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model.config import InstallConfig
from ..model.validate import validate
from ..errors import ValidationFailed
from .screens import Context
from .settings import SETTINGS, unanswered
from .widgets import Item, Menu, Outcome, Screen


@dataclass(frozen=True)
class Finished:
    """What the operator ended with, and whether they meant to."""

    config: InstallConfig | None

    @property
    def cancelled(self) -> bool:
        return self.config is None


def run(screen: Screen, start: InstallConfig, context: Context) -> Finished:
    current = start
    while True:
        blocked = _blocked(current, context)
        items: list[Item[int]] = [
            Item(
                label=setting.label,
                value=index,
                detail=setting.value(current, context),
            )
            for index, setting in enumerate(SETTINGS)
        ]
        items.append(
            Item(
                label=context.translate("Install"),
                value=len(SETTINGS),
                disabled_because=blocked,
            )
        )
        menu: Menu[int] = Menu(
            title="gentoo-install",
            items=items,
            footer="  ".join(
                (
                    f"[enter] {context.translate('Continue')}",
                    f"[q] {context.translate('Cancel')}",
                )
            ),
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Finished(None)
        chosen = answer.unwrap()[0]
        if chosen == len(SETTINGS):
            return Finished(current)
        edited = SETTINGS[chosen].edit(screen, current, context)
        if edited.outcome is Outcome.CANCELLED:
            return Finished(None)
        if edited.chosen:
            current = edited.unwrap()


def _blocked(config: InstallConfig, context: Context) -> str:
    """Why the install cannot start, in the row that would start it."""
    missing = unanswered(config, context)
    if missing:
        return f"{', '.join(missing)} still needs an answer"
    try:
        validate(config)
    except ValidationFailed as error:
        return str(error).splitlines()[-1].strip()
    return ""
