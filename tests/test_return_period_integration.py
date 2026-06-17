from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg2.extras import Json, execute_values

from packages.common.forecast_store import (
    MVP_STATION_VARIABLES,
    PsycopgForecastStore,
    _qhh_latest_candidate_response,
)
from tests.integration_helpers import (
    apply_migrations_from_zero,
    backfill_integration_run_product_quality,
    psycopg_connection,
)

pytestmark = pytest.mark.integration

# Real-SQL oracle for the return-period LEFT JOIN that feeds
# `availability.return_period_status` on the latest QHH display product. The unit
# suite only feeds `flood_return_period_rows` through recording cursors and never
# executes `_flood_product_quality_join`; this test runs the actual SQL against a
# real database so the join + the non-null peak-row caliber are exercised, and it
# guards the red line that return-period availability is supplemental and NEVER
# blocks the product `ready` decision (its code must stay out of
# `unavailable_reasons`).
_PREFIX = "itrp"
_BASIN_PEAK = f"{_PREFIX}_basin_peak"
_BASIN_NONPEAK = f"{_PREFIX}_basin_nonpeak"
_BASIN_NONPEAK_NONNULL = f"{_PREFIX}_basin_nonpeak_nonnull"
_SOURCE = "GFS"
_RUN_START = datetime(2026, 5, 14, 0, tzinfo=UTC)
_RUN_END = datetime(2026, 5, 14, 1, tzinfo=UTC)
_CYCLE_TIME = datetime(2026, 5, 14, 0, tzinfo=UTC)
_VALID_TIME = datetime(2026, 5, 14, 1, tzinfo=UTC)

# return-period unavailable code that must NEVER appear in the blocking set.
_RETURN_PERIOD_REASON_CODE = "RETURN_PERIOD_RESULT_UNAVAILABLE"


