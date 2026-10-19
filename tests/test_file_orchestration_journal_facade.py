"""Re-export hygiene of the file orchestration journal facade (entropy-burndown-2026-10, task 5.1).

``services/orchestrator/file_orchestration_journal.py`` declares its external
read surface in ``__all__``.  This suite pins the declaration's form and that
every listed name has at least one consumer somewhere in the repository, so a
name whose last consumer goes away is noticed instead of lingering as a dead
re-export.  It reads source with ``ast`` only and never imports the journal.

A consumer is any of these, in any ``.py`` under ``apps packages services
workers scripts tests`` (the facade itself and this file excluded):

(a) ``from services.orchestrator.file_orchestration_journal import X [as Y]`` at
    any nesting depth (``if TYPE_CHECKING:`` included); a relative import is
    resolved against the importing file's package;
(b) ``A.X`` in any context, first attribute only, where ``A`` is a journal
    alias bound by ``from services.orchestrator import file_orchestration_journal
    [as A]``, ``import services.orchestrator.file_orchestration_journal as A``,
    ``A = importlib.import_module("services.orchestrator.file_orchestration_journal")``,
    ``A = <alias>`` or ``A = helper()`` (``helper`` a same-file function that
    returns one of these); ``helper().X`` counts too;
(c) ``getattr`` / ``hasattr`` / ``setattr`` / ``delattr`` / ``patch.object`` (and
    ``monkeypatch.setattr`` / ``monkeypatch.delattr``) whose first argument is
    a journal alias and whose second is the ``str`` literal ``"X"``; a
    non-literal name there fails loudly with ``file:line``;
(d) a ``str`` constant that is not a docstring and fully matches
    ``services.orchestrator.file_orchestration_journal.X[.more]``.

Not consumers: comments, docstrings, f-strings, string concatenation, the bare
logger name ``"services.orchestrator.file_orchestration_journal"``, and a
function parameter the module is passed into.

How a name is bound in the facade is not checked here (ruff F822 covers a
listed name that is not bound), so names moved to ``file_journal_*`` siblings
and re-bound by import keep passing.  The reverse direction (every consumer is
listed) is left to task 5.6.
"""

from __future__ import annotations

import ast
import itertools
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FACADE = "services/orchestrator/file_orchestration_journal.py"
FACADE_MODULE = "services.orchestrator.file_orchestration_journal"
FACADE_PACKAGE, FACADE_LEAF = FACADE_MODULE.rsplit(".", 1)
THIS_TEST = "tests/test_file_orchestration_journal_facade.py"
SCAN_ROOTS = ("apps", "packages", "services", "workers", "scripts", "tests")
_PRUNED_DIRS = frozenset({"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"})
_DOTTED_NAME = re.compile(re.escape(FACADE_MODULE) + r"\.([A-Za-z_]\w*)(?:\.\w+)*")
_NAMED_ATTRIBUTE_BUILTINS = frozenset({"getattr", "hasattr", "setattr", "delattr"})
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True, order=True)
class Consumer:
    file: str
    lineno: int
    name: str
    form: str  # "a" | "b" | "c" | "d"
    reads: bool  # from-import, attribute load, getattr/hasattr; False for patch/store forms


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _binds_all(node: ast.stmt) -> bool:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    else:
        return False
    return any(isinstance(n, ast.Name) and n.id == "__all__" for t in targets for n in ast.walk(t))


def declared_all(root: Path) -> tuple[tuple[str, ...], list[str]]:
    """Return the facade's ``__all__`` names and the problems with its form."""
    statements = [node for node in _parse(root / FACADE).body if _binds_all(node)]
    if len(statements) != 1:
        lines = [node.lineno for node in statements]
        return (), [f"{FACADE}: `__all__` must be assigned exactly once at module level, found at lines {lines}"]
    node = statements[0]
    where = f"{FACADE}:{node.lineno}"
    if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple)):
        return (), [f"{where}: `__all__` must be a plain assignment of a tuple literal"]
    elements = node.value.elts
    if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in elements):
        return (), [f"{where}: every `__all__` element must be a str literal"]
    names = tuple(e.value for e in elements)
    problems: list[str] = []
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        problems.append(f"{where}: `__all__` lists {duplicates} more than once")
    if list(names) != sorted(names):
        out_of_order = [f"{before!r} > {after!r}" for before, after in itertools.pairwise(names) if before > after]
        problems.append(f"{where}: `__all__` must be sorted by code point: {out_of_order}")
    return names, problems


# ---------------------------------------------------------------------------
# Consumers
# ---------------------------------------------------------------------------


def _python_files(root: Path) -> Iterator[Path]:
    for top in SCAN_ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            if not _PRUNED_DIRS.intersection(path.parts):
                yield path


