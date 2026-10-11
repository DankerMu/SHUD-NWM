"""The nine wired forcing readers: what they execute, and its narrow shape.

Written for #1990 task 7.2 (wiring), rewritten for #1991 task 7.3 (per-version
routing between two stores) and **rewritten again for #1993's first cut**, which
removed the legacy templates and every read of the routing column. Nothing in
this module executes SQL against a database; it drives the readers with
recording cursors and reads what they hand them.

1. **One store.** Every ``render_forcing_ts_sql`` call site passes the literal
   ``"narrow"`` — asserted structurally (by AST, over every registered reader's
   file) and on the executed text, which must name the canonical table and no
   text identity column of the pre-expand fact row.

2. **#2511 — the scan restriction stays on the hypertable.** The QHH fallback
   and the display-coverage station leg bind ``fst.forcing_version_key`` next to
   the ``fv.forcing_version_id`` guard, so the fact table keeps a literal
   predicate of its own.

3. **I1–I5 — the registered templates' shape.** A text oracle over the register.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from packages.common import best_available, display_coverage, forecast_store
from packages.common.forcing_ts_render import FORCING_TABLE, render_forcing_ts_sql
from scripts import reset_qhh_smoke_db
from tests.forcing_ts_template_registry import FORCING_REGISTRY, REPO_ROOT, entry_by_key

# AT MODULE SCOPE ON PURPOSE, and the scope is the whole point rather than a
# style preference. Reader #2 is driven through four private helpers of
# ``tests/test_qhh_latest_fallback_pushdown.py``, which is a member of river's
# ``SQL_SHAPE_ORACLE_TESTS``. ``select_ci_tests._build_suite_importer_index``
# inverts MODULE-LEVEL import edges only, so while this import sat inside
# ``_executed_latest_product`` a PR renaming ``_fallback_statements`` over there
# did not select this suite and went red on the post-merge master run instead.
# Hoisting it is what makes the dependency visible to the selector; it is also
# the shape that file already uses for its own cross-suite helper import
# (``from tests.test_sql_shape_helpers import outer_predicates``).
from tests.test_qhh_latest_fallback_pushdown import (
    _HEADER_ROW,
    BindingCheckedCursor,
    _fallback_statements,
    _run_fallback,
)
from workers.model_registry import qhh_production_bootstrap

#: Every module that contains a wired forcing reader. Derived from the register
#: rather than listed, so a tenth reader in a sixth file is covered by the AST
#: sweep below the moment it is registered.
WIRED_READER_PATHS: tuple[str, ...] = tuple(sorted({entry.path for entry in FORCING_REGISTRY}))

#: The narrow forcing table's columns (`design.md` D9). The templates may
#: reference these on the fact alias and nothing else — which is invariants I1
#: and I2 in one subset check: every text identity column of the pre-expand
#: table (``station_id``, ``forcing_version_id``, ``variable``, ``unit``,
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
#: an explicit cast, which is the I5 trap: ``BTRIM(unit_e)`` is not legal SQL.
NARROW_ENUM_COLUMNS = ("variable_e", "unit_e", "quality_flag_e")

#: #2511's fold-away scan predicate on the fact table itself, whitespace-folded.
SCAN_FORCING_VERSION_KEY_PREDICATE = (
    "(%(scan_forcing_version_key)s IS NULL OR fst.forcing_version_key = %(scan_forcing_version_key)s)"
)


def _folded(sql: str) -> str:
    return " ".join(sql.split())


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


def _executed_membership() -> tuple[str, Any]:
    cursor = RecordingCursor()
    forecast = forecast_store.PsycopgForecastStore("postgresql://test")
    with pytest.raises(forecast_store.ForecastStoreError):
        forecast._validate_station_forcing_membership(
            cursor,
            station_id="qhh_stn_001",
            forcing_version={"forcing_version_id": "forc_qhh_gfs_2026050700"},
            valid_time_start=_dt("2026-05-07T00:00:00Z"),
            valid_time_end=_dt("2026-05-08T00:00:00Z"),
        )
    return cursor.statements[-1]


def _executed_readiness_overall() -> tuple[str, Any]:
    cursor = RecordingCursor()
    forecast = forecast_store.PsycopgForecastStore("postgresql://test")
    forecast._fetch_forcing_readiness_overall(
        cursor,
        forcing_version_id="forc_qhh_gfs_2026050700",
        valid_time_start=_dt("2026-05-07T00:00:00Z"),
        valid_time_end=_dt("2026-05-08T00:00:00Z"),
        variables=["PRCP"],
    )
    return cursor.statements[-1]


def _executed_readiness_variable_rows() -> tuple[str, Any]:
    cursor = RecordingCursor()
    forecast = forecast_store.PsycopgForecastStore("postgresql://test")
    forecast._fetch_forcing_readiness_variable_rows(
        cursor,
        forcing_version_id="forc_qhh_gfs_2026050700",
        valid_time_start=_dt("2026-05-07T00:00:00Z"),
        valid_time_end=_dt("2026-05-08T00:00:00Z"),
        variables=["PRCP"],
    )
    return cursor.statements[-1]


def _executed_station_series(*, from_time: datetime | None, to_time: datetime | None) -> tuple[str, Any]:
    cursor = RecordingCursor()
    forecast = forecast_store.PsycopgForecastStore("postgresql://test")
    forecast._fetch_station_series_rows(
        cursor,
        station_id="qhh_stn_001",
        forcing_version_id="forc_qhh_gfs_2026050700",
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


def _rendered_forcing_delete() -> str:
    return render_forcing_ts_sql(
        reset_qhh_smoke_db._FORCING_TIMESERIES_DELETE_TEMPLATES,
        "narrow",
        entry="reset_qhh_smoke_db.forcing_timeseries_delete",
    ).sql


def _executed_latest_product() -> str:
    cursor = BindingCheckedCursor(header_rows=[dict(_HEADER_ROW)])
    _run_fallback(cursor)
    _header_sql, heavy_sql = _fallback_statements(cursor)
    return heavy_sql


def _station_cte(sql: str) -> str:
    return sql[sql.index("station_sample_rows AS") : sql.index("station_identity_coverage AS")]


# ---------------------------------------------------------------------------
# The two readers whose composed statement changed shape at #1993, and #2511
# ---------------------------------------------------------------------------


def test_display_coverage_reads_one_station_leg_with_no_store_filter() -> None:
    """Reader #1 after #1993: one rendered leg, spliced in bare.

    The expand window composed two rendered variants with ``UNION ALL`` and
    filtered each on ``met.forcing_version``'s store column. Both are gone: the
    station CTE is the narrow template and nothing else, so it reads the fact
    table once and never mentions the routing column.
    """
    station = _station_cte(display_coverage._REFRESH_SQL)
    assert re.findall(r"FROM (met\.forcing_station_timeseries\w*)", station) == [FORCING_TABLE]
    assert "UNION ALL" not in station
    assert "timeseries_store" not in display_coverage._REFRESH_SQL
    assert "_store_rows" not in station
    assert display_coverage._STATION_SAMPLE_ROWS_NARROW_SQL in station


def test_display_coverage_station_leg_keeps_the_version_predicate_on_the_fact_table() -> None:
    """#2511 (a), on the coverage refresh: ``fst.forcing_version_key`` is bound.

    Deleting the legacy leg left the version restriction on
    ``fv.forcing_version_id`` only — a predicate on the JOINED row, which the
    planner has to push through the join to exclude a single chunk. The 633 s
    precedent is a scan with no literal predicate on the hypertable, so the
    restriction is repeated on the fact table itself, in the same fold-away form
    as its neighbour.
    """
    station = _folded(_station_cte(display_coverage._REFRESH_SQL))
    assert SCAN_FORCING_VERSION_KEY_PREDICATE in station
    assert "scan_forcing_version_key" in display_coverage._SCAN_PARAM_KEYS
    assert "forcing_version_key," in _folded(display_coverage._SCAN_HEADER_SQL)


def test_latest_product_fallback_reads_the_narrow_station_leg() -> None:
    """Reader #2: one candidate, one rendered template, the canonical table."""
    heavy_sql = _executed_latest_product()
    station = _station_cte(heavy_sql)
    assert re.findall(r"FROM (met\.forcing_station_timeseries\w*)", heavy_sql) == [FORCING_TABLE]
    assert "fst.variable_e" in station
    assert "timeseries_store" not in heavy_sql


