import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apps.api.auth import audit_record, evaluate_request_action
from apps.api.errors import error_response, register_error_handlers
from apps.api.routes.best_available import router as best_available_router
from apps.api.routes.data_sources import router as data_sources_router
from apps.api.routes.flood_alerts import TILE_X_DESCRIPTION, TILE_Y_DESCRIPTION
from apps.api.routes.flood_alerts import router as flood_alerts_router
from apps.api.routes.forecast import router as forecast_router
from apps.api.routes.hindcast import router as hindcast_router
from apps.api.routes.models import router as models_router
from apps.api.routes.pipeline import (
    PIPELINE_JOB_STATUS_VALUES,
    PIPELINE_PUBLIC_LOG_URI_MAX_LENGTH,
    PIPELINE_STAGE_BASIN_RESULTS_LIMIT,
)
from apps.api.routes.pipeline import router as pipeline_router
from apps.api.routes.state_snapshots import router as state_snapshots_router
from apps.api.runtime_mode import RuntimeConfig, load_runtime_config
from packages.common.forecast_store import MAX_STATION_SERIES_LIMIT
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

_PRE_BODY_PROTECTED_MUTATIONS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("POST", "/api/v1/basins"): ("models.switch_version", "model_registry", "basins"),
    ("POST", "/api/v1/river-networks"): ("models.switch_version", "model_registry", "river-networks"),
    ("POST", "/api/v1/mesh-versions"): ("models.switch_version", "model_registry", "mesh-versions"),
    ("POST", "/api/v1/models"): ("models.switch_version", "model_registry", "models"),
    (
        "POST",
        "/api/v1/river-segment-crosswalks",
    ): ("models.switch_version", "model_registry", "river-segment-crosswalks"),
    ("POST", "/api/v1/hindcast/submit"): ("pipeline.rerun_cycle", "hindcast", "pre-body"),
}
_ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES = 4096
runtime_router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])


async def protected_mutation_auth_guard(request: Any, call_next: Any) -> Any:
    path_policy = await _protected_mutation_policy(request)
    if path_policy is None:
        return await call_next(request)
    if isinstance(path_policy, _PreBodyPolicyError):
        return error_response(
            request,
            status_code=path_policy.status_code,
            code=path_policy.code,
            message=path_policy.message,
            details=path_policy.details,
        )

    request_id = _ensure_request_id(request)
    action_id, target_type, target_id = path_policy
    decision = evaluate_request_action(request, action_id, target_type=target_type, target_id=target_id)
    if decision.decision == "allow":
        return await call_next(request)

    audit = audit_record(decision, request_id=request_id)
    details = {"policy_decision": decision.to_dict(), "audit_record": audit}
    if decision.decision == "release_blocked":
        details["removal_criteria"] = "Configure and prove live backend identity-provider role mapping."
        return error_response(
            request,
            status_code=503,
            code="RELEASE_BLOCKED",
            message=decision.reason,
            details=details,
        )
    if decision.reason_code == "AUTH_REQUIRED":
        return error_response(
            request,
            status_code=401,
            code="AUTH_REQUIRED",
            message=decision.reason,
            details=details,
        )
    return error_response(
        request,
        status_code=403,
        code="RBAC_FORBIDDEN",
        message=decision.reason,
        details=details,
    )


class _PreBodyPolicyError:
    def __init__(self, *, status_code: int, code: str, message: str, details: Any | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


async def _protected_mutation_policy(request: Any) -> tuple[str, str, str] | _PreBodyPolicyError | None:
    method = request.method.upper()
    path = request.url.path
    policy = _PRE_BODY_PROTECTED_MUTATIONS.get((method.upper(), path))
    if policy is not None:
        return policy
    if method == "POST" and path.startswith("/api/v1/basins/") and path.endswith("/versions"):
        basin_id = path.removeprefix("/api/v1/basins/").removesuffix("/versions")
        if basin_id and "/" not in basin_id:
            return ("models.switch_version", "model_registry", basin_id)
    if method == "PUT" and path.startswith("/api/v1/models/") and path.endswith("/active"):
        model_id = path.removeprefix("/api/v1/models/").removesuffix("/active")
        if model_id and "/" not in model_id:
            active = await _active_toggle_flag(request)
            if isinstance(active, _PreBodyPolicyError):
                return active
            action_id = "models.activate" if active else "models.deactivate"
            return (action_id, "model_instance", model_id)
    if (
        method == "POST"
        and path.startswith("/api/v1/models/")
        and (path.endswith("/lifecycle") or path.endswith("/preflight"))
    ):
        suffix = "/lifecycle" if path.endswith("/lifecycle") else "/preflight"
        model_id = path.removeprefix("/api/v1/models/").removesuffix(suffix)
        if model_id and "/" not in model_id:
            operation = await _model_lifecycle_operation_flag(request)
            if isinstance(operation, _PreBodyPolicyError):
                return operation
            action_id = {
                "activate": "models.activate",
                "deactivate": "models.deactivate",
                "switch_version": "models.switch_version",
                "rollback_version": "models.rollback_version",
                "supersede": "models.supersede",
                "deprecate": "models.deactivate",
            }[operation]
            return (action_id, "model_instance", model_id)
    return None


async def _active_toggle_flag(request: Any) -> bool | _PreBodyPolicyError:
    request_id = _ensure_request_id(request)
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES:
            return _active_toggle_validation_error(request_id)
    except ValueError:
        return _active_toggle_validation_error(request_id)

    body = await _read_bounded_active_toggle_body(request)
    if body is None:
        return _active_toggle_validation_error(request_id)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _active_toggle_validation_error(request_id)
    if not isinstance(payload, dict):
        return _active_toggle_validation_error(request_id)
    active = payload.get("active", payload.get("active_flag"))
    if not isinstance(active, bool):
        return _active_toggle_validation_error(request_id)
    return active


async def _read_bounded_active_toggle_body(request: Any) -> bytes | None:
    chunks: list[bytes] = []
    buffered = 0
    max_with_sentinel = _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES + 1

    while True:
        message = await request._receive()
        if message.get("type") == "http.disconnect":
            return None

        chunk = message.get("body", b"")
        if chunk:
            remaining = max_with_sentinel - buffered
            if remaining > 0:
                chunks.append(chunk[:remaining])
                buffered += min(len(chunk), remaining)
            if buffered > _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES:
                return None

        if not message.get("more_body", False):
            break

    return b"".join(chunks)


def _active_toggle_validation_error(request_id: str) -> _PreBodyPolicyError:
    return _PreBodyPolicyError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details=[
            {
                "field": "body.active",
                "rejected_value": None,
                "reason": (
                    "Active-toggle requests require a bounded JSON object with boolean active "
                    "or active_flag before authorization can be evaluated."
                ),
                "request_id": request_id,
            }
        ],
    )


