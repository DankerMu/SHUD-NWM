from __future__ import annotations

from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# Issue #1468: the identifier trigram index is an EXPRESSION index, so equality
# lookups cannot select it, and the four core identity tables carry per-table
# autovacuum analyze parameters. Real PostgreSQL only: every assertion below is
# about what the planner and the catalog actually do with migration 000052.
# ---------------------------------------------------------------------------

AUTHORITY_STATS_HYGIENE_MIGRATION = "000052_authority_stats_hygiene_trgm_expression_index.sql"
# >= 34 characters, mirroring the production `basins_jialingjiang_shud_shud_riv_`
# family whose shared trigrams made the posting lists cover the whole table.
_SHARED_ID_PREFIX = "basins_pytest1468_shud_shud_riv_shared_"


def _river_segment_id_trgm_index(cursor: Any) -> tuple[str, bool]:
    cursor.execute(
        """
        SELECT pg_get_indexdef(ix.indexrelid) AS indexdef, ix.indisvalid
        FROM pg_index ix
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_class t ON t.oid = ix.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'core'
          AND t.relname = 'river_segment'
          AND i.relname = 'river_segment_id_trgm_idx'
        """
    )
    row = cursor.fetchone()
    assert row is not None, "river_segment_id_trgm_idx is missing"
    return row[0], row[1]


def _river_segment_id_trgm_opfamily_operators(cursor: Any) -> set[str]:
    """Every operator the index's operator family can answer, as `name(left,right)`.

    Which predicates an index is ELIGIBLE for is a property of its indexed
    expression plus its operator family -- a catalog fact, decided before the
    planner ever costs anything.
    """

    cursor.execute(
        """
        SELECT oc.opcfamily
        FROM pg_index ix
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_class t ON t.oid = ix.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_opclass oc ON oc.oid = ix.indclass[0]
        WHERE n.nspname = 'core'
          AND t.relname = 'river_segment'
          AND i.relname = 'river_segment_id_trgm_idx'
        """
    )
    row = cursor.fetchone()
    assert row is not None, "river_segment_id_trgm_idx is missing"
    cursor.execute(
        "SELECT amopopr::regoperator::text FROM pg_amop WHERE amopfamily = %s",
        (row[0],),
    )
    return {operator for (operator,) in cursor.fetchall()}


def _legacy_index_exists(cursor: Any) -> bool:
    cursor.execute(
        """
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'core' AND indexname = 'river_segment_id_trgm_idx_legacy'
        """
    )
    return cursor.fetchone() is not None


def _autovacuum_analyze_options(cursor: Any) -> dict[str, dict[str, str]]:
    cursor.execute(
        """
        SELECT c.relname, c.reloptions
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'core'
          AND c.relname IN (
              'river_segment', 'river_segment_crosswalk',
              'river_network_version', 'basin_version'
          )
        """
    )
    options: dict[str, dict[str, str]] = {}
    for relname, reloptions in cursor.fetchall():
        parsed: dict[str, str] = {}
        for option in reloptions or []:
            key, _, value = option.partition("=")
            parsed[key] = value
        options[relname] = parsed
    return options


def _assert_000052_state(cursor: Any) -> None:
    """Every catalog fact 000052 is responsible for, in one place.

    Called after the first apply and again after the replay, so the idempotence
    case compares against the same oracle rather than against itself.
    """

    indexdef, indisvalid = _river_segment_id_trgm_index(cursor)
    # The expression is the whole point: a predicate over the bare column cannot
    # match `lower(river_segment_id)`, whatever the statistics say.
    assert "lower(river_segment_id)" in indexdef, indexdef
    assert "gin_trgm_ops" in indexdef, indexdef
    assert "USING gin" in indexdef, indexdef
    # An interrupted CREATE INDEX CONCURRENTLY leaves a same-named INVALID index
    # that the planner silently ignores -- the index list alone cannot see it.
    assert indisvalid is True, "river_segment_id_trgm_idx is INVALID (interrupted concurrent build?)"
    assert not _legacy_index_exists(cursor), "the renamed bare-column index was not dropped"

    options = _autovacuum_analyze_options(cursor)
    for relname in ("river_segment", "river_segment_crosswalk"):
        assert float(options[relname]["autovacuum_analyze_scale_factor"]) == 0.01, options[relname]
        assert int(options[relname]["autovacuum_analyze_threshold"]) == 500, options[relname]
    # 20-row version tables: a single-row change must cross the bar.
    for relname in ("river_network_version", "basin_version"):
        assert float(options[relname]["autovacuum_analyze_scale_factor"]) == 0.0, options[relname]
        assert int(options[relname]["autovacuum_analyze_threshold"]) == 1, options[relname]


