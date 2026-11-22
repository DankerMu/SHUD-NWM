"""Facade name-resolution guard (OpenSpec change ``redesign-orchestration-core``, task 10.2, #3270).

``services/orchestrator/scheduler.py`` and ``services/orchestrator/chain.py``
are facades.  Owner modules read names through them (``_scheduler.X``,
``_chain.X``) and tests patch names on them.  F2 (task 11) deleted the compat
installers (``globals().update(...)``) from both facades, so a name that an
owner reads, or a test patches, must exist on the facade through an explicit
import or alias line: otherwise the read raises ``AttributeError`` and a
``raising=False`` patch silently patches nothing.

This is a static check that is meant to stay green across F2.  It collects
every such name and asserts ``hasattr(importlib.import_module(facade), name)``.
When F2 removes a name that is still read, the guard goes red, and F2 must add
an explicit facade import or delete the read.  It must not add an exemption.

Scope:

* files: ``services/orchestrator/**/*.py`` (*owner* sources) and
  ``tests/**/*.py`` (*test* sources);
* reads in a facade's own source that target that same facade are skipped.
  A facade's imports from its own owner modules are not reads.

Facade bindings, tracked per function scope:

* ``from services.orchestrator import scheduler|chain [as A]``;
* ``import services.orchestrator.scheduler|chain as A``;
* the bare dotted form ``services.orchestrator.chain.X`` (after a plain
  ``import services.orchestrator.chain``).

A binding made at module level applies in the whole module.  A binding made
inside a ``def`` applies in that function and in the functions nested in it.
A parameter with the alias's name shadows it.  A class body is not a scope of
its own, so the scope over-reach is fail-safe (a false read goes red, it is
never hidden).

Collected reads, keyed on the first attribute after the facade
(``chain.time.monotonic`` yields ``time``):

* ``attr-load`` / ``attr-store`` / ``attr-del``: ``A.X``;
* ``bare-dotted``: ``services.orchestrator.chain.X``;
* ``getattr``: ``getattr(A, "X")`` with no default;
* ``setattr-object`` / ``delattr-object``: ``setattr`` / ``delattr`` or any
  ``<obj>.setattr`` / ``<obj>.delattr`` (``monkeypatch``) on ``(A, "X", ...)``;
* ``patch-object``: ``patch.object`` / ``mock.patch.object`` on ``(A, "X", ...)``;
* ``setattr-string`` / ``delattr-string`` / ``patch-string``: a first-argument
  string target ``"services.orchestrator.scheduler.X[.…]"`` /
  ``"services.orchestrator.chain.X[.…]"`` in ``<obj>.setattr`` /
  ``<obj>.delattr`` / ``patch`` / ``mock.patch``;
* ``from-import``: ``from services.orchestrator.scheduler|chain import X``;
* ``owner-dispatch``: in an owner source, a module-level function whose body
  calls ``getattr(A, <its own parameter>)`` is a dispatch wrapper (today
  ``_scheduler_call`` in ``scheduler_candidate_manifest.py`` and
  ``scheduler_candidate_execution_evidence.py``).  Each call to it with a
  literal argument in that parameter's position is a read of that name.

``raising=False`` patches are collected like any other: they are exactly the
ones that go silent after F2.

Not collected, because these are probes and not reads:

* ``hasattr(A, "X")``;
* ``getattr(A, "X", default)``;
* a non-literal name in a test source (``getattr(A, name)``,
  ``monkeypatch.setattr(A, name, ...)``): counted and reported.  Such a read
  raises on its own once the name is gone, unless it is a patch with
  ``raising=False``: that one silently creates the name, and this guard does
  not catch it.  The same holds for a facade path
  string that reaches a patch call through a variable (a parametrised target);
  those are listed in the report too.

In an owner source a non-literal ``getattr(A, name)`` / ``setattr`` /
``delattr`` / ``patch.object`` on a facade binding fails the guard unless it is
inside a recognised dispatch wrapper, and so does a non-literal
``getattr(A, name, default)`` and a call to a dispatch wrapper whose name
argument is not a literal.  None of them can be resolved statically, and each
has zero sites at a306fd5e4.

Not bindings, so not scanned (out of the 10.2 contract): relative imports
(``from .scheduler import X``, ``from . import chain``),
``importlib.import_module(...)`` and ``sys.modules[...]`` lookups.

Three silent forms are not collected either, and each has zero facade sites at
f45e332b2: a dispatch wrapper that rebinds its name parameter before the
``getattr`` (the name it really reads is never seen),
``functools.partial(_scheduler_call, "X")``, and a keyword-form string target
such as ``mock.patch(target="services.orchestrator.chain.X")``.

``EXEMPTIONS`` may hold a target that does not resolve today only when a test
deliberately injects a new attribute; each entry carries its site and a
written reason.  It is empty.  An entry that resolves, has no reason, or
whose site no longer reads that name fails the guard.
"""

