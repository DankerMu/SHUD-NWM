"""G2 grid-identity precondition + nearest-cell mapping algorithm.

This module implements OpenSpec change ``forcing-mapping-asset-build`` §2.0 and
§2.1 (Epic #909 SUB-5). Two public entry points:

* :func:`verify_grid_identity_precondition` — Gate G2 precondition (§2.0):
  loads the registered grid snapshot from ``canonical-source-grid-registry``
  by ``(source_id, grid_id)``, recomputes ``grid_signature`` via the shared
  helper :func:`packages.common.grid_signature.grid_signature_hash`, asserts
  equality with the snapshot's stored value, and asserts every element
  barycenter (WGS84) lies inside the snapshot coverage bbox. Fail-closed on
  any violation.
* :func:`nearest_cell_barycenter_geodesic_v1` — the versioned mapping
  algorithm (§2.1): reads mesh geometry via
  :func:`workers.mapping_builder.integrity.read_sp_mesh_geometry`, transforms
  barycenters ``((x1+x2+x3)/3, (y1+y2+y3)/3)`` in the package CRS to WGS84
  via the ``Transformer`` built from the checksum-bound ``gis/*.prj`` WKT,
  then selects the nearest registered cell by :func:`pyproj.Geod.inv`
  geodesic distance. Ties, candidate count, and geodesic distance are
  recorded per element.

Regular lat/lon grid fast path
------------------------------
When the loaded snapshot's cells form a regular lat/lon grid (unique lons ×
unique lats == cell count and both axes have uniform step),
:func:`nearest_cell_barycenter_geodesic_v1` additionally computes each
element's cell via independent lon/lat rounding and cross-checks it against
the geodesic pick. Any divergence raises :class:`RegularGridFastPathParityError`
before the algorithm returns a result — the fast path is a defense-in-depth
invariant, never a silent shortcut around the geodesic definition.

Fail-closed guarantee
---------------------
Every raise in this module happens BEFORE the function returns; no partial
output artifact escapes. The versioned algorithm identifier
:data:`algorithm_id` MUST NOT change without a new version suffix (spec §2.1
"distance definition, tie-break, index order, and coordinate precision do
not change without a new version identifier").

Store dependency
----------------
The DB-side snapshot lookup is abstracted through the
:class:`GridSnapshotLoader` Protocol so tests may substitute an in-memory
loader (see ``tests/fixtures/mapping_builder/in_memory_grid_snapshot.py``).
Production wiring MUST supply a concrete loader that returns
``(CanonicalGridSnapshot, list[CanonicalGridCell]) | None`` for a given
``(source_id, grid_id)`` identity — this protocol is intentionally narrower
than the SUB-5-writer-scoped
:meth:`packages.common.grid_registry_store.PsycopgGridRegistryStore.find_snapshot_by_identity`
(which returns only a ``UUID`` for idempotency checks); the mapping builder
needs the full snapshot + cell rows to reach the shared signature helper.
"""

from __future__ import annotations

import math
import pathlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import pyproj

from packages.common.grid_registry_store import (
    CanonicalGridCell,
    CanonicalGridSnapshot,
)
from packages.common.grid_signature import grid_signature_hash
from workers.mapping_builder.integrity import (
    read_sp_mesh_geometry,
    verify_package_crs,
)

#: Versioned identifier of the mapping algorithm. Distance definition,
#: tie-break, index order, and coordinate precision are pinned to this string;
#: changing any of them requires a new suffix (spec §2.1).
algorithm_id: str = "nearest_cell_barycenter_geodesic_v1"

#: Uniform-step tolerance used when deciding whether a set of unique lon/lat
#: values forms a regular grid axis. Matches the 12-decimal rounding used by
#: :func:`packages.common.grid_signature.grid_signature_tuples` and the
#: ``_has_uniform_step`` heuristic in :mod:`workers.mapping_builder.integrity`,
#: so a snapshot whose cells the shared signature helper treats as identical
#: axis anchors is treated the same way here.
_REGULAR_GRID_STEP_TOLERANCE: float = 1e-6

#: Geodesic-distance tolerance (meters) for detecting ties at the min distance.
#: Distances below this threshold are treated as equal; the spec §2.1 records
#: tie status alongside geodesic distance, and §2.2 (a later sibling task)
#: pins the tolerance for the tie-BREAK rule. This value is intentionally
#: small — it only classifies exact-repeat coordinates as tied.
_GEODESIC_TIE_TOLERANCE_M: float = 1.0e-6


