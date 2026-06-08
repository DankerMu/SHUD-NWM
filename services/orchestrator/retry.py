from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from packages.common.auth_policy import PolicyDecision, require_policy_evidence, trusted_internal_policy_decision
from packages.common.redaction import redact_payload
from services.orchestrator.persistence import PipelineJob, PipelineStore
from services.slurm_gateway.config import SlurmGatewaySettings
from services.slurm_gateway.gateway import SlurmGatewayError
from services.slurm_gateway.models import SubmitJobRequest

TRANSIENT_ERROR_CODES: set[str] = {
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
NON_TRANSIENT_ERROR_CODES: set[str] = {
    "INVALID_MANIFEST",
    "MALFORMED_INPUT",
    "POLICY_BLOCKED",
    "PERMISSION_DENIED",
    "OUTPUT_INCOMPLETE",
    "TEMPLATE_NOT_ALLOWED",
    "MANIFEST_SCHEMA_INVALID",
    "WARM_START_CHECKPOINT_RETRY",
}
DEFAULT_BACKOFF_SCHEDULE = [60, 300, 900]
ACTIVE_RETRY_STATUSES = {"pending", "queued", "submitted", "running"}
FAILED_RETRY_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed"}
MANUAL_RETRY_SOURCE_STATUSES = FAILED_RETRY_STATUSES | {"cancelled"}
TERMINAL_SUCCESS_RETRY_STATUSES = {"succeeded", "complete", "published"}
DURABLE_HYDRO_SUCCESS_STATUSES = {"succeeded", "parsed", "frequency_done", "published"}
PARTIAL_OR_FAILED_HYDRO_STATUSES = {"failed", "cancelled", "partially_failed"}
REUSABLE_AUTO_RETRY_STATUSES = {"pending", "submission_failed"}


def is_transient_error(error_code: str | None) -> bool:
    return error_code in TRANSIENT_ERROR_CODES


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
    if code in {"PUBLISH_FAILED", "FAILED_PUBLISH", "FREQUENCY_FAILED", "NO_PUBLISHABLE_PRODUCTS"}:
        return "publication_failure"
    if code in {
        "SLURM_TIMEOUT",
        "SLURM_JOB_TIMEOUT",
        "NODE_FAILURE",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "SLURM_UNAVAILABLE",
        "SBATCH_SUBMISSION_FAILED",
        "STORAGE_WRITE_FAILED",
    }:
        return "transient_slurm_runtime"
    if code in {"SHUD_FAILED", "FAILED_RUN", "RUNTIME_FAILED"}:
        return "shud_runtime_failure"
    if code == "WARM_START_CHECKPOINT_RETRY":
        return "warm_start_checkpoint_repair"
    if code in {"INVALID_MANIFEST", "MANIFEST_SCHEMA_INVALID", "MALFORMED_INPUT"}:
        return "malformed_input"
    if code in {"POLICY_BLOCKED", "PERMISSION_DENIED", "TEMPLATE_NOT_ALLOWED"}:
        return "policy_blocked"
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


class RetrySubmitter(Protocol):
    def submit_job(self, request: SubmitJobRequest) -> Any:
        raise NotImplementedError


@dataclass(frozen=True)
class _RetrySubmissionJob:
    job_id: str
    run_id: str | None
    cycle_id: str | None
    job_type: str
    model_id: str | None
    stage: str | None
    retry_count: int


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
            },
        )
        return job

    def attempt_manual_retry(
        self,
        run_id: str,
        gateway: RetrySubmitter | None = None,
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> PipelineJob:
        if trusted_internal:
            policy_decision = trusted_internal_policy_decision(
                "pipeline.retry_run",
                target_type="pipeline_run",
                target_id=run_id,
                actor_id="trusted-internal:retry-service",
                roles=("sys_admin",),
            )
        decision = require_policy_evidence(
            policy_decision,
            action_id="pipeline.retry_run",
            target_type="pipeline_run",
            target_id=run_id,
        )
        if decision.decision != "allow":
            raise RetryError(
                decision.reason_code,
                decision.reason,
                {
                    "run_id": run_id,
                    "policy_decision": decision.to_dict(),
                    "no_mutation_expected": True,
                },
            )
        if gateway is None:
            raise RetryError(
                "RETRY_EXECUTION_UNAVAILABLE",
                "No Slurm gateway available for retry submission.",
                {"run_id": run_id},
            )

        has_hydro_run_table = _has_hydro_run_table(self.store)
        with self.store.session.begin_nested():
            lock_statement = (
                select(PipelineJob)
                .where(PipelineJob.run_id == run_id)
                .order_by(PipelineJob.submitted_at.asc(), PipelineJob.created_at.asc())
                .with_for_update()
            )
            locked_jobs = list(self.store.session.scalars(lock_statement))
            if not locked_jobs:
                raise RetryNotFoundError(run_id)

            durable_run_status = _hydro_run_status(self.store, run_id) if has_hydro_run_table else None
            if durable_run_status in DURABLE_HYDRO_SUCCESS_STATUSES:
                raise RetryNotFoundError(run_id)

            jobs = _jobs_by_truth_time(self.store.query_jobs_by_run(run_id))
            active_job = next((job for job in jobs if job.status in ACTIVE_RETRY_STATUSES), None)
            if active_job is not None:
                raise RetryConflictError(run_id, active_job)

            latest_truth_job = jobs[-1]
            failed_job = _retry_source_job_for_run(jobs, durable_run_status=durable_run_status)
            if latest_truth_job.status in TERMINAL_SUCCESS_RETRY_STATUSES and failed_job is None:
                raise RetryNotFoundError(run_id)
            if failed_job is None:
                raise RetryNotFoundError(run_id)

            status_from = failed_job.status
            previous_error = failed_job.error_code or ("cancelled" if failed_job.status == "cancelled" else None)
            next_retry_count = failed_job.retry_count + 1
            classification = classify_failure(
                previous_error,
                attempt=next_retry_count,
                retry_limit=self.config.max_retries,
                manual=True,
            )
            retry_job = self._create_pending_manual_retry_job(failed_job, run_id=run_id)
            retry_job.retry_count = next_retry_count
            self.store.session.add(retry_job)
            self.store.insert_event(
                entity_type="pipeline_job",
                entity_id=retry_job.job_id,
                event_type="retry",
                status_from=status_from,
                status_to="pending",
                details={
                    "trigger": "manual",
                    "retry_count": next_retry_count,
                    "previous_error": previous_error,
                    "previous_job_id": failed_job.job_id,
                    "slurm_job_id": retry_job.slurm_job_id,
                    "manual_retry_marker": True,
                    "prior_failure_reason": previous_error,
                    "failure": classification,
                },
                commit=False,
            )
            submission_job = _RetrySubmissionJob(
                job_id=retry_job.job_id,
                run_id=retry_job.run_id,
                cycle_id=retry_job.cycle_id,
                job_type=retry_job.job_type,
                model_id=self._resolve_retry_model_id(retry_job),
                stage=retry_job.stage,
                retry_count=retry_job.retry_count,
            )
            retry_job_id = retry_job.job_id
            retry_run_id = failed_job.run_id

        self.store.session.commit()

        try:
            submitted_payload = self._submit_retry_job(submission_job, gateway)
        except Exception as error:
            with self.store.session.begin_nested():
                retry_job = self._locked_retry_job(retry_job_id)
                self._record_retry_submission_failure(retry_job, error)
            self.store.session.commit()
            self.store.session.refresh(retry_job)
            return retry_job

        with self.store.session.begin_nested():
            retry_job = self._locked_retry_job(retry_job_id)
            self._record_retry_submission_success(retry_job, submitted_payload)
            if retry_run_id and has_hydro_run_table and retry_job.status in {"submitted", "running"}:
                self.store.session.execute(
                    text(
                        """
                        UPDATE hydro.hydro_run
                        SET status = 'pending',
                            error_code = NULL,
                            error_message = NULL
                        WHERE run_id = :run_id
                          AND status IN ('failed', 'cancelled')
                        """
                    ),
                    {"run_id": retry_run_id},
                )
        self.store.session.commit()
        self.store.session.refresh(retry_job)
        return retry_job

    def expire_stale_retries(self, max_age_seconds: int) -> list[PipelineJob]:
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        statement = (
            select(PipelineJob)
            .where(PipelineJob.status == "pending")
            .where(PipelineJob.retry_count > 0)
            .where(PipelineJob.slurm_job_id.is_(None))
            .where(PipelineJob.created_at < cutoff)
            .order_by(PipelineJob.created_at.asc())
        )
        candidates = list(self.store.session.scalars(statement))
        expired: list[PipelineJob] = []
        for job in candidates:
            status_from = job.status
            finished_at = datetime.now(UTC)
            result = self.store.session.execute(
                update(PipelineJob)
                .where(PipelineJob.job_id == job.job_id)
                .where(PipelineJob.status == "pending")
                .values(
                    status="failed",
                    error_code="RETRY_STALE_PENDING",
                    error_message=f"Pending retry exceeded max age of {max_age_seconds} seconds.",
                    finished_at=finished_at,
                    updated_at=finished_at,
                )
            )
            if result.rowcount != 1:
                continue
            self.store.insert_event(
                entity_type="pipeline_job",
                entity_id=job.job_id,
                event_type="retry_expired",
                status_from=status_from,
                status_to="failed",
                message="Pending retry expired before Slurm submission.",
                details={
                    "run_id": job.run_id,
                    "max_age_seconds": max_age_seconds,
                    "error_code": "RETRY_STALE_PENDING",
                },
                commit=False,
            )
            expired.append(job)
        self.store.session.commit()
        for job in expired:
            self.store.session.refresh(job)
        return expired

    def _submit_retry_job(self, retry_job: _RetrySubmissionJob, gateway: RetrySubmitter) -> dict[str, Any]:
        model_id = retry_job.model_id or _model_id_from_run_id(retry_job.run_id) or "unknown"
        manifest = _retry_submission_manifest(retry_job, model_id=model_id)
        submitted = gateway.submit_job(
            SubmitJobRequest(
                run_id=retry_job.run_id,
                model_id=model_id,
                job_type=retry_job.job_type,
                manifest=manifest,
            )
        )
        return _coerce_gateway_payload(submitted)

    def _record_retry_submission_failure(self, retry_job: PipelineJob, error: Exception) -> None:
        error_message = _safe_error_message(str(getattr(error, "message", None) or error))
        retry_job.status = "submission_failed"
        retry_job.error_code = _retry_submission_error_code(error)
        retry_job.error_message = error_message
        retry_job.finished_at = datetime.now(UTC)
        retry_job.updated_at = retry_job.finished_at
        self.store.session.add(retry_job)
        self.store.insert_event(
            entity_type="pipeline_job",
            entity_id=retry_job.job_id,
            event_type="submission",
            status_from="pending",
            status_to="submission_failed",
            message=f"Manual retry submission failed: {error_message}",
            details={
                "trigger": "manual",
                "error_code": retry_job.error_code,
                "error_message": error_message,
            },
            commit=False,
        )

    def _record_retry_submission_success(self, retry_job: PipelineJob, submitted_payload: dict[str, Any]) -> None:
        slurm_job_id = submitted_payload.get("job_id") or submitted_payload.get("slurm_job_id")
        retry_job.slurm_job_id = str(slurm_job_id) if slurm_job_id is not None else None
        retry_job.submitted_at = _parse_gateway_time(submitted_payload.get("submitted_at")) or datetime.now(UTC)
        retry_job.started_at = _parse_gateway_time(submitted_payload.get("started_at"))
        retry_job.finished_at = _parse_gateway_time(submitted_payload.get("finished_at"))
        retry_job.status = "submitted"
        retry_job.error_code = None
        retry_job.error_message = None
        retry_job.updated_at = datetime.now(UTC)
        self.store.session.add(retry_job)
        self.store.insert_event(
            entity_type="pipeline_job",
            entity_id=retry_job.job_id,
            event_type="submission",
            status_from="pending",
            status_to="submitted",
            message=f"Manual retry submitted as Slurm job {retry_job.slurm_job_id}.",
            details={
                "trigger": "manual",
                "slurm_job_id": retry_job.slurm_job_id,
                "gateway_status": _gateway_status(submitted_payload),
            },
            commit=False,
        )

    def _locked_retry_job(self, job_id: str) -> PipelineJob:
        statement = select(PipelineJob).where(PipelineJob.job_id == job_id).with_for_update()
        retry_job = self.store.session.scalars(statement).one()
        return retry_job

    def _resolve_retry_model_id(self, retry_job: PipelineJob) -> str | None:
        if retry_job.model_id:
            return retry_job.model_id
        return _model_id_from_hydro_run(self.store, retry_job.run_id) or _model_id_from_run_id(retry_job.run_id)

    def _create_pending_manual_retry_job(self, failed_job: PipelineJob, *, run_id: str) -> PipelineJob:
        job_id = _next_manual_retry_job_id_for_run(self.store, run_id)
        try:
            return self.store.create_job(
                job_id=job_id,
                run_id=failed_job.run_id,
                cycle_id=failed_job.cycle_id,
                job_type=failed_job.job_type,
                slurm_job_id=None,
                model_id=failed_job.model_id,
                stage=failed_job.stage,
                status="pending",
                retry_count=failed_job.retry_count + 1,
                manual_retry_marker=True,
                commit=False,
            )
        except IntegrityError as error:
            active_job = _active_retry_job_for_run(self.store, run_id) or failed_job
            raise RetryConflictError(run_id, active_job) from error
        except SQLAlchemyError as error:
            self.store.session.rollback()
            raise RetryError(
                "RETRY_GUARD_UNAVAILABLE",
                "Manual retry guard could not be acquired.",
                {"run_id": run_id},
            ) from error

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


def _jobs_by_truth_time(jobs: list[PipelineJob]) -> list[PipelineJob]:
    return sorted(
        jobs,
        key=lambda job: (
            _job_truth_timestamp(job) or datetime.min.replace(tzinfo=UTC),
            job.created_at or datetime.min.replace(tzinfo=UTC),
            job.job_id,
        ),
    )


def _retry_source_job_for_run(jobs: list[PipelineJob], *, durable_run_status: str | None) -> PipelineJob | None:
    latest_truth_job = jobs[-1]
    if latest_truth_job.status in MANUAL_RETRY_SOURCE_STATUSES:
        return latest_truth_job
    if durable_run_status is not None and (
        durable_run_status in PARTIAL_OR_FAILED_HYDRO_STATUSES or str(durable_run_status).startswith("failed")
    ):
        return next((job for job in reversed(jobs) if job.status in MANUAL_RETRY_SOURCE_STATUSES), None)
    return None


def _active_retry_job_for_run(store: PipelineStore, run_id: str) -> PipelineJob | None:
    statement = (
        select(PipelineJob)
        .where(PipelineJob.run_id == run_id)
        .where(PipelineJob.manual_retry_marker.is_(True))
        .where(PipelineJob.status.in_(ACTIVE_RETRY_STATUSES))
        .order_by(PipelineJob.submitted_at.desc(), PipelineJob.created_at.desc())
    )
    try:
        return store.session.scalars(statement).first()
    except SQLAlchemyError:
        return None


def _auto_retry_job_can_be_reused(retry_job: PipelineJob) -> bool:
    if retry_job.manual_retry_marker:
        return False
    if retry_job.slurm_job_id is not None or retry_job.array_task_id is not None:
        return False
    return retry_job.status in REUSABLE_AUTO_RETRY_STATUSES


def _next_manual_retry_job_id_for_run(store: PipelineStore, run_id: str) -> str:
    prefix = f"{run_id}_retry_"
    statement = select(PipelineJob.job_id, PipelineJob.manual_retry_marker).where(PipelineJob.run_id == run_id)
    used_retry_job_ids = {
        str(job_id)
        for job_id, manual_retry_marker in store.session.execute(statement)
        if manual_retry_marker is True or str(job_id).startswith(prefix)
    }
    deterministic_job_id = f"{run_id}_retry_active"
    if deterministic_job_id not in used_retry_job_ids:
        return deterministic_job_id
    sequence = 2
    while f"{run_id}_retry_{sequence}" in used_retry_job_ids:
        sequence += 1
    return f"{run_id}_retry_{sequence}"


def _job_truth_timestamp(job: PipelineJob) -> datetime | None:
    return job.updated_at or job.finished_at or job.submitted_at or job.started_at or job.created_at


def _has_hydro_run_table(store: PipelineStore) -> bool:
    try:
        return inspect(store.session.get_bind()).has_table("hydro_run", schema="hydro")
    except SQLAlchemyError:
        return False


def _model_id_from_hydro_run(store: PipelineStore, run_id: str | None) -> str | None:
    if not run_id:
        return None
    try:
        inspector = inspect(store.session.get_bind())
        column_names = {column["name"] for column in inspector.get_columns("hydro_run", schema="hydro")}
        if "run_id" not in column_names or "model_id" not in column_names:
            return None
        value = store.session.execute(
            text("SELECT model_id FROM hydro.hydro_run WHERE run_id = :run_id LIMIT 1"),
            {"run_id": run_id},
        ).scalar_one_or_none()
    except SQLAlchemyError:
        return None
    return str(value) if value else None


def _hydro_run_status(store: PipelineStore, run_id: str | None) -> str | None:
    if not run_id:
        return None
    try:
        value = store.session.execute(
            text("SELECT status FROM hydro.hydro_run WHERE run_id = :run_id LIMIT 1"),
            {"run_id": run_id},
        ).scalar_one_or_none()
    except SQLAlchemyError:
        return None
    return str(value) if value else None


def _model_id_from_run_id(run_id: str | None) -> str | None:
    if not run_id:
        return None
    match = re.search(r"(?:^|_)(model(?:_[A-Za-z0-9.-]+)+)$", run_id)
    if match is None:
        return None
    return match.group(1)


def _retry_submission_manifest(retry_job: _RetrySubmissionJob, *, model_id: str) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "run_id": retry_job.run_id,
        "model_id": model_id,
        "cycle_id": retry_job.cycle_id,
        "job_type": retry_job.job_type,
        "stage": retry_job.stage,
        "pipeline_job_id": retry_job.job_id,
        "retry_count": retry_job.retry_count,
        "manual_retry_marker": True,
    }
    cycle_identity = _source_cycle_identity(retry_job.cycle_id)
    if retry_job.job_type == "download_source_cycle" and cycle_identity is not None:
        source_id, cycle_time = cycle_identity
        manifest["source_id"] = source_id
        manifest["cycle_time"] = cycle_time
    return manifest


def _source_cycle_identity(cycle_id: str | None) -> tuple[str, str] | None:
    if not cycle_id:
        return None
    match = re.fullmatch(r"(?P<source>[A-Za-z0-9]+)_(?P<cycle>[0-9]{10})", cycle_id)
    if match is None:
        return None
    return match.group("source"), match.group("cycle")


def _coerce_gateway_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise TypeError(f"Expected mapping-like Slurm submission payload, got {type(value).__name__}")


def _parse_gateway_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return None


def _gateway_status(payload: dict[str, Any]) -> str | None:
    status = payload.get("status")
    value = getattr(status, "value", status)
    return str(value) if value is not None else None


def _retry_submission_error_code(error: Exception) -> str:
    if isinstance(error, SlurmGatewayError):
        return error.code
    return "SBATCH_SUBMISSION_FAILED"


def _safe_error_message(message: str) -> str:
    redacted = redact_payload(message)
    return redacted if isinstance(redacted, str) else str(redacted)
