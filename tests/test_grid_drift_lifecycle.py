"""SUB-9 grid drift lifecycle unit tests (§6.1 pinned test IDs).

All 8 test IDs enumerated in
``openspec/changes/canonical-source-grid-registry/tasks.md`` §6.1 last pinned
block land here. Fixture provisioning is in-memory only — no DB, no NetCDF.
The fake ``_FakeDriftStore`` implements
:class:`workers.grid_registry.drift_lifecycle.DriftLifecycleStoreProtocol` with
dict-backed state so composer-layer semantics can be asserted without touching
a real DB (real-DB integration test deferred to follow-up node-27 run per
§6.1 Non-goal).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from packages.common import grid_registry_store as grid_registry_store_module
from packages.common.grid_registry_store import (
    CanonicalGridSnapshot,
    RegistryImmutabilityError,
    RegistryStoreError,
)
from workers.grid_registry import drift_lifecycle
from workers.grid_registry.drift_lifecycle import (
    DerivedCacheStoreProtocol,
    DriftLifecycleStoreProtocol,
    InPlaceSignatureReplacementForbiddenError,
    get_snapshot_supersession,
    latest_snapshot_for,
    register_new_version,
    reject_inplace_signature_replacement,
)

# -----------------------------------------------------------------------------
# Pinned constants (matching §3.1b provenance)
# -----------------------------------------------------------------------------

_PINNED_BBOX = {
    "south": 8.0,
    "north": 64.0,
    "west": 63.0,
    "east": 145.0,
}
_PINNED_NATIVE_RESOLUTION = 0.25
_VALID_FROM = datetime(2026, 5, 3, 0, 0, tzinfo=UTC)
_PRIOR_SUPERSEDED_AT = datetime(2026, 5, 3, 6, 0, tzinfo=UTC)
_NEW_SUPERSEDED_AT = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
_CANONICAL_KEY = "d" * 64
_PRIOR_SIGNATURE = "sig_prior_" + "0" * 54  # 64 chars
_NEW_SIGNATURE_TEMPLATE = "sig_new__{vector}"  # padded to 64 in the builder


# -----------------------------------------------------------------------------
# Fake stores — mirror SUB-3 semantics minimally
# -----------------------------------------------------------------------------


class _FakeDriftStore:
    """In-memory :class:`DriftLifecycleStoreProtocol` implementation.

    Captures ``insert_snapshot`` + ``supersede`` call sequences and answers
    ``load_snapshot_by_key`` / ``load_snapshot_by_id`` from a pre-seeded
    dict-backed state. On ``supersede`` the stored snapshot's
    ``superseded_at`` is updated in place (via :func:`dataclasses.replace`
    since ``CanonicalGridSnapshot`` is frozen) so subsequent reads reflect
    the current lifecycle state. ``insert_snapshot`` treats a new snapshot
    as inserted for id-lookup AND key-lookup routing (the composer semantics
    require latest_snapshot_for to return the newly inserted row after a
    supersession).
    """

    def __init__(
        self,
        seeded_by_id: dict[UUID, CanonicalGridSnapshot] | None = None,
    ) -> None:
        self._by_id: dict[UUID, CanonicalGridSnapshot] = dict(seeded_by_id or {})
        # Track the "current" (non-superseded) snapshot per canonical_grid_key
        # so latest_snapshot_for filters correctly.
        self._current_by_key: dict[str, UUID] = {}
        for snapshot_id, snapshot in self._by_id.items():
            if snapshot.superseded_at is None:
                self._current_by_key[snapshot.canonical_grid_key] = snapshot_id
        self.insert_snapshot_calls: list[
            tuple[CanonicalGridSnapshot, Sequence]
        ] = []
        self.supersede_calls: list[tuple[UUID, datetime]] = []

    def insert_snapshot(
        self,
        snapshot: CanonicalGridSnapshot,
        cells: Sequence,
    ) -> None:
        self.insert_snapshot_calls.append((snapshot, cells))
        assert snapshot.grid_snapshot_id is not None
        self._by_id[snapshot.grid_snapshot_id] = snapshot
        if snapshot.superseded_at is None:
            self._current_by_key[snapshot.canonical_grid_key] = (
                snapshot.grid_snapshot_id
            )

    def supersede(
        self,
        grid_snapshot_id: UUID,
        superseded_at: datetime,
    ) -> None:
        self.supersede_calls.append((grid_snapshot_id, superseded_at))
        prior = self._by_id[grid_snapshot_id]
        updated = replace(prior, superseded_at=superseded_at)
        self._by_id[grid_snapshot_id] = updated
        # Remove from current-by-key if this was the current pointer.
        key = updated.canonical_grid_key
        if self._current_by_key.get(key) == grid_snapshot_id:
            del self._current_by_key[key]

    def load_snapshot_by_key(
        self,
        canonical_grid_key: str,
    ) -> CanonicalGridSnapshot | None:
        snapshot_id = self._current_by_key.get(canonical_grid_key)
        if snapshot_id is None:
            return None
        return self._by_id[snapshot_id]

    def load_snapshot_by_id(
        self,
        grid_snapshot_id: UUID,
    ) -> CanonicalGridSnapshot | None:
        return self._by_id.get(grid_snapshot_id)


class _FakeDerivedCache:
    """In-memory :class:`DerivedCacheStoreProtocol` capture-only fake."""

    def __init__(self) -> None:
        self.mark_derived_stale_calls: list[UUID] = []

    def mark_derived_stale(self, grid_snapshot_id: UUID) -> None:
        self.mark_derived_stale_calls.append(grid_snapshot_id)


class _FailingDerivedCache:
    """:class:`DerivedCacheStoreProtocol` fake that raises on
    ``mark_derived_stale`` — exercises the composer's best-effort rollback."""

    def __init__(self) -> None:
        self.mark_derived_stale_calls: list[UUID] = []

    def mark_derived_stale(self, grid_snapshot_id: UUID) -> None:
        self.mark_derived_stale_calls.append(grid_snapshot_id)
        raise RuntimeError(
            f"derived cache failure for grid_snapshot_id={grid_snapshot_id}"
        )


