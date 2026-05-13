from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request

from apps.api.errors import ApiError
from packages.common.forecast_store import ForecastStoreError, PsycopgForecastStore

router = APIRouter(prefix="/api/v1", tags=["forecast"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_HINDCAST_ACCESS_ROLES = {"analyst", "operator", "model_admin", "sys_admin"}


def get_forecast_store() -> PsycopgForecastStore:
    try:
        return PsycopgForecastStore.from_env()
    except ForecastStoreError as error:
        raise _api_error(error) from error


@router.get("/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series")
def get_forecast_series(
    request: Request,
    basin_version_id: str,
    segment_id: str,
    issue_time: str = Query(default="latest"),
    variables: str = Query(default="q_down"),
    scenarios: str = Query(default="GFS"),
    include_analysis: bool = Query(default=False),
    run_types: str | None = Query(default=None),
    store: PsycopgForecastStore = Depends(get_forecast_store),
) -> dict[str, Any]:
    run_type_tokens = _split_query_list(run_types) if run_types is not None else None
    if run_type_tokens is not None and "hindcast" in {token.lower() for token in run_type_tokens}:
        _require_hindcast_access_role(request)
    try:
        return store.forecast_series(
            basin_version_id=basin_version_id,
            segment_id=segment_id,
            issue_time=issue_time,
            variables=_split_query_list(variables),
            scenarios=_split_query_list(scenarios),
            include_analysis=include_analysis,
            run_types=run_type_tokens,
        )
    except ForecastStoreError as error:
        raise _api_error(error) from error


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    request: Request,
    store: PsycopgForecastStore = Depends(get_forecast_store),
) -> dict[str, Any]:
    try:
        return _ok(request, store.get_run(run_id))
    except ForecastStoreError as error:
        raise _api_error(error) from error


@router.get("/runs")
def list_runs(
    request: Request,
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
        page = store.list_runs(
            basin_id=basin_id,
            source=source,
            cycle_time=cycle_time,
            status=status,
            limit=capped_limit,
            offset=offset,
        )
        return _ok(request, _paginated_payload(page))
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


def _ok(request: Request, data: Any) -> dict[str, Any]:
    return {
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
        "status": "ok",
        "data": data,
    }


def _paginated_payload(page: dict[str, Any]) -> dict[str, Any]:
    total = int(page.get("total", page.get("total_count", 0)) or 0)
    return {
        **page,
        "total": total,
        "total_count": total,
    }


def _require_hindcast_access_role(request: Request) -> None:
    role = request.headers.get("X-User-Role")
    if role is not None and role.strip().lower() in _HINDCAST_ACCESS_ROLES:
        return
    raise ApiError(
        status_code=403,
        code="PERMISSION_DENIED",
        message="Analyst, operator, or admin role required for hindcast series.",
    )
