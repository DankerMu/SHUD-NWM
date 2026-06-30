from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Generator, Mapping
from datetime import datetime
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.display_cache import display_catalog_cached
from apps.api.errors import ApiError
from apps.api.routes.pipeline import _ok
from services.tiles.mvt import (
    DEFAULT_FLOOD_RETURN_PERIOD_DURATION,
    MVT_BUFFER,
    MVT_ENCODER_VERSION,
    MVT_EXTENT,
    MVT_MAX_TILE_COORDINATE,
    MVT_MAX_ZOOM,
    MVT_MEDIA_TYPE,
    MVT_SCHEMA_VERSION,
    MVT_VALID_TIME_SAMPLE_LIMIT,
    SUPPORTED_FLOOD_RETURN_PERIOD_DURATIONS,
    SUPPORTED_HYDRO_MVT_VARIABLES,
    TileError,
    TileInput,
    TileResponse,
    ValidTimeDiscovery,
    canonical_mvt_time,
    latest_frequency_ready_run,
    latest_ready_run,
    layer_metadata,
    national_discharge_valid_times,
    postgis_tile_sql,
    public_hydro_layer_id,
    valid_times_for_layer,
)
from services.tiles.mvt import (
    build_raw_tile_response as _build_raw_tile_response,
)
from services.tiles.mvt import (
    build_tile_response as _build_tile_response,
)
from services.tiles.mvt import (
    read_cached_tile_response as _read_cached_tile_response,
)
from services.tiles.mvt import (
    simplification_tolerance_m as _simplification_tolerance_m,
)
from services.tiles.mvt import (
    validate_identifier as _validate_tile_identifier,
)
from services.tiles.mvt import (
    validate_xyz as _validate_tile_xyz,
)

router = APIRouter(tags=["flood-alerts"])

# Stable national-overview tile identity. The discharge layer aggregates every basin's
# latest display-ready run, so the tile/cache identity is a fixed national id rather
# than a single run_id.
HYDRO_NATIONAL_SOURCE_ID = "hydro-national"
HYDRO_NATIONAL_SOURCE_VERSION = "hydro-national-latest-per-basin"

WARNING_COLORS = {
    "normal": "#808080",
    "elevated": "#4A90D9",
    "watch": "#FFD700",
    "warning": "#FF8C00",
    "high_risk": "#FF4500",
    "severe": "#DC143C",
    "extreme": "#800080",
}
WARNING_LEVELS = tuple(WARNING_COLORS)
USABLE_CURVE_FLAGS = {"ok", "partial_sample", "monotonicity_corrected"}
DISPLAY_PRODUCT_READY_STATUSES = {"succeeded", "parsed", "frequency_done", "published"}
FLOOD_PRODUCT_READY_STATUSES = {"frequency_done", "published"}
# Single source of truth for the public layer catalog advertised by
# `/api/v1/layers` and accepted by `/api/v1/layers/{layer_id}/valid-times`.
# `_default_layer_catalog` walks these definitions to build `Layer` rows;
# `SUPPORTED_PUBLIC_LAYER_IDS` is derived from the same literal so the two
# cannot drift when a 5th layer is added. Tuple shape:
#   (layer_id, display_name, layer_type, variables)
_PUBLIC_LAYER_DEFINITIONS: tuple[tuple[str, str, str, list[str]], ...] = (
    ("discharge", "Discharge", "hydrology", ["q_down"]),
    ("flood-return-period", "Flood return period", "hydrology", ["return_period"]),
    ("warning-level", "Warning level", "hydrology", ["warning_level"]),
    ("river-network", "River network", "base", ["geometry"]),
)
# Public layer_ids advertised by `_default_layer_catalog`. Used by the
# `/api/v1/layers/{layer_id}/valid-times` route to reject layer_ids that
# are not part of the canonical hydrology/base catalog with a 422 before
# any DB work, so retired/unknown ids fail loudly at the route boundary.
SUPPORTED_PUBLIC_LAYER_IDS = frozenset(definition[0] for definition in _PUBLIC_LAYER_DEFINITIONS)
FLOOD_PRODUCT_QUALITY_EXPLICIT_COLUMNS = frozenset(
    {
        "quality_state",
        "quality_source",
        "unavailable_products",
        "residual_blockers",
        "expected_result_rows",
        "expected_max_result_rows",
        "expected_timestep_result_rows",
        "meaningful_result_rows",
        "meaningful_max_result_rows",
        "meaningful_timestep_result_rows",
        "no_frequency_curve_rows",
        "no_usable_frequency_curve_rows",
        "warning_threshold_unavailable_rows",
    }
)
NO_USABLE_CURVE_NOTE = "No usable frequency curves available"
NO_SEGMENT_CURVE_NOTE = "No frequency curve available for this segment"
SEGMENT_LIST_DEFAULT_LIMIT = 100
SEGMENT_LIST_MAX_LIMIT = 500
FLOOD_ALERT_TIMELINE_DEFAULT_MAX_POINTS = 168
FLOOD_ALERT_TIMELINE_MAX_POINTS = 1_000
FLOOD_RETURN_PERIOD_MAP_DEFAULT_LIMIT = 10_000
FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT = 10_000
FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES = 10_000
FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES = 50_000
FLOOD_RETURN_PERIOD_MAP_MAX_COORDINATE_DIMENSIONS = 3
FLOOD_RETURN_PERIOD_MAP_MAX_SERIALIZED_BYTES = 2_000_000
MVT_RESPONSE_HEADERS = {
    "Cache-Control": {"schema": {"type": "string"}},
    "ETag": {"schema": {"type": "string"}},
    "X-Tile-Layer-ID": {"schema": {"type": "string"}},
    "X-Tile-Checksum": {"schema": {"type": "string"}},
    "X-Tile-Cache": {"schema": {"type": "string", "enum": ["hit", "miss", "bypass"]}},
    "X-Tile-Cache-Key": {"schema": {"type": "string"}},
    "X-MVT-Schema-Version": {"schema": {"type": "string"}},
}
MVT_ROUTE_RESPONSES = {
    200: {
        "description": "Raw Mapbox vector tile",
        "headers": MVT_RESPONSE_HEADERS,
        "content": {MVT_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
    },
    424: {
        "description": "Live PostGIS MVT is unavailable for this canonical tile route.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["request_id", "status", "error"],
                    "properties": {
                        "request_id": {"type": "string"},
                        "status": {"type": "string", "enum": ["error"]},
                        "error": {
                            "type": "object",
                            "required": ["code", "message"],
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "enum": ["MVT_LIVE_POSTGIS_UNAVAILABLE"],
                                },
                                "message": {"type": "string"},
                                "details": {
                                    "type": "object",
                                    "nullable": True,
                                    "additionalProperties": True,
                                },
                            },
                        },
                    },
                }
            }
        },
    },
    "4XX": {
        "description": "Canonical MVT request validation or source-identity error.",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
            }
        },
    },
    "5XX": {
        "description": "Canonical MVT server error.",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
            }
        },
    },
}
TILE_X_DESCRIPTION = (
    f"Web Mercator XYZ tile column. Global schema bounds are 0..{MVT_MAX_TILE_COORDINATE} "
    f"for max zoom {MVT_MAX_ZOOM}; each request also enforces 0 <= x < 2^z."
)
TILE_Y_DESCRIPTION = (
    f"Web Mercator XYZ tile row. Global schema bounds are 0..{MVT_MAX_TILE_COORDINATE} "
    f"for max zoom {MVT_MAX_ZOOM}; each request also enforces 0 <= y < 2^z."
)


