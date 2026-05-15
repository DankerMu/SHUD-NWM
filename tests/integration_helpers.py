from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import Json, RealDictCursor, execute_values
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from packages.common.migrate import (
    MIGRATIONS_DIR,
    apply_migration,
    ensure_schema_migrations_table,
    migration_has_been_applied,
)

ISSUE_126_PREFIX = "it126"
BASIN_ID = f"{ISSUE_126_PREFIX}_basin"
BASIN_VERSION_ID = f"{ISSUE_126_PREFIX}_basin_v1"
RIVER_NETWORK_VERSION_ID = f"{ISSUE_126_PREFIX}_rnv_v1"
MESH_VERSION_ID = f"{ISSUE_126_PREFIX}_mesh_v1"
MODEL_ID = f"{ISSUE_126_PREFIX}_model"
SOURCE_ID = "gfs"
CYCLE_TIME = datetime(2026, 5, 3, 0, tzinfo=UTC)
CYCLE_ID = "gfs_2026050300"
FORECAST_RUN_ID = f"{ISSUE_126_PREFIX}_forecast_run"
HINDCAST_RUN_ID = f"{ISSUE_126_PREFIX}_hindcast_run"
FORCING_VERSION_ID = f"{ISSUE_126_PREFIX}_forcing_v1"
STATE_ID = f"{ISSUE_126_PREFIX}_state_2026050300"
VALID_TIME_1 = datetime(2026, 5, 3, 1, tzinfo=UTC)
VALID_TIME_2 = datetime(2026, 5, 3, 2, tzinfo=UTC)


def apply_migrations_from_zero(database_url: str) -> None:
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        ensure_schema_migrations_table(connection)
        for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if not migration_has_been_applied(connection, migration_file.name):
                apply_migration(connection, migration_file)
    finally:
        connection.close()


def sqlalchemy_engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


