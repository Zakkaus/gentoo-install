"""Whether a string looks like a package atom.

Syntax only. Whether the atom resolves to an ebuild is a question for the
target's repositories, and `plan/portage.py` asks it there once the tree is
synced: the live medium often carries no repository at all.
"""

from __future__ import annotations

import re
from typing import Final

#: `category/name`, with the optional trailing `:slot` and `::repository` that
#: portage accepts. A leading version operator is deliberately not allowed: it
#: needs a version too, and the interface asks for a package.
_ATOM: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9][a-z0-9+._-]*/[a-zA-Z0-9+._-]+(:[a-zA-Z0-9+._/-]+)?(::[a-zA-Z0-9_-]+)?$"
)


def looks_like_an_atom(text: str) -> bool:
    return bool(_ATOM.match(text))


def split(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The atoms in `text`, and the words that are not atoms."""
    good: list[str] = []
    bad: list[str] = []
    for word in text.split():
        (good if looks_like_an_atom(word) else bad).append(word)
    return tuple(good), tuple(bad)
