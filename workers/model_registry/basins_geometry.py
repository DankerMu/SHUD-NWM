from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHAPEFILE_REQUIRED_SUFFIXES = ("shp", "shx", "dbf", "prj")
SHUD_CANONICAL_SUFFIXES = {
    "cfg_para": ".cfg.para",
    "cfg_ic": ".cfg.ic",
    "cfg_calib": ".cfg.calib",
    "sp_mesh": ".sp.mesh",
    "sp_riv": ".sp.riv",
    "sp_rivseg": ".sp.rivseg",
    "sp_att": ".sp.att",
    "para_soil": ".para.soil",
    "para_geol": ".para.geol",
    "para_lc": ".para.lc",
    "tsd_forc": ".tsd.forc",
    "tsd_lai": ".tsd.lai",
    "tsd_mf": ".tsd.mf",
    "tsd_rl": ".tsd.rl",
}
MAX_BASINS_GIS_FEATURES = 250_000
MAX_BASINS_GIS_POINTS = 5_000_000


class BasinsGeometryError(RuntimeError):
    """Raised when Basins GIS or SHUD evidence cannot be parsed safely."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.path = path
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.path is not None:
            payload["path"] = self.path
        payload.update(self.details)
        return payload


@dataclass(frozen=True)
class RiverSegmentGeometry:
    river_segment_id: str
    segment_order: int
    downstream_segment_id: str | None
    length_m: float | None
    geom_wkt: str
    properties: dict[str, Any]


@dataclass(frozen=True)
class ParsedBasinsGeometry:
    domain_wkt: str
    domain_checksum: str
    domain_source_uri: str
    river_segments: list[RiverSegmentGeometry]
    river_network_checksum: str
    river_network_source_uri: str
    segment_count: int
    evidence_counts: dict[str, int]


def parse_basins_geometry(
    *,
    model_id: str,
    input_dir: Path,
    shud_input_name: str,
    required_files: dict[str, Any],
) -> ParsedBasinsGeometry:
    """Parse domain and river geometry from inventory-referenced Basins files."""

    _validate_required_files_canonical(required_files, shud_input_name, input_dir)
    input_root = input_dir.resolve()
    domain_base = _validated_layer_base(input_root, "domain", required_files)
    river_base = _validated_layer_base(input_root, "river", required_files)
    seg_base = _validated_layer_base(input_root, "seg", required_files)
    _validate_prj(domain_base)
    _validate_prj(river_base)
    _validate_prj(seg_base)

    sp_riv = _required_input_file(input_root, required_files, "sp_riv", shud_input_name)
    sp_rivseg = _required_input_file(input_root, required_files, "sp_rivseg", shud_input_name)
    domain_wkt = _domain_multipolygon_wkt(domain_base)

    river_segments = _river_segments_from_layer(seg_base, model_id=model_id)
    selected_base = seg_base
    if not river_segments:
        river_segments = _river_segments_from_layer(river_base, model_id=model_id)
        selected_base = river_base
    if not river_segments:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_PARSE_FAILED",
            "Basins river/seg shapefile did not contain any LineString features.",
            path=str(seg_base.with_suffix(".shp")),
        )

    evidence_counts = {
        "sp_riv": _shud_segment_count(sp_riv),
        "sp_rivseg": _shud_segment_count(sp_rivseg),
    }
    for evidence_name, evidence_count in evidence_counts.items():
        if evidence_count != len(river_segments):
            raise BasinsGeometryError(
                "BASINS_REGISTRY_SEGMENT_COUNT_MISMATCH",
                "Basins river segment count does not match SHUD evidence.",
                path=str(sp_riv if evidence_name == "sp_riv" else sp_rivseg),
                details={
                    "model_id": model_id,
                    "gis_segment_count": len(river_segments),
                    "evidence_count": evidence_count,
                    "evidence": evidence_name,
                },
            )

    return ParsedBasinsGeometry(
        domain_wkt=domain_wkt,
        domain_checksum=_layer_checksum(domain_base),
        domain_source_uri=str(domain_base.with_suffix(".shp")),
        river_segments=river_segments,
        river_network_checksum=_river_network_checksum(selected_base, sp_riv, sp_rivseg),
        river_network_source_uri=str(selected_base.with_suffix(".shp")),
        segment_count=len(river_segments),
        evidence_counts=evidence_counts,
    )


def _validated_layer_base(input_dir: Path, layer: str, required_files: dict[str, Any]) -> Path:
    for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
        role = f"gis_{layer}_{suffix}"
        expected = f"gis/{layer}.{suffix}"
        values = required_files.get(role)
        if values != [expected]:
            raise BasinsGeometryError(
                "BASINS_REGISTRY_GIS_SIDECAR_MISSING",
                f"Basins inventory is missing required GIS sidecar role {role}.",
                path=str(input_dir / expected),
                details={"missing_sidecar": expected, "role": role},
            )
        path = input_dir / expected
        _validate_safe_file(path, input_dir, role=role, error_code="BASINS_REGISTRY_GIS_SIDECAR_MISSING")
        if not path.is_file():
            raise BasinsGeometryError(
                "BASINS_REGISTRY_GIS_SIDECAR_MISSING",
                f"Basins GIS sidecar is missing: {expected}",
                path=str(path),
                details={"missing_sidecar": expected, "role": role},
            )
    return input_dir / "gis" / layer


def _required_input_file(input_dir: Path, required_files: dict[str, Any], role: str, shud_input_name: str) -> Path:
    values = required_files.get(role)
    expected = f"{shud_input_name}{SHUD_CANONICAL_SUFFIXES[role]}"
    if not isinstance(values, list) or values != [expected]:
        raise BasinsGeometryError(
            "BASINS_REQUIRED_FILES_NON_CANONICAL",
            f"Basins inventory source role {role} must be canonical: {expected}.",
            path=str(input_dir),
            details={"role": role},
        )
    path = input_dir / expected
    _validate_safe_file(path, input_dir, role=role, error_code="BASINS_REGISTRY_SOURCE_MISSING")
    if not path.is_file():
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            f"Basins source file is missing for role {role}.",
            path=str(path),
            details={"role": role},
        )
    return path


def _validate_prj(layer_base: Path) -> None:
    prj_path = layer_base.with_suffix(".prj")
    _validate_safe_file(
        prj_path,
        layer_base.parent.parent,
        role="prj",
        error_code="BASINS_REGISTRY_GIS_CRS_UNSUPPORTED",
    )
    text = prj_path.read_text(encoding="utf-8", errors="ignore")
    normalized = re.sub(r"[\s_]+", "", text.upper())
    is_projected = "PROJCS[" in normalized or "PROJCRS[" in normalized or "PROJECTION[" in normalized
    is_wgs84 = "GEOGCS[\"GCSWGS1984\"" in normalized or "DATUM[\"DWGS1984\"" in normalized
    is_epsg4326 = "AUTHORITY[\"EPSG\",\"4326\"]" in normalized or "EPSG\",\"4326" in normalized
    is_cgcs2000 = (
        "CGCS2000" in normalized
        or "CHINAGEODETICCOORDINATESYSTEM2000" in normalized
        or "AUTHORITY[\"EPSG\",\"4490\"]" in normalized
        or "EPSG\",\"4490" in normalized
    )
    if is_projected or not (is_wgs84 or is_epsg4326 or is_cgcs2000):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_CRS_UNSUPPORTED",
            "Basins shapefile projection must be compatible with SRID 4490.",
            path=str(prj_path),
        )


def _domain_multipolygon_wkt(layer_base: Path) -> str:
    reader = _shape_reader(layer_base)
    polygons: list[str] = []
    feature_count = 0
    point_count = 0
    for shape in reader.iterShapes():
        feature_count += 1
        _check_resource_limit(feature_count, MAX_BASINS_GIS_FEATURES, "features", str(layer_base.with_suffix(".shp")))
        shape_rings: list[list[tuple[float, float]]] = []
        for ring in _shape_parts(shape):
            point_count += len(ring)
            _check_resource_limit(point_count, MAX_BASINS_GIS_POINTS, "points", str(layer_base.with_suffix(".shp")))
            closed = _closed_ring(ring)
            if len(closed) >= 4:
                shape_rings.append(closed)
        polygons.extend(_polygon_wkts_from_rings(shape_rings))
    if not polygons:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_PARSE_FAILED",
            "Basins domain shapefile did not contain a non-empty polygon.",
            path=str(layer_base.with_suffix(".shp")),
        )
    return "MULTIPOLYGON(" + ", ".join(polygons) + ")"


def _river_segments_from_layer(layer_base: Path, *, model_id: str) -> list[RiverSegmentGeometry]:
    reader = _shape_reader(layer_base)
    pending: list[dict[str, Any]] = []
    feature_count = 0
    point_count = 0
    for record_index, shape_record in enumerate(reader.iterShapeRecords(), start=1):
        feature_count += 1
        _check_resource_limit(feature_count, MAX_BASINS_GIS_FEATURES, "features", str(layer_base.with_suffix(".shp")))
        attrs = _record_dict(shape_record)
        parts = _shape_parts(shape_record.shape)
        for part_index, points in enumerate(parts, start=1):
            point_count += len(points)
            _check_resource_limit(point_count, MAX_BASINS_GIS_POINTS, "points", str(layer_base.with_suffix(".shp")))
            if len(points) < 2:
                continue
            order = _optional_int(_pick_attr(attrs, ("segment_order", "stream_order", "order", "ord", "seg_order")))
            segment_order = order if order is not None else len(pending) + 1
            raw_id = _pick_attr(attrs, ("river_segment_id", "segment_id", "seg_id", "segid", "comid", "linkno", "id"))
            multi_part_index = part_index if len(parts) > 1 else None
            segment_id = _stable_segment_id(model_id, raw_id, segment_order, multi_part_index)
            downstream = _optional_text(
                _pick_attr(attrs, ("downstream_segment_id", "downstream", "down_id", "to_segment", "toid", "dslinkno"))
            )
            length_m = _optional_float(_pick_attr(attrs, ("length_m", "length", "len_m", "shape_leng", "shapeleng")))
            if length_m is None:
                length_m = _approximate_length_m(points)
            properties = {str(key): _jsonable(value) for key, value in attrs.items()}
            properties.update(
                {
                    "source_layer": layer_base.name,
                    "source_record_index": record_index,
                    "source_part_index": part_index,
                    "source_raw_segment_id": _jsonable(raw_id),
                    "source_downstream_segment_id": _jsonable(downstream),
                }
            )
            pending.append(
                {
                    "river_segment_id": segment_id,
                    "segment_order": segment_order,
                    "raw_id": raw_id,
                    "raw_downstream": downstream,
                    "length_m": length_m,
                    "geom_wkt": "LINESTRING(" + ", ".join(_point_wkt(point) for point in points) + ")",
                    "properties": properties,
                }
            )
    raw_to_segment_id = {
        key: item["river_segment_id"]
        for item in pending
        if (key := _raw_segment_key(item["raw_id"])) is not None
    }
    segment_ids = {item["river_segment_id"] for item in pending}
    segments: list[RiverSegmentGeometry] = []
    for item in pending:
        downstream_segment_id = _mapped_downstream_segment_id(
            item["raw_downstream"],
            raw_id=item["raw_id"],
            river_segment_id=item["river_segment_id"],
            raw_to_segment_id=raw_to_segment_id,
            segment_ids=segment_ids,
        )
        segments.append(
            RiverSegmentGeometry(
                river_segment_id=item["river_segment_id"],
                segment_order=item["segment_order"],
                downstream_segment_id=downstream_segment_id,
                length_m=item["length_m"],
                geom_wkt=item["geom_wkt"],
                properties=item["properties"],
            )
        )
    return segments


def _shape_reader(layer_base: Path) -> Any:
    try:
        import shapefile
    except ImportError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SHAPEFILE_DEPENDENCY_MISSING",
            "pyshp is required for Basins shapefile parsing.",
            path=str(layer_base.with_suffix(".shp")),
        ) from error
    try:
        input_root = layer_base.parent.parent
        for suffix in ("shp", "shx", "dbf"):
            _validate_safe_file(
                layer_base.with_suffix(f".{suffix}"),
                input_root,
                role=f"gis_{layer_base.name}_{suffix}",
                error_code="BASINS_REGISTRY_GIS_SIDECAR_MISSING",
            )
        return shapefile.Reader(
            shp=str(layer_base.with_suffix(".shp")),
            shx=str(layer_base.with_suffix(".shx")),
            dbf=str(layer_base.with_suffix(".dbf")),
        )
    except BasinsGeometryError:
        raise
    except Exception as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_PARSE_FAILED",
            f"Failed to parse Basins shapefile: {error}",
            path=str(layer_base.with_suffix(".shp")),
        ) from error


def _shape_parts(shape: Any) -> list[list[tuple[float, float]]]:
    points = [(float(x), float(y)) for x, y, *_rest in shape.points]
    if not points:
        return []
    part_starts = list(shape.parts) + [len(points)]
    return [points[start:end] for start, end in zip(part_starts, part_starts[1:], strict=False) if end > start]


def _closed_ring(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not points:
        return points
    if points[0] == points[-1]:
        return points
    return [*points, points[0]]


def _polygon_wkts_from_rings(rings: list[list[tuple[float, float]]]) -> list[str]:
    polygons: list[dict[str, Any]] = []
    for ring in sorted(rings, key=lambda item: abs(_ring_area(item)), reverse=True):
        containing_index = None
        for index, polygon in enumerate(polygons):
            if _point_in_ring(ring[0], polygon["outer"]):
                containing_index = index
                break
        if containing_index is None:
            polygons.append({"outer": ring, "holes": []})
        else:
            polygons[containing_index]["holes"].append(ring)
    return [
        "(" + ", ".join(
            "(" + ", ".join(_point_wkt(point) for point in ring) + ")"
            for ring in [polygon["outer"], *polygon["holes"]]
        ) + ")"
        for polygon in polygons
    ]


def _ring_area(points: list[tuple[float, float]]) -> float:
    area = 0.0
    for first, second in zip(points, points[1:], strict=False):
        area += first[0] * second[1] - second[0] * first[1]
    return area / 2.0


def _point_in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for first, second in zip(ring, ring[1:], strict=False):
        x1, y1 = first
        x2, y2 = second
        intersects = (y1 > y) != (y2 > y) and x < ((x2 - x1) * (y - y1) / ((y2 - y1) or 1e-30) + x1)
        if intersects:
            inside = not inside
    return inside


def _record_dict(shape_record: Any) -> dict[str, Any]:
    try:
        return dict(shape_record.record.as_dict())
    except AttributeError:
        return {}


def _pick_attr(attrs: dict[str, Any], names: tuple[str, ...]) -> Any:
    normalized = {_normalize_attr_name(key): value for key, value in attrs.items()}
    for name in names:
        value = normalized.get(_normalize_attr_name(name))
        if value not in (None, ""):
            return value
    return None


def _normalize_attr_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def _stable_segment_id(model_id: str, raw_id: Any, segment_order: int, part_index: int | None) -> str:
    if raw_id not in (None, ""):
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(raw_id).strip()).strip("_").lower()
        if slug:
            return f"{model_id}_seg_{slug}"
    suffix = f"{segment_order:06d}" if part_index is None else f"{segment_order:06d}_p{part_index}"
    return f"{model_id}_seg_{suffix}"


def _raw_segment_key(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    number = _optional_float(text)
    if number is not None and number.is_integer():
        return str(int(number))
    normalized = text.strip().lower()
    return normalized or None


def _mapped_downstream_segment_id(
    value: Any,
    *,
    raw_id: Any,
    river_segment_id: str,
    raw_to_segment_id: dict[str, str],
    segment_ids: set[str],
) -> str | None:
    key = _raw_segment_key(value)
    raw_key = _raw_segment_key(raw_id)
    if key in (None, "0", "-1") or key == raw_key:
        return None
    mapped = raw_to_segment_id.get(key)
    if mapped is not None and mapped != river_segment_id:
        return mapped
    text = _optional_text(value)
    if text in segment_ids and text != river_segment_id:
        return text
    return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _approximate_length_m(points: list[tuple[float, float]]) -> float:
    length_degrees = 0.0
    for first, second in zip(points, points[1:], strict=False):
        length_degrees += math.hypot(second[0] - first[0], second[1] - first[1])
    return length_degrees * 111_000.0


def _point_wkt(point: tuple[float, float]) -> str:
    return f"{point[0]:.12g} {point[1]:.12g}"


def _shud_segment_count(path: Path) -> int:
    input_root = path.parent
    _validate_safe_file(path, input_root, role="shud_evidence", error_code="BASINS_REGISTRY_SOURCE_MISSING")
    first: list[str] | None = None
    row_count = 0
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//", "%")):
                continue
            tokens = re.split(r"[\s,]+", stripped)
            if not tokens:
                continue
            if any(re.search(r"[A-Za-z]", token) for token in tokens):
                continue
            if first is None:
                first = tokens
            row_count += 1
    if first is None:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SHUD_PARSE_FAILED",
            "SHUD river evidence did not contain any numeric rows.",
            path=str(path),
        )
    if len(first) == 1:
        declared = _optional_int(first[0])
        if declared is not None and declared >= 0:
            return declared
    return row_count


def _layer_checksum(layer_base: Path) -> str:
    digest = hashlib.sha256()
    for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
        digest.update(suffix.encode("utf-8"))
        digest.update(_sha256_file(layer_base.with_suffix(f".{suffix}")).encode("ascii"))
    return digest.hexdigest()


def _river_network_checksum(layer_base: Path, sp_riv: Path, sp_rivseg: Path) -> str:
    material = {
        "gis": _layer_checksum(layer_base),
        "sp_riv": _sha256_file(sp_riv),
        "sp_rivseg": _sha256_file(sp_rivseg),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    _validate_safe_file(path, path.parent, role="checksum", error_code="BASINS_REGISTRY_SOURCE_MISSING")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_basins_file_sha256(path: Path, containment_root: Path) -> str:
    _validate_safe_file(path, containment_root, role="checksum", error_code="BASINS_REGISTRY_SOURCE_MISSING")
    return _sha256_file(path)


def _validate_required_files_canonical(required_files: dict[str, Any], shud_input_name: str, input_dir: Path) -> None:
    expected: dict[str, str] = {
        role: f"{shud_input_name}{suffix}" for role, suffix in SHUD_CANONICAL_SUFFIXES.items()
    }
    expected.update(
        {
            f"gis_{layer}_{suffix}": f"gis/{layer}.{suffix}"
            for layer in ("domain", "river", "seg")
            for suffix in SHAPEFILE_REQUIRED_SUFFIXES
        }
    )
    for role, expected_path in expected.items():
        values = required_files.get(role)
        if values != [expected_path]:
            raise BasinsGeometryError(
                "BASINS_REQUIRED_FILES_NON_CANONICAL",
                f"Basins inventory source role {role} must be canonical: {expected_path}.",
                path=str(input_dir),
                details={"role": role, "expected_path": expected_path},
            )
    extras = sorted(str(role) for role in required_files if str(role) not in expected)
    if extras:
        raise BasinsGeometryError(
            "BASINS_REQUIRED_FILES_NON_CANONICAL",
            "Basins inventory contains non-canonical required file roles.",
            path=str(input_dir),
            details={"roles": extras},
        )


def _validate_safe_file(path: Path, containment_root: Path, *, role: str, error_code: str) -> None:
    root = containment_root.resolve()
    candidate = path if path.is_absolute() else containment_root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path is outside the canonical input directory.",
            path=str(candidate),
            details={"role": role},
        ) from error
    if ".." in relative.parts:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path contains parent traversal.",
            path=str(candidate),
            details={"role": role},
        )
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if current == candidate:
                raise BasinsGeometryError(
                    error_code,
                    "Basins source file is missing.",
                    path=str(candidate),
                    details={"role": role},
                ) from None
            raise
        except OSError as error:
            raise BasinsGeometryError(
                "BASINS_REGISTRY_PATH_UNSAFE",
                "Basins source path cannot be safely inspected.",
                path=str(current),
                details={"role": role},
            ) from error
        if stat.S_ISLNK(mode):
            raise BasinsGeometryError(
                "BASINS_REGISTRY_PATH_UNSAFE",
                "Basins source path contains a symlink descendant.",
                path=str(current),
                details={"role": role},
            )
    if not candidate.is_file():
        raise BasinsGeometryError(
            error_code,
            "Basins source path is not a regular file.",
            path=str(candidate),
            details={"role": role},
        )


def _check_resource_limit(count: int, limit: int, resource: str, path: str) -> None:
    if count > limit:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_RESOURCE_LIMIT_EXCEEDED",
            f"Basins GIS {resource} exceeded import limit.",
            path=path,
            details={"resource": resource, "count": count, "limit": limit},
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return str(value)
