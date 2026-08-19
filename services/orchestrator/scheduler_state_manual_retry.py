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
    # Re-exported under this module's name: the predicate sank to the rows module (the
    # import floor) so the attempt derivation and the candidate-scoped failed-stage
    # resolver can share it, and importers of this module keep working unchanged.
    _job_is_cycle_scope_row,
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
# The candidate's own run id: ``fcst_<source>_<stamp>_<model_id>``.  The first two groups are the
# ``(source, stamp)`` pair the cycle pattern above captures too, so a marker entity id's cycle can
# be compared with the candidate's own.  The third group is the model id and takes the WHOLE tail:
# production model ids carry underscores of their own (``model_a``), so ``[^_]+`` would truncate
# them into a comparison that silently never matches.  The tail is allowed to be empty so the
# ``(source, stamp)`` half keeps matching exactly the run ids it matched before the group existed;
# an empty tail names no model and fails the model comparison closed.
_CANDIDATE_RUN_ID_RE = re.compile(r"^fcst_([^_]+)_(\d{10})_(.*)")
#: The marker's OWN write-time record of the row it repairs: ``details`` key -> target row field.
#: Written by ``file_orchestration_journal.record_manual_repair``, passed through by the identity
#: filter's retry-event carve-out, and read back here to rebuild the target when its row is gone
#: from the decision state.  One tuple rather than three mirrored literals, so the writer, the
#: sanitizer whitelist and this consumer cannot drift apart (#1308).
#:
#: CLOSURE INVARIANT: this key set must close over EVERY row field read by the transitive closure
#: of ``_job_row_is_live_failure`` — its own status chain, plus
#: ``_pipeline_job_is_repaired_stage_evidence`` (``repair_status``/``active_blocker``), plus
#: ``_job_is_unsubmitted_auto_retry_placeholder`` (``status``, ``manual_retry_marker``,
#: ``slurm_job_id``, ``array_task_id``, ``retry_count``, ``job_id``) — plus the ``model_id`` the
#: router needs to route.  ``job_id`` is the marker's own ``entity_id`` and is not recorded twice.
#: A predicate that starts reading a new row field MUST gain a key here, or the reconstruction
#: stops being the row it claims to be (anchored by the residue matrix's anti-drift assertions).
#:
#: Two of the eight are GATE CONTRACT keys the current writer cannot fill:
#: ``repair_status``/``active_blocker`` are projection-time annotations applied to a row COPY
#: (``chain_repository_state._annotate_repaired_pipeline_jobs``), and the journal's closed row
#: constructor (``file_orchestration_journal._pipeline_job_row``) has no such fields, so the
#: persisted row ``record_manual_repair`` reads never carries them.  They stay in the tuple
#: because the closure invariant is what makes the reconstruction a faithful row and because a
#: record that DOES carry them must be honoured; a target already annotated repaired at write
#: time therefore still pins here where the row-present twin refuses — a disclosed permanent
#: limitation, with the write-face fix routed to #1482.
MARKER_TARGET_ROW_DETAIL_FIELDS = (
    ("target_status", "status"),
    # Gate-contract keys: see the note above — the current write face never emits these two.
    ("target_repair_status", "repair_status"),
    ("target_active_blocker", "active_blocker"),
    ("target_model_id", "model_id"),
    ("target_slurm_job_id", "slurm_job_id"),
    ("target_retry_count", "retry_count"),
    ("target_manual_retry_marker", "manual_retry_marker"),
    ("target_array_task_id", "array_task_id"),
)
MARKER_TARGET_ROW_DETAIL_KEYS = tuple(detail_key for detail_key, _row_field in MARKER_TARGET_ROW_DETAIL_FIELDS)
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

    The twin refuses to pin a marker whose target row is no longer a live failure (its
    ``_job_row_is_live_failure`` test), reading the status off the ROW.  When the row is gone
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

