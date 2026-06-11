from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from packages.common.auth_policy import PolicyDecision, require_policy_evidence, trusted_internal_policy_decision
from packages.common.redaction import redact_payload
from packages.common.slurm_env import secret_manifest_value_reason
from services.orchestrator.persistence import PipelineEvent, PipelineJob, PipelineStore
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
DOWNLOAD_SOURCE_CYCLE_JOB_TYPE = "download_source_cycle"
RETRY_RUNTIME_ROOTS_UNRESOLVED = "RETRY_RUNTIME_ROOTS_UNRESOLVED"
RETRY_RUNTIME_ROOTS_SECRET_BEARING = "RETRY_RUNTIME_ROOTS_SECRET_BEARING"
RETRY_RUNTIME_ROOTS_UNSAFE = "RETRY_RUNTIME_ROOTS_UNSAFE"
_RUNTIME_ROOT_FIELDS = (
    "workspace_dir",
    "object_store_root",
    "object_store_prefix",
    "published_artifact_root",
    "published_artifact_uri_prefix",
)
_REQUIRED_RUNTIME_ROOT_FIELDS = ("workspace_dir", "object_store_root")
_LOCAL_RUNTIME_ROOT_FIELDS = ("workspace_dir", "object_store_root", "published_artifact_root")
_PUBLISHED_RUNTIME_ROOT_FIELDS = ("published_artifact_root", "published_artifact_uri_prefix")
_ROOT_EVIDENCE_VALUE_MAX_LENGTH = 256
_RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT = 32
_RUNTIME_ROOT_EVENT_ROW_SCAN_LIMIT = 64
_RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT = 16
_URI_STYLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


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
class _RuntimeRootResolution:
    manifest_fields: dict[str, str]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _RetrySubmissionResult:
    payload: dict[str, Any]
    runtime_root_resolution: dict[str, Any] | None = None
    runtime_root_contract: dict[str, str] | None = None


@dataclass(frozen=True)
class _RetrySubmissionJob:
    job_id: str
    run_id: str | None
    cycle_id: str | None
    job_type: str
    model_id: str | None
    stage: str | None
    retry_count: int
    previous_job_id: str | None


@dataclass(frozen=True)
class _RuntimeRootCandidateResolution:
    resolved: dict[str, tuple[str, str]]
    missing: list[str]
    rejected: list[dict[str, str]]
    secret_rejected: bool
    unsafe_rejected: bool

    @property
    def complete(self) -> bool:
        return not self.missing and not self.secret_rejected and not self.unsafe_rejected


@dataclass(frozen=True)
class _RuntimeRootCandidate:
    source: str
    value: Mapping[str, Any]


@dataclass(frozen=True)
class _RuntimeRootCandidateBatch:
    candidates: list[_RuntimeRootCandidate]
    event_candidate_returned_count: int = 0
    event_candidate_total_count: int = 0
    event_candidate_omitted_count: int = 0
    event_rows_scanned_count: int = 0
    event_rows_total_count: int = 0
    event_rows_omitted_count: int = 0
    manual_retry_event_rows_ignored: int = 0


