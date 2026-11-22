"""Retry-accounting AST guard (OpenSpec change ``redesign-orchestration-core``, task 9.5, design D2).

``services/orchestrator/retry_identity.py`` is the single owner of persisted
retry identity: the ``<base>_retry_<n>`` job id and the attempt number in it.
This guard parses every production ``*.py`` and fails on any module that
builds such an id locally, splits on the marker, or adds an integer literal to
a retry reader's result.  The allowlist is empty -- T2 (tasks 9.1-9.4) left no
local site -- so there is no allowlist file; an entry would need a written
reason and a deliberate edit here.

Scope:

* files: ``sorted(root.rglob("*.py"))`` for every root in ``_SCAN_ROOTS``,
  skipping any path with a part in ``_EXCLUDED_DIR_PARTS``;
* ``services/orchestrator/retry_identity.py`` (the owner) is excluded by exact
  repo-relative path.  It is the only exclusion;
* ``tests/`` is out of scope: the T1 table and the owner tests build ids on
  purpose;
* a ``SyntaxError`` is left to surface: a module the guard cannot parse is a
  module whose retry accounting it cannot see.

A *marker* is any of:

* a str constant that starts with ``_retry_(%|\\{|\\d|$)``: the bare marker,
  ``_retry_<digits>`` and the leading templates ``"_retry_%d"`` /
  ``"_retry_{}"`` / ``"_retry_{n}"``;
* a str constant containing
  ``(%(\\([^)]*\\))?[-#0 +]*\\d*(\\.\\d+)?[sdriu]|\\})_retry_(%|\\{|\\d|$)``: the
  ``%`` / ``.format`` placeholder forms, including width, keyed and positional
  ones (``"%5s_retry_%03d"``, ``"%(b)s_retry_%(n)d"``, ``"{0}_retry_{1}"``);
* the name ``RETRY_JOB_ID_MARKER``, or an attribute with that name;
* an alias: a name bound by ``X = <marker>`` (``=`` or annotated ``=``, whose
  value is a marker constant -- template constants included -- a marker name
  or a marker attribute) or by ``from ... import RETRY_JOB_ID_MARKER as M``.

Aliases are collected per scope, breadth-first, before the scope is scanned,
so a use may precede its alias binding; a chained alias (``B = A``) resolves
only when ``A`` is collected first in that order.  A module-level alias
applies everywhere in the module, an alias bound in a ``def`` / ``async def``
applies inside that function and the functions nested in it, and nowhere
else: a local ``sep = "_retry_"`` does not turn another function's ``sep``
parameter into a marker.
A class body is not a scope of its own, so a class attribute alias applies
in the class's enclosing scope (a fail-safe over-reach).

Field names such as ``"_retry_count"``, ``f"{stage}_retry_count"``,
``prefix + "_retry_attempt_floor"``, ``"%s_retry_count" % stage``,
``"{}_retry_limit".format(x)``, ``key += "_retry_count"`` and
``k.removesuffix("_retry_count")`` are therefore not markers, and neither is
unrelated text such as ``f"manual_retry_{key}"``.  ``"_retry_" in job_id``,
``re.compile(r"_retry_\\d+$")`` and ``re.compile(f"{re.escape(base)}_retry_")``
are parse / membership uses and never flag.

Violation kinds (the failure message is ``path:lineno kind``):

* ``id-fstring``: an f-string with a marker ``FormattedValue``, or with a
  constant part that is a marker (``_retry_`` followed by a ``FormattedValue``,
  or a full ``_retry_\\d*`` part).  An f-string in the pattern position
  (``args[0]`` or the ``pattern=`` keyword) of ``re.compile`` / ``re.match`` /
  ``re.search`` / ``re.fullmatch`` (receiver: the name ``re``) is a regex, not
  an id, and does not flag; anything nested inside it is still scanned.  An
  f-string in any other argument -- the subject, ``string=`` -- is scanned as
  usual, so ``re.fullmatch(PATTERN, f"{base}_retry_{n}")`` flags;
* ``id-concat``: ``BinOp`` ``Add`` / ``Mod`` with a marker operand, or a
  ``Mod`` -- whatever its left side (a literal, a name, an attribute) --
  whose right side is a Tuple with a marker element or a Dict with a marker
  value: ``"%s%s%d" % (base, RETRY_JOB_ID_MARKER, n)``, ``fmt % (b,
  RETRY_JOB_ID_MARKER, n)``;
* ``id-format``: ``.format`` / ``.format_map`` with a marker receiver (a
  marker constant or an alias such as ``TPL = "{}_retry_{}"``), a marker
  positional argument or keyword value, a marker value of a ``**{...}`` Dict
  literal, or -- for ``format_map`` -- a marker value of a Dict literal
  argument;
* ``id-join``: ``join`` with a marker receiver, or with an argument that is,
  or structurally contains, a marker: from each argument the guard descends
  through ``BinOp`` (both sides), ``IfExp`` (body and ``orelse``),
  ``Starred`` and List / Tuple / Set literal elements, as in ``"".join([b,
  RETRY_JOB_ID_MARKER] + [str(n)])``.  It never enters a generator, a
  comprehension or a call, so ``", ".join(j for j in ids if
  RETRY_JOB_ID_MARKER in j)`` stays clean;
* ``id-augassign``: ``x += <marker>``;
* ``marker-split``: ``rsplit`` / ``split`` / ``partition`` / ``rpartition`` /
  ``removesuffix`` / ``rstrip`` with a marker first argument or ``sep=``
  keyword;
* ``parser+int``: ``BinOp`` ``Add`` of an int literal and an operand that
  derives from a reader, in either order;
* ``parser+=int``: ``AugAssign`` ``Add`` of an int literal on a name that
  derives from a reader.

The *reader set* needs no hand-maintained list:

* every public function of the owner: the top-level ``FunctionDef`` /
  ``AsyncFunctionDef`` names of ``retry_identity.py``, read from its AST (not
  ``inspect``, so a future decorator cannot hide one), not ``_``-prefixed;
* plus every call whose callee name (bare or attribute) contains ``attempt``.
  This covers the private scheduler-state readers (``_state_retry_attempt``,
  ``_job_retry_attempt``, ``_fallback_previous_attempt``,
  ``_retry_attempt_from_basins``) and their family with no list.

An operand *derives from a reader* when its subtree (``ast.walk``) contains a
call to a reader, a ``marker-split`` call, or a tainted name.  Because the
reader may sit anywhere in that subtree, ``len(<reader>(...)) + 1`` flags too:
a fail-safe over-flag, kept so that no wrapper can hide a reader.  Taint is per
function (a module body, ``def``, ``async def`` or ``lambda`` each get a fresh
scope) and name-based only.  A name is tainted when it is:

* the target of an assignment (``=``, annotated ``=``, ``:=``) whose value
  derives from a reader -- only ``ast.Name`` targets and the Names inside
  Tuple / List / Starred targets bind; the base of an Attribute or Subscript
  target (``self.a = ...``, ``d[k] = ...``) is never tainted;
* the Name receiver of ``append`` / ``extend`` / ``add`` / ``insert`` /
  ``appendleft`` called with an argument that derives from a reader.

Taint follows source order within its scope: a name is tainted from its
binding onward, so a use that precedes the binding (e.g. earlier in a loop
body) is not flagged, and a later rebinding does not clear it.  Taint does
not cross a function boundary in either direction: closures and callers are
not followed.

The guard is a syntactic heuristic, not a proof.  It catches every form T2
removed (the 26 pre-T2 hits at ``5570a72b4``) and the common construction,
split and increment shapes listed above.  The misses below are known
examples, and the list is not exhaustive.  Each listed miss has a not-flagged
bite row, so tightening one later is a deliberate edit.  A new miss found
later is fixed by tightening the guard or by adding a row; it never needs an
allowlist entry.

Known misses:

* ``f"{a}x_retry_{n}"``: the marker does not start a constant part.  Accepted
  so that unrelated text stays clean;
* a counter read from row data plus one, e.g. ``int(source.get("retry_count")
  or 0) + 1`` (the pre-T2 non-master formula).  It cannot be told apart from
  legitimate counters such as a ``submission_attempt + 1`` local;
* a regex-group parse plus one: ``int(re.match(r"(.*)_retry_(\\d+)$",
  j).group(2)) + 1``;
* a late-binding call ``getattr(_chain, "_retry_attempt_from_basins")(...) +
  1``: the callee name is a string;
* ``for`` / comprehension targets (never tainted), closures (taint does not
  cross the boundary), and ``sum(...)`` over reader results that does the
  addition itself (``sum([reader(...)], 1)``: no ``BinOp``);
* subscript-store taint: ``by[j] = retry_suffix_attempt(j)`` then
  ``max(by.values()) + 1`` (a Subscript target binds nothing);
* ``operator.add(<reader>(...), 1)``: the addition is a call, not a ``BinOp``;
* an ``rfind`` / ``index`` offset plus slicing, e.g.
  ``int(j[j.rfind("_retry_") + 7:]) + 1``: ``rfind`` / ``index`` are neither
  splitters nor readers;
* a ``join`` over a generator or a ``map(...)`` with the marker inside, e.g.
  ``"".join(str(p) for p in (base, RETRY_JOB_ID_MARKER, n))``: ``id-join``
  does not enter generator / call arguments, so that membership filters stay
  clean.
"""

