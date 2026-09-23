from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from services.orchestrator import scheduler_evidence as _scheduler_evidence

_BOUNDED_CANDIDATE_SUMMARY_KEYS = (
    "candidate_id",
    "source",
    "source_id",
    "cycle_time",
    "cycle_time_utc",
    "scenario_id",
    "run_id",
    "forcing_version_id",
    "basin_id",
    "model_id",
    "status",
    "reason",
    # #1199: the fixed-shape mixed-cohort forced-resubmit veto record must
    # survive bounded candidate summarization so the operator-facing zero-submit
    # explanation stays traceable under evidence pressure.  It is one bounded
    # scalar block (no lists), so retaining it verbatim keeps the summary
    # bounded while preserving the exact schema/reason, identities, counts,
    # decision, restart stage, and cause.
    "terminal_stage_forced_resubmit_veto",
    # Retained so an already-summarized row survives a second summary pass.
    "summary_error",
)
_BOUNDED_CANDIDATE_STATE_EVIDENCE_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("decision", ("decision",)),
    ("missing_forcing_repair_status", ("missing_forcing_repair", "status")),
    ("quarantined_skip_reason", ("journal_predecessor_identity", "quarantined_skip_reason")),
    # #1152: the §8.6 predecessor-pending triage boolean.  Stalls are exactly
    # the passes that overflow the byte budget (one more blocked predecessor
    # row per pass), so dropping it here would make the runbook's
    # single-boolean triage unexecutable where it is needed most.
    ("operator_action_required", ("operator_action_required",)),
    # #1543: the §8.6 emission-feasibility boolean, read together with the one
    # above; ``False`` must survive as well (retention is ``is not None``).
    ("predecessor_emission_blocked", ("predecessor_emission_blocked",)),
    # #1186: the operator manual-action surface identifies rows by ``decision``
    # and reports these numbers; a summarized pass must keep them, ``0``/``False``
    # included.  Summary names avoid the row-level keys above.
    ("retry_attempt", ("retry_policy", "attempt")),
    ("retry_limit", ("retry_policy", "retry_limit")),
    ("retry_occurrences", ("retry_policy", "occurrences")),
    ("manual_retry_required", ("retry_policy", "manual_retry_required")),
    # #1186 round 2: the operator re-entry token, the same surface's only
    # executable output for the breaker arm.  ``confirm-operator-reentry``
    # refuses that arm without ``--recorded-init-state-id``, its refusal payload
    # does not echo the live token, and no other operator surface prints it -- so
    # dropping it here leaves the listed action unexecutable exactly on the passes
    # that overflow the byte budget.  One bounded scalar out of the same identity
    # block as ``quarantined_skip_reason`` above.
    ("recorded_init_state_id", ("journal_predecessor_identity", "recorded_init_state_id")),
    # #1555: the sink refusal's only triage key -- it is what tells an operator
    # WHICH pre-forecast input to repair.  Exactly one pull of the five-field
    # ``operator_reentry_sink_refusal`` block: retaining the block whole is
    # unbounded-shaped, and evidence pressure is precisely the condition under
    # which this refusal has to stay actionable.
    ("refused_restart_stage", ("operator_reentry_sink_refusal", "refused_restart_stage")),
)
_UNRECOGNIZED_CANDIDATE_SUMMARY_ERROR = "unrecognized_candidate_shape"
#: #1905: the mode recorded by the non-blocking summary tier.  The reader keys
#: "this is NOT a size fallback" on the absence of ``limit``, so the mode is an
#: operator/diagnostic marker, not a status.
_NON_BLOCKING_SUMMARY_MODE = "non_blocking_summary"
#: #2402: the reader's own key spellings for a breaker-released source cycle
#: (``operator_action_listing._pass_actions``).  ``cycle_time_utc`` rather than
#: ``cycle_time``: that is the only spelling the released leg is read by.
_BOUNDED_SOURCE_CYCLE_KEYS = (
    "source_id",
    "cycle_time_utc",
    "selection_status",
    "selection_reason",
)
#: The per-model keys of the same leg: the identity, the occurrence count and the
#: re-entry token ``confirm-operator-reentry`` requires.
_BOUNDED_SOURCE_CYCLE_MODEL_KEYS = ("model_id", "occurrences", "recorded_init_state_id")
#: #2570: the failure-cause projection's row keys.  Identity so the operator can
#: name WHICH unit died, ``status`` so the row reads as the producer wrote it,
#: and the two fields that are the crash site's only record.  ``error_message``
#: is deliberately NOT here: ``error_traceback_tail`` already ends with the
#: ``Type: message`` summary (``scheduler_execution.py:165-168``), so carrying it
#: too would spend the byte budget on a duplicate.
_BOUNDED_MODEL_RUN_FAILURE_KEYS = (
    "candidate_id",
    "source_id",
    "cycle_time_utc",
    "model_id",
    # ``basin_version_id``, not ``basin_id``: the producer's identity block
    # (``scheduler_candidate_execution_evidence.py:293-325``) writes the versioned
    # spelling and never the bare one, so a ``basin_id`` entry here would be a key
    # that can never match a row.
    "basin_version_id",
    "run_id",
    "status",
    "error_code",
    "error_traceback_tail",
)
# Both reconcile segments record their own failure key (scheduler_runtime.py:1542,1572)
# and either can be the only one present, so the compact block must keep both.
_BOUNDED_RESTART_RECONCILE_KEYS = ("status", "reserved_unbound_error", "inflight_error")
# #1797: the same symmetry applies to the outcome rows.  `identity_mismatch_blocked` is
# produced by `reconcile_inflight_jobs` (reconcile.py:1076-1087) and lands in `inflight`,
# so compacting only `reserved_unbound` deleted the lane the streak guard below exists to
# protect, and a discarded lane reads exactly like a lane with no outcomes.
_BOUNDED_RESTART_RECONCILE_LANES = ("inflight", "reserved_unbound")
# Producer key set: scheduler_runtime.py:1515-1538 (sole writer of outcome rows).
_BOUNDED_RESTART_RECONCILE_OUTCOME_KEYS = (
    "job_id",
    "action",
    "status",
    "reconciliation_reason_class",
    # The no-progress counter is the whole point of the compact block under
    # evidence pressure; dropping it would hide the wedge it exists to expose.
    "identity_blocked_streak",
    "quarantine_reason",
    "quarantine_field",
    # #1850 Fix A: attempt-scoped binding provenance rides the bounded evidence
    # projection like the other accepted-submit attempt evidence keys; it is
    # additive and never changes an existing key/value.
    "slurm_binding_source",
    "slurm_accounting_submitted_at",
)


def _serialize_evidence_json(payload: Any, *, compact: bool = False) -> str:
    if compact:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return json.dumps(payload, indent=_scheduler_evidence._EVIDENCE_JSON_INDENT, sort_keys=True)