def _seed_shared_prefix_family(cursor: Any) -> None:
    """A synthetic id family sharing a >= 34 character prefix, plus its scope rows.

    Written inside the caller's transaction and rolled back: this is planner
    input, not fixture data other tests should see.
    """

    cursor.execute(
        """
        INSERT INTO core.basin (basin_id, basin_name)
        VALUES ('it1468_basin', 'Issue 1468 Trigram Basin')
        """
    )
    cursor.execute(
        """
        INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom)
        VALUES (
            'it1468_basin_v1', 'it1468_basin', 'v1',
            ST_Multi(ST_GeomFromText('POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))', 4490))
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO core.river_network_version (
            river_network_version_id, basin_version_id, version_label, segment_count
        )
        VALUES ('it1468_rnv_v1', 'it1468_basin_v1', 'v1', 600)
        """
    )
    cursor.executemany(
        """
        INSERT INTO core.river_segment (river_segment_id, river_network_version_id, segment_order)
        VALUES (%s, 'it1468_rnv_v1', %s)
        """,
        [(f"{_SHARED_ID_PREFIX}{index:04d}", index % 5) for index in range(600)],
    )
    # ANALYZE is legal inside a transaction block (VACUUM is not); without it the
    # planner would work from default estimates and prove nothing about costs.
    cursor.execute("ANALYZE core.river_segment")


def _explain(cursor: Any, sql: str, params: tuple[Any, ...] | None = None) -> str:
    cursor.execute(f"EXPLAIN {sql}", params)
    return "\n".join(row[0] for row in cursor.fetchall())


