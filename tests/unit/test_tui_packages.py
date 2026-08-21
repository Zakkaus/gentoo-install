# SPDX-License-Identifier: GPL-2.0-or-later
"""The package screens, which is where a licence stops an install."""

from __future__ import annotations

from dataclasses import replace

from .test_tui_app import config, context




def test_an_extra_package_says_the_licence_will_stop_the_install() -> None:
    """Portage masks a package whose licence is not accepted, and the operator
    met that an hour into the merge rather than at the field they typed it in.
    """
    from gentoo_install.tui.packages import _licence_warning

    at = context()
    off = config()
    assert tuple(off.portage.accept_license) != ("*",)

    # Nothing typed is nothing to warn about.
    assert _licence_warning((), off, at) == ""

    # An atom this installer ships the licence of is named.
    named = next(
        (group.packages[0] for group in at.groups.values() if group.accept_license),
        "",
    )
    assert named, sorted(at.groups)
    warned = _licence_warning((named,), off, at)
    assert named in warned, warned

    # And one it knows nothing about still gets the rule and where to change it.
    unknown = _licence_warning(("dev-util/codex",), off, at)
    assert unknown and "dev-util/codex" not in unknown, unknown

    # With every licence accepted there is nothing to say.
    every = replace(off, portage=replace(off.portage, accept_license=("*",)))
    assert _licence_warning((named,), every, at) == ""
