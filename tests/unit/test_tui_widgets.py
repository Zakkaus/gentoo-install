# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import pytest

from gentoo_install.tui.widgets import (
    Answer,
    Confirm,
    Field,
    Form,
    FormRejected,
    Item,
    Menu,
    MultipleChoiceMenu,
    Outcome,
    PaneRow,
    SEPARATOR,
    Style,
    TextField,
    TextFieldRejected,
)

from gentoo_install.i18n import width

from .fake_screen import FakeScreen

#: A wide character by codepoint: no CJK literal belongs in the test tree, and
#: the layout rules exist for exactly this character class.
WIDE = "\u78c1\u789f"


def menu() -> Menu[str]:
    return Menu(
        title="Disks",
        items=[
            Item(label="/dev/vda", value="vda", detail="20 GiB"),
            Item(label="/dev/vdb", value="vdb", detail="40 GiB"),
        ],
    )


def test_choosing_returns_the_value_and_not_the_label() -> None:
    screen = FakeScreen(keys=["KEY_DOWN", "\n"])
    answer = menu().run(screen)
    assert answer.outcome is Outcome.CHOSE
    assert answer.unwrap() == "vdb"


def test_going_back_is_not_cancelling() -> None:
    """Three states, because a caller that treats back as cancel drops the
    operator out of the installer when they meant to fix the previous step."""
    assert menu().run(FakeScreen(keys=["KEY_LEFT"])).outcome is Outcome.BACK
    assert menu().run(FakeScreen(keys=["q"])).outcome is Outcome.CANCELLED
    assert not Answer(Outcome.BACK).chosen


def test_answer_map_transforms_only_a_chosen_value() -> None:
    transformed: list[int] = []

    def describe(value: int) -> str:
        transformed.append(value)
        return f"value {value}"

    chosen: Answer[int] = Answer(Outcome.CHOSE, 3)
    back: Answer[int] = Answer(Outcome.BACK)
    cancelled: Answer[int] = Answer(Outcome.CANCELLED)

    assert chosen.map(describe).unwrap() == "value 3"
    assert back.map(describe).outcome is Outcome.BACK
    assert cancelled.map(describe).outcome is Outcome.CANCELLED
    assert transformed == [3]


def test_a_disabled_row_cannot_be_chosen_and_says_why() -> None:
    """The reason comes from the compatibility table, so the interface and the
    validator give the operator the same sentence."""
    excluded: Menu[str] = Menu(
        title="Bootloader",
        items=[
            Item(
                label="systemd-boot",
                value="systemd-boot",
                disabled_because="it only has an EFI implementation",
            ),
            Item(label="GRUB", value="grub"),
        ],
    )
    screen = FakeScreen(keys=["KEY_UP", "\n"])
    assert excluded.run(screen).unwrap() == "grub"
    assert "it only has an EFI implementation" in screen.last


def test_a_disabled_reason_wraps_at_the_80_column_floor() -> None:
    reason = "this reason remains visible instead of disappearing beyond the right edge"
    screen = FakeScreen(keys=["\n"], lines=24, columns=80)
    Menu(
        title="Bootloader",
        items=[Item(label="systemd-boot", value="sd", disabled_because=reason), Item(label="GRUB", value="grub")],
    ).run(screen)
    drawn = "".join(screen.frames[0])
    assert "this reason remains visible instead of disappearing beyond" in drawn
    assert "the right edge" in drawn


def test_the_cursor_starts_on_a_row_that_can_be_chosen() -> None:
    excluded: Menu[str] = Menu(
        title="Bootloader",
        items=[
            Item(label="systemd-boot", value="sd", disabled_because="EFI only"),
            Item(label="GRUB", value="grub"),
        ],
    )
    assert excluded.run(FakeScreen(keys=["\n"])).unwrap() == "grub"


def test_several_answers_are_marked_without_relying_on_a_glyph() -> None:
    """A console with no CJK font and no box-drawing set still has to show
    which rows are selected."""
    several: MultipleChoiceMenu[str] = MultipleChoiceMenu(
        title="Applications",
        items=[Item(label="firefox", value="firefox"), Item(label="bluetooth", value="bluetooth")],
    )
    screen = FakeScreen(keys=[" ", "KEY_DOWN", " ", "\n"])
    assert several.run(screen).unwrap() == ("firefox", "bluetooth")
    assert "[x] firefox" in screen.last


def test_marking_a_second_row_preferred_moves_the_mark() -> None:
    fonts: MultipleChoiceMenu[str] = MultipleChoiceMenu(
        title="Fonts",
        items=[
            Item(label="Noto Sans CJK", value="noto", preference_group="sans-serif"),
            Item(label="Source Han Sans", value="source", preference_group="sans-serif"),
        ],
        tri_state=True,
    )
    screen = FakeScreen(keys=[" ", " ", "KEY_DOWN", " ", " ", "\n"])

    assert fonts.run(screen).unwrap() == ("noto", "source")
    assert fonts.preferred == {1}
    assert "[-] Noto Sans CJK" in screen.last
    assert "[x] Source Han Sans" in screen.last


def test_an_installed_only_row_never_gets_a_preferred_mark() -> None:
    fonts: MultipleChoiceMenu[str] = MultipleChoiceMenu(
        title="Fonts",
        items=[Item(label="Open Sans", value="open-sans")],
        tri_state=True,
    )
    fonts._toggle(0)
    assert fonts.selected == {0}
    fonts._toggle(0)
    assert fonts.selected == set()
    assert fonts.preferred == set()


def test_none_is_a_choice_and_not_an_absent_answer() -> None:
    none = Menu[str | None](
        title="Filesystem",
        items=[Item(label="ext4", value="ext4"), Item(label="none", value=None)],
        current=None,
    )
    chosen = none.run(FakeScreen(keys=["\n"]))

    assert chosen.outcome is Outcome.CHOSE
    assert chosen.unwrap() is None
    with pytest.raises(ValueError):
        none.run(FakeScreen(keys=["KEY_LEFT"])).unwrap()


def test_a_wide_title_is_cut_to_the_screen_rather_than_overflowing() -> None:
    """FakeScreen asserts on overflow, so a layout that assumed one cell per
    character fails here instead of corrupting a serial console."""
    wide: Menu[str] = Menu(title=WIDE * 60, items=[Item(label=WIDE * 60, value="x")])
    wide.run(FakeScreen(keys=["\n"]))


def test_a_text_field_hands_back_what_was_typed() -> None:
    screen = FakeScreen(keys=["g", "e", "n", "t", "o", "o", "\n"])
    assert TextField(title="Hostname").run(screen).unwrap() == "gentoo"


def test_backspace_on_an_empty_field_goes_back() -> None:
    """Otherwise the only way out of a field the operator opened by mistake is
    to cancel the whole install."""
    assert TextField(title="Hostname").run(FakeScreen(keys=["\x7f"])).outcome is Outcome.BACK


def test_left_leaves_a_prefilled_field_without_editing_it() -> None:
    """A field that arrives with a value answers backspace by deleting, and
    escape by offering to end the run, so there was no way back out of the
    hostname screen at all: a menu walk on a real console left the machine
    calling itself `gento`."""
    answer = TextField(title="Hostname", value="gentoo").run(FakeScreen(keys=["KEY_LEFT"]))
    assert answer.outcome is Outcome.BACK
    assert answer.value is None

    filled = Form(
        title="User account",
        fields=[Field(label="name", value="zakk"), Field(label="shell", value="/bin/bash")],
    )
    assert filled.run(FakeScreen(keys=["KEY_LEFT"])).outcome is Outcome.BACK


