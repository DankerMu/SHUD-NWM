from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO, FileIO
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
MAX_BASINS_GIS_SIDECAR_BYTES = 64 * 1024 * 1024
MAX_BASINS_GIS_LAYER_BYTES = 128 * 1024 * 1024
MAX_BASINS_GIS_TOTAL_BYTES = 384 * 1024 * 1024
# SHUD segment evidence files should be tiny count/header or row-count inputs.
# These guards keep stale or hostile local files from turning import into an
# unbounded scan before registry writes begin.
MAX_BASINS_SHUD_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_BASINS_SHUD_EVIDENCE_LINES = 250_000
_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_SAFE_OPEN_TEST_HOOK: Any = None


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
    # `.sp.riv` reach count: the SHUD output/product topology, distinct from the
    # finer `seg.shp`/`.sp.rivseg` display geometry counted by ``segment_count``.
    output_segment_count: int
    evidence_counts: dict[str, int | None]


@dataclass(frozen=True)
class _LayerSnapshot:
    base: Path
    data: dict[str, bytes]
    digests: dict[str, str]


@dataclass(frozen=True)
class _CoordinateTransform:
    source_name: str
    projection_method: str | None
    transformer: Any | None
    source_is_projected: bool


@dataclass(frozen=True)
class TrustedBasinsRoot:
    path: Path
    resolved_path: Path
    identity: tuple[int, int, int]


@dataclass
class _GisReadBudget:
    total_bytes: int = 0


def trusted_basins_root(path: Path, *, role: str) -> TrustedBasinsRoot:
    expanded = Path(path).expanduser()
    root = expanded if expanded.is_absolute() else Path(os.path.abspath(expanded))
    try:
        st = root.lstat()
    except OSError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins trusted root cannot be safely inspected.",
            path=str(root),
            details={"role": role},
        ) from error
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins trusted root is not a regular no-symlink directory.",
            path=str(root),
            details={"role": role},
        )
    return TrustedBasinsRoot(
        path=root,
        resolved_path=root,
        identity=(st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode)),
    )


def parse_basins_geometry(
    *,
    model_id: str,
    input_dir: Path | TrustedBasinsRoot,
    shud_input_name: str,
    required_files: dict[str, Any],
    expected_checksums: dict[str, str] | None = None,
) -> ParsedBasinsGeometry:
    """Parse domain and river geometry from inventory-referenced Basins files."""

    input_root = _coerce_trusted_root(input_dir, role="shud_input_name")
    _run_safe_open_test_hook(input_root.path, "shud_input_name", "before_parse")
    _validate_required_files_canonical(required_files, shud_input_name, input_root.path)
    domain_base = _validated_layer_base(input_root, "domain", required_files)
    river_base = _validated_layer_base(input_root, "river", required_files)
    seg_base = _validated_layer_base(input_root, "seg", required_files)
    _enforce_gis_sidecar_byte_limits((domain_base, river_base, seg_base), input_root)
    _run_safe_open_test_hook(input_root.path, "gis_sidecar_limits", "after_precheck")
    gis_budget = _GisReadBudget()
    domain_layer = _load_layer_snapshot(domain_base, input_root, expected_checksums, gis_budget)
    river_layer = _load_layer_snapshot(river_base, input_root, expected_checksums, gis_budget)
    seg_layer = _load_layer_snapshot(seg_base, input_root, expected_checksums, gis_budget)
    domain_transform = _coordinate_transform(domain_layer)
    river_transform = _coordinate_transform(river_layer)
    seg_transform = _coordinate_transform(seg_layer)

    sp_riv = _required_input_file(input_root, required_files, "sp_riv", shud_input_name)
    sp_rivseg = _required_input_file(input_root, required_files, "sp_rivseg", shud_input_name)
    sp_riv_header, sp_riv_digest = _shud_count_header(sp_riv, expected_checksums)
    sp_rivseg_header, sp_rivseg_digest = _shud_count_header(sp_rivseg, expected_checksums)
    domain_wkt = _domain_multipolygon_wkt(domain_layer, domain_transform)

    river_segments = _river_segments_from_layer(seg_layer, model_id=model_id, coordinate_transform=seg_transform)
    selected_layer = seg_layer
    if not river_segments:
        river_segments = _river_segments_from_layer(
            river_layer,
            model_id=model_id,
            coordinate_transform=river_transform,
        )
        selected_layer = river_layer
    if not river_segments:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_PARSE_FAILED",
            "Basins river/seg shapefile did not contain any LineString features.",
            path=str(seg_base.with_suffix(".shp")),
        )

    evidence_counts = {
        "river_count": sp_riv_header["count"],
        "river_columns": sp_riv_header["columns"],
        "rivseg_segment_count": sp_rivseg_header["count"],
        "rivseg_columns": sp_rivseg_header["columns"],
    }
    if sp_rivseg_header["count"] != len(river_segments):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SEGMENT_COUNT_MISMATCH",
            "Basins GIS segment count does not match SHUD rivseg segment evidence.",
            path=str(sp_rivseg),
            details={
                "model_id": model_id,
                "gis_segment_count": len(river_segments),
                "evidence_count": sp_rivseg_header["count"],
                "evidence": "rivseg_segment_count",
            },
        )

    return ParsedBasinsGeometry(
        domain_wkt=domain_wkt,
        domain_checksum=_layer_checksum_from_snapshot(domain_layer),
        domain_source_uri=str(domain_base.with_suffix(".shp")),
        river_segments=river_segments,
        river_network_checksum=_river_network_checksum_from_snapshot(
            selected_layer,
            sp_riv_digest=sp_riv_digest,
            sp_rivseg_digest=sp_rivseg_digest,
        ),
        river_network_source_uri=str(selected_layer.base.with_suffix(".shp")),
        segment_count=len(river_segments),
        output_segment_count=int(sp_riv_header["count"] or 0),
        evidence_counts=evidence_counts,
    )


