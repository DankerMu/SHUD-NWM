"""Runtime OpenAPI patch orchestration: the factory, the call order, the finalizer.

Implementation families were split out (#2074) to `openapi_patching_nullable.py`,
`openapi_patching_security.py`, `openapi_patching_envelopes.py`,
`openapi_patching_parameters.py`, `openapi_patching_ops_schemas.py`,
`openapi_patching_display_schemas.py`, `openapi_patching_pipeline.py` and
`openapi_patching_response_models.py` (#2348). This
module stays the compatibility facade and the single owner of the ORDER in which
patches are applied and of `_finalize_openapi_schema`'s timing.

Re-exports here are plain `from ... import <name>` on purpose. `apps/api/main.py`
binds 14 of these names at import time and `tests/test_openapi_drift.py` asserts
`is` identity for `_patch_mvt_tile_openapi` and `_patch_pipeline_openapi`, so a
wrapper, a lambda or an alias chain would break the identity contract silently.

An owner module never imports this facade, at module level or function-locally.
"""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from apps.api.openapi_patching_display_schemas import (
    _discharge_cycles_schema,
    _layer_metadata_schema,
    _layer_schema,
    _layer_valid_times_schema,
    _qhh_latest_availability_schema,
    _qhh_latest_product_schema,
    _qhh_latest_quality_note_schema,
    _qhh_latest_quality_schema,
    _qhh_latest_query_index_schema,
    _qhh_latest_station_variable_coverage_schema,
    _qhh_latest_unavailable_reason_schema,
    _station_series_metadata_schema,
    _station_series_point_schema,
    _station_series_response_schema,
    _station_series_schema,
    _station_series_station_schema,
)
from apps.api.openapi_patching_envelopes import (
    _error_example,
    _error_response_schema,
    _set_operation_response_schema,
    _station_series_error_response,
    _success_envelope_schema,
    _success_response_schema,
    _validation_error_detail_schema,
)
from apps.api.openapi_patching_nullable import _remove_nullable_keywords
from apps.api.openapi_patching_ops_schemas import _runtime_config_schema
from apps.api.openapi_patching_parameters import _station_series_parameters
from apps.api.openapi_patching_pipeline import _patch_pipeline_openapi
from apps.api.openapi_patching_response_models import (
    _patch_response_model_envelopes,
    _prune_unreferenced_response_model_components,
)
from apps.api.openapi_patching_security import _publish_security_boundary
from apps.api.openapi_restored_schemas import (
    _basin_schema,
    _basin_version_schema,
    _forecast_series_response_schema,
    _geojson_line_string_schema,
    _geojson_multi_line_string_schema,
    _geojson_multi_polygon_schema,
    _hydro_run_page_schema,
    _hydro_run_schema,
    _met_station_page_schema,
    _met_station_schema,
    _model_instance_page_schema,
    _model_instance_schema,
    _model_lifecycle_result_schema,
    _model_operation_preflight_schema,
    _river_segment_feature_collection_schema,
    _river_segment_feature_schema,
    _river_segment_schema,
    _river_series_response_schema,
    _run_status_schema,
    _run_type_schema,
    _series_segment_schema,
    _spliced_forecast_response_schema,
)
from apps.api.routes.hydro_display import TILE_X_DESCRIPTION, TILE_Y_DESCRIPTION
from services.tiles.mvt import (
    MVT_MAX_TILE_COORDINATE,
    MVT_MAX_ZOOM,
    SUPPORTED_HYDRO_MVT_VARIABLES,
)

OpenApiPatchSchema = Callable[[dict], None]


def custom_openapi_factory(api: FastAPI, *, patch_schema: OpenApiPatchSchema | None = None) -> Any:
    def custom_openapi() -> dict[str, Any]:
        if api.openapi_schema:
            return api.openapi_schema
        schema = get_openapi(
            title=api.title,
            version=api.version,
            description=api.description,
            license_info=api.license_info,
            routes=api.routes,
        )
        if patch_schema is None:
            patch_openapi_schema(schema)
        else:
            patch_schema(schema)
        api.openapi_schema = schema
        return api.openapi_schema

    return custom_openapi


