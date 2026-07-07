"""Tests for db/migrations/000043_canonical_grid_snapshot.sql.

Covers issue #899 (Epic #897 SUB-2) Task 2.1 Evidence Floor:
- immutable met.canonical_grid_snapshot + met.canonical_grid_cell tables
- nullable met.canonical_met_product.grid_snapshot_id FK
- derived-cache staleness columns on met.met_station / met.interp_weight
- URI-match and identity-immutability triggers
- migration idempotency

Static-SQL tests always run. Integration tests are marked with
``pytest.mark.integration`` and require ``NHMS_RUN_INTEGRATION=1`` +
``NHMS_INTEGRATION_DATABASE_URL`` (SKIP is expected locally).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest
from psycopg2.extras import Json

from tests.integration_helpers import apply_migrations_from_zero

MIGRATION_FILENAME = "000043_canonical_grid_snapshot.sql"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "db" / "migrations" / MIGRATION_FILENAME
)

# Run-unique prefix so integration tests do not collide with other suites.
RUN_PREFIX = "sub2_899"


# -----------------------------------------------------------------------------
# Static-SQL sanity tests (always run; parse the migration file text).
# -----------------------------------------------------------------------------


def _migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.exists(), f"missing migration file: {MIGRATION_PATH}"


def test_migration_creates_canonical_grid_snapshot_table() -> None:
    sql = _migration_sql()
    assert "CREATE TABLE IF NOT EXISTS met.canonical_grid_snapshot (" in sql


def test_migration_creates_canonical_grid_cell_table() -> None:
    sql = _migration_sql()
    assert "CREATE TABLE IF NOT EXISTS met.canonical_grid_cell (" in sql


def test_snapshot_table_declares_required_identity_columns() -> None:
    sql = _migration_sql()
    required = (
        "grid_snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid()",
        "canonical_grid_key TEXT NOT NULL",
        "source_id TEXT NOT NULL REFERENCES met.data_source(source_id)",
        "grid_id TEXT NOT NULL",
        "grid_signature TEXT NOT NULL",
        "grid_definition_uri TEXT NOT NULL",
        "grid_definition_checksum TEXT NOT NULL",
        "longitude_convention TEXT NOT NULL",
        "latitude_order TEXT NOT NULL",
        "flatten_order TEXT NOT NULL",
        "native_resolution DOUBLE PRECISION NOT NULL",
        "bbox_south DOUBLE PRECISION NOT NULL",
        "bbox_north DOUBLE PRECISION NOT NULL",
        "bbox_west DOUBLE PRECISION NOT NULL",
        "bbox_east DOUBLE PRECISION NOT NULL",
        "converter_version TEXT NOT NULL",
        "valid_from TIMESTAMPTZ NOT NULL",
        "valid_to TIMESTAMPTZ NULL",
        "applicable_source_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]",
        "superseded_at TIMESTAMPTZ NULL",
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    )
    for fragment in required:
        assert fragment in sql, f"missing snapshot column declaration: {fragment!r}"


def test_cell_table_declares_ordered_geometry_and_cascades() -> None:
    sql = _migration_sql()
    for fragment in (
        "grid_snapshot_id UUID NOT NULL REFERENCES met.canonical_grid_snapshot(grid_snapshot_id) ON DELETE CASCADE",
        "grid_cell_id TEXT NOT NULL",
        "longitude DOUBLE PRECISION NOT NULL",
        "latitude DOUBLE PRECISION NOT NULL",
        "canonical_ordinal INTEGER NOT NULL CHECK (canonical_ordinal >= 1)",
        "PRIMARY KEY (grid_snapshot_id, grid_cell_id)",
        "UNIQUE (grid_snapshot_id, canonical_ordinal)",
    ):
        assert fragment in sql, f"missing cell column declaration: {fragment!r}"


def test_canonical_met_product_gets_grid_snapshot_fk_column() -> None:
    sql = _migration_sql()
    assert (
        "ALTER TABLE met.canonical_met_product\n"
        "  ADD COLUMN IF NOT EXISTS grid_snapshot_id UUID REFERENCES met.canonical_grid_snapshot(grid_snapshot_id)"
    ) in sql


def test_met_station_gains_supersession_columns() -> None:
    sql = _migration_sql()
    assert (
        "ALTER TABLE met.met_station\n"
        "  ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ NULL"
    ) in sql
    assert (
        "ALTER TABLE met.met_station\n"
        "  ADD COLUMN IF NOT EXISTS grid_snapshot_id UUID REFERENCES met.canonical_grid_snapshot(grid_snapshot_id)"
    ) in sql


def test_interp_weight_gains_active_flag_and_supersession_columns() -> None:
    sql = _migration_sql()
    for fragment in (
        (
            "ALTER TABLE met.interp_weight\n"
            "  ADD COLUMN IF NOT EXISTS active_flag BOOLEAN NOT NULL DEFAULT true"
        ),
        (
            "ALTER TABLE met.interp_weight\n"
            "  ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ NULL"
        ),
        (
            "ALTER TABLE met.interp_weight\n"
            "  ADD COLUMN IF NOT EXISTS grid_snapshot_id UUID REFERENCES met.canonical_grid_snapshot(grid_snapshot_id)"
        ),
    ):
        assert fragment in sql, f"missing interp_weight column addition: {fragment!r}"


def test_uri_match_trigger_function_and_trigger_declared() -> None:
    sql = _migration_sql()
    assert (
        "CREATE OR REPLACE FUNCTION met.canonical_met_product_grid_definition_uri_match()"
        in sql
    )
    assert (
        "DROP TRIGGER IF EXISTS canonical_met_product_grid_definition_uri_match_trg"
        in sql
    )
    assert (
        "CREATE TRIGGER canonical_met_product_grid_definition_uri_match_trg" in sql
    )
    assert (
        "BEFORE INSERT OR UPDATE ON met.canonical_met_product" in sql
    )
    # RAISE EXCEPTION mentions both URIs and the snapshot id for actionable error text.
    assert (
        "canonical_met_product.grid_definition_uri (%) does not match snapshot % grid_definition_uri (%)"
        in sql
    )


def test_identity_immutability_trigger_function_and_trigger_declared() -> None:
    sql = _migration_sql()
    assert (
        "CREATE OR REPLACE FUNCTION met.canonical_grid_snapshot_identity_immutable()"
        in sql
    )
    assert (
        "DROP TRIGGER IF EXISTS canonical_grid_snapshot_identity_immutable_trg" in sql
    )
    assert "CREATE TRIGGER canonical_grid_snapshot_identity_immutable_trg" in sql
    assert "BEFORE UPDATE ON met.canonical_grid_snapshot" in sql
    # Identity fields the trigger must guard.
    for guarded in (
        "grid_signature is immutable",
        "grid_definition_uri is immutable",
        "grid_definition_checksum is immutable",
        "canonical_grid_key is immutable",
        "bbox is immutable",
        "native_resolution is immutable",
    ):
        assert guarded in sql, f"missing immutability guard: {guarded!r}"


def test_migration_uses_if_not_exists_for_idempotency() -> None:
    sql = _migration_sql()
    # All schema-defining statements must be IF-NOT-EXISTS guarded so re-application
    # is a no-op (per migration file idempotency contract).
    assert "CREATE TABLE IF NOT EXISTS met.canonical_grid_snapshot" in sql
    assert "CREATE TABLE IF NOT EXISTS met.canonical_grid_cell" in sql
    # DROP-then-CREATE for triggers keeps them idempotent (no CREATE TRIGGER IF NOT
    # EXISTS in PG < 14). We now declare 4 triggers total.
    assert sql.count("DROP TRIGGER IF EXISTS") >= 4


def test_migration_declares_cell_immutability_triggers() -> None:
    """Per-cell rows are identity components of the immutable snapshot per
    grid-snapshot-registration/spec.md scenario "Snapshots are never updated in
    place". The migration MUST declare both a BEFORE UPDATE trigger and a
    BEFORE DELETE (direct-only) trigger on met.canonical_grid_cell."""
    sql = _migration_sql()
    # UPDATE-rejection function + trigger.
    assert (
        "CREATE OR REPLACE FUNCTION met.canonical_grid_cell_immutable()" in sql
    )
    assert "DROP TRIGGER IF EXISTS canonical_grid_cell_immutable_trg" in sql
    assert "CREATE TRIGGER canonical_grid_cell_immutable_trg" in sql
    assert "BEFORE UPDATE ON met.canonical_grid_cell" in sql
    assert (
        "canonical_grid_cell rows are immutable once inserted" in sql
    )
    # Direct-DELETE rejection function + trigger; the pg_trigger_depth() = 1 check
    # is the mechanism that lets CASCADE-driven deletes still succeed.
    assert (
        "CREATE OR REPLACE FUNCTION met.canonical_grid_cell_direct_delete_blocked()"
        in sql
    )
    assert (
        "DROP TRIGGER IF EXISTS canonical_grid_cell_direct_delete_blocked_trg"
        in sql
    )
    assert "CREATE TRIGGER canonical_grid_cell_direct_delete_blocked_trg" in sql
    assert "BEFORE DELETE ON met.canonical_grid_cell" in sql
    assert "pg_trigger_depth() = 1" in sql
    assert "canonical_grid_cell rows may not be deleted directly" in sql


# -----------------------------------------------------------------------------
# Real-DB integration tests. Skipped locally without NHMS_RUN_INTEGRATION=1.
# -----------------------------------------------------------------------------


# Note: every integration test below carries its own ``@pytest.mark.integration``
# decorator; a module-level ``pytestmark = pytest.mark.integration`` would also
# poison the always-run static-SQL tests above with the integration marker.


def _seed_data_source(cursor, source_id: str) -> None:
    cursor.execute(
        """
        INSERT INTO met.data_source (
            source_id, source_name, source_type, status, native_format, adapter_name, config_json
        )
        VALUES (%s, %s, 'forecast', 'mock', 'netcdf', %s, %s)
        ON CONFLICT (source_id) DO NOTHING
        """,
        (source_id, f"{source_id} test source", source_id, Json({"test": True})),
    )


def _insert_snapshot(
    cursor,
    *,
    source_id: str,
    grid_id: str,
    canonical_grid_key: str,
    grid_definition_uri: str,
    grid_signature: str = "deadbeef",
    grid_definition_checksum: str = "cafefeed",
) -> str:
    """Insert a fully-populated snapshot row and return its grid_snapshot_id."""
    cursor.execute(
        """
        INSERT INTO met.canonical_grid_snapshot (
            canonical_grid_key,
            source_id,
            grid_id,
            grid_signature,
            grid_definition_uri,
            grid_definition_checksum,
            longitude_convention,
            latitude_order,
            flatten_order,
            native_resolution,
            bbox_south,
            bbox_north,
            bbox_west,
            bbox_east,
            converter_version,
            valid_from,
            valid_to,
            applicable_source_ids
        )
        VALUES (
            %s, %s, %s, %s, %s, %s,
            '[-180,180)', 'descending', 'y_major_lat_then_lon',
            0.25,
            8.0, 64.0, 63.0, 145.0,
            'converter-v1',
            '2026-01-01T00:00:00Z', NULL,
            ARRAY[%s]::TEXT[]
        )
        RETURNING grid_snapshot_id::text
        """,
        (
            canonical_grid_key,
            source_id,
            grid_id,
            grid_signature,
            grid_definition_uri,
            grid_definition_checksum,
            source_id,
        ),
    )
    return cursor.fetchone()["grid_snapshot_id"]


@pytest.fixture(scope="module")
def migrated_database(integration_database_url: str) -> str:
    """Apply all migrations once per module and yield the database URL."""
    apply_migrations_from_zero(integration_database_url)
    return integration_database_url


# NOTE on transaction handling: constraint / trigger violations poison the entire
# transaction ("current transaction is aborted"), so each expect-raises test
# opens its own psycopg2 connection with autocommit=True and issues each
# statement independently. This mirrors the migration runner's behavior.


def _fresh_connection(database_url: str):
    connection = psycopg2.connect(
        database_url, cursor_factory=psycopg2.extras.RealDictCursor
    )
    connection.autocommit = True
    return connection


@pytest.mark.integration
def test_canonical_grid_snapshot_and_cell_tables_exist(migrated_database: str) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'met'
                  AND table_name IN ('canonical_grid_snapshot', 'canonical_grid_cell')
                """
            )
            rows = {row["table_name"] for row in cursor.fetchall()}
        assert rows == {"canonical_grid_snapshot", "canonical_grid_cell"}
    finally:
        connection.close()