def _coerce_trusted_root(root: Path | TrustedBasinsRoot, *, role: str) -> TrustedBasinsRoot:
    if isinstance(root, TrustedBasinsRoot):
        _validate_trusted_root(root, role=role)
        return root
    return trusted_basins_root(root, role=role)


def _validated_layer_base(input_dir: TrustedBasinsRoot, layer: str, required_files: dict[str, Any]) -> Path:
    for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
        role = f"gis_{layer}_{suffix}"
        expected = f"gis/{layer}.{suffix}"
        values = required_files.get(role)
        if values != [expected]:
            raise BasinsGeometryError(
                "BASINS_REGISTRY_GIS_SIDECAR_MISSING",
                f"Basins inventory is missing required GIS sidecar role {role}.",
                path=str(input_dir.path / expected),
                details={"missing_sidecar": expected, "role": role},
            )
        path = input_dir.path / expected
        try:
            _validate_safe_file(path, input_dir, role=role, error_code="BASINS_REGISTRY_GIS_SIDECAR_MISSING")
        except BasinsGeometryError as error:
            if error.error_code == "BASINS_REGISTRY_GIS_SIDECAR_MISSING":
                error.details.setdefault("missing_sidecar", expected)
            raise
        if not path.is_file():
            raise BasinsGeometryError(
                "BASINS_REGISTRY_GIS_SIDECAR_MISSING",
                f"Basins GIS sidecar is missing: {expected}",
                path=str(path),
                details={"missing_sidecar": expected, "role": role},
            )
    return input_dir.path / "gis" / layer


def _enforce_gis_sidecar_byte_limits(layer_bases: tuple[Path, ...], input_root: TrustedBasinsRoot) -> None:
    total_bytes = 0
    for layer_base in layer_bases:
        layer_bytes = 0
        for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
            role = f"gis_{layer_base.name}_{suffix}"
            path = layer_base.with_suffix(f".{suffix}")
            size_bytes = _verified_file_size(path, input_root, role=role)
            _check_resource_limit(
                size_bytes,
                MAX_BASINS_GIS_SIDECAR_BYTES,
                "gis_sidecar_bytes",
                str(path),
            )
            layer_bytes += size_bytes
            total_bytes += size_bytes
            _check_resource_limit(
                layer_bytes,
                MAX_BASINS_GIS_LAYER_BYTES,
                "gis_layer_bytes",
                str(path),
            )
            _check_resource_limit(
                total_bytes,
                MAX_BASINS_GIS_TOTAL_BYTES,
                "gis_total_bytes",
                str(path),
            )


