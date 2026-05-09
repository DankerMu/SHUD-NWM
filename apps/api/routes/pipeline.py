from __future__ import annotations

import os
from collections.abc import Generator
from functools import lru_cache
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apps.api.errors import ApiError
from services.orchestrator.persistence import PipelineStore
from services.orchestrator.retry import RetryConfig, RetryConflictError, RetryNotFoundError, RetryService
from services.slurm_gateway.config import SlurmGatewaySettings, get_settings

router = APIRouter(prefix="/api/v1", tags=["pipeline"])


@lru_cache
def _engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def get_pipeline_store() -> Generator[PipelineStore, None, None]:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ApiError(
            status_code=500,
            code="DATABASE_URL_MISSING",
            message="DATABASE_URL is required for pipeline retry operations.",
        )
    with Session(_engine(database_url)) as session:
        yield PipelineStore(session)


def get_retry_service(
    store: PipelineStore = Depends(get_pipeline_store),
    settings: SlurmGatewaySettings = Depends(get_settings),
) -> RetryService:
    return RetryService(store, RetryConfig.from_settings(settings))


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: str,
    request: Request,
    service: RetryService = Depends(get_retry_service),
) -> dict[str, Any]:
    try:
        job = service.attempt_manual_retry(run_id)
    except RetryConflictError as error:
        raise _api_error(error) from error
    except RetryNotFoundError as error:
        raise _api_error(error) from error

    return {
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
        "status": "ok",
        "data": {
            "job_id": job.job_id,
            "run_id": job.run_id,
            "retry_count": job.retry_count,
            "status": job.status,
        },
    }


def _api_error(error: RetryConflictError | RetryNotFoundError) -> ApiError:
    return ApiError(
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        details=error.details,
    )
