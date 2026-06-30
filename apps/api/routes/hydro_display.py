from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Generator
from datetime import datetime
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.display_cache import display_catalog_cached
from apps.api.errors import ApiError
from apps.api.routes.pipeline import _ok
from services.tiles.mvt import (
    MVT_BUFFER,
    MVT_EXTENT,
    MVT_MAX_COORDINATES,
    MVT_MAX_FEATURES,
    MVT_MAX_TILE_COORDINATE,
    MVT_MAX_ZOOM,
    MVT_MEDIA_TYPE,
    MVT_SCHEMA_VERSION,
    MVT_VALID_TIME_SAMPLE_LIMIT,
    SUPPORTED_HYDRO_MVT_VARIABLES,
    TileError,
    TileInput,
    TileResponse,
    ValidTimeDiscovery,
    canonical_mvt_time,
    display_ready_run,
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
    read_cached_tile_response as _read_cached_tile_response,
)
from services.tiles.mvt import (
    validate_identifier as _validate_tile_identifier,
)
from services.tiles.mvt import (
    validate_xyz as _validate_tile_xyz,
)

router = APIRouter(tags=["hydro-display"])

HYDRO_NATIONAL_SOURCE_ID = "hydro-national"
HYDRO_NATIONAL_SOURCE_VERSION = "hydro-national-latest-per-basin"
DISPLAY_PRODUCT_READY_STATUSES = {"succeeded", "parsed", "published"}
PUBLIC_LAYER_DEFINITIONS: tuple[tuple[str, str, str, list[str]], ...] = (
    ("discharge", "Discharge", "hydrology", ["q_down"]),
    ("river-network", "River network", "base", ["geometry"]),
    ("met-stations", "Meteorological stations", "base", ["station_point"]),
)
SUPPORTED_PUBLIC_LAYER_IDS = frozenset(definition[0] for definition in PUBLIC_LAYER_DEFINITIONS)
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
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    },
    "4XX": {"description": "MVT request validation error."},
    "5XX": {"description": "MVT server error."},
}
TILE_X_DESCRIPTION = (
    f"Web Mercator XYZ tile column. Global schema bounds are 0..{MVT_MAX_TILE_COORDINATE} "
    f"for max zoom {MVT_MAX_ZOOM}; each request also enforces 0 <= x < 2^z."
)
TILE_Y_DESCRIPTION = (
    f"Web Mercator XYZ tile row. Global schema bounds are 0..{MVT_MAX_TILE_COORDINATE} "
    f"for max zoom {MVT_MAX_ZOOM}; each request also enforces 0 <= y < 2^z."
)


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


def get_hydro_display_session() -> Generator[Session, None, None]:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ApiError(
            status_code=500,
            code="DATABASE_URL_MISSING",
            message="DATABASE_URL is required for hydro display API operations.",
        )
    with Session(_engine(database_url)) as session:
        yield session


def _tile_api_error(exc: TileError) -> ApiError:
    return ApiError(status_code=exc.status_code, code=exc.code, message=exc.message, details=exc.details)


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


@router.get("/api/v1/layers", response_model=LayerListResponse)
def list_layers(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    run_id: str | None = Query(default=None),
    session: Session = Depends(get_hydro_display_session),
) -> dict[str, Any]:
    if run_id is not None:
        validate_identifier(run_id, "run_id")

    def _load() -> list[dict[str, Any]]:
        run = _require_display_ready(session, run_id) if run_id is not None else display_ready_run(session)
        if run is None:
            return []
        resolved_run_id = str(run["run_id"])
        basin_version_id, river_network_version_id = _require_run_source_identity(run, layer_id="layers")
        source_version = _run_source_version(run)
        river_network_source_version = _river_network_source_version(session, basin_version_id)
        layers = _default_layer_catalog(
            session,
            run_id=resolved_run_id,
            source_version=source_version,
            river_network_source_version=river_network_source_version,
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
            national=run_id is None,
        )
        return [layer.model_dump() for layer in layers[offset : offset + limit]]

    return _ok(request, display_catalog_cached(request, f"layers:{run_id}:{limit}:{offset}", _load))


