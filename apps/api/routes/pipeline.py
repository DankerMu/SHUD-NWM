from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.auth import PolicyDecision, require_action
from apps.api.errors import ApiError
from packages.common.redaction import redact_payload
from packages.common.source_identity import normalize_source_id
from services.artifacts import (
    ArtifactLogError,
    ArtifactReader,
    ArtifactReaderConfig,
    safe_public_log_uri,
)
from services.orchestrator.persistence import PipelineJob, PipelineStore
from services.orchestrator.retry import RetryConfig, RetryConflictError, RetryError, RetryNotFoundError, RetryService
from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.gateway import SlurmGateway, SlurmGatewayError
from workers.data_adapters.base import format_cycle_time, parse_cycle_time

router = APIRouter(prefix="/api/v1", tags=["pipeline"])
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
PIPELINE_JOB_STATUS_VALUES = (
    "pending",
    "queued",
    "submitted",
    "running",
    "succeeded",
    "partially_failed",
    "failed",
    "submission_failed",
    "permanently_failed",
    "cancelled",
    "skipped",
)
PIPELINE_STAGE_BASIN_RESULTS_LIMIT = 50
PIPELINE_PUBLIC_LOG_URI_MAX_LENGTH = 512
_ACTIVE_JOB_STATUSES = {"pending", "queued", "submitted", "running"}
_PENDING_JOB_STATUSES = {"pending", "queued"}
_FAILED_JOB_STATUSES = {"failed", "submission_failed", "permanently_failed", "cancelled"}
_TERMINAL_HYDRO_STATUSES = {"succeeded", "parsed", "frequency_done", "published", "failed", "cancelled", "superseded"}
_TERMINAL_CYCLE_STATUSES = {
    "complete",
    "published",
    "parsed_partial",
    "failed_download",
    "failed_convert",
    "failed_forcing",
    "failed_run",
    "failed_parse",
    "failed_publish",
    "cancelled",
}
_STAGE_ORDER = ("download", "convert", "forcing", "forecast", "parse", "frequency", "publish")
_MAX_JOBS_LIMIT = 200
_MAX_LOG_BYTES = 1024 * 1024
_MAX_PUBLIC_LOG_URI_LENGTH = PIPELINE_PUBLIC_LOG_URI_MAX_LENGTH
_DISPLAY_READONLY_MODE = "display_readonly"
_CONTROL_PLANE_MANUAL_ACTION_REQUIRED = "CONTROL_PLANE_MANUAL_ACTION_REQUIRED"
_CONTROL_PLANE_QUEUE_UNAVAILABLE = "CONTROL_PLANE_QUEUE_UNAVAILABLE"


@dataclass
class _StageStats:
    total: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class _RetryExecutionContext:
    policy_decision: PolicyDecision
    service: RetryService
    gateway: SlurmGateway


@dataclass(frozen=True)
class _CancelExecutionContext:
    store: PipelineStore
    gateway: SlurmGateway


@lru_cache
def _engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def get_pipeline_store() -> Generator[PipelineStore, None, None]:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ApiError(
            status_code=500,
            code="DATABASE_URL_MISSING",
            message="DATABASE_URL is required for pipeline retry operations.",
        )
    with Session(_engine(database_url)) as session:
        yield PipelineStore(session)


def get_retry_service(
    store: PipelineStore = Depends(get_pipeline_store),
    settings: SlurmGatewaySettings = Depends(get_settings),
) -> RetryService:
    return RetryService(store, RetryConfig.from_settings(settings))


def get_slurm_gateway() -> SlurmGateway:
    from services.slurm_gateway.routes import slurm_gateway

    return slurm_gateway


def _validated_run_id(run_id: str) -> str:
    if not _SAFE_RUN_ID_RE.fullmatch(run_id):
        raise ApiError(
            status_code=400,
            code="INVALID_RUN_ID",
            message="Invalid run identifier.",
        )
    return run_id


def require_retry_action(
    request: Request,
    run_id: str = Depends(_validated_run_id),
) -> PolicyDecision:
    return require_action(request, "pipeline.retry_run", target_type="pipeline_run", target_id=run_id)


def require_cancel_action(
    request: Request,
    run_id: str = Depends(_validated_run_id),
) -> PolicyDecision:
    return require_action(request, "pipeline.cancel_run", target_type="pipeline_run", target_id=run_id)


def require_retry_control_action(
    request: Request,
    policy_decision: PolicyDecision = Depends(require_retry_action),
) -> PolicyDecision:
    _raise_display_manual_action_if_needed(request, run_id=policy_decision.target_id, control_action="retry")
    return policy_decision


def require_cancel_control_action(
    request: Request,
    policy_decision: PolicyDecision = Depends(require_cancel_action),
) -> PolicyDecision:
    _raise_display_manual_action_if_needed(request, run_id=policy_decision.target_id, control_action="cancel")
    return policy_decision


def get_retry_execution_context(
    policy_decision: PolicyDecision = Depends(require_retry_control_action),
    service: RetryService = Depends(get_retry_service),
    gateway: SlurmGateway = Depends(get_slurm_gateway),
) -> _RetryExecutionContext:
    return _RetryExecutionContext(policy_decision=policy_decision, service=service, gateway=gateway)


def get_cancel_execution_context(
    _policy_decision: PolicyDecision = Depends(require_cancel_control_action),
    store: PipelineStore = Depends(get_pipeline_store),
    gateway: SlurmGateway = Depends(get_slurm_gateway),
) -> _CancelExecutionContext:
    return _CancelExecutionContext(store=store, gateway=gateway)


def require_queue_depth_available(request: Request) -> None:
    if _display_readonly(request):
        raise ApiError(
            status_code=503,
            code=_CONTROL_PLANE_QUEUE_UNAVAILABLE,
            message="Queue depth is unavailable from a display_readonly API.",
            details={
                "display_mode": _DISPLAY_READONLY_MODE,
                "queue_depth_mode": "display_readonly_unavailable",
            },
        )


def get_queue_depth_gateway(
    _guard: None = Depends(require_queue_depth_available),
    gateway: SlurmGateway = Depends(get_slurm_gateway),
) -> SlurmGateway:
    return gateway


