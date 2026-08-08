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
from .widgets import Confirm, Item, Menu, Outcome, Screen


@dataclass(frozen=True)
class Finished:
    """What the operator ended with, and whether they meant to."""

    config: InstallConfig | None

    @property
    def cancelled(self) -> bool:
        return self.config is None


def run(screen: Screen, start: InstallConfig, context: Context) -> Finished:
    current = start
    #: Kept across redraws: coming back to the top after every edit makes the
    #: operator hunt for where they were.
    cursor = 0
    while True:
        blocked = _blocked(current, context)
        items: list[Item[int]] = [
            Item(
                label=context.translate(setting.label),
                value=index,
                detail=setting.value(current, context),
                disabled_because="" if setting.edit else "detected from this machine",
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
            cursor=cursor,
            footer="  ".join(
                (
                    f"[enter] {context.translate('Continue')}",
                    f"[q] {context.translate('Cancel')}",
                )
            ),
        )
        answer = menu.run(screen)
        cursor = menu.cursor
        if not answer.chosen:
            # Asked rather than obeyed: one stray escape should not throw away
            # every answer the operator has entered.
            leaving = Confirm(
                title=context.translate("Leave without installing?"),
                footer=context.translate("Cancel"),
            ).run(screen)
            if leaving.chosen and leaving.unwrap():
                return Finished(None)
            continue
        chosen = answer.unwrap()[0]
        if chosen == len(SETTINGS):
            return Finished(current)
        editor = SETTINGS[chosen].edit
        if editor is None:
            continue
        edited = editor(screen, current, context)
        if edited.outcome is Outcome.CANCELLED:
            return Finished(None)
        if edited.chosen:
            current = edited.unwrap()


def _blocked(config: InstallConfig, context: Context) -> str:
    """Why the install cannot start, in the row that would start it."""
    missing = [context.translate(label) for label in unanswered(config, context)]
    if missing:
        return f"{', '.join(missing)}: {context.translate('still needs an answer')}"
    try:
        validate(config)
    except ValidationFailed as error:
        return str(error).splitlines()[-1].strip()
    return ""
