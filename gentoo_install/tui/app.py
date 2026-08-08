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
from .screens import Context, answers, overview_screen
from .settings import SETTINGS, style_of, unanswered
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
                disabled_because=""
                if setting.edit
                else context.translate("detected from this machine"),
                style=style_of(setting, current, context),
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
            if _leaving(screen, context):
                return Finished(None)
            continue
        chosen = answer.unwrap()[0]
        if chosen == len(SETTINGS):
            # The operation list itself, then one confirmation: the row before
            # this one is the last chance to see what the disk is about to get.
            seen = overview_screen(screen, current, context)
            if seen.chosen:
                return Finished(seen.unwrap())
            if seen.outcome is Outcome.CANCELLED and _leaving(screen, context):
                return Finished(None)
            continue
        editor = SETTINGS[chosen].edit
        if editor is None:
            continue
        context.visited.add(SETTINGS[chosen].key)
        edited = editor(screen, current, context)
        if edited.outcome is Outcome.CANCELLED:
            if _leaving(screen, context):
                return Finished(None)
            # A grouped row hands back what was edited inside it, so staying
            # keeps those answers instead of dropping the whole group.
            if edited.value is not None:
                current = edited.value
            continue
        if edited.chosen:
            current = edited.unwrap()


def _leaving(screen: Screen, context: Context) -> bool:
    """Asked rather than obeyed, and asked wherever the escape came from: one
    stray key should not throw away every answer the operator has entered."""
    leaving = Confirm(
        **answers(context.translate),
        title=context.translate("Leave without installing?"),
        footer=context.translate("Cancel"),
    ).run(screen)
    return leaving.chosen and leaving.unwrap()


def _blocked(config: InstallConfig, context: Context) -> str:
    """Why the install cannot start, in the row that would start it.

    One row named and the rest counted: every name would run past 80 columns
    and the reason itself is what gets truncated away.
    """
    missing = [context.translate(label) for label in unanswered(config, context)]
    if missing:
        rest = f" +{len(missing) - 1}" if len(missing) > 1 else ""
        return f"{missing[0]}{rest}: {context.translate('still needs an answer')}"
    try:
        validate(config)
    except ValidationFailed as error:
        return str(error).splitlines()[-1].strip()
    return ""
