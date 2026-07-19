"""Materialize per-run display coverage (all basins) into ``hydro.run_display_coverage``.

The latest-product readiness path derives, per candidate run, a set of coverage
values (station/segment counts, station/river valid-time windows, the
per-variable jsonb array) via a deep stack of CTEs over
``met.forcing_station_timeseries`` and ``hydro.river_timeseries``. Those values
are fixed for a finished run, so recomputing them on every request is pure
waste.

This module recomputes them once with the *identical* CTE arithmetic lifted
verbatim from ``PsycopgForecastStore._fetch_latest_qhh_display_candidates`` and
stores the result. ``forecast_store`` then reads them back through a cheap
``run_id`` JOIN (see its availability branch). Because the same SQL produces the
materialized values, the cheap path is a byte-for-byte stand-in for the CTE
path — verified by the parity test.

Refresh is scoped to one ``run_id`` (or all parsed/finished QHH forecast runs).
It never touches node-22; it runs against whichever DB ``DATABASE_URL`` points
at (node-27 local).
"""

from __future__ import annotations

import concurrent.futures
import os
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from packages.common.forecast_store import (
    MVP_STATION_VARIABLES,
    QHH_LATEST_EXPECTED_HORIZON_HOURS,
)

# ---------------------------------------------------------------------------
# Coverage CTE chain — lifted verbatim from the candidate query's CTEs
# (candidate_runs .. hydro_coverage). The ONLY deltas vs the request-time query:
#   * candidate_runs has no source filter / no strict-identity / no LIMIT and no
#     product-specific join; it is optionally narrowed to a single run via
#     %(run_id)s.
#   * horizon / station-variable params are named (%(horizon)s, %(variables)s,
#     %(variable_count)s) instead of positional.
#   * the final projection emits the coverage columns keyed by run_id for upsert.
# Keeping the arithmetic identical is what guarantees parity with the CTE path.
# ---------------------------------------------------------------------------
_COVERAGE_CTES = """
        WITH candidate_runs AS (
            SELECT
                h.run_id,
                h.model_id,
                h.basin_version_id,
                h.forcing_version_id,
                h.source_id,
                h.cycle_time,
                mi.river_network_version_id,
                COALESCE(
                    CASE WHEN mi.resource_profile->>'output_segment_count' ~ '^[0-9]+$'
                        THEN (mi.resource_profile->>'output_segment_count')::integer END,
                    CASE WHEN mi.resource_profile->>'shud_output_segment_count' ~ '^[0-9]+$'
                        THEN (mi.resource_profile->>'shud_output_segment_count')::integer END,
                    CASE WHEN mi.resource_profile->>'shud_output_river_count' ~ '^[0-9]+$'
                        THEN (mi.resource_profile->>'shud_output_river_count')::integer END,
                    CASE WHEN mi.resource_profile->'output_river'->>'output_segment_count' ~ '^[0-9]+$'
                        THEN (mi.resource_profile->'output_river'->>'output_segment_count')::integer END,
                    CASE WHEN mi.resource_profile->'output_river'->>'segment_count' ~ '^[0-9]+$'
                        THEN (mi.resource_profile->'output_river'->>'segment_count')::integer END,
                    rnv.segment_count
                ) AS expected_segment_count,
                fv.station_count AS expected_station_count,
                GREATEST(h.cycle_time, h.start_time, fv.start_time) AS display_start_time,
                LEAST(
                    h.end_time,
                    fv.end_time,
                    h.cycle_time + (%(horizon)s * INTERVAL '1 hour')
                ) AS display_end_time
            FROM hydro.hydro_run h
            JOIN core.basin_version bv
              ON bv.basin_version_id = h.basin_version_id
            LEFT JOIN core.model_instance mi
              ON mi.model_id = h.model_id
            LEFT JOIN core.river_network_version rnv
              ON rnv.river_network_version_id = mi.river_network_version_id
            LEFT JOIN met.forcing_version fv
              ON fv.forcing_version_id = h.forcing_version_id
            WHERE (%(basin_id)s IS NULL OR bv.basin_id = %(basin_id)s)
              AND h.run_type = 'forecast'
              AND h.status IN ('succeeded', 'parsed', 'published')
              AND h.cycle_time IS NOT NULL
              AND (%(run_id)s IS NULL OR h.run_id = %(run_id)s)
        ),
        station_sample_rows AS (
            SELECT
                cr.run_id,
                cr.model_id,
                cr.display_start_time,
                cr.display_end_time,
                fst.forcing_version_id,
                fst.basin_version_id,
                LOWER(fst.source_id) AS station_source_id,
                fst.station_id,
                fst.variable,
                cr.expected_station_count,
                fst.valid_time,
                fst.unit,
                fst.quality_flag
            FROM met.forcing_station_timeseries fst
            JOIN candidate_runs cr
              ON cr.forcing_version_id = fst.forcing_version_id
             AND fst.basin_version_id = cr.basin_version_id
             AND LOWER(fst.source_id) = LOWER(cr.source_id)
            WHERE fst.variable = ANY(%(variables)s)
              AND fst.valid_time >= cr.display_start_time
              AND fst.valid_time <= cr.display_end_time
              AND EXISTS (
                  SELECT 1
                  FROM met.interp_weight iw
                  WHERE iw.model_id = cr.model_id
                    AND iw.station_id = fst.station_id
                    AND iw.variable = fst.variable
                    AND LOWER(iw.source_id) = LOWER(cr.source_id)
              )
        ),
        station_identity_coverage AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable, station_id,
                COUNT(*) AS sample_count,
                MIN(valid_time) AS valid_time_start,
                MAX(valid_time) AS valid_time_end
            FROM station_sample_rows
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable, station_id
        ),
        station_time_coverage AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable, expected_station_count, valid_time,
                COUNT(DISTINCT station_id) AS station_count
            FROM station_sample_rows
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable, expected_station_count, valid_time
        ),
        station_variable_complete_times AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable, valid_time
            FROM station_time_coverage
            WHERE expected_station_count IS NOT NULL
              AND station_count = expected_station_count
        ),
        station_variable_common_times AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable,
                MIN(valid_time) AS valid_time_start,
                MAX(valid_time) AS valid_time_end
            FROM station_variable_complete_times
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable
        ),
        station_all_variable_complete_times AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                valid_time,
                COUNT(DISTINCT variable) AS complete_variable_count
            FROM station_variable_complete_times
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                valid_time
            HAVING COUNT(DISTINCT variable) = %(variable_count)s
        ),
        station_identity_rollup AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                COUNT(DISTINCT station_id) AS station_count,
                SUM(sample_count) AS station_sample_count
            FROM station_identity_coverage
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id
        ),
        station_common_window AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                MIN(valid_time) AS station_valid_time_start,
                MAX(valid_time) AS station_valid_time_end
            FROM station_all_variable_complete_times
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id
        ),
        station_coverage AS (
            SELECT
                rollup.run_id,
                rollup.model_id,
                rollup.display_start_time,
                rollup.display_end_time,
                rollup.forcing_version_id,
                rollup.basin_version_id,
                rollup.station_source_id,
                rollup.station_count,
                rollup.station_sample_count,
                common_window.station_valid_time_start,
                common_window.station_valid_time_end
            FROM station_identity_rollup rollup
            LEFT JOIN station_common_window common_window
              ON common_window.run_id = rollup.run_id
             AND common_window.model_id = rollup.model_id
             AND common_window.display_start_time = rollup.display_start_time
             AND common_window.display_end_time = rollup.display_end_time
             AND common_window.forcing_version_id = rollup.forcing_version_id
             AND common_window.basin_version_id = rollup.basin_version_id
             AND common_window.station_source_id = rollup.station_source_id
        ),
        station_variable_sample_stats AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable,
                COUNT(*) AS sample_count,
                COUNT(DISTINCT NULLIF(BTRIM(unit), '')) AS unit_count,
                COUNT(DISTINCT NULLIF(BTRIM(quality_flag), '')) AS quality_flag_count,
                SUM(CASE WHEN unit IS NULL OR BTRIM(unit) = '' THEN 1 ELSE 0 END)
                    AS missing_unit_samples,
                SUM(CASE WHEN quality_flag IS NULL OR BTRIM(quality_flag) = '' THEN 1 ELSE 0 END)
                    AS missing_quality_flag_samples
            FROM station_sample_rows
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable
        ),
        station_variable_identity_stats AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable,
                COUNT(DISTINCT station_id) AS station_count
            FROM station_identity_coverage
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable
        ),
        station_variable_coverage AS (
            SELECT
                identity_stats.run_id,
                identity_stats.model_id,
                identity_stats.display_start_time,
                identity_stats.display_end_time,
                identity_stats.forcing_version_id,
                identity_stats.basin_version_id,
                identity_stats.station_source_id,
                jsonb_agg(
                    jsonb_build_object(
                        'variable', identity_stats.variable,
                        'station_count', identity_stats.station_count,
                        'sample_count', sample_stats.sample_count,
                        'unit_count', sample_stats.unit_count,
                        'quality_flag_count', sample_stats.quality_flag_count,
                        'missing_unit_samples', sample_stats.missing_unit_samples,
                        'missing_quality_flag_samples', sample_stats.missing_quality_flag_samples,
                        'valid_time_start', common_times.valid_time_start,
                        'valid_time_end', common_times.valid_time_end
                    )
                    ORDER BY identity_stats.variable
                ) AS station_variable_coverage
            FROM station_variable_identity_stats identity_stats
            JOIN station_variable_sample_stats sample_stats
              ON sample_stats.run_id = identity_stats.run_id
             AND sample_stats.model_id = identity_stats.model_id
             AND sample_stats.display_start_time = identity_stats.display_start_time
             AND sample_stats.display_end_time = identity_stats.display_end_time
             AND sample_stats.forcing_version_id = identity_stats.forcing_version_id
             AND sample_stats.basin_version_id = identity_stats.basin_version_id
             AND sample_stats.station_source_id = identity_stats.station_source_id
             AND sample_stats.variable = identity_stats.variable
            LEFT JOIN station_variable_common_times common_times
              ON common_times.run_id = identity_stats.run_id
             AND common_times.model_id = identity_stats.model_id
             AND common_times.display_start_time = identity_stats.display_start_time
             AND common_times.display_end_time = identity_stats.display_end_time
             AND common_times.forcing_version_id = identity_stats.forcing_version_id
             AND common_times.basin_version_id = identity_stats.basin_version_id
             AND common_times.station_source_id = identity_stats.station_source_id
             AND common_times.variable = identity_stats.variable
            GROUP BY
                identity_stats.run_id,
                identity_stats.model_id,
                identity_stats.display_start_time,
                identity_stats.display_end_time,
                identity_stats.forcing_version_id,
                identity_stats.basin_version_id,
                identity_stats.station_source_id
        ),
        river_sample_rows AS (
            SELECT
                rt.run_id,
                rt.basin_version_id,
                rt.river_network_version_id,
                rt.river_segment_id,
                cr.expected_segment_count,
                rt.valid_time,
                rt.lead_time_hours
            FROM hydro.river_timeseries rt
            JOIN candidate_runs cr
              ON cr.run_id = rt.run_id
             AND cr.basin_version_id = rt.basin_version_id
             AND cr.river_network_version_id = rt.river_network_version_id
            WHERE rt.variable = 'q_down'
              AND rt.valid_time >= cr.display_start_time
              AND rt.valid_time <= cr.display_end_time
        ),
        river_identity_coverage AS (
            SELECT
                run_id, basin_version_id, river_network_version_id, river_segment_id,
                COUNT(*) AS sample_count,
                MIN(valid_time) AS valid_time_start,
                MAX(valid_time) AS valid_time_end,
                MIN(lead_time_hours) AS min_lead_time_hours,
                MAX(lead_time_hours) AS max_lead_time_hours
            FROM river_sample_rows
            GROUP BY run_id, basin_version_id, river_network_version_id, river_segment_id
        ),
        river_time_coverage AS (
            SELECT
                run_id, basin_version_id, river_network_version_id,
                expected_segment_count, valid_time,
                COUNT(DISTINCT river_segment_id) AS segment_count
            FROM river_sample_rows
            GROUP BY run_id, basin_version_id, river_network_version_id, expected_segment_count, valid_time
        ),
        river_common_window AS (
            SELECT
                run_id, basin_version_id, river_network_version_id,
                MIN(valid_time) AS river_valid_time_start,
                MAX(valid_time) AS river_valid_time_end
            FROM river_time_coverage
            WHERE expected_segment_count IS NOT NULL
              AND segment_count = expected_segment_count
            GROUP BY run_id, basin_version_id, river_network_version_id
        ),
        river_identity_rollup AS (
            SELECT
                run_id, basin_version_id, river_network_version_id,
                COUNT(DISTINCT river_segment_id) AS segment_count,
                SUM(sample_count) AS river_sample_count,
                MAX(min_lead_time_hours) AS min_lead_time_hours,
                MIN(max_lead_time_hours) AS max_lead_time_hours
            FROM river_identity_coverage
            GROUP BY run_id, basin_version_id, river_network_version_id
        ),
        hydro_coverage AS (
            SELECT
                rollup.run_id,
                rollup.basin_version_id,
                rollup.river_network_version_id,
                rollup.segment_count,
                rollup.river_sample_count,
                common_window.river_valid_time_start,
                common_window.river_valid_time_end,
                rollup.min_lead_time_hours,
                rollup.max_lead_time_hours
            FROM river_identity_rollup rollup
            LEFT JOIN river_common_window common_window
              ON common_window.run_id = rollup.run_id
             AND common_window.basin_version_id = rollup.basin_version_id
             AND common_window.river_network_version_id = rollup.river_network_version_id
        ),
        coverage AS (
            SELECT
                cr.run_id,
                COALESCE(sc.station_count, 0) AS station_count,
                COALESCE(sc.station_sample_count, 0) AS station_sample_count,
                sc.station_source_id,
                sc.display_start_time AS station_display_start_time,
                sc.display_end_time AS station_display_end_time,
                sc.station_valid_time_start,
                sc.station_valid_time_end,
                COALESCE(svc.station_variable_coverage, '[]'::jsonb) AS station_variable_coverage,
                COALESCE(hc.segment_count, 0) AS segment_count,
                COALESCE(hc.river_sample_count, 0) AS river_sample_count,
                hc.river_valid_time_start,
                hc.river_valid_time_end,
                hc.min_lead_time_hours,
                hc.max_lead_time_hours
            FROM candidate_runs cr
            LEFT JOIN station_coverage sc
              ON sc.run_id = cr.run_id
             AND sc.model_id = cr.model_id
             AND sc.display_start_time = cr.display_start_time
             AND sc.display_end_time = cr.display_end_time
             AND sc.forcing_version_id = cr.forcing_version_id
             AND sc.basin_version_id = cr.basin_version_id
             AND sc.station_source_id = LOWER(cr.source_id)
            LEFT JOIN station_variable_coverage svc
              ON svc.run_id = cr.run_id
             AND svc.model_id = cr.model_id
             AND svc.display_start_time = cr.display_start_time
             AND svc.display_end_time = cr.display_end_time
             AND svc.forcing_version_id = cr.forcing_version_id
             AND svc.basin_version_id = cr.basin_version_id
             AND svc.station_source_id = LOWER(cr.source_id)
            LEFT JOIN hydro_coverage hc
              ON hc.run_id = cr.run_id
             AND hc.basin_version_id = cr.basin_version_id
             AND hc.river_network_version_id = cr.river_network_version_id
        )
"""

