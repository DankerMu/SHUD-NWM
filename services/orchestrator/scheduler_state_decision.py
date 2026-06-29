from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from services.orchestrator.scheduler_state_common import _evidence_safe
from services.orchestrator.scheduler_state_evidence_owner import _candidate_state_evidence
from services.orchestrator.scheduler_state_failure import (
    _cancelled_state_evidence,
    _canonical_downstream_stage,
    _completed_upstream_stage_retry_evidence,
    _downstream_retry_evidence,
    _manual_retry_state_evidence,
    _missing_raw_manifest_repair_evidence,
    _permanent_failure_evidence,
    _repaired_raw_manifest_downstream_retry_evidence,
    _retry_failure_evidence,
    _state_has_failure_signal,
)
from services.orchestrator.scheduler_state_identity_filter import (
    _candidate_state_decision_state,
    _pipeline_terminal_success_is_candidate_scoped,
    _terminal_hydro_truth_supersedes_failure,
)
from services.orchestrator.scheduler_state_manual_retry import (
    _latest_manual_retry_blocker,
    _manual_retry_requested,
)
from services.orchestrator.scheduler_state_rows import (
    _bounded_candidate_state,
    _candidate_state_has_identity_mismatch,
    _state_active_jobs,
    _state_has_only_unsubmitted_auto_retry_placeholders,
    _state_output_uri,
    _state_status,
)
from services.orchestrator.scheduler_state_types import (
    ACTIVE_HYDRO_STATUSES,
    ACTIVE_PIPELINE_STATUSES,
    DURABLE_HYDRO_SUCCESS_STATUSES,
    FAILED_PIPELINE_STATUSES,
    TERMINAL_PIPELINE_SUCCESS_STATUSES,
    CandidateStateDecision,
    SchedulerCandidateLike,
)


def _candidate_state_decision(
    candidate: SchedulerCandidateLike,
    raw_state: Mapping[str, Any] | None,
) -> CandidateStateDecision | None:
    if raw_state is None:
        return None
    state = _bounded_candidate_state(raw_state)
    evidence = _candidate_state_evidence(candidate, state)
    if _candidate_state_has_identity_mismatch(evidence):
        return CandidateStateDecision(
            "blocked",
            "production_identity_mismatch",
            {
                **evidence,
                "decision": "blocked_identity_mismatch",
                "replacement_submitted": False,
            },
        )
    decision_state = _candidate_state_decision_state(state, evidence)
    active_jobs = _state_active_jobs(decision_state)
    if active_jobs:
        return CandidateStateDecision(
            "skip",
            "active_slurm_job",
            {
                **evidence,
                "decision": "skip_active",
                "active_slurm_jobs": active_jobs,
                "replacement_submitted": False,
            },
        )

    hydro_status = _state_status(decision_state, "hydro_status", "hydro_run_status")
    pipeline_status = _state_status(decision_state, "pipeline_status", "job_status", "status")
    if pipeline_status in ACTIVE_PIPELINE_STATUSES and _state_has_only_unsubmitted_auto_retry_placeholders(
        decision_state,
    ):
        pipeline_status = None
    if hydro_status in ACTIVE_HYDRO_STATUSES or pipeline_status in ACTIVE_PIPELINE_STATUSES:
        return CandidateStateDecision(
            "skip",
            "active_duplicate_pipeline",
            {
                **evidence,
                "decision": "skip_active",
                "active_status": hydro_status or pipeline_status,
                "replacement_submitted": False,
            },
        )
    active_truth = _latest_manual_retry_blocker(decision_state)
    if active_truth is not None and active_truth.get("active") is True:
        return CandidateStateDecision(
            "skip",
            "active_duplicate_pipeline",
            {
                **evidence,
                "decision": "skip_active",
                "active_status": active_truth.get("status"),
                "active_truth": _evidence_safe(active_truth),
                "replacement_submitted": False,
            },
        )

    if hydro_status in DURABLE_HYDRO_SUCCESS_STATUSES and _terminal_hydro_truth_supersedes_failure(decision_state):
        return CandidateStateDecision(
            "skip",
            "terminal_hydro_success",
            {
                **evidence,
                "decision": "skip_terminal",
                "terminal_source": "hydro_run",
                "terminal_status": hydro_status,
                "durable_hydro_status": hydro_status,
                "durable_output_reused": bool(_state_output_uri(decision_state)),
                "native_shud_resubmitted": False,
                "parse_resubmitted": False,
                "frequency_resubmitted": False,
                "publish_resubmitted": False,
            },
        )

    completed_stage_retry = _completed_upstream_stage_retry_evidence(candidate, decision_state, evidence)
    if completed_stage_retry is not None:
        return CandidateStateDecision("retry", "resume_after_completed_stage", completed_stage_retry)

    if pipeline_status in TERMINAL_PIPELINE_SUCCESS_STATUSES and _pipeline_terminal_success_is_candidate_scoped(
        candidate,
        decision_state,
    ):
        return CandidateStateDecision(
            "skip",
            "terminal_pipeline_success",
            {
                **evidence,
                "decision": "skip_terminal",
                "terminal_source": "pipeline_job",
                "terminal_status": pipeline_status,
                "native_shud_resubmitted": False,
            },
        )

    if _manual_retry_requested(decision_state):
        return CandidateStateDecision(
            "retry",
            "manual_retry_requested",
            _manual_retry_state_evidence(candidate, decision_state, evidence),
        )

    downstream_retry = _downstream_retry_evidence(candidate, decision_state, evidence)
    if downstream_retry is not None:
        return CandidateStateDecision("retry", "resume_downstream_after_durable_shud", downstream_retry)

    manifest_repair = _missing_raw_manifest_repair_evidence(candidate, decision_state, evidence)
    if manifest_repair is not None:
        return CandidateStateDecision("retry", "repair_missing_raw_manifest", manifest_repair)

    downstream_after_raw_repair = _repaired_raw_manifest_downstream_retry_evidence(
        candidate,
        decision_state,
        evidence,
    )
    if downstream_after_raw_repair is not None:
        return CandidateStateDecision("retry", "retry_downstream_after_raw_repair", downstream_after_raw_repair)

    permanent = _permanent_failure_evidence(candidate, decision_state, evidence)
    if permanent is not None:
        return CandidateStateDecision(
            "blocked",
            str(permanent.get("reason") or "permanent_failure_guard"),
            permanent,
        )

    cancelled = _cancelled_state_evidence(candidate, decision_state, evidence)
    if cancelled is not None:
        return CandidateStateDecision(
            "blocked",
            str(cancelled.get("reason") or "manual_retry_required_after_cancelled"),
            cancelled,
        )

    if pipeline_status in FAILED_PIPELINE_STATUSES or hydro_status == "failed" or _state_has_failure_signal(
        decision_state,
    ):
        return CandidateStateDecision(
            "retry",
            "retry_failed_candidate",
            _retry_failure_evidence(candidate, decision_state, evidence),
        )

    return None