def _tile_api_error(exc: TileError) -> ApiError:
    return ApiError(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


def validate_identifier(value: str, field_name: str) -> None:
    try:
        _validate_tile_identifier(value, field_name)
    except TileError as exc:
        raise _tile_api_error(exc) from exc


def validate_xyz(z: int, x: int, y: int, *, max_zoom: int = MVT_MAX_ZOOM) -> None:
    try:
        _validate_tile_xyz(z, x, y, max_zoom=max_zoom)
    except TileError as exc:
        raise _tile_api_error(exc) from exc


def build_tile_response(
    session: Session,
    tile: TileInput,
    layer_name: str,
    features: list[Mapping[str, Any]],
) -> TileResponse:
    try:
        return _build_tile_response(session, tile, layer_name, features)
    except TileError as exc:
        raise _tile_api_error(exc) from exc


def build_raw_tile_response(session: Session, tile: TileInput, data: bytes) -> TileResponse:
    try:
        return _build_raw_tile_response(session, tile, data)
    except TileError as exc:
        raise _tile_api_error(exc) from exc


def read_cached_tile_response(session: Session, tile: TileInput) -> TileResponse | None:
    try:
        return _read_cached_tile_response(session, tile)
    except TileError as exc:
        raise _tile_api_error(exc) from exc


def simplification_tolerance_m(z: int) -> float:
    try:
        return _simplification_tolerance_m(z)
    except TileError as exc:
        raise _tile_api_error(exc) from exc


class AlertLevelCount(BaseModel):
    level: str
    count: int
    color: str


class AlertSummaryResponse(BaseModel):
    run_id: str
    levels: list[AlertLevelCount]
    total_segments: int
    usable_curves: int
    unavailable_count: int = 0
    quality_note: str | None = None


class RankingItem(BaseModel):
    rank: int
    river_segment_id: str
    segment_id: str
    segment_name: str | None = None
    basin_version_id: str
    river_network_version_id: str | None = None
    q_value: float
    q_unit: str
    return_period: float | None = None
    warning_level: str | None = None
    duration: str
    valid_time: str
    geom_centroid: GeoPoint | None = None


class RankingResponse(BaseModel):
    items: list[RankingItem]
    total: int
    limit: int
    offset: int


class GeoPoint(BaseModel):
    type: str = "Point"
    coordinates: list[float]


class SegmentAlert(BaseModel):
    river_segment_id: str
    segment_id: str
    segment_name: str | None = None
    basin_version_id: str
    river_network_version_id: str | None = None
    q_value: float
    return_period: float | None = None
    warning_level: str | None = None
    valid_time: str
    geom_centroid: GeoPoint | None = None


class SegmentListResponse(BaseModel):
    segments: list[SegmentAlert]
    total: int
    limit: int
    offset: int


class FrequencyThresholds(BaseModel):
    Q2: float | None = None
    Q5: float | None = None
    Q10: float | None = None
    Q20: float | None = None
    Q50: float | None = None
    Q100: float | None = None
    sample_quality: dict[str, Any] | None = None


class TimelinePoint(BaseModel):
    valid_time: str
    return_period: float | None = None
    warning_level: str | None = None
    q_value: float


class TimelineResponse(BaseModel):
    run_id: str
    segment_id: str
    river_segment_id: str
    river_network_version_id: str
    timesteps: list[TimelinePoint]
    timeline: list[TimelinePoint]
    peak: TimelinePoint | None = None
    frequency_thresholds: FrequencyThresholds | None = None
    quality_note: str | None = None


class TileFeature(BaseModel):
    type: str = "Feature"
    properties: dict[str, Any]
    geometry: dict[str, Any] | None = None


class TileFeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    features: list[TileFeature] = Field(default_factory=list)
    product_quality: dict[str, Any] | None = None


class Layer(BaseModel):
    layer_id: str
    layer_name: str
    layer_type: str
    variables: list[str]
    metadata: dict[str, Any] | None = None


class ApiSuccessEnvelope(BaseModel):
    request_id: str
    status: str


class LayerListResponse(ApiSuccessEnvelope):
    data: list[Layer]


class LayerValidTimes(BaseModel):
    valid_times: list[str]
    items: list[str]
    limit: int
    observed_count: int
    truncated: bool


class LayerValidTimesResponse(ApiSuccessEnvelope):
    data: LayerValidTimes


@lru_cache
def _engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def get_flood_alert_session() -> Generator[Session, None, None]:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ApiError(
            status_code=500,
            code="DATABASE_URL_MISSING",
            message="DATABASE_URL is required for flood alert API operations.",
        )
    with Session(_engine(database_url)) as session:
        yield session


@router.get("/api/v1/layers", response_model=LayerListResponse)
def list_layers(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    run_id: str | None = Query(
        default=None,
        description=(
            "Optional concrete hydro_run.run_id/source reference used to scope layer metadata and cache identity."
        ),
    ),
    session: Session = Depends(get_flood_alert_session),
) -> dict[str, Any]:
    if run_id is not None:
        validate_identifier(run_id, "run_id")

    def _load() -> list[dict[str, Any]]:
        if run_id is not None:
            run = _require_display_ready(session, run_id)
        else:
            # 目录默认 run 选最新 display-ready（不要求洪频完整）：discharge/river-network
            # 仅需可展示水文 run；洪频/预警可用性由 _annotate_flood_layer_quality 独立标注。
            # 无洪频基线的流域（QHH/Heihe）因此仍能暴露 discharge，而非整目录空。
            run = latest_frequency_ready_run(session)
            if run is None:
                return []
        resolved_run_id = str(run["run_id"]) if run else None
        basin_version_id, river_network_version_id = _require_run_source_identity(run, layer_id="layers")
        source_version = _run_source_version(run) if run else None
        river_network_source_version = (
            _river_network_source_version(session, basin_version_id) if basin_version_id is not None else source_version
        )
        flood_product_quality = (
            _flood_product_quality(session, resolved_run_id, status=_optional_str(run.get("status")))
            if resolved_run_id is not None
            else None
        )
        layers = _default_layer_catalog(
            session,
            run_id=resolved_run_id,
            source_version=source_version,
            river_network_source_version=river_network_source_version,
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
            national=run_id is None,
        )
        if flood_product_quality is not None:
            _annotate_flood_layer_quality(layers, flood_product_quality)
        return [layer.model_dump() for layer in layers[offset : offset + limit]]

    # display 角色 TTL 缓存：目录解析最新 run + 洪频质量聚合在只读副本上 ~14s。
    return _ok(request, display_catalog_cached(request, f"layers:{run_id}:{limit}:{offset}", _load))


@router.get("/api/v1/layers/{layer_id}/valid-times", response_model=LayerValidTimesResponse)
def list_layer_valid_times(
    request: Request,
    layer_id: str,
    run_id: str | None = Query(
        default=None,
        description="Optional concrete hydro_run.run_id/source reference used to scope valid-time discovery.",
    ),
    duration: str | None = Query(
        default=None,
        description=(
            "Optional flood return-period duration for flood-return-period and warning-level discovery; "
            "defaults to the current UI route identity of 1h."
        ),
    ),
    session: Session = Depends(get_flood_alert_session),
) -> dict[str, Any]:
    validate_identifier(layer_id, "layer_id")
    if layer_id not in SUPPORTED_PUBLIC_LAYER_IDS:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Unsupported layer_id for valid-time discovery.",
            details={"layer_id": layer_id, "supported": sorted(SUPPORTED_PUBLIC_LAYER_IDS)},
        )
    requested_run_id = run_id

    def _load() -> dict[str, Any]:
        run_id = requested_run_id
        if run_id is None and layer_id == "discharge":
            # National discharge: union of valid-times across every basin's latest
            # display-ready run, matching the run-less catalog/tile template.
            if duration is not None:
                raise ApiError(
                    status_code=422,
                    code="VALIDATION_ERROR",
                    message="Duration is only supported for flood return-period valid-time discovery.",
                    details={"layer_id": layer_id, "duration": duration},
                )
            return national_discharge_valid_times(session).model_dump()
        if run_id is not None:
            validate_identifier(run_id, "run_id")
            run = (
                _require_frequency_ready(session, run_id)
                if layer_id in {"flood-return-period", "warning-level"}
                else _require_display_ready(session, run_id)
            )
        else:
            # 洪频/预警 valid-times 维持 latest_ready_run（要求洪频完整，无则空、不抛错）；
            # discharge/river-network 用 display-ready 选择器，使无洪频流域也能发现有效时间。
            if layer_id in {"flood-return-period", "warning-level"}:
                run = latest_ready_run(session)
            else:
                run = latest_frequency_ready_run(session)
            if run is None:
                return _empty_valid_times().model_dump()
            run_id = str(run["run_id"]) if run else None
        basin_version_id, river_network_version_id = _require_run_source_identity(run, layer_id=layer_id)
        if duration is not None:
            validate_identifier(duration, "duration")
        if layer_id in {"flood-return-period", "warning-level"}:
            resolved_duration = duration or DEFAULT_FLOOD_RETURN_PERIOD_DURATION
            _validate_supported_flood_duration(resolved_duration)
        elif duration is not None:
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="Duration is only supported for flood return-period valid-time discovery.",
                details={"layer_id": layer_id, "duration": duration},
            )
        else:
            resolved_duration = DEFAULT_FLOOD_RETURN_PERIOD_DURATION
        valid_time_sample = valid_times_for_layer(
            session,
            layer_id,
            run_id=run_id,
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
            duration=resolved_duration,
        )
        return valid_time_sample.model_dump()

    # display 角色 TTL 缓存：valid-time 发现按 run/全国扫描洪频结果（只读副本上数秒级）。
    return _ok(
        request,
        display_catalog_cached(request, f"valid-times:{layer_id}:{requested_run_id}:{duration}", _load),
    )


@router.get("/api/v1/flood-alerts/summary", response_model=dict[str, Any])
def flood_alert_summary(
    request: Request,
    run_id: str = Query(...),
    threshold: str | float | None = Query(default=None),
    valid_time: datetime | None = Query(default=None),
    session: Session = Depends(get_flood_alert_session),
) -> dict[str, Any]:
    run = _require_frequency_ready(session, run_id)
    _require_flood_product_ready(session, run_id, status=_optional_str(run.get("status")))
    min_return_period = _parse_threshold(threshold)
    rows = session.execute(
        text(
            f"""
            SELECT warning_level, COUNT(*) AS count
            FROM flood.return_period_result
            WHERE run_id = :run_id
              AND {_time_filter_sql(valid_time)}
              AND (:min_return_period IS NULL OR return_period >= :min_return_period)
              AND quality_flag IN :usable_flags
              AND warning_level IS NOT NULL
            GROUP BY warning_level
            """
        ).bindparams(bindparam("usable_flags", expanding=True)),
        {
            "run_id": run_id,
            "valid_time": valid_time,
            "max_over_window": True,
            "min_return_period": min_return_period,
            "usable_flags": tuple(USABLE_CURVE_FLAGS),
        },
    ).mappings()
    counts = {str(row["warning_level"]): int(row["count"]) for row in rows}
    total_segments = _result_segment_count(session, run_id, run, valid_time=valid_time)
    usable_curves = _usable_curve_count(session, run_id, valid_time=valid_time)
    unavailable_count = max(total_segments - usable_curves, 0)
    data = AlertSummaryResponse(
        run_id=run_id,
        levels=[
            AlertLevelCount(level=level, count=counts.get(level, 0), color=WARNING_COLORS[level])
            for level in WARNING_LEVELS
        ],
        total_segments=total_segments,
        usable_curves=usable_curves,
        unavailable_count=unavailable_count,
        quality_note=NO_USABLE_CURVE_NOTE if total_segments > 0 and usable_curves == 0 else None,
    )
    return _ok(request, data.model_dump())


