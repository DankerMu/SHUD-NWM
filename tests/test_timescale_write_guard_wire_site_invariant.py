"""Wire-site invariant tests for the compressed-chunk write guard.

Mirrors #850 cand-E / #851 R3: a shape-first invariant that greps for every
production ``DELETE FROM {schema}.{table}`` targeting a guarded hypertable
and asserts the surrounding module ALSO calls
``check_batch_targets_uncompressed``. This closes the ordering-contract gap
that per-path unit tests alone cannot cover: a future contributor who adds
a fourth write path but forgets the guard call would slip past the wired
tests (which only cover the three known paths) — this AST-scan catches the
new site by structure, not by manual audit.

R2 hardening:

* **J1** — Per-DELETE-site enforcement (was module-level): for each
  ``ast.Call`` whose SQL literal argument starts
  ``DELETE FROM {schema}.{table}``, walk up to the enclosing
  ``FunctionDef``/``AsyncFunctionDef`` and assert a call to
  ``check_batch_targets_uncompressed`` exists inside that function's
  body — direct OR nested (a ``pre_write_cursor_hook=_guard`` where
  ``_guard`` is a locally-defined function containing the guard call
  still matches because ``ast.walk`` on the FunctionDef visits nested
  definitions).
* **J2** — Explicit AST check on
  ``workers/forcing_producer/store.py::replace_forcing_timeseries``:
  the ``pre_write_cursor_hook=`` keyword must be bound to the
  local ``_guard`` Name, not ``None`` — locks the silent-disable
  scenario the R2 verifier flagged.
* **J3** — ``_scan_roots`` includes ``scripts/`` and ``db/`` so the
  whitelist (``_INTENTIONALLY_UNWIRED_MODULES``) is load-bearing;
  a new script that DELETEs a guarded hypertable would fail this
  invariant unless explicitly whitelisted with a documented reason.
* **J4** — ``_module_contains_delete_from`` requires the DELETE literal
  to be an argument to a call node (``cursor.execute``, ``_fetch_all``,
  ``_replace_values``, etc.), NOT just a bare ``ast.Constant``. Skips
  module-level docstring nodes and other decorative string uses.

The scan is *derived from* :data:`packages.common.timescale_write_guard.HYPERTABLES_GUARDED`
so adding a new guarded hypertable + wiring it up does not require editing
this test; only removing a wired guard call OR adding an unwired DELETE
site causes a failure.

Scan surface:

* ``workers/**/*.py``
* ``packages/common/**/*.py``
* ``scripts/**/*.py``
* ``db/**/*.py``

Whitelisted non-wiring modules (documented as intentionally unwired):

* ``packages/common/timescale_write_guard.py`` — the guard module itself.
* ``db/seeds/seed_demo.py`` — seeds fresh empty demo DB; never targets
  compressed chunks in production. Docstring documents the exemption.
* ``scripts/reset_qhh_smoke_db.py`` — smoke-DB reset script; analogous
  non-wiring rationale to seed_demo (fresh DB reset, never touches
  production compressed data).
* ``packages/common/compressed_chunk_cold_probe/shell.py`` — disposable
  2.10.2 probe schema bootstrap; DELETE targets only the four-column
  fixture on a fail-closed isolated cluster, never production. Docstring
  documents the exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from packages.common.timescale_write_guard import HYPERTABLES_GUARDED

REPO_ROOT = Path(__file__).resolve().parents[1]

_GUARD_MODULE_PATH = REPO_ROOT / "packages" / "common" / "timescale_write_guard.py"

# Modules documented as intentionally unwired. Any addition MUST come with a
# module-docstring justification (see seed_demo.py:1-21 as the template).
_INTENTIONALLY_UNWIRED_MODULES: frozenset[Path] = frozenset(
    {
        _GUARD_MODULE_PATH,
        REPO_ROOT / "db" / "seeds" / "seed_demo.py",
        REPO_ROOT / "scripts" / "reset_qhh_smoke_db.py",
        REPO_ROOT / "packages" / "common" / "compressed_chunk_cold_probe" / "shell.py",
    }
)

_EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {"__pycache__", "tests", ".venv", "node_modules"}
)


def _scan_roots() -> tuple[Path, ...]:
    """The scoped source tree the scan walks.

    workers/** is where SHUD ingest workers live; packages/common/** hosts
    the shared apply site; scripts/** hosts operational runners; db/**
    hosts the seed + migration entrypoints. Display API (``apps/api``),
    frontend (``apps/frontend``), and unrelated packages are OUT of scope
    for the guard by ADR 0001. The scripts/ and db/ inclusions make the
    ``_INTENTIONALLY_UNWIRED_MODULES`` whitelist load-bearing (J3): a new
    script or seed that DELETEs a guarded hypertable will fail this
    invariant unless it is explicitly whitelisted with a documented reason.
    """
    return (
        REPO_ROOT / "workers",
        REPO_ROOT / "packages" / "common",
        REPO_ROOT / "scripts",
        REPO_ROOT / "db",
    )


def _iter_python_sources() -> list[Path]:
    files: list[Path] = []
    for root in _scan_roots():
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in _EXCLUDED_DIR_PARTS for part in path.parts):
                continue
            if path in _INTENTIONALLY_UNWIRED_MODULES:
                continue
            files.append(path)
    return files


def _iter_call_argument_strings(call: ast.Call) -> list[str]:
    """Yield every string-value contribution passed as an argument to ``call``.

    Includes both positional ``args`` and keyword ``keywords``. For
    ``ast.Constant`` strings, yields the raw value; for ``ast.JoinedStr``
    (f-strings), yields each constant part joined so a needle that spans
    only literal parts (e.g. ``"DELETE FROM foo.bar"`` in a bare f-string
    without variable substitutions) is still found. Non-string constants
    are ignored.
    """
    strings: list[str] = []
    argument_nodes: list[ast.expr] = []
    argument_nodes.extend(call.args)
    for kw in call.keywords:
        if kw.value is not None:
            argument_nodes.append(kw.value)
    for node in argument_nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            # F-string literal constant parts joined together (variable
            # substitutions surface as FormattedValue, which we skip — a
            # DELETE that goes through a dynamic ``{table}`` substitution
            # cannot be statically matched here and is expected to be on
            # the intentionally-unwired whitelist).
            joined = "".join(
                value.value
                for value in node.values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
            strings.append(joined)
    return strings


#: The complete set of admitted `valid_time` bound spellings for a guarded DELETE
#: (#1642).  A guarded DELETE must carry BOTH — the lower bound `valid_time >=` and
#: the upper bound `valid_time <=` — in the SAME string literal.
#:
#: This is the whole allowlist, and the half-open upper bound `valid_time <` is
#: deliberately NOT in it: the guard certifies a window closed on both ends
#: (`check_batch_targets_uncompressed(valid_time_min, valid_time_max)`), so a
#: half-open DELETE targets a different row set than the one that was inspected —
#: exactly the "guard window narrower than the DELETE's target set" hole this
#: invariant exists to refuse.  A writer that legitimately needs another spelling
#: widens this set IN THE SAME CHANGE with a recorded reason.
_ADMITTED_VALID_TIME_LOWER_BOUND = "valid_time >="
_ADMITTED_VALID_TIME_UPPER_BOUND = "valid_time <="


def _delete_literal_is_valid_time_bounded(text: str) -> bool:
    """True iff a DELETE literal carries both admitted `valid_time` bounds.

    Python concatenates adjacent string literals at parse time, so the two
    two-part writers (``workers/forcing_producer/store.py``,
    ``packages/common/forcing_domain_handoff_apply.py``) and the triple-quoted
    one (``workers/output_parser/parser.py``) all present as ONE
    ``ast.Constant`` — the predicate can therefore be a plain substring pair on
    a single literal, with no cross-node reassembly.

    Note what "refused" falls out of for free: `valid_time <` alone never
    contains `valid_time <=`, so the half-open form fails the upper-bound test
    without a separate rule.  A literal carrying `valid_time <= %s` obviously
    also contains the substring `valid_time <`, which is why the predicate tests
    for the ADMITTED spellings rather than scanning for refused ones.
    """

    return _ADMITTED_VALID_TIME_LOWER_BOUND in text and _ADMITTED_VALID_TIME_UPPER_BOUND in text


def _missing_valid_time_bounds(text: str) -> list[str]:
    """The admitted bound spellings absent from ``text``, for the failure message."""

    return [
        bound
        for bound in (_ADMITTED_VALID_TIME_LOWER_BOUND, _ADMITTED_VALID_TIME_UPPER_BOUND)
        if bound not in text
    ]


def _guarded_delete_literals(tree: ast.AST, schema: str, table: str) -> list[tuple[str, str]]:
    """Every ``(enclosing function name, literal)`` that DELETEs from ``schema.table``.

    Covers exactly the sites :func:`_delete_site_has_guard_in_same_function`
    governs — a string literal passed as a call argument (J4) — and attributes
    each to its INNERMOST enclosing function, so a locally-defined
    ``pre_write_cursor_hook`` is named as itself rather than as its enclosing
    writer.  A DELETE outside any function is reported under
    ``"<module level>"`` rather than skipped: an unbounded module-level DELETE
    is still a hole.
    """

    needle = f"DELETE FROM {schema}.{table}"
    hits: list[tuple[str, str]] = []

    def _descend(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_owner = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else owner
            )
            if isinstance(child, ast.Call):
                for value in _iter_call_argument_strings(child):
                    if needle in value:
                        hits.append((child_owner, value))
            _descend(child, child_owner)

    _descend(tree, "<module level>")
    return hits


def _module_contains_delete_from(tree: ast.AST, schema: str, table: str) -> bool:
    """Return True if ``tree`` contains ``DELETE FROM {schema}.{table}`` as a Call arg.

    J4 tightening: match only string literals that are ARGUMENTS to a
    ``ast.Call`` node (e.g. ``cursor.execute(...)``, ``_fetch_all(...)``,
    ``_replace_values(...)``). Module-level docstrings, class docstrings,
    and other decorative string uses are not call arguments and therefore
    cannot be false-positive matches.
    """
    needle = f"DELETE FROM {schema}.{table}"
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for value in _iter_call_argument_strings(node):
            if needle in value:
                return True
    return False


def _function_contains_guard_call(function_node: ast.AST) -> bool:
    """Return True if ``function_node`` (a FunctionDef subtree) calls the guard.

    ``ast.walk`` on a FunctionDef visits nested definitions too, so a
    ``pre_write_cursor_hook=_guard`` where ``_guard`` is a locally-defined
    function containing the guard call still returns True. Matches either
    an unqualified call (``check_batch_targets_uncompressed(...)``) or an
    attribute-access call (``mod.check_batch_targets_uncompressed(...)``).
    """
    for node in ast.walk(function_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "check_batch_targets_uncompressed":
            return True
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "check_batch_targets_uncompressed"
        ):
            return True
    return False


def _delete_site_has_guard_in_same_function(
    tree: ast.AST, schema: str, table: str
) -> tuple[bool, list[str]]:
    """For every enclosing function containing a DELETE-from-guarded-table site,
    verify that function ALSO contains a call to the guard.

    Returns ``(all_wired, failures)`` where ``failures`` names the enclosing
    functions that DELETE from ``{schema}.{table}`` without a co-located
    guard call. If NO enclosing function was found containing the DELETE
    (e.g. a module-level executable statement), returns ``(True, [])`` —
    the module-level DELETE from a guarded table would be caught by the
    broader :func:`_module_contains_delete_from` scan combined with the
    off-manifest test below.
    """
    needle = f"DELETE FROM {schema}.{table}"
    failures: list[str] = []
    # Walk every FunctionDef / AsyncFunctionDef; per J1 the check is
    # per-function, not per-module. Nested functions are covered because
    # the enclosing FunctionDef's ast.walk visits them.
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        has_delete = False
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.Call):
                continue
            for value in _iter_call_argument_strings(descendant):
                if needle in value:
                    has_delete = True
                    break
            if has_delete:
                break
        if not has_delete:
            continue
        if not _function_contains_guard_call(node):
            failures.append(node.name)
    return (not failures, failures)


def _module_imports_guard(tree: ast.AST) -> bool:
    """Belt-and-suspenders: the module MUST also import from the guard module.

    Any wire site that calls ``check_batch_targets_uncompressed`` should
    import it from ``packages.common.timescale_write_guard`` — a bare call
    without the import would fail at Python parse-time, but explicitly
    asserting the import shape catches "wired the call but stubbed the
    import" bugs.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "packages.common.timescale_write_guard":
                for alias in node.names:
                    if alias.name == "check_batch_targets_uncompressed":
                        return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "packages.common.timescale_write_guard":
                    return True
    return False


def _wire_site_hits() -> dict[tuple[str, str], set[Path]]:
    """For each guarded pair, return the modules that DELETE from it."""
    hits: dict[tuple[str, str], set[Path]] = {pair: set() for pair in HYPERTABLES_GUARDED}
    for path in _iter_python_sources():
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for schema, table in HYPERTABLES_GUARDED:
            if _module_contains_delete_from(tree, schema, table):
                hits[(schema, table)].add(path)
    return hits


# ---------------------------------------------------------------------------
# C1 — Every guarded hypertable's DELETE site co-locates a guard call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hypertable",
    sorted(HYPERTABLES_GUARDED),
    ids=lambda pair: f"{pair[0]}.{pair[1]}",
)
def test_every_guarded_hypertable_has_a_guarded_delete_site(
    hypertable: tuple[str, str],
) -> None:
    """For each ``(schema, table)`` in HYPERTABLES_GUARDED, every production
    function that issues ``DELETE FROM {schema}.{table}`` MUST also call
    ``check_batch_targets_uncompressed`` in the SAME enclosing function
    (J1 per-DELETE-site enforcement), and the module MUST import the guard
    from the shared helper module.
    """
    schema, table = hypertable
    hits = _wire_site_hits()[hypertable]
    assert hits, (
        f"No production module was found DELETing from {schema}.{table}; "
        "either the wire test is stale or the write path was removed. "
        "Update HYPERTABLES_GUARDED / this test in sync."
    )
    for module_path in sorted(hits):
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        all_wired, unwired_functions = _delete_site_has_guard_in_same_function(
            tree, schema, table
        )
        assert all_wired, (
            f"{module_path.relative_to(REPO_ROOT)}: function(s) "
            f"{unwired_functions!r} DELETE from {schema}.{table} but do NOT "
            "call check_batch_targets_uncompressed in the same function. "
            "Wire the guard before the DELETE (directly, or via a "
            "pre_write_cursor_hook whose value is a locally-defined "
            "function containing the guard call)."
        )
        assert _module_imports_guard(tree), (
            f"{module_path.relative_to(REPO_ROOT)} calls the guard but does "
            "not import it from packages.common.timescale_write_guard. "
            "Divergent per-path implementations are forbidden (design D5)."
        )


# ---------------------------------------------------------------------------
# #1642 — Every guarded DELETE carries the window its guard certified
# ---------------------------------------------------------------------------


#: Synthetic literals pinning the window predicate independently of the
#: repository's current writers, in the style of ``_UNADMITTED_MODULE_LEVEL_FORMS``
#: in ``tests/test_production_scheduler.py``.  Without these, deleting the
#: predicate's body and returning ``True`` would keep the repository scan green
#: forever — the scan can only ever observe source that already passes.
_VALID_TIME_WINDOW_PREDICATE_CASES: tuple[tuple[str, str, bool], ...] = (
    (
        "bounded_one_line",
        "DELETE FROM met.forcing_station_timeseries "
        "WHERE forcing_version_id = %s AND valid_time >= %s AND valid_time <= %s",
        True,
    ),
    (
        "bounded_multi_line_indented",
        """
                DELETE FROM hydro.river_timeseries
                WHERE run_key = %s
                  AND valid_time >= %s
                  AND valid_time <= %s
                """,
        True,
    ),
    (
        "bounded_named_parameters",
        "DELETE FROM hydro.river_timeseries WHERE valid_time >= %(valid_time_min)s "
        "AND valid_time <= %(valid_time_max)s",
        True,
    ),
    (
        "unbounded",
        "DELETE FROM hydro.river_timeseries WHERE run_key = %s",
        False,
    ),
    (
        "lower_bound_only",
        "DELETE FROM hydro.river_timeseries WHERE run_key = %s AND valid_time >= %s",
        False,
    ),
    (
        "upper_bound_only",
        "DELETE FROM hydro.river_timeseries WHERE run_key = %s AND valid_time <= %s",
        False,
    ),
    (
        "half_open_upper_bound",
        "DELETE FROM hydro.river_timeseries WHERE valid_time >= %s AND valid_time < %s",
        False,
    ),
)


@pytest.mark.parametrize(
    ("literal", "expected"),
    [(literal, expected) for _, literal, expected in _VALID_TIME_WINDOW_PREDICATE_CASES],
    ids=[case_id for case_id, _, _ in _VALID_TIME_WINDOW_PREDICATE_CASES],
)
def test_valid_time_window_predicate_admits_only_the_closed_window_spellings(
    literal: str, expected: bool
) -> None:
    """#1642: pin the window predicate on synthetic literals, both directions.

    The bounded forms pass across line breaks, indentation and parameter style,
    so reformatting a writer cannot false-red the scan.  All four unbounded
    shapes fail — including ``half_open_upper_bound``, which is the one a
    reasonable author writes by accident: the guard's certified window is closed
    on both ends, so ``valid_time <`` targets a different row set than the one
    ``check_batch_targets_uncompressed`` inspected.
    """

    assert _delete_literal_is_valid_time_bounded(literal) is expected


def test_every_guarded_delete_literal_carries_the_certified_valid_time_window() -> None:
    """#1642: presence of the guard call is not the same claim as a bounded DELETE.

    ``test_every_guarded_hypertable_has_a_guarded_delete_site`` above asserts the
    guard is CALLED next to the DELETE; it says nothing about the window the
    DELETE actually targets.  A writer that calls the guard for
    ``[valid_time_min, valid_time_max]`` and then DELETEs the lineage unbounded
    passes that test while destroying rows the guard never inspected — and an
    unbounded DELETE is rejected outright by TimescaleDB once any chunk of the
    hypertable is compressed.  This test closes that gap structurally.

    ``packages/common/compressed_chunk_cold_probe/shell.py`` carries the
    half-open form but sits on ``_INTENTIONALLY_UNWIRED_MODULES`` and therefore
    never reaches ``_wire_site_hits()``; the admitted-spelling set is NOT widened
    for it.
    """

    scanned = 0
    for hypertable, modules in sorted(_wire_site_hits().items()):
        schema, table = hypertable
        for module_path in sorted(modules):
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            literals = _guarded_delete_literals(tree, schema, table)
            assert literals, (
                f"{module_path.relative_to(REPO_ROOT)} is a wire-site hit for "
                f"{schema}.{table} but no DELETE literal could be re-located inside "
                "it — the scan and this predicate have drifted apart."
            )
            for function_name, literal in literals:
                scanned += 1
                missing = _missing_valid_time_bounds(literal)
                assert not missing, (
                    f"{module_path.relative_to(REPO_ROOT)}::{function_name} DELETEs "
                    f"from {schema}.{table} without {' and '.join(missing)} in the same "
                    "SQL literal. The guard certifies a window closed on BOTH ends "
                    "(check_batch_targets_uncompressed(valid_time_min, valid_time_max)), "
                    "so the DELETE literal must carry both bounds -- this predicate checks "
                    "their PRESENCE, not that they name the guard's own window. Admitted "
                    f"spellings are exactly {_ADMITTED_VALID_TIME_LOWER_BOUND!r} and "
                    f"{_ADMITTED_VALID_TIME_UPPER_BOUND!r}; a half-open 'valid_time <' "
                    "is refused. Bound the DELETE, or widen the admitted set in this "
                    "file in the same change with a recorded reason.\n"
                    f"literal: {literal!r}"
                )
    assert scanned >= 3, (
        f"Only {scanned} guarded DELETE literal(s) reached the window predicate; at least "
        "the three writers known at authoring time must reach it. This is a FLOOR, not a "
        "count -- more literals are expected as writers are added. A vacuous scan would "
        "make this invariant unfalsifiable."
    )


# ---------------------------------------------------------------------------
# C1 — No off-manifest write site DELETEs a guarded hypertable
# ---------------------------------------------------------------------------


_EXPECTED_WIRE_MODULES: frozenset[Path] = frozenset(
    {
        REPO_ROOT / "workers" / "output_parser" / "parser.py",
        REPO_ROOT / "workers" / "forcing_producer" / "store.py",
        REPO_ROOT / "packages" / "common" / "forcing_domain_handoff_apply.py",
    }
)


def test_no_off_manifest_write_site_deletes_guarded_hypertable() -> None:
    """The union of wire hits across ALL guarded pairs MUST equal the three
    expected modules. A fourth hit is a hard fail — either a new wire site
    slipped in unaudited, or an old module regressed to DELETE a guarded
    hypertable. Either way the invariant fails until reviewed.
    """
    all_hits: set[Path] = set()
    for hits in _wire_site_hits().values():
        all_hits.update(hits)
    unexpected = all_hits - _EXPECTED_WIRE_MODULES
    assert not unexpected, (
        "Unexpected module(s) DELETE from a guarded hypertable — audit the "
        "new wire site and either add it here or wire the guard:\n"
        + "\n".join(sorted(str(path.relative_to(REPO_ROOT)) for path in unexpected))
    )
    missing = _EXPECTED_WIRE_MODULES - all_hits
    assert not missing, (
        "Expected wire site(s) no longer DELETE a guarded hypertable — "
        "verify the write path was intentionally removed and update this "
        "invariant:\n"
        + "\n".join(sorted(str(path.relative_to(REPO_ROOT)) for path in missing))
    )


def test_intentionally_unwired_modules_are_documented() -> None:
    """Sanity check: every module in ``_INTENTIONALLY_UNWIRED_MODULES`` MUST
    exist on disk (so a rename doesn't silently drop it from the whitelist)
    and MUST NOT accidentally call the guard (which would signal that the
    "intentional" label is stale).
    """
    for path in _INTENTIONALLY_UNWIRED_MODULES:
        assert path.exists(), f"Whitelisted non-wiring module missing: {path}"
        if path == _GUARD_MODULE_PATH:
            continue  # The guard module defines the function.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Legacy module-level check: whitelisted modules must not accidentally
        # start calling the guard (drift signal). Uses the anywhere-in-module
        # variant, not the per-function variant, because a stale whitelist
        # is a paper-cut regardless of which function contains the guard call.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "check_batch_targets_uncompressed":
                pytest.fail(
                    f"{path.relative_to(REPO_ROOT)} is on the intentionally-"
                    "unwired whitelist but now calls the guard. Either remove "
                    "it from the whitelist or drop the guard call."
                )
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "check_batch_targets_uncompressed"
            ):
                pytest.fail(
                    f"{path.relative_to(REPO_ROOT)} is on the intentionally-"
                    "unwired whitelist but now calls the guard. Either remove "
                    "it from the whitelist or drop the guard call."
                )


# ---------------------------------------------------------------------------
# J2 — Explicit `pre_write_cursor_hook=` binding lock (forcing_producer/store.py)
# ---------------------------------------------------------------------------


def test_forcing_producer_store_pre_write_cursor_hook_is_wired() -> None:
    """R2/J2: lock the ``pre_write_cursor_hook=`` binding on the forcing store.

    ``workers/forcing_producer/store.py::replace_forcing_timeseries`` wires
    the compressed-chunk guard through ``_replace_values`` via a
    ``pre_write_cursor_hook`` keyword. A future refactor that silently
    passes ``None`` (or removes the keyword) would disable the guard on
    the forcing path — the R1 wire-path tests would still pass because
    they run against ``_replace_values`` directly. This test AST-parses
    the source, finds the ``_replace_values`` call inside
    ``replace_forcing_timeseries``, and asserts the keyword's value is
    the ``ast.Name('_guard')`` reference — NOT ``ast.Constant(None)``.
    """
    store_path = REPO_ROOT / "workers" / "forcing_producer" / "store.py"
    tree = ast.parse(store_path.read_text(encoding="utf-8"), filename=str(store_path))

    target_function: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "replace_forcing_timeseries"
        ):
            target_function = node
            break
    assert target_function is not None, (
        "workers/forcing_producer/store.py must define replace_forcing_timeseries"
    )

    replace_values_calls: list[ast.Call] = []
    for descendant in ast.walk(target_function):
        if not isinstance(descendant, ast.Call):
            continue
        func = descendant.func
        # Match self._replace_values(...) attribute-access form.
        if isinstance(func, ast.Attribute) and func.attr == "_replace_values":
            replace_values_calls.append(descendant)

    assert len(replace_values_calls) == 1, (
        f"Expected exactly one self._replace_values(...) call inside "
        f"replace_forcing_timeseries, found {len(replace_values_calls)}."
    )
    call = replace_values_calls[0]
    hook_keyword: ast.keyword | None = None
    for kw in call.keywords:
        if kw.arg == "pre_write_cursor_hook":
            hook_keyword = kw
            break
    assert hook_keyword is not None, (
        "replace_forcing_timeseries must pass pre_write_cursor_hook= to "
        "_replace_values — the guard is wired through this hook."
    )
    # The hook value MUST be a Name binding to a local function. A
    # bare ``None`` (Constant) would silently disable the guard.
    value = hook_keyword.value
    assert isinstance(value, ast.Name), (
        "pre_write_cursor_hook= must be bound to a locally-defined "
        f"function Name, not {ast.dump(value)!r}."
    )
    # Also confirm the referenced Name is defined as a FunctionDef in the
    # enclosing replace_forcing_timeseries scope (guarding against a stray
    # module-level Name that happens to share the identifier).
    local_function_names: set[str] = {
        inner.name
        for inner in target_function.body
        if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert value.id in local_function_names, (
        f"pre_write_cursor_hook= references {value.id!r} but no local "
        "function with that name is defined inside "
        f"replace_forcing_timeseries. Defined locally: {sorted(local_function_names)}."
    )
    # And confirm that local function actually calls the guard — anchoring
    # the invariant end-to-end.
    local_hook_function: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for inner in target_function.body:
        if (
            isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
            and inner.name == value.id
        ):
            local_hook_function = inner
            break
    assert local_hook_function is not None
    assert _function_contains_guard_call(local_hook_function), (
        f"Local hook function {value.id!r} does not call "
        "check_batch_targets_uncompressed — the guard is effectively "
        "disabled even though the wiring shape is intact."
    )
