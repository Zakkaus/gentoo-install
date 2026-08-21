# SPDX-License-Identifier: GPL-2.0-or-later
"""The screen an agent reads, rebuilt from what the interface wrote."""

from __future__ import annotations

from gentoo_install.i18n import width

from tests.tui.screen import Screen


def test_the_last_write_to_a_cell_is_what_is_on_screen() -> None:
    """`ncurses` repaints by moving the cursor and writing over cells, so the
    same row appears many times in the stream. Reading the stream instead of
    the screen shows every draft of the menu at once."""
    grid = Screen(lines=4, columns=20)
    grid.feed(b"\x1b[1;1Hfirst\x1b[1;1Hsecond")
    assert grid.text().splitlines()[0] == "second"


def test_a_wide_character_owns_two_cells() -> None:
    """Counting it as one put every column after a Chinese label one cell to
    the left, and the pane's right edge landed inside the text."""
    grid = Screen(lines=2, columns=12)
    # Two wide characters fill cells 0 to 3, so the escape that names column
    # 7 lands two cells further on. Counting them as one puts it four on.
    grid.feed("\u5206\u5340\x1b[1;7H|".encode())
    row = grid.text().splitlines()[0]
    assert row == "\u5206\u5340  |", repr(row)
    assert width(row) == 7


def test_a_band_is_the_row_drawn_in_reverse_video() -> None:
    """What a section heading is: the agent finds them without knowing what
    the interface calls its sections."""
    grid = Screen(lines=3, columns=10)
    grid.feed(b"\x1b[1;1H\x1b[7m   Disks  \x1b[m\x1b[2;1Hplain")
    assert grid.banded() == ["Disks"]


def test_an_erase_clears_what_it_names_and_no_more() -> None:
    grid = Screen(lines=3, columns=8)
    grid.feed(b"\x1b[1;1Habcdefgh\x1b[2;1Hijkl\x1b[1;4H\x1b[K")
    rows = grid.text().splitlines()
    assert rows[0] == "abc"
    assert rows[1] == "ijkl"


def test_an_escape_it_does_not_know_draws_nothing() -> None:
    """A stray `[` in the middle of a menu row is worse than a missing
    colour, so an unknown escape is dropped rather than printed."""
    grid = Screen(lines=2, columns=12)
    grid.feed(b"\x1b(B\x1b=ok\x1b>\x1b]0;title\x07!")
    assert grid.text().splitlines()[0] == "ok!"


def test_a_character_split_across_two_reads_is_one_character() -> None:
    """A read boundary falls wherever the network put it.

    Decoding each chunk on its own turned one three-byte character into three
    replacement marks, and the guest at 120x40 drew three of them where the
    interface had written the second half of a word.
    """
    from tests.tui.screen import Screen

    # `\u53d6\u4ee3`: a literal here would be unreadable to a contributor who
    # does not read Chinese, and the tree carries none.
    wide = "\u53d6\u4ee3".encode()
    grid = Screen(lines=4, columns=20)
    grid.feed(wide[:4])
    grid.feed(wide[4:])
    assert grid.text().splitlines()[0] == "\u53d6\u4ee3", grid.text()

    # Negative control: the same bytes in one chunk, so a test that only ever
    # sees whole characters cannot be what is passing above.
    whole = Screen(lines=4, columns=20)
    whole.feed(wide)
    assert whole.text().splitlines()[0] == "\u53d6\u4ee3"


def test_an_escape_split_across_two_reads_does_not_reach_a_row() -> None:
    """The letter that ends a sequence is drawn as text when the escape is cut.

    `\\x1b[2` and `J` arriving separately used to clear nothing and put a `J`
    into the row, which reads as a value the operator never typed.
    """
    from tests.tui.screen import Screen

    grid = Screen(lines=4, columns=20)
    grid.feed(b"ab\x1b[")
    grid.feed(b"2J")
    assert "J" not in grid.text(), grid.text()

    # Negative control: a `J` that is not part of an escape still lands.
    plain = Screen(lines=4, columns=20)
    plain.feed(b"aJ")
    assert plain.text().splitlines()[0] == "aJ"


def test_a_screen_is_answered_only_once_the_guest_stops_drawing() -> None:
    """Read during a repaint the grid holds half of each of two layouts.

    A real read at 120x40 showed three rows twice and no section headings:
    a page the interface never drew, which an operator reports as a defect
    in the interface rather than in the harness.
    """
    from tests.tui.screen import Screen
    from tests.tui.session import _settled

    class Repainting:
        """A console that delivers one redraw across three reads."""

        def __init__(self) -> None:
            self.chunks = [b"\x1b[2Jfirst", b" second", b" third", b""]

        def read_available(self, seconds: float) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

    grid = Screen(lines=4, columns=40)
    assert _settled(grid, Repainting()).splitlines()[0] == "first second third"

    # Negative control: one read of the same console stops at the first chunk,
    # which is the torn page this exists to prevent.
    torn = Screen(lines=4, columns=40)
    console = Repainting()
    torn.feed(console.read_available(0.25))
    assert torn.text().splitlines()[0] == "first"
