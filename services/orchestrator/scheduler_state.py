from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from packages.common.object_store import LocalObjectStore
from packages.common.redaction import redact_payload
from packages.common.slurm_env import secret_manifest_key_reason, secret_manifest_value_reason
from packages.common.source_identity import normalize_source_id
from services.orchestrator.production_contract import (
    PRODUCTION_EVIDENCE_CORRELATION_FIELDS,
    PRODUCTION_IDENTITY_FIELDS,
    ProductionContractError,
    validate_compatible_production_identity,
)
from services.orchestrator.retry import classify_failure
from workers.data_adapters.base import cycle_id_for, format_cycle_time

CANDIDATE_STATE_TASK_RESULT_LIMIT = 16
DEFAULT_RETRY_LIMIT = 3
DEFAULT_CANDIDATE_STATE_JOB_LIMIT = 100
DEFAULT_CANDIDATE_STATE_EVENT_LIMIT = 100
STATE_M23_COMPARISON_FIELDS = (
    "basin_id",
    "basin_version_id",
    "river_network_version_id",
    "canonical_product_id",
    "forcing_version_id",
    "hydro_run_id",
    "published_manifest_id",
)
STATE_CANDIDATE_SCOPED_PROOF_FIELDS = (
    "run_id",
    "forcing_version_id",
    "hydro_run_id",
    "published_manifest_id",
)
STATE_STRONG_CANDIDATE_SCOPED_PROOF_FIELDS = STATE_CANDIDATE_SCOPED_PROOF_FIELDS
ACTIVE_PIPELINE_STATUSES = {"pending", "queued", "submitted", "running"}
ACTIVE_HYDRO_STATUSES = {"created", "staged", "pending", "submitted", "running"}
DURABLE_HYDRO_SUCCESS_STATUSES = {"succeeded", "parsed", "frequency_done", "published", "complete"}
TERMINAL_PIPELINE_SUCCESS_STATUSES = {"succeeded", "complete", "published"}
FAILED_PIPELINE_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed"}
DOWNSTREAM_RESTART_STAGES = ("parse", "state_save_qc", "frequency", "publish")
DOWNSTREAM_STAGE_ALIASES = {
    "parse": "parse",
    "parse_output": "parse",
    "state_save_qc": "state_save_qc",
    "save_state_snapshot": "state_save_qc",
    "save_state_snapshot_array": "state_save_qc",
    "frequency": "frequency",
    "compute_frequency": "frequency",
    "publish": "publish",
    "publish_tiles": "publish",
}
NATIVE_SHUD_STAGE_ALIASES = {"forecast", "run_shud_forecast", "forecast_run", "analysis_run"}
TRANSIENT_RETRY_REASON_CODES = {
    "SLURM_TIMEOUT",
    "SLURM_JOB_TIMEOUT",
    "NODE_FAILURE",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "STORAGE_WRITE_FAILED",
    "SBATCH_SUBMISSION_FAILED",
    "SLURM_UNAVAILABLE",
    "SOURCE_CYCLE_UNAVAILABLE",
    "SOURCE_UNAVAILABLE",
    "ADAPTER_UNAVAILABLE",
}


class SchedulerCandidateLike(Protocol):
    candidate_id: str
    source_id: str
    cycle_id: str
    cycle_time_utc: datetime
    model_id: str
    basin_id: str
    basin_version_id: str | None
    river_network_version_id: str | None
    resource_profile: Mapping[str, Any]
    run_id: str
    forcing_version_id: str


@dataclass(frozen=True)
class CandidateStateDecision:
    action: str
    reason: str | None
    evidence: Mapping[str, Any] = field(default_factory=dict)


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


def _state_has_only_unsubmitted_auto_retry_placeholders(state: Mapping[str, Any]) -> bool:
    jobs = _state_jobs(state)
    active_jobs = [
        job
        for job in jobs
        if str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        in ACTIVE_PIPELINE_STATUSES
    ]
    return bool(active_jobs) and all(_job_is_unsubmitted_auto_retry_placeholder(job) for job in active_jobs)


def _job_is_unsubmitted_auto_retry_placeholder(job: Mapping[str, Any]) -> bool:
    status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
    if status not in {"pending", "submission_failed"}:
        return False
    if job.get("manual_retry_marker") is True:
        return False
    if job.get("slurm_job_id") not in (None, "") or job.get("array_task_id") not in (None, ""):
        return False
    retry_count = _coerce_int(job.get("retry_count"), default=0)
    if retry_count <= 0:
        return False
    job_id = str(job.get("job_id") or "")
    return "_retry_" in job_id


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


