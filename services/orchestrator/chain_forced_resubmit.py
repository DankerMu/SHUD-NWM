from __future__ import annotations

from typing import Any, Mapping, Sequence

from services.orchestrator.chain_types import TERMINAL_JOB_STATUSES, CycleOrchestrationContext

#: Closed decision whitelist for terminal-stage forced resubmission (#1199).
#: A basin's ``state_evidence["decision"]`` must name one of these to qualify a
#: terminal forced resubmit.  Owned here so the evaluator and the compatibility
#: alias on ``chain_forecast_orchestrator_cycle`` read ONE set; the sibling
#: ``chain_runtime_utils.force_replacement_decisions`` stays a deliberately
#: separate copy (its ``retry_repair_missing_forcing`` difference is pinned by
#: the whitelist-set tests in ``tests/test_warm_start_chaining.py``).
_FORCE_TERMINAL_RESUBMIT_DECISIONS = {
    "retry_repair_missing_forcing",
    "retry_missing_forecast_output",
    "retry_strict_warm_start_terminal_init_state_mismatch",
    "retry_strict_warm_start_terminal_run_manifest_missing",
    "retry_strict_warm_start_retry_run_manifest_mismatch",
    "retry_terminal_run_manifest_missing",
    # §8.7 quarantine rerun (#1157): the cycle's forecast job already
    # SUCCEEDED with a stale predecessor lineage, so an idle resume would
    # re-adopt that very run.  The rerun only means anything as a real
    # replacement submission.  Its breaker fail-stop decision
    # (``blocked_journal_predecessor_identity_quarantine``) is deliberately
    # NOT a member — nothing may revive it.
    "retry_journal_predecessor_identity_mismatch",
}
_STAGE_ORDER = {
    "convert": 0,
    "forcing": 1,
    "forecast": 2,
    "state_save_qc": 3,
    "parse": 4,
    "publish": 5,
}

#: Fixed-shape typed record emitted when a mixed cohort vetoes terminal-stage
#: forced resubmission (#1199).  One stable schema token and one stable reason
#: token; the record itself is the bounded, invocation-local receipt described
#: in ``openspec/changes/scheduler-cohort-retry-evidence``.  Bounded scalars
#: only — no lists, raw state evidence, paths, URIs, secrets, or journal payload.
FORCED_RESUBMIT_VETO_SCHEMA = "nhms.chain.terminal_stage_forced_resubmit_veto.v1"
FORCED_RESUBMIT_VETO_REASON = "mixed_cohort_forced_resubmit_veto"

#: Stable veto causes.  ``state_evidence_missing`` covers a missing or
#: non-mapping ``state_evidence`` on a vetoing basin; the others name the exact
#: qualification clause that failed.
FORCED_RESUBMIT_VETO_CAUSE_STATE_EVIDENCE_MISSING = "state_evidence_missing"
FORCED_RESUBMIT_VETO_CAUSE_DECISION_NOT_IN_WHITELIST = "decision_not_in_whitelist"
FORCED_RESUBMIT_VETO_CAUSE_RESTART_STAGE_UNAVAILABLE = "restart_stage_unavailable"
FORCED_RESUBMIT_VETO_CAUSE_STAGE_BEFORE_RESTART = "stage_before_restart_stage"

#: Per-field scalar cap for every textual value entering the fixed-shape veto
#: record.  The record promises bounded scalars and survives bounded candidate
#: summarization verbatim, so every copied identity/decision string is capped at
#: this explicit small maximum.  Existing safe IDs/tokens (cycle/run/job ids,
#: candidate/model/basin identities, whitelisted decision tokens) are all well
#: under it, so normal production strings stay byte-identical.  Only over-limit
#: values are truncated (with a stable ``…(truncated)`` suffix) so the record
#: never grows arbitrarily even for an over-limit non-whitelisted decision.
FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX = 256


