# SPDX-License-Identifier: GPL-2.0-or-later
"""The three layers, checked by reading the source rather than by convention.

`model/` holds data and validation with no I/O, `plan/` derives operations as
pure functions, and `exec/` is the only place that touches the machine. Each
of these was true when it was written and each one drifted once.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

PACKAGE = Path(__file__).resolve().parents[2] / "gentoo_install"

#: What reading a file looks like in an AST. `Path.read_text` and friends are
#: attribute calls; `open` is a bare name.
_READERS = frozenset({"read_text", "read_bytes", "write_text", "write_bytes", "open"})


def _modules(layer: str) -> list[Path]:
    return sorted(one for one in (PACKAGE / layer).rglob("*.py") if one.is_file())


def _imports(tree: ast.AST) -> set[str]:
    """Return the modules named by ordinary and from-import statements."""
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            if node.level == 0 and node.module == "gentoo_install":
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif node.module is not None:
                imported.add(prefix + node.module)
            else:
                imported.update(prefix + alias.name for alias in node.names)
    return imported


def _imports_module(imported: str, module: str) -> bool:
    return imported == module or imported.startswith(f"{module}.")


def test_import_scan_handles_import_and_from_import() -> None:
    """`from subprocess import run` bypassed the Import-only command boundary."""
    assert _imports(ast.parse("import subprocess")) == {"subprocess"}
    assert _imports(ast.parse("from subprocess import run")) == {"subprocess"}


def test_import_scan_handles_relative_and_package_imports() -> None:
    """Both spellings can cross a layer boundary."""
    tree = ast.parse("from ..exec import Machine\nfrom gentoo_install import tui")
    assert _imports(tree) == {"..exec", "gentoo_install.tui"}


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
            imported = _imports(ast.parse(module.read_text()))
            offenders = sorted(
                name for name in imported if _imports_module(name, "subprocess")
            )
            assert not offenders, f"{module}: imports {offenders}"


def test_the_model_layer_imports_nothing_below_it() -> None:
    """Calls go downward only: `model` knows nothing of `plan`, `exec` or the
    interface, and `plan` knows nothing of `exec` or the interface."""
    forbidden = {"model": ("plan", "exec", "tui"), "plan": ("exec", "tui")}
    for layer, below in forbidden.items():
        for module in _modules(layer):
            imported = _imports(ast.parse(module.read_text()))
            for other in below:
                forbidden_modules = (f"..{other}", f"gentoo_install.{other}")
                offenders = sorted(
                    name
                    for name in imported
                    if any(_imports_module(name, forbidden) for forbidden in forbidden_modules)
                )
                assert not offenders, f"{module}: imports {offenders}"


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
    be taken from a GPL-3 project at all.

    Shell as well as Python: the README says every source file carries the
    identifier, and `bootstrap.sh` — the one file a stranger runs first — did
    not, because this test only ever looked at `*.py`.
    """
    root = PACKAGE.parent
    silent: list[Path] = []
    read: list[Path] = []
    for pattern, line in (("*.py", 0), ("*.sh", 1)):
        for one in sorted(root.rglob(pattern)):
            if not one.is_file() or ".git" in one.parts or "lab" in one.parts:
                continue
            read.append(one.relative_to(root))
            # A shell script's first line is its interpreter, so the identifier
            # is on the second; Python has no such line and uses the first.
            head = one.read_text(encoding="utf-8").splitlines()[: line + 1]
            if head[line : line + 1] != [SPDX]:
                silent.append(one.relative_to(root))

    assert not silent, f"no licence on the first line of: {silent}"
    # Not vacuous: the shell scripts have to be among the files examined, or
    # narrowing the scan back to Python would pass while the README's claim
    # about every source file stayed false.
    assert any(one.suffix == ".sh" for one in read), read[:5]


Reference = tuple[Path, int, int]
ReferenceGraph = dict[str, list[Reference]]


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.asname is not None:
                aliases[alias.asname] = alias.name
    return aliases


