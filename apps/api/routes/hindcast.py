from __future__ import annotations

import os
from collections.abc import Generator
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apps.api.auth import require_action
from apps.api.errors import ApiError
from apps.api.routes.pipeline import _ok
from workers.flood_frequency.config import HindcastConfig
from workers.flood_frequency.hindcast import (
    HINDCAST_FORCING_PACKAGE_UNAVAILABLE,
    HindcastError,
    calendar_years,
    mark_hindcast_runs_failed,
    submit_hindcast,
    submit_hindcast_slurm,
)

router = APIRouter(prefix="/api/v1", tags=["hindcast"])


class HindcastSubmitRequest(BaseModel):
    model_id: str
    source_id: str
    start_time: str
    end_time: str
    purpose: str = "flood_frequency_sample"


@lru_cache
def _engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def get_hindcast_session() -> Generator[Session, None, None]:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ApiError(
            status_code=500,
            code="DATABASE_URL_MISSING",
            message="DATABASE_URL is required for hindcast operations.",
        )
    with Session(_engine(database_url)) as session:
        yield session


def get_hindcast_config() -> HindcastConfig:
    return HindcastConfig.from_env()


@router.post("/hindcast/submit")
def submit_hindcast_api(
    body: HindcastSubmitRequest,
    request: Request,
    session: Session = Depends(get_hindcast_session),
    config: HindcastConfig = Depends(get_hindcast_config),
) -> dict[str, Any]:
    require_action(request, "pipeline.rerun_cycle", target_type="hindcast", target_id=body.model_id)
    if body.source_id.upper() != "ERA5":
        raise ApiError(
            status_code=400,
            code="INVALID_SOURCE_ID",
            message="Hindcast source_id must be ERA5.",
            details={"source_id": body.source_id},
        )
    _validate_time_range(body.start_time, body.end_time)
    basin_version_id = _require_model_exists(session, body.model_id)

    try:
        result = submit_hindcast(
            body.model_id,
            body.source_id,
            body.start_time,
            body.end_time,
            body.purpose,
            session,
        )
        years = _years_from_run_ids(result.run_ids)
        slurm_config = HindcastConfig(
            workspace_root=config.workspace_root,
            object_store_root=config.object_store_root,
            object_store_prefix=config.object_store_prefix,
            slurm_gateway_url=config.slurm_gateway_url,
            slurm_client=config.slurm_client,
            db_session=session,
        )
        try:
            slurm = (
                submit_hindcast_slurm(
                    body.model_id,
                    body.source_id,
                    years,
                    slurm_config,
                    basin_version_id=basin_version_id,
                )
                if years
                else None
            )
        except HindcastError as error:
            if error.error_code == HINDCAST_FORCING_PACKAGE_UNAVAILABLE:
                mark_hindcast_runs_failed(session, result.run_ids, error.error_code, error.message)
            raise
    except HindcastError as error:
        raise _api_error(error) from error

    return _ok(
        request,
        {
            "total_runs": result.total_runs,
            "run_ids": result.run_ids,
            "skipped_years": result.skipped_years,
            "active_years": result.active_years,
            "slurm_job_array_id": slurm.slurm_job_array_id if slurm is not None else None,
        },
    )


def _validate_time_range(start_time: str, end_time: str) -> None:
    try:
        calendar_years(start_time, end_time)
    except HindcastError as error:
        raise ApiError(
            status_code=400,
            code=error.error_code,
            message=error.message,
            details=error.details,
        ) from error


def _require_model_exists(session: Session, model_id: str) -> str:
    row = session.execute(
        text("SELECT model_id, basin_version_id FROM core.model_instance WHERE model_id = :model_id LIMIT 1"),
        {"model_id": model_id},
    ).mappings().first()
    if row is None:
        raise ApiError(
            status_code=404,
            code="MODEL_NOT_FOUND",
            message=f"Model not found: {model_id}",
            details={"model_id": model_id},
        )
    return str(row["basin_version_id"])


def _years_from_run_ids(run_ids: list[str]) -> list[int]:
    years: list[int] = []
    for run_id in run_ids:
        try:
            years.append(int(run_id.rsplit("_", maxsplit=1)[1]))
        except (IndexError, ValueError):
            continue
    return years


def _api_error(error: HindcastError) -> ApiError:
    status_code = 404 if error.error_code == "MODEL_NOT_FOUND" else 400
    return ApiError(
        status_code=status_code,
        code=error.error_code,
        message=error.message,
        details=error.details,
    )