def test_latest_product_station_leg_keeps_the_version_predicate_on_the_fact_table() -> None:
    """#2511 (a), on the QHH fallback: bound from the header's surrogate key.

    Asserted on the EXECUTED heavy statement, inside the station CTE, and on the
    parameters the cursor was handed — ``BindingCheckedCursor`` refuses an
    unbound named placeholder, so reaching here already proves the key is bound.
    """
    cursor = BindingCheckedCursor(header_rows=[dict(_HEADER_ROW)])
    _run_fallback(cursor)
    (header_sql, _header_params), (heavy_sql, heavy_params) = [
        (statement, params) for statement, params in cursor.executed if "to_regclass(" not in statement
    ]
    assert SCAN_FORCING_VERSION_KEY_PREDICATE in _folded(_station_cte(heavy_sql))
    assert heavy_params["scan_forcing_version_key"] == _HEADER_ROW["forcing_version_key"]
    assert "forcing_version_key," in _folded(header_sql)
    assert "fv.forcing_version_key," in header_sql


def test_latest_product_index_evidence_names_the_relation_the_leg_scanned() -> None:
    """#2517: the diagnostic describes the SQL, or it is a false public claim.

    ``/api/v1/mvp/qhh/latest-product`` publishes ``quality.query_indexes``. The
    oracle is the EXECUTED statement, not a second copy of the payload's
    literals: the relation the rendered station leg scans is extracted from the
    heavy statement and compared with the relation the payload names.
    """
    heavy_sql = _executed_latest_product()
    scanned = set(re.findall(r"FROM (met\.forcing_station_timeseries\w*) fst", heavy_sql))
    assert scanned == {FORCING_TABLE}

    entries = [
        entry
        for entry in forecast_store._qhh_latest_query_indexes()
        if entry["table"].startswith("met.forcing_station_timeseries")
    ]
    assert [entry["table"] for entry in entries] == list(scanned)
    assert entries == [forecast_store._QHH_LATEST_FORCING_QUERY_INDEX]
    # Copied, not aliased: these dicts are serialized into a public response and
    # a caller mutating one must not rewrite the next request's evidence.
    assert entries[0] is not forecast_store._QHH_LATEST_FORCING_QUERY_INDEX
    assert entries[0]["columns"] is not forecast_store._QHH_LATEST_FORCING_QUERY_INDEX["columns"]


