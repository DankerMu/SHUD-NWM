from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from services.orchestrator import scheduler as _scheduler
from services.orchestrator.chain import ForecastOrchestrator
from services.orchestrator.chain_types import PipelineResult
from services.orchestrator.production_contract import (
    production_identity_contract_evidence,
    production_stage_for,
    production_status_for,
)
from services.orchestrator.scheduler_evidence import (
    MODEL_RUN_EVIDENCE_SCHEMA_VERSION,
    SCHEDULER_EVIDENCE_CONTRACT_ID,
    SCHEDULER_EVIDENCE_GITHUB_ISSUE,
    SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE,
    UNKNOWN_AFTER_ATTEMPT,
)
from services.slurm_gateway.gateway import ConfigurationError
from services.slurm_gateway.resource_validation import ResourceProfileValidationError, validate_resource_profile

MAX_MODEL_RUN_STAGE_TASK_ROWS = _scheduler.MAX_MODEL_RUN_STAGE_TASK_ROWS
ProductionSchedulerConfig = _scheduler.ProductionSchedulerConfig
SLURM_RESOURCE_PROFILE_DIRECTIVE_FIELDS = _scheduler.SLURM_RESOURCE_PROFILE_DIRECTIVE_FIELDS
SLURM_RESOURCE_PROFILE_TEMPLATE_IDENTITY_FIELDS = _scheduler.SLURM_RESOURCE_PROFILE_TEMPLATE_IDENTITY_FIELDS
SchedulerCandidate = _scheduler.SchedulerCandidate
TASK_RESULT_CANDIDATE_IDENTITY_FIELDS = _scheduler.TASK_RESULT_CANDIDATE_IDENTITY_FIELDS
TASK_RESULT_INDEX_IDENTITY_FIELDS = _scheduler.TASK_RESULT_INDEX_IDENTITY_FIELDS
_scheduler_evidence = _scheduler._scheduler_evidence
redact_payload = _scheduler.redact_payload


def _scheduler_call(name: str, *args: Any, **kwargs: Any) -> Any:
    return getattr(_scheduler, name)(*args, **kwargs)


