"""Coordinate transforms and shapefile geometry for the Basins parser (#3081).

Split from ``basins_geometry``. Feature/point resource limits are passed in as
``max_features`` / ``max_points`` by the caller in ``basins_geometry``, which
reads its patchable module-level limit globals at call time; this module reads
no limit global and no safe-open test hook.
"""

from __future__ import annotations

import math
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from .basins_geometry_types import (
    BasinsGeometryError,
    CrosswalkRow,
    RiverSegmentGeometry,
    _check_resource_limit,
    _CoordinateTransform,
    _jsonable,
    _LayerSnapshot,
)


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


def _domain_multipolygon_wkt(
    layer: _LayerSnapshot,
    coordinate_transform: _CoordinateTransform,
    *,
    max_features: int,
    max_points: int,
) -> str:
    reader = _shape_reader(layer)
    try:
        polygons: list[str] = []
        feature_count = 0
        point_count = 0
        for shape in reader.iterShapes():
            feature_count += 1
            _check_resource_limit(
                feature_count,
                max_features,
                "features",
                str(layer.base.with_suffix(".shp")),
            )
            shape_rings: list[list[tuple[float, float]]] = []
            for ring in _shape_parts(shape):
                point_count += len(ring)
                _check_resource_limit(point_count, max_points, "points", str(layer.base.with_suffix(".shp")))
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
    max_features: int,
    max_points: int,
) -> list[RiverSegmentGeometry]:
    """Build reach-level river segments from ``gis/river.shp``.

    PR 2 (feat-reach-geom-from-river-shp / spec D1): each record in
    ``river.shp`` is one SHUD ``.sp.riv`` reach -- flow-ordered, single-part
    by design. The parser emits one ``RiverSegmentGeometry`` per record with
    ``river_segment_id = f"{model_id}_reach_{Index:06d}"`` and downstream
    resolved from the ``Down`` reach Index via the same zero-padded format
    (terminal reaches with ``Down ∈ {0, -1}`` get ``None`` plus a
    ``terminal_reach=True`` flag on ``properties_json``).

    Invariants enforced upstream by
    :func:`_validate_river_shp_single_part_invariant`: single part per
    record, ≥ 2 vertices per part, record count == ``.sp.riv`` reach count,
    full set of required dbf fields present.
    """

    reader = _shape_reader(layer)
    segments: list[RiverSegmentGeometry] = []
    try:
        feature_count = 0
        point_count = 0
        for record_index, shape_record in enumerate(reader.iterShapeRecords(), start=1):
            feature_count += 1
            _check_resource_limit(
                feature_count,
                max_features,
                "features",
                str(layer.base.with_suffix(".shp")),
            )
            attrs = _record_dict(shape_record)
            source_parts = [points for points in _shape_parts(shape_record.shape) if len(points) >= 2]
            point_count += sum(len(points) for points in source_parts)
            _check_resource_limit(
                point_count,
                max_points,
                "points",
                str(layer.base.with_suffix(".shp")),
            )
            # The single-part invariant is enforced by
            # _validate_river_shp_single_part_invariant before this loop runs.
            # The defensive check below would only trip if a foreign caller
            # bypassed parse_basins_geometry; we keep it for safety.
            if len(source_parts) != 1:
                raise BasinsGeometryError(
                    "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED",
                    "Basins river.shp record is not single-part.",
                    path=str(layer.base.with_suffix(".shp")),
                    details={"offending_index": record_index, "part_count": len(source_parts)},
                )
            transformed_points = _transform_points(
                source_parts[0],
                coordinate_transform,
                path=layer.base.with_suffix(".shp"),
            )
            iriv_raw = _pick_attr(attrs, ("Index",))
            iriv = _optional_int(iriv_raw)
            if iriv is None:
                raise BasinsGeometryError(
                    "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED",
                    "Basins river.shp record is missing a numeric Index.",
                    path=str(layer.base.with_suffix(".shp")),
                    details={
                        "offending_index": record_index,
                        "attrs": {str(k): _jsonable(v) for k, v in attrs.items()},
                    },
                )
            river_segment_id = f"{model_id}_reach_{iriv:06d}"
            down_raw = _pick_attr(attrs, ("Down",))
            down_int = _optional_int(down_raw)
            terminal_reach = down_int in (0, -1, None)
            downstream_segment_id = (
                None if terminal_reach else f"{model_id}_reach_{down_int:06d}"
            )
            length_m = _optional_float(_pick_attr(attrs, ("Length", "length_m", "length")))
            if length_m is None:
                length_m = _approximate_length_m(transformed_points)
            # Preserve every dbf attribute verbatim under properties_json so
            # the SHUD reach physical parameters (Slope/Depth/BankSlope/...)
            # round-trip through the registry for downstream consumers.
            properties: dict[str, Any] = {str(key): _jsonable(value) for key, value in attrs.items()}
            properties.update(
                {
                    "source_layer": layer.base.name,
                    "source_record_index": record_index,
                    "source_part_count": len(source_parts),
                    "iRiv": iriv,
                    "source_down_index": down_int,
                    "terminal_reach": terminal_reach,
                    "source_crs": coordinate_transform.source_name,
                    "source_crs_projected": coordinate_transform.source_is_projected,
                    "source_projection_method": coordinate_transform.projection_method,
                }
            )
            geom_wkt = (
                "LINESTRING("
                + ", ".join(_point_wkt(point) for point in transformed_points)
                + ")"
            )
            segments.append(
                RiverSegmentGeometry(
                    river_segment_id=river_segment_id,
                    segment_order=iriv,
                    downstream_segment_id=downstream_segment_id,
                    length_m=length_m,
                    geom_wkt=geom_wkt,
                    properties=properties,
                )
            )
    finally:
        reader.close()
    return segments


