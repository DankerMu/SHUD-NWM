import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"

EXPECTED_MIGRATIONS = [
    "000001_extensions.sql",
    "000002_schemas.sql",
    "000003_enums.sql",
    "000004_core.sql",
    "000005_met.sql",
    "000006_hydro.sql",
    "000007_flood.sql",
    "000008_map.sql",
    "000009_ops.sql",
    "000010_indexes.sql",
    "000011_pipeline_job_model_id.sql",
    "000012_pipeline_job_array_task.sql",
    "000013_enum_remediation.sql",
    "000014_best_available_lineage.sql",
    "000015_flood_return_period_identity_indexes.sql",
    "000016_river_segment_pagination_indexes.sql",
    "000017_return_period_max_over_window_identity.sql",
    "000018_tile_cache_m16_contract.sql",
    "000019_hydro_mvt_identity_lookup_idx.sql",
    "000020_valid_time_discovery_indexes.sql",
    "000021_latest_ready_run_discovery_idx.sql",
    "000022_model_asset_lifecycle.sql",
    "000023_interp_weight_grid_signature.sql",
    "000024_qhh_latest_display_product_indexes.sql",
    "000025_active_manual_retry_guard.sql",
    "000026_ops_strict_identity_indexes.sql",
    "000027_cycle_status_canonical_incomplete.sql",
    "000028_state_lineage.sql",
    "000029_pipeline_reservation.sql",
    "000030_qhh_latest_display_parsed_status_index.sql",
    "000031_search_discovery_return_period_performance.sql",
    "000032_source_specific_state_snapshot.sql",
    "000033_station_mvt_active_source_index.sql",
    "000034_return_period_run_quality_materialization.sql",
    "000035_qhh_display_coverage_materialization.sql",
    "000036_run_product_quality_explicit_source.sql",
    "000037_river_segment_multilinestring.sql",
    "000038_direct_grid_interp_weight_constraints.sql",
    "000039_crosswalk_external_identity.sql",
]

EXPECTED_SCHEMAS = {"core", "met", "hydro", "flood", "map", "ops"}
EXPECTED_TABLES = {
    "core.basin",
    "core.basin_version",
    "core.river_network_version",
    "core.river_segment",
    "core.mesh_version",
    "core.river_segment_crosswalk",
    "core.model_instance",
    "met.data_source",
    "met.forecast_cycle",
    "met.canonical_met_product",
    "met.met_station",
    "met.interp_weight",
    "met.forcing_version",
    "met.forcing_version_component",
    "met.forcing_station_timeseries",
    "met.best_available_selection",
    "hydro.hydro_run",
    "hydro.state_snapshot",
    "hydro.river_timeseries",
    "flood.flood_frequency_curve",
    "flood.return_period_result",
    "flood.run_product_quality",
    "hydro.run_display_coverage",
    "map.tile_layer",
    "map.tile_cache",
    "ops.pipeline_job",
    "ops.pipeline_event",
    "ops.qc_result",
    "ops.audit_log",
}
EXPECTED_TYPES = {"hydro.run_type", "hydro.run_status", "met.source_status", "met.cycle_status"}


def _migration_sql() -> list[tuple[str, str]]:
    return [(path.name, path.read_text(encoding="utf-8")) for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]


def test_all_migration_files_exist_with_expected_names() -> None:
    migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]

    assert migration_names == EXPECTED_MIGRATIONS


def test_migration_files_are_non_empty_sql() -> None:
    required_keywords = ("create", "select", "do", "alter")

    for migration_name, sql in _migration_sql():
        normalized = sql.strip().lower()

        assert normalized, f"{migration_name} is empty"
        assert normalized.endswith(";"), f"{migration_name} should end with a SQL statement terminator"
        assert any(keyword in normalized for keyword in required_keywords), f"{migration_name} has no SQL keywords"


def test_migration_dependency_order() -> None:
    migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]

    assert migration_names.index("000001_extensions.sql") < migration_names.index("000002_schemas.sql")
    assert migration_names.index("000002_schemas.sql") < migration_names.index("000003_enums.sql")
    assert migration_names.index("000003_enums.sql") < migration_names.index("000004_core.sql")
    assert migration_names.index("000004_core.sql") < migration_names.index("000005_met.sql")
    assert migration_names.index("000005_met.sql") < migration_names.index("000006_hydro.sql")
    assert migration_names.index("000006_hydro.sql") < migration_names.index("000007_flood.sql")