def test_a_password_is_not_drawn() -> None:
    screen = FakeScreen(keys=["s", "e", "c", "r", "e", "t", "\n"])
    assert TextField(title="Password", masked=True).run(screen).unwrap() == "secret"
    assert "secret" not in screen.last
    assert "******" in screen.last


def test_a_destructive_confirmation_has_no_default_to_press_enter_on() -> None:
    """Erasing a disk is confirmed by typing its name, so a held-down return
    key cannot do it."""
    question = Confirm(title="Type the disk name to confirm.", phrase="/dev/vdb")
    typed = list("/dev/vdb") + ["\n"]
    assert question.run(FakeScreen(keys=typed)).unwrap() is True
    wrong = list("/dev/vda") + ["\n"]
    assert question.run(FakeScreen(keys=wrong)).unwrap() is False
    assert Confirm(title="Erase?").run(FakeScreen(keys=["\n"])).unwrap() is False


def test_a_text_field_draws_somewhere_to_type() -> None:
    """A bare string at the top of an empty screen does not read as an input:
    the maintainer could not tell which screens wanted typing."""
    field = TextField(title="Size", placeholder="512MiB")
    screen = FakeScreen(keys=["2", "0", "G", "\n"])
    assert field.run(screen).unwrap() == "20G"
    first, last = screen.frames[0], screen.frames[-1]
    # The caret comes first while the field is empty: drawn without one, a
    # placeholder cannot be told from a value already entered.
    assert any("[ _512MiB" in line for line in first), first
    assert any("[ 20G_" in line for line in last), last


def test_a_field_naming_an_exact_answer_keeps_the_box_empty() -> None:
    """`detail` is drawn on its own line. The erase screen used `placeholder`
    for the disk selector, so the box looked filled in and an operator pressed
    enter on it."""
    field = TextField(title="Type the disk name", detail="/dev/disk/by-id/wwn-0x5000")
    screen = FakeScreen(keys=["\n"])
    field.run(screen)
    drawn = [line for line in screen.frames[0] if line.strip()]
    assert any(line.strip() == "/dev/disk/by-id/wwn-0x5000" for line in drawn), drawn
    box = next(line for line in drawn if line.lstrip().startswith("["))
    assert box.split("[", 1)[1].strip(" ]") == "_", box


def test_a_masked_field_shows_the_caret_and_not_the_characters() -> None:
    field = TextField(title="Passphrase", masked=True)
    screen = FakeScreen(keys=[*"hunter2", "\n"])
    assert field.run(screen).unwrap() == "hunter2"
    drawn = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "hunter2" not in drawn
    assert "[ *******_" in drawn


def test_ctrl_c_asks_rather_than_ending_the_run() -> None:
    """raw mode delivers it as a byte instead of a signal, so it reaches the
    widget and is answered with the same question an escape is."""
    menu: Menu[int] = Menu(title="t", items=[Item(label="one", value=1)])
    assert menu.run(FakeScreen(keys=["\x03"])).outcome is Outcome.CANCELLED
    field = TextField(title="t")
    assert field.run(FakeScreen(keys=["\x03"])).outcome is Outcome.CANCELLED


def test_q_is_a_character_in_a_field_and_a_way_out_of_a_menu() -> None:
    """A hostname can contain q; a menu row cannot be typed into."""
    field = TextField(title="t")
    assert field.run(FakeScreen(keys=[*"qemu", "\n"])).unwrap() == "qemu"
    menu: Menu[int] = Menu(title="t", items=[Item(label="one", value=1)])
    assert menu.run(FakeScreen(keys=["q"])).outcome is Outcome.CANCELLED


def test_a_form_moves_between_its_fields_with_the_arrow_keys() -> None:
    """One field per screen makes the operator answer six questions without
    ever seeing them together, which is the failure this replaces."""
    form = Form(
        title="Network",
        fields=[Field(label="Address"), Field(label="Gateway"), Field(label="DNS")],
    )
    keys = [
        *"10.0.0.2/24", "KEY_DOWN",
        *"10.0.0.1", "KEY_DOWN",
        *"1.1.1.1", "KEY_DOWN", "\n",
    ]
    answered = form.run(FakeScreen(keys=keys, lines=30, columns=100))
    assert answered.unwrap() == ["10.0.0.2/24", "10.0.0.1", "1.1.1.1"]


def test_a_form_edits_the_field_the_cursor_is_on_and_no_other() -> None:
    form = Form(title="t", fields=[Field(label="a", value="one"), Field(label="b", value="two")])
    keys = ["KEY_DOWN", "\x7f", "\x7f", "\x7f", *"three", "KEY_DOWN", "\n"]
    assert form.run(FakeScreen(keys=keys, lines=20, columns=80)).unwrap() == ["one", "three"]


def test_a_form_shows_every_field_at_once() -> None:
    form = Form(
        title="Network",
        fields=[Field(label="Address", placeholder="10.0.0.2/24"), Field(label="DNS")],
    )
    screen = FakeScreen(keys=["KEY_DOWN", "KEY_DOWN", "\n"], lines=20, columns=90)
    form.run(screen)
    drawn = screen.frames[0]
    assert any("Address" in line and "10.0.0.2/24" in line for line in drawn), drawn
    assert any("DNS" in line for line in drawn)
    assert any("Done" in line for line in drawn)


def test_a_form_with_an_overlong_wide_label_still_accepts_input() -> None:
    """`room` was `columns - widest - 10`, which goes negative once a label is
    wider than the terminal. The trimming loop then deleted a string that was
    already empty, so the redraw never ended and the form never took a key.
    """
    form = Form(title="Network", fields=[Field(label=WIDE * 60)])
    screen = FakeScreen(keys=["KEY_DOWN", "\n"], lines=24, columns=80)

    assert form.run(screen).unwrap() == [""]


def test_escape_leaves_a_form_without_an_answer() -> None:
    """Back rather than Cancel: escape has one meaning at every depth below
    the main menu, and it had two inside one feature."""
    form = Form(title="t", fields=[Field(label="a")])
    assert form.run(FakeScreen(keys=["\x1b"])).outcome is Outcome.BACK


def test_a_field_that_had_content_can_be_cleared() -> None:
    """Backspace on an empty field leaves the screen, so clearing one used to
    be impossible: the keystroke that emptied it was followed by one that left
    and kept the old value. Several fields mean something by empty."""
    field = TextField(title="Mount point", value="/srv")
    keys = ["\x7f", "\x7f", "\x7f", "\x7f", "\x7f", "\n"]
    assert field.run(FakeScreen(keys=keys)).unwrap() == ""


def test_backspace_still_goes_back_from_a_field_nobody_touched() -> None:
    # A field with content takes one backspace per character before it is
    # empty, and only then does the next one leave.
    field = TextField(title="Mount point", value="/srv")
    assert field.run(FakeScreen(keys=["\x7f"] * 4 + ["\x7f", "\n"])).unwrap() == ""
    empty = TextField(title="Mount point")
    assert empty.run(FakeScreen(keys=["\x7f"])).outcome is Outcome.BACK


def test_a_form_takes_the_back_its_footer_offers() -> None:
    """Every caller draws `[backspace] Back` and the form had no BACK branch,
    so the only way out of it was to answer."""
    from gentoo_install.tui.widgets import Field, Form

    empty = Form(title="Address", fields=[Field(label="Port"), Field(label="Address")])
    assert empty.run(FakeScreen(keys=["KEY_BACKSPACE"])).outcome is Outcome.BACK

    # A field with content deletes instead, the same as a single text field.
    filled = Form(
        title="Address",
        fields=[Field(label="Port", value="222"), Field(label="Address")],
    )
    answer = filled.run(FakeScreen(keys=["KEY_BACKSPACE", "KEY_DOWN", "KEY_DOWN", "\n"]))
    assert answer.outcome is Outcome.CHOSE
    assert answer.unwrap()[0] == "22"


