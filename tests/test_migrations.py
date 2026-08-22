import re
from pathlib import Path

from packages.common.migrate import split_sql_statements
from tests.test_sql_shape_helpers import (
    FORBIDDEN_TEXT_FACT_COLUMNS,
    outer_predicates,
    sql_literals,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"

EXPECTED_MIGRATIONS = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]

EXPECTED_SCHEMAS = {"core", "met", "hydro", "map", "ops"}
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
    "met.canonical_grid_snapshot",
    "met.canonical_grid_cell",
    "met.met_station",
    "met.interp_weight",
    "met.forcing_version",
    "met.forcing_version_component",
    "met.forcing_station_timeseries",
    "met.best_available_selection",
    "hydro.hydro_run",
    "hydro.state_snapshot",
    "hydro.river_timeseries",
    "hydro.run_display_coverage",
    "map.tile_layer",
    "map.tile_cache",
    "ops.pipeline_job",
    "ops.pipeline_event",
    "ops.qc_result",
    "ops.audit_log",
}
EXPECTED_TYPES = {
    "hydro.run_type",
    "hydro.run_status",
    "met.source_status",
    "met.cycle_status",
    # 000050 (issue #1339): native enums replacing the repeated text identity
    # columns on hydro.river_timeseries.
    "hydro.river_variable",
    "hydro.river_unit",
    "hydro.river_quality_flag",
}


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
    assert migration_names.index("000006_hydro.sql") < migration_names.index("000008_map.sql")


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
    assert EXPECTED_TABLES <= created_tables
    assert created_types == EXPECTED_TYPES



def test_river_segment_pagination_migration_adds_lookup_indexes() -> None:
    migration = dict(_migration_sql())["000016_river_segment_pagination_indexes.sql"]

    assert "CREATE INDEX IF NOT EXISTS river_segment_network_order_idx" in migration
    assert "ON core.river_segment (river_network_version_id, segment_order, river_segment_id)" in migration
    assert "CREATE INDEX IF NOT EXISTS river_network_version_basin_lookup_idx" in migration
    assert "ON core.river_network_version (basin_version_id, river_network_version_id)" in migration


def test_river_network_public_identity_lookup_uses_indexed_version_table() -> None:
    migration = dict(_migration_sql())["000016_river_segment_pagination_indexes.sql"]
    route_source = (Path(__file__).resolve().parents[1] / "apps" / "api" / "routes" / "hydro_display.py").read_text(
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
        mvt_source.index("def display_ready_run") : mvt_source.index("def valid_times_for_layer")
    ]

    assert "CREATE INDEX IF NOT EXISTS hydro_run_latest_ready_run_idx" in migration
    assert "ON hydro.hydro_run (cycle_time DESC, run_id DESC)" in migration
    assert "WHERE h.status IN ('succeeded', 'parsed', 'published')" in function_source
    assert "ORDER BY h.cycle_time DESC, h.run_id DESC" in function_source
    assert "LIMIT 1" in function_source


def test_river_segment_stream_type_is_generated_and_indexed() -> None:
    migration = dict(_migration_sql())["000048_river_segment_stream_type.sql"]

    assert "ADD COLUMN IF NOT EXISTS stream_type DOUBLE PRECISION" in migration
    assert "GENERATED ALWAYS AS" in migration
    assert "properties_json ->> 'Type'" in migration
    assert "river_segment_network_stream_type_idx" in migration
    assert "river_network_version_id" in migration
    assert "stream_type DESC" in migration



DROP_REDUNDANT_RIVER_INDEX_MIGRATION = (
    "000049_drop_redundant_river_mvt_identity_and_valid_time_discovery_idx.sql"
)
DROPPED_RIVER_TIMESERIES_INDEXES = (
    "hydro.river_timeseries_mvt_identity_lookup_idx",
    "hydro.river_timeseries_valid_time_discovery_idx",
)
RETAINED_RIVER_TIMESERIES_INDEXES = (
    "river_timeseries_pkey",
    "river_ts_segment_time_idx",
    "river_timeseries_valid_time_idx",
    "river_timeseries_mvt_selected_identity_valid_time_discovery_idx",
    # 000051 (issue #1341): the integer discovery index that serves the switched
    # display-boundary reads. It joins the keep-list on day one — nothing may
    # drop it, and the text indexes above stay for rollback and for the
    # out-of-boundary text readers until #1342 retires them.
    "river_ts_selected_identity_key_valid_time_idx",
)
SURROGATE_KEY_READ_INDEX_MIGRATION = "000051_river_ts_surrogate_key_read_index.sql"

