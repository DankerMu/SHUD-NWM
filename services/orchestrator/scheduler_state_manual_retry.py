from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.orchestrator.retry_identity import effective_retry_attempt
from services.orchestrator.scheduler_state_common import (
    _coerce_int,
    _coerce_mapping_for_state,
    _evidence_safe,
    _first_nonempty,
    _first_state_datetime,
    _first_state_int,
)
from services.orchestrator.scheduler_state_rows import (
    _canonical_downstream_stage,
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

# Mirrors ``file_orchestration_journal._ACCEPTED_SUBMIT_MASTER_JOB_ID_RE``: cycle-scope
# (cohort master) job ids are persisted as ``job_cycle_<source>_<stamp>_<suffix>``.
_CYCLE_SCOPE_JOB_ID_RE = re.compile(r"^job_cycle_([^_]+)_(\d{10})_.+$")
# The candidate's own run id: ``fcst_<source>_<stamp>_<model_id>``.  Both patterns capture
# ``(source, stamp)``, so a marker entity id's cycle can be compared with the candidate's own.
_CANDIDATE_RUN_ID_RE = re.compile(r"^fcst_([^_]+)_(\d{10})_")


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
        if not _event_is_adopted_manual_retry_marker(state, event):
            continue
        details = event.get("details")
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

def _manual_retry_marker_shape(event: Mapping[str, Any]) -> bool:
    """Marker-shape test only: no candidate attribution, no scope semantics."""
    details = event.get("details")
    if event.get("event_type") not in {"retry", "manual_retry"} or not isinstance(details, Mapping):
        return False
    return details.get("trigger") == "manual" or details.get("manual_retry_marker") is True

def _candidate_model_ids(state: Mapping[str, Any]) -> set[str]:
    """Model ids derived from the candidate state's job rows (empty set closes the escape)."""
    return {str(job.get("model_id")) for job in _state_jobs(state) if job.get("model_id") not in (None, "")}

def _event_model_attribution(event: Mapping[str, Any]) -> Any | None:
    details = event.get("details")
    if isinstance(details, Mapping) and details.get("model_id") not in (None, ""):
        return details.get("model_id")
    model_id = event.get("model_id")
    return model_id if model_id not in (None, "") else None

def _manual_retry_marker_is_candidate_attributed(state: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """Knife 1: cycle-granularity markers need explicit model attribution; all other markers pass."""
    if event.get("entity_type") != "forecast_cycle":
        return True
    model_id = _event_model_attribution(event)
    if model_id is None:
        return False
    return str(model_id) in _candidate_model_ids(state)

def _event_is_adopted_manual_retry_marker(state: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    return _manual_retry_marker_shape(event) and _manual_retry_marker_is_candidate_attributed(state, event)

def _job_is_cycle_scope_row(job: Mapping[str, Any]) -> bool:
    """Row-level cycle-scope test, shared by the marker predicate and the pin rule.

    Mirrors the journal's strict ``_is_model_less_cycle_scope_job`` semantics: cycle
    scope requires an empty ``model_id`` AND the ``cycle_<source>_<stamp>[_suffix]``
    run-id grammar.  ``model_id`` alone is not sufficient — model-less rows can also
    carry the candidate's OWN ``fcst_...`` run id, and treating those as cycle scope
    would silently discard the operator-pinned attempt number.  Scheduler state
    carries no source/cycle context, so the grammar is checked by prefix.
    """
    if job.get("model_id") not in (None, ""):
        return False
    return str(job.get("run_id") or "").startswith("cycle_")

def _event_entity_job_row(state: Mapping[str, Any], event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    entity_id = event.get("entity_id")
    if entity_id in (None, ""):
        return None
    entity_id_text = str(entity_id)
    for job in _state_jobs(state):
        if str(job.get("job_id") or job.get("pipeline_job_id") or "") == entity_id_text:
            return job
    return None

def _unresolvable_marker_entity_pins_attempt(state: Mapping[str, Any], entity_id: Any) -> bool:
    """Pin gate for markers whose entity resolves to no job row.

    The decision state deletes non-authoritative cohort master rows (see
    ``scheduler_state_identity_filter._candidate_state_decision_state``), and the repository
    projection truncates jobs and events independently, so a marker routinely outlives its
    target row on the path production decides on.  Row absence therefore proves nothing by
    itself, and the id has to carry the evidence instead.

    Non-cycle-grammar ids keep the historical fail-open (synthetic and compacted states depend
    on it).  Cycle-grammar ids pin only with evidence equivalent to the resolved-row rule: the
    id's cycle must be the candidate's own (foreign-cycle rows are unreachable through both
    read paths, but sanitized/legacy states can still carry foreign markers), and the id's
    stage must be the repair target (suffix match on ``failed_stage``; with no ``failed_stage``,
    the "only failure left" arm applies, mirroring ``_cycle_scope_marker_pins_attempt``).
    """
    match = _CYCLE_SCOPE_JOB_ID_RE.fullmatch(str(entity_id)) if entity_id not in (None, "") else None
    if match is None:
        return True
    run_match = _CANDIDATE_RUN_ID_RE.match(str(state.get("run_id") or ""))
    if run_match is None or run_match.groups() != match.groups():
        return False
    failed_stage = state.get("failed_stage")
    if failed_stage not in (None, ""):
        return str(entity_id).endswith(f"_{failed_stage}")
    return not _state_has_candidate_scope_failed_job(state)

def _job_status_text(job: Mapping[str, Any]) -> str:
    return str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")

def _state_has_candidate_scope_failed_job(state: Mapping[str, Any]) -> bool:
    """True when the candidate's own scope still carries a LIVE failure of any kind.

    "Live" reuses the STATUS vocabulary of this module's blocker predicates — their failure
    half, read off the predicates themselves rather than restated, so the two status sets
    cannot drift.  The SOURCES scanned here are deliberately narrower than the blocker scan's:
    only job rows and the candidate's own hydro run.  State-level ``pipeline_status`` and
    pipeline events are blocker sources but are excluded here, because a top-level
    ``pipeline_status: "failed"`` records the cycle failure this rule is being asked about, not
    a candidate-scope one; counting it would make arm 2 unreachable (the positive guard pins
    exactly that arm).  Within the job/hydro sources:

    * a non-cycle-scope job row whose status blocks a manual retry without being ACTIVE —
      ``FAILED_PIPELINE_STATUSES`` plus ``cancelled``, because a cancelled job is a
      first-class manual-retry source (``retry.MANUAL_RETRY_SOURCE_STATUSES``) and therefore
      a live repair target; and
    * a hydro run in a blocking non-ACTIVE status (``failed`` / ``cancelled`` /
      ``permanently_failed``), checked at state level because a hydro run is not a job row.

    The ACTIVE half of both blocker domains is excluded on purpose: work still in flight is
    not a repair target, and counting it would make every queued candidate look like it had
    a failure of its own.

    Two shapes keep a literal failure status while carrying no blocking force anywhere else
    in this module — a repaired stage-evidence row and an unsubmitted auto-retry placeholder
    — so neither counts here either.  (Both exclusions apply to every row regardless of
    status; the placeholder predicate is itself gated to ``pending``/``submission_failed``,
    exactly as the blocker scan applies it, so a cancelled row of placeholder shape blocks
    there and counts here.)  Counting an excluded row would make the cycle failure look like
    "not the only failure left" and silently discard the operator-pinned attempt number.
    """
    for job in _state_jobs(state):
        if _job_is_cycle_scope_row(job):
            continue
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        if _job_is_unsubmitted_auto_retry_placeholder(job):
            continue
        status = _job_status_text(job)
        if _manual_retry_blocking_pipeline_status(status) and status not in ACTIVE_PIPELINE_STATUSES:
            return True
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    return _manual_retry_blocking_hydro_status(hydro_status) and hydro_status not in ACTIVE_HYDRO_STATUSES

def _cycle_scope_marker_pins_attempt(state: Mapping[str, Any], job: Mapping[str, Any]) -> bool:
    """Whether a cycle-scope job's counter belongs to the attempt this decision derives.

    The derived attempt and the scope it is spent against must share an origin: a
    cycle-scope counter may only be pinned when the cycle stage's failure IS what this
    decision repairs.  That holds when ``failed_stage`` names the marker job's own stage
    (explicit same-stage repair), or when the cycle failure is the only failure left in
    the state (nothing else could be the repair target).  A job that is no longer a LIVE
    failure — resolved, repaired stage evidence whose failed status is historical, or an
    unsubmitted auto-retry placeholder — is a stale marker target and pins nothing, even when
    ``failed_stage`` names its stage.

    The two sides of this rule do NOT share one status domain.  This marker-target test keeps
    the narrower bare ``FAILED_PIPELINE_STATUSES`` set on purpose: #1287 widened only the
    candidate-scope side (``_state_has_candidate_scope_failed_job``, the failure half of the
    blocker vocabulary, i.e. ``cancelled`` rows and failed hydro runs too), and whether a
    ``cancelled`` cohort master row should also be a valid marker target is out of scope there
    and tracked separately by #1294.
    """
    if (
        _pipeline_job_is_repaired_stage_evidence(job)
        or _job_is_unsubmitted_auto_retry_placeholder(job)
        or _job_status_text(job) not in FAILED_PIPELINE_STATUSES
    ):
        return False
    failed_stage = state.get("failed_stage")
    if failed_stage not in (None, "") and str(failed_stage) == str(job.get("stage") or ""):
        return True
    return not _state_has_candidate_scope_failed_job(state)

def _marker_event_pins_attempt(state: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """Attempt-derivation gate for one adopted marker: cycle-scope rows need the pin rule."""
    job = _event_entity_job_row(state, event)
    if job is None:
        return _unresolvable_marker_entity_pins_attempt(state, event.get("entity_id"))
    if not _job_is_cycle_scope_row(job):
        return True
    return _cycle_scope_marker_pins_attempt(state, job)

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
    job_status_by_id = _pipeline_job_statuses_by_id(state)
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
        if status in ACTIVE_PIPELINE_STATUSES and _pipeline_event_active_status_shadowed_by_job(
            event,
            details_mapping,
            job_status_by_id=job_status_by_id,
        ):
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

def _pipeline_job_statuses_by_id(state: Mapping[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for job in _state_jobs(state):
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if not status:
            continue
        for key in ("job_id", "pipeline_job_id"):
            job_id = job.get(key)
            if job_id not in (None, ""):
                statuses[str(job_id)] = status
    return statuses

def _pipeline_event_active_status_shadowed_by_job(
    event: Mapping[str, Any],
    details: Mapping[str, Any],
    *,
    job_status_by_id: Mapping[str, str],
) -> bool:
    for key in ("entity_id", "job_id", "pipeline_job_id"):
        value = event.get(key)
        if value not in (None, ""):
            status = job_status_by_id.get(str(value))
            if status not in (None, ""):
                return status not in ACTIVE_PIPELINE_STATUSES
        detail_value = details.get(key)
        if detail_value not in (None, ""):
            status = job_status_by_id.get(str(detail_value))
            if status not in (None, ""):
                return status not in ACTIVE_PIPELINE_STATUSES
    return False

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
    """Blocker-scan exclusion: marker-shaped events are never blockers, regardless of attribution."""
    return _manual_retry_marker_shape(event)

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
        if not _event_is_adopted_manual_retry_marker(state, event):
            continue
        details = event.get("details")
        payload.setdefault("marker", True)
        payload.setdefault("requested", True)
        if details.get("retry_count") not in (None, "") and _marker_event_pins_attempt(state, event):
            payload.setdefault("new_attempt", _coerce_int(details.get("retry_count"), default=0))
        for key in ("prior_failure_reason", "previous_error", "previous_job_id", "slurm_job_id"):
            value = details.get(key)
            if value not in (None, ""):
                payload.setdefault(key, value)
        break
    return _evidence_safe(payload)

def _candidate_scope_consumed_attempt(state: Mapping[str, Any]) -> int:
    """Highest attempt number the candidate's OWN rows durably prove was already minted.

    The axis here is identity consumption, not live failure: a row proves ``{base}_retry_<n>``
    exists in the journal regardless of what its status is now, so the two live-failure
    exclusions (repaired stage evidence, unsubmitted auto-retry placeholder) deliberately do
    NOT apply — a repaired ``_retry_3`` row still means attempt 3 was spent, and an unsubmitted
    placeholder still holds the reservation that a replay would collide with.  This mirrors
    ``chain._next_retry_attempt_for_stage``, the collision-safe minting rule, which likewise
    scans every row carrying the ``{base}_retry_`` prefix without consulting status.

    Cycle-scope rows are excluded: their counters belong to the cohort's identity, never to
    this candidate's (the same separation ``_state_retry_attempt`` keeps).  Attempt derivation
    is suffix-aware via ``effective_retry_attempt`` because the journal's clean-reservation
    invariant resets master-row ``retry_count`` to 0 — the id suffix is the durable record.
    """

    return max(
        (
            effective_retry_attempt(job.get("job_id"), job.get("retry_count"))
            for job in _state_jobs(state)
            if not _job_is_cycle_scope_row(job)
        ),
        default=0,
    )

def _fallback_previous_attempt(state: Mapping[str, Any], previous_attempt: int) -> int:
    """Clamp the fallback's ``previous_attempt`` from below by the durable consumed record.

    Callers derive ``previous_attempt`` as
    ``_state_retry_attempt(state, stage=_failed_stage(state))``.  That composition is
    suffix-aware only while ``_failed_stage`` resolves a CANONICAL downstream stage; when it
    cannot — the live failure is a ``cancelled`` row or a hydro run, neither of which
    ``_failed_stage`` can see, so it resolves ``None`` or a non-canonical stage such as a
    cohort's ``download`` — ``_state_retry_attempt`` short-circuits to the flat top-level
    ``retry_count``, which the clean-reservation invariant has reset to 0.  The candidate's own
    ``_retry_2`` row would then derive attempt 1 again, re-minting an identity the journal
    already holds; production reads that as a reservation conflict and silently skips the
    submission.  In exactly that unnameable-stage case the durable record decides instead, so a
    candidate that consumed attempt N derives at least N + 1.

    When the stage IS nameable the caller's value already carries the stage's suffix record, so
    this returns it untouched: no cross-stage row may inflate a budget that resolves correctly.
    ``_failed_stage`` is imported lazily because ``scheduler_state_failure`` imports this
    module; restating its resolution here is what let the two sides drift in the first place.
    """

    from services.orchestrator.scheduler_state_failure import _failed_stage

    if _canonical_downstream_stage(_failed_stage(state)) is not None:
        return previous_attempt
    return max(previous_attempt, _candidate_scope_consumed_attempt(state))

def _manual_retry_new_attempt(state: Mapping[str, Any], *, previous_attempt: int) -> int:
    manual = _manual_retry_payload(state)
    for key in ("new_attempt", "retry_count"):
        value = manual.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=_fallback_previous_attempt(state, previous_attempt) + 1)
    # The newest retry_count-carrying adopted marker decides.  (The payload scan breaks
    # at the newest adopted marker regardless of retry_count; here markers without a
    # retry_count carry no attempt claim, so the scan keeps walking back past them.)
    # A non-pinning hit is TERMINAL: falling through to an older own-model marker would
    # replay an attempt number already consumed.
    for event in reversed(_state_events(state)):
        if not _event_is_adopted_manual_retry_marker(state, event):
            continue
        details = event.get("details")
        value = details.get("retry_count")
        if value in (None, ""):
            continue
        if not _marker_event_pins_attempt(state, event):
            return _fallback_previous_attempt(state, previous_attempt) + 1
        return _coerce_int(value, default=_fallback_previous_attempt(state, previous_attempt) + 1)
    return _fallback_previous_attempt(state, previous_attempt) + 1