async def _model_lifecycle_operation_flag(request: Any) -> str | _PreBodyPolicyError:
    request_id = _ensure_request_id(request)
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES:
            return _model_lifecycle_validation_error(request_id)
    except ValueError:
        return _model_lifecycle_validation_error(request_id)

    body = await _read_bounded_active_toggle_body(request)
    if body is None:
        return _model_lifecycle_validation_error(request_id)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _model_lifecycle_validation_error(request_id)
    if not isinstance(payload, dict):
        return _model_lifecycle_validation_error(request_id)
    operation = payload.get("operation")
    if operation not in {"activate", "deactivate", "switch_version", "rollback_version", "supersede", "deprecate"}:
        return _model_lifecycle_validation_error(request_id)
    return str(operation)


def _model_lifecycle_validation_error(request_id: str) -> _PreBodyPolicyError:
    return _PreBodyPolicyError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details=[
            {
                "field": "body.operation",
                "rejected_value": None,
                "reason": "Model lifecycle requests require a bounded JSON object with a supported operation.",
                "request_id": request_id,
            }
        ],
    )


def _ensure_request_id(request: Any) -> str:
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        return request_id
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    request.state.request_id = request_id
    return request_id


@runtime_router.get("/config")
def runtime_config(request: Request) -> dict[str, Any]:
    config: RuntimeConfig = request.app.state.runtime_config
    return _ok(request, config.public_dict())


def create_app(env: Mapping[str, str] | None = None) -> FastAPI:
    runtime_config = load_runtime_config(env)
    api = FastAPI(
        title="NHMS API",
        description="National Hydrological Modeling System API",
        version="0.1.0",
    )
    api.state.runtime_config = runtime_config

    register_error_handlers(api)
    api.middleware("http")(protected_mutation_auth_guard)

    api.include_router(models_router)
    api.include_router(forecast_router)
    api.include_router(hindcast_router)
    api.include_router(best_available_router)
    api.include_router(data_sources_router)
    api.include_router(state_snapshots_router)
    api.include_router(pipeline_router)
    api.include_router(flood_alerts_router)
    api.include_router(runtime_router)
    if runtime_config.slurm_routes_enabled:
        api.include_router(slurm_router)

    _register_static_and_health_routes(api)
    api.openapi = _custom_openapi_factory(api)
    return api


def _custom_openapi_factory(api: FastAPI) -> Any:
    def custom_openapi() -> dict[str, Any]:
        if api.openapi_schema:
            return api.openapi_schema
        schema = get_openapi(
            title=api.title,
            version=api.version,
            description=api.description,
            routes=api.routes,
        )
        _patch_mvt_tile_openapi(schema)
        _patch_flood_duration_openapi(schema)
        _patch_station_series_openapi(schema)
        _patch_qhh_latest_product_openapi(schema)
        _patch_layer_metadata_openapi(schema)
        _patch_pipeline_openapi(schema)
        _patch_runtime_openapi(schema)
        api.openapi_schema = schema
        return api.openapi_schema

    return custom_openapi


def custom_openapi() -> dict[str, Any]:
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
    _patch_station_series_openapi(schema)
    _patch_qhh_latest_product_openapi(schema)
    _patch_layer_metadata_openapi(schema)
    _patch_pipeline_openapi(schema)
    _patch_runtime_openapi(schema)
    app.openapi_schema = schema
    return app.openapi_schema


