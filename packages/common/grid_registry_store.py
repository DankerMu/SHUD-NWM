"""Registry store for the canonical Grid Snapshot registry (SUB-3 / issue #900).

This module owns the write-side interface for `met.canonical_grid_snapshot` +
`met.canonical_grid_cell` (introduced by SUB-2 migration
``db/migrations/000043_canonical_grid_snapshot.sql``). The store is deliberately
minimal: it exposes only the primitives required by the append-only
registration lifecycle described in
``openspec/changes/canonical-source-grid-registry/design.md`` §4 and by
``openspec/changes/canonical-source-grid-registry/specs/grid-snapshot-registration/spec.md``:

* ``insert_snapshot(snapshot, cells)`` — atomic INSERT of one snapshot plus its
  ordered cells (rolled back as a unit on mid-write failure).
* ``load_snapshot(id, *, object_reader)`` — checksum-verified load; fetches the
  bytes at the snapshot's ``grid_definition_uri`` via an injectable
  ``object_reader`` (e.g. ``LocalObjectStore.read_bytes``) and recomputes the
  SHA-256 against the stored ``grid_definition_checksum``.
* ``supersede(id, ts)`` — permitted post-insert write A (SUB-9 lifecycle).
* ``extend_applicable_source_ids(id, source_ids)`` — permitted post-insert
  write B (SUB-8 shared-eligibility acceptance).
* ``delete_snapshot`` / ``delete_cell`` / ``update_identity_field`` — always
  raise ``RegistryImmutabilityError`` per spec.md:92-109 (registration is
  append-only and immutable).

The design intentionally does NOT expose lifecycle decision logic:

* Idempotency of ``supersede`` is a SUB-9 concern.
* Deciding whether ``extend_applicable_source_ids`` is permitted is a SUB-8
  concern (this module only exposes the primitive).

Errors: the store surfaces its own rejections through the
``RegistryStoreError`` hierarchy. One boundary error class is intentionally
NOT wrapped: ``ValueError`` raised by ``normalize_source_id`` (from
``packages.common.source_identity``) propagates raw, matching the
established sibling convention in ``packages.common.met_store``
(``met_store.py:63`` calls ``normalize_source_id`` and lets ``ValueError``
propagate). Callers who want a single ``except`` surface must catch both
``RegistryStoreError`` and ``ValueError``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.common.object_store import sha256_bytes
from packages.common.source_identity import normalize_source_id

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


class RegistryStoreError(RuntimeError):
    """Base error raised by the grid-registry store on any store-level failure.

    Subclasses carry structured attributes for the specific rejection mode.
    """


class RegistryImmutabilityError(RegistryStoreError):
    """Raised when a caller attempts to mutate an existing snapshot's identity.

    Registration is append-only (spec.md scenario "Snapshots are never updated
    in place"); the store rejects any per-cell DELETE, per-snapshot DELETE, or
    identity-field UPDATE on an already-registered ``grid_snapshot_id``.
    """

    def __init__(self, *, grid_snapshot_id: UUID | str, field_or_op: str) -> None:
        self.grid_snapshot_id = grid_snapshot_id
        self.field_or_op = field_or_op
        super().__init__(
            f"canonical_grid_snapshot {grid_snapshot_id} identity field/op "
            f"{field_or_op!r} is immutable; register a new snapshot version to "
            "change identity."
        )


class RegistryContiguityError(RegistryStoreError):
    """Raised when the supplied cells' ``canonical_ordinal`` set is not the
    contiguous integer sequence ``1..N``.

    The DB ``UNIQUE(grid_snapshot_id, canonical_ordinal)`` constraint alone
    cannot enforce contiguity (spec.md:41-45); the store validates it before
    opening the transaction and raises with the offending value named in
    ``.gap``. Priority for ``.gap`` selection:

    * If duplicates are present, ``.gap`` names the SMALLEST duplicated
      ordinal (the true bug when duplicates exist).
    * Otherwise ``.gap`` names the SMALLEST offending value across BOTH
      missing integers in ``1..N`` AND out-of-range values (below 1 or
      above N).
    """

    def __init__(self, *, expected: list[int], actual: list[int], gap: int) -> None:
        self.expected = expected
        self.actual = actual
        self.gap = gap
        super().__init__(
            f"canonical_ordinal contiguity violation: expected {expected}, "
            f"got {actual}; smallest offending ordinal (duplicate, missing, "
            f"or out-of-range) is {gap}."
        )


class RegistryChecksumError(RegistryStoreError):
    """Raised when a checksum-verified load detects content drift.

    The stored ``grid_definition_checksum`` disagrees with the SHA-256 of the
    bytes returned by the injected ``object_reader`` for the snapshot's
    ``grid_definition_uri``.
    """

    def __init__(self, *, expected_hash: str, actual_hash: str, uri: str) -> None:
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        self.uri = uri
        super().__init__(
            f"grid_definition checksum mismatch at {uri!r}: "
            f"expected {expected_hash!r}, computed {actual_hash!r}"
        )


# Identity field names (spec.md:96) mirrored to keep messages informative when
# the store rejects update_identity_field on any of these.
IDENTITY_FIELDS: frozenset[str] = frozenset(
    {
        "grid_signature",
        "grid_definition_uri",
        "grid_definition_checksum",
        "canonical_grid_key",
        "bbox_south",
        "bbox_north",
        "bbox_west",
        "bbox_east",
        "native_resolution",
    }
)


def default_database_url() -> str:
    """Return ``DATABASE_URL`` from the environment or raise ``RegistryStoreError``."""
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RegistryStoreError(
            "DATABASE_URL is required for grid-registry database operations."
        )
    return database_url


def _require_tz_aware(value: datetime | None, field_name: str) -> datetime | None:
    """Reject naive ``datetime`` values so writes bind unambiguous UTC.

    Postgres ``TIMESTAMPTZ`` interprets a naive datetime bind in the session
    timezone, which is not guaranteed to be UTC. Rejecting naive values at
    the write boundary matches the strictness expected of registry lifecycle
    timestamps (spec.md scenario "Timestamps are wall-clock UTC").
    Sibling coercion patterns (`state_manager._ensure_utc`,
    `object_store_forcing._ensure_utc`) accept naive by treating it as UTC;
    the registry chooses the stricter reject-on-naive stance for symmetry
    with the append-only immutability guarantees.
    """
    if value is not None and value.tzinfo is None:
        raise RegistryStoreError(
            f"{field_name} must be tz-aware datetime; got naive value {value!r}."
        )
    return value


def _normalize_checksum(checksum: str) -> str:
    """Validate a SHA-256 hex checksum and return its lowercase form.

    ``sha256_bytes`` emits lowercase hex; snapshot checksums MUST be a valid
    64-character hex string so equality comparisons on ``load_snapshot`` are
    case-insensitive by construction. Any legacy tool that wrote uppercase
    hex would still round-trip cleanly because the stored value is always
    lowercased on write.
    """
    if not isinstance(checksum, str) or _HEX64.fullmatch(checksum) is None:
        raise RegistryStoreError(
            f"grid_definition_checksum must be a 64-character hex string; "
            f"got {checksum!r}."
        )
    return checksum.lower()


@dataclass(frozen=True)
class CanonicalGridSnapshot:
    """Row-shaped record for ``met.canonical_grid_snapshot``.

    ``grid_snapshot_id`` may be ``None`` on insert to let the DB
    (``gen_random_uuid()``) generate the UUID; the store returns the generated
    id from ``insert_snapshot``. Every other field is required by the DB schema
    (spec.md scenario "Snapshot records required identity fields").
    """

    grid_snapshot_id: UUID | None
    canonical_grid_key: str
    source_id: str
    grid_id: str
    grid_signature: str
    grid_definition_uri: str
    grid_definition_checksum: str
    longitude_convention: str
    latitude_order: str
    flatten_order: str
    native_resolution: float
    bbox_south: float
    bbox_north: float
    bbox_west: float
    bbox_east: float
    converter_version: str
    valid_from: datetime
    valid_to: datetime | None
    applicable_source_ids: tuple[str, ...]
    superseded_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class CanonicalGridCell:
    """Row-shaped record for ``met.canonical_grid_cell``.

    ``grid_cell_id`` is the producer-side string id (``str(index)`` for the
    0-based iteration). ``canonical_ordinal`` is the registry-side 1..N
    counterpart (contiguous, unique within the snapshot).
    """

    grid_cell_id: str
    longitude: float
    latitude: float
    canonical_ordinal: int


def _validate_ordinal_contiguity(cells: Sequence[CanonicalGridCell]) -> None:
    """Raise ``RegistryContiguityError`` if the ordinals are not ``1..N``.

    The gap reported is the smallest offending value across BOTH the missing
    integers in ``1..N`` AND the out-of-range values (below 1 or above N).
    Duplicates are detected first and the smallest duplicated ordinal is
    reported in ``.gap`` — this names the true bug rather than the downstream
    missing ordinal a duplicate would otherwise imply.
    """
    if not cells:
        raise RegistryStoreError(
            "snapshot must have at least one cell row (got cells=[])"
        )
    n = len(cells)
    expected = list(range(1, n + 1))
    actual = sorted(int(cell.canonical_ordinal) for cell in cells)
    if actual == expected:
        return
    # Duplicates are the true bug when present; naming the "missing" ordinal
    # would misdirect the caller. Detect first, before comparing to expected.
    if len(set(actual)) != len(actual):
        seen: set[int] = set()
        duplicates: set[int] = set()
        for value in actual:
            if value in seen:
                duplicates.add(value)
            else:
                seen.add(value)
        raise RegistryContiguityError(
            expected=expected, actual=actual, gap=min(duplicates)
        )
    expected_set = set(expected)
    # Report the smallest offending value across BOTH sets (missing +
    # out-of-range) so `[1, 2, 4]` (n=3) names the missing 3, not the
    # out-of-range 4.
    missing = expected_set - set(actual)
    out_of_range = {value for value in actual if value < 1 or value > n}
    offending = missing | out_of_range
    gap = min(offending)
    raise RegistryContiguityError(expected=expected, actual=actual, gap=gap)


@dataclass(frozen=True)
class PsycopgGridRegistryStore:
    """psycopg2-backed store for the canonical Grid Snapshot registry."""

    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgGridRegistryStore:
        return cls(default_database_url())

    def insert_snapshot(
        self,
        snapshot: CanonicalGridSnapshot,
        cells: Sequence[CanonicalGridCell],
    ) -> UUID:
        """Insert one snapshot and its ordered cells atomically.

        Write-boundary validations, in order (all before any DB touch):

        * ``superseded_at`` must be ``None`` on insert (use ``supersede()``
          post-registration to mark supersession — a born-superseded row
          would be silently filtered by SUB-9 lifecycle queries).
        * ``valid_from`` and ``valid_to`` must be tz-aware ``datetime``
          values so Postgres ``TIMESTAMPTZ`` binds unambiguous UTC.
        * ``grid_definition_checksum`` must be a valid 64-character hex
          string; it is normalized to lowercase before writing.
        * The ordered cells' ``canonical_ordinal`` set must be the
          contiguous integer sequence ``1..N`` with no duplicates.

        The snapshot's ``source_id`` and each ``applicable_source_ids``
        entry are normalized via ``normalize_source_id`` before writing;
        ``ValueError`` from that helper propagates raw (matching the
        established sibling convention in ``met_store``).

        On any mid-write DB error the entire transaction rolls back; zero
        rows remain for the (possibly auto-generated) ``grid_snapshot_id``.

        Returns the ``grid_snapshot_id`` (fetched back from the DB if the
        caller passed ``None`` and the DB generated one).
        """
        if snapshot.superseded_at is not None:
            raise RegistryStoreError(
                "superseded_at must be None on insert; use supersede() "
                "post-registration for supersession."
            )
        _require_tz_aware(snapshot.valid_from, "valid_from")
        _require_tz_aware(snapshot.valid_to, "valid_to")
        # Defense-in-depth: if a future refactor stops rejecting non-None
        # superseded_at on insert, this line still ensures the timestamp is
        # tz-aware.
        _require_tz_aware(snapshot.superseded_at, "superseded_at")
        normalized_checksum = _normalize_checksum(snapshot.grid_definition_checksum)
        _validate_ordinal_contiguity(cells)
        normalized_source = normalize_source_id(snapshot.source_id)
        normalized_applicable = tuple(
            normalize_source_id(item) for item in snapshot.applicable_source_ids
        )

        try:
            import psycopg2
        except ImportError as error:
            raise RegistryStoreError(
                "psycopg2 is required for grid-registry database operations."
            ) from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                if snapshot.grid_snapshot_id is None:
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
                            applicable_source_ids,
                            superseded_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        RETURNING grid_snapshot_id
                        """,
                        (
                            snapshot.canonical_grid_key,
                            normalized_source,
                            snapshot.grid_id,
                            snapshot.grid_signature,
                            snapshot.grid_definition_uri,
                            normalized_checksum,
                            snapshot.longitude_convention,
                            snapshot.latitude_order,
                            snapshot.flatten_order,
                            snapshot.native_resolution,
                            snapshot.bbox_south,
                            snapshot.bbox_north,
                            snapshot.bbox_west,
                            snapshot.bbox_east,
                            snapshot.converter_version,
                            snapshot.valid_from,
                            snapshot.valid_to,
                            list(normalized_applicable),
                            snapshot.superseded_at,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO met.canonical_grid_snapshot (
                            grid_snapshot_id,
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
                            applicable_source_ids,
                            superseded_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        RETURNING grid_snapshot_id
                        """,
                        (
                            str(snapshot.grid_snapshot_id),
                            snapshot.canonical_grid_key,
                            normalized_source,
                            snapshot.grid_id,
                            snapshot.grid_signature,
                            snapshot.grid_definition_uri,
                            normalized_checksum,
                            snapshot.longitude_convention,
                            snapshot.latitude_order,
                            snapshot.flatten_order,
                            snapshot.native_resolution,
                            snapshot.bbox_south,
                            snapshot.bbox_north,
                            snapshot.bbox_west,
                            snapshot.bbox_east,
                            snapshot.converter_version,
                            snapshot.valid_from,
                            snapshot.valid_to,
                            list(normalized_applicable),
                            snapshot.superseded_at,
                        ),
                    )
                returned = cursor.fetchone()
                if returned is None:
                    raise RegistryStoreError(
                        "canonical_grid_snapshot INSERT did not return the "
                        "grid_snapshot_id."
                    )
                grid_snapshot_id = UUID(str(returned[0]))
                # Per-row cell inserts keep the mid-write failure semantics
                # crisp: any error inside this loop rolls the whole tx back.
                for cell in cells:
                    cursor.execute(
                        """
                        INSERT INTO met.canonical_grid_cell (
                            grid_snapshot_id,
                            grid_cell_id,
                            longitude,
                            latitude,
                            canonical_ordinal
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            str(grid_snapshot_id),
                            cell.grid_cell_id,
                            cell.longitude,
                            cell.latitude,
                            cell.canonical_ordinal,
                        ),
                    )
            connection.commit()
            return grid_snapshot_id
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise RegistryStoreError(
                f"canonical_grid_snapshot INSERT failed: {error}"
            ) from error
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def load_snapshot(
        self,
        grid_snapshot_id: UUID,
        *,
        object_reader: Callable[[str], bytes],
    ) -> tuple[CanonicalGridSnapshot, list[CanonicalGridCell]]:
        """Fetch a snapshot + cells, verifying the ``grid_definition`` checksum.

        The caller supplies an ``object_reader`` (e.g.
        ``LocalObjectStore.read_bytes``) so tests can simulate content drift
        without touching a real object store. On checksum mismatch the store
        raises ``RegistryChecksumError`` BEFORE returning the loaded snapshot
        (spec.md scenario "grid_definition_uri is checksum-bound").
        """
        try:
            import psycopg2
        except ImportError as error:
            raise RegistryStoreError(
                "psycopg2 is required for grid-registry database operations."
            ) from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        grid_snapshot_id,
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
                        applicable_source_ids,
                        superseded_at,
                        created_at
                    FROM met.canonical_grid_snapshot
                    WHERE grid_snapshot_id = %s
                    """,
                    (str(grid_snapshot_id),),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RegistryStoreError(
                        f"grid_snapshot_id {grid_snapshot_id} not found."
                    )
                snapshot = CanonicalGridSnapshot(
                    grid_snapshot_id=UUID(str(row[0])),
                    canonical_grid_key=row[1],
                    source_id=row[2],
                    grid_id=row[3],
                    grid_signature=row[4],
                    grid_definition_uri=row[5],
                    grid_definition_checksum=row[6],
                    longitude_convention=row[7],
                    latitude_order=row[8],
                    flatten_order=row[9],
                    native_resolution=float(row[10]),
                    bbox_south=float(row[11]),
                    bbox_north=float(row[12]),
                    bbox_west=float(row[13]),
                    bbox_east=float(row[14]),
                    converter_version=row[15],
                    valid_from=row[16],
                    valid_to=row[17],
                    applicable_source_ids=tuple(row[18] or ()),
                    superseded_at=row[19],
                    created_at=row[20],
                )
                cursor.execute(
                    """
                    SELECT grid_cell_id, longitude, latitude, canonical_ordinal
                    FROM met.canonical_grid_cell
                    WHERE grid_snapshot_id = %s
                    ORDER BY canonical_ordinal ASC
                    """,
                    (str(grid_snapshot_id),),
                )
                cell_rows = cursor.fetchall()
                cells = [
                    CanonicalGridCell(
                        grid_cell_id=r[0],
                        longitude=float(r[1]),
                        latitude=float(r[2]),
                        canonical_ordinal=int(r[3]),
                    )
                    for r in cell_rows
                ]
            connection.commit()
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise RegistryStoreError(
                f"canonical_grid_snapshot load failed: {error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

        # Checksum-verify AFTER we have the snapshot row. If the object bytes
        # drift, we fail closed before handing anything back to the caller.
        # Case-insensitive compare (`sha256_bytes` returns lowercase; the
        # store lowercases on write via `_normalize_checksum`, but a legacy
        # tool may have written uppercase hex directly — we still accept it).
        content = object_reader(snapshot.grid_definition_uri)
        actual_hash = sha256_bytes(content)
        if actual_hash.lower() != snapshot.grid_definition_checksum.lower():
            raise RegistryChecksumError(
                expected_hash=snapshot.grid_definition_checksum,
                actual_hash=actual_hash,
                uri=snapshot.grid_definition_uri,
            )
        return snapshot, cells

    def supersede(self, grid_snapshot_id: UUID, superseded_at: datetime) -> None:
        """Set the snapshot's ``superseded_at`` timestamp (permitted write A).

        SUB-9 owns idempotency (whether a re-supersede is a no-op or an error);
        the store only exposes the primitive. SUB-2 migration Trigger B
        explicitly allows ``superseded_at`` UPDATEs.

        ``superseded_at`` MUST be a tz-aware, non-null ``datetime``:

        * ``None`` is rejected — un-supersession is not a permitted lifecycle
          event (spec treats ``superseded_at`` as an append-only lifecycle
          marker; the row can be RE-superseded to a different ts, but not
          cleared to NULL through this API).
        * Naive datetimes are rejected — Postgres ``TIMESTAMPTZ`` binds a
          naive value in the session TZ, which is not guaranteed to be UTC.
        """
        if superseded_at is None:
            raise RegistryStoreError(
                "supersede requires a non-null timestamp; un-supersession is "
                "not a permitted lifecycle event."
            )
        _require_tz_aware(superseded_at, "superseded_at")
        try:
            import psycopg2
        except ImportError as error:
            raise RegistryStoreError(
                "psycopg2 is required for grid-registry database operations."
            ) from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE met.canonical_grid_snapshot
                    SET superseded_at = %s
                    WHERE grid_snapshot_id = %s
                    """,
                    (superseded_at, str(grid_snapshot_id)),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    raise RegistryStoreError(
                        f"grid_snapshot_id {grid_snapshot_id} not found."
                    )
            connection.commit()
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise RegistryStoreError(
                f"supersede failed for {grid_snapshot_id}: {error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    def extend_applicable_source_ids(
        self,
        grid_snapshot_id: UUID,
        source_ids: Sequence[str],
    ) -> None:
        """Extend ``applicable_source_ids`` with the given source ids.

        Order is preserved: existing entries stay in their positions; new
        source ids (after ``normalize_source_id``) are appended in the order
        supplied. Duplicates are skipped (calling twice with the same list is
        a no-op). SUB-8 owns the eligibility DECISION; the store only exposes
        the primitive.
        """
        normalized_new = [normalize_source_id(item) for item in source_ids]
        try:
            import psycopg2
        except ImportError as error:
            raise RegistryStoreError(
                "psycopg2 is required for grid-registry database operations."
            ) from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT applicable_source_ids
                    FROM met.canonical_grid_snapshot
                    WHERE grid_snapshot_id = %s
                    FOR UPDATE
                    """,
                    (str(grid_snapshot_id),),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.rollback()
                    raise RegistryStoreError(
                        f"grid_snapshot_id {grid_snapshot_id} not found."
                    )
                current: list[str] = list(row[0] or ())
                merged: list[str] = list(current)
                for candidate in normalized_new:
                    if candidate not in merged:
                        merged.append(candidate)
                if merged == current:
                    connection.rollback()
                    return
                cursor.execute(
                    """
                    UPDATE met.canonical_grid_snapshot
                    SET applicable_source_ids = %s
                    WHERE grid_snapshot_id = %s
                    """,
                    (merged, str(grid_snapshot_id)),
                )
            connection.commit()
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise RegistryStoreError(
                f"extend_applicable_source_ids failed for {grid_snapshot_id}: "
                f"{error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    def delete_snapshot(self, grid_snapshot_id: UUID) -> None:
        """Always raises ``RegistryImmutabilityError``.

        Snapshots are append-only per spec.md:92-109; SUB-2 migration
        ``000043_canonical_grid_snapshot.sql:24-27`` explicitly delegates
        snapshot-DELETE rejection to this store method. The rejection surface
        exists so tests can prove the store's API-level guarantee.
        """
        raise RegistryImmutabilityError(
            grid_snapshot_id=grid_snapshot_id, field_or_op="delete_snapshot"
        )

    def delete_cell(self, grid_snapshot_id: UUID, grid_cell_id: str) -> None:
        """Always raises ``RegistryImmutabilityError``.

        Cell rows are immutable per SUB-2 Trigger C (UPDATE) / Trigger D
        (DELETE); the store surfaces the rejection at the API layer so callers
        never craft a direct DELETE that the DB would otherwise reject.
        """
        raise RegistryImmutabilityError(
            grid_snapshot_id=grid_snapshot_id,
            field_or_op=f"delete_cell({grid_cell_id})",
        )

    def update_identity_field(
        self,
        grid_snapshot_id: UUID,
        field_name: str,
        new_value: Any,
    ) -> None:
        """Always raises ``RegistryImmutabilityError``.

        The six identity fields enumerated by
        ``openspec/changes/canonical-source-grid-registry/specs/grid-snapshot-registration/spec.md:96``
        (``grid_signature``, ``grid_definition_uri``,
        ``grid_definition_checksum``, ``bbox`` (any of the four corners),
        ``canonical_grid_key``, and per-cell rows via ``delete_cell``) MUST
        NOT be mutated on an existing snapshot; the caller must register a
        new snapshot version instead. ``new_value`` is accepted for API
        symmetry but never applied.
        """
        del new_value  # intentional: rejection is unconditional
        raise RegistryImmutabilityError(
            grid_snapshot_id=grid_snapshot_id, field_or_op=field_name
        )

    def find_snapshot_by_identity(
        self,
        source_id: str,
        grid_id: str,
        grid_signature: str,
    ) -> UUID | None:
        """Return the ``grid_snapshot_id`` matching the identity triple, or ``None``.

        Used by the SUB-5 writer (:mod:`workers.grid_registry.registry`) for
        idempotent re-registration on backfill re-runs. The ``source_id`` is
        normalized before comparison so callers may pass raw case forms
        (``"ifs"`` / ``"GFS"``) and still match the stored row. Superseded
        rows (``superseded_at IS NOT NULL``) are intentionally excluded so a
        post-supersede re-register writes a NEW row rather than falsely
        satisfying idempotency against the historical superseded row.
        """
        normalized_source = normalize_source_id(source_id)
        try:
            import psycopg2
        except ImportError as error:
            raise RegistryStoreError(
                "psycopg2 is required for grid-registry database operations."
            ) from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT grid_snapshot_id
                    FROM met.canonical_grid_snapshot
                    WHERE source_id = %s
                      AND grid_id = %s
                      AND grid_signature = %s
                      AND superseded_at IS NULL
                    """,
                    (normalized_source, grid_id, grid_signature),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return UUID(str(row[0]))
        except psycopg2.Error as error:
            raise RegistryStoreError(
                f"find_snapshot_by_identity failed: {error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    def find_conflicting_snapshot_by_source_grid(
        self,
        source_id: str,
        grid_id: str,
        excluded_signature: str,
    ) -> tuple[UUID, str] | None:
        """Return ``(grid_snapshot_id, grid_signature)`` of a drift-conflicting row.

        Used by the SUB-5 writer (:mod:`workers.grid_registry.registry`) to
        detect grid drift: a row with the same ``(source_id, grid_id)`` but a
        different ``grid_signature`` than the caller's registry-computed one.
        Returns the oldest such row (``ORDER BY created_at ASC LIMIT 1``) so
        error messages name the original snapshot the operator must supersede.
        Superseded rows are excluded so the drift check does not surface
        already-resolved history as a fresh drift.
        """
        normalized_source = normalize_source_id(source_id)
        try:
            import psycopg2
        except ImportError as error:
            raise RegistryStoreError(
                "psycopg2 is required for grid-registry database operations."
            ) from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT grid_snapshot_id, grid_signature
                    FROM met.canonical_grid_snapshot
                    WHERE source_id = %s
                      AND grid_id = %s
                      AND grid_signature <> %s
                      AND superseded_at IS NULL
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (normalized_source, grid_id, excluded_signature),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return (UUID(str(row[0])), str(row[1]))
        except psycopg2.Error as error:
            raise RegistryStoreError(
                f"find_conflicting_snapshot_by_source_grid failed: {error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()
