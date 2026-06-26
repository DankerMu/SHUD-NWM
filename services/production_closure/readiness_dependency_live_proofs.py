from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping, Sequence

from services.production_closure import (
    readiness_dependency_summaries as _readiness_dependency_summaries,
)
from services.production_closure import (
    readiness_live_proofs as _readiness_live_proofs,
)
from services.production_closure import (
    readiness_shared_artifacts as _readiness_shared_artifacts,
)

PROOF_CONTRACTS = _readiness_live_proofs.PROOF_CONTRACTS
DEPENDENCY_SUMMARY_CONTRACTS = _readiness_dependency_summaries.DEPENDENCY_SUMMARY_CONTRACTS
DEPENDENCY_PROOF_KEYS = frozenset({"slurm", "object_store", "source", "e2e", "mvt"})
DEPENDENCY_BINDING_ALIAS_GROUPS: Mapping[str, tuple[str, ...]] = {
    "dependency": ("dependency_surface", "dependency_name", "dependency"),
    "producer_issue": ("producer_issue", "summary_issue"),
    "producer_schema": ("producer_schema", "summary_schema"),
    "producer_run_id": ("producer_run_id", "summary_run_id"),
    "producer_artifact_ref": (
        "producer_artifact_ref",
        "producer_artifact_path",
        "producer_artifact_uri",
        "summary_ref",
        "summary_path",
        "artifact_ref",
        "artifact_path",
        "artifact_uri",
    ),
    "producer_checksum_or_receipt_id": (
        "summary_checksum",
        "producer_checksum",
        "checksum",
        "digest",
        "producer_receipt_id",
        "receipt_id",
    ),
}
DEPENDENCY_BINDING_ALIAS_ERROR_SUFFIXES = {
    "dependency": "dependency_alias_mismatch",
    "producer_issue": "producer_issue_alias_mismatch",
    "producer_schema": "producer_schema_alias_mismatch",
    "producer_run_id": "producer_run_id_alias_mismatch",
    "producer_artifact_ref": "producer_artifact_ref_alias_mismatch",
    "producer_checksum_or_receipt_id": "producer_checksum_or_receipt_id_alias_mismatch",
}

_non_empty_string = _readiness_live_proofs._non_empty_string
_has_meaningful_value = _readiness_live_proofs._has_meaningful_value
_receipt_validation_payload = _readiness_shared_artifacts._receipt_validation_payload
_receipt_details = _readiness_shared_artifacts._receipt_details


def _surface_live_item(
    config: Any,
    receipt: Mapping[str, Any],
    *,
    proof_key: str,
    dependency_bindings: Mapping[str, Mapping[str, Any]],
    item_id: str,
    surface: str,
    missing_risk: str,
    removal: str,
    surface_live_receipt_errors: Callable[..., list[str]] | None = None,
    required_live_blocker: Callable[..., dict[str, Any]] | None = None,
    receipt_validation_payload: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    receipt_details: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    _ensure_dependency_proof_key(proof_key)
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
        dependency_bindings=dependency_bindings,
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
    common_live_receipt_errors: Callable[..., list[str]] | None = None,
    dependency_receipt_errors: Callable[..., list[str]] | None = None,
) -> list[str]:
    _ensure_dependency_proof_key(proof_key)
    common_live_receipt_errors = common_live_receipt_errors or _readiness_live_proofs._common_live_receipt_errors
    dependency_receipt_errors = dependency_receipt_errors or _dependency_receipt_errors
    errors = common_live_receipt_errors(payload, proof_key=proof_key, config=config)
    errors.extend(
        dependency_receipt_errors(payload, proof_key=proof_key, dependency_bindings=dependency_bindings)
    )
    return errors


