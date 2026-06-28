from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow, ensure_directory_no_follow
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from services.orchestrator.scheduler_state import _format_utc
from workers.data_adapters.base import parse_cycle_time

HISTORICAL_NODE22_DB_PORT = 55433
MIGRATION_RECEIPT_SCHEMA_VERSION = "nhms.scheduler.file_orchestration_migration.v1"
HISTORICAL_NODE22_DB_HOSTS = {
    "127.0.0.1",
    "::1",
    "localhost",
    "node22",
    "node-22",
    "10.0.2.100",
    "210.77.77.22",
}

_FAILED_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed", "cancelled"}


def import_historical_scheduler_state(
    *,
    journal_root: str | Path,
    forecast_cycles: Iterable[Mapping[str, Any]] = (),
    hydro_runs: Iterable[Mapping[str, Any]] = (),
    pipeline_jobs: Iterable[Mapping[str, Any]] = (),
    pipeline_events: Iterable[Mapping[str, Any]] = (),
    cutoff_time: datetime | None = None,
    source: str = "node22:55433",
) -> dict[str, Any]:
    cutoff = _ensure_utc(cutoff_time or datetime.now(UTC))
    cycles = [_normalized_mapping(row) for row in forecast_cycles]
    runs = [_normalized_mapping(row) for row in hydro_runs]
    jobs = [_normalized_mapping(row) for row in pipeline_jobs]
    events = [_normalized_mapping(row) for row in pipeline_events]
    repository = FileOrchestrationJournalRepository(journal_root)

    for row in cycles:
        repository.append_historical_forecast_cycle(row)

    for row in runs:
        repository.append_historical_hydro_run(row)

    for row in jobs:
        repository.append_historical_pipeline_job(row)

    for row in events:
        repository.append_historical_pipeline_event(row)

    replay_status = _migration_replay_status(repository, jobs)
    receipt = {
        "schema_version": MIGRATION_RECEIPT_SCHEMA_VERSION,
        "source": source,
        "cutoff_time": _format_utc(cutoff),
        "row_counts": {
            "forecast_cycles": len(cycles),
            "hydro_runs": len(runs),
            "pipeline_jobs": len(jobs),
            "pipeline_events": len(events),
        },
        "checksums": {
            "forecast_cycles": _rows_checksum(cycles),
            "hydro_runs": _rows_checksum(runs),
            "pipeline_jobs": _rows_checksum(jobs),
            "pipeline_events": _rows_checksum(events),
        },
        "replay_status": replay_status,
        "stale_download_source_cycle_supersession": _download_source_cycle_supersession(jobs, events),
    }
    return receipt


def export_scheduler_state_from_postgres(
    *,
    database_url: str,
    journal_root: str | Path,
    allow_historical_node22: bool = False,
    cutoff_time: datetime | None = None,
) -> dict[str, Any]:
    _validate_historical_node22_database_url(database_url, allow_historical_node22=allow_historical_node22)
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise RuntimeError("psycopg is required to export historical scheduler state") from error

    cutoff = _ensure_utc(cutoff_time or datetime.now(UTC))
    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10) as connection:
        forecast_cycles = _fetch_rows(
            connection,
            """
            SELECT cycle_id, source_id, cycle_time, issue_time, status, manifest_uri,
                   retry_count, error_code, error_message, created_at
            FROM met.forecast_cycle
            WHERE created_at <= %(cutoff)s OR cycle_time <= %(cutoff)s
            ORDER BY cycle_time ASC, cycle_id ASC
            """,
            {"cutoff": cutoff},
        )
        hydro_runs = _fetch_rows(
            connection,
            """
            SELECT run_id, run_type, scenario_id, model_id, basin_version_id,
                   forcing_version_id, init_state_id, source_id, cycle_time,
                   start_time, end_time, status, run_manifest_uri, output_uri,
                   log_uri, slurm_job_id, error_code, error_message, created_at, updated_at
            FROM hydro.hydro_run
            WHERE created_at <= %(cutoff)s OR cycle_time <= %(cutoff)s
            ORDER BY cycle_time ASC, run_id ASC
            """,
            {"cutoff": cutoff},
        )
        pipeline_jobs = _fetch_rows(
            connection,
            """
            SELECT job_id, run_id, cycle_id, job_type, slurm_job_id, array_task_id,
                   model_id, status, stage, idempotency_key, candidate_id,
                   submitted_at, started_at, finished_at, exit_code, retry_count,
                   manual_retry_marker, error_code, error_message, log_uri,
                   created_at, updated_at
            FROM ops.pipeline_job
            WHERE created_at <= %(cutoff)s
               OR updated_at <= %(cutoff)s
               OR finished_at <= %(cutoff)s
            ORDER BY created_at ASC NULLS FIRST, job_id ASC
            """,
            {"cutoff": cutoff},
        )
        pipeline_events = _fetch_rows(
            connection,
            """
            SELECT event_id, entity_type, entity_id, event_type, status_from,
                   status_to, message, details, created_at
            FROM ops.pipeline_event
            WHERE created_at <= %(cutoff)s
            ORDER BY created_at ASC NULLS FIRST, event_id ASC
            """,
            {"cutoff": cutoff},
        )
    return import_historical_scheduler_state(
        journal_root=journal_root,
        forecast_cycles=forecast_cycles,
        hydro_runs=hydro_runs,
        pipeline_jobs=pipeline_jobs,
        pipeline_events=pipeline_events,
        cutoff_time=cutoff,
        source=_historical_source_label(database_url),
    )


