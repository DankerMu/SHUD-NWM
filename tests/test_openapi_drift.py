from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from apps.api.main import _patch_mvt_tile_openapi, app
from apps.api.routes import flood_alerts as flood_alert_routes
from apps.api.routes.flood_alerts import RankingItem
from services.tiles.mvt import (
    DEFAULT_FLOOD_RETURN_PERIOD_DURATION,
    MVT_MAX_TILE_COORDINATE,
    MVT_MAX_ZOOM,
    SUPPORTED_FLOOD_RETURN_PERIOD_DURATIONS,
    SUPPORTED_HYDRO_MVT_VARIABLES,
)

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
RouteKey = tuple[str, str]

# Issue #123 leaves these documented routes deferred because the fixture
# explicitly excludes implementing future registry read, lineage, layer, and
# legacy tile backing stores. Each entry carries a narrow reason so future drift
# cannot hide behind a broad allowlist.
DEFERRED_ROUTE_REASONS: dict[RouteKey, str] = {
    (
        "GET",
        "/api/v1/models/{model_id}/flood-frequency-curves",
    ): "issue-123 future model frequency metadata read; no promoted backing store yet",
    (
        "GET",
        "/api/v1/basin-versions/{basin_version_id}/river-network-versions",
    ): "issue-123 future registry read surface; backing read store is out of scope",
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


def test_station_series_runtime_openapi_matches_static_parameters_and_schema() -> None:
    static_spec = _openapi_spec()
    app.openapi_schema = None
    fastapi_spec: dict[str, Any] = app.openapi()
    path = "/api/v1/met/stations/{station_id}/series"
    static_operation = static_spec["paths"][path]["get"]
    runtime_operation = fastapi_spec["paths"][path]["get"]

    expected_params = {
        "station_id",
        "forcing_version_id",
        "model_id",
        "source_id",
        "cycle_time",
        "variables",
        "from",
        "to",
        "limit",
    }
    static_parameters = _operation_parameters_by_name(static_operation, static_spec)
    runtime_parameters = _operation_parameters_by_name(runtime_operation, fastapi_spec)
    assert set(static_parameters) == expected_params
    assert set(runtime_parameters) == expected_params
    assert runtime_parameters == static_parameters

    static_response = static_operation["responses"]["200"]["content"]["application/json"]["schema"]
    runtime_response = runtime_operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert runtime_response == static_response
    assert runtime_operation["responses"]["4XX"] == static_operation["responses"]["4XX"]
    assert runtime_operation["responses"]["5XX"] == static_operation["responses"]["5XX"]
    assert static_response["allOf"][1]["properties"]["data"]["$ref"] == "#/components/schemas/StationSeriesResponse"

    assert static_parameters["variables"]["schema"] == {
        "oneOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ]
    }
    assert static_parameters["limit"]["schema"] == {"type": "integer", "minimum": 1, "maximum": 10000}
    assert static_parameters["cycle_time"]["schema"] == {"type": "string", "format": "date-time"}

    for schema_name in (
        "SuccessEnvelope",
        "ErrorResponse",
        "ValidationErrorDetail",
        "StationSeriesPoint",
        "StationSeriesStation",
        "StationSeriesMetadata",
        "StationSeries",
        "StationSeriesResponse",
    ):
        assert fastapi_spec["components"]["schemas"][schema_name] == static_spec["components"]["schemas"][schema_name]
    assert fastapi_spec["components"]["responses"]["Error"] == static_spec["components"]["responses"]["Error"]


def test_qhh_latest_product_runtime_openapi_matches_static_parameters_and_schema() -> None:
    static_spec = _openapi_spec()
    app.openapi_schema = None
    fastapi_spec: dict[str, Any] = app.openapi()
    path = "/api/v1/mvp/qhh/latest-product"
    static_operation = static_spec["paths"][path]["get"]
    runtime_operation = fastapi_spec["paths"][path]["get"]

    static_parameters = _operation_parameters_by_name(static_operation, static_spec)
    runtime_parameters = _operation_parameters_by_name(runtime_operation, fastapi_spec)
    assert set(static_parameters) == {"source"}
    assert runtime_parameters == static_parameters

    static_response = static_operation["responses"]["200"]["content"]["application/json"]["schema"]
    runtime_response = runtime_operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert runtime_response == static_response
    assert runtime_operation["responses"]["4XX"] == static_operation["responses"]["4XX"]
    assert runtime_operation["responses"]["5XX"] == static_operation["responses"]["5XX"]
    assert static_response["allOf"][1]["properties"]["data"]["$ref"] == "#/components/schemas/QhhLatestProduct"
    assert static_parameters["source"]["schema"] == {"type": "string", "enum": ["GFS", "IFS"]}

    for schema_name in (
        "QhhLatestUnavailableReason",
        "QhhLatestQualityNote",
        "QhhLatestStationVariableCoverage",
        "QhhLatestQueryIndex",
        "QhhLatestAvailability",
        "QhhLatestQuality",
        "QhhLatestProduct",
    ):
        assert fastapi_spec["components"]["schemas"][schema_name] == static_spec["components"]["schemas"][schema_name]


@pytest.mark.parametrize(
    ("path", "method", "data_schema"),
    [
        ("/api/v1/pipeline/status", "get", {"$ref": "#/components/schemas/PipelineStatus"}),
        (
            "/api/v1/pipeline/stages",
            "get",
            {"type": "array", "items": {"$ref": "#/components/schemas/PipelineStage"}},
        ),
        ("/api/v1/jobs", "get", {"$ref": "#/components/schemas/PipelineJobPage"}),
        ("/api/v1/jobs/{job_id}/logs", "get", {"$ref": "#/components/schemas/JobLogs"}),
        ("/api/v1/runs/{run_id}/retry", "post", {"$ref": "#/components/schemas/RetryRunResult"}),
    ],
)
def test_ops_runtime_openapi_matches_static_success_schema(
    path: str,
    method: str,
    data_schema: dict[str, Any],
) -> None:
    static_spec = _openapi_spec()
    app.openapi_schema = None
    runtime_spec: dict[str, Any] = app.openapi()
    static_operation = static_spec["paths"][path][method]
    runtime_operation = runtime_spec["paths"][path][method]

    assert runtime_operation["operationId"] == static_operation["operationId"]
    assert _operation_parameters_by_name(runtime_operation, runtime_spec) == _operation_parameters_by_name(
        static_operation,
        static_spec,
    )
    static_response = static_operation["responses"]["200"]["content"]["application/json"]["schema"]
    runtime_response = runtime_operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert runtime_response == static_response
    assert static_response["allOf"][0]["$ref"] == "#/components/schemas/SuccessEnvelope"
    assert static_response["allOf"][1]["properties"]["data"] == data_schema
    assert runtime_operation["responses"]["4XX"] == static_operation["responses"]["4XX"]
    assert runtime_operation["responses"]["5XX"] == static_operation["responses"]["5XX"]

    for schema_name in (
        "JobStatusCounts",
        "PipelineStatus",
        "BasinProgress",
        "BasinResult",
        "PipelineStage",
        "PipelineJob",
        "PipelineJobPage",
        "JobLogs",
        "RetryRunResult",
    ):
        assert runtime_spec["components"]["schemas"][schema_name] == static_spec["components"]["schemas"][schema_name]


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


def test_flood_return_period_collection_product_quality_matches_runtime_and_static_openapi() -> None:
    static_spec = _openapi_spec()
    fastapi_spec: dict[str, Any] = app.openapi()

    static_schema = static_spec["components"]["schemas"]["FloodReturnPeriodFeatureCollection"]
    runtime_response = fastapi_spec["paths"]["/api/v1/tiles/flood-return-period"]["get"]["responses"]["200"]
    runtime_schema_ref = runtime_response["content"]["application/json"]["schema"]["$ref"]
    runtime_schema = _resolve_ref(runtime_schema_ref, fastapi_spec)

    assert static_schema["properties"]["product_quality"] == {
        "type": "object",
        "additionalProperties": True,
        "nullable": True,
        "description": "Flood return-period readiness evidence for the selected run.",
    }
    runtime_product_quality = runtime_schema["properties"]["product_quality"]
    assert {"additionalProperties": True, "type": "object"} in runtime_product_quality["anyOf"]
    assert {"type": "null"} in runtime_product_quality["anyOf"]


def test_mvt_tile_z_openapi_maximum_matches_runtime_contract() -> None:
    static_spec = _openapi_spec()
    fastapi_spec: dict[str, Any] = app.openapi()
    mvt_paths = (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
    )

    assert static_spec["components"]["parameters"]["MvtTileZ"]["schema"]["maximum"] == MVT_MAX_ZOOM
    assert static_spec["components"]["parameters"]["TileZ"]["schema"]["maximum"] == 24
    for spec in (static_spec, fastapi_spec):
        for path in mvt_paths:
            z_param = _operation_parameter(spec, path, "get", "path", "z")
            assert z_param["schema"]["maximum"] == MVT_MAX_ZOOM

    met_z_param = _operation_parameter(
        static_spec,
        "/api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png",
        "get",
        "path",
        "z",
    )
    assert met_z_param["schema"]["maximum"] == 24


def test_mvt_tile_xy_openapi_bounds_match_runtime_contract() -> None:
    static_spec = _openapi_spec()
    fastapi_spec: dict[str, Any] = app.openapi()
    mvt_paths = (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
    )
    expected_descriptions = {
        "x": f"Web Mercator XYZ tile column. Global schema bounds are 0..{MVT_MAX_TILE_COORDINATE} "
        f"for max zoom {MVT_MAX_ZOOM}; each request also enforces 0 <= x < 2^z.",
        "y": f"Web Mercator XYZ tile row. Global schema bounds are 0..{MVT_MAX_TILE_COORDINATE} "
        f"for max zoom {MVT_MAX_ZOOM}; each request also enforces 0 <= y < 2^z.",
    }

    for param_name in ("MvtTileX", "MvtTileY"):
        schema = static_spec["components"]["parameters"][param_name]["schema"]
        assert schema["minimum"] == 0
        assert schema["maximum"] == MVT_MAX_TILE_COORDINATE

    for spec in (static_spec, fastapi_spec):
        for path in mvt_paths:
            for name, description in expected_descriptions.items():
                param = _operation_parameter(spec, path, "get", "path", name)
                assert param["description"] == description
                assert param["schema"]["minimum"] == 0
                assert param["schema"]["maximum"] == MVT_MAX_TILE_COORDINATE


def test_met_png_tile_openapi_keeps_legacy_bounds_not_mvt_limits() -> None:
    static_spec = _openapi_spec()
    path = "/api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png"

    z_param = _operation_parameter(static_spec, path, "get", "path", "z")
    x_param = _operation_parameter(static_spec, path, "get", "path", "x")
    y_param = _operation_parameter(static_spec, path, "get", "path", "y")

    assert z_param["schema"]["maximum"] == 24
    for param in (x_param, y_param):
        assert param["schema"]["minimum"] == 0
        assert param["schema"]["maximum"] == 16777215
        assert "max zoom 14" not in param.get("description", "")


def test_runtime_mvt_openapi_patch_does_not_narrow_met_png_route() -> None:
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
            "/api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png": {
                "get": {
                    "parameters": [
                        {"name": "z", "in": "path", "schema": {"type": "integer", "maximum": 24}},
                        {"name": "x", "in": "path", "schema": {"type": "integer", "maximum": 16777215}},
                        {"name": "y", "in": "path", "schema": {"type": "integer", "maximum": 16777215}},
                    ]
                }
            },
        }
    }

    _patch_mvt_tile_openapi(schema)

    mvt_z = _operation_parameter(
        schema, "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf", "get", "path", "z"
    )
    met_z = _operation_parameter(
        schema, "/api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png", "get", "path", "z"
    )
    met_x = _operation_parameter(
        schema, "/api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png", "get", "path", "x"
    )
    met_y = _operation_parameter(
        schema, "/api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png", "get", "path", "y"
    )

    assert mvt_z["schema"]["maximum"] == MVT_MAX_ZOOM
    assert met_z["schema"]["maximum"] == 24
    assert met_x["schema"]["maximum"] == 16777215
    assert met_y["schema"]["maximum"] == 16777215


