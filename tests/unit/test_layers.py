"""The three layers, checked by reading the source rather than by convention.

`model/` holds data and validation with no I/O, `plan/` derives operations as
pure functions, and `exec/` is the only place that touches the machine. Each
of these was true when it was written and each one drifted once.
"""

from __future__ import annotations

import ast
from pathlib import Path

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
