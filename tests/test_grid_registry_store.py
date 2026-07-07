"""Tests for packages/common/grid_registry_store.py.

Covers issue #900 (Epic #897 SUB-3) Task 2.2 Evidence Floor:

* round-trip identity (snapshot + ordered cells + ORDER-preserved
  ``applicable_source_ids`` + checksum-verified load),
* non-contiguous ``canonical_ordinal`` rejection with a structured error,
* checksum-mismatch fails closed on load,
* per-field mutation rejection at the store API layer (snapshot DELETE, cell
  DELETE, and identity-field UPDATE),
* mid-write atomicity (a failure mid-INSERT leaves zero rows),
* the two permitted post-insert writes (``supersede`` /
  ``extend_applicable_source_ids``).

Static tests always run. Integration tests are marked with
``pytest.mark.integration`` and require ``NHMS_RUN_INTEGRATION=1`` +
``NHMS_INTEGRATION_DATABASE_URL`` (SKIP is expected locally).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import psycopg2.extras
import pytest
from psycopg2.extras import Json

from packages.common.grid_registry_store import (
    CanonicalGridCell,
    CanonicalGridSnapshot,
    PsycopgGridRegistryStore,
    RegistryChecksumError,
    RegistryContiguityError,
    RegistryImmutabilityError,
    RegistryStoreError,
    default_database_url,
)
from packages.common.object_store import sha256_bytes
from tests.integration_helpers import apply_migrations_from_zero

RUN_PREFIX = "sub3_900"


# -----------------------------------------------------------------------------
# Always-run static shape tests: exception hierarchy, env handling, rejection
# API surface. No DB / psycopg2 connection required.
# -----------------------------------------------------------------------------


def test_registry_store_exception_hierarchy() -> None:
    """The four exception classes exist and inherit from RegistryStoreError,
    which itself is a RuntimeError subclass. Consumers rely on this so `except
    RegistryStoreError` catches every store rejection surface."""
    assert issubclass(RegistryStoreError, RuntimeError)
    assert issubclass(RegistryImmutabilityError, RegistryStoreError)
    assert issubclass(RegistryContiguityError, RegistryStoreError)
    assert issubclass(RegistryChecksumError, RegistryStoreError)


def test_default_database_url_raises_when_env_missing(monkeypatch: Any) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RegistryStoreError, match="DATABASE_URL"):
        default_database_url()


def test_default_database_url_raises_when_env_empty(monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", "   ")
    with pytest.raises(RegistryStoreError, match="DATABASE_URL"):
        default_database_url()


def test_from_env_raises_when_database_url_missing(monkeypatch: Any) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RegistryStoreError, match="DATABASE_URL"):
        PsycopgGridRegistryStore.from_env()


def test_delete_snapshot_always_raises_immutability_error() -> None:
    """delete_snapshot MUST raise before opening any DB connection; the store
    surface is the sole API-level rejection oracle per SUB-2 migration line
    24-27's delegation to SUB-3."""
    store = PsycopgGridRegistryStore(database_url="postgres://never-touched")
    snapshot_id = uuid.uuid4()
    with pytest.raises(RegistryImmutabilityError) as excinfo:
        store.delete_snapshot(snapshot_id)
    assert excinfo.value.grid_snapshot_id == snapshot_id
    assert excinfo.value.field_or_op == "delete_snapshot"


def test_delete_cell_always_raises_immutability_error() -> None:
    """delete_cell MUST raise before opening any DB connection. Per-cell rows
    are enforced immutable by SUB-2 Triggers C/D at the DB layer; the store
    additionally raises at the API layer so callers cannot bypass by
    crafting raw SQL through this API."""
    store = PsycopgGridRegistryStore(database_url="postgres://never-touched")
    snapshot_id = uuid.uuid4()
    with pytest.raises(RegistryImmutabilityError) as excinfo:
        store.delete_cell(snapshot_id, "12345")
    assert excinfo.value.grid_snapshot_id == snapshot_id
    assert excinfo.value.field_or_op == "delete_cell(12345)"


