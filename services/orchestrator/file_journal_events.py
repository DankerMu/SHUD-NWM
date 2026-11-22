"""Status and events cluster of the file orchestration journal, as a repository mixin.

Pipeline-job status updates, pipeline-event appends (live and historical), the
event-target resolution behind them, forecast-cycle status updates and the
per-cycle stage-status listing.  The journal's error type and the historical
pipeline-event row builder stay in the facade and are imported inside the
methods that raise or call them.  The facade-resident patched clock is looked
up on the facade module at call time.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from packages.common.status_vocab import PIPELINE_JOB_SUCCESS, PIPELINE_JOB_TERMINAL_OVERRIDES
from services.orchestrator.accepted_submit_identity import (
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
)
from services.orchestrator.chain_types import OrchestratorError
from services.orchestrator.file_journal_candidates import (
    _blocked_stage_status,
    _cycle_time_from_job,
    _pipeline_event_entity_type,
    _public_scheduler_row,
    _resolved_caller_evidence,
    _source_cycle_from_cycle_id,
    _source_id_from_job,
)
from services.orchestrator.file_journal_paths import _optional_safe_identity
from services.orchestrator.file_journal_primitives import (
    _cycle_id_for_file_source,
    _normalize_file_source_id,
    _required_safe_identity,
    _safe_identity_text,
)
from services.orchestrator.file_journal_read_attribution import _install_public_read_attribution
from services.orchestrator.file_journal_replay import _db_compatible_stage_status_order_key
from services.orchestrator.file_journal_retry_helpers import _durable_error_message
from services.orchestrator.scheduler_state import _format_utc


@_install_public_read_attribution
class _FileJournalEventsMixin:
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
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        initial = self._pipeline_job_for_id_unlocked(job_id)
        if initial is None:
            raise OrchestratorError("PIPELINE_JOB_NOT_FOUND", f"pipeline_job not found: {job_id}")
        if accepted_submit_contract_is_current(initial) and accepted_submit_row_kind(initial) == "master":
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="status",
            )
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(job_id)
            if existing is None:
                raise OrchestratorError("PIPELINE_JOB_NOT_FOUND", f"pipeline_job not found: {job_id}")
            if accepted_submit_contract_is_current(existing) and accepted_submit_row_kind(existing) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="status",
                )
            previous_status = str(existing.get("status") or "") or None
            terminal_guarded = (
                previous_status in {"succeeded", "failed", "cancelled"}
                and status not in PIPELINE_JOB_TERMINAL_OVERRIDES
            )
            if previous_status == "permanently_failed" or terminal_guarded:
                return previous_status, _public_scheduler_row(existing)
            row = dict(existing)
            row["status"] = status
            # #1589 (design D3): resolve before the guards below.  No ``durable=``
            # fallback and no equality gate is added here on purpose: ``row``
            # starts as a copy of ``existing``, so a guard that declines already
            # keeps the persisted value, and this leg appending on every call --
            # gate-free -- is pre-existing behaviour that is not this fix's to
            # change.  What the fix owes is value convergence, and the guards
            # give exactly that.
            started_at = _resolved_caller_evidence(started_at)
            finished_at = _resolved_caller_evidence(finished_at)
            exit_code = _resolved_caller_evidence(exit_code)
            error_code = _resolved_caller_evidence(error_code)
            error_message = _resolved_caller_evidence(error_message)
            log_uri = _resolved_caller_evidence(log_uri)
            safe_error_message = _durable_error_message(error_message)
            for key, value in (
                ("started_at", started_at),
                ("finished_at", finished_at),
                ("exit_code", exit_code),
                ("log_uri", log_uri),
            ):
                if value is not None:
                    row[key] = _format_utc(value) if isinstance(value, datetime) else value
            if status in PIPELINE_JOB_SUCCESS and error_code is None:
                row["error_code"] = None
            elif error_code is not None:
                row["error_code"] = error_code
            if status in PIPELINE_JOB_SUCCESS and error_message is None:
                row["error_message"] = None
            elif error_message is not None:
                row["error_message"] = safe_error_message
            row["updated_at"] = _format_utc(file_orchestration_journal._utcnow())
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
        from services.orchestrator import file_orchestration_journal

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
            "created_at": _format_utc(file_orchestration_journal._utcnow()),
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
        from services.orchestrator.file_orchestration_journal import _historical_pipeline_event_row

        entity_type = _pipeline_event_entity_type(record.get("entity_type") or "pipeline_job")
        entity_id = _required_safe_identity(record, "entity_id")
        try:
            source_id, cycle_time, model_id = self._pipeline_event_target(
                entity_type=entity_type,
                entity_id=entity_id,
            )
        except OrchestratorError:
            return None
        row = _historical_pipeline_event_row(record, entity_type=entity_type, entity_id=entity_id)
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
        from services.orchestrator import file_orchestration_journal

        source_id = _normalize_file_source_id(source_id, field="source_id")
        row = {
            "cycle_id": _cycle_id_for_file_source(source_id, cycle_time),
            "source_id": source_id,
            "cycle_time": _format_utc(cycle_time),
            "issue_time": _format_utc(cycle_time),
            "status": status,
            "error_code": error_code,
            "error_message": _durable_error_message(error_message),
            "updated_at": _format_utc(file_orchestration_journal._utcnow()),
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
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

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
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

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
