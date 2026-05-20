import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apps.api.errors import register_error_handlers
from apps.api.routes.best_available import router as best_available_router
from apps.api.routes.data_sources import router as data_sources_router
from apps.api.routes.flood_alerts import TILE_X_DESCRIPTION, TILE_Y_DESCRIPTION
from apps.api.routes.flood_alerts import router as flood_alerts_router
from apps.api.routes.forecast import router as forecast_router
from apps.api.routes.hindcast import router as hindcast_router
from apps.api.routes.models import router as models_router
from apps.api.routes.pipeline import router as pipeline_router
from apps.api.routes.state_snapshots import router as state_snapshots_router
from services.slurm_gateway.routes import router as slurm_router
from services.tiles.mvt import (
    DEFAULT_FLOOD_RETURN_PERIOD_DURATION,
    MVT_MAX_TILE_COORDINATE,
    MVT_MAX_ZOOM,
    SUPPORTED_FLOOD_RETURN_PERIOD_DURATIONS,
    SUPPORTED_HYDRO_MVT_VARIABLES,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = REPO_ROOT / "apps" / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST_DIR / "index.html"

app = FastAPI(
    title="NHMS API",
    description="National Hydrological Modeling System API",
    version="0.1.0",
)

register_error_handlers(app)
app.include_router(models_router)
app.include_router(forecast_router)
app.include_router(hindcast_router)
app.include_router(best_available_router)
app.include_router(data_sources_router)
app.include_router(state_snapshots_router)
app.include_router(pipeline_router)
app.include_router(flood_alerts_router)
app.include_router(slurm_router)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    _patch_mvt_tile_openapi(schema)
    _patch_flood_duration_openapi(schema)
    _patch_layer_metadata_openapi(schema)
    app.openapi_schema = schema
    return app.openapi_schema


def _patch_mvt_tile_openapi(schema: dict) -> None:
    mvt_paths = (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
    )
    for path in mvt_paths:
        operation = schema.get("paths", {}).get(path, {}).get("get", {})
        for parameter in operation.get("parameters", []):
            name = parameter.get("name")
            if path == "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf" and name == "variable":
                parameter["schema"] = {"type": "string", "enum": list(SUPPORTED_HYDRO_MVT_VARIABLES)}
            if (
                path == "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf"
                and name == "duration"
            ):
                parameter["schema"] = {"type": "string", "enum": list(SUPPORTED_FLOOD_RETURN_PERIOD_DURATIONS)}
            if name == "z":
                parameter["description"] = "Web Mercator XYZ zoom level."
                parameter.setdefault("schema", {})["minimum"] = 0
                parameter["schema"]["maximum"] = MVT_MAX_ZOOM
            if name == "x":
                parameter["description"] = TILE_X_DESCRIPTION
                parameter.setdefault("schema", {})["minimum"] = 0
                parameter["schema"]["maximum"] = MVT_MAX_TILE_COORDINATE
            if name == "y":
                parameter["description"] = TILE_Y_DESCRIPTION
                parameter.setdefault("schema", {})["minimum"] = 0
                parameter["schema"]["maximum"] = MVT_MAX_TILE_COORDINATE


app.openapi = custom_openapi


def _patch_flood_duration_openapi(schema: dict) -> None:
    duration_schema = {
        "type": "string",
        "default": DEFAULT_FLOOD_RETURN_PERIOD_DURATION,
        "enum": list(SUPPORTED_FLOOD_RETURN_PERIOD_DURATIONS),
    }
    flood_map_duration = _operation_parameter(
        schema,
        "/api/v1/tiles/flood-return-period",
        name="duration",
        location="query",
    )
    if flood_map_duration is not None:
        flood_map_duration["schema"] = dict(duration_schema)

    valid_times_duration = _operation_parameter(
        schema,
        "/api/v1/layers/{layer_id}/valid-times",
        name="duration",
        location="query",
    )
    if valid_times_duration is not None:
        valid_times_duration["schema"] = {
            "type": "string",
            "nullable": True,
            "enum": list(SUPPORTED_FLOOD_RETURN_PERIOD_DURATIONS),
        }


def _patch_layer_metadata_openapi(schema: dict) -> None:
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components.pop("Layer", None)
    components.pop("LayerMetadata", None)
    components.pop("LayerValidTimes", None)
    layer_list_response = components.pop("LayerListResponse", None)
    layer_valid_times_response = components.pop("LayerValidTimesResponse", None)
    api_success_envelope = components.pop("ApiSuccessEnvelope", None)

    if api_success_envelope is not None:
        components.setdefault("SuccessEnvelope", api_success_envelope)
    components["Layer"] = _layer_schema()
    components["LayerMetadata"] = _layer_metadata_schema()
    components["LayerValidTimes"] = _layer_valid_times_schema()

    if layer_list_response is not None:
        layer_list_response = _success_response_schema(
            {"type": "array", "items": {"$ref": "#/components/schemas/Layer"}}
        )
        _set_operation_response_schema(schema, "/api/v1/layers", layer_list_response)

    if layer_valid_times_response is not None:
        layer_valid_times_response = _success_response_schema({"$ref": "#/components/schemas/LayerValidTimes"})
        _set_operation_response_schema(schema, "/api/v1/layers/{layer_id}/valid-times", layer_valid_times_response)


def _operation_parameter(schema: dict, path: str, *, name: str, location: str) -> dict | None:
    operation = schema.get("paths", {}).get(path, {}).get("get", {})
    for parameter in operation.get("parameters", []):
        if parameter.get("name") == name and parameter.get("in") == location:
            return parameter
    return None


def _success_response_schema(data_schema: dict) -> dict:
    return {
        "allOf": [
            {"$ref": "#/components/schemas/SuccessEnvelope"},
            {
                "type": "object",
                "required": ["data"],
                "properties": {"data": data_schema},
            },
        ]
    }


def _set_operation_response_schema(schema: dict, path: str, response_schema: dict) -> None:
    operation = schema.get("paths", {}).get(path, {}).get("get")
    if not operation:
        return
    response = operation.get("responses", {}).get("200", {})
    content = response.get("content", {}).get("application/json", {})
    content["schema"] = response_schema


def _layer_schema() -> dict:
    return {
        "type": "object",
        "required": ["layer_id", "layer_name", "layer_type", "variables"],
        "properties": {
            "layer_id": {"type": "string"},
            "layer_name": {"type": "string"},
            "layer_type": {"type": "string"},
            "variables": {"type": "array", "items": {"type": "string"}},
            "metadata": {
                "oneOf": [
                    {"$ref": "#/components/schemas/LayerMetadata"},
                    {"type": "null"},
                ]
            },
        },
    }


def _layer_valid_times_schema() -> dict:
    return {
        "type": "object",
        "required": ["valid_times", "items", "limit", "observed_count", "truncated"],
        "properties": {
            "valid_times": {"type": "array", "items": {"type": "string", "format": "date-time"}},
            "items": {"type": "array", "items": {"type": "string", "format": "date-time"}},
            "limit": {"type": "integer"},
            "observed_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
    }


def _nullable(schema: dict) -> dict:
    return {**schema, "nullable": True}


def _layer_metadata_schema() -> dict:
    string_array = {"type": "array", "items": {"type": "string"}}
    number_array = {"type": "array", "items": {"type": "number"}}
    return {
        "type": "object",
        "required": ["layer_id", "tile_format", "fallback_available", "release_blocking"],
        "properties": {
            "layer_id": {"type": "string"},
            "tile_format": {"type": "string", "enum": ["mvt", "geojson_compatibility"]},
            "url_template": _nullable({"type": "string"}),
            "tile_url_template": _nullable({"type": "string"}),
            "required_placeholders": string_array,
            "maplibre_source_layer": _nullable({"type": "string"}),
            "source_layer": _nullable({"type": "string"}),
            "property_schema_version": _nullable({"type": "string"}),
            "schema_version": _nullable({"type": "string"}),
            "encoder_version": _nullable({"type": "string"}),
            "property_schema": _nullable({"type": "object", "additionalProperties": True}),
            "min_zoom": _nullable({"type": "integer"}),
            "max_zoom": _nullable({"type": "integer"}),
            "bounds_crs": _nullable({"type": "string"}),
            "bounds": _nullable(number_array),
            "wgs84_bounds": _nullable(number_array),
            "valid_times": {"type": "array", "items": {"type": "string", "format": "date-time"}},
            "valid_time_limit": {"type": "integer"},
            "valid_time_observed_count": {"type": "integer"},
            "valid_times_truncated": {"type": "boolean"},
            "source_refs": _nullable({"type": "object", "additionalProperties": True}),
            "cache_layer_id": _nullable({"type": "string"}),
            "route_variable": _nullable({"type": "string"}),
            "legacy_layer_ids": string_array,
            "alias_of": _nullable({"type": "string"}),
            "alias_semantic": _nullable({"type": "string"}),
            "canonical_route_layer_id": _nullable({"type": "string"}),
            "cache_etag": _nullable({"type": "string"}),
            "cache_version": _nullable({"type": "string"}),
            "fallback_available": {"type": "boolean"},
            "fallback_endpoint": _nullable({"type": "string"}),
            "release_blocking": {"type": "boolean"},
            "production_mvt_readiness_claimed": _nullable({"type": "boolean"}),
        },
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nhms-api",
        "version": "0.1.0",
    }


app.mount(
    "/assets",
    StaticFiles(directory=FRONTEND_DIST_DIR / "assets", check_dir=False),
    name="frontend-assets",
)


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    if full_path.startswith("api/") or full_path == "api":
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(FRONTEND_INDEX)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("apps.api.main:app", host="0.0.0.0", port=port, reload=True)