# -----------------------------------------------------------------------------
# Snapshot builder — fills the 21-field dataclass
# -----------------------------------------------------------------------------


def _make_snapshot(
    grid_signature: str,
    canonical_grid_key: str = _CANONICAL_KEY,
    *,
    source_id: str = "ifs",
    grid_snapshot_id: UUID | None = None,
    superseded_at: datetime | None = None,
    converter_version: str = "1.0.0",
    latitude_order: str = "ascending",
    flatten_order: str = "y_major_lat_then_lon",
    longitude_convention: str = "[-180, 180)",
    native_resolution: float = _PINNED_NATIVE_RESOLUTION,
    bbox: dict[str, float] | None = None,
) -> CanonicalGridSnapshot:
    """Build a valid :class:`CanonicalGridSnapshot` with pinned §3.1b defaults."""
    resolved_id = grid_snapshot_id if grid_snapshot_id is not None else uuid4()
    resolved_bbox = bbox if bbox is not None else _PINNED_BBOX
    return CanonicalGridSnapshot(
        grid_snapshot_id=resolved_id,
        canonical_grid_key=canonical_grid_key,
        source_id=source_id,
        grid_id=f"{source_id}_0p25",
        grid_signature=grid_signature,
        grid_definition_uri=f"s3://nhms-canonical/{source_id}/grid/grid.json",
        grid_definition_checksum="a" * 64,
        longitude_convention=longitude_convention,
        latitude_order=latitude_order,
        flatten_order=flatten_order,
        native_resolution=native_resolution,
        bbox_south=resolved_bbox["south"],
        bbox_north=resolved_bbox["north"],
        bbox_west=resolved_bbox["west"],
        bbox_east=resolved_bbox["east"],
        converter_version=converter_version,
        valid_from=_VALID_FROM,
        valid_to=None,
        applicable_source_ids=("IFS",),
        superseded_at=superseded_at,
        created_at=None,
    )


def _pad_signature(prefix: str) -> str:
    """Right-pad an arbitrary marker to a 64-char signature-shaped string."""
    padded = (prefix + "0" * 64)[:64]
    return padded


# -----------------------------------------------------------------------------
# Test 1: new version supersedes old — 9 identity vectors
# -----------------------------------------------------------------------------


# Nine identity vectors in the exact §6.1 order:
# (1) cell count, (2) coordinates, (3) latitude order, (4) longitude convention,
# (5) grid_cell_id, (6) flatten order, (7) bbox, (8) converter cell-identity
# semantics (converter_version bump), (9) source product upgrade.
_IDENTITY_VECTORS = [
    "cell_count",
    "coordinates",
    "latitude_order",
    "longitude_convention",
    "grid_cell_id",
    "flatten_order",
    "bbox",
    "converter_version",
    "source_product_upgrade",
]


