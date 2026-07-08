"""SUB-8 shared-binding eligibility unit tests (§5.1 pinned test IDs).

All 13 test IDs enumerated in ``openspec/changes/canonical-source-grid-registry/tasks.md``
§5.1 last Required-evidence bullet are landed here. Fixture provisioning is
in-memory only — no DB, no NetCDF. The fake ``_FakeStore`` mirrors SUB-3 store
semantics (position-preserving append, dedup on write) so acceptance-path
tests can assert final row state without touching a real store.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from packages.common import canonical_grid_key as canonical_grid_key_module
from packages.common import source_identity as source_identity_module
from packages.common.canonical_grid_key import derive_canonical_grid_key
from packages.common.grid_registry_store import CanonicalGridSnapshot, RegistryStoreError
from packages.common.source_identity import normalize_source_id
from workers.grid_registry import shared_binding_eligibility
from workers.grid_registry.shared_binding_eligibility import (
    ApplicableSourceIdsIncompleteError,
    CanonicalGridKeyMismatchError,
    ComparisonEvidenceAbsentError,
    GridRegistryStoreProtocol,
    SharedBindingEligibilityError,
    SharedBindingVerificationEvidence,
    SingleSourceVerifiedError,
    evaluate_shared_binding_eligibility,
)

# -----------------------------------------------------------------------------
# Pinned constants (matching §3.1b provenance and §3.3 backfill)
# -----------------------------------------------------------------------------

_BACKFILL_SIGNATURE_IFS_GFS = "6c008901b8b7" + "0" * 52  # 64 chars, hex, matches SUB-4/SUB-5
_ALT_SIGNATURE = "deadbeef" + "0" * 56  # 64 chars, hex, distinct from above
_PINNED_BBOX = {
    "south": 8.0,
    "north": 64.0,
    "west": 63.0,
    "east": 145.0,
}
_PINNED_NATIVE_RESOLUTION = 0.25
_VALID_FROM = datetime(2026, 5, 3, 0, 0, tzinfo=UTC)
_COMPARISON_URI = "s3://nhms-evidence/canonical-grid-registry/ifs-gfs-comparison.json"


# -----------------------------------------------------------------------------
# Fake store implementation — mirrors SUB-3 semantics
# -----------------------------------------------------------------------------


class _FakeStore:
    """In-memory store mirroring SUB-3 ``extend_applicable_source_ids`` semantics.

    Position-preserving append: existing entries stay in place; novel ids
    are appended in the supplied order. Duplicates are skipped (dedup on
    write), so calling twice with the same list is a no-op.
    """

    def __init__(self, initial_state: dict[UUID, list[str]]) -> None:
        self._state: dict[UUID, list[str]] = {
            k: list(v) for k, v in initial_state.items()
        }
        self.calls: list[tuple[UUID, tuple[str, ...]]] = []

    def extend_applicable_source_ids(
        self,
        grid_snapshot_id: UUID,
        source_ids: Sequence[str],
    ) -> None:
        self.calls.append((grid_snapshot_id, tuple(source_ids)))
        current = self._state[grid_snapshot_id]
        for candidate in source_ids:
            if candidate not in current:
                current.append(candidate)

    def state(self, grid_snapshot_id: UUID) -> list[str]:
        return list(self._state[grid_snapshot_id])


# -----------------------------------------------------------------------------
# Snapshot fixture builder — fills the 21-field dataclass
# -----------------------------------------------------------------------------


def _make_snapshot(
    grid_signature: str,
    source_id: str,
    applicable_source_ids: tuple[str, ...] = (),
    *,
    grid_snapshot_id: UUID | None = None,
    bbox: dict[str, float] | None = None,
    native_resolution: float = _PINNED_NATIVE_RESOLUTION,
) -> CanonicalGridSnapshot:
    """Build a valid ``CanonicalGridSnapshot`` with pinned §3.1b defaults."""
    resolved_id = grid_snapshot_id if grid_snapshot_id is not None else uuid4()
    resolved_bbox = bbox if bbox is not None else _PINNED_BBOX
    normalized_source = normalize_source_id(source_id)
    return CanonicalGridSnapshot(
        grid_snapshot_id=resolved_id,
        canonical_grid_key=derive_canonical_grid_key(
            grid_signature, resolved_bbox, native_resolution
        ),
        source_id=normalized_source,
        grid_id=f"{normalized_source.lower()}_0p25",
        grid_signature=grid_signature,
        grid_definition_uri=f"s3://nhms-canonical/{normalized_source}/grid/grid.json",
        grid_definition_checksum="a" * 64,
        longitude_convention="[-180, 180)",
        latitude_order="ascending",
        flatten_order="y_major_lat_then_lon",
        native_resolution=native_resolution,
        bbox_south=resolved_bbox["south"],
        bbox_north=resolved_bbox["north"],
        bbox_west=resolved_bbox["west"],
        bbox_east=resolved_bbox["east"],
        converter_version="1.0.0",
        valid_from=_VALID_FROM,
        valid_to=None,
        applicable_source_ids=applicable_source_ids,
        superseded_at=None,
        created_at=None,
    )


def _both_verified_evidence() -> SharedBindingVerificationEvidence:
    return SharedBindingVerificationEvidence(
        verified_source_ids=frozenset({"IFS", "gfs"}),
        comparison_evidence_uri=_COMPARISON_URI,
    )


# -----------------------------------------------------------------------------
# Test 1: happy path — same canonical key across different grid_id strings
# -----------------------------------------------------------------------------


def test_shareable_when_same_canonical_grid_key_different_grid_ids() -> None:
    """Shareable when signatures match (bbox + native_resolution pinned) even
    though ``grid_id`` strings differ across sources."""
    ifs_id = uuid4()
    gfs_id = uuid4()
    ifs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "ifs",
        applicable_source_ids=("IFS", "gfs"),
        grid_snapshot_id=ifs_id,
    )
    gfs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "gfs",
        applicable_source_ids=("gfs", "IFS"),
        grid_snapshot_id=gfs_id,
    )
    # grid_id strings must differ across sources per the spec scenario
    assert ifs.grid_id != gfs.grid_id
    store = _FakeStore({ifs_id: ["IFS", "gfs"], gfs_id: ["gfs", "IFS"]})
    result = evaluate_shared_binding_eligibility(
        ifs,
        gfs,
        verification_evidence=_both_verified_evidence(),
        store=store,
    )
    assert result is None
    assert sorted(store.state(ifs_id)) == ["IFS", "gfs"]
    assert sorted(store.state(gfs_id)) == ["IFS", "gfs"]


# -----------------------------------------------------------------------------
# Test 2: canonical key mismatch (different signatures)
# -----------------------------------------------------------------------------


def test_not_shareable_when_different_signatures() -> None:
    """Different ``grid_signature`` values yield different canonical keys and
    fail closed with :class:`CanonicalGridKeyMismatchError`."""
    ifs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "ifs",
        applicable_source_ids=("IFS", "gfs"),
    )
    gfs = _make_snapshot(
        _ALT_SIGNATURE,
        "gfs",
        applicable_source_ids=("gfs", "IFS"),
    )
    store = _FakeStore(
        {ifs.grid_snapshot_id: ["IFS", "gfs"], gfs.grid_snapshot_id: ["gfs", "IFS"]}
    )
    with pytest.raises(CanonicalGridKeyMismatchError) as excinfo:
        evaluate_shared_binding_eligibility(
            ifs,
            gfs,
            verification_evidence=_both_verified_evidence(),
            store=store,
        )
    err = excinfo.value
    assert err.canonical_grid_key_a != err.canonical_grid_key_b
    # Re-derived at eligibility time — must equal the SUB-4 helper for each side.
    assert err.canonical_grid_key_a == derive_canonical_grid_key(
        _BACKFILL_SIGNATURE_IFS_GFS, _PINNED_BBOX, _PINNED_NATIVE_RESOLUTION
    )
    assert err.canonical_grid_key_b == derive_canonical_grid_key(
        _ALT_SIGNATURE, _PINNED_BBOX, _PINNED_NATIVE_RESOLUTION
    )
    # No writes on denial.
    assert store.calls == []


# -----------------------------------------------------------------------------
# Test 3: single-source verification denies sharing
# -----------------------------------------------------------------------------


def test_denied_when_single_source_verified() -> None:
    """When ``verified_source_ids`` omits one of the snapshot source ids,
    :class:`SingleSourceVerifiedError` is raised carrying both attributes."""
    ifs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "ifs",
        applicable_source_ids=("IFS", "gfs"),
    )
    gfs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "gfs",
        applicable_source_ids=("gfs", "IFS"),
    )
    evidence = SharedBindingVerificationEvidence(
        verified_source_ids=frozenset({"IFS"}),  # gfs missing
        comparison_evidence_uri=_COMPARISON_URI,
    )
    store = _FakeStore(
        {ifs.grid_snapshot_id: ["IFS", "gfs"], gfs.grid_snapshot_id: ["gfs", "IFS"]}
    )
    with pytest.raises(SingleSourceVerifiedError) as excinfo:
        evaluate_shared_binding_eligibility(
            ifs,
            gfs,
            verification_evidence=evidence,
            store=store,
        )
    err = excinfo.value
    assert err.verified_source_ids == frozenset({"IFS"})
    assert set(err.snapshot_source_ids) == {"IFS", "gfs"}
    assert store.calls == []


# -----------------------------------------------------------------------------
# Tests 4-5: applicable_source_ids omission — split by which side omits
# -----------------------------------------------------------------------------


def test_denied_when_applicable_source_ids_omits_source_a() -> None:
    """When ``snapshot_a`` (IFS) omits gfs, first-raise is
    :class:`ApplicableSourceIdsIncompleteError` naming snapshot_a's id."""
    ifs_id = uuid4()
    gfs_id = uuid4()
    ifs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "ifs",
        applicable_source_ids=("IFS",),  # gfs missing
        grid_snapshot_id=ifs_id,
    )
    gfs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "gfs",
        applicable_source_ids=("gfs", "IFS"),
        grid_snapshot_id=gfs_id,
    )
    store = _FakeStore({ifs_id: ["IFS"], gfs_id: ["gfs", "IFS"]})
    with pytest.raises(ApplicableSourceIdsIncompleteError) as excinfo:
        evaluate_shared_binding_eligibility(
            ifs,
            gfs,
            verification_evidence=_both_verified_evidence(),
            store=store,
        )
    err = excinfo.value
    assert err.grid_snapshot_id == ifs_id
    assert err.applicable_source_ids == ("IFS",)
    assert err.missing_source_id == "gfs"
    assert store.calls == []


