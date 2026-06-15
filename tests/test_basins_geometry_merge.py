"""Unit coverage for `_merge_polyline_parts` multi-part stitching.

Regression guard for the heihe "cross-ridge straight line" symptom: a
multi-part shapefile polyline must be stitched by nearest shared endpoints
(reordering / reversing parts), never by raw storage order, so the merged line
never contains a fabricated long jump.
"""

from __future__ import annotations

from workers.model_registry.basins_geometry import _merge_polyline_parts


def _max_edge(points: list[tuple[float, float]]) -> float:
    return max(
        ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
        for a, b in zip(points, points[1:], strict=False)
    )


def test_single_part_is_returned_unchanged() -> None:
    part = [(0.0, 0.0), (1.0, 0.0), (2.0, 1.0)]
    assert _merge_polyline_parts([part]) == part


def test_in_order_parts_join_without_duplicating_shared_joints() -> None:
    parts = [[(0.0, 0.0), (1.0, 0.0)], [(1.0, 0.0), (2.0, 0.0)], [(2.0, 0.0), (3.0, 0.0)]]
    assert _merge_polyline_parts(parts) == [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]


def test_reversed_part_is_flipped_instead_of_jumping() -> None:
    # middle part stored end-first; old blind concat drew a jump back and forth
    parts = [[(0.0, 0.0), (1.0, 0.0)], [(2.0, 0.0), (1.0, 0.0)], [(2.0, 0.0), (3.0, 0.0)]]
    merged = _merge_polyline_parts(parts)
    assert merged == [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]
    assert _max_edge(merged) == 1.0


def test_out_of_storage_order_parts_are_reordered_by_proximity() -> None:
    # parts stored 0,2,1; must reorder to 0,1,2 by shared endpoints
    parts = [[(0.0, 0.0), (1.0, 0.0)], [(2.0, 0.0), (3.0, 0.0)], [(1.0, 0.0), (2.0, 0.0)]]
    merged = _merge_polyline_parts(parts)
    assert merged == [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]
    assert _max_edge(merged) == 1.0


def test_disjoint_parts_join_by_shortest_link_not_storage_order() -> None:
    near = [(0.0, 0.0), (1.0, 0.0)]
    far = [(10.0, 0.0), (11.0, 0.0)]
    merged = _merge_polyline_parts([near, far])
    # genuinely disjoint: link the two NEAREST endpoints (1,0)->(10,0), never (1,0)->(11,0)
    assert merged == [(0.0, 0.0), (1.0, 0.0), (10.0, 0.0), (11.0, 0.0)]