def test_mvt_pbf_response_contract_matches_runtime_and_static_openapi() -> None:
    static_spec = _openapi_spec()
    fastapi_spec: dict[str, Any] = app.openapi()
    mvt_paths = (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
    )
    expected_headers = {
        "Cache-Control",
        "ETag",
        "X-Tile-Layer-ID",
        "X-Tile-Checksum",
        "X-Tile-Cache",
        "X-Tile-Cache-Key",
        "X-MVT-Schema-Version",
    }

    for path in mvt_paths:
        static_200 = static_spec["paths"][path]["get"]["responses"]["200"]
        runtime_200 = fastapi_spec["paths"][path]["get"]["responses"]["200"]
        assert set(static_200["content"]) == {"application/x-protobuf"}
        assert set(runtime_200["content"]) == {"application/x-protobuf"}
        assert static_200["content"]["application/x-protobuf"]["schema"] == {
            "type": "string",
            "format": "binary",
        }
        assert runtime_200["content"]["application/x-protobuf"]["schema"] == {
            "type": "string",
            "format": "binary",
        }
        assert expected_headers.issubset(static_200["headers"])
        assert expected_headers.issubset(runtime_200["headers"])
        for header in expected_headers:
            assert runtime_200["headers"][header]["schema"] == static_200["headers"][header]["schema"]