def _serialize_evidence_json_if_within_limit(
    payload: Any,
    *,
    max_evidence_bytes: int,
    compact: bool = False,
) -> str | None:
    if compact:
        encoder = json.JSONEncoder(separators=(",", ":"), sort_keys=True)
    else:
        encoder = json.JSONEncoder(indent=_scheduler_evidence._EVIDENCE_JSON_INDENT, sort_keys=True)
    chunks: list[str] = []
    serialized_bytes = 0
    for chunk in encoder.iterencode(payload):
        serialized_bytes += len(chunk.encode("utf-8"))
        if serialized_bytes > max_evidence_bytes:
            return None
        chunks.append(chunk)
    return "".join(chunks)


def _payload_fits(payload: Mapping[str, Any], *, max_evidence_bytes: int, compact: bool = False) -> bool:
    return (
        _serialize_evidence_json_if_within_limit(
            payload,
            max_evidence_bytes=max_evidence_bytes,
            compact=compact,
        )
        is not None
    )


def _serialized_evidence_within_limit(
    context: Any,
    payload: dict[str, Any],
    *,
    artifact_path: Path,
) -> tuple[dict[str, Any], str]:
    serialized = _serialize_evidence_json_if_within_limit(
        payload,
        max_evidence_bytes=context.max_evidence_bytes,
    )
    if serialized is not None:
        return payload, serialized

    # #1118: the size verdict itself must not be decided by the observe-only
    # marker. A pass that fits without it fits, full stop — otherwise the
    # marker alone would flip a healthy near-limit pass to
    # ``resource_limit_blocked`` and summarize its candidate lists, i.e. an
    # evidence-only feature would rewrite the pass's terminal status.
    if "no_progress_circuit" in payload:
        without_circuit = {key: value for key, value in payload.items() if key != "no_progress_circuit"}
        serialized = _serialize_evidence_json_if_within_limit(
            without_circuit,
            max_evidence_bytes=context.max_evidence_bytes,
        )
        if serialized is not None:
            return without_circuit, serialized
        payload = without_circuit

    # A finished candidate cannot be acted on by this pass any more, so retaining
    # its complete journal history only makes the receipt grow with historical
    # success.  Likewise, retention's per-file inventory is useful while the
    # cleanup runs, but repeating thousands of unchanged ``skipped`` entries in
    # every scheduler receipt can make an otherwise admissible pass fail closed.
    #
    # This is deliberately a *pre-fallback* projection: it keeps the original
    # pass status, preserves non-terminal/in-flight skip evidence verbatim, and
    # records exactly which normal-detail blocks were summarized.  If this
    # bounded projection still does not fit, the existing fail-closed evidence
    # fallback below remains authoritative.
    admission_compacted = _compact_admissible_pass_payload(payload)
    if admission_compacted is not None:
        serialized = _serialize_evidence_json_if_within_limit(
            admission_compacted,
            max_evidence_bytes=context.max_evidence_bytes,
        )
        if serialized is not None:
            return admission_compacted, serialized

    # #1905: candidate detail is what makes a NORMAL pass overflow, and rewriting
    # such a pass's status to ``resource_limit_blocked`` costs far more than the
    # detail does: the size-fallback product is non-evaluating for
    # ``list-operator-actions``, so a whole window of healthy oversized passes
    # answers ``exit 3`` (undecidable) instead of ``exit 0``.  This tier keeps the
    # TRUE status and every other top-level key verbatim -- ``source_cycles``,
    # ``counts``, the scope keys and ``model_run_evidence`` included (the
    # readiness reader validates those rows) -- and replaces only the three
    # candidate lists with the SAME bounded summary rows the fallback uses, which
    # is why a summarized pass lists exactly what the full pass lists.  It starts
    # from the admission projection when that ran, so terminal skipped rows are
    # already projected, and it records the admission record nested rather than
    # overwriting it.  If it still does not fit, the fail-closed fallback below
    # remains authoritative.
    summarized_payload = _non_blocking_summary_payload(
        admission_compacted if admission_compacted is not None else payload,
        max_evidence_bytes=context.max_evidence_bytes,
    )
    if summarized_payload is not None:
        serialized = _serialize_evidence_json_if_within_limit(
            summarized_payload,
            max_evidence_bytes=context.max_evidence_bytes,
        )
        if serialized is not None:
            return summarized_payload, serialized

    bounded_payload = _call_bounded_evidence_payload(context, payload, reason="evidence_size_limit_exceeded")
    bounded_payload = _fit_bounded_evidence_payload(
        bounded_payload,
        max_evidence_bytes=context.max_evidence_bytes,
    )
    serialized = _serialize_evidence_json_if_within_limit(
        bounded_payload,
        max_evidence_bytes=context.max_evidence_bytes,
    )
    if serialized is not None:
        return bounded_payload, serialized
    serialized = _serialize_evidence_json_if_within_limit(
        bounded_payload,
        max_evidence_bytes=context.max_evidence_bytes,
        compact=True,
    )
    if serialized is not None:
        return bounded_payload, serialized

    raise _scheduler_evidence.SchedulerEvidenceWriteError(
        "evidence_size_limit_exceeded",
        {
            "artifact_path": str(artifact_path),
            "max_evidence_bytes": context.max_evidence_bytes,
        },
    )


def _fit_bounded_evidence_payload(
    payload: Mapping[str, Any],
    *,
    max_evidence_bytes: int,
) -> dict[str, Any]:
    bounded_payload = dict(payload)
    _compact_required_bounded_fields(bounded_payload)
    if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
        return bounded_payload

    # #1118: the no-progress circuit is an observe-only marker, so it is the
    # first thing shed under byte pressure — before any existing field is
    # summarized or dropped. Every later tier then sees exactly the payload it
    # saw before the marker existed, so no existing key's trimming moves. The
    # aggregated WARNING and the tracker state file are unaffected, so counting
    # and the ops signal survive a pass whose artifact could not carry them.
    if bounded_payload.pop("no_progress_circuit", None) is not None and _payload_fits(
        bounded_payload,
        max_evidence_bytes=max_evidence_bytes,
        compact=True,
    ):
        return bounded_payload

    _summarize_bounded_candidate_lists(bounded_payload)
    if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
        return bounded_payload

    for field_name in _scheduler_evidence._DROPPABLE_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        emptied_non_empty = bool(bounded_payload[field_name])
        if field_name in _scheduler_evidence._EMPTY_MAPPING_DROPPABLE_BOUNDED_EVIDENCE_FIELDS:
            bounded_payload[field_name] = {}
        else:
            bounded_payload[field_name] = []
        # Only a candidate list that actually lost rows makes the marker "dropped";
        # emptying an already-empty list drops nothing, so the marker stays "summarized".
        if field_name in _scheduler_evidence._BOUNDED_CANDIDATE_LIST_FIELDS and emptied_non_empty:
            _mark_bounded_candidate_lists(bounded_payload, "dropped")
        # #2402: the same rule for the breaker-released source-cycle projection.
        # Clearing rows that were there means the artifact can no longer show a
        # release, and ``dropped`` says exactly that.
        if field_name == "source_cycles" and emptied_non_empty:
            _mark_bounded_source_cycles(bounded_payload, "dropped")
        # #2570: the same rule for the failure-cause projection.  It is shed last
        # of the droppable fields, and when it is shed the marker says so instead
        # of leaving an empty list that reads as "nothing failed".
        if field_name == "model_run_failures" and emptied_non_empty:
            _mark_bounded_model_run_failures(bounded_payload, "dropped")
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name, compactor in (
        ("counts", _compact_counts),
        ("review_contract", _compact_review_contract),
    ):
        if field_name not in bounded_payload:
            continue
        bounded_payload[field_name] = compactor(bounded_payload[field_name])
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _scheduler_evidence._SUMMARIZABLE_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        if _is_required_bounded_field(bounded_payload, field_name):
            continue
        bounded_payload[field_name] = _compact_retained_bounded_field(field_name, bounded_payload[field_name])
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    _drop_empty_optional_bounded_fields(bounded_payload)
    if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
        return bounded_payload

    _drop_not_required_optional_proofs(bounded_payload)
    if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
        return bounded_payload

    for field_name in _scheduler_evidence._OPTIONAL_MINIMAL_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        if _is_required_bounded_field(bounded_payload, field_name):
            continue
        bounded_payload[field_name] = _bounded_retained_field_summary(field_name, bounded_payload[field_name])
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _scheduler_evidence._OPTIONAL_MINIMAL_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        if _is_required_bounded_field(bounded_payload, field_name):
            continue
        bounded_payload[field_name] = _minimal_bounded_retained_field_summary()
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _scheduler_evidence._DROPPABLE_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in bounded_payload:
            continue
        # Removing the key is the same loss as clearing it (#2402).  Reached with
        # a non-empty list only if a caller supplies its own bounded shape: the
        # empty-assign tier above always runs first on the ladder's own product.
        if field_name == "source_cycles" and bounded_payload[field_name]:
            _mark_bounded_source_cycles(bounded_payload, "dropped")
        if field_name == "model_run_failures" and bounded_payload[field_name]:
            _mark_bounded_model_run_failures(bounded_payload, "dropped")
        bounded_payload.pop(field_name)
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    for field_name in _scheduler_evidence._OPTIONAL_BOUNDED_EVIDENCE_DROP_FIELDS:
        if field_name not in bounded_payload:
            continue
        bounded_payload.pop(field_name)
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    if "limit" in bounded_payload:
        bounded_payload["limit"] = _compact_limit(bounded_payload["limit"])
        if _payload_fits(bounded_payload, max_evidence_bytes=max_evidence_bytes, compact=True):
            return bounded_payload

    return bounded_payload


