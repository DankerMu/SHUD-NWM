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
off, filters naming a subset, or ``--source`` naming a subset of
:data:`SCOPE_COMPLETE_SOURCES`) answers only for its own scope, so it can not
say "nothing waits" either: it is non-evaluating with reason
:data:`SCOPE_NARROWED_REASON` and leaves the hidden-pass flag exactly as it
found it, while a pass whose scope keys are missing (:data:`SCOPE_UNKNOWN_REASON`)
arms it.  One candidate seen in several passes is listed once, and apart from
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
#: subset of models/basins/an expression, or ``--source`` naming a subset of
#: :data:`SCOPE_COMPLETE_SOURCES`) listed nothing only INSIDE ITS OWN
#: SCOPE, and the breaker-released entries of one of the listed decisions are
#: produced on the backfill leg alone.  It is therefore non-evaluating -- but it
#: LEAVES the hidden-pass flag as it found it: it may not clear it (it did not
#: look everywhere) and it may not arm it (the narrowing was the operator's own
#: instruction and hides nothing unexpectedly).
SCOPE_NARROWED_REASON = "scope_narrowed"
#: An otherwise-evaluating pass whose ``backfill``, ``operator_filters`` or
#: ``sources`` key is missing -- or present but PARTIAL, missing one of the
#: sub-keys the scope test reads, or of a shape the writer can not produce:
#: its scope can not be read, so it is not assumed scope-complete.  This
#: is the same class of uncertainty as a size fallback -- the pass MAY have
#: evaluated candidates it can not show -- so it ARMS the flag (positionally: a
#: newer scope-complete pass clears it again), and is not a global veto the way a
#: dropped candidate list is.
SCOPE_UNKNOWN_REASON = "scope_unknown"

#: Every decision the db-free scheduler writes ``manual_retry_required: True`` on.
#: Literals, not imports: this surface stays db-free and importer-free, so the
#: closure pin in ``tests/test_operator_action_listing.py`` reads the two writer
#: modules with ``ast`` instead.  ``blocked_operator_reentry_restart_stage_refused``
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
#: A LOCAL literal on purpose: the authority is ``scheduler.py``'s
#: ``DEFAULT_PRODUCTION_SOURCES``, but that module carries 35 top-level imports and
#: the lease-compat facade, and this listing surface must stay db-free and cheap to
#: import.  ``tests/test_operator_action_listing.py`` pins the three spellings
#: (here, ``scheduler.py``, and the ``cli.py`` fallback) against each other.
SCOPE_COMPLETE_SOURCES = ("gfs", "IFS")

LIST_OPERATOR_ACTIONS_HELP = (
    "List candidates waiting on an operator (#1186): permanent_failure, "
    "cancelled_manual_retry_required, blocked_strict_warm_start_init_state_mismatch, "
    "blocked_journal_predecessor_identity_quarantine, "
    "blocked_operator_reentry_restart_stage_refused. Read-only: scans the newest "
    "--passes terminal scheduler pass evidence files under --evidence-root "
    f"(default ${EVIDENCE_ROOT_ENV}). Exit 1 when actions are listed, 0 when none "
    "and at least one scanned pass evaluated candidates over the whole scope, 3 when "
    "none but undecidable (a pass dropped its candidate lists, no scanned pass is "
    "readable, evaluating and scope-complete, or a pass that is neither evaluating "
    "nor transparent -- size-fallback, unreadable, lease_lost, resource_limit_blocked, "
    "unknown status; transparent = lock_contended, preflight_blocked -- is newer than "
    "the newest evaluating pass; see non_evaluating_passes / unreadable_passes; an "
    "empty root counts, a size-fallback pass never counts as evaluating because "
    "its source_cycles were dropped, and a pass file that vanished between the "
    "directory scan and its stat is reported under unreadable_passes and vetoes "
    "exit 0 wherever it sat). A pass the operator narrowed (backfill disabled, "
    "--model-id/--basin-id filters, or --source naming a subset of gfs/IFS) is "
    "reported with reason scope_narrowed and never counts as evaluating: it "
    "answered only for its own scope. Exit 2 when the root is missing or "
    "unreadable, or --passes is not an integer >= 1. Runbook: "
    "docs/runbooks/node22-control-plane-manual-recovery.md"
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
            # ``scope_unknown`` is NOT exempt -- it arms, like every other pass
            # that may have evaluated candidates it can not show.
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

    Takes the whole payload because the scope test (r5-01) reads three of its
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
_REQUIRED_OPERATOR_FILTER_KEYS = ("basin_ids", "model_ids", "expression")


def _scope_reason(payload: Mapping[str, Any]) -> str | None:
    """``None`` when the pass looked everywhere, else why it can not answer for everywhere.

    The dividing line is KEY PRESENCE, and it is the whole point of the two
    reasons: a key that is THERE carrying a narrowing value is the operator's own
    instruction (``scope_narrowed``, which hides nothing unexpectedly and so
    leaves the hidden-pass flag), while a key that is MISSING means the scope can
    not be read at all (``scope_unknown``, the same uncertainty as a size
    fallback, so it arms).  A partial mapping -- ``operator_filters: {}``, or one
    missing just ``expression`` -- must therefore NOT fall through to the value
    test: every absent key would read as an empty default and the pass would be
    called scope-complete, clearing the flag and permitting exit 0 off a pass
    whose scope nobody established.  That is r5-01's own failure mode through a
    different door.

    Within a mapping whose keys are all present, scope is read from the filter
    VALUES, never from the mapping's size: a normal production pass always writes
    the four keys at their empty defaults (measured on all 169 live node-22
    passes, 2026-09-16), so a rule keyed on mapping-emptiness would call every
    production pass narrowed and never reach exit 0.

    ``sources`` is the THIRD narrowing dimension, on exactly the same footing:
    ``plan-production --source gfs`` lands as the top-level ``sources`` list
    (``scheduler_evidence.py:268``) and such a pass never looked at IFS.  It can
    not be judged by emptiness -- ``cli.py:421`` makes the list non-empty always,
    defaulting to the whole set -- so it is compared against
    :data:`SCOPE_COMPLETE_SOURCES` as a set.
    """

    backfill = payload.get("backfill")
    operator_filters = payload.get("operator_filters")
    sources = payload.get("sources")
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
    if backfill["enabled"] is not True:
        return SCOPE_NARROWED_REASON
    if operator_filters["basin_ids"] or operator_filters["model_ids"]:
        return SCOPE_NARROWED_REASON
    if operator_filters["expression"] is not None:
        return SCOPE_NARROWED_REASON
    if set(sources) != set(SCOPE_COMPLETE_SOURCES):
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
                recorded_init_state_id=identity.get("recorded_init_state_id"),
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