@pytest.mark.integration
def test_snapshot_columns_have_expected_types(migrated_database: str) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, data_type, is_nullable, udt_name
                FROM information_schema.columns
                WHERE table_schema = 'met' AND table_name = 'canonical_grid_snapshot'
                ORDER BY ordinal_position
                """
            )
            columns = {row["column_name"]: row for row in cursor.fetchall()}
        expected = {
            "grid_snapshot_id": ("uuid", "NO"),
            "canonical_grid_key": ("text", "NO"),
            "source_id": ("text", "NO"),
            "grid_id": ("text", "NO"),
            "grid_signature": ("text", "NO"),
            "grid_definition_uri": ("text", "NO"),
            "grid_definition_checksum": ("text", "NO"),
            "longitude_convention": ("text", "NO"),
            "latitude_order": ("text", "NO"),
            "flatten_order": ("text", "NO"),
            "native_resolution": ("double precision", "NO"),
            "bbox_south": ("double precision", "NO"),
            "bbox_north": ("double precision", "NO"),
            "bbox_west": ("double precision", "NO"),
            "bbox_east": ("double precision", "NO"),
            "converter_version": ("text", "NO"),
            "valid_from": ("timestamp with time zone", "NO"),
            "valid_to": ("timestamp with time zone", "YES"),
            "applicable_source_ids": ("ARRAY", "NO"),
            "superseded_at": ("timestamp with time zone", "YES"),
            "created_at": ("timestamp with time zone", "NO"),
        }
        for name, (data_type, is_nullable) in expected.items():
            assert name in columns, f"missing snapshot column {name!r}"
            assert columns[name]["data_type"] == data_type, (
                f"{name} type mismatch: {columns[name]['data_type']} vs {data_type}"
            )
            assert columns[name]["is_nullable"] == is_nullable, (
                f"{name} nullability mismatch: {columns[name]['is_nullable']} vs {is_nullable}"
            )
        # applicable_source_ids is a TEXT[]; verify the array element type.
        assert columns["applicable_source_ids"]["udt_name"] == "_text"
    finally:
        connection.close()


@pytest.mark.integration
def test_cell_columns_have_expected_types(migrated_database: str) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'met' AND table_name = 'canonical_grid_cell'
                ORDER BY ordinal_position
                """
            )
            columns = {row["column_name"]: row for row in cursor.fetchall()}
        expected = {
            "grid_snapshot_id": ("uuid", "NO"),
            "grid_cell_id": ("text", "NO"),
            "longitude": ("double precision", "NO"),
            "latitude": ("double precision", "NO"),
            "canonical_ordinal": ("integer", "NO"),
        }
        for name, (data_type, is_nullable) in expected.items():
            assert name in columns, f"missing cell column {name!r}"
            assert columns[name]["data_type"] == data_type
            assert columns[name]["is_nullable"] == is_nullable
    finally:
        connection.close()