def test_denied_when_applicable_source_ids_omits_source_b() -> None:
    """When ``snapshot_b`` (gfs) omits IFS, first-raise is
    :class:`ApplicableSourceIdsIncompleteError` naming snapshot_b's id."""
    ifs_id = uuid4()
    gfs_id = uuid4()
    ifs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "ifs",
        applicable_source_ids=("IFS", "gfs"),
        grid_snapshot_id=ifs_id,
    )
    gfs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "gfs",
        applicable_source_ids=("gfs",),  # IFS missing
        grid_snapshot_id=gfs_id,
    )
    store = _FakeStore({ifs_id: ["IFS", "gfs"], gfs_id: ["gfs"]})
    with pytest.raises(ApplicableSourceIdsIncompleteError) as excinfo:
        evaluate_shared_binding_eligibility(
            ifs,
            gfs,
            verification_evidence=_both_verified_evidence(),
            store=store,
        )
    err = excinfo.value
    assert err.grid_snapshot_id == gfs_id
    assert err.applicable_source_ids == ("gfs",)
    assert err.missing_source_id == "IFS"
    assert store.calls == []


# -----------------------------------------------------------------------------
# Test 6: comparison evidence absent
# -----------------------------------------------------------------------------


def test_denied_when_comparison_evidence_absent() -> None:
    """``comparison_evidence_uri is None`` raises
    :class:`ComparisonEvidenceAbsentError` (no attributes)."""
    ifs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "ifs",
        applicable_source_ids=("IFS", "gfs"),
    )
    gfs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "gfs",
        applicable_source_ids=("gfs", "IFS"),
    )
    evidence = SharedBindingVerificationEvidence(
        verified_source_ids=frozenset({"IFS", "gfs"}),
        comparison_evidence_uri=None,
    )
    store = _FakeStore(
        {ifs.grid_snapshot_id: ["IFS", "gfs"], gfs.grid_snapshot_id: ["gfs", "IFS"]}
    )
    with pytest.raises(ComparisonEvidenceAbsentError):
        evaluate_shared_binding_eligibility(
            ifs,
            gfs,
            verification_evidence=evidence,
            store=store,
        )
    assert store.calls == []