_REFRESH_SQL = (
    _COVERAGE_CTES
    + """
        INSERT INTO hydro.run_display_coverage (
            run_id, station_count, station_sample_count, station_source_id,
            station_display_start_time, station_display_end_time,
            station_valid_time_start, station_valid_time_end,
            station_variable_coverage, segment_count, river_sample_count,
            river_valid_time_start, river_valid_time_end,
            min_lead_time_hours, max_lead_time_hours, refreshed_at
        )
        SELECT
            run_id, station_count, station_sample_count, station_source_id,
            station_display_start_time, station_display_end_time,
            station_valid_time_start, station_valid_time_end,
            station_variable_coverage, segment_count, river_sample_count,
            river_valid_time_start, river_valid_time_end,
            min_lead_time_hours, max_lead_time_hours, now()
        FROM coverage
        ON CONFLICT (run_id) DO UPDATE SET
            station_count = EXCLUDED.station_count,
            station_sample_count = EXCLUDED.station_sample_count,
            station_source_id = EXCLUDED.station_source_id,
            station_display_start_time = EXCLUDED.station_display_start_time,
            station_display_end_time = EXCLUDED.station_display_end_time,
            station_valid_time_start = EXCLUDED.station_valid_time_start,
            station_valid_time_end = EXCLUDED.station_valid_time_end,
            station_variable_coverage = EXCLUDED.station_variable_coverage,
            segment_count = EXCLUDED.segment_count,
            river_sample_count = EXCLUDED.river_sample_count,
            river_valid_time_start = EXCLUDED.river_valid_time_start,
            river_valid_time_end = EXCLUDED.river_valid_time_end,
            min_lead_time_hours = EXCLUDED.min_lead_time_hours,
            max_lead_time_hours = EXCLUDED.max_lead_time_hours,
            refreshed_at = EXCLUDED.refreshed_at
        RETURNING run_id
    """
)