@pytest.mark.parametrize(
    "field",
    [
        "grid_signature",
        "grid_definition_uri",
        "grid_definition_checksum",
        "canonical_grid_key",
        "bbox_south",
        "bbox_north",
        "bbox_west",
        "bbox_east",
    ],
)
def test_update_identity_field_always_raises_immutability_error(field: str) -> None:
    """The 6 identity field-groups enumerated by spec.md:96 MUST all reject
    mutation at the store API layer. The store never touches the DB for
    these calls."""
    store = PsycopgGridRegistryStore(database_url="postgres://never-touched")
    snapshot_id = uuid.uuid4()
    with pytest.raises(RegistryImmutabilityError) as excinfo:
        store.update_identity_field(snapshot_id, field, "new-value")
    assert excinfo.value.grid_snapshot_id == snapshot_id
    assert excinfo.value.field_or_op == field


def test_contiguity_error_carries_structured_attributes() -> None:
    """RegistryContiguityError MUST expose expected / actual / gap so callers
    (test asserts, monitoring, PR reviewers) can inspect the offending gap
    without regex-parsing the string. `{1, 2, 4}` for a 3-cell snapshot
    reports gap=3."""
    error = RegistryContiguityError(expected=[1, 2, 3], actual=[1, 2, 4], gap=3)
    assert error.expected == [1, 2, 3]
    assert error.actual == [1, 2, 4]
    assert error.gap == 3
    # And the base RegistryStoreError catches it.
    assert isinstance(error, RegistryStoreError)


def test_checksum_error_carries_structured_attributes() -> None:
    """RegistryChecksumError MUST expose expected_hash / actual_hash / uri so
    the caller can build a structured drift receipt without regex."""
    error = RegistryChecksumError(
        expected_hash="deadbeef",
        actual_hash="cafefeed",
        uri="s3://nhms/canonical/x/grid.json",
    )
    assert error.expected_hash == "deadbeef"
    assert error.actual_hash == "cafefeed"
    assert error.uri == "s3://nhms/canonical/x/grid.json"


# -----------------------------------------------------------------------------
# Real-DB integration tests. Skipped locally without NHMS_RUN_INTEGRATION=1.
# -----------------------------------------------------------------------------


# Note: every integration test carries its own ``@pytest.mark.integration``
# decorator; a module-level ``pytestmark = pytest.mark.integration`` would poison
# the always-run static tests above with the integration marker.


@pytest.fixture(scope="module")
def migrated_database(integration_database_url: str) -> str:
    """Apply all migrations once per module and yield the database URL."""
    apply_migrations_from_zero(integration_database_url)
    _seed_normalized_data_sources(integration_database_url)
    return integration_database_url


def _seed_normalized_data_sources(database_url: str) -> None:
    """Seed the three normalized source ids (`IFS`, `gfs`, `ERA5`) into
    met.data_source so snapshot INSERTs whose source_id passes through
    ``normalize_source_id`` do not FK-fail."""
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            for source_id in ("IFS", "gfs", "ERA5"):
                cursor.execute(
                    """
                    INSERT INTO met.data_source (
                        source_id, source_name, source_type, status, native_format,
                        adapter_name, config_json
                    )
                    VALUES (%s, %s, 'forecast', 'mock', 'netcdf', %s, %s)
                    ON CONFLICT (source_id) DO NOTHING
                    """,
                    (source_id, f"{source_id} test source", source_id, Json({"test": True})),
                )
    finally:
        connection.close()