def _patch_mvt_tile_openapi(schema: dict) -> None:
    mvt_paths = (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
    )
    _ensure_mvt_live_postgis_unavailable_response(schema)
    for path in mvt_paths:
        operation = schema.get("paths", {}).get(path, {}).get("get", {})
        operation.setdefault("responses", {})["424"] = {
            "$ref": "#/components/responses/MvtLivePostgisUnavailable"
        }
        operation["responses"]["4XX"] = {"$ref": "#/components/responses/Error"}
        operation["responses"]["5XX"] = {"$ref": "#/components/responses/Error"}
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

def _ensure_mvt_live_postgis_unavailable_response(schema: dict) -> None:
    responses = schema.setdefault("components", {}).setdefault("responses", {})
    responses["MvtLivePostgisUnavailable"] = {
        "description": "Live PostGIS MVT is unavailable for this canonical tile route.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["request_id", "status", "error"],
                    "properties": {
                        "request_id": {"type": "string"},
                        "status": {"type": "string", "enum": ["error"]},
                        "error": {
                            "type": "object",
                            "required": ["code", "message"],
                            "properties": {
                                "code": {"type": "string", "enum": ["MVT_LIVE_POSTGIS_UNAVAILABLE"]},
                                "message": {"type": "string"},
                                "details": {
                                    "type": "object",
                                    "nullable": True,
                                    "additionalProperties": True,
                                },
                            },
                        },
                    },
                }
            }
        },
    }


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


def _patch_station_series_openapi(schema: dict) -> None:
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SuccessEnvelope"] = _success_envelope_schema()
    schemas["ErrorResponse"] = _error_response_schema()
    schemas["ValidationErrorDetail"] = _validation_error_detail_schema()
    schemas["StationSeriesPoint"] = _station_series_point_schema()
    schemas["StationSeriesStation"] = _station_series_station_schema()
    schemas["StationSeriesMetadata"] = _station_series_metadata_schema()
    schemas["StationSeries"] = _station_series_schema()
    schemas["StationSeriesResponse"] = _station_series_response_schema()

    responses = components.setdefault("responses", {})
    responses["Error"] = {
        "description": "Error response",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    }

    operation = schema.get("paths", {}).get("/api/v1/met/stations/{station_id}/series", {}).get("get")
    if not operation:
        return
    operation["summary"] = "Get station forcing time series"
    operation["tags"] = ["met"]
    operation["parameters"] = _station_series_parameters()
    operation["responses"] = {
        "200": {
            "description": "Station time series",
            "content": {
                "application/json": {
                    "schema": _success_response_schema({"$ref": "#/components/schemas/StationSeriesResponse"})
                }
            },
        },
        "4XX": {"$ref": "#/components/responses/Error"},
        "5XX": {"$ref": "#/components/responses/Error"},
    }


def _patch_qhh_latest_product_openapi(schema: dict) -> None:
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SuccessEnvelope"] = _success_envelope_schema()
    schemas["ErrorResponse"] = _error_response_schema()
    schemas["ValidationErrorDetail"] = _validation_error_detail_schema()
    schemas["QhhLatestUnavailableReason"] = _qhh_latest_unavailable_reason_schema()
    schemas["QhhLatestQualityNote"] = _qhh_latest_quality_note_schema()
    schemas["QhhLatestStationVariableCoverage"] = _qhh_latest_station_variable_coverage_schema()
    schemas["QhhLatestQueryIndex"] = _qhh_latest_query_index_schema()
    schemas["QhhLatestAvailability"] = _qhh_latest_availability_schema()
    schemas["QhhLatestQuality"] = _qhh_latest_quality_schema()
    schemas["QhhLatestProduct"] = _qhh_latest_product_schema()

    responses = components.setdefault("responses", {})
    responses["Error"] = {
        "description": "Error response",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    }

    operation = schema.get("paths", {}).get("/api/v1/mvp/qhh/latest-product", {}).get("get")
    if not operation:
        return
    operation["summary"] = "Get latest QHH display product"
    operation["tags"] = ["runs"]
    operation["parameters"] = [
        {
            "name": "source",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "enum": ["GFS", "IFS"]},
            "description": "MVP forecast source. Accepted case-insensitively and normalized to GFS or IFS.",
        },
        {
            "name": "run_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "minLength": 1},
            "description": (
                "Strict QHH run identity. If supplied, source, cycle_time, and model_id must also be supplied; "
                "the API will not fall back to source-only latest selection."
            ),
        },
        {
            "name": "cycle_time",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "date-time"},
            "description": (
                "Strict QHH cycle time. If supplied, source, run_id, and model_id must also be supplied; "
                "the API will not fall back to source-only latest selection."
            ),
        },
        {
            "name": "model_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "minLength": 1},
            "description": (
                "Strict QHH model identity. If supplied, source, run_id, and cycle_time must also be supplied; "
                "the API will not fall back to source-only latest selection."
            ),
        },
    ]
    operation["responses"] = {
        "200": {
            "description": "Latest QHH display product",
            "content": {
                "application/json": {
                    "schema": _success_response_schema({"$ref": "#/components/schemas/QhhLatestProduct"})
                }
            },
        },
        "4XX": {"$ref": "#/components/responses/Error"},
        "5XX": {"$ref": "#/components/responses/Error"},
    }