def _marker_record_target_row(event: Mapping[str, Any], entity_id: Any) -> dict[str, Any] | None:
    """Rebuild the marker's target row from the marker's own write-time record, or ``None``.

    The reconstruction is complete only when the record is: ``target_status`` AND ``failed_stage``
    must both be present, because those are the two fields the resolved-row routing cannot answer
    without (the live-failure test and the repair-target stage test).  A HALF record — the shape
    the current writer still produces when the target row carries no stage, since an empty value is
    neither written nor passed through by the sanitizer — is not decided on here at all: it falls
    back to the legacy arm, id-token backstop included, so nothing about that arm degrades.

    Every other ``target_*`` key is optional in exactly the way the row field it mirrors is: the
    writer omits ``None``/``""`` and the sanitizer strips them, so a missing key means the row
    carried no value there, which is what the predicates' own defaults already mean.  ``0`` and
    ``False`` are recorded VALUES, not absences, on both sides of that rule.

    ``job_id`` is the marker's ``entity_id`` — the id the placeholder predicate reads for its
    ``_retry_<n>`` test — and ``stage`` is the recorded ``failed_stage``.  ``stage`` as a dict key
    exists only in this in-memory row; the RECORD deliberately never uses that key name (the
    candidate-state record-stage reader consumes ``details.stage``).  No ``run_id`` is
    reconstructed: ``_job_is_cycle_scope_row`` decides cycle scope from ``run_id`` + empty
    ``model_id``, but this row got here through the ``_CYCLE_SCOPE_JOB_ID_RE`` entity-id grammar,
    which IS the cycle-scope proof, and the caller reads model-ness off ``target_model_id``
    directly — so the two predicates map onto each other without a synthesized run id.
    """

    details = event.get("details")
    if not isinstance(details, Mapping):
        return None
    if details.get("target_status") in (None, "") or details.get("failed_stage") in (None, ""):
        return None
    row: dict[str, Any] = {"job_id": entity_id, "stage": details.get("failed_stage")}
    for detail_key, row_field in MARKER_TARGET_ROW_DETAIL_FIELDS:
        value = details.get(detail_key)
        if value not in (None, ""):
            row[row_field] = value
    return row