def patch_openapi_schema(schema: dict) -> None:
    _patch_response_model_envelopes(schema)
    _patch_mvt_tile_openapi(schema)
    _patch_station_series_openapi(schema)
    _patch_qhh_latest_product_openapi(schema)
    _patch_met_stations_list_openapi(schema)
    _patch_layer_metadata_openapi(schema)
    _patch_pipeline_openapi(schema)
    _patch_runtime_openapi(schema)
    _prune_unreferenced_response_model_components(schema)
    _finalize_openapi_schema(schema)


def _finalize_openapi_schema(schema: dict) -> None:
    """Normalize hand-patched nullable schemas and publish truthful security metadata.

    FastAPI emits OpenAPI 3.1.0, where JSON Schema nullability is a type union
    rather than the 3.0-only ``nullable`` keyword. All 111 ``nullable: true``
    nodes are injected by this module, so the single post-patch finalizer below
    is the only place that re-expresses them. It recurses the whole document,
    replaces scalar ``type`` with ``[type, "null"]`` for the 110 ordinary nodes,
    and re-expresses the one ``type + allOf`` composition (Layer.metadata) as a
    complete composition-or-null union so openapi-typescript keeps generating
    ``LayerMetadata | null`` instead of an erroneous intersection. Any other
    nullable shape fails loudly rather than being silently weakened.

    The same owner publishes the same-origin ``servers`` entry and the security
    boundary: public operations inherit explicit root anonymous, and exactly the
    operations enforced today override root with the conditional credential
    alternatives the server actually accepts. No credential value is embedded.
    """
    normalized = _remove_nullable_keywords(schema)
    schema.clear()
    schema.update(normalized)
    _publish_security_boundary(schema)


_NATIONAL_SOURCE_CYCLE_TILE_PATH = (
    "/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf"
)


def _patch_mvt_tile_openapi(schema: dict) -> None:
    mvt_paths = (
        "/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf",
        _NATIONAL_SOURCE_CYCLE_TILE_PATH,
        "/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
    )
    _ensure_mvt_live_postgis_unavailable_response(schema)
    _ensure_mvt_national_identity_unavailable_response(schema)
    _ensure_mvt_cold_generation_busy_response(schema)
    for path in mvt_paths:
        operation = schema.get("paths", {}).get(path, {}).get("get", {})
        # #2153: only the canonical source/cycle route can emit
        # MVT_NATIONAL_IDENTITY_INCOMPLETE, so only it documents the wider enum.
        response_name = (
            "MvtNationalIdentityUnavailable"
            if path == _NATIONAL_SOURCE_CYCLE_TILE_PATH
            else "MvtLivePostgisUnavailable"
        )
        operation.setdefault("responses", {})["424"] = {"$ref": f"#/components/responses/{response_name}"}
        operation["responses"]["503"] = {"$ref": "#/components/responses/MvtColdGenerationBusy"}
        operation["responses"]["4XX"] = {"$ref": "#/components/responses/Error"}
        operation["responses"]["5XX"] = {"$ref": "#/components/responses/Error"}
        for parameter in operation.get("parameters", []):
            name = parameter.get("name")
            if "/tiles/hydro" in path and name == "variable":
                parameter["schema"] = {"type": "string", "enum": list(SUPPORTED_HYDRO_MVT_VARIABLES)}
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
    responses["MvtLivePostgisUnavailable"] = _typed_mvt_error_response(
        "Live PostGIS MVT is unavailable for this canonical tile route.",
        ["MVT_LIVE_POSTGIS_UNAVAILABLE"],
    )


def _ensure_mvt_national_identity_unavailable_response(schema: dict) -> None:
    responses = schema.setdefault("components", {}).setdefault("responses", {})
    responses["MvtNationalIdentityUnavailable"] = _typed_mvt_error_response(
        "Live PostGIS MVT is unavailable, or the requested national identity is not "
        "covered by every active river network.",
        ["MVT_LIVE_POSTGIS_UNAVAILABLE", "MVT_NATIONAL_IDENTITY_INCOMPLETE"],
    )


