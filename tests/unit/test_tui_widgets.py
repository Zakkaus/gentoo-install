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