def test_station_forcing_readiness_index_evidence_names_the_relation_it_scanned() -> None:
    """The internal sibling of the pin above.

    ``station_forcing_readiness`` has no HTTP route, so this payload is not
    published — but it makes the identical claim about the identical catalog,
    so it is pinned the identical way.
    """
    statements = [_executed_readiness_overall()[0], _executed_readiness_variable_rows()[0]]
    scanned = {
        match
        for statement in statements
        for match in re.findall(r"FROM (met\.forcing_station_timeseries\w*)", statement)
    }
    assert scanned == {FORCING_TABLE}

    entry = forecast_store._query_index_copy(forecast_store._STATION_FORCING_READINESS_QUERY_INDEX)
    assert entry["table"] == FORCING_TABLE
    assert entry == forecast_store._STATION_FORCING_READINESS_QUERY_INDEX
    assert entry is not forecast_store._STATION_FORCING_READINESS_QUERY_INDEX
    assert entry["columns"] is not forecast_store._STATION_FORCING_READINESS_QUERY_INDEX["columns"]


def test_dynamic_forcing_count_is_one_count_over_one_leg() -> None:
    """Reader #8 after #1993: one bound parameter fewer.

    The registered template still projects rows and the reader still wraps ONE
    ``COUNT(*)`` around it; what went is the second leg and its bind.
    """
    statement, parameters = _executed_dynamic_forcing_count()
    assert statement.count("SELECT COUNT(*)") == 1
    assert statement.count("SELECT 1 AS present") == 1
    assert "UNION ALL" not in statement
    assert re.findall(r"FROM (met\.forcing_station_timeseries\w*)", statement) == [FORCING_TABLE]
    assert statement.count("%s") == len(parameters) == 1
    assert parameters == ("basins_qhh_shud",)


