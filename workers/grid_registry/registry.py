"""Registry writer (SUB-5 / Task 3.1b).

The :func:`register_snapshot` function is the public API the Task 3.1b writer
exposes. It consumes a :class:`GridSnapshotInputRecord` produced by SUB-4's
:func:`read_input_record`, computes the ``grid_signature`` through SUB-1's
shared helper, derives the ``canonical_grid_key`` through SUB-4's
:func:`derive_canonical_grid_key`, re-runs the producer's live signature path
as a cross-implementation check, and writes one immutable snapshot plus its
ordered cells atomically through the SUB-3 :class:`PsycopgGridRegistryStore`.

Public error hierarchy (all inherit from :class:`RegistrationError`):

* :class:`RegistrationInvariantError` — defensive invariant violation caught
  before touching the DB (e.g. an un-normalized longitude on a record built
  via direct dataclass instantiation).
* :class:`LiveProducerSignatureMismatchError` — the registry-computed
  ``grid_signature`` disagrees with the producer's live
  ``_grid_points_from_definition`` recomputation on the same grid definition
  bytes (spec.md §"Live signature verification at registration").
* :class:`GridDriftDetectedError` — a snapshot with the same
  ``(source_id, grid_id)`` but a different ``grid_signature`` already exists,
  so this is a drift event that the SUB-9 supersession flow must own; SUB-5
  refuses to write.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from packages.common.canonical_grid_key import derive_canonical_grid_key
from packages.common.grid_registry_store import (
    CanonicalGridCell,
    CanonicalGridSnapshot,
    PsycopgGridRegistryStore,
    RegistryUniqueViolationError,
)
from packages.common.grid_signature import grid_signature_hash
from packages.common.source_identity import normalize_source_id
from workers.grid_registry.input_record import GridSnapshotInputRecord

_LONGITUDE_CONVENTION = "[-180, 180)"


class RegistrationError(RuntimeError):
    """Base class for all SUB-5 registration failures."""


class RegistrationInvariantError(RegistrationError):
    """Raised when a defensive writer-side invariant is violated.

    Currently emitted when the input record's cells contain a longitude that
    lies outside ``[-180.0, 180.0)`` — i.e. SUB-4's ``_build_cells`` did not
    normalize the value. Includes the offending cell index and value so a
    future SUB-4 refactor is diagnosable at test time.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class LiveProducerSignatureMismatchError(RegistrationError):
    """Registry-computed signature disagrees with the live producer recompute.

    Carries both hashes on the exception attributes so callers logging the
    error surface both paths for triage.
    """

    def __init__(
        self,
        *,
        registry_computed: str,
        live_producer_computed: str | None,
        reason: str,
    ) -> None:
        self.registry_computed = registry_computed
        self.live_producer_computed = live_producer_computed
        self.reason = reason
        super().__init__(
            f"grid_signature mismatch between registry and live-producer paths: "
            f"registry_computed={registry_computed!r}, "
            f"live_producer_computed={live_producer_computed!r}. {reason}"
        )


class GridDriftDetectedError(RegistrationError):
    """A row with the same ``(source_id, grid_id)`` but a different signature already exists.

    SUB-5 refuses to write in this case; the operator must invoke the SUB-9
    supersession flow to record the drift explicitly. Both signatures are
    carried on the exception attributes for triage.
    """

    def __init__(
        self,
        *,
        source_id: str,
        grid_id: str,
        registry_computed_signature: str,
        existing_signature: str,
        existing_snapshot_id: UUID,
    ) -> None:
        self.source_id = source_id
        self.grid_id = grid_id
        self.registry_computed_signature = registry_computed_signature
        self.existing_signature = existing_signature
        self.existing_snapshot_id = existing_snapshot_id
        super().__init__(
            f"grid drift detected for (source_id={source_id!r}, grid_id={grid_id!r}): "
            f"registry-computed signature {registry_computed_signature!r} disagrees "
            f"with existing snapshot {existing_snapshot_id} signature "
            f"{existing_signature!r}. Register a new snapshot version via the "
            "SUB-9 supersession flow rather than re-registering here."
        )


