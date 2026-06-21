from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from apps.api.errors import ApiError
from apps.api.routes.forecast import DEFAULT_LIMIT, MAX_LIMIT, _ok
from packages.common.forecast_store import (
    MAX_STATION_SERIES_LIMIT,
    ForecastStoreError,
    PsycopgForecastStore,
)
from packages.common.object_store_forcing import (
    PsycopgStationLookup,
    StationLookup,
    read_station_forcing_csv,
)

router = APIRouter(prefix="/api/v1", tags=["data-sources"])


def get_data_source_store() -> PsycopgForecastStore:
    try:
        return PsycopgForecastStore.from_env()
    except ForecastStoreError as error:
        raise _api_error(error) from error


def get_station_lookup() -> StationLookup:
    try:
        return PsycopgStationLookup.from_env()
    except ForecastStoreError as error:
        raise _api_error(error) from error


def get_object_store_root(request: Request) -> Path:
    object_store_root = getattr(request.app.state, "object_store_root", None)
    if object_store_root is None:
        raise ApiError(
            status_code=500,
            code="OBJECT_STORE_ROOT_NOT_CONFIGURED",
            message="OBJECT_STORE_ROOT is not configured for station forcing reads.",
            details=None,
        )
    return Path(object_store_root)


@router.get("/data-sources")
def list_data_sources(
    request: Request,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
    store: PsycopgForecastStore = Depends(get_data_source_store),
) -> dict[str, Any]:
    try:
        return _ok(request, store.list_data_sources(limit=min(limit, MAX_LIMIT), offset=offset))
    except ForecastStoreError as error:
        raise _api_error(error) from error


@router.get("/data-sources/{source_id}/cycles")
def list_cycles(
    request: Request,
    source_id: str,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    status: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
    store: PsycopgForecastStore = Depends(get_data_source_store),
) -> dict[str, Any]:
    try:
        return _ok(
            request,
            store.list_cycles(
                source_id=source_id,
                from_time=from_time,
                to_time=to_time,
                status=status,
                limit=min(limit, MAX_LIMIT),
                offset=offset,
            ),
        )
    except ForecastStoreError as error:
        raise _api_error(error) from error


@router.get("/met/stations")
def list_met_stations(
    request: Request,
    basin_version_id: str | None = None,
    model_id: str | None = None,
    search: str | None = Query(
        default=None,
        description="Case-insensitive substring match over station_id and station name (backend-applied).",
    ),
    variables: list[str] | None = Query(
        default=None,
        description=(
            "Variable coverage filter. Repeat or comma-separate values "
            "(PRCP, TEMP, RH, wind, Rn, Press). Applied only when model_id is set; "
            "otherwise reported unavailable in the response filters block."
        ),
    ),
    qc_status: str | None = Query(
        default=None,
        description=(
            "QC status filter (advisory). Reported unavailable in the response "
            "filters block because QC fields are not present on the station inventory."
        ),
    ),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
    store: PsycopgForecastStore = Depends(get_data_source_store),
) -> dict[str, Any]:
    try:
        return _ok(
            request,
            store.list_met_stations(
                basin_version_id=basin_version_id,
                model_id=model_id,
                search=search,
                variables=variables,
                qc_status=qc_status,
                limit=min(limit, MAX_LIMIT),
                offset=offset,
            ),
        )
    except ForecastStoreError as error:
        raise _api_error(error) from error


@router.get("/met/stations/{station_id}/series", operation_id="getMetStationSeries")
def get_met_station_series(
    request: Request,
    station_id: str,
    forcing_version_id: str | None = Query(default=None, min_length=1),
    model_id: str | None = Query(default=None, min_length=1),
    source_id: str | None = Query(default=None, min_length=1),
    cycle_time: datetime | None = Query(default=None),
    variables: str | list[str] | None = Query(
        default=None,
        description=(
            "Station forcing variables. Repeat the parameter or provide comma-separated values. "
            "Public station-series variables are PRCP, TEMP, RH, wind, and Rn."
        ),
    ),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int | None = Query(default=None, ge=1, le=MAX_STATION_SERIES_LIMIT),
    station_lookup: StationLookup = Depends(get_station_lookup),
    object_store_root: Path = Depends(get_object_store_root),
) -> dict[str, Any]:
    try:
        return _ok(
            request,
            read_station_forcing_csv(
                station_lookup=station_lookup,
                object_store_root=object_store_root,
                station_id=station_id,
                model_id=model_id,
                source_id=source_id,
                cycle_time=cycle_time,
                variables=variables,
                from_time=from_time,
                to_time=to_time,
                limit=limit,
            ),
        )
    except ForecastStoreError as error:
        raise _api_error(error) from error


def _api_error(error: ForecastStoreError) -> ApiError:
    return ApiError(
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        details=error.details,
    )
