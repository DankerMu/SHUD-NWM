from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from packages.common.redaction import redact_payload
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_no_follow,
)
from packages.common.source_identity import normalize_source_id
from services.artifacts import ArtifactLogError, published_log_relative_path, published_log_uri
from services.orchestrator import chain_runtime_utils
from services.orchestrator.chain_types import (
    DisplayLogPublication,
    DisplayLogPublicationAttempt,
    OrchestratorError,
)

__all__ = (
    "display_log_publication_for_pipeline_job",
    "display_log_publication_for_stage",
    "log_persistence_error",
    "log_uri_for_pipeline_job",
    "log_uri_for_stage",
    "persist_gateway_logs",
    "published_log_path",
    "raise_publish_error_after_durable_update",
    "safe_workspace_read_bytes",
    "safe_workspace_write_bytes",
    "try_publish_log_for_advertise",
    "write_local_stage_log",
    "workspace_path",
    "workspace_relative_parts",
)


def log_uri_for_pipeline_job(
    orchestrator: Any,
    job: Mapping[str, Any],
    *,
    source_id_from_cycle_id: Callable[[object], str | None] = chain_runtime_utils._source_id_from_cycle_id,
    cycle_time_from_cycle_id: Callable[[object], datetime | None] = chain_runtime_utils._cycle_time_from_cycle_id,
) -> str | None:
    if job.get("log_uri"):
        return str(job["log_uri"])
    run_id = job.get("run_id")
    stage = job.get("stage")
    job_id = job.get("job_id")
    if run_id and stage and job_id:
        return orchestrator._log_uri_for_stage(
            source_id=source_id_from_cycle_id(job.get("cycle_id")) or orchestrator.config.source_id,
            cycle_time=cycle_time_from_cycle_id(job.get("cycle_id")),
            run_id=str(run_id),
            job_id=str(job_id),
            stage=str(stage),
        )
    if run_id and stage:
        return orchestrator.object_store.uri_for_key(f"runs/{run_id}/logs/{stage}.log")
    return None


def display_log_publication_for_stage(
    orchestrator: Any,
    *,
    source_id: str,
    cycle_time: datetime | None,
    run_id: str,
    job_id: str,
    stage: str,
    existing_log_uri: str | None = None,
) -> DisplayLogPublication:
    candidate_uri = existing_log_uri or orchestrator._log_uri_for_stage(
        source_id=source_id,
        cycle_time=cycle_time,
        run_id=run_id,
        job_id=job_id,
        stage=stage,
    )
    should_persist_logs = existing_log_uri is None
    advertised_uri = existing_log_uri
    return DisplayLogPublication(
        candidate_uri=candidate_uri,
        advertised_uri=advertised_uri,
        should_persist_logs=should_persist_logs,
    )


def display_log_publication_for_pipeline_job(
    orchestrator: Any,
    job: Mapping[str, Any],
) -> DisplayLogPublication | None:
    candidate_uri = orchestrator._log_uri_for_pipeline_job(job)
    if candidate_uri is None:
        return None
    existing_log_uri = str(job["log_uri"]) if job.get("log_uri") else None
    should_persist_logs = existing_log_uri is None
    advertised_uri = existing_log_uri
    return DisplayLogPublication(
        candidate_uri=candidate_uri,
        advertised_uri=advertised_uri,
        should_persist_logs=should_persist_logs,
    )


def try_publish_log_for_advertise(
    orchestrator: Any,
    slurm_job_id: str,
    publication: DisplayLogPublication,
) -> DisplayLogPublicationAttempt:
    if not publication.should_persist_logs:
        return DisplayLogPublicationAttempt(advertised_uri=publication.advertised_uri)
    try:
        orchestrator._persist_gateway_logs(slurm_job_id, publication.candidate_uri)
    except Exception as exc:
        publish_error = orchestrator._log_persistence_error(publication.candidate_uri, exc)
        return DisplayLogPublicationAttempt(advertised_uri=None, error=publish_error)
    return DisplayLogPublicationAttempt(advertised_uri=publication.candidate_uri)


