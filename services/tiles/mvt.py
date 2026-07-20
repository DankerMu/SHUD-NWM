from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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
MVT_MAX_TILE_COORDINATE = (1 << MVT_MAX_ZOOM) - 1
MVT_MAX_FEATURES = 10_000
MVT_MAX_COORDINATES = 50_000
MVT_MAX_BYTES = 5_000_000
MVT_VALID_TIME_SAMPLE_LIMIT = 100
MVT_MIN_SIMPLIFICATION_TOLERANCE_M = 0.5
MVT_MAX_SIMPLIFICATION_TOLERANCE_M = 256.0
MVT_FILE_CACHE_DIR_ENV = "NHMS_MVT_FILE_CACHE_DIR"
SUPPORTED_HYDRO_MVT_VARIABLES = ("q_down",)
POSTGIS_NON_FINITE_DOUBLE_SQL = (
    "'NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision"
)
WEB_MERCATOR_BOUNDS = [-20037508.342789244, -20037508.342789244, 20037508.342789244, 20037508.342789244]
CHINA_WGS84_BOUNDS = [73.5, 18.1, 134.8, 53.6]

_LOCAL_TILE_LOCKS_GUARD = threading.Lock()
_LOCAL_TILE_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()


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


@dataclass(frozen=True)
class ValidTimeDiscovery:
    valid_times: list[str]
    limit: int
    observed_count: int
    truncated: bool

    def model_dump(self) -> dict[str, Any]:
        return {
            "valid_times": self.valid_times,
            "items": self.valid_times,
            "limit": self.limit,
            "observed_count": self.observed_count,
            "truncated": self.truncated,
        }


class TileError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def validate_identifier(value: str, field_name: str) -> None:
    if not SAFE_TILE_IDENTIFIER_RE.fullmatch(value):
        raise TileError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"{field_name} must be a stable tile identifier.",
            details={field_name: value},
        )