def test_the_smoke_reset_narrow_delete_is_keyed_on_the_surrogate() -> None:
    """Reader #9's registered statement. The guarded legacy DELETE beside it is
    outside the renderer (fixture ``I14-1993.md`` D5) and is pinned on the
    executed script in ``tests/test_qhh_scripts_static.py``."""
    statement = _rendered_forcing_delete()
    assert statement.startswith(f"DELETE FROM {FORCING_TABLE} AS fst")
    assert "fst.forcing_version_key IN (" in statement
    assert "_legacy" not in statement


# ---------------------------------------------------------------------------
# Reader #3 — templated at 7.2, and the change is a fold-away
# ---------------------------------------------------------------------------


def test_station_series_statement_is_constant_across_the_optional_bounds() -> None:
    """The whole point of templating reader #3.

    Before task 7.2 the two optional bounds appended CONJUNCTS to a Python list,
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
        "narrow",
        entry="forecast_store.station_series_rows",
    ).sql


def test_station_series_binds_every_optional_bound_twice_in_a_fixed_ten() -> None:
    """The tuple pin, at the seam rather than only in the API suite.

    Ten binds, always: the variables array, the four mandatory identity/window
    values, each optional bound TWICE (the guard names it on both sides of the
    ``OR``), and the limit. ``None`` on both sides folds ``NULL IS NULL`` to TRUE
    and the conjunct away, which is what makes the fixed shape a no-op — the same
    device the QHH fallback already uses for its ``scan_*`` guards.
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
    ones, in the narrow spelling of each, so "templated" cannot quietly mean
    "dropped a predicate" — the failure this reader's change is most exposed to,
    and the one its payload pins would only catch with the right fixture data.
    """
    statement, _parameters = _executed_station_series(from_time=None, to_time=None)
    for clause in (
        "fv.forcing_version_id = %s",
        "ms.station_id = %s",
        "fst.variable_e::text = requested.variable",
        "fst.valid_time >= %s",
        "fst.valid_time <= %s",
    ):
        assert clause in statement, clause
    assert statement.count("ORDER BY fst.valid_time") == 1
    assert "LIMIT %s" in statement


# ---------------------------------------------------------------------------
# One store — every render call site passes the literal "narrow"
#
# The expand window's property was the opposite ("no call site may be pinned to
# a store literal"), because one forcing version's rows lived in exactly one of
# two tables. #1993's first cut removed the second table from the read path and
# the routing column from the code, so a store that is NOT the literal is now
# the defect: an expression there is a reader still trying to route.
# ---------------------------------------------------------------------------


RENDER_FUNCTION = "render_forcing_ts_sql"


def _is_render_call(node: ast.AST) -> bool:
    """A call to the forcing renderer, in either callee form.

    BOTH forms, because a sweep with a published bypass proves nothing: an ``ast.Name``-only check made
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
    it fails the literal-``narrow`` assertion below instead of disappearing from
    it.
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
def test_every_render_call_site_passes_the_narrow_literal(path: str) -> None:
    """No reader routes any more (#1993), by AST over every wired reader file."""
    source = REPO_ROOT.joinpath(*path.split("/")).read_text(encoding="utf-8")
    stores = _render_call_stores(source)
    assert stores, f"{path}: registered as a wired reader but calls no renderer"
    for node in stores:
        assert isinstance(node, ast.Constant) and node.value == "narrow", (
            f"{path}: render_forcing_ts_sql must be called with the store literal 'narrow'; "
            f"got {ast.dump(node)[:120]}"
        )