def log_persistence_error(candidate_uri: str, error: Exception) -> OrchestratorError:
    if isinstance(error, OrchestratorError) and error.error_code == "PUBLISHED_LOG_WRITE_FAILED":
        details = dict(error.details)
        if details.get("log_uri") == candidate_uri:
            return error
    return OrchestratorError(
        "PUBLISHED_LOG_WRITE_FAILED",
        "Failed to publish gateway logs.",
        {"log_uri": candidate_uri},
    )


def raise_publish_error_after_durable_update(attempt: DisplayLogPublicationAttempt | None) -> None:
    if attempt is not None and attempt.error is not None:
        raise attempt.error


def persist_gateway_logs(
    orchestrator: Any,
    slurm_job_id: str,
    log_uri: str,
    *,
    coerce_mapping: Callable[[Any], dict[str, Any]],
    absolute_configured_path: Callable[[Path], Path] = chain_runtime_utils._absolute_configured_path,
    ensure_directory: Callable[..., Path] = ensure_directory_no_follow,
    atomic_write_bytes: Callable[..., Path] = atomic_write_bytes_no_follow,
    safe_filesystem_error_cls: type[Exception] = SafeFilesystemError,
    artifact_log_error_cls: type[Exception] = ArtifactLogError,
) -> None:
    logs = coerce_mapping(orchestrator.slurm_client.fetch_logs(slurm_job_id))
    content = str(logs.get("logs", ""))
    try:
        published_path = orchestrator._published_log_path(log_uri)
        if published_path is None:
            orchestrator.object_store.write_bytes_atomic(log_uri, content.encode("utf-8"))
            return
        published_root = absolute_configured_path(Path(os.environ["NHMS_PUBLISHED_ARTIFACT_ROOT"]))
        try:
            ensure_directory(published_root)
            atomic_write_bytes(
                published_path,
                content.encode("utf-8"),
                containment_root=published_root,
                temp_suffix="part",
            )
        except (OSError, safe_filesystem_error_cls) as exc:
            raise OrchestratorError(
                "PUBLISHED_LOG_WRITE_FAILED",
                "Failed to publish gateway logs.",
                {"log_uri": log_uri},
            ) from exc
    except artifact_log_error_cls as exc:
        raise OrchestratorError(
            "PUBLISHED_LOG_WRITE_FAILED",
            "Failed to publish gateway logs.",
            {"log_uri": log_uri},
        ) from exc


def write_local_stage_log(
    orchestrator: Any,
    log_uri: str,
    payload: Mapping[str, Any],
    *,
    redact_payload_fn: Callable[[Any], Any] = redact_payload,
    absolute_configured_path: Callable[[Path], Path] = chain_runtime_utils._absolute_configured_path,
    ensure_directory: Callable[..., Path] = ensure_directory_no_follow,
    atomic_write_bytes: Callable[..., Path] = atomic_write_bytes_no_follow,
    safe_filesystem_error_cls: type[Exception] = SafeFilesystemError,
) -> str:
    content = json.dumps(redact_payload_fn(dict(payload)), sort_keys=True).encode("utf-8")
    published_path = orchestrator._published_log_path(log_uri)
    if published_path is None:
        orchestrator.object_store.write_bytes_atomic(log_uri, content)
        return log_uri
    published_root = absolute_configured_path(Path(os.environ["NHMS_PUBLISHED_ARTIFACT_ROOT"]))
    try:
        ensure_directory(published_root)
        atomic_write_bytes(
            published_path,
            content,
            containment_root=published_root,
            temp_suffix="part",
        )
    except (OSError, safe_filesystem_error_cls) as exc:
        raise OrchestratorError(
            "PUBLISHED_LOG_WRITE_FAILED",
            "Failed to publish local stage logs.",
            {"log_uri": log_uri},
        ) from exc
    return log_uri


