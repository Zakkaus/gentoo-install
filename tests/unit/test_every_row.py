"""Every row of the menu, opened once and read back.

Two interface defects reached an operator in one session, and neither was a
model defect: the timezone screen offered `UTC` and nothing else on a medium
whose `/usr/share/zoneinfo` was empty, and the Install row said `root
password: still needs an answer` beside a row that read `set`. Both were
invisible to the tests because nothing drove the panel.

These walk it. Not one screen apiece — every row in `SETTINGS`, and every row
behind a grouped one, so a screen added later is covered the day it is added.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from gentoo_install.model.config import InstallConfig
from gentoo_install.tui import app, settings
from gentoo_install.tui.screens import Context
from gentoo_install.tui.settings import UNSET, Setting
from gentoo_install.tui.widgets import Answer, Outcome

from .fake_screen import FakeScreen
from .layouts import config, ext4_on_gpt
from .test_tui_app import context


def every_row() -> list[Setting]:
    """Every row an operator can put a cursor on, groups flattened."""
    found: list[Setting] = []
    for one in settings.SETTINGS:
        found.append(one)
        found.extend(one.rows)
    return found


def openable() -> list[Setting]:
    return [one for one in every_row() if one.edit is not None]


#: What leaves any widget, pressed until it does. `q` is a character a
#: hostname may contain, so a text field cannot take it and escape is what
#: works everywhere. Forty because a nested screen returns to the one that
#: opened it: a row that has not left by then is one an operator cannot get
#: out of, which is itself the finding.
LEAVE = ["\x1b"] * 40


def leave(
    row: Setting, before: InstallConfig, at: Context, columns: int = 100
) -> tuple[FakeScreen, Answer[InstallConfig]]:
    """Open a row and press cancel until it gives up the screen."""
    screen = FakeScreen(keys=list(LEAVE), lines=30, columns=columns)
    assert row.edit is not None
    try:
        answer = row.edit(screen, before, at)
    except AssertionError as exhausted:
        raise AssertionError(
            f"{row.key} did not leave after {len(LEAVE)} cancels: {exhausted}"
        ) from exhausted
    return screen, answer


@pytest.mark.parametrize("row", openable(), ids=lambda row: row.key)
def test_every_row_offers_an_answer(row: Setting) -> None:
    """A row either draws something to choose or flips on the spot.

    A screen that offers nothing is a row the operator cannot answer: the
    timezone screen was exactly that on a medium with no zone data, one entry
    reading `UTC` and no way to reach another. A toggle draws nothing on
    purpose — the row already says `on` or `off`, and asking again is asking
    what was just answered — so it has to change the configuration instead.
    """
    at = context()
    at.columns = 100
    before = config(ext4_on_gpt())
    screen, answer = leave(row, before, at)

    if screen.frames:
        drawn = [line for line in screen.frames[0] if line.strip()]
        assert len(drawn) >= 2, f"{row.key} drew only {drawn}"
        return
    assert answer.outcome is Outcome.CHOSE, f"{row.key} drew nothing and answered nothing"
    assert answer.value != before, f"{row.key} drew nothing and changed nothing"


@pytest.mark.parametrize("row", openable(), ids=lambda row: row.key)
def test_no_row_draws_past_the_edge_of_an_eighty_column_console(row: Setting) -> None:
    """The interface has to be usable on the console a live medium gives, and
    a line that wraps there is a line that cannot be read."""
    from gentoo_install.i18n import width

    at = context()
    at.columns = 80
    screen, _ = leave(row, config(ext4_on_gpt()), at, columns=80)
    for frame in screen.frames:
        for line in frame:
            assert width(line) <= 80, f"{row.key}: {line!r}"


def test_the_install_row_never_asks_for_something_a_row_says_is_answered() -> None:
    """`root password: still needs an answer` was printed beside a row reading
    `set`, because the two views of one state disagreed. Whatever the rule, a
    row showing a value must not be named as missing an answer."""
    at = context()
    at.columns = 200
    whole = config(ext4_on_gpt())
    for one in settings.unanswered(whole, at):
        shown = one.value(whole, at)
        assert shown == UNSET or one.detected, (
            f"{one.key} shows {shown!r} and is still asked for, and it has no detected default"
        )


def test_every_required_row_can_be_answered_from_the_menu() -> None:
    """A required row with no editor is one the install can never start from.

    `Firmware` is the exception and says so: it is read from the machine and
    there is nothing to choose.
    """
    for one in every_row():
        if not one.required:
            continue
        assert one.edit is not None or one.key == "firmware", one.key


def test_leaving_a_row_alone_changes_nothing() -> None:
    """`q` out of every screen and the configuration has to come back as it
    was. A screen that writes on the way out edits what the operator declined
    to edit."""
    at = context()
    at.columns = 100
    before = config(ext4_on_gpt())
    for row in openable():
        screen, answer = leave(row, before, at)
        if not screen.frames:
            continue  # A toggle: flipping is what it is for.
        if answer.outcome is Outcome.CHOSE:
            assert answer.value == before, row.key


def test_a_row_that_cannot_be_opened_says_why() -> None:
    """A row drawn as unavailable with no reason is one the operator presses
    and nothing happens to."""
    at = context()
    zfs = replace(config(ext4_on_gpt()))
    for one in every_row():
        said = one.unavailable(zfs, at)
        assert said == "" or said.strip(), one.key


def test_a_row_that_reads_the_machine_still_offers_something_when_it_finds_nothing() -> None:
    """A live medium with an empty `/usr/share/zoneinfo` left the timezone
    screen showing `UTC` and nothing else, so no timezone could be chosen.

    Every row whose list comes from the machine is the same shape of risk, and
    a medium that ships one of them empty is not exotic: the one that produced
    this report shipped no zone data at all.
    """
    at = context()
    at.columns = 100
    # The lists a medium can ship empty. The disk list is not one of them: a
    # machine with no disk is refused by preflight before the menu opens.
    at.timezones = ()
    at.keymaps = lambda: ()
    at.cpu_flags = ()

    thin: list[str] = []
    for row in openable():
        screen, _ = leave(row, config(ext4_on_gpt()), at)
        if not screen.frames:
            continue
        drawn = [line for line in screen.frames[0] if line.strip()]
        if len(drawn) < 2:
            thin.append(f"{row.key}: {drawn}")
    assert not thin, thin


def test_the_timezone_screen_offers_cities_behind_a_region() -> None:
    """The first screen is four region shortcuts, so a list with nothing in it
    still draws four rows. The defect an operator met was one level down:
    `Asia` opened on nothing, and `UTC` was the only reachable answer.

    The list handed in is the one the installer really passes, so this fails
    if `Probe.timezones` ever answers empty again.
    """
    from pathlib import Path

    from gentoo_install.exec.probe import Probe
    from gentoo_install.exec.runner import Runner

    real = Probe(runner=Runner(log=lambda line: None), work=Path("/tmp")).timezones()
    assert len(real) > 300, len(real)

    at = context()
    at.columns = 100
    at.timezones = real
    row = next(one for one in every_row() if one.key == "timezone")
    assert row.edit is not None
    # Enter the first region, then leave: the frame after the first is the
    # city list, and a region with no cities is the failure.
    screen = FakeScreen(keys=["\n", *LEAVE], lines=30, columns=100)
    row.edit(screen, config(ext4_on_gpt()), at)
    cities = [line for line in screen.frames[1] if line.strip()]
    assert len(cities) > 5, cities


def test_reopening_root_login_over_ssh_does_not_widen_it() -> None:
    """The cursor started on `allowed`, so an operator who had refused root
    over ssh and opened the row again to read it widened root's access by
    pressing enter. The shared reopening test set this to `True`, which is the
    first item, so it could not see the difference."""
    at = context()
    at.columns = 100
    refused = replace(
        config(ext4_on_gpt()),
        system=replace(config(ext4_on_gpt()).system, sshd=True, sshd_root_login=False),
    )
    row = next(one for one in every_row() if one.key == "rootlogin")
    assert row.edit is not None
    answer = row.edit(FakeScreen(keys=["\n"] * 4, lines=40, columns=100), refused, at)
    assert answer.outcome is Outcome.CHOSE
    assert answer.unwrap().system.sshd_root_login is False


def test_reopening_encryption_keeps_the_container_and_its_passphrase() -> None:
    """The cursor started on `No`, so reopening the row and pressing enter
    removed the LUKS container and cleared the staged passphrase. Cancelling
    the passphrase field is declining to change it, not declining to have one.
    """
    from .layouts import encrypted_root

    row = next(one for one in every_row() if one.key == "encryption")
    assert row.edit is not None

    at = context()
    at.columns = 100
    at.choice = replace(at.choice, passphrase_file="/run/keys/root")
    # Enter accepts `still encrypted`, escape declines to retype the key.
    row.edit(
        FakeScreen(keys=["\n", "\x1b", "\x1b"], lines=40, columns=100),
        config(encrypted_root()),
        at,
    )
    assert at.choice.passphrase_file == "/run/keys/root"

    # Choosing no still turns it off: this is a default, not a refusal. The
    # items are `No` then `Yes`, and the cursor now starts on `Yes`, so `No`
    # is one row up.
    off = context()
    off.columns = 100
    off.choice = replace(off.choice, passphrase_file="/run/keys/root")
    row.edit(
        FakeScreen(keys=["KEY_UP", "\n"], lines=40, columns=100),
        config(encrypted_root()),
        off,
    )
    assert off.choice.passphrase_file == ""


#: Rows that answer by flipping rather than by offering a list. Reopening one
#: and pressing enter flips it again, which is what it is for.
FLIPPED: frozenset[str] = frozenset({"cron"})


def test_no_row_loses_its_value_when_it_is_opened_again() -> None:
    """Change a row to something other than its first entry, open it again and
    press enter: the value it shows has to be the one that was chosen.

    Two rows changed a setting the operator only opened to read — `Root login
    over SSH` widened root's access and `Encryption` removed the container —
    and six more read their first entry back. Each menu was missing `current`,
    and no test drove a row twice, so none of them could be seen.
    """
    at = context()
    at.columns = 100
    base = config(ext4_on_gpt())
    wrong: list[str] = []
    walked = 0
    for row in openable():
        if row.key in FLIPPED:
            continue
        assert row.edit is not None
        try:
            first = row.edit(
                FakeScreen(keys=["KEY_DOWN", "\n", *(["\n"] * 6)], lines=40, columns=100),
                base,
                at,
            )
        except AssertionError:
            continue  # A screen enter is not an answer to; `LEAVE` covers those.
        if first.outcome is not Outcome.CHOSE:
            continue
        changed = first.unwrap()
        chose = row.value(changed, at)
        if chose == row.value(base, at):
            continue  # The row has one entry, or the second is what it held.
        try:
            again = row.edit(FakeScreen(keys=["\n"] * 8, lines=40, columns=100), changed, at)
        except AssertionError:
            continue
        if again.outcome is not Outcome.CHOSE:
            continue
        walked += 1
        if row.value(again.unwrap(), at) != chose:
            wrong.append(f"{row.key}: chose {chose!r}, reopened as {row.value(again.unwrap(), at)!r}")
    assert not wrong, wrong
    assert walked > 10, walked