@pytest.mark.integration
def test_grid_cell_id_unique_within_snapshot(migrated_database: str) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_cellid_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_a",
                canonical_grid_key=f"{RUN_PREFIX}_key_a",
                grid_definition_uri="s3://nhms/canonical/a/grid.json",
            )
            cursor.execute(
                """
                INSERT INTO met.canonical_grid_cell (
                    grid_snapshot_id, grid_cell_id, longitude, latitude, canonical_ordinal
                ) VALUES (%s, '0', 63.0, 8.0, 1)
                """,
                (snap_id,),
            )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO met.canonical_grid_cell (
                        grid_snapshot_id, grid_cell_id, longitude, latitude, canonical_ordinal
                    ) VALUES (%s, '0', 63.0, 8.0, 2)
                    """,
                    (snap_id,),
                )
    finally:
        connection.close()


@pytest.mark.integration
def test_canonical_ordinal_unique_within_snapshot(migrated_database: str) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_ord_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_ord",
                canonical_grid_key=f"{RUN_PREFIX}_key_ord",
                grid_definition_uri="s3://nhms/canonical/ord/grid.json",
            )
            cursor.execute(
                """
                INSERT INTO met.canonical_grid_cell (
                    grid_snapshot_id, grid_cell_id, longitude, latitude, canonical_ordinal
                ) VALUES (%s, '0', 63.0, 8.0, 1)
                """,
                (snap_id,),
            )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO met.canonical_grid_cell (
                        grid_snapshot_id, grid_cell_id, longitude, latitude, canonical_ordinal
                    ) VALUES (%s, '1', 63.25, 8.0, 1)
                    """,
                    (snap_id,),
                )
    finally:
        connection.close()


