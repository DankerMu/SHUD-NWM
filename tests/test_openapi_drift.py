from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from apps.api import main as api_main
from apps.api import openapi_patching
from apps.api.main import _patch_mvt_tile_openapi, app
from apps.api.routes.hydro_display import get_hydro_display_session
from services.tiles.mvt import MVT_MAX_ZOOM

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
RouteKey = tuple[str, str]

INTERNAL_ROUTE_REASONS: dict[RouteKey, str] = {
    ("GET", "/openapi.json"): "issue-123 framework OpenAPI JSON endpoint, not versioned public API",
    ("GET", "/docs"): "issue-123 framework Swagger UI endpoint, not versioned public API",
    ("GET", "/docs/oauth2-redirect"): "issue-123 framework Swagger OAuth redirect endpoint, not versioned public API",
    ("GET", "/redoc"): "issue-123 framework ReDoc endpoint, not versioned public API",
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
    ("GET", "/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf"): (
        "issue-350 runtime national overview MVT route; public static OpenAPI promotion remains out of scope"
    ),
    ("GET", "/api/v1/state-snapshots"): "issue-123 internal state snapshot surface",
    ("GET", "/api/v1/state-snapshots/{state_id}"): "issue-123 internal state snapshot surface",
    ("POST", "/api/v1/basins"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/basins/{basin_id}/versions"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/river-networks"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/mesh-versions"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/models"): "issue-123 write-side registry API remains internal",
    ("POST", "/api/v1/river-segment-crosswalks"): "issue-123 write-side registry API remains internal",
    ("GET", "/health"): "issue-123 root service health endpoint, not versioned public API",
    ("GET", "/{full_path}"): "issue-123 frontend SPA fallback, not an API contract",
}
INTERNAL_ROUTES = set(INTERNAL_ROUTE_REASONS)


def test_static_openapi_matches_runtime_schema() -> None:
    static_spec = _openapi_spec()
    app.openapi_schema = None

    assert static_spec == app.openapi()


def test_openapi_public_routes_match_fastapi_routes_except_explicit_internal_allowlist() -> None:
    openapi_routes = _openapi_routes()
    fastapi_routes = _fastapi_routes()

    documented_but_missing = openapi_routes - fastapi_routes
    implemented_but_undocumented = fastapi_routes - openapi_routes

    assert not documented_but_missing
    assert not implemented_but_undocumented - INTERNAL_ROUTES


def test_openapi_drift_allowlists_have_issue_scoped_reasons() -> None:
    for route, reason in INTERNAL_ROUTE_REASONS.items():
        assert route[0].lower() in HTTP_METHODS
        assert route[1].startswith("/")
        assert "issue-" in reason
        assert len(reason) >= 40


def test_runtime_mvt_openapi_patch_narrows_only_mvt_routes() -> None:
    schema: dict[str, Any] = {
        "paths": {
            "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf": {
                "get": {
                    "parameters": [
                        {"name": "z", "in": "path", "schema": {"type": "integer"}},
                        {"name": "x", "in": "path", "schema": {"type": "integer"}},
                        {"name": "y", "in": "path", "schema": {"type": "integer"}},
                    ]
                }
            },
        }
    }

    _patch_mvt_tile_openapi(schema)

    z_param = _operation_parameter(
        schema,
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
        "get",
        "path",
        "z",
    )
    assert z_param["schema"]["maximum"] == MVT_MAX_ZOOM


def test_openapi_patch_owner_module_keeps_main_compatibility_facade() -> None:
    from apps.api.main import _patch_pipeline_openapi

    assert api_main._patch_mvt_tile_openapi is openapi_patching._patch_mvt_tile_openapi
    assert _patch_pipeline_openapi is openapi_patching._patch_pipeline_openapi
    assert api_main.custom_openapi() == app.openapi()


def test_openapi_patch_owner_module_preserves_main_monkeypatch_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False
    api = api_main.create_app()

    def fake_pipeline_patch(schema: dict[str, Any]) -> None:
        nonlocal called
        called = True
        schema.setdefault("x-test-openapi-facade", {})["pipeline_patch"] = "called"

    monkeypatch.setattr(api_main, "_patch_pipeline_openapi", fake_pipeline_patch)

    schema = api.openapi()

    assert called
    assert schema["x-test-openapi-facade"]["pipeline_patch"] == "called"


def test_mvt_tile_z_above_documented_max_returns_runtime_validation_error() -> None:
    class FakeDialect:
        name = "sqlite"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

    app.dependency_overrides[get_hydro_display_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/tiles/river-network/basin_v1/15/0/0.pbf")
    finally:
        app.dependency_overrides.pop(get_hydro_display_session, None)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "TILE_XYZ_INVALID"
    assert body["error"]["details"]["max_z"] == MVT_MAX_ZOOM


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/tiles/river-network/basin_v1/0/1/0.pbf",
        "/api/v1/tiles/river-network/basin_v1/0/0/1.pbf",
    ],
)
def test_mvt_low_zoom_tile_xy_matrix_overflow_returns_runtime_validation_error(path: str) -> None:
    class FakeDialect:
        name = "sqlite"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

    app.dependency_overrides[get_hydro_display_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get(path)
    finally:
        app.dependency_overrides.pop(get_hydro_display_session, None)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "TILE_XYZ_INVALID"
    assert body["error"]["details"]["z"] == 0
    assert body["error"]["details"]["max_exclusive"] == 1


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


def _operation_parameter(
    spec: dict[str, Any],
    path: str,
    method: str,
    location: str,
    name: str,
) -> dict[str, Any]:
    for parameter in spec["paths"][path][method]["parameters"]:
        resolved = _resolve_ref(parameter["$ref"], spec) if "$ref" in parameter else parameter
        if resolved["in"] == location and resolved["name"] == name:
            return resolved
    raise AssertionError(f"parameter not found: {method.upper()} {path} {location}.{name}")


def _resolve_ref(ref: str, spec: dict[str, Any]) -> dict[str, Any]:
    node: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def _fastapi_routes() -> set[RouteKey]:
    routes: set[RouteKey] = set()
    for route in app.routes:
        path = getattr(route, "path_format", getattr(route, "path", None))
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method.lower() in HTTP_METHODS:
                routes.add((method.upper(), path))
    return routes
