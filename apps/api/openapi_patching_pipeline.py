"""The pipeline patch family: `_patch_pipeline_openapi` and its two helpers.

Split out of `apps/api/openapi_patching.py` (#2074). The facade re-exports
`_patch_pipeline_openapi` with a plain `from ... import`, because
`tests/test_openapi_drift.py` asserts `is` identity between
`apps.api.main._patch_pipeline_openapi` and
`apps.api.openapi_patching._patch_pipeline_openapi`.
"""

from typing import Any

from apps.api.openapi_patching_envelopes import (
    _error_response_schema,
    _ops_strict_identity_schema,
    _ops_success_response_schema,
    _set_operation_response_schema,
    _success_envelope_schema,
    _success_response_schema,
    _typed_error_response,
    _validation_error_detail_schema,
)
from apps.api.openapi_patching_ops_schemas import (
    _basin_progress_schema,
    _basin_result_schema,
    _job_logs_schema,
    _job_status_counts_schema,
    _pipeline_job_page_schema,
    _pipeline_job_schema,
    _pipeline_stage_schema,
    _pipeline_status_schema,
    _retry_run_result_schema,
)
from apps.api.openapi_patching_parameters import (
    _cycle_time_query_parameter,
    _run_id_query_parameter,
    _source_query_parameter,
    _strict_model_id_query_parameter,
)
from apps.api.openapi_restored_schemas import (
    _queue_depth_schema,
    _stage_duration_metric_schema,
    _success_rate_metric_schema,
)