from __future__ import annotations

import ast
import collections
import importlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OWNER_ROOT = "services/orchestrator"
SCAN_ROOTS = ("services/orchestrator", "tests")

FACADE_MODULES = {
    "services.orchestrator.scheduler": "scheduler",
    "services.orchestrator.chain": "chain",
}
FACADE_SOURCES = {
    "services/orchestrator/scheduler.py": "scheduler",
    "services/orchestrator/chain.py": "chain",
}
_FACADE_IMPORT_PARENT = "services.orchestrator"
_STRING_TARGET_RE = re.compile(r"^services\.orchestrator\.(scheduler|chain)\.([A-Za-z_][A-Za-z0-9_]*)")
_FULL_STRING_TARGET_RE = re.compile(r"^services\.orchestrator\.(scheduler|chain)(\.[A-Za-z_][A-Za-z0-9_]*)+$")

ALL_FORMS = (
    "attr-load",
    "attr-store",
    "attr-del",
    "bare-dotted",
    "getattr",
    "setattr-object",
    "delattr-object",
    "patch-object",
    "setattr-string",
    "delattr-string",
    "patch-string",
    "from-import",
    "owner-dispatch",
)
# Forms that each facade must yield at least once at HEAD (coverage meta-test).
REQUIRED_FORMS_PER_FACADE = ("attr-load", "from-import", "getattr", "setattr-object", "setattr-string")


@dataclass(frozen=True)
class Read:
    facade: str
    name: str
    form: str
    path: str
    line: int


@dataclass(frozen=True)
class Exemption:
    facade: str
    name: str
    path: str
    reason: str


@dataclass
class ScanResult:
    reads: list[Read] = field(default_factory=list)
    # Test sources only: (path, line, kind) of a facade read whose name is not a literal.
    nonliteral: list[tuple[str, int, str]] = field(default_factory=list)
    # Owner sources only: (path, line, message) of a facade access that cannot be resolved.
    violations: list[tuple[str, int, str]] = field(default_factory=list)
    # Owner sources only: (path, line, wrapper) of the getattr inside a dispatch wrapper.
    wrapper_sites: list[tuple[str, int, str]] = field(default_factory=list)
    # Test sources only: facade path strings that are not a literal patch-call target.
    stray_string_targets: list[tuple[str, int, str]] = field(default_factory=list)

    def extend(self, other: ScanResult) -> None:
        self.reads.extend(other.reads)
        self.nonliteral.extend(other.nonliteral)
        self.violations.extend(other.violations)
        self.wrapper_sites.extend(other.wrapper_sites)
        self.stray_string_targets.extend(other.stray_string_targets)


# Targets that do not resolve today, allowed only because the test at ``path``
# deliberately injects a new attribute on the facade.  Empty at a306fd5e4.
EXEMPTIONS: tuple[Exemption, ...] = ()


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _str_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_patch_object(func: ast.AST) -> bool:
    if not (isinstance(func, ast.Attribute) and func.attr == "object"):
        return False
    receiver = _dotted(func.value)
    return receiver is not None and receiver.split(".")[-1] == "patch"


