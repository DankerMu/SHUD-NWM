"""#2713 wire-site invariant: every compressed-hypertable writer takes the ingest fence first.

Sibling of ``tests/test_timescale_write_guard_wire_site_invariant.py`` and built
the same way (AST scan over the same source roots and the same documented
unwired whitelist). The table set is DERIVED, from
``packages.common.timeseries_compression_fence.COMPRESSED_HYPERTABLES`` -- the
lifecycle set compression and retention operate on -- so a new compressed
hypertable is covered without editing this file.

The rule, per write site (a ``DELETE FROM`` / ``INSERT INTO`` / ``UPDATE`` /
``COPY`` literal naming a compressed hypertable, passed as a call argument):

* its innermost enclosing function is FENCED when, among the database calls in
  that function's subtree taken in source order, the first one is
  ``try_ingest_fence(<cursor>, <that hypertable>)``. A database call is an
  ``execute``-family / repository statement helper call, or any call handed a
  cursor. Nested definitions count, so a ``pre_write_cursor_hook`` that opens
  with the fence fences its writer;
* or every intra-module call site of that function sits in a fenced function,
  after that function's fence call (applied transitively). This is what covers
  a mid-transaction helper such as
  ``forcing_domain_handoff_apply._replace_forcing_station_timeseries``.

Anything else fails and names the function. Synthetic cases pin the predicate
in both directions, so a gutted predicate cannot keep the repository scan green.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from packages.common.timeseries_compression_fence import COMPRESSED_HYPERTABLES
from tests.test_timescale_write_guard_wire_site_invariant import (
    _INTENTIONALLY_UNWIRED_MODULES,
    _iter_call_argument_strings,
    _iter_python_sources,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_FENCE_MODULE = "packages.common.timeseries_compression_fence"
_FENCE_CALL = "try_ingest_fence"
_DB_CALL_NAMES = frozenset(
    {
        "execute",
        "executemany",
        "execute_values",
        "copy_expert",
        "_execute_values",
        "_fetch_all",
        "_fetch_one",
        "_fetch_optional",
        "_replace_values",
        "check_batch_targets_uncompressed",
    }
)
_EXPECTED_FENCED_MODULES: frozenset[Path] = frozenset(
    {
        REPO_ROOT / "workers" / "output_parser" / "parser.py",
        REPO_ROOT / "workers" / "forcing_producer" / "store.py",
        REPO_ROOT / "packages" / "common" / "forcing_domain_handoff_apply.py",
    }
)


def _write_pattern(hypertable: str) -> re.Pattern[str]:
    # The negative lookahead keeps ``hydro.river_timeseries`` from matching the
    # ``_legacy`` sibling, which is a different hypertable (sharing the family
    # fence key) scanned under its own pattern.
    return re.compile(
        rf"\b(?:DELETE\s+FROM|INSERT\s+INTO|UPDATE|COPY)\s+{re.escape(hypertable)}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )


_WRITE_PATTERNS = {hypertable: _write_pattern(hypertable) for hypertable in COMPRESSED_HYPERTABLES}


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _module_strings(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, to resolve the fence's table argument."""
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value.value
    return bindings


def _fence_target(call: ast.Call, bindings: dict[str, str]) -> str | None:
    if len(call.args) < 2:
        return None
    argument = call.args[1]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    if isinstance(argument, ast.Name):
        return bindings.get(argument.id)
    return None


def _is_db_call(call: ast.Call) -> bool:
    if _call_name(call) in _DB_CALL_NAMES:
        return True
    return any(isinstance(arg, ast.Name) and arg.id.endswith("cursor") for arg in call.args)


def _calls_in_source_order(function: ast.AST) -> list[ast.Call]:
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    return sorted(calls, key=lambda call: (call.lineno, call.col_offset))


def _fence_position(function: ast.AST, hypertables: set[str], bindings: dict[str, str]) -> tuple[int, int] | None:
    """Source position of the fence call when it is the subtree's first DB call, else None."""
    for call in _calls_in_source_order(function):
        if not _is_db_call(call) and _call_name(call) != _FENCE_CALL:
            continue
        if _call_name(call) == _FENCE_CALL and _fence_target(call, bindings) in hypertables:
            return (call.lineno, call.col_offset)
        return None
    return None


