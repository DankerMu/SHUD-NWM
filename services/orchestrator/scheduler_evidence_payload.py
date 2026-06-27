from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from services.orchestrator import scheduler_evidence as _scheduler_evidence


def _serialize_evidence_json(payload: Any, *, compact: bool = False) -> str:
    if compact:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return json.dumps(payload, indent=_scheduler_evidence._EVIDENCE_JSON_INDENT, sort_keys=True)


def _serialize_evidence_json_if_within_limit(
    payload: Any,
    *,
    max_evidence_bytes: int,
    compact: bool = False,
) -> str | None:
    if compact:
        encoder = json.JSONEncoder(separators=(",", ":"), sort_keys=True)
    else:
        encoder = json.JSONEncoder(indent=_scheduler_evidence._EVIDENCE_JSON_INDENT, sort_keys=True)
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
    context: Any,
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

    raise _scheduler_evidence.SchedulerEvidenceWriteError(
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
    _compact_required_bounded_fields(bounded_payload)
    if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
        return bounded_payload

    for field_name in _scheduler_evidence._DROPPABLE_BOUNDED_EVIDENCE_FIELDS:
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

    for field_name in _scheduler_evidence._SUMMARIZABLE_BOUNDED_EVIDENCE_FIELDS:
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

    for field_name in _scheduler_evidence._OPTIONAL_MINIMAL_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        if _is_required_bounded_field(bounded_payload, field_name):
            continue
        bounded_payload[field_name] = _bounded_retained_field_summary(field_name, bounded_payload[field_name])
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _scheduler_evidence._OPTIONAL_MINIMAL_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        if _is_required_bounded_field(bounded_payload, field_name):
            continue
        bounded_payload[field_name] = _minimal_bounded_retained_field_summary()
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _scheduler_evidence._DROPPABLE_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        bounded_payload.pop(field_name)
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _scheduler_evidence._OPTIONAL_BOUNDED_EVIDENCE_DROP_FIELDS:
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
    for field_name in _scheduler_evidence._REQUIRED_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in payload:
            continue
        if field_name == "counts":
            payload[field_name] = _compact_counts(payload[field_name])
        elif field_name not in {"schema_version", "pass_id", "status", "artifact_path", "limit"}:
            payload[field_name] = _compact_required_bounded_field(field_name, payload[field_name])


def _is_required_bounded_field(payload: Mapping[str, Any], field_name: str) -> bool:
    return field_name in _scheduler_evidence._REQUIRED_BOUNDED_EVIDENCE_FIELDS and field_name in payload


def _compact_required_bounded_field(field_name: str, value: Any) -> Any:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        return value
    if field_name == "resolved_runtime_roots":
        return _compact_resolved_runtime_roots(value)
    if field_name == "runtime_config":
        return _compact_runtime_config(value)
    if field_name == "db_free_runtime":
        return _compact_db_free_runtime(value)
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
    if field_name in {
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
        "restart_reconcile_proof",
    }:
        return _compact_write_proof(field_name, value)
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
                "restart_reconcile_writes",
            ),
        )
    if field_name == "retention":
        return _compact_retention(value)
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


