#!/usr/bin/env python3
"""Build the national static river-network GeoJSON from each basin's SHUD shapefile.

Source : <basins-root>/**/input/*/gis/river.shp  (production layout)
         or legacy <repo-root>/SHUD/input/<basin>/gis/river.shp when --basins-root
         is unset; per-basin .prj carries the CRS (projected or geographic).
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
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import shapefile  # pyshp
from pyproj import CRS, Transformer

DEFAULT_BASINS = ()
MAX_TYPE = 5
# Quantize shared river endpoints to one node. Snap unit follows the source CRS:
# projected -> metres; geographic (lon/lat degrees) -> ~1.1 m at 1e-5 deg.
NODE_SNAP_M = 1.0
GEOGRAPHIC_SNAP_DEG = 1e-5


def _basin_id(name: str) -> str:
    return f"basins_{name.lower()}"


def _discover_basin_gis_dirs(basins_root: Path) -> list[tuple[str, Path]]:
    discovered: list[tuple[str, Path]] = []
    for shp in sorted(basins_root.glob("**/input/*/gis/river.shp")):
        try:
            relative = shp.relative_to(basins_root)
        except ValueError:
            continue
        parts = relative.parts
        if len(parts) < 5 or parts[-4] != "input" or parts[-2] != "gis":
            continue
        basin_name = parts[0]
        if basin_name == "zhaochen" and len(parts) >= 6:
            basin_name = f"{parts[0]}_{parts[1].lower()}"
        discovered.append((basin_name, shp.parent))
    return discovered


def _shape_source_digest(gis_dir: Path) -> str:
    digest = hashlib.sha256()
    for suffix in (".shp", ".shx", ".dbf", ".prj"):
        path = gis_dir / f"river{suffix}"
        digest.update(suffix.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _select_model_package_gis_dir(name: str, candidates: list[Path]) -> Path:
    digests = {_shape_source_digest(path) for path in candidates}
    if len(digests) > 1:
        raise ValueError(f"ambiguous model packages for {name}: {len(candidates)} distinct river sources")
    return sorted(candidates)[0]


def _discover_model_package_gis_dirs(model_packages_root: Path) -> list[tuple[str, Path]]:
    grouped: dict[str, list[Path]] = {}
    for shp in sorted(model_packages_root.glob("basins_*_shud/*/package/gis/river.shp")):
        model_name = shp.parents[3].name
        if not model_name.startswith("basins_") or not model_name.endswith("_shud"):
            continue
        name = model_name.removeprefix("basins_").removesuffix("_shud")
        grouped.setdefault(name, []).append(shp.parent)
    return [(name, _select_model_package_gis_dir(name, candidates)) for name, candidates in sorted(grouped.items())]


def _named_basin_gis_dir(basins_root: Path, name: str) -> Path:
    candidates = sorted((basins_root / name).glob("input/*/gis/river.shp"))
    if candidates:
        return candidates[0].parent
    if "_" in name:
        group, child = name.split("_", 1)
        candidates = sorted((basins_root / group).glob(f"{child.upper()}/input/*/gis/river.shp"))
        if candidates:
            return candidates[0].parent
    return basins_root / name / "input" / name / "gis"


def _named_model_package_gis_dir(model_packages_root: Path, name: str) -> Path | None:
    candidates = [
        path.parent
        for path in sorted(model_packages_root.glob(f"basins_{name.lower()}_shud/*/package/gis/river.shp"))
    ]
    return _select_model_package_gis_dir(name, candidates) if candidates else None


def _load_transformer(prj_path: Path) -> tuple[Transformer, float]:
    crs = CRS.from_wkt(prj_path.read_text().strip())
    transformer = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
    snap = GEOGRAPHIC_SNAP_DEG if crs.is_geographic else NODE_SNAP_M
    return transformer, snap


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


def _node_key(point: tuple[float, float], snap: float) -> tuple[int, int]:
    return (round(point[0] / snap), round(point[1] / snap))


def _strahler_orders(segments: list[list[tuple[float, float]]], snap: float) -> list[int]:
    """Strahler order per segment using point order (first=upstream, last=downstream)."""
    up_node = [_node_key(seg[0], snap) for seg in segments]
    down_node = [_node_key(seg[-1], snap) for seg in segments]
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


def build_basin_features(gis: Path, name: str, decimals: int) -> list[dict]:
    shp, prj = gis / "river.shp", gis / "river.prj"
    if not shp.exists() or not prj.exists():
        print(f"[skip] {name}: missing {shp}/{prj.name}", file=sys.stderr)
        return []

    transformer, snap = _load_transformer(prj)
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
        strahler = _strahler_orders(seg_parts, snap)
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
        "--basins",
        default=",".join(DEFAULT_BASINS),
        help="comma-separated basin names; defaults to auto-discovery when --basins-root is set",
    )
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument(
        "--basins-root",
        default=None,
        help="production Basins root; per-basin gis resolved as <root>/<name>/input/<name>/gis. "
        "If unset, legacy <repo-root>/SHUD/input/<name>/gis.",
    )
    parser.add_argument(
        "--model-packages-root",
        default=None,
        help="optional object-store models root; used when a basin is absent from --basins-root",
    )
    parser.add_argument(
        "--exclude-basins",
        default="",
        help="comma-separated basin names excluded from the generated display layer",
    )
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
    basins_root = Path(args.basins_root).resolve() if args.basins_root else None
    model_packages_root = Path(args.model_packages_root).resolve() if args.model_packages_root else None
    out_path = Path(args.out) if args.out else repo_root / "apps/frontend/public/geo/national-basin-river.geojson"

    def legacy_gis_dir(name: str) -> Path:
        if basins_root is not None:
            candidate = _named_basin_gis_dir(basins_root, name)
            if (candidate / "river.shp").is_file():
                return candidate
        if model_packages_root is not None:
            candidate = _named_model_package_gis_dir(model_packages_root, name)
            if candidate is not None:
                return candidate
        return repo_root / "SHUD" / "input" / name / "gis"

    excluded = {item.strip().lower() for item in args.exclude_basins.split(",") if item.strip()}
    requested_names = [b.strip() for b in args.basins.split(",") if b.strip()]
    if requested_names:
        basin_inputs = [(name, legacy_gis_dir(name)) for name in requested_names if name.lower() not in excluded]
    elif basins_root is not None or model_packages_root is not None:
        discovered = {}
        if model_packages_root is not None:
            discovered.update(_discover_model_package_gis_dirs(model_packages_root))
        if basins_root is not None:
            discovered.update(_discover_basin_gis_dirs(basins_root))
        basin_inputs = [(name, gis) for name, gis in sorted(discovered.items()) if name.lower() not in excluded]
    else:
        basin_inputs = [(name, legacy_gis_dir(name)) for name in ("qhh", "heihe")]
    features: list[dict] = []
    for name, gis in basin_inputs:
        features.extend(build_basin_features(gis, name, args.decimals))

    if not features:
        print("[fail] no river features generated", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")))
    print(f"[write] {out_path} ({out_path.stat().st_size // 1024} KiB, {len(features)} features)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