def _innermost_functions(tree: ast.Module) -> list[tuple[ast.AST, str, set[str]]]:
    """``(function node, name, hypertables written)`` for every write site's innermost function."""
    found: dict[int, tuple[ast.AST, str, set[str]]] = {}

    def descend(node: ast.AST, owner: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            child_owner = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else owner
            if isinstance(child, ast.Call):
                for text in _iter_call_argument_strings(child):
                    for hypertable, pattern in _WRITE_PATTERNS.items():
                        if pattern.search(text):
                            key = id(child_owner)
                            name = child_owner.name if child_owner is not None else "<module level>"
                            entry = found.setdefault(key, (child_owner, name, set()))
                            entry[2].add(hypertable)
            descend(child, child_owner)

    descend(tree, None)
    return list(found.values())


def _unfenced_write_sites(source: str) -> list[str]:
    """Names (with the reason) of write-site functions the fence does not order first."""
    tree = ast.parse(source)
    bindings = _module_strings(tree)
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]

    def fenced(function: ast.AST, hypertables: set[str], seen: frozenset[int]) -> bool:
        if _fence_position(function, hypertables, bindings) is not None:
            return True
        name = getattr(function, "name", None)
        if name is None or id(function) in seen:
            return False
        callers: list[tuple[ast.AST, ast.Call]] = [
            (caller, call)
            for caller in functions
            if caller is not function
            for call in ast.walk(caller)
            if isinstance(call, ast.Call) and _call_name(call) == name
        ]
        if not callers:
            return False
        for caller, call in callers:
            position = _fence_position(caller, hypertables, bindings)
            if position is None:
                if not fenced(caller, hypertables, seen | {id(function)}):
                    return False
                continue
            if (call.lineno, call.col_offset) <= position:
                return False
        return True

    failures: list[str] = []
    for function, name, hypertables in _innermost_functions(tree):
        if function is None or not fenced(function, hypertables, frozenset()):
            failures.append(f"{name} (writes {', '.join(sorted(hypertables))})")
    return failures


def _module_imports_fence(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == _FENCE_MODULE
        and any(alias.name == _FENCE_CALL for alias in node.names)
        for node in ast.walk(tree)
    )


def _write_site_modules() -> dict[Path, list[tuple[str, set[str]]]]:
    modules: dict[Path, list[tuple[str, set[str]]]] = {}
    for path in _iter_python_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        sites = [(name, hypertables) for _function, name, hypertables in _innermost_functions(tree)]
        if sites:
            modules[path] = sites
    return modules


# ---------------------------------------------------------------------------
# Repository scan
# ---------------------------------------------------------------------------


def test_every_compressed_hypertable_writer_takes_the_ingest_fence_first() -> None:
    modules = _write_site_modules()
    assert modules, "no compressed-hypertable write site found: the scan is stale"
    for path in sorted(modules):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        failures = _unfenced_write_sites(path.read_text(encoding="utf-8"))
        assert not failures, (
            f"{path.relative_to(REPO_ROOT)}: {failures!r} write a compressed hypertable without "
            f"`{_FENCE_CALL}(cursor, <hypertable>)` as the first statement of the write "
            "transaction (#2713). Take the fence before any other DB call -- an FK-referenced "
            "table lock or a probe taken first re-creates the compression deadlock."
        )
        assert _module_imports_fence(tree), (
            f"{path.relative_to(REPO_ROOT)} must import {_FENCE_CALL} from {_FENCE_MODULE}"
        )


def test_the_fenced_writer_set_is_exactly_the_audited_three() -> None:
    found = set(_write_site_modules())
    assert found == _EXPECTED_FENCED_MODULES, (
        "compressed-hypertable writer modules changed; audit the new write site and fence it, "
        f"then update this pin. unexpected={sorted(str(p) for p in found - _EXPECTED_FENCED_MODULES)} "
        f"missing={sorted(str(p) for p in _EXPECTED_FENCED_MODULES - found)}"
    )
    sites = [site for sites in _write_site_modules().values() for site in sites]
    assert len(sites) >= 3, sites


def test_the_unwired_whitelist_still_excludes_only_fresh_database_writers() -> None:
    # seed_demo INSERTs into both hypertables of a fresh demo DB that is never
    # compressed; it stays out of the fence scan for the same documented reason
    # it stays out of the compressed-chunk guard scan.
    seed = REPO_ROOT / "db" / "seeds" / "seed_demo.py"
    assert seed in _INTENTIONALLY_UNWIRED_MODULES
    assert any(_innermost_functions(ast.parse(seed.read_text(encoding="utf-8"))))


