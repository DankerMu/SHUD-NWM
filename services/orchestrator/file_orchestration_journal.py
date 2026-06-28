from __future__ import annotations

import json
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, read_bytes_limited_no_follow, stat_no_follow
from services.orchestrator import chain_repository_state
from services.orchestrator.chain_repository import (
    ACTIVE_HYDRO_STATUSES,
    COMPLETED_HYDRO_STATUSES,
    DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
)
from services.orchestrator.chain_source_cycle import _datetime_sort_key
from services.orchestrator.chain_types import ForcingContext, ModelContext, OrchestratorError
from services.orchestrator.scheduler_file_providers import (
    _public_raw_manifest_evidence,
    _sanitize_file_provider_evidence_scalar,
)
from services.orchestrator.scheduler_state import _ensure_utc, _evidence_safe, _format_utc
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
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

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
    ) -> None:
        self.root = Path(journal_root)
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.max_depth = int(max_depth)
        self.max_json_nodes = int(max_json_nodes)
        self.max_json_depth = int(max_json_depth)

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        try:
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None)
        except FileOrchestrationJournalError:
            return True
        return any(_job_is_active(job) for job in rows.pipeline_jobs.values())

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        try:
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return True
        hydro_run = rows.hydro_run
        if _row_matches_candidate(hydro_run, source_id=source_id, cycle_time=cycle_time, model_id=model_id):
            if str(hydro_run.get("status") or "") in ACTIVE_HYDRO_STATUSES:
                return True
        return any(
            _job_is_active(job)
            and _job_matches_candidate(job, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
            for job in rows.pipeline_jobs.values()
        )

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        try:
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return False
        hydro_run = rows.hydro_run
        if not _row_matches_candidate(hydro_run, source_id=source_id, cycle_time=cycle_time, model_id=model_id):
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
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return [
                {
                    "job_id": "file_journal_read_blocked",
                    "cycle_id": cycle_id_for(source_id, cycle_time),
                    "model_id": model_id,
                    "status": "running",
                    "stage": "file_journal_read",
                    "slurm_job_id": "unknown_after_attempt",
                }
            ]
        jobs = [
            _public_scheduler_row(job)
            for job in rows.pipeline_jobs.values()
            if job.get("slurm_job_id") not in (None, "")
            and _job_is_active(job)
            and _job_matches_candidate(job, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        ]
        jobs.sort(
            key=lambda job: (
                _datetime_sort_key(job.get("submitted_at")),
                _datetime_sort_key(job.get("created_at")),
            )
        )
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
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
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
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            run_id=run_id,
            forcing_version_id=forcing_version_id,
            candidate_id=candidate_id,
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
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
            if rows.forcing_version is None:
                forcing_context = self._forcing_context(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
            else:
                forcing_context = rows.forcing_version
            if forcing_context is None:
                return ForcingContext(None, None)
            return _forcing_context_from_mapping(forcing_context)
        except FileOrchestrationJournalError:
            return ForcingContext(None, None)

    def query_candidate_state(self, idempotency_key: str) -> dict[str, Any] | None:
        try:
            for job in self._iter_pipeline_job_records():
                if str(job.get("idempotency_key") or "") == idempotency_key:
                    return _public_scheduler_row(job)
        except FileOrchestrationJournalError as error:
            return _blocked_query_job(error, idempotency_key=idempotency_key)
        return None

    def get_pipeline_job(self, job_id: str) -> dict[str, Any] | None:
        try:
            _safe_segment(job_id)
            for job in self._iter_pipeline_job_records():
                if str(job.get("job_id") or "") == job_id:
                    return _public_scheduler_row(job)
        except FileOrchestrationJournalError as error:
            return _blocked_query_job(error, job_id=job_id)
        return None

    def query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        try:
            return [
                _public_scheduler_row(job)
                for job in self._iter_pipeline_job_records()
                if str(job.get("cycle_id") or "") == cycle_id
            ]
        except FileOrchestrationJournalError as error:
            return [_blocked_query_job(error, cycle_id=cycle_id)]

    def query_pipeline_jobs_by_run(self, run_id: str) -> list[dict[str, Any]]:
        try:
            return [
                _public_scheduler_row(job)
                for job in self._iter_pipeline_job_records()
                if str(job.get("run_id") or "") == run_id
            ]
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

    def _write_not_implemented(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise OrchestratorError(
            "FILE_JOURNAL_WRITE_NOT_IMPLEMENTED",
            "File orchestration journal write side is implemented in a later slice.",
        )

    ensure_forecast_cycle = _write_not_implemented
    create_hydro_run = _write_not_implemented
    create_hydro_run_from_basin = _write_not_implemented
    update_hydro_run_status = _write_not_implemented
    upsert_pipeline_job = _write_not_implemented
    reserve_pipeline_job = _write_not_implemented
    reclaim_pipeline_job_reservation = _write_not_implemented
    bind_pipeline_job_reservation = _write_not_implemented
    update_pipeline_job_status = _write_not_implemented
    insert_pipeline_event = _write_not_implemented
    update_forecast_cycle_status = _write_not_implemented

    def list_stage_statuses(
        self,
        *,
        source_id: str | None,
        cycle_time: datetime,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if source_id is None:
            return []
        rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        return [
            {
                "stage": job.get("stage"),
                "status": job.get("status"),
                "job_id": job.get("job_id"),
                "slurm_job_id": job.get("slurm_job_id"),
                "model_id": job.get("model_id"),
            }
            for job in rows.pipeline_jobs.values()
        ]

    def _cycle_rows(self, *, source_id: str, cycle_time: datetime, model_id: str | None) -> _CycleRows:
        rows = _CycleRows()
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
            self._apply_journal_record(rows, record, source_id=source_id, cycle_time=cycle_time)
        for record in self._read_jsonl(self.root / "pipeline-events" / source_segment / f"{cycle_segment}.jsonl"):
            self._apply_journal_record(
                rows,
                record,
                source_id=source_id,
                cycle_time=cycle_time,
                expected_record_type="pipeline_event",
            )
        for job in self._iter_direct_pipeline_job_records():
            if str(job.get("cycle_id") or "") == cycle_id_for(source_id, cycle_time):
                _insert_missing_by_key(rows.pipeline_jobs, job, key="job_id")
        if model_id is not None:
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
        hydro_run = _first_mapping(payload, "hydro_run", "hydro")
        if hydro_run is not None:
            _validate_hydro_run_identity(
                hydro_run,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=expected_model_id,
            )
        forecast_cycle = _first_mapping(payload, "forecast_cycle")
        if forecast_cycle is not None:
            _validate_forecast_cycle_identity(forecast_cycle, source_id=source_id, cycle_time=cycle_time)
        forcing_version = _first_mapping(payload, "forcing_version", "forcing_context")
        if forcing_version is not None:
            _validate_forcing_version_identity(
                forcing_version,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=expected_model_id,
            )
        model_context = _first_mapping(payload, "model_context")
        if model_context is not None:
            _validate_model_context_identity(model_context, model_id=expected_model_id)
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
            _upsert_by_key(rows.pipeline_jobs, job, key="job_id")
        for event in _record_list(payload, "pipeline_events", "events", single_key="pipeline_event"):
            _validate_event_identity(event)
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
    ) -> None:
        _require_schema(record, FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION)
        _require_source_cycle(record, source_id=source_id, cycle_time=cycle_time)
        payload = _payload_or_record_payload(record)
        record_type = str(record.get("record_type") or payload.get("record_type") or "")
        if expected_record_type is not None and record_type != expected_record_type:
            raise FileOrchestrationJournalError(
                "file_journal_record_type_mismatch",
                field="record_type",
                evidence={"expected": expected_record_type, "actual": record_type[:80]},
            )
        if record_type == "pipeline_job":
            _validate_pipeline_job_identity(
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=_record_model_id(record, payload),
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
                model_id=_record_model_id(record, payload),
            )
            setattr(rows, record_type, _latest_mapping(getattr(rows, record_type), payload))
        else:
            raise FileOrchestrationJournalError("file_journal_unknown_record_type", field="record_type")

    def _apply_event_record(self, rows: _CycleRows, record: Mapping[str, Any]) -> None:
        payload = _payload_or_record_payload(record)
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
            records.append(
                _decode_mapping(
                    raw_line,
                    field=f"{_relative_evidence(path, self.root)}:{line_number}",
                    max_nodes=self.max_json_nodes,
                    max_depth=self.max_json_depth,
                )
            )
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

    def _iter_pipeline_job_records(self) -> Iterable[dict[str, Any]]:
        jobs: dict[str, dict[str, Any]] = {}
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
                rows = _CycleRows()
                self._apply_journal_record(rows, record, source_id=source_id, cycle_time=cycle_time)
                for job in rows.pipeline_jobs.values():
                    _upsert_by_key(jobs, job, key="job_id")
        for job in self._iter_direct_pipeline_job_records():
            _insert_missing_by_key(jobs, job, key="job_id")
        yield from jobs.values()

    def _model_context(self, model_id: str) -> dict[str, Any] | None:
        payload = self._read_optional_json(self.root / "models" / f"{_safe_segment(model_id)}.json")
        if payload is not None:
            model_context = _payload_or_record_payload(payload)
            _validate_model_context_identity(model_context, model_id=model_id)
            return model_context
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
            forcing_context = _payload_or_record_payload(payload)
            _validate_forcing_version_identity(
                forcing_context,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                require_forcing_version_id=False,
            )
            return forcing_context
        return None

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
        source_id = _required_text(record, "source_id")
        cycle_time = _parse_cycle_time_field(record, "cycle_time")
        model_id = _record_model_id(record, payload)
        if model_id in (None, ""):
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        _validate_pipeline_job_identity(
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            expected_job_id=expected_job_id,
        )
        return payload


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
    cycle_id = cycle_id_for(source_id, cycle_time)
    return {
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
            "evidence": _evidence_safe(error.evidence),
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
    cycle_id = cycle_id_for(source_id, cycle_time)
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


def _row_matches_candidate(
    row: Mapping[str, Any] | None,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> bool:
    if not isinstance(row, Mapping):
        return False
    if str(row.get("source_id") or "") not in ("", source_id):
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
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    return dict(record)


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
    current_time = _datetime_sort_key(current.get("updated_at") or current.get("created_at"))
    incoming_time = _datetime_sort_key(incoming.get("updated_at") or incoming.get("created_at"))
    return dict(incoming) if incoming_time >= current_time else current


def _upsert_by_key(target: dict[str, dict[str, Any]], row: Mapping[str, Any], *, key: str) -> None:
    row_key = row.get(key)
    if row_key in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=key)
    existing = target.get(str(row_key))
    target[str(row_key)] = _latest_mapping(existing, row) or dict(row)


def _insert_missing_by_key(target: dict[str, dict[str, Any]], row: Mapping[str, Any], *, key: str) -> None:
    row_key = row.get(key)
    if row_key in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=key)
    target.setdefault(str(row_key), dict(row))


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


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    return str(value)


def _parse_cycle_time_field(row: Mapping[str, Any], field: str) -> datetime:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    try:
        return parse_cycle_time(str(value))
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field=field) from error


def _require_source_cycle(row: Mapping[str, Any], *, source_id: str, cycle_time: datetime) -> None:
    actual_source = _required_text(row, "source_id")
    if actual_source != source_id:
        raise FileOrchestrationJournalError(
            "file_journal_source_mismatch",
            field="source_id",
            evidence={"expected": source_id, "actual": actual_source[:80]},
        )
    parsed_cycle_time = _parse_cycle_time_field(row, "cycle_time")
    if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
        raise FileOrchestrationJournalError(
            "file_journal_cycle_mismatch",
            field="cycle_time",
            evidence={"expected": _format_utc(cycle_time), "actual": _format_utc(parsed_cycle_time)},
        )


def _require_cycle_id(row: Mapping[str, Any], expected_cycle_id: str) -> None:
    actual = _required_text(row, "cycle_id")
    if actual != expected_cycle_id:
        raise FileOrchestrationJournalError(
            "file_journal_cycle_id_mismatch",
            field="cycle_id",
            evidence={"expected": expected_cycle_id, "actual": actual[:80]},
        )


def _require_model_id(row: Mapping[str, Any], expected_model_id: str, *, required: bool) -> None:
    actual = row.get("model_id")
    if actual in (None, ""):
        if required:
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        return
    if str(actual) != expected_model_id:
        raise FileOrchestrationJournalError(
            "file_journal_model_mismatch",
            field="model_id",
            evidence={"expected": expected_model_id, "actual": str(actual)[:80]},
        )


def _record_model_id(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    value = record.get("model_id")
    if value in (None, ""):
        value = payload.get("model_id")
    return None if value in (None, "") else str(value)


def _validate_hydro_run_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> None:
    _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    _require_model_id(row, model_id, required=True)
    expected_run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
    actual_run_id = _required_text(row, "run_id")
    if actual_run_id != expected_run_id:
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": expected_run_id, "actual": actual_run_id[:80]},
        )


def _validate_forecast_cycle_identity(row: Mapping[str, Any], *, source_id: str, cycle_time: datetime) -> None:
    _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    _require_cycle_id(row, cycle_id_for(source_id, cycle_time))


def _validate_forcing_version_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    require_forcing_version_id: bool = True,
) -> None:
    if row.get("source_id") not in (None, ""):
        actual_source = str(row["source_id"])
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
    _require_model_id(row, model_id, required=False)
    forcing_version_id = row.get("forcing_version_id")
    if forcing_version_id in (None, ""):
        if require_forcing_version_id:
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="forcing_version_id")
        return
    expected_prefix = f"forc_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
    if str(forcing_version_id) != expected_prefix:
        raise FileOrchestrationJournalError(
            "file_journal_forcing_version_mismatch",
            field="forcing_version_id",
            evidence={"expected": expected_prefix, "actual": str(forcing_version_id)[:80]},
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
    job_id = _required_text(row, "job_id")
    if expected_job_id is not None and job_id != expected_job_id:
        raise FileOrchestrationJournalError(
            "file_journal_job_mismatch",
            field="job_id",
            evidence={"expected": expected_job_id, "actual": job_id[:80]},
        )
    _require_cycle_id(row, cycle_id_for(source_id, cycle_time))
    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    if model_id not in (None, ""):
        _require_model_id(row, str(model_id), required=False)
        candidate_run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
        run_id = row.get("run_id")
        if run_id not in (None, "") and str(run_id) not in {candidate_run_id, cycle_run_id}:
            raise FileOrchestrationJournalError(
                "file_journal_run_mismatch",
                field="run_id",
                evidence={"expected": f"{candidate_run_id}|{cycle_run_id}", "actual": str(run_id)[:80]},
            )
        return
    run_id = _required_text(row, "run_id")
    if run_id != cycle_run_id and not run_id.startswith(f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_"):
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": cycle_run_id, "actual": run_id[:80]},
        )


def _validate_event_identity(row: Mapping[str, Any]) -> None:
    entity_id = _required_text(row, "entity_id")
    entity_type = row.get("entity_type")
    if entity_type not in (None, "", "pipeline_job"):
        raise FileOrchestrationJournalError(
            "file_journal_event_entity_type_mismatch",
            field="entity_type",
            evidence={"expected": "pipeline_job", "actual": str(entity_type)[:80]},
        )
    if "/" in entity_id or entity_id in {".", ".."}:
        raise FileOrchestrationJournalError("file_journal_unsafe_identity", field="entity_id")


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
    source_id = _safe_segment(parts[1])
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
    source_id = _safe_segment(parts[1])
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
    count = 0

    def walk(current: Path, depth: int) -> Iterable[Path]:
        nonlocal count
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
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except FileNotFoundError:
            return
        except OSError as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        for entry in entries:
            if _SAFE_SEGMENT_RE.fullmatch(entry.name) is None:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_path_segment",
                    field=str(_relative_evidence(entry, root)),
                )
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
            if entry.name.endswith(suffix):
                if not stat.S_ISREG(mode):
                    raise FileOrchestrationJournalError(
                        "file_journal_unsafe_scanned_entry",
                        field=str(_relative_evidence(entry, root)),
                        evidence={"entry_type": "not_regular_file"},
                    )
                count += 1
                if count > max_files:
                    raise FileOrchestrationJournalError(
                        "file_journal_file_limit_exceeded",
                        field=str(_relative_evidence(directory, root)),
                        evidence={"max_files": max_files},
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
        model_id=str(row["model_id"]),
        basin_id=row.get("basin_id"),
        basin_version_id=_required_context_str(row, "basin_version_id"),
        river_network_version_id=_required_context_str(row, "river_network_version_id"),
        segment_count=_required_int(row, "segment_count"),
        model_package_uri=_required_context_str(row, "model_package_uri"),
        output_segment_count=_optional_int(row.get("output_segment_count"), field="output_segment_count"),
        model_package_checksum=_optional_str(row.get("model_package_checksum") or row.get("package_checksum")),
    )


def _forcing_context_from_mapping(row: Mapping[str, Any]) -> ForcingContext:
    return ForcingContext(
        _optional_str(row.get("forcing_version_id")),
        _optional_str(row.get("forcing_package_uri")),
        _optional_datetime(row.get("start_time"), field="start_time"),
        _optional_datetime(row.get("end_time"), field="end_time"),
        _optional_str(row.get("source_id")),
        _optional_int(row.get("max_lead_hours"), field="max_lead_hours"),
        _optional_str(row.get("forcing_package_manifest_uri")),
        _optional_str(row.get("forcing_package_manifest_checksum")),
    )


def _optional_str(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _required_context_str(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_field", field=field)
    return str(value)


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
