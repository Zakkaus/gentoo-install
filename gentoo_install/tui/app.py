# SPDX-License-Identifier: GPL-2.0-or-later
"""The main menu: every setting, its current value, and what edits it.

Not a wizard. The operator opens rows in any order and as often as they like,
and starts the install from the last row when nothing required is missing.
`archinstall` and `oddlama-gentoo-install` each arrived at this shape on their
own, because installing is a task people change their minds during.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from ..i18n import width
from ..model import compat
from ..model.config import InstallConfig
from ..errors import GentooInstallError
from ..plan.build import build
from .overview import overview_screen
from .screens import Context
from .screens import _say as say
from .settings import SETTINGS, UNSET, Setting, shown_value, style_of, unanswered
from .widgets import Item, Menu, Outcome, Screen, Style, TextField


#: The default name for a saved configuration, offered as the field's example.
SAVE_AS: Final[str] = "my-install.toml"


class MainMenuContext(Context):
    """A leaf-screen context extended with main-menu session state."""

    def __init__(self, base: Context) -> None:
        self.__dict__ = base.__dict__.copy()
        self.columns = 80
        self.visited = set()



#: What a row's state is called beside its label. A colour and a marker say
#: nothing on a console with no colour, and a legend elsewhere is not the row.
_STATE_NAMES: Final[Mapping[Style, str]] = MappingProxyType(
    {Style.REQUIRED: "required", Style.UNTOUCHED: "never opened"}
)


def _labelled(setting: Setting, current: InstallConfig, context: Context) -> str:
    """The row's label, with its state named when it has one."""
    named = _STATE_NAMES.get(style_of(setting, current, context))
    label = context.translate(setting.label)
    return f"{label} [{context.translate(named)}]" if named else label


def _menu_footer(context: Context) -> str:
    """Label the main-menu action by what Enter opens."""
    return "  ".join(
        (
            f"[enter] {context.translate('Open')}",
            f"[q] {context.translate('Cancel')}",
        )
    )


@dataclass(frozen=True)
class Finished:
    """What the operator ended with, and whether they meant to."""

    config: InstallConfig | None
    #: Where the configuration was written on the way out, for `cli.py` to
    #: print once curses has given the terminal back.
    saved: str = ""
    #: The address it was sent to, printed the same way and with a code beside
    #: it, because a console address cannot be copied.
    published: str = ""

    @property
    def cancelled(self) -> bool:
        return self.config is None


def run(screen: Screen, start: InstallConfig, context: Context) -> Finished:
    if not isinstance(context, MainMenuContext):
        context = MainMenuContext(context)
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
                label=_labelled(setting, current, context),
                value=index,
                detail=_drawn(setting, current, context),
                # `unavailable` first: `nested()` reads it and this loop did
                # not, so a top-level row carrying a reason would have opened a
                # screen the nested path refuses.
                disabled_because=setting.unavailable(current, context)
                or ("" if setting.edit else context.translate("detected")),
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
            footer=_menu_footer(context),
            legend=_legend(current, context),
        )
        answer = menu.run(screen)
        cursor = menu.cursor
        if not answer.chosen:
            left = _leaving(screen, current, context)
            if left is not None:
                return left
            continue
        chosen = answer.unwrap()
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
        if editor is None or SETTINGS[chosen].unavailable(current, context):
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


def _legend(config: InstallConfig, context: Context) -> str:
    """What the two colours mean, on the line that has room for it.

    Only the colours in use: a legend for red on a menu with nothing red is one
    more thing to read. The words repeat what each row already says, because a
    console with no colour has to convey the same thing.
    """
    shown = [style_of(one, config, context) for one in SETTINGS]
    parts = []
    if Style.REQUIRED in shown:
        parts.append(f"* {context.translate('required')}")
    if Style.UNTOUCHED in shown:
        parts.append(f"~ {context.translate('never opened')}")
    return "  ".join(parts)


#: Room the Install row's own label and the reason need, so the names are
#: measured against what is left rather than against the whole line.
_MARGIN: Final[int] = 30


