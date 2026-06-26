from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from packages.common.safe_fs import (
    SafeFilesystemError,
    read_bytes_limited_no_follow,
)
from services.production_closure import (
    readiness_item_contracts as _readiness_item_contracts,
)
from services.production_closure import (
    readiness_shared_artifacts as _readiness_shared_artifacts,
)

validate_readiness_item = _readiness_item_contracts.validate_readiness_item
ProductionReadinessValidationError = _readiness_item_contracts.ProductionReadinessValidationError

PATH_TOKEN_RE = _readiness_shared_artifacts.PATH_TOKEN_RE
_bounded_redacted_payload = _readiness_shared_artifacts._bounded_redacted_payload
_path_for_evidence = _readiness_shared_artifacts._path_for_evidence
_redact_paths = _readiness_shared_artifacts._redact_paths
_redacted_preview = _readiness_shared_artifacts._redacted_preview
_refuse_symlink_components = _readiness_shared_artifacts._refuse_symlink_components

_SCHEDULER_ITEM_SUFFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

SCHEDULER_EVIDENCE_SCHEMA = "nhms.production_scheduler.pass_evidence.v1"
MAX_SCHEDULER_EVIDENCE_BYTES = 256 * 1024
MAX_SCHEDULER_EVIDENCE_FILES = 16
SCHEDULER_REVIEW_EXECUTION_MODES = frozenset(
    {
        "deterministic",
        "deterministic_fixture",
        "dry_run",
        "planning_only",
        "production_like",
        "production_orchestration",
        "slurm_cancellation",
        "slurm_gateway_orchestration",
        "slurm_preflight",
        "slurm_status_sync",
        "simulated",
    }
)
SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES = frozenset({"production_orchestration"})
SCHEDULER_REVIEW_PASSED_STATUSES = frozenset(
    {
        "planned",
        "ready",
        "passed",
        "submitted",
        "slurm_cancelled",
        "slurm_status_synced",
        "completed",
        "succeeded",
    }
)
SCHEDULER_REVIEW_BLOCKED_STATUSES = frozenset(
    {
        "blocked",
        "failed",
        "lock_contended",
        "permanently_failed",
        "preflight_blocked",
        "resource_limit_blocked",
        "slurm_cancellation_blocked",
        "slurm_partially_cancelled",
        "slurm_status_sync_failed",
        "submission_failed",
        "submitted_partial",
        "partial",
        "partially_failed",
    }
)

SCHEDULER_DRY_RUN_NO_MUTATION_FALSE_FIELDS = (
    "adapter_download_called",
    "slurm_submit_called",
    "slurm_status_sync_called",
    "slurm_cancellation_called",
    "shud_runtime_called",
    "hydro_result_table_writes",
    "met_result_table_writes",
    "pipeline_status_writes",
    "pipeline_event_writes",
)
SCHEDULER_PARTIAL_MODEL_RUN_STATUSES = frozenset(
    {
        "partial",
        "partially_failed",
        "submitted_partial",
    }
)
SCHEDULER_FAILED_MODEL_RUN_STATUSES = frozenset({"failed", "permanently_failed", "submission_failed"})
SCHEDULER_BLOCKED_MODEL_RUN_STATUSES = frozenset(
    {
        "blocked",
        "cancelled",
        "lock_contended",
        "preflight_blocked",
        "resource_limit_blocked",
        "unavailable",
    }
)
SCHEDULER_MODEL_RUN_STATUS_KEYS = frozenset({"status", "outcome", "result", "state"})
SCHEDULER_REQUIRED_COUNT_FIELDS = (
    "candidate_count",
    "blocked_candidate_count",
    "skipped_candidate_count",
    "submitted_count",
    "failed_count",
    "partial_count",
)
SCHEDULER_LIVE_WORK_STATUSES = frozenset({"submitted", "completed", "succeeded", "passed"})
SCHEDULER_LIVE_MODEL_RUN_STATUS_COMPATIBILITY: Mapping[str, frozenset[str]] = {
    "submitted": frozenset(
        {
            "accepted",
            "active",
            "complete",
            "completed",
            "passed",
            "published",
            "queued",
            "running",
            "submitted",
            "succeeded",
            "success",
        }
    ),
    "completed": frozenset({"complete", "completed", "passed", "published", "succeeded", "success"}),
    "succeeded": frozenset({"complete", "completed", "passed", "published", "succeeded", "success"}),
    "passed": frozenset({"complete", "completed", "passed", "published", "succeeded", "success"}),
}
SCHEDULER_LIVE_COMPATIBLE_MODEL_RUN_STATUSES = frozenset(
    status
    for statuses in SCHEDULER_LIVE_MODEL_RUN_STATUS_COMPATIBILITY.values()
    for status in statuses
)


@dataclass(frozen=True)
class _SchedulerCandidateIdentity:
    source_id: str
    cycle_identity: str
    model_id: str
    scenario_id: str


@dataclass(frozen=True)
class _SchedulerModelRunOutcome:
    status_values: frozenset[str]
    has_status_evidence: bool
    submitted: bool
    submitted_explicitly_false: bool
    failed: bool
    partial: bool
    blocked: bool
    producer_partial: bool


