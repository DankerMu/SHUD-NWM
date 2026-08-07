from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.orchestrator.retry_identity import split_retry_job_identity
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
    _job_stage_name,
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
    NATIVE_SHUD_STAGE_ALIASES,
)

# Mirrors ``file_orchestration_journal._ACCEPTED_SUBMIT_MASTER_JOB_ID_RE``: cycle-scope
# (cohort master) job ids are persisted as ``job_cycle_<source>_<stamp>_<suffix>``.
_CYCLE_SCOPE_JOB_ID_RE = re.compile(r"^job_cycle_([^_]+)_(\d{10})_.+$")
# The candidate's own run id: ``fcst_<source>_<stamp>_<model_id>``.  Both patterns capture
# ``(source, stamp)``, so a marker entity id's cycle can be compared with the candidate's own.
_CANDIDATE_RUN_ID_RE = re.compile(r"^fcst_([^_]+)_(\d{10})_")
# A hydro run is the candidate's SHUD forecast leg, not a job row, so it has no ``stage`` field to
# read.  Its stage family is the canonical normalization of the module's native-SHUD aliases
# (``scheduler_state_types.NATIVE_SHUD_STAGE_ALIASES``) through ``_canonical_downstream_stage``
# (``scheduler_state_rows.py:417-423``), which keeps ``forecast`` / ``run_shud_forecast`` and drops
# the two aliases that name no downstream restart stage.  Derived rather than restated so an alias
# table change cannot leave a stale literal here.
_HYDRO_RUN_STAGE_FAMILY = frozenset(
    stage for stage in map(_canonical_downstream_stage, NATIVE_SHUD_STAGE_ALIASES) if stage is not None
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

def _loop_stripped_retry_identity(job_id: str) -> str:
    """Strip EVERY stacked ``_retry_<n>`` suffix off a job id.

    ``retry_identity.split_retry_job_identity`` strips ONE layer per call (its ``rsplit``
    takes the last suffix, which is the authoritative attempt), but production ids stack
    them — the node-27 archive carries ``..._state_save_qc_retry_1_retry_2_retry_3`` — so a
    single call still leaves ``_retry_1_retry_2`` glued to the stage token.  The loop stops
    as soon as a call returns its input unchanged, which is what the helper does for an id
    with no parsable trailing suffix, so an unparsable tail (``..._retry_active``) is left in
    place rather than being chewed through.
    """

    base = job_id
    while True:
        stripped, _attempt = split_retry_job_identity(base)
        if stripped == base:
            return base
        base = stripped

def _state_repaired_stage_evidence_names_job(state: Mapping[str, Any], entity_id: Any) -> bool:
    """Row-absent half of the twin's repaired-target refusal: the state-level mapping.

    The twin reads the repaired flags off the marker's target ROW; with the row gone the only
    surviving evidence of the same shape is the state's ``repaired_stage_evidence`` mapping,
    compared by EXACT id.  Suffix-aware comparison is deliberately NOT used: it would make a
    still-failed ``..._retry_3`` target match its repaired ``..._retry_2`` ancestor and refuse
    a pin the twin grants.  This also differs on purpose from
    ``_manual_retry_marker_repairs_historical_failure``'s row-absent read, which keys on the
    marker's ``previous_job_id`` plus a repairing-retry conjunct because it answers a different
    question ("does this marker repair the historical failure", not "is this marker's own
    target already repaired").
    """

    repaired_stage = state.get("repaired_stage_evidence")
    if not isinstance(repaired_stage, Mapping):
        return False
    return str(repaired_stage.get("original_failed_job_id") or "") == str(entity_id)

def _state_completed_stage_evidence_names_job(state: Mapping[str, Any], entity_id: Any) -> bool:
    """Row-absent half of the twin's NON-FAILED-target refusal: the state-level mapping.

    The twin refuses to pin a marker whose target row is no longer a failure (its
    ``FAILED_PIPELINE_STATUSES`` test), reading the status off the ROW.  When the row is gone
    the state's ``completed_stage_evidence`` is the surviving surface of exactly that fact:
    ``chain_repository_state._completed_stage_success_evidence`` (:241-265) builds it only from
    a job in ``TERMINAL_PIPELINE_SUCCESS_STATUSES`` and stamps the winning row's ``job_id`` into
    it, and the mapping is a top-level state field the identity filter does not strip — so it
    outlives the very row deletion that creates the need for it.

    Compared by EXACT id, for the same reason the repaired-evidence sibling is: a suffix-aware
    match would let a still-failed ``..._retry_3`` target inherit the success of its
    ``..._retry_2`` ancestor and refuse a pin the twin grants.

    The OTHER writer of this key copies the repaired mapping into it
    (``chain_repository_state.py:856-862``), and that payload (:222-238) carries no ``job_id``
    key at all — ``mapping.get("job_id")`` is then ``None``, the comparison is against ``""``,
    and a marker entity id can never equal it.  So the copy path produces no false hit and this
    guard only ever tightens.
    """

    completed_stage = state.get("completed_stage_evidence")
    if not isinstance(completed_stage, Mapping):
        return False
    return str(completed_stage.get("job_id") or "") == str(entity_id)

def _unresolvable_marker_stage_is_repair_target(
    event: Mapping[str, Any],
    entity_id: Any,
    failed_stage: Any,
) -> bool:
    """Whether the marker's own recorded stage names the state's failed stage.

    Primary evidence is the marker's ``details.failed_stage`` — written by
    ``file_orchestration_journal.record_manual_repair`` and preserved through the identity
    filter's retry-event carve-out — so the verdict no longer depends on id text at all.
    Markers written before that field existed (legacy and synthesized states) fall back to the
    id token, read only AFTER every stacked ``_retry_<n>`` suffix is stripped; the suffix match
    is kept for the token comparison because stage names carry underscores of their own
    (``state_save_qc``).
    """

    details = event.get("details")
    recorded_stage = details.get("failed_stage") if isinstance(details, Mapping) else None
    if recorded_stage not in (None, ""):
        return str(recorded_stage) == str(failed_stage)
    return _loop_stripped_retry_identity(str(entity_id)).endswith(f"_{failed_stage}")

def _unresolvable_marker_entity_pins_attempt(state: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """Pin gate for markers whose entity resolves to no job row.

    The decision state deletes non-authoritative cohort master rows (see
    ``scheduler_state_identity_filter._candidate_state_decision_state``), and the repository
    projection truncates jobs and events independently, so a marker routinely outlives its
    target row on the path production decides on.  Row absence therefore proves nothing by
    itself, and the MARKER has to carry the evidence instead — its own record first, its id
    text only as the legacy backstop.

    Delivered semantics, in the twin's own order (``_cycle_scope_marker_pins_attempt``):

    * a non-cycle-grammar id keeps the historical fail-open (synthetic and compacted states
      depend on it, and the SQL retry service's ``{run_id}_retry_active`` shape lands here);
    * a cycle-grammar id whose ``(source, stamp)`` is not the candidate's own never pins;
    * staleness first, through the two row-absent surfaces of it: the pin is refused when the
      state's ``repaired_stage_evidence`` names this marker's target as its
      ``original_failed_job_id`` (the target was already repaired), and when the state's
      ``completed_stage_evidence`` names it as its ``job_id`` (the target already SUCCEEDED, so
      it is not a repair target at all);
    * then the stage evidence — ``details.failed_stage`` primary, loop-stripped id token
      backstop — pins when it names the state's ``failed_stage``;
    * anything else (stage mismatch, or a state carrying no ``failed_stage`` at all) falls
      through to ``not _state_has_candidate_scope_failed_job(state)``, the same predicate
      object and therefore the same live-failure domain the twin's arm 2 uses.

    Equivalence with the twin is claimed for FAILED-STATUS targets that are neither unsubmitted
    auto-retry placeholders nor repaired-flagged: on those the two arms ask the same question of
    the same objects and answer identically.  (``cancelled`` is outside that set on purpose —
    the twin's marker-target test keeps the bare ``FAILED_PIPELINE_STATUSES`` vocabulary, #1294.)

    The twin also refuses on evidence that lives on the ROW alone, and only ONE of those shapes
    has a state-level surface that outlives the row: a non-failed target is covered here when
    ``completed_stage_evidence`` names it, by the conjunct above.  The rest still pin here where
    the twin refuses, and that residue is disclosed, not fixed, by this rule (design.md
    Residue 1):

    * an unsubmitted auto-retry placeholder (``pending``/``submission_failed``, ``_retry_<n>``
      id, no slurm id) — a submission-time row shape with no state-level projection at all;
    * a repaired-flagged row (``repair_status``/``active_blocker``) the state's
      ``repaired_stage_evidence`` does NOT name;
    * a non-failed target the completed-stage evidence does NOT name — a later stage's success
      outranked it (``_best_completed_stage_success_evidence`` keeps one winner), the projection
      took the repaired-copy branch (that payload has no ``job_id``), or the state carries no
      such mapping at all.

    The id-token backstop's stage inference on stage-less legacy markers is disclosed with it:
    it reads the loop-stripped id text, not a recorded field.
    """
    entity_id = event.get("entity_id")
    match = _CYCLE_SCOPE_JOB_ID_RE.fullmatch(str(entity_id)) if entity_id not in (None, "") else None
    if match is None:
        return True
    run_match = _CANDIDATE_RUN_ID_RE.match(str(state.get("run_id") or ""))
    if run_match is None or run_match.groups() != match.groups():
        return False
    if _state_repaired_stage_evidence_names_job(state, entity_id):
        return False
    if _state_completed_stage_evidence_names_job(state, entity_id):
        return False
    failed_stage = state.get("failed_stage")
    if failed_stage not in (None, "") and _unresolvable_marker_stage_is_repair_target(
        event, entity_id, failed_stage
    ):
        return True
    return not _state_has_candidate_scope_failed_job(state)

def _job_status_text(job: Mapping[str, Any]) -> str:
    return str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")

def _job_is_live_candidate_scope_failure(job: Mapping[str, Any]) -> bool:
    """Row-level half of the live-failure domain, shared by both of its consumers.

    Extracted so the two questions asked of the same domain cannot drift:
    ``_state_has_candidate_scope_failed_job`` asks "does ANY row still carry a live failure",
    ``_restarted_stage_family`` asks the same of each row in order to read its stage.  The
    semantics — which statuses count, why the ACTIVE half is subtracted, and why the two
    exclusions apply to every row regardless of status — are documented once, on
    ``_state_has_candidate_scope_failed_job`` below.
    """

    if _job_is_cycle_scope_row(job):
        return False
    if _pipeline_job_is_repaired_stage_evidence(job):
        return False
    if _job_is_unsubmitted_auto_retry_placeholder(job):
        return False
    status = _job_status_text(job)
    return _manual_retry_blocking_pipeline_status(status) and status not in ACTIVE_PIPELINE_STATUSES

def _state_hydro_run_is_live_failure(state: Mapping[str, Any]) -> bool:
    """Hydro-leg half of the live-failure domain, shared by both of its consumers.

    A hydro run is not a job row, so it is read at state level off the same field chain the
    blocker scan uses (``_state_status(state, "hydro_status", "hydro_run_status")``), and the
    ACTIVE half of ``_manual_retry_blocking_hydro_status`` is subtracted exactly as it is for job
    rows.
    """

    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    return _manual_retry_blocking_hydro_status(hydro_status) and hydro_status not in ACTIVE_HYDRO_STATUSES

def _state_has_candidate_scope_failed_job(state: Mapping[str, Any]) -> bool:
    """True when the candidate's own scope still carries a LIVE failure of any kind.

    "Live" reuses the STATUS vocabulary of this module's blocker predicates — their failure
    half, read off the predicates themselves rather than restated, so the two status sets
    cannot drift.  The SOURCES this predicate is meant to scan are narrower than the blocker
    scan's: only job rows and the candidate's own hydro run.  State-level ``pipeline_status``
    and pipeline events are blocker sources, but must not be a live-failure source here: a
    top-level ``pipeline_status: "failed"`` records the cycle failure this rule is being asked
    about, not a candidate-scope one, and counting it would close arm 2 (the positive guard
    pins exactly that arm).

    That narrowing is enforced by the projection SHAPE on both production read paths, NOT by
    any filter in this module.  On the journal path a ``pipeline_job`` marker survives event
    filtering only when its ``entity_id`` is among the projected job rows
    (``file_orchestration_journal._event_matches_candidate_rows``), and a row excluded from the
    candidate projection drops its ``pipeline_job`` events in the SAME step (the foreign-model
    filter pair in ``candidate_state``'s ``pipeline_jobs=``/``pipeline_events=`` derivations,
    #1288) — dropping the row half alone would leave an orphaned marker that re-enters the pin
    decision through ``_unresolvable_marker_entity_pins_attempt``; on the DB path the event query returns only
    ``pipeline_job`` events whose ``entity_id`` is selected from the same job predicate, bound
    to the same values, that the jobs query runs (``chain_repository_state.py:548-558`` against
    ``:510-515``).  A marker-bearing production state therefore always carries real job rows,
    ``_state_jobs`` returns those rows, and no synthesized row participates.

    On a synthetic or compacted state with no job rows at all the exclusion does NOT hold:
    ``_state_jobs`` then synthesizes the state itself as a single job row whenever the state
    carries any recognized job field (``scheduler_state_rows.py:400-401``), ``pipeline_status``
    being one of them (``scheduler_state_rows.py:391``), and ``_job_status_text`` reads exactly
    that field (``scheduler_state_manual_retry.py:184``) — so a top-level ``pipeline_status``
    does reach this predicate there.  Hardening this by skipping the id-less synthesized row is
    tracked by #1299.

    Within the job/hydro sources:

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

    The two halves live in ``_job_is_live_candidate_scope_failure`` and
    ``_state_hydro_run_is_live_failure`` above, which ``_restarted_stage_family`` reuses.
    """
    if any(_job_is_live_candidate_scope_failure(job) for job in _state_jobs(state)):
        return True
    return _state_hydro_run_is_live_failure(state)

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
        return _unresolvable_marker_entity_pins_attempt(state, event)
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

def _restarted_stage_family(state: Mapping[str, Any]) -> set[str]:
    """The stages a fallback restart of this candidate would replay: its LIVE failures' stages.

    Membership is decided row by row by ``_job_is_live_candidate_scope_failure``, the same
    predicate ``_state_has_candidate_scope_failed_job`` applies, so a row that predicate excludes
    — cycle scope, repaired stage evidence, unsubmitted auto-retry placeholder, ACTIVE, or a
    status outside the blocker vocabulary — contributes no stage at all.  Stage identity comes
    from ``_job_stage_name`` (``scheduler_state_rows.py:481-486``), i.e. the projection's
    ``stage``/``job_type`` fields, never from job-id substrings: production ids embed several
    stage tokens (``..._convert_model_0_forecast_retry_1_retry_2``).

    A live hydro failure is not a job row and carries no ``stage`` field, so it contributes
    ``_HYDRO_RUN_STAGE_FAMILY`` instead.

    The returned names are the raw projection values; ``_state_retry_attempt(state, stage=...)``
    normalizes them itself (``scheduler_state_rows.py:448``), so aliases of one canonical stage
    answer identically.  An empty set means the candidate has no live failure anywhere in its own
    scope — the stale-marker shape, where there is no restarted stage to floor.
    """

    family: set[str] = set()
    for job in _state_jobs(state):
        if not _job_is_live_candidate_scope_failure(job):
            continue
        stage = _job_stage_name(job)
        if stage is not None:
            family.add(stage)
    if _state_hydro_run_is_live_failure(state):
        family |= _HYDRO_RUN_STAGE_FAMILY
    return family

def _fallback_previous_attempt(state: Mapping[str, Any], previous_attempt: int) -> int:
    """Floor the fallback's ``previous_attempt`` by what the RESTARTED stages already spent.

    Callers derive ``previous_attempt`` as ``_state_retry_attempt(state,
    stage=_failed_stage(state))`` (``scheduler_state_failure.py:1088``).  That composition is
    suffix-aware only while ``_failed_stage`` resolves a CANONICAL downstream stage: with a
    ``None`` or non-canonical stage ``_state_retry_attempt`` never reaches its stage-scoped,
    suffix-aware arm (``scheduler_state_rows.py:449-453``) and answers the flat top-level
    ``retry_count`` instead — which the journal's clean-reservation invariant has reset to 0 —
    falling back to the rows' recorded counts only on a state carrying no
    ``retry_attempt``/``attempt``/``retry_count`` key at all (:450-452, :455-460).
    Two live-failure shapes land there: with no ``failed_stage``/``stage``/``restart_stage`` key
    on the state (``scheduler_state_failure.py:51-54``) the resolver walks job rows and matches
    ``FAILED_PIPELINE_STATUSES`` only (:58-59), so it cannot name a ``cancelled`` row, and it
    reads job rows only, so it cannot see a hydro run at all.  The candidate's own ``_retry_2``
    row would then derive attempt 1 again, re-minting an identity the journal already holds;
    production reads that as a reservation conflict and silently skips the submission.

    In exactly that unnameable-stage case the floor comes from ``_restarted_stage_family`` — the
    stages this restart replays — and each stage's spent attempt is read with the SAME production
    function the nameable case trusts, ``_state_retry_attempt(state, stage=...)``.  Within one
    stage that function is status-blind: it takes the higher of a stage-matching row's recorded
    ``retry_count`` and its ``_retry_<n>`` id suffix (``scheduler_state_rows.py:462-479``), so a
    repaired or already-succeeded ``_retry_3`` row at a family stage still proves attempt 3 was
    spent.  Stages OUTSIDE the family contribute nothing, which is the whole point: the family is
    the scope axis, so another stage's consumed counter — a per-model forcing retry, or a
    single-basin cycle's own ``model_id``-stamped download/convert rows — can no longer be
    charged to this restart's budget (#1293 round 4).

    An empty family is the stale-marker shape (no live failure anywhere in candidate scope);
    there is no restarted stage to floor, so the caller's value is returned as is.

    When the stage IS nameable the caller's value already carries that stage's suffix record, so
    this returns it untouched.  ``_failed_stage`` is imported lazily because
    ``scheduler_state_failure`` imports this module; restating its resolution here is what let
    the two sides drift in the first place.
    """

    from services.orchestrator.scheduler_state_failure import _failed_stage

    if _canonical_downstream_stage(_failed_stage(state)) is not None:
        return previous_attempt
    family_floors = [_state_retry_attempt(state, stage=stage) for stage in _restarted_stage_family(state)]
    return max([previous_attempt, *family_floors])

def _manual_retry_new_attempt(state: Mapping[str, Any], *, previous_attempt: int) -> int:
    manual = _manual_retry_payload(state)
    for key in ("new_attempt", "retry_count"):
        value = manual.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=_fallback_previous_attempt(state, previous_attempt) + 1)
    # The newest ADOPTED marker decides, and the scan is terminal there -- the same
    # termination rule ``_manual_retry_payload`` applies with its unconditional break, so the
    # two scanners cannot report different markers for one event list (#1289).  An absent or
    # empty retry_count is no operator attempt claim, so that marker yields the fallback.
    # A non-pinning hit of either kind is TERMINAL: falling through to an older own-model
    # marker would replay an attempt number already consumed.  An UNADOPTED marker-shaped
    # event neither decides nor terminates.
    for event in reversed(_state_events(state)):
        if not _event_is_adopted_manual_retry_marker(state, event):
            continue
        details = event.get("details")
        value = details.get("retry_count")
        if value in (None, ""):
            return _fallback_previous_attempt(state, previous_attempt) + 1
        if not _marker_event_pins_attempt(state, event):
            return _fallback_previous_attempt(state, previous_attempt) + 1
        return _coerce_int(value, default=_fallback_previous_attempt(state, previous_attempt) + 1)
    return _fallback_previous_attempt(state, previous_attempt) + 1
