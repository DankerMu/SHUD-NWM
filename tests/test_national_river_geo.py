"""Strahler stream-order logic for the national river basemap generator.

The generator (scripts/geo/build_national_river_geo.py) derives a 1..5 `Type`
grade for basins whose shp lacks one, by walking the LineString point order
(first=upstream, last=downstream). These tests pin the ordering contract
independent of any real shapefile.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("shapefile")
pytest.importorskip("pyproj")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "geo" / "build_national_river_geo.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_national_river_geo", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = _load_module()


def test_single_chain_stays_order_one() -> None:
    # A->B->C with no confluence: every segment is a first-order stream.
    segments = [[(0.0, 0.0), (100.0, 0.0)], [(100.0, 0.0), (200.0, 0.0)]]
    assert MOD._strahler_orders(segments, MOD.NODE_SNAP_M) == [1, 1]


def test_two_first_order_streams_merge_to_second_order() -> None:
    # Two order-1 streams meeting at (100,100) raise the downstream to order 2.
    segments = [
        [(0.0, 0.0), (100.0, 100.0)],
        [(200.0, 0.0), (100.0, 100.0)],
        [(100.0, 100.0), (100.0, 200.0)],
    ]
    orders = MOD._strahler_orders(segments, MOD.NODE_SNAP_M)
    assert orders[0] == 1 and orders[1] == 1
    assert orders[2] == 2


def test_unequal_tributary_keeps_higher_order() -> None:
    # An order-2 trunk joined by a single order-1 tributary stays order 2 (Strahler rule).
    segments = [
        [(0.0, 0.0), (50.0, 50.0)],  # order 1
        [(100.0, 0.0), (50.0, 50.0)],  # order 1
        [(50.0, 50.0), (50.0, 150.0)],  # order 2 (two 1s merge)
        [(200.0, 150.0), (50.0, 150.0)],  # order 1 tributary
        [(50.0, 150.0), (50.0, 300.0)],  # 2 + 1 -> stays 2
    ]
    orders = MOD._strahler_orders(segments, MOD.NODE_SNAP_M)
    assert orders[2] == 2
    assert orders[4] == 2


def test_quantization_cycle_does_not_hang() -> None:
    # A degenerate self-referential pair (cycle) must resolve, not recurse forever.
    segments = [[(0.0, 0.0), (10.0, 0.0)], [(10.0, 0.0), (0.0, 0.0)]]
    orders = MOD._strahler_orders(segments, MOD.NODE_SNAP_M)
    assert all(order >= 1 for order in orders)


def test_geographic_snap_confluence_at_degree_coords() -> None:
    # CRS-aware snap: two order-1 streams sharing a lon/lat confluence raise the
    # downstream to order 2 under the geographic snap (1e-5 deg). The projected
    # 1.0 m snap would round whole degrees to one bucket and falsely fuse nodes.
    node = (101.5, 38.2)
    segments = [
        [(101.0, 38.0), node],
        [(102.0, 38.0), node],
        [node, (101.5, 39.0)],
    ]
    orders = MOD._strahler_orders(segments, MOD.GEOGRAPHIC_SNAP_DEG)
    assert orders[0] == 1 and orders[1] == 1
    assert orders[2] == 2
