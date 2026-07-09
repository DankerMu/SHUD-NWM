"""In-memory ``GridSnapshot`` loader fixture for mapping-builder tests.

Reusable helpers for constructing ``CanonicalGridSnapshot`` + cell rows and a
tiny ``GridSnapshotLoader``-satisfying loader without touching a real DB.

Design contract
---------------

* Snapshot's ``grid_signature`` is computed via the SHARED helper
  :func:`packages.common.grid_signature.grid_signature_hash` on the ordered
  cells. Tests that check "shared helper is the authority" pin invocation
  against this same helper.
* ``bbox_*`` fields default to a tight axis-aligned bounding box around the
  supplied cells, with an optional pad so tests can construct barycenters
  intentionally inside or outside the bbox by picking coordinates.
* ``canonical_ordinal`` is assigned ``1..N`` in cell insertion order.

The loader itself intentionally implements only
``find_snapshot_by_identity`` — the algorithm module's Protocol needs no
other method, and adding methods would risk drift with the real DB store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from packages.common.canonical_grid_key import derive_canonical_grid_key
from packages.common.grid_registry_store import (
    CanonicalGridCell,
    CanonicalGridSnapshot,
)
from packages.common.grid_signature import grid_signature_hash


def make_regular_grid_cells(
    *,
    lon0: float,
    lat0: float,
    lon_step: float,
    lat_step: float,
    lon_count: int,
    lat_count: int,
) -> list[CanonicalGridCell]:
    """Return ``lon_count * lat_count`` cells on a regular lat/lon grid.

    ``canonical_ordinal`` runs ``1..N`` in ``(lat_index * lon_count + lon_index + 1)``
    order (lon-major within each lat row, lat-major overall). ``grid_cell_id``
    is ``str(index)`` matching the producer-side 0-based iteration used by
    :mod:`workers.forcing_producer.producer`.

    Cells are returned as a fresh list so the caller can mutate or reorder
    them for negative-path tests without touching this factory's state.
    """
    if lon_count <= 0 or lat_count <= 0:
        raise ValueError(
            f"lon_count and lat_count must be positive, got {lon_count=}, {lat_count=}"
        )
    cells: list[CanonicalGridCell] = []
    index = 0
    for lat_index in range(lat_count):
        lat = lat0 + lat_index * lat_step
        for lon_index in range(lon_count):
            lon = lon0 + lon_index * lon_step
            cells.append(
                CanonicalGridCell(
                    grid_cell_id=str(index),
                    longitude=float(lon),
                    latitude=float(lat),
                    canonical_ordinal=index + 1,
                )
            )
            index += 1
    return cells


def make_snapshot(
    *,
    source_id: str,
    grid_id: str,
    cells: list[CanonicalGridCell],
    bbox_pad: float = 0.0,
    grid_signature_override: str | None = None,
    bbox_override: tuple[float, float, float, float] | None = None,
    native_resolution: float | None = None,
) -> CanonicalGridSnapshot:
    """Return a :class:`CanonicalGridSnapshot` matching the supplied cells.

    Signature is computed via the SHARED helper unless
    ``grid_signature_override`` is supplied (negative-path tests use this to
    stage a tampered signature). ``bbox_*`` is derived from cell lon/lat
    ranges with the given ``bbox_pad`` applied on all four edges, unless
    ``bbox_override`` is supplied. ``native_resolution`` defaults to the
    smallest positive lon step observed across cells (or 0.0 for degenerate
    single-cell fixtures).

    The snapshot's ``canonical_grid_key`` is derived via the production
    helper :func:`packages.common.canonical_grid_key.derive_canonical_grid_key`
    so tests do not silently invent an incompatible key.
    """
    if not cells:
        raise ValueError("cannot build snapshot from an empty cell list")

    if grid_signature_override is None:
        grid_signature = grid_signature_hash(cells)
    else:
        grid_signature = grid_signature_override

    if bbox_override is None:
        lons = [float(c.longitude) for c in cells]
        lats = [float(c.latitude) for c in cells]
        south = min(lats) - bbox_pad
        north = max(lats) + bbox_pad
        west = min(lons) - bbox_pad
        east = max(lons) + bbox_pad
    else:
        south, north, west, east = bbox_override

    if native_resolution is None:
        sorted_lons = sorted({round(float(c.longitude), 12) for c in cells})
        if len(sorted_lons) >= 2:
            native_resolution = sorted_lons[1] - sorted_lons[0]
        else:
            sorted_lats = sorted({round(float(c.latitude), 12) for c in cells})
            if len(sorted_lats) >= 2:
                native_resolution = sorted_lats[1] - sorted_lats[0]
            else:
                native_resolution = 0.0

    # Guard against zero native_resolution which the canonical-grid-key
    # helper rejects (a truly degenerate 1-cell test snapshot still needs a
    # positive value); we pick a tiny sentinel that does not change the
    # snapshot's semantic identity.
    resolution_for_key = float(native_resolution)
    if resolution_for_key <= 0.0:
        resolution_for_key = 1.0e-9

    canonical_grid_key = derive_canonical_grid_key(
        grid_signature=grid_signature,
        bbox={
            "south": float(south),
            "north": float(north),
            "west": float(west),
            "east": float(east),
        },
        native_resolution=resolution_for_key,
    )

    return CanonicalGridSnapshot(
        grid_snapshot_id=UUID(int=1),
        canonical_grid_key=canonical_grid_key,
        source_id=source_id,
        grid_id=grid_id,
        grid_signature=grid_signature,
        grid_definition_uri="memory://tests/mapping_builder/fixture",
        grid_definition_checksum="0" * 64,
        longitude_convention="pm180",
        latitude_order="south_to_north",
        flatten_order="lat_major",
        native_resolution=float(native_resolution),
        bbox_south=float(south),
        bbox_north=float(north),
        bbox_west=float(west),
        bbox_east=float(east),
        converter_version="test",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_to=None,
        applicable_source_ids=(source_id,),
    )


class InMemoryGridSnapshotLoader:
    """Test-only implementation of :class:`workers.mapping_builder.GridSnapshotLoader`.

    Populated by the test with a single ``(source_id, grid_id)`` identity +
    matching snapshot + cells. Any lookup with a different identity returns
    ``None`` (proving the unregistered-identity fail-closed path).
    """

    def __init__(
        self,
        *,
        source_id: str,
        grid_id: str,
        snapshot: CanonicalGridSnapshot,
        cells: list[CanonicalGridCell],
    ) -> None:
        self._source_id = source_id
        self._grid_id = grid_id
        self._snapshot = snapshot
        self._cells = list(cells)
        self.call_log: list[tuple[str, str]] = []

    def find_snapshot_by_identity(
        self, source_id: str, grid_id: str
    ) -> tuple[CanonicalGridSnapshot, list[CanonicalGridCell]] | None:
        """Return ``(snapshot, cells)`` iff the identity matches; else ``None``."""
        self.call_log.append((source_id, grid_id))
        if source_id == self._source_id and grid_id == self._grid_id:
            return self._snapshot, list(self._cells)
        return None