def _make_snapshot(
    *,
    canonical_grid_key: str,
    source_id: str = "IFS",
    grid_id: str = "grid_a",
    grid_signature: str = "sig-a",
    grid_definition_uri: str = "s3://nhms/canonical/a/grid.json",
    grid_definition_checksum: str = "checksum-a",
    applicable_source_ids: tuple[str, ...] = ("IFS",),
) -> CanonicalGridSnapshot:
    """Construct a fully-populated CanonicalGridSnapshot for tests."""
    return CanonicalGridSnapshot(
        grid_snapshot_id=None,
        canonical_grid_key=canonical_grid_key,
        source_id=source_id,
        grid_id=grid_id,
        grid_signature=grid_signature,
        grid_definition_uri=grid_definition_uri,
        grid_definition_checksum=grid_definition_checksum,
        longitude_convention="[-180,180)",
        latitude_order="descending",
        flatten_order="y_major_lat_then_lon",
        native_resolution=0.25,
        bbox_south=8.0,
        bbox_north=64.0,
        bbox_west=63.0,
        bbox_east=145.0,
        converter_version="converter-v1",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_to=None,
        applicable_source_ids=applicable_source_ids,
    )


def _make_cells(count: int) -> list[CanonicalGridCell]:
    return [
        CanonicalGridCell(
            grid_cell_id=str(index),
            longitude=63.0 + 0.25 * index,
            latitude=8.0,
            canonical_ordinal=index + 1,
        )
        for index in range(count)
    ]


def _count_rows(database_url: str, *, grid_snapshot_id: uuid.UUID | str) -> tuple[int, int]:
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM met.canonical_grid_snapshot WHERE grid_snapshot_id = %s",
                (str(grid_snapshot_id),),
            )
            snap_count = cursor.fetchone()[0]
            cursor.execute(
                "SELECT COUNT(*) FROM met.canonical_grid_cell WHERE grid_snapshot_id = %s",
                (str(grid_snapshot_id),),
            )
            cell_count = cursor.fetchone()[0]
        return snap_count, cell_count
    finally:
        connection.close()


