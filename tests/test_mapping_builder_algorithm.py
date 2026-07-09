"""Tests for :mod:`workers.mapping_builder.algorithm` (Epic #909 SUB-5, §2.0 + §2.1).

Coverage:

* G2 grid-identity precondition (§2.0) — positive path, four fail-closed
  negative paths (unregistered / signature mismatch / bbox violation) plus
  the "shared helper is the signature authority" invocation contract.
* ``nearest_cell_barycenter_geodesic_v1`` (§2.1) — barycenter equals the
  mesh three-vertex mean in the package CRS; nearest selection uses
  geodesic distance rather than an undeclared planar-degree distance;
  regular lat/lon grid fast path matches the geodesic pick and tie
  behavior; algorithm_id is the versioned constant.
* Immutability + signature-pin invariants matching the SUB-1..SUB-4 pattern
  ``test_*_signature_pinned`` used elsewhere in ``test_mapping_builder_integrity.py``.
"""

from __future__ import annotations

import dataclasses
import inspect
import pathlib
import typing

import pyproj
import pytest

from packages.common import grid_signature as grid_signature_module
from packages.common.grid_registry_store import (
    CanonicalGridCell,
    CanonicalGridSnapshot,
)
from tests.fixtures.mapping_builder.in_memory_grid_snapshot import (
    InMemoryGridSnapshotLoader,
    make_regular_grid_cells,
    make_snapshot,
)
from workers.mapping_builder import (
    DistanceSanityBoundExceededError,
    ElementBarycenterOutOfCoverageError,
    ElementOwnership,
    GridSignatureMismatchError,
    GridSnapshotLoader,
    MappingAlgorithmError,
    RegularGridFastPathParityError,
    SupersededGridSnapshotError,
    UnregisteredGridSnapshotError,
    algorithm_id,
    assign_shud_forcing_index,
    derive_used_cell_subset,
    nearest_cell_barycenter_geodesic_v1,
    resolve_tie_by_canonical_ordinal,
    verify_grid_identity_precondition,
    verify_half_cell_diagonal_sanity_bound,
)
from workers.mapping_builder import algorithm as algorithm_module

# --- fixture CRS WKTs ------------------------------------------------------

#: Real keliya package CRS (custom Albers, central meridian 105°E). Sourced
#: from ``tests/fixtures/mapping_builder/keliya_minimal/gis/keliya.prj`` per
#: the §1.2 live audit; used here to prove real-world WKT integrates cleanly
#: with the algorithm's ``verify_package_crs`` -> Transformer pipeline.
_KELIYA_ALBERS_WKT = (
    'PROJCS["unknown",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
    'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Albers"],PARAMETER["False_Easting",0.0],'
    'PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",105.0],'
    'PARAMETER["Standard_Parallel_1",25.0],'
    'PARAMETER["Standard_Parallel_2",47.0],'
    'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]'
)

#: Plain WGS84 geographic CRS used as an identity transformer (X = lon,
#: Y = lat) for tests where placing barycenters at explicit lon/lat is more
#: important than proving a projected transform. Kept as ``UNIT["degree"]``
#: so pyproj classifies it as geographic and treats X/Y as lon/lat when
#: ``always_xy=True``.
_WGS84_GEOGRAPHIC_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
    'AUTHORITY["EPSG","4326"]]'
)


# --- basin fixture builder -------------------------------------------------


def _write_baseline(
    baseline_root: pathlib.Path,
    *,
    elements: list[tuple[int, int, int, int]],
    node_xy: dict[int, tuple[float, float]],
    prj_wkt: str,
    basin: str = "testbasin",
) -> pathlib.Path:
    """Create a minimal SHUD baseline directory under ``baseline_root``.

    Only the pieces the mapping algorithm reads are populated:

    * ``<basin>.sp.mesh`` — element table + node table in the SHUD 4/8-column
      layout expected by :func:`workers.mapping_builder.read_sp_mesh_geometry`.
    * ``gis/<basin>.prj`` — one-line WKT.

    ``.sp.att`` / ``.tsd.forc`` are intentionally omitted: the algorithm
    entry points only require mesh + prj, and Task 2.0/2.1 do not gate on
    G0. Returns the baseline root path.
    """
    baseline_root.mkdir(parents=True, exist_ok=True)
    (baseline_root / "gis").mkdir(parents=True, exist_ok=True)
    (baseline_root / "gis" / f"{basin}.prj").write_text(prj_wkt + "\n", encoding="utf-8")

    n_elements = len(elements)
    n_element_cols = 8  # ID Node1 Node2 Node3 Nabr1 Nabr2 Nabr3 Zmax
    n_nodes = len(node_xy)
    n_node_cols = 5  # ID X Y AqDepth Elevation

    lines: list[str] = []
    lines.append(f"{n_elements}\t{n_element_cols}")
    lines.append("ID\tNode1\tNode2\tNode3\tNabr1\tNabr2\tNabr3\tZmax")
    for element_id, v1, v2, v3 in elements:
        lines.append(f"{element_id}\t{v1}\t{v2}\t{v3}\t0\t0\t0\t100")
    lines.append(f"{n_nodes}\t{n_node_cols}")
    lines.append("ID\tX\tY\tAqDepth\tElevation")
    for node_id in sorted(node_xy.keys()):
        x, y = node_xy[node_id]
        lines.append(f"{node_id}\t{x}\t{y}\t8\t100")
    (baseline_root / f"{basin}.sp.mesh").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return baseline_root


