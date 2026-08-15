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
  bumping writer IS seen is asserted.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

import scripts.node27_autopipeline as autopipe
from packages.common import forecast_store
from packages.common.display_coverage import _stale_run_ids, refresh_run_display_coverage
from packages.common.forecast_store import PsycopgForecastStore
from tests.integration_helpers import (
    BASIN_ID,
    BASIN_VERSION_ID,
    FORECAST_RUN_ID,
    ISSUE_126_PREFIX,
    RIVER_NETWORK_VERSION_ID,
    SOURCE_ID,
    VALID_TIME_2,
    apply_migrations_from_zero,
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

        _execute(
            connection,
            """
            INSERT INTO hydro.river_timeseries (
                run_id, basin_version_id, river_network_version_id, river_segment_id,
                valid_time, lead_time_hours, variable, value, unit, quality_flag
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'q_down', 275.0, 'm3/s', 'ok')
            """,
            (
                FORECAST_RUN_ID,
                BASIN_VERSION_ID,
                RIVER_NETWORK_VERSION_ID,
                f"{ISSUE_126_PREFIX}_seg_inside",
                VALID_TIME_2 + timedelta(hours=1),
                3,
            ),
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

