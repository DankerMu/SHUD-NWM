from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.orchestrator.production_contract import ProductionContractError, validate_compatible_production_identity
from services.orchestrator.scheduler_state_common import (
    _candidate_canonical_product_id,
    _candidate_production_identity,
    _candidate_published_manifest_id,
    _evidence_safe,
    _format_utc,
)
from services.orchestrator.scheduler_state_identity_filter import (
    _candidate_state_source_allows_nested_authority,
    _candidate_state_source_has_authoritative_ancestor,
)
from services.orchestrator.scheduler_state_manual_retry import _manual_retry_payload
from services.orchestrator.scheduler_state_rows import (
    _bounded_candidate_state,
    _event_identity_containers,
    _job_state_evidence,
    _legacy_compatible_state_row,
    _optional_mapping_state,
    _state_events,
    _state_jobs,
    _state_output_uri,
    _state_overflow_evidence,
    _state_retry_attempt,
    _state_retry_limit,
    _state_row_has_authoritative_candidate_proof,
    _state_row_has_m23_comparison_evidence,
    _state_row_is_scoped_to_other_candidate,
    _state_status,
)
from services.orchestrator.scheduler_state_types import SchedulerCandidateLike


def _bounded_active_slurm_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    max_jobs: int,
) -> list[dict[str, Any]]:
    bounded = [_evidence_safe(dict(job)) for job in list(jobs)[: max(int(max_jobs), 1)] if isinstance(job, Mapping)]
    total = len(jobs)
    if total > max_jobs:
        bounded.append(
            {
                "overflow": True,
                "reason": "active_slurm_job_limit_applied",
                "returned": len(bounded),
                "total": total,
                "limit": max_jobs,
            }
        )
    return bounded

def _candidate_state_evidence(candidate: SchedulerCandidateLike, state: Mapping[str, Any]) -> dict[str, Any]:
    state = _bounded_candidate_state(state)
    jobs = [_job_state_evidence(job) for job in _state_jobs(state)]
    events = [_evidence_safe(event) for event in _state_events(state)]
    identity_validation = _candidate_state_identity_validation(candidate, state)
    evidence = {
        "candidate_identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
            "canonical_product_id": _candidate_canonical_product_id(candidate),
            "forcing_version_id": candidate.forcing_version_id,
            "hydro_run_id": candidate.run_id,
            "published_manifest_id": _candidate_published_manifest_id(candidate),
            "source_id": candidate.source_id,
            "source": candidate.source_id,
            "cycle_time_utc": _format_utc(candidate.cycle_time_utc),
            "cycle_time": _format_utc(candidate.cycle_time_utc),
            "model_id": candidate.model_id,
            "scenario_id": candidate.scenario_id,
            "basin_id": candidate.basin_id,
            "basin_version_id": candidate.basin_version_id,
            "river_network_version_id": candidate.river_network_version_id,
        },
        "production_identity_validation": identity_validation,
        "pipeline_jobs": jobs,
        "pipeline_events": events,
        "hydro_run": _optional_mapping_state(
            state.get("hydro_run"),
            defaults={
                "run_id": state.get("run_id") or candidate.run_id,
                "status": _state_status(state, "hydro_status", "hydro_run_status"),
                "output_uri": _state_output_uri(state),
                "error_code": state.get("hydro_error_code"),
                "error_message": state.get("hydro_error_message"),
            },
        ),
        "forcing_version": _optional_mapping_state(
            state.get("forcing_version"),
            defaults={
                "forcing_version_id": state.get("forcing_version_id") or candidate.forcing_version_id,
                "status": _state_status(state, "forcing_status", "forcing_version_status"),
            },
        ),
        "forecast_cycle": _optional_mapping_state(
            state.get("forecast_cycle"),
            defaults={
                "cycle_id": state.get("cycle_id") or candidate.cycle_id,
                "status": _state_status(state, "cycle_status", "forecast_cycle_status"),
            },
        ),
        "manual_retry": _manual_retry_payload(state),
        "retry": {
            "attempt": _state_retry_attempt(state),
            "retry_limit": _state_retry_limit(state),
        },
    }
    repaired_stage = state.get("repaired_stage_evidence")
    if isinstance(repaired_stage, Mapping):
        evidence["repaired_stage_evidence"] = _evidence_safe(dict(repaired_stage))
    source_cycle_repair = state.get("source_cycle_repair_evidence")
    if isinstance(source_cycle_repair, Mapping):
        evidence["source_cycle_repair_evidence"] = _evidence_safe(dict(source_cycle_repair))
    nfs_raw_manifest = state.get("nfs_raw_manifest")
    if isinstance(nfs_raw_manifest, Mapping):
        evidence["nfs_raw_manifest"] = _evidence_safe(dict(nfs_raw_manifest))
    overflow = _state_overflow_evidence(state)
    if overflow:
        evidence["state_bounds"] = overflow
    return evidence

def _candidate_state_identity_validation(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    state = _bounded_candidate_state(state)
    expected = _candidate_production_identity(candidate)
    containers: list[tuple[str, Mapping[str, Any]]] = [("candidate_state", state)]
    for key in ("hydro_run", "forcing_version", "forecast_cycle", "published_manifest", "canonical_product"):
        value = state.get(key)
        if isinstance(value, Mapping):
            containers.append((key, value))
    for key in ("pipeline_job", "job"):
        value = state.get(key)
        if isinstance(value, Mapping):
            containers.append((key, value))
    for index, job in enumerate(_state_jobs(state)):
        containers.append((f"pipeline_jobs[{index}]", job))
    for index, event in enumerate(_state_events(state)):
        containers.extend(_event_identity_containers(index, event))
    mismatches: list[dict[str, Any]] = []
    compared: dict[str, dict[str, Any]] = {}
    legacy_non_authoritative: list[str] = []
    records = [
        {
            "source": source,
            "payload": payload,
            "authoritative": _state_row_has_authoritative_candidate_proof(
                expected,
                payload,
                include_nested=_candidate_state_source_allows_nested_authority(source),
            ),
        }
        for source, payload in containers
    ]
    authoritative_sources = {str(record["source"]) for record in records if record["authoritative"] is True}
    for record in records:
        source = str(record["source"])
        payload = record["payload"]
        if not isinstance(payload, Mapping):
            continue
        scoped_to_other_candidate = _state_row_is_scoped_to_other_candidate(expected, payload)
        authoritative = record["authoritative"] is True
        if not scoped_to_other_candidate and (authoritative or _state_row_has_m23_comparison_evidence(payload)):
            validation_payload = _legacy_compatible_state_row(expected, payload)
            try:
                fields = validate_compatible_production_identity(expected, validation_payload)
            except ProductionContractError as exc:
                mismatches.append({"source": source, **exc.to_dict()})
                continue
            if fields:
                compared[source] = fields
        if (
            bool(payload)
            and not authoritative
            and not _candidate_state_source_has_authoritative_ancestor(source, authoritative_sources)
        ):
            legacy_non_authoritative.append(source)
    return {
        "schema_version": "nhms.production.identity_validation.v1",
        "status": "mismatch" if mismatches else "compatible",
        "checked_sources": [source for source, _payload in containers],
        "compared": compared,
        "legacy_non_authoritative": legacy_non_authoritative,
        "mismatches": mismatches,
    }
