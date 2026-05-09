from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.errors import ApiError
from services.orchestrator.persistence import PipelineJob, PipelineStore
from services.orchestrator.retry import RetryConfig, RetryConflictError, RetryNotFoundError, RetryService
from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.gateway import SlurmGateway, SlurmGatewayError
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

router = APIRouter(prefix="/api/v1", tags=["pipeline"])
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_OPERATOR_ROLES = {"operator", "model_admin", "sys_admin"}
_ACTIVE_JOB_STATUSES = {"pending", "submitted", "running"}
_FAILED_JOB_STATUSES = {"failed", "submission_failed", "permanently_failed", "cancelled"}
_STAGE_ORDER = ("download", "convert", "forcing", "forecast", "parse", "frequency", "publish")
_MAX_JOBS_LIMIT = 200


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
    jobs = store.query_jobs_by_cycle(cycle_id_for(source, parsed_cycle_time))
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
            return _ok(request, [])
        statement = statement.where(PipelineJob.run_id.in_(run_ids))
    else:
        if run_type is not None:
            statement = statement.where(PipelineJob.run_id.like(f"%{run_type}%"))
        if scenario is not None:
            statement = statement.where(PipelineJob.run_id.like(f"%{scenario}%"))

    statement = (
        statement.order_by(PipelineJob.submitted_at.desc(), PipelineJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    jobs = list(store.session.scalars(statement))
    return _ok(request, [_job_payload(job) for job in jobs])


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
            "content": log_path.read_text(encoding="utf-8", errors="replace"),
        },
    )


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: str,
    request: Request,
    service: RetryService = Depends(get_retry_service),
) -> dict[str, Any]:
    _require_operator_role(request)
    if not _SAFE_RUN_ID_RE.fullmatch(run_id):
        raise ApiError(
            status_code=400,
            code="INVALID_RUN_ID",
            message="Invalid run identifier.",
        )

    try:
        job = service.attempt_manual_retry(run_id)
    except RetryConflictError as error:
        raise _api_error(error) from error
    except RetryNotFoundError as error:
        raise _api_error(error) from error

    return _ok(
        request,
        {
            "job_id": job.job_id,
            "run_id": job.run_id,
            "retry_count": job.retry_count,
            "status": job.status,
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
    now = datetime.now(UTC)
    for job in active_jobs:
        previous_status = job.status
        if job.slurm_job_id:
            try:
                gateway.cancel_job(job.slurm_job_id)
            except SlurmGatewayError as error:
                raise ApiError(
                    status_code=error.status_code,
                    code=error.code,
                    message=error.message,
                    details=error.details,
                ) from error

        updated = store.update_job_status(job.job_id, "cancelled", finished_at=now)
        store.insert_event(
            entity_type="pipeline_job",
            entity_id=job.job_id,
            event_type="cancel",
            status_from=previous_status,
            status_to="cancelled",
            message=f"Cancelled run {run_id}.",
            details={"run_id": run_id, "slurm_job_id": job.slurm_job_id},
        )
        cancelled_jobs.append(_job_payload(updated))

    return _ok(
        request,
        {
            "run_id": run_id,
            "cancelled_jobs": cancelled_jobs,
            "cancelled": cancelled_jobs,
        },
    )


@router.get("/metrics/stage-duration")
def stage_duration_metrics(
    request: Request,
    days: int = Query(default=7, ge=1),
    store: PipelineStore = Depends(get_pipeline_store),
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    statement = select(PipelineJob).where(
        PipelineJob.started_at.is_not(None),
        PipelineJob.finished_at.is_not(None),
        PipelineJob.finished_at >= cutoff,
    )
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
    days: int = Query(default=7, ge=1),
    store: PipelineStore = Depends(get_pipeline_store),
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    statement = select(PipelineJob).where(PipelineJob.created_at >= cutoff)
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


def _api_error(error: RetryConflictError | RetryNotFoundError) -> ApiError:
    return ApiError(
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        details=error.details,
    )


def _ok(request: Request, data: Any) -> dict[str, Any]:
    return {
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
        "status": "ok",
        "data": data,
    }


def _require_operator_role(request: Request) -> None:
    role = request.headers.get("X-User-Role")
    if role is None:
        return
    normalized_role = role.strip().lower()
    if normalized_role in _OPERATOR_ROLES:
        return
    raise ApiError(
        status_code=403,
        code="FORBIDDEN",
        message="User role is not allowed to perform this operation.",
        details={"role": role, "required_roles": sorted(_OPERATOR_ROLES)},
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
        row = store.session.execute(
            statement,
            {"source": source, "cycle_time": cycle_time, "cycle_id": cycle_id},
        ).mappings().first()
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
    if "running" in statuses:
        return "running"
    if statuses <= {"pending", "submitted"}:
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


def _job_payload(job: PipelineJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "cycle_id": job.cycle_id,
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


def _local_log_path(log_uri: str) -> Path | None:
    if log_uri.startswith("file://"):
        return Path(log_uri.removeprefix("file://")).expanduser()
    if "://" in log_uri:
        return None
    return Path(log_uri).expanduser()