def _candidate_state_decision_state(state: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    validation = evidence.get("production_identity_validation")
    if not isinstance(validation, Mapping):
        return _candidate_state_filtered_decision_state(state, evidence)
    legacy_sources = {str(source) for source in validation.get("legacy_non_authoritative", [])}
    if not legacy_sources:
        return _candidate_state_filtered_decision_state(state, evidence)
    unresolved_source_cycle_job_ids = _inconclusive_source_cycle_unresolved_job_ids(state)
    filtered = dict(state)
    if "candidate_state" in legacy_sources:
        _strip_top_level_candidate_state_decision_fields(filtered)
        _restore_top_level_source_cycle_download_blocker(filtered, state, evidence)
    for key in ("hydro_run", "forcing_version", "forecast_cycle", "published_manifest", "canonical_product"):
        if key in legacy_sources:
            filtered.pop(key, None)
    if "hydro_run" in legacy_sources:
        _strip_top_level_hydro_decision_fields(filtered)
    for key in ("pipeline_job", "job"):
        if key in legacy_sources:
            filtered.pop(key, None)
            _strip_top_level_pipeline_decision_fields(filtered)
    jobs = _state_jobs(state)
    if jobs:
        filtered["pipeline_jobs"] = []
        for index, job in enumerate(jobs):
            if _state_row_references_job_ids(job, unresolved_source_cycle_job_ids):
                continue
            source = f"pipeline_jobs[{index}]"
            if source in legacy_sources and not _global_source_cycle_download_blocker_job(job, evidence):
                continue
            filtered["pipeline_jobs"].append(dict(job))
        filtered.pop("jobs", None)
    events = _state_events(state)
    if events:
        filtered["pipeline_events"] = [
            _candidate_state_decision_event(
                event,
                authoritative=f"pipeline_events[{index}]" not in legacy_sources,
                source=f"pipeline_events[{index}]",
                legacy_sources=legacy_sources,
            )
            for index, event in enumerate(events)
            if not _state_event_references_job_ids(event, unresolved_source_cycle_job_ids)
        ]
        filtered.pop("events", None)
    if filtered.get("pipeline_jobs") == []:
        _strip_top_level_pipeline_decision_fields(filtered)
    if filtered.get("pipeline_events") == [] and not filtered.get("pipeline_jobs"):
        _strip_top_level_pipeline_decision_fields(filtered)
    return _candidate_state_filtered_decision_state(filtered, evidence)


def _candidate_state_filtered_decision_state(state: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    filtered = _inconclusive_source_cycle_decision_state(state)
    return _repaired_stage_decision_state(_candidate_scoped_shared_cycle_aggregate_state(filtered, evidence))


def _inconclusive_source_cycle_decision_state(state: Mapping[str, Any]) -> dict[str, Any]:
    unresolved_job_ids = _inconclusive_source_cycle_unresolved_job_ids(state)
    if not unresolved_job_ids:
        return dict(state)
    filtered = dict(state)
    job_rows = _state_jobs(state)
    if job_rows:
        filtered["pipeline_jobs"] = [
            dict(job) for job in job_rows if not _state_row_references_job_ids(job, unresolved_job_ids)
        ]
        filtered.pop("jobs", None)
    single_job = state.get("pipeline_job") or state.get("job")
    if isinstance(single_job, Mapping) and _state_row_references_job_ids(single_job, unresolved_job_ids):
        filtered.pop("pipeline_job", None)
        filtered.pop("job", None)
    elif not isinstance(single_job, Mapping) and _state_row_references_job_ids(state, unresolved_job_ids):
        _strip_top_level_pipeline_decision_fields(filtered)
    event_rows = _state_events(state)
    if event_rows:
        filtered["pipeline_events"] = [
            dict(event)
            for event in event_rows
            if not _state_event_references_job_ids(event, unresolved_job_ids)
        ]
        filtered.pop("events", None)
    return filtered


def _inconclusive_source_cycle_unresolved_job_ids(state: Mapping[str, Any]) -> set[str]:
    repair_evidence = state.get("source_cycle_repair_evidence")
    if not isinstance(repair_evidence, Mapping):
        return set()
    if repair_evidence.get("status") != "inconclusive_truncated":
        return set()
    values = repair_evidence.get("unresolved_failed_job_ids")
    if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
        return set()
    return {str(value) for value in values if value not in (None, "")}


def _state_row_references_job_ids(row: Mapping[str, Any], job_ids: set[str]) -> bool:
    if not job_ids:
        return False
    for key in ("job_id", "pipeline_job_id", "entity_id", "previous_job_id", "failed_job_id"):
        value = row.get(key)
        if value not in (None, "") and str(value) in job_ids:
            return True
    return False


def _state_event_references_job_ids(event: Mapping[str, Any], job_ids: set[str]) -> bool:
    if _state_row_references_job_ids(event, job_ids):
        return True
    details = event.get("details")
    return isinstance(details, Mapping) and _state_row_references_job_ids(details, job_ids)


def _restore_top_level_source_cycle_download_blocker(
    filtered: dict[str, Any],
    state: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    candidate_identity = evidence.get("candidate_identity")
    if not isinstance(candidate_identity, Mapping):
        return
    expected = _candidate_identity_from_evidence(candidate_identity)
    if not expected or not _top_level_source_cycle_download_blocker(expected, state):
        return
    for key in (
        "pipeline_status",
        "job_status",
        "status",
        "failed_stage",
        "stage",
        "job_type",
        "error_code",
        "reason_code",
        "failure_reason",
        "last_error",
        "previous_error",
        "error_message",
        "message",
        "retry_attempt",
        "attempt",
        "retry_count",
        "retry_limit",
        "max_retries",
        "retryable",
        "permanent",
        "failure_classifier",
        "classifier",
        "shared_cycle_aggregate",
    ):
        if key in state:
            filtered[key] = state[key]


def _repaired_stage_decision_state(state: Mapping[str, Any]) -> dict[str, Any]:
    filtered = dict(state)
    if _state_has_only_repaired_pipeline_failure_signal(filtered):
        _strip_top_level_pipeline_decision_fields(filtered)
    return filtered


def _candidate_scoped_shared_cycle_aggregate_state(
    state: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    filtered = dict(state)
    if filtered.get("shared_cycle_aggregate") is not True:
        return filtered
    candidate_identity = evidence.get("candidate_identity")
    if not isinstance(candidate_identity, Mapping):
        return filtered
    expected = _candidate_identity_from_evidence(candidate_identity)
    if not expected:
        return filtered
    if _shared_cycle_aggregate_has_candidate_failure(expected, filtered):
        filtered["pipeline_events"] = _candidate_scoped_shared_cycle_events(expected, _state_events(filtered))
        filtered.pop("events", None)
        return filtered
    global_source_cycle_blockers = [
        dict(job) for job in _state_jobs(filtered) if _global_source_cycle_download_blocker_job(job, evidence)
    ]
    top_level_source_cycle_blocker = _top_level_source_cycle_download_blocker(expected, filtered)
    if global_source_cycle_blockers or top_level_source_cycle_blocker:
        retained_jobs = [
            dict(job)
            for job in _state_jobs(filtered)
            if _shared_cycle_row_is_candidate_scoped(expected, job)
            or _global_source_cycle_download_blocker_job(job, evidence)
        ]
        if not top_level_source_cycle_blocker:
            _strip_top_level_pipeline_decision_fields(filtered)
        filtered["pipeline_jobs"] = retained_jobs
        filtered.pop("jobs", None)
        filtered["pipeline_events"] = _candidate_scoped_shared_cycle_events(expected, _state_events(filtered))
        filtered.pop("events", None)
        if filtered.get("pipeline_jobs") == []:
            filtered.pop("pipeline_jobs", None)
        if filtered.get("pipeline_events") == []:
            filtered.pop("pipeline_events", None)
        return filtered
    _strip_top_level_pipeline_decision_fields(filtered)
    filtered["pipeline_jobs"] = [
        dict(job) for job in _state_jobs(filtered) if _shared_cycle_row_is_candidate_scoped(expected, job)
    ]
    filtered.pop("jobs", None)
    filtered["pipeline_events"] = _candidate_scoped_shared_cycle_events(expected, _state_events(filtered))
    filtered.pop("events", None)
    if filtered.get("pipeline_jobs") == []:
        filtered.pop("pipeline_jobs", None)
    if filtered.get("pipeline_events") == []:
        filtered.pop("pipeline_events", None)
    return filtered


def _candidate_identity_from_evidence(candidate_identity: Mapping[str, Any]) -> dict[str, Any]:
    identity = {
        "run_id": candidate_identity.get("run_id"),
        "model_id": candidate_identity.get("model_id"),
        "basin_id": candidate_identity.get("basin_id"),
        "source": candidate_identity.get("source") or candidate_identity.get("source_id"),
        "source_id": candidate_identity.get("source_id") or candidate_identity.get("source"),
        "cycle_time": candidate_identity.get("cycle_time") or candidate_identity.get("cycle_time_utc"),
        "cycle_time_utc": candidate_identity.get("cycle_time_utc") or candidate_identity.get("cycle_time"),
        "basin_version_id": candidate_identity.get("basin_version_id"),
        "river_network_version_id": candidate_identity.get("river_network_version_id"),
        "canonical_product_id": candidate_identity.get("canonical_product_id"),
        "forcing_version_id": candidate_identity.get("forcing_version_id"),
        "hydro_run_id": candidate_identity.get("hydro_run_id") or candidate_identity.get("run_id"),
        "published_manifest_id": candidate_identity.get("published_manifest_id"),
    }
    return {key: value for key, value in identity.items() if value not in (None, "")}


def _shared_cycle_aggregate_has_candidate_failure(
    expected: Mapping[str, Any],
    state: Mapping[str, Any],
) -> bool:
    for job in _state_jobs(state):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if status not in FAILED_PIPELINE_STATUSES and job.get("error_code") in (None, ""):
            continue
        if _shared_cycle_row_is_candidate_scoped(expected, job):
            return True
    for event in _state_events(state):
        if _event_has_candidate_scoped_failure(expected, event):
            return True
    return False


def _top_level_source_cycle_download_blocker(
    expected: Mapping[str, Any],
    state: Mapping[str, Any],
) -> bool:
    if state.get("shared_cycle_aggregate") is not True:
        return False
    if not _source_cycle_identity_matches_expected(expected, state):
        return False
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    if pipeline_status not in FAILED_PIPELINE_STATUSES and state.get("error_code") in (None, ""):
        return False
    stage = str(state.get("failed_stage") or state.get("stage") or "")
    if not _is_source_cycle_download_stage(stage):
        return False
    jobs = _state_jobs(state)
    if not jobs:
        return True
    return any(
        _global_source_cycle_download_blocker_job(job, {"candidate_identity": expected})
        for job in jobs
    )


def _global_source_cycle_download_blocker_job(
    job: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> bool:
    if _pipeline_job_is_repaired_stage_evidence(job):
        return False
    status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
    if status not in FAILED_PIPELINE_STATUSES and job.get("error_code") in (None, ""):
        return False
    stage = str(job.get("stage") or job.get("job_type") or "")
    if not _is_source_cycle_download_stage(stage):
        return False
    candidate_identity = evidence.get("candidate_identity")
    if not isinstance(candidate_identity, Mapping):
        return False
    expected = _candidate_identity_from_evidence(candidate_identity)
    return bool(expected) and _source_cycle_identity_matches_expected(expected, job)


def _is_source_cycle_download_stage(stage: str | None) -> bool:
    return stage in {"download", "download_source_cycle", "download_gfs"}


def _source_cycle_identity_matches_expected(
    expected: Mapping[str, Any],
    row: Mapping[str, Any],
) -> bool:
    expected_values = _legacy_identity_values(expected)
    row_values = _legacy_identity_values(row)
    source = row_values.get("source")
    expected_source = expected_values.get("source")
    if source not in (None, "") and expected_source not in (None, "") and source != expected_source:
        return False
    cycle_time = row_values.get("cycle_time")
    expected_cycle_time = expected_values.get("cycle_time")
    if cycle_time not in (None, "") and expected_cycle_time not in (None, "") and cycle_time != expected_cycle_time:
        return False
    cycle_id = str(row.get("cycle_id") or "")
    expected_source_id = str(expected.get("source_id") or expected.get("source") or "").lower()
    expected_cycle_text = str(expected.get("cycle_time") or expected.get("cycle_time_utc") or "")
    if cycle_id and expected_source_id and expected_cycle_text:
        try:
            expected_cycle_id = cycle_id_for(
                expected_source_id,
                datetime.fromisoformat(expected_cycle_text.replace("Z", "+00:00")),
            )
        except ValueError:
            expected_cycle_id = ""
        if expected_cycle_id and cycle_id.lower() != expected_cycle_id.lower():
            return False
    run_id = str(row.get("run_id") or "")
    if run_id:
        if _stage_cycle_run_matches_candidate(run_id, expected_values):
            return True
        if expected_source_id and expected_cycle_text:
            try:
                compact_cycle = format_cycle_time(expected_cycle_text)
            except (TypeError, ValueError):
                compact_cycle = ""
            cycle_run_id = f"cycle_{expected_source_id}_{compact_cycle}" if compact_cycle else ""
            if cycle_run_id and run_id != cycle_run_id:
                return False
    return any(row_values.get(key) not in (None, "") for key in ("source", "cycle_time")) or bool(cycle_id or run_id)


def _candidate_scoped_shared_cycle_events(
    expected: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    scoped_events: list[dict[str, Any]] = []
    for event in events:
        event_payload = dict(event)
        details = event_payload.get("details")
        if isinstance(details, Mapping):
            scoped_tasks = [
                dict(task)
                for task in _bounded_task_result_rows(details)
                if _task_result_is_candidate_scoped(expected, task)
            ]
            details_payload = dict(details)
            if scoped_tasks:
                details_payload["task_results"] = scoped_tasks
                details_payload["task_results_total"] = len(scoped_tasks)
                details_payload["task_results_included"] = len(scoped_tasks)
                details_payload["task_results_limit"] = CANDIDATE_STATE_TASK_RESULT_LIMIT
                details_payload["task_results_overflow"] = False
            else:
                details_payload.pop("task_results", None)
                details_payload.pop("task_results_total", None)
                details_payload.pop("task_results_included", None)
                details_payload.pop("task_results_limit", None)
                details_payload.pop("task_results_overflow", None)
                details_payload.pop("task_results_omitted", None)
            event_payload["details"] = details_payload
        if _shared_cycle_row_is_candidate_scoped(expected, event_payload) or _event_has_candidate_scoped_failure(
            expected,
            event_payload,
        ):
            scoped_events.append(event_payload)
    return scoped_events


def _event_has_candidate_scoped_failure(expected: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    details = event.get("details")
    if not isinstance(details, Mapping):
        return False
    for key in ("task_identity", "failed_task", "failed_task_identity"):
        value = details.get(key)
        if (
            isinstance(value, Mapping)
            and _state_row_has_authoritative_candidate_proof(expected, value)
            and _state_task_payload_failed(value)
        ):
            return True
    for task in _bounded_task_result_rows(details):
        if _task_result_is_candidate_scoped(expected, task) and _state_task_payload_failed(task):
            return True
    return False


def _task_result_is_candidate_scoped(expected: Mapping[str, Any], task: Mapping[str, Any]) -> bool:
    return _shared_cycle_row_is_candidate_scoped(expected, task)


def _shared_cycle_row_is_candidate_scoped(expected: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    row_values = _legacy_identity_values(row)
    expected_values = _legacy_identity_values(expected)
    if _shared_cycle_identity_values_match_candidate(row_values, expected_values):
        return True
    for nested in _nested_state_identity_payloads(row):
        if _shared_cycle_identity_values_match_candidate(_legacy_identity_values(nested), expected_values):
            return True
    return False


def _shared_cycle_identity_values_match_candidate(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    if not row_values:
        return False
    if _state_values_are_scoped_to_other_candidate(row_values, expected_values):
        return False
    if _state_values_have_complete_m23_identity(row_values, expected_values):
        return True
    for identity_field in ("model_id", "forcing_version_id", "hydro_run_id", "published_manifest_id", "run_id"):
        value = row_values.get(identity_field)
        expected = expected_values.get(identity_field)
        if value not in (None, "") and expected not in (None, "") and value == expected:
            return True
    return False


def _state_task_payload_failed(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("state") or "")
    return status not in {"", "succeeded", "complete", "published"}


def _candidate_state_source_has_authoritative_ancestor(source: str, authoritative_sources: set[str]) -> bool:
    current = source
    while "." in current:
        current = current.rsplit(".", 1)[0]
        if current in authoritative_sources:
            return True
    return False


def _candidate_state_source_allows_nested_authority(source: str) -> bool:
    return source != "candidate_state" and not re.fullmatch(r"pipeline_events\[\d+\]", source)


def _candidate_state_decision_event(
    event: Mapping[str, Any],
    *,
    authoritative: bool,
    source: str,
    legacy_sources: set[str],
) -> dict[str, Any]:
    if authoritative:
        return dict(event)
    sanitized: dict[str, Any] = {}
    for key in ("event_id", "entity_id", "created_at", "updated_at"):
        value = event.get(key)
        if value not in (None, ""):
            sanitized[key] = value
    details = event.get("details")
    if isinstance(details, Mapping):
        details_payload: dict[str, Any] = {}
        retry_binding_id = _first_nonempty(details, "previous_job_id", "failed_job_id", "job_id", "pipeline_job_id")
        if event.get("event_type") in {"retry", "manual_retry"} and retry_binding_id not in (None, ""):
            sanitized["event_type"] = event.get("event_type")
            for key in (
                "trigger",
                "manual_retry_marker",
                "retry_count",
                "new_attempt",
                "previous_job_id",
                "failed_job_id",
                "job_id",
                "pipeline_job_id",
                "prior_failure_reason",
                "slurm_job_id",
            ):
                value = details.get(key)
                if value not in (None, ""):
                    details_payload[key] = value
        for key in ("stage", "job_type"):
            value = details.get(key)
            if value not in (None, ""):
                details_payload[key] = value
        for key in ("task_identity", "failed_task", "failed_task_identity"):
            value = details.get(key)
            nested_source = f"{source}.details.{key}"
            if isinstance(value, Mapping) and nested_source not in legacy_sources:
                details_payload[key] = value
        task_results = [
            task
            for task_index, task in enumerate(_bounded_task_result_rows(details))
            if f"{source}.details.task_results[{task_index}]" not in legacy_sources
        ]
        if task_results:
            details_payload["task_results"] = task_results
            details_payload["task_results_total"] = len(task_results)
            details_payload["task_results_included"] = len(task_results)
            details_payload["task_results_limit"] = CANDIDATE_STATE_TASK_RESULT_LIMIT
            details_payload["task_results_overflow"] = False
        if details_payload:
            sanitized["details"] = details_payload
    return sanitized


def _strip_top_level_candidate_state_decision_fields(state: dict[str, Any]) -> None:
    _strip_top_level_hydro_decision_fields(state)
    _strip_top_level_pipeline_decision_fields(state)
    for key in (
        "retry_limit",
        "max_retries",
        "cycle_status",
        "forecast_cycle_status",
        "forcing_status",
        "forcing_version_status",
    ):
        state.pop(key, None)


def _strip_top_level_hydro_decision_fields(state: dict[str, Any]) -> None:
    for key in (
        "hydro_status",
        "hydro_run_status",
        "output_uri",
        "durable_output_uri",
        "hydro_error_code",
        "hydro_error_message",
        "durable_shud_output_exists",
        "force_native_shud_rerun",
        "force_rerun",
        "force_shud_rerun",
    ):
        state.pop(key, None)


def _strip_top_level_pipeline_decision_fields(state: dict[str, Any]) -> None:
    for key in (
        "active_slurm_jobs",
        "pipeline_status",
        "job_status",
        "status",
        "failed_stage",
        "stage",
        "restart_stage",
        "error_code",
        "reason_code",
        "failure_reason",
        "last_error",
        "previous_error",
        "error_message",
        "message",
        "retry_attempt",
        "attempt",
        "retry_count",
        "manual_retry",
        "manual_retry_marker",
        "manual_retry_requested_by",
        "manual_retry_request_id",
        "manual_retry_reason",
        "manual_retry_created_at",
        "manual_retry_requested_at",
        "prior_failure_reason",
        "retryable",
        "permanent",
        "failure_classifier",
        "classifier",
        "array_task_id",
        "task_id",
        "original_task_id",
        "slurm_job_id",
        "successful_sibling_outputs_reused",
        "shared_cycle_aggregate",
        "shared_cycle_ambiguous_failure",
    ):
        state.pop(key, None)


def _pipeline_terminal_success_is_candidate_scoped(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
) -> bool:
    if state.get("shared_cycle_aggregate") is True:
        return False
    matching_jobs = [
        job
        for job in _state_jobs(state)
        if str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        in TERMINAL_PIPELINE_SUCCESS_STATUSES
    ]
    if not matching_jobs:
        return not _has_candidate_task_failure(state)
    for job in reversed(matching_jobs):
        run_id = str(job.get("run_id") or "")
        model_id = job.get("model_id")
        if run_id == candidate.run_id:
            return True
        if str(model_id or "") == candidate.model_id:
            return True
        if run_id.startswith("cycle_") and model_id in (None, ""):
            return False
    return False


def _terminal_hydro_truth_supersedes_failure(state: Mapping[str, Any]) -> bool:
    hydro_run = state.get("hydro_run")
    if isinstance(hydro_run, Mapping):
        hydro_truth_time = _first_state_datetime(hydro_run, "updated_at", "finished_at", "created_at")
    else:
        hydro_truth_time = None
    if hydro_truth_time is None:
        return not _state_has_failure_signal(state)
    failure_truth_time = _latest_failure_truth_timestamp(state)
    return failure_truth_time is None or hydro_truth_time >= failure_truth_time


def _latest_failure_truth_timestamp(state: Mapping[str, Any]) -> datetime | None:
    timestamps: list[datetime] = []
    for job in _state_jobs(state):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if status not in FAILED_PIPELINE_STATUSES and not job.get("error_code"):
            continue
        timestamp = _first_state_datetime(job, "updated_at", "finished_at", "submitted_at", "created_at")
        if timestamp is not None:
            timestamps.append(timestamp)
    for event in _state_events(state):
        if _event_is_manual_retry_marker(event):
            continue
        details = event.get("details")
        details_mapping = details if isinstance(details, Mapping) else {}
        status = str(
            event.get("status_to")
            or details_mapping.get("status_to")
            or details_mapping.get("status")
            or details_mapping.get("state")
            or ""
        )
        if status not in FAILED_PIPELINE_STATUSES and not details_mapping.get("error_code"):
            continue
        timestamp = _first_state_datetime(event, "created_at", "updated_at", "finished_at", "submitted_at")
        if timestamp is not None:
            timestamps.append(timestamp)
    return max(timestamps) if timestamps else None


def _has_candidate_task_failure(state: Mapping[str, Any]) -> bool:
    for event in _state_events(state):
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        for task in _bounded_task_result_rows(details):
            if str(task.get("status") or task.get("state") or "") not in {"", "succeeded"}:
                return True
    return False


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


def _bounded_candidate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    bounded = dict(state)
    events = _state_events(bounded)
    if events:
        bounded["pipeline_events"] = [_bounded_candidate_event(event) for event in events]
        bounded.pop("events", None)
    return bounded


def _bounded_candidate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    details = payload.get("details")
    if not isinstance(details, Mapping):
        return payload
    details_payload = dict(details)
    task_sample = _bounded_task_result_sample(details_payload)
    if task_sample is not None:
        task_rows, task_metadata = task_sample
        details_payload["task_results"] = task_rows
        details_payload.update(task_metadata)
    payload["details"] = details_payload
    return payload


def _event_identity_containers(index: int, event: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    containers: list[tuple[str, Mapping[str, Any]]] = [(f"pipeline_events[{index}]", event)]
    details = event.get("details")
    if not isinstance(details, Mapping):
        return containers
    identity = details.get("identity")
    if isinstance(identity, Mapping):
        containers.append((f"pipeline_events[{index}].details.identity", identity))
    containers.append((f"pipeline_events[{index}].details", details))
    for task_index, task in enumerate(_bounded_task_result_rows(details)):
        containers.append((f"pipeline_events[{index}].details.task_results[{task_index}]", task))
        task_identity = task.get("identity")
        if isinstance(task_identity, Mapping):
            containers.append(
                (f"pipeline_events[{index}].details.task_results[{task_index}].identity", task_identity)
            )
    for key in ("task_identity", "failed_task", "failed_task_identity"):
        value = details.get(key)
        if isinstance(value, Mapping):
            containers.append((f"pipeline_events[{index}].details.{key}", value))
    return containers


def _legacy_non_authoritative_state_row(expected: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    return bool(row) and not _state_row_has_authoritative_candidate_proof(expected, row)


def _state_row_has_authoritative_candidate_proof(
    expected: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    include_nested: bool = True,
) -> bool:
    row_values = _legacy_identity_values(row)
    expected_values = _legacy_identity_values(expected)
    if _state_values_have_authoritative_candidate_proof(row_values, expected_values):
        return True
    if not include_nested:
        return False
    for nested in _nested_state_identity_payloads(row):
        nested_values = _legacy_identity_values(nested)
        if _state_values_have_authoritative_candidate_proof(nested_values, expected_values):
            return True
    return False


def _state_values_have_authoritative_candidate_proof(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    if not row_values:
        return False
    if _state_values_are_scoped_to_other_candidate(row_values, expected_values):
        return False
    if _state_values_have_complete_m23_identity(row_values, expected_values):
        return True
    if _state_values_have_candidate_scoped_m23_proof(row_values, expected_values):
        return True
    return _legacy_values_prove_same_candidate(row_values, expected_values)


def _state_row_is_scoped_to_other_candidate(expected: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    expected_values = _legacy_identity_values(expected)
    if _state_values_are_scoped_to_other_candidate(_legacy_identity_values(row), expected_values):
        return True
    return any(
        _state_values_are_scoped_to_other_candidate(_legacy_identity_values(nested), expected_values)
        for nested in _nested_state_identity_payloads(row)
    )


def _state_values_are_scoped_to_other_candidate(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    for identity_field in STATE_CANDIDATE_SCOPED_PROOF_FIELDS:
        value = row_values.get(identity_field)
        expected = expected_values.get(identity_field)
        if identity_field == "run_id" and _stage_cycle_run_matches_candidate(value, expected_values):
            continue
        if value not in (None, "") and expected not in (None, "") and value != expected:
            return True
    return False


def _state_values_have_complete_m23_identity(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    for identity_field in PRODUCTION_IDENTITY_FIELDS:
        value = row_values.get(identity_field)
        expected = expected_values.get(identity_field)
        if value in (None, "") or expected in (None, "") or value != expected:
            return False
    return True


def _state_values_have_candidate_scoped_m23_proof(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    return any(
        row_values.get(field) not in (None, "") and row_values.get(field) == expected_values.get(field)
        for field in STATE_CANDIDATE_SCOPED_PROOF_FIELDS
    )


def _legacy_values_prove_same_candidate(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    if not row_values:
        return False
    for identity_field in ("model_id", "source", "cycle_time"):
        value = row_values.get(identity_field)
        expected_value = expected_values.get(identity_field)
        if value not in (None, "") and expected_value not in (None, "") and value != expected_value:
            return False
    run_id = row_values.get("run_id")
    expected_run_id = expected_values.get("run_id")
    if run_id not in (None, ""):
        if run_id == expected_run_id:
            return True
        if not _stage_cycle_run_matches_candidate(run_id, expected_values):
            return False
        return True
    source = row_values.get("source")
    cycle_time = row_values.get("cycle_time")
    model_id = row_values.get("model_id")
    if source in (None, "") or cycle_time in (None, ""):
        return False
    if source != expected_values.get("source") or cycle_time != expected_values.get("cycle_time"):
        return False
    return model_id in (None, "", expected_values.get("model_id"))


def _state_row_has_m23_comparison_fields(values: Mapping[str, str]) -> bool:
    return any(
        field in values
        for field in (
            *STATE_M23_COMPARISON_FIELDS,
            *PRODUCTION_EVIDENCE_CORRELATION_FIELDS,
        )
    )


def _state_row_has_m23_comparison_evidence(row: Mapping[str, Any]) -> bool:
    if _state_row_has_m23_comparison_fields(_legacy_identity_values(row)):
        return True
    return any(
        _state_row_has_m23_comparison_fields(_legacy_identity_values(nested))
        for nested in _nested_state_identity_payloads(row)
    )


def _nested_state_identity_payloads(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payloads: list[Mapping[str, Any]] = []
    for key in ("identity", "task_identity", "failed_task", "failed_task_identity"):
        value = row.get(key)
        if isinstance(value, Mapping):
            payloads.append(value)
    details = row.get("details")
    if isinstance(details, Mapping):
        payloads.append(details)
        for key in ("identity", "task_identity", "failed_task", "failed_task_identity"):
            value = details.get(key)
            if isinstance(value, Mapping):
                payloads.append(value)
        for task in _bounded_task_result_rows(details):
            payloads.append(task)
            identity = task.get("identity")
            if isinstance(identity, Mapping):
                payloads.append(identity)
    return payloads


def _bounded_task_result_rows(details: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_sample = _bounded_task_result_sample(details)
    if task_sample is None:
        return []
    return task_sample[0]


def _bounded_task_result_sample(
    details: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], dict[str, Any]] | None:
    task_results = details.get("task_results")
    if not isinstance(task_results, Sequence) or isinstance(task_results, str | bytes | bytearray):
        return None
    task_rows: list[Mapping[str, Any]] = []
    observed_count = 0
    overflow = False
    for index, task in enumerate(task_results):
        observed_count = index + 1
        if index >= CANDIDATE_STATE_TASK_RESULT_LIMIT:
            overflow = True
            break
        if isinstance(task, Mapping):
            task_rows.append(dict(task))
    reported_total = _coerce_optional_nonnegative_int(details.get("task_results_total"))
    total = max(reported_total, observed_count) if reported_total is not None else observed_count
    included = len(task_rows)
    overflow = overflow or total > included
    metadata: dict[str, Any] = {
        "task_results_total": total,
        "task_results_included": included,
        "task_results_limit": CANDIDATE_STATE_TASK_RESULT_LIMIT,
        "task_results_overflow": overflow,
    }
    if overflow:
        metadata["task_results_omitted"] = max(total - included, 0)
    return task_rows, metadata


def _legacy_compatible_state_row(expected: Mapping[str, Any], row: Mapping[str, Any]) -> Mapping[str, Any]:
    row_values = _legacy_identity_values(row)
    expected_values = _legacy_identity_values(expected)
    if not _stage_cycle_run_matches_candidate(row_values.get("run_id"), expected_values):
        return row
    payload = dict(row)
    payload.pop("run_id", None)
    identity = payload.get("identity")
    if isinstance(identity, Mapping):
        identity_payload = dict(identity)
        identity_payload.pop("run_id", None)
        payload["identity"] = identity_payload
    return payload


def _legacy_identity_values(payload: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    aliases: dict[str, tuple[tuple[str, ...], ...]] = {
        "run_id": (("run_id",), ("identity", "run_id")),
        "model_id": (("model_id",), ("identity", "model_id")),
        "basin_id": (("basin_id",), ("identity", "basin_id")),
        "source": (("source",), ("source_id",), ("identity", "source"), ("identity", "source_id")),
        "cycle_time": (
            ("cycle_time",),
            ("cycle_time_utc",),
            ("identity", "cycle_time"),
            ("identity", "cycle_time_utc"),
        ),
        "basin_version_id": (("basin_version_id",), ("identity", "basin_version_id")),
        "river_network_version_id": (("river_network_version_id",), ("identity", "river_network_version_id")),
        "canonical_product_id": (("canonical_product_id",), ("identity", "canonical_product_id")),
        "forcing_version_id": (("forcing_version_id",), ("identity", "forcing_version_id")),
        "hydro_run_id": (("hydro_run_id",), ("identity", "hydro_run_id")),
        "published_manifest_id": (("published_manifest_id",), ("identity", "published_manifest_id")),
        "pipeline_job_id": (("pipeline_job_id",), ("identity", "pipeline_job_id")),
        "pipeline_event_id": (("pipeline_event_id",), ("identity", "pipeline_event_id")),
    }
    for identity_field, field_aliases in aliases.items():
        value = _first_nested_state_value(payload, field_aliases)
        if value in (None, ""):
            continue
        if identity_field == "source":
            try:
                value = normalize_source_id(str(value))
            except ValueError:
                value = str(value).strip()
        elif identity_field == "cycle_time":
            try:
                value = _format_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
            except ValueError:
                try:
                    value = _format_utc(datetime.strptime(str(value), "%Y%m%d%H").replace(tzinfo=UTC))
                except ValueError:
                    value = str(value).strip()
        else:
            value = str(value).strip()
        if value:
            values[identity_field] = value
    job_id = payload.get("job_id") or payload.get("entity_id")
    if "pipeline_job_id" not in values and job_id not in (None, "") and _looks_like_production_job_id(job_id):
        values["stage_job_id"] = str(job_id).strip()
    event_id = payload.get("event_id")
    if "pipeline_event_id" not in values and event_id not in (None, ""):
        values["stage_event_id"] = str(event_id).strip()
    return values


def _stage_cycle_run_matches_candidate(run_id: str | None, expected_values: Mapping[str, str]) -> bool:
    if run_id in (None, ""):
        return False
    source = str(expected_values.get("source") or "").lower()
    cycle_time = str(expected_values.get("cycle_time") or "")
    model_id = str(expected_values.get("model_id") or "")
    if not source or not cycle_time or not model_id:
        return False
    try:
        compact_cycle = format_cycle_time(cycle_time)
    except (TypeError, ValueError):
        return False
    prefix = f"cycle_{source}_{compact_cycle}"
    text = str(run_id)
    return text == prefix or (
        text.startswith(f"{prefix}_") and (text.endswith(f"_{model_id}") or f"_{model_id}_" in text)
    )


def _first_nested_state_value(payload: Mapping[str, Any], aliases: Sequence[tuple[str, ...]]) -> Any:
    for path in aliases:
        current: Any = payload
        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                current = None
                break
            current = current[key]
        if current not in (None, ""):
            return current
    return None


def _looks_like_production_job_id(value: Any) -> bool:
    text = str(value or "")
    return text.startswith(("job_fcst_", "job_cycle_"))


def _candidate_state_has_identity_mismatch(evidence: Mapping[str, Any]) -> bool:
    validation = evidence.get("production_identity_validation")
    return isinstance(validation, Mapping) and validation.get("status") == "mismatch"


def _state_jobs(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = state.get("pipeline_jobs") or state.get("jobs")
    max_jobs = _state_job_limit(state)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) for item in value if isinstance(item, Mapping)][:max_jobs]
    single = state.get("pipeline_job") or state.get("job")
    if isinstance(single, Mapping):
        return [dict(single)]
    fields = {
        "job_id",
        "pipeline_job_id",
        "run_id",
        "cycle_id",
        "job_type",
        "slurm_job_id",
        "array_task_id",
        "model_id",
        "status",
        "pipeline_status",
        "job_status",
        "stage",
        "exit_code",
        "retry_count",
        "error_code",
        "error_message",
        "log_uri",
    }
    if any(key in state for key in fields):
        return [dict(state)]
    return []


def _state_events(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = state.get("pipeline_events") or state.get("events")
    max_events = _state_event_limit(state)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) for item in value if isinstance(item, Mapping)][:max_events]
    return []


def _state_job_limit(state: Mapping[str, Any]) -> int:
    return max(_coerce_int(state.get("job_limit"), default=DEFAULT_CANDIDATE_STATE_JOB_LIMIT), 1)


def _state_event_limit(state: Mapping[str, Any]) -> int:
    return max(_coerce_int(state.get("event_limit"), default=DEFAULT_CANDIDATE_STATE_EVENT_LIMIT), 1)


def _state_overflow_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "job_limit": _state_job_limit(state),
        "event_limit": _state_event_limit(state),
    }
    overflow = False
    for count_key, limit_key, output_key in (
        ("pipeline_jobs_total", "job_limit", "pipeline_jobs"),
        ("pipeline_events_total", "event_limit", "pipeline_events"),
    ):
        count = state.get(count_key)
        if count in (None, ""):
            continue
        count_value = _coerce_int(count, default=0)
        limit_value = int(evidence[limit_key])
        evidence[f"{output_key}_total"] = count_value
        evidence[f"{output_key}_returned"] = min(count_value, limit_value)
        if count_value > limit_value:
            evidence[f"{output_key}_overflow"] = True
            overflow = True
    if state.get("state_truncated") is True:
        overflow = True
        evidence["state_truncated"] = True
    if not overflow:
        return {}
    evidence["bounded"] = True
    evidence["overflow"] = True
    evidence["reason"] = "candidate_state_row_limit_applied"
    return evidence


def _job_state_evidence(job: Mapping[str, Any]) -> dict[str, Any]:
    kept = {
        key: job.get(key)
        for key in (
            "job_id",
            "pipeline_job_id",
            "pipeline_event_id",
            "run_id",
            "cycle_id",
            "job_type",
            "slurm_job_id",
            "array_task_id",
            "model_id",
            "basin_id",
            "source",
            "source_id",
            "cycle_time",
            "basin_version_id",
            "river_network_version_id",
            "canonical_product_id",
            "forcing_version_id",
            "hydro_run_id",
            "published_manifest_id",
            "status",
            "stage",
            "exit_code",
            "retry_count",
            "error_code",
            "error_message",
            "log_uri",
            "repair_status",
            "superseded_by_job_id",
            "repaired_by_job_id",
            "repairs_job_id",
            "repairs_job_ids",
            "active_blocker",
        )
        if key in job and job.get(key) is not None
    }
    return _evidence_safe(kept)


def _optional_mapping_state(value: Any, *, defaults: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = dict(value) if isinstance(value, Mapping) else {}
    for key, fallback in defaults.items():
        if fallback not in (None, ""):
            payload.setdefault(key, fallback)
    payload = {key: val for key, val in payload.items() if val not in (None, "")}
    return _evidence_safe(payload) if payload else None


def _state_status(state: Mapping[str, Any], *keys: str) -> str | None:
    explicit_key_seen = False
    for key in keys:
        explicit_key_seen = explicit_key_seen or key in state
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    if explicit_key_seen:
        return None
    for job in reversed(_state_jobs(state)):
        for key in keys:
            value = job.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _state_output_uri(state: Mapping[str, Any]) -> str | None:
    for container_key in ("hydro_run", "outputs", "runtime_outputs"):
        value = state.get(container_key)
        if isinstance(value, Mapping) and value.get("output_uri") not in (None, ""):
            return str(value["output_uri"])
    value = state.get("output_uri") or state.get("durable_output_uri")
    return str(value) if value not in (None, "") else None


def _state_active_jobs(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    explicit = state.get("active_slurm_jobs")
    if isinstance(explicit, Sequence) and not isinstance(explicit, str | bytes | bytearray):
        return [_evidence_safe(dict(job)) for job in explicit if isinstance(job, Mapping)]
    active: list[dict[str, Any]] = []
    for job in _state_jobs(state):
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if job.get("slurm_job_id") and status in ACTIVE_PIPELINE_STATUSES:
            active.append(_job_state_evidence(job))
    return active


def _manual_retry_requested(state: Mapping[str, Any]) -> bool:
    marker = _latest_manual_retry_marker(state)
    if marker is None:
        return False
    if _manual_retry_marker_repairs_historical_failure(state, marker):
        return False
    blocker = _latest_manual_retry_blocker(state)
    if blocker is None:
        return True
    return _manual_retry_marker_overrides_blocker(marker, blocker)


def _manual_retry_markers(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    marker = state.get("manual_retry") or state.get("manual_retry_marker")
    if isinstance(marker, Mapping):
        if marker.get("marker") or marker.get("requested") or marker.get("enabled"):
            markers.append(
                _manual_retry_marker_record(
                    marker,
                    state=state,
                    source="state",
                    order=-1,
                    default_attempt=_state_retry_attempt(state) + 1,
                )
            )
    elif marker is not None and bool(marker):
        markers.append(
            _manual_retry_marker_record(
                {},
                state=state,
                source="state",
                order=-1,
                default_attempt=_state_retry_attempt(state) + 1,
            )
        )
    for order, event in enumerate(_state_events(state)):
        details = event.get("details")
        if event.get("event_type") in {"retry", "manual_retry"} and isinstance(details, Mapping):
            if details.get("trigger") == "manual" or details.get("manual_retry_marker") is True:
                markers.append(
                    _manual_retry_marker_record(
                        details,
                        state=event,
                        source="event",
                        order=order,
                        event_id=event.get("event_id"),
                        entity_id=event.get("entity_id"),
                    )
                )
    return markers


def _manual_retry_marker_record(
    payload: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    source: str,
    order: int,
    default_attempt: int | None = None,
    event_id: Any = None,
    entity_id: Any = None,
) -> dict[str, Any]:
    timestamp = _first_state_datetime(
        payload,
        "created_at",
        "requested_at",
        "updated_at",
        "submitted_at",
    ) or _first_state_datetime(
        state,
        "manual_retry_created_at",
        "manual_retry_requested_at",
        "created_at",
        "updated_at",
        "submitted_at",
    )
    attempt = _first_state_int(payload, "new_attempt", "retry_count", "attempt", default=default_attempt)
    return {
        "source": source,
        "timestamp": timestamp,
        "attempt": attempt,
        "previous_job_id": _first_nonempty(payload, "previous_job_id", "failed_job_id", "job_id"),
        "entity_id": entity_id,
        "event_id": event_id,
        "order": order,
    }


def _latest_manual_retry_marker(state: Mapping[str, Any]) -> dict[str, Any] | None:
    markers = _manual_retry_markers(state)
    if not markers:
        return None
    return max(markers, key=_state_truth_sort_key)


def _manual_retry_marker_repairs_historical_failure(
    state: Mapping[str, Any],
    marker: Mapping[str, Any],
) -> bool:
    previous_job_id = marker.get("previous_job_id")
    if previous_job_id in (None, ""):
        return False
    previous_job_id_text = str(previous_job_id)
    for job in _state_jobs(state):
        if str(job.get("job_id") or job.get("pipeline_job_id") or "") == previous_job_id_text:
            if not _pipeline_job_is_repaired_stage_evidence(job):
                return False
            marker_entity_id = marker.get("entity_id")
            repairing_retry_job_id = job.get("repaired_by_job_id") or job.get("superseded_by_job_id")
            return marker_entity_id in (None, "") or repairing_retry_job_id in (None, "") or str(
                marker_entity_id
            ) == str(repairing_retry_job_id)
    repaired_stage = state.get("repaired_stage_evidence")
    if not isinstance(repaired_stage, Mapping):
        return False
    if str(repaired_stage.get("original_failed_job_id") or "") != previous_job_id_text:
        return False
    marker_entity_id = marker.get("entity_id")
    repairing_retry_job_id = repaired_stage.get("repairing_retry_job_id")
    return marker_entity_id in (None, "") or str(marker_entity_id) == str(repairing_retry_job_id)


def _latest_manual_retry_blocker(state: Mapping[str, Any]) -> dict[str, Any] | None:
    blockers: list[dict[str, Any]] = []
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    if (
        _manual_retry_blocking_pipeline_status(pipeline_status)
        and not _state_has_only_unsubmitted_auto_retry_placeholders(state)
    ):
        blockers.append(
            _manual_retry_blocker_record(
                state,
                status=pipeline_status,
                source="pipeline_state",
                order=-1,
                attempt=_state_retry_attempt(state),
                active=pipeline_status in ACTIVE_PIPELINE_STATUSES,
            )
        )
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if _manual_retry_blocking_hydro_status(hydro_status):
        blockers.append(
            _manual_retry_blocker_record(
                _coerce_mapping_for_state(state.get("hydro_run")) or state,
                status=hydro_status,
                source="hydro_state",
                order=-1,
                attempt=_state_retry_attempt(state),
                active=hydro_status in ACTIVE_HYDRO_STATUSES,
            )
        )
    for order, job in enumerate(_state_jobs(state)):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if not _manual_retry_blocking_pipeline_status(status):
            continue
        if _job_is_unsubmitted_auto_retry_placeholder(job):
            continue
        blockers.append(
            _manual_retry_blocker_record(
                job,
                status=status,
                source="pipeline_job",
                order=order,
                attempt=_coerce_int(job.get("retry_count"), default=0),
                active=status in ACTIVE_PIPELINE_STATUSES,
            )
        )
    for order, event in enumerate(_state_events(state)):
        if _event_is_manual_retry_marker(event):
            continue
        details = event.get("details")
        details_mapping = details if isinstance(details, Mapping) else {}
        status = str(
            event.get("status_to")
            or details_mapping.get("status_to")
            or details_mapping.get("status")
            or details_mapping.get("state")
            or ""
        )
        if not _manual_retry_blocking_pipeline_status(status):
            continue
        blockers.append(
            _manual_retry_blocker_record(
                {**dict(details_mapping), **dict(event)},
                status=status,
                source="pipeline_event",
                order=order,
                attempt=_first_state_int(details_mapping, "final_retry_count", "retry_count", "attempt", default=0),
                active=status in ACTIVE_PIPELINE_STATUSES,
            )
        )
    if not blockers:
        return None
    return max(blockers, key=_state_truth_sort_key)


def _manual_retry_blocker_record(
    payload: Mapping[str, Any],
    *,
    status: str | None,
    source: str,
    order: int,
    attempt: int | None,
    active: bool,
) -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "active": active,
        "timestamp": _first_state_datetime(
            payload,
            "updated_at",
            "finished_at",
            "submitted_at",
            "started_at",
            "created_at",
            "event_created_at",
        ),
        "attempt": attempt,
        "job_id": _first_nonempty(payload, "job_id", "pipeline_job_id", "entity_id"),
        "event_id": payload.get("event_id"),
        "order": order,
    }


def _manual_retry_marker_overrides_blocker(marker: Mapping[str, Any], blocker: Mapping[str, Any]) -> bool:
    if blocker.get("active") is True:
        return False
    if _manual_retry_marker_bound_to_blocker(marker, blocker):
        return True
    marker_timestamp = marker.get("timestamp")
    blocker_timestamp = blocker.get("timestamp")
    if isinstance(marker_timestamp, datetime) and isinstance(blocker_timestamp, datetime):
        if marker_timestamp > blocker_timestamp:
            return True
        if marker_timestamp == blocker_timestamp and _state_truth_sequence(marker) > _state_truth_sequence(blocker):
            return True
        return False
    if isinstance(marker_timestamp, datetime) and blocker_timestamp is None:
        return True
    if marker_timestamp is None and blocker_timestamp is None:
        marker_attempt = marker.get("attempt")
        blocker_attempt = blocker.get("attempt")
        if marker_attempt is not None and blocker_attempt is not None:
            return _coerce_int(marker_attempt, default=-1) > _coerce_int(blocker_attempt, default=-1)
        return True
    return False


def _manual_retry_marker_bound_to_blocker(marker: Mapping[str, Any], blocker: Mapping[str, Any]) -> bool:
    if blocker.get("active") is True:
        return False
    marker_attempt = marker.get("attempt")
    blocker_attempt = blocker.get("attempt")
    if marker_attempt is None or blocker_attempt is None:
        return False
    if _coerce_int(marker_attempt, default=-1) <= _coerce_int(blocker_attempt, default=-1):
        return False
    previous_job_id = marker.get("previous_job_id")
    blocker_job_id = blocker.get("job_id")
    if previous_job_id not in (None, "") and blocker_job_id not in (None, ""):
        return str(previous_job_id) == str(blocker_job_id)
    return True


def _manual_retry_blocking_pipeline_status(status: str | None) -> bool:
    return status in ACTIVE_PIPELINE_STATUSES or status in FAILED_PIPELINE_STATUSES or status == "cancelled"


def _manual_retry_blocking_hydro_status(status: str | None) -> bool:
    return status in ACTIVE_HYDRO_STATUSES or status in {"failed", "cancelled", "permanently_failed"}


def _event_is_manual_retry_marker(event: Mapping[str, Any]) -> bool:
    details = event.get("details")
    if event.get("event_type") not in {"retry", "manual_retry"} or not isinstance(details, Mapping):
        return False
    return details.get("trigger") == "manual" or details.get("manual_retry_marker") is True


def _state_truth_sort_key(truth: Mapping[str, Any]) -> tuple[int, datetime, int, int, int]:
    timestamp = truth.get("timestamp")
    parsed = timestamp if isinstance(timestamp, datetime) else datetime.min.replace(tzinfo=UTC)
    return (
        1 if isinstance(timestamp, datetime) else 0,
        parsed,
        _coerce_int(truth.get("attempt"), default=-1),
        _coerce_int(truth.get("event_id"), default=-1),
        _coerce_int(truth.get("order"), default=-1),
    )


def _state_truth_sequence(truth: Mapping[str, Any]) -> tuple[int, int]:
    return (
        _coerce_int(truth.get("event_id"), default=-1),
        _coerce_int(truth.get("order"), default=-1),
    )


def _first_state_datetime(payload: Mapping[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        value = payload.get(key)
        parsed = _parse_state_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _parse_state_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, str) and value.strip():
        try:
            return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _first_state_int(payload: Mapping[str, Any], *keys: str, default: int | None = None) -> int | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=default or 0)
    return default


def _first_nonempty(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _coerce_mapping_for_state(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _manual_retry_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    marker = state.get("manual_retry") or state.get("manual_retry_marker")
    payload = dict(marker) if isinstance(marker, Mapping) else {}
    if marker and not payload:
        payload["marker"] = True
    for key in ("requested_by", "request_id", "reason", "created_at"):
        value = state.get(f"manual_retry_{key}") or state.get(key)
        if value not in (None, ""):
            payload.setdefault(key, value)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if event.get("event_type") in {"retry", "manual_retry"} and isinstance(details, Mapping):
            if details.get("trigger") != "manual" and details.get("manual_retry_marker") is not True:
                continue
            payload.setdefault("marker", True)
            payload.setdefault("requested", True)
            if details.get("retry_count") not in (None, ""):
                payload.setdefault("new_attempt", _coerce_int(details.get("retry_count"), default=0))
            for key in ("prior_failure_reason", "previous_error", "previous_job_id", "slurm_job_id"):
                value = details.get(key)
                if value not in (None, ""):
                    payload.setdefault(key, value)
            break
    return _evidence_safe(payload)


def _state_retry_attempt(state: Mapping[str, Any]) -> int:
    for key in ("retry_attempt", "attempt", "retry_count"):
        value = state.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=0)
    jobs = _state_jobs(state)
    if jobs:
        return max(_coerce_int(job.get("retry_count"), default=0) for job in jobs)
    return 0


def _state_retry_limit(state: Mapping[str, Any]) -> int | None:
    for key in ("retry_limit", "max_retries"):
        value = state.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=0)
    return DEFAULT_RETRY_LIMIT


def _failed_stage(state: Mapping[str, Any]) -> str | None:
    for key in ("failed_stage", "stage", "restart_stage"):
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    for job in reversed(_state_jobs(state)):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        status = str(job.get("status") or "")
        if status in FAILED_PIPELINE_STATUSES and job.get("stage") not in (None, ""):
            return str(job["stage"])
    return None


def _canonical_downstream_stage(stage: str | None) -> str | None:
    if stage is None:
        return None
    normalized = DOWNSTREAM_STAGE_ALIASES.get(stage)
    if normalized in DOWNSTREAM_RESTART_STAGES:
        return normalized
    return None


def _durable_shud_output_exists(state: Mapping[str, Any]) -> bool:
    if state.get("durable_shud_output_exists") is not None:
        return bool(state.get("durable_shud_output_exists"))
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if hydro_status in {"succeeded", "parsed", "frequency_done", "published", "complete"}:
        return True
    if _state_output_uri(state):
        for job in _state_jobs(state):
            stage = str(job.get("stage") or job.get("job_type") or "")
            status = str(job.get("status") or "")
            if stage in NATIVE_SHUD_STAGE_ALIASES and status in TERMINAL_PIPELINE_SUCCESS_STATUSES:
                return True
    return False


def _force_native_shud_rerun(state: Mapping[str, Any]) -> bool:
    return bool(state.get("force_native_shud_rerun") or state.get("force_rerun") or state.get("force_shud_rerun"))


def _failure_policy_payload(
    state: Mapping[str, Any],
    *,
    default_error_code: str | None = None,
    manual: bool = False,
) -> dict[str, Any]:
    error_code = _state_error_code(state) or default_error_code or "UNKNOWN_FAILURE"
    attempt = _state_retry_attempt(state)
    retry_limit = _state_retry_limit(state)
    classification = classify_failure(error_code, attempt=attempt, retry_limit=retry_limit, manual=manual)
    stage = _failed_stage(state)
    explicit_classifier = state.get("failure_classifier") or state.get("classifier")
    if explicit_classifier not in (None, ""):
        classification["classifier"] = str(explicit_classifier)
    if state.get("retryable") is True and not classification["limit_exhausted"]:
        classification["retryable"] = True
        classification["permanent"] = False
    if state.get("permanent") is True:
        classification["retryable"] = False
        classification["permanent"] = True
    return {
        **classification,
        "error_message": _state_error_message(state),
        "stage": stage,
        "task_identity": _state_task_identity(state),
    }


def _state_error_code(state: Mapping[str, Any]) -> str | None:
    for key in ("error_code", "reason_code", "failure_reason", "last_error", "previous_error"):
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    hydro_run = state.get("hydro_run")
    if isinstance(hydro_run, Mapping):
        for key in ("error_code", "reason_code", "failure_reason", "last_error", "previous_error"):
            value = hydro_run.get(key)
            if value not in (None, ""):
                return str(value)
    for job in reversed(_state_jobs(state)):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        value = job.get("error_code") or job.get("reason_code")
        if value not in (None, ""):
            return str(value)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if isinstance(details, Mapping):
            value = details.get("error_code") or details.get("last_error") or details.get("previous_error")
            if value not in (None, ""):
                return str(value)
    return None


def _state_error_message(state: Mapping[str, Any]) -> str | None:
    for key in ("error_message", "message"):
        value = state.get(key)
        if value not in (None, ""):
            return str(_evidence_safe(str(value)))
    hydro_run = state.get("hydro_run")
    if isinstance(hydro_run, Mapping):
        for key in ("error_message", "message"):
            value = hydro_run.get(key)
            if value not in (None, ""):
                return str(_evidence_safe(str(value)))
    for job in reversed(_state_jobs(state)):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        value = job.get("error_message")
        if value not in (None, ""):
            return str(_evidence_safe(str(value)))
    return None


def _state_task_identity(state: Mapping[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key in ("task_id", "array_task_id", "original_task_id", "stage", "job_id", "slurm_job_id"):
        value = state.get(key)
        if value not in (None, ""):
            identity[key] = value
    if identity:
        return _evidence_safe(identity)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        for key in ("task_identity", "failed_task", "failed_task_identity"):
            value = details.get(key)
            if isinstance(value, Mapping):
                for nested_key in ("task_id", "array_task_id", "original_task_id", "stage", "job_id", "slurm_job_id"):
                    nested_value = value.get(nested_key)
                    if nested_value not in (None, ""):
                        identity[nested_key] = nested_value
                if identity:
                    return _evidence_safe(identity)
        for task in _bounded_task_result_rows(details):
            status = str(task.get("status") or task.get("state") or "")
            if status in {"succeeded", ""}:
                continue
            identity["array_task_id"] = task.get("array_task_id", task.get("task_id"))
            identity["task_id"] = task.get("task_id", task.get("array_task_id"))
            if details.get("stage") not in (None, ""):
                identity["stage"] = details.get("stage")
            if task.get("slurm_job_id") not in (None, ""):
                identity["slurm_job_id"] = task.get("slurm_job_id")
            return _evidence_safe(identity)
    for job in reversed(_state_jobs(state)):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        for key in ("array_task_id", "stage", "job_id", "slurm_job_id"):
            value = job.get(key)
            if value not in (None, ""):
                identity[key] = value
        if identity:
            return _evidence_safe(identity)
    return {}


def _permanent_reason(state: Mapping[str, Any], failure: Mapping[str, Any]) -> str:
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    if pipeline_status == "permanently_failed":
        return "permanent_failure_guard"
    if failure.get("classifier") == "policy_blocked":
        return "policy_blocked"
    if failure.get("limit_exhausted") and failure.get("retryable") is False:
        if str(failure.get("reason_code") or "") in TRANSIENT_RETRY_REASON_CODES:
            return "retry_limit_exhausted"
    return "permanent_failure_guard"


def _prior_failure_reason(state: Mapping[str, Any]) -> str | None:
    for key in ("prior_failure_reason", "previous_error", "last_error", "error_code"):
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if isinstance(details, Mapping):
            value = details.get("prior_failure_reason") or details.get("previous_error") or details.get("last_error")
            if value not in (None, ""):
                return str(value)
    return None


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_nonnegative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _downstream_retry_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _durable_shud_output_exists(state):
        return None
    failed_stage = _canonical_downstream_stage(_failed_stage(state))
    if failed_stage is None:
        return None
    if _force_native_shud_rerun(state):
        return None
    failure = _failure_policy_payload(state, default_error_code=f"{failed_stage.upper()}_FAILED")
    if _downstream_failure_restartable(failure):
        failure = {
            **failure,
            "retryable": True,
            "permanent": False,
            "limit_exhausted": False,
        }
    if failure["permanent"]:
        return None
    return {
        **base_evidence,
        "decision": "retry_downstream",
        "reason": "resume_downstream_after_durable_shud",
        "restart_stage": failed_stage,
        "restart_from_stage": failed_stage,
        "native_shud_resubmitted": False,
        "durable_shud_output_reused": True,
        "durable_output_uri": _state_output_uri(state),
        "force_native_shud_rerun": False,
        "failure": failure,
        "retry_policy": {
            "automatic_retry_allowed": failure["retryable"],
            "manual_retry_required": failure["permanent"],
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
    }


def _downstream_failure_restartable(failure: Mapping[str, Any]) -> bool:
    if failure.get("limit_exhausted") is True:
        return False
    if str(failure.get("classifier") or "") in {"malformed_input", "policy_blocked"}:
        return False
    reason_code = str(failure.get("reason_code") or "").upper()
    if reason_code in {"INVALID_MANIFEST", "MANIFEST_SCHEMA_INVALID", "MALFORMED_INPUT", "POLICY_BLOCKED"}:
        return False
    return True


def _missing_raw_manifest_repair_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _state_has_failure_signal(state):
        return None
    failed_stage = str(_failed_stage(state) or "")
    if failed_stage == "" or _is_source_cycle_download_stage(failed_stage):
        return None
    manifest_uri = _forecast_cycle_manifest_uri(candidate, state)
    if manifest_uri in (None, ""):
        return None
    if not _is_raw_manifest_object_uri(str(manifest_uri)):
        return None
    if not _has_successful_download_stage(state):
        return None
    if not _object_manifest_is_missing(candidate, str(manifest_uri)):
        return None
    failure = _failure_policy_payload(state)
    failure = {
        **failure,
        "retryable": True,
        "permanent": False,
        "limit_exhausted": False,
        "classifier": "recoverable_missing_raw_manifest",
    }
    return {
        **base_evidence,
        "decision": "retry_failed",
        "reason": "repair_missing_raw_manifest",
        "restart_stage": None,
        "restart_from_stage": "download",
        "fresh_ingestion": {"required": True, "mode": "full_chain"},
        "stage": failed_stage,
        "task_identity": _state_task_identity(state),
        "failure": failure,
        "raw_manifest_repair": {
            "manifest_uri": str(manifest_uri),
            "manifest_exists": False,
            "successful_download_stage": True,
            "downstream_failed_stage": failed_stage,
        },
        "retry_policy": {
            "automatic_retry_allowed": True,
            "manual_retry_required": False,
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _repaired_raw_manifest_downstream_retry_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _state_has_failure_signal(state):
        return None
    failed_stage = str(_failed_stage(state) or "")
    if failed_stage == "" or _is_source_cycle_download_stage(failed_stage):
        return None
    manifest_uri = _forecast_cycle_manifest_uri(candidate, state)
    if manifest_uri in (None, ""):
        return None
    if not _is_raw_manifest_object_uri(str(manifest_uri)):
        return None
    if _object_manifest_is_missing(candidate, str(manifest_uri)):
        return None
    repair_download = _latest_successful_download_stage(state)
    if repair_download is None:
        return None
    failed_job = _latest_failed_job_for_stage(state, failed_stage)
    if failed_job is None:
        return None
    repair_time = _job_terminal_time(repair_download)
    failed_time = _job_terminal_time(failed_job)
    if repair_time is not None and failed_time is not None and repair_time <= failed_time:
        return None
    failure = _failure_policy_payload(state)
    failure = {
        **failure,
        "retryable": True,
        "permanent": False,
        "limit_exhausted": False,
        "classifier": "recoverable_downstream_after_raw_repair",
    }
    return {
        **base_evidence,
        "decision": "retry_failed",
        "reason": "retry_downstream_after_raw_repair",
        "restart_stage": None,
        "restart_from_stage": "download",
        "fresh_ingestion": {"required": False, "mode": "reuse_repaired_raw_then_full_chain"},
        "stage": failed_stage,
        "task_identity": _state_task_identity(state),
        "failure": failure,
        "raw_manifest_repair": {
            "manifest_uri": str(manifest_uri),
            "manifest_exists": True,
            "successful_download_stage": True,
            "successful_download_job_id": repair_download.get("job_id") or repair_download.get("pipeline_job_id"),
            "downstream_failed_stage": failed_stage,
            "downstream_failed_job_id": failed_job.get("job_id") or failed_job.get("pipeline_job_id"),
        },
        "retry_policy": {
            "automatic_retry_allowed": True,
            "manual_retry_required": False,
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _forecast_cycle_manifest_uri(candidate: SchedulerCandidateLike, state: Mapping[str, Any]) -> str | None:
    forecast_cycle = state.get("forecast_cycle")
    if isinstance(forecast_cycle, Mapping):
        value = forecast_cycle.get("manifest_uri")
        if value not in (None, ""):
            return str(value)
    value = state.get("manifest_uri") or state.get("raw_manifest_uri")
    if value not in (None, ""):
        return str(value)
    return f"raw/{candidate.source_id}/{format_cycle_time(candidate.cycle_time_utc)}/manifest.json"


def _is_raw_manifest_object_uri(manifest_uri: str) -> bool:
    value = manifest_uri.strip()
    if value.startswith("s3://"):
        parsed = urlparse(value)
        path = parsed.path.lstrip("/")
        return path.startswith("raw/") and path.endswith("/manifest.json")
    return value.startswith("raw/") and value.endswith("/manifest.json")


def _has_successful_download_stage(state: Mapping[str, Any]) -> bool:
    return _latest_successful_download_stage(state) is not None


def _latest_successful_download_stage(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    matches: list[Mapping[str, Any]] = []
    for job in _state_jobs(state):
        stage = str(job.get("stage") or job.get("job_type") or "")
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if _is_source_cycle_download_stage(stage) and status in TERMINAL_PIPELINE_SUCCESS_STATUSES:
            matches.append(job)
    if not matches:
        return None
    return max(matches, key=_job_terminal_sort_key)


def _latest_failed_job_for_stage(state: Mapping[str, Any], stage_name: str) -> Mapping[str, Any] | None:
    normalized_stage = _canonical_downstream_stage(stage_name) or stage_name
    matches: list[Mapping[str, Any]] = []
    for job in _state_jobs(state):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        stage = str(job.get("stage") or job.get("job_type") or "")
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if stage in {normalized_stage, stage_name} and status in FAILED_PIPELINE_STATUSES:
            matches.append(job)
    if not matches:
        return None
    return max(matches, key=_job_terminal_sort_key)


def _job_terminal_sort_key(job: Mapping[str, Any]) -> tuple[int, datetime]:
    value = _job_terminal_time(job)
    if value is None:
        return (0, datetime.min.replace(tzinfo=UTC))
    return (1, value)


def _job_terminal_time(job: Mapping[str, Any]) -> datetime | None:
    return _first_state_datetime(job, "finished_at", "updated_at", "submitted_at", "started_at", "created_at")


def _object_manifest_is_missing(candidate: SchedulerCandidateLike, manifest_uri: str) -> bool:
    object_root = candidate.resource_profile.get("object_store_root") or os.getenv("OBJECT_STORE_ROOT")
    if object_root in (None, ""):
        return False
    prefix = str(candidate.resource_profile.get("object_store_prefix") or os.getenv("OBJECT_STORE_PREFIX", ""))
    return not LocalObjectStore(str(object_root), object_store_prefix=prefix).exists(manifest_uri)


def _retry_failure_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    failure = _failure_policy_payload(state)
    failed_stage = _failed_stage(state)
    restart_stage = (
        "forecast" if failed_stage in NATIVE_SHUD_STAGE_ALIASES else _canonical_downstream_stage(failed_stage)
    )
    return {
        **base_evidence,
        "decision": "retry_failed",
        "reason": "retry_failed_candidate",
        "stage": failed_stage,
        "restart_stage": restart_stage,
        "restart_from_stage": restart_stage,
        "task_identity": _state_task_identity(state),
        "failure": failure,
        "retry_policy": {
            "automatic_retry_allowed": failure["retryable"],
            "manual_retry_required": failure["permanent"],
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
        "reuse": {
            "successful_sibling_outputs_reused": bool(state.get("successful_sibling_outputs_reused")),
            "durable_output_reused": _durable_shud_output_exists(state),
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _permanent_failure_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _state_has_failure_signal(state):
        return None
    failure = _failure_policy_payload(state)
    if not failure["permanent"]:
        return None
    return {
        **base_evidence,
        "decision": "permanent_failure",
        "reason": _permanent_reason(state, failure),
        "stage": _failed_stage(state),
        "task_identity": _state_task_identity(state),
        "failure": failure,
        "retry_policy": {
            "automatic_retry_allowed": False,
            "manual_retry_required": True,
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
        "manual_retry_required": True,
        "prior_failure_reason": failure["reason_code"],
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _state_has_failure_signal(state: Mapping[str, Any]) -> bool:
    if _state_has_only_repaired_pipeline_failure_signal(state):
        return False
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if pipeline_status in FAILED_PIPELINE_STATUSES or hydro_status in {"failed", "permanently_failed"}:
        return True
    if (
        pipeline_status in ACTIVE_PIPELINE_STATUSES
        and _state_has_only_unsubmitted_auto_retry_placeholders(state)
        and _failed_stage(state) is not None
        and _state_error_code(state) not in (None, "")
    ):
        return True
    if pipeline_status is not None:
        return False
    if _failed_stage(state) is not None and _state_error_code(state) not in (None, ""):
        return True
    return False


def _pipeline_job_is_repaired_stage_evidence(job: Mapping[str, Any]) -> bool:
    return job.get("repair_status") == "repaired" or job.get("active_blocker") is False


def _state_has_only_repaired_pipeline_failure_signal(state: Mapping[str, Any]) -> bool:
    jobs = _state_jobs(state)
    if not jobs:
        return False
    active_failure_jobs = [
        job
        for job in jobs
        if not _pipeline_job_is_repaired_stage_evidence(job)
        and (
            str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
            in FAILED_PIPELINE_STATUSES
            or job.get("error_code") not in (None, "")
            or job.get("reason_code") not in (None, "")
        )
    ]
    if active_failure_jobs:
        return False
    active_failure_events = [
        event
        for event in _state_events(state)
        if not _event_is_manual_retry_marker(event) and _event_has_failure_signal(event)
    ]
    if active_failure_events:
        return False
    repaired_failure_jobs = [
        job
        for job in jobs
        if _pipeline_job_is_repaired_stage_evidence(job)
        and (
            str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
            in FAILED_PIPELINE_STATUSES
            or job.get("error_code") not in (None, "")
            or job.get("reason_code") not in (None, "")
        )
    ]
    return bool(repaired_failure_jobs)


def _event_has_failure_signal(event: Mapping[str, Any]) -> bool:
    details = event.get("details")
    details_mapping = details if isinstance(details, Mapping) else {}
    status = str(
        event.get("status_to")
        or details_mapping.get("status_to")
        or details_mapping.get("status")
        or details_mapping.get("state")
        or ""
    )
    return status in FAILED_PIPELINE_STATUSES or details_mapping.get("error_code") not in (None, "")


def _cancelled_state_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if pipeline_status != "cancelled" and hydro_status != "cancelled":
        return None
    return {
        **base_evidence,
        "decision": "cancelled_manual_retry_required",
        "reason": "manual_retry_required_after_cancelled",
        "terminal_status": "cancelled",
        "cancelled": True,
        "replacement_submitted": False,
        "manual_retry_required": True,
        "retry_policy": {
            "automatic_retry_allowed": False,
            "manual_retry_required": True,
            "attempt": _state_retry_attempt(state),
            "retry_limit": _state_retry_limit(state),
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _manual_retry_state_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    failure = _failure_policy_payload(state, manual=True)
    manual = _manual_retry_payload(state)
    prior_failure = _prior_failure_reason(state) or failure["reason_code"]
    previous_attempt = _state_retry_attempt(state)
    new_attempt = _manual_retry_new_attempt(state, previous_attempt=previous_attempt)
    return {
        **base_evidence,
        "decision": "manual_retry",
        "reason": "manual_retry_requested",
        "manual_retry": {
            **manual,
            "marker": True,
            "allowed": True,
            "previous_attempt": previous_attempt,
            "new_attempt": new_attempt,
        },
        "failure": {
            **failure,
            "prior_failure_reason": prior_failure,
            "previous_attempt": previous_attempt,
            "new_attempt": new_attempt,
        },
        "retry_policy": {
            "automatic_retry_allowed": False,
            "manual_retry_required": False,
            "manual_retry_marker": True,
            "attempt": new_attempt,
            "previous_attempt": previous_attempt,
            "new_attempt": new_attempt,
            "retry_limit": failure["retry_limit"],
        },
        "prior_failure_reason": prior_failure,
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _manual_retry_new_attempt(state: Mapping[str, Any], *, previous_attempt: int) -> int:
    manual = _manual_retry_payload(state)
    for key in ("new_attempt", "retry_count"):
        value = manual.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=previous_attempt + 1)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        if event.get("event_type") not in {"retry", "manual_retry"}:
            continue
        if details.get("trigger") != "manual" and details.get("manual_retry_marker") is not True:
            continue
        value = details.get("retry_count")
        if value not in (None, ""):
            return _coerce_int(value, default=previous_attempt + 1)
    return previous_attempt + 1


def _redact_secret_manifest_for_evidence(value: Any, path: str = "manifest") -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            field_path = f"{path}.{key_text}"
            if secret_manifest_key_reason(key_text) is not None:
                continue
            redacted[key_text] = _redact_secret_manifest_for_evidence(nested, field_path)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_secret_manifest_for_evidence(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, str) and secret_manifest_value_reason(value) is not None:
        return "[redacted]"
    return value


def _candidate_production_identity(candidate: SchedulerCandidateLike) -> dict[str, Any]:
    identity = {
        "run_id": candidate.run_id,
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "source": candidate.source_id,
        "source_id": candidate.source_id,
        "cycle_time": _format_utc(candidate.cycle_time_utc),
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "canonical_product_id": _candidate_canonical_product_id(candidate),
        "forcing_version_id": candidate.forcing_version_id,
        "hydro_run_id": candidate.run_id,
        "published_manifest_id": _candidate_published_manifest_id(candidate),
    }
    pipeline_job_id = _candidate_contract_pipeline_job_id(candidate)
    if pipeline_job_id not in (None, ""):
        identity["pipeline_job_id"] = pipeline_job_id
    return identity


def _candidate_canonical_product_id(candidate: SchedulerCandidateLike) -> str:
    explicit = candidate.resource_profile.get("canonical_product_id")
    if explicit not in (None, ""):
        return str(explicit)
    return f"canon_{candidate.source_id.lower()}_{format_cycle_time(candidate.cycle_time_utc)}"


def _candidate_published_manifest_id(candidate: SchedulerCandidateLike) -> str:
    explicit = candidate.resource_profile.get("published_manifest_id")
    if explicit not in (None, ""):
        return str(explicit)
    return f"manifest_{candidate.run_id}"


def _candidate_contract_pipeline_job_id(candidate: SchedulerCandidateLike) -> str | None:
    explicit = candidate.resource_profile.get("pipeline_job_id")
    if explicit not in (None, ""):
        return str(explicit)
    return None


def _evidence_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_utc(value)
    if isinstance(value, Mapping):
        manifest_redacted = _redact_secret_manifest_for_evidence(
            {str(key): _evidence_safe(nested) for key, nested in value.items()},
            "evidence",
        )
        return redact_payload(manifest_redacted)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_evidence_safe(item) for item in value]
    if isinstance(value, str):
        return redact_payload(value)
    return value


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")