def _build_prior_and_new_for_vector(
    vector: str,
) -> tuple[CanonicalGridSnapshot, CanonicalGridSnapshot]:
    """For a given identity vector, produce a (prior, new) pair whose
    signatures differ, and — for the ``bbox`` vector — whose bboxes differ.

    The composer does NOT recompute the signature; SUB-1 owns that. Here we
    encode "identity changed" by supplying distinct ``grid_signature``
    literals, and — for the vector kinds that also live on the snapshot row
    itself (latitude_order, longitude_convention, flatten_order, bbox,
    converter_version) — mutate the corresponding row field so the round-trip
    can verify the new snapshot lands with the changed field intact.
    """
    prior_kwargs: dict[str, object] = {}
    new_kwargs: dict[str, object] = {}
    if vector == "latitude_order":
        prior_kwargs["latitude_order"] = "ascending"
        new_kwargs["latitude_order"] = "descending"
    elif vector == "longitude_convention":
        prior_kwargs["longitude_convention"] = "[-180, 180)"
        new_kwargs["longitude_convention"] = "[0, 360)"
    elif vector == "flatten_order":
        prior_kwargs["flatten_order"] = "y_major_lat_then_lon"
        new_kwargs["flatten_order"] = "x_major_lon_then_lat"
    elif vector == "bbox":
        new_kwargs["bbox"] = {
            "south": _PINNED_BBOX["south"],
            "north": _PINNED_BBOX["north"],
            "west": _PINNED_BBOX["west"],
            "east": _PINNED_BBOX["east"] + 1.0,
        }
    elif vector == "converter_version":
        prior_kwargs["converter_version"] = "1.0.0"
        new_kwargs["converter_version"] = "2.0.0"
    # cell_count / coordinates / grid_cell_id / source_product_upgrade are
    # encoded purely via signature-literal divergence — the row-shape-visible
    # column set on CanonicalGridSnapshot does not carry cell coordinates or
    # cell counts (those live on the met.canonical_grid_cell child table,
    # out-of-scope for the composer).

    prior = _make_snapshot(
        _PRIOR_SIGNATURE,
        canonical_grid_key=_CANONICAL_KEY,
        grid_snapshot_id=uuid4(),
        **prior_kwargs,  # type: ignore[arg-type]
    )
    new = _make_snapshot(
        _pad_signature(_NEW_SIGNATURE_TEMPLATE.format(vector=vector)),
        canonical_grid_key=_CANONICAL_KEY,
        grid_snapshot_id=uuid4(),
        **new_kwargs,  # type: ignore[arg-type]
    )
    return prior, new


@pytest.mark.parametrize("vector", _IDENTITY_VECTORS)
def test_new_version_supersedes_old(vector: str) -> None:
    """For each of the 9 identity vectors: a new snapshot with a different
    ``grid_signature`` supersedes the prior; the prior's identity fields
    remain byte-identical; ``latest_snapshot_for(key)`` returns the new
    snapshot; ``mark_derived_stale(prior_id)`` was called."""
    prior, new = _build_prior_and_new_for_vector(vector)
    prior_id = prior.grid_snapshot_id
    assert prior_id is not None
    new_id = new.grid_snapshot_id
    assert new_id is not None

    # Sanity: signatures differ per vector — this is the identity-changed
    # oracle. Signature computation itself is SUB-1's responsibility; the
    # composer does not recompute it.
    assert new.grid_signature != prior.grid_signature

    store = _FakeDriftStore(seeded_by_id={prior_id: prior})
    derived_cache = _FakeDerivedCache()

    returned = register_new_version(
        new,
        prior_id,
        _NEW_SUPERSEDED_AT,
        store=store,
        derived_cache_store=derived_cache,
    )
    assert returned == new_id

    # (b) supersede captured (prior_id, superseded_at).
    assert store.supersede_calls == [(prior_id, _NEW_SUPERSEDED_AT)]

    # (c) prior's identity fields byte-identical (only superseded_at flipped).
    updated_prior = store.load_snapshot_by_id(prior_id)
    assert updated_prior is not None
    assert updated_prior.grid_signature == prior.grid_signature
    assert updated_prior.grid_definition_uri == prior.grid_definition_uri
    assert updated_prior.grid_definition_checksum == prior.grid_definition_checksum
    assert updated_prior.canonical_grid_key == prior.canonical_grid_key
    assert updated_prior.bbox_south == prior.bbox_south
    assert updated_prior.bbox_north == prior.bbox_north
    assert updated_prior.bbox_west == prior.bbox_west
    assert updated_prior.bbox_east == prior.bbox_east
    assert updated_prior.superseded_at == _NEW_SUPERSEDED_AT

    # (d) latest_snapshot_for returns the new snapshot.
    latest = latest_snapshot_for(_CANONICAL_KEY, store=store)
    assert latest is not None
    assert latest.grid_snapshot_id == new_id
    assert latest.grid_signature == new.grid_signature

    # (e) mark_derived_stale was called with prior_id.
    assert derived_cache.mark_derived_stale_calls == [prior_id]