def _patch_pipeline_openapi(schema: dict) -> None:
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SuccessEnvelope"] = _success_envelope_schema()
    schemas["ErrorResponse"] = _error_response_schema()
    schemas["ValidationErrorDetail"] = _validation_error_detail_schema()
    schemas["JobStatusCounts"] = _job_status_counts_schema()
    schemas["PipelineStatus"] = _pipeline_status_schema()
    schemas["BasinProgress"] = _basin_progress_schema()
    schemas["BasinResult"] = _basin_result_schema()
    schemas["PipelineStage"] = _pipeline_stage_schema()
    schemas["PipelineJob"] = _pipeline_job_schema()
    schemas["PipelineJobPage"] = _pipeline_job_page_schema()
    schemas["JobLogs"] = _job_logs_schema()
    schemas["RetryRunResult"] = _retry_run_result_schema()

    responses = components.setdefault("responses", {})
    responses["Error"] = {
        "description": "Error response",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    }

    _patch_pipeline_operation(
        schema,
        "/api/v1/pipeline/status",
        "get",
        operation_id="getPipelineStatus",
        summary="Get pipeline status for a cycle",
        tags=["pipeline"],
        parameters=[_source_query_parameter(required=True), _cycle_time_query_parameter(required=True)],
        data_schema={"$ref": "#/components/schemas/PipelineStatus"},
        description="Pipeline status",
    )
    _patch_pipeline_operation(
        schema,
        "/api/v1/pipeline/stages",
        "get",
        operation_id="listPipelineStages",
        summary="List pipeline stages for a cycle",
        tags=["pipeline"],
        parameters=[_source_query_parameter(required=True), _cycle_time_query_parameter(required=True)],
        data_schema={"type": "array", "items": {"$ref": "#/components/schemas/PipelineStage"}},
        description="Pipeline stage list",
    )
    _patch_pipeline_operation(
        schema,
        "/api/v1/jobs",
        "get",
        operation_id="listPipelineJobs",
        summary="List pipeline jobs",
        tags=["pipeline"],
        parameters=[
            _source_query_parameter(required=False),
            _cycle_time_query_parameter(required=False),
            {"name": "status", "in": "query", "required": False, "schema": {"type": "string"}},
            {"name": "model_id", "in": "query", "required": False, "schema": {"type": "string"}},
            {"name": "stage", "in": "query", "required": False, "schema": {"type": "string"}},
            {"name": "run_type", "in": "query", "required": False, "schema": {"type": "string"}},
            {"name": "scenario", "in": "query", "required": False, "schema": {"type": "string"}},
            {
                "name": "sort_by",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "enum": ["submitted_at", "duration_seconds"], "default": "submitted_at"},
            },
            {
                "name": "sort_order",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
            },
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            {
                "name": "offset",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 0, "default": 0},
            },
        ],
        data_schema={"$ref": "#/components/schemas/PipelineJobPage"},
        description="Pipeline job list",
    )
    _patch_pipeline_operation(
        schema,
        "/api/v1/jobs/{job_id}/logs",
        "get",
        operation_id="getPipelineJobLogs",
        summary="Get pipeline job logs",
        tags=["pipeline"],
        parameters=[{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}],
        data_schema={"$ref": "#/components/schemas/JobLogs"},
        description="Pipeline job logs",
    )
    _patch_pipeline_operation(
        schema,
        "/api/v1/runs/{run_id}/retry",
        "post",
        operation_id="retryRun",
        summary="Retry a failed or cancelled run",
        tags=["runs"],
        parameters=[
            {"name": "run_id", "in": "path", "required": True, "schema": {"type": "string"}},
            {
                "name": "X-User-Role",
                "in": "header",
                "required": False,
                "schema": {"type": "string", "enum": ["operator", "model_admin", "sys_admin"]},
            },
        ],
        data_schema={"$ref": "#/components/schemas/RetryRunResult"},
        description="Retry request accepted",
    )


def _patch_runtime_openapi(schema: dict) -> None:
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SuccessEnvelope"] = _success_envelope_schema()
    schemas["ErrorResponse"] = _error_response_schema()
    schemas["ValidationErrorDetail"] = _validation_error_detail_schema()
    schemas["RuntimeConfig"] = _runtime_config_schema()

    responses = components.setdefault("responses", {})
    responses["Error"] = {
        "description": "Error response",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    }

    operation = schema.get("paths", {}).get("/api/v1/runtime/config", {}).get("get")
    if not operation:
        return
    operation["operationId"] = "getRuntimeConfig"
    operation["summary"] = "Get runtime service configuration"
    operation["tags"] = ["runtime"]
    operation["responses"] = {
        "200": {
            "description": "Runtime service configuration",
            "content": {
                "application/json": {
                    "schema": _success_response_schema({"$ref": "#/components/schemas/RuntimeConfig"})
                }
            },
        },
        "4XX": {"$ref": "#/components/responses/Error"},
        "5XX": {"$ref": "#/components/responses/Error"},
    }


def _patch_pipeline_operation(
    schema: dict,
    path: str,
    method: str,
    *,
    operation_id: str,
    summary: str,
    tags: list[str],
    parameters: list[dict[str, Any]],
    data_schema: dict[str, Any],
    description: str,
) -> None:
    operation = schema.get("paths", {}).get(path, {}).get(method)
    if not operation:
        return
    operation["operationId"] = operation_id
    operation["summary"] = summary
    operation["tags"] = tags
    operation["parameters"] = parameters
    operation["responses"] = {
        "200": {
            "description": description,
            "content": {"application/json": {"schema": _success_response_schema(data_schema)}},
        },
        "4XX": {"$ref": "#/components/responses/Error"},
        "5XX": {"$ref": "#/components/responses/Error"},
    }