from __future__ import annotations

import ast
import re
from collections import deque
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

OWNER_RELATIVE_PATH = "services/orchestrator/retry_identity.py"

_SCAN_ROOTS: tuple[str, ...] = ("apps", "db", "packages", "scripts", "services", "workers")

_EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
)

MARKER_NAME = "RETRY_JOB_ID_MARKER"

_MARKER_CONSTANT = re.compile(r"_retry_\d*")
_LEADING_MARKER = re.compile(r"_retry_(%|\{|\d|$)")
_PLACEHOLDER_MARKER = re.compile(r"(%(\([^)]*\))?[-#0 +]*\d*(\.\d+)?[sdriu]|\})_retry_(%|\{|\d|$)")

_SPLITTERS: frozenset[str] = frozenset({"rsplit", "split", "partition", "rpartition", "removesuffix", "rstrip"})
_CONTAINER_ADDERS: frozenset[str] = frozenset({"append", "extend", "add", "insert", "appendleft"})
_REGEX_FUNCTIONS: frozenset[str] = frozenset({"compile", "match", "search", "fullmatch"})
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

# The private scheduler-state readers behind the 9.3 sites.  They are not owner
# functions; the ``attempt`` name rule covers them.  Pinned by module so that a
# rename which drops ``attempt`` from the name cannot silently leave the set.
_SCHEDULER_READERS: dict[str, str] = {
    "_state_retry_attempt": "services/orchestrator/scheduler_state_rows.py",
    "_job_retry_attempt": "services/orchestrator/scheduler_state_rows.py",
    "_fallback_previous_attempt": "services/orchestrator/scheduler_state_manual_retry.py",
    "_retry_attempt_from_basins": "services/orchestrator/chain_runtime_utils.py",
}