def _facade_bindings(statements: Iterable[ast.stmt]) -> dict[str, str]:
    """Facade aliases bound directly in a scope (nested defs and lambdas excluded)."""

    aliases: dict[str, str] = {}
    stack: list[ast.AST] = list(statements)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == _FACADE_IMPORT_PARENT:
            for item in node.names:
                if item.name in FACADE_MODULES.values():
                    aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.Import):
            for item in node.names:
                if item.name in FACADE_MODULES and item.asname:
                    aliases[item.asname] = FACADE_MODULES[item.name]
        stack.extend(ast.iter_child_nodes(node))
    return aliases


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> list[str]:
    args = node.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    names.extend(a.arg for a in (args.vararg, args.kwarg) if a is not None)
    return names


def _positional_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [a.arg for a in (*node.args.posonlyargs, *node.args.args)]


def _function_scope(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, parent: dict[str, str]
) -> dict[str, str]:
    scope = {k: v for k, v in parent.items() if k not in set(_parameters(node))}
    if not isinstance(node, ast.Lambda):
        scope.update(_facade_bindings(node.body))
    return scope


def _dispatch_wrappers(tree: ast.Module, module_scope: dict[str, str]) -> dict[str, tuple[str, int, str]]:
    """Module-level functions whose body calls ``getattr(<facade>, <own parameter>)``.

    Returns ``{function name: (facade, positional index of the parameter, parameter)}``.
    """

    wrappers: dict[str, tuple[str, int, str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope = _function_scope(node, module_scope)
        positional = _positional_parameters(node)
        for inner in ast.walk(node):
            if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "getattr"):
                continue
            if len(inner.args) != 2 or inner.keywords:
                continue
            target, name = inner.args
            if isinstance(target, ast.Name) and target.id in scope and isinstance(name, ast.Name):
                if name.id in positional:
                    wrappers[node.name] = (scope[target.id], positional.index(name.id), name.id)
    return wrappers


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str, *, owner: bool, tree: ast.Module) -> None:
        self.path = path
        self.owner = owner
        self.self_facade = FACADE_SOURCES.get(path)
        self.result = ScanResult()
        module_scope = _facade_bindings(tree.body)
        self.scopes: list[dict[str, str]] = [module_scope]
        self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda] = []
        self.wrappers = _dispatch_wrappers(tree, module_scope) if owner else {}
        self.patch_targets: set[int] = set()

    # -- helpers -----------------------------------------------------------
    def _read(self, facade: str, name: str, form: str, line: int) -> None:
        if facade == self.self_facade:
            return
        self.result.reads.append(Read(facade, name, form, self.path, line))

    def _facade_of(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.scopes[-1].get(node.id)
        dotted = _dotted(node)
        return FACADE_MODULES.get(dotted) if dotted is not None else None

    def _dynamic(self, node: ast.Call, facade: str, kind: str) -> None:
        if facade == self.self_facade:
            return
        if not self.owner:
            self.result.nonliteral.append((self.path, node.lineno, kind))
            return
        self.result.violations.append(
            (self.path, node.lineno, f"non-literal {kind} on facade {facade!r} outside a dispatch wrapper")
        )

    def _in_wrapper(self, name_node: ast.AST) -> str | None:
        if len(self.functions) != 1 or not isinstance(name_node, ast.Name):
            return None
        function = self.functions[0]
        if isinstance(function, ast.Lambda):
            return None
        wrapper = self.wrappers.get(function.name)
        if wrapper is not None and wrapper[2] == name_node.id:
            return function.name
        return None

    # -- scopes ------------------------------------------------------------
    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        if not isinstance(node, ast.Lambda):
            for decorator in node.decorator_list:
                self.visit(decorator)
        args = node.args
        outer = [*args.defaults, *[d for d in args.kw_defaults if d is not None]]
        if not isinstance(node, ast.Lambda):
            outer.extend(a.annotation for a in (*args.posonlyargs, *args.args, *args.kwonlyargs) if a.annotation)
            outer.extend(a.annotation for a in (args.vararg, args.kwarg) if a is not None and a.annotation)
            if node.returns is not None:
                outer.append(node.returns)
        for expression in outer:
            self.visit(expression)
        self.scopes.append(_function_scope(node, self.scopes[-1]))
        self.functions.append(node)
        body = [node.body] if isinstance(node, ast.Lambda) else node.body
        for statement in body:
            self.visit(statement)
        self.functions.pop()
        self.scopes.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function
    visit_Lambda = _visit_function

    # -- reads -------------------------------------------------------------
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module in FACADE_MODULES:
            for item in node.names:
                if item.name != "*":
                    self._read(FACADE_MODULES[node.module], item.name, "from-import", node.lineno)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            facade = self.scopes[-1].get(node.value.id)
            if facade is not None:
                form = {ast.Load: "attr-load", ast.Store: "attr-store", ast.Del: "attr-del"}[type(node.ctx)]
                self._read(facade, node.attr, form, node.lineno)
        else:
            dotted = _dotted(node.value)
            if dotted in FACADE_MODULES:
                self._read(FACADE_MODULES[dotted], node.attr, "bare-dotted", node.lineno)
                return
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if (
            not self.owner
            and isinstance(node.value, str)
            and id(node) not in self.patch_targets
            and _FULL_STRING_TARGET_RE.match(node.value)
        ):
            self.result.stray_string_targets.append((self.path, node.lineno, node.value))

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        args = node.args
        if name == "getattr" and isinstance(node.func, ast.Name) and args:
            facade = self._facade_of(args[0])
            if facade is not None and len(args) == 2 and not node.keywords:
                literal = _str_constant(args[1])
                if literal is not None:
                    self._read(facade, literal, "getattr", node.lineno)
                elif self.owner and (wrapper := self._in_wrapper(args[1])) is not None:
                    self.result.wrapper_sites.append((self.path, node.lineno, wrapper))
                else:
                    self._dynamic(node, facade, "getattr")
            elif facade is not None and self.owner and len(args) == 3 and _str_constant(args[1]) is None:
                # A defaulted probe with a non-literal name: not resolvable statically (0 sites at a306fd5e4).
                self._dynamic(node, facade, "getattr-with-default")
        elif name in ("setattr", "delattr") or _is_patch_object(node.func):
            form = "patch-object" if _is_patch_object(node.func) else f"{name}-object"
            if args:
                facade = self._facade_of(args[0])
                literal_target = _str_constant(args[0])
                if facade is not None and len(args) >= 2:
                    literal = _str_constant(args[1])
                    if literal is not None:
                        self._read(facade, literal, form, node.lineno)
                    else:
                        self._dynamic(node, facade, form)
                elif literal_target is not None and form != "patch-object":
                    self._string_target(args[0], literal_target, f"{name}-string")
        elif name == "patch" and args:
            literal_target = _str_constant(args[0])
            if literal_target is not None:
                self._string_target(args[0], literal_target, "patch-string")
        if isinstance(node.func, ast.Name) and node.func.id in self.wrappers:
            facade, index, _ = self.wrappers[node.func.id]
            if len(args) > index and not any(isinstance(a, ast.Starred) for a in args[: index + 1]):
                literal = _str_constant(args[index])
                if literal is not None:
                    self._read(facade, literal, "owner-dispatch", node.lineno)
                else:
                    self.result.violations.append(
                        (self.path, node.lineno, f"dispatch wrapper {node.func.id}() called with a non-literal name")
                    )
            else:
                self.result.violations.append(
                    (self.path, node.lineno, f"dispatch wrapper {node.func.id}() called without a literal name")
                )
        self.generic_visit(node)

    def _string_target(self, node: ast.AST, value: str, form: str) -> None:
        match = _STRING_TARGET_RE.match(value)
        if match is not None:
            self.patch_targets.add(id(node))
            self._read(match.group(1), match.group(2), form, node.lineno)


def scan_source(source: str, path: str) -> ScanResult:
    """Collect the facade reads of one source; ``path`` is repo-relative POSIX."""

    tree = ast.parse(source, filename=path)
    collector = _Collector(path, owner=path.startswith(OWNER_ROOT + "/"), tree=tree)
    collector.visit(tree)
    return collector.result


def iter_scanned_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for scan_root in SCAN_ROOTS:
        files.update(path for path in (root / scan_root).rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files)


@lru_cache(maxsize=None)
def scan_tree(root: Path = REPO_ROOT) -> ScanResult:
    result = ScanResult()
    for path in iter_scanned_files(root):
        result.extend(scan_source(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix()))
    return result


@lru_cache(maxsize=None)
def _facade_module(facade: str):
    module = {short: full for full, short in FACADE_MODULES.items()}[facade]
    return importlib.import_module(module)


def resolves(facade: str, name: str) -> bool:
    return hasattr(_facade_module(facade), name)


def _exempted(read: Read, table: Sequence[Exemption]) -> bool:
    return any((e.facade, e.name, e.path) == (read.facade, read.name, read.path) for e in table)


def unresolved_reads(
    reads: Iterable[Read],
    table: Sequence[Exemption] = EXEMPTIONS,
    resolver: Callable[[str, str], bool] = resolves,
) -> list[Read]:
    return sorted(
        (r for r in reads if not resolver(r.facade, r.name) and not _exempted(r, table)),
        key=lambda r: (r.facade, r.name, r.path, r.line),
    )


def exemption_problems(
    table: Sequence[Exemption],
    reads: Sequence[Read],
    resolver: Callable[[str, str], bool] = resolves,
) -> list[str]:
    problems = []
    for entry in table:
        label = f"({entry.facade}, {entry.name}, {entry.path})"
        if not entry.reason.strip():
            problems.append(f"{label}: no written reason")
        if resolver(entry.facade, entry.name):
            problems.append(f"{label}: resolves on the facade; delete the exemption")
        if not any((r.facade, r.name, r.path) == (entry.facade, entry.name, entry.path) for r in reads):
            problems.append(f"{label}: site gone (no read of that name in that file); delete the exemption")
    return problems


def _format_unresolved(unresolved: Sequence[Read]) -> str:
    lines = [f"{len(unresolved)} facade read(s) do not resolve on the imported facade:"]
    lines.extend(f"  ({r.facade}, {r.name}, {r.path}:{r.line}) [{r.form}]" for r in unresolved)
    lines.append(
        "Add an explicit import of the name to the facade, or delete the read. Do not add an exemption "
        "unless the test deliberately injects a new attribute."
    )
    return "\n".join(lines)


def _report(result: ScanResult) -> str:
    forms = collections.Counter((r.facade, r.form) for r in result.reads)
    distinct = len({(r.facade, r.name) for r in result.reads})
    lines = [f"collected reads: {len(result.reads)}; distinct (facade, name): {distinct}"]
    for facade in sorted(FACADE_MODULES.values()):
        facade_reads = [r for r in result.reads if r.facade == facade]
        lines.append(f"  {facade}: reads={len(facade_reads)} distinct_names={len({r.name for r in facade_reads})}")
        for form in ALL_FORMS:
            lines.append(f"    {form}: {forms.get((facade, form), 0)}")
    dispatch = sorted({r.name for r in result.reads if r.form == "owner-dispatch"})
    lines.append(f"owner-dispatch distinct literal names: {len(dispatch)}")
    lines.append(f"dispatch wrapper getattr sites: {sorted(set(result.wrapper_sites))}")
    lines.append(f"non-literal reads in tests (counted, not collected): {len(result.nonliteral)}")
    for kind, count in sorted(collections.Counter(kind for _, _, kind in result.nonliteral).items()):
        lines.append(f"    {kind}: {count}")
    lines.append(f"facade path strings outside a literal patch target (tests): {len(result.stray_string_targets)}")
    lines.extend(f"    {path}:{line} {value}" for path, line, value in result.stray_string_targets)
    lines.append(f"owner violations: {len(result.violations)}")
    lines.append(f"exemption table ({len(EXEMPTIONS)} entries): {list(EXEMPTIONS)}")
    return "\n".join(lines)


# -- the guard ---------------------------------------------------------------


def test_every_facade_read_resolves() -> None:
    result = scan_tree()
    print(_report(result))
    unresolved = unresolved_reads(result.reads)
    if unresolved:
        pytest.fail(_format_unresolved(unresolved), pytrace=False)


def test_owner_sources_have_no_unresolvable_dynamic_facade_access() -> None:
    violations = sorted(scan_tree().violations)
    if violations:
        pytest.fail("\n".join(f"{path}:{line} {message}" for path, line, message in violations), pytrace=False)


def test_exemption_table_is_valid() -> None:
    assert exemption_problems(EXEMPTIONS, scan_tree().reads) == []


def test_exemption_self_check_rejects_stale_entries() -> None:
    reads = [Read("chain", "_injected_3270", "setattr-object", "tests/test_x.py", 3)]
    never = lambda facade, name: False  # noqa: E731
    always = lambda facade, name: True  # noqa: E731
    good = Exemption("chain", "_injected_3270", "tests/test_x.py", "test injects a new attribute")
    assert exemption_problems([good], reads, never) == []
    assert unresolved_reads(reads, [good], never) == []
    assert unresolved_reads(reads, [], never) == reads

    resolved = exemption_problems([good], reads, always)
    assert len(resolved) == 1 and "resolves on the facade" in resolved[0]
    gone = exemption_problems([Exemption("chain", "_injected_3270", "tests/test_y.py", "r")], reads, never)
    assert len(gone) == 1 and "site gone" in gone[0]
    unreasoned = exemption_problems([Exemption("chain", "_injected_3270", "tests/test_x.py", " ")], reads, never)
    assert len(unreasoned) == 1 and "no written reason" in unreasoned[0]


# -- meta-test 1: bite rows ----------------------------------------------------

MISSING = "_no_such_name_3270"
OWNER_PATH = "services/orchestrator/bite_row_owner.py"
TEST_PATH = "tests/test_bite_row.py"
S = "from services.orchestrator import scheduler as _scheduler\n"
C = "from services.orchestrator import chain as _chain\n"

# (id, path, source, expected flags) where a flag is (facade, name, form) for an
# unresolved read, or ("violation", "", "") for an owner violation.
_FLAG_ROWS = [
    ("attr-load", OWNER_PATH, S + f"_scheduler.{MISSING}\n", {("scheduler", MISSING, "attr-load")}),
    ("attr-store", OWNER_PATH, C + f"_chain.{MISSING} = 1\n", {("chain", MISSING, "attr-store")}),
    ("attr-del", TEST_PATH, C + f"del _chain.{MISSING}\n", {("chain", MISSING, "attr-del")}),
    (
        "attr-nested",
        TEST_PATH,
        "from services.orchestrator import chain\n" + f"chain.{MISSING}.monotonic()\n",
        {("chain", MISSING, "attr-load")},
    ),
    (
        "import-as",
        TEST_PATH,
        "import services.orchestrator.scheduler as scheduler_module\n" + f"scheduler_module.{MISSING}\n",
        {("scheduler", MISSING, "attr-load")},
    ),
    (
        "bare-dotted",
        TEST_PATH,
        "import services.orchestrator.chain\n" + f"services.orchestrator.chain.{MISSING}\n",
        {("chain", MISSING, "bare-dotted")},
    ),
    (
        "function-local-alias",
        TEST_PATH,
        "def test_x():\n    import services.orchestrator.chain as legacy_chain\n" + f"    legacy_chain.{MISSING}\n",
        {("chain", MISSING, "attr-load")},
    ),
    (
        "nested-function-inherits-alias",
        TEST_PATH,
        "def test_x():\n    from services.orchestrator import chain as c\n"
        f"    def inner():\n        return c.{MISSING}\n",
        {("chain", MISSING, "attr-load")},
    ),
    ("getattr", OWNER_PATH, S + f"getattr(_scheduler, {MISSING!r})\n", {("scheduler", MISSING, "getattr")}),
    ("setattr-builtin", TEST_PATH, C + f"setattr(_chain, {MISSING!r}, 1)\n", {("chain", MISSING, "setattr-object")}),
    (
        "monkeypatch-setattr-raising-false",
        TEST_PATH,
        S + f"def test_x(monkeypatch):\n    monkeypatch.setattr(_scheduler, {MISSING!r}, 1, raising=False)\n",
        {("scheduler", MISSING, "setattr-object")},
    ),
    (
        "monkeypatch-delattr",
        TEST_PATH,
        C + f"def test_x(monkeypatch):\n    monkeypatch.delattr(_chain, {MISSING!r})\n",
        {("chain", MISSING, "delattr-object")},
    ),
    (
        "mock-patch-object",
        TEST_PATH,
        "from unittest import mock\n" + S + f"mock.patch.object(_scheduler, {MISSING!r})\n",
        {("scheduler", MISSING, "patch-object")},
    ),
    (
        "patch-object",
        TEST_PATH,
        "from unittest.mock import patch\n" + C + f"@patch.object(_chain, {MISSING!r})\ndef test_x(m):\n    pass\n",
        {("chain", MISSING, "patch-object")},
    ),
    (
        "bare-dotted-setattr",
        TEST_PATH,
        "import services.orchestrator.scheduler\n"
        f"def test_x(monkeypatch):\n    monkeypatch.setattr(services.orchestrator.scheduler, {MISSING!r}, 1)\n",
        {("scheduler", MISSING, "setattr-object")},
    ),
    (
        "string-setattr",
        TEST_PATH,
        f"def test_x(monkeypatch):\n    monkeypatch.setattr('services.orchestrator.scheduler.{MISSING}', 1)\n",
        {("scheduler", MISSING, "setattr-string")},
    ),
    (
        "string-setattr-nested",
        TEST_PATH,
        f"def test_x(monkeypatch):\n    monkeypatch.setattr('services.orchestrator.chain.{MISSING}.monotonic', 1)\n",
        {("chain", MISSING, "setattr-string")},
    ),
    (
        "string-delattr",
        TEST_PATH,
        f"def test_x(monkeypatch):\n    monkeypatch.delattr('services.orchestrator.chain.{MISSING}', raising=False)\n",
        {("chain", MISSING, "delattr-string")},
    ),
    (
        "string-mock-patch",
        TEST_PATH,
        f"from unittest import mock\nmock.patch('services.orchestrator.scheduler.{MISSING}')\n",
        {("scheduler", MISSING, "patch-string")},
    ),
    (
        "string-patch",
        TEST_PATH,
        f"from unittest.mock import patch\n@patch('services.orchestrator.chain.{MISSING}')\ndef test_x(m):\n    pass\n",
        {("chain", MISSING, "patch-string")},
    ),
    (
        "from-import",
        TEST_PATH,
        f"def test_x():\n    from services.orchestrator.chain import ForecastOrchestrator, {MISSING}\n",
        {("chain", MISSING, "from-import")},
    ),
    (
        "owner-dispatch",
        OWNER_PATH,
        S
        + "def _scheduler_call(name, *args, **kwargs):\n    return getattr(_scheduler, name)(*args, **kwargs)\n"
        + f"def helper():\n    return _scheduler_call({MISSING!r})\n",
        {("scheduler", MISSING, "owner-dispatch")},
    ),
    (
        "owner-dispatch-non-literal",
        OWNER_PATH,
        S
        + "def _scheduler_call(name, *args, **kwargs):\n    return getattr(_scheduler, name)(*args, **kwargs)\n"
        + "def helper(key):\n    return _scheduler_call(key)\n",
        {("violation", "", "")},
    ),
    (
        "owner-getattr-outside-wrapper-module-level",
        OWNER_PATH,
        S + "for name in ('ProductionScheduler',):\n    getattr(_scheduler, name)\n",
        {("violation", "", "")},
    ),
    (
        "owner-getattr-outside-wrapper-local-variable",
        OWNER_PATH,
        S + "def helper(key):\n    name = key.strip()\n    return getattr(_scheduler, name)\n",
        {("violation", "", "")},
    ),
    (
        "owner-getattr-in-nested-function",
        OWNER_PATH,
        S + "def outer():\n    def inner(name):\n        return getattr(_scheduler, name)\n    return inner\n",
        {("violation", "", "")},
    ),
    (
        "owner-getattr-non-parameter",
        OWNER_PATH,
        C + "NAME = 'x'\ndef helper(name):\n    return getattr(_chain, NAME)\n",
        {("violation", "", "")},
    ),
    (
        "owner-getattr-default-non-literal",
        OWNER_PATH,
        C + "def helper(name):\n    return getattr(_chain, name, None)\n",
        {("violation", "", "")},
    ),
    (
        "owner-setattr-non-literal",
        OWNER_PATH,
        C + "def helper(name, value):\n    setattr(_chain, name, value)\n",
        {("violation", "", "")},
    ),
]

_NO_FLAG_ROWS = [
    ("hasattr", TEST_PATH, S + f"assert not hasattr(_scheduler, {MISSING!r})\n"),
    ("getattr-default", OWNER_PATH, C + f"getattr(_chain, {MISSING!r}, None)\n"),
    (
        "non-facade-alias-same-spelling",
        OWNER_PATH,
        "from services.orchestrator import chain_types as _chain\n" + f"_chain.{MISSING}\n",
    ),
    ("non-facade-module-same-name", TEST_PATH, "from somewhere_else import scheduler\n" + f"scheduler.{MISSING}\n"),
    (
        "other-module-dotted",
        TEST_PATH,
        f"import services.orchestrator.chain_types\nservices.orchestrator.chain_types.{MISSING}\n",
    ),
    (
        "alias-does-not-leak-across-functions",
        TEST_PATH,
        "def test_a():\n    from services.orchestrator import chain\n    return chain\n"
        f"def test_b(chain):\n    return chain.{MISSING}\n",
    ),
    ("parameter-shadows-module-alias", TEST_PATH, C + f"def test_b(_chain):\n    return _chain.{MISSING}\n"),
    ("resolving-names", TEST_PATH, S + C + "_scheduler.ProductionScheduler\n_chain.ForecastOrchestrator\n"),
    (
        "resolving-string-target",
        TEST_PATH,
        "def test_x(monkeypatch):\n    monkeypatch.setattr('services.orchestrator.chain.time.monotonic', lambda: 0)\n",
    ),
    ("test-non-literal-getattr", TEST_PATH, C + "def test_x(name):\n    getattr(_chain, name)\n"),
    (
        "facade-own-owner-import",
        "services/orchestrator/chain.py",
        f"from services.orchestrator.chain_types import {MISSING}\n"
        "from services.orchestrator import chain_stages as _chain_stages\n",
    ),
    ("relative-import-out-of-contract", OWNER_PATH, f"from .scheduler import {MISSING}\n"),
]


def _flags(source: str, path: str) -> set[tuple[str, str, str]]:
    result = scan_source(source, path)
    flags = {(r.facade, r.name, r.form) for r in unresolved_reads(result.reads, ())}
    if result.violations:
        flags.add(("violation", "", ""))
    return flags


@pytest.mark.parametrize(
    ("path", "source", "expected"), [row[1:] for row in _FLAG_ROWS], ids=[r[0] for r in _FLAG_ROWS]
)
def test_bite_row_flags(path: str, source: str, expected: set[tuple[str, str, str]]) -> None:
    assert _flags(source, path) == expected


@pytest.mark.parametrize(("path", "source"), [row[1:] for row in _NO_FLAG_ROWS], ids=[r[0] for r in _NO_FLAG_ROWS])
def test_bite_row_does_not_flag(path: str, source: str) -> None:
    assert _flags(source, path) == set()


def test_bite_rows_cover_every_read_form() -> None:
    covered = {form for _, _, _, expected in _FLAG_ROWS for _, _, form in expected}
    assert set(ALL_FORMS) <= covered


def test_non_literal_test_read_is_counted_not_collected() -> None:
    result = scan_source(C + "def test_x(monkeypatch, name):\n    monkeypatch.setattr(_chain, name, 1)\n", TEST_PATH)
    assert result.reads == []
    assert result.violations == []
    assert result.nonliteral == [(TEST_PATH, 3, "setattr-object")]


# -- meta-test 2: coverage at HEAD ---------------------------------------------


def test_collector_coverage_at_head() -> None:
    result = scan_tree()
    forms = collections.Counter((r.facade, r.form) for r in result.reads)
    print(_report(result))
    missing = [
        (facade, form)
        for facade in sorted(FACADE_MODULES.values())
        for form in REQUIRED_FORMS_PER_FACADE
        if not forms.get((facade, form))
    ]
    assert not missing, f"collector found no site for: {missing}"
    assert forms.get(("scheduler", "owner-dispatch"), 0) >= 1
    assert {wrapper for _, _, wrapper in result.wrapper_sites} == {"_scheduler_call"}