def test_a_validated_form_retains_values_and_draws_the_error_inline() -> None:
    submitted: list[list[str]] = []

    def validate(values: list[str]) -> Answer[list[str]] | FormRejected:
        submitted.append(values)
        if len(submitted) == 1:
            return FormRejected("That value is not valid.")
        return Answer(Outcome.CHOSE, values)

    screen = FakeScreen(
        keys=[*"kept", "KEY_DOWN", "\n", "KEY_DOWN", "\n"], lines=20, columns=80
    )
    answer = Form(title="Account", fields=[Field(label="Name")]).run_validated(
        screen, validate
    )

    assert answer.unwrap() == ["kept"]
    assert submitted == [["kept"], ["kept"]]
    retry = next(frame for frame in screen.frames if "That value is not valid." in "\n".join(frame))
    assert "kept" in "\n".join(retry)


def test_a_validated_text_field_retains_the_rejected_value_and_error() -> None:
    submitted: list[str] = []

    def validate(value: str) -> Answer[str] | TextFieldRejected:
        submitted.append(value)
        if value == "wrong":
            return TextFieldRejected("That value is not valid.", value)
        return Answer(Outcome.CHOSE, value)

    screen = FakeScreen(keys=[*"wrong", "\n", *(["\x7f"] * 5), *"right", "\n"])
    answer = TextField(title="Size").run_validated(screen, validate)

    assert answer.unwrap() == "right"
    assert submitted == ["wrong", "right"]
    retry = next(frame for frame in screen.frames if "That value is not valid." in "\n".join(frame))
    assert "wrong_" in "\n".join(retry)

def test_a_validator_can_correct_one_field_without_losing_the_others() -> None:
    submitted: list[list[str]] = []

    def validate(values: list[str]) -> Answer[tuple[str, str]] | FormRejected:
        submitted.append(values)
        if values[1] != "right":
            return FormRejected("The two do not match.", {1: ""})
        return Answer(Outcome.CHOSE, (values[0], values[1]))

    keys = [
        *"kept", "KEY_DOWN", *"wrong", "KEY_DOWN", "\n",
        "KEY_DOWN", *"right", "KEY_DOWN", "\n",
    ]
    answer = Form(
        title="Password",
        fields=[Field(label="First"), Field(label="Again")],
    ).run_validated(FakeScreen(keys=keys), validate)

    assert answer.unwrap() == ("kept", "right")
    assert submitted == [["kept", "wrong"], ["kept", "right"]]


@pytest.mark.parametrize(
    ("pressed", "outcome"),
    [("KEY_BACKSPACE", Outcome.BACK), ("\x1b", Outcome.BACK)],
)
def test_a_rejected_form_can_still_go_back_or_cancel(pressed: str, outcome: Outcome) -> None:
    def reject(values: list[str]) -> Answer[str] | FormRejected:
        return FormRejected("Try again.")

    form = Form(title="Account", fields=[Field(label="Name")])
    answer = form.run_validated(FakeScreen(keys=["KEY_DOWN", "\n", pressed]), reject)
    assert answer.outcome is outcome


def test_a_choice_that_became_invalid_can_still_be_dropped() -> None:
    """An application from an overlay the operator then removed leaves a
    selected row disabled. Space refused the keystroke and the cursor skipped
    the row, so the invalid choice could not be undone without putting the
    overlay back."""
    menu: MultipleChoiceMenu[str] = MultipleChoiceMenu(
        title="Applications",
        items=[
            Item(label="vim", value="vim"),
            Item(label="wechat", value="wechat", disabled_because="needs gentoo-zh"),
            Item(label="mpv", value="mpv"),
        ],
        selected={1},
    )
    # Down from the first row reaches the disabled row, because it is selected.
    assert menu._step(0, 1) == 1
    menu._toggle(1)
    assert menu.selected == set()
    # And it cannot be selected again while it is disabled.
    menu._toggle(1)
    assert menu.selected == set()


def test_the_cursor_still_skips_a_disabled_row_nobody_chose() -> None:
    menu: MultipleChoiceMenu[str] = MultipleChoiceMenu(
        title="Applications",
        items=[
            Item(label="vim", value="vim"),
            Item(label="wechat", value="wechat", disabled_because="needs gentoo-zh"),
            Item(label="mpv", value="mpv"),
        ],
    )
    assert menu._step(0, 1) == 2


def test_tab_moves_on_and_shift_tab_moves_back() -> None:
    """An operator coming from a browser or a graphical installer reaches for
    tab first, and nothing in these screens used it for anything. The arrows
    keep working; this is another way to the same row."""
    from gentoo_install.tui.widgets import Field, Form, Item, Menu

    # Three rows, tab twice to the third, shift-tab once back to the second.
    menu: Menu[int] = Menu(
        title="pick",
        items=[Item(label="one", value=1), Item(label="two", value=2), Item(label="three", value=3)],
    )
    answer = menu.run(FakeScreen(keys=["\t", "\t", "KEY_BTAB", "\n"], lines=24))
    assert answer.unwrap() == 2

    # The raw sequence too: ncurses reports `KEY_BTAB` only once keypad mode is
    # on, and what arrives before that is the escape sequence itself.
    raw: Menu[int] = Menu(
        title="pick",
        items=[Item(label="one", value=1), Item(label="two", value=2)],
    )
    assert raw.run(FakeScreen(keys=["\t", "\x1b[Z", "\n"], lines=24)).unwrap() == 1

    # A form moves between fields the same way, and tab is not typed into one.
    form = Form(
        title="account",
        fields=[Field(label="name"), Field(label="shell")],
    )
    typed = form.run(
        FakeScreen(keys=[*"zakk", "\t", *"bash", "KEY_BTAB", "\t", "\n", "\n"], lines=24)
    )
    assert typed.unwrap() == ["zakk", "bash"]


def test_a_window_that_grows_while_a_widget_waits_is_read_again() -> None:
    """curses keeps the size it started with until it is asked again, so a list
    stayed as tall as the window was when the menu opened. The widget reads
    `size()` on every redraw, and an unrecognised key is one to redraw on.

    The ncurses side of this, where a real `SIGWINCH` turns into `KEY_RESIZE`,
    has no test: driven through a pty under pytest the resize never reaches the
    child, and a mechanism that does not run proves nothing.
    """

    class Growing(FakeScreen):
        """A terminal enlarged while the widget was waiting for a key."""

        def key(self) -> str:
            pressed = super().key()
            self.lines = 30
            return pressed

    items = [Item(label=f"row{n:02d}", value=n) for n in range(25)]
    screen = Growing(keys=["x", "q"], lines=12)
    Menu(title="rows", items=items).run(screen)
    drawn = [len([row for row in frame if "row" in row]) for frame in screen.frames]
    assert len(drawn) >= 2, screen.frames
    assert drawn[-1] > drawn[0], drawn


def test_a_band_fills_the_row_so_the_reverse_video_reaches_both_edges() -> None:
    """A header that stops where its text stops is a highlighted word, not a
    band, and the screen still reads as a printout."""
    from gentoo_install.tui.widgets import band
    from gentoo_install.i18n import width

    screen = FakeScreen(columns=40)
    band(screen, 0, "gentoo-install", "6/22 answered")
    drawn = screen.frames[-1][0] if screen.frames else screen.drawn(0)

    # Cells, not characters: a row of 40 characters can occupy 60 columns, and
    # a band that reaches both edges is a statement about columns.
    assert width(drawn) == 40
    assert drawn.strip().startswith("gentoo-install")
    assert drawn.strip().endswith("6/22 answered")
    assert drawn in screen.highlighted


