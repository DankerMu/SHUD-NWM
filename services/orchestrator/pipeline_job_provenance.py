"""Source-owned pipeline job provenance publication and node-27 projection.

Publisher (node-22) reads the DB-free journal without write locks or mkdir,
cross-checks the selected run's scientific manifest, and writes a closed
sidecar at ``runs/<run_id>/input/pipeline_jobs.json`` plus any verified
canonical published logs. Importer (node-27) projects that sidecar into
``ops.pipeline_job`` in one transaction per run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packages.common.copyback_guard import CopybackLockError, copyback_batch_lock
from packages.common.object_store import (
    MAX_OBJECT_MANIFEST_BYTES,
    LocalObjectStore,
    ObjectStoreError,
)
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
    verify_directory_no_follow,
)
from packages.common.source_identity import normalize_source_id
from services.artifacts import published_log_relative_path, published_log_uri
from services.orchestrator.file_orchestration_journal import (
    FILE_JOURNAL_READ_BLOCKED_STATUS,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.journal_root_authority import verify_journal_root_authority
from services.orchestrator.run_identity import FORECAST_RUN_ID_RE
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

LOGGER = logging.getLogger(__name__)

PROVENANCE_SCHEMA_VERSION = "nhms.published.pipeline_jobs.v1"
SIDECAR_OBJECT_KEY_TEMPLATE = "runs/{run_id}/input/pipeline_jobs.json"
MAX_SIDECAR_BYTES = MAX_OBJECT_MANIFEST_BYTES
MAX_SIDECAR_JOBS = 256
MAX_PROVENANCE_RUNS_PER_BATCH = MAX_SIDECAR_JOBS
JOB_PROJECTION_LOCK_NAMESPACE = "ops.pipeline_job"

MAX_IDENTITY_CHARS = 128
MAX_STATUS_CHARS = 64
MAX_LOG_BYTES = 16 * 1024 * 1024
REDACTION_SENTINELS = frozenset({"[object-uri]", "[uri]", "[local-path]", "[redacted]"})
_SAFE_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

JOB_IDENTITY_FIELDS = ("job_id", "run_id", "cycle_id", "job_type", "model_id", "stage")
JOB_ALLOWLIST_FIELDS = (
    "job_id",
    "run_id",
    "cycle_id",
    "job_type",
    "slurm_job_id",
    "array_task_id",
    "model_id",
    "status",
    "stage",
    "submitted_at",
    "started_at",
    "finished_at",
    "exit_code",
    "retry_count",
    "error_code",
    "error_message",
    "log_uri",
    "log_truncated",
    "log_stdout_bytes",
    "log_stderr_bytes",
    "created_at",
    "updated_at",
)
ALLOWED_JOB_STATUSES = frozenset(
    {
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
        "reserved",
        "complete",
        "published",
    }
)
ALLOWED_STAGES = frozenset(
    {
        "download",
        "convert",
        "forcing",
        "forecast",
        "parse",
        "state_save_qc",
        "publish",
    }
)

_CYCLE_SCOPED_STAGES = frozenset({"convert", "forcing", "state_save_qc", "download", "publish"})


class PipelineJobProvenanceError(RuntimeError):
    """Raised when a provenance publication or import cannot complete safely."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class _ParentArrayLogCache:
    def __init__(self, fetch_logs: Callable[[str], Mapping[str, Any]]) -> None:
        self._fetch_logs = fetch_logs
        self._responses: dict[str, Mapping[str, Any]] = {}

    def fetch(self, parent_job_id: str) -> Mapping[str, Any]:
        cached = self._responses.get(parent_job_id)
        if cached is not None:
            return cached
        response = self._fetch_logs(parent_job_id)
        if not isinstance(response, Mapping):
            raise PipelineJobProvenanceError(
                "PARENT_ARRAY_LOGS_INVALID",
                "Gateway parent-array log response is not a mapping.",
                {"parent_job_id": parent_job_id},
            )
        payload = dict(response)
        self._responses[parent_job_id] = payload
        return payload


def sidecar_object_key(run_id: str) -> str:
    return SIDECAR_OBJECT_KEY_TEMPLATE.format(run_id=_require_safe_identity(run_id, field="run_id"))


def publication_summary(
    *,
    run_id: str,
    status: str,
    reason: str | None = None,
    job_count: int = 0,
    advertised_logs: int = 0,
    sidecar_key: str | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "job_count": int(job_count),
        "advertised_logs": int(advertised_logs),
    }
    if reason:
        summary["reason"] = reason
    if sidecar_key:
        summary["sidecar_key"] = sidecar_key
    return summary


def publish_run_pipeline_job_provenance(
    *,
    run_id: str,
    journal_root: str | Path,
    object_store_root: str | Path,
    object_store_prefix: str = "",
    published_artifact_root: str | Path | None = None,
    slurm_client: Any | None = None,
    fetch_logs: Callable[[str], Mapping[str, Any]] | None = None,
    include_cycle_scoped: bool = True,
    log_cache: _ParentArrayLogCache | None = None,
) -> dict[str, Any]:
    """Publish one run's sidecar and any verified canonical logs.

    Journal reads never mkdir, take a journal write lock, or mutate source
    rows. The existing object-store mutex serializes each complete log/sidecar
    publication so a stale concurrent source cannot replace newer evidence.
    """

    safe_run_id = _require_safe_identity(run_id, field="run_id")
    verified_journal_root = verify_journal_root_authority(
        journal_root,
        setting="NHMS_SCHEDULER_JOURNAL_ROOT",
    )
    object_root = _require_existing_directory(object_store_root, field="object_store_root")
    try:
        with copyback_batch_lock(object_root):
            store = LocalObjectStore(root=object_root, object_store_prefix=object_store_prefix)
            manifest = _load_run_manifest(store, safe_run_id)
            identity = _identity_from_manifest(manifest, expected_run_id=safe_run_id)
            repository = FileOrchestrationJournalRepository(verified_journal_root)
            jobs = _export_jobs_for_run(
                repository,
                identity=identity,
                include_cycle_scoped=include_cycle_scoped,
                published_artifact_root=published_artifact_root,
            )
            cache = log_cache or _ParentArrayLogCache(_bound_fetch_logs(slurm_client, fetch_logs))
            sidecar_key = sidecar_object_key(safe_run_id)
            existing_jobs: dict[str, dict[str, Any]] = {}
            if store.exists(sidecar_key):
                existing_sidecar = _load_sidecar(store, sidecar_key)
                _require_sidecar_matches_manifest(existing_sidecar, identity)
                existing_jobs = _validated_jobs_by_id(existing_sidecar["jobs"], identity=identity)
            advertised_logs = 0
            published_jobs: list[dict[str, Any]] = []
            staged_logs: list[dict[str, Any]] = []
            for job in jobs:
                advertised_job, advertised = _maybe_advertise_verified_log(
                    job,
                    identity=identity,
                    log_cache=cache,
                    published_artifact_root=published_artifact_root,
                    existing_job=existing_jobs.get(str(job.get("job_id") or "")),
                    stage_only=True,
                )
                staged_logs.append(
                    {
                        "job_id": str(advertised_job.get("job_id") or ""),
                        "stdout": advertised_job.pop("_pending_stdout_bytes", None),
                        "stderr": advertised_job.pop("_pending_stderr_bytes", None),
                        "out_uri": advertised_job.pop("_pending_out_uri", None),
                        "err_uri": advertised_job.pop("_pending_err_uri", None),
                    }
                )
                if advertised:
                    advertised_logs += 1
                published_jobs.append(advertised_job)
            _require_jobs_mergeable_with_existing(
                published_jobs,
                existing_jobs=existing_jobs,
                identity=identity,
            )
            for staged in staged_logs:
                existing_job = existing_jobs.get(staged["job_id"])
                refuse_divergent_existing = False
                if existing_job is not None:
                    incoming_job = next(
                        (job for job in published_jobs if str(job.get("job_id") or "") == staged["job_id"]),
                        None,
                    )
                    refuse_divergent_existing = incoming_job is not None and _job_source_timestamp(
                        existing_job
                    ) >= _job_source_timestamp(incoming_job)
                existing_out_uri = None if existing_job is None else existing_job.get("log_uri")
                if staged["stdout"] is not None and staged["out_uri"]:
                    _write_published_log(
                        staged["out_uri"],
                        staged["stdout"],
                        published_artifact_root=published_artifact_root,
                        existing_uri=existing_out_uri if refuse_divergent_existing else None,
                    )
                if staged["stderr"] is not None and staged["err_uri"]:
                    _write_published_log(
                        staged["err_uri"],
                        staged["stderr"],
                        published_artifact_root=published_artifact_root,
                        existing_uri=(
                            staged["err_uri"]
                            if refuse_divergent_existing and existing_job and existing_job.get("log_stderr_bytes")
                            else None
                        ),
                    )
            sidecar = _merge_published_sidecar(
                store,
                sidecar_key=sidecar_key,
                identity=identity,
                jobs=published_jobs,
                existing_jobs=existing_jobs,
            )


            store.write_bytes_atomic(sidecar_key, _encode_sidecar(sidecar))
            return publication_summary(
                run_id=safe_run_id,
                status="published",
                job_count=len(published_jobs),
                advertised_logs=advertised_logs,
                sidecar_key=sidecar_key,
            )
    except CopybackLockError as error:
        raise PipelineJobProvenanceError(
            "PUBLICATION_LOCK_FAILED",
            "Pipeline-job provenance publication could not acquire the object-store mutex.",
            {"run_id": safe_run_id},
        ) from error


