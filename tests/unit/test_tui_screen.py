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