def _source_query_parameter(*, required: bool) -> dict[str, Any]:
    return {"name": "source", "in": "query", "required": required, "schema": {"type": "string"}}


def _cycle_time_query_parameter(*, required: bool) -> dict[str, Any]:
    return {
        "name": "cycle_time",
        "in": "query",
        "required": required,
        "schema": {"type": "string", "format": "date-time"},
    }


def _station_series_parameters() -> list[dict[str, Any]]:
    return [
        {
            "name": "station_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        },
        {
            "name": "forcing_version_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "minLength": 1},
        },
        {
            "name": "model_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "minLength": 1},
        },
        {
            "name": "source_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "minLength": 1},
        },
        {
            "name": "cycle_time",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "date-time"},
        },
        {
            "name": "variables",
            "in": "query",
            "required": False,
            "style": "form",
            "explode": True,
            "schema": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "description": (
                "Station forcing variables. Repeat the parameter or provide comma-separated values. "
                "Allowed values are PRCP, TEMP, RH, wind, Rn, and Press."
            ),
        },
        {
            "name": "from",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "date-time"},
        },
        {
            "name": "to",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "date-time"},
        },
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1, "maximum": MAX_STATION_SERIES_LIMIT},
        },
    ]


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


def _success_envelope_schema() -> dict:
    return {
        "type": "object",
        "description": "Standard success envelope returned by API endpoints using the `_ok()` response pattern.",
        "required": ["request_id", "status"],
        "properties": {
            "request_id": {"type": "string", "example": "req_01J0NHMS"},
            "status": {"type": "string", "enum": ["ok"], "example": "ok"},
        },
    }


def _error_response_schema() -> dict:
    return {
        "type": "object",
        "required": ["request_id", "status", "error"],
        "properties": {
            "request_id": {"type": "string", "example": "req_01J0NHMS"},
            "status": {"type": "string", "enum": ["error"], "example": "error"},
            "error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string", "example": "NOT_FOUND"},
                    "message": {"type": "string", "example": "Requested resource was not found."},
                    "details": _error_details_schema(),
                },
            },
        },
    }


def _error_details_schema() -> dict:
    return {
        "oneOf": [
            {"type": "object", "nullable": True, "additionalProperties": True},
            {
                "type": "array",
                "items": {"$ref": "#/components/schemas/ValidationErrorDetail"},
            },
        ]
    }


def _validation_error_detail_schema() -> dict:
    return {
        "type": "object",
        "required": ["field", "reason"],
        "properties": {
            "field": {"type": "string"},
            "rejected_value": _json_value_schema(),
            "reason": {"type": "string"},
        },
        "additionalProperties": True,
    }


def _job_status_counts_schema() -> dict:
    return {
        "type": "object",
        "required": ["succeeded", "failed", "running", "pending"],
        "properties": {
            "succeeded": {"type": "integer", "minimum": 0},
            "failed": {"type": "integer", "minimum": 0},
            "running": {"type": "integer", "minimum": 0},
            "pending": {"type": "integer", "minimum": 0},
        },
    }


def _pipeline_status_schema() -> dict:
    return {
        "type": "object",
        "required": ["cycle_id", "source", "cycle_time", "current_state", "started_at", "updated_at", "job_counts"],
        "properties": {
            "cycle_id": {"type": "string"},
            "source": {"type": "string", "nullable": True},
            "cycle_time": {"type": "string", "format": "date-time", "nullable": True},
            "current_state": {"type": "string"},
            "started_at": {"type": "string", "format": "date-time", "nullable": True},
            "updated_at": {"type": "string", "format": "date-time", "nullable": True},
            "job_counts": {"$ref": "#/components/schemas/JobStatusCounts"},
        },
    }


def _basin_progress_schema() -> dict:
    return {
        "type": "object",
        "required": ["completed", "total", "failed"],
        "properties": {
            "completed": {"type": "integer", "minimum": 0},
            "total": {"type": "integer", "minimum": 0},
            "failed": {"type": "integer", "minimum": 0},
        },
    }


def _pipeline_stage_schema() -> dict:
    stage_statuses = ["pending", "running", "succeeded", "partially_failed", "failed", "skipped"]
    return {
        "type": "object",
        "required": [
            "stage",
            "display_status",
            "duration_seconds",
            "basin_progress",
            "basin_results",
            "basin_results_limit",
            "basin_results_total",
            "basin_results_returned",
            "basin_results_truncated",
        ],
        "properties": {
            "stage": {"type": "string"},
            "display_status": {"type": "string", "enum": stage_statuses},
            "status": {
                "type": "string",
                "enum": stage_statuses,
                "description": "Backward-compatible alias of display_status.",
            },
            "duration_seconds": {"type": "integer", "nullable": True},
            "basin_progress": {"$ref": "#/components/schemas/BasinProgress"},
            "basin_results_limit": {"type": "integer", "minimum": 0, "maximum": PIPELINE_STAGE_BASIN_RESULTS_LIMIT},
            "basin_results_total": {"type": "integer", "minimum": 0},
            "basin_results_returned": {"type": "integer", "minimum": 0, "maximum": PIPELINE_STAGE_BASIN_RESULTS_LIMIT},
            "basin_results_truncated": {"type": "boolean"},
            "basin_results": {
                "type": "array",
                "maxItems": PIPELINE_STAGE_BASIN_RESULTS_LIMIT,
                "items": {"$ref": "#/components/schemas/BasinResult"},
            },
        },
    }