@pytest.mark.integration
def test_canonical_ordinal_check_enforces_positive_range(migrated_database: str) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_chk_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_chk",
                canonical_grid_key=f"{RUN_PREFIX}_key_chk",
                grid_definition_uri="s3://nhms/canonical/chk/grid.json",
            )
        with pytest.raises(psycopg2.errors.CheckViolation):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO met.canonical_grid_cell (
                        grid_snapshot_id, grid_cell_id, longitude, latitude, canonical_ordinal
                    ) VALUES (%s, '0', 63.0, 8.0, 0)
                    """,
                    (snap_id,),
                )
    finally:
        connection.close()


@pytest.mark.integration
def test_cell_rows_cascade_on_snapshot_delete(migrated_database: str) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_cascade_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_cascade",
                canonical_grid_key=f"{RUN_PREFIX}_key_cascade",
                grid_definition_uri="s3://nhms/canonical/cascade/grid.json",
            )
            cursor.execute(
                """
                INSERT INTO met.canonical_grid_cell (
                    grid_snapshot_id, grid_cell_id, longitude, latitude, canonical_ordinal
                )
                VALUES (%s, '0', 63.0, 8.0, 1),
                       (%s, '1', 63.25, 8.0, 2)
                """,
                (snap_id, snap_id),
            )
            cursor.execute(
                "SELECT COUNT(*) AS n FROM met.canonical_grid_cell WHERE grid_snapshot_id = %s",
                (snap_id,),
            )
            assert cursor.fetchone()["n"] == 2

            cursor.execute(
                "DELETE FROM met.canonical_grid_snapshot WHERE grid_snapshot_id = %s",
                (snap_id,),
            )
            cursor.execute(
                "SELECT COUNT(*) AS n FROM met.canonical_grid_cell WHERE grid_snapshot_id = %s",
                (snap_id,),
            )
            assert cursor.fetchone()["n"] == 0
    finally:
        connection.close()


@pytest.mark.integration
def test_canonical_grid_cell_direct_update_rejected(migrated_database: str) -> None:
    """Per-cell rows are identity components of the immutable snapshot per
    grid-snapshot-registration/spec.md scenario "Snapshots are never updated in
    place". Any direct UPDATE on a cell row MUST raise."""
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_cellup_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_cellup",
                canonical_grid_key=f"{RUN_PREFIX}_key_cellup",
                grid_definition_uri="s3://nhms/canonical/cellup/grid.json",
            )
            cursor.execute(
                """
                INSERT INTO met.canonical_grid_cell (
                    grid_snapshot_id, grid_cell_id, longitude, latitude, canonical_ordinal
                ) VALUES (%s, '0', 63.0, 8.0, 1)
                """,
                (snap_id,),
            )
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE met.canonical_grid_cell
                    SET longitude = 999.0
                    WHERE grid_snapshot_id = %s AND grid_cell_id = '0'
                    """,
                    (snap_id,),
                )
        message = str(excinfo.value)
        assert "canonical_grid_cell rows are immutable" in message
        assert snap_id in message
        assert "grid_cell_id=0" in message
    finally:
        connection.close()


@pytest.mark.integration
def test_canonical_grid_cell_direct_delete_rejected(migrated_database: str) -> None:
    """Direct DELETE on cell rows MUST raise (cells are only removable via
    cascade from the parent snapshot). See migration trigger D and
    grid-snapshot-registration/spec.md "Snapshots are never updated in place"."""
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_celldel_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_celldel",
                canonical_grid_key=f"{RUN_PREFIX}_key_celldel",
                grid_definition_uri="s3://nhms/canonical/celldel/grid.json",
            )
            cursor.execute(
                """
                INSERT INTO met.canonical_grid_cell (
                    grid_snapshot_id, grid_cell_id, longitude, latitude, canonical_ordinal
                )
                VALUES (%s, '0', 63.0, 8.0, 1),
                       (%s, '1', 63.25, 8.0, 2)
                """,
                (snap_id, snap_id),
            )
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM met.canonical_grid_cell
                    WHERE grid_snapshot_id = %s AND canonical_ordinal = 1
                    """,
                    (snap_id,),
                )
        message = str(excinfo.value)
        assert "canonical_grid_cell rows may not be deleted directly" in message
        assert snap_id in message
    finally:
        connection.close()


