from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.errors import ApiError
from services.orchestrator.persistence import PipelineJob, PipelineStore
from services.orchestrator.retry import RetryConfig, RetryConflictError, RetryError, RetryNotFoundError, RetryService
from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.gateway import SlurmGateway, SlurmGatewayError
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

router = APIRouter(prefix="/api/v1", tags=["pipeline"])
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_OPERATOR_ROLES = {"operator", "model_admin", "sys_admin"}
_ACTIVE_JOB_STATUSES = {"pending", "submitted", "running"}
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
LOG_ROOT = Path(os.getenv("LOG_ROOT", "workspace"))


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


@router.get("/pipeline/status")
def pipeline_status(
    request: Request,
    source: str = Query(...),
    cycle_time: str = Query(...),
    store: PipelineStore = Depends(get_pipeline_store),
) -> dict[str, Any]:
    parsed_cycle_time = _parse_cycle_time(cycle_time)
    cycle_id = cycle_id_for(source, parsed_cycle_time)
    cycle = _fetch_forecast_cycle(store, source=source, cycle_time=parsed_cycle_time, cycle_id=cycle_id)
    if cycle is None:
        raise ApiError(
            status_code=404,
            code="PIPELINE_CYCLE_NOT_FOUND",
            message="No forecast cycle found for the requested source and cycle_time.",
            details={"source": source, "cycle_time": parsed_cycle_time.isoformat(), "cycle_id": cycle_id},
        )

    return _ok(
        request,
        {
            "cycle_id": cycle.get("cycle_id") or cycle_id,
            "source": cycle.get("source") or source,
            "cycle_time": cycle.get("cycle_time") or parsed_cycle_time,
            "current_state": cycle["current_state"],
            "started_at": cycle.get("started_at"),
            "updated_at": cycle.get("updated_at"),
            "job_counts": _job_count_summary(store, cycle_id),
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
    cycle_id = cycle_id_for(source, parsed_cycle_time)
    cycle = _fetch_forecast_cycle(store, source=source, cycle_time=parsed_cycle_time, cycle_id=cycle_id)
    if cycle is None:
        raise ApiError(
            status_code=404,
            code="PIPELINE_CYCLE_NOT_FOUND",
            message="No forecast cycle found for the requested source and cycle_time.",
            details={"source": source, "cycle_time": parsed_cycle_time.isoformat(), "cycle_id": cycle_id},
        )

    jobs = store.query_jobs_by_cycle(cycle_id)
    return _ok(request, _stage_summaries(jobs))


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
            statement = statement.where(PipelineJob.cycle_id == cycle_id_for(source, parsed_cycle_time))
        else:
            statement = statement.where(PipelineJob.cycle_id.like(f"%_{format_cycle_time(parsed_cycle_time)}"))
    elif source is not None:
        statement = statement.where(PipelineJob.cycle_id.like(f"{source.lower()}_%"))

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
    if not job.log_uri:
        raise ApiError(
            status_code=404,
            code="JOB_LOG_NOT_FOUND",
            message="No log_uri is available for this job.",
            details={"job_id": job_id},
        )

    log_path = _local_log_path(job.log_uri)
    if log_path is None or not log_path.is_file():
        raise ApiError(
            status_code=404,
            code="JOB_LOG_NOT_FOUND",
            message="Job log file was not found.",
            details={"job_id": job_id, "log_uri": job.log_uri},
        )

    return _ok(
        request,
        {
            "job_id": job.job_id,
            "log_uri": job.log_uri,
            "content": _read_log_tail(log_path),
        },
    )


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: str,
    request: Request,
    service: RetryService = Depends(get_retry_service),
    gateway: SlurmGateway = Depends(get_slurm_gateway),
) -> dict[str, Any]:
    _require_operator_role(request)
    if not _SAFE_RUN_ID_RE.fullmatch(run_id):
        raise ApiError(
            status_code=400,
            code="INVALID_RUN_ID",
            message="Invalid run identifier.",
        )

    try:
        retry_gateway = gateway if callable(getattr(gateway, "submit_job", None)) else None
        if retry_gateway is None:
            raise ApiError(
                status_code=503,
                code="RETRY_EXECUTION_UNAVAILABLE",
                message="Retry execution path unavailable.",
                details={"run_id": run_id},
            )
        job = service.attempt_manual_retry(run_id, gateway=retry_gateway)
    except RetryConflictError as error:
        raise _api_error(error) from error
    except RetryNotFoundError as error:
        raise _api_error(error) from error
    except RetryError as error:
        raise _api_error(error) from error

    if job.status == "submission_failed":
        raise ApiError(
            status_code=503,
            code=job.error_code or "RETRY_SUBMISSION_FAILED",
            message=job.error_message or "Retry submission failed.",
            details={
                "run_id": job.run_id,
                "job_id": job.job_id,
                "pipeline_job_id": job.job_id,
                "status": job.status,
                "slurm_job_id": job.slurm_job_id,
                "error_code": job.error_code,
                "error_message": job.error_message,
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
    store: PipelineStore = Depends(get_pipeline_store),
    gateway: SlurmGateway = Depends(get_slurm_gateway),
) -> dict[str, Any]:
    _require_operator_role(request)
    if not _SAFE_RUN_ID_RE.fullmatch(run_id):
        raise ApiError(
            status_code=400,
            code="INVALID_RUN_ID",
            message="Invalid run identifier.",
        )

    active_jobs = [job for job in store.query_jobs_by_run(run_id) if job.status in _ACTIVE_JOB_STATUSES]
    cancelled_jobs: list[dict[str, Any]] = []
    failed_jobs: list[dict[str, Any]] = []
    idempotent_jobs: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for job in active_jobs:
        previous_status = job.status
        idempotent = False
        if job.slurm_job_id:
            try:
                gateway.cancel_job(job.slurm_job_id)
            except SlurmGatewayError as error:
                if not _is_idempotent_slurm_cancel_error(error):
                    failure = {
                        "job_id": job.job_id,
                        "run_id": run_id,
                        "status": job.status,
                        "slurm_job_id": job.slurm_job_id,
                        "error": {
                            "status_code": error.status_code,
                            "code": error.code,
                            "message": error.message,
                            "details": error.details or {},
                        },
                    }
                    failed_jobs.append(failure)
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
                            "error": failure["error"],
                        },
                    )
                    continue
                idempotent = True

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
                "idempotent": idempotent,
            },
        )
        payload = _job_payload(updated)
        cancelled_jobs.append(payload)
        if idempotent:
            idempotent_jobs.append(
                {
                    "job_id": job.job_id,
                    "slurm_job_id": job.slurm_job_id,
                    "note": "Slurm job was already terminal or absent; local job was marked cancelled.",
                }
            )

    hydro_transition = None
    forecast_cycle_transition = None
    if not failed_jobs:
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
            "partial_failure": bool(failed_jobs),
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
        statement = statement.where(PipelineJob.cycle_id.like(f"{source.lower()}_%"))
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
        statement = statement.where(PipelineJob.cycle_id.like(f"{source.lower()}_%"))
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
    gateway: SlurmGateway = Depends(get_slurm_gateway),
) -> dict[str, Any]:
    queue_depth_method = getattr(gateway, "queue_depth", None)
    if callable(queue_depth_method):
        depth = dict(queue_depth_method())
    else:
        try:
            records = gateway.list_jobs(limit=1000, offset=0)
        except SlurmGatewayError as error:
            raise ApiError(
                status_code=error.status_code,
                code=error.code,
                message=error.message,
                details=error.details,
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
        message=error.message,
        details=error.details,
    )