def _required_input_file(
    input_dir: TrustedBasinsRoot,
    required_files: dict[str, Any],
    role: str,
    shud_input_name: str,
) -> Path:
    values = required_files.get(role)
    expected = f"{shud_input_name}{SHUD_CANONICAL_SUFFIXES[role]}"
    if not isinstance(values, list) or values != [expected]:
        raise BasinsGeometryError(
            "BASINS_REQUIRED_FILES_NON_CANONICAL",
            f"Basins inventory source role {role} must be canonical: {expected}.",
            path=str(input_dir.path),
            details={"role": role},
        )
    path = input_dir.path / expected
    _validate_safe_file(path, input_dir, role=role, error_code="BASINS_REGISTRY_SOURCE_MISSING")
    if not path.is_file():
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            f"Basins source file is missing for role {role}.",
            path=str(path),
            details={"role": role},
        )
    return path


def _load_layer_snapshot(
    layer_base: Path,
    input_root: TrustedBasinsRoot,
    expected_checksums: dict[str, str] | None,
    gis_budget: _GisReadBudget,
) -> _LayerSnapshot:
    data: dict[str, bytes] = {}
    digests: dict[str, str] = {}
    layer_bytes = 0
    for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
        role = f"gis_{layer_base.name}_{suffix}"
        path = layer_base.with_suffix(f".{suffix}")
        payload, digest = _read_verified_binary(
            path,
            input_root,
            role=role,
            expected_checksums=expected_checksums,
            error_code="BASINS_REGISTRY_GIS_SIDECAR_MISSING",
            max_bytes=MAX_BASINS_GIS_SIDECAR_BYTES,
            max_bytes_resource="gis_sidecar_bytes",
        )
        layer_bytes += len(payload)
        gis_budget.total_bytes += len(payload)
        _check_resource_limit(layer_bytes, MAX_BASINS_GIS_LAYER_BYTES, "gis_layer_bytes", str(path))
        _check_resource_limit(gis_budget.total_bytes, MAX_BASINS_GIS_TOTAL_BYTES, "gis_total_bytes", str(path))
        data[suffix] = payload
        digests[suffix] = digest
    return _LayerSnapshot(base=layer_base, data=data, digests=digests)


def _coordinate_transform(layer: _LayerSnapshot) -> _CoordinateTransform:
    prj_path = layer.base.with_suffix(".prj")
    text = layer.data["prj"].decode("utf-8", errors="ignore")
    try:
        from pyproj import CRS, Transformer
    except ImportError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PROJ_DEPENDENCY_MISSING",
            "pyproj is required for Basins CRS parsing and reprojection.",
            path=str(prj_path),
        ) from error
    try:
        crs = CRS.from_wkt(text)
    except Exception as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_CRS_UNSUPPORTED",
            "Basins shapefile projection could not be parsed.",
            path=str(prj_path),
        ) from error

    if crs.is_geographic and _is_wgs84_or_cgcs2000(crs):
        return _CoordinateTransform(
            source_name=str(crs.name or ""),
            projection_method=None,
            transformer=None,
            source_is_projected=False,
        )

    method = _projection_method_name(crs)
    if crs.is_projected and _is_wgs84_or_cgcs2000(crs.geodetic_crs) and method in {
        "albers equal area",
        "transverse mercator",
    }:
        try:
            transformer = Transformer.from_crs(crs, "EPSG:4490", always_xy=True)
        except Exception as error:
            raise BasinsGeometryError(
                "BASINS_REGISTRY_GIS_CRS_UNSUPPORTED",
                "Basins projected CRS could not be transformed to SRID 4490.",
                path=str(prj_path),
            ) from error
        return _CoordinateTransform(
            source_name=str(crs.name or ""),
            projection_method=method,
            transformer=transformer,
            source_is_projected=True,
        )

    raise BasinsGeometryError(
        "BASINS_REGISTRY_GIS_CRS_UNSUPPORTED",
        "Basins shapefile projection must be WGS84/CGCS2000 geographic or supported Basins Albers/TM projected CRS.",
        path=str(prj_path),
    )