def _non_blocking_summary_payload(
    payload: Mapping[str, Any],
    *,
    max_evidence_bytes: int,
) -> dict[str, Any] | None:
    """Summarize the candidate lists WITHOUT touching the pass status (#1905).

    ``None`` when the payload carries no candidate list at all: there is nothing
    to summarize, so the tier would only add a marker and still not fit.  No
    ``limit`` block is written -- this is not a size fallback, and the reader
    must keep reading such a pass as evaluating.
    """

    summarized_fields = [
        field_name for field_name in _scheduler_evidence._BOUNDED_CANDIDATE_LIST_FIELDS if field_name in payload
    ]
    if not summarized_fields:
        return None
    summarized = dict(payload)
    for field_name in summarized_fields:
        summarized[field_name] = _bounded_candidate_summary_rows(payload[field_name])
    evidence_compaction: dict[str, Any] = {
        "status": "applied",
        "reason": "evidence_size_limit_exceeded",
        "mode": _NON_BLOCKING_SUMMARY_MODE,
        "max_evidence_bytes": max_evidence_bytes,
        "pre_compaction_status": payload.get("status"),
        "summarized_fields": summarized_fields,
    }
    admission = payload.get("evidence_compaction")
    if isinstance(admission, Mapping):
        # Nested, never overwritten: both projections ran on this pass and the
        # admission record is the only statement of what the FIRST one dropped.
        evidence_compaction["admission"] = dict(admission)
    summarized["evidence_compaction"] = evidence_compaction
    return summarized