def _single_element_basin(
    tmp_path: pathlib.Path,
    *,
    v1: tuple[float, float],
    v2: tuple[float, float],
    v3: tuple[float, float],
    prj_wkt: str = _WGS84_GEOGRAPHIC_WKT,
) -> pathlib.Path:
    """Build a 1-element basin with vertex X/Y placed at the supplied coords."""
    return _write_baseline(
        tmp_path / "basin",
        elements=[(1, 1, 2, 3)],
        node_xy={1: v1, 2: v2, 3: v3},
        prj_wkt=prj_wkt,
    )


# --- §2.0 G2 precondition tests -------------------------------------------


def _make_loader(
    *,
    cells: list[CanonicalGridCell] | None = None,
    source_id: str = "ifs",
    grid_id: str = "test_grid_v1",
    bbox_pad: float = 0.5,
    grid_signature_override: str | None = None,
    bbox_override: tuple[float, float, float, float] | None = None,
) -> InMemoryGridSnapshotLoader:
    """Return an :class:`InMemoryGridSnapshotLoader` populated with a 3x3 grid.

    The default 3x3 grid is centered at (10.0°E, 20.0°N) with 0.1° step so
    tests can place barycenters trivially inside or outside the bbox.
    """
    if cells is None:
        cells = make_regular_grid_cells(
            lon0=9.9,
            lat0=19.9,
            lon_step=0.1,
            lat_step=0.1,
            lon_count=3,
            lat_count=3,
        )
    snapshot = make_snapshot(
        source_id=source_id,
        grid_id=grid_id,
        cells=cells,
        bbox_pad=bbox_pad,
        grid_signature_override=grid_signature_override,
        bbox_override=bbox_override,
    )
    return InMemoryGridSnapshotLoader(
        source_id=source_id,
        grid_id=grid_id,
        snapshot=snapshot,
        cells=cells,
    )


def test_g2_grid_identity_positive() -> None:
    """Happy path: shared helper hash matches stored + all barycenters in bbox."""
    loader = _make_loader()
    barycenters = [
        (1, 10.0, 20.0),  # dead center
        (2, 10.05, 20.05),
        (3, 9.95, 19.95),
    ]

    snapshot, cells = verify_grid_identity_precondition(
        source_id="ifs",
        grid_id="test_grid_v1",
        barycenters_wgs84=barycenters,
        store=loader,
    )

    assert isinstance(snapshot, CanonicalGridSnapshot)
    assert isinstance(cells, tuple)
    assert len(cells) == 9
    assert all(isinstance(c, CanonicalGridCell) for c in cells)
    # Shared-helper recomputed signature MUST equal the stored one.
    assert snapshot.grid_signature == grid_signature_module.grid_signature_hash(list(cells))
    # Loader was invoked exactly once with the expected identity.
    assert loader.call_log == [("ifs", "test_grid_v1")]


def test_g2_signature_mismatch_fails_closed() -> None:
    """Tampered stored signature -> raises GridSignatureMismatchError."""
    cells = make_regular_grid_cells(
        lon0=9.9, lat0=19.9, lon_step=0.1, lat_step=0.1, lon_count=3, lat_count=3
    )
    true_signature = grid_signature_module.grid_signature_hash(cells)
    # Flip the last hex character to build a different-but-valid 64-char hex.
    flipped_last = "0" if true_signature[-1] != "0" else "1"
    tampered_signature = true_signature[:-1] + flipped_last
    assert tampered_signature != true_signature
    loader = _make_loader(cells=cells, grid_signature_override=tampered_signature)

    with pytest.raises(GridSignatureMismatchError) as exc_info:
        verify_grid_identity_precondition(
            source_id="ifs",
            grid_id="test_grid_v1",
            barycenters_wgs84=[(1, 10.0, 20.0)],
            store=loader,
        )

    assert exc_info.value.source_id == "ifs"
    assert exc_info.value.grid_id == "test_grid_v1"
    assert exc_info.value.expected == tampered_signature
    assert exc_info.value.recomputed == true_signature
    assert isinstance(exc_info.value, MappingAlgorithmError)


def test_g2_barycenter_out_of_coverage_fails_closed() -> None:
    """Barycenter outside snapshot bbox -> raises + zero output artifact."""
    loader = _make_loader(bbox_override=(19.9, 20.1, 9.9, 10.1))
    barycenters = [
        (1, 10.0, 20.0),  # inside
        (7, 15.0, 20.0),  # OUTSIDE (lon > east)
    ]
    with pytest.raises(ElementBarycenterOutOfCoverageError) as exc_info:
        verify_grid_identity_precondition(
            source_id="ifs",
            grid_id="test_grid_v1",
            barycenters_wgs84=barycenters,
            store=loader,
        )
    assert exc_info.value.element_id == 7
    assert exc_info.value.barycenter_lon == 15.0
    assert exc_info.value.barycenter_lat == 20.0
    assert exc_info.value.bbox == (19.9, 20.1, 9.9, 10.1)
    assert isinstance(exc_info.value, MappingAlgorithmError)


def test_g2_unregistered_grid_fails_closed() -> None:
    """find_snapshot_by_identity returns None -> UnregisteredGridSnapshotError."""
    loader = _make_loader(source_id="ifs", grid_id="registered_grid")
    with pytest.raises(UnregisteredGridSnapshotError) as exc_info:
        verify_grid_identity_precondition(
            source_id="ifs",
            grid_id="different_grid",  # not registered
            barycenters_wgs84=[(1, 10.0, 20.0)],
            store=loader,
        )
    assert exc_info.value.source_id == "ifs"
    assert exc_info.value.grid_id == "different_grid"
    assert isinstance(exc_info.value, MappingAlgorithmError)


