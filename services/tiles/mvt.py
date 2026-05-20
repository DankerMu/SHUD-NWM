from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

SAFE_TILE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MVT_MEDIA_TYPE = "application/x-protobuf"
MVT_EXTENT = 4096
MVT_BUFFER = 64
MVT_SCHEMA_VERSION = "m16-hydrology-mvt-v1"
MVT_ENCODER_VERSION = "deterministic-mvt-v1"
MVT_MAX_ZOOM = 14
MVT_MAX_FEATURES = 10_000
MVT_MAX_COORDINATES = 50_000
MVT_MAX_BYTES = 5_000_000
MVT_MIN_SIMPLIFICATION_TOLERANCE_M = 0.5
MVT_MAX_SIMPLIFICATION_TOLERANCE_M = 256.0
POSTGIS_NON_FINITE_DOUBLE_SQL = (
    "'NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision"
)
WEB_MERCATOR_BOUNDS = [-20037508.342789244, -20037508.342789244, 20037508.342789244, 20037508.342789244]
CHINA_WGS84_BOUNDS = [73.5, 18.1, 134.8, 53.6]


@dataclass(frozen=True)
class TileInput:
    layer_id: str
    source_id: str
    source_version: str
    valid_time: str | None
    z: int
    x: int
    y: int
    style_id: str = "default"
    variant_id: str | None = None
    schema_version: str = MVT_SCHEMA_VERSION
    encoder_version: str = MVT_ENCODER_VERSION


@dataclass(frozen=True)
class TileResponse:
    data: bytes
    checksum: str
    etag: str
    cache_key: str
    cache_status: str
    layer_id: str


def validate_identifier(value: str, field_name: str) -> None:
    if not SAFE_TILE_IDENTIFIER_RE.fullmatch(value):
        from apps.api.errors import ApiError

        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"{field_name} must be a stable tile identifier.",
            details={field_name: value},
        )


def validate_xyz(z: int, x: int, y: int, *, max_zoom: int = MVT_MAX_ZOOM) -> None:
    from apps.api.errors import ApiError

    if z < 0 or z > max_zoom:
        raise ApiError(
            status_code=422,
            code="TILE_XYZ_INVALID",
            message="Tile z is outside the supported Web Mercator zoom range.",
            details={"z": z, "min_z": 0, "max_z": max_zoom},
        )
    limit = 1 << z
    if x < 0 or y < 0 or x >= limit or y >= limit:
        raise ApiError(
            status_code=422,
            code="TILE_XYZ_INVALID",
            message="Tile x/y are outside the standard Web Mercator XYZ tile matrix.",
            details={"z": z, "x": x, "y": y, "min": 0, "max_exclusive": limit},
        )


def cache_key(tile: TileInput) -> str:
    basis = {
        "encoder_version": tile.encoder_version,
        "layer_id": tile.layer_id,
        "schema_version": tile.schema_version,
        "source_id": tile.source_id,
        "source_version": tile.source_version,
        "style_id": tile.style_id,
        "valid_time": tile.valid_time,
        "variant_id": tile.variant_id,
        "x": tile.x,
        "y": tile.y,
        "z": tile.z,
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def stable_etag(data: bytes) -> str:
    return f'W/"m16-{hashlib.sha256(data).hexdigest()}"'


def public_hydro_layer_id(variable: str) -> str:
    return {"q_down": "discharge", "water_level": "water-level"}.get(variable, f"hydro:{variable}")


def simplification_tolerance_m(z: int) -> float:
    validate_xyz(z, 0, 0)
    tile_width_m = (WEB_MERCATOR_BOUNDS[2] - WEB_MERCATOR_BOUNDS[0]) / float(1 << z)
    pixel_width_m = tile_width_m / float(MVT_EXTENT)
    return min(MVT_MAX_SIMPLIFICATION_TOLERANCE_M, max(MVT_MIN_SIMPLIFICATION_TOLERANCE_M, pixel_width_m / 2.0))


def build_tile_response(
    session: Session,
    tile: TileInput,
    layer_name: str,
    features: list[Mapping[str, Any]],
) -> TileResponse:
    validate_xyz(tile.z, tile.x, tile.y)
    _enforce_feature_budget(features)
    key = cache_key(tile)
    cached = _safe_read_cache(session, tile, key)
    if cached is not None:
        data, checksum, etag = cached
        return TileResponse(
            data=data,
            checksum=checksum,
            etag=etag,
            cache_key=key,
            cache_status="hit",
            layer_id=tile.layer_id,
        )

    data = encode_mvt_layer(layer_name, features, extent=MVT_EXTENT)
    if len(data) > MVT_MAX_BYTES:
        from apps.api.errors import ApiError

        raise ApiError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Encoded MVT tile payload exceeded the configured byte budget.",
            details={"max_bytes": MVT_MAX_BYTES, "payload_bytes": len(data), "layer_id": tile.layer_id},
        )
    checksum = hashlib.sha256(data).hexdigest()
    etag = stable_etag(data)
    cache_status = "miss" if _safe_write_cache(session, tile, key, data, checksum, etag) else "bypass"
    return TileResponse(
        data=data,
        checksum=checksum,
        etag=etag,
        cache_key=key,
        cache_status=cache_status,
        layer_id=tile.layer_id,
    )