def test_mvt_pbf_error_responses_match_runtime_and_static_openapi() -> None:
    static_spec = _openapi_spec()
    app.openapi_schema = None
    runtime_spec: dict[str, Any] = app.openapi()
    mvt_paths = (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
    )
    expected_response_keys = {"200", "4XX", "424", "5XX"}

    assert (
        static_spec["components"]["responses"]["MvtLivePostgisUnavailable"]
        == runtime_spec["components"]["responses"]["MvtLivePostgisUnavailable"]
    )
    documented_424_schema = static_spec["components"]["responses"]["MvtLivePostgisUnavailable"]["content"][
        "application/json"
    ]["schema"]
    assert documented_424_schema["properties"]["error"]["properties"]["code"]["enum"] == [
        "MVT_LIVE_POSTGIS_UNAVAILABLE"
    ]

    for path in mvt_paths:
        static_responses = static_spec["paths"][path]["get"]["responses"]
        runtime_responses = runtime_spec["paths"][path]["get"]["responses"]
        assert set(static_responses) == expected_response_keys
        assert set(runtime_responses) == expected_response_keys
        for key in ("4XX", "424", "5XX"):
            assert runtime_responses[key] == static_responses[key]


def test_layer_valid_times_openapi_documents_bounded_envelope() -> None:
    spec = _openapi_spec()
    response_schema = spec["paths"]["/api/v1/layers/{layer_id}/valid-times"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    data_schema = response_schema["allOf"][1]["properties"]["data"]
    envelope_schema = spec["components"]["schemas"]["LayerValidTimes"]
    metadata_schema = spec["components"]["schemas"]["LayerMetadata"]
    run_id_param = _operation_parameter(spec, "/api/v1/layers/{layer_id}/valid-times", "get", "query", "run_id")

    assert data_schema["$ref"] == "#/components/schemas/LayerValidTimes"
    assert run_id_param["required"] is False
    assert envelope_schema["required"] == ["valid_times", "items", "limit", "observed_count", "truncated"]
    assert envelope_schema["properties"]["valid_times"]["type"] == "array"
    assert envelope_schema["properties"]["items"]["type"] == "array"
    assert envelope_schema["properties"]["limit"]["type"] == "integer"
    assert envelope_schema["properties"]["observed_count"]["type"] == "integer"
    assert envelope_schema["properties"]["truncated"]["type"] == "boolean"
    assert metadata_schema["properties"]["valid_time_limit"]["type"] == "integer"
    assert metadata_schema["properties"]["valid_time_observed_count"]["type"] == "integer"
    assert metadata_schema["properties"]["valid_times_truncated"]["type"] == "boolean"
    assert "basin_version_id" in metadata_schema["properties"]["source_refs"]["description"]
    assert "river_network_version_id" in metadata_schema["properties"]["source_refs"]["description"]


@pytest.mark.parametrize(
    ("path", "data_schema"),
    [
        ("/api/v1/layers", {"type": "array", "items": {"$ref": "#/components/schemas/Layer"}}),
        ("/api/v1/layers/{layer_id}/valid-times", {"$ref": "#/components/schemas/LayerValidTimes"}),
    ],
)
def test_layer_metadata_runtime_openapi_matches_static_success_schema(path: str, data_schema: dict[str, Any]) -> None:
    static_spec = _openapi_spec()
    app.openapi_schema = None
    runtime_spec: dict[str, Any] = app.openapi()
    static_schema = static_spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    runtime_schema = runtime_spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]

    assert runtime_schema == static_schema
    assert runtime_schema["allOf"][0]["$ref"] == "#/components/schemas/SuccessEnvelope"
    assert runtime_schema["allOf"][1]["properties"]["data"] == data_schema