def _scheduler_evidence_items(
    config: Any,
    *,
    read_scheduler_evidence_item: Callable[..., dict[str, Any]] | None = None,
    scheduler_evidence_blocked: Callable[..., dict[str, Any]] | None = None,
    find_scheduler_evidence_files: Callable[[Path], list[Path]] | None = None,
) -> list[dict[str, Any]]:
    reader = read_scheduler_evidence_item or _read_scheduler_evidence_item
    blocked = scheduler_evidence_blocked or _scheduler_evidence_blocked
    finder = find_scheduler_evidence_files or _find_scheduler_evidence_files
    configured = config.scheduler_evidence_root is not None or config.scheduler_evidence_file is not None
    if not configured:
        return []
    if config.scheduler_evidence_root is not None and config.scheduler_evidence_file is not None:
        return [
            blocked(
                config.scheduler_evidence_file,
                config=config,
                reason="Provide either scheduler_evidence_root or scheduler_evidence_file, not both.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_AMBIGUOUS",
            )
        ]
    if config.scheduler_evidence_file is not None:
        return [reader(config.scheduler_evidence_file, config=config)]
    return _read_scheduler_evidence_root_items(
        config.scheduler_evidence_root,
        config=config,
        read_scheduler_evidence_item=reader,
        scheduler_evidence_blocked=blocked,
        find_scheduler_evidence_files=finder,
    )


def _read_scheduler_evidence_root_items(
    root: Path | None,
    *,
    config: Any,
    read_scheduler_evidence_item: Callable[..., dict[str, Any]] | None = None,
    scheduler_evidence_blocked: Callable[..., dict[str, Any]] | None = None,
    find_scheduler_evidence_files: Callable[[Path], list[Path]] | None = None,
) -> list[dict[str, Any]]:
    reader = read_scheduler_evidence_item or _read_scheduler_evidence_item
    blocked = scheduler_evidence_blocked or _scheduler_evidence_blocked
    finder = find_scheduler_evidence_files or _find_scheduler_evidence_files
    if root is None:
        return []
    try:
        evidence_files = finder(root)
    except (FileNotFoundError, OSError, SafeFilesystemError, ProductionReadinessValidationError) as error:
        return [
            blocked(
                root,
                config=config,
                reason=f"Scheduler evidence could not be discovered: {_redact_paths(str(error), config=config)}.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_DISCOVERY_FAILED",
            )
        ]
    if not evidence_files:
        return [
            blocked(
                root,
                config=config,
                reason="No scheduler evidence JSON file was found under the configured scheduler evidence root.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_MISSING",
            )
        ]
    if len(evidence_files) > MAX_SCHEDULER_EVIDENCE_FILES:
        return [
            blocked(
                root,
                config=config,
                reason=f"Scheduler evidence root contains more than {MAX_SCHEDULER_EVIDENCE_FILES} JSON artifacts.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_FILE_LIMIT",
            )
        ]
    return [reader(path, config=config) for path in evidence_files]


def _read_scheduler_evidence_item(
    path: Path,
    *,
    config: Any,
    safe_scheduler_evidence_file: Callable[[Path], Path] | None = None,
    scheduler_evidence_blocked: Callable[..., dict[str, Any]] | None = None,
    scheduler_evidence_errors: Callable[[Mapping[str, Any]], list[str]] | None = None,
    scheduler_readiness_status: Callable[..., str] | None = None,
    scheduler_evidence_mode: Callable[[Mapping[str, Any]], str] | None = None,
    scheduler_evidence_artifact_ref: Callable[..., str] | None = None,
    scheduler_item_suffix: Callable[[Mapping[str, Any], Path], str] | None = None,
) -> dict[str, Any]:
    safe_file = safe_scheduler_evidence_file or _safe_scheduler_evidence_file
    blocked = scheduler_evidence_blocked or _scheduler_evidence_blocked
    evidence_errors = scheduler_evidence_errors or _scheduler_evidence_errors
    readiness_status = scheduler_readiness_status or _scheduler_readiness_status
    evidence_mode = scheduler_evidence_mode or _scheduler_evidence_mode
    artifact_ref_for = scheduler_evidence_artifact_ref or _scheduler_evidence_artifact_ref
    item_suffix = scheduler_item_suffix or _scheduler_item_suffix
    try:
        evidence_path = safe_file(path)
        raw = read_bytes_limited_no_follow(evidence_path, max_bytes=MAX_SCHEDULER_EVIDENCE_BYTES)
        if len(raw) > MAX_SCHEDULER_EVIDENCE_BYTES:
            return blocked(
                evidence_path,
                config=config,
                reason=f"Scheduler evidence exceeds {MAX_SCHEDULER_EVIDENCE_BYTES} bytes.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_TOO_LARGE",
                raw_preview=_redacted_preview(raw, config=config),
            )
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, Mapping):
            return blocked(
                evidence_path,
                config=config,
                reason="Scheduler evidence JSON must be an object.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_JSON_INVALID",
                raw_preview=_redacted_preview(raw, config=config),
            )
        raw_payload = parsed
        payload = _bounded_redacted_payload(parsed, config=config)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        RecursionError,
        UnicodeDecodeError,
        SafeFilesystemError,
        ProductionReadinessValidationError,
    ) as error:
        return blocked(
            path,
            config=config,
            reason=f"Scheduler evidence could not be read: {_redact_paths(str(error), config=config)}.",
            error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_READ_FAILED",
        )

    errors = evidence_errors(raw_payload)
    status = str(raw_payload.get("status") or "unknown").strip()
    execution_mode = evidence_mode(raw_payload)
    item_status = readiness_status(raw_payload, errors=errors)
    summary_checksum = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    artifact_ref = artifact_ref_for(evidence_path, config=config)
    details = _bounded_redacted_payload(
        {
            "producer": "production_scheduler",
            "producer_schema": SCHEDULER_EVIDENCE_SCHEMA,
            "scheduler_schema": raw_payload.get("schema") or raw_payload.get("schema_version"),
            "scheduler_pass_id": raw_payload.get("pass_id"),
            "scheduler_status": status,
            "scheduler_execution_mode": execution_mode,
            "scheduler_artifact_ref": artifact_ref,
            "scheduler_checksum": summary_checksum,
            "candidate_count": _count_value(raw_payload, "candidate_count"),
            "blocked_candidate_count": _count_value(raw_payload, "blocked_candidate_count"),
            "submitted_count": _count_value(raw_payload, "submitted_count"),
            "skipped_candidate_count": _count_value(raw_payload, "skipped_candidate_count"),
            "partial_count": _count_value(raw_payload, "partial_count"),
            "failed_count": _count_value(raw_payload, "failed_count"),
            "no_mutation_proof": raw_payload.get("no_mutation_proof"),
            "execution_boundary": raw_payload.get("execution_boundary"),
            "acceptance_errors": errors,
            "payload": payload,
        },
        config=config,
    )
    return _item(
        item_id=f"deterministic-scheduler-evidence-{item_suffix(raw_payload, evidence_path)}",
        surface="scheduler_production_like_evidence",
        status=item_status,
        execution_mode="deterministic" if item_status == "passed" else "not_executed",
        required_for_final=False,
        live_proof_accepted=False,
        artifact_refs=[_path_for_evidence(evidence_path, config=config)],
        residual_risk=(
            "Scheduler evidence was consumed as deterministic/non-final review evidence; it is not live proof."
            if item_status == "passed"
            else "Scheduler evidence is malformed, stale, unsafe, or outside the expected review contract."
        ),
        removal_criteria=(
            "Provide an accepted live scheduler evidence receipt for live proof; keep scheduler evidence available "
            "for reviewer lineage."
            if item_status == "passed"
            else "Provide bounded scheduler pass evidence with matching schema, pass id, execution mode, and counts."
        ),
        dependencies=[
            f"schema={SCHEDULER_EVIDENCE_SCHEMA}",
            f"scheduler_pass_id={raw_payload.get('pass_id')}",
            f"scheduler_status={status}",
            f"scheduler_execution_mode={execution_mode}",
            f"producer_artifact_ref={artifact_ref}",
            f"scheduler_checksum={summary_checksum}",
        ],
        details=details,
    )