@pytest.mark.integration
def test_canonical_grid_cell_cascade_delete_still_works(
    migrated_database: str,
) -> None:
    """The direct-delete trigger's pg_trigger_depth() = 1 guard must NOT block
    CASCADE-driven deletes originating from met.canonical_grid_snapshot; the
    parent-driven removal path remains the sanctioned way to remove cells."""
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_cellcasc_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_cellcasc",
                canonical_grid_key=f"{RUN_PREFIX}_key_cellcasc",
                grid_definition_uri="s3://nhms/canonical/cellcasc/grid.json",
            )
            cursor.execute(
                """
                INSERT INTO met.canonical_grid_cell (
                    grid_snapshot_id, grid_cell_id, longitude, latitude, canonical_ordinal
                )
                VALUES (%s, '0', 63.0, 8.0, 1),
                       (%s, '1', 63.25, 8.0, 2),
                       (%s, '2', 63.5, 8.0, 3)
                """,
                (snap_id, snap_id, snap_id),
            )
            cursor.execute(
                "SELECT COUNT(*) AS n FROM met.canonical_grid_cell WHERE grid_snapshot_id = %s",
                (snap_id,),
            )
            assert cursor.fetchone()["n"] == 3
            # CASCADE path: this must NOT raise, and it must remove all child cells.
            cursor.execute(
                "DELETE FROM met.canonical_grid_snapshot WHERE grid_snapshot_id = %s",
                (snap_id,),
            )
            cursor.execute(
                "SELECT COUNT(*) AS n FROM met.canonical_grid_cell WHERE grid_snapshot_id = %s",
                (snap_id,),
            )
            assert cursor.fetchone()["n"] == 0
    finally:
        connection.close()


@pytest.mark.integration
def test_canonical_met_product_grid_snapshot_fk_column_exists_and_nullable(
    migrated_database: str,
) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'met'
                  AND table_name = 'canonical_met_product'
                  AND column_name = 'grid_snapshot_id'
                """
            )
            row = cursor.fetchone()
        assert row is not None, "grid_snapshot_id FK column missing on canonical_met_product"
        assert row["data_type"] == "uuid"
        assert row["is_nullable"] == "YES"
    finally:
        connection.close()


@pytest.mark.integration
def test_canonical_met_product_rejects_unregistered_snapshot_fk(
    migrated_database: str,
) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_prodfk_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
        fake_snapshot_id = str(uuid.uuid4())
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO met.canonical_met_product (
                        canonical_product_id, source_id, cycle_time, valid_time,
                        variable, unit, grid_id, object_uri, checksum,
                        grid_snapshot_id
                    ) VALUES (
                        %s, %s, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z',
                        'Prcp', 'mm/h', 'test_grid', 's3://x', 'chk', %s
                    )
                    """,
                    (f"{RUN_PREFIX}_prodrow", source_id, fake_snapshot_id),
                )
    finally:
        connection.close()


@pytest.mark.integration
def test_met_station_and_interp_weight_supersession_columns(
    migrated_database: str,
) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'met' AND table_name = 'met_station'
                  AND column_name IN ('active_flag', 'superseded_at', 'grid_snapshot_id')
                """
            )
            station_cols = {row["column_name"]: row for row in cursor.fetchall()}
        assert station_cols["superseded_at"]["data_type"] == "timestamp with time zone"
        assert station_cols["superseded_at"]["is_nullable"] == "YES"
        assert station_cols["grid_snapshot_id"]["data_type"] == "uuid"
        assert station_cols["grid_snapshot_id"]["is_nullable"] == "YES"
        # Existing active_flag from 000005 is retained unchanged.
        assert station_cols["active_flag"]["is_nullable"] == "NO"

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'met' AND table_name = 'interp_weight'
                  AND column_name IN ('active_flag', 'superseded_at', 'grid_snapshot_id')
                """
            )
            weight_cols = {row["column_name"]: row for row in cursor.fetchall()}
        assert weight_cols["active_flag"]["data_type"] == "boolean"
        assert weight_cols["active_flag"]["is_nullable"] == "NO"
        assert "true" in (weight_cols["active_flag"]["column_default"] or "").lower()
        assert weight_cols["superseded_at"]["data_type"] == "timestamp with time zone"
        assert weight_cols["superseded_at"]["is_nullable"] == "YES"
        assert weight_cols["grid_snapshot_id"]["data_type"] == "uuid"
        assert weight_cols["grid_snapshot_id"]["is_nullable"] == "YES"
    finally:
        connection.close()