def _dependency_receipt_errors(
    payload: Mapping[str, Any],
    *,
    proof_key: str,
    dependency_bindings: Mapping[str, Mapping[str, Any]],
    proof_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    dependency_summary_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    issue_matches: Callable[[Any, int], bool] | None = None,
    contains_placeholder_value: Callable[[Any], bool] | None = None,
    non_empty_string: Callable[[Any], bool] | None = None,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> list[str]:
    proof_contracts = proof_contracts or PROOF_CONTRACTS
    dependency_summary_contracts = dependency_summary_contracts or DEPENDENCY_SUMMARY_CONTRACTS
    _ensure_dependency_proof_key(
        proof_key,
        proof_contracts=proof_contracts,
        dependency_summary_contracts=dependency_summary_contracts,
    )
    issue_matches = issue_matches or _issue_matches
    non_empty_string = non_empty_string or _non_empty_string
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    contains_placeholder_value = contains_placeholder_value or (
        lambda value: _contains_placeholder_value(value, has_meaningful_value=has_meaningful_value)
    )

    errors: list[str] = []
    expected_dependency = str(proof_contracts[proof_key]["dependency"])
    contract = dependency_summary_contracts[proof_key]
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), Mapping) else {}
    top_level_binding = _dependency_producer_binding(payload, has_meaningful_value=has_meaningful_value)
    provenance_binding = _dependency_producer_binding(provenance, has_meaningful_value=has_meaningful_value)
    binding_values = {
        field: _coalesced_binding_value(
            top_level_binding,
            provenance_binding,
            field,
            has_meaningful_value=has_meaningful_value,
        )
        for field in DEPENDENCY_BINDING_ALIAS_GROUPS
    }
    errors.extend(_dependency_binding_alias_errors(top_level_binding, source="top_level"))
    errors.extend(_dependency_binding_alias_errors(provenance_binding, source="provenance"))
    errors.extend(_dependency_binding_consistency_errors(top_level_binding, provenance_binding))

    dependency = binding_values["dependency"]
    if dependency != expected_dependency:
        errors.append("dependency_surface_mismatch")

    producer_issue = binding_values["producer_issue"]
    if not issue_matches(producer_issue, int(contract["issue"])):
        errors.append("producer_issue_mismatch")

    producer_schema = binding_values["producer_schema"]
    if producer_schema != contract["schema"]:
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

    binding = dependency_bindings.get(expected_dependency)
    if binding:
        if producer_run_id != binding.get("summary_run_id"):
            errors.append("producer_run_id_mismatch")
        if artifact_ref != binding.get("producer_artifact_ref"):
            errors.append("producer_artifact_ref_mismatch")
        if checksum_or_receipt != binding.get("summary_checksum"):
            errors.append("producer_checksum_mismatch")
        errors.extend(_dependency_binding_summary_errors(top_level_binding, binding, source="top_level"))
        errors.extend(_dependency_binding_summary_errors(provenance_binding, binding, source="provenance"))
    return errors