def test_identifier_trigram_index_is_an_expression_index_equality_cannot_select(
    integration_database_url: str,
) -> None:
    """000052's structural claim, proved structurally (issue #1468).

    Production picked the trigram index for `river_segment_id = $1` and paid
    51 s instead of 17 ms, with FRESH statistics and a cheaper estimated cost --
    so a cost- or statistics-based assertion would prove nothing. The negative
    half is therefore a planner fact costs cannot flip: the equality qual cannot
    MATCH the expression index under any path setting, so no plan names it. The
    positive half is a catalog fact: the index's expression and operator family
    cover the rewritten `lower(...) LIKE ...` predicate.

    The positive half deliberately does NOT consult the planner. On this 600-row
    synthetic family with `enable_seqscan = off` the planner substitutes a
    no-condition bitmap scan over an unrelated btree, which is cheaper than the
    GIN path -- an outcome about costs, not about selectability. Positive planner
    evidence on real data lives in the node-27 E4(iii) receipt.
    """

    import psycopg2

    apply_migrations_from_zero(integration_database_url)
    connection = psycopg2.connect(integration_database_url)
    try:
        with connection.cursor() as cursor:
            _assert_000052_state(cursor)

            _seed_shared_prefix_family(cursor)

            cursor.execute(
                """
                CREATE TEMP TABLE it1468_backfill_batch (
                    river_segment_id TEXT,
                    river_network_version_id TEXT
                ) ON COMMIT DROP
                """
            )
            cursor.executemany(
                "INSERT INTO it1468_backfill_batch VALUES (%s, 'it1468_rnv_v1')",
                [(f"{_SHARED_ID_PREFIX}{index:04d}",) for index in range(0, 600, 6)],
            )
            cursor.execute("ANALYZE it1468_backfill_batch")

            # The `_BATCH_UPDATE_SQL` join shape (scripts/node27_river_identity_backfill.py):
            # both identity columns bound by equality against a batch of rows.
            equality_join = """
                SELECT rs.river_segment_id
                FROM core.river_segment rs, it1468_backfill_batch t
                WHERE rs.river_segment_id = t.river_segment_id
                  AND rs.river_network_version_id = t.river_network_version_id
            """
            default_plan = _explain(cursor, equality_join)
            assert "river_segment_id_trgm_idx" not in default_plan, default_plan

            # ... and it is not the sequential scan hiding the trap: with every
            # non-bitmap path disabled, the trigram index is the ONLY other
            # candidate an equality qual could reach -- and still cannot.
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_indexscan = off")
            cursor.execute("SET LOCAL enable_indexonlyscan = off")
            bitmap_only_plan = _explain(cursor, equality_join)
            assert "river_segment_id_trgm_idx" not in bitmap_only_plan, bitmap_only_plan
            # No planner call follows, and the transaction is rolled back below,
            # so these `SET LOCAL` toggles die with it -- nothing to restore.

            # The other half of the contract, read out of the catalog instead of
            # out of a plan: the rewritten id arm in packages/common/model_registry.py
            # searches with `lower(river_segment_id) LIKE ...`, and this index CAN
            # serve that -- its indexed expression is exactly `lower(river_segment_id)`,
            # it is a valid gin_trgm_ops index, and its operator family answers `~~`.
            indexdef, indisvalid = _river_segment_id_trgm_index(cursor)
            assert "lower(river_segment_id)" in indexdef, indexdef
            assert "gin_trgm_ops" in indexdef, indexdef
            assert indisvalid is True, "river_segment_id_trgm_idx is INVALID"
            operators = _river_segment_id_trgm_opfamily_operators(cursor)
            assert "~~(text,text)" in operators, sorted(operators)
            # The same family also answers `=`: since pg_trgm 1.6 `gin_trgm_ops`
            # supports equality, which is precisely why a BARE-column trigram index
            # was eligible for `river_segment_id = $1` in the first place, and why
            # 000052 had to move the index onto an expression to make it ineligible.
            assert "=(text,text)" in operators, sorted(operators)
    finally:
        # Planner input only: nothing this test seeded outlives it.
        connection.rollback()
        connection.close()


def test_authority_stats_hygiene_migration_replays_without_changing_anything(
    integration_database_url: str,
) -> None:
    """Idempotence, proved by an actual second apply (issue #1468, design D3).

    ``apply_migrations_from_zero`` is ledger-gated, so calling it twice replays
    nothing and cannot serve as this evidence: the ledger row has to be removed
    first. Runs on an autocommit connection because the file contains
    CREATE/DROP INDEX CONCURRENTLY, which PostgreSQL rejects inside a
    transaction block.
    """

    import psycopg2

    from packages.common.migrate import apply_migration

    apply_migrations_from_zero(integration_database_url)
    connection = psycopg2.connect(integration_database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            # Pin the state BEFORE the replay, so a first-apply defect cannot
            # masquerade as a replay defect below.
            _assert_000052_state(cursor)
            indexdef_before, _valid_before = _river_segment_id_trgm_index(cursor)
            options_before = _autovacuum_analyze_options(cursor)
            cursor.execute(
                "DELETE FROM public.schema_migrations WHERE version LIKE '000052%'"
            )

        apply_migration(connection, MIGRATIONS_DIR / AUTHORITY_STATS_HYGIENE_MIGRATION)

        with connection.cursor() as cursor:
            _assert_000052_state(cursor)
            indexdef_after, _valid_after = _river_segment_id_trgm_index(cursor)
            assert indexdef_after == indexdef_before
            assert _autovacuum_analyze_options(cursor) == options_before
            cursor.execute(
                "SELECT version FROM public.schema_migrations WHERE version LIKE '000052%'"
            )
            assert [row[0] for row in cursor.fetchall()] == [AUTHORITY_STATS_HYGIENE_MIGRATION]
    finally:
        connection.close()
