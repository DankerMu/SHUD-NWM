from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from services.orchestrator.persistence import PipelineJob, PipelineStore
from services.slurm_gateway.config import SlurmGatewaySettings
from services.slurm_gateway.gateway import SlurmGatewayError
from services.slurm_gateway.models import SubmitJobRequest

TRANSIENT_ERROR_CODES: set[str] = {
    "SLURM_TIMEOUT",
    "SLURM_JOB_TIMEOUT",
    "NODE_FAILURE",
    "STORAGE_WRITE_FAILED",
    "SBATCH_SUBMISSION_FAILED",
    "SLURM_UNAVAILABLE",
}
NON_TRANSIENT_ERROR_CODES: set[str] = {
    "INVALID_MANIFEST",
    "PERMISSION_DENIED",
    "OUTPUT_INCOMPLETE",
    "TEMPLATE_NOT_ALLOWED",
    "MANIFEST_SCHEMA_INVALID",
    "OUT_OF_MEMORY",
}
DEFAULT_BACKOFF_SCHEDULE = [60, 300, 900]
ACTIVE_RETRY_STATUSES = {"pending", "submitted", "running"}
FAILED_RETRY_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed"}


def is_transient_error(error_code: str | None) -> bool:
    return error_code in TRANSIENT_ERROR_CODES


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


class RetryService:
    def __init__(self, store: PipelineStore, config: RetryConfig) -> None:
        self.store = store
        self.config = config

    def should_auto_retry(self, job: PipelineJob) -> bool:
        if job.status == "permanently_failed":
            return False
        if not is_transient_error(job.error_code):
            return False
        return job.retry_count < self.config.max_retries

    def handle_failed_job(self, job: PipelineJob) -> PipelineJob:
        if self.should_auto_retry(job):
            return self.schedule_auto_retry(job)
        return self.mark_permanently_failed(job)

    def schedule_auto_retry(self, job: PipelineJob) -> PipelineJob:
        status_from = job.status
        previous_error = job.error_code
        next_retry_count = job.retry_count + 1
        backoff_seconds = compute_backoff_seconds(job.retry_count, self.config.backoff_schedule)

        retry_job = self.store.create_job(
            job_id=f"{job.job_id}_retry_{next_retry_count}",
            run_id=job.run_id,
            cycle_id=job.cycle_id,
            job_type=job.job_type,
            slurm_job_id=None,
            model_id=job.model_id,
            stage=job.stage,
            status="pending",
            commit=False,
        )
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
            },
        )
        return job

    def attempt_manual_retry(self, run_id: str, gateway: RetrySubmitter | None = None) -> PipelineJob:
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

            jobs = self.store.query_jobs_by_run(run_id)
            active_job = next((job for job in jobs if job.status in ACTIVE_RETRY_STATUSES), None)
            if active_job is not None:
                raise RetryConflictError(run_id, active_job)

            failed_job = next((job for job in reversed(jobs) if job.status in FAILED_RETRY_STATUSES), None)
            if failed_job is None:
                raise RetryNotFoundError(run_id)

            status_from = failed_job.status
            previous_error = failed_job.error_code
            next_retry_count = failed_job.retry_count + 1
            retry_job = self.store.create_job(
                job_id=f"{run_id}_retry_{uuid4().hex[:8]}",
                run_id=failed_job.run_id,
                cycle_id=failed_job.cycle_id,
                job_type=failed_job.job_type,
                slurm_job_id=None,
                model_id=failed_job.model_id,
                stage=failed_job.stage,
                status="pending",
                commit=False,
            )
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
                },
                commit=False,
            )
            if gateway is not None:
                self._submit_retry_job(retry_job, gateway)
            if failed_job.run_id and has_hydro_run_table:
                self.store.session.execute(
                    text(
                        """
                        UPDATE hydro.hydro_run
                        SET status = 'pending',
                            error_code = NULL,
                            error_message = NULL
                        WHERE run_id = :run_id
                          AND status = 'failed'
                        """
                    ),
                    {"run_id": failed_job.run_id},
                )
        self.store.session.commit()
        self.store.session.refresh(retry_job)
        return retry_job

    def expire_stale_retries(self, max_age_seconds: int) -> list[PipelineJob]:
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        statement = (
            select(PipelineJob)
            .where(PipelineJob.status == "pending")
            .where(PipelineJob.created_at < cutoff)
            .order_by(PipelineJob.created_at.asc())
        )
        expired = list(self.store.session.scalars(statement))
        for job in expired:
            status_from = job.status
            job.status = "failed"
            job.error_code = "RETRY_STALE_PENDING"
            job.error_message = f"Pending retry exceeded max age of {max_age_seconds} seconds."
            job.finished_at = datetime.now(UTC)
            job.updated_at = job.finished_at
            self.store.session.add(job)
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
        self.store.session.commit()
        for job in expired:
            self.store.session.refresh(job)
        return expired

    def _submit_retry_job(self, retry_job: PipelineJob, gateway: RetrySubmitter) -> None:
        try:
            submitted = gateway.submit_job(
                SubmitJobRequest(
                    run_id=retry_job.run_id,
                    model_id=retry_job.model_id,
                    job_type=retry_job.job_type,
                    manifest={
                        "run_id": retry_job.run_id,
                        "model_id": retry_job.model_id,
                        "cycle_id": retry_job.cycle_id,
                        "job_type": retry_job.job_type,
                        "stage": retry_job.stage,
                        "pipeline_job_id": retry_job.job_id,
                        "retry_count": retry_job.retry_count,
                    },
                )
            )
        except Exception as error:
            retry_job.status = "submission_failed"
            retry_job.error_code = _retry_submission_error_code(error)
            retry_job.error_message = str(getattr(error, "message", None) or error)
            retry_job.finished_at = datetime.now(UTC)
            retry_job.updated_at = retry_job.finished_at
            self.store.session.add(retry_job)
            self.store.insert_event(
                entity_type="pipeline_job",
                entity_id=retry_job.job_id,
                event_type="submission",
                status_from="pending",
                status_to="submission_failed",
                message=f"Manual retry submission failed: {retry_job.error_message}",
                details={
                    "trigger": "manual",
                    "error_code": retry_job.error_code,
                    "error_message": retry_job.error_message,
                },
                commit=False,
            )
            return

        submitted_payload = _coerce_gateway_payload(submitted)
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


def _has_hydro_run_table(store: PipelineStore) -> bool:
    try:
        return inspect(store.session.get_bind()).has_table("hydro_run", schema="hydro")
    except SQLAlchemyError:
        return False


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
