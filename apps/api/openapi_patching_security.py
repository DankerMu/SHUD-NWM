"""Published OpenAPI security boundary: the schemes and the protected-operation map.

Split out of `apps/api/openapi_patching.py` (#2074). `PROTECTED_OPERATION_OVERRIDES`
holds `_PROTECTED_OPERATION_SECURITY` BY REFERENCE, so the two values stay in the
same module: splitting them would replace the shared list object with a copy.
`_publish_security_boundary` is re-exported by the facade because
`tests/test_slurm_gateway_openapi_security.py` calls it through `openapi_patching`.
"""

import copy

# Alternative requirement objects are OR; the live-proof leg is a single object
# requiring the proof token AND the live user id AND roles headers.
_PROTECTED_OPERATION_SECURITY: list[dict] = [
    {"DevRoleHeader": []},
    {"DevBearerToken": []},
    {"InternalLiveProof": [], "LiveUserID": [], "LiveUserRoles": []},
]

# Exactly the operations enforced by apps/api/main.protected_mutation_auth_guard
# (registry mutations, model active/lifecycle/preflight) or the pipeline
# retry/cancel require_action dependencies.
PROTECTED_OPERATION_OVERRIDES: dict[tuple[str, str], list[dict]] = {
    ("POST", "/api/v1/basins"): _PROTECTED_OPERATION_SECURITY,
    ("POST", "/api/v1/basins/{basin_id}/versions"): _PROTECTED_OPERATION_SECURITY,
    ("POST", "/api/v1/river-networks"): _PROTECTED_OPERATION_SECURITY,
    ("POST", "/api/v1/mesh-versions"): _PROTECTED_OPERATION_SECURITY,
    ("POST", "/api/v1/models"): _PROTECTED_OPERATION_SECURITY,
    ("POST", "/api/v1/models/{model_id}/preflight"): _PROTECTED_OPERATION_SECURITY,
    ("POST", "/api/v1/models/{model_id}/lifecycle"): _PROTECTED_OPERATION_SECURITY,
    ("POST", "/api/v1/river-segment-crosswalks"): _PROTECTED_OPERATION_SECURITY,
    ("PUT", "/api/v1/models/{model_id}/active"): _PROTECTED_OPERATION_SECURITY,
    ("POST", "/api/v1/runs/{run_id}/retry"): _PROTECTED_OPERATION_SECURITY,
    ("POST", "/api/v1/runs/{run_id}/cancel"): _PROTECTED_OPERATION_SECURITY,
}


def _publish_security_boundary(schema: dict) -> None:
    """Overwrite the published server/security truth, never keep a wrong value.

    The finalizer is the current authority for the same-origin server, the
    explicit anonymous-by-default root security, and the conditional credential
    schemes. Exact assignment (not ``setdefault``) means a pre-existing wrong
    ``servers: []``, a false global bearer root, or a stale same-name scheme is
    replaced by the fixture-pinned truth rather than silently preserved. Each
    value is a deep copy so the deterministic YAML dump cannot alias shared
    objects.
    """
    schema["servers"] = [{"url": "/", "description": "Same-origin API endpoint."}]
    schema["security"] = []
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes["DevRoleHeader"] = _dev_role_header_scheme()
    schemes["DevBearerToken"] = _dev_bearer_token_scheme()
    schemes["InternalLiveProof"] = _internal_live_proof_scheme()
    schemes["LiveUserID"] = _live_user_id_scheme()
    schemes["LiveUserRoles"] = _live_user_roles_scheme()
    for (method, path), requirements in PROTECTED_OPERATION_OVERRIDES.items():
        operation = schema.get("paths", {}).get(path, {}).get(method.lower())
        if operation is not None:
            # Each operation gets its own deep copy so a consumer mutating one
            # operation's security requirement cannot corrupt the others, and so
            # the deterministic YAML dump does not alias the shared empty lists.
            operation["security"] = copy.deepcopy(requirements)


def _dev_role_header_scheme() -> dict:
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


def _dev_bearer_token_scheme() -> dict:
    return {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Non-production bearer token matching the configured NHMS_DEV_AUTH_TOKEN, "
            "accepted only when AUTH_BACKEND is not live/live_idp/oidc/saml, that "
            "token is configured, and NHMS_AUTH_MODE is not production/live/live_idp."
        ),
    }


def _internal_live_proof_scheme() -> dict:
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


def _live_user_id_scheme() -> dict:
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


def _live_user_roles_scheme() -> dict:
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
