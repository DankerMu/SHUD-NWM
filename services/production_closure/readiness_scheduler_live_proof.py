from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping, Sequence

from services.production_closure import (
    readiness_dependency_live_proofs as _readiness_dependency_live_proofs,
)
from services.production_closure import (
    readiness_live_proofs as _readiness_live_proofs,
)
from services.production_closure import (
    readiness_scheduler_evidence as _readiness_scheduler_evidence,
)
from services.production_closure import (
    readiness_shared_artifacts as _readiness_shared_artifacts,
)

PROOF_CONTRACTS = _readiness_live_proofs.PROOF_CONTRACTS
SCHEDULER_EVIDENCE_SCHEMA = _readiness_scheduler_evidence.SCHEDULER_EVIDENCE_SCHEMA
SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES = (
    _readiness_scheduler_evidence.SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES
)
SCHEDULER_LIVE_WORK_STATUSES = _readiness_scheduler_evidence.SCHEDULER_LIVE_WORK_STATUSES

SCHEDULER_BINDING_ALIAS_GROUPS: Mapping[str, tuple[str, ...]] = {
    "producer_schema": ("producer_schema", "scheduler_schema"),
    "producer_run_id": ("producer_run_id", "scheduler_pass_id", "pass_id"),
    "producer_artifact_ref": (
        "producer_artifact_ref",
        "producer_artifact_path",
        "producer_artifact_uri",
        "scheduler_artifact_ref",
        "scheduler_artifact_path",
        "artifact_ref",
        "artifact_path",
        "artifact_uri",
    ),
    "producer_checksum_or_receipt_id": (
        "scheduler_checksum",
        "producer_checksum",
        "summary_checksum",
        "checksum",
        "digest",
        "producer_receipt_id",
        "receipt_id",
    ),
}
SCHEDULER_BINDING_ALIAS_ERROR_SUFFIXES = {
    "producer_schema": "producer_schema_alias_mismatch",
    "producer_run_id": "producer_run_id_alias_mismatch",
    "producer_artifact_ref": "producer_artifact_ref_alias_mismatch",
    "producer_checksum_or_receipt_id": "producer_checksum_or_receipt_id_alias_mismatch",
}

_non_empty_string = _readiness_live_proofs._non_empty_string
_has_meaningful_value = _readiness_live_proofs._has_meaningful_value
_contains_placeholder_value = _readiness_dependency_live_proofs._contains_placeholder_value
_normalized_binding_value = _readiness_dependency_live_proofs._normalized_binding_value
_receipt_validation_payload = _readiness_shared_artifacts._receipt_validation_payload
_receipt_details = _readiness_shared_artifacts._receipt_details