AUTHORITY_STATS_HYGIENE_MIGRATION = "000052_authority_stats_hygiene_trgm_expression_index.sql"


def test_drop_redundant_river_index_migration_drops_exactly_the_two_measured_indexes() -> None:
    migration_sql = dict(_migration_sql())
    migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    migration_name = DROP_REDUNDANT_RIVER_INDEX_MIGRATION

    assert migration_name in migration_sql, (
        f"{migration_name} must exist as the redundant-index drop migration for issue #1338"
    )
    assert migration_names.index("000048_river_segment_stream_type.sql") < migration_names.index(migration_name)
    assert [name for name in migration_names if name.startswith("000049")] == [migration_name], (
        "migration 000049 must be a single file with no duplicate numeric prefix"
    )

    migration = migration_sql[migration_name]

    # Comment-stripped body: the migration is exactly two CONCURRENTLY drops, nothing
    # else. Any extra statement (or a plain non-CONCURRENTLY drop, which would lock the
    # display read path on a 162 GB hypertable index) fails here.
    statements = [
        line.strip()
        for line in migration.splitlines()
        if line.strip() and not line.lstrip().startswith("--")
    ]
    assert statements == [
        f"DROP INDEX CONCURRENTLY IF EXISTS {index_name};" for index_name in DROPPED_RIVER_TIMESERIES_INDEXES
    ], f"{migration_name} must drop exactly the two measured indexes, found: {statements}"

    # Retained set (issue #1338 explicit keep-list) must never appear as a DROP target.
    for retained_index in RETAINED_RIVER_TIMESERIES_INDEXES:
        offending = [statement for statement in statements if retained_index in statement]
        assert not offending, f"{migration_name} must not drop retained index {retained_index}: {offending}"


def test_drop_redundant_river_index_migration_records_rollback_ddl_and_out_of_band_origin() -> None:
    migration = dict(_migration_sql())[DROP_REDUNDANT_RIVER_INDEX_MIGRATION]
    comment_body = "\n".join(line for line in migration.splitlines() if line.lstrip().startswith("--"))

    # Drops are only reversible if the exact re-create DDL survives in the repo.
    # river_timeseries_valid_time_discovery_idx has no in-repo CREATE at all (created
    # out-of-band on node-27), so this comment is its only recorded definition.
    assert (
        "CREATE INDEX IF NOT EXISTS river_timeseries_mvt_identity_lookup_idx "
        "ON hydro.river_timeseries (run_id, variable, valid_time, river_network_version_id, river_segment_id);"
        in comment_body
    )
    assert (
        "CREATE INDEX river_timeseries_valid_time_discovery_idx "
        "ON hydro.river_timeseries USING btree (run_id, variable, valid_time DESC);"
        in comment_body
    )
    assert "out-of-band" in comment_body, (
        "the migration must self-document that it drops an index no in-repo migration created"
    )


def test_drop_redundant_river_index_migration_never_executes_its_recorded_rollback_ddl() -> None:
    # `split_sql_statements` is the splitter `packages.common.migrate` uses to apply
    # migrations, so it is the authoritative statement oracle. The rollback DDL above
    # lives inside `--` comments and carries its own semicolons; if the apply lane ever
    # treated it as executable, applying 000049 would rebuild the 162 GB index it just
    # dropped.
    statements = split_sql_statements(
        (MIGRATIONS_DIR / DROP_REDUNDANT_RIVER_INDEX_MIGRATION).read_text(encoding="utf-8")
    )

    assert len(statements) == 2, f"expected two executable statements, got {len(statements)}"
    for statement in statements:
        executable = "\n".join(
            line for line in statement.splitlines() if not line.lstrip().startswith("--")
        ).strip()
        assert executable.startswith("DROP INDEX CONCURRENTLY IF EXISTS hydro.river_timeseries_"), executable
        assert "CREATE" not in executable.upper(), executable