def test_g2_superseded_snapshot_fails_closed() -> None:
    """Snapshot with non-NULL ``superseded_at`` -> SupersededGridSnapshotError.

    Cross-change contract from ``grid-drift-lifecycle/spec.md`` §"Consumers
    of a superseded snapshot fail closed": the mapping-asset build MUST fail
    closed when the loaded snapshot has a non-NULL ``superseded_at``, never
    silently producing mapping output bound to a stale grid identity. The
    guard runs BEFORE signature recomputation, so a superseded snapshot
    cannot slip through by still having a matching signature.
    """
    import datetime as _dt

    cells = make_regular_grid_cells(
        lon0=9.9, lat0=19.9, lon_step=0.1, lat_step=0.1, lon_count=3, lat_count=3
    )
    base_snapshot = make_snapshot(
        source_id="ifs",
        grid_id="test_grid_v1",
        cells=cells,
        bbox_pad=0.5,
    )
    superseded_at = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    superseded_snapshot = dataclasses.replace(
        base_snapshot, superseded_at=superseded_at
    )
    loader = InMemoryGridSnapshotLoader(
        source_id="ifs",
        grid_id="test_grid_v1",
        snapshot=superseded_snapshot,
        cells=cells,
    )

    with pytest.raises(SupersededGridSnapshotError) as exc_info:
        verify_grid_identity_precondition(
            source_id="ifs",
            grid_id="test_grid_v1",
            # Barycenter is well inside the bbox and cells match the stored
            # signature — the ONLY reason to fail is superseded_at.
            barycenters_wgs84=[(1, 10.0, 20.0)],
            store=loader,
        )
    assert exc_info.value.source_id == "ifs"
    assert exc_info.value.grid_id == "test_grid_v1"
    assert exc_info.value.superseded_at == superseded_at
    assert isinstance(exc_info.value, MappingAlgorithmError)


