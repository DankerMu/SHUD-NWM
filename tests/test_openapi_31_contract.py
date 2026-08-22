from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from apps.api import openapi_patching
from apps.api.main import _PRE_BODY_PROTECTED_MUTATIONS, app
from apps.api.routes import pipeline as pipeline_routes

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

LIVE_PROOF_AND_GROUP = {"InternalLiveProof": [], "LiveUserID": [], "LiveUserRoles": []}

# Every nullable node the patch functions inject (pre-finalizer). The
# Layer.metadata node is the one type+allOf composition; the rest are ordinary
# scalar/array/object typed nodes. The exact count and the composed-node identity
# are pinned so a regression in either direction reddens.
BASELINE_NULLABLE_COUNT = 109
COMPOSED_NULLABLE_PATH = ("components", "schemas", "Layer", "properties", "metadata")

# The exact pinned openapi-typescript package the generated-type assertions run
# with. Must be resolvable on the hosted runner (targeted Unit Tests job has no
# pnpm install / node_modules), so the seam is `npx --yes <exact-pin>` and never
# a local `node_modules/.bin` binary.
OPENAPI_TYPESCRIPT_PACKAGE = "openapi-typescript@7.13.0"


def _generator_function_source() -> str:
    """The ``_generate_types`` function body only, avoiding self-reference.

    The negative pin below must not trip on its own assertion text, so it reads
    just the function the seam lives in rather than the whole module. The
    newline prefix anchors on the definition line, not on the helper's own
    ``source.index("def _generate_types(")`` text.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    start = source.index("\ndef _generate_types(") + 1
    end = source.index("\ndef _", start + 1)
    return source[start:end]


def test_generator_seam_is_hosted_runner_resolvable_exact_pin() -> None:
    # The targeted Unit Tests job has no pnpm install / node_modules, so the
    # generated-type seam must be `npx --yes <exact-version>` — never a local
    # `node_modules/.bin` binary, and never a bare unversioned `npx
    # openapi-typescript` (which drifts with latest). Both generated-type
    # assertions in this suite and tests/test_api_contract.py share the pin.
    generator_source = _generator_function_source()
    assert "node_modules/.bin/openapi-typescript" not in generator_source
    # The seam references the shared constant (not a hardcoded version literal),
    # and the constant is an exact versioned package spec.
    assert '"npx",\n                "--yes",\n                OPENAPI_TYPESCRIPT_PACKAGE,' in generator_source
    assert OPENAPI_TYPESCRIPT_PACKAGE.startswith("openapi-typescript@")
    assert "." in OPENAPI_TYPESCRIPT_PACKAGE.split("@", 1)[1]


def test_generator_seam_reds_on_bare_latest_or_local_bin() -> None:
    # Constructed source text, tracked file untouched: reverting to a bare
    # `npx openapi-typescript` or a `node_modules/.bin` binary must red the
    # exact-pin contract.
    generator_source = _generator_function_source()
    exact_seam = '"npx",\n                "--yes",\n                OPENAPI_TYPESCRIPT_PACKAGE,'

    bare_latest = generator_source.replace(exact_seam, '"npx",\n                "openapi-typescript",')
    assert bare_latest != generator_source
    assert exact_seam not in bare_latest

    local_bin = generator_source.replace(exact_seam, '"node_modules/.bin/openapi-typescript",')
    assert local_bin != generator_source
    assert "node_modules/.bin/openapi-typescript" in local_bin


def test_openapi_typescript_pin_is_shared_across_both_contract_suites() -> None:
    # The pin is defined in both tests/test_api_contract.py and this suite, and
    # the local static guard only covers this file. This guard imports the other
    # file's constant and asserts both equal the same exact versioned spec, so
    # one file drifting to a bare/latest value reds here.
    from tests.test_api_contract import OPENAPI_TYPESCRIPT_PACKAGE as api_contract_pin

    assert api_contract_pin == "openapi-typescript@7.13.0"
    assert OPENAPI_TYPESCRIPT_PACKAGE == api_contract_pin


def test_openapi_typescript_pin_guard_discriminates_on_mismatch() -> None:
    # Constructed mismatched constant, tracked file untouched: a bare `npx
    # openapi-typescript` or a drifted version in the other suite must not
    # satisfy the shared-pin equality.
    from tests.test_api_contract import OPENAPI_TYPESCRIPT_PACKAGE as api_contract_pin

    assert api_contract_pin != "openapi-typescript"
    assert api_contract_pin != "openapi-typescript@8.0.0"
    assert api_contract_pin == OPENAPI_TYPESCRIPT_PACKAGE


def test_finalizer_replaces_all_nullables_and_keeps_dialect() -> None:
    pre_finalized = _pre_finalized_runtime_schema()
    nullable_paths = _nullable_paths(pre_finalized)
    assert len(nullable_paths) == BASELINE_NULLABLE_COUNT

    composed = _deep_get(pre_finalized, COMPOSED_NULLABLE_PATH)
    assert composed["type"] == "object"
    assert composed["nullable"] is True
    assert composed["allOf"] == [{"$ref": "#/components/schemas/LayerMetadata"}]

    finalized = copy.deepcopy(pre_finalized)
    openapi_patching._finalize_openapi_schema(finalized)

    assert finalized["openapi"] == "3.1.0"
    assert "nullable" not in json.dumps(finalized)
    assert finalized["components"]["schemas"]["Layer"]["properties"]["metadata"] == {
        "anyOf": [
            {
                "type": "object",
                "allOf": [{"$ref": "#/components/schemas/LayerMetadata"}],
            },
            {"type": "null"},
        ]
    }
    assert len(_nullable_paths(finalized)) == 0
    # The 108 ordinary nodes become scalar type unions [T, "null"]; the composed
    # node becomes an anyOf union. FastAPI's own anyOf-null unions stay untouched
    # (they were never nullable-keyword nodes, so they keep their shape).
    assert _scalar_type_union_null_count(finalized) == BASELINE_NULLABLE_COUNT - 1
    assert _anyof_with_null_branch_count(finalized) == 1 + _pre_existing_anyof_null_count(pre_finalized)


def test_finalizer_preserves_ordinary_sibling_keywords() -> None:
    pre_finalized = _pre_finalized_runtime_schema()
    ordinary = [
        path
        for path in _nullable_paths(pre_finalized)
        if path != COMPOSED_NULLABLE_PATH and isinstance(_deep_get(pre_finalized, path)["type"], str)
    ]
    assert len(ordinary) == BASELINE_NULLABLE_COUNT - 1

    finalized = copy.deepcopy(pre_finalized)
    openapi_patching._finalize_openapi_schema(finalized)

    for path in ordinary:
        before = _deep_get(pre_finalized, path)
        after = _deep_get(finalized, path)
        assert before["type"] == after["type"][0]
        assert after["type"][1] == "null"
        siblings = {key: value for key, value in before.items() if key not in ("type", "nullable")}
        for key, value in siblings.items():
            assert after[key] == value, f"{path}: sibling {key} not preserved"


def test_finalizer_is_deterministic_and_idempotent() -> None:
    pre_finalized = _pre_finalized_runtime_schema()
    first = copy.deepcopy(pre_finalized)
    second = copy.deepcopy(pre_finalized)
    openapi_patching._finalize_openapi_schema(first)
    openapi_patching._finalize_openapi_schema(second)
    assert first == second

    rerun = copy.deepcopy(first)
    openapi_patching._finalize_openapi_schema(rerun)
    assert rerun == first


def test_finalizer_leaves_pydantic_native_anyof_null_untouched() -> None:
    # FastAPI/Pydantic's own anyOf-null unions (e.g. the station-series
    # variables query parameter) must not be rewritten by the finalizer.
    pre_finalized = _pre_finalized_runtime_schema()
    variables = pre_finalized["paths"]["/api/v1/met/stations/{station_id}/series"]["get"]["parameters"][5]["schema"]
    assert variables == {
        "oneOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ]
    }

    finalized = copy.deepcopy(pre_finalized)
    openapi_patching._finalize_openapi_schema(finalized)
    assert finalized["paths"]["/api/v1/met/stations/{station_id}/series"]["get"]["parameters"][5]["schema"] == variables


def test_finalizer_rejects_unsupported_nullable_shapes() -> None:
    malformed = {"nullable": True, "enum": ["a"]}
    with pytest.raises(ValueError, match="without a scalar type"):
        openapi_patching._finalize_openapi_schema({"components": {"schemas": {"Broken": malformed}}})


def test_finalizer_recurses_into_nested_nullable_children() -> None:
    # A nullable parent must not leave a nullable child behind: children are
    # normalized before the current node, so every nested nullable is removed.
    pre_finalized = _pre_finalized_runtime_schema()
    assert "nullable" in json.dumps(pre_finalized)

    finalized = copy.deepcopy(pre_finalized)
    openapi_patching._finalize_openapi_schema(finalized)

    assert "nullable" not in json.dumps(finalized)


def test_finalizer_constructed_nested_nullable() -> None:
    nested = {
        "type": "object",
        "nullable": True,
        "properties": {"child": {"type": "string", "nullable": True}},
    }
    normalized = openapi_patching._remove_nullable_keywords(nested)
    assert normalized == {
        "type": ["object", "null"],
        "properties": {"child": {"type": ["string", "null"]}},
    }


def test_finalizer_nullable_false_keeps_non_nullable_domain() -> None:
    # `nullable: false` is a legacy no-op: the keyword is dropped and the value
    # domain stays non-nullable — no type union is introduced.
    normalized = openapi_patching._remove_nullable_keywords({"type": "string", "nullable": False})
    assert normalized == {"type": "string"}


def test_finalizer_existing_nullable_type_union_is_not_nested() -> None:
    # A node already expressed as `type: [T, "null"]` (idempotent re-run or a
    # hand-authored union) keeps its member order and does not gain a second
    # null member or a nested list.
    normalized = openapi_patching._remove_nullable_keywords({"type": ["string", "null"], "nullable": True})
    assert normalized == {"type": ["string", "null"]}


def test_finalizer_type_array_without_null_appends_null_once() -> None:
    # A valid JSON Schema type array without null (e.g. `["string", "number"]`)
    # keeps its member order and gains `"null"` appended exactly once.
    normalized = openapi_patching._remove_nullable_keywords({"type": ["string", "number"], "nullable": True})
    assert normalized == {"type": ["string", "number", "null"]}


def test_finalizer_rejects_empty_or_non_string_type_arrays() -> None:
    with pytest.raises(ValueError, match="non-empty string type array"):
        openapi_patching._remove_nullable_keywords({"type": [], "nullable": True})
    with pytest.raises(ValueError, match="non-empty string type array"):
        openapi_patching._remove_nullable_keywords({"type": ["string", 3], "nullable": True})


def test_finalizer_composed_type_array_stays_non_null_in_composition_branch() -> None:
    # The type+allOf composition branch keeps a valid (non-empty string) type
    # array, with null living only in the outer anyOf branch — putting null
    # inside the composition branch would create an erroneous intersection.
    normalized = openapi_patching._remove_nullable_keywords(
        {"type": ["string", "number"], "nullable": True, "allOf": [{"$ref": "#/components/schemas/X"}]}
    )
    assert normalized == {
        "anyOf": [
            {
                "allOf": [{"$ref": "#/components/schemas/X"}],
                "type": ["string", "number"],
            },
            {"type": "null"},
        ]
    }


def test_finalizer_scalar_null_type_becomes_single_null_union() -> None:
    # A scalar `type: "null"` + nullable must become `type: ["null"]` — exactly
    # one null member, never a duplicated pair.
    normalized = openapi_patching._remove_nullable_keywords({"type": "null", "nullable": True})
    assert normalized == {"type": ["null"]}


def test_finalizer_deduplicates_null_in_existing_type_array() -> None:
    # A type array carrying one or more null members keeps its member order but
    # normalizes to exactly one null (dedupe existing, no duplication).
    single = openapi_patching._remove_nullable_keywords({"type": ["string", "null"], "nullable": True})
    assert single == {"type": ["string", "null"]}

    multiple = openapi_patching._remove_nullable_keywords(
        {"type": ["string", "null", "number", "null"], "nullable": True}
    )
    assert multiple == {"type": ["string", "number", "null"]}


def test_finalizer_composition_branch_strips_null_from_type_array() -> None:
    # A type+allOf composition whose type array contains null must have the null
    # stripped from the composition branch (never an erroneous intersection) and
    # expressed only in the outer anyOf branch.
    normalized = openapi_patching._remove_nullable_keywords(
        {"type": ["object", "null"], "nullable": True, "allOf": [{"$ref": "#/components/schemas/X"}]}
    )
    assert normalized == {
        "anyOf": [
            {
                "allOf": [{"$ref": "#/components/schemas/X"}],
                "type": ["object"],
            },
            {"type": "null"},
        ]
    }


def test_finalizer_composition_branch_requires_non_null_domain() -> None:
    # A type+allOf composition with no non-null type member (`type: ["null"]`),
    # a scalar `type: "null"`, or with no type at all has no composition domain
    # and must raise a stable ValueError, never a KeyError.
    with pytest.raises(ValueError, match="no non-null"):
        openapi_patching._remove_nullable_keywords(
            {"type": ["null"], "nullable": True, "allOf": [{"$ref": "#/components/schemas/X"}]}
        )
    with pytest.raises(ValueError, match="no non-null"):
        openapi_patching._remove_nullable_keywords(
            {"type": "null", "nullable": True, "allOf": [{"$ref": "#/components/schemas/X"}]}
        )
    with pytest.raises(ValueError, match="without a scalar type or non-empty type array"):
        openapi_patching._remove_nullable_keywords(
            {"nullable": True, "allOf": [{"$ref": "#/components/schemas/X"}]}
        )


def test_finalizer_rejects_non_boolean_nullable() -> None:
    with pytest.raises(ValueError, match="non-boolean nullable value"):
        openapi_patching._remove_nullable_keywords({"type": "string", "nullable": "yes"})


def test_security_boundary_overwrites_wrong_pre_existing_values() -> None:
    # The finalizer is the current authority: a pre-existing `servers: []`,
    # a false global bearer root, or a stale same-name scheme must be replaced
    # by the fixture-pinned truth, not silently preserved by setdefault.
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


def test_layer_metadata_composed_node_keeps_generated_null_union() -> None:
    spec = _static_spec()
    metadata = spec["components"]["schemas"]["Layer"]["properties"]["metadata"]
    assert metadata == {
        "anyOf": [
            {
                "type": "object",
                "allOf": [{"$ref": "#/components/schemas/LayerMetadata"}],
            },
            {"type": "null"},
        ]
    }
    assert "nullable" not in json.dumps(metadata)

    generated = _generated_types()
    layer_start = generated.index("Layer:")
    layer_metadata_start = generated.index("LayerMetadata:")
    layer_types = generated[layer_start:layer_metadata_start]
    assert 'metadata?: components["schemas"]["LayerMetadata"] | null;' in layer_types


def test_naive_type_array_and_allof_intersection_mutant_reds() -> None:
    # A naive mutation that keeps `type: [object, null]` next to allOf makes
    # openapi-typescript generate an erroneous intersection
    # `(LayerMetadata | null) & LayerMetadata`; the composition must be expressed
    # as composition-or-null so the generated type stays the union.
    mutated = copy.deepcopy(_static_spec())
    mutated["components"]["schemas"]["Layer"]["properties"]["metadata"] = {
        "type": ["object", "null"],
        "allOf": [{"$ref": "#/components/schemas/LayerMetadata"}],
    }
    generated = _generate_types(yaml.safe_dump(mutated, sort_keys=False, default_flow_style=False))

    layer_start = generated.index("Layer:")
    layer_metadata_start = generated.index("LayerMetadata:")
    layer_types = generated[layer_start:layer_metadata_start]
    # The mutant must generate the intersection, not the pinned union.
    assert (
        'metadata?: (components["schemas"]["LayerMetadata"] | null) & '
        'components["schemas"]["LayerMetadata"];'
    ) in layer_types
    assert 'metadata?: components["schemas"]["LayerMetadata"] | null;' not in layer_types


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


def test_exactly_eleven_protected_operations_override_root_security() -> None:
    spec = _static_spec()
    overridden: set[tuple[str, str]] = set()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method.lower() not in {"get", "post", "put"}:
                continue
            if "security" in operation:
                overridden.add((method.upper(), path))
    assert overridden == {
        ("POST", "/api/v1/basins"),
        ("POST", "/api/v1/basins/{basin_id}/versions"),
        ("POST", "/api/v1/river-networks"),
        ("POST", "/api/v1/mesh-versions"),
        ("POST", "/api/v1/models"),
        ("POST", "/api/v1/models/{model_id}/preflight"),
        ("POST", "/api/v1/models/{model_id}/lifecycle"),
        ("POST", "/api/v1/river-segment-crosswalks"),
        ("PUT", "/api/v1/models/{model_id}/active"),
        ("POST", "/api/v1/runs/{run_id}/retry"),
        ("POST", "/api/v1/runs/{run_id}/cancel"),
    }


def test_live_proof_requirements_form_one_and_group() -> None:
    spec = _static_spec()
    for method, path in sorted(ENFORCED_MUTATIONS):
        operation = spec["paths"][path][method.lower()]
        requirements = operation["security"]
        assert requirements == PROTECTED_OVERRIDE_KEYS, (method, path)
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
        if method.lower() in {"get", "post", "put"} and "security" in operation
    }
    public_operations = [
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method.lower() in {"get", "post", "put"}
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
    # pre-body guard covers the registry mutations, and the model active/
    # lifecycle/preflight plus pipeline retry/cancel are covered by the
    # require_action dependencies. This binding reddens if a route is added or
    # removed from enforcement without updating the OpenAPI override set.
    runtime_paths = _runtime_operation_paths()
    for method, path in ENFORCED_MUTATIONS:
        assert (method, path) in runtime_paths, (method, path)
    documented = {
        (method.upper(), path)
        for path, operations in _static_spec()["paths"].items()
        for method, operation in operations.items()
        if method.lower() in {"get", "post", "put"} and "security" in operation
    }
    assert documented == ENFORCED_MUTATIONS


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
    expected_scheme_names = {"DevRoleHeader", "DevBearerToken", "InternalLiveProof", "LiveUserID", "LiveUserRoles"}
    if set(schemes) != expected_scheme_names:
        offenders.append(f"securitySchemes set is {sorted(schemes)}, expected {sorted(expected_scheme_names)}")
    for name in ("DevRoleHeader", "DevBearerToken", "InternalLiveProof", "LiveUserID", "LiveUserRoles"):
        scheme = schemes.get(name)
        if scheme is None:
            offenders.append(f"required security scheme {name} is missing")
        elif not scheme.get("description"):
            offenders.append(f"security scheme {name} has no environment/condition description")

    overridden = {
        (method.upper(), path)
        for path, operations in spec.get("paths", {}).items()
        for method, operation in operations.items()
        if method.lower() in {"get", "post", "put"} and "security" in operation
    }
    if overridden != ENFORCED_MUTATIONS:
        missing = sorted(ENFORCED_MUTATIONS - overridden)
        extra = sorted(overridden - ENFORCED_MUTATIONS)
        if missing:
            offenders.append(f"protected operations missing security override: {missing}")
        if extra:
            offenders.append(f"public operations carry a security override: {extra}")

    for method, path in sorted(ENFORCED_MUTATIONS):
        operation = spec.get("paths", {}).get(path, {}).get(method.lower())
        if operation is None:
            offenders.append(f"enforced operation {method} {path} is not documented")
            continue
        requirements = operation.get("security")
        if requirements != PROTECTED_OVERRIDE_KEYS:
            offenders.append(
                f"{method} {path} security requirements are {requirements!r}, "
                f"expected {PROTECTED_OVERRIDE_KEYS!r}"
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
    # The real finalizer wiring (direct pipeline and the main facade) must leave
    # the published contract clean — this is the durable version of the
    # one-off temporary mutation proofs, exercised on a pre-finalized real
    # schema rather than the committed static artifact.
    from apps.api import openapi_patching

    pre_finalized = _pre_finalized_runtime_schema()
    assert _openapi_contract_offenders(pre_finalized)  # pre-finalizer is dirty

    direct = copy.deepcopy(pre_finalized)
    openapi_patching.patch_openapi_schema(direct)
    assert _openapi_contract_offenders(direct) == []

    facade = copy.deepcopy(pre_finalized)
    openapi_patching._finalize_openapi_schema(facade)
    assert _openapi_contract_offenders(facade) == []


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


def _pre_finalized_runtime_schema() -> dict[str, Any]:
    """Build the schema from the real patch functions, stopping before the finalizer."""
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    for patch in (
        openapi_patching._patch_mvt_tile_openapi,
        openapi_patching._patch_station_series_openapi,
        openapi_patching._patch_qhh_latest_product_openapi,
        openapi_patching._patch_met_stations_list_openapi,
        openapi_patching._patch_layer_metadata_openapi,
        openapi_patching._patch_pipeline_openapi,
        openapi_patching._patch_runtime_openapi,
    ):
        patch(schema)
    return schema


def _nullable_paths(node: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if isinstance(node, dict):
        found: list[tuple[str, ...]] = []
        if "nullable" in node:
            found.append(path)
        for key, value in node.items():
            found.extend(_nullable_paths(value, (*path, key)))
        return found
    if isinstance(node, list):
        found = []
        for index, item in enumerate(node):
            found.extend(_nullable_paths(item, (*path, str(index))))
        return found
    return []


def _scalar_type_union_null_count(node: Any) -> int:
    """Count nodes whose ``type`` is a scalar-type union containing ``"null"``."""
    if isinstance(node, dict):
        count = 0
        if isinstance(node.get("type"), list) and "null" in node["type"]:
            count += 1
        for value in node.values():
            count += _scalar_type_union_null_count(value)
        return count
    if isinstance(node, list):
        return sum(_scalar_type_union_null_count(item) for item in node)
    return 0


def _anyof_with_null_branch_count(node: Any) -> int:
    if isinstance(node, dict):
        count = 0
        if "anyOf" in node and any(branch == {"type": "null"} for branch in node["anyOf"]):
            count += 1
        for value in node.values():
            count += _anyof_with_null_branch_count(value)
        return count
    if isinstance(node, list):
        return sum(_anyof_with_null_branch_count(item) for item in node)
    return 0


def _pre_existing_anyof_null_count(node: Any) -> int:
    """anyOf-null unions that exist before the finalizer (FastAPI/Pydantic-native).

    These are the nodes the finalizer must leave untouched; the composed-node
    count is the pre-existing count plus one for Layer.metadata.
    """
    return _anyof_with_null_branch_count(node)


def _deep_get(node: Any, path: tuple[str, ...]) -> Any:
    for part in path:
        if isinstance(node, list):
            node = node[int(part)]
        else:
            node = node[part]
    return node


def _static_spec() -> dict[str, Any]:
    spec_path = _repo_root() / "openapi" / "nhms.v1.yaml"
    return yaml.safe_load(spec_path.read_text(encoding="utf-8"))


def _repo_root() -> Any:
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def _generated_types() -> str:
    return (_repo_root() / "apps" / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


def _generate_types(spec_text: str) -> str:
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as handle:
        handle.write(spec_text)
        spec_path = handle.name
    output_path = tempfile.mktemp(suffix=".ts")
    try:
        subprocess.run(
            [
                "npx",
                "--yes",
                OPENAPI_TYPESCRIPT_PACKAGE,
                spec_path,
                "--output",
                output_path,
            ],
            cwd=_repo_root() / "apps" / "frontend",
            check=True,
            capture_output=True,
            text=True,
        )
        return open(output_path, encoding="utf-8").read()
    finally:
        import os

        os.unlink(spec_path)
        os.unlink(output_path)


def _runtime_operation_paths() -> set[tuple[str, str]]:
    app.openapi_schema = None
    schema = app.openapi()
    return {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
        if method.lower() in {"get", "post", "put"}
    }


def _enable_live_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_BACKEND", "oidc")
    monkeypatch.setenv("NHMS_TRUSTED_LIVE_PROOF_MODE", "test_internal")
    monkeypatch.setenv("NHMS_INTERNAL_LIVE_PROOF_TOKEN", "proof-token")


class _client:
    def __init__(
        self,
        store: Any,
        gateway: Any | None = None,
        *,
        allow_dev_role_header: bool = False,
    ) -> None:
        from tests.test_monitoring_api import _MockGateway

        self.store = store
        self.gateway = gateway or _MockGateway()
        self.allow_dev_role_header = allow_dev_role_header
        self.client: TestClient | None = None

    def __enter__(self) -> TestClient:
        import os

        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: self.store
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: self.gateway
        if self.allow_dev_role_header:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
        self.client = TestClient(app)
        return self.client

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        import os

        app.dependency_overrides.pop(pipeline_routes.get_pipeline_store, None)
        app.dependency_overrides.pop(pipeline_routes.get_slurm_gateway, None)
        if self.allow_dev_role_header:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        if self.client is not None:
            self.client.close()
