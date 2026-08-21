"""Real-database integration tests for the #1120 residual debt fixes.

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_display_coverage_residual_debt_integration.py

``throwaway_database_url`` (tests/conftest.py) creates and drops a
uniquely-named database per TEST, so nothing here can touch a live one.

Scope: what only a real database can answer.

* the rewritten fallback binds and executes for real — a dict handed to a
  statement full of ``%(name)s`` either works or raises here, which no text
  assertion can decide — and returns rows identical to the fast path;
* publish (status-only) leaves the coverage refreshed during ingest fresh, with
  the pre-#1120 shape (``updated_at = now()``) as the mutation contrast;
* an out-of-band data write that does NOT bump ``updated_at`` — the self-healing
  side effect the old double bump accidentally provided — is measured, not
  assumed: the observation feeds the node-27 receipt, and the contract that a
  bumping writer IS seen is asserted;
* the #1413 pushdown fallback returns the same rows as the single-statement
  fallback it replaced (#1414). The baseline is the frozen pre-#1413 text in
  ``tests/fixtures/legacy_qhh_fallback_pre_1413.sql``, executed on the SAME
  snapshot as the production fallback, and compared row-by-row in order on the
  projected column set: the three #1442 surrogate-key columns are popped off the
  production rows and the popped set is ASSERTED, so a future extra column
  reddens the comparison instead of widening it. Comparing against the fast path
  (the test above) cannot answer this — the fast path's coverage cache is built
  by the same pushdown idiom, so a shared-idiom bug cancels out on both sides.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor, execute_values

import scripts.node27_autopipeline as autopipe
from packages.common import forecast_store
from packages.common.display_coverage import _stale_run_ids, refresh_run_display_coverage
from packages.common.forecast_store import PsycopgForecastStore
from tests.integration_helpers import (
    BASIN_ID,
    BASIN_VERSION_ID,
    CYCLE_TIME,
    FORCING_VERSION_ID,
    FORECAST_RUN_ID,
    ISSUE_126_PREFIX,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
    SOURCE_ID,
    VALID_TIME_1,
    VALID_TIME_2,
    apply_migrations_from_zero,
    insert_river_timeseries_dual_written,
    seed_issue_126_data,
)

pytestmark = pytest.mark.integration

# The publish UPDATE as it stood before #1120; kept here ONLY as the mutation
# contrast for the staleness assertion.
_LEGACY_PUBLISH_SQL = """
    UPDATE hydro.hydro_run h
    SET status = 'published', updated_at = now()
    WHERE h.status = 'parsed'
      AND EXISTS (SELECT 1 FROM hydro.river_timeseries rt WHERE rt.run_id = h.run_id)