def test_migrations_do_not_reference_future_objects() -> None:
    created_schemas: set[str] = set()
    created_tables: set[str] = set()
    created_types: set[str] = set()
    built_in_functions = {"create_hypertable", "now"}
    data_types = {"geometry", "jsonb", "timestamptz", "inet"}

    for migration_name, sql in _migration_sql():
        lower_sql = sql.lower()

        for schema in re.findall(r"\bcreate\s+schema\s+if\s+not\s+exists\s+([a-z_][a-z0-9_]*)", lower_sql):
            created_schemas.add(schema)

        for schema, type_name in re.findall(r"\bcreate\s+type\s+([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)", lower_sql):
            assert schema in created_schemas, f"{migration_name} creates type in missing schema {schema}"
            created_types.add(f"{schema}.{type_name}")

        for schema, table in re.findall(
            r"\bcreate\s+table\s+if\s+not\s+exists\s+([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)",
            lower_sql,
        ):
            assert schema in created_schemas, f"{migration_name} creates table in missing schema {schema}"
            created_tables.add(f"{schema}.{table}")

        for schema, table in re.findall(r"\breferences\s+([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", lower_sql):
            referenced_table = f"{schema}.{table}"
            assert referenced_table in created_tables, f"{migration_name} references missing table {referenced_table}"

        for schema, table in re.findall(r"\bon\s+([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", lower_sql):
            referenced_table = f"{schema}.{table}"
            assert referenced_table in created_tables, f"{migration_name} indexes missing table {referenced_table}"

        for schema, table in re.findall(r"create_hypertable\('([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)'", lower_sql):
            referenced_table = f"{schema}.{table}"
            assert referenced_table in created_tables, f"{migration_name} converts missing table {referenced_table}"

        for schema, type_name in re.findall(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\s+not\s+null", lower_sql):
            qualified_name = f"{schema}.{type_name}"
            if schema in created_schemas and type_name not in data_types and type_name not in built_in_functions:
                assert qualified_name in created_types, f"{migration_name} uses missing enum {qualified_name}"

    assert created_schemas == EXPECTED_SCHEMAS
    assert created_tables == EXPECTED_TABLES
    assert created_types == EXPECTED_TYPES


def test_flood_return_period_result_has_versioned_identity_and_hot_path_indexes() -> None:
    migration_sql = dict(_migration_sql())
    initial_schema = migration_sql["000007_flood.sql"]
    repair_schema = migration_sql["000015_flood_return_period_identity_indexes.sql"]
    max_over_window_schema = migration_sql["000017_return_period_max_over_window_identity.sql"]

    expected_versioned_primary_key = (
        "PRIMARY KEY (run_id, river_network_version_id, river_segment_id, duration, valid_time)"
    )
    expected_max_over_window_primary_key = (
        "PRIMARY KEY (run_id, river_network_version_id, river_segment_id, duration, valid_time, max_over_window)"
    )
    assert expected_versioned_primary_key in initial_schema
    assert expected_versioned_primary_key in repair_schema
    assert expected_max_over_window_primary_key in max_over_window_schema
    assert "ALTER COLUMN max_over_window SET NOT NULL" in max_over_window_schema

    for index_name in (
        "return_period_result_summary_idx",
        "return_period_result_ranking_idx",
        "return_period_result_valid_time_ranking_idx",
        "return_period_result_timeline_idx",
        "return_period_result_map_idx",
    ):
        assert index_name in repair_schema

    expected_valid_time_prefix = (
        "run_id,\n"
        "    valid_time,\n"
        "    max_over_window,\n"
        "    quality_flag,\n"
        "    return_period DESC NULLS LAST"
    )
    assert expected_valid_time_prefix in repair_schema


def test_flood_return_period_repair_migration_preflights_duplicate_versioned_rows() -> None:
    repair_schema = dict(_migration_sql())["000015_flood_return_period_identity_indexes.sql"]

    preflight_position = repair_schema.index("duplicate versioned return-period rows exist")
    drop_position = repair_schema.index("ALTER TABLE flood.return_period_result DROP CONSTRAINT")

    assert preflight_position < drop_position
    assert "GROUP BY run_id, river_network_version_id, river_segment_id, duration, valid_time" in repair_schema
    assert "HAVING COUNT(*) > 1" in repair_schema
    assert "Deduplicate or quarantine duplicate return-period rows before applying migration 000015" in repair_schema
    assert "IF NOT EXISTS" in repair_schema


def test_flood_return_period_max_over_window_migration_preflights_duplicate_rows() -> None:
    migration = dict(_migration_sql())["000017_return_period_max_over_window_identity.sql"]

    preflight_position = migration.index("duplicate max-over-window return-period rows exist")
    drop_position = migration.index("ALTER TABLE flood.return_period_result DROP CONSTRAINT")

    assert preflight_position < drop_position
    assert (
        "GROUP BY run_id, river_network_version_id, river_segment_id, duration, valid_time, max_over_window"
        in migration
    )
    assert "HAVING COUNT(*) > 1" in migration
    assert "Deduplicate or quarantine duplicate return-period rows before applying migration 000017" in migration
    assert "IF NOT EXISTS" in migration


def test_river_segment_pagination_migration_adds_lookup_indexes() -> None:
    migration = dict(_migration_sql())["000016_river_segment_pagination_indexes.sql"]

    assert "CREATE INDEX IF NOT EXISTS river_segment_network_order_idx" in migration
    assert "ON core.river_segment (river_network_version_id, segment_order, river_segment_id)" in migration
    assert "CREATE INDEX IF NOT EXISTS river_network_version_basin_lookup_idx" in migration
    assert "ON core.river_network_version (basin_version_id, river_network_version_id)" in migration


def test_river_network_public_identity_lookup_uses_indexed_version_table() -> None:
    migration = dict(_migration_sql())["000016_river_segment_pagination_indexes.sql"]
    route_source = (Path(__file__).resolve().parents[1] / "apps" / "api" / "routes" / "flood_alerts.py").read_text(
        encoding="utf-8"
    )

    function_source = route_source[
        route_source.index("def _river_network_source_version") : route_source.index(
            "def _require_hydro_mvt_source_identity"
        )
    ]
    assert "FROM core.river_network_version" in function_source
    assert "WHERE basin_version_id = :basin_version_id" in function_source
    assert "FROM core.model_instance" not in function_source
    assert "ON core.river_network_version (basin_version_id, river_network_version_id)" in migration


def test_tile_cache_m16_migration_upgrades_preexisting_cache_contract() -> None:
    migration = dict(_migration_sql())["000018_tile_cache_m16_contract.sql"]

    for expected in (
        "ADD COLUMN IF NOT EXISTS cache_key TEXT",
        "ADD COLUMN IF NOT EXISTS checksum TEXT",
        "ADD COLUMN IF NOT EXISTS source_id TEXT",
        "ADD COLUMN IF NOT EXISTS source_version TEXT",
        "ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ready'",
        "SET cache_key = NULL",
        "SET tile_uri = NULL",
        "SET cache_key = tile_uri",
        "jsonb_build_object",
        "'legacy_identity', 'map.tile_cache'",
        "digest(",
        "'sha256'",
        "Duplicate tile cache cache_key rows exist after deterministic M16 backfill",
        "Deduplicate or quarantine duplicate cache rows before applying migration 000018",
        "ALTER COLUMN cache_key SET NOT NULL",
        "ALTER TABLE map.tile_cache DROP CONSTRAINT",
        "CREATE UNIQUE INDEX IF NOT EXISTS tile_cache_cache_key_uidx ON map.tile_cache (cache_key)",
    ):
        assert expected in migration

    assert migration.index("ADD COLUMN IF NOT EXISTS cache_key TEXT") < migration.index(
        "UPDATE map.tile_cache\nSET cache_key = NULL"
    )
    assert migration.index("SET cache_key = tile_uri") < migration.index("jsonb_build_object")
    assert migration.index("jsonb_build_object") < migration.index(
        "Duplicate tile cache cache_key rows exist after deterministic M16 backfill"
    )
    assert (
        migration.index("Duplicate tile cache cache_key rows exist after deterministic M16 backfill")
        < migration.index("ALTER COLUMN cache_key SET NOT NULL")
    )
    assert migration.index("ALTER COLUMN cache_key SET NOT NULL") < migration.index(
        "CREATE UNIQUE INDEX IF NOT EXISTS tile_cache_cache_key_uidx"
    )


def test_hydro_mvt_identity_migration_adds_ordered_lookup_index() -> None:
    migration = dict(_migration_sql())["000019_hydro_mvt_identity_lookup_idx.sql"]

    assert "CREATE INDEX IF NOT EXISTS river_timeseries_mvt_identity_lookup_idx" in migration
    assert (
        "ON hydro.river_timeseries (run_id, variable, valid_time, river_network_version_id, river_segment_id)"
        in migration
    )
    assert migration.index("run_id") < migration.index("variable") < migration.index("valid_time")


def test_hydro_mvt_identity_index_protects_public_valid_time_lookup_contract() -> None:
    migration_sql = dict(_migration_sql())
    initial_schema = migration_sql["000006_hydro.sql"]
    identity_migration = migration_sql["000019_hydro_mvt_identity_lookup_idx.sql"]

    assert "PRIMARY KEY (run_id, river_network_version_id, river_segment_id, variable, valid_time)" in initial_schema
    assert "river_ts_segment_time_idx" not in identity_migration
    assert "river_timeseries_mvt_identity_lookup_idx" in identity_migration

    public_identity_columns = ("run_id", "variable", "valid_time")
    indexed_columns = re.search(r"ON hydro\.river_timeseries \(([^)]+)\)", identity_migration)
    assert indexed_columns is not None
    ordered_columns = tuple(column.strip() for column in indexed_columns.group(1).split(","))
    assert ordered_columns[:3] == public_identity_columns
    assert ordered_columns[3:] == ("river_network_version_id", "river_segment_id")


def test_valid_time_discovery_migration_adds_dedicated_ordered_indexes() -> None:
    migration = dict(_migration_sql())["000020_valid_time_discovery_indexes.sql"]

    flood_columns = _index_columns(migration, "flood", "return_period_result")
    hydro_columns = _index_columns(migration, "hydro", "river_timeseries")

    assert "CREATE INDEX IF NOT EXISTS return_period_result_valid_time_discovery_idx" in migration
    assert flood_columns == ("run_id", "duration", "max_over_window", "valid_time DESC")
    assert "CREATE INDEX IF NOT EXISTS river_timeseries_valid_time_discovery_idx" in migration
    assert hydro_columns == ("run_id", "variable", "valid_time DESC")


def test_model_asset_lifecycle_migration_prevents_active_state_drift() -> None:
    migration = dict(_migration_sql())["000022_model_asset_lifecycle.sql"]

    assert "model_instance_active_lifecycle_consistency_chk" in migration
    assert "active_flag = true AND lifecycle_state <> 'active'" in migration
    assert "lifecycle_state = 'active' AND active_flag <> true" in migration
    assert "active_flag = true AND lifecycle_state = 'active'" in migration
    assert "active_flag = false AND lifecycle_state <> 'active'" in migration
    assert "WHERE active_flag = true AND lifecycle_state = 'active'" in migration


def test_latest_ready_run_discovery_migration_matches_query_predicate_and_order() -> None:
    migration = dict(_migration_sql())["000021_latest_ready_run_discovery_idx.sql"]
    mvt_source = (Path(__file__).resolve().parents[1] / "services" / "tiles" / "mvt.py").read_text(
        encoding="utf-8"
    )
    function_source = mvt_source[
        mvt_source.index("def latest_ready_run") : mvt_source.index("def valid_times_for_layer")
    ]

    assert "CREATE INDEX IF NOT EXISTS hydro_run_latest_ready_run_idx" in migration
    assert "ON hydro.hydro_run (cycle_time DESC, run_id DESC)" in migration
    assert "WHERE status IN ('frequency_done', 'published')" in migration
    assert "WHERE h.status IN ('frequency_done', 'published')" in function_source
    assert "ORDER BY h.cycle_time DESC, h.run_id DESC" in function_source
    assert "LIMIT 1" in function_source


def test_selected_run_mvt_identity_migration_matches_strict_preflight_predicates() -> None:
    migration = dict(_migration_sql())["000021_latest_ready_run_discovery_idx.sql"]
    route_source = (Path(__file__).resolve().parents[1] / "apps" / "api" / "routes" / "flood_alerts.py").read_text(
        encoding="utf-8"
    )

    hydro_preflight = route_source[
        route_source.index("def _require_hydro_mvt_source_identity") : route_source.index(
            "def _require_flood_mvt_source_identity"
        )
    ]
    flood_preflight = route_source[
        route_source.index("def _require_flood_mvt_source_identity") : route_source.index(
            "def _require_run_source_identity"
        )
    ]
    hydro_columns = _index_columns_by_name(migration, "river_timeseries_mvt_selected_identity_lookup_idx")
    flood_columns = _index_columns_by_name(migration, "return_period_result_mvt_selected_identity_lookup_idx")

    assert hydro_columns[:5] == (
        "run_id",
        "basin_version_id",
        "river_network_version_id",
        "variable",
        "valid_time",
    )
    assert flood_columns[:6] == (
        "run_id",
        "basin_version_id",
        "river_network_version_id",
        "duration",
        "max_over_window",
        "valid_time",
    )
    for expected in (
        "run_id = :run_id",
        "basin_version_id = :basin_version_id",
        "river_network_version_id = :river_network_version_id",
    ):
        assert expected in hydro_preflight
        assert expected in flood_preflight
    assert "variable = :variable" in hydro_preflight
    assert "duration = :duration" in flood_preflight
    assert "max_over_window = false" in flood_preflight


def test_selected_run_valid_time_discovery_migration_matches_strict_identity_predicates() -> None:
    migration = dict(_migration_sql())["000021_latest_ready_run_discovery_idx.sql"]
    mvt_source = (Path(__file__).resolve().parents[1] / "services" / "tiles" / "mvt.py").read_text(
        encoding="utf-8"
    )
    valid_time_source = mvt_source[
        mvt_source.index("def valid_times_for_layer") : mvt_source.index("def _valid_time_discovery")
    ]
    hydro_columns = _index_columns_by_name(
        migration,
        "river_timeseries_mvt_selected_identity_valid_time_discovery_idx",
    )
    flood_columns = _index_columns_by_name(
        migration,
        "return_period_result_mvt_selected_identity_valid_time_discovery_idx",
    )

    assert hydro_columns == (
        "run_id",
        "basin_version_id",
        "river_network_version_id",
        "variable",
        "valid_time DESC",
    )
    assert flood_columns == (
        "run_id",
        "basin_version_id",
        "river_network_version_id",
        "duration",
        "max_over_window",
        "valid_time DESC",
    )
    for expected in (
        "run_id = :run_id",
        "basin_version_id = :basin_version_id",
        "river_network_version_id = :river_network_version_id",
    ):
        assert expected in valid_time_source
    assert "variable = :variable" in valid_time_source
    assert "duration = :duration" in valid_time_source
    assert "max_over_window = false" in valid_time_source
    assert "(:basin_version_id IS NULL OR basin_version_id = :basin_version_id)" not in valid_time_source
    assert "(:river_network_version_id IS NULL OR river_network_version_id = :river_network_version_id)" not in (
        valid_time_source
    )


def test_qhh_latest_display_product_migration_matches_candidate_and_window_queries() -> None:
    migration = dict(_migration_sql())["000024_qhh_latest_display_product_indexes.sql"]
    parsed_status_migration = dict(_migration_sql())["000030_qhh_latest_display_parsed_status_index.sql"]
    performance_migration = dict(_migration_sql())["000031_search_discovery_return_period_performance.sql"]
    store_source = (
        Path(__file__).resolve().parents[1] / "packages" / "common" / "forecast_store.py"
    ).read_text(encoding="utf-8")
    query_source = store_source[
        store_source.index("def _fetch_latest_qhh_display_candidates") : store_source.index(
            "def _fetch_station_for_series"
        )
    ]
    index_evidence_source = store_source[
        store_source.index("def _qhh_latest_query_indexes") : store_source.index("def _non_negative_int")
    ]
    flood_quality_join_source = store_source[
        store_source.index("def _flood_product_quality_join") : store_source.index("def _flood_product_quality_select")
    ]

    assert _index_columns_by_name(migration, "hydro_run_qhh_latest_candidate_idx") == (
        "LOWER(source_id)",
        "run_type",
        "basin_version_id",
        "cycle_time DESC",
        "run_id DESC",
    )
    assert "hydro_run_ops_strict_identity_candidates_idx" not in migration
    assert "WHERE cycle_time IS NOT NULL" in migration
    assert "AND status IN ('frequency_done', 'published')" in migration
    assert _index_columns_by_name(parsed_status_migration, "hydro_run_qhh_latest_candidate_parsed_idx") == (
        "LOWER(source_id)",
        "run_type",
        "basin_version_id",
        "cycle_time DESC",
        "run_id DESC",
    )
    assert "WHERE cycle_time IS NOT NULL" in parsed_status_migration
    assert (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_qhh_latest_candidate_parsed_idx"
        in parsed_status_migration
    )
    assert "AND status IN ('parsed', 'frequency_done', 'published')" in parsed_status_migration
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_display_product_basin_status_idx" in performance_migration
    assert "ON hydro.hydro_run (basin_version_id, status)" in performance_migration
    assert "WHERE status IN ('parsed', 'frequency_done', 'published')" in performance_migration
    assert _index_columns_by_name(migration, "basin_version_qhh_latest_lookup_idx") == (
        "basin_id",
        "basin_version_id",
    )
    assert _index_columns_by_name(migration, "forcing_station_timeseries_qhh_latest_window_idx") == (
        "forcing_version_id",
        "basin_version_id",
        "LOWER(source_id)",
        "variable",
        "valid_time DESC",
        "station_id",
    )
    assert _index_columns_by_name(migration, "interp_weight_qhh_latest_membership_idx") == (
        "model_id",
        "station_id",
        "variable",
        "LOWER(source_id)",
    )
    assert _index_columns_by_name(migration, "river_timeseries_qhh_latest_window_idx") == (
        "run_id",
        "basin_version_id",
        "river_network_version_id",
        "variable",
        "valid_time DESC",
        "river_segment_id",
    )
    for index_name in (
        "hydro_run_qhh_latest_candidate_idx",
        "basin_version_qhh_latest_lookup_idx",
        "forcing_station_timeseries_qhh_latest_window_idx",
        "interp_weight_qhh_latest_membership_idx",
        "river_timeseries_qhh_latest_window_idx",
    ):
        assert index_name in index_evidence_source
    assert "run_product_quality_pkey" in index_evidence_source

    assert "LOWER(h.source_id) = LOWER(%s)" in query_source
    assert "h.run_type = 'forecast'" in query_source
    assert "h.status IN ('parsed', 'frequency_done', 'published')" in query_source
    assert "h.status NOT IN ('parsed', 'frequency_done', 'published')" in query_source
    assert "h.cycle_time IS NOT NULL" in query_source
    # #5 起 _flood_product_quality_join 增加 node-27 缺表 fallback 分支（available=False
    # 用 LATERAL 聚合 flood.return_period_result），函数源码因此合法含 LATERAL/GROUP BY 字样。
    # "默认走 run_product_quality 物化表、不退化为聚合" 的双分支契约改由
    # tests/test_forecast_store_product_quality_sql.py 锁定；此处只确认物化 join 仍在源码中。
    assert "LEFT JOIN flood.run_product_quality" in flood_quality_join_source
    assert "ON {alias}.run_id = h.run_id" in flood_quality_join_source
    assert "QHH_LATEST_SEARCH_LIMIT" in query_source
    assert "QHH_LATEST_CONTEXT_LIMIT" in query_source
    assert "QHH_LATEST_EXPECTED_HORIZON_HOURS" in query_source
    assert "fst.basin_version_id = cr.basin_version_id" in query_source
    assert "LOWER(fst.source_id) = LOWER(cr.source_id)" in query_source
    assert "FROM met.interp_weight iw" in query_source
    assert "iw.model_id = cr.model_id" in query_source
    assert "iw.station_id = fst.station_id" in query_source
    assert "cr.run_id," in query_source
    assert "cr.model_id," in query_source
    assert "cr.display_start_time," in query_source
    assert "cr.display_end_time," in query_source
    assert "station_identity_coverage AS" in query_source
    assert "station_time_coverage AS" in query_source
    assert "station_variable_complete_times AS" in query_source
    assert "station_variable_common_times AS" in query_source
    assert "station_all_variable_complete_times AS" in query_source
    assert "variable,\n                    station_id" in query_source
    assert "cr.expected_station_count" in query_source
    assert "station_count = expected_station_count" in query_source
    assert "COUNT(DISTINCT variable) AS complete_variable_count" in query_source
    assert "HAVING COUNT(DISTINCT variable) = %s" in query_source
    assert "MIN(valid_time) AS valid_time_start" in query_source
    assert "MAX(valid_time) AS valid_time_end" in query_source
    assert "MIN(valid_time) AS station_valid_time_start" in query_source
    assert "MAX(valid_time) AS station_valid_time_end" in query_source
    assert "MAX(valid_time_start) AS station_valid_time_start" not in query_source
    assert "MIN(valid_time_end) AS station_valid_time_end" not in query_source
    assert "ON sc.run_id = cr.run_id" in query_source
    assert "AND sc.model_id = cr.model_id" in query_source
    assert "AND sc.display_start_time = cr.display_start_time" in query_source
    assert "AND sc.display_end_time = cr.display_end_time" in query_source
    assert "ON svc.run_id = cr.run_id" in query_source
    assert "AND svc.model_id = cr.model_id" in query_source
    assert "AND svc.display_start_time = cr.display_start_time" in query_source
    assert "AND svc.display_end_time = cr.display_end_time" in query_source
    assert "river_identity_coverage AS" in query_source
    assert "river_time_coverage AS" in query_source
    assert "river_common_window AS" in query_source
    assert "river_segment_id" in query_source
    assert "cr.expected_segment_count" in query_source
    assert "segment_count = expected_segment_count" in query_source
    assert "MIN(valid_time) AS river_valid_time_start" in query_source
    assert "MAX(valid_time) AS river_valid_time_end" in query_source
    assert "GREATEST(h.cycle_time, h.start_time, fv.start_time) AS display_start_time" in query_source
    assert "h.cycle_time + (%s * INTERVAL '1 hour')" in query_source
    assert "fst.valid_time >= cr.display_start_time" in query_source
    assert "fst.valid_time <= cr.display_end_time" in query_source
    assert "rt.valid_time >= cr.display_start_time" in query_source
    assert "rt.valid_time <= cr.display_end_time" in query_source


def test_interp_weight_grid_signature_migration_is_historical_column_only_migration() -> None:
    migration = dict(_migration_sql())["000023_interp_weight_grid_signature.sql"]

    assert "ADD COLUMN IF NOT EXISTS grid_signature TEXT" in migration
    assert "interp_weight_direct_grid_exact_weight_chk" not in migration
    assert "interp_weight_direct_grid_signature_chk" not in migration
    assert "interp_weight_direct_grid_station_variable_uidx" not in migration


def test_direct_grid_interp_weight_constraints_forward_migration_supports_persistence_contract() -> None:
    migration = dict(_migration_sql())["000038_direct_grid_interp_weight_constraints.sql"]

    assert "ADD COLUMN IF NOT EXISTS grid_signature TEXT" not in migration
    assert "FROM pg_constraint" in migration
    assert "interp_weight_direct_grid_exact_weight_chk" in migration
    assert "ADD CONSTRAINT interp_weight_direct_grid_exact_weight_chk" in migration
    assert "CHECK (method <> 'direct_grid' OR weight = 1.0)" in migration
    assert "interp_weight_direct_grid_signature_chk" in migration
    assert "ADD CONSTRAINT interp_weight_direct_grid_signature_chk" in migration
    assert "CHECK (method <> 'direct_grid' OR NULLIF(BTRIM(grid_signature), '') IS NOT NULL)" in migration
    assert "CREATE UNIQUE INDEX IF NOT EXISTS interp_weight_direct_grid_station_variable_uidx" in migration
    assert _index_columns_by_name(migration, "interp_weight_direct_grid_station_variable_uidx") == (
        "source_id",
        "grid_id",
        "model_id",
        "station_id",
        "variable",
    )
    assert "WHERE method = 'direct_grid'" in _index_sql_by_name(
        migration,
        "interp_weight_direct_grid_station_variable_uidx",
    )


def test_search_discovery_performance_migration_adds_trgm_and_quality_indexes() -> None:
    migration = dict(_migration_sql())["000031_search_discovery_return_period_performance.sql"]

    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in migration
    for index_name in (
        "river_segment_id_trgm_idx",
        "river_segment_name_trgm_idx",
        "river_segment_segment_name_trgm_idx",
        "met_station_id_trgm_idx",
        "met_station_name_trgm_idx",
        "hydro_run_display_product_basin_status_idx",
    ):
        assert f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}" in migration
    assert "CREATE INDEX IF NOT EXISTS return_period_result_run_quality_idx" in migration
    assert migration.count("USING GIN") == 5
    assert "gin_trgm_ops" in migration
    assert "WHERE active_flag = true" in migration
    assert "met_station_active_basin_station_idx" not in migration
    assert "ON hydro.hydro_run (basin_version_id, status)" in migration
    assert "ON flood.return_period_result (run_id, max_over_window, return_period, warning_level)" in migration


