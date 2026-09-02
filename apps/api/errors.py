from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from packages.common.auth_policy import redact_audit_payload
from packages.common.redaction import REDACTION_MARKER
from services.slurm_gateway.validation_errors import slurm_request_validation_error_response

logger = logging.getLogger(__name__)

# `details` keys whose VALUE is raw client input of any shape. `redact_audit_payload`
# only redacts value-shaped secrets (paths, URIs, checksums) and sensitive key
# names, so `rejected_value: "sk-live-ABC"` would otherwise reach the log verbatim.
_CLIENT_INPUT_KEYS = frozenset({"rejected_value"})


class ApiError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def register_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def add_request_id(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith("/api/v1/slurm"):
            return slurm_request_validation_error_response(request, exc)
        details = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "rejected_value": error.get("input"),
                "reason": error.get("msg", "Invalid value"),
            }
            for error in exc.errors()
        ]
        return error_response(
            request,
            status_code=422,
            code="VALIDATION_ERROR",
            message="Request validation failed.",
            details=details,
        )


def _redact_client_input_keys(value: Any) -> Any:
    """Redact `_CLIENT_INPUT_KEYS` by KEY, recursing into the validation arm's list."""
    if isinstance(value, Mapping):
        return {
            str(key): (REDACTION_MARKER if str(key) in _CLIENT_INPUT_KEYS else _redact_client_input_keys(nested))
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact_client_input_keys(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_client_input_keys(item) for item in value)
    return value


def _redact_error_details(details: Any) -> Any:
    """What may be written to the server log for one error response.

    `redact_audit_payload` first (shared with the audit trail: paths, URIs,
    checksums, sensitive key names), then the key-level pass above for raw
    client input that has no recognisable shape.
    """
    return _redact_client_input_keys(redact_audit_payload(details))


def _log_error_response(
    request: Any,
    *,
    request_id: str,
    status_code: int,
    code: str,
    details: Any | None,
) -> None:
    """One grep-able server-side line per error response (#1704).

    The response is the contract; this line is best effort and must never
    change it, so every failure mode below degrades to silence rather than
    propagating. A redaction failure logs the exception TYPE only -- never the
    object, which is the raw payload this function exists to keep out of the log.
    """
    try:
        try:
            safe_details = _redact_error_details(details)
        except Exception as error:  # noqa: BLE001 - never log the value we failed to redact
            safe_details = f"<redaction-failed:{type(error).__name__}>"
        path = getattr(getattr(request, "url", None), "path", None) or "<unknown>"
        level = logging.ERROR if status_code >= 500 else logging.WARNING
        logger.log(
            level,
            "api_error request_id=%s code=%s status=%s path=%s details=%s",
            request_id,
            code,
            status_code,
            path,
            safe_details,
        )
    except Exception:  # noqa: BLE001 - logging must never break the error response
        pass


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    _log_error_response(
        request,
        request_id=request_id,
        status_code=status_code,
        code=code,
        details=details,
    )
    body: dict[str, Any] = {
        "request_id": request_id,
        "status": "error",
        "error": {
            "code": code,
            "message": message,
            "details": details,
        },
    }
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body),
        headers={"X-Request-ID": request_id},
    )
