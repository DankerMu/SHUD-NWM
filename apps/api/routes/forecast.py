from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from apps.api.errors import ApiError
from packages.common.forecast_store import ForecastStoreError, PsycopgForecastStore

router = APIRouter(prefix="/api/v1", tags=["forecast"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def get_forecast_store() -> PsycopgForecastStore:
    try:
        return PsycopgForecastStore.from_env()
    except ForecastStoreError as error:
        raise _api_error(error) from error


@router.get("/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series")
def get_forecast_series(
    basin_version_id: str,
    segment_id: str,
    issue_time: str = Query(default="latest"),
    variables: str = Query(default="q_down"),
    scenarios: str = Query(default="GFS"),
    store: PsycopgForecastStore = Depends(get_forecast_store),
) -> dict[str, Any]:
    try:
        return store.forecast_series(
            basin_version_id=basin_version_id,
            segment_id=segment_id,
            issue_time=issue_time,
            variables=_split_query_list(variables),
            scenarios=_split_query_list(scenarios),
        )
    except ForecastStoreError as error:
        raise _api_error(error) from error


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    store: PsycopgForecastStore = Depends(get_forecast_store),
) -> dict[str, Any]:
    try:
        return store.get_run(run_id)
    except ForecastStoreError as error:
        raise _api_error(error) from error


@router.get("/runs")
def list_runs(
    basin_id: str | None = None,
    source: str | None = None,
    cycle_time: datetime | None = None,
    status: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
    store: PsycopgForecastStore = Depends(get_forecast_store),
) -> dict[str, Any]:
    capped_limit = min(limit, MAX_LIMIT)
    try:
        return store.list_runs(
            basin_id=basin_id,
            source=source,
            cycle_time=cycle_time,
            status=status,
            limit=capped_limit,
            offset=offset,
        )
    except ForecastStoreError as error:
        raise _api_error(error) from error


def _split_query_list(value: str) -> list[str]:
    return [token.strip() for token in value.split(",") if token.strip()]


def _api_error(error: ForecastStoreError) -> ApiError:
    return ApiError(
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        details=error.details,
    )
