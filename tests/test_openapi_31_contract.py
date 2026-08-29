from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from apps.api import openapi_patching
from apps.api.main import app
from apps.api.routes import pipeline as pipeline_routes

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
        if method.lower() in {"get", "post", "put", "delete"}
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
