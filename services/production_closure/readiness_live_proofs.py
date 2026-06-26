from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from services.production_closure import (
    readiness_item_contracts as _readiness_item_contracts,
)
from services.production_closure import (
    readiness_shared_artifacts as _readiness_shared_artifacts,
)

LIVE_PROOF_SCHEMA = _readiness_shared_artifacts.LIVE_PROOF_SCHEMA
_receipt_details = _readiness_shared_artifacts._receipt_details
_receipt_validation_payload = _readiness_shared_artifacts._receipt_validation_payload
validate_readiness_item = _readiness_item_contracts.validate_readiness_item

EXPECTED_TARGET_ENVIRONMENT = "production"
PROOF_SPECIFIC_KEYS = frozenset({"auth", "alert", "rollback", "target_env"})
SURFACE_PROOF_KEYS = frozenset({"alert", "rollback", "target_env"})
PROOF_CONTRACTS = {
    "auth": {
        "proof_type": "auth",
        "surface": "live_backend_auth",
        "allowed_statuses": {"passed"},
    },
    "alert": {
        "proof_type": "alert",
        "surface": "live_alert_sink_delivery",
        "allowed_statuses": {"passed", "delivered"},
    },
    "rollback": {
        "proof_type": "rollback",
        "surface": "live_rollback_execution",
        "allowed_statuses": {"passed", "executed"},
    },
    "scheduler": {
        "proof_type": "scheduler_evidence",
        "surface": "live_scheduler_evidence_proof",
        "allowed_statuses": {"passed", "accepted", "ready", "submitted", "completed"},
    },
    "slurm": {
        "proof_type": "dependency",
        "surface": "live_slurm_dependency_proof",
        "dependency": "slurm",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
    "object_store": {
        "proof_type": "dependency",
        "surface": "live_object_store_dependency_proof",
        "dependency": "object_store",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
    "source": {
        "proof_type": "dependency",
        "surface": "live_source_weather_dependency_proof",
        "dependency": "source",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
    "e2e": {
        "proof_type": "dependency",
        "surface": "live_e2e_dependency_proof",
        "dependency": "e2e",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
    "mvt": {
        "proof_type": "dependency",
        "surface": "live_mvt_performance_proof",
        "dependency": "mvt",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
    "target_env": {
        "proof_type": "target_env",
        "surface": "target_environment_config_proof",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
}
REQUIRED_AUTH_ACTIONS = frozenset(
    {
        "pipeline.retry_run",
        "pipeline.cancel_run",
        "pipeline.rerun_cycle",
        "qc.override_result",
        "tiles.republish",
        "sources.update_config",
        "models.activate",
        "models.deactivate",
        "models.switch_version",
        "models.rollback_version",
        "models.supersede",
        "users.manage",
    }
)


def _auth_live_item(
    config: Any,
    receipt: Mapping[str, Any],
    *,
    required_auth_actions: set[str] | frozenset[str] | None = None,
    string_set: Callable[[Any], set[str]] | None = None,
    has_meaningful_value: Callable[[Any], bool] | None = None,
    first_meaningful_mapping: Callable[[Mapping[str, Any], Sequence[str]], Mapping[str, Any] | None] | None = None,
    has_any_key_value: Callable[[Mapping[str, Any], Sequence[str]], bool] | None = None,
    non_empty_string: Callable[[Any], bool] | None = None,
    common_live_receipt_errors: Callable[..., list[str]] | None = None,
    provider_metadata_is_meaningful: Callable[[Mapping[str, Any]], bool] | None = None,
    role_mapping_is_meaningful: Callable[[Any], bool] | None = None,
    required_live_blocker: Callable[..., dict[str, Any]] | None = None,
    receipt_validation_payload: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    receipt_details: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if required_auth_actions is None:
        required_auth_actions = REQUIRED_AUTH_ACTIONS
    required_auth_actions = frozenset(required_auth_actions)
    string_set = string_set or _string_set
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    first_meaningful_mapping = first_meaningful_mapping or (
        lambda payload, keys: _first_meaningful_mapping(payload, keys, has_meaningful_value=has_meaningful_value)
    )
    has_any_key_value = has_any_key_value or (
        lambda mapping, keys: _has_any_key_value(mapping, keys, has_meaningful_value=has_meaningful_value)
    )
    non_empty_string = non_empty_string or _non_empty_string
    common_live_receipt_errors = common_live_receipt_errors or _common_live_receipt_errors
    provider_metadata_is_meaningful = provider_metadata_is_meaningful or (
        lambda payload: _provider_metadata_is_meaningful(
            payload,
            first_meaningful_mapping=first_meaningful_mapping,
            has_any_key_value=has_any_key_value,
        )
    )
    role_mapping_is_meaningful = role_mapping_is_meaningful or (
        lambda value: _role_mapping_is_meaningful(
            value,
            non_empty_string=non_empty_string,
            string_set=string_set,
        )
    )
    required_live_blocker = required_live_blocker or _required_live_blocker
    receipt_validation_payload = receipt_validation_payload or _receipt_validation_payload
    receipt_details = receipt_details or _receipt_details
    base = {
        "item_id": "live-backend-auth",
        "surface": "live_backend_auth",
        "required_for_final": True,
        "artifact_refs": ["live_proof_receipts.json"],
        "residual_risk": "Live backend IdP proof is missing or incomplete.",
        "removal_criteria": (
            "Provide accepted live auth proof with provider metadata plus allowed and denied coverage for every "
            "canonical protected action."
        ),
    }
    if receipt["status"] != "parsed":
        return required_live_blocker(config=config, receipt=receipt, **base)
    payload = receipt_validation_payload(receipt)
    allowed = set(string_set(payload.get("allowed_actions") or payload.get("allowed_coverage")))
    denied = set(string_set(payload.get("denied_actions") or payload.get("denied_coverage")))
    missing_allowed = sorted(required_auth_actions - allowed)
    missing_denied = sorted(required_auth_actions - denied)
    errors = common_live_receipt_errors(payload, proof_key="auth", config=config)
    if not provider_metadata_is_meaningful(payload):
        errors.append("missing_provider_metadata")
    if not role_mapping_is_meaningful(payload.get("role_mapping")) and not role_mapping_is_meaningful(
        payload.get("role_mappings")
    ):
        errors.append("missing_role_mapping")
    if missing_allowed:
        errors.append("missing_allowed_actions")
    if missing_denied:
        errors.append("missing_denied_actions")
    accepted = not errors
    if accepted:
        return _item(
            item_id=base["item_id"],
            surface=base["surface"],
            required_for_final=base["required_for_final"],
            artifact_refs=base["artifact_refs"],
            status="passed",
            execution_mode="live_proof",
            live_proof_accepted=True,
            residual_risk="Accepted live auth proof is present for required protected action coverage.",
            removal_criteria="Keep the accepted live auth receipt attached to the release evidence bundle.",
            details=receipt_details(receipt, config=config),
        )
    return _item(
        **base,
        status="release_blocked",
        execution_mode="live_proof",
        live_proof_accepted=False,
        details=receipt_details(
            {
                **receipt,
                "acceptance_errors": {
                    "errors": errors,
                    "accepted": payload.get("accepted") is True,
                    "missing_allowed_actions": missing_allowed,
                    "missing_denied_actions": missing_denied,
                },
            },
            config=config,
        ),
    )


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
    if proof_key not in SURFACE_PROOF_KEYS:
        raise ValueError(f"unsupported surface proof key: {proof_key}")
    surface_live_receipt_errors = surface_live_receipt_errors or _surface_live_receipt_errors
    required_live_blocker = required_live_blocker or _required_live_blocker
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
        scheduler_binding=scheduler_binding,
    )
    if not errors:
        return _item(
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
    return _item(
        **base,
        status="release_blocked",
        execution_mode="live_proof",
        live_proof_accepted=False,
        details=receipt_details({**receipt, "acceptance_errors": {"errors": errors}}, config=config),
    )


def _required_live_blocker(
    *,
    config: Any,
    receipt: Mapping[str, Any],
    item_id: str,
    surface: str,
    required_for_final: bool,
    artifact_refs: list[str],
    residual_risk: str,
    removal_criteria: str,
    receipt_details: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    receipt_details = receipt_details or _receipt_details
    execution_mode = "live_proof" if receipt["status"] in {"invalid", "too_large"} else "not_executed"
    return _item(
        item_id=item_id,
        surface=surface,
        status="release_blocked",
        execution_mode=execution_mode,
        required_for_final=required_for_final,
        live_proof_accepted=False,
        artifact_refs=artifact_refs,
        residual_risk=residual_risk,
        removal_criteria=removal_criteria,
        details=receipt_details(receipt, config=config),
    )


def _surface_live_receipt_errors(
    payload: Mapping[str, Any],
    *,
    proof_key: str,
    config: Any,
    dependency_bindings: Mapping[str, Mapping[str, Any]],
    scheduler_binding: Sequence[Mapping[str, Any]] = (),
    common_live_receipt_errors: Callable[..., list[str]] | None = None,
    non_empty_string: Callable[[Any], bool] | None = None,
    has_meaningful_value: Callable[[Any], bool] | None = None,
    first_meaningful_mapping: Callable[[Mapping[str, Any], Sequence[str]], Mapping[str, Any] | None] | None = None,
    has_any_key_value: Callable[[Mapping[str, Any], Sequence[str]], bool] | None = None,
    value_from: Callable[..., Any] | None = None,
    alert_sink_metadata_is_meaningful: Callable[[Mapping[str, Any]], bool] | None = None,
    alert_delivery_metadata_is_meaningful: Callable[[Mapping[str, Any]], bool] | None = None,
    rollback_command_metadata_is_meaningful: Callable[[Mapping[str, Any]], bool] | None = None,
    rollback_result_is_meaningful: Callable[[Mapping[str, Any]], bool] | None = None,
    target_env_config_metadata_is_meaningful: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[str]:
    if proof_key not in SURFACE_PROOF_KEYS:
        raise ValueError(f"unsupported surface proof key: {proof_key}")
    del dependency_bindings, scheduler_binding
    common_live_receipt_errors = common_live_receipt_errors or _common_live_receipt_errors
    non_empty_string = non_empty_string or _non_empty_string
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    first_meaningful_mapping = first_meaningful_mapping or (
        lambda payload, keys: _first_meaningful_mapping(payload, keys, has_meaningful_value=has_meaningful_value)
    )
    has_any_key_value = has_any_key_value or (
        lambda mapping, keys: _has_any_key_value(mapping, keys, has_meaningful_value=has_meaningful_value)
    )
    value_from = value_from or (
        lambda payload, keys, *, fallback=None: _value_from(
            payload,
            keys,
            fallback=fallback,
            has_meaningful_value=has_meaningful_value,
        )
    )
    alert_sink_metadata_is_meaningful = alert_sink_metadata_is_meaningful or (
        lambda payload: _alert_sink_metadata_is_meaningful(
            payload,
            first_meaningful_mapping=first_meaningful_mapping,
            has_any_key_value=has_any_key_value,
        )
    )
    alert_delivery_metadata_is_meaningful = (
        alert_delivery_metadata_is_meaningful
        or (
            lambda payload: _alert_delivery_metadata_is_meaningful(
                payload,
                first_meaningful_mapping=first_meaningful_mapping,
                has_any_key_value=has_any_key_value,
            )
        )
    )
    rollback_command_metadata_is_meaningful = (
        rollback_command_metadata_is_meaningful
        or (
            lambda payload: _rollback_command_metadata_is_meaningful(
                payload,
                first_meaningful_mapping=first_meaningful_mapping,
                has_any_key_value=has_any_key_value,
                non_empty_string=non_empty_string,
            )
        )
    )
    rollback_result_is_meaningful = rollback_result_is_meaningful or (
        lambda payload: _rollback_result_is_meaningful(
            payload,
            value_from=value_from,
            non_empty_string=non_empty_string,
        )
    )
    target_env_config_metadata_is_meaningful = (
        target_env_config_metadata_is_meaningful
        or (
            lambda payload: _target_env_config_metadata_is_meaningful(
                payload,
                first_meaningful_mapping=first_meaningful_mapping,
                has_meaningful_value=has_meaningful_value,
                has_any_key_value=has_any_key_value,
            )
        )
    )
    errors = common_live_receipt_errors(payload, proof_key=proof_key, config=config)
    if proof_key == "alert":
        if not alert_sink_metadata_is_meaningful(payload):
            errors.append("missing_sink_metadata")
        if not alert_delivery_metadata_is_meaningful(payload):
            errors.append("missing_delivery_metadata")
        if payload.get("delivered") is not True and str(payload.get("status", "")) != "delivered":
            errors.append("delivery_not_confirmed")
    elif proof_key == "rollback":
        if not has_meaningful_value(payload.get("preconditions")):
            errors.append("missing_preconditions")
        if not rollback_command_metadata_is_meaningful(payload):
            errors.append("missing_command_or_drill_metadata")
        if not rollback_result_is_meaningful(payload):
            errors.append("rollback_not_executed")
    elif proof_key == "target_env" and not target_env_config_metadata_is_meaningful(payload):
        errors.append("missing_target_environment_config_metadata")
    return errors


def _common_live_receipt_errors(
    payload: Mapping[str, Any],
    *,
    proof_key: str,
    config: Any,
    proof_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    live_proof_schema: str = LIVE_PROOF_SCHEMA,
    expected_target_environment: str = EXPECTED_TARGET_ENVIRONMENT,
    non_empty_string: Callable[[Any], bool] | None = None,
    has_meaningful_value: Callable[[Any], bool] | None = None,
    has_meaningful_ref: Callable[[Any], bool] | None = None,
    target_environment_name: Callable[[Any], str] | None = None,
    is_live_proof_mode: Callable[[Mapping[str, Any]], bool] | None = None,
    has_artifact_or_evidence_refs: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[str]:
    proof_contracts = proof_contracts or PROOF_CONTRACTS
    non_empty_string = non_empty_string or _non_empty_string
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    has_meaningful_ref = has_meaningful_ref or (
        lambda value: _has_meaningful_ref(value, has_meaningful_value=has_meaningful_value)
    )
    target_environment_name = target_environment_name or _target_environment_name
    is_live_proof_mode = is_live_proof_mode or _is_live_proof_mode
    has_artifact_or_evidence_refs = has_artifact_or_evidence_refs or (
        lambda receipt: _has_artifact_or_evidence_refs(receipt, has_meaningful_ref=has_meaningful_ref)
    )
    contract = proof_contracts[proof_key]
    errors: list[str] = []
    if payload.get("accepted") is not True:
        errors.append("accepted_not_true")
    status = payload.get("status")
    if not isinstance(status, str) or not status.strip():
        errors.append("missing_status")
    elif status not in contract["allowed_statuses"]:
        errors.append("status_not_allowed")
    if payload.get("schema") != live_proof_schema:
        errors.append("schema_mismatch")
    if payload.get("proof_type", payload.get("receipt_type")) != contract["proof_type"]:
        errors.append("proof_type_mismatch")
    if payload.get("surface") != contract["surface"]:
        errors.append("surface_mismatch")
    if payload.get("run_id") != config.run_id:
        errors.append("run_id_mismatch")
    target_environment = payload.get("target_environment")
    if not non_empty_string(target_environment) and not has_meaningful_value(target_environment):
        errors.append("missing_target_environment")
    elif target_environment_name(target_environment) != expected_target_environment:
        errors.append("target_environment_mismatch")
    if not is_live_proof_mode(payload):
        errors.append("execution_mode_not_live_proof")
    if not has_artifact_or_evidence_refs(payload):
        errors.append("missing_artifact_or_evidence_refs")
    return errors


def _is_live_proof_mode(payload: Mapping[str, Any]) -> bool:
    values = {
        str(payload.get("execution_mode", "")),
        str(payload.get("proof_mode", "")),
        str(payload.get("mode", "")),
    }
    return bool(values & {"live_proof", "live_execution", "live"})


def _has_artifact_or_evidence_refs(
    payload: Mapping[str, Any],
    *,
    has_meaningful_ref: Callable[[Any], bool] | None = None,
) -> bool:
    has_meaningful_ref = has_meaningful_ref or _has_meaningful_ref
    for key in ("artifact_refs", "evidence_refs", "artifacts", "evidence"):
        if has_meaningful_ref(payload.get(key)):
            return True
    return False


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, Mapping):
        return any(str(key).strip() and _has_meaningful_value(nested) for key, nested in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_has_meaningful_value(item) for item in value)
    return True


def _has_meaningful_ref(
    value: Any,
    *,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> bool:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        ref_keys = (
            "id",
            "ref",
            "path",
            "uri",
            "url",
            "checksum",
            "digest",
            "receipt_id",
            "artifact_ref",
            "artifact_path",
            "artifact_uri",
            "summary_path",
            "summary_ref",
            "summary_checksum",
        )
        return any(has_meaningful_value(value.get(key)) for key in ref_keys) or any(
            _has_meaningful_ref(nested, has_meaningful_value=has_meaningful_value) for nested in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_has_meaningful_ref(item, has_meaningful_value=has_meaningful_value) for item in value)
    return False


def _target_environment_name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("name", "environment", "id"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = set()
        for item in value:
            if isinstance(item, str) and item.strip():
                values.add(item.strip())
        return values
    return set()


def _provider_metadata_is_meaningful(
    payload: Mapping[str, Any],
    *,
    first_meaningful_mapping: Callable[[Mapping[str, Any], Sequence[str]], Mapping[str, Any] | None] | None = None,
    has_any_key_value: Callable[[Mapping[str, Any], Sequence[str]], bool] | None = None,
) -> bool:
    first_meaningful_mapping = first_meaningful_mapping or _first_meaningful_mapping
    has_any_key_value = has_any_key_value or _has_any_key_value
    provider = first_meaningful_mapping(payload, ("provider", "provider_metadata", "idp_metadata"))
    if provider is not None and has_any_key_value(
        provider,
        (
            "issuer",
            "issuer_url",
            "provider_id",
            "provider",
            "provider_name",
            "idp",
            "idp_id",
            "tenant_id",
            "subject",
            "client_id",
        ),
    ):
        return True
    return has_any_key_value(
        payload,
        ("issuer", "issuer_url", "provider_id", "provider_name", "idp_id", "tenant_id", "subject", "client_id"),
    )


def _role_mapping_is_meaningful(
    value: Any,
    *,
    non_empty_string: Callable[[Any], bool] | None = None,
    string_set: Callable[[Any], set[str]] | None = None,
) -> bool:
    non_empty_string = non_empty_string or _non_empty_string
    string_set = string_set or _string_set
    if not isinstance(value, Mapping):
        return False
    for role, mapped in value.items():
        if not non_empty_string(role):
            continue
        if string_set(mapped):
            return True
        if isinstance(mapped, Mapping) and any(
            string_set(mapped.get(key)) for key in ("actions", "roles", "permissions", "allowed_actions")
        ):
            return True
    return False


def _alert_sink_metadata_is_meaningful(
    payload: Mapping[str, Any],
    *,
    first_meaningful_mapping: Callable[[Mapping[str, Any], Sequence[str]], Mapping[str, Any] | None] | None = None,
    has_any_key_value: Callable[[Mapping[str, Any], Sequence[str]], bool] | None = None,
) -> bool:
    first_meaningful_mapping = first_meaningful_mapping or _first_meaningful_mapping
    has_any_key_value = has_any_key_value or _has_any_key_value
    sink = first_meaningful_mapping(payload, ("sink_metadata", "sink"))
    if sink is not None and has_any_key_value(sink, ("sink_id", "id", "name", "sink_name", "url", "uri", "channel")):
        return True
    return has_any_key_value(payload, ("sink_id", "sink_name", "sink_url", "sink", "channel"))


def _alert_delivery_metadata_is_meaningful(
    payload: Mapping[str, Any],
    *,
    first_meaningful_mapping: Callable[[Mapping[str, Any], Sequence[str]], Mapping[str, Any] | None] | None = None,
    has_any_key_value: Callable[[Mapping[str, Any], Sequence[str]], bool] | None = None,
) -> bool:
    first_meaningful_mapping = first_meaningful_mapping or _first_meaningful_mapping
    has_any_key_value = has_any_key_value or _has_any_key_value
    delivery = first_meaningful_mapping(payload, ("delivery_metadata", "delivery_result", "delivery"))
    if delivery is None:
        return False
    has_id = has_any_key_value(delivery, ("delivery_id", "message_id", "id", "receipt_id"))
    has_timestamp = has_any_key_value(delivery, ("delivered_at", "timestamp", "time", "completed_at"))
    has_result = has_any_key_value(delivery, ("result", "status", "delivery_status", "outcome"))
    return has_id and has_timestamp and has_result


def _rollback_command_metadata_is_meaningful(
    payload: Mapping[str, Any],
    *,
    first_meaningful_mapping: Callable[[Mapping[str, Any], Sequence[str]], Mapping[str, Any] | None] | None = None,
    has_any_key_value: Callable[[Mapping[str, Any], Sequence[str]], bool] | None = None,
    non_empty_string: Callable[[Any], bool] | None = None,
) -> bool:
    first_meaningful_mapping = first_meaningful_mapping or _first_meaningful_mapping
    has_any_key_value = has_any_key_value or _has_any_key_value
    non_empty_string = non_empty_string or _non_empty_string
    command = first_meaningful_mapping(payload, ("command_metadata", "drill_metadata", "command"))
    if command is not None and (
        has_any_key_value(command, ("command", "command_id", "drill_id", "id", "runbook", "rollback_id"))
        or non_empty_string(command.get("argv"))
    ):
        return True
    return has_any_key_value(payload, ("command", "command_id", "drill_id", "rollback_id"))


def _rollback_result_is_meaningful(
    payload: Mapping[str, Any],
    *,
    value_from: Callable[..., Any] | None = None,
    non_empty_string: Callable[[Any], bool] | None = None,
) -> bool:
    value_from = value_from or _value_from
    non_empty_string = non_empty_string or _non_empty_string
    if payload.get("executed") is True:
        return True
    result = value_from(payload, ("execution_result", "result", "rollback_result", "outcome"))
    if non_empty_string(result):
        return str(result).strip().lower() in {"passed", "executed", "success", "succeeded"}
    status = payload.get("status")
    return isinstance(status, str) and status.strip().lower() == "executed"


def _target_env_config_metadata_is_meaningful(
    payload: Mapping[str, Any],
    *,
    first_meaningful_mapping: Callable[[Mapping[str, Any], Sequence[str]], Mapping[str, Any] | None] | None = None,
    has_meaningful_value: Callable[[Any], bool] | None = None,
    has_any_key_value: Callable[[Mapping[str, Any], Sequence[str]], bool] | None = None,
) -> bool:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    first_meaningful_mapping = first_meaningful_mapping or (
        lambda receipt, keys: _first_meaningful_mapping(receipt, keys, has_meaningful_value=has_meaningful_value)
    )
    has_any_key_value = has_any_key_value or (
        lambda mapping, keys: _has_any_key_value(mapping, keys, has_meaningful_value=has_meaningful_value)
    )
    config_metadata = first_meaningful_mapping(payload, ("config_metadata", "environment_metadata"))
    if config_metadata is None:
        return False
    has_metadata = has_meaningful_value(config_metadata)
    has_identifier = has_any_key_value(
        payload,
        ("config_receipt_id", "config_id", "environment_id", "environment_name", "target_config_id"),
    ) or has_any_key_value(
        config_metadata,
        ("config_receipt_id", "config_id", "environment_id", "environment_name", "name", "id", "cluster"),
    )
    return has_metadata and has_identifier


def _first_meaningful_mapping(
    payload: Mapping[str, Any],
    keys: Sequence[str],
    *,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> Mapping[str, Any] | None:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping) and has_meaningful_value(value):
            return value
    return None


def _has_any_key_value(
    mapping: Mapping[str, Any],
    keys: Sequence[str],
    *,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> bool:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    return any(has_meaningful_value(mapping.get(key)) for key in keys)


def _value_from(
    payload: Mapping[str, Any],
    keys: Sequence[str],
    *,
    fallback: Mapping[str, Any] | None = None,
    has_meaningful_value: Callable[[Any], bool] | None = None,
) -> Any:
    has_meaningful_value = has_meaningful_value or _has_meaningful_value
    for key in keys:
        if has_meaningful_value(payload.get(key)):
            return payload.get(key)
    if fallback is not None:
        for key in keys:
            if has_meaningful_value(fallback.get(key)):
                return fallback.get(key)
    return None


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