def test_layer_catalog_runtime_openapi_documents_run_scoped_metadata_parameter() -> None:
    static_spec = _openapi_spec()
    app.openapi_schema = None
    runtime_spec: dict[str, Any] = app.openapi()

    for spec in (static_spec, runtime_spec):
        run_id_param = _operation_parameter(spec, "/api/v1/layers", "get", "query", "run_id")
        assert run_id_param["required"] is False
        assert "scope layer metadata" in run_id_param["description"]


def test_mvt_route_variant_openapi_enums_match_runtime_contract() -> None:
    spec = _openapi_spec()
    hydro_variable = spec["components"]["parameters"]["HydroMvtVariable"]["schema"]["enum"]
    hydro_path_variable = _operation_parameter(
        spec,
        "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
        "get",
        "path",
        "variable",
    )["schema"]["enum"]
    met_path_variable = _operation_parameter(
        spec,
        "/api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png",
        "get",
        "path",
        "variable",
    )["schema"]
    flood_map_duration = _operation_parameter(spec, "/api/v1/tiles/flood-return-period", "get", "query", "duration")[
        "schema"
    ]["enum"]
    flood_mvt_duration = _operation_parameter(
        spec,
        "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
        "get",
        "path",
        "duration",
    )["schema"]["enum"]
    valid_times_duration = _operation_parameter(
        spec, "/api/v1/layers/{layer_id}/valid-times", "get", "query", "duration"
    )["schema"]

    assert hydro_variable == list(SUPPORTED_HYDRO_MVT_VARIABLES)
    assert hydro_path_variable == hydro_variable
    assert met_path_variable["type"] == "string"
    assert "enum" not in met_path_variable
    assert flood_map_duration == list(SUPPORTED_FLOOD_RETURN_PERIOD_DURATIONS)
    assert flood_mvt_duration == flood_map_duration
    assert valid_times_duration["nullable"] is True
    assert "default" not in valid_times_duration
    assert valid_times_duration["enum"] == flood_map_duration