def _is_wgs84_or_cgcs2000(crs: Any | None) -> bool:
    if crs is None:
        return False
    authority = crs.to_authority()
    if authority in {("EPSG", "4326"), ("EPSG", "4490")}:
        return True
    text = " ".join(
        str(value or "")
        for value in (
            getattr(crs, "name", None),
            getattr(getattr(crs, "datum", None), "name", None),
            crs.to_wkt() if hasattr(crs, "to_wkt") else "",
        )
    ).upper()
    return any(
        marker in text
        for marker in (
            "WGS 84",
            "WGS_1984",
            "WORLD GEODETIC SYSTEM 1984",
            "CGCS2000",
            "CHINA GEODETIC COORDINATE SYSTEM 2000",
        )
    )


def _projection_method_name(crs: Any) -> str | None:
    operation = getattr(crs, "coordinate_operation", None)
    method = getattr(operation, "method_name", None)
    if method in (None, ""):
        return None
    normalized = re.sub(r"[_\s]+", " ", str(method).strip().lower())
    aliases = {
        "albers conic equal area": "albers equal area",
        "albers equal area": "albers equal area",
        "transverse mercator": "transverse mercator",
    }
    return aliases.get(normalized, normalized)


def _transform_points(
    points: list[tuple[float, float]],
    coordinate_transform: _CoordinateTransform,
    *,
    path: Path,
) -> list[tuple[float, float]]:
    if coordinate_transform.transformer is None:
        return points
    try:
        transformed = [coordinate_transform.transformer.transform(x, y) for x, y in points]
    except Exception as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_CRS_UNSUPPORTED",
            "Basins projected coordinates could not be transformed to SRID 4490.",
            path=str(path),
        ) from error
    result = [(float(x), float(y)) for x, y in transformed]
    for x, y in result:
        if not (math.isfinite(x) and math.isfinite(y) and -180.0 <= x <= 180.0 and -90.0 <= y <= 90.0):
            raise BasinsGeometryError(
                "BASINS_REGISTRY_GIS_CRS_UNSUPPORTED",
                "Basins transformed coordinate is outside lon/lat bounds.",
                path=str(path),
                details={"x": x, "y": y},
            )
    return result


def _domain_multipolygon_wkt(layer: _LayerSnapshot, coordinate_transform: _CoordinateTransform) -> str:
    reader = _shape_reader(layer)
    try:
        polygons: list[str] = []
        feature_count = 0
        point_count = 0
        for shape in reader.iterShapes():
            feature_count += 1
            _check_resource_limit(
                feature_count,
                MAX_BASINS_GIS_FEATURES,
                "features",
                str(layer.base.with_suffix(".shp")),
            )
            shape_rings: list[list[tuple[float, float]]] = []
            for ring in _shape_parts(shape):
                point_count += len(ring)
                _check_resource_limit(point_count, MAX_BASINS_GIS_POINTS, "points", str(layer.base.with_suffix(".shp")))
                transformed_ring = _transform_points(
                    ring,
                    coordinate_transform,
                    path=layer.base.with_suffix(".shp"),
                )
                closed = _closed_ring(transformed_ring)
                if len(closed) >= 4:
                    shape_rings.append(closed)
            polygons.extend(_polygon_wkts_from_rings(shape_rings))
    finally:
        reader.close()
    if not polygons:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_PARSE_FAILED",
            "Basins domain shapefile did not contain a non-empty polygon.",
            path=str(layer.base.with_suffix(".shp")),
        )
    return "MULTIPOLYGON(" + ", ".join(polygons) + ")"