def _compact_runtime_config(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("runtime_config", value)
    db_free_runtime = value.get("db_free_runtime")
    db_free_required = value.get("scheduler_db_free_required") is True or (
        isinstance(db_free_runtime, Mapping) and db_free_runtime.get("required") is True
    )
    if not db_free_required:
        return _compact_mapping(
            value,
            (
                "service_role",
                "require_runtime_roots",
                "dry_run",
                "allowed_cycle_hours_utc",
            ),
        )
    compact = _compact_mapping(
        value,
        (
            "service_role",
            "require_runtime_roots",
            "database_url_configured",
            "scheduler_db_free_required",
            "scheduler_state_backend",
            "scheduler_lock_backend",
            "scheduler_registry_backend",
            "scheduler_canonical_readiness_backend",
            "scheduler_journal_backend",
            "scheduler_state_index_backend",
            "dry_run",
            "allowed_cycle_hours_utc",
        ),
    )
    if isinstance(db_free_runtime, Mapping):
        compact["db_free_runtime"] = _compact_db_free_runtime(value.get("db_free_runtime"))
    return compact


def _compact_db_free_runtime(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("db_free_runtime", value)
    compact = _compact_mapping(
        value,
        (
            "status",
            "required",
            "required_env",
            "database_url_configured",
            "canonical_selector_fields",
            "canonical_path_fields",
        ),
    )
    selectors = value.get("selectors")
    if isinstance(selectors, Mapping):
        compact["selectors"] = {
            str(env): _compact_mapping(
                selector,
                ("configured", "selected", "required_value", "file_selected"),
            )
            for env, selector in selectors.items()
            if isinstance(selector, Mapping)
        }
    paths = value.get("paths")
    if isinstance(paths, Mapping):
        compact["paths"] = {
            str(env): _compact_db_free_path_or_check(path)
            for env, path in paths.items()
            if isinstance(path, Mapping)
        }
    checks = value.get("checks")
    if isinstance(checks, Mapping):
        compact["checks"] = {
            str(env): _compact_db_free_path_or_check(check)
            for env, check in checks.items()
            if isinstance(check, Mapping)
        }
    blockers = value.get("blockers")
    if isinstance(blockers, Sequence) and not isinstance(blockers, str | bytes | bytearray):
        compact["blockers"] = [
            _compact_mapping(blocker, ("code", "field", "reason", "path", "error_type"))
            for blocker in blockers
            if isinstance(blocker, Mapping)
        ]
    provider_blocker = value.get("provider_blocker")
    if isinstance(provider_blocker, Mapping):
        compact["provider_blocker"] = _compact_mapping(provider_blocker, ("code", "field", "reason"))
    nested_evidence = value.get("evidence")
    if (
        isinstance(nested_evidence, Mapping)
        and "selectors" not in compact
        and "paths" not in compact
        and "checks" not in compact
    ):
        compact["evidence"] = _compact_db_free_runtime(nested_evidence)
    return compact


def _compact_db_free_path_or_check(value: Mapping[str, Any]) -> dict[str, Any]:
    return _compact_mapping(
        value,
        (
            "configured",
            "selected",
            "required_value",
            "file_selected",
            "value_recorded",
            "path",
            "kind",
            "uri",
            "object_uri",
            "supported_object_uri",
            "scheme",
            "absolute",
            "contained",
            "exists",
            "writable",
            "object_boundary",
            "bucket",
            "namespace",
        ),
    )


def _compact_retention(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("retention", value)
    compact = _compact_mapping(
        value,
        (
            "status",
            "enabled",
            "dry_run",
            "forced_dry_run_by_scheduler",
            "forced_dry_run_reason",
            "retention_days",
            "freed_bytes",
        ),
    )
    counts = value.get("counts")
    if isinstance(counts, Mapping):
        compact["counts"] = _compact_mapping(counts, ("planned", "deleted", "skipped", "failed"))
    for field_name in ("planned", "deleted", "skipped", "failed"):
        items = value.get(field_name)
        if isinstance(items, Sequence) and not isinstance(items, str | bytes | bytearray):
            compact[f"{field_name}_count"] = len(items)
    if "deleted_count" in value:
        compact["deleted_count"] = value["deleted_count"]
    return compact


def _compact_retained_bounded_field(field_name: str, value: Any) -> Any:
    if value is None:
        return {}
    if field_name == "resolved_runtime_roots":
        return _compact_resolved_runtime_roots(value)
    if field_name == "runtime_config":
        return _compact_runtime_config(value)
    if field_name == "db_free_runtime":
        return _compact_db_free_runtime(value)
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
    if field_name in {
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
        "restart_reconcile_proof",
    }:
        return _compact_write_proof(field_name, value)
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
                "restart_reconcile_writes",
            ),
        )
    if field_name == "retention":
        return _compact_retention(value)
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


def _compact_write_proof(field_name: str, value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary(field_name, value)
    if (
        field_name == "restart_reconcile_proof"
        and value.get("mutation_occurred") is not True
        and value.get("mutation_outcome") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_status_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_event_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    ):
        return _compact_mapping(value, ("status", "mutation_occurred"))
    if (
        field_name == "execution_write_proof"
        and value.get("mutation_outcome") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("slurm_submit_called") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_status_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_event_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    ):
        return _compact_mapping(
            value,
            (
                "status",
                "protected_by_pre_execution_evidence",
                "submitted_count",
                "slurm_submit_called",
                "mutation_occurred",
                "pipeline_status_writes",
                "pipeline_event_writes",
            ),
        )
    if (
        field_name == "slurm_cancellation_proof"
        and value.get("mutation_occurred") is not True
        and value.get("mutation_outcome") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_status_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_event_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    ):
        return _compact_mapping(
            value,
            (
                "status",
                "cancellation_required",
                "cancel_called",
                "mutation_occurred",
            ),
        )
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
            "mutation_outcome",
            "mutation_occurred",
            "bind_reservation_count",
            "update_job_status_count",
            "reserved_unbound_mutation_count",
            "inflight_mutation_count",
            "pipeline_status_writes",
            "pipeline_event_writes",
            "pipeline_status_write_outcome",
            "pipeline_event_write_outcome",
            "pipeline_status_write_count",
            "pipeline_event_write_count",
            "pipeline_status_writes_proven_absent",
            "pipeline_event_writes_proven_absent",
            "error_fields",
        ),
    )


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
        "reason": _scheduler_evidence._RETAINED_FIELD_SUMMARY_REASON,
    }
    if isinstance(value, Mapping):
        summary["omitted_key_count"] = len(value)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        summary["omitted_item_count"] = len(value)
    elif value is None:
        summary["original_value"] = None
    else:
        summary["omitted_value_type"] = type(value).__name__
    if field_name in {
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
        "restart_reconcile_proof",
    }:
        summary["proof_status"] = _mapping_status(value)
    elif field_name in {"evidence_pre_execution", "root_preflight", "readiness"}:
        summary["source_status"] = _mapping_status(value)
    return summary


