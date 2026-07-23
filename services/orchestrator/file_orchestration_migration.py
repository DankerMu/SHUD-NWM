from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow, ensure_directory_no_follow
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
    _public_evidence,
)
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
MAX_HISTORICAL_MIGRATION_ROWS_PER_RELATION = 100_000
HISTORICAL_MIGRATION_ROW_LIMITS = {
    "forecast_cycles": MAX_HISTORICAL_MIGRATION_ROWS_PER_RELATION,
    "hydro_runs": MAX_HISTORICAL_MIGRATION_ROWS_PER_RELATION,
    "pipeline_jobs": MAX_HISTORICAL_MIGRATION_ROWS_PER_RELATION,
    "pipeline_events": MAX_HISTORICAL_MIGRATION_ROWS_PER_RELATION,
}
EXPORT_FETCHMANY_BATCH_SIZE = 1_000

_FAILED_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed", "cancelled"}


def prepare_file_journal_rollback(
    *,
    journal_root: str | Path,
    workspace_root: str | Path,
    lock_path: str | Path | None = None,
    scheduler_lock_backend: str = "file",
    lock_ttl_seconds: int = 60,
    scheduler_state: str,
    active_scheduler_processes: int,
    checked_at: datetime,
    checked_by: str,
    target_writer_generation: str,
) -> dict[str, Any]:
    """Produce the durable receipt required before launching an old writer."""

    config, lease_identity = _rollback_file_lease_config(
        journal_root=journal_root,
        workspace_root=workspace_root,
        lock_path=lock_path,
        scheduler_lock_backend=scheduler_lock_backend,
        lock_ttl_seconds=lock_ttl_seconds,
    )
    lease, heartbeat, pass_id = _acquire_rollback_file_lease(config, operation="prepare")
    repository = FileOrchestrationJournalRepository(journal_root)
    try:
        return repository._prepare_reconcile_inventory_rollback_under_scheduler_lease(
            scheduler_lease_identity=lease_identity,
            scheduler_lease_guard=lambda: _rollback_lease_is_held(
                lease,
                heartbeat,
                pass_id=pass_id,
            ),
            scheduler_state=scheduler_state,
            active_scheduler_processes=active_scheduler_processes,
            checked_at=checked_at,
            checked_by=checked_by,
            target_writer_generation=target_writer_generation,
        )
    finally:
        try:
            heartbeat.stop()
        finally:
            lease.release(pass_id=pass_id)


def require_file_journal_rollback_prepared(
    *,
    journal_root: str | Path,
    workspace_root: str | Path,
    receipt_id: str,
    lock_path: str | Path | None = None,
    scheduler_lock_backend: str = "file",
    lock_ttl_seconds: int = 60,
) -> dict[str, Any]:
    """Old-writer launch gate for the supported rollback path."""

    _config, lease_identity = _rollback_file_lease_config(
        journal_root=journal_root,
        workspace_root=workspace_root,
        lock_path=lock_path,
        scheduler_lock_backend=scheduler_lock_backend,
        lock_ttl_seconds=lock_ttl_seconds,
    )
    repository = FileOrchestrationJournalRepository(journal_root)
    return repository._require_reconcile_inventory_rollback_prepared(
        receipt_id=receipt_id,
        scheduler_lease_identity=lease_identity,
    )


def complete_file_journal_rollforward(
    *,
    journal_root: str | Path,
    workspace_root: str | Path,
    preparation_receipt_id: str,
    lock_path: str | Path | None = None,
    scheduler_lock_backend: str = "file",
    lock_ttl_seconds: int = 60,
) -> dict[str, Any]:
    """Rebuild inventory and consume the rollback fence under the scheduler lease."""

    config, lease_identity = _rollback_file_lease_config(
        journal_root=journal_root,
        workspace_root=workspace_root,
        lock_path=lock_path,
        scheduler_lock_backend=scheduler_lock_backend,
        lock_ttl_seconds=lock_ttl_seconds,
    )
    lease, heartbeat, pass_id = _acquire_rollback_file_lease(config, operation="rollforward")
    repository = FileOrchestrationJournalRepository(journal_root)
    try:
        return repository._complete_reconcile_inventory_rollforward_under_scheduler_lease(
            preparation_receipt_id=preparation_receipt_id,
            scheduler_lease_identity=lease_identity,
            scheduler_lease_guard=lambda: _rollback_lease_is_held(
                lease,
                heartbeat,
                pass_id=pass_id,
            ),
        )
    finally:
        try:
            heartbeat.stop()
        finally:
            lease.release(pass_id=pass_id)