"""

# The #1414 parity oracle: the authoritative latest-QHH fallback EXACTLY as it
# stood before the #1413 pushdown rewrite (commit 90dc4a7e), held as data so a
# reformat cannot quietly touch it. It is a comparison baseline and nothing
# else — never re-sync it with packages/common/forecast_store.py; the file's own
# header states the freeze/re-freeze rules.
_LEGACY_FALLBACK_SQL = (Path(__file__).parent / "fixtures" / "legacy_qhh_fallback_pre_1413.sql").read_text(
    encoding="utf-8"
)

# Columns the production candidate CTE carries that the frozen text does not:
# #1442 added the three surrogate keys for the river fact-table join and the
# final SELECT is `cr.*`, so they ride out to the caller. The parity helper pops
# exactly this set off each production row and ASSERTS what it popped — the
# projection is pinned, not tolerant, so a fourth new column reddens the parity
# test instead of being silently excused.
_NEW_ONLY_COLUMNS = frozenset({"run_key", "basin_version_key", "river_network_version_key"})

# Sentinel for the pop above: a column present with value None must still count
# as present, which `row.pop(key, None)` cannot express.
_MISSING = object()

_PARITY_STATION_ID = f"{ISSUE_126_PREFIX}_station_1"
_PARITY_GRID_ID = f"{ISSUE_126_PREFIX}_grid"
_PARITY_GRID_CELL_ID = f"{ISSUE_126_PREFIX}_grid_cell_1"
_NULL_FORCING_RUN_ID = f"{ISSUE_126_PREFIX}_forecast_run_nullfv"
_NULL_FORCING_RUN_SHIFT = timedelta(hours=6)

# One unit and one deterministic value per MVP station variable. Units only have
# to be non-empty and stable: the coverage CTEs count DISTINCT units per
# variable, they do not interpret them.
_PARITY_STATION_UNITS = {
    "PRCP": "mm/h",
    "TEMP": "degC",
    "RH": "percent",
    "wind": "m/s",
    "Rn": "W/m2",
    "Press": "kPa",
}
# Checked at import so a change to MVP_STATION_VARIABLES fails at collection —
# on any machine — instead of as a KeyError inside a DB-only test.
assert set(_PARITY_STATION_UNITS) == set(forecast_store.MVP_STATION_VARIABLES), (
    "_PARITY_STATION_UNITS must cover exactly forecast_store.MVP_STATION_VARIABLES"
)


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    return connection


def _prepared_database(database_url: str) -> None:
    apply_migrations_from_zero(database_url)
    seed_issue_126_data(database_url)


def _execute(connection: Any, sql: str, params: Any = None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


def _scalar(connection: Any, sql: str, params: Any = None) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return None if row is None else next(iter(row.values()))


def _candidates(store: PsycopgForecastStore) -> list[dict[str, Any]]:
    with store._transaction() as cursor:
        return store._fetch_latest_qhh_display_candidates(
            cursor,
            basin_id=BASIN_ID,
            source_id=SOURCE_ID,
        )


def test_forced_fallback_binds_named_parameters_and_matches_the_fast_path(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepared_database(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        assert refresh_run_display_coverage(connection, FORECAST_RUN_ID) is True
    finally:
        connection.close()
    store = PsycopgForecastStore(throwaway_database_url)

    fast_rows = _candidates(store)
    monkeypatch.setattr(forecast_store, "_run_display_coverage_available", lambda _cursor: False)
    fallback_rows = _candidates(store)

    assert [row["run_id"] for row in fast_rows] == [FORECAST_RUN_ID]
    assert fallback_rows == fast_rows
    assert fallback_rows[0]["segment_count"] > 0


def test_status_only_publish_keeps_ingest_refreshed_coverage_fresh(throwaway_database_url: str) -> None:
    _prepared_database(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        assert refresh_run_display_coverage(connection, FORECAST_RUN_ID) is True
        assert _stale_run_ids(connection, [FORECAST_RUN_ID]) == set()

        assert autopipe._publish_display_runs(throwaway_database_url) == 1
        published_status = _scalar(
            connection,
            "SELECT status FROM hydro.hydro_run WHERE run_id = %s",
            (FORECAST_RUN_ID,),
        )
        assert published_status == "published"
        assert _stale_run_ids(connection, [FORECAST_RUN_ID]) == set()

        # Mutation contrast: the pre-#1120 publish shape re-stales the very run
        # whose coverage the same tick refreshed.
        _execute(
            connection,
            "UPDATE hydro.hydro_run SET status = 'parsed' WHERE run_id = %s",
            (FORECAST_RUN_ID,),
        )
        _execute(connection, _LEGACY_PUBLISH_SQL)
        assert _stale_run_ids(connection, [FORECAST_RUN_ID]) == {FORECAST_RUN_ID}
    finally:
        connection.close()


def test_out_of_band_write_without_updated_at_bump_backstop_visibility(
    throwaway_database_url: str,
) -> None:
    _prepared_database(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        assert refresh_run_display_coverage(connection, FORECAST_RUN_ID) is True
        assert autopipe._publish_display_runs(throwaway_database_url) == 1

        # Dual-write shape, like every other river_timeseries row this fixture
        # writes. What is being measured is whether a data write that does not
        # bump `updated_at` is visible to the staleness backstop — the row's
        # column shape is incidental to that, but seeding the pre-#1340 shape
        # here would leave the fixture writing rows production no longer
        # writes, which is precisely the drift that broke the parity test
        # above.
        with connection.cursor() as cursor:
            insert_river_timeseries_dual_written(
                cursor,
                [
                    (
                        FORECAST_RUN_ID,
                        BASIN_VERSION_ID,
                        RIVER_NETWORK_VERSION_ID,
                        f"{ISSUE_126_PREFIX}_seg_inside",
                        VALID_TIME_2 + timedelta(hours=1),
                        3,
                        "q_down",
                        275.0,
                        "m3/s",
                        "ok",
                    ),
                ],
            )

        # Recorded observation (receipt input, no pass threshold): repeat the
        # measurement so the result is known to be deterministic rather than racy.
        observations = [_stale_run_ids(connection, [FORECAST_RUN_ID]) for _ in range(2)]
        assert observations[0] == observations[1]
        out_of_band_visible = bool(observations[0])
        print(
            "receipt[#1120 out-of-band write without updated_at bump]: "
            f"backstop_sees_run={out_of_band_visible}"
        )
        # The contract the cron backstop comment now states: a writer that
        # mutates run data must bump updated_at, and then it IS seen.
        _execute(
            connection,
            "UPDATE hydro.hydro_run SET updated_at = now() WHERE run_id = %s",
            (FORECAST_RUN_ID,),
        )
        assert _stale_run_ids(connection, [FORECAST_RUN_ID]) == {FORECAST_RUN_ID}
    finally:
        connection.close()


# --------------------------------------------------------------------------
# #1414: frozen-statement parity for the forced fallback.
# --------------------------------------------------------------------------


def _legacy_parameters(source_id: str) -> tuple[Any, ...]:
    """Positional binding in the pre-#1413 call site's order.

    The legacy statement took `%s` placeholders and the call site spread
    `*identity_params` between `source_id` and `candidate_limit`; with
    identity=None that spread is empty, so this is the exact tuple it built.
    The production constants are reused deliberately: they are shared INPUTS
    (the new path binds the same names), not arithmetic being re-derived.
    """
    return (
        forecast_store.QHH_LATEST_EXPECTED_HORIZON_HOURS,
        BASIN_ID,
        source_id,
        forecast_store.QHH_LATEST_SEARCH_LIMIT,
        list(forecast_store.MVP_STATION_VARIABLES),
        len(forecast_store.MVP_STATION_VARIABLES),
    )


def _parity_pair(
    store: PsycopgForecastStore,
    *,
    source_id: str = SOURCE_ID,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run both fallbacks on ONE snapshot and return (production, frozen) rows.

    Both statements execute inside a single `store._transaction()` (REPEATABLE
    READ, readonly), so a concurrent writer cannot make the two sides disagree
    for reasons that have nothing to do with the SQL.

    The production rows are projected onto the frozen text's column set by
    popping `_NEW_ONLY_COLUMNS` and asserting the popped set, then the column
    sets are compared position-wise. Callers get rows that are directly
    `==`-comparable.
    """
    with store._transaction() as cursor:
        new_rows = store._fetch_latest_qhh_display_candidates(
            cursor,
            basin_id=BASIN_ID,
            source_id=source_id,
        )
        legacy_rows = store._fetch_all(cursor, _LEGACY_FALLBACK_SQL, _legacy_parameters(source_id))

    for row in new_rows:
        popped = {key for key in _NEW_ONLY_COLUMNS if row.pop(key, _MISSING) is not _MISSING}
        assert popped == _NEW_ONLY_COLUMNS, (
            "production candidate row no longer carries exactly the #1442 surrogate-key columns "
            f"(popped {sorted(popped)}); re-freeze the oracle or widen _NEW_ONLY_COLUMNS deliberately"
        )
    assert [set(row) for row in new_rows] == [set(row) for row in legacy_rows], (
        "projected production columns differ from the frozen statement's columns: "
        f"{[sorted(set(row)) for row in new_rows]} vs {[sorted(set(row)) for row in legacy_rows]}"
    )
    return new_rows, legacy_rows


