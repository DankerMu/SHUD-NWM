from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from packages.common.slurm_env import secret_manifest_key_reason
from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.gateway import SlurmGatewayError, create_gateway
from services.slurm_gateway.models import (
    ArraySubmitJobRequest,
    ErrorBody,
    ErrorResponse,
    ResetRequest,
    SubmitJobRequest,
)


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
slurm_gateway = create_gateway()
SLURM_ROUTE_JOB_ID_PATTERN = r"^(?:\d+(?:_\d+)?|mock_\d+)$"


def _request_id_for(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID")
    if request_id:
        return str(request_id)
    request_id = f"req_{uuid4().hex}"
    request.state.request_id = request_id
    return request_id


def _safe_validation_details(errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for error in errors:
        error_type = str(error.get("type") or "value_error")
        details.append(
            {
                "field": ".".join(_safe_validation_location_part(part) for part in error.get("loc", ())),
                "reason": _safe_validation_reason(error_type),
                "type": error_type,
            }
        )
    return details


def _safe_validation_location_part(part: Any) -> str:
    if isinstance(part, int):
        return str(part)
    text = str(part)
    if secret_manifest_key_reason(text) is not None:
        return "[redacted]"
    return text


def _safe_validation_reason(error_type: str) -> str:
    if error_type == "missing":
        return "Field required."
    return "Invalid request value."


def slurm_request_validation_error_response(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = _request_id_for(request)
    response = ErrorResponse(
        request_id=request_id,
        error=ErrorBody(
            code="VALIDATION_ERROR",
            message="Request validation failed.",
            details={"validation_errors": _safe_validation_details(exc.errors())},
        ),
    )
    return JSONResponse(
        status_code=422,
        content=response.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


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