def _river_segments_from_layer(
    layer: _LayerSnapshot,
    *,
    model_id: str,
    coordinate_transform: _CoordinateTransform,
) -> list[RiverSegmentGeometry]:
    reader = _shape_reader(layer)
    try:
        pending: list[dict[str, Any]] = []
        feature_count = 0
        point_count = 0
        for record_index, shape_record in enumerate(reader.iterShapeRecords(), start=1):
            feature_count += 1
            _check_resource_limit(
                feature_count,
                MAX_BASINS_GIS_FEATURES,
                "features",
                str(layer.base.with_suffix(".shp")),
            )
            attrs = _record_dict(shape_record)
            source_parts = [points for points in _shape_parts(shape_record.shape) if len(points) >= 2]
            point_count += sum(len(points) for points in source_parts)
            _check_resource_limit(point_count, MAX_BASINS_GIS_POINTS, "points", str(layer.base.with_suffix(".shp")))
            merged_points = _merge_polyline_parts(source_parts)
            if len(merged_points) < 2:
                continue
            transformed_points = _transform_points(
                merged_points,
                coordinate_transform,
                path=layer.base.with_suffix(".shp"),
            )
            order = _optional_int(_pick_attr(attrs, ("segment_order", "stream_order", "order", "ord", "seg_order")))
            segment_order = order if order is not None else len(pending) + 1
            raw_id = _pick_attr(
                attrs,
                ("river_segment_id", "segment_id", "seg_id", "segid", "comid", "linkno", "id", "iriv", "iele"),
            )
            segment_id_base = _stable_segment_id(model_id, raw_id, segment_order)
            downstream = _optional_text(
                _pick_attr(
                    attrs,
                    ("downstream_segment_id", "downstream", "down_id", "to_segment", "toid", "dslinkno"),
                )
            )
            length_m = _optional_float(
                _pick_attr(attrs, ("length_m", "length", "len_m", "shape_leng", "shapeleng"))
            )
            if length_m is None:
                length_m = _approximate_length_m(transformed_points)
            properties = {str(key): _jsonable(value) for key, value in attrs.items()}
            properties.update(
                {
                    "source_layer": layer.base.name,
                    "source_record_index": record_index,
                    "source_part_count": len(source_parts),
                    "source_raw_segment_id": _jsonable(raw_id),
                    "source_stable_segment_id_base": segment_id_base,
                    "source_downstream_segment_id": _jsonable(downstream),
                    "source_crs": coordinate_transform.source_name,
                    "source_crs_projected": coordinate_transform.source_is_projected,
                    "source_projection_method": coordinate_transform.projection_method,
                }
            )
            pending.append(
                {
                    "river_segment_id_base": segment_id_base,
                    "river_segment_id": segment_id_base,
                    "segment_order": segment_order,
                    "record_index": record_index,
                    "raw_id": raw_id,
                    "raw_downstream": downstream,
                    "length_m": length_m,
                    "geom_wkt": "LINESTRING(" + ", ".join(_point_wkt(point) for point in transformed_points) + ")",
                    "properties": properties,
                }
            )
    finally:
        reader.close()
    _deduplicate_pending_segment_ids(pending)
    raw_to_segment_ids: dict[str, set[str]] = {}
    for item in pending:
        key = _raw_segment_key(item["raw_id"])
        if key is None:
            continue
        raw_to_segment_ids.setdefault(key, set()).add(item["river_segment_id"])
    raw_to_segment_id = {key: next(iter(ids)) for key, ids in raw_to_segment_ids.items() if len(ids) == 1}
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


def _shape_reader(layer: _LayerSnapshot) -> Any:
    try:
        import shapefile
    except ImportError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SHAPEFILE_DEPENDENCY_MISSING",
            "pyshp is required for Basins shapefile parsing.",
            path=str(layer.base.with_suffix(".shp")),
        ) from error
    try:
        handles = {suffix: BytesIO(layer.data[suffix]) for suffix in ("shp", "shx", "dbf")}
        return shapefile.Reader(shp=handles["shp"], shx=handles["shx"], dbf=handles["dbf"])
    except BasinsGeometryError:
        raise
    except Exception as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_PARSE_FAILED",
            f"Failed to parse Basins shapefile: {error}",
            path=str(layer.base.with_suffix(".shp")),
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


