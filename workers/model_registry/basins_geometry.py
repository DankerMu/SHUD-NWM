from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHAPEFILE_REQUIRED_SUFFIXES = ("shp", "shx", "dbf", "prj")
WGS84_PRJ_HINTS = ("WGS_1984", "GCS_WGS_1984", "EPSG", "4490", "4326")


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

    domain_base = _validated_layer_base(input_dir, "domain", required_files)
    river_base = _validated_layer_base(input_dir, "river", required_files)
    seg_base = _validated_layer_base(input_dir, "seg", required_files)
    _validate_prj(domain_base)
    _validate_prj(river_base)
    _validate_prj(seg_base)

    sp_riv = _required_input_file(input_dir, required_files, "sp_riv")
    sp_rivseg = _required_input_file(input_dir, required_files, "sp_rivseg")
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
        if not path.is_file():
            raise BasinsGeometryError(
                "BASINS_REGISTRY_GIS_SIDECAR_MISSING",
                f"Basins GIS sidecar is missing: {expected}",
                path=str(path),
                details={"missing_sidecar": expected, "role": role},
            )
    return input_dir / "gis" / layer


def _required_input_file(input_dir: Path, required_files: dict[str, Any], role: str) -> Path:
    values = required_files.get(role)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            f"Basins inventory is missing required source role {role}.",
            path=str(input_dir),
            details={"role": role},
        )
    path = input_dir / values[0]
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
    text = prj_path.read_text(encoding="utf-8", errors="ignore")
    if not any(hint in text for hint in WGS84_PRJ_HINTS):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_CRS_UNSUPPORTED",
            "Basins shapefile projection must be compatible with SRID 4490.",
            path=str(prj_path),
        )


def _domain_multipolygon_wkt(layer_base: Path) -> str:
    reader = _shape_reader(layer_base)
    polygons: list[str] = []
    for shape in reader.shapes():
        for ring in _shape_parts(shape):
            closed = _closed_ring(ring)
            if len(closed) >= 4:
                polygons.append("((" + ", ".join(_point_wkt(point) for point in closed) + "))")
    if not polygons:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_PARSE_FAILED",
            "Basins domain shapefile did not contain a non-empty polygon.",
            path=str(layer_base.with_suffix(".shp")),
        )
    return "MULTIPOLYGON(" + ", ".join(polygons) + ")"


def _river_segments_from_layer(layer_base: Path, *, model_id: str) -> list[RiverSegmentGeometry]:
    reader = _shape_reader(layer_base)
    segments: list[RiverSegmentGeometry] = []
    for record_index, shape_record in enumerate(reader.iterShapeRecords(), start=1):
        attrs = _record_dict(shape_record)
        for part_index, points in enumerate(_shape_parts(shape_record.shape), start=1):
            if len(points) < 2:
                continue
            order = _optional_int(_pick_attr(attrs, ("segment_order", "stream_order", "order", "ord", "seg_order")))
            segment_order = order if order is not None else len(segments) + 1
            raw_id = _pick_attr(attrs, ("river_segment_id", "segment_id", "seg_id", "segid", "comid", "linkno", "id"))
            multi_part_index = part_index if len(_shape_parts(shape_record.shape)) > 1 else None
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
                }
            )
            segments.append(
                RiverSegmentGeometry(
                    river_segment_id=segment_id,
                    segment_order=segment_order,
                    downstream_segment_id=downstream,
                    length_m=length_m,
                    geom_wkt="LINESTRING(" + ", ".join(_point_wkt(point) for point in points) + ")",
                    properties=properties,
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
        return shapefile.Reader(str(layer_base))
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
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "%")):
            continue
        tokens = re.split(r"[\s,]+", stripped)
        if not tokens:
            continue
        if any(re.search(r"[A-Za-z]", token) for token in tokens):
            continue
        rows.append(tokens)
    if not rows:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SHUD_PARSE_FAILED",
            "SHUD river evidence did not contain any numeric rows.",
            path=str(path),
        )
    first = rows[0]
    if len(first) == 1:
        declared = _optional_int(first[0])
        if declared is not None and declared >= 0:
            return declared
    return len(rows)


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
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return str(value)