def _bounded_veto_text(value: Any, *, truncation_marker: str = "…(truncated)") -> str | None:
    """Coerce ONE veto-record text field to a capped ``str | None`` scalar.

    Only plain text enters the record: non-string scalars are stringified,
    ``None`` stays ``None``, and mappings/sequences are NEVER stringified into
    the receipt — they collapse to a stable bounded placeholder instead of
    leaking nested data.  Over-limit strings are truncated so the retained
    fixed-shape block stays bounded even for hostile input.
    """

    if value is None:
        return None
    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    ):
        return "unrepresentable"
    text = value if isinstance(value, str) else str(value)
    if len(text) <= FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX:
        return text
    limit = FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX - len(truncation_marker)
    return text[:limit] + truncation_marker


def _canonical_stage(value: Any) -> str | None:
    if value in (None, ""):
        return None
    stage = str(value)
    aliases = {
        "convert_canonical": "convert",
        "produce_forcing_array": "forcing",
        "run_shud_forecast_array": "forecast",
        "save_state_snapshot_array": "state_save_qc",
        "parse_output_array": "parse",
        "publish_tiles": "publish",
    }
    stage = aliases.get(stage, stage)
    return stage if stage in _STAGE_ORDER else None


def _basin_qualifies_forced_resubmit(
    basin: Mapping[str, Any],
    *,
    job_stage: str,
    context_restart_stage: Any,
) -> tuple[bool, dict[str, Any] | None]:
    """Evaluate ONE active basin's existing forced-resubmit qualification.

    Byte-for-byte the same predicate the pre-#1199 gate ran inline per basin:
    the basin must carry a mapping ``state_evidence`` whose ``decision`` is in
    the closed ``_FORCE_TERMINAL_RESUBMIT_DECISIONS`` whitelist AND whose
    canonical restart stage (from ``restart_stage`` / ``restart_from_stage`` /
    the context fallback) exists and is at or before the terminal job stage.
    The canonical restart stage is computed even when a later clause fails, so
    the veto evidence always names it.  Returns ``(qualified, veto_details)``
    where ``veto_details`` is ``None`` for a qualifying basin and a bounded
    cause/decision/restart-stage shape for the FIRST stable clause a
    non-qualifying basin failed.
    """

    state_evidence = basin.get("state_evidence")
    if not isinstance(state_evidence, Mapping):
        return False, {
            "veto_cause": FORCED_RESUBMIT_VETO_CAUSE_STATE_EVIDENCE_MISSING,
            "veto_decision": None,
            "canonical_restart_stage": None,
        }
    decision = state_evidence.get("decision")
    # The canonical restart stage is computed from the same source precedence
    # (``restart_stage`` / ``restart_from_stage`` / context fallback) even when
    # the decision check is the failing clause, so the decisive veto evidence
    # always carries the basin's canonical restart stage alongside the veto
    # decision (#1199).  A non-whitelisted decision must still stay
    # non-qualifying: the boolean predicate and clause precedence are unchanged.
    restart_stage = _canonical_stage(
        state_evidence.get("restart_stage")
        or state_evidence.get("restart_from_stage")
        or context_restart_stage
    )
    # A mapping/list ``decision`` is unhashable and would raise inside the
    # ``in`` membership test; it can never be a whitelist member anyway, so it
    # is a non-whitelisted decision by construction (never admitted, never a
    # crash).  ``_bounded_veto_text`` later keeps it out of the record as nested
    # data, so evidence emission stays safe for the malformed shape.
    try:
        decision_in_whitelist = decision in _FORCE_TERMINAL_RESUBMIT_DECISIONS
    except TypeError:
        decision_in_whitelist = False
    if not decision_in_whitelist:
        return False, {
            "veto_cause": FORCED_RESUBMIT_VETO_CAUSE_DECISION_NOT_IN_WHITELIST,
            "veto_decision": decision,
            "canonical_restart_stage": restart_stage,
        }
    if restart_stage is None:
        return False, {
            "veto_cause": FORCED_RESUBMIT_VETO_CAUSE_RESTART_STAGE_UNAVAILABLE,
            "veto_decision": decision,
            "canonical_restart_stage": None,
        }
    if _STAGE_ORDER[job_stage] < _STAGE_ORDER[restart_stage]:
        return False, {
            "veto_cause": FORCED_RESUBMIT_VETO_CAUSE_STAGE_BEFORE_RESTART,
            "veto_decision": decision,
            "canonical_restart_stage": restart_stage,
        }
    return True, None


