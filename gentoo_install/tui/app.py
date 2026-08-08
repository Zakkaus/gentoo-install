"""The main menu: every setting, its current value, and what edits it.

Not a wizard. The operator opens rows in any order and as often as they like,
and starts the install from the last row when nothing required is missing.
`archinstall` and `oddlama-gentoo-install` each arrived at this shape on their
own, because installing is a task people change their minds during.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..model.config import InstallConfig
from ..model.validate import validate
from ..errors import GentooInstallError, ValidationFailed
from .screens import Context, overview_screen
from .settings import SETTINGS, UNSET, Setting, style_of, unanswered
from .widgets import Item, Menu, Outcome, Screen, TextField


#: The default name for a saved configuration, offered as the field's example.
SAVE_AS: Final[str] = "my-install.toml"


@dataclass(frozen=True)
class Finished:
    """What the operator ended with, and whether they meant to."""

    config: InstallConfig | None
    #: Where the configuration was written on the way out, for `cli.py` to
    #: print once curses has given the terminal back.
    saved: str = ""

    @property
    def cancelled(self) -> bool:
        return self.config is None


def run(screen: Screen, start: InstallConfig, context: Context) -> Finished:
    current = start
    #: Kept across redraws: coming back to the top after every edit makes the
    #: operator hunt for where they were.
    cursor = 0
    while True:
        # Before the rows are built: a grouped row fits its summary to this.
        context.columns = screen.size()[1]
        blocked = _blocked(current, context)
        items: list[Item[int]] = [
            Item(
                label=context.translate(setting.label),
                value=index,
                detail=_drawn(setting, current, context),
                disabled_because=""
                if setting.edit
                else context.translate("detected"),
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
            left = _leaving(screen, current, context)
            if left is not None:
                return left
            continue
        chosen = answer.unwrap()[0]
        if chosen == len(SETTINGS):
            # The operation list itself, then one confirmation: the row before
            # this one is the last chance to see what the disk is about to get.
            seen = overview_screen(screen, current, context)
            if seen.chosen:
                return Finished(seen.unwrap())
            if seen.outcome is Outcome.CANCELLED:
                left = _leaving(screen, current, context)
                if left is not None:
                    return left
            continue
        editor = SETTINGS[chosen].edit
        if editor is None:
            continue
        context.visited.add(SETTINGS[chosen].key)
        edited = editor(screen, current, context)
        if edited.outcome is Outcome.CANCELLED:
            left = _leaving(screen, current, context)
            if left is not None:
                return left
            # A grouped row hands back what was edited inside it, so staying
            # keeps those answers instead of dropping the whole group.
            if edited.value is not None:
                current = edited.value
            continue
        if edited.chosen:
            current = edited.unwrap()


def _drawn(setting: Setting, config: InstallConfig, context: Context) -> str:
    """A row's value as the operator reads it. `UNSET` is the sentinel
    `style_of` compares against, so it is translated here and not there."""
    value = setting.value(config, context)
    return context.translate(value) if value == UNSET else value


def _leaving(screen: Screen, config: InstallConfig, context: Context) -> Finished | None:
    """Asked rather than obeyed, and asked wherever the escape came from: one
    stray key should not throw away every answer the operator has entered."""
    menu: Menu[str] = Menu(
        title=context.translate("Leave without installing?"),
        items=[
            Item(label=context.translate("Back to the menu"), value="stay"),
            Item(label=context.translate("Leave"), value="leave"),
            Item(label=context.translate("Save the configuration and leave"), value="save"),
        ],
        footer=context.translate("Cancel"),
    )
    answer = menu.run(screen)
    if not answer.chosen or answer.unwrap()[0] == "stay":
        return None
    if answer.unwrap()[0] == "leave":
        return Finished(None)
    return _saving(screen, config, context)


def _saving(screen: Screen, config: InstallConfig, context: Context) -> Finished | None:
    """Ask for a name and write the file, retrying on the message the write
    failed with: a path that cannot be written is a typo far more often than a
    reason to throw away every answer."""
    title = context.translate("Save the configuration as")
    while True:
        typed = TextField(
            title=title, placeholder=SAVE_AS, footer=context.translate("Cancel")
        ).run(screen)
        if not typed.chosen:
            return None
        try:
            return Finished(None, saved=context.save_config(config, typed.unwrap() or SAVE_AS))
        except GentooInstallError as error:
            title = str(error)


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