def _retry_execution_status(status: str) -> str:
    if status == "pending":
        return "queued"
    if status == "submitted":
        return "submitted"
    return status


def _is_idempotent_slurm_cancel_error(error: SlurmGatewayError) -> bool:
    code = error.code.lower()
    message = error.message.lower()
    return error.status_code == 404 or "not found" in code or "not found" in message or "invalid job" in message


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
    return {
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
        "status": "ok",
        "data": data,
    }


def _require_operator_role(request: Request) -> None:
    role = request.headers.get("X-User-Role")
    if role is not None and role.strip().lower() in _OPERATOR_ROLES:
        return
    raise ApiError(
        status_code=403,
        code="FORBIDDEN",
        message="Operator role required.",
    )


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

    state_column = "current_state" if "current_state" in column_names else "status"
    source_column = "source_id" if "source_id" in column_names else "source"
    started_expression = "started_at" if "started_at" in column_names else "created_at"
    updated_expression = "updated_at" if "updated_at" in column_names else started_expression

    filters: list[str] = []
    if "cycle_id" in column_names:
        filters.append("cycle_id = :cycle_id")
    if source_column in column_names and "cycle_time" in column_names:
        filters.append(f"({source_column} = :source AND cycle_time = :cycle_time)")
    if not filters or state_column not in column_names:
        raise ApiError(
            status_code=500,
            code="FORECAST_CYCLE_SCHEMA_UNSUPPORTED",
            message="met.forecast_cycle does not expose the required monitoring columns.",
        )

    source_select = f"{source_column} AS source" if source_column in column_names else "NULL AS source"
    cycle_time_select = "cycle_time" if "cycle_time" in column_names else "NULL AS cycle_time"
    cycle_id_select = "cycle_id" if "cycle_id" in column_names else "NULL AS cycle_id"
    statement = text(
        f"""
        SELECT
            {cycle_id_select},
            {source_select},
            {cycle_time_select},
            {state_column} AS current_state,
            {started_expression} AS started_at,
            {updated_expression} AS updated_at
        FROM met.forecast_cycle
        WHERE {" OR ".join(filters)}
        LIMIT 1
        """
    )
    try:
        row = (
            store.session.execute(
                statement,
                {"source": source, "cycle_time": cycle_time, "cycle_id": cycle_id},
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


def _stage_summaries(jobs: list[PipelineJob]) -> list[dict[str, Any]]:
    jobs_by_stage: dict[str, list[PipelineJob]] = defaultdict(list)
    for job in jobs:
        if job.stage:
            jobs_by_stage[job.stage].append(job)

    stages = list(_STAGE_ORDER)
    for stage in jobs_by_stage:
        if stage not in stages:
            stages.append(stage)

    summaries: list[dict[str, Any]] = []
    previous_failed = False
    for stage in stages:
        stage_jobs = jobs_by_stage.get(stage, [])
        display_status = _stage_display_status(stage_jobs)
        if not stage_jobs and previous_failed:
            display_status = "skipped"
        if display_status == "failed":
            previous_failed = True

        summaries.append(
            {
                "stage": stage,
                "display_status": display_status,
                "status": display_status,
                "duration_seconds": _stage_duration_seconds(stage_jobs),
                "basin_progress": _basin_progress(stage_jobs),
                "basin_results": [_basin_result(job) for job in stage_jobs],
            }
        )
    return summaries


def _stage_display_status(jobs: list[PipelineJob]) -> str:
    if not jobs:
        return "pending"

    statuses = {job.status for job in jobs}
    if statuses & {"submitted", "running"}:
        return "running"
    if statuses == {"pending"}:
        return "pending"
    if "partially_failed" in statuses:
        return "partially_failed"
    if statuses & _FAILED_JOB_STATUSES:
        return "partially_failed" if "succeeded" in statuses else "failed"
    if statuses == {"succeeded"}:
        return "succeeded"
    return "running" if statuses & _ACTIVE_JOB_STATUSES else "failed"


def _stage_duration_seconds(jobs: list[PipelineJob]) -> int | None:
    starts = [job.started_at for job in jobs if job.started_at is not None]
    finishes = [job.finished_at for job in jobs if job.finished_at is not None]
    if not starts or not finishes:
        return None
    return _duration_seconds(min(starts), max(finishes))


def _basin_progress(jobs: list[PipelineJob]) -> dict[str, int]:
    failed = sum(1 for job in jobs if job.status in _FAILED_JOB_STATUSES or job.status == "partially_failed")
    completed = sum(1 for job in jobs if job.status == "succeeded")
    return {"completed": completed, "total": len(jobs), "failed": failed}


def _basin_result(job: PipelineJob) -> dict[str, Any]:
    return {
        "model_id": job.model_id,
        "basin_id": None,
        "status": job.status,
        "error_code": job.error_code,
        "error_message": job.error_message,
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
        "error_message": job.error_message,
        "log_uri": job.log_uri,
        "duration_seconds": _duration_seconds(job.started_at, job.finished_at),
    }


def _duration_seconds(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    if started_at is None or finished_at is None:
        return None
    return max(0, int((finished_at - started_at).total_seconds()))


def _log_root() -> Path:
    configured_root = os.getenv("LOG_ROOT", "").strip()
    return Path(configured_root or LOG_ROOT).expanduser().resolve()


def _local_log_path(log_uri: str) -> Path | None:
    if log_uri.startswith("file://"):
        raw_path = Path(log_uri.removeprefix("file://")).expanduser()
    elif "://" in log_uri:
        return None
    else:
        raw_path = Path(log_uri).expanduser()

    log_root = _log_root()
    if raw_path.is_symlink():
        _raise_log_path_forbidden(log_uri, log_root)

    candidates = [raw_path.resolve()]
    if not raw_path.is_absolute():
        rooted_raw_path = log_root / raw_path
        if rooted_raw_path.is_symlink():
            _raise_log_path_forbidden(log_uri, log_root)
        rooted_path = rooted_raw_path.resolve()
        if rooted_path not in candidates:
            candidates.append(rooted_path)

    for candidate in candidates:
        try:
            candidate.relative_to(log_root)
        except ValueError:
            continue
        return candidate

    _raise_log_path_forbidden(log_uri, log_root)


def _raise_log_path_forbidden(log_uri: str, log_root: Path) -> None:
    raise ApiError(
        status_code=403,
        code="FORBIDDEN",
        message="Job log path is outside the configured log root.",
        details={"log_uri": log_uri, "log_root": str(log_root)},
    )


def _read_log_tail(log_path: Path) -> str:
    size = log_path.stat().st_size
    with log_path.open("rb") as file:
        if size > _MAX_LOG_BYTES:
            file.seek(size - _MAX_LOG_BYTES)
        return file.read(_MAX_LOG_BYTES).decode("utf-8", errors="replace")