def test_station_mvt_active_source_index_migration_is_forward_upgrade_safe() -> None:
    migration_sql = dict(_migration_sql())
    migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    migration = migration_sql["000033_station_mvt_active_source_index.sql"]

    assert migration_names.index("000032_source_specific_state_snapshot.sql") < migration_names.index(
        "000033_station_mvt_active_source_index.sql"
    )
    assert "met_station_active_basin_station_idx" not in migration_sql[
        "000031_search_discovery_return_period_performance.sql"
    ]
    assert _index_columns_by_name(migration, "met_station_active_basin_station_idx") == (
        "basin_version_id",
        "station_id",
    )
    active_station_index = _index_sql_by_name(migration, "met_station_active_basin_station_idx")
    assert active_station_index.startswith(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS met_station_active_basin_station_idx"
    )
    assert "ON met.met_station (basin_version_id, station_id)" in active_station_index
    assert "WHERE active_flag = true" in active_station_index
    assert "USING GIN" not in active_station_index


def test_run_quality_materialization_adds_explicit_quality_source_without_null_indexes() -> None:
    migration_sql = dict(_migration_sql())
    migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    migration = migration_sql["000034_return_period_run_quality_materialization.sql"]
    explicit_migration = migration_sql["000036_run_product_quality_explicit_source.sql"]

    assert migration_names.index("000033_station_mvt_active_source_index.sql") < migration_names.index(
        "000034_return_period_run_quality_materialization.sql"
    )
    assert migration_names.index("000035_qhh_display_coverage_materialization.sql") < migration_names.index(
        "000036_run_product_quality_explicit_source.sql"
    )
    assert "CREATE TABLE IF NOT EXISTS flood.run_product_quality" in migration
    assert "run_id TEXT PRIMARY KEY REFERENCES hydro.hydro_run(run_id) ON DELETE CASCADE" in migration
    for column in (
        "quality_state TEXT NOT NULL DEFAULT 'ready'",
        "quality_source TEXT NOT NULL DEFAULT 'historical_backfill'",
        "unavailable_products JSONB NOT NULL DEFAULT '[]'::jsonb",
        "residual_blockers JSONB NOT NULL DEFAULT '[]'::jsonb",
        "result_rows BIGINT NOT NULL DEFAULT 0",
        "max_result_rows BIGINT NOT NULL DEFAULT 0",
        "return_period_rows BIGINT NOT NULL DEFAULT 0",
        "warning_rows BIGINT NOT NULL DEFAULT 0",
        "max_return_period_rows BIGINT NOT NULL DEFAULT 0",
        "max_warning_rows BIGINT NOT NULL DEFAULT 0",
        "expected_result_rows BIGINT NOT NULL DEFAULT 0",
        "expected_max_result_rows BIGINT NOT NULL DEFAULT 0",
        "expected_timestep_result_rows BIGINT NOT NULL DEFAULT 0",
        "meaningful_result_rows BIGINT NOT NULL DEFAULT 0",
        "meaningful_max_result_rows BIGINT NOT NULL DEFAULT 0",
        "meaningful_timestep_result_rows BIGINT NOT NULL DEFAULT 0",
        "no_frequency_curve_rows BIGINT NOT NULL DEFAULT 0",
        "no_usable_frequency_curve_rows BIGINT NOT NULL DEFAULT 0",
        "warning_threshold_unavailable_rows BIGINT NOT NULL DEFAULT 0",
        "refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    ):
        assert column in migration
    assert "CONSTRAINT run_product_quality_unavailable_products_array_chk" in migration
    assert "CHECK (jsonb_typeof(unavailable_products) = 'array')" in migration
    assert "CONSTRAINT run_product_quality_residual_blockers_array_chk" in migration
    assert "CHECK (jsonb_typeof(residual_blockers) = 'array')" in migration

    for index_name in (
        "return_period_result_null_return_period_run_idx",
        "return_period_result_null_warning_level_run_idx",
    ):
        assert index_name not in migration
        assert index_name not in explicit_migration

    assert "ALTER TABLE flood.run_product_quality" in explicit_migration
    assert "ADD COLUMN IF NOT EXISTS quality_state TEXT NOT NULL DEFAULT 'ready'" in explicit_migration
    assert "ADD COLUMN IF NOT EXISTS residual_blockers JSONB NOT NULL DEFAULT '[]'::jsonb" in explicit_migration
    assert "DO $$" in explicit_migration
    assert "FROM pg_constraint" in explicit_migration
    assert "ADD CONSTRAINT run_product_quality_unavailable_products_array_chk" in explicit_migration
    assert "ADD CONSTRAINT run_product_quality_residual_blockers_array_chk" in explicit_migration
    assert "WITH source_quality AS" in explicit_migration
    assert "SUM(CASE WHEN quality_flag = 'no_frequency_curve' THEN 1 ELSE 0 END)" in explicit_migration
    assert "SUM(CASE WHEN quality_flag = 'no_usable_frequency_curve' THEN 1 ELSE 0 END)" in explicit_migration
    assert "SUM(CASE WHEN quality_flag = 'warning_thresholds_unavailable' THEN 1 ELSE 0 END)" in explicit_migration
    assert "jsonb_array_elements_text(backfill.unavailable_products)" in explicit_migration
    assert "|| backfill.residual_blockers" in explicit_migration
    assert "WHEN unavailable_products <> '[]'::jsonb THEN unavailable_products" not in explicit_migration
    assert "jsonb_build_object" in explicit_migration


def test_ops_strict_identity_index_migration_is_forward_upgrade_safe() -> None:
    migration_sql = dict(_migration_sql())
    migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    migration = migration_sql["000026_ops_strict_identity_indexes.sql"]

    assert migration_names.index("000024_qhh_latest_display_product_indexes.sql") < migration_names.index(
        "000026_ops_strict_identity_indexes.sql"
    )
    assert migration_names.index("000025_active_manual_retry_guard.sql") < migration_names.index(
        "000026_ops_strict_identity_indexes.sql"
    )
    assert "hydro_run_ops_strict_identity_candidates_idx" not in migration_sql[
        "000024_qhh_latest_display_product_indexes.sql"
    ]
    assert "CREATE INDEX IF NOT EXISTS hydro_run_ops_strict_identity_candidates_idx" in migration
    assert _index_columns_by_name(migration, "hydro_run_ops_strict_identity_candidates_idx") == (
        "source_id",
        "cycle_time",
        "run_id",
        "model_id",
    )


def test_fresh_tile_cache_schema_requires_non_null_cache_key_identity() -> None:
    migration = dict(_migration_sql())["000008_map.sql"]
    tile_cache = migration[migration.index("CREATE TABLE IF NOT EXISTS map.tile_cache") :]

    assert "cache_key TEXT NOT NULL" in tile_cache
    assert "PRIMARY KEY (cache_key)" in tile_cache


def test_active_manual_retry_guard_is_run_level_active_marker_invariant() -> None:
    migration = dict(_migration_sql())["000025_active_manual_retry_guard.sql"]

    assert "ADD COLUMN IF NOT EXISTS manual_retry_marker BOOLEAN NOT NULL DEFAULT false" in migration
    assert "WITH ranked_active_legacy_retries AS" in migration
    assert "row_number() OVER" in migration
    assert "PARTITION BY run_id" in migration
    assert "retry_rank" in migration
    assert "ranked.retry_rank = 1" in migration
    assert "UPDATE ops.pipeline_job AS job" in migration
    assert "substr(job_id, 1, length(run_id || '_retry_')) = run_id || '_retry_'" in migration
    assert "job_id LIKE run_id || '_retry_%'" not in migration
    assert "CREATE UNIQUE INDEX IF NOT EXISTS pipeline_job_active_manual_retry_guard_idx" in migration
    assert "ON ops.pipeline_job (run_id)" in migration
    assert "manual_retry_marker IS true" in migration
    assert "run_id IS NOT NULL" in migration
    assert "status IN ('pending', 'queued', 'submitted', 'running')" in migration
    assert "job_id = run_id || '_retry_active'" not in migration


def test_active_manual_retry_guard_backfill_is_duplicate_safe_before_index() -> None:
    migration = dict(_migration_sql())["000025_active_manual_retry_guard.sql"]

    ranked_position = migration.index("WITH ranked_active_legacy_retries AS")
    update_position = migration.index("UPDATE ops.pipeline_job AS job")
    index_position = migration.index("CREATE UNIQUE INDEX IF NOT EXISTS pipeline_job_active_manual_retry_guard_idx")

    assert ranked_position < update_position < index_position
    ranked_source = migration[ranked_position:index_position]
    assert "PARTITION BY run_id" in ranked_source
    for ordering in (
        "submitted_at DESC NULLS LAST",
        "created_at DESC NULLS LAST",
        "updated_at DESC NULLS LAST",
        "finished_at DESC NULLS LAST",
        "job_id DESC",
    ):
        assert ordering in ranked_source
    assert "status IN ('pending', 'queued', 'submitted', 'running')" in ranked_source
    assert "ranked.retry_rank = 1" in ranked_source


def test_active_manual_retry_guard_predicate_matches_runtime_guard() -> None:
    migration = dict(_migration_sql())["000025_active_manual_retry_guard.sql"]
    persistence_source = (
        Path(__file__).resolve().parents[1] / "services" / "orchestrator" / "persistence.py"
    ).read_text(encoding="utf-8")

    index_source = migration[migration.index("CREATE UNIQUE INDEX IF NOT EXISTS") :]
    assert "manual_retry_marker IS true" in index_source
    assert "run_id IS NOT NULL" in index_source
    assert "status IN ('pending', 'queued', 'submitted', 'running')" in index_source
    assert 'ACTIVE_MANUAL_RETRY_STATUSES = ("pending", "queued", "submitted", "running")' in persistence_source
    assert "PipelineJob.manual_retry_marker.is_(True)" in persistence_source
    assert "PipelineJob.run_id.is_not(None)" in persistence_source
    assert "PipelineJob.status.in_(ACTIVE_MANUAL_RETRY_STATUSES)" in persistence_source


def test_pipeline_reservation_partial_unique_index_matches_runtime_orm() -> None:
    """Migration 000029's partial unique index on ``idempotency_key`` must match
    the runtime ORM Index in persistence.py exactly: same index name, same
    ``idempotency_key IS NOT NULL`` predicate. If the migration and the ORM
    drift, the reservation protocol's at-most-once guard differs between fresh
    schema and migrated schema.
    """

    migration = dict(_migration_sql())["000029_pipeline_reservation.sql"]
    persistence_source = (
        Path(__file__).resolve().parents[1] / "services" / "orchestrator" / "persistence.py"
    ).read_text(encoding="utf-8")

    index_source = migration[migration.index("CREATE UNIQUE INDEX IF NOT EXISTS") :]
    # Partial unique index, predicate idempotency_key IS NOT NULL, shared name.
    assert "CREATE UNIQUE INDEX IF NOT EXISTS pipeline_job_idempotency_key_uidx" in migration
    assert "ON ops.pipeline_job (idempotency_key)" in index_source
    assert "WHERE idempotency_key IS NOT NULL" in index_source

    # Runtime ORM Index mirrors the same name + partial predicate.
    assert '"pipeline_job_idempotency_key_uidx"' in persistence_source
    assert "PipelineJob.idempotency_key," in persistence_source
    assert "unique=True" in persistence_source
    assert "PipelineJob.idempotency_key.is_not(None)" in persistence_source


def _index_columns(migration: str, schema: str, table: str) -> tuple[str, ...]:
    match = re.search(rf"ON {schema}\.{table} \(([^)]+)\)", migration)
    assert match is not None
    return tuple(column.strip() for column in match.group(1).split(","))


def _index_columns_by_name(migration: str, index_name: str) -> tuple[str, ...]:
    match = re.search(
        rf"CREATE (?:UNIQUE )?INDEX(?: CONCURRENTLY)? IF NOT EXISTS {index_name}\s+ON\s+",
        migration,
    )
    assert match is not None
    start = migration.index("(", match.end())
    depth = 0
    end = start
    for position in range(start, len(migration)):
        character = migration[position]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                end = position
                break
    assert end > start
    return tuple(re.sub(r"\s+", " ", column).strip() for column in _split_index_columns(migration[start + 1 : end]))


def _index_sql_by_name(migration: str, index_name: str) -> str:
    match = re.search(rf"CREATE (?:UNIQUE )?INDEX(?: CONCURRENTLY)? IF NOT EXISTS {index_name}\b", migration)
    assert match is not None
    end = migration.index(";", match.start())
    return re.sub(r"\s+", " ", migration[match.start() : end]).strip()


def _split_index_columns(columns_sql: str) -> list[str]:
    columns: list[str] = []
    depth = 0
    current: list[str] = []
    for character in columns_sql:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            columns.append("".join(current))
            current = []
            continue
        current.append(character)
    if current:
        columns.append("".join(current))
    return columns