# -----------------------------------------------------------------------------
# Test 2: derived caches marked stale
# -----------------------------------------------------------------------------


def test_derived_caches_marked_stale() -> None:
    """The derived cache's ``mark_derived_stale`` is invoked with the prior
    snapshot's ``grid_snapshot_id`` after supersession."""
    prior = _make_snapshot(_PRIOR_SIGNATURE, grid_snapshot_id=uuid4())
    prior_id = prior.grid_snapshot_id
    assert prior_id is not None
    new = _make_snapshot(
        _pad_signature("sig_new_derived_"),
        grid_snapshot_id=uuid4(),
    )
    store = _FakeDriftStore(seeded_by_id={prior_id: prior})
    derived_cache = _FakeDerivedCache()

    register_new_version(
        new,
        prior_id,
        _NEW_SUPERSEDED_AT,
        store=store,
        derived_cache_store=derived_cache,
    )
    assert derived_cache.mark_derived_stale_calls == [prior_id]


# -----------------------------------------------------------------------------
# Test 3: registry rejects in-place signature replacement
# -----------------------------------------------------------------------------


def test_registry_rejects_inplace_signature_replacement() -> None:
    """``reject_inplace_signature_replacement`` ALWAYS raises with the pinned
    byte-for-byte message and pinned attributes."""
    snapshot_id = uuid4()
    attempted_signature = _pad_signature("sig_inplace_")
    store = _FakeDriftStore()
    with pytest.raises(InPlaceSignatureReplacementForbiddenError) as excinfo:
        reject_inplace_signature_replacement(
            snapshot_id,
            attempted_signature,
            store=store,
        )
    err = excinfo.value
    assert err.grid_snapshot_id == snapshot_id
    assert err.attempted_new_signature == attempted_signature
    assert err.field_or_op == "grid_signature_inplace_replace"
    # Pinned byte-for-byte message body per §6.1.
    assert str(err) == (
        "in-place grid_signature replacement is forbidden; "
        "register a new snapshot version instead"
    )
    # No state was mutated (the store fake would have recorded any writes).
    assert store.insert_snapshot_calls == []
    assert store.supersede_calls == []


# -----------------------------------------------------------------------------
# Test 4: latest_snapshot_for returns only current version
# -----------------------------------------------------------------------------


def test_latest_snapshot_for_returns_only_current_version() -> None:
    """When the store's ``load_snapshot_by_key`` returns a snapshot, so does
    :func:`latest_snapshot_for`. When it returns ``None`` (no current version
    exists), so does the composer."""
    current = _make_snapshot(_PRIOR_SIGNATURE, grid_snapshot_id=uuid4())
    current_id = current.grid_snapshot_id
    assert current_id is not None
    store = _FakeDriftStore(seeded_by_id={current_id: current})

    result = latest_snapshot_for(_CANONICAL_KEY, store=store)
    assert result is not None
    assert result.grid_snapshot_id == current_id

    # A key with no current version returns None.
    result_missing = latest_snapshot_for("no_such_key_" + "0" * 52, store=store)
    assert result_missing is None


# -----------------------------------------------------------------------------
# Test 5: get_snapshot_supersession returns row intact
# -----------------------------------------------------------------------------


