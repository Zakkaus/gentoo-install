# SPDX-License-Identifier: GPL-2.0-or-later
"""Render the final configuration and operation overview."""

from __future__ import annotations

from typing import Final, Sequence

from ..errors import GentooInstallError
from ..model.config import InstallConfig
from ..i18n import Catalog
from ..plan import automatic as automatic_values
from ..plan.build import build as plan_build
from ..plan.operations import Operation
from ..plan.render import counts
from .context import Context, answers, footer, say, show_address
from .settings import UNSET, settings_for
from .widgets import Answer, Confirm, Item, Menu, Outcome, Screen


_INSTALL: Final[int] = 2
_EXPORT: Final[int] = 1


def _counted(operations: Sequence[Operation], translate: Catalog) -> str:
    """Return the translated operation count and stage breakdown."""
    counted = counts(operations)
    parts = [f"{translate(stage.value)} {count}" for stage, count in counted.items()]
    return "{}: {}".format(
        translate("{count} operations").format(count=sum(counted.values())), ", ".join(parts)
    )


def _operation_label(operation: Operation, translate: Catalog) -> str:
    """Return an operation label, including older descriptions without parts."""
    parts = operation.describe_parts()
    if parts is None:
        return operation.describe()
    template, values = parts
    return translate(template).format(*values)


def overview_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Show the operation sequence and ask for final confirmation."""
    translate = context.translate
    try:
        operations = plan_build(config, context.groups)
    except GentooInstallError as error:
        say(screen, context, str(error).splitlines()[-1].strip())
        return Answer(Outcome.CANCELLED)
    items: list[Item[int]] = []
    # The table the menu asked from, not every table: in `dd` mode the menu
    # asks two rows and this listed the twenty-three of a disk install, so the
    # last screen before writing reviewed rows nobody answered and hid the
    # ones they did.
    for group in settings_for(config):
        for row in group.rows or (group,):
            value = row.value(config, context)
            items.append(
                Item(
                    label=f"{translate(row.label)}  {translate(value) if value == UNSET else value}",
                    value=0,
                )
            )
    given = (
        *automatic_values.video_cards(config, context.groups),
        *automatic_values.use_flags(config, context.groups),
        *automatic_values.user_groups(config, context.groups),
        *automatic_values.environment(config, context.groups),
        *automatic_values.kernel_parameters(config),
    )
    if given:
        items.append(Item(label=f"— {translate('added for you')} —", value=0))
        items += [
            Item(
                label=f"  {one.value}",
                value=0,
                detail=f"{translate(one.because)} ({one.source})"
                if one.source
                else translate(one.because),
            )
            for one in given
        ]
    items.append(Item(label=f"— {translate('Operations')} —", value=0))
    items += [
        Item(label=_operation_label(one, translate), value=0) for one in operations
    ]
    items.insert(
        0,
        Item(
            label=translate("Send the configuration to the pastebin"),
            value=_EXPORT,
            detail=translate("public, without the password hashes"),
        ),
    )
    items.insert(
        0,
        Item(
            label=translate("Start the installation"),
            value=_INSTALL,
            detail=translate("everything below is what it will do"),
        ),
    )
    while True:
        menu: Menu[int] = Menu(
            title=f"{translate('Overview')}: {_counted(operations, translate)}",
            items=items,
            footer=footer(translate, "Choose a row"),
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        chosen = answer.unwrap()
        if chosen == _INSTALL:
            break
        if chosen != _EXPORT:
            continue
        try:
            show_address(screen, context, context.publish_config(config))
        except GentooInstallError as error:
            say(screen, context, str(error))
    confirmed = Confirm(
        **answers(translate),
        title=translate("Install"),
        footer=footer(translate, "Start writing to the disks"),
    ).run(screen)
    if not confirmed.chosen:
        return Answer(confirmed.outcome)
    return Answer(Outcome.CHOSE, config) if confirmed.unwrap() else Answer(Outcome.BACK)
