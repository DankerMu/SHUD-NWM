"""Fail-closed bbox consistency guard between deployment env and registered snapshot.

This module owns the single canonical comparison between the deployment env's
download bbox (read via :func:`workers.data_adapters.region.china_buffered_bbox_from_env`)
and a registered ``canonical_grid_snapshot`` row's bbox. It is called
identically by:

* the SUB-5 registry writer (``workers/grid_registry/registry.py``) as a
  registration-time pin, and
* the follow-up ``cmfd-direct-grid-platform-readiness`` producer preflight and
  platform-readiness checker.

The guard is a pure function with no side effects: no snapshot mutation, no
logging, no reads except through the injected ``env_reader`` and the four
``bbox_*`` attributes + ``grid_snapshot_id`` of the passed-in snapshot. A raw
:class:`ValueError` from a malformed env or an invalid :class:`GeoBBox` is
propagated unchanged, matching the ``normalize_source_id`` propagation
convention documented at ``packages/common/grid_registry_store.py:30-37``.

Compare semantic — mirrors SUB-4 exactly
----------------------------------------
The guard mirrors ``packages.common.canonical_grid_key.derive_canonical_grid_key``
bbox handling EXACTLY: bit-pattern float equality after ``math.isfinite``
rejection, no ``.12f`` truncation and no tolerance. Rationale:
:func:`derive_canonical_grid_key` at ``canonical_grid_key.py:59-73`` serializes
bbox floats via raw ``json.dumps`` (only ``native_resolution`` is truncated to
12 decimals), so any bit-different pair of bbox floats produces a different
``canonical_grid_key``. Applying ``.12f`` truncation on the guard side would
silently pass at the ``1e-13`` boundary where the two derived keys are already
distinct, breaking the pinned §3.2 invariant "bbox match ⟺ same
``canonical_grid_key``".

The compare uses IEEE-754 ``==`` PLUS a signed-zero disambiguation via
``math.copysign(1.0, x)`` on both sides. IEEE-754 says ``-0.0 == 0.0`` is
True, but SUB-4's ``json.dumps`` produces ``"-0.0" != "0.0"``, so plain
``==`` alone would silently pass a signed-zero divergence and break the ⟺
invariant. The sign-bit check catches this without introducing any string
formatting, keeping bbox match ⟺ same ``canonical_grid_key``.

Finiteness — matches SUB-4 policy
---------------------------------
Both env-side and snapshot-side values are rejected with a raw ``ValueError``
when ``not math.isfinite(value)``. This matches :func:`_validate_bbox` at
``canonical_grid_key.py:106-139`` where NaN/inf raise ``ValueError`` naming the
offending key. Without this gate, ``f"{nan:.12f}" == "nan"`` would degenerate
into silent-pass on the snapshot side.

Signed zero (``-0.0`` vs ``0.0``) — DEFERRED joint normalization
---------------------------------------------------------------
Signed zero is treated as DIFFERENT here to preserve the ⟺ invariant with
SUB-4, which differentiates via ``json.dumps("-0.0") != json.dumps("0.0")``.
A single-side normalization on the guard would BREAK the invariant this
module exists to preserve. A jointly-normalizing follow-up openspec change
(guard + SUB-4 ``_validate_bbox`` normalizing via ``float(v) + 0.0``) can
revisit signed-zero handling if operator experience with Prime-Meridian
spellings warrants.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from packages.common.grid_registry_store import RegistryStoreError
from workers.data_adapters.region import GeoBBox, china_buffered_bbox_from_env

__all__ = [
    "BboxMismatchError",
    "RegisteredBboxSnapshotProtocol",
    "verify_download_bbox_matches_registry",
]


@runtime_checkable
class RegisteredBboxSnapshotProtocol(Protocol):
    """Structural type for the four bbox corners + ``grid_snapshot_id``.

    Duck-compatible with
    :class:`packages.common.grid_registry_store.CanonicalGridSnapshot` but does
    NOT import it, so this module has no runtime dependency on SUB-3's
    dataclass shape. Marked ``@runtime_checkable`` so tests may
    ``isinstance()`` against it to prove Protocol compatibility with
    ``CanonicalGridSnapshot`` without hard-importing the dataclass here.
    """

    bbox_south: float
    bbox_north: float
    bbox_west: float
    bbox_east: float
    grid_snapshot_id: uuid.UUID | None


class BboxMismatchError(RegistryStoreError):
    """Raised when the env bbox and the registered snapshot bbox disagree.

    Inherits from :class:`RegistryStoreError` so bbox-consistency failures live
    in the same taxonomy as ``RegistryImmutabilityError`` /
    ``RegistryChecksumError`` (post-registration integrity errors), rather
    than ``GridSnapshotInputError`` which is reserved for SUB-4 sidecar parse
    failures per the pinned §3.1a contract.
    """

    def __init__(
        self,
        *,
        expected_bbox: dict[str, float],
        actual_bbox: dict[str, float],
        grid_snapshot_id: uuid.UUID | None,
    ) -> None:
        self.expected_bbox = expected_bbox
        self.actual_bbox = actual_bbox
        self.grid_snapshot_id = grid_snapshot_id
        super().__init__(
            f"Bbox mismatch (grid_snapshot_id={grid_snapshot_id}): "
            f"expected={expected_bbox}, actual={actual_bbox}"
        )


def _reject_non_finite(field: str, value: float, side: str) -> None:
    """Raise ``ValueError`` if ``value`` is NaN or inf.

    Mirrors the SUB-4 :func:`packages.common.canonical_grid_key._validate_bbox`
    policy at ``canonical_grid_key.py:106-139`` where NaN/inf raise
    ``ValueError`` naming the offending key. Both env-side and snapshot-side
    values are gated so a corrupted snapshot NaN cannot silently degenerate
    through ``f"{nan:.12f}" == "nan"`` string-compare.
    """
    if not math.isfinite(value):
        raise ValueError(
            f"bbox {field} on {side} side must be finite: got {value!r}"
        )


def verify_download_bbox_matches_registry(
    registered_snapshot: RegisteredBboxSnapshotProtocol,
    *,
    env_reader: Callable[[], GeoBBox] = china_buffered_bbox_from_env,
) -> None:
    """Fail closed if the env bbox does not match the registered snapshot bbox.

    Reads the deployment env bbox through ``env_reader`` (default:
    :func:`china_buffered_bbox_from_env`) and compares each of the four
    corners ``{south, north, west, east}`` to the corresponding
    ``bbox_{south,north,west,east}`` attribute on ``registered_snapshot`` via
    exact IEEE-754 float equality after a finiteness gate on both sides.

    Parameters
    ----------
    registered_snapshot:
        Any object satisfying :class:`RegisteredBboxSnapshotProtocol`. Only
        the four ``bbox_*`` floats and ``grid_snapshot_id`` are read; the
        snapshot is not mutated.
    env_reader:
        Keyword-only, defaults to
        :func:`workers.data_adapters.region.china_buffered_bbox_from_env`.
        The default is the pinned entry point so a future ad-hoc lambda
        default would be a signature drift.

    Returns
    -------
    ``None`` on a field-by-field exact-equality match.

    Raises
    ------
    BboxMismatchError
        On any field disagreement, carrying ``expected_bbox`` (env side),
        ``actual_bbox`` (snapshot side), and ``grid_snapshot_id``.
    ValueError
        Two disjoint sources: (a) propagated unchanged from ``env_reader``
        (a malformed ``NHMS_DOWNLOAD_BBOX_*`` env var or an invalid
        :class:`GeoBBox`), and (b) raised locally when either env-side or
        snapshot-side bbox value is not finite. Non-finite values are a
        shape-integrity failure, not a mismatch, so they surface as raw
        ``ValueError`` rather than being wrapped in ``BboxMismatchError``.
        Mirrors the ``ValueError`` propagation policy at
        ``packages/common/grid_registry_store.py:30-37``.
    """
    env_bbox = env_reader()

    expected = {
        "south": env_bbox.south,
        "north": env_bbox.north,
        "west": env_bbox.west,
        "east": env_bbox.east,
    }
    actual = {
        "south": registered_snapshot.bbox_south,
        "north": registered_snapshot.bbox_north,
        "west": registered_snapshot.bbox_west,
        "east": registered_snapshot.bbox_east,
    }

    # Finiteness gate FIRST. A corrupted snapshot NaN must raise ValueError
    # rather than silently passing through `f"{nan:.12f}" == "nan"` degeneracy.
    for field in ("south", "north", "west", "east"):
        _reject_non_finite(field, expected[field], "env")
        _reject_non_finite(field, actual[field], "snapshot")

    # Bit-pattern float equality — no format, no round, no tolerance.
    # Mirrors SUB-4 `derive_canonical_grid_key` which serializes bbox floats
    # via raw json.dumps (no .12f truncation), so any bit-different pair of
    # bbox floats produces a different canonical_grid_key.
    #
    # IEEE-754 `==` treats `-0.0 == 0.0` as True, but SUB-4's json.dumps
    # produces "-0.0" != "0.0". To preserve the ⟺ invariant, we augment
    # `==` with a `math.copysign` sign-bit check on both sides so signed
    # zero is treated as distinct (matching SUB-4's json.dumps behavior).
    # A jointly-normalizing follow-up openspec change can revisit signed
    # zero on BOTH surfaces at once.
    for field in ("south", "north", "west", "east"):
        exp_v = expected[field]
        act_v = actual[field]
        if exp_v != act_v or math.copysign(1.0, exp_v) != math.copysign(1.0, act_v):
            raise BboxMismatchError(
                expected_bbox=expected,
                actual_bbox=actual,
                grid_snapshot_id=registered_snapshot.grid_snapshot_id,
            )
    return None
