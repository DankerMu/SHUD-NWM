"""Requirement coverage for the backend river-segment gap-split helper.

``gap_split_positions`` / ``gap_split_multilinestring_wkt`` must agree EXACTLY with
the frontend ``splitPositionsAtGaps`` (apps/frontend/src/lib/m11/gapAwareGeometry.ts)
on the same physical judgement: a link longer than
``max(300m, 4 * median_edge)`` is a fabricated cross-gap bridge. The numeric cases
below mirror the frontend gapAwareGeometry.test.ts fixtures (latitude steps at
lon=98.5, where the cos factor is constant so distance is driven by Δlat only):
0.0007°≈78m normal mesh edge, 0.003°≈334m just over the 300m floor, 0.015°≈1668m
typical source gap, 0.004°≈445m uniform coarse edge.
"""

from __future__ import annotations

from workers.model_registry.basins_geometry import (
    RIVER_GAP_ABSOLUTE_M,
    gap_split_multilinestring_wkt,
    gap_split_positions,
)

LON = 98.5


def _vline(*lats: float) -> list[tuple[float, float]]:
    return [(LON, lat) for lat in lats]


def test_seamless_fine_line_stays_single_part() -> None:
    points = _vline(38.0, 38.0007, 38.0014, 38.0021, 38.0028)
    parts = gap_split_positions(points)
    assert parts == [points]


def test_single_gap_splits_into_two_parts_gap_in_no_part() -> None:
    # 78,78 | 1668(gap) | 78,78 -> the gap (38.0014->38.0164) is in neither part
    points = _vline(38.0, 38.0007, 38.0014, 38.0164, 38.0171, 38.0178)
    parts = gap_split_positions(points)
    assert parts == [_vline(38.0, 38.0007, 38.0014), _vline(38.0164, 38.0171, 38.0178)]
    for part in parts:
        for index in range(1, len(part)):
            assert part[index][1] - part[index - 1][1] < 0.01


def test_two_gaps_split_into_three_parts() -> None:
    points = _vline(38.0, 38.0007, 38.02, 38.0207, 38.04, 38.0407)
    parts = gap_split_positions(points)
    assert len(parts) == 3
    assert all(len(part) == 2 for part in parts)


def test_uniform_coarse_edges_are_not_split_by_relative_threshold() -> None:
    # ~445m uniform edges: threshold = 4 * 445 = ~1779m, so nothing splits.
    points = _vline(38.0, 38.004, 38.008, 38.012, 38.016)
    assert gap_split_positions(points) == [points]


def test_small_median_segment_splits_at_absolute_floor() -> None:
    # 56,56,56 | 334(gap) | 56 : relative 4*56=222m < 300m floor, so the 334m gap
    # is still cut by the absolute lower bound.
    points = _vline(38.0, 38.0005, 38.001, 38.0015, 38.0045, 38.005)
    parts = gap_split_positions(points)
    assert [len(part) for part in parts] == [4, 2]


def test_isolated_point_between_two_gaps_is_dropped() -> None:
    points = _vline(38.0, 38.0007, 38.0014, 38.02, 38.04, 38.0407, 38.0414, 38.0421)
    parts = gap_split_positions(points)
    assert parts == [_vline(38.0, 38.0007, 38.0014), _vline(38.04, 38.0407, 38.0414, 38.0421)]
    assert not any(point[1] == 38.02 for part in parts for point in part)


def test_degenerate_geometry_returns_single_part_without_raising() -> None:
    assert gap_split_positions(_vline(38.0)) == [_vline(38.0)]
    assert gap_split_positions([]) == [[]]


def test_absolute_floor_constant_matches_contract() -> None:
    assert RIVER_GAP_ABSOLUTE_M == 300.0


def test_multilinestring_wkt_single_part_is_still_multilinestring() -> None:
    points = _vline(38.0, 38.0007, 38.0014)
    wkt = gap_split_multilinestring_wkt(points)
    assert wkt.startswith("MULTILINESTRING((")
    # one part -> exactly one inner ring (no second "),(")
    assert "),(" not in wkt.replace(" ", "")


def test_multilinestring_wkt_splits_gap_into_two_rings() -> None:
    points = _vline(38.0, 38.0007, 38.0014, 38.0164, 38.0171, 38.0178)
    wkt = gap_split_multilinestring_wkt(points)
    assert wkt.startswith("MULTILINESTRING(")
    # two parts -> two inner rings
    assert wkt.replace(" ", "").count("),(") == 1
    # the gap endpoints 38.0014 and 38.0164 are never adjacent within a ring
    assert "38.0014,98.5 38.0164" not in wkt.replace(" 38", " 38").replace(", ", ",")


def test_multilinestring_wkt_degenerate_falls_back_to_single_ring() -> None:
    # A 2-point line with no gap is one ring; a sub-2-point fallback never returns
    # an empty MULTILINESTRING.
    wkt = gap_split_multilinestring_wkt(_vline(38.0, 38.0007))
    assert wkt.startswith("MULTILINESTRING((")
    assert wkt.endswith("))")