def _surface_live_item(
    config: Any,
    receipt: Mapping[str, Any],
    *,
    proof_key: str,
    dependency_bindings: Mapping[str, Mapping[str, Any]],
    scheduler_binding: Sequence[Mapping[str, Any]] = (),
    item_id: str,
    surface: str,
    missing_risk: str,
    removal: str,
    surface_live_receipt_errors: Callable[..., list[str]] | None = None,
    required_live_blocker: Callable[..., dict[str, Any]] | None = None,
    receipt_validation_payload: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    receipt_details: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    _ensure_scheduler_proof_key(proof_key)
    del dependency_bindings
    surface_live_receipt_errors = surface_live_receipt_errors or _surface_live_receipt_errors
    required_live_blocker = required_live_blocker or _readiness_live_proofs._required_live_blocker
    receipt_validation_payload = receipt_validation_payload or _receipt_validation_payload
    receipt_details = receipt_details or _receipt_details
    base = {
        "item_id": item_id,
        "surface": surface,
        "required_for_final": True,
        "artifact_refs": ["live_proof_receipts.json"],
        "residual_risk": missing_risk,
        "removal_criteria": removal,
    }
    if receipt["status"] != "parsed":
        return required_live_blocker(config=config, receipt=receipt, **base)
    payload = receipt_validation_payload(receipt)
    errors = surface_live_receipt_errors(
        payload,
        proof_key=proof_key,
        config=config,
        dependency_bindings={},
        scheduler_binding=scheduler_binding,
    )
    if not errors:
        return _readiness_live_proofs._item(
            item_id=base["item_id"],
            surface=base["surface"],
            required_for_final=base["required_for_final"],
            artifact_refs=base["artifact_refs"],
            status="passed",
            execution_mode="live_proof",
            live_proof_accepted=True,
            residual_risk=f"Accepted live proof is present for {surface}.",
            removal_criteria="Keep the accepted live proof receipt attached to the release evidence bundle.",
            details=receipt_details(receipt, config=config),
        )
    return _readiness_live_proofs._item(
        **base,
        status="release_blocked",
        execution_mode="live_proof",
        live_proof_accepted=False,
        details=receipt_details({**receipt, "acceptance_errors": {"errors": errors}}, config=config),
    )


def _surface_live_receipt_errors(
    payload: Mapping[str, Any],
    *,
    proof_key: str,
    config: Any,
    dependency_bindings: Mapping[str, Mapping[str, Any]],
    scheduler_binding: Sequence[Mapping[str, Any]] = (),
    common_live_receipt_errors: Callable[..., list[str]] | None = None,
    scheduler_receipt_errors: Callable[..., list[str]] | None = None,
) -> list[str]:
    _ensure_scheduler_proof_key(proof_key)
    del dependency_bindings
    common_live_receipt_errors = common_live_receipt_errors or _readiness_live_proofs._common_live_receipt_errors
    scheduler_receipt_errors = scheduler_receipt_errors or _scheduler_receipt_errors
    errors = common_live_receipt_errors(payload, proof_key=proof_key, config=config)
    errors.extend(scheduler_receipt_errors(payload, scheduler_binding=scheduler_binding))
    return errors


def _scheduler_receipt_errors(
    payload: Mapping[str, Any],
    *,
    scheduler_binding: Sequence[Mapping[str, Any]],
    contains_placeholder_value: Callable[[Any], bool] | None = None,
    non_empty_string: Callable[[Any], bool] | None = None,
    has_meaningful_value: Callable[[Any], bool] | None = None,
    normalized_binding_value: Callable[..., Any] | None = None,
) -> list[str]:
    non_empty_string = non_empty_string or _non_empty_string
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    contains_placeholder_value = contains_placeholder_value or (
        lambda value: _contains_placeholder_value(value, has_meaningful_value=has_meaningful_value)
    )
    normalized_binding_value = normalized_binding_value or _normalized_binding_value

    errors: list[str] = []
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), Mapping) else {}
    top_level_binding = _scheduler_producer_binding(
        payload,
        has_meaningful_value=has_meaningful_value,
        normalized_binding_value=normalized_binding_value,
    )
    provenance_binding = _scheduler_producer_binding(
        provenance,
        has_meaningful_value=has_meaningful_value,
        normalized_binding_value=normalized_binding_value,
    )
    binding_values = {
        field: _coalesced_scheduler_binding_value(
            top_level_binding,
            provenance_binding,
            field,
            has_meaningful_value=has_meaningful_value,
        )
        for field in SCHEDULER_BINDING_ALIAS_GROUPS
    }
    errors.extend(_scheduler_binding_alias_errors(top_level_binding, source="top_level"))
    errors.extend(_scheduler_binding_alias_errors(provenance_binding, source="provenance"))
    errors.extend(_scheduler_binding_consistency_errors(top_level_binding, provenance_binding))

    producer_schema = binding_values["producer_schema"]
    if producer_schema != SCHEDULER_EVIDENCE_SCHEMA:
        errors.append("producer_schema_mismatch")

    producer_run_id = binding_values["producer_run_id"]
    if not non_empty_string(producer_run_id):
        errors.append("missing_producer_run_id")

    artifact_ref = binding_values["producer_artifact_ref"]
    if not non_empty_string(artifact_ref):
        errors.append("missing_producer_artifact_ref")

    checksum_or_receipt = binding_values["producer_checksum_or_receipt_id"]
    if not non_empty_string(checksum_or_receipt):
        errors.append("missing_producer_checksum_or_receipt_id")

    if not has_meaningful_value(provenance):
        errors.append("missing_provenance")
    elif contains_placeholder_value(provenance):
        errors.append("placeholder_provenance")

    producer_run_matches = [
        binding for binding in scheduler_binding if producer_run_id == binding.get("scheduler_pass_id")
    ]
    artifact_matches = [
        binding for binding in producer_run_matches if artifact_ref == binding.get("scheduler_artifact_ref")
    ]
    matches = [
        binding
        for binding in artifact_matches
        if checksum_or_receipt == binding.get("scheduler_checksum")
    ]
    if not scheduler_binding:
        errors.append("missing_scheduler_evidence_binding")
    elif not matches:
        errors.append("scheduler_evidence_binding_not_found")
        if not producer_run_matches:
            errors.append("producer_run_id_mismatch")
        elif not artifact_matches:
            errors.append("producer_artifact_ref_mismatch")
        else:
            errors.append("producer_checksum_mismatch")
    else:
        if len(matches) > 1:
            errors.append("ambiguous_scheduler_evidence_binding")
        binding = matches[0]
        if producer_schema != binding.get("scheduler_schema"):
            errors.append("producer_schema_mismatch")
        scheduler_mode = binding.get("scheduler_execution_mode")
        if scheduler_mode not in SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES:
            errors.append("scheduler_execution_mode_not_live_eligible")
        scheduler_status = str(binding.get("scheduler_status") or "").strip().lower()
        if scheduler_status not in SCHEDULER_LIVE_WORK_STATUSES:
            errors.append("scheduler_status_not_live_eligible")
        errors.extend(_scheduler_binding_summary_errors(top_level_binding, binding, source="top_level"))
        errors.extend(_scheduler_binding_summary_errors(provenance_binding, binding, source="provenance"))
    return errors


