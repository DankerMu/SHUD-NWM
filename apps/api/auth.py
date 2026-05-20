from __future__ import annotations

import os
import re
from collections.abc import Sequence
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
POLICY_CONFIG_ERROR = "POLICY_CONFIG_ERROR"
POLICY_ACTION_UNKNOWN = "POLICY_ACTION_UNKNOWN"

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
    provider_metadata: Mapping[str, Any] | None = None
    role_mapping_result: Mapping[str, Any] | None = None


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
    provider_metadata: Mapping[str, Any] | None = None
    role_mapping_result: Mapping[str, Any] | None = None

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
            "provider_metadata": redact_audit_payload(dict(self.provider_metadata or {})),
            "role_mapping_result": redact_audit_payload(dict(self.role_mapping_result or {})),
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


def evaluate_policy(
    context: AuthContext | None,
    action_id: str,
    *,
    target_type: str,
    target_id: str,
    execution_mode: ExecutionMode | None = None,
) -> PolicyDecision:
    required_roles = ACTION_MATRIX.get(action_id)
    if required_roles is None:
        return PolicyDecision(
            action_id=action_id,
            decision="deny",
            required_roles=(),
            matched_roles=(),
            actor_id=context.actor_id if context is not None else "anonymous",
            target_type=target_type,
            target_id=target_id,
            reason="Protected action is not registered in the canonical RBAC matrix.",
            reason_code=POLICY_ACTION_UNKNOWN,
            roles=context.roles if context is not None else (),
            execution_mode=execution_mode or "backend_route_executed",
            no_mutation_expected=True,
            auth_mode=context.auth_mode if context is not None else "none",
            live_backend_auth_executed=context.live_backend_auth_executed if context is not None else False,
            provider_metadata=context.provider_metadata if context is not None else None,
            role_mapping_result=context.role_mapping_result if context is not None else None,
        )
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
            provider_metadata=context.provider_metadata,
            role_mapping_result=context.role_mapping_result,
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
            provider_metadata=None,
            role_mapping_result=None,
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
            provider_metadata=context.provider_metadata,
            role_mapping_result=context.role_mapping_result,
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
        provider_metadata=context.provider_metadata,
        role_mapping_result=context.role_mapping_result,
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
            "provider_metadata": dict(getattr(decision, "provider_metadata", None) or {}),
            "role_mapping_result": dict(getattr(decision, "role_mapping_result", None) or {}),
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


