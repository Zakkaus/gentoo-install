# SPDX-License-Identifier: GPL-2.0-or-later
"""What a session cost the operator, counted rather than reported.

An agent's own account of a run is a claim: it says it understood the screen
because it believes it did. These five numbers come from the keys it pressed
and the screens it was shown, so they say the same thing whatever it writes
in its report, and each one names a screen rather than a feeling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gentoo_install.i18n import Catalog

#: A screen the cursor entered and left with nothing changed. Named because
#: it is the one count that means the row's own name failed: the operator
#: opened it to find out what it was.
LOST: Final[str] = "lost"

#: The languages the interface offers, so a screen is matched in the one it
#: was drawn in.
TAGS: Final[tuple[str, ...]] = ("en", "zh-TW", "zh-CN", "ja", "ko")

#: How many keys on one screen before it counts as stuck. Twelve is four rows
#: of movement and a mistake; a screen that takes more is one the operator
#: cannot answer.
STUCK_AFTER: Final[int] = 12


@dataclass(frozen=True)
class Report:
    """One session, in five numbers and the screens behind them."""

    finished: bool
    lost: tuple[str, ...]
    helped: int
    stuck: tuple[str, ...]
    refused: int

    def as_dict(self) -> dict[str, object]:
        return {
            "finished": self.finished,
            "lost": list(self.lost),
            "helped": self.helped,
            "stuck": list(self.stuck),
            "refused": self.refused,
        }


#: What the installer prints when it has run its whole plan. Matched against
#: the guest's own console rather than a screen: the main list always carries
#: a `Start the installation` row, so a session that ended anywhere on that
#: list read as one that had installed, which is a check that cannot fail.
INSTALLED: Final[re.Pattern[str]] = re.compile(r"installed \d+ operations into")


def _installed(session: Path) -> bool:
    """Whether the plan ran, read off the console the guest wrote."""
    for log in sorted(session.glob("*.log")):
        if INSTALLED.search(log.read_bytes().decode("utf-8", "replace")):
            return True
    return False


def title_of(screen: str) -> str:
    """The framed pane's own heading, which names the row that is open.

    The title bar says `gentoo-install` on every screen, so it identifies
    nothing; the frame names what the operator is looking at.
    """
    found = re.search(r"\+-\s+(.+?)\s+-{2,}", screen)
    if found:
        return found.group(1).strip()
    first = screen.splitlines()[0].strip() if screen.splitlines() else ""
    return first


def read(session: Path) -> Report:
    """Count one session from what it left behind."""
    keys = _lines(session / "keys.txt")
    screens = _screens(session / "screens.txt")
    return Report(
        finished=_installed(session),
        lost=tuple(_lost(keys, screens)),
        helped=sum(one.split().count("help") for one in keys),
        stuck=tuple(_stuck(screens)),
        refused=sum(1 for one in screens if _refusal(one)),
    )


def _lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [one for one in path.read_text(encoding="utf-8").splitlines() if one.strip()]


def _screens(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [one for one in path.read_text(encoding="utf-8").split("\f") if one.strip()]


def _lost(keys: list[str], screens: list[str]) -> list[str]:
    """A screen entered and left with the same values on it."""
    lost: list[str] = []
    for at in range(1, len(screens) - 1):
        before, inside, after = screens[at - 1], screens[at], screens[at + 1]
        entered = "enter" in keys[at - 1] if at - 1 < len(keys) else False
        left = at < len(keys) and ("esc" in keys[at] or "left" in keys[at])
        if entered and left and _values(before) == _values(after):
            lost.append(title_of(inside))
    return lost


def _stuck(screens: list[str]) -> list[str]:
    """The same screen for more keys than answering one takes."""
    stuck: list[str] = []
    run = 1
    for before, after in zip(screens, screens[1:]):
        if title_of(before) == title_of(after):
            run += 1
            if run == STUCK_AFTER:
                stuck.append(title_of(after))
        else:
            run = 1
    return stuck


def _values(screen: str) -> tuple[str, ...]:
    """Every row as drawn, label and value together.

    The value is what an edit changes, so comparing the labels answers that
    nothing changed on every screen and makes every visit a lost one.
    """
    return tuple(
        line.rstrip()
        for line in screen.splitlines()
        if "|" in line and line[:1] in {"*", "~", " "}
    )


def _refusal(screen: str) -> bool:
    """A field that would not take what was typed."""
    return _says("Not a package name", screen)


def _says(source: str, screen: str) -> bool:
    """Whether a screen carries a string, in whatever language it is drawn in.

    Written against the catalogs rather than as a literal: a session runs in
    the language its spec is in, and a translated word pasted into a test goes
    stale the next time the wording is corrected.
    """
    for tag in TAGS:
        if Catalog(tag)(source) in screen:
            return True
    return False
