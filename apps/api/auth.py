from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from fastapi import Request

from apps.api.errors import ApiError
from packages.common.redaction import REDACTION_MARKER, is_sensitive_key, redact_text

AuthRole = Literal["viewer", "analyst", "operator", "model_admin", "sys_admin"]
ActionDecision = Literal["allow", "deny", "release_blocked"]
ExecutionMode = Literal["policy_simulated", "backend_route_executed", "live_proof", "release_blocked"]

AUTH_REQUIRED = "AUTH_REQUIRED"
RBAC_FORBIDDEN = "RBAC_FORBIDDEN"
RELEASE_BLOCKED = "RELEASE_BLOCKED"

ROLE_VOCABULARY: tuple[AuthRole, ...] = ("viewer", "analyst", "operator", "model_admin", "sys_admin")

ACTION_MATRIX: dict[str, tuple[AuthRole, ...]] = {
    "pipeline.retry_run": ("operator", "model_admin", "sys_admin"),
    "pipeline.cancel_run": ("operator", "model_admin", "sys_admin"),
    "pipeline.rerun_cycle": ("operator", "model_admin", "sys_admin"),
    "qc.override_result": ("operator", "sys_admin"),
    "tiles.republish": ("operator", "sys_admin"),
    "sources.update_config": ("sys_admin",),
    "models.activate": ("model_admin", "sys_admin"),
    "models.deactivate": ("model_admin", "sys_admin"),
    "models.switch_version": ("model_admin", "sys_admin"),
    "models.rollback_version": ("model_admin", "sys_admin"),
    "models.supersede": ("model_admin", "sys_admin"),
    "users.manage": ("sys_admin",),
}

_TRUTHY = {"1", "true", "yes", "on"}
_URI_OR_PATH_KEY_RE = re.compile(r"(uri|url|path|log|checksum|lineage|manifest|credential)", re.IGNORECASE)
_LOCAL_PATH_RE = re.compile(r"(^|[\s=:])(?:/[A-Za-z0-9_.-][^\s,;]*|[A-Za-z]:\\[^\s,;]*)")
_CHECKSUM_RE = re.compile(r"\b(?:sha(?:256|1)?[:=_-]?)?[a-f0-9]{32,128}\b", re.IGNORECASE)


@dataclass(frozen=True)
class AuthContext:
    actor_id: str
    roles: tuple[AuthRole, ...]
    auth_mode: str
    live_backend_auth_executed: bool


@dataclass(frozen=True)
class PolicyDecision:
    action_id: str
    decision: ActionDecision
    required_roles: tuple[AuthRole, ...]
    matched_roles: tuple[AuthRole, ...]
    actor_id: str
    target_type: str
    target_id: str
    reason: str
    reason_code: str
    roles: tuple[AuthRole, ...]
    execution_mode: ExecutionMode
    no_mutation_expected: bool
    auth_mode: str
    live_backend_auth_executed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "decision": self.decision,
            "required_roles": list(self.required_roles),
            "matched_roles": list(self.matched_roles),
            "actor_id": self.actor_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "roles": list(self.roles),
            "execution_mode": self.execution_mode,
            "no_mutation_expected": self.no_mutation_expected,
            "auth_mode": self.auth_mode,
            "live_backend_auth_executed": self.live_backend_auth_executed,
        }


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
    if _live_auth_release_blocked():
        return AuthContext(
            actor_id="release-blocked",
            roles=(),
            auth_mode="live_idp",
            live_backend_auth_executed=False,
        )

    if _live_auth_proof_enabled():
        live_actor = request.headers.get("X-Live-User-ID", "").strip()
        live_roles = _parse_roles(request.headers.get("X-Live-User-Roles", ""))
        if live_actor and live_roles:
            return AuthContext(
                actor_id=live_actor,
                roles=live_roles,
                auth_mode="live_idp",
                live_backend_auth_executed=True,
            )

    if _allow_dev_role_header() and not _production_mode():
        roles = _parse_roles(request.headers.get("X-User-Role", ""))
        if roles:
            actor = request.headers.get("X-User-ID", "").strip() or f"dev-test:{roles[0]}"
            return AuthContext(
                actor_id=actor,
                roles=roles,
                auth_mode="dev_test",
                live_backend_auth_executed=False,
            )

    configured_token = os.getenv("NHMS_DEV_AUTH_TOKEN", "").strip()
    authorization = request.headers.get("Authorization", "")
    if configured_token and authorization == f"Bearer {configured_token}" and not _production_mode():
        roles = _parse_roles(request.headers.get("X-User-Role", "operator")) or ("operator",)
        actor = request.headers.get("X-User-ID", "").strip() or "dev-test:token"
        return AuthContext(actor_id=actor, roles=roles, auth_mode="dev_test", live_backend_auth_executed=False)

    return None