def test_mvt_route_variant_runtime_openapi_matches_static_enums_and_defaults() -> None:
    static_spec = _openapi_spec()
    app.openapi_schema = None
    runtime_spec: dict[str, Any] = app.openapi()

    parameter_cases = [
        (
            "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
            "path",
            "variable",
        ),
        (
            "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
            "path",
            "duration",
        ),
        ("/api/v1/tiles/flood-return-period", "query", "duration"),
        ("/api/v1/layers/{layer_id}/valid-times", "query", "duration"),
    ]

    for path, location, name in parameter_cases:
        static_param = _operation_parameter(static_spec, path, "get", location, name)
        runtime_param = _operation_parameter(runtime_spec, path, "get", location, name)

        assert runtime_param["required"] == static_param["required"]
        assert runtime_param["schema"] == static_param["schema"]

    flood_duration = _operation_parameter(
        runtime_spec,
        "/api/v1/tiles/flood-return-period",
        "get",
        "query",
        "duration",
    )["schema"]
    assert flood_duration["default"] == DEFAULT_FLOOD_RETURN_PERIOD_DURATION
    assert flood_duration["enum"] == list(SUPPORTED_FLOOD_RETURN_PERIOD_DURATIONS)


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

    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get(path)
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "TILE_XYZ_INVALID"
    assert body["error"]["details"]["z"] == 0
    assert body["error"]["details"]["max_exclusive"] == 1


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


def test_basin_limit_parameters_match_fastapi_runtime_contract() -> None:
    static_spec = _openapi_spec()
    fastapi_spec: dict[str, Any] = app.openapi()

    expected = {
        "/api/v1/basins": {"default": 200, "maximum": 500},
        "/api/v1/basins/{basin_id}/versions": {"default": 50, "maximum": 500},
    }
    for path, expected_schema in expected.items():
        static_limit = _parameter_by_name(static_spec["paths"][path]["get"]["parameters"], static_spec, "limit")
        fastapi_limit = _parameter_by_name(fastapi_spec["paths"][path]["get"]["parameters"], fastapi_spec, "limit")
        assert static_limit["schema"]["default"] == expected_schema["default"]
        assert static_limit["schema"]["maximum"] == expected_schema["maximum"]
        assert fastapi_limit["schema"]["default"] == expected_schema["default"]
        assert fastapi_limit["schema"]["maximum"] == expected_schema["maximum"]


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


def _parameter_by_name(parameters: list[dict[str, Any]], spec: dict[str, Any], name: str) -> dict[str, Any]:
    for parameter in parameters:
        if "$ref" in parameter:
            ref = parameter["$ref"].removeprefix("#/")
            resolved: Any = spec
            for part in ref.split("/"):
                resolved = resolved[part]
            parameter = resolved
        if parameter.get("name") == name:
            return parameter
    raise AssertionError(f"parameter not found: {name}")


def _operation_parameters_by_name(operation: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for parameter in operation["parameters"]:
        resolved = _resolve_ref(parameter["$ref"], spec) if "$ref" in parameter else parameter
        parameters[resolved["name"]] = resolved
    return parameters


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