def publish_runs_pipeline_job_provenance(
    *,
    run_ids: Sequence[str],
    journal_root: str | Path,
    object_store_root: str | Path,
    object_store_prefix: str = "",
    published_artifact_root: str | Path | None = None,
    slurm_client: Any | None = None,
    fetch_logs: Callable[[str], Mapping[str, Any]] | None = None,
    include_cycle_scoped: bool = True,
) -> dict[str, Any]:
    """Publish an explicitly bounded run batch using the copyback publisher."""

    requested = [_require_safe_identity(run_id, field="run_id") for run_id in run_ids]
    if not requested:
        raise PipelineJobProvenanceError("RUN_IDS_REQUIRED", "At least one run_id is required.")
    if len(set(requested)) != len(requested):
        raise PipelineJobProvenanceError("DUPLICATE_RUN_ID", "Each backfill run_id must be requested once.")
    if len(requested) > MAX_PROVENANCE_RUNS_PER_BATCH:
        raise PipelineJobProvenanceError(
            "RUN_COUNT_LIMIT",
            "Provenance batch exceeds the existing transport row budget.",
            {"run_count": len(requested), "max_runs": MAX_PROVENANCE_RUNS_PER_BATCH},
        )

    verify_journal_root_authority(journal_root, setting="NHMS_SCHEDULER_JOURNAL_ROOT")
    object_root = _require_existing_directory(object_store_root, field="object_store_root")
    preflight_store = LocalObjectStore(root=object_root, object_store_prefix=object_store_prefix)
    for run_id in requested:
        _identity_from_manifest(_load_run_manifest(preflight_store, run_id), expected_run_id=run_id)

    summaries: list[dict[str, Any]] = []
    log_cache = _ParentArrayLogCache(_bound_fetch_logs(slurm_client, fetch_logs))
    for run_id in requested:
        try:
            summaries.append(
                publish_run_pipeline_job_provenance(
                    run_id=run_id,
                    journal_root=journal_root,
                    object_store_root=object_store_root,
                    object_store_prefix=object_store_prefix,
                    published_artifact_root=published_artifact_root,
                    slurm_client=slurm_client,
                    fetch_logs=fetch_logs,
                    include_cycle_scoped=include_cycle_scoped,
                    log_cache=log_cache,
                )
            )
        except PipelineJobProvenanceError as error:
            summaries.append(
                publication_summary(
                    run_id=run_id,
                    status="failed",
                    reason=error.code,
                )
            )
        except Exception as error:  # noqa: BLE001 - bounded per-run backfill isolation
            summaries.append(
                publication_summary(
                    run_id=run_id,
                    status="failed",
                    reason=type(error).__name__,
                )
            )

    failed = [item for item in summaries if item["status"] != "published"]
    return {
        "status": "failed" if failed else "published",
        "runs": summaries,
        "published": len(summaries) - len(failed),
        "failed": len(failed),
    }


