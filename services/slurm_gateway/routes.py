from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from services.slurm_gateway.gateway import SlurmGatewayError, create_gateway
from services.slurm_gateway.models import (
    ArraySubmitJobRequest,
    ErrorBody,
    ErrorResponse,
    ResetRequest,
    SubmitJobRequest,
)

router = APIRouter(prefix="/api/v1/slurm", tags=["slurm"])
slurm_gateway = create_gateway()


def _gateway_error_response(exc: SlurmGatewayError) -> JSONResponse:
    response = ErrorResponse(
        request_id=f"req_{uuid4().hex}",
        error=ErrorBody(code=exc.code, message=exc.message, details=exc.details),
    )
    return JSONResponse(status_code=exc.status_code, content=response.model_dump(mode="json"))


@router.get("/health")
async def health_check():
    return slurm_gateway.health()


@router.post("/jobs", status_code=201)
async def submit_job(request: SubmitJobRequest):
    try:
        return slurm_gateway.submit_job(request)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.post("/job-arrays", status_code=201)
async def submit_job_array(request: Annotated[ArraySubmitJobRequest, Body()]):
    try:
        submit_array = getattr(slurm_gateway, "submit_job_array")
        return submit_array(request)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.get("/jobs")
async def list_jobs(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    try:
        return slurm_gateway.list_jobs(limit=limit, offset=offset)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    try:
        return slurm_gateway.get_job_status(job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.get("/jobs/{job_id}/array-tasks")
async def get_array_task_results(job_id: str):
    try:
        return slurm_gateway.get_array_task_results(job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    try:
        return slurm_gateway.cancel_job(job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.get("/jobs/{job_id}/logs")
async def fetch_logs(job_id: str):
    try:
        return slurm_gateway.fetch_logs(job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.post("/internal/reset")
async def reset_registry(request: Annotated[ResetRequest | None, Body()] = None):
    return slurm_gateway.reset(request)
