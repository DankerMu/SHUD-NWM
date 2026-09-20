"""Shared response-envelope and error vocabulary for the runtime OpenAPI patches.

Split out of `apps/api/openapi_patching.py` (#2074). These builders are the
cross-cutting success-envelope / typed-error shapes that every patch family
composes; they own no capability of their own and import nothing from the
repository.
"""

from typing import Any


def _typed_error_response(description: str, codes: list[str]) -> dict[str, Any]:
    return {
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


def _ops_success_response_schema(data_schema: dict, *, job_identity: bool) -> dict:
    identity_schema: dict[str, Any] = {
        "allOf": [
            {"$ref": "#/components/schemas/OpsStrictIdentity"},
        ],
        "description": (
            "Present on strict Ops successes with the resolved source, cycle_time, "
            "run_id, and model_id. Omitted for non-strict browsing. When present, "
            "those four fields are required; job_id is additionally required on logs."
        ),
    }

    if job_identity:
        identity_schema = {
            "allOf": [
                {"$ref": "#/components/schemas/OpsStrictIdentity"},
                {
                    "type": "object",
                    "required": ["job_id"],
                    "properties": {"job_id": {"type": "string"}},
                },
            ],
            "description": (
                "Present on strict Ops log successes with the resolved source, "
                "cycle_time, run_id, model_id, and job_id. Omitted for non-strict browsing."
            ),
        }
    return {
        "allOf": [
            {"$ref": "#/components/schemas/SuccessEnvelope"},
            {
                "type": "object",
                "required": ["data"],
                "properties": {
                    "data": data_schema,
                    "identity": identity_schema,
                },
            },
        ]
    }




def _station_series_error_response(description: str, examples: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "examples": examples,
            }
        },
    }


def _error_example(code: str, message: str, *, details: Any | None = None) -> dict[str, Any]:
    return {
        "summary": code,
        "value": {
            "request_id": "req_01J0NHMS",
            "status": "error",
            "error": {
                "code": code,
                "message": message,
                "details": details,
            },
        },
    }


def _set_operation_response_schema(
    schema: dict,
    path: str,
    response_schema: dict,
    *,
    method: str = "get",
) -> None:
    """Rewrite an operation's 200 ``application/json`` schema in place.

    ``method`` is explicit because the POST lifecycle operations need the same
    rewrite; defaulting it to ``get`` would silently no-op on them and leave the
    injected component orphaned (a named schema in ``components`` that no
    operation references, which type-checks green while the contract still
    advertises ``additionalProperties: true``).
    """
    operation = schema.get("paths", {}).get(path, {}).get(method)
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


def _ops_strict_identity_schema() -> dict:
    return {
        "type": "object",
        "description": (
            "Resolved strict Ops identity returned as a top-level sibling of data "
            "on successful status, stages, jobs, and job-log responses. Present only "
            "when the request used a complete strict identity; required fields apply "
            "whenever this object is present."
        ),
        "required": ["source", "cycle_time", "run_id", "model_id"],
        "properties": {
            "source": {"type": "string"},
            "cycle_time": {"type": "string", "format": "date-time"},
            "run_id": {"type": "string"},
            "model_id": {"type": "string"},
            "job_id": {"type": "string"},
        },
        "additionalProperties": False,
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