def _merge_polyline_parts(parts: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    """Stitch a multi-part polyline into one continuous point list by joining
    parts at their nearest endpoints, reversing parts when needed.

    Shapefile part *storage* order is not flow order and parts may be stored
    reversed. Concatenating them blindly (the old behaviour) linked a part's
    first point to the running tail even when another endpoint was nearer,
    fabricating a longer-than-necessary straight "jump" between mis-ordered
    parts. Greedy nearest-endpoint chaining from either chain end always takes
    the shortest available link instead. NOTE: the dominant heihe cross-ridge
    lines came from the output-reach backfill stitch (fixed separately in
    basins_registry_import); where a record's parts are genuinely far apart in
    the source GIS, the shortest link is still long -- faithful to the source,
    not a stitch-order artifact this can remove.
    """
    chain = [list(points) for points in parts if len(points) >= 2]
    if not chain:
        return [point for points in parts for point in points]
    merged = chain.pop(0)
    while chain:
        index, use_start, at_tail = _nearest_attachment(chain, merged[0], merged[-1])
        piece = chain.pop(index)
        if at_tail:
            oriented = piece if use_start else piece[::-1]
            merged.extend(oriented[1:] if merged[-1] == oriented[0] else oriented)
        else:
            oriented = piece[::-1] if use_start else piece
            merged[:0] = oriented[:-1] if oriented[-1] == merged[0] else oriented
    return merged


def _nearest_attachment(
    chain: list[list[tuple[float, float]]],
    head: tuple[float, float],
    tail: tuple[float, float],
) -> tuple[int, bool, bool]:
    """Pick (part index, endpoint-is-its-start, attach-at-tail) for the unused
    part whose endpoint sits closest to either free end of the running chain."""
    best: tuple[float, int, bool, bool] = (float("inf"), 0, True, True)
    for index, points in enumerate(chain):
        for endpoint, use_start in ((points[0], True), (points[-1], False)):
            for anchor, at_tail in ((tail, True), (head, False)):
                dist = (endpoint[0] - anchor[0]) ** 2 + (endpoint[1] - anchor[1]) ** 2
                if dist < best[0]:
                    best = (dist, index, use_start, at_tail)
    return best[1], best[2], best[3]


def _stable_segment_id(model_id: str, raw_id: Any, segment_order: int) -> str:
    if raw_id not in (None, ""):
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(raw_id).strip()).strip("_").lower()
        if slug:
            return f"{model_id}_seg_{slug}"
    return f"{model_id}_seg_{segment_order:06d}"


def _deduplicate_pending_segment_ids(pending: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    for item in pending:
        base_id = item["river_segment_id_base"]
        counts[base_id] = counts.get(base_id, 0) + 1
    for item in pending:
        base_id = item["river_segment_id_base"]
        if counts[base_id] == 1:
            continue
        item["river_segment_id"] = f"{base_id}_ord_{item['segment_order']:06d}_rec_{item['record_index']:06d}"
        item["properties"]["source_duplicate_segment_id_disambiguated"] = True


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


def _shud_count_header(path: Path, expected_checksums: dict[str, str] | None) -> tuple[dict[str, int | None], str]:
    input_root = path.parent
    byte_count = 0
    payload, digest = _read_verified_binary(
        path,
        input_root,
        role="shud_evidence",
        expected_checksums=expected_checksums,
        error_code="BASINS_REGISTRY_SOURCE_MISSING",
        max_bytes=MAX_BASINS_SHUD_EVIDENCE_BYTES,
    )
    for line_count, line in enumerate(payload.decode("utf-8", errors="ignore").splitlines(), start=1):
        _check_resource_limit(
            line_count,
            MAX_BASINS_SHUD_EVIDENCE_LINES,
            "shud_evidence_lines",
            str(path),
        )
        byte_count += len(line.encode("utf-8")) + 1
        _check_resource_limit(
            byte_count,
            MAX_BASINS_SHUD_EVIDENCE_BYTES,
            "shud_evidence_bytes",
            str(path),
        )
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "%")):
            continue
        tokens = re.split(r"[\s,]+", stripped)
        if not tokens:
            continue
        if any(re.search(r"[A-Za-z]", token) for token in tokens):
            continue
        declared = _optional_int(tokens[0])
        columns = _optional_int(tokens[1]) if len(tokens) > 1 else None
        if declared is not None and declared >= 0:
            return {"count": declared, "columns": columns}, digest
        break
    raise BasinsGeometryError(
        "BASINS_REGISTRY_SHUD_PARSE_FAILED",
        "SHUD river evidence did not contain a valid numeric count header.",
        path=str(path),
    )