def _ensure_mvt_cold_generation_busy_response(schema: dict) -> None:
    responses = schema.setdefault("components", {}).setdefault("responses", {})
    responses["MvtColdGenerationBusy"] = _typed_mvt_error_response(
        "Cold MVT generation is saturated; retry after the stated delay.",
        ["MVT_COLD_GENERATION_BUSY"],
        headers={
            "Retry-After": {"schema": {"type": "string"}},
            "Cache-Control": {"schema": {"type": "string"}},
            "X-Request-ID": {"schema": {"type": "string"}},
        },
    )


def _typed_mvt_error_response(
    description: str, codes: list[str], *, headers: dict[str, Any] | None = None
) -> dict:
    response = {
        "description": description,
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
                                "code": {"type": "string", "enum": codes},
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
    if headers:
        response["headers"] = headers
    return response


def _patch_met_stations_list_openapi(schema: dict) -> None:
    """Align the list-stations ``variables`` query schema with the static contract.

    The route declares ``variables: list[str] | None`` so FastAPI binds repeated
    params correctly, but it emits ``anyOf: [array, null]``. The published
    contract advertises ``oneOf: [string, array]`` (repeat or comma-separate), so
    we restore that documented form without touching the static spec / types.ts.

    The named ``MetStation``/``MetStationPage`` response schemas are injected here
    rather than in a separate patch because this operation is already owned by
    this function; a later patch would clobber whichever rewrite ran first.
    """
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SuccessEnvelope"] = _success_envelope_schema()
    schemas["MetStation"] = _met_station_schema()
    schemas["MetStationPage"] = _met_station_page_schema()

    operation = schema.get("paths", {}).get("/api/v1/met/stations", {}).get("get")
    if not operation:
        return
    _set_operation_response_schema(
        schema,
        "/api/v1/met/stations",
        _success_response_schema({"$ref": "#/components/schemas/MetStationPage"}),
    )
    for parameter in operation.get("parameters", []):
        if parameter.get("name") != "variables":
            continue
        parameter["style"] = "form"
        parameter["explode"] = True
        parameter["schema"] = {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        }


def _patch_layer_metadata_openapi(schema: dict) -> None:
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components.pop("Layer", None)
    components.pop("LayerMetadata", None)
    components.pop("LayerValidTimes", None)
    components.pop("DischargeCycle", None)
    components.pop("DischargeCycles", None)
    layer_list_response = components.pop("LayerListResponse", None)
    layer_valid_times_response = components.pop("LayerValidTimesResponse", None)
    discharge_cycles_response = components.pop("DischargeCyclesResponse", None)
    api_success_envelope = components.pop("ApiSuccessEnvelope", None)

    if api_success_envelope is not None:
        components.setdefault("SuccessEnvelope", api_success_envelope)
    components["Layer"] = _layer_schema()
    components["LayerMetadata"] = _layer_metadata_schema()
    components["LayerValidTimes"] = _layer_valid_times_schema()
    components["DischargeCycles"] = _discharge_cycles_schema()

    if layer_list_response is not None:
        layer_list_response = _success_response_schema(
            {"type": "array", "items": {"$ref": "#/components/schemas/Layer"}}
        )
        _set_operation_response_schema(schema, "/api/v1/layers", layer_list_response)

    if layer_valid_times_response is not None:
        layer_valid_times_response = _success_response_schema({"$ref": "#/components/schemas/LayerValidTimes"})
        _set_operation_response_schema(schema, "/api/v1/layers/{layer_id}/valid-times", layer_valid_times_response)

    if discharge_cycles_response is not None:
        discharge_cycles_response = _success_response_schema({"$ref": "#/components/schemas/DischargeCycles"})
        _set_operation_response_schema(schema, "/api/v1/layers/discharge/cycles", discharge_cycles_response)


def _patch_basin_registry_openapi(schema: dict) -> None:
    """Restore the published ``Basin`` schema and the basin-list response body.

    ``GET /api/v1/basins`` returns the ``_ok()`` envelope around a list of
    registry rows. The named component and the envelope-wrapped 200 body are
    hand-written here, exactly as the layer and station-series contracts are;
    the route's runtime ``BasinListEnvelope`` model (#2348) is bound to them by
    ``tests/test_response_model_schema_parity.py``.

    ``SuccessEnvelope`` is (re)assigned rather than assumed so this patch is
    order-independent with respect to ``_patch_layer_metadata_openapi``, which
    renames the route-generated ``ApiSuccessEnvelope`` component.
    """
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SuccessEnvelope"] = _success_envelope_schema()
    schemas["Basin"] = _basin_schema()
    schemas["GeoJsonMultiPolygon"] = _geojson_multi_polygon_schema()
    schemas["BasinVersion"] = _basin_version_schema()

    _set_operation_response_schema(
        schema,
        "/api/v1/basins",
        _success_response_schema({"type": "array", "items": {"$ref": "#/components/schemas/Basin"}}),
    )
    _set_operation_response_schema(
        schema,
        "/api/v1/basins/{basin_id}/versions",
        _success_response_schema({"type": "array", "items": {"$ref": "#/components/schemas/BasinVersion"}}),
    )


def _patch_river_segment_openapi(schema: dict) -> None:
    """Restore the river-segment GeoJSON collection and detail response bodies.

    Both handlers return ``_ok()`` around the store payload; their runtime
    models (#2348) are bound to these hand schemas by the parity test.
    """
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SuccessEnvelope"] = _success_envelope_schema()
    schemas["GeoJsonLineString"] = _geojson_line_string_schema()
    schemas["GeoJsonMultiLineString"] = _geojson_multi_line_string_schema()
    schemas["RiverSegment"] = _river_segment_schema()
    schemas["RiverSegmentFeature"] = _river_segment_feature_schema()
    schemas["RiverSegmentFeatureCollection"] = _river_segment_feature_collection_schema()

    _set_operation_response_schema(
        schema,
        "/api/v1/basin-versions/{basin_version_id}/river-segments",
        _success_response_schema({"$ref": "#/components/schemas/RiverSegmentFeatureCollection"}),
    )
    _set_operation_response_schema(
        schema,
        "/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}",
        _success_response_schema({"$ref": "#/components/schemas/RiverSegment"}),
    )


def _patch_model_instance_openapi(schema: dict) -> None:
    """Restore the model registry list/detail and lifecycle response bodies.

    The two lifecycle operations are POSTs, so they pass ``method="post"``; the
    default would no-op and orphan ``ModelOperationPreflight`` /
    ``ModelLifecycleResult``.
    """
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SuccessEnvelope"] = _success_envelope_schema()
    schemas["ModelInstance"] = _model_instance_schema()
    schemas["ModelInstancePage"] = _model_instance_page_schema()
    schemas["ModelOperationPreflight"] = _model_operation_preflight_schema()
    schemas["ModelLifecycleResult"] = _model_lifecycle_result_schema()

    _set_operation_response_schema(
        schema,
        "/api/v1/models",
        _success_response_schema({"$ref": "#/components/schemas/ModelInstancePage"}),
    )
    _set_operation_response_schema(
        schema,
        "/api/v1/models/{model_id}",
        _success_response_schema({"$ref": "#/components/schemas/ModelInstance"}),
    )
    _set_operation_response_schema(
        schema,
        "/api/v1/models/{model_id}/preflight",
        _success_response_schema({"$ref": "#/components/schemas/ModelOperationPreflight"}),
        method="post",
    )
    _set_operation_response_schema(
        schema,
        "/api/v1/models/{model_id}/lifecycle",
        _success_response_schema({"$ref": "#/components/schemas/ModelLifecycleResult"}),
        method="post",
    )


def _patch_hydro_run_openapi(schema: dict) -> None:
    """Publish the hand ``HydroRun`` / ``HydroRunPage`` contract (#2222).

    The run-list body is set here; the run-detail body
    (``allOf[SuccessEnvelope, {data: $ref HydroRun}]``) comes from the
    ``HydroRunEnvelope`` response model via ``_patch_response_model_envelopes``.
    """
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SuccessEnvelope"] = _success_envelope_schema()
    schemas["RunType"] = _run_type_schema()
    schemas["RunStatus"] = _run_status_schema()
    schemas["HydroRun"] = _hydro_run_schema()
    schemas["HydroRunPage"] = _hydro_run_page_schema()

    _set_operation_response_schema(
        schema,
        "/api/v1/runs",
        _success_response_schema({"$ref": "#/components/schemas/HydroRunPage"}),
    )


def _patch_forecast_series_openapi(schema: dict) -> None:
    """Restore the river forecast-series response body.

    This is the one route in this family whose 200 is **not** wrapped in
    ``SuccessEnvelope``: ``get_forecast_series`` returns ``store.forecast_series``
    directly (``apps/api/routes/forecast.py:68``) rather than through ``_ok``, so
    the body is the bare ``oneOf`` of the two payload shapes.
    """
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SeriesSegment"] = _series_segment_schema()
    schemas["RiverSeriesResponse"] = _river_series_response_schema()
    schemas["SplicedForecastResponse"] = _spliced_forecast_response_schema()

    _set_operation_response_schema(
        schema,
        "/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series",
        _forecast_series_response_schema(),
    )


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
        "4XX": _station_series_error_response(
            "Station series client error",
            {
                "stationNotFound": _error_example("STATION_NOT_FOUND", "Station not found."),
                "missingRequiredFilter": _error_example(
                    "MISSING_REQUIRED_FILTER",
                    "forcing_version_id or model_id, source_id, and cycle_time are required "
                    "for station series queries.",
                    details={
                        "required_alternatives": [
                            ["forcing_version_id"],
                            ["model_id", "source_id", "cycle_time"],
                        ]
                    },
                ),
                "stationForcingFileNotFound": _error_example(
                    "STATION_FORCING_FILE_NOT_FOUND",
                    "Station forcing file not found.",
                ),
            },
        ),
        "5XX": _station_series_error_response(
            "Station series server error",
            {
                "stationForcingFilenameMissing": _error_example(
                    "STATION_FORCING_FILENAME_MISSING",
                    "Station forcing filename is missing.",
                ),
                "stationForcingFileMalformed": _error_example(
                    "STATION_FORCING_FILE_MALFORMED",
                    "Station forcing file is malformed.",
                ),
            },
        ),
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
            "name": "basin_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": (
                "Target basin id for the latest display product. Defaults to basins_qhh when omitted, "
                "preserving backward compatibility for /api/v1/mvp/qhh/latest-product and M22 cross-plane callers."
            ),
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
        {
            "name": "identity_only",
            "in": "query",
            "required": False,
            "schema": {"type": "boolean", "default": False},
            "description": (
                "Lightweight resolver for popups: returns run identity + cycle + horizon "
                "(+ recent cycles as available_issue_times) WITHOUT the expensive station/segment "
                "coverage computation. cycle_time may be supplied alone to select a specific cycle."
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


def _patch_precip_openapi(schema: dict) -> None:
    """#2010: the precip index answers the shared ``_ok`` envelope.

    FastAPI emits a standalone ``PrecipIndexResponse`` for the declared response
    model; every other envelope-returning display route publishes
    ``allOf: [SuccessEnvelope, {data}]`` instead, and the hand-maintained
    ``openapi/nhms.v1.yaml`` mirrors that one shape. ``PrecipIndex`` and
    ``PrecipLegendEntry`` stay as generated -- ``LayerMetadata.legend`` refs the
    latter.
    """
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    if components.pop("PrecipIndexResponse", None) is None:
        return
    _set_operation_response_schema(
        schema,
        "/api/v1/precip/{source}/{cycle}/index",
        _success_response_schema({"$ref": "#/components/schemas/PrecipIndex"}),
    )