def _candidate_repaired_state_audit_evidence(
    candidate: SchedulerCandidateLike,
    raw_state: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if raw_state is None:
        return None
    state = _bounded_candidate_state(raw_state)
    if not isinstance(state.get("repaired_stage_evidence"), Mapping) and not isinstance(
        state.get("source_cycle_repair_evidence"),
        Mapping,
    ):
        return None
    return {"candidate_state": _candidate_state_evidence(candidate, state)}

def _call_candidate_state_provider(
    provider: Callable[..., Mapping[str, Any] | None],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    run_id: str,
    forcing_version_id: str,
    candidate_id: str,
    retry_limit: int,
    job_limit: int,
    event_limit: int,
) -> Mapping[str, Any] | None:
    kwargs: dict[str, Any] = {
        "source_id": source_id,
        "cycle_time": cycle_time,
        "model_id": model_id,
        "run_id": run_id,
        "forcing_version_id": forcing_version_id,
        "candidate_id": candidate_id,
        "retry_limit": retry_limit,
        "job_limit": job_limit,
        "event_limit": event_limit,
    }
    try:
        state = provider(**kwargs)
    except TypeError as error:
        if "unexpected keyword" not in str(error):
            raise
        state = provider(
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            run_id=run_id,
            forcing_version_id=forcing_version_id,
            candidate_id=candidate_id,
        )
    if state is None:
        return None
    payload = dict(state)
    payload.setdefault("retry_limit", retry_limit)
    payload.setdefault("job_limit", job_limit)
    payload.setdefault("event_limit", event_limit)
    return _bounded_candidate_state(payload)

def _candidate_state_is_candidate_scoped_retry(decision: CandidateStateDecision | None) -> bool:
    if decision is None or decision.action != "retry":
        return False
    evidence = decision.evidence
    if not isinstance(evidence, Mapping):
        return False
    identity = evidence.get("identity")
    if not isinstance(identity, Mapping):
        identity = evidence.get("candidate_identity")
    restart_stage = _canonical_downstream_stage(
        str(evidence.get("restart_stage") or evidence.get("restart_from_stage") or "")
    )
    task_identity = evidence.get("task_identity")
    return bool(
        isinstance(identity, Mapping)
        and identity.get("candidate_id")
        and identity.get("run_id")
        and (restart_stage is not None or (isinstance(task_identity, Mapping) and bool(task_identity)))
    )

def _call_active_slurm_jobs_provider(
    provider: Callable[..., Sequence[Mapping[str, Any]]],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    limit: int,
) -> Sequence[Mapping[str, Any]]:
    try:
        return provider(source_id=source_id, cycle_time=cycle_time, model_id=model_id, limit=limit)
    except TypeError as error:
        if "unexpected keyword" not in str(error):
            raise
    return provider(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
