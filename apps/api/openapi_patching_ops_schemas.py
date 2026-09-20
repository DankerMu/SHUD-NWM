"""Pipeline / job / runtime component schemas for the runtime OpenAPI patches.

Split out of `apps/api/openapi_patching.py` (#2074).
"""

from apps.api.routes.pipeline import (
    PIPELINE_JOB_STATUS_VALUES,
    PIPELINE_PUBLIC_LOG_URI_MAX_LENGTH,
    PIPELINE_STAGE_BASIN_RESULTS_LIMIT,
)


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