def _seed_station_coverage(connection: Any) -> None:
    """Seed the station side of the coverage CTEs (1 station, 6 vars, 2 times).

    `seed_issue_126_data` seeds no `met.met_station` / `met.interp_weight` /
    `met.forcing_station_timeseries` rows, and it must not start: it is run
    twice against one session-scoped database by
    `tests/test_real_database_integration.py`, while `_clear_issue_126_rows`
    never deletes met_station / interp_weight — shared station rows would
    primary-key conflict there. This module owns its rows instead, on a
    per-test throwaway database.

    The recipe is not arbitrary. The seed's `forcing_version.station_count` is
    1, and the coverage CTEs only admit a (variable, valid_time) pair once
    `station_count = expected_station_count`, then only report a common window
    for valid_times where ALL `MVP_STATION_VARIABLES` are complete. One station
    with all six variables at both valid_times is the smallest state in which
    `station_valid_time_start/end` and the per-variable jsonb windows are
    non-NULL — with five variables the parity comparison would still pass but on
    a degenerate all-NULL row.
    """
    variables = list(forecast_store.MVP_STATION_VARIABLES)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO met.met_station (
                station_id, basin_version_id, station_name, geom, elevation_m
            )
            VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4490), %s)
            """,
            (
                _PARITY_STATION_ID,
                BASIN_VERSION_ID,
                "Issue 1414 parity station",
                110.5,
                30.5,
                420.0,
            ),
        )
        # method 'idw' on purpose: 000038 puts extra CHECK constraints and a
        # partial unique index on 'direct_grid' rows, and none of that is what
        # this fixture is about. The coverage CTEs only ask whether a row exists
        # for (model_id, station_id, variable, lower(source_id)).
        execute_values(
            cursor,
            """
            INSERT INTO met.interp_weight (
                source_id, grid_id, model_id, station_id, variable, grid_cell_id, weight, method
            )
            VALUES %s
            """,
            [
                (
                    SOURCE_ID,
                    _PARITY_GRID_ID,
                    MODEL_ID,
                    _PARITY_STATION_ID,
                    variable,
                    _PARITY_GRID_CELL_ID,
                    1.0,
                    "idw",
                )
                for variable in variables
            ],
        )
        execute_values(
            cursor,
            """
            INSERT INTO met.forcing_station_timeseries (
                forcing_version_id, basin_version_id, station_id, valid_time,
                source_id, variable, value, unit, quality_flag
            )
            VALUES %s
            """,
            [
                (
                    FORCING_VERSION_ID,
                    BASIN_VERSION_ID,
                    _PARITY_STATION_ID,
                    valid_time,
                    SOURCE_ID,
                    variable,
                    float(10 * index + hour_offset),
                    _PARITY_STATION_UNITS[variable],
                    "ok",
                )
                for index, variable in enumerate(variables)
                for hour_offset, valid_time in enumerate((VALID_TIME_1, VALID_TIME_2))
            ],
        )


def _insert_null_forcing_run(connection: Any) -> str:
    """Add a NULL-`forcing_version_id` forecast run that outranks the seed's.

    `hydro.hydro_run.forcing_version_id` is nullable (`000006_hydro.sql:7`), and
    a NULL there is the state that disables the pushdown's
    `scan_forcing_version_id` guard. Its cycle_time is 6h after the seed's, so
    `ORDER BY cycle_time DESC LIMIT 1` makes it THE candidate.

    `GREATEST`/`LEAST` ignore NULL arguments, so with no forcing version the
    display window is `[start_time, end_time]` = the shifted 07:00..08:00 hour;
    the river rows below sit on both edges, keeping `segment_count` non-trivial
    while station coverage is legitimately zero (the station join is an equality
    on `forcing_version_id`, and NULL matches nothing).

    Written over the caller's autocommit connection, NOT `store._transaction()`,
    which is readonly.
    """
    cycle_time = CYCLE_TIME + _NULL_FORCING_RUN_SHIFT
    start_time = VALID_TIME_1 + _NULL_FORCING_RUN_SHIFT
    end_time = VALID_TIME_2 + _NULL_FORCING_RUN_SHIFT
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id,
                forcing_version_id, source_id, cycle_time, start_time, end_time,
                status, run_manifest_uri, output_uri, log_uri
            )
            VALUES (
                %s, 'forecast', 'forecast_gfs_deterministic', %s, %s,
                NULL, %s, %s, %s, %s,
                'parsed', %s, %s, %s
            )
            """,
            (
                _NULL_FORCING_RUN_ID,
                MODEL_ID,
                BASIN_VERSION_ID,
                SOURCE_ID,
                cycle_time,
                start_time,
                end_time,
                "s3://nhms/runs/it126-nullfv/input/manifest.json",
                "s3://nhms/runs/it126-nullfv/output/",
                "s3://nhms/runs/it126-nullfv/logs/",
            ),
        )
        # After the run row: the dual-write helper inner-joins hydro_run to
        # resolve run_key and raises on a row-count shortfall.
        insert_river_timeseries_dual_written(
            cursor,
            [
                (
                    _NULL_FORCING_RUN_ID,
                    BASIN_VERSION_ID,
                    RIVER_NETWORK_VERSION_ID,
                    segment_id,
                    valid_time,
                    lead_time_hours,
                    "q_down",
                    float(100 + 10 * segment_index + lead_time_hours),
                    "m3/s",
                    "ok",
                )
                for segment_index, segment_id in enumerate(
                    (f"{ISSUE_126_PREFIX}_seg_inside", f"{ISSUE_126_PREFIX}_seg_outside")
                )
                for lead_time_hours, valid_time in ((1, start_time), (2, end_time))
            ],
        )
    return _NULL_FORCING_RUN_ID


