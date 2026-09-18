"""The nine wired forcing readers: byte identity, store constancy, narrow shape (#1990, task 7.2).

Three contracts live here, because they are three halves of one claim — that
wiring every forcing fact-table reader through
``packages.common.forcing_ts_render`` changed nothing that runs, and prepared
something that will:

1. **M1 — byte identity.** Eight of the nine readers emit SQL byte-identical to
   the text they emitted before the wiring. Asserted as STRING EQUALITY against
   ``tests/fixtures/forcing_read_path_pre_wiring_0c92093e7.json``, a snapshot
   captured by executing the readers in a worktree checked out at the PRE-WIRING
   commit — an independent source of truth, not a golden regenerated from the
   code it certifies. Fixture decision D1 is what makes this reachable: both
   table-name constants spell the deployed name today, so the legacy render
   cannot differ by so much as the ``_legacy`` suffix.

   The ninth (``forecast_store.station_series_rows``) is excluded ON PURPOSE and
   is the reason M1a exists — it was not a template before this task, so its text
   and its positional tuple both change. Its pin is the ``station_series()``
   response payload in ``tests/test_forecast_api.py``, plus the tuple assertion
   there, plus the fold-away equivalence pinned below.

2. **M6 — no narrow render reaches an executed statement.** THE PROPERTY THAT
   KEEPS NODE-27 SAFE, and the one the fixture says must not be weakened for
   convenience. ``met.forcing_version.timeseries_store`` does not exist until
   task 7.3, so there is nothing to route on and a narrow render would name
   ``forcing_version_key`` / ``variable_e`` against a table that has neither.
   Import-time scope alone is NOT sufficient: two readers compose at CALL time,
   so an import-clean module can still execute narrow SQL. Both halves are
   asserted — structurally (every ``render_forcing_ts_sql`` call site passes the
   literal ``"legacy"``, by AST) and on the executed text (no narrow-only
   identifier appears in what any reader actually hands a cursor).

3. **I1–I5 — the narrow variants' shape.** The narrow half of every pair is DEAD
   TEXT until task 7.3 creates the table, so a text oracle is the only thing
   standing between "authored carefully" and "authored plausibly". Without these
   assertions task 7.3 inherits nine unpinned templates that have never been
   parsed by anything.

I7 (cross-store composition) is NOT covered here and is not coverable here: it
needs a narrow branch that can run. It is task 7.3's acceptance item. Nothing in
this module executes SQL.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from packages.common import best_available, display_coverage, forecast_store
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


def _frozen() -> dict[str, str]:
    payload = json.loads(PRE_WIRING_FIXTURE.read_text(encoding="utf-8"))
    assert payload["captured_from_commit"] == PRE_WIRING_COMMIT
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


def _executed_membership() -> tuple[str, Any]:
    cursor = RecordingCursor()
    store = forecast_store.PsycopgForecastStore("postgresql://test")
    with pytest.raises(forecast_store.ForecastStoreError):
        store._validate_station_forcing_membership(
            cursor,
            station_id="qhh_stn_001",
            forcing_version={"forcing_version_id": "forc_qhh_gfs_2026050700"},
            valid_time_start=_dt("2026-05-07T00:00:00Z"),
            valid_time_end=_dt("2026-05-08T00:00:00Z"),
        )
    return cursor.statements[-1]


def _executed_readiness_overall() -> tuple[str, Any]:
    cursor = RecordingCursor()
    store = forecast_store.PsycopgForecastStore("postgresql://test")
    store._fetch_forcing_readiness_overall(
        cursor,
        forcing_version_id="forc_qhh_gfs_2026050700",
        valid_time_start=_dt("2026-05-07T00:00:00Z"),
        valid_time_end=_dt("2026-05-08T00:00:00Z"),
        variables=["PRCP"],
    )
    return cursor.statements[-1]


def _executed_readiness_variable_rows() -> tuple[str, Any]:
    cursor = RecordingCursor()
    store = forecast_store.PsycopgForecastStore("postgresql://test")
    store._fetch_forcing_readiness_variable_rows(
        cursor,
        forcing_version_id="forc_qhh_gfs_2026050700",
        valid_time_start=_dt("2026-05-07T00:00:00Z"),
        valid_time_end=_dt("2026-05-08T00:00:00Z"),
        variables=["PRCP"],
    )
    return cursor.statements[-1]


def _executed_station_series(*, from_time: datetime | None, to_time: datetime | None) -> tuple[str, Any]:
    cursor = RecordingCursor()
    store = forecast_store.PsycopgForecastStore("postgresql://test")
    store._fetch_station_series_rows(
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


def _executed_forcing_delete() -> tuple[str, Any]:
    cursor = RecordingCursor()
    deleted: dict[str, int] = {}
    reset_qhh_smoke_db._delete_rendered(
        cursor,
        deleted,
        FORCING_TABLE_LEGACY,
        render_forcing_ts_sql(
            reset_qhh_smoke_db._FORCING_TIMESERIES_DELETE_TEMPLATES,
            "legacy",
            entry="reset_qhh_smoke_db.forcing_timeseries_delete",
        ).sql,
        (["forc_qhh_gfs_2026050700"],),
    )
    assert deleted == {"met.forcing_station_timeseries": 0}
    return cursor.statements[-1]


def _executed_latest_product() -> str:
    cursor = BindingCheckedCursor(header_rows=[dict(_HEADER_ROW)])
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


def test_display_coverage_refresh_statement_is_byte_identical() -> None:
    """Reader #1, pinned on the WHOLE import-time statement, not just the leg.

    The leg is spliced back into a concatenation chain, so an equality on the leg
    alone would pass while the surrounding indentation or the trailing ``),`` had
    drifted. This is also the strongest available evidence for C4: a single
    legacy render, no union, and the statement every process that imports this
    module builds at import time is the statement node-27 has been running.
    """
    assert display_coverage._REFRESH_SQL == FROZEN["display_coverage.refresh_statement"]


def test_latest_product_fallback_statement_is_byte_identical() -> None:
    """Reader #2, pinned on the whole composed statement.

    This one composes at CALL time and interleaves a RIVER render (which routes
    per store) with the forcing leg (which does not), so the splice is the risk
    and whole-statement equality is the only thing that sees it.
    """
    assert _executed_latest_product() == FROZEN["forecast_store.latest_product_statement"]


@pytest.mark.parametrize(
    ("key", "executed"),
    [
        ("forecast_store.station_forcing_membership", lambda: _executed_membership()[0]),
        ("forecast_store.forcing_readiness_overall", lambda: _executed_readiness_overall()[0]),
        (
            "forecast_store.forcing_readiness_variable_rows",
            lambda: _executed_readiness_variable_rows()[0],
        ),
        ("qhh_production_bootstrap.dynamic_forcing_count", lambda: _executed_dynamic_forcing_count()[0]),
        ("reset_qhh_smoke_db.forcing_timeseries_delete", lambda: _executed_forcing_delete()[0]),
    ],
)
def test_executed_statements_are_byte_identical(key: str, executed) -> None:
    """The five readers whose whole executed statement is the render.

    Driven through the reader rather than compared against the template constant:
    a template that renders correctly and a call site that ignores it are
    indistinguishable at the constant.
    """
    assert executed() == FROZEN[key]


def test_best_available_forcing_inputs_render_is_byte_identical() -> None:
    """Reader #7. Its call site builds its own connection, so the render is compared directly.

    The call-site half is covered by the AST sweep below (the literal store) and
    by the census (the module no longer spells the table name at all), so the
    only thing left for this to check is the text.
    """
    rendered = render_forcing_ts_sql(
        best_available._FORCING_INPUTS_TEMPLATES,
        "legacy",
        entry="best_available.forcing_inputs",
    ).sql
    assert rendered == FROZEN["best_available.forcing_inputs"]


def test_the_delete_payload_key_follows_the_renderer_constant() -> None:
    """Reader #9's ``deleted`` key is the PHYSICAL relation, not a literal.

    ``_delete_rendered`` takes the table name only for the receipt payload. If it
    were a literal in the script the census would count it as an unregistered
    read; taking it from the constant also makes the key follow task 7.3's rename
    for free, which is the behaviour the smoke receipt should have.

    ``next(...)`` would take the FIRST call and pin only that one. There is
    exactly one today and the count is asserted rather than assumed: task 7.3
    adds the narrow table's delete, and that PR should have to edit this pin
    deliberately rather than inherit a silent pass on its new call site.
    """
    source = (REPO_ROOT / "scripts/reset_qhh_smoke_db.py").read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_delete_rendered"
    ]
    assert len(calls) == 1, f"expected exactly one _delete_rendered call site, found {len(calls)}"
    table = calls[0].args[2]
    assert isinstance(table, ast.Name)
    assert table.id == "FORCING_TABLE_LEGACY"


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
# M6 — no narrow render reaches an executed statement
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
def test_every_render_call_site_passes_the_literal_legacy(path: str) -> None:
    """M6, structural half. The store is a CONSTANT, provably, not by convention.

    ``met.forcing_version.timeseries_store`` does not exist until task 7.3, so a
    store EXPRESSION here could only be reading something else — and in
    ``packages/common/forecast_store.py`` there is something else in scope at the
    forcing call site: ``store``, the RIVER route off
    ``hydro.hydro_run.timeseries_store``. Passing it would render narrow forcing
    SQL for every narrow-routed run, against a table with no
    ``forcing_version_key``. An AST check is what distinguishes "passes legacy"
    from "passes a name that currently happens to hold legacy".
    """
    source = REPO_ROOT.joinpath(*path.split("/")).read_text(encoding="utf-8")
    stores = _render_call_stores(source)
    assert stores, f"{path}: registered as a wired reader but calls no renderer"
    for node in stores:
        assert isinstance(node, ast.Constant) and node.value == "legacy", (
            f"{path}: render_forcing_ts_sql must be called with the literal 'legacy' in this task "
            f"(got {ast.dump(node)}; a whole Call node here means the store could not be determined "
            "statically, which is refused rather than skipped)"
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


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("display_coverage.refresh", lambda: display_coverage._REFRESH_SQL),
        ("display_coverage.scan_header", lambda: display_coverage._SCAN_HEADER_SQL),
        ("forecast_store.latest_product", _executed_latest_product),
        ("forecast_store.station_forcing_membership", lambda: _executed_membership()[0]),
        ("forecast_store.station_series_rows", lambda: _executed_station_series(from_time=None, to_time=None)[0]),
        ("forecast_store.forcing_readiness_overall", lambda: _executed_readiness_overall()[0]),
        ("forecast_store.forcing_readiness_variable_rows", lambda: _executed_readiness_variable_rows()[0]),
        ("qhh_production_bootstrap.dynamic_forcing_count", lambda: _executed_dynamic_forcing_count()[0]),
        ("reset_qhh_smoke_db.forcing_timeseries_delete", lambda: _executed_forcing_delete()[0]),
        (
            "best_available.forcing_inputs",
            lambda: render_forcing_ts_sql(best_available._FORCING_INPUTS_TEMPLATES, "legacy").sql,
        ),
    ],
)
def test_no_executed_statement_carries_a_narrow_only_identifier(label: str, statement) -> None:
    """M6, executed half — the one import-cleanliness cannot buy.

    Readers #2 and #8 compose at CALL time, so a module that imports cleanly can
    still hand a cursor narrow SQL. Every statement any wired reader executes is
    checked here against the identifiers only the narrow table has, plus the
    table name it must name.

    ``display_coverage._SCAN_HEADER_SQL`` is in the list although it is not a
    registered reader: it is built from the same import-time chain and is the
    prefetch that runs immediately before the refresh, so if the chain ever grew
    a narrow branch this is where it would show up second.
    """
    sql = statement()
    for token in NARROW_ONLY_TOKENS:
        assert token not in sql, f"{label}: narrow-only identifier {token!r} in an executed statement"
    if "forcing_station_timeseries" in sql:
        assert FORCING_TABLE_LEGACY in sql


def test_the_two_table_constants_still_agree_which_is_why_the_above_is_provable() -> None:
    """D1, restated where this suite's byte-identity claim depends on it.

    Every equality above holds because the legacy store names the relation that
    is actually deployed. Task 7.3 flips ``FORCING_TABLE_LEGACY`` in the
    migration's own commit and re-captures the snapshot; until then a flip here
    alone would leave master naming a relation that does not exist, which is the
    single failure D1 exists to prevent.
    """
    assert FORCING_TABLE == FORCING_TABLE_LEGACY == "met.forcing_station_timeseries"


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