def run_display_coverage_available(cursor: Any) -> bool:
    """Whether ``hydro.run_display_coverage`` exists (cheap-path gate)."""
    cursor.execute("SELECT to_regclass('hydro.run_display_coverage') AS reg")
    row = cursor.fetchone()
    value = row["reg"] if isinstance(row, dict) else row[0]
    return value is not None


# Per-run refresh statement timeout (ms). Small legacy/QHH runs finish in a few
# seconds, but production direct-grid basins can contain millions of river rows
# and legitimately exceed the former 90-second bound. Keep the query bounded,
# while allowing operators to tune it without code edits for larger basins.
_REFRESH_STATEMENT_TIMEOUT_ENV = "NHMS_DISPLAY_COVERAGE_REFRESH_STATEMENT_TIMEOUT_MS"
_DEFAULT_REFRESH_STATEMENT_TIMEOUT_MS = 900_000
_MIN_REFRESH_STATEMENT_TIMEOUT_MS = 90_000
_MAX_REFRESH_STATEMENT_TIMEOUT_MS = 3_600_000


def _refresh_statement_timeout_ms() -> int:
    raw = os.getenv(_REFRESH_STATEMENT_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_REFRESH_STATEMENT_TIMEOUT_MS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_REFRESH_STATEMENT_TIMEOUT_MS
    if not _MIN_REFRESH_STATEMENT_TIMEOUT_MS <= value <= _MAX_REFRESH_STATEMENT_TIMEOUT_MS:
        return _DEFAULT_REFRESH_STATEMENT_TIMEOUT_MS
    return value


def _refresh(connection: Any, run_id: str | None) -> list[str]:
    params = {
        "horizon": QHH_LATEST_EXPECTED_HORIZON_HOURS,
        # Per-run refresh: run_id uniquely identifies the run and its basin, so
        # no basin filter is needed (basin-agnostic — works for any basin).
        "basin_id": None,
        "run_id": run_id,
        "variables": list(MVP_STATION_VARIABLES),
        "variable_count": len(MVP_STATION_VARIABLES),
    }
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SET LOCAL statement_timeout = %s", (_refresh_statement_timeout_ms(),))
        cursor.execute(_REFRESH_SQL, params)
        rows = cursor.fetchall()
    return [r["run_id"] for r in rows]


def refresh_run_display_coverage(connection: Any, run_id: str) -> bool:
    """Recompute and upsert coverage for one run. Returns True if a row resulted.

    A finished forecast run (any basin) always yields a coverage row (counts may
    be 0 if its forcing/river data is absent); a non-eligible run yields none.
    """
    refreshed = _refresh(connection, run_id)
    connection.commit()
    return run_id in refreshed


def _eligible_run_ids(connection: Any) -> list[str]:
    """Every parsed/finished forecast run across all basins (coverage is
    basin-agnostic; ``--all`` backfills every displayable basin)."""
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT h.run_id
            FROM hydro.hydro_run h
            WHERE h.run_type = 'forecast'
              AND h.status IN ('succeeded', 'parsed', 'published')
              AND h.cycle_time IS NOT NULL
            ORDER BY h.cycle_time DESC, h.run_id DESC
            """,
        )
        return [r["run_id"] for r in cursor.fetchall()]


def refresh_all_run_display_coverage(
    connection: Any,
    *,
    dsn: str,
    skip_fresh: bool = False,
    on_progress: Any = None,
    workers: int = 1,
) -> dict[str, int]:
    """Recompute coverage for every parsed/finished forecast run (all basins).

    Done one run at a time, each on its OWN short-lived connection (opened from
    ``dsn``). Per-run scoping keeps each refresh cheap (the single-run
    ``candidate_runs`` narrows before the station/river CTEs fan out; a single
    all-runs SQL lets them explode cartesian-style and is pathologically slow).
    Dedicated connections mean a killed driver or a timed-out run releases the
    table lock immediately instead of leaving an orphan holding it.

    ``dsn`` is passed explicitly (not read from ``connection.dsn``, which
    psycopg2 strips the password from). A per-run failure (e.g. statement
    timeout) is recorded and skipped so one bad run never aborts the batch. With
    ``skip_fresh`` only runs whose coverage is missing or older than the run's
    ``updated_at`` are recomputed (resumable). Returns counts.
    """
    run_ids = _eligible_run_ids(connection)
    if skip_fresh:
        # Compute the stale set ONCE (a single LEFT JOIN), not per element.
        stale = _stale_run_ids(connection, run_ids)
        run_ids = [r for r in run_ids if r in stale]

    if workers < 1 or workers > 8:
        raise ValueError("coverage workers must be between 1 and 8")

    def refresh_one(run_id: str) -> tuple[str, str]:
        # connect() is inside the try so a connection failure counts as a failed
        # run and the batch continues — one bad run never aborts the whole batch.
        conn = None
        try:
            conn = psycopg2.connect(dsn)
            present = run_id in _refresh(conn, run_id)
            conn.commit()
            return run_id, "refreshed" if present else "no-row"
        except Exception as exc:  # noqa: BLE001 - isolate one run's failure
            if conn is not None:
                conn.rollback()
            return run_id, f"FAILED: {type(exc).__name__}"
        finally:
            if conn is not None:
                conn.close()

    if workers == 1:
        results = [refresh_one(run_id) for run_id in run_ids]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="coverage-refresh",
        ) as executor:
            results = list(executor.map(refresh_one, run_ids))

    refreshed = skipped = failed = 0
    for run_id, status in results:
        if status == "refreshed":
            refreshed += 1
        elif status == "no-row":
            skipped += 1
        else:
            failed += 1
        if on_progress is not None:
            on_progress(run_id, status)
    return {"refreshed": refreshed, "skipped": skipped, "failed": failed}


def _stale_run_ids(connection: Any, run_ids: list[str]) -> set[str]:
    """Runs whose coverage is missing or older than hydro_run.updated_at."""
    if not run_ids:
        return set()
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT h.run_id
            FROM hydro.hydro_run h
            LEFT JOIN hydro.run_display_coverage cov ON cov.run_id = h.run_id
            WHERE h.run_id = ANY(%(run_ids)s)
              AND (cov.run_id IS NULL OR cov.refreshed_at < h.updated_at)
            """,
            {"run_ids": run_ids},
        )
        return {r["run_id"] for r in cursor.fetchall()}