def _shud_segment_count(path: Path, expected_checksums: dict[str, str] | None) -> tuple[int, str]:
    header, digest = _shud_count_header(path, expected_checksums)
    count = header["count"]
    if count is None:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SHUD_PARSE_FAILED",
            "SHUD river evidence did not contain a valid segment count.",
            path=str(path),
        )
    return count, digest


def _layer_checksum_from_snapshot(layer: _LayerSnapshot) -> str:
    digest = hashlib.sha256()
    for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
        digest.update(suffix.encode("utf-8"))
        digest.update(layer.digests[suffix].encode("ascii"))
    return digest.hexdigest()


def _river_network_checksum_from_snapshot(
    layer: _LayerSnapshot,
    *,
    sp_riv_digest: str,
    sp_rivseg_digest: str,
) -> str:
    material = {
        "gis": _layer_checksum_from_snapshot(layer),
        "sp_riv": sp_riv_digest,
        "sp_rivseg": sp_rivseg_digest,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def _sha256_file(path: Path, containment_root: Path | TrustedBasinsRoot) -> str:
    _validate_safe_file(path, containment_root, role="checksum", error_code="BASINS_REGISTRY_SOURCE_MISSING")
    digest = hashlib.sha256()
    with _open_safe_binary(path, containment_root, role="checksum") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_basins_file_sha256(path: Path, containment_root: Path | TrustedBasinsRoot) -> str:
    _validate_safe_file(path, containment_root, role="checksum", error_code="BASINS_REGISTRY_SOURCE_MISSING")
    return _sha256_file(path, containment_root)


def _read_verified_binary(
    path: Path,
    containment_root: Path | TrustedBasinsRoot,
    *,
    role: str,
    expected_checksums: dict[str, str] | None,
    error_code: str,
    max_bytes: int | None = None,
    max_bytes_resource: str = "shud_evidence_bytes",
) -> tuple[bytes, str]:
    _validate_safe_file(path, containment_root, role=role, error_code=error_code)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    byte_count = 0
    with _open_safe_binary(path, containment_root, role=role) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_count += len(chunk)
            if max_bytes is not None:
                _check_resource_limit(byte_count, max_bytes, max_bytes_resource, str(path))
            digest.update(chunk)
            chunks.append(chunk)
    actual = digest.hexdigest()
    _run_safe_open_test_hook(path, role, "after_read")
    relative_path = _relative_to_root(path, containment_root)
    expected = expected_checksums.get(relative_path) if expected_checksums is not None else None
    if expected_checksums is not None and not expected:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins source file is missing an expected manifest checksum.",
            path=str(path),
            details={"role": role, "relative_path": relative_path},
        )
    if expected is not None and actual != expected:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            "Basins source bytes do not match the manifest-verified checksum.",
            path=str(path),
            details={"role": role, "relative_path": relative_path},
        )
    return b"".join(chunks), actual