def _minimal_bounded_retained_field_summary() -> dict[str, str]:
    return {
        "status": "omitted",
        "reason": _scheduler_evidence._RETAINED_FIELD_SUMMARY_REASON,
    }


def _mapping_status(value: Any) -> str | None:
    if isinstance(value, Mapping):
        status = value.get("status")
        if status not in (None, ""):
            return str(status)
    return None


def _call_bounded_evidence_payload(
    context: Any,
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
    return _scheduler_evidence.bounded_evidence_payload(
        payload,
        reason=reason,
        max_evidence_bytes=context.max_evidence_bytes,
    )


def bounded_evidence_payload(
    payload: Mapping[str, Any],
    *,
    reason: str,
    max_evidence_bytes: int = _scheduler_evidence.MAX_EVIDENCE_BYTES,
) -> dict[str, Any]:
    bounded_payload = {
        "schema_version": payload.get(
            "schema_version",
            _scheduler_evidence.SCHEDULER_EVIDENCE_SCHEMA_VERSION,
        ),
        "review_contract": payload.get(
            "review_contract",
            {
                "contract_id": _scheduler_evidence.SCHEDULER_EVIDENCE_CONTRACT_ID,
                "github_issue": _scheduler_evidence.SCHEDULER_EVIDENCE_GITHUB_ISSUE,
                "openspec_change": _scheduler_evidence.SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE,
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
        "counts": payload.get("counts", _scheduler_evidence.empty_counts()),
        "resolved_runtime_roots": payload.get("resolved_runtime_roots"),
        "runtime_config": payload.get("runtime_config"),
        "db_free_runtime": payload.get("db_free_runtime"),
        "root_preflight": payload.get("root_preflight"),
        "evidence_pre_execution": payload.get("evidence_pre_execution"),
        "candidates": [],
        "blocked_candidates": [],
        "skipped_candidates": [],
        "duplicate_exclusions": payload.get("duplicate_exclusions", []),
        "source_cycles": [],
        "model_discovery": _scheduler_evidence.empty_model_discovery(),
        "artifact_path": payload.get("artifact_path"),
        "execution_boundary": payload.get("execution_boundary", "planning_only"),
        "execution_write_proof": payload.get("execution_write_proof"),
        "slurm_status_sync_proof": payload.get("slurm_status_sync_proof"),
        "slurm_cancellation_proof": payload.get("slurm_cancellation_proof"),
        "restart_reconcile_proof": payload.get("restart_reconcile_proof"),
        "no_mutation_proof": payload.get("no_mutation_proof", _scheduler_evidence.no_mutation_proof()),
        "retention": payload.get("retention"),
    }
    if "db_free_runtime" not in payload:
        bounded_payload.pop("db_free_runtime", None)
    if "retention" not in payload:
        bounded_payload.pop("retention", None)
    if "restart_reconcile_proof" not in payload:
        bounded_payload.pop("restart_reconcile_proof", None)
    return _fit_bounded_evidence_payload(bounded_payload, max_evidence_bytes=max_evidence_bytes)


__all__ = [
    "_bounded_retained_field_summary",
    "_call_bounded_evidence_payload",
    "_compact_counts",
    "_compact_limit",
    "_compact_mapping",
    "_compact_db_free_runtime",
    "_compact_retention",
    "_compact_required_bounded_field",
    "_compact_required_bounded_fields",
    "_compact_resolved_runtime_roots",
    "_compact_retained_bounded_field",
    "_compact_review_contract",
    "_compact_root_preflight",
    "_drop_empty_optional_bounded_fields",
    "_drop_not_required_optional_proofs",
    "_fit_bounded_evidence_payload",
    "_is_required_bounded_field",
    "_mapping_status",
    "_minimal_bounded_retained_field_summary",
    "_payload_fits",
    "_serialized_evidence_within_limit",
    "_serialize_evidence_json",
    "_serialize_evidence_json_if_within_limit",
    "bounded_evidence_payload",
]