@pytest.mark.integration
def test_met_station_rejects_unregistered_snapshot_fk(migrated_database: str) -> None:
    """Existing rows loaded without a snapshot are allowed (NULL); an unregistered
    non-NULL FK must be rejected."""
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_msfk_src"
        basin_id = f"{RUN_PREFIX}_msfk_basin"
        basin_version_id = f"{RUN_PREFIX}_msfk_bv"
        station_id = f"{RUN_PREFIX}_msfk_station"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            cursor.execute(
                """
                INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
                VALUES (%s, 'msfk basin', 'test', 'msfk')
                ON CONFLICT (basin_id) DO NOTHING
                """,
                (basin_id,),
            )
            cursor.execute(
                """
                INSERT INTO core.basin_version (
                    basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
                )
                VALUES (
                    %s, %s, 'v1', ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490)),
                    true, 'test://basin', 'basin-sha'
                )
                ON CONFLICT (basin_version_id) DO NOTHING
                """,
                (basin_version_id, basin_id),
            )
        fake_snapshot_id = str(uuid.uuid4())
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO met.met_station (
                        station_id, basin_version_id, geom, grid_snapshot_id
                    ) VALUES (
                        %s, %s, ST_SetSRID(ST_MakePoint(110.0, 30.0), 4490), %s
                    )
                    """,
                    (station_id, basin_version_id, fake_snapshot_id),
                )
    finally:
        connection.close()


@pytest.mark.integration
def test_interp_weight_rejects_unregistered_snapshot_fk(migrated_database: str) -> None:
    """Mirror of the met_station FK test for met.interp_weight — pre-existing rows
    load with grid_snapshot_id NULL; any non-NULL FK MUST reference a registered
    snapshot. Task 2.1 Evidence Floor bullet 3 requires this."""
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_iwfk_src"
        basin_id = f"{RUN_PREFIX}_iwfk_basin"
        basin_version_id = f"{RUN_PREFIX}_iwfk_bv"
        river_network_version_id = f"{RUN_PREFIX}_iwfk_rnv"
        mesh_version_id = f"{RUN_PREFIX}_iwfk_mesh"
        model_id = f"{RUN_PREFIX}_iwfk_model"
        station_id = f"{RUN_PREFIX}_iwfk_station"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            cursor.execute(
                """
                INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
                VALUES (%s, 'iwfk basin', 'test', 'iwfk')
                ON CONFLICT (basin_id) DO NOTHING
                """,
                (basin_id,),
            )
            cursor.execute(
                """
                INSERT INTO core.basin_version (
                    basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
                )
                VALUES (
                    %s, %s, 'v1', ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490)),
                    true, 'test://basin', 'basin-sha'
                )
                ON CONFLICT (basin_version_id) DO NOTHING
                """,
                (basin_version_id, basin_id),
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version (
                    river_network_version_id, basin_version_id, version_label,
                    segment_count, source_uri, checksum
                )
                VALUES (%s, %s, 'v1', 1, 'test://river-network', 'rnv-sha')
                ON CONFLICT (river_network_version_id) DO NOTHING
                """,
                (river_network_version_id, basin_version_id),
            )
            cursor.execute(
                """
                INSERT INTO core.mesh_version (
                    mesh_version_id, basin_version_id, version_label,
                    mesh_uri, checksum, properties_json
                )
                VALUES (%s, %s, 'v1', 'test://mesh', 'mesh-sha', %s)
                ON CONFLICT (mesh_version_id) DO NOTHING
                """,
                (mesh_version_id, basin_version_id, Json({"cell_count": 1})),
            )
            # model_instance schema derives from 000004_core.sql (base columns) +
            # 000022_model_asset_lifecycle.sql (adds lifecycle_state).
            cursor.execute(
                """
                INSERT INTO core.model_instance (
                    model_id, basin_version_id, river_network_version_id,
                    mesh_version_id, calibration_version_id, shud_code_version,
                    model_package_uri, active_flag, lifecycle_state, resource_profile
                )
                VALUES (
                    %s, %s, %s, %s, 'calib-v1', 'shud-v1',
                    's3://nhms/models/iwfk/package/', true, 'active', %s
                )
                ON CONFLICT (model_id) DO NOTHING
                """,
                (
                    model_id,
                    basin_version_id,
                    river_network_version_id,
                    mesh_version_id,
                    Json({"partition": "test"}),
                ),
            )
            cursor.execute(
                """
                INSERT INTO met.met_station (
                    station_id, basin_version_id, geom
                ) VALUES (
                    %s, %s, ST_SetSRID(ST_MakePoint(110.0, 30.0), 4490)
                )
                ON CONFLICT (station_id) DO NOTHING
                """,
                (station_id, basin_version_id),
            )
        fake_snapshot_id = str(uuid.uuid4())
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO met.interp_weight (
                        source_id, grid_id, model_id, station_id, variable,
                        grid_cell_id, weight, method, grid_snapshot_id
                    ) VALUES (
                        %s, %s, %s, %s, 'Prcp', '0', 1.0, 'idw', %s
                    )
                    """,
                    (
                        source_id,
                        f"{RUN_PREFIX}_iwfk_grid",
                        model_id,
                        station_id,
                        fake_snapshot_id,
                    ),
                )
    finally:
        connection.close()


@pytest.mark.integration
def test_uri_match_trigger_rejects_mismatched_grid_definition_uri(
    migrated_database: str,
) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_urimatch_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_urim",
                canonical_grid_key=f"{RUN_PREFIX}_key_urim",
                grid_definition_uri="s3://nhms/canonical/registered/grid.json",
            )
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO met.canonical_met_product (
                        canonical_product_id, source_id, cycle_time, valid_time,
                        variable, unit, grid_id, grid_definition_uri, object_uri,
                        checksum, grid_snapshot_id
                    ) VALUES (
                        %s, %s, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z',
                        'Prcp', 'mm/h', 'test_grid', %s, 's3://x', 'chk', %s
                    )
                    """,
                    (
                        f"{RUN_PREFIX}_urim_prod",
                        source_id,
                        "s3://nhms/canonical/DRIFTED/grid.json",
                        snap_id,
                    ),
                )
        message = str(excinfo.value)
        assert "canonical_met_product.grid_definition_uri" in message
        assert "s3://nhms/canonical/DRIFTED/grid.json" in message
        assert "s3://nhms/canonical/registered/grid.json" in message
    finally:
        connection.close()


@pytest.mark.integration
def test_uri_match_trigger_allows_null_grid_definition_uri(
    migrated_database: str,
) -> None:
    """When grid_definition_uri is NULL the cross-check is skipped and only the FK
    enforces identity; the row inserts cleanly."""
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_uriok_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_uriok",
                canonical_grid_key=f"{RUN_PREFIX}_key_uriok",
                grid_definition_uri="s3://nhms/canonical/ok/grid.json",
            )
            cursor.execute(
                """
                INSERT INTO met.canonical_met_product (
                    canonical_product_id, source_id, cycle_time, valid_time,
                    variable, unit, grid_id, object_uri, checksum,
                    grid_snapshot_id
                ) VALUES (
                    %s, %s, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z',
                    'Prcp', 'mm/h', 'test_grid', 's3://x', 'chk', %s
                )
                """,
                (f"{RUN_PREFIX}_uriok_prod", source_id, snap_id),
            )
    finally:
        connection.close()


