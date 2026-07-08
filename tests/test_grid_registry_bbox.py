"""Tests for packages/common/grid_registry_bbox_guard.py.

Covers issue #903 (Epic #897 SUB-6) Task 3.2 Evidence Floor:

* matching env vs snapshot bbox returns ``None`` silently,
* per-field mismatch fails closed with ``BboxMismatchError`` carrying both
  ``expected_bbox`` and ``actual_bbox`` dicts + ``grid_snapshot_id``,
* ``1e-13`` perturbation passes; ``1e-11`` perturbation raises,
* raw ``ValueError`` from a malformed env or an invalid ``GeoBBox`` propagates
  unwrapped (NOT re-raised as ``BboxMismatchError``),
* the guard has no side effects on the snapshot (``copy.deepcopy`` compare),
* the guard's signature is exactly two parameters (``registered_snapshot`` +
  keyword-only ``env_reader``); a third parameter is a hard fail,
* the ``RegisteredBboxSnapshotProtocol`` accepts a real
  :class:`CanonicalGridSnapshot` via ``isinstance``,
* drift consequence chain: a bbox mismatch under the same signature +
  same ``native_resolution`` produces a different ``canonical_grid_key``.

All tests use a lightweight ``_FakeSnapshot`` dataclass that satisfies the
Protocol via structural subtyping; only the Protocol-compat test constructs a
real ``CanonicalGridSnapshot``.
"""

from __future__ import annotations

import copy
import inspect
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
# 3. Tolerance boundary at 12 decimals: 1e-13 passes, 1e-11 rejects.
# -----------------------------------------------------------------------------


def test_bbox_matches_within_1e13_perturbation() -> None:
    """Env south = registered + 1e-13 rounds to same 12-decimal form; passes."""
    snapshot = _make_snapshot(south=_PINNED_SOUTH)
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH + 1e-13,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
    )
    assert verify_download_bbox_matches_registry(snapshot, env_reader=reader) is None


def test_bbox_rejects_1e11_perturbation() -> None:
    """Env south = registered + 1e-11 differs at the 12th decimal; rejects."""
    snapshot = _make_snapshot(south=_PINNED_SOUTH)
    reader = _reader_returning(
        GeoBBox(
            south=_PINNED_SOUTH + 1e-11,
            north=_PINNED_NORTH,
            west=_PINNED_WEST,
            east=_PINNED_EAST,
        )
    )
    with pytest.raises(BboxMismatchError) as excinfo:
        verify_download_bbox_matches_registry(snapshot, env_reader=reader)
    err = excinfo.value
    # The canonicalized 12-decimal strings must distinguish the two values so
    # the offending field is recoverable from the dicts.
    assert f"{err.expected_bbox['south']:.12f}" != f"{err.actual_bbox['south']:.12f}"


# -----------------------------------------------------------------------------
# 4. Env-reader failure propagation: raw ValueError, not BboxMismatchError.
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
# 5. Error attributes carry grid_snapshot_id (both real UUID and None).
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
# 6. Side-effect-free guarantee (deepcopy compare, both match + mismatch case).
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
# 7. Signature contract: exactly two parameters, env_reader keyword-only,
#    default is `china_buffered_bbox_from_env` (not an ad-hoc lambda).
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
# 8. Protocol compatibility with the real CanonicalGridSnapshot dataclass.
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
        grid_signature="6c008901b8b7" + "0" * 52,
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
# 9. Drift consequence chain: a mismatched bbox under the same signature +
#     same native_resolution produces a different `canonical_grid_key`. This
#     closes the "guard rejects" ⟹ "cell keys would drift" loop.
# -----------------------------------------------------------------------------


def test_drift_consequence_chain_bbox_mismatch_implies_different_canonical_grid_key() -> None:
    """Two same-signature bboxes differing by 0.001 in south derive different keys."""
    grid_signature = "6c008901b8b7" + "0" * 52
    native_resolution = 0.25
    registered_bbox = {
        "south": _PINNED_SOUTH,
        "north": _PINNED_NORTH,
        "west": _PINNED_WEST,
        "east": _PINNED_EAST,
    }
    perturbed_bbox = {
        "south": _PINNED_SOUTH + 0.001,
        "north": _PINNED_NORTH,
        "west": _PINNED_WEST,
        "east": _PINNED_EAST,
    }
    key_registered = derive_canonical_grid_key(grid_signature, registered_bbox, native_resolution)
    key_perturbed = derive_canonical_grid_key(grid_signature, perturbed_bbox, native_resolution)
    assert key_registered != key_perturbed


# -----------------------------------------------------------------------------
# 10. Exception hierarchy: BboxMismatchError is a RegistryStoreError. Callers
#     that catch RegistryStoreError alone must trap bbox mismatches too.
# -----------------------------------------------------------------------------


def test_bbox_mismatch_error_inherits_from_registry_store_error() -> None:
    """A single ``except RegistryStoreError`` trap covers bbox mismatches."""
    assert issubclass(BboxMismatchError, RegistryStoreError)
