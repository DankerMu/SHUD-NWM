from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from fastapi import Request

from apps.api.errors import ApiError
from packages.common.auth_policy import (
    ACTION_MATRIX,
    AUTH_REQUIRED,
    POLICY_ACTION_UNKNOWN,
    RBAC_FORBIDDEN,
    RELEASE_BLOCKED,
    ROLE_VOCABULARY,
    ActionDecision,
    AuthContext,
    AuthRole,
    ExecutionMode,
    PolicyDecision,
    audit_record,
    cli_policy_decision_from_evidence,
    evaluate_policy,
    redact_audit_payload,
    require_policy_evidence,
    simulated_decisions_for_action,
    trusted_internal_policy_decision,
)
from packages.common.request_auth import (
    SLURM_GATEWAY_SERVICE_TOKEN_ENV,
    SLURM_SERVICE_ACTOR,
    slurm_mutation_auth_context,
    slurm_service_auth_context,
)
from packages.common.request_auth import (
    auth_context_from_request as _shared_auth_context_from_request,
)

POLICY_CONFIG_ERROR = "POLICY_CONFIG_ERROR"

_TRUTHY = {"1", "true", "yes", "on"}
_LIVE_AUTH_BACKENDS = {"live", "live_idp", "oidc", "saml"}

__all__ = [
    "ACTION_MATRIX",
    "AUTH_REQUIRED",
    "POLICY_ACTION_UNKNOWN",
    "POLICY_CONFIG_ERROR",
    "RBAC_FORBIDDEN",
    "RELEASE_BLOCKED",
    "ROLE_VOCABULARY",
    "SLURM_GATEWAY_SERVICE_TOKEN_ENV",
    "SLURM_SERVICE_ACTOR",
    "ActionDecision",
    "AuthContext",
    "AuthRole",
    "ExecutionMode",
    "PolicyDecision",
    "audit_record",
    "auth_context_from_request",
    "cli_policy_decision_from_evidence",
    "evaluate_policy",
    "evaluate_request_action",
    "redact_audit_payload",
    "require_action",
    "require_policy_evidence",
    "simulated_decisions_for_action",
    "slurm_mutation_auth_context",
    "slurm_service_auth_context",
    "trusted_internal_policy_decision",
]


def require_action(
    request: Request,
    action_id: str,
    *,
    target_type: str,
    target_id: str,
    payload: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    decision = evaluate_request_action(
        request,
        action_id,
        target_type=target_type,
        target_id=target_id,
    )
    _record_decision(request, decision, payload=payload)
    if decision.decision == "allow":
        return decision
    details = {
        "policy_decision": redact_audit_payload(decision.to_dict()),
        "audit_record": audit_record(decision, request_id=getattr(request.state, "request_id", None), payload=payload),
    }
    if decision.decision == "release_blocked":
        raise ApiError(
            status_code=503,
            code=RELEASE_BLOCKED,
            message=decision.reason,
            details={
                **details,
                "removal_criteria": "Configure and prove live backend identity-provider role mapping.",
            },
        )
    if decision.reason_code == AUTH_REQUIRED:
        raise ApiError(status_code=401, code=AUTH_REQUIRED, message=decision.reason, details=details)
    if decision.reason_code == POLICY_ACTION_UNKNOWN:
        raise ApiError(status_code=403, code=POLICY_CONFIG_ERROR, message=decision.reason, details=details)
    raise ApiError(status_code=403, code=RBAC_FORBIDDEN, message=decision.reason, details=details)


def evaluate_request_action(
    request: Request,
    action_id: str,
    *,
    target_type: str,
    target_id: str,
) -> PolicyDecision:
    context = auth_context_from_request(request)
    return evaluate_policy(context, action_id, target_type=target_type, target_id=target_id)


def auth_context_from_request(request: Request) -> AuthContext | None:
    """Build the request auth context (dev/test or live/release-blocked).

    Construction lives in ``packages.common.request_auth`` so that
    ``services.slurm_gateway`` can reuse it without importing ``apps.api``. The
    Slurm scheduler service bearer is deliberately NOT accepted here: it must
    never authenticate an original business mutation.
    """
    return _shared_auth_context_from_request(request)


def _record_decision(request: Request, decision: PolicyDecision, *, payload: Mapping[str, Any] | None) -> None:
    decisions = getattr(request.state, "auth_policy_decisions", None)
    if decisions is None:
        decisions = []
        request.state.auth_policy_decisions = decisions
    decisions.append(audit_record(decision, request_id=getattr(request.state, "request_id", None), payload=payload))


def _allow_dev_role_header() -> bool:
    return os.getenv("ALLOW_DEV_ROLE_HEADER", "").strip().lower() in _TRUTHY


def _production_mode() -> bool:
    return os.getenv("NHMS_AUTH_MODE", "").strip().lower() in {"production", "live", "live_idp"}


def _live_auth_requested() -> bool:
    auth_backend = os.getenv("AUTH_BACKEND", "").strip().lower()
    auth_mode = os.getenv("NHMS_AUTH_MODE", "").strip().lower()
    return auth_backend in _LIVE_AUTH_BACKENDS or auth_mode in {"live", "live_idp"}


def _live_auth_release_blocked() -> bool:
    return _live_auth_requested() and not _trusted_live_auth_proof_available()


def _release_blocked_auth_context() -> AuthContext:
    return AuthContext(
        actor_id="release-blocked",
        roles=(),
        auth_mode="live_idp",
        live_backend_auth_executed=False,
    )


def _trusted_live_auth_proof_enabled() -> bool:
    return os.getenv("NHMS_TRUSTED_LIVE_PROOF_MODE", "").strip().lower() == "test_internal"


def _trusted_live_auth_proof_available() -> bool:
    token = os.getenv("NHMS_INTERNAL_LIVE_PROOF_TOKEN", "").strip()
    return _trusted_live_auth_proof_enabled() and bool(token) and not _production_mode()


def _internal_live_proof_token_matches(request: Request) -> bool:
    if not _trusted_live_auth_proof_available():
        return False
    configured_token = os.getenv("NHMS_INTERNAL_LIVE_PROOF_TOKEN", "").strip()
    return request.headers.get("X-NHMS-Internal-Live-Proof", "") == configured_token