def _compact_admissible_pass_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Summarize only completed audit detail before the fail-closed fallback.

    The scheduler has already consumed ``skipped_candidates`` and ``retention``
    by the time this writer-side projection runs.  Therefore it cannot weaken
    cancellation, retry, frontier, or deletion behavior.  It exists solely to
    keep a normal pass receipt within its fixed size contract.
    """

    compacted = dict(payload)
    compacted_terminal_skips = 0
    skipped_candidates = payload.get("skipped_candidates")
    if isinstance(skipped_candidates, Sequence) and not isinstance(skipped_candidates, str | bytes | bytearray):
        compacted_skips: list[Any] = []
        for row in skipped_candidates:
            if _is_terminal_skipped_candidate(row):
                compacted_skips.append(_bounded_candidate_summary(row))
                compacted_terminal_skips += 1
            else:
                compacted_skips.append(row)
        if compacted_terminal_skips:
            compacted["skipped_candidates"] = compacted_skips

    retention_entry_counts = _retention_entry_counts(payload.get("retention"))
    if retention_entry_counts:
        compacted["retention"] = _compact_retention(payload.get("retention"))

    if not compacted_terminal_skips and not retention_entry_counts:
        return None

    evidence_compaction: dict[str, Any] = {
        "status": "applied",
        "reason": "pre_write_size_pressure",
    }
    if compacted_terminal_skips:
        evidence_compaction["terminal_skipped_candidates_compacted"] = compacted_terminal_skips
    if retention_entry_counts:
        evidence_compaction["retention_entry_counts_compacted"] = retention_entry_counts
    compacted["evidence_compaction"] = evidence_compaction
    return compacted


def _is_terminal_skipped_candidate(row: Any) -> bool:
    """Whether a skipped row is a completed terminal outcome, never in-flight work."""

    return isinstance(row, Mapping) and str(row.get("reason") or "").startswith("terminal_")


def _retention_entry_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    counts: dict[str, int] = {}
    for field_name in ("planned", "deleted", "skipped", "failed"):
        entries = value.get(field_name)
        if isinstance(entries, Sequence) and not isinstance(entries, str | bytes | bytearray) and entries:
            counts[field_name] = len(entries)
    return counts


def _summarize_bounded_candidate_lists(payload: dict[str, Any]) -> None:
    """Degrade candidate detail to fixed-key summary rows before dropping the lists.

    Three duties: summarize the candidate lists, compact ``restart_reconcile``, and mark
    ``limit.candidate_lists`` as ``summarized`` unless the lists were already dropped.
    Idempotent: a payload whose lists already hold summary rows is unchanged, so the
    tier is safe on payloads that already went through ``bounded_evidence_payload``.
    """

    summarized = False
    for field_name in _scheduler_evidence._BOUNDED_CANDIDATE_LIST_FIELDS:
        if field_name not in payload:
            continue
        payload[field_name] = _bounded_candidate_summary_rows(payload[field_name])
        summarized = True
    if "restart_reconcile" in payload:
        payload["restart_reconcile"] = _compact_bounded_restart_reconcile(payload["restart_reconcile"])
    if summarized and _bounded_candidate_lists_state(payload) != "dropped":
        _mark_bounded_candidate_lists(payload, "summarized")


def _bounded_candidate_summary_rows(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [_bounded_candidate_summary(row) for row in value]


def _bounded_candidate_summary(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {"summary_error": _UNRECOGNIZED_CANDIDATE_SUMMARY_ERROR}
    summary = _present_bounded_summary_keys(row, _BOUNDED_CANDIDATE_SUMMARY_KEYS)
    state_evidence = row.get("state_evidence")
    already_summarized = not isinstance(state_evidence, Mapping)
    for summary_key, path in _BOUNDED_CANDIDATE_STATE_EVIDENCE_KEYS:
        # Row-level incident keys have no candidate producer, so reading them back
        # only re-reads this helper's own output (idempotency).
        value = row.get(summary_key) if already_summarized else _nested_mapping_value(state_evidence, path)
        if value is not None:
            summary[summary_key] = value
    return summary


def _compact_bounded_restart_reconcile(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    compact = _present_bounded_summary_keys(value, _BOUNDED_RESTART_RECONCILE_KEYS)
    for lane in _BOUNDED_RESTART_RECONCILE_LANES:
        lane_outcomes = _compact_bounded_reconcile_lane(value.get(lane))
        if lane_outcomes is not None:
            compact[lane] = {"outcomes": lane_outcomes}
    return compact


def _compact_bounded_reconcile_lane(lane: Any) -> list[dict[str, Any]] | None:
    """Filter one reconcile lane's outcome rows, or ``None`` when the lane has none.

    ``None`` means "emit no lane": both a lane absent from the source and a lane
    present without an ``outcomes`` sequence stay absent from the compact block, so a
    fabricated empty list never claims the lane ran with zero outcomes.
    """

    outcomes = lane.get("outcomes") if isinstance(lane, Mapping) else None
    if not isinstance(outcomes, Sequence) or isinstance(outcomes, str | bytes | bytearray):
        return None
    return [
        _present_bounded_summary_keys(outcome, _BOUNDED_RESTART_RECONCILE_OUTCOME_KEYS)
        if isinstance(outcome, Mapping)
        else {}
        for outcome in outcomes
    ]


def _present_bounded_summary_keys(value: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {key: value[key] for key in keys if value.get(key) is not None}


def _nested_mapping_value(value: Any, path: Sequence[str]) -> Any:
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _bounded_breaker_released_source_cycles(value: Any) -> tuple[list[dict[str, Any]], int]:
    """The capped breaker-released projection and the released TOTAL (#2402).

    Only ``not_selected`` entries carrying the breaker-released
    ``selection_reason`` are kept, in the READER's key spellings
    (``operator_action_listing._pass_actions``): everything else -- selected
    cycles, deferred cycles, the ``backfill_audit`` entry -- is dropped, because
    the fallback exists to bound the artifact and only this leg carries an
    operator action.  The total is counted before the cap, so an overflow shows
    up as ``retained < breaker_released_total``.
    """

    rows: list[dict[str, Any]] = []
    released_total = 0
    for item in _bounded_sequence(value):
        if not isinstance(item, Mapping):
            continue
        if item.get("selection_status") != "not_selected":
            continue
        if item.get("selection_reason") != _scheduler_evidence._BREAKER_RELEASED_SELECTION_REASON:
            continue
        released_total += 1
        if len(rows) >= _scheduler_evidence._BOUNDED_SOURCE_CYCLE_PROJECTION_LIMIT:
            continue
        row = _present_bounded_summary_keys(item, _BOUNDED_SOURCE_CYCLE_KEYS)
        quarantine = item.get("journal_predecessor_identity_quarantine")
        models = quarantine.get("models") if isinstance(quarantine, Mapping) else None
        row["journal_predecessor_identity_quarantine"] = {
            "models": [
                _present_bounded_summary_keys(model, _BOUNDED_SOURCE_CYCLE_MODEL_KEYS)
                for model in _bounded_sequence(models)
                if isinstance(model, Mapping)
            ]
        }
        rows.append(row)
    return rows, released_total


def _bounded_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return ()


def _mark_bounded_source_cycles(payload: dict[str, Any], state: str) -> None:
    """Set ``limit.source_cycles.status``; ``dropped`` is never downgraded (#2402)."""

    limit = payload.get("limit")
    if not isinstance(limit, Mapping):
        return
    marker = limit.get("source_cycles")
    marker = dict(marker) if isinstance(marker, Mapping) else {}
    if marker.get("status") == "dropped":
        return
    marker["status"] = state
    if state == "dropped":
        # The rows are gone, so a retained count from the summarize tier would
        # be a lie; the released TOTAL stays, it is what was lost.
        marker["retained"] = 0
    payload["limit"] = {**limit, "source_cycles": marker}


def _bounded_source_cycles_state(payload: Mapping[str, Any]) -> Any:
    limit = payload.get("limit")
    if not isinstance(limit, Mapping):
        return None
    marker = limit.get("source_cycles")
    return marker.get("status") if isinstance(marker, Mapping) else None


def _bounded_model_run_failures(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """The capped failure-cause projection and the failed TOTAL (#2570).

    Source rows are the ``model_run_evidence`` rows that carry an
    ``error_code`` -- the dispatch catch-all writes one per submitted candidate
    of the unit whose chain died (``scheduler_execution.py:743-754``) -- and
    only the fixed keys above are kept, passed through exactly as the producer
    bounded them.  Rows without an ``error_code`` are a healthy run's detail and
    are not the fallback's business.

    A payload that is ITSELF a fallback product no longer carries
    ``model_run_evidence``; its own projection is then the only surviving record
    of the cause, so it is re-projected rather than discarded.  Re-projecting a
    projected row is a no-op: the row is already exactly this fixed key subset.

    The cap fills from the rows that CARRY a crash site first.  Six sites in the
    same unit loop append an ``error_code`` row to ``model_run_evidence``
    (``scheduler_execution.py``:592 ``OUTPUT_URI_UNAVAILABLE``, :640/:658/:664
    the Slurm-preflight and secret-manifest blocks, :697
    ``RAW_INPUT_STAGING_FAILED``, :742 the dispatch catch-all) and only the LAST
    of them carries ``error_traceback_tail``.  A plain prefix cap would
    therefore let one cycle's worth of tail-less blocked rows fill all 64 slots
    and evict the single row that says why the pass died -- exactly the #2570
    failure at a smaller scale.  Each partition keeps the producer's order, so
    the retained rows stay identifiable.

    The total is counted before the cap, so an overflow shows up as
    ``retained < failed_total``.
    """

    rows_source = _bounded_sequence(payload.get("model_run_evidence"))
    if not rows_source:
        rows_source = _bounded_sequence(payload.get("model_run_failures"))
    limit = _scheduler_evidence._BOUNDED_MODEL_RUN_FAILURE_PROJECTION_LIMIT
    with_cause: list[dict[str, Any]] = []
    without_cause: list[dict[str, Any]] = []
    failed_total = 0
    for item in rows_source:
        if not isinstance(item, Mapping) or item.get("error_code") is None:
            continue
        failed_total += 1
        bucket = with_cause if item.get("error_traceback_tail") else without_cause
        if len(bucket) >= limit:
            continue
        bucket.append(_present_bounded_summary_keys(item, _BOUNDED_MODEL_RUN_FAILURE_KEYS))
    rows = with_cause[:limit]
    rows.extend(without_cause[: limit - len(rows)])
    return rows, failed_total


def _mark_bounded_model_run_failures(payload: dict[str, Any], state: str) -> None:
    """Set ``limit.model_run_failures.status``; ``dropped`` is never downgraded (#2570)."""

    limit = payload.get("limit")
    if not isinstance(limit, Mapping):
        return
    marker = limit.get("model_run_failures")
    marker = dict(marker) if isinstance(marker, Mapping) else {}
    if marker.get("status") == "dropped":
        return
    marker["status"] = state
    if state == "dropped":
        # The rows are gone, so a retained count from the projection would be a
        # lie; the failed TOTAL stays, it is what was lost.
        marker["retained"] = 0
    payload["limit"] = {**limit, "model_run_failures": marker}


def _bounded_model_run_failures_marker(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    limit = payload.get("limit")
    if not isinstance(limit, Mapping):
        return None
    marker = limit.get("model_run_failures")
    return marker if isinstance(marker, Mapping) else None


def _bounded_candidate_lists_state(payload: Mapping[str, Any]) -> Any:
    limit = payload.get("limit")
    return limit.get("candidate_lists") if isinstance(limit, Mapping) else None


def _mark_bounded_candidate_lists(payload: dict[str, Any], state: str) -> None:
    limit = payload.get("limit")
    if not isinstance(limit, Mapping):
        return
    payload["limit"] = {**limit, "candidate_lists": state}


def _compact_required_bounded_fields(payload: dict[str, Any]) -> None:
    runtime_config = payload.get("runtime_config")
    runtime_db_free = runtime_config.get("db_free_runtime") if isinstance(runtime_config, Mapping) else None
    runtime_contract_carries_selectors = bool(
        isinstance(runtime_db_free, Mapping)
        and isinstance(runtime_db_free.get("selectors"), Mapping)
        and isinstance(runtime_db_free.get("paths"), Mapping)
    )
    for field_name in _scheduler_evidence._REQUIRED_BOUNDED_EVIDENCE_FIELDS:
        if field_name not in payload:
            continue
        if field_name == "counts":
            payload[field_name] = _compact_counts(payload[field_name])
        elif field_name == "db_free_runtime":
            payload[field_name] = _compact_db_free_runtime(
                payload[field_name],
                omit_runtime_contract_checks=runtime_contract_carries_selectors,
            )
        elif field_name not in {"schema_version", "pass_id", "status", "artifact_path", "limit"}:
            payload[field_name] = _compact_required_bounded_field(field_name, payload[field_name])


def _is_required_bounded_field(payload: Mapping[str, Any], field_name: str) -> bool:
    return field_name in _scheduler_evidence._REQUIRED_BOUNDED_EVIDENCE_FIELDS and field_name in payload


def _compact_required_bounded_field(field_name: str, value: Any) -> Any:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        return value
    if field_name == "resolved_runtime_roots":
        return _compact_resolved_runtime_roots(value)
    if field_name == "runtime_config":
        return _compact_runtime_config(value)
    if field_name == "db_free_runtime":
        return _compact_db_free_runtime(value)
    if field_name == "root_preflight":
        return _compact_root_preflight(value)
    if field_name == "evidence_pre_execution":
        return _compact_mapping(
            value,
            (
                "status",
                "proof",
                "candidate_count",
            ),
        )
    if field_name in {
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
        "restart_reconcile_proof",
    }:
        return _compact_write_proof(field_name, value)
    if field_name == "no_mutation_proof":
        return _compact_mapping(
            value,
            (
                "adapter_download_called",
                "slurm_submit_called",
                "slurm_status_sync_called",
                "slurm_cancellation_called",
                "shud_runtime_called",
                "hydro_result_table_writes",
                "met_result_table_writes",
                "pipeline_status_writes",
                "pipeline_event_writes",
                "restart_reconcile_writes",
            ),
        )
    if field_name == "retention":
        return _compact_retention(value)
    if field_name == "readiness":
        return _compact_mapping(
            value,
            (
                "schema_version",
                "interpretation",
                "production_ready",
                "final_production_readiness_claimed",
                "can_claim_final_production_readiness",
            ),
        )
    return _compact_retained_bounded_field(field_name, value)


def _drop_empty_optional_bounded_fields(payload: dict[str, Any]) -> None:
    for field_name in (
        "finished_at",
        "execution_mode",
        "readiness_interpretation",
        "model_discovery",
        "source_cycles",
        "candidates",
        "blocked_candidates",
        "skipped_candidates",
        "duplicate_exclusions",
        "restart_reconcile",
    ):
        if payload.get(field_name) in (None, "", [], {}):
            payload.pop(field_name, None)


def _drop_not_required_optional_proofs(payload: dict[str, Any]) -> None:
    for field_name in (
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
    ):
        if _is_required_bounded_field(payload, field_name):
            continue
        value = payload.get(field_name)
        if not isinstance(value, Mapping):
            continue
        if (
            value.get("status") == "not_required"
            and value.get("protected_by_pre_execution_evidence") is not True
            and value.get("mutation_occurred") is not True
        ):
            payload.pop(field_name, None)


def _compact_counts(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    compact: dict[str, Any] = {}
    for key, raw_value in value.items():
        if raw_value not in (0, None, False, "", [], {}):
            compact[str(key)] = raw_value
    return compact or {"candidate_count": 0}


def _compact_review_contract(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    compact = _compact_mapping(value, ("contract_id", "github_issue", "openspec_change", "scope"))
    if _payload_fits(compact, max_evidence_bytes=160, compact=True):
        return compact
    return _compact_mapping(value, ("contract_id", "github_issue"))


def _compact_limit(value: Any) -> Any:
    return _compact_mapping(value, ("reason",))


def _compact_runtime_config(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("runtime_config", value)
    db_free_runtime = value.get("db_free_runtime")
    db_free_required = value.get("scheduler_db_free_required") is True or (
        isinstance(db_free_runtime, Mapping) and db_free_runtime.get("required") is True
    )
    if not db_free_required:
        return _compact_mapping(
            value,
            (
                "service_role",
                "require_runtime_roots",
                "dry_run",
                "allowed_cycle_hours_utc",
                "slurm_array_concurrency_bound",
            ),
        )
    compact = _compact_mapping(
        value,
        (
            "service_role",
            "require_runtime_roots",
            "database_url_configured",
            "scheduler_db_free_required",
            "scheduler_state_backend",
            "scheduler_lock_backend",
            "scheduler_registry_backend",
            "scheduler_canonical_readiness_backend",
            "scheduler_journal_backend",
            "scheduler_state_index_backend",
            "dry_run",
            "allowed_cycle_hours_utc",
            "slurm_array_concurrency_bound",
        ),
    )
    if isinstance(db_free_runtime, Mapping):
        compact["db_free_runtime"] = _compact_db_free_runtime(value.get("db_free_runtime"))
    return compact


def _compact_db_free_runtime(
    value: Any,
    *,
    omit_runtime_contract_checks: bool = False,
) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("db_free_runtime", value)
    compact = _compact_mapping(
        value,
        (
            "status",
            "required",
            "required_env",
            "database_url_configured",
            "canonical_selector_fields",
            "canonical_path_fields",
        ),
    )
    selectors = value.get("selectors")
    if isinstance(selectors, Mapping):
        compact["selectors"] = {
            str(env): _compact_mapping(
                selector,
                ("configured", "selected", "required_value", "file_selected"),
            )
            for env, selector in selectors.items()
            if isinstance(selector, Mapping)
        }
    paths = value.get("paths")
    if isinstance(paths, Mapping):
        compact["paths"] = {
            str(env): _compact_db_free_path_or_check(path)
            for env, path in paths.items()
            if isinstance(path, Mapping)
        }
    checks = value.get("checks")
    if isinstance(checks, Mapping):
        compact["checks"] = _compact_db_free_preflight_checks(
            checks,
            omit_runtime_contract_checks=omit_runtime_contract_checks,
        )
    blockers = value.get("blockers")
    if isinstance(blockers, Sequence) and not isinstance(blockers, str | bytes | bytearray):
        compact["blockers"] = [
            _compact_mapping(blocker, ("code", "field", "reason", "path", "error_type"))
            for blocker in blockers
            if isinstance(blocker, Mapping)
        ]
    provider_blocker = value.get("provider_blocker")
    if isinstance(provider_blocker, Mapping):
        compact["provider_blocker"] = _compact_mapping(provider_blocker, ("code", "field", "reason"))
    nested_evidence = value.get("evidence")
    if (
        isinstance(nested_evidence, Mapping)
        and "selectors" not in compact
        and "paths" not in compact
        and "checks" not in compact
    ):
        compact["evidence"] = _compact_db_free_runtime(nested_evidence)
    return compact


def _compact_db_free_preflight_checks(
    value: Mapping[str, Any],
    *,
    omit_runtime_contract_checks: bool,
) -> dict[str, Any]:
    """Keep preflight-only proof without duplicating runtime selector evidence."""

    compact: dict[str, Any] = {}
    authority_roots = {
        "NHMS_OBJECT_STORE_COPYBACK_ROOT",
        "NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT",
    }
    for raw_env, raw_check in value.items():
        if not isinstance(raw_check, Mapping):
            continue
        env = str(raw_env)
        if "selected" in raw_check or "file_selected" in raw_check:
            if omit_runtime_contract_checks:
                continue
            compact[env] = _compact_mapping(
                raw_check,
                ("configured", "selected", "required_value", "file_selected"),
            )
        elif env == "database_url":
            continue
        elif env in authority_roots:
            compact[env] = _compact_mapping(
                raw_check,
                (
                    "path",
                    "topology_matches",
                    "authority_matches",
                    "canonical_authority_configured",
                ),
            )
        elif env == "NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX":
            compact[env] = _compact_mapping(raw_check, ("configured", "scheme", "supported"))
        elif "path" in raw_check:
            compact[env] = _compact_mapping(raw_check, ("path",))
        else:
            compact[env] = _compact_mapping(raw_check, ("configured", "value_recorded"))
    return compact


def _compact_db_free_path_or_check(value: Mapping[str, Any]) -> dict[str, Any]:
    return _compact_mapping(
        value,
        (
            "configured",
            "selected",
            "required_value",
            "file_selected",
            "value_recorded",
            "path",
            "kind",
            "uri",
            "object_uri",
            "supported_object_uri",
            "scheme",
            "absolute",
            "contained",
            "exists",
            "writable",
            "object_boundary",
            "bucket",
            "namespace",
        ),
    )


def _compact_retention(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("retention", value)
    compact = _compact_mapping(
        value,
        (
            "status",
            "enabled",
            "dry_run",
            "forced_dry_run_by_scheduler",
            "forced_dry_run_reason",
            "retention_days",
            "freed_bytes",
            # Issue #1307: the frontier block is a constant-size scalar block;
            # keeping it verbatim means the exemption evidence survives the
            # size compaction that strips the per-entry skipped detail.
            "frontier",
            # Issue #1318, same rationale: a bounded scalar block (gate, window,
            # cutoff, at most a handful of roots). A compacted receipt reports a
            # single ``retention_days`` and one cross-root ``freed_bytes``, so
            # without this block the reader cannot tell which window governed
            # the additional roots -- and compaction is triggered by exactly the
            # large additional-root sweeps that most need to be read.
            "extra_roots",
            # Three scalar counts (lock_timeout / lock_unsafe /
            # lock_budget_exhausted): the only lock-failure signal left once the
            # per-entry `failed` detail is compacted away.
            "copyback_lock_failures",
        ),
    )
    counts = value.get("counts")
    if isinstance(counts, Mapping):
        compact["counts"] = _compact_mapping(counts, ("planned", "deleted", "skipped", "failed"))
    for field_name in ("planned", "deleted", "skipped", "failed"):
        items = value.get(field_name)
        if isinstance(items, Sequence) and not isinstance(items, str | bytes | bytearray):
            compact[f"{field_name}_count"] = len(items)
    if "deleted_count" in value:
        compact["deleted_count"] = value["deleted_count"]
    return compact


def _compact_retained_bounded_field(field_name: str, value: Any) -> Any:
    if value is None:
        return {}
    if field_name == "resolved_runtime_roots":
        return _compact_resolved_runtime_roots(value)
    if field_name == "runtime_config":
        return _compact_runtime_config(value)
    if field_name == "db_free_runtime":
        return _compact_db_free_runtime(value)
    if field_name == "root_preflight":
        return _compact_root_preflight(value)
    if field_name == "evidence_pre_execution":
        return _compact_mapping(
            value,
            (
                "status",
                "proof",
                "candidate_count",
            ),
        )
    if field_name in {
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
        "restart_reconcile_proof",
    }:
        return _compact_write_proof(field_name, value)
    if field_name == "no_mutation_proof":
        return _compact_mapping(
            value,
            (
                "adapter_download_called",
                "slurm_submit_called",
                "slurm_status_sync_called",
                "slurm_cancellation_called",
                "shud_runtime_called",
                "hydro_result_table_writes",
                "met_result_table_writes",
                "pipeline_status_writes",
                "pipeline_event_writes",
                "restart_reconcile_writes",
            ),
        )
    if field_name == "retention":
        return _compact_retention(value)
    if field_name == "readiness":
        compact = _compact_mapping(
            value,
            (
                "schema_version",
                "interpretation",
                "production_ready",
                "final_production_readiness_claimed",
                "can_claim_final_production_readiness",
            ),
        )
        return compact if compact else _bounded_retained_field_summary(field_name, value)
    return _bounded_retained_field_summary(field_name, value)


def _compact_mapping(value: Any, keys: Sequence[str]) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("", value)
    return {key: value[key] for key in keys if key in value}


def _compact_write_proof(field_name: str, value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary(field_name, value)
    if (
        field_name == "restart_reconcile_proof"
        and value.get("mutation_occurred") is not True
        and value.get("mutation_outcome") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_status_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_event_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    ):
        return _compact_mapping(value, ("status", "mutation_occurred"))
    if (
        field_name == "execution_write_proof"
        and value.get("mutation_outcome") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("slurm_submit_called") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_status_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_event_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    ):
        return _compact_mapping(
            value,
            (
                "status",
                "protected_by_pre_execution_evidence",
                "submitted_count",
                "slurm_submit_called",
                "slurm_submit_count",
                "slurm_submit_proven_absent",
                "mutation_occurred",
                "pipeline_status_writes",
                "pipeline_event_writes",
            ),
        )
    if (
        field_name == "slurm_cancellation_proof"
        and value.get("mutation_occurred") is not True
        and value.get("mutation_outcome") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_status_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        and value.get("pipeline_event_writes") != _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    ):
        return _compact_mapping(
            value,
            (
                "status",
                "cancellation_required",
                "cancel_called",
                "mutation_occurred",
            ),
        )
    return _compact_mapping(
        value,
        (
            "status",
            "protected_by_pre_execution_evidence",
            "evidence_pre_execution_status",
            "submitted_count",
            "slurm_submit_called",
            "slurm_submit_count",
            "slurm_submit_proven_absent",
            "sync_called",
            "updated_job_count",
            "cancellation_required",
            "cancel_called",
            "cancelled_job_count",
            "mutation_outcome",
            "mutation_occurred",
            "bind_reservation_count",
            "update_job_status_count",
            "reserved_unbound_mutation_count",
            "inflight_mutation_count",
            "pipeline_status_writes",
            "pipeline_event_writes",
            "pipeline_status_write_outcome",
            "pipeline_event_write_outcome",
            "pipeline_status_write_count",
            "pipeline_event_write_count",
            "pipeline_status_writes_proven_absent",
            "pipeline_event_writes_proven_absent",
            "error_fields",
        ),
    )


def _compact_resolved_runtime_roots(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("resolved_runtime_roots", value)
    compact_roots: dict[str, Any] = {}
    root_names = ("workspace_root", "evidence_root")
    for root_name in root_names:
        if root_name not in value:
            continue
        root_value = value[root_name]
        if isinstance(root_value, Mapping):
            compact_roots[root_name] = _compact_mapping(root_value, ("path",))
        else:
            compact_roots[root_name] = root_value
    return compact_roots


def _compact_root_preflight(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _bounded_retained_field_summary("root_preflight", value)
    compact: dict[str, Any] = _compact_mapping(value, ("status", "checked_at"))
    checks = value.get("checks")
    if isinstance(checks, Mapping):
        compact_checks: dict[str, Any] = {}
        allowed_roots_policy = checks.get("allowed_roots_policy")
        if isinstance(allowed_roots_policy, Mapping):
            compact_checks["allowed_roots_policy"] = _compact_mapping(
                allowed_roots_policy,
                ("non_empty", "allowed"),
            )
        evidence_root = checks.get("evidence_root")
        if isinstance(evidence_root, Mapping):
            compact_checks["evidence_root"] = _compact_mapping(evidence_root, ("writable", "safe"))
        compact["checks"] = compact_checks
    return compact


def _bounded_retained_field_summary(field_name: str, value: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": "omitted",
        "reason": _scheduler_evidence._RETAINED_FIELD_SUMMARY_REASON,
    }
    if isinstance(value, Mapping):
        summary["omitted_key_count"] = len(value)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        summary["omitted_item_count"] = len(value)
    elif value is None:
        summary["original_value"] = None
    else:
        summary["omitted_value_type"] = type(value).__name__
    if field_name in {
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
        "restart_reconcile_proof",
    }:
        summary["proof_status"] = _mapping_status(value)
    elif field_name in {"evidence_pre_execution", "root_preflight", "readiness"}:
        summary["source_status"] = _mapping_status(value)
    return summary


def _minimal_bounded_retained_field_summary() -> dict[str, str]:
    return {
        "status": "omitted",
        "reason": _scheduler_evidence._RETAINED_FIELD_SUMMARY_REASON,
    }


def _mapping_status(value: Any) -> str | None:
    if isinstance(value, Mapping):
        status = value.get("status")
        if status not in (None, ""):
            return str(status)
    return None


def _call_bounded_evidence_payload(
    context: Any,
    payload: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    if context.bounded_evidence_payload is not None:
        return context.bounded_evidence_payload(
            payload,
            reason=reason,
            max_evidence_bytes=context.max_evidence_bytes,
        )
    return _scheduler_evidence.bounded_evidence_payload(
        payload,
        reason=reason,
        max_evidence_bytes=context.max_evidence_bytes,
    )


def _bounded_limit_block(
    payload: Mapping[str, Any],
    *,
    reason: str,
    max_evidence_bytes: int,
    source_cycle_projection: Sequence[Mapping[str, Any]] = (),
    breaker_released_total: int = 0,
    model_run_failure_projection: Sequence[Mapping[str, Any]] = (),
    model_run_failed_total: int = 0,
) -> dict[str, Any]:
    """Limit block for the fallback shape: fail-closed reason plus observability keys."""

    limit: dict[str, Any] = {
        "reason": reason,
        "max_evidence_bytes": max_evidence_bytes,
        "candidate_lists": "summarized",
    }
    # #2402: the projection marker rides the same block.  ``dropped`` on the
    # SOURCE payload is never downgraded: summarizing an already-dropped fallback
    # product a second time may not claim the released cycles are still shown.
    source_cycles_state = "summarized" if _bounded_source_cycles_state(payload) != "dropped" else "dropped"
    limit["source_cycles"] = {
        "status": source_cycles_state,
        "breaker_released_total": breaker_released_total,
        "retained": len(source_cycle_projection) if source_cycles_state == "summarized" else 0,
    }
    # #2570: the failure-cause marker only exists when there is a failure to
    # speak about.  A pass with no failed model run gets no marker and no
    # projection key -- a fabricated ``failed_total: 0`` would be a statement the
    # payload cannot make, and every pre-#2570 fallback shape stays unchanged.
    prior_failures = _bounded_model_run_failures_marker(payload)
    if model_run_failure_projection or prior_failures is not None:
        prior_total = prior_failures.get("failed_total") if prior_failures is not None else None
        # An overflow the SOURCE already recorded is never argued away: re-bounding
        # a capped projection can only count the rows that survived the cap, so
        # taking the larger total is what keeps "retained < failed_total" true.
        failed_total = max(model_run_failed_total, prior_total) if isinstance(prior_total, int) else (
            model_run_failed_total
        )
        if prior_failures is not None and prior_failures.get("status") == "dropped":
            # Never downgraded: summarizing an already-dropped fallback product a
            # second time may not claim the failure rows are still shown.
            limit["model_run_failures"] = {
                "status": "dropped",
                "failed_total": failed_total,
                "retained": 0,
            }
        else:
            limit["model_run_failures"] = {
                "status": "summarized",
                "failed_total": failed_total,
                "retained": len(model_run_failure_projection),
            }
    pre_limit_status = payload.get("status")
    if pre_limit_status is not None:
        limit["pre_limit_status"] = pre_limit_status
    return limit


def bounded_evidence_payload(
    payload: Mapping[str, Any],
    *,
    reason: str,
    max_evidence_bytes: int = _scheduler_evidence.MAX_EVIDENCE_BYTES,
) -> dict[str, Any]:
    source_cycle_projection, breaker_released_total = _bounded_breaker_released_source_cycles(
        payload.get("source_cycles")
    )
    if _bounded_source_cycles_state(payload) == "dropped":
        # The source is itself a fallback product whose rows were already cut:
        # whatever it still carries can not be presented as the whole release set.
        source_cycle_projection = []
    model_run_failure_projection, model_run_failed_total = _bounded_model_run_failures(payload)
    prior_failure_marker = _bounded_model_run_failures_marker(payload)
    if prior_failure_marker is not None and prior_failure_marker.get("status") == "dropped":
        # Same rule as the source cycles: the rows were already cut once and the
        # marker keeps saying so.
        model_run_failure_projection = []
    bounded_payload = {
        "schema_version": payload.get(
            "schema_version",
            _scheduler_evidence.SCHEDULER_EVIDENCE_SCHEMA_VERSION,
        ),
        "review_contract": payload.get(
            "review_contract",
            {
                "contract_id": _scheduler_evidence.SCHEDULER_EVIDENCE_CONTRACT_ID,
                "github_issue": _scheduler_evidence.SCHEDULER_EVIDENCE_GITHUB_ISSUE,
                "openspec_change": _scheduler_evidence.SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE,
                "scope": "scheduler_pass_evidence",
            },
        ),
        "pass_id": payload.get("pass_id"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "status": "resource_limit_blocked",
        "execution_mode": payload.get("execution_mode"),
        "readiness_interpretation": payload.get("readiness_interpretation", "non_final_scheduler_evidence"),
        "readiness": payload.get(
            "readiness",
            {
                "schema_version": "nhms.production_readiness.scheduler_input.v1",
                "interpretation": "non_final_scheduler_evidence",
                "live_receipts": [],
                "production_ready": False,
                "final_production_readiness_claimed": False,
                "can_claim_final_production_readiness": False,
            },
        ),
        "limit": _bounded_limit_block(
            payload,
            reason=reason,
            max_evidence_bytes=max_evidence_bytes,
            source_cycle_projection=source_cycle_projection,
            breaker_released_total=breaker_released_total,
            model_run_failure_projection=model_run_failure_projection,
            model_run_failed_total=model_run_failed_total,
        ),
        "counts": payload.get("counts", _scheduler_evidence.empty_counts()),
        "resolved_runtime_roots": payload.get("resolved_runtime_roots"),
        "runtime_config": payload.get("runtime_config"),
        "db_free_runtime": payload.get("db_free_runtime"),
        "root_preflight": payload.get("root_preflight"),
        "evidence_pre_execution": payload.get("evidence_pre_execution"),
        "candidates": _bounded_candidate_summary_rows(payload.get("candidates")),
        "blocked_candidates": _bounded_candidate_summary_rows(payload.get("blocked_candidates")),
        "skipped_candidates": _bounded_candidate_summary_rows(payload.get("skipped_candidates")),
        "duplicate_exclusions": payload.get("duplicate_exclusions", []),
        # #2402: NOT ``[]`` any more -- a breaker-released cycle is the only
        # place a released quarantine appears, and emptying the list made every
        # size-fallback pass unable to show that nothing else was released.
        "source_cycles": source_cycle_projection,
        "model_discovery": _scheduler_evidence.empty_model_discovery(),
        "artifact_path": payload.get("artifact_path"),
        "execution_boundary": payload.get("execution_boundary", "planning_only"),
        "execution_write_proof": payload.get("execution_write_proof"),
        "slurm_status_sync_proof": payload.get("slurm_status_sync_proof"),
        "slurm_cancellation_proof": payload.get("slurm_cancellation_proof"),
        "restart_reconcile_proof": payload.get("restart_reconcile_proof"),
        "restart_reconcile": _compact_bounded_restart_reconcile(payload.get("restart_reconcile")),
        # #2570: the only record of why a unit's chain died.  Absent unless the
        # pass actually has a failed model run -- see the pop below.
        "model_run_failures": model_run_failure_projection,
        "no_mutation_proof": payload.get("no_mutation_proof", _scheduler_evidence.no_mutation_proof()),
        "no_progress_circuit": payload.get("no_progress_circuit"),
        "retention": payload.get("retention"),
        "timing": payload.get("timing"),
        # #1734 D11: measured at ~51 live (tag, calls, bytes) triples per pass
        # window, 72 distinct across a 308-test session, against a cap of 256
        # with ``tags_dropped`` observed 0. It survives the
        # bounded path because it is the pass's only read attribution and
        # dropping it would blind the very measurement the artifact exists
        # to carry, at a cost of well under a kilobyte.
        "journal_read_attribution": payload.get("journal_read_attribution"),
    }
    if "db_free_runtime" not in payload:
        bounded_payload.pop("db_free_runtime", None)
    if "retention" not in payload:
        bounded_payload.pop("retention", None)
    if "restart_reconcile_proof" not in payload:
        bounded_payload.pop("restart_reconcile_proof", None)
    if "restart_reconcile" not in payload:
        bounded_payload.pop("restart_reconcile", None)
    # No failed model run means no key at all: an empty list would read as "the
    # pass had failures and they were cut", which is what the ``dropped`` marker
    # is for.
    if not model_run_failure_projection:
        bounded_payload.pop("model_run_failures", None)
    # #1118: with the circuit disabled the source payload has no such key, and a
    # literal ``None`` here would make the over-budget path differ from the
    # plain one — disabled must stay byte-for-byte as before.
    if "no_progress_circuit" not in payload:
        bounded_payload.pop("no_progress_circuit", None)
    if "timing" not in payload:
        bounded_payload.pop("timing", None)
    if "journal_read_attribution" not in payload:
        bounded_payload.pop("journal_read_attribution", None)
    return _fit_bounded_evidence_payload(bounded_payload, max_evidence_bytes=max_evidence_bytes)


__all__ = [
    "_bounded_breaker_released_source_cycles",
    "_bounded_candidate_lists_state",
    "_bounded_candidate_summary",
    "_bounded_candidate_summary_rows",
    "_bounded_limit_block",
    "_bounded_model_run_failures",
    "_bounded_retained_field_summary",
    "_call_bounded_evidence_payload",
    "_compact_bounded_restart_reconcile",
    "_compact_counts",
    "_compact_limit",
    "_compact_mapping",
    "_compact_db_free_runtime",
    "_compact_retention",
    "_compact_required_bounded_field",
    "_compact_required_bounded_fields",
    "_compact_resolved_runtime_roots",
    "_compact_retained_bounded_field",
    "_compact_review_contract",
    "_compact_root_preflight",
    "_drop_empty_optional_bounded_fields",
    "_drop_not_required_optional_proofs",
    "_fit_bounded_evidence_payload",
    "_is_required_bounded_field",
    "_mapping_status",
    "_mark_bounded_candidate_lists",
    "_mark_bounded_model_run_failures",
    "_mark_bounded_source_cycles",
    "_minimal_bounded_retained_field_summary",
    "_nested_mapping_value",
    "_non_blocking_summary_payload",
    "_payload_fits",
    "_present_bounded_summary_keys",
    "_serialized_evidence_within_limit",
    "_serialize_evidence_json",
    "_serialize_evidence_json_if_within_limit",
    "_summarize_bounded_candidate_lists",
    "bounded_evidence_payload",
]