def _coalesced_scheduler_binding_value(
    top_level_binding: Mapping[str, Any],
    provenance_binding: Mapping[str, Any],
    field: str,
    *,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> Any:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    top_value = _scheduler_binding_canonical_value(
        top_level_binding,
        field,
        has_meaningful_value=has_meaningful_value,
    )
    if has_meaningful_value(top_value):
        return top_value
    return _scheduler_binding_canonical_value(
        provenance_binding,
        field,
        has_meaningful_value=has_meaningful_value,
    )


def _scheduler_producer_binding(
    payload: Mapping[str, Any],
    *,
    has_meaningful_value: Callable[[Any], bool] | None = None,
    normalized_binding_value: Callable[..., Any] | None = None,
) -> dict[str, dict[str, Any]]:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    normalized_binding_value = normalized_binding_value or _normalized_binding_value
    return {
        field: {
            key: normalized_binding_value(payload.get(key), field=field)
            for key in aliases
            if has_meaningful_value(payload.get(key))
        }
        for field, aliases in SCHEDULER_BINDING_ALIAS_GROUPS.items()
    }


def _scheduler_binding_values(receipt_binding: Mapping[str, Any], field: str) -> dict[str, Any]:
    values = receipt_binding.get(field)
    return values if isinstance(values, dict) else {}


def _scheduler_binding_canonical_value(
    receipt_binding: Mapping[str, Any],
    field: str,
    *,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> Any:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    values = _scheduler_binding_values(receipt_binding, field)
    for alias in SCHEDULER_BINDING_ALIAS_GROUPS[field]:
        value = values.get(alias)
        if has_meaningful_value(value):
            return value
    return None


def _scheduler_binding_alias_errors(receipt_binding: Mapping[str, Any], *, source: str) -> list[str]:
    errors: list[str] = []
    for binding_field, suffix in SCHEDULER_BINDING_ALIAS_ERROR_SUFFIXES.items():
        values = list(_scheduler_binding_values(receipt_binding, binding_field).values())
        if values and any(value != values[0] for value in values[1:]):
            errors.append(f"{source}_{suffix}")
    return errors


def _scheduler_binding_consistency_errors(
    top_level_binding: Mapping[str, Any],
    provenance_binding: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    for binding_field, error in (
        ("producer_schema", "provenance_producer_schema_mismatch"),
        ("producer_run_id", "provenance_producer_run_id_mismatch"),
        ("producer_artifact_ref", "provenance_producer_artifact_ref_mismatch"),
        ("producer_checksum_or_receipt_id", "provenance_producer_checksum_or_receipt_id_mismatch"),
    ):
        top_values = list(_scheduler_binding_values(top_level_binding, binding_field).values())
        provenance_values = list(_scheduler_binding_values(provenance_binding, binding_field).values())
        if top_values and provenance_values and any(
            top_value != provenance_value for top_value in top_values for provenance_value in provenance_values
        ):
            errors.append(error)
    return errors


def _scheduler_binding_summary_errors(
    receipt_binding: Mapping[str, Any],
    summary_binding: Mapping[str, Any],
    *,
    source: str,
) -> list[str]:
    errors: list[str] = []
    for binding_field, summary_field, error_suffix in (
        ("producer_schema", "scheduler_schema", "producer_schema_mismatch"),
        ("producer_run_id", "scheduler_pass_id", "producer_run_id_mismatch"),
        ("producer_artifact_ref", "scheduler_artifact_ref", "producer_artifact_ref_mismatch"),
        ("producer_checksum_or_receipt_id", "scheduler_checksum", "producer_checksum_mismatch"),
    ):
        summary_value = summary_binding.get(summary_field)
        values = list(_scheduler_binding_values(receipt_binding, binding_field).values())
        if values and any(value != summary_value for value in values):
            errors.append(f"{source}_summary_{error_suffix}")
    return errors


def _ensure_scheduler_proof_key(
    proof_key: str,
    *,
    proof_contracts: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    proof_contracts = proof_contracts or PROOF_CONTRACTS
    if proof_key != "scheduler" or proof_key not in proof_contracts:
        raise ValueError(f"unsupported scheduler proof key: {proof_key}")