def _fetch_snapshot_row(database_url: str, grid_snapshot_id: uuid.UUID) -> dict[str, Any]:
    connection = psycopg2.connect(
        database_url, cursor_factory=psycopg2.extras.RealDictCursor
    )
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM met.canonical_grid_snapshot
                WHERE grid_snapshot_id = %s
                """,
                (str(grid_snapshot_id),),
            )
            row = cursor.fetchone()
        assert row is not None
        return dict(row)
    finally:
        connection.close()


@pytest.mark.integration
def test_round_trip_identity_preserves_all_fields(migrated_database: str) -> None:
    """A snapshot with 6 cells round-trips: signature str-eq, checksum str-eq,
    bbox exact, applicable_source_ids ORDER-preserved (list equality, not
    set), 6 cells with canonical_ordinal 1..6 ascending."""
    store = PsycopgGridRegistryStore(database_url=migrated_database)
    content = b"grid definition bytes for round-trip test"
    checksum = sha256_bytes(content)
    inserted = _make_snapshot(
        canonical_grid_key=f"{RUN_PREFIX}_key_roundtrip",
        grid_id=f"{RUN_PREFIX}_grid_roundtrip",
        grid_signature=f"{RUN_PREFIX}-sig-roundtrip",
        grid_definition_uri=f"s3://nhms/canonical/{RUN_PREFIX}/roundtrip/grid.json",
        grid_definition_checksum=checksum,
        # ORDER matters: IFS then gfs; a set-based store would sort them.
        applicable_source_ids=("IFS", "gfs"),
    )
    cells = _make_cells(6)
    snapshot_id = store.insert_snapshot(inserted, cells)
    assert isinstance(snapshot_id, uuid.UUID)

    def reader(uri: str) -> bytes:
        assert uri == inserted.grid_definition_uri
        return content

    loaded, loaded_cells = store.load_snapshot(snapshot_id, object_reader=reader)

    # Identity fields — every one must be preserved byte-for-byte.
    assert loaded.grid_snapshot_id == snapshot_id
    assert loaded.canonical_grid_key == inserted.canonical_grid_key
    assert loaded.source_id == "IFS"  # normalized on write
    assert loaded.grid_id == inserted.grid_id
    assert loaded.grid_signature == inserted.grid_signature
    assert loaded.grid_definition_uri == inserted.grid_definition_uri
    assert loaded.grid_definition_checksum == checksum
    assert loaded.longitude_convention == inserted.longitude_convention
    assert loaded.latitude_order == inserted.latitude_order
    assert loaded.flatten_order == inserted.flatten_order
    assert loaded.native_resolution == inserted.native_resolution
    assert loaded.bbox_south == inserted.bbox_south
    assert loaded.bbox_north == inserted.bbox_north
    assert loaded.bbox_west == inserted.bbox_west
    assert loaded.bbox_east == inserted.bbox_east
    assert loaded.converter_version == inserted.converter_version
    assert loaded.valid_from == inserted.valid_from
    assert loaded.valid_to is None
    # ORDER-preserving TEXT[] round trip: list equality, not set membership.
    assert list(loaded.applicable_source_ids) == ["IFS", "gfs"]
    assert loaded.superseded_at is None
    assert loaded.created_at is not None

    # Cells: 1..6 ascending, per-cell identity intact.
    assert len(loaded_cells) == 6
    assert [c.canonical_ordinal for c in loaded_cells] == [1, 2, 3, 4, 5, 6]
    assert [c.grid_cell_id for c in loaded_cells] == ["0", "1", "2", "3", "4", "5"]
    for original, roundtripped in zip(cells, loaded_cells, strict=True):
        assert roundtripped.grid_cell_id == original.grid_cell_id
        assert roundtripped.longitude == original.longitude
        assert roundtripped.latitude == original.latitude
        assert roundtripped.canonical_ordinal == original.canonical_ordinal


@pytest.mark.integration
@pytest.mark.parametrize(
    ("ordinals", "gap"),
    [
        # Missing "3" in the middle: expected {1,2,3}, actual {1,2,4}.
        ([1, 2, 4], 3),
        # Starts below 1 (0-based ordinals): out-of-range value 0.
        ([0, 1, 2], 0),
        # Extra beyond N=4 (expected {1,2,3,4}, actual has 5 which is > N).
        ([1, 2, 3, 5], 5),
    ],
)
def test_non_contiguous_ordinals_rejected_and_no_rows_written(
    migrated_database: str, ordinals: list[int], gap: int
) -> None:
    """Non-contiguous ``canonical_ordinal`` sets MUST raise
    RegistryContiguityError. The store validates BEFORE opening the DB
    transaction, so afterwards no snapshot or cell rows exist for any UUID
    that might have been generated."""
    store = PsycopgGridRegistryStore(database_url=migrated_database)
    unique_key = f"{RUN_PREFIX}_key_gap_{'_'.join(str(x) for x in ordinals)}"
    snapshot = _make_snapshot(
        canonical_grid_key=unique_key,
        grid_id=f"{RUN_PREFIX}_grid_gap",
        grid_signature=f"{RUN_PREFIX}-sig-gap-{ordinals}",
        grid_definition_uri=f"s3://nhms/canonical/{RUN_PREFIX}/gap-{ordinals}/grid.json",
    )
    cells = [
        CanonicalGridCell(
            grid_cell_id=str(idx),
            longitude=63.0 + 0.25 * idx,
            latitude=8.0,
            canonical_ordinal=ordinal,
        )
        for idx, ordinal in enumerate(ordinals)
    ]
    with pytest.raises(RegistryContiguityError) as excinfo:
        store.insert_snapshot(snapshot, cells)
    assert excinfo.value.gap == gap

    # And after rejection: the store did not touch the DB at all, so the row
    # count for the unique canonical_grid_key remains 0.
    connection = psycopg2.connect(migrated_database)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM met.canonical_grid_snapshot
                WHERE canonical_grid_key = %s
                """,
                (unique_key,),
            )
            assert cursor.fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.integration