@router.get("/api/v1/flood-alerts/ranking", response_model=dict[str, Any])
def flood_alert_ranking(
    request: Request,
    run_id: str = Query(...),
    limit: int = Query(default=10, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    basin_id: str | None = Query(default=None),
    valid_time: datetime | None = Query(default=None),
    session: Session = Depends(get_flood_alert_session),
) -> dict[str, Any]:
    run = _require_frequency_ready(session, run_id)
    _require_flood_product_ready(session, run_id, status=_optional_str(run.get("status")))
    geom_sql, centroid_sql = _geometry_select_sql(session)
    where_sql, params = _ranking_filters(run_id=run_id, basin_id=basin_id, valid_time=valid_time)
    count_statement = text(f"SELECT COUNT(*) AS count FROM flood.return_period_result r {where_sql}").bindparams(
        bindparam("usable_flags", expanding=True)
    )
    total = int(
        session.execute(
            count_statement,
            params,
        )
        .mappings()
        .one()["count"]
    )
    rows = session.execute(
        text(
            f"""
            SELECT r.river_segment_id, r.basin_version_id, r.q_value, r.q_unit, r.return_period,
                   r.warning_level, r.duration, r.valid_time, r.river_network_version_id, rs.properties_json,
                   {centroid_sql} AS geom_centroid,
                   {geom_sql} AS geom_json
            FROM flood.return_period_result r
            LEFT JOIN core.river_segment rs
              ON rs.river_segment_id = r.river_segment_id
             AND rs.river_network_version_id = r.river_network_version_id
            {where_sql}
            ORDER BY r.return_period DESC NULLS LAST, r.q_value DESC, r.river_network_version_id, r.river_segment_id,
                     r.valid_time
            LIMIT :limit OFFSET :offset
            """
        ).bindparams(bindparam("usable_flags", expanding=True)),
        {**params, "limit": limit, "offset": offset},
    ).mappings()
    items = [
        RankingItem(
            rank=offset + index,
            river_segment_id=str(row["river_segment_id"]),
            segment_id=str(row["river_segment_id"]),
            segment_name=_segment_name(row.get("properties_json")),
            basin_version_id=str(row["basin_version_id"]),
            river_network_version_id=_optional_str(row["river_network_version_id"]),
            q_value=_finite_result_float(row["q_value"], field="q_value"),
            q_unit=str(row["q_unit"] or "m3/s"),
            return_period=_optional_float(row["return_period"]),
            warning_level=_optional_str(row["warning_level"]),
            duration=str(row["duration"]),
            valid_time=_format_time(row["valid_time"]),
            geom_centroid=_centroid_payload(row.get("geom_centroid") or row.get("geom_json")),
        )
        for index, row in enumerate(rows, start=1)
    ]
    return _ok(request, RankingResponse(items=items, total=total, limit=limit, offset=offset).model_dump())


@router.get("/api/v1/flood-alerts/segments", response_model=dict[str, Any])
def flood_alert_segments(
    request: Request,
    run_id: str = Query(...),
    min_return_period: float | None = Query(default=None, ge=0),
    warning_level: str | None = Query(default=None),
    valid_time: datetime | None = Query(default=None),
    limit: int = Query(default=SEGMENT_LIST_DEFAULT_LIMIT, ge=1, le=SEGMENT_LIST_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_flood_alert_session),
) -> dict[str, Any]:
    run = _require_frequency_ready(session, run_id)
    _require_flood_product_ready(session, run_id, status=_optional_str(run.get("status")))
    if min_return_period is not None:
        min_return_period = _finite_query_float(
            min_return_period,
            field="min_return_period",
            original=min_return_period,
        )
    geom_sql, centroid_sql = _geometry_select_sql(session)
    levels = _split_csv(warning_level)
    params: dict[str, Any] = {
        "run_id": run_id,
        "valid_time": valid_time,
        "min_return_period": min_return_period,
        "levels": tuple(levels),
    }
    level_filter = "AND r.warning_level IN :levels" if levels else ""
    where_sql = f"""
            WHERE r.run_id = :run_id
              AND {_time_filter_sql(valid_time, alias="r")}
              AND (:min_return_period IS NULL OR r.return_period >= :min_return_period)
              {level_filter}
              AND r.quality_flag IN :usable_flags
            """
    count_statement = text(f"SELECT COUNT(*) AS count FROM flood.return_period_result r {where_sql}").bindparams(
        bindparam("usable_flags", expanding=True)
    )
    if levels:
        count_statement = count_statement.bindparams(bindparam("levels", expanding=True))
    total = int(
        session.execute(
            count_statement,
            {**params, "max_over_window": True, "usable_flags": tuple(USABLE_CURVE_FLAGS)},
        )
        .mappings()
        .one()["count"]
    )
    statement = text(
        f"""
            SELECT r.river_segment_id, r.basin_version_id, r.q_value, r.return_period,
                   r.warning_level, r.valid_time, r.river_network_version_id, rs.properties_json,
                   {centroid_sql} AS geom_centroid,
                   {geom_sql} AS geom_json
            FROM flood.return_period_result r
            LEFT JOIN core.river_segment rs
              ON rs.river_segment_id = r.river_segment_id
             AND rs.river_network_version_id = r.river_network_version_id
            {where_sql}
            ORDER BY r.return_period DESC NULLS LAST, r.river_network_version_id, r.river_segment_id, r.valid_time
            LIMIT :limit OFFSET :offset
            """
    ).bindparams(bindparam("usable_flags", expanding=True))
    if levels:
        statement = statement.bindparams(bindparam("levels", expanding=True))
    rows = session.execute(
        statement,
        {
            **params,
            "max_over_window": True,
            "usable_flags": tuple(USABLE_CURVE_FLAGS),
            "limit": limit,
            "offset": offset,
        },
    ).mappings()
    segments = [
        SegmentAlert(
            river_segment_id=str(row["river_segment_id"]),
            segment_id=str(row["river_segment_id"]),
            segment_name=_segment_name(row.get("properties_json")),
            basin_version_id=str(row["basin_version_id"]),
            river_network_version_id=_optional_str(row["river_network_version_id"]),
            q_value=_finite_result_float(row["q_value"], field="q_value"),
            return_period=_optional_float(row["return_period"]),
            warning_level=_optional_str(row["warning_level"]),
            valid_time=_format_time(row["valid_time"]),
            geom_centroid=_centroid_payload(row.get("geom_centroid") or row.get("geom_json")),
        )
        for row in rows
    ]
    return _ok(request, SegmentListResponse(segments=segments, total=total, limit=limit, offset=offset).model_dump())


@router.get("/api/v1/flood-alerts/timeline", response_model=dict[str, Any])
def flood_alert_timeline(
    request: Request,
    run_id: str = Query(...),
    segment_id: str = Query(...),
    river_network_version_id: str = Query(
        ...,
        min_length=1,
        description=(
            "River network version for the selected segment; required because river_segment_id is only unique "
            "within a river network version."
        ),
    ),
    max_points: int = Query(
        default=FLOOD_ALERT_TIMELINE_DEFAULT_MAX_POINTS,
        ge=1,
        le=FLOOD_ALERT_TIMELINE_MAX_POINTS,
        description="Maximum timeline points to return. Requests whose result set exceeds this budget fail with 413.",
    ),
    session: Session = Depends(get_flood_alert_session),
) -> dict[str, Any]:
    run = _require_frequency_ready(session, run_id)
    _require_flood_product_ready(session, run_id, status=_optional_str(run.get("status")))
    rows = list(
        session.execute(
            text(
                """
                SELECT river_segment_id, valid_time, q_value, return_period, warning_level, model_id,
                       river_network_version_id, basin_version_id, duration
                FROM flood.return_period_result
                WHERE run_id = :run_id
                  AND river_segment_id = :segment_id
                  AND river_network_version_id = :river_network_version_id
                  AND max_over_window = :max_over_window
                ORDER BY valid_time
                LIMIT :query_limit
                """
            ),
            {
                "run_id": run_id,
                "segment_id": segment_id,
                "river_network_version_id": river_network_version_id,
                "max_over_window": False,
                "query_limit": max_points + 1,
            },
        ).mappings()
    )
    if not rows:
        rows = list(
            session.execute(
                text(
                    """
                    SELECT river_segment_id, valid_time, q_value, return_period, warning_level, model_id,
                           river_network_version_id, basin_version_id, duration
                    FROM flood.return_period_result
                    WHERE run_id = :run_id
                      AND river_segment_id = :segment_id
                      AND river_network_version_id = :river_network_version_id
                    ORDER BY max_over_window, valid_time
                    LIMIT :query_limit
                    """
                ),
                {
                    "run_id": run_id,
                    "segment_id": segment_id,
                    "river_network_version_id": river_network_version_id,
                    "query_limit": max_points + 1,
                },
            ).mappings()
        )
    if len(rows) > max_points:
        raise ApiError(
            status_code=413,
            code="FLOOD_ALERT_TIMELINE_POINT_LIMIT_EXCEEDED",
            message="Flood alert timeline point budget exceeded; request a smaller result window.",
            details={"max_points": max_points},
        )
    timesteps = [
        TimelinePoint(
            valid_time=_format_time(row["valid_time"]),
            q_value=_finite_result_float(row["q_value"], field="q_value"),
            return_period=_optional_float(row["return_period"]),
            warning_level=_optional_str(row["warning_level"]),
        )
        for row in rows
    ]
    peak = max(timesteps, key=lambda point: point.return_period or -1, default=None)
    thresholds = _frequency_thresholds_for_result(session, rows[0]) if rows else None
    data = TimelineResponse(
        run_id=run_id,
        segment_id=segment_id,
        river_segment_id=segment_id,
        river_network_version_id=river_network_version_id,
        timesteps=timesteps,
        timeline=timesteps,
        peak=peak,
        frequency_thresholds=thresholds,
        quality_note=NO_SEGMENT_CURVE_NOTE if thresholds is None else None,
    )
    return _ok(request, data.model_dump())


@router.get(
    "/api/v1/tiles/flood-return-period",
    response_model=TileFeatureCollection,
)
def flood_return_period_map(
    run_id: str = Query(...),
    duration: str = Query(default="1h"),
    valid_time: datetime = Query(...),
    bbox: str | None = Query(default=None, description="Optional minLon,minLat,maxLon,maxLat filter."),
    return_period: float | None = Query(default=None, ge=0),
    limit: int = Query(
        default=FLOOD_RETURN_PERIOD_MAP_DEFAULT_LIMIT,
        ge=1,
        le=FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT,
        description="Maximum GeoJSON features to return. Requests that exceed the budget fail with 413.",
    ),
    session: Session = Depends(get_flood_alert_session),
) -> JSONResponse:
    """Return flood return-period map data as GeoJSON.

    This query route remains bounded compatibility for small or degraded views.
    National rendering should use the canonical `.pbf` MVT route discovered
    through `/api/v1/layers` metadata.
    """
    _validate_supported_flood_duration(duration)
    run = _require_frequency_ready(session, run_id)
    product_quality = _require_flood_route_product_ready(
        session,
        run_id=run_id,
        duration=duration,
        valid_time=valid_time,
        max_over_window=False,
        status=_optional_str(run.get("status")),
    )
    if return_period is not None:
        return_period = _finite_query_float(return_period, field="return_period", original=return_period)
    bounds = _parse_bbox(bbox)
    bbox_filter = ""
    if bounds is not None:
        bbox_filter = """
              AND EXISTS (
                SELECT 1
                FROM json_each(json_extract(rs.geom, '$.coordinates')) AS point
                WHERE json_extract(point.value, '$[0]') BETWEEN :min_lon AND :max_lon
                  AND json_extract(point.value, '$[1]') BETWEEN :min_lat AND :max_lat
              )
        """
        if session.get_bind().dialect.name != "sqlite":
            bbox_filter = """
              AND rs.geom && ST_Transform(
                ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326),
                4490
              )
            """
    rows = session.execute(
        text(_flood_return_period_map_sql(session, bbox_filter=bbox_filter)),
        {
            "run_id": run_id,
            "duration": duration,
            "valid_time": valid_time,
            "max_over_window": False,
            "return_period": return_period,
            "query_limit": limit + 1,
            "feature_coordinate_limit": FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES,
            "collection_coordinate_limit": FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES,
            "max_coordinate_dimensions": FLOOD_RETURN_PERIOD_MAP_MAX_COORDINATE_DIMENSIONS,
            **(_bbox_params(bounds) if bounds is not None else {}),
        },
    ).mappings()
    rows = list(rows)
    geometry_overflow_row = next((row for row in rows if row.get("geometry_limit_type")), None)
    if geometry_overflow_row is not None:
        limit_type = str(geometry_overflow_row["geometry_limit_type"])
        details: dict[str, Any] = {
            "limit_type": limit_type,
            "feature_count": int(geometry_overflow_row.get("geometry_feature_count") or 0),
        }
        if limit_type == "feature_coordinates":
            details["max_coordinates"] = FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES
            details["coordinate_count"] = int(geometry_overflow_row.get("geometry_coordinate_count") or 0)
        elif limit_type == "coordinate_dimensions":
            details["max_dimensions"] = FLOOD_RETURN_PERIOD_MAP_MAX_COORDINATE_DIMENSIONS
            details["coordinate_dimensions"] = int(geometry_overflow_row.get("geometry_dimension_count") or 0)
        raise ApiError(
            status_code=413,
            code="FLOOD_RETURN_PERIOD_GEOJSON_BUDGET_EXCEEDED",
            message="Flood return-period GeoJSON geometry budget exceeded; provide a bbox.",
            details=details,
        )
    overflow_row = next((row for row in rows if row.get("collection_overflow")), None)
    if overflow_row is not None:
        raise ApiError(
            status_code=413,
            code="FLOOD_RETURN_PERIOD_GEOJSON_BUDGET_EXCEEDED",
            message="Flood return-period GeoJSON geometry budget exceeded; provide a bbox.",
            details={
                "limit_type": "collection_coordinates",
                "max_coordinates": FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES,
                "coordinate_count": int(overflow_row.get("collection_coordinate_count") or 0),
            },
        )
    if len(rows) > limit:
        raise ApiError(
            status_code=413,
            code="FLOOD_RETURN_PERIOD_FEATURE_LIMIT_EXCEEDED",
            message="Flood return-period GeoJSON feature budget exceeded; provide a bbox or lower the result size.",
            details={"limit": limit},
        )
    payload = TileFeatureCollection(
        product_quality=product_quality,
        features=[
            TileFeature(
                properties={
                    "feature_id": _flood_return_period_feature_id(row),
                    "segment_id": str(row["river_segment_id"]),
                    "basin_version_id": str(row["basin_version_id"]),
                    "river_network_version_id": str(row["river_network_version_id"]),
                    "value": _finite_result_float(row["q_value"], field="q_value"),
                    "unit": str(row["q_unit"] or "m³/s"),
                    "quality_flag": str(row["quality_flag"]),
                    "return_period": _optional_float(row["return_period"]) or 0.0,
                    "warning_level": _optional_str(row["warning_level"]) or "unavailable",
                },
                geometry=_geojson_geometry(row.get("geom_json")),
            )
            for row in rows
        ]
    )
    _enforce_flood_return_period_geojson_budget(payload)
    return JSONResponse(content=payload.model_dump(), media_type="application/json")


@router.get(
    "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
)
def flood_return_period_mvt_tile(
    run_id: str,
    duration: str,
    valid_time: datetime,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_flood_alert_session),
) -> Response:
    validate_identifier(run_id, "run_id")
    validate_identifier(duration, "duration")
    _validate_supported_flood_duration(duration)
    validate_xyz(z, x, y)
    run = _require_frequency_ready(session, run_id)
    _require_flood_route_product_ready(
        session,
        run_id=run_id,
        duration=duration,
        valid_time=valid_time,
        max_over_window=False,
        status=_optional_str(run.get("status")),
    )
    basin_version_id, river_network_version_id = _require_run_source_identity(run, layer_id="flood-return-period")
    _require_flood_mvt_source_identity(
        session,
        run_id=run_id,
        duration=duration,
        valid_time=valid_time,
        basin_version_id=basin_version_id,
        river_network_version_id=river_network_version_id,
    )
    source_version = _run_source_version(run)
    tile_input = TileInput(
        layer_id="flood-return-period",
        source_id=run_id,
        source_version=source_version,
        valid_time=_format_time(valid_time),
        z=z,
        x=x,
        y=y,
        variant_id=f"duration:{duration}",
    )
    cached = read_cached_tile_response(session, tile_input)
    if cached is not None:
        return _mvt_response(cached)
    _require_live_postgis_mvt(session, tile_input.layer_id)
    data = _fetch_flood_mvt_tile_bytes(
        session,
        run_id=run_id,
        duration=duration,
        valid_time=valid_time,
        basin_version_id=basin_version_id,
        river_network_version_id=river_network_version_id,
        z=z,
        x=x,
        y=y,
    )
    return _mvt_response(build_raw_tile_response(session, tile_input, data))


