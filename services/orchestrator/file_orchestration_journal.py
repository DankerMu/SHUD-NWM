from __future__ import annotations

import hashlib
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
from packages.common.redaction import is_sensitive_key
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    list_directory_no_follow_limited,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from packages.common.slurm_env import secret_manifest_value_reason
from packages.common.source_identity import normalize_source_id
from services.orchestrator import chain_repository_state
from services.orchestrator.accepted_submit_identity import ordered_cohort_members
from services.orchestrator.chain_repository import (
    ACTIVE_HYDRO_STATUSES,
    COMPLETED_HYDRO_STATUSES,
    DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
)
from services.orchestrator.chain_source_cycle import _datetime_sort_key
from services.orchestrator.chain_types import ForcingContext, ModelContext, OrchestratorError
from services.orchestrator.retry import (
    _DB_FREE_REQUIRED_SELECTOR_FIELDS,
    _DB_FREE_RUNTIME_FIELDS,
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
    _candidate_batch_db_free_required,
    _event_details_is_manual_retry_submission,
    _has_runtime_root_field,
    _mapping_at,
    _resolve_db_free_runtime_candidate,
    _resolve_runtime_root_candidate,
    _retry_submission_error_code,
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

FILE_LOCK_GUARD_MODE_ENV = "NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE"
FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION = "nhms.scheduler.file_orchestration_journal.v1"
FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION = "nhms.scheduler.file_orchestration_latest.v1"
FILE_ORCHESTRATION_PRIVATE_RECOVERY_SCHEMA_VERSION = "nhms.scheduler.file_orchestration_private_recovery.v1"
MAX_FILE_JOURNAL_JSON_BYTES = 16 * 1024 * 1024
MAX_FILE_JOURNAL_RECORDS = 100_000
MAX_FILE_JOURNAL_DISCOVERED_FILES = 100_000
MAX_FILE_JOURNAL_SCAN_DEPTH = 32
MAX_FILE_JOURNAL_JSON_DEPTH = 64
MAX_FILE_JOURNAL_JSON_NODES = 300_000
MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS = 255
MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES = 512
MAX_FILE_JOURNAL_READ_CACHE_ENTRIES = 4096
MAX_FILE_JOURNAL_READ_CACHE_BYTES = 64 * 1024 * 1024
FILE_RECONCILE_SCAN_LIMIT_ENV = "NHMS_FILE_RECONCILE_SCAN_LIMIT"
DEFAULT_FILE_RECONCILE_SCAN_LIMIT = 512
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
    "previous_job_id",
    "error_code",
    "error_message",
    "log_uri",
    "submit_outcome",
    "slurm_comment",
    "cohort_members",
    "cohort_digest",
    "restart_stage",
    "submission_attempt",
    "submission_attempt_started_at",
    "expected_slurm_user",
    "expected_slurm_account",
    "slurm_ownership_required",
    "reconciliation_source",
    "reconciliation_decision",
    "matched_slurm_job_id",
    "candidate_projections",
    "native_shud_resubmitted",
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
_PRIVATE_RUNTIME_ROOT_RECOVERY_RECORD_TYPE = "pipeline_event_runtime_root_recovery"
_RUNTIME_ROOT_SAME_RUN_JOB_SCAN_LIMIT = 32
_SUPPORTED_PIPELINE_EVENT_ENTITY_TYPES = {"pipeline_job", "forecast_cycle"}
_ARRAY_MANUAL_RETRY_JOB_TYPES = frozenset(
    {
        "hindcast",
        "produce_forcing_array",
        "run_shud_forecast_array",
        "parse_output_array",
    }
)
_ARRAY_MANUAL_RETRY_MANIFEST_INDEX_NAMES = {
    "produce_forcing_array": "forcing_manifest_index.json",
    "run_shud_forecast_array": "forecast_manifest_index.json",
    "parse_output_array": "parse_manifest_index.json",
    "hindcast": "hindcast_manifest_index.json",
}

TERMINAL_PIPELINE_STATUSES = {
    "succeeded",
    "partially_failed",
    "failed",
    "cancelled",
    "submission_failed",
    "reservation_lost",
    "permanently_failed",
}
_TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES = {"complete", "succeeded", "parsed", "published"}
_STAGE_STATUS_ORDER = {
    "download": 1,
    "download_gfs": 1,
    "download_source_cycle": 1,
    "convert": 2,
    "convert_canonical": 2,
    "forcing": 3,
    "produce_forcing": 3,
    "forecast": 4,
    "run_shud_forecast": 4,
    "parse": 5,
    "state_save_qc": 6,
    "publish": 8,
    "era5_download": 11,
    "canonical_convert": 12,
    "forcing_produce": 13,
    "analysis_run": 14,
    "parse_output": 15,
}
_UNKNOWN_STAGE_STATUS_ORDER = 99

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


def _submit_file_manual_retry_job(gateway: Any, request: SubmitJobRequest) -> Any:
    job_type = request.resolved_job_type()
    if job_type in _ARRAY_MANUAL_RETRY_JOB_TYPES:
        submit_job_array = getattr(gateway, "submit_job_array", None)
        if callable(submit_job_array):
            return submit_job_array(request)
    return gateway.submit_job(request)


def _file_manual_retry_array_tasks(
    retry_job: _RetrySubmissionJob,
    runtime_root_fields: Mapping[str, str] | None,
) -> list[dict[str, Any]] | None:
    filename = _ARRAY_MANUAL_RETRY_MANIFEST_INDEX_NAMES.get(str(retry_job.job_type or ""))
    if filename is None:
        return None
    run_id = str(retry_job.run_id or "")
    if not run_id or _SAFE_SEGMENT_RE.fullmatch(run_id) is None:
        return None
    workspace_dir = str((runtime_root_fields or {}).get("workspace_dir") or os.getenv("WORKSPACE_ROOT") or "")
    if not workspace_dir:
        return None
    workspace_root = Path(workspace_dir).expanduser().resolve()
    manifest_index_path = workspace_root / "runs" / run_id / "input" / filename
    try:
        payload = json.loads(
            read_bytes_limited_no_follow(
                manifest_index_path,
                max_bytes=MAX_FILE_JOURNAL_JSON_BYTES,
                containment_root=workspace_root,
            ).decode("utf-8")
        )
    except (FileNotFoundError, OSError, SafeFilesystemError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(payload, list):
        tasks = payload
    elif isinstance(payload, Mapping):
        tasks = payload.get("tasks") or payload.get("manifests") or payload.get("basins")
    else:
        return None
    if not isinstance(tasks, Sequence) or isinstance(tasks, str | bytes | bytearray):
        return None
    return [dict(task) for task in tasks if isinstance(task, Mapping)]


@dataclass
class _CycleRows:
    hydro_run: dict[str, Any] | None = None
    forecast_cycle: dict[str, Any] | None = None
    forcing_version: dict[str, Any] | None = None
    model_context: dict[str, Any] | None = None
    pipeline_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    pipeline_events: list[dict[str, Any]] = field(default_factory=list)
    replay: dict[str, Any] = field(default_factory=dict)


def _clone_cycle_rows(rows: _CycleRows) -> _CycleRows:
    return _CycleRows(
        hydro_run=dict(rows.hydro_run) if isinstance(rows.hydro_run, Mapping) else None,
        forecast_cycle=dict(rows.forecast_cycle) if isinstance(rows.forecast_cycle, Mapping) else None,
        forcing_version=dict(rows.forcing_version) if isinstance(rows.forcing_version, Mapping) else None,
        model_context=dict(rows.model_context) if isinstance(rows.model_context, Mapping) else None,
        pipeline_jobs={str(job_id): dict(job) for job_id, job in rows.pipeline_jobs.items()},
        pipeline_events=[dict(event) for event in rows.pipeline_events],
        replay=dict(rows.replay),
    )


def _filter_cycle_rows_for_model(
    rows: _CycleRows,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> None:
    scoped_forecast_cycle = _candidate_scoped_forecast_cycle(rows.forecast_cycle)
    cycle_terminated = rows.forecast_cycle is not None and scoped_forecast_cycle is None
    rows.forecast_cycle = scoped_forecast_cycle
    rows.hydro_run = (
        rows.hydro_run
        if _row_matches_candidate(rows.hydro_run, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        else None
    )
    rows.forcing_version = (
        rows.forcing_version
        if _row_matches_candidate(rows.forcing_version, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        else None
    )
    rows.model_context = (
        rows.model_context
        if _row_matches_candidate(rows.model_context, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        else None
    )
    rows.pipeline_jobs = {
        job_id: job
        for job_id, job in rows.pipeline_jobs.items()
        if _job_matches_candidate(job, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
    }
    rows.pipeline_events = [
        event
        for event in rows.pipeline_events
        if _event_matches_candidate_rows(
            event,
            source_id=source_id,
            cycle_time=cycle_time,
            pipeline_jobs=rows.pipeline_jobs,
            forecast_cycle=rows.forecast_cycle,
            cycle_terminated=cycle_terminated,
        )
    ]


@dataclass(frozen=True)
class _CycleSourceDiscovery:
    source_id: str
    source_segments: tuple[str, ...]

    @property
    def source_segment(self) -> str:
        return self.source_segments[0]


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
    # Capability marker used by the shared submit/reconcile paths.  Legacy and
    # PostgreSQL repositories deliberately keep their historical behaviour.
    supports_accepted_submit_reconcile = True
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
        self._cycle_rows_cache: dict[
            tuple[str, str, str | None, tuple[str, ...]],
            tuple[tuple[Any, ...] | None, _CycleRows],
        ] = {}
        self._direct_jobs_cycle_cache: dict[
            tuple[str, str],
            tuple[tuple[int, int, int] | None, list[dict[str, Any]]],
        ] = {}
        self._read_bytes_cache: dict[str, tuple[tuple[int, int, int], bytes, bool]] = {}
        self._read_bytes_cache_total = 0

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=None)
        except FileOrchestrationJournalError:
            return True
        return any(_job_is_active(job) for job in _current_terminal_jobs(rows.pipeline_jobs.values()))

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return True
        candidate_jobs = [
            job
            for job in _current_terminal_jobs(rows.pipeline_jobs.values())
            if _job_matches_candidate(job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        ]
        has_terminal_completion = any(
            _job_is_terminal_success(job) and _job_is_current_terminal_completion(job) for job in candidate_jobs
        )
        hydro_run = rows.hydro_run
        if _row_matches_candidate(hydro_run, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id):
            if str(hydro_run.get("status") or "") in ACTIVE_HYDRO_STATUSES and not has_terminal_completion:
                return True
        return any(_job_is_active(job) for job in candidate_jobs)

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return False
        hydro_run = rows.hydro_run
        hydro_run_matches = _row_matches_candidate(
            hydro_run,
            source_id=canonical_source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        )
        if hydro_run is not None and not hydro_run_matches:
            return False
        if hydro_run_matches and str(hydro_run.get("status") or "") in COMPLETED_HYDRO_STATUSES:
            return True
        return any(
            _job_is_terminal_success(job)
            and _job_is_current_terminal_completion(job)
            and _job_matches_candidate(job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
            for job in _current_terminal_jobs(rows.pipeline_jobs.values())
        )

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
            for job in _current_terminal_jobs(rows.pipeline_jobs.values())
            if _file_journal_real_slurm_job_id(job.get("slurm_job_id"))
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
        run_manifest_identity = _run_manifest_model_package_identity(rows.hydro_run)
        if run_manifest_identity is not None:
            state["run_manifest_model_package"] = run_manifest_identity
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

    def query_reserved_unbound_jobs(self) -> list[SimpleNamespace]:
        jobs = [
            _file_reconcile_namespace(job)
            for job in self._iter_reconcile_pipeline_job_records()
            if str(job.get("status") or "") == "reserved"
            and job.get("slurm_job_id") in (None, "")
            and job.get("idempotency_key") not in (None, "")
        ]
        jobs.sort(key=lambda job: (_datetime_sort_key(job.created_at), str(job.job_id)))
        return jobs

    def query_inflight_jobs(self) -> list[SimpleNamespace]:
        jobs = [
            _file_reconcile_namespace(job)
            for job in self._iter_reconcile_pipeline_job_records()
            if str(job.get("status") or "") in {"pending", "queued", "submitted", "running", "reconcile_unverified"}
            and _file_journal_real_slurm_job_id(job.get("slurm_job_id"))
        ]
        jobs.sort(
            key=lambda job: (
                _datetime_sort_key(job.submitted_at),
                _datetime_sort_key(job.created_at),
                str(job.job_id),
            )
        )
        return jobs

    def bind_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> SimpleNamespace | None:
        bound = self.bind_pipeline_job_reservation(
            idempotency_key,
            slurm_job_id=slurm_job_id,
            status=status,
            array_task_id=array_task_id,
        )
        return _file_reconcile_namespace(bound) if bound is not None else None

    def update_job_status(
        self,
        job_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> SimpleNamespace:
        _previous_status, updated = self.update_pipeline_job_status(
            job_id,
            status,
            error_code=error_code,
            error_message=error_message,
        )
        return _file_reconcile_namespace(updated)

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
        model = manifest.get("model") if isinstance(manifest.get("model"), Mapping) else {}
        row = {
            "run_id": str(context.run_id),
            "candidate_id": manifest.get("candidate_id"),
            "run_type": manifest.get("run_type", "forecast"),
            "scenario_id": manifest["scenario_id"],
            "model_id": str(context.model_id),
            "basin_id": model.get("basin_id") or getattr(context, "basin_id", None),
            "array_task_id": manifest.get("array_task_id", getattr(context, "array_task_id", None)),
            "basin_version_id": str(context.basin_version_id),
            "forcing_version_id": str(context.forcing_version_id),
            "init_state_id": getattr(context, "init_state_id", None) or init_state.get("state_id"),
            "source_id": _normalize_file_source_id(context.source_id, field="source_id"),
            "cycle_time": _format_utc(context.cycle_time),
            "start_time": _format_utc(context.start_time),
            "end_time": _format_utc(context.end_time),
            "status": "created",
            "submission_attempt": max(int(manifest.get("submission_attempt") or 1), 1),
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
            "candidate_id": manifest.get("candidate_id"),
            "run_type": manifest.get("run_type", "forecast"),
            "scenario_id": manifest["scenario_id"],
            "model_id": str(model["model_id"]),
            "basin_id": model.get("basin_id") or basin.get("basin_id"),
            "array_task_id": basin.get("task_id"),
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
            "submission_attempt": max(int(manifest.get("submission_attempt") or 1), 1),
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

    def forecast_cohort_runtime_identity_matches(self, identity: Mapping[str, Any]) -> bool:
        """Validate accepted-submit members against independently written run manifests."""
        members = ordered_cohort_members(identity.get("cohort_members"))
        if not members:
            return False
        try:
            source_id = _normalize_file_source_id(identity.get("source_id"), field="source_id")
            cycle_id = _required_safe_identity(identity, "cycle_id")
            cycle_time = parse_cycle_time(cycle_id.split("_", maxsplit=1)[1])
            submission_attempt = max(int(identity.get("submission_attempt") or 1), 1)
            expected_cycle_time = _format_utc(cycle_time)
            for member in members:
                hydro_run = self._hydro_run_for(str(member.get("run_id") or ""))
                if hydro_run is None:
                    return False
                if {
                    "candidate_id": str(hydro_run.get("candidate_id") or ""),
                    "run_id": str(hydro_run.get("run_id") or ""),
                    "model_id": str(hydro_run.get("model_id") or ""),
                    "basin_id": str(hydro_run.get("basin_id") or ""),
                    "scenario_id": str(hydro_run.get("scenario_id") or ""),
                    "array_task_id": int(hydro_run.get("array_task_id")),
                } != {
                    "candidate_id": str(member.get("candidate_id") or ""),
                    "run_id": str(member.get("run_id") or ""),
                    "model_id": str(member.get("model_id") or ""),
                    "basin_id": str(member.get("basin_id") or ""),
                    "scenario_id": str(member.get("scenario_id") or ""),
                    "array_task_id": int(member.get("array_task_id")),
                }:
                    return False
                if (
                    _normalize_file_source_id(hydro_run.get("source_id"), field="source_id") != source_id
                    or _format_utc(_parse_cycle_time_field(hydro_run, "cycle_time")) != expected_cycle_time
                    or max(int(hydro_run.get("submission_attempt") or 1), 1) != submission_attempt
                ):
                    return False
        except (FileOrchestrationJournalError, IndexError, TypeError, ValueError):
            return False
        return True

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
            safe_error_message = _durable_error_message(error_message)
            row.update({"status": status, "updated_at": _format_utc(_utcnow())})
            for key, value in (("slurm_job_id", slurm_job_id),):
                if value is not None:
                    row[key] = value
            if status in {"pending", "created", "succeeded", "complete", "parsed", "published"}:
                row["error_code"] = error_code
                row["error_message"] = safe_error_message
            else:
                if error_code is not None:
                    row["error_code"] = error_code
                if error_message is not None:
                    row["error_message"] = safe_error_message
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
            row["submission_attempt"] = max(
                int(existing.get("submission_attempt") or 1) + 1,
                int(request_row.get("submission_attempt") or 1),
            )
            row["submission_attempt_started_at"] = request_row.get("submission_attempt_started_at") or _format_utc(
                _utcnow()
            )
            for key in (
                "run_id",
                "cycle_id",
                "model_id",
                "stage",
                "candidate_id",
                "job_type",
                "slurm_comment",
                "cohort_members",
                "cohort_digest",
                "restart_stage",
                "expected_slurm_user",
                "expected_slurm_account",
                "slurm_ownership_required",
                "native_shud_resubmitted",
            ):
                if key in request_row and request_row.get(key) not in (None, ""):
                    row[key] = request_row[key]
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
                    "submit_outcome": "accepted",
                    "submitted_at": row.get("submitted_at") or _format_utc(_utcnow()),
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            if array_task_id is not None:
                row["array_task_id"] = array_task_id
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def record_pipeline_job_reconciliation(
        self,
        job_id: str,
        *,
        submit_outcome: str | None = None,
        reconciliation_decision: str | None = None,
        matched_slurm_job_id: str | None = None,
        candidate_projections: Sequence[Mapping[str, Any]] | None = None,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically append bounded accepted-submit reconciliation evidence."""
        initial = self._pipeline_job_for_id_unlocked(job_id)
        if initial is None:
            return None
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(job_id)
            if existing is None:
                return None
            row = dict(existing)
            if submit_outcome is not None:
                row["submit_outcome"] = submit_outcome
            if reconciliation_decision is not None:
                row["reconciliation_source"] = "slurm_exact_comment"
                row["reconciliation_decision"] = reconciliation_decision
                row["matched_slurm_job_id"] = matched_slurm_job_id
            if candidate_projections is not None:
                row["candidate_projections"] = [dict(item) for item in candidate_projections]
            if status is not None:
                row["status"] = status
            row["updated_at"] = _format_utc(_utcnow())
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def permit_pipeline_job_retry(self, job_id: str) -> int:
        """Move one still-reserved cohort to retryable exactly once under its cycle lock."""
        initial = self._pipeline_job_for_id_unlocked(job_id)
        if initial is None:
            return 0
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(job_id)
            if existing is None or str(existing.get("status") or "") != "reserved":
                return 0
            if existing.get("slurm_job_id") not in (None, ""):
                return 0
            cohort_row = dict(existing)
            cohort_row.update(
                {
                    "status": "reservation_lost",
                    "reconciliation_source": "slurm_exact_comment",
                    "reconciliation_decision": "absence_retry_permitted",
                    "matched_slurm_job_id": None,
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            attempt = max(int(existing.get("submission_attempt") or 1), 1)
            source_id = _source_id_from_job(existing)
            cycle_time = _cycle_time_from_job(existing)
            payloads: list[tuple[str, dict[str, Any], str | None]] = []
            touched_models: set[str] = set()
            for member in _bounded_cohort_members(existing.get("cohort_members")):
                run_id = str(member.get("run_id") or "")
                model_id = str(member.get("model_id") or "")
                if not run_id or not model_id:
                    continue
                hydro = self._hydro_run_for(run_id)
                if (
                    hydro is None
                    or int(hydro.get("submission_attempt") or 1) != attempt
                    or str(hydro.get("status") or "") not in ACTIVE_HYDRO_STATUSES
                ):
                    continue
                hydro_row = dict(hydro)
                hydro_row.update(
                    {
                        "status": "failed",
                        "error_code": "SLURM_RESERVATION_LOST",
                        "error_message": "Forecast submission was authoritatively absent; this attempt is retryable.",
                        "updated_at": _format_utc(_utcnow()),
                    }
                )
                payloads.append(("hydro_run", hydro_row, model_id))
                touched_models.add(model_id)
            payloads.append(("pipeline_job", cohort_row, _optional_safe_identity(cohort_row, "model_id")))
            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            direct_path = self.root / "pipeline-jobs" / f"{_required_safe_identity(cohort_row, 'job_id')}.json"
            self._atomic_write_json_unlocked(direct_path, records[-1])
            for model_id in sorted(touched_models):
                self._materialize_latest_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
            return len(records)

    def project_forecast_cohort_tasks(
        self,
        job_id: str,
        *,
        master_slurm_job_id: str,
        projections: Sequence[Mapping[str, Any]],
        complete: bool,
        master_status: str,
        master_error_code: str | None,
        reconciliation_decision: str,
    ) -> dict[str, int]:
        """Project one accounting pass under one cycle lock and one materialization/model."""
        initial = self._pipeline_job_for_id_unlocked(job_id)
        if initial is None:
            return {"total": 0, "pipeline_status": 0, "pipeline_event": 0}
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(job_id)
            if existing is None or str(existing.get("slurm_job_id") or "") != master_slurm_job_id:
                return {"total": 0, "pipeline_status": 0, "pipeline_event": 0}
            existing_projections = {
                int(item["array_task_id"]): dict(item)
                for item in _bounded_candidate_projections(existing.get("candidate_projections"))
                if str(item.get("array_task_id", "")).isdigit()
            }
            verified: list[dict[str, Any]] = []
            for projection in sorted(projections, key=lambda item: int(item.get("array_task_id") or 0)):
                bounded = _bounded_candidate_projections([projection])
                if not bounded or bounded[0].get("array_task_outcome") not in {"succeeded", "failed"}:
                    continue
                item = bounded[0]
                task_id = int(item["array_task_id"])
                previous = existing_projections.get(task_id)
                if previous is not None and previous.get("array_task_outcome") in {"succeeded", "failed"}:
                    continue
                existing_projections[task_id] = item
                verified.append(
                    {
                        **item,
                        "task_slurm_job_id": projection.get("task_slurm_job_id"),
                        "error_code": projection.get("error_code"),
                    }
                )

            payloads: list[tuple[str, dict[str, Any], str | None]] = []
            direct_jobs: list[dict[str, Any]] = []
            touched_models: set[str] = set()
            event_id = self._next_event_id_unlocked(source_id=source_id, cycle_time=cycle_time, model_id=None)
            for projection in verified:
                run_id = str(projection.get("run_id") or "")
                model_id = str(projection.get("model_id") or "")
                candidate_id = str(projection.get("candidate_id") or "")
                task_id = int(projection["array_task_id"])
                if not run_id or not model_id or not candidate_id:
                    continue
                task_status = str(projection["array_task_outcome"])
                task_status = "succeeded" if task_status == "succeeded" else "failed"
                candidate_job_id = f"job_{run_id}_forecast_reconciled_{master_slurm_job_id}_{task_id}"
                candidate_job = self._pipeline_job_row(
                    {
                        "job_id": candidate_job_id,
                        "run_id": run_id,
                        "cycle_id": existing["cycle_id"],
                        "job_type": "run_shud_forecast_array",
                        "slurm_job_id": str(projection.get("task_slurm_job_id") or f"{master_slurm_job_id}_{task_id}"),
                        "array_task_id": task_id,
                        "model_id": model_id,
                        "status": task_status,
                        "stage": "forecast",
                        "candidate_id": candidate_id,
                        "error_code": None if task_status == "succeeded" else projection.get("error_code"),
                        "submit_outcome": "accepted",
                        "restart_stage": projection.get("restart_stage"),
                        "native_shud_resubmitted": False,
                    }
                )
                payloads.append(("pipeline_job", candidate_job, model_id))
                direct_jobs.append(candidate_job)
                event = {
                    "event_id": event_id,
                    "entity_type": "pipeline_job",
                    "entity_id": candidate_job_id,
                    "event_type": "array_task_reconciled",
                    "status_from": "reconciling",
                    "status_to": task_status,
                    "message": None,
                    "details": _bounded_candidate_projections([projection])[0],
                    "created_at": _format_utc(_utcnow()),
                }
                event_id += 1
                payloads.append(("pipeline_event", event, model_id))
                hydro = self._hydro_run_for(run_id)
                if (
                    task_status == "succeeded"
                    and isinstance(hydro, Mapping)
                    and hydro.get("error_code") in {None, "SLURM_GATEWAY_UNAVAILABLE", "SLURM_RESERVATION_LOST"}
                ):
                    hydro_row = dict(hydro)
                    hydro_row.update(
                        {
                            "status": "created",
                            "slurm_job_id": candidate_job["slurm_job_id"],
                            "error_code": None,
                            "error_message": None,
                            "updated_at": _format_utc(_utcnow()),
                        }
                    )
                    payloads.append(("hydro_run", hydro_row, model_id))
                touched_models.add(model_id)

            cohort_row = dict(existing)
            cohort_row.update(
                {
                    "candidate_projections": [
                        existing_projections[task_id] for task_id in sorted(existing_projections)
                    ],
                    "status": master_status if complete else "reconcile_unverified",
                    "error_code": master_error_code if complete else "SLURM_TASK_ACCOUNTING_INCOMPLETE",
                    "reconciliation_source": "slurm_exact_comment",
                    "reconciliation_decision": reconciliation_decision,
                    "matched_slurm_job_id": master_slurm_job_id,
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            cohort_changed = any(
                cohort_row.get(key) != existing.get(key)
                for key in (
                    "candidate_projections",
                    "status",
                    "error_code",
                    "reconciliation_source",
                    "reconciliation_decision",
                    "matched_slurm_job_id",
                )
            )
            if cohort_changed:
                payloads.append(("pipeline_job", cohort_row, _optional_safe_identity(cohort_row, "model_id")))
            if not payloads:
                return {"total": 0, "pipeline_status": 0, "pipeline_event": 0}

            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            materialization_next_sequence = next_sequence + len(records)
            pipeline_records = [record for record in records if str(record.get("record_type") or "") == "pipeline_job"]
            for record, direct_job in zip(pipeline_records[: len(direct_jobs)], direct_jobs, strict=True):
                direct_path = self.root / "pipeline-jobs" / f"{_required_safe_identity(direct_job, 'job_id')}.json"
                self._atomic_write_json_unlocked(direct_path, record)
            if cohort_changed:
                direct_path = self.root / "pipeline-jobs" / f"{_required_safe_identity(cohort_row, 'job_id')}.json"
                self._atomic_write_json_unlocked(direct_path, pipeline_records[-1])
            for model_id in sorted(touched_models):
                self._materialize_latest_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    next_sequence=materialization_next_sequence,
                )
            event_writes = sum(record_type == "pipeline_event" for record_type, _payload, _model in payloads)
            return {
                "total": len(records),
                "pipeline_status": len(records) - event_writes,
                "pipeline_event": event_writes,
            }

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
            safe_error_message = _durable_error_message(error_message)
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
                row["error_message"] = safe_error_message
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
        normalized_entity_type = _pipeline_event_entity_type(entity_type)
        source_id, cycle_time, model_id = self._pipeline_event_target(
            entity_type=normalized_entity_type,
            entity_id=entity_id,
        )
        row = {
            "entity_type": normalized_entity_type,
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
            if normalized_entity_type == "forecast_cycle":
                self._materialize_cycle_latest_unlocked(source_id=source_id, cycle_time=cycle_time)
        return _public_scheduler_row(row)

    def append_historical_pipeline_event(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        entity_type = _pipeline_event_entity_type(record.get("entity_type") or "pipeline_job")
        entity_id = _required_safe_identity(record, "entity_id")
        try:
            source_id, cycle_time, model_id = self._pipeline_event_target(
                entity_type=entity_type,
                entity_id=entity_id,
            )
        except OrchestratorError:
            return None
        row = {
            "event_id": record.get("event_id"),
            "entity_type": entity_type,
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
                    and str(event.get("entity_type") or "pipeline_job") == entity_type
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
            if entity_type == "forecast_cycle":
                self._materialize_cycle_latest_unlocked(source_id=source_id, cycle_time=cycle_time)
        return _public_scheduler_row(row)

    def _pipeline_event_target(
        self,
        *,
        entity_type: str,
        entity_id: str,
    ) -> tuple[str, datetime, str | None]:
        if entity_type == "pipeline_job":
            job = self.get_pipeline_job(entity_id)
            if job is None:
                raise OrchestratorError("PIPELINE_JOB_NOT_FOUND", f"pipeline_job not found for event: {entity_id}")
            return _source_id_from_job(job), _cycle_time_from_job(job), _optional_safe_identity(job, "model_id")
        if entity_type == "forecast_cycle":
            cycle_id = _safe_identity_text(str(entity_id), field="entity_id")
            source_id, cycle_time = _source_cycle_from_cycle_id(cycle_id)
            return source_id, cycle_time, None
        raise OrchestratorError(
            "PIPELINE_EVENT_ENTITY_UNSUPPORTED",
            f"pipeline_event entity_type is not supported by file journal: {entity_type}",
        )

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
            "error_message": _durable_error_message(error_message),
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
        statuses: list[dict[str, Any]] = []
        if source_id is None:
            try:
                sources = self._cycle_source_discoveries(cycle_time=cycle_time)
            except FileOrchestrationJournalError as error:
                return [
                    _blocked_stage_status(
                        error,
                        source_id="unknown",
                        cycle_time=cycle_time,
                        model_id=model_id,
                    )
                ]
            for source in sources:
                statuses.extend(
                    self._list_stage_statuses_for_source(
                        source_id=source.source_id,
                        cycle_time=cycle_time,
                        model_id=model_id,
                        source_segment_overrides=source.source_segments,
                    )
                )
            statuses.sort(key=_db_compatible_stage_status_order_key)
            return statuses
        statuses = self._list_stage_statuses_for_source(
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        )
        statuses.sort(key=_db_compatible_stage_status_order_key)
        return statuses

    def _list_stage_statuses_for_source(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
        source_segment_override: str | None = None,
        source_segment_overrides: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        try:
            rows = self._cycle_rows(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                source_segment_override=source_segment_override,
                source_segment_overrides=source_segment_overrides,
            )
        except FileOrchestrationJournalError as error:
            return [_blocked_stage_status(error, source_id=source_id, cycle_time=cycle_time, model_id=model_id)]
        return [
            _public_scheduler_row(
                {
                    "stage": job.get("stage"),
                    "status": job.get("status"),
                    "job_id": job.get("job_id"),
                    "run_id": job.get("run_id"),
                    "cycle_id": job.get("cycle_id"),
                    "job_type": job.get("job_type"),
                    "slurm_job_id": job.get("slurm_job_id"),
                    "model_id": job.get("model_id"),
                    "source_id": source_id,
                    "submitted_at": job.get("submitted_at"),
                    "started_at": job.get("started_at"),
                    "finished_at": job.get("finished_at"),
                    "exit_code": job.get("exit_code"),
                    "error_code": job.get("error_code"),
                    "error_message": job.get("error_message"),
                    "log_uri": job.get("log_uri"),
                }
            )
            for job in rows.pipeline_jobs.values()
        ]

    def _cycle_source_ids(self, *, cycle_time: datetime) -> list[str]:
        return sorted({source.source_id for source in self._cycle_source_discoveries(cycle_time=cycle_time)})

    def _cycle_source_discoveries(self, *, cycle_time: datetime) -> list[_CycleSourceDiscovery]:
        cycle_segment = format_cycle_time(cycle_time)
        sources: dict[str, _CycleSourceDiscovery] = {}
        for path in sorted(
            _iter_regular_json_files(
                self.root / "latest",
                root=self.root,
                recursive=True,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        ):
            parts = path.relative_to(self.root).parts
            if len(parts) == 4 and parts[0] == "latest" and parts[2] == cycle_segment:
                source = _cycle_source_discovery_from_segment(parts[1])
                _merge_cycle_source_discovery(sources, source)
        for surface in ("journal", "pipeline-events"):
            for path in sorted(
                _iter_jsonl_files(
                    self.root / surface,
                    root=self.root,
                    max_files=self.max_files,
                    max_depth=self.max_depth,
                )
            ):
                parts = path.relative_to(self.root).parts
                if len(parts) == 3 and parts[0] == surface and Path(parts[2]).stem == cycle_segment:
                    source = _cycle_source_discovery_from_segment(parts[1])
                    _merge_cycle_source_discovery(sources, source)
        file_source_ids = set(sources)
        for job in self._iter_direct_pipeline_job_records():
            if _format_utc(_cycle_time_from_job(job)) == _format_utc(cycle_time):
                source_id = _source_id_from_job(job)
                if source_id not in file_source_ids:
                    sources.setdefault(
                        source_id,
                        _CycleSourceDiscovery(source_id=source_id, source_segments=(_safe_segment(source_id),)),
                    )
        return sorted(sources.values(), key=lambda source: (source.source_id, source.source_segments))

    def _cycle_rows(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
        source_segment_override: str | None = None,
        source_segment_overrides: tuple[str, ...] | None = None,
    ) -> _CycleRows:
        rows = _CycleRows()
        source_id = _normalize_file_source_id(source_id, field="source_id")
        source_segments = _cycle_read_source_segments(
            source_id=source_id,
            source_segment_override=source_segment_override,
            source_segment_overrides=source_segment_overrides,
        )
        cycle_segment = format_cycle_time(cycle_time)
        cache_key = (source_id, cycle_segment, model_id, source_segments)
        # Inside a locked write window the cycle flock excludes external
        # writers and the append hook keeps the cache coherent, so hits are
        # trusted as-is. Outside a window a hit must prove its source files
        # are stat-identical, otherwise writes from other processes (or
        # direct file fixtures) would be served stale forever.
        in_write_window = self._write_lock.locked()
        fingerprint = (
            None
            if in_write_window
            else self._cycle_rows_source_fingerprint(source_segments=source_segments, cycle_segment=cycle_segment)
        )
        cached = self._cycle_rows_cache.get(cache_key)
        if cached is not None and (in_write_window or (fingerprint is not None and cached[0] == fingerprint)):
            return _clone_cycle_rows(cached[1])
        # Model-scoped reads build from the model's own latest view plus
        # model-filtered journal records. They must not derive hydro_run /
        # forcing_version / model_context from the merged model_id=None rows:
        # _CycleRows keeps single slots for those, so the cross-model merge
        # keeps one winner and the model filter then erases every other
        # model's rows. Only pipeline jobs (keyed by job_id, collapse-free)
        # are shared with the base rows so the pipeline-jobs directory is
        # scanned once per cycle instead of once per model.
        for source_segment in source_segments:
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
        for job in self._direct_pipeline_job_records_for_cycle_cached(
            source_id=source_id,
            cycle_time=cycle_time,
        ):
            _insert_missing_by_key(rows.pipeline_jobs, job, key="job_id")
        if model_id is not None:
            _filter_cycle_rows_for_model(rows, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        rows.pipeline_events = _dedupe_events(rows.pipeline_events)
        self._cache_cycle_rows(cache_key, rows, fingerprint=fingerprint)
        return _clone_cycle_rows(rows)

    def _direct_pipeline_job_records_for_cycle_cached(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> list[dict[str, Any]]:
        """One pipeline-jobs directory scan per cycle, shared across models.

        Entries in that directory only change via atomic rename, which bumps
        the directory's (mtime_ns, size, inode); a matching signature proves
        the memoized listing still reflects the on-disk job set. Model-level
        scoping happens in _filter_cycle_rows_for_model, mirroring how the
        unfiltered scan feeds the model_id=None rows.
        """
        cache_key = (source_id, format_cycle_time(cycle_time))
        signature = _stat_signature(self.root / "pipeline-jobs")
        cached = self._direct_jobs_cycle_cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            return [dict(job) for job in cached[1]]
        jobs = [
            dict(job)
            for job in self._iter_direct_pipeline_job_records_for_cycle(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=None,
            )
        ]
        cache_limit = max(int(MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES), 1)
        if cache_key not in self._direct_jobs_cycle_cache and len(self._direct_jobs_cycle_cache) >= cache_limit:
            self._direct_jobs_cycle_cache.pop(next(iter(self._direct_jobs_cycle_cache)), None)
        self._direct_jobs_cycle_cache[cache_key] = (signature, [dict(job) for job in jobs])
        return jobs

    def _cycle_rows_source_fingerprint(
        self,
        *,
        source_segments: tuple[str, ...],
        cycle_segment: str,
    ) -> tuple[Any, ...]:
        """Stat-level identity of every file that feeds `_cycle_rows`.

        Appends, atomic replaces, additions and removals all change the
        (mtime_ns, size, inode) of a source file — or the latest-directory
        listing, or the pipeline-jobs directory whose entries only change
        via rename — so a matching fingerprint proves a cached entry still
        reflects the on-disk state.
        """
        latest_entries: list[tuple[str, str, tuple[int, int, int] | None]] = []
        journal_signatures: list[tuple[str, tuple[int, int, int] | None]] = []
        event_signatures: list[tuple[str, tuple[int, int, int] | None]] = []
        for source_segment in source_segments:
            try:
                with os.scandir(self.root / "latest" / source_segment / cycle_segment) as it:
                    for entry in it:
                        if entry.name.endswith(".json"):
                            try:
                                entry_stat = entry.stat(follow_symlinks=False)
                                latest_entries.append(
                                    (
                                        source_segment,
                                        entry.name,
                                        (entry_stat.st_mtime_ns, entry_stat.st_size, entry_stat.st_ino),
                                    )
                                )
                            except OSError:
                                latest_entries.append((source_segment, entry.name, None))
            except OSError:
                pass
            journal_signatures.append(
                (
                    source_segment,
                    _stat_signature(self.root / "journal" / source_segment / f"{cycle_segment}.jsonl"),
                )
            )
            event_signatures.append(
                (
                    source_segment,
                    _stat_signature(self.root / "pipeline-events" / source_segment / f"{cycle_segment}.jsonl"),
                )
            )
        return (
            tuple(journal_signatures),
            tuple(event_signatures),
            tuple(sorted(latest_entries)),
            _stat_signature(self.root / "pipeline-jobs"),
        )

    def _cache_cycle_rows(
        self,
        cache_key: tuple[str, str, str | None, tuple[str, ...]],
        rows: _CycleRows,
        *,
        fingerprint: tuple[Any, ...] | None,
    ) -> None:
        cache_limit = max(int(MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES), 1)
        if cache_key not in self._cycle_rows_cache and len(self._cycle_rows_cache) >= cache_limit:
            self._cycle_rows_cache.pop(next(iter(self._cycle_rows_cache)), None)
        self._cycle_rows_cache[cache_key] = (fingerprint, _clone_cycle_rows(rows))

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
            _validate_event_identity(event, source_id=source_id, cycle_time=cycle_time)
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
        if expected_model_id is not None and record_model_id is not None and record_model_id != expected_model_id:
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
            self._apply_event_record(rows, record, source_id=source_id, cycle_time=cycle_time)
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

    def _apply_event_record(
        self,
        rows: _CycleRows,
        record: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> None:
        payload = _with_replay_order(_payload_or_record_payload(record), record)
        if "event_id" not in payload and record.get("sequence") not in (None, ""):
            payload["event_id"] = record.get("sequence")
        _validate_event_identity(payload, source_id=source_id, cycle_time=cycle_time)
        rows.pipeline_events.append(dict(payload))

    def _read_json(self, path: Path) -> dict[str, Any]:
        payload = self._read_optional_json(path)
        if payload is None:
            raise FileOrchestrationJournalError(
                "file_journal_view_missing",
                field=str(_relative_evidence(path, self.root)),
            )
        return payload

    def _read_bytes_limited_cached(self, path: Path) -> tuple[bytes, bool]:
        """Read file bytes through a stat-identity cache.

        The stat probe is only a cache key: any anomaly (missing file,
        symlink, non-regular target) falls through to the hardened
        no-follow reader, which stays the sole authority for errors and
        content. A hit requires an exact (mtime_ns, size, inode) match,
        so appends and atomic-rename replacements always miss. The
        returned flag is True when these exact bytes already passed
        `_decode_mapping` validation in this process.
        """
        key = str(path)
        signature: tuple[int, int, int] | None = None
        try:
            probe = os.stat(path, follow_symlinks=False)
        except OSError:
            probe = None
            self._read_bytes_cache_drop(key)
        if probe is not None and stat.S_ISREG(probe.st_mode):
            signature = (probe.st_mtime_ns, probe.st_size, probe.st_ino)
            cached = self._read_bytes_cache.get(key)
            if cached is not None and cached[0] == signature:
                return cached[1], cached[2]
        content = read_bytes_limited_no_follow(path, max_bytes=self.max_bytes, containment_root=self.root)
        if signature is not None and len(content) == probe.st_size:
            self._read_bytes_cache_store(key, signature, content)
        return content, False

    def _read_bytes_cache_store(self, key: str, signature: tuple[int, int, int], content: bytes) -> None:
        if len(content) > MAX_FILE_JOURNAL_READ_CACHE_BYTES:
            return
        self._read_bytes_cache_drop(key)
        while self._read_bytes_cache and (
            len(self._read_bytes_cache) >= MAX_FILE_JOURNAL_READ_CACHE_ENTRIES
            or self._read_bytes_cache_total + len(content) > MAX_FILE_JOURNAL_READ_CACHE_BYTES
        ):
            self._read_bytes_cache_drop(next(iter(self._read_bytes_cache)))
        self._read_bytes_cache[key] = (signature, content, False)
        self._read_bytes_cache_total += len(content)

    def _read_bytes_cache_drop(self, key: str) -> None:
        entry = self._read_bytes_cache.pop(key, None)
        if entry is not None:
            self._read_bytes_cache_total -= len(entry[1])

    def _read_bytes_cache_mark_validated(self, key: str, content: bytes) -> None:
        entry = self._read_bytes_cache.get(key)
        if entry is not None and entry[1] is content:
            self._read_bytes_cache[key] = (entry[0], entry[1], True)

    def _read_optional_json(self, path: Path) -> dict[str, Any] | None:
        try:
            content, prevalidated = self._read_bytes_limited_cached(path)
        except FileNotFoundError:
            return None
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(path, self.root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        self._require_within_byte_limit(content, path)
        if prevalidated:
            return _decode_mapping_prevalidated(content, field=str(_relative_evidence(path, self.root)))
        payload = _decode_mapping(
            content,
            field=str(_relative_evidence(path, self.root)),
            max_nodes=self.max_json_nodes,
            max_depth=self.max_json_depth,
        )
        self._read_bytes_cache_mark_validated(str(path), content)
        return payload

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        try:
            content, prevalidated = self._read_bytes_limited_cached(path)
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
            if prevalidated:
                record = _decode_mapping_prevalidated(
                    raw_line,
                    field=f"{_relative_evidence(path, self.root)}:{line_number}",
                )
            else:
                record = _decode_mapping(
                    raw_line,
                    field=f"{_relative_evidence(path, self.root)}:{line_number}",
                    max_nodes=self.max_json_nodes,
                    max_depth=self.max_json_depth,
                )
            record[_REPLAY_ORDER_FIELD] = line_number
            records.append(record)
        if not prevalidated:
            self._read_bytes_cache_mark_validated(str(path), content)
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

    def _iter_reconcile_pipeline_job_records(self) -> Iterable[dict[str, Any]]:
        """Bounded restart-reconcile scan for DB-free file journals.

        The full read surface intentionally reconstructs historical state from
        every latest/journal/direct file. Restart reconcile runs at the top of
        every live scheduler pass, so it must not recursively validate the whole
        journal tree before the progress guard can trip. Scan recent direct job
        records and recent journal files only; candidate-scoped reads still use
        the full identity-bound surfaces later in the pass.
        """

        jobs: dict[str, dict[str, Any]] = {}
        budget = _RecordBudget(max(self.max_records, 1), "reconcile_pipeline_job_records")
        for job in self._iter_reconcile_direct_pipeline_job_records():
            if not _job_needs_restart_reconcile(job):
                continue
            budget.consume()
            _upsert_by_key(jobs, job, key="job_id")
        for path in self._iter_recent_reconcile_journal_paths(_file_reconcile_scan_limit()):
            try:
                source_id, cycle_time = _journal_identity_from_path(path, root=self.root, surface="journal")
                records = self._read_jsonl(path)
            except FileOrchestrationJournalError:
                continue
            rows = _CycleRows()
            try:
                for record in records:
                    budget.consume()
                    self._apply_journal_record(rows, record, source_id=source_id, cycle_time=cycle_time)
            except FileOrchestrationJournalError:
                continue
            for job in rows.pipeline_jobs.values():
                if not _job_needs_restart_reconcile(job):
                    continue
                _upsert_by_key(jobs, job, key="job_id")
        yield from jobs.values()

    def _iter_reconcile_direct_pipeline_job_records(self) -> Iterable[dict[str, Any]]:
        for path in self._iter_reconcile_direct_pipeline_job_paths():
            try:
                expected_job_id = _safe_segment(path.stem)
                payload = self._read_optional_json(path)
                if payload is not None:
                    yield self._validated_direct_pipeline_job_record(payload, expected_job_id=expected_job_id)
            except FileOrchestrationJournalError:
                continue

    def _iter_reconcile_direct_pipeline_job_paths(self) -> Iterable[Path]:
        directory = self.root / "pipeline-jobs"
        if self.max_files <= 0 or self.max_depth < 0:
            return
        try:
            directory_mode = stat_no_follow(directory, containment_root=self.root).st_mode
        except FileNotFoundError:
            return
        except (OSError, SafeFilesystemError):
            return
        if not stat.S_ISDIR(directory_mode):
            return
        try:
            entry_names = list_directory_no_follow_limited(
                directory,
                containment_root=self.root,
                max_entries=self.max_files,
            )
        except FileNotFoundError:
            return
        except (OSError, SafeFilesystemError):
            return
        if len(entry_names) > self.max_files:
            return
        for entry_name in sorted(entry_names):
            if not entry_name.endswith(".json"):
                continue
            if _SAFE_SEGMENT_RE.fullmatch(entry_name) is None:
                continue
            path = directory / entry_name
            try:
                mode = stat_no_follow(path, containment_root=self.root).st_mode
            except FileNotFoundError:
                continue
            except (OSError, SafeFilesystemError):
                continue
            if stat.S_ISREG(mode):
                yield path

    def _iter_recent_direct_pipeline_job_records(self, limit: int) -> Iterable[dict[str, Any]]:
        for path in self._recent_files(self.root / "pipeline-jobs", suffix=".json", limit=limit):
            try:
                expected_job_id = _safe_segment(path.stem)
                payload = self._read_optional_json(path)
                if payload is not None:
                    yield self._validated_direct_pipeline_job_record(payload, expected_job_id=expected_job_id)
            except FileOrchestrationJournalError:
                continue

    def _iter_recent_reconcile_journal_paths(self, limit: int) -> Iterable[Path]:
        yield from self._recent_files(self.root / "journal", suffix=".jsonl", limit=limit)

    def _recent_files(self, directory: Path, *, suffix: str, limit: int) -> Iterable[Path]:
        if limit <= 0 or not directory.exists():
            return
        discovered: list[tuple[int, str, Path]] = []
        root = self.root.resolve(strict=False)
        if suffix == ".jsonl":
            iterator = _iter_jsonl_files(
                directory,
                root=self.root,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        elif suffix == ".json":
            iterator = _iter_regular_json_files(
                directory,
                root=self.root,
                recursive=True,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        else:
            return
        try:
            paths = list(iterator)
        except (FileOrchestrationJournalError, OSError):
            return
        for path in paths:
            try:
                resolved = path.resolve(strict=False)
                rel = resolved.relative_to(root)
                stat_result = path.stat()
            except (OSError, ValueError):
                continue
            discovered.append((stat_result.st_mtime_ns, str(rel), path))
        for _mtime, _rel, path in sorted(discovered, reverse=True)[:limit]:
            yield path

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
        row = _redact_durable_error_message_fields("hydro_run", row)
        source_id = _required_source_id(row, "source_id")
        cycle_time = _parse_cycle_time_field(row, "cycle_time")
        model_id = _required_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._hydro_run_for(str(row["run_id"]))
            if (
                retriable_only
                and existing is not None
                and str(existing.get("status") or "")
                not in {
                    "failed",
                    "cancelled",
                }
            ):
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
            "previous_job_id": _optional_safe_identity(record, "previous_job_id"),
            "error_code": record.get("error_code"),
            "error_message": _durable_error_message(record.get("error_message")),
            "log_uri": record.get("log_uri"),
            "submit_outcome": record.get("submit_outcome"),
            "slurm_comment": record.get("slurm_comment"),
            "cohort_members": _bounded_cohort_members(record.get("cohort_members")),
            "cohort_digest": record.get("cohort_digest"),
            "restart_stage": record.get("restart_stage"),
            "submission_attempt": record.get("submission_attempt", 1),
            "submission_attempt_started_at": _optional_format_datetime(
                record.get("submission_attempt_started_at"), field="submission_attempt_started_at"
            ),
            "expected_slurm_user": record.get("expected_slurm_user"),
            "expected_slurm_account": record.get("expected_slurm_account"),
            "slurm_ownership_required": bool(record.get("slurm_ownership_required", False)),
            "reconciliation_source": record.get("reconciliation_source"),
            "reconciliation_decision": record.get("reconciliation_decision"),
            "matched_slurm_job_id": record.get("matched_slurm_job_id"),
            "candidate_projections": _bounded_candidate_projections(record.get("candidate_projections")),
            "native_shud_resubmitted": record.get("native_shud_resubmitted"),
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
        row = _redact_durable_error_message_fields("pipeline_job", row)
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        row = {**row, "source_id": source_id}
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
        payload = _redact_durable_error_message_fields(record_type, payload)
        private_recovery_payload = dict(payload) if record_type == "pipeline_event" else None
        if record_type == "pipeline_event":
            payload = _public_pipeline_event_payload(payload)
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
        if private_recovery_payload is not None:
            self._write_pipeline_event_private_recovery_unlocked(
                private_recovery_payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
        self._append_journal_record_unlocked(source_id=source_id, cycle_time=cycle_time, record=record)
        if materialize_model_id is not None:
            self._materialize_latest_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=materialize_model_id,
            )

    def _write_pipeline_event_private_recovery_unlocked(
        self,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
    ) -> None:
        record = _private_runtime_root_recovery_record(
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        )
        if record is None:
            return
        path = _private_runtime_root_recovery_path(
            self.root,
            source_id=source_id,
            cycle_time=cycle_time,
            entity_id=str(record["entity_id"]),
            event_id=str(record["event_id"]),
        )
        content = _json_bytes(record)
        self._require_within_byte_limit(content, path)
        self._atomic_write_bytes_unlocked(path, content)

    def _pipeline_event_private_runtime_root_candidates(
        self,
        job: Mapping[str, Any],
        event: Mapping[str, Any],
        *,
        candidate_budget: int,
    ) -> _RuntimeRootCandidateBatch | None:
        event_id = event.get("event_id")
        if event_id in (None, ""):
            return None
        source_id = _source_id_from_job(job)
        cycle_time = _cycle_time_from_job(job)
        path = _private_runtime_root_recovery_path(
            self.root,
            source_id=source_id,
            cycle_time=cycle_time,
            entity_id=str(event.get("entity_id") or ""),
            event_id=str(event_id),
        )
        payload = self._read_optional_json(path)
        if payload is None:
            return None
        try:
            _validate_private_runtime_root_recovery_record(
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=_optional_safe_identity(job, "model_id"),
                event=event,
            )
        except FileOrchestrationJournalError:
            return None
        candidates_payload = payload.get("candidates")
        if not isinstance(candidates_payload, Sequence) or isinstance(candidates_payload, str | bytes | bytearray):
            return None
        candidates: list[_RuntimeRootCandidate] = []
        total_count = 0
        for item in candidates_payload:
            if not isinstance(item, Mapping):
                return None
            raw_path = item.get("path")
            value = item.get("value")
            if not isinstance(raw_path, Sequence) or isinstance(raw_path, str | bytes | bytearray):
                return None
            if not isinstance(value, Mapping) or not _has_runtime_root_field(value):
                continue
            candidate_path = tuple(str(part) for part in raw_path)
            if not candidate_path:
                return None
            total_count += 1
            if len(candidates) < candidate_budget:
                candidates.append(
                    _RuntimeRootCandidate(
                        f"file_journal_event:{event.get('entity_id')}:{event_id}:{'.'.join(candidate_path)}",
                        dict(value),
                    )
                )
        return _RuntimeRootCandidateBatch(
            candidates=candidates,
            event_candidate_returned_count=len(candidates),
            event_candidate_total_count=total_count,
            event_candidate_omitted_count=max(total_count - len(candidates), 0),
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
        source_id = _normalize_file_source_id(source_id, field="source_id")
        cycle_segment = format_cycle_time(cycle_time)
        source_segments = _cycle_read_source_segments(source_id=source_id, source_segment_override=None)
        sequences: list[int] = []
        for source_segment in source_segments:
            for surface in ("journal", "pipeline-events"):
                path = self.root / surface / source_segment / f"{cycle_segment}.jsonl"
                if not self._sequence_regular_file_exists(path):
                    continue
                records = self._read_jsonl(path)
                sequences.extend((_optional_replay_sequence(record) or 0) for record in records)
        sequences.extend(
            self._latest_replay_sequences_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                source_segments=source_segments,
            )
        )
        return max(sequences, default=0) + 1

    def _latest_replay_sequences_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        source_segments: tuple[str, ...] | None = None,
    ) -> list[int]:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        if source_segments is None:
            source_segments = _cycle_read_source_segments(source_id=source_id, source_segment_override=None)
        sequences: list[int] = []
        cycle_segment = format_cycle_time(cycle_time)
        for source_segment in source_segments:
            if not self._sequence_directory_exists(self.root / "latest" / source_segment / cycle_segment):
                continue
            for path in self._latest_paths(source_segment, cycle_segment, model_id=None):
                payload = self._read_optional_json(path)
                if payload is None:
                    continue
                _require_schema(payload, FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION)
                _require_source_cycle(payload, source_id=source_id, cycle_time=cycle_time)
                sequences.append(_latest_replay_sequence(payload) or 0)
        return sequences

    def _sequence_regular_file_exists(self, path: Path) -> bool:
        try:
            mode = os.stat(path, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return False
        except OSError as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(path, self.root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        return stat.S_ISREG(mode)

    def _sequence_directory_exists(self, path: Path) -> bool:
        try:
            mode = os.stat(path, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return False
        except OSError as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(path, self.root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        return stat.S_ISDIR(mode)

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
        self._apply_record_to_cycle_rows_cache(source_id=source_id, cycle_time=cycle_time, record=record)

    def _append_journal_records_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        """Append a validated record batch with one bounded journal rewrite."""
        if not records:
            return
        path = self._journal_path(source_id=source_id, cycle_time=cycle_time)
        try:
            existing = read_bytes_limited_no_follow(path, max_bytes=self.max_bytes, containment_root=self.root)
        except FileNotFoundError:
            existing = b""
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to read existing file journal before batch append",
                {"error_type": type(error).__name__},
            ) from error
        content = existing
        if content and not content.endswith(b"\n"):
            content += b"\n"
        content += b"\n".join(_json_bytes(record).rstrip(b"\n") for record in records) + b"\n"
        self._require_within_byte_limit(content, path)
        self._atomic_write_bytes_unlocked(path, content)
        for record in records:
            self._apply_record_to_cycle_rows_cache(source_id=source_id, cycle_time=cycle_time, record=record)

    def _apply_record_to_cycle_rows_cache(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        record: Mapping[str, Any],
    ) -> None:
        """Keep the in-window rows cache coherent with a just-appended record.

        Applying the record through the same reducer used by fresh reads is
        equivalent to re-reading the journal: merges are decided by the
        strictly increasing replay sequence, not list position. Derived
        (model-scoped and segment-override) entries are dropped and rebuilt
        lazily from the updated base entry.
        """
        source_id = _normalize_file_source_id(source_id, field="source_id")
        cycle_segment = format_cycle_time(cycle_time)
        base_key = (source_id, cycle_segment, None, None)
        stale_keys = [
            key for key in self._cycle_rows_cache if key[0] == source_id and key[1] == cycle_segment and key != base_key
        ]
        for key in stale_keys:
            self._cycle_rows_cache.pop(key, None)
        cached = self._cycle_rows_cache.get(base_key)
        if cached is None:
            return
        updated = _clone_cycle_rows(cached[1])
        try:
            self._apply_journal_record(updated, record, source_id=source_id, cycle_time=cycle_time)
            updated.pipeline_events = _dedupe_events(updated.pipeline_events)
        except FileOrchestrationJournalError:
            self._cycle_rows_cache.pop(base_key, None)
            return
        # In-window entries carry no fingerprint: hits are trusted while the
        # cycle flock is held, and the window clears the cache on exit.
        self._cycle_rows_cache[base_key] = (None, updated)

    def _materialize_latest_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        next_sequence: int | None = None,
    ) -> None:
        rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        if next_sequence is None:
            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
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
                "latest_sequence": next_sequence - 1,
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

    def _materialize_cycle_latest_unlocked(self, *, source_id: str, cycle_time: datetime) -> None:
        # The journal cannot change mid-sweep (cycle write lock is held), so
        # the next sequence is computed once instead of per model.
        next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
        for model_id in self._cycle_materialization_model_ids_unlocked(source_id=source_id, cycle_time=cycle_time):
            self._materialize_latest_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                next_sequence=next_sequence,
            )

    def _cycle_materialization_model_ids_unlocked(self, *, source_id: str, cycle_time: datetime) -> list[str]:
        model_ids: set[str] = set()
        source_segment = _safe_segment(_normalize_file_source_id(source_id, field="source_id"))
        cycle_segment = format_cycle_time(cycle_time)
        for path in self._latest_paths(source_segment, cycle_segment, model_id=None):
            model_ids.add(_safe_segment(path.stem))
        try:
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None)
        except FileOrchestrationJournalError:
            return sorted(model_ids)
        for job in rows.pipeline_jobs.values():
            model_id = _optional_safe_identity(job, "model_id")
            if model_id is not None:
                model_ids.add(model_id)
        for row in (rows.hydro_run, rows.forcing_version, rows.model_context):
            if isinstance(row, Mapping):
                model_id = _optional_safe_identity(row, "model_id")
                if model_id is not None:
                    model_ids.add(model_id)
        return sorted(model_ids)

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
        self._read_bytes_cache_drop(str(path))

    @contextmanager
    def _locked_cycle_write(self, *, source_id: str, cycle_time: datetime) -> Iterable[None]:
        with self._write_lock:
            self._cycle_rows_cache.clear()
            self._ensure_root_unlocked()
            try:
                with self._cycle_file_lock_unlocked(source_id=source_id, cycle_time=cycle_time):
                    yield
            finally:
                self._cycle_rows_cache.clear()

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
        lock_held = False
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
            if _file_lock_guard_mode() == "flock":
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                lock_held = True
            yield
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to acquire file orchestration journal cycle lock",
                {"error_type": type(error).__name__},
            ) from error
        finally:
            if lock_fd is not None:
                if lock_held:
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


def _file_lock_guard_mode() -> str:
    value = os.getenv(FILE_LOCK_GUARD_MODE_ENV, "flock").strip().lower()
    if value in {"", "flock", "fcntl"}:
        return "flock"
    if value in {"atomic", "none", "off", "disabled"}:
        return "atomic"
    raise SafeFilesystemError(f"Unsupported {FILE_LOCK_GUARD_MODE_ENV}: {value}")


def _file_reconcile_scan_limit() -> int:
    value = os.getenv(FILE_RECONCILE_SCAN_LIMIT_ENV)
    try:
        return max(int(value), 1) if value not in (None, "") else DEFAULT_FILE_RECONCILE_SCAN_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_FILE_RECONCILE_SCAN_LIMIT


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
                "previous_job_id": failed_job["job_id"],
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
            submitted = _submit_file_manual_retry_job(gateway, request)
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
        array_tasks = _file_manual_retry_array_tasks(submission_job, runtime_root_contract)
        if array_tasks is not None:
            manifest["tasks"] = array_tasks
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
        candidate_batch = self._file_retry_runtime_root_candidates(retry_job)
        db_free_required = _candidate_batch_db_free_required(candidate_batch)
        runtime_roots_required = retry_job.job_type == DOWNLOAD_SOURCE_CYCLE_JOB_TYPE or db_free_required
        rejected: list[dict[str, str]] = []
        rejected_total_count = 0
        best_resolved: dict[str, tuple[str, str]] = {}
        best_missing = list(_REQUIRED_RUNTIME_ROOT_FIELDS)
        secret_rejected = False
        unsafe_rejected = False
        best_db_free_resolved: dict[str, tuple[str, str]] = {}
        best_db_free_missing: list[str] = list(_DB_FREE_REQUIRED_SELECTOR_FIELDS) if db_free_required else []
        for candidate in candidate_batch.candidates:
            resolution = _resolve_runtime_root_candidate(candidate.source, candidate.value)
            db_free_resolution = (
                _resolve_db_free_runtime_candidate(candidate.source, candidate.value) if db_free_required else None
            )
            rejected_total_count += len(resolution.rejected)
            if db_free_resolution is not None:
                rejected_total_count += len(db_free_resolution.rejected)
            if len(rejected) < _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT:
                remaining = _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT - len(rejected)
                rejected.extend(resolution.rejected[:remaining])
            if db_free_resolution is not None and len(rejected) < _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT:
                remaining = _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT - len(rejected)
                rejected.extend(db_free_resolution.rejected[:remaining])
            candidate_secret_rejected = resolution.secret_rejected
            candidate_unsafe_rejected = resolution.unsafe_rejected
            secret_rejected = secret_rejected or candidate_secret_rejected
            unsafe_rejected = unsafe_rejected or candidate_unsafe_rejected
            if db_free_resolution is not None:
                candidate_secret_rejected = candidate_secret_rejected or db_free_resolution.secret_rejected
                candidate_unsafe_rejected = candidate_unsafe_rejected or db_free_resolution.unsafe_rejected
                secret_rejected = secret_rejected or db_free_resolution.secret_rejected
                unsafe_rejected = unsafe_rejected or db_free_resolution.unsafe_rejected
            if len(resolution.resolved) > len(best_resolved):
                best_resolved = resolution.resolved
                best_missing = resolution.missing
            if db_free_resolution is not None and len(db_free_resolution.resolved) > len(best_db_free_resolved):
                best_db_free_resolved = db_free_resolution.resolved
                best_db_free_missing = db_free_resolution.missing
            db_free_complete = db_free_resolution is None or db_free_resolution.complete
            if (
                not resolution.complete
                or not db_free_complete
                or candidate_secret_rejected
                or candidate_unsafe_rejected
            ):
                continue
            evidence = _runtime_root_resolution_evidence(
                retry_job,
                resolved=resolution.resolved,
                missing=[],
                rejected=rejected,
                rejected_total_count=rejected_total_count,
                candidate_batch=candidate_batch,
                db_free_resolved=db_free_resolution.resolved if db_free_resolution is not None else {},
                db_free_missing=[] if db_free_resolution is not None else [],
                db_free_required=db_free_required,
            )
            manifest_fields = {field: value for field, (value, _source) in resolution.resolved.items()}
            if db_free_resolution is not None:
                manifest_fields.update(
                    {field: value for field, (value, _source) in db_free_resolution.resolved.items()}
                )
            return SimpleNamespace(manifest_fields=manifest_fields, evidence=evidence)
        if not runtime_roots_required:
            return None
        evidence = _runtime_root_resolution_evidence(
            retry_job,
            resolved=best_resolved,
            missing=best_missing,
            rejected=rejected,
            rejected_total_count=rejected_total_count,
            candidate_batch=candidate_batch,
            db_free_resolved=best_db_free_resolved,
            db_free_missing=best_db_free_missing,
            db_free_required=db_free_required,
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
                "Manual retry cannot resolve required object-store runtime roots.",
                evidence,
            )
        if best_db_free_missing:
            raise _RetryRuntimeRootResolutionError(
                RETRY_RUNTIME_ROOTS_UNRESOLVED,
                "Manual retry cannot resolve required DB-free scheduler runtime selectors.",
                evidence,
            )
        raise _RetryRuntimeRootResolutionError(
            RETRY_RUNTIME_ROOTS_UNRESOLVED,
            "Manual retry cannot resolve required runtime roots.",
            evidence,
        )

    def _file_retry_runtime_root_candidates(self, retry_job: _RetrySubmissionJob) -> _RuntimeRootCandidateBatch:
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
            provenance_job_ids = self._file_retry_provenance_job_ids(str(retry_job.previous_job_id))
        for job_id in provenance_job_ids:
            if len(candidates) >= _RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT:
                break
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
        excluded = set(provenance_job_ids)
        if retry_job.run_id:
            same_run_jobs = [
                job
                for job in sorted(
                    self.repository.query_pipeline_jobs_by_run(str(retry_job.run_id)),
                    key=_db_compatible_pipeline_job_order_key,
                )
                if str(job.get("job_id") or "")
                and str(job.get("job_id") or "") not in excluded
                and str(job.get("job_id") or "") != retry_job.job_id
                and str(job.get("job_type") or "") == DOWNLOAD_SOURCE_CYCLE_JOB_TYPE
                and not (
                    retry_job.cycle_id
                    and job.get("cycle_id") not in (None, "")
                    and job.get("cycle_id") != retry_job.cycle_id
                )
                and job.get("manual_retry_marker") is not True
            ]
            same_run_scan_jobs = same_run_jobs[:_RUNTIME_ROOT_SAME_RUN_JOB_SCAN_LIMIT]
            same_run_jobs_omitted = max(len(same_run_jobs) - len(same_run_scan_jobs), 0)
            event_rows_total_count += len(same_run_jobs)
            event_rows_omitted_count += same_run_jobs_omitted
            for job in same_run_scan_jobs:
                job_id = str(job.get("job_id") or "")
                if len(candidates) >= _RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT:
                    break
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

    def _file_retry_provenance_job_ids(self, job_id: str) -> list[str]:
        job_ids: list[str] = []
        seen: set[str] = set()
        current: str | None = job_id
        for _ in range(16):
            if not current or current in seen:
                break
            seen.add(current)
            job_ids.append(current)
            current = self._file_retry_previous_job_id(current)
        return job_ids

    def _file_retry_previous_job_id(self, job_id: str) -> str | None:
        job = self.repository.get_pipeline_job(job_id)
        if job is None:
            return None
        source_id = _source_id_from_job(job)
        cycle_time = _cycle_time_from_job(job)
        model_id = _optional_safe_identity(job, "model_id")
        rows = self.repository._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        retry_events = sorted(
            (
                event
                for event in rows.pipeline_events
                if str(event.get("entity_id") or "") == job_id and str(event.get("event_type") or "") == "retry"
            ),
            key=lambda event: _optional_positive_int(event.get("event_id")) or 0,
            reverse=True,
        )
        for event in retry_events:
            details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
            previous_job_id = details.get("previous_job_id")
            if isinstance(previous_job_id, str) and previous_job_id.strip():
                return previous_job_id.strip()
        return None

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
            if str(event.get("entity_id") or "") == job_id and str(event.get("event_type") or "") == "submission"
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
            private_batch = self.repository._pipeline_event_private_runtime_root_candidates(
                job,
                event,
                candidate_budget=candidate_budget - len(candidates),
            )
            if private_batch is not None:
                candidates.extend(private_batch.candidates)
                event_candidate_total_count += private_batch.event_candidate_total_count
                continue
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
        error_code = _retry_submission_error_code(error)
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
        if latest_status in TERMINAL_SUCCESS_RETRY_STATUSES:
            return None, None
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
    payload = _redact_durable_error_message_fields(record_type, payload)
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


def _public_pipeline_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    details = row.get("details")
    row["details"] = _public_evidence(details) if isinstance(details, Mapping) else _public_evidence(details or {})
    if "message" in row:
        row["message"] = _public_message(row.get("message"))
    return row


def _private_runtime_root_recovery_record(
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
) -> dict[str, Any] | None:
    details = payload.get("details") if isinstance(payload.get("details"), Mapping) else {}
    candidates = _runtime_root_recovery_candidate_records(details)
    if not candidates:
        return None
    event_id = payload.get("event_id")
    if event_id in (None, ""):
        return None
    entity_id = _required_safe_identity(payload, "entity_id")
    record: dict[str, Any] = {
        "schema_version": FILE_ORCHESTRATION_PRIVATE_RECOVERY_SCHEMA_VERSION,
        "record_type": _PRIVATE_RUNTIME_ROOT_RECOVERY_RECORD_TYPE,
        "source_id": _normalize_file_source_id(source_id, field="source_id"),
        "cycle_time": _format_utc(cycle_time),
        "entity_type": str(payload.get("entity_type") or "pipeline_job"),
        "entity_id": entity_id,
        "event_type": str(payload.get("event_type") or ""),
        "event_id": str(event_id),
        "status_from": payload.get("status_from"),
        "status_to": payload.get("status_to"),
        "event_created_at": payload.get("created_at"),
        "created_at": _format_utc(_utcnow()),
        "candidates": candidates,
    }
    if model_id not in (None, ""):
        record["model_id"] = _safe_identity_text(str(model_id), field="model_id")
    _validate_json_complexity(
        record,
        field="private_runtime_root_recovery",
        max_nodes=MAX_FILE_JOURNAL_JSON_NODES,
        max_depth=MAX_FILE_JOURNAL_JSON_DEPTH,
    )
    return record


def _runtime_root_recovery_candidate_records(details: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for path in _RUNTIME_ROOT_EVENT_CANDIDATE_PATHS:
        candidate = _mapping_at(details, path)
        if candidate and _has_runtime_root_field(candidate):
            value = _runtime_root_recovery_candidate_value(candidate)
            if value:
                candidates.append({"path": list(path), "value": value})
    if _has_runtime_root_field(details):
        value = _runtime_root_recovery_candidate_value(details)
        if value:
            candidates.append({"path": ["details"], "value": value})
    return candidates


def _runtime_root_recovery_candidate_value(candidate: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for root_field in (*_RUNTIME_ROOT_FIELDS, *_DB_FREE_RUNTIME_FIELDS):
        if root_field not in candidate:
            continue
        value = _strip_internal_fields(candidate[root_field])
        if isinstance(value, str) and secret_manifest_value_reason(value) is not None:
            continue
        values[root_field] = value
    return values


def _private_runtime_root_recovery_path(
    root: Path,
    *,
    source_id: str,
    cycle_time: datetime,
    entity_id: str,
    event_id: str,
) -> Path:
    return (
        root
        / "private"
        / "runtime-root-recovery"
        / _safe_segment(_normalize_file_source_id(source_id, field="source_id"))
        / format_cycle_time(cycle_time)
        / _safe_segment(entity_id)
        / f"{_safe_segment(event_id)}.json"
    )


def _validate_private_runtime_root_recovery_record(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
    event: Mapping[str, Any],
) -> None:
    _require_schema(row, FILE_ORCHESTRATION_PRIVATE_RECOVERY_SCHEMA_VERSION)
    if row.get("record_type") != _PRIVATE_RUNTIME_ROOT_RECOVERY_RECORD_TYPE:
        raise FileOrchestrationJournalError("file_journal_record_type_mismatch", field="record_type")
    _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    if model_id not in (None, ""):
        _require_model_id(row, str(model_id), required=False)
    for identity_field in ("entity_type", "entity_id", "event_type", "event_id", "status_from", "status_to"):
        if _private_event_identity_value(row.get(identity_field)) != _private_event_identity_value(
            event.get(identity_field)
        ):
            raise FileOrchestrationJournalError(
                "file_journal_event_mismatch",
                field=identity_field,
                evidence={
                    "expected": _private_event_identity_value(event.get(identity_field))[:80],
                    "actual": _private_event_identity_value(row.get(identity_field))[:80],
                },
            )
    event_created_at = _private_event_identity_value(event.get("created_at"))
    if event_created_at and _private_event_identity_value(row.get("event_created_at")) != event_created_at:
        raise FileOrchestrationJournalError("file_journal_event_mismatch", field="event_created_at")


def _private_event_identity_value(value: Any) -> str:
    return "" if value in (None, "") else str(value)


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


def _durable_error_message(value: Any) -> str | None:
    if value is None:
        return None
    return _safe_error_message(str(value))


def _redact_durable_error_message_fields(record_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    if record_type in {"pipeline_job", "hydro_run", "forecast_cycle"} and "error_message" in row:
        row["error_message"] = _durable_error_message(row.get("error_message"))
    return row


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


def _file_reconcile_namespace(row: Mapping[str, Any]) -> SimpleNamespace:
    payload = dict(row)
    for dt_field in (
        "created_at",
        "updated_at",
        "submitted_at",
        "started_at",
        "finished_at",
        "cycle_time",
        "submission_attempt_started_at",
    ):
        value = payload.get(dt_field)
        if value in (None, ""):
            continue
        try:
            payload[dt_field] = _coerce_datetime(value, field=dt_field)
        except FileOrchestrationJournalError:
            pass
    return SimpleNamespace(**payload)


def _bounded_cohort_members(value: Any) -> list[dict[str, Any]]:
    """Return the durable, credential-safe ordered member identity map."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    allowed = (
        "array_task_id",
        "candidate_id",
        "run_id",
        "model_id",
        "basin_id",
        "scenario_id",
        "restart_stage",
    )
    result: list[dict[str, Any]] = []
    for item in value[:256]:
        if not isinstance(item, Mapping):
            continue
        member = {key: item.get(key) for key in allowed}
        result.append(member)
    return result


def _bounded_candidate_projections(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    allowed = (
        "candidate_id",
        "run_id",
        "model_id",
        "array_task_id",
        "array_task_outcome",
        "restart_stage",
        "native_shud_resubmitted",
    )
    return [{key: item.get(key) for key in allowed} for item in value[:256] if isinstance(item, Mapping)]


def _file_journal_real_slurm_job_id(value: Any) -> bool:
    text = str(value or "")
    return bool(text and text.lower() != "local")


def _job_needs_restart_reconcile(job: Mapping[str, Any]) -> bool:
    status = str(job.get("status") or "")
    if status == "reserved" and job.get("slurm_job_id") in (None, "") and job.get("idempotency_key") not in (None, ""):
        return True
    return status in {
        "pending",
        "queued",
        "submitted",
        "running",
        "reconcile_unverified",
    } and _file_journal_real_slurm_job_id(job.get("slurm_job_id"))


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
        "previous_job_id",
        "error_code",
        "error_message",
        "log_uri",
        "created_at",
        "updated_at",
    )
    record = {name: _file_retry_job_value(job, name) for name in fields if _file_retry_job_value(job, name) is not None}
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
    return any(field in value for field in (*_RUNTIME_ROOT_FIELDS, *_DB_FREE_RUNTIME_FIELDS))


def _file_retry_job_truth_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _datetime_sort_key(
            row.get("updated_at")
            or row.get("finished_at")
            or row.get("submitted_at")
            or row.get("started_at")
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


def _run_manifest_model_package_identity(hydro_run: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(hydro_run, Mapping):
        return None
    manifest_path = _object_store_uri_local_path(str(hydro_run.get("run_manifest_uri") or ""))
    if manifest_path is None:
        return None
    object_root = _object_store_root()
    if object_root is None:
        return None
    try:
        payload = json.loads(
            read_bytes_limited_no_follow(
                manifest_path,
                max_bytes=MAX_FILE_JOURNAL_JSON_BYTES,
                containment_root=object_root,
            ).decode("utf-8")
        )
    except (FileNotFoundError, OSError, SafeFilesystemError, json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    identity: dict[str, Any] = {"source": "run_manifest", "status": "loaded"}
    package_uri = _first_nested_text(
        payload,
        ("model", "model_package_uri"),
        ("identity", "model_package_uri"),
        ("model_package_uri",),
    )
    if package_uri is not None:
        identity["model_package_uri_sha256"] = _stable_sha256(package_uri)
    package_manifest_uri = _first_nested_text(
        payload,
        ("model", "model_package_manifest_uri"),
        ("identity", "model_package_manifest_uri"),
        ("model_package_manifest_uri",),
    )
    if package_manifest_uri is not None:
        identity["model_package_manifest_uri_sha256"] = _stable_sha256(package_manifest_uri)
    package_checksum = _first_nested_text(
        payload,
        ("model", "model_package_checksum"),
        ("identity", "model_package_checksum"),
        ("model", "package_checksum"),
        ("package_checksum",),
    )
    if package_checksum is not None:
        identity["model_package_checksum"] = package_checksum
        identity["model_package_checksum_sha256"] = _stable_sha256(package_checksum)
    if len(identity) == 2:
        return None
    return identity


def _object_store_uri_local_path(uri: str) -> Path | None:
    text = uri.strip()
    if not text:
        return None
    root = _object_store_root()
    if root is None:
        return None
    prefix = os.getenv("OBJECT_STORE_PREFIX", "").strip().rstrip("/")
    key: str | None
    if prefix and text.startswith(f"{prefix}/"):
        key = text[len(prefix) + 1 :]
    elif "://" not in text and not text.startswith("/"):
        key = text
    else:
        return None
    if not key:
        return None
    return root / key


def _object_store_root() -> Path | None:
    root = os.getenv("OBJECT_STORE_ROOT", "").strip()
    if not root:
        return None
    return Path(root).expanduser().resolve()


def _first_nested_text(payload: Mapping[str, Any], *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        current: Any = payload
        for part in path:
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(part)
        if current not in (None, ""):
            return str(current)
    return None


def _stable_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _public_scheduler_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return _public_evidence(row)


def _public_candidate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    raw_manifest = payload.get("nfs_raw_manifest")
    if isinstance(raw_manifest, Mapping):
        payload["nfs_raw_manifest"] = _public_raw_manifest_evidence(raw_manifest)
    return _public_evidence(payload)


def _public_evidence(value: Any) -> Any:
    return _sanitize_public_evidence(value)


def _sanitize_public_evidence(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_utc(value)
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
    if is_sensitive_key(key):
        return "[redacted]" if value not in (None, "") else value
    if lowered == "message" or lowered.endswith("_message"):
        return _public_message(value)
    if lowered.endswith("_path") or lowered.endswith("_root") or lowered in {"path", "root"}:
        return "[local-path]" if value not in (None, "") else value
    if lowered.endswith("_uri") or lowered in {"uri", "object_uri", "manifest_uri"}:
        return _sanitize_file_provider_evidence_scalar(key, value)
    return _sanitize_public_evidence(value)


def _sanitize_public_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    sanitized = _sanitize_public_path_or_uri_scalar(value)
    if sanitized != value:
        return sanitized
    return _sanitize_public_text(value)


def _sanitize_public_path_or_uri_scalar(value: str) -> str:
    text = value.strip()
    if not text or any(char.isspace() for char in text):
        return value
    if (
        text.startswith("/")
        or text.startswith("~")
        or "://" in text
        or text.startswith("s3:")
        or text.startswith("published:")
    ):
        return _sanitize_file_provider_evidence_scalar("uri", value)
    return value


def _public_message(value: Any) -> Any:
    if value in (None, ""):
        return value
    if not isinstance(value, str):
        return _sanitize_public_evidence(value)
    return _sanitize_public_text(value)


def _sanitize_public_text(value: str) -> str:
    redacted = _safe_error_message(value)
    return _sanitize_public_text_tokens(redacted)


def _sanitize_public_text_tokens(value: str) -> str:
    rendered: list[str] = []
    token = ""
    for char in value:
        if char.isspace():
            if token:
                rendered.append(_sanitize_public_text_token(token))
                token = ""
            rendered.append(char)
        else:
            token += char
    if token:
        rendered.append(_sanitize_public_text_token(token))
    return "".join(rendered)


def _sanitize_public_text_token(value: str) -> str:
    prefix_length = 0
    suffix_length = 0
    while prefix_length < len(value) and value[prefix_length] in "'\"([{<":
        prefix_length += 1
    while suffix_length < len(value) - prefix_length and value[len(value) - suffix_length - 1] in "'\".,;:!?)]}>":
        suffix_length += 1
    prefix = value[:prefix_length]
    suffix = value[len(value) - suffix_length :] if suffix_length else ""
    core = value[prefix_length : len(value) - suffix_length if suffix_length else len(value)]
    if not core:
        return value
    sanitized = _sanitize_public_path_or_uri_scalar(core)
    if sanitized == core:
        for separator in ("=", ":"):
            key, found, nested = core.partition(separator)
            if not found or not key or not nested:
                continue
            sanitized_nested = _sanitize_public_path_or_uri_scalar(nested)
            if sanitized_nested != nested:
                sanitized = f"{key}{found}{sanitized_nested}"
                break
    return f"{prefix}{sanitized}{suffix}" if sanitized != core else value


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
    if _job_is_unsubmitted_retry_placeholder(job, status=status):
        return False
    return status not in ("", *TERMINAL_PIPELINE_STATUSES)


def _candidate_scoped_forecast_cycle(forecast_cycle: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(forecast_cycle, Mapping):
        return None
    status = str(forecast_cycle.get("status") or "")
    if status in _TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES:
        return None
    return forecast_cycle


def _job_is_unsubmitted_retry_placeholder(job: Mapping[str, Any], *, status: str | None = None) -> bool:
    job_status = str(job.get("status") or "") if status is None else status
    if job_status not in {"pending", "queued", "submitted"}:
        return False
    if job.get("slurm_job_id") not in (None, "") or job.get("array_task_id") not in (None, ""):
        return False
    if job.get("submitted_at") not in (None, ""):
        return False
    try:
        retry_count = int(job.get("retry_count") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count > 0 and job.get("candidate_id") in (None, "") and job.get("idempotency_key") in (None, "")


def _job_is_terminal_success(job: Mapping[str, Any]) -> bool:
    return str(job.get("status") or "") in {"succeeded", "complete", "published"}


def _job_is_current_terminal_completion(job: Mapping[str, Any]) -> bool:
    stage = chain_repository_state._normalized_record_stage(job)
    if chain_repository_state._compute_state_save_qc_terminal_enabled():
        return stage == "state_save_qc"
    return stage in {"parse", "state_save_qc", "publish"}


def _current_terminal_jobs(jobs: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [job for job in jobs if chain_repository_state._record_allowed_for_compute_state_terminal(job)]


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


def _event_matches_candidate_rows(
    event: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    pipeline_jobs: Mapping[str, Mapping[str, Any]],
    forecast_cycle: Mapping[str, Any] | None,
    cycle_terminated: bool = False,
) -> bool:
    entity_type = str(event.get("entity_type") or "pipeline_job")
    entity_id = str(event.get("entity_id") or "")
    if entity_type == "pipeline_job":
        return entity_id in pipeline_jobs
    if entity_type == "forecast_cycle":
        expected_cycle_id = _cycle_id_for_file_source(source_id, cycle_time)
        if entity_id != expected_cycle_id:
            return False
        if forecast_cycle is not None:
            return str(forecast_cycle.get("cycle_id") or "") == expected_cycle_id
        # A terminally-succeeded cycle keeps its events suppressed so stale
        # cohort events cannot resurrect candidate work; a cycle with no row
        # at all still surfaces its own events (read contract).
        return not cycle_terminated
    return False


def _pipeline_event_entity_type(value: Any) -> str:
    entity_type = _scalar_text(
        "pipeline_job" if value in (None, "") else value,
        field="entity_type",
        invalid_reason="file_journal_invalid_identity",
    )
    if entity_type not in _SUPPORTED_PIPELINE_EVENT_ENTITY_TYPES:
        raise FileOrchestrationJournalError(
            "file_journal_event_entity_type_mismatch",
            field="entity_type",
            evidence={
                "expected": "|".join(sorted(_SUPPORTED_PIPELINE_EVENT_ENTITY_TYPES)),
                "actual": entity_type[:80],
            },
        )
    return entity_type


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


def _db_compatible_stage_status_order_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    stage = str(row.get("stage") or "")
    return (
        _STAGE_STATUS_ORDER.get(stage, _UNKNOWN_STAGE_STATUS_ORDER),
        stage,
        str(row.get("source_id") or ""),
        str(row.get("cycle_id") or ""),
        str(row.get("model_id") or ""),
        str(row.get("job_id") or ""),
        str(row.get("run_id") or ""),
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


def _stat_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return (file_stat.st_mtime_ns, file_stat.st_size, file_stat.st_ino)


def _decode_mapping_prevalidated(content: bytes, *, field: str) -> dict[str, Any]:
    """Decode bytes whose complexity validation already passed once.

    The complexity limits are a pure function of the bytes, so the graph
    walk is skipped for byte-identical re-reads; decoding still returns
    fresh objects on every call.
    """
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


def _cycle_source_discovery_from_segment(source_segment: str) -> _CycleSourceDiscovery:
    source_segment = _safe_segment(source_segment)
    return _CycleSourceDiscovery(
        source_id=_normalize_file_source_id(source_segment, field="source_id"),
        source_segments=(source_segment,),
    )


def _merge_cycle_source_discovery(
    sources: dict[str, _CycleSourceDiscovery],
    source: _CycleSourceDiscovery,
) -> None:
    existing = sources.get(source.source_id)
    if existing is None:
        sources[source.source_id] = source
        return
    source_segments = list(existing.source_segments)
    for source_segment in source.source_segments:
        if source_segment not in source_segments:
            source_segments.append(source_segment)
    sources[source.source_id] = _CycleSourceDiscovery(
        source_id=existing.source_id,
        source_segments=tuple(source_segments),
    )


def _cycle_read_source_segment(*, source_id: str, source_segment_override: str | None) -> str:
    if source_segment_override is None:
        return _safe_segment(source_id)
    source_segment = _safe_segment(source_segment_override)
    segment_source_id = _normalize_file_source_id(source_segment, field="source_id")
    if segment_source_id != source_id:
        raise FileOrchestrationJournalError(
            "file_journal_source_mismatch",
            field="source_id",
            evidence={"expected": source_id, "actual": segment_source_id[:80]},
        )
    return source_segment


def _cycle_read_source_segments(
    *,
    source_id: str,
    source_segment_override: str | None,
    source_segment_overrides: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    if source_segment_overrides is not None:
        segments: list[str] = []
        for source_segment_override_item in source_segment_overrides:
            segment = _cycle_read_source_segment(
                source_id=source_id,
                source_segment_override=source_segment_override_item,
            )
            if segment not in segments:
                segments.append(segment)
        if not segments:
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="source_id")
        return tuple(segments)
    primary = _cycle_read_source_segment(
        source_id=source_id,
        source_segment_override=source_segment_override,
    )
    if source_segment_override is not None:
        return (primary,)
    segments = [primary]
    for alias in (source_id.lower(), source_id.upper()):
        segment = _safe_segment(alias)
        if segment not in segments:
            segments.append(segment)
    return tuple(segments)


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
    if isinstance(value, Mapping) or (isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)):
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
        actual_run_id != expected_cycle_run_prefix and not actual_run_id.startswith(f"{expected_cycle_run_prefix}_")
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
    if (
        run_id != cycle_run_id
        and not run_id.startswith(f"{cycle_run_id}_")
        and not run_id.startswith(f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_")
    ):
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": f"{cycle_run_id}|{cycle_run_id}_<cohort>", "actual": run_id[:80]},
        )


def _validate_event_identity(
    row: Mapping[str, Any],
    *,
    source_id: str | None = None,
    cycle_time: datetime | None = None,
) -> None:
    _optional_text(row, "event_id")
    entity_id = _required_safe_identity(row, "entity_id")
    entity_type = _pipeline_event_entity_type(row.get("entity_type") or "pipeline_job")
    if entity_type == "forecast_cycle" and source_id is not None and cycle_time is not None:
        expected_cycle_id = _cycle_id_for_file_source(source_id, cycle_time)
        if entity_id != expected_cycle_id:
            raise FileOrchestrationJournalError(
                "file_journal_event_entity_mismatch",
                field="entity_id",
                evidence={"expected": expected_cycle_id, "actual": entity_id[:80]},
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
