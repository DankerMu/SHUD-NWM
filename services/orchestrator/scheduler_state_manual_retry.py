from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.orchestrator.scheduler_state_common import (
    _coerce_int,
    _coerce_mapping_for_state,
    _evidence_safe,
    _first_nonempty,
    _first_state_datetime,
    _first_state_int,
)
from services.orchestrator.scheduler_state_rows import (
    _job_is_unsubmitted_auto_retry_placeholder,
    _pipeline_job_is_repaired_stage_evidence,
    _state_events,
    _state_has_only_unsubmitted_auto_retry_placeholders,
    _state_jobs,
    _state_retry_attempt,
    _state_status,
)
from services.orchestrator.scheduler_state_types import (
    ACTIVE_HYDRO_STATUSES,
    ACTIVE_PIPELINE_STATUSES,
    FAILED_PIPELINE_STATUSES,
)


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
