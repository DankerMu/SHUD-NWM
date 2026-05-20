from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.routes import flood_alerts as flood_alert_routes
from apps.api.routes.flood_alerts import RankingItem
from services.tiles.mvt import MVT_MAX_ZOOM

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
RouteKey = tuple[str, str]

# Issue #123 leaves these documented routes deferred because the fixture
# explicitly excludes implementing future registry read, lineage, layer, and
# legacy tile backing stores. Each entry carries a narrow reason so future drift
# cannot hide behind a broad allowlist.
DEFERRED_ROUTE_REASONS: dict[RouteKey, str] = {
    ("GET", "/api/v1/basins"): "issue-123 future registry read surface; backing read store is out of scope",
    ("GET", "/api/v1/basins/{basin_id}/versions"): (
        "issue-123 future registry read surface; backing read store is out of scope"
    ),
    (
        "GET",
        "/api/v1/models/{model_id}/flood-frequency-curves",
    ): "issue-123 future model frequency metadata read; no promoted backing store yet",
    (
        "GET",
        "/api/v1/basin-versions/{basin_version_id}/river-network-versions",
    ): "issue-123 future registry read surface; backing read store is out of scope",
    ("GET", "/api/v1/met/stations/{station_id}/series"): (
        "issue-123 future station time-series read; data-source migration is out of scope"
    ),
    (
        "GET",
        "/api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png",
    ): "issue-123 future raster tile route; tile publisher promotion is out of scope",
    ("GET", "/api/v1/lineage/river-point"): "issue-123 future lineage route; lineage store promotion is out of scope",
    ("GET", "/api/v1/lineage/forcing-point"): "issue-123 future lineage route; lineage store promotion is out of scope",
    ("GET", "/api/v1/lineage/product/{product_id}"): (
        "issue-123 future lineage route; lineage store promotion is out of scope"
    ),
}
DEFERRED_ROUTES = set(DEFERRED_ROUTE_REASONS)

# Issue #123 keeps these implemented FastAPI routes outside the public OpenAPI
# because they are internal/admin, write-side registry, compatibility, health,
# or SPA fallback surfaces.
INTERNAL_ROUTE_REASONS: dict[RouteKey, str] = {
    ("GET", "/api/v1/slurm/health"): "issue-123 internal Slurm gateway health surface",
    ("POST", "/api/v1/slurm/jobs"): "issue-123 internal Slurm gateway command surface",
    ("GET", "/api/v1/slurm/jobs"): "issue-123 internal Slurm gateway inspection surface",
    ("POST", "/api/v1/slurm/job-arrays"): "issue-123 internal Slurm gateway command surface",
    ("GET", "/api/v1/slurm/jobs/{job_id}"): "issue-123 internal Slurm gateway inspection surface",
    ("DELETE", "/api/v1/slurm/jobs/{job_id}"): "issue-123 internal Slurm gateway command surface",
    ("GET", "/api/v1/slurm/jobs/{job_id}/array-tasks"): "issue-123 internal Slurm gateway inspection surface",
    ("GET", "/api/v1/slurm/jobs/{job_id}/logs"): "issue-123 internal Slurm gateway inspection surface",
    ("POST", "/api/v1/slurm/internal/reset"): (
        "issue-123 test/admin reset surface kept internal to non-production workflows"
    ),
    ("GET", "/api/v1/met/best-available"): (
        "issue-123 internal compatibility route until data-source public contract is promoted"
    ),
    ("GET", "/api/v1/state-snapshots"): "issue-123 internal state snapshot surface",
    ("GET", "/api/v1/state-snapshots/{state_id}"): "issue-123 internal state snapshot surface",
    ("POST", "/api/v1/basins"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/basins/{basin_id}/versions"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/river-networks"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/mesh-versions"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/models"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/river-segment-crosswalks"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/hindcast/submit"): "issue-123 hindcast submission public contract is out of scope",
    ("GET", "/health"): "issue-123 root service health endpoint, not versioned public API",
    ("GET", "/{full_path}"): "issue-123 frontend SPA fallback, not an API contract",
}
INTERNAL_ROUTES = set(INTERNAL_ROUTE_REASONS)


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


def test_openapi_drift_allowlists_have_issue_scoped_reasons() -> None:
    for route, reason in {**DEFERRED_ROUTE_REASONS, **INTERNAL_ROUTE_REASONS}.items():
        assert route[0].lower() in HTTP_METHODS
        assert route[1].startswith("/")
        assert "issue-" in reason
        assert len(reason) >= 40


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


def test_forecast_series_river_network_query_parameter_matches_fastapi_openapi() -> None:
    static_spec = _openapi_spec()
    fastapi_spec: dict[str, Any] = app.openapi()
    path = "/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series"

    static_param = _operation_parameter(static_spec, path, "get", "query", "river_network_version_id")
    fastapi_param = _operation_parameter(fastapi_spec, path, "get", "query", "river_network_version_id")

    for param in (static_param, fastapi_param):
        assert param["required"] is True
        assert param["schema"]["type"] == "string"
        assert param["schema"]["minLength"] == 1


