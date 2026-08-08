from __future__ import annotations

from gentoo_install.tui.widgets import Answer, Confirm, Item, Menu, Outcome, TextField

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
    assert answer.unwrap() == ["vdb"]


def test_going_back_is_not_cancelling() -> None:
    """Three states, because a caller that treats back as cancel drops the
    operator out of the installer when they meant to fix the previous step."""
    assert menu().run(FakeScreen(keys=["KEY_LEFT"])).outcome is Outcome.BACK
    assert menu().run(FakeScreen(keys=["q"])).outcome is Outcome.CANCELLED
    assert not Answer(Outcome.BACK).chosen


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
    assert excluded.run(screen).unwrap() == ["grub"]
    assert "it only has an EFI implementation" in screen.last


def test_the_cursor_starts_on_a_row_that_can_be_chosen() -> None:
    excluded: Menu[str] = Menu(
        title="Bootloader",
        items=[
            Item(label="systemd-boot", value="sd", disabled_because="EFI only"),
            Item(label="GRUB", value="grub"),
        ],
    )
    assert excluded.run(FakeScreen(keys=["\n"])).unwrap() == ["grub"]


def test_several_answers_are_marked_without_relying_on_a_glyph() -> None:
    """A console with no CJK font and no box-drawing set still has to show
    which rows are selected."""
    several: Menu[str] = Menu(
        title="Applications",
        items=[Item(label="firefox", value="firefox"), Item(label="bluetooth", value="bluetooth")],
        multiple=True,
    )
    screen = FakeScreen(keys=[" ", "KEY_DOWN", " ", "\n"])
    assert several.run(screen).unwrap() == ["firefox", "bluetooth"]
    assert "[x] firefox" in screen.last


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
    assert any("[ 512MiB" in line for line in first), first
    assert any("[ 20G_" in line for line in last), last


def test_a_masked_field_shows_the_caret_and_not_the_characters() -> None:
    field = TextField(title="Passphrase", masked=True)
    screen = FakeScreen(keys=[*"hunter2", "\n"])
    assert field.run(screen).unwrap() == "hunter2"
    drawn = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "hunter2" not in drawn
    assert "[ *******_" in drawn