def test_selected_run_valid_time_discovery_migration_matches_strict_identity_predicates() -> None:
    """The named-identity discovery branch matches its serving index, column for column.

    Re-pinned by issue #1341: the branch now filters on the surrogate keys, so
    the index it must match is 000051's integer one, not 000021's text one
    (which stays in the database for the out-of-boundary text readers — see
    RETAINED_RIVER_TIMESERIES_INDEXES). The row-selection authority is the key
    prefix; the sanctioned text conjuncts beside it are the transitional
    compressed-chunk pushdown aids and do not participate in index matching
    (this index has no text column), which is exactly why the four-column key
    prefix still has to be spelled out here.

    Both halves of the pin run on ``outer_predicates``: the key-resolution
    sub-selects legitimately contain ``run_id = :run_id`` against the authority
    table, and a raw-source check would therefore pass on code that never
    switched.
    """
    migration = dict(_migration_sql())[SURROGATE_KEY_READ_INDEX_MIGRATION]
    text_migration = dict(_migration_sql())["000021_latest_ready_run_discovery_idx.sql"]
    mvt_source = (Path(__file__).resolve().parents[1] / "services" / "tiles" / "mvt.py").read_text(
        encoding="utf-8"
    )
    valid_time_source = mvt_source[
        mvt_source.index("def valid_times_for_layer") : mvt_source.index("def national_discharge_valid_times")
    ]
    named_branch_sql, no_named_branch_sql = sql_literals(valid_time_source)
    hydro_columns = _index_columns_by_name(
        migration,
        "river_ts_selected_identity_key_valid_time_idx",
    )

    assert hydro_columns == (
        "run_key",
        "basin_version_key",
        "river_network_version_key",
        "variable_e",
        "valid_time DESC",
    )
    # Key form and text form are the same identity in the same order: the
    # switched branch is a strict prefix of the new index exactly as the old
    # branch was of the old one.
    assert _index_columns_by_name(
        text_migration,
        "river_timeseries_mvt_selected_identity_valid_time_discovery_idx",
    ) == (
        "run_id",
        "basin_version_id",
        "river_network_version_id",
        "variable",
        "valid_time DESC",
    )

    for expected in (
        "AND run_key = (",
        "SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id",
        "AND basin_version_key = (",
        "SELECT basin_version_key FROM core.basin_version",
        "WHERE basin_version_id = :basin_version_id",
        "AND river_network_version_key = (",
        "SELECT river_network_version_key FROM core.river_network_version",
        "WHERE river_network_version_id = :river_network_version_id",
        "variable_e = (",
        "SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e",
        "WHERE e::text = :variable",
        "ORDER BY valid_time DESC",
    ):
        assert expected in named_branch_sql, expected

    # The whole outer query, spelled out. Equality rather than a list of `in`
    # checks: it pins the four key columns in the index's own order, pins each
    # sanctioned text aid as ADJACENT to its counterpart (a pair split across
    # the query stops being a self-evident no-op), and is red the moment any
    # other predicate — text or key — appears.
    assert outer_predicates(named_branch_sql) == (
        "SELECT DISTINCT valid_time FROM hydro.river_timeseries "
        "WHERE run_id = :run_id AND run_key = "
        "AND basin_version_key = "
        "AND river_network_version_id = :river_network_version_id AND river_network_version_key = "
        "AND variable = :variable AND variable_e = "
        "ORDER BY valid_time DESC LIMIT :limit"
    )

    # Negative pin (never delete, only re-point): the text columns that
    # compression does NOT let us push down stay off the fact table entirely,
    # in either branch, and neither branch keeps the old IS-NULL-or-equals
    # text guards.
    #
    # Word-boundary matching, not bare substrings: these two branches are
    # single-table queries with no alias to qualify on, and `unit` /
    # `quality_flag` are prefixes of the legitimate `unit_e` /
    # `quality_flag_e`. A bare `in` check would false-red the moment either
    # enum column is projected here, and would false-red on correct post-#1342
    # code for the same reason.
    for branch in (named_branch_sql, no_named_branch_sql):
        outer = outer_predicates(branch)
        for forbidden in FORBIDDEN_TEXT_FACT_COLUMNS:
            assert re.search(rf"\b{forbidden}\b", outer) is None, forbidden
    assert "(:basin_version_id IS NULL OR basin_version_id = :basin_version_id)" not in valid_time_source
    assert "(:river_network_version_id IS NULL OR river_network_version_id = :river_network_version_id)" not in (
        valid_time_source
    )