def _forced_resubmit_veto_record(
    context: CycleOrchestrationContext,
    *,
    job: Mapping[str, Any],
    job_stage: str,
    basin: Mapping[str, Any],
    details: Mapping[str, Any],
    cohort_size: int,
    qualifying_request_count: int,
) -> dict[str, Any]:
    """Build the one fixed-shape, bounded, typed veto record (#1199).

    Scalar fields only; no lists, raw ``state_evidence``, paths, URIs,
    secrets, or journal payload enter the record.  Every textual value is
    coerced through ``_bounded_veto_text`` so the retained block stays within
    ``FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX`` per field even for over-limit or
    non-string input, while normal production strings stay byte-identical.
    """

    return {
        "schema": FORCED_RESUBMIT_VETO_SCHEMA,
        "reason": FORCED_RESUBMIT_VETO_REASON,
        "cycle_id": _bounded_veto_text(context.cycle_id) or "",
        "pipeline_run_id": _bounded_veto_text(context.run_id) or "",
        "terminal_job_id": _bounded_veto_text(job.get("job_id") or None) or "",
        "canonical_job_stage": _bounded_veto_text(job_stage) or "",
        "cohort_size": cohort_size,
        "qualifying_request_count": qualifying_request_count,
        "first_veto_candidate_id": _bounded_veto_text(
            basin.get("candidate_id") or basin.get("run_id") or None
        )
        or "",
        "first_veto_model_id": _bounded_veto_text(basin.get("model_id") or None) or "",
        "first_veto_basin_id": _bounded_veto_text(
            basin.get("basin_id") or basin.get("model_id") or None
        )
        or "",
        "veto_decision": _bounded_veto_text(details.get("veto_decision")),
        "canonical_restart_stage": _bounded_veto_text(details.get("canonical_restart_stage")),
        "veto_cause": _bounded_veto_text(details.get("veto_cause")),
    }


def _terminal_stage_needs_forced_resubmit(
    context: CycleOrchestrationContext,
    job: Mapping[str, Any],
) -> bool:
    """Whether the cohort's active basins all demand a terminal forced resubmit.

    The verdict is the exact pre-#1199 all-basin conjunction over
    ``_basin_qualifies_forced_resubmit`` — the whitelist, canonical stage-order
    rule, and nonempty-active-basin guard are unchanged and no new decision is
    admitted.  Full evaluation additionally counts qualifying basins and finds
    the first non-qualifying basin in stable active-basin order.  When at least
    one basin qualifies and at least one does not, the FIRST such veto is
    recorded on ``context.forced_resubmit_veto`` as one invocation-local
    fixed-shape record (first veto wins; later checks never overwrite it), and
    ``False`` is returned exactly as before.  All-qualifying cohorts return
    ``True`` with no record; zero-qualifying cohorts return ``False`` with no
    misleading mixed-cohort incident.
    """

    status = str(job.get("status") or "")
    if status not in TERMINAL_JOB_STATUSES:
        return False
    if not context.active_basins:
        return False
    job_stage = _canonical_stage(job.get("stage") or job.get("job_type"))
    if job_stage is None:
        return False
    qualifications = [
        _basin_qualifies_forced_resubmit(
            basin,
            job_stage=job_stage,
            context_restart_stage=context.restart_stage,
        )
        for basin in context.active_basins
    ]
    if not all(qualified for qualified, _details in qualifications):
        if getattr(context, "forced_resubmit_veto", None) is None and any(
            qualified for qualified, _details in qualifications
        ):
            for basin, (qualified, details) in zip(context.active_basins, qualifications, strict=True):
                if qualified or details is None:
                    continue
                context.forced_resubmit_veto = _forced_resubmit_veto_record(
                    context,
                    job=job,
                    job_stage=job_stage,
                    basin=basin,
                    details=details,
                    cohort_size=len(context.active_basins),
                    qualifying_request_count=sum(
                        1 for qualified, _details in qualifications if qualified
                    ),
                )
                break
        return False
    return True
