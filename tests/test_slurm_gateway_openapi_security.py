"""Published security-boundary contract for the NHMS OpenAPI.

Owns the security/server metadata half of the 3.1-contract suite: the exact
protected-operation set (the eleven business mutations plus the four Slurm
mutations), the per-operation security requirement alternatives including the
exact order (service bearer first on the Slurm mutations), the scheme shapes
and their environment-gating descriptions, the same-origin server and
anonymous root, and the mutation proofs that every one of those invariants
reddens when violated. The nullable/dialect/generated-type half of the 3.1
contract stays in ``tests/test_openapi_31_contract.py``, which also exports the
shared runtime/static helpers this module imports.

``tests/test_openapi_drift.py`` remains the exact-equality owner of the static
YAML against the runtime schema; this module asserts the security truth from
both sources. The complete Slurm scheme and per-operation security list are
published by FastAPI from the route-level ``HTTPBearer`` dependency plus
``openapi_extra`` in ``services/slurm_gateway/routes.py`` and are preserved by
the ``openapi_patching`` pipeline, so the real route-generated pre-finalized
schema already carries them; ``_publish_security_boundary`` owns only the
business-mutation security boundary.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import openapi_patching
from apps.api.main import _PRE_BODY_PROTECTED_MUTATIONS
from apps.api.routes import pipeline as pipeline_routes
from packages.common.openapi_auth_security import security_scheme_definitions
from tests.test_openapi_31_contract import (
    _client,
    _enable_live_proof,
    _pre_finalized_runtime_schema,
    _runtime_operation_paths,
    _static_spec,
)

# The two enforcement surfaces the security metadata must mirror:
# apps/api/main.protected_mutation_auth_guard (pre-body guard, static map plus
# the dynamic basin-version and model-active pattern matches) plus the pipeline
# retry/cancel require_action dependencies.
ENFORCED_MUTATIONS = {
    (method, path)
    for (method, path) in _PRE_BODY_PROTECTED_MUTATIONS
    if path.startswith("/api/v1/")
}
ENFORCED_MUTATIONS.update(
    {
        # Enforced by the dynamic pattern matches in main._protected_mutation_policy.
        ("POST", "/api/v1/basins/{basin_id}/versions"),
        ("PUT", "/api/v1/models/{model_id}/active"),
        ("POST", "/api/v1/models/{model_id}/preflight"),
        ("POST", "/api/v1/models/{model_id}/lifecycle"),
        ("POST", "/api/v1/runs/{run_id}/retry"),
        ("POST", "/api/v1/runs/{run_id}/cancel"),
    }
)

PROTECTED_OVERRIDE_KEYS = [
    {"DevRoleHeader": []},
    {"DevBearerToken": []},
    {"InternalLiveProof": [], "LiveUserID": [], "LiveUserRoles": []},
]

SLURM_PROTECTED_OVERRIDE_KEYS = [
    # Exact runtime order: FastAPI emits the route-level HTTPBearer dependency
    # security entry first, then appends the openapi_extra alternatives.
    {"SlurmServiceBearer": []},
    *PROTECTED_OVERRIDE_KEYS,
]

LIVE_PROOF_AND_GROUP = {"InternalLiveProof": [], "LiveUserID": [], "LiveUserRoles": []}

# Exactly the four Slurm mutations enforced by the shared Slurm router auth
# dependency across every supported HTTP method (including DELETE).
SLURM_ENFORCED_MUTATIONS = {
    ("POST", "/api/v1/slurm/jobs"),
    ("POST", "/api/v1/slurm/job-arrays"),
    ("DELETE", "/api/v1/slurm/jobs/{job_id}"),
    ("POST", "/api/v1/slurm/internal/reset"),
}


def test_security_boundary_overwrites_wrong_pre_existing_values() -> None:
    # The finalizer is the current authority: a pre-existing `servers: []`,
    # a false global bearer root, or a stale same-name scheme must be replaced
    # by the fixture-pinned truth, not silently preserved by setdefault. The
    # handcrafted fixture covers only the business-mutation boundary owned by
    # _publish_security_boundary; the complete Slurm scheme/security comes from
    # the real route-generated pre-finalized schema (see
    # test_openapi_contract_offenders_clean_after_finalizer_wiring).
    schema = {
        "servers": [],
        "security": [{"DevBearerToken": []}],
        "components": {
            "securitySchemes": {
                "DevRoleHeader": {"type": "apiKey", "name": "WRONG", "in": "header"},
            }
        },
        "paths": {
            "/api/v1/basins": {"post": {}},
            "/api/v1/runs/{run_id}/retry": {"post": {}},
        },
    }
    openapi_patching._publish_security_boundary(schema)

    assert schema["servers"] == [{"url": "/", "description": "Same-origin API endpoint."}]
    assert schema["security"] == []
    assert schema["components"]["securitySchemes"]["DevRoleHeader"]["name"] == "X-User-Role"
    assert schema["components"]["securitySchemes"]["DevRoleHeader"]["in"] == "header"
    assert set(schema["components"]["securitySchemes"]) == {
        "DevRoleHeader",
        "DevBearerToken",
        "InternalLiveProof",
        "LiveUserID",
        "LiveUserRoles",
    }
    # Each operation override is an independent deep copy (no shared alias).
    basins_security = schema["paths"]["/api/v1/basins"]["post"]["security"]
    retry_security = schema["paths"]["/api/v1/runs/{run_id}/retry"]["post"]["security"]
    assert basins_security == PROTECTED_OVERRIDE_KEYS
    assert retry_security == PROTECTED_OVERRIDE_KEYS
    assert basins_security is not retry_security


def test_same_origin_server_and_root_security() -> None:
    spec = _static_spec()
    assert spec["servers"] == [{"url": "/", "description": "Same-origin API endpoint."}]
    assert spec.get("security") == []


def test_security_scheme_shapes_and_no_credentials() -> None:
    spec = _static_spec()
    schemes = spec["components"]["securitySchemes"]

    assert schemes["DevRoleHeader"] == {
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
    assert schemes["DevBearerToken"]["type"] == "http"
    assert schemes["DevBearerToken"]["scheme"] == "bearer"
    assert schemes["InternalLiveProof"]["type"] == "apiKey"
    assert schemes["InternalLiveProof"]["in"] == "header"
    assert schemes["InternalLiveProof"]["name"] == "X-NHMS-Internal-Live-Proof"
    assert schemes["LiveUserID"]["name"] == "X-Live-User-ID"
    assert schemes["LiveUserRoles"]["name"] == "X-Live-User-Roles"
    assert schemes["SlurmServiceBearer"]["type"] == "http"
    assert schemes["SlurmServiceBearer"]["scheme"] == "bearer"
    assert "SLURM_GATEWAY_SERVICE_TOKEN" in schemes["SlurmServiceBearer"]["description"]
    assert "never accepted by business mutation operations" in schemes["SlurmServiceBearer"]["description"]

    serialized = json.dumps(spec)
    for secret_token in ("proof-token", "NHMS_INTERNAL_LIVE_PROOF_TOKEN=", "Bearer secret", "dev-test:"):
        assert secret_token not in serialized


def test_dev_scheme_descriptions_gate_all_three_production_modes_and_live_backend() -> None:
    # _production_mode() in apps/api/auth.py includes production, live, and
    # live_idp, and auth_context_from_request() routes AUTH_BACKEND in
    # live/live_idp/oidc/saml straight to the live/release-blocked branch — so
    # the dev role header and dev bearer token are unavailable in all three
    # production modes AND whenever a live auth backend is configured, even with
    # an empty NHMS_AUTH_MODE. The descriptions must say both gates.
    schemes = _static_spec()["components"]["securitySchemes"]

    dev_role = schemes["DevRoleHeader"]["description"]
    assert "AUTH_BACKEND is not live/live_idp/oidc/saml" in dev_role
    assert "ALLOW_DEV_ROLE_HEADER is enabled" in dev_role
    assert "NHMS_AUTH_MODE is not production/live/live_idp" in dev_role

    dev_bearer = schemes["DevBearerToken"]["description"]
    assert "AUTH_BACKEND is not live/live_idp/oidc/saml" in dev_bearer
    assert "NHMS_DEV_AUTH_TOKEN" in dev_bearer
    assert "that token is configured" in dev_bearer
    assert "NHMS_AUTH_MODE is not production/live/live_idp" in dev_bearer


def test_dev_credentials_unreachable_when_live_backend_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # auth_context_from_request() checks _live_auth_requested() first, so with
    # AUTH_BACKEND=oidc (and otherwise-enabled dev role header) the request must
    # go down the live/release-blocked path — the dev credential is unreachable
    # even though NHMS_AUTH_MODE is empty. Observable: 503 RELEASE_BLOCKED, no
    # mutation.
    from tests.test_monitoring_api import GENERIC_RETRY_JOB_TYPE, _create_job, _MockGateway, _store

    monkeypatch.setenv("AUTH_BACKEND", "oidc")
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    monkeypatch.setenv("NHMS_TRUSTED_LIVE_PROOF_MODE", "test_internal")
    monkeypatch.setenv("NHMS_INTERNAL_LIVE_PROOF_TOKEN", "proof-token")

    with _store() as store:
        _create_job(
            store,
            job_id="job_live_backend_dev_unreachable",
            run_id="run_live_backend_dev_unreachable",
            job_type=GENERIC_RETRY_JOB_TYPE,
            stage="forecast",
            status="failed",
        )
        gateway = _MockGateway()
        with _client(store, gateway) as client:
            response = client.post(
                "/api/v1/runs/run_live_backend_dev_unreachable/retry",
                headers={"X-User-Role": "operator"},
            )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RELEASE_BLOCKED"
    assert gateway.submissions == []


def test_internal_live_proof_description_matches_actual_gating() -> None:
    # _trusted_live_auth_proof_available() requires NHMS_TRUSTED_LIVE_PROOF_MODE
    # = test_internal, a configured NHMS_INTERNAL_LIVE_PROOF_TOKEN, and NOT
    # production mode. When NHMS_AUTH_MODE is live/live_idp, production mode is
    # true, so the internal proof is never accepted and requests stay
    # release-blocked. The description must state that boundary and must not
    # present the token as a production identity-provider credential.
    description = _static_spec()["components"]["securitySchemes"]["InternalLiveProof"]["description"]

    assert "AUTH_BACKEND is live/live_idp/oidc/saml" in description
    assert "NHMS_TRUSTED_LIVE_PROOF_MODE is test_internal" in description
    assert "NHMS_INTERNAL_LIVE_PROOF_TOKEN is configured" in description
    assert "NHMS_AUTH_MODE is not production/live/live_idp" in description
    assert "NHMS_AUTH_MODE is live/live_idp the internal proof is not accepted" in description
    assert "remain release-blocked" in description
    assert "not a production identity-provider token" in description


def test_live_user_header_descriptions_name_the_same_and_group_conditions() -> None:
    schemes = _static_spec()["components"]["securitySchemes"]

    for scheme in ("LiveUserID", "LiveUserRoles"):
        description = schemes[scheme]["description"]
        assert "X-NHMS-Internal-Live-Proof" in description
        assert "test_internal" in description
    assert "X-Live-User-ID" in schemes["LiveUserRoles"]["description"]
    assert "X-Live-User-Roles" in schemes["LiveUserID"]["description"]
    # X-Live-Provider stays optional: no required scheme is published for it.
    assert "X-Live-Provider" not in {
        scheme["name"] for scheme in schemes.values() if scheme.get("name")
    }


def test_nhms_auth_mode_live_release_blocks_despite_correct_internal_proof_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The description boundary is observable: with NHMS_AUTH_MODE=live the
    # internal proof token is not accepted even when every header matches, so
    # the request must stay release-blocked (503).
    from tests.test_monitoring_api import GENERIC_RETRY_JOB_TYPE, _create_job, _MockGateway, _store

    monkeypatch.setenv("AUTH_BACKEND", "oidc")
    monkeypatch.setenv("NHMS_AUTH_MODE", "live")
    monkeypatch.setenv("NHMS_TRUSTED_LIVE_PROOF_MODE", "test_internal")
    monkeypatch.setenv("NHMS_INTERNAL_LIVE_PROOF_TOKEN", "proof-token")
    monkeypatch.delenv("ALLOW_DEV_ROLE_HEADER", raising=False)

    with _store() as store:
        _create_job(
            store,
            job_id="job_live_mode_blocked",
            run_id="run_live_mode_blocked",
            job_type=GENERIC_RETRY_JOB_TYPE,
            stage="forecast",
            status="failed",
        )
        gateway = _MockGateway()
        with _client(store, gateway) as client:
            response = client.post(
                "/api/v1/runs/run_live_mode_blocked/retry",
                headers={
                    "X-NHMS-Internal-Live-Proof": "proof-token",
                    "X-Live-User-ID": "live-actor",
                    "X-Live-User-Roles": "operator",
                },
            )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RELEASE_BLOCKED"
    assert gateway.submissions == []


def test_exactly_fifteen_protected_operations_override_root_security() -> None:
    spec = _static_spec()
    overridden: set[tuple[str, str]] = set()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method.lower() not in {"get", "post", "put", "delete"}:
                continue
            if "security" in operation:
                overridden.add((method.upper(), path))
    assert overridden == ENFORCED_MUTATIONS | SLURM_ENFORCED_MUTATIONS


def test_live_proof_requirements_form_one_and_group() -> None:
    spec = _static_spec()
    for method, path in sorted(ENFORCED_MUTATIONS | SLURM_ENFORCED_MUTATIONS):
        operation = spec["paths"][path][method.lower()]
        requirements = operation["security"]
        expected = (
            SLURM_PROTECTED_OVERRIDE_KEYS
            if (method, path) in SLURM_ENFORCED_MUTATIONS
            else PROTECTED_OVERRIDE_KEYS
        )
        assert requirements == expected, (method, path)
        assert LIVE_PROOF_AND_GROUP in requirements
        # The live-proof leg is a single object with all three schemes (AND), not
        # three separate objects (which would be alternatives/OR).
        and_group = next(
            requirement for requirement in requirements if "InternalLiveProof" in requirement
        )
        assert set(and_group) == {"InternalLiveProof", "LiveUserID", "LiveUserRoles"}


def test_public_operations_inherit_root_anonymous_not_global_bearer() -> None:
    spec = _static_spec()
    overridden = {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if method.lower() in {"get", "post", "put", "delete"} and "security" in operation
    }
    public_operations = [
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method.lower() in {"get", "post", "put", "delete"}
    ]
    public_only = [pair for pair in public_operations if pair not in overridden]
    assert public_only, "expected at least one public operation"
    for method, path in public_only:
        assert "security" not in spec["paths"][path][method.lower()], (method, path)
    # A public operation must not inherit a global bearer requirement: root is
    # explicitly anonymous.
    assert spec.get("security") == []


def test_protected_operation_set_binds_to_enforcement() -> None:
    # The documented protected set must equal the enforced surfaces. The
    # pre-body guard covers the registry mutations, the model active/
    # lifecycle/preflight plus pipeline retry/cancel are covered by the
    # require_action dependencies, and the four Slurm mutations are covered by
    # the shared Slurm router dependency. This binding reddens if a route is
    # added or removed from enforcement without updating the OpenAPI override
    # set, across every HTTP method including DELETE.
    runtime_paths = _runtime_operation_paths()
    for method, path in ENFORCED_MUTATIONS | SLURM_ENFORCED_MUTATIONS:
        assert (method, path) in runtime_paths, (method, path)
    documented = {
        (method.upper(), path)
        for path, operations in _static_spec()["paths"].items()
        for method, operation in operations.items()
        if method.lower() in {"get", "post", "put", "delete"} and "security" in operation
    }
    assert documented == ENFORCED_MUTATIONS | SLURM_ENFORCED_MUTATIONS


def _openapi_contract_offenders(spec: dict[str, Any]) -> list[str]:
    """Every published-contract invariant violation in ``spec``, concretely named.

    One owner for the whole contract truth so the positive tests, the mutation
    proofs below, and any future runtime/static real tests share the same
    invariant check instead of scattering assertions. Returns a list of
    human-readable violations; the clean published contract yields an empty list.
    """
    offenders: list[str] = []

    if spec.get("openapi") != "3.1.0":
        offenders.append(f"declared dialect is not OpenAPI 3.1.0: {spec.get('openapi')!r}")
    if "nullable" in json.dumps(spec):
        offenders.append("schema still contains an OpenAPI-3.0-only nullable keyword")

    expected_servers = [{"url": "/", "description": "Same-origin API endpoint."}]
    if spec.get("servers") != expected_servers:
        offenders.append(f"servers is not the exact same-origin entry: {spec.get('servers')!r}")
    if spec.get("security") != []:
        offenders.append(f"root security is not explicit anonymous: {spec.get('security')!r}")

    schemes = spec.get("components", {}).get("securitySchemes", {})
    expected_scheme_names = {
        "DevRoleHeader",
        "DevBearerToken",
        "InternalLiveProof",
        "LiveUserID",
        "LiveUserRoles",
        "SlurmServiceBearer",
    }
    if set(schemes) != expected_scheme_names:
        offenders.append(f"securitySchemes set is {sorted(schemes)}, expected {sorted(expected_scheme_names)}")
    for name in (
        "DevRoleHeader",
        "DevBearerToken",
        "InternalLiveProof",
        "LiveUserID",
        "LiveUserRoles",
        "SlurmServiceBearer",
    ):
        scheme = schemes.get(name)
        if scheme is None:
            offenders.append(f"required security scheme {name} is missing")
        elif not scheme.get("description"):
            offenders.append(f"security scheme {name} has no environment/condition description")

    referenced_names = {
        requirement_name
        for _path, operations in spec.get("paths", {}).items()
        for operation in operations.values()
        if isinstance(operation, dict)
        for requirement in operation.get("security", [])
        for requirement_name in requirement
    }
    dangling = sorted(referenced_names - set(schemes))
    if dangling:
        offenders.append(f"operations reference undefined security schemes: {dangling}")

    overridden = {
        (method.upper(), path)
        for path, operations in spec.get("paths", {}).items()
        for method, operation in operations.items()
        if method.lower() in {"get", "post", "put", "delete"} and "security" in operation
    }
    if overridden != ENFORCED_MUTATIONS | SLURM_ENFORCED_MUTATIONS:
        missing = sorted((ENFORCED_MUTATIONS | SLURM_ENFORCED_MUTATIONS) - overridden)
        extra = sorted(overridden - (ENFORCED_MUTATIONS | SLURM_ENFORCED_MUTATIONS))
        if missing:
            offenders.append(f"protected operations missing security override: {missing}")
        if extra:
            offenders.append(f"public operations carry a security override: {extra}")

    for method, path in sorted(ENFORCED_MUTATIONS | SLURM_ENFORCED_MUTATIONS):
        operation = spec.get("paths", {}).get(path, {}).get(method.lower())
        if operation is None:
            offenders.append(f"enforced operation {method} {path} is not documented")
            continue
        requirements = operation.get("security")
        expected = (
            SLURM_PROTECTED_OVERRIDE_KEYS
            if (method, path) in SLURM_ENFORCED_MUTATIONS
            else PROTECTED_OVERRIDE_KEYS
        )
        if requirements != expected:
            offenders.append(
                f"{method} {path} security requirements are {requirements!r}, expected {expected!r}"
            )
        and_group = next(
            (requirement for requirement in requirements or [] if "InternalLiveProof" in requirement),
            None,
        )
        if and_group is None or set(and_group) != {"InternalLiveProof", "LiveUserID", "LiveUserRoles"}:
            offenders.append(
                f"{method} {path} live-proof leg is not a single AND group of all three live schemes"
            )

    serialized = json.dumps(spec)
    for secret_token in ("proof-token", "Bearer secret", "dev-test:", "token=secret"):
        if secret_token in serialized:
            offenders.append(f"contract embeds a credential-looking value: {secret_token!r}")
    return offenders


def test_openapi_contract_offenders_clean_on_real_static() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []


def test_openapi_contract_offenders_clean_after_finalizer_wiring() -> None:
    # The real finalizer wiring (the direct patch-openapi-schema pipeline, which
    # is also what the main facade runs) must leave the published contract clean
    # — this is the durable version of the one-off temporary mutation proofs,
    # exercised on a pre-finalized real schema rather than the committed static
    # artifact. FastAPI publishes the complete Slurm scheme and per-operation
    # security list from the route-level HTTPBearer dependency plus
    # openapi_extra in services/slurm_gateway/routes.py, and
    # openapi_patching.patch_openapi_schema preserves that route metadata, so
    # the direct pipeline is the contract owner for the Slurm leg too.
    from apps.api import openapi_patching

    pre_finalized = _pre_finalized_runtime_schema()
    assert _openapi_contract_offenders(pre_finalized)  # pre-finalizer is dirty

    direct = copy.deepcopy(pre_finalized)
    openapi_patching.patch_openapi_schema(direct)
    assert _openapi_contract_offenders(direct) == []


def test_full_api_scheme_set_matches_shared_owner_shape() -> None:
    # The published full-API securitySchemes must equal the shared owner's six
    # definitions exactly (dict equality, including the Slurm bearer shape
    # FastAPI emits from the route-level HTTPBearer dependency).
    schemes = _static_spec()["components"]["securitySchemes"]

    assert schemes == security_scheme_definitions()


def test_direct_patch_scheme_set_matches_shared_owner_shape() -> None:
    from apps.api import openapi_patching

    pre_finalized = _pre_finalized_runtime_schema()
    direct = copy.deepcopy(pre_finalized)
    openapi_patching.patch_openapi_schema(direct)

    assert _openapi_contract_offenders(direct) == []
    assert direct["components"]["securitySchemes"] == security_scheme_definitions()


def test_mutation_undefined_referenced_scheme_reds() -> None:
    # A dangling ref (referenced scheme not defined in securitySchemes) must be
    # reported by the contract oracle (issue #1684 regression).
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    del mutated["components"]["securitySchemes"]["DevRoleHeader"]
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("undefined security schemes" in offender for offender in offenders)


def test_mutation_malformed_security_scheme_reds() -> None:
    # A malformed/unknown scheme (e.g. a scheme we do not publish) must be
    # reported rather than silently accepted.
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    mutated["components"]["securitySchemes"]["DevRoleHeader"] = {"type": "apiKey", "in": "query", "name": "X-User-Role"}
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("description" in offender for offender in offenders)


def test_mutation_reintroduced_nullable_reds() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    mutated["components"]["schemas"]["LayerMetadata"]["properties"]["url_template"] = {
        "type": "string",
        "nullable": True,
    }
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("nullable" in offender for offender in offenders)


def test_mutation_removed_server_reds() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    del mutated["servers"]
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("servers" in offender for offender in offenders)


def test_mutation_wrong_server_url_reds() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    mutated["servers"] = [{"url": "https://example.com", "description": "Remote"}]
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("servers" in offender for offender in offenders)


def test_mutation_removed_root_security_reds() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    del mutated["security"]
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("root security" in offender for offender in offenders)


def test_mutation_false_global_bearer_reds() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    mutated["security"] = [{"DevBearerToken": []}]
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("root security" in offender for offender in offenders)


def test_mutation_removed_protected_override_reds() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    del mutated["paths"]["/api/v1/runs/{run_id}/retry"]["post"]["security"]
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("missing security override" in offender for offender in offenders)


def test_mutation_removed_required_scheme_reds() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    del mutated["components"]["securitySchemes"]["InternalLiveProof"]
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("required security scheme InternalLiveProof is missing" in offender for offender in offenders)


def test_mutation_split_live_and_group_into_or_reds() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    mutated["paths"]["/api/v1/basins"]["post"]["security"] = [
        {"DevRoleHeader": []},
        {"DevBearerToken": []},
        {"InternalLiveProof": []},
        {"LiveUserID": []},
        {"LiveUserRoles": []},
    ]
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("AND group" in offender for offender in offenders)


def test_mutation_removed_live_header_scheme_reds() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    mutated["paths"]["/api/v1/runs/{run_id}/retry"]["post"]["security"] = [
        {"DevRoleHeader": []},
        {"DevBearerToken": []},
        {"InternalLiveProof": [], "LiveUserRoles": []},
    ]
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("AND group" in offender for offender in offenders)


def test_mutation_public_operation_security_override_reds() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    public_path = "/api/v1/pipeline/status"
    assert "security" not in spec["paths"][public_path]["get"]

    mutated = copy.deepcopy(spec)
    mutated["paths"][public_path]["get"]["security"] = [{"DevBearerToken": []}]
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("public operations carry a security override" in offender for offender in offenders)


def test_openapi_contract_offenders_checks_no_secrets() -> None:
    spec = _static_spec()
    assert _openapi_contract_offenders(spec) == []

    mutated = copy.deepcopy(spec)
    mutated["components"]["securitySchemes"]["DevBearerToken"]["description"] += " token=secret"
    assert mutated != spec

    offenders = _openapi_contract_offenders(mutated)
    assert any("credential-looking value" in offender for offender in offenders)


def test_authorized_live_proof_request_reaches_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_live_proof(monkeypatch)
    from tests.test_monitoring_api import GENERIC_RETRY_JOB_TYPE, _create_job, _MockGateway, _store

    with _store() as store:
        _create_job(
            store,
            job_id="job_live_contract",
            run_id="run_live_contract",
            job_type=GENERIC_RETRY_JOB_TYPE,
            stage="forecast",
            status="failed",
        )
        gateway = _MockGateway()
        with _client(store, gateway) as client:
            response = client.post(
                "/api/v1/runs/run_live_contract/retry",
                headers={
                    "X-NHMS-Internal-Live-Proof": "proof-token",
                    "X-Live-User-ID": "live-actor",
                    "X-Live-User-Roles": "operator",
                },
            )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "submitted"
    assert len(gateway.submissions) == 1
    assert gateway.submissions[0].run_id == "run_live_contract"


def test_authorized_live_proof_request_reaches_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_live_proof(monkeypatch)
    from tests.test_monitoring_api import _create_job, _MockGateway, _store

    with _store() as store:
        _create_job(store, job_id="job_live_cancel", run_id="run_live_cancel", status="running")
        gateway = _MockGateway()
        with _client(store, gateway) as client:
            response = client.post(
                "/api/v1/runs/run_live_cancel/cancel",
                headers={
                    "X-NHMS-Internal-Live-Proof": "proof-token",
                    "X-Live-User-ID": "live-actor",
                    "X-Live-User-Roles": "operator",
                },
            )

    assert response.status_code == 200
    assert gateway.cancelled == ["slurm_1"]
    cancelled = response.json()["data"]["cancelled_jobs"]
    assert [job["job_id"] for job in cancelled] == ["job_live_cancel"]


def test_missing_proof_release_blocks_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_live_proof(monkeypatch)
    from tests.test_monitoring_api import GENERIC_RETRY_JOB_TYPE, _create_job, _MockGateway, _store

    with _store() as store:
        _create_job(
            store,
            job_id="job_missing_live_proof",
            run_id="run_missing_live_proof",
            job_type=GENERIC_RETRY_JOB_TYPE,
            stage="forecast",
            status="failed",
        )
        gateway = _MockGateway()
        with _client(store, gateway) as client:
            response = client.post(
                "/api/v1/runs/run_missing_live_proof/retry",
                headers={"X-Live-User-ID": "live-actor", "X-Live-User-Roles": "operator"},
            )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RELEASE_BLOCKED"
    assert response.json()["error"]["details"]["policy_decision"]["execution_mode"] == "release_blocked"
    assert gateway.submissions == []


def test_retry_unauthorized_preserves_403() -> None:
    from tests.test_monitoring_api import _create_job, _store

    with _store() as store:
        _create_job(store, job_id="job_retry_forbidden", run_id="run_retry_forbidden", status="failed")
        with _client(store, allow_dev_role_header=True) as client:
            response = client.post(
                "/api/v1/runs/run_retry_forbidden/retry",
                headers={"X-User-Role": "viewer"},
            )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "RBAC_FORBIDDEN"


def test_retry_anonymous_preserves_401() -> None:
    from tests.test_monitoring_api import _create_job, _store

    with _store() as store:
        _create_job(store, job_id="job_retry_anon", run_id="run_retry_anon", status="failed")
        with _client(store) as client:
            response = client.post("/api/v1/runs/run_retry_anon/retry")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_display_retry_manual_action_409_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.api.main import create_app
    from tests.test_monitoring_api import _dependency_forbidden, _display_env

    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    display_app = create_app(_display_env())
    display_app.dependency_overrides[pipeline_routes.get_pipeline_store] = _dependency_forbidden(
        "pipeline store must not be constructed"
    )
    display_app.dependency_overrides[pipeline_routes.get_retry_service] = _dependency_forbidden(
        "retry service must not be constructed"
    )
    display_app.dependency_overrides[pipeline_routes.get_slurm_gateway] = _dependency_forbidden(
        "slurm gateway must not be constructed"
    )
    with TestClient(display_app) as client:
        response = client.post(
            "/api/v1/runs/run_display_manual/retry",
            headers={"X-User-Role": "operator"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONTROL_PLANE_MANUAL_ACTION_REQUIRED"
