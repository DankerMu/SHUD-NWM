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


def test_tile_cache_m16_migration_upgrades_preexisting_cache_contract() -> None:
    migration = dict(_migration_sql())["000018_tile_cache_m16_contract.sql"]

    for expected in (
        "ADD COLUMN IF NOT EXISTS cache_key TEXT",
        "ADD COLUMN IF NOT EXISTS checksum TEXT",
        "ADD COLUMN IF NOT EXISTS source_id TEXT",
        "ADD COLUMN IF NOT EXISTS source_version TEXT",
        "ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ready'",
        "SET cache_key = COALESCE(cache_key, tile_uri)",
        "ALTER TABLE map.tile_cache DROP CONSTRAINT",
        "CREATE UNIQUE INDEX IF NOT EXISTS tile_cache_cache_key_uidx ON map.tile_cache (cache_key)",
    ):
        assert expected in migration

    assert migration.index("ADD COLUMN IF NOT EXISTS cache_key TEXT") < migration.index(
        "CREATE UNIQUE INDEX IF NOT EXISTS tile_cache_cache_key_uidx"
    )