# ---------------------------------------------------------------------------
# Synthetic cases: the predicate bites in both directions
# ---------------------------------------------------------------------------

_HEADER = '''
from packages.common.timeseries_compression_fence import try_ingest_fence
RIVER = "hydro.river_timeseries"
'''

_PREDICATE_CASES: tuple[tuple[str, str, list[str]], ...] = (
    (
        "fence-first",
        _HEADER
        + '''
def write(connection):
    with connection.cursor() as cursor:
        if not try_ingest_fence(cursor, RIVER):
            raise RuntimeError
        cursor.execute("SELECT 1 FROM hydro.hydro_run FOR UPDATE")
        cursor.execute("DELETE FROM hydro.river_timeseries WHERE run_key = %s", (1,))
''',
        [],
    ),
    (
        "no-fence",
        _HEADER
        + '''
def write(cursor):
    cursor.execute("INSERT INTO hydro.river_timeseries VALUES (1)")
''',
        ["write (writes hydro.river_timeseries)"],
    ),
    (
        "fence-after-fk-lock",
        _HEADER
        + '''
def write(cursor):
    cursor.execute("SELECT 1 FROM hydro.hydro_run FOR UPDATE")
    try_ingest_fence(cursor, RIVER)
    cursor.execute("DELETE FROM hydro.river_timeseries WHERE run_key = 1")
''',
        ["write (writes hydro.river_timeseries)"],
    ),
    (
        "fence-for-another-hypertable",
        _HEADER
        + '''
def write(cursor):
    try_ingest_fence(cursor, "met.forcing_station_timeseries")
    cursor.execute("DELETE FROM hydro.river_timeseries WHERE run_key = 1")
''',
        ["write (writes hydro.river_timeseries)"],
    ),
    (
        "legacy-sibling-needs-its-own-fence",
        _HEADER
        + '''
def write(cursor):
    try_ingest_fence(cursor, RIVER)
    cursor.execute("DELETE FROM hydro.river_timeseries_legacy WHERE run_id = 'x'")
''',
        ["write (writes hydro.river_timeseries_legacy)"],
    ),
    (
        "helper-reached-only-after-the-callers-fence",
        _HEADER
        + '''
def _replace(cursor):
    cursor.execute("SELECT key FROM ref")
    cursor.execute("DELETE FROM hydro.river_timeseries WHERE k = 1")

def apply(cursor):
    try_ingest_fence(cursor, RIVER)
    _refuse(cursor)
    _replace(cursor)
''',
        [],
    ),
    (
        "helper-called-before-the-callers-fence",
        _HEADER
        + '''
def _replace(cursor):
    cursor.execute("DELETE FROM hydro.river_timeseries WHERE k = 1")

def apply(cursor):
    _replace(cursor)
    try_ingest_fence(cursor, RIVER)
''',
        ["_replace (writes hydro.river_timeseries)"],
    ),
    (
        "helper-with-one-unfenced-caller",
        _HEADER
        + '''
def _replace(cursor):
    cursor.execute("DELETE FROM hydro.river_timeseries WHERE k = 1")

def fenced(cursor):
    try_ingest_fence(cursor, RIVER)
    _replace(cursor)

def unfenced(cursor):
    _replace(cursor)
''',
        ["_replace (writes hydro.river_timeseries)"],
    ),
    (
        "hook-opening-with-the-fence",
        _HEADER
        + '''
def replace(self, rows):
    def _guard(cursor):
        if not try_ingest_fence(cursor, RIVER):
            raise RuntimeError
        cursor.execute("SELECT key FROM ref")
    self._replace_values(None, (), "DELETE FROM hydro.river_timeseries WHERE k = %s", (), "INSERT INTO x", rows,
                         pre_write_cursor_hook=_guard)
''',
        [],
    ),
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [(source, expected) for _case, source, expected in _PREDICATE_CASES],
    ids=[case for case, _source, _expected in _PREDICATE_CASES],
)
def test_the_fence_first_predicate_on_synthetic_writers(source: str, expected: list[str]) -> None:
    assert _unfenced_write_sites(source) == expected