def log_uri_for_stage(
    orchestrator: Any,
    *,
    source_id: str,
    cycle_time: datetime | None,
    run_id: str,
    job_id: str,
    stage: str,
    published_artifact_root_configured: Callable[[], bool] = chain_runtime_utils._published_artifact_root_configured,
    utcnow: Callable[[], datetime],
    log_stream_for_stage: Callable[[str], str] = chain_runtime_utils._log_stream_for_stage,
    normalize_source_id_fn: Callable[[str], str] = normalize_source_id,
    published_log_uri_fn: Callable[..., str] = published_log_uri,
) -> str:
    if published_artifact_root_configured():
        return published_log_uri_fn(
            source=normalize_source_id_fn(source_id),
            cycle_time=cycle_time or utcnow(),
            run_id=run_id,
            job_id=job_id,
            stream=log_stream_for_stage(stage),
        )
    return orchestrator.object_store.uri_for_key(f"runs/{run_id}/logs/{stage}.log")


def published_log_path(
    log_uri: str,
    *,
    absolute_configured_path: Callable[[Path], Path] = chain_runtime_utils._absolute_configured_path,
    published_log_relative_path_fn: Callable[..., Path] = published_log_relative_path,
) -> Path | None:
    published_root = os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT", "").strip()
    if not published_root:
        return None
    prefix = os.getenv("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", "published://").strip() or "published://"
    if not log_uri.startswith(prefix):
        return None
    relative = published_log_relative_path_fn(log_uri, uri_prefix=prefix)
    root = absolute_configured_path(Path(published_root))
    return root / relative


def workspace_path(orchestrator: Any, *parts: str) -> Path:
    workspace_root = Path(orchestrator.config.workspace_root).expanduser().resolve()
    if any(Path(part).is_absolute() or ".." in Path(part).parts for part in parts):
        raise OrchestratorError(
            "WORKSPACE_PATH_ESCAPE",
            "Workspace path components must be relative and must not contain traversal segments.",
            {"parts": list(parts), "workspace_root": str(workspace_root)},
        )
    resolved = workspace_root.joinpath(*parts)
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise OrchestratorError(
            "WORKSPACE_PATH_ESCAPE",
            "Resolved workspace path is outside workspace_root.",
            {"path": str(resolved), "workspace_root": str(workspace_root)},
        ) from exc
    return resolved


def safe_workspace_write_bytes(
    orchestrator: Any,
    path: Path,
    content: bytes,
    *,
    workspace_relative_parts_fn: Callable[[Path, Path], tuple[str, ...]] | None = None,
    ensure_directory: Callable[..., Path] = ensure_directory_no_follow,
    atomic_write_bytes: Callable[..., Path] = atomic_write_bytes_no_follow,
) -> Path:
    workspace_relative_parts_fn = workspace_relative_parts_fn or workspace_relative_parts
    workspace_root = Path(orchestrator.config.workspace_root).expanduser().resolve()
    workspace_relative_parts_fn(path, workspace_root)
    ensure_directory(workspace_root)
    ensure_directory(path.parent, containment_root=workspace_root)
    return atomic_write_bytes(path, content, containment_root=workspace_root, temp_suffix="part")


def safe_workspace_read_bytes(
    orchestrator: Any,
    path: Path,
    *,
    workspace_relative_parts_fn: Callable[[Path, Path], tuple[str, ...]] | None = None,
    read_bytes: Callable[..., bytes] = read_bytes_no_follow,
) -> bytes:
    workspace_relative_parts_fn = workspace_relative_parts_fn or workspace_relative_parts
    workspace_root = Path(orchestrator.config.workspace_root).expanduser().resolve()
    workspace_relative_parts_fn(path, workspace_root)
    return read_bytes(path, containment_root=workspace_root)


def workspace_relative_parts(path: Path, workspace_root: Path) -> tuple[str, ...]:
    try:
        relative = path.relative_to(workspace_root)
    except ValueError as exc:
        raise SafeFilesystemError(f"Path must stay under workspace root: {path}") from exc
    parts = tuple(relative.parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise SafeFilesystemError(f"Unsafe workspace path: {path}")
    return parts
