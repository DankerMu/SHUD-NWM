"""Source-shared binding eligibility decision (SUB-8 / Task 5.1).

Owns the pure-function decision for granting shared-binding eligibility between
two ``CanonicalGridSnapshot`` rows keyed on ``canonical_grid_key`` equality.
The oracle for both keys is the SUB-4
:func:`packages.common.canonical_grid_key.derive_canonical_grid_key` helper
applied to each snapshot's ``(grid_signature, bbox, native_resolution)``; the
source-id case is normalized through
:func:`packages.common.source_identity.normalize_source_id`, which is the same
rule the contract parser uses.

Public API is a single entry point
:func:`evaluate_shared_binding_eligibility` returning ``None`` on acceptance
after calling ``store.extend_applicable_source_ids`` for BOTH snapshots with
the SAME canonically-ordered pair, and raising a
:class:`SharedBindingEligibilityError` subclass on any denial.

Denial check order (pinned by §5.1)
-----------------------------------
1. :class:`CanonicalGridKeyMismatchError` — pre-eligibility gate. If the two
   snapshots derive different ``canonical_grid_key`` values, all other checks
   are moot.
2. :class:`ComparisonEvidenceAbsentError` — cheap URI-None check on the
   ``verification_evidence.comparison_evidence_uri`` attribute.
3. :class:`SingleSourceVerifiedError` — evidence-set check that both
   normalized source ids appear in ``verification_evidence.verified_source_ids``.
4. :class:`ApplicableSourceIdsIncompleteError` — registry-state check that
   BOTH snapshot rows already list both source ids in their
   ``applicable_source_ids`` tuple.

The order is required so ``test_denial_check_order_first_raise_matches_pin``
can lock the sequence and a fix in one branch does not accidentally unmask
another.

Canonical pair ordering
-----------------------
On acceptance the pair passed to BOTH store calls is
``tuple(sorted({normalize_source_id(a), normalize_source_id(b)}))``. Python
default ``sorted()`` on the set gives ``("IFS", "gfs")`` for the IFS/gfs pair
(uppercase ``I`` < lowercase ``g`` in ASCII), matching the §3.3 backfill
set-equality oracle regardless of argument position.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from packages.common.canonical_grid_key import derive_canonical_grid_key
from packages.common.grid_registry_store import (
    CanonicalGridSnapshot,
    RegistryStoreError,
)
from packages.common.source_identity import normalize_source_id

__all__ = [
    "ApplicableSourceIdsIncompleteError",
    "CanonicalGridKeyMismatchError",
    "ComparisonEvidenceAbsentError",
    "GridRegistryStoreProtocol",
    "SharedBindingEligibilityError",
    "SharedBindingVerificationEvidence",
    "SingleSourceVerifiedError",
    "evaluate_shared_binding_eligibility",
]


# -----------------------------------------------------------------------------
# Exception hierarchy — pinned attribute contracts per §5.1
# -----------------------------------------------------------------------------


class SharedBindingEligibilityError(RegistryStoreError):
    """Base for all shared-binding eligibility denials.

    Sibling of :class:`packages.common.grid_registry_bbox_guard.BboxMismatchError`
    and :class:`workers.grid_registry.stability.StabilityVerificationError`
    under the shared :class:`RegistryStoreError` taxonomy.
    """


class CanonicalGridKeyMismatchError(SharedBindingEligibilityError):
    """Raised when the two snapshots derive different ``canonical_grid_key`` values.

    Re-derived at eligibility time via
    :func:`packages.common.canonical_grid_key.derive_canonical_grid_key` — NOT
    read from ``snapshot.canonical_grid_key`` directly — so a stored-key vs
    current-code-derivation drift surfaces here as a mismatch rather than
    silently accepting a stale persisted key.
    """

    def __init__(
        self,
        *,
        canonical_grid_key_a: str,
        canonical_grid_key_b: str,
    ) -> None:
        self.canonical_grid_key_a = canonical_grid_key_a
        self.canonical_grid_key_b = canonical_grid_key_b
        super().__init__(
            f"canonical_grid_key mismatch: "
            f"canonical_grid_key_a={canonical_grid_key_a!r}, "
            f"canonical_grid_key_b={canonical_grid_key_b!r}"
        )


class SingleSourceVerifiedError(SharedBindingEligibilityError):
    """Raised when only one source has been verified on representative cycles."""

    def __init__(
        self,
        *,
        verified_source_ids: frozenset[str],
        snapshot_source_ids: tuple[str, str],
    ) -> None:
        self.verified_source_ids = verified_source_ids
        self.snapshot_source_ids = snapshot_source_ids
        super().__init__(
            f"shared-binding eligibility requires both sources verified: "
            f"verified_source_ids={sorted(verified_source_ids)!r}, "
            f"snapshot_source_ids={snapshot_source_ids!r}"
        )


class ApplicableSourceIdsIncompleteError(SharedBindingEligibilityError):
    """Raised when a snapshot's ``applicable_source_ids`` omits a required source id.

    The registry state alone answers the eligibility question per spec.md
    scenario "applicable_source_ids omission denies sharing" — no external
    manifest is consulted.
    """

    def __init__(
        self,
        *,
        grid_snapshot_id: UUID,
        applicable_source_ids: tuple[str, ...],
        missing_source_id: str,
    ) -> None:
        self.grid_snapshot_id = grid_snapshot_id
        self.applicable_source_ids = applicable_source_ids
        self.missing_source_id = missing_source_id
        super().__init__(
            f"applicable_source_ids on grid_snapshot_id={grid_snapshot_id} "
            f"omits required source_id={missing_source_id!r}: "
            f"applicable_source_ids={applicable_source_ids!r}"
        )


class ComparisonEvidenceAbsentError(SharedBindingEligibilityError):
    """Raised when the archived comparison evidence URI is ``None``."""

    def __init__(self) -> None:
        super().__init__(
            "shared-binding eligibility requires archived comparison evidence: "
            "verification_evidence.comparison_evidence_uri is None"
        )


# -----------------------------------------------------------------------------
# Value objects
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SharedBindingVerificationEvidence:
    """Summarized verification evidence carrier consumed by the decision.

    Attributes
    ----------
    verified_source_ids:
        Set of normalized source ids (post-``normalize_source_id``) that have
        been verified against the shared ``canonical_grid_key`` on
        representative cycles. Both snapshot source ids MUST appear here or
        eligibility is denied. ``__post_init__`` re-runs ``normalize_source_id``
        on every entry: a lowercase mis-wire that would silently produce a
        ``SingleSourceVerifiedError`` false-denial instead lands as canonical
        casing at construction; an unknown id fails-fast with ``ValueError``.
    comparison_evidence_uri:
        URI of the archived multi-cycle dual-source signature-comparison
        artifact. ``None`` denies eligibility (a missing URI means no
        archived evidence exists).
    """

    verified_source_ids: frozenset[str]
    comparison_evidence_uri: str | None

    def __post_init__(self) -> None:
        # Enforce the "post-normalize_source_id" docstring contract at
        # construction time — lowercase mis-wire => canonical casing; unknown
        # id => fail-fast ValueError from normalize_source_id.
        normalized = frozenset(
            normalize_source_id(sid) for sid in self.verified_source_ids
        )
        object.__setattr__(self, "verified_source_ids", normalized)


@runtime_checkable
class GridRegistryStoreProtocol(Protocol):
    """Structural type for the ``extend_applicable_source_ids`` primitive.

    Duck-compatible with
    :class:`packages.common.grid_registry_store.PsycopgGridRegistryStore` but
    does NOT import it, so this module has no runtime dependency on the
    concrete psycopg2-backed store implementation. Tests inject a minimal
    fake that faithfully mirrors SUB-3 store semantics (position-preserving
    append, dedup on write).
    """

    def extend_applicable_source_ids(
        self,
        grid_snapshot_id: UUID,
        source_ids: Sequence[str],
    ) -> None: ...


# -----------------------------------------------------------------------------
# Decision entry point
# -----------------------------------------------------------------------------


def _snapshot_bbox_dict(snapshot: CanonicalGridSnapshot) -> dict[str, float]:
    return {
        "south": snapshot.bbox_south,
        "north": snapshot.bbox_north,
        "west": snapshot.bbox_west,
        "east": snapshot.bbox_east,
    }


def evaluate_shared_binding_eligibility(
    snapshot_a: CanonicalGridSnapshot,
    snapshot_b: CanonicalGridSnapshot,
    *,
    verification_evidence: SharedBindingVerificationEvidence,
    store: GridRegistryStoreProtocol,
) -> None:
    """Decide shared-binding eligibility for two ``CanonicalGridSnapshot`` rows.

    Parameters
    ----------
    snapshot_a, snapshot_b:
        The two ``CanonicalGridSnapshot`` rows to evaluate. Argument position
        does not affect the outcome: the canonical pair passed to the store
        is always ``tuple(sorted({normalize_source_id(a), normalize_source_id(b)}))``.
    verification_evidence:
        Summarized verification set + comparison-evidence URI.
    store:
        Object satisfying :class:`GridRegistryStoreProtocol` — invoked twice
        on acceptance (once per snapshot) with the SAME canonical pair.

    Returns
    -------
    ``None`` on acceptance.

    Raises
    ------
    CanonicalGridKeyMismatchError
        When the two snapshots derive different ``canonical_grid_key``
        values under the SUB-4 helper.
    ComparisonEvidenceAbsentError
        When ``verification_evidence.comparison_evidence_uri is None``.
    SingleSourceVerifiedError
        When either normalized source id is missing from
        ``verification_evidence.verified_source_ids``.
    ApplicableSourceIdsIncompleteError
        When EITHER snapshot's ``applicable_source_ids`` omits a required
        source id at eligibility-check time. ``snapshot_a`` is checked
        before ``snapshot_b`` to keep the first-raise sequence deterministic.
    ValueError
        Raised for caller-contract violations that precede the 4-tier denial
        cascade: (1) either snapshot has ``grid_snapshot_id is None``
        (unpersisted snapshot — the pinned
        :class:`ApplicableSourceIdsIncompleteError.grid_snapshot_id` is
        non-optional ``UUID``), (2) the two snapshots normalize to the SAME
        ``source_id`` (shared-binding is a CROSS-SOURCE decision per spec.md;
        same-source is a caller contract violation, not a denial branch), or
        (3) either snapshot's ``source_id`` is not one of the accepted
        labels (propagated unchanged from :func:`normalize_source_id`).
    """
    # Precondition 1: persisted-snapshot invariant. Downstream raise sites
    # populate ApplicableSourceIdsIncompleteError.grid_snapshot_id (pinned
    # non-optional UUID at tasks.md §5.1 line 146); enforce here so mypy /
    # runtime agree the field is never None past this point.
    if snapshot_a.grid_snapshot_id is None or snapshot_b.grid_snapshot_id is None:
        raise ValueError(
            "evaluate_shared_binding_eligibility requires persisted snapshots; "
            f"snapshot_a.grid_snapshot_id={snapshot_a.grid_snapshot_id!r}, "
            f"snapshot_b.grid_snapshot_id={snapshot_b.grid_snapshot_id!r}"
        )

    # normalize_source_id may raise ValueError on unknown source ids — let it
    # propagate per §5.1 (an unknown source id fails closed).
    source_id_a = normalize_source_id(snapshot_a.source_id)
    source_id_b = normalize_source_id(snapshot_b.source_id)

    # Precondition 2: distinct-source invariant. Shared-binding is defined by
    # spec.md as a CROSS-SOURCE decision (IFS/gfs, IFS/ERA5, gfs/ERA5); same-
    # source is a caller contract violation, not a denial branch, and does
    # NOT warrant a SharedBindingEligibilityError subclass.
    if source_id_a == source_id_b:
        raise ValueError(
            f"shared-binding eligibility requires two DISTINCT source ids; both "
            f"snapshots normalize to {source_id_a!r}"
        )

    # Re-derive both canonical keys at eligibility time (do NOT trust the
    # stored ``canonical_grid_key`` — a stored-key vs current-code drift
    # surfaces here rather than silently accepting a stale persisted key).
    key_a = derive_canonical_grid_key(
        snapshot_a.grid_signature,
        _snapshot_bbox_dict(snapshot_a),
        snapshot_a.native_resolution,
    )
    key_b = derive_canonical_grid_key(
        snapshot_b.grid_signature,
        _snapshot_bbox_dict(snapshot_b),
        snapshot_b.native_resolution,
    )

    # (1) Pre-eligibility gate — canonical key equality.
    if key_a != key_b:
        raise CanonicalGridKeyMismatchError(
            canonical_grid_key_a=key_a,
            canonical_grid_key_b=key_b,
        )

    # (2) Cheap URI-None check on the archived comparison evidence.
    if verification_evidence.comparison_evidence_uri is None:
        raise ComparisonEvidenceAbsentError()

    # (3) Evidence-set check — both normalized source ids must appear.
    snapshot_source_ids = (source_id_a, source_id_b)
    missing_from_evidence = {source_id_a, source_id_b} - verification_evidence.verified_source_ids
    if missing_from_evidence:
        raise SingleSourceVerifiedError(
            verified_source_ids=verification_evidence.verified_source_ids,
            snapshot_source_ids=snapshot_source_ids,
        )

    # (4) Registry-state check — BOTH snapshots must already list both
    # source ids in ``applicable_source_ids``. Check snapshot_a first, then
    # snapshot_b, so multi-violation ordering is deterministic.
    for snapshot in (snapshot_a, snapshot_b):
        applicable = tuple(snapshot.applicable_source_ids)
        for required in (source_id_a, source_id_b):
            if required not in applicable:
                raise ApplicableSourceIdsIncompleteError(
                    grid_snapshot_id=snapshot.grid_snapshot_id,
                    applicable_source_ids=applicable,
                    missing_source_id=required,
                )

    # Acceptance — canonical pair passed identically to BOTH store calls.
    canonical_pair = tuple(sorted({source_id_a, source_id_b}))
    store.extend_applicable_source_ids(snapshot_a.grid_snapshot_id, canonical_pair)
    store.extend_applicable_source_ids(snapshot_b.grid_snapshot_id, canonical_pair)
    return None
