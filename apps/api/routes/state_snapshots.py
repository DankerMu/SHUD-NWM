from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from apps.api.errors import ApiError
from apps.api.routes.forecast import DEFAULT_LIMIT, MAX_LIMIT
from packages.common.state_manager import StateManager, StateManagerError, state_snapshot_to_dict

router = APIRouter(prefix="/api/v1", tags=["state-snapshots"])


def get_state_manager() -> StateManager:
    try:
        return StateManager.from_env()
    except StateManagerError as error:
        raise _api_error(500, "STATE_MANAGER_UNAVAILABLE", str(error)) from error


@router.get("/state-snapshots")
def list_state_snapshots(
    model_id: str | None = None,
    usable: bool | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    manager: StateManager = Depends(get_state_manager),
) -> dict[str, Any]:
    try:
        return manager.list_state_snapshots(
            model_id=model_id,
            usable=usable,
            limit=min(limit, MAX_LIMIT),
            offset=offset,
        )
    except StateManagerError as error:
        raise _api_error(500, "STATE_MANAGER_ERROR", str(error)) from error


@router.get("/state-snapshots/{state_id}")
def get_state_snapshot(
    state_id: str,
    manager: StateManager = Depends(get_state_manager),
) -> dict[str, Any]:
    try:
        snapshot = manager.get_state_snapshot(state_id)
    except StateManagerError as error:
        raise _api_error(500, "STATE_MANAGER_ERROR", str(error)) from error
    if snapshot is None:
        raise _api_error(
            404,
            "STATE_SNAPSHOT_NOT_FOUND",
            f"State snapshot not found: {state_id}",
            {"state_id": state_id},
        )
    return state_snapshot_to_dict(snapshot)


def _api_error(
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
) -> ApiError:
    return ApiError(status_code=status_code, code=code, message=message, details=details)