# -----------------------------------------------------------------------------
# Test 7: normalize_source_id — five pinned equalities
# -----------------------------------------------------------------------------


def test_normalize_source_id_five_equalities() -> None:
    """The five pinned normalize_source_id equalities from §5.1."""
    assert normalize_source_id("ifs") == "IFS"
    assert normalize_source_id("IFS") == "IFS"
    assert normalize_source_id("GFS") == "gfs"
    assert normalize_source_id("gfs") == "gfs"
    assert normalize_source_id("era5") == "ERA5"


# -----------------------------------------------------------------------------
# Test 8: unknown source id raises and propagates
# -----------------------------------------------------------------------------


def test_normalize_source_id_unknown_raises() -> None:
    """Unknown source ids raise ``ValueError`` from ``normalize_source_id``
    directly AND propagate through :func:`evaluate_shared_binding_eligibility`."""
    with pytest.raises(ValueError):
        normalize_source_id("nope")

    # We CANNOT pass an unknown source_id through _make_snapshot because
    # _make_snapshot itself calls normalize_source_id for its defaults; we
    # therefore build a raw dataclass directly.
    ifs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "ifs",
        applicable_source_ids=("IFS", "gfs"),
    )
    # Rebuild ifs with a raw unknown source_id by dataclasses.replace.
    from dataclasses import replace

    ifs_unknown = replace(ifs, source_id="nope")
    gfs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "gfs",
        applicable_source_ids=("gfs", "IFS"),
    )
    store = _FakeStore(
        {ifs.grid_snapshot_id: ["IFS", "gfs"], gfs.grid_snapshot_id: ["gfs", "IFS"]}
    )
    with pytest.raises(ValueError):
        evaluate_shared_binding_eligibility(
            ifs_unknown,
            gfs,
            verification_evidence=_both_verified_evidence(),
            store=store,
        )


