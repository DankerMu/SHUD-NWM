from __future__ import annotations

import json
import os
import re
import stat
import threading
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from packages.common.auth_policy import PolicyDecision, require_policy_evidence, trusted_internal_policy_decision
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    list_directory_no_follow_limited,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from packages.common.source_identity import normalize_source_id
from services.orchestrator import chain_repository_state
from services.orchestrator.chain_repository import (
    ACTIVE_HYDRO_STATUSES,
    COMPLETED_HYDRO_STATUSES,
    DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
)
from services.orchestrator.chain_source_cycle import _datetime_sort_key
from services.orchestrator.chain_types import ForcingContext, ModelContext, OrchestratorError
from services.orchestrator.retry import (
    _REQUIRED_RUNTIME_ROOT_FIELDS,
    _RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT,
    _RUNTIME_ROOT_EVENT_ROW_SCAN_LIMIT,
    _RUNTIME_ROOT_FIELDS,
    _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT,
    ACTIVE_RETRY_STATUSES,
    DOWNLOAD_SOURCE_CYCLE_JOB_TYPE,
    DURABLE_HYDRO_SUCCESS_STATUSES,
    MANUAL_RETRY_SOURCE_STATUSES,
    PARTIAL_OR_FAILED_HYDRO_STATUSES,
    RETRY_RUNTIME_ROOTS_SECRET_BEARING,
    RETRY_RUNTIME_ROOTS_UNRESOLVED,
    RETRY_RUNTIME_ROOTS_UNSAFE,
    TERMINAL_SUCCESS_RETRY_STATUSES,
    RetryConfig,
    RetryConflictError,
    RetryError,
    RetryNotFoundError,
    _attach_retry_runtime_root_contract,
    _attach_retry_runtime_root_resolution,
    _event_details_is_manual_retry_submission,
    _has_runtime_root_field,
    _mapping_at,
    _resolve_runtime_root_candidate,
    _retry_submission_manifest,
    _RetryRuntimeRootResolutionError,
    _RetrySubmissionJob,
    _runtime_root_contract_from_error,
    _runtime_root_env_candidate,
    _runtime_root_resolution_evidence,
    _runtime_root_resolution_from_error,
    _RuntimeRootCandidate,
    _RuntimeRootCandidateBatch,
    _safe_error_message,
    classify_failure,
    compute_backoff_seconds,
)
from services.orchestrator.scheduler_file_providers import (
    _public_raw_manifest_evidence,
    _sanitize_file_provider_evidence_scalar,
)
from services.orchestrator.scheduler_state import _ensure_utc, _evidence_safe, _format_utc
from services.slurm_gateway.models import SubmitJobRequest
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION = "nhms.scheduler.file_orchestration_journal.v1"
FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION = "nhms.scheduler.file_orchestration_latest.v1"
MAX_FILE_JOURNAL_JSON_BYTES = 16 * 1024 * 1024
MAX_FILE_JOURNAL_RECORDS = 100_000
MAX_FILE_JOURNAL_DISCOVERED_FILES = 100_000
MAX_FILE_JOURNAL_SCAN_DEPTH = 32
MAX_FILE_JOURNAL_JSON_DEPTH = 64
MAX_FILE_JOURNAL_JSON_NODES = 300_000
MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS = 255
_LATEST_REPLAY_ORDER_SENTINEL = MAX_FILE_JOURNAL_RECORDS + 1
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_FORECAST_RUN_ID_RE = re.compile(r"^fcst_([^_]+)_(\d{10})_(.+)$")
_CYCLE_RUN_ID_RE = re.compile(r"^cycle_([^_]+)_(\d{10})$")
_CYCLE_COHORT_RUN_ID_RE = re.compile(r"^cycle_([^_]+)_(\d{10})(?:_.+)?$")
_REPLAY_SEQUENCE_FIELD = "_file_journal_replay_sequence"
_REPLAY_ORDER_FIELD = "_file_journal_replay_order"
_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS = (
    "slurm_job_id",
    "array_task_id",
    "model_id",
    "status",
    "stage",
    "idempotency_key",
    "candidate_id",
    "submitted_at",
    "started_at",
    "finished_at",
    "exit_code",
    "retry_count",
    "manual_retry_marker",
    "error_code",
    "error_message",
    "log_uri",
)
_RUNTIME_ROOT_EVENT_CANDIDATE_PATHS = (
    ("runtime_root_contract",),
    ("submission_manifest",),
    ("submitted_manifest",),
    ("request_manifest",),
    ("slurm_submission_manifest",),
    ("manifest",),
    ("gateway_response", "manifest"),
    ("slurm", "manifest"),
)

TERMINAL_PIPELINE_STATUSES = {
    "succeeded",
    "partially_failed",
    "failed",
    "cancelled",
    "submission_failed",
    "permanently_failed",
}

__all__ = (
    "FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION",
    "FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION",
    "FileJournalRetryService",
    "FileOrchestrationJournalError",
    "FileOrchestrationJournalRepository",
)


