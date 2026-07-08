"""Tests for packages/common/grid_registry_bbox_guard.py.

Covers issue #903 (Epic #897 SUB-6) Task 3.2 Evidence Floor:

* matching env vs snapshot bbox returns ``None`` silently,
* per-field mismatch fails closed with ``BboxMismatchError`` carrying both
  ``expected_bbox`` and ``actual_bbox`` dicts + ``grid_snapshot_id``,
* exact IEEE-754 float equality: any bit-difference rejects (parametrized over
  a rejection ladder including ``+1e-13`` / ``+1e-11`` / ``+1e-9`` / ``+1e-6`` /
  ``+0.001``),
* dual-surface boundary agreement at ``+1e-13`` (guard raises AND
  ``derive_canonical_grid_key`` produces a different key) and at exact ``+0.0``
  (guard returns None AND derived keys are byte-equal),
* finiteness contract: NaN / inf / -inf on either env or snapshot side raises
  raw ``ValueError`` naming the offending field + side (NOT
  ``BboxMismatchError``),
* signed zero (``-0.0`` vs ``0.0``) is treated as DIFFERENT on both surfaces —
  guard raises AND ``derive_canonical_grid_key`` yields distinct keys — locking
  the deferred joint-normalization divergence in step with SUB-4,
* raw ``ValueError`` from a malformed env or an invalid ``GeoBBox`` propagates
  unwrapped (NOT re-raised as ``BboxMismatchError``),
* the guard has no side effects on the snapshot (``copy.deepcopy`` compare),
* the guard's signature is exactly two parameters (``registered_snapshot`` +
  keyword-only ``env_reader``); a third parameter is a hard fail,
* the ``RegisteredBboxSnapshotProtocol`` accepts a real
  :class:`CanonicalGridSnapshot` via ``isinstance``,
* drift consequence chain: a bbox mismatch under the same signature +
  same ``native_resolution`` produces a different ``canonical_grid_key`` at
  the tightest float boundary (``+1e-13``).

All tests use a lightweight ``_FakeSnapshot`` dataclass that satisfies the
Protocol via structural subtyping; only the Protocol-compat test constructs a
real ``CanonicalGridSnapshot``.
"""

from __future__ import annotations

import copy
import inspect
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from packages.common.canonical_grid_key import derive_canonical_grid_key
from packages.common.grid_registry_bbox_guard import (
    BboxMismatchError,
    RegisteredBboxSnapshotProtocol,
    verify_download_bbox_matches_registry,
)
from packages.common.grid_registry_store import (
    CanonicalGridSnapshot,
    RegistryStoreError,
)
from workers.data_adapters.region import (
    DEFAULT_BBOX_EAST,
    DEFAULT_BBOX_NORTH,
    DEFAULT_BBOX_SOUTH,
    DEFAULT_BBOX_WEST,
    GeoBBox,
    china_buffered_bbox_from_env,
)

# IFS/GFS 0.25° pinned China-buffered bbox — sourced from
# workers/data_adapters/region.py::DEFAULT_BBOX_* so any future re-tune of the
# default region propagates here without silent drift.
_PINNED_SOUTH = DEFAULT_BBOX_SOUTH  # 8.0
_PINNED_NORTH = DEFAULT_BBOX_NORTH  # 64.0
_PINNED_WEST = DEFAULT_BBOX_WEST    # 63.0
_PINNED_EAST = DEFAULT_BBOX_EAST    # 145.0

# Shared grid identity for dual-surface (guard vs derive_canonical_grid_key)
# assertions. The signature suffix is padded to 64 lowercase hex characters.
_SHARED_SIGNATURE = "6c008901b8b7" + "0" * 52
_SHARED_NATIVE_RESOLUTION = 0.25