def cli_policy_decision_from_evidence(
    action_id: str,
    *,
    target_type: str,
    target_id: str,
    actor_id: str | None = None,
    roles: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> PolicyDecision | None:
    """Build deterministic dev/test CLI policy evidence from explicit flags or env."""
    source_env = os.environ if env is None else env
    raw_actor = (actor_id or source_env.get("NHMS_CLI_AUTH_ACTOR_ID", "")).strip()
    raw_role_values = [str(role) for role in roles or () if str(role).strip()]
    if not raw_role_values:
        env_roles = source_env.get("NHMS_CLI_AUTH_ROLES", "")
        raw_role_values = [env_roles] if env_roles.strip() else []
    if not raw_actor and not raw_role_values:
        return None

    raw_roles_text = ",".join(raw_role_values)
    mapped_roles = _parse_roles(raw_roles_text)
    raw_roles = _raw_roles(raw_roles_text)
    role_mapping_result = _role_mapping_result(
        raw_roles,
        mapped_roles=mapped_roles,
        input_present=bool(raw_role_values),
    )
    blocked_auth_mode = _cli_dev_test_blocking_auth_mode(source_env)
    if blocked_auth_mode is not None:
        return PolicyDecision(
            action_id=action_id,
            decision="release_blocked",
            required_roles=ACTION_MATRIX.get(action_id, ()),
            matched_roles=(),
            actor_id=raw_actor or "cli:missing-actor",
            target_type=target_type,
            target_id=target_id,
            reason=(
                "Deterministic CLI dev/test policy evidence is blocked while production/live auth mode is configured."
            ),
            reason_code=RELEASE_BLOCKED,
            roles=mapped_roles,
            execution_mode="release_blocked",
            no_mutation_expected=True,
            auth_mode=f"cli_dev_test_blocked_by_{blocked_auth_mode}",
            live_backend_auth_executed=False,
            provider_metadata=None,
            role_mapping_result=role_mapping_result,
        )
    if not raw_actor or not mapped_roles:
        return PolicyDecision(
            action_id=action_id,
            decision="deny",
            required_roles=ACTION_MATRIX.get(action_id, ()),
            matched_roles=(),
            actor_id=raw_actor or "cli:missing-actor",
            target_type=target_type,
            target_id=target_id,
            reason="CLI auth evidence must include an actor id and at least one known role.",
            reason_code=RBAC_FORBIDDEN,
            roles=mapped_roles,
            execution_mode="backend_route_executed",
            no_mutation_expected=True,
            auth_mode="cli_dev_test",
            live_backend_auth_executed=False,
            provider_metadata=None,
            role_mapping_result=role_mapping_result,
        )
    context = AuthContext(
        actor_id=raw_actor or "cli:missing-actor",
        roles=mapped_roles,
        auth_mode="cli_dev_test",
        live_backend_auth_executed=False,
        role_mapping_result=role_mapping_result,
    )
    return evaluate_policy(context, action_id, target_type=target_type, target_id=target_id)


def trusted_internal_policy_decision(
    action_id: str,
    *,
    target_type: str,
    target_id: str,
    actor_id: str = "trusted-internal",
    roles: tuple[AuthRole, ...] = ("sys_admin",),
) -> PolicyDecision:
    context = AuthContext(
        actor_id=actor_id,
        roles=roles,
        auth_mode="trusted_internal",
        live_backend_auth_executed=False,
    )
    return evaluate_policy(context, action_id, target_type=target_type, target_id=target_id)


def require_policy_evidence(
    decision: PolicyDecision | None,
    *,
    action_id: str,
    target_type: str,
    target_id: str,
) -> PolicyDecision:
    if decision is None:
        return evaluate_policy(None, action_id, target_type=target_type, target_id=target_id)
    if (
        decision.action_id != action_id
        or decision.target_type != target_type
        or decision.target_id != target_id
    ):
        return PolicyDecision(
            action_id=action_id,
            decision="deny",
            required_roles=ACTION_MATRIX.get(action_id, ()),
            matched_roles=(),
            actor_id=decision.actor_id,
            target_type=target_type,
            target_id=target_id,
            reason="Policy evidence does not authorize this protected mutation.",
            reason_code=RBAC_FORBIDDEN,
            roles=decision.roles,
            execution_mode=decision.execution_mode,
            no_mutation_expected=True,
            auth_mode=decision.auth_mode,
            live_backend_auth_executed=decision.live_backend_auth_executed,
            provider_metadata=decision.provider_metadata,
            role_mapping_result=decision.role_mapping_result,
        )
    return decision


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


def _raw_roles(value: str) -> tuple[str, ...]:
    roles: list[str] = []
    for item in re.split(r"[, ]+", value.strip().lower()):
        if item and item not in roles:
            roles.append(item)
    return tuple(roles)


def _role_mapping_result(
    raw_roles: tuple[str, ...],
    *,
    mapped_roles: tuple[AuthRole, ...],
    input_present: bool,
) -> dict[str, Any]:
    return {
        "raw_roles_present": bool(raw_roles),
        "raw_roles_input_present": input_present,
        "raw_roles": raw_roles,
        "mapped_roles": mapped_roles,
        "unmapped_roles": tuple(role for role in raw_roles if role not in ROLE_VOCABULARY),
        "mapping_status": "mapped" if mapped_roles else "unmapped",
    }


def _allow_dev_role_header() -> bool:
    return os.getenv("ALLOW_DEV_ROLE_HEADER", "").strip().lower() in _TRUTHY


def _production_mode() -> bool:
    return os.getenv("NHMS_AUTH_MODE", "").strip().lower() in {"production", "live", "live_idp"}


def _cli_dev_test_blocking_auth_mode(env: Mapping[str, str]) -> str | None:
    auth_mode = env.get("NHMS_AUTH_MODE", "").strip().lower()
    if auth_mode in {"production", "live", "live_idp"}:
        return auth_mode
    auth_backend = env.get("AUTH_BACKEND", "").strip().lower()
    if auth_backend in {"oidc", "live", "live_idp"}:
        return f"auth_backend_{auth_backend}"
    return None


def _live_auth_requested() -> bool:
    auth_backend = os.getenv("AUTH_BACKEND", "").strip().lower()
    auth_mode = os.getenv("NHMS_AUTH_MODE", "").strip().lower()
    return auth_backend in {"live", "live_idp", "oidc", "saml"} or auth_mode in {"live", "live_idp"}


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
