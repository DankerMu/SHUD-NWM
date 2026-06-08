#!/usr/bin/env python3
"""Build the national static river-network GeoJSON from each basin's SHUD shapefile.

Source : SHUD/input/<basin>/gis/river.shp (projected CRS, per-basin .prj)
Output : apps/frontend/public/geo/national-basin-river.geojson (WGS84 / EPSG:4326)

The frontend renders this as an always-on basemap layer and uses the `Type` field
(1..5, 5 = trunk) to filter/scale rivers by zoom, so the river network shows
instantly without waiting on the (slow) discharge run/layer APIs.

Stream order (Type):
- shp carries a `Type` column (e.g. qhh) -> rounded and clamped to 1..5.
- shp has no `Type` (e.g. heihe/ccw)     -> Strahler order computed from the
  LineString point order (SHUD draws upstream->downstream), normalized to 1..5.

honest-display: never fabricate. A basin whose shp/field is missing is skipped
with a warning rather than guessed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import shapefile  # pyshp
from pyproj import CRS, Transformer

DEFAULT_BASINS = ("qhh", "heihe")
MAX_TYPE = 5
# Snap projected metres to ~1 m so shared river endpoints quantize to one node.
NODE_SNAP_M = 1.0


def _basin_id(name: str) -> str:
    return f"basins_{name}"


def _load_transformer(prj_path: Path) -> Transformer:
    crs = CRS.from_wkt(prj_path.read_text().strip())
    return Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)


def _type_field_index(reader: shapefile.Reader) -> int | None:
    for idx, field in enumerate(reader.fields[1:]):  # skip DeletionFlag
        if field[0].lower() == "type":
            return idx
    return None


def _line_parts(shape: shapefile.Shape) -> list[list[tuple[float, float]]]:
    points = [(float(x), float(y)) for x, y in shape.points]
    if not points:
        return []
    starts = list(shape.parts) or [0]
    bounds = starts + [len(points)]
    return [points[bounds[i] : bounds[i + 1]] for i in range(len(starts)) if bounds[i + 1] - bounds[i] >= 2]


def _node_key(point: tuple[float, float]) -> tuple[int, int]:
    return (round(point[0] / NODE_SNAP_M), round(point[1] / NODE_SNAP_M))


def _strahler_orders(segments: list[list[tuple[float, float]]]) -> list[int]:
    """Strahler order per segment using point order (first=upstream, last=downstream)."""
    up_node = [_node_key(seg[0]) for seg in segments]
    down_node = [_node_key(seg[-1]) for seg in segments]
    incoming: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, node in enumerate(down_node):
        incoming[node].append(i)

    orders: list[int | None] = [None] * len(segments)

    def resolve(i: int, stack: frozenset[int]) -> int:
        if orders[i] is not None:
            return orders[i]  # type: ignore[return-value]
        if i in stack:  # cycle guard (quantization artefact): treat as headwater
            return 1
        ups = incoming.get(up_node[i], [])
        if not ups:
            orders[i] = 1
            return 1
        child = [resolve(j, stack | {i}) for j in ups]
        top = max(child)
        orders[i] = top + 1 if child.count(top) >= 2 else top
        return orders[i]  # type: ignore[return-value]

    sys.setrecursionlimit(max(10000, len(segments) * 4))
    return [resolve(i, frozenset()) for i in range(len(segments))]


def _round_coords(part: list[tuple[float, float]], transformer: Transformer, decimals: int) -> list[list[float]]:
    out: list[list[float]] = []
    prev: tuple[float, float] | None = None
    for x, y in part:
        lon, lat = transformer.transform(x, y)
        rounded = (round(lon, decimals), round(lat, decimals))
        if rounded != prev:  # drop coincident points after rounding
            out.append([rounded[0], rounded[1]])
            prev = rounded
    return out


def build_basin_features(repo_root: Path, name: str, decimals: int) -> list[dict]:
    gis = repo_root / "SHUD" / "input" / name / "gis"
    shp, prj = gis / "river.shp", gis / "river.prj"
    if not shp.exists() or not prj.exists():
        print(f"[skip] {name}: missing {shp.name}/{prj.name}", file=sys.stderr)
        return []

    transformer = _load_transformer(prj)
    reader = shapefile.Reader(str(shp))
    type_idx = _type_field_index(reader)

    records = list(reader.shapeRecords())
    seg_parts: list[list[tuple[float, float]]] = []
    seg_owner: list[int] = []  # index back into records for the Type field
    for ri, sr in enumerate(records):
        for part in _line_parts(sr.shape):
            seg_parts.append(part)
            seg_owner.append(ri)

    if type_idx is not None:
        types = [max(1, min(MAX_TYPE, int(round(float(records[o].record[type_idx] or 1))))) for o in seg_owner]
        source = "shp:Type"
    else:
        strahler = _strahler_orders(seg_parts)
        types = [max(1, min(MAX_TYPE, s)) for s in strahler]
        source = "strahler"

    features: list[dict] = []
    for part, type_value in zip(seg_parts, types):
        coords = _round_coords(part, transformer, decimals)
        if len(coords) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {"basin_id": _basin_id(name), "Type": type_value},
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )
    dist = {t: types.count(t) for t in range(1, MAX_TYPE + 1)}
    print(f"[ok]   {name}: {len(features)} segments ({source}) Type dist {dist}")
    return features


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--basins", default=",".join(DEFAULT_BASINS), help="comma-separated basin names under SHUD/input"
    )
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument(
        "--out",
        default=None,
        help="output geojson (default apps/frontend/public/geo/national-basin-river.geojson)",
    )
    parser.add_argument(
        "--decimals", type=int, default=5, help="coordinate decimal places (~1 m at 5, plenty for a basemap)"
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    out_path = Path(args.out) if args.out else repo_root / "apps/frontend/public/geo/national-basin-river.geojson"

    features: list[dict] = []
    for name in [b.strip() for b in args.basins.split(",") if b.strip()]:
        features.extend(build_basin_features(repo_root, name, args.decimals))

    if not features:
        print("[fail] no river features generated", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")))
    print(f"[write] {out_path} ({out_path.stat().st_size // 1024} KiB, {len(features)} features)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
