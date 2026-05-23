from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.gateway import SlurmGateway, SlurmGatewayError, create_gateway
from services.slurm_gateway.models import (
    ArraySubmitJobRequest,
    ErrorBody,
    ErrorResponse,
    ResetRequest,
    SubmitJobRequest,
)
from services.slurm_gateway.validation_errors import slurm_request_validation_error_response


class SlurmSafeValidationRoute(APIRoute):
    def get_route_handler(self) -> Any:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Any:
            try:
                return await original_route_handler(request)
            except RequestValidationError as exc:
                return slurm_request_validation_error_response(request, exc)

        return custom_route_handler


router = APIRouter(prefix="/api/v1/slurm", tags=["slurm"], route_class=SlurmSafeValidationRoute)
SLURM_ROUTE_JOB_ID_PATTERN = r"^(?:\d+(?:_\d+)?|mock_\d+)$"


class LazySlurmGateway:
    def __init__(self) -> None:
        self._instance: SlurmGateway | None = None

    def _get(self) -> SlurmGateway:
        if self._instance is None:
            self._instance = create_gateway()
        return self._instance

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)

    def reset_instance(self) -> None:
        self._instance = None


slurm_gateway = LazySlurmGateway()


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
async def get_job_status(job_id: Annotated[str, Path(pattern=SLURM_ROUTE_JOB_ID_PATTERN)]):
    try:
        return slurm_gateway.get_job_status(job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.get("/jobs/{job_id}/array-tasks")
async def get_array_task_results(job_id: Annotated[str, Path(pattern=SLURM_ROUTE_JOB_ID_PATTERN)]):
    try:
        return slurm_gateway.get_array_task_results(job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: Annotated[str, Path(pattern=SLURM_ROUTE_JOB_ID_PATTERN)]):
    try:
        return slurm_gateway.cancel_job(job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.get("/jobs/{job_id}/logs")
async def fetch_logs(job_id: Annotated[str, Path(pattern=SLURM_ROUTE_JOB_ID_PATTERN)]):
    try:
        return slurm_gateway.fetch_logs(job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


@router.post("/internal/reset")
async def reset_registry(
    settings: Annotated[SlurmGatewaySettings, Depends(get_settings)],
    request: Annotated[ResetRequest | None, Body()] = None,
):
    if not settings.allow_internal_reset:
        exc = SlurmGatewayError(
            403,
            "SLURM_INTERNAL_RESET_DISABLED",
            "Internal Slurm reset is disabled.",
            {"setting": "SLURM_GATEWAY_ALLOW_INTERNAL_RESET"},
        )
        return _gateway_error_response(exc)
    return slurm_gateway.reset(request)
