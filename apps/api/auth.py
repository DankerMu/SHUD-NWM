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
    _parse_roles,
    _raw_roles,
    _role_mapping_result,
    audit_record,
    cli_policy_decision_from_evidence,
    evaluate_policy,
    redact_audit_payload,
    require_policy_evidence,
    simulated_decisions_for_action,
    trusted_internal_policy_decision,
)
from packages.common.redaction import REDACTION_MARKER

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
    if _live_auth_requested():
        if not _internal_live_proof_token_matches(request):
            return _release_blocked_auth_context()
        live_actor = request.headers.get("X-Live-User-ID", "").strip()
        raw_roles = _raw_roles(request.headers.get("X-Live-User-Roles", ""))
        mapped_roles = _parse_roles(request.headers.get("X-Live-User-Roles", ""))
        provider = request.headers.get("X-Live-Provider", "").strip() or "test-internal-live-proof"
        if not live_actor:
            return _release_blocked_auth_context()
        return AuthContext(
            actor_id=live_actor,
            roles=mapped_roles,
            auth_mode="live_idp",
            live_backend_auth_executed=True,
            provider_metadata={
                "provider": provider,
                "contract": "test_internal_trusted_live_proof",
                "credential_header": REDACTION_MARKER,
            },
            role_mapping_result={
                "raw_roles_present": bool(raw_roles),
                "raw_roles": raw_roles,
                "mapped_roles": mapped_roles,
                "unmapped_roles": tuple(role for role in raw_roles if role not in ROLE_VOCABULARY),
                "mapping_status": "mapped" if mapped_roles else "unmapped",
            },
        )

    if _allow_dev_role_header() and not _production_mode() and "X-User-Role" in request.headers:
        raw_role_text = request.headers.get("X-User-Role", "")
        roles = _parse_roles(raw_role_text)
        raw_roles = _raw_roles(raw_role_text)
        if roles:
            actor = request.headers.get("X-User-ID", "").strip() or f"dev-test:{roles[0]}"
            return AuthContext(
                actor_id=actor,
                roles=roles,
                auth_mode="dev_test",
                live_backend_auth_executed=False,
                role_mapping_result=_role_mapping_result(raw_roles, mapped_roles=roles, input_present=True),
            )
        actor = request.headers.get("X-User-ID", "").strip() or "dev-test:unmapped-role"
        return AuthContext(
            actor_id=actor,
            roles=(),
            auth_mode="dev_test",
            live_backend_auth_executed=False,
            role_mapping_result=_role_mapping_result(raw_roles, mapped_roles=(), input_present=True),
        )

    configured_token = os.getenv("NHMS_DEV_AUTH_TOKEN", "").strip()
    authorization = request.headers.get("Authorization", "")
    if configured_token and authorization == f"Bearer {configured_token}" and not _production_mode():
        role_header_present = "X-User-Role" in request.headers
        raw_role_text = request.headers.get("X-User-Role", "") if role_header_present else "operator"
        roles = _parse_roles(raw_role_text)
        raw_roles = _raw_roles(raw_role_text)
        actor = request.headers.get("X-User-ID", "").strip() or "dev-test:token"
        return AuthContext(
            actor_id=actor,
            roles=roles,
            auth_mode="dev_test",
            live_backend_auth_executed=False,
            role_mapping_result=_role_mapping_result(raw_roles, mapped_roles=roles, input_present=role_header_present),
        )

    return None


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