def test_checksum_mismatch_fails_closed_on_load(migrated_database: str) -> None:
    """When the ``object_reader`` returns bytes whose SHA-256 does not match
    the stored ``grid_definition_checksum``, ``load_snapshot`` MUST raise
    ``RegistryChecksumError`` with structured attributes. The registry row
    is not modified by the failed load."""
    store = PsycopgGridRegistryStore(database_url=migrated_database)
    stored_checksum = "deadbeef" * 8  # 64 hex chars
    snapshot = _make_snapshot(
        canonical_grid_key=f"{RUN_PREFIX}_key_chksum",
        grid_id=f"{RUN_PREFIX}_grid_chksum",
        grid_signature=f"{RUN_PREFIX}-sig-chksum",
        grid_definition_uri=f"s3://nhms/canonical/{RUN_PREFIX}/chksum/grid.json",
        grid_definition_checksum=stored_checksum,
    )
    snapshot_id = store.insert_snapshot(snapshot, _make_cells(3))

    drift_bytes = b"drifted grid definition"
    drift_hash = sha256_bytes(drift_bytes)
    assert drift_hash != stored_checksum

    def drifted_reader(uri: str) -> bytes:
        assert uri == snapshot.grid_definition_uri
        return drift_bytes

    with pytest.raises(RegistryChecksumError) as excinfo:
        store.load_snapshot(snapshot_id, object_reader=drifted_reader)
    error = excinfo.value
    assert error.expected_hash == stored_checksum
    assert error.actual_hash == drift_hash
    assert error.uri == snapshot.grid_definition_uri

    # Verify no side effect: the snapshot row is byte-identical before/after.
    row = _fetch_snapshot_row(migrated_database, snapshot_id)
    assert row["grid_definition_checksum"] == stored_checksum


@pytest.mark.integration
def test_delete_snapshot_via_store_api_rejected_and_row_unchanged(
    migrated_database: str,
) -> None:
    """The store's ``delete_snapshot`` MUST raise before touching the DB;
    afterwards the DB row is byte-identical to what was inserted."""
    store = PsycopgGridRegistryStore(database_url=migrated_database)
    snapshot = _make_snapshot(
        canonical_grid_key=f"{RUN_PREFIX}_key_delsnap",
        grid_id=f"{RUN_PREFIX}_grid_delsnap",
        grid_signature=f"{RUN_PREFIX}-sig-delsnap",
        grid_definition_uri=f"s3://nhms/canonical/{RUN_PREFIX}/delsnap/grid.json",
        grid_definition_checksum="deadbeef",
    )
    snapshot_id = store.insert_snapshot(snapshot, _make_cells(2))
    row_before = _fetch_snapshot_row(migrated_database, snapshot_id)

    with pytest.raises(RegistryImmutabilityError) as excinfo:
        store.delete_snapshot(snapshot_id)
    assert excinfo.value.field_or_op == "delete_snapshot"

    row_after = _fetch_snapshot_row(migrated_database, snapshot_id)
    assert row_before == row_after
    snap_count, cell_count = _count_rows(migrated_database, grid_snapshot_id=snapshot_id)
    assert snap_count == 1
    assert cell_count == 2


@pytest.mark.integration
def test_delete_cell_via_store_api_rejected_and_row_unchanged(
    migrated_database: str,
) -> None:
    """The store's ``delete_cell`` MUST raise before touching the DB;
    afterwards the DB cell row is byte-identical."""
    store = PsycopgGridRegistryStore(database_url=migrated_database)
    snapshot = _make_snapshot(
        canonical_grid_key=f"{RUN_PREFIX}_key_delcell",
        grid_id=f"{RUN_PREFIX}_grid_delcell",
        grid_signature=f"{RUN_PREFIX}-sig-delcell",
        grid_definition_uri=f"s3://nhms/canonical/{RUN_PREFIX}/delcell/grid.json",
        grid_definition_checksum="deadbeef",
    )
    snapshot_id = store.insert_snapshot(snapshot, _make_cells(3))

    with pytest.raises(RegistryImmutabilityError) as excinfo:
        store.delete_cell(snapshot_id, "1")
    assert excinfo.value.field_or_op == "delete_cell(1)"

    # DB rows unchanged.
    snap_count, cell_count = _count_rows(migrated_database, grid_snapshot_id=snapshot_id)
    assert snap_count == 1
    assert cell_count == 3