def _assert_normalized_longitudes(record: GridSnapshotInputRecord) -> None:
    """Defensive check: every cell longitude MUST fall in ``[-180.0, 180.0)``.

    Reuses ``workers.forcing_producer.producer._normalize_longitude`` as the
    reference predicate so a future producer refactor that changes the
    convention forces the writer to be updated in the same commit.
    """
    from workers.forcing_producer.producer import _normalize_longitude

    for index, cell in enumerate(record.cells):
        expected = _normalize_longitude(cell.longitude)
        if cell.longitude != expected or not (-180.0 <= cell.longitude < 180.0):
            raise RegistrationInvariantError(
                f"cell {index} (grid_cell_id={cell.grid_cell_id!r}) has "
                f"un-normalized longitude {cell.longitude!r}; the "
                f"[-180.0, 180.0) convention requires {expected!r}."
            )


def _live_producer_signature(
    record: GridSnapshotInputRecord,
    *,
    registry_computed: str,
) -> str:
    """Recompute ``grid_signature`` through the producer's live path.

    Constructs a :class:`SimpleNamespace` shim whose ``object_store.read_bytes``
    returns ``record.raw_grid_json_bytes`` — the exact bytes SUB-4 read from
    the local ``grid.json`` path and pinned via ``grid_definition_checksum``.
    Reusing the checksum-verified bytes closes the TOCTOU window a second
    open would introduce AND guarantees the live path re-parses the raw JSON
    rather than a re-serialization derived from the same SUB-4-parsed axis
    tuples (which would let a SUB-4 parsing bug propagate identically to
    both hash paths).

    A synthetic :class:`CanonicalProduct` is passed to the unbound method
    call; only ``grid_definition_uri`` and ``canonical_product_id`` matter
    for the ``layout=="rectilinear"`` branch. On a ``None`` return from
    the producer helper, the mismatch error carries the caller's
    ``registry_computed`` so operators triaging the log see BOTH paths.
    """
    from datetime import UTC, datetime

    from workers.forcing_producer.producer import (
        CanonicalProduct,
        ForcingProducer,
    )

    def _read_bytes(uri: str) -> bytes:
        if uri != record.grid_definition_uri:
            raise RuntimeError(
                f"live-producer shim: unexpected read {uri!r}; expected "
                f"{record.grid_definition_uri!r}."
            )
        return record.raw_grid_json_bytes

    shim = SimpleNamespace(object_store=SimpleNamespace(read_bytes=_read_bytes))
    synthetic_product = CanonicalProduct(
        canonical_product_id=f"registry-recompute:{record.grid_id}",
        source_id="registry-recompute",
        cycle_time=datetime(2026, 1, 1, tzinfo=UTC),
        valid_time=datetime(2026, 1, 1, tzinfo=UTC),
        variable="Prcp",
        unit="mm/day",
        grid_id=record.grid_id,
        object_uri="unused://registry-recompute",
        checksum="0" * 64,
        grid_definition_uri=record.grid_definition_uri,
    )
    points = ForcingProducer._grid_points_from_definition(
        shim, synthetic_product, expected_count=len(record.cells)
    )
    if points is None:
        raise LiveProducerSignatureMismatchError(
            registry_computed=registry_computed,
            live_producer_computed=None,
            reason=(
                "producer._grid_points_from_definition returned None; the "
                "rectilinear branch could not reconstruct the grid from the "
                "record's checksum-verified bytes."
            ),
        )
    return grid_signature_hash(points)


def _build_snapshot(
    *,
    record: GridSnapshotInputRecord,
    normalized_source: str,
    grid_signature: str,
    canonical_grid_key: str,
) -> tuple[CanonicalGridSnapshot, list[CanonicalGridCell]]:
    snapshot = CanonicalGridSnapshot(
        grid_snapshot_id=None,
        canonical_grid_key=canonical_grid_key,
        source_id=normalized_source,
        grid_id=record.grid_id,
        grid_signature=grid_signature,
        grid_definition_uri=record.grid_definition_uri,
        grid_definition_checksum=record.grid_definition_checksum,
        longitude_convention=_LONGITUDE_CONVENTION,
        latitude_order=record.latitude_order,
        flatten_order=record.flatten_order,
        native_resolution=record.native_resolution,
        bbox_south=float(record.download_bbox["south"]),
        bbox_north=float(record.download_bbox["north"]),
        bbox_west=float(record.download_bbox["west"]),
        bbox_east=float(record.download_bbox["east"]),
        converter_version=record.converter_version,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
        applicable_source_ids=(normalized_source,),
    )
    cells = [
        CanonicalGridCell(
            grid_cell_id=cell.grid_cell_id,
            longitude=cell.longitude,
            latitude=cell.latitude,
            canonical_ordinal=cell.canonical_ordinal,
        )
        for cell in record.cells
    ]
    return snapshot, cells


