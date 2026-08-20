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
    Style,
    TextField,
)

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
    form = Form(title="t", fields=[Field(label="a")])
    assert form.run(FakeScreen(keys=["\x1b"])).outcome is Outcome.CANCELLED


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
    [("KEY_BACKSPACE", Outcome.BACK), ("\x1b", Outcome.CANCELLED)],
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
    assert drawn.startswith("gentoo-install")
    assert drawn.endswith("6/22 answered")
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


@pytest.mark.parametrize(
    "tag,left,right",
    [("en", 27, 51), ("zh-TW", 20, 58), ("zh-CN", 20, 58), ("ja", 32, 46), ("ko", 26, 52)],
)
def test_the_panes_are_measured_from_the_catalog_they_will_draw(
    tag: str, left: int, right: int
) -> None:
    """One rule, five answers. A pane sized for English cuts every Japanese
    label, and one sized for Japanese leaves 46 columns for a device path."""
    from gentoo_install.tui.widgets import left_pane_width, right_pane_width

    assert left_pane_width(catalog_labels(tag)) == left
    assert right_pane_width(80, left) == right
    assert left + right + 2 == 80


def test_a_label_wider_than_the_clamp_is_cut_and_says_so() -> None:
    """No catalog reaches it today, so the boundary is held by a test rather
    than by the input: a cut name that reads as a whole one is the defect."""
    from gentoo_install.tui.widgets import CUT, SEPARATOR, TwoPane

    screen = Recording(keys=["\x1b"])
    TwoPane(title="gentoo-install", rows=[PaneRow(label="x" * 40, value=0)]).run(screen)

    assert screen.drawn(2)[2:34] == "x" * 31 + CUT
    assert cell(screen, 2, 34) == SEPARATOR


def test_a_wide_label_is_cut_by_cells_and_not_by_characters() -> None:
    """Sixteen characters are 32 cells, and a cut counted in characters puts
    half of the seventeenth into the separator column."""
    from gentoo_install.i18n import width
    from gentoo_install.tui.widgets import CUT, SEPARATOR, TwoPane

    screen = Recording(keys=["\x1b"])
    TwoPane(title="gentoo-install", rows=[PaneRow(label=WIDE[0] * 20, value=0)]).run(screen)

    drawn = screen.drawn(2)
    assert CUT in drawn
    assert width(drawn[: drawn.index(CUT) + 1]) <= 34
    assert cell(screen, 2, 34) == SEPARATOR


def test_a_right_pane_line_too_long_for_the_pane_is_cut_and_says_so() -> None:
    """A truncated mirror URL is indistinguishable from a whole one, which is
    why every cut leaves a mark."""
    from gentoo_install.i18n import width
    from gentoo_install.tui.widgets import CUT, TwoPane

    address = "https://mirror.example.org/gentoo/releases/amd64/autobuilds/latest-stage3"
    screen = Recording(keys=["\x1b"])
    TwoPane(
        title="gentoo-install",
        rows=[PaneRow(label="Mirrors", value=0, detail=(address,))],
    ).run(screen)

    drawn = screen.drawn(2)
    assert address not in drawn
    assert drawn.endswith(CUT)
    assert width(drawn) == 80


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
    left = left_pane_width(row.label for row in rows)

    # The title and the status line are full width by definition, so the
    # claim is about the rows between them.
    body = [span for span in screen.spans if 2 <= span[0] <= 22]
    assert body
    for line, column, text in body:
        if text == SEPARATOR and column == left:
            continue
        assert column + width(text) <= left or column > left, (line, column, text)
    for line in range(2, 23):
        assert cell(screen, line, left) == SEPARATOR


def test_below_the_floor_one_pane_carries_two_lines_under_the_cursor() -> None:
    """79 columns and 23 lines are each below the only size guaranteed to
    exist, and the right pane has nowhere to stand."""
    from gentoo_install.tui.widgets import SEPARATOR, TwoPane

    rows = [
        PaneRow(label="Mirrors", value=0, state="set", detail=("first", "second", "third")),
        PaneRow(label="Kernel", value=1, state="set", detail=("other",)),
    ]
    for lines, columns in ((24, 79), (23, 80)):
        screen = Recording(keys=["\x1b"], lines=lines, columns=columns)
        TwoPane(title="gentoo-install", rows=rows).run(screen)

        assert "Mirrors" in screen.drawn(2)
        assert screen.drawn(3).strip() == "first"
        assert screen.drawn(4).strip() == "second"
        assert "Kernel" in screen.drawn(5)
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
    assert title.startswith("gentoo-install")
    assert title.endswith("6/22 answered")


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
        assert "[←]" in drawn, (tag, drawn)
        assert drawn.count(translate("Back")) == 2, (tag, drawn)

    # And the key the footer names is the key the widgets answer Back to,
    # with a value already in the field.
    typed = TextField(title="Hostname", value="gentoo")
    assert typed.run(FakeScreen(keys=["KEY_LEFT"])).outcome is Outcome.BACK
    form = Form(title="User", fields=[Field(label="Name", value="zakk")])
    assert form.run(FakeScreen(keys=["KEY_LEFT"])).outcome is Outcome.BACK