def _raise_display_manual_action_if_needed(request: Request, *, run_id: str, control_action: str) -> None:
    if not _display_readonly(request):
        return
    raise ApiError(
        status_code=409,
        code=_CONTROL_PLANE_MANUAL_ACTION_REQUIRED,
        message=f"Control-plane {control_action} requires manual action in display_readonly mode.",
        details={
            "run_id": run_id,
            "display_mode": _DISPLAY_READONLY_MODE,
            "suggested_action": _manual_action_suggestion(control_action),
            "recovery_runbook": "node22-control-plane-manual-recovery",
        },
    )


def _display_readonly(request: Request) -> bool:
    runtime_config = getattr(request.app.state, "runtime_config", None)
    return bool(getattr(runtime_config, "display_readonly", False))


def _manual_action_suggestion(control_action: str) -> str:
    if control_action == "retry":
        return "Ask a node 22 operator to rerun this run from the compute_control API or runbook."
    if control_action == "cancel":
        return "Ask a node 22 operator to stop this run from the compute_control API or runbook."
    return "Ask a node 22 operator to handle this control-plane action from the compute_control runbook."


@router.get("/pipeline/status")
def pipeline_status(
    request: Request,
    source: str = Query(...),
    cycle_time: str = Query(...),
    store: PipelineStore = Depends(get_pipeline_store),
) -> dict[str, Any]:
    parsed_cycle_time = _parse_cycle_time(cycle_time)
    cycle_id = _cycle_id_for_source(source, parsed_cycle_time)
    cycle = _fetch_forecast_cycle(store, source=source, cycle_time=parsed_cycle_time, cycle_id=cycle_id)
    if cycle is None:
        raise ApiError(
            status_code=404,
            code="PIPELINE_CYCLE_NOT_FOUND",
            message="No forecast cycle found for the requested source and cycle_time.",
            details={"source": source, "cycle_time": parsed_cycle_time.isoformat(), "cycle_id": cycle_id},
        )

    resolved_cycle_id = str(cycle.get("cycle_id") or cycle_id)
    return _ok(
        request,
        {
            "cycle_id": resolved_cycle_id,
            "source": cycle.get("source") or source,
            "cycle_time": cycle.get("cycle_time") or parsed_cycle_time,
            "current_state": cycle["current_state"],
            "started_at": cycle.get("started_at"),
            "updated_at": cycle.get("updated_at"),
            "job_counts": _job_count_summary(store, resolved_cycle_id),
        },
    )


@router.get("/pipeline/stages")
def pipeline_stages(
    request: Request,
    source: str = Query(...),
    cycle_time: str = Query(...),
    store: PipelineStore = Depends(get_pipeline_store),
) -> dict[str, Any]:
    parsed_cycle_time = _parse_cycle_time(cycle_time)
    cycle_id = _cycle_id_for_source(source, parsed_cycle_time)
    cycle = _fetch_forecast_cycle(store, source=source, cycle_time=parsed_cycle_time, cycle_id=cycle_id)
    if cycle is None:
        raise ApiError(
            status_code=404,
            code="PIPELINE_CYCLE_NOT_FOUND",
            message="No forecast cycle found for the requested source and cycle_time.",
            details={"source": source, "cycle_time": parsed_cycle_time.isoformat(), "cycle_id": cycle_id},
        )

    return _ok(request, _stage_summaries(store, str(cycle.get("cycle_id") or cycle_id)))


@router.get("/jobs")
def list_jobs(
    request: Request,
    source: str | None = None,
    cycle_time: str | None = None,
    status: str | None = None,
    model_id: str | None = None,
    stage: str | None = None,
    run_type: str | None = None,
    scenario: str | None = None,
    sort_by: Literal["submitted_at", "duration_seconds"] = Query(default="submitted_at"),
    sort_order: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=_MAX_JOBS_LIMIT),
    offset: int = Query(default=0, ge=0),
    store: PipelineStore = Depends(get_pipeline_store),
) -> dict[str, Any]:
    statement = select(PipelineJob)

    if cycle_time is not None:
        parsed_cycle_time = _parse_cycle_time(cycle_time)
        if source is not None:
            cycle = _fetch_forecast_cycle_or_404(store, source=source, cycle_time=parsed_cycle_time)
            statement = statement.where(
                PipelineJob.cycle_id == (cycle.get("cycle_id") or _cycle_id_for_source(source, parsed_cycle_time))
            )
        else:
            statement = statement.where(PipelineJob.cycle_id.like(f"%_{format_cycle_time(parsed_cycle_time)}"))
    elif source is not None:
        statement = statement.where(PipelineJob.cycle_id.like(f"{_cycle_id_prefix_for_source(source)}%"))

    if status is not None:
        statement = statement.where(PipelineJob.status == status)
    if model_id is not None:
        statement = statement.where(PipelineJob.model_id == model_id)
    if stage is not None:
        statement = statement.where(PipelineJob.stage == stage)

    run_ids = _run_ids_matching_filters(store, run_type=run_type, scenario=scenario)
    if run_ids is not None:
        if not run_ids:
            return _ok(request, {"items": [], "total": 0, "limit": limit, "offset": offset})
        statement = statement.where(PipelineJob.run_id.in_(run_ids))
    else:
        if run_type is not None:
            statement = statement.where(PipelineJob.run_id.like(f"%{run_type}%"))
        if scenario is not None:
            statement = statement.where(PipelineJob.run_id.like(f"%{scenario}%"))

    total = store.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    statement = statement.order_by(*_job_sort_clauses(store, sort_by, sort_order)).limit(limit).offset(offset)
    jobs = list(store.session.scalars(statement))
    run_metadata = _run_metadata_by_ids(store, {job.run_id for job in jobs if job.run_id})
    return _ok(
        request,
        {
            "items": [_job_payload(job, run_metadata.get(job.run_id or "")) for job in jobs],
            "total": int(total),
            "limit": limit,
            "offset": offset,
        },
    )