@pytest.mark.parametrize(
    ("label", "call"),
    [
        ("positional store", 'render_forcing_ts_sql(PAIR, "narrow", entry="probe")'),
        ("keyword store", 'render_forcing_ts_sql(PAIR, store="narrow")'),
        ("attribute callee", 'forcing_ts_render.render_forcing_ts_sql(PAIR, "narrow")'),
        ("attribute callee, keyword store", 'forcing_ts_render.render_forcing_ts_sql(PAIR, store="narrow")'),
    ],
)
def test_the_render_call_sweep_resolves_every_spelling_of_the_store(label: str, call: str) -> None:
    """Every callee/argument spelling is RESOLVED to the actual argument.

    An ``ast.Name``-only callee check or a ``len(args) >= 2`` filter each drops
    a call SILENTLY, and a dropped call is indistinguishable from a compliant
    one — ``assert stores`` only notices when a module's EVERY call is dropped.
    """
    stores = _render_call_stores(f"{call}\n")
    assert len(stores) == 1, label
    assert isinstance(stores[0], ast.Constant) and stores[0].value == "narrow", label


@pytest.mark.parametrize(
    ("label", "call"),
    [
        ("store from a name", "render_forcing_ts_sql(PAIR, store)"),
        ("store from a call", "render_forcing_ts_sql(PAIR, forcing_version_store(version))"),
        ("the legacy literal", 'render_forcing_ts_sql(PAIR, "legacy")'),
        ("splatted arguments", "render_forcing_ts_sql(*ARGS)"),
        ("splatted positional store", "render_forcing_ts_sql(PAIR, *REST)"),
        ("splatted keywords", "render_forcing_ts_sql(PAIR, **KWARGS)"),
    ],
)
def test_the_render_call_sweep_refuses_anything_but_the_narrow_literal(label: str, call: str) -> None:
    """A routed, stale or unreadable store is REFUSED, not dropped."""
    stores = _render_call_stores(f"{call}\n")
    assert len(stores) == 1, label
    assert not (isinstance(stores[0], ast.Constant) and stores[0].value == "narrow"), label
    if "*" in call:
        assert isinstance(stores[0], ast.Call), label


#: The pre-expand table's TEXT IDENTITY columns, on the forcing fact alias. A
#: statement that still carries one of these is reading a column the narrow
#: table does not have (invariant I1).
TEXT_IDENTITY_COLUMNS = (
    "forcing_version_id",
    "station_id",
    "basin_version_id",
    "source_id",
    "variable",
    "unit",
    "quality_flag",
)


def _fact_alias_columns(sql: str) -> set[str]:
    return set(re.findall(r"\bfst\.([a-z_][a-z0-9_]*)", sql, flags=re.IGNORECASE))


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("forecast_store.latest_product", _executed_latest_product),
        ("forecast_store.station_forcing_membership", lambda: _executed_membership()[0]),
        (
            "forecast_store.station_series_rows",
            lambda: _executed_station_series(from_time=None, to_time=None)[0],
        ),
        ("forecast_store.forcing_readiness_overall", lambda: _executed_readiness_overall()[0]),
        ("forecast_store.forcing_readiness_variable_rows", lambda: _executed_readiness_variable_rows()[0]),
        ("reset_qhh_smoke_db.forcing_timeseries_delete", _rendered_forcing_delete),
        (
            "best_available.forcing_inputs",
            lambda: render_forcing_ts_sql(best_available._FORCING_INPUTS_TEMPLATES, "narrow").sql,
        ),
        ("qhh_production_bootstrap.dynamic_forcing_count", lambda: _executed_dynamic_forcing_count()[0]),
        ("display_coverage.refresh_statement", lambda: display_coverage._REFRESH_SQL),
    ],
)
def test_an_executed_statement_reads_the_narrow_table_and_only_its_columns(label, statement) -> None:
    """What reaches the CURSOR, for all nine readers.

    Driven through the reader, so this sees what a caller actually hands a
    cursor rather than what a template says: the canonical relation, never the
    renamed legacy one, and no text identity column on the fact alias — a
    statement carrying one fails at run time and only at run time.
    """
    sql = statement()
    leaked = {column for column in _fact_alias_columns(sql) if column in TEXT_IDENTITY_COLUMNS}
    assert not leaked, f"{label}: text identity column(s) {sorted(leaked)} on the fact alias"
    assert set(re.findall(r"met\.forcing_station_timeseries\w*", sql)) == {FORCING_TABLE}, label