@dataclass(frozen=True)
class _FakeSnapshot:
    """Structural stand-in for ``CanonicalGridSnapshot`` used by the guard.

    Carries only the five attributes named by
    :class:`RegisteredBboxSnapshotProtocol`. Using a minimal dataclass keeps
    each test independent of SUB-3's full dataclass shape.
    """

    bbox_south: float
    bbox_north: float
    bbox_west: float
    bbox_east: float
    grid_snapshot_id: uuid.UUID | None


def _make_snapshot(
    *,
    south: float = _PINNED_SOUTH,
    north: float = _PINNED_NORTH,
    west: float = _PINNED_WEST,
    east: float = _PINNED_EAST,
    grid_snapshot_id: uuid.UUID | None = None,
) -> _FakeSnapshot:
    return _FakeSnapshot(
        bbox_south=south,
        bbox_north=north,
        bbox_west=west,
        bbox_east=east,
        grid_snapshot_id=grid_snapshot_id,
    )


def _reader_returning(bbox: GeoBBox) -> Callable[[], GeoBBox]:
    """Return an ``env_reader`` closure that yields the given ``GeoBBox``."""
    return lambda: bbox


def _reader_returning_with_field(field: str, value: float) -> Callable[[], GeoBBox]:
    """Return an ``env_reader`` that constructs a valid GeoBBox and then
    overrides ``field`` to ``value`` via ``object.__setattr__``.

    Used to inject NaN / inf / -inf / signed zero on the env side without
    tripping :class:`GeoBBox.__post_init__` validation. ``dataclasses.replace``
    re-invokes ``__post_init__`` for a ``frozen=True`` dataclass and therefore
    cannot be used to bypass validation; ``object.__setattr__`` is the
    documented escape hatch for frozen dataclasses.
    """

    def reader() -> GeoBBox:
        bbox = GeoBBox(
            south=_PINNED_SOUTH,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
        object.__setattr__(bbox, field, value)
        return bbox

    return reader


# -----------------------------------------------------------------------------
# 1. Happy path: matching env vs snapshot returns None silently.
# -----------------------------------------------------------------------------


def test_matching_bbox_passes_silently() -> None:
    """Env reader returns an exact match; guard returns None (no side effects)."""
    snapshot = _make_snapshot()
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
    )
    assert verify_download_bbox_matches_registry(snapshot, env_reader=reader) is None


def test_bbox_exact_equality_matches() -> None:
    """Bit-identical env vs snapshot bbox passes; documents the exact-equality contract."""
    snapshot = _make_snapshot()
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
    )
    assert verify_download_bbox_matches_registry(snapshot, env_reader=reader) is None


# -----------------------------------------------------------------------------
# 2. Per-field mismatch fails closed with dict-form expected + actual.
# -----------------------------------------------------------------------------