def test_flood_alert_timeline_river_network_query_parameter_matches_fastapi_openapi() -> None:
    static_spec = _openapi_spec()
    fastapi_spec: dict[str, Any] = app.openapi()
    path = "/api/v1/flood-alerts/timeline"

    static_param = _operation_parameter(static_spec, path, "get", "query", "river_network_version_id")
    fastapi_param = _operation_parameter(fastapi_spec, path, "get", "query", "river_network_version_id")

    for param in (static_param, fastapi_param):
        assert param["required"] is True
        assert param["schema"]["type"] == "string"
        assert param["schema"]["minLength"] == 1


def test_flood_alert_ranking_limit_contract_matches_runtime_and_static_openapi() -> None:
    static_spec = _openapi_spec()
    fastapi_spec: dict[str, Any] = app.openapi()
    path = "/api/v1/flood-alerts/ranking"

    static_param = _operation_parameter(static_spec, path, "get", "query", "limit")
    fastapi_param = _operation_parameter(fastapi_spec, path, "get", "query", "limit")

    for param in (static_param, fastapi_param):
        assert param["schema"]["type"] == "integer"
        assert param["schema"]["default"] == 10
        assert param["schema"]["maximum"] == 200
        assert param["schema"]["minimum"] == 1

    static_item = static_spec["components"]["schemas"]["FloodAlertRankingItem"]
    runtime_item_schema = RankingItem.model_json_schema()
    assert "geom_centroid" in static_item["required"]
    assert static_item["properties"]["geom_centroid"] == {
        "type": "object",
        "nullable": True,
        "allOf": [{"$ref": "#/components/schemas/GeoJSONPoint"}],
        "description": "GeoJSON point centroid, or null",
    }
    assert runtime_item_schema["properties"]["geom_centroid"]["anyOf"][0]["$ref"] == "#/$defs/GeoPoint"
    assert runtime_item_schema["properties"]["geom_centroid"]["default"] is None


def test_flood_alert_timeline_max_points_contract_matches_runtime_and_static_openapi() -> None:
    static_spec = _openapi_spec()
    fastapi_spec: dict[str, Any] = app.openapi()
    path = "/api/v1/flood-alerts/timeline"

    static_param = _operation_parameter(static_spec, path, "get", "query", "max_points")
    fastapi_param = _operation_parameter(fastapi_spec, path, "get", "query", "max_points")

    for param in (static_param, fastapi_param):
        assert param["schema"]["type"] == "integer"
        assert param["schema"]["default"] == 168
        assert param["schema"]["maximum"] == 1000
        assert param["schema"]["minimum"] == 1


def test_flood_return_period_feature_properties_document_stable_identity() -> None:
    spec = _openapi_spec()
    schema = spec["components"]["schemas"]["FloodReturnPeriodFeatureProperties"]

    assert {"feature_id", "segment_id", "river_network_version_id"} <= set(schema["required"])
    assert schema["properties"]["feature_id"]["type"] == "string"
    assert "river_network_version_id::segment_id" in schema["properties"]["feature_id"]["description"]
    assert schema["properties"]["segment_id"]["type"] == "string"
    assert schema["properties"]["river_network_version_id"]["type"] == "string"


def test_mvt_tile_z_openapi_maximum_matches_runtime_contract() -> None:
    spec = _openapi_spec()
    mvt_paths = (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
    )

    assert spec["components"]["parameters"]["TileZ"]["schema"]["maximum"] == MVT_MAX_ZOOM
    for path in mvt_paths:
        z_param = _operation_parameter(spec, path, "get", "path", "z")
        assert z_param["schema"]["maximum"] == MVT_MAX_ZOOM


def test_mvt_tile_z_above_documented_max_returns_runtime_validation_error() -> None:
    class FakeDialect:
        name = "sqlite"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/tiles/river-network/basin_v1/15/0/0.pbf")
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "TILE_XYZ_INVALID"
    assert body["error"]["details"]["max_z"] == MVT_MAX_ZOOM


def test_river_segment_collection_413_contract_matches_runtime_and_static_openapi() -> None:
    static_spec = _openapi_spec()
    fastapi_spec: dict[str, Any] = app.openapi()
    paths = (
        "/api/v1/basin-versions/{basin_version_id}/river-segments",
        "/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}",
    )

    for path in paths:
        assert static_spec["paths"][path]["get"]["responses"]["413"]["$ref"] == "#/components/responses/Error"
        assert fastapi_spec["paths"][path]["get"]["responses"]["413"]["description"] == (
            "River segment GeoJSON payload budget exceeded."
        )


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


def _operation_parameter(
    spec: dict[str, Any],
    path: str,
    method: str,
    location: str,
    name: str,
) -> dict[str, Any]:
    parameters = spec["paths"][path][method]["parameters"]
    for param in parameters:
        resolved = _resolve_ref(param["$ref"], spec) if "$ref" in param else param
        if resolved.get("in") == location and resolved.get("name") == name:
            return resolved
    raise AssertionError(f"{method.upper()} {path} missing {location} parameter {name}")


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
