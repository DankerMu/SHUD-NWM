from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from apps.api.main import app

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
RouteKey = tuple[str, str]

# Documented in the public OpenAPI contract, but deferred until the backing
# registry, lineage, layer, and tile implementations are promoted to the API.
DEFERRED_ROUTES: set[RouteKey] = {
    ("GET", "/api/v1/basins"),
    ("GET", "/api/v1/basins/{basin_id}/versions"),
    ("GET", "/api/v1/models/{model_id}"),
    ("GET", "/api/v1/models/{model_id}/flood-frequency-curves"),
    ("GET", "/api/v1/basin-versions/{basin_version_id}/river-network-versions"),
    ("GET", "/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}"),
    ("GET", "/api/v1/met/stations/{station_id}/series"),
    ("GET", "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf"),
    ("GET", "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf"),
    ("GET", "/api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png"),
    ("GET", "/api/v1/lineage/river-point"),
    ("GET", "/api/v1/lineage/forcing-point"),
    ("GET", "/api/v1/lineage/product/{product_id}"),
    ("GET", "/api/v1/layers"),
    ("GET", "/api/v1/layers/{layer_id}/valid-times"),
}

# Implemented by FastAPI, but intentionally excluded from the public OpenAPI
# contract because they are internal/admin surfaces, write-side registry APIs,
# compatibility shims, root health checks, or frontend SPA fallback routes.
INTERNAL_ROUTES: set[RouteKey] = {
    ("GET", "/api/v1/slurm/health"),
    ("POST", "/api/v1/slurm/jobs"),
    ("GET", "/api/v1/slurm/jobs"),
    ("POST", "/api/v1/slurm/job-arrays"),
    ("GET", "/api/v1/slurm/jobs/{job_id}"),
    ("DELETE", "/api/v1/slurm/jobs/{job_id}"),
    ("GET", "/api/v1/slurm/jobs/{job_id}/array-tasks"),
    ("GET", "/api/v1/slurm/jobs/{job_id}/logs"),
    ("POST", "/api/v1/slurm/internal/reset"),
    ("GET", "/api/v1/met/best-available"),
    ("GET", "/api/v1/state-snapshots"),
    ("GET", "/api/v1/state-snapshots/{state_id}"),
    ("POST", "/api/v1/basins"),
    ("POST", "/api/v1/basins/{basin_id}/versions"),
    ("POST", "/api/v1/river-networks"),
    ("POST", "/api/v1/mesh-versions"),
    ("POST", "/api/v1/models"),
    ("POST", "/api/v1/river-segment-crosswalks"),
    ("POST", "/api/v1/hindcast/submit"),
    ("GET", "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf"),
    ("GET", "/health"),
    ("GET", "/{full_path}"),
}


def test_openapi_public_routes_match_fastapi_routes_except_explicit_allowlists() -> None:
    openapi_routes = _openapi_routes()
    fastapi_routes = _fastapi_routes()

    documented_but_missing = openapi_routes - fastapi_routes
    implemented_but_undocumented = fastapi_routes - openapi_routes

    assert not documented_but_missing - DEFERRED_ROUTES
    assert not DEFERRED_ROUTES - documented_but_missing
    assert not implemented_but_undocumented - INTERNAL_ROUTES
    assert not INTERNAL_ROUTES - implemented_but_undocumented
    assert DEFERRED_ROUTES.isdisjoint(INTERNAL_ROUTES)


def test_openapi_success_envelope_does_not_constrain_data_shape() -> None:
    spec = _openapi_spec()
    envelope = spec["components"]["schemas"]["SuccessEnvelope"]

    assert envelope["required"] == ["request_id", "status"]
    assert "data" not in envelope["properties"]


def test_openapi_issue_time_documents_latest_and_iso_datetime() -> None:
    spec = _openapi_spec()
    issue_time = spec["components"]["parameters"]["IssueTime"]

    assert "latest" in issue_time["description"]
    assert {"type": "string", "enum": ["latest"]} in issue_time["schema"]["oneOf"]
    assert {"type": "string", "format": "date-time"} in issue_time["schema"]["oneOf"]


def test_openapi_success_envelope_accepts_array_data_composition() -> None:
    spec = _openapi_spec()
    schema = spec["paths"]["/api/v1/basins"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    resolved = _resolve_all_of(schema, spec)

    assert resolved["properties"]["data"]["type"] == "array"
    assert _matches_schema(
        {
            "request_id": "req_test",
            "status": "ok",
            "data": [],
        },
        resolved,
    )


def _openapi_routes() -> set[RouteKey]:
    spec = _openapi_spec()
    routes: set[RouteKey] = set()
    for path, operations in spec["paths"].items():
        for method in operations:
            if method.lower() in HTTP_METHODS:
                routes.add((method.upper(), path))
    return routes


def _openapi_spec() -> dict[str, Any]:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    return yaml.safe_load(spec_path.read_text(encoding="utf-8"))


def _resolve_all_of(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = {"properties": {}, "required": []}
    for subschema in schema.get("allOf", []):
        if "$ref" in subschema:
            subschema = _resolve_ref(subschema["$ref"], spec)
        resolved["properties"].update(subschema.get("properties", {}))
        resolved["required"].extend(item for item in subschema.get("required", []) if item not in resolved["required"])
    return resolved


def _resolve_ref(ref: str, spec: dict[str, Any]) -> dict[str, Any]:
    node: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def _matches_schema(value: dict[str, Any], schema: dict[str, Any]) -> bool:
    for field in schema.get("required", []):
        if field not in value:
            return False
    for field, field_schema in schema.get("properties", {}).items():
        if field not in value:
            continue
        expected_type = field_schema.get("type")
        if expected_type == "array" and not isinstance(value[field], list):
            return False
        if expected_type == "object" and not isinstance(value[field], dict):
            return False
        if expected_type == "string" and not isinstance(value[field], str):
            return False
    return True


def _fastapi_routes() -> set[RouteKey]:
    schema: dict[str, Any] = app.openapi()
    routes: set[RouteKey] = set()
    for path, operations in schema["paths"].items():
        for method in operations:
            if method.lower() in HTTP_METHODS:
                routes.add((method.upper(), path))
    return routes
