from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from errno import EEXIST, EISDIR, ELOOP, ENOTDIR
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

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
    evidence_write_error_payload: Callable[[OSError], dict[str, Any]] | None = None


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
    if not isinstance(evidence_check, Mapping) or evidence_check.get("writable") is not True:
        return None
    try:
        if write_evidence_callback is not None:
            return write_evidence_callback(pass_id, evidence)
        return write_evidence(context, pass_id, evidence)
    except SchedulerEvidenceWriteError as error:
        evidence["evidence_write_error"] = {"reason": error.reason, **error.details}
        return None
    except OSError as error:
        evidence["evidence_write_error"] = {"reason": "evidence_write_failed", "error": str(error)}
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
        "artifact_path": str(artifact_path),
        "final_evidence_artifact": str(evidence_dir / final_artifact_name),
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
        return _call_reservation_blocked_payload(
            context,
            pass_id,
            artifact_path,
            "evidence_write_failed",
            {"error": str(error)},
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
    payload["artifact_path"] = str(artifact_path)
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
        evidence.setdefault("artifact_path", str(artifact_path))
    return artifact_path


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


def _serialize_evidence_json(payload: Any, *, compact: bool = False) -> str:
    if compact:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return json.dumps(payload, indent=_EVIDENCE_JSON_INDENT, sort_keys=True)


def _serialize_evidence_json_if_within_limit(
    payload: Any,
    *,
    max_evidence_bytes: int,
    compact: bool = False,
) -> str | None:
    if compact:
        encoder = json.JSONEncoder(separators=(",", ":"), sort_keys=True)
    else:
        encoder = json.JSONEncoder(indent=_EVIDENCE_JSON_INDENT, sort_keys=True)
    chunks: list[str] = []
    serialized_bytes = 0
    for chunk in encoder.iterencode(payload):
        serialized_bytes += len(chunk.encode("utf-8"))
        if serialized_bytes > max_evidence_bytes:
            return None
        chunks.append(chunk)
    return "".join(chunks)


def _payload_fits(payload: Mapping[str, Any], *, max_evidence_bytes: int, compact: bool = False) -> bool:
    return (
        _serialize_evidence_json_if_within_limit(
            payload,
            max_evidence_bytes=max_evidence_bytes,
            compact=compact,
        )
        is not None
    )


def _serialized_evidence_within_limit(
    context: SchedulerEvidenceWriteContext,
    payload: dict[str, Any],
    *,
    artifact_path: Path,
) -> tuple[dict[str, Any], str]:
    serialized = _serialize_evidence_json_if_within_limit(
        payload,
        max_evidence_bytes=context.max_evidence_bytes,
    )
    if serialized is not None:
        return payload, serialized

    bounded_payload = _call_bounded_evidence_payload(context, payload, reason="evidence_size_limit_exceeded")
    bounded_payload = _fit_bounded_evidence_payload(
        bounded_payload,
        max_evidence_bytes=context.max_evidence_bytes,
    )
    serialized = _serialize_evidence_json_if_within_limit(
        bounded_payload,
        max_evidence_bytes=context.max_evidence_bytes,
    )
    if serialized is not None:
        return bounded_payload, serialized
    serialized = _serialize_evidence_json_if_within_limit(
        bounded_payload,
        max_evidence_bytes=context.max_evidence_bytes,
        compact=True,
    )
    if serialized is not None:
        return bounded_payload, serialized

    raise SchedulerEvidenceWriteError(
        "evidence_size_limit_exceeded",
        {
            "artifact_path": str(artifact_path),
            "max_evidence_bytes": context.max_evidence_bytes,
        },
    )


def _fit_bounded_evidence_payload(
    payload: Mapping[str, Any],
    *,
    max_evidence_bytes: int,
) -> dict[str, Any]:
    bounded_payload = dict(payload)
    if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
        return bounded_payload

    _compact_required_bounded_fields(bounded_payload)
    if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
        return bounded_payload

    for field_name in _DROPPABLE_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        bounded_payload[field_name] = {} if field_name == "model_discovery" else []
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name, compactor in (
        ("counts", _compact_counts),
        ("review_contract", _compact_review_contract),
    ):
        if field_name not in bounded_payload:
            continue
        bounded_payload[field_name] = compactor(bounded_payload[field_name])
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _SUMMARIZABLE_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        if _is_required_bounded_field(bounded_payload, field_name):
            continue
        bounded_payload[field_name] = _compact_retained_bounded_field(field_name, bounded_payload[field_name])
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    _drop_empty_optional_bounded_fields(bounded_payload)
    if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
        return bounded_payload

    _drop_not_required_optional_proofs(bounded_payload)
    if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
        return bounded_payload

    for field_name in _OPTIONAL_MINIMAL_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        if _is_required_bounded_field(bounded_payload, field_name):
            continue
        bounded_payload[field_name] = _bounded_retained_field_summary(field_name, bounded_payload[field_name])
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _OPTIONAL_MINIMAL_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        if _is_required_bounded_field(bounded_payload, field_name):
            continue
        bounded_payload[field_name] = _minimal_bounded_retained_field_summary()
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _DROPPABLE_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        bounded_payload.pop(field_name)
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _OPTIONAL_BOUNDED_EVIDENCE_DROP_FIELDS:
        if field_name not in bounded_payload:
            continue
        bounded_payload.pop(field_name)
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    if "limit" in bounded_payload:
        bounded_payload["limit"] = _compact_limit(bounded_payload["limit"])
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    return bounded_payload


def _compact_required_bounded_fields(payload: dict[str, Any]) -> None:
    for field_name in _REQUIRED_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in payload:
            continue
        if field_name == "counts":
            payload[field_name] = _compact_counts(payload[field_name])
        elif field_name not in {"schema_version", "pass_id", "status", "artifact_path", "limit"}:
            payload[field_name] = _compact_required_bounded_field(field_name, payload[field_name])


def _is_required_bounded_field(payload: Mapping[str, Any], field_name: str) -> bool:
    return field_name in _REQUIRED_BOUNDED_EVIDENCE_FIELDS and field_name in payload


def _compact_required_bounded_field(field_name: str, value: Any) -> Any:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        return value
    if field_name == "resolved_runtime_roots":
        return _compact_resolved_runtime_roots(value)
    if field_name == "runtime_config":
        return _compact_mapping(
            value,
            (
                "service_role",
                "require_runtime_roots",
                "dry_run",
            ),
        )
    if field_name == "root_preflight":
        return _compact_root_preflight(value)
    if field_name == "evidence_pre_execution":
        return _compact_mapping(
            value,
            (
                "status",
                "proof",
                "candidate_count",
            ),
        )
    if field_name in {"execution_write_proof", "slurm_status_sync_proof", "slurm_cancellation_proof"}:
        return _compact_mapping(
            value,
            (
                "status",
                "protected_by_pre_execution_evidence",
                "evidence_pre_execution_status",
                "submitted_count",
                "slurm_submit_called",
                "slurm_submit_count",
                "slurm_submit_proven_absent",
                "sync_called",
                "updated_job_count",
                "cancellation_required",
                "cancel_called",
                "cancelled_job_count",
                "mutation_occurred",
            ),
        )
    if field_name == "no_mutation_proof":
        return _compact_mapping(
            value,
            (
                "adapter_download_called",
                "slurm_submit_called",
                "slurm_status_sync_called",
                "slurm_cancellation_called",
                "shud_runtime_called",
                "hydro_result_table_writes",
                "met_result_table_writes",
                "pipeline_status_writes",
                "pipeline_event_writes",
            ),
        )
    if field_name == "readiness":
        return _compact_mapping(
            value,
            (
                "schema_version",
                "interpretation",
                "production_ready",
                "final_production_readiness_claimed",
                "can_claim_final_production_readiness",
            ),
        )
    return _compact_retained_bounded_field(field_name, value)


def _drop_empty_optional_bounded_fields(payload: dict[str, Any]) -> None:
    for field_name in (
        "finished_at",
        "execution_mode",
        "readiness_interpretation",
        "model_discovery",
        "source_cycles",
        "candidates",
        "blocked_candidates",
        "skipped_candidates",
        "duplicate_exclusions",
    ):
        if payload.get(field_name) in (None, "", [], {}):
            payload.pop(field_name, None)


def _drop_not_required_optional_proofs(payload: dict[str, Any]) -> None:
    for field_name in (
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
    ):
        if _is_required_bounded_field(payload, field_name):
            continue
        value = payload.get(field_name)
        if not isinstance(value, Mapping):
            continue
        if (
            value.get("status") == "not_required"
            and value.get("protected_by_pre_execution_evidence") is not True
            and value.get("mutation_occurred") is not True
        ):
            payload.pop(field_name, None)


def _compact_counts(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    compact: dict[str, Any] = {}
    for key, raw_value in value.items():
        if raw_value not in (0, None, False, "", [], {}):
            compact[str(key)] = raw_value
    return compact or {"candidate_count": 0}


def _compact_review_contract(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    compact = _compact_mapping(value, ("contract_id", "github_issue", "openspec_change", "scope"))
    if _payload_fits(compact, max_evidence_bytes=160, compact=True):
        return compact
    return _compact_mapping(value, ("contract_id", "github_issue"))


def _compact_limit(value: Any) -> Any:
    return _compact_mapping(value, ("reason",))


def _compact_retained_bounded_field(field_name: str, value: Any) -> Any:
    if value is None:
        return {}
    if field_name == "resolved_runtime_roots":
        return _compact_resolved_runtime_roots(value)
    if field_name == "runtime_config":
        return _compact_mapping(
            value,
            (
                "service_role",
                "require_runtime_roots",
                "dry_run",
            ),
        )
    if field_name == "root_preflight":
        return _compact_root_preflight(value)
    if field_name == "evidence_pre_execution":
        return _compact_mapping(
            value,
            (
                "status",
                "proof",
                "candidate_count",
            ),
        )
    if field_name in {"execution_write_proof", "slurm_status_sync_proof", "slurm_cancellation_proof"}:
        return _compact_mapping(
            value,
            (
                "status",
                "protected_by_pre_execution_evidence",
                "evidence_pre_execution_status",
                "submitted_count",
                "slurm_submit_called",
                "slurm_submit_count",
                "slurm_submit_proven_absent",
                "sync_called",
                "updated_job_count",
                "cancellation_required",
                "cancel_called",
                "cancelled_job_count",
                "mutation_occurred",
            ),
        )
    if field_name == "no_mutation_proof":
        return _compact_mapping(
            value,
            (
                "adapter_download_called",
                "slurm_submit_called",
                "slurm_status_sync_called",
                "slurm_cancellation_called",
                "shud_runtime_called",
                "hydro_result_table_writes",
                "met_result_table_writes",
                "pipeline_status_writes",
                "pipeline_event_writes",
            ),
        )
    if field_name == "readiness":
        compact = _compact_mapping(
            value,
            (
                "schema_version",
                "interpretation",
                "production_ready",
                "final_production_readiness_claimed",
                "can_claim_final_production_readiness",
            ),
        )
        return compact if compact else _bounded_retained_field_summary(field_name, value)
    return _bounded_retained_field_summary(field_name, value)


def _compact_mapping(value: Any, keys: Sequence[str]) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("", value)
    return {key: value[key] for key in keys if key in value}


def _compact_resolved_runtime_roots(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("resolved_runtime_roots", value)
    compact_roots: dict[str, Any] = {}
    root_names = ("workspace_root", "evidence_root")
    for root_name in root_names:
        if root_name not in value:
            continue
        root_value = value[root_name]
        if isinstance(root_value, Mapping):
            compact_roots[root_name] = _compact_mapping(root_value, ("path",))
        else:
            compact_roots[root_name] = root_value
    return compact_roots


def _compact_root_preflight(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("root_preflight", value)
    compact: dict[str, Any] = _compact_mapping(value, ("status", "checked_at"))
    checks = value.get("checks")
    if isinstance(checks, Mapping):
        compact_checks: dict[str, Any] = {}
        allowed_roots_policy = checks.get("allowed_roots_policy")
        if isinstance(allowed_roots_policy, Mapping):
            compact_checks["allowed_roots_policy"] = _compact_mapping(
                allowed_roots_policy,
                ("non_empty", "allowed"),
            )
        evidence_root = checks.get("evidence_root")
        if isinstance(evidence_root, Mapping):
            compact_checks["evidence_root"] = _compact_mapping(evidence_root, ("writable", "safe"))
        compact["checks"] = compact_checks
    return compact


def _bounded_retained_field_summary(field_name: str, value: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": "omitted",
        "reason": _RETAINED_FIELD_SUMMARY_REASON,
    }
    if isinstance(value, Mapping):
        summary["omitted_key_count"] = len(value)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        summary["omitted_item_count"] = len(value)
    elif value is None:
        summary["original_value"] = None
    else:
        summary["omitted_value_type"] = type(value).__name__
    if field_name in {"execution_write_proof", "slurm_status_sync_proof", "slurm_cancellation_proof"}:
        summary["proof_status"] = _mapping_status(value)
    elif field_name in {"evidence_pre_execution", "root_preflight", "readiness"}:
        summary["source_status"] = _mapping_status(value)
    return summary


def _minimal_bounded_retained_field_summary() -> dict[str, str]:
    return {
        "status": "omitted",
        "reason": _RETAINED_FIELD_SUMMARY_REASON,
    }


def _mapping_status(value: Any) -> str | None:
    if isinstance(value, Mapping):
        status = value.get("status")
        if status not in (None, ""):
            return str(status)
    return None


def _call_bounded_evidence_payload(
    context: SchedulerEvidenceWriteContext,
    payload: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    if context.bounded_evidence_payload is not None:
        return context.bounded_evidence_payload(
            payload,
            reason=reason,
            max_evidence_bytes=context.max_evidence_bytes,
        )
    return bounded_evidence_payload(payload, reason=reason, max_evidence_bytes=context.max_evidence_bytes)


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
            pass_id=pass_id,
            artifact_path=artifact_path,
            reason=reason,
            details=details,
            evidence_safe=context.evidence_safe,
        )
    return evidence_reservation_blocked_payload(
        pass_id=pass_id,
        artifact_path=artifact_path,
        reason=reason,
        details=details,
        evidence_safe=context.evidence_safe,
    )


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


def scheduler_pass_status_from_cancellation(cancellation_evidence: Sequence[Mapping[str, Any]]) -> str:
    if not cancellation_evidence:
        return "planned"
    statuses = {str(item.get("status") or "") for item in cancellation_evidence}
    if statuses == {"cancelled"}:
        return "slurm_cancelled"
    if "cancelled" in statuses or "partially_cancelled" in statuses:
        return "slurm_partially_cancelled"
    if statuses == {"preflight_blocked"}:
        return "preflight_blocked"
    return "slurm_cancellation_blocked"


def scheduler_execution_boundary_from_cancellation(cancellation_evidence: Sequence[Mapping[str, Any]]) -> str:
    if not cancellation_evidence:
        return "planning_only"
    if all(str(item.get("status") or "") == "preflight_blocked" for item in cancellation_evidence):
        return "evidence_preflight_blocked"
    return "slurm_cancellation"


def slurm_status_sync_proof(
    *,
    sync_required: bool = False,
    reservation: Mapping[str, Any] | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "sync_required": sync_required,
        "sync_called": False,
        "mutation_occurred": False,
        "protected_by_pre_execution_evidence": False,
    }
    if blocked:
        proof["status"] = "preflight_blocked"
    elif sync_required:
        proof["status"] = "pending_reservation"
    else:
        proof["status"] = "not_required"
    if reservation is not None:
        proof["evidence_pre_execution_status"] = reservation.get("status")
        proof["protected_by_pre_execution_evidence"] = reservation.get("status") == "reserved"
        if reservation.get("status") == "blocked":
            proof["block_reason"] = reservation.get("reason")
    return proof


def slurm_status_sync_proof_from_candidates(
    slurm_status_sync_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    sync_payloads = list(slurm_status_sync_evidence)
    failed_payloads = [item for item in sync_payloads if str(item.get("status") or "") == "failed"]
    update_count = sum(len(item.get("updates") or []) for item in sync_payloads)
    terminal_update_count = sum(len(item.get("terminal_updates") or []) for item in sync_payloads)
    unknown_after_attempt = any(item.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT for item in failed_payloads)
    status = "failed" if failed_payloads else ("synced" if sync_payloads else "not_required")
    proof: dict[str, Any] = {
        "status": status,
        "sync_required": bool(sync_payloads),
        "sync_called": bool(sync_payloads),
        "mutation_occurred": update_count > 0,
        "protected_by_pre_execution_evidence": reservation.get("status") == "reserved",
        "evidence_pre_execution_status": reservation.get("status"),
        "synced_cycle_count": len({str(item.get("cycle_id") or "") for item in sync_payloads if item.get("cycle_id")}),
        "updated_job_count": update_count,
        "terminal_update_count": terminal_update_count,
    }
    if failed_payloads:
        proof.update(
            {
                "failed_sync_count": len(failed_payloads),
                "error_code": failed_payloads[0].get("error_code"),
                "error_message": failed_payloads[0].get("error_message"),
            }
        )
    if unknown_after_attempt:
        proof["mutation_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["mutation_occurred"] = UNKNOWN_AFTER_ATTEMPT
        proof["pipeline_status_writes_proven_absent"] = False
        proof["pipeline_event_writes_proven_absent"] = False
    return proof


def execution_write_proof(
    *,
    reservation: Mapping[str, Any] | None = None,
    execution_required: bool = False,
    blocked: bool = False,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "execution_required": execution_required,
        "orchestration_called": False,
        "mutation_occurred": False,
        "protected_by_pre_execution_evidence": False,
    }
    if blocked:
        proof["status"] = "preflight_blocked"
    elif execution_required:
        proof["status"] = "pending_reservation"
    else:
        proof["status"] = "not_required"
    if reservation is not None:
        proof["evidence_pre_execution_status"] = reservation.get("status")
        proof["protected_by_pre_execution_evidence"] = reservation.get("status") == "reserved"
        if reservation.get("status") == "blocked":
            proof["block_reason"] = reservation.get("reason")
    return proof


def execution_write_proof_from_evidence(
    execution_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    execution_payloads = list(execution_evidence)
    orchestration_called = any(item.get("execution_attempted") is True for item in execution_payloads)
    submitted_count = sum(1 for item in execution_payloads if item.get("submitted") is True)
    slurm_submit_count = sum(1 for item in execution_payloads if item.get("slurm_submit_called") is True)
    unknown_slurm_submit_count = sum(
        1 for item in execution_payloads if item.get("slurm_submit_called") == UNKNOWN_AFTER_ATTEMPT
    )
    pipeline_status_write_count = sum(
        1 for item in execution_payloads if item.get("pipeline_status_write") is True
    )
    pipeline_event_write_count = sum(1 for item in execution_payloads if item.get("pipeline_event_write") is True)
    unknown_pipeline_status_write_count = sum(
        1 for item in execution_payloads if item.get("pipeline_status_write") == UNKNOWN_AFTER_ATTEMPT
    )
    unknown_pipeline_event_write_count = sum(
        1 for item in execution_payloads if item.get("pipeline_event_write") == UNKNOWN_AFTER_ATTEMPT
    )
    unknown_after_attempt_count = sum(
        1 for item in execution_payloads if item.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT
    )
    hydro_result_table_write_count = sum(
        1 for item in execution_payloads if item.get("hydro_result_table_write") is True
    )
    met_result_table_write_count = sum(
        1 for item in execution_payloads if item.get("met_result_table_write") is True
    )
    unknown_hydro_result_table_write_count = sum(
        1 for item in execution_payloads if item.get("hydro_result_table_write") == UNKNOWN_AFTER_ATTEMPT
    )
    unknown_met_result_table_write_count = sum(
        1 for item in execution_payloads if item.get("met_result_table_write") == UNKNOWN_AFTER_ATTEMPT
    )
    preflight_blocked = bool(execution_payloads) and all(
        str(item.get("status") or "") == "preflight_blocked" for item in execution_payloads
    )
    if unknown_after_attempt_count:
        status = UNKNOWN_AFTER_ATTEMPT
    elif submitted_count:
        status = "submitted"
    elif preflight_blocked:
        status = "preflight_blocked"
    elif execution_payloads:
        status = "completed_no_submit"
    else:
        status = "not_required"
    if unknown_slurm_submit_count:
        slurm_submit_value: bool | str = UNKNOWN_AFTER_ATTEMPT
    else:
        slurm_submit_value = slurm_submit_count > 0
    if unknown_hydro_result_table_write_count:
        hydro_result_table_write: bool | str = UNKNOWN_AFTER_ATTEMPT
    elif hydro_result_table_write_count:
        hydro_result_table_write = True
    else:
        hydro_result_table_write = slurm_submit_value
    if unknown_met_result_table_write_count:
        met_result_table_write: bool | str = UNKNOWN_AFTER_ATTEMPT
    elif met_result_table_write_count:
        met_result_table_write = True
    else:
        met_result_table_write = slurm_submit_value
    if unknown_pipeline_status_write_count:
        pipeline_status_write: bool | str = UNKNOWN_AFTER_ATTEMPT
    else:
        pipeline_status_write = pipeline_status_write_count > 0
    if unknown_pipeline_event_write_count:
        pipeline_event_write: bool | str = UNKNOWN_AFTER_ATTEMPT
    else:
        pipeline_event_write = pipeline_event_write_count > 0
    proof: dict[str, Any] = {
        "status": status,
        "execution_required": bool(execution_payloads),
        "orchestration_called": orchestration_called,
        "mutation_occurred": execution_mutation_value(
            slurm_submit_value,
            hydro_result_table_write,
            met_result_table_write,
            pipeline_status_write,
            pipeline_event_write,
        ),
        "protected_by_pre_execution_evidence": reservation.get("status") == "reserved",
        "evidence_pre_execution_status": reservation.get("status"),
        "submitted_count": submitted_count,
        "slurm_submit_called": slurm_submit_value,
        "slurm_submit_count": slurm_submit_count,
        "hydro_result_table_writes": hydro_result_table_write,
        "met_result_table_writes": met_result_table_write,
        "pipeline_status_writes": pipeline_status_write,
        "pipeline_event_writes": pipeline_event_write,
        "pipeline_status_write_count": pipeline_status_write_count,
        "pipeline_event_write_count": pipeline_event_write_count,
        "hydro_result_table_write_count": hydro_result_table_write_count,
        "met_result_table_write_count": met_result_table_write_count,
    }
    if unknown_slurm_submit_count:
        proof["slurm_submit_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_slurm_submit_count"] = unknown_slurm_submit_count
        proof["slurm_submit_proven_absent"] = False
    else:
        proof["slurm_submit_proven_absent"] = slurm_submit_count == 0
    proof["hydro_result_table_writes_proven_absent"] = hydro_result_table_write is False
    proof["met_result_table_writes_proven_absent"] = met_result_table_write is False
    if unknown_hydro_result_table_write_count:
        proof["hydro_result_table_write_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_hydro_result_table_write_count"] = unknown_hydro_result_table_write_count
        proof["hydro_result_table_writes_proven_absent"] = False
    if unknown_met_result_table_write_count:
        proof["met_result_table_write_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_met_result_table_write_count"] = unknown_met_result_table_write_count
        proof["met_result_table_writes_proven_absent"] = False
    if unknown_pipeline_status_write_count:
        proof["pipeline_status_write_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_pipeline_status_write_count"] = unknown_pipeline_status_write_count
        proof["pipeline_status_writes_proven_absent"] = False
    else:
        proof["pipeline_status_writes_proven_absent"] = pipeline_status_write_count == 0
    if unknown_pipeline_event_write_count:
        proof["pipeline_event_write_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_pipeline_event_write_count"] = unknown_pipeline_event_write_count
        proof["pipeline_event_writes_proven_absent"] = False
    else:
        proof["pipeline_event_writes_proven_absent"] = pipeline_event_write_count == 0
    if unknown_after_attempt_count:
        proof["mutation_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["mutation_occurred"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_execution_count"] = unknown_after_attempt_count
        if hydro_result_table_write == UNKNOWN_AFTER_ATTEMPT:
            proof["hydro_result_table_writes_proven_absent"] = False
        if met_result_table_write == UNKNOWN_AFTER_ATTEMPT:
            proof["met_result_table_writes_proven_absent"] = False
        if unknown_pipeline_status_write_count or pipeline_status_write_count:
            proof["pipeline_status_writes_proven_absent"] = False
        if unknown_pipeline_event_write_count or pipeline_event_write_count:
            proof["pipeline_event_writes_proven_absent"] = False
    return proof


def slurm_cancellation_proof(
    *,
    cancellation_required: bool = False,
    reservation: Mapping[str, Any] | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "cancellation_required": cancellation_required,
        "cancel_called": False,
        "mutation_occurred": False,
        "protected_by_pre_execution_evidence": False,
    }
    if blocked:
        proof["status"] = "preflight_blocked"
    elif cancellation_required:
        proof["status"] = "pending_reservation"
    else:
        proof["status"] = "not_required"
    if reservation is not None:
        proof["evidence_pre_execution_status"] = reservation.get("status")
        proof["protected_by_pre_execution_evidence"] = reservation.get("status") == "reserved"
        if reservation.get("status") == "blocked":
            proof["block_reason"] = reservation.get("reason")
    return proof


def slurm_cancellation_proof_from_evidence(
    cancellation_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    cancel_called = any(item.get("cancel_attempted") is True for item in cancellation_evidence)
    cancelled_count = slurm_cancelled_count(cancellation_evidence)
    blocked_count = slurm_cancellation_blocked_count(cancellation_evidence)
    unknown_after_attempt_count = sum(
        1 for item in cancellation_evidence if item.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT
    )
    pipeline_status_write_count = sum(1 for item in cancellation_evidence if item.get("pipeline_status_write") is True)
    pipeline_event_write_count = sum(1 for item in cancellation_evidence if item.get("pipeline_event_write") is True)
    proof: dict[str, Any] = {
        "status": scheduler_pass_status_from_cancellation(cancellation_evidence),
        "cancellation_required": bool(cancellation_evidence),
        "cancel_called": cancel_called,
        "mutation_occurred": cancelled_count > 0,
        "protected_by_pre_execution_evidence": reservation.get("status") == "reserved",
        "evidence_pre_execution_status": reservation.get("status"),
        "cancelled_job_count": cancelled_count,
        "blocked_cancellation_count": blocked_count,
        "pipeline_status_write_count": pipeline_status_write_count,
        "pipeline_event_write_count": pipeline_event_write_count,
    }
    if pipeline_status_write_count or pipeline_event_write_count:
        proof["mutation_occurred"] = True
    if unknown_after_attempt_count:
        proof["mutation_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["mutation_occurred"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_cancellation_count"] = unknown_after_attempt_count
        proof["slurm_cancellation_proven_absent"] = False
        proof["pipeline_status_writes_proven_absent"] = False
        proof["pipeline_event_writes_proven_absent"] = False
    else:
        proof["pipeline_status_writes_proven_absent"] = pipeline_status_write_count == 0
        proof["pipeline_event_writes_proven_absent"] = pipeline_event_write_count == 0
    return proof


def slurm_status_sync_count(proof: Mapping[str, Any]) -> int:
    value = proof.get("updated_job_count")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def slurm_status_sync_unknown_count(proof: Mapping[str, Any]) -> int:
    value = proof.get("failed_sync_count")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def slurm_status_sync_mutated(proof: Mapping[str, Any]) -> bool:
    return proof.get("mutation_occurred") is True


def slurm_status_sync_failed(proof: Mapping[str, Any]) -> bool:
    return str(proof.get("status") or "") == "failed" and proof.get("sync_called") is True


def slurm_cancelled_count(cancellation_evidence: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for item in cancellation_evidence:
        for job in item.get("cancelled_jobs") or []:
            if isinstance(job, Mapping) and str(job.get("status") or "").lower() == "cancelled":
                total += 1
    return total


def slurm_cancellation_blocked_count(cancellation_evidence: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for item in cancellation_evidence if str(item.get("status") or "") != "cancelled")


def slurm_cancellation_unknown_count(proof: Mapping[str, Any]) -> int:
    value = proof.get("unknown_cancellation_count")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 1 if proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT else 0


def scheduler_mutation_proof(
    *,
    execution_write_proof: Mapping[str, Any],
    slurm_status_sync_proof: Mapping[str, Any],
    slurm_cancellation_proof: Mapping[str, Any],
) -> dict[str, bool | str]:
    execution_slurm_submit = slurm_submit_proof_value(execution_write_proof)
    hydro_result_table_write = named_proof_value(
        execution_write_proof,
        "hydro_result_table_writes",
        "hydro_result_table_writes_proven_absent",
    )
    met_result_table_write = named_proof_value(
        execution_write_proof,
        "met_result_table_writes",
        "met_result_table_writes_proven_absent",
    )
    sync_mutation = proof_mutation_value(slurm_status_sync_proof)
    cancellation_mutation = proof_mutation_value(slurm_cancellation_proof)
    pipeline_status_write = merge_proof_values(
        pipeline_status_write_proof_value(execution_write_proof),
        sync_mutation,
        pipeline_status_write_proof_value(slurm_cancellation_proof),
    )
    pipeline_event_write = merge_proof_values(
        pipeline_event_write_proof_value(execution_write_proof),
        sync_mutation,
        pipeline_event_write_proof_value(slurm_cancellation_proof),
    )
    return {
        "slurm_submit_called": execution_slurm_submit,
        "hydro_result_table_writes": hydro_result_table_write,
        "met_result_table_writes": met_result_table_write,
        "pipeline_status_writes": pipeline_status_write,
        "pipeline_event_writes": pipeline_event_write,
        "slurm_status_sync_writes": sync_mutation,
        "slurm_cancellation_writes": cancellation_mutation,
    }


def proof_mutation_value(proof: Mapping[str, Any]) -> bool | str:
    if proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_occurred") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    return proof.get("mutation_occurred") is True


def named_proof_value(proof: Mapping[str, Any], write_field: str, absent_field: str) -> bool | str:
    value = proof.get(write_field)
    if value == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get(absent_field) is True:
        return False
    if proof.get(absent_field) is False and proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    return proof_mutation_value(proof)


def slurm_submit_proof_value(proof: Mapping[str, Any]) -> bool | str:
    value = proof.get("slurm_submit_called")
    if value == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get("slurm_submit_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if positive_count(proof.get("slurm_submit_count")):
        return True
    if proof.get("slurm_submit_proven_absent") is True:
        return False
    if proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_occurred") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    return proof.get("mutation_occurred") is True


def pipeline_status_write_proof_value(proof: Mapping[str, Any]) -> bool | str:
    value = proof.get("pipeline_status_writes")
    if value == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get("pipeline_status_write_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if "pipeline_status_write_count" in proof:
        return positive_count(proof.get("pipeline_status_write_count"))
    if proof.get("pipeline_status_writes_proven_absent") is True:
        return False
    if proof.get("mutation_occurred") is True:
        return True
    return False


def pipeline_event_write_proof_value(proof: Mapping[str, Any]) -> bool | str:
    value = proof.get("pipeline_event_writes")
    if value == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get("pipeline_event_write_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if "pipeline_event_write_count" in proof:
        return positive_count(proof.get("pipeline_event_write_count"))
    if proof.get("pipeline_event_writes_proven_absent") is True:
        return False
    if proof.get("mutation_occurred") is True:
        return True
    return False


def merge_proof_values(*values: bool | str) -> bool | str:
    if any(value == UNKNOWN_AFTER_ATTEMPT for value in values):
        return UNKNOWN_AFTER_ATTEMPT
    return any(value is True for value in values)


def positive_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def execution_mutation_value(*values: bool | str | None) -> bool | str:
    if any(value == UNKNOWN_AFTER_ATTEMPT for value in values):
        return UNKNOWN_AFTER_ATTEMPT
    return any(value is True for value in values)


def empty_counts() -> dict[str, int]:
    return {
        "candidate_count": 0,
        "blocked_candidate_count": 0,
        "skipped_candidate_count": 0,
        "selected_model_count": 0,
        "source_cycle_count": 0,
        "submitted_count": 0,
        "failed_count": 0,
        "partial_count": 0,
        "slurm_status_sync_count": 0,
        "slurm_status_sync_unknown_count": 0,
        "slurm_cancelled_count": 0,
        "slurm_cancellation_blocked_count": 0,
        "slurm_cancellation_unknown_count": 0,
    }


def no_mutation_proof() -> dict[str, bool]:
    return {
        "adapter_download_called": False,
        "slurm_submit_called": False,
        "slurm_status_sync_called": False,
        "slurm_cancellation_called": False,
        "shud_runtime_called": False,
        "hydro_result_table_writes": False,
        "met_result_table_writes": False,
        "pipeline_status_writes": False,
        "pipeline_event_writes": False,
    }


def evidence_reservation_blocked_payload(
    *,
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
        "artifact_path": str(artifact_path),
        "reason": reason,
        "error_code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "message": "Scheduler evidence write proof failed before production mutation.",
    }
    payload.update(dict(details or {}))
    return evidence_safe(payload)


def evidence_write_error_payload(error: OSError) -> dict[str, Any]:
    if isinstance(error, SchedulerEvidenceWriteError):
        return {"reason": error.reason, **error.details}
    return {"reason": "evidence_write_failed", "error": str(error)}


def scheduler_resolved_runtime_roots(config: SchedulerEvidenceConfig) -> dict[str, Any]:
    return {
        "workspace_root": root_evidence_item(
            config.workspace_root,
            env="WORKSPACE_ROOT",
            required=config.require_runtime_roots,
        ),
        "object_store_root": root_evidence_item(
            config.object_store_root,
            env="OBJECT_STORE_ROOT",
            required=config.require_runtime_roots,
        ),
        "published_artifact_root": root_evidence_item(
            config.published_artifact_root,
            env="NHMS_PUBLISHED_ARTIFACT_ROOT",
            required=config.require_runtime_roots,
        ),
        "lock_root": root_evidence_item(
            Path(config.lock_path).parent,
            env="NHMS_SCHEDULER_LOCK_ROOT",
            fallback="WORKSPACE_ROOT/scheduler",
            required=config.require_runtime_roots,
        ),
        "lock_path": root_evidence_item(
            config.lock_path,
            env="NHMS_SCHEDULER_LOCK_ROOT",
            fallback="WORKSPACE_ROOT/scheduler/production-scheduler.lock",
            required=config.require_runtime_roots,
        ),
        "evidence_root": root_evidence_item(
            config.evidence_dir,
            env="NHMS_SCHEDULER_EVIDENCE_ROOT",
            fallback="WORKSPACE_ROOT/scheduler/evidence",
            required=config.require_runtime_roots,
        ),
        "runtime_root": root_evidence_item(
            config.runtime_root,
            env="NHMS_SCHEDULER_RUNTIME_ROOT|NHMS_RUNTIME_ROOT|RUN_WORKSPACE_ROOT|SHUD_RUNTIME_ROOT",
            required=config.require_runtime_roots,
        ),
        "temp_root": root_evidence_item(
            config.temp_root,
            env="NHMS_SCHEDULER_TEMP_ROOT|NHMS_TEMP_ROOT|TMPDIR",
            required=config.require_runtime_roots,
        ),
    }


def root_evidence_item(
    value: Path | str | None,
    *,
    env: str,
    required: bool,
    fallback: str | None = None,
) -> dict[str, Any]:
    path = None if value in (None, "") else str(Path(value).expanduser().resolve(strict=False))
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
    return {
        "service_role": config.service_role,
        "require_runtime_roots": config.require_runtime_roots,
        "dry_run": config.dry_run,
        "continuous": config.continuous,
        "interval_seconds": config.interval_seconds,
        "sources": list(config.sources),
        "model_ids": list(config.model_ids),
        "basin_ids": list(config.basin_ids),
        "lookback_hours": config.lookback_hours,
        "cycle_lag_hours": config.cycle_lag_hours,
        "max_cycles_per_source": config.max_cycles_per_source,
        "retry_limit": config.retry_limit,
    }


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


def bounded_evidence_payload(
    payload: Mapping[str, Any],
    *,
    reason: str,
    max_evidence_bytes: int = MAX_EVIDENCE_BYTES,
) -> dict[str, Any]:
    bounded_payload = {
        "schema_version": payload.get("schema_version", SCHEDULER_EVIDENCE_SCHEMA_VERSION),
        "review_contract": payload.get(
            "review_contract",
            {
                "contract_id": SCHEDULER_EVIDENCE_CONTRACT_ID,
                "github_issue": SCHEDULER_EVIDENCE_GITHUB_ISSUE,
                "openspec_change": SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE,
                "scope": "scheduler_pass_evidence",
            },
        ),
        "pass_id": payload.get("pass_id"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "status": "resource_limit_blocked",
        "execution_mode": payload.get("execution_mode"),
        "readiness_interpretation": payload.get("readiness_interpretation", "non_final_scheduler_evidence"),
        "readiness": payload.get(
            "readiness",
            {
                "schema_version": "nhms.production_readiness.scheduler_input.v1",
                "interpretation": "non_final_scheduler_evidence",
                "live_receipts": [],
                "production_ready": False,
                "final_production_readiness_claimed": False,
                "can_claim_final_production_readiness": False,
            },
        ),
        "limit": {"reason": reason, "max_evidence_bytes": max_evidence_bytes},
        "counts": payload.get("counts", empty_counts()),
        "resolved_runtime_roots": payload.get("resolved_runtime_roots"),
        "runtime_config": payload.get("runtime_config"),
        "root_preflight": payload.get("root_preflight"),
        "evidence_pre_execution": payload.get("evidence_pre_execution"),
        "candidates": [],
        "blocked_candidates": [],
        "skipped_candidates": [],
        "duplicate_exclusions": payload.get("duplicate_exclusions", []),
        "source_cycles": [],
        "model_discovery": empty_model_discovery(),
        "artifact_path": payload.get("artifact_path"),
        "execution_boundary": payload.get("execution_boundary", "planning_only"),
        "execution_write_proof": payload.get("execution_write_proof"),
        "slurm_status_sync_proof": payload.get("slurm_status_sync_proof"),
        "slurm_cancellation_proof": payload.get("slurm_cancellation_proof"),
        "no_mutation_proof": payload.get("no_mutation_proof", no_mutation_proof()),
    }
    return _fit_bounded_evidence_payload(bounded_payload, max_evidence_bytes=max_evidence_bytes)


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