def _resolve_from(node: ast.ImportFrom, rel: str) -> str:
    if node.level == 0:
        return node.module or ""
    package = rel.removesuffix(".py").split("/")[:-1]
    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*base, *([node.module] if node.module else [])])


def _docstring_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, *_SCOPES)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _scope_nodes(scope: ast.AST) -> tuple[list[ast.AST], list[ast.AST]]:
    """Split a scope into its own nodes and its directly nested def/class scopes.

    Decorators, defaults, bases and keywords of a nested def/class are
    evaluated in the enclosing scope, so they stay with it.
    """
    own: list[ast.AST] = []
    nested: list[ast.AST] = []
    stack: list[ast.AST] = list(scope.body) if isinstance(scope, (ast.Module, *_SCOPES)) else [scope]
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPES):
            nested.append(node)
            stack.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                stack.extend(node.bases)
                stack.extend(node.keywords)
            else:
                stack.extend(node.args.defaults)
                stack.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        own.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return own, nested


def _is_patch_object(func: ast.expr) -> bool:
    if not (isinstance(func, ast.Attribute) and func.attr == "object"):
        return False
    base = func.value
    return (isinstance(base, ast.Name) and base.id == "patch") or (
        isinstance(base, ast.Attribute) and base.attr == "patch"
    )


def _named_attribute_call(func: ast.expr) -> bool | None:
    """``True`` for a reading call, ``False`` for a patching one, ``None`` otherwise."""
    if isinstance(func, ast.Name) and func.id in _NAMED_ATTRIBUTE_BUILTINS:
        return func.id in {"getattr", "hasattr"}
    if isinstance(func, ast.Attribute) and func.attr in {"setattr", "delattr"}:
        return False
    if _is_patch_object(func):
        return False
    return None


class _FileScan:
    def __init__(self, tree: ast.Module, rel: str) -> None:
        self.tree = tree
        self.rel = rel
        self.helpers: set[str] = set()
        self.found: set[Consumer] = set()

    def run(self) -> set[Consumer]:
        while True:
            helpers = set(self.helpers)
            self.found = set()
            self._visit(self.tree, frozenset())
            if helpers == self.helpers:
                break
        self._strings()
        return self.found

    def _add(self, node: ast.AST, name: str, form: str, reads: bool) -> None:
        self.found.add(Consumer(self.rel, node.lineno, name, form, reads))

    def _import_aliases(self, nodes: list[ast.AST]) -> set[str]:
        aliases: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                aliases.update(a.asname for a in node.names if a.name == FACADE_MODULE and a.asname)
            elif isinstance(node, ast.ImportFrom):
                module = _resolve_from(node, self.rel)
                if module == FACADE_MODULE:
                    for alias in node.names:
                        if alias.name == "*":
                            raise AssertionError(f"{self.rel}:{node.lineno}: `import *` of the journal facade")
                        self._add(node, alias.name, "a", True)
                elif module == FACADE_PACKAGE:
                    aliases.update(a.asname or a.name for a in node.names if a.name == FACADE_LEAF)
        return aliases

    def _is_facade(self, expr: ast.expr | None, aliases: set[str]) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in aliases
        if not isinstance(expr, ast.Call):
            return False
        func = expr.func
        if isinstance(func, ast.Name) and func.id in self.helpers:
            return True
        is_import_module = (isinstance(func, ast.Name) and func.id == "import_module") or (
            isinstance(func, ast.Attribute) and func.attr == "import_module"
        )
        return (
            is_import_module
            and bool(expr.args)
            and isinstance(expr.args[0], ast.Constant)
            and expr.args[0].value == FACADE_MODULE
        )

    def _visit(self, scope: ast.AST, inherited: frozenset[str]) -> None:
        own, nested = _scope_nodes(scope)
        aliases = set(inherited) | self._import_aliases(own)
        changed = True
        while changed:
            changed = False
            for node in own:
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign) and self._is_facade(node.value, aliases):
                    targets = node.targets
                elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and self._is_facade(node.value, aliases):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases.add(target.id)
                        changed = True
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(node, ast.Return) and self._is_facade(node.value, aliases) for node in own
        ):
            self.helpers.add(scope.name)
        for node in own:
            if isinstance(node, ast.Attribute) and self._is_facade(node.value, aliases):
                self._add(node, node.attr, "b", isinstance(node.ctx, ast.Load))
            elif isinstance(node, ast.Call):
                self._named_call(node, aliases)
        for child in nested:
            self._visit(child, frozenset(aliases))

    def _named_call(self, call: ast.Call, aliases: set[str]) -> None:
        reads = _named_attribute_call(call.func)
        if reads is None or not call.args or not self._is_facade(call.args[0], aliases):
            return
        if len(call.args) < 2 or not (isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str)):
            raise AssertionError(
                f"{self.rel}:{call.lineno}: attribute call on the journal facade whose name is not a str literal"
            )
        self._add(call, call.args[1].value, "c", reads)

    def _strings(self) -> None:
        skipped = _docstring_ids(self.tree)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.JoinedStr):
                skipped.update(id(value) for value in node.values)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped:
                match = _DOTTED_NAME.fullmatch(node.value)
                if match:
                    self._add(node, match.group(1), "d", False)