def build_raw_tile_response(session: Session, tile: TileInput, data: bytes) -> TileResponse:
    validate_xyz(tile.z, tile.x, tile.y)
    if len(data) > MVT_MAX_BYTES:
        from apps.api.errors import ApiError

        raise ApiError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Raw MVT tile payload exceeded the configured byte budget.",
            details={"max_bytes": MVT_MAX_BYTES, "payload_bytes": len(data), "layer_id": tile.layer_id},
        )

    key = cache_key(tile)
    cached = _safe_read_cache(session, tile, key)
    if cached is not None:
        cached_data, checksum, etag = cached
        return TileResponse(
            data=cached_data,
            checksum=checksum,
            etag=etag,
            cache_key=key,
            cache_status="hit",
            layer_id=tile.layer_id,
        )

    checksum = hashlib.sha256(data).hexdigest()
    etag = stable_etag(data)
    cache_status = "miss" if _safe_write_cache(session, tile, key, data, checksum, etag) else "bypass"
    return TileResponse(
        data=data,
        checksum=checksum,
        etag=etag,
        cache_key=key,
        cache_status=cache_status,
        layer_id=tile.layer_id,
    )


def read_cached_tile_response(session: Session, tile: TileInput) -> TileResponse | None:
    validate_xyz(tile.z, tile.x, tile.y)
    key = cache_key(tile)
    cached = _safe_read_cache(session, tile, key)
    if cached is None:
        return None
    data, checksum, etag = cached
    return TileResponse(
        data=data,
        checksum=checksum,
        etag=etag,
        cache_key=key,
        cache_status="hit",
        layer_id=tile.layer_id,
    )


def encode_mvt_layer(layer_name: str, features: list[Mapping[str, Any]], *, extent: int = MVT_EXTENT) -> bytes:
    validate_identifier(layer_name, "source_layer")
    keys = _ordered_keys(features)
    value_index: dict[tuple[str, Any], int] = {}
    values: list[Any] = []
    encoded_features = []
    for index, feature in enumerate(features):
        tags: list[int] = []
        for key_index, key in enumerate(keys):
            if key not in feature:
                continue
            value = _validated_property(feature[key], key)
            value_key = _value_key(value)
            if value_key not in value_index:
                value_index[value_key] = len(values)
                values.append(value)
            tags.extend([key_index, value_index[value_key]])
        point_x = 1 + ((index * 7919) % max(extent - 2, 1))
        point_y = 1 + ((index * 1543) % max(extent - 2, 1))
        encoded_features.append(
            b"".join(
                (
                    _field_varint(1, index + 1),
                    _field_packed_varints(2, tags),
                    _field_varint(3, 1),
                    _field_packed_varints(4, [9, _zig_zag(point_x), _zig_zag(point_y)]),
                )
            )
        )
    layer_payload = bytearray()
    layer_payload.extend(_field_string(1, layer_name))
    for feature_payload in encoded_features:
        layer_payload.extend(_field_message(2, feature_payload))
    for key in keys:
        layer_payload.extend(_field_string(3, key))
    for value in values:
        layer_payload.extend(_field_message(4, _encode_value(value)))
    layer_payload.extend(_field_varint(5, extent))
    layer_payload.extend(_field_varint(15, 2))
    return _field_message(3, bytes(layer_payload))