def parse_seg_shp_crosswalk(layer: Any) -> list[CrosswalkRow]:
    """Extract segment-to-reach mapping records from ``gis/seg.shp``.

    Pure attribute extraction; no geometry, no transformation, no IO beyond
    iterating the supplied shapefile reader. Each record contributes one
    :class:`CrosswalkRow` carrying the SHUD-internal ``(iRiv, iEle)`` pair
    plus a 0-indexed ``segment_order`` (the record's position in the source
    shapefile's natural enumeration order, matching ``pyshp``'s
    ``iterShapeRecords`` traversal). ``length_m`` is read from the ``Length``
    dbf field when present; for the qhh basin's ``seg.shp`` (fields are only
    ``iRiv`` / ``iEle``) it is always ``None``.

    Args:
        layer: A ``pyshp.shapefile.Reader``-compatible object exposing
            ``iterShapeRecords()``. In production this comes from
            :func:`_shape_reader` against an in-memory ``_LayerSnapshot``; in
            tests it is acceptable to pass a directly-opened
            ``shapefile.Reader(path)``.

    Returns:
        Records in source-file order. ``segment_order`` values form a
        monotonically increasing 0-indexed sequence ``0, 1, ..., N-1``.
    """

    rows: list[CrosswalkRow] = []
    for segment_order, shape_record in enumerate(layer.iterShapeRecords()):
        attrs = _record_dict(shape_record)
        iriv_value = _pick_attr(attrs, ("iRiv",))
        iele_value = _pick_attr(attrs, ("iEle",))
        if iriv_value is None or iele_value is None:
            # iRiv / iEle are the two mandatory attributes for a SHUD seg.shp
            # record (the file is by design the segment -> mesh-element index).
            # A missing value indicates the source file is malformed; surface
            # it through the same structured-error pathway used by the parser.
            raise BasinsGeometryError(
                "BASINS_REGISTRY_SEG_SHP_INVARIANT_VIOLATED",
                "Basins seg.shp record is missing required iRiv/iEle attribute.",
                details={
                    "segment_order": segment_order,
                    "attrs": {str(k): _jsonable(v) for k, v in attrs.items()},
                },
            )
        length_value = _pick_attr(attrs, ("Length", "length_m", "length"))
        length_m: float | None
        if length_value is None:
            length_m = None
        else:
            try:
                length_m = float(length_value)
            except (TypeError, ValueError):
                length_m = None
        rows.append(
            CrosswalkRow(
                iRiv=int(iriv_value),
                iEle=int(iele_value),
                segment_order=segment_order,
                length_m=length_m,
            )
        )
    return rows


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