def _candidate_artifact_refs(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_artifact_refs", *args, **kwargs)


def _candidate_canonical_product_id(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_canonical_product_id", *args, **kwargs)


def _candidate_display_evidence(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_display_evidence", *args, **kwargs)


def _candidate_forcing_evidence(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_forcing_evidence", *args, **kwargs)


def _candidate_output_evidence(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_output_evidence", *args, **kwargs)


def _candidate_output_key(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_output_key", *args, **kwargs)


def _candidate_output_uri(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_output_uri", *args, **kwargs)


def _candidate_product_counts(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_product_counts", *args, **kwargs)


def _candidate_production_identity(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_production_identity", *args, **kwargs)


def _candidate_quality_states(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_quality_states", *args, **kwargs)


def _candidate_resource_summary(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_resource_summary", *args, **kwargs)


def _candidate_residual_blockers(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_residual_blockers", *args, **kwargs)


def _ensure_utc(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_ensure_utc", *args, **kwargs)


def _evidence_safe(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_evidence_safe", *args, **kwargs)


def _format_utc(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_format_utc", *args, **kwargs)


def _is_non_submitted_terminal_or_unavailable_status(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_is_non_submitted_terminal_or_unavailable_status", *args, **kwargs)


def _is_partial_candidate_evidence(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_is_partial_candidate_evidence", *args, **kwargs)


def _mapping_value(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_mapping_value", *args, **kwargs)


def _model_package_manifest_uri(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_model_package_manifest_uri", *args, **kwargs)


def _nested_bool(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_nested_bool", *args, **kwargs)


def _redact_secret_manifest_for_evidence(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_redact_secret_manifest_for_evidence", *args, **kwargs)


def _slurm_preflight(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_slurm_preflight", *args, **kwargs)


def _candidate_execution_attempted(outcome: Mapping[str, Any] | None, submitted: bool) -> bool:
    if submitted and outcome is None:
        return True
    if not outcome:
        return False
    return any(
        outcome.get(field) not in (None, "")
        for field in ("slurm_job_id", "exit_code", "log_uri", "accounting", "task_id", "original_task_id")
    )


def _pipeline_result_slurm_submit_called(result: PipelineResult) -> bool:
    for stage in result.stages:
        if _nonempty_evidence_value(getattr(stage, "slurm_job_id", None)):
            return True
        task_results = getattr(stage, "task_results", ()) or ()
        for task in task_results:
            if isinstance(task, Mapping) and _nonempty_evidence_value(task.get("slurm_job_id")):
                return True
    return any(
        _nonempty_evidence_value(outcome.get("slurm_job_id"))
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if isinstance(outcome, Mapping)
    )


def _pipeline_result_pipeline_status_write(result: PipelineResult) -> bool | str:
    return _pipeline_result_pipeline_write_value(
        result,
        write_field="pipeline_status_write",
        absent_field="pipeline_status_writes_proven_absent",
    )


def _pipeline_result_pipeline_event_write(result: PipelineResult) -> bool | str:
    return _pipeline_result_pipeline_write_value(
        result,
        write_field="pipeline_event_write",
        absent_field="pipeline_event_writes_proven_absent",
    )


def _pipeline_result_pipeline_write_value(
    result: PipelineResult,
    *,
    write_field: str,
    absent_field: str,
) -> bool | str:
    outcome_values = [
        _candidate_pipeline_write_value(outcome, write_field, fallback=None)
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if isinstance(outcome, Mapping)
    ]
    if any(value is True for value in outcome_values):
        return True
    if any(value == UNKNOWN_AFTER_ATTEMPT for value in outcome_values):
        return UNKNOWN_AFTER_ATTEMPT
    if _pipeline_result_has_pipeline_job_evidence(result):
        return True
    if outcome_values and all(value is False for value in outcome_values):
        return False
    if _pipeline_result_write_absence_proven(result, absent_field):
        return False
    return UNKNOWN_AFTER_ATTEMPT


def _pipeline_result_has_pipeline_job_evidence(result: PipelineResult) -> bool:
    for stage in result.stages:
        if _nonempty_evidence_value(getattr(stage, "pipeline_job_id", None)):
            return True
        task_results = getattr(stage, "task_results", ()) or ()
        for task in task_results:
            if isinstance(task, Mapping) and _nonempty_evidence_value(task.get("pipeline_job_id")):
                return True
    return any(
        _nonempty_evidence_value(outcome.get("pipeline_job_id"))
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if isinstance(outcome, Mapping)
    )


def _pipeline_result_write_absence_proven(result: PipelineResult, absent_field: str) -> bool:
    outcomes = [outcome for outcome in getattr(result, "candidate_outcomes", ()) or () if isinstance(outcome, Mapping)]
    if outcomes and all(outcome.get(absent_field) is True for outcome in outcomes):
        return True
    return False


def _candidate_slurm_submit_called(outcome: Mapping[str, Any] | None, fallback: bool) -> bool:
    if outcome and _nonempty_evidence_value(outcome.get("slurm_job_id")):
        return True
    return fallback


def _candidate_pipeline_write_value(
    outcome: Mapping[str, Any] | None,
    write_field: str,
    *,
    fallback: bool | str | None,
) -> bool | str | None:
    if outcome:
        value = outcome.get(write_field)
        if value == UNKNOWN_AFTER_ATTEMPT:
            return UNKNOWN_AFTER_ATTEMPT
        coerced = _nested_bool(outcome, write_field)
        if coerced is True:
            return True
        absent_field = f"{write_field}s_proven_absent"
        if outcome.get(absent_field) is True:
            return False
    return fallback


def _execution_mutation_value(*values: bool | str | None) -> bool | str:
    if any(value == UNKNOWN_AFTER_ATTEMPT for value in values):
        return UNKNOWN_AFTER_ATTEMPT
    return any(value is True for value in values)


def _nonempty_evidence_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _candidate_identity_evidence(
    candidate: SchedulerCandidate,
    *,
    output_uri: str | None = None,
    include_state_evidence: bool = True,
) -> dict[str, Any]:
    contract_identity = _candidate_production_identity(candidate)
    evidence = {
        "production_identity_contract": production_identity_contract_evidence(contract_identity),
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id,
        "source": candidate.source_id,
        "cycle_id": candidate.cycle_id,
        "cycle_time_utc": _format_utc(candidate.cycle_time_utc),
        "cycle_time": _format_utc(candidate.cycle_time_utc),
        "model_id": candidate.model_id,
        "scenario_id": candidate.scenario_id,
        "run_id": candidate.run_id,
        "canonical_product_id": contract_identity["canonical_product_id"],
        "forcing_version_id": candidate.forcing_version_id,
        "hydro_run_id": contract_identity["hydro_run_id"],
        "published_manifest_id": contract_identity["published_manifest_id"],
        "model_package_uri": _redact_secret_manifest_for_evidence(candidate.model_package_uri, "model_package_uri"),
        "model_package_manifest_uri": _redact_secret_manifest_for_evidence(
            _model_package_manifest_uri(candidate),
            "model_package_manifest_uri",
        ),
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "segment_count": candidate.segment_count,
        "output_segment_count": candidate.output_segment_count,
        "output_key": _candidate_output_key(candidate),
    }
    if contract_identity.get("pipeline_job_id") not in (None, ""):
        evidence["pipeline_job_id"] = contract_identity["pipeline_job_id"]
    resolved_output_uri = output_uri or _candidate_output_uri(candidate)
    if resolved_output_uri is not None:
        evidence["output_uri"] = _redact_secret_manifest_for_evidence(resolved_output_uri, "output_uri")
    if include_state_evidence and candidate.state_evidence:
        evidence["state_evidence"] = _evidence_safe(candidate.state_evidence)
    return evidence


def _candidate_preflight_blocked_evidence(
    candidate: SchedulerCandidate,
    *,
    config: ProductionSchedulerConfig | None = None,
) -> dict[str, Any]:
    if config is not None and config.slurm_execution_enabled:
        preflight = _slurm_preflight(config)
        return _candidate_slurm_preflight_blocked_evidence(candidate, preflight)
    return {
        **_candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "unsupported_without_safe_preflight",
        "error_code": "PRODUCTION_PREFLIGHT_UNSUPPORTED",
        "error_message": (
            "Default non-dry-run production scheduling is blocked until the Slurm/database preflight "
            "from issue #194 is available or a deterministic orchestrator_factory is injected."
        ),
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": "PRODUCTION_PREFLIGHT_UNSUPPORTED",
                "state": "blocked",
                "quality_flag": "preflight_required",
                "residual_risk": "No scheduler mutation was attempted.",
            }
        ],
    }


def _candidate_slurm_preflight_blocked_evidence(
    candidate: SchedulerCandidate,
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    blockers = list(preflight.get("blockers") or [])
    primary = (
        blockers[0]
        if blockers
        else {
            "code": "SLURM_PREFLIGHT_BLOCKED",
            "message": "Slurm preflight blocked submission.",
        }
    )
    return {
        **_candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "slurm_preflight",
        "slurm_preflight": redact_payload(preflight),
        "error_code": str(primary.get("code") or "SLURM_PREFLIGHT_BLOCKED"),
        "error_message": str(primary.get("message") or "Slurm preflight blocked submission."),
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": str(blocker.get("code") or "SLURM_PREFLIGHT_BLOCKED"),
                "field": blocker.get("field"),
                "state": "blocked",
                "quality_flag": "slurm_preflight_blocked",
                "residual_risk": blocker.get("message"),
            }
            for blocker in blockers
        ],
    }


def _candidate_evidence_write_blocked_evidence(
    candidate: SchedulerCandidate,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    return _scheduler_evidence.candidate_evidence_write_blocked_evidence(
        candidate,
        reservation,
        candidate_model_run_review_evidence=_candidate_model_run_review_evidence,
        candidate_identity_evidence=_candidate_identity_evidence,
        standard_chain_shape=[stage.stage for stage in ForecastOrchestrator.stages],
        evidence_safe=_evidence_safe,
    )


def _cancel_candidate_evidence_write_blocked_evidence(
    candidate: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    return _scheduler_evidence.cancel_candidate_evidence_write_blocked_evidence(
        candidate,
        reservation,
        ensure_utc=_ensure_utc,
        evidence_safe=_evidence_safe,
    )


def _sync_candidate_evidence_write_blocked_evidence(
    candidate: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    return _scheduler_evidence.sync_candidate_evidence_write_blocked_evidence(
        candidate,
        reservation,
        standard_chain_shape=[stage.stage for stage in ForecastOrchestrator.stages],
        evidence_safe=_evidence_safe,
    )


def _candidate_secret_manifest_blocked_evidence(
    candidate: SchedulerCandidate,
    *,
    findings: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    return {
        **_candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "slurm_preflight",
        "error_code": "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED",
        "error_message": "Slurm submission manifests reject secret-bearing fields and URL values.",
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED",
                "field": finding.get("field"),
                "state": "blocked",
                "quality_flag": "slurm_preflight_blocked",
                "residual_risk": "Secret-bearing manifest field or URL value was rejected before submission.",
            }
            for finding in findings
        ],
    }


def _slurm_resource_profile_blockers(resource_profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    collision_fields = sorted(SLURM_RESOURCE_PROFILE_TEMPLATE_IDENTITY_FIELDS.intersection(resource_profile))
    if collision_fields:
        return [
            {
                "code": "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID",
                "field": f"resource_profile.{field}",
                "message": "Slurm resource profile cannot override manifest or template identity fields.",
                "reason": "manifest_identity_collision",
            }
            for field in collision_fields
        ]
    directive_fields = {
        key: resource_profile[key] for key in SLURM_RESOURCE_PROFILE_DIRECTIVE_FIELDS if key in resource_profile
    }
    if not directive_fields:
        return []
    try:
        validate_resource_profile(directive_fields, require_required=False)
    except ResourceProfileValidationError as exc:
        return [
            {
                "code": "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID",
                "field": exc.details.get("field"),
                "message": "Slurm resource profile contains invalid directive values.",
                "reason": exc.details.get("reason") or exc.details.get("type"),
            }
        ]
    except ConfigurationError as exc:
        return [
            {
                "code": "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID",
                "field": (exc.details or {}).get("field"),
                "message": "Slurm resource profile contains invalid directive values.",
            }
        ]
    return []


def _resource_profile_evidence(resource_profile: Mapping[str, Any]) -> dict[str, Any]:
    redacted = _redact_secret_manifest_for_evidence(dict(resource_profile), "resource_profile")
    redacted = _redact_digest_keys_for_evidence(redacted)
    if not isinstance(redacted, Mapping):
        return {}
    evidence = dict(redacted)
    direct_grid = evidence.get("direct_grid_forcing")
    if isinstance(direct_grid, Mapping):
        stations = direct_grid.get("stations")
        station_count = (
            len(stations)
            if isinstance(stations, Sequence) and not isinstance(stations, str | bytes | bytearray)
            else None
        )
        retained_keys = (
            "forcing_mapping_mode",
            "mapping_asset_identity",
            "model_input_package_id",
            "binding_uri",
            "binding_checksum",
            "applicable_source_ids",
            "grid_id",
            "grid_signature",
            "sp_att_manifest_path",
            "sp_att_checksum",
        )
        compact_direct_grid = {
            key: direct_grid[key] for key in retained_keys if key in direct_grid
        }
        if station_count is not None:
            compact_direct_grid["station_count"] = station_count
        evidence["direct_grid_forcing"] = compact_direct_grid
    invalid_fields = {
        str(blocker.get("field", "")).removeprefix("resource_profile.")
        for blocker in _slurm_resource_profile_blockers(resource_profile)
        if blocker.get("field")
    }
    for field_name in invalid_fields:
        if field_name in evidence:
            evidence[field_name] = "[unsafe]"
    return evidence


def _redact_digest_keys_for_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if _is_digest_evidence_key(key_text):
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = _redact_digest_keys_for_evidence(nested)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_digest_keys_for_evidence(item) for item in value]
    return value


def _is_digest_evidence_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if any(token in normalized for token in ("checksum", "digest", "hash")):
        return True
    return any(token == normalized or normalized.endswith(f"_{token}") for token in ("sha", "sha1", "sha256", "sha512"))


def _candidate_execution_evidence(
    result: PipelineResult,
    candidates: Sequence[SchedulerCandidate],
    *,
    output_uris: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    stage_names = [stage.stage for stage in result.stages]
    stage_statuses = [_stage_run_evidence(stage) for stage in result.stages]
    slurm_submit_called = _pipeline_result_slurm_submit_called(result)
    pipeline_status_write = _pipeline_result_pipeline_status_write(result)
    pipeline_event_write = _pipeline_result_pipeline_event_write(result)
    outcomes_by_candidate = {
        str(outcome.get("candidate_id")): dict(outcome)
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if outcome.get("candidate_id")
    }
    return [
        _candidate_execution_evidence_item(
            result,
            candidate,
            output_uri=(output_uris or {}).get(candidate.candidate_id),
            outcome=outcomes_by_candidate.get(candidate.candidate_id),
            slurm_submit_called=slurm_submit_called,
            pipeline_status_write=pipeline_status_write,
            pipeline_event_write=pipeline_event_write,
            stage_names=stage_names,
            stage_statuses=stage_statuses,
        )
        for candidate in candidates
    ]


def _candidate_forcing_ready_evidence(candidate: SchedulerCandidate, result: Any) -> dict[str, Any]:
    status = str(getattr(result, "status", "forcing_ready") or "forcing_ready")
    met_write = status != "already_done"
    payload = {
        **_candidate_identity_evidence(candidate, output_uri=None),
        "stage": "forcing",
        "production_stage": production_stage_for("forcing"),
        "status": status,
        "production_status": production_status_for(status),
        "submitted": False,
        "slurm_submit_called": False,
        "execution_attempted": True,
        "forcing_producer_called": True,
        "mutation_occurred": met_write,
        "met_result_table_write": met_write,
        "hydro_result_table_write": False,
        "pipeline_status_writes_proven_absent": True,
        "pipeline_event_writes_proven_absent": True,
        "qhh_script_invoked": False,
        "rshud_runtime_called": False,
        "forcing": {
            "forcing_version_id": getattr(result, "forcing_version_id", candidate.forcing_version_id),
            "forcing_package_uri": getattr(result, "forcing_package_uri", None),
            "package_uri": getattr(result, "forcing_package_uri", None),
            "checksum": getattr(result, "checksum", None),
            "manifest_checksum": getattr(result, "checksum", None),
            "station_count": getattr(result, "station_count", None),
            "timestep_count": getattr(result, "timestep_count", None),
            "variable_count": getattr(result, "variable_count", None),
            "time_range": dict(getattr(result, "time_range", {}) or {}),
            "units": dict(getattr(result, "units", {}) or {}),
            "file_uris": dict(getattr(result, "file_uris", {}) or {}),
        },
    }
    return _evidence_safe(payload)


def _candidate_forcing_blocked_evidence(candidate: SchedulerCandidate, error: Exception) -> dict[str, Any]:
    error_code = str(getattr(error, "error_code", "FORCING_PRODUCTION_BLOCKED"))
    payload = {
        **_candidate_identity_evidence(candidate, output_uri=None),
        "stage": "forcing",
        "production_stage": production_stage_for("forcing"),
        "status": "blocked",
        "production_status": production_status_for("blocked"),
        "submitted": False,
        "slurm_submit_called": False,
        "execution_attempted": True,
        "forcing_producer_called": True,
        "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
        "mutation_occurred": UNKNOWN_AFTER_ATTEMPT,
        "met_result_table_write": UNKNOWN_AFTER_ATTEMPT,
        "hydro_result_table_write": False,
        "pipeline_status_writes_proven_absent": True,
        "pipeline_event_writes_proven_absent": True,
        "qhh_script_invoked": False,
        "rshud_runtime_called": False,
        "error_code": error_code,
        "error_message": _evidence_safe(getattr(error, "message", str(error))),
        "residual_blockers": [
            {
                "code": error_code,
                "stage": "forcing",
                "state": "blocked",
                "quality_flag": "forcing_production_blocked",
                "residual_risk": "Station forcing production did not complete; SHUD submission is blocked.",
            }
        ],
    }
    return _evidence_safe(payload)


def _candidate_execution_evidence_item(
    result: PipelineResult,
    candidate: SchedulerCandidate,
    *,
    output_uri: str | None,
    outcome: Mapping[str, Any] | None,
    slurm_submit_called: bool,
    pipeline_status_write: bool | str,
    pipeline_event_write: bool | str,
    stage_names: Sequence[str],
    stage_statuses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if outcome is None:
        status = result.status
        candidate_submitted = slurm_submit_called
        candidate_outcome: dict[str, Any] | None = None
        execution_attempted = True
    else:
        outcome_status = str(outcome.get("status") or "")
        status = _candidate_status_from_outcome(result.status, outcome_status)
        execution_attempted = True
        candidate_slurm_submit_called = _candidate_slurm_submit_called(outcome, slurm_submit_called)
        candidate_submitted = candidate_slurm_submit_called and (outcome_status == "active" or execution_attempted)
        candidate_outcome = dict(outcome)
    candidate_pipeline_status_write = _candidate_pipeline_write_value(
        outcome,
        "pipeline_status_write",
        fallback=pipeline_status_write,
    )
    candidate_pipeline_event_write = _candidate_pipeline_write_value(
        outcome,
        "pipeline_event_write",
        fallback=pipeline_event_write,
    )
    mutation_occurred = _execution_mutation_value(
        candidate_submitted,
        candidate_pipeline_status_write,
        candidate_pipeline_event_write,
    )
    review_evidence = _candidate_model_run_review_evidence(
        candidate,
        output_uri=output_uri,
        outcome=outcome,
        status=status,
        stage_statuses=stage_statuses,
    )
    item = {
        **review_evidence,
        "status": status,
        "submitted": candidate_submitted,
        "slurm_submit_called": candidate_submitted,
        "execution_attempted": execution_attempted,
        "final_candidate_success": (
            status == result.status and not _is_non_submitted_terminal_or_unavailable_status(status)
        ),
        "mutation_occurred": mutation_occurred,
        "pipeline_run_id": result.run_id,
        "standard_chain_shape": stage_names,
        "qhh_script_invoked": False,
    }
    if candidate_pipeline_status_write is True:
        item["pipeline_status_write"] = True
        item["pipeline_status_writes_proven_absent"] = False
    elif candidate_pipeline_status_write == UNKNOWN_AFTER_ATTEMPT:
        item["pipeline_status_write"] = UNKNOWN_AFTER_ATTEMPT
        item["pipeline_status_writes_proven_absent"] = False
    else:
        item["pipeline_status_writes_proven_absent"] = True
    if candidate_pipeline_event_write is True:
        item["pipeline_event_write"] = True
        item["pipeline_event_writes_proven_absent"] = False
    elif candidate_pipeline_event_write == UNKNOWN_AFTER_ATTEMPT:
        item["pipeline_event_write"] = UNKNOWN_AFTER_ATTEMPT
        item["pipeline_event_writes_proven_absent"] = False
    else:
        item["pipeline_event_writes_proven_absent"] = True
    if mutation_occurred == UNKNOWN_AFTER_ATTEMPT:
        item["mutation_outcome"] = UNKNOWN_AFTER_ATTEMPT
    if candidate_outcome is not None:
        candidate_outcome = _evidence_safe(candidate_outcome)
        item["candidate_outcome"] = candidate_outcome
        if _is_partial_candidate_evidence(item):
            item["error_code"] = str(candidate_outcome.get("reason") or f"CANDIDATE_{status}").upper()
            item["error_message"] = f"Candidate {candidate.candidate_id} was {status} in the partial multi-basin cycle."
            if not any(blocker.get("code") == item["error_code"] for blocker in item["residual_blockers"]):
                item["residual_blockers"].append(
                    {
                        "code": item["error_code"],
                        "stage": candidate_outcome.get("stage") or candidate_outcome.get("failed_stage"),
                        "state": "blocked",
                        "quality_flag": "partial_candidate",
                        "residual_risk": item["error_message"],
                    }
                )
    return item


def _candidate_status_from_outcome(result_status: str, outcome_status: str) -> str:
    if outcome_status == "active":
        return result_status
    if _is_non_submitted_terminal_or_unavailable_status(outcome_status):
        return outcome_status
    return "unavailable"


def _candidate_model_run_review_evidence(
    candidate: SchedulerCandidate,
    *,
    output_uri: str | None,
    outcome: Mapping[str, Any] | None,
    status: str,
    stage_statuses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    stage_status_payload = _candidate_stage_evidence(candidate, stage_statuses, outcome=outcome)
    quality_states = _candidate_quality_states(candidate, outcome=outcome, status=status)
    artifact_refs = _candidate_artifact_refs(candidate, output_uri=output_uri)
    return {
        "schema_version": MODEL_RUN_EVIDENCE_SCHEMA_VERSION,
        "review_contract": {
            "contract_id": SCHEDULER_EVIDENCE_CONTRACT_ID,
            "github_issue": SCHEDULER_EVIDENCE_GITHUB_ISSUE,
            "openspec_change": SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE,
            "scope": "model_run_evidence",
        },
        # The scheduler pass already preserves the complete candidate state in
        # its top-level ``candidates`` collection.  Repeating that potentially
        # large journal/readiness history once more for every model-run record
        # made a normal 18-basin x 2-source pass exceed the durable 5 MB
        # evidence limit.  Keep model-run evidence candidate-scoped, but do not
        # duplicate the state history here.
        **_candidate_identity_evidence(
            candidate,
            output_uri=output_uri,
            include_state_evidence=False,
        ),
        "stage_statuses": stage_status_payload,
        "stage_evidence": stage_status_payload,
        "artifact_refs": artifact_refs,
        "artifact_locations": dict(artifact_refs),
        "resource_profile": _resource_profile_evidence(candidate.resource_profile),
        "resource_summary": _candidate_resource_summary(
            candidate,
            stage_statuses=stage_status_payload,
            outcome=outcome,
        ),
        "forcing": _candidate_forcing_evidence(candidate),
        "outputs": _candidate_output_evidence(candidate, output_uri=output_uri, outcome=outcome),
        "display": _candidate_display_evidence(candidate),
        "quality_states": quality_states,
        "residual_blockers": _candidate_residual_blockers(
            candidate,
            outcome=outcome,
            status=status,
            quality_states=quality_states,
        ),
    }


def _candidate_stage_evidence(
    candidate: SchedulerCandidate,
    stage_statuses: Sequence[Mapping[str, Any]],
    *,
    outcome: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    return [
        _candidate_stage_evidence_item(candidate, _evidence_safe(dict(stage)), outcome=outcome)
        for stage in stage_statuses
    ]


def _candidate_stage_evidence_item(
    candidate: SchedulerCandidate,
    stage: Mapping[str, Any],
    *,
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    stage_payload = dict(stage)
    stage_payload["production_stage"] = production_stage_for(
        stage_payload.get("stage") or stage_payload.get("job_type")
    )
    stage_payload["production_status"] = production_status_for(stage_payload.get("status"))
    task_results = _stage_task_results(stage_payload)
    total_count = len(task_results)
    status_counts = Counter(str(task.get("status") or task.get("state") or "unknown") for task in task_results)
    matched_tasks = [task for task in task_results if _task_result_matches_candidate(task, candidate, outcome=outcome)]
    exact_match_available = _task_candidate_matching_available(task_results, outcome=outcome)
    if exact_match_available:
        selected_tasks = matched_tasks[:MAX_MODEL_RUN_STAGE_TASK_ROWS]
    else:
        selected_tasks = task_results[:MAX_MODEL_RUN_STAGE_TASK_ROWS]
    selected_count = len(selected_tasks)
    stage_payload["task_results"] = [_evidence_safe(dict(task)) for task in selected_tasks]
    stage_payload["task_results_summary"] = _evidence_safe(
        {
            "total_count": total_count,
            "included_count": selected_count,
            "omitted_count": max(total_count - selected_count, 0),
            "matched_count": len(matched_tasks),
            "matching": "candidate_identity" if exact_match_available else "bounded_sample",
            "limit": MAX_MODEL_RUN_STAGE_TASK_ROWS,
            "status_counts": dict(sorted(status_counts.items())),
        }
    )
    return _evidence_safe(stage_payload)


def _stage_run_evidence(stage: Any) -> dict[str, Any]:
    task_results = [
        _task_result_evidence(task)
        for task in tuple(getattr(stage, "task_results", ()) or ())
        if isinstance(task, Mapping)
    ]
    payload = {
        "stage": getattr(stage, "stage", None),
        "production_stage": production_stage_for(getattr(stage, "stage", None) or getattr(stage, "job_type", None)),
        "job_type": getattr(stage, "job_type", None),
        "pipeline_job_id": getattr(stage, "pipeline_job_id", None),
        "slurm_job_id": getattr(stage, "slurm_job_id", None),
        "status": getattr(stage, "status", None),
        "production_status": production_status_for(getattr(stage, "status", None)),
        "exit_code": getattr(stage, "exit_code", None),
        "error_code": getattr(stage, "error_code", None),
        "error_message": getattr(stage, "error_message", None),
        "log_uri": getattr(stage, "log_uri", None),
        "accounting": getattr(stage, "accounting", {}) or {},
        "resource_metrics": _resource_metrics_from_mapping(getattr(stage, "accounting", {}) or {}),
        "task_results": task_results,
    }
    if not payload["accounting"]:
        payload["accounting_gap"] = {
            "available": False,
            "reason": "accounting_unavailable",
            "fabricated_metrics": False,
        }
    return _evidence_safe(payload)


def _stage_task_results(stage: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_results = stage.get("task_results") or []
    if not isinstance(task_results, Sequence) or isinstance(task_results, str | bytes | bytearray):
        return []
    return [task for task in task_results if isinstance(task, Mapping)]


def _task_result_matches_candidate(
    task: Mapping[str, Any],
    candidate: SchedulerCandidate,
    *,
    outcome: Mapping[str, Any] | None,
) -> bool:
    identity_fields = {
        "candidate_id": candidate.candidate_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "model_id": candidate.model_id,
    }
    for field_name, expected in identity_fields.items():
        if _normalized_identity(task.get(field_name)) == _normalized_identity(expected):
            return True
    identity = task.get("identity")
    if isinstance(identity, Mapping):
        for field_name, expected in identity_fields.items():
            if _normalized_identity(identity.get(field_name)) == _normalized_identity(expected):
                return True
    if outcome is None:
        return False
    for field_name in TASK_RESULT_CANDIDATE_IDENTITY_FIELDS:
        task_value = _normalized_identity(task.get(field_name))
        outcome_value = _normalized_identity(outcome.get(field_name))
        if task_value is not None and task_value == outcome_value:
            return True
    outcome_task_ids = {
        _normalized_identity(outcome.get(field_name)) for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS
    }
    outcome_task_ids.discard(None)
    task_ids = {_normalized_identity(task.get(field_name)) for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS}
    task_ids.discard(None)
    return bool(task_ids.intersection(outcome_task_ids))


def _task_candidate_matching_available(
    tasks: Sequence[Mapping[str, Any]],
    *,
    outcome: Mapping[str, Any] | None,
) -> bool:
    for task in tasks:
        if any(task.get(field_name) not in (None, "") for field_name in TASK_RESULT_CANDIDATE_IDENTITY_FIELDS):
            return True
        identity = task.get("identity")
        if isinstance(identity, Mapping) and any(
            identity.get(field_name) not in (None, "") for field_name in TASK_RESULT_CANDIDATE_IDENTITY_FIELDS
        ):
            return True
    if outcome is None:
        return False
    outcome_has_task_identity = any(
        outcome.get(field_name) not in (None, "") for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS
    )
    if not outcome_has_task_identity:
        return False
    return any(
        any(task.get(field_name) not in (None, "") for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS)
        for task in tasks
    )


def _normalized_identity(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _task_result_evidence(task: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(task)
    payload["accounting"] = dict(_mapping_value(payload.get("accounting")))
    metrics = _resource_metrics_from_mapping(payload.get("resource_metrics") or payload["accounting"])
    if metrics:
        payload["resource_metrics"] = metrics
    elif "resource_metrics" not in payload:
        payload["resource_metrics"] = {}
    return _evidence_safe(payload)


def _resource_metrics_from_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    aliases = {
        "elapsed": ("elapsed", "elapsed_time"),
        "max_rss": ("max_rss", "MaxRSS", "maxrss"),
        "ave_rss": ("ave_rss", "AveRSS", "averss"),
        "alloc_tres": ("alloc_tres", "AllocTRES", "tres"),
        "max_disk_read": ("max_disk_read", "MaxDiskRead"),
        "max_disk_write": ("max_disk_write", "MaxDiskWrite"),
    }
    metrics: dict[str, Any] = {}
    for normalized, keys in aliases.items():
        for key in keys:
            if key in value and value[key] not in (None, ""):
                metrics[normalized] = value[key]
                break
    return _evidence_safe(metrics)