def postgis_tile_sql(layer: str) -> str:
    layer_name = _source_layer_id(layer)
    if layer == "river-network":
        required_property_checks = {
            "feature_id": "feature_id IS NULL OR feature_id::text = ''",
            "segment_id": "segment_id IS NULL OR segment_id::text = ''",
            "river_segment_id": "river_segment_id IS NULL OR river_segment_id::text = ''",
            "river_network_version_id": "river_network_version_id IS NULL OR river_network_version_id::text = ''",
            "basin_version_id": "basin_version_id IS NULL OR basin_version_id::text = ''",
        }
        source_cte = """
            SELECT (rs.river_network_version_id || '::' || rs.river_segment_id) AS feature_id,
                   rs.river_segment_id AS segment_id,
                   rs.river_segment_id,
                   rs.river_network_version_id,
                   CAST(:basin_version_id AS text) AS basin_version_id,
                   rs.geom, rs.properties_json
            FROM core.river_segment rs
            JOIN core.model_instance mi ON mi.river_network_version_id = rs.river_network_version_id
            WHERE mi.basin_version_id = :basin_version_id
        """
    elif layer == "hydro":
        required_property_checks = {
            "feature_id": "feature_id IS NULL OR feature_id::text = ''",
            "segment_id": "segment_id IS NULL OR segment_id::text = ''",
            "river_segment_id": "river_segment_id IS NULL OR river_segment_id::text = ''",
            "river_network_version_id": "river_network_version_id IS NULL OR river_network_version_id::text = ''",
            "basin_version_id": "basin_version_id IS NULL OR basin_version_id::text = ''",
            "value": f"value IS NULL OR value::double precision IN ({POSTGIS_NON_FINITE_DOUBLE_SQL})",
            "unit": "unit IS NULL OR unit::text = ''",
            "quality_flag": "quality_flag IS NULL OR quality_flag::text = ''",
            "valid_time": "valid_time IS NULL",
        }
        source_cte = """
            SELECT (ts.river_network_version_id || '::' || ts.river_segment_id) AS feature_id,
                   ts.river_segment_id AS segment_id,
                   ts.river_segment_id,
                   ts.river_network_version_id,
                   ts.basin_version_id,
                   ts.value, ts.unit,
                   ts.quality_flag, ts.valid_time, rs.geom
            FROM hydro.river_timeseries ts
            JOIN core.river_segment rs
              ON rs.river_segment_id = ts.river_segment_id
             AND rs.river_network_version_id = ts.river_network_version_id
            WHERE ts.run_id = :run_id
              AND ts.variable = :variable
              AND ts.valid_time = :valid_time
        """
    elif layer == "flood-return-period":
        required_property_checks = {
            "feature_id": "feature_id IS NULL OR feature_id::text = ''",
            "segment_id": "segment_id IS NULL OR segment_id::text = ''",
            "river_segment_id": "river_segment_id IS NULL OR river_segment_id::text = ''",
            "river_network_version_id": "river_network_version_id IS NULL OR river_network_version_id::text = ''",
            "basin_version_id": "basin_version_id IS NULL OR basin_version_id::text = ''",
            "value": f"value IS NULL OR value::double precision IN ({POSTGIS_NON_FINITE_DOUBLE_SQL})",
            "unit": "unit IS NULL OR unit::text = ''",
            "quality_flag": "quality_flag IS NULL OR quality_flag::text = ''",
            "return_period": (
                "return_period IS NULL "
                f"OR return_period::double precision IN ({POSTGIS_NON_FINITE_DOUBLE_SQL})"
            ),
            "warning_level": "warning_level IS NULL OR warning_level::text = ''",
            "valid_time": "valid_time IS NULL",
        }
        source_cte = """
            SELECT (r.river_network_version_id || '::' || r.river_segment_id) AS feature_id,
                   r.river_segment_id AS segment_id,
                   r.river_segment_id,
                   r.river_network_version_id,
                   r.basin_version_id,
                   r.q_value AS value,
                   r.q_unit AS unit, r.quality_flag, r.return_period, r.warning_level, r.valid_time, rs.geom
            FROM flood.return_period_result r
            JOIN core.river_segment rs
              ON rs.river_segment_id = r.river_segment_id
             AND rs.river_network_version_id = r.river_network_version_id
            WHERE r.run_id = :run_id
              AND r.duration = :duration
              AND r.valid_time = :valid_time
              AND r.max_over_window = false
        """
    else:
        raise ValueError(f"Unsupported tile layer: {layer}")
    invalid_property_count_sql = " + ".join(
        f"COUNT(*) FILTER (WHERE {condition})" for condition in required_property_checks.values()
    )
    invalid_property_names_sql = ",\n                   ".join(
        f"CASE WHEN COUNT(*) FILTER (WHERE {condition}) > 0 THEN '{name}' END"
        for name, condition in required_property_checks.items()
    )
    return f"""
        WITH bounds AS (
            SELECT ST_TileEnvelope(:z, :x, :y) AS geom_3857
        ),
        source_rows AS (
            {source_cte}
        ),
        source_stats AS (
            SELECT CASE WHEN EXISTS (SELECT 1 FROM source_rows) THEN 1 ELSE 0 END AS source_feature_count
        ),
        intersecting AS (
            SELECT source_rows.*,
                   ST_NPoints(source_rows.geom) AS source_coordinate_count,
                   ST_NDims(source_rows.geom) AS source_coordinate_dimensions
            FROM source_rows, bounds
            WHERE source_rows.geom IS NOT NULL
              AND source_rows.geom && ST_Transform(bounds.geom_3857, 4490)
        ),
        simplified AS (
            SELECT intersecting.*,
                   ST_SimplifyPreserveTopology(
                       ST_MakeValid(ST_Transform(intersecting.geom, 3857)),
                       :simplification_tolerance_m
                   ) AS geom_3857
            FROM intersecting
        ),
        prefilter_stats AS (
            SELECT COUNT(*) AS intersecting_feature_count,
                   COALESCE(SUM(source_coordinate_count), 0) AS intersecting_coordinate_count,
                   COALESCE(MAX(source_coordinate_count), 0) AS feature_coordinate_count,
                   COUNT(*) FILTER (
                       WHERE source_coordinate_count > :feature_coordinate_limit
                   ) AS feature_coordinate_overflow_count,
                   COALESCE(MAX(source_coordinate_dimensions), 0) AS coordinate_dimension_count,
                   COUNT(*) FILTER (
                       WHERE source_coordinate_dimensions > :max_coordinate_dimensions
                   ) AS coordinate_dimension_overflow_count,
                   {invalid_property_count_sql} AS invalid_property_count,
                   concat_ws(',',
                   {invalid_property_names_sql}
                   ) AS invalid_properties
            FROM intersecting
        ),
        clipped AS (
            SELECT simplified.*,
                   ST_AsMVTGeom(
                       simplified.geom_3857,
                       bounds.geom_3857,
                       extent => {MVT_EXTENT},
                       buffer => {MVT_BUFFER},
                       clip_geom => true
                   ) AS mvt_geom
            FROM simplified, bounds
            WHERE simplified.source_coordinate_count <= :feature_coordinate_limit
              AND simplified.source_coordinate_dimensions <= :max_coordinate_dimensions
        ),
        budgeted AS (
            SELECT *, COUNT(*) OVER () AS feature_count, SUM(ST_NPoints(geom)) OVER () AS coordinate_count
            FROM clipped
            WHERE mvt_geom IS NOT NULL
        ),
        budget_stats AS (
            SELECT COALESCE(MAX(feature_count), 0) AS feature_count,
                   COALESCE(MAX(coordinate_count), 0) AS coordinate_count
            FROM budgeted
        )
        SELECT (
            SELECT ST_AsMVT(tile_rows, '{layer_name}', {MVT_EXTENT}, 'mvt_geom')
            FROM (
                SELECT *
                FROM budgeted
                WHERE feature_count <= :feature_limit
                  AND coordinate_count <= :collection_coordinate_limit
                ORDER BY river_network_version_id, river_segment_id
            ) AS tile_rows
        ) AS tile,
        (SELECT source_feature_count FROM source_stats) AS source_feature_count,
        (SELECT feature_count FROM budget_stats) AS feature_count,
        (SELECT coordinate_count FROM budget_stats) AS coordinate_count,
        (SELECT feature_coordinate_overflow_count FROM prefilter_stats) AS feature_coordinate_overflow_count,
        (SELECT feature_coordinate_count FROM prefilter_stats) AS feature_coordinate_count,
        (SELECT coordinate_dimension_overflow_count FROM prefilter_stats) AS coordinate_dimension_overflow_count,
        (SELECT coordinate_dimension_count FROM prefilter_stats) AS coordinate_dimension_count,
        (SELECT invalid_property_count FROM prefilter_stats) AS invalid_property_count,
        (SELECT invalid_properties FROM prefilter_stats) AS invalid_properties
        FROM source_stats, budget_stats, prefilter_stats
    """