def test_a_band_drops_the_right_side_rather_than_cutting_it() -> None:
    """Half a count reads as a different count."""
    from gentoo_install.tui.widgets import band
    from gentoo_install.i18n import width

    screen = FakeScreen(columns=20)
    band(screen, 0, "gentoo-install", "6/22 answered")
    drawn = screen.drawn(0)

    assert width(drawn) == 20
    # Whole or absent. A truncating band leaves `6/22 `, which reads as a
    # different count rather than as a missing one.
    assert "6/22" not in drawn
    assert drawn.rstrip() == "gentoo-install"


def test_a_band_counts_cells_not_characters() -> None:
    """A CJK title takes two cells each, and a band measured in characters
    runs past the last column: `FakeScreen` refuses that, and so does curses."""
    from gentoo_install.tui.widgets import band
    from gentoo_install.i18n import width

    screen = FakeScreen(columns=30)
    # Two wide characters each: four cells, not two. Written as escapes
    # because a literal here would be unreadable to half the contributors.
    band(screen, 0, "\u5b89\u88dd", "\u5b8c\u6210")

    assert width(screen.drawn(0)) == 30


def test_a_field_refuses_a_character_it_can_never_hold() -> None:
    """Rejecting the submitted form tells the operator they were wrong; the
    field can tell them before they are. A host name never holds a space."""
    from gentoo_install.tui.widgets import Accepts, Field, Form

    screen = FakeScreen(keys=["a", " ", "b", "\t", "\n"], lines=20, columns=60)
    answered = Form(
        title="Proxy",
        fields=[Field(label="host", accepts=Accepts.NO_SPACE)],
        footer="",
    ).run(screen)

    assert answered.unwrap() == ["ab"]


def test_a_port_field_takes_digits_and_nothing_else() -> None:
    from gentoo_install.tui.widgets import Accepts, Field, Form

    screen = FakeScreen(keys=["8", "o", "0", "-", "8", "\t", "\n"], lines=20, columns=60)
    answered = Form(
        title="Proxy",
        fields=[Field(label="port", accepts=Accepts.DIGITS)],
        footer="",
    ).run(screen)

    assert answered.unwrap() == ["808"]


def test_a_field_with_no_rule_takes_what_it_is_given() -> None:
    """Most fields hold prose; a table that refused by default would be a rule
    nobody asked for."""
    from gentoo_install.tui.widgets import Field, Form

    screen = FakeScreen(keys=["a", " ", "b", "\t", "\n"], lines=20, columns=60)
    answered = Form(title="Any", fields=[Field(label="free")], footer="").run(screen)

    assert answered.unwrap() == ["a b"]


#: Two wide characters and a combining acute accent, by codepoint: no CJK
#: literal belongs in the test tree, and only escapes survive a review here.
AN = "\u5b89"
ZHUANG = "\u88dd"
ACUTE = "\u0301"


def test_a_narrow_write_keeps_the_characters_it_placed_beside_a_wide_one() -> None:
    """`FakeScreen` measures every layout test in this file, so a cell grid
    that blanks its own writes hides the defects those tests exist to catch."""
    from gentoo_install.i18n import width

    screen = FakeScreen(columns=10)
    screen.write(0, 0, AN + ZHUANG)
    screen.write(0, 0, "ok")

    assert screen.drawn(0) == "ok" + ZHUANG
    assert width(screen.drawn(0)) == 4


def test_a_write_spanning_two_wide_characters_stays_inside_the_screen() -> None:
    """A repair walking cell by cell blanked a cell the same write had filled,
    and the row then measured five cells on a four-column screen."""
    from gentoo_install.i18n import width

    screen = FakeScreen(columns=4)
    screen.write(0, 1, AN)
    screen.write(0, 0, AN + AN)

    assert screen.drawn(0) == AN + AN
    assert width(screen.drawn(0)) == 4


def test_overwriting_the_first_half_of_a_wide_character_clears_the_second() -> None:
    """The orphaned second cell is empty, so the row measured three cells while
    occupying four and every later column read one place left."""
    from gentoo_install.i18n import width

    screen = FakeScreen(columns=4)
    screen.write(0, 0, AN + ZHUANG)
    screen.write(0, 0, "x")

    assert screen.drawn(0) == "x " + ZHUANG
    assert width(screen.drawn(0)) == 4


def test_a_combining_mark_joins_the_cell_before_it_instead_of_taking_one() -> None:
    """A mark occupies no cell, so advancing by its width left the grid too
    short for the next character and the write raised `IndexError`."""
    from gentoo_install.i18n import width

    screen = FakeScreen(columns=20)
    screen.write(0, 0, "e" + ACUTE + "x")

    assert screen.drawn(0) == "e" + ACUTE + "x"
    assert width(screen.drawn(0)) == 2


class Recording(FakeScreen):
    """A `FakeScreen` that keeps every write, so a test can assert where a
    widget wrote and not only what the row ended up reading."""

    def __init__(self, keys: list[str], lines: int = 24, columns: int = 80) -> None:
        super().__init__(keys=keys, lines=lines, columns=columns)
        self.spans: list[tuple[int, int, str]] = []

    def write(
        self,
        line: int,
        column: int,
        text: str,
        highlight: bool = False,
        style: Style = Style.PLAIN,
    ) -> None:
        self.spans.append((line, column, text))
        super().write(line, column, text, highlight, style)


def cell(screen: FakeScreen, line: int, column: int) -> str:
    """What occupies that column. Walked by cells: indexing the row as a
    string reads one place left for every wide character before it."""
    from gentoo_install.i18n import width

    at = 0
    for character in screen.drawn(line):
        step = width(character)
        if at <= column < at + step:
            return character
        at += step
    return " "


def catalog_labels(tag: str) -> list[str]:
    """Every setting label the main menu can draw, in one language."""
    from gentoo_install.i18n import Catalog
    from gentoo_install.tui.settings import SETTINGS, Setting

    translate = Catalog(tag)

    def walk(rows: tuple[Setting, ...]) -> list[str]:
        found: list[str] = []
        for one in rows:
            found.append(translate(one.label))
            found.extend(walk(one.rows))
        return found

    return walk(SETTINGS)


def pane_rows(count: int = 4, label: str = "Mirrors") -> list[PaneRow[int]]:
    return [
        PaneRow(label=f"{label}{index}", value=index, state="set", detail=(f"line {index}",))
        for index in range(count)
    ]


@pytest.mark.parametrize("tag", ["en", "zh-TW", "zh-CN", "ja", "ko"])
def test_the_panes_are_measured_from_the_catalog_they_will_draw(tag: str) -> None:
    """One rule, five answers, and the terminal is one of its inputs: sizing
    from the label alone left the value a few cells, which `spread` dropped
    whole. Half the width is the ceiling, so the right pane always stands."""
    from gentoo_install.tui.widgets import left_pane_width, right_pane_width

    # A value as long as the ones the menu really draws: `Profile` answers
    # `default/linux/amd64/23.0/systemd`, which is what makes the ceiling bite.
    rows = [(label, "default/linux/amd64/23.0/systemd") for label in catalog_labels(tag)]
    for columns in (80, 120, 200):
        left = left_pane_width(rows, columns)
        assert left <= columns // 2, (tag, columns, left)
        assert right_pane_width(columns, left) + left + 2 == columns
    # A wider terminal gives the labels more room rather than the same 34.
    assert left_pane_width(rows, 200) > left_pane_width(rows, 80)