@router.get("/api/v1/layers/{layer_id}/valid-times", response_model=LayerValidTimesResponse)
def list_layer_valid_times(
    request: Request,
    layer_id: str,
    run_id: str | None = Query(default=None),
    session: Session = Depends(get_hydro_display_session),
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
            return national_discharge_valid_times(session).model_dump()
        if layer_id != "discharge":
            return _empty_valid_times().model_dump()
        if run_id is not None:
            validate_identifier(run_id, "run_id")
            run = _require_display_ready(session, run_id)
        else:
            run = display_ready_run(session)
            if run is None:
                return _empty_valid_times().model_dump()
            run_id = str(run["run_id"])
        basin_version_id, river_network_version_id = _require_run_source_identity(run, layer_id=layer_id)
        valid_time_sample = valid_times_for_layer(
            session,
            layer_id,
            run_id=run_id,
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
        )
        return valid_time_sample.model_dump()

    return _ok(request, display_catalog_cached(request, f"valid-times:{layer_id}:{requested_run_id}", _load))


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
    session: Session = Depends(get_hydro_display_session),
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
    tile_input = TileInput(
        layer_id=public_hydro_layer_id(variable),
        source_id=run_id,
        source_version=_run_source_version(run),
        valid_time=_format_time(valid_time),
        z=z,
        x=x,
        y=y,
        variant_id=f"variable:{variable}",
    )
    cached = read_cached_tile_response(session, tile_input)
    if cached is not None:
        return _mvt_response(cached)
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
    session: Session = Depends(get_hydro_display_session),
) -> Response:
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
    data = _fetch_hydro_national_mvt_tile_bytes(session, variable=variable, valid_time=valid_time, z=z, x=x, y=y)
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
    session: Session = Depends(get_hydro_display_session),
) -> Response:
    validate_identifier(basin_version_id, "basin_version_id")
    validate_xyz(z, x, y)
    tile_input = TileInput(
        layer_id="river-network",
        source_id=basin_version_id,
        source_version=_river_network_source_version(session, basin_version_id),
        valid_time=None,
        z=z,
        x=x,
        y=y,
    )
    cached = read_cached_tile_response(session, tile_input)
    if cached is not None:
        return _mvt_response(cached)
    data = _fetch_river_network_mvt_tile_bytes(session, basin_version_id=basin_version_id, z=z, x=x, y=y)
    return _mvt_response(build_raw_tile_response(session, tile_input, data))


@router.get(
    "/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
    operation_id="getMetStationTile",
)
def met_station_mvt_tile(
    basin_version_id: str,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_hydro_display_session),
) -> Response:
    validate_identifier(basin_version_id, "basin_version_id")
    validate_xyz(z, x, y)
    tile_input = TileInput(
        layer_id="met-stations",
        source_id=basin_version_id,
        source_version=_station_source_version(session, basin_version_id),
        valid_time=None,
        z=z,
        x=x,
        y=y,
    )
    cached = read_cached_tile_response(session, tile_input)
    if cached is not None:
        return _mvt_response(cached)
    data = _fetch_station_mvt_tile_bytes(session, basin_version_id=basin_version_id, z=z, x=x, y=y)
    return _mvt_response(build_raw_tile_response(session, tile_input, data))


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
        details={"layer_id": layer_id, "required_env": "NHMS_ENABLE_LIVE_POSTGIS_MVT=true"},
    )


def _fetch_postgis_tile_bytes(session: Session, layer: str, params: dict[str, Any], *, z: int, x: int, y: int) -> bytes:
    _require_live_postgis_mvt(session, layer)
    detail_layer_id = (
        public_hydro_layer_id(str(params["variable"]))
        if layer in {"hydro", "hydro-national"} and "variable" in params
        else layer
    )
    row = session.execute(text(postgis_tile_sql(layer)), _postgis_tile_params(params, z=z, x=x, y=y)).mappings().first()
    feature_count = int(row.get("feature_count") or 0) if row else 0
    coordinate_count = int(row.get("coordinate_count") or 0) if row else 0
    source_identity_count = int(row.get("source_identity_count") or 0) if row else 0
    invalid_property_count = int(row.get("invalid_property_count") or 0) if row else 0
    if invalid_property_count > 0:
        raise ApiError(
            status_code=500,
            code="MVT_TILE_CONTRACT_INVALID",
            message="Live PostGIS MVT tile source rows violate the public tile contract.",
            details={
                "layer_id": detail_layer_id,
                "z": z,
                "x": x,
                "y": y,
                "invalid_property_count": invalid_property_count,
                "properties": _mvt_invalid_properties(row.get("invalid_properties") if row else None),
            },
        )
    if feature_count > MVT_MAX_FEATURES or coordinate_count > MVT_MAX_COORDINATES:
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
                "max_features": MVT_MAX_FEATURES,
                "coordinate_count": coordinate_count,
                "max_coordinates": MVT_MAX_COORDINATES,
            },
        )
    if not row or source_identity_count <= 0:
        raise ApiError(
            status_code=424,
            code="MVT_LIVE_POSTGIS_UNAVAILABLE",
            message="Live PostGIS MVT query returned no source rows for the requested identity.",
            details={"layer_id": detail_layer_id, "z": z, "x": x, "y": y},
        )
    return bytes(row["tile"] or b"")