class GridSnapshotLoader(Protocol):
    """Structural protocol for the grid-snapshot lookup used by G2.

    Any object exposing ``find_snapshot_by_identity(source_id, grid_id)``
    returning ``(CanonicalGridSnapshot, list[CanonicalGridCell])`` or
    ``None`` satisfies this protocol. Tests use an in-memory loader; the
    production wiring is a thin adapter over the DB store.
    """

    def find_snapshot_by_identity(
        self, source_id: str, grid_id: str
    ) -> tuple[CanonicalGridSnapshot, list[CanonicalGridCell]] | None: ...


# --- error hierarchy ------------------------------------------------------


class MappingAlgorithmError(Exception):
    """Base class for §2.0 / §2.1 mapping-algorithm failures.

    Distinct from :class:`workers.mapping_builder.integrity.BaselineIntegrityError`
    because G2 / ownership failures come from a different oracle (the grid
    registry + WGS84 coverage) than G0 / G1 (the baseline package on disk).
    Sharing a base class would let a caller silently absorb both families
    with one ``except`` clause; the mapping builder's fail-closed guarantee
    is meaningful only when callers can tell them apart.
    """


class UnregisteredGridSnapshotError(MappingAlgorithmError):
    """Raised when ``(source_id, grid_id)`` has no registered snapshot.

    The mapping builder MUST NOT invent a snapshot; per spec §"Grid snapshot
    is loaded from the registry by (source_id, grid_id)", an absent snapshot
    is a G2 blocker.
    """

    def __init__(self, source_id: str, grid_id: str) -> None:
        super().__init__(
            f"no registered grid snapshot for identity source_id={source_id!r} "
            f"grid_id={grid_id!r}"
        )
        self.source_id = source_id
        self.grid_id = grid_id


class GridSignatureMismatchError(MappingAlgorithmError):
    """Raised when the recomputed signature disagrees with the stored value.

    Per spec §"grid_signature is recomputed via the shared helper and matches
    the registered value": the recomputation MUST use the shared helper and
    MUST equal the snapshot's stored signature. Any drift is a G2 blocker.
    """

    def __init__(
        self,
        source_id: str,
        grid_id: str,
        expected: str,
        recomputed: str,
    ) -> None:
        super().__init__(
            f"grid_signature mismatch for source_id={source_id!r} "
            f"grid_id={grid_id!r}: stored={expected!r} recomputed={recomputed!r}"
        )
        self.source_id = source_id
        self.grid_id = grid_id
        self.expected = expected
        self.recomputed = recomputed


class ElementBarycenterOutOfCoverageError(MappingAlgorithmError):
    """Raised when an element barycenter falls outside the snapshot bbox.

    Per spec §"Basin lies fully inside the registered grid coverage": the
    builder MUST fail closed with no output when any element barycenter is
    outside the registered coverage bbox. The mapping builder never silently
    crops uncovered elements.
    """

    def __init__(
        self,
        element_id: int,
        barycenter_lon: float,
        barycenter_lat: float,
        bbox: tuple[float, float, float, float],
    ) -> None:
        # bbox = (south, north, west, east)
        south, north, west, east = bbox
        super().__init__(
            f"element_id={element_id} barycenter "
            f"(lon={barycenter_lon!r}, lat={barycenter_lat!r}) lies outside "
            f"registered coverage bbox "
            f"(south={south!r}, north={north!r}, west={west!r}, east={east!r})"
        )
        self.element_id = element_id
        self.barycenter_lon = barycenter_lon
        self.barycenter_lat = barycenter_lat
        self.bbox = bbox


class RegularGridFastPathParityError(MappingAlgorithmError):
    """Raised when the regular-grid fast path diverges from the geodesic pick.

    The fast path (independent lon/lat rounding) is defense-in-depth — it
    MUST agree with the geodesic definition on every element for the loaded
    snapshot. A divergence means the algorithm's fast path is buggy or the
    snapshot's cells are not truly regular; either is a fail-closed blocker.
    """

    def __init__(
        self,
        element_id: int,
        geodesic_grid_cell_id: str,
        fast_path_grid_cell_id: str,
    ) -> None:
        super().__init__(
            f"element_id={element_id} regular-grid fast path picked "
            f"grid_cell_id={fast_path_grid_cell_id!r} but geodesic picked "
            f"{geodesic_grid_cell_id!r}"
        )
        self.element_id = element_id
        self.geodesic_grid_cell_id = geodesic_grid_cell_id
        self.fast_path_grid_cell_id = fast_path_grid_cell_id