def collect_consumers(root: Path) -> list[Consumer]:
    """Every consumer of the journal facade under ``root``, sorted."""
    found: set[Consumer] = set()
    for path in _python_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in {FACADE, THIS_TEST}:
            continue
        text = path.read_text(encoding="utf-8")
        if "file_orchestration_journal" not in text:
            continue
        found |= _FileScan(ast.parse(text, filename=str(path)), rel).run()
    return sorted(found)


def unconsumed(root: Path, names: tuple[str, ...]) -> list[str]:
    consumed = {consumer.name for consumer in collect_consumers(root)}
    return sorted(set(names) - consumed)


# ---------------------------------------------------------------------------
# Real tree
# ---------------------------------------------------------------------------


def test_all_is_one_sorted_unique_tuple_of_str_literals() -> None:
    names, problems = declared_all(REPO_ROOT)
    assert problems == []
    assert names


def test_every_listed_name_has_a_consumer() -> None:
    names, _ = declared_all(REPO_ROOT)
    missing = unconsumed(REPO_ROOT, names)
    assert missing == [], f"listed in __all__ but no consumer anywhere in the repository: {missing}"


# ---------------------------------------------------------------------------
# Synthetic trees (tmp_path)
# ---------------------------------------------------------------------------

_SYNTHETIC_FACADE = '"""Synthetic facade."""\n\n__all__ = ("_foo",)\n\n\ndef _foo():\n    return 1\n'


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(root: Path, consumers: dict[str, str], facade: str = _SYNTHETIC_FACADE) -> tuple[str, ...]:
    _write(root, FACADE, facade)
    for rel, text in consumers.items():
        _write(root, rel, text)
    names, problems = declared_all(root)
    assert problems == []
    return names


_GREEN_FORMS = {
    "from_import_in_function": (
        "tests/test_x.py",
        "def test():\n    from services.orchestrator.file_orchestration_journal import _foo\n\n    assert _foo\n",
        "a",
    ),
    "from_package_import_as": (
        "tests/test_x.py",
        "from services.orchestrator import file_orchestration_journal as journal_module\n\n\n"
        "def test():\n    assert journal_module._foo()\n",
        "b",
    ),
    "import_as": (
        "scripts/x.py",
        "import services.orchestrator.file_orchestration_journal as m\n\nVALUE = m._foo\n",
        "b",
    ),
    "import_module_alias": (
        "tests/test_x.py",
        "import importlib\n\njournal = importlib.import_module('services.orchestrator.file_orchestration_journal')\n"
        "again = journal\n\n\ndef test():\n    assert again._foo\n",
        "b",
    ),
    "helper_returned_module": (
        "tests/test_x.py",
        "def _journal():\n    from services.orchestrator import file_orchestration_journal\n\n"
        "    return file_orchestration_journal\n\n\n"
        "def test():\n    journal = _journal()\n    assert journal._foo\n",
        "b",
    ),
    "helper_call_attribute": (
        "tests/test_x.py",
        "def _journal():\n    import services.orchestrator.file_orchestration_journal as module\n\n"
        "    return module\n\n\ndef test():\n    assert _journal()._foo\n",
        "b",
    ),
    "relative_import": (
        "services/orchestrator/x.py",
        "from .file_orchestration_journal import _foo as renamed\n\nVALUE = renamed\n",
        "a",
    ),
    "relative_package_alias": (
        "services/orchestrator/x.py",
        "from . import file_orchestration_journal as journal\n\nVALUE = journal._foo\n",
        "b",
    ),
    "type_checking_import": (
        "apps/x.py",
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n"
        "    from services.orchestrator.file_orchestration_journal import _foo\n",
        "a",
    ),
    "monkeypatch_setattr": (
        "tests/test_x.py",
        "from services.orchestrator import file_orchestration_journal as fj\n\n\n"
        "def test(monkeypatch):\n    monkeypatch.setattr(fj, '_foo', lambda: 2)\n",
        "c",
    ),
    "getattr_literal": (
        "tests/test_x.py",
        "from services.orchestrator import file_orchestration_journal as fj\n\nVALUE = getattr(fj, '_foo')\n",
        "c",
    ),
    "mock_patch_string": (
        "tests/test_x.py",
        "from unittest import mock\n\n\n"
        "@mock.patch('services.orchestrator.file_orchestration_journal._foo')\ndef test(patched):\n    pass\n",
        "d",
    ),
}