def test_surrogate_key_read_index_migration_adds_one_plain_index_and_drops_nothing() -> None:
    """000051 is exactly one non-concurrent CREATE INDEX, and it removes nothing.

    ``CREATE INDEX CONCURRENTLY`` is rejected on this hypertable (measured on
    node-27 during #1338), so the statement must stay plain — and because a
    plain build holds a SHARE lock that blocks ingest for its whole duration,
    the header has to carry that operational constraint for the operator who
    schedules it.
    """
    migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    migration_sql = dict(_migration_sql())

    assert SURROGATE_KEY_READ_INDEX_MIGRATION in migration_sql
    assert [name for name in migration_names if name.startswith("000051")] == [
        SURROGATE_KEY_READ_INDEX_MIGRATION
    ]
    # The index is on columns 000050 adds, so it must apply after it.
    assert migration_names.index("000050_river_identity_normalization.sql") < migration_names.index(
        SURROGATE_KEY_READ_INDEX_MIGRATION
    )

    migration = migration_sql[SURROGATE_KEY_READ_INDEX_MIGRATION]
    statements = split_sql_statements(migration)
    assert len(statements) == 1, f"expected exactly one executable statement, got {len(statements)}"
    executable = "\n".join(
        line for line in statements[0].splitlines() if not line.lstrip().startswith("--")
    ).strip()
    assert executable.startswith(
        "CREATE INDEX IF NOT EXISTS river_ts_selected_identity_key_valid_time_idx"
    ), executable
    assert "CONCURRENTLY" not in executable, (
        "hypertables reject CREATE INDEX CONCURRENTLY; the build is deliberately plain"
    )
    assert "DROP" not in executable.upper(), (
        "000051 adds an index; retiring the text indexes is issue #1342"
    )
    for retained_index in RETAINED_RIVER_TIMESERIES_INDEXES:
        if retained_index == "river_ts_selected_identity_key_valid_time_idx":
            continue
        assert retained_index not in executable, retained_index

    comment_body = "\n".join(line for line in migration.splitlines() if line.lstrip().startswith("--"))
    for operational_note in ("CONCURRENTLY", "SHARE lock", "ingest", "cycle"):
        assert operational_note in comment_body, operational_note


