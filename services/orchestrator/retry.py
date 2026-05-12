from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from services.orchestrator.persistence import PipelineJob, PipelineStore
from services.slurm_gateway.config import SlurmGatewaySettings

TRANSIENT_ERROR_CODES: set[str] = {
    "SLURM_TIMEOUT",
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

    def attempt_manual_retry(self, run_id: str) -> PipelineJob:
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
                },
                commit=False,
            )
            if failed_job.run_id and has_hydro_run_table:
                self.store.session.execute(
                    text(
                        """
                        UPDATE hydro.hydro_run
                        SET status = 'running',
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


def _has_hydro_run_table(store: PipelineStore) -> bool:
    try:
        return inspect(store.session.get_bind()).has_table("hydro_run", schema="hydro")
    except SQLAlchemyError:
        return False