@pytest.mark.integration
def test_identity_immutability_trigger_rejects_grid_signature_edit(
    migrated_database: str,
) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_immut_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_immut",
                canonical_grid_key=f"{RUN_PREFIX}_key_immut",
                grid_definition_uri="s3://nhms/canonical/immut/grid.json",
                grid_signature="original_signature",
            )
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE met.canonical_grid_snapshot
                    SET grid_signature = 'mutated_signature'
                    WHERE grid_snapshot_id = %s
                    """,
                    (snap_id,),
                )
        assert "grid_signature is immutable" in str(excinfo.value)
    finally:
        connection.close()


@pytest.mark.integration
def test_identity_immutability_trigger_rejects_uri_and_bbox_edits(
    migrated_database: str,
) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        source_id = f"{RUN_PREFIX}_immut2_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_immut2",
                canonical_grid_key=f"{RUN_PREFIX}_key_immut2",
                grid_definition_uri="s3://nhms/canonical/immut2/grid.json",
            )
        # URI mutation blocked.
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE met.canonical_grid_snapshot
                    SET grid_definition_uri = 's3://nhms/canonical/mutated/grid.json'
                    WHERE grid_snapshot_id = %s
                    """,
                    (snap_id,),
                )
        assert "grid_definition_uri is immutable" in str(excinfo.value)
        # BBox mutation blocked.
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE met.canonical_grid_snapshot
                    SET bbox_west = 0.0
                    WHERE grid_snapshot_id = %s
                    """,
                    (snap_id,),
                )
        assert "bbox is immutable" in str(excinfo.value)
    finally:
        connection.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("field", "new_value_sql", "expected_message"),
    [
        (
            "grid_definition_checksum",
            "'mutated-checksum-value'",
            "grid_definition_checksum is immutable",
        ),
        (
            "canonical_grid_key",
            "'mutated_canonical_grid_key'",
            "canonical_grid_key is immutable",
        ),
        (
            "native_resolution",
            "0.5",
            "native_resolution is immutable",
        ),
    ],
)
def test_identity_immutability_trigger_rejects_additional_identity_field_edits(
    migrated_database: str, field: str, new_value_sql: str, expected_message: str
) -> None:
    """The identity trigger guards 6 field-groups; this covers the 3 that had no
    runtime coverage (checksum, canonical_grid_key, native_resolution). A
    regression that removed one of the IS DISTINCT FROM checks would slip past
    the static-SQL grep test in isolation."""
    connection = _fresh_connection(migrated_database)
    try:
        # Use the field name in the id to keep parametrized snapshots distinct.
        source_id = f"{RUN_PREFIX}_immut3_{field}_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id,
                grid_id=f"{RUN_PREFIX}_grid_immut3_{field}",
                canonical_grid_key=f"{RUN_PREFIX}_key_immut3_{field}",
                grid_definition_uri=f"s3://nhms/canonical/immut3_{field}/grid.json",
            )
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE met.canonical_grid_snapshot
                    SET {field} = {new_value_sql}
                    WHERE grid_snapshot_id = %s
                    """,
                    (snap_id,),
                )
        assert expected_message in str(excinfo.value), (
            f"expected {expected_message!r} in error, got {excinfo.value!r}"
        )
    finally:
        connection.close()


@pytest.mark.integration
def test_identity_immutability_trigger_allows_supersession_and_applicable_source_edits(
    migrated_database: str,
) -> None:
    connection = _fresh_connection(migrated_database)
    try:
        source_id_a = f"{RUN_PREFIX}_ok1_src"
        source_id_b = f"{RUN_PREFIX}_ok2_src"
        with connection.cursor() as cursor:
            _seed_data_source(cursor, source_id_a)
            _seed_data_source(cursor, source_id_b)
            snap_id = _insert_snapshot(
                cursor,
                source_id=source_id_a,
                grid_id=f"{RUN_PREFIX}_grid_ok",
                canonical_grid_key=f"{RUN_PREFIX}_key_ok",
                grid_definition_uri="s3://nhms/canonical/ok2/grid.json",
            )
            # Supersession stamp: permitted.
            cursor.execute(
                """
                UPDATE met.canonical_grid_snapshot
                SET superseded_at = now()
                WHERE grid_snapshot_id = %s
                """,
                (snap_id,),
            )
            # applicable_source_ids extension: permitted.
            cursor.execute(
                """
                UPDATE met.canonical_grid_snapshot
                SET applicable_source_ids = ARRAY[%s, %s]::TEXT[]
                WHERE grid_snapshot_id = %s
                """,
                (source_id_a, source_id_b, snap_id),
            )
            cursor.execute(
                """
                SELECT superseded_at, applicable_source_ids
                FROM met.canonical_grid_snapshot
                WHERE grid_snapshot_id = %s
                """,
                (snap_id,),
            )
            row = cursor.fetchone()
            assert row["superseded_at"] is not None
            assert set(row["applicable_source_ids"]) == {source_id_a, source_id_b}
    finally:
        connection.close()


