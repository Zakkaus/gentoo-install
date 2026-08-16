# SPDX-License-Identifier: GPL-2.0-or-later
"""The three layers, checked by reading the source rather than by convention.

`model/` holds data and validation with no I/O, `plan/` derives operations as
pure functions, and `exec/` is the only place that touches the machine. Each
of these was true when it was written and each one drifted once.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

PACKAGE = Path(__file__).resolve().parents[2] / "gentoo_install"

#: What reading a file looks like in an AST. `Path.read_text` and friends are
#: attribute calls; `open` is a bare name.
_READERS = frozenset({"read_text", "read_bytes", "write_text", "write_bytes", "open"})


def _modules(layer: str) -> list[Path]:
    return sorted(one for one in (PACKAGE / layer).rglob("*.py") if one.is_file())


def _calls(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute) and target.attr in _READERS:
            found.append((target.attr, node.lineno))
        elif isinstance(target, ast.Name) and target.id == "open":
            found.append((target.id, node.lineno))
    return found


def test_the_model_layer_opens_no_file() -> None:
    """`load(path)` lived in `model/parse.py`, so the declared boundary was
    false and a model test could not cover configuration loading without a
    real file on the host. Reading a path is I/O; it belongs to `exec/`."""
    offenders: list[str] = []
    for module in _modules("model"):
        for name, line in _calls(ast.parse(module.read_text())):
            offenders.append(f"{module.relative_to(PACKAGE)}:{line} calls {name}")
    assert not offenders, offenders


def test_the_model_layer_runs_no_command() -> None:
    """`exec/runner.py` is the only module allowed to import subprocess."""
    for layer in ("model", "plan"):
        for module in _modules(layer):
            tree = ast.parse(module.read_text())
            imported = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            assert "subprocess" not in imported, module


def test_the_model_layer_imports_nothing_below_it() -> None:
    """Calls go downward only: `model` knows nothing of `plan`, `exec` or the
    interface, and `plan` knows nothing of `exec` or the interface."""
    forbidden = {"model": ("plan", "exec", "tui"), "plan": ("exec", "tui")}
    for layer, below in forbidden.items():
        for module in _modules(layer):
            source = module.read_text()
            for other in below:
                assert f"from ..{other}" not in source, f"{module}: imports {other}"
                assert f"from gentoo_install.{other}" not in source, f"{module}: {other}"


def test_no_suppression_hides_a_finding() -> None:
    """`AGENTS.md` forbids both forms. Seventeen of them once stood between
    strict typing and the runner fakes, the config builders, the replaced
    methods and a forked child's exception channel."""
    import re

    root = PACKAGE.parent
    #: A suppression is a comment that starts one; a comment that quotes the
    #: name while explaining why one was removed is not.
    marker = re.compile(r"#\s*(type:\s*ignore|noqa)\b")
    offenders: list[str] = []
    for module in sorted(root.rglob("*.py")):
        if "/.git/" in str(module) or "__pycache__" in str(module):
            continue
        for number, line in enumerate(module.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#:") or stripped.startswith("#"):
                continue
            if marker.search(line):
                offenders.append(f"{module.relative_to(root)}:{number}")
    assert not offenders, offenders


#: The grant Zakk chose on 2026-08-15. Machine-readable and per file, because
#: `or later` decides what a release carrying borrowed code goes out under.
SPDX: Final[str] = "# SPDX-License-Identifier: GPL-2.0-or-later"


def test_every_source_file_states_the_licence() -> None:
    """The repository is GPL-2 or later. A file that says nothing leaves the
    question to whoever finds it, and the `or later` clause is what lets code
    be taken from a GPL-3 project at all."""
    root = PACKAGE.parent
    silent = [
        one.relative_to(root)
        for one in sorted(root.rglob("*.py"))
        if one.is_file()
        and ".git" not in one.parts
        and "lab" not in one.parts
        and one.read_text(encoding="utf-8").splitlines()[:1] != [SPDX]
    ]

    assert not silent, f"no licence on the first line of: {silent}"


def test_no_definition_in_the_package_is_unreachable() -> None:
    """A rebase left `RemoveUnbootableKernels` and its five helpers in the tree
    after the operation it replaced them with had landed: the class was there,
    nothing built it, and nothing tested it."""
    import ast
    import re

    root = Path(__file__).resolve().parents[2]
    everything = "\n".join(
        path.read_text()
        for where in ("gentoo_install", "tests")
        for path in sorted((root / where).rglob("*.py"))
    )
    orphans: list[str] = []
    for path in sorted((root / "gentoo_install").rglob("*.py")):
        lines = path.read_text().splitlines()
        for node in ast.parse(path.read_text()).body:
            named: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                named = [node.name]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                named = [node.target.id]
            elif isinstance(node, ast.Assign):
                named = [one.id for one in node.targets if isinstance(one, ast.Name)]
            # The definition's own text is not a reference to it: a recursive
            # helper names itself, so counting the whole tree accepted one that
            # nothing calls.
            own = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            for name in named:
                if name.startswith("__"):
                    continue
                pattern = rf"\b{re.escape(name)}\b"
                outside = len(re.findall(pattern, everything)) - len(
                    re.findall(pattern, own)
                )
                if outside == 0:
                    orphans.append(f"{path.relative_to(root)}:{node.lineno} {name}")
    assert orphans == [], orphans


BANNED_WORDS = (
    "simply", "note that", "basically", "obviously",
    "powerful", "robust", "seamlessly", "leverage", "utilize",
)


def test_no_source_file_uses_a_banned_filler_word() -> None:
    """The list is in CLAUDE.md and had five hits when it was first run: a
    comment that says a thing is simple says nothing about the thing."""
    import re

    root = Path(__file__).resolve().parents[2]
    pattern = re.compile("|".join(rf"\b{word}\b" for word in BANNED_WORDS), re.IGNORECASE)
    found: list[str] = []
    for path in sorted((root / "gentoo_install").rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                found.append(f"{path.relative_to(root)}:{number} {line.strip()[:60]}")
    assert found == [], found