def test_forced_fallback_matches_frozen_pre_pushdown_statement_on_covered_candidate(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spec's "Result parity" scenario, against its literal baseline.

    The frozen statement is the thing #1413 replaced; comparing against it is
    the only in-repo way to answer "did the pushdown change any row?".
    """
    _prepared_database(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_station_coverage(connection)
    finally:
        connection.close()

    monkeypatch.setattr(forecast_store, "_run_display_coverage_available", lambda _cursor: False)
    new_rows, legacy_rows = _parity_pair(PsycopgForecastStore(throwaway_database_url))

    # Non-vacuity first: "equal" must not mean "both empty" or "both all-NULL".
    assert len(new_rows) == 1
    assert new_rows[0]["run_id"] == FORECAST_RUN_ID
    assert new_rows[0]["station_count"] == 1
    assert isinstance(new_rows[0]["station_variable_coverage"], list)
    assert new_rows[0]["station_variable_coverage"]
    assert new_rows[0]["station_valid_time_start"] is not None
    assert new_rows[0]["segment_count"] > 0

    assert new_rows == legacy_rows


def test_forced_fallback_matches_frozen_statement_with_null_forcing_version_candidate(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `scan_forcing_version_id IS NULL` branch of the pushdown guards.

    Station rows for the seed's forcing version exist in this snapshot, so a
    guard that degraded into "NULL means match everything" would have something
    to wrongly admit; both sides must still report zero station coverage while
    river coverage stays non-trivial.
    """
    _prepared_database(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_station_coverage(connection)
        null_forcing_run_id = _insert_null_forcing_run(connection)
    finally:
        connection.close()

    monkeypatch.setattr(forecast_store, "_run_display_coverage_available", lambda _cursor: False)
    new_rows, legacy_rows = _parity_pair(PsycopgForecastStore(throwaway_database_url))

    assert len(new_rows) == 1
    assert new_rows[0]["run_id"] == null_forcing_run_id
    assert new_rows[0]["forcing_version_id"] is None
    assert new_rows[0]["segment_count"] > 0
    for row in (new_rows[0], legacy_rows[0]):
        assert row["station_count"] == 0
        assert row["station_variable_coverage"] == []

    assert new_rows == legacy_rows


def test_forced_fallback_matches_frozen_statement_on_empty_candidate_set(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No candidate: the pushdown short-circuits, the frozen text scans. Both empty.

    Vacuous by construction — that is the point. The states above carry the
    non-vacuity; this one pins that the short-circuit did not invent a
    difference at the boundary.
    """
    _prepared_database(throwaway_database_url)

    monkeypatch.setattr(forecast_store, "_run_display_coverage_available", lambda _cursor: False)
    new_rows, legacy_rows = _parity_pair(
        PsycopgForecastStore(throwaway_database_url),
        source_id="no_such_source",
    )

    assert new_rows == []
    assert legacy_rows == []


def test_parity_oracle_is_independent_of_the_production_candidate_sql(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: break production, parity MUST fail.

    Without this, a parity test that accidentally compared the production
    statement with itself would look identical to a working one. The mutant
    shrinks the display horizon from 1 hour to 1 second per horizon unit: with
    `QHH_LATEST_EXPECTED_HORIZON_HOURS = 168` and a 00:00Z cycle the horizon cap
    moves from 00:00Z+168h (inert on this seed, which ends at 02:00Z — a
    '1 minute' mutant would change nothing) to 00:02:48Z, which lands below the
    candidate's own display_start_time. `_qhh_latest_candidate_runs_sql` reads
    the module global at call time, so the monkeypatch reaches the executed SQL.
    """
    _prepared_database(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_station_coverage(connection)
    finally:
        connection.close()

    mutant = forecast_store._QHH_LATEST_CANDIDATE_RUNS_SQL.replace("INTERVAL '1 hour'", "INTERVAL '1 second'")
    assert mutant != forecast_store._QHH_LATEST_CANDIDATE_RUNS_SQL
    monkeypatch.setattr(forecast_store, "_QHH_LATEST_CANDIDATE_RUNS_SQL", mutant)
    monkeypatch.setattr(forecast_store, "_run_display_coverage_available", lambda _cursor: False)

    new_rows, legacy_rows = _parity_pair(PsycopgForecastStore(throwaway_database_url))

    # The frozen side is untouched by the monkeypatch — it is a file, not the
    # module's SQL — so it still sees the covered candidate.
    assert len(legacy_rows) == 1
    assert new_rows != legacy_rows
    if new_rows:
        assert new_rows[0]["display_end_time"] != legacy_rows[0]["display_end_time"]
