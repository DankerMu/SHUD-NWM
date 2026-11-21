from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from packages.common import status_vocab as _vocab
from packages.common.redaction import redact_payload
from services.orchestrator.persistence import PipelineJob
from services.orchestrator.scheduler_state_types import DOWNSTREAM_RESTART_STAGES
from services.slurm_gateway.config import SlurmGatewaySettings

LOGGER = logging.getLogger(__name__)

TRANSIENT_ERROR_CODES: set[str] = {
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
    # #2584: a state_save_qc submit that crossed the gateway boundary without a proven
    # rejection -- Slurm may already hold the job; the gateway's own code travels as
    # ``origin_error_code`` on the submission-failure event.
    "STATE_SAVE_SUBMIT_AMBIGUOUS",
}
# The `{STAGE}_FAILED` members are derived from the canonical downstream stage domain
# (openspec change retry-stage-failure-classification, #1462) so that adding a stage to
# `DOWNSTREAM_RESTART_STAGES` classifies its minted failure code here too, instead of
# leaving a production-mainline code on neither list.  Deliberately a closed-domain
# derivation and NOT an `endswith("_FAILED")` predicate: a suffix rule would swallow
# `SLURM_JOB_FAILED`, which openspec change slurm-error-code-transient-coverage pins onto
# NEITHER list, as well as the transient `SBATCH_SUBMISSION_FAILED` / `STORAGE_WRITE_FAILED`.
NON_TRANSIENT_ERROR_CODES: set[str] = {
    "INVALID_MANIFEST",
    "MALFORMED_INPUT",
    "POLICY_BLOCKED",
    "PERMISSION_DENIED",
    "OUTPUT_INCOMPLETE",
    "OUT_OF_MEMORY",
    "TEMPLATE_NOT_ALLOWED",
    "MANIFEST_SCHEMA_INVALID",
    "WARM_START_CHECKPOINT_RETRY",
    # SHUD runtime family (classifier `shud_runtime_failure`): rerunning the same
    # configuration does not converge.
    "SHUD_FAILED",
    "FAILED_RUN",
    "RUNTIME_FAILED",
    # Task-level codes minted alongside the stage family by the scheduler-state reader.
    "STATE_SAVE_QC_TASK_FAILED",
    "PARSE_TASK_FAILED",
    "PUBLISH_TASK_FAILED",
} | {f"{stage.upper()}_FAILED" for stage in DOWNSTREAM_RESTART_STAGES}
AUTO_RETRY_SKIPPED_NON_TRANSIENT_REASON = "non_transient_error"
AUTO_RETRY_SKIPPED_UNKNOWN_REASON = "unknown_error_code_defaulted_non_transient"
DEFAULT_BACKOFF_SCHEDULE = [60, 300, 900]
ACTIVE_RETRY_STATUSES = set(_vocab.ACTIVE_PIPELINE_STATUSES)
FAILED_RETRY_STATUSES = set(_vocab.FAILED_PIPELINE_STATUSES)
MANUAL_RETRY_SOURCE_STATUSES = FAILED_RETRY_STATUSES | {"cancelled"}
TERMINAL_SUCCESS_RETRY_STATUSES = set(_vocab.PIPELINE_JOB_SUCCESS)
# Manual-retry refusal predicate: a durable hydro run in one of these statuses makes the run
# non-retryable by hand (RetryNotFoundError). Deliberately NOT the same set as
# scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES, which additionally holds "complete":
# that set answers "is the pipeline durably done?" for scheduler decisions, this one answers
# "may an operator retry this run?". Do not merge the two (see change durable-status-name-split).
MANUAL_RETRY_DURABLE_SUCCESS_STATUSES = {"succeeded", "parsed", "published"}
PARTIAL_OR_FAILED_HYDRO_STATUSES = {"failed", "cancelled", "partially_failed"}
REUSABLE_AUTO_RETRY_STATUSES = {"pending", "submission_failed"}


def is_transient_error(error_code: str | None) -> bool:
    return error_code in TRANSIENT_ERROR_CODES


def auto_retry_skipped_details(error_code: str | None) -> dict[str, Any] | None:
    """Payload for a job whose RECORDED error code is blocked by classification.

    Returns ``None`` whenever the block is not classification-caused: a
    transient code (its permanent failure means the retry budget ran out) and a
    job with no recorded code (nothing to audit-classify) both stay keyless and
    are told apart by the event's existing ``failure`` sub-dict.
    """

    if error_code is None or str(error_code) == "":
        return None
    code = str(error_code)
    if code in TRANSIENT_ERROR_CODES:
        return None
    reason = (
        AUTO_RETRY_SKIPPED_NON_TRANSIENT_REASON
        if code in NON_TRANSIENT_ERROR_CODES
        else AUTO_RETRY_SKIPPED_UNKNOWN_REASON
    )
    return {"auto_retry_skipped": True, "reason": reason, "error_code": code}


def warn_unknown_error_code(auto_retry_skipped: Mapping[str, Any] | None) -> None:
    """Emit the spec-required warning once per APPENDED permanently_failed event.

    Call sites invoke this only after the event is known to have landed, so a
    repository outcome that skips the append leaves no orphan warning behind.
    """

    if not auto_retry_skipped or auto_retry_skipped.get("reason") != AUTO_RETRY_SKIPPED_UNKNOWN_REASON:
        return
    LOGGER.warning(
        "unknown error_code '%s' defaulted to non-transient — add to classification list",
        auto_retry_skipped.get("error_code"),
    )