def test_mismatched_south_fails_closed_with_expected_and_actual() -> None:
    """Env south=10.0 vs snapshot south=8.0; error carries both bbox dicts."""
    snapshot = _make_snapshot(south=_PINNED_SOUTH)
    env_bbox = GeoBBox(
        south=10.0,
        north=_PINNED_NORTH,
        west=_PINNED_WEST,
        east=_PINNED_EAST,
    )
    reader = _reader_returning(env_bbox)

    with pytest.raises(BboxMismatchError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    err = excinfo.value
    assert err.expected_bbox["south"] == 10.0
    assert err.actual_bbox["south"] == _PINNED_SOUTH
    # All other fields agree, but are still populated in both dicts.
    assert err.expected_bbox["north"] == _PINNED_NORTH
    assert err.actual_bbox["north"] == _PINNED_NORTH
    assert err.expected_bbox["west"] == _PINNED_WEST
    assert err.actual_bbox["west"] == _PINNED_WEST
    assert err.expected_bbox["east"] == _PINNED_EAST
    assert err.actual_bbox["east"] == _PINNED_EAST


# One mismatched-field parametrization over all four bbox corners.
_FIELD_TO_PERTURBED_ENV = {
    "south": {"south": _PINNED_SOUTH + 0.5, "north": _PINNED_NORTH, "west": _PINNED_WEST, "east": _PINNED_EAST},
    "north": {"south": _PINNED_SOUTH, "north": _PINNED_NORTH - 0.5, "west": _PINNED_WEST, "east": _PINNED_EAST},
    "west": {"south": _PINNED_SOUTH, "north": _PINNED_NORTH, "west": _PINNED_WEST + 0.5, "east": _PINNED_EAST},
    "east": {"south": _PINNED_SOUTH, "north": _PINNED_NORTH, "west": _PINNED_WEST, "east": _PINNED_EAST - 0.5},
}


@pytest.mark.parametrize("field", ["south", "north", "west", "east"])
def test_mismatched_single_field_fails_closed(field: str) -> None:
    """Perturbing any single field by 0.5 forces ``BboxMismatchError``."""
    snapshot = _make_snapshot()
    env = _FIELD_TO_PERTURBED_ENV[field]
    reader = _reader_returning(GeoBBox(**env))

    with pytest.raises(BboxMismatchError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    err = excinfo.value
    assert err.expected_bbox[field] == env[field]
    snapshot_value = getattr(snapshot, f"bbox_{field}")
    assert err.actual_bbox[field] == snapshot_value


# -----------------------------------------------------------------------------
# 3. Exact IEEE-754 equality: any bit-difference rejects, no tolerance.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("delta", [1e-13, 1e-12, 1e-11, 1e-9, 1e-6, 0.001])
def test_bbox_rejects_any_bit_perturbation(delta: float) -> None:
    """Any non-zero perturbation raises; guard mirrors SUB-4 raw-float compare.

    Covers the whole rejection ladder from the tightest float boundary
    (``+1e-13`` — where SUB-4's raw ``json.dumps`` also produces distinct
    keys) up through operator-scale ``0.001`` degrees.
    """
    snapshot = _make_snapshot(south=_PINNED_SOUTH)
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH + delta,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
    )
    with pytest.raises(BboxMismatchError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    err = excinfo.value
    # The offending field must be recoverable from the dicts via inequality.
    assert err.expected_bbox["south"] != err.actual_bbox["south"]


# -----------------------------------------------------------------------------
# 4. Dual-surface boundary agreement: guard AND derive_canonical_grid_key
#    agree at both the tightest reject boundary (+1e-13) and the exact match
#    boundary (+0.0). Locks the pinned §3.2 "bbox match ⟺ same
#    canonical_grid_key" invariant at the float boundary.
# -----------------------------------------------------------------------------


def test_bbox_boundary_agreement_at_1e13_both_reject() -> None:
    """At +1e-13, BOTH guard AND derive_canonical_grid_key disagree with baseline.

    Locks the ⟺ invariant at the tightest float boundary: any bit-different
    bbox that would silently pass a ``.12f``-canonicalized guard is (a) a
    distinct ``canonical_grid_key`` under SUB-4, so guard MUST also reject.
    """
    baseline = {
        "south": _PINNED_SOUTH,
        "north": _PINNED_NORTH,
        "west": _PINNED_WEST,
        "east": _PINNED_EAST,
    }
    perturbed = {**baseline, "south": _PINNED_SOUTH + 1e-13}

    # SUB-4 side: derived keys differ at +1e-13 (raw json.dumps on bbox).
    key_baseline = derive_canonical_grid_key(
        _SHARED_SIGNATURE, baseline, _SHARED_NATIVE_RESOLUTION
    )
    key_perturbed = derive_canonical_grid_key(
        _SHARED_SIGNATURE, perturbed, _SHARED_NATIVE_RESOLUTION
    )
    assert key_baseline != key_perturbed, (
        "SUB-4 derive_canonical_grid_key must yield distinct keys at +1e-13; "
        "if this ever collapses, the guard's exact-equality compare no longer "
        "mirrors SUB-4 and the ⟺ invariant is broken."
    )

    # Guard side: MUST also reject at +1e-13.
    snapshot = _make_snapshot(
        south=baseline["south"],
        grid_snapshot_id=uuid.uuid4(),
    )
    reader = _reader_returning(
        GeoBBox(
            south=perturbed["south"],
            north=perturbed["north"],
            west=perturbed["west"],
            east=perturbed["east"],
        )
    )
    with pytest.raises(BboxMismatchError):
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)


def test_bbox_boundary_agreement_at_exact_zero_both_pass() -> None:
    """At +0.0 exact, BOTH guard AND derive_canonical_grid_key agree with baseline.

    Symmetric partner to the +1e-13 test: bit-identical bboxes MUST produce
    identical ``canonical_grid_key`` AND the guard MUST return None. Locks
    the "match ⟺ same key" side of the invariant.
    """
    baseline = {
        "south": _PINNED_SOUTH,
        "north": _PINNED_NORTH,
        "west": _PINNED_WEST,
        "east": _PINNED_EAST,
    }
    same = dict(baseline)

    key_baseline = derive_canonical_grid_key(
        _SHARED_SIGNATURE, baseline, _SHARED_NATIVE_RESOLUTION
    )
    key_same = derive_canonical_grid_key(
        _SHARED_SIGNATURE, same, _SHARED_NATIVE_RESOLUTION
    )
    assert key_baseline == key_same

    snapshot = _make_snapshot()
    reader = _reader_returning(
        GeoBBox(
            south=same["south"],
            north=same["north"],
            west=same["west"],
            east=same["east"],
        )
    )
    assert verify_download_bbox_matches_registry(snapshot, env_reader=reader) is None


# -----------------------------------------------------------------------------
# 5. Finiteness contract: NaN / inf / -inf on either side raises raw
#    ValueError (NOT BboxMismatchError) naming field + side. Matches SUB-4
#    _validate_bbox policy at canonical_grid_key.py:106-139.
# -----------------------------------------------------------------------------


def test_bbox_rejects_nan_on_env_side() -> None:
    """Env-side NaN raises raw ValueError naming the field and 'env'."""
    snapshot = _make_snapshot()
    reader = _reader_returning_with_field("south", float("nan"))
    with pytest.raises(ValueError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    assert not isinstance(excinfo.value, BboxMismatchError)
    msg = str(excinfo.value)
    assert "south" in msg
    assert "env" in msg


def test_bbox_rejects_nan_on_snapshot_side() -> None:
    """Snapshot-side NaN raises raw ValueError naming the field and 'snapshot'.

    Guards against the pre-fix silent-pass where ``f"{nan:.12f}" == "nan"``
    degenerates into string-compare equality on the snapshot side.
    """
    snapshot = _make_snapshot(south=float("nan"))
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
    )
    with pytest.raises(ValueError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    assert not isinstance(excinfo.value, BboxMismatchError)
    msg = str(excinfo.value)
    assert "south" in msg
    assert "snapshot" in msg


_NON_FINITE_VALUES = [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="neginf"),
]


@pytest.mark.parametrize("bad_value", _NON_FINITE_VALUES)
@pytest.mark.parametrize("field", ["south", "north", "west", "east"])
def test_bbox_rejects_non_finite_on_env_side(bad_value: float, field: str) -> None:
    """Env-side NaN/inf/-inf on any of the four fields raises raw ValueError."""
    snapshot = _make_snapshot()
    reader = _reader_returning_with_field(field, bad_value)
    with pytest.raises(ValueError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    assert not isinstance(excinfo.value, BboxMismatchError)
    assert field in str(excinfo.value)
    assert "env" in str(excinfo.value)


@pytest.mark.parametrize("bad_value", _NON_FINITE_VALUES)
@pytest.mark.parametrize("field", ["south", "north", "west", "east"])
def test_bbox_rejects_non_finite_on_snapshot_side(
    bad_value: float, field: str
) -> None:
    """Snapshot-side NaN/inf/-inf on any of the four fields raises raw ValueError."""
    snapshot_kwargs = {field: bad_value}
    snapshot = _make_snapshot(**snapshot_kwargs)
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
    )
    with pytest.raises(ValueError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    assert not isinstance(excinfo.value, BboxMismatchError)
    assert field in str(excinfo.value)
    assert "snapshot" in str(excinfo.value)


# -----------------------------------------------------------------------------
# 6. Signed zero (-0.0 vs 0.0): treated as DIFFERENT on both surfaces. Locks
#    the deferred joint-normalization divergence — single-side normalization
#    on the guard would BREAK the ⟺ invariant this task exists to preserve.
# -----------------------------------------------------------------------------


def test_bbox_signed_zero_treated_as_different_matching_sub4() -> None:
    """Guard raises AND ``derive_canonical_grid_key`` yields distinct keys for -0.0 vs 0.0.

    Both surfaces AGREE that ``-0.0`` and ``+0.0`` are different:

    * SUB-4 (``derive_canonical_grid_key``) — via ``json.dumps("-0.0") !=
      json.dumps("0.0")``: the two derived keys differ.
    * Guard (``verify_download_bbox_matches_registry``) — via a
      ``math.copysign`` sign-bit check that catches the case IEEE-754
      ``==`` misses (``-0.0 == 0.0`` is True under IEEE-754), so
      ``BboxMismatchError`` is raised.

    Locks the deferred joint-normalization divergence: a single-side fix on
    EITHER surface would BREAK the ⟺ invariant SUB-6 exists to preserve.
    The follow-up carrier bullet in tasks.md §3.2 records that a coordinated
    joint openspec change SHOULD normalize signed zero (via ``float(v) +
    0.0``) on BOTH surfaces simultaneously; this test must update in that
    coordinated change to reflect the unified behavior.
    """
    baseline_bbox = {
        "south": _PINNED_SOUTH,
        "north": _PINNED_NORTH,
        "west": 0.0,
        "east": _PINNED_EAST,
    }
    signed_zero_bbox = {**baseline_bbox, "west": -0.0}

    # SUB-4 side: signed-zero produces distinct keys via json.dumps.
    key_positive = derive_canonical_grid_key(
        _SHARED_SIGNATURE, baseline_bbox, _SHARED_NATIVE_RESOLUTION
    )
    key_signed = derive_canonical_grid_key(
        _SHARED_SIGNATURE, signed_zero_bbox, _SHARED_NATIVE_RESOLUTION
    )
    assert key_positive != key_signed, (
        "SUB-4 derive_canonical_grid_key must yield distinct keys for -0.0 vs 0.0 "
        "via json.dumps('-0.0') != json.dumps('0.0'). If SUB-4 ever normalizes "
        "signed zero to positive, the guard's behavior must be updated in a "
        "coordinated joint change."
    )

    # Guard side: env west=-0.0 vs snapshot bbox_west=0.0 MUST raise. The
    # sign-bit check via math.copysign distinguishes signed zero even
    # though IEEE-754 == treats them as equal.
    snapshot = _make_snapshot(west=0.0)
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH,
            north=_PINNED_NORTH,
            west=-0.0,
            east=_PINNED_EAST,
        )
    )
    with pytest.raises(BboxMismatchError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    # Both dict forms carry the exact float values (sign preserved).
    err = excinfo.value
    assert err.expected_bbox["west"] == 0.0  # IEEE-754: -0.0 == 0.0 == 0.0
    # But signs differ — that is why we raised.
    assert math.copysign(1.0, err.expected_bbox["west"]) == -1.0
    assert math.copysign(1.0, err.actual_bbox["west"]) == 1.0


# -----------------------------------------------------------------------------
# 7. Env-reader failure propagation: raw ValueError, not BboxMismatchError.
# -----------------------------------------------------------------------------


def test_bbox_guard_propagates_malformed_env_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NHMS_DOWNLOAD_BBOX_SOUTH=notafloat`` surfaces raw ``ValueError``."""
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_SOUTH", "notafloat")
    snapshot = _make_snapshot()
    with pytest.raises(ValueError) as excinfo:
        # Default env_reader is china_buffered_bbox_from_env; do not inject.
        verify_download_bbox_matches_registry(snapshot)
    # Ensure the raw error escaped rather than being wrapped as a mismatch.
    assert not isinstance(excinfo.value, BboxMismatchError)


def test_bbox_guard_propagates_invalid_geobbox_valueerror() -> None:
    """A ``GeoBBox`` with latitude out of range raises raw ``ValueError``."""

    def bad_reader() -> GeoBBox:
        # south=100.0 fails the __post_init__ latitude range check.
        return GeoBBox(south=100.0, north=110.0, west=_PINNED_WEST, east=_PINNED_EAST)

    snapshot = _make_snapshot()
    with pytest.raises(ValueError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=bad_reader)
    assert not isinstance(excinfo.value, BboxMismatchError)


# -----------------------------------------------------------------------------
# 8. Error attributes carry grid_snapshot_id (both real UUID and None).
# -----------------------------------------------------------------------------


def test_bbox_error_carries_grid_snapshot_id() -> None:
    """A real UUID on the snapshot is copied verbatim onto the error."""
    snapshot_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    snapshot = _make_snapshot(south=_PINNED_SOUTH, grid_snapshot_id=snapshot_id)
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH + 1.0,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
    )
    with pytest.raises(BboxMismatchError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    assert excinfo.value.grid_snapshot_id == snapshot_id


def test_bbox_error_grid_snapshot_id_none_when_unregistered_candidate() -> None:
    """An unregistered candidate snapshot (``grid_snapshot_id=None``) propagates as None."""
    snapshot = _make_snapshot(south=_PINNED_SOUTH, grid_snapshot_id=None)
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH + 1.0,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
    )
    with pytest.raises(BboxMismatchError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    assert excinfo.value.grid_snapshot_id is None


# -----------------------------------------------------------------------------
# 9. Side-effect-free guarantee (deepcopy compare, both match + mismatch case).
# -----------------------------------------------------------------------------


def test_guard_has_no_side_effects_on_snapshot() -> None:
    """Match and mismatch paths both leave the snapshot bit-for-bit equal."""
    # Match path
    snapshot = _make_snapshot()
    before = copy.deepcopy(snapshot)
    verify_download_bbox_matches_registry(
        snapshot,
        env_reader=_reader_returning(
            GeoBBox(
                south=_PINNED_SOUTH,
                north=_PINNED_NORTH,
                west=_PINNED_WEST,
                east=_PINNED_EAST,
            )
        ),
    )
    assert snapshot == before

    # Mismatch path
    snapshot2 = _make_snapshot()
    before2 = copy.deepcopy(snapshot2)
    try:
        verify_download_bbox_matches_registry(
            snapshot2,
            env_reader=_reader_returning(
                GeoBBox(
                    south=_PINNED_SOUTH + 1.0,
                    north=_PINNED_NORTH,
                    west=_PINNED_WEST,
                    east=_PINNED_EAST,
                )
            ),
        )
    except BboxMismatchError:
        pass
    assert snapshot2 == before2


# -----------------------------------------------------------------------------
# 10. Signature contract: exactly two parameters, env_reader keyword-only,
#     default is `china_buffered_bbox_from_env` (not an ad-hoc lambda).
# -----------------------------------------------------------------------------


def test_guard_signature_is_two_parameters_exactly() -> None:
    """No third positional (e.g. a future ``store=None``) is allowed."""
    parameters = inspect.signature(verify_download_bbox_matches_registry).parameters
    assert list(parameters.keys()) == ["registered_snapshot", "env_reader"]
    assert parameters["env_reader"].kind is inspect.Parameter.KEYWORD_ONLY


def test_guard_env_reader_default_is_china_buffered() -> None:
    """The module-level default is the pinned entry point, not a lambda."""
    default = inspect.signature(verify_download_bbox_matches_registry).parameters[
        "env_reader"
    ].default
    assert default is china_buffered_bbox_from_env


# -----------------------------------------------------------------------------
# 11. Protocol compatibility with the real CanonicalGridSnapshot dataclass.
#     Proves the guard has no runtime dependency on the store dataclass but
#     still accepts it structurally.
# -----------------------------------------------------------------------------


def test_registered_bbox_snapshot_protocol_accepts_canonical_grid_snapshot() -> None:
    """``CanonicalGridSnapshot`` structurally satisfies the guard's Protocol."""
    snap = CanonicalGridSnapshot(
        grid_snapshot_id=uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        canonical_grid_key="0" * 64,
        source_id="IFS",
        grid_id="ifs_0p25",
        grid_signature=_SHARED_SIGNATURE,
        grid_definition_uri="canonical/IFS/grid/ifs_0p25/grid.json",
        grid_definition_checksum="0" * 64,
        longitude_convention="-180..180",
        latitude_order="descending",
        flatten_order="lat_major",
        native_resolution=0.25,
        bbox_south=_PINNED_SOUTH,
        bbox_north=_PINNED_NORTH,
        bbox_west=_PINNED_WEST,
        bbox_east=_PINNED_EAST,
        converter_version="v1",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_to=None,
        applicable_source_ids=("IFS",),
    )
    assert isinstance(snap, RegisteredBboxSnapshotProtocol)


# -----------------------------------------------------------------------------
# 12. Drift consequence chain: a mismatched bbox under the same signature +
#     same native_resolution produces a different `canonical_grid_key` at the
#     tightest float boundary (+1e-13). This closes the "guard rejects" ⟹
#     "cell keys would drift" loop at the boundary the guard actually cares
#     about.
# -----------------------------------------------------------------------------


def test_drift_consequence_chain_bbox_mismatch_implies_different_canonical_grid_key() -> None:
    """At +1e-13, same-signature same-resolution bboxes derive different keys.

    Tightened from ``+0.001`` (which never probed the float boundary) to
    ``+1e-13`` so it actually cross-checks the guard boundary. Complements
    ``test_bbox_boundary_agreement_at_1e13_both_reject`` — this focuses on
    the SUB-4 consequence; that focuses on guard-vs-SUB-4 agreement.
    """
    grid_signature = _SHARED_SIGNATURE
    native_resolution = _SHARED_NATIVE_RESOLUTION
    registered_bbox = {
        "south": _PINNED_SOUTH,
        "north": _PINNED_NORTH,
        "west": _PINNED_WEST,
        "east": _PINNED_EAST,
    }
    perturbed_bbox = {
        "south": _PINNED_SOUTH + 1e-13,
        "north": _PINNED_NORTH,
        "west": _PINNED_WEST,
        "east": _PINNED_EAST,
    }
    key_registered = derive_canonical_grid_key(grid_signature, registered_bbox, native_resolution)
    key_perturbed = derive_canonical_grid_key(grid_signature, perturbed_bbox, native_resolution)
    assert key_registered != key_perturbed

    # Also assert the guard rejects at the same boundary — the dual-surface
    # link that makes "guard rejects" ⟺ "cell keys would drift" concrete.
    snapshot = _make_snapshot(south=_PINNED_SOUTH)
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH + 1e-13,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
    )
    with pytest.raises(BboxMismatchError):
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)


# -----------------------------------------------------------------------------
# 13. Exception hierarchy: BboxMismatchError is a RegistryStoreError. Callers
#     that catch RegistryStoreError alone must trap bbox mismatches too.
# -----------------------------------------------------------------------------


def test_bbox_mismatch_error_inherits_from_registry_store_error() -> None:
    """A single ``except RegistryStoreError`` trap covers bbox mismatches."""
    assert issubclass(BboxMismatchError, RegistryStoreError)
