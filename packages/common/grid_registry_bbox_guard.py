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

The comparison canonicalizes each bbox field via ``f"{x:.12f}"``. This forces
"bbox match" iff "same ``canonical_grid_key``": a ``1e-13``-perturbed env
bbox MUST match a registered value; a ``1e-11``-perturbed env bbox MUST raise.
"""

from __future__ import annotations

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


def _canonicalize_bbox_field(value: float) -> str:
    """Return the 12-decimal string used for bbox-equality comparison.

    Reuses the 12-decimal precision applied to ``native_resolution`` in
    ``packages.common.canonical_grid_key.derive_canonical_grid_key`` so
    "bbox match" iff "same ``canonical_grid_key``".
    """
    # See packages/common/canonical_grid_key.py::_validate_bbox and the
    # `f"{native_resolution:.12f}"` canonical form at line 68 of that module.
    return f"{float(value):.12f}"


def verify_download_bbox_matches_registry(
    registered_snapshot: RegisteredBboxSnapshotProtocol,
    *,
    env_reader: Callable[[], GeoBBox] = china_buffered_bbox_from_env,
) -> None:
    """Fail closed if the env bbox does not match the registered snapshot bbox.

    Reads the deployment env bbox through ``env_reader`` (default:
    :func:`china_buffered_bbox_from_env`) and compares each of the four
    corners ``{south, north, west, east}`` to the corresponding
    ``bbox_{south,north,west,east}`` attribute on ``registered_snapshot``
    using the pinned 12-decimal canonical form.

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
    ``None`` on a canonicalized field-by-field match.

    Raises
    ------
    BboxMismatchError
        On any field disagreement, carrying ``expected_bbox`` (env side),
        ``actual_bbox`` (snapshot side), and ``grid_snapshot_id``.
    ValueError
        Propagated unchanged from ``env_reader``: a malformed
        ``NHMS_DOWNLOAD_BBOX_*`` env var or an invalid :class:`GeoBBox`
        (e.g. latitude out of ``[-90, 90]``) surfaces the raw exception
        rather than being wrapped in ``BboxMismatchError``. This mirrors the
        ``ValueError`` propagation policy at
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

    for field in ("south", "north", "west", "east"):
        if _canonicalize_bbox_field(expected[field]) != _canonicalize_bbox_field(actual[field]):
            raise BboxMismatchError(
                expected_bbox=expected,
                actual_bbox=actual,
                grid_snapshot_id=registered_snapshot.grid_snapshot_id,
            )
    return None