def classify_failure(
    error_code: str | None,
    *,
    attempt: int = 0,
    retry_limit: int | None = None,
    manual: bool = False,
) -> dict[str, Any]:
    code = str(error_code or "UNKNOWN_FAILURE")
    classifier = failure_classifier(code)
    retryable = is_retryable_failure(code)
    limit_exhausted = retry_limit is not None and attempt >= retry_limit
    permanent = not manual and (not retryable or limit_exhausted)
    return {
        "classifier": classifier,
        "reason_code": code,
        "retryable": retryable and not limit_exhausted,
        "permanent": permanent,
        "attempt": attempt,
        "retry_limit": retry_limit,
        "limit_exhausted": limit_exhausted,
        "manual_retry_marker": manual,
    }


def failure_classifier(error_code: str | None) -> str:
    code = str(error_code or "").upper()
    if code in {"SOURCE_CYCLE_UNAVAILABLE", "SOURCE_UNAVAILABLE", "ADAPTER_UNAVAILABLE"}:
        return "source_unavailable"
    if code in {"ADAPTER_FAILURE", "DATA_ADAPTER_FAILED", "DOWNLOAD_FAILED", "FAILED_DOWNLOAD"}:
        return "adapter_failure"
    if code in {"FORCING_FAILED", "FAILED_FORCING", "FORCING_TASK_FAILED"}:
        return "forcing_failure"
    if code in {"PARSE_FAILED", "FAILED_PARSE", "OUTPUT_INCOMPLETE"}:
        return "parse_failure"
    if code in {"PUBLISH_FAILED", "FAILED_PUBLISH", "NO_PUBLISHABLE_PRODUCTS"}:
        return "publication_failure"
    if code in {
        "SLURM_TIMEOUT",
        "SLURM_JOB_TIMEOUT",
        "SLURM_DEADLINE",
        "NODE_FAILURE",
        "PREEMPTED",
        "SLURM_UNAVAILABLE",
        "SLURM_RESERVATION_LOST",
        "SBATCH_SUBMISSION_FAILED",
        "STORAGE_WRITE_FAILED",
        "STATE_SAVE_SUBMIT_AMBIGUOUS",
    }:
        return "transient_slurm_runtime"
    if code in {"SHUD_FAILED", "FAILED_RUN", "RUNTIME_FAILED"}:
        return "shud_runtime_failure"
    if code == "WARM_START_CHECKPOINT_RETRY":
        return "warm_start_checkpoint_repair"
    if code == "OUT_OF_MEMORY":
        return "resource_configuration"
    if code in {"INVALID_MANIFEST", "MANIFEST_SCHEMA_INVALID", "MALFORMED_INPUT"}:
        return "malformed_input"
    if code in {"POLICY_BLOCKED", "PERMISSION_DENIED", "TEMPLATE_NOT_ALLOWED"}:
        return "policy_blocked"
    if code == "SLURM_JOB_FAILED":
        # The gateway's catch-all for terminal Slurm states with no specific
        # mapping.  It is deliberately registered on no classification surface, so
        # it defaults to non-transient and downstream resume refuses it until an
        # operator adjudicates.  Spelled out rather than left to fall through, so a
        # later branch cannot adopt it by accident (openspec change
        # slurm-error-code-transient-coverage).
        return "unknown_failure"
    return "unknown_failure"


def is_retryable_failure(error_code: str | None) -> bool:
    return is_transient_error(error_code)


def compute_backoff_seconds(retry_count: int, backoff_schedule: list[int] | None = None) -> int:
    schedule = backoff_schedule or DEFAULT_BACKOFF_SCHEDULE
    index = min(max(retry_count, 0), len(schedule) - 1)
    return schedule[index]


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 3
    backoff_schedule: list[int] = field(default_factory=lambda: list(DEFAULT_BACKOFF_SCHEDULE))

    @classmethod
    def from_settings(cls, settings: SlurmGatewaySettings) -> RetryConfig:
        return cls(
            max_retries=settings.max_retries,
            backoff_schedule=list(settings.retry_backoff_seconds),
        )


class RetryError(RuntimeError):
    status_code = 500

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class RetryConflictError(RetryError):
    status_code = 409

    def __init__(self, run_id: str, active_job: PipelineJob) -> None:
        super().__init__(
            "RETRY_CONFLICT",
            "A retry is already in progress for this run.",
            {
                "run_id": run_id,
                "active_job_id": active_job.job_id,
                "active_status": active_job.status,
            },
        )


class RetryNotFoundError(RetryError):
    status_code = 404

    def __init__(self, run_id: str) -> None:
        super().__init__(
            "RETRY_NOT_FOUND",
            "No retryable failure found for this run.",
            {"run_id": run_id},
        )


def _auto_retry_job_can_be_reused(retry_job: PipelineJob) -> bool:
    if retry_job.manual_retry_marker:
        return False
    if retry_job.slurm_job_id is not None or retry_job.array_task_id is not None:
        return False
    return retry_job.status in REUSABLE_AUTO_RETRY_STATUSES


def _safe_error_message(message: str) -> str:
    redacted = redact_payload(message)
    return redacted if isinstance(redacted, str) else str(redacted)