@pytest.mark.parametrize("case", sorted(_GREEN_FORMS))
def test_synthetic_consumer_form_is_recognised(tmp_path: Path, case: str) -> None:
    rel, text, form = _GREEN_FORMS[case]
    names = _tree(tmp_path, {rel: text})
    assert unconsumed(tmp_path, names) == []
    assert {(c.name, c.form) for c in collect_consumers(tmp_path)} == {("_foo", form)}


def test_synthetic_name_without_a_consumer_is_named(tmp_path: Path) -> None:
    names = _tree(tmp_path, {"tests/test_x.py": "def test():\n    pass\n"})
    assert unconsumed(tmp_path, names) == ["_foo"]


def test_synthetic_mentions_that_are_not_consumers(tmp_path: Path) -> None:
    names = _tree(
        tmp_path,
        {
            "tests/test_x.py": (
                '"""services.orchestrator.file_orchestration_journal._foo is documented here."""\n'
                "from services.orchestrator import file_orchestration_journal as fj\n\n"
                "# services.orchestrator.file_orchestration_journal._foo in a comment\n"
                "LOGGER_NAME = 'services.orchestrator.file_orchestration_journal'\n"
                "JOINED = 'services.orchestrator.' + 'file_orchestration_journal._foo'\n\n\n"
                "def test(name):\n"
                '    """services.orchestrator.file_orchestration_journal._foo"""\n'
                "    assert f'services.orchestrator.file_orchestration_journal._foo{name}'\n"
                "    assert f'{name}services.orchestrator.file_orchestration_journal._foo'\n"
                "    assert fj is not None\n\n\n"
                "def _record(module):\n    return module._foo\n\n\n"
                "def test_param():\n    _record(fj)\n"
            )
        },
    )
    assert unconsumed(tmp_path, names) == ["_foo"]


def test_synthetic_scan_skips_the_facade_and_this_file(tmp_path: Path) -> None:
    importer = "from services.orchestrator.file_orchestration_journal import _foo\n"
    names = _tree(tmp_path, {THIS_TEST: importer}, facade=_SYNTHETIC_FACADE + "\nVALUE = _foo\n")
    assert unconsumed(tmp_path, names) == ["_foo"]


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        ('("b", "a")', "must be sorted by code point: [\"'b' > 'a'\"]"),
        ('("_foo", "Z")', "must be sorted by code point: [\"'_foo' > 'Z'\"]"),
        ('("a", "a")', "lists ['a'] more than once"),
        ('["a"]', "must be a plain assignment of a tuple literal"),
        ('("a", NAME)', "every `__all__` element must be a str literal"),
    ],
)
def test_synthetic_malformed_declaration_fails(tmp_path: Path, declaration: str, expected: str) -> None:
    _write(tmp_path, FACADE, f"NAME = 'a'\n__all__ = {declaration}\n")
    names, problems = declared_all(tmp_path)
    assert len(problems) == 1 and problems[0].startswith(f"{FACADE}:2: ") and expected in problems[0], problems


def test_synthetic_second_all_binding_fails(tmp_path: Path) -> None:
    _write(tmp_path, FACADE, '__all__ = ("a",)\n__all__ += ("b",)\n')
    assert declared_all(tmp_path) == (
        (),
        [f"{FACADE}: `__all__` must be assigned exactly once at module level, found at lines [1, 2]"],
    )


@pytest.mark.parametrize(
    "call",
    [
        "getattr(fj, name)",
        "hasattr(fj, name)",
        "setattr(fj, name, 1)",
        "monkeypatch.delattr(fj, name)",
        "mock.patch.object(fj, name)",
    ],
)
def test_synthetic_non_literal_name_fails_loudly(tmp_path: Path, call: str) -> None:
    _tree(
        tmp_path,
        {
            "tests/test_dynamic.py": (
                "from unittest import mock\n\nfrom services.orchestrator import file_orchestration_journal as fj\n\n\n"
                f"def test(monkeypatch, name):\n    {call}\n"
            )
        },
    )
    with pytest.raises(AssertionError, match=r"^tests/test_dynamic\.py:7: attribute call on the journal facade"):
        collect_consumers(tmp_path)


def test_synthetic_star_import_fails_loudly(tmp_path: Path) -> None:
    _tree(tmp_path, {"scripts/x.py": "from services.orchestrator.file_orchestration_journal import *\n"})
    with pytest.raises(AssertionError, match=r"^scripts/x\.py:1: `import \*` of the journal facade"):
        collect_consumers(tmp_path)
