"""The nine wired forcing readers: per-store routing, composition, narrow shape.

Written for #1990 task 7.2 (wiring) and **rewritten for #1991 task 7.3**
(routing), which is where its central claim inverts. Nothing in this module
executes SQL against a database; it drives the readers with recording cursors and
reads what they hand them.

1. **Routing, not a store literal.** I11's must-preserve M6 was "no narrow render
   ever reaches an executed statement", because the narrow table did not exist.
   ``db/migrations/000061_forcing_station_timeseries_narrow_expand.sql`` creates
   it, and the property becomes its own inverse: no reader may be PINNED to
   either store. A reader left on ``"legacy"`` never sees a version written after
   the expand; a reader hardcoded to ``"narrow"`` goes dark for every version
   000061 classified as legacy. Both are must-preserve M2 failures and both are
   invisible in CI, because either statement runs and returns rows — just not all
   of them. Asserted structurally (by AST, over every registered reader's file)
   and on the executed text, per store.

2. **What survives of M1 — byte identity.**
   ``tests/fixtures/forcing_read_path_pre_wiring_0c92093e7.json`` is a snapshot
   captured by executing the readers in a worktree checked out at the PRE-WIRING
   commit: an independent source of truth, not a golden regenerated from the code
   it certifies. Five readers must still match it exactly when the version in
   scope is legacy-routed, modulo ONE substitution — the relation 000061 renamed.
   Three carry declared task 7.3 deltas and are re-pinned structurally instead:
   display-coverage (now a two-store composition), the QHH fallback (its embedded
   candidate CTE gained the store projection) and the bootstrap count (its
   registered pair changed shape so the aggregate could move outside the
   composition).

3. **I1–I5 — the narrow variants' shape.** Unchanged from I11 and still a text
   oracle over the registered pairs.

4. **I7 — cross-store composition**, which I11 could write but not prove. The two
   readers that span versions of both stores compose the two rendered fact-row
   subrelations inside the owning reader, ahead of any outer aggregate, with no
   shared text-level union combinator.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from packages.common import best_available, display_coverage, forecast_store
from packages.common.forcing_store_routing import (
    FORCING_STORE_LEGACY,
    FORCING_STORE_NARROW,
)
from packages.common.forcing_ts_render import (
    FORCING_STORES,
    FORCING_TABLE,
    FORCING_TABLE_LEGACY,
    render_forcing_ts_sql,
)
from scripts import reset_qhh_smoke_db
from tests.forcing_ts_template_registry import FORCING_REGISTRY, REPO_ROOT, entry_by_key

# AT MODULE SCOPE ON PURPOSE, and the scope is the whole point rather than a
# style preference. Reader #2's byte-identity pin is driven through four private
# helpers of ``tests/test_qhh_latest_fallback_pushdown.py``, which is a member of
# river's ``SQL_SHAPE_ORACLE_TESTS``. ``select_ci_tests._build_suite_importer_index``
# inverts MODULE-LEVEL import edges only, so while this import sat inside
# ``_executed_latest_product`` a PR renaming ``_fallback_statements`` over there
# did not select this suite and went red on the post-merge master run instead —
# the exact debt class #1990 cut (b) was sent to repay, pointing the other way.
# Hoisting it is what makes the dependency visible to the selector; it is also
# the shape that file already uses for its own cross-suite helper import
# (``from tests.test_sql_shape_helpers import outer_predicates``, :38).
from tests.test_qhh_latest_fallback_pushdown import (
    _HEADER_ROW,
    BindingCheckedCursor,
    _fallback_statements,
    _run_fallback,
)
from workers.model_registry import qhh_production_bootstrap

PRE_WIRING_FIXTURE = REPO_ROOT / "tests/fixtures/forcing_read_path_pre_wiring_0c92093e7.json"

#: The commit the snapshot was captured at — the merge of cut (a), which is the
#: last state in which the nine readers still spelled their own SQL. Pinned so a
#: re-capture at a later base cannot be passed off as the same evidence.
PRE_WIRING_COMMIT = "0c92093e72be3d0030cd4c2adbaef75203f99254"

#: Every module that contains a wired forcing reader. Derived from the register
#: rather than listed, so a tenth reader in a sixth file is covered by the AST
#: sweep below the moment it is registered.
WIRED_READER_PATHS: tuple[str, ...] = tuple(sorted({entry.path for entry in FORCING_REGISTRY}))

#: The narrow forcing table's columns (`design.md` D9). The narrow variants may
#: reference these on the fact alias and nothing else — which is invariants I1
#: and I2 in one subset check: every text identity column of the legacy table
#: (``station_id``, ``forcing_version_id``, ``variable``, ``unit``,
#: ``quality_flag``, ``source_id``, ``basin_version_id``) is absent from it, and
#: so is any column that simply does not exist.
NARROW_FACT_COLUMNS = frozenset(
    {
        "forcing_version_key",
        "station_key",
        "valid_time",
        "variable_e",
        "value",
        "unit_e",
        "quality_flag_e",
        "native_resolution",
    }
)

#: The enum columns. A text function over one of these does not compile without
#: an explicit cast, which is the I5 trap: ``BTRIM(unit)`` is legal legacy SQL and
#: ``BTRIM(unit_e)`` is not legal narrow SQL.
NARROW_ENUM_COLUMNS = ("variable_e", "unit_e", "quality_flag_e")


#: The statements re-recorded by a DECLARED delta rather than captured at
#: ``PRE_WIRING_COMMIT``. The fixture carries the same list under ``delta_from``;
#: both are asserted against each other so a silent re-record of a third
#: statement cannot pass as the same evidence.
DELTA_RECORDED_STATEMENTS = frozenset(
    {"display_coverage.refresh_statement", "forecast_store.latest_product_statement"}
)


def _frozen() -> dict[str, str]:
    payload = json.loads(PRE_WIRING_FIXTURE.read_text(encoding="utf-8"))
    assert payload["captured_from_commit"] == PRE_WIRING_COMMIT
    delta = payload["delta_from"]
    assert delta["captured_from_commit"] == PRE_WIRING_COMMIT
    assert set(delta["statements"]) == DELTA_RECORDED_STATEMENTS
    return payload["statements"]


FROZEN = _frozen()


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


class RecordingCursor:
    """The narrowest cursor the nine readers need, recording what they execute."""

    rowcount = 0
    description: list[Any] = []

    def __init__(self, scalar: dict[str, Any] | None = None) -> None:
        self.statements: list[tuple[str, Any]] = []
        self._scalar = scalar

    def execute(self, statement: str, parameters: Any = None) -> None:
        self.statements.append((statement, parameters))

    def fetchone(self) -> Any:
        return self._scalar

    def fetchall(self) -> list[Any]:
        return []


def _executed_membership(store: str = FORCING_STORE_LEGACY) -> tuple[str, Any]:
    cursor = RecordingCursor()
    forecast = forecast_store.PsycopgForecastStore("postgresql://test")
    with pytest.raises(forecast_store.ForecastStoreError):
        forecast._validate_station_forcing_membership(
            cursor,
            station_id="qhh_stn_001",
            forcing_version={
                "forcing_version_id": "forc_qhh_gfs_2026050700",
                "timeseries_store": store,
            },
            valid_time_start=_dt("2026-05-07T00:00:00Z"),
            valid_time_end=_dt("2026-05-08T00:00:00Z"),
        )
    return cursor.statements[-1]


def _executed_readiness_overall(store: str = FORCING_STORE_LEGACY) -> tuple[str, Any]:
    cursor = RecordingCursor()
    forecast = forecast_store.PsycopgForecastStore("postgresql://test")
    forecast._fetch_forcing_readiness_overall(
        cursor,
        forcing_version_id="forc_qhh_gfs_2026050700",
        store=store,
        valid_time_start=_dt("2026-05-07T00:00:00Z"),
        valid_time_end=_dt("2026-05-08T00:00:00Z"),
        variables=["PRCP"],
    )
    return cursor.statements[-1]


def _executed_readiness_variable_rows(store: str = FORCING_STORE_LEGACY) -> tuple[str, Any]:
    cursor = RecordingCursor()
    forecast = forecast_store.PsycopgForecastStore("postgresql://test")
    forecast._fetch_forcing_readiness_variable_rows(
        cursor,
        forcing_version_id="forc_qhh_gfs_2026050700",
        store=store,
        valid_time_start=_dt("2026-05-07T00:00:00Z"),
        valid_time_end=_dt("2026-05-08T00:00:00Z"),
        variables=["PRCP"],
    )
    return cursor.statements[-1]


def _executed_station_series(
    *,
    from_time: datetime | None,
    to_time: datetime | None,
    store: str = FORCING_STORE_LEGACY,
) -> tuple[str, Any]:
    cursor = RecordingCursor()
    forecast = forecast_store.PsycopgForecastStore("postgresql://test")
    forecast._fetch_station_series_rows(
        cursor,
        station_id="qhh_stn_001",
        forcing_version_id="forc_qhh_gfs_2026050700",
        store=store,
        valid_time_start=_dt("2026-05-07T00:00:00Z"),
        valid_time_end=_dt("2026-05-08T00:00:00Z"),
        variables=["PRCP", "TEMP"],
        from_time=from_time,
        to_time=to_time,
        limit=2,
    )
    return cursor.statements[-1]


def _executed_dynamic_forcing_count() -> tuple[str, Any]:
    cursor = RecordingCursor(scalar={"count": 0})
    qhh_production_bootstrap._dynamic_forcing_counts(cursor, "basins_qhh_shud")
    return cursor.statements[-1]


def _executed_forcing_delete(store: str = FORCING_STORE_LEGACY) -> tuple[str, Any]:
    physical = FORCING_TABLE_LEGACY if store == FORCING_STORE_LEGACY else FORCING_TABLE
    cursor = RecordingCursor()
    deleted: dict[str, int] = {}
    reset_qhh_smoke_db._delete_rendered(
        cursor,
        deleted,
        physical,
        render_forcing_ts_sql(
            reset_qhh_smoke_db._FORCING_TIMESERIES_DELETE_TEMPLATES,
            store,
            entry="reset_qhh_smoke_db.forcing_timeseries_delete",
        ).sql,
        (["forc_qhh_gfs_2026050700"],),
    )
    assert deleted == {physical: 0}
    return cursor.statements[-1]


def _executed_latest_product(store: str = FORCING_STORE_LEGACY) -> str:
    cursor = BindingCheckedCursor(header_rows=[{**_HEADER_ROW, "forcing_timeseries_store": store}])
    _run_fallback(cursor)
    _header_sql, heavy_sql = _fallback_statements(cursor)
    return heavy_sql


# ---------------------------------------------------------------------------
# M1 — byte identity against the pre-wiring snapshot
# ---------------------------------------------------------------------------


def test_the_snapshot_covers_the_eight_byte_identical_readers() -> None:
    """Eight keys, and the ninth deliberately absent.

    A missing key would make the corresponding equality test below silently
    vacuous if it were written as ``FROZEN.get(...)``; it is not, but the set is
    pinned anyway so that "reader X is no longer byte-identical" has to be an
    explicit edit here rather than a quiet deletion there.
    """
    assert set(FROZEN) == {
        "best_available.forcing_inputs",
        "display_coverage.refresh_statement",
        "forecast_store.forcing_readiness_overall",
        "forecast_store.forcing_readiness_variable_rows",
        "forecast_store.latest_product_statement",
        "forecast_store.station_forcing_membership",
        "qhh_production_bootstrap.dynamic_forcing_count",
        "reset_qhh_smoke_db.forcing_timeseries_delete",
    }
    assert "forecast_store.station_series_rows" not in FROZEN


#: The line the task 7.3 projection is inserted AFTER in the candidate_runs CTE,
#: and the six lines inserted. Both verbatim from
#: ``packages/common/forecast_store.py:716-722``, indentation included, so the
#: pin below stays a sequence equality on the WHOLE statement rather than a
#: membership test that cannot see a line moved or duplicated.
_LATEST_PRODUCT_STORE_ANCHOR = "                    fv.forcing_version_id AS fv_forcing_version_id,\n"
_LATEST_PRODUCT_STORE_PROJECTION = (
    "                    -- #1991 (task 7.3): the candidate's forcing store. This CTE\n"
    "                    -- already LEFT JOINs met.forcing_version, so routing the\n"
    "                    -- fallback's station leg costs no round trip and no extra\n"
    "                    -- join -- the header statement projects it and the heavy\n"
    "                    -- statement renders the matching variant.\n"
    "                    fv.timeseries_store AS forcing_timeseries_store,\n"
)

#: The SECOND declared delta, #2517: the CTE's store column, passed through the
#: heavy statement's final SELECT so the served row carries it. Task 7.3 stopped
#: at the CTE because only the header needed to read it; the public
#: ``quality.query_indexes`` evidence is assembled from the ROW, and a column
#: that never leaves the CTE cannot route it. Declared the same way as the
#: projection above — anchor plus inserted line, applied to the snapshot — so
#: the pin stays a whole-statement sequence equality.
_LATEST_PRODUCT_ROW_STORE_ANCHOR = "                cr.forcing_checksum,\n"
_LATEST_PRODUCT_ROW_STORE_PROJECTION = "                cr.forcing_timeseries_store,\n"


def _renamed_to_legacy(frozen_sql: str) -> str:
    """The pre-wiring text with the fact table under the name 000061 left it.

    The ONE licensed difference between the snapshot and what a legacy-routed
    reader emits after task 7.3. It is a whole-string substitution, so a reader
    that changed anything else — a predicate, a join, the indentation — still
    fails the equality. The snapshot carries no ``…_legacy`` spelling of its own,
    so this cannot double-apply.
    """
    return frozen_sql.replace(FORCING_TABLE, FORCING_TABLE_LEGACY)


@pytest.mark.parametrize(
    ("key", "executed"),
    [
        ("forecast_store.station_forcing_membership", lambda: _executed_membership()[0]),
        ("forecast_store.forcing_readiness_overall", lambda: _executed_readiness_overall()[0]),
        (
            "forecast_store.forcing_readiness_variable_rows",
            lambda: _executed_readiness_variable_rows()[0],
        ),
        ("reset_qhh_smoke_db.forcing_timeseries_delete", lambda: _executed_forcing_delete()[0]),
        (
            "best_available.forcing_inputs",
            lambda: render_forcing_ts_sql(
                best_available._FORCING_INPUTS_TEMPLATES,
                FORCING_STORE_LEGACY,
                entry="best_available.forcing_inputs",
            ).sql,
        ),
    ],
)
def test_legacy_routed_statements_differ_from_the_snapshot_only_by_the_rename(key: str, executed) -> None:
    """M1, as much of it as task 7.3 leaves standing — and it is the useful part.

    These five readers now ROUTE instead of passing the literal ``"legacy"``, and
    when the version in scope is legacy-routed their statement must be the text
    they emitted before any of this started, with one substitution: the relation
    000061 renamed. Anything else that moved in these statements between the
    pre-wiring commit and now shows up here as an inequality.

    Driven through the reader rather than compared against the template constant,
    for the pre-existing reason: a template that renders correctly and a call site
    that ignores it are indistinguishable at the constant.
    """
    assert executed() == _renamed_to_legacy(FROZEN[key])


def test_display_coverage_composes_both_stores_and_filters_each_leg() -> None:
    """Reader #1, C4 delivered: the static two-store composition (I7).

    This is a DECLARED task 7.3 delta against the snapshot and not a byte pin any
    more — the statement is now two rendered variants composed at import time,
    which is exactly the change C4 deferred to this task. What is asserted
    instead is the property the composition exists for:

    * both physical relations appear, so no forcing version written on either
      side of 000061 is dark (must-preserve M2);
    * each leg is restricted to the versions ``timeseries_store`` routes to it,
      so materialising one version in both tables counts it once — the spec's own
      acceptance scenario (:36) does exactly that, and an unfiltered
      ``UNION ALL`` would double every coverage count it produces;
    * the composition sits INSIDE ``station_sample_rows``, ahead of the
      ``station_identity_coverage`` aggregate that consumes it (I7).
    """
    sql = display_coverage._REFRESH_SQL
    assert sql != FROZEN["display_coverage.refresh_statement"], "7.3 changes this statement by design"
    assert FORCING_TABLE_LEGACY in sql
    assert f"FROM {FORCING_TABLE} fst" in sql
    for store in (FORCING_STORE_LEGACY, FORCING_STORE_NARROW):
        assert (
            f"SELECT forcing_version_id FROM met.forcing_version\n                WHERE timeseries_store = '{store}'"
            in sql
        ), store
    union_at = sql.index("UNION ALL")
    assert sql.index("station_identity_coverage") > union_at, "composition must precede the aggregate (I7)"


@pytest.mark.parametrize("store", list(FORCING_STORES))
def test_latest_product_fallback_follows_the_candidate_store(store: str) -> None:
    """Reader #2: one candidate, one store, one rendered variant — no composition.

    ``QHH_LATEST_SEARCH_LIMIT`` is 1, so the header pins exactly one candidate and
    therefore exactly one forcing version, which lives in exactly one table. That
    makes this a routing site, and composing here would scan both tables on every
    request for an answer that can only come from one.

    The legacy branch is still checked against the snapshot (modulo the rename),
    so routing did not quietly rewrite the statement it used to emit.
    """
    heavy_sql = _executed_latest_product(store)
    if store == FORCING_STORE_LEGACY:
        # WHOLE-STATEMENT equality, kept: this is the longest composed statement
        # on the read path and the no-regression claim for reader #2 rests
        # entirely on it. The one declared delta -- the candidate_runs CTE gained
        # `fv.timeseries_store AS forcing_timeseries_store`, which is how the
        # header learns which variant to ask for -- is expressed by APPLYING it
        # to the snapshot and comparing SEQUENCES, not by comparing line
        # membership. Membership is insensitive to order and to multiplicity, so
        # a predicate moved between CTEs, or a line duplicated or lost where the
        # same text occurs twice, would pass it; sequence equality sees all
        # three. Each delta is a single contiguous insert
        # (`packages/common/forecast_store.py:717-722` and #2517's pass-through
        # in the final SELECT), which is what makes the strong form available at
        # all.
        frozen = _renamed_to_legacy(FROZEN["forecast_store.latest_product_statement"])
        assert frozen.count(_LATEST_PRODUCT_STORE_ANCHOR) == 1, "the insertion point must be unambiguous"
        assert frozen.count(_LATEST_PRODUCT_ROW_STORE_ANCHOR) == 1, "the insertion point must be unambiguous"
        expected = frozen.replace(
            _LATEST_PRODUCT_STORE_ANCHOR,
            _LATEST_PRODUCT_STORE_ANCHOR + _LATEST_PRODUCT_STORE_PROJECTION,
            1,
        ).replace(
            _LATEST_PRODUCT_ROW_STORE_ANCHOR,
            _LATEST_PRODUCT_ROW_STORE_ANCHOR + _LATEST_PRODUCT_ROW_STORE_PROJECTION,
            1,
        )
        assert heavy_sql == expected
        assert "fst.forcing_version_key" not in heavy_sql
        return
    assert FORCING_TABLE_LEGACY not in heavy_sql
    assert f"FROM {FORCING_TABLE} fst" in heavy_sql
    assert "fst.forcing_version_key" in heavy_sql
    assert "fst.variable_e" in heavy_sql


@pytest.mark.parametrize("store", list(FORCING_STORES))
def test_latest_product_index_evidence_names_the_relation_the_leg_scanned(store: str) -> None:
    """#2517: the diagnostic routes with the SQL, or it is a false public claim.

    ``/api/v1/mvp/qhh/latest-product`` publishes ``quality.query_indexes``, and
    until #2517 the forcing entry was a parameterless constant while the leg it
    described was routed at ``forecast_store.py:2088``. After 000061 that made
    every response wrong on both routes at once — narrow named an index that
    lives on the legacy relation, legacy named a relation its SQL had stopped
    reading.

    The oracle is the EXECUTED statement, not a second copy of the payload's
    literals: the relation the rendered station leg scans is extracted from the
    heavy statement and compared with the relation the payload names. A pin that
    restated the literals is what shipped the defect.
    """
    heavy_sql = _executed_latest_product(store)
    scanned = set(re.findall(r"FROM (met\.forcing_station_timeseries(?:_legacy)?) fst", heavy_sql))
    assert len(scanned) == 1, scanned

    entries = [
        entry
        for entry in forecast_store._qhh_latest_query_indexes(store)
        if entry["table"].startswith("met.forcing_station_timeseries")
    ]
    assert [entry["table"] for entry in entries] == list(scanned)
    assert entries == [forecast_store._QHH_LATEST_FORCING_QUERY_INDEX_BY_STORE[store]]
    # Copied, not aliased: these dicts are serialized into a public response and
    # a caller mutating one must not rewrite the next request's evidence.
    assert entries[0] is not forecast_store._QHH_LATEST_FORCING_QUERY_INDEX_BY_STORE[store]
    assert entries[0]["columns"] is not forecast_store._QHH_LATEST_FORCING_QUERY_INDEX_BY_STORE[store]["columns"]
    # The other route's relation must not appear anywhere in the evidence.
    other = FORCING_TABLE if store == FORCING_STORE_LEGACY else FORCING_TABLE_LEGACY
    assert other not in {entry["table"] for entry in forecast_store._qhh_latest_query_indexes(store)}


@pytest.mark.parametrize("store", list(FORCING_STORES))
def test_station_forcing_readiness_index_evidence_names_the_relation_it_scanned(store: str) -> None:
    """The internal sibling of the pin above, routed off the same column.

    ``station_forcing_readiness`` has no HTTP route, so this payload is not
    published — but it made the identical claim about the identical catalog and
    was wrong in the identical way, so it is pinned the identical way.
    """
    statements = [
        _executed_readiness_overall(store)[0],
        _executed_readiness_variable_rows(store)[0],
    ]
    scanned = {
        match
        for statement in statements
        for match in re.findall(r"FROM (met\.forcing_station_timeseries(?:_legacy)?)\b", statement)
    }
    assert len(scanned) == 1, scanned

    entry = forecast_store._routed_query_index(
        forecast_store._STATION_FORCING_READINESS_QUERY_INDEX_BY_STORE,
        store,
    )
    assert entry["table"] == scanned.pop()
    assert entry == forecast_store._STATION_FORCING_READINESS_QUERY_INDEX_BY_STORE[store]
    assert entry is not forecast_store._STATION_FORCING_READINESS_QUERY_INDEX_BY_STORE[store]


def test_dynamic_forcing_count_aggregates_over_both_stores_once() -> None:
    """Reader #8, invariant I7 — and the only registered pair whose SHAPE changed.

    Both variants used to be ``SELECT COUNT(*)``. Two counts can only be combined
    by adding two answers in Python, which is the shape I7 forbids ("compose the
    two rendered fact-row subrelations inside the owning reader, before any outer
    aggregate"). They now project rows and this reader wraps ONE ``COUNT(*)``
    around the composition.

    That is a declared task 7.3 delta against the snapshot: the registered text
    changed, deliberately, and is re-pinned here.
    """
    statement, parameters = _executed_dynamic_forcing_count()
    assert statement != FROZEN["qhh_production_bootstrap.dynamic_forcing_count"]
    assert statement.count("SELECT COUNT(*)") == 1
    assert statement.count("SELECT 1 AS present") == 2
    assert statement.count("UNION ALL") == 1
    assert FORCING_TABLE_LEGACY in statement
    assert f"FROM {FORCING_TABLE} fst" in statement
    # One bind per leg, in FORCING_STORES order.
    assert statement.count("%s") == len(parameters) == 2
    assert parameters == ("basins_qhh_shud", "basins_qhh_shud")


def test_the_smoke_reset_deletes_from_both_stores_under_their_own_keys() -> None:
    """Reader #9's two-group split, and the receipt that follows it.

    One rendered ``DELETE`` per physical table, each recorded under the relation
    it touched. A single ``DELETE`` would leave the other table's rows behind and
    report a count for a table it never opened; both tables hold live rows until
    task 8.2 drops the legacy one.

    The (store, physical_table) PAIRING is pinned on the loop's own literals,
    which is the successor of the single call site's ``args[2]`` pin this split
    replaced. ``_delete_rendered`` takes the table name only for the receipt, so
    swapping the loop's tuple to ``(FORCING_STORE_LEGACY, FORCING_TABLE)`` would
    still delete the right rows and file the count against a relation it never
    opened -- precisely what this test's name claims to prevent, and something
    the rendered statements below cannot see because they are driven per store
    rather than through the loop.
    """
    source = (REPO_ROOT / "scripts/reset_qhh_smoke_db.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_delete_rendered"
    ]
    assert len(calls) == 1, "the split is a loop over the two stores, so there is one call site"

    loops = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For) and calls[0] in set(ast.walk(node))
    ]
    assert len(loops) == 1, "the one call site must sit inside exactly one loop"
    loop = loops[0]
    assert isinstance(loop.iter, ast.Tuple)
    assert [
        (pair.elts[0].id, pair.elts[1].id)
        for pair in loop.iter.elts
        if isinstance(pair, ast.Tuple)
        and isinstance(pair.elts[0], ast.Name)
        and isinstance(pair.elts[1], ast.Name)
    ] == [
        ("FORCING_STORE_LEGACY", "FORCING_TABLE_LEGACY"),
        ("FORCING_STORE_NARROW", "FORCING_TABLE"),
    ]
    # ...and the call site really consumes those two loop variables: the receipt
    # relation is the loop's table and the rendered variant is the loop's store.
    assert isinstance(loop.target, ast.Tuple)
    store_name, table_name = (element.id for element in loop.target.elts)
    assert isinstance(calls[0].args[2], ast.Name) and calls[0].args[2].id == table_name
    rendered = calls[0].args[3]  # `render_forcing_ts_sql(...).sql`
    assert isinstance(rendered, ast.Attribute) and rendered.attr == "sql"
    render_call = rendered.value
    assert isinstance(render_call, ast.Call) and render_call.func.id == "render_forcing_ts_sql"
    assert isinstance(render_call.args[1], ast.Name) and render_call.args[1].id == store_name

    legacy_statement, _ = _executed_forcing_delete(FORCING_STORE_LEGACY)
    narrow_statement, _ = _executed_forcing_delete(FORCING_STORE_NARROW)
    assert legacy_statement == f"DELETE FROM {FORCING_TABLE_LEGACY} WHERE forcing_version_id = ANY(%s)"
    assert narrow_statement.startswith(f"DELETE FROM {FORCING_TABLE} AS fst")
    assert "fst.forcing_version_key IN (" in narrow_statement


# ---------------------------------------------------------------------------
# M1a — reader #3 is the one that changed, and the change is a fold-away
# ---------------------------------------------------------------------------


def test_station_series_statement_is_constant_across_the_optional_bounds() -> None:
    """The whole point of templating reader #3.

    Before this task the two optional bounds appended CONJUNCTS to a Python list,
    so the statement text and the parameter tuple both varied with the request —
    which is why it could not be a registered template. Now the text is fixed and
    only the binds move.
    """
    both = _executed_station_series(from_time=_dt("2026-05-07T01:00:00Z"), to_time=_dt("2026-05-07T03:00:00Z"))
    neither = _executed_station_series(from_time=None, to_time=None)
    only_from = _executed_station_series(from_time=_dt("2026-05-07T01:00:00Z"), to_time=None)

    assert both[0] == neither[0] == only_from[0]
    assert both[0] == render_forcing_ts_sql(
        forecast_store._STATION_SERIES_ROWS_TEMPLATES,
        "legacy",
        entry="forecast_store.station_series_rows",
    ).sql


def test_station_series_binds_every_optional_bound_twice_in_a_fixed_ten() -> None:
    """The tuple pin, at the seam rather than only in the API suite.

    Ten binds, always: the variables array, the four mandatory identity/window
    values, each optional bound TWICE (the guard names it on both sides of the
    ``OR``), and the limit. ``None`` on both sides folds ``NULL IS NULL`` to TRUE
    and the conjunct away, which is what makes the fixed shape a no-op — the same
    device this file's QHH fallback already uses for its ``scan_*`` guards.
    """
    from_time = _dt("2026-05-07T01:00:00Z")
    to_time = _dt("2026-05-07T03:00:00Z")
    statement, parameters = _executed_station_series(from_time=from_time, to_time=to_time)

    assert parameters == (
        ["PRCP", "TEMP"],
        "forc_qhh_gfs_2026050700",
        "qhh_stn_001",
        _dt("2026-05-07T00:00:00Z"),
        _dt("2026-05-08T00:00:00Z"),
        from_time,
        from_time,
        to_time,
        to_time,
        3,
    )
    assert statement.count("%s") == len(parameters)
    assert "AND (%s IS NULL OR fst.valid_time >= %s)" in statement
    assert "AND (%s IS NULL OR fst.valid_time <= %s)" in statement

    _unset_statement, unset_parameters = _executed_station_series(from_time=None, to_time=None)
    assert unset_parameters[5:9] == (None, None, None, None)
    assert len(unset_parameters) == len(parameters)


def test_station_series_predicates_survived_the_templating() -> None:
    """Every conjunct the pre-wiring ``clauses`` list could emit is still emitted.

    Enumerated against the five unconditional clauses and the two conditional
    ones by their pre-wiring text, so "templated" cannot quietly mean "dropped a
    predicate" — the failure this reader's change is most exposed to, and the one
    its payload pins would only catch with the right fixture data.
    """
    statement, _parameters = _executed_station_series(from_time=None, to_time=None)
    for clause in (
        "fst.forcing_version_id = %s",
        "fst.station_id = %s",
        "fst.variable = requested.variable",
        "fst.valid_time >= %s",
        "fst.valid_time <= %s",
    ):
        assert clause in statement, clause
    assert statement.count("ORDER BY fst.valid_time") == 1
    assert "LIMIT %s" in statement


# ---------------------------------------------------------------------------
# Routing — every reader's store comes from the data, none is pinned to a literal
#
# M6 ("no narrow render reaches an executed statement") was I11's property and it
# RETIRES here: 000061 creates the narrow table, so a narrow render is now the
# correct statement for every version 000061 did not classify as legacy. Its
# successor is the opposite claim, and it is the one must-preserve M2 rests on —
# a reader still pinned to `"legacy"` after this task reads a table that stops
# receiving rows, silently, for the fourteen days task 8.2's entry gate requires.
# ---------------------------------------------------------------------------


RENDER_FUNCTION = "render_forcing_ts_sql"


def _is_render_call(node: ast.AST) -> bool:
    """A call to the forcing renderer, in either callee form.

    BOTH forms, because ``store`` is what M6 rests on and a sweep with a
    published bypass proves nothing: an ``ast.Name``-only check made
    ``forcing_ts_render.render_forcing_ts_sql(pair, "narrow")`` invisible, and
    the modules under this sweep are free to import the module rather than the
    function.
    """
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id == RENDER_FUNCTION
    return isinstance(node.func, ast.Attribute) and node.func.attr == RENDER_FUNCTION


def _render_call_stores(source: str) -> list[ast.expr]:
    """The ``store`` expression of every renderer call site, FAIL-CLOSED.

    ``store`` is positional-or-keyword
    (``packages/common/forcing_ts_render.py``), so
    ``render_forcing_ts_sql(pair, store="narrow")`` has ``len(node.args) == 1``.
    The previous ``len(node.args) >= 2`` filter therefore SKIPPED it — not
    flagged, skipped — and ``assert stores`` only noticed for the four modules
    that call the renderer exactly once. ``forecast_store.py`` calls it five
    times and could have lost one silently.

    A call whose store cannot be determined statically (``*args``, ``**kwargs``,
    or no store at all) yields the CALL NODE itself rather than being dropped, so
    it fails the literal-``legacy`` assertion below instead of disappearing from
    it. That keeps this strictly a superset of the sweep it replaces: nothing
    that was checked before is now skipped.
    """
    stores: list[ast.expr] = []
    for node in ast.walk(ast.parse(source)):
        if not _is_render_call(node):
            continue
        assert isinstance(node, ast.Call)
        if len(node.args) >= 2 and not isinstance(node.args[1], ast.Starred):
            stores.append(node.args[1])
            continue
        keyword = next((word.value for word in node.keywords if word.arg == "store"), None)
        stores.append(keyword if keyword is not None else node)
    return stores


@pytest.mark.parametrize("path", WIRED_READER_PATHS)
def test_no_render_call_site_is_pinned_to_a_store_literal(path: str) -> None:
    """The successor to M6's structural half, and the inverse claim (#1991).

    Every ``render_forcing_ts_sql`` call site must take its store from something
    that can vary per forcing version. A hardcoded ``"legacy"`` after task 7.3 is
    a reader that will never see a version written after the expand; a hardcoded
    ``"narrow"`` is a reader that goes dark for every version 000061 classified as
    legacy. Both are must-preserve M2 failures and both are invisible in CI,
    because either statement runs and returns rows — just not all of them.

    A store the sweep cannot resolve statically yields the whole ``ast.Call`` and
    is ACCEPTED here, unlike under M6: routing is exactly the case where the store
    is an expression. What is refused is the shape that cannot route.

    The one licensed literal is a loop over ``FORCING_STORES``, which is how the
    two cross-store readers compose — an ``ast.Name`` at the call site, so it
    needs no exception.
    """
    source = REPO_ROOT.joinpath(*path.split("/")).read_text(encoding="utf-8")
    stores = _render_call_stores(source)
    assert stores, f"{path}: registered as a wired reader but calls no renderer"
    for node in stores:
        assert not isinstance(node, ast.Constant), (
            f"{path}: render_forcing_ts_sql is called with the store literal {node.value!r}. "
            "After task 7.3 the store must come from met.forcing_version.timeseries_store for the "
            "version in scope, or from an iteration over FORCING_STORES for a cross-store reader."
        )


@pytest.mark.parametrize(
    ("label", "call"),
    [
        ("keyword store", 'render_forcing_ts_sql(PAIR, store="narrow")'),
        ("attribute callee", 'forcing_ts_render.render_forcing_ts_sql(PAIR, "narrow")'),
        ("attribute callee, keyword store", 'forcing_ts_render.render_forcing_ts_sql(PAIR, store="narrow")'),
    ],
)
def test_the_render_call_sweep_resolves_the_store_it_used_to_skip(label: str, call: str) -> None:
    """Every spelling that used to be SKIPPED rather than flagged, RESOLVED.

    The sweep's two old filters (``ast.Name`` callee, ``len(args) >= 2``) each
    dropped a call SILENTLY, and a dropped call is indistinguishable from a
    compliant one — ``assert stores`` only notices when a module's EVERY call is
    dropped, which is four of the five wired modules and not the one with five
    call sites. A guard a caller steps around by moving one argument to a keyword
    is not the thing M6 is written against.

    Asserted on the RESOLVED NODE rather than on "does not equal legacy": the
    fail-closed sentinel below also does not equal ``legacy``, so a keyword
    resolution that was broken and always fell through to the sentinel would pass
    a negative assertion while being unable to tell ``store="legacy"`` from
    ``store="narrow"``. This pins that it reads the actual argument.
    """
    stores = _render_call_stores(f"{call}\n")
    assert len(stores) == 1, label
    assert isinstance(stores[0], ast.Constant) and stores[0].value == "narrow", label


@pytest.mark.parametrize(
    ("label", "call"),
    [
        ("store from a name", "render_forcing_ts_sql(PAIR, store)"),
        ("splatted arguments", "render_forcing_ts_sql(*ARGS)"),
        ("splatted positional store", "render_forcing_ts_sql(PAIR, *REST)"),
        ("splatted keywords", "render_forcing_ts_sql(PAIR, **KWARGS)"),
    ],
)
def test_the_render_call_sweep_fails_closed_on_a_store_it_cannot_read(label: str, call: str) -> None:
    """A store no static reader can resolve is REFUSED, not dropped.

    ``store`` from a name was always refused (an ``ast.Name`` is not the literal
    ``legacy``); the three splatted forms are new, and dropping them would be the
    same silent skip in a new spelling. The whole ``ast.Call`` is what the sweep
    yields, so the M6 assertion fails and its message says the store could not be
    determined.
    """
    stores = _render_call_stores(f"{call}\n")
    assert len(stores) == 1, label
    assert not (isinstance(stores[0], ast.Constant) and stores[0].value == "legacy"), label
    if "*" in call:
        assert isinstance(stores[0], ast.Call), label


@pytest.mark.parametrize(
    "call",
    [
        'render_forcing_ts_sql(PAIR, "legacy", entry="probe")',
        'render_forcing_ts_sql(PAIR, store="legacy", entry="probe")',
        'forcing_ts_render.render_forcing_ts_sql(PAIR, store="legacy")',
    ],
)
def test_the_render_call_sweep_still_accepts_the_compliant_form(call: str) -> None:
    """The other direction: the superset must not have become a blanket refusal.

    The keyword and attribute spellings are here too, and that is the half that
    matters — a resolver that refused everything it could not read positionally
    would pass every negative test above while making the compliant keyword form
    unwritable.
    """
    stores = _render_call_stores(f"{call}\n")
    assert len(stores) == 1
    assert isinstance(stores[0], ast.Constant) and stores[0].value == "legacy"


#: Identifiers that exist ONLY on the narrow forcing table. Scoped to the forcing
#: fact alias where the token is ambiguous: reader #2's statement legitimately
#: carries ``rt.variable_e``, ``run_key`` and ``basin_version_key`` from the RIVER
#: leg, which routes per store and is none of this task's business.
NARROW_ONLY_TOKENS = ("forcing_version_key", "station_key", "fst.variable_e", "fst.unit_e", "fst.quality_flag_e")


#: The legacy table's TEXT IDENTITY columns, on the forcing fact alias. A narrow
#: statement that still carries one of these is reading a column 000061's narrow
#: table does not have (invariant I1).
#: Matched with a trailing word boundary, which is load bearing: ``fst.unit``
#: is a prefix of the narrow column ``fst.unit_e`` and a plain substring test
#: would report every narrow readiness statement as carrying a legacy column.
LEGACY_ONLY_TOKENS = (
    "forcing_version_id",
    "station_id",
    "basin_version_id",
    "source_id",
    "variable",
    "unit",
    "quality_flag",
)


def _fact_alias_legacy_columns(sql: str) -> set[str]:
    """Legacy text identity columns read off the forcing fact alias in ``sql``."""
    return {column for column in _fact_alias_columns(sql) if column in LEGACY_ONLY_TOKENS}


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("forecast_store.latest_product", lambda store: _executed_latest_product(store)),
        ("forecast_store.station_forcing_membership", lambda store: _executed_membership(store)[0]),
        (
            "forecast_store.station_series_rows",
            lambda store: _executed_station_series(from_time=None, to_time=None, store=store)[0],
        ),
        ("forecast_store.forcing_readiness_overall", lambda store: _executed_readiness_overall(store)[0]),
        (
            "forecast_store.forcing_readiness_variable_rows",
            lambda store: _executed_readiness_variable_rows(store)[0],
        ),
        ("reset_qhh_smoke_db.forcing_timeseries_delete", lambda store: _executed_forcing_delete(store)[0]),
        (
            "best_available.forcing_inputs",
            lambda store: render_forcing_ts_sql(best_available._FORCING_INPUTS_TEMPLATES, store).sql,
        ),
    ],
)
@pytest.mark.parametrize("store", list(FORCING_STORES))
def test_a_routed_statement_names_its_own_store_and_only_its_own_columns(label, statement, store) -> None:
    """The successor to M6's executed half: routing reaches the CURSOR, per store.

    Driven through the reader for each store in turn, so this sees what a caller
    actually hands a cursor rather than what a template says. Two directions,
    because each catches a different mis-wiring:

    * a ``narrow`` route that still emits a text identity column is reading a
      column the narrow table does not have — the statement fails at run time and
      only at run time;
    * a ``legacy`` route that emits a key column is the mirror, and it is the
      failure mode that used to be impossible only because the store was a
      literal.

    Each statement must also name the relation its store resolves to, which is
    what makes the two constants' divergence observable at the call site rather
    than only at the renderer.
    """
    sql = statement(store)
    if store == FORCING_STORE_NARROW:
        leaked = _fact_alias_legacy_columns(sql)
        assert not leaked, f"{label}/narrow: text identity column(s) {sorted(leaked)} on the fact alias"
    else:
        for token in NARROW_ONLY_TOKENS:
            assert token not in sql, f"{label}/legacy: {token!r} has no place in a legacy statement"
    assert "forcing_station_timeseries" in sql
    if store == FORCING_STORE_LEGACY:
        assert FORCING_TABLE_LEGACY in sql
    else:
        assert FORCING_TABLE_LEGACY not in sql


def test_the_two_table_constants_diverged_at_the_migration_that_made_it_true() -> None:
    """D1 discharged (#1991, must-preserve M1).

    The flip and ``db/migrations/000061_forcing_station_timeseries_narrow_expand.sql``
    are in the same commit, so master never carries a read path naming a relation
    that does not exist. Pinned as an EQUALITY on both names rather than as
    ``!=``: the failure this guards is not "they are the same", it is "one of them
    is something else", and the legacy name must be exactly the canonical name
    plus the suffix ``ALTER TABLE … RENAME TO`` produced.
    """
    assert FORCING_TABLE == "met.forcing_station_timeseries"
    assert FORCING_TABLE_LEGACY == "met.forcing_station_timeseries_legacy"
    assert FORCING_TABLE_LEGACY == f"{FORCING_TABLE}_legacy"


def test_the_import_time_scan_header_still_names_no_fact_table() -> None:
    """``display_coverage._SCAN_HEADER_SQL`` is the prefetch, not a fact read.

    Carried over from M6's executed half, where it was in the sweep because it is
    built from the same import-time chain as the refresh statement. It survives
    task 7.3 unchanged and must: it exists to bind the scan scalars cheaply, so a
    composition leaking into it would put both hypertables in the prefetch.
    """
    assert "forcing_station_timeseries" not in display_coverage._SCAN_HEADER_SQL
    assert "river_timeseries" not in display_coverage._SCAN_HEADER_SQL


# ---------------------------------------------------------------------------
# I1–I5 — the narrow variants' shape, the only oracle they will have until 7.3
# ---------------------------------------------------------------------------


def _fact_alias_columns(sql: str) -> set[str]:
    return set(re.findall(r"\bfst\.([a-z_][a-z0-9_]*)", sql, flags=re.IGNORECASE))


def _fact_projection(sql: str) -> list[str]:
    """Output names of the SELECT list that feeds the fact-table scan.

    The last ``SELECT`` before the fact table's ``FROM`` — which is the innermost
    one over the fact rows in every registered template, including reader #3's
    LATERAL subquery and reader #7's ``DISTINCT ON`` list.

    Located by :data:`FORCING_TABLE`, not ``FORCING_TABLE_LEGACY``: this helper
    runs over the *narrow* render too, and 7.3 flips the legacy constant to
    ``…_legacy`` while the narrow render keeps the base name. ``FORCING_TABLE``
    is a prefix of both physical names, before and after that flip.
    """
    table = sql.index(FORCING_TABLE)
    head = sql[:table]
    select_at = head.upper().rindex("SELECT")
    from_at = head.upper().rindex("FROM")
    body = head[select_at + len("SELECT") : from_at]

    items: list[str] = []
    depth = 0
    current: list[str] = []
    for character in body:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            items.append("".join(current))
            current = []
            continue
        current.append(character)
    items.append("".join(current))

    names: list[str] = []
    for item in items:
        tokens = item.replace("\n", " ").split()
        if not tokens:
            continue
        name = tokens[-1]
        names.append(name.rsplit(".", 1)[-1].strip('"'))
    return names


@pytest.mark.parametrize("entry", FORCING_REGISTRY, ids=lambda entry: entry.key)
def test_i1_i2_the_narrow_variant_touches_only_narrow_fact_columns(entry) -> None:
    """I1 and I2 in one subset check, over the fact alias.

    Every text identity column the legacy fact row carries — ``station_id``,
    ``forcing_version_id``, ``variable``, ``unit``, ``quality_flag``,
    ``source_id``, ``basin_version_id`` — is absent from
    :data:`NARROW_FACT_COLUMNS`, so a subset assertion is a stronger statement
    than a per-column denylist AND also catches a column that simply does not
    exist on either table. The legacy half is asserted in the same shape for the
    same reason: a typo there ships.
    """
    narrow = render_forcing_ts_sql(entry.source("narrow"), "narrow", entry=entry.key).sql
    referenced = _fact_alias_columns(narrow)
    assert referenced, f"{entry.key}: the narrow variant aliases the fact table but reads nothing from it"
    assert referenced <= NARROW_FACT_COLUMNS, (
        f"{entry.key}: narrow variant reads {sorted(referenced - NARROW_FACT_COLUMNS)} off the fact row; "
        "the narrow table has keys and enums only, and text identity comes from the authority joins"
    )


@pytest.mark.parametrize("entry", FORCING_REGISTRY, ids=lambda entry: entry.key)
def test_i3_i4_text_identity_comes_from_the_authority_joins(entry) -> None:
    """I3 and I4: ``source_id`` from ``met.forcing_version``, ``basin_version_id`` from ``met.met_station``.

    I4 is the SEMANTIC CHANGE this epic carries: today the fact row stores its own
    ``basin_version_id`` with no FK to ``met.met_station``
    (``000005_met.sql:101``), so the two can diverge, and the narrow variant
    deliberately believes the station authority. Proving that at the ROW level
    needs both tables and is task 7.3's (fixture C1); here it is pinned in the
    text.

    Stated as a RELATION BETWEEN THE TWO VARIANTS rather than as "the narrow
    variant must join both authorities": two of the nine legitimately need
    neither identity, because they replaced a ``COUNT(DISTINCT station_id)`` and
    a ``ms.station_id = fst.station_id`` join with the surrogate key, which is
    1:1 with it. Demanding an unused join there would be demanding a slower
    query for a nicer-looking assertion.
    """
    legacy = render_forcing_ts_sql(entry.source("legacy"), "legacy", entry=entry.key).sql
    narrow = render_forcing_ts_sql(entry.source("narrow"), "narrow", entry=entry.key).sql

    stray = re.findall(
        r"\bfst\.(source_id|basin_version_id|station_id|forcing_version_id|variable|unit|quality_flag)\b",
        narrow,
    )
    assert not stray, f"{entry.key}: narrow variant reads text identity {stray} off the fact row"

    # An alias is only an authority if the join that binds it to the fact row's
    # surrogate key is there. Without this the two `if` bodies below could be
    # satisfied by any relation that happened to be called `fv`.
    if re.search(r"\bfv\.", narrow):
        assert "JOIN met.forcing_version fv" in narrow, entry.key
        assert "fv.forcing_version_key = fst.forcing_version_key" in narrow, entry.key
    if re.search(r"\bms\.", narrow):
        assert "JOIN met.met_station ms" in narrow, entry.key
        assert "ms.station_key = fst.station_key" in narrow, entry.key

    if re.search(r"\bfst\.source_id\b", legacy):
        assert re.search(r"\bfv\.source_id\b", narrow), f"{entry.key}: I3 — source_id lost its authority"
    if re.search(r"\bfst\.basin_version_id\b", legacy):
        assert re.search(r"\bms\.basin_version_id\b", narrow), f"{entry.key}: I4 — basin lost the station authority"


def test_i3_and_i4_are_exercised_by_at_least_one_reader_each() -> None:
    """The two branches above are conditional; this is what stops both going dark.

    A refactor that dropped ``source_id`` from every legacy variant would leave
    the parametrised test green over nine skipped branches, and I3 would be
    "covered" by nothing at all.
    """
    with_source, with_basin = [], []
    for entry in FORCING_REGISTRY:
        legacy = render_forcing_ts_sql(entry.source("legacy"), "legacy", entry=entry.key).sql
        if re.search(r"\bfst\.source_id\b", legacy):
            with_source.append(entry.key)
        if re.search(r"\bfst\.basin_version_id\b", legacy):
            with_basin.append(entry.key)
    assert with_source, "no registered legacy variant reads source_id off the fact row"
    assert with_basin, "no registered legacy variant reads basin_version_id off the fact row"


def test_i3_the_qhh_fallback_narrow_variant_matches_the_spec_scenario() -> None:
    """The spec's named scenario "QHH fallback narrow variant shape", verbatim.

    "The fact-table predicates are ``forcing_version_key``, ``station_key``,
    ``variable_e`` only and ``LOWER(source_id)`` is applied to the joined
    ``met.forcing_version`` row." Asserted on the one reader the scenario names
    rather than folded into the parametrised sweep, because a scenario nobody can
    point at is a scenario nobody re-reads.
    """
    entry = entry_by_key("forecast_store.latest_product_station_source")
    narrow = render_forcing_ts_sql(entry.source("narrow"), "narrow", entry=entry.key).sql

    assert "LOWER(fv.source_id)" in narrow
    assert "LOWER(fst.source_id)" not in narrow
    predicates = _fact_alias_columns(narrow) - {"valid_time", "value", "unit_e", "quality_flag_e"}
    assert predicates <= {"forcing_version_key", "station_key", "variable_e"}
    assert "fst.variable_e = ANY(%(variables)s::met.forcing_variable[])" in narrow


@pytest.mark.parametrize("entry", FORCING_REGISTRY, ids=lambda entry: entry.key)
def test_i5_both_variants_project_the_same_names_in_the_same_order(entry) -> None:
    """I5, first half. A renamed or reordered column is a silently wrong payload.

    ``station_series()`` and the coverage refresh both consume these by NAME from
    a ``RealDictCursor``, so a narrow variant that projected ``variable_e``
    instead of ``variable`` would not fail — it would return rows missing a key,
    at 7.3, in production.
    """
    if entry.kind == "dml":
        pytest.skip("a DELETE projects nothing")
    legacy = render_forcing_ts_sql(entry.source("legacy"), "legacy", entry=entry.key).sql
    narrow = render_forcing_ts_sql(entry.source("narrow"), "narrow", entry=entry.key).sql
    assert _fact_projection(legacy) == _fact_projection(narrow)


@pytest.mark.parametrize("entry", FORCING_REGISTRY, ids=lambda entry: entry.key)
def test_i5_every_text_function_over_an_enum_carries_an_explicit_cast(entry) -> None:
    """I5, second half — the BTRIM trap, which does not compile rather than mislead.

    ``_fetch_forcing_readiness_variable_rows`` does ``BTRIM(unit)`` and
    ``BTRIM(quality_flag)`` over columns that are ENUMS on the narrow side.
    ``BTRIM(unit_e)`` is a type error; ``BTRIM(unit_e::text)`` is not. The same
    applies to ``ORDER BY``: an enum sorts by DECLARATION order, so ordering on
    the raw enum silently reorders a response array that legacy ordered
    alphabetically.
    """
    narrow = render_forcing_ts_sql(entry.source("narrow"), "narrow", entry=entry.key).sql
    for column in NARROW_ENUM_COLUMNS:
        uncast = re.findall(
            rf"(?:BTRIM|LOWER|UPPER|TRIM|LENGTH)\(\s*fst\.{column}(?!::text)",
            narrow,
            flags=re.IGNORECASE,
        )
        assert not uncast, f"{entry.key}: text function over fst.{column} without ::text"
        for order_by in re.findall(r"ORDER BY[^;]*", narrow, flags=re.IGNORECASE | re.DOTALL):
            # Word-boundary, not line-end: ``ORDER BY fst.variable_e, fst.valid_time``
            # is the same trap and a ``\n`` anchor would walk straight past it.
            assert not re.search(rf"\bfst\.{column}\b(?!::text)", order_by, flags=re.IGNORECASE), (
                f"{entry.key}: ORDER BY on the raw enum fst.{column}"
            )


@pytest.mark.parametrize("entry", FORCING_REGISTRY, ids=lambda entry: entry.key)
@pytest.mark.parametrize("store", FORCING_STORES)
def test_every_registered_pair_renders_and_declares_its_parameter_style(entry, store) -> None:
    """The register's ``params`` field is a claim about BOTH variants.

    Nothing computes it — the two texts are authored separately — so a narrow
    variant written with named placeholders beside a positional legacy one would
    render fine and fail only when task 7.3 binds it.
    """
    rendered = render_forcing_ts_sql(entry.source(store), store, entry=entry.key)
    assert rendered.store == store
    if entry.params == "positional":
        assert "%(" not in rendered.sql
        assert "%s" in rendered.sql
    else:
        assert "%(" in rendered.sql
