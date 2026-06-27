from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from errno import EEXIST, EISDIR, ELOOP, ENOTDIR
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol
from urllib.parse import urlparse

from services.orchestrator.production_contract import production_contract_matrix
from services.orchestrator.scheduler_lease import UnsafeSchedulerLockError, _open_lock_parent_directory
from services.orchestrator.scheduler_state import _evidence_safe, _format_utc
from workers.data_adapters.base import cycle_id_for

SCHEDULER_EVIDENCE_SCHEMA_VERSION = "nhms.production_scheduler.pass_evidence.v1"
SCHEDULER_EVIDENCE_CONTRACT_ID = "runtime-evidence-and-operations.scheduler-evidence.v1"
SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE = "m20-production-multibasin-continuous-automation"
SCHEDULER_EVIDENCE_GITHUB_ISSUE = 196
MODEL_RUN_EVIDENCE_SCHEMA_VERSION = "nhms.production_scheduler.model_run_evidence.v1"
MAX_EVIDENCE_BYTES = 5_000_000
UNKNOWN_AFTER_ATTEMPT = "unknown_after_attempt"
_EVIDENCE_JSON_INDENT = 2
_RETAINED_FIELD_SUMMARY_REASON = "evidence_size_limit_exceeded"
_REQUIRED_BOUNDED_EVIDENCE_FIELDS = frozenset(
    (
        "schema_version",
        "pass_id",
        "status",
        "artifact_path",
        "limit",
        "counts",
        "readiness",
        "resolved_runtime_roots",
        "runtime_config",
        "root_preflight",
        "evidence_pre_execution",
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
        "no_mutation_proof",
    )
)
_SUMMARIZABLE_BOUNDED_EVIDENCE_FIELDS = (
    "duplicate_exclusions",
)
_OPTIONAL_MINIMAL_BOUNDED_EVIDENCE_FIELDS = (
    "duplicate_exclusions",
)
_DROPPABLE_BOUNDED_EVIDENCE_FIELDS = (
    "model_discovery",
    "source_cycles",
    "candidates",
    "blocked_candidates",
    "skipped_candidates",
)
_OPTIONAL_BOUNDED_EVIDENCE_DROP_FIELDS = (
    "finished_at",
    "duplicate_exclusions",
    "readiness_interpretation",
    "execution_mode",
)
_DB_FREE_DB_BACKEND_VALUES = frozenset({"postgres", "postgresql", "psycopg", "psycopg2", "pg"})


class SchedulerEvidenceWriteError(OSError):
    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


class SchedulerEvidenceConfig(Protocol):
    cycle_lag_hours: int
    lookback_hours: int
    dry_run: bool
    model_ids: Sequence[str]
    basin_ids: Sequence[str]
    sources: Sequence[str]
    allowed_cycle_hours_utc: Sequence[int]
    source_exclusions: Sequence[Mapping[str, Any]]
    evidence_dir: Path | str
    workspace_root: Path | str
    object_store_root: Path | str
    published_artifact_root: Path | str
    lock_path: Path | str
    runtime_root: Path | str
    temp_root: Path | str
    require_runtime_roots: bool
    service_role: str | None
    continuous: bool
    interval_seconds: float
    max_cycles_per_source: int
    retry_limit: int
    database_url_configured: bool
    scheduler_db_free_required: bool
    scheduler_state_backend: str | None
    scheduler_lock_backend: str | None
    scheduler_registry_backend: str | None
    scheduler_canonical_readiness_backend: str | None
    scheduler_journal_backend: str | None
    scheduler_state_index_backend: str | None
    scheduler_registry_manifest: Path | str | None
    scheduler_canonical_readiness_index: Path | str | None
    scheduler_journal_root: Path | str | None
    scheduler_state_index: Path | str | None


class SchedulerCandidateLike(Protocol):
    candidate_id: str
    source_id: str
    cycle_id: str
    cycle_time_utc: datetime
    model_id: str
    basin_id: str
    basin_version_id: str
    river_network_version_id: str
    segment_count: int | None
    output_segment_count: int | None
    model_package_uri: str
    resource_profile: Mapping[str, Any]
    display_capabilities: Mapping[str, Any]
    frequency_capabilities: Mapping[str, Any]
    horizon: Mapping[str, Any]
    scenario_id: str
    run_id: str
    forcing_version_id: str
    status: str
    reason: str | None
    state_evidence: Mapping[str, Any]


