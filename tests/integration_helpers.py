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


# Column order of the tuples `insert_river_timeseries_dual_written` accepts.
RIVER_TIMESERIES_SEED_COLUMNS = (
    "run_id",
    "basin_version_id",
    "river_network_version_id",
    "river_segment_id",
    "valid_time",
    "lead_time_hours",
    "variable",
    "value",
    "unit",
    "quality_flag",
)


def insert_river_timeseries_dual_written(cursor: Any, rows: list[tuple[Any, ...]]) -> None:
    """Insert ``hydro.river_timeseries`` rows in the #1340 dual-write shape.

    Callers pass the ten TEXT-era columns (see
    ``RIVER_TIMESERIES_SEED_COLUMNS``); the four surrogate keys and three enum
    columns are resolved here from the authority tables and from the row's own
    text values, which is exactly what the #1340 writer does.

    Seeding the old text-only shape stopped being a neutral choice at #1341:
    display-boundary readers now select rows by surrogate key, so NULL-key rows
    are invisible to them while the out-of-boundary text readers still see
    them. A fixture that writes a shape production no longer writes turns any
    fast-path-vs-fallback parity assertion into a false red. Tests that want
    the legacy shape on purpose must say so by inserting it directly, and say
    why (see ``tests/test_river_ts_read_path_surrogate_keys_integration.py``,
    which does exactly that to pin the exclusion contract).

    The authority joins are inner joins, so a typo'd segment or run id would
    silently insert nothing; the row-count check turns that into a loud
    failure at seed time rather than a mystifying empty result later.
    """
    execute_values(
        cursor,
        """
        INSERT INTO hydro.river_timeseries (
            run_id, basin_version_id, river_network_version_id, river_segment_id,
            valid_time, lead_time_hours, variable, value, unit, quality_flag,
            run_key, basin_version_key, river_network_version_key, river_segment_key,
            variable_e, unit_e, quality_flag_e
        )
        SELECT v.run_id, v.basin_version_id, v.river_network_version_id, v.river_segment_id,
               v.valid_time, v.lead_time_hours, v.variable, v.value, v.unit, v.quality_flag,
               h.run_key, bv.basin_version_key, rnv.river_network_version_key, rs.river_segment_key,
               v.variable::hydro.river_variable,
               v.unit::hydro.river_unit,
               v.quality_flag::hydro.river_quality_flag
        FROM (VALUES %s) AS v (
            run_id, basin_version_id, river_network_version_id, river_segment_id,
            valid_time, lead_time_hours, variable, value, unit, quality_flag
        )
        JOIN hydro.hydro_run h ON h.run_id = v.run_id
        JOIN core.basin_version bv ON bv.basin_version_id = v.basin_version_id
        JOIN core.river_network_version rnv
          ON rnv.river_network_version_id = v.river_network_version_id
        JOIN core.river_segment rs
          ON rs.river_network_version_id = v.river_network_version_id
         AND rs.river_segment_id = v.river_segment_id
        """,
        rows,
        page_size=max(len(rows), 1),
    )
    if cursor.rowcount != len(rows):
        raise AssertionError(
            f"river_timeseries seed inserted {cursor.rowcount} of {len(rows)} rows; "
            "an authority row (run / basin_version / river_network_version / river_segment) is missing"
        )