def test_get_snapshot_supersession_returns_row_intact() -> None:
    """A superseded snapshot is returned intact alongside its
    ``superseded_at`` timestamp — spec.md scenario "Superseded snapshot is
    queryable but flagged"."""
    superseded_snapshot = _make_snapshot(
        _PRIOR_SIGNATURE,
        grid_snapshot_id=uuid4(),
        superseded_at=_PRIOR_SUPERSEDED_AT,
    )
    superseded_id = superseded_snapshot.grid_snapshot_id
    assert superseded_id is not None
    store = _FakeDriftStore(seeded_by_id={superseded_id: superseded_snapshot})

    returned_snapshot, returned_ts = get_snapshot_supersession(
        superseded_id, store=store
    )
    assert returned_snapshot is superseded_snapshot
    assert returned_ts == _PRIOR_SUPERSEDED_AT

    # Missing id raises RegistryStoreError.
    with pytest.raises(RegistryStoreError):
        get_snapshot_supersession(uuid4(), store=store)


# -----------------------------------------------------------------------------
# Test 6: best-effort rollback on derived-stale failure
# -----------------------------------------------------------------------------


def test_register_new_version_best_effort_rollback_on_derived_stale_failure() -> None:
    """When ``mark_derived_stale`` raises: (a) the failure propagates, (b) the
    composer's best-effort rollback re-invokes ``supersede`` with the prior's
    ORIGINAL ``superseded_at`` if it was non-None, and (c) if the prior's
    original ``superseded_at`` is ``None`` no rollback ``supersede`` is
    issued (SUB-3 store rejects ``superseded_at=None``)."""
    # (a) prior had a NON-None original superseded_at — rollback IS attempted.
    prior_with_ts = _make_snapshot(
        _PRIOR_SIGNATURE,
        grid_snapshot_id=uuid4(),
        superseded_at=_PRIOR_SUPERSEDED_AT,
    )
    prior_ts_id = prior_with_ts.grid_snapshot_id
    assert prior_ts_id is not None
    new_ts = _make_snapshot(
        _pad_signature("sig_new_rollback_ts_"),
        grid_snapshot_id=uuid4(),
    )
    store_ts = _FakeDriftStore(seeded_by_id={prior_ts_id: prior_with_ts})
    failing_cache_ts = _FailingDerivedCache()

    with pytest.raises(RuntimeError):
        register_new_version(
            new_ts,
            prior_ts_id,
            _NEW_SUPERSEDED_AT,
            store=store_ts,
            derived_cache_store=failing_cache_ts,
        )
    # supersede was called TWICE: first the new supersession, then the
    # best-effort rollback restoring the prior's original superseded_at.
    assert store_ts.supersede_calls == [
        (prior_ts_id, _NEW_SUPERSEDED_AT),
        (prior_ts_id, _PRIOR_SUPERSEDED_AT),
    ]
    # mark_derived_stale WAS attempted before raising.
    assert failing_cache_ts.mark_derived_stale_calls == [prior_ts_id]

    # (b) prior's original superseded_at was None — no rollback supersede
    # can be issued because SUB-3's store rejects superseded_at=None. The
    # prior is left in the "superseded" state (best-effort ends).
    prior_no_ts = _make_snapshot(
        _PRIOR_SIGNATURE,
        grid_snapshot_id=uuid4(),
        superseded_at=None,
    )
    prior_no_ts_id = prior_no_ts.grid_snapshot_id
    assert prior_no_ts_id is not None
    new_no_ts = _make_snapshot(
        _pad_signature("sig_new_rollback_no_ts_"),
        grid_snapshot_id=uuid4(),
    )
    store_no_ts = _FakeDriftStore(seeded_by_id={prior_no_ts_id: prior_no_ts})
    failing_cache_no_ts = _FailingDerivedCache()

    with pytest.raises(RuntimeError):
        register_new_version(
            new_no_ts,
            prior_no_ts_id,
            _NEW_SUPERSEDED_AT,
            store=store_no_ts,
            derived_cache_store=failing_cache_no_ts,
        )
    # Only the initial supersede call fired — no rollback second call.
    assert store_no_ts.supersede_calls == [(prior_no_ts_id, _NEW_SUPERSEDED_AT)]
    assert failing_cache_no_ts.mark_derived_stale_calls == [prior_no_ts_id]


# -----------------------------------------------------------------------------
# Test 7: static-import guard — module uses pinned SUB-3 symbols
# -----------------------------------------------------------------------------


