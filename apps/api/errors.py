from __future__ import annotations

import logging
import re
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
# The plural is the sibling shape the stores raise (`packages/common/forecast_store.py`
# for an unknown `variables` token; `apps/api/routes/forecast.py` and
# `apps/api/routes/pipeline.py` build it as a mapping of field -> reflected value),
# and it reaches this chokepoint through `ApiError.details`.
# RESIDUAL, by design: only these keys are redacted unconditionally. Client-supplied
# identifiers under any other key (`station_id`, `layer_id`, `run_id`, `cycle_time`)
# stay verbatim unless value-shaped or under a sensitive key.
_CLIENT_INPUT_KEYS = frozenset({"rejected_value", "rejected_values"})

# Budget for the rendered `details=` segment of one line. The validation arm
# renders one `{field, rejected_value, reason}` entry PER invalid item, so a
# single authorised POST of a few thousand items wrote a multi-megabyte line
# into an unrotated unit log (measured 8.5 MB from a 20 000-item body). The
# response body is unaffected; only what is written server-side is bounded.
_DETAILS_RENDER_BUDGET_BYTES = 8192
_DETAILS_TRUNCATION_MARKER = "…[truncated {dropped} bytes]"

# An inbound `X-Request-ID` is echoed into the response header, the audit record
# and this log line. Accepting it verbatim let a client inject `code=`/`path=`
# tokens into the line it would later be grepped from, so anything outside this
# shape is replaced by a server-minted UUID.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def sanitize_request_id(header_value: Any) -> str:
    """The inbound `X-Request-ID` if it is safe to echo, else a fresh UUID.

    Shared by `add_request_id` (the response header + `request.state`) and
    `apps/api/main.py::_ensure_request_id` (the pre-body auth path), so the
    header, the audit record and the log line can never disagree.
    """
    if isinstance(header_value, str) and _REQUEST_ID_RE.fullmatch(header_value):
        return header_value
    return str(uuid4())


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
        request_id = sanitize_request_id(request.headers.get("X-Request-ID"))
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

    The key-level pass above runs FIRST, then `redact_audit_payload` (shared
    with the audit trail: paths, URIs, checksums, sensitive key names). The
    order is behaviour-identical -- `redact_audit_payload` maps the
    `[redacted]` marker to itself -- but it means a body-sized `rejected_value`
    subtree is collapsed to one string before the deep audit walk instead of
    being traversed node by node first.
    """
    return redact_audit_payload(_redact_client_input_keys(details))


def _render_details(safe_details: Any) -> str:
    """The `details=` segment, bounded to `_DETAILS_RENDER_BUDGET_BYTES`.

    Length is measured on the UTF-8 encoding (what actually lands in the file);
    the cut is decoded with `errors="ignore"` so it always falls on a character
    boundary, and the dropped byte count is stated rather than implied.
    """
    text = str(safe_details)
    encoded = text.encode("utf-8")
    if len(encoded) <= _DETAILS_RENDER_BUDGET_BYTES:
        return text
    kept = encoded[:_DETAILS_RENDER_BUDGET_BYTES].decode("utf-8", "ignore")
    dropped = len(encoded) - len(kept.encode("utf-8"))
    return kept + _DETAILS_TRUNCATION_MARKER.format(dropped=dropped)


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
            rendered_details = _render_details(_redact_error_details(details))
        except Exception as error:  # noqa: BLE001 - never log the value we failed to redact
            rendered_details = f"<redaction-failed:{type(error).__name__}>"
        path = getattr(getattr(request, "url", None), "path", None) or "<unknown>"
        level = logging.ERROR if status_code >= 500 else logging.WARNING
        logger.log(
            level,
            "api_error request_id=%s code=%s status=%s path=%s details=%s",
            request_id,
            code,
            status_code,
            path,
            rendered_details,
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