def _patch_pipeline_openapi(schema: dict) -> None:
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["SuccessEnvelope"] = _success_envelope_schema()
    schemas["OpsStrictIdentity"] = _ops_strict_identity_schema()
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
    schemas["QueueDepth"] = _queue_depth_schema()
    schemas["StageDurationMetric"] = _stage_duration_metric_schema()
    schemas["SuccessRateMetric"] = _success_rate_metric_schema()

    responses = components.setdefault("responses", {})
    responses["Error"] = {
        "description": "Error response",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    }
    responses["ControlPlaneManualActionRequired"] = _typed_error_response(
        "Control-plane mutation is unavailable from a display_readonly API node; "
        "the authorized actor must perform the action from the compute_control runbook on node 22.",
        ["CONTROL_PLANE_MANUAL_ACTION_REQUIRED"],
    )
    responses["ControlPlaneQueueUnavailable"] = _typed_error_response(
        "Queue depth is unavailable from a display_readonly API node; "
        "the compute_control node exposes the live queue state.",
        ["CONTROL_PLANE_QUEUE_UNAVAILABLE"],
    )
    # #1678: `GET /api/v1/queue/depth` re-raises whatever the Slurm gateway
    # raised (`apps/api/routes/pipeline.py:838-845`). `RealSlurmGateway.list_jobs`
    # reaches `_run_command` (`SlurmCommandError` 502 / `SlurmTimeoutError` 504)
    # and `_parse_sacct_list` (`SlurmParseError` 502), so those two upstream
    # statuses are reachable and are declared here. No 4XX is reachable on that
    # operation, so none is declared.
    responses["SlurmGatewayUpstreamError"] = _typed_error_response(
        "The Slurm gateway command failed or returned output the gateway could not parse.",
        ["SLURM_COMMAND_ERROR", "SLURM_PARSE_ERROR"],
    )
    responses["SlurmGatewayTimeout"] = _typed_error_response(
        "The Slurm gateway command exceeded its timeout before returning.",
        ["SLURM_TIMEOUT"],
    )
    responses["JobLogError"] = _typed_error_response(
        "Pipeline job log retrieval failed using the canonical error envelope.",
        [
            "JOB_LOG_NOT_PUBLISHED",
            "JOB_LOG_URI_UNSUPPORTED",
            "JOB_LOG_ACCESS_DENIED",
            "JOB_LOG_NOT_FOUND",
        ],
    )

    _patch_pipeline_operation(
        schema,
        "/api/v1/pipeline/status",
        "get",
        operation_id="getPipelineStatus",
        summary="Get pipeline status for a cycle",
        tags=["pipeline"],
        parameters=[
            _source_query_parameter(required=True),
            _cycle_time_query_parameter(required=True),
            _run_id_query_parameter(required=False),
            _strict_model_id_query_parameter(required=False),
        ],
        data_schema={"$ref": "#/components/schemas/PipelineStatus"},
        description="Pipeline status",
        strict_identity=True,
    )
    _patch_pipeline_operation(
        schema,
        "/api/v1/pipeline/stages",
        "get",
        operation_id="listPipelineStages",
        summary="List pipeline stages for a cycle",
        tags=["pipeline"],
        parameters=[
            _source_query_parameter(required=True),
            _cycle_time_query_parameter(required=True),
            _run_id_query_parameter(required=False),
            _strict_model_id_query_parameter(required=False),
        ],
        data_schema={"type": "array", "items": {"$ref": "#/components/schemas/PipelineStage"}},
        description="Pipeline stage list",
        strict_identity=True,
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
            _run_id_query_parameter(required=False),
            {"name": "status", "in": "query", "required": False, "schema": {"type": "string"}},
            _strict_model_id_query_parameter(required=False),
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
        strict_identity=True,
    )
    _patch_pipeline_operation(
        schema,
        "/api/v1/jobs/{job_id}/logs",
        "get",
        operation_id="getPipelineJobLogs",
        summary="Get pipeline job logs",
        tags=["pipeline"],
        parameters=[
            {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}},
            _source_query_parameter(required=False),
            _cycle_time_query_parameter(required=False),
            _run_id_query_parameter(required=False),
            _strict_model_id_query_parameter(required=False),
        ],
        data_schema={"$ref": "#/components/schemas/JobLogs"},
        description="Pipeline job logs",
        strict_identity=True,
        job_identity=True,
        extra_responses={
            "400": {"$ref": "#/components/responses/JobLogError"},
            "403": {"$ref": "#/components/responses/JobLogError"},
            "404": {"$ref": "#/components/responses/JobLogError"},
        },
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
        extra_responses={"409": {"$ref": "#/components/responses/ControlPlaneManualActionRequired"}},
    )
    # The metrics routes are plain `_ok()` handlers (routes/pipeline.py:750/795),
    # not `_patch_pipeline_operation` operations, so only their 200 body is
    # rewritten; their FastAPI-generated parameters and error responses stand.
    _set_operation_response_schema(
        schema,
        "/api/v1/metrics/stage-duration",
        _success_response_schema(
            {"type": "array", "items": {"$ref": "#/components/schemas/StageDurationMetric"}}
        ),
    )
    _set_operation_response_schema(
        schema,
        "/api/v1/metrics/success-rate",
        _success_response_schema(
            {"type": "array", "items": {"$ref": "#/components/schemas/SuccessRateMetric"}}
        ),
    )
    # Cancel keeps FastAPI's generated success schema; only the display-mode
    # error responses are injected so the runtime spec matches the static
    # contract for those control-plane status codes. queue/depth additionally
    # gets its named 200 body, written before `_inject_operation_responses` so
    # the error injection (which only adds keys) cannot clobber it.
    _inject_operation_responses(
        schema,
        "/api/v1/runs/{run_id}/cancel",
        "post",
        {"409": {"$ref": "#/components/responses/ControlPlaneManualActionRequired"}},
    )
    _set_operation_response_schema(
        schema,
        "/api/v1/queue/depth",
        _success_response_schema({"$ref": "#/components/schemas/QueueDepth"}),
    )
    _inject_operation_responses(
        schema,
        "/api/v1/queue/depth",
        "get",
        {
            "502": {"$ref": "#/components/responses/SlurmGatewayUpstreamError"},
            "503": {"$ref": "#/components/responses/ControlPlaneQueueUnavailable"},
            "504": {"$ref": "#/components/responses/SlurmGatewayTimeout"},
        },
    )


def _inject_operation_responses(
    schema: dict,
    path: str,
    method: str,
    responses: dict[str, dict[str, Any]],
) -> None:
    operation = schema.get("paths", {}).get(path, {}).get(method)
    if not operation:
        return
    operation.setdefault("responses", {}).update(responses)


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
    strict_identity: bool = False,
    job_identity: bool = False,
    extra_responses: dict[str, dict[str, Any]] | None = None,
) -> None:
    operation = schema.get("paths", {}).get(path, {}).get(method)
    if not operation:
        return
    operation["operationId"] = operation_id
    operation["summary"] = summary
    operation["tags"] = tags
    operation["parameters"] = parameters
    response_schema = (
        _ops_success_response_schema(data_schema, job_identity=job_identity)
        if strict_identity
        else _success_response_schema(data_schema)
    )
    operation["responses"] = {
        "200": {
            "description": description,
            "content": {"application/json": {"schema": response_schema}},
        },
        **(extra_responses or {}),
        "4XX": {"$ref": "#/components/responses/Error"},
        "5XX": {"$ref": "#/components/responses/Error"},
    }