def test_a_label_wider_than_half_the_terminal_is_cut_and_says_so() -> None:
    """The ceiling is the terminal now rather than a constant, and the right
    pane has to keep standing: a cut name that reads as a whole one is the
    defect, and a label that took the whole width would leave no pane."""
    from gentoo_install.tui.widgets import CUT, SEPARATOR, TwoPane

    screen = Recording(keys=["\x1b"], lines=24, columns=80)
    TwoPane(title="gentoo-install", rows=[PaneRow(label="x" * 90, value=0)]).run(screen)

    # The rows start at line 2 and the frame's left edge stands in the column
    # the single rule used to, which is only drawn between its own corners.
    assert cell(screen, 3, 40) == SEPARATOR
    assert screen.drawn(2)[2:40].rstrip().endswith(CUT), screen.drawn(2)


def test_a_wide_label_is_cut_by_cells_and_not_by_characters() -> None:
    """Sixteen characters are 32 cells, and a cut counted in characters puts
    half of the seventeenth into the separator column."""
    from gentoo_install.i18n import width
    from gentoo_install.tui.widgets import CUT, SEPARATOR, TwoPane

    screen = Recording(keys=["\x1b"], lines=24, columns=80)
    TwoPane(title="gentoo-install", rows=[PaneRow(label=WIDE[0] * 30, value=0)]).run(screen)

    drawn = screen.drawn(2)
    assert CUT in drawn
    assert width(drawn[: drawn.index(CUT) + 1]) <= 40
    assert cell(screen, 3, 40) == SEPARATOR


def test_a_right_pane_line_too_long_for_the_pane_continues_on_the_next() -> None:
    """It was cut with a mark, which loses the end of a mirror address the
    operator has no other way to read. The pane has the height to carry it, so
    a line that does not fit continues instead of ending."""
    from gentoo_install.i18n import width
    from gentoo_install.tui.widgets import TwoPane, right_pane_width

    address = "https://mirror.example.org/gentoo/releases/amd64/autobuilds/latest-stage3"
    screen = Recording(keys=["\x1b"], lines=24, columns=80)
    TwoPane(
        title="gentoo-install",
        rows=[PaneRow(label="Mirrors", value=0, detail=(address,))],
    ).run(screen)

    left = 20
    room = right_pane_width(80, left)
    carried = "".join(screen.drawn(line).split(SEPARATOR)[-2].strip() for line in (3, 4, 5))
    assert address in carried, carried
    # And no line of it runs into the column beside it.
    for line in (3, 4, 5):
        beside = screen.drawn(line).split(SEPARATOR)[-2]
        assert width(beside.strip()) <= room, beside


def test_no_write_lands_on_the_separator_column() -> None:
    """The column between the panes belongs to neither. A label that spills
    into it is what makes the right pane start one column late."""
    from gentoo_install.i18n import width
    from gentoo_install.tui.widgets import SEPARATOR, TwoPane, left_pane_width

    rows = [
        PaneRow(label="x" * 40, value=0, state="set", detail=("y" * 90,)),
        PaneRow(label="Mirrors", value=1, state="not set", detail=("z" * 90,)),
    ]
    screen = Recording(keys=["KEY_DOWN", "\x1b"])
    TwoPane(title="gentoo-install", rows=rows, footer="[enter] open").run(screen)
    left = left_pane_width(((row.label, row.state) for row in rows), 80)

    # The title and the status line are full width by definition, and so are
    # the frame's own top and bottom edges: the claim is about the rows
    # between them, where a label spilling into the frame is the defect.
    lines, _ = screen.size()
    body = [span for span in screen.spans if 3 <= span[0] <= lines - 3]
    assert body
    for line, column, text in body:
        if text == SEPARATOR and column in (left, screen.columns - 1):
            continue
        assert column + width(text) <= left or column > left, (line, column, text)
    # The frame's corners sit at its top and bottom; the edge between them is
    # the column the single rule used to hold.
    lines, _ = screen.size()
    assert cell(screen, 2, left) == "+"
    assert cell(screen, lines - 2, left) == "+"
    for line in range(3, lines - 2):
        assert cell(screen, line, left) == SEPARATOR


def test_below_the_floor_one_pane_carries_the_cursor_lines_in_a_band() -> None:
    """79 columns and 23 lines are each below the only size guaranteed to
    exist, and the right pane has nowhere to stand.

    The lines sit in a band above the status line rather than between the
    rows: pushed in there they moved every row under the cursor, so walking
    the list made the interface jump and hid the row below.
    """
    from gentoo_install.tui.widgets import SEPARATOR, TwoPane

    rows = [
        PaneRow(label="Mirrors", value=0, state="set", detail=("first", "second", "third")),
        PaneRow(label="Kernel", value=1, state="set", detail=("other",)),
    ]
    for lines, columns in ((24, 79), (23, 80)):
        screen = Recording(keys=["\x1b"], lines=lines, columns=columns)
        TwoPane(title="gentoo-install", rows=rows).run(screen)

        # The rows keep their places, one after the other.
        assert "Mirrors" in screen.drawn(2)
        assert "Kernel" in screen.drawn(3)
        # The cursor's lines are the last two before the status line, under a
        # rule; the third does not fit and is not drawn.
        assert screen.drawn(lines - 2).strip() == "second"
        assert screen.drawn(lines - 3).strip() == "first"
        assert set(screen.drawn(lines - 4).strip()) == {"-"}
        assert "third" not in screen.last
        assert SEPARATOR not in screen.last


def test_a_screen_too_small_for_a_row_draws_what_fits_and_raises_nothing() -> None:
    """A serial console that came up at 80x10, and a resize down to nothing:
    neither is an error the operator can act on."""
    from gentoo_install.tui.widgets import TwoPane

    for lines, columns in ((10, 40), (4, 20), (2, 12), (1, 8)):
        screen = Recording(keys=["\x1b"], lines=lines, columns=columns)
        assert TwoPane(title="gentoo-install", rows=pane_rows()).run(screen).outcome is Outcome.BACK


def test_the_cursor_moves_and_enter_answers_with_the_row_under_it() -> None:
    from gentoo_install.tui.widgets import TwoPane

    rows = pane_rows()
    assert TwoPane(title="t", rows=rows).run(FakeScreen(keys=["KEY_DOWN", "\n"])).unwrap() == 1
    moved = TwoPane(title="t", rows=rows)
    assert moved.run(FakeScreen(keys=["KEY_DOWN", "KEY_DOWN", "KEY_UP", "KEY_RIGHT"])).unwrap() == 1
    # Stops rather than wraps: wrapping past either end loses the operator's
    # place in a list of twenty-two rows.
    assert TwoPane(title="t", rows=rows).run(FakeScreen(keys=["KEY_UP", "\n"])).unwrap() == 0


def test_left_and_escape_both_answer_back_and_the_caller_decides() -> None:
    """One meaning for `KEY_LEFT` at every depth. What Back means here is the
    main menu's business: it is where the run is ended."""
    from gentoo_install.tui.widgets import TwoPane

    rows = pane_rows()
    assert TwoPane(title="t", rows=rows).run(FakeScreen(keys=["KEY_LEFT"])).outcome is Outcome.BACK
    assert TwoPane(title="t", rows=rows).run(FakeScreen(keys=["\x1b"])).outcome is Outcome.BACK
    interrupted = TwoPane(title="t", rows=rows).run(FakeScreen(keys=["\x03"]))
    assert interrupted.outcome is Outcome.CANCELLED


def test_the_title_row_carries_the_counter_at_the_right_edge() -> None:
    from gentoo_install.i18n import width
    from gentoo_install.tui.widgets import TwoPane

    screen = Recording(keys=["\x1b"])
    TwoPane(title="gentoo-install", rows=pane_rows(), counter="6/22 answered").run(screen)

    title = screen.drawn(0)
    assert width(title) == 80
    assert title.strip().startswith("gentoo-install")
    assert title.strip().endswith("6/22 answered")