# --- result records -------------------------------------------------------


@dataclass(frozen=True)
class ElementOwnership:
    """One row of the §2.1 per-element ownership record.

    Fields are immutable so downstream evidence can bind the result byte-
    for-byte. ``tie_status`` is ``"unique"`` when exactly one cell is at the
    minimum geodesic distance, else ``"tied_with_N"`` where ``N`` is the
    number of cells within :data:`_GEODESIC_TIE_TOLERANCE_M` of that minimum
    (including the picked cell — so ``N >= 2``). Tie-BREAK by smallest
    canonical ordinal is a §2.2 responsibility; §2.1 only reports the tie
    status.
    """

    element_id: int
    grid_cell_id: str
    canonical_ordinal: int
    geodesic_distance_m: float
    tie_status: str  # "unique" | "tied_with_N"
    candidate_count: int


# --- G2 precondition (§2.0) -----------------------------------------------


def verify_grid_identity_precondition(
    source_id: str,
    grid_id: str,
    barycenters_wgs84: Sequence[tuple[int, float, float]],
    store: GridSnapshotLoader,
) -> tuple[CanonicalGridSnapshot, tuple[CanonicalGridCell, ...]]:
    """Verify the G2 grid-identity precondition and return the loaded snapshot.

    Executes the three §2.0 subchecks in order and fail-closed:

    1. ``store.find_snapshot_by_identity(source_id, grid_id)`` MUST return a
       non-``None`` ``(snapshot, cells)`` pair; else raises
       :class:`UnregisteredGridSnapshotError`.
    2. :func:`packages.common.grid_signature.grid_signature_hash` (the SOLE
       signature authority) is invoked over the loaded cells; its result
       MUST equal ``snapshot.grid_signature``; else raises
       :class:`GridSignatureMismatchError`.
    3. Every ``(element_id, lon, lat)`` in ``barycenters_wgs84`` MUST lie
       inside the snapshot's bbox (inclusive endpoints); the first violation
       raises :class:`ElementBarycenterOutOfCoverageError`.

    Parameters
    ----------
    source_id, grid_id:
        Identity keys passed through to ``store.find_snapshot_by_identity``.
    barycenters_wgs84:
        Sequence of ``(element_id, longitude, latitude)`` in WGS84 degrees.
        Order is caller-owned; the first out-of-bbox violation raises with
        that element's id.
    store:
        Any object satisfying :class:`GridSnapshotLoader`.

    Returns
    -------
    tuple
        ``(snapshot, cells_tuple)`` — the loaded snapshot record and its
        ordered cells as a tuple (immutable) so the caller can iterate the
        same rows the signature helper hashed.

    Raises
    ------
    UnregisteredGridSnapshotError
        No snapshot registered for the identity pair.
    GridSignatureMismatchError
        Recomputed signature differs from the stored value.
    ElementBarycenterOutOfCoverageError
        An element barycenter falls outside the snapshot coverage bbox.
    """
    lookup = store.find_snapshot_by_identity(source_id, grid_id)
    if lookup is None:
        raise UnregisteredGridSnapshotError(source_id=source_id, grid_id=grid_id)
    snapshot, cells = lookup

    # Shared helper is the SOLE signature authority (never hand-rolled).
    recomputed = grid_signature_hash(cells)
    if recomputed != snapshot.grid_signature:
        raise GridSignatureMismatchError(
            source_id=source_id,
            grid_id=grid_id,
            expected=snapshot.grid_signature,
            recomputed=recomputed,
        )

    bbox = (
        float(snapshot.bbox_south),
        float(snapshot.bbox_north),
        float(snapshot.bbox_west),
        float(snapshot.bbox_east),
    )
    south, north, west, east = bbox
    for element_id, lon, lat in barycenters_wgs84:
        if not (south <= lat <= north and west <= lon <= east):
            raise ElementBarycenterOutOfCoverageError(
                element_id=element_id,
                barycenter_lon=lon,
                barycenter_lat=lat,
                bbox=bbox,
            )

    return snapshot, tuple(cells)


# --- barycenter helpers ---------------------------------------------------