# -----------------------------------------------------------------------------
# Test 9: acceptance — both final states satisfy sorted(...) == ["IFS", "gfs"]
# -----------------------------------------------------------------------------


def test_acceptance_extends_applicable_source_ids_on_both_snapshots() -> None:
    """On acceptance, BOTH ``extend_applicable_source_ids`` calls fire with
    the SAME canonical pair, and the FINAL row state on BOTH snapshots
    satisfies ``sorted(state) == ["IFS", "gfs"]``."""
    ifs_id = uuid4()
    gfs_id = uuid4()
    ifs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "ifs",
        applicable_source_ids=("IFS", "gfs"),
        grid_snapshot_id=ifs_id,
    )
    gfs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "gfs",
        applicable_source_ids=("gfs", "IFS"),
        grid_snapshot_id=gfs_id,
    )
    store = _FakeStore({ifs_id: ["IFS", "gfs"], gfs_id: ["gfs", "IFS"]})
    evaluate_shared_binding_eligibility(
        ifs,
        gfs,
        verification_evidence=_both_verified_evidence(),
        store=store,
    )
    assert sorted(store.state(ifs_id)) == ["IFS", "gfs"]
    assert sorted(store.state(gfs_id)) == ["IFS", "gfs"]
    # Same canonical pair to both store calls.
    assert len(store.calls) == 2
    call_a_id, call_a_pair = store.calls[0]
    call_b_id, call_b_pair = store.calls[1]
    assert call_a_id == ifs_id
    assert call_b_id == gfs_id
    assert call_a_pair == ("IFS", "gfs")
    assert call_b_pair == ("IFS", "gfs")


