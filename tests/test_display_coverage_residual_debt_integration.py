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
    HINDCAST_RUN_ID,
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
# Checked at import so a stray percent in the frozen file's prose header fails at
# collection — on any machine — instead of at execute time inside a DB-only test.
# The whole file, comments included, is handed to cursor.execute, and psycopg2
# interpolates the entire statement; this module is integration-marked, so that
# failure would otherwise only ever surface on the node-27 lane. The header's own
# re-freeze/retire rule invites #1342 to edit that prose, hence the guard.
assert _LEGACY_FALLBACK_SQL.count("%") == 6 and _LEGACY_FALLBACK_SQL.count("%s") == 6, (
    "the frozen fallback must hold exactly its six positional placeholders and no other "
    "percent sign: psycopg2 interpolates comments too, so a stray % in the header raises "
    "at execute time, and only on the node-27 integration lane"
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

# The #1674 D2 legacy cohort, one row: `published` before the dual-write cutover
# and therefore never stamped with `parsed_at`. Local to this module (see
# `_seed_legacy_published_run`).
_LEGACY_PUBLISHED_RUN_ID = f"{ISSUE_126_PREFIX}_legacy_published_run"
_LEGACY_PUBLISHED_RUN_SHIFT = timedelta(days=30)

# The #1779 discriminator, one row: `parsed`, WITH key-visible fact rows, and no
# `parsed_at`. It is the only row SEEDED HERE on which the retired EXISTS probe
# and the authority-state predicate disagree; the other disagreeing shape is
# named, and deliberately not seeded, in
# `_seed_parsed_run_with_rows_but_no_parse_timestamp`. Local to this module for
# the same reason as the legacy row above.
_ROWS_WITHOUT_PARSED_AT_RUN_ID = f"{ISSUE_126_PREFIX}_rows_without_parsed_at_run"
_ROWS_WITHOUT_PARSED_AT_RUN_SHIFT = timedelta(days=15)

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


def _status(connection: Any, run_id: str) -> Any:
    return _scalar(connection, "SELECT status FROM hydro.hydro_run WHERE run_id = %s", (run_id,))


def _updated_at(connection: Any, run_id: str) -> Any:
    return _scalar(connection, "SELECT updated_at FROM hydro.hydro_run WHERE run_id = %s", (run_id,))


def _parsed_at(connection: Any, run_id: str) -> Any:
    return _scalar(connection, "SELECT parsed_at FROM hydro.hydro_run WHERE run_id = %s", (run_id,))


def _seed_legacy_published_run(connection: Any) -> None:
    """One #1674 D2 legacy row: already ``published``, never stamped.

    Seeded here and not in ``seed_issue_126_data`` on purpose — that helper is
    shared with ``tests/test_real_database_integration.py`` and
    ``tests/test_integration_helpers_bounded_teardown.py``, and a third run in
    the shared seed would move their counts for a row only this module asserts
    on. The throwaway database is created and dropped per test, so the extra row
    needs no teardown of its own.
    """
    _execute(
        connection,
        """
        INSERT INTO hydro.hydro_run (
            run_id, run_type, scenario_id, model_id, basin_version_id,
            forcing_version_id, source_id, cycle_time, start_time, end_time,
            status, parsed_at, run_manifest_uri, output_uri, log_uri
        )
        VALUES (%s, 'hindcast', 'hindcast_era5', %s, %s, NULL, 'gfs', %s, %s, %s,
                'published', NULL, %s, %s, %s)
        """,
        (
            _LEGACY_PUBLISHED_RUN_ID,
            MODEL_ID,
            BASIN_VERSION_ID,
            CYCLE_TIME - _LEGACY_PUBLISHED_RUN_SHIFT,
            CYCLE_TIME - _LEGACY_PUBLISHED_RUN_SHIFT,
            CYCLE_TIME,
            "s3://nhms/runs/it126-legacy/input/manifest.json",
            "s3://nhms/runs/it126-legacy/output/",
            "s3://nhms/runs/it126-legacy/logs/",
        ),
    )


def _key_visible_river_row_count(connection: Any, run_id: str) -> int:
    """Fact rows reachable exactly the way the RETIRED publish probe reached them.

    Correlated on ``run_key``, not ``run_id``, on purpose: that is the access the
    pre-#1779 ``EXISTS`` predicate had. Asserting it non-zero is what keeps the
    discriminating row discriminating — if the dual-write helper ever stopped
    populating ``run_key``, the row would quietly degrade into a second
    do-nothing negative and the test would go on passing under both predicates.
    """
    return _scalar(
        connection,
        """
        SELECT count(*)
        FROM hydro.river_timeseries rt
        JOIN hydro.hydro_run h ON h.run_key = rt.run_key
        WHERE h.run_id = %s
        """,
        (run_id,),
    )


def _seed_parsed_run_with_rows_but_no_parse_timestamp(connection: Any) -> None:
    """One ``parsed`` run WITH key-visible fact rows and NULL ``parsed_at``.

    The only row seeded here on which the two publish predicates disagree. The
    retired probe asked the fact table whether any row correlated with the run
    and would publish this one; the #1779 predicate asks ``parsed_at IS NOT
    NULL`` and must leave it ``parsed``. Both renditions of the old probe fire on
    it — this module's ``_LEGACY_PUBLISH_SQL`` correlates on ``rt.run_id`` while
    pre-#1779 production correlated on ``rt.run_key``, and the dual-write helper
    populates both — so the row reddens either way.

    It is not the only shape the two predicates disagree about, merely the only
    one seeded here. A ``parsed`` run stamped with ``parsed_at`` whose parser wrote
    ZERO fact rows disagrees in the opposite direction: published under the #1779
    predicate, NOT published under either rendition of the old probe, whose
    ``EXISTS`` is false when no fact row correlates. That direction is the
    declared behaviour change rather than a regression — the #1789 owner decision
    recorded in ``scripts/node27_autopipeline.py``'s publish criterion — and it is
    production-reachable, because ``mark_run_parsed`` stamps ``parsed_at``
    unconditionally, zero rows written included. No row here seeds it;
    ``tests/test_display_publish_status_only.py``'s
    ``test_publish_predicate_reads_authority_state_and_no_fact_table`` pins it at
    the statement instead — asserting the publish statement reads neither the fact
    table nor ``EXISTS`` is exactly what makes a stamped zero-row parse publish.

    It carries no timestamp because it is NOT a completed parse, which is the
    qualifier the "integration seeds carry the timestamp only for completed
    parses" contract turns on: ``mark_run_parsed`` stamps ``parsed_at`` in the
    same transaction that commits the rows, so no parser can produce this state.
    It is the counterfactual the old probe misread as a finished parse, held
    here to pin which column the predicate reads. Do not "fix" it by stamping
    ``parsed_at`` — that deletes the only assertion in this test that the old
    predicate fails.

    Its cycle_time sits BEFORE the seed's so it can never outrank the seeded
    forecast run in an ``ORDER BY cycle_time DESC`` path. Seeded in this module
    rather than in ``seed_issue_126_data`` for the reason spelled out in
    ``_seed_legacy_published_run``.
    """
    cycle_time = CYCLE_TIME - _ROWS_WITHOUT_PARSED_AT_RUN_SHIFT
    start_time = VALID_TIME_1 - _ROWS_WITHOUT_PARSED_AT_RUN_SHIFT
    end_time = VALID_TIME_2 - _ROWS_WITHOUT_PARSED_AT_RUN_SHIFT
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id,
                forcing_version_id, source_id, cycle_time, start_time, end_time,
                status, parsed_at, run_manifest_uri, output_uri, log_uri
            )
            VALUES (
                %s, 'forecast', 'forecast_gfs_deterministic', %s, %s,
                %s, %s, %s, %s, %s,
                'parsed', NULL, %s, %s, %s
            )
            """,
            (
                _ROWS_WITHOUT_PARSED_AT_RUN_ID,
                MODEL_ID,
                BASIN_VERSION_ID,
                FORCING_VERSION_ID,
                SOURCE_ID,
                cycle_time,
                start_time,
                end_time,
                "s3://nhms/runs/it126-rows-no-stamp/input/manifest.json",
                "s3://nhms/runs/it126-rows-no-stamp/output/",
                "s3://nhms/runs/it126-rows-no-stamp/logs/",
            ),
        )
        # After the run row: the dual-write helper inner-joins hydro_run to
        # resolve run_key and raises on a row-count shortfall.
        insert_river_timeseries_dual_written(
            cursor,
            [
                (
                    _ROWS_WITHOUT_PARSED_AT_RUN_ID,
                    BASIN_VERSION_ID,
                    RIVER_NETWORK_VERSION_ID,
                    segment_id,
                    valid_time,
                    lead_time_hours,
                    "q_down",
                    float(200 + 10 * segment_index + lead_time_hours),
                    "m3/s",
                    "ok",
                )
                for segment_index, segment_id in enumerate(
                    (f"{ISSUE_126_PREFIX}_seg_inside", f"{ISSUE_126_PREFIX}_seg_outside")
                )
                for lead_time_hours, valid_time in ((1, start_time), (2, end_time))
            ],
        )


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
        # Exactly one run moved: the hindcast seed is also `parsed`, and it is
        # the seeded run with no completed parse behind it (NULL `parsed_at`,
        # no fact rows), so `== 1` above is a two-sided count, not a lucky one.
        assert _status(connection, HINDCAST_RUN_ID) == "parsed"

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


def test_publish_predicate_publishes_completed_parses_and_nothing_else(
    throwaway_database_url: str,
) -> None:
    """#1779: the four regression rows of the authority-state predicate.

    A source-text pin (``tests/test_display_publish_status_only.py``) can say the
    statement reads ``parsed_at``; only a database can say which rows that moves.
    Four rows, one execution:

    * positive — ``parsed`` with a parse timestamp becomes ``published``;
    * negative — ``parsed`` with NULL ``parsed_at`` and no fact rows (a status
      nothing parsed produced) is left alone. Without this the predicate could
      have degenerated to ``status = 'parsed'`` and every assertion above would
      still pass;
    * discriminator — ``parsed`` with key-visible fact rows and NULL
      ``parsed_at``. Of these four rows it is the ONLY one the retired ``EXISTS``
      probe and the authority-state predicate disagree about, and therefore the
      only reason this test is red on pre-#1779 code: the probe publishes it
      (rowcount 2), the predicate leaves it ``parsed`` (rowcount 1). Every other
      assertion here holds under both statements. The predicates also disagree
      on a shape no row here seeds — a ``parsed`` run stamped with ``parsed_at``
      whose parser wrote zero fact rows, which the new predicate publishes and
      the old probe did not — but that direction is #1789's owner decision, not
      a regression, and ``tests/test_display_publish_status_only.py``'s
      ``test_publish_predicate_reads_authority_state_and_no_fact_table`` pins it
      on the statement (see
      ``_seed_parsed_run_with_rows_but_no_parse_timestamp``);
    * legacy — a run already ``published`` with NULL ``parsed_at`` (the #1674 D2
      pre-cutover cohort, 1360 rows in production) is outside the predicate and
      keeps its ``updated_at``. It is the row a predicate that keyed on
      ``parsed_at IS NULL``, or that bumped ``updated_at``, would disturb.
    """
    _prepared_database(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_legacy_published_run(connection)
        _seed_parsed_run_with_rows_but_no_parse_timestamp(connection)
        legacy_updated_at = _updated_at(connection, _LEGACY_PUBLISHED_RUN_ID)
        forecast_updated_at = _updated_at(connection, FORECAST_RUN_ID)
        rows_only_updated_at = _updated_at(connection, _ROWS_WITHOUT_PARSED_AT_RUN_ID)
        assert _parsed_at(connection, FORECAST_RUN_ID) is not None
        assert _parsed_at(connection, HINDCAST_RUN_ID) is None
        # The discriminator's two preconditions, asserted and not assumed: no
        # timestamp, and fact rows the old probe would have found.
        assert _parsed_at(connection, _ROWS_WITHOUT_PARSED_AT_RUN_ID) is None
        assert _key_visible_river_row_count(connection, _ROWS_WITHOUT_PARSED_AT_RUN_ID) > 0

        assert autopipe._publish_display_runs(throwaway_database_url) == 1

        assert _status(connection, FORECAST_RUN_ID) == "published"
        assert _status(connection, HINDCAST_RUN_ID) == "parsed"
        assert _status(connection, _ROWS_WITHOUT_PARSED_AT_RUN_ID) == "parsed"
        assert _status(connection, _LEGACY_PUBLISHED_RUN_ID) == "published"
        assert _updated_at(connection, FORECAST_RUN_ID) == forecast_updated_at
        assert _updated_at(connection, _ROWS_WITHOUT_PARSED_AT_RUN_ID) == rows_only_updated_at
        assert _updated_at(connection, _LEGACY_PUBLISHED_RUN_ID) == legacy_updated_at

        # Idempotent: a second tick finds nothing left to do.
        assert autopipe._publish_display_runs(throwaway_database_url) == 0
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

    What this state pins is binding, not admission: with the candidate's
    forcing_version_id NULL, the one NULL scan scalar -- scan_forcing_version_id,
    read only by the station guard -- binds and executes through the
    named-parameter path, while the river guards bind non-NULL.

    It deliberately does not claim to catch a forcing-version guard that
    "degraded into NULL means match everything" -- that IS the guard's
    specification, not a degradation, and it would be unobservable here anyway.
    station_sample_rows inner-joins candidate_runs ON cr.forcing_version_id =
    fst.forcing_version_id, so a NULL candidate forcing version annihilates the
    station side on BOTH statements whatever the scan guards do; the scan guards
    are conjuncts and can only narrow, never admit. The assertions below pin the
    resulting zero-coverage COALESCE arms equal on both sides. Nor can the
    station side be strengthened: met.forcing_station_timeseries.forcing_version_id
    is TEXT NOT NULL and part of the primary key (db/migrations/000005_met.sql), so
    no station row that a degraded forcing-version guard could admit is
    constructible.

    The river side stays live and carries the non-vacuity (segment_count > 0),
    so this state discriminates a mis-bound scan_run_id / scan_basin_version_id
    / scan_display_start / scan_display_end.
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