@router.get("/jobs/{job_id}/logs")
def job_logs(
    job_id: str,
    request: Request,
    store: PipelineStore = Depends(get_pipeline_store),
) -> dict[str, Any]:
    job = store.get_job(job_id)
    if job is None:
        raise ApiError(
            status_code=404,
            code="JOB_NOT_FOUND",
            message="Pipeline job was not found.",
            details={"job_id": job_id},
        )

    try:
        log_result = _artifact_reader_for_request(request).read_text_tail(job.log_uri)
    except ArtifactLogError as error:
        raise _job_log_api_error(error, job_id=job_id) from error

    return _ok(
        request,
        {
            "job_id": job.job_id,
            "log_uri": log_result.log_uri,
            "content": log_result.content,
        },
    )


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: str,
    request: Request,
    context: _RetryExecutionContext = Depends(get_retry_execution_context),
) -> dict[str, Any]:
    run_id = _validated_run_id(run_id)

    try:
        retry_gateway = context.gateway if callable(getattr(context.gateway, "submit_job", None)) else None
        if retry_gateway is None:
            raise ApiError(
                status_code=503,
                code="RETRY_EXECUTION_UNAVAILABLE",
                message="Retry execution path unavailable.",
                details={"run_id": run_id},
            )
        job = context.service.attempt_manual_retry(
            run_id,
            gateway=retry_gateway,
            policy_decision=context.policy_decision,
        )
    except RetryConflictError as error:
        raise _api_error(error) from error
    except RetryNotFoundError as error:
        raise _api_error(error) from error
    except RetryError as error:
        raise _api_error(error) from error

    if job.status == "submission_failed":
        error_message = _safe_redacted_text(job.error_message or "Retry submission failed.")
        raise ApiError(
            status_code=503,
            code=job.error_code or "RETRY_SUBMISSION_FAILED",
            message=error_message,
            details={
                "run_id": job.run_id,
                "job_id": job.job_id,
                "pipeline_job_id": job.job_id,
                "status": job.status,
                "slurm_job_id": job.slurm_job_id,
                "error_code": job.error_code,
                "error_message": error_message,
            },
        )

    return _ok(
        request,
        {
            "job_id": job.job_id,
            "pipeline_job_id": job.job_id,
            "run_id": job.run_id,
            "retry_count": job.retry_count,
            "status": job.status,
            "slurm_job_id": job.slurm_job_id,
            "execution_status": _retry_execution_status(job.status),
        },
    )


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    request: Request,
    context: _CancelExecutionContext = Depends(get_cancel_execution_context),
) -> dict[str, Any]:
    run_id = _validated_run_id(run_id)

    store = context.store
    gateway = context.gateway
    active_jobs = [job for job in store.query_jobs_by_run(run_id) if job.status in _ACTIVE_JOB_STATUSES]
    cancelled_jobs: list[dict[str, Any]] = []
    failed_jobs: list[dict[str, Any]] = []
    blocked_jobs: list[dict[str, Any]] = []
    idempotent_jobs: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for job in active_jobs:
        previous_status = job.status
        if job.slurm_job_id:
            try:
                cancellation = _coerce_mapping(gateway.cancel_job(job.slurm_job_id))
            except SlurmGatewayError as error:
                gap = _slurm_cancellation_gap_payload(job, run_id, error)
                if _is_unproven_slurm_cancel_error(error):
                    blocked_jobs.append(gap)
                    idempotent_jobs.append(
                        {
                            "job_id": job.job_id,
                            "slurm_job_id": job.slurm_job_id,
                            "note": "Slurm cancellation was not proven; local job state was preserved.",
                            "error_code": error.code,
                        }
                    )
                    store.insert_event(
                        entity_type="pipeline_job",
                        entity_id=job.job_id,
                        event_type="slurm_cancellation_gap",
                        status_from=previous_status,
                        status_to="blocked",
                        message=(
                            f"Slurm cancellation for run {run_id} was not proven; "
                            "local job state was preserved."
                        ),
                        details={
                            "run_id": run_id,
                            "slurm_job_id": job.slurm_job_id,
                            "previous_status": previous_status,
                            "error": gap["error"],
                            "cancellation_proven": False,
                        },
                    )
                    continue

                failed_jobs.append(gap)
                store.insert_event(
                    entity_type="pipeline_job",
                    entity_id=job.job_id,
                    event_type="cancel_failed",
                    status_from=previous_status,
                    status_to=previous_status,
                    message=f"Failed to cancel run {run_id}.",
                    details={
                        "run_id": run_id,
                        "slurm_job_id": job.slurm_job_id,
                        "previous_status": previous_status,
                        "error": gap["error"],
                        "cancellation_proven": False,
                    },
                )
                continue
            if not _cancellation_payload_proven(cancellation):
                gap = _unproven_slurm_cancellation_payload(job, run_id, cancellation)
                blocked_jobs.append(gap)
                store.insert_event(
                    entity_type="pipeline_job",
                    entity_id=job.job_id,
                    event_type="slurm_cancellation_gap",
                    status_from=previous_status,
                    status_to="blocked",
                    message=(
                        f"Slurm cancellation for run {run_id} did not return terminal cancelled evidence; "
                        "local job state was preserved."
                    ),
                    details={
                        "run_id": run_id,
                        "slurm_job_id": job.slurm_job_id,
                        "previous_status": previous_status,
                        "gateway_response": _safe_redacted_payload(cancellation),
                        "cancellation_proven": False,
                    },
                )
                continue

        updated = store.update_job_status(job.job_id, "cancelled", finished_at=now)
        store.insert_event(
            entity_type="pipeline_job",
            entity_id=job.job_id,
            event_type="cancel",
            status_from=previous_status,
            status_to="cancelled",
            message=f"Cancelled run {run_id}.",
            details={
                "run_id": run_id,
                "slurm_job_id": job.slurm_job_id,
                "previous_status": previous_status,
                "cancellation_proven": True,
            },
        )
        payload = _job_payload(updated)
        cancelled_jobs.append(payload)

    hydro_transition = None
    forecast_cycle_transition = None
    cancellation_gaps = [*blocked_jobs, *failed_jobs]
    if not cancellation_gaps:
        hydro_transition = _cancel_hydro_run(store, run_id)
        if hydro_transition is not None:
            store.insert_event(
                entity_type="hydro_run",
                entity_id=run_id,
                event_type="cancel",
                status_from=hydro_transition["previous_status"],
                status_to=hydro_transition["status"],
                message=f"Cancelled run {run_id}.",
                details={
                    "run_id": run_id,
                    "previous_status": hydro_transition["previous_status"],
                    "status": hydro_transition["status"],
                },
            )
        forecast_cycle_transition = _cancel_forecast_cycle(store, active_jobs)
        if forecast_cycle_transition is not None:
            store.insert_event(
                entity_type="forecast_cycle",
                entity_id=forecast_cycle_transition["cycle_id"],
                event_type="cancel",
                status_from=forecast_cycle_transition["previous_status"],
                status_to=forecast_cycle_transition["status"],
                message=f"Cancelled forecast cycle {forecast_cycle_transition['cycle_id']}.",
                details={
                    "run_id": run_id,
                    "cycle_id": forecast_cycle_transition["cycle_id"],
                    "previous_status": forecast_cycle_transition["previous_status"],
                    "status": forecast_cycle_transition["status"],
                    "preserved": forecast_cycle_transition["preserved"],
                },
            )

    return _ok(
        request,
        {
            "run_id": run_id,
            "cancelled_jobs": cancelled_jobs,
            "cancelled": cancelled_jobs,
            "failed_jobs": failed_jobs,
            "slurm_failures": failed_jobs,
            "blocked_jobs": blocked_jobs,
            "slurm_cancellation_gaps": blocked_jobs,
            "partial_failure": bool(cancellation_gaps),
            "idempotent_jobs": idempotent_jobs,
            "hydro_run": hydro_transition,
            "forecast_cycle": forecast_cycle_transition,
        },
    )