def test_the_status_line_stays_on_the_last_line_of_both_layouts() -> None:
    from gentoo_install.tui.widgets import TwoPane

    for lines, columns in ((24, 80), (23, 80)):
        screen = Recording(keys=["\x1b"], lines=lines, columns=columns)
        TwoPane(
            title="gentoo-install",
            rows=pane_rows(),
            footer="[enter] open",
            legend="* required",
        ).run(screen)

        assert screen.drawn(lines - 1).startswith("[enter] open")
        assert screen.drawn(lines - 1).endswith("* required")


def test_the_left_pane_fills_every_row_between_the_title_and_the_status_line() -> None:
    """Twenty-one rows fit under a title and above a status line on 24 lines,
    and a pane that stops one row early hides the row it stops on."""
    from gentoo_install.tui.widgets import TwoPane

    rows = [PaneRow(label=f"row{index:02d}", value=index) for index in range(21)]
    screen = Recording(keys=["\x1b"])
    TwoPane(title="gentoo-install", rows=rows, footer="[enter] open").run(screen)

    assert screen.drawn(2).strip().startswith("row00")
    assert screen.drawn(22).strip().startswith("row20")
    assert screen.drawn(23).startswith("[enter] open")


def test_a_list_longer_than_the_pane_scrolls_to_keep_the_cursor_on_screen() -> None:
    from gentoo_install.tui.widgets import TwoPane

    rows = [PaneRow(label=f"row{index:02d}", value=index) for index in range(40)]
    screen = Recording(keys=["KEY_DOWN"] * 30 + ["\n"])
    assert TwoPane(title="gentoo-install", rows=rows).run(screen).unwrap() == 30
    assert "row30" in screen.last


def test_the_footer_names_the_key_that_always_goes_back() -> None:
    """Left is the only Back every widget takes: backspace deletes a character
    in a field that has one, and escape ends the run. A footer that named
    neither left it to the operator to guess, and the walk that found this
    spent eighteen rows deleting the hostname one letter at a time."""
    from gentoo_install.i18n import Catalog
    from gentoo_install.tui.context import footer
    from gentoo_install.tui.widgets import Field, Form, TextField

    for tag in ("en", "zh-TW", "zh-CN", "ja", "ko"):
        translate = Catalog(tag)
        drawn = footer(translate)
        # Three keys and no Cancel, in one entry: three entries reading the
        # same word filled half the line and read as three different things.
        # Escape steps back below the main menu, which is where ending the run
        # is asked about.
        for key in ("\u2190", "backspace", "esc"):
            assert key in drawn, (tag, key, drawn)
        assert drawn.count(translate("Back")) == 1, (tag, drawn)
        assert translate("Cancel") not in drawn, (tag, drawn)

    # And the key the footer names is the key the widgets answer Back to,
    # with a value already in the field.
    typed = TextField(title="Hostname", value="gentoo")
    assert typed.run(FakeScreen(keys=["KEY_LEFT"])).outcome is Outcome.BACK
    form = Form(title="User", fields=[Field(label="Name", value="zakk")])
    assert form.run(FakeScreen(keys=["KEY_LEFT"])).outcome is Outcome.BACK


def test_the_field_keeps_the_end_of_what_was_typed_where_the_caret_is() -> None:
    from gentoo_install.tui.widgets import _tail_that_fits

    assert _tail_that_fits("gentoo", 6) == "gentoo"
    assert _tail_that_fits("gentoo", 3) == "too"
    # A wide character owns two cells, so three cells hold one of them and not
    # a half of the other.
    assert _tail_that_fits("\u4e2d\u6587", 3) == "\u6587"


def test_a_terminal_narrower_than_the_brackets_still_redraws() -> None:
    """8 columns leaves the field no room at all, and dropping a character off
    an empty string never makes it shorter: the trim has to end on the string.
    """
    from gentoo_install.tui.widgets import TextField, _tail_that_fits

    assert _tail_that_fits("gentoo", 0) == ""
    assert _tail_that_fits("gentoo", -1) == ""
    screen = FakeScreen(keys=["g", "\x1b"], lines=24, columns=8)
    assert TextField(title="hostname").run(screen).outcome is Outcome.BACK


def test_every_widget_draws_at_the_floor_the_interface_refuses_below() -> None:
    """The floor is measured, not chosen: below it a widget drops content off
    the screen, and the operator gets a wrecked menu instead of a message."""
    from gentoo_install.tui.widgets import (
        MINIMUM_COLUMNS,
        MINIMUM_LINES,
        Accepts,
        Confirm,
        Field,
        Form,
        Menu,
        MultipleChoiceMenu,
        TextField,
        TwoPane,
    )

    items = [Item(label=f"choice {index}", value=index, detail="what it means") for index in range(6)]
    field = Field(label="Address", accepts=Accepts.NO_SPACE)
    drawn = (
        lambda screen: Menu(title="gentoo-install", items=items, footer="[enter] pick").run(screen),
        lambda screen: MultipleChoiceMenu(title="t", items=items).run(screen),
        lambda screen: TextField(title="hostname", value="gentoo", detail="the name").run(screen),
        lambda screen: Confirm(title="erase", phrase="vda", detail="type it").run(screen),
        lambda screen: TwoPane(title="gentoo-install", rows=pane_rows(), footer="[enter]").run(screen),
        lambda screen: Form(title="net", fields=[field]).run(screen),
    )
    for widget in drawn:
        screen = FakeScreen(keys=["\x1b"] * 8, lines=MINIMUM_LINES, columns=MINIMUM_COLUMNS)
        assert widget(screen).outcome is Outcome.BACK

    # 7x5 is the narrowest any of them can still put its content on, measured
    # on 2026-08-21. The floor stands above that, so nothing is drawing off
    # the edge at the size the interface agrees to run in.
    assert MINIMUM_COLUMNS >= 7 and MINIMUM_LINES >= 5


def test_one_pane_below_the_two_pane_floor_and_two_at_it() -> None:
    """The boundary the interface actually crosses, not a constant compared
    with itself: 79 columns is one pane and 80 is two."""
    from gentoo_install.tui.widgets import (
        SEPARATOR,
        TWO_PANE_COLUMNS,
        TWO_PANE_LINES,
        TwoPane,
    )

    wide = Recording(keys=["\x1b"], lines=TWO_PANE_LINES, columns=TWO_PANE_COLUMNS)
    TwoPane(title="gentoo-install", rows=pane_rows()).run(wide)
    assert SEPARATOR in wide.last

    for lines, columns in ((TWO_PANE_LINES, TWO_PANE_COLUMNS - 1), (TWO_PANE_LINES - 1, TWO_PANE_COLUMNS)):
        narrow = Recording(keys=["\x1b"], lines=lines, columns=columns)
        TwoPane(title="gentoo-install", rows=pane_rows()).run(narrow)
        assert SEPARATOR not in narrow.last
        assert "Mirrors0" in narrow.drawn(2)


def test_a_title_too_wide_for_the_row_is_cut_with_a_mark() -> None:
    """`spread` cut the left side with no mark, so a mirror name that lost its
    end read exactly like one that never had it."""
    from gentoo_install.i18n import CUT, width
    from gentoo_install.tui.widgets import spread

    row = spread("Mirrors and the site every fetch goes to", "12/20", 20)
    assert width(row) == 20
    assert row.endswith(CUT), row
    # Nothing is cut when it fits, and the right side still lands at the end.
    assert spread("Mirrors", "12/20", 20) == "Mirrors" + " " * 8 + "12/20"


def test_one_mark_says_a_line_was_cut() -> None:
    """`partitions.py` carried its own mark and its own cut beside `clip`, so
    one rule lived in two places and only one of them was measured."""
    from pathlib import Path

    from gentoo_install.i18n import CUT

    carrying = sorted(
        f"{path}:{number}"
        for path in Path("gentoo_install/tui").rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if CUT in line
    )
    assert carrying == [], carrying


