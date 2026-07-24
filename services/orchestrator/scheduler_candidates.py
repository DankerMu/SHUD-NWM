from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from packages.common.source_identity import normalize_source_id
from services.orchestrator import scheduler_discovery as _scheduler_discovery
from services.orchestrator.chain_source_cycle import (
    RAW_MANIFEST_READY_CYCLE_STATUSES,
    _raw_manifest_uri_matches_source_cycle,
)
from services.orchestrator.scheduler_file_providers import _public_raw_manifest_evidence
from services.orchestrator.scheduler_state import (
    CandidateStateDecision,
    _bounded_active_slurm_jobs,
    _call_active_slurm_jobs_provider,
    _call_candidate_state_provider,
    _candidate_canonical_product_id,
    _candidate_repaired_state_audit_evidence,
    _candidate_state_decision,
    _candidate_state_has_identity_mismatch,
    _candidate_state_is_candidate_scoped_retry,
    _ensure_utc,
    _evidence_safe,
    _format_utc,
)
from services.orchestrator.source_cycle_raw_manifest import (
    NFS_RAW_MANIFEST_READY_SOURCE,
    nfs_raw_manifest_readiness,
)
from workers.data_adapters.base import CycleDiscovery, cycle_id_for
from workers.forcing_producer.direct_grid_contract import (
    DirectGridContractError,
    load_forcing_mapping_contract_from_manifest,
)

# Source-scope fail-closed reason codes (Epic #961, §4.1). Inline sentinels — no
# module-level enum: the codes appear only at the two raise/append sites below
# and on the evidence keys under state_evidence.
_DIRECT_GRID_SOURCE_OUT_OF_SCOPE_REASON = "direct_grid_source_out_of_scope"
_DIRECT_GRID_CONTRACT_INVALID_REASON = "direct_grid_contract_invalid"
_DIRECT_GRID_SOURCE_SCOPE_ERROR_MESSAGE = "Direct-grid contract does not apply to the current source."

MAX_CANDIDATES = 10000
UNKNOWN_AFTER_ATTEMPT = "unknown_after_attempt"

CANDIDATE_CONSTRUCTION_TERMINAL_PIPELINE_STATUSES = {
    "succeeded",
    "partially_failed",
    "failed",
    "cancelled",
    "submission_failed",
    "permanently_failed",
}
_STRICT_WARM_START_TERMINAL_SKIP_REASONS = {"terminal_hydro_success", "terminal_pipeline_success"}

SchedulerResourceLimitError = _scheduler_discovery.SchedulerResourceLimitError
_source_discovery_evidence_safe = _scheduler_discovery._source_discovery_evidence_safe
_source_secret_text_safe = _scheduler_discovery._source_secret_text_safe


class SchedulerCandidateLike(Protocol):
    candidate_id: str
    source_id: str
    cycle_id: str
    cycle_time_utc: datetime
    model_id: str
    basin_id: str
    basin_version_id: str | None
    river_network_version_id: str | None
    segment_count: int | None
    output_segment_count: int | None
    model_package_uri: str
    resource_profile: Mapping[str, Any]
    display_capabilities: Mapping[str, Any]
    horizon: Mapping[str, Any]
    scenario_id: str
    run_id: str
    forcing_version_id: str
    status: str
    reason: str | None
    state_evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError


class SchedulerModelLike(Protocol):
    model_id: str


class SchedulerConfigLike(Protocol):
    retry_limit: int
    candidate_state_job_limit: int
    candidate_state_event_limit: int
    cancel_active_slurm: bool
    dry_run: bool
    require_direct_grid: bool
    repair_missing_forcing: bool
    repair_missing_forcing_cycle_time: datetime | None
    nfs_raw_manifest_root: str | Path | None
    nfs_raw_manifest_prefix: str


class SchedulerSourceCycleLike(Protocol):
    discovery: CycleDiscovery
    horizon: Mapping[str, Any]


@dataclass(frozen=True)
class SchedulerCandidateConstructionContext:
    config: SchedulerConfigLike
    active_repository: Any | None
    canonical_readiness_for_candidate: Callable[
        [SchedulerCandidateLike, SchedulerSourceCycleLike],
        Mapping[str, Any] | None,
    ]
    strict_warm_start_for_candidate: Callable[
        [SchedulerCandidateLike, SchedulerSourceCycleLike],
        Mapping[str, Any] | None,
    ]
    orchestrator_for: Callable[[str], Any]
    candidate_factory: Callable[..., SchedulerCandidateLike]
    candidate_state_provider_caller: Callable[..., Any] = _call_candidate_state_provider
    active_slurm_jobs_provider_caller: Callable[..., Any] = _call_active_slurm_jobs_provider
    active_slurm_jobs_bounder: Callable[..., list[dict[str, Any]]] = _bounded_active_slurm_jobs
    candidate_state_decider: Callable[
        [SchedulerCandidateLike, Mapping[str, Any] | None],
        CandidateStateDecision | None,
    ] = _candidate_state_decision
    candidate_state_identity_mismatch_detector: Callable[[Mapping[str, Any]], bool] = (
        _candidate_state_has_identity_mismatch
    )
    candidate_state_scoped_retry_detector: Callable[[CandidateStateDecision | None], bool] = (
        _candidate_state_is_candidate_scoped_retry
    )
    repaired_state_audit_evidence_builder: Callable[
        [SchedulerCandidateLike, Mapping[str, Any] | None],
        dict[str, Any] | None,
    ] = _candidate_repaired_state_audit_evidence
    successor_state_for_candidate: Callable[
        [SchedulerCandidateLike, SchedulerSourceCycleLike],
        Mapping[str, Any] | None,
    ] | None = None
    max_candidates: int = MAX_CANDIDATES


