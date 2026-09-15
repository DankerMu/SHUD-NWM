"""``list-operator-actions`` (#1186): enumerate manual-action decisions from pass evidence.

The db-free scheduler writes ``manual_retry_required: true`` on four terminal
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
the window undecidable.  Exit codes: ``1`` actions listed, ``0`` none, ``3``
none but undecidable (a scanned pass dropped its candidate lists, no scanned
pass is readable and evaluating -- including an empty root --, or a pass that
is neither decidable nor transparent is newer than the newest decidable pass),
``2`` evidence root missing or unreadable.
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

OPERATOR_ACTION_DECISIONS = frozenset(
    (
        "permanent_failure",
        "cancelled_manual_retry_required",
        "blocked_strict_warm_start_init_state_mismatch",
        BREAKER_DECISION,
    )
)

LIST_OPERATOR_ACTIONS_HELP = (
    "List candidates waiting on an operator (#1186): permanent_failure, "
    "cancelled_manual_retry_required, blocked_strict_warm_start_init_state_mismatch, "
    "blocked_journal_predecessor_identity_quarantine. Read-only: scans the newest "
    "--passes terminal scheduler pass evidence files under --evidence-root "
    f"(default ${EVIDENCE_ROOT_ENV}). Exit 1 when actions are listed, 0 when none "
    "and at least one scanned pass evaluated candidates, 3 when none but undecidable "
    "(a pass dropped its candidate lists, no scanned pass is readable and "
    "evaluating, or a pass that is neither evaluating nor transparent -- "
    "size-fallback, unreadable, lease_lost, resource_limit_blocked, unknown status; "
    "transparent = lock_contended, preflight_blocked -- is newer than the newest "
    "evaluating pass; see non_evaluating_passes / unreadable_passes; an empty root "
    "counts, and a size-fallback pass never counts as evaluating because its "
    "source_cycles were dropped), 2 when the root is missing or unreadable. Runbook: "
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
    selected = _newest_pass_files(root, passes)

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
        non_evaluating_entry = _non_evaluating_entry(name, payload.get("status"), limit)
        if non_evaluating_entry is None:
            hidden_after_decidable = False
        else:
            non_evaluating.append(non_evaluating_entry)
            status = payload.get("status")
            # A size fallback's raw status is ``resource_limit_blocked``: never transparent.
            if not (isinstance(status, str) and status in TRANSPARENT_PASS_STATUSES):
                hidden_after_decidable = True
        if limit.get("candidate_lists") == "dropped":
            dropped.append(name)
        for action in _pass_actions(payload):
            key = (action["source_id"] or "", action["cycle_time"] or "", action["model_id"] or "", action["decision"])
            existing = actions.get(key)
            if existing is None:
                actions[key] = {**action, "first_seen_pass": name, "last_seen_pass": name, "seen_in_passes": 1}
                continue
            if existing["candidate_id"] is None:
                existing["candidate_id"] = action["candidate_id"]
            existing["last_seen_pass"] = name
            existing["seen_in_passes"] += 1

    listed = [actions[key] for key in sorted(actions)]
    receipt = {
        "schema_version": LIST_OPERATOR_ACTIONS_SCHEMA_VERSION,
        "evidence_root": str(root),
        "passes_scanned": len(selected),
        "unreadable_passes": sorted(unreadable),
        "operator_action_count": len(listed),
        "operator_actions": listed,
        "candidate_lists_dropped_passes": sorted(dropped),
        "non_evaluating_passes": sorted(non_evaluating, key=lambda item: item["pass"]),
    }
    evaluating_count = len(selected) - len(unreadable) - len(non_evaluating)
    if listed:
        return receipt, 1
    if dropped or evaluating_count < 1 or hidden_after_decidable:
        return receipt, 3
    return receipt, 0


def _non_evaluating_entry(name: str, status: Any, limit: Mapping[str, Any]) -> dict[str, Any] | None:
    """``None`` for an evaluating pass, else its ``non_evaluating_passes`` entry."""

    if status == _SIZE_FALLBACK_STATUS and limit.get("candidate_lists") in _SIZE_FALLBACK_CANDIDATE_LISTS:
        kept = limit.get("pre_limit_status")
        return {
            "pass": name,
            "status": str(kept) if kept not in (None, "") else _SIZE_FALLBACK_STATUS,
            "reason": SIZE_FALLBACK_NON_EVALUATING_REASON,
        }
    if isinstance(status, str) and status in EVALUATING_PASS_STATUSES:
        return None
    return {
        "pass": name,
        "status": status if isinstance(status, str) and status else None,
        "reason": STATUS_NOT_EVALUATING_REASON,
    }


def _newest_pass_files(root: Path, passes: int) -> list[tuple[str, Path]]:
    """Top-level terminal pass files, newest ``passes`` by mtime (uuid suffixes are not time-ordered)."""

    entries: list[tuple[int, str, Path]] = []
    try:
        with os.scandir(root) as iterator:
            for entry in iterator:
                name = entry.name
                if not is_scheduler_pass_evidence_filename(name) or name.endswith(_PRE_EXECUTION_SUFFIX):
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                entries.append((entry.stat(follow_symlinks=False).st_mtime_ns, name, Path(entry.path)))
    except OSError as error:
        raise OperatorActionListingError(
            f"evidence root unreadable: {type(error).__name__}: {root}"
        ) from error
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(name, path) for _mtime, name, path in entries[:passes]]


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
