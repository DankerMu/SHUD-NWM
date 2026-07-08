"""Grid drift lifecycle primitives (SUB-9 / Task 6.1).

Owns the pure-function decision layer for grid drift supersession over the
SUB-3 store primitives (:mod:`packages.common.grid_registry_store`). Any change
to a grid's identity (cell count, coordinates, latitude order, longitude
convention, ``grid_cell_id``, flatten order, bbox, converter cell-identity
semantics, or source product upgrade) registers a NEW immutable snapshot
version rather than editing the existing one — see the
``grid-drift-lifecycle`` spec for the frozen Requirements + Scenarios.

Public API (5 free functions + 2 protocols)
-------------------------------------------
* :func:`register_new_version` — INSERTs a new snapshot, SETs the prior's
  ``superseded_at``, and MARKs derived caches stale for the prior. Cross-
  primitive best-effort rollback: if ``mark_derived_stale`` raises, invert
  ``supersede`` back to the prior's original ``superseded_at`` (may be
  ``None``). Documented as best-effort — cross-primitive atomicity is
  out-of-scope for a free-function composer.
* :func:`latest_snapshot_for` — returns the most recent non-superseded
  snapshot for a ``canonical_grid_key``, or ``None``. The store's Protocol
  contract already filters non-superseded rows; this composer does not
  re-check.
* :func:`get_snapshot_supersession` — returns ``(snapshot, superseded_at)``
  for a persisted ``grid_snapshot_id``. ``superseded_at`` is ``None`` for
  current-version rows.
* :func:`reject_inplace_signature_replacement` — ALWAYS raises
  :class:`InPlaceSignatureReplacementForbiddenError`. Never mutates state.
* :class:`DriftLifecycleStoreProtocol` — structural type for the four SUB-3
  store primitives this module consumes (insert, supersede, load-by-key,
  load-by-id). ``load_snapshot_by_key`` is a NEW query; the implementer may
  add it to SUB-3's psycopg2-backed store OR wrap raw SQL locally.
* :class:`DerivedCacheStoreProtocol` — structural type for the NEW
  ``mark_derived_stale`` primitive that flips ``active_flag=false`` +
  ``superseded_at=now()`` on ``met.met_station`` and ``met.interp_weight``
  rows tied to a passed ``grid_snapshot_id``.

Registry surface is decision-only: no DB integration test lands in this PR
(deferred to the SUB-9 follow-up node-27 run per tasks.md §6.1 Non-goal;
SUB-3 store already covers the physical ``supersede`` primitive and SUB-2
migration covers the derived-cache column existence).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from packages.common.grid_registry_store import (
    CanonicalGridSnapshot,
    RegistryImmutabilityError,
    RegistryStoreError,
)

__all__ = [
    "DerivedCacheStoreProtocol",
    "DriftLifecycleStoreProtocol",
    "InPlaceSignatureReplacementForbiddenError",
    "get_snapshot_supersession",
    "latest_snapshot_for",
    "register_new_version",
    "reject_inplace_signature_replacement",
]


# -----------------------------------------------------------------------------
# Exception hierarchy — pinned attribute contract per §6.1
# -----------------------------------------------------------------------------


class InPlaceSignatureReplacementForbiddenError(RegistryImmutabilityError):
    """Raised when a caller attempts to replace a snapshot's ``grid_signature``
    in place rather than registering a new snapshot version.

    Subclass of :class:`packages.common.grid_registry_store.RegistryImmutabilityError`
    (transitively of :class:`RegistryStoreError`) so callers who already handle
    the SUB-3 append-only rejection surface also handle this one. The
    ``field_or_op`` attribute inherited from
    :class:`RegistryImmutabilityError` is fixed as
    ``"grid_signature_inplace_replace"`` and the message body pins the literal
    ``"in-place grid_signature replacement is forbidden; register a new
    snapshot version instead"`` byte-for-byte.
    """

    def __init__(
        self,
        *,
        grid_snapshot_id: UUID,
        attempted_new_signature: str,
    ) -> None:
        self.grid_snapshot_id = grid_snapshot_id
        self.attempted_new_signature = attempted_new_signature
        self.field_or_op = "grid_signature_inplace_replace"
        # Bypass RegistryImmutabilityError.__init__ (which composes a store-
        # facing message about immutability); we pin our own byte-for-byte
        # message per §6.1 while retaining the RegistryImmutabilityError base
        # taxonomy for callers that already handle append-only rejections.
        RuntimeError.__init__(
            self,
            "in-place grid_signature replacement is forbidden; "
            "register a new snapshot version instead",
        )


# -----------------------------------------------------------------------------
# Structural protocols — @runtime_checkable so tests can inject fakes
# -----------------------------------------------------------------------------


@runtime_checkable
class DriftLifecycleStoreProtocol(Protocol):
    """Structural type for the four SUB-3 store primitives this module uses.

    ``insert_snapshot`` matches
    :meth:`packages.common.grid_registry_store.PsycopgGridRegistryStore.insert_snapshot`;
    ``supersede`` matches its :meth:`supersede` primitive. ``load_snapshot_by_key``
    is a NEW query — the implementer may add it to the psycopg2-backed store
    OR wrap raw SQL locally; the composer only depends on the structural type.
    ``load_snapshot_by_id`` matches an id-keyed fetch that ignores
    supersession state (a superseded row is returned intact so callers can
    read its ``superseded_at`` field).
    """

    def insert_snapshot(
        self,
        snapshot: CanonicalGridSnapshot,
        cells: Sequence,
    ) -> None: ...

    def supersede(
        self,
        grid_snapshot_id: UUID,
        superseded_at: datetime,
    ) -> None: ...

    def load_snapshot_by_key(
        self,
        canonical_grid_key: str,
    ) -> CanonicalGridSnapshot | None: ...

    def load_snapshot_by_id(
        self,
        grid_snapshot_id: UUID,
    ) -> CanonicalGridSnapshot | None: ...


@runtime_checkable
class DerivedCacheStoreProtocol(Protocol):
    """Structural type for the derived-cache staleness primitive.

    ``mark_derived_stale`` flips ``active_flag=false`` + ``superseded_at=now()``
    on ``met.met_station`` and ``met.interp_weight`` rows tied to the passed
    ``grid_snapshot_id`` per spec.md scenario "met.met_station and
    met.interp_weight rows are marked stale on supersession". Rows are NOT
    deleted (audit history retained). The real DB implementation lives outside
    this module; the composer only depends on the structural type.
    """

    def mark_derived_stale(self, grid_snapshot_id: UUID) -> None: ...


# -----------------------------------------------------------------------------
# Composer entry points
# -----------------------------------------------------------------------------


def register_new_version(
    new_snapshot: CanonicalGridSnapshot,
    prior_snapshot_id: UUID,
    superseded_at: datetime,
    *,
    store: DriftLifecycleStoreProtocol,
    derived_cache_store: DerivedCacheStoreProtocol,
) -> UUID:
    """Register a new snapshot version and supersede the prior.

    Steps (executed in order):

    1. INSERT ``new_snapshot`` via ``store.insert_snapshot`` (empty cells
       sequence — the composer does not own cell provisioning; callers
       supply a fully-populated snapshot and this thin composer only wires
       the lifecycle primitives).
    2. Mark the prior snapshot superseded via ``store.supersede``.
    3. Flip derived-cache staleness on the prior via
       ``derived_cache_store.mark_derived_stale``. If this raises, attempt a
       best-effort rollback of step 2 by re-invoking ``store.supersede`` with
       the prior's ORIGINAL ``superseded_at`` (may be ``None``); if
       ``prior_original_superseded_at`` is ``None`` the SUB-3 store cannot
       un-supersede (it rejects ``superseded_at=None``), so best-effort ends
       there and the prior is left in the "superseded" state. Re-raise the
       original ``mark_derived_stale`` failure either way.

    Parameters
    ----------
    new_snapshot:
        The new immutable ``CanonicalGridSnapshot`` to register. MUST have a
        non-None ``grid_snapshot_id`` (the SUB-3 store also generates the id
        when None, but this composer requires the id up front so the return
        value is deterministic and testable without touching a real DB).
    prior_snapshot_id:
        UUID of the snapshot whose ``superseded_at`` is being set. MUST refer
        to an existing row loadable via ``store.load_snapshot_by_id``.
    superseded_at:
        Tz-aware UTC timestamp to write on the prior row. The SUB-3 store
        rejects naive datetimes for ``superseded_at``; this composer performs
        an eager tz-aware check so the caller sees the ``ValueError`` at
        composer entry rather than mid-way through the lifecycle.
    store:
        Object satisfying :class:`DriftLifecycleStoreProtocol`.
    derived_cache_store:
        Object satisfying :class:`DerivedCacheStoreProtocol`. Invoked with
        ``prior_snapshot_id`` after the ``supersede`` call succeeds.

    Returns
    -------
    ``new_snapshot.grid_snapshot_id`` on success.

    Raises
    ------
    ValueError
        When ``new_snapshot.grid_snapshot_id is None`` or ``superseded_at``
        is naive (no tzinfo).
    RegistryStoreError
        When ``prior_snapshot_id`` does not resolve via
        ``store.load_snapshot_by_id``.
    Exception
        Any error raised by ``store.insert_snapshot`` /
        ``store.supersede`` / ``derived_cache_store.mark_derived_stale``
        propagates. If ``mark_derived_stale`` raises AND
        ``prior_original_superseded_at`` is non-None, a best-effort rollback
        of step 2 is attempted before re-raising. If the rollback
        ``store.supersede`` call ITSELF raises (double-fault), the rollback
        failure is DISCARDED (swallowed by a bare ``except: pass``) and the
        ORIGINAL ``mark_derived_stale`` exception is re-raised — the primary
        failure is never masked by a transient DB error on the compensating
        write. The discarded rollback exception is NOT preserved as
        ``__context__`` on the re-raised original (the bare ``raise`` re-
        raises the ORIGINAL with its original context; Python does not
        retro-fit the swallowed rollback exception onto a re-raise). Post-
        mortem after a double-fault: check derived-cache logs for the
        primary, then inspect registry state manually — the compensating
        write's failure is not observable through the exception chain.

    Orphan-state gap (documented, not defended)
    -------------------------------------------
    If step 2 (``store.supersede``) raises AFTER step 1
    (``store.insert_snapshot``) has already committed, the composer CANNOT
    roll back the insert: SUB-3's store forbids ``delete_snapshot`` on
    already-committed rows (append-only immutability). The exception
    propagates as-is, but the registry is left with BOTH the prior
    (non-superseded) AND the new (non-superseded) row for the same
    ``canonical_grid_key`` — two "active" rows for one key. Operator
    intervention is required to reconcile (typically: retry ``supersede``
    on the prior once the transient failure clears). Naming this gap out
    is intentional: cross-primitive atomicity is out-of-scope for a
    free-function composer.
    """
    if new_snapshot.grid_snapshot_id is None:
        raise ValueError(
            "register_new_version requires new_snapshot.grid_snapshot_id to "
            "be non-None; the composer's return value is the id it was given."
        )
    if superseded_at.tzinfo is None:
        raise ValueError(
            "register_new_version requires tz-aware superseded_at; got naive "
            f"value {superseded_at!r}."
        )

    prior_snapshot = store.load_snapshot_by_id(prior_snapshot_id)
    if prior_snapshot is None:
        raise RegistryStoreError(
            f"prior_snapshot_id {prior_snapshot_id} not found."
        )
    prior_original_superseded_at = prior_snapshot.superseded_at

    # Step 1: INSERT the new snapshot. The composer does not own cell
    # provisioning; callers supply a fully-populated snapshot and pass empty
    # cells here (the SUB-3 store's insert_snapshot contract requires a
    # Sequence positional; test fixtures use an in-memory fake so real cell
    # shape is not enforced at this composer layer).
    store.insert_snapshot(new_snapshot, [])

    # Step 2: Mark the prior superseded.
    store.supersede(prior_snapshot_id, superseded_at)

    # Step 3: Flip derived-cache staleness on the prior; best-effort rollback
    # of step 2 if this raises.
    try:
        derived_cache_store.mark_derived_stale(prior_snapshot_id)
    except Exception:
        if prior_original_superseded_at is not None:
            # SUB-3 store's supersede accepts any tz-aware datetime; restoring
            # the prior's original superseded_at is a valid re-supersede.
            # The rollback itself is wrapped so a double-fault (rollback
            # ``supersede`` also raises) cannot mask the primary
            # ``mark_derived_stale`` exception. The rollback failure is
            # DISCARDED by the inner ``except: pass`` below — it is NOT
            # preserved as ``__context__`` on the re-raised original. The
            # bare ``raise`` outside this inner try re-raises the ORIGINAL
            # ``mark_derived_stale`` exception with its ORIGINAL context;
            # Python does not retro-fit the swallowed rollback exception
            # onto a re-raise. Losing the rollback exception is intentional:
            # surface visibility of the primary derived-cache failure is
            # the priority; the rollback double-fault is discoverable only
            # via post-mortem inspection of registry state.
            try:
                store.supersede(prior_snapshot_id, prior_original_superseded_at)
            except Exception:
                # Discard the rollback failure — the ORIGINAL
                # mark_derived_stale exception is still the primary that
                # gets re-raised on the bare ``raise`` below. The rollback
                # exception is lost (not chained via __context__ on the
                # re-raise); rely on operator-side registry inspection to
                # detect a failed compensating write.
                pass
        # If prior_original_superseded_at is None, the SUB-3 store cannot
        # un-supersede (it rejects superseded_at=None), so best-effort ends
        # here — the prior is left in the "superseded" state.
        raise

    return new_snapshot.grid_snapshot_id


def latest_snapshot_for(
    canonical_grid_key: str,
    *,
    store: DriftLifecycleStoreProtocol,
) -> CanonicalGridSnapshot | None:
    """Return the current (non-superseded) snapshot for ``canonical_grid_key``.

    The store's ``load_snapshot_by_key`` Protocol contract already filters
    non-superseded rows (returns the most recent non-superseded snapshot for
    the key, or ``None`` if none exists). This composer is a thin pass-through
    kept as a named entry point so downstream consumers (forcing producer
    preflight, mapping-asset build, station-binding manifest validator) can
    depend on the API name rather than the store method directly.
    """
    return store.load_snapshot_by_key(canonical_grid_key)


def get_snapshot_supersession(
    grid_snapshot_id: UUID,
    *,
    store: DriftLifecycleStoreProtocol,
) -> tuple[CanonicalGridSnapshot, datetime | None]:
    """Return ``(snapshot, superseded_at)`` for a persisted ``grid_snapshot_id``.

    ``superseded_at`` is ``None`` for current-version rows and non-None for
    superseded rows (spec.md scenario "Superseded snapshot is queryable but
    flagged"). The row is returned intact regardless of supersession state.

    Raises
    ------
    RegistryStoreError
        When ``grid_snapshot_id`` does not resolve via
        ``store.load_snapshot_by_id``.
    """
    snapshot = store.load_snapshot_by_id(grid_snapshot_id)
    if snapshot is None:
        raise RegistryStoreError(
            f"grid_snapshot_id {grid_snapshot_id} not found."
        )
    return snapshot, snapshot.superseded_at


def reject_inplace_signature_replacement(
    grid_snapshot_id: UUID,
    new_signature: str,
    *,
    store: DriftLifecycleStoreProtocol,
) -> None:
    """ALWAYS raise :class:`InPlaceSignatureReplacementForbiddenError`.

    The registry API forbids any request that would replace an already-
    registered snapshot's ``grid_signature`` in place — the caller must
    register a new snapshot version instead (spec.md scenario "Registry API
    rejects in-place signature replacement"). This composer never mutates
    state; the ``store`` parameter is accepted for API symmetry with future
    extensions that may need to consult the store before rejecting (for
    example to surface the prior signature in the error).
    """
    del store  # unused: rejection is unconditional
    raise InPlaceSignatureReplacementForbiddenError(
        grid_snapshot_id=grid_snapshot_id,
        attempted_new_signature=new_signature,
    )
