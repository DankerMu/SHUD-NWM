#!/usr/bin/env python3
"""Build the national static basin-domain outline GeoJSON from SHUD mesh shapefiles.

Source : <basins-root>/**/input/*/gis/domain.shp  (production layout)
         or legacy <repo-root>/SHUD/input/<basin>/gis/domain.shp when --basins-root
         is unset; per-basin .prj carries the CRS (projected or geographic).
Output : apps/frontend/public/geo/national-basin-domain.geojson (WGS84 / EPSG:4326)

The mesh is dissolved exactly: an edge shared by two triangles is interior, an
edge used once is on the domain boundary. Boundary edges are chained into closed
rings, then smoothed with Chaikin corner-cutting (the raw outline is a sawtooth
of mesh edges). The result keeps full mesh-resolution detail (~thousands of
vertices) instead of the previous ~200-vertex simplification, so the on-map
boundary reads as a smooth basin outline.

honest-display: smoothing only rounds mesh-edge corners (sub-edge-length
deviation); no geometry is fabricated. Basins with missing shp are skipped
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
# Quantize shared triangle vertices to one node. Snap unit follows the source CRS:
# projected -> metres; geographic (lon/lat degrees) -> ~1.1 m at 1e-5 deg.
NODE_SNAP_M = 1.0
GEOGRAPHIC_SNAP_DEG = 1e-5
CHAIKIN_ITERATIONS = 2


def _basin_id(name: str) -> str:
    return f"basins_{name.lower()}"


def _discover_basin_gis_dirs(basins_root: Path) -> list[tuple[str, Path]]:
    discovered: list[tuple[str, Path]] = []
    for shp in sorted(basins_root.glob("**/input/*/gis/domain.shp")):
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
        path = gis_dir / f"domain{suffix}"
        digest.update(suffix.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _select_model_package_gis_dir(name: str, candidates: list[Path]) -> Path:
    digests = {_shape_source_digest(path) for path in candidates}
    if len(digests) > 1:
        raise ValueError(f"ambiguous model packages for {name}: {len(candidates)} distinct domain sources")
    return sorted(candidates)[0]


def _discover_model_package_gis_dirs(model_packages_root: Path) -> list[tuple[str, Path]]:
    grouped: dict[str, list[Path]] = {}
    for shp in sorted(model_packages_root.glob("basins_*_shud/*/package/gis/domain.shp")):
        model_name = shp.parents[3].name
        if not model_name.startswith("basins_") or not model_name.endswith("_shud"):
            continue
        name = model_name.removeprefix("basins_").removesuffix("_shud")
        grouped.setdefault(name, []).append(shp.parent)
    return [(name, _select_model_package_gis_dir(name, candidates)) for name, candidates in sorted(grouped.items())]


def _named_basin_gis_dir(basins_root: Path, name: str) -> Path:
    candidates = sorted((basins_root / name).glob("input/*/gis/domain.shp"))
    if candidates:
        return candidates[0].parent
    if "_" in name:
        group, child = name.split("_", 1)
        candidates = sorted((basins_root / group).glob(f"{child.upper()}/input/*/gis/domain.shp"))
        if candidates:
            return candidates[0].parent
    return basins_root / name / "input" / name / "gis"


def _named_model_package_gis_dir(model_packages_root: Path, name: str) -> Path | None:
    candidates = [
        path.parent
        for path in sorted(model_packages_root.glob(f"basins_{name.lower()}_shud/*/package/gis/domain.shp"))
    ]
    return _select_model_package_gis_dir(name, candidates) if candidates else None


def _load_transformer(prj_path: Path) -> tuple[Transformer, float]:
    crs = CRS.from_wkt(prj_path.read_text().strip())
    transformer = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
    snap = GEOGRAPHIC_SNAP_DEG if crs.is_geographic else NODE_SNAP_M
    return transformer, snap


def _node_key(point: tuple[float, float], snap: float) -> tuple[int, int]:
    return (round(point[0] / snap), round(point[1] / snap))


def _polygon_rings(shape: shapefile.Shape) -> list[list[tuple[float, float]]]:
    points = [(float(x), float(y)) for x, y in shape.points]
    if not points:
        return []
    starts = list(shape.parts) or [0]
    bounds = starts + [len(points)]
    return [points[bounds[i] : bounds[i + 1]] for i in range(len(starts)) if bounds[i + 1] - bounds[i] >= 3]


def _boundary_rings(shapes: list[shapefile.Shape], snap: float) -> list[list[tuple[float, float]]]:
    """Dissolve mesh polygons: keep edges used exactly once, chain them into closed rings."""
    edge_count: dict[tuple[tuple[int, int], tuple[int, int]], int] = defaultdict(int)
    node_coord: dict[tuple[int, int], tuple[float, float]] = {}
    for shape in shapes:
        for ring in _polygon_rings(shape):
            closed = ring if ring[0] == ring[-1] else [*ring, ring[0]]
            for a, b in zip(closed, closed[1:]):
                ka, kb = _node_key(a, snap), _node_key(b, snap)
                if ka == kb:
                    continue
                node_coord.setdefault(ka, a)
                node_coord.setdefault(kb, b)
                edge_count[(ka, kb) if ka < kb else (kb, ka)] += 1

    adjacency: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for (ka, kb), count in edge_count.items():
        if count == 1:
            adjacency[ka].append(kb)
            adjacency[kb].append(ka)

    visited: set[frozenset[tuple[int, int]]] = set()
    rings: list[list[tuple[float, float]]] = []
    for start in list(adjacency):
        for first in adjacency[start]:
            if frozenset((start, first)) in visited:
                continue
            ring_keys = [start, first]
            visited.add(frozenset((start, first)))
            while ring_keys[-1] != start:
                current = ring_keys[-1]
                step = next(
                    (n for n in adjacency[current] if frozenset((current, n)) not in visited),
                    None,
                )
                if step is None:
                    break  # open chain (non-manifold artefact): drop
                visited.add(frozenset((current, step)))
                ring_keys.append(step)
            if ring_keys[-1] == start and len(ring_keys) >= 4:
                rings.append([node_coord[k] for k in ring_keys[:-1]])
    return rings


def _signed_area(ring: list[tuple[float, float]]) -> float:
    area = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _point_in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _chaikin_closed(ring: list[tuple[float, float]], iterations: int) -> list[tuple[float, float]]:
    points = ring
    for _ in range(iterations):
        smoothed: list[tuple[float, float]] = []
        for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
            smoothed.append((0.75 * x1 + 0.25 * x2, 0.75 * y1 + 0.25 * y2))
            smoothed.append((0.25 * x1 + 0.75 * x2, 0.25 * y1 + 0.75 * y2))
        points = smoothed
    return points


def _to_wgs84(ring: list[tuple[float, float]], transformer: Transformer, decimals: int) -> list[list[float]]:
    out: list[list[float]] = []
    prev: tuple[float, float] | None = None
    for x, y in ring:
        lon, lat = transformer.transform(x, y)
        rounded = (round(lon, decimals), round(lat, decimals))
        if rounded != prev:
            out.append([rounded[0], rounded[1]])
            prev = rounded
    if out and out[0] != out[-1]:
        out.append(list(out[0]))
    return out


def build_basin_feature(gis: Path, name: str, decimals: int) -> dict | None:
    shp, prj = gis / "domain.shp", gis / "domain.prj"
    if not shp.exists() or not prj.exists():
        print(f"[skip] {name}: missing {shp}/{prj.name}", file=sys.stderr)
        return None

    transformer, snap = _load_transformer(prj)
    reader = shapefile.Reader(str(shp))
    rings = _boundary_rings(reader.shapes(), snap)
    if not rings:
        print(f"[skip] {name}: no boundary rings dissolved", file=sys.stderr)
        return None

    rings.sort(key=lambda ring: abs(_signed_area(ring)), reverse=True)
    outer = rings[0]
    # Assemble outer + holes; rings outside the outer become extra polygons (rare islands).
    polygons: list[list[list[tuple[float, float]]]] = [[outer]]
    for ring in rings[1:]:
        if _point_in_ring(ring[0], outer):
            polygons[0].append(ring)
        else:
            polygons.append([ring])

    coordinates: list[list[list[list[float]]]] = []
    vertex_count = 0
    for polygon in polygons:
        wgs_rings: list[list[list[float]]] = []
        for ring in polygon:
            smoothed = _chaikin_closed(ring, CHAIKIN_ITERATIONS)
            wgs = _to_wgs84(smoothed, transformer, decimals)
            if len(wgs) >= 4:
                wgs_rings.append(wgs)
                vertex_count += len(wgs)
        if wgs_rings:
            coordinates.append(wgs_rings)

    print(f"[ok]   {name}: {len(rings)} rings -> {len(coordinates)} polygons, {vertex_count} vertices")
    return {
        "type": "Feature",
        "properties": {"basin_id": _basin_id(name)},
        "geometry": {"type": "MultiPolygon", "coordinates": coordinates},
    }


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
        help="output geojson (default apps/frontend/public/geo/national-basin-domain.geojson)",
    )
    parser.add_argument(
        "--decimals", type=int, default=5, help="coordinate decimal places (~1 m at 5, plenty for a basemap)"
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    basins_root = Path(args.basins_root).resolve() if args.basins_root else None
    model_packages_root = Path(args.model_packages_root).resolve() if args.model_packages_root else None
    out_path = Path(args.out) if args.out else repo_root / "apps/frontend/public/geo/national-basin-domain.geojson"

    def legacy_gis_dir(name: str) -> Path:
        if basins_root is not None:
            candidate = _named_basin_gis_dir(basins_root, name)
            if (candidate / "domain.shp").is_file():
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
    features = [
        feature
        for name, gis in basin_inputs
        if (feature := build_basin_feature(gis, name, args.decimals)) is not None
    ]
    if not features:
        print("[fail] no domain features generated", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")))
    print(f"[write] {out_path} ({out_path.stat().st_size // 1024} KiB, {len(features)} features)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