def _compute_element_barycenters_package_crs(
    elements: Sequence[tuple[int, int, int, int]],
    node_xy: dict[int, tuple[float, float]],
) -> tuple[tuple[int, float, float], ...]:
    """Return ``((element_id, x_mean, y_mean), ...)`` in the package CRS.

    ``x_mean, y_mean`` are the arithmetic mean of the three vertex X/Y
    coordinates (spec §"Element representative point is the mesh barycenter"
    — ``(v1+v2+v3)/3``). Missing node ids raise :class:`KeyError`; upstream
    G1 validation should have caught them, but we do not silently substitute
    a default here.
    """
    result: list[tuple[int, float, float]] = []
    for element_id, v1, v2, v3 in elements:
        x1, y1 = node_xy[v1]
        x2, y2 = node_xy[v2]
        x3, y3 = node_xy[v3]
        result.append(
            (
                element_id,
                (x1 + x2 + x3) / 3.0,
                (y1 + y2 + y3) / 3.0,
            )
        )
    return tuple(result)


def _transform_barycenters_to_wgs84(
    barycenters_pkg: Sequence[tuple[int, float, float]],
    transformer: pyproj.Transformer,
) -> tuple[tuple[int, float, float], ...]:
    """Transform ``(element_id, x, y)`` pkg-CRS points to ``(element_id, lon, lat)``.

    ``transformer`` MUST be built with ``always_xy=True`` so the (x, y) input
    order is preserved regardless of the target CRS's canonical axis order.
    """
    result: list[tuple[int, float, float]] = []
    for element_id, x, y in barycenters_pkg:
        lon, lat = transformer.transform(x, y)
        result.append((element_id, float(lon), float(lat)))
    return tuple(result)


# --- regular-grid fast path -----------------------------------------------


@dataclass(frozen=True)
class _RegularGridStructure:
    """Sorted unique lons/lats + step + cell index for the fast path."""

    unique_lons: tuple[float, ...]
    unique_lats: tuple[float, ...]
    lon_step: float
    lat_step: float
    cell_by_coord: dict[tuple[float, float], CanonicalGridCell]


def _detect_regular_grid(
    cells: Sequence[CanonicalGridCell],
) -> _RegularGridStructure | None:
    """Return a :class:`_RegularGridStructure` iff ``cells`` form a regular grid.

    Rule (mirrors the RECORD-ONLY heuristic in
    :func:`workers.mapping_builder.integrity._looks_like_regular_grid` but
    scoped to CanonicalGridCell inputs): the unique lons and unique lats
    each have uniform step within :data:`_REGULAR_GRID_STEP_TOLERANCE`, AND
    ``len(unique_lons) * len(unique_lats) == len(cells)``. Cells with
    duplicate (lon, lat) coordinates fail the count check and are therefore
    not classified as regular.
    """
    if not cells:
        return None
    unique_lons = sorted({round(float(c.longitude), 12) for c in cells})
    unique_lats = sorted({round(float(c.latitude), 12) for c in cells})
    if len(unique_lons) * len(unique_lats) != len(cells):
        return None
    lon_step = _uniform_step(unique_lons)
    if lon_step is None:
        return None
    lat_step = _uniform_step(unique_lats)
    if lat_step is None:
        return None
    cell_by_coord: dict[tuple[float, float], CanonicalGridCell] = {}
    for cell in cells:
        key = (round(float(cell.longitude), 12), round(float(cell.latitude), 12))
        if key in cell_by_coord:
            # Duplicate coord — cannot form a regular grid unambiguously.
            return None
        cell_by_coord[key] = cell
    return _RegularGridStructure(
        unique_lons=tuple(unique_lons),
        unique_lats=tuple(unique_lats),
        lon_step=lon_step,
        lat_step=lat_step,
        cell_by_coord=cell_by_coord,
    )


def _uniform_step(sorted_values: list[float]) -> float | None:
    """Return the uniform step of ``sorted_values`` or ``None`` if non-uniform.

    A single-value axis returns ``0.0`` (degenerate but permitted — a 1×N
    grid still passes the fast path when only one lat or lon is present).
    """
    if len(sorted_values) < 2:
        return 0.0
    steps = [sorted_values[i + 1] - sorted_values[i] for i in range(len(sorted_values) - 1)]
    first = steps[0]
    for step in steps[1:]:
        if abs(step - first) > _REGULAR_GRID_STEP_TOLERANCE:
            return None
    return float(first)