def _basin_result_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "job_id",
            "run_id",
            "cycle_id",
            "job_type",
            "slurm_job_id",
            "model_id",
            "basin_id",
            "status",
            "stage",
            "submitted_at",
            "started_at",
            "finished_at",
            "duration_seconds",
            "retry_count",
            "error_code",
            "error_message",
            "log_uri",
        ],
        "properties": {
            "job_id": {"type": "string"},
            "run_id": {"type": "string", "nullable": True},
            "cycle_id": {"type": "string", "nullable": True},
            "job_type": {"type": "string"},
            "slurm_job_id": {"type": "string", "nullable": True},
            "model_id": {"type": "string", "nullable": True},
            "basin_id": {"type": "string", "nullable": True},
            "status": {"type": "string", "enum": list(PIPELINE_JOB_STATUS_VALUES)},
            "stage": {"type": "string", "nullable": True},
            "submitted_at": {"type": "string", "format": "date-time", "nullable": True},
            "started_at": {"type": "string", "format": "date-time", "nullable": True},
            "finished_at": {"type": "string", "format": "date-time", "nullable": True},
            "duration_seconds": {"type": "integer", "nullable": True},
            "retry_count": {"type": "integer", "minimum": 0},
            "error_code": {"type": "string", "nullable": True},
            "error_message": {"type": "string", "nullable": True},
            "log_uri": {"type": "string", "nullable": True, "maxLength": PIPELINE_PUBLIC_LOG_URI_MAX_LENGTH},
        },
    }


def _pipeline_job_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "job_id",
            "run_id",
            "cycle_id",
            "run_type",
            "scenario",
            "job_type",
            "slurm_job_id",
            "model_id",
            "status",
            "stage",
            "submitted_at",
            "started_at",
            "finished_at",
            "exit_code",
            "retry_count",
            "error_code",
            "error_message",
            "log_uri",
            "duration_seconds",
        ],
        "properties": {
            "job_id": {"type": "string"},
            "run_id": {"type": "string", "nullable": True},
            "cycle_id": {"type": "string", "nullable": True},
            "run_type": {"type": "string", "nullable": True},
            "scenario": {"type": "string", "nullable": True},
            "job_type": {"type": "string"},
            "slurm_job_id": {"type": "string", "nullable": True},
            "model_id": {"type": "string", "nullable": True},
            "status": {"type": "string", "enum": list(PIPELINE_JOB_STATUS_VALUES)},
            "stage": {"type": "string", "nullable": True},
            "submitted_at": {"type": "string", "format": "date-time", "nullable": True},
            "started_at": {"type": "string", "format": "date-time", "nullable": True},
            "finished_at": {"type": "string", "format": "date-time", "nullable": True},
            "exit_code": {"type": "integer", "nullable": True},
            "retry_count": {"type": "integer", "minimum": 0},
            "error_code": {"type": "string", "nullable": True},
            "error_message": {"type": "string", "nullable": True},
            "log_uri": {"type": "string", "nullable": True, "maxLength": PIPELINE_PUBLIC_LOG_URI_MAX_LENGTH},
            "duration_seconds": {"type": "integer", "nullable": True},
        },
    }


def _pipeline_job_page_schema() -> dict:
    return {
        "type": "object",
        "required": ["items", "total", "limit", "offset"],
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/components/schemas/PipelineJob"}},
            "total": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "offset": {"type": "integer", "minimum": 0},
        },
    }


def _job_logs_schema() -> dict:
    return {
        "type": "object",
        "required": ["job_id", "log_uri", "content"],
        "properties": {
            "job_id": {"type": "string"},
            "log_uri": {"type": "string", "maxLength": PIPELINE_PUBLIC_LOG_URI_MAX_LENGTH},
            "content": {"type": "string"},
        },
    }


def _runtime_config_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "service_role",
            "control_mutations_enabled",
            "slurm_routes_enabled",
            "queue_depth_mode",
            "display_readonly",
        ],
        "properties": {
            "service_role": {
                "type": "string",
                "enum": ["dev_monolith", "compute_control", "display_readonly", "slurm_gateway"],
            },
            "control_mutations_enabled": {"type": "boolean"},
            "slurm_routes_enabled": {"type": "boolean"},
            "queue_depth_mode": {
                "type": "string",
                "enum": ["slurm_gateway", "display_readonly_unavailable"],
            },
            "display_readonly": {"type": "boolean"},
        },
    }


def _retry_run_result_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "job_id",
            "pipeline_job_id",
            "run_id",
            "retry_count",
            "status",
            "slurm_job_id",
            "execution_status",
        ],
        "properties": {
            "job_id": {"type": "string"},
            "pipeline_job_id": {"type": "string", "description": "Alias of job_id for pipeline-control clients."},
            "run_id": {"type": "string", "nullable": True},
            "retry_count": {"type": "integer", "minimum": 0},
            "status": {"type": "string", "enum": ["submitted"]},
            "slurm_job_id": {"type": "string", "nullable": True},
            "execution_status": {"type": "string", "enum": ["submitted"]},
        },
    }