def _rollback_file_lease_config(
    *,
    journal_root: str | Path,
    workspace_root: str | Path,
    lock_path: str | Path | None,
    scheduler_lock_backend: str,
    lock_ttl_seconds: int,
) -> tuple[Any, dict[str, str]]:
    from services.orchestrator.scheduler import ProductionSchedulerConfig

    config = ProductionSchedulerConfig(
        workspace_root=workspace_root,
        lock_path=lock_path,
        scheduler_db_free_required=True,
        scheduler_lock_backend=scheduler_lock_backend,
        scheduler_journal_root=journal_root,
        lock_ttl_seconds=lock_ttl_seconds,
    )
    if config.scheduler_lock_backend != "file":
        raise FileOrchestrationJournalError(
            "file_journal_rollback_requires_file_scheduler_lease",
            field="scheduler_lock_backend",
        )
    workspace = Path(config.workspace_root)
    scheduler_lock = Path(config.lock_path)
    identity = {
        "backend": "file",
        "lock_path_digest": hashlib.sha256(str(scheduler_lock).encode("utf-8")).hexdigest(),
        "workspace_root_digest": hashlib.sha256(str(workspace).encode("utf-8")).hexdigest(),
    }
    return config, identity


def _acquire_rollback_file_lease(config: Any, *, operation: str) -> tuple[Any, Any, str]:
    from services.orchestrator.scheduler_lease import FileSchedulerLease, _LeaseHeartbeat

    pass_id = f"file-journal-{operation}-{uuid4().hex}"
    lease = FileSchedulerLease(
        Path(config.lock_path),
        ttl_seconds=config.lock_ttl_seconds,
        workspace_root=Path(config.workspace_root),
    )
    acquired = lease.acquire(pass_id=pass_id, started_at=datetime.now(UTC))
    if not acquired.get("acquired"):
        raise FileOrchestrationJournalError(
            "file_journal_scheduler_lease_contended",
            field="scheduler_lock",
        )
    heartbeat = _LeaseHeartbeat(
        lease,
        pass_id,
        max(1, config.lock_ttl_seconds // 3),
    )
    heartbeat.start()
    if not _rollback_lease_is_held(lease, heartbeat, pass_id=pass_id):
        heartbeat.stop()
        lease.release(pass_id=pass_id)
        raise FileOrchestrationJournalError(
            "file_journal_scheduler_lease_lost",
            field="scheduler_lock",
        )
    return lease, heartbeat, pass_id


def _rollback_lease_is_held(lease: Any, heartbeat: Any, *, pass_id: str) -> bool:
    return not heartbeat.lost and bool(lease.renew(pass_id=pass_id))


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
    cycles = _normalized_rows_limited("forecast_cycles", forecast_cycles)
    runs = _normalized_rows_limited("hydro_runs", hydro_runs)
    jobs = _normalized_rows_limited("pipeline_jobs", pipeline_jobs)
    events = _normalized_rows_limited("pipeline_events", pipeline_events)
    repository = FileOrchestrationJournalRepository(journal_root)
    imported_cycles: list[dict[str, Any]] = []
    imported_runs: list[dict[str, Any]] = []
    imported_jobs: list[dict[str, Any]] = []
    imported_events: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, str]] = []

    for row in cycles:
        skip_reason = _unsupported_forecast_cycle_reason(row)
        if skip_reason is not None:
            skipped_rows.append(_skipped_row("forecast_cycles", row, skip_reason))
            continue
        repository.append_historical_forecast_cycle(row)
        imported_cycles.append(row)

    for row in runs:
        skip_reason = _unsupported_run_reason(row)
        if skip_reason is not None:
            skipped_rows.append(_skipped_row("hydro_runs", row, skip_reason))
            continue
        repository.append_historical_hydro_run(row)
        imported_runs.append(row)

    for row in jobs:
        skip_reason = _unsupported_job_reason(row)
        if skip_reason is not None:
            skipped_rows.append(_skipped_row("pipeline_jobs", row, skip_reason))
            continue
        repository.append_historical_pipeline_job(row)
        imported_jobs.append(row)

    imported_job_ids = {
        str(row.get("job_id"))
        for row in imported_jobs
        if row.get("job_id") not in (None, "")
    }
    imported_cycle_ids = {
        str(row.get("cycle_id"))
        for row in imported_cycles
        if row.get("cycle_id") not in (None, "")
    }
    for row in events:
        skip_reason = _unsupported_event_reason(
            row,
            imported_job_ids=imported_job_ids,
            imported_cycle_ids=imported_cycle_ids,
        )
        if skip_reason is not None:
            skipped_rows.append(_skipped_row("pipeline_events", row, skip_reason))
            continue
        written = repository.append_historical_pipeline_event(row)
        if written is None:
            skipped_rows.append(_skipped_row("pipeline_events", row, "unsupported_pipeline_event_target"))
            continue
        imported_events.append(row)

    replay_status = _migration_replay_status(repository, imported_jobs)
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
        "imported_row_counts": {
            "forecast_cycles": len(imported_cycles),
            "hydro_runs": len(imported_runs),
            "pipeline_jobs": len(imported_jobs),
            "pipeline_events": len(imported_events),
        },
        "skipped_rows": _skipped_rows_summary(skipped_rows),
        "checksums": {
            "forecast_cycles": _rows_checksum(cycles),
            "hydro_runs": _rows_checksum(runs),
            "pipeline_jobs": _rows_checksum(jobs),
            "pipeline_events": _rows_checksum(events),
        },
        "replay_status": replay_status,
        "stale_download_source_cycle_supersession": _download_source_cycle_supersession(imported_jobs, imported_events),
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
            relation="forecast_cycles",
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
            relation="hydro_runs",
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
            relation="pipeline_jobs",
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
            relation="pipeline_events",
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