@router.get(
    "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
)
def hydro_mvt_tile(
    run_id: str,
    variable: str,
    valid_time: datetime,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_flood_alert_session),
) -> Response:
    validate_identifier(run_id, "run_id")
    validate_identifier(variable, "variable")
    _validate_supported_hydro_variable(variable)
    validate_xyz(z, x, y)
    run = _require_display_ready(session, run_id)
    basin_version_id, river_network_version_id = _require_run_source_identity(
        run,
        layer_id=public_hydro_layer_id(variable),
    )
    _require_hydro_mvt_source_identity(
        session,
        run_id=run_id,
        variable=variable,
        valid_time=valid_time,
        basin_version_id=basin_version_id,
        river_network_version_id=river_network_version_id,
    )
    source_version = _run_source_version(run)
    tile_input = TileInput(
        layer_id=public_hydro_layer_id(variable),
        source_id=run_id,
        source_version=source_version,
        valid_time=_format_time(valid_time),
        z=z,
        x=x,
        y=y,
        variant_id=f"variable:{variable}",
    )
    cached = read_cached_tile_response(session, tile_input)
    if cached is not None:
        return _mvt_response(cached)
    _require_live_postgis_mvt(session, tile_input.layer_id)
    data = _fetch_hydro_mvt_tile_bytes(
        session,
        run_id=run_id,
        variable=variable,
        valid_time=valid_time,
        basin_version_id=basin_version_id,
        river_network_version_id=river_network_version_id,
        z=z,
        x=x,
        y=y,
    )
    return _mvt_response(build_raw_tile_response(session, tile_input, data))


@router.get(
    "/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
)
def hydro_national_mvt_tile(
    variable: str,
    valid_time: datetime,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_flood_alert_session),
) -> Response:
    """National discharge overview tile.

    Renders the requested hydrological variable for every basin by joining each river
    network's latest display-ready run (selected inside the SQL). There is no single
    run/basin identity, so the per-run display-ready / source-identity preconditions
    used by the single-run hydro route do not apply; the live-PostGIS gate still does.
    """
    validate_identifier(variable, "variable")
    _validate_supported_hydro_variable(variable)
    validate_xyz(z, x, y)
    tile_input = TileInput(
        layer_id=public_hydro_layer_id(variable),
        source_id=HYDRO_NATIONAL_SOURCE_ID,
        source_version=HYDRO_NATIONAL_SOURCE_VERSION,
        valid_time=_format_time(valid_time),
        z=z,
        x=x,
        y=y,
        variant_id=f"variable:{variable}",
    )
    cached = read_cached_tile_response(session, tile_input)
    if cached is not None:
        return _mvt_response(cached)
    _require_live_postgis_mvt(session, tile_input.layer_id)
    data = _fetch_hydro_national_mvt_tile_bytes(
        session,
        variable=variable,
        valid_time=valid_time,
        z=z,
        x=x,
        y=y,
    )
    return _mvt_response(build_raw_tile_response(session, tile_input, data))


@router.get(
    "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
)
def river_network_mvt_tile(
    basin_version_id: str,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_flood_alert_session),
) -> Response:
    validate_identifier(basin_version_id, "basin_version_id")
    validate_xyz(z, x, y)
    source_version = _river_network_source_version(session, basin_version_id)
    tile_input = TileInput(
        layer_id="river-network",
        source_id=basin_version_id,
        source_version=source_version,
        valid_time=None,
        z=z,
        x=x,
        y=y,
    )
    cached = read_cached_tile_response(session, tile_input)
    if cached is not None:
        return _mvt_response(cached)
    _require_live_postgis_mvt(session, "river-network")
    data = _fetch_river_network_mvt_tile_bytes(session, basin_version_id=basin_version_id, z=z, x=x, y=y)
    return _mvt_response(build_raw_tile_response(session, tile_input, data))


@router.get(
    "/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
    operation_id="getMetStationTile",
    summary="Get meteorological station vector tile",
    description=(
        "Canonical M16 Mapbox Vector Tile route for meteorological station points. "
        "The vector source-layer is `met_stations` and features include `station_id`, "
        "`basin_version_id`, `station_name`, `station_role`, and `active_flag`."
    ),
)
def met_station_mvt_tile(
    basin_version_id: str,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_flood_alert_session),
) -> Response:
    validate_identifier(basin_version_id, "basin_version_id")
    validate_xyz(z, x, y)
    source_version = _station_source_version(session, basin_version_id)
    tile_input = TileInput(
        layer_id="met-stations",
        source_id=basin_version_id,
        source_version=source_version,
        valid_time=None,
        z=z,
        x=x,
        y=y,
    )
    cached = read_cached_tile_response(session, tile_input)
    if cached is not None:
        return _mvt_response(cached)
    _require_live_postgis_mvt(session, "met-stations")
    data = _fetch_station_mvt_tile_bytes(session, basin_version_id=basin_version_id, z=z, x=x, y=y)
    return _mvt_response(build_raw_tile_response(session, tile_input, data))


def _build_deterministic_tile_response_for_tests(
    session: Session,
    tile: TileInput,
    layer_name: str,
    features: list[dict[str, Any]],
) -> Response:
    """Private helper for deterministic encoder unit coverage; not used by canonical routes."""
    return _mvt_response(
        build_tile_response(
            session,
            tile,
            layer_name,
            features,
        )
    )


def _mvt_live_postgis_enabled(session: Session) -> bool:
    return session.get_bind().dialect.name != "sqlite" and os.getenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _require_live_postgis_mvt(session: Session, layer_id: str) -> None:
    if _mvt_live_postgis_enabled(session):
        return
    raise ApiError(
        status_code=424,
        code="MVT_LIVE_POSTGIS_UNAVAILABLE",
        message="Live PostGIS MVT is required for canonical .pbf tile routes and is not enabled.",
        details={
            "layer_id": layer_id,
            "required_env": "NHMS_ENABLE_LIVE_POSTGIS_MVT=true",
            "fallback_endpoint": "/api/v1/tiles/flood-return-period" if layer_id == "flood-return-period" else None,
        },
    )


def _fetch_postgis_tile_bytes(session: Session, layer: str, params: dict[str, Any], *, z: int, x: int, y: int) -> bytes:
    _require_live_postgis_mvt(session, layer)
    detail_layer_id = (
        public_hydro_layer_id(str(params["variable"]))
        if layer in {"hydro", "hydro-national"} and "variable" in params
        else layer
    )
    row = session.execute(
        text(postgis_tile_sql(layer)),
        _postgis_tile_params(params, z=z, x=x, y=y),
    ).mappings().first()
    feature_count = int(row.get("feature_count") or 0) if row else 0
    coordinate_count = int(row.get("coordinate_count") or 0) if row else 0
    feature_coordinate_overflow_count = int(row.get("feature_coordinate_overflow_count") or 0) if row else 0
    source_identity_count = int(row.get("source_identity_count") or 0) if row else 0
    feature_coordinate_count = int(row.get("feature_coordinate_count") or 0) if row else 0
    coordinate_dimension_overflow_count = int(row.get("coordinate_dimension_overflow_count") or 0) if row else 0
    coordinate_dimension_count = int(row.get("coordinate_dimension_count") or 0) if row else 0
    invalid_property_count = int(row.get("invalid_property_count") or 0) if row else 0
    invalid_properties = _mvt_invalid_properties(row.get("invalid_properties") if row else None)
    if feature_coordinate_overflow_count > 0:
        raise ApiError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Live PostGIS MVT tile contained a feature over the coordinate budget.",
            details={
                "layer_id": detail_layer_id,
                "z": z,
                "x": x,
                "y": y,
                "limit_type": "feature_coordinates",
                "feature_count": feature_coordinate_overflow_count,
                "coordinate_count": feature_coordinate_count,
                "max_coordinates": FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES,
            },
        )
    if coordinate_dimension_overflow_count > 0:
        raise ApiError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Live PostGIS MVT tile contained a feature over the coordinate dimension budget.",
            details={
                "layer_id": detail_layer_id,
                "z": z,
                "x": x,
                "y": y,
                "limit_type": "coordinate_dimensions",
                "feature_count": coordinate_dimension_overflow_count,
                "coordinate_dimensions": coordinate_dimension_count,
                "max_coordinate_dimensions": FLOOD_RETURN_PERIOD_MAP_MAX_COORDINATE_DIMENSIONS,
            },
        )
    if invalid_property_count > 0:
        raise ApiError(
            status_code=500,
            code="MVT_PROPERTY_INVALID",
            message="Live PostGIS MVT tile contained missing or non-finite required feature properties.",
            details={
                "layer_id": detail_layer_id,
                "z": z,
                "x": x,
                "y": y,
                "invalid_property_count": invalid_property_count,
                "properties": invalid_properties,
            },
        )
    if (
        feature_count > FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT
        or coordinate_count > FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES
    ):
        raise ApiError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Live PostGIS MVT tile exceeded the configured feature or coordinate budget.",
            details={
                "layer_id": detail_layer_id,
                "z": z,
                "x": x,
                "y": y,
                "feature_count": feature_count,
                "max_features": FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT,
                "coordinate_count": coordinate_count,
                "max_coordinates": FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES,
            },
        )
    data = bytes(row["tile"] or b"") if row and row.get("tile") is not None else b""
    if not row or source_identity_count <= 0:
        raise ApiError(
            status_code=424,
            code="MVT_LIVE_POSTGIS_UNAVAILABLE",
            message="Live PostGIS MVT query returned no source rows for the requested identity.",
            details={"layer_id": detail_layer_id, "z": z, "x": x, "y": y},
        )
    return data


