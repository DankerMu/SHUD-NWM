from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from services.orchestrator.scheduler_state_common import (
    _first_nonempty,
    _first_state_datetime,
)
from services.orchestrator.scheduler_state_failure import (
    _state_has_failure_signal,
    _state_has_only_repaired_pipeline_failure_signal,
)
from services.orchestrator.scheduler_state_manual_retry import _event_is_manual_retry_marker
from services.orchestrator.scheduler_state_rows import (
    _bounded_task_result_rows,
    _is_source_cycle_download_stage,
    _legacy_identity_values,
    _nested_state_identity_payloads,
    _pipeline_job_is_repaired_stage_evidence,
    _stage_cycle_run_matches_candidate,
    _state_events,
    _state_jobs,
    _state_row_has_authoritative_candidate_proof,
    _state_status,
    _state_task_payload_failed,
    _state_values_are_scoped_to_other_candidate,
    _state_values_have_complete_m23_identity,
)
from services.orchestrator.scheduler_state_types import (
    CANDIDATE_STATE_TASK_RESULT_LIMIT,
    FAILED_PIPELINE_STATUSES,
    TERMINAL_PIPELINE_SUCCESS_STATUSES,
    SchedulerCandidateLike,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time


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