def _fetch_rows(connection: Any, sql: str, params: Mapping[str, Any], *, relation: str) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        fetchmany = getattr(cursor, "fetchmany", None)
        if callable(fetchmany):
            return _fetch_rows_limited(cursor, relation=relation)
        rows = [dict(row) for row in cursor.fetchall()]
        _validate_relation_row_count(relation, len(rows))
        return rows


def _fetch_rows_limited(cursor: Any, *, relation: str) -> list[dict[str, Any]]:
    limit = _relation_row_limit(relation)
    rows: list[dict[str, Any]] = []
    while True:
        remaining_probe_rows = limit + 1 - len(rows)
        if remaining_probe_rows <= 0:
            _raise_relation_row_limit(relation, limit)
        batch_size = min(EXPORT_FETCHMANY_BATCH_SIZE, remaining_probe_rows)
        batch = cursor.fetchmany(batch_size)
        if not batch:
            return rows
        rows.extend(dict(row) for row in batch)
        if len(rows) > limit:
            _raise_relation_row_limit(relation, limit)


def _unsupported_forecast_cycle_reason(row: Mapping[str, Any]) -> str | None:
    if row.get("cycle_id") in (None, ""):
        return None
    try:
        _source_cycle_from_cycle_id(str(row["cycle_id"]))
    except (FileOrchestrationJournalError, ValueError):
        return "unsupported_forecast_cycle_identity"
    return None