def validate_xyz(z: int, x: int, y: int, *, max_zoom: int = MVT_MAX_ZOOM) -> None:
    if z < 0 or z > max_zoom:
        raise TileError(
            status_code=422,
            code="TILE_XYZ_INVALID",
            message="Tile z is outside the supported Web Mercator zoom range.",
            details={"z": z, "min_z": 0, "max_z": max_zoom},
        )
    limit = 1 << z
    if x < 0 or y < 0 or x >= limit or y >= limit:
        raise TileError(
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
        "valid_time": canonical_mvt_time(tile.valid_time),
        "variant_id": tile.variant_id,
        "x": tile.x,
        "y": tile.y,
        "z": tile.z,
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@contextmanager
def tile_generation_lock(tile: TileInput) -> Iterable[None]:
    """Single-flight a cache miss in this process and across uvicorn workers."""
    key = cache_key(tile)
    with _LOCAL_TILE_LOCKS_GUARD:
        local_lock = _LOCAL_TILE_LOCKS.get(key)
        if local_lock is None:
            local_lock = threading.Lock()
            _LOCAL_TILE_LOCKS[key] = local_lock

    with local_lock:
        lock_path = _file_cache_lock_path(key)
        if lock_path is None:
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def stable_etag(data: bytes) -> str:
    return f'W/"m16-{hashlib.sha256(data).hexdigest()}"'


def public_hydro_layer_id(variable: str) -> str:
    return {"q_down": "discharge"}.get(variable, f"hydro:{variable}")


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
    if cached is None:
        cached = _safe_read_file_cache(key)
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
        raise TileError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Encoded MVT tile payload exceeded the configured byte budget.",
            details={"max_bytes": MVT_MAX_BYTES, "payload_bytes": len(data), "layer_id": tile.layer_id},
        )
    checksum = hashlib.sha256(data).hexdigest()
    etag = stable_etag(data)
    cache_status = (
        "miss"
        if _safe_write_cache(session, tile, key, data, checksum, etag)
        or _safe_write_file_cache(key, data)
        else "bypass"
    )
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
        raise TileError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Raw MVT tile payload exceeded the configured byte budget.",
            details={"max_bytes": MVT_MAX_BYTES, "payload_bytes": len(data), "layer_id": tile.layer_id},
        )

    key = cache_key(tile)
    cached = _safe_read_cache(session, tile, key)
    if cached is None:
        cached = _safe_read_file_cache(key)
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
    cache_status = (
        "miss"
        if _safe_write_cache(session, tile, key, data, checksum, etag)
        or _safe_write_file_cache(key, data)
        else "bypass"
    )
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
        cached = _safe_read_file_cache(key)
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
    source_identity_stats_sql = (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM source_rows) THEN 1 ELSE 0 END AS source_identity_count"
    )
    if layer in {"river-network", "river-network-national"}:
        required_property_checks = {
            "feature_id": "feature_id IS NULL OR feature_id::text = ''",
            "segment_id": "segment_id IS NULL OR segment_id::text = ''",
            "river_segment_id": "river_segment_id IS NULL OR river_segment_id::text = ''",
            "river_network_version_id": "river_network_version_id IS NULL OR river_network_version_id::text = ''",
            "basin_version_id": "basin_version_id IS NULL OR basin_version_id::text = ''",
        }
        network_filter = (
            "rnv.basin_version_id = :basin_version_id"
            if layer == "river-network"
            else "EXISTS (SELECT 1 FROM core.model_instance mi "
            "WHERE mi.river_network_version_id = rnv.river_network_version_id AND mi.active_flag = true)"
        )
        source_cte = f"""
            SELECT (rs.river_network_version_id || '::' || rs.river_segment_id) AS feature_id,
                   rs.river_segment_id AS segment_id,
                   rs.river_segment_id,
                   rs.river_network_version_id,
                   rnv.basin_version_id,
                   rs.stream_type AS "Type",
                   CASE
                       WHEN :z >= 9 THEN rs.geom
                       ELSE ST_Transform(
                           ST_SimplifyPreserveTopology(
                               ST_Transform(rs.geom, 3857),
                               CASE
                                   WHEN :z <= 4 THEN 2000.0
                                   WHEN :z = 5 THEN 1000.0
                                   WHEN :z = 6 THEN 500.0
                                   WHEN :z = 7 THEN 200.0
                                   ELSE 80.0
                               END
                           ),
                           4490
                       )
                   END AS geom
            FROM core.river_segment rs
            JOIN core.river_network_version rnv
              ON rnv.river_network_version_id = rs.river_network_version_id
            CROSS JOIN bounds
            WHERE {network_filter}
              AND rs.geom IS NOT NULL
              AND rs.geom && ST_Transform(bounds.geom_3857, 4490)
              AND (
                  :z >= 9
                  OR rs.stream_type >= CASE
                      WHEN :z <= 4 THEN 5.0
                      WHEN :z = 5 THEN 4.0
                      WHEN :z = 6 THEN 3.0
                      WHEN :z = 7 THEN 2.0
                      ELSE 1.0
                  END
              )
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
            "run_id": "run_id IS NULL OR run_id::text = ''",
            "variable": "variable IS NULL OR variable::text = ''",
            "valid_time": "valid_time IS NULL",
        }
        source_cte = """
            SELECT (ts.river_network_version_id || '::' || ts.river_segment_id) AS feature_id,
                   ts.river_segment_id AS segment_id,
                   ts.river_segment_id,
                   ts.river_network_version_id,
                   ts.basin_version_id,
                   ts.value, ts.unit,
                   ts.quality_flag,
                   ts.run_id, ts.variable,
                   to_char(ts.valid_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS valid_time,
                   rs.geom
            FROM hydro.river_timeseries ts
            JOIN core.river_segment rs
              ON rs.river_segment_id = ts.river_segment_id
             AND rs.river_network_version_id = ts.river_network_version_id
            WHERE ts.run_id = :run_id
              AND ts.basin_version_id = :basin_version_id
              AND ts.river_network_version_id = :river_network_version_id
              AND ts.variable = :variable
              AND ts.valid_time = :valid_time
        """
    elif layer == "hydro-national":
        # National overview: render q_down for every basin by joining each river
        # network's latest display-ready run. Identity (run/network) is chosen by
        # the deterministic DISTINCT ON sub-select, not by request parameters; only
        # :variable and :valid_time are bound. Output columns/checks match "hydro".
        required_property_checks = {
            "feature_id": "feature_id IS NULL OR feature_id::text = ''",
            "segment_id": "segment_id IS NULL OR segment_id::text = ''",
            "river_segment_id": "river_segment_id IS NULL OR river_segment_id::text = ''",
            "river_network_version_id": "river_network_version_id IS NULL OR river_network_version_id::text = ''",
            "basin_version_id": "basin_version_id IS NULL OR basin_version_id::text = ''",
            "value": f"value IS NULL OR value::double precision IN ({POSTGIS_NON_FINITE_DOUBLE_SQL})",
            "unit": "unit IS NULL OR unit::text = ''",
            "quality_flag": "quality_flag IS NULL OR quality_flag::text = ''",
            "run_id": "run_id IS NULL OR run_id::text = ''",
            "variable": "variable IS NULL OR variable::text = ''",
            "valid_time": "valid_time IS NULL",
        }
        # Trunk generalization by zoom. Output reaches inherit river.shp ``Type``
        # during geometry backfill, so topology-backed source stream class is the
        # primary low-zoom selector. q_down rank remains the compatibility fallback
        # for older/imported rows without ``Type``.
        # We rank segments WITHIN each river network (PERCENT_RANK partitioned by
        # river_network_version_id, so a small basin's trunk is kept even though its
        # absolute q is lower than a big river's tributary) and keep only the top
        # fraction at low zoom. NULL-value segments cannot be ranked as trunk and are
        # dropped at low zoom (low zoom only shows data-bearing main channels). The CASE
        # is keyed on the existing :z bind (ST_TileEnvelope(:z,...)). Cutoffs:
        #   z<=4 -> top 10% (cutoff 0.90): main trunk only
        #   z=5  -> top 30% (cutoff 0.70)
        #   z=6  -> top 60% (cutoff 0.40)
        #   z=7  -> top 85% (cutoff 0.15)
        #   z=8  -> top 96% (cutoff 0.04)
        #   z>=9 -> no filter (full detail).
        # Why z7/z8 still filter: a dense basin (e.g. Heihe) packs >10k segments / >50k
        # coordinates into a single z7 tile, blowing the per-tile budget (HTTP 413). The
        # progressive cutoff keeps the main channels and stays inside budget; by z9 a tile
        # covers a small enough area that full detail fits. The percent_rank filter runs
        # BEFORE bounded_rows/budget counting (it is in the source CTE), so it truly reduces
        # the feature set fed to the per-tile budget.
        #
        # Per-zoom coarse ST_SimplifyPreserveTopology (mercator metres) on the source geom
        # is applied here too so low-zoom trunks are also cheaper in coordinates; tolerance
        # decreases with zoom. Topology is preserved. z>=9 keeps the raw geom and relies on
        # the shared pixel-based simplify in the template.
        #   z<=4 -> 2000m, z=5 -> 1000m, z=6 -> 500m, z=7 -> 200m, z=8 -> 80m, z>=9 -> 0.
        #
        # Spatial filtering is deliberately pushed into the source CTE for this national
        # layer. Otherwise each z/x/y request ranks every nationwide river_timeseries row
        # for the selected valid_time before clipping a tiny tile, which turns cache misses
        # into multi-second gateway risks.
        source_identity_stats_sql = """
            SELECT CASE WHEN EXISTS (
                SELECT 1
                FROM hydro.river_timeseries ts
                JOIN (
                    SELECT DISTINCT ON (mi.river_network_version_id)
                           h.run_id, mi.river_network_version_id
                    FROM hydro.hydro_run h
                    JOIN core.model_instance mi ON mi.model_id = h.model_id
                    WHERE h.status IN ('succeeded', 'parsed', 'published')
                      AND mi.river_network_version_id IS NOT NULL
                    ORDER BY mi.river_network_version_id, h.cycle_time DESC, h.run_id DESC
                ) lr ON lr.run_id = ts.run_id AND lr.river_network_version_id = ts.river_network_version_id
                WHERE ts.variable = :variable
                  AND ts.valid_time = :valid_time
                LIMIT 1
            ) THEN 1 ELSE 0 END AS source_identity_count
        """
        source_cte = """
            WITH latest_runs AS MATERIALIZED (
                SELECT DISTINCT ON (mi.river_network_version_id)
                       h.run_id, mi.river_network_version_id
                FROM hydro.hydro_run h
                JOIN core.model_instance mi ON mi.model_id = h.model_id
                WHERE h.status IN ('succeeded', 'parsed', 'published')
                  AND mi.river_network_version_id IS NOT NULL
                ORDER BY mi.river_network_version_id, h.cycle_time DESC, h.run_id DESC
            ),
            tile_segments AS MATERIALIZED (
                SELECT rs.river_segment_id,
                       rs.river_network_version_id,
                       rs.stream_type
                FROM core.river_segment rs
                CROSS JOIN bounds
                WHERE rs.geom IS NOT NULL
                  AND rs.geom && ST_Transform(bounds.geom_3857, 4490)
                  AND (
                      :z >= 9
                      OR rs.stream_type IS NULL
                      OR rs.stream_type >= CASE
                          WHEN :z <= 4 THEN 5.0
                          WHEN :z = 5 THEN 4.0
                          WHEN :z = 6 THEN 3.0
                          WHEN :z = 7 THEN 2.0
                          ELSE 1.0
                      END
                  )
            ),
            typed_values AS (
                SELECT ts.river_segment_id,
                       ts.river_network_version_id,
                       ts.basin_version_id,
                       ts.value,
                       ts.unit,
                       ts.quality_flag,
                       ts.run_id,
                       ts.variable,
                       ts.valid_time
                FROM latest_runs lr
                JOIN hydro.river_timeseries ts
                  ON ts.run_id = lr.run_id
                 AND ts.river_network_version_id = lr.river_network_version_id
                JOIN tile_segments seg
                  ON seg.river_segment_id = ts.river_segment_id
                 AND seg.river_network_version_id = ts.river_network_version_id
                WHERE ts.variable = :variable
                  AND ts.valid_time = :valid_time
                  AND (:z >= 9 OR seg.stream_type IS NOT NULL)
            ),
            untyped_ranked AS (
                SELECT ts.river_segment_id,
                       ts.river_network_version_id,
                       ts.basin_version_id,
                       ts.value,
                       ts.unit,
                       ts.quality_flag,
                       ts.run_id,
                       ts.variable,
                       ts.valid_time,
                       CASE
                           WHEN ts.value IS NULL THEN NULL
                           ELSE PERCENT_RANK() OVER (
                               PARTITION BY ts.river_network_version_id
                               ORDER BY ts.value
                           )
                       END AS value_percent_rank
                FROM latest_runs lr
                JOIN hydro.river_timeseries ts
                  ON ts.run_id = lr.run_id
                 AND ts.river_network_version_id = lr.river_network_version_id
                JOIN tile_segments seg
                  ON seg.river_segment_id = ts.river_segment_id
                 AND seg.river_network_version_id = ts.river_network_version_id
                WHERE :z < 9
                  AND seg.stream_type IS NULL
                  AND ts.variable = :variable
                  AND ts.valid_time = :valid_time
            ),
            selected_values AS (
                SELECT river_segment_id, river_network_version_id, basin_version_id,
                       value, unit, quality_flag, run_id, variable, valid_time
                FROM typed_values
                UNION ALL
                SELECT river_segment_id, river_network_version_id, basin_version_id,
                       value, unit, quality_flag, run_id, variable, valid_time
                FROM untyped_ranked
                WHERE value_percent_rank IS NOT NULL
                  AND value_percent_rank >= CASE
                      WHEN :z <= 4 THEN 0.90
                      WHEN :z = 5 THEN 0.70
                      WHEN :z = 6 THEN 0.40
                      WHEN :z = 7 THEN 0.15
                      ELSE 0.04
                  END
            )
            SELECT (sv.river_network_version_id || '::' || sv.river_segment_id) AS feature_id,
                   sv.river_segment_id AS segment_id,
                   sv.river_segment_id,
                   sv.river_network_version_id,
                   sv.basin_version_id,
                   bv.basin_id,
                   sv.value,
                   sv.unit,
                   sv.quality_flag,
                   sv.run_id,
                   sv.variable,
                   to_char(sv.valid_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS valid_time,
                   CASE
                       WHEN :z >= 9 THEN rs.geom
                       ELSE ST_Transform(
                           ST_SimplifyPreserveTopology(
                               ST_Transform(rs.geom, 3857),
                               CASE
                                   WHEN :z <= 4 THEN 2000.0
                                   WHEN :z = 5 THEN 1000.0
                                   WHEN :z = 6 THEN 500.0
                                   WHEN :z = 7 THEN 200.0
                                   ELSE 80.0
                               END
                           ),
                           4490
                       )
                   END AS geom
            FROM selected_values sv
            JOIN core.river_segment rs
              ON rs.river_segment_id = sv.river_segment_id
             AND rs.river_network_version_id = sv.river_network_version_id
            LEFT JOIN core.basin_version bv
              ON bv.basin_version_id = sv.basin_version_id
        """
    elif layer == "met-stations":
        required_property_checks = {
            "station_id": "station_id IS NULL OR station_id::text = ''",
            "basin_version_id": "basin_version_id IS NULL OR basin_version_id::text = ''",
            "station_role": "station_role IS NULL OR station_role::text = ''",
            "active_flag": "active_flag IS NULL",
        }
        source_cte = """
            SELECT ms.station_id,
                   ms.basin_version_id,
                   COALESCE(ms.station_name, '') AS station_name,
                   ms.station_role,
                   ms.active_flag,
                   ms.geom
            FROM met.met_station ms
            WHERE ms.basin_version_id = :basin_version_id
              AND ms.active_flag = true
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
    tile_row_columns = ",\n                       ".join(_mvt_public_tile_columns(layer))
    return f"""
        WITH bounds AS (
            SELECT ST_TileEnvelope(:z, :x, :y) AS geom_3857
        ),
        source_rows AS NOT MATERIALIZED (
            {source_cte}
        ),
        source_identity_stats AS (
            {source_identity_stats_sql}
        ),
        bounded_rows AS (
            SELECT source_rows.*,
                   ST_NPoints(source_rows.geom) AS source_coordinate_count,
                   ST_NDims(source_rows.geom) AS source_coordinate_dimensions
            FROM source_rows, bounds
            WHERE source_rows.geom IS NOT NULL
              AND source_rows.geom && ST_Transform(bounds.geom_3857, 4490)
        ),
        source_stats AS (
            SELECT CASE WHEN EXISTS (SELECT 1 FROM bounded_rows) THEN 1 ELSE 0 END AS source_feature_count
        ),
        eligible AS (
            SELECT *
            FROM bounded_rows
            WHERE source_coordinate_count <= :feature_coordinate_limit
              AND source_coordinate_dimensions <= :max_coordinate_dimensions
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
            FROM bounded_rows
        ),
        budget_stats AS (
            SELECT COUNT(*) AS feature_count,
                   COALESCE(SUM(source_coordinate_count), 0) AS coordinate_count
            FROM eligible
        ),
        budget_gate AS (
            SELECT budget_stats.feature_count, budget_stats.coordinate_count
            FROM budget_stats, prefilter_stats
            WHERE budget_stats.feature_count <= :feature_limit
              AND budget_stats.coordinate_count <= :collection_coordinate_limit
              AND prefilter_stats.feature_coordinate_overflow_count = 0
              AND prefilter_stats.coordinate_dimension_overflow_count = 0
              AND prefilter_stats.invalid_property_count = 0
        ),
        simplified AS (
            SELECT eligible.*,
                   ST_SimplifyPreserveTopology(
                       ST_MakeValid(ST_Transform(eligible.geom, 3857)),
                       :simplification_tolerance_m
                   ) AS geom_3857
            FROM eligible
            CROSS JOIN budget_gate
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
        ),
        budgeted AS (
            SELECT clipped.*,
                   budget_gate.feature_count,
                   budget_gate.coordinate_count
            FROM clipped
            CROSS JOIN budget_gate
            WHERE mvt_geom IS NOT NULL
        )
        SELECT (
            SELECT ST_AsMVT(tile_rows, '{layer_name}', {MVT_EXTENT}, 'mvt_geom')
            FROM (
                SELECT {tile_row_columns}
                FROM budgeted
                ORDER BY {_mvt_tile_order_by(layer)}
            ) AS tile_rows
        ) AS tile,
        (SELECT source_identity_count FROM source_identity_stats) AS source_identity_count,
        (SELECT source_feature_count FROM source_stats) AS source_feature_count,
        (SELECT feature_count FROM budget_stats) AS feature_count,
        (SELECT coordinate_count FROM budget_stats) AS coordinate_count,
        (SELECT feature_coordinate_overflow_count FROM prefilter_stats) AS feature_coordinate_overflow_count,
        (SELECT feature_coordinate_count FROM prefilter_stats) AS feature_coordinate_count,
        (SELECT coordinate_dimension_overflow_count FROM prefilter_stats) AS coordinate_dimension_overflow_count,
        (SELECT coordinate_dimension_count FROM prefilter_stats) AS coordinate_dimension_count,
        (SELECT invalid_property_count FROM prefilter_stats) AS invalid_property_count,
        (SELECT invalid_properties FROM prefilter_stats) AS invalid_properties
        FROM source_identity_stats, source_stats, budget_stats, prefilter_stats
    """


def _mvt_public_tile_columns(layer: str) -> tuple[str, ...]:
    if layer in {"river-network", "river-network-national"}:
        return (
            "segment_id",
            "river_segment_id",
            "river_network_version_id",
            "basin_version_id",
            '"Type"',
            "mvt_geom",
        )
    if layer == "hydro-national":
        # National overview tile self-describes basin_id (LEFT JOIN core.basin_version),
        # so the click→popup curve resolves the basin without an N+1 versions fetch.
        return (
            "feature_id",
            "segment_id",
            "river_segment_id",
            "river_network_version_id",
            "basin_version_id",
            "basin_id",
            "value",
            "unit",
            "quality_flag",
            "run_id",
            "variable",
            "valid_time",
            "mvt_geom",
        )
    if layer == "hydro":
        return (
            "feature_id",
            "segment_id",
            "river_segment_id",
            "river_network_version_id",
            "basin_version_id",
            "value",
            "unit",
            "quality_flag",
            "run_id",
            "variable",
            "valid_time",
            "mvt_geom",
        )
    if layer == "met-stations":
        return (
            "station_id",
            "basin_version_id",
            "station_name",
            "station_role",
            "active_flag",
            "mvt_geom",
        )
    raise ValueError(f"Unsupported tile layer: {layer}")


def _mvt_tile_order_by(layer: str) -> str:
    if layer == "met-stations":
        return "station_id"
    return "river_network_version_id, river_segment_id"


_NATIONAL_DISCHARGE_METADATA = {
    "tile_url_template": "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf",
    "required_placeholders": ["valid_time", "z", "x", "y"],
    # 全国并集瓦片在密集流域（如黑河）单块塞下整流域河段会超 per-tile 预算（413）。改为按 zoom
    # 干流概化：postgis_tile_sql("hydro-national") 用 q_down(value) 的 per-network PERCENT_RANK
    # 渐进保留高流量干流（z<=4 顶 10%、z5 顶 30%、z6 顶 60%、z7 顶 85%、z8 顶 96%、z>=9 全量），
    # 并按 zoom 加粗 ST_SimplifyPreserveTopology 容差，使每个 zoom 都落入预算（含 z7/z8 密集流域）。
    # min_zoom=3 对齐前端初始全国视图 zoom=3.35，使默认（未放大）也能看到主干河道。
    "min_zoom": 3,
    # 全国瓦片自带 basin_id（LEFT JOIN core.basin_version），点击河段即可直接定位流域取曲线，
    # 不必为每个流域发 versions 请求（N+1）。单 run discharge 瓦片不带，故只在此 override。
    "properties": [
        "feature_id",
        "segment_id",
        "river_segment_id",
        "basin_version_id",
        "basin_id",
        "river_network_version_id",
        "value",
        "unit",
        "quality_flag",
        "run_id",
        "variable",
        "valid_time",
    ],
}

_NATIONAL_RIVER_NETWORK_METADATA = {
    "tile_url_template": "/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf",
    "required_placeholders": ["z", "x", "y"],
    "min_zoom": 0,
}


def layer_metadata(
    layer_id: str,
    *,
    run_id: str | None = None,
    valid_times: list[str] | None = None,
    valid_time_limit: int = MVT_VALID_TIME_SAMPLE_LIMIT,
    valid_time_observed_count: int = 0,
    valid_times_truncated: bool = False,
    source_version: str | None = None,
    basin_version_id: str | None = None,
    river_network_version_id: str | None = None,
    release_blocking: bool = False,
    national: bool = False,
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
                "feature_id",
                "segment_id",
                "river_segment_id",
                "basin_version_id",
                "river_network_version_id",
                "value",
                "unit",
                "quality_flag",
                "run_id",
                "variable",
                "valid_time",
            ],
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
    # National overview sources resolve their identities server-side, so their public
    # routes carry no run_id/basin/network placeholders.
    national_discharge = national and layer_id == "discharge"
    national_river_network = national and layer_id == "river-network"
    if national_discharge:
        base = {**base, **_NATIONAL_DISCHARGE_METADATA}
    elif national_river_network:
        base = {**base, **_NATIONAL_RIVER_NETWORK_METADATA}
    cache_layer_id = layer_id
    source_refs = (
        {}
        if national_discharge or national_river_network
        else _layer_source_refs(
            layer_id,
            run_id=run_id,
            source_version=source_version,
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
        )
    )
    source_ref_constants = {"z", "x", "y", "valid_time"}
    if not (national_discharge or national_river_network) and any(
        placeholder not in source_ref_constants and not source_refs.get(placeholder)
        for placeholder in base["required_placeholders"]
    ):
        return {
            "layer_id": layer_id,
            "tile_format": "geojson_compatibility",
            "fallback_available": False,
            "release_blocking": True,
        }
    route_variable = "q_down" if layer_id == "discharge" else None
    alias_of = None
    alias_semantic = None
    legacy_layer_ids = ["hydro:q_down"] if layer_id == "discharge" else []
    property_schema = {"version": MVT_SCHEMA_VERSION, "required": base["properties"]}
    version = _stable_json_hash(
        {
            "alias_of": alias_of,
            "alias_semantic": alias_semantic,
            "cache_layer_id": cache_layer_id,
            "canonical_route_layer_id": cache_layer_id,
            "encoder_version": MVT_ENCODER_VERSION,
            "layer_id": layer_id,
            "legacy_layer_ids": legacy_layer_ids,
            "maplibre_source_layer": base["maplibre_source_layer"],
            "property_schema": property_schema,
            "release_blocking": release_blocking,
            "required_placeholders": base["required_placeholders"],
            "route_variable": route_variable,
            "schema_version": MVT_SCHEMA_VERSION,
            "source_refs": source_refs,
            "source_generation": source_version if national else None,
            "tile_url_template": base["tile_url_template"],
            "valid_time_limit": valid_time_limit,
            "valid_time_observed_count": valid_time_observed_count,
            "valid_times": valid_times or [],
            "valid_times_truncated": valid_times_truncated,
        }
    )
    return {
        "layer_id": layer_id,
        "tile_format": "mvt",
        "url_template": base["tile_url_template"],
        "tile_url_template": base["tile_url_template"],
        "required_placeholders": base["required_placeholders"],
        "maplibre_source_layer": base["maplibre_source_layer"],
        "source_layer": base["maplibre_source_layer"],
        "property_schema_version": MVT_SCHEMA_VERSION,
        "property_schema": property_schema,
        "min_zoom": base.get("min_zoom", 0),
        "max_zoom": MVT_MAX_ZOOM,
        "bounds_crs": "EPSG:3857",
        "bounds": WEB_MERCATOR_BOUNDS,
        "wgs84_bounds": CHINA_WGS84_BOUNDS,
        "valid_times": valid_times or [],
        "valid_time_limit": valid_time_limit,
        "valid_time_observed_count": valid_time_observed_count,
        "valid_times_truncated": valid_times_truncated,
        "source_refs": source_refs,
        "source_generation": source_version if national else None,
        "cache_layer_id": cache_layer_id,
        "route_variable": route_variable,
        "alias_of": alias_of,
        "alias_semantic": alias_semantic,
        "canonical_route_layer_id": cache_layer_id,
        "legacy_layer_ids": legacy_layer_ids,
        "cache_etag": f'W/"metadata-{version}"',
        "cache_version": version,
        "schema_version": MVT_SCHEMA_VERSION,
        "encoder_version": MVT_ENCODER_VERSION,
        "fallback_available": False,
        "fallback_endpoint": None,
        "release_blocking": release_blocking,
        "production_mvt_readiness_claimed": False,
    }


def _layer_source_refs(
    layer_id: str,
    *,
    run_id: str | None,
    source_version: str | None,
    basin_version_id: str | None,
    river_network_version_id: str | None,
) -> dict[str, str | None]:
    assert layer_id != "discharge", (
        "discharge layer must use national source_refs={} via layer_metadata; "
        "_layer_source_refs is unreachable for discharge per PR #602 spec invariant "
        "(mvt-tile-contract: Discharge canonical URL is national across all callers)"
    )
    refs = {
        key: value
        for key, value in {
            "run_id": run_id if layer_id != "river-network" else None,
            "source_version": source_version,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        }.items()
        if value is not None
    }
    return refs


def display_ready_run(session: Session) -> Mapping[str, Any] | None:
    """Latest display-ready hydro run for layer catalog discovery."""
    row = session.execute(
        text(
            """
            SELECT h.run_id, h.status, h.model_id, h.basin_version_id, h.source_id, h.cycle_time, h.updated_at,
                   mi.river_network_version_id
            FROM hydro.hydro_run h
            LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
            WHERE h.status IN ('succeeded', 'parsed', 'published')
            ORDER BY h.cycle_time DESC, h.run_id DESC
            LIMIT 1
            """
        )
    ).mappings().first()
    return dict(row) if row is not None else None


def national_discharge_source_version(session: Session) -> str:
    """Digest the exact latest display-ready run selected for every network."""
    rows = (
        session.execute(
            text(
                """
                SELECT run_id, river_network_version_id, cycle_time, updated_at
                FROM (
                    SELECT h.run_id,
                           mi.river_network_version_id,
                           h.cycle_time,
                           h.updated_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY mi.river_network_version_id
                               ORDER BY h.cycle_time DESC, h.run_id DESC
                           ) AS rn
                    FROM hydro.hydro_run h
                    JOIN core.model_instance mi ON mi.model_id = h.model_id
                    WHERE h.status IN ('succeeded', 'parsed', 'published')
                      AND mi.river_network_version_id IS NOT NULL
                ) ranked
                WHERE rn = 1
                ORDER BY river_network_version_id, run_id
                """
            )
        )
        .mappings()
        .all()
    )
    return _national_source_digest("hydro-national", rows)


def national_river_network_source_version(session: Session) -> str:
    """Digest active river-network identities and their immutable inventory metadata."""
    active_predicate = "mi.active_flag = 1" if session.get_bind().dialect.name == "sqlite" else "mi.active_flag = true"
    rows = (
        session.execute(
            text(
                f"""
                SELECT DISTINCT rnv.river_network_version_id,
                       rnv.basin_version_id,
                       rnv.segment_count,
                       rnv.checksum,
                       rnv.created_at
                FROM core.river_network_version rnv
                JOIN core.model_instance mi
                  ON mi.river_network_version_id = rnv.river_network_version_id
                WHERE {active_predicate}
                ORDER BY rnv.river_network_version_id
                """
            )
        )
        .mappings()
        .all()
    )
    return _national_source_digest("river-network-national", rows)


def _national_source_digest(prefix: str, rows: Iterable[Mapping[str, Any]]) -> str:
    basis = [dict(row) for row in rows]
    digest = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:20]
    return f"{prefix}:{digest}:{len(basis)}"


def valid_times_for_layer(
    session: Session,
    layer_id: str,
    *,
    run_id: str | None = None,
    basin_version_id: str | None = None,
    river_network_version_id: str | None = None,
    limit: int = MVT_VALID_TIME_SAMPLE_LIMIT,
) -> ValidTimeDiscovery:
    sample_limit = max(0, limit)
    query_limit = sample_limit + 1
    selected_identity_params = {
        "run_id": run_id,
        "basin_version_id": basin_version_id,
        "river_network_version_id": river_network_version_id,
        "limit": query_limit,
    }
    if layer_id == "discharge":
        if run_id is not None and (basin_version_id is None or river_network_version_id is None):
            raise ValueError("Concrete hydro valid-time discovery requires selected basin and river-network identity.")
        variable = "q_down"
        sql = (
            """
                SELECT DISTINCT valid_time
                FROM hydro.river_timeseries
                WHERE run_id = :run_id
                  AND basin_version_id = :basin_version_id
                  AND river_network_version_id = :river_network_version_id
                  AND variable = :variable
                ORDER BY valid_time DESC
                LIMIT :limit
            """
            if run_id is not None
            else """
                SELECT DISTINCT valid_time
                FROM hydro.river_timeseries
                WHERE variable = :variable
                ORDER BY valid_time DESC
                LIMIT :limit
            """
        )
        rows = (
            session.execute(text(sql), {**selected_identity_params, "variable": variable})
            .mappings()
            .all()
        )
        return _valid_time_discovery(rows, sample_limit)
    else:
        return ValidTimeDiscovery(valid_times=[], limit=sample_limit, observed_count=0, truncated=False)


def national_discharge_valid_times(
    session: Session,
    *,
    variable: str = "q_down",
    limit: int = MVT_VALID_TIME_SAMPLE_LIMIT,
) -> ValidTimeDiscovery:
    """Union of distinct discharge valid-times across every basin's latest display-ready run.

    Mirrors the national tile SQL identity selection (each river network's latest
    display-ready run) but only enumerates DISTINCT valid_time. Written
    with a ROW_NUMBER() window instead of Postgres-only DISTINCT ON so the catalog /
    valid-times contract stays testable on sqlite while remaining equivalent on
    Postgres. No data is fabricated: empty when no ready run/series exists.
    """
    sample_limit = max(0, limit)
    rows = (
        session.execute(
            text(
                """
                WITH latest_run AS (
                    SELECT run_id, river_network_version_id
                    FROM (
                        SELECT h.run_id,
                               mi.river_network_version_id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY mi.river_network_version_id
                                   ORDER BY h.cycle_time DESC, h.run_id DESC
                               ) AS rn
                        FROM hydro.hydro_run h
                        JOIN core.model_instance mi ON mi.model_id = h.model_id
                        WHERE h.status IN ('succeeded', 'parsed', 'published')
                          AND mi.river_network_version_id IS NOT NULL
                    ) ranked
                    WHERE rn = 1
                )
                SELECT DISTINCT ts.valid_time
                FROM hydro.river_timeseries ts
                JOIN latest_run lr
                  ON lr.run_id = ts.run_id
                 AND lr.river_network_version_id = ts.river_network_version_id
                WHERE ts.variable = :variable
                ORDER BY ts.valid_time DESC
                LIMIT :limit
                """
            ),
            {"variable": variable, "limit": sample_limit + 1},
        )
        .mappings()
        .all()
    )
    return _valid_time_discovery(rows, sample_limit)


def _valid_time_discovery(rows: Iterable[Mapping[str, Any]], limit: int) -> ValidTimeDiscovery:
    formatted = [_format_time(row["valid_time"]) for row in rows]
    truncated = len(formatted) > limit
    valid_times = sorted(formatted[:limit])
    return ValidTimeDiscovery(
        valid_times=valid_times,
        limit=limit,
        observed_count=len(formatted),
        truncated=truncated,
    )


def _source_layer_id(layer: str) -> str:
    # "hydro-national" reuses the "hydro" maplibre source layer so the frontend
    # source-layer name stays identical between single-run and national tiles.
    return {
        "river-network": "river_network",
        "river-network-national": "river_network",
        "hydro": "hydro",
        "hydro-national": "hydro",
        "met-stations": "met_stations",
    }[layer]


def _enforce_feature_budget(features: list[Mapping[str, Any]]) -> None:
    if len(features) > MVT_MAX_FEATURES:
        raise TileError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="MVT tile feature budget exceeded.",
            details={"feature_count": len(features), "max_features": MVT_MAX_FEATURES},
    )
    coordinate_count = len(features)
    if coordinate_count > MVT_MAX_COORDINATES:
        raise TileError(
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
    if "valid_time" in columns and canonical_mvt_time(row.get("valid_time")) != canonical_mvt_time(tile.valid_time):
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


def _file_cache_path(key: str) -> Path | None:
    root = os.getenv(MVT_FILE_CACHE_DIR_ENV, "").strip()
    if not root:
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        return None
    return Path(root).expanduser() / key[:2] / f"{key}.pbf"


def _file_cache_lock_path(key: str) -> Path | None:
    root = os.getenv(MVT_FILE_CACHE_DIR_ENV, "").strip()
    if not root or not re.fullmatch(r"[0-9a-f]{64}", key):
        return None
    return Path(root).expanduser() / ".locks" / key[:2] / f"{key}.lock"


def _read_file_cache(key: str) -> tuple[bytes, str, str] | None:
    path = _file_cache_path(key)
    if path is None or not path.is_file():
        return None
    data = path.read_bytes()
    if len(data) > MVT_MAX_BYTES:
        return None
    checksum = hashlib.sha256(data).hexdigest()
    return data, checksum, stable_etag(data)


def _safe_read_file_cache(key: str) -> tuple[bytes, str, str] | None:
    try:
        return _read_file_cache(key)
    except OSError:
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
        "valid_time": canonical_mvt_time(tile.valid_time),
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
        "source_run_id": None if tile.layer_id in {"river-network", "met-stations"} else tile.source_id,
        "source_product_id": tile.source_id,
        "source_version": tile.source_version,
        "variable": metadata.get("variable"),
        "valid_time": canonical_mvt_time(tile.valid_time),
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
    if tile.layer_id == "river-network":
        return {
            "layer_type": "river_network",
            "tile_uri_template": "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
            "maplibre_source_layer": "river_network",
            "variable": None,
            "fallback_available": False,
        }
    if tile.layer_id == "met-stations":
        return {
            "layer_type": "meteorological_station",
            "tile_uri_template": "/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf",
            "maplibre_source_layer": "met_stations",
            "variable": None,
            "fallback_available": False,
        }
    if tile.layer_id == "discharge":
        variable = "q_down"
        return {
            "layer_type": "hydrological_output",
            "tile_uri_template": f"/api/v1/tiles/hydro/{{run_id}}/{variable}/{{valid_time}}/{{z}}/{{x}}/{{y}}.pbf",
            "maplibre_source_layer": "hydro",
            "variable": variable,
            "fallback_available": False,
        }
    if tile.layer_id.startswith("hydro:"):
        variable = tile.layer_id.split(":", 1)[1]
        # Defense-in-depth: route-level handlers already validate `variable`
        # against `SUPPORTED_HYDRO_MVT_VARIABLES`, but non-route callers
        # (CLI tools, workers, future debug constructors) could synthesize a
        # `TileInput(layer_id="hydro:<retired>")` and silently materialize a
        # legacy-shaped URI template / cache row. Reject any hydro variant
        # outside the canonical allow-list before any URI/cache work.
        if variable not in SUPPORTED_HYDRO_MVT_VARIABLES:
            raise ValueError(
                f"Unsupported hydro layer_id={tile.layer_id!r}; "
                f"supported variables: {SUPPORTED_HYDRO_MVT_VARIABLES}"
            )
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
            if tile.layer_id in {"river-network", "met-stations"}
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


def _write_file_cache(key: str, data: bytes) -> bool:
    path = _file_cache_path(key)
    if path is None:
        return False
    if len(data) > MVT_MAX_BYTES:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return True


def _safe_write_file_cache(key: str, data: bytes) -> bool:
    try:
        return _write_file_cache(key, data)
    except OSError:
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
    if value is None:
        raise TileError(
            status_code=500,
            code="MVT_PROPERTY_INVALID",
            message="MVT required feature property is missing.",
            details={"field": field_name},
        )
    if isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TileError(
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


def canonical_mvt_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    text_value = str(value)
    if " " in text_value and "T" not in text_value:
        text_value = text_value.replace(" ", "T", 1)
    parsed = _parse_iso_datetime(text_value)
    if parsed is not None:
        dt = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return text_value


def _format_time(value: Any) -> str:
    formatted = canonical_mvt_time(value)
    return "None" if formatted is None else formatted


def _parse_iso_datetime(value: str) -> datetime | None:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None
