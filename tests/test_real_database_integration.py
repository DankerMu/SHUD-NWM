from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.main import app
from apps.api.routes import flood_alerts as flood_alert_routes
from apps.api.routes import pipeline as pipeline_routes
from packages.common.migrate import MIGRATIONS_DIR, apply_migration
from tests.integration_helpers import (
    BASIN_VERSION_ID,
    CYCLE_TIME,
    FORECAST_RUN_ID,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
    STATE_ID,
    VALID_TIME_1,
    VALID_TIME_2,
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
            assert {"postgis", "timescaledb", "pgcrypto", "pg_trgm"} <= extension_names

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
                "return_period_result_summary_idx",
                "return_period_result_ranking_idx",
                "return_period_result_valid_time_ranking_idx",
                "return_period_result_timeline_idx",
                "return_period_result_map_idx",
                "river_segment_network_order_idx",
                "river_network_version_basin_lookup_idx",
                "hydro_run_ops_strict_identity_candidates_idx",
                "river_segment_id_trgm_idx",
                "river_segment_name_trgm_idx",
                "river_segment_segment_name_trgm_idx",
                "met_station_id_trgm_idx",
                "met_station_name_trgm_idx",
                "hydro_run_display_product_basin_status_idx",
                "return_period_result_run_quality_idx",
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

            return_period_key_columns = [
                row["column_name"]
                for row in connection.execute(
                    text(
                        """
                        SELECT kcu.column_name
                        FROM information_schema.key_column_usage kcu
                        WHERE kcu.table_schema = 'flood'
                          AND kcu.table_name = 'return_period_result'
                          AND kcu.constraint_name = 'return_period_result_pkey'
                        ORDER BY kcu.ordinal_position
                        """
                    )
                ).mappings()
            ]
            assert return_period_key_columns == [
                "run_id",
                "river_network_version_id",
                "river_segment_id",
                "duration",
                "valid_time",
                "max_over_window",
            ]

            valid_time_ranking_columns = [
                row["column_name"]
                for row in connection.execute(
                    text(
                        """
                        SELECT a.attname AS column_name
                        FROM pg_class i
                        JOIN pg_namespace n ON n.oid = i.relnamespace
                        JOIN pg_index ix ON ix.indexrelid = i.oid
                        JOIN pg_attribute a ON a.attrelid = ix.indrelid
                          AND a.attnum = ANY(ix.indkey)
                        WHERE n.nspname = 'flood'
                          AND i.relname = 'return_period_result_valid_time_ranking_idx'
                        ORDER BY array_position(ix.indkey::int[], a.attnum::int)
                        """
                    )
                ).mappings()
            ]
            assert valid_time_ranking_columns[:4] == ["run_id", "valid_time", "max_over_window", "quality_flag"]

            river_segment_lookup_columns = [
                row["column_name"]
                for row in connection.execute(
                    text(
                        """
                        SELECT a.attname AS column_name
                        FROM pg_class i
                        JOIN pg_namespace n ON n.oid = i.relnamespace
                        JOIN pg_index ix ON ix.indexrelid = i.oid
                        JOIN pg_attribute a ON a.attrelid = ix.indrelid
                          AND a.attnum = ANY(ix.indkey)
                        WHERE n.nspname = 'core'
                          AND i.relname = 'river_segment_network_order_idx'
                        ORDER BY array_position(ix.indkey::int[], a.attnum::int)
                        """
                    )
                ).mappings()
            ]
            assert river_segment_lookup_columns == [
                "river_network_version_id",
                "segment_order",
                "river_segment_id",
            ]

            river_network_lookup_columns = [
                row["column_name"]
                for row in connection.execute(
                    text(
                        """
                        SELECT a.attname AS column_name
                        FROM pg_class i
                        JOIN pg_namespace n ON n.oid = i.relnamespace
                        JOIN pg_index ix ON ix.indexrelid = i.oid
                        JOIN pg_attribute a ON a.attrelid = ix.indrelid
                          AND a.attnum = ANY(ix.indkey)
                        WHERE n.nspname = 'core'
                          AND i.relname = 'river_network_version_basin_lookup_idx'
                        ORDER BY array_position(ix.indkey::int[], a.attnum::int)
                        """
                    )
                ).mappings()
            ]
            assert river_network_lookup_columns == ["basin_version_id", "river_network_version_id"]
    finally:
        engine.dispose()


def test_real_return_period_repair_migration_replaces_old_key_idempotently(
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    migration_files = [
        MIGRATIONS_DIR / "000015_flood_return_period_identity_indexes.sql",
        MIGRATIONS_DIR / "000017_return_period_max_over_window_identity.sql",
    ]
    engine = sqlalchemy_engine(integration_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE flood.return_period_result DROP CONSTRAINT return_period_result_pkey"))
            connection.execute(
                text(
                    """
                    ALTER TABLE flood.return_period_result
                      ADD CONSTRAINT return_period_result_pkey
                      PRIMARY KEY (run_id, river_segment_id, duration, valid_time)
                    """
                )
            )

        import psycopg2

        psycopg_connection = psycopg2.connect(integration_database_url)
        psycopg_connection.autocommit = True
        try:
            for migration_file in migration_files:
                apply_migration(psycopg_connection, migration_file)
            for migration_file in migration_files:
                apply_migration(psycopg_connection, migration_file)
        finally:
            psycopg_connection.close()

        with engine.connect() as connection:
            return_period_key_columns = [
                row["column_name"]
                for row in connection.execute(
                    text(
                        """
                        SELECT kcu.column_name
                        FROM information_schema.key_column_usage kcu
                        WHERE kcu.table_schema = 'flood'
                          AND kcu.table_name = 'return_period_result'
                          AND kcu.constraint_name = 'return_period_result_pkey'
                        ORDER BY kcu.ordinal_position
                        """
                    )
                ).mappings()
            ]
        assert return_period_key_columns == [
            "run_id",
            "river_network_version_id",
            "river_segment_id",
            "duration",
            "valid_time",
            "max_over_window",
        ]

        seed_issue_126_data(integration_database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO core.river_network_version (
                        river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
                    )
                    VALUES ('it126_rnv_v2', :basin_version_id, 'integration-v2', 1, 'object://rivnet-v2', 'checksum-v2')
                    ON CONFLICT (river_network_version_id) DO NOTHING
                    """
                ),
                {"basin_version_id": BASIN_VERSION_ID},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO core.river_segment (
                        river_segment_id, river_network_version_id, segment_order, geom, properties_json
                    )
                    VALUES (
                        'it126_seg_inside',
                        'it126_rnv_v2',
                        1,
                        ST_SetSRID(ST_MakeLine(ST_Point(110.1, 30.1), ST_Point(110.2, 30.2)), 4490),
                        '{}'::jsonb
                    )
                    ON CONFLICT (river_segment_id, river_network_version_id) DO NOTHING
                    """
                ),
                {"basin_version_id": BASIN_VERSION_ID},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO flood.return_period_result (
                        run_id, scenario_id, basin_version_id, river_network_version_id, model_id,
                        river_segment_id, valid_time, duration, q_value, return_period, warning_level,
                        source_id, cycle_time, max_over_window, quality_flag
                    )
                    VALUES
                      (
                        :run_id, 'forecast_gfs_deterministic', :basin_version_id, :rnv_v1, :model_id,
                        'it126_seg_inside', :valid_time, '24h', 10, 2, 'elevated',
                        'GFS', :cycle_time, false, 'ok'
                      ),
                      (
                        :run_id, 'forecast_gfs_deterministic', :basin_version_id, 'it126_rnv_v2', :model_id,
                        'it126_seg_inside', :valid_time, '24h', 20, 5, 'watch',
                        'GFS', :cycle_time, false, 'ok'
                      )
                    """
                ),
                {
                    "run_id": FORECAST_RUN_ID,
                    "basin_version_id": BASIN_VERSION_ID,
                    "rnv_v1": RIVER_NETWORK_VERSION_ID,
                    "model_id": MODEL_ID,
                    "valid_time": VALID_TIME_2,
                    "cycle_time": CYCLE_TIME,
                },
            )
            versioned_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*) AS count
                    FROM flood.return_period_result
                    WHERE run_id = :run_id
                      AND river_segment_id = 'it126_seg_inside'
                      AND duration = '24h'
                      AND valid_time = :valid_time
                    """
                ),
                {"run_id": FORECAST_RUN_ID, "valid_time": VALID_TIME_2},
            ).mappings().one()["count"]
            assert versioned_count == 2
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
            params={
                "river_network_version_id": RIVER_NETWORK_VERSION_ID,
                "issue_time": "latest",
                "variables": "q_down",
                "scenarios": "GFS",
            },
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
            params={
                "run_id": FORECAST_RUN_ID,
                "segment_id": "it126_seg_inside",
                "river_network_version_id": RIVER_NETWORK_VERSION_ID,
            },
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
    assert jobs.json()["data"]["items"][0]["slurm_job_id"] == "8101"
    assert summary.json()["data"]["total_segments"] == 2
    assert ranking.json()["data"]["items"][0]["river_segment_id"] == "it126_seg_inside"
    assert ranking.json()["data"]["items"][0]["river_network_version_id"] == RIVER_NETWORK_VERSION_ID
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


def test_real_reserve_pipeline_job_absorbs_job_id_pk_conflict(
    integration_database_url: str,
) -> None:
    """GAP-1 (real Postgres): the production reserve SQL's untargeted
    ``ON CONFLICT DO NOTHING`` must absorb a job_id PRIMARY KEY clash — not just
    an idempotency_key clash — against the real partial unique index from
    migration 000029. A legacy row with the SAME job_id but NULL idempotency_key
    slips past the partial idem index and hits the job_id PK; reserve must report
    a clean loss (``None``) WITHOUT raising.

    This replaces the fake-self-proving SQLite check with the real ON CONFLICT
    semantics the production path actually runs.
    """

    from services.orchestrator.chain import PsycopgOrchestratorRepository

    apply_migrations_from_zero(integration_database_url)
    engine = sqlalchemy_engine(integration_database_url)
    try:
        with engine.begin() as connection:
            # Legacy / non-reserve row: job_id present, idempotency_key NULL.
            connection.execute(
                text(
                    """
                    INSERT INTO ops.pipeline_job (job_id, job_type, status, idempotency_key)
                    VALUES ('dup-x', 'forcing', 'running', NULL)
                    """
                )
            )
    finally:
        engine.dispose()

    repository = PsycopgOrchestratorRepository(integration_database_url)
    # New idempotency_key but the same job_id: the idem partial index does not
    # cover the legacy NULL row, so the job_id PRIMARY KEY is what conflicts.
    result = repository.reserve_pipeline_job(
        {
            "job_id": "dup-x",
            "run_id": "run_1",
            "cycle_id": "cycle_1",
            "job_type": "forcing",
            "model_id": "model_1",
            "stage": "forcing",
            "status": "reserved",
            "idempotency_key": "gfs:cyc:basin:forcing",
            "candidate_id": "run_1",
        }
    )
    # Clean loss, never an exception.
    assert result is None


def test_real_reserve_candidate_reclaims_dead_reservation(
    integration_database_url: str,
) -> None:
    """GAP-1 (real Postgres): a DEAD reservation (``submission_failed``,
    ``slurm_job_id IS NULL``) that still occupies the idempotency_key partial
    unique index is atomically taken over by ``reserve_candidate`` —
    ``created=True`` and the row returns to ``reserved`` — proving the take-over
    UPDATE works against real Postgres, not just the in-memory fakes.
    """

    from services.orchestrator.chain import PsycopgOrchestratorRepository
    from services.orchestrator.reservation import reserve_candidate

    apply_migrations_from_zero(integration_database_url)
    engine = sqlalchemy_engine(integration_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO ops.pipeline_job (
                        job_id, run_id, cycle_id, job_type, model_id, stage,
                        status, slurm_job_id, idempotency_key, candidate_id
                    )
                    VALUES (
                        'dead-k', 'run_1', 'cycle_1', 'forcing', 'model_1', 'forcing',
                        'submission_failed', NULL, 'K', 'run_1'
                    )
                    """
                )
            )
    finally:
        engine.dispose()

    repository = PsycopgOrchestratorRepository(integration_database_url)
    result = reserve_candidate(
        repository,
        idempotency_key="K",
        job_id="dead-k",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
        candidate_id="run_1",
    )

    assert result.created is True
    state = repository.query_candidate_state("K")
    assert state is not None
    assert state["status"] == "reserved"
    assert state["slurm_job_id"] is None
