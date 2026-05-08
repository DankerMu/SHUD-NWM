from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from fastapi import APIRouter, Depends, Query

from apps.api.errors import ApiError
from packages.common.best_available import BestAvailableError, BestAvailableManager

router = APIRouter(prefix="/api/v1", tags=["best-available"])


def get_best_available_manager() -> BestAvailableManager:
    try:
        return BestAvailableManager.from_env()
    except BestAvailableError as error:
        raise _api_error(error) from error


@router.get("/met/best-available")
def list_best_available(
    from_value: str = Query(alias="from"),
    to_value: str = Query(alias="to"),
    variable: str | None = None,
    manager: BestAvailableManager = Depends(get_best_available_manager),
) -> list[dict[str, Any]]:
    try:
        return manager.list_selections(
            from_time=_parse_query_time(from_value, end_of_day=False),
            to_time=_parse_query_time(to_value, end_of_day=True),
            variable=variable,
        )
    except BestAvailableError as error:
        raise _api_error(error) from error


def _parse_query_time(value: str, *, end_of_day: bool) -> datetime:
    try:
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            parsed_date = date.fromisoformat(value)
            boundary = time.max if end_of_day else time.min
            return datetime.combine(parsed_date, boundary, tzinfo=UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError as error:
        raise BestAvailableError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="from and to must be YYYY-MM-DD dates or ISO 8601 timestamps.",
            details={"rejected_value": value},
        ) from error


def _api_error(error: BestAvailableError) -> ApiError:
    return ApiError(
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        details=error.details,
    )