# -----------------------------------------------------------------------------
# Test 10: canonical order independent of argument position
# -----------------------------------------------------------------------------


def test_acceptance_canonical_order_independent_of_argument_position() -> None:
    """Swapping ``(snapshot_a, snapshot_b)`` still produces the same canonical
    pair ``("IFS", "gfs")`` and the same final row states."""
    ifs_id = uuid4()
    gfs_id = uuid4()
    ifs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "ifs",
        applicable_source_ids=("IFS", "gfs"),
        grid_snapshot_id=ifs_id,
    )
    gfs = _make_snapshot(
        _BACKFILL_SIGNATURE_IFS_GFS,
        "gfs",
        applicable_source_ids=("gfs", "IFS"),
        grid_snapshot_id=gfs_id,
    )
    # Forward order.
    store_forward = _FakeStore({ifs_id: ["IFS", "gfs"], gfs_id: ["gfs", "IFS"]})
    evaluate_shared_binding_eligibility(
        ifs, gfs, verification_evidence=_both_verified_evidence(), store=store_forward
    )
    # Swapped order.
    store_swapped = _FakeStore({ifs_id: ["IFS", "gfs"], gfs_id: ["gfs", "IFS"]})
    evaluate_shared_binding_eligibility(
        gfs, ifs, verification_evidence=_both_verified_evidence(), store=store_swapped
    )
    # Both invocations produce identical final row states.
    assert sorted(store_forward.state(ifs_id)) == ["IFS", "gfs"]
    assert sorted(store_forward.state(gfs_id)) == ["IFS", "gfs"]
    assert sorted(store_swapped.state(ifs_id)) == ["IFS", "gfs"]
    assert sorted(store_swapped.state(gfs_id)) == ["IFS", "gfs"]
    # Same canonical pair passed regardless of argument position.
    for _snapshot_id, pair in store_forward.calls:
        assert pair == ("IFS", "gfs")
    for _snapshot_id, pair in store_swapped.calls:
        assert pair == ("IFS", "gfs")


# -----------------------------------------------------------------------------
# Test 11: static-import guard — module uses pinned SUB-4 / source_identity
# -----------------------------------------------------------------------------


def test_uses_pinned_symbols() -> None:
    """The module MUST resolve ``derive_canonical_grid_key`` and
    ``normalize_source_id`` to the pinned SUB-4 / ``packages.common.source_identity``
    symbols. A future re-import from another module would drift the oracle."""
    assert (
        shared_binding_eligibility.derive_canonical_grid_key
        is canonical_grid_key_module.derive_canonical_grid_key
    )
    assert (
        shared_binding_eligibility.normalize_source_id
        is source_identity_module.normalize_source_id
    )


# -----------------------------------------------------------------------------
# Test 12: exception hierarchy — all 4 subclasses under both bases
# -----------------------------------------------------------------------------


def test_shared_binding_eligibility_error_hierarchy() -> None:
    """All 4 subclasses inherit from BOTH
    :class:`SharedBindingEligibilityError` AND :class:`RegistryStoreError`."""
    subclasses = (
        CanonicalGridKeyMismatchError,
        SingleSourceVerifiedError,
        ApplicableSourceIdsIncompleteError,
        ComparisonEvidenceAbsentError,
    )
    for subclass in subclasses:
        assert issubclass(subclass, SharedBindingEligibilityError) is True
        assert issubclass(subclass, RegistryStoreError) is True