def _station_series_point_schema() -> dict:
    return {
        "type": "object",
        "required": ["valid_time", "value", "quality_flag"],
        "properties": {
            "valid_time": {"type": "string", "format": "date-time"},
            "value": {"type": "number"},
            "quality_flag": {"type": "string", "nullable": True},
            "source_id": {"type": "string", "nullable": True},
        },
    }


def _json_value_schema() -> dict:
    return {
        "oneOf": [
            {"type": "string", "nullable": True},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "object", "additionalProperties": True},
            {"type": "array", "items": {}},
        ]
    }


def _station_series_station_schema() -> dict:
    return {
        "type": "object",
        "required": ["station_id", "basin_version_id"],
        "properties": {
            "station_id": {"type": "string"},
            "basin_version_id": {"type": "string"},
            "station_name": {"type": "string", "nullable": True},
            "name": {"type": "string", "nullable": True},
            "longitude": {"type": "number", "nullable": True},
            "latitude": {"type": "number", "nullable": True},
            "elevation_m": {"type": "number", "nullable": True},
            "elevation": {"type": "number", "nullable": True},
            "station_role": {"type": "string", "nullable": True},
            "active_flag": {"type": "boolean", "nullable": True},
            "properties_json": {"type": "object", "nullable": True, "additionalProperties": True},
            "created_at": {"type": "string", "format": "date-time", "nullable": True},
        },
    }


def _station_series_metadata_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "limit",
            "returned_points",
            "requested_from",
            "requested_to",
            "returned_from",
            "returned_to",
            "truncated",
        ],
        "properties": {
            "limit": {"type": "integer", "minimum": 1},
            "returned_points": {"type": "integer", "minimum": 0},
            "requested_from": {"type": "string", "format": "date-time", "nullable": True},
            "requested_to": {"type": "string", "format": "date-time", "nullable": True},
            "returned_from": {"type": "string", "format": "date-time", "nullable": True},
            "returned_to": {"type": "string", "format": "date-time", "nullable": True},
            "truncated": {"type": "boolean"},
        },
    }


def _station_series_schema() -> dict:
    return {
        "type": "object",
        "required": ["variable", "unit", "native_resolution", "points", "truncated", "metadata"],
        "properties": {
            "variable": {"type": "string", "enum": ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"]},
            "unit": {"type": "string", "nullable": True},
            "native_resolution": {"type": "string", "nullable": True},
            "source_id": {"type": "string", "nullable": True},
            "cycle_time": {"type": "string", "format": "date-time", "nullable": True},
            "points": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/StationSeriesPoint"},
            },
            "truncated": {"type": "boolean"},
            "metadata": {"$ref": "#/components/schemas/StationSeriesMetadata"},
        },
    }


def _station_series_response_schema() -> dict:
    return {
        "type": "object",
        "required": ["station_id", "station", "forcing_version_id", "source_id", "limit", "series"],
        "properties": {
            "station_id": {"type": "string"},
            "station": {"$ref": "#/components/schemas/StationSeriesStation"},
            "forcing_version_id": {"type": "string"},
            "model_id": {"type": "string", "nullable": True},
            "source_id": {"type": "string"},
            "cycle_time": {"type": "string", "format": "date-time", "nullable": True},
            "valid_time_start": {"type": "string", "format": "date-time", "nullable": True},
            "valid_time_end": {"type": "string", "format": "date-time", "nullable": True},
            "limit": {"type": "integer", "minimum": 1},
            "requested_from": {"type": "string", "format": "date-time", "nullable": True},
            "requested_to": {"type": "string", "format": "date-time", "nullable": True},
            "series": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/StationSeries"},
            },
        },
    }


def _qhh_latest_unavailable_reason_schema() -> dict:
    return {
        "type": "object",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string"},
            "message": {"type": "string"},
            "run_id": {"type": "string", "nullable": True},
            "source_id": {"type": "string", "nullable": True},
        },
        "additionalProperties": True,
    }


def _qhh_latest_quality_note_schema() -> dict:
    return {
        "type": "object",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string"},
            "message": {"type": "string"},
            "expected_horizon_hours": {"type": "integer", "nullable": True},
            "available_horizon_hours": {"type": "integer", "nullable": True},
            "available_end_time": {"type": "string", "format": "date-time", "nullable": True},
        },
        "additionalProperties": True,
    }


def _qhh_latest_station_variable_coverage_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "variable",
            "station_count",
            "sample_count",
            "unit_count",
            "quality_flag_count",
            "missing_unit_samples",
            "missing_quality_flag_samples",
            "valid_time_start",
            "valid_time_end",
        ],
        "properties": {
            "variable": {"type": "string", "enum": ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"]},
            "station_count": {"type": "integer", "minimum": 0},
            "sample_count": {"type": "integer", "minimum": 0},
            "unit_count": {"type": "integer", "minimum": 0},
            "quality_flag_count": {"type": "integer", "minimum": 0},
            "missing_unit_samples": {"type": "integer", "minimum": 0},
            "missing_quality_flag_samples": {"type": "integer", "minimum": 0},
            "valid_time_start": {"type": "string", "format": "date-time", "nullable": True},
            "valid_time_end": {"type": "string", "format": "date-time", "nullable": True},
        },
    }


