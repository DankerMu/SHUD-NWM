from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.main import app
from apps.api.routes import pipeline as pipeline_routes
from packages.common.migrate import MIGRATIONS_DIR
from tests.integration_helpers import (
    BASIN_ID,
    BASIN_VERSION_ID,
    CYCLE_TIME,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
    STATE_ID,
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
                    {"schemas": ["core", "met", "hydro", "map", "ops"]},
                ).mappings()
            }
            assert schemas == {"core", "met", "hydro", "map", "ops"}

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
            assert ("hydro", "run_status", "parsed") in enum_labels
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
            # 000036 widened geom to MultiLineString so a reach can express a real
            # source gap as separate parts instead of a fabricated cross-gap bridge.
            assert geometry_columns["core.river_segment.geom"]["type"] == "MULTILINESTRING"
            assert geometry_columns["met.met_station.geom"]["type"] == "POINT"

            indexes = {
                row["indexname"]
                for row in connection.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname IN ('core', 'met', 'hydro', 'ops')
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
                "river_segment_network_order_idx",
                "river_network_version_basin_lookup_idx",
                "hydro_run_ops_strict_identity_candidates_idx",
                "river_segment_id_trgm_idx",
                "river_segment_name_trgm_idx",
                "river_segment_segment_name_trgm_idx",
                "met_station_id_trgm_idx",
                "met_station_name_trgm_idx",
                "met_station_active_basin_station_idx",
                "hydro_run_display_product_basin_status_idx",
            } <= indexes

            constraints = {
                row["constraint_name"]
                for row in connection.execute(
                    text(
                        """
                        SELECT constraint_name
                        FROM information_schema.table_constraints
                        WHERE table_schema IN ('core', 'met', 'hydro', 'ops')
                        """
                    )
                ).mappings()
            }
            assert "river_segment_pkey" in constraints
            assert "state_snapshot_model_source_valid_time_key" in indexes

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
    assert forecast.json()["series"][0]["variable"] == "q_down"
    assert status.json()["data"]["current_state"] == "complete"
    assert {stage["stage"] for stage in stages.json()["data"]} >= {"download", "forecast"}
    assert jobs.json()["data"]["items"][0]["slurm_job_id"] == "8101"
    assert states.json()["items"][0]["state_id"] == STATE_ID
    assert state_detail.json()["state_id"] == STATE_ID
    assert state_detail.json()["usable_flag"] is True


def test_list_models_real_db_returns_basin_id_and_basin_name(
    integration_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    object_root = tmp_path / "object-store"
    seed_issue_126_data(integration_database_url, object_root=object_root)
    set_integration_env(integration_database_url, object_root, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/models", params={"active": "all"})

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    item = next((m for m in items if m["model_id"] == MODEL_ID), None)
    assert item is not None, f"seeded MODEL_ID={MODEL_ID} not in /api/v1/models items"
    assert item["basin_id"] == BASIN_ID
    assert item["basin_name"] == "Issue 126 Integration Basin"


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


# ---------------------------------------------------------------------------
# PR 6 (issue #566): post-ingest no-cross-gap invariant on real PostGIS.
# Covers tasks.md 6.2: every reach polyline is single-part, non-trivial, and
# its ST_Length(geog) is within 5% of the river.shp dbf-declared Length.
# ---------------------------------------------------------------------------


def test_no_cross_gap_invariant_holds_after_ingest(
    integration_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 2 contract on real DB: after a full reingest of the qhh-sample
    fixture, every reach row in ``core.river_segment`` is (a) single-part,
    (b) non-trivial (≥ 2 vertices), and (c) within 5% of its dbf-declared
    Length when measured by ``ST_Length(geom::geography)``. Proves no
    cross-gap inflation and no truncation."""

    import psycopg2

    from tests.test_basins_reingest import _stage_qhh_sample_basin
    from workers.model_registry.basins_reingest import reingest_basin

    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")

    apply_migrations_from_zero(integration_database_url)
    basins_root, basin_slug, model_id = _stage_qhh_sample_basin(tmp_path)
    receipt = reingest_basin(
        basin_slug=basin_slug,
        model_id=model_id,
        package_version=f"v-cross-gap-{tmp_path.name}",
        basins_root=basins_root,
        database_url=integration_database_url,
        work_dir=tmp_path / "work",
        output_path=tmp_path / "receipt.json",
        auth_actor_id="cli-model-admin",
        auth_roles=["model_admin"],
    )
    assert receipt["imported_reach_count"] > 0
    assert receipt["multi_part_violation_count"] == 0

    connection = psycopg2.connect(integration_database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT rs.river_segment_id,
                       ST_NumGeometries(rs.geom) AS num_parts,
                       ST_NPoints(rs.geom)       AS n_points,
                       ST_Length(rs.geom::geography) AS measured_m,
                       rs.length_m AS declared_m
                FROM core.river_segment rs
                WHERE rs.river_segment_id LIKE %s
                  AND COALESCE(rs.properties_json->>'shud_output_river', 'false') = 'false'
                """,
                (f"{model_id}_reach_%",),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    assert rows, "no reach rows imported"
    for row in rows:
        # Single-part: PR-2 contract; the column allows multi-part for
        # future-proofing but every row written today is single-part.
        assert row[1] == 1, f"reach {row[0]} has {row[1]} parts (expected 1)"
        # Non-trivial polyline: at least one edge.
        assert row[2] > 1, f"reach {row[0]} has {row[2]} vertices (expected ≥ 2)"
        # Cross-gap inflation / truncation check: measured length within 5%
        # of the dbf-declared length. river.shp's Length is in metres at
        # source; the qhh-sample fixture's sample reaches use very short
        # polylines so an absolute floor (1m) keeps short reaches from
        # tripping the ratio check below.
        declared = float(row[4]) if row[4] is not None else 0.0
        measured = float(row[3])
        if declared > 1.0:
            ratio = abs(measured - declared) / declared
            assert ratio < 0.05, (
                f"reach {row[0]}: declared={declared:.3f}m measured={measured:.3f}m "
                f"drift={ratio:.3%} exceeds 5% bound"
            )
