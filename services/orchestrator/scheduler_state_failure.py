from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.orchestrator.retry import classify_failure
from services.orchestrator.scheduler_state_common import (
    _evidence_safe,
    _first_state_datetime,
    _forecast_cycle_manifest_uri,
    _is_raw_manifest_object_uri,
    _object_manifest_is_missing,
)
from services.orchestrator.scheduler_state_manual_retry import (
    _event_is_manual_retry_marker,
    _manual_retry_new_attempt,
    _manual_retry_payload,
)
from services.orchestrator.scheduler_state_rows import (
    _bounded_task_result_rows,
    _event_has_failure_signal,
    _is_source_cycle_download_stage,
    _pipeline_job_is_repaired_stage_evidence,
    _state_events,
    _state_has_only_unsubmitted_auto_retry_placeholders,
    _state_jobs,
    _state_output_uri,
    _state_retry_attempt,
    _state_retry_limit,
    _state_status,
)
from services.orchestrator.scheduler_state_types import (
    ACTIVE_PIPELINE_STATUSES,
    DOWNSTREAM_RESTART_STAGES,
    DOWNSTREAM_STAGE_ALIASES,
    FAILED_PIPELINE_STATUSES,
    NATIVE_SHUD_STAGE_ALIASES,
    TERMINAL_PIPELINE_SUCCESS_STATUSES,
    TRANSIENT_RETRY_REASON_CODES,
    SchedulerCandidateLike,
)


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


def _completed_upstream_stage_retry_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    completed_stage = state.get("completed_stage_evidence")
    if not isinstance(completed_stage, Mapping):
        return None
    restart_stage = _canonical_downstream_stage(
        str(
            state.get("restart_stage")
            or state.get("restart_from_stage")
            or completed_stage.get("restart_stage")
            or completed_stage.get("restart_from_stage")
            or ""
        )
    )
    if restart_stage is None:
        return None
    failure_state = dict(state)
    failure_state.pop("restart_stage", None)
    failure_state.pop("restart_from_stage", None)
    if _state_has_failure_signal(failure_state):
        return None
    return {
        **base_evidence,
        "decision": "retry_after_completed_stage",
        "reason": "resume_after_completed_stage",
        "restart_stage": restart_stage,
        "restart_from_stage": restart_stage,
        "native_shud_resubmitted": restart_stage == "forecast",
        "completed_stage_evidence": _evidence_safe(dict(completed_stage)),
        "retry_policy": {
            "automatic_retry_allowed": True,
            "manual_retry_required": False,
            "attempt": _state_retry_attempt(state),
            "retry_limit": _state_retry_limit(state),
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


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