@pytest.mark.integration
def test_pre_existing_columns_not_narrowed_after_migration(
    migrated_database: str,
) -> None:
    """Task 2.1 Evidence Floor bullet 4 requires that the migration NOT narrow
    pre-existing NOT NULL / column-type / default invariants on the three
    tables it appends nullable columns to. This test whitelists the columns
    defined in db/migrations/000005_met.sql:27-72 (i.e. every pre-existing
    column) and asserts (a) each column still exists, (b) its data_type and
    is_nullable are unchanged, (c) where relevant its column_default matches.

    The whitelists are curated by hand from 000005_met.sql; if that schema
    is legitimately changed by a later migration, update this whitelist in
    the same PR so the check remains truthful.
    """
    # Each entry maps column_name -> (expected_is_nullable, expected_data_type,
    # optional column_default LIKE-fragment; None means default is not checked
    # because it either does not exist or is not load-bearing).
    expected_columns: dict[str, dict[str, tuple[str, str, str | None]]] = {
        "canonical_met_product": {
            "canonical_product_id": ("NO", "text", None),
            "source_id": ("NO", "text", None),
            "cycle_time": ("NO", "timestamp with time zone", None),
            "valid_time": ("NO", "timestamp with time zone", None),
            "variable": ("NO", "text", None),
            "unit": ("NO", "text", None),
            "grid_id": ("NO", "text", None),
            "object_uri": ("NO", "text", None),
            "checksum": ("NO", "text", None),
            "quality_flag": ("NO", "text", "%ok%"),
            "lineage_json": ("NO", "jsonb", None),
            "created_at": ("NO", "timestamp with time zone", "%now()%"),
        },
        "met_station": {
            "station_id": ("NO", "text", None),
            "basin_version_id": ("NO", "text", None),
            "geom": ("NO", "USER-DEFINED", None),
            "station_role": ("NO", "text", "%forcing_proxy%"),
            "active_flag": ("NO", "boolean", "%true%"),
            "properties_json": ("NO", "jsonb", None),
            "created_at": ("NO", "timestamp with time zone", None),
        },
        "interp_weight": {
            "weight_id": ("NO", "bigint", None),
            "source_id": ("NO", "text", None),
            "grid_id": ("NO", "text", None),
            "model_id": ("NO", "text", None),
            "station_id": ("NO", "text", None),
            "variable": ("NO", "text", None),
            "grid_cell_id": ("NO", "text", None),
            "weight": ("NO", "double precision", None),
            "method": ("NO", "text", None),
            "created_at": ("NO", "timestamp with time zone", None),
        },
    }
    connection = _fresh_connection(migrated_database)
    try:
        for table_name, columns in expected_columns.items():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT column_name, is_nullable, data_type, column_default
                    FROM information_schema.columns
                    WHERE table_schema = 'met'
                      AND table_name = %s
                    """,
                    (table_name,),
                )
                actual = {row["column_name"]: row for row in cursor.fetchall()}
            for column_name, (
                expected_nullable,
                expected_type,
                default_like,
            ) in columns.items():
                assert column_name in actual, (
                    f"met.{table_name}.{column_name} was removed by a later "
                    f"migration; pre-existing invariant broken"
                )
                row = actual[column_name]
                assert row["is_nullable"] == expected_nullable, (
                    f"met.{table_name}.{column_name} nullability drifted: "
                    f"expected {expected_nullable}, got {row['is_nullable']}"
                )
                assert row["data_type"] == expected_type, (
                    f"met.{table_name}.{column_name} data_type drifted: "
                    f"expected {expected_type}, got {row['data_type']}"
                )
                if default_like is not None:
                    actual_default = (row["column_default"] or "").lower()
                    like_stripped = default_like.strip("%").lower()
                    assert like_stripped in actual_default, (
                        f"met.{table_name}.{column_name} default drifted: "
                        f"expected LIKE {default_like!r}, got {row['column_default']!r}"
                    )
    finally:
        connection.close()


@pytest.mark.integration
def test_migration_is_idempotent(migrated_database: str) -> None:
    """Re-applying the migration file after schema_migrations has recorded it must
    be a no-op; explicitly rerunning the statements against the same DB must not
    raise a duplicate-object error. Also asserts the exact 4-trigger and
    4-function count so a stray re-declaration would be caught."""
    from packages.common.migrate import apply_migration

    connection = psycopg2.connect(migrated_database)
    connection.autocommit = True
    try:
        # Idempotency: the migration file's IF NOT EXISTS + DROP TRIGGER IF EXISTS
        # guards must let apply_migration succeed a second time.
        apply_migration(connection, MIGRATION_PATH)
        # No side effect: table row counts unchanged (nothing is inserted by
        # applying the DDL again).
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM met.canonical_grid_snapshot")
            snapshot_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM met.canonical_grid_cell")
            cell_count = cursor.fetchone()[0]
        # Whatever integration-test rows accumulated remain; the count is
        # nonnegative and the migration re-application does not throw.
        assert snapshot_count >= 0
        assert cell_count >= 0
        # After re-apply, exactly the 4 declared triggers + 4 declared functions
        # must exist — no duplicates, no missing entries. A stray re-declaration
        # or a rename would be caught here.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS n FROM pg_trigger
                 WHERE tgname IN (
                   'canonical_met_product_grid_definition_uri_match_trg',
                   'canonical_grid_snapshot_identity_immutable_trg',
                   'canonical_grid_cell_immutable_trg',
                   'canonical_grid_cell_direct_delete_blocked_trg'
                 )
                """
            )
            assert cursor.fetchone()[0] == 4, "expected 4 named triggers post re-apply"
            cursor.execute(
                """
                SELECT COUNT(*) AS n FROM pg_proc p
                 JOIN pg_namespace n ON p.pronamespace = n.oid
                 WHERE n.nspname = 'met'
                   AND p.proname IN (
                     'canonical_met_product_grid_definition_uri_match',
                     'canonical_grid_snapshot_identity_immutable',
                     'canonical_grid_cell_immutable',
                     'canonical_grid_cell_direct_delete_blocked'
                   )
                """
            )
            assert cursor.fetchone()[0] == 4, "expected 4 named functions post re-apply"
    finally:
        connection.close()