def _seed_run(
    connection: Any,
    *,
    basin_id: str,
    with_peak_return_period: bool,
    with_rows: bool = True,
    nonpeak_return_period: int | None = None,
) -> str:
    suffix = basin_id
    basin_version_id = f"{suffix}_v1"
    river_network_version_id = f"{suffix}_rnv"
    mesh_version_id = f"{suffix}_mesh"
    model_id = f"{suffix}_model"
    run_id = f"{suffix}_run"
    river_segment_id = f"{suffix}_seg"
    forcing_version_id = f"{suffix}_forcing"
    station_id = f"{suffix}_station"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO met.data_source (
                source_id, source_name, source_type, status, native_format, adapter_name, config_json
            )
            VALUES (%s, 'GFS Integration', 'forecast', 'mock', 'netcdf', 'gfs', %s)
            ON CONFLICT (source_id) DO NOTHING
            """,
            (_SOURCE, Json({"return_period_integration": True})),
        )
        cursor.execute(
            "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) VALUES (%s, %s, %s, %s)",
            (basin_id, f"ZZ {basin_id}", "return-period", "return period integration"),
        )
        cursor.execute(
            """
            INSERT INTO core.basin_version (
                basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
            )
            VALUES (
                %s, %s, 'v1', ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490)),
                true, 'integration://basin', 'basin-sha'
            )
            """,
            (basin_version_id, basin_id),
        )
        cursor.execute(
            """
            INSERT INTO core.river_network_version (
                river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
            )
            VALUES (%s, %s, 'v1', 1, 'integration://rnv', 'rnv-sha')
            """,
            (river_network_version_id, basin_version_id),
        )
        cursor.execute(
            """
            INSERT INTO core.river_segment (
                river_segment_id, river_network_version_id, segment_order, geom, properties_json
            )
            VALUES (
                %s, %s, 1,
                ST_Multi(ST_SetSRID(ST_MakeLine(ST_Point(110.1, 30.1), ST_Point(110.2, 30.2)), 4490)),
                '{}'::jsonb
            )
            """,
            (river_segment_id, river_network_version_id),
        )
        cursor.execute(
            """
            INSERT INTO core.mesh_version (
                mesh_version_id, basin_version_id, version_label, mesh_uri, checksum
            )
            VALUES (%s, %s, 'v1', 'integration://mesh', 'mesh-sha')
            """,
            (mesh_version_id, basin_version_id),
        )
        cursor.execute(
            """
            INSERT INTO core.model_instance (
                model_id, basin_version_id, river_network_version_id, mesh_version_id,
                calibration_version_id, shud_code_version, model_package_uri, active_flag, lifecycle_state
            )
            VALUES (%s, %s, %s, %s, 'calib-v1', 'shud-v1', 'integration://package/', true, 'active')
            """,
            (model_id, basin_version_id, river_network_version_id, mesh_version_id),
        )
        cursor.execute(
            """
            INSERT INTO met.met_station (
                station_id, basin_version_id, station_name, geom, elevation_m,
                station_role, active_flag, properties_json
            )
            VALUES (
                %s, %s, 'Return period station',
                ST_SetSRID(ST_Point(110.15, 30.15), 4490),
                3200, 'forcing', true, %s
            )
            """,
            (station_id, basin_version_id, Json({"return_period_integration": True})),
        )
        execute_values(
            cursor,
            """
            INSERT INTO met.interp_weight (
                source_id, grid_id, model_id, station_id, variable, grid_cell_id, weight, method
            )
            VALUES %s
            """,
            [
                (_SOURCE, f"{suffix}_grid", model_id, station_id, variable, f"{suffix}_{variable}_cell", 1.0, "nearest")
                for variable in MVP_STATION_VARIABLES
            ],
        )
        cursor.execute(
            """
            INSERT INTO met.forcing_version (
                forcing_version_id, model_id, source_id, cycle_time, start_time, end_time,
                station_count, forcing_package_uri, checksum, lineage_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, 1, 'integration://forcing/package', 'forcing-sha', %s)
            """,
            (forcing_version_id, model_id, _SOURCE, _CYCLE_TIME, _RUN_START, _RUN_END, Json({"integration": True})),
        )
        execute_values(
            cursor,
            """
            INSERT INTO met.forcing_station_timeseries (
                forcing_version_id, basin_version_id, station_id, valid_time, source_id, variable,
                value, unit, native_resolution, quality_flag
            )
            VALUES %s
            """,
            [
                (
                    forcing_version_id,
                    basin_version_id,
                    station_id,
                    valid_time,
                    _SOURCE,
                    variable,
                    index + 1,
                    "mm" if variable == "PRCP" else "unit",
                    "1h",
                    "ok",
                )
                for index, variable in enumerate(MVP_STATION_VARIABLES)
                for valid_time in (_RUN_START, _VALID_TIME)
            ],
        )
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id,
                forcing_version_id, source_id, cycle_time, start_time, end_time, status, run_manifest_uri
            )
            VALUES (
                %s, 'forecast', 'forecast_gfs_deterministic', %s, %s, %s,
                %s, %s, %s, %s, 'frequency_done', 'integration://manifest.json'
            )
            """,
            (run_id, model_id, basin_version_id, forcing_version_id, _SOURCE, _CYCLE_TIME, _RUN_START, _RUN_END),
        )
        execute_values(
            cursor,
            """
            INSERT INTO hydro.river_timeseries (
                run_id, basin_version_id, river_network_version_id, river_segment_id,
                valid_time, lead_time_hours, variable, value, unit, quality_flag
            )
            VALUES %s
            """,
            [
                (
                    run_id,
                    basin_version_id,
                    river_network_version_id,
                    river_segment_id,
                    _RUN_START,
                    0,
                    "q_down",
                    10.0,
                    "m3/s",
                    "ok",
                ),
                (
                    run_id,
                    basin_version_id,
                    river_network_version_id,
                    river_segment_id,
                    _VALID_TIME,
                    1,
                    "q_down",
                    12.0,
                    "m3/s",
                    "ok",
                ),
            ],
        )
        if with_rows:
            cursor.execute(
                """
                INSERT INTO flood.return_period_result (
                    run_id, scenario_id, basin_version_id, river_network_version_id, model_id,
                    river_segment_id, valid_time, duration, q_value, return_period, warning_level,
                    source_id, cycle_time, max_over_window, quality_flag
                )
                VALUES (%s, 'forecast_gfs_deterministic', %s, %s, %s, %s, %s, '24h', 10, %s, %s, %s, %s, %s, 'ok')
                """,
                (
                    run_id,
                    basin_version_id,
                    river_network_version_id,
                    model_id,
                    river_segment_id,
                    _VALID_TIME,
                    # Peak: max_over_window=true with a non-null return_period.
                    # Non-peak: max_over_window=false (no peak row), so the join's
                    # non-null peak-row count is 0.
                    2 if with_peak_return_period else nonpeak_return_period,
                    "elevated" if with_peak_return_period else None,
                    _SOURCE,
                    _CYCLE_TIME,
                    with_peak_return_period,
                ),
            )
    return run_id


