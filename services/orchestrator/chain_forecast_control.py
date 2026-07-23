from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from packages.common.source_identity import normalize_source_id
from services.orchestrator import chain as _chain
from services.orchestrator.accepted_submit_identity import (
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
)
from services.orchestrator.chain_types import (
    CycleOrchestrationContext,
    DisplayLogPublicationAttempt,
    ModelContext,
    OrchestratorError,
    PipelineResult,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

__all__ = (
    "cancel_active_cycle_jobs",
    "orchestrate_cycle",
    "sync_cycle_statuses",
)


def _active_orchestration_conflicts(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_active_orchestration_conflicts")(*args, **kwargs)


def _coerce_mapping(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return getattr(_chain, "_coerce_mapping")(*args, **kwargs)


def _cycle_orchestration_run_id(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_cycle_orchestration_run_id")(*args, **kwargs)


def _format_time(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_format_time")(*args, **kwargs)


def _in_memory_active_cycle_conflicts(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_in_memory_active_cycle_conflicts")(*args, **kwargs)


def _parse_gateway_time(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_parse_gateway_time")(*args, **kwargs)


def _resource_metrics_from_payload(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_resource_metrics_from_payload")(*args, **kwargs)


def _restart_stage_from_basins(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_restart_stage_from_basins")(*args, **kwargs)


def _retry_attempt_from_basins(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_retry_attempt_from_basins")(*args, **kwargs)


def _safe_pipeline_event_details(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_safe_pipeline_event_details")(*args, **kwargs)


def _slurm_accounting_from_payload(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_slurm_accounting_from_payload")(*args, **kwargs)


def _slurm_client_error_cls() -> type[Exception]:
    return getattr(_chain, "SlurmClientError")


def _is_current_accepted_master(job: Mapping[str, Any]) -> bool:
    return accepted_submit_contract_is_current(job) and accepted_submit_row_kind(job) == "master"


def _stage_status_message(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_stage_status_message")(*args, **kwargs)


def _status_from_gateway_job(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_status_from_gateway_job")(*args, **kwargs)


def _terminal_job_statuses() -> set[str]:
    return getattr(_chain, "TERMINAL_JOB_STATUSES")


def _utcnow(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_utcnow")(*args, **kwargs)


def _validate_safe_id(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_validate_safe_id")(*args, **kwargs)


def orchestrate_cycle(
    self: Any,
    source: str,
    cycle_time: str | datetime,
    basins: Sequence[Mapping[str, Any] | ModelContext],
) -> PipelineResult:
    _validate_safe_id("source", source)
    source = normalize_source_id(source)
    parsed_cycle_time = parse_cycle_time(cycle_time)
    cycle_id = cycle_id_for(source, parsed_cycle_time)
    _validate_safe_id("cycle_id", cycle_id)

    normalized_basins = self._normalize_cycle_basins(basins, source, parsed_cycle_time)
    if not normalized_basins:
        raise OrchestratorError("EMPTY_BASIN_LIST", "orchestrate_cycle requires at least one basin.")
    self._apply_cohort_warm_start(normalized_basins, source, parsed_cycle_time)
    self._validate_cycle_basin_identities(normalized_basins, source, parsed_cycle_time, cycle_id)
    context_run_id = _cycle_orchestration_run_id(source, parsed_cycle_time, normalized_basins)
    if _active_orchestration_conflicts(
        self.repository,
        source_id=source,
        cycle_time=parsed_cycle_time,
        cycle_id=cycle_id,
        run_id=context_run_id,
        basins=normalized_basins,
    ):
        raise OrchestratorError(
            "PIPELINE_ALREADY_ACTIVE",
            f"An active orchestration already exists for {source} {format_cycle_time(parsed_cycle_time)}.",
            {"source_id": source, "cycle_time": _format_time(parsed_cycle_time), "cycle_id": cycle_id},
        )
    if _in_memory_active_cycle_conflicts(cycle_id, self._active_cycles, normalized_basins):
        raise OrchestratorError(
            "PIPELINE_ALREADY_ACTIVE",
            f"An active orchestration already exists for {source} {format_cycle_time(parsed_cycle_time)}.",
            {"source_id": source, "cycle_time": _format_time(parsed_cycle_time), "cycle_id": cycle_id},
        )

    self._active_cycles.add(cycle_id)
    try:
        self.repository.ensure_forecast_cycle(source_id=source, cycle_time=parsed_cycle_time)
        context = CycleOrchestrationContext(
            source_id=source,
            cycle_time=parsed_cycle_time,
            cycle_id=cycle_id,
            run_id=context_run_id,
            all_basins=normalized_basins,
            active_basins=list(normalized_basins),
            restart_stage=_restart_stage_from_basins(normalized_basins),
            retry_attempt=_retry_attempt_from_basins(normalized_basins),
        )
        return self._run_cycle_chain(context)
    finally:
        self._active_cycles.discard(cycle_id)


def sync_cycle_statuses(self: Any, cycle_id: str) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    deferred_publish_attempt: DisplayLogPublicationAttempt | None = None
    for job in self._query_pipeline_jobs_by_cycle(cycle_id):
        slurm_job_id = str(job.get("slurm_job_id") or "")
        if (
            str(job.get("status")) in _terminal_job_statuses()
            or not slurm_job_id
            or slurm_job_id.lower() == "local"
        ):
            continue
        gateway_job = _coerce_mapping(self.slurm_client.get_job_status(slurm_job_id))
        new_status = _status_from_gateway_job(gateway_job)
        if new_status == str(job.get("status")):
            continue
        current_master = _is_current_accepted_master(job)
        if current_master and new_status in _terminal_job_statuses():
            # Gateway master state is not sufficient terminal truth for a
            # forecast array. Restart reconcile owns exact task projection.
            continue
        publication = (
            self._display_log_publication_for_pipeline_job(job) if new_status in _terminal_job_statuses() else None
        )
        publication_attempt = (
            self._try_publish_log_for_advertise(slurm_job_id, publication)
            if publication is not None
            else None
        )
        log_uri = publication_attempt.advertised_uri if publication_attempt is not None else None
        if current_master:
            transition = self.repository.transition_pipeline_job_runtime_status(
                str(job["job_id"]),
                new_status,
                expected_statuses=(str(job.get("status") or ""),),
                started_at=_parse_gateway_time(gateway_job.get("started_at")),
                exit_code=gateway_job.get("exit_code"),
            )
            if not transition.committed:
                continue
            previous_status = str(job.get("status") or "")
            record = dict(transition.row or {})
        else:
            previous_status, record = self.repository.update_pipeline_job_status(
                str(job["job_id"]),
                new_status,
                started_at=_parse_gateway_time(gateway_job.get("started_at")),
                finished_at=_parse_gateway_time(gateway_job.get("finished_at")),
                exit_code=gateway_job.get("exit_code"),
                error_code=gateway_job.get("error_code"),
                error_message=gateway_job.get("error_message"),
                log_uri=str(log_uri) if log_uri else None,
            )
        if str(record.get("status")) != new_status:
            continue
        details = _safe_pipeline_event_details(
            {
                "cycle_id": cycle_id,
                "slurm_job_id": job.get("slurm_job_id"),
                "exit_code": gateway_job.get("exit_code"),
                "error_code": gateway_job.get("error_code"),
                "slurm": {
                    "job_id": job.get("slurm_job_id"),
                    "state": gateway_job.get("state") or gateway_job.get("status"),
                    "exit_code": gateway_job.get("exit_code"),
                    "log_uri": log_uri,
                    "accounting": _slurm_accounting_from_payload(gateway_job),
                    "resource_metrics": _resource_metrics_from_payload(gateway_job),
                },
            }
        )
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=str(job["job_id"]),
            event_type="status_change",
            status_from=previous_status or str(job.get("status")),
            status_to=new_status,
            message=_stage_status_message(str(job.get("stage") or job.get("job_type")), new_status, gateway_job),
            details=details,
        )
        updates.append(record)
        if (
            deferred_publish_attempt is None
            and publication_attempt is not None
            and publication_attempt.error is not None
        ):
            deferred_publish_attempt = publication_attempt
    self._raise_publish_error_after_durable_update(deferred_publish_attempt)
    return updates


def cancel_active_cycle_jobs(self: Any, cycle_id: str, *, reason: str = "operator_requested") -> list[dict[str, Any]]:
    cancelled: list[dict[str, Any]] = []
    cancel_job = getattr(self.slurm_client, "cancel_job", None)
    slurm_client_error_cls = _slurm_client_error_cls()
    if not callable(cancel_job):
        raise slurm_client_error_cls(
            "SLURM_CANCEL_UNSUPPORTED",
            "Slurm Gateway client does not expose a cancel contract.",
            {"cycle_id": cycle_id},
        )
    for job in self._query_pipeline_jobs_by_cycle(cycle_id):
        status = str(job.get("status") or "")
        slurm_job_id = job.get("slurm_job_id")
        if status in _terminal_job_statuses() or not slurm_job_id:
            continue
        current_master = _is_current_accepted_master(job)
        if current_master:
            intent = self.repository.request_pipeline_job_cancellation(
                str(job["job_id"]),
                expected_statuses=(status,),
                reason=reason,
            )
            if not intent.committed:
                continue
            status = str((intent.row or {}).get("status") or "cancellation_pending")
            self.repository.insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=str(job["job_id"]),
                event_type="cancellation_requested",
                status_from=str(job.get("status") or ""),
                status_to=status,
                message=f"Cancellation intent persisted for Slurm job {slurm_job_id}.",
                details=_safe_pipeline_event_details(
                    {
                        "cycle_id": cycle_id,
                        "reason": reason,
                        "slurm_job_id": slurm_job_id,
                        "replacement_submitted": False,
                    }
                ),
            )
        try:
            cancelled_payload = _coerce_mapping(cancel_job(str(slurm_job_id)))
        except slurm_client_error_cls as error:
            details = dict(error.details or {})
            response = details.get("response")
            response_mapping = response if isinstance(response, Mapping) else {}
            error_mapping = response_mapping.get("error") if isinstance(response_mapping, Mapping) else None
            gateway_details = dict(error_mapping.get("details") or {}) if isinstance(error_mapping, Mapping) else {}
            if error.error_code == "JOB_ALREADY_TERMINAL":
                details_payload = _safe_pipeline_event_details(
                    {
                        "cycle_id": cycle_id,
                        "stage": job.get("stage"),
                        "job_type": job.get("job_type"),
                        "reason": reason,
                        "replacement_submitted": False,
                        "error_code": error.error_code,
                        "gateway_status": gateway_details.get("status"),
                        "gateway_details": gateway_details,
                        "slurm": {
                            "job_id": slurm_job_id,
                            "state": gateway_details.get("status"),
                            "log_uri": job.get("log_uri"),
                            "cancellation_proven": False,
                        },
                    }
                )
                self.repository.insert_pipeline_event(
                    entity_type="pipeline_job",
                    entity_id=str(job["job_id"]),
                    event_type="slurm_cancellation_gap",
                    status_from=status,
                    status_to="blocked",
                    message=(
                        f"Slurm job {slurm_job_id} was already terminal at the gateway; "
                        "pipeline state was not rewritten to cancelled."
                    ),
                    details=details_payload,
                )
                cancelled.append(
                    {
                        **dict(job),
                        "status": status,
                        "error_code": error.error_code,
                        "cancellation_proven": False,
                        "replacement_submitted": False,
                    }
                )
                continue
            raise
        if current_master:
            completion = self.repository.complete_pipeline_job_cancellation(
                str(job["job_id"]),
                finished_at=_parse_gateway_time(cancelled_payload.get("finished_at")) or _utcnow(),
                exit_code=cancelled_payload.get("exit_code"),
                error_code=cancelled_payload.get("error_code"),
                error_message=cancelled_payload.get("error_message"),
                log_uri=job.get("log_uri"),
            )
            if not completion.committed:
                continue
            previous_status = status
            record = dict(completion.row or {})
            persisted_status = str(record.get("status") or "reconcile_unverified")
        else:
            previous_status, record = self.repository.update_pipeline_job_status(
                str(job["job_id"]),
                "cancelled",
                finished_at=_parse_gateway_time(cancelled_payload.get("finished_at")) or _utcnow(),
                exit_code=cancelled_payload.get("exit_code"),
                error_code=cancelled_payload.get("error_code"),
                error_message=cancelled_payload.get("error_message"),
                log_uri=job.get("log_uri"),
            )
            persisted_status = "cancelled"
        details = _safe_pipeline_event_details(
            {
                "cycle_id": cycle_id,
                "stage": job.get("stage"),
                "job_type": job.get("job_type"),
                "reason": reason,
                "replacement_submitted": False,
                "slurm": {
                    "job_id": slurm_job_id,
                    "state": cancelled_payload.get("status", "cancelled"),
                    "exit_code": cancelled_payload.get("exit_code"),
                    "error_code": cancelled_payload.get("error_code"),
                    "error_message": cancelled_payload.get("error_message"),
                    "log_uri": job.get("log_uri") or cancelled_payload.get("log_uri"),
                },
            }
        )
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=str(job["job_id"]),
            event_type="cancel",
            status_from=previous_status or status,
            status_to=persisted_status,
            message=(
                f"Cancelled Slurm job {slurm_job_id}; exact task reconciliation remains pending."
                if current_master
                else f"Cancelled Slurm job {slurm_job_id}; no replacement submitted in this pass."
            ),
            details=details,
        )
        cancelled.append(record)
    return cancelled
