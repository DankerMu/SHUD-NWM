"""Reusable request-auth construction shared below the API layer.

This module owns:

- the route-scoped Slurm scheduler service bearer credential
  (``SLURM_GATEWAY_SERVICE_TOKEN``): fail-closed validation, constant-time
  comparison, and derivation of the fixed scheduler ``AuthContext``;
- the existing dev/test and live-proof request-auth context construction that
  used to live in ``apps.api.auth``. ``apps.api.auth`` keeps its public facade
  and re-exports these names, but the construction itself is reusable by
  ``services.slurm_gateway`` (which MUST NOT import ``apps.api``).

No credential value is ever logged, serialized, repr'd, or emitted into
evidence/OpenAPI here; only the environment variable name and the redaction
marker may appear.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from typing import Any

from packages.common.auth_policy import (
    ROLE_VOCABULARY,
    AuthContext,
    AuthRole,
    _parse_roles,
    _raw_roles,
    _role_mapping_result,
)
from packages.common.redaction import REDACTION_MARKER

SLURM_GATEWAY_SERVICE_TOKEN_ENV = "SLURM_GATEWAY_SERVICE_TOKEN"
SLURM_SERVICE_ACTOR = "slurm-scheduler"
SLURM_SERVICE_ROLES: tuple[AuthRole, ...] = ("operator",)
SERVICE_TOKEN_MIN_LENGTH = 16

_TRUTHY = {"1", "true", "yes", "on"}
_LIVE_AUTH_BACKENDS = {"live", "live_idp", "oidc", "saml"}


def read_configured_service_token(env: Mapping[str, str] | None = None) -> str | None:
    """Return the configured service token or ``None`` when not usable.

    Fail closed: missing, empty, whitespace-bearing (any ``isspace()`` byte
    anywhere, including leading/trailing — the raw value is never trimmed), or
    shorter than ``SERVICE_TOKEN_MIN_LENGTH`` credentials are treated as not
    configured. A legitimate token is returned byte-for-byte unchanged so the
    constant-time comparison sees exactly what was configured.
    """
    raw = _env_value(env, SLURM_GATEWAY_SERVICE_TOKEN_ENV)
    if not _is_valid_token(raw):
        return None
    return raw


def service_token_configured(env: Mapping[str, str] | None = None) -> bool:
    return read_configured_service_token(env) is not None


def service_bearer_matches(request: Any, env: Mapping[str, str] | None = None) -> bool:
    """Constant-time comparison of ``Authorization: Bearer ...`` to the token."""
    configured = read_configured_service_token(env)
    if configured is None:
        return False
    provided = request.headers.get("Authorization", "")
    return hmac.compare_digest(provided, f"Bearer {configured}")


def slurm_service_auth_context(request: Any, env: Mapping[str, str] | None = None) -> AuthContext | None:
    """Derive the fixed scheduler service ``AuthContext`` from the bearer.

    Only a constant-time match of a configured, valid service token produces a
    context; anything else returns ``None`` so the route can fall through to the
    existing identity modes (which never accept this credential).
    """
    if not service_bearer_matches(request, env):
        return None
    return AuthContext(
        actor_id=SLURM_SERVICE_ACTOR,
        roles=SLURM_SERVICE_ROLES,
        auth_mode="slurm_service",
        live_backend_auth_executed=False,
        provider_metadata={
            "credential_header": REDACTION_MARKER,
            "scope": "slurm_gateway_mutations",
        },
    )


def auth_context_from_request(request: Any, env: Mapping[str, str] | None = None) -> AuthContext | None:
    """Build the request auth context from the existing dev/live identity modes.

    This is the construction formerly owned by ``apps.api.auth``; behavior is
    preserved exactly. The Slurm service bearer is deliberately NOT accepted
    here: it must never authenticate an original business mutation.
    """
    if _live_auth_requested(env):
        if not _internal_live_proof_token_matches(request, env):
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

    if _allow_dev_role_header(env) and not _production_mode(env) and "X-User-Role" in request.headers:
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

    configured_token = _env_value(env, "NHMS_DEV_AUTH_TOKEN").strip()
    authorization = request.headers.get("Authorization", "")
    if configured_token and authorization == f"Bearer {configured_token}" and not _production_mode(env):
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


def slurm_mutation_auth_context(request: Any, env: Mapping[str, str] | None = None) -> AuthContext | None:
    """Route-scoped context for Slurm mutations only.

    The scheduler service bearer is tried first; when absent, the existing
    dev/test/live-proof identity modes remain available under their existing
    environment gates (including the release-blocked live mode).
    """
    service_context = slurm_service_auth_context(request, env)
    if service_context is not None:
        return service_context
    return auth_context_from_request(request, env)


def record_request_policy_decision(
    request: Any,
    decision: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Append one redacted audit record to ``request.state.auth_policy_decisions``.

    ``request_id``, when provided by the caller, is the single stable id already
    stored on the request state by the route's request-id owner; otherwise the
    existing state/header fallback is used. Both paths must agree so the
    policy/audit record id matches the response body/header id exactly.
    """
    from packages.common.auth_policy import audit_record

    decisions = getattr(request.state, "auth_policy_decisions", None)
    if decisions is None:
        decisions = []
        request.state.auth_policy_decisions = decisions
    resolved_request_id = request_id or getattr(request.state, "request_id", None) or request.headers.get(
        "X-Request-ID"
    )
    decisions.append(audit_record(decision, request_id=resolved_request_id, payload=payload))


def _is_valid_token(value: str) -> bool:
    return len(value) >= SERVICE_TOKEN_MIN_LENGTH and not any(character.isspace() for character in value)


def _env_value(env: Mapping[str, str] | None, key: str) -> str:
    if env is None:
        return os.getenv(key, "") or ""
    return str(env.get(key, "") or "")


def _allow_dev_role_header(env: Mapping[str, str] | None = None) -> bool:
    return _env_value(env, "ALLOW_DEV_ROLE_HEADER").strip().lower() in _TRUTHY


def _production_mode(env: Mapping[str, str] | None = None) -> bool:
    return _env_value(env, "NHMS_AUTH_MODE").strip().lower() in {"production", "live", "live_idp"}


def _live_auth_requested(env: Mapping[str, str] | None = None) -> bool:
    auth_backend = _env_value(env, "AUTH_BACKEND").strip().lower()
    auth_mode = _env_value(env, "NHMS_AUTH_MODE").strip().lower()
    return auth_backend in _LIVE_AUTH_BACKENDS or auth_mode in {"live", "live_idp"}


def _release_blocked_auth_context() -> AuthContext:
    return AuthContext(
        actor_id="release-blocked",
        roles=(),
        auth_mode="live_idp",
        live_backend_auth_executed=False,
    )


def _trusted_live_auth_proof_enabled(env: Mapping[str, str] | None = None) -> bool:
    return _env_value(env, "NHMS_TRUSTED_LIVE_PROOF_MODE").strip().lower() == "test_internal"


def _trusted_live_auth_proof_available(env: Mapping[str, str] | None = None) -> bool:
    token = _env_value(env, "NHMS_INTERNAL_LIVE_PROOF_TOKEN").strip()
    return _trusted_live_auth_proof_enabled(env) and bool(token) and not _production_mode(env)


def _internal_live_proof_token_matches(request: Any, env: Mapping[str, str] | None = None) -> bool:
    if not _trusted_live_auth_proof_available(env):
        return False
    configured_token = _env_value(env, "NHMS_INTERNAL_LIVE_PROOF_TOKEN").strip()
    return request.headers.get("X-NHMS-Internal-Live-Proof", "") == configured_token