def evaluate_policy(
    context: AuthContext | None,
    action_id: str,
    *,
    target_type: str,
    target_id: str,
    execution_mode: ExecutionMode | None = None,
) -> PolicyDecision:
    required_roles = ACTION_MATRIX[action_id]
    if context is not None and context.auth_mode == "live_idp" and not context.live_backend_auth_executed:
        return PolicyDecision(
            action_id=action_id,
            decision="release_blocked",
            required_roles=required_roles,
            matched_roles=(),
            actor_id=context.actor_id,
            target_type=target_type,
            target_id=target_id,
            reason="Live backend auth is configured but no accepted live IdP proof is available.",
            reason_code=RELEASE_BLOCKED,
            roles=context.roles,
            execution_mode="release_blocked",
            no_mutation_expected=True,
            auth_mode=context.auth_mode,
            live_backend_auth_executed=False,
        )
    if context is None:
        return PolicyDecision(
            action_id=action_id,
            decision="deny",
            required_roles=required_roles,
            matched_roles=(),
            actor_id="anonymous",
            target_type=target_type,
            target_id=target_id,
            reason="Authentication is required for this protected action.",
            reason_code=AUTH_REQUIRED,
            roles=(),
            execution_mode=execution_mode or "backend_route_executed",
            no_mutation_expected=True,
            auth_mode="none",
            live_backend_auth_executed=False,
        )
    matched_roles = tuple(role for role in context.roles if role in required_roles)
    if not matched_roles:
        return PolicyDecision(
            action_id=action_id,
            decision="deny",
            required_roles=required_roles,
            matched_roles=(),
            actor_id=context.actor_id,
            target_type=target_type,
            target_id=target_id,
            reason="Actor roles are not authorized for this protected action.",
            reason_code=RBAC_FORBIDDEN,
            roles=context.roles,
            execution_mode=execution_mode
            or ("live_proof" if context.live_backend_auth_executed else "backend_route_executed"),
            no_mutation_expected=True,
            auth_mode=context.auth_mode,
            live_backend_auth_executed=context.live_backend_auth_executed,
        )
    return PolicyDecision(
        action_id=action_id,
        decision="allow",
        required_roles=required_roles,
        matched_roles=matched_roles,
        actor_id=context.actor_id,
        target_type=target_type,
        target_id=target_id,
        reason="Actor roles satisfy the canonical RBAC policy.",
        reason_code="RBAC_ALLOWED",
        roles=context.roles,
        execution_mode=execution_mode
        or ("live_proof" if context.live_backend_auth_executed else "backend_route_executed"),
        no_mutation_expected=False,
        auth_mode=context.auth_mode,
        live_backend_auth_executed=context.live_backend_auth_executed,
    )