def _relative_to_root(path: Path, containment_root: Path | TrustedBasinsRoot) -> str:
    root = _coerce_trusted_root(containment_root, role="relative").resolved_path
    candidate = path if path.is_absolute() else root / path
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _file_identity(path: Path, containment_root: Path | TrustedBasinsRoot, *, role: str) -> tuple[int, int, int]:
    _validate_safe_file(path, containment_root, role=role, error_code="BASINS_REGISTRY_SOURCE_MISSING")
    try:
        st = path.lstat()
    except OSError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path cannot be safely inspected.",
            path=str(path),
            details={"role": role},
        ) from error
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path is not a regular no-symlink file.",
            path=str(path),
            details={"role": role},
        )
    return (st.st_dev, st.st_ino, st.st_mode)


def _verified_file_size(path: Path, containment_root: Path | TrustedBasinsRoot, *, role: str) -> int:
    _validate_safe_file(path, containment_root, role=role, error_code="BASINS_REGISTRY_SOURCE_MISSING")
    try:
        st = path.lstat()
    except OSError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path cannot be safely inspected.",
            path=str(path),
            details={"role": role},
        ) from error
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path is not a regular no-symlink file.",
            path=str(path),
            details={"role": role},
        )
    return int(st.st_size)


@contextmanager
def _open_safe_binary(path: Path, containment_root: Path | TrustedBasinsRoot, *, role: str) -> Iterator[FileIO]:
    handle = _open_safe_binary_handle(path, containment_root, role=role)
    try:
        yield handle
    finally:
        handle.close()


def _open_safe_binary_handle(
    path: Path,
    containment_root: Path | TrustedBasinsRoot,
    *,
    role: str,
    expected_identity: tuple[int, int, int] | None = None,
) -> FileIO:
    expected = expected_identity or _file_identity(path, containment_root, role=role)
    _run_safe_open_test_hook(path, role, "before_open")
    flags = os.O_RDONLY | os.O_CLOEXEC | _OPEN_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source file cannot be safely opened.",
            path=str(path),
            details={"role": role},
        ) from error
    try:
        st = os.fstat(fd)
        actual = (st.st_dev, st.st_ino, st.st_mode)
        if actual != expected or not stat.S_ISREG(st.st_mode):
            raise BasinsGeometryError(
                "BASINS_REGISTRY_PATH_UNSAFE",
                "Basins source path changed during safe open.",
                path=str(path),
                details={"role": role},
            )
        _run_safe_open_test_hook(path, role, "after_open")
        if _file_identity(path, containment_root, role=role) != expected:
            raise BasinsGeometryError(
                "BASINS_REGISTRY_PATH_UNSAFE",
                "Basins source path changed after safe open.",
                path=str(path),
                details={"role": role},
            )
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def _validate_trusted_root(root: TrustedBasinsRoot, *, role: str) -> None:
    if root.path != root.resolved_path:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins trusted root path is not canonical.",
            path=str(root.path),
            details={"role": role},
        )
    try:
        st = root.path.lstat()
    except OSError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins trusted root cannot be safely inspected.",
            path=str(root.path),
            details={"role": role},
        ) from error
    actual = (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))
    if actual != root.identity or not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins trusted root changed during import.",
            path=str(root.path),
            details={"role": role},
        )


def _run_safe_open_test_hook(path: Path, role: str, phase: str) -> None:
    hook = _SAFE_OPEN_TEST_HOOK
    if hook is not None:
        hook(path, role, phase)


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


def _validate_safe_file(path: Path, containment_root: Path | TrustedBasinsRoot, *, role: str, error_code: str) -> None:
    trusted_root = _coerce_trusted_root(containment_root, role=role)
    root = trusted_root.resolved_path
    _validate_trusted_root(trusted_root, role=role)
    candidate = path if path.is_absolute() else root / path
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
            details: dict[str, Any] = {"role": role}
            if error_code == "BASINS_REGISTRY_GIS_SIDECAR_MISSING":
                details["missing_sidecar"] = relative.as_posix()
            raise BasinsGeometryError(
                error_code,
                "Basins source path component is missing.",
                path=str(candidate),
                details=details,
            ) from None
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