@contextmanager
def psycopg_connection(database_url: str) -> Iterator[Any]:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = False
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def seed_issue_126_data(database_url: str, *, object_root: Path | None = None) -> None:
    state_uri = "s3://nhms/states/it126_model/2026050300/state.cfg.ic"
    if object_root is not None:
        state_path = object_root / "states" / "it126_model" / "2026050300" / "state.cfg.ic"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("state\n", encoding="utf-8")
    with psycopg_connection(database_url) as connection:
        _clear_issue_126_rows(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
                VALUES (%s, %s, %s, %s)
                """,
                (BASIN_ID, "Issue 126 Integration Basin", "integration", "Deterministic integration smoke basin."),
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
                (BASIN_VERSION_ID, BASIN_ID),
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version (
                    river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
                )
                VALUES (%s, %s, 'v1', 2, 'integration://river-network', 'rnv-sha')
                """,
                (RIVER_NETWORK_VERSION_ID, BASIN_VERSION_ID),
            )
            execute_values(
                cursor,
                """
                INSERT INTO core.river_segment (
                    river_segment_id,
                    river_network_version_id,
                    segment_order,
                    length_m,
                    geom,
                    properties_json
                )
                VALUES %s
                """,
                [
                    (
                        f"{ISSUE_126_PREFIX}_seg_inside",
                        RIVER_NETWORK_VERSION_ID,
                        1,
                        1200.0,
                        "LINESTRING(110.0 30.0, 110.6 30.6)",
                        Json({"name": "Inside segment"}),
                    ),
                    (
                        f"{ISSUE_126_PREFIX}_seg_outside",
                        RIVER_NETWORK_VERSION_ID,
                        2,
                        1800.0,
                        "LINESTRING(116.0 36.0, 116.6 36.6)",
                        Json({"name": "Outside segment"}),
                    ),
                ],
                template="(%s, %s, %s, %s, ST_GeomFromText(%s, 4490), %s)",
            )
            cursor.execute(
                """
                INSERT INTO core.mesh_version (
                    mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json
                )
                VALUES (%s, %s, 'v1', 's3://nhms/models/it126/mesh', 'mesh-sha', %s)
                """,
                (MESH_VERSION_ID, BASIN_VERSION_ID, Json({"cell_count": 2})),
            )
            cursor.execute(
                """
                INSERT INTO core.model_instance (
                    model_id,
                    basin_version_id,
                    river_network_version_id,
                    mesh_version_id,
                    calibration_version_id,
                    shud_code_version,
                    model_package_uri,
                    active_flag,
                    resource_profile
                )
                VALUES (%s, %s, %s, %s, 'calib-v1', 'shud-v1', 's3://nhms/models/it126/package/', true, %s)
                """,
                (MODEL_ID, BASIN_VERSION_ID, RIVER_NETWORK_VERSION_ID, MESH_VERSION_ID, Json({"partition": "test"})),
            )
            cursor.execute(
                """
                INSERT INTO met.data_source (
                    source_id, source_name, source_type, status, native_format, adapter_name, config_json
                )
                VALUES (%s, 'GFS Integration', 'forecast', 'mock', 'netcdf', 'gfs', %s)
                """,
                (SOURCE_ID, Json({"integration": True})),
            )
            cursor.execute(
                """
                INSERT INTO met.forecast_cycle (
                    cycle_id, source_id, cycle_time, issue_time, status, manifest_uri
                )
                VALUES (%s, %s, %s, %s, 'complete', 's3://nhms/raw/it126/manifest.json')
                """,
                (CYCLE_ID, SOURCE_ID, CYCLE_TIME, CYCLE_TIME),
            )
            cursor.execute(
                """
                INSERT INTO met.forcing_version (
                    forcing_version_id,
                    model_id,
                    source_id,
                    cycle_time,
                    start_time,
                    end_time,
                    station_count,
                    forcing_package_uri,
                    checksum,
                    lineage_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, 1, 's3://nhms/forcing/it126/', 'forcing-sha', %s)
                """,
                (
                    FORCING_VERSION_ID,
                    MODEL_ID,
                    SOURCE_ID,
                    CYCLE_TIME,
                    VALID_TIME_1,
                    VALID_TIME_2,
                    Json({"integration": True}),
                ),
            )
            execute_values(
                cursor,
                """
                INSERT INTO hydro.hydro_run (
                    run_id,
                    run_type,
                    scenario_id,
                    model_id,
                    basin_version_id,
                    forcing_version_id,
                    source_id,
                    cycle_time,
                    start_time,
                    end_time,
                    status,
                    run_manifest_uri,
                    output_uri,
                    log_uri
                )
                VALUES %s
                """,
                [
                    (
                        FORECAST_RUN_ID,
                        "forecast",
                        "forecast_gfs_deterministic",
                        MODEL_ID,
                        BASIN_VERSION_ID,
                        FORCING_VERSION_ID,
                        SOURCE_ID,
                        CYCLE_TIME,
                        VALID_TIME_1,
                        VALID_TIME_2,
                        "frequency_done",
                        "s3://nhms/runs/it126/input/manifest.json",
                        "s3://nhms/runs/it126/output/",
                        "s3://nhms/runs/it126/logs/",
                    ),
                    (
                        HINDCAST_RUN_ID,
                        "hindcast",
                        "hindcast_era5",
                        MODEL_ID,
                        BASIN_VERSION_ID,
                        None,
                        "gfs",
                        datetime(2025, 1, 1, tzinfo=UTC),
                        datetime(2025, 1, 1, tzinfo=UTC),
                        datetime(2025, 1, 1, 1, tzinfo=UTC),
                        "parsed",
                        "s3://nhms/runs/it126-hindcast/input/manifest.json",
                        "s3://nhms/runs/it126-hindcast/output/",
                        "s3://nhms/runs/it126-hindcast/logs/",
                    ),
                ],
            )
            execute_values(
                cursor,
                """
                INSERT INTO hydro.river_timeseries (
                    run_id,
                    basin_version_id,
                    river_network_version_id,
                    river_segment_id,
                    valid_time,
                    lead_time_hours,
                    variable,
                    value,
                    unit,
                    quality_flag
                )
                VALUES %s
                """,
                [
                    (FORECAST_RUN_ID, BASIN_VERSION_ID, RIVER_NETWORK_VERSION_ID, f"{ISSUE_126_PREFIX}_seg_inside",
                     VALID_TIME_1, 1, "q_down", 180.0, "m3/s", "ok"),
                    (FORECAST_RUN_ID, BASIN_VERSION_ID, RIVER_NETWORK_VERSION_ID, f"{ISSUE_126_PREFIX}_seg_inside",
                     VALID_TIME_2, 2, "q_down", 250.0, "m3/s", "ok"),
                    (FORECAST_RUN_ID, BASIN_VERSION_ID, RIVER_NETWORK_VERSION_ID, f"{ISSUE_126_PREFIX}_seg_outside",
                     VALID_TIME_1, 1, "q_down", 120.0, "m3/s", "ok"),
                    (FORECAST_RUN_ID, BASIN_VERSION_ID, RIVER_NETWORK_VERSION_ID, f"{ISSUE_126_PREFIX}_seg_outside",
                     VALID_TIME_2, 2, "q_down", 150.0, "m3/s", "ok"),
                ],
            )
            execute_values(
                cursor,
                """
                INSERT INTO flood.flood_frequency_curve (
                    curve_id,
                    model_id,
                    river_network_version_id,
                    basin_version_id,
                    river_segment_id,
                    duration,
                    method,
                    sample_period_start,
                    sample_period_end,
                    sample_size,
                    parameters_json,
                    q2,
                    q5,
                    q10,
                    q20,
                    q50,
                    q100,
                    unit,
                    quality_flag
                )
                VALUES %s
                """,
                [
                    (
                        f"{ISSUE_126_PREFIX}_curve_inside",
                        MODEL_ID,
                        RIVER_NETWORK_VERSION_ID,
                        BASIN_VERSION_ID,
                        f"{ISSUE_126_PREFIX}_seg_inside",
                        "1h",
                        "empirical",
                        "2001-01-01",
                        "2025-12-31",
                        25,
                        Json({"sample_quality": {"Q20": {"met": True}}}),
                        100.0,
                        140.0,
                        180.0,
                        220.0,
                        280.0,
                        340.0,
                        "m3/s",
                        "ok",
                    ),
                    (
                        f"{ISSUE_126_PREFIX}_curve_outside",
                        MODEL_ID,
                        RIVER_NETWORK_VERSION_ID,
                        BASIN_VERSION_ID,
                        f"{ISSUE_126_PREFIX}_seg_outside",
                        "1h",
                        "empirical",
                        "2001-01-01",
                        "2025-12-31",
                        25,
                        Json({"sample_quality": {"Q10": {"met": True}}}),
                        80.0,
                        110.0,
                        140.0,
                        170.0,
                        220.0,
                        260.0,
                        "m3/s",
                        "ok",
                    ),
                    (
                        f"{ISSUE_126_PREFIX}_curve_worker_1",
                        MODEL_ID,
                        RIVER_NETWORK_VERSION_ID,
                        BASIN_VERSION_ID,
                        "seg_0001",
                        "1h",
                        "empirical",
                        "2001-01-01",
                        "2025-12-31",
                        25,
                        Json({"sample_quality": {"Q20": {"met": True}}}),
                        80.0,
                        100.0,
                        120.0,
                        140.0,
                        180.0,
                        220.0,
                        "m3/s",
                        "ok",
                    ),
                    (
                        f"{ISSUE_126_PREFIX}_curve_worker_2",
                        MODEL_ID,
                        RIVER_NETWORK_VERSION_ID,
                        BASIN_VERSION_ID,
                        "seg_0002",
                        "1h",
                        "empirical",
                        "2001-01-01",
                        "2025-12-31",
                        25,
                        Json({"sample_quality": {"Q20": {"met": True}}}),
                        80.0,
                        100.0,
                        120.0,
                        140.0,
                        180.0,
                        220.0,
                        "m3/s",
                        "ok",
                    ),
                ],
            )
            execute_values(
                cursor,
                """
                INSERT INTO flood.return_period_result (
                    run_id,
                    scenario_id,
                    basin_version_id,
                    river_network_version_id,
                    model_id,
                    river_segment_id,
                    valid_time,
                    duration,
                    q_value,
                    q_unit,
                    return_period,
                    warning_level,
                    source_id,
                    cycle_time,
                    max_over_window,
                    quality_flag
                )
                VALUES %s
                """,
                [
                    (
                        FORECAST_RUN_ID,
                        "forecast_gfs_deterministic",
                        BASIN_VERSION_ID,
                        RIVER_NETWORK_VERSION_ID,
                        MODEL_ID,
                        f"{ISSUE_126_PREFIX}_seg_inside",
                        VALID_TIME_1,
                        "1h",
                        180.0,
                        "m3/s",
                        10.0,
                        "watch",
                        SOURCE_ID,
                        CYCLE_TIME,
                        False,
                        "ok",
                    ),
                    (
                        FORECAST_RUN_ID,
                        "forecast_gfs_deterministic",
                        BASIN_VERSION_ID,
                        RIVER_NETWORK_VERSION_ID,
                        MODEL_ID,
                        f"{ISSUE_126_PREFIX}_seg_inside",
                        VALID_TIME_2,
                        "1h",
                        250.0,
                        "m3/s",
                        31.0,
                        "high_risk",
                        SOURCE_ID,
                        CYCLE_TIME,
                        False,
                        "ok",
                    ),
                    (
                        FORECAST_RUN_ID,
                        "forecast_gfs_deterministic",
                        BASIN_VERSION_ID,
                        RIVER_NETWORK_VERSION_ID,
                        MODEL_ID,
                        f"{ISSUE_126_PREFIX}_seg_inside",
                        VALID_TIME_2,
                        "2h",
                        250.0,
                        "m3/s",
                        31.0,
                        "high_risk",
                        SOURCE_ID,
                        CYCLE_TIME,
                        True,
                        "ok",
                    ),
                    (
                        FORECAST_RUN_ID,
                        "forecast_gfs_deterministic",
                        BASIN_VERSION_ID,
                        RIVER_NETWORK_VERSION_ID,
                        MODEL_ID,
                        f"{ISSUE_126_PREFIX}_seg_outside",
                        VALID_TIME_2,
                        "2h",
                        150.0,
                        "m3/s",
                        14.0,
                        "warning",
                        SOURCE_ID,
                        CYCLE_TIME,
                        True,
                        "ok",
                    ),
                    (
                        FORECAST_RUN_ID,
                        "forecast_gfs_deterministic",
                        BASIN_VERSION_ID,
                        RIVER_NETWORK_VERSION_ID,
                        MODEL_ID,
                        f"{ISSUE_126_PREFIX}_seg_outside",
                        VALID_TIME_1,
                        "1h",
                        120.0,
                        "m3/s",
                        6.0,
                        "watch",
                        SOURCE_ID,
                        CYCLE_TIME,
                        False,
                        "ok",
                    ),
                ],
            )
            cursor.execute(
                """
                INSERT INTO hydro.state_snapshot (
                    state_id, model_id, run_id, valid_time, state_uri, checksum, usable_flag
                )
                VALUES (%s, %s, %s, %s, %s, 'state-sha', true)
                """,
                (STATE_ID, MODEL_ID, FORECAST_RUN_ID, CYCLE_TIME, state_uri),
            )
            execute_values(
                cursor,
                """
                INSERT INTO ops.pipeline_job (
                    job_id, run_id, cycle_id, job_type, slurm_job_id, array_task_id, model_id, stage, status, log_uri
                )
                VALUES %s
                """,
                [
                    (
                        f"{ISSUE_126_PREFIX}_job_download",
                        FORECAST_RUN_ID,
                        CYCLE_ID,
                        "download_source_cycle",
                        "8100",
                        None,
                        MODEL_ID,
                        "download",
                        "succeeded",
                        "s3://nhms/logs/download.out",
                    ),
                    (
                        f"{ISSUE_126_PREFIX}_job_forecast_0",
                        FORECAST_RUN_ID,
                        CYCLE_ID,
                        "run_shud_forecast_array",
                        "8101",
                        0,
                        MODEL_ID,
                        "forecast",
                        "succeeded",
                        "s3://nhms/logs/forecast-0.out",
                    ),
                ],
            )


