from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from packages.common.source_identity import normalize_source_id
from services.orchestrator import scheduler_discovery as _scheduler_discovery
from services.orchestrator.chain_source_cycle import (
    RAW_MANIFEST_READY_CYCLE_STATUSES,
    _raw_manifest_uri_matches_source_cycle,
)
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
from workers.data_adapters.base import CycleDiscovery, cycle_id_for

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
    frequency_capabilities: Mapping[str, Any]
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
            if not discovery.available:
                blocked.append(
                    _blocked_candidate(
                        candidate,
                        discovery.reason or "source_cycle_unavailable",
                        state_evidence=_source_blocked_evidence(candidate, discovery),
                    )
                )
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
            if callable(completed_provider) and completed_provider(
                source_id=discovery.source_id,
                cycle_time=discovery.cycle_time,
                model_id=model.model_id,
            ):
                skipped.append({**candidate.to_dict(), "reason": "completed_duplicate_pipeline"})
                continue
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
                skipped.append(
                    {
                        **candidate.to_dict(),
                        "reason": state_decision.reason,
                        "state_evidence": _evidence_safe(state_decision.evidence),
                    }
                )
                continue
            strict_warm_start = context.strict_warm_start_for_candidate(candidate, cycle)
            if strict_warm_start is not None and not bool(strict_warm_start.get("ready")):
                blocked.append(
                    _blocked_candidate(
                        candidate,
                        str(strict_warm_start.get("reason") or "state_snapshot_index_unavailable"),
                        state_evidence=strict_warm_start,
                    )
                )
                continue
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