def _select_nearest_axis_value(
    query: float,
    sorted_values: tuple[float, ...],
) -> float:
    """Return the sorted-axis value nearest to ``query`` (min-value tie-break).

    Iterating in sorted order + strict ``<`` comparison ensures the smallest
    axis value wins on a tie, which pairs with canonical-ordinal-min tie
    break at the cell level.
    """
    best = sorted_values[0]
    best_dist = abs(query - best)
    for value in sorted_values[1:]:
        dist = abs(query - value)
        if dist < best_dist:
            best = value
            best_dist = dist
    return best


def _select_cell_by_lonlat_round(
    lon: float,
    lat: float,
    structure: _RegularGridStructure,
) -> CanonicalGridCell:
    """Fast path: independent lon/lat rounding to the nearest axis value.

    Returns the :class:`CanonicalGridCell` at the closest ``(unique_lon,
    unique_lat)`` pair. Cross-checked against the geodesic pick by
    :func:`nearest_cell_barycenter_geodesic_v1` when this path is taken.
    """
    nearest_lon = _select_nearest_axis_value(lon, structure.unique_lons)
    nearest_lat = _select_nearest_axis_value(lat, structure.unique_lats)
    return structure.cell_by_coord[(nearest_lon, nearest_lat)]


# --- geodesic O(M) scan ---------------------------------------------------


def _select_nearest_cell_geodesic(
    lon: float,
    lat: float,
    cells: Sequence[CanonicalGridCell],
    geod: pyproj.Geod,
) -> tuple[CanonicalGridCell, float, str, int]:
    """Return ``(cell, geodesic_distance_m, tie_status, candidate_count)``.

    Iterates ``cells`` in the caller-supplied order (spec §2.1 canonical-
    ordinal order — the caller feeds sorted cells). Ties (distances within
    :data:`_GEODESIC_TIE_TOLERANCE_M` of the running minimum) are counted
    but not broken here; §2.2 owns the smallest-canonical-ordinal tie-break.
    Iteration order + strict ``<`` comparison implicitly picks the FIRST
    cell at the min distance, which is the canonical-ordinal-smallest cell
    when the caller sorts by canonical_ordinal.

    ``tie_status`` is ``"unique"`` when exactly one cell is within the tie
    tolerance of the min distance; else ``f"tied_with_{N}"`` where ``N`` is
    the number of tied cells (including the picked one, so ``N >= 2``).
    ``candidate_count`` is the total number of cells scanned.
    """
    best_cell: CanonicalGridCell | None = None
    best_distance = math.inf
    for cell in cells:
        # pyproj.Geod.inv returns (forward_azimuth, back_azimuth, distance_m).
        _, _, distance_m = geod.inv(lon, lat, float(cell.longitude), float(cell.latitude))
        if distance_m < best_distance:
            best_distance = distance_m
            best_cell = cell
    if best_cell is None:
        # `cells` was empty; caller MUST guarantee non-empty (G2 loaded a
        # populated snapshot). We surface a controlled error rather than a
        # KeyError so tests can assert on the message.
        raise MappingAlgorithmError(
            "cannot select nearest cell: no candidate cells supplied"
        )
    # Count ties on a second pass — we cannot count during the first pass
    # because ties are relative to the FINAL best_distance.
    tied_count = 0
    for cell in cells:
        _, _, distance_m = geod.inv(lon, lat, float(cell.longitude), float(cell.latitude))
        if abs(distance_m - best_distance) <= _GEODESIC_TIE_TOLERANCE_M:
            tied_count += 1
    if tied_count == 1:
        tie_status = "unique"
    else:
        tie_status = f"tied_with_{tied_count}"
    return best_cell, float(best_distance), tie_status, len(cells)


# --- public entry point (§2.1) --------------------------------------------


