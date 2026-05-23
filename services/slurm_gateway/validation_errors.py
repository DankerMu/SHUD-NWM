from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from packages.common.slurm_env import secret_manifest_key_reason
from services.slurm_gateway.models import ErrorBody, ErrorResponse


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