def test_qhh_latest_display_product_migration_matches_candidate_and_window_queries() -> None:
    migration = dict(_migration_sql())["000024_qhh_latest_display_product_indexes.sql"]
    parsed_status_migration = dict(_migration_sql())["000030_qhh_latest_display_parsed_status_index.sql"]
    display_ready_migration = dict(_migration_sql())["000040_display_ready_succeeded_status_index.sql"]
    drop_redundant_river_index_migration = dict(_migration_sql())[
        "000041_drop_redundant_river_qhh_latest_window_idx.sql"
    ]
    drop_selected_identity_index_migration = dict(_migration_sql())[
        "000042_drop_redundant_river_selected_identity_lookup_idx.sql"
    ]
    store_source = (
        Path(__file__).resolve().parents[1] / "packages" / "common" / "forecast_store.py"
    ).read_text(encoding="utf-8")
    # The candidate_runs CTE now lives in one module-level constant shared by the
    # fallback's scan header and its heavy statement, so bind the candidate
    # assertions to that constant and everything else to the fallback method
    # ALONE (endpoint narrowed to the fast path's def). A slice that ran on to
    # _fetch_station_for_series also covered the fast path and the
    # unavailable-context query, where most of these literals exist too — it
    # would have gone quietly green on a broken fallback.
    candidate_source = store_source[
        store_source.index("_QHH_LATEST_CANDIDATE_RUNS_SQL = ") : store_source.index(
            "def _qhh_latest_candidate_runs_sql"
        )
    ]
    fallback_source = store_source[
        store_source.index("def _fetch_latest_qhh_display_candidates(") : store_source.index(
            "def _fetch_latest_qhh_display_candidates_fast"
        )
    ]
    query_source = candidate_source + fallback_source
    context_source = store_source[
        store_source.index("def _fetch_latest_qhh_display_unavailable_context") : store_source.index(
            "def _fetch_station_for_series"
        )
    ]
    index_evidence_source = store_source[
        store_source.index("def _qhh_latest_query_indexes") : store_source.index("def _non_negative_int")
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
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_display_ready_candidate_idx" in display_ready_migration
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_display_ready_basin_status_idx" in display_ready_migration
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
    assert (
        "DROP INDEX CONCURRENTLY IF EXISTS hydro.river_timeseries_qhh_latest_window_idx"
        in drop_redundant_river_index_migration
    )
    assert (
        "DROP INDEX CONCURRENTLY IF EXISTS hydro.river_timeseries_mvt_selected_identity_lookup_idx"
        in drop_selected_identity_index_migration
    )
    for index_name in (
        "hydro_run_qhh_latest_candidate_idx",
        "basin_version_qhh_latest_lookup_idx",
        "forcing_station_timeseries_qhh_latest_window_idx",
        "interp_weight_qhh_latest_membership_idx",
    ):
        assert index_name in index_evidence_source

    # #1442 switched the latest-product river leg onto the surrogate keys and the
    # enum, so the evidence payload must name migration 000051's key index — the
    # leg's four equality binds plus the valid_time range are exactly its columns,
    # in order. Both superseded text indexes must be absent: the 000021 discovery
    # index (its leading run_id is no longer bound at all) and the 000049-dropped
    # mvt_identity_lookup. The pkey is still not the successor either.
    # Matched on the payload key/value pairs rather than a bare substring, so the
    # rationale comments may keep naming the superseded indexes in prose.
    assert '"index": "river_ts_selected_identity_key_valid_time_idx"' in index_evidence_source
    assert '"status": "covered_by_selected_identity_key_valid_time_index"' in index_evidence_source
    assert (
        '"index": "river_timeseries_mvt_selected_identity_valid_time_discovery_idx"'
        not in index_evidence_source
    )
    assert '"status": "covered_by_selected_identity_valid_time_discovery_index"' not in index_evidence_source
    assert '"index": "river_timeseries_mvt_identity_lookup_idx"' not in index_evidence_source
    assert '"status": "covered_by_mvt_identity_lookup_index"' not in index_evidence_source
    assert '"index": "river_timeseries_pkey"' not in index_evidence_source
    assert '"status": "covered_by_primary_key_prefix"' not in index_evidence_source

    assert "LOWER(h.source_id) = LOWER(%(source_id)s)" in query_source
    assert "h.run_type = 'forecast'" in query_source
    assert "h.status IN ('succeeded', 'parsed', 'published')" in query_source
    assert "h.status NOT IN ('succeeded', 'parsed', 'published')" in context_source
    assert "h.cycle_time IS NOT NULL" in query_source
    assert "QHH_LATEST_SEARCH_LIMIT" in query_source
    assert "QHH_LATEST_CONTEXT_LIMIT" in context_source
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
    assert "HAVING COUNT(DISTINCT variable) = %(variable_count)s" in query_source
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
    # Since #1442 the river chain counts distinct SEGMENT KEYS, not segment text.
    assert "COUNT(DISTINCT river_segment_key)" in query_source
    assert "rt.river_segment_id" not in query_source
    assert "cr.expected_segment_count" in query_source
    assert "segment_count = expected_segment_count" in query_source
    assert "MIN(valid_time) AS river_valid_time_start" in query_source
    assert "MAX(valid_time) AS river_valid_time_end" in query_source
    assert "GREATEST(h.cycle_time, h.start_time, fv.start_time) AS display_start_time" in query_source
    assert "h.cycle_time + (%(horizon)s * INTERVAL '1 hour')" in query_source
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



def test_station_mvt_active_source_index_migration_is_forward_upgrade_safe() -> None:
    migration_sql = dict(_migration_sql())
    migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    migration = migration_sql["000033_station_mvt_active_source_index.sql"]

    assert migration_names.index("000032_source_specific_state_snapshot.sql") < migration_names.index(
        "000033_station_mvt_active_source_index.sql"
    )
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


def test_state_snapshot_clone_provenance_migration_is_column_only_forward_upgrade() -> None:
    migration_sql = dict(_migration_sql())
    migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    migration_name = "000046_state_snapshot_clone_provenance.sql"

    assert migration_name in migration_sql, (
        f"{migration_name} must exist as the clone-provenance migration for the "
        "mapping-variant-state-compatibility change"
    )
    assert migration_names.index("000045_hydro_run_type_hindcast.sql") < migration_names.index(migration_name)

    migration = migration_sql[migration_name]

    # Non-empty SQL with column-only ALTER form.
    normalized = migration.strip()
    assert normalized, f"{migration_name} is empty"
    assert normalized.lower().endswith(";"), f"{migration_name} should end with a SQL terminator"

    # Exactly the three nullable provenance columns are added, all TEXT DEFAULT NULL.
    for expected in (
        "ADD COLUMN IF NOT EXISTS cloned_from_state_id TEXT DEFAULT NULL",
        "ADD COLUMN IF NOT EXISTS cloned_from_model_id TEXT DEFAULT NULL",
        "ADD COLUMN IF NOT EXISTS clone_gate_fingerprint TEXT DEFAULT NULL",
    ):
        assert expected in migration, f"{migration_name} must contain: {expected}"

    # Column-only ALTER — no destructive, data-touching, or scope-widening
    # statements. Fixture rule: a DB migration on a live table with pre-existing
    # rows must be guarded even at low severity levels, because a silent typo
    # (DROP COLUMN, UPDATE, TRUNCATE, ALTER COLUMN, …) can destroy or rewrite
    # data that the mechanism relies on. Word-boundary regex is used (not raw
    # substring) so operator-adjacent whitespace (TAB, newline, table name)
    # cannot slip past the check, and so descriptive words like "SUPDATED" or
    # "REDROP" cannot falsely trigger it. Comments are stripped first so header
    # prose that legitimately mentions "no drop of the existing index" or
    # similar does not need to dodge the guard.
    code_lines = [
        line
        for line in migration.splitlines()
        if not line.lstrip().startswith("--")
    ]
    code_body = "\n".join(code_lines)
    forbidden_token_patterns = (
        r"\bDROP\s+INDEX\b",
        r"\bDROP\s+CONSTRAINT\b",
        r"\bDROP\s+COLUMN\b",
        r"\bDROP\s+TABLE\b",
        r"\bDROP\s+TRIGGER\b",
        r"\bCREATE\s+INDEX\b",
        r"\bCREATE\s+UNIQUE\s+INDEX\b",
        r"\bCREATE\s+TABLE\b",
        r"\bCREATE\s+TRIGGER\b",
        r"\bCREATE\s+FUNCTION\b",
        r"\bALTER\s+COLUMN\b",
        r"\bALTER\s+INDEX\b",
        r"\bTRUNCATE\b",
        r"\bRENAME\b",
        r"\bGRANT\b",
        r"\bREVOKE\b",
        r"\bUPDATE\b",
        r"\bINSERT\b",
        r"\bDELETE\b",
        r"\bCOMMENT\s+ON\b",
    )
    for token_pattern in forbidden_token_patterns:
        matches = [
            line
            for line in code_lines
            if re.search(token_pattern, line, re.IGNORECASE)
        ]
        assert not matches, (
            f"Forbidden DDL matching {token_pattern!r} found in {migration_name}: {matches}"
        )

    # Exactly three ADD COLUMN / ALTER TABLE statements — one per provenance
    # column. Guards against a future edit that quietly duplicates or omits a
    # column and still passes the presence assertions above. Counted on the
    # comment-stripped body so header prose cannot inflate the count.
    add_column_stmts = re.findall(r"\bADD\s+COLUMN\b", code_body, re.IGNORECASE)
    assert len(add_column_stmts) == 3, (
        f"Migration {migration_name} must add exactly 3 columns, "
        f"found {len(add_column_stmts)}"
    )
    alter_table_stmts = re.findall(r"\bALTER\s+TABLE\b", code_body, re.IGNORECASE)
    assert len(alter_table_stmts) == 3, (
        f"Migration {migration_name} must have exactly 3 ALTER TABLE statements, "
        f"found {len(alter_table_stmts)}"
    )

    # Scope-boundary lock: every ALTER TABLE must target ONLY
    # hydro.state_snapshot. A stray edit that adds "ALTER TABLE hydro.other …"
    # in the same migration would silently widen the change surface without
    # this assertion. Captured against the comment-stripped body for the same
    # reason as the counts above.
    altered_tables = re.findall(
        r"\bALTER\s+TABLE\s+(\S+)", code_body, re.IGNORECASE
    )
    assert altered_tables == ["hydro.state_snapshot"] * 3, (
        f"Migration {migration_name} must ONLY touch hydro.state_snapshot, "
        f"found altered tables: {altered_tables}"
    )

    # The (model_id, COALESCE(source_id, ''), valid_time) unique index MUST NOT be
    # altered by this migration. The name may appear in the header comment as
    # explicit documentation of what stays intact, but no DDL statement may act
    # on it. Guard: no ALTER INDEX / DROP INDEX line references the index name.
    for line in migration.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        if "state_snapshot_model_source_valid_time_key" in stripped:
            raise AssertionError(
                f"{migration_name} must never touch state_snapshot_model_source_valid_time_key in DDL; "
                f"offending line: {line!r}"
            )

    # No references to future migration objects (000047..000099).
    for future in [f"{n:06d}" for n in range(47, 100)]:
        assert future not in migration, (
            f"{migration_name} must not reference future migration {future}"
        )


def test_state_snapshot_clone_gate_kind_migration_is_column_only_forward_upgrade() -> None:
    """000053 follows 000046's column-only house style, one column instead of three.

    Same static shape contract as
    ``test_state_snapshot_clone_provenance_migration_is_column_only_forward_upgrade``:
    a live ``hydro.state_snapshot`` with pre-existing rows means a single typo
    (DROP COLUMN, UPDATE, ALTER COLUMN, …) would destroy or rewrite the warm
    states the whole carry-over mechanism depends on, and only a static check
    can see that without a database.
    """

    migration_sql = dict(_migration_sql())
    migration_names = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    migration_name = "000053_state_snapshot_clone_gate_kind.sql"

    assert migration_name in migration_sql, (
        f"{migration_name} must exist as the clone-gate-kind migration for the "
        "recalibration-state-carryover change"
    )
    assert migration_names.index("000046_state_snapshot_clone_provenance.sql") < (
        migration_names.index(migration_name)
    )

    migration = migration_sql[migration_name]

    normalized = migration.strip()
    assert normalized, f"{migration_name} is empty"
    assert normalized.lower().endswith(";"), f"{migration_name} should end with a SQL terminator"

    # Exactly the one nullable gate-kind column is added, TEXT DEFAULT NULL, so
    # every pre-existing row keeps its identity and is not rewritten.
    assert "ADD COLUMN IF NOT EXISTS clone_gate_kind TEXT DEFAULT NULL" in migration, (
        f"{migration_name} must add clone_gate_kind as TEXT DEFAULT NULL"
    )

    code_lines = [
        line
        for line in migration.splitlines()
        if not line.lstrip().startswith("--")
    ]
    code_body = "\n".join(code_lines)
    forbidden_token_patterns = (
        r"\bDROP\s+INDEX\b",
        r"\bDROP\s+CONSTRAINT\b",
        r"\bDROP\s+COLUMN\b",
        r"\bDROP\s+TABLE\b",
        r"\bDROP\s+TRIGGER\b",
        r"\bCREATE\s+INDEX\b",
        r"\bCREATE\s+UNIQUE\s+INDEX\b",
        r"\bCREATE\s+TABLE\b",
        r"\bCREATE\s+TRIGGER\b",
        r"\bCREATE\s+FUNCTION\b",
        r"\bALTER\s+COLUMN\b",
        r"\bALTER\s+INDEX\b",
        r"\bTRUNCATE\b",
        r"\bRENAME\b",
        r"\bGRANT\b",
        r"\bREVOKE\b",
        r"\bUPDATE\b",
        r"\bINSERT\b",
        r"\bDELETE\b",
        r"\bCOMMENT\s+ON\b",
    )
    for token_pattern in forbidden_token_patterns:
        matches = [
            line
            for line in code_lines
            if re.search(token_pattern, line, re.IGNORECASE)
        ]
        assert not matches, (
            f"Forbidden DDL matching {token_pattern!r} found in {migration_name}: {matches}"
        )

    add_column_stmts = re.findall(r"\bADD\s+COLUMN\b", code_body, re.IGNORECASE)
    assert len(add_column_stmts) == 1, (
        f"Migration {migration_name} must add exactly 1 column, "
        f"found {len(add_column_stmts)}"
    )
    alter_table_stmts = re.findall(r"\bALTER\s+TABLE\b", code_body, re.IGNORECASE)
    assert len(alter_table_stmts) == 1, (
        f"Migration {migration_name} must have exactly 1 ALTER TABLE statement, "
        f"found {len(alter_table_stmts)}"
    )

    altered_tables = re.findall(
        r"\bALTER\s+TABLE\s+(\S+)", code_body, re.IGNORECASE
    )
    assert altered_tables == ["hydro.state_snapshot"], (
        f"Migration {migration_name} must ONLY touch hydro.state_snapshot, "
        f"found altered tables: {altered_tables}"
    )

    # The per-source warm-state identity index must survive untouched. The name
    # may appear in the header comment as documentation of what stays intact,
    # but no DDL statement may act on it.
    for line in migration.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        if "state_snapshot_model_source_valid_time_key" in stripped:
            raise AssertionError(
                f"{migration_name} must never touch "
                f"state_snapshot_model_source_valid_time_key in DDL; "
                f"offending line: {line!r}"
            )

    # No references to future migration objects (000054..000099).
    for future in [f"{n:06d}" for n in range(54, 100)]:
        assert future not in migration, (
            f"{migration_name} must not reference future migration {future}"
        )


def test_authority_stats_hygiene_migration_splits_into_the_expected_statements() -> None:
    """000052 mixes a DO block with four CONCURRENTLY statements (issue #1468).

    ``split_sql_statements`` is the splitter ``packages.common.migrate`` applies
    migrations with, so it is the authoritative statement oracle. Three things
    must hold at once and only this test can see them without a database:

    * the ``$$`` body survives whole -- the DO block carries five internal
      semicolons, and a splitter that cut on them would ship five syntactically
      broken fragments;
    * no statement is wrapped in a transaction and the CONCURRENTLY statements
      arrive as their own top-level statements -- ``CREATE/DROP INDEX
      CONCURRENTLY`` is rejected inside a transaction block, and
      ``apply_migration`` runs one statement per ``cursor.execute`` on an
      autocommit connection;
    * no index is dropped NON-concurrently. ``core.river_segment`` serves live
      MVT reads and a plain ``DROP INDEX`` takes ACCESS EXCLUSIVE on the TABLE,
      not merely on the index -- including on the INVALID-leftover branch, whose
      leftover is therefore RENAMED aside and dropped concurrently in step 3
      (round-1 review C1, design D3).
    """

    migration = dict(_migration_sql())[AUTHORITY_STATS_HYGIENE_MIGRATION]
    statements = split_sql_statements(migration)

    executables = [
        "\n".join(line for line in statement.splitlines() if not line.lstrip().startswith("--")).strip()
        for statement in statements
    ]

    assert len(executables) == 9, f"expected nine executable statements, got {len(executables)}"

    # Step 0: a leftover `_invalid` from a PREVIOUS interrupted run is cleared
    # first, so the DO block's rename below cannot collide with the name.
    assert executables[0] == (
        "DROP INDEX CONCURRENTLY IF EXISTS core.river_segment_id_trgm_idx_invalid;"
    ), executables[0]

    do_block = executables[1]
    assert do_block.startswith("DO $$"), do_block
    assert do_block.endswith("$$;"), do_block
    # The whole body, semicolons and all, is inside statement 1.
    for body_fragment in (
        "SET LOCAL lock_timeout = '2s';",
        "RENAME TO river_segment_id_trgm_idx_invalid;",
        "ALTER INDEX core.river_segment_id_trgm_idx",
        "RENAME TO river_segment_id_trgm_idx_legacy;",
        "ix.indisvalid = false",
        "indexdef NOT LIKE '%lower(%'",
    ):
        assert body_fragment in do_block, body_fragment

    assert executables[2].startswith(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS river_segment_id_trgm_idx"
    ), executables[2]
    # The equality trap is closed by the EXPRESSION, not by the index existing.
    assert "GIN (lower(river_segment_id) gin_trgm_ops)" in executables[2], executables[2]
    assert executables[3] == (
        "DROP INDEX CONCURRENTLY IF EXISTS core.river_segment_id_trgm_idx_legacy;"
    ), executables[3]
    assert executables[4] == (
        "DROP INDEX CONCURRENTLY IF EXISTS core.river_segment_id_trgm_idx_invalid;"
    ), executables[4]

    # Every DROP INDEX in the executable body -- the DO block's included -- must
    # be CONCURRENTLY. Checked against the comment-stripped statements so the
    # header's prose about non-concurrent drops cannot satisfy or trip it.
    for statement in executables:
        for match in re.finditer(r"\bDROP\s+INDEX\b(?!\s+CONCURRENTLY\b)", statement, re.IGNORECASE):
            raise AssertionError(
                f"non-concurrent DROP INDEX takes ACCESS EXCLUSIVE on core.river_segment: "
                f"{statement[match.start() : match.start() + 120]!r}"
            )

    assert [
        re.sub(r"\s+", " ", statement).strip() for statement in executables[5:]
    ] == [
        "ALTER TABLE core.river_segment SET (autovacuum_analyze_scale_factor = 0.01,"
        " autovacuum_analyze_threshold = 500);",
        "ALTER TABLE core.river_segment_crosswalk SET (autovacuum_analyze_scale_factor = 0.01,"
        " autovacuum_analyze_threshold = 500);",
        "ALTER TABLE core.river_network_version SET (autovacuum_analyze_scale_factor = 0,"
        " autovacuum_analyze_threshold = 1);",
        "ALTER TABLE core.basin_version SET (autovacuum_analyze_scale_factor = 0,"
        " autovacuum_analyze_threshold = 1);",
    ]

    # CONCURRENTLY is rejected inside a transaction block; the file must not
    # open one, and the applier must not be handed one to open.
    upper = migration.upper()
    assert "BEGIN;" not in upper
    assert "COMMIT;" not in upper


def test_authority_stats_hygiene_migration_documents_the_trap_and_the_recovery() -> None:
    """The header is the only place an operator learns why the index is an
    expression index and what to do after an interrupted concurrent build."""

    migration = dict(_migration_sql())[AUTHORITY_STATS_HYGIENE_MIGRATION]
    comment_body = "\n".join(line for line in migration.splitlines() if line.lstrip().startswith("--"))

    for required_note in (
        "#1468",
        "pg_trgm 1.6",
        "gin_trgm_ops",
        "51,029 ms",
        "17 ms",
        "STRUCTURAL",
        "INVALID",
        "re-run this file",
    ):
        assert required_note in comment_body, required_note


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