def _top_level_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def owner_function_names(repo_root: Path = REPO_ROOT) -> frozenset[str]:
    """Public top-level function names of the owner module, read from its AST."""

    names = _top_level_function_names(repo_root / OWNER_RELATIVE_PATH)
    return frozenset(name for name in names if not name.startswith("_"))


OWNER_READERS: frozenset[str] = owner_function_names()


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _bound_names(target: ast.AST) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _bound_names(element)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


def _is_int_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)


def _is_marker_text(value: str) -> bool:
    return bool(_LEADING_MARKER.match(value) or _PLACEHOLDER_MARKER.search(value))


def _own_scope_nodes(scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> Iterator[ast.AST]:
    """Breadth-first nodes of ``scope``'s body, not entering nested ``def`` / ``lambda`` bodies."""

    queue: deque[ast.AST] = deque(scope.body if isinstance(scope.body, list) else [scope.body])
    while queue:
        node = queue.popleft()
        yield node
        if not isinstance(node, _SCOPE_NODES):
            queue.extend(ast.iter_child_nodes(node))


class _RetryAccountingScan(ast.NodeVisitor):
    def __init__(self, owner_readers: frozenset[str]) -> None:
        self.owner_readers = owner_readers
        self.hits: set[tuple[int, str]] = set()
        self.alias_scopes: list[set[str]] = []
        self.taint_scopes: list[set[str]] = []
        self.regex_fstrings: set[ast.JoinedStr] = set()

    # -- markers -------------------------------------------------------------

    def is_marker(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _is_marker_text(node.value)
        if isinstance(node, ast.Name):
            return node.id == MARKER_NAME or any(node.id in aliases for aliases in self.alias_scopes)
        if isinstance(node, ast.Attribute):
            return node.attr == MARKER_NAME
        return False

    def _collect_marker_aliases(self, scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        aliases = self.alias_scopes[-1]
        for node in _own_scope_nodes(scope):
            if isinstance(node, ast.ImportFrom):
                aliases.update(alias.asname for alias in node.names if alias.name == MARKER_NAME and alias.asname)
            elif (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(node.value, (ast.Constant, ast.Name, ast.Attribute))
                and self.is_marker(node.value)
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                aliases.update(t.id for t in targets if isinstance(t, ast.Name))

    # -- readers and taint ---------------------------------------------------

    def is_reader(self, name: str | None) -> bool:
        return name is not None and (name in self.owner_readers or "attempt" in name)

    def is_marker_split(self, node: ast.Call) -> bool:
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in _SPLITTERS):
            return False
        return (bool(node.args) and self.is_marker(node.args[0])) or any(
            keyword.arg == "sep" and self.is_marker(keyword.value) for keyword in node.keywords
        )

    def derives_from_reader(self, node: ast.AST) -> bool:
        taint = self.taint_scopes[-1]
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and (self.is_reader(_callee_name(child)) or self.is_marker_split(child)):
                return True
            if isinstance(child, ast.Name) and child.id in taint:
                return True
        return False

    def _bind(self, targets: Iterable[ast.AST], value: ast.AST | None) -> None:
        if value is not None and self.derives_from_reader(value):
            for target in targets:
                self.taint_scopes[-1].update(_bound_names(target))

    def _scoped(self, node: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        self.taint_scopes.append(set())
        self.alias_scopes.append(set())
        self._collect_marker_aliases(node)
        self.generic_visit(node)
        self.alias_scopes.pop()
        self.taint_scopes.pop()

    visit_Module = visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _scoped

    def visit_Assign(self, node: ast.Assign) -> None:
        self._bind(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind([node.target], node.value)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind([node.target], node.value)
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        call = node.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in _CONTAINER_ADDERS
            and isinstance(call.func.value, ast.Name)
            and any(self.derives_from_reader(arg) for arg in call.args)
        ):
            self.taint_scopes[-1].add(call.func.value.id)
        self.generic_visit(node)

    # -- violation kinds -----------------------------------------------------

    def _hit(self, node: ast.expr | ast.stmt, kind: str) -> None:
        self.hits.add((node.lineno, kind))

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        if node not in self.regex_fstrings:
            for part in node.values:
                if (
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and _MARKER_CONSTANT.fullmatch(part.value)
                ):
                    self._hit(node, "id-fstring")
                elif isinstance(part, ast.FormattedValue) and self.is_marker(part.value):
                    self._hit(node, "id-fstring")
        self.generic_visit(node)

    def _builds_id(self, node: ast.BinOp) -> bool:
        if not isinstance(node.op, (ast.Add, ast.Mod)):
            return False
        if self.is_marker(node.left) or self.is_marker(node.right):
            return True
        if not isinstance(node.op, ast.Mod):
            return False
        right = node.right
        elements = right.elts if isinstance(right, ast.Tuple) else right.values if isinstance(right, ast.Dict) else []
        return any(self.is_marker(element) for element in elements)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if self._builds_id(node):
            self._hit(node, "id-concat")
        elif isinstance(node.op, ast.Add) and any(
            _is_int_literal(literal) and self.derives_from_reader(operand)
            for literal, operand in ((node.left, node.right), (node.right, node.left))
        ):
            self._hit(node, "parser+int")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.op, ast.Add):
            if self.is_marker(node.value):
                self._hit(node, "id-augassign")
            elif (
                _is_int_literal(node.value)
                and isinstance(node.target, ast.Name)
                and node.target.id in self.taint_scopes[-1]
            ):
                self._hit(node, "parser+=int")
        self.generic_visit(node)

    @staticmethod
    def _format_operands(node: ast.Call, func: ast.Attribute) -> Iterator[ast.AST]:
        yield func.value
        yield from node.args
        for keyword in node.keywords:
            yield keyword.value
            if keyword.arg is None and isinstance(keyword.value, ast.Dict):
                yield from keyword.value.values
        if func.attr == "format_map" and node.args and isinstance(node.args[0], ast.Dict):
            yield from node.args[0].values

    @staticmethod
    def _join_parts(node: ast.AST) -> Iterator[ast.AST]:
        """``node`` and what it structurally holds; never a generator, comprehension or call."""

        yield node
        if isinstance(node, ast.BinOp):
            children: list[ast.AST] = [node.left, node.right]
        elif isinstance(node, ast.IfExp):
            children = [node.body, node.orelse]
        elif isinstance(node, ast.Starred):
            children = [node.value]
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            children = list(node.elts)
        else:
            children = []
        for child in children:
            yield from _RetryAccountingScan._join_parts(child)

    @staticmethod
    def _join_elements(node: ast.Call, func: ast.Attribute) -> Iterator[ast.AST]:
        yield func.value
        for arg in node.args:
            yield from _RetryAccountingScan._join_parts(arg)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in _REGEX_FUNCTIONS and isinstance(func.value, ast.Name) and func.value.id == "re":
                patterns = [*node.args[:1], *(keyword.value for keyword in node.keywords if keyword.arg == "pattern")]
                self.regex_fstrings.update(arg for arg in patterns if isinstance(arg, ast.JoinedStr))
            if func.attr in ("format", "format_map") and any(
                self.is_marker(operand) for operand in self._format_operands(node, func)
            ):
                self._hit(node, "id-format")
            elif func.attr == "join" and any(self.is_marker(element) for element in self._join_elements(node, func)):
                self._hit(node, "id-join")
            elif self.is_marker_split(node):
                self._hit(node, "marker-split")
        self.generic_visit(node)


def find_hits(source: str, owner_readers: frozenset[str] | None = None) -> list[tuple[int, str]]:
    """Return the sorted ``(lineno, kind)`` violations in ``source``.

    ``owner_readers`` defaults to the AST-derived ``OWNER_READERS``.
    """

    scan = _RetryAccountingScan(OWNER_READERS if owner_readers is None else owner_readers)
    scan.visit(ast.parse(source))
    return sorted(scan.hits)


def _iter_python_files(repo_root: Path) -> Iterator[Path]:
    for root in _SCAN_ROOTS:
        for path in sorted((repo_root / root).rglob("*.py")):
            if not _EXCLUDED_DIR_PARTS.intersection(path.relative_to(repo_root).parts):
                yield path


def iter_scanned_sources(repo_root: Path = REPO_ROOT) -> list[Path]:
    return [
        path for path in _iter_python_files(repo_root) if path.relative_to(repo_root).as_posix() != OWNER_RELATIVE_PATH
    ]


def scan_repository(repo_root: Path = REPO_ROOT) -> tuple[list[str], int]:
    """Return the ``path:lineno kind`` violations and the number of scanned files."""

    violations: list[str] = []
    paths = iter_scanned_sources(repo_root)
    for path in paths:
        relative = path.relative_to(repo_root).as_posix()
        tree_source = path.read_text(encoding="utf-8")
        try:
            hits = find_hits(tree_source)
        except SyntaxError as exc:
            raise SyntaxError(f"{relative}: {exc}") from exc
        violations.extend(f"{relative}:{lineno} {kind}" for lineno, kind in hits)
    return violations, len(paths)


# -- repo scan ---------------------------------------------------------------


def test_no_production_module_builds_retry_identity_outside_the_owner() -> None:
    for root in _SCAN_ROOTS:
        assert (REPO_ROOT / root).is_dir(), f"scan root {root}/ is missing"
    violations, scanned = scan_repository()
    assert scanned >= 400, f"only {scanned} files scanned; the walker collapsed"
    assert violations == [], (
        "retry identity is owned by " + OWNER_RELATIVE_PATH + "; delegate to it instead of building "
        "ids, splitting on the marker or adding to a reader result:\n" + "\n".join(violations)
    )


# -- bite rows ---------------------------------------------------------------

_MUST_FLAG: list[tuple[str, str, list[tuple[int, str]]]] = [
    ("fstring-literal", 'job_id = f"{base}_retry_{n}"', [(1, "id-fstring")]),
    ("fstring-marker", 'job_id = f"{base}{RETRY_JOB_ID_MARKER}{n}"', [(1, "id-fstring")]),
    ("fstring-leading-part", 'suffix = f"_retry_{n}"', [(1, "id-fstring")]),
    ("concat-literal", 'job_id = base + "_retry_" + str(n)', [(1, "id-concat")]),
    ("concat-marker", "job_id = base + RETRY_JOB_ID_MARKER + str(n)", [(1, "id-concat")]),
    ("concat-qualified-marker", "job_id = base + retry_identity.RETRY_JOB_ID_MARKER + str(n)", [(1, "id-concat")]),
    ("percent", 'job_id = "%s_retry_%d" % (base, n)', [(1, "id-concat")]),
    ("format", 'job_id = "{}_retry_{}".format(base, n)', [(1, "id-format")]),
    ("join-marker-receiver", 'job_id = "_retry_".join([base, str(n)])', [(1, "id-join")]),
    ("join-marker-argument", 'job_id = "".join([base, RETRY_JOB_ID_MARKER, str(n)])', [(1, "id-join")]),
    ("local-alias", 'SEP = "_retry_"\njob_id = base + SEP + str(n)', [(2, "id-concat")]),
    (
        "import-as-alias",
        'from services.orchestrator.retry_identity import RETRY_JOB_ID_MARKER as M\njob_id = f"{base}{M}{n}"',
        [(2, "id-fstring")],
    ),
    ("augassign-literal", 'job_id = base\njob_id += "_retry_"', [(2, "id-augassign")]),
    ("rsplit-literal", 'base, attempt = job_id.rsplit("_retry_", 1)', [(1, "marker-split")]),
    ("rsplit-marker-maxsplit", "attempt = job_id.rsplit(RETRY_JOB_ID_MARKER, maxsplit=1)[1]", [(1, "marker-split")]),
    ("partition-marker", "base, _, attempt = job_id.partition(RETRY_JOB_ID_MARKER)", [(1, "marker-split")]),
    ("split-literal", 'parts = job_id.split("_retry_")', [(1, "marker-split")]),
    ("plus1-owner-reader", "n = effective_retry_attempt(j, c) + 1", [(1, "parser+int")]),
    ("plus1-attribute-call", "n = ri.retry_suffix_attempt(j) + 1", [(1, "parser+int")]),
    ("plus1-reversed-order", "n = 1 + _state_retry_attempt(state)", [(1, "parser+int")]),
    ("plus1-over-max", "n = max(c, retry_suffix_attempt(j), f - 1) + 1", [(1, "parser+int")]),
    ("plus1-owner-tuple-item", "n = split_retry_job_identity(j)[1] + 1", [(1, "parser+int")]),
    (
        "plus1-tainted-local",
        "def f(j):\n    base, attempt = split_retry_job_identity(j)\n    return attempt + 1",
        [(3, "parser+int")],
    ),
    (
        "plus-equals-tainted-local",
        "def f(j):\n    a = effective_retry_attempt(j)\n    a += 1\n    return a",
        [(3, "parser+=int")],
    ),
    ("plus1-fallback-previous-attempt", "n = _fallback_previous_attempt(state, previous) + 1", [(1, "parser+int")]),
    ("plus1-over-marker-split", 'n = int(j.rsplit("_retry_", 1)[1]) + 1', [(1, "marker-split"), (1, "parser+int")]),
    # No ``attempt`` in the name: only the AST-derived owner set catches it.
    ("plus1-owner-name-without-attempt", "n = next_after_failure_master(j, c)[1] + 1", [(1, "parser+int")]),
    ("plus1-next-effective-attempt", "n = next_effective_attempt(j, c) + 1", [(1, "parser+int")]),
    (
        "plus1-container-taint",
        "def f(jobs):\n"
        "    attempts = []\n"
        "    for j in jobs:\n"
        "        attempts.append(retry_suffix_attempt(j))\n"
        "    return max(attempts, default=0) + 1",
        [(5, "parser+int")],
    ),
    ("plus1-walrus", "n = (a := effective_retry_attempt(j)) + 1", [(1, "parser+int")]),
    ("plus1-walrus-taint", "if (a := effective_retry_attempt(j)) > 0:\n    n = a + 1", [(2, "parser+int")]),
    ("plus1-private-reader-family", "n = _state_job_retry_attempt(s) + 1", [(1, "parser+int")]),
    # Leading-marker templates.
    ("percent-leading-template", 'suffix = "_retry_%d" % n', [(1, "id-concat")]),
    ("format-leading-template", 'suffix = "_retry_{}".format(n)', [(1, "id-format")]),
    ("format-leading-keyed", 'suffix = "_retry_{n}".format(n=n)', [(1, "id-format")]),
    ("concat-percent-leading", 'job_id = base + "_retry_%d" % n', [(1, "id-concat")]),
    ("augassign-percent-leading", 'job_id = base\njob_id += "_retry_%d" % n', [(2, "id-concat")]),
    # Width, keyed and positional placeholders.
    ("percent-width", 'job_id = "%5s_retry_%03d" % (base, n)', [(1, "id-concat")]),
    ("percent-keyed", 'job_id = "%(b)s_retry_%(n)d" % {"b": base, "n": n}', [(1, "id-concat")]),
    ("format-positional", 'job_id = "{0}_retry_{1}".format(base, n)', [(1, "id-format")]),
    # The marker as a direct ``%`` / ``.format`` operand.
    ("percent-marker-element", 'job_id = "%s%s%d" % (base, RETRY_JOB_ID_MARKER, n)', [(1, "id-concat")]),
    ("percent-literal-element", 'job_id = "%s%s%d" % (base, "_retry_", n)', [(1, "id-concat")]),
    (
        "percent-dict-marker-value",
        'job_id = "%(b)s%(m)s%(n)d" % {"b": base, "m": RETRY_JOB_ID_MARKER, "n": n}',
        [(1, "id-concat")],
    ),
    ("format-marker-argument", 'job_id = "{}{}{}".format(base, RETRY_JOB_ID_MARKER, n)', [(1, "id-format")]),
    ("format-marker-keyword", 'job_id = "{}{sep}{}".format(base, n, sep=RETRY_JOB_ID_MARKER)', [(1, "id-format")]),
    ("format-map-template", 'job_id = "{b}_retry_{n}".format_map(d)', [(1, "id-format")]),
    (
        "format-map-marker-value",
        'job_id = "{b}{m}{n}".format_map({"b": base, "m": RETRY_JOB_ID_MARKER, "n": n})',
        [(1, "id-format")],
    ),
    # Splitters, including the ``sep=`` keyword.
    ("rsplit-sep-keyword", "base, attempt = j.rsplit(sep=RETRY_JOB_ID_MARKER, maxsplit=1)", [(1, "marker-split")]),
    (
        "plus1-over-sep-keyword-split",
        "n = int(j.rsplit(sep=RETRY_JOB_ID_MARKER, maxsplit=1)[1]) + 1",
        [(1, "marker-split"), (1, "parser+int")],
    ),
    ("rpartition-literal", 'base, _, attempt = job_id.rpartition("_retry_")', [(1, "marker-split")]),
    ("removesuffix-marker", "stem = job_id.removesuffix(RETRY_JOB_ID_MARKER)", [(1, "marker-split")]),
    ("rstrip-literal", 'stem = job_id.rstrip("_retry_")', [(1, "marker-split")]),
    # Aliases and their scopes.
    ("module-template-alias", 'TPL = "{}_retry_{}"\n\n\ndef f(b, n):\n    return TPL.format(b, n)', [(5, "id-format")]),
    ("local-template-alias", 'def f(b, n):\n    tpl = "%s_retry_%d"\n    return tpl % (b, n)', [(3, "id-concat")]),
    (
        "module-alias-in-function",
        "SEP = RETRY_JOB_ID_MARKER\n\n\ndef f(b, n):\n    return b + SEP + str(n)",
        [(5, "id-concat")],
    ),
    (
        "local-alias-in-own-function",
        'def f(b, n):\n    sep = "_retry_"\n    return b + sep + str(n)',
        [(3, "id-concat")],
    ),
    (
        "local-alias-in-nested-function",
        'def f(b, n):\n    sep = "_retry_"\n\n    def g():\n        return b + sep + str(n)\n\n    return g',
        [(5, "id-concat")],
    ),
    (
        "class-body-alias-reaches-enclosing-scope",
        'class C:\n    SEP = "_retry_"\n\n\ndef f(b, n):\n    return b + SEP + str(n)',
        [(6, "id-concat")],
    ),
    ("chained-alias", 'A = "_retry_"\nB = A\njob_id = base + B + str(n)', [(3, "id-concat")]),
    # ``join`` over a Tuple literal, and the narrowness of the regex exemption.
    ("join-tuple-literal", 'job_id = "".join((base, "_retry_", str(n)))', [(1, "id-join")]),
    ("join-list-concat", 'job_id = "".join([b, RETRY_JOB_ID_MARKER] + [str(n)])', [(1, "id-join")]),
    ("join-ifexp", 'job_id = "".join([b, RETRY_JOB_ID_MARKER, str(n)] if x else [])', [(1, "id-join")]),
    ("join-starred", 'job_id = "".join([*[b, RETRY_JOB_ID_MARKER], str(n)])', [(1, "id-join")]),
    # ``%`` with a non-literal template, and ``.format(**{...})``.
    ("percent-name-template", 'FMT = "%s%s%d"\nj = FMT % (b, RETRY_JOB_ID_MARKER, n)', [(2, "id-concat")]),
    (
        "percent-parameter-template",
        "def f(fmt, b, n):\n    return fmt % (b, RETRY_JOB_ID_MARKER, n)",
        [(2, "id-concat")],
    ),
    ("percent-attribute-template", "j = self.fmt % (b, RETRY_JOB_ID_MARKER, n)", [(1, "id-concat")]),
    ("percent-name-template-dict", 'j = FMT % {"m": RETRY_JOB_ID_MARKER}', [(1, "id-concat")]),
    (
        "format-double-star-dict",
        'j = "{b}{m}{n}".format(**{"b": b, "m": RETRY_JOB_ID_MARKER, "n": n})',
        [(1, "id-format")],
    ),
    ("annotated-alias", 'SEP: str = "_retry_"\njob_id = base + SEP + str(n)', [(2, "id-concat")]),
    ("fstring-into-non-re-match", 'ok = pattern.match(f"{base}_retry_{n}")', [(1, "id-fstring")]),
    ("fstring-into-re-sub", 'import re\ns = re.sub("x", "y", f"{b}_retry_{n}")', [(2, "id-fstring")]),
    ("fstring-into-re-subject", 'import re\nok = re.fullmatch(PATTERN, f"{base}_retry_{n}")', [(2, "id-fstring")]),
    ("fstring-into-re-string-keyword", 'import re\nm = re.match(P, string=f"{base}_retry_{n}")', [(2, "id-fstring")]),
    (
        "regex-fstring-still-scans-inside",
        'import re\np = re.compile(f"{base + RETRY_JOB_ID_MARKER}")',
        [(2, "id-concat")],
    ),
    # Taint: other adders, annotated assignment, ``async def`` and ``lambda`` scopes.
    ("plus1-insert-taint", "xs = []\nxs.insert(0, retry_suffix_attempt(j))\nn = max(xs) + 1", [(3, "parser+int")]),
    (
        "plus1-appendleft-taint",
        "q = deque()\nq.appendleft(retry_suffix_attempt(j))\nn = q[0] + 1",
        [(3, "parser+int")],
    ),
    ("plus1-set-add-taint", "s = set()\ns.add(retry_suffix_attempt(j))\nn = max(s) + 1", [(3, "parser+int")]),
    (
        "plus1-extend-taint",
        "xs = []\nxs.extend([retry_suffix_attempt(j) for j in js])\nn = max(xs) + 1",
        [(3, "parser+int")],
    ),
    ("plus1-annassign-taint", "a: int = effective_retry_attempt(j, c)\nn = a + 1", [(2, "parser+int")]),
    (
        "plus1-async-def-scope",
        "async def f(j):\n    a = effective_retry_attempt(j)\n    return a + 1",
        [(3, "parser+int")],
    ),
    ("plus1-lambda-scope", "f = lambda j: (a := effective_retry_attempt(j)) and a + 1", [(1, "parser+int")]),
    # Documented fail-safe over-flag: the reader may sit anywhere in the operand.
    ("plus1-len-of-reader-overflag", "n = len(cohort_retry_attempt_floors(b, s)) + 1", [(1, "parser+int")]),
]

_MUST_NOT_FLAG: list[tuple[str, str]] = [
    ("manual-retry-key", 'value = state.get(f"manual_retry_{key}")'),
    ("membership", 'ok = "_retry_" in job_id'),
    ("regex-compile", 'import re\nPATTERN = re.compile(r"_retry_\\d+$")'),
    ("counters", 'seq = events[-1]["sequence"] + 1\nidx += 1\nrecovered[s] = recovered.get(s, 0) + 1'),
    ("reader-into-max", "n = effective_retry_attempt(j, c)\nm = max(n, 3)"),
    ("reader-compared", "ok = retry_suffix_attempt(j) > 0"),
    ("reader-minus-1", "n = effective_retry_attempt(j) - 1"),
    ("owner-composition", "jid = retry_job_id(base, next_effective_attempt(j, c))"),
    ("other-split", 'a, b = s.split(":")'),
    ("text-with-retry", 'msg = f"decision retry_{x} for manual_retry_{y}"'),
    ("constant-code", 'CODE = "retry_strict_warm_start_x"'),
    ("name-called-attempt", "attempt = 2\nattempt += 1\nn = attempt + 1"),
    (
        "taint-across-functions",
        "def f(j):\n    a = effective_retry_attempt(j)\n    return a\n\n\ndef g(a):\n    return a + 1",
    ),
    ("attribute-target", "def f(self, j):\n    self.a = effective_retry_attempt(j)\n    return self.count + 1"),
    ("field-name-fstring", 'key = f"{stage}_retry_count"'),
    ("field-name-concat", 'key = prefix + "_retry_attempt_floor"'),
    ("field-name-format", 'key = "{}_retry_limit".format(x)'),
    ("field-name-removesuffix", 'stem = k.removesuffix("_retry_count")'),
    ("field-name-bare", 'FIELD = "_retry_count"\nkey = stage + FIELD'),
    ("field-name-percent", 'key = "%s_retry_count" % stage'),
    ("field-name-augassign", 'key = stage\nkey += "_retry_count"'),
    ("join-generator", 's = ", ".join(str(x) for x in xs)'),
    ("join-ifexp-field-names", 's = ", ".join([a, "_retry_count"] if x else [b])'),
    ("percent-name-template-field", 'key = FMT % (stage, "_retry_count")'),
    ("join-membership-filter", 's = ", ".join(j for j in ids if RETRY_JOB_ID_MARKER in j)'),
    (
        "local-alias-stays-in-its-function",
        'def f(b):\n    sep = "_retry_"\n    return sep in b\n\n\ndef g(parts, sep):\n    return sep.join(parts) + sep',
    ),
    ("regex-fstring-compile", 'import re\nPATTERN = re.compile(f"{re.escape(base)}_retry_")'),
    ("regex-fstring-match", 'import re\nm = re.match(f"^{re.escape(base)}_retry_{n}$", j)'),
    ("regex-fstring-match-pattern-position", 'import re\nm = re.match(f"{re.escape(base)}_retry_", j)'),
    ("regex-fstring-pattern-keyword", 'import re\nP = re.compile(pattern=f"{re.escape(base)}_retry_")'),
    (
        "async-def-taint-stays-in-scope",
        "async def f(j):\n    a = effective_retry_attempt(j)\n    return a\n\n\nn = a + 1",
    ),
    ("lambda-taint-stays-in-scope", "f = lambda j: (a := effective_retry_attempt(j))\nn = a + 1"),
    # Documented misses, one row each.
    ("miss-subscript-store-taint", "by[j] = retry_suffix_attempt(j)\nn = max(by.values()) + 1"),
    ("miss-operator-add", "import operator\nn = operator.add(effective_retry_attempt(j), 1)"),
    ("miss-rfind-slice", 'n = int(j[j.rfind("_retry_") + 7:]) + 1'),
    ("miss-join-generator", 'job_id = "".join(str(p) for p in (base, RETRY_JOB_ID_MARKER, n))'),
    ("miss-join-map", 'job_id = "".join(map(str, [base, RETRY_JOB_ID_MARKER, n]))'),
    ("miss-index-slice", "n = int(j[j.index(RETRY_JOB_ID_MARKER) + len(RETRY_JOB_ID_MARKER) :]) + 1"),
    ("miss-marker-mid-constant", 'job_id = f"{a}x_retry_{n}"'),
    ("miss-row-data-counter", 'retry_count = int(source.get("retry_count") or 0) + 1'),
    ("miss-regex-group", 'import re\nn = int(re.match(r"(.*)_retry_(\\d+)$", j).group(2)) + 1'),
    ("miss-late-binding-getattr", 'n = getattr(_chain, "_retry_attempt_from_basins")(basins) + 1'),
    ("miss-for-target", "for a in [effective_retry_attempt(j) for j in jobs]:\n    n = a + 1"),
    ("miss-comprehension-target", "ns = [a + 1 for a in [effective_retry_attempt(j) for j in jobs]]"),
    (
        "miss-for-target-enumerate",
        "def f(b, s):\n    for i, fl in enumerate(cohort_retry_attempt_floors(b, s)):\n        n = i + 1",
    ),
    (
        "miss-closure",
        "def f(j):\n    a = effective_retry_attempt(j)\n\n    def g():\n        return a + 1\n\n    return g",
    ),
    ("miss-sum-start", "n = sum([effective_retry_attempt(j, c)], 1)"),
]


@pytest.mark.parametrize(("source", "expected"), [(s, e) for _, s, e in _MUST_FLAG], ids=[i for i, _, _ in _MUST_FLAG])
def test_bite_row_is_flagged(source: str, expected: list[tuple[int, str]]) -> None:
    assert find_hits(source) == expected


@pytest.mark.parametrize("source", [s for _, s in _MUST_NOT_FLAG], ids=[i for i, _ in _MUST_NOT_FLAG])
def test_near_miss_row_is_not_flagged(source: str) -> None:
    assert find_hits(source) == []


# -- meta-tests --------------------------------------------------------------


def test_walker_excludes_exactly_the_owner_module() -> None:
    assert (REPO_ROOT / OWNER_RELATIVE_PATH).is_file()
    every_file = {path.relative_to(REPO_ROOT).as_posix() for path in _iter_python_files(REPO_ROOT)}
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in iter_scanned_sources()}
    assert scanned <= every_file
    assert every_file - scanned == {OWNER_RELATIVE_PATH}


def test_reader_set_is_derived_from_the_owner_and_covers_the_scheduler_readers() -> None:
    assert OWNER_READERS, "the AST-derived owner reader set is empty"
    assert "effective_retry_attempt" in OWNER_READERS
    for name, module in _SCHEDULER_READERS.items():
        assert name in _top_level_function_names(REPO_ROOT / module), f"{name} is no longer defined in {module}"
        assert find_hits(f"n = {name}(x) + 1", owner_readers=frozenset()) == [(1, "parser+int")], (
            f"{name} is not matched by the attempt name rule"
        )
