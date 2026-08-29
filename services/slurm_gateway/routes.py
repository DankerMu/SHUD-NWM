from __future__ import annotations

import threading
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer
from starlette.concurrency import run_in_threadpool

from packages.common.auth_policy import (
    AUTH_REQUIRED,
    POLICY_ACTION_UNKNOWN,
    RBAC_FORBIDDEN,
    RELEASE_BLOCKED,
    PolicyDecision,
    audit_record,
    evaluate_policy,
    redact_audit_payload,
)
from packages.common.openapi_auth_security import (
    SLURM_SERVICE_BEARER_DESCRIPTION,
    identity_security_alternatives,
)
from packages.common.request_auth import record_request_policy_decision, slurm_mutation_auth_context
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

# Route-level OpenAPI security metadata for the four Slurm mutations. FastAPI
# publishes the HTTPBearer scheme into components.securitySchemes from the
# dependency; the operation-level `openapi_extra` carries the three existing
# identity alternatives. Both the scheme description and the alternative list
# come from the shared owner (packages.common.openapi_auth_security) so the
# standalone and full-API published contracts cannot drift. One canonical auth
# decision still owns enforcement (`_guarded_mutation`); the bearer dependency
# is auto_error=False so a missing credential never produces a framework 401
# before the policy decision.
_SLURM_SERVICE_BEARER_SCHEME = HTTPBearer(
    scheme_name="SlurmServiceBearer",
    description=SLURM_SERVICE_BEARER_DESCRIPTION,
    auto_error=False,
)


class _SlurmMutationDenied(Exception):
    """Internal denial carrier raised by a mutation auth dependency."""

    def __init__(self, decision: PolicyDecision, request: Request) -> None:
        super().__init__(decision.reason)
        self.decision = decision
        self.request = request


def _ensure_request_id(request: Request) -> str:
    """Return a stable request id, setting it exactly once on the request.

    The standalone gateway app has no API request-id middleware, so this is the
    single place that derives the id from a safe caller-supplied
    ``X-Request-ID`` header or a fresh ``req_``-prefixed UUID. Later callers
    read ``request.state.request_id`` so the same id flows into policy/audit
    records, the response body, and the ``X-Request-ID`` response header.
    """
    existing = getattr(request.state, "request_id", None)
    if existing:
        return existing
    header_value = (request.headers.get("X-Request-ID") or "").strip()
    request_id = header_value or f"req_{uuid4().hex}"
    request.state.request_id = request_id
    return request_id


class SlurmSafeValidationRoute(APIRoute):
    def get_route_handler(self) -> Any:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Any:
            try:
                return await original_route_handler(request)
            except _SlurmMutationDenied as exc:
                return _policy_error_response(exc.request, exc.decision)
            except RequestValidationError as exc:
                return slurm_request_validation_error_response(request, exc)

        return custom_route_handler


SLURM_ROUTE_JOB_ID_PATTERN = r"^(?:\d+(?:_\d+)?|mock_\d+)$"


class LazySlurmGateway:
    def __init__(self) -> None:
        self._instance: SlurmGateway | None = None
        self._lock = threading.Lock()

    def _get(self) -> SlurmGateway:
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = create_gateway()
        return self._instance

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)

    def reset_instance(self) -> None:
        with self._lock:
            self._instance = None


slurm_gateway = LazySlurmGateway()