def _mvt_invalid_properties(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [item for item in str(value).split(",") if item]


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
    return _fetch_postgis_tile_bytes(session, "river-network", {"basin_version_id": basin_version_id}, z=z, x=x, y=y)


def _fetch_station_mvt_tile_bytes(
    session: Session,
    *,
    basin_version_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    return _fetch_postgis_tile_bytes(session, "met-stations", {"basin_version_id": basin_version_id}, z=z, x=x, y=y)


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
        row_limit = MVT_MAX_FEATURES + 1
        if session.get_bind().dialect.name == "sqlite":
            rows = session.execute(
                text(
                    """
                    SELECT station_id, basin_version_id, COALESCE(station_name, '') AS station_name,
                           station_role, active_flag, geom, created_at
                    FROM met.met_station
                    WHERE basin_version_id = :basin_version_id
                      AND active_flag = 1
                    ORDER BY station_id
                    LIMIT :limit
                    """
                ),
                {"basin_version_id": basin_version_id, "limit": row_limit},
            ).mappings().all()
        else:
            rows = session.execute(
                text(
                    """
                    SELECT station_id, basin_version_id, COALESCE(station_name, '') AS station_name,
                           station_role, active_flag, encode(ST_AsEWKB(geom), 'hex') AS geom, created_at
                    FROM met.met_station
                    WHERE basin_version_id = :basin_version_id
                      AND active_flag = true
                    ORDER BY station_id
                    LIMIT :limit
                    """
                ),
                {"basin_version_id": basin_version_id, "limit": row_limit},
            ).mappings().all()
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
        raise ApiError(
            status_code=404,
            code="MVT_SOURCE_IDENTITY_NOT_FOUND",
            message="Station MVT source identity was not found for the requested basin version.",
            details={"layer_id": "met-stations", "basin_version_id": basin_version_id},
        )
    if len(rows) > MVT_MAX_FEATURES:
        raise ApiError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Station MVT source inventory exceeded the configured feature budget.",
            details={"layer_id": "met-stations", "basin_version_id": basin_version_id},
        )
    basis = {
        "rows": [
            [
                row.get("station_id"),
                row.get("basin_version_id"),
                row.get("station_name"),
                row.get("station_role"),
                _station_active_flag(row.get("active_flag")),
                row.get("geom"),
                _format_time(row.get("created_at")) if row.get("created_at") is not None else None,
            ]
            for row in rows
        ],
    }
    digest = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"met-stations:{digest}:{basin_version_id}:{len(rows)}"


def _station_active_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "t", "true", "yes"}
    return bool(value)


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
        "cycle_time": _format_time(run.get("cycle_time")) if run.get("cycle_time") is not None else None,
        "river_network_version_id": run.get("river_network_version_id"),
        "run_id": run.get("run_id"),
        "source_id": run.get("source_id"),
        "status": run.get("status"),
        "updated_at": _format_time(run.get("updated_at")) if run.get("updated_at") is not None else None,
    }
    digest = hashlib.sha256(
        json.dumps(revision_basis, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"{base_version};run-revision:{digest}"


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
    run_id: str,
    source_version: str,
    basin_version_id: str,
    river_network_version_id: str,
    river_network_source_version: str,
    national: bool = False,
) -> list[Layer]:
    layers = []
    for layer_id, name, layer_type, variables in PUBLIC_LAYER_DEFINITIONS:
        if layer_id == "discharge":
            valid_time_sample = national_discharge_valid_times(session)
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
                    source_version=(
                        river_network_source_version
                        if layer_id in {"river-network", "met-stations"}
                        else source_version
                    ),
                    basin_version_id=basin_version_id,
                    river_network_version_id=river_network_version_id,
                    release_blocking=not _mvt_live_postgis_enabled(session),
                    national=layer_id == "discharge",
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


def _postgis_tile_params(params: dict[str, Any], *, z: int, x: int, y: int) -> dict[str, Any]:
    return {
        **params,
        "z": z,
        "x": x,
        "y": y,
        "feature_limit": MVT_MAX_FEATURES,
        "feature_coordinate_limit": MVT_MAX_COORDINATES,
        "collection_coordinate_limit": MVT_MAX_COORDINATES,
        "max_coordinate_dimensions": 3,
        "extent": MVT_EXTENT,
        "buffer": MVT_BUFFER,
    }


def _format_time(value: Any) -> str:
    return canonical_mvt_time(value) or str(value)