@pytest.mark.integration
@pytest.mark.parametrize(
    ("field", "new_value"),
    [
        ("grid_signature", "mutated-sig"),
        ("grid_definition_uri", "s3://nhms/canonical/mutated/grid.json"),
        ("grid_definition_checksum", "mutated-checksum"),
        ("bbox_south", 999.0),
        ("canonical_grid_key", "mutated-key"),
    ],
)
def test_update_identity_field_rejected_and_row_unchanged(
    migrated_database: str, field: str, new_value: Any
) -> None:
    """For each of the 5 non-cell identity fields the store's
    ``update_identity_field`` MUST raise before touching the DB. The DB row
    is byte-identical to the inserted value on read-back."""
    store = PsycopgGridRegistryStore(database_url=migrated_database)
    snapshot = _make_snapshot(
        canonical_grid_key=f"{RUN_PREFIX}_key_upd_{field}",
        grid_id=f"{RUN_PREFIX}_grid_upd_{field}",
        grid_signature=f"{RUN_PREFIX}-sig-upd-{field}",
        grid_definition_uri=f"s3://nhms/canonical/{RUN_PREFIX}/upd-{field}/grid.json",
        grid_definition_checksum=f"chksum-{field}",
    )
    snapshot_id = store.insert_snapshot(snapshot, _make_cells(2))
    row_before = _fetch_snapshot_row(migrated_database, snapshot_id)

    with pytest.raises(RegistryImmutabilityError) as excinfo:
        store.update_identity_field(snapshot_id, field, new_value)
    assert excinfo.value.field_or_op == field

    row_after = _fetch_snapshot_row(migrated_database, snapshot_id)
    assert row_before == row_after


@pytest.mark.integration
def test_mid_write_failure_rolls_back_all_rows(migrated_database: str) -> None:
    """Insert 5 cells where the 4th cell's ``grid_cell_id`` duplicates the
    2nd cell's -- violating the DB ``UNIQUE(grid_snapshot_id, grid_cell_id)``
    constraint mid-loop. Assert ``insert_snapshot`` raises
    ``RegistryStoreError`` and no rows remain in either table for the
    would-have-been-generated ``grid_snapshot_id``."""
    store = PsycopgGridRegistryStore(database_url=migrated_database)
    unique_key = f"{RUN_PREFIX}_key_midwrite"
    snapshot = _make_snapshot(
        canonical_grid_key=unique_key,
        grid_id=f"{RUN_PREFIX}_grid_midwrite",
        grid_signature=f"{RUN_PREFIX}-sig-midwrite",
        grid_definition_uri=f"s3://nhms/canonical/{RUN_PREFIX}/midwrite/grid.json",
    )
    # Ordinals are 1..5 contiguous (contiguity check passes); the duplicate
    # grid_cell_id fires from the DB during the 4th cell INSERT.
    cells = [
        CanonicalGridCell(grid_cell_id="0", longitude=63.0, latitude=8.0, canonical_ordinal=1),
        CanonicalGridCell(grid_cell_id="1", longitude=63.25, latitude=8.0, canonical_ordinal=2),
        CanonicalGridCell(grid_cell_id="2", longitude=63.5, latitude=8.0, canonical_ordinal=3),
        # This duplicate collides with cell #1 (grid_cell_id="1") — the DB
        # PK-plus-unique on (grid_snapshot_id, grid_cell_id) rejects this row.
        CanonicalGridCell(grid_cell_id="1", longitude=63.75, latitude=8.0, canonical_ordinal=4),
        CanonicalGridCell(grid_cell_id="4", longitude=64.0, latitude=8.0, canonical_ordinal=5),
    ]
    with pytest.raises(RegistryStoreError):
        store.insert_snapshot(snapshot, cells)

    # Verify no partial state: the unique canonical_grid_key remains
    # completely absent from both tables.
    connection = psycopg2.connect(migrated_database)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM met.canonical_grid_snapshot
                WHERE canonical_grid_key = %s
                """,
                (unique_key,),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM met.canonical_grid_cell c
                JOIN met.canonical_grid_snapshot s USING (grid_snapshot_id)
                WHERE s.canonical_grid_key = %s
                """,
                (unique_key,),
            )
            assert cursor.fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.integration