def _as_many_as_fit(names: list[str], context: Context, *, extra: int = 0) -> str:
    """The names that fit on this terminal, and how many did not.

    `extra` counts what was left out before this call, so the number the
    operator reads is everything still unanswered rather than everything
    dropped from one list.
    """
    room = max(20, context.columns - _MARGIN)
    taken: list[str] = []
    for name in names:
        rest = len(names) - len(taken) - 1
        if taken and width(", ".join([*taken, name])) + (4 if rest else 0) > room:
            break
        taken.append(name)
    left = len(names) - len(taken) + extra
    joined = ", ".join(taken)
    return f"{joined} +{left}" if left else joined


def _drawn(setting: Setting, config: InstallConfig, context: Context) -> str:
    """A row's value as the operator reads it, the same way a grouped row
    renders the rows behind it."""
    return shown_value(setting, config, context)


def _leaving(screen: Screen, config: InstallConfig, context: Context) -> Finished | None:
    """Asked rather than obeyed, and asked wherever the escape came from: one
    stray key should not throw away every answer the operator has entered."""
    menu: Menu[str] = Menu(
        title=context.translate("Leave without installing?"),
        items=[
            Item(label=context.translate("Back to the menu"), value="stay"),
            Item(label=context.translate("Leave"), value="leave"),
            Item(label=context.translate("Save the configuration and leave"), value="save"),
            Item(
                label=context.translate("Send the configuration to the pastebin and leave"),
                value="publish",
                detail=context.translate("public, without the password hashes"),
            ),
        ],
        footer=context.translate("Cancel"),
    )
    answer = menu.run(screen)
    if not answer.chosen or answer.unwrap() == "stay":
        return None
    if answer.unwrap() == "leave":
        return Finished(None)
    if answer.unwrap() == "publish":
        return _publishing(screen, config, context)
    return _saving(screen, config, context)


def _publishing(screen: Screen, config: InstallConfig, context: Context) -> Finished | None:
    """Send the configuration to the pastebin, so an issue can point at it.

    Every password hash is replaced first. The address is public and a crypt
    hash is what an offline attack starts from.
    """
    try:
        return Finished(None, published=context.publish_config(config))
    except GentooInstallError as error:
        say(screen, context, str(error))
        return None


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

    As many names as the terminal has room for, then a count. Naming one and
    counting the rest was the same on an 80-column console and on a 200-column
    one, so a screen wide enough to list all four still said `+3`.
    """
    missing = unanswered(config, context)
    if missing:
        # One segment per reason: a confirmation nobody agreed to is not a
        # field nobody filled in, and saying so in one sentence made the two
        # read as one list with a stray count on the end.
        reasons: list[str] = []
        for one in missing:
            if one.missing not in reasons:
                reasons.append(one.missing)
        segments = [
            "{}: {}".format(
                ", ".join(
                    context.translate(one.label) for one in missing if one.missing == reason
                ),
                context.translate(reason),
            )
            for reason in reasons
        ]
        whole = "; ".join(segments)
        if width(whole) <= max(20, context.columns - _MARGIN):
            return whole
        # Too narrow for all of it: the names of the first reason, then a count
        # of everything else still unanswered.
        first = [one for one in missing if one.missing == reasons[0]]
        labels = [context.translate(one.label) for one in first]
        left = len(missing) - len(first)
        said = _as_many_as_fit(labels, context, extra=left)
        return f"{said}: {context.translate(reasons[0])}"
    # Asked of the table rather than read off the exception: `validate` builds
    # its message in English for a log, and this row is the one the operator
    # reads. `root on ZFS excludes BIOS boot:` was drawn in English in front of
    # a translated reason.
    broken = compat.violations(config)
    if broken:
        return broken[0].describe(context.translate)
    try:
        # The whole plan, not `validate` alone: a group whose packages live in
        # an overlay nobody selected raises from `plan.build`, and the row that
        # blocks the install is the only place that can say so before the
        # operator presses it and loses every answer to a traceback.
        build(config, context.groups)
    except GentooInstallError as error:
        return str(error).splitlines()[-1].strip()
    return ""