def test_uses_pinned_symbols() -> None:
    """The module MUST resolve :class:`RegistryImmutabilityError`,
    :class:`CanonicalGridSnapshot`, and :class:`RegistryStoreError` to the
    pinned SUB-3 :mod:`packages.common.grid_registry_store` symbols. A future
    re-import from another module would drift the oracle."""
    assert (
        drift_lifecycle.RegistryImmutabilityError
        is grid_registry_store_module.RegistryImmutabilityError
    )
    assert (
        drift_lifecycle.CanonicalGridSnapshot
        is grid_registry_store_module.CanonicalGridSnapshot
    )
    assert (
        drift_lifecycle.RegistryStoreError
        is grid_registry_store_module.RegistryStoreError
    )


# -----------------------------------------------------------------------------
# Test 8: exception hierarchy — InPlaceSignatureReplacementForbiddenError
# -----------------------------------------------------------------------------


def test_drift_lifecycle_error_hierarchy() -> None:
    """:class:`InPlaceSignatureReplacementForbiddenError` inherits from BOTH
    :class:`RegistryImmutabilityError` (direct parent) AND
    :class:`RegistryStoreError` (transitive base)."""
    assert issubclass(
        InPlaceSignatureReplacementForbiddenError, RegistryImmutabilityError
    ) is True
    assert issubclass(
        InPlaceSignatureReplacementForbiddenError, RegistryStoreError
    ) is True


# -----------------------------------------------------------------------------
# Supplementary guardrails (composer preconditions) — kept alongside the 8
# pinned test IDs so a future refactor that drops the guardrail surfaces here.
# -----------------------------------------------------------------------------


def test_register_new_version_requires_new_snapshot_id() -> None:
    """The composer requires ``new_snapshot.grid_snapshot_id`` non-None so
    the return value is deterministic without touching a real DB."""
    prior = _make_snapshot(_PRIOR_SIGNATURE, grid_snapshot_id=uuid4())
    prior_id = prior.grid_snapshot_id
    assert prior_id is not None
    new_without_id = replace(
        _make_snapshot(_pad_signature("sig_new_no_id_")),
        grid_snapshot_id=None,
    )
    store = _FakeDriftStore(seeded_by_id={prior_id: prior})
    derived_cache = _FakeDerivedCache()
    with pytest.raises(ValueError):
        register_new_version(
            new_without_id,
            prior_id,
            _NEW_SUPERSEDED_AT,
            store=store,
            derived_cache_store=derived_cache,
        )


def test_register_new_version_requires_tz_aware_superseded_at() -> None:
    """SUB-3's store rejects naive ``superseded_at``; the composer performs an
    eager check so callers see ``ValueError`` at composer entry."""
    prior = _make_snapshot(_PRIOR_SIGNATURE, grid_snapshot_id=uuid4())
    prior_id = prior.grid_snapshot_id
    assert prior_id is not None
    new = _make_snapshot(
        _pad_signature("sig_new_naive_"),
        grid_snapshot_id=uuid4(),
    )
    store = _FakeDriftStore(seeded_by_id={prior_id: prior})
    derived_cache = _FakeDerivedCache()
    naive_ts = datetime(2026, 7, 6, 12, 0)  # no tzinfo
    with pytest.raises(ValueError):
        register_new_version(
            new,
            prior_id,
            naive_ts,
            store=store,
            derived_cache_store=derived_cache,
        )


def test_register_new_version_rejects_missing_prior() -> None:
    """When ``load_snapshot_by_id`` returns ``None`` the composer raises
    ``RegistryStoreError`` before any write."""
    new = _make_snapshot(
        _pad_signature("sig_new_missing_prior_"),
        grid_snapshot_id=uuid4(),
    )
    store = _FakeDriftStore()  # empty
    derived_cache = _FakeDerivedCache()
    with pytest.raises(RegistryStoreError):
        register_new_version(
            new,
            uuid4(),  # never seeded
            _NEW_SUPERSEDED_AT,
            store=store,
            derived_cache_store=derived_cache,
        )
    assert store.insert_snapshot_calls == []
    assert store.supersede_calls == []


def test_protocols_are_runtime_checkable() -> None:
    """Both structural types are ``@runtime_checkable`` so tests (and future
    consumers) can duck-test conformance without importing the concrete
    store implementation."""
    fake_store = _FakeDriftStore()
    fake_cache = _FakeDerivedCache()
    assert isinstance(fake_store, DriftLifecycleStoreProtocol)
    assert isinstance(fake_cache, DerivedCacheStoreProtocol)


# Unused imports guarded by test bodies elsewhere; suppress unused warnings by
# referencing the symbol.
_ = timedelta