def layer_metadata(
    layer_id: str,
    *,
    run_id: str | None = None,
    valid_times: list[str] | None = None,
    source_version: str | None = None,
    release_blocking: bool = False,
) -> dict[str, Any]:
    metadata_by_layer = {
        "river-network": {
            "tile_url_template": "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
            "maplibre_source_layer": "river_network",
            "required_placeholders": ["basin_version_id", "z", "x", "y"],
            "properties": ["segment_id", "river_segment_id", "basin_version_id", "river_network_version_id"],
        },
        "discharge": {
            "tile_url_template": "/api/v1/tiles/hydro/{run_id}/q_down/{valid_time}/{z}/{x}/{y}.pbf",
            "maplibre_source_layer": "hydro",
            "required_placeholders": ["run_id", "valid_time", "z", "x", "y"],
            "properties": [
                "segment_id",
                "river_segment_id",
                "basin_version_id",
                "river_network_version_id",
                "value",
                "unit",
                "quality_flag",
            ],
        },
        "water-level": {
            "tile_url_template": "/api/v1/tiles/hydro/{run_id}/water_level/{valid_time}/{z}/{x}/{y}.pbf",
            "maplibre_source_layer": "hydro",
            "required_placeholders": ["run_id", "valid_time", "z", "x", "y"],
            "properties": [
                "feature_id",
                "segment_id",
                "river_segment_id",
                "basin_version_id",
                "river_network_version_id",
                "value",
                "unit",
                "quality_flag",
            ],
        },
        "flood-return-period": {
            "tile_url_template": "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
            "maplibre_source_layer": "flood_return_period",
            "required_placeholders": ["run_id", "duration", "valid_time", "z", "x", "y"],
            "properties": [
                "feature_id",
                "segment_id",
                "river_segment_id",
                "basin_version_id",
                "river_network_version_id",
                "value",
                "unit",
                "return_period",
                "warning_level",
                "quality_flag",
            ],
        },
        "warning-level": {
            "tile_url_template": "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
            "maplibre_source_layer": "flood_return_period",
            "required_placeholders": ["run_id", "duration", "valid_time", "z", "x", "y"],
            "properties": ["feature_id", "segment_id", "river_network_version_id", "warning_level", "quality_flag"],
        },
    }
    base = metadata_by_layer.get(layer_id)
    if base is None:
        return {
            "layer_id": layer_id,
            "tile_format": "geojson_compatibility",
            "fallback_available": True,
            "release_blocking": release_blocking,
        }
    cache_layer_id = "flood-return-period" if layer_id == "warning-level" else layer_id
    is_warning_alias = layer_id == "warning-level"
    version = _stable_json_hash({"layer_id": layer_id, "source_version": source_version, "schema": MVT_SCHEMA_VERSION})
    return {
        "layer_id": layer_id,
        "tile_format": "mvt",
        "url_template": base["tile_url_template"],
        "tile_url_template": base["tile_url_template"],
        "required_placeholders": base["required_placeholders"],
        "maplibre_source_layer": base["maplibre_source_layer"],
        "source_layer": base["maplibre_source_layer"],
        "property_schema_version": MVT_SCHEMA_VERSION,
        "property_schema": {"version": MVT_SCHEMA_VERSION, "required": base["properties"]},
        "min_zoom": 0,
        "max_zoom": MVT_MAX_ZOOM,
        "bounds_crs": "EPSG:3857",
        "bounds": WEB_MERCATOR_BOUNDS,
        "wgs84_bounds": CHINA_WGS84_BOUNDS,
        "valid_times": valid_times or [],
        "source_refs": {"run_id": run_id, "source_version": source_version},
        "cache_layer_id": cache_layer_id,
        "route_variable": (
            "q_down"
            if layer_id == "discharge"
            else "water_level"
            if layer_id == "water-level"
            else "return_period"
            if layer_id in {"flood-return-period", "warning-level"}
            else None
        ),
        "alias_of": "flood-return-period" if is_warning_alias else None,
        "alias_semantic": "style_layer" if is_warning_alias else None,
        "canonical_route_layer_id": cache_layer_id,
        "legacy_layer_ids": [f"hydro:{'q_down' if layer_id == 'discharge' else 'water_level'}"]
        if layer_id in {"discharge", "water-level"}
        else ["flood_return_period_{run_id}"]
        if layer_id == "flood-return-period"
        else ["flood-return-period", "flood_return_period_{run_id}"]
        if is_warning_alias
        else [],
        "cache_etag": f'W/"metadata-{version}"',
        "cache_version": version,
        "schema_version": MVT_SCHEMA_VERSION,
        "encoder_version": MVT_ENCODER_VERSION,
        "fallback_available": layer_id == "flood-return-period",
        "fallback_endpoint": "/api/v1/tiles/flood-return-period" if layer_id == "flood-return-period" else None,
        "release_blocking": release_blocking,
        "production_mvt_readiness_claimed": False,
    }