def test_g2_shared_helper_is_the_signature_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove ``grid_signature_hash`` is the SOLE signature authority.

    Monkeypatches the algorithm module's ``grid_signature_hash`` symbol to a
    call-tracking wrapper; on invocation the wrapper records ``cells`` and
    delegates to the real helper. Any hand-rolled signature would never
    touch this wrapper, so a passing test proves invocation contract.
    """
    real_helper = grid_signature_module.grid_signature_hash
    captured_calls: list[list[CanonicalGridCell]] = []

    def _tracking_helper(cells):
        captured_calls.append(list(cells))
        return real_helper(cells)

    monkeypatch.setattr(algorithm_module, "grid_signature_hash", _tracking_helper)

    loader = _make_loader()
    snapshot, cells = verify_grid_identity_precondition(
        source_id="ifs",
        grid_id="test_grid_v1",
        barycenters_wgs84=[(1, 10.0, 20.0)],
        store=loader,
    )

    assert len(captured_calls) == 1, "shared helper MUST be invoked exactly once"
    # The captured cells are the ones the loader handed back.
    assert list(captured_calls[0]) == list(cells)
    # And their hash equals the stored signature (proving the algorithm did
    # NOT hand-roll its own signature).
    assert real_helper(captured_calls[0]) == snapshot.grid_signature


# --- §2.1 nearest-cell tests ----------------------------------------------


def test_barycenter_is_mesh_three_vertex_mean(tmp_path: pathlib.Path) -> None:
    """Barycenter equals ``((x1+x2+x3)/3, (y1+y2+y3)/3)`` in package CRS.

    Uses the WGS84 geographic pkg-CRS so pkg-CRS X/Y == lon/lat and we can
    both (a) assert the mean equality in the package CRS and (b) verify the
    WGS84 transform is identity. Placing vertices at asymmetric coordinates
    catches an off-by-one row-order bug that a symmetric fixture would miss.
    """
    v1 = (10.00, 20.00)
    v2 = (10.30, 20.00)
    v3 = (10.00, 20.30)
    expected_mean = (
        (v1[0] + v2[0] + v3[0]) / 3.0,
        (v1[1] + v2[1] + v3[1]) / 3.0,
    )
    baseline = _single_element_basin(tmp_path, v1=v1, v2=v2, v3=v3)

    # Snapshot registered at the mean, so no G2 violation.
    cells = [
        CanonicalGridCell(
            grid_cell_id="0",
            longitude=expected_mean[0],
            latitude=expected_mean[1],
            canonical_ordinal=1,
        )
    ]
    loader = InMemoryGridSnapshotLoader(
        source_id="ifs",
        grid_id="single_cell",
        snapshot=make_snapshot(
            source_id="ifs",
            grid_id="single_cell",
            cells=cells,
            bbox_pad=1.0,
        ),
        cells=cells,
    )
    ownerships = nearest_cell_barycenter_geodesic_v1(
        baseline_root=baseline,
        source_id="ifs",
        grid_id="single_cell",
        store=loader,
    )
    assert len(ownerships) == 1
    only = ownerships[0]
    assert only.element_id == 1
    assert only.grid_cell_id == "0"
    # Geodesic distance from a point to itself is 0m (within numeric noise).
    assert only.geodesic_distance_m == pytest.approx(0.0, abs=1.0e-6)

    # Independent verification: parse mesh, compute mean, transform, compare.
    from workers.mapping_builder import read_sp_mesh_geometry

    elements, node_xy = read_sp_mesh_geometry(baseline)
    assert elements == ((1, 1, 2, 3),)
    x_mean = sum(node_xy[v][0] for v in (1, 2, 3)) / 3.0
    y_mean = sum(node_xy[v][1] for v in (1, 2, 3)) / 3.0
    assert (x_mean, y_mean) == pytest.approx(expected_mean, abs=1.0e-12)


def test_nearest_cell_uses_geodesic_not_planar_degree(tmp_path: pathlib.Path) -> None:
    """At high latitude, planar-degree and geodesic nearest DIFFER.

    Barycenter at (0°E, 85°N). Two candidate cells:

    * A at (5°E, 85°N)   — planar-degree = 5.0, geodesic ≈ 48 km.
    * B at (0°E, 85.5°N) — planar-degree = 0.5, geodesic ≈ 56 km.

    Planar-degree would pick B (0.5 < 5.0); geodesic picks A. The algorithm
    MUST pick A. Also asserts the recorded distance is a real number of
    meters, not a degrees-shaped value.
    """
    baseline = _single_element_basin(
        tmp_path,
        v1=(-0.1, 84.9),
        v2=(0.1, 84.9),
        v3=(0.0, 85.2),
    )
    # Barycenter of the above = (0.0, 85.0) — exactly where we want it.

    cell_a = CanonicalGridCell(
        grid_cell_id="A", longitude=5.0, latitude=85.0, canonical_ordinal=1
    )
    cell_b = CanonicalGridCell(
        grid_cell_id="B", longitude=0.0, latitude=85.5, canonical_ordinal=2
    )
    cells = [cell_a, cell_b]

    loader = InMemoryGridSnapshotLoader(
        source_id="ifs",
        grid_id="polar_grid",
        snapshot=make_snapshot(
            source_id="ifs",
            grid_id="polar_grid",
            cells=cells,
            bbox_override=(84.0, 90.0, -10.0, 10.0),
        ),
        cells=cells,
    )

    ownerships = nearest_cell_barycenter_geodesic_v1(
        baseline_root=baseline,
        source_id="ifs",
        grid_id="polar_grid",
        store=loader,
    )
    assert len(ownerships) == 1
    (own,) = ownerships
    # Geodesic picks A; planar-degree would have picked B.
    assert own.grid_cell_id == "A", (
        "geodesic MUST pick A at (5°E, 85°N); if this fails, the algorithm "
        "is using planar-degree distance"
    )
    # Sanity: independently verify geodesic distances to prove which is nearer.
    geod = pyproj.Geod(ellps="WGS84")
    _, _, dist_a = geod.inv(0.0, 85.0, cell_a.longitude, cell_a.latitude)
    _, _, dist_b = geod.inv(0.0, 85.0, cell_b.longitude, cell_b.latitude)
    assert dist_a < dist_b
    assert own.geodesic_distance_m == pytest.approx(dist_a, rel=1.0e-9)
    # Meter magnitudes: km-scale, not degree-scale.
    assert 40_000.0 < own.geodesic_distance_m < 60_000.0
    assert own.tie_status == "unique"
    assert own.candidate_count == 2


def test_regular_grid_fast_path_matches_geodesic(tmp_path: pathlib.Path) -> None:
    """Regular grid: independent lon/lat rounding agrees with geodesic pick.

    Constructs a 4-element basin whose barycenters land at distinct grid
    cells, invokes ``nearest_cell_barycenter_geodesic_v1`` (which runs the
    parity check internally), and independently invokes the two module
    selectors to prove per-element cell_id and tie behavior are identical.
    """
    # Regular 3×3 grid, 0.1° step centered at (30.0, 40.0).
    cells = make_regular_grid_cells(
        lon0=29.9, lat0=39.9, lon_step=0.1, lat_step=0.1, lon_count=3, lat_count=3
    )
    loader = InMemoryGridSnapshotLoader(
        source_id="ifs",
        grid_id="regular_grid",
        snapshot=make_snapshot(
            source_id="ifs",
            grid_id="regular_grid",
            cells=cells,
            bbox_pad=1.0,
        ),
        cells=cells,
    )

    # 4 elements: each barycenter mildly perturbed off a grid center so
    # geodesic and lon/lat-rounding both point to the same nearest cell.
    # Vertices arranged so the mean lands at the target.
    element_barycenter_targets = [
        (30.02, 40.03),  # nearest cell: (30.0, 40.0)
        (30.11, 39.92),  # nearest cell: (30.1, 39.9)
        (29.88, 40.11),  # nearest cell: (29.9, 40.1)
        (30.09, 40.09),  # nearest cell: (30.1, 40.1)
    ]
    node_xy: dict[int, tuple[float, float]] = {}
    elements: list[tuple[int, int, int, int]] = []
    next_node_id = 1
    for elem_index, (tgt_lon, tgt_lat) in enumerate(element_barycenter_targets, start=1):
        # Three vertices around the target so their mean == target.
        v_a = (tgt_lon - 0.01, tgt_lat - 0.01)
        v_b = (tgt_lon + 0.02, tgt_lat - 0.01)
        v_c = (tgt_lon - 0.01, tgt_lat + 0.02)
        ids: list[int] = []
        for xy in (v_a, v_b, v_c):
            node_xy[next_node_id] = xy
            ids.append(next_node_id)
            next_node_id += 1
        elements.append((elem_index, ids[0], ids[1], ids[2]))
    baseline = _write_baseline(
        tmp_path / "basin",
        elements=elements,
        node_xy=node_xy,
        prj_wkt=_WGS84_GEOGRAPHIC_WKT,
    )

    # Main path: should succeed (parity check embedded).
    ownerships = nearest_cell_barycenter_geodesic_v1(
        baseline_root=baseline,
        source_id="ifs",
        grid_id="regular_grid",
        store=loader,
    )
    assert len(ownerships) == 4

    # Independent parity: invoke both selectors directly and compare.
    geod = pyproj.Geod(ellps="WGS84")
    structure = algorithm_module._detect_regular_grid(cells)
    assert structure is not None, "test premise: cells MUST be detected as a regular grid"

    for own, (tgt_lon, tgt_lat) in zip(
        ownerships, element_barycenter_targets, strict=True
    ):
        geo_cell, geo_dist, geo_tie, geo_count = (
            algorithm_module._select_nearest_cell_geodesic(
                lon=tgt_lon, lat=tgt_lat, cells=cells, geod=geod
            )
        )
        fast_cell = algorithm_module._select_cell_by_lonlat_round(
            lon=tgt_lon, lat=tgt_lat, structure=structure
        )
        assert geo_cell.grid_cell_id == fast_cell.grid_cell_id, (
            "regular-grid fast path and geodesic scan disagree on "
            f"nearest cell for barycenter ({tgt_lon}, {tgt_lat})"
        )
        # And the algorithm's public output matches both.
        assert own.grid_cell_id == geo_cell.grid_cell_id
        assert own.tie_status == geo_tie
        assert own.candidate_count == geo_count == 9
        assert own.geodesic_distance_m == pytest.approx(geo_dist, rel=1.0e-12, abs=1.0e-9)


def test_regular_grid_fast_path_divergence_raises(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force a divergence between fast path and geodesic -> parity error.

    Guards the "Do NOT ship a fast path that silently diverges from geodesic"
    invariant: monkeypatches ``_select_cell_by_lonlat_round`` to return the
    wrong cell, then confirms :class:`RegularGridFastPathParityError` is
    raised BEFORE any ownership tuple leaks out.
    """
    cells = make_regular_grid_cells(
        lon0=29.9, lat0=39.9, lon_step=0.1, lat_step=0.1, lon_count=3, lat_count=3
    )
    loader = InMemoryGridSnapshotLoader(
        source_id="ifs",
        grid_id="regular_grid",
        snapshot=make_snapshot(
            source_id="ifs",
            grid_id="regular_grid",
            cells=cells,
            bbox_pad=1.0,
        ),
        cells=cells,
    )
    baseline = _single_element_basin(
        tmp_path,
        v1=(30.02 - 0.01, 40.03 - 0.01),
        v2=(30.02 + 0.02, 40.03 - 0.01),
        v3=(30.02 - 0.01, 40.03 + 0.02),
    )

    # Return a cell that is definitely NOT the geodesic pick.
    wrong_cell = next(c for c in cells if c.grid_cell_id == "8")

    def _lying_fast_path(lon, lat, structure):  # noqa: ANN001 - test double
        return wrong_cell

    monkeypatch.setattr(
        algorithm_module, "_select_cell_by_lonlat_round", _lying_fast_path
    )

    with pytest.raises(RegularGridFastPathParityError) as exc_info:
        nearest_cell_barycenter_geodesic_v1(
            baseline_root=baseline,
            source_id="ifs",
            grid_id="regular_grid",
            store=loader,
        )
    assert exc_info.value.element_id == 1
    assert exc_info.value.fast_path_grid_cell_id == "8"
    assert exc_info.value.geodesic_grid_cell_id != "8"
    assert isinstance(exc_info.value, MappingAlgorithmError)