# -----------------------------------------------------------------------------
# Test 13: denial check order — first-raise matches pinned sequence
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "expected_error_type"),
    [
        ("all_four_violations", CanonicalGridKeyMismatchError),
        ("three_violations_no_key_mismatch", ComparisonEvidenceAbsentError),
        ("two_violations_evidence_and_state", SingleSourceVerifiedError),
        ("only_state_violation", ApplicableSourceIdsIncompleteError),
    ],
)
def test_denial_check_order_first_raise_matches_pin(
    scenario: str, expected_error_type: type[SharedBindingEligibilityError]
) -> None:
    """Multi-violation snapshots MUST raise in the pinned order:
    ``CanonicalGridKeyMismatchError`` -> ``ComparisonEvidenceAbsentError`` ->
    ``SingleSourceVerifiedError`` -> ``ApplicableSourceIdsIncompleteError``."""
    ifs_id = uuid4()
    gfs_id = uuid4()

    if scenario == "all_four_violations":
        # (1) mismatched signatures + (2) URI None + (3) verified empty +
        # (4) applicable empty. Expected: KEY mismatch fires first.
        ifs = _make_snapshot(
            _BACKFILL_SIGNATURE_IFS_GFS,
            "ifs",
            applicable_source_ids=(),
            grid_snapshot_id=ifs_id,
        )
        gfs = _make_snapshot(
            _ALT_SIGNATURE,
            "gfs",
            applicable_source_ids=(),
            grid_snapshot_id=gfs_id,
        )
        evidence = SharedBindingVerificationEvidence(
            verified_source_ids=frozenset(),
            comparison_evidence_uri=None,
        )
    elif scenario == "three_violations_no_key_mismatch":
        # Keys match; (2) URI None + (3) verified empty + (4) applicable empty.
        # Expected: EVIDENCE absent fires first.
        ifs = _make_snapshot(
            _BACKFILL_SIGNATURE_IFS_GFS,
            "ifs",
            applicable_source_ids=(),
            grid_snapshot_id=ifs_id,
        )
        gfs = _make_snapshot(
            _BACKFILL_SIGNATURE_IFS_GFS,
            "gfs",
            applicable_source_ids=(),
            grid_snapshot_id=gfs_id,
        )
        evidence = SharedBindingVerificationEvidence(
            verified_source_ids=frozenset(),
            comparison_evidence_uri=None,
        )
    elif scenario == "two_violations_evidence_and_state":
        # Keys match; URI present; (3) verified empty + (4) applicable empty.
        # Expected: SINGLE-SOURCE verified fires first.
        ifs = _make_snapshot(
            _BACKFILL_SIGNATURE_IFS_GFS,
            "ifs",
            applicable_source_ids=(),
            grid_snapshot_id=ifs_id,
        )
        gfs = _make_snapshot(
            _BACKFILL_SIGNATURE_IFS_GFS,
            "gfs",
            applicable_source_ids=(),
            grid_snapshot_id=gfs_id,
        )
        evidence = SharedBindingVerificationEvidence(
            verified_source_ids=frozenset(),
            comparison_evidence_uri=_COMPARISON_URI,
        )
    elif scenario == "only_state_violation":
        # Keys match; URI present; both verified; only (4) applicable empty.
        # Expected: APPLICABLE incomplete fires.
        ifs = _make_snapshot(
            _BACKFILL_SIGNATURE_IFS_GFS,
            "ifs",
            applicable_source_ids=(),
            grid_snapshot_id=ifs_id,
        )
        gfs = _make_snapshot(
            _BACKFILL_SIGNATURE_IFS_GFS,
            "gfs",
            applicable_source_ids=(),
            grid_snapshot_id=gfs_id,
        )
        evidence = _both_verified_evidence()
    else:  # pragma: no cover
        raise AssertionError(f"unhandled scenario {scenario!r}")

    store = _FakeStore({ifs_id: [], gfs_id: []})
    with pytest.raises(expected_error_type):
        evaluate_shared_binding_eligibility(
            ifs,
            gfs,
            verification_evidence=evidence,
            store=cast(GridRegistryStoreProtocol, store),
        )
    assert store.calls == []