class BoundedEvidencePayloadCallback(Protocol):
    def __call__(
        self,
        payload: Mapping[str, Any],
        *,
        reason: str,
        max_evidence_bytes: int,
    ) -> dict[str, Any]:
        ...


class WriteNewRegularFileCallback(Protocol):
    def __call__(self, artifact_name: str, serialized: str, *, dir_fd: int, artifact_path: Path) -> None:
        ...


class RequireEvidenceArtifactAvailableCallback(Protocol):
    def __call__(self, artifact_name: str, *, dir_fd: int, artifact_path: Path) -> None:
        ...


class ReservationBlockedPayloadCallback(Protocol):
    def __call__(
        self,
        *,
        config: SchedulerEvidenceConfig,
        pass_id: str,
        artifact_path: Path,
        reason: str,
        details: Mapping[str, Any] | None,
        evidence_safe: Callable[[Any], Any],
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class SchedulerEvidenceWriteContext:
    config: SchedulerEvidenceConfig
    require_safe_directory_final_component: Callable[[Path, Path, str], None]
    require_under_workspace: Callable[[Path, Path, str], None]
    evidence_safe: Callable[[Any], Any] = _evidence_safe
    max_evidence_bytes: int = MAX_EVIDENCE_BYTES
    bounded_evidence_payload: BoundedEvidencePayloadCallback | None = None
    open_evidence_directory: Callable[[Path, Path], int] | None = None
    write_new_regular_file: WriteNewRegularFileCallback | None = None
    require_evidence_artifact_available: RequireEvidenceArtifactAvailableCallback | None = None
    reservation_blocked_payload: ReservationBlockedPayloadCallback | None = None
    evidence_write_error_payload: Callable[..., dict[str, Any]] | None = None


def base_evidence(
    config: SchedulerEvidenceConfig,
    pass_id: str,
    started_at: datetime,
    *,
    resolved_runtime_roots: Callable[[SchedulerEvidenceConfig], dict[str, Any]] | None = None,
    runtime_config_evidence: Callable[[SchedulerEvidenceConfig], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    end_time = started_at - timedelta(hours=config.cycle_lag_hours)
    start_time = end_time - timedelta(hours=config.lookback_hours)
    execution_mode = "dry_run" if config.dry_run else "production_orchestration"
    readiness_interpretation = "deterministic_review_only" if config.dry_run else "non_final_scheduler_evidence"
    operator_filters = {
        "model_ids": list(config.model_ids),
        "basin_ids": list(config.basin_ids),
        "expression": filter_expression(config.model_ids, config.basin_ids),
        "excluded_runnable_count": 0,
    }
    return {
        "schema_version": SCHEDULER_EVIDENCE_SCHEMA_VERSION,
        "review_contract": {
            "contract_id": SCHEDULER_EVIDENCE_CONTRACT_ID,
            "github_issue": SCHEDULER_EVIDENCE_GITHUB_ISSUE,
            "openspec_change": SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE,
            "scope": "scheduler_pass_evidence",
        },
        "production_contract": production_contract_matrix(),
        "pass_id": pass_id,
        "started_at": _format_utc(started_at),
        "execution_mode": execution_mode,
        "readiness_interpretation": readiness_interpretation,
        "dry_run": config.dry_run,
        "sources": list(config.sources),
        "duplicate_exclusions": list(config.source_exclusions),
        "cycle_window": {
            "start_time_utc": _format_utc(start_time),
            "end_time_utc": _format_utc(end_time),
            "lookback_hours": config.lookback_hours,
            "cycle_lag_hours": config.cycle_lag_hours,
            "max_cycles_per_source": config.max_cycles_per_source,
        },
        "operator_filters": dict(operator_filters),
        "filters": dict(operator_filters),
        "readiness": {
            "schema_version": "nhms.production_readiness.scheduler_input.v1",
            "interpretation": readiness_interpretation,
            "deterministic_fixture": config.dry_run,
            "scheduler_evidence_accepted_for_review": True,
            "live_receipts": [],
            "production_ready": False,
            "final_production_readiness_claimed": False,
            "can_claim_final_production_readiness": False,
            "reason": "scheduler evidence requires accepted live proof receipts for final readiness",
        },
        "resolved_runtime_roots": resolved_runtime_roots(config)
        if resolved_runtime_roots is not None
        else scheduler_resolved_runtime_roots(config),
        "runtime_config": (
            runtime_config_evidence(config)
            if runtime_config_evidence is not None
            else scheduler_runtime_config_evidence(config)
        ),
    }


def write_prelock_blocked_evidence(
    context: SchedulerEvidenceWriteContext,
    pass_id: str,
    evidence: dict[str, Any],
    root_preflight: Mapping[str, Any],
    *,
    write_evidence_callback: Callable[[str, Mapping[str, Any]], Path | None] | None = None,
) -> Path | None:
    checks = root_preflight.get("checks")
    evidence_check = checks.get("evidence_root") if isinstance(checks, Mapping) else None
    blockers = root_preflight.get("blockers")
    evidence_root_blocked = (
        any(isinstance(blocker, Mapping) and blocker.get("field") == "evidence_root" for blocker in blockers)
        if isinstance(blockers, list)
        else False
    )
    if evidence_root_blocked:
        return None
    if not isinstance(evidence_check, Mapping) or evidence_check.get("writable") is not True:
        return None
    try:
        if write_evidence_callback is not None:
            return write_evidence_callback(pass_id, evidence)
        return write_evidence(context, pass_id, evidence)
    except SchedulerEvidenceWriteError as error:
        evidence["evidence_write_error"] = _call_evidence_write_error_payload(context, error)
        return None
    except (OSError, RuntimeError, ValueError) as error:
        evidence["evidence_write_error"] = _call_evidence_write_error_payload(context, error)
        return None


def reserve_pre_execution_evidence(
    context: SchedulerEvidenceWriteContext,
    pass_id: str,
    started_at: datetime,
    candidate_count: int,
    *,
    now: datetime,
) -> dict[str, Any]:
    evidence_dir = Path(context.config.evidence_dir)
    workspace_root = Path(context.config.workspace_root)
    artifact_name = f"{pass_id}.pre_execution.json"
    final_artifact_name = f"{pass_id}.json"
    artifact_path = evidence_dir / artifact_name
    payload = {
        "schema_version": "nhms.production_scheduler.pre_execution_evidence_reservation.v1",
        "pass_id": pass_id,
        "started_at": _format_utc(started_at),
        "reserved_at": _format_utc(now),
        "status": "reserved",
        "candidate_count": candidate_count,
        "artifact_path": artifact_path_evidence(context.config, artifact_path),
        "final_evidence_artifact": artifact_path_evidence(context.config, evidence_dir / final_artifact_name),
        "proof": "scheduler_evidence_directory_write_before_production_mutation",
    }
    try:
        validate_evidence_artifact_name(artifact_name, artifact_path=artifact_path)
        validate_evidence_artifact_name(final_artifact_name, artifact_path=evidence_dir / final_artifact_name)
        context.require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
        context.require_under_workspace(artifact_path.parent.resolve(), workspace_root, "evidence_dir")
        serialized = _serialize_evidence_json(context.evidence_safe(payload))
        evidence_dir_fd = _call_open_evidence_directory(context, evidence_dir, workspace_root)
        try:
            _call_require_evidence_artifact_available(
                context,
                final_artifact_name,
                evidence_dir_fd,
                evidence_dir / final_artifact_name,
            )
            _call_write_new_regular_file(context, artifact_name, serialized, evidence_dir_fd, artifact_path)
        finally:
            os.close(evidence_dir_fd)
    except SchedulerEvidenceWriteError as error:
        return _call_reservation_blocked_payload(
            context,
            pass_id,
            artifact_path,
            error.reason,
            error.details,
        )
    except OSError as error:
        details = (
            {"error_type": type(error).__name__}
            if bool(getattr(context.config, "scheduler_db_free_required", False))
            else {"error": str(error)}
        )
        return _call_reservation_blocked_payload(
            context,
            pass_id,
            artifact_path,
            "evidence_write_failed",
            details,
        )
    return payload


def write_evidence(
    context: SchedulerEvidenceWriteContext,
    pass_id: str,
    evidence: Mapping[str, Any],
) -> Path | None:
    evidence_dir = Path(context.config.evidence_dir)
    workspace_root = Path(context.config.workspace_root)
    artifact_name = f"{pass_id}.json"
    artifact_path = evidence_dir / artifact_name
    validate_evidence_artifact_name(artifact_name, artifact_path=artifact_path)
    context.require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
    context.require_under_workspace(artifact_path.parent.resolve(), workspace_root, "evidence_dir")
    payload = context.evidence_safe(dict(evidence))
    if not isinstance(payload, dict):
        payload = {}
    payload["artifact_path"] = artifact_path_evidence(context.config, artifact_path)
    payload_to_write, serialized = _serialized_evidence_within_limit(
        context,
        payload,
        artifact_path=artifact_path,
    )
    evidence_dir_fd = _call_open_evidence_directory(context, evidence_dir, workspace_root)
    try:
        _call_write_new_regular_file(context, artifact_name, serialized, evidence_dir_fd, artifact_path)
    finally:
        os.close(evidence_dir_fd)
    if isinstance(evidence, dict):
        evidence.clear()
        evidence.update(payload_to_write)
        evidence.setdefault("artifact_path", artifact_path_evidence(context.config, artifact_path))
    return artifact_path


def artifact_path_evidence(config: SchedulerEvidenceConfig, artifact_path: Path) -> str:
    if bool(getattr(config, "scheduler_db_free_required", False)):
        return "[local-path]"
    return str(artifact_path)


def validate_evidence_artifact_name(artifact_name: str, *, artifact_path: Path) -> None:
    path_name = Path(artifact_name)
    windows_path_name = PureWindowsPath(artifact_name)
    if (
        not artifact_name
        or artifact_name in {".", ".."}
        or "/" in artifact_name
        or "\\" in artifact_name
        or (os.altsep is not None and os.altsep in artifact_name)
        or path_name.is_absolute()
        or windows_path_name.is_absolute()
        or bool(windows_path_name.drive)
        or path_name.name != artifact_name
        or windows_path_name.name != artifact_name
    ):
        raise SchedulerEvidenceWriteError(
            "unsafe_evidence_artifact",
            {"artifact_path": str(artifact_path)},
        )


from services.orchestrator.scheduler_evidence_payload import (  # noqa: E402, F401, I001
    _bounded_retained_field_summary,
    _call_bounded_evidence_payload,
    _compact_counts,
    _compact_limit,
    _compact_mapping,
    _compact_required_bounded_field,
    _compact_required_bounded_fields,
    _compact_resolved_runtime_roots,
    _compact_retained_bounded_field,
    _compact_review_contract,
    _compact_root_preflight,
    _drop_empty_optional_bounded_fields,
    _drop_not_required_optional_proofs,
    _fit_bounded_evidence_payload,
    _is_required_bounded_field,
    _mapping_status,
    _minimal_bounded_retained_field_summary,
    _payload_fits,
    _serialized_evidence_within_limit,
    _serialize_evidence_json,
    _serialize_evidence_json_if_within_limit,
    bounded_evidence_payload,
)


def _call_open_evidence_directory(
    context: SchedulerEvidenceWriteContext,
    evidence_dir: Path,
    workspace_root: Path,
) -> int:
    if context.open_evidence_directory is not None:
        return context.open_evidence_directory(evidence_dir, workspace_root)
    return open_evidence_directory(evidence_dir, workspace_root)


def _call_write_new_regular_file(
    context: SchedulerEvidenceWriteContext,
    artifact_name: str,
    serialized: str,
    dir_fd: int,
    artifact_path: Path,
) -> None:
    validate_evidence_artifact_name(artifact_name, artifact_path=artifact_path)
    if context.write_new_regular_file is not None:
        context.write_new_regular_file(
            artifact_name,
            serialized,
            dir_fd=dir_fd,
            artifact_path=artifact_path,
        )
        return
    write_new_regular_file(artifact_name, serialized, dir_fd=dir_fd, artifact_path=artifact_path)


def _call_require_evidence_artifact_available(
    context: SchedulerEvidenceWriteContext,
    artifact_name: str,
    dir_fd: int,
    artifact_path: Path,
) -> None:
    validate_evidence_artifact_name(artifact_name, artifact_path=artifact_path)
    if context.require_evidence_artifact_available is not None:
        context.require_evidence_artifact_available(
            artifact_name,
            dir_fd=dir_fd,
            artifact_path=artifact_path,
        )
        return
    require_evidence_artifact_available(artifact_name, dir_fd=dir_fd, artifact_path=artifact_path)


def _call_reservation_blocked_payload(
    context: SchedulerEvidenceWriteContext,
    pass_id: str,
    artifact_path: Path,
    reason: str,
    details: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if context.reservation_blocked_payload is not None:
        return context.reservation_blocked_payload(
            config=context.config,
            pass_id=pass_id,
            artifact_path=artifact_path,
            reason=reason,
            details=details,
            evidence_safe=context.evidence_safe,
        )
    return evidence_reservation_blocked_payload(
        config=context.config,
        pass_id=pass_id,
        artifact_path=artifact_path,
        reason=reason,
        details=details,
        evidence_safe=context.evidence_safe,
    )


def _call_evidence_write_error_payload(context: SchedulerEvidenceWriteContext, error: OSError) -> dict[str, Any]:
    if context.evidence_write_error_payload is not None:
        try:
            return context.evidence_write_error_payload(error, context.config)
        except TypeError:
            return context.evidence_write_error_payload(error)
    return evidence_write_error_payload(error, context.config)


def candidate_evidence_write_blocked_evidence(
    candidate: SchedulerCandidateLike,
    reservation: Mapping[str, Any],
    *,
    candidate_model_run_review_evidence: Callable[..., dict[str, Any]],
    candidate_identity_evidence: Callable[[SchedulerCandidateLike], dict[str, Any]],
    standard_chain_shape: Sequence[str],
    evidence_safe: Callable[[Any], Any] = _evidence_safe,
) -> dict[str, Any]:
    blocker = {
        "code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "state": "blocked",
        "quality_flag": "evidence_preflight_blocked",
        "residual_risk": "Scheduler evidence write proof failed before production mutation.",
    }
    reason = reservation.get("reason")
    if reason not in (None, ""):
        blocker["reason"] = str(reason)
    return {
        **candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "evidence_preflight",
        "evidence_pre_execution": evidence_safe(dict(reservation)),
        "error_code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "error_message": "Scheduler evidence write proof failed before production mutation.",
        "standard_chain_shape": list(standard_chain_shape),
        "qhh_script_invoked": False,
        "residual_blockers": [blocker],
    }


def cancel_candidate_evidence_write_blocked_evidence(
    candidate: Mapping[str, Any],
    reservation: Mapping[str, Any],
    *,
    ensure_utc: Callable[[datetime], datetime],
    evidence_safe: Callable[[Any], Any] = _evidence_safe,
) -> dict[str, Any]:
    reason = reservation.get("reason")
    blocker = {
        "code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "state": "blocked",
        "quality_flag": "evidence_preflight_blocked",
        "residual_risk": "Scheduler evidence write proof failed before Slurm cancellation mutation.",
    }
    if reason not in (None, ""):
        blocker["reason"] = str(reason)
    source_id = str(candidate.get("source_id") or "")
    cycle_time_text = str(candidate.get("cycle_time_utc") or "")
    item: dict[str, Any] = {
        "source_id": source_id,
        "cycle_time_utc": cycle_time_text,
        "status": "preflight_blocked",
        "error_code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "error_message": "Scheduler evidence write proof failed before Slurm cancellation mutation.",
        "replacement_submitted": False,
        "mutation_occurred": False,
        "cancel_attempted": False,
        "evidence_pre_execution": evidence_safe(dict(reservation)),
        "active_slurm_jobs": evidence_safe(candidate.get("active_slurm_jobs", [])),
        "residual_blockers": [blocker],
    }
    if source_id and cycle_time_text:
        cycle_time = ensure_utc(datetime.fromisoformat(cycle_time_text.replace("Z", "+00:00")))
        item["cycle_id"] = cycle_id_for(source_id, cycle_time)
    return item


def sync_candidate_evidence_write_blocked_evidence(
    candidate: Mapping[str, Any],
    reservation: Mapping[str, Any],
    *,
    standard_chain_shape: Sequence[str],
    evidence_safe: Callable[[Any], Any] = _evidence_safe,
) -> dict[str, Any]:
    reason = reservation.get("reason")
    blocker = {
        "code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "state": "blocked",
        "quality_flag": "evidence_preflight_blocked",
        "residual_risk": "Scheduler evidence write proof failed before Slurm status sync mutation.",
    }
    if reason not in (None, ""):
        blocker["reason"] = str(reason)
    item = {
        **dict(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "evidence_preflight",
        "evidence_pre_execution": evidence_safe(dict(reservation)),
        "error_code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "error_message": "Scheduler evidence write proof failed before Slurm status sync mutation.",
        "sync_attempted": False,
        "standard_chain_shape": list(standard_chain_shape),
        "qhh_script_invoked": False,
        "residual_blockers": [blocker],
    }
    return evidence_safe(item)


from services.orchestrator.scheduler_evidence_proofs import (  # noqa: E402, F401, I001
    empty_counts,
    execution_mutation_value,
    execution_write_proof,
    execution_write_proof_from_evidence,
    merge_proof_values,
    named_proof_value,
    no_mutation_proof,
    pipeline_event_write_proof_value,
    pipeline_status_write_proof_value,
    positive_count,
    proof_mutation_value,
    scheduler_execution_boundary_from_cancellation,
    scheduler_mutation_proof,
    scheduler_pass_status_from_cancellation,
    slurm_cancellation_blocked_count,
    slurm_cancellation_proof,
    slurm_cancellation_proof_from_evidence,
    slurm_cancellation_unknown_count,
    slurm_cancelled_count,
    slurm_status_sync_count,
    slurm_status_sync_failed,
    slurm_status_sync_mutated,
    slurm_status_sync_proof,
    slurm_status_sync_proof_from_candidates,
    slurm_status_sync_unknown_count,
    slurm_submit_proof_value,
)


def evidence_reservation_blocked_payload(
    *,
    config: SchedulerEvidenceConfig,
    pass_id: str,
    artifact_path: Path,
    reason: str,
    details: Mapping[str, Any] | None = None,
    evidence_safe: Callable[[Any], Any] = _evidence_safe,
) -> dict[str, Any]:
    payload = {
        "schema_version": "nhms.production_scheduler.pre_execution_evidence_reservation.v1",
        "pass_id": pass_id,
        "status": "blocked",
        "artifact_path": artifact_path_evidence(config, artifact_path),
        "reason": reason,
        "error_code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "message": "Scheduler evidence write proof failed before production mutation.",
    }
    safe_details = dict(details or {})
    if "artifact_path" in safe_details:
        safe_details["artifact_path"] = artifact_path_evidence(config, artifact_path)
    payload.update(safe_details)
    return evidence_safe(payload)


def evidence_write_error_payload(
    error: OSError,
    config: SchedulerEvidenceConfig | None = None,
) -> dict[str, Any]:
    if isinstance(error, SchedulerEvidenceWriteError):
        details = dict(error.details)
        artifact_path = details.get("artifact_path")
        if config is not None and artifact_path not in (None, ""):
            details["artifact_path"] = artifact_path_evidence(config, Path(str(artifact_path)))
        return {"reason": error.reason, **details}
    if config is not None and bool(getattr(config, "scheduler_db_free_required", False)):
        return {"reason": "evidence_write_failed", "error_type": type(error).__name__}
    return {"reason": "evidence_write_failed", "error": str(error)}


def scheduler_resolved_runtime_roots(config: SchedulerEvidenceConfig) -> dict[str, Any]:
    evidence_safe_paths = bool(getattr(config, "scheduler_db_free_required", False))
    return {
        "workspace_root": root_evidence_item(
            config.workspace_root,
            env="WORKSPACE_ROOT",
            required=config.require_runtime_roots,
            evidence_safe_paths=evidence_safe_paths,
        ),
        "object_store_root": root_evidence_item(
            config.object_store_root,
            env="OBJECT_STORE_ROOT",
            required=config.require_runtime_roots,
            evidence_safe_paths=evidence_safe_paths,
        ),
        "published_artifact_root": root_evidence_item(
            config.published_artifact_root,
            env="NHMS_PUBLISHED_ARTIFACT_ROOT",
            required=config.require_runtime_roots,
            evidence_safe_paths=evidence_safe_paths,
        ),
        "lock_root": root_evidence_item(
            Path(config.lock_path).parent,
            env="NHMS_SCHEDULER_LOCK_ROOT",
            fallback="WORKSPACE_ROOT/scheduler",
            required=config.require_runtime_roots,
            evidence_safe_paths=evidence_safe_paths,
        ),
        "lock_path": root_evidence_item(
            config.lock_path,
            env="NHMS_SCHEDULER_LOCK_ROOT",
            fallback="WORKSPACE_ROOT/scheduler/production-scheduler.lock",
            required=config.require_runtime_roots,
            evidence_safe_paths=evidence_safe_paths,
        ),
        "evidence_root": root_evidence_item(
            config.evidence_dir,
            env="NHMS_SCHEDULER_EVIDENCE_ROOT",
            fallback="WORKSPACE_ROOT/scheduler/evidence",
            required=config.require_runtime_roots,
            evidence_safe_paths=evidence_safe_paths,
        ),
        "runtime_root": root_evidence_item(
            config.runtime_root,
            env="NHMS_SCHEDULER_RUNTIME_ROOT|NHMS_RUNTIME_ROOT|RUN_WORKSPACE_ROOT|SHUD_RUNTIME_ROOT",
            required=config.require_runtime_roots,
            evidence_safe_paths=evidence_safe_paths,
        ),
        "temp_root": root_evidence_item(
            config.temp_root,
            env="NHMS_SCHEDULER_TEMP_ROOT|NHMS_TEMP_ROOT|TMPDIR",
            required=config.require_runtime_roots,
            evidence_safe_paths=evidence_safe_paths,
        ),
    }


def root_evidence_item(
    value: Path | str | None,
    *,
    env: str,
    required: bool,
    fallback: str | None = None,
    evidence_safe_paths: bool = False,
) -> dict[str, Any]:
    if value in (None, ""):
        path = None
    elif evidence_safe_paths:
        path = "[local-path]"
    else:
        path = str(Path(value).expanduser().resolve(strict=False))
    payload = {
        "path": path,
        "configured": path is not None,
        "env": env,
        "required": required,
    }
    if fallback is not None:
        payload["fallback"] = fallback
    return payload


def scheduler_runtime_config_evidence(config: SchedulerEvidenceConfig) -> dict[str, Any]:
    db_free_required = bool(getattr(config, "scheduler_db_free_required", False))
    payload = {
        "service_role": config.service_role,
        "require_runtime_roots": config.require_runtime_roots,
        "database_url_configured": bool(getattr(config, "database_url_configured", False)),
        "scheduler_db_free_required": db_free_required,
        "scheduler_state_backend": _scheduler_backend_evidence(
            getattr(config, "scheduler_state_backend", None), db_free_required=db_free_required
        ),
        "scheduler_lock_backend": _scheduler_backend_evidence(
            getattr(config, "scheduler_lock_backend", None), db_free_required=db_free_required
        ),
        "scheduler_registry_backend": _scheduler_backend_evidence(
            getattr(config, "scheduler_registry_backend", None), db_free_required=db_free_required
        ),
        "scheduler_canonical_readiness_backend": _scheduler_backend_evidence(
            getattr(config, "scheduler_canonical_readiness_backend", None), db_free_required=db_free_required
        ),
        "scheduler_journal_backend": _scheduler_backend_evidence(
            getattr(config, "scheduler_journal_backend", None), db_free_required=db_free_required
        ),
        "scheduler_state_index_backend": _scheduler_backend_evidence(
            getattr(config, "scheduler_state_index_backend", None), db_free_required=db_free_required
        ),
        "dry_run": config.dry_run,
        "continuous": config.continuous,
        "interval_seconds": config.interval_seconds,
        "sources": list(config.sources),
        "allowed_cycle_hours_utc": list(config.allowed_cycle_hours_utc),
        "model_ids": list(config.model_ids),
        "basin_ids": list(config.basin_ids),
        "lookback_hours": config.lookback_hours,
        "cycle_lag_hours": config.cycle_lag_hours,
        "max_cycles_per_source": config.max_cycles_per_source,
        "retry_limit": config.retry_limit,
    }
    db_free_evidence = getattr(config, "db_free_runtime_evidence", None)
    if callable(db_free_evidence):
        payload["db_free_runtime"] = db_free_evidence()
    return payload


def _scheduler_backend_evidence(value: Any, *, db_free_required: bool) -> Any:
    if value in (None, ""):
        return value
    text = str(value).strip()
    if db_free_required and text == "file":
        return "file"
    try:
        parsed = urlparse(text)
    except ValueError:
        if db_free_required:
            return "[invalid-uri]"
        return "[invalid-uri]" if ":" in text else text
    if parsed.scheme:
        scheme = parsed.scheme.lower()
        if scheme in _DB_FREE_DB_BACKEND_VALUES or "postgres" in scheme or "psycopg" in scheme:
            return "[db-like]"
        return "[uri]"
    lower = text.lower()
    if lower in _DB_FREE_DB_BACKEND_VALUES or "postgres" in lower or "psycopg" in lower:
        return "[db-like]"
    if db_free_required:
        return "[non-file]"
    return text


def open_evidence_directory(evidence_dir: Path, workspace_root: Path) -> int:
    try:
        return _open_lock_parent_directory(evidence_dir, workspace_root)
    except UnsafeSchedulerLockError as error:
        raise SchedulerEvidenceWriteError("unsafe_evidence_directory") from error


def write_new_regular_file(
    artifact_name: str,
    serialized: str,
    *,
    dir_fd: int,
    artifact_path: Path,
) -> None:
    validate_evidence_artifact_name(artifact_name, artifact_path=artifact_path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(artifact_name, flags, 0o644, dir_fd=dir_fd)
    except FileExistsError as error:
        try:
            artifact_stat = os.stat(artifact_name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            artifact_stat = None
        reason = (
            "evidence_artifact_exists"
            if artifact_stat is not None and stat.S_ISREG(artifact_stat.st_mode)
            else "unsafe_evidence_artifact"
        )
        raise SchedulerEvidenceWriteError(
            reason,
            {"artifact_path": str(artifact_path)},
        ) from error
    except OSError as error:
        if error.errno in {EEXIST, EISDIR, ELOOP, ENOTDIR}:
            raise SchedulerEvidenceWriteError(
                "unsafe_evidence_artifact",
                {"artifact_path": str(artifact_path)},
            ) from error
        raise
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
    with handle:
        handle.write(serialized)


def require_evidence_artifact_available(
    artifact_name: str,
    *,
    dir_fd: int,
    artifact_path: Path,
) -> None:
    validate_evidence_artifact_name(artifact_name, artifact_path=artifact_path)
    try:
        artifact_stat = os.stat(artifact_name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno in {EISDIR, ELOOP, ENOTDIR}:
            raise SchedulerEvidenceWriteError(
                "unsafe_evidence_artifact",
                {"artifact_path": str(artifact_path)},
            ) from error
        raise
    reason = "evidence_artifact_exists" if stat.S_ISREG(artifact_stat.st_mode) else "unsafe_evidence_artifact"
    raise SchedulerEvidenceWriteError(reason, {"artifact_path": str(artifact_path)})


def evidence_status(evidence: Mapping[str, Any], fallback: str) -> str:
    status = evidence.get("status")
    return str(status) if status not in (None, "") else fallback


def empty_model_discovery() -> dict[str, Any]:
    return {
        "active_model_count": 0,
        "runnable_model_count": 0,
        "selected_model_count": 0,
        "excluded_model_count": 0,
        "models": [],
        "exclusions": [],
        "operator_filters": {"expression": None, "excluded_runnable_count": 0},
    }


def filter_expression(model_ids: Sequence[str], basin_ids: Sequence[str]) -> str | None:
    parts: list[str] = []
    if model_ids:
        parts.append("model_id in [" + ",".join(model_ids) + "]")
    if basin_ids:
        parts.append("basin_id in [" + ",".join(basin_ids) + "]")
    return " and ".join(parts) if parts else None


__all__ = [
    "MAX_EVIDENCE_BYTES",
    "MODEL_RUN_EVIDENCE_SCHEMA_VERSION",
    "SCHEDULER_EVIDENCE_CONTRACT_ID",
    "SCHEDULER_EVIDENCE_GITHUB_ISSUE",
    "SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE",
    "SCHEDULER_EVIDENCE_SCHEMA_VERSION",
    "UNKNOWN_AFTER_ATTEMPT",
    "SchedulerEvidenceWriteContext",
    "SchedulerEvidenceWriteError",
    "base_evidence",
    "bounded_evidence_payload",
    "cancel_candidate_evidence_write_blocked_evidence",
    "candidate_evidence_write_blocked_evidence",
    "empty_counts",
    "empty_model_discovery",
    "evidence_reservation_blocked_payload",
    "evidence_status",
    "evidence_write_error_payload",
    "execution_mutation_value",
    "execution_write_proof",
    "execution_write_proof_from_evidence",
    "filter_expression",
    "merge_proof_values",
    "named_proof_value",
    "no_mutation_proof",
    "open_evidence_directory",
    "pipeline_event_write_proof_value",
    "pipeline_status_write_proof_value",
    "positive_count",
    "proof_mutation_value",
    "require_evidence_artifact_available",
    "reserve_pre_execution_evidence",
    "root_evidence_item",
    "scheduler_execution_boundary_from_cancellation",
    "scheduler_mutation_proof",
    "scheduler_pass_status_from_cancellation",
    "scheduler_resolved_runtime_roots",
    "scheduler_runtime_config_evidence",
    "slurm_cancellation_blocked_count",
    "slurm_cancellation_proof",
    "slurm_cancellation_proof_from_evidence",
    "slurm_cancellation_unknown_count",
    "slurm_cancelled_count",
    "slurm_status_sync_count",
    "slurm_status_sync_failed",
    "slurm_status_sync_mutated",
    "slurm_status_sync_proof",
    "slurm_status_sync_proof_from_candidates",
    "slurm_status_sync_unknown_count",
    "sync_candidate_evidence_write_blocked_evidence",
    "write_evidence",
    "write_new_regular_file",
    "write_prelock_blocked_evidence",
]