def register_snapshot(
    record: GridSnapshotInputRecord,
    *,
    source_id: str,
    store: PsycopgGridRegistryStore,
) -> UUID:
    """Register one immutable snapshot + ordered cells atomically.

    Returns the inserted ``grid_snapshot_id``. If a snapshot with the same
    normalized ``source_id``, ``grid_id``, and ``grid_signature`` already
    exists, returns that existing UUID (idempotent). If a snapshot with the
    same ``(source_id, grid_id)`` but a different ``grid_signature`` exists,
    raises :class:`GridDriftDetectedError` — SUB-5 does not own supersession.
    """
    _assert_normalized_longitudes(record)

    normalized_source = normalize_source_id(source_id)
    registry_computed = grid_signature_hash(record.cells)

    live_producer_computed = _live_producer_signature(
        record, registry_computed=registry_computed
    )
    if registry_computed != live_producer_computed:
        raise LiveProducerSignatureMismatchError(
            registry_computed=registry_computed,
            live_producer_computed=live_producer_computed,
            reason=(
                "registry hash from record.cells disagrees with producer's "
                "live _grid_points_from_definition recompute on the same "
                "grid definition bytes."
            ),
        )

    existing_id = store.find_snapshot_by_identity(
        normalized_source, record.grid_id, registry_computed
    )
    if existing_id is not None:
        return existing_id

    drift_error = _check_drift(store, normalized_source, record.grid_id, registry_computed)
    if drift_error is not None:
        raise drift_error

    canonical_grid_key = derive_canonical_grid_key(
        registry_computed, dict(record.download_bbox), record.native_resolution
    )

    snapshot, cells = _build_snapshot(
        record=record,
        normalized_source=normalized_source,
        grid_signature=registry_computed,
        canonical_grid_key=canonical_grid_key,
    )
    try:
        return store.insert_snapshot(snapshot, cells)
    except RegistryUniqueViolationError as unique_error:
        # Concurrency backstop: the DB-level partial UNIQUE index on
        # (source_id, grid_id, grid_signature) WHERE superseded_at IS NULL
        # (000044) rejects racing duplicates the check-then-insert cannot
        # see. Scope the fallback to SQLSTATE 23505 only so a transient
        # network/connection failure surfaces as a real error rather than a
        # race-lost misinterpretation.
        winner_id = store.find_snapshot_by_identity(
            normalized_source, record.grid_id, registry_computed
        )
        if winner_id is not None:
            return winner_id
        # Race winner rolled back after the unique violation fired but before
        # our re-query. Re-raise the original unique-violation with the note
        # so callers see the true cause.
        unique_error.add_note(
            "race-fallback re-query returned no winner; the racing transaction "
            "likely rolled back after the unique constraint fired."
        )
        raise


def _check_drift(
    store: PsycopgGridRegistryStore,
    normalized_source: str,
    grid_id: str,
    registry_computed: str,
) -> GridDriftDetectedError | None:
    """Return a populated :class:`GridDriftDetectedError` if drift is detected.

    Delegates to :meth:`PsycopgGridRegistryStore.find_conflicting_snapshot_by_source_grid`
    so DB / connection errors surface as ``RegistryStoreError`` through the
    store boundary (not raw ``psycopg2`` exceptions leaking through the
    writer to the CLI).
    """
    conflict = store.find_conflicting_snapshot_by_source_grid(
        normalized_source, grid_id, registry_computed
    )
    if conflict is None:
        return None
    existing_id, existing_signature = conflict
    return GridDriftDetectedError(
        source_id=normalized_source,
        grid_id=grid_id,
        registry_computed_signature=registry_computed,
        existing_signature=existing_signature,
        existing_snapshot_id=existing_id,
    )


# Explicit __all__ so `from workers.grid_registry.registry import *` stays crisp.
__all__: tuple[str, ...] = (
    "GridDriftDetectedError",
    "LiveProducerSignatureMismatchError",
    "RegistrationError",
    "RegistrationInvariantError",
    "register_snapshot",
)


