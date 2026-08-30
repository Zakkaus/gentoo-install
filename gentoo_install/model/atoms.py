# SPDX-License-Identifier: GPL-2.0-or-later
"""Whether a word the operator typed has the shape its destination needs.

Syntax only. Whether an atom resolves to an ebuild is a question for the
target's repositories, and `plan/portage.py` asks it there once the tree is
synced: the live medium often carries no repository at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

#: `category/name`, with the optional trailing `:slot` and `::repository` that
#: portage accepts. A leading version operator is deliberately not allowed: it
#: needs a version too, and the interface asks for a package.
_CATEGORY: Final[str] = r"[a-z0-9][a-z0-9+._-]*"
_PACKAGE: Final[str] = r"[a-zA-Z0-9+._-]+"
#: Portage's own, from `versions.py`, plus its optional revision. A name
#: ending in one of these is a versioned atom with no operator, which
#: `isvalidatom` refuses and `emerge --pretend` stops the install over.
_VERSION: Final[str] = (
    r"\d+(?:\.\d+)*[a-z]?(?:_(?:pre|p|beta|alpha|rc)\d*)*(?:-r\d+)?"
)
#: One optional subslot, the way `slot_re` in portage's `dep/__init__.py`
#: builds it: `sys-libs/zlib:0/1/2` is an `InvalidAtom` there.
_SLOT: Final[str] = r":[a-zA-Z0-9+._-]+(?:/[a-zA-Z0-9+._-]+)?"
_REPOSITORY: Final[str] = r"::[a-zA-Z0-9_-]+"
_ATOM: Final[re.Pattern[str]] = re.compile(
    rf"^{_CATEGORY}/(?!{_PACKAGE}-{_VERSION}(?=:|$)){_PACKAGE}"
    rf"(?:{_SLOT})?(?:{_REPOSITORY})?$"
)


def looks_like_an_atom(text: str) -> bool:
    return bool(_ATOM.match(text))


_JUST_A_VERSION: Final[re.Pattern[str]] = re.compile(rf"^{_VERSION}$")


def looks_like_a_version(text: str) -> bool:
    """Whether this is a version and nothing else.

    `plan/portage.py` builds `=package-version` and trims the version back off
    to name the package in `--usepkg-exclude`, which takes package names and
    slot atoms only. A version carrying a slot -- `7.1.7-r2:0` -- survives that
    trim, so emerge answers `Invalid Atom(s)` after the disks are written.
    """
    return bool(_JUST_A_VERSION.match(text))


def split(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The atoms in `text`, and the words that are not atoms."""
    return _split(text, looks_like_an_atom)


#: A USE flag as PMS defines it: alphanumeric first, then the four punctuation
#: characters Portage allows. `-` in front turns one off and `-*` clears the
#: inherited set, so both are accepted here and nowhere else.
_USE_FLAG: Final[re.Pattern[str]] = re.compile(r"^-?[A-Za-z0-9][A-Za-z0-9+_@-]*$")

#: What `/etc/default/grub` cannot carry. That file is a shell script sourced
#: by `grub-mkconfig`, and the parameters are written inside a double-quoted
#: assignment, so a quote ends the value early and `$(`, backtick or `\`
#: reaches the shell. The kernel would not accept any of them either.
_SHELL_CHARACTERS: Final[str] = "\"'`$\\"


def looks_like_a_use_flag(text: str) -> bool:
    return text == "-*" or bool(_USE_FLAG.match(text))


def split_use_flags(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The USE flags in `text`, and the words that are not."""
    return _split(text, looks_like_a_use_flag)


def looks_like_a_kernel_parameter(text: str) -> bool:
    """A bare flag or `key=value`. Length is the kernel's problem; what is
    rejected here is anything that would escape the file it is written into."""
    return bool(text) and not any(character in text for character in _SHELL_CHARACTERS)


def split_kernel_parameters(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The kernel parameters in `text`, and the words that cannot be written."""
    return _split(text, looks_like_a_kernel_parameter)


def _split(
    text: str, accepts: Callable[[str], bool]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    good: list[str] = []
    bad: list[str] = []
    for word in text.split():
        (good if accepts(word) else bad).append(word)
    return tuple(good), tuple(bad)