def seed_issue_126_data(database_url: str, *, object_root: Path | None = None) -> None:
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
                # geom is geometry(MultiLineString, 4490) (000036); ST_Multi wraps the
                # LineString fixtures so the insert satisfies the column type.
                template="(%s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s)",
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
                    lifecycle_state,
                    resource_profile
                )
                VALUES (%s, %s, %s, %s, 'calib-v1', 'shud-v1', 's3://nhms/models/it126/package/', true, 'active', %s)
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
                VALUES (
                    %s, %s, %s, %s, %s, %s, 1,
                    's3://nhms/forcing/gfs/2026050300/it126_basin_v1/it126_model/',
                    'forcing-sha',
                    %s
                )
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
                        "parsed",
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
            cursor.execute(
                """
                INSERT INTO hydro.state_snapshot (
                    state_id,
                    model_id,
                    run_id,
                    valid_time,
                    state_uri,
                    checksum,
                    usable_flag,
                    source_id,
                    cycle_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (state_id) DO NOTHING
                """,
                (
                    STATE_ID,
                    MODEL_ID,
                    FORECAST_RUN_ID,
                    VALID_TIME_1,
                    "s3://nhms/state/it126/2026050300.pkl",
                    "0" * 64,
                    True,
                    SOURCE_ID,
                    CYCLE_ID,
                ),
            )
            insert_river_timeseries_dual_written(
                cursor,
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
            cursor.execute(
                """
                INSERT INTO ops.pipeline_job (
                    job_id, run_id, cycle_id, job_type, slurm_job_id,
                    model_id, status, stage, submitted_at, started_at, finished_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO NOTHING
                """,
                (
                    f"{ISSUE_126_PREFIX}_forecast_job",
                    FORECAST_RUN_ID,
                    CYCLE_ID,
                    "forecast",
                    "8101",
                    MODEL_ID,
                    "succeeded",
                    "forecast",
                    VALID_TIME_1,
                    VALID_TIME_1,
                    VALID_TIME_2,
                ),
            )


def _clear_issue_126_rows(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM ops.pipeline_job WHERE job_id LIKE %s", (f"{ISSUE_126_PREFIX}%",))
        cursor.execute("DELETE FROM ops.pipeline_event WHERE entity_id LIKE %s", (f"{ISSUE_126_PREFIX}%",))
        cursor.execute("DELETE FROM ops.qc_result WHERE target_id LIKE %s", (f"{ISSUE_126_PREFIX}%",))
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
        # canonical_grid_snapshot is referenced by three nullable FKs
        # (canonical_met_product.grid_snapshot_id, met_station.grid_snapshot_id,
        # interp_weight.grid_snapshot_id) that do NOT cascade (000043). Null those
        # references first, then delete the snapshots, then the data_source parent.
        # canonical_grid_cell rides the ON DELETE CASCADE on canonical_grid_snapshot.
        for table in ("canonical_met_product", "met_station", "interp_weight"):
            cursor.execute(
                f"UPDATE met.{table} SET grid_snapshot_id = NULL "
                f"WHERE grid_snapshot_id IN "
                f"(SELECT grid_snapshot_id FROM met.canonical_grid_snapshot WHERE source_id = %s)",
                (SOURCE_ID,),
            )
        cursor.execute("DELETE FROM met.canonical_grid_snapshot WHERE source_id = %s", (SOURCE_ID,))
        cursor.execute("DELETE FROM met.data_source WHERE source_id = %s", (SOURCE_ID,))
        cursor.execute("DELETE FROM core.model_instance WHERE model_id = %s", (MODEL_ID,))
        cursor.execute("DELETE FROM core.mesh_version WHERE mesh_version_id = %s", (MESH_VERSION_ID,))
        cursor.execute(
            "DELETE FROM core.river_segment WHERE river_network_version_id LIKE %s",
            (f"{ISSUE_126_PREFIX}%",),
        )
        cursor.execute(
            "DELETE FROM core.river_network_version WHERE river_network_version_id LIKE %s",
            (f"{ISSUE_126_PREFIX}%",),
        )
        cursor.execute("DELETE FROM core.basin_version WHERE basin_version_id = %s", (BASIN_VERSION_ID,))
        cursor.execute("DELETE FROM core.basin WHERE basin_id = %s", (BASIN_ID,))


def set_integration_env(database_url: str, object_root: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("WORKSPACE_ROOT", str(object_root / "workspace"))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    os.environ["DATABASE_URL"] = database_url
