"""Shared OpenAPI security-scheme and requirement-alternative definitions.

Shared definition owner consumed by the Slurm route/standalone surfaces: the
Slurm router (``services.slurm_gateway.routes``) builds its ``HTTPBearer``
description and ``openapi_extra`` from the values here, and the standalone
gateway app (``services.slurm_gateway.app``) publishes the schemes into its
custom OpenAPI so no referenced scheme dangles.

The full business API patcher (``apps.api.openapi_patching``) does NOT consume
this module: it keeps its existing private scheme helpers in place (that file
is a large-file exempt owner untouched in this issue), and the published full
API shape is pinned to these shared definitions by
``tests/test_slurm_gateway_openapi_security.py`` parity tests, not by shared
code.

Names and condition descriptions only: no configured credential or token value
is ever embedded here. Role vocabulary and action mappings stay in
``packages.common.auth_policy`` — this module must not grow a second policy
matrix.
"""

from __future__ import annotations

import copy
from typing import Any

DEV_ROLE_HEADER = "DevRoleHeader"
DEV_BEARER_TOKEN = "DevBearerToken"
INTERNAL_LIVE_PROOF = "InternalLiveProof"
LIVE_USER_ID = "LiveUserID"
LIVE_USER_ROLES = "LiveUserRoles"
SLURM_SERVICE_BEARER = "SlurmServiceBearer"

SECURITY_SCHEME_NAMES: tuple[str, ...] = (
    DEV_ROLE_HEADER,
    DEV_BEARER_TOKEN,
    INTERNAL_LIVE_PROOF,
    LIVE_USER_ID,
    LIVE_USER_ROLES,
    SLURM_SERVICE_BEARER,
)

SLURM_SERVICE_BEARER_DESCRIPTION = (
    "Route-scoped Slurm gateway service bearer. Accepted only at the "
    "Slurm mutation operations (job submit, array submit, job cancel, "
    "enabled registry reset) when the configured "
    "SLURM_GATEWAY_SERVICE_TOKEN is present and the bearer constant-time "
    "matches it; the credential authenticates the fixed scheduler actor "
    "with operator role and is never accepted by business mutation "
    "operations. The configured token value is never embedded in this "
    "document."
)

# Alternative requirement objects are OR; the live-proof leg is a single object
# requiring the proof token AND the live user id AND roles headers.
_IDENTITY_SECURITY_ALTERNATIVES: tuple[dict[str, list[str]], ...] = (
    {DEV_ROLE_HEADER: []},
    {DEV_BEARER_TOKEN: []},
    {INTERNAL_LIVE_PROOF: [], LIVE_USER_ID: [], LIVE_USER_ROLES: []},
)


def identity_security_alternatives() -> list[dict[str, list[str]]]:
    """Fresh copies of the exact three existing identity alternatives."""
    return copy.deepcopy(list(_IDENTITY_SECURITY_ALTERNATIVES))


def slurm_mutation_security_alternatives() -> list[dict[str, list[str]]]:
    """Fresh copies of the exact four Slurm mutation alternatives (bearer first)."""
    bearer_first: list[dict[str, list[str]]] = [{SLURM_SERVICE_BEARER: []}]
    bearer_first.extend(identity_security_alternatives())
    return bearer_first


def dev_role_header_scheme() -> dict[str, Any]:
    return {
        "type": "apiKey",
        "in": "header",
        "name": "X-User-Role",
        "description": (
            "Non-production development role header. Accepted only when "
            "AUTH_BACKEND is not live/live_idp/oidc/saml, ALLOW_DEV_ROLE_HEADER "
            "is enabled, and NHMS_AUTH_MODE is not production/live/live_idp; "
            "grants roles directly without a token."
        ),
    }


def dev_bearer_token_scheme() -> dict[str, Any]:
    return {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Non-production bearer token matching the configured NHMS_DEV_AUTH_TOKEN, "
            "accepted only when AUTH_BACKEND is not live/live_idp/oidc/saml, that "
            "token is configured, and NHMS_AUTH_MODE is not production/live/live_idp."
        ),
    }


def internal_live_proof_scheme() -> dict[str, Any]:
    return {
        "type": "apiKey",
        "in": "header",
        "name": "X-NHMS-Internal-Live-Proof",
        "description": (
            "Non-production internal live-proof token for the test_internal trusted "
            "live-proof mode. Required together with X-Live-User-ID and "
            "X-Live-User-Roles when AUTH_BACKEND is live/live_idp/oidc/saml, "
            "NHMS_TRUSTED_LIVE_PROOF_MODE is test_internal, the "
            "NHMS_INTERNAL_LIVE_PROOF_TOKEN is configured, and NHMS_AUTH_MODE is not "
            "production/live/live_idp. When NHMS_AUTH_MODE is live/live_idp the "
            "internal proof is not accepted and requests remain release-blocked; "
            "this is a test-mode credential, not a production identity-provider "
            "token. The configured token value is never embedded in this document."
        ),
    }


def live_user_id_scheme() -> dict[str, Any]:
    return {
        "type": "apiKey",
        "in": "header",
        "name": "X-Live-User-ID",
        "description": (
            "Non-production live identity actor id. Supplied together with "
            "X-NHMS-Internal-Live-Proof and X-Live-User-Roles under the same "
            "test_internal live-proof conditions."
        ),
    }


def live_user_roles_scheme() -> dict[str, Any]:
    return {
        "type": "apiKey",
        "in": "header",
        "name": "X-Live-User-Roles",
        "description": (
            "Non-production live identity role list. Supplied together with "
            "X-NHMS-Internal-Live-Proof and X-Live-User-ID under the same "
            "test_internal live-proof conditions."
        ),
    }


def slurm_service_bearer_scheme() -> dict[str, Any]:
    """The FastAPI-native HTTPBearer shape (type/description/scheme order)."""
    return {
        "type": "http",
        "description": SLURM_SERVICE_BEARER_DESCRIPTION,
        "scheme": "bearer",
    }


def security_scheme_definitions() -> dict[str, dict[str, Any]]:
    """Fresh copies of the six published security scheme definitions."""
    return {
        DEV_ROLE_HEADER: dev_role_header_scheme(),
        DEV_BEARER_TOKEN: dev_bearer_token_scheme(),
        INTERNAL_LIVE_PROOF: internal_live_proof_scheme(),
        LIVE_USER_ID: live_user_id_scheme(),
        LIVE_USER_ROLES: live_user_roles_scheme(),
        SLURM_SERVICE_BEARER: slurm_service_bearer_scheme(),
    }
