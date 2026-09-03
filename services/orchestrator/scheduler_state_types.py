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
# THE single definition of "is this hydro run in flight?" — chain, chain_repository and
# file_orchestration_journal all bind this very object. #1581's "pending"-less copies are
# adjudicated away: "pending" is what manual retry writes once the retry job is submitted.
ACTIVE_HYDRO_STATUSES = {"created", "staged", "pending", "submitted", "running"}
# Scheduler durable-success predicate: "is the pipeline durably done?". THE single definition —
# chain.COMPLETED_HYDRO_STATUSES, chain_repository.COMPLETED_HYDRO_STATUSES and
# scheduler_state_failure._durable_shud_output_exists all read this very object (#1581).
# Holds "complete" on top of the three members of the manual-retry refusal set in
# services/orchestrator/retry.py, which answers the different question "may an operator retry this
# run?" and is named distinctly on purpose (change durable-status-name-split — grep that name for
# both sides). Do not merge the two.
#
# "complete" is kept as the one member outside the hydro.run_status enum (#1581 design D5). On the
# DB lane it is dead: hydro.run_status is a closed enum (db/migrations/000003_enums.sql plus the
# "pending" ADD VALUE in 000013) and has_completed_pipeline compares status::text, so the member
# can never match. On the file-journal lane no production writer emits it, but
# _validate_hydro_run_identity constrains identity fields only and never the status, and the
# journal's test construction face writes hydro_status="complete" — so dropping it would change
# journal decisions there. tests/test_hydro_status_set_parity.py pins it as that single exception.
DURABLE_HYDRO_SUCCESS_STATUSES = {"succeeded", "parsed", "published", "complete"}
# The hydro_run statuses whose journal write CLEARS the row's error code -- it is NOT the
# durable-output success set above, which answers a different question. Consumed by
# file_orchestration_journal.update_hydro_run_status (the write at :2485 of 4f3fd89a) and, as
# scheduler_state_failure._HYDRO_RUN_CODE_CLEARING_STATUSES, by _downstream_recorded_error_code.
# The SQL backend's update_hydro_run_status only assigns when the incoming value is not None, so a
# successful transition there leaves an older code in place: a code sitting on a run row in one of
# these statuses is stale residue rather than the current failure's own record. Frozen because
# tests/test_production_scheduler.py pins the alias' top-level type.
HYDRO_RUN_CODE_CLEARING_STATUSES = frozenset({"pending", "created", "succeeded", "complete", "parsed", "published"})
TERMINAL_PIPELINE_SUCCESS_STATUSES = {"succeeded", "complete", "published"}
TERMINAL_PIPELINE_COMPLETION_STAGES = {"parse", "state_save_qc", "publish"}
FAILED_PIPELINE_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed"}
DOWNSTREAM_RESTART_STAGES = ("convert", "forcing", "forecast", "parse", "state_save_qc", "publish", "copyback")
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
    "publish": "publish",
    "publish_tiles": "publish",
    "copyback": "copyback",
    "run_tree_copyback": "copyback",
}
NATIVE_SHUD_STAGE_ALIASES = {"forecast", "run_shud_forecast", "forecast_run", "analysis_run"}
TRANSIENT_RETRY_REASON_CODES = {
    "SLURM_TIMEOUT",
    "SLURM_JOB_TIMEOUT",
    "SLURM_DEADLINE",
    "NODE_FAILURE",
    "PREEMPTED",
    "STORAGE_WRITE_FAILED",
    "SBATCH_SUBMISSION_FAILED",
    "SLURM_UNAVAILABLE",
    "SLURM_RESERVATION_LOST",
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