def import_run_pipeline_job_provenance(
    *,
    database_url: str,
    object_store_root: str | Path,
    run_id: str,
    object_store_prefix: str = "",
    connect: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Project one validated sidecar into ``ops.pipeline_job``.

    Missing sidecars are unavailable provenance, not ingest failure.
    """

    safe_run_id = _require_safe_identity(run_id, field="run_id")
    object_root = _require_existing_directory(object_store_root, field="object_store_root")
    store = LocalObjectStore(root=object_root, object_store_prefix=object_store_prefix)
    sidecar_key = sidecar_object_key(safe_run_id)
    if not store.exists(sidecar_key):
        return {
            "run_id": safe_run_id,
            "status": "unavailable",
            "reason": "sidecar_missing",
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "ignored_stale": 0,
        }
    sidecar = _load_sidecar(store, sidecar_key)
    manifest = _load_run_manifest(store, safe_run_id)
    identity = _identity_from_manifest(manifest, expected_run_id=safe_run_id)
    _require_sidecar_matches_manifest(sidecar, identity)
    jobs = [_validate_imported_job(job, sidecar_identity=sidecar["identity"]) for job in sidecar["jobs"]]

    connector = connect or _psycopg2_connect
    connection = connector(database_url, fallback_application_name="nhms-pipeline-job-provenance")
    try:
        with connection:
            with connection.cursor() as cursor:
                hydro = _load_hydro_identity(cursor, safe_run_id)
                _require_hydro_matches_sidecar(hydro, sidecar["identity"])
                _lock_projection_jobs(cursor, jobs)
                result = _project_jobs(cursor, jobs)
    finally:
        connection.close()
    result["run_id"] = safe_run_id
    result["status"] = "imported"
    return result


def import_runs_pipeline_job_provenance(
    *,
    database_url: str,
    object_store_root: str | Path,
    run_ids: Sequence[str],
    object_store_prefix: str = "",
    connect: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Project an explicitly bounded batch of already-published sidecars."""

    requested = [_require_safe_identity(run_id, field="run_id") for run_id in run_ids]
    if not requested:
        raise PipelineJobProvenanceError("RUN_IDS_REQUIRED", "At least one run_id is required.")
    if len(set(requested)) != len(requested):
        raise PipelineJobProvenanceError("DUPLICATE_RUN_ID", "Each import run_id must be requested once.")
    if len(requested) > MAX_PROVENANCE_RUNS_PER_BATCH:
        raise PipelineJobProvenanceError(
            "RUN_COUNT_LIMIT",
            "Provenance batch exceeds the existing transport row budget.",
            {"run_count": len(requested), "max_runs": MAX_PROVENANCE_RUNS_PER_BATCH},
        )
    return _import_requested_pipeline_job_provenance(
        database_url=database_url,
        object_store_root=object_store_root,
        run_ids=requested,
        object_store_prefix=object_store_prefix,
        connect=connect,
    )


def import_discovered_pipeline_job_provenance(
    *,
    database_url: str,
    object_store_root: str | Path,
    run_ids: Sequence[str],
    object_store_prefix: str = "",
    connect: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Project an unbounded discovered set through the existing 256-run cap."""

    requested = [_require_safe_identity(run_id, field="run_id") for run_id in run_ids]
    if len(set(requested)) != len(requested):
        raise PipelineJobProvenanceError("DUPLICATE_RUN_ID", "Each import run_id must be requested once.")
    summaries: list[dict[str, Any]] = []
    for offset in range(0, len(requested), MAX_PROVENANCE_RUNS_PER_BATCH):
        chunk = requested[offset : offset + MAX_PROVENANCE_RUNS_PER_BATCH]
        if not chunk:
            continue
        chunk_summary = _import_requested_pipeline_job_provenance(
            database_url=database_url,
            object_store_root=object_store_root,
            run_ids=chunk,
            object_store_prefix=object_store_prefix,
            connect=connect,
        )
        summaries.extend(chunk_summary["runs"])
    failed = [item for item in summaries if item["status"] == "failed"]
    return {
        "status": "failed" if failed else "imported",
        "runs": summaries,
        "imported": len([item for item in summaries if item["status"] == "imported"]),
        "unavailable": len([item for item in summaries if item["status"] == "unavailable"]),
        "failed": len(failed),
    }


def _import_requested_pipeline_job_provenance(
    *,
    database_url: str,
    object_store_root: str | Path,
    run_ids: Sequence[str],
    object_store_prefix: str = "",
    connect: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    for run_id in run_ids:
        try:
            summaries.append(
                import_run_pipeline_job_provenance(
                    database_url=database_url,
                    object_store_root=object_store_root,
                    run_id=run_id,
                    object_store_prefix=object_store_prefix,
                    connect=connect,
                )
            )
        except PipelineJobProvenanceError as error:
            summaries.append(
                {
                    "run_id": run_id,
                    "status": "failed",
                    "reason": error.code,
                    "inserted": 0,
                    "updated": 0,
                    "unchanged": 0,
                    "ignored_stale": 0,
                }
            )
        except Exception as error:  # noqa: BLE001 - bounded per-run isolation
            summaries.append(
                {
                    "run_id": run_id,
                    "status": "failed",
                    "reason": type(error).__name__,
                    "inserted": 0,
                    "updated": 0,
                    "unchanged": 0,
                    "ignored_stale": 0,
                }
            )

    failed = [item for item in summaries if item["status"] == "failed"]
    return {
        "status": "failed" if failed else "imported",
        "runs": summaries,
        "imported": len([item for item in summaries if item["status"] == "imported"]),
        "unavailable": len([item for item in summaries if item["status"] == "unavailable"]),
        "failed": len(failed),
    }



def _bound_fetch_logs(
    slurm_client: Any | None,
    fetch_logs: Callable[[str], Mapping[str, Any]] | None,
) -> Callable[[str], Mapping[str, Any]]:
    if fetch_logs is not None:
        return fetch_logs
    if slurm_client is not None and callable(getattr(slurm_client, "fetch_logs", None)):
        return slurm_client.fetch_logs

    def _unavailable(_job_id: str) -> Mapping[str, Any]:
        raise PipelineJobProvenanceError(
            "GATEWAY_LOGS_UNAVAILABLE",
            "No gateway log client is configured for verified task-log publication.",
        )

    return _unavailable


def _require_existing_directory(value: str | Path, *, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise PipelineJobProvenanceError("ROOT_REQUIRED", f"{field} is required.", {"field": field})
    try:
        path = Path(text).expanduser()
    except RuntimeError as error:
        raise PipelineJobProvenanceError(
            "ROOT_UNEXPANDABLE",
            f"{field} home directory cannot be expanded.",
            {"field": field},
        ) from error
    if not path.is_absolute():
        raise PipelineJobProvenanceError(
            "ROOT_NOT_ABSOLUTE",
            f"{field} must be an absolute directory.",
            {"field": field},
        )
    try:
        return verify_directory_no_follow(path)
    except (OSError, SafeFilesystemError) as error:
        raise PipelineJobProvenanceError(
            "ROOT_UNSAFE",
            f"{field} is missing, not a directory, or not a real directory chain.",
            {"field": field},
        ) from error


def _load_run_manifest(store: LocalObjectStore, run_id: str) -> dict[str, Any]:
    key = f"runs/{run_id}/input/manifest.json"
    try:
        payload = json.loads(store.read_bytes_limited(key, max_bytes=MAX_SIDECAR_BYTES).decode("utf-8"))
    except FileNotFoundError as error:
        raise PipelineJobProvenanceError(
            "MANIFEST_MISSING",
            "Run scientific manifest is missing.",
            {"run_id": run_id},
        ) from error
    except (ObjectStoreError, OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PipelineJobProvenanceError(
            "MANIFEST_UNREADABLE",
            "Run scientific manifest could not be read.",
            {"run_id": run_id},
        ) from error
    if not isinstance(payload, Mapping):
        raise PipelineJobProvenanceError(
            "MANIFEST_INVALID",
            "Run scientific manifest is not an object.",
            {"run_id": run_id},
        )
    return dict(payload)


def _identity_from_manifest(manifest: Mapping[str, Any], *, expected_run_id: str) -> dict[str, str]:
    nested = manifest.get("identity") if isinstance(manifest.get("identity"), Mapping) else {}
    run_id = _require_safe_identity(
        manifest.get("run_id") or nested.get("run_id"),
        field="run_id",
    )
    if run_id != expected_run_id:
        raise PipelineJobProvenanceError(
            "MANIFEST_RUN_MISMATCH",
            "Scientific manifest run_id does not match the selected run.",
            {"expected_run_id": expected_run_id, "manifest_run_id": run_id},
        )
    source = _normalize_source(manifest.get("source_id") or nested.get("source_id") or nested.get("source"))
    cycle_time = _require_cycle_time(manifest.get("cycle_time") or nested.get("cycle_time"))
    model_id = _require_safe_identity(
        nested.get("model_id") or manifest.get("model_id") or _model_id_from_run_id(run_id),
        field="model_id",
    )
    match = FORECAST_RUN_ID_RE.fullmatch(run_id)
    if match is None:
        raise PipelineJobProvenanceError(
            "RUN_ID_NOT_FORECAST",
            "Selected run_id is not a forecast run identity.",
            {"run_id": run_id},
        )
    embedded_source = _normalize_source(match.group(1))
    embedded_cycle = parse_cycle_time(match.group(2))
    if embedded_source != source or format_cycle_time(embedded_cycle) != format_cycle_time(cycle_time):
        raise PipelineJobProvenanceError(
            "MANIFEST_IDENTITY_MISMATCH",
            "Scientific manifest identity does not match the forecast run_id.",
            {"run_id": run_id, "source": source, "cycle_time": _format_utc(cycle_time)},
        )
    if match.group(3) != model_id:
        raise PipelineJobProvenanceError(
            "MANIFEST_MODEL_MISMATCH",
            "Scientific manifest model_id does not match the forecast run_id.",
            {"run_id": run_id, "model_id": model_id},
        )
    return {
        "source": source,
        "cycle_time": _format_utc(cycle_time),
        "cycle_id": cycle_id_for(source, cycle_time),
        "run_id": run_id,
        "model_id": model_id,
    }


def _export_jobs_for_run(
    repository: FileOrchestrationJournalRepository,
    *,
    identity: Mapping[str, str],
    include_cycle_scoped: bool,
    published_artifact_root: str | Path | None,
) -> list[dict[str, Any]]:
    cycle_id = identity["cycle_id"]
    try:
        cycle_jobs = repository.iter_publication_pipeline_jobs_by_cycle(cycle_id)
    except FileOrchestrationJournalError as error:
        raise PipelineJobProvenanceError(
            "JOURNAL_READ_FAILED",
            "File journal could not be read for the selected cycle.",
            {"cycle_id": cycle_id, "reason": error.reason},
        ) from error
    exported: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in cycle_jobs:
        if str(raw.get("status") or "") == FILE_JOURNAL_READ_BLOCKED_STATUS:
            raise PipelineJobProvenanceError(
                "JOURNAL_READ_BLOCKED",
                "File journal publication view returned a blocked row.",
                {"cycle_id": cycle_id},
            )
        job = _export_job_row(
            raw,
            identity=identity,
            include_cycle_scoped=include_cycle_scoped,
            published_artifact_root=published_artifact_root,
        )
        if job is None:
            continue
        job_id = job["job_id"]
        if job_id in seen:
            raise PipelineJobProvenanceError(
                "DUPLICATE_JOB_ID",
                "Journal snapshot contains duplicate job_id values.",
                {"job_id": job_id},
            )
        seen.add(job_id)
        exported.append(job)
    if not any(job["run_id"] == identity["run_id"] and job.get("model_id") == identity["model_id"] for job in exported):
        raise PipelineJobProvenanceError(
            "FORECAST_JOB_MISSING",
            "Journal snapshot has no jobs bound to the selected forecast run.",
            {"run_id": identity["run_id"], "model_id": identity["model_id"]},
        )
    return exported


def _export_job_row(
    raw: Mapping[str, Any],
    *,
    identity: Mapping[str, str],
    include_cycle_scoped: bool,
    published_artifact_root: str | Path | None,
) -> dict[str, Any] | None:
    job_id = raw.get("job_id")
    if not isinstance(job_id, str) or not _SAFE_IDENTITY_RE.fullmatch(job_id):
        return None
    run_id = raw.get("run_id")
    model_id = raw.get("model_id")
    selected_run = str(run_id or "") == identity["run_id"] and str(model_id or "") == identity["model_id"]
    cycle_scoped = _is_cycle_scoped_row(raw, identity=identity)
    if selected_run:
        exported_run_id = identity["run_id"]
        exported_model_id = identity["model_id"]
    elif include_cycle_scoped and cycle_scoped:
        exported_run_id = str(run_id)
        exported_model_id = None
    else:
        return None
    status = raw.get("status")
    if status not in ALLOWED_JOB_STATUSES:
        raise PipelineJobProvenanceError(
            "JOB_STATUS_INVALID",
            "Journal job status is not in the published allowlist.",
            {"job_id": job_id, "status": status},
        )
    stage = raw.get("stage")
    if stage not in (None, "") and stage not in ALLOWED_STAGES:
        raise PipelineJobProvenanceError(
            "JOB_STAGE_INVALID",
            "Journal job stage is not in the published allowlist.",
            {"job_id": job_id, "stage": stage},
        )
    log_identity = {
        "source": identity["source"],
        "cycle_time": identity["cycle_time"],
        "run_id": exported_run_id,
        "model_id": exported_model_id or identity["model_id"],
    }
    log_uri = _exported_existing_log_uri(
        raw.get("log_uri"),
        identity=log_identity,
        job_id=job_id,
        published_artifact_root=published_artifact_root,
    )
    return {
        "job_id": job_id,
        "run_id": exported_run_id,
        "cycle_id": identity["cycle_id"],
        "job_type": _optional_bounded_text(raw.get("job_type"), field="job_type", required=True),
        "slurm_job_id": _optional_bounded_text(raw.get("slurm_job_id"), field="slurm_job_id"),
        "array_task_id": _optional_int(raw.get("array_task_id"), field="array_task_id"),
        "model_id": exported_model_id,
        "status": status,
        "stage": None if stage in (None, "") else stage,
        "submitted_at": _optional_timestamp(raw.get("submitted_at"), field="submitted_at"),
        "started_at": _optional_timestamp(raw.get("started_at"), field="started_at"),
        "finished_at": _optional_timestamp(raw.get("finished_at"), field="finished_at"),
        "exit_code": _optional_int(raw.get("exit_code"), field="exit_code"),
        "retry_count": _optional_int(raw.get("retry_count"), field="retry_count") or 0,
        "error_code": _optional_bounded_text(raw.get("error_code"), field="error_code"),
        "error_message": _optional_bounded_text(raw.get("error_message"), field="error_message", max_chars=512),
        "log_uri": log_uri,
        "log_truncated": None,
        "log_stdout_bytes": None,
        "log_stderr_bytes": None,
        "created_at": _optional_timestamp(raw.get("created_at"), field="created_at"),
        "updated_at": _optional_timestamp(raw.get("updated_at"), field="updated_at"),
    }


def _is_cycle_scoped_row(raw: Mapping[str, Any], *, identity: Mapping[str, str]) -> bool:
    if raw.get("model_id") not in (None, ""):
        return False
    run_id = str(raw.get("run_id") or "")
    if not run_id or run_id == identity["run_id"]:
        return False
    if str(raw.get("cycle_id") or "") != identity["cycle_id"]:
        return False
    stage = str(raw.get("stage") or "")
    return stage in _CYCLE_SCOPED_STAGES or run_id.startswith("cycle_")


def _exported_existing_log_uri(
    value: Any,
    *,
    identity: Mapping[str, str],
    job_id: str,
    published_artifact_root: str | Path | None,
) -> str | None:
    if value in (None, "") or not isinstance(value, str) or value in REDACTION_SENTINELS:
        return None
    if not _is_canonical_published_log_uri(value, identity=identity, job_id=job_id):
        return None
    try:
        root = _published_artifact_root(published_artifact_root)
        relative = published_log_relative_path(value)
        read_bytes_limited_no_follow(root / relative, max_bytes=MAX_LOG_BYTES, containment_root=root)
    except (PipelineJobProvenanceError, OSError, SafeFilesystemError):
        return None
    return value


def _maybe_advertise_verified_log(
    job: Mapping[str, Any],
    *,
    identity: Mapping[str, str],
    log_cache: _ParentArrayLogCache,
    published_artifact_root: str | Path | None,
    existing_job: Mapping[str, Any] | None = None,
    stage_only: bool = False,
) -> tuple[dict[str, Any], bool]:
    exported = dict(job)
    if exported.get("log_uri"):
        return exported, False
    if job.get("run_id") != identity["run_id"] or job.get("model_id") != identity["model_id"]:
        return exported, False
    task_id = job.get("array_task_id")
    slurm_job_id = job.get("slurm_job_id")
    if not isinstance(task_id, int) or not isinstance(slurm_job_id, str):
        return exported, False
    parent_job_id, separator, child_task_id = slurm_job_id.partition("_")
    if not parent_job_id.isdigit():
        return exported, False
    if separator and (not child_task_id.isdigit() or int(child_task_id) != task_id):
        return exported, False
    try:
        response = log_cache.fetch(parent_job_id)
        entry = _select_matching_task_entry(
            response,
            parent_job_id=parent_job_id,
            array_task_id=task_id,
            run_id=identity["run_id"],
            model_id=identity["model_id"],
        )
        if entry is None:
            return exported, False
        advertised = _publish_task_log_streams(
            entry,
            identity=identity,
            job_id=str(job["job_id"]),
            published_artifact_root=published_artifact_root,
            existing_job=existing_job,
            stage_only=stage_only,
            exported=exported,
        )
    except PipelineJobProvenanceError:
        return exported, False
    except Exception:  # noqa: BLE001 - missing logs stay unadvertised
        return exported, False
    if not advertised:
        return exported, False
    stdout = entry.get("stdout")
    stderr = entry.get("stderr")
    exported["log_uri"] = advertised
    exported["log_truncated"] = bool(response.get("truncated") or entry.get("truncated"))
    exported["log_stdout_bytes"] = len(stdout.encode("utf-8")) if isinstance(stdout, str) else None
    exported["log_stderr_bytes"] = len(stderr.encode("utf-8")) if isinstance(stderr, str) else None
    return exported, True





def _select_matching_task_entry(
    response: Mapping[str, Any],
    *,
    parent_job_id: str,
    array_task_id: int,
    run_id: str,
    model_id: str,
) -> Mapping[str, Any] | None:
    if (
        not parent_job_id.isdigit()
        or str(response.get("job_id") or "") != parent_job_id
        or response.get("complete") is not True
        or response.get("metadata_complete") is not True
    ):
        return None
    entries = response.get("array_task_logs")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
        return None
    matches: list[Mapping[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        task_id = entry.get("task_id") if "task_id" in entry else entry.get("array_task_id")
        try:
            parsed_task_id = int(task_id)
        except (TypeError, ValueError):
            continue
        if parsed_task_id == array_task_id:
            matches.append(entry)
    if len(matches) != 1:
        return None
    entry = matches[0]
    expected_task_job_id = f"{parent_job_id}_{array_task_id}"
    for field in ("job_id", "slurm_job_id"):
        bound_task_job_id = entry.get(field)
        if bound_task_job_id not in (None, "") and str(bound_task_job_id) != expected_task_job_id:
            return None
    if entry.get("identity_complete") is not True:
        return None
    if str(entry.get("run_id") or "") != run_id or str(entry.get("model_id") or "") != model_id:
        return None
    return entry


def _publish_task_log_streams(
    entry: Mapping[str, Any],
    *,
    identity: Mapping[str, str],
    job_id: str,
    published_artifact_root: str | Path | None,
    existing_job: Mapping[str, Any] | None = None,
    stage_only: bool = False,
    exported: dict[str, Any] | None = None,
) -> str | None:
    stdout = entry.get("stdout")
    if not isinstance(stdout, str) or not stdout:
        return None
    stdout_bytes = stdout.encode("utf-8")
    if len(stdout_bytes) > MAX_LOG_BYTES:
        return None
    cycle_time = parse_cycle_time(identity["cycle_time"])
    out_uri = published_log_uri(
        source=identity["source"],
        cycle_time=cycle_time,
        run_id=identity["run_id"],
        job_id=job_id,
        stream="out",
    )
    stderr = entry.get("stderr")
    stderr_bytes: bytes | None = None
    err_uri: str | None = None
    if isinstance(stderr, str) and stderr:
        encoded_stderr = stderr.encode("utf-8")
        if len(encoded_stderr) <= MAX_LOG_BYTES:
            stderr_bytes = encoded_stderr
            err_uri = published_log_uri(
                source=identity["source"],
                cycle_time=cycle_time,
                run_id=identity["run_id"],
                job_id=job_id,
                stream="err",
            )
    if stage_only:
        if exported is not None:
            exported["_pending_stdout_bytes"] = stdout_bytes
            exported["_pending_out_uri"] = out_uri
            if stderr_bytes is not None and err_uri is not None:
                exported["_pending_stderr_bytes"] = stderr_bytes
                exported["_pending_err_uri"] = err_uri
        return out_uri
    _write_published_log(
        out_uri,
        stdout_bytes,
        published_artifact_root=published_artifact_root,
        existing_uri=None if existing_job is None else existing_job.get("log_uri"),
    )
    if stderr_bytes is not None and err_uri is not None:
        _write_published_log(
            err_uri,
            stderr_bytes,
            published_artifact_root=published_artifact_root,
            existing_uri=err_uri if existing_job and existing_job.get("log_stderr_bytes") else None,
        )
    return out_uri






def _published_artifact_root(published_artifact_root: str | Path | None) -> Path:
    root_value = str(published_artifact_root or os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT", "")).strip()
    if not root_value:
        raise PipelineJobProvenanceError(
            "PUBLISHED_ROOT_REQUIRED",
            "NHMS_PUBLISHED_ARTIFACT_ROOT is required to publish verified logs.",
        )
    return _require_existing_directory(root_value, field="published_artifact_root")


def _write_published_log(
    log_uri: str,
    content: bytes,
    *,
    published_artifact_root: str | Path | None,
    existing_uri: Any = None,
) -> None:
    root = _published_artifact_root(published_artifact_root)
    target = root / published_log_relative_path(log_uri)
    if existing_uri not in (None, "") and existing_uri == log_uri:
        try:
            persisted = read_bytes_limited_no_follow(target, max_bytes=MAX_LOG_BYTES, containment_root=root)
        except FileNotFoundError:
            raise PipelineJobProvenanceError(
                "PUBLISHED_LOG_MISSING",
                "Advertised published log is missing from the published artifact root.",
                {"log_uri": log_uri},
            )
        except (OSError, SafeFilesystemError) as error:
            raise PipelineJobProvenanceError(
                "PUBLISHED_LOG_UNREADABLE",
                "Advertised published log could not be read without following links.",
                {"log_uri": log_uri},
            ) from error
        if persisted == content:
            return
        raise PipelineJobProvenanceError(
            "EQUAL_VERSION_CONFLICT",
            "Equal-version published provenance conflicts with the existing advertised log.",
            {"log_uri": log_uri},
        )
    atomic_write_bytes_no_follow(target, content, containment_root=root, temp_suffix="part")
    persisted = read_bytes_limited_no_follow(target, max_bytes=MAX_LOG_BYTES, containment_root=root)
    if persisted != content:
        raise PipelineJobProvenanceError(
            "PUBLISHED_LOG_VERIFY_FAILED",
            "Published task-log content did not match the verified gateway stream.",
            {"log_uri": log_uri},
        )






def _build_sidecar(*, identity: Mapping[str, str], jobs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(jobs) > MAX_SIDECAR_JOBS:
        raise PipelineJobProvenanceError(
            "JOB_COUNT_LIMIT",
            "Provenance sidecar exceeds the published job-row budget.",
            {"job_count": len(jobs), "max_jobs": MAX_SIDECAR_JOBS},
        )
    source_version = _source_version_from_jobs(jobs)
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "source_version": source_version,
        "identity": {
            "source": identity["source"],
            "cycle_time": identity["cycle_time"],
            "run_id": identity["run_id"],
            "model_id": identity["model_id"],
        },
        "jobs": [dict(job) for job in jobs],
    }


def _merge_published_sidecar(
    store: LocalObjectStore,
    *,
    sidecar_key: str,
    identity: Mapping[str, str],
    jobs: Sequence[Mapping[str, Any]],
    existing_jobs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    incoming = _validated_jobs_by_id(jobs, identity=identity)
    if existing_jobs is None:
        if not store.exists(sidecar_key):
            return _build_sidecar(identity=identity, jobs=list(incoming.values()))
        existing_sidecar = _load_sidecar(store, sidecar_key)
        _require_sidecar_matches_manifest(existing_sidecar, identity)
        existing = _validated_jobs_by_id(existing_sidecar["jobs"], identity=identity)
    elif not existing_jobs:
        return _build_sidecar(identity=identity, jobs=list(incoming.values()))
    else:
        existing = {job_id: dict(job) for job_id, job in existing_jobs.items()}
    return _merge_incoming_jobs_into_existing(existing, incoming, identity=identity)


def _require_jobs_mergeable_with_existing(
    jobs: Sequence[Mapping[str, Any]],
    *,
    existing_jobs: Mapping[str, Mapping[str, Any]],
    identity: Mapping[str, str],
) -> None:
    if not existing_jobs:
        if len(jobs) > MAX_SIDECAR_JOBS:
            raise PipelineJobProvenanceError(
                "JOB_COUNT_LIMIT",
                "Provenance sidecar exceeds the published job-row budget.",
                {"job_count": len(jobs), "max_jobs": MAX_SIDECAR_JOBS},
            )
        return
    incoming = _validated_jobs_by_id(jobs, identity=identity)
    _merge_incoming_jobs_into_existing(
        {job_id: dict(job) for job_id, job in existing_jobs.items()},
        incoming,
        identity=identity,
    )


def _merge_incoming_jobs_into_existing(
    existing: Mapping[str, Mapping[str, Any]],
    incoming: Mapping[str, Mapping[str, Any]],
    *,
    identity: Mapping[str, str],
) -> dict[str, Any]:
    merged = {job_id: dict(job) for job_id, job in existing.items()}
    for job_id, incoming_job in incoming.items():
        existing_job = existing.get(job_id)
        if existing_job is None:
            merged[job_id] = dict(incoming_job)
            continue
        existing_identity = {field: existing_job.get(field) for field in JOB_IDENTITY_FIELDS}
        incoming_identity = {field: incoming_job.get(field) for field in JOB_IDENTITY_FIELDS}
        if existing_identity != incoming_identity:
            raise PipelineJobProvenanceError(
                "PUBLISHED_JOB_IDENTITY_CONFLICT",
                "Existing published provenance has a conflicting immutable job identity.",
                {"job_id": job_id},
            )
        candidate = _merge_published_log_evidence(existing_job, incoming_job)
        existing_version = _job_source_timestamp(existing_job)
        incoming_version = _job_source_timestamp(candidate)
        if existing_version > incoming_version:
            continue
        if existing_version == incoming_version:
            if _published_job_payload_equal(existing_job, candidate):
                merged[job_id] = candidate
                continue
            if (
                existing_job.get("log_uri") in (None, "")
                and candidate.get("log_uri") not in (None, "")
                and _job_payload_equal(existing_job, candidate, compare_log_uri=False)
            ):
                merged[job_id] = candidate
                continue
            raise PipelineJobProvenanceError(
                "EQUAL_VERSION_CONFLICT",
                "Equal-version published provenance conflicts with the existing sidecar.",
                {"job_id": job_id},
            )
        merged[job_id] = candidate
    return _build_sidecar(identity=identity, jobs=[merged[job_id] for job_id in sorted(merged)])




def _validated_jobs_by_id(
    jobs: Sequence[Mapping[str, Any]],
    *,
    identity: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    validated: dict[str, dict[str, Any]] = {}
    sidecar_identity = {
        "source": identity["source"],
        "cycle_time": identity["cycle_time"],
        "run_id": identity["run_id"],
        "model_id": identity["model_id"],
    }
    for raw_job in jobs:
        job = _validate_imported_job(raw_job, sidecar_identity=sidecar_identity)
        job_id = job["job_id"]
        if job_id in validated:
            raise PipelineJobProvenanceError(
                "DUPLICATE_JOB_ID",
                "Provenance sidecar contains duplicate job_id values.",
                {"job_id": job_id},
            )
        validated[job_id] = job
    return validated


def _merge_published_log_evidence(
    existing: Mapping[str, Any],
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(incoming)
    existing_log = existing.get("log_uri")
    incoming_log = incoming.get("log_uri")
    if existing_log not in (None, "") and incoming_log not in (None, "") and existing_log != incoming_log:
        raise PipelineJobProvenanceError(
            "LOG_URI_CONFLICT",
            "Published provenance cannot replace an existing verified log URI.",
            {"job_id": incoming.get("job_id")},
        )
    if existing_log not in (None, "") and incoming_log in (None, ""):
        merged["log_uri"] = existing_log
    if merged.get("log_uri") == existing_log:
        for field in ("log_truncated", "log_stdout_bytes", "log_stderr_bytes"):
            if merged.get(field) is None:
                merged[field] = existing.get(field)
    return merged


def _published_job_payload_equal(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> bool:
    return _job_payload_equal(existing, incoming) and all(
        _normalize_comparable(existing.get(field)) == _normalize_comparable(incoming.get(field))
        for field in ("log_truncated", "log_stdout_bytes", "log_stderr_bytes")
    )


def _source_version_from_jobs(jobs: Sequence[Mapping[str, Any]]) -> int:
    latest = max((_job_source_timestamp(job) for job in jobs), default=None)
    if latest is None:
        raise PipelineJobProvenanceError(
            "SOURCE_VERSION_UNKNOWN",
            "Provenance sidecar has no source-owned timestamp for ordering.",
        )
    # Schema v1 uses an integer source_version. Per-job RFC 3339 timestamps
    # carry the authoritative fractional precision used by projection.
    return int(latest.timestamp())


def _encode_sidecar(sidecar: Mapping[str, Any]) -> bytes:
    payload = json.dumps(sidecar, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(payload) > MAX_SIDECAR_BYTES:
        raise PipelineJobProvenanceError(
            "SIDECAR_TOO_LARGE",
            "Provenance sidecar exceeds the published byte budget.",
            {"bytes": len(payload), "max_bytes": MAX_SIDECAR_BYTES},
        )
    return payload


def _load_sidecar(store: LocalObjectStore, sidecar_key: str) -> dict[str, Any]:
    try:
        raw = store.read_bytes_limited(sidecar_key, max_bytes=MAX_SIDECAR_BYTES)
        payload = json.loads(raw.decode("utf-8"))
    except (ObjectStoreError, OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PipelineJobProvenanceError(
            "SIDECAR_UNREADABLE",
            "Provenance sidecar could not be read.",
            {"sidecar_key": sidecar_key},
        ) from error
    if not isinstance(payload, Mapping):
        raise PipelineJobProvenanceError("SIDECAR_INVALID", "Provenance sidecar is not an object.")
    if payload.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise PipelineJobProvenanceError(
            "SIDECAR_SCHEMA_MISMATCH",
            "Provenance sidecar schema_version is not supported.",
            {"schema_version": payload.get("schema_version")},
        )
    identity = payload.get("identity")
    jobs = payload.get("jobs")
    if not isinstance(identity, Mapping) or not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
        raise PipelineJobProvenanceError("SIDECAR_INVALID", "Provenance sidecar identity or jobs are invalid.")
    if any(not isinstance(job, Mapping) for job in jobs):
        raise PipelineJobProvenanceError("SIDECAR_INVALID", "Provenance sidecar jobs must all be objects.")
    if len(jobs) > MAX_SIDECAR_JOBS:
        raise PipelineJobProvenanceError("JOB_COUNT_LIMIT", "Provenance sidecar exceeds the published job-row budget.")
    source_version = payload.get("source_version")
    try:
        parsed_version = int(source_version)
    except (TypeError, ValueError) as error:
        raise PipelineJobProvenanceError(
            "SIDECAR_VERSION_INVALID",
            "Provenance sidecar source_version is invalid.",
        ) from error
    if parsed_version < 0:
        raise PipelineJobProvenanceError("SIDECAR_VERSION_INVALID", "Provenance sidecar source_version is invalid.")
    closed_identity = {
        "source": _normalize_source(identity.get("source") or identity.get("source_id")),
        "cycle_time": _format_utc(_require_cycle_time(identity.get("cycle_time"))),
        "run_id": _require_safe_identity(identity.get("run_id"), field="run_id"),
        "model_id": _require_safe_identity(identity.get("model_id"), field="model_id"),
    }
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "source_version": parsed_version,
        "identity": closed_identity,
        "jobs": [dict(job) for job in jobs],
    }


def _require_sidecar_matches_manifest(sidecar: Mapping[str, Any], identity: Mapping[str, str]) -> None:
    sidecar_identity = sidecar["identity"]
    for field in ("source", "cycle_time", "run_id", "model_id"):
        if sidecar_identity.get(field) != identity.get(field):
            raise PipelineJobProvenanceError(
                "SIDECAR_MANIFEST_MISMATCH",
                "Provenance sidecar identity does not match the scientific manifest.",
                {"field": field},
            )


def _validate_imported_job(job: Mapping[str, Any], *, sidecar_identity: Mapping[str, str]) -> dict[str, Any]:
    unexpected = set(job) - set(JOB_ALLOWLIST_FIELDS)
    if unexpected:
        raise PipelineJobProvenanceError(
            "JOB_FIELD_UNALLOWLISTED",
            "Provenance job row contains fields outside the closed allowlist.",
            {"fields": sorted(unexpected)},
        )
    job_id = _require_safe_identity(job.get("job_id"), field="job_id")
    run_id = _require_safe_identity(job.get("run_id"), field="run_id")
    cycle_id = _require_safe_identity(job.get("cycle_id"), field="cycle_id")
    expected_cycle_id = cycle_id_for(
        sidecar_identity["source"],
        parse_cycle_time(sidecar_identity["cycle_time"]),
    )
    if cycle_id != expected_cycle_id:
        raise PipelineJobProvenanceError(
            "JOB_CYCLE_MISMATCH",
            "Provenance job cycle_id does not match the sidecar identity.",
            {"job_id": job_id, "cycle_id": cycle_id},
        )
    model_id = job.get("model_id")
    if model_id in (None, ""):
        exported_model_id = None
        if run_id == sidecar_identity["run_id"]:
            raise PipelineJobProvenanceError(
                "FORECAST_JOB_MODEL_REQUIRED",
                "Forecast-scoped provenance jobs must carry the selected model_id.",
                {"job_id": job_id},
            )
    else:
        exported_model_id = _require_safe_identity(model_id, field="model_id")
        if run_id == sidecar_identity["run_id"] and exported_model_id != sidecar_identity["model_id"]:
            raise PipelineJobProvenanceError(
                "JOB_MODEL_MISMATCH",
                "Forecast-scoped provenance job model_id does not match the sidecar.",
                {"job_id": job_id},
            )
        if run_id != sidecar_identity["run_id"]:
            raise PipelineJobProvenanceError(
                "CYCLE_JOB_MODEL_FORBIDDEN",
                "Cycle-scoped provenance jobs must keep a null model_id.",
                {"job_id": job_id},
            )
    status = job.get("status")
    if status not in ALLOWED_JOB_STATUSES:
        raise PipelineJobProvenanceError(
            "JOB_STATUS_INVALID",
            "Provenance job status is not in the published allowlist.",
            {"job_id": job_id, "status": status},
        )
    stage = job.get("stage")
    if stage not in (None, "") and stage not in ALLOWED_STAGES:
        raise PipelineJobProvenanceError(
            "JOB_STAGE_INVALID",
            "Provenance job stage is not in the published allowlist.",
            {"job_id": job_id, "stage": stage},
        )
    log_uri = job.get("log_uri")
    if log_uri not in (None, ""):
        log_identity = {
            "source": sidecar_identity["source"],
            "cycle_time": sidecar_identity["cycle_time"],
            "run_id": run_id,
            "model_id": exported_model_id or sidecar_identity["model_id"],
        }
        if not _is_canonical_published_log_uri(str(log_uri), identity=log_identity, job_id=job_id):
            raise PipelineJobProvenanceError(
                "LOG_URI_INVALID",
                "Provenance log_uri is not a verified canonical published log.",
                {"job_id": job_id},
            )
    log_truncated = _optional_bool(job.get("log_truncated"), field="log_truncated")
    log_stdout_bytes = _optional_nonnegative_int(job.get("log_stdout_bytes"), field="log_stdout_bytes")
    log_stderr_bytes = _optional_nonnegative_int(job.get("log_stderr_bytes"), field="log_stderr_bytes")
    if log_uri in (None, "") and any(
        value is not None for value in (log_truncated, log_stdout_bytes, log_stderr_bytes)
    ):
        raise PipelineJobProvenanceError(
            "LOG_METADATA_INVALID",
            "Log metadata cannot be advertised without a verified log_uri.",
            {"job_id": job_id},
        )
    return {
        "job_id": job_id,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "job_type": _optional_bounded_text(job.get("job_type"), field="job_type", required=True),
        "slurm_job_id": _optional_bounded_text(job.get("slurm_job_id"), field="slurm_job_id"),
        "array_task_id": _optional_int(job.get("array_task_id"), field="array_task_id"),
        "model_id": exported_model_id,
        "status": status,
        "stage": None if stage in (None, "") else stage,
        "submitted_at": _optional_timestamp(job.get("submitted_at"), field="submitted_at"),
        "started_at": _optional_timestamp(job.get("started_at"), field="started_at"),
        "finished_at": _optional_timestamp(job.get("finished_at"), field="finished_at"),
        "exit_code": _optional_int(job.get("exit_code"), field="exit_code"),
        "retry_count": _optional_int(job.get("retry_count"), field="retry_count") or 0,
        "error_code": _optional_bounded_text(job.get("error_code"), field="error_code"),
        "error_message": _optional_bounded_text(job.get("error_message"), field="error_message", max_chars=512),
        "log_uri": None if log_uri in (None, "") else str(log_uri),
        "log_truncated": log_truncated,
        "log_stdout_bytes": log_stdout_bytes,
        "log_stderr_bytes": log_stderr_bytes,
        "created_at": _optional_timestamp(job.get("created_at"), field="created_at"),
        "updated_at": _optional_timestamp(job.get("updated_at"), field="updated_at"),
    }


def _load_hydro_identity(cursor: Any, run_id: str) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT run_id, source_id, cycle_time, model_id, status, updated_at
        FROM hydro.hydro_run
        WHERE run_id = %s
        LIMIT 1
        """,
        (run_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise PipelineJobProvenanceError(
            "HYDRO_RUN_MISSING",
            "Hydro run is missing for the provenance sidecar.",
            {"run_id": run_id},
        )
    if isinstance(row, Mapping):
        return dict(row)
    return {
        "run_id": row[0],
        "source_id": row[1],
        "cycle_time": row[2],
        "model_id": row[3],
        "status": row[4],
        "updated_at": row[5],
    }


def _require_hydro_matches_sidecar(hydro: Mapping[str, Any], identity: Mapping[str, str]) -> None:
    hydro_source = _normalize_source(hydro.get("source_id"))
    hydro_cycle = hydro.get("cycle_time")
    if isinstance(hydro_cycle, datetime):
        hydro_cycle_text = _format_utc(hydro_cycle)
    else:
        hydro_cycle_text = _format_utc(_require_cycle_time(hydro_cycle))
    if (
        str(hydro.get("run_id") or "") != identity["run_id"]
        or hydro_source != identity["source"]
        or hydro_cycle_text != identity["cycle_time"]
        or str(hydro.get("model_id") or "") != identity["model_id"]
    ):
        raise PipelineJobProvenanceError(
            "HYDRO_IDENTITY_MISMATCH",
            "Hydro run identity does not match the provenance sidecar.",
            {"run_id": identity["run_id"]},
        )


def _project_jobs(cursor: Any, jobs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    inserted = 0
    updated = 0
    unchanged = 0
    ignored_stale = 0
    seen: set[str] = set()
    for job in jobs:
        job_id = str(job["job_id"])
        if job_id in seen:
            raise PipelineJobProvenanceError(
                "DUPLICATE_JOB_ID",
                "Provenance sidecar contains duplicate job_id values.",
                {"job_id": job_id},
            )
        seen.add(job_id)
        incoming_version = _job_source_timestamp(job)
        existing = _fetch_existing_job(cursor, job_id)
        if existing is None:
            _insert_job(cursor, job)
            inserted += 1
            continue
        existing_identity = {field: existing.get(field) for field in JOB_IDENTITY_FIELDS}
        incoming_identity = {field: job.get(field) for field in JOB_IDENTITY_FIELDS}
        if existing_identity != incoming_identity:
            raise PipelineJobProvenanceError(
                "JOB_IDENTITY_CONFLICT",
                "Existing pipeline_job identity does not match the provenance row.",
                {"job_id": job_id},
            )
        existing_version = _existing_source_version(existing)
        if existing_version is not None and existing_version > incoming_version:
            ignored_stale += 1
            continue
        if _job_payload_equal(existing, job):
            unchanged += 1
            continue
        if existing_version == incoming_version:
            if (
                existing.get("log_uri") in (None, "")
                and job.get("log_uri") not in (None, "")
                and _job_payload_equal(existing, job, compare_log_uri=False)
            ):
                _update_job(cursor, job, existing=existing)
                updated += 1
                continue
            if (
                existing.get("log_uri") not in (None, "")
                and job.get("log_uri") in (None, "")
                and _job_payload_equal(existing, job, compare_log_uri=False)
            ):
                unchanged += 1
                continue
            raise PipelineJobProvenanceError(
                "EQUAL_VERSION_CONFLICT",
                "Equal-version provenance payload conflicts with the existing job row.",
                {"job_id": job_id},
            )
        _update_job(cursor, job, existing=existing)
        updated += 1
    return {
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "ignored_stale": ignored_stale,
    }


def _lock_projection_jobs(cursor: Any, jobs: Sequence[Mapping[str, Any]]) -> None:
    lock_keys = sorted(
        {
            f"{JOB_PROJECTION_LOCK_NAMESPACE}:{job_id}"
            for job_id in (str(job.get("job_id") or "") for job in jobs)
            if job_id
        }
    )
    for lock_key in lock_keys:
        try:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
        except Exception:  # noqa: BLE001 - SQLite adapters have no advisory locks
            return




def _fetch_existing_job(cursor: Any, job_id: str) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT
            job_id, run_id, cycle_id, job_type, slurm_job_id, array_task_id, model_id,
            status, stage, submitted_at, started_at, finished_at, exit_code, retry_count,
            error_code, error_message, log_uri, created_at, updated_at
        FROM ops.pipeline_job
        WHERE job_id = %s
        FOR UPDATE
        """,
        (job_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    columns = [
        "job_id",
        "run_id",
        "cycle_id",
        "job_type",
        "slurm_job_id",
        "array_task_id",
        "model_id",
        "status",
        "stage",
        "submitted_at",
        "started_at",
        "finished_at",
        "exit_code",
        "retry_count",
        "error_code",
        "error_message",
        "log_uri",
        "created_at",
        "updated_at",
    ]
    return dict(zip(columns, row, strict=True))




def _insert_job(cursor: Any, job: Mapping[str, Any]) -> None:
    source_timestamp = _format_utc(_job_source_timestamp(job))
    created_at = job.get("created_at") or job.get("updated_at") or source_timestamp
    updated_at = job.get("updated_at") or source_timestamp
    cursor.execute(
        """
        INSERT INTO ops.pipeline_job (
            job_id, run_id, cycle_id, job_type, slurm_job_id, array_task_id, model_id,
            status, stage, submitted_at, started_at, finished_at, exit_code, retry_count,
            error_code, error_message, log_uri, created_at, updated_at
        )
        VALUES (
            %(job_id)s, %(run_id)s, %(cycle_id)s, %(job_type)s, %(slurm_job_id)s,
            %(array_task_id)s, %(model_id)s, %(status)s, %(stage)s, %(submitted_at)s,
            %(started_at)s, %(finished_at)s, %(exit_code)s, %(retry_count)s,
            %(error_code)s, %(error_message)s, %(log_uri)s, %(created_at)s, %(updated_at)s
        )
        """,
        {
            **job,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def _update_job(
    cursor: Any,
    job: Mapping[str, Any],
    *,
    existing: Mapping[str, Any],
) -> None:
    log_uri = existing.get("log_uri")
    incoming_log = job.get("log_uri")
    if log_uri in (None, "") and incoming_log not in (None, ""):
        log_uri = incoming_log
    elif log_uri not in (None, "") and incoming_log not in (None, "") and incoming_log != log_uri:
        raise PipelineJobProvenanceError(
            "LOG_URI_CONFLICT",
            "Existing pipeline_job log_uri conflicts with incoming provenance.",
            {"job_id": job["job_id"]},
        )
    updated_at = job.get("updated_at") or _format_utc(_job_source_timestamp(job))
    cursor.execute(
        """
        UPDATE ops.pipeline_job
        SET
            slurm_job_id = %(slurm_job_id)s,
            array_task_id = %(array_task_id)s,
            status = %(status)s,
            submitted_at = %(submitted_at)s,
            started_at = %(started_at)s,
            finished_at = %(finished_at)s,
            exit_code = %(exit_code)s,
            retry_count = %(retry_count)s,
            error_code = %(error_code)s,
            error_message = %(error_message)s,
            log_uri = %(log_uri)s,
            updated_at = %(updated_at)s
        WHERE job_id = %(job_id)s
        """,
        {
            **job,
            "log_uri": log_uri,
            "updated_at": updated_at,
        },
    )


def _existing_source_version(existing: Mapping[str, Any]) -> datetime | None:
    return _source_timestamp_or_none(existing)


def _job_source_timestamp(job: Mapping[str, Any]) -> datetime:
    timestamp = _source_timestamp_or_none(job)
    if timestamp is None:
        raise PipelineJobProvenanceError(
            "SOURCE_VERSION_UNKNOWN",
            "Provenance job has no source-owned timestamp for monotonic ordering.",
            {"job_id": job.get("job_id")},
        )
    return timestamp


def _source_timestamp_or_none(job: Mapping[str, Any]) -> datetime | None:
    timestamps = [
        parsed
        for field in ("updated_at", "finished_at", "started_at", "submitted_at", "created_at")
        if (parsed := _timestamp_as_utc(job.get(field))) is not None
    ]
    return max(timestamps) if timestamps else None


def _timestamp_as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_cycle_time(value)
    except (TypeError, ValueError):
        return None

def _normalize_comparable(value: Any) -> Any:
    """Normalize database datetimes and wire-format timestamps before equality."""

    timestamp = _timestamp_as_utc(value)
    return _format_utc(timestamp) if timestamp is not None else value


def _job_payload_equal(
    existing: Mapping[str, Any],
    incoming: Mapping[str, Any],
    *,
    compare_log_uri: bool = True,
) -> bool:
    comparable_fields = (
        "run_id",
        "cycle_id",
        "job_type",
        "slurm_job_id",
        "array_task_id",
        "model_id",
        "status",
        "stage",
        "submitted_at",
        "started_at",
        "finished_at",
        "exit_code",
        "retry_count",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
    )
    for field in comparable_fields:
        if _normalize_comparable(existing.get(field)) != _normalize_comparable(incoming.get(field)):
            return False
    return (
        not compare_log_uri
        or _normalize_comparable(existing.get("log_uri")) == _normalize_comparable(incoming.get("log_uri"))
    )


def _is_canonical_published_log_uri(value: str, *, identity: Mapping[str, str], job_id: str) -> bool:
    if value in REDACTION_SENTINELS:
        return False
    parsed = urlsplit(value)
    if parsed.scheme != "published" or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return False
    try:
        relative = published_log_relative_path(value)
    except Exception:
        return False
    cycle_stamp = format_cycle_time(parse_cycle_time(identity["cycle_time"]))
    expected_out = Path("logs") / identity["source"] / cycle_stamp / identity["run_id"] / f"{job_id}.out"
    expected_err = Path("logs") / identity["source"] / cycle_stamp / identity["run_id"] / f"{job_id}.err"
    return relative == expected_out or relative == expected_err


def _require_safe_identity(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_IDENTITY_CHARS
        or not _SAFE_IDENTITY_RE.fullmatch(value)
    ):
        raise PipelineJobProvenanceError(
            "IDENTITY_INVALID",
            f"{field} is missing or not a safe identity.",
            {"field": field},
        )
    return value


def _optional_bounded_text(
    value: Any,
    *,
    field: str,
    required: bool = False,
    max_chars: int = MAX_IDENTITY_CHARS,
) -> str | None:
    if value in (None, ""):
        if required:
            raise PipelineJobProvenanceError(
                "FIELD_REQUIRED",
                f"{field} is required.",
                {"field": field},
            )
        return None
    if not isinstance(value, str) or value in REDACTION_SENTINELS or len(value) > max_chars:
        if required:
            raise PipelineJobProvenanceError(
                "FIELD_INVALID",
                f"{field} is invalid or redacted.",
                {"field": field},
            )
        return None
    return value


def _optional_int(value: Any, *, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise PipelineJobProvenanceError(
            "FIELD_INVALID",
            f"{field} is not an integer.",
            {"field": field},
        ) from error


def _optional_nonnegative_int(value: Any, *, field: str) -> int | None:
    parsed = _optional_int(value, field=field)
    if parsed is not None and parsed < 0:
        raise PipelineJobProvenanceError(
            "FIELD_INVALID",
            f"{field} cannot be negative.",
            {"field": field},
        )
    return parsed


def _optional_bool(value: Any, *, field: str) -> bool | None:
    if value in (None, ""):
        return None
    if not isinstance(value, bool):
        raise PipelineJobProvenanceError(
            "FIELD_INVALID",
            f"{field} is not a boolean.",
            {"field": field},
        )
    return value


def _optional_timestamp(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _format_utc(value)
    if not isinstance(value, str) or value in REDACTION_SENTINELS:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PipelineJobProvenanceError(
            "TIMESTAMP_INVALID",
            f"{field} is not a valid timestamp.",
            {"field": field},
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _format_utc(parsed.astimezone(UTC))


def _require_cycle_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = parse_cycle_time(str(value))
        except (TypeError, ValueError) as error:
            raise PipelineJobProvenanceError(
                "CYCLE_TIME_INVALID",
                "cycle_time is missing or invalid.",
            ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_source(value: Any) -> str:
    try:
        return normalize_source_id(str(value))
    except (TypeError, ValueError) as error:
        raise PipelineJobProvenanceError(
            "SOURCE_INVALID",
            "source is missing or not a supported monitoring source.",
            {"source": value},
        ) from error


def _model_id_from_run_id(run_id: str) -> str | None:
    match = FORECAST_RUN_ID_RE.fullmatch(run_id)
    return match.group(3) if match is not None else None


def _format_utc(value: datetime) -> str:
    utc = value.astimezone(UTC)
    if utc.microsecond:
        return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def _psycopg2_connect(database_url: str, *, fallback_application_name: str) -> Any:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    return psycopg2.connect(
        database_url,
        cursor_factory=RealDictCursor,
        fallback_application_name=fallback_application_name,
    )


def sidecar_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_sidecar_bytes_no_follow(path: Path, *, containment_root: Path) -> bytes:
    """Read a sidecar without following symlinks. Used by importer path checks."""

    stat_no_follow(path, containment_root=containment_root)
    return read_bytes_limited_no_follow(path, max_bytes=MAX_SIDECAR_BYTES, containment_root=containment_root)
