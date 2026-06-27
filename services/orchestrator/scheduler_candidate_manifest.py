from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from services.orchestrator import scheduler as _scheduler
from services.orchestrator import scheduler_candidates as _scheduler_candidates

CycleDiscovery = _scheduler.CycleDiscovery
RegisteredSchedulerModel = _scheduler.RegisteredSchedulerModel
SchedulerCandidate = _scheduler.SchedulerCandidate


def _scheduler_call(name: str, *args: Any, **kwargs: Any) -> Any:
    return getattr(_scheduler, name)(*args, **kwargs)


def _candidate_canonical_product_id(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_canonical_product_id", *args, **kwargs)


def _candidate_contract_pipeline_job_id(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_contract_pipeline_job_id", *args, **kwargs)


def _candidate_is_fresh_full_chain(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_is_fresh_full_chain", *args, **kwargs)


def _candidate_output_key(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_output_key", *args, **kwargs)


def _candidate_output_river_manifest(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_output_river_manifest", *args, **kwargs)


def _candidate_published_manifest_id(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_published_manifest_id", *args, **kwargs)


def _candidate_station_count(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_station_count", *args, **kwargs)


def _candidate_station_ids(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_candidate_station_ids", *args, **kwargs)


def _ensure_utc(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_ensure_utc", *args, **kwargs)


def _evidence_safe(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_evidence_safe", *args, **kwargs)


def _format_utc(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_format_utc", *args, **kwargs)


def _model_package_manifest_uri(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_model_package_manifest_uri", *args, **kwargs)


def _nested_bool(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_nested_bool", *args, **kwargs)


def _resource_profile_project_identity(*args: Any, **kwargs: Any) -> Any:
    return _scheduler_call("_resource_profile_project_identity", *args, **kwargs)


def cycle_id_for(*args: Any, **kwargs: Any) -> Any:
    return _scheduler.cycle_id_for(*args, **kwargs)


def format_cycle_time(*args: Any, **kwargs: Any) -> Any:
    return _scheduler.format_cycle_time(*args, **kwargs)


def normalize_source_id(*args: Any, **kwargs: Any) -> Any:
    return _scheduler.normalize_source_id(*args, **kwargs)


def scenario_for_source(*args: Any, **kwargs: Any) -> Any:
    return _scheduler.scenario_for_source(*args, **kwargs)


def _candidate_for(
    *,
    discovery: CycleDiscovery,
    model: RegisteredSchedulerModel,
    horizon: Mapping[str, Any],
) -> SchedulerCandidate:
    source_id = normalize_source_id(discovery.source_id)
    cycle_time = _ensure_utc(discovery.cycle_time)
    compact_cycle = format_cycle_time(cycle_time)
    scenario_id = scenario_for_source(source_id)
    candidate_id = f"{source_id}:{_format_utc(cycle_time)}:{model.model_id}:{scenario_id}"
    return SchedulerCandidate(
        candidate_id=candidate_id,
        source_id=source_id,
        cycle_id=cycle_id_for(source_id, cycle_time),
        cycle_time_utc=cycle_time,
        model_id=model.model_id,
        basin_id=model.basin_id,
        basin_version_id=model.basin_version_id,
        river_network_version_id=model.river_network_version_id,
        segment_count=model.segment_count,
        output_segment_count=model.output_segment_count,
        model_package_uri=model.model_package_uri,
        resource_profile=model.resource_profile,
        display_capabilities=model.display_capabilities,
        frequency_capabilities=model.frequency_capabilities,
        horizon=horizon,
        scenario_id=scenario_id,
        run_id=f"fcst_{source_id.lower()}_{compact_cycle}_{model.model_id}",
        forcing_version_id=f"forc_{source_id.lower()}_{compact_cycle}_{model.model_id}",
        status="selected",
    )


def _candidate_max_lead_hours(candidate: SchedulerCandidate) -> int | None:
    for key in ("max_lead_hours", "forecast_horizon_hours"):
        value = candidate.horizon.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


_blocked_candidate = _scheduler_candidates._blocked_candidate
_candidate_with_state_evidence = _scheduler_candidates._candidate_with_state_evidence


def _candidate_with_forcing_result(candidate: SchedulerCandidate, result: Any) -> SchedulerCandidate:
    forcing_version_id = str(getattr(result, "forcing_version_id", candidate.forcing_version_id))
    file_uris = dict(getattr(result, "file_uris", {}) or {})
    forcing_package_uri = getattr(result, "forcing_package_uri", None)
    checksum = getattr(result, "checksum", None)
    resource_profile = dict(candidate.resource_profile)
    resource_profile.update(
        {
            "forcing_version_id": forcing_version_id,
            "forcing_package_uri": forcing_package_uri,
            "forcing_package_manifest_uri": file_uris.get("package_manifest"),
            "forcing_manifest_checksum": checksum,
            "forcing_uri": file_uris.get("tsd_forc") or forcing_package_uri,
            "forcing_file_uris": file_uris,
            "forcing_variable_count": getattr(result, "variable_count", None),
            "forcing_time_range": dict(getattr(result, "time_range", {}) or {}),
            "forcing_units": dict(getattr(result, "units", {}) or {}),
        }
    )
    if getattr(result, "station_count", None) not in (None, ""):
        resource_profile["station_count"] = getattr(result, "station_count")
    return replace(candidate, forcing_version_id=forcing_version_id, resource_profile=resource_profile)


def _candidate_scheduler_canonical_identity(candidate: SchedulerCandidate) -> dict[str, Any]:
    readiness = candidate.state_evidence.get("canonical_readiness")
    identity = {"canonical_product_id": _candidate_canonical_product_id(candidate)}
    if isinstance(readiness, Mapping):
        for key in ("policy_identity", "source_object_identity", "accepted_horizon", "expected_leads"):
            value = readiness.get(key)
            if value not in (None, ""):
                identity[key] = _evidence_safe(value)
    return _evidence_safe(identity)


_merge_state_evidence = _scheduler_candidates._merge_state_evidence


def _candidate_basin_manifest(
    candidate: SchedulerCandidate,
    *,
    output_uri: str,
    orchestration_run_id: str | None = None,
) -> dict[str, Any]:
    resource_profile = dict(candidate.resource_profile)
    manifest = {
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id,
        "cycle_id": candidate.cycle_id,
        "cycle_time": format_cycle_time(candidate.cycle_time_utc),
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "segment_count": candidate.segment_count,
        "output_segment_count": candidate.output_segment_count,
        "model_package_uri": candidate.model_package_uri,
        "model_package_manifest_uri": _model_package_manifest_uri(candidate),
        "resource_profile": dict(candidate.resource_profile),
        "display_capabilities": dict(candidate.display_capabilities),
        "frequency_capabilities": dict(candidate.frequency_capabilities),
        "scenario_id": candidate.scenario_id,
        "run_id": candidate.run_id,
        "canonical_product_id": _candidate_canonical_product_id(candidate),
        "forcing_version_id": candidate.forcing_version_id,
        "forcing_package_uri": resource_profile.get("forcing_package_uri"),
        "forcing_package_manifest_uri": resource_profile.get("forcing_package_manifest_uri"),
        "forcing_manifest_checksum": resource_profile.get("forcing_manifest_checksum"),
        "forcing_uri": resource_profile.get("forcing_uri"),
        "forcing_file_uris": dict(resource_profile.get("forcing_file_uris") or {}),
        "hydro_run_id": candidate.run_id,
        "published_manifest_id": _candidate_published_manifest_id(candidate),
        "forecast_horizon_hours": candidate.horizon.get("forecast_horizon_hours")
        or candidate.horizon.get("max_lead_hours"),
        "max_lead_hours": candidate.horizon.get("max_lead_hours"),
        "station_count": _candidate_station_count(candidate),
        "station_ids": _candidate_station_ids(candidate),
        "frequency_curves_available": _nested_bool(
            candidate.frequency_capabilities,
            "curves_available",
            fallback=_nested_bool(candidate.frequency_capabilities, "return_periods"),
        ),
        "warning_thresholds_available": _nested_bool(candidate.frequency_capabilities, "warning_thresholds_available"),
        "optional_weather_available": _nested_bool(candidate.display_capabilities, "optional_weather_available"),
        "output_river": _candidate_output_river_manifest(candidate),
        "output_key": _candidate_output_key(candidate),
        "output_uri": output_uri,
    }
    if orchestration_run_id not in (None, ""):
        manifest["orchestration_run_id"] = orchestration_run_id
    pipeline_job_id = _candidate_contract_pipeline_job_id(candidate)
    if pipeline_job_id not in (None, ""):
        manifest["pipeline_job_id"] = pipeline_job_id
    _apply_candidate_warm_start_fields(manifest, candidate)
    if candidate.state_evidence:
        state_evidence = _evidence_safe(candidate.state_evidence)
        manifest["state_evidence"] = state_evidence
        restart_stage = state_evidence.get("restart_stage") if isinstance(state_evidence, Mapping) else None
        # Defense in depth: fresh full-chain ingestion never carries a basin
        # restart_stage even if a residual marker survived upstream merges.
        if restart_stage and not _candidate_is_fresh_full_chain(candidate):
            manifest["restart_stage"] = restart_stage
        if state_evidence.get("durable_shud_output_reused") is True:
            manifest["durable_shud_output_reused"] = True
            manifest["native_shud_resubmitted"] = False
        retry_attempt = _candidate_manual_retry_attempt(candidate)
        if retry_attempt is not None:
            manifest["manual_retry_attempt"] = retry_attempt
            manifest["retry_attempt"] = retry_attempt
    forcing_metadata = resource_profile.get("forcing_station_metadata")
    if isinstance(forcing_metadata, Mapping):
        manifest["forcing_station_metadata"] = dict(forcing_metadata)
    slurm_env = resource_profile.get("slurm_env")
    if isinstance(slurm_env, Mapping):
        manifest["slurm_env"] = {str(key): str(value) for key, value in slurm_env.items()}
    project_identity = _resource_profile_project_identity(resource_profile)
    if project_identity is not None:
        manifest["project_name"] = project_identity["project_name"]
        manifest["shud_input_name"] = project_identity["shud_input_name"]
    package_checksum = resource_profile.get("package_checksum")
    if package_checksum not in (None, ""):
        manifest["package_checksum"] = str(package_checksum)
        manifest.setdefault("model_package_checksum", str(package_checksum))
    source_inventory_checksum = resource_profile.get("source_inventory_checksum")
    if source_inventory_checksum not in (None, ""):
        manifest["source_inventory_checksum"] = str(source_inventory_checksum)
    return manifest


def _candidate_manual_retry_attempt(candidate: SchedulerCandidate) -> int | None:
    state_evidence = candidate.state_evidence
    if not isinstance(state_evidence, Mapping):
        return None
    manual_retry = state_evidence.get("manual_retry")
    if not isinstance(manual_retry, Mapping):
        return None
    if manual_retry.get("allowed") is False:
        return None
    for key in ("new_attempt", "attempt", "retry_count"):
        try:
            value = int(manual_retry.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _apply_candidate_warm_start_fields(manifest: dict[str, Any], candidate: SchedulerCandidate) -> None:
    """Carry warm-start init-state fields onto the scheduler basin record.

    When the candidate's ``state_evidence`` already names a selected warm-start state
    (``candidate_state``), copy ``init_state_*`` + lineage onto the basin manifest so
    the scheduler basin record agrees with the cycle-stage and forecast runtime
    manifests (M24 §2 Lane 2). When absent, the orchestrator's ``_apply_cohort_warm_start``
    is authoritative and fills these fields; this is a no-op passthrough.
    """

    evidence = candidate.state_evidence
    if not isinstance(evidence, Mapping):
        return
    selected = evidence.get("candidate_state")
    if not isinstance(selected, Mapping):
        return
    uri = selected.get("init_state_uri") or selected.get("state_uri") or selected.get("ic_file_uri")
    if uri in (None, ""):
        return
    manifest["init_state_uri"] = str(uri)
    for key in ("init_state_id", "init_state_checksum", "init_state_valid_time", "init_state_quality"):
        value = selected.get(key) or selected.get(key.removeprefix("init_state_"))
        if value not in (None, ""):
            manifest[key] = value
    lineage = selected.get("init_state_lineage") or selected.get("lineage")
    if isinstance(lineage, Mapping):
        manifest["init_state_lineage"] = dict(lineage)


def _resource_profile_project_identity(resource_profile: Mapping[str, Any]) -> dict[str, str] | None:
    project_name = resource_profile.get("project_name")
    shud_input_name = resource_profile.get("shud_input_name")
    project = str(project_name) if project_name not in (None, "") else None
    shud_input = str(shud_input_name) if shud_input_name not in (None, "") else None
    if project is None and shud_input is None:
        return None
    return {"project_name": project or shud_input or "", "shud_input_name": shud_input or project or ""}