def _unsupported_run_reason(row: Mapping[str, Any]) -> str | None:
    run_id = row.get("run_id")
    if run_id in (None, ""):
        return None
    return None if _run_id_is_file_journal_supported(str(run_id)) else "unsupported_run_identity"


def _unsupported_job_reason(row: Mapping[str, Any]) -> str | None:
    run_id = row.get("run_id")
    if run_id in (None, ""):
        return None
    return None if _run_id_is_file_journal_supported(str(run_id)) else "unsupported_run_identity"


def _unsupported_event_reason(
    row: Mapping[str, Any],
    *,
    imported_job_ids: set[str],
    imported_cycle_ids: set[str],
) -> str | None:
    entity_type = str(row.get("entity_type") or "pipeline_job")
    entity_id = row.get("entity_id")
    if entity_type == "pipeline_job":
        if entity_id in (None, ""):
            return None
        return None if str(entity_id) in imported_job_ids else "unsupported_pipeline_event_target"
    if entity_type == "forecast_cycle":
        if entity_id in (None, ""):
            return None
        if str(entity_id) in imported_cycle_ids:
            return None
        try:
            _source_cycle_from_cycle_id(str(entity_id))
        except (FileOrchestrationJournalError, ValueError):
            return "unsupported_forecast_cycle_event_identity"
        return None
    return "unsupported_pipeline_event_entity_type"


def _run_id_is_file_journal_supported(run_id: str) -> bool:
    return run_id.startswith("fcst_") or run_id.startswith("cycle_")


def _source_cycle_from_cycle_id(cycle_id: str) -> tuple[str, datetime]:
    source, separator, cycle_stamp = cycle_id.rpartition("_")
    if not separator:
        raise ValueError(f"Cannot infer source/cycle from cycle_id: {cycle_id}")
    return source, parse_cycle_time(cycle_stamp)


def _skipped_row(relation: str, row: Mapping[str, Any], reason: str) -> dict[str, str]:
    return {
        "relation": relation,
        "reason": reason,
        "identity": _migration_receipt_text(_skipped_row_identity(row)),
    }


def _skipped_row_identity(row: Mapping[str, Any]) -> str:
    for field in ("event_id", "job_id", "run_id", "cycle_id", "entity_id"):
        value = row.get(field)
        if value not in (None, ""):
            return f"{field}:{str(value)[:160]}"
    return "unknown"


def _skipped_rows_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    by_relation: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for row in rows:
        by_relation[row["relation"]] = by_relation.get(row["relation"], 0) + 1
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
    return {
        "count": len(rows),
        "by_relation": by_relation,
        "by_reason": by_reason,
        "samples": [_migration_receipt_sample(row) for row in rows[:8]],
    }


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


def _normalized_rows_limited(relation: str, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    limit = _relation_row_limit(relation)
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if len(normalized) >= limit:
            _raise_relation_row_limit(relation, limit)
        normalized.append(_normalized_mapping(row))
    return normalized


def _relation_row_limit(relation: str) -> int:
    try:
        return int(HISTORICAL_MIGRATION_ROW_LIMITS[relation])
    except KeyError as error:
        raise ValueError(f"historical migration row limit is not configured for relation {relation!r}") from error


def _validate_relation_row_count(relation: str, row_count: int) -> None:
    limit = _relation_row_limit(relation)
    if row_count > limit:
        _raise_relation_row_limit(relation, limit)


def _raise_relation_row_limit(relation: str, limit: int) -> None:
    raise ValueError(
        f"historical migration relation {relation!r} exceeds row limit {limit}; "
        "split the migration or raise the per-relation cap intentionally"
    )


def _migration_receipt_sample(value: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = _public_evidence(value)
    return dict(sanitized) if isinstance(sanitized, Mapping) else {}


def _migration_receipt_text(value: str) -> str:
    sanitized = _public_evidence(value)
    return sanitized if isinstance(sanitized, str) else str(sanitized)


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
                _migration_receipt_sample(
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
            )
    return {"count": len(superseded), "samples": superseded[:8]}