def latest_ready_run(session: Session) -> Mapping[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT h.run_id, h.status, h.model_id, h.basin_version_id, h.source_id, h.cycle_time,
                   mi.river_network_version_id
            FROM hydro.hydro_run h
            LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
            WHERE h.status IN ('frequency_done', 'published')
            ORDER BY h.cycle_time DESC, h.run_id DESC
            LIMIT 1
            """
        )
    ).mappings().first()
    return dict(row) if row is not None else None


def valid_times_for_layer(session: Session, layer_id: str, *, run_id: str | None = None) -> list[str]:
    if layer_id in {"flood-return-period", "warning-level"}:
        sql = """
            SELECT DISTINCT valid_time
            FROM flood.return_period_result
            WHERE max_over_window = false
              AND (:run_id IS NULL OR run_id = :run_id)
            ORDER BY valid_time
        """
    elif layer_id in {"discharge", "water-level"}:
        variable = "q_down" if layer_id == "discharge" else "water_level"
        sql = """
            SELECT DISTINCT valid_time
            FROM hydro.river_timeseries
            WHERE variable = :variable
              AND (:run_id IS NULL OR run_id = :run_id)
            ORDER BY valid_time
        """
        rows = session.execute(text(sql), {"run_id": run_id, "variable": variable}).mappings().all()
        return [_format_time(row["valid_time"]) for row in rows]
    else:
        return []
    rows = session.execute(text(sql), {"run_id": run_id}).mappings().all()
    return [_format_time(row["valid_time"]) for row in rows]


def _source_layer_id(layer: str) -> str:
    return {"river-network": "river_network", "hydro": "hydro", "flood-return-period": "flood_return_period"}[layer]


def _enforce_feature_budget(features: list[Mapping[str, Any]]) -> None:
    from apps.api.errors import ApiError

    if len(features) > MVT_MAX_FEATURES:
        raise ApiError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="MVT tile feature budget exceeded.",
            details={"feature_count": len(features), "max_features": MVT_MAX_FEATURES},
        )
    coordinate_count = len(features)
    if coordinate_count > MVT_MAX_COORDINATES:
        raise ApiError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="MVT tile coordinate budget exceeded.",
            details={"coordinate_count": coordinate_count, "max_coordinates": MVT_MAX_COORDINATES},
        )


def _read_cache(session: Session, tile: TileInput, key: str) -> tuple[bytes, str, str] | None:
    if not _table_exists(session, "tile_cache", "map"):
        return None
    columns = _table_columns(session, "tile_cache", "map")
    checksum_sql = "checksum" if "checksum" in columns else "NULL AS checksum"
    key_filter = "cache_key = :cache_key" if "cache_key" in columns else "tile_uri = :cache_key"
    selected_optional_columns = [
        "source_id",
        "source_version",
        "valid_time",
        "style_id",
        "schema_version",
        "encoder_version",
        "status",
    ]
    optional_select = "".join(f", {column}" for column in selected_optional_columns if column in columns)
    row = session.execute(
        text(
            f"""
            SELECT tile_data, {checksum_sql}, etag{optional_select}
            FROM map.tile_cache
            WHERE layer_id = :layer_id
              AND z = :z
              AND x = :x
              AND y = :y
              AND {key_filter}
            LIMIT 1
            """
        ),
        {"layer_id": tile.layer_id, "z": tile.z, "x": tile.x, "y": tile.y, "cache_key": key},
    ).mappings().first()
    if row is None or row["tile_data"] is None:
        return None
    data = bytes(row["tile_data"])
    if len(data) > MVT_MAX_BYTES:
        return None
    computed_checksum = hashlib.sha256(data).hexdigest()
    checksum = str(row.get("checksum") or "")
    if not checksum or checksum != computed_checksum:
        return None
    computed_etag = stable_etag(data)
    etag = str(row.get("etag") or "")
    if not etag or etag != computed_etag:
        return None
    if "status" in columns and row.get("status") != "ready":
        return None
    if "schema_version" in columns and row.get("schema_version") != tile.schema_version:
        return None
    if "encoder_version" in columns and row.get("encoder_version") != tile.encoder_version:
        return None
    if "source_id" in columns and row.get("source_id") != tile.source_id:
        return None
    if "source_version" in columns and row.get("source_version") != tile.source_version:
        return None
    if "valid_time" in columns and _format_time(row.get("valid_time")) != _format_time(tile.valid_time):
        return None
    if "style_id" in columns and row.get("style_id") != tile.style_id:
        return None
    return data, checksum, etag


def _safe_read_cache(
    session: Session,
    tile: TileInput,
    key: str,
) -> tuple[bytes, str, str] | None:
    try:
        return _read_cache(session, tile, key)
    except SQLAlchemyError:
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        return None


def _write_cache(session: Session, tile: TileInput, key: str, data: bytes, checksum: str, etag: str) -> bool:
    if not _table_exists(session, "tile_cache", "map"):
        return False
    if not _ensure_tile_layer(session, tile):
        return False
    params = {
        "layer_id": tile.layer_id,
        "z": tile.z,
        "x": tile.x,
        "y": tile.y,
        "tile_data": data,
        "tile_uri": key,
        "cache_key": key,
        "etag": etag,
        "checksum": checksum,
        "source_id": tile.source_id,
        "source_version": tile.source_version,
        "valid_time": tile.valid_time,
        "style_id": tile.style_id,
        "schema_version": tile.schema_version,
        "encoder_version": tile.encoder_version,
        "status": "ready",
    }
    columns = _table_columns(session, "tile_cache", "map")
    supported = {name: value for name, value in params.items() if name in columns}
    if "cache_key" not in supported:
        return False
    conflict_target = "(cache_key)"
    conflict_immutable = {"cache_key"}
    assignments = ", ".join(f"{name} = excluded.{name}" for name in supported if name not in conflict_immutable)
    if not assignments:
        return False
    session.execute(
        text(
            f"""
            INSERT INTO map.tile_cache ({", ".join(supported)})
            VALUES ({", ".join(f":{name}" for name in supported)})
            ON CONFLICT{conflict_target} DO UPDATE SET {assignments}
            """
        ),
        supported,
    )
    session.commit()
    return True


def _ensure_tile_layer(session: Session, tile: TileInput) -> bool:
    if not _table_exists(session, "tile_layer", "map"):
        return True
    columns = _table_columns(session, "tile_layer", "map")
    metadata = _cache_layer_metadata(tile)
    values = {
        "layer_id": tile.layer_id,
        "layer_type": metadata["layer_type"],
        "source_run_id": tile.source_id if tile.layer_id != "river-network" else None,
        "source_product_id": tile.source_id,
        "source_version": tile.source_version,
        "variable": metadata.get("variable"),
        "valid_time": tile.valid_time,
        "tile_format": "mvt",
        "tile_uri_template": metadata["tile_uri_template"],
        "maplibre_source_layer": metadata["maplibre_source_layer"],
        "property_schema_version": tile.schema_version,
        "cache_version": _stable_json_hash(
            {"layer_id": tile.layer_id, "schema": tile.schema_version, "source_version": tile.source_version}
        ),
        "fallback_available": bool(metadata["fallback_available"]),
        "release_blocking": False,
        "min_zoom": 0,
        "max_zoom": MVT_MAX_ZOOM,
        "published_flag": False,
    }
    supported = {name: value for name, value in values.items() if name in columns}
    required = {"layer_id", "layer_type", "tile_format", "tile_uri_template"}
    if not required.issubset(supported):
        return False
    assignments = [
        f"{column} = excluded.{column}"
        for column in supported
        if column not in {"layer_id"}
    ]
    if not assignments:
        return True
    session.execute(
        text(
            f"""
            INSERT INTO map.tile_layer ({", ".join(supported)})
            VALUES ({", ".join(f":{name}" for name in supported)})
            ON CONFLICT (layer_id) DO UPDATE SET {", ".join(assignments)}
            """
        ),
        supported,
    )
    return True


def _cache_layer_metadata(tile: TileInput) -> dict[str, Any]:
    if tile.layer_id == "flood-return-period":
        return {
            "layer_type": "flood_return_period",
            "tile_uri_template": "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
            "maplibre_source_layer": "flood_return_period",
            "variable": "return_period",
            "fallback_available": True,
        }
    if tile.layer_id == "river-network":
        return {
            "layer_type": "river_network",
            "tile_uri_template": "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
            "maplibre_source_layer": "river_network",
            "variable": None,
            "fallback_available": False,
        }
    if tile.layer_id in {"discharge", "water-level"}:
        variable = "q_down" if tile.layer_id == "discharge" else "water_level"
        return {
            "layer_type": "hydrological_output",
            "tile_uri_template": f"/api/v1/tiles/hydro/{{run_id}}/{variable}/{{valid_time}}/{{z}}/{{x}}/{{y}}.pbf",
            "maplibre_source_layer": "hydro",
            "variable": variable,
            "fallback_available": False,
        }
    if tile.layer_id.startswith("hydro:"):
        variable = tile.layer_id.split(":", 1)[1]
        return {
            "layer_type": "hydrological_output",
            "tile_uri_template": f"/api/v1/tiles/hydro/{{run_id}}/{variable}/{{valid_time}}/{{z}}/{{x}}/{{y}}.pbf",
            "maplibre_source_layer": "hydro",
            "variable": variable,
            "fallback_available": False,
        }
    return {
        "layer_type": "mvt",
        "tile_uri_template": f"/api/v1/tiles/{tile.layer_id}/{{z}}/{{x}}/{{y}}.pbf",
        "maplibre_source_layer": (
            _source_layer_id(tile.layer_id)
            if tile.layer_id in {"river-network", "flood-return-period"}
            else tile.layer_id
        ),
        "variable": None,
        "fallback_available": False,
    }


def _safe_write_cache(session: Session, tile: TileInput, key: str, data: bytes, checksum: str, etag: str) -> bool:
    try:
        return _write_cache(session, tile, key, data, checksum, etag)
    except SQLAlchemyError:
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        return False


def _table_exists(session: Session, table_name: str, schema: str) -> bool:
    if session.get_bind().dialect.name == "sqlite":
        try:
            row = session.execute(
                text(f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name=:table_name"),
                {"table_name": table_name},
            ).first()
        except SQLAlchemyError:
            return False
        return row is not None
    row = session.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = :schema AND table_name = :table_name
            LIMIT 1
            """
        ),
        {"schema": schema, "table_name": table_name},
    ).first()
    return row is not None


