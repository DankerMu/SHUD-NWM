"""Newest-truth guard for the completed-type terminal skips (#2401).

``terminal_hydro_success`` already refuses to classify a candidate as terminal
when its latest failure truth is newer than the ``hydro_run`` truth
(``_terminal_hydro_truth_supersedes_failure``).  ``terminal_pipeline_success``
and ``terminal_completed_cycle`` apply the same rule here, reusing the same
failure-row selection as ``_latest_failure_truth_timestamp`` (repaired-stage
evidence and manual-retry markers excluded) with ONE refinement: a permanence
mark is not a new failure (``_real_failure_truth_timestamp``).  So there is
exactly one comparator shape:

* success ``>=`` failure (ties included) -> terminal, as before;
* failure strictly newer -> not terminal, the candidate reaches the budgeted
  failure path;
* success without any timestamp -> terminal, exactly the pre-change decision.

The same rule gates the one rung below the removed skip that reads the same
frozen ``hydro_run`` truth: a durable-SHUD downstream resume that would restart
``forecast`` itself (``_forecast_resume_reads_superseded_hydro_truth``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from services.orchestrator.scheduler_state_common import _first_state_datetime
from services.orchestrator.scheduler_state_identity_filter import (
    _candidate_identity_from_candidate,
    _pipeline_success_job_is_completion_stage,
    _shared_cycle_row_is_candidate_scoped,
    _task_result_is_candidate_scoped,
)
from services.orchestrator.scheduler_state_manual_retry import _event_is_manual_retry_marker
from services.orchestrator.scheduler_state_rows import (
    _bounded_task_result_rows,
    _pipeline_job_is_repaired_stage_evidence,
    _state_events,
    _state_jobs,
)
from services.orchestrator.scheduler_state_types import (
    DOWNSTREAM_STAGE_ALIASES,
    FAILED_PIPELINE_STATUSES,
    TERMINAL_PIPELINE_COMPLETION_STAGES,
    TERMINAL_PIPELINE_SUCCESS_STATUSES,
    SchedulerCandidateLike,
    cohort_member_row_is_attributed,
)

# Same key order ``_latest_failure_truth_timestamp`` reads for each row type.
_JOB_TIME_KEYS = ("updated_at", "finished_at", "submitted_at", "created_at")
_EVENT_TIME_KEYS = ("created_at", "updated_at", "finished_at", "submitted_at")
# A permanence mark rewrites BOTH ``updated_at`` and ``finished_at`` to the mark
# time: ``mark_pipeline_job_permanently_failed`` stamps ``updated_at=now`` and
# takes ``finished_at`` from its caller, and both production callers
# (``FileJournalRetryService._mark_master_permanently_failed`` and the
# non-master ``mark_permanently_failed`` leg) pass ``finished_at=_utcnow()``.
# Only the submission-side times still describe the failed attempt.
_PERMANENT_JOB_TIME_KEYS = ("submitted_at", "created_at")
_PERMANENCE_MARK_EVENT_TYPE = "permanently_failed"


def _real_failure_truth_timestamp(state: Mapping[str, Any]) -> datetime | None:
    """``_latest_failure_truth_timestamp`` minus the permanence re-label.

    Marking an old failure ``permanently_failed`` (a declined retry) records no
    new failure: the mark event (``event_type == "permanently_failed"``) is
    skipped, and a ``permanently_failed`` job row contributes its submission
    time, never the mark-rewritten ``updated_at`` / ``finished_at``.  Every other
    failed row is read exactly as the hydro leg's primitive reads it.
    """

    timestamps: list[datetime] = []
    for job in _state_jobs(state):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if status not in FAILED_PIPELINE_STATUSES and not job.get("error_code"):
            continue
        keys = _PERMANENT_JOB_TIME_KEYS if status == "permanently_failed" else _JOB_TIME_KEYS
        timestamp = _first_state_datetime(job, *keys)
        if timestamp is not None:
            timestamps.append(timestamp)
    for event in _state_events(state):
        if _event_is_manual_retry_marker(event) or event.get("event_type") == _PERMANENCE_MARK_EVENT_TYPE:
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
        timestamp = _first_state_datetime(event, *_EVENT_TIME_KEYS)
        if timestamp is not None:
            timestamps.append(timestamp)
    return max(timestamps) if timestamps else None


def _success_truth_supersedes_failure(state: Mapping[str, Any], success_time: datetime | None) -> bool:
    if success_time is None:
        return True
    failure_time = _real_failure_truth_timestamp(state)
    return failure_time is None or success_time >= failure_time


def _pipeline_success_truth_timestamp(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
) -> datetime | None:
    """Newest time among the rows ``_pipeline_terminal_success_is_candidate_scoped`` accepts."""

    timestamps: list[datetime] = []
    for job in _state_jobs(state):
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if status not in TERMINAL_PIPELINE_SUCCESS_STATUSES or not _pipeline_success_job_is_completion_stage(job):
            continue
        if (
            str(job.get("run_id") or "") != candidate.run_id
            and str(job.get("model_id") or "") != candidate.model_id
            and not cohort_member_row_is_attributed(job)
        ):
            continue
        timestamp = _first_state_datetime(job, *_JOB_TIME_KEYS)
        if timestamp is not None:
            timestamps.append(timestamp)
    expected = _candidate_identity_from_candidate(candidate)
    for event in _state_events(state):
        if _success_event_is_candidate_scoped(expected, event):
            timestamp = _first_state_datetime(event, *_EVENT_TIME_KEYS)
            if timestamp is not None:
                timestamps.append(timestamp)
    return max(timestamps) if timestamps else None


def _success_event_is_candidate_scoped(expected: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    # Mirrors ``_pipeline_terminal_success_event_is_candidate_scoped`` for ONE event.
    details = event.get("details")
    details_mapping = details if isinstance(details, Mapping) else {}
    status = str(
        event.get("status_to")
        or details_mapping.get("status_to")
        or details_mapping.get("status")
        or details_mapping.get("state")
        or ""
    )
    if status not in TERMINAL_PIPELINE_SUCCESS_STATUSES:
        return False
    stage = str(event.get("stage") or details_mapping.get("stage") or details_mapping.get("job_type") or "")
    if DOWNSTREAM_STAGE_ALIASES.get(stage, stage) not in TERMINAL_PIPELINE_COMPLETION_STAGES:
        return False
    task_results = _bounded_task_result_rows(details_mapping)
    if task_results:
        return any(
            str(task.get("status") or task.get("state") or "") in TERMINAL_PIPELINE_SUCCESS_STATUSES
            and task.get("error_code") in (None, "")
            and _task_result_is_candidate_scoped(expected, task)
            for task in task_results
        )
    return _shared_cycle_row_is_candidate_scoped(expected, event) or _shared_cycle_row_is_candidate_scoped(
        expected,
        details_mapping,
    )


def _pipeline_terminal_success_supersedes_failure(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
) -> bool:
    return _success_truth_supersedes_failure(state, _pipeline_success_truth_timestamp(candidate, state))


def _completed_cycle_terminal_supersedes_failure(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    terminal_row: Mapping[str, Any] | None,
) -> bool:
    """``terminal_row`` is the candidate-matched row ``_completed_cycle_terminal_evidence`` binds.

    Its own time is the success truth.  The cycle-level ``forecast_cycle``
    timestamp is deliberately NOT used: it is shared by every model of the
    cycle, so a sibling's later completion would mask this model's failure.
    When the bound row carries no time (for example an event ``details``
    mapping), the candidate-scoped pipeline success time is the fallback.
    """

    success_time = None
    if terminal_row is not None:
        success_time = _first_state_datetime(terminal_row, *_JOB_TIME_KEYS)
    if success_time is None:
        success_time = _pipeline_success_truth_timestamp(candidate, state)
    return _success_truth_supersedes_failure(state, success_time)


def _forecast_resume_reads_superseded_hydro_truth(
    state: Mapping[str, Any],
    downstream_retry: Mapping[str, Any] | None,
) -> bool:
    """True when a ``restart_stage == forecast`` durable-SHUD resume rests on an older ``hydro_run``.

    "Resume downstream reusing durable SHUD output" while restarting the SHUD
    stage itself is only coherent when the durable truth is not older than the
    forecast failure.  A strictly newer failure means that hydro truth belongs
    to a superseded run (for example the first run of a failed same-run_id
    rerun), so the candidate must take the budgeted failure path instead.
    Same ``hydro_run`` timestamp keys as ``_terminal_hydro_truth_supersedes_failure``
    and the same failure time as the terminal legs; a
    ``hydro_run`` without a timestamp, or a resume of any later stage, keeps
    the pre-change decision.
    """

    if downstream_retry is None or downstream_retry.get("restart_stage") != "forecast":
        return False
    hydro_run = state.get("hydro_run")
    if not isinstance(hydro_run, Mapping):
        return False
    hydro_time = _first_state_datetime(hydro_run, "updated_at", "finished_at", "created_at")
    if hydro_time is None:
        return False
    failure_time = _real_failure_truth_timestamp(state)
    return failure_time is not None and failure_time > hydro_time