def test_the_import_time_scan_header_still_names_no_fact_table() -> None:
    """``display_coverage._SCAN_HEADER_SQL`` is the prefetch, not a fact read.

    It is built from the same import-time chain as the refresh statement and
    exists to bind the scan scalars cheaply, so a fact read leaking into it
    would put a hypertable in the prefetch.
    """
    assert "forcing_station_timeseries" not in display_coverage._SCAN_HEADER_SQL
    assert "river_timeseries" not in display_coverage._SCAN_HEADER_SQL


# ---------------------------------------------------------------------------
# I1–I5 — the registered templates' shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", FORCING_REGISTRY, ids=lambda entry: entry.key)
def test_i1_i2_the_narrow_variant_touches_only_narrow_fact_columns(entry) -> None:
    """I1 and I2 in one subset check, over the fact alias.

    Every text identity column the pre-expand fact row carried — ``station_id``,
    ``forcing_version_id``, ``variable``, ``unit``, ``quality_flag``,
    ``source_id``, ``basin_version_id`` — is absent from
    :data:`NARROW_FACT_COLUMNS`, so a subset assertion is a stronger statement
    than a per-column denylist AND also catches a column that simply does not
    exist.
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

    I4 is the SEMANTIC CHANGE the narrow store carried: the pre-expand fact row
    stored its own ``basin_version_id`` with no FK to ``met.met_station``, so
    the two could diverge, and the narrow template deliberately believes the
    station authority.

    Two of the nine legitimately need neither identity, because they use the
    surrogate key, which is 1:1 with it. Demanding an unused join there would be
    demanding a slower query for a nicer-looking assertion — so the joins are
    required exactly where the authority alias is used.
    """
    narrow = render_forcing_ts_sql(entry.source("narrow"), "narrow", entry=entry.key).sql

    stray = re.findall(
        r"\bfst\.(source_id|basin_version_id|station_id|forcing_version_id|variable|unit|quality_flag)\b",
        narrow,
    )
    assert not stray, f"{entry.key}: template reads text identity {stray} off the fact row"

    # An alias is only an authority if the join that binds it to the fact row's
    # surrogate key is there. Without this the checks could be satisfied by any
    # relation that happened to be called `fv`.
    if re.search(r"\bfv\.", narrow):
        assert "JOIN met.forcing_version fv" in narrow, entry.key
        assert "fv.forcing_version_key = fst.forcing_version_key" in narrow, entry.key
    if re.search(r"\bms\.", narrow):
        assert "JOIN met.met_station ms" in narrow, entry.key
        assert "ms.station_key = fst.station_key" in narrow, entry.key


def test_i3_and_i4_are_exercised_by_at_least_one_reader_each() -> None:
    """The two branches above are conditional; this is what stops both going dark."""
    with_source, with_basin = [], []
    for entry in FORCING_REGISTRY:
        narrow = render_forcing_ts_sql(entry.source("narrow"), "narrow", entry=entry.key).sql
        if re.search(r"\bfv\.source_id\b", narrow):
            with_source.append(entry.key)
        if re.search(r"\bms\.basin_version_id\b", narrow):
            with_basin.append(entry.key)
    assert with_source, "no registered template takes source_id from met.forcing_version"
    assert with_basin, "no registered template takes basin_version_id from met.met_station"


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
def test_i5_every_text_function_over_an_enum_carries_an_explicit_cast(entry) -> None:
    """I5, second half — the BTRIM trap, which does not compile rather than mislead.

    ``_fetch_forcing_readiness_variable_rows`` does ``BTRIM(unit)`` and
    ``BTRIM(quality_flag)`` over columns that are ENUMS on the narrow side.
    ``BTRIM(unit_e)`` is a type error; ``BTRIM(unit_e::text)`` is not. The same
    applies to ``ORDER BY``: an enum sorts by DECLARATION order, so ordering on
    the raw enum silently reorders a response array that has always been
    alphabetical.
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
def test_every_registered_template_renders_and_declares_its_parameter_style(entry) -> None:
    """The register's ``params`` field is a claim nothing computes."""
    rendered = render_forcing_ts_sql(entry.source("narrow"), "narrow", entry=entry.key)
    assert rendered.store == "narrow"
    if entry.params == "positional":
        assert "%(" not in rendered.sql
        assert "%s" in rendered.sql
    else:
        assert "%(" in rendered.sql
