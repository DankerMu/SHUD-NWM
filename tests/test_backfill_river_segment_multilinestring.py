"""Coverage for the DB-free logic of the river-segment re-split backfill script.

The DB write path is exercised by operations on node-22; here we pin the pure
re-split decision (the part that decides whether a row changes) so the script
stays idempotent and split-equivalent with the parser/frontend gap detector.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backfill_river_segment_multilinestring",
    Path(__file__).resolve().parents[1] / "scripts" / "backfill_river_segment_multilinestring.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_module)

_coordinates_from_geojson = _module._coordinates_from_geojson
_resplit_parts = _module._resplit_parts
_multilinestring_wkt = _module._multilinestring_wkt
_parts_equal = _module._parts_equal

LON = 98.5


def _vline(*lats: float) -> list[tuple[float, float]]:
    return [(LON, lat) for lat in lats]


def test_geojson_linestring_yields_single_part() -> None:
    geometry = {"type": "LineString", "coordinates": [[98.5, 38.0], [98.5, 38.0007]]}
    assert _coordinates_from_geojson(geometry) == [_vline(38.0, 38.0007)]


def test_geojson_multilinestring_preserves_parts_in_order() -> None:
    geometry = {
        "type": "MultiLineString",
        "coordinates": [[[98.5, 38.0], [98.5, 38.0007]], [[98.5, 38.02], [98.5, 38.0207]]],
    }
    assert _coordinates_from_geojson(geometry) == [_vline(38.0, 38.0007), _vline(38.02, 38.0207)]


def test_geojson_drops_higher_dimensions_for_metric() -> None:
    geometry = {"type": "LineString", "coordinates": [[98.5, 38.0, 100.0], [98.5, 38.0007, 110.0]]}
    assert _coordinates_from_geojson(geometry) == [_vline(38.0, 38.0007)]


def test_resplit_single_part_with_gap_splits() -> None:
    # migration leaves a single-part MLS that still carries the gap inside it
    parts = [_vline(38.0, 38.0007, 38.0014, 38.0164, 38.0171, 38.0178)]
    out = _resplit_parts(parts)
    assert out == [_vline(38.0, 38.0007, 38.0014), _vline(38.0164, 38.0171, 38.0178)]


def test_resplit_is_idempotent_on_already_split_parts() -> None:
    # an already-correctly-split row re-splits to the identical part set: concatenating
    # the parts re-introduces the same gap edge, which is detected and cut again.
    already = [_vline(38.0, 38.0007, 38.0014), _vline(38.0164, 38.0171, 38.0178)]
    out = _resplit_parts(already)
    assert out == already
    assert _parts_equal(out, already)


def test_resplit_returns_none_for_degenerate_geometry() -> None:
    assert _resplit_parts([[(98.5, 38.0)]]) is None
    assert _resplit_parts([]) is None


def test_multilinestring_wkt_round_trips_parts() -> None:
    parts = [_vline(38.0, 38.0007, 38.0014), _vline(38.0164, 38.0171)]
    wkt = _multilinestring_wkt(parts)
    assert wkt.startswith("MULTILINESTRING(")
    assert wkt.replace(" ", "").count("),(") == 1
