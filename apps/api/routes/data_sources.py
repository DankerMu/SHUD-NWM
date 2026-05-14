from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from apps.api.errors import ApiError
from apps.api.routes.forecast import DEFAULT_LIMIT, MAX_LIMIT, _ok
from packages.common.forecast_store import ForecastStoreError, PsycopgForecastStore

router = APIRouter(prefix="/api/v1", tags=["data-sources"])


def get_data_source_store() -> PsycopgForecastStore:
    try:
        return PsycopgForecastStore.from_env()
    except ForecastStoreError as error:
        raise _api_error(error) from error


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
                limit=min(limit, MAX_LIMIT),
                offset=offset,
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
