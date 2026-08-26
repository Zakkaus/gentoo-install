# SPDX-License-Identifier: GPL-2.0-or-later
"""Draw every screen in Traditional Chinese and refuse English sentences.

A reason built at runtime cannot be a catalog key, so it reaches a translated
screen in English while the call site still reads `translate(...)`. Rendering
is the only way to see that: `#840` found two such lines in a screenshot, and
every unit test passed with them on screen.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Final

from gentoo_install.i18n import Catalog
from gentoo_install.tui import screens

from .fake_screen import FakeScreen
from .test_tui_app import config, context

#: Three or more lower-case English words in a row, which no device path,
#: package atom or identifier is.
SENTENCE: Final[re.Pattern[str]] = re.compile(r"(?:\b[a-z]{2,}\b[ ,]+){2,}\b[a-z]{2,}\b")

#: What the system itself calls things, which a translation would break. Unix
#: group names are the operator's own `/etc/group`, not prose.
NOT_PROSE: Final[tuple[str, ...]] = ("plugdev kvm docker",)


def test_no_screen_draws_an_english_sentence_in_a_chinese_menu() -> None:
    at = context()
    at.translate = Catalog("zh-TW")
    at.tag = "zh-TW"
    installation = config()
    found: dict[str, set[str]] = {}
    refused: dict[str, str] = {}
    drawn = 0
    walked = 0
    for name, screen_function in sorted(vars(screens).items()):
        if not name.endswith("_screen") or not callable(screen_function):
            continue
        try:
            parameters = list(inspect.signature(screen_function).parameters)
        except (TypeError, ValueError):
            continue
        if parameters[:1] != ["screen"]:
            continue
        walked += 1
        display = FakeScreen(keys=["\x1b"] * 40, lines=40, columns=120)
        answered: Any = None
        try:
            if len(parameters) >= 3:
                answered = screen_function(display, installation, at)
            else:
                answered = screen_function(display, at)
        except Exception as error:
            refused[name] = f"{type(error).__name__}: {error}"
            continue
        del answered
        drawn += 1
        for frame in display.frames:
            for line in frame:
                for hit in SENTENCE.findall(line):
                    hit = hit.strip()
                    if len(hit) > 12 and not any(one in hit for one in NOT_PROSE):
                        found.setdefault(name, set()).add(hit)
    # Every screen this configuration reaches is drawn, so a screen that stops
    # opening is a hole in the check above rather than a screen out of scope:
    # the floor this assertion replaced was 8 against 49 that draw today.
    assert refused == {}, refused
    assert drawn == walked, (drawn, walked)
    assert found == {}, {name: sorted(hits) for name, hits in found.items()}


def test_every_refusal_the_entry_point_can_answer_is_a_catalog_key() -> None:
    """The rendering above reads `context`'s defaults, and `cli.py` fills that
    field on a real machine: `str(error)` there is an English exception message
    on a Chinese screen, and no drawn frame in a test would show it."""
    import ast
    import inspect

    from gentoo_install import cli
    from gentoo_install.model import refusals

    tree = ast.parse(inspect.getsource(cli))
    offers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_conversion_offer", "_image_write_offer"}
    ]
    assert len(offers) == 2, [node.name for node in offers]
    answered: set[str] = set()
    for offer in offers:
        for node in ast.walk(offer):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            parts = node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
            # The `Refusal` itself, wherever it sits: the tuple also carries
            # what was measured and, since the Install row began deriving a
            # conversion's plan, the layout it was read from.
            refusal = [
                one for one in parts if ast.unparse(one).startswith("refusals.")
            ]
            answer = refusal[0] if refusal else parts[-1]
            # `Refusal(REASON, detail)`: the reason is what a catalog holds and
            # the detail is a device or a command, which none does.
            if isinstance(answer, ast.Call) and answer.args:
                answer = answer.args[0]
            answered.add(ast.unparse(answer))
    named = {f"refusals.{name}" for name in dir(refusals) if name.isupper()}
    for reason in answered:
        assert reason in named or reason in {'""', "''"}, (reason, sorted(named))