def build_candidates(
    context: SchedulerCandidateConstructionContext,
    *,
    models: Sequence[SchedulerModelLike],
    cycles: Sequence[SchedulerSourceCycleLike],
    allow_slurm_status_sync: bool = False,
) -> tuple[
    list[SchedulerCandidateLike],
    list[SchedulerCandidateLike],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    candidates: list[SchedulerCandidateLike] = []
    blocked: list[SchedulerCandidateLike] = []
    skipped: list[dict[str, Any]] = []
    duplicate_exclusions: list[dict[str, Any]] = []
    slurm_status_sync_evidence: list[dict[str, Any]] = []
    seen_candidate_ids: set[str] = set()
    active_orchestration_provider = (
        getattr(context.active_repository, "has_active_orchestration", None)
        if context.active_repository is not None
        else None
    )
    completed_provider = (
        getattr(context.active_repository, "has_completed_pipeline", None)
        if context.active_repository is not None
        else None
    )
    state_provider = (
        getattr(context.active_repository, "candidate_state", None)
        if context.active_repository is not None
        else None
    )
    active_slurm_jobs_provider = (
        getattr(context.active_repository, "active_slurm_jobs", None)
        if context.active_repository is not None
        else None
    )
    max_candidates = int(context.max_candidates)
    for cycle in cycles:
        discovery = cycle.discovery
        has_active_orchestration: bool | None = None
        for model in models:
            model_source_is_out_of_scope = _direct_grid_model_source_is_out_of_scope(
                model,
                discovery,
            )
            repair_target = _is_explicit_missing_forcing_repair_target(
                context.config,
                discovery.cycle_time,
            )
            if model_source_is_out_of_scope and not repair_target:
                continue
            if len(candidates) + len(blocked) + len(skipped) >= max_candidates:
                raise SchedulerResourceLimitError(
                    "candidate_limit_exceeded",
                    {
                        "max_candidates": max_candidates,
                        "source_cycle_count": len(cycles),
                        "selected_model_count": len(models),
                    },
                )
            candidate = context.candidate_factory(discovery=discovery, model=model, horizon=cycle.horizon)
            if candidate.candidate_id in seen_candidate_ids:
                exclusion = {
                    **candidate.to_dict(),
                    "status": "excluded",
                    "reason": "duplicate_candidate_identity",
                }
                skipped.append(exclusion)
                duplicate_exclusions.append({"type": "candidate", **exclusion})
                continue
            seen_candidate_ids.add(candidate.candidate_id)
            raw_candidate_state: Mapping[str, Any] | None = None
            state_decision: CandidateStateDecision | None = None
            candidate_state_classified = False

            def classify_candidate_state() -> None:
                nonlocal raw_candidate_state, state_decision, candidate_state_classified
                if candidate_state_classified:
                    return
                raw_candidate_state = (
                    context.candidate_state_provider_caller(
                        state_provider,
                        source_id=discovery.source_id,
                        cycle_time=discovery.cycle_time,
                        model_id=model.model_id,
                        run_id=candidate.run_id,
                        forcing_version_id=candidate.forcing_version_id,
                        candidate_id=candidate.candidate_id,
                        retry_limit=context.config.retry_limit,
                        job_limit=context.config.candidate_state_job_limit,
                        event_limit=context.config.candidate_state_event_limit,
                    )
                    if callable(state_provider)
                    else None
                )
                state_decision = context.candidate_state_decider(candidate, raw_candidate_state)
                candidate_state_classified = True

            if model_source_is_out_of_scope:
                classify_candidate_state()
                direct_grid_scope_block = _direct_grid_source_scope_block(candidate, discovery)
                repair_blocker = _repair_precondition_blocker(
                    context.config,
                    candidate,
                    state_decision,
                    direct_grid_scope_block,
                    precondition="direct_grid_contract",
                )
                if repair_blocker is not None:
                    blocked.append(repair_blocker)
                continue
            if not discovery.available:
                if repair_target:
                    classify_candidate_state()
                discovery_block = _blocked_candidate(
                    candidate,
                    discovery.reason or "source_cycle_unavailable",
                    state_evidence=_source_blocked_evidence(candidate, discovery),
                )
                repair_blocker = _repair_precondition_blocker(
                    context.config,
                    candidate,
                    state_decision,
                    discovery_block,
                    precondition="source_discovery",
                )
                blocked.append(repair_blocker or discovery_block)
                continue
            direct_grid_scope_block = _direct_grid_source_scope_block(candidate, discovery)
            if direct_grid_scope_block is not None:
                if repair_target:
                    classify_candidate_state()
                repair_blocker = _repair_precondition_blocker(
                    context.config,
                    candidate,
                    state_decision,
                    direct_grid_scope_block,
                    precondition="direct_grid_contract",
                )
                blocked.append(repair_blocker or direct_grid_scope_block)
                continue
            if has_active_orchestration is None:
                has_active_orchestration = bool(
                    callable(active_orchestration_provider)
                    and active_orchestration_provider(
                        source_id=discovery.source_id,
                        cycle_time=discovery.cycle_time,
                    )
                )
            if (
                has_active_orchestration
                and not context.config.cancel_active_slurm
                and not callable(state_provider)
            ):
                skipped.append({**candidate.to_dict(), "reason": "active_duplicate_pipeline"})
                continue
            if (
                not context.config.cancel_active_slurm
                and not callable(state_provider)
                and context.active_repository is not None
                and context.active_repository.has_active_pipeline(
                    source_id=discovery.source_id,
                    cycle_time=discovery.cycle_time,
                    model_id=model.model_id,
                )
            ):
                skipped.append({**candidate.to_dict(), "reason": "active_duplicate_pipeline"})
                continue
            strict_warm_start = context.strict_warm_start_for_candidate(candidate, cycle)
            if (
                strict_warm_start is not None
                and not bool(strict_warm_start.get("ready"))
                and not bool(getattr(context.config, "repair_missing_forcing", False))
            ):
                blocked.append(
                    _blocked_candidate(
                        candidate,
                        str(strict_warm_start.get("reason") or "state_snapshot_index_unavailable"),
                        state_evidence=strict_warm_start,
                    )
                )
                continue
            successor_state = (
                context.successor_state_for_candidate(candidate, cycle)
                if callable(context.successor_state_for_candidate)
                else None
            )
            if callable(completed_provider) and completed_provider(
                source_id=discovery.source_id,
                cycle_time=discovery.cycle_time,
                model_id=model.model_id,
            ) and strict_warm_start is None and not callable(state_provider) and _successor_state_terminal_can_skip(
                successor_state
            ):
                skipped.append({**candidate.to_dict(), "reason": "completed_duplicate_pipeline"})
                continue
            classify_candidate_state()
            state_decision = _apply_explicit_missing_forcing_repair_policy(
                context.config,
                candidate,
                raw_candidate_state,
                state_decision,
                strict_warm_start=strict_warm_start,
            )
            if state_decision is not None and context.candidate_state_identity_mismatch_detector(
                state_decision.evidence,
            ):
                blocked.append(
                    _blocked_candidate(
                        candidate,
                        "production_identity_mismatch",
                        state_evidence=state_decision.evidence,
                    )
                )
                continue
            if state_decision is not None and state_decision.action == "blocked":
                blocked.append(
                    _blocked_candidate(
                        candidate,
                        state_decision.reason or "candidate_state_blocked",
                        state_evidence=state_decision.evidence,
                    )
                )
                continue
            if (
                state_decision is not None
                and state_decision.action == "skip"
                and state_decision.reason != "active_slurm_job"
            ):
                if (
                    strict_warm_start is not None
                    and state_decision.reason in _STRICT_WARM_START_TERMINAL_SKIP_REASONS
                ):
                    if _terminal_decision_matches_strict_warm_start(state_decision.evidence, strict_warm_start):
                        if not _terminal_decision_run_manifest_matches_strict_warm_start(
                            state_decision.evidence,
                            strict_warm_start,
                        ):
                            state_decision = CandidateStateDecision(
                                "retry",
                                "strict_warm_start_terminal_run_manifest_missing",
                                _strict_warm_start_run_manifest_retry_evidence(
                                    state_decision.evidence,
                                    strict_warm_start,
                                ),
                            )
                        elif successor_state is not None and not bool(successor_state.get("ready")):
                            state_decision = CandidateStateDecision(
                                "retry",
                                "strict_warm_start_successor_checkpoint_missing",
                                _strict_warm_start_successor_retry_evidence(
                                    state_decision.evidence,
                                    successor_state,
                                ),
                            )
                        else:
                            skipped.append(
                                {
                                    **candidate.to_dict(),
                                    "reason": state_decision.reason,
                                    "state_evidence": _evidence_safe(
                                        _merge_state_evidence(state_decision.evidence, strict_warm_start)
                                    ),
                                }
                            )
                            continue
                    else:
                        state_decision = CandidateStateDecision(
                            "retry",
                            "strict_warm_start_terminal_init_state_mismatch",
                            _strict_warm_start_terminal_retry_evidence(state_decision.evidence, strict_warm_start),
                        )
                elif (
                    successor_state is not None
                    and not bool(successor_state.get("ready"))
                    and state_decision.reason in _STRICT_WARM_START_TERMINAL_SKIP_REASONS
                ):
                    state_decision = CandidateStateDecision(
                        "retry",
                        "strict_warm_start_successor_checkpoint_missing",
                        _strict_warm_start_successor_retry_evidence(state_decision.evidence, successor_state),
                    )
                elif (
                    state_decision.reason in _STRICT_WARM_START_TERMINAL_SKIP_REASONS
                    and not _terminal_decision_has_run_manifest(state_decision.evidence)
                ):
                    state_decision = CandidateStateDecision(
                        "retry",
                        "terminal_run_manifest_missing",
                        _terminal_run_manifest_retry_evidence(state_decision.evidence),
                    )
                else:
                    skipped.append(
                        {
                            **candidate.to_dict(),
                            "reason": state_decision.reason,
                            "state_evidence": _evidence_safe(state_decision.evidence),
                        }
                    )
                    continue
            state_decision = _upgrade_retry_for_strict_warm_start_manifest(
                state_decision,
                strict_warm_start,
            )
            if strict_warm_start is not None:
                candidate = _candidate_with_state_evidence(candidate, strict_warm_start)
            if (
                state_decision is not None
                and state_decision.action == "skip"
                and state_decision.reason == "active_slurm_job"
            ):
                active_slurm_jobs = (
                    list(
                        context.active_slurm_jobs_provider_caller(
                            active_slurm_jobs_provider,
                            source_id=discovery.source_id,
                            cycle_time=discovery.cycle_time,
                            model_id=model.model_id,
                            limit=context.config.candidate_state_job_limit,
                        )
                    )
                    if callable(active_slurm_jobs_provider)
                    else list(state_decision.evidence.get("active_slurm_jobs", []))
                )
                active_slurm_jobs = context.active_slurm_jobs_bounder(
                    active_slurm_jobs,
                    max_jobs=context.config.candidate_state_job_limit,
                )
                if active_slurm_jobs and not context.config.cancel_active_slurm and not context.config.dry_run:
                    cycle_id = cycle_id_for(discovery.source_id, discovery.cycle_time)
                    sync = None
                    if allow_slurm_status_sync:
                        sync = getattr(context.orchestrator_for(discovery.source_id), "sync_cycle_statuses", None)
                    if allow_slurm_status_sync and callable(sync):
                        try:
                            synced_updates = context.active_slurm_jobs_bounder(
                                [dict(item) for item in sync(cycle_id)],
                                max_jobs=context.config.candidate_state_job_limit,
                            )
                        except Exception as error:
                            slurm_state_sync = _slurm_status_sync_failed_evidence(
                                candidate,
                                cycle_id=cycle_id,
                                active_slurm_jobs=active_slurm_jobs,
                                error=error,
                            )
                            slurm_status_sync_evidence.append(slurm_state_sync)
                            skipped.append(
                                {
                                    **candidate.to_dict(),
                                    "reason": "active_slurm_status_sync_failed",
                                    "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                                    "sync_required": True,
                                    "sync_attempted": True,
                                    "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
                                    "state_evidence": {"slurm_state_sync": slurm_state_sync},
                                }
                            )
                            continue
                        slurm_state_sync = {
                            "cycle_id": cycle_id,
                            "status": "synced",
                            "updates": synced_updates,
                            "terminal_updates": [
                                item
                                for item in synced_updates
                                if str(item.get("status") or "")
                                in CANDIDATE_CONSTRUCTION_TERMINAL_PIPELINE_STATUSES
                            ],
                        }
                        slurm_status_sync_evidence.append(slurm_state_sync)
                        if synced_updates:
                            if callable(state_provider):
                                raw_candidate_state = context.candidate_state_provider_caller(
                                    state_provider,
                                    source_id=discovery.source_id,
                                    cycle_time=discovery.cycle_time,
                                    model_id=model.model_id,
                                    run_id=candidate.run_id,
                                    forcing_version_id=candidate.forcing_version_id,
                                    candidate_id=candidate.candidate_id,
                                    retry_limit=context.config.retry_limit,
                                    job_limit=context.config.candidate_state_job_limit,
                                    event_limit=context.config.candidate_state_event_limit,
                                )
                                state_decision = context.candidate_state_decider(candidate, raw_candidate_state)
                                state_decision = _apply_explicit_missing_forcing_repair_policy(
                                    context.config,
                                    candidate,
                                    raw_candidate_state,
                                    state_decision,
                                    strict_warm_start=strict_warm_start,
                                )
                            if state_decision is not None:
                                state_decision = CandidateStateDecision(
                                    action=state_decision.action,
                                    reason=state_decision.reason,
                                    evidence={
                                        **dict(state_decision.evidence),
                                        "slurm_state_sync": slurm_state_sync,
                                    },
                                )
                            if state_decision is not None and context.candidate_state_identity_mismatch_detector(
                                state_decision.evidence,
                            ):
                                blocked.append(
                                    _blocked_candidate(
                                        candidate,
                                        "production_identity_mismatch",
                                        state_evidence=state_decision.evidence,
                                    )
                                )
                                continue
                            active_slurm_jobs = (
                                context.active_slurm_jobs_bounder(
                                    list(
                                        context.active_slurm_jobs_provider_caller(
                                            active_slurm_jobs_provider,
                                            source_id=discovery.source_id,
                                            cycle_time=discovery.cycle_time,
                                            model_id=model.model_id,
                                            limit=context.config.candidate_state_job_limit,
                                        )
                                    ),
                                    max_jobs=context.config.candidate_state_job_limit,
                                )
                                if callable(active_slurm_jobs_provider)
                                else []
                            )
                            if state_decision is not None and state_decision.action == "retry":
                                candidate = _candidate_with_state_evidence(candidate, state_decision.evidence)
                            elif state_decision is None:
                                state_evidence = {"slurm_state_sync": slurm_state_sync}
                                repaired_state_evidence = context.repaired_state_audit_evidence_builder(
                                    candidate,
                                    raw_candidate_state,
                                )
                                if repaired_state_evidence is not None:
                                    state_evidence.update(repaired_state_evidence)
                                candidate = _candidate_with_state_evidence(
                                    candidate,
                                    state_evidence,
                                )
                        if active_slurm_jobs:
                            skip_evidence = dict(state_decision.evidence if state_decision is not None else {})
                            skip_evidence["slurm_state_sync"] = slurm_state_sync
                            skipped.append(
                                {
                                    **candidate.to_dict(),
                                    "reason": "active_slurm_job",
                                    "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                                    "state_evidence": _evidence_safe(skip_evidence),
                                }
                            )
                            continue
                    if not allow_slurm_status_sync:
                        skipped.append(
                            {
                                **candidate.to_dict(),
                                "reason": "active_slurm_status_sync_deferred",
                                "cycle_id": cycle_id,
                                "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                                "sync_required": True,
                                "sync_attempted": False,
                                "mutation_occurred": False,
                            }
                        )
                        continue
                if active_slurm_jobs:
                    if context.config.cancel_active_slurm:
                        skipped.append(
                            {
                                **candidate.to_dict(),
                                "reason": "cancel_requested_active_slurm",
                                "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                                "replacement_submitted": False,
                            }
                        )
                    else:
                        skipped.append(
                            {
                                **candidate.to_dict(),
                                "reason": "active_slurm_job",
                                "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                                "state_evidence": _evidence_safe(state_decision.evidence),
                            }
                        )
                    continue
            nfs_raw_manifest_gate = _nfs_raw_manifest_gate(raw_candidate_state)
            if (
                nfs_raw_manifest_gate is not None
                and bool(nfs_raw_manifest_gate.get("required"))
                and nfs_raw_manifest_gate.get("status") != "ready"
            ):
                blocked.append(
                    _blocked_candidate(
                        candidate,
                        _nfs_raw_manifest_block_reason(nfs_raw_manifest_gate),
                        state_evidence={"nfs_raw_manifest": nfs_raw_manifest_gate},
                    )
                )
                continue
            if (
                nfs_raw_manifest_gate is not None
                and nfs_raw_manifest_gate.get("status") == "ready"
                and not _nfs_raw_manifest_matches_source_cycle(candidate, nfs_raw_manifest_gate)
            ):
                blocked.append(
                    _blocked_candidate(
                        candidate,
                        "nfs_raw_manifest_identity_mismatch",
                        state_evidence={"nfs_raw_manifest": nfs_raw_manifest_gate},
                    )
                )
                continue
            if nfs_raw_manifest_gate is not None and nfs_raw_manifest_gate.get("status") == "ready":
                candidate = _candidate_with_state_evidence(candidate, {"nfs_raw_manifest": nfs_raw_manifest_gate})
            raw_manifest_restart = _source_raw_manifest_restart_evidence(candidate, raw_candidate_state)
            raw_manifest_restart_applied = False
            canonical_readiness = context.canonical_readiness_for_candidate(candidate, cycle)
            if canonical_readiness is not None and not bool(canonical_readiness.get("ready")):
                if _decision_is_authorized_missing_forcing_repair(state_decision):
                    state_evidence = dict(state_decision.evidence)
                    repair = dict(state_evidence.get("missing_forcing_repair") or {})
                    repair.update(
                        {
                            "status": "rejected",
                            "reason": "canonical_not_ready",
                            "canonical_readiness": _evidence_safe(canonical_readiness),
                        }
                    )
                    state_evidence["missing_forcing_repair"] = repair
                    state_evidence.update(
                        {
                            "decision": "blocked_missing_forcing_package_uri",
                            "reason": "missing_forcing_package_uri",
                            "classifier": "missing_upstream_artifact",
                            "restart_stage": "forecast",
                            "restart_from_stage": "forecast",
                        }
                    )
                    blocked.append(
                        _blocked_candidate(
                            candidate,
                            "missing_forcing_package_uri",
                            state_evidence=state_evidence,
                        )
                    )
                    continue
                if _canonical_evidence_is_fresh_zero_row(canonical_readiness):
                    state_evidence = {}
                    if state_decision is not None and state_decision.action == "retry":
                        state_evidence.update(state_decision.evidence)
                    state_evidence["canonical_readiness"] = canonical_readiness
                    if raw_manifest_restart is not None:
                        state_evidence.update(raw_manifest_restart)
                        raw_manifest_restart_applied = True
                    else:
                        state_evidence.update(_production_raw_manifest_missing_evidence(raw_candidate_state))
                        blocked.append(
                            _blocked_candidate(
                                candidate,
                                "nfs_raw_manifest_required",
                                state_evidence=state_evidence,
                            )
                        )
                        continue
                    candidate = _candidate_with_state_evidence(candidate, state_evidence)
                else:
                    state_evidence = {"canonical_readiness": canonical_readiness}
                    if state_decision is not None:
                        state_evidence["candidate_state"] = state_decision.evidence
                    blocked.append(
                        _blocked_candidate(
                            candidate,
                            str(canonical_readiness.get("reason") or "canonical_incomplete"),
                            state_evidence=state_evidence,
                        )
                    )
                    continue
            elif canonical_readiness is not None:
                candidate = _candidate_with_state_evidence(
                    candidate,
                    {"canonical_readiness": canonical_readiness},
                )
            if (
                state_decision is not None
                and state_decision.action == "retry"
                and not _candidate_is_fresh_full_chain(candidate)
                and not raw_manifest_restart_applied
            ):
                candidate = _candidate_with_state_evidence(candidate, state_decision.evidence)
            if state_decision is None:
                repaired_state_evidence = context.repaired_state_audit_evidence_builder(
                    candidate,
                    raw_candidate_state,
                )
                if repaired_state_evidence is not None:
                    candidate = _candidate_with_state_evidence(candidate, repaired_state_evidence)
            if has_active_orchestration is None:
                has_active_orchestration = bool(
                    callable(active_orchestration_provider)
                    and active_orchestration_provider(
                        source_id=discovery.source_id,
                        cycle_time=discovery.cycle_time,
                    )
                )
            active_slurm_jobs = (
                list(
                    context.active_slurm_jobs_provider_caller(
                        active_slurm_jobs_provider,
                        source_id=discovery.source_id,
                        cycle_time=discovery.cycle_time,
                        model_id=model.model_id,
                        limit=context.config.candidate_state_job_limit,
                    )
                )
                if callable(active_slurm_jobs_provider)
                else []
            )
            active_slurm_jobs = context.active_slurm_jobs_bounder(
                active_slurm_jobs,
                max_jobs=context.config.candidate_state_job_limit,
            )
            slurm_state_sync: dict[str, Any] | None = None
            if active_slurm_jobs and not context.config.cancel_active_slurm and not context.config.dry_run:
                cycle_id = cycle_id_for(discovery.source_id, discovery.cycle_time)
                sync = None
                if allow_slurm_status_sync:
                    sync = getattr(context.orchestrator_for(discovery.source_id), "sync_cycle_statuses", None)
                if allow_slurm_status_sync and callable(sync):
                    try:
                        synced_updates = context.active_slurm_jobs_bounder(
                            [dict(item) for item in sync(cycle_id)],
                            max_jobs=context.config.candidate_state_job_limit,
                        )
                    except Exception as error:
                        slurm_state_sync = _slurm_status_sync_failed_evidence(
                            candidate,
                            cycle_id=cycle_id,
                            active_slurm_jobs=active_slurm_jobs,
                            error=error,
                        )
                        slurm_status_sync_evidence.append(slurm_state_sync)
                        skipped.append(
                            {
                                **candidate.to_dict(),
                                "reason": "active_slurm_status_sync_failed",
                                "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                                "sync_required": True,
                                "sync_attempted": True,
                                "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
                                "state_evidence": {"slurm_state_sync": slurm_state_sync},
                            }
                        )
                        continue
                    slurm_state_sync = {
                        "cycle_id": cycle_id,
                        "status": "synced",
                        "updates": synced_updates,
                        "terminal_updates": [
                            item
                            for item in synced_updates
                            if str(item.get("status") or "")
                            in CANDIDATE_CONSTRUCTION_TERMINAL_PIPELINE_STATUSES
                        ],
                    }
                    slurm_status_sync_evidence.append(slurm_state_sync)
                    if synced_updates:
                        if callable(state_provider):
                            raw_candidate_state = context.candidate_state_provider_caller(
                                state_provider,
                                source_id=discovery.source_id,
                                cycle_time=discovery.cycle_time,
                                model_id=model.model_id,
                                run_id=candidate.run_id,
                                forcing_version_id=candidate.forcing_version_id,
                                candidate_id=candidate.candidate_id,
                                retry_limit=context.config.retry_limit,
                                job_limit=context.config.candidate_state_job_limit,
                                event_limit=context.config.candidate_state_event_limit,
                            )
                            state_decision = context.candidate_state_decider(candidate, raw_candidate_state)
                            state_decision = _apply_explicit_missing_forcing_repair_policy(
                                context.config,
                                candidate,
                                raw_candidate_state,
                                state_decision,
                                strict_warm_start=strict_warm_start,
                            )
                        if state_decision is not None:
                            state_decision = CandidateStateDecision(
                                action=state_decision.action,
                                reason=state_decision.reason,
                                evidence={
                                    **dict(state_decision.evidence),
                                    "slurm_state_sync": slurm_state_sync,
                                },
                            )
                        if state_decision is not None and context.candidate_state_identity_mismatch_detector(
                            state_decision.evidence,
                        ):
                            blocked.append(
                                _blocked_candidate(
                                    candidate,
                                    "production_identity_mismatch",
                                    state_evidence=state_decision.evidence,
                                )
                            )
                            continue
                        active_slurm_jobs = (
                            context.active_slurm_jobs_bounder(
                                list(
                                    context.active_slurm_jobs_provider_caller(
                                        active_slurm_jobs_provider,
                                        source_id=discovery.source_id,
                                        cycle_time=discovery.cycle_time,
                                        model_id=model.model_id,
                                        limit=context.config.candidate_state_job_limit,
                                    )
                                ),
                                max_jobs=context.config.candidate_state_job_limit,
                            )
                            if callable(active_slurm_jobs_provider)
                            else []
                        )
                        if state_decision is not None and state_decision.action == "retry":
                            candidate = _candidate_with_state_evidence(candidate, state_decision.evidence)
                        elif state_decision is None:
                            state_evidence = {"slurm_state_sync": slurm_state_sync}
                            repaired_state_evidence = context.repaired_state_audit_evidence_builder(
                                candidate,
                                raw_candidate_state,
                            )
                            if repaired_state_evidence is not None:
                                state_evidence.update(repaired_state_evidence)
                            candidate = _candidate_with_state_evidence(candidate, state_evidence)
                elif not allow_slurm_status_sync:
                    skipped.append(
                        {
                            **candidate.to_dict(),
                            "reason": "active_slurm_status_sync_deferred",
                            "cycle_id": cycle_id,
                            "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                            "sync_required": True,
                            "sync_attempted": False,
                            "mutation_occurred": False,
                        }
                    )
                    continue
            if state_decision is not None and state_decision.action == "blocked":
                blocked.append(
                    _blocked_candidate(
                        candidate,
                        state_decision.reason or "candidate_state_blocked",
                        state_evidence=state_decision.evidence,
                    )
                )
                continue
            if (
                state_decision is not None
                and state_decision.action == "skip"
                and not (context.config.cancel_active_slurm and state_decision.reason == "active_slurm_job")
            ):
                skipped.append(
                    {
                        **candidate.to_dict(),
                        "reason": state_decision.reason,
                        "state_evidence": _evidence_safe(state_decision.evidence),
                    }
                )
                continue
            cycle_active_blocks_candidate = (
                has_active_orchestration
                and not callable(state_provider)
                and not (context.config.cancel_active_slurm and active_slurm_jobs)
            )
            if cycle_active_blocks_candidate and context.candidate_state_scoped_retry_detector(
                state_decision,
            ):
                cycle_active_blocks_candidate = False
            if cycle_active_blocks_candidate:
                skipped.append({**candidate.to_dict(), "reason": "active_duplicate_pipeline"})
                continue
            if active_slurm_jobs:
                active_slurm_skip: dict[str, Any]
                if context.config.cancel_active_slurm:
                    active_slurm_skip = {
                        **candidate.to_dict(),
                        "reason": "cancel_requested_active_slurm",
                        "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                        "replacement_submitted": False,
                    }
                else:
                    active_slurm_skip = {
                        **candidate.to_dict(),
                        "reason": "active_slurm_job",
                        "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                    }
                if slurm_state_sync is not None:
                    skip_evidence = dict(active_slurm_skip.get("state_evidence") or {})
                    skip_evidence["slurm_state_sync"] = slurm_state_sync
                    active_slurm_skip["state_evidence"] = _evidence_safe(skip_evidence)
                skipped.append(active_slurm_skip)
                continue
            if state_decision is not None and state_decision.action == "skip":
                skip_evidence = dict(state_decision.evidence)
                if slurm_state_sync is not None:
                    skip_evidence["slurm_state_sync"] = slurm_state_sync
                skipped.append(
                    {
                        **candidate.to_dict(),
                        "reason": state_decision.reason,
                        "state_evidence": _evidence_safe(skip_evidence),
                    }
                )
                continue
            if (
                context.active_repository is not None
                and context.active_repository.has_active_pipeline(
                    source_id=discovery.source_id,
                    cycle_time=discovery.cycle_time,
                    model_id=model.model_id,
                )
                and not context.candidate_state_scoped_retry_detector(state_decision)
            ):
                skipped.append({**candidate.to_dict(), "reason": "active_duplicate_pipeline"})
                continue
            candidates.append(candidate)
    # Issue #1081 §8.6: emit predecessor-select candidates for every §8
    # ``block_predecessor_pending`` block AFTER the main loop so the deep
    # candidate-factory / decision plumbing stays untouched.  The helper
    # runs the same strict-warm-start gate on each emitted candidate so a
    # predecessor whose own §8 gate blocks lands in ``blocked`` with its
    # own typed reason rather than silently masquerading as admittable.
    #
    # R2-B3 (round-2 review): pass ``max_candidates`` so an admitted
    # predecessor prepend cannot bypass the fail-closed governance cap.
    # R2-C3: hand the active_repository down so §8.6 can skip predecessor
    # cycles whose prior pipeline is still in-flight.
    # R2-C4: bind the emission summary + attach the summary to the affected
    # successor blocked entries so operators can audit
    # ``emitted / blocked / skipped / truncated`` counts without needing a
    # new tuple slot on the caller-facing shape.  The natural home is the
    # successor block's ``state_evidence`` — that block is the reason the
    # emitter fired in the first place.
    from services.orchestrator import scheduler_backfill_predecessor as _bf
    predecessor_emission_evidence = _bf.emit_predecessor_candidates(
        models=models, cycles=cycles, candidates=candidates, blocked=blocked,
        candidate_factory=context.candidate_factory,
        strict_warm_start_for_candidate=context.strict_warm_start_for_candidate,
        blocked_candidate_factory=_blocked_candidate,
        active_repository=context.active_repository,
        max_candidates=max_candidates,
        # R3-C-1: hand the main-loop ``skipped`` list down so §8.6's pre-admit
        # cap projection matches ``candidates + blocked + skipped`` at line
        # 185; otherwise admitted predecessors can silently breach the cap.
        skipped=skipped,
    )
    if predecessor_emission_evidence:
        _bf.attach_emission_summary_to_blocked(
            blocked, predecessor_emission_evidence
        )
    return candidates, blocked, skipped, duplicate_exclusions, slurm_status_sync_evidence


def _source_blocked_evidence(candidate: SchedulerCandidateLike, discovery: CycleDiscovery) -> dict[str, Any]:
    retryable = True if discovery.retryable is None else bool(discovery.retryable)
    reason = discovery.reason or "source_cycle_unavailable"
    classifier = discovery.classifier or (
        "source_unavailable" if reason == "source_cycle_unavailable" else discovery.status or "unavailable"
    )
    evidence = {
        "decision": "blocked_retryable" if retryable else "blocked_permanent",
        "reason": reason,
        "failure": {
            "classifier": classifier,
            "status": discovery.status or "unavailable",
            "reason_code": _reason_code(reason),
            "retryable": retryable,
            "permanent": not retryable,
            "attempt": 0,
            "retry_limit": None,
        },
        "retry_policy": {
            "automatic_retry_allowed": retryable,
            "enum_safe_storage": "scheduler_evidence",
            "unsupported_db_enum_written": False,
        },
        "storage": {
            "met_forecast_cycle_status_written": None,
            "ops_pipeline_event_details": True,
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
            "forcing_version_id": candidate.forcing_version_id,
            "source_id": discovery.source_id,
            "cycle_id": discovery.cycle_id,
            "cycle_time_utc": _format_utc(discovery.cycle_time),
            "cycle_hour": discovery.cycle_hour,
            "probe_uri": _source_secret_text_safe(discovery.probe_uri) if discovery.probe_uri is not None else None,
        },
    }
    if discovery.evidence:
        evidence["source_discovery"] = _source_discovery_evidence_safe(discovery.evidence)
    return _evidence_safe(evidence)


def _direct_grid_model_source_is_out_of_scope(
    model: SchedulerModelLike,
    discovery: CycleDiscovery,
) -> bool:
    """Exclude an expected source-scoped variant mismatch before candidate creation.

    A direct-grid registry contains one model variant per basin and forcing
    source.  Pairing every variant with every discovered source would create a
    Cartesian product in which half the rows are intentionally inapplicable.
    Those rows are not failed work and therefore must not become candidates.

    Only the parser's precise source-scope mismatch is excluded here.  Missing
    or malformed contracts continue into ``_direct_grid_source_scope_block``
    below so contract corruption still fails closed with evidence.
    """

    resource_profile = getattr(model, "resource_profile", None)
    direct_grid_section = (
        resource_profile.get("direct_grid_forcing")
        if isinstance(resource_profile, Mapping)
        else None
    )
    if direct_grid_section is None:
        return False
    manifest = {
        "forcing_mapping_mode": "direct_grid",
        "direct_grid_forcing": direct_grid_section,
    }
    try:
        load_forcing_mapping_contract_from_manifest(
            manifest,
            source_id=discovery.source_id,
        )
    except DirectGridContractError as error:
        return str(error) == _DIRECT_GRID_SOURCE_SCOPE_ERROR_MESSAGE
    return False


def _direct_grid_source_scope_block(
    candidate: SchedulerCandidateLike,
    discovery: CycleDiscovery,
) -> SchedulerCandidateLike | None:
    """Fail-closed source-scope precondition (Epic #961, §4.1, INV-4/INV-5).

    Legacy IDW candidates (no ``resource_profile.direct_grid_forcing``) are
    passed through unchanged. When a direct-grid contract is present, invoke
    the shared parser with the requested ``source_id``: the parser's own
    membership check raises when the source is outside
    ``applicable_source_ids``. That out-of-scope error blocks the candidate
    with a distinct reason code; any other parser failure indicates a
    malformed contract row and is blocked under a separate reason code.
    The candidate never reaches ``forcing_producer.produce`` (INV-5).
    """

    resource_profile = candidate.resource_profile
    direct_grid_section = (
        resource_profile.get("direct_grid_forcing")
        if isinstance(resource_profile, Mapping)
        else None
    )
    if direct_grid_section is None:
        return None
    manifest = {
        "forcing_mapping_mode": "direct_grid",
        "direct_grid_forcing": direct_grid_section,
    }
    try:
        load_forcing_mapping_contract_from_manifest(manifest, source_id=discovery.source_id)
    except DirectGridContractError as error:
        if str(error) == _DIRECT_GRID_SOURCE_SCOPE_ERROR_MESSAGE:
            return _blocked_candidate(
                candidate,
                _DIRECT_GRID_SOURCE_OUT_OF_SCOPE_REASON,
                state_evidence=_direct_grid_source_out_of_scope_evidence(discovery, error),
            )
        return _blocked_candidate(
            candidate,
            _DIRECT_GRID_CONTRACT_INVALID_REASON,
            state_evidence=_direct_grid_contract_invalid_evidence(discovery, error),
        )
    return None


def _direct_grid_source_out_of_scope_evidence(
    discovery: CycleDiscovery,
    error: DirectGridContractError,
) -> dict[str, Any]:
    payload = error.to_dict()
    applicable = payload.get("applicable_source_ids") or ()
    return {
        "direct_grid_source_scope": {
            "code": _DIRECT_GRID_SOURCE_OUT_OF_SCOPE_REASON,
            "requested_source_id": discovery.source_id,
            "normalized_source_id": error.source_id,
            "applicable_source_ids": list(applicable),
            "message": str(error),
        }
    }


def _direct_grid_contract_invalid_evidence(
    discovery: CycleDiscovery,
    error: DirectGridContractError,
) -> dict[str, Any]:
    return {
        "direct_grid_contract": {
            "code": _DIRECT_GRID_CONTRACT_INVALID_REASON,
            "requested_source_id": discovery.source_id,
            "field": error.field,
            "message": str(error),
            "error": error.to_dict(),
        }
    }


def _reason_code(reason: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", reason.upper()).strip("_") or "UNKNOWN"


def _canonical_readiness_unavailable_evidence(
    discovery: CycleDiscovery,
    candidate: SchedulerCandidateLike,
    *,
    forecast_hours: Sequence[int],
    policy_identity: Mapping[str, Any],
    source_object_identity: Mapping[str, Any],
    reason: str,
    dependency: str,
    retryable: bool,
    error: Exception | None = None,
) -> dict[str, Any]:
    failure: dict[str, Any] = {
        "classifier": "dependency_unavailable" if "absent" in reason or "unavailable" in reason else "query_failed",
        "reason_code": _reason_code(reason),
        "dependency": dependency,
        "retryable": retryable,
        "permanent": not retryable,
    }
    if error is not None:
        failure["error_type"] = type(error).__name__
        failure["error_message_redacted"] = True
    return _evidence_safe(
        {
            "source": discovery.source_id,
            "source_id": discovery.source_id,
            "cycle_id": discovery.cycle_id,
            "cycle_time": _format_utc(discovery.cycle_time),
            "status": "canonical_unavailable",
            "ready": False,
            "reason": reason,
            "canonical_product_id": _candidate_canonical_product_id(candidate),
            "model_id": candidate.model_id,
            "basin_id": candidate.basin_id,
            "expected_leads": list(forecast_hours),
            "accepted_horizon": _accepted_horizon_from_hours(forecast_hours),
            "policy_identity": dict(policy_identity),
            "source_object_identity": dict(source_object_identity),
            "policy_identity_matched": False,
            "source_object_identity_matched": False,
            "dependency": {
                "name": dependency,
                "status": "unavailable",
                "retryable": retryable,
            },
            "failure": failure,
        }
    )


def _canonical_candidate_row_count(canonical_readiness: Mapping[str, Any] | None) -> int:
    """Count canonical rows already present for this cycle."""

    if not isinstance(canonical_readiness, Mapping):
        return 0
    try:
        return int(canonical_readiness.get("candidate_row_count") or 0)
    except (TypeError, ValueError):
        return 0


def _canonical_evidence_is_fresh_zero_row(canonical_readiness: Mapping[str, Any] | None) -> bool:
    """True only for a genuine readiness evaluation that found zero canonical rows."""

    if not isinstance(canonical_readiness, Mapping):
        return False
    if "candidate_row_count" not in canonical_readiness:
        return False
    if str(canonical_readiness.get("status") or "") == "canonical_unavailable":
        return False
    if str(canonical_readiness.get("reason") or "") == "no_expected_leads":
        return False
    if not canonical_readiness.get("expected_leads"):
        return False
    return _canonical_candidate_row_count(canonical_readiness) == 0


def _candidate_is_fresh_full_chain(candidate: SchedulerCandidateLike) -> bool:
    state_evidence = candidate.state_evidence
    if not isinstance(state_evidence, Mapping):
        return False
    marker = state_evidence.get("fresh_ingestion")
    if not isinstance(marker, Mapping):
        return False
    return bool(marker.get("required")) and str(marker.get("mode") or "") == "full_chain"


def _nfs_raw_manifest_gate(raw_state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw_state, Mapping):
        return None
    evidence = raw_state.get("nfs_raw_manifest")
    if not isinstance(evidence, Mapping):
        return None
    return _evidence_safe(dict(evidence))


def _nfs_raw_manifest_block_reason(evidence: Mapping[str, Any]) -> str:
    reason = str(evidence.get("reason") or evidence.get("status") or "unavailable")
    return reason if reason.startswith("nfs_raw_manifest_") else f"nfs_raw_manifest_{reason}"


def _is_explicit_missing_forcing_repair_target(
    config: SchedulerConfigLike,
    cycle_time: datetime,
) -> bool:
    if not bool(getattr(config, "repair_missing_forcing", False)):
        return False
    target_cycle = getattr(config, "repair_missing_forcing_cycle_time", None)
    return isinstance(target_cycle, datetime) and _format_utc(target_cycle) == _format_utc(
        cycle_time
    )


def _missing_forcing_repair_policy_evidence(
    config: SchedulerConfigLike,
    candidate: SchedulerCandidateLike,
) -> dict[str, Any]:
    target_cycle = getattr(config, "repair_missing_forcing_cycle_time", None)
    return {
        "policy": "operator_exact_cycle_missing_forcing_repair",
        "requested": True,
        "target_cycle_time": _format_utc(target_cycle) if isinstance(target_cycle, datetime) else None,
        "candidate_cycle_time": _format_utc(candidate.cycle_time_utc),
        "source_id": candidate.source_id,
        "model_id": candidate.model_id,
        "candidate_id": candidate.candidate_id,
        "default_policy": "fail_closed",
        "plan_only": bool(config.dry_run),
    }


def _missing_forcing_repair_rejected_decision(
    config: SchedulerConfigLike,
    candidate: SchedulerCandidateLike,
    decision: CandidateStateDecision,
    reason: str,
    **details: Any,
) -> CandidateStateDecision:
    original_evidence = dict(decision.evidence)
    original_raw_manifest = original_evidence.get("nfs_raw_manifest")
    if isinstance(original_raw_manifest, Mapping):
        original_evidence["nfs_raw_manifest"] = _public_raw_manifest_evidence(
            original_raw_manifest
        )
    return CandidateStateDecision(
        "blocked",
        decision.reason,
        {
            **original_evidence,
            "missing_forcing_repair": _evidence_safe(
                {
                    **_missing_forcing_repair_policy_evidence(config, candidate),
                    "status": "rejected",
                    "reason": reason,
                    **details,
                }
            ),
        },
    )


def _repair_precondition_blocker(
    config: SchedulerConfigLike,
    candidate: SchedulerCandidateLike,
    decision: CandidateStateDecision | None,
    ordinary_blocker: SchedulerCandidateLike | None,
    *,
    precondition: str,
) -> SchedulerCandidateLike | None:
    if (
        not _is_explicit_missing_forcing_repair_target(config, candidate.cycle_time_utc)
        or decision is None
        or decision.action != "blocked"
        or decision.reason != "missing_forcing_package_uri"
        or ordinary_blocker is None
    ):
        return None
    reason = ordinary_blocker.reason or "repair_precondition_blocked"
    rejection = _missing_forcing_repair_rejected_decision(
        config,
        candidate,
        decision,
        reason,
        detail={
            "precondition": precondition,
            "blocker": {
                "reason": reason,
                **dict(ordinary_blocker.state_evidence),
            },
        },
    )
    return _blocked_candidate(
        candidate,
        rejection.reason or "missing_forcing_package_uri",
        state_evidence=rejection.evidence,
    )


def _apply_explicit_missing_forcing_repair_policy(
    config: SchedulerConfigLike,
    candidate: SchedulerCandidateLike,
    raw_state: Mapping[str, Any] | None,
    decision: CandidateStateDecision | None,
    *,
    strict_warm_start: Mapping[str, Any] | None,
) -> CandidateStateDecision | None:
    """Authorize one exact-cycle forcing rebuild without weakening the default guard.

    The policy is deliberately evaluated after the normal candidate-state
    decision.  It can only reclassify the stable missing-forcing blocker; every
    other blocker and retry decision remains owned by the normal state machine.
    """

    if not bool(getattr(config, "repair_missing_forcing", False)):
        return decision
    if (
        decision is None
        or decision.action != "blocked"
        or decision.reason != "missing_forcing_package_uri"
    ):
        return decision

    target_cycle = getattr(config, "repair_missing_forcing_cycle_time", None)
    policy_evidence = _missing_forcing_repair_policy_evidence(config, candidate)

    def rejected(reason: str, **details: Any) -> CandidateStateDecision:
        return _missing_forcing_repair_rejected_decision(
            config,
            candidate,
            decision,
            reason,
            **details,
        )

    if not isinstance(target_cycle, datetime) or _format_utc(target_cycle) != _format_utc(
        candidate.cycle_time_utc
    ):
        return rejected("exact_cycle_identity_mismatch")
    if not bool(getattr(config, "require_direct_grid", False)):
        return rejected("production_direct_grid_not_required")

    resource_profile = candidate.resource_profile
    direct_grid_section = (
        resource_profile.get("direct_grid_forcing")
        if isinstance(resource_profile, Mapping)
        else None
    )
    if (
        not isinstance(resource_profile, Mapping)
        or resource_profile.get("forcing_mapping_mode") != "direct_grid"
        or not isinstance(direct_grid_section, Mapping)
    ):
        return rejected("candidate_not_direct_grid")
    try:
        load_forcing_mapping_contract_from_manifest(
            {
                "forcing_mapping_mode": "direct_grid",
                "direct_grid_forcing": direct_grid_section,
            },
            source_id=candidate.source_id,
        )
    except DirectGridContractError as error:
        return rejected("direct_grid_contract_invalid", direct_grid_error=error.to_dict())

    artifact_guard = decision.evidence.get("artifact_guard")
    if not isinstance(artifact_guard, Mapping):
        return rejected("missing_forcing_blocker_contract_invalid")
    if (
        artifact_guard.get("artifact_type") != "forcing_package_uri"
        or artifact_guard.get("stable_classifier") != "FORCING_PACKAGE_URI_MISSING"
        or artifact_guard.get("artifact_exists") is not False
        or decision.evidence.get("classifier") != "missing_upstream_artifact"
        or str(decision.evidence.get("restart_stage") or "") != "forecast"
    ):
        return rejected("missing_forcing_blocker_contract_invalid")
    if artifact_guard.get("unsafe_reason") not in (None, ""):
        return rejected(
            "forcing_artifact_reference_unsafe",
            unsafe_reason=artifact_guard.get("unsafe_reason"),
        )

    warm_state, warm_rejection = _verified_repair_warm_state(candidate, strict_warm_start)
    if warm_state is None:
        return rejected(warm_rejection or "warm_state_missing")

    raw_manifest, raw_rejection = _verified_repair_raw_manifest(config, candidate, raw_state)
    if raw_manifest is None:
        return rejected(raw_rejection or "raw_manifest_not_ready")

    repair_evidence: dict[str, Any] = {
        **dict(decision.evidence),
        "decision": "retry_repair_missing_forcing",
        "reason": "operator_repair_missing_forcing",
        "classifier": "operator_authorized_missing_forcing_repair",
        "restart_stage": "forcing",
        "restart_from_stage": "forcing",
        "native_shud_resubmitted": True,
        "replacement_submitted": False,
        "durable_shud_output_reused": False,
        "cold_fallback_allowed": False,
        "initial_state_selection": "preserved",
        "nfs_raw_manifest": raw_manifest,
        "fresh_ingestion": {"required": False, "mode": "repair_missing_forcing"},
        "raw_manifest_reuse": {
            "status": "ready",
            "source": NFS_RAW_MANIFEST_READY_SOURCE,
            "manifest_uri": raw_manifest.get("manifest_uri"),
        },
        "missing_forcing_repair": {
            **policy_evidence,
            "status": "authorized",
            "reason": "exact_cycle_direct_grid_raw_manifest_ready",
            "restart_stage": "forcing",
            "slurm_stage": "produce_forcing_array",
            "login_node_forcing": False,
        },
        "retry_policy": {
            **dict(decision.evidence.get("retry_policy") or {}),
            "automatic_retry_allowed": False,
            "operator_exact_cycle_repair_authorized": True,
            "cold_fallback_allowed": False,
        },
    }
    repair_evidence["candidate_state"] = _evidence_safe(dict(warm_state))
    repair_evidence["strict_warm_start"] = _evidence_safe(dict(strict_warm_start or {}))
    return CandidateStateDecision(
        "retry",
        "operator_repair_missing_forcing",
        _evidence_safe(repair_evidence),
    )


def _decision_is_authorized_missing_forcing_repair(
    decision: CandidateStateDecision | None,
) -> bool:
    if decision is None or decision.action != "retry":
        return False
    repair = decision.evidence.get("missing_forcing_repair")
    return (
        isinstance(repair, Mapping)
        and repair.get("status") == "authorized"
        and decision.evidence.get("restart_stage") == "forcing"
    )


def _verified_repair_raw_manifest(
    config: SchedulerConfigLike,
    candidate: SchedulerCandidateLike,
    raw_state: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    recorded = _nfs_raw_manifest_gate(raw_state)
    if recorded is None or recorded.get("status") != "ready" or recorded.get("required") is not True:
        return None, "raw_manifest_not_ready"
    if str(recorded.get("source") or "") != NFS_RAW_MANIFEST_READY_SOURCE:
        return None, "raw_manifest_authority_mismatch"
    if not _nfs_raw_manifest_matches_source_cycle(candidate, recorded):
        return None, "raw_manifest_identity_mismatch"
    root = getattr(config, "nfs_raw_manifest_root", None)
    if root in (None, ""):
        return None, "raw_manifest_presence_unverified"
    try:
        actual = nfs_raw_manifest_readiness(
            source_id=candidate.source_id,
            cycle_time=candidate.cycle_time_utc,
            object_store_root=str(root),
            object_store_prefix=str(getattr(config, "nfs_raw_manifest_prefix", "s3://nhms")),
            required=True,
        )
    except (OSError, ValueError):
        return None, "raw_manifest_presence_unverified"
    if actual.get("status") != "ready":
        return None, "raw_manifest_not_ready"
    if not _nfs_raw_manifest_matches_source_cycle(candidate, actual):
        return None, "raw_manifest_identity_mismatch"
    for key in ("manifest_key",):
        recorded_value = recorded.get(key)
        actual_value = actual.get(key)
        if recorded_value not in (None, "") and str(recorded_value) != str(actual_value):
            return None, "raw_manifest_identity_mismatch"
    return _public_raw_manifest_evidence(actual), None


def _verified_repair_warm_state(
    candidate: SchedulerCandidateLike,
    strict_warm_start: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(strict_warm_start, Mapping):
        return None, "warm_state_missing"
    if strict_warm_start.get("ready") is not True:
        return None, "warm_state_not_ready"
    selected = strict_warm_start.get("candidate_state")
    if not isinstance(selected, Mapping):
        return None, "warm_state_missing"
    required_fields = ("state_id", "uri", "checksum", "valid_time")
    if any(_state_field(selected, field) in (None, "") for field in required_fields):
        return None, "warm_state_identity_incomplete"
    try:
        valid_time = _ensure_utc(
            datetime.fromisoformat(str(_state_field(selected, "valid_time")).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return None, "warm_state_identity_incomplete"
    if _format_utc(valid_time) != _format_utc(candidate.cycle_time_utc):
        return None, "warm_state_valid_time_mismatch"
    lineage = selected.get("init_state_lineage") or selected.get("lineage")
    quality = str(selected.get("init_state_quality") or selected.get("quality") or "").lower()
    start_mode = str(lineage.get("start_mode") or "").lower() if isinstance(lineage, Mapping) else ""
    if not isinstance(lineage, Mapping) or not lineage:
        return None, "warm_state_identity_incomplete"
    if "cold" in quality or (start_mode and not start_mode.startswith("warm")):
        return None, "warm_state_not_warm"
    return dict(selected), None


def _production_raw_manifest_missing_evidence(raw_state: Mapping[str, Any] | None) -> dict[str, Any]:
    evidence = _nfs_raw_manifest_gate(raw_state)
    if evidence is not None:
        return {"nfs_raw_manifest": evidence}
    return {
        "nfs_raw_manifest": {
            "status": "missing",
            "ready": False,
            "required": True,
            "source": "node27_nfs_raw_manifest",
            "reason": "production_download_retired",
        }
    }


def _source_raw_manifest_restart_evidence(
    candidate: SchedulerCandidateLike,
    raw_state: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(raw_state, Mapping):
        return None
    nfs_evidence = _nfs_raw_manifest_gate(raw_state)
    manifest_uri = None
    source = "forecast_cycle"
    payload: dict[str, Any] = {}
    if nfs_evidence is not None and nfs_evidence.get("status") == "ready":
        if not _nfs_raw_manifest_matches_source_cycle(candidate, nfs_evidence):
            return None
        manifest_uri = nfs_evidence.get("manifest_uri")
        source = str(nfs_evidence.get("source") or "nfs_raw_manifest")
        payload["nfs_raw_manifest"] = nfs_evidence
    if manifest_uri in (None, ""):
        forecast_cycle = raw_state.get("forecast_cycle")
        if not isinstance(forecast_cycle, Mapping):
            return None
        status = str(forecast_cycle.get("status") or "")
        manifest_uri = forecast_cycle.get("manifest_uri")
        if status not in RAW_MANIFEST_READY_CYCLE_STATUSES or manifest_uri in (None, ""):
            return None
        if not _raw_manifest_uri_matches_source_cycle(
            str(manifest_uri),
            source_id=candidate.source_id,
            cycle_time=candidate.cycle_time_utc,
        ):
            return None
        payload["forecast_cycle"] = _evidence_safe(dict(forecast_cycle))
    return {
        **payload,
        "restart_stage": "convert",
        "restart_from_stage": "convert",
        "restart_reason": "raw_manifest_ready_without_canonical",
        "fresh_ingestion": {"required": False, "mode": "reuse_raw_then_convert"},
        "raw_manifest_reuse": {
            "status": "ready",
            "source": source,
            "manifest_uri": str(manifest_uri),
        },
    }


def _nfs_raw_manifest_matches_source_cycle(
    candidate: SchedulerCandidateLike,
    evidence: Mapping[str, Any],
) -> bool:
    manifest_uri = evidence.get("manifest_uri")
    if manifest_uri in (None, ""):
        return False
    manifest_text = str(manifest_uri)
    redacted_manifest_uri = manifest_text == "[object-uri]"
    if redacted_manifest_uri:
        for field_name in ("source_id", "cycle_id", "cycle_time"):
            if evidence.get(field_name) in (None, ""):
                return False
    elif not _raw_manifest_uri_matches_source_cycle(
        manifest_text,
        source_id=candidate.source_id,
        cycle_time=candidate.cycle_time_utc,
    ):
        return False
    source_id = evidence.get("source_id")
    if source_id not in (None, ""):
        try:
            if normalize_source_id(str(source_id)) != normalize_source_id(candidate.source_id):
                return False
        except ValueError:
            return False
    cycle_id = evidence.get("cycle_id")
    if cycle_id not in (None, "") and str(cycle_id) != str(candidate.cycle_id):
        return False
    cycle_time = evidence.get("cycle_time")
    if cycle_time not in (None, ""):
        try:
            parsed_cycle_time = _ensure_utc(
                cycle_time
                if isinstance(cycle_time, datetime)
                else datetime.fromisoformat(str(cycle_time).replace("Z", "+00:00"))
            )
        except (TypeError, ValueError):
            return False
        if _format_utc(parsed_cycle_time) != _format_utc(candidate.cycle_time_utc):
            return False
    return True


def _blocked_candidate(
    candidate: SchedulerCandidateLike,
    reason: str,
    *,
    state_evidence: Mapping[str, Any] | None = None,
) -> SchedulerCandidateLike:
    evidence = _merge_state_evidence(candidate.state_evidence, state_evidence)
    return replace(candidate, status="blocked", reason=reason, state_evidence=evidence)


def _candidate_with_state_evidence(
    candidate: SchedulerCandidateLike,
    state_evidence: Mapping[str, Any],
) -> SchedulerCandidateLike:
    return replace(
        candidate,
        state_evidence=_merge_state_evidence(candidate.state_evidence, state_evidence),
    )


def _merge_state_evidence(
    existing: Mapping[str, Any] | None,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(existing or {})
    for key, value in dict(extra or {}).items():
        if key in merged and isinstance(merged[key], Mapping) and isinstance(value, Mapping):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return _evidence_safe(merged)


def _terminal_decision_matches_strict_warm_start(
    terminal_evidence: Mapping[str, Any],
    strict_evidence: Mapping[str, Any],
) -> bool:
    selected = strict_evidence.get("candidate_state")
    if not isinstance(selected, Mapping) or _state_field(selected, "state_id") in (None, ""):
        return False
    terminal_candidate_state = terminal_evidence.get("candidate_state")
    if (
        terminal_evidence.get("terminal_source") == "pipeline_job"
        and isinstance(terminal_candidate_state, Mapping)
        and _warm_state_record_matches(selected, terminal_candidate_state)
    ):
        return True
    hydro_run = terminal_evidence.get("hydro_run")
    if not isinstance(hydro_run, Mapping):
        return False
    terminal_init_state_id = _state_field(hydro_run, "state_id")
    if (
        terminal_evidence.get("terminal_source") == "pipeline_job"
        and terminal_evidence.get("terminal_status") == "succeeded"
        and terminal_init_state_id in (None, "")
        and hydro_run.get("status") == "failed"
        and hydro_run.get("error_code") == "COLD_START_QUARANTINED"
    ):
        return True
    return _warm_state_record_matches(selected, hydro_run)


def _terminal_decision_has_run_manifest(terminal_evidence: Mapping[str, Any]) -> bool:
    run_manifest_initial_state = terminal_evidence.get("run_manifest_initial_state")
    return isinstance(run_manifest_initial_state, Mapping)


def _terminal_decision_run_manifest_matches_strict_warm_start(
    terminal_evidence: Mapping[str, Any],
    strict_evidence: Mapping[str, Any],
) -> bool:
    selected = strict_evidence.get("candidate_state")
    if not isinstance(selected, Mapping) or _state_field(selected, "state_id") in (None, ""):
        return False
    run_manifest_initial_state = terminal_evidence.get("run_manifest_initial_state")
    if not isinstance(run_manifest_initial_state, Mapping):
        return False
    return _warm_state_record_matches(selected, run_manifest_initial_state)


def _state_field(record: Mapping[str, Any], field: str) -> Any:
    aliases = {
        "state_id": ("init_state_id", "initial_state_id", "state_id"),
        "checksum": ("init_state_checksum", "initial_state_checksum", "checksum"),
        "uri": ("init_state_uri", "initial_state_uri", "ic_file_uri", "state_uri"),
        "valid_time": ("init_state_valid_time", "initial_state_valid_time", "valid_time"),
    }
    for key in aliases[field]:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _warm_state_record_matches(selected: Mapping[str, Any], observed: Mapping[str, Any]) -> bool:
    """Require every selected warm-state identity field to match the observed run.

    A repaired checkpoint intentionally retains its deterministic ``state_id`` while
    its checksum changes. Comparing the ID alone would therefore let a terminal run
    produced from the corrupt object masquerade as current and skip the required
    forecast replay.
    """

    for field in ("state_id", "checksum", "uri", "valid_time"):
        expected = _state_field(selected, field)
        if expected in (None, ""):
            continue
        actual = _state_field(observed, field)
        if str(actual or "") != str(expected):
            return False
    return True


def _upgrade_retry_for_strict_warm_start_manifest(
    state_decision: CandidateStateDecision | None,
    strict_evidence: Mapping[str, Any] | None,
) -> CandidateStateDecision | None:
    if state_decision is None or state_decision.action != "retry" or strict_evidence is None:
        return state_decision
    forcing_repair = state_decision.evidence.get("missing_forcing_repair")
    if (
        isinstance(forcing_repair, Mapping)
        and forcing_repair.get("status") == "authorized"
        and state_decision.evidence.get("restart_stage") == "forcing"
    ):
        return state_decision
    if (
        state_decision.evidence.get("native_shud_resubmitted") is True
        and state_decision.evidence.get("restart_stage") == "forecast"
    ):
        return state_decision
    if _terminal_decision_run_manifest_matches_strict_warm_start(
        state_decision.evidence,
        strict_evidence,
    ):
        return state_decision
    return CandidateStateDecision(
        "retry",
        "strict_warm_start_retry_run_manifest_mismatch",
        _strict_warm_start_retry_run_manifest_evidence(
            state_decision.evidence,
            strict_evidence,
        ),
    )


def _terminal_run_manifest_retry_evidence(
    terminal_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return _evidence_safe(
        {
            **dict(terminal_evidence),
            "decision": "retry_terminal_run_manifest_missing",
            "reason": "terminal_run_manifest_missing",
            "restart_stage": "forecast",
            "restart_from_stage": "forecast",
            "native_shud_resubmitted": True,
            "durable_output_reused": False,
        }
    )


def _strict_warm_start_terminal_retry_evidence(
    terminal_evidence: Mapping[str, Any],
    strict_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        **dict(terminal_evidence),
        "decision": "retry_strict_warm_start_terminal_init_state_mismatch",
        "reason": "strict_warm_start_terminal_init_state_mismatch",
        "restart_stage": "forecast",
        "restart_from_stage": "forecast",
        "strict_warm_start": _evidence_safe(dict(strict_evidence)),
        "native_shud_resubmitted": True,
        "durable_output_reused": False,
    }
    selected = strict_evidence.get("candidate_state")
    if isinstance(selected, Mapping):
        payload["candidate_state"] = _evidence_safe(dict(selected))
    index = strict_evidence.get("state_snapshot_index")
    if isinstance(index, Mapping):
        payload["state_snapshot_index"] = _evidence_safe(dict(index))
    return _evidence_safe(payload)


def _strict_warm_start_run_manifest_retry_evidence(
    terminal_evidence: Mapping[str, Any],
    strict_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        **dict(terminal_evidence),
        "decision": "retry_strict_warm_start_terminal_run_manifest_missing",
        "reason": "strict_warm_start_terminal_run_manifest_missing",
        "restart_stage": "forecast",
        "restart_from_stage": "forecast",
        "strict_warm_start": _evidence_safe(dict(strict_evidence)),
        "native_shud_resubmitted": True,
        "durable_output_reused": False,
    }
    selected = strict_evidence.get("candidate_state")
    if isinstance(selected, Mapping):
        payload["candidate_state"] = _evidence_safe(dict(selected))
    return _evidence_safe(payload)


def _strict_warm_start_retry_run_manifest_evidence(
    retry_evidence: Mapping[str, Any],
    strict_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        **dict(retry_evidence),
        "decision": "retry_strict_warm_start_retry_run_manifest_mismatch",
        "reason": "strict_warm_start_retry_run_manifest_mismatch",
        "restart_stage": "forecast",
        "restart_from_stage": "forecast",
        "strict_warm_start": _evidence_safe(dict(strict_evidence)),
        "native_shud_resubmitted": True,
        "durable_output_reused": False,
        "durable_shud_output_reused": False,
    }
    selected = strict_evidence.get("candidate_state")
    if isinstance(selected, Mapping):
        payload["candidate_state"] = _evidence_safe(dict(selected))
    return _evidence_safe(payload)


def _successor_state_terminal_can_skip(successor_state: Mapping[str, Any] | None) -> bool:
    return successor_state is None or bool(successor_state.get("ready"))


def _strict_warm_start_successor_retry_evidence(
    terminal_evidence: Mapping[str, Any],
    successor_state: Mapping[str, Any],
) -> dict[str, Any]:
    # A terminal forecast with the exact warm-start manifest already produced the
    # deterministic SHUD output needed by state-save/QC.  Re-running forecast
    # cannot repair a missing or rejected successor checkpoint; resume at the
    # checkpoint boundary and let the downstream artifact guards reject the
    # retry if that durable output is no longer available.
    return _evidence_safe(
        {
            **dict(terminal_evidence),
            "decision": "retry_strict_warm_start_successor_checkpoint_missing",
            "reason": "strict_warm_start_successor_checkpoint_missing",
            "restart_stage": "state_save_qc",
            "restart_from_stage": "state_save_qc",
            "successor_state": _evidence_safe(dict(successor_state)),
            "native_shud_resubmitted": False,
            "durable_shud_output_reused": True,
            "durable_output_reused": True,
            "force_native_shud_rerun": False,
        }
    )


def _slurm_status_sync_failed_evidence(
    candidate: SchedulerCandidateLike,
    *,
    cycle_id: str,
    active_slurm_jobs: Sequence[Mapping[str, Any]],
    error: Exception,
) -> dict[str, Any]:
    error_code = str(getattr(error, "error_code", "SLURM_STATUS_SYNC_FAILED") or "SLURM_STATUS_SYNC_FAILED")
    return {
        "cycle_id": cycle_id,
        "source_id": candidate.source_id,
        "cycle_time_utc": _format_utc(candidate.cycle_time_utc),
        "candidate_id": candidate.candidate_id,
        "model_id": candidate.model_id,
        "scenario_id": candidate.scenario_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "status": "failed",
        "sync_required": True,
        "sync_called": True,
        "sync_attempted": True,
        "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
        "error_code": error_code,
        "error_message": _evidence_safe(getattr(error, "message", str(error))),
        "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
        "residual_blockers": [
            {
                "code": error_code,
                "state": "blocked",
                "quality_flag": "slurm_status_sync_failed",
                "residual_risk": (
                    "Slurm status sync raised after the downstream sync method was called; "
                    "pipeline status/event mutation outcome is unknown."
                ),
            }
        ],
    }


def _source_forecast_hours(
    discovery: CycleDiscovery,
    adapter: Any | None,
    horizon: Mapping[str, Any],
) -> list[int]:
    config = getattr(adapter, "config", None)
    if config is not None and hasattr(config, "forecast_hours_for_cycle"):
        return list(config.forecast_hours_for_cycle(discovery.cycle_time))
    if config is not None and hasattr(config, "forecast_hours"):
        return list(config.forecast_hours())
    max_lead_hours = horizon.get("max_lead_hours")
    step_hours = horizon.get("forecast_step_hours") or 3
    start_hour = horizon.get("forecast_start_hour") or 0
    if max_lead_hours is None:
        max_lead_hours = (
            144 if normalize_source_id(discovery.source_id) == "IFS" and discovery.cycle_hour in {6, 18} else 168
        )
    return list(range(int(start_hour), int(max_lead_hours) + 1, int(step_hours)))


def _source_policy_identity(
    discovery: CycleDiscovery,
    adapter: Any | None,
    forecast_hours: Sequence[int],
) -> dict[str, Any]:
    if adapter is not None and hasattr(adapter, "source_policy_identity"):
        try:
            return dict(adapter.source_policy_identity(discovery.cycle_time, list(forecast_hours)))
        except TypeError:
            return dict(adapter.source_policy_identity(list(forecast_hours)))
    return {
        "source": discovery.source_id,
        "cycle_hour": discovery.cycle_hour,
        "forecast_hours": list(forecast_hours),
    }


def _source_object_identity(
    discovery: CycleDiscovery,
    adapter: Any | None,
    forecast_hours: Sequence[int],
) -> dict[str, Any]:
    if adapter is not None and hasattr(adapter, "source_object_identity"):
        return dict(adapter.source_object_identity(discovery.cycle_time, list(forecast_hours)))
    return {
        "source": discovery.source_id,
        "cycle_time": _format_utc(discovery.cycle_time),
        "cycle_id": discovery.cycle_id,
        "forecast_hour_count": len(forecast_hours),
    }


def _accepted_horizon_from_hours(forecast_hours: Sequence[int]) -> dict[str, Any]:
    hours = sorted(int(hour) for hour in forecast_hours)
    return {
        "first_lead_hour": min(hours) if hours else None,
        "last_lead_hour": max(hours) if hours else None,
        "lead_count": len(hours),
    }
