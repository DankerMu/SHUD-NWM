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

* a str constant that fully matches ``_retry_\\d*``;
* a str constant containing ``(%[sdri]|\\})_retry_(%|\\{|\\d|$)`` (the ``%`` /
  ``.format`` placeholder forms);
* the name ``RETRY_JOB_ID_MARKER``, or an attribute with that name;
* a module-local alias, collected in a pre-pass from ``X = "_retry_"``,
  ``X = RETRY_JOB_ID_MARKER`` and ``from ... import RETRY_JOB_ID_MARKER as M``.

Field names such as ``f"{stage}_retry_count"``, ``prefix +
"_retry_attempt_floor"``, ``"{}_retry_limit".format(x)`` and
``k.removesuffix("_retry_count")`` are therefore not markers, and neither is
unrelated text such as ``f"manual_retry_{key}"``.  ``"_retry_" in job_id`` and
``re.compile(r"_retry_\\d+$")`` are parse / membership uses and never flag.

Violation kinds (the failure message is ``path:lineno kind``):

* ``id-fstring``: an f-string with a marker ``FormattedValue``, or with a
  constant part that is a marker (``_retry_`` followed by a ``FormattedValue``,
  or a full ``_retry_\\d*`` part);
* ``id-concat``: ``BinOp`` ``Add`` / ``Mod`` with a marker operand;
* ``id-format``: ``.format`` / ``.format_map`` on a marker-bearing constant;
* ``id-join``: ``join`` with the marker as receiver or anywhere in its args;
* ``id-augassign``: ``x += <marker>``;
* ``marker-split``: ``rsplit`` / ``split`` / ``partition`` / ``rpartition`` /
  ``removesuffix`` / ``rstrip`` with a marker first argument;
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
call to a reader, a ``marker-split`` call, or a tainted name.  Taint is per
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

Known, documented misses.  These are the only accepted gaps; each has a
not-flagged bite row, so tightening one later is a deliberate edit:

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
  addition itself (``sum([reader(...)], 1)``: no ``BinOp``).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

OWNER_RELATIVE_PATH = "services/orchestrator/retry_identity.py"

_SCAN_ROOTS: tuple[str, ...] = ("apps", "db", "packages", "scripts", "services", "workers")

_EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
)

MARKER_LITERAL = "_retry_"
MARKER_NAME = "RETRY_JOB_ID_MARKER"

_MARKER_CONSTANT = re.compile(r"_retry_\d*")
_PLACEHOLDER_MARKER = re.compile(r"(%[sdri]|\})_retry_(%|\{|\d|$)")

_SPLITTERS: frozenset[str] = frozenset({"rsplit", "split", "partition", "rpartition", "removesuffix", "rstrip"})
_CONTAINER_ADDERS: frozenset[str] = frozenset({"append", "extend", "add", "insert", "appendleft"})

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
    return bool(_MARKER_CONSTANT.fullmatch(value) or _PLACEHOLDER_MARKER.search(value))


class _RetryAccountingScan(ast.NodeVisitor):
    def __init__(self, owner_readers: frozenset[str]) -> None:
        self.owner_readers = owner_readers
        self.hits: set[tuple[int, str]] = set()
        self.marker_names: set[str] = {MARKER_NAME}
        self.taint_scopes: list[set[str]] = [set()]

    # -- markers -------------------------------------------------------------

    def is_marker(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _is_marker_text(node.value)
        if isinstance(node, ast.Name):
            return node.id in self.marker_names
        if isinstance(node, ast.Attribute):
            return node.attr == MARKER_NAME
        return False

    def contains_marker(self, node: ast.AST) -> bool:
        return any(self.is_marker(child) for child in ast.walk(node))

    def _collect_marker_aliases(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.marker_names.update(
                    alias.asname for alias in node.names if alias.name == MARKER_NAME and alias.asname
                )
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                value = node.value
                aliases_marker = (isinstance(value, ast.Constant) and value.value == MARKER_LITERAL) or (
                    isinstance(value, (ast.Name, ast.Attribute)) and self.is_marker(value)
                )
                if aliases_marker:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    self.marker_names.update(t.id for t in targets if isinstance(t, ast.Name))

    # -- readers and taint ---------------------------------------------------

    def is_reader(self, name: str | None) -> bool:
        return name is not None and (name in self.owner_readers or "attempt" in name)

    def is_marker_split(self, node: ast.Call) -> bool:
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in _SPLITTERS
            and bool(node.args)
            and self.is_marker(node.args[0])
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

    def _scoped(self, node: ast.AST) -> None:
        self.taint_scopes.append(set())
        self.generic_visit(node)
        self.taint_scopes.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _scoped

    def visit_Module(self, node: ast.Module) -> None:
        self._collect_marker_aliases(node)
        self.generic_visit(node)

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

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, (ast.Add, ast.Mod)) and (self.is_marker(node.left) or self.is_marker(node.right)):
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

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            if (
                func.attr in ("format", "format_map")
                and isinstance(func.value, ast.Constant)
                and isinstance(func.value.value, str)
                and _is_marker_text(func.value.value)
            ):
                self._hit(node, "id-format")
            elif func.attr == "join" and (
                self.is_marker(func.value) or any(self.contains_marker(arg) for arg in node.args)
            ):
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
    # Documented misses, one row each.
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
