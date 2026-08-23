# SPDX-License-Identifier: GPL-2.0-or-later
"""The three layers, checked by reading the source rather than by convention.

`model/` holds data and validation with no I/O, `plan/` derives operations as
pure functions, and `exec/` is the only place that touches the machine. Each
of these was true when it was written and each one drifted once.
"""

from __future__ import annotations

import ast
import subprocess
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "gentoo_install"
HARNESS = Path(__file__).resolve().parents[1] / "vm"

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
        # `importlib.import_module("gentoo_install.exec.convert")` is an
        # import that `ast.Import` never sees, and `plan/convert.py` reached
        # `exec/` through one for as long as this guard has existed.
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            imported.add(node.args[0].value)
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


def test_the_profile_release_is_written_once() -> None:
    """`PROFILES` spelled `default/linux/amd64/23.0` ten times beside
    `BASE_PROFILE`'s eleventh, so moving off 23.0 is eleven edits and the ten
    that are missed are silent: the menu offers a profile the tree no longer
    carries and the install stops at `emerge --sync`.

    The whole package, not `tui/`: reading one directory left the default in
    `model/config.py` and the binary package path in `model/mirrors.py`
    outside the rule, so the release was still written in three places.
    """
    from gentoo_install.model.config import PROFILE_RELEASE

    # String literals, read with `ast`: a comment quoting a real log line is
    # evidence and stays, and a release spelled inside a longer path — which
    # is how `model/mirrors.py` carried its second copy — is still a copy.
    written: dict[str, int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        found = sum(
            1
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and PROFILE_RELEASE in node.value
        )
        if found:
            written[str(path.relative_to(PACKAGE))] = found
    assert written == {"model/config.py": 1}, written

    # And the paths that carry it are built from it rather than spelled again.
    from gentoo_install.model.config import BASE_PROFILE
    from gentoo_install.model.mirrors import BINPACKAGES

    assert BASE_PROFILE.endswith(f"/{PROFILE_RELEASE}"), BASE_PROFILE
    assert BINPACKAGES.endswith(f"/{PROFILE_RELEASE}"), BINPACKAGES

    # The shipped desktop files spell the whole profile, and the menu rebuilds
    # theirs with `str.replace(BASE_PROFILE, ...)`, which does nothing at all
    # when it does not match: a release moved here and not there installs
    # every desktop against the profile that is going away, silently.
    import tomllib

    for path in sorted((PACKAGE / "data" / "profiles").rglob("*.toml")):
        said = tomllib.loads(path.read_text(encoding="utf-8")).get("profile", "")
        if said:
            assert said.startswith(BASE_PROFILE), (str(path), said)


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
            relative = one.relative_to(root)
            if not one.is_file() or ".git" in relative.parts or "lab" in relative.parts:
                continue
            read.append(relative)
            # A shell script's first line is its interpreter, so the identifier
            # is on the second; Python has no such line and uses the first.
            head = one.read_text(encoding="utf-8").splitlines()[: line + 1]
            if head[line : line + 1] != [SPDX]:
                silent.append(relative)

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
        for node in _loaded_nodes(tree):
            if isinstance(node, ast.Name):
                name = aliases.get(node.id, node.id)
            elif isinstance(node, ast.Attribute):
                name = node.attr
            else:
                continue
            assert node.end_lineno is not None
            graph.setdefault(name, []).append((path, node.lineno, node.end_lineno))
    return graph


def _loaded_nodes(tree: ast.AST) -> Iterator[ast.expr]:
    """Every name a module reads, quoted annotations included.

    A forward reference is a string to the parser, so `link: "Reopenable"`
    read as nothing at all and the only use of that protocol was invisible.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            yield node
            continue
        annotation = _annotation_of(node)
        if not isinstance(annotation, ast.Constant) or not isinstance(annotation.value, str):
            continue
        try:
            quoted = ast.parse(annotation.value, mode="eval")
        except SyntaxError:
            continue
        for inner in ast.walk(quoted):
            if isinstance(inner, (ast.Name, ast.Attribute)) and isinstance(inner.ctx, ast.Load):
                # The quoted expression has its own line numbers, so the
                # annotation's are used: a reference has to be locatable.
                yield ast.copy_location(inner, annotation)


def _annotation_of(node: ast.AST) -> ast.expr | None:
    if isinstance(node, (ast.AnnAssign, ast.arg)):
        return node.annotation
    if isinstance(node, ast.FunctionDef):
        return node.returns
    return None


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
    # `tests/vm/` as well as the package: a constant nothing reads is as dead
    # in the harness as in the installer, and `CONVERT_IDLE` had sat beside
    # the ceiling it was named after since the conversion runner was written.
    # A definition its own unit test still names is reachable to this rule, so
    # the wiring of a verdict rule is pinned beside that rule instead.
    # `tests/unit/` is excluded: pytest calls its tests.
    for path in sorted(
        [*(root / "gentoo_install").rglob("*.py"), *(root / "tests" / "vm").rglob("*.py")]
    ):
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


def _tracked(where: str) -> bool:
    """Whether git tracks this path, cached for one run of the suite."""
    if not _TRACKED:
        listed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=PACKAGE.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        _TRACKED.update(one for one in listed.stdout.split("\0") if one)
    return where in _TRACKED


#: Filled once by `_tracked`.
_TRACKED: set[str] = set()


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
            # A worktree the agent tooling opens inside the repository carries
            # a copy of every excepted file, and the scan read all of them as
            # offenders. What the repository holds is what git tracks.
            if not _tracked(where):
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


def _bindings_in(
    body: Mapping[str, ast.FunctionDef], known: Mapping[str, ast.expr]
) -> dict[str, ast.expr]:
    """Every name `apply()` binds to something a path can be read out of.

    `destinations()` is where an operation derives the paths both halves read,
    so a write through one of its results resolves to that method rather than
    to the loop variable that carried it.
    """
    reachable = dict(known)
    derived = body.get("destinations")
    for step in ast.walk(body["apply"]):
        if isinstance(step, ast.Assign) and step.value is not None:
            reachable.update(
                {one.id: step.value for one in step.targets if isinstance(one, ast.Name)}
            )
        elif derived is not None and isinstance(step, (ast.For, ast.Assign)):
            source = step.iter if isinstance(step, ast.For) else step.value
            if source is not None and "destinations()" in ast.unparse(source):
                target = step.target if isinstance(step, ast.For) else step.targets[0]
                reachable.update(
                    {
                        one.id: ast.Constant(_destination_text(derived))
                        for one in ast.walk(target)
                        if isinstance(one, ast.Name)
                    }
                )
    return reachable


def _destination_text(derived: ast.FunctionDef) -> str:
    """The path literals `destinations()` returns, as one string a description
    can be searched against."""
    return " ".join(
        node.value
        for node in ast.walk(derived)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _written_names(expr: ast.expr, known: Mapping[str, ast.expr]) -> set[str]:
    """What a reader of `describe()` could recognise the written path by: the
    expression itself, any module constant it names, and the last component of
    every string those reach."""
    found = {ast.unparse(expr)}
    reachable = [expr]
    if isinstance(expr, ast.Name) and expr.id in known:
        reachable.append(known[expr.id])
        found.add(ast.unparse(known[expr.id]))
    for node in list(reachable):
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and inner.id in known:
                found.add(inner.id)
                reachable.append(known[inner.id])
    for node in reachable:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                found.add(inner.value.rsplit("/", 1)[-1])
    return {one for one in found if one}


def test_every_operation_names_the_files_it_writes() -> None:
    """`--dry-run` prints `describe()` for the same operations `apply()` runs,
    so a file named by neither is a file an operator learns about by finding it
    on the disk. Two operations had this fixed by hand and three others still
    had it, so the rule moves out of their comments and into a check.

    Only the paths `apply()` writes itself: an operation that delegates to
    another one is described by the operation it delegates to.

    The count is asserted at the end. Every `context.write` call is found by
    its attribute name, so renaming that method would make the loop skip all
    of them and the check would pass having compared nothing.
    """
    read = 0
    for module in _modules("plan"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        known: dict[str, ast.expr] = {}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    known[node.target.id] = node.value
            elif isinstance(node, ast.Assign) and node.value is not None:
                known.update(
                    {one.id: node.value for one in node.targets if isinstance(one, ast.Name)}
                )
        for cls in (one for one in ast.walk(tree) if isinstance(one, ast.ClassDef)):
            body = {one.name: one for one in cls.body if isinstance(one, ast.FunctionDef)}
            if "apply" not in body or "describe" not in body:
                continue
            said = ast.unparse(body["describe"])
            reachable = _bindings_in(body, known)
            for call in ast.walk(body["apply"]):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                if call.func.attr != "write" or not call.args:
                    continue
                read += 1
                names = _written_names(call.args[0], reachable)
                assert any(one in said for one in names), (
                    f"{module.name}:{call.lineno} {cls.name}.describe names none of "
                    f"{sorted(names)}"
                )
    # Measured 2026-08-20: nineteen `context.write` calls sit in operations
    # that define `describe`. A scan that suddenly reads none of them is a
    # renamed method, not a tree that stopped writing files.
    assert read >= 15, read


def test_the_dry_run_names_every_file_only_its_owner_may_read() -> None:
    """A file written `0600` holds a password, a key or an access grant, so
    where it lands is what an operator most needs from `--dry-run`. Eight of
    them were named by no description: the four `WriteProxyClients` writes,
    `dirmngr.conf`, `wired.nmconnection`, `authorized_keys` and the sshd
    drop-in.

    Both spellings of the description are read. The wider rule above sees only
    `describe`, so every operation whose text is translated sat outside it;
    thirty of them still name no file, and that is `docs/tasks.md` row 241.
    """
    private = 0
    for module in _modules("plan"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        known: dict[str, ast.expr] = {}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    known[node.target.id] = node.value
            elif isinstance(node, ast.Assign) and node.value is not None:
                known.update(
                    {one.id: node.value for one in node.targets if isinstance(one, ast.Name)}
                )
        for cls in (one for one in ast.walk(tree) if isinstance(one, ast.ClassDef)):
            body = {one.name: one for one in cls.body if isinstance(one, ast.FunctionDef)}
            describing = body.get("describe") or body.get("describe_parts")
            if "apply" not in body or describing is None:
                continue
            said = ast.unparse(describing)
            reachable = _bindings_in(body, known)
            for call in ast.walk(body["apply"]):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                if call.func.attr != "write" or not call.args:
                    continue
                # `ast.unparse` prints `0o600` as 384, so the value is read.
                if not any(
                    keyword.arg == "mode"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value in (0o600, 0o400)
                    for keyword in call.keywords
                ):
                    continue
                private += 1
                names = _written_names(call.args[0], reachable)
                assert any(one in said for one in names), (
                    f"{module.name}:{call.lineno} {cls.name} writes a private file "
                    f"its description does not name: {sorted(names)}"
                )
    # The same denominator: twelve writes carry `0600` or `0400` today.
    assert private >= 10, private


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
    # The harness as well as the package: `tests/vm/` carried seven of its own.
    for path in [*sorted(PACKAGE.rglob("*.py")), *sorted(HARNESS.rglob("*.py"))]:
        source = path.read_text()
        tree = ast.parse(source)
        lines = source.splitlines()
        imported: dict[str, int] = {}
        # Every line of the statement, not the line it starts on: a name
        # inside `from x import (\n    Name,\n)` sits on its own line, and
        # excluding only the first left it there to be found as its own use.
        spanned: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                # `from __future__ import annotations` has no user by design.
                if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                    continue
                spanned.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
                for alias in node.names:
                    imported[alias.asname or alias.name.split(".")[0]] = node.lineno
        # The import statements themselves are not uses of what they bind.
        elsewhere = "\n".join(
            line for number, line in enumerate(lines, 1) if number not in spanned
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
    from gentoo_install.tui.context import PASSPHRASE_MINIMUM

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


def test_the_package_screens_reach_nothing_in_the_screen_module() -> None:
    """`tui/packages.py` is the closure of the seven screens that choose
    packages. They share one set of helpers — `Effects`, `derive_effects`,
    `settle` — so no single screen's closure is the module; the union of the
    seven is, with six names crossing outward and none inward."""
    tui = PACKAGE / "tui"
    imported = _imports(ast.parse((tui / "packages.py").read_text()))
    forbidden = {
        name
        for name in imported
        if name.split(".")[-1] in {"screens", "settings", "partitions"}
    }
    assert not forbidden, forbidden

    source = (tui / "screens.py").read_text()
    crossing = {
        "_profile_for",
        "_record_operator",
        "_set_font_configuration",
        "_typed_beside_automatic",
        "font_configuration_group",
        "input_configuration_group",
    }
    for name in crossing:
        assert re.search(rf"from \.packages import \([^)]*\b{name}\b", source, re.S), name


def test_the_disk_screens_reach_nothing_in_the_screen_module() -> None:
    """`tui/partitions.py` is the closure of `partitions_screen`: everything
    it reaches and nothing else. `screens.py` reaches four of those names and
    this module reaches none of its, which is what makes the split one way. An
    earlier attempt cut the same subject by hand and found edges running both
    ways, so the boundary was computed instead."""
    tui = PACKAGE / "tui"
    imported = _imports(ast.parse((tui / "partitions.py").read_text()))
    forbidden = {name for name in imported if name.split(".")[-1] in {"screens", "settings"}}
    assert not forbidden, forbidden

    # And the four names that cross the other way, by name: a fifth would mean
    # the closure moved and the boundary was not recomputed.
    source = (tui / "screens.py").read_text()
    crossing = {
        name
        for name in ("partitions_screen", "_from_layout", "_zfs_bootloader", "_edit_passphrase")
        if re.search(rf"\b{name}\b", source)
    }
    assert crossing == {
        "partitions_screen",
        "_from_layout",
        "_zfs_bootloader",
        "_edit_passphrase",
    }, crossing

    # Whatever `screens.py` uses from there, it imports from there.
    for name in crossing:
        assert re.search(rf"from \.partitions import \([^)]*\b{name}\b", source, re.S), name


def test_one_table_decides_what_an_architecture_is_called() -> None:
    """`x86_64` and `amd64` are the same machine under two ecosystems' names,
    and the only table that said so lived in `plan/netboot.py`, where `exec`
    and `model` cannot read it. A second table is how the fourteen sites that
    spell an architecture drifted apart."""
    import ast

    from gentoo_install.model.compat import ARCHITECTURES

    pairs = {(one.kernel_name, one.gentoo_name) for one in ARCHITECTURES}
    assert ("x86_64", "amd64") in pairs

    # Any other place holding both spellings of one architecture together is
    # the second table this exists to prevent. One file is exempt, by path: a
    # basename exempted every future `plan/compat.py` from the rule too.
    for path in sorted(PACKAGE.rglob("*.py")):
        if path == PACKAGE / "model" / "compat.py":
            continue
        text = path.read_text()
        for kernel_name, gentoo_name in pairs:
            for node in ast.walk(ast.parse(text)):
                if not isinstance(node, (ast.Dict, ast.Tuple, ast.List, ast.Set)):
                    continue
                spelled = {
                    one.value
                    for one in ast.walk(node)
                    if isinstance(one, ast.Constant) and isinstance(one.value, str)
                }
                assert not {kernel_name, gentoo_name} <= spelled, (
                    f"{path}:{node.lineno} holds both names of one architecture"
                )


def test_the_default_architecture_is_pinned_by_name_not_by_row_order() -> None:
    """`DEFAULT_ARCHITECTURE = ARCHITECTURES[0]` made sorting the table or
    adding an arm64 row above amd64 tell `exec/preflight.py` to accept aarch64
    and refuse x86_64, while the stage3 and profile paths still fetch amd64."""
    import ast

    from gentoo_install.model import compat

    assert compat.DEFAULT_ARCHITECTURE.kernel_name == "x86_64"
    assert compat.DEFAULT_ARCHITECTURE.gentoo_name == "amd64"
    assert compat.DEFAULT_ARCHITECTURE in compat.ARCHITECTURES

    source = (PACKAGE / "model" / "compat.py").read_text()
    assigned = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name) and target.id == "DEFAULT_ARCHITECTURE"
        if node.value is not None
    ]
    assert len(assigned) == 1, assigned
    # Every name the assignment reads, so `ARCHITECTURES[0]`, `next(iter(...))`
    # and any other way of taking the default out of the ordered table fail.
    read = {one.id for one in ast.walk(assigned[0]) if isinstance(one, ast.Name)}
    assert "ARCHITECTURES" not in read, "the default is taken out of the ordered table"


def test_an_architecture_row_cannot_be_written_with_its_names_swapped() -> None:
    """Both fields are strings that read the same way round, so a positional
    row with the two names exchanged passed mypy and every test."""
    from typing import Any, Callable

    import pytest

    from gentoo_install.model.compat import Architecture

    # Called through a name mypy cannot resolve to the constructor, so the
    # rejection this pins is the runtime one and not a silenced type error.
    build: Callable[..., Any] = Architecture
    with pytest.raises(TypeError):
        build("amd64", "x86_64")


def test_no_test_writes_its_artifacts_inside_the_checkout() -> None:
    """A driver CD or a screen transcript in the tree is one commit away.

    The workspace's `lab/` is where a build artifact belongs; a relative path
    puts it wherever the harness happened to be run from, which for every
    invocation of these modules is the repository.
    """
    import ast

    root = Path(__file__).resolve().parents[2]
    written: list[str] = []
    for one in sorted((root / "tests").rglob("*.py")):
        tree = ast.parse(one.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "Path" or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            if first.value.startswith("lab/"):
                written.append(f"{one.relative_to(root)}:{node.lineno}")
    assert not written, f"an artifact path relative to the checkout: {written}"


def test_the_probe_offers_no_lookup_nothing_calls() -> None:
    """`Probe.disk_of` wrapped `disk_of_path` and had no caller: the
    bootloader route calls `disk_of_path` directly. A second spelling of one
    lookup is a second place for a device-graph question to be asked."""
    from gentoo_install.exec.probe import Probe

    assert not hasattr(Probe, "disk_of"), "nothing called it when it was deleted"
    assert hasattr(Probe, "disk_of_path"), "the one the production path uses"


def test_every_builder_refuses_a_conversion_whose_layout_is_unread() -> None:
    """A conversion's graph is empty until `plan/convert.layout_graph()` reads
    the machine, and `plan/build._in_place()` is the only caller allowed to
    derive a populated configuration from it. Seven callers each asked the
    placeholder a question instead; the invariant that stops the eighth is
    that a builder handed the unread configuration refuses rather than
    inventing an answer for an empty graph."""
    from dataclasses import replace

    from gentoo_install.errors import GentooInstallError
    from gentoo_install.model.config import DiskMode
    from gentoo_install.model.device import DeviceGraph, DeviceId
    from gentoo_install.plan import bootloader, kernel, system

    from .layouts import config as a_config

    unread = replace(
        a_config(),
        disk=replace(
            a_config().disk,
            mode=DiskMode.IN_PLACE,
            graph=DeviceGraph.build([]),
            root=DeviceId(""),
        ),
    )
    assert unread.disk.layout_is_read_from_the_machine

    # One entry per builder `plan/build._in_place()` hands the derived
    # configuration to. A builder added there is added here, and the first
    # thing this test asks of it is whether it can tell the two apart.
    builders = (
        ("bootloader.build", bootloader.build),
        ("bootloader.boot_facts", bootloader.boot_facts),
        ("kernel.build", kernel.build),
        ("system.build", system.build),
    )
    answered = []
    for name, builder in builders:
        try:
            builder(unread)
        except (GentooInstallError, KeyError, LookupError):
            continue
        answered.append(name)
    assert not answered, f"answered a question about an empty graph: {answered}"


def test_the_planner_is_the_only_route_a_conversion_takes() -> None:
    """`plan.build()` refuses without the layout rather than planning from the
    placeholder, so the refusal above is never what a caller sees."""
    from dataclasses import replace

    from gentoo_install.data import load_catalog
    from gentoo_install.errors import ConversionUnsupported
    from gentoo_install.model.config import DiskMode
    from gentoo_install.model.device import DeviceGraph, DeviceId
    from gentoo_install.plan.build import build

    from .layouts import config as a_config

    unread = replace(
        a_config(),
        disk=replace(
            a_config().disk,
            mode=DiskMode.IN_PLACE,
            graph=DeviceGraph.build([]),
            root=DeviceId(""),
        ),
    )
    with pytest.raises(ConversionUnsupported, match="running layout"):
        build(unread, load_catalog())


def test_offering_a_conversion_and_reading_its_layout_are_one_answer() -> None:
    """`_conversion_offer` returns the refusal and the layout together, and
    `tui/app._blocked` plans with that layout. Offered without one, the plan
    raises `the running layout was not read` into the Install row, where the
    text is an internal invariant no operator can act on and no answer clears.
    The two travel together so that state cannot be built."""
    import ast
    import inspect

    from gentoo_install import cli

    source = inspect.getsource(cli._conversion_offer)
    returns = [
        (node.lineno, node.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
    ]
    assert returns, source
    offered = 0
    for line, tuple_node in returns:
        refusal, layout = tuple_node.elts[1], tuple_node.elts[2]
        # `refusals.OFFERED` is the sentinel for "nothing stops it", so it is
        # the one return that has to carry a layout; every other one refuses
        # and carries none.
        takes_it = (
            isinstance(refusal, ast.Attribute) and refusal.attr == "OFFERED"
        )
        unread = isinstance(layout, ast.Constant) and layout.value is None
        assert takes_it != unread, (
            f"line {line} returns a refusal and a layout that disagree: "
            "an offered conversion without a layout reaches the planner as "
            "`the running layout was not read`"
        )
        offered += takes_it
    assert offered == 1, f"{offered} returns offer the conversion"


def test_no_screen_plans_without_the_machine_it_is_planning_for() -> None:
    """A conversion's graph comes from `context.running_layout`, so every
    `plan.build` under `tui/` has to pass it. `#917` fixed `app._blocked` and
    left `overview.py` planning without it, so pressing Install answered `the
    running layout was not read` and cancelled: no conversion could be started
    from the menu at all, while every `--config` run converted fine."""
    import ast

    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2] / "gentoo_install" / "tui"
    unplanned = []
    for source in sorted(root.glob("*.py")):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name not in {"build", "plan_build"}:
                continue
            # `DeviceGraph.build` and the template builders are other calls of
            # the same name; only the planner takes a catalog as its second
            # argument, so the layout keyword is what identifies this one.
            if len(node.args) < 2:
                continue
            if any(word.arg == "layout" for word in node.keywords):
                continue
            unplanned.append(f"{source.name}:{node.lineno}")
    assert not unplanned, f"plans without the running layout: {unplanned}"