@router.get("/metrics/stage-duration")
def stage_duration_metrics(
    request: Request,
    days: int = Query(default=7, ge=1, le=365),
    source: str | None = Query(default=None),
    scenario: str | None = Query(default=None),
    store: PipelineStore = Depends(get_pipeline_store),
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    statement = select(PipelineJob).where(
        PipelineJob.started_at.is_not(None),
        PipelineJob.finished_at.is_not(None),
        PipelineJob.finished_at >= cutoff,
    )
    if source is not None:
        statement = statement.where(PipelineJob.cycle_id.like(f"{_cycle_id_prefix_for_source(source)}%"))
    run_ids = _run_ids_matching_filters(store, run_type=None, scenario=scenario)
    if run_ids is not None:
        if not run_ids:
            return _ok(request, [])
        statement = statement.where(PipelineJob.run_id.in_(run_ids))
    elif scenario is not None:
        statement = statement.where(PipelineJob.run_id.like(f"%{scenario}%"))
    jobs = list(store.session.scalars(statement))
    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for job in jobs:
        if job.stage is None or job.finished_at is None:
            continue
        duration = _duration_seconds(job.started_at, job.finished_at)
        if duration is None:
            continue
        buckets[(job.finished_at.date().isoformat(), job.stage)].append(duration)

    data = [
        {
            "date": day,
            "stage": stage,
            "average_duration_seconds": sum(durations) / len(durations),
            "job_count": len(durations),
        }
        for (day, stage), durations in sorted(buckets.items())
    ]
    return _ok(request, data)


@router.get("/metrics/success-rate")
def success_rate_metrics(
    request: Request,
    days: int = Query(default=7, ge=1, le=365),
    source: str | None = Query(default=None),
    scenario: str | None = Query(default=None),
    store: PipelineStore = Depends(get_pipeline_store),
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    statement = select(PipelineJob).where(PipelineJob.created_at >= cutoff)
    if source is not None:
        statement = statement.where(PipelineJob.cycle_id.like(f"{_cycle_id_prefix_for_source(source)}%"))
    run_ids = _run_ids_matching_filters(store, run_type=None, scenario=scenario)
    if run_ids is not None:
        if not run_ids:
            return _ok(request, [])
        statement = statement.where(PipelineJob.run_id.in_(run_ids))
    elif scenario is not None:
        statement = statement.where(PipelineJob.run_id.like(f"%{scenario}%"))
    jobs = list(store.session.scalars(statement))
    cycle_jobs: dict[str, list[PipelineJob]] = defaultdict(list)
    for job in jobs:
        if job.cycle_id:
            cycle_jobs[job.cycle_id].append(job)

    day_cycles: dict[str, list[bool]] = defaultdict(list)
    for cycle_jobs_for_id in cycle_jobs.values():
        day = max(job.finished_at or job.submitted_at or job.created_at for job in cycle_jobs_for_id).date().isoformat()
        day_cycles[day].append(all(job.status == "succeeded" for job in cycle_jobs_for_id))

    data = [
        {
            "date": day,
            "success_rate": sum(1 for success in successes if success) / len(successes),
            "succeeded_cycles": sum(1 for success in successes if success),
            "total_cycles": len(successes),
        }
        for day, successes in sorted(day_cycles.items())
    ]
    return _ok(request, data)


@router.get("/queue/depth")
def queue_depth(
    request: Request,
    gateway: SlurmGateway = Depends(get_queue_depth_gateway),
) -> dict[str, Any]:
    queue_depth_method = getattr(gateway, "queue_depth", None)
    if callable(queue_depth_method):
        try:
            depth = dict(queue_depth_method())
        except SlurmGatewayError as error:
            raise ApiError(
                status_code=error.status_code,
                code=error.code,
                message=_safe_redacted_text(error.message),
                details=_safe_redacted_payload(error.details),
            ) from error
    else:
        try:
            records = gateway.list_jobs(limit=1000, offset=0)
        except SlurmGatewayError as error:
            raise ApiError(
                status_code=error.status_code,
                code=error.code,
                message=_safe_redacted_text(error.message),
                details=_safe_redacted_payload(error.details),
            ) from error
        depth = {"running": 0, "pending": 0, "idle": 0}
        for record in records:
            status = getattr(record.status, "value", record.status)
            if status == "running":
                depth["running"] += 1
            elif status in {"pending", "submitted"}:
                depth["pending"] += 1
            elif status == "idle":
                depth["idle"] += 1

    return _ok(
        request,
        {
            "running": int(depth.get("running", 0)),
            "pending": int(depth.get("pending", 0)),
            "idle": int(depth.get("idle", 0)),
        },
    )


def _api_error(error: RetryError) -> ApiError:
    return ApiError(
        status_code=error.status_code,
        code=error.code,
        message=_safe_redacted_text(error.message),
        details=_safe_redacted_payload(error.details),
    )


def _retry_execution_status(status: str) -> str:
    if status == "pending":
        return "queued"
    if status == "submitted":
        return "submitted"
    return status


def _is_unproven_slurm_cancel_error(error: SlurmGatewayError) -> bool:
    code = error.code.lower()
    message = error.message.lower()
    return (
        error.status_code in {404, 409}
        or "not found" in code
        or "not found" in message
        or "invalid job" in message
        or "already_terminal" in code
        or "terminal" in message
        or "conflict" in code
    )


def _slurm_cancellation_gap_payload(job: PipelineJob, run_id: str, error: SlurmGatewayError) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "run_id": run_id,
        "status": job.status,
        "slurm_job_id": job.slurm_job_id,
        "cancellation_proven": False,
        "error": {
            "status_code": error.status_code,
            "code": error.code,
            "message": _safe_redacted_text(error.message),
            "details": _safe_redacted_payload(error.details or {}),
        },
    }


def _unproven_slurm_cancellation_payload(
    job: PipelineJob,
    run_id: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "run_id": run_id,
        "status": job.status,
        "slurm_job_id": job.slurm_job_id,
        "cancellation_proven": False,
        "gateway_response": _safe_redacted_payload(response),
    }


def _safe_redacted_payload(value: Any) -> Any:
    return redact_payload(value)


def _safe_redacted_text(value: str) -> str:
    redacted = redact_payload(value)
    return redacted if isinstance(redacted, str) else str(redacted)


def _safe_public_log_uri(value: str | None) -> str | None:
    return safe_public_log_uri(value, max_length=_MAX_PUBLIC_LOG_URI_LENGTH)


def _cancellation_payload_proven(response: dict[str, Any]) -> bool:
    status = str(response.get("status") or "").lower()
    if response.get("error_code"):
        return False
    if response.get("cancellation_proven") is False:
        return False
    return status == "cancelled"


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="json"))
    return dict(value)