def _reference_graph(trees: Mapping[Path, ast.AST]) -> ReferenceGraph:
    """Map every loaded name and attribute to its source location."""
    graph: ReferenceGraph = {}
    for path, tree in trees.items():
        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = aliases.get(node.id, node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            else:
                continue
            assert node.end_lineno is not None
            graph.setdefault(name, []).append((path, node.lineno, node.end_lineno))
    return graph


def _defined_names(tree: ast.Module) -> list[tuple[ast.stmt, str]]:
    definitions: list[tuple[ast.stmt, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            definitions.append((node, node.name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            definitions.append((node, node.target.id))
        elif isinstance(node, ast.Assign):
            definitions.extend(
                (node, target.id) for target in node.targets if isinstance(target, ast.Name)
            )
    return definitions


def _referenced_outside(
    name: str, path: Path, definition: ast.stmt, graph: ReferenceGraph
) -> bool:
    end_line = definition.end_lineno
    assert end_line is not None
    for reference_path, line, reference_end in graph.get(name, []):
        inside_definition = (
            reference_path == path
            and definition.lineno <= line
            and reference_end <= end_line
        )
        if not inside_definition:
            return True
    return False


def test_reference_graph_ignores_comments_and_strings() -> None:
    """A name in prose is not a reachable definition."""
    declaring = Path("declaring.py")
    definition_tree = ast.parse("class Forgotten:\n    pass\n")
    definition = definition_tree.body[0]
    assert isinstance(definition, ast.ClassDef)
    graph = _reference_graph(
        {
            declaring: definition_tree,
            Path("commentary.py"): ast.parse('# Forgotten\nmessage = "Forgotten"\n'),
        }
    )
    assert not _referenced_outside("Forgotten", declaring, definition, graph)
    graph = _reference_graph(
        {
            declaring: definition_tree,
            Path("consumer.py"): ast.parse("Forgotten()"),
        }
    )
    assert _referenced_outside("Forgotten", declaring, definition, graph)
    alias_graph = _reference_graph(
        {
            declaring: definition_tree,
            Path("consumer.py"): ast.parse(
                "from declaring import Forgotten as remembered\nremembered()"
            ),
        }
    )
    assert _referenced_outside("Forgotten", declaring, definition, alias_graph)


def test_no_definition_in_the_package_is_unreachable() -> None:
    """A rebase left `RemoveUnbootableKernels` and its five helpers in the tree
    after the operation it replaced them with had landed: the class was there,
    nothing built it, and nothing tested it."""
    root = Path(__file__).resolve().parents[2]
    source_paths = [
        path
        for where in ("gentoo_install", "tests")
        for path in sorted((root / where).rglob("*.py"))
    ]
    trees = {path: ast.parse(path.read_text()) for path in source_paths}
    graph = _reference_graph(trees)
    orphans: list[str] = []
    for path in sorted((root / "gentoo_install").rglob("*.py")):
        for node, name in _defined_names(trees[path]):
            if name.startswith("__"):
                continue
            if not _referenced_outside(name, path, node, graph):
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


#: The one hit `AGENTS.md` allows outside the catalogs. The console pattern
#: has to match text a localized system emits.
CJK_EXCEPTIONS: Final[frozenset[str]] = frozenset({"tests/vm/console.py"})

#: Written as escapes, not as a literal range: this file is scanned too.
WIDE: Final[re.Pattern[str]] = re.compile("[\u4e00-\u9fff]")


def _wide_character_scan() -> tuple[list[str], list[str]]:
    """Return the offending `path:line` list and every path that was read.

    Both are returned from one walk so that a test can hold the denominator.
    A scan that stops reading files reports no offenders, and a separate
    file count would not notice.
    """
    root = PACKAGE.parent
    offenders: list[str] = []
    scanned: list[str] = []
    for pattern in ("*.py", "*.toml"):
        for path in sorted(root.rglob(pattern)):
            where = str(path.relative_to(root))
            if "/.git/" in f"/{where}" or "__pycache__" in where:
                continue
            body = path.read_text()
            scanned.append(where)
            if where.startswith("gentoo_install/data/locale/") or where in CJK_EXCEPTIONS:
                continue
            for number, line in enumerate(body.splitlines(), 1):
                if WIDE.search(line):
                    offenders.append(f"{where}:{number}")
    return offenders, scanned


def test_no_wide_character_is_written_into_code_or_a_data_file() -> None:
    """`AGENTS.md` carries this as a grep and nothing ran it, so one line of
    Chinese sat in a docstring in `tests/unit/test_plan_convert.py` and was
    found by a review rather than by the suite. A comment nobody on the
    project can read splits one codebase into two languages; a test needing a
    wide character writes it as a codepoint escape.
    """
    offenders, _ = _wide_character_scan()
    assert not offenders, offenders


def test_the_wide_character_scan_reaches_the_files_it_claims_to() -> None:
    """The denominator, read from the same walk as the offender list. Every
    excepted path is named by a file that was actually read and does carry a
    wide character, so an exception that stopped being needed fails here
    rather than sitting as dead weight.
    """
    offenders, scanned = _wide_character_scan()
    assert not offenders, offenders

    root = PACKAGE.parent
    assert sum(1 for one in scanned if one.endswith(".py")) > 100, len(scanned)
    assert sum(1 for one in scanned if one.endswith(".toml")) > 30, len(scanned)
    assert "tests/unit/test_layers.py" in scanned

    for where in CJK_EXCEPTIONS:
        assert where in scanned, where
        assert WIDE.search((root / where).read_text()), where

    catalogs = [one for one in scanned if one.startswith("gentoo_install/data/locale/")]
    assert catalogs, scanned[:5]
    # Not every catalog: `ko.toml` is hangul throughout and carries no hanja,
    # so requiring one per file would fail on a correct translation.
    assert [one for one in catalogs if WIDE.search((root / one).read_text())], catalogs


def test_every_operation_still_has_to_say_what_it_does() -> None:
    """`describe()` stopped being abstract when it gained a default built from
    `describe_parts()`, so a class defining neither used to fail the type check
    and now raises at the moment an operator is reading the plan. The
    guarantee moves here rather than being dropped.
    """
    import gentoo_install.plan.operations as operations

    # Imported for their side effect: a subclass is only reachable once the
    # module defining it has been loaded.
    for name in ("build", "disk", "system", "portage", "bootloader", "kernel", "convert"):
        __import__(f"gentoo_install.plan.{name}")

    found: list[type[operations.Operation]] = []
    pending: list[type[operations.Operation]] = list(operations.Operation.__subclasses__())
    while pending:
        one = pending.pop()
        pending += one.__subclasses__()
        if not getattr(one, "__abstractmethods__", frozenset()):
            found.append(one)

    silent = [
        one.__name__
        for one in found
        if one.describe is operations.Operation.describe
        and one.describe_parts is operations.Operation.describe_parts
    ]
    assert not silent, silent
    assert len(found) > 60, len(found)


def test_the_proxy_endpoint_is_defined_once() -> None:
    """It was written twice, byte for byte, in `plan/portage.py` and
    `plan/system.py`, while `system.py` already imported from `portage.py`. A
    second copy of a one-line rule is where the two answers diverge."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "gentoo_install" / "plan"
    defined = [
        path.name
        for path in sorted(root.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == "_proxy_endpoint"
    ]

    assert defined == ["portage.py"], defined


def test_the_tui_context_knows_nothing_about_a_screen() -> None:
    """`Context`, the footer and the one-line acknowledgement moved out of
    `screens.py` because `settings.py`, `overview.py`, `app.py` and `cli.py`
    all reached into a screen module for them. The move is only worth its
    churn while the direction stays one way: an import of `screens` or
    `settings` from here is the cycle that put them together in the first
    place."""
    tui = PACKAGE / "tui"
    imported = _imports(ast.parse((tui / "context.py").read_text()))

    forbidden = {
        name
        for name in imported
        if name.split(".")[-1] in {"screens", "settings", "mirror", "overview", "app"}
    }
    assert not forbidden, forbidden

    # And the names it owns are not defined a second time next door, which is
    # how a moved definition comes back as a copy.
    owned = {
        "Context",
        "FieldDescriptor",
        "answers",
        "DONE",
        "current_menu",
        "footer",
        "pick",
        "say",
        "show_address",
    }
    for neighbour in ("screens.py", "settings.py", "overview.py", "app.py", "mirror.py"):
        tree = ast.parse((tui / neighbour).read_text())
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in owned
        }
        assert not defined, f"{neighbour} defines {defined} again"


def test_no_module_imports_a_name_it_never_uses() -> None:
    """Twenty dead imports were in the tree at once, twelve of them left in
    `tui/screens.py` by two moves that took their users away. `mypy` does not
    see them and no lint gate is configured, so an import that survives its
    last caller reads as a dependency the module still has."""
    dead: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        lines = source.splitlines()
        imported: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                # `from __future__ import annotations` has no user by design.
                if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                    continue
                for alias in node.names:
                    imported[alias.asname or alias.name.split(".")[0]] = node.lineno
        # The import statements themselves are not uses of what they bind.
        elsewhere = "\n".join(
            line for number, line in enumerate(lines, 1) if number not in set(imported.values())
        )
        dead += [
            f"{path.relative_to(PACKAGE.parent)}:{line} {name}"
            for name, line in sorted(imported.items())
            if not re.search(rf"\b{re.escape(name)}\b", elsewhere)
        ]
    assert not dead, dead


def test_the_passphrase_minimum_is_one_number() -> None:
    """`zpool create` refuses a short passphrase after the disk is already
    partitioned, so the menu asks for the same length the preflight does. Two
    constants carried the same 8 and the same reason, and raising one would
    have left the menu accepting what the install then stops on."""
    from gentoo_install.exec.preflight import ZFS_PASSPHRASE_MINIMUM
    from gentoo_install.tui.screens import PASSPHRASE_MINIMUM

    assert PASSPHRASE_MINIMUM is ZFS_PASSPHRASE_MINIMUM

    # And nothing else in the package writes a minimum of its own. The file,
    # not the line: a number in this assertion would break on any edit above
    # it and say nothing about the rule.
    written = {
        str(path.relative_to(PACKAGE.parent))
        for path in sorted(PACKAGE.rglob("*.py"))
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id.endswith("PASSPHRASE_MINIMUM")
        and isinstance(node.value, ast.Constant)
    }
    assert written == {"gentoo_install/exec/preflight.py"}, written