def test_algorithm_id_constant() -> None:
    """``algorithm_id`` is pinned and importable from the mapping_builder package."""
    from workers.mapping_builder import algorithm_id as reexported_id
    from workers.mapping_builder.algorithm import algorithm_id as source_id

    assert algorithm_id == "nearest_cell_barycenter_geodesic_v1"
    assert reexported_id is algorithm_id
    assert source_id is algorithm_id


# --- signature pins -------------------------------------------------------


def test_verify_grid_identity_precondition_signature_pinned() -> None:
    """Signature pin: parameter names + resolved type hints + return type frozen."""
    sig = inspect.signature(verify_grid_identity_precondition)
    assert list(sig.parameters) == [
        "source_id",
        "grid_id",
        "barycenters_wgs84",
        "store",
    ]

    hints = typing.get_type_hints(verify_grid_identity_precondition)
    assert hints["source_id"] is str
    assert hints["grid_id"] is str
    assert hints["store"] is GridSnapshotLoader
    # Return type is a tuple of (snapshot, tuple[cells...]); at minimum the
    # origin is tuple so downstream evidence knows the shape.
    return_hint = hints["return"]
    assert typing.get_origin(return_hint) is tuple


def test_nearest_cell_barycenter_geodesic_v1_signature_pinned() -> None:
    """Signature pin: parameter names + resolved type hints + return type frozen."""
    sig = inspect.signature(nearest_cell_barycenter_geodesic_v1)
    assert list(sig.parameters) == [
        "baseline_root",
        "source_id",
        "grid_id",
        "store",
    ]

    hints = typing.get_type_hints(nearest_cell_barycenter_geodesic_v1)
    assert hints["baseline_root"] is pathlib.Path
    assert hints["source_id"] is str
    assert hints["grid_id"] is str
    assert hints["store"] is GridSnapshotLoader
    return_hint = hints["return"]
    assert typing.get_origin(return_hint) is tuple
    return_args = typing.get_args(return_hint)
    # tuple[ElementOwnership, ...] carries ElementOwnership as first arg.
    assert return_args[0] is ElementOwnership