def _scheduler_evidence_blocked(
    path: Path,
    *,
    config: Any,
    reason: str,
    error_code: str = "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_BLOCKED",
    raw_preview: str | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "producer": "production_scheduler",
        "producer_schema": SCHEDULER_EVIDENCE_SCHEMA,
        "error_code": error_code,
        "reason": reason,
    }
    if raw_preview is not None:
        details["raw_preview"] = raw_preview
    return _item(
        item_id="deterministic-scheduler-evidence-blocked",
        surface="scheduler_production_like_evidence",
        status="blocked",
        execution_mode="not_executed",
        required_for_final=False,
        live_proof_accepted=False,
        artifact_refs=[_path_for_evidence(path, config=config)],
        residual_risk=reason,
        removal_criteria="Provide a readable bounded production scheduler evidence JSON artifact.",
        details=_bounded_redacted_payload(details, config=config),
    )


def _scheduler_bindings(items: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    bindings: list[Mapping[str, Any]] = []
    for item in items:
        if item.get("status") != "passed":
            continue
        details = item.get("details")
        if isinstance(details, Mapping):
            bindings.append(details)
    return tuple(bindings)


def _find_scheduler_evidence_files(
    root: Path,
    *,
    safe_scheduler_evidence_file: Callable[[Path], Path] | None = None,
) -> list[Path]:
    safe_file = safe_scheduler_evidence_file or _safe_scheduler_evidence_file
    root = root.expanduser()
    _refuse_symlink_components(root)
    try:
        root_stat = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise SafeFilesystemError(f"Failed to stat scheduler evidence root: {root}", kind="io") from error
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SafeFilesystemError(f"Scheduler evidence root must be a directory: {root}")
    candidates: list[Path] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            candidates.append(root / entry.name)
            if len(candidates) > MAX_SCHEDULER_EVIDENCE_FILES:
                return candidates
    return [safe_file(candidate) for candidate in sorted(candidates, key=lambda path: path.name)]


def _safe_scheduler_evidence_file(path: Path) -> Path:
    candidate = path.expanduser()
    _refuse_symlink_components(candidate)
    if candidate.is_symlink():
        raise SafeFilesystemError(f"Scheduler evidence must not be a symlink: {candidate}")
    try:
        file_stat = candidate.stat(follow_symlinks=False)
    except OSError as error:
        raise SafeFilesystemError(f"Failed to stat scheduler evidence: {candidate}", kind="io") from error
    if not stat.S_ISREG(file_stat.st_mode):
        raise SafeFilesystemError(f"Scheduler evidence must be a regular file: {candidate}")
    return candidate


def _scheduler_evidence_errors(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    schema = payload.get("schema") or payload.get("schema_version")
    if schema != SCHEDULER_EVIDENCE_SCHEMA:
        errors.append("schema_mismatch")
    if not _non_empty_string(payload.get("pass_id")):
        errors.append("missing_pass_id")
    execution_mode = _scheduler_evidence_mode(payload)
    if execution_mode not in SCHEDULER_REVIEW_EXECUTION_MODES:
        errors.append("execution_mode_not_review_evidence")
    status = str(payload.get("status") or "").strip()
    if not status:
        errors.append("missing_status")
    elif status not in SCHEDULER_REVIEW_PASSED_STATUSES and status not in SCHEDULER_REVIEW_BLOCKED_STATUSES:
        errors.append("status_not_allowed")
    counts = {count_field: _count_value(payload, count_field) for count_field in SCHEDULER_REQUIRED_COUNT_FIELDS}
    for count_field, value in counts.items():
        if not _count_value_present(payload, count_field) or value is None:
            errors.append(f"missing_{count_field}")
    if any(value is not None and value < 0 for value in counts.values()):
        errors.append("negative_counts")
    if _scheduler_evidence_is_stale(payload):
        errors.append("stale_scheduler_evidence")
    if _has_final_readiness_claim(payload):
        errors.append("scheduler_evidence_claimed_final_readiness")
    if execution_mode == "dry_run" and not _dry_run_no_mutation_proven(payload):
        errors.append("dry_run_no_mutation_proof_missing")
    if _has_unsafe_scheduler_identity(payload):
        errors.append("unsafe_scheduler_identity")
    errors.extend(_scheduler_identity_errors(payload))
    return errors


def _scheduler_readiness_status(payload: Mapping[str, Any], *, errors: Sequence[str]) -> str:
    if errors:
        return "blocked"
    status = str(payload.get("status") or "").strip()
    if status in SCHEDULER_REVIEW_BLOCKED_STATUSES or status.endswith(("_blocked", "_failed")):
        return "blocked"
    return "passed"


def _scheduler_evidence_mode(payload: Mapping[str, Any]) -> str:
    for key in ("execution_mode", "proof_mode", "mode"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _count_value_present(payload: Mapping[str, Any], field: str) -> bool:
    counts = payload.get("counts")
    if isinstance(counts, Mapping) and field in counts:
        return True
    return field in payload


def _count_value(payload: Mapping[str, Any], field: str) -> int | None:
    counts = payload.get("counts")
    value = counts.get(field) if isinstance(counts, Mapping) and field in counts else payload.get(field)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _scheduler_evidence_is_stale(payload: Mapping[str, Any]) -> bool:
    stale = payload.get("stale")
    if stale is True:
        return True
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), Mapping) else {}
    if freshness.get("stale") is True:
        return True
    if str(payload.get("status") or "").strip() in {"stale", "expired"}:
        return True
    return False


def _has_final_readiness_claim(payload: Mapping[str, Any]) -> bool:
    if payload.get("final_production_readiness_claimed") is True:
        return True
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), Mapping) else {}
    return readiness.get("production_ready") is True or readiness.get("final_production_readiness_claimed") is True