class _RetryRuntimeRootResolutionError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {"runtime_root_resolution": evidence}


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
                previous_job_id=failed_job.job_id,
            )
            retry_job_id = retry_job.job_id
            retry_run_id = failed_job.run_id

        self.store.session.commit()

        try:
            submitted_result = self._submit_retry_job(submission_job, gateway)
        except Exception as error:
            with self.store.session.begin_nested():
                retry_job = self._locked_retry_job(retry_job_id)
                self._record_retry_submission_failure(retry_job, error)
            self.store.session.commit()
            self.store.session.refresh(retry_job)
            return retry_job

        with self.store.session.begin_nested():
            retry_job = self._locked_retry_job(retry_job_id)
            self._record_retry_submission_success(
                retry_job,
                submitted_result.payload,
                runtime_root_resolution=submitted_result.runtime_root_resolution,
                runtime_root_contract=submitted_result.runtime_root_contract,
            )
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

    def submission_runtime_root_resolution(self, job_id: str) -> dict[str, Any] | None:
        statement = (
            select(PipelineEvent)
            .where(PipelineEvent.entity_type == "pipeline_job")
            .where(PipelineEvent.entity_id == job_id)
            .where(PipelineEvent.event_type == "submission")
            .order_by(PipelineEvent.event_id.desc())
        )
        for event in self.store.session.scalars(statement):
            details = event.details if isinstance(event.details, Mapping) else {}
            evidence = details.get("runtime_root_resolution")
            if isinstance(evidence, Mapping):
                return _redacted_mapping(evidence)
        return None

    def _submit_retry_job(self, retry_job: _RetrySubmissionJob, gateway: RetrySubmitter) -> _RetrySubmissionResult:
        model_id = retry_job.model_id or _model_id_from_run_id(retry_job.run_id) or "unknown"
        runtime_root_resolution = self._resolve_retry_runtime_roots(retry_job)
        manifest = _retry_submission_manifest(
            retry_job,
            model_id=model_id,
            runtime_root_fields=runtime_root_resolution.manifest_fields if runtime_root_resolution else None,
        )
        try:
            submitted = gateway.submit_job(
                SubmitJobRequest(
                    run_id=retry_job.run_id,
                    model_id=model_id,
                    job_type=retry_job.job_type,
                    manifest=manifest,
                )
            )
        except Exception as error:
            if runtime_root_resolution is not None:
                _attach_retry_runtime_root_resolution(error, runtime_root_resolution.evidence)
                _attach_retry_runtime_root_contract(error, runtime_root_resolution.manifest_fields)
            raise
        return _RetrySubmissionResult(
            payload=_coerce_gateway_payload(submitted),
            runtime_root_resolution=runtime_root_resolution.evidence if runtime_root_resolution else None,
            runtime_root_contract=runtime_root_resolution.manifest_fields if runtime_root_resolution else None,
        )

    def _resolve_retry_runtime_roots(self, retry_job: _RetrySubmissionJob) -> _RuntimeRootResolution | None:
        if retry_job.job_type != DOWNLOAD_SOURCE_CYCLE_JOB_TYPE:
            return None

        candidate_batch = self._retry_runtime_root_candidates(retry_job)
        rejected: list[dict[str, str]] = []
        rejected_total_count = 0
        best_resolved: dict[str, tuple[str, str]] = {}
        best_missing: list[str] = list(_REQUIRED_RUNTIME_ROOT_FIELDS)
        secret_rejected = False
        unsafe_rejected = False

        for candidate in candidate_batch.candidates:
            resolution = _resolve_runtime_root_candidate(candidate.source, candidate.value)
            rejected_total_count += len(resolution.rejected)
            if len(rejected) < _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT:
                remaining_rejections = _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT - len(rejected)
                rejected.extend(resolution.rejected[:remaining_rejections])
            secret_rejected = secret_rejected or resolution.secret_rejected
            unsafe_rejected = unsafe_rejected or resolution.unsafe_rejected
            if len(resolution.resolved) > len(best_resolved):
                best_resolved = resolution.resolved
                best_missing = resolution.missing
            if not resolution.complete or secret_rejected:
                continue

            evidence = _runtime_root_resolution_evidence(
                retry_job,
                resolved=resolution.resolved,
                missing=[],
                rejected=rejected,
                rejected_total_count=rejected_total_count,
                candidate_batch=candidate_batch,
            )
            manifest_fields = {field: value for field, (value, _source) in resolution.resolved.items()}
            return _RuntimeRootResolution(manifest_fields=manifest_fields, evidence=evidence)

        evidence = _runtime_root_resolution_evidence(
            retry_job,
            resolved=best_resolved,
            missing=best_missing,
            rejected=rejected,
            rejected_total_count=rejected_total_count,
            candidate_batch=candidate_batch,
        )
        if secret_rejected:
            raise _RetryRuntimeRootResolutionError(
                RETRY_RUNTIME_ROOTS_SECRET_BEARING,
                "Manual retry runtime-root evidence contains secret-bearing values.",
                evidence,
            )
        if unsafe_rejected:
            raise _RetryRuntimeRootResolutionError(
                RETRY_RUNTIME_ROOTS_UNSAFE,
                "Manual retry runtime-root evidence contains unsafe local root values.",
                evidence,
            )
        if best_missing:
            raise _RetryRuntimeRootResolutionError(
                RETRY_RUNTIME_ROOTS_UNRESOLVED,
                "Manual retry cannot resolve required object-store runtime roots for download_source_cycle.",
                evidence,
            )
        raise _RetryRuntimeRootResolutionError(
            RETRY_RUNTIME_ROOTS_UNRESOLVED,
            "Manual retry cannot resolve required object-store runtime roots for download_source_cycle.",
            evidence,
        )

    def _retry_runtime_root_candidates(self, retry_job: _RetrySubmissionJob) -> _RuntimeRootCandidateBatch:
        candidates: list[_RuntimeRootCandidate] = []
        provenance_job_ids: list[str] = []
        event_candidate_returned_count = 0
        event_candidate_total_count = 0
        event_candidate_omitted_count = 0
        event_rows_scanned_count = 0
        event_rows_total_count = 0
        event_rows_omitted_count = 0
        manual_retry_event_rows_ignored = 0

        if retry_job.previous_job_id:
            provenance_job_ids = _retry_provenance_job_ids(self.store, retry_job.previous_job_id)
            for job_id in provenance_job_ids:
                event_batch = _event_runtime_root_candidates(
                    self.store,
                    job_id,
                    candidate_budget=_RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT - len(candidates),
                )
                candidates.extend(event_batch.candidates)
                event_candidate_returned_count += event_batch.event_candidate_returned_count
                event_candidate_total_count += event_batch.event_candidate_total_count
                event_candidate_omitted_count += event_batch.event_candidate_omitted_count
                event_rows_scanned_count += event_batch.event_rows_scanned_count
                event_rows_total_count += event_batch.event_rows_total_count
                event_rows_omitted_count += event_batch.event_rows_omitted_count
                manual_retry_event_rows_ignored += event_batch.manual_retry_event_rows_ignored
                if len(candidates) >= _RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT:
                    break
        for job_id in _same_run_source_submission_job_ids(self.store, retry_job, excluded=set(provenance_job_ids)):
            if len(candidates) >= _RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT:
                break
            event_batch = _event_runtime_root_candidates(
                self.store,
                job_id,
                candidate_budget=_RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT - len(candidates),
            )
            candidates.extend(event_batch.candidates)
            event_candidate_returned_count += event_batch.event_candidate_returned_count
            event_candidate_total_count += event_batch.event_candidate_total_count
            event_candidate_omitted_count += event_batch.event_candidate_omitted_count
            event_rows_scanned_count += event_batch.event_rows_scanned_count
            event_rows_total_count += event_batch.event_rows_total_count
            event_rows_omitted_count += event_batch.event_rows_omitted_count
            manual_retry_event_rows_ignored += event_batch.manual_retry_event_rows_ignored

        env_candidate = _runtime_root_env_candidate()
        if env_candidate:
            candidates.append(_RuntimeRootCandidate("runtime_config:environment", env_candidate))
        return _RuntimeRootCandidateBatch(
            candidates=candidates,
            event_candidate_returned_count=event_candidate_returned_count,
            event_candidate_total_count=event_candidate_total_count,
            event_candidate_omitted_count=event_candidate_omitted_count,
            event_rows_scanned_count=event_rows_scanned_count,
            event_rows_total_count=event_rows_total_count,
            event_rows_omitted_count=event_rows_omitted_count,
            manual_retry_event_rows_ignored=manual_retry_event_rows_ignored,
        )

    def _record_retry_submission_failure(self, retry_job: PipelineJob, error: Exception) -> None:
        error_message = _safe_error_message(str(getattr(error, "message", None) or error))
        retry_job.status = "submission_failed"
        retry_job.error_code = _retry_submission_error_code(error)
        retry_job.error_message = error_message
        retry_job.finished_at = datetime.now(UTC)
        retry_job.updated_at = retry_job.finished_at
        self.store.session.add(retry_job)
        details: dict[str, Any] = {
            "trigger": "manual",
            "error_code": retry_job.error_code,
            "error_message": error_message,
        }
        runtime_root_resolution = _runtime_root_resolution_from_error(error)
        if runtime_root_resolution is not None:
            details["runtime_root_resolution"] = runtime_root_resolution
        runtime_root_contract = _runtime_root_contract_from_error(error)
        if runtime_root_contract is not None:
            details["runtime_root_contract"] = runtime_root_contract
        self.store.insert_event(
            entity_type="pipeline_job",
            entity_id=retry_job.job_id,
            event_type="submission",
            status_from="pending",
            status_to="submission_failed",
            message=f"Manual retry submission failed: {error_message}",
            details=details,
            commit=False,
        )

    def _record_retry_submission_success(
        self,
        retry_job: PipelineJob,
        submitted_payload: dict[str, Any],
        *,
        runtime_root_resolution: dict[str, Any] | None = None,
        runtime_root_contract: dict[str, str] | None = None,
    ) -> None:
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
        details: dict[str, Any] = {
            "trigger": "manual",
            "slurm_job_id": retry_job.slurm_job_id,
            "gateway_status": _gateway_status(submitted_payload),
        }
        if runtime_root_resolution is not None:
            details["runtime_root_resolution"] = runtime_root_resolution
        if runtime_root_contract is not None:
            details["runtime_root_contract"] = _redacted_mapping(runtime_root_contract)
        self.store.insert_event(
            entity_type="pipeline_job",
            entity_id=retry_job.job_id,
            event_type="submission",
            status_from="pending",
            status_to="submitted",
            message=f"Manual retry submitted as Slurm job {retry_job.slurm_job_id}.",
            details=details,
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


def _retry_submission_manifest(
    retry_job: _RetrySubmissionJob,
    *,
    model_id: str,
    runtime_root_fields: Mapping[str, str] | None = None,
) -> dict[str, Any]:
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
    if runtime_root_fields:
        manifest.update(runtime_root_fields)
    cycle_identity = _source_cycle_identity(retry_job.cycle_id)
    if retry_job.job_type == DOWNLOAD_SOURCE_CYCLE_JOB_TYPE and cycle_identity is not None:
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
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return "SBATCH_SUBMISSION_FAILED"


def _safe_error_message(message: str) -> str:
    redacted = redact_payload(message)
    return redacted if isinstance(redacted, str) else str(redacted)


def _attach_retry_runtime_root_resolution(error: Exception, evidence: dict[str, Any]) -> None:
    try:
        setattr(error, "retry_runtime_root_resolution", evidence)
    except Exception:
        return


def _attach_retry_runtime_root_contract(error: Exception, contract: dict[str, str]) -> None:
    try:
        setattr(error, "retry_runtime_root_contract", contract)
    except Exception:
        return


def _runtime_root_resolution_from_error(error: Exception) -> dict[str, Any] | None:
    evidence = getattr(error, "retry_runtime_root_resolution", None)
    if isinstance(evidence, Mapping):
        return _redacted_mapping(evidence)
    details = getattr(error, "details", None)
    if isinstance(details, Mapping):
        nested = details.get("runtime_root_resolution")
        if isinstance(nested, Mapping):
            return _redacted_mapping(nested)
    return None


def _runtime_root_contract_from_error(error: Exception) -> dict[str, Any] | None:
    contract = getattr(error, "retry_runtime_root_contract", None)
    if isinstance(contract, Mapping):
        return _redacted_mapping(contract)
    return None


def _retry_provenance_job_ids(store: PipelineStore, job_id: str) -> list[str]:
    job_ids: list[str] = []
    seen: set[str] = set()
    current: str | None = job_id
    for _ in range(16):
        if not current or current in seen:
            break
        seen.add(current)
        job_ids.append(current)
        current = _retry_previous_job_id(store, current)
    return job_ids


def _retry_previous_job_id(store: PipelineStore, job_id: str) -> str | None:
    statement = (
        select(PipelineEvent)
        .where(PipelineEvent.entity_type == "pipeline_job")
        .where(PipelineEvent.entity_id == job_id)
        .where(PipelineEvent.event_type == "retry")
        .order_by(PipelineEvent.event_id.desc())
    )
    for event in store.session.scalars(statement):
        details = event.details if isinstance(event.details, Mapping) else {}
        previous_job_id = details.get("previous_job_id")
        if isinstance(previous_job_id, str) and previous_job_id.strip():
            return previous_job_id.strip()
    return None


def _same_run_source_submission_job_ids(
    store: PipelineStore,
    retry_job: _RetrySubmissionJob,
    *,
    excluded: set[str],
) -> list[str]:
    if not retry_job.run_id:
        return []
    statement = (
        select(PipelineJob)
        .where(PipelineJob.run_id == retry_job.run_id)
        .where(PipelineJob.job_type == DOWNLOAD_SOURCE_CYCLE_JOB_TYPE)
        .order_by(PipelineJob.submitted_at.asc(), PipelineJob.created_at.asc(), PipelineJob.job_id.asc())
    )
    job_ids: list[str] = []
    for job in store.session.scalars(statement):
        if job.job_id in excluded or job.job_id == retry_job.job_id:
            continue
        if retry_job.cycle_id and job.cycle_id and job.cycle_id != retry_job.cycle_id:
            continue
        if job.manual_retry_marker:
            continue
        job_ids.append(job.job_id)
    return job_ids


def _event_runtime_root_candidates(
    store: PipelineStore,
    job_id: str,
    *,
    candidate_budget: int,
) -> _RuntimeRootCandidateBatch:
    row_filter = (
        (PipelineEvent.entity_type == "pipeline_job")
        & (PipelineEvent.entity_id == job_id)
        & (PipelineEvent.event_type == "submission")
    )
    event_rows_total_count = int(
        store.session.execute(select(func.count()).select_from(PipelineEvent).where(row_filter)).scalar_one() or 0
    )
    if _pipeline_job_is_manual_retry(store, job_id):
        return _RuntimeRootCandidateBatch(
            candidates=[],
            event_rows_total_count=event_rows_total_count,
            manual_retry_event_rows_ignored=event_rows_total_count,
        )

    statement = (
        select(PipelineEvent)
        .where(row_filter)
        .order_by(PipelineEvent.event_id.desc())
        .limit(_RUNTIME_ROOT_EVENT_ROW_SCAN_LIMIT)
    )
    candidates: list[_RuntimeRootCandidate] = []
    event_candidate_total_count = 0
    manual_retry_event_rows_ignored = 0
    events = list(store.session.scalars(statement))
    for event in events:
        details = event.details if isinstance(event.details, Mapping) else {}
        if _event_details_is_manual_retry_submission(details):
            manual_retry_event_rows_ignored += 1
            continue

        event_source = f"pipeline_event:{event.event_type}:{event.event_id}"
        for path in (
            ("runtime_root_contract",),
            ("submission_manifest",),
            ("submitted_manifest",),
            ("request_manifest",),
            ("slurm_submission_manifest",),
            ("manifest",),
            ("gateway_response", "manifest"),
            ("slurm", "manifest"),
        ):
            candidate = _mapping_at(details, path)
            if candidate and _has_runtime_root_field(candidate):
                event_candidate_total_count += 1
                if len(candidates) < candidate_budget:
                    candidates.append(_RuntimeRootCandidate(f"{event_source}:{'.'.join(path)}", candidate))
        if _has_runtime_root_field(details):
            event_candidate_total_count += 1
            if len(candidates) < candidate_budget:
                candidates.append(_RuntimeRootCandidate(f"{event_source}:details", details))

    return _RuntimeRootCandidateBatch(
        candidates=candidates,
        event_candidate_returned_count=len(candidates),
        event_candidate_total_count=event_candidate_total_count,
        event_candidate_omitted_count=max(event_candidate_total_count - len(candidates), 0),
        event_rows_scanned_count=len(events),
        event_rows_total_count=event_rows_total_count,
        event_rows_omitted_count=max(event_rows_total_count - len(events), 0),
        manual_retry_event_rows_ignored=manual_retry_event_rows_ignored,
    )


def _pipeline_job_is_manual_retry(store: PipelineStore, job_id: str) -> bool:
    value = store.session.execute(
        select(PipelineJob.manual_retry_marker).where(PipelineJob.job_id == job_id)
    ).scalar_one_or_none()
    return value is True


def _event_details_is_manual_retry_submission(details: Mapping[str, Any]) -> bool:
    return details.get("trigger") == "manual" or details.get("manual_retry_marker") is True


def _runtime_root_env_candidate() -> dict[str, str]:
    candidate: dict[str, str] = {}
    workspace_root = _runtime_root_value(os.getenv("WORKSPACE_ROOT"))
    object_store_root = _runtime_root_value(os.getenv("OBJECT_STORE_ROOT"))
    object_store_prefix = os.getenv("OBJECT_STORE_PREFIX")
    published_artifact_root = _runtime_root_value(os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT"))
    published_artifact_uri_prefix = _runtime_root_value(os.getenv("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX"))
    if workspace_root is not None:
        candidate["workspace_dir"] = workspace_root
    if object_store_root is not None:
        candidate["object_store_root"] = object_store_root
    if object_store_prefix is not None:
        candidate["object_store_prefix"] = str(object_store_prefix)
    if published_artifact_root is not None:
        candidate["published_artifact_root"] = published_artifact_root
        candidate["published_artifact_uri_prefix"] = published_artifact_uri_prefix or "published://"
    elif published_artifact_uri_prefix is not None:
        candidate["published_artifact_uri_prefix"] = published_artifact_uri_prefix
    return candidate


def _resolve_runtime_root_candidate(
    source: str,
    candidate: Mapping[str, Any],
) -> _RuntimeRootCandidateResolution:
    resolved: dict[str, tuple[str, str]] = {}
    rejected: list[dict[str, str]] = []
    comparable_local_roots: dict[str, str] = {}
    secret_rejected = False
    unsafe_rejected = False

    for root_field in _RUNTIME_ROOT_FIELDS:
        if root_field not in candidate:
            continue
        value = _runtime_root_value(candidate.get(root_field))
        if value is None:
            continue

        secret_reason = secret_manifest_value_reason(value)
        if secret_reason is not None:
            secret_rejected = True
            rejected.append(_runtime_root_rejection(root_field, source, secret_reason, value))
            continue

        if root_field in _LOCAL_RUNTIME_ROOT_FIELDS and not _is_uri_style_value(value):
            safety = _local_runtime_root_safety(value)
            if safety[0] is None:
                unsafe_rejected = True
                rejected.append(_runtime_root_rejection(root_field, source, safety[1], value))
                continue
            comparable_local_roots[root_field] = safety[0]

        resolved[root_field] = (value, source)

    workspace_value = resolved.get("workspace_dir", ("", source))[0]
    object_store_value = resolved.get("object_store_root", ("", source))[0]
    workspace_comparable = comparable_local_roots.get("workspace_dir")
    object_store_comparable = comparable_local_roots.get("object_store_root")
    object_store_matches_workspace = bool(
        workspace_value and object_store_value and workspace_value == object_store_value
    )
    object_store_matches_workspace = object_store_matches_workspace or bool(
        workspace_comparable and object_store_comparable and workspace_comparable == object_store_comparable
    )
    if object_store_matches_workspace:
        unsafe_rejected = True
        rejected.append(
            _runtime_root_rejection(
                "object_store_root",
                source,
                "resolves_to_workspace_dir",
                object_store_value,
            )
        )
        resolved.pop("object_store_root", None)

    if "object_store_prefix" not in resolved:
        resolved["object_store_prefix"] = ("", f"{source}:OBJECT_STORE_PREFIX.default_empty")

    missing = [field for field in _REQUIRED_RUNTIME_ROOT_FIELDS if field not in resolved]
    return _RuntimeRootCandidateResolution(
        resolved=resolved,
        missing=missing,
        rejected=rejected,
        secret_rejected=secret_rejected,
        unsafe_rejected=unsafe_rejected,
    )


def _runtime_root_rejection(field: str, source: str, reason: str, value: str) -> dict[str, str]:
    return {
        "field": field,
        "source": source,
        "reason": reason,
        "value": _bounded_redacted_text(value),
    }


def _is_uri_style_value(value: str) -> bool:
    return bool(_URI_STYLE_RE.match(value))


def _local_runtime_root_safety(value: str) -> tuple[str | None, str]:
    path = Path(value).expanduser()
    if not path.is_absolute():
        reason = "parent_traversal_local_root" if ".." in path.parts else "relative_local_root"
        return None, reason
    try:
        return str(path.resolve(strict=False)), "ok"
    except OSError:
        return None, "unresolvable_local_root"


def _mapping_at(details: Mapping[str, Any], path: tuple[str, ...]) -> Mapping[str, Any] | None:
    value: Any = details
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value if isinstance(value, Mapping) else None


def _has_runtime_root_field(value: Mapping[str, Any]) -> bool:
    return any(field in value for field in _RUNTIME_ROOT_FIELDS)


def _runtime_root_value(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _runtime_root_resolution_evidence(
    retry_job: _RetrySubmissionJob,
    *,
    resolved: Mapping[str, tuple[str, str]],
    missing: list[str],
    rejected: list[dict[str, str]],
    rejected_total_count: int,
    candidate_batch: _RuntimeRootCandidateBatch,
) -> dict[str, Any]:
    resolved_evidence = {}
    for root_field in _RUNTIME_ROOT_FIELDS:
        item = resolved.get(root_field)
        if item is None:
            continue
        value, source = item
        resolved_evidence[root_field] = {
            "present": True,
            "source": _bounded_redacted_text(source),
            "value": _bounded_redacted_text(value),
        }
    if "object_store_root" in resolved and "workspace_dir" in resolved:
        resolved_evidence["object_store_root"]["same_as_workspace"] = (
            resolved["object_store_root"][0] == resolved["workspace_dir"][0]
        )
    rejected_omitted_count = max(rejected_total_count - len(rejected), 0)
    return _redacted_mapping(
        {
            "job_type": retry_job.job_type,
            "retry_job_id": retry_job.job_id,
            "previous_job_id": retry_job.previous_job_id,
            "cycle_id": retry_job.cycle_id,
            "required": list(_REQUIRED_RUNTIME_ROOT_FIELDS),
            "resolved": resolved_evidence,
            "missing": missing,
            "candidate_counts": {
                "event_candidates_returned": candidate_batch.event_candidate_returned_count,
                "event_candidates_total": candidate_batch.event_candidate_total_count,
                "event_candidates_omitted": candidate_batch.event_candidate_omitted_count,
                "event_candidate_limit": _RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT,
                "event_rows_scanned": candidate_batch.event_rows_scanned_count,
                "event_rows_total": candidate_batch.event_rows_total_count,
                "event_rows_omitted": candidate_batch.event_rows_omitted_count,
                "event_row_scan_limit": _RUNTIME_ROOT_EVENT_ROW_SCAN_LIMIT,
                "manual_retry_event_rows_ignored": candidate_batch.manual_retry_event_rows_ignored,
            },
            "rejected": [
                {
                    "field": _bounded_redacted_text(item.get("field", "")),
                    "source": _bounded_redacted_text(item.get("source", "")),
                    "reason": _bounded_redacted_text(item.get("reason", "")),
                    "value": _bounded_redacted_text(item.get("value", "")),
                }
                for item in rejected
            ],
            "rejected_total_count": rejected_total_count,
            "rejected_omitted_count": rejected_omitted_count,
            "rejected_limit": _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT,
            "published_fields_available": [
                field for field in _PUBLISHED_RUNTIME_ROOT_FIELDS if field in resolved
            ],
        }
    )


def _bounded_redacted_text(value: Any) -> str:
    redacted = redact_payload(str(value))
    text_value = redacted if isinstance(redacted, str) else str(redacted)
    if len(text_value) <= _ROOT_EVIDENCE_VALUE_MAX_LENGTH:
        return text_value
    return f"{text_value[: _ROOT_EVIDENCE_VALUE_MAX_LENGTH - 3]}..."


def _redacted_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_payload(dict(value))
    return dict(redacted) if isinstance(redacted, Mapping) else {}