def _coalesced_binding_value(
    top_level_binding: Mapping[str, Any],
    provenance_binding: Mapping[str, Any],
    field: str,
    *,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> Any:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    top_value = _binding_canonical_value(top_level_binding, field, has_meaningful_value=has_meaningful_value)
    if has_meaningful_value(top_value):
        return top_value
    return _binding_canonical_value(provenance_binding, field, has_meaningful_value=has_meaningful_value)


def _dependency_producer_binding(
    payload: Mapping[str, Any],
    *,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> dict[str, dict[str, Any]]:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    return {
        field: {
            key: _normalized_binding_value(payload.get(key), field=field)
            for key in aliases
            if has_meaningful_value(payload.get(key))
        }
        for field, aliases in DEPENDENCY_BINDING_ALIAS_GROUPS.items()
    }


def _binding_values(receipt_binding: Mapping[str, Any], field: str) -> dict[str, Any]:
    values = receipt_binding.get(field)
    return values if isinstance(values, dict) else {}


def _binding_canonical_value(
    receipt_binding: Mapping[str, Any],
    field: str,
    *,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> Any:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    values = _binding_values(receipt_binding, field)
    for alias in DEPENDENCY_BINDING_ALIAS_GROUPS[field]:
        value = values.get(alias)
        if has_meaningful_value(value):
            return value
    return None


def _normalized_binding_value(value: Any, *, field: str) -> Any:
    if value is None:
        return None
    if field == "producer_issue":
        if isinstance(value, str):
            return value.strip().lstrip("#")
        return str(value).strip()
    if isinstance(value, str):
        return value.strip()
    return value


def _dependency_binding_alias_errors(receipt_binding: Mapping[str, Any], *, source: str) -> list[str]:
    errors: list[str] = []
    for binding_field, suffix in DEPENDENCY_BINDING_ALIAS_ERROR_SUFFIXES.items():
        values = list(_binding_values(receipt_binding, binding_field).values())
        if values and any(value != values[0] for value in values[1:]):
            errors.append(f"{source}_{suffix}")
    return errors


def _dependency_binding_consistency_errors(
    top_level_binding: Mapping[str, Any],
    provenance_binding: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    for binding_field, error in (
        ("dependency", "provenance_dependency_mismatch"),
        ("producer_issue", "provenance_producer_issue_mismatch"),
        ("producer_schema", "provenance_producer_schema_mismatch"),
        ("producer_run_id", "provenance_producer_run_id_mismatch"),
        ("producer_artifact_ref", "provenance_producer_artifact_ref_mismatch"),
        ("producer_checksum_or_receipt_id", "provenance_producer_checksum_or_receipt_id_mismatch"),
    ):
        top_values = list(_binding_values(top_level_binding, binding_field).values())
        provenance_values = list(_binding_values(provenance_binding, binding_field).values())
        if top_values and provenance_values and any(
            top_value != provenance_value for top_value in top_values for provenance_value in provenance_values
        ):
            errors.append(error)
    return errors


def _dependency_binding_summary_errors(
    receipt_binding: Mapping[str, Any],
    summary_binding: Mapping[str, Any],
    *,
    source: str,
) -> list[str]:
    errors: list[str] = []
    for binding_field, summary_field, error_suffix in (
        ("producer_run_id", "summary_run_id", "producer_run_id_mismatch"),
        ("producer_artifact_ref", "producer_artifact_ref", "producer_artifact_ref_mismatch"),
        ("producer_checksum_or_receipt_id", "summary_checksum", "producer_checksum_mismatch"),
    ):
        summary_value = summary_binding.get(summary_field)
        values = list(_binding_values(receipt_binding, binding_field).values())
        if values and any(value != summary_value for value in values):
            errors.append(f"{source}_summary_{error_suffix}")
    return errors


def _issue_matches(value: Any, expected: int) -> bool:
    if value == expected:
        return True
    if isinstance(value, str):
        return value.strip().lstrip("#") == str(expected)
    return False


def _contains_placeholder_value(
    value: Any,
    *,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> bool:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    placeholders = {"placeholder", "fabricated", "fake", "dummy", "todo", "tbd", "unknown", "null", "none"}
    if isinstance(value, str):
        stripped = value.strip().lower()
        return stripped in placeholders or stripped.startswith("placeholder-")
    if isinstance(value, Mapping):
        meaningful = [nested for nested in value.values() if has_meaningful_value(nested)]
        return bool(meaningful) and all(
            _contains_placeholder_value(nested, has_meaningful_value=has_meaningful_value)
            for nested in meaningful
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        meaningful = [nested for nested in value if has_meaningful_value(nested)]
        return bool(meaningful) and all(
            _contains_placeholder_value(nested, has_meaningful_value=has_meaningful_value)
            for nested in meaningful
        )
    return False


def _ensure_dependency_proof_key(
    proof_key: str,
    *,
    proof_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    dependency_summary_contracts: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    proof_contracts = proof_contracts or PROOF_CONTRACTS
    dependency_summary_contracts = dependency_summary_contracts or DEPENDENCY_SUMMARY_CONTRACTS
    if (
        proof_key not in DEPENDENCY_PROOF_KEYS
        or proof_key not in proof_contracts
        or proof_key not in dependency_summary_contracts
    ):
        raise ValueError(f"unsupported dependency proof key: {proof_key}")