def _unresolvable_marker_entity_pins_attempt(state: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """Pin gate for markers whose entity resolves to no job row.

    The decision state deletes non-authoritative cohort master rows (see
    ``scheduler_state_identity_filter._candidate_state_decision_state``), and the repository
    projection truncates jobs and events independently, so a marker routinely outlives its
    target row on the path production decides on.  Row absence therefore proves nothing by
    itself, and the MARKER has to carry the evidence instead — its own record first, its id
    text only as the legacy backstop.

    Two guards run first, for every marker alike:

    * a non-cycle-grammar id keeps the historical fail-open (synthetic and compacted states
      depend on it, and the SQL retry service's ``{run_id}_retry_active`` shape lands here);
    * a cycle-grammar id whose ``(source, stamp)`` is not the candidate's own never pins.

    After them the gate splits on WHAT THE MARKER RECORDED (#1308).

    RECORD-BORNE LEG — ``target_status`` and ``failed_stage`` both present.  The target is
    rebuilt from the record (``_marker_record_target_row``) and decided by the SAME routing
    ``_marker_event_pins_attempt`` runs on a resolved row, so on everything the record captures
    the two arms are one rule by construction:

    * a model-BEARING record short-circuits to an unconditional pin exactly as the router does
      for a non-cycle-scope row (:608-609) — cross-stage and same-stage alike, and ahead of the
      two staleness mappings, because that is what the router does.  The comparison is
      fail-CLOSED and its source is deliberate: the candidate's own model comes off the TAIL of
      the state's ``run_id`` (``fcst_<source>_<stamp>_<model_id>``), never from the surviving job
      rows — ``_candidate_model_ids`` derives from rows, and the row-window truncation that
      creates this very code path can leave that set EMPTY, which would turn the conjunction
      permanently false and resurrect the under-pin this leg exists to close.  A record naming
      any other model, and a state whose run id yields no model at all, do not pin;
    * a model-LESS record runs ``_cycle_scope_marker_pins_attempt`` over the reconstruction —
      the twin itself, so the shared ``_job_row_is_live_failure`` predicate answers on the
      recorded shape: a placeholder record, a repaired-flagged record and a record whose status
      is outside the live-failure domain refuse the pin exactly as the row-present twin refuses
      the same row.  The middle one of those three is a contract on the record rather than a
      shape the current writer emits (the repaired flags are projection-time annotations, absent
      from the persisted rows the writer reads — see ``MARKER_TARGET_ROW_DETAIL_FIELDS``), as is
      a success status (the writer only ever targets a failure).  The two state-level staleness
      mappings stay in front of it, because they answer a question the record cannot: the
      target's fate AFTER the marker was written.

    BACKSTOP LEG — no record, or a half record (the writer's own shape when the target row
    carries no stage: the empty value is not written and the sanitizer passes no empties).
    Legacy markers, every marker the SQL retry service writes, and synthesized states keep the
    previously delivered behaviour bit for bit: the two staleness mappings, then the stage
    evidence (``details.failed_stage`` primary, loop-stripped id token backstop), then
    ``not _state_has_candidate_scope_failed_job(state)`` — the same predicate object, and
    therefore the same live-failure domain, the twin's arm 2 uses.

    Equivalence claim (delivered domain): for a marker carrying the record, this arm's verdict
    equals the resolved-row routing's verdict on a row of exactly the recorded shape.  What the
    record does not capture stays outside the claim, and that is a PERMANENT LIMITATION,
    disclosed rather than delivered (spec: "Unresolvable cycle-grammar marker pins with
    marker-record evidence").  The record is a write-time snapshot, so most of it is the
    target's POST-WRITE fate — the part the two state mappings do not cover — and it is exactly:

    * the target succeeded after the write but the completed-stage evidence does not name it —
      a later-stage winner evicted it (``_best_completed_stage_success_evidence`` keeps one),
      the projection took the repaired-copy branch (that payload carries no ``job_id``), or its
      stage has no ``_stage_after`` successor at all (``download``/``state_save_qc``/``publish``
      queue targets, whose producer can never name them); widening that producer is refused
      because the same mapping drives restart routing (``chain_repository_state.py:884-886``);
    * the target was repaired after the write without ``repaired_stage_evidence`` naming it
      (that mapping keeps one winner too);
    * the target was re-activated after the write — resubmitted out of a non-terminal failure
      status back into the ACTIVE domain.  ``update_pipeline_job_status``'s terminal guard does
      not cover ``submission_failed``, so on legacy-contract rows and on the auto-retry reuse
      path (``_file_auto_retry_job_can_be_reused``) that transition is producible; on a
      current-contract cohort MASTER row it is not (the typed API freezes ``status`` on ordinary
      upsert, the runtime transition needs an accepted submit outcome, and reclaim needs
      ``reservation_lost``).  Producible or not, it belongs to this same clause.

    One member of the clause is not about post-write fate at all: the target was ALREADY
    annotated repaired when the marker was written.  The annotation exists only on the
    projection's row copy, so the writer cannot see it and the record cannot carry it (#1482) —
    the record reads "live failure" for the same reason as the shapes above, and this arm pins
    the same way.

    In every one of those shapes the record still reads "live failure" and this arm pins where
    the row-present twin, reading the row as it is NOW, would refuse.

    The id-token backstop's stage inference is disclosed with the backstop leg it belongs to: it
    reads the loop-stripped id TEXT, not recorded evidence, and its ceiling is the legacy marker
    set plus the half records the current writer still produces — the token's stage may not be
    the stage the target row actually carried.
    """
    entity_id = event.get("entity_id")
    match = _CYCLE_SCOPE_JOB_ID_RE.fullmatch(str(entity_id)) if entity_id not in (None, "") else None
    if match is None:
        return True
    run_match = _CANDIDATE_RUN_ID_RE.match(str(state.get("run_id") or ""))
    if run_match is None or run_match.group(1, 2) != match.groups():
        return False
    target_row = _marker_record_target_row(event, entity_id)
    if target_row is not None and str(target_row.get("model_id") or ""):
        return str(target_row["model_id"]) == run_match.group(3)
    if _state_repaired_stage_evidence_names_job(state, entity_id):
        return False
    if _state_completed_stage_evidence_names_job(state, entity_id):
        return False
    if target_row is not None:
        return _cycle_scope_marker_pins_attempt(state, target_row)
    failed_stage = state.get("failed_stage")
    if failed_stage not in (None, "") and _unresolvable_marker_stage_is_repair_target(
        event, entity_id, failed_stage
    ):
        return True
    return not _state_has_candidate_scope_failed_job(state)

def _job_status_text(job: Mapping[str, Any]) -> str:
    return str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")

def _job_row_is_live_failure(job: Mapping[str, Any]) -> bool:
    """Is THIS row a live failure — a repair target rather than a historical or in-flight one.

    The one row-level live-failure test in this module, scope-blind on purpose: both of its
    consumers ask it of rows on opposite sides of the cycle-scope split, so the split itself
    stays with the caller (``_job_is_live_candidate_scope_failure`` below subtracts cycle-scope
    rows; ``_cycle_scope_marker_pins_attempt`` is only ever reached WITH one).  Folding the
    exclusion in here would make the marker-target arm constantly false.

    The domain is the FAILURE half of this module's blocker vocabulary — read off
    ``_manual_retry_blocking_pipeline_status`` minus ``ACTIVE_PIPELINE_STATUSES`` rather than
    restated, so it cannot drift from the blocker scan — i.e. ``FAILED_PIPELINE_STATUSES`` plus
    ``cancelled`` (the two constant sets are disjoint, so the subtraction removes nothing else).
    ``cancelled`` belongs because a cancelled job is a first-class manual-retry source
    (``retry.MANUAL_RETRY_SOURCE_STATUSES``): a live failure when the candidate owns it and a
    valid repair target when a marker names it.  The ACTIVE half is subtracted because work
    still in flight is not a repair target at all.

    Two shapes keep a literal failure status while carrying no blocking force anywhere else in
    this module — a repaired stage-evidence row and an unsubmitted auto-retry placeholder — so
    neither counts, at any status.  (The placeholder predicate is itself gated to
    ``pending``/``submission_failed``, exactly as the blocker scan applies it, so a cancelled row
    of placeholder shape is outside the gate and does count.)

    The repaired exclusion only bites if the projection can actually PRODUCE that annotation for
    every status in this domain, so the producers gate on the same domain named once as
    ``chain_source_cycle.REPAIRABLE_PIPELINE_STATUSES``
    (``chain_source_cycle._source_cycle_download_repair_state``,
    ``chain_repository_state._candidate_manual_stage_repair_state``).  While they filtered repair
    targets on the bare ``FAILED_PIPELINE_STATUSES`` set, a repaired ``cancelled`` row got no
    annotation at all and this predicate read it as live forever (#1294 round-3).
    """

    if _pipeline_job_is_repaired_stage_evidence(job):
        return False
    if _job_is_unsubmitted_auto_retry_placeholder(job):
        return False
    status = _job_status_text(job)
    return _manual_retry_blocking_pipeline_status(status) and status not in ACTIVE_PIPELINE_STATUSES

def _job_is_live_candidate_scope_failure(job: Mapping[str, Any]) -> bool:
    """Row-level half of the CANDIDATE-scope live-failure domain, shared by both consumers.

    Cycle-scope rows are the cohort's failure, not the candidate's own, so they are subtracted
    here and only here; everything else is ``_job_row_is_live_failure`` above, which the
    marker-target rule reads too — the two sides are the same predicate by construction (#1294).

    Extracted so the two questions asked of this domain cannot drift:
    ``_state_has_candidate_scope_failed_job`` asks "does ANY row still carry a live failure",
    ``_restarted_stage_family`` asks the same of each row in order to read its stage.  Why the
    ACTIVE half is subtracted, why the two exclusions apply to every row regardless of status,
    and which SOURCES this domain may be read from are documented once, on
    ``_state_has_candidate_scope_failed_job`` below.

    The row-IDENTITY guard comes first (#1299).  ``_state_jobs`` synthesizes the state itself
    as a single row when the state carries no job rows at all, and that row carries no job id
    while ``_job_status_text`` happily reads the state's top-level ``pipeline_status`` off it —
    which is the cycle failure this domain is being asked ABOUT, not a candidate-scope one.
    Every legitimate production row carries an id on both read paths (the DB projection selects
    ``job_id`` first, the journal upserts by it), so requiring one excludes exactly the
    synthesized row and nothing else: an id-bearing row derives exactly as before, including
    the single-mapping and flattened historical shapes whose id sits at the state's top level.
    """

    if job.get("job_id") in (None, "") and job.get("pipeline_job_id") in (None, ""):
        return False
    return not _job_is_cycle_scope_row(job) and _job_row_is_live_failure(job)

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

    That narrowing is enforced by this module itself, with the row-IDENTITY predicate in
    ``_job_is_live_candidate_scope_failure`` above: ``_state_jobs`` synthesizes the state as a
    single job row whenever the state carries any recognized job field
    (``pipeline_status`` being one of them), that synthesized row carries no job id, and an
    id-less row is never a live-failure source — so the top-level ``pipeline_status`` leak is
    closed on job-row-less synthesized and compacted states too (#1299).  The exclusion is
    ID-shaped, not row-count-shaped: an id-BEARING row derives exactly as before, so the
    single-mapping and flattened historical state shapes that embed a job id keep reading a
    flattened failing status as a live failure, exactly as today — which is the fail-open
    behaviour ``_unresolvable_marker_entity_pins_attempt`` records a dependency on.

    The projection SHAPE remains as defence in depth rather than the only line.  On the journal
    path a ``pipeline_job`` marker survives event filtering only when its ``entity_id`` is
    among the projected job rows (``file_orchestration_journal._event_matches_candidate_rows``),
    and a row excluded from the candidate projection drops its ``pipeline_job`` events in the
    SAME step (the foreign-model filter pair in ``candidate_state``'s
    ``pipeline_jobs=``/``pipeline_events=`` derivations, #1288) — dropping the row half alone
    would leave an orphaned marker that re-enters the pin decision through
    ``_unresolvable_marker_entity_pins_attempt``; on the DB path the event query returns only
    ``pipeline_job`` events whose ``entity_id`` is selected from the same job predicate, bound
    to the same values, that the jobs query runs (``chain_repository_state.py:548-558`` against
    ``:510-515``).  A surviving marker therefore proves real job rows exist beside it.

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
    ``_state_hydro_run_is_live_failure`` above, which ``_restarted_stage_family`` reuses.  The
    row half is itself the scope-blind ``_job_row_is_live_failure`` minus cycle-scope rows, and
    that scope-blind predicate is what ``_cycle_scope_marker_pins_attempt`` tests its marker
    target with — so the marker-target side and this scan share one status domain by
    construction (#1294) and only the cycle-scope subtraction and the hydro leg separate them.
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
    failure — resolved/succeeded, still ACTIVE, repaired stage evidence whose failed status is
    historical, or an unsubmitted auto-retry placeholder — is a stale marker target and pins
    nothing, even when ``failed_stage`` names its stage.

    Both sides of this rule read ONE row-level domain: the marker-target test below is
    ``_job_row_is_live_failure`` itself, and the candidate-scope scan arm 2 consults
    (``_state_has_candidate_scope_failed_job``) reads that same predicate plus the cycle-scope
    subtraction and the hydro leg — so the two cannot drift by construction (#1294 closed the
    asymmetry #1287 left, where this arm still tested the bare ``FAILED_PIPELINE_STATUSES``
    set and read a ``cancelled`` cohort master row as a stale target).  A ``cancelled`` target
    is therefore a valid repair target here exactly as it is a live failure there.  The
    row-ABSENT arm (``_unresolvable_marker_entity_pins_attempt``) reuses this function rather than
    paraphrasing it: when its marker carries the target's write-time record it rebuilds the row
    from that record and calls THIS predicate with it, so the shapes below are refused on both
    arms by the same code (#1308).  What the record cannot answer is the target's fate AFTER the
    marker was written; beyond the two state-level staleness mappings that arm consults, that
    remainder stays a disclosed divergence, enumerated there and in the spec.
    """
    if not _job_row_is_live_failure(job):
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
    normalizes them itself, so aliases of one canonical stage answer identically, and a name
    that normalizes to no canonical stage is matched raw against the rows (#1298).  An empty
    set means the candidate has no live failure anywhere in its own
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
    stage=_candidate_failed_stage(state))`` (``scheduler_state_failure._manual_retry_state_evidence``).
    That composition is suffix-aware only while the resolver names a CANONICAL downstream
    stage: with a ``None`` stage ``_state_retry_attempt`` answers the flat top-level
    ``retry_count`` instead — which the journal's clean-reservation invariant has reset to 0 —
    falling back to the rows' recorded counts only on a state carrying no
    ``retry_attempt``/``attempt``/``retry_count`` key at all.
    Three live-failure shapes land there: with no ``failed_stage``/``stage``/``restart_stage``
    key on the state the resolver walks job rows and matches ``FAILED_PIPELINE_STATUSES`` only,
    so it cannot name a ``cancelled`` row; it reads job rows only, so it cannot see a hydro run
    at all; and since #1300 it skips cycle-scope rows, so a cohort-only geometry names nothing.
    The candidate's own ``_retry_2`` row would then derive attempt 1 again, re-minting an
    identity the journal already holds; production reads that as a reservation conflict and
    silently skips the submission.

    In exactly that unnameable-stage case the floor comes from ``_restarted_stage_family`` — the
    stages this restart replays — and each stage's spent attempt is read with the SAME production
    function the nameable case trusts, ``_state_retry_attempt(state, stage=...)``.  Within one
    stage that function is status-blind: it takes the higher of a stage-matching row's recorded
    ``retry_count`` and its ``_retry_<n>`` id suffix, so a
    repaired or already-succeeded ``_retry_3`` row at a family stage still proves attempt 3 was
    spent — at a NON-canonical family stage too since #1298, matched by raw stage name and
    minus the cohort's rows.  Stages OUTSIDE the family contribute nothing, which is the whole
    point: the family is the scope axis, so another stage's consumed counter — a per-model
    forcing retry, or a single-basin cycle's own ``model_id``-stamped download/convert rows —
    can no longer be charged to this restart's budget (#1293 round 4).

    An empty family is the stale-marker shape (no live failure anywhere in candidate scope);
    there is no restarted stage to floor, so the caller's value is returned as is.

    When the stage IS nameable the caller's value already carries that stage's suffix record, so
    this returns it untouched.  The gate reads the SAME resolver the caller composed with,
    ``_candidate_failed_stage`` (#1300).  Reading the scope-blind ``_failed_stage`` here while
    the caller reads the candidate-scoped one closes the gate on a cohort-only geometry whose
    ``previous_attempt`` is the flat 0 — and re-mints attempt 1, the exact replay this floor
    exists to prevent.  Repointing it widens the open surface to every geometry where the
    cycle's stage is canonical and the candidate's own is unnameable; the floor only ever
    raises, and only within the candidate-scoped family, so the direction is safe.  The
    resolver is imported lazily because ``scheduler_state_failure`` imports this module;
    restating its resolution here is what let the two sides drift in the first place.
    """

    from services.orchestrator.scheduler_state_failure import _candidate_failed_stage

    if _canonical_downstream_stage(_candidate_failed_stage(state)) is not None:
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