def _mvt_invalid_properties(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [item for item in str(value).split(",") if item]


def _fetch_flood_mvt_tile_bytes(
    session: Session,
    *,
    run_id: str,
    duration: str,
    valid_time: datetime,
    basin_version_id: str,
    river_network_version_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    return _fetch_postgis_tile_bytes(
        session,
        "flood-return-period",
        {
            "run_id": run_id,
            "duration": duration,
            "valid_time": valid_time,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
        z=z,
        x=x,
        y=y,
    )


def _fetch_hydro_mvt_tile_bytes(
    session: Session,
    *,
    run_id: str,
    variable: str,
    valid_time: datetime,
    basin_version_id: str,
    river_network_version_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    return _fetch_postgis_tile_bytes(
        session,
        "hydro",
        {
            "run_id": run_id,
            "variable": variable,
            "valid_time": valid_time,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
        z=z,
        x=x,
        y=y,
    )


def _fetch_hydro_national_mvt_tile_bytes(
    session: Session,
    *,
    variable: str,
    valid_time: datetime,
    z: int,
    x: int,
    y: int,
) -> bytes:
    # National overview binds only variable/valid_time; the latest-per-basin run/network
    # identity is resolved inside postgis_tile_sql("hydro-national").
    return _fetch_postgis_tile_bytes(
        session,
        "hydro-national",
        {"variable": variable, "valid_time": valid_time},
        z=z,
        x=x,
        y=y,
    )


def _fetch_river_network_mvt_tile_bytes(
    session: Session,
    *,
    basin_version_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    return _fetch_postgis_tile_bytes(
        session,
        "river-network",
        {"basin_version_id": basin_version_id},
        z=z,
        x=x,
        y=y,
    )


def _fetch_station_mvt_tile_bytes(
    session: Session,
    *,
    basin_version_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    return _fetch_postgis_tile_bytes(
        session,
        "met-stations",
        {"basin_version_id": basin_version_id},
        z=z,
        x=x,
        y=y,
    )


def _river_network_source_version(session: Session, basin_version_id: str) -> str:
    rows = session.execute(
        text(
            """
            SELECT DISTINCT river_network_version_id
            FROM core.river_network_version
            WHERE basin_version_id = :basin_version_id
            ORDER BY river_network_version_id
            """
        ),
        {"basin_version_id": basin_version_id},
    ).mappings().all()
    versions = [str(row["river_network_version_id"]) for row in rows if row.get("river_network_version_id") is not None]
    if not versions:
        raise ApiError(
            status_code=404,
            code="MVT_SOURCE_IDENTITY_NOT_FOUND",
            message="River-network MVT source identity was not found for the requested basin version.",
            details={"layer_id": "river-network", "basin_version_id": basin_version_id},
        )
    joined = ",".join(versions)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
    return f"river-network-set:{digest}:{joined}"


def _station_source_version(session: Session, basin_version_id: str) -> str:
    try:
        limit = FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT
        row_limit = limit + 1
        if session.get_bind().dialect.name == "sqlite":
            rows = (
                session.execute(
                    text(
                        """
                        SELECT station_id,
                               basin_version_id,
                               COALESCE(station_name, '') AS station_name,
                               station_role,
                               active_flag, geom, created_at
                        FROM met.met_station
                        WHERE basin_version_id = :basin_version_id
                          AND active_flag = 1
                        ORDER BY station_id
                        LIMIT :limit
                        """
                    ),
                    {"basin_version_id": basin_version_id, "limit": row_limit},
                )
                .mappings()
                .all()
            )
        else:
            rows = (
                session.execute(
                    text(
                        """
                        SELECT station_id,
                               basin_version_id,
                               COALESCE(station_name, '') AS station_name,
                               station_role,
                               active_flag,
                               encode(ST_AsEWKB(geom), 'hex') AS geom,
                               created_at
                        FROM met.met_station
                        WHERE basin_version_id = :basin_version_id
                          AND active_flag = true
                        ORDER BY station_id
                        LIMIT :limit
                        """
                    ),
                    {"basin_version_id": basin_version_id, "limit": row_limit},
                )
                .mappings()
                .all()
            )
    except SQLAlchemyError as exc:
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        raise ApiError(
            status_code=424,
            code="MVT_LIVE_POSTGIS_UNAVAILABLE",
            message="Station MVT source inventory is unavailable for canonical .pbf tile generation.",
            details={"layer_id": "met-stations", "basin_version_id": basin_version_id},
        ) from exc

    if not rows:
        _raise_station_source_not_found(basin_version_id)
    if len(rows) > FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT:
        _raise_station_source_budget_exceeded(basin_version_id, observed_count=len(rows))

    basis = {
        "columns": [
            "station_id",
            "basin_version_id",
            "station_name",
            "station_role",
            "active_flag",
            "geom",
            "created_at",
        ],
        "rows": [
            [
                row.get("station_id"),
                row.get("basin_version_id"),
                row.get("station_name"),
                row.get("station_role"),
                _station_active_flag(row.get("active_flag")),
                row.get("geom"),
                canonical_mvt_time(row.get("created_at")),
            ]
            for row in rows
        ],
    }
    digest = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    station_count = len(rows)
    min_station_id = str(rows[0].get("station_id") or "")
    max_station_id = str(rows[-1].get("station_id") or "")
    return f"met-stations:{digest}:{basin_version_id}:{station_count}:{min_station_id}:{max_station_id}"


def _station_active_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "t", "true", "yes"}
    return bool(value)


def _raise_station_source_budget_exceeded(basin_version_id: str, *, observed_count: int) -> None:
    raise ApiError(
        status_code=413,
        code="MVT_TILE_BUDGET_EXCEEDED",
        message="Station MVT source inventory exceeded the configured feature budget.",
        details={
            "layer_id": "met-stations",
            "basin_version_id": basin_version_id,
            "limit_type": "source_inventory",
            "feature_count": observed_count,
            "max_features": FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT,
        },
    )

def _raise_station_source_not_found(basin_version_id: str) -> None:
    raise ApiError(
        status_code=404,
        code="MVT_SOURCE_IDENTITY_NOT_FOUND",
        message="Station MVT source identity was not found for the requested basin version.",
        details={"layer_id": "met-stations", "basin_version_id": basin_version_id},
    )


def _require_hydro_mvt_source_identity(
    session: Session,
    *,
    run_id: str,
    variable: str,
    valid_time: datetime,
    basin_version_id: str,
    river_network_version_id: str,
) -> None:
    row = session.execute(
        text(
            """
            SELECT 1
            FROM hydro.river_timeseries
            WHERE run_id = :run_id
              AND basin_version_id = :basin_version_id
              AND river_network_version_id = :river_network_version_id
              AND variable = :variable
              AND valid_time = :valid_time
            LIMIT 1
            """
        ),
        {
            "run_id": run_id,
            "variable": variable,
            "valid_time": valid_time,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
    ).first()
    if row is not None:
        return
    raise ApiError(
        status_code=404,
        code="MVT_SOURCE_IDENTITY_NOT_FOUND",
        message="Hydrological MVT source/time identity was not found for the requested route.",
        details={
            "layer_id": public_hydro_layer_id(variable),
            "run_id": run_id,
            "variable": variable,
            "valid_time": _format_time(valid_time),
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
    )


def _require_flood_mvt_source_identity(
    session: Session,
    *,
    run_id: str,
    duration: str,
    valid_time: datetime,
    basin_version_id: str,
    river_network_version_id: str,
) -> None:
    row = session.execute(
        text(
            """
            SELECT 1
            FROM flood.return_period_result
            WHERE run_id = :run_id
              AND basin_version_id = :basin_version_id
              AND river_network_version_id = :river_network_version_id
              AND duration = :duration
              AND max_over_window = false
              AND valid_time = :valid_time
            LIMIT 1
            """
        ),
        {
            "run_id": run_id,
            "duration": duration,
            "valid_time": valid_time,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
    ).first()
    if row is not None:
        return
    raise ApiError(
        status_code=404,
        code="MVT_SOURCE_IDENTITY_NOT_FOUND",
        message="Flood return-period MVT source/time identity was not found for the requested route.",
        details={
            "layer_id": "flood-return-period",
            "run_id": run_id,
            "duration": duration,
            "max_over_window": False,
            "valid_time": _format_time(valid_time),
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
    )


def _require_flood_route_product_ready(
    session: Session,
    *,
    run_id: str,
    duration: str,
    valid_time: datetime,
    max_over_window: bool,
    status: str | None = None,
) -> dict[str, Any]:
    quality = _flood_product_quality(session, run_id, status=status)
    if quality["quality_state"] != "ready":
        unavailable_products = list(quality["unavailable_products"])
        raise ApiError(
            status_code=409,
            code="FLOOD_PRODUCT_UNAVAILABLE",
            message="Flood return-period product is unavailable or degraded for the requested tile identity.",
            details={
                "run_id": run_id,
                "duration": duration,
                "valid_time": _format_time(valid_time),
                "max_over_window": max_over_window,
                "return_period_result": (
                    "unavailable" if "return_period_result" in unavailable_products else "available"
                ),
                "frequency_curves": "unavailable" if "frequency_curves" in unavailable_products else "available",
                "warning_thresholds": (
                    "unavailable" if "warning_thresholds" in unavailable_products else "available"
                ),
                **quality,
            },
        )
    row = session.execute(
        text(
            """
            SELECT COUNT(*) AS result_rows,
                   SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) AS return_period_rows,
                   SUM(CASE WHEN warning_level IS NOT NULL THEN 1 ELSE 0 END) AS warning_rows
            FROM flood.return_period_result
            WHERE run_id = :run_id
              AND duration = :duration
              AND valid_time = :valid_time
              AND max_over_window = :max_over_window
            """
        ),
        {
            "run_id": run_id,
            "duration": duration,
            "valid_time": valid_time,
            "max_over_window": max_over_window,
        },
    ).mappings().one()
    result_rows = int(row["result_rows"] or 0)
    return_period_rows = int(row["return_period_rows"] or 0)
    warning_rows = int(row["warning_rows"] or 0)
    unavailable_products: list[str] = []
    residual_blockers: list[dict[str, Any]] = []
    if result_rows <= 0:
        return quality
    if return_period_rows <= 0:
        unavailable_products.append("return_period_result")
        residual_blockers.append(
            {
                "code": "RETURN_PERIOD_RESULT_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run_id,
                "residual_risk": "No non-null return-period rows are available for the requested tile identity.",
            }
        )
    elif result_rows > return_period_rows:
        unavailable_products.append("frequency_curves")
        residual_blockers.append(
            {
                "code": "FREQUENCY_CURVES_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run_id,
                "residual_risk": (
                    "Some requested tile rows have null return_period because frequency curves are unavailable."
                ),
            }
        )
    if return_period_rows > 0 and warning_rows < return_period_rows:
        unavailable_products.append("warning_thresholds")
        residual_blockers.append(
            {
                "code": "WARNING_THRESHOLDS_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run_id,
                "residual_risk": "warning_level remains null for requested tile rows.",
            }
        )
    if unavailable_products:
        raise ApiError(
            status_code=409,
            code="FLOOD_PRODUCT_UNAVAILABLE",
            message="Flood return-period product is unavailable or degraded for the requested tile identity.",
            details={
                "run_id": run_id,
                "duration": duration,
                "valid_time": _format_time(valid_time),
                "max_over_window": max_over_window,
                "quality_state": "unavailable",
                "result_rows": result_rows,
                "return_period_rows": return_period_rows,
                "warning_rows": warning_rows,
                "return_period_result": (
                    "unavailable" if "return_period_result" in unavailable_products else "available"
                ),
                "frequency_curves": "unavailable" if "frequency_curves" in unavailable_products else "available",
                "warning_thresholds": (
                    "unavailable" if "warning_thresholds" in unavailable_products else "available"
                ),
                "unavailable_products": unavailable_products,
                "residual_blockers": residual_blockers,
            },
        )
    return quality


def _require_run_source_identity(run: dict[str, Any] | Any, *, layer_id: str) -> tuple[str, str]:
    basin_version_id = run.get("basin_version_id")
    river_network_version_id = run.get("river_network_version_id")
    if basin_version_id and river_network_version_id:
        return str(basin_version_id), str(river_network_version_id)
    raise ApiError(
        status_code=404,
        code="MVT_SOURCE_IDENTITY_NOT_FOUND",
        message="Run-scoped MVT source identity was not found for the selected ready run.",
        details={
            "layer_id": layer_id,
            "run_id": run.get("run_id"),
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
    )


def _run_source_version(run: dict[str, Any] | Any) -> str:
    base_version = str(run.get("river_network_version_id") or run.get("basin_version_id") or run.get("run_id"))
    revision_basis = {
        "basin_version_id": run.get("basin_version_id"),
        "cycle_time": canonical_mvt_time(run.get("cycle_time")),
        "river_network_version_id": run.get("river_network_version_id"),
        "run_id": run.get("run_id"),
        "source_id": run.get("source_id"),
        "status": run.get("status"),
        "updated_at": canonical_mvt_time(run.get("updated_at")),
    }
    digest = hashlib.sha256(
        json.dumps(revision_basis, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"{base_version};run-revision:{digest}"


def _require_frequency_ready(session: Session, run_id: str) -> dict[str, Any]:
    row = _run_row(session, run_id)
    if str(row["status"]) not in FLOOD_PRODUCT_READY_STATUSES:
        raise ApiError(
            status_code=409,
            code="FREQUENCY_NOT_COMPUTED",
            message="Return period results not yet available for this run",
            details={"run_id": run_id, "status": row["status"]},
        )
    return row


def _require_display_ready(session: Session, run_id: str) -> dict[str, Any]:
    row = _run_row(session, run_id)
    if str(row["status"]) not in DISPLAY_PRODUCT_READY_STATUSES:
        raise ApiError(
            status_code=409,
            code="DISPLAY_PRODUCT_NOT_READY",
            message="Display hydrology products are not yet available for this run",
            details={
                "run_id": run_id,
                "status": row["status"],
                "allowed_statuses": sorted(DISPLAY_PRODUCT_READY_STATUSES),
            },
        )
    return row


def _run_row(session: Session, run_id: str) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT h.run_id, h.status, h.model_id, h.basin_version_id, h.source_id, h.cycle_time,
                   h.updated_at, mi.river_network_version_id
            FROM hydro.hydro_run h
            LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
            WHERE h.run_id = :run_id
            LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if row is None:
        raise ApiError(
            status_code=404,
            code="RUN_NOT_FOUND",
            message=f"Run not found: {run_id}",
            details={"run_id": run_id},
        )
    return dict(row)


def _require_run(session: Session, run_id: str) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT h.run_id, h.status, h.model_id, h.basin_version_id, h.source_id, h.cycle_time,
                   h.updated_at, mi.river_network_version_id
            FROM hydro.hydro_run h
            LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
            WHERE h.run_id = :run_id
            LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if row is None:
        raise ApiError(
            status_code=404,
            code="RUN_NOT_FOUND",
            message=f"Run not found: {run_id}",
            details={"run_id": run_id},
        )
    return dict(row)


def _require_flood_product_ready(session: Session, run_id: str, *, status: str | None = None) -> dict[str, Any]:
    quality = _flood_product_quality(session, run_id, status=status)
    if quality["quality_state"] != "ready":
        raise ApiError(
            status_code=409,
            code="FLOOD_PRODUCT_UNAVAILABLE",
            message="Flood return-period product is unavailable or degraded for this run.",
            details={"run_id": run_id, **quality},
        )
    return quality


def _flood_product_quality(session: Session, run_id: str, *, status: str | None = None) -> dict[str, Any]:
    mode = _flood_product_quality_mode(session)
    if mode == "explicit":
        row = _explicit_flood_product_quality_row(session, run_id)
        quality = _explicit_flood_quality_from_row(row, run_id=run_id)
        if status is not None:
            quality["status"] = status
        return quality

    row = _flood_product_quality_counts(session, run_id, max_over_window=True)
    max_over_window: bool | None = True
    if int(row["result_rows"] or 0) <= 0:
        row = _flood_product_quality_counts(session, run_id, max_over_window=None)
        max_over_window = None
    result_rows = int(row["result_rows"] or 0)
    return_period_rows = int(row["return_period_rows"] or 0)
    warning_rows = int(row["warning_rows"] or 0)
    unavailable_products: list[str] = []
    residual_blockers: list[dict[str, Any]] = []
    if return_period_rows <= 0:
        unavailable_products.append("return_period_result")
        residual_blockers.append(
            {
                "code": "RETURN_PERIOD_RESULT_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run_id,
                "residual_risk": "No non-null peak return-period rows are available for this run.",
            }
        )
    elif result_rows > return_period_rows:
        unavailable_products.append("frequency_curves")
        residual_blockers.append(
            {
                "code": "FREQUENCY_CURVES_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run_id,
                "residual_risk": "Some peak rows have null return_period because frequency curves are unavailable.",
            }
        )
    if return_period_rows > 0 and warning_rows < return_period_rows:
        unavailable_products.append("warning_thresholds")
        residual_blockers.append(
            {
                "code": "WARNING_THRESHOLDS_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run_id,
                "residual_risk": "warning_level remains null for peak return-period rows.",
            }
        )
    quality_state = "ready"
    if "warning_thresholds" in unavailable_products or "return_period_result" in unavailable_products:
        quality_state = "unavailable"
    elif unavailable_products:
        quality_state = "degraded"
    return {
        "quality_state": quality_state,
        "quality_source": "legacy_row_count",
        **({"status": status} if status is not None else {}),
        "max_over_window": max_over_window,
        "result_rows": result_rows,
        "return_period_rows": return_period_rows,
        "warning_rows": warning_rows,
        "expected_result_rows": result_rows,
        "expected_max_result_rows": result_rows if max_over_window else 0,
        "expected_timestep_result_rows": result_rows if max_over_window is False else 0,
        "meaningful_result_rows": return_period_rows,
        "meaningful_max_result_rows": return_period_rows if max_over_window else 0,
        "meaningful_timestep_result_rows": return_period_rows if max_over_window is False else 0,
        "no_frequency_curve_rows": max(result_rows - return_period_rows, 0),
        "no_usable_frequency_curve_rows": 0,
        "warning_threshold_unavailable_rows": max(return_period_rows - warning_rows, 0),
        "unavailable_products": unavailable_products,
        "residual_blockers": residual_blockers,
    }


def _flood_product_quality_mode(session: Session) -> str:
    columns = _flood_run_product_quality_columns(session)
    if not columns:
        return "missing_table"
    return "explicit" if FLOOD_PRODUCT_QUALITY_EXPLICIT_COLUMNS <= columns else "legacy_table"


def _flood_run_product_quality_columns(session: Session) -> set[str]:
    if session.get_bind().dialect.name == "sqlite":
        try:
            rows = session.execute(text("PRAGMA flood.table_info(run_product_quality)")).mappings()
            return {str(row["name"]) for row in rows}
        except SQLAlchemyError:
            return set()
    try:
        rows = session.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'flood'
                  AND table_name = 'run_product_quality'
                """
            )
        ).mappings()
        return {str(row["column_name"]) for row in rows}
    except SQLAlchemyError:
        return set()


def _explicit_flood_product_quality_row(session: Session, run_id: str) -> Any:
    return session.execute(
        text(
            """
            SELECT
                run_id,
                quality_state,
                quality_source,
                unavailable_products,
                residual_blockers,
                result_rows,
                max_result_rows,
                return_period_rows,
                warning_rows,
                max_return_period_rows,
                max_warning_rows,
                expected_result_rows,
                expected_max_result_rows,
                expected_timestep_result_rows,
                meaningful_result_rows,
                meaningful_max_result_rows,
                meaningful_timestep_result_rows,
                no_frequency_curve_rows,
                no_usable_frequency_curve_rows,
                warning_threshold_unavailable_rows
            FROM flood.run_product_quality
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run_id},
    ).mappings().first()


def _explicit_flood_quality_from_row(row: Any, *, run_id: str) -> dict[str, Any]:
    if row is None:
        return _missing_explicit_flood_quality(run_id)
    result_rows = _non_negative_int(row.get("max_result_rows") or row.get("result_rows"))
    return_period_rows = _non_negative_int(row.get("max_return_period_rows") or row.get("return_period_rows"))
    warning_rows = _non_negative_int(row.get("max_warning_rows") or row.get("warning_rows"))
    quality_state = str(row.get("quality_state") or "unavailable")
    if quality_state not in {"ready", "degraded", "unavailable"}:
        quality_state = "unavailable"
    unavailable_products = _json_list(row.get("unavailable_products"), strings=True)
    residual_blockers = _json_list(row.get("residual_blockers"), mappings=True)
    if quality_state != "ready" and not unavailable_products:
        unavailable_products = ["return_period_result"]
    if quality_state != "ready" and not residual_blockers:
        residual_blockers = [
            {
                "code": "RETURN_PERIOD_RESULT_UNAVAILABLE",
                "state": quality_state,
                "quality_flag": "explicit_flood_product_unavailable",
                "run_id": run_id,
                "residual_risk": "Explicit run-level flood product quality is not ready.",
            }
        ]
    return {
        "quality_state": quality_state,
        "quality_source": str(row.get("quality_source") or "explicit"),
        "max_over_window": bool(row.get("max_result_rows")) if result_rows > 0 else None,
        "result_rows": result_rows,
        "return_period_rows": return_period_rows,
        "warning_rows": warning_rows,
        "expected_result_rows": _non_negative_int(row.get("expected_result_rows")),
        "expected_max_result_rows": _non_negative_int(row.get("expected_max_result_rows")),
        "expected_timestep_result_rows": _non_negative_int(row.get("expected_timestep_result_rows")),
        "meaningful_result_rows": _non_negative_int(row.get("meaningful_result_rows")),
        "meaningful_max_result_rows": _non_negative_int(row.get("meaningful_max_result_rows")),
        "meaningful_timestep_result_rows": _non_negative_int(row.get("meaningful_timestep_result_rows")),
        "no_frequency_curve_rows": _non_negative_int(row.get("no_frequency_curve_rows")),
        "no_usable_frequency_curve_rows": _non_negative_int(row.get("no_usable_frequency_curve_rows")),
        "warning_threshold_unavailable_rows": _non_negative_int(row.get("warning_threshold_unavailable_rows")),
        "unavailable_products": unavailable_products,
        "residual_blockers": residual_blockers,
    }


def _missing_explicit_flood_quality(run_id: str) -> dict[str, Any]:
    return {
        "quality_state": "unavailable",
        "quality_source": "explicit",
        "max_over_window": None,
        "result_rows": 0,
        "return_period_rows": 0,
        "warning_rows": 0,
        "expected_result_rows": 0,
        "expected_max_result_rows": 0,
        "expected_timestep_result_rows": 0,
        "meaningful_result_rows": 0,
        "meaningful_max_result_rows": 0,
        "meaningful_timestep_result_rows": 0,
        "no_frequency_curve_rows": 0,
        "no_usable_frequency_curve_rows": 0,
        "warning_threshold_unavailable_rows": 0,
        "unavailable_products": ["return_period_result"],
        "residual_blockers": [
            {
                "code": "RETURN_PERIOD_RESULT_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": "missing_run_product_quality",
                "run_id": run_id,
                "residual_risk": "No run-level flood product quality row exists for this run.",
            }
        ],
    }


def _json_list(value: Any, *, strings: bool = False, mappings: bool = False) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list | tuple):
        return []
    if strings:
        return [str(item) for item in value if str(item or "").strip()]
    if mappings:
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return list(value)


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _flood_product_quality_counts(session: Session, run_id: str, *, max_over_window: bool | None) -> Any:
    if _flood_product_quality_mode(session) == "missing_table":
        return _missing_table_flood_product_quality_counts(session, run_id)
    row = session.execute(
        text(
            """
            SELECT
                CASE WHEN :max_over_window IS TRUE THEN max_result_rows ELSE result_rows END AS result_rows,
                CASE WHEN :max_over_window IS TRUE THEN max_return_period_rows ELSE return_period_rows END
                    AS return_period_rows,
                CASE WHEN :max_over_window IS TRUE THEN max_warning_rows ELSE warning_rows END AS warning_rows
            FROM flood.run_product_quality
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run_id, "max_over_window": max_over_window},
    ).mappings().first()
    if row is not None:
        return row
    return session.execute(
        text(
            """
            SELECT 0 AS result_rows,
                   0 AS return_period_rows,
                   0 AS warning_rows
            """
        ),
    ).mappings().one()


def _missing_table_flood_product_quality_counts(session: Session, run_id: str) -> Any:
    row = session.execute(
        text(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM flood.return_period_result
                    WHERE run_id = :run_id
                ) AS has_product
            """
        ),
        {"run_id": run_id},
    ).mappings().one()
    has_product = bool(row["has_product"])
    return {
        "result_rows": 1 if has_product else 0,
        "return_period_rows": 1 if has_product else 0,
        "warning_rows": 1 if has_product else 0,
    }


def _annotate_flood_layer_quality(layers: list[Layer], quality: dict[str, Any]) -> None:
    for layer in layers:
        if layer.layer_id not in {"flood-return-period", "warning-level"}:
            continue
        metadata = dict(layer.metadata or {})
        metadata["product_quality"] = quality
        metadata["quality_state"] = quality["quality_state"]
        metadata["unavailable_products"] = list(quality["unavailable_products"])
        metadata["residual_blockers"] = list(quality["residual_blockers"])
        layer.metadata = metadata


def _mvt_response(tile: Any) -> Response:
    return Response(
        content=tile.data,
        media_type=MVT_MEDIA_TYPE,
        headers={
            "Cache-Control": "public, max-age=300",
            "ETag": tile.etag,
            "X-Tile-Layer-ID": tile.layer_id,
            "X-Tile-Checksum": tile.checksum,
            "X-Tile-Cache-Key": tile.cache_key,
            "X-Tile-Cache": tile.cache_status,
            "X-MVT-Schema-Version": MVT_SCHEMA_VERSION,
        },
    )


def _default_layer_catalog(
    session: Session,
    *,
    run_id: str | None,
    source_version: str | None,
    basin_version_id: str | None,
    river_network_version_id: str | None = None,
    river_network_source_version: str | None = None,
    national: bool = False,
) -> list[Layer]:
    if run_id is not None and (basin_version_id is None or river_network_version_id is None):
        raise ApiError(
            status_code=404,
            code="MVT_SOURCE_IDENTITY_NOT_FOUND",
            message="Run-scoped MVT source identity was not found for the selected ready run.",
            details={
                "layer_id": "layers",
                "run_id": run_id,
                "basin_version_id": basin_version_id,
                "river_network_version_id": river_network_version_id,
            },
        )
    layers = []
    for layer_id, name, layer_type, variables in _PUBLIC_LAYER_DEFINITIONS:
        # spec invariant (overview-data-contracts: Default discharge tile URL is national across all
        # /api/v1/layers callers): discharge layer is always national, regardless of whether the caller
        # passed run_id. Without this, frontend enrichment fetchLayers(latestRun.run_id) collapses the
        # discharge tile URL to single-basin /api/v1/tiles/hydro/{run_id}/... and basins other than
        # latestRun's get no tile at all (root cause of issue #601 / heihe-invisible regression).
        # Flood-return-period/warning-level continue to honor the caller's run_id via the elif below.
        national_discharge = layer_id == "discharge"
        if national_discharge:
            # No run_id: discharge becomes a national overview (union across every
            # basin's latest display-ready run), with a run-less tile template.
            valid_time_sample = national_discharge_valid_times(session)
        elif run_id is not None:
            valid_time_sample = valid_times_for_layer(
                session,
                layer_id,
                run_id=run_id,
                basin_version_id=basin_version_id,
                river_network_version_id=river_network_version_id,
            )
        else:
            valid_time_sample = _empty_valid_times()
        layers.append(
            Layer(
                layer_id=layer_id,
                layer_name=name,
                layer_type=layer_type,
                variables=variables,
                metadata=layer_metadata(
                    layer_id,
                    run_id=run_id,
                    valid_times=valid_time_sample.valid_times,
                    valid_time_limit=valid_time_sample.limit,
                    valid_time_observed_count=valid_time_sample.observed_count,
                    valid_times_truncated=valid_time_sample.truncated,
                    source_version=river_network_source_version if layer_id == "river-network" else source_version,
                    basin_version_id=basin_version_id,
                    river_network_version_id=river_network_version_id,
                    release_blocking=not _mvt_live_postgis_enabled(session),
                    national=national_discharge,
                ),
            )
        )
    return layers


def _empty_valid_times(limit: int = MVT_VALID_TIME_SAMPLE_LIMIT) -> ValidTimeDiscovery:
    return ValidTimeDiscovery(valid_times=[], limit=limit, observed_count=0, truncated=False)


def _validate_supported_hydro_variable(variable: str) -> None:
    if variable in SUPPORTED_HYDRO_MVT_VARIABLES:
        return
    raise ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Unsupported hydrological MVT variable.",
        details={"variable": variable, "supported": list(SUPPORTED_HYDRO_MVT_VARIABLES)},
    )


def _validate_supported_flood_duration(duration: str) -> None:
    if duration in SUPPORTED_FLOOD_RETURN_PERIOD_DURATIONS:
        return
    raise ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Unsupported flood return-period duration.",
        details={"duration": duration, "supported": list(SUPPORTED_FLOOD_RETURN_PERIOD_DURATIONS)},
    )


def _postgis_tile_params(params: dict[str, Any], *, z: int, x: int, y: int) -> dict[str, Any]:
    return {
        **params,
        "z": z,
        "x": x,
        "y": y,
        "feature_limit": FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT,
        "feature_coordinate_limit": FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES,
        "collection_coordinate_limit": FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES,
        "max_coordinate_dimensions": FLOOD_RETURN_PERIOD_MAP_MAX_COORDINATE_DIMENSIONS,
        "extent": MVT_EXTENT,
        "buffer": MVT_BUFFER,
        "simplification_tolerance_m": simplification_tolerance_m(z),
        "encoder_version": MVT_ENCODER_VERSION,
    }


def _time_filter_sql(valid_time: datetime | None, *, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    if valid_time is not None:
        return f"({prefix}valid_time = :valid_time AND {prefix}max_over_window = false)"
    return f"{prefix}max_over_window = :max_over_window"


def _ranking_filters(*, run_id: str, basin_id: str | None, valid_time: datetime | None) -> tuple[str, dict[str, Any]]:
    clauses = [
        "r.run_id = :run_id",
        _time_filter_sql(valid_time, alias="r"),
        "r.quality_flag IN :usable_flags",
    ]
    params: dict[str, Any] = {
        "run_id": run_id,
        "valid_time": valid_time,
        "max_over_window": True,
        "usable_flags": tuple(USABLE_CURVE_FLAGS),
    }
    if basin_id is not None:
        clauses.append(
            """
            EXISTS (
                SELECT 1 FROM core.basin_version bv
                WHERE bv.basin_version_id = r.basin_version_id
                  AND bv.basin_id = :basin_id
            )
            """
        )
        params["basin_id"] = basin_id
    return f"WHERE {' AND '.join(clauses)}", params


def _result_segment_count(
    session: Session,
    run_id: str,
    run: dict[str, Any],
    *,
    valid_time: datetime | None,
) -> int:
    result_count = int(
        session.execute(
            text(
                f"""
                SELECT COUNT(*) AS count
                FROM (
                    SELECT river_network_version_id, river_segment_id
                    FROM flood.return_period_result
                    WHERE run_id = :run_id
                      AND {_time_filter_sql(valid_time)}
                    GROUP BY river_network_version_id, river_segment_id
                ) AS versioned_segments
                """
            ),
            {"run_id": run_id, "valid_time": valid_time, "max_over_window": True},
        )
        .mappings()
        .one()["count"]
    )
    if result_count:
        return result_count
    river_network_version_id = run.get("river_network_version_id")
    if river_network_version_id is None:
        return 0
    return int(
        session.execute(
            text(
                """
                SELECT COUNT(*) AS count
                FROM core.river_segment
                WHERE river_network_version_id = :river_network_version_id
                """
            ),
            {"river_network_version_id": river_network_version_id},
        )
        .mappings()
        .one()["count"]
    )


def _usable_curve_count(session: Session, run_id: str, *, valid_time: datetime | None) -> int:
    return int(
        session.execute(
            text(
                f"""
                SELECT COUNT(*) AS count
                FROM (
                    SELECT river_network_version_id, river_segment_id
                    FROM flood.return_period_result
                    WHERE run_id = :run_id
                      AND {_time_filter_sql(valid_time)}
                      AND quality_flag IN :usable_flags
                    GROUP BY river_network_version_id, river_segment_id
                ) AS versioned_segments
                """
            ).bindparams(bindparam("usable_flags", expanding=True)),
            {
                "run_id": run_id,
                "valid_time": valid_time,
                "max_over_window": True,
                "usable_flags": tuple(USABLE_CURVE_FLAGS),
            },
        )
        .mappings()
        .one()["count"]
    )


def _frequency_thresholds_for_result(session: Session, row: Any) -> FrequencyThresholds | None:
    curve = session.execute(
        text(
            """
            SELECT q2, q5, q10, q20, q50, q100, parameters_json
            FROM flood.flood_frequency_curve
            WHERE model_id = :model_id
              AND river_network_version_id = :river_network_version_id
              AND river_segment_id = :river_segment_id
              AND duration = :duration
              AND quality_flag IN :usable_flags
            ORDER BY sample_period_end DESC
            LIMIT 1
            """
        ).bindparams(bindparam("usable_flags", expanding=True)),
        {
            "model_id": row["model_id"],
            "river_network_version_id": row["river_network_version_id"],
            "river_segment_id": row.get("river_segment_id") or row.get("segment_id"),
            "duration": row.get("duration") or "1h",
            "usable_flags": tuple(USABLE_CURVE_FLAGS),
        },
    ).mappings().first()
    if curve is None:
        return None
    parameters = _json_dict(curve.get("parameters_json"))
    return FrequencyThresholds(
        Q2=_optional_float(curve["q2"]),
        Q5=_optional_float(curve["q5"]),
        Q10=_optional_float(curve["q10"]),
        Q20=_optional_float(curve["q20"]),
        Q50=_optional_float(curve["q50"]),
        Q100=_optional_float(curve["q100"]),
        sample_quality=parameters.get("sample_quality") if isinstance(parameters, dict) else None,
    )


def _parse_threshold(value: str | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return _finite_query_float(float(value), field="threshold", original=value)
    normalized = value.strip().upper()
    if normalized.startswith("Q"):
        normalized = normalized[1:]
    try:
        return _finite_query_float(float(normalized), field="threshold", original=value)
    except ValueError as error:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="threshold must be numeric or a Q-prefixed return period such as Q10.",
            details={"threshold": value},
        ) from error


def _geometry_select_sql(session: Session) -> tuple[str, str]:
    if session.get_bind().dialect.name == "sqlite":
        return "rs.geom", "rs.geom"
    return "ST_AsGeoJSON(rs.geom)::text", "ST_AsGeoJSON(ST_Centroid(rs.geom))::text"


def _flood_return_period_map_sql(session: Session, *, bbox_filter: str) -> str:
    if session.get_bind().dialect.name == "sqlite":
        geom_sql, _centroid_sql = _geometry_select_sql(session)
        return f"""
            SELECT r.river_segment_id, r.basin_version_id, r.river_network_version_id, r.return_period,
                   r.warning_level, r.q_value, r.q_unit, r.quality_flag, {geom_sql} AS geom_json
            FROM flood.return_period_result r
            LEFT JOIN core.river_segment rs
              ON rs.river_segment_id = r.river_segment_id
             AND rs.river_network_version_id = r.river_network_version_id
            WHERE r.run_id = :run_id
              AND r.duration = :duration
              AND r.valid_time = :valid_time
              AND r.max_over_window = :max_over_window
              AND (:return_period IS NULL OR r.return_period >= :return_period)
              {bbox_filter}
            ORDER BY r.river_network_version_id, r.river_segment_id
            LIMIT :query_limit
            """

    return f"""
            WITH matching_segments AS (
                SELECT r.river_segment_id, r.basin_version_id, r.river_network_version_id,
                       r.return_period, r.warning_level, r.q_value, r.q_unit, r.quality_flag,
                       rs.geom,
                       CASE WHEN rs.geom IS NULL THEN NULL ELSE ST_NPoints(rs.geom) END AS coordinate_count,
                       CASE WHEN rs.geom IS NULL THEN NULL ELSE ST_NDims(rs.geom) END AS coordinate_dimensions
                FROM flood.return_period_result r
                LEFT JOIN core.river_segment rs
                  ON rs.river_segment_id = r.river_segment_id
                 AND rs.river_network_version_id = r.river_network_version_id
                WHERE r.run_id = :run_id
                  AND r.duration = :duration
                  AND r.valid_time = :valid_time
                  AND r.max_over_window = :max_over_window
                  AND (:return_period IS NULL OR r.return_period >= :return_period)
                  {bbox_filter}
            ),
            geometry_exclusions AS (
                SELECT
                    COUNT(*) FILTER (WHERE geom IS NULL) AS null_geometry_count,
                    COUNT(*) FILTER (
                        WHERE geom IS NOT NULL AND coordinate_count > :feature_coordinate_limit
                    ) AS feature_coordinate_overflow_count,
                    COALESCE(MAX(coordinate_count) FILTER (
                        WHERE geom IS NOT NULL AND coordinate_count > :feature_coordinate_limit
                    ), 0) AS feature_coordinate_count,
                    COUNT(*) FILTER (
                        WHERE geom IS NOT NULL AND coordinate_count < 2
                    ) AS malformed_geometry_count,
                    COALESCE(MIN(coordinate_count) FILTER (
                        WHERE geom IS NOT NULL AND coordinate_count < 2
                    ), 0) AS malformed_coordinate_count,
                    COUNT(*) FILTER (
                        WHERE geom IS NOT NULL AND coordinate_dimensions > :max_coordinate_dimensions
                    ) AS dimension_overflow_count,
                    COALESCE(MAX(coordinate_dimensions) FILTER (
                        WHERE geom IS NOT NULL AND coordinate_dimensions > :max_coordinate_dimensions
                    ), 0) AS dimension_count
                FROM matching_segments
            ),
            eligible_segments AS (
                SELECT *
                FROM matching_segments
                WHERE geom IS NOT NULL
                  AND coordinate_count BETWEEN 2 AND :feature_coordinate_limit
                  AND coordinate_dimensions <= :max_coordinate_dimensions
            ),
            budgeted_segments AS (
                SELECT *,
                       SUM(coordinate_count) OVER (
                           ORDER BY river_network_version_id, river_segment_id
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS running_coordinate_count
                FROM eligible_segments
            ),
            overflow AS (
                SELECT
                    COALESCE(MAX(running_coordinate_count), 0::bigint) > :collection_coordinate_limit
                        AS collection_overflow,
                    COALESCE(MAX(running_coordinate_count), 0::bigint) AS collection_coordinate_count
                FROM budgeted_segments
            ),
            bounded_segments AS (
                SELECT *
                FROM budgeted_segments
                WHERE running_coordinate_count <= :collection_coordinate_limit
            )
            SELECT river_segment_id::text AS river_segment_id,
                   basin_version_id::text AS basin_version_id,
                   river_network_version_id::text AS river_network_version_id,
                   return_period::double precision AS return_period,
                   warning_level::text AS warning_level,
                   q_value::double precision AS q_value,
                   q_unit::text AS q_unit,
                   quality_flag::text AS quality_flag,
                   ST_AsGeoJSON(geom)::text AS geom_json,
                   false::boolean AS collection_overflow,
                   (SELECT collection_coordinate_count FROM overflow)::bigint AS collection_coordinate_count,
                   NULL::text AS geometry_limit_type,
                   NULL::bigint AS geometry_feature_count,
                   NULL::bigint AS geometry_coordinate_count,
                   NULL::integer AS geometry_dimension_count
            FROM bounded_segments
            WHERE NOT EXISTS (
                SELECT 1
                FROM geometry_exclusions
                WHERE null_geometry_count > 0
                   OR feature_coordinate_overflow_count > 0
                   OR malformed_geometry_count > 0
                   OR dimension_overflow_count > 0
            )
            UNION ALL
            SELECT NULL::text AS river_segment_id,
                   NULL::text AS basin_version_id,
                   NULL::text AS river_network_version_id,
                   NULL::double precision AS return_period,
                   NULL::text AS warning_level,
                   NULL::double precision AS q_value,
                   NULL::text AS q_unit,
                   NULL::text AS quality_flag,
                   NULL::text AS geom_json,
                   true::boolean AS collection_overflow,
                   collection_coordinate_count::bigint AS collection_coordinate_count,
                   NULL::text AS geometry_limit_type,
                   NULL::bigint AS geometry_feature_count,
                   NULL::bigint AS geometry_coordinate_count,
                   NULL::integer AS geometry_dimension_count
            FROM overflow
            WHERE collection_overflow
              AND NOT EXISTS (
                  SELECT 1
                  FROM geometry_exclusions
                  WHERE null_geometry_count > 0
                     OR feature_coordinate_overflow_count > 0
                     OR malformed_geometry_count > 0
                     OR dimension_overflow_count > 0
              )
            UNION ALL
            SELECT NULL::text AS river_segment_id,
                   NULL::text AS basin_version_id,
                   NULL::text AS river_network_version_id,
                   NULL::double precision AS return_period,
                   NULL::text AS warning_level,
                   NULL::double precision AS q_value,
                   NULL::text AS q_unit,
                   NULL::text AS quality_flag,
                   NULL::text AS geom_json,
                   false::boolean AS collection_overflow,
                   NULL::bigint AS collection_coordinate_count,
                   'feature_coordinates'::text AS geometry_limit_type,
                   feature_coordinate_overflow_count::bigint AS geometry_feature_count,
                   feature_coordinate_count::bigint AS geometry_coordinate_count,
                   NULL::integer AS geometry_dimension_count
            FROM geometry_exclusions
            WHERE feature_coordinate_overflow_count > 0
            UNION ALL
            SELECT NULL::text AS river_segment_id,
                   NULL::text AS basin_version_id,
                   NULL::text AS river_network_version_id,
                   NULL::double precision AS return_period,
                   NULL::text AS warning_level,
                   NULL::double precision AS q_value,
                   NULL::text AS q_unit,
                   NULL::text AS quality_flag,
                   NULL::text AS geom_json,
                   false::boolean AS collection_overflow,
                   NULL::bigint AS collection_coordinate_count,
                   'coordinate_dimensions'::text AS geometry_limit_type,
                   dimension_overflow_count::bigint AS geometry_feature_count,
                   NULL::bigint AS geometry_coordinate_count,
                   dimension_count::integer AS geometry_dimension_count
            FROM geometry_exclusions
            WHERE dimension_overflow_count > 0
            UNION ALL
            SELECT NULL::text AS river_segment_id,
                   NULL::text AS basin_version_id,
                   NULL::text AS river_network_version_id,
                   NULL::double precision AS return_period,
                   NULL::text AS warning_level,
                   NULL::double precision AS q_value,
                   NULL::text AS q_unit,
                   NULL::text AS quality_flag,
                   NULL::text AS geom_json,
                   false::boolean AS collection_overflow,
                   NULL::bigint AS collection_coordinate_count,
                   'malformed_geometry'::text AS geometry_limit_type,
                   malformed_geometry_count::bigint AS geometry_feature_count,
                   malformed_coordinate_count::bigint AS geometry_coordinate_count,
                   NULL::integer AS geometry_dimension_count
            FROM geometry_exclusions
            WHERE malformed_geometry_count > 0
            UNION ALL
            SELECT NULL::text AS river_segment_id,
                   NULL::text AS basin_version_id,
                   NULL::text AS river_network_version_id,
                   NULL::double precision AS return_period,
                   NULL::text AS warning_level,
                   NULL::double precision AS q_value,
                   NULL::text AS q_unit,
                   NULL::text AS quality_flag,
                   NULL::text AS geom_json,
                   false::boolean AS collection_overflow,
                   NULL::bigint AS collection_coordinate_count,
                   'null_geometry'::text AS geometry_limit_type,
                   null_geometry_count::bigint AS geometry_feature_count,
                   NULL::bigint AS geometry_coordinate_count,
                   NULL::integer AS geometry_dimension_count
            FROM geometry_exclusions
            WHERE null_geometry_count > 0
            ORDER BY
                geometry_limit_type NULLS LAST,
                collection_overflow DESC,
                river_network_version_id,
                river_segment_id
            LIMIT :query_limit
            """


def _flood_return_period_feature_id(row: Any) -> str:
    return f"{row['river_network_version_id']}::{row['river_segment_id']}"


def _centroid_payload(value: Any) -> GeoPoint | None:
    geometry = _geojson_geometry(value)
    if geometry is None:
        return None
    if geometry.get("type") == "Point":
        coordinates = geometry.get("coordinates") or []
        return GeoPoint(coordinates=[float(coordinates[0]), float(coordinates[1])])
    if geometry.get("type") == "LineString":
        coordinates = geometry.get("coordinates") or []
        if not coordinates:
            return None
        lon = sum(float(point[0]) for point in coordinates) / len(coordinates)
        lat = sum(float(point[1]) for point in coordinates) / len(coordinates)
        return GeoPoint(coordinates=[lon, lat])
    return None


def _geojson_geometry(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("{"):
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else None
    return None


def _enforce_flood_return_period_geojson_budget(payload: TileFeatureCollection) -> None:
    total_coordinates = 0
    for feature in payload.features:
        coordinate_count = _geojson_coordinate_count(feature.geometry)
        if coordinate_count > FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES:
            raise ApiError(
                status_code=413,
                code="FLOOD_RETURN_PERIOD_GEOJSON_BUDGET_EXCEEDED",
                message="Flood return-period GeoJSON geometry budget exceeded; provide a bbox.",
                details={
                    "limit_type": "feature_coordinates",
                    "max_coordinates": FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES,
                    "coordinate_count": coordinate_count,
                },
            )
        total_coordinates += coordinate_count
        if total_coordinates > FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES:
            raise ApiError(
                status_code=413,
                code="FLOOD_RETURN_PERIOD_GEOJSON_BUDGET_EXCEEDED",
                message="Flood return-period GeoJSON geometry budget exceeded; provide a bbox.",
                details={
                    "limit_type": "collection_coordinates",
                    "max_coordinates": FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES,
                    "coordinate_count": total_coordinates,
                },
            )

    serialized_bytes = len(json.dumps(payload.model_dump(), separators=(",", ":")).encode("utf-8"))
    if serialized_bytes > FLOOD_RETURN_PERIOD_MAP_MAX_SERIALIZED_BYTES:
        raise ApiError(
            status_code=413,
            code="FLOOD_RETURN_PERIOD_GEOJSON_BUDGET_EXCEEDED",
            message="Flood return-period GeoJSON payload budget exceeded; provide a bbox or lower the result size.",
            details={
                "limit_type": "serialized_bytes",
                "max_bytes": FLOOD_RETURN_PERIOD_MAP_MAX_SERIALIZED_BYTES,
                "serialized_bytes": serialized_bytes,
            },
        )


def _geojson_coordinate_count(geometry: dict[str, Any] | None) -> int:
    if geometry is None:
        return 0
    return _coordinate_count(geometry.get("coordinates"))


def _coordinate_count(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    if len(value) >= 2 and all(isinstance(item, int | float) for item in value[:2]):
        return 1
    return sum(_coordinate_count(item) for item in value)


def _segment_name(value: Any) -> str | None:
    properties = _json_dict(value)
    for key in ("name", "segment_name", "display_name"):
        candidate = properties.get(key)
        if candidate:
            return str(candidate)
    return None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _split_csv(value: str | None) -> list[str]:
    if value is None:
        return []
    return [token.strip() for token in value.split(",") if token.strip()]


def _parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="bbox must contain minLon,minLat,maxLon,maxLat.",
            details={"bbox": value},
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(part) for part in parts)
    except ValueError as error:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="bbox values must be numeric.",
            details={"bbox": value},
        ) from error
    if not all(math.isfinite(coordinate) for coordinate in (min_lon, min_lat, max_lon, max_lat)):
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="bbox values must be finite numbers.",
            details={"bbox": value},
        )
    if min_lon > max_lon or min_lat > max_lat:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="bbox minimum coordinates must not exceed maximum coordinates.",
            details={"bbox": value},
        )
    return min_lon, min_lat, max_lon, max_lat


def _bbox_params(bounds: tuple[float, float, float, float]) -> dict[str, float]:
    min_lon, min_lat, max_lon, max_lat = bounds
    return {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat}


def _optional_float(value: Any) -> float | None:
    return _finite_result_float(value, field="numeric result") if value is not None else None


def _finite_query_float(value: Any, *, field: str, original: Any) -> float:
    numeric = float(value)
    if math.isfinite(numeric):
        return numeric
    raise ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message=f"{field} must be a finite number.",
        details={field: original},
    )


def _finite_result_float(value: Any, *, field: str) -> float:
    numeric = float(value)
    if math.isfinite(numeric):
        return numeric
    raise ApiError(
        status_code=500,
        code="NON_FINITE_NUMERIC_RESULT",
        message=f"{field} is not finite.",
    )


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _format_time(value: Any) -> str:
    formatted = canonical_mvt_time(value)
    return "None" if formatted is None else formatted
