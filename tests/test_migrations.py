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
    required_keywords = ("create", "select", "do")

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
