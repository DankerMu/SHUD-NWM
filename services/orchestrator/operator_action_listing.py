"""``list-operator-actions`` (#1186): enumerate manual-action decisions from pass evidence.

The db-free scheduler writes ``manual_retry_required: true`` on five terminal
decisions but node-22 had no surface that lists them.  This read-only command
scans the newest terminal pass evidence files under the evidence root and prints
one sorted-key JSON line naming every candidate that waits on an operator.

Identification is by DECISION literal, never by the ``manual_retry_required``
flag: a bounded-summarized pass keeps ``decision`` (and, since #1186, the
``retry_policy`` numbers) but drops ``state_evidence`` wholesale.  A
breaker-released backfill cycle never reaches candidate construction, so its
models are read from the not-selected ``source_cycles`` entry instead.

Evidence only, never the journal: the journal carries no decisions.  A pass
only answers "nothing waits" when candidate construction ran in it and its
evidence is whole: its status must be in :data:`EVALUATING_PASS_STATUSES`, and
it must not be a size-fallback artifact -- ``bounded_evidence_payload`` empties
``source_cycles``, the only place a breaker-released cycle appears, so such a
pass can still LIST its summarized blocked candidates but can never prove none
waits.  Nor may an OLDER decidable pass answer for a newer pass that may have
evaluated candidates it can not show (rounds 3/4 r3-01, r4-02): the breaker may
have engaged after the decidable pass.  Only a TRANSPARENT pass
(:data:`TRANSPARENT_PASS_STATUSES`) is known to hide nothing; any other
non-decidable pass -- size-fallback, unreadable (for example half-written),
``lease_lost``, exception-path ``resource_limit_blocked``, an unknown status --
newer (in the mtime order of the scan) than the newest decidable pass leaves
the window undecidable.  A pass the operator NARROWED (round 5 r5-01: backfill
off, filters naming a subset, ``--source`` naming less than
:data:`SCOPE_COMPLETE_SOURCES`, a degenerate ``--lookback-hours 0`` window, or an
``allowed_cycle_hours_utc`` narrower than :data:`SCOPE_COMPLETE_CYCLE_HOURS_UTC`)
answers only for its own scope, so it can not
say "nothing waits" either: it is non-evaluating with reason
:data:`SCOPE_NARROWED_REASON` and leaves the hidden-pass flag exactly as it
found it, while a pass whose scope keys are missing (:data:`SCOPE_UNKNOWN_REASON`)
or that selected no models at all (:data:`NO_MODELS_EVALUATED_REASON`) arms it.
One candidate seen in several passes is listed once, and apart from
``first_seen_pass``/``seen_in_passes`` EVERY value field of that entry comes from
the ``last_seen_pass``: the newest pass wins, because the operator feeds
``recorded_init_state_id`` straight into ``confirm-operator-reentry``, where a
stale token is refused.  Exit codes: ``1`` actions listed, ``0`` none, ``3`` none
but undecidable (a scanned pass dropped its candidate lists, a pass file vanished
mid-scan, no scanned pass is readable, evaluating and scope-complete -- including
an empty root --, or a pass that is neither evaluating-and-scope-complete, nor
transparent, nor merely narrowed is newer than the newest evaluating and
scope-complete pass), ``2`` evidence root missing or unreadable, or ``--passes``
misused.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .scheduler import DEFAULT_ALLOWED_CYCLE_HOURS_UTC, DEFAULT_PRODUCTION_SOURCES
from .scheduler_evidence import MAX_EVIDENCE_BYTES, is_scheduler_pass_evidence_filename

LIST_OPERATOR_ACTIONS_COMMAND = "list-operator-actions"
LIST_OPERATOR_ACTIONS_SCHEMA_VERSION = "nhms.operator_action_listing.v1"
EVIDENCE_ROOT_ENV = "NHMS_SCHEDULER_EVIDENCE_ROOT"
DEFAULT_PASSES = 6

BREAKER_DECISION = "blocked_journal_predecessor_identity_quarantine"
BREAKER_RELEASED_SELECTION_REASON = "journal_predecessor_identity_quarantine_breaker_engaged"
#: Terminal pass statuses written only AFTER ``_build_candidates`` ran
#: (``scheduler_runtime.py`` main path), so an empty action list is an answer.
#: Positive and closed: ``lock_contended``, ``lease_lost``, the exception-path
#: ``resource_limit_blocked`` and every unknown status are non-evaluating;
#: ``preflight_blocked`` is written both before candidate construction (root,
#: runtime, lock-side preflights) and after it, and the status alone can not
#: tell which, so it is non-evaluating too.  A size-fallback artifact (status
#: ``resource_limit_blocked`` with summarized/dropped candidate lists) is
#: non-evaluating whatever ``limit.pre_limit_status`` it kept: its
#: ``source_cycles`` were emptied (reason ``size_fallback_source_cycles_absent``).
#: Recency (round 4): a decidable pass clears the hidden-pass flag, a
#: :data:`TRANSPARENT_PASS_STATUSES` pass leaves it, every other pass arms it.
EVALUATING_PASS_STATUSES = frozenset(
    (
        "planned",
        "blocked",
        "unavailable",
        "submitted",
        "submitted_partial",
        "slurm_status_synced",
        "slurm_status_sync_failed",
        "slurm_cancelled",
        "slurm_partially_cancelled",
        "slurm_cancellation_blocked",
        "restart_reconciled",
        "restart_reconcile_unknown",
        # Passed through ``_scheduler_pass_status_from_execution`` from the last
        # execution-evidence item (#2398 round 2).  Execution evidence is only
        # built after candidate construction, so each is post-construction.
        "submission_failed",
        "skipped_duplicate_submission",
        "reconciling",
        "submit_result_ambiguous",
        "reconcile_unverified",
        "cancelled",
        "complete",
        "succeeded",
        "parsed_partial",
        "forcing_ready_partial",
        "forcing_ready",
        "already_done",
    )
)
#: Non-decidable statuses known to HIDE nothing, so a newer one does not make an
#: older decidable pass stale (round 4).  Closed; checked against the writers in
#: ``scheduler_runtime.py``: ``lock_contended`` is written before candidate
#: construction with empty lists (716); ``preflight_blocked`` is written either
#: before construction with empty lists (594/644/674/762/799/841/898) or after it
#: with the full candidate lists and ``source_cycles`` (1328-1343).  NOT here:
#: ``lease_lost`` (988) and the exception-path ``resource_limit_blocked`` (1473)
#: run after construction and empty the lists; a size-fallback product keeps
#: status ``resource_limit_blocked`` and emptied ``source_cycles``.
TRANSPARENT_PASS_STATUSES = frozenset(("lock_contended", "preflight_blocked"))
_SIZE_FALLBACK_STATUS = "resource_limit_blocked"
_SIZE_FALLBACK_CANDIDATE_LISTS = frozenset(("summarized", "dropped"))
STATUS_NOT_EVALUATING_REASON = "status_not_evaluating"
SIZE_FALLBACK_NON_EVALUATING_REASON = "size_fallback_source_cycles_absent"
#: Round 5 r5-01.  A pass the operator narrowed (backfill off, filters naming a
#: subset of models/basins/an expression, ``--source`` naming less than
#: :data:`SCOPE_COMPLETE_SOURCES`, a ``cycle_window.lookback_hours`` of ``0``,
#: which is a zero-width window, or a ``runtime_config.allowed_cycle_hours_utc``
#: narrower than :data:`SCOPE_COMPLETE_CYCLE_HOURS_UTC`) listed nothing only INSIDE ITS OWN
#: SCOPE, and the breaker-released entries of one of the listed decisions are
#: produced on the backfill leg alone.  It is therefore non-evaluating -- but it
#: LEAVES the hidden-pass flag as it found it: it may not clear it (it did not
#: look everywhere) and it may not arm it (the narrowing was the operator's own
#: instruction and hides nothing unexpectedly).
SCOPE_NARROWED_REASON = "scope_narrowed"
#: An otherwise-evaluating pass whose ``backfill``, ``operator_filters``,
#: ``sources``, ``cycle_window``, ``counts`` or ``runtime_config`` key is missing
#: -- or present but PARTIAL, missing one of the sub-keys the scope test reads
#: (``cycle_window.lookback_hours``, ``counts.selected_model_count``,
#: ``runtime_config.allowed_cycle_hours_utc``), or of a shape the writer
#: can not produce:
#: its scope can not be read, so it is not assumed scope-complete.  This
#: is the same class of uncertainty as a size fallback -- the pass MAY have
#: evaluated candidates it can not show -- so it ARMS the flag (positionally: a
#: newer scope-complete pass clears it again), and is not a global veto the way a
#: dropped candidate list is.
SCOPE_UNKNOWN_REASON = "scope_unknown"
#: #2443: a pass that selected ZERO models evaluated nothing at all.  Not a
#: narrowing -- narrowing is the operator saying "look here only", while this pass
#: looked nowhere -- so it ARMS the flag like a size fallback does.  It is
#: reachable without any operator instruction: the db-free registry manifest
#: accepts ``models: []`` (``scheduler_file_providers.py:883-940`` checks schema,
#: freshness, checksum and the upper bound, never a lower one) and a non-empty
#: manifest whose rows are all excluded by ``scheduler_models.py:137-145``
#: (inactive, not runnable, not a SHUD model, incomplete metadata, duplicate
#: identity) lands in the same place with the registry still ``ready``.  Only
#: ``registry.status == "blocked"`` takes the early ``preflight_blocked`` exit,
#: which is transparent and therefore safe; zero models does not.  The evidence
#: still reports ``backfill.enabled: true`` on such a pass because the writer
#: records the CONFIGURED leg, not the executed one
#: (``scheduler_runtime.py:1395`` vs ``scheduler_discovery.py:700``, where
#: ``backfill_mode`` also requires a non-empty model set) -- that writer-side half
#: is tracked by #2443; this is the read side.
NO_MODELS_EVALUATED_REASON = "no_models_evaluated"

#: Every decision the db-free scheduler writes ``manual_retry_required: True`` on.
#: Literals rather than an alias -- unlike :data:`SCOPE_COMPLETE_SOURCES` below,
#: this set has NO single authority to alias: the five decisions are written from
#: several modules (``scheduler_candidates.py``, ``scheduler_state_failure.py``,
#: ...), each from its own literal or module constant.  So the closure is enforced
#: instead by the pin in ``tests/test_operator_action_listing.py``, which reads
#: EVERY module under ``services/orchestrator/`` (recursively, sub-packages
#: included) with ``ast`` and fails on any writer this set does not name.
#: ``blocked_operator_reentry_restart_stage_refused``
#: is written by ``scheduler_candidates.py`` from its own module constant
#: ``OPERATOR_REENTRY_SINK_REFUSAL_DECISION`` and is handled by the second step of
#: ``docs/runbooks/node22-control-plane-manual-recovery.md``.
OPERATOR_ACTION_DECISIONS = frozenset(
    (
        "permanent_failure",
        "cancelled_manual_retry_required",
        "blocked_strict_warm_start_init_state_mismatch",
        "blocked_operator_reentry_restart_stage_refused",
        BREAKER_DECISION,
    )
)

#: The whole set of production sources a pass must have looked at to answer for
#: everywhere; a pass narrowed with ``--source`` answers only for its own subset.
#: ALIASED FROM THE AUTHORITY, not copied (round 2): ``scheduler.py``'s
#: ``DEFAULT_PRODUCTION_SOURCES``.  It used to be a local literal defended by
#: "``scheduler.py`` is too heavy to import from a db-free listing surface"; that
#: rationale died the moment :data:`SCOPE_COMPLETE_CYCLE_HOURS_UTC` below had to be
#: imported from the same module, and keeping one of two closed sets from ONE
#: authority hand-copied is the very failure this round exists to close.  The
#: import is db-free (``scheduler.py`` reads no database at import time) and costs
#: ~0.1s once; ``cli.py`` already imports the same module.
#: ``tests/test_operator_action_listing.py`` still pins the ``cli.py``
#: ``resolved_sources`` fallback -- an INDEPENDENT literal -- against it.
SCOPE_COMPLETE_SOURCES = DEFAULT_PRODUCTION_SOURCES
#: The cycle hours a scope-complete pass must have looked at.  ``discover_cycles``
#: drops every cycle whose hour is outside this set (``_filter_allowed_cycle_hours``),
#: so a pass run with a set NARROWER than the production default evaluated fewer
#: cycle slots per day and can not answer for the ones it skipped.  Judged by
#: COVERAGE, exactly like :data:`SCOPE_COMPLETE_SOURCES`: a superset still looked at
#: every default hour.  The completeness authority is the code DEFAULT, not the
#: 0-23 full day -- judging against the full day would call every production pass
#: narrowed (production runs the default) and manufacture a false exit 3.
SCOPE_COMPLETE_CYCLE_HOURS_UTC = DEFAULT_ALLOWED_CYCLE_HOURS_UTC

LIST_OPERATOR_ACTIONS_HELP = (
    "List candidates waiting on an operator (#1186): permanent_failure, "
    "cancelled_manual_retry_required, blocked_strict_warm_start_init_state_mismatch, "
    "blocked_journal_predecessor_identity_quarantine, "
    "blocked_operator_reentry_restart_stage_refused. Read-only: scans the newest "
    "--passes terminal scheduler pass evidence files under --evidence-root "
    f"(default ${EVIDENCE_ROOT_ENV}). Exit 1 when actions are listed, 0 when none "
    "and at least one scanned pass evaluated candidates over the whole scope, 3 when "
    "none but undecidable (a pass dropped its candidate lists, no scanned pass is "
    "readable, evaluating and scope-complete, or a pass that is neither "
    "evaluating-and-scope-complete, nor transparent, nor merely scope-narrowed is "
    "newer than the newest evaluating and scope-complete pass -- transparent means "
    "status lock_contended or preflight_blocked; every other non-evaluating pass "
    "(size-fallback, unreadable, lease_lost, resource_limit_blocked, unknown status, "
    "scope_unknown, no_models_evaluated) makes the window undecidable from where it "
    "sits. The reason is reported per pass under non_evaluating_passes / "
    "unreadable_passes and is one of status_not_evaluating, "
    "size_fallback_source_cycles_absent, scope_narrowed, scope_unknown, "
    "no_models_evaluated; an empty root counts, a size-fallback pass never counts as "
    "evaluating because its source_cycles were dropped, and a pass file that vanished "
    "between the directory scan and its stat is reported under unreadable_passes and "
    "vetoes exit 0 wherever it sat). A pass the operator narrowed -- backfill "
    "disabled, --model-id/--basin-id filters, --source naming less than the whole "
    f"production set {'/'.join(SCOPE_COMPLETE_SOURCES)}, the degenerate "
    "--lookback-hours 0 window, or an allowed_cycle_hours_utc narrower than the "
    f"code default ({', '.join(str(hour) for hour in SCOPE_COMPLETE_CYCLE_HOURS_UTC)}) -- is "
    "reported with reason scope_narrowed and never counts as evaluating: it answered "
    "only for its own scope. A pass whose scope keys (backfill, operator_filters, "
    "sources, cycle_window, counts, runtime_config) are missing or partial is reported "
    "scope_unknown, "
    "and a pass whose counts.selected_model_count is 0 evaluated nothing at all and "
    "is reported no_models_evaluated; unlike scope_narrowed, both arm the hidden-pass "
    "flag. Four known boundaries of exit 0, "
    "all decided and not defects: (1) time window -- exit 0 asserts only that the "
    "pass found nothing pending inside ITS OWN window "
    "[cycle_window.start_time_utc, cycle_window.end_time_utc]; production runs "
    "lookback_hours=32 with cycle_lag_hours=16, so cycles newer than the 16h lag sit "
    "in no window at all, and there is no repo-internal authority for a 'complete' "
    "window (production 32, code default 24); (2) single slot -- the backfill leg "
    "evaluates only the OLDEST unfinished cycle per source per pass, newer gaps are "
    "recorded as backfill_deferred_waiting_for_prior_cycle. That is narrower than it "
    "sounds: an unresolved action keeps its own cycle in the gap set, so it holds the "
    "slot and is re-evaluated and re-listed every pass AS LONG AS THAT CYCLE STAYS "
    "INSIDE THE LATER PASSES' DISCOVERY RANGE (see (4)); what is deferred is work on "
    "NEWER cycles, which can not be created before the older one clears. "
    "blocked_journal_predecessor_identity_quarantine is exempt from (2) entirely -- "
    "the breaker release and its evidence are written before the single-slot split, "
    "and it releases every consecutive breaker-engaged cycle from the oldest on. "
    "(3) inactive models -- exit 0 asserts nothing about models the registry manifest "
    "marks inactive. model_discovery.registry.model_count counts every model "
    "REGISTERED in the manifest, while active_model_count counts what "
    "list_models(active=True, ...) returned, and that filter runs before "
    "discover_models ever sees a row, so model_discovery.exclusions structurally can "
    "not record the difference (it only explains rows that were dropped after they "
    "arrived). The two numbers are deliberately not required to agree: that would "
    "forbid retiring a model from the manifest, and the repo holds no authority for "
    "how many models a manifest should carry. "
    "(4) discovery retraction -- exit 0 asserts nothing about actions on cycles that "
    "an earlier, WIDER configuration could see and the current one can not. That is "
    "what qualifies (2)'s mitigation: _filter_allowed_cycle_hours runs in "
    "scheduler_discovery.py BEFORE the single-slot gap selection, and candidates are "
    "built only from the cycles THIS pass discovered, so a cycle whose hour leaves "
    "allowed_cycle_hours_utc -- or that a shortened lookback_hours moves out of the "
    "window -- is not merely deferred: it stops being discovered, stops producing "
    "candidates, and its already-listed unresolved action stops appearing in "
    "blocked_candidates and source_cycles alike. Documented rather than judged, for "
    "the same reason as (1) and (3): this surface reads only the current pass's own "
    "configuration and the repo holds no authority for what an earlier one was. The "
    "cycle-hour knob is environment-only (no CLI flag), so reaching this state takes "
    "a deliberate widen-then-retract of the service environment, not an ordinary run. "
    "This boundary is NOT max_cycles_per_source, which is inert for any pass whose "
    "backfill.enabled is True. Unlike (2), (4) has no exemption for "
    "blocked_journal_predecessor_identity_quarantine: the breaker release reads the "
    "already hour-filtered discoveries, so a retracted cycle is invisible to it too. "
    "Exit 2 when the root is missing or unreadable, or --passes is not an integer "
    ">= 1. Runbook: docs/runbooks/node22-control-plane-manual-recovery.md"
)

_PRE_EXECUTION_SUFFIX = ".pre_execution.json"


class OperatorActionListingError(ValueError):
    """The evidence root cannot be scanned; rendered as one stderr line, exit 2."""


def list_operator_actions(*, evidence_root: str | None, passes: int = DEFAULT_PASSES) -> tuple[dict[str, Any], int]:
    root_text = evidence_root if evidence_root not in (None, "") else os.environ.get(EVIDENCE_ROOT_ENV, "")
    if not str(root_text or "").strip():
        raise OperatorActionListingError(f"evidence root not set: pass --evidence-root or set {EVIDENCE_ROOT_ENV}")
    if type(passes) is not int or passes < 1:
        raise OperatorActionListingError("--passes must be an integer >= 1")
    root = Path(str(root_text))
    selected, vanished = _newest_pass_files(root, passes)

    unreadable: list[str] = []
    dropped: list[str] = []
    non_evaluating: list[dict[str, Any]] = []
    actions: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    # r3-01/r4-02: a pass that is neither decidable nor transparent seen after
    # (newer than) the newest decidable pass.  Transparent passes leave it as is.
    hidden_after_decidable = False
    # Oldest first, so first/last seen read in time order.
    for name, path in reversed(selected):
        payload = _read_pass(path)
        if payload is None:
            unreadable.append(name)
            hidden_after_decidable = True
            continue
        limit = payload.get("limit")
        limit = limit if isinstance(limit, Mapping) else {}
        non_evaluating_entry = _non_evaluating_entry(name, payload)
        if non_evaluating_entry is None:
            hidden_after_decidable = False
        else:
            non_evaluating.append(non_evaluating_entry)
            status = payload.get("status")
            # A size fallback's raw status is ``resource_limit_blocked``: never
            # transparent.  Read the RAW status here, never the entry's, which for
            # a size fallback is the ``limit.pre_limit_status`` it kept.
            transparent = isinstance(status, str) and status in TRANSPARENT_PASS_STATUSES
            # r5-01 third state: a scope-narrowed pass leaves the flag alone.
            # ``scope_unknown`` and ``no_models_evaluated`` are NOT exempt -- they
            # arm, like every other pass that may have evaluated candidates it can
            # not show, or evaluated nothing at all.
            if not transparent and non_evaluating_entry["reason"] != SCOPE_NARROWED_REASON:
                hidden_after_decidable = True
        if limit.get("candidate_lists") == "dropped":
            dropped.append(name)
        for action in _pass_actions(payload):
            key = (action["source_id"] or "", action["cycle_time"] or "", action["model_id"] or "", action["decision"])
            existing = actions.get(key)
            if existing is None:
                actions[key] = {**action, "first_seen_pass": name, "last_seen_pass": name, "seen_in_passes": 1}
                continue
            # Newest wins (the loop runs oldest -> newest): every value field is
            # the one ``last_seen_pass`` carried.  Keeping the OLDEST values while
            # naming the newest pass produced a self-contradictory receipt, and the
            # runbook feeds ``recorded_init_state_id`` from it into
            # ``confirm-operator-reentry``, which refuses a stale token.  Only
            # ``first_seen_pass`` and the occurrence count survive from before; a
            # missing ``candidate_id`` (the breaker-released leg has none) falls
            # back to whatever an earlier pass knew.
            candidate_id = action["candidate_id"]
            actions[key] = {
                **action,
                "candidate_id": candidate_id if candidate_id is not None else existing["candidate_id"],
                "first_seen_pass": existing["first_seen_pass"],
                "last_seen_pass": name,
                "seen_in_passes": existing["seen_in_passes"] + 1,
            }

    listed = [actions[key] for key in sorted(actions)]
    receipt = {
        "schema_version": LIST_OPERATOR_ACTIONS_SCHEMA_VERSION,
        "evidence_root": str(root),
        "passes_scanned": len(selected),
        # Both kinds of "could not read this pass" are reported here, but only the
        # ones that were SELECTED are subtracted from ``evaluating_count`` below:
        # a file that vanished mid-scan never entered ``selected``, so subtracting
        # it would undercount.  Asymmetry on purpose, see the exit chain.
        "unreadable_passes": sorted(unreadable + vanished),
        "operator_action_count": len(listed),
        "operator_actions": listed,
        "candidate_lists_dropped_passes": sorted(dropped),
        "non_evaluating_passes": sorted(non_evaluating, key=lambda item: item["pass"]),
    }
    evaluating_count = len(selected) - len(unreadable) - len(non_evaluating)
    if listed:
        return receipt, 1
    # ``vanished`` is a GLOBAL veto, like ``dropped`` and unlike an unreadable
    # selected pass: its mtime was never read, so it can not be placed in the time
    # order at all.  A pass whose position is unknown can not arm the flag
    # positionally, and the only honest answer left is "undecidable".
    if dropped or vanished or evaluating_count < 1 or hidden_after_decidable:
        return receipt, 3
    return receipt, 0


def _non_evaluating_entry(name: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """``None`` for an evaluating, scope-complete pass, else its ``non_evaluating_passes`` entry.

    Takes the whole payload because the scope test (r5-01) reads five of its
    top-level keys.  STATUS IS JUDGED FIRST and scope only within what the status
    leaves evaluating: a pass written before candidate construction structurally
    carries no ``backfill`` key, so testing scope ahead of status would call every
    transparent pass ``scope_unknown`` and make a ``lock_contended`` pass newer
    than a decidable one force exit 3 (``spec.md:54``).
    """

    status = payload.get("status")
    limit = payload.get("limit")
    limit = limit if isinstance(limit, Mapping) else {}
    if status == _SIZE_FALLBACK_STATUS and limit.get("candidate_lists") in _SIZE_FALLBACK_CANDIDATE_LISTS:
        kept = limit.get("pre_limit_status")
        return {
            "pass": name,
            "status": str(kept) if kept not in (None, "") else _SIZE_FALLBACK_STATUS,
            "reason": SIZE_FALLBACK_NON_EVALUATING_REASON,
        }
    if not (isinstance(status, str) and status in EVALUATING_PASS_STATUSES):
        return {
            "pass": name,
            "status": status if isinstance(status, str) and status else None,
            "reason": STATUS_NOT_EVALUATING_REASON,
        }
    scope_reason = _scope_reason(payload)
    if scope_reason is None:
        return None
    return {"pass": name, "status": status, "reason": scope_reason}


#: The scope keys a pass that reached candidate construction ALWAYS carries, so
#: that missing one of them means "unreadable scope", not "unnarrowed scope":
#: ``scheduler_evidence.py:248-253`` writes the top-level ``operator_filters`` as
#: an unconditional four-key dict literal, and ``scheduler_runtime.py:1394-1402``
#: writes ``backfill`` on BOTH legs of its if/else, each carrying ``enabled``.
#: Do NOT read ``scheduler_evidence.py:995`` as a counter-example: that two-key
#: mapping is ``model_discovery.operator_filters``, a NESTED mirror this module
#: never reads -- the scope test reads the TOP-LEVEL keys only.  The third scope
#: dimension, the top-level ``sources`` list, is written just as unconditionally
#: (``scheduler_evidence.py:268``, ``list(config.sources)``); the second spelling
#: at ``:859`` is the nested runtime-config mirror, which this module never reads.
#: The fourth, ``cycle_window`` (``scheduler_evidence.py:270-276``), is written
#: unconditionally in the very same ``base_evidence`` dict literal; only
#: ``lookback_hours`` of its five sub-keys is read, see
#: :data:`_REQUIRED_CYCLE_WINDOW_KEYS`.
_REQUIRED_OPERATOR_FILTER_KEYS = ("basin_ids", "model_ids", "expression")
#: The one ``cycle_window`` sub-key the scope test reads.  ``start_time_utc`` and
#: ``end_time_utc`` are derived from it (``scheduler_evidence.py:244-245``), so
#: judging the source is enough; ``cycle_lag_hours`` shifts the window rather than
#: collapsing it and has no objective degenerate point (a documented boundary of
#: exit 0 instead, see :data:`LIST_OPERATOR_ACTIONS_HELP`); and
#: ``max_cycles_per_source`` is inert on any pass with backfill enabled -- its
#: truncation lives in ``_select_legacy_source_cycles``
#: (``scheduler_discovery.py:904-906``), which the backfill leg never reaches
#: (``:700``), and a pass without backfill is already ``scope_narrowed`` below.
_REQUIRED_CYCLE_WINDOW_KEYS = ("lookback_hours",)
#: The one ``counts`` sub-key this surface reads (#2443).  ``counts`` is written
#: in the same main-path evidence dict as ``blocked_candidates`` itself
#: (``scheduler_runtime.py:1346-1350``) and was present on EVERY live node-22
#: evaluating pass without exception (measured 2026-09-16), each with
#: ``selected_model_count`` 76 -- so requiring it does not turn any live pass
#: unknown, and the zero test does not turn any live pass undecidable.  The other
#: counters are candidate/submission tallies, not statements about what the pass
#: was able to look at.  Name trap: this ``counts.candidate_count`` is the sum of
#: the three candidate list lengths (``scheduler_runtime.py:1327``), while
#: ``progress_guard.checkpoints[].details.candidate_count`` (``:942``/``:1112``) is
#: only ``len(candidates)`` -- same name, different quantity.
_REQUIRED_COUNTS_KEYS = ("selected_model_count",)
#: The one ``runtime_config`` sub-key this surface reads.  The TOP-LEVEL
#: ``runtime_config`` block is written unconditionally by ``base_evidence``
#: (``scheduler_evidence.py:293-297``) and never overwritten afterwards, which
#: makes it the only authority; the nested mirror at ``scheduler_evidence.py:860``
#: is not read.  ``allowed_cycle_hours_utc`` is the only knob in that block with no
#: second declaration elsewhere in the payload -- every other entry is either a
#: copy of a top-level key already judged, a ``config.dry_run`` derivative, a
#: throughput/retry parameter, or a binary block-and-rewrite-``status`` contract
#: switch.  Measured 2026-09-16: every live node-22 pass carries ``[0, 12]``, the
#: code default element for element, so the coverage test flips no live exit code.
_REQUIRED_RUNTIME_CONFIG_KEYS = ("allowed_cycle_hours_utc",)


def _scope_reason(payload: Mapping[str, Any]) -> str | None:
    """``None`` when the pass looked everywhere, else why it can not answer for everywhere.

    The dividing line is KEY PRESENCE, and it is the whole point of the reasons
    this returns: a key that is THERE carrying a narrowing value is the operator's
    own instruction (``scope_narrowed``, which hides nothing unexpectedly and so
    leaves the hidden-pass flag), while a key that is MISSING means the scope can
    not be read at all (``scope_unknown``, the same uncertainty as a size
    fallback, so it arms).  The third case is a key that is there carrying a value
    meaning "this pass observed nothing at all" -- that is neither an instruction
    nor unreadable, and it arms too (:data:`NO_MODELS_EVALUATED_REASON`).
    A partial mapping -- ``operator_filters: {}``, or one
    missing just ``expression`` -- must therefore NOT fall through to the value
    test: every absent key would read as an empty default and the pass would be
    called scope-complete, clearing the flag and permitting exit 0 off a pass
    whose scope nobody established.  That is r5-01's own failure mode through a
    different door.

    Within a mapping whose keys are all present, scope is read from the filter
    VALUES, never from the mapping's size: a normal production pass always writes
    the four keys at their empty defaults (measured on every live node-22 pass
    without exception, EF-8, 2026-09-16), so a rule keyed on mapping-emptiness
    would call every production pass narrowed and never reach exit 0.

    ``sources`` is the third narrowing dimension, on exactly the same footing:
    ``plan-production --source gfs`` lands as the top-level ``sources`` list
    (``scheduler_evidence.py:268``) and such a pass never looked at IFS.  It can
    not be judged by emptiness -- ``cli.py:421`` makes the list non-empty always,
    defaulting to the whole set -- so it is tested for COVERAGE of
    :data:`SCOPE_COMPLETE_SOURCES`: a superset (``--source gfs --source IFS
    --source ERA5``) did look at gfs and IFS and is scope-complete, only a subset
    is narrowed.  Not case-folded on purpose: ``scheduler_config/config.py:448``
    normalizes every spelling through ``normalize_source_id`` and raises on an
    unknown one, so a case variant can not reach the pass file -- while the
    db-free adapter's manifest path ``raw/{source_id}/...``
    (``scheduler_adapters.py:289,368-376``) IS case-sensitive, so folding here
    would hand a key to a false-exit-0 door that the config layer keeps shut.

    ``cycle_window.lookback_hours`` is the fourth: a pass run with
    ``--lookback-hours 0`` has a zero-width discovery window and is blind to every
    older cycle, while the breaker release sits by construction on the oldest side
    (``scheduler_discovery.py:824-832``).  The top-level ``cycle_window`` spelling
    is read rather than ``backfill.lookback_hours`` for two reasons: it is written
    unconditionally in ``base_evidence`` (``scheduler_evidence.py:270-276``) while
    ``backfill.lookback_hours`` exists only on the ``backfill_enabled`` leg
    (``scheduler_runtime.py:1396``), and promoting a key that only one leg writes
    to a required field would couple the presence check to the ``enabled`` test
    below in order-dependent ways.  ``lookback_hours > 0`` in general is NOT
    judged: there is no repo-internal authority for a "complete" window
    (production 32, code default 24), so that is a documented boundary of exit 0
    (see :data:`LIST_OPERATOR_ACTIONS_HELP`), not a narrowing.

    ``runtime_config.allowed_cycle_hours_utc`` is the fifth, and the last door:
    ``discover_cycles`` filters out every cycle whose hour is not in this set
    (``_filter_allowed_cycle_hours``), so the default ``(0, 12)`` already means
    only two of a day's twenty-four cycle slots are ever evaluated.  A pass run
    with a NARROWER set skipped slots the next pass may not revisit, so it is
    judged by coverage of :data:`SCOPE_COMPLETE_CYCLE_HOURS_UTC` -- the code
    DEFAULT, deliberately not the full 0-23 day.  Judging against the full day
    would call every production pass narrowed, which is a false exit 3 and exactly
    the invented threshold ``cycle_lag_hours`` was refused for.

    ``counts.selected_model_count`` (#2443) is judged on a different axis: ``0``
    means the pass evaluated NOTHING, which is not a narrowing at all, so it gets
    its own arming reason -- see :data:`NO_MODELS_EVALUATED_REASON`.
    """

    backfill = payload.get("backfill")
    operator_filters = payload.get("operator_filters")
    sources = payload.get("sources")
    cycle_window = payload.get("cycle_window")
    counts = payload.get("counts")
    runtime_config = payload.get("runtime_config")
    if not isinstance(backfill, Mapping) or "enabled" not in backfill:
        return SCOPE_UNKNOWN_REASON
    if not isinstance(operator_filters, Mapping):
        return SCOPE_UNKNOWN_REASON
    if any(key not in operator_filters for key in _REQUIRED_OPERATOR_FILTER_KEYS):
        return SCOPE_UNKNOWN_REASON
    # Anything but a list/tuple of strings is a shape the writer can not produce,
    # so the scope can not be read -- and set() on an unhashable element would
    # escape the 0/1/2/3 contract as a TypeError.
    if not isinstance(sources, list | tuple) or not all(isinstance(item, str) for item in sources):
        return SCOPE_UNKNOWN_REASON
    if not isinstance(cycle_window, Mapping):
        return SCOPE_UNKNOWN_REASON
    # ``type(...) is not int`` rather than ``isinstance``: it rejects ``bool``,
    # which is an ``int`` subclass, the same way ``--passes`` is validated above.
    # The writer only ever puts ``config.lookback_hours`` here, an ``int`` built by
    # ``max(int(...), 0)`` (``scheduler_config/config.py:458``); anything else is a
    # shape the writer can not produce, so the window can not be read.
    if any(type(cycle_window.get(key)) is not int for key in _REQUIRED_CYCLE_WINDOW_KEYS):
        return SCOPE_UNKNOWN_REASON
    if not isinstance(counts, Mapping):
        return SCOPE_UNKNOWN_REASON
    if any(type(counts.get(key)) is not int for key in _REQUIRED_COUNTS_KEYS):
        return SCOPE_UNKNOWN_REASON
    if not isinstance(runtime_config, Mapping):
        return SCOPE_UNKNOWN_REASON
    # Same shape discipline as ``sources``: the writer puts ``list(config....)``
    # here, so anything that is not a sequence of plain ints is a shape it can not
    # produce, and ``set()`` over it could raise instead of returning an exit code.
    # ``type(...) is not int`` again excludes ``bool``.
    for key in _REQUIRED_RUNTIME_CONFIG_KEYS:
        value = runtime_config.get(key)
        if not isinstance(value, list | tuple) or any(type(hour) is not int for hour in value):
            return SCOPE_UNKNOWN_REASON
    # #2443, and it outranks every narrowing below: a pass that selected no models
    # could not answer even for its own narrowed scope, so reporting
    # ``scope_narrowed`` (which LEAVES the flag) would be the wrong of the two.
    # ``<= 0`` is defensive in the same way the window test below is: ``len(models)``
    # can only produce ``0``.
    if counts["selected_model_count"] <= 0:
        return NO_MODELS_EVALUATED_REASON
    if backfill["enabled"] is not True:
        return SCOPE_NARROWED_REASON
    if operator_filters["basin_ids"] or operator_filters["model_ids"]:
        return SCOPE_NARROWED_REASON
    if operator_filters["expression"] is not None:
        return SCOPE_NARROWED_REASON
    if not set(SCOPE_COMPLETE_SOURCES) <= set(sources):
        return SCOPE_NARROWED_REASON
    # ``<= 0`` is defensive: the only reachable degenerate value is exactly ``0``
    # (``cli.py:431-435`` puts no lower bound on ``--lookback-hours`` and
    # ``scheduler_config/config.py:458`` clamps negatives to ``0``), and
    # ``--cycle-time`` sets it to ``0`` on purpose (``cli.py:451``).
    if cycle_window["lookback_hours"] <= 0:
        return SCOPE_NARROWED_REASON
    # Coverage, not equality, for the same reason ``sources`` is: a pass allowed
    # MORE hours than the default still looked at every default hour.  An empty
    # list is narrowed (it looked at no cycle hour at all).
    if not set(SCOPE_COMPLETE_CYCLE_HOURS_UTC) <= set(runtime_config["allowed_cycle_hours_utc"]):
        return SCOPE_NARROWED_REASON
    return None


def _newest_pass_files(root: Path, passes: int) -> tuple[list[tuple[str, Path]], list[str]]:
    """Top-level terminal pass files, newest ``passes`` by mtime, plus the names that vanished.

    Only ``os.scandir(root)`` itself is a scan-aborting error (exit 2, "the root
    is unreadable").  A SINGLE entry that raises -- the retention timer deletes
    under this very root on its own schedule -- is reported and skipped, never
    rendered as "evidence root unreadable", which would name the root, hide the
    file that went away, and throw every readable pass away with it.
    """

    entries: list[tuple[int, str, Path]] = []
    vanished: list[str] = []
    try:
        with os.scandir(root) as iterator:
            for entry in iterator:
                name = entry.name
                if not is_scheduler_pass_evidence_filename(name) or name.endswith(_PRE_EXECUTION_SUFFIX):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    mtime = entry.stat(follow_symlinks=False).st_mtime_ns
                except OSError:
                    vanished.append(name)
                    continue
                entries.append((mtime, name, Path(entry.path)))
    except OSError as error:
        raise OperatorActionListingError(
            f"evidence root unreadable: {type(error).__name__}: {root}"
        ) from error
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(name, path) for _mtime, name, path in entries[:passes]], vanished


def _read_pass(path: Path) -> dict[str, Any] | None:
    try:
        # Pass evidence is bounded by the writer; anything larger is not ours to parse.
        with path.open("rb") as handle:
            content = handle.read(MAX_EVIDENCE_BYTES + 1)
        if len(content) > MAX_EVIDENCE_BYTES:
            return None
        payload = json.loads(content)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _pass_actions(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for row in _sequence(payload.get("blocked_candidates")):
        if not isinstance(row, Mapping):
            continue
        state_evidence = row.get("state_evidence")
        state_evidence = state_evidence if isinstance(state_evidence, Mapping) else {}
        decision = row.get("decision") or state_evidence.get("decision")
        if decision not in OPERATOR_ACTION_DECISIONS:
            continue
        retry_policy = state_evidence.get("retry_policy")
        retry_policy = retry_policy if isinstance(retry_policy, Mapping) else {}
        identity = state_evidence.get("journal_predecessor_identity")
        identity = identity if isinstance(identity, Mapping) else {}
        found.append(
            _action(
                candidate_id=row.get("candidate_id"),
                source_id=row.get("source_id") or row.get("source"),
                cycle_time=row.get("cycle_time_utc") or row.get("cycle_time"),
                model_id=row.get("model_id"),
                decision=str(decision),
                reason=row.get("reason"),
                attempt=_first_present(retry_policy.get("attempt"), row.get("retry_attempt")),
                retry_limit=_first_present(retry_policy.get("retry_limit"), row.get("retry_limit")),
                occurrences=_first_present(retry_policy.get("occurrences"), row.get("retry_occurrences")),
                # Row-level second read like the retry triple above: a bounded
                # summary drops ``state_evidence`` wholesale and projects this
                # token to the row level instead (``scheduler_evidence_payload.py``
                # ``_BOUNDED_CANDIDATE_STATE_EVIDENCE_KEYS``).  Reading only the
                # nested spelling reported ``null`` for a pass that DID observe the
                # token -- and this token is the only way to supply
                # ``confirm-operator-reentry --recorded-init-state-id``.
                recorded_init_state_id=_first_present(
                    identity.get("recorded_init_state_id"), row.get("recorded_init_state_id")
                ),
            )
        )
    for item in _sequence(payload.get("source_cycles")):
        if not isinstance(item, Mapping):
            continue
        if item.get("selection_status") != "not_selected":
            continue
        if item.get("selection_reason") != BREAKER_RELEASED_SELECTION_REASON:
            continue
        quarantine = item.get("journal_predecessor_identity_quarantine")
        models = quarantine.get("models") if isinstance(quarantine, Mapping) else None
        for model in _sequence(models):
            if not isinstance(model, Mapping):
                continue
            found.append(
                _action(
                    candidate_id=None,
                    source_id=item.get("source_id"),
                    cycle_time=item.get("cycle_time_utc"),
                    model_id=model.get("model_id"),
                    decision=BREAKER_DECISION,
                    reason=BREAKER_RELEASED_SELECTION_REASON,
                    attempt=None,
                    retry_limit=None,
                    occurrences=model.get("occurrences"),
                    recorded_init_state_id=model.get("recorded_init_state_id"),
                )
            )
    return found


def _action(**fields: Any) -> dict[str, Any]:
    return {key: (None if value in (None, "") else value) for key, value in fields.items()}


def _first_present(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return ()


def register_click_list_operator_actions_command(cli: Any) -> None:
    """Register ``list-operator-actions`` on the Click group ``cli``."""

    import click

    @cli.command(LIST_OPERATOR_ACTIONS_COMMAND, help=LIST_OPERATOR_ACTIONS_HELP)
    @click.option("--evidence-root", default=None)
    @click.option("--passes", default=DEFAULT_PASSES, type=int, show_default=True)
    def list_operator_actions_command(evidence_root: str | None, passes: int) -> None:
        try:
            receipt, exit_code = list_operator_actions(evidence_root=evidence_root, passes=passes)
        except OperatorActionListingError as error:
            click.echo(str(error), err=True)
            raise SystemExit(2) from error
        click.echo(json.dumps(receipt, sort_keys=True))
        if exit_code != 0:
            raise SystemExit(exit_code)


def add_argparse_list_operator_actions_subparser(subparsers: Any) -> None:
    """Add the ``list-operator-actions`` argparse subparser to ``subparsers``."""

    parser = subparsers.add_parser(
        LIST_OPERATOR_ACTIONS_COMMAND,
        help=LIST_OPERATOR_ACTIONS_HELP,
        description=LIST_OPERATOR_ACTIONS_HELP,
    )
    parser.add_argument("--evidence-root", default=None)
    parser.add_argument("--passes", type=int, default=DEFAULT_PASSES)


def run_argparse_list_operator_actions_command(args: Any) -> int:
    """Dispatch one argparse ``list-operator-actions`` invocation."""

    try:
        receipt, exit_code = list_operator_actions(evidence_root=args.evidence_root, passes=args.passes)
    except OperatorActionListingError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return exit_code
