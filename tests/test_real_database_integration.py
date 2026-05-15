from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.main import app
from apps.api.routes import flood_alerts as flood_alert_routes
from apps.api.routes import pipeline as pipeline_routes
from packages.common.migrate import MIGRATIONS_DIR
from tests.integration_helpers import (
    BASIN_VERSION_ID,
    CYCLE_TIME,
    FORECAST_RUN_ID,
    MODEL_ID,
    STATE_ID,
    VALID_TIME_1,
    apply_migrations_from_zero,
    seed_issue_126_data,
    set_integration_env,
    sqlalchemy_engine,
)

pytestmark = pytest.mark.integration


def test_real_postgres_postgis_timescale_migrations_from_zero_are_idempotent(
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    apply_migrations_from_zero(integration_database_url)
    engine = sqlalchemy_engine(integration_database_url)
    try:
        with engine.connect() as connection:
            extension_names = {
                row["extname"] for row in connection.execute(text("SELECT extname FROM pg_extension")).mappings()
            }
            assert {"postgis", "timescaledb", "pgcrypto"} <= extension_names

            schemas = {
                row["schema_name"]
                for row in connection.execute(
                    text("SELECT schema_name FROM information_schema.schemata WHERE schema_name = ANY(:schemas)"),
                    {"schemas": ["core", "met", "hydro", "flood", "map", "ops"]},
                ).mappings()
            }
            assert schemas == {"core", "met", "hydro", "flood", "map", "ops"}

            migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]
            applied = [
                row["version"]
                for row in connection.execute(
                    text("SELECT version FROM public.schema_migrations ORDER BY version")
                ).mappings()
            ]
            assert applied == migration_names

            enum_labels = {
                (row["schema_name"], row["type_name"], row["enum_label"])
                for row in connection.execute(
                    text(
                        """
                        SELECT n.nspname AS schema_name, t.typname AS type_name, e.enumlabel AS enum_label
                        FROM pg_type t
                        JOIN pg_namespace n ON n.oid = t.typnamespace
                        JOIN pg_enum e ON e.enumtypid = t.oid
                        WHERE n.nspname IN ('hydro', 'met')
                        """
                    )
                ).mappings()
            }
            assert ("hydro", "run_status", "frequency_done") in enum_labels
            assert ("hydro", "run_status", "pending") in enum_labels
            assert ("met", "cycle_status", "complete") in enum_labels

            hypertables = {
                f"{row['hypertable_schema']}.{row['hypertable_name']}"
                for row in connection.execute(
                    text("SELECT hypertable_schema, hypertable_name FROM timescaledb_information.hypertables")
                ).mappings()
            }
            assert {
                "met.forcing_station_timeseries",
                "met.best_available_selection",
                "hydro.river_timeseries",
                "flood.return_period_result",
            } <= hypertables

            geometry_columns = {
                f"{row['f_table_schema']}.{row['f_table_name']}.{row['f_geometry_column']}": row
                for row in connection.execute(
                    text(
                        """
                        SELECT f_table_schema, f_table_name, f_geometry_column, srid, type
                        FROM public.geometry_columns
                        WHERE f_table_schema IN ('core', 'met')
                        """
                    )
                ).mappings()
            }
            assert geometry_columns["core.basin_version.geom"]["srid"] == 4490
            assert geometry_columns["core.basin_version.geom"]["type"] == "MULTIPOLYGON"
            assert geometry_columns["core.river_segment.geom"]["srid"] == 4490
            assert geometry_columns["core.river_segment.geom"]["type"] == "LINESTRING"
            assert geometry_columns["met.met_station.geom"]["type"] == "POINT"

            indexes = {
                row["indexname"]
                for row in connection.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname IN ('core', 'met', 'hydro', 'flood', 'ops')
                        """
                    )
                ).mappings()
            }
            assert {
                "basin_version_geom_gix",
                "river_segment_geom_gix",
                "river_ts_segment_time_idx",
                "pipeline_job_slurm_job_idx",
                "pipeline_job_array_task_idx",
            } <= indexes

            constraints = {
                row["constraint_name"]
                for row in connection.execute(
                    text(
                        """
                        SELECT constraint_name
                        FROM information_schema.table_constraints
                        WHERE table_schema IN ('core', 'met', 'hydro', 'flood', 'ops')
                        """
                    )
                ).mappings()
            }
            assert "river_segment_pkey" in constraints
            assert "return_period_result_pkey" in constraints
            assert "state_snapshot_model_id_valid_time_key" in constraints
    finally:
        engine.dispose()


def test_real_schema_api_and_postgis_spatial_smoke(
    integration_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    object_root = tmp_path / "object-store"
    seed_issue_126_data(integration_database_url, object_root=object_root)
    set_integration_env(integration_database_url, object_root, monkeypatch)
    pipeline_routes._engine.cache_clear()
    flood_alert_routes._engine.cache_clear()

    with TestClient(app) as client:
        models = client.get("/api/v1/models", params={"active": "all"})
        active_models = client.get("/api/v1/models")
        segments = client.get(f"/api/v1/basin-versions/{BASIN_VERSION_ID}/river-segments")
        forecast = client.get(
            f"/api/v1/basin-versions/{BASIN_VERSION_ID}/river-segments/it126_seg_inside/forecast-series",
            params={"issue_time": "latest", "variables": "q_down", "scenarios": "GFS"},
        )
        status = client.get(
            "/api/v1/pipeline/status",
            params={"source": "GFS", "cycle_time": CYCLE_TIME.isoformat()},
        )
        stages = client.get(
            "/api/v1/pipeline/stages",
            params={"source": "GFS", "cycle_time": CYCLE_TIME.isoformat()},
        )
        jobs = client.get("/api/v1/jobs", params={"model_id": MODEL_ID, "stage": "forecast"})
        summary = client.get("/api/v1/flood-alerts/summary", params={"run_id": FORECAST_RUN_ID})
        ranking = client.get("/api/v1/flood-alerts/ranking", params={"run_id": FORECAST_RUN_ID})
        timeline = client.get(
            "/api/v1/flood-alerts/timeline",
            params={"run_id": FORECAST_RUN_ID, "segment_id": "it126_seg_inside"},
        )
        flood_map = client.get(
            "/api/v1/tiles/flood-return-period",
            params={"run_id": FORECAST_RUN_ID, "duration": "1h", "valid_time": VALID_TIME_1.isoformat()},
        )
        flood_bbox = client.get(
            "/api/v1/tiles/flood-return-period",
            params={
                "run_id": FORECAST_RUN_ID,
                "duration": "1h",
                "valid_time": VALID_TIME_1.isoformat(),
                "bbox": "109.5,29.5,111,31",
            },
        )
        states = client.get("/api/v1/state-snapshots", params={"model_id": MODEL_ID, "usable": "true"})
        state_detail = client.get(f"/api/v1/state-snapshots/{STATE_ID}")

    for response in (
        models,
        active_models,
        segments,
        forecast,
        status,
        stages,
        jobs,
        summary,
        ranking,
        timeline,
        flood_map,
        flood_bbox,
        states,
        state_detail,
    ):
        assert response.status_code == 200, response.text

    assert any(item["model_id"] == MODEL_ID for item in models.json()["data"]["items"])
    assert any(item["model_id"] == MODEL_ID for item in active_models.json()["data"]["items"])
    assert {feature["properties"]["segment_id"] for feature in segments.json()["data"]["features"]} == {
        "it126_seg_inside",
        "it126_seg_outside",
    }
    assert forecast.json()["segment_id"] == "it126_seg_inside"
    assert forecast.json()["frequency_thresholds"]["Q20"] == 220.0
    assert status.json()["data"]["current_state"] == "complete"
    assert {stage["stage"] for stage in stages.json()["data"]} >= {"download", "forecast"}
    assert jobs.json()["data"]["items"][0]["array_task_id"] == 0
    assert summary.json()["data"]["total_segments"] == 2
    assert ranking.json()["data"]["items"][0]["river_segment_id"] == "it126_seg_inside"
    assert timeline.json()["data"]["peak"]["warning_level"] == "high_risk"
    assert flood_map.json()["type"] == "FeatureCollection"
    assert {feature["properties"]["segment_id"] for feature in flood_map.json()["features"]} == {
        "it126_seg_inside",
        "it126_seg_outside",
    }
    assert {feature["properties"]["segment_id"] for feature in flood_bbox.json()["features"]} == {
        "it126_seg_inside",
    }
    assert flood_bbox.json()["features"][0]["geometry"]["type"] == "LineString"
    assert states.json()["items"][0]["state_id"] == STATE_ID
    assert state_detail.json()["state_id"] == STATE_ID
    assert state_detail.json()["usable_flag"] is True
