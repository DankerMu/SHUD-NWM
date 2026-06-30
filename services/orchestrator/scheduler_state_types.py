from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

CANDIDATE_STATE_TASK_RESULT_LIMIT = 16
DEFAULT_RETRY_LIMIT = 3
DEFAULT_CANDIDATE_STATE_JOB_LIMIT = 100
DEFAULT_CANDIDATE_STATE_EVENT_LIMIT = 100
STATE_M23_COMPARISON_FIELDS = (
    "basin_id",
    "basin_version_id",
    "river_network_version_id",
    "canonical_product_id",
    "forcing_version_id",
    "hydro_run_id",
    "published_manifest_id",
)
STATE_CANDIDATE_SCOPED_PROOF_FIELDS = (
    "run_id",
    "forcing_version_id",
    "hydro_run_id",
    "published_manifest_id",
)
STATE_STRONG_CANDIDATE_SCOPED_PROOF_FIELDS = STATE_CANDIDATE_SCOPED_PROOF_FIELDS
ACTIVE_PIPELINE_STATUSES = {"pending", "queued", "submitted", "running"}
ACTIVE_HYDRO_STATUSES = {"created", "staged", "pending", "submitted", "running"}
DURABLE_HYDRO_SUCCESS_STATUSES = {"succeeded", "parsed", "frequency_done", "published", "complete"}
TERMINAL_PIPELINE_SUCCESS_STATUSES = {"succeeded", "complete", "published"}
TERMINAL_PIPELINE_COMPLETION_STAGES = {"parse", "state_save_qc", "publish"}
FAILED_PIPELINE_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed"}
DOWNSTREAM_RESTART_STAGES = ("convert", "forcing", "forecast", "parse", "state_save_qc")
DOWNSTREAM_STAGE_ALIASES = {
    "convert": "convert",
    "convert_canonical": "convert",
    "canonical": "convert",
    "forcing": "forcing",
    "produce_forcing": "forcing",
    "produce_forcing_array": "forcing",
    "forecast": "forecast",
    "run_shud_forecast": "forecast",
    "run_shud_forecast_array": "forecast",
    "parse": "parse",
    "parse_output": "parse",
    "parse_output_array": "parse",
    "state_save_qc": "state_save_qc",
    "save_state_snapshot": "state_save_qc",
    "save_state_snapshot_array": "state_save_qc",
    "frequency": "frequency",
    "compute_frequency": "frequency",
    "publish": "publish",
    "publish_tiles": "publish",
}
NATIVE_SHUD_STAGE_ALIASES = {"forecast", "run_shud_forecast", "forecast_run", "analysis_run"}
TRANSIENT_RETRY_REASON_CODES = {
    "SLURM_TIMEOUT",
    "SLURM_JOB_TIMEOUT",
    "NODE_FAILURE",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "STORAGE_WRITE_FAILED",
    "SBATCH_SUBMISSION_FAILED",
    "SLURM_UNAVAILABLE",
    "SOURCE_CYCLE_UNAVAILABLE",
    "SOURCE_UNAVAILABLE",
    "ADAPTER_UNAVAILABLE",
}


class SchedulerCandidateLike(Protocol):
    candidate_id: str
    source_id: str
    cycle_id: str
    cycle_time_utc: datetime
    model_id: str
    basin_id: str
    basin_version_id: str | None
    river_network_version_id: str | None
    resource_profile: Mapping[str, Any]
    run_id: str
    forcing_version_id: str

@dataclass(frozen=True)
class CandidateStateDecision:
    action: str
    reason: str | None
    evidence: Mapping[str, Any] = field(default_factory=dict)