def _cancel_hydro_run(store: PipelineStore, run_id: str) -> dict[str, Any] | None:
    column_names = _table_columns(store, "hydro_run", "hydro")
    if "run_id" not in column_names or "status" not in column_names:
        return None

    row = (
        store.session.execute(
            text("SELECT status FROM hydro.hydro_run WHERE run_id = :run_id LIMIT 1"),
            {"run_id": run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    previous_status = str(row["status"])
    if previous_status not in _TERMINAL_HYDRO_STATUSES:
        store.session.execute(
            text(
                """
                UPDATE hydro.hydro_run
                SET status = 'cancelled'
                WHERE run_id = :run_id
                """
            ),
            {"run_id": run_id},
        )
        store.session.commit()

    return {
        "run_id": run_id,
        "previous_status": previous_status,
        "status": "cancelled" if previous_status not in _TERMINAL_HYDRO_STATUSES else previous_status,
        "preserved": previous_status in _TERMINAL_HYDRO_STATUSES,
    }


def _cancel_forecast_cycle(store: PipelineStore, jobs: list[PipelineJob]) -> dict[str, Any] | None:
    cycle_id = next((job.cycle_id for job in jobs if job.cycle_id), None)
    if cycle_id is None:
        return None

    column_names = _table_columns(store, "forecast_cycle", "met")
    if "current_state" in column_names:
        state_column = "current_state"
    elif "status" in column_names:
        state_column = "status"
    else:
        state_column = None
    if "cycle_id" not in column_names or state_column is None:
        return None

    row = (
        store.session.execute(
            text(f"SELECT {state_column} AS status FROM met.forecast_cycle WHERE cycle_id = :cycle_id LIMIT 1"),
            {"cycle_id": cycle_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    previous_status = str(row["status"])
    preserved = previous_status in _TERMINAL_CYCLE_STATUSES
    if not preserved:
        updated_at_clause = ", updated_at = :updated_at" if "updated_at" in column_names else ""
        store.session.execute(
            text(
                f"""
                UPDATE met.forecast_cycle
                SET {state_column} = 'cancelled'{updated_at_clause}
                WHERE cycle_id = :cycle_id
                """
            ),
            {"cycle_id": cycle_id, "updated_at": datetime.now(UTC)},
        )
        store.session.commit()

    return {
        "cycle_id": cycle_id,
        "previous_status": previous_status,
        "status": previous_status if preserved else "cancelled",
        "preserved": preserved,
    }


def _table_columns(store: PipelineStore, table_name: str, schema: str) -> set[str]:
    try:
        inspector = inspect(store.session.get_bind())
        return {column["name"] for column in inspector.get_columns(table_name, schema=schema)}
    except (NoSuchTableError, SQLAlchemyError):
        return set()


def _ok(request: Request, data: Any) -> dict[str, Any]:
    body = {
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
        "status": "ok",
        "data": data,
    }
    decisions = getattr(request.state, "auth_policy_decisions", None)
    if decisions:
        body["auth_policy_decisions"] = decisions
    return body


def _cycle_id_prefix_for_source(source: str) -> str:
    return f"{_normalize_source_for_query(source).lower()}_"


def _cycle_id_for_source(source: str, cycle_time: datetime) -> str:
    return f"{_normalize_source_for_query(source).lower()}_{format_cycle_time(cycle_time)}"


def _normalize_source_for_query(source: str) -> str:
    try:
        return normalize_source_id(source)
    except ValueError as error:
        raise ApiError(
            status_code=422,
            code="INVALID_SOURCE",
            message="source must be a supported monitoring source.",
            details={"source": source, "supported_sources": ["GFS", "IFS", "ERA5"]},
        ) from error


def _parse_cycle_time(value: str) -> datetime:
    try:
        return parse_cycle_time(value)
    except ValueError as error:
        raise ApiError(
            status_code=422,
            code="INVALID_CYCLE_TIME",
            message="cycle_time must be a valid ISO 8601 datetime.",
            details={"cycle_time": value},
        ) from error


def _fetch_forecast_cycle(
    store: PipelineStore,
    *,
    source: str,
    cycle_time: datetime,
    cycle_id: str,
) -> dict[str, Any] | None:
    try:
        inspector = inspect(store.session.get_bind())
        column_names = {column["name"] for column in inspector.get_columns("forecast_cycle", schema="met")}
    except NoSuchTableError as error:
        raise ApiError(
            status_code=500,
            code="FORECAST_CYCLE_TABLE_MISSING",
            message="met.forecast_cycle table is not available.",
        ) from error
    except SQLAlchemyError as error:
        raise ApiError(
            status_code=500,
            code="FORECAST_CYCLE_SCHEMA_UNAVAILABLE",
            message="Unable to inspect met.forecast_cycle.",
        ) from error

    state_column = _first_present_column(column_names, ("current_state", "status"))
    if state_column is None:
        raise ApiError(
            status_code=500,
            code="FORECAST_CYCLE_SCHEMA_UNSUPPORTED",
            message="met.forecast_cycle does not expose the required monitoring columns.",
        )
    source_column = "source_id" if "source_id" in column_names else "source"
    started_expression = _first_present_column(column_names, ("started_at", "created_at")) or "NULL"
    updated_expression = _first_present_column(column_names, ("updated_at", "created_at", "started_at")) or "NULL"
    source_select = f"{source_column} AS source" if source_column in column_names else "NULL AS source"
    cycle_time_select = "cycle_time" if "cycle_time" in column_names else "NULL AS cycle_time"
    cycle_id_select = "cycle_id" if "cycle_id" in column_names else "NULL AS cycle_id"
    selected_columns = f"""
        {cycle_id_select},
        {source_select},
        {cycle_time_select},
        {state_column} AS current_state,
        {started_expression} AS started_at,
        {updated_expression} AS updated_at
    """

    if "cycle_id" in column_names:
        row = _forecast_cycle_row_by_filters(
            store,
            selected_columns=selected_columns,
            filters=["cycle_id = :cycle_id"],
            parameters={"cycle_id": cycle_id},
            source=source,
            cycle_time=cycle_time,
            cycle_id=cycle_id,
        )
        if row is not None:
            return _verified_forecast_cycle_row(
                row,
                source=source,
                cycle_time=cycle_time,
                verify_source=source_column in column_names,
                verify_cycle_time="cycle_time" in column_names,
            )

    if source_column not in column_names or "cycle_time" not in column_names:
        if "cycle_id" in column_names:
            return None
        raise ApiError(
            status_code=500,
            code="FORECAST_CYCLE_SCHEMA_UNSUPPORTED",
            message="met.forecast_cycle does not expose the required monitoring columns.",
        )

    source_aliases = _source_aliases_for_query(source)
    source_bind_names = [f"source_{index}" for index, _source in enumerate(source_aliases)]
    parameters: dict[str, Any] = {"cycle_time": cycle_time, **dict(zip(source_bind_names, source_aliases, strict=True))}
    source_filter = f"{source_column} IN ({', '.join(f':{name}' for name in source_bind_names)})"
    return _forecast_cycle_row_by_filters(
        store,
        selected_columns=selected_columns,
        filters=[source_filter, "cycle_time = :cycle_time"],
        parameters=parameters,
        source=source,
        cycle_time=cycle_time,
        cycle_id=cycle_id,
    )


def _forecast_cycle_row_by_filters(
    store: PipelineStore,
    *,
    selected_columns: str,
    filters: list[str],
    parameters: dict[str, Any],
    source: str,
    cycle_time: datetime,
    cycle_id: str,
) -> dict[str, Any] | None:
    statement = text(
        f"""
        SELECT
            {selected_columns}
        FROM met.forecast_cycle
        WHERE {" AND ".join(filters)}
        LIMIT 1
        """
    )
    try:
        row = (
            store.session.execute(
                statement,
                parameters,
            )
            .mappings()
            .first()
        )
    except SQLAlchemyError as error:
        raise ApiError(
            status_code=500,
            code="FORECAST_CYCLE_QUERY_FAILED",
            message="Failed to query met.forecast_cycle.",
            details={"source": source, "cycle_time": cycle_time.isoformat(), "cycle_id": cycle_id},
        ) from error
    return dict(row) if row is not None else None


def _verified_forecast_cycle_row(
    row: dict[str, Any],
    *,
    source: str,
    cycle_time: datetime,
    verify_source: bool,
    verify_cycle_time: bool,
) -> dict[str, Any] | None:
    if verify_source and not _forecast_cycle_source_matches(row.get("source"), source):
        return None
    if verify_cycle_time and not _forecast_cycle_time_matches(row.get("cycle_time"), cycle_time):
        return None
    return row


def _forecast_cycle_source_matches(value: Any, source: str) -> bool:
    if value is None:
        return False
    return str(value) in set(_source_aliases_for_query(source))


def _forecast_cycle_time_matches(value: Any, cycle_time: datetime) -> bool:
    parsed = _coerce_datetime(value)
    return parsed == cycle_time


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None


def _first_present_column(column_names: set[str], candidates: tuple[str, ...]) -> str | None:
    return next((column for column in candidates if column in column_names), None)


def _fetch_forecast_cycle_or_404(
    store: PipelineStore,
    *,
    source: str,
    cycle_time: datetime,
) -> dict[str, Any]:
    cycle_id = _cycle_id_for_source(source, cycle_time)
    cycle = _fetch_forecast_cycle(store, source=source, cycle_time=cycle_time, cycle_id=cycle_id)
    if cycle is None:
        raise ApiError(
            status_code=404,
            code="PIPELINE_CYCLE_NOT_FOUND",
            message="No forecast cycle found for the requested source and cycle_time.",
            details={"source": source, "cycle_time": cycle_time.isoformat(), "cycle_id": cycle_id},
        )
    return cycle


def _source_aliases_for_query(source: str) -> list[str]:
    normalized = _normalize_source_for_query(source)
    aliases = {source, normalized, normalized.upper(), normalized.lower()}
    return sorted(alias for alias in aliases if alias)


def _run_ids_matching_filters(
    store: PipelineStore,
    *,
    run_type: str | None,
    scenario: str | None,
) -> set[str] | None:
    if run_type is None and scenario is None:
        return None

    try:
        inspector = inspect(store.session.get_bind())
        column_names = {column["name"] for column in inspector.get_columns("hydro_run", schema="hydro")}
    except (NoSuchTableError, SQLAlchemyError):
        return None

    if "run_id" not in column_names:
        return None

    filters: list[str] = []
    params: dict[str, str] = {}
    if run_type is not None:
        if "run_type" not in column_names:
            return None
        filters.append("run_type = :run_type")
        params["run_type"] = run_type
    if scenario is not None:
        scenario_column = "scenario_id" if "scenario_id" in column_names else "scenario"
        if scenario_column not in column_names:
            return None
        filters.append(f"{scenario_column} = :scenario")
        params["scenario"] = scenario

    try:
        rows = store.session.execute(
            text(f"SELECT run_id FROM hydro.hydro_run WHERE {' AND '.join(filters)}"),
            params,
        ).mappings()
    except SQLAlchemyError:
        return None
    return {str(row["run_id"]) for row in rows if row.get("run_id") is not None}


def _run_metadata_by_ids(store: PipelineStore, run_ids: set[str]) -> dict[str, dict[str, str | None]]:
    if not run_ids:
        return {}

    try:
        inspector = inspect(store.session.get_bind())
        column_names = {column["name"] for column in inspector.get_columns("hydro_run", schema="hydro")}
    except (NoSuchTableError, SQLAlchemyError):
        return {}

    if "run_id" not in column_names:
        return {}

    selected_columns = ["run_id"]
    if "run_type" in column_names:
        selected_columns.append("run_type")
    scenario_column = "scenario_id" if "scenario_id" in column_names else "scenario"
    if scenario_column in column_names:
        selected_columns.append(f"{scenario_column} AS scenario")

    bind_names = [f"run_id_{index}" for index, _run_id in enumerate(run_ids)]
    params = dict(zip(bind_names, run_ids, strict=True))
    placeholders = ", ".join(f":{name}" for name in bind_names)
    try:
        rows = store.session.execute(
            text(
                f"""
                SELECT {", ".join(selected_columns)}
                FROM hydro.hydro_run
                WHERE run_id IN ({placeholders})
                """
            ),
            params,
        ).mappings()
    except SQLAlchemyError:
        return {}

    metadata: dict[str, dict[str, str | None]] = {}
    for row in rows:
        run_id = row.get("run_id")
        if run_id is None:
            continue
        metadata[str(run_id)] = {
            "run_type": str(row["run_type"]) if row.get("run_type") is not None else None,
            "scenario": str(row["scenario"]) if row.get("scenario") is not None else None,
        }
    return metadata


def _job_count_summary(store: PipelineStore, cycle_id: str) -> dict[str, int]:
    counts = {"succeeded": 0, "failed": 0, "running": 0, "pending": 0}
    failed_statuses = _FAILED_JOB_STATUSES | {"partially_failed"}
    rows = store.session.execute(
        select(PipelineJob.status, func.count()).where(PipelineJob.cycle_id == cycle_id).group_by(PipelineJob.status)
    )
    for status, count in rows:
        if status == "succeeded":
            counts["succeeded"] += count
        elif status in failed_statuses:
            counts["failed"] += count
        elif status in {"running", "submitted"}:
            counts["running"] += count
        else:
            counts["pending"] += count
    return counts


def _job_sort_clauses(
    store: PipelineStore,
    sort_by: Literal["submitted_at", "duration_seconds"],
    sort_order: Literal["asc", "desc"],
) -> list[Any]:
    descending = sort_order == "desc"
    if sort_by == "duration_seconds":
        duration_expr = _duration_sort_expression(store)
        primary = duration_expr.desc() if descending else duration_expr.asc()
        return [
            (PipelineJob.started_at.is_(None) | PipelineJob.finished_at.is_(None)).asc(),
            primary,
            PipelineJob.submitted_at.desc(),
            PipelineJob.created_at.desc(),
        ]

    primary = PipelineJob.submitted_at.desc() if descending else PipelineJob.submitted_at.asc()
    created = PipelineJob.created_at.desc() if descending else PipelineJob.created_at.asc()
    return [PipelineJob.submitted_at.is_(None).asc(), primary, created]


def _duration_sort_expression(store: PipelineStore) -> Any:
    dialect_name = store.session.get_bind().dialect.name
    if dialect_name == "sqlite":
        return func.strftime("%s", PipelineJob.finished_at) - func.strftime("%s", PipelineJob.started_at)
    if dialect_name == "postgresql":
        return func.extract("epoch", PipelineJob.finished_at - PipelineJob.started_at)
    return PipelineJob.finished_at - PipelineJob.started_at


def _stage_summaries(store: PipelineStore, cycle_id: str) -> list[dict[str, Any]]:
    stats_by_stage = _stage_stats_by_cycle(store, cycle_id)
    samples_by_stage = _stage_result_samples_by_cycle(store, cycle_id)

    stages = list(_STAGE_ORDER)
    for stage in stats_by_stage:
        if stage not in stages:
            stages.append(stage)

    summaries: list[dict[str, Any]] = []
    previous_failed = False
    for stage in stages:
        stats = stats_by_stage.get(stage, _StageStats())
        stage_jobs = samples_by_stage.get(stage, [])
        display_status = _stage_display_status(stats.status_counts)
        if stats.total == 0 and previous_failed:
            display_status = "skipped"
        if display_status in {"failed", "partially_failed"}:
            previous_failed = True
        basin_results_total = stats.total
        basin_results_returned = len(stage_jobs)

        summaries.append(
            {
                "stage": stage,
                "display_status": display_status,
                "status": display_status,
                "duration_seconds": _duration_seconds(stats.started_at, stats.finished_at),
                "basin_progress": _basin_progress(stats),
                "basin_results_limit": PIPELINE_STAGE_BASIN_RESULTS_LIMIT,
                "basin_results_total": basin_results_total,
                "basin_results_returned": basin_results_returned,
                "basin_results_truncated": basin_results_returned < basin_results_total,
                "basin_results": [_basin_result(job) for job in stage_jobs],
            }
        )
    return summaries


def _stage_stats_by_cycle(store: PipelineStore, cycle_id: str) -> dict[str, _StageStats]:
    stats_by_stage: dict[str, _StageStats] = {}
    latest_jobs = _latest_stage_truth_job_ids_by_cycle(cycle_id)
    rows = store.session.execute(
        select(
            PipelineJob.stage,
            PipelineJob.status,
            func.count(),
            func.min(PipelineJob.started_at),
            func.max(PipelineJob.finished_at),
        )
        .join(latest_jobs, PipelineJob.job_id == latest_jobs.c.job_id)
        .where(latest_jobs.c.truth_row_number == 1)
        .where(PipelineJob.cycle_id == cycle_id)
        .where(PipelineJob.stage.is_not(None))
        .group_by(PipelineJob.stage, PipelineJob.status)
    )
    for stage, status, count, started_at, finished_at in rows:
        if stage is None:
            continue
        stats = stats_by_stage.setdefault(str(stage), _StageStats())
        status_text = str(status)
        count_int = int(count)
        stats.total += count_int
        stats.status_counts[status_text] = stats.status_counts.get(status_text, 0) + count_int
        if started_at is not None and (stats.started_at is None or started_at < stats.started_at):
            stats.started_at = started_at
        if finished_at is not None and (stats.finished_at is None or finished_at > stats.finished_at):
            stats.finished_at = finished_at
    return stats_by_stage


def _stage_result_samples_by_cycle(store: PipelineStore, cycle_id: str) -> dict[str, list[PipelineJob]]:
    latest_jobs = _latest_stage_truth_job_ids_by_cycle(cycle_id)
    ranked_jobs = (
        select(
            PipelineJob.job_id.label("job_id"),
            func.row_number()
            .over(
                partition_by=PipelineJob.stage,
                order_by=(PipelineJob.submitted_at.asc(), PipelineJob.created_at.asc(), PipelineJob.job_id.asc()),
            )
            .label("stage_row_number"),
        )
        .join(latest_jobs, PipelineJob.job_id == latest_jobs.c.job_id)
        .where(latest_jobs.c.truth_row_number == 1)
        .where(PipelineJob.cycle_id == cycle_id)
        .where(PipelineJob.stage.is_not(None))
        .subquery()
    )
    statement = (
        select(PipelineJob)
        .join(ranked_jobs, PipelineJob.job_id == ranked_jobs.c.job_id)
        .where(ranked_jobs.c.stage_row_number <= PIPELINE_STAGE_BASIN_RESULTS_LIMIT)
        .order_by(
            PipelineJob.stage.asc(),
            PipelineJob.submitted_at.asc(),
            PipelineJob.created_at.asc(),
            PipelineJob.job_id.asc(),
        )
    )
    samples_by_stage: dict[str, list[PipelineJob]] = defaultdict(list)
    for job in store.session.scalars(statement):
        if job.stage is None:
            continue
        samples_by_stage[str(job.stage)].append(job)
    return samples_by_stage


def _latest_stage_truth_job_ids_by_cycle(cycle_id: str) -> Any:
    logical_run_id = func.coalesce(func.nullif(PipelineJob.run_id, ""), PipelineJob.job_id)
    return (
        select(
            PipelineJob.job_id.label("job_id"),
            func.row_number()
            .over(
                partition_by=(PipelineJob.cycle_id, PipelineJob.stage, logical_run_id),
                order_by=_latest_stage_truth_order(descending=True),
            )
            .label("truth_row_number"),
        )
        .where(PipelineJob.cycle_id == cycle_id)
        .where(PipelineJob.stage.is_not(None))
        .subquery()
    )


def _latest_stage_truth_order(*, descending: bool) -> tuple[Any, ...]:
    if descending:
        return (
            PipelineJob.submitted_at.desc().nulls_last(),
            PipelineJob.created_at.desc().nulls_last(),
            PipelineJob.updated_at.desc().nulls_last(),
            PipelineJob.finished_at.desc().nulls_last(),
            PipelineJob.job_id.desc(),
        )
    return (
        PipelineJob.submitted_at.asc().nulls_last(),
        PipelineJob.created_at.asc().nulls_last(),
        PipelineJob.updated_at.asc().nulls_last(),
        PipelineJob.finished_at.asc().nulls_last(),
        PipelineJob.job_id.asc(),
    )


def _stage_display_status(status_counts: dict[str, int]) -> str:
    if not status_counts:
        return "pending"

    statuses = set(status_counts)
    if statuses & _ACTIVE_JOB_STATUSES:
        return "running"
    if statuses == {"skipped"}:
        return "skipped"
    if statuses <= {"succeeded", "skipped"}:
        return "succeeded" if "succeeded" in statuses else "skipped"
    if "partially_failed" in statuses:
        return "partially_failed"
    if statuses & _FAILED_JOB_STATUSES:
        return "partially_failed" if "succeeded" in statuses else "failed"
    if statuses == {"succeeded"}:
        return "succeeded"
    return "running" if statuses & _ACTIVE_JOB_STATUSES else "failed"


def _basin_progress(stats: _StageStats) -> dict[str, int]:
    failed = sum(count for status, count in stats.status_counts.items() if status in _FAILED_JOB_STATUSES)
    failed += stats.status_counts.get("partially_failed", 0)
    completed = stats.status_counts.get("succeeded", 0)
    return {"completed": completed, "total": stats.total, "failed": failed}


def _basin_result(job: PipelineJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "cycle_id": job.cycle_id,
        "job_type": job.job_type,
        "slurm_job_id": job.slurm_job_id,
        "model_id": job.model_id,
        "basin_id": None,
        "status": job.status,
        "stage": job.stage,
        "submitted_at": job.submitted_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "duration_seconds": _duration_seconds(job.started_at, job.finished_at),
        "retry_count": job.retry_count,
        "error_code": job.error_code,
        "error_message": _safe_redacted_text(job.error_message) if job.error_message is not None else None,
        "log_uri": _safe_public_log_uri(job.log_uri),
    }


def _job_payload(job: PipelineJob, run_metadata: dict[str, str | None] | None = None) -> dict[str, Any]:
    run_metadata = run_metadata or {}
    return {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "cycle_id": job.cycle_id,
        "run_type": run_metadata.get("run_type"),
        "scenario": run_metadata.get("scenario"),
        "job_type": job.job_type,
        "slurm_job_id": job.slurm_job_id,
        "model_id": job.model_id,
        "status": job.status,
        "stage": job.stage,
        "submitted_at": job.submitted_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "exit_code": job.exit_code,
        "retry_count": job.retry_count,
        "error_code": job.error_code,
        "error_message": _safe_redacted_text(job.error_message) if job.error_message is not None else None,
        "log_uri": _safe_public_log_uri(job.log_uri),
        "duration_seconds": _duration_seconds(job.started_at, job.finished_at),
    }


def _duration_seconds(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    if started_at is None or finished_at is None:
        return None
    return max(0, int((finished_at - started_at).total_seconds()))


def _artifact_reader_for_request(request: Request) -> ArtifactReader:
    reader = getattr(request.app.state, "artifact_reader", None)
    if isinstance(reader, ArtifactReader):
        return reader
    runtime_config = getattr(request.app.state, "runtime_config", None)
    display_readonly = bool(getattr(runtime_config, "display_readonly", False))
    config = ArtifactReaderConfig.from_env(display_readonly=display_readonly)
    config = ArtifactReaderConfig(
        published_root=config.published_root,
        uri_prefix=config.uri_prefix,
        s3_bucket=config.s3_bucket,
        s3_prefix=config.s3_prefix,
        tail_max_bytes=min(config.tail_max_bytes, _MAX_LOG_BYTES),
        allow_legacy_local_file_logs=config.allow_legacy_local_file_logs,
        legacy_log_root=config.legacy_log_root,
        display_readonly=config.display_readonly,
    )
    return ArtifactReader(config)


def _job_log_api_error(error: ArtifactLogError, *, job_id: str) -> ApiError:
    details: dict[str, Any] = {"job_id": job_id}
    if error.safe_uri is not None:
        details["log_uri"] = error.safe_uri
    if error.reason is not None:
        details["reason"] = error.reason
    return ApiError(
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        details=details,
    )