class FileOrchestrationJournalError(RuntimeError):
    def __init__(self, reason: str, *, field: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field
        self.evidence = dict(evidence or {})


@dataclass
class _CycleRows:
    hydro_run: dict[str, Any] | None = None
    forecast_cycle: dict[str, Any] | None = None
    forcing_version: dict[str, Any] | None = None
    model_context: dict[str, Any] | None = None
    pipeline_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    pipeline_events: list[dict[str, Any]] = field(default_factory=list)
    replay: dict[str, Any] = field(default_factory=dict)


@dataclass
class _RecordBudget:
    limit: int
    field: str
    count: int = 0

    def consume(self, amount: int = 1) -> None:
        self.count += amount
        if self.count > self.limit:
            raise FileOrchestrationJournalError("file_journal_record_limit_exceeded", field=self.field)


class FileOrchestrationJournalRepository:
    """Read-side file implementation for scheduler orchestration state."""

    def __init__(
        self,
        journal_root: str | Path,
        *,
        max_bytes: int = MAX_FILE_JOURNAL_JSON_BYTES,
        max_files: int = MAX_FILE_JOURNAL_DISCOVERED_FILES,
        max_depth: int = MAX_FILE_JOURNAL_SCAN_DEPTH,
        max_json_nodes: int = MAX_FILE_JOURNAL_JSON_NODES,
        max_json_depth: int = MAX_FILE_JOURNAL_JSON_DEPTH,
        max_records: int = MAX_FILE_JOURNAL_RECORDS,
    ) -> None:
        self.root = Path(journal_root)
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.max_depth = int(max_depth)
        self.max_json_nodes = int(max_json_nodes)
        self.max_json_depth = int(max_json_depth)
        self.max_records = int(max_records)
        self._write_lock = threading.Lock()

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=None)
        except FileOrchestrationJournalError:
            return True
        return any(_job_is_active(job) for job in rows.pipeline_jobs.values())

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return True
        hydro_run = rows.hydro_run
        if _row_matches_candidate(hydro_run, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id):
            if str(hydro_run.get("status") or "") in ACTIVE_HYDRO_STATUSES:
                return True
        return any(
            _job_is_active(job)
            and _job_matches_candidate(job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
            for job in rows.pipeline_jobs.values()
        )

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return False
        hydro_run = rows.hydro_run
        if not _row_matches_candidate(
            hydro_run,
            source_id=canonical_source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        ):
            return False
        return str(hydro_run.get("status") or "") in COMPLETED_HYDRO_STATUSES

    def active_slurm_jobs(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        limit: int = DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
    ) -> list[dict[str, Any]]:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return [
                _public_scheduler_row(
                    {
                        "job_id": "file_journal_read_blocked",
                        "cycle_id": _blocked_cycle_id(source_id, cycle_time),
                        "model_id": model_id,
                        "status": "running",
                        "stage": "file_journal_read",
                        "slurm_job_id": "unknown_after_attempt",
                    }
                )
            ]
        jobs = [
            _public_scheduler_row(job)
            for job in rows.pipeline_jobs.values()
            if job.get("slurm_job_id") not in (None, "")
            and _job_is_active(job)
            and _job_matches_candidate(job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        ]
        jobs.sort(key=_db_compatible_pipeline_job_order_key)
        return jobs[: max(int(limit), 1)]

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
        retry_limit: int | None = None,
        job_limit: int = DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
        event_limit: int = DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    ) -> dict[str, Any] | None:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            canonical_run_id = _canonical_candidate_run_id(
                run_id,
                source_id=canonical_source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
            canonical_forcing_version_id = _canonical_forcing_version_id(
                forcing_version_id,
                source_id=canonical_source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
            canonical_candidate_id = _canonical_candidate_id(
                candidate_id,
                source_id=canonical_source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError as error:
            return _file_journal_blocked_candidate_state(
                error,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                run_id=run_id,
                forcing_version_id=forcing_version_id,
                candidate_id=candidate_id,
                retry_limit=retry_limit,
                job_limit=job_limit,
                event_limit=event_limit,
            )
        state = chain_repository_state.candidate_state_from_rows(
            source_id=canonical_source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            run_id=canonical_run_id,
            forcing_version_id=canonical_forcing_version_id,
            candidate_id=canonical_candidate_id,
            hydro_run=rows.hydro_run,
            pipeline_jobs=[_public_scheduler_row(job) for job in rows.pipeline_jobs.values()],
            pipeline_events=[_public_scheduler_row(event) for event in rows.pipeline_events],
            forcing_version=rows.forcing_version,
            forecast_cycle=rows.forecast_cycle,
            retry_limit=retry_limit,
            job_limit=job_limit,
            event_limit=event_limit,
        )
        if state is None:
            return None
        return _public_candidate_state(state)

    def load_model_context(self, model_id: str) -> ModelContext:
        try:
            model_context = self._model_context(model_id)
            if model_context is None:
                raise OrchestratorError("MODEL_NOT_FOUND", f"model context not found in file journal: {model_id}")
            return _model_context_from_mapping(model_context, model_id=model_id)
        except FileOrchestrationJournalError as error:
            raise OrchestratorError(
                "FILE_JOURNAL_READ_BLOCKED",
                f"model context blocked by file journal state: {error.reason}",
            ) from error

    def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
            if rows.forcing_version is None:
                forcing_context = self._forcing_context(
                    source_id=canonical_source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
            else:
                forcing_context = rows.forcing_version
            if forcing_context is None:
                return ForcingContext(None, None)
            return _forcing_context_from_mapping(forcing_context)
        except FileOrchestrationJournalError as error:
            raise OrchestratorError(
                "FILE_JOURNAL_READ_BLOCKED",
                f"forcing context blocked by file journal state: {error.reason}",
            ) from error

    def query_candidate_state(self, idempotency_key: str) -> dict[str, Any] | None:
        try:
            for job in self._iter_pipeline_job_records():
                if str(job.get("idempotency_key") or "") == idempotency_key:
                    return _public_scheduler_row(job)
        except FileOrchestrationJournalError as error:
            return _blocked_query_job(error, idempotency_key=idempotency_key)
        return None

    def _candidate_job_for_idempotency_unlocked(self, idempotency_key: str) -> dict[str, Any] | None:
        for job in self._iter_pipeline_job_records():
            if str(job.get("idempotency_key") or "") == idempotency_key:
                return dict(job)
        return None

    def get_pipeline_job(self, job_id: str) -> dict[str, Any] | None:
        try:
            job = self._pipeline_job_for_id_unlocked(job_id)
            if job is not None:
                return _public_scheduler_row(job)
        except FileOrchestrationJournalError as error:
            return _blocked_query_job(error, job_id=job_id)
        return None

    def _pipeline_job_for_id_unlocked(self, job_id: str) -> dict[str, Any] | None:
        expected_job_id = _safe_segment(job_id)
        for job in self._iter_pipeline_job_records(include_direct=False):
            if str(job.get("job_id") or "") == expected_job_id:
                return dict(job)
        direct_job = self._direct_pipeline_job_record(expected_job_id)
        return dict(direct_job) if direct_job is not None else None

    def query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        try:
            jobs = [
                _public_scheduler_row(job)
                for job in self._iter_pipeline_job_records()
                if str(job.get("cycle_id") or "") == cycle_id
            ]
            jobs.sort(key=_db_compatible_pipeline_job_order_key)
            return jobs
        except FileOrchestrationJournalError as error:
            return [_blocked_query_job(error, cycle_id=cycle_id)]

    def query_pipeline_jobs_by_run(self, run_id: str) -> list[dict[str, Any]]:
        try:
            jobs = [
                _public_scheduler_row(job)
                for job in self._iter_pipeline_job_records()
                if str(job.get("run_id") or "") == run_id
            ]
            jobs.sort(key=_db_compatible_pipeline_job_order_key)
            return jobs
        except FileOrchestrationJournalError as error:
            return [_blocked_query_job(error, run_id=run_id)]

    def query_pipeline_job_by_slurm_id(self, slurm_job_id: str) -> dict[str, Any] | None:
        try:
            for job in self._iter_pipeline_job_records():
                if str(job.get("slurm_job_id") or "") == slurm_job_id:
                    return _public_scheduler_row(job)
        except FileOrchestrationJournalError as error:
            return _blocked_query_job(error, slurm_job_id=slurm_job_id)
        return None

    @property
    def supports_writes(self) -> bool:
        return True

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None).forecast_cycle
            if existing is not None:
                row = dict(existing)
                changed = False
                for key, value in (
                    ("cycle_id", _cycle_id_for_file_source(source_id, cycle_time)),
                    ("source_id", source_id),
                    ("cycle_time", _format_utc(cycle_time)),
                    ("issue_time", _format_utc(cycle_time)),
                ):
                    if row.get(key) in (None, ""):
                        row[key] = value
                        changed = True
                if not changed:
                    return _public_scheduler_row(row)
                row["updated_at"] = _format_utc(_utcnow())
                self._append_validated_record_unlocked(
                    "forecast_cycle",
                    row,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                return _public_scheduler_row(row)
            row = {
                "cycle_id": _cycle_id_for_file_source(source_id, cycle_time),
                "source_id": source_id,
                "cycle_time": _format_utc(cycle_time),
                "issue_time": _format_utc(cycle_time),
                "status": "discovered",
                "created_at": _format_utc(_utcnow()),
                "updated_at": _format_utc(_utcnow()),
            }
            self._append_validated_record_unlocked(
                "forecast_cycle",
                row,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            return _public_scheduler_row(row)

    def append_historical_forecast_cycle(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        source_id = _required_source_id(record, "source_id")
        cycle_time = _parse_cycle_time_field(record, "cycle_time")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None).forecast_cycle
            if existing is not None:
                return _public_scheduler_row(existing)
            self._append_validated_record_unlocked(
                "forecast_cycle",
                record,
                source_id=source_id,
                cycle_time=cycle_time,
            )
        return _public_scheduler_row(record)

    def create_hydro_run(self, context: Any, manifest: dict[str, Any]) -> dict[str, Any]:
        init_state = manifest.get("initial_state") if isinstance(manifest.get("initial_state"), Mapping) else {}
        row = {
            "run_id": str(context.run_id),
            "run_type": manifest.get("run_type", "forecast"),
            "scenario_id": manifest["scenario_id"],
            "model_id": str(context.model_id),
            "basin_version_id": str(context.basin_version_id),
            "forcing_version_id": str(context.forcing_version_id),
            "init_state_id": getattr(context, "init_state_id", None) or init_state.get("state_id"),
            "source_id": _normalize_file_source_id(context.source_id, field="source_id"),
            "cycle_time": _format_utc(context.cycle_time),
            "start_time": _format_utc(context.start_time),
            "end_time": _format_utc(context.end_time),
            "status": "created",
            "run_manifest_uri": context.run_manifest_uri,
            "output_uri": context.output_uri,
            "log_uri": context.log_uri,
            "created_at": _format_utc(_utcnow()),
            "updated_at": _format_utc(_utcnow()),
        }
        return self._write_hydro_run(row, retriable_only=True)

    def create_hydro_run_from_basin(self, basin: Mapping[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        model = _mapping_value(manifest, "model")
        forcing = _optional_mapping_value(manifest, "forcing")
        outputs = _optional_mapping_value(manifest, "outputs")
        initial_state = _optional_mapping_value(manifest, "initial_state")
        cycle_time = parse_cycle_time(str(manifest["cycle_time"]))
        row = {
            "run_id": str(manifest["run_id"]),
            "run_type": manifest.get("run_type", "forecast"),
            "scenario_id": manifest["scenario_id"],
            "model_id": str(model["model_id"]),
            "basin_version_id": str(model["basin_version_id"]),
            "forcing_version_id": forcing.get("forcing_version_id"),
            "init_state_id": initial_state.get("state_id") or basin.get("init_state_id"),
            "source_id": _normalize_file_source_id(
                manifest.get("source_id") or basin.get("source_id"),
                field="source_id",
            ),
            "cycle_time": _format_utc(cycle_time),
            "start_time": _format_utc(_coerce_datetime(manifest["start_time"], field="start_time")),
            "end_time": _format_utc(_coerce_datetime(manifest["end_time"], field="end_time")),
            "status": "created",
            "run_manifest_uri": outputs.get("run_manifest_uri"),
            "output_uri": outputs.get("output_uri"),
            "log_uri": outputs.get("log_uri"),
            "created_at": _format_utc(_utcnow()),
            "updated_at": _format_utc(_utcnow()),
        }
        try:
            return self._write_hydro_run(row, retriable_only=True)
        except OrchestratorError as error:
            if error.error_code != "HYDRO_RUN_NOT_RETRIABLE":
                raise
            existing = self._hydro_run_for(row["run_id"])
            if existing is None:
                raise OrchestratorError(
                    "HYDRO_RUN_NOT_FOUND",
                    f"hydro_run not found after conflict: {row['run_id']}",
                ) from error
            return _public_scheduler_row(existing)

    def append_historical_hydro_run(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        source_id = _required_source_id(record, "source_id")
        cycle_time = _parse_cycle_time_field(record, "cycle_time")
        model_id = _required_safe_identity(record, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._hydro_run_for(str(record["run_id"]))
            if existing is not None:
                return _public_scheduler_row(existing)
            self._append_validated_record_unlocked(
                "hydro_run",
                record,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=model_id,
            )
        return _public_scheduler_row(record)

    def update_hydro_run_status(
        self,
        run_id: str,
        status: str,
        *,
        slurm_job_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        try:
            source_id, cycle_time = _source_cycle_from_file_run_id(run_id)
        except FileOrchestrationJournalError as error:
            raise OrchestratorError("HYDRO_RUN_NOT_FOUND", f"hydro_run not found: {run_id}") from error
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._hydro_run_for(run_id)
            if existing is None:
                raise OrchestratorError("HYDRO_RUN_NOT_FOUND", f"hydro_run not found: {run_id}")
            row = dict(existing)
            row.update({"status": status, "updated_at": _format_utc(_utcnow())})
            for key, value in (
                ("slurm_job_id", slurm_job_id),
            ):
                if value is not None:
                    row[key] = value
            if status in {"pending", "created", "succeeded", "complete", "parsed", "frequency_done", "published"}:
                row["error_code"] = error_code
                row["error_message"] = error_message
            else:
                if error_code is not None:
                    row["error_code"] = error_code
                if error_message is not None:
                    row["error_message"] = error_message
            model_id = _required_safe_identity(row, "model_id")
            self._append_validated_record_unlocked(
                "hydro_run",
                row,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=model_id,
            )
            return _public_scheduler_row(row)

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        row = self._pipeline_job_row(record)
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        model_id = _optional_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(str(row["job_id"]))
            if existing is not None:
                explicit_fields = set(record)
                incoming = row
                row = dict(existing)
                for key in _PIPELINE_JOB_UPSERT_MUTABLE_FIELDS:
                    if key in explicit_fields:
                        row[key] = incoming[key]
                row["updated_at"] = _format_utc(_utcnow())
                model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def append_historical_pipeline_job(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        row = self._pipeline_job_row(dict(record))
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        model_id = _optional_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(str(row["job_id"]))
            if existing is not None:
                return _public_scheduler_row(existing)
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
        row = self._pipeline_job_row(
            {
                **record,
                "status": record.get("status", "reserved"),
                "submitted_at": None,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "error_code": None,
                "error_message": None,
                "log_uri": None,
            }
        )
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        model_id = _optional_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            if self._pipeline_job_conflicts_unlocked(row):
                return None
            return self._write_pipeline_job_unlocked(row, exclusive_direct=True, model_id=model_id)

    def reclaim_pipeline_job_reservation(self, record: dict[str, Any]) -> dict[str, Any] | None:
        request_row = self._pipeline_job_row(record)
        idempotency_key = str(request_row["idempotency_key"])
        source_id = _source_id_from_job(request_row)
        cycle_time = _cycle_time_from_job(request_row)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._candidate_job_for_idempotency_unlocked(idempotency_key)
            matched_by_key = existing is not None
            if existing is None and request_row.get("job_id") not in (None, ""):
                existing = self._pipeline_job_for_id_unlocked(str(request_row["job_id"]))
            if existing is None:
                return None
            existing_status = str(existing.get("status") or "")
            if matched_by_key:
                if existing.get("slurm_job_id") not in (None, "") or existing_status not in {
                    "submission_failed",
                    "reservation_lost",
                }:
                    return None
            else:
                if (
                    existing.get("idempotency_key") not in (None, "")
                    or existing.get("slurm_job_id") not in (None, "")
                    or existing_status != "pending"
                    or self._candidate_job_for_idempotency_unlocked(idempotency_key) is not None
                ):
                    return None
            row = dict(existing)
            row.update(
                {
                    "status": "reserved",
                    "slurm_job_id": None,
                    "array_task_id": None,
                    "submitted_at": None,
                    "started_at": None,
                    "finished_at": None,
                    "exit_code": None,
                    "error_code": None,
                    "error_message": None,
                    "idempotency_key": idempotency_key,
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            for key in ("run_id", "cycle_id", "model_id", "stage", "candidate_id", "job_type"):
                if row.get(key) in (None, "") and request_row.get(key) not in (None, ""):
                    row[key] = request_row[key]
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def bind_pipeline_job_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> dict[str, Any] | None:
        initial = self._candidate_job_for_idempotency_unlocked(idempotency_key)
        if initial is None:
            return None
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._candidate_job_for_idempotency_unlocked(idempotency_key)
            if existing is None or existing.get("slurm_job_id") not in (None, ""):
                return None
            row = dict(existing)
            row.update(
                {
                    "slurm_job_id": str(slurm_job_id),
                    "status": status,
                    "submitted_at": row.get("submitted_at") or _format_utc(_utcnow()),
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            if array_task_id is not None:
                row["array_task_id"] = array_task_id
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def update_pipeline_job_status(
        self,
        job_id: str,
        status: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        initial = self._pipeline_job_for_id_unlocked(job_id)
        if initial is None:
            raise OrchestratorError("PIPELINE_JOB_NOT_FOUND", f"pipeline_job not found: {job_id}")
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(job_id)
            if existing is None:
                raise OrchestratorError("PIPELINE_JOB_NOT_FOUND", f"pipeline_job not found: {job_id}")
            previous_status = str(existing.get("status") or "") or None
            terminal_guarded = previous_status in {"succeeded", "failed", "cancelled"} and status not in {
                "partially_failed",
                "permanently_failed",
            }
            if previous_status == "permanently_failed" or terminal_guarded:
                return previous_status, _public_scheduler_row(existing)
            row = dict(existing)
            row["status"] = status
            for key, value in (
                ("started_at", started_at),
                ("finished_at", finished_at),
                ("exit_code", exit_code),
                ("log_uri", log_uri),
            ):
                if value is not None:
                    row[key] = _format_utc(value) if isinstance(value, datetime) else value
            if status in {"succeeded", "complete", "published"} and error_code is None:
                row["error_code"] = None
            elif error_code is not None:
                row["error_code"] = error_code
            if status in {"succeeded", "complete", "published"} and error_message is None:
                row["error_message"] = None
            elif error_message is not None:
                row["error_message"] = error_message
            row["updated_at"] = _format_utc(_utcnow())
            model_id = _optional_safe_identity(row, "model_id")
            return previous_status, self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def insert_pipeline_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job = self.get_pipeline_job(entity_id)
        if job is None:
            raise OrchestratorError("PIPELINE_JOB_NOT_FOUND", f"pipeline_job not found for event: {entity_id}")
        source_id = _source_id_from_job(job)
        cycle_time = _cycle_time_from_job(job)
        model_id = _optional_safe_identity(job, "model_id")
        row = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "message": message,
            "details": details or {},
            "created_at": _format_utc(_utcnow()),
        }
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            event_id = self._next_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
            row["event_id"] = event_id
            self._append_validated_record_unlocked(
                "pipeline_event",
                row,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=model_id,
                sequence=event_id,
            )
        return _public_scheduler_row(row)

    def append_historical_pipeline_event(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        entity_id = _required_safe_identity(record, "entity_id")
        job = self.get_pipeline_job(entity_id)
        if job is None:
            return None
        source_id = _source_id_from_job(job)
        cycle_time = _cycle_time_from_job(job)
        model_id = _optional_safe_identity(job, "model_id")
        row = {
            "event_id": record.get("event_id"),
            "entity_type": str(record.get("entity_type") or "pipeline_job"),
            "entity_id": entity_id,
            "event_type": str(record["event_type"]),
            "status_from": record.get("status_from"),
            "status_to": record.get("status_to"),
            "message": record.get("message"),
            "details": dict(record.get("details") or {}),
            "created_at": _optional_format_datetime(record.get("created_at"), field="created_at")
            or _format_utc(_utcnow()),
        }
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
            event_id = row.get("event_id")
            if event_id in (None, ""):
                event_id = self._next_event_id_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
                row["event_id"] = event_id
            existing = next(
                (
                    event
                    for event in rows.pipeline_events
                    if str(event.get("event_id") or "") == str(event_id)
                    and str(event.get("entity_id") or "") == entity_id
                ),
                None,
            )
            if existing is not None:
                return _public_scheduler_row(existing)
            self._append_validated_record_unlocked(
                "pipeline_event",
                row,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=model_id,
            )
        return _public_scheduler_row(row)

    def update_forecast_cycle_status(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        row = {
            "cycle_id": _cycle_id_for_file_source(source_id, cycle_time),
            "source_id": source_id,
            "cycle_time": _format_utc(cycle_time),
            "issue_time": _format_utc(cycle_time),
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
            "updated_at": _format_utc(_utcnow()),
        }
        self._append_validated_record("forecast_cycle", row, source_id=source_id, cycle_time=cycle_time)
        return _public_scheduler_row(row)

    def list_stage_statuses(
        self,
        *,
        source_id: str | None,
        cycle_time: datetime,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if source_id is None:
            return []
        try:
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError as error:
            return [_blocked_stage_status(error, source_id=source_id, cycle_time=cycle_time, model_id=model_id)]
        return [
            _public_scheduler_row(
                {
                    "stage": job.get("stage"),
                    "status": job.get("status"),
                    "job_id": job.get("job_id"),
                    "slurm_job_id": job.get("slurm_job_id"),
                    "model_id": job.get("model_id"),
                }
            )
            for job in rows.pipeline_jobs.values()
        ]

    def _cycle_rows(self, *, source_id: str, cycle_time: datetime, model_id: str | None) -> _CycleRows:
        rows = _CycleRows()
        source_id = _normalize_file_source_id(source_id, field="source_id")
        source_segment = _safe_segment(source_id)
        cycle_segment = format_cycle_time(cycle_time)
        latest_paths = self._latest_paths(source_segment, cycle_segment, model_id=model_id)
        for path in latest_paths:
            payload = self._read_optional_json(path)
            if payload is not None:
                self._apply_latest_view(
                    rows,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    expected_model_id=_safe_segment(path.stem),
                )
        for record in self._read_jsonl(self.root / "journal" / source_segment / f"{cycle_segment}.jsonl"):
            self._apply_journal_record(
                rows,
                record,
                source_id=source_id,
                cycle_time=cycle_time,
                expected_model_id=model_id,
            )
        for record in self._read_jsonl(self.root / "pipeline-events" / source_segment / f"{cycle_segment}.jsonl"):
            self._apply_journal_record(
                rows,
                record,
                source_id=source_id,
                cycle_time=cycle_time,
                expected_record_type="pipeline_event",
                expected_model_id=model_id,
            )
        for job in self._iter_direct_pipeline_job_records_for_cycle(
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        ):
            _insert_missing_by_key(rows.pipeline_jobs, job, key="job_id")
        if model_id is not None:
            rows.hydro_run = (
                rows.hydro_run
                if _row_matches_candidate(rows.hydro_run, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
                else None
            )
            rows.forcing_version = (
                rows.forcing_version
                if _row_matches_candidate(
                    rows.forcing_version,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
                else None
            )
            rows.model_context = (
                rows.model_context
                if _row_matches_candidate(
                    rows.model_context,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
                else None
            )
            rows.pipeline_jobs = {
                job_id: job
                for job_id, job in rows.pipeline_jobs.items()
                if _job_matches_candidate(job, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
            }
            rows.pipeline_events = [
                event for event in rows.pipeline_events if str(event.get("entity_id") or "") in rows.pipeline_jobs
            ]
        rows.pipeline_events = _dedupe_events(rows.pipeline_events)
        return rows

    def _latest_paths(self, source_segment: str, cycle_segment: str, *, model_id: str | None) -> list[Path]:
        directory = self.root / "latest" / source_segment / cycle_segment
        if model_id is not None:
            return [directory / f"{_safe_segment(model_id)}.json"]
        return sorted(
            _iter_regular_json_files(
                directory,
                root=self.root,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        )

    def _apply_latest_view(
        self,
        rows: _CycleRows,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        expected_model_id: str,
    ) -> None:
        _require_schema(payload, FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION)
        _require_source_cycle(payload, source_id=source_id, cycle_time=cycle_time)
        _require_model_id(payload, expected_model_id, required=True)
        latest_replay_sequence = _latest_replay_sequence(payload)
        hydro_run = _first_mapping(payload, "hydro_run", "hydro")
        if hydro_run is not None:
            _validate_hydro_run_identity(
                hydro_run,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=expected_model_id,
            )
            hydro_run = _with_latest_replay_order(hydro_run, latest_replay_sequence)
        forecast_cycle = _first_mapping(payload, "forecast_cycle")
        if forecast_cycle is not None:
            _validate_forecast_cycle_identity(forecast_cycle, source_id=source_id, cycle_time=cycle_time)
            forecast_cycle = _with_latest_replay_order(forecast_cycle, latest_replay_sequence)
        forcing_version = _first_mapping(payload, "forcing_version", "forcing_context")
        if forcing_version is not None:
            _validate_forcing_version_identity(
                forcing_version,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=expected_model_id,
            )
            forcing_version = _with_latest_replay_order(forcing_version, latest_replay_sequence)
        model_context = _first_mapping(payload, "model_context")
        if model_context is not None:
            _validate_model_context_identity(model_context, model_id=expected_model_id)
            model_context = _with_latest_replay_order(model_context, latest_replay_sequence)
        rows.hydro_run = _latest_mapping(rows.hydro_run, hydro_run)
        rows.forecast_cycle = _latest_mapping(rows.forecast_cycle, forecast_cycle)
        rows.forcing_version = _latest_mapping(
            rows.forcing_version,
            forcing_version,
        )
        rows.model_context = _latest_mapping(rows.model_context, model_context)
        for job in _record_list(payload, "pipeline_jobs", "jobs", single_key="pipeline_job"):
            _validate_pipeline_job_identity(
                job,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=expected_model_id,
            )
            job = _with_latest_replay_order(job, latest_replay_sequence)
            _upsert_by_key(rows.pipeline_jobs, job, key="job_id")
        for event in _record_list(payload, "pipeline_events", "events", single_key="pipeline_event"):
            _validate_event_identity(event)
            event = _with_latest_replay_order(event, latest_replay_sequence)
            rows.pipeline_events.append(event)
        replay = payload.get("replay")
        if isinstance(replay, Mapping):
            rows.replay.update(_evidence_safe(dict(replay)))

    def _apply_journal_record(
        self,
        rows: _CycleRows,
        record: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        expected_record_type: str | None = None,
        expected_model_id: str | None = None,
    ) -> None:
        payload = _payload_or_record_payload(record)
        record_type = _record_type(record, payload)
        _require_schema(record, FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION)
        _require_source_cycle(record, source_id=source_id, cycle_time=cycle_time)
        _require_record_payload_identity_match(record_type, record, payload)
        record_model_id = _record_model_id(
            record,
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
        )
        if expected_record_type is not None and record_type != expected_record_type:
            raise FileOrchestrationJournalError(
                "file_journal_record_type_mismatch",
                field="record_type",
                evidence={"expected": expected_record_type, "actual": record_type[:80]},
            )
        if (
            expected_model_id is not None
            and record_model_id is not None
            and record_model_id != expected_model_id
        ):
            return
        payload = _with_replay_order(payload, record)
        if record_type == "pipeline_job":
            _validate_pipeline_job_identity(
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=record_model_id if record_model_id is not None else expected_model_id,
            )
            _upsert_by_key(rows.pipeline_jobs, payload, key="job_id")
        elif record_type == "pipeline_event":
            self._apply_event_record(rows, record)
        elif record_type in {"hydro_run", "forecast_cycle", "forcing_version", "model_context"}:
            _validate_payload_identity(
                record_type,
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=record_model_id,
            )
            setattr(rows, record_type, _latest_mapping(getattr(rows, record_type), payload))
        else:
            raise FileOrchestrationJournalError("file_journal_unknown_record_type", field="record_type")

    def _apply_event_record(self, rows: _CycleRows, record: Mapping[str, Any]) -> None:
        payload = _with_replay_order(_payload_or_record_payload(record), record)
        if "event_id" not in payload and record.get("sequence") not in (None, ""):
            payload["event_id"] = record.get("sequence")
        _validate_event_identity(payload)
        rows.pipeline_events.append(dict(payload))

    def _read_json(self, path: Path) -> dict[str, Any]:
        payload = self._read_optional_json(path)
        if payload is None:
            raise FileOrchestrationJournalError(
                "file_journal_view_missing",
                field=str(_relative_evidence(path, self.root)),
            )
        return payload

    def _read_optional_json(self, path: Path) -> dict[str, Any] | None:
        try:
            content = read_bytes_limited_no_follow(path, max_bytes=self.max_bytes, containment_root=self.root)
        except FileNotFoundError:
            return None
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(path, self.root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        self._require_within_byte_limit(content, path)
        return _decode_mapping(
            content,
            field=str(_relative_evidence(path, self.root)),
            max_nodes=self.max_json_nodes,
            max_depth=self.max_json_depth,
        )

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        try:
            content = read_bytes_limited_no_follow(path, max_bytes=self.max_bytes, containment_root=self.root)
        except FileNotFoundError:
            return []
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(path, self.root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        self._require_within_byte_limit(content, path)
        records: list[dict[str, Any]] = []
        for line_number, raw_line in enumerate(content.splitlines(), start=1):
            if not raw_line.strip():
                continue
            if len(records) >= MAX_FILE_JOURNAL_RECORDS:
                raise FileOrchestrationJournalError("file_journal_record_limit_exceeded", field="journal")
            record = _decode_mapping(
                raw_line,
                field=f"{_relative_evidence(path, self.root)}:{line_number}",
                max_nodes=self.max_json_nodes,
                max_depth=self.max_json_depth,
            )
            record[_REPLAY_ORDER_FIELD] = line_number
            records.append(record)
        return records

    def _require_within_byte_limit(self, content: bytes, path: Path) -> None:
        if len(content) > self.max_bytes:
            raise FileOrchestrationJournalError(
                "file_journal_byte_limit_exceeded",
                field=str(_relative_evidence(path, self.root)),
            )

    def _iter_direct_pipeline_job_records(self) -> Iterable[dict[str, Any]]:
        directory = self.root / "pipeline-jobs"
        for path in sorted(
            _iter_regular_json_files(
                directory,
                root=self.root,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        ):
            payload = self._read_optional_json(path)
            if payload is not None:
                yield self._validated_direct_pipeline_job_record(payload, expected_job_id=_safe_segment(path.stem))

    def _direct_pipeline_job_record(self, expected_job_id: str) -> dict[str, Any] | None:
        payload = self._read_optional_json(self.root / "pipeline-jobs" / f"{expected_job_id}.json")
        if payload is None:
            return None
        return self._validated_direct_pipeline_job_record(payload, expected_job_id=expected_job_id)

    def _iter_direct_pipeline_job_records_for_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
    ) -> Iterable[dict[str, Any]]:
        directory = self.root / "pipeline-jobs"
        for path in sorted(
            _iter_regular_json_files(
                directory,
                root=self.root,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        ):
            expected_job_id = _safe_segment(path.stem)
            payload = self._read_optional_json(path)
            if payload is None:
                continue
            job = self._validated_direct_pipeline_job_record(payload, expected_job_id=expected_job_id)
            if model_id is None:
                if _job_matches_source_cycle(job, source_id=source_id, cycle_time=cycle_time):
                    yield job
                continue
            if _job_matches_candidate(job, source_id=source_id, cycle_time=cycle_time, model_id=model_id):
                yield job

    def _iter_pipeline_job_records(self, *, include_direct: bool = True) -> Iterable[dict[str, Any]]:
        jobs: dict[str, dict[str, Any]] = {}
        budget = _RecordBudget(max(self.max_records, 1), "pipeline_job_records")
        for path in sorted(
            _iter_regular_json_files(
                self.root / "latest",
                root=self.root,
                recursive=True,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        ):
            payload = self._read_optional_json(path)
            if payload is None:
                continue
            source_id, cycle_time, model_id = _latest_identity_from_path(path, root=self.root)
            rows = _CycleRows()
            self._apply_latest_view(
                rows,
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                expected_model_id=model_id,
            )
            for job in rows.pipeline_jobs.values():
                budget.consume()
                _upsert_by_key(jobs, job, key="job_id")
        for path in sorted(
            _iter_jsonl_files(
                self.root / "journal",
                root=self.root,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        ):
            source_id, cycle_time = _journal_identity_from_path(path, root=self.root, surface="journal")
            for record in self._read_jsonl(path):
                budget.consume()
                rows = _CycleRows()
                self._apply_journal_record(rows, record, source_id=source_id, cycle_time=cycle_time)
                for job in rows.pipeline_jobs.values():
                    _upsert_by_key(jobs, job, key="job_id")
        if include_direct:
            for job in self._iter_direct_pipeline_job_records():
                budget.consume()
                _insert_missing_by_key(jobs, job, key="job_id")
        yield from jobs.values()

    def _model_context(self, model_id: str) -> dict[str, Any] | None:
        payload = self._read_optional_json(self.root / "models" / f"{_safe_segment(model_id)}.json")
        if payload is not None:
            return self._validated_direct_model_context_record(payload, expected_model_id=model_id)
        for latest in _iter_regular_json_files(
            self.root / "latest",
            root=self.root,
            recursive=True,
            max_files=self.max_files,
            max_depth=self.max_depth,
        ):
            view = self._read_optional_json(latest)
            if view is None:
                continue
            source_id, cycle_time, latest_model_id = _latest_identity_from_path(latest, root=self.root)
            rows = _CycleRows()
            self._apply_latest_view(
                rows,
                view,
                source_id=source_id,
                cycle_time=cycle_time,
                expected_model_id=latest_model_id,
            )
            model_context = rows.model_context
            if model_context is not None and str(model_context.get("model_id") or "") == model_id:
                return model_context
        return None

    def _forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> dict[str, Any] | None:
        path = (
            self.root
            / "forcing"
            / _safe_segment(source_id)
            / format_cycle_time(cycle_time)
            / f"{_safe_segment(model_id)}.json"
        )
        payload = self._read_optional_json(path)
        if payload is not None:
            return self._validated_direct_forcing_context_record(
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
        return None

    def _validated_direct_model_context_record(
        self,
        record: Mapping[str, Any],
        *,
        expected_model_id: str,
    ) -> dict[str, Any]:
        _require_schema(record, FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION)
        payload = _payload_or_record_payload(record)
        record_type = _record_type(record, payload)
        if record_type != "model_context":
            raise FileOrchestrationJournalError(
                "file_journal_record_type_mismatch",
                field="record_type",
                evidence={"expected": "model_context", "actual": record_type[:80]},
            )
        _require_record_payload_identity_match(record_type, record, payload)
        record_model_id = _explicit_record_model_id(record, payload)
        if record_model_id in (None, ""):
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        if record_model_id != expected_model_id:
            raise FileOrchestrationJournalError(
                "file_journal_model_mismatch",
                field="model_id",
                evidence={"expected": expected_model_id, "actual": record_model_id[:80]},
            )
        _validate_model_context_identity(payload, model_id=expected_model_id)
        return payload

    def _validated_direct_forcing_context_record(
        self,
        record: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> dict[str, Any]:
        _require_schema(record, FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION)
        payload = _payload_or_record_payload(record)
        record_type = _record_type(record, payload)
        if record_type != "forcing_version":
            raise FileOrchestrationJournalError(
                "file_journal_record_type_mismatch",
                field="record_type",
                evidence={"expected": "forcing_version", "actual": record_type[:80]},
            )
        _require_record_payload_identity_match(record_type, record, payload)
        _require_source_cycle(record, source_id=source_id, cycle_time=cycle_time)
        _require_model_id(record, model_id, required=True)
        _validate_forcing_version_identity(
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            require_source_cycle=True,
            require_model_id=True,
            require_forcing_version_id=True,
        )
        return payload

    def _validated_direct_pipeline_job_record(
        self,
        record: Mapping[str, Any],
        *,
        expected_job_id: str,
    ) -> dict[str, Any]:
        _require_schema(record, FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION)
        payload = _payload_or_record_payload(record)
        record_type = str(record.get("record_type") or payload.get("record_type") or "")
        if record_type != "pipeline_job":
            raise FileOrchestrationJournalError(
                "file_journal_record_type_mismatch",
                field="record_type",
                evidence={"expected": "pipeline_job", "actual": record_type[:80]},
            )
        _require_record_payload_identity_match(record_type, record, payload)
        source_id = _required_source_id(record, "source_id")
        cycle_time = _parse_cycle_time_field(record, "cycle_time")
        model_id = _record_model_id(record, payload, source_id=source_id, cycle_time=cycle_time)
        _validate_pipeline_job_identity(
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            expected_job_id=expected_job_id,
        )
        return payload

    def _write_hydro_run(self, row: Mapping[str, Any], *, retriable_only: bool) -> dict[str, Any]:
        source_id = _required_source_id(row, "source_id")
        cycle_time = _parse_cycle_time_field(row, "cycle_time")
        model_id = _required_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._hydro_run_for(str(row["run_id"]))
            if retriable_only and existing is not None and str(existing.get("status") or "") not in {
                "failed",
                "cancelled",
            }:
                raise OrchestratorError(
                    "HYDRO_RUN_NOT_RETRIABLE",
                    f"hydro_run already exists and is not retriable: {row['run_id']}",
                )
            self._append_validated_record_unlocked(
                "hydro_run",
                row,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=model_id,
            )
            return _public_scheduler_row(row)

    def _hydro_run_for(self, run_id: str) -> dict[str, Any] | None:
        safe_run_id = _safe_identity_text(str(run_id), field="run_id")
        match = _FORECAST_RUN_ID_RE.fullmatch(safe_run_id)
        model_id = _model_id_from_file_run_id(safe_run_id)
        if match is None:
            match = _CYCLE_COHORT_RUN_ID_RE.fullmatch(safe_run_id)
        if match is None:
            return None
        run_source, run_cycle = match.group(1), match.group(2)
        source_id = _normalize_file_source_id(run_source, field="run_id")
        cycle_time = parse_cycle_time(run_cycle)
        rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        if rows.hydro_run is not None and str(rows.hydro_run.get("run_id") or "") == safe_run_id:
            return _public_scheduler_row(rows.hydro_run)
        return None

    def _pipeline_job_row(self, record: Mapping[str, Any]) -> dict[str, Any]:
        cycle_id = _required_safe_identity(record, "cycle_id")
        source_id, cycle_time = _source_cycle_from_cycle_id(cycle_id)
        now = _format_utc(_utcnow())
        row = {
            "job_id": _required_safe_identity(record, "job_id"),
            "run_id": _required_safe_identity(record, "run_id"),
            "cycle_id": cycle_id,
            "source_id": source_id,
            "cycle_time": _format_utc(cycle_time),
            "job_type": _required_text(record, "job_type"),
            "slurm_job_id": record.get("slurm_job_id"),
            "array_task_id": record.get("array_task_id"),
            "model_id": record.get("model_id"),
            "status": str(record.get("status") or "pending"),
            "stage": record.get("stage"),
            "idempotency_key": record.get("idempotency_key"),
            "candidate_id": record.get("candidate_id"),
            "submitted_at": _optional_format_datetime(record.get("submitted_at"), field="submitted_at"),
            "started_at": _optional_format_datetime(record.get("started_at"), field="started_at"),
            "finished_at": _optional_format_datetime(record.get("finished_at"), field="finished_at"),
            "exit_code": record.get("exit_code"),
            "retry_count": record.get("retry_count", 0),
            "manual_retry_marker": bool(record.get("manual_retry_marker", False)),
            "error_code": record.get("error_code"),
            "error_message": record.get("error_message"),
            "log_uri": record.get("log_uri"),
            "created_at": _optional_format_datetime(record.get("created_at"), field="created_at") or now,
            "updated_at": _optional_format_datetime(record.get("updated_at"), field="updated_at") or now,
        }
        _validate_pipeline_job_identity(
            row,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=_optional_safe_identity(row, "model_id"),
        )
        return row

    def _write_pipeline_job(self, row: Mapping[str, Any], *, exclusive_direct: bool) -> dict[str, Any] | None:
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        model_id = _optional_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            return self._write_pipeline_job_unlocked(row, exclusive_direct=exclusive_direct, model_id=model_id)

    def _write_pipeline_job_unlocked(
        self,
        row: Mapping[str, Any],
        *,
        exclusive_direct: bool,
        model_id: str | None,
    ) -> dict[str, Any] | None:
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        if exclusive_direct and self._pipeline_job_conflicts_unlocked(row):
            return None
        sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
        record = _journal_record_for_write(
            "pipeline_job",
            row,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            sequence=sequence,
        )
        self._validate_outgoing_record(
            record,
            source_id=source_id,
            cycle_time=cycle_time,
            record_type="pipeline_job",
            model_id=model_id,
        )
        self._append_journal_record_unlocked(source_id=source_id, cycle_time=cycle_time, record=record)
        direct_path = self.root / "pipeline-jobs" / f"{_required_safe_identity(row, 'job_id')}.json"
        self._atomic_write_json_unlocked(direct_path, record)
        if model_id is not None:
            self._materialize_latest_unlocked(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        return _public_scheduler_row(row)

    def _pipeline_job_conflicts_unlocked(self, row: Mapping[str, Any]) -> bool:
        job_id = str(row.get("job_id") or "")
        if job_id and self.get_pipeline_job(job_id) is not None:
            return True
        idempotency_key = row.get("idempotency_key")
        return idempotency_key not in (None, "") and self.query_candidate_state(str(idempotency_key)) is not None

    def _append_validated_record(
        self,
        record_type: str,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None = None,
        materialize_model_id: str | None = None,
        sequence: int | None = None,
    ) -> None:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            self._append_validated_record_unlocked(
                record_type,
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=materialize_model_id,
                sequence=sequence,
            )

    def _append_validated_record_unlocked(
        self,
        record_type: str,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None = None,
        materialize_model_id: str | None = None,
        sequence: int | None = None,
    ) -> None:
        record_sequence = sequence or self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
        record = _journal_record_for_write(
            record_type,
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            sequence=record_sequence,
        )
        self._validate_outgoing_record(
            record,
            source_id=source_id,
            cycle_time=cycle_time,
            record_type=record_type,
            model_id=model_id,
        )
        self._append_journal_record_unlocked(source_id=source_id, cycle_time=cycle_time, record=record)
        if materialize_model_id is not None:
            self._materialize_latest_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=materialize_model_id,
            )

    def _validate_outgoing_record(
        self,
        record: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        record_type: str,
        model_id: str | None,
    ) -> None:
        rows = _CycleRows()
        self._apply_journal_record(
            rows,
            record,
            source_id=source_id,
            cycle_time=cycle_time,
            expected_record_type=record_type,
            expected_model_id=model_id,
        )

    def _next_sequence(self, *, source_id: str, cycle_time: datetime) -> int:
        with self._write_lock:
            return self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)

    def _next_sequence_unlocked(self, *, source_id: str, cycle_time: datetime) -> int:
        records = self._read_jsonl(self._journal_path(source_id=source_id, cycle_time=cycle_time))
        sequences = [_optional_replay_sequence(record) or 0 for record in records]
        return max(sequences, default=0) + 1

    def _next_event_id_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
    ) -> int:
        sequence_floor = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time) - 1
        rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None)
        event_ids = [_optional_positive_int(event.get("event_id")) or 0 for event in rows.pipeline_events]
        return max([sequence_floor, *event_ids], default=0) + 1

    def _append_journal_record_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        record: Mapping[str, Any],
    ) -> None:
        path = self._journal_path(source_id=source_id, cycle_time=cycle_time)
        try:
            existing = read_bytes_limited_no_follow(path, max_bytes=self.max_bytes, containment_root=self.root)
        except FileNotFoundError:
            existing = b""
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to read existing file journal before append",
                {"error_type": type(error).__name__},
            ) from error
        self._require_within_byte_limit(existing, path)
        line = _json_bytes(record)
        content = existing
        if content and not content.endswith(b"\n"):
            content += b"\n"
        content += line
        self._require_within_byte_limit(content, path)
        self._atomic_write_bytes_unlocked(path, content)

    def _materialize_latest_unlocked(self, *, source_id: str, cycle_time: datetime, model_id: str) -> None:
        rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        latest = {
            "schema_version": FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
            "generated_at": _format_utc(_utcnow()),
            "source_id": source_id,
            "cycle_time": _format_utc(cycle_time),
            "model_id": model_id,
            "hydro_run": _strip_internal_fields(rows.hydro_run),
            "forecast_cycle": _strip_internal_fields(rows.forecast_cycle),
            "forcing_version": _strip_internal_fields(rows.forcing_version),
            "model_context": _strip_internal_fields(rows.model_context),
            "pipeline_jobs": [_strip_internal_fields(job) for job in rows.pipeline_jobs.values()],
            "pipeline_events": [_strip_internal_fields(event) for event in rows.pipeline_events],
            "replay": {
                "latest_sequence": self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time) - 1,
                "job_count": len(rows.pipeline_jobs),
                "event_count": len(rows.pipeline_events),
            },
        }
        self._atomic_write_json_unlocked(
            self.root
            / "latest"
            / _safe_segment(source_id)
            / format_cycle_time(cycle_time)
            / f"{_safe_segment(model_id)}.json",
            latest,
        )

    def _atomic_write_json_unlocked(self, path: Path, payload: Mapping[str, Any]) -> None:
        self._atomic_write_bytes_unlocked(path, _json_bytes(payload))

    def _atomic_write_bytes_unlocked(self, path: Path, content: bytes) -> None:
        try:
            atomic_write_bytes_no_follow(path, content, containment_root=self.root)
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to atomically write file journal state",
                {"error_type": type(error).__name__},
            ) from error

    @contextmanager
    def _locked_cycle_write(self, *, source_id: str, cycle_time: datetime) -> Iterable[None]:
        with self._write_lock:
            self._ensure_root_unlocked()
            with self._cycle_file_lock_unlocked(source_id=source_id, cycle_time=cycle_time):
                yield

    @contextmanager
    def _cycle_file_lock_unlocked(self, *, source_id: str, cycle_time: datetime) -> Iterable[None]:
        import fcntl

        lock_path = (
            self.root
            / ".locks"
            / _safe_segment(_normalize_file_source_id(source_id, field="source_id"))
            / f"{format_cycle_time(cycle_time)}.lock"
        )
        parent_fd: int | None = None
        lock_fd: int | None = None
        try:
            lock_dir = ensure_directory_no_follow(lock_path.parent, containment_root=self.root)
            parent_fd = os.open(
                lock_dir,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            lock_fd = os.open(
                lock_path.name,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o666,
                dir_fd=parent_fd,
            )
            lock_stat = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode):
                raise SafeFilesystemError(f"Cycle lock target must be a regular file: {lock_path}")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to acquire file orchestration journal cycle lock",
                {"error_type": type(error).__name__},
            ) from error
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_fd)
            if parent_fd is not None:
                os.close(parent_fd)

    def _journal_path(self, *, source_id: str, cycle_time: datetime) -> Path:
        return self.root / "journal" / _safe_segment(source_id) / f"{format_cycle_time(cycle_time)}.jsonl"

    def _ensure_root_unlocked(self) -> None:
        try:
            ensure_directory_no_follow(self.root)
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to create file orchestration journal root",
                {"error_type": type(error).__name__},
            ) from error


class FileJournalRetryService:
    """Retry adapter that records retry state in the file orchestration journal."""

    def __init__(
        self,
        repository: FileOrchestrationJournalRepository,
        config: RetryConfig | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or RetryConfig()

    def should_auto_retry(self, job: Any) -> bool:
        return bool(self.retry_policy_for_job(job)["auto_retry"])

    def retry_policy_for_job(self, job: Any) -> dict[str, Any]:
        status = _file_retry_job_text(job, "status") or ""
        error_code = _file_retry_job_value(job, "error_code")
        retry_count = _file_retry_job_int(job, "retry_count")
        classification = classify_failure(error_code, attempt=retry_count, retry_limit=self.config.max_retries)
        return {
            **classification,
            "auto_retry": status != "permanently_failed"
            and classification["retryable"]
            and not classification["permanent"],
        }

    def handle_failed_job(self, job: Any) -> SimpleNamespace:
        if self.should_auto_retry(job):
            return self.schedule_auto_retry(job)
        return self.mark_permanently_failed(job)

    def schedule_auto_retry(self, job: Any) -> SimpleNamespace:
        source = _file_retry_job_record(job)
        previous_error = source.get("error_code")
        status_from = str(source.get("status") or "")
        next_retry_count = int(source.get("retry_count") or 0) + 1
        retry_job_id = f"{source['job_id']}_retry_{next_retry_count}"
        existing = self.repository.get_pipeline_job(retry_job_id)
        reused_existing_retry_job = False
        if existing is not None:
            if not _file_auto_retry_job_can_be_reused(existing):
                raise RetryError(
                    "AUTO_RETRY_JOB_CONFLICT",
                    "Existing file-journal auto retry job cannot be reset safely.",
                    {
                        "retry_job_id": retry_job_id,
                        "existing_status": existing.get("status"),
                        "existing_slurm_job_id": existing.get("slurm_job_id"),
                        "existing_array_task_id": existing.get("array_task_id"),
                        "previous_job_id": source["job_id"],
                    },
                )
            reused_existing_retry_job = True
        retry_record = {
            **source,
            "job_id": retry_job_id,
            "slurm_job_id": None,
            "array_task_id": None,
            "status": "pending",
            "submitted_at": None,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "retry_count": next_retry_count,
            "manual_retry_marker": False,
            "idempotency_key": None,
            "candidate_id": None,
            "error_code": None,
            "error_message": None,
            "log_uri": None,
            "updated_at": _format_utc(_utcnow()),
        }
        written = self.repository.upsert_pipeline_job(retry_record)
        backoff_seconds = compute_backoff_seconds(int(source.get("retry_count") or 0), self.config.backoff_schedule)
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=retry_job_id,
            event_type="retry",
            status_from=status_from,
            status_to="pending",
            details={
                "trigger": "auto",
                "retry_count": next_retry_count,
                "previous_error": previous_error,
                "backoff_seconds": backoff_seconds,
                "previous_job_id": source["job_id"],
                "slurm_job_id": written.get("slurm_job_id"),
                "failure": classify_failure(
                    previous_error,
                    attempt=int(source.get("retry_count") or 0),
                    retry_limit=self.config.max_retries,
                ),
                "reused_existing_retry_job": reused_existing_retry_job,
            },
        )
        return _file_retry_namespace(written)

    def mark_permanently_failed(self, job: Any) -> SimpleNamespace:
        source = _file_retry_job_record(job)
        if str(source.get("status") or "") == "permanently_failed":
            return _file_retry_namespace(source)
        status_from = str(source.get("status") or "")
        last_error = source.get("error_code")
        _previous_status, written = self.repository.update_pipeline_job_status(
            str(source["job_id"]),
            "permanently_failed",
            error_code=str(last_error) if last_error not in (None, "") else None,
            error_message=source.get("error_message"),
            finished_at=_utcnow(),
        )
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=str(source["job_id"]),
            event_type="permanently_failed",
            status_from=status_from,
            status_to="permanently_failed",
            details={
                "final_retry_count": int(source.get("retry_count") or 0),
                "last_error": last_error,
                "failure": classify_failure(
                    last_error,
                    attempt=int(source.get("retry_count") or 0),
                    retry_limit=self.config.max_retries,
                ),
                "automatic_retry_stopped": True,
            },
        )
        return _file_retry_namespace(written)

    def record_manual_repair(
        self,
        run_id: str,
        *,
        requested_by: str | None = None,
        request_id: str | None = None,
        reason: str | None = None,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> SimpleNamespace:
        if trusted_internal:
            policy_decision = trusted_internal_policy_decision(
                "pipeline.retry_run",
                target_type="pipeline_run",
                target_id=run_id,
                actor_id="trusted-internal:file-journal-retry-service",
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
                {"run_id": run_id, "policy_decision": decision.to_dict(), "no_mutation_expected": True},
            )
        source_id, cycle_time = _source_cycle_from_file_run_id(run_id)
        with self.repository._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            failed_job, active_job = self._manual_retry_source_for_run(run_id)
            if active_job is not None:
                raise RetryConflictError(run_id, _file_retry_namespace(active_job))
            if failed_job is None:
                raise RetryNotFoundError(run_id)

            previous_error = failed_job.get("error_code") or (
                "cancelled" if failed_job.get("status") == "cancelled" else None
            )
            next_retry_count = int(failed_job.get("retry_count") or 0) + 1
            details: dict[str, Any] = {
                "trigger": "manual",
                "retry_count": next_retry_count,
                "previous_error": previous_error,
                "previous_job_id": failed_job["job_id"],
                "slurm_job_id": None,
                "manual_retry_marker": True,
                "prior_failure_reason": previous_error,
                "failure": classify_failure(
                    previous_error,
                    attempt=next_retry_count,
                    retry_limit=self.config.max_retries,
                    manual=True,
                ),
            }
            if requested_by not in (None, ""):
                details["requested_by"] = requested_by
            if request_id not in (None, ""):
                details["request_id"] = request_id
            if reason not in (None, ""):
                details["reason"] = reason
            details["policy_decision"] = decision.to_dict()
            self._append_pipeline_event_unlocked(
                failed_job,
                event_type="retry",
                status_from=str(failed_job.get("status") or ""),
                status_to="manual_repair_requested",
                details=details,
            )
            marker = {
                "job_id": failed_job["job_id"],
                "run_id": run_id,
                "cycle_id": failed_job.get("cycle_id"),
                "job_type": failed_job.get("job_type"),
                "model_id": failed_job.get("model_id"),
                "stage": failed_job.get("stage"),
                "status": "manual_repair_requested",
                "retry_count": next_retry_count,
                "manual_retry_marker": True,
                "previous_job_id": failed_job["job_id"],
                "prior_failure_reason": previous_error,
            }
            return _file_retry_namespace(marker)

    def _create_pending_manual_retry_job(self, run_id: str) -> SimpleNamespace:
        source_id, cycle_time = _source_cycle_from_file_run_id(run_id)
        with self.repository._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            failed_job, active_job = self._manual_retry_source_for_run(run_id)
            if active_job is not None:
                raise RetryConflictError(run_id, _file_retry_namespace(active_job))
            if failed_job is None:
                raise RetryNotFoundError(run_id)

            previous_error = failed_job.get("error_code") or (
                "cancelled" if failed_job.get("status") == "cancelled" else None
            )
            next_retry_count = int(failed_job.get("retry_count") or 0) + 1
            retry_job_id = _next_file_manual_retry_job_id_for_run(self.repository, run_id)
            retry_record = {
                **failed_job,
                "job_id": retry_job_id,
                "status": "pending",
                "slurm_job_id": None,
                "array_task_id": None,
                "submitted_at": None,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "retry_count": next_retry_count,
                "manual_retry_marker": True,
                "idempotency_key": f"manual_retry:{run_id}:{next_retry_count}",
                "candidate_id": None,
                "error_code": None,
                "error_message": None,
                "log_uri": None,
                "updated_at": _format_utc(_utcnow()),
            }
            retry_row = self.repository._pipeline_job_row(retry_record)
            if self.repository._pipeline_job_conflicts_unlocked(retry_row):
                conflict = active_job or self.repository._pipeline_job_for_id_unlocked(retry_job_id) or retry_record
                raise RetryConflictError(run_id, _file_retry_namespace(conflict))
            written = self.repository._write_pipeline_job_unlocked(
                retry_row,
                exclusive_direct=True,
                model_id=_optional_safe_identity(retry_row, "model_id"),
            )
            if written is None:
                conflict = active_job or self.repository._pipeline_job_for_id_unlocked(retry_job_id) or retry_record
                raise RetryConflictError(run_id, _file_retry_namespace(conflict))
            self._append_pipeline_event_unlocked(
                written,
                event_type="retry",
                status_from=str(failed_job.get("status") or ""),
                status_to="pending",
                details={
                    "trigger": "manual",
                    "retry_count": next_retry_count,
                    "previous_error": previous_error,
                    "previous_job_id": failed_job["job_id"],
                    "slurm_job_id": written.get("slurm_job_id"),
                    "manual_retry_marker": True,
                    "prior_failure_reason": previous_error,
                    "failure": classify_failure(
                        previous_error,
                        attempt=next_retry_count,
                        retry_limit=self.config.max_retries,
                        manual=True,
                    ),
                },
            )
            return _file_retry_namespace(written)

    def _append_pipeline_event_unlocked(
        self,
        job: Mapping[str, Any],
        *,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        source_id = _source_id_from_job(job)
        cycle_time = _cycle_time_from_job(job)
        model_id = _optional_safe_identity(job, "model_id")
        row = {
            "event_id": self.repository._next_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            ),
            "entity_type": "pipeline_job",
            "entity_id": str(job["job_id"]),
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "message": None,
            "details": details,
            "created_at": _format_utc(_utcnow()),
        }
        self.repository._append_validated_record_unlocked(
            "pipeline_event",
            row,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            materialize_model_id=model_id,
        )
        return _public_scheduler_row(row)

    def attempt_manual_retry(
        self,
        run_id: str,
        gateway: Any | None = None,
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> SimpleNamespace:
        if trusted_internal:
            policy_decision = trusted_internal_policy_decision(
                "pipeline.retry_run",
                target_type="pipeline_run",
                target_id=run_id,
                actor_id="trusted-internal:file-journal-retry-service",
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
                {"run_id": run_id, "policy_decision": decision.to_dict(), "no_mutation_expected": True},
            )
        if gateway is None:
            raise RetryError(
                "RETRY_EXECUTION_UNAVAILABLE",
                "No Slurm gateway available for retry submission.",
                {"run_id": run_id},
            )

        retry_job = self._create_pending_manual_retry_job(run_id)
        runtime_root_resolution: dict[str, Any] | None = None
        runtime_root_contract: dict[str, str] | None = None
        try:
            request, runtime_root_resolution, runtime_root_contract = self._manual_retry_submission_request(retry_job)
            submitted = gateway.submit_job(request)
        except Exception as error:
            if runtime_root_resolution is not None:
                _attach_retry_runtime_root_resolution(error, runtime_root_resolution)
            if runtime_root_contract is not None:
                _attach_retry_runtime_root_contract(error, runtime_root_contract)
            updated = self._record_manual_retry_submission_failure(retry_job.job_id, error)
            return _file_retry_namespace(updated)
        updated = self._record_manual_retry_submission_success(
            retry_job.job_id,
            submitted,
            runtime_root_resolution=runtime_root_resolution,
            runtime_root_contract=runtime_root_contract,
        )
        return _file_retry_namespace(updated)

    def _manual_retry_submission_request(
        self,
        retry_job: SimpleNamespace,
    ) -> tuple[SubmitJobRequest, dict[str, Any] | None, dict[str, str] | None]:
        model_id = retry_job.model_id or _model_id_from_file_run_id(retry_job.run_id) or "unknown"
        submission_job = _RetrySubmissionJob(
            job_id=retry_job.job_id,
            run_id=retry_job.run_id,
            cycle_id=retry_job.cycle_id,
            job_type=retry_job.job_type,
            model_id=model_id,
            stage=retry_job.stage,
            retry_count=int(retry_job.retry_count or 0),
            previous_job_id=getattr(retry_job, "previous_job_id", None),
        )
        runtime_root = self._resolve_file_retry_runtime_roots(submission_job)
        runtime_root_contract = runtime_root.manifest_fields if runtime_root is not None else None
        runtime_root_resolution = runtime_root.evidence if runtime_root is not None else None
        manifest = _retry_submission_manifest(
            submission_job,
            model_id=model_id,
            runtime_root_fields=runtime_root_contract,
        )
        return (
            SubmitJobRequest(
                run_id=retry_job.run_id,
                model_id=model_id,
                job_type=retry_job.job_type,
                manifest=manifest,
            ),
            runtime_root_resolution,
            runtime_root_contract,
        )

    def _resolve_file_retry_runtime_roots(self, retry_job: _RetrySubmissionJob) -> SimpleNamespace | None:
        if retry_job.job_type != DOWNLOAD_SOURCE_CYCLE_JOB_TYPE:
            return None
        candidate_batch = self._file_retry_runtime_root_candidates(retry_job)
        rejected: list[dict[str, str]] = []
        rejected_total_count = 0
        best_resolved: dict[str, tuple[str, str]] = {}
        best_missing = list(_REQUIRED_RUNTIME_ROOT_FIELDS)
        secret_rejected = False
        unsafe_rejected = False
        for candidate in candidate_batch.candidates:
            resolution = _resolve_runtime_root_candidate(candidate.source, candidate.value)
            rejected_total_count += len(resolution.rejected)
            if len(rejected) < _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT:
                remaining = _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT - len(rejected)
                rejected.extend(resolution.rejected[:remaining])
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
            return SimpleNamespace(manifest_fields=manifest_fields, evidence=evidence)
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
        raise _RetryRuntimeRootResolutionError(
            RETRY_RUNTIME_ROOTS_UNRESOLVED,
            "Manual retry cannot resolve required object-store runtime roots for download_source_cycle.",
            evidence,
        )

    def _file_retry_runtime_root_candidates(self, retry_job: _RetrySubmissionJob) -> _RuntimeRootCandidateBatch:
        candidates: list[_RuntimeRootCandidate] = []
        seen_job_ids: set[str] = set()
        job_ids: list[str] = []
        event_candidate_returned_count = 0
        event_candidate_total_count = 0
        event_candidate_omitted_count = 0
        event_rows_scanned_count = 0
        event_rows_total_count = 0
        event_rows_omitted_count = 0
        manual_retry_event_rows_ignored = 0
        if retry_job.previous_job_id:
            job_ids.append(str(retry_job.previous_job_id))
        excluded = set(job_ids)
        if retry_job.run_id:
            same_run_jobs = sorted(
                self.repository.query_pipeline_jobs_by_run(str(retry_job.run_id)),
                key=_db_compatible_pipeline_job_order_key,
            )
            for job in same_run_jobs:
                job_id = str(job.get("job_id") or "")
                if not job_id or job_id in excluded or job_id == retry_job.job_id:
                    continue
                if str(job.get("job_type") or "") != DOWNLOAD_SOURCE_CYCLE_JOB_TYPE:
                    continue
                if (
                    retry_job.cycle_id
                    and job.get("cycle_id") not in (None, "")
                    and job.get("cycle_id") != retry_job.cycle_id
                ):
                    continue
                if job.get("manual_retry_marker") is True:
                    continue
                job_ids.append(job_id)
        for job_id in job_ids:
            if job_id in seen_job_ids:
                continue
            if len(candidates) >= _RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT:
                break
            seen_job_ids.add(job_id)
            event_batch = self._file_retry_event_runtime_root_candidates(
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

    def _file_retry_event_runtime_root_candidates(
        self,
        job_id: str,
        *,
        candidate_budget: int,
    ) -> _RuntimeRootCandidateBatch:
        job = self.repository.get_pipeline_job(job_id)
        if job is None:
            return _RuntimeRootCandidateBatch(candidates=[])
        source_id = _source_id_from_job(job)
        cycle_time = _cycle_time_from_job(job)
        model_id = _optional_safe_identity(job, "model_id")
        rows = self.repository._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        submission_events = [
            event
            for event in rows.pipeline_events
            if str(event.get("entity_id") or "") == job_id
            and str(event.get("event_type") or "") == "submission"
        ]
        event_rows_total_count = len(submission_events)
        if job.get("manual_retry_marker") is True:
            return _RuntimeRootCandidateBatch(
                candidates=[],
                event_rows_total_count=event_rows_total_count,
                manual_retry_event_rows_ignored=event_rows_total_count,
            )
        events = sorted(
            submission_events,
            key=lambda event: _optional_positive_int(event.get("event_id")) or 0,
            reverse=True,
        )[:_RUNTIME_ROOT_EVENT_ROW_SCAN_LIMIT]
        candidates: list[_RuntimeRootCandidate] = []
        event_candidate_total_count = 0
        manual_retry_event_rows_ignored = 0
        for event in events:
            details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
            if _event_details_is_manual_retry_submission(details):
                manual_retry_event_rows_ignored += 1
                continue

            event_id = event.get("event_id")
            event_source = f"file_journal_event:{job_id}:{event_id}"
            for path in _RUNTIME_ROOT_EVENT_CANDIDATE_PATHS:
                candidate = _mapping_at(details, path)
                if candidate and _has_runtime_root_field(candidate):
                    event_candidate_total_count += 1
                    if len(candidates) < candidate_budget:
                        candidates.append(
                            _RuntimeRootCandidate(
                                f"{event_source}:{'.'.join(path)}",
                                candidate,
                            )
                        )
            if _has_runtime_root_field(details):
                event_candidate_total_count += 1
                if len(candidates) < candidate_budget:
                    candidates.append(
                        _RuntimeRootCandidate(
                            f"{event_source}:details",
                            details,
                        )
                    )
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

    def _record_manual_retry_submission_success(
        self,
        job_id: str,
        submitted: Any,
        *,
        runtime_root_resolution: dict[str, Any] | None = None,
        runtime_root_contract: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = _file_retry_gateway_payload(submitted)
        row = self.repository.get_pipeline_job(job_id)
        if row is None:
            raise RetryNotFoundError(job_id)
        slurm_job_id = payload.get("job_id") or payload.get("slurm_job_id")
        row.update(
            {
                "status": "submitted",
                "slurm_job_id": str(slurm_job_id) if slurm_job_id is not None else None,
                "submitted_at": _format_utc(_file_retry_gateway_time(payload.get("submitted_at")) or _utcnow()),
                "started_at": _optional_format_datetime(payload.get("started_at"), field="started_at"),
                "finished_at": _optional_format_datetime(payload.get("finished_at"), field="finished_at"),
                "error_code": None,
                "error_message": None,
                "updated_at": _format_utc(_utcnow()),
            }
        )
        written = self.repository.upsert_pipeline_job(row)
        self._reset_hydro_run_after_retry_submission(written)
        details: dict[str, Any] = {
            "trigger": "manual",
            "slurm_job_id": written.get("slurm_job_id"),
            "gateway_status": str(payload.get("status")) if payload.get("status") is not None else None,
        }
        if runtime_root_resolution is not None:
            details["runtime_root_resolution"] = _public_evidence(runtime_root_resolution)
        if runtime_root_contract is not None:
            details["runtime_root_contract"] = _public_evidence(runtime_root_contract)
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=job_id,
            event_type="submission",
            status_from="pending",
            status_to="submitted",
            message=f"Manual retry submitted as Slurm job {written.get('slurm_job_id')}.",
            details=details,
        )
        return written

    def _reset_hydro_run_after_retry_submission(self, retry_job: Mapping[str, Any]) -> None:
        run_id = retry_job.get("run_id")
        if run_id in (None, ""):
            return
        existing = self.repository._hydro_run_for(str(run_id))
        if existing is None or str(existing.get("status") or "") not in {"failed", "cancelled"}:
            return
        self.repository.update_hydro_run_status(str(run_id), "pending", slurm_job_id=retry_job.get("slurm_job_id"))

    def _record_manual_retry_submission_failure(self, job_id: str, error: Exception) -> dict[str, Any]:
        error_code = str(getattr(error, "code", None) or "RETRY_SUBMISSION_FAILED")
        error_message = _safe_error_message(str(getattr(error, "message", None) or error))
        _previous_status, written = self.repository.update_pipeline_job_status(
            job_id,
            "submission_failed",
            error_code=error_code,
            error_message=error_message,
            finished_at=_utcnow(),
        )
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=job_id,
            event_type="submission",
            status_from="pending",
            status_to="submission_failed",
            message=f"Manual retry submission failed: {error_message}",
            details=_manual_retry_submission_failure_details(error, error_code=error_code, error_message=error_message),
        )
        return written

    def _manual_retry_source_for_run(self, run_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        jobs = self.repository.query_pipeline_jobs_by_run(run_id)
        safe_jobs = sorted(
            (job for job in jobs if str(job.get("job_id") or "") != "file_journal_read_blocked"),
            key=_file_retry_job_truth_sort_key,
        )
        durable_run = self.repository._hydro_run_for(run_id)
        durable_status = str(durable_run.get("status") or "") if durable_run is not None else None
        if durable_status in DURABLE_HYDRO_SUCCESS_STATUSES:
            return None, None
        active_job = next((job for job in safe_jobs if str(job.get("status") or "") in ACTIVE_RETRY_STATUSES), None)
        if active_job is not None:
            return None, active_job
        if not safe_jobs:
            return None, None
        latest_job = safe_jobs[-1]
        latest_status = str(latest_job.get("status") or "")
        if latest_status in TERMINAL_SUCCESS_RETRY_STATUSES:
            return None, None
        if latest_status in MANUAL_RETRY_SOURCE_STATUSES:
            return latest_job, None
        if durable_status is not None and (
            durable_status in PARTIAL_OR_FAILED_HYDRO_STATUSES or durable_status.startswith("failed")
        ):
            failed_job = next(
                (job for job in reversed(safe_jobs) if str(job.get("status") or "") in MANUAL_RETRY_SOURCE_STATUSES),
                None,
            )
            return failed_job, None
        return None, None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default) + "\n").encode("utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _format_utc(value)
    return str(value)


def _journal_record_for_write(
    record_type: str,
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
    sequence: int,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "sequence": int(sequence),
        "record_type": record_type,
        "source_id": _normalize_file_source_id(source_id, field="source_id"),
        "cycle_time": _format_utc(cycle_time),
        "created_at": _format_utc(_utcnow()),
        "payload": _strip_internal_fields(payload),
    }
    if model_id not in (None, ""):
        record["model_id"] = _safe_identity_text(str(model_id), field="model_id")
    for payload_field in ("job_id", "run_id", "cycle_id", "event_id", "entity_id", "forcing_version_id"):
        value = payload.get(payload_field)
        if value not in (None, ""):
            record[payload_field] = value
    return record


def _strip_internal_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_internal_fields(item)
            for key, item in value.items()
            if not str(key).startswith("_file_journal_")
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_strip_internal_fields(item) for item in value]
    if isinstance(value, datetime):
        return _format_utc(value)
    return value


def _mapping_value(row: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = row.get(field)
    if not isinstance(value, Mapping):
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    return value


def _optional_mapping_value(row: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = row.get(field)
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    return value


def _coerce_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    try:
        return parse_cycle_time(str(value))
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_datetime", field=field) from error


def _optional_format_datetime(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    return _format_utc(_coerce_datetime(value, field=field))


def _file_retry_job_value(job: Any, field: str) -> Any:
    if isinstance(job, Mapping):
        return job.get(field)
    return getattr(job, field, None)


def _file_retry_job_text(job: Any, field: str) -> str | None:
    value = _file_retry_job_value(job, field)
    return str(value) if value not in (None, "") else None


def _file_retry_job_int(job: Any, field: str) -> int:
    value = _file_retry_job_value(job, field)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _file_retry_job_record(job: Any) -> dict[str, Any]:
    fields = (
        "job_id",
        "run_id",
        "cycle_id",
        "source_id",
        "cycle_time",
        "job_type",
        "slurm_job_id",
        "array_task_id",
        "model_id",
        "status",
        "stage",
        "idempotency_key",
        "candidate_id",
        "submitted_at",
        "started_at",
        "finished_at",
        "exit_code",
        "retry_count",
        "manual_retry_marker",
        "error_code",
        "error_message",
        "log_uri",
        "created_at",
        "updated_at",
    )
    record = {
        name: _file_retry_job_value(job, name)
        for name in fields
        if _file_retry_job_value(job, name) is not None
    }
    for identity_field in ("job_id", "run_id", "cycle_id"):
        record[identity_field] = _safe_identity_text(str(record.get(identity_field) or ""), field=identity_field)
    record["job_type"] = str(record.get("job_type") or "")
    if record["job_type"] == "":
        raise FileOrchestrationJournalError("file_journal_missing_field", field="job_type")
    record["status"] = str(record.get("status") or "failed")
    record["retry_count"] = _file_retry_job_int(job, "retry_count")
    record["manual_retry_marker"] = bool(record.get("manual_retry_marker", False))
    return record


def _file_retry_namespace(row: Mapping[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**dict(row))


def _file_auto_retry_job_can_be_reused(job: Mapping[str, Any]) -> bool:
    if job.get("manual_retry_marker") is True:
        return False
    if job.get("slurm_job_id") not in (None, "") or job.get("array_task_id") not in (None, ""):
        return False
    return str(job.get("status") or "") in {"pending", "submission_failed"}


def _next_file_manual_retry_job_id_for_run(repository: FileOrchestrationJournalRepository, run_id: str) -> str:
    prefix = f"{_safe_identity_text(run_id, field='run_id')}_retry_"
    used_retry_job_ids = {
        str(job.get("job_id"))
        for job in repository.query_pipeline_jobs_by_run(run_id)
        if job.get("manual_retry_marker") is True or str(job.get("job_id") or "").startswith(prefix)
    }
    deterministic_job_id = f"{prefix}active"
    if deterministic_job_id not in used_retry_job_ids:
        return deterministic_job_id
    sequence = 2
    while f"{prefix}{sequence}" in used_retry_job_ids:
        sequence += 1
    return f"{prefix}{sequence}"


def _file_retry_gateway_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            payload = dict(model_dump(mode="json"))
        elif hasattr(value, "__dict__"):
            payload = dict(value.__dict__)
        else:
            raise TypeError(f"Expected mapping-like Slurm submission payload, got {type(value).__name__}")
    status = payload.get("status")
    status_value = getattr(status, "value", status)
    if status_value is not None:
        payload["status"] = status_value
    return payload


def _file_retry_gateway_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return _coerce_datetime(value, field="gateway_time")


def _manual_retry_submission_failure_details(
    error: Exception,
    *,
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "trigger": "manual",
        "error_code": error_code,
        "error_message": error_message,
    }
    runtime_root_resolution = _runtime_root_resolution_from_error(error)
    if runtime_root_resolution is not None:
        details["runtime_root_resolution"] = _public_evidence(runtime_root_resolution)
    runtime_root_contract = _runtime_root_contract_from_error(error)
    if runtime_root_contract is not None:
        details["runtime_root_contract"] = _public_evidence(runtime_root_contract)
    return details


def _mapping_has_runtime_root_fields(value: Mapping[str, Any]) -> bool:
    return any(field in value for field in _RUNTIME_ROOT_FIELDS)


def _file_retry_job_truth_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _datetime_sort_key(
            row.get("finished_at")
            or row.get("submitted_at")
            or row.get("started_at")
            or row.get("updated_at")
            or row.get("created_at")
        ),
        _datetime_sort_key(row.get("created_at")),
        str(row.get("job_id") or ""),
    )


def _model_id_from_file_run_id(run_id: str | None) -> str | None:
    if not run_id:
        return None
    text = str(run_id)
    match = _FORECAST_RUN_ID_RE.fullmatch(text)
    if match is not None:
        return match.group(3)
    suffix_match = re.search(r"(?:^|_)(model(?:_[A-Za-z0-9.-]+)+)$", text)
    return suffix_match.group(1) if suffix_match is not None else None


def _source_cycle_from_file_run_id(run_id: str) -> tuple[str, datetime]:
    safe_run_id = _safe_identity_text(str(run_id), field="run_id")
    match = _FORECAST_RUN_ID_RE.fullmatch(safe_run_id) or _CYCLE_COHORT_RUN_ID_RE.fullmatch(safe_run_id)
    if match is None:
        raise FileOrchestrationJournalError("file_journal_invalid_identity", field="run_id")
    source_id = _normalize_file_source_id(match.group(1), field="run_id")
    try:
        return source_id, parse_cycle_time(match.group(2))
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field="run_id") from error


def _source_cycle_from_cycle_id(cycle_id: str) -> tuple[str, datetime]:
    source, separator, cycle_stamp = str(cycle_id).rpartition("_")
    if not separator:
        raise FileOrchestrationJournalError("file_journal_invalid_identity", field="cycle_id")
    source_id = _normalize_file_source_id(source, field="cycle_id")
    try:
        cycle_time = parse_cycle_time(cycle_stamp)
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field="cycle_id") from error
    expected = _cycle_id_for_file_source(source_id, cycle_time)
    if cycle_id != expected:
        raise FileOrchestrationJournalError(
            "file_journal_cycle_id_mismatch",
            field="cycle_id",
            evidence={"expected": expected, "actual": cycle_id[:80]},
        )
    return source_id, cycle_time


def _source_id_from_job(job: Mapping[str, Any]) -> str:
    source = _optional_source_id(job, "source_id")
    if source is not None:
        return source
    source, _cycle_time = _source_cycle_from_cycle_id(_required_safe_identity(job, "cycle_id"))
    return source


def _cycle_time_from_job(job: Mapping[str, Any]) -> datetime:
    if job.get("cycle_time") not in (None, ""):
        return _parse_cycle_time_field(job, "cycle_time")
    _source, cycle_time = _source_cycle_from_cycle_id(_required_safe_identity(job, "cycle_id"))
    return cycle_time


def _file_journal_blocked_candidate_state(
    error: FileOrchestrationJournalError,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    run_id: str,
    forcing_version_id: str,
    candidate_id: str,
    retry_limit: int | None,
    job_limit: int,
    event_limit: int,
) -> dict[str, Any]:
    cycle_id = _blocked_cycle_id(source_id, cycle_time)
    return _public_candidate_state(
        {
            "candidate_id": candidate_id,
            "run_id": run_id,
            "forcing_version_id": forcing_version_id,
            "retry_limit": retry_limit,
            "job_limit": job_limit,
            "event_limit": event_limit,
            "pipeline_status": "running",
            "stage": "file_journal_read",
            "file_journal": {
                "status": "blocked",
                "reason": error.reason,
                "field": error.field,
                "evidence": _public_evidence(error.evidence),
            },
            "pipeline_jobs": [
                {
                    "job_id": "file_journal_read_blocked",
                    "run_id": run_id,
                    "cycle_id": cycle_id,
                    "model_id": model_id,
                    "status": "running",
                    "stage": "file_journal_read",
                    "error_code": error.reason,
                }
            ],
        }
    )


def _blocked_stage_status(
    error: FileOrchestrationJournalError,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
) -> dict[str, Any]:
    return _public_evidence(
        {
            "stage": "file_journal_read",
            "status": "running",
            "job_id": "file_journal_read_blocked",
            "cycle_id": _blocked_cycle_id(source_id, cycle_time),
            "model_id": model_id,
            "slurm_job_id": "unknown_after_attempt",
            "error_code": error.reason,
            "file_journal": {
                "status": "blocked",
                "reason": error.reason,
                "field": error.field,
                "evidence": _public_evidence(error.evidence),
            },
        }
    )


def _public_scheduler_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return _public_evidence(row)


def _public_candidate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    raw_manifest = payload.get("nfs_raw_manifest")
    if isinstance(raw_manifest, Mapping):
        payload["nfs_raw_manifest"] = _public_raw_manifest_evidence(raw_manifest)
    return _public_evidence(payload)


def _public_evidence(value: Any) -> Any:
    return _evidence_safe(_sanitize_public_evidence(value))


def _sanitize_public_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_public_field(str(key), nested)
            for key, nested in value.items()
            if not str(key).startswith("_file_journal_")
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_sanitize_public_evidence(item) for item in value]
    return _sanitize_public_scalar(value)


def _sanitize_public_field(key: str, value: Any) -> Any:
    lowered = key.lower()
    if lowered.endswith("_path") or lowered.endswith("_root") or lowered in {"path", "root"}:
        return "[local-path]" if value not in (None, "") else value
    if lowered.endswith("_uri") or lowered in {"uri", "object_uri", "manifest_uri"}:
        return _sanitize_file_provider_evidence_scalar(key, value)
    return _sanitize_public_evidence(value)


def _sanitize_public_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if (
        text.startswith("/")
        or text.startswith("~")
        or "://" in text
        or text.startswith("s3:")
        or text.startswith("published:")
    ):
        return _sanitize_file_provider_evidence_scalar("uri", value)
    return value


def _blocked_query_job(
    error: FileOrchestrationJournalError,
    *,
    job_id: str = "file_journal_read_blocked",
    idempotency_key: str | None = None,
    cycle_id: str | None = None,
    run_id: str | None = None,
    slurm_job_id: str | None = None,
) -> dict[str, Any]:
    return _public_evidence(
        {
            "job_id": job_id or "file_journal_read_blocked",
            "idempotency_key": idempotency_key,
            "cycle_id": cycle_id,
            "run_id": run_id,
            "slurm_job_id": slurm_job_id or "unknown_after_attempt",
            "status": "running",
            "stage": "file_journal_read",
            "error_code": error.reason,
            "file_journal": {
                "status": "blocked",
                "reason": error.reason,
                "field": error.field,
                "evidence": _evidence_safe(error.evidence),
            },
        }
    )


def _job_is_active(job: Mapping[str, Any]) -> bool:
    status = str(job.get("status") or "")
    return status not in ("", *TERMINAL_PIPELINE_STATUSES)


def _job_matches_candidate(job: Mapping[str, Any], *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
    source_id = _normalize_file_source_id(source_id, field="source_id")
    cycle_id = _cycle_id_for_file_source(source_id, cycle_time)
    cycle_stamp = format_cycle_time(cycle_time)
    cycle_run_id = f"cycle_{source_id.lower()}_{cycle_stamp}"
    candidate_run_id = f"fcst_{source_id.lower()}_{cycle_stamp}_{model_id}"
    if str(job.get("cycle_id") or "") != cycle_id:
        return False
    return (
        str(job.get("run_id") or "") in {candidate_run_id, cycle_run_id}
        or str(job.get("model_id") or "") == model_id
        or (job.get("model_id") in (None, "") and str(job.get("run_id") or "") == cycle_run_id)
    )


def _job_matches_source_cycle(job: Mapping[str, Any], *, source_id: str, cycle_time: datetime) -> bool:
    source_id = _normalize_file_source_id(source_id, field="source_id")
    cycle_id = _cycle_id_for_file_source(source_id, cycle_time)
    if str(job.get("cycle_id") or "") != cycle_id:
        return False
    cycle_stamp = format_cycle_time(cycle_time)
    run_id = str(job.get("run_id") or "")
    return run_id == f"cycle_{source_id.lower()}_{cycle_stamp}" or run_id.startswith(
        f"fcst_{source_id.lower()}_{cycle_stamp}_"
    )


def _row_matches_candidate(
    row: Mapping[str, Any] | None,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> bool:
    if not isinstance(row, Mapping):
        return False
    source_id = _normalize_file_source_id(source_id, field="source_id")
    actual_source = _optional_source_id(row, "source_id")
    if actual_source is not None and actual_source != source_id:
        return False
    if row.get("cycle_time") not in (None, ""):
        try:
            parsed_cycle_time = parse_cycle_time(str(row["cycle_time"]))
        except (TypeError, ValueError) as error:
            raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field="cycle_time") from error
        if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
            return False
    return str(row.get("model_id") or "") in ("", model_id)


def _payload_or_record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    if "payload" in record:
        payload = record.get("payload")
        if isinstance(payload, Mapping):
            return dict(payload)
        raise FileOrchestrationJournalError("file_journal_expected_object", field="payload")
    return dict(record)


def _record_type(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    value = record.get("record_type")
    if value in (None, ""):
        value = payload.get("record_type")
    if value in (None, ""):
        return ""
    return _scalar_text(value, field="record_type", invalid_reason="file_journal_invalid_field")


def _record_list(payload: Mapping[str, Any], *keys: str, single_key: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    single = payload.get(single_key)
    if isinstance(single, Mapping):
        records.append(dict(single))
    elif single not in (None, ""):
        raise FileOrchestrationJournalError("file_journal_expected_object", field=single_key)
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for index, item in enumerate(value):
                if not isinstance(item, Mapping):
                    raise FileOrchestrationJournalError("file_journal_expected_object", field=f"{key}[{index}]")
                records.append(dict(item))
            continue
        raise FileOrchestrationJournalError("file_journal_expected_list", field=key)
    return records


def _first_mapping(payload: Mapping[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
        if value not in (None, ""):
            raise FileOrchestrationJournalError("file_journal_expected_object", field=key)
    return None


def _latest_mapping(current: dict[str, Any] | None, incoming: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if incoming is None:
        return current
    if current is None:
        return dict(incoming)
    current_replay_key = _replay_order_key(current)
    incoming_replay_key = _replay_order_key(incoming)
    if current_replay_key is not None or incoming_replay_key is not None:
        if current_replay_key is None:
            return dict(incoming)
        if incoming_replay_key is None:
            return current
        return dict(incoming) if incoming_replay_key >= current_replay_key else current
    current_time = _datetime_sort_key(current.get("updated_at") or current.get("created_at"))
    incoming_time = _datetime_sort_key(incoming.get("updated_at") or incoming.get("created_at"))
    return dict(incoming) if incoming_time >= current_time else current


def _upsert_by_key(target: dict[str, dict[str, Any]], row: Mapping[str, Any], *, key: str) -> None:
    row_key = _required_safe_identity(row, key)
    existing = target.get(row_key)
    target[row_key] = _latest_mapping(existing, row) or dict(row)


def _insert_missing_by_key(target: dict[str, dict[str, Any]], row: Mapping[str, Any], *, key: str) -> None:
    row_key = _required_safe_identity(row, key)
    target.setdefault(row_key, dict(row))


def _with_replay_order(payload: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    sequence = _optional_replay_sequence(record)
    if sequence is not None:
        row[_REPLAY_SEQUENCE_FIELD] = sequence
    line_order = record.get(_REPLAY_ORDER_FIELD)
    if isinstance(line_order, int):
        row[_REPLAY_ORDER_FIELD] = line_order
    return row


def _with_latest_replay_order(row: Mapping[str, Any], latest_replay_sequence: int | None) -> dict[str, Any]:
    if latest_replay_sequence is None:
        return dict(row)
    payload = dict(row)
    payload[_REPLAY_SEQUENCE_FIELD] = latest_replay_sequence
    payload[_REPLAY_ORDER_FIELD] = _LATEST_REPLAY_ORDER_SENTINEL
    return payload


def _latest_replay_sequence(payload: Mapping[str, Any]) -> int | None:
    replay = payload.get("replay")
    if not isinstance(replay, Mapping):
        return None
    value = replay.get("latest_sequence")
    if value in (None, ""):
        return None
    text = _scalar_text(
        value,
        field="replay.latest_sequence",
        invalid_reason="file_journal_invalid_field",
    )
    try:
        return int(text)
    except ValueError as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field="replay.latest_sequence") from error


def _optional_replay_sequence(record: Mapping[str, Any]) -> int | None:
    value = record.get("sequence")
    if value in (None, ""):
        return None
    text = _scalar_text(value, field="sequence", invalid_reason="file_journal_invalid_field")
    try:
        return int(text)
    except ValueError as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field="sequence") from error


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _replay_order_key(row: Mapping[str, Any]) -> tuple[int, int] | None:
    sequence = row.get(_REPLAY_SEQUENCE_FIELD)
    line_order = row.get(_REPLAY_ORDER_FIELD)
    if not isinstance(sequence, int) and not isinstance(line_order, int):
        return None
    sequence_value = sequence if isinstance(sequence, int) else -1
    line_order_value = line_order if isinstance(line_order, int) else -1
    return sequence_value, line_order_value


def _dedupe_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keyed: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for event in events:
        key = event.get("event_id")
        if key in (None, ""):
            unkeyed.append(dict(event))
            continue
        keyed[str(key)] = _latest_mapping(keyed.get(str(key)), event) or dict(event)
    return [*keyed.values(), *unkeyed]


def _db_compatible_pipeline_job_order_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    submitted_at = row.get("submitted_at")
    submitted_missing = submitted_at in (None, "")
    submitted_key = datetime.max.replace(tzinfo=UTC) if submitted_missing else _datetime_sort_key(submitted_at)
    return (
        submitted_missing,
        submitted_key,
        _datetime_sort_key(row.get("created_at")),
        str(row.get("job_id") or ""),
        str(row.get("run_id") or ""),
        str(row.get("slurm_job_id") or ""),
    )


def _decode_mapping(content: bytes, *, field: str, max_nodes: int, max_depth: int) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_malformed_json",
            field=field,
            evidence={"error_type": type(error).__name__},
        ) from error
    if not isinstance(payload, Mapping):
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    _validate_json_complexity(payload, field=field, max_nodes=max_nodes, max_depth=max_depth)
    return dict(payload)


def _validate_json_complexity(value: Any, *, field: str, max_nodes: int, max_depth: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited > max_nodes:
            raise FileOrchestrationJournalError(
                "file_journal_json_node_limit_exceeded",
                field=field,
                evidence={"max_nodes": max_nodes},
            )
        if depth > max_depth:
            raise FileOrchestrationJournalError(
                "file_journal_json_depth_exceeded",
                field=field,
                evidence={"max_depth": max_depth},
            )
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            stack.extend((child, depth + 1) for child in item)


def _require_schema(payload: Mapping[str, Any], expected: str) -> None:
    if payload.get("schema_version") != expected:
        raise FileOrchestrationJournalError(
            "file_journal_schema_mismatch",
            field="schema_version",
            evidence={"expected": expected, "actual": str(payload.get("schema_version") or "")[:80]},
        )


def _normalize_file_source_id(value: Any, *, field: str) -> str:
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    text = _scalar_text(value, field=field, invalid_reason="file_journal_invalid_identity")
    text = _safe_identity_text(text, field=field)
    try:
        return normalize_source_id(text)
    except ValueError as error:
        raise FileOrchestrationJournalError(
            "file_journal_invalid_identity",
            field=field,
            evidence={"actual": text[:80]},
        ) from error


def _required_source_id(row: Mapping[str, Any], field: str) -> str:
    return _normalize_file_source_id(row.get(field), field=field)


def _optional_source_id(row: Mapping[str, Any], field: str) -> str | None:
    value = row.get(field)
    if value in (None, ""):
        return None
    return _normalize_file_source_id(value, field=field)


def _cycle_id_for_file_source(source_id: str, cycle_time: datetime) -> str:
    return cycle_id_for(_normalize_file_source_id(source_id, field="source_id"), cycle_time)


def _blocked_cycle_id(source_id: str, cycle_time: datetime) -> str:
    try:
        return _cycle_id_for_file_source(source_id, cycle_time)
    except FileOrchestrationJournalError:
        return "file_journal_read_blocked"


def _canonical_candidate_run_id(value: str, *, source_id: str, cycle_time: datetime, model_id: str) -> str:
    cycle_stamp = format_cycle_time(cycle_time)
    match = _FORECAST_RUN_ID_RE.fullmatch(str(value))
    if match is None:
        return value
    run_source, run_cycle, run_model = match.groups()
    try:
        matches_source = _normalize_file_source_id(run_source, field="run_id") == source_id
    except FileOrchestrationJournalError:
        return value
    if matches_source and run_cycle == cycle_stamp and run_model == model_id:
        return f"fcst_{source_id.lower()}_{cycle_stamp}_{model_id}"
    return value


def _canonical_forcing_version_id(value: str, *, source_id: str, cycle_time: datetime, model_id: str) -> str:
    cycle_stamp = format_cycle_time(cycle_time)
    match = re.fullmatch(r"forc_([^_]+)_(\d{10})_(.+)", str(value))
    if match is None:
        return value
    forcing_source, forcing_cycle, forcing_model = match.groups()
    try:
        matches_source = _normalize_file_source_id(forcing_source, field="forcing_version_id") == source_id
    except FileOrchestrationJournalError:
        return value
    if matches_source and forcing_cycle == cycle_stamp and forcing_model == model_id:
        return f"forc_{source_id.lower()}_{cycle_stamp}_{model_id}"
    return value


def _canonical_candidate_id(value: str, *, source_id: str, cycle_time: datetime, model_id: str) -> str:
    text = str(value)
    candidate_source, separator, remainder = text.partition(":")
    if not separator:
        return value
    try:
        matches_source = _normalize_file_source_id(candidate_source, field="candidate_id") == source_id
    except FileOrchestrationJournalError:
        return value
    if not matches_source:
        return value
    remainder = remainder.replace(f"forecast_{candidate_source}_", f"forecast_{source_id}_", 1)
    return f"{source_id}:{remainder}"


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    return _scalar_text(value, field=field, invalid_reason="file_journal_invalid_identity")


def _required_safe_identity(row: Mapping[str, Any], field: str) -> str:
    return _safe_identity_text(_required_text(row, field), field=field)


def _optional_text(
    row: Mapping[str, Any],
    field: str,
    *,
    invalid_reason: str = "file_journal_invalid_field",
) -> str | None:
    value = row.get(field)
    if value in (None, ""):
        return None
    return _scalar_text(value, field=field, invalid_reason=invalid_reason)


def _optional_safe_identity(row: Mapping[str, Any], field: str) -> str | None:
    value = _optional_text(row, field, invalid_reason="file_journal_invalid_identity")
    if value is None:
        return None
    return _safe_identity_text(value, field=field)


def _scalar_text(value: Any, *, field: str, invalid_reason: str) -> str:
    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)
    ):
        raise FileOrchestrationJournalError(invalid_reason, field=field)
    if isinstance(value, bytes | bytearray):
        raise FileOrchestrationJournalError(invalid_reason, field=field)
    return str(value)


def _safe_identity_text(value: str, *, field: str) -> str:
    if (
        not value
        or len(value) > MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS
        or value in {".", ".."}
        or _SAFE_SEGMENT_RE.fullmatch(value) is None
    ):
        raise FileOrchestrationJournalError("file_journal_unsafe_identity", field=field)
    return value


def _validate_scheduler_visible_fields(row: Mapping[str, Any]) -> None:
    for visible_field in (
        "status",
        "stage",
        "slurm_job_id",
        "idempotency_key",
        "error_code",
        "event_type",
        "status_from",
        "status_to",
    ):
        _optional_text(row, visible_field)


def _parse_cycle_time_field(row: Mapping[str, Any], field: str) -> datetime:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    try:
        return parse_cycle_time(str(value))
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field=field) from error


def _require_source_cycle(row: Mapping[str, Any], *, source_id: str, cycle_time: datetime) -> None:
    expected_source = _normalize_file_source_id(source_id, field="source_id")
    actual_source = _required_source_id(row, "source_id")
    if actual_source != expected_source:
        raise FileOrchestrationJournalError(
            "file_journal_source_mismatch",
            field="source_id",
            evidence={"expected": expected_source, "actual": actual_source[:80]},
        )
    parsed_cycle_time = _parse_cycle_time_field(row, "cycle_time")
    if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
        raise FileOrchestrationJournalError(
            "file_journal_cycle_mismatch",
            field="cycle_time",
            evidence={"expected": _format_utc(cycle_time), "actual": _format_utc(parsed_cycle_time)},
        )


def _require_cycle_id(row: Mapping[str, Any], expected_cycle_id: str) -> None:
    actual = _required_safe_identity(row, "cycle_id")
    if actual != expected_cycle_id:
        raise FileOrchestrationJournalError(
            "file_journal_cycle_id_mismatch",
            field="cycle_id",
            evidence={"expected": expected_cycle_id, "actual": actual[:80]},
        )


def _require_model_id(row: Mapping[str, Any], expected_model_id: str, *, required: bool) -> None:
    actual = _optional_safe_identity(row, "model_id")
    if actual is None:
        if required:
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        return
    if actual != expected_model_id:
        raise FileOrchestrationJournalError(
            "file_journal_model_mismatch",
            field="model_id",
            evidence={"expected": expected_model_id, "actual": actual[:80]},
        )


_RECORD_PAYLOAD_IDENTITY_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "hydro_run": (
        ("run_id", "file_journal_run_mismatch"),
        ("model_id", "file_journal_model_mismatch"),
    ),
    "forecast_cycle": (("cycle_id", "file_journal_cycle_id_mismatch"),),
    "forcing_version": (
        ("forcing_version_id", "file_journal_forcing_version_mismatch"),
        ("model_id", "file_journal_model_mismatch"),
    ),
    "model_context": (("model_id", "file_journal_model_mismatch"),),
    "pipeline_job": (
        ("job_id", "file_journal_job_mismatch"),
        ("run_id", "file_journal_run_mismatch"),
        ("model_id", "file_journal_model_mismatch"),
    ),
    "pipeline_event": (
        ("event_id", "file_journal_event_mismatch"),
        ("entity_id", "file_journal_job_mismatch"),
    ),
}


def _require_record_payload_identity_match(
    record_type: str,
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    for identity_field, reason in _RECORD_PAYLOAD_IDENTITY_FIELDS.get(record_type, ()):
        envelope_value = _optional_safe_identity(record, identity_field)
        payload_value = _optional_safe_identity(payload, identity_field)
        if envelope_value is not None and payload_value is not None and envelope_value != payload_value:
            raise FileOrchestrationJournalError(
                reason,
                field=identity_field,
                evidence={"expected": envelope_value, "actual": payload_value[:80]},
            )


def _record_model_id(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
) -> str | None:
    envelope_model_id = _optional_safe_identity(record, "model_id")
    payload_model_id = _optional_safe_identity(payload, "model_id")
    envelope_run_model_id = _model_id_from_run_identity(
        record.get("run_id"),
        source_id=source_id,
        cycle_time=cycle_time,
    )
    payload_run_model_id = _model_id_from_run_identity(
        payload.get("run_id"),
        source_id=source_id,
        cycle_time=cycle_time,
    )
    if (
        envelope_run_model_id is not None
        and payload_run_model_id is not None
        and envelope_run_model_id != payload_run_model_id
    ):
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": envelope_run_model_id, "actual": payload_run_model_id[:80]},
        )
    if envelope_model_id is not None and payload_model_id is not None and envelope_model_id != payload_model_id:
        raise FileOrchestrationJournalError(
            "file_journal_model_mismatch",
            field="model_id",
            evidence={"expected": envelope_model_id, "actual": payload_model_id[:80]},
        )
    explicit_model_id = envelope_model_id if envelope_model_id is not None else payload_model_id
    inferred_run_model_id = envelope_run_model_id if envelope_run_model_id is not None else payload_run_model_id
    if (
        explicit_model_id is not None
        and inferred_run_model_id is not None
        and explicit_model_id != inferred_run_model_id
    ):
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": explicit_model_id, "actual": inferred_run_model_id[:80]},
        )
    return explicit_model_id if explicit_model_id is not None else inferred_run_model_id


def _explicit_record_model_id(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    envelope_model_id = _optional_safe_identity(record, "model_id")
    payload_model_id = _optional_safe_identity(payload, "model_id")
    if envelope_model_id is not None and payload_model_id is not None and envelope_model_id != payload_model_id:
        raise FileOrchestrationJournalError(
            "file_journal_model_mismatch",
            field="model_id",
            evidence={"expected": envelope_model_id, "actual": payload_model_id[:80]},
        )
    return envelope_model_id if envelope_model_id is not None else payload_model_id


def _model_id_from_run_identity(value: Any, *, source_id: str, cycle_time: datetime) -> str | None:
    if value in (None, ""):
        return None
    run_id = _safe_identity_text(
        _scalar_text(value, field="run_id", invalid_reason="file_journal_invalid_identity"),
        field="run_id",
    )
    cycle_stamp = format_cycle_time(cycle_time)
    forecast_match = _FORECAST_RUN_ID_RE.fullmatch(run_id)
    if forecast_match is not None:
        run_source, run_cycle, run_model = forecast_match.groups()
        if _normalize_file_source_id(run_source, field="run_id") == source_id and run_cycle == cycle_stamp:
            return _safe_identity_text(run_model, field="run_id")
        return None
    cycle_match = _CYCLE_RUN_ID_RE.fullmatch(run_id)
    if cycle_match is not None:
        run_source, run_cycle = cycle_match.groups()
        if _normalize_file_source_id(run_source, field="run_id") == source_id and run_cycle == cycle_stamp:
            return None
    return None


def _validate_hydro_run_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> None:
    _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    _require_model_id(row, model_id, required=True)
    actual_run_id = _required_safe_identity(row, "run_id")
    cycle_stamp = format_cycle_time(cycle_time)
    expected_forecast_run_id = f"fcst_{source_id.lower()}_{cycle_stamp}_{model_id}"
    expected_cycle_run_prefix = f"cycle_{source_id.lower()}_{cycle_stamp}"
    if actual_run_id != expected_forecast_run_id and (
        actual_run_id != expected_cycle_run_prefix
        and not actual_run_id.startswith(f"{expected_cycle_run_prefix}_")
    ):
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={
                "expected": f"{expected_forecast_run_id}|{expected_cycle_run_prefix}",
                "actual": actual_run_id[:80],
            },
        )
    _validate_scheduler_visible_fields(row)


def _validate_forecast_cycle_identity(row: Mapping[str, Any], *, source_id: str, cycle_time: datetime) -> None:
    _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    _require_cycle_id(row, _cycle_id_for_file_source(source_id, cycle_time))
    _validate_scheduler_visible_fields(row)


def _validate_forcing_version_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    require_forcing_version_id: bool = True,
    require_source_cycle: bool = False,
    require_model_id: bool = False,
) -> None:
    source_id = _normalize_file_source_id(source_id, field="source_id")
    if require_source_cycle:
        _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    else:
        actual_source = _optional_source_id(row, "source_id")
        if actual_source is not None and actual_source != source_id:
            raise FileOrchestrationJournalError(
                "file_journal_source_mismatch",
                field="source_id",
                evidence={"expected": source_id, "actual": actual_source[:80]},
            )
        if row.get("cycle_time") not in (None, ""):
            parsed_cycle_time = _parse_cycle_time_field(row, "cycle_time")
            if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
                raise FileOrchestrationJournalError(
                    "file_journal_cycle_mismatch",
                    field="cycle_time",
                    evidence={"expected": _format_utc(cycle_time), "actual": _format_utc(parsed_cycle_time)},
                )
    _require_model_id(row, model_id, required=require_model_id)
    forcing_version_id = row.get("forcing_version_id")
    if forcing_version_id in (None, ""):
        if require_forcing_version_id:
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="forcing_version_id")
        return
    expected_prefix = f"forc_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
    actual_forcing_version_id = _required_safe_identity(row, "forcing_version_id")
    if actual_forcing_version_id != expected_prefix:
        raise FileOrchestrationJournalError(
            "file_journal_forcing_version_mismatch",
            field="forcing_version_id",
            evidence={"expected": expected_prefix, "actual": actual_forcing_version_id[:80]},
        )


def _validate_model_context_identity(row: Mapping[str, Any], *, model_id: str) -> None:
    _require_model_id(row, model_id, required=True)


def _validate_pipeline_job_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
    expected_job_id: str | None = None,
) -> None:
    source_id = _normalize_file_source_id(source_id, field="source_id")
    job_id = _required_safe_identity(row, "job_id")
    if expected_job_id is not None and job_id != expected_job_id:
        raise FileOrchestrationJournalError(
            "file_journal_job_mismatch",
            field="job_id",
            evidence={"expected": expected_job_id, "actual": job_id[:80]},
        )
    actual_source = _optional_source_id(row, "source_id")
    if actual_source is not None:
        if actual_source != source_id:
            raise FileOrchestrationJournalError(
                "file_journal_source_mismatch",
                field="source_id",
                evidence={"expected": source_id, "actual": actual_source[:80]},
            )
    if row.get("cycle_time") not in (None, ""):
        parsed_cycle_time = _parse_cycle_time_field(row, "cycle_time")
        if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
            raise FileOrchestrationJournalError(
                "file_journal_cycle_mismatch",
                field="cycle_time",
                evidence={"expected": _format_utc(cycle_time), "actual": _format_utc(parsed_cycle_time)},
            )
    _require_cycle_id(row, _cycle_id_for_file_source(source_id, cycle_time))
    _validate_scheduler_visible_fields(row)
    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    if model_id not in (None, ""):
        _require_model_id(row, str(model_id), required=False)
        candidate_run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
        cycle_run_prefix = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
        run_id = _required_safe_identity(row, "run_id")
        if run_id != candidate_run_id and run_id != cycle_run_id and not run_id.startswith(f"{cycle_run_prefix}_"):
            raise FileOrchestrationJournalError(
                "file_journal_run_mismatch",
                field="run_id",
                evidence={"expected": f"{candidate_run_id}|{cycle_run_prefix}", "actual": run_id[:80]},
            )
        return
    run_id = _required_safe_identity(row, "run_id")
    if run_id != cycle_run_id and not run_id.startswith(f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_"):
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": cycle_run_id, "actual": run_id[:80]},
        )


def _validate_event_identity(row: Mapping[str, Any]) -> None:
    _optional_text(row, "event_id")
    _required_safe_identity(row, "entity_id")
    entity_type = _optional_text(row, "entity_type", invalid_reason="file_journal_invalid_identity")
    if entity_type not in (None, "", "pipeline_job"):
        raise FileOrchestrationJournalError(
            "file_journal_event_entity_type_mismatch",
            field="entity_type",
            evidence={"expected": "pipeline_job", "actual": str(entity_type)[:80]},
        )
    _validate_scheduler_visible_fields(row)


def _validate_payload_identity(
    record_type: str,
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
) -> None:
    if record_type == "hydro_run":
        if model_id in (None, ""):
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        _validate_hydro_run_identity(payload, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
    elif record_type == "forecast_cycle":
        _validate_forecast_cycle_identity(payload, source_id=source_id, cycle_time=cycle_time)
    elif record_type == "forcing_version":
        if model_id in (None, ""):
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        _validate_forcing_version_identity(payload, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
    elif record_type == "model_context":
        if model_id in (None, ""):
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        _validate_model_context_identity(payload, model_id=model_id)


def _latest_identity_from_path(path: Path, *, root: Path) -> tuple[str, datetime, str]:
    parts = path.relative_to(root).parts
    if len(parts) != 4 or parts[0] != "latest":
        raise FileOrchestrationJournalError(
            "file_journal_path_identity_mismatch",
            field=str(_relative_evidence(path, root)),
        )
    source_id = _normalize_file_source_id(parts[1], field="source_id")
    cycle_segment = _safe_segment(parts[2])
    model_id = _safe_segment(Path(parts[3]).stem)
    return source_id, _parse_cycle_segment(cycle_segment, field=str(_relative_evidence(path, root))), model_id


def _journal_identity_from_path(path: Path, *, root: Path, surface: str) -> tuple[str, datetime]:
    parts = path.relative_to(root).parts
    if len(parts) != 3 or parts[0] != surface:
        raise FileOrchestrationJournalError(
            "file_journal_path_identity_mismatch",
            field=str(_relative_evidence(path, root)),
        )
    source_id = _normalize_file_source_id(parts[1], field="source_id")
    cycle_segment = _safe_segment(Path(parts[2]).stem)
    return source_id, _parse_cycle_segment(cycle_segment, field=str(_relative_evidence(path, root)))


def _parse_cycle_segment(value: str, *, field: str) -> datetime:
    try:
        return parse_cycle_time(value)
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field=field) from error


def _iter_regular_json_files(
    directory: Path,
    *,
    root: Path,
    recursive: bool = False,
    max_files: int,
    max_depth: int,
) -> Iterable[Path]:
    yield from _iter_discovered_files(
        directory,
        root=root,
        suffix=".json",
        recursive=recursive,
        max_files=max_files,
        max_depth=max_depth,
    )


def _iter_jsonl_files(directory: Path, *, root: Path, max_files: int, max_depth: int) -> Iterable[Path]:
    yield from _iter_discovered_files(
        directory,
        root=root,
        suffix=".jsonl",
        recursive=True,
        max_files=max_files,
        max_depth=max_depth,
    )


def _iter_discovered_files(
    directory: Path,
    *,
    root: Path,
    suffix: str,
    recursive: bool,
    max_files: int,
    max_depth: int,
) -> Iterable[Path]:
    scanned_entries = 0

    def walk(current: Path, depth: int) -> Iterable[Path]:
        nonlocal scanned_entries
        if depth > max_depth:
            raise FileOrchestrationJournalError(
                "file_journal_depth_limit_exceeded",
                field=str(_relative_evidence(current, root)),
                evidence={"max_depth": max_depth},
            )
        try:
            current_mode = stat_no_follow(current, containment_root=root).st_mode
        except FileNotFoundError:
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unsafe_scanned_entry",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        if not stat.S_ISDIR(current_mode):
            raise FileOrchestrationJournalError(
                "file_journal_unsafe_scanned_entry",
                field=str(_relative_evidence(current, root)),
                evidence={"entry_type": "not_directory"},
            )
        remaining_entries = max_files - scanned_entries
        if remaining_entries < 0:
            raise FileOrchestrationJournalError(
                "file_journal_file_limit_exceeded",
                field=str(_relative_evidence(directory, root)),
                evidence={"max_files": max_files},
            )
        try:
            entry_names = list_directory_no_follow_limited(
                current,
                containment_root=root,
                max_entries=remaining_entries,
            )
        except FileNotFoundError:
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        if len(entry_names) > remaining_entries:
            raise FileOrchestrationJournalError(
                "file_journal_file_limit_exceeded",
                field=str(_relative_evidence(directory, root)),
                evidence={"max_files": max_files},
            )
        scanned_entries += len(entry_names)
        for entry_name in sorted(entry_names):
            if _SAFE_SEGMENT_RE.fullmatch(entry_name) is None:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_path_segment",
                    field=str(_relative_evidence(current / entry_name, root)),
                )
            entry = current / entry_name
            try:
                mode = stat_no_follow(entry, containment_root=root).st_mode
            except FileNotFoundError:
                continue
            except (OSError, SafeFilesystemError) as error:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_scanned_entry",
                    field=str(_relative_evidence(entry, root)),
                    evidence={"error_type": type(error).__name__},
                ) from error
            if stat.S_ISDIR(mode):
                if recursive:
                    yield from walk(entry, depth + 1)
                continue
            if entry_name.endswith(suffix):
                if not stat.S_ISREG(mode):
                    raise FileOrchestrationJournalError(
                        "file_journal_unsafe_scanned_entry",
                        field=str(_relative_evidence(entry, root)),
                        evidence={"entry_type": "not_regular_file"},
                    )
                yield entry

    yield from walk(directory, 0)


def _safe_segment(value: str) -> str:
    text = str(value)
    if (
        not text
        or len(text) > MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS
        or text in {".", ".."}
        or _SAFE_SEGMENT_RE.fullmatch(text) is None
    ):
        raise FileOrchestrationJournalError("file_journal_unsafe_path_segment", field="path")
    return text


def _relative_evidence(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return Path("[local-path]")


def _model_context_from_mapping(row: Mapping[str, Any], *, model_id: str) -> ModelContext:
    _validate_model_context_identity(row, model_id=model_id)
    return ModelContext(
        model_id=_required_safe_identity(row, "model_id"),
        basin_id=_optional_str(row.get("basin_id"), field="basin_id"),
        basin_version_id=_required_context_str(row, "basin_version_id"),
        river_network_version_id=_required_context_str(row, "river_network_version_id"),
        segment_count=_required_int(row, "segment_count"),
        model_package_uri=_required_context_str(row, "model_package_uri"),
        output_segment_count=_optional_int(row.get("output_segment_count"), field="output_segment_count"),
        model_package_checksum=_optional_str(
            row.get("model_package_checksum") or row.get("package_checksum"),
            field="model_package_checksum",
        ),
    )


def _forcing_context_from_mapping(row: Mapping[str, Any]) -> ForcingContext:
    lineage = _lineage_mapping(row.get("lineage_json"))
    return ForcingContext(
        _optional_str(row.get("forcing_version_id"), field="forcing_version_id"),
        _optional_str(row.get("forcing_package_uri"), field="forcing_package_uri"),
        _optional_datetime(row.get("start_time"), field="start_time"),
        _optional_datetime(row.get("end_time"), field="end_time"),
        _optional_str(row.get("source_id"), field="source_id"),
        _optional_int(
            _fallback_value(row.get("max_lead_hours"), lineage.get("max_lead_hours")),
            field="max_lead_hours",
        ),
        _optional_str(
            _fallback_value(
                row.get("forcing_package_manifest_uri"),
                lineage.get("forcing_package_manifest_uri"),
            ),
            field="forcing_package_manifest_uri",
        ),
        _optional_str(
            _fallback_value(
                row.get("forcing_package_manifest_checksum"),
                lineage.get("forcing_package_manifest_checksum"),
            ),
            field="forcing_package_manifest_checksum",
        ),
    )


def _fallback_value(primary: Any, fallback: Any) -> Any:
    return fallback if primary in (None, "") else primary


def _lineage_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, Mapping):
        return value
    return {}


def _optional_str(value: Any, *, field: str = "value") -> str | None:
    if value in (None, ""):
        return None
    return _scalar_text(value, field=field, invalid_reason="file_journal_invalid_field")


def _required_context_str(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_field", field=field)
    return _scalar_text(value, field=field, invalid_reason="file_journal_invalid_field")


def _required_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_field", field=field)
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field=field) from error


def _optional_int(value: Any, *, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field=field) from error


def _optional_datetime(value: Any, *, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field=field) from error