def test_a_list_answers_the_help_key_and_the_footer_names_it() -> None:
    """`KEY_LEFT` went back from every screen and no status line named it, so
    a key the operator cannot find is treated here as a key that is missing.
    """
    from gentoo_install.i18n import Catalog
    from gentoo_install.tui.context import footer
    from gentoo_install.tui.widgets import HELP_KEY, Menu, TwoPane

    screen = FakeScreen(keys=[HELP_KEY, "\x1b"])
    Menu(title="Mirrors", items=[Item(label="nju", value=0)]).run(screen)
    assert screen.helped == 1

    pane = FakeScreen(keys=[HELP_KEY, HELP_KEY, "\x1b"])
    TwoPane(title="gentoo-install", rows=pane_rows()).run(pane)
    assert pane.helped == 2

    assert f"[{HELP_KEY}]" in footer(Catalog("en"))


def test_the_help_page_names_every_key_a_widget_answers() -> None:
    """One table, and it is read against the widgets rather than trusted: a
    page listing a key nothing answers is worse than no page."""
    import ast
    import inspect

    from gentoo_install.tui import widgets

    listed = "  ".join(row.keys for row in widgets.KEY_HELP)
    for named in ("enter", "j", "k", "tab", "space", "backspace", "esc", "q", "ctrl-c", "?"):
        assert named in listed, named

    # Every string literal a widget compares a key press against, so a key
    # added to a widget and not to the table fails here.
    source = ast.parse(inspect.getsource(widgets))
    answered: set[str] = set()
    for node in ast.walk(source):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id != "pressed":
                continue
            for other in node.comparators:
                for constant in ast.walk(other):
                    if isinstance(constant, ast.Constant) and isinstance(constant.value, str):
                        answered.add(constant.value)
    # The names curses gives them, mapped to what the table writes.
    written = {
        "KEY_UP": "\u2191", "KEY_DOWN": "\u2193", "KEY_LEFT": "\u2190",
        "KEY_RIGHT": "enter", "KEY_ENTER": "enter", "KEY_BACKSPACE": "backspace",
        "KEY_BTAB": "shift-tab", "\x1b[Z": "shift-tab", "\t": "tab", "\n": "enter",
        "\x1b": "esc", "\x7f": "backspace", "\x03": "ctrl-c", " ": "space",
    }
    for key in answered:
        assert written.get(key, key) in listed, key


def test_a_long_list_moves_one_screen_and_to_its_ends() -> None:
    """169 timezones live under `America`, which is eight screens of `j` on a
    console 24 lines tall."""
    from gentoo_install.tui.widgets import Menu

    items = [Item(label=f"zone {index}", value=index) for index in range(169)]

    # A page is what this screen draws, not a constant: the title, a blank, the
    # rows and the status line.
    for lines, page in ((24, 20), (12, 8)):
        screen = FakeScreen(keys=["KEY_NPAGE", "\n"], lines=lines, columns=80)
        assert Menu(title="Timezone", items=items).run(screen).unwrap() == page

    down = FakeScreen(keys=["KEY_NPAGE", "KEY_NPAGE", "KEY_PPAGE", "\n"], lines=24, columns=80)
    assert Menu(title="Timezone", items=items).run(down).unwrap() == 20

    # The ends, and neither runs off: paging past the last row stops on it.
    assert Menu(title="Timezone", items=items).run(
        FakeScreen(keys=["KEY_END", "\n"], lines=24, columns=80)
    ).unwrap() == 168
    assert Menu(title="Timezone", items=items).run(
        FakeScreen(keys=["G", "g", "\n"], lines=24, columns=80)
    ).unwrap() == 0
    assert Menu(title="Timezone", items=items).run(
        FakeScreen(keys=[*(["KEY_NPAGE"] * 20), "\n"], lines=24, columns=80)
    ).unwrap() == 168


def test_the_main_menu_pages_the_same_way() -> None:
    """One key table, so the two-pane list answers what a menu answers."""
    from gentoo_install.tui.widgets import TwoPane

    rows = pane_rows(count=60)
    paged = Recording(keys=["KEY_NPAGE", "\n"], lines=24, columns=80)
    assert TwoPane(title="gentoo-install", rows=rows).run(paged).unwrap() == 21
    ended = Recording(keys=["KEY_END", "\n"], lines=24, columns=80)
    assert TwoPane(title="gentoo-install", rows=rows).run(ended).unwrap() == 59


def test_typing_after_the_filter_key_narrows_a_long_list() -> None:
    """169 timezones under `America`, and the operator knows the name: `/` and
    three letters beat eight screens of paging."""
    from gentoo_install.tui.widgets import FILTER_KEY, Menu

    zones = ["Asia/Shanghai", "Asia/Taipei", "Europe/Berlin", "America/Shanghai_Falls"]
    items = [Item(label=zone, value=zone) for zone in zones]

    # Two rows hold `sha`, and the cursor lands on the first of them.
    screen = Recording(keys=[FILTER_KEY, "s", "h", "a", "\n"], lines=24, columns=80)
    assert Menu(title="Timezone", items=items).run(screen).unwrap() == "Asia/Shanghai"
    assert "Europe/Berlin" not in screen.last
    assert "Asia/Taipei" not in screen.last
    # What was typed and how many rows are left, where the keys usually are.
    assert f"{FILTER_KEY}sha" in screen.last and "2" in screen.drawn(23)

    # Case does not matter: the label is read, not matched.
    upper = Recording(keys=[FILTER_KEY, "B", "E", "R", "\n"], lines=24, columns=80)
    assert Menu(title="Timezone", items=items).run(upper).unwrap() == "Europe/Berlin"

    # Backspace widens it again and the rows it had hidden come back, while
    # the cursor stays on the row the operator had narrowed down to.
    wider = Recording(keys=[FILTER_KEY, "t", "a", "i", "\x7f", "\x7f", "\x7f", "\n"],
                      lines=24, columns=80)
    assert Menu(title="Timezone", items=items).run(wider).unwrap() == "Asia/Taipei"
    assert "Europe/Berlin" in wider.last


def test_escape_closes_the_filter_before_it_goes_back() -> None:
    """`esc` is Back everywhere else, so the first one has to be spent on the
    filter or an operator who mistyped cannot get the whole list back."""
    from gentoo_install.tui.widgets import FILTER_KEY, Menu

    items = [Item(label=name, value=name) for name in ("nju", "tuna", "ustc")]
    screen = Recording(keys=[FILTER_KEY, "n", "j", "\x1b", "\n"], lines=24, columns=80)
    assert Menu(title="Mirrors", items=items).run(screen).unwrap() == "nju"
    assert "ustc" in screen.last, screen.last

    # And the second one leaves.
    away = Recording(keys=[FILTER_KEY, "n", "\x1b", "\x1b"], lines=24, columns=80)
    assert Menu(title="Mirrors", items=items).run(away).outcome is Outcome.BACK


def test_a_filter_that_matches_nothing_says_so_rather_than_drawing_nothing() -> None:
    from gentoo_install.tui.widgets import FILTER_KEY, Menu

    items = [Item(label=name, value=name) for name in ("nju", "tuna")]
    screen = Recording(keys=[FILTER_KEY, "z", "z", "\x1b", "\x1b"], lines=24, columns=80)
    assert Menu(title="Mirrors", items=items).run(screen).outcome is Outcome.BACK


