from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from packages.common import status_vocab as _vocab
from packages.common.redaction import redact_payload
from services.orchestrator.persistence import PipelineJob, PipelineStore
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
_RUNTIME_ROOT_FIELDS = (
    "workspace_dir",
    "object_store_root",
    "object_store_prefix",
    "published_artifact_root",
    "published_artifact_uri_prefix",
)
_DB_FREE_REQUIRED_FIELD = "scheduler_db_free_required"
_DB_FREE_SELECTOR_FIELDS = (
    "scheduler_allowed_roots",
    "scheduler_registry_backend",
    "scheduler_registry_manifest",
    "scheduler_canonical_readiness_backend",
    "scheduler_canonical_readiness_index",
    "scheduler_state_index_backend",
    "scheduler_state_index",
)
_DB_FREE_RUNTIME_FIELDS = (_DB_FREE_REQUIRED_FIELD, *_DB_FREE_SELECTOR_FIELDS)


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


class RetryService:
    def __init__(self, store: PipelineStore, config: RetryConfig) -> None:
        self.store = store
        self.config = config

    def should_auto_retry(self, job: PipelineJob) -> bool:
        policy = self.retry_policy_for_job(job)
        return bool(policy["auto_retry"])

    def retry_policy_for_job(self, job: PipelineJob) -> dict[str, Any]:
        classification = classify_failure(
            job.error_code,
            attempt=job.retry_count,
            retry_limit=self.config.max_retries,
        )
        return {
            **classification,
            "auto_retry": job.status != "permanently_failed"
            and classification["retryable"]
            and not classification["permanent"],
        }

    def handle_failed_job(self, job: PipelineJob) -> PipelineJob:
        if self.should_auto_retry(job):
            return self.schedule_auto_retry(job)
        return self.mark_permanently_failed(job)

    def schedule_auto_retry(self, job: PipelineJob) -> PipelineJob:
        status_from = job.status
        previous_error = job.error_code
        classification = classify_failure(
            previous_error,
            attempt=job.retry_count,
            retry_limit=self.config.max_retries,
        )
        next_retry_count = job.retry_count + 1
        backoff_seconds = compute_backoff_seconds(job.retry_count, self.config.backoff_schedule)
        retry_job_id = f"{job.job_id}_retry_{next_retry_count}"
        reused_existing_retry_job = False

        retry_job = self._auto_retry_job_for_update(retry_job_id)
        if retry_job is None:
            retry_job = self.store.create_job(
                job_id=retry_job_id,
                run_id=job.run_id,
                cycle_id=job.cycle_id,
                job_type=job.job_type,
                slurm_job_id=None,
                model_id=job.model_id,
                stage=job.stage,
                status="pending",
                commit=False,
            )
        elif not _auto_retry_job_can_be_reused(retry_job):
            raise RetryError(
                "AUTO_RETRY_JOB_CONFLICT",
                "Existing auto retry job cannot be reset safely.",
                {
                    "retry_job_id": retry_job_id,
                    "existing_status": retry_job.status,
                    "existing_slurm_job_id": retry_job.slurm_job_id,
                    "existing_array_task_id": retry_job.array_task_id,
                    "previous_job_id": job.job_id,
                },
            )
        else:
            reused_existing_retry_job = True
            self._reset_auto_retry_job(retry_job, source_job=job, retry_count=next_retry_count)
        retry_job.retry_count = next_retry_count
        self.store.session.add(retry_job)
        self.store.insert_event(
            entity_type="pipeline_job",
            entity_id=retry_job.job_id,
            event_type="retry",
            status_from=status_from,
            status_to="pending",
            details={
                "trigger": "auto",
                "retry_count": next_retry_count,
                "previous_error": previous_error,
                "backoff_seconds": backoff_seconds,
                "previous_job_id": job.job_id,
                "slurm_job_id": retry_job.slurm_job_id,
                "failure": classification,
                "reused_existing_retry_job": reused_existing_retry_job,
            },
            commit=False,
        )
        self.store.session.commit()
        self.store.session.refresh(retry_job)
        return retry_job

    def mark_permanently_failed(self, job: PipelineJob) -> PipelineJob:
        if job.status == "permanently_failed":
            return job

        status_from = job.status
        last_error = job.error_code
        classification = classify_failure(
            last_error,
            attempt=job.retry_count,
            retry_limit=self.config.max_retries,
        )
        auto_retry_skipped = auto_retry_skipped_details(last_error)
        job.status = "permanently_failed"
        job.updated_at = datetime.now(UTC)
        self.store.session.add(job)
        self.store.session.flush()
        self.store.insert_event(
            entity_type="pipeline_job",
            entity_id=job.job_id,
            event_type="permanently_failed",
            status_from=status_from,
            status_to="permanently_failed",
            details={
                "final_retry_count": job.retry_count,
                "last_error": last_error,
                "failure": classification,
                "automatic_retry_stopped": True,
                **(auto_retry_skipped or {}),
            },
        )
        warn_unknown_error_code(auto_retry_skipped)
        return job

    def _auto_retry_job_for_update(self, job_id: str) -> PipelineJob | None:
        statement = select(PipelineJob).where(PipelineJob.job_id == job_id).with_for_update()
        return self.store.session.scalars(statement).first()

    @staticmethod
    def _reset_auto_retry_job(retry_job: PipelineJob, *, source_job: PipelineJob, retry_count: int) -> None:
        now = datetime.now(UTC)
        retry_job.run_id = source_job.run_id
        retry_job.cycle_id = source_job.cycle_id
        retry_job.job_type = source_job.job_type
        retry_job.model_id = source_job.model_id
        retry_job.stage = source_job.stage
        retry_job.status = "pending"
        retry_job.slurm_job_id = None
        retry_job.array_task_id = None
        retry_job.submitted_at = now
        retry_job.started_at = None
        retry_job.finished_at = None
        retry_job.exit_code = None
        retry_job.retry_count = retry_count
        retry_job.manual_retry_marker = False
        retry_job.idempotency_key = None
        retry_job.candidate_id = None
        retry_job.error_code = None
        retry_job.error_message = None
        retry_job.log_uri = None
        retry_job.updated_at = now


def _auto_retry_job_can_be_reused(retry_job: PipelineJob) -> bool:
    if retry_job.manual_retry_marker:
        return False
    if retry_job.slurm_job_id is not None or retry_job.array_task_id is not None:
        return False
    return retry_job.status in REUSABLE_AUTO_RETRY_STATUSES


def _safe_error_message(message: str) -> str:
    redacted = redact_payload(message)
    return redacted if isinstance(redacted, str) else str(redacted)


def _mapping_at(details: Mapping[str, Any], path: tuple[str, ...]) -> Mapping[str, Any] | None:
    value: Any = details
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value if isinstance(value, Mapping) else None


def _has_runtime_root_field(value: Mapping[str, Any]) -> bool:
    return any(field in value for field in (*_RUNTIME_ROOT_FIELDS, *_DB_FREE_RUNTIME_FIELDS))