def _policy_error_response(request: Request, decision: PolicyDecision) -> JSONResponse:
    request_id = _ensure_request_id(request)
    details = {
        "policy_decision": redact_audit_payload(decision.to_dict()),
        "audit_record": audit_record(decision, request_id=request_id),
    }
    if decision.decision == "release_blocked":
        status_code = 503
        code = RELEASE_BLOCKED
        message = decision.reason
        details["removal_criteria"] = "Configure and prove live backend identity-provider role mapping."
    elif decision.reason_code == AUTH_REQUIRED:
        status_code = 401
        code = AUTH_REQUIRED
        message = decision.reason
    elif decision.reason_code == POLICY_ACTION_UNKNOWN:
        status_code = 403
        code = "POLICY_CONFIG_ERROR"
        message = decision.reason
    else:
        status_code = 403
        code = RBAC_FORBIDDEN
        message = decision.reason
    response = ErrorResponse(
        request_id=request_id,
        error=ErrorBody(code=code, message=message, details=details),
    )
    return JSONResponse(
        status_code=status_code,
        content=response.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


def require_slurm_mutation_decision(
    request: Request,
    action_id: str,
    *,
    target_type: str,
    target_id: str,
) -> PolicyDecision:
    """One canonical policy decision per registered Slurm mutation.

    Runs as a pre-handler dependency so authorization resolves before gateway
    construction/calls and before request-body validation/side effects. The
    decision is recorded as redacted request policy/audit evidence with the
    stable request id.
    """
    request_id = _ensure_request_id(request)
    context = slurm_mutation_auth_context(request)
    decision = evaluate_policy(context, action_id, target_type=target_type, target_id=target_id)
    record_request_policy_decision(request, decision, request_id=request_id)
    return decision


def _guarded_mutation(request: Request, action_id: str, *, target_type: str, target_id: str) -> PolicyDecision:
    decision = require_slurm_mutation_decision(
        request,
        action_id,
        target_type=target_type,
        target_id=target_id,
    )
    if decision.decision != "allow":
        raise _SlurmMutationDenied(decision, request)
    return decision


def _guarded_submit_dependency(request: Request) -> PolicyDecision:
    return _guarded_mutation(request, "slurm.submit_job", target_type="slurm_gateway", target_id="job-submit")


def _guarded_cancel_dependency(request: Request) -> PolicyDecision:
    return _guarded_mutation(request, "slurm.cancel_job", target_type="slurm_gateway", target_id="job-cancel")


def _guarded_reset_dependency(
    request: Request,
    settings: Annotated[SlurmGatewaySettings, Depends(get_settings)],
) -> PolicyDecision | None:
    # Preserve full-app behavior: a disabled reset already refuses before any
    # side effect (the handler returns SLURM_INTERNAL_RESET_DISABLED), so no
    # credential is required on that path. The standalone app does not even
    # register the route when disabled (404 before auth). When enabled the
    # reset is sys_admin-only and requires one canonical policy decision.
    if not settings.allow_internal_reset:
        return None
    return _guarded_mutation(request, "slurm.reset_registry", target_type="slurm_gateway", target_id="registry-reset")


def _gateway_error_response(exc: SlurmGatewayError) -> JSONResponse:
    response = ErrorResponse(
        request_id=f"req_{uuid4().hex}",
        error=ErrorBody(code=exc.code, message=exc.message, details=exc.details),
    )
    return JSONResponse(status_code=exc.status_code, content=response.model_dump(mode="json"))


async def _run_gateway_call(method_name: str, *args: Any, **kwargs: Any) -> Any:
    def call() -> Any:
        method = getattr(slurm_gateway, method_name)
        return method(*args, **kwargs)

    return await run_in_threadpool(call)


async def health_check():
    try:
        return await _run_gateway_call("health")
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


async def submit_job(request: SubmitJobRequest):
    try:
        return await _run_gateway_call("submit_job", request)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


async def submit_job_array(request: Annotated[ArraySubmitJobRequest, Body()]):
    try:
        return await _run_gateway_call("submit_job_array", request)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


async def list_jobs(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    try:
        return await _run_gateway_call("list_jobs", limit=limit, offset=offset)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


async def get_job_status(job_id: Annotated[str, Path(pattern=SLURM_ROUTE_JOB_ID_PATTERN)]):
    try:
        return await _run_gateway_call("get_job_status", job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


async def get_array_task_results(job_id: Annotated[str, Path(pattern=SLURM_ROUTE_JOB_ID_PATTERN)]):
    try:
        return await _run_gateway_call("get_array_task_results", job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


async def cancel_job(job_id: Annotated[str, Path(pattern=SLURM_ROUTE_JOB_ID_PATTERN)]):
    try:
        return await _run_gateway_call("cancel_job", job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


async def fetch_logs(job_id: Annotated[str, Path(pattern=SLURM_ROUTE_JOB_ID_PATTERN)]):
    try:
        return await _run_gateway_call("fetch_logs", job_id)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


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
    try:
        return await _run_gateway_call("reset", request)
    except SlurmGatewayError as exc:
        return _gateway_error_response(exc)


def _slurm_mutation_openapi_extra() -> dict[str, Any]:
    """Operation-level security metadata: the three existing identity alternatives.

    The route-level ``HTTPBearer`` dependency publishes ``SlurmServiceBearer``
    into the operation's security list first; FastAPI appends ``openapi_extra``
    after it, so this carries exactly the identity alternatives (never a second
    bearer leg).
    """
    return {"security": identity_security_alternatives()}


def create_slurm_router(*, include_internal_reset: bool = True) -> APIRouter:
    router = APIRouter(prefix="/api/v1/slurm", tags=["slurm"], route_class=SlurmSafeValidationRoute)
    router.add_api_route("/health", health_check, methods=["GET"])
    router.add_api_route(
        "/jobs",
        submit_job,
        methods=["POST"],
        status_code=201,
        dependencies=[
            Depends(_guarded_submit_dependency),
            Depends(_SLURM_SERVICE_BEARER_SCHEME),
        ],
        openapi_extra=_slurm_mutation_openapi_extra(),
    )
    router.add_api_route(
        "/job-arrays",
        submit_job_array,
        methods=["POST"],
        status_code=201,
        dependencies=[
            Depends(_guarded_submit_dependency),
            Depends(_SLURM_SERVICE_BEARER_SCHEME),
        ],
        openapi_extra=_slurm_mutation_openapi_extra(),
    )
    router.add_api_route("/jobs", list_jobs, methods=["GET"])
    router.add_api_route("/jobs/{job_id}", get_job_status, methods=["GET"])
    router.add_api_route("/jobs/{job_id}/array-tasks", get_array_task_results, methods=["GET"])
    router.add_api_route(
        "/jobs/{job_id}",
        cancel_job,
        methods=["DELETE"],
        dependencies=[
            Depends(_guarded_cancel_dependency),
            Depends(_SLURM_SERVICE_BEARER_SCHEME),
        ],
        openapi_extra=_slurm_mutation_openapi_extra(),
    )
    router.add_api_route("/jobs/{job_id}/logs", fetch_logs, methods=["GET"])
    if include_internal_reset:
        router.add_api_route(
            "/internal/reset",
            reset_registry,
            methods=["POST"],
            dependencies=[
                Depends(_guarded_reset_dependency),
                Depends(_SLURM_SERVICE_BEARER_SCHEME),
            ],
            openapi_extra=_slurm_mutation_openapi_extra(),
        )
    return router


router = create_slurm_router()