def test_element_ownership_frozen() -> None:
    """``ElementOwnership`` is a frozen dataclass; field assignment must raise."""
    record = ElementOwnership(
        element_id=1,
        grid_cell_id="0",
        canonical_ordinal=1,
        geodesic_distance_m=42.0,
        tie_status="unique",
        candidate_count=9,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.element_id = 99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.grid_cell_id = "x"  # type: ignore[misc]


# --- §2.2 tie-break + sanity bound tests ----------------------------------


def test_tie_break_selects_smallest_canonical_ordinal(tmp_path: pathlib.Path) -> None:
    """Barycenter equidistant to two cells -> pick smallest canonical ordinal.

    3x3 regular grid at (10.0°E, 20.0°N) with 0.1° step. Cell (10.0, 20.0)
    is canonical_ordinal=5; cell (10.1, 20.0) is canonical_ordinal=6.
    Placing the barycenter at (10.05, 20.0) puts it exactly between the two
    same-latitude cells — by WGS84-ellipsoid symmetry the geodesic distances
    are identical to within :data:`_GEODESIC_TIE_TOLERANCE_M`, triggering the
    §2.2 tie-break. The rule picks the SMALLEST canonical ordinal, so ord5
    must win.
    """
    cells = make_regular_grid_cells(
        lon0=9.9, lat0=19.9, lon_step=0.1, lat_step=0.1, lon_count=3, lat_count=3
    )
    loader = InMemoryGridSnapshotLoader(
        source_id="ifs",
        grid_id="tie_grid",
        snapshot=make_snapshot(
            source_id="ifs",
            grid_id="tie_grid",
            cells=cells,
            bbox_pad=0.5,
        ),
        cells=cells,
    )
    # Vertices arranged so the mean is exactly (10.05, 20.0). y_mean = 60.0/3
    # is exact in IEEE-754; x_mean = 30.15/3 lands at the nearest float to
    # 10.05 — either way, symmetry keeps the two candidate distances tied.
    baseline = _single_element_basin(
        tmp_path,
        v1=(10.04, 19.99),
        v2=(10.07, 19.99),
        v3=(10.04, 20.02),
    )
    ownerships = nearest_cell_barycenter_geodesic_v1(
        baseline_root=baseline,
        source_id="ifs",
        grid_id="tie_grid",
        store=loader,
    )
    assert len(ownerships) == 1
    (only,) = ownerships
    # ord5 corresponds to grid_cell_id="4" (0-based cell index).
    assert only.canonical_ordinal == 5, (
        "tie-break MUST pick smallest canonical ordinal; got ord="
        f"{only.canonical_ordinal}"
    )
    assert only.grid_cell_id == "4"
    # Verify the tie was actually detected (not resolved by strict distance
    # comparison alone).
    assert only.tie_status.startswith("tied_with_"), (
        "expected the two same-latitude cells to be flagged as tied, got "
        f"tie_status={only.tie_status!r}"
    )


def test_tie_break_is_reproducible_across_runs(tmp_path: pathlib.Path) -> None:
    """Two independent runs on the same input yield byte-identical ownerships.

    Reproducibility is a first-class §7 determinism requirement. This test
    builds two fresh loaders (so no cached state carries between runs) and
    asserts the returned tuples compare equal.
    """
    cells = make_regular_grid_cells(
        lon0=9.9, lat0=19.9, lon_step=0.1, lat_step=0.1, lon_count=3, lat_count=3
    )

    def _fresh_loader() -> InMemoryGridSnapshotLoader:
        return InMemoryGridSnapshotLoader(
            source_id="ifs",
            grid_id="tie_grid",
            snapshot=make_snapshot(
                source_id="ifs",
                grid_id="tie_grid",
                cells=cells,
                bbox_pad=0.5,
            ),
            cells=cells,
        )

    baseline = _single_element_basin(
        tmp_path,
        v1=(10.04, 19.99),
        v2=(10.07, 19.99),
        v3=(10.04, 20.02),
    )
    result_1 = nearest_cell_barycenter_geodesic_v1(
        baseline_root=baseline,
        source_id="ifs",
        grid_id="tie_grid",
        store=_fresh_loader(),
    )
    result_2 = nearest_cell_barycenter_geodesic_v1(
        baseline_root=baseline,
        source_id="ifs",
        grid_id="tie_grid",
        store=_fresh_loader(),
    )
    assert result_1 == result_2, (
        "identical input MUST yield byte-identical ownership tuples "
        "(§7 determinism requirement)"
    )


def test_sanity_bound_blocks_when_distance_exceeds_half_cell_diagonal(
    tmp_path: pathlib.Path,
) -> None:
    """In-coverage barycenter far from any cell -> DistanceSanityBoundExceededError.

    Constructs a 3x3 regular grid (0.1° step at 20°N, half-cell-diagonal
    ~7.6 km) but overrides the snapshot bbox to a much wider box so the
    barycenter still passes G2 coverage. Placing the barycenter at (10.9°E,
    20.5°N) lands it ~99 km from the nearest cell — well beyond the local
    half-cell-diagonal plus tolerance. Fail-closed: no ownership escapes.
    """
    cells = make_regular_grid_cells(
        lon0=9.9, lat0=19.9, lon_step=0.1, lat_step=0.1, lon_count=3, lat_count=3
    )
    loader = InMemoryGridSnapshotLoader(
        source_id="ifs",
        grid_id="sparse_grid",
        snapshot=make_snapshot(
            source_id="ifs",
            grid_id="sparse_grid",
            cells=cells,
            bbox_override=(19.0, 21.0, 9.0, 11.0),  # wide bbox
        ),
        cells=cells,
    )
    # Barycenter at (10.9, 20.5) — inside bbox but ~99 km from nearest cell.
    baseline = _single_element_basin(
        tmp_path,
        v1=(10.89, 20.49),
        v2=(10.92, 20.49),
        v3=(10.89, 20.52),
    )
    with pytest.raises(DistanceSanityBoundExceededError) as exc_info:
        nearest_cell_barycenter_geodesic_v1(
            baseline_root=baseline,
            source_id="ifs",
            grid_id="sparse_grid",
            store=loader,
        )
    assert exc_info.value.element_id == 1
    assert exc_info.value.distance_m > (
        exc_info.value.half_cell_diagonal_m + exc_info.value.tolerance_m
    )
    # Half-diagonal for 0.1° step at 20°N is ~7.6 km; distance is ~99 km.
    assert 6_000.0 < exc_info.value.half_cell_diagonal_m < 10_000.0, (
        "half-cell-diagonal not km-scale — sanity-bound math is off"
    )
    assert exc_info.value.distance_m > 50_000.0, (
        "test premise: barycenter must be ~99 km from nearest cell"
    )
    assert isinstance(exc_info.value, MappingAlgorithmError)


def test_sanity_bound_passes_for_normal_element(tmp_path: pathlib.Path) -> None:
    """Well-formed grid + barycenter near a cell center -> no sanity error.

    Positive-path smoke test: distance is small relative to the local
    half-cell-diagonal, so the bound MUST NOT fire. Guards against a
    regression that would over-trigger the blocker on legitimate inputs.
    """
    cells = make_regular_grid_cells(
        lon0=9.9, lat0=19.9, lon_step=0.1, lat_step=0.1, lon_count=3, lat_count=3
    )
    loader = InMemoryGridSnapshotLoader(
        source_id="ifs",
        grid_id="normal_grid",
        snapshot=make_snapshot(
            source_id="ifs",
            grid_id="normal_grid",
            cells=cells,
            bbox_pad=0.5,
        ),
        cells=cells,
    )
    # Barycenter at (10.02, 20.02) — ~3 km from cell (10.0, 20.0).
    baseline = _single_element_basin(
        tmp_path,
        v1=(10.01, 20.01),
        v2=(10.04, 20.01),
        v3=(10.01, 20.04),
    )
    ownerships = nearest_cell_barycenter_geodesic_v1(
        baseline_root=baseline,
        source_id="ifs",
        grid_id="normal_grid",
        store=loader,
    )
    assert len(ownerships) == 1
    (only,) = ownerships
    # Distance is km-scale but well below the ~7.6 km half-cell-diagonal.
    assert 0.0 < only.geodesic_distance_m < 5_000.0, (
        "distance-to-cell should be well below the half-cell-diagonal for a "
        f"well-formed input; got {only.geodesic_distance_m}m"
    )
    # ord5 is (10.0, 20.0) → grid_cell_id="4".
    assert only.grid_cell_id == "4"


# --- §2.3 used-cell subset + shud_forcing_index tests ---------------------


def _mk_ownership(element_id: int, cell: CanonicalGridCell) -> ElementOwnership:
    """Build an ownership record with placeholder distance/tie/count fields.

    §2.3 tests exercise the subset + index derivations directly, so realistic
    distance values don't matter — only ``grid_cell_id`` and
    ``canonical_ordinal`` feed downstream.
    """
    return ElementOwnership(
        element_id=element_id,
        grid_cell_id=cell.grid_cell_id,
        canonical_ordinal=int(cell.canonical_ordinal),
        geodesic_distance_m=0.0,
        tie_status="unique",
        candidate_count=1,
    )


def test_used_cell_subset_excludes_unreferenced_cells() -> None:
    """9-cell grid, 3 elements referencing ord=2/5/7 -> subset is exactly those.

    Cells with canonical_ordinal 1/3/4/6/8/9 are NOT referenced; they MUST
    NOT appear in the subset (zero unused bindings, spec §"Only referenced
    cells become binding cells").
    """
    cells = make_regular_grid_cells(
        lon0=9.9, lat0=19.9, lon_step=0.1, lat_step=0.1, lon_count=3, lat_count=3
    )
    cell_by_ord = {c.canonical_ordinal: c for c in cells}
    ownerships = (
        _mk_ownership(1, cell_by_ord[2]),
        _mk_ownership(2, cell_by_ord[5]),
        _mk_ownership(3, cell_by_ord[7]),
    )
    subset = derive_used_cell_subset(ownerships, cells)
    assert len(subset) == 3, (
        "subset must contain exactly the referenced cells, not the full grid"
    )
    assert [c.canonical_ordinal for c in subset] == [2, 5, 7], (
        "subset must be ordered by canonical_ordinal ascending"
    )
    assert [c.grid_cell_id for c in subset] == [
        cell_by_ord[2].grid_cell_id,
        cell_by_ord[5].grid_cell_id,
        cell_by_ord[7].grid_cell_id,
    ]


def test_used_cell_subset_dedupes_shared_cells() -> None:
    """Multiple elements pointing at same cell -> subset entry appears once.

    Spec §"Only referenced cells become binding cells" — many-to-one is
    the entire point of the mapping (M elements → N<=M cells); the subset
    dedupes so that each USED cell contributes one binding entry.
    """
    cells = make_regular_grid_cells(
        lon0=9.9, lat0=19.9, lon_step=0.1, lat_step=0.1, lon_count=3, lat_count=3
    )
    target_cell = next(c for c in cells if c.canonical_ordinal == 5)
    ownerships = tuple(_mk_ownership(i, target_cell) for i in range(1, 4))
    subset = derive_used_cell_subset(ownerships, cells)
    assert len(subset) == 1, "shared cell must appear exactly once in subset"
    assert subset[0].canonical_ordinal == 5
    assert subset[0].grid_cell_id == target_cell.grid_cell_id


def test_shud_forcing_index_is_1_to_N_contiguous() -> None:
    """5 used cells -> shud_forcing_index values = {1, 2, 3, 4, 5}.

    Spec §"shud_forcing_index is contiguous by canonical ordinal": values
    MUST be contiguous ``1..N`` and unique. This test checks the SET of
    values; :func:`test_shud_forcing_index_ordered_by_canonical_ordinal`
    checks the ordering separately.
    """
    cells = make_regular_grid_cells(
        lon0=9.9, lat0=19.9, lon_step=0.1, lat_step=0.1, lon_count=5, lat_count=1
    )
    assert len(cells) == 5
    result = assign_shud_forcing_index(cells)
    assert len(result) == 5, "one entry per used cell"
    assert set(result.values()) == {1, 2, 3, 4, 5}, (
        "shud_forcing_index values must be contiguous 1..N"
    )
    # All grid_cell_ids present exactly once.
    assert set(result.keys()) == {c.grid_cell_id for c in cells}


def test_shud_forcing_index_ordered_by_canonical_ordinal() -> None:
    """Cells with canonical_ordinal [7, 3, 5] -> {ord3: 1, ord5: 2, ord7: 3}.

    Spec §"shud_forcing_index is contiguous by canonical ordinal": the cell
    with the SMALLEST canonical ordinal gets ``shud_forcing_index=1``, next
    gets 2, etc. Input order does not affect the assignment.
    """
    cell_a = CanonicalGridCell(
        grid_cell_id="A", longitude=1.0, latitude=1.0, canonical_ordinal=7
    )
    cell_b = CanonicalGridCell(
        grid_cell_id="B", longitude=2.0, latitude=1.0, canonical_ordinal=3
    )
    cell_c = CanonicalGridCell(
        grid_cell_id="C", longitude=3.0, latitude=1.0, canonical_ordinal=5
    )
    # Deliberately pass cells in NON-canonical-ordinal order to prove the
    # function sorts internally.
    result = assign_shud_forcing_index([cell_a, cell_b, cell_c])
    assert result == {"B": 1, "C": 2, "A": 3}, (
        "canonical_ordinal [3, 5, 7] -> shud_forcing_index [1, 2, 3]; "
        f"got {result!r}"
    )


def test_shud_forcing_index_reproducible_across_runs() -> None:
    """Two independent runs on same input -> identical shud_forcing_index dict.

    Also proves order-independence: reversing the input list must produce
    the same mapping (Python dict equality; both key set and value pairs
    identical).
    """
    cells = make_regular_grid_cells(
        lon0=9.9, lat0=19.9, lon_step=0.1, lat_step=0.1, lon_count=5, lat_count=1
    )
    result_1 = assign_shud_forcing_index(cells)
    result_2 = assign_shud_forcing_index(cells)
    assert result_1 == result_2, (
        "identical input MUST yield identical shud_forcing_index mapping "
        "(§7 determinism requirement)"
    )
    result_reversed = assign_shud_forcing_index(list(reversed(cells)))
    assert result_reversed == result_1, (
        "shud_forcing_index MUST be independent of input cell order"
    )


# --- direct unit tests for §2.2 helpers -----------------------------------


def test_resolve_tie_by_canonical_ordinal_empty_raises() -> None:
    """Empty candidate sequence -> MappingAlgorithmError.

    Guards the caller-owned "non-empty" contract documented on
    :func:`workers.mapping_builder.resolve_tie_by_canonical_ordinal`. The
    entry-point path (``nearest_cell_barycenter_geodesic_v1``) always passes at
    least the picked cell itself, so the empty branch is unreachable in
    production — but the helper is public API and must fail loudly rather than
    ``min()`` on an empty sequence when a future caller mis-wires it.
    """
    with pytest.raises(MappingAlgorithmError) as exc_info:
        resolve_tie_by_canonical_ordinal([])
    assert "at least one candidate" in str(exc_info.value)


def test_verify_half_cell_diagonal_sanity_bound_direct() -> None:
    """Direct within-/exceeding-bound coverage bypassing the entry point.

    Constructs a minimal ownership + cell + snapshot inline so the sanity-bound
    invariant gate is exercised without the mesh / CRS / G2 stack in the way.
    Symmetrically mirrors the direct coverage of :func:`derive_used_cell_subset`
    and :func:`assign_shud_forcing_index`.
    """
    cell = CanonicalGridCell(
        grid_cell_id="42",
        longitude=10.0,
        latitude=20.0,
        canonical_ordinal=1,
    )
    snapshot = make_snapshot(
        source_id="ifs",
        grid_id="direct_bound_grid",
        cells=[cell],
        bbox_pad=0.5,
        native_resolution=0.1,
    )
    # within-bound: mm-scale distance << km-scale half-diagonal for 0.1° step.
    ok_ownership = ElementOwnership(
        element_id=1,
        grid_cell_id=cell.grid_cell_id,
        canonical_ordinal=int(cell.canonical_ordinal),
        geodesic_distance_m=1.0e-4,
        tie_status="unique",
        candidate_count=1,
    )
    assert (
        verify_half_cell_diagonal_sanity_bound(
            ownership=ok_ownership, cell=cell, snapshot=snapshot
        )
        is None
    ), "within-bound distance must pass the sanity gate with no return value"

    # exceeding-bound: 99 km distance vs ~7.6 km half-diagonal at 20°N / 0.1°.
    bad_ownership = ElementOwnership(
        element_id=7,
        grid_cell_id=cell.grid_cell_id,
        canonical_ordinal=int(cell.canonical_ordinal),
        geodesic_distance_m=99_000.0,
        tie_status="unique",
        candidate_count=1,
    )
    with pytest.raises(DistanceSanityBoundExceededError) as exc_info:
        verify_half_cell_diagonal_sanity_bound(
            ownership=bad_ownership, cell=cell, snapshot=snapshot
        )
    err = exc_info.value
    assert err.element_id == 7
    assert err.distance_m == 99_000.0
    # Half-diagonal at 20°N for 0.1° step is ~7.6 km.
    assert 6_000.0 < err.half_cell_diagonal_m < 10_000.0
    assert err.tolerance_m == 1.0e-3
    assert err.grid_cell_id == "42"
    assert isinstance(err, MappingAlgorithmError)