def _clear_issue_126_rows(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM ops.pipeline_job WHERE job_id LIKE %s", (f"{ISSUE_126_PREFIX}%",))
        cursor.execute("DELETE FROM ops.pipeline_event WHERE entity_id LIKE %s", (f"{ISSUE_126_PREFIX}%",))
        cursor.execute("DELETE FROM ops.qc_result WHERE target_id LIKE %s", (f"{ISSUE_126_PREFIX}%",))
        cursor.execute(
            "DELETE FROM flood.return_period_result WHERE run_id IN (%s, %s)",
            (FORECAST_RUN_ID, HINDCAST_RUN_ID),
        )
        cursor.execute("DELETE FROM flood.flood_frequency_curve WHERE curve_id LIKE %s", (f"{ISSUE_126_PREFIX}%",))
        cursor.execute("DELETE FROM hydro.state_snapshot WHERE state_id LIKE %s", (f"{ISSUE_126_PREFIX}%",))
        cursor.execute(
            "DELETE FROM hydro.river_timeseries WHERE run_id IN (%s, %s)",
            (FORECAST_RUN_ID, HINDCAST_RUN_ID),
        )
        cursor.execute("DELETE FROM hydro.hydro_run WHERE run_id IN (%s, %s)", (FORECAST_RUN_ID, HINDCAST_RUN_ID))
        cursor.execute(
            "DELETE FROM met.forcing_version_component WHERE forcing_version_id LIKE %s",
            (f"{ISSUE_126_PREFIX}%",),
        )
        cursor.execute(
            "DELETE FROM met.forcing_station_timeseries WHERE forcing_version_id LIKE %s",
            (f"{ISSUE_126_PREFIX}%",),
        )
        cursor.execute("DELETE FROM met.forcing_version WHERE forcing_version_id LIKE %s", (f"{ISSUE_126_PREFIX}%",))
        cursor.execute("DELETE FROM met.forecast_cycle WHERE cycle_id = %s", (CYCLE_ID,))
        cursor.execute("DELETE FROM met.data_source WHERE source_id = %s", (SOURCE_ID,))
        cursor.execute("DELETE FROM core.model_instance WHERE model_id = %s", (MODEL_ID,))
        cursor.execute("DELETE FROM core.mesh_version WHERE mesh_version_id = %s", (MESH_VERSION_ID,))
        cursor.execute(
            "DELETE FROM core.river_segment WHERE river_network_version_id = %s",
            (RIVER_NETWORK_VERSION_ID,),
        )
        cursor.execute(
            "DELETE FROM core.river_network_version WHERE river_network_version_id = %s",
            (RIVER_NETWORK_VERSION_ID,),
        )
        cursor.execute("DELETE FROM core.basin_version WHERE basin_version_id = %s", (BASIN_VERSION_ID,))
        cursor.execute("DELETE FROM core.basin WHERE basin_id = %s", (BASIN_ID,))


def set_integration_env(database_url: str, object_root: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("WORKSPACE_ROOT", str(object_root / "workspace"))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    os.environ["DATABASE_URL"] = database_url
