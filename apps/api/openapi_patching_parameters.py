"""Query and path parameter builders for the runtime OpenAPI patches.

Split out of `apps/api/openapi_patching.py` (#2074).
"""

from typing import Any

from apps.api.routes.pipeline import PIPELINE_STRICT_IDENTITY_TEXT_MAX_LENGTH
from packages.common.forecast_store import MAX_STATION_SERIES_LIMIT


def _source_query_parameter(*, required: bool) -> dict[str, Any]:
    return {"name": "source", "in": "query", "required": required, "schema": {"type": "string"}}


def _cycle_time_query_parameter(*, required: bool) -> dict[str, Any]:
    return {
        "name": "cycle_time",
        "in": "query",
        "required": required,
        "schema": {"type": "string", "format": "date-time"},
    }


def _run_id_query_parameter(*, required: bool) -> dict[str, Any]:
    return {
        "name": "run_id",
        "in": "query",
        "required": required,
        "schema": {"type": "string", "maxLength": PIPELINE_STRICT_IDENTITY_TEXT_MAX_LENGTH},
    }


def _model_id_query_parameter(*, required: bool) -> dict[str, Any]:
    return {"name": "model_id", "in": "query", "required": required, "schema": {"type": "string"}}


def _strict_model_id_query_parameter(*, required: bool) -> dict[str, Any]:
    return {
        "name": "model_id",
        "in": "query",
        "required": required,
        "schema": {"type": "string", "maxLength": PIPELINE_STRICT_IDENTITY_TEXT_MAX_LENGTH},
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
            "deprecated": True,
            "description": (
                "Deprecated compatibility parameter. The disk-only route ignores this value when "
                "model_id, source_id, and cycle_time are supplied; by itself it no longer selects DB-backed series."
            ),
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
                "Public station-series variables are PRCP, TEMP, RH, wind, and Rn."
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