def _dry_run_no_mutation_proven(payload: Mapping[str, Any]) -> bool:
    proof = payload.get("no_mutation_proof")
    if not isinstance(proof, Mapping):
        return False
    return all(proof.get(key) is False for key in SCHEDULER_DRY_RUN_NO_MUTATION_FALSE_FIELDS)


def _has_unsafe_scheduler_identity(payload: Mapping[str, Any]) -> bool:
    identities = [payload.get("pass_id")]
    for collection_key in ("candidates", "blocked_candidates", "skipped_candidates", "model_run_evidence"):
        collection = payload.get(collection_key)
        if isinstance(collection, Sequence) and not isinstance(collection, (str, bytes, bytearray)):
            for item in collection:
                if isinstance(item, Mapping):
                    identities.extend(
                        item.get(key)
                        for key in (
                            "candidate_id",
                            "run_id",
                            "forcing_version_id",
                            "model_id",
                            "source_id",
                            "scenario_id",
                        )
                    )
    return any(isinstance(value, str) and _identity_value_looks_unsafe(value) for value in identities)


def _scheduler_identity_errors(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    candidate_count = _count_value(payload, "candidate_count")
    candidate_records = _scheduler_collection_identity_records(payload, "candidates")
    blocked_records = _scheduler_collection_identity_records(payload, "blocked_candidates")
    skipped_records = _scheduler_collection_identity_records(payload, "skipped_candidates")
    model_run_records = _scheduler_model_run_identity_records(payload)
    candidate_side_records = candidate_records + blocked_records + skipped_records
    identities = (
        [("candidates", record) for record in candidate_records]
        + [("blocked_candidates", record) for record in blocked_records]
        + [("skipped_candidates", record) for record in skipped_records]
        + [("model_run_evidence", record) for record in model_run_records]
    )
    if candidate_count is not None and candidate_count > 0 and not candidate_side_records:
        return ["missing_scheduler_candidate_identity"]

    selected_identity_by_candidate_id: dict[str, Mapping[str, Any]] = {}
    blocked_candidate_ids: set[str] = set()
    skipped_candidate_ids: set[str] = set()
    for collection_name, record in identities:
        record_errors = _scheduler_identity_record_errors(record, collection_name=collection_name)
        errors.extend(error for error in record_errors if error not in errors)
        candidate_id = _identity_string(record.get("candidate_id"))
        if not candidate_id:
            continue
        if collection_name == "candidates":
            if candidate_id in selected_identity_by_candidate_id:
                errors.append("duplicate_scheduler_candidate_identity")
            selected_identity_by_candidate_id.setdefault(candidate_id, record)
        elif collection_name == "blocked_candidates":
            blocked_candidate_ids.add(candidate_id)
        elif collection_name == "skipped_candidates":
            skipped_candidate_ids.add(candidate_id)

    for record in _scheduler_model_run_identity_records(payload):
        errors.extend(
            error
            for error in _scheduler_model_run_identity_errors(
                record,
                selected_identity_by_candidate_id=selected_identity_by_candidate_id,
                blocked_candidate_ids=blocked_candidate_ids,
                skipped_candidate_ids=skipped_candidate_ids,
            )
            if error not in errors
        )
    errors.extend(error for error in _scheduler_count_cardinality_errors(payload) if error not in errors)
    return errors


def _scheduler_collection_identity_records(payload: Mapping[str, Any], collection_name: str) -> list[Mapping[str, Any]]:
    return _mapping_sequence(payload.get(collection_name))


def _scheduler_model_run_identity_records(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for record in _mapping_sequence(payload.get("model_run_evidence")):
        records.append(record)
        nested = record.get("candidate_identity")
        if isinstance(nested, Mapping):
            records.append(nested)
    return records


def _scheduler_identity_record_errors(record: Mapping[str, Any], *, collection_name: str) -> list[str]:
    errors: list[str] = []
    if not _identity_string(record.get("candidate_id")):
        errors.append(f"{collection_name}_missing_candidate_id")
    if not _identity_string(record.get("source_id")):
        errors.append(f"{collection_name}_missing_source_id")
    if not _identity_string(record.get("cycle_time_utc")) and not _identity_string(record.get("cycle_id")):
        errors.append(f"{collection_name}_missing_cycle_identity")
    if not _identity_string(record.get("model_id")):
        errors.append(f"{collection_name}_missing_model_id")
    if not _identity_string(record.get("scenario_id")):
        errors.append(f"{collection_name}_missing_scenario_id")
    if collection_name in {"candidates", "model_run_evidence"}:
        if not _identity_string(record.get("run_id")):
            errors.append(f"{collection_name}_missing_run_id")
        if not _identity_string(record.get("forcing_version_id")):
            errors.append(f"{collection_name}_missing_forcing_version_id")
    parsed_identity = _parse_scheduler_candidate_identity(record.get("candidate_id"))
    if parsed_identity is not None:
        errors.extend(_scheduler_candidate_identity_mismatch_errors(record, parsed_identity, collection_name))
    elif _identity_string(record.get("candidate_id")):
        errors.append(f"{collection_name}_candidate_id_identity_mismatch")
    errors.extend(_scheduler_run_forcing_derivation_errors(record, parsed_identity, collection_name))
    return errors


def _scheduler_model_run_identity_errors(
    record: Mapping[str, Any],
    *,
    selected_identity_by_candidate_id: Mapping[str, Mapping[str, Any]],
    blocked_candidate_ids: set[str],
    skipped_candidate_ids: set[str],
) -> list[str]:
    errors = _scheduler_identity_record_errors(record, collection_name="model_run_evidence")
    candidate_id = _identity_string(record.get("candidate_id"))
    if not candidate_id:
        return errors
    candidate_record = selected_identity_by_candidate_id.get(candidate_id)
    if candidate_id in blocked_candidate_ids:
        errors.append("model_run_evidence_candidate_blocked")
    if candidate_id in skipped_candidate_ids:
        errors.append("model_run_evidence_candidate_skipped")
    if candidate_record is None:
        errors.append("model_run_evidence_candidate_not_selected")
    else:
        for field in (
            "source_id",
            "cycle_time_utc",
            "cycle_id",
            "model_id",
            "scenario_id",
            "run_id",
            "forcing_version_id",
        ):
            record_value = _identity_string(record.get(field))
            candidate_value = _identity_string(candidate_record.get(field))
            if record_value and candidate_value and record_value != candidate_value:
                errors.append(f"model_run_evidence_{field}_identity_mismatch")
    nested_run_id = _model_run_nested_value(record, "run_id")
    if nested_run_id and _identity_string(record.get("run_id")) != nested_run_id:
        errors.append("model_run_evidence_run_id_mismatch")
    nested_forcing_version_id = _model_run_nested_value(record, "forcing_version_id")
    if nested_forcing_version_id and _identity_string(record.get("forcing_version_id")) != nested_forcing_version_id:
        errors.append("model_run_evidence_forcing_version_id_mismatch")
    return errors


def _model_run_nested_value(record: Mapping[str, Any], field: str) -> str:
    values: list[str] = []
    if field == "forcing_version_id":
        nested = record.get("forcing")
        if isinstance(nested, Mapping):
            values.append(_identity_string(nested.get("forcing_version_id") or nested.get("id")))
        nested = record.get("forcing_version")
        if isinstance(nested, Mapping):
            values.append(_identity_string(nested.get("forcing_version_id") or nested.get("id")))
    elif field == "run_id":
        nested = record.get("hydro_run")
        if isinstance(nested, Mapping):
            values.append(_identity_string(nested.get("run_id") or nested.get("id")))
        nested = record.get("run")
        if isinstance(nested, Mapping):
            values.append(_identity_string(nested.get("run_id") or nested.get("id")))
    meaningful = [value for value in values if value]
    if not meaningful:
        return ""
    if any(value != meaningful[0] for value in meaningful[1:]):
        return ""
    return meaningful[0]


def _parse_scheduler_candidate_identity(value: Any) -> _SchedulerCandidateIdentity | None:
    candidate_id = _identity_string(value)
    if not candidate_id:
        return None
    parts = candidate_id.split(":")
    if len(parts) < 4 or not all(parts):
        return None
    cycle_identity = ":".join(parts[1:-2])
    if not cycle_identity:
        return None
    return _SchedulerCandidateIdentity(
        source_id=parts[0],
        cycle_identity=cycle_identity,
        model_id=parts[-2],
        scenario_id=parts[-1],
    )


def _scheduler_candidate_identity_mismatch_errors(
    record: Mapping[str, Any],
    parsed_identity: _SchedulerCandidateIdentity,
    collection_name: str,
) -> list[str]:
    errors: list[str] = []
    explicit_source = _identity_string(record.get("source_id"))
    if explicit_source and parsed_identity.source_id != explicit_source:
        errors.append(f"{collection_name}_candidate_id_identity_mismatch")
    explicit_model = _identity_string(record.get("model_id"))
    if explicit_model and parsed_identity.model_id != explicit_model:
        errors.append(f"{collection_name}_candidate_id_identity_mismatch")
    explicit_scenario = _identity_string(record.get("scenario_id"))
    if explicit_scenario and parsed_identity.scenario_id != explicit_scenario:
        errors.append(f"{collection_name}_scenario_id_identity_mismatch")
    explicit_cycle_time = _identity_string(record.get("cycle_time_utc"))
    explicit_cycle_id = _identity_string(record.get("cycle_id"))
    explicit_cycles: set[str] = set()
    explicit_cycles.update(_cycle_identity_aliases(explicit_cycle_time))
    explicit_cycles.update(_cycle_identity_aliases(explicit_cycle_id))
    parsed_cycles = _cycle_identity_aliases(parsed_identity.cycle_identity)
    if explicit_cycles and parsed_cycles.isdisjoint(explicit_cycles):
        errors.append(f"{collection_name}_candidate_id_identity_mismatch")
    return _dedupe_errors(errors)


def _scheduler_run_forcing_derivation_errors(
    record: Mapping[str, Any],
    parsed_identity: _SchedulerCandidateIdentity | None,
    collection_name: str,
) -> list[str]:
    expected = _scheduler_expected_run_forcing_ids(record, parsed_identity)
    if expected is None:
        return []
    errors: list[str] = []
    expected_run_id, expected_forcing_version_id = expected
    run_id = _identity_string(record.get("run_id"))
    if run_id and run_id != expected_run_id:
        errors.append(f"{collection_name}_run_id_derivation_mismatch")
    forcing_version_id = _identity_string(record.get("forcing_version_id"))
    if forcing_version_id and forcing_version_id != expected_forcing_version_id:
        errors.append(f"{collection_name}_forcing_version_id_derivation_mismatch")
    return errors


def _scheduler_expected_run_forcing_ids(
    record: Mapping[str, Any],
    parsed_identity: _SchedulerCandidateIdentity | None,
) -> tuple[str, str] | None:
    source_id = _scheduler_explicit_or_parsed_identity(record, "source_id", parsed_identity)
    model_id = _scheduler_explicit_or_parsed_identity(record, "model_id", parsed_identity)
    cycle_token = _scheduler_compact_cycle_token(record, parsed_identity)
    if not source_id or not model_id or not cycle_token:
        return None
    source_lower = source_id.lower()
    return (
        f"fcst_{source_lower}_{cycle_token}_{model_id}",
        f"forc_{source_lower}_{cycle_token}_{model_id}",
    )


def _scheduler_explicit_or_parsed_identity(
    record: Mapping[str, Any],
    field: str,
    parsed_identity: _SchedulerCandidateIdentity | None,
) -> str:
    explicit = _identity_string(record.get(field))
    if explicit:
        return explicit
    if parsed_identity is None:
        return ""
    if field == "source_id":
        return parsed_identity.source_id
    if field == "model_id":
        return parsed_identity.model_id
    if field == "scenario_id":
        return parsed_identity.scenario_id
    return ""


def _scheduler_compact_cycle_token(
    record: Mapping[str, Any],
    parsed_identity: _SchedulerCandidateIdentity | None,
) -> str:
    for value in (
        _identity_string(record.get("cycle_time_utc")),
        _identity_string(record.get("cycle_id")),
        parsed_identity.cycle_identity if parsed_identity is not None else "",
    ):
        token = _compact_cycle_token(value)
        if token:
            return token
    return ""


def _compact_cycle_token(value: str) -> str:
    if not value:
        return ""
    compact = _compact_cycle_id_suffix(value)
    if compact.isdigit() and len(compact) == 10:
        return compact
    parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(parsed).astimezone(UTC).strftime("%Y%m%d%H")
    except ValueError:
        return ""


def _scheduler_count_cardinality_errors(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    candidates = _scheduler_collection_identity_records(payload, "candidates")
    blocked_candidates = _scheduler_collection_identity_records(payload, "blocked_candidates")
    skipped_candidates = _scheduler_collection_identity_records(payload, "skipped_candidates")
    model_run_rows = _mapping_sequence(payload.get("model_run_evidence"))
    model_run_outcomes = [_scheduler_model_run_outcome(record) for record in model_run_rows]
    has_submitted_or_attempted_work = _scheduler_has_submitted_or_attempted_model_run(model_run_rows)
    has_attempted_or_terminal_work = _scheduler_has_attempted_or_terminal_model_run(
        model_run_rows,
        model_run_outcomes,
    )

    candidate_count = _count_value(payload, "candidate_count")
    if candidate_count is not None:
        identity_count = len(candidates) + len(blocked_candidates) + len(skipped_candidates)
        if candidate_count != identity_count:
            errors.append("candidate_count_identity_cardinality_mismatch")
        if candidate_count == 0 and model_run_rows:
            errors.append("candidate_count_identity_cardinality_mismatch")

    count_expectations = (
        (
            "submitted_count",
            sum(1 for outcome in model_run_outcomes if outcome.submitted),
            "submitted_count_model_run_evidence_mismatch",
        ),
        ("blocked_candidate_count", len(blocked_candidates), "blocked_candidate_count_identity_cardinality_mismatch"),
        ("skipped_candidate_count", len(skipped_candidates), "skipped_candidate_count_identity_cardinality_mismatch"),
    )
    for count_field, actual, error in count_expectations:
        value = _count_value(payload, count_field)
        if value is not None and value != actual:
            errors.append(error)

    submitted_count = _count_value(payload, "submitted_count")
    model_run_capacity = submitted_count if submitted_count is not None else len(model_run_rows)
    status = str(payload.get("status") or "").strip().lower()
    count_expectations_by_field = {
        "failed_count": _scheduler_failed_count_model_run_rows(model_run_outcomes),
        "partial_count": _scheduler_partial_count_model_run_rows(
            model_run_outcomes,
            pass_status=status,
            submitted_count=submitted_count,
            has_submitted_or_attempted_work=has_submitted_or_attempted_work,
        ),
    }
    for count_field, error_prefix in (
        ("failed_count", "failed_count"),
        ("partial_count", "partial_count"),
    ):
        value = _count_value(payload, count_field)
        if value is None:
            continue
        count_capacity = _scheduler_count_model_run_capacity(
            count_field,
            pass_status=status,
            model_run_capacity=model_run_capacity,
            model_run_row_count=len(model_run_rows),
            has_submitted_or_attempted_work=has_submitted_or_attempted_work,
            has_attempted_or_terminal_work=has_attempted_or_terminal_work,
        )
        if value > count_capacity or value > len(model_run_rows):
            errors.append(f"{error_prefix}_exceeds_model_run_evidence")
            continue
        matching_rows = count_expectations_by_field[count_field]
        if value != matching_rows:
            errors.append(f"{error_prefix}_status_cardinality_mismatch")
    errors.extend(
        _scheduler_live_status_count_errors(
            payload,
            model_run_rows=model_run_rows,
            model_run_outcomes=model_run_outcomes,
        )
    )
    return errors


def _scheduler_failed_count_model_run_rows(model_run_outcomes: Sequence[_SchedulerModelRunOutcome]) -> int:
    return sum(1 for outcome in model_run_outcomes if outcome.failed)


def _scheduler_has_submitted_or_attempted_model_run(model_run_rows: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        record.get("submitted") is True or record.get("execution_attempted") is True for record in model_run_rows
    )


def _scheduler_has_attempted_or_terminal_model_run(
    model_run_rows: Sequence[Mapping[str, Any]],
    model_run_outcomes: Sequence[_SchedulerModelRunOutcome],
) -> bool:
    return any(
        record.get("execution_attempted") is True or outcome.failed or outcome.blocked
        for record, outcome in zip(model_run_rows, model_run_outcomes, strict=True)
    )


def _scheduler_count_model_run_capacity(
    count_field: str,
    *,
    pass_status: str,
    model_run_capacity: int,
    model_run_row_count: int,
    has_submitted_or_attempted_work: bool,
    has_attempted_or_terminal_work: bool,
) -> int:
    if (
        count_field in {"failed_count", "partial_count"}
        and _scheduler_pass_uses_model_run_count_capacity(pass_status)
        and has_attempted_or_terminal_work
    ):
        return model_run_row_count
    if count_field == "partial_count" and pass_status == "submitted_partial" and has_submitted_or_attempted_work:
        return model_run_row_count
    return model_run_capacity


def _scheduler_pass_uses_model_run_count_capacity(pass_status: str) -> bool:
    return pass_status in SCHEDULER_REVIEW_BLOCKED_STATUSES or pass_status.endswith(("_blocked", "_failed"))


def _scheduler_partial_count_model_run_rows(
    model_run_outcomes: Sequence[_SchedulerModelRunOutcome],
    *,
    pass_status: str,
    submitted_count: int | None,
    has_submitted_or_attempted_work: bool,
) -> int:
    if _scheduler_pass_uses_producer_partial_count(pass_status, submitted_count=submitted_count):
        if not has_submitted_or_attempted_work:
            return 0
        return sum(1 for outcome in model_run_outcomes if outcome.producer_partial)
    return sum(1 for outcome in model_run_outcomes if outcome.partial)


def _scheduler_pass_uses_producer_partial_count(pass_status: str, *, submitted_count: int | None) -> bool:
    if pass_status == "submitted_partial":
        return True
    if submitted_count != 0:
        return False
    return pass_status in SCHEDULER_REVIEW_BLOCKED_STATUSES or pass_status.endswith(("_blocked", "_failed"))


def _scheduler_live_status_count_errors(
    payload: Mapping[str, Any],
    *,
    model_run_rows: Sequence[Mapping[str, Any]],
    model_run_outcomes: Sequence[_SchedulerModelRunOutcome],
) -> list[str]:
    execution_mode = _scheduler_evidence_mode(payload)
    status = str(payload.get("status") or "").strip().lower()
    if execution_mode not in SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES or status not in SCHEDULER_LIVE_WORK_STATUSES:
        return []

    errors: list[str] = []
    submitted_count = _count_value(payload, "submitted_count")
    failed_count = _count_value(payload, "failed_count")
    partial_count = _count_value(payload, "partial_count")
    submitted_rows = sum(1 for outcome in model_run_outcomes if outcome.submitted)
    failed_rows = sum(1 for outcome in model_run_outcomes if outcome.failed)
    partial_rows = sum(1 for outcome in model_run_outcomes if outcome.partial)
    blocked_rows = sum(1 for outcome in model_run_outcomes if outcome.blocked)
    allowed_statuses = SCHEDULER_LIVE_MODEL_RUN_STATUS_COMPATIBILITY.get(status, frozenset())
    incompatible_rows = sum(
        1
        for outcome in model_run_outcomes
        if outcome.submitted_explicitly_false
        or not outcome.has_status_evidence
        or not outcome.status_values & allowed_statuses
        or outcome.failed
        or outcome.partial
        or outcome.blocked
    )

    if submitted_count is None or submitted_count <= 0 or not model_run_rows:
        errors.append("submitted_status_without_model_run_evidence")
    elif submitted_count != submitted_rows:
        errors.append("submitted_count_model_run_evidence_mismatch")

    if failed_count != 0:
        errors.append("live_status_failed_count_nonzero")
    if partial_count != 0:
        errors.append("live_status_partial_count_nonzero")
    if failed_rows or partial_rows or blocked_rows:
        errors.append("live_status_model_run_blocked_outcome")
    if incompatible_rows:
        errors.append("submitted_status_model_run_status_mismatch")
    return errors


def _scheduler_model_run_outcome(record: Mapping[str, Any]) -> _SchedulerModelRunOutcome:
    status_values = frozenset(_scheduler_model_run_status_values(record))
    submitted_explicitly_false = record.get("submitted") is False
    submitted = record.get("submitted") is True or bool(status_values & SCHEDULER_LIVE_COMPATIBLE_MODEL_RUN_STATUSES)
    if submitted_explicitly_false:
        submitted = False
    failed = any(_scheduler_model_run_failed_status(status) for status in status_values)
    partial = any(_scheduler_model_run_partial_status(status) for status in status_values)
    blocked = any(_scheduler_model_run_blocked_status(status) for status in status_values)
    producer_partial = any(_scheduler_model_run_producer_partial_status(status) for status in status_values)
    return _SchedulerModelRunOutcome(
        status_values=status_values,
        has_status_evidence=bool(status_values),
        submitted=submitted,
        submitted_explicitly_false=submitted_explicitly_false,
        failed=failed,
        partial=partial,
        blocked=blocked,
        producer_partial=producer_partial,
    )


def _scheduler_model_run_status_values(record: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("status", "outcome", "result", "state", "candidate_outcome"):
        value = record.get(key)
        values.update(_nested_scheduler_status_values(value, is_status_value=True))
    for key in ("stage_statuses", "stage_evidence", "task_results"):
        values.update(_nested_scheduler_status_values(record.get(key)))
    task_results_summary = record.get("task_results_summary")
    if isinstance(task_results_summary, Mapping):
        values.update(_scheduler_status_count_values(task_results_summary.get("status_counts")))
    return values


def _scheduler_status_count_values(value: Any) -> set[str]:
    if not isinstance(value, Mapping):
        return set()
    return {
        str(status).strip().lower()
        for status, count in value.items()
        if str(status).strip() and _positive_scheduler_status_count(count)
    }


def _positive_scheduler_status_count(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        return value > 0
    if isinstance(value, str):
        try:
            return float(value.strip()) > 0
        except ValueError:
            return False
    return False


def _nested_scheduler_status_values(value: Any, *, is_status_value: bool = False) -> set[str]:
    values: set[str] = set()
    stack: list[tuple[Any, bool]] = [(value, is_status_value)]
    seen_containers: set[int] = set()
    while stack:
        current, current_is_status_value = stack.pop()
        if isinstance(current, str):
            if current_is_status_value and current.strip():
                values.add(current.strip().lower())
            continue
        if isinstance(current, Mapping):
            current_id = id(current)
            if current_id in seen_containers:
                continue
            seen_containers.add(current_id)
            for key, nested in current.items():
                if str(key) == "status_counts":
                    values.update(_scheduler_status_count_values(nested))
                else:
                    stack.append((nested, str(key) in SCHEDULER_MODEL_RUN_STATUS_KEYS))
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            current_id = id(current)
            if current_id in seen_containers:
                continue
            seen_containers.add(current_id)
            stack.extend((item, current_is_status_value) for item in current)
    return values


def _scheduler_model_run_failed_status(status: str) -> bool:
    return status in SCHEDULER_FAILED_MODEL_RUN_STATUSES or status.endswith("_failed")


def _scheduler_model_run_partial_status(status: str) -> bool:
    return status in SCHEDULER_PARTIAL_MODEL_RUN_STATUSES or status.endswith("_partial")


def _scheduler_model_run_blocked_status(status: str) -> bool:
    return status in SCHEDULER_BLOCKED_MODEL_RUN_STATUSES or status.endswith(
        ("_blocked", "_cancelled", "_unavailable")
    )


def _scheduler_model_run_producer_partial_status(status: str) -> bool:
    return (
        status in SCHEDULER_PARTIAL_MODEL_RUN_STATUSES
        or status in SCHEDULER_FAILED_MODEL_RUN_STATUSES
        or status in SCHEDULER_BLOCKED_MODEL_RUN_STATUSES
        or status.endswith(("_blocked", "_cancelled", "_failed", "_unavailable"))
    )


def _dedupe_errors(errors: Sequence[str]) -> list[str]:
    unique: list[str] = []
    for error in errors:
        if error not in unique:
            unique.append(error)
    return unique


def _cycle_identity_aliases(value: str) -> set[str]:
    if not value:
        return set()
    aliases = {value}
    aliases.add(_compact_cycle_id_suffix(value))
    parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        aliases.add(datetime.fromisoformat(parsed).astimezone(UTC).strftime("%Y%m%d%H"))
    except ValueError:
        pass
    return aliases


def _compact_cycle_id_suffix(cycle_id: str) -> str:
    parts = cycle_id.rsplit("_", 1)
    return parts[-1] if len(parts) == 2 and parts[-1].isdigit() else cycle_id


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _identity_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _identity_value_looks_unsafe(value: str) -> bool:
    stripped = value.strip()
    return (
        not stripped
        or "\x00" in stripped
        or any(separator in stripped for separator in ("../", "..\\"))
        or bool(PATH_TOKEN_RE.search(stripped))
    )


def _scheduler_evidence_artifact_ref(path: Path, *, config: Any) -> str:
    if config.scheduler_evidence_root is not None:
        try:
            relative = path.resolve(strict=False).relative_to(
                config.scheduler_evidence_root.expanduser().resolve(strict=False)
            )
        except ValueError:
            relative = Path(path.name)
    else:
        relative = Path(path.name)
    return f"scheduler:{relative.as_posix()}"


def _scheduler_item_suffix(payload: Mapping[str, Any], path: Path) -> str:
    pass_id = payload.get("pass_id")
    if isinstance(pass_id, str) and _SCHEDULER_ITEM_SUFFIX_RE.fullmatch(pass_id):
        return pass_id
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    return digest


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _item(
    *,
    item_id: str,
    surface: str,
    status: str,
    execution_mode: str,
    required_for_final: bool,
    live_proof_accepted: bool,
    artifact_refs: Sequence[str],
    residual_risk: str,
    removal_criteria: str,
    exclusions: Sequence[Mapping[str, Any]] = (),
    dependencies: Sequence[str] = (),
    details: Mapping[str, Any] | None = None,
    owner: str = "release_owner",
    action: str | None = None,
) -> dict[str, Any]:
    item = {
        "item_id": item_id,
        "surface": surface,
        "status": status,
        "execution_mode": execution_mode,
        "required_for_final": required_for_final,
        "live_proof_accepted": live_proof_accepted,
        "artifact_refs": list(artifact_refs),
        "residual_risk": residual_risk,
        "removal_criteria": removal_criteria,
        "exclusions": [dict(exclusion) for exclusion in exclusions],
        "dependencies": list(dependencies),
        "owner": owner,
        "action": action or removal_criteria,
    }
    if details is not None:
        item["details"] = dict(details)
    validate_readiness_item(item)
    return item