def test_every_field_says_what_it_takes_before_it_refuses_one() -> None:
    """`code` is a package name everywhere else and an atom needs a category,
    and the field said so only by rejecting it. A field the operator has to
    guess at is the defect; a password field is not, because the shape of a
    password is the operator's own."""
    import ast
    from pathlib import Path

    #: Fields whose content the operator already holds, where a hint would be
    #: an instruction about their own secret or a name only they know.
    THEIR_OWN = {
        "Password",
        "Type it again",
        "sudo",
        "Proxy password",
        "User name",
        "Bypass hosts",
    }
    bare: list[str] = []
    for path in sorted(Path("gentoo_install").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") not in {"TextField", "Field"}:
                continue
            named = {word.arg for word in node.keywords}
            if named & {"detail", "placeholder"} or "secret" in named or "toggle" in named:
                continue
            title = next(
                (
                    str(word.value.args[0].value)
                    for word in node.keywords
                    if word.arg in {"title", "label"}
                    and isinstance(word.value, ast.Call)
                    and word.value.args
                    and isinstance(word.value.args[0], ast.Constant)
                ),
                "",
            )
            if title in THEIR_OWN:
                continue
            bare.append(f"{path}:{node.lineno} {title or ast.unparse(node)[:40]}")
    # Two answer with a variable title, built by the screen that opens them;
    # both carry their own line above the field.
    assert len(bare) <= 3, bare


def test_the_box_closes_at_the_same_column_on_both_edges() -> None:
    """The top edge was a cell wider than the pane and lost its corner.

    Read off a real guest at 120x40: the bottom edge ended in `+`, the top in
    the ellipsis `clip` leaves behind, so the frame the operator saw had no
    right-hand corner at all. A wide title made it worse by two cells.
    """
    from gentoo_install.tui.widgets import TwoPane

    for title in ("Mode", WIDE):
        screen = FakeScreen(keys=["\n"], lines=24, columns=120)
        TwoPane(
            title="gentoo-install",
            rows=[PaneRow(label=title, value="vda", state="/dev/vda")],
        ).frame(screen, 0, dimmed=False)
        # Not the pane's own arithmetic: the two edges are read off the cells.
        # From the corner rightwards: the top edge shares its line with the
        # row the cursor is on, so a line that merely starts with the box is
        # not how either edge is found.
        edges = [
            screen.drawn(line)[screen.drawn(line).index("+-") :]
            for line in range(24)
            if "+-" in screen.drawn(line)
        ]
        assert len(edges) == 2, edges
        top, bottom = edges[0].rstrip(), edges[-1].rstrip()
        assert top.endswith("+"), (title, top)
        assert width(top) == width(bottom), (title, top, bottom)


def test_right_chooses_in_a_list_because_left_goes_back_from_one() -> None:
    """The main list opened a row on right; a menu ignored it.

    Watched on a guest: the first agent to drive the interface pressed right
    twice inside a menu and nothing happened, because only the two-pane list
    took it. Left is Back in both, so right is forward in both.
    """
    assert menu().run(FakeScreen(keys=["KEY_RIGHT"])).unwrap() == "vda"

    # Negative control: where several rows are chosen at once, right would
    # accept the screen on the way to a row the operator meant to mark.
    several = MultipleChoiceMenu(
        title="Locales",
        items=[Item(label="zh_TW", value="zh_TW"), Item(label="en_US", value="en_US")],
    )
    answer = several.run(FakeScreen(keys=["KEY_RIGHT", " ", "\n"]))
    assert answer.unwrap() == ("zh_TW",), answer


def test_a_field_that_arrives_with_a_value_can_be_emptied() -> None:
    """Replacing one meant counting its characters and pressing backspace.

    Read off a guest: an agent asked for a 512 MiB partition, typed it into a
    field already holding `1GiB`, and the field answered `1GiB512MiB`.
    """
    from gentoo_install.tui.widgets import CLEAR_KEY

    screen = FakeScreen(keys=[CLEAR_KEY, "5", "1", "2", "M", "\n"])
    answered = TextField(title="Size", value="1GiB").run(screen)
    assert answered.unwrap() == "512M", answered

    # Negative control: without it the same keys append, which is the value
    # the operator saw.
    appended = TextField(title="Size", value="1GiB").run(
        FakeScreen(keys=["5", "1", "2", "M", "\n"])
    )
    assert appended.unwrap() == "1GiB512M"

    # And the key is not a character: a field that took it as text would lose
    # the one way out of a prefilled value.
    assert not CLEAR_KEY.isprintable()


def test_a_list_that_scrolls_says_where_in_it_the_cursor_is() -> None:
    """A list with a row off the screen read as the whole list.

    Fourteen profiles in thirteen rows of room showed thirteen and nothing
    said the fourteenth existed, so the screen answered a question about
    completeness with a wrong answer.
    """
    items = [Item(label=f"row {at}", value=at) for at in range(14)]

    tight = FakeScreen(keys=["\n"], lines=17, columns=40)
    Menu(title="Portage", items=items).run(tight)
    assert tight.last.splitlines()[0].rstrip().endswith("1/14"), tight.last

    # Negative control: a screen with room for every row says nothing, or the
    # count becomes noise on every list in the interface.
    roomy = FakeScreen(keys=["\n"], lines=30, columns=40)
    Menu(title="Portage", items=items).run(roomy)
    assert roomy.last.splitlines()[0].strip() == "Portage", roomy.last

    # And it follows the cursor, so it says where in the list the operator is
    # rather than only that there is more of it.
    moved = FakeScreen(keys=["KEY_DOWN", "KEY_DOWN", "\n"], lines=17, columns=40)
    Menu(title="Portage", items=items).run(moved)
    assert moved.last.splitlines()[0].rstrip().endswith("3/14"), moved.last


def test_the_box_names_the_row_only_while_it_is_closed() -> None:
    """Opened, the screen inside writes its own title on the first line.

    Read off a guest: the box said `Install mode` and the line one cell below
    it said `Install mode` again, which is a line of a pane spent on a word
    the reader had just read.
    """
    from gentoo_install.tui.widgets import TwoPane

    rows = [PaneRow(label="Install mode", value=0, state="partition a disk")]

    closed = FakeScreen(keys=["\n"], lines=24, columns=110)
    TwoPane(title="gentoo-install", rows=rows).frame(closed, 0, dimmed=False)
    shut = "\n".join(closed.drawn(line) for line in range(24))
    assert "Install mode" in shut.split("+-", 1)[1].split("\n", 1)[0], shut

    # Negative control: with the row open the box is bare, so the name below
    # it is the only one and the pane keeps that line.
    opened = FakeScreen(keys=["\n"], lines=24, columns=110)
    TwoPane(title="gentoo-install", rows=rows).frame(opened, 0, dimmed=True)
    ajar = "\n".join(opened.drawn(line) for line in range(24))
    assert "Install mode" not in ajar.split("+-", 1)[1].split("\n", 1)[0], ajar


def test_a_detail_too_long_for_one_row_keeps_its_end() -> None:
    """The exact string to type is the last thing in the line and the first
    thing a clip removes: an operator whose screen cut the line short typed the
    row's own name into the field instead of the word it was asking for."""
    screen = FakeScreen(keys=["\n"], lines=24, columns=40)
    TextField(
        title="replace the running system",
        detail="Replaced: /bin, /sbin, /etc, /lib, /usr, /var. Type convert to confirm.",
    ).run(screen)
    assert "convert" in screen.last, screen.last

    # Negative control: a detail that fits is drawn on one row, so the rule
    # cannot be adding a row to every screen.
    short = FakeScreen(keys=["\n"], lines=24, columns=40)
    TextField(title="name", detail="convert").run(short)
    assert short.frames[-1][2].strip() == "convert", short.frames[-1][:5]
    assert short.frames[-1][3].strip() == "", short.frames[-1][:5]