def test_supersede_permitted_write(migrated_database: str) -> None:
    """``supersede`` sets ``superseded_at`` on the snapshot. SUB-2 Trigger B
    permits this UPDATE, and the store exposes it as a primitive (idempotency
    is a SUB-9 concern, so a second call with a different ts overwrites)."""
    store = PsycopgGridRegistryStore(database_url=migrated_database)
    snapshot = _make_snapshot(
        canonical_grid_key=f"{RUN_PREFIX}_key_supersede",
        grid_id=f"{RUN_PREFIX}_grid_supersede",
        grid_signature=f"{RUN_PREFIX}-sig-supersede",
        grid_definition_uri=f"s3://nhms/canonical/{RUN_PREFIX}/supersede/grid.json",
        grid_definition_checksum="deadbeef",
    )
    snapshot_id = store.insert_snapshot(snapshot, _make_cells(2))

    first_ts = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    store.supersede(snapshot_id, first_ts)
    row = _fetch_snapshot_row(migrated_database, snapshot_id)
    assert row["superseded_at"] == first_ts

    # Second call with a later ts overwrites — no idempotency at store layer.
    later_ts = first_ts + timedelta(hours=1)
    store.supersede(snapshot_id, later_ts)
    row = _fetch_snapshot_row(migrated_database, snapshot_id)
    assert row["superseded_at"] == later_ts


@pytest.mark.integration
def test_extend_applicable_source_ids_preserves_order_and_dedupes(
    migrated_database: str,
) -> None:
    """``extend_applicable_source_ids`` extends the TEXT[] in-place: existing
    entries stay ordered, new source ids (post-normalize) are appended in
    the order supplied, duplicates are dropped, unknown source ids raise."""
    store = PsycopgGridRegistryStore(database_url=migrated_database)
    snapshot = _make_snapshot(
        canonical_grid_key=f"{RUN_PREFIX}_key_extend",
        grid_id=f"{RUN_PREFIX}_grid_extend",
        grid_signature=f"{RUN_PREFIX}-sig-extend",
        grid_definition_uri=f"s3://nhms/canonical/{RUN_PREFIX}/extend/grid.json",
        grid_definition_checksum="deadbeef",
        applicable_source_ids=("IFS",),
    )
    snapshot_id = store.insert_snapshot(snapshot, _make_cells(2))
    row = _fetch_snapshot_row(migrated_database, snapshot_id)
    assert row["applicable_source_ids"] == ["IFS"]

    # Extend with "gfs" — appended after IFS.
    store.extend_applicable_source_ids(snapshot_id, ["gfs"])
    row = _fetch_snapshot_row(migrated_database, snapshot_id)
    assert row["applicable_source_ids"] == ["IFS", "gfs"]

    # Extend again with ["gfs"] — no-op / idempotent (already present).
    store.extend_applicable_source_ids(snapshot_id, ["gfs"])
    row = _fetch_snapshot_row(migrated_database, snapshot_id)
    assert row["applicable_source_ids"] == ["IFS", "gfs"]

    # Mixed input: "gfs" (dup) + "era5" (new, normalizes to ERA5).
    store.extend_applicable_source_ids(snapshot_id, ["gfs", "era5"])
    row = _fetch_snapshot_row(migrated_database, snapshot_id)
    assert row["applicable_source_ids"] == ["IFS", "gfs", "ERA5"]

    # Unknown source id raises (via normalize_source_id) BEFORE writing.
    with pytest.raises(ValueError, match="Unknown source_id"):
        store.extend_applicable_source_ids(snapshot_id, ["unknown_source"])
    row = _fetch_snapshot_row(migrated_database, snapshot_id)
    assert row["applicable_source_ids"] == ["IFS", "gfs", "ERA5"]