def audit_record(
    decision: PolicyDecision,
    *,
    request_id: str | None,
    payload: Mapping[str, Any] | None = None,
    previous_state: Mapping[str, Any] | None = None,
    new_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return redact_audit_payload(
        {
            "request_id": request_id,
            "actor": decision.actor_id,
            "actor_id": decision.actor_id,
            "roles": list(decision.roles),
            "action": decision.action_id,
            "action_id": decision.action_id,
            "target": {"type": decision.target_type, "id": decision.target_id},
            "decision": decision.decision,
            "reason": decision.reason,
            "reason_code": decision.reason_code,
            "execution_mode": decision.execution_mode,
            "auth_mode": decision.auth_mode,
            "live_backend_auth_executed": decision.live_backend_auth_executed,
            "no_mutation_expected": decision.no_mutation_expected,
            "previous_state": previous_state or {},
            "new_state": new_state or {},
            "lineage": payload or {},
        }
    )


def simulated_decisions_for_action(
    action_id: str,
    *,
    target_id: str,
    target_type: str = "fixture",
) -> list[PolicyDecision]:
    allowed_role = ACTION_MATRIX[action_id][0]
    state = [
        AuthContext(
            actor_id=f"ops-{allowed_role}",
            roles=(allowed_role,),
            auth_mode="dev_test",
            live_backend_auth_executed=False,
        ),
        AuthContext(actor_id="ops-viewer", roles=("viewer",), auth_mode="dev_test", live_backend_auth_executed=False),
        AuthContext(
            actor_id="release-blocked",
            roles=(allowed_role,),
            auth_mode="live_idp",
            live_backend_auth_executed=False,
        ),
    ]
    modes: list[ExecutionMode] = ["policy_simulated", "policy_simulated", "release_blocked"]
    return [
        evaluate_policy(context, action_id, target_type=target_type, target_id=target_id, execution_mode=mode)
        for context, mode in zip(state, modes, strict=True)
    ]


def redact_audit_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text) or _URI_OR_PATH_KEY_RE.search(key_text):
                redacted[key_text] = _redact_sensitive_shape(nested)
            else:
                redacted[key_text] = redact_audit_payload(nested)
        return redacted
    if isinstance(value, list):
        return [redact_audit_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_audit_payload(item) for item in value)
    if isinstance(value, str):
        return _redact_text_shapes(value)
    return value


def _redact_sensitive_shape(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _redact_sensitive_shape(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive_shape(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_shape(item) for item in value)
    if value is None:
        return None
    return REDACTION_MARKER


def _redact_text_shapes(value: str) -> str:
    redacted = redact_text(value)
    if "://" in redacted or _LOCAL_PATH_RE.search(redacted) or _CHECKSUM_RE.search(redacted):
        return REDACTION_MARKER
    return redacted


def _record_decision(request: Request, decision: PolicyDecision, *, payload: Mapping[str, Any] | None) -> None:
    decisions = getattr(request.state, "auth_policy_decisions", None)
    if decisions is None:
        decisions = []
        request.state.auth_policy_decisions = decisions
    decisions.append(audit_record(decision, request_id=getattr(request.state, "request_id", None), payload=payload))


def _parse_roles(value: str) -> tuple[AuthRole, ...]:
    roles: list[AuthRole] = []
    for item in re.split(r"[, ]+", value.strip().lower()):
        if item in ROLE_VOCABULARY and item not in roles:
            roles.append(item)  # type: ignore[arg-type]
    return tuple(roles)


def _allow_dev_role_header() -> bool:
    return os.getenv("ALLOW_DEV_ROLE_HEADER", "").strip().lower() in _TRUTHY


def _production_mode() -> bool:
    return os.getenv("NHMS_AUTH_MODE", "").strip().lower() in {"production", "live", "live_idp"}


def _live_auth_proof_enabled() -> bool:
    return os.getenv("NHMS_LIVE_AUTH_PROOF_ACCEPTED", "").strip().lower() in _TRUTHY


def _live_auth_release_blocked() -> bool:
    auth_backend = os.getenv("AUTH_BACKEND", "").strip().lower()
    auth_mode = os.getenv("NHMS_AUTH_MODE", "").strip().lower()
    live_requested = auth_backend in {"live", "live_idp", "oidc", "saml"} or auth_mode in {"live", "live_idp"}
    return live_requested and not _live_auth_proof_enabled()