def write_migration_receipt(
    receipt: Mapping[str, Any],
    receipt_path: str | Path,
    *,
    containment_root: str | Path | None = None,
) -> None:
    root = Path(containment_root) if containment_root is not None else None
    path = Path(receipt_path)
    if root is not None and not path.is_absolute():
        path = root / path
    content = (json.dumps(receipt, sort_keys=True, indent=2, default=_json_default) + "\n").encode("utf-8")
    try:
        if root is not None:
            ensure_directory_no_follow(root)
        atomic_write_bytes_no_follow(path, content, containment_root=root)
    except (OSError, SafeFilesystemError) as error:
        raise ValueError(f"failed to write migration receipt safely: {error}") from error


def _fetch_rows(connection: Any, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def _validate_historical_node22_database_url(database_url: str, *, allow_historical_node22: bool) -> None:
    if not allow_historical_node22:
        raise ValueError("historical node-22 export requires --allow-historical-node22")
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError("historical scheduler-state export requires a PostgreSQL URL")
    if parsed.query or parsed.fragment:
        raise ValueError("historical scheduler-state export does not allow libpq URL query parameters")
    if parsed.port != HISTORICAL_NODE22_DB_PORT:
        raise ValueError(f"historical scheduler-state export must target port {HISTORICAL_NODE22_DB_PORT}")
    if (parsed.hostname or "") not in HISTORICAL_NODE22_DB_HOSTS:
        raise ValueError("historical scheduler-state export must target the node-22 historical PostgreSQL host")


def _historical_source_label(database_url: str) -> str:
    parsed = urlparse(database_url)
    return f"{parsed.hostname or 'unknown'}:{parsed.port or HISTORICAL_NODE22_DB_PORT}"


def _normalized_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _normalize_json_value(value) for key, value in row.items() if value is not None}


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_utc(_ensure_utc(value))
    if isinstance(value, Mapping):
        return {str(key): _normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize_json_value(item) for item in value]
    return value


def _source_cycle_from_row(row: Mapping[str, Any]) -> tuple[str, datetime]:
    source_id = row.get("source_id")
    cycle_time = row.get("cycle_time")
    if source_id not in (None, "") and cycle_time not in (None, ""):
        return str(source_id), _coerce_datetime(cycle_time)
    cycle_id = str(row["cycle_id"])
    source, separator, cycle_stamp = cycle_id.rpartition("_")
    if not separator:
        raise ValueError(f"Cannot infer source/cycle from cycle_id: {cycle_id}")
    return source, parse_cycle_time(cycle_stamp)


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    return parse_cycle_time(str(value))


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _rows_checksum(rows: list[Mapping[str, Any]]) -> str:
    content = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _format_utc(_ensure_utc(value))
    return str(value)


def _migration_replay_status(
    repository: FileOrchestrationJournalRepository,
    jobs: list[Mapping[str, Any]],
) -> dict[str, Any]:
    seen: set[tuple[str, str, str, str]] = set()
    blocked: list[dict[str, str]] = []
    checked = 0
    for job in jobs:
        if job.get("model_id") in (None, "") or job.get("run_id") in (None, "") or job.get("cycle_id") in (None, ""):
            continue
        source_id, cycle_time = _source_cycle_from_row(job)
        key = (source_id, _format_utc(cycle_time), str(job["model_id"]), str(job["run_id"]))
        if key in seen:
            continue
        seen.add(key)
        checked += 1
        state = repository.candidate_state(
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=str(job["model_id"]),
            run_id=str(job["run_id"]),
            forcing_version_id=f"forc_{source_id}_{cycle_time:%Y%m%d%H}_{job['model_id']}",
            candidate_id=f"migration:{source_id}:{cycle_time:%Y%m%d%H}:{job['model_id']}",
        )
        if isinstance(state, Mapping) and isinstance(state.get("file_journal"), Mapping):
            blocked.append(
                {
                    "run_id": str(job["run_id"]),
                    "reason": str(state["file_journal"].get("reason") or "file_journal_blocked"),
                }
            )
    return {
        "status": "ok" if not blocked else "blocked",
        "checked_candidate_states": checked,
        "blocked_count": len(blocked),
        "blocked_samples": blocked[:8],
    }


def _download_source_cycle_supersession(
    jobs: list[Mapping[str, Any]],
    events: list[Mapping[str, Any]],
) -> dict[str, Any]:
    job_by_id = {str(job.get("job_id")): job for job in jobs if job.get("job_id") not in (None, "")}
    superseded: list[dict[str, Any]] = []
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
        previous_job_id = details.get("previous_job_id")
        if previous_job_id in (None, ""):
            continue
        previous = job_by_id.get(str(previous_job_id))
        if not previous or previous.get("job_type") != "download_source_cycle":
            continue
        if str(previous.get("status") or "") not in _FAILED_STATUSES:
            continue
        if details.get("manual_retry_marker") is True or details.get("trigger") == "manual":
            superseded.append(
                {
                    "failed_job_id": str(previous_job_id),
                    "superseding_event_id": str(event.get("event_id") or ""),
                    "superseding_entity_id": str(event.get("entity_id") or ""),
                    "cycle_id": str(previous.get("cycle_id") or ""),
                    "prior_failure_reason": str(
                        details.get("prior_failure_reason")
                        or details.get("previous_error")
                        or previous.get("error_code")
                        or ""
                    ),
                }
            )
    return {"count": len(superseded), "samples": superseded[:8]}