def nearest_cell_barycenter_geodesic_v1(
    baseline_root: pathlib.Path,
    source_id: str,
    grid_id: str,
    store: GridSnapshotLoader,
) -> tuple[ElementOwnership, ...]:
    """Compute per-element ownership by geodesic nearest-cell (algorithm v1).

    Steps (fail-closed at every gate):

    1. Parse mesh geometry via
       :func:`workers.mapping_builder.integrity.read_sp_mesh_geometry`.
    2. Read the package CRS WKT via
       :func:`workers.mapping_builder.integrity.verify_package_crs`, then
       build a ``pyproj.Transformer(package_crs -> EPSG:4326,
       always_xy=True)``.
    3. Compute each element's barycenter as ``((x1+x2+x3)/3, (y1+y2+y3)/3)``
       in the package CRS, then transform to WGS84.
    4. Call :func:`verify_grid_identity_precondition` — G2 gate. Raises if
       the snapshot is unregistered, the shared-helper signature disagrees
       with the stored value, or any barycenter is outside the snapshot
       bbox. On success, returns ``(snapshot, cells)``.
    5. For each element barycenter, iterate the loaded cells (in the caller-
       supplied order — the snapshot's cells are already canonical-ordinal-
       ordered per the registry contract) and pick the geodesic-nearest.
    6. If the cells form a regular lat/lon grid, additionally compute each
       element's cell via independent lon/lat rounding and raise
       :class:`RegularGridFastPathParityError` if it disagrees with the
       geodesic pick. The reported ``geodesic_distance_m`` is always the
       geodesic value — the fast path is a defense-in-depth cross-check,
       never a silent shortcut.

    Parameters
    ----------
    baseline_root:
        Directory containing the baseline basin model package. Must carry
        at least a parseable ``.sp.mesh`` and a parseable ``gis/*.prj``.
    source_id, grid_id:
        Identity keys for the registered grid snapshot.
    store:
        Any :class:`GridSnapshotLoader` — production code passes the DB
        adapter, tests pass an in-memory loader.

    Returns
    -------
    tuple[ElementOwnership, ...]
        Immutable per-element ownership records ordered by ``element_id``
        ascending. Length equals the mesh element count.

    Raises
    ------
    workers.mapping_builder.integrity.BaselineIntegrityError
        Passes through from ``verify_package_crs`` (missing / unparseable
        ``.prj``) or ``read_sp_mesh_geometry`` (missing / unparseable
        ``.sp.mesh``).
    UnregisteredGridSnapshotError
        No snapshot registered for ``(source_id, grid_id)``.
    GridSignatureMismatchError
        Recomputed signature differs from the stored value.
    ElementBarycenterOutOfCoverageError
        An element barycenter is outside the snapshot coverage bbox.
    RegularGridFastPathParityError
        Regular-grid fast path disagrees with the geodesic pick.
    """
    if not isinstance(baseline_root, pathlib.Path):
        raise TypeError(
            f"nearest_cell_barycenter_geodesic_v1 expects pathlib.Path, got "
            f"{type(baseline_root).__name__}"
        )

    # Steps 1-2: geometry + CRS transformer.
    elements, node_xy = read_sp_mesh_geometry(baseline_root)
    crs_report = verify_package_crs(baseline_root)
    package_crs = pyproj.CRS.from_wkt(crs_report.wkt)
    transformer = pyproj.Transformer.from_crs(package_crs, "EPSG:4326", always_xy=True)

    # Step 3: barycenters in package CRS -> WGS84.
    barycenters_pkg = _compute_element_barycenters_package_crs(elements, node_xy)
    barycenters_wgs84 = _transform_barycenters_to_wgs84(barycenters_pkg, transformer)

    # Step 4: G2 gate.
    _snapshot, cells = verify_grid_identity_precondition(
        source_id=source_id,
        grid_id=grid_id,
        barycenters_wgs84=barycenters_wgs84,
        store=store,
    )

    # Step 5-6: nearest-cell selection + optional fast-path parity check.
    geod = pyproj.Geod(ellps="WGS84")
    structure = _detect_regular_grid(cells)
    ownerships: list[ElementOwnership] = []
    for element_id, lon, lat in barycenters_wgs84:
        cell, distance_m, tie_status, candidate_count = _select_nearest_cell_geodesic(
            lon=lon,
            lat=lat,
            cells=cells,
            geod=geod,
        )
        if structure is not None:
            fast_pick = _select_cell_by_lonlat_round(lon, lat, structure)
            if fast_pick.grid_cell_id != cell.grid_cell_id:
                raise RegularGridFastPathParityError(
                    element_id=element_id,
                    geodesic_grid_cell_id=cell.grid_cell_id,
                    fast_path_grid_cell_id=fast_pick.grid_cell_id,
                )
        ownerships.append(
            ElementOwnership(
                element_id=element_id,
                grid_cell_id=cell.grid_cell_id,
                canonical_ordinal=int(cell.canonical_ordinal),
                geodesic_distance_m=distance_m,
                tie_status=tie_status,
                candidate_count=candidate_count,
            )
        )

    ownerships.sort(key=lambda o: o.element_id)
    return tuple(ownerships)