def _table_columns(session: Session, table_name: str, schema: str) -> set[str]:
    if session.get_bind().dialect.name == "sqlite":
        try:
            rows = session.execute(text(f"PRAGMA {schema}.table_info({table_name})")).mappings().all()
        except SQLAlchemyError:
            return set()
        return {str(row["name"]) for row in rows}
    rows = session.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = :table_name
            """
        ),
        {"schema": schema, "table_name": table_name},
    ).mappings().all()
    return {str(row["column_name"]) for row in rows}


def _ordered_keys(features: list[Mapping[str, Any]]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for feature in features:
        for key in feature:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _validated_property(value: Any, field_name: str) -> str | int | float | bool:
    from apps.api.errors import ApiError

    if value is None:
        raise ApiError(
            status_code=500,
            code="MVT_PROPERTY_INVALID",
            message="MVT required feature property is missing.",
            details={"field": field_name},
        )
    if isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ApiError(
                status_code=500,
                code="MVT_PROPERTY_INVALID",
                message="MVT numeric feature property must be finite.",
                details={"field": field_name},
            )
        return value
    return str(value)


def _value_key(value: str | int | float | bool) -> tuple[str, Any]:
    return (type(value).__name__, value)


def _encode_value(value: str | int | float | bool) -> bytes:
    if isinstance(value, bool):
        return _field_varint(7, 1 if value else 0)
    if isinstance(value, int):
        return _field_varint(4, value)
    if isinstance(value, float):
        import struct

        return _key(3, 1) + struct.pack("<d", value)
    return _field_string(1, value)


def _field_message(field_number: int, payload: bytes) -> bytes:
    return _key(field_number, 2) + _varint(len(payload)) + payload


def _field_string(field_number: int, value: str) -> bytes:
    payload = value.encode("utf-8")
    return _key(field_number, 2) + _varint(len(payload)) + payload


def _field_varint(field_number: int, value: int) -> bytes:
    return _key(field_number, 0) + _varint(value)


def _field_packed_varints(field_number: int, values: Iterable[int]) -> bytes:
    payload = b"".join(_varint(value) for value in values)
    return _key(field_number, 2) + _varint(len(payload)) + payload


def _key(field_number: int, wire_type: int) -> bytes:
    return _varint((field_number << 3) | wire_type)


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot encode negative values")
    chunks = bytearray()
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            chunks.append(to_write | 0x80)
        else:
            chunks.append(to_write)
            return bytes(chunks)


def _zig_zag(value: int) -> int:
    return (value << 1) ^ (value >> 31)


def _stable_json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _format_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    text_value = str(value)
    if " " in text_value and "T" not in text_value:
        text_value = text_value.replace(" ", "T", 1)
    if text_value.endswith("+00:00"):
        return text_value[:-6] + "Z"
    return text_value