def _qhh_latest_query_index_schema() -> dict:
    return {
        "type": "object",
        "required": ["table", "index", "status", "columns"],
        "properties": {
            "table": {"type": "string"},
            "index": {"type": "string"},
            "status": {"type": "string"},
            "columns": {"type": "array", "items": {"type": "string"}},
            "predicate": {"type": "string", "nullable": True},
        },
        "additionalProperties": True,
    }


def _qhh_latest_availability_schema() -> dict:
    return {
        "type": "object",
        "required": ["ready", "unavailable_reasons", "quality_flags", "quality_notes"],
        "properties": {
            "ready": {"type": "boolean"},
            "unavailable_reasons": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/QhhLatestUnavailableReason"},
            },
            "quality_flags": {"type": "array", "items": {"type": "string"}},
            "quality_notes": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/QhhLatestQualityNote"},
            },
        },
    }


def _qhh_latest_quality_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "station_sample_count",
            "river_sample_count",
            "required_station_variables",
            "station_variable_coverage",
            "candidate_limit",
            "search_limit",
            "context_limit",
            "query_indexes",
        ],
        "properties": {
            "station_sample_count": {"type": "integer", "minimum": 0},
            "river_sample_count": {"type": "integer", "minimum": 0},
            "required_station_variables": {
                "type": "array",
                "items": {"type": "string", "enum": ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"]},
            },
            "station_variable_coverage": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/QhhLatestStationVariableCoverage"},
            },
            "candidate_limit": {"type": "integer", "minimum": 1},
            "search_limit": {"type": "integer", "minimum": 1},
            "context_limit": {"type": "integer", "minimum": 1},
            "query_indexes": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/QhhLatestQueryIndex"},
            },
        },
    }


def _qhh_latest_product_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "basin_id",
            "model_id",
            "basin_version_id",
            "river_network_version_id",
            "source_id",
            "cycle_time",
            "run_id",
            "forcing_version_id",
            "station_count",
            "expected_station_count",
            "segment_count",
            "expected_segment_count",
            "status",
            "run_status",
            "valid_time_start",
            "valid_time_end",
            "river_valid_time_start",
            "river_valid_time_end",
            "forcing_valid_time_start",
            "forcing_valid_time_end",
            "available_horizon_hours",
            "expected_horizon_hours",
            "shorter_horizon",
            "availability",
            "quality",
        ],
        "properties": {
            "basin_id": {"type": "string"},
            "model_id": {"type": "string"},
            "basin_version_id": {"type": "string"},
            "river_network_version_id": {"type": "string"},
            "source_id": {"type": "string", "enum": ["GFS", "IFS"]},
            "cycle_time": {"type": "string", "format": "date-time"},
            "run_id": {"type": "string"},
            "forcing_version_id": {"type": "string"},
            "station_count": {"type": "integer", "minimum": 0},
            "expected_station_count": {"type": "integer", "minimum": 0, "nullable": True},
            "segment_count": {"type": "integer", "minimum": 0},
            "expected_segment_count": {"type": "integer", "minimum": 0, "nullable": True},
            "status": {"type": "string", "enum": ["ready", "unavailable"]},
            "run_status": {"type": "string"},
            "valid_time_start": {"type": "string", "format": "date-time", "nullable": True},
            "valid_time_end": {"type": "string", "format": "date-time", "nullable": True},
            "river_valid_time_start": {"type": "string", "format": "date-time", "nullable": True},
            "river_valid_time_end": {"type": "string", "format": "date-time", "nullable": True},
            "forcing_valid_time_start": {"type": "string", "format": "date-time", "nullable": True},
            "forcing_valid_time_end": {"type": "string", "format": "date-time", "nullable": True},
            "available_horizon_hours": {"type": "integer", "nullable": True},
            "expected_horizon_hours": {"type": "integer"},
            "shorter_horizon": {"type": "boolean"},
            "availability": {"$ref": "#/components/schemas/QhhLatestAvailability"},
            "quality": {"$ref": "#/components/schemas/QhhLatestQuality"},
        },
    }


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
                "type": "object",
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/LayerMetadata"}],
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
            "source_refs": _nullable(
                {
                    "type": "object",
                    "description": (
                        "Concrete source identity used to resolve non-XYZ route placeholders, including "
                        "run_id, basin_version_id, river_network_version_id, and bounded source_version/run "
                        "revision when advertised by required_placeholders."
                    ),
                    "additionalProperties": True,
                }
            ),
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


def _register_static_and_health_routes(api: FastAPI) -> None:
    @api.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "nhms-api",
            "version": "0.1.0",
        }

    api.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST_DIR / "assets", check_dir=False),
        name="frontend-assets",
    )

    @api.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(FRONTEND_INDEX)


def _ok(request: Request, data: Any) -> dict[str, Any]:
    return {
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
        "status": "ok",
        "data": data,
    }


app = create_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("apps.api.main:app", host="0.0.0.0", port=port, reload=True)