def _clear(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM flood.run_product_quality WHERE run_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM flood.return_period_result WHERE run_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM hydro.river_timeseries WHERE run_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM hydro.hydro_run WHERE run_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM met.forcing_station_timeseries WHERE forcing_version_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM met.forcing_version WHERE forcing_version_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM met.interp_weight WHERE model_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM met.met_station WHERE station_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM core.model_instance WHERE model_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM core.mesh_version WHERE mesh_version_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM core.river_segment WHERE river_segment_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute(
            "DELETE FROM core.river_network_version WHERE river_network_version_id LIKE %s",
            (f"{_PREFIX}%",),
        )
        cursor.execute("DELETE FROM core.basin_version WHERE basin_version_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM core.basin WHERE basin_id LIKE %s", (f"{_PREFIX}%",))


def _candidate_response(store: PsycopgForecastStore, *, basin_id: str) -> dict[str, Any]:
    source_id = _SOURCE
    with store._transaction() as cursor:
        rows = store._fetch_latest_qhh_display_candidates(
            cursor,
            basin_id=basin_id,
            source_id=source_id,
            identity=None,
        )
    assert rows, "candidate query must return the seeded forecast run"
    return _qhh_latest_candidate_response(rows[0], basin_id=basin_id)


def test_return_period_status_ready_when_non_null_peak_rows_exist(integration_database_url: str) -> None:
    apply_migrations_from_zero(integration_database_url)
    with psycopg_connection(integration_database_url) as connection:
        _clear(connection)
        run_id = _seed_run(connection, basin_id=_BASIN_PEAK, with_peak_return_period=True)
    backfill_integration_run_product_quality(integration_database_url, [run_id])

    store = PsycopgForecastStore(integration_database_url)
    try:
        response = _candidate_response(store, basin_id=_BASIN_PEAK)
        availability = response["product"]["availability"]
        assert response["ready"] is True
        assert response["product"]["status"] == "ready"
        assert availability["ready"] is True
        assert availability["return_period_status"] == "ready"
        assert availability["return_period_reasons"] == []
    finally:
        with psycopg_connection(integration_database_url) as connection:
            _clear(connection)


def test_return_period_status_unavailable_does_not_block_ready(integration_database_url: str) -> None:
    apply_migrations_from_zero(integration_database_url)
    with psycopg_connection(integration_database_url) as connection:
        _clear(connection)
        # Only non-peak rows (max_over_window=false, return_period NULL): the join
        # yields zero non-null peak rows.
        run_id = _seed_run(connection, basin_id=_BASIN_NONPEAK, with_peak_return_period=False)
    backfill_integration_run_product_quality(integration_database_url, [run_id])

    store = PsycopgForecastStore(integration_database_url)
    try:
        response = _candidate_response(store, basin_id=_BASIN_NONPEAK)
        availability = response["product"]["availability"]
        assert response["ready"] is True
        assert response["product"]["status"] == "ready"
        assert availability["ready"] is True
        assert availability["return_period_status"] == "unavailable"

        # Red line: the return-period code must NOT be in the blocking set, so it
        # can never demote the product to unavailable on its own.
        blocking_codes = {reason["code"] for reason in availability["unavailable_reasons"]}
        assert blocking_codes == set()
        assert _RETURN_PERIOD_REASON_CODE not in blocking_codes
    finally:
        with psycopg_connection(integration_database_url) as connection:
            _clear(connection)


def test_return_period_status_ignores_non_peak_non_null_rows(integration_database_url: str) -> None:
    apply_migrations_from_zero(integration_database_url)
    with psycopg_connection(integration_database_url) as connection:
        _clear(connection)
        # Caliber guard: non-peak timestep rows with return_period values are
        # not product rows and must not make the supplemental status ready.
        run_id = _seed_run(
            connection,
            basin_id=_BASIN_NONPEAK_NONNULL,
            with_peak_return_period=False,
            nonpeak_return_period=2,
        )
    backfill_integration_run_product_quality(integration_database_url, [run_id])

    store = PsycopgForecastStore(integration_database_url)
    try:
        response = _candidate_response(store, basin_id=_BASIN_NONPEAK_NONNULL)
        availability = response["product"]["availability"]
        assert response["ready"] is True
        assert response["product"]["status"] == "ready"
        assert availability["ready"] is True
        assert availability["return_period_status"] == "unavailable"

        return_period_codes = {reason["code"] for reason in availability["return_period_reasons"]}
        assert _RETURN_PERIOD_REASON_CODE in return_period_codes

        blocking_codes = {reason["code"] for reason in availability["unavailable_reasons"]}
        assert blocking_codes == set()
        assert _RETURN_PERIOD_REASON_CODE not in blocking_codes
    finally:
        with psycopg_connection(integration_database_url) as connection:
            _clear(connection)
