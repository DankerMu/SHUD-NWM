"""#2668: held reservations in ``list-operator-actions`` (the closed per-action table).

A ``reserved`` forecast master with no Slurm binding makes #2667 skip every
member ``active_duplicate_pipeline``, which freezes the whole source's forward
lane and never reaches ``blocked_candidates``.  So a held reservation that
restart reconcile could not resolve is read from the pass evidence
``restart_reconcile.reserved_unbound.outcomes[]`` instead, and listed by the
closed per-action table below as decision :data:`HELD_RESERVATION_DECISION`.
The table is pinned against the reconcile ``reserved_unbound`` action
vocabulary by ``tests/test_operator_action_listing_held_reservations.py``.

Kept apart from :mod:`services.orchestrator.operator_action_listing` so that
module stays under the large-file guard; the listing imports these names.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .accepted_submit_cohort import FORECAST_COHORT_STAGE_ALIASES
from .file_orchestration_journal import FileOrchestrationJournalError, _accepted_submit_source_cycle_from_job_id
from .forcing_submit_identity import FORCING_STAGE_ALIASES

HELD_RESERVATION_DECISION = "held_reservation_unresolved"
HELD_RESERVATION_RUNBOOK = "docs/runbooks/failed-basin-retry.md"
#: #2682: the operator-verified absence exit for a held forcing master.
HELD_FORCING_ABSENCE_RUNBOOK = "docs/runbooks/held-forcing-absence-exit.md"
#: The operator commands a held entry can name.  ``triage`` means "bind if sacct
#: shows the job, demote if it is confirmed dead" (runbook case 2).
HELD_RESERVATION_OPERATOR_COMMANDS = frozenset(("bind-reserved-job", "triage", "escalate"))
#: Anchor age at which a possibly-transient held action becomes an operator action.
HELD_RESERVATION_MIN_AGE = timedelta(hours=6)
#: Never listed: the row is resolved (bound, released for retry, or released
#: as identity-blocked) and no longer held.
HELD_RESERVATION_NEVER_LISTED_ACTIONS = frozenset(
    ("bound", "reservation_lost", "absence_retry_permitted", "identity_mismatch_released")
)
#: Listed whatever the anchor age: no automatic pass can resolve these.
HELD_RESERVATION_ALWAYS_LISTED_ACTIONS: Mapping[str, str] = {
    "ambiguous_fallback_match": "bind-reserved-job",
    "legacy_unversioned_read_only": "escalate",
    "multiple_matches_blocked": "escalate",
}
#: Listed once the attempt anchor is at least :data:`HELD_RESERVATION_MIN_AGE`
#: older than the pass start, or unknown (a pass written before #2668 carries no
#: anchor).
HELD_RESERVATION_AGED_LISTED_ACTIONS: Mapping[str, str] = {
    "query_unavailable": "triage",
    "fallback_no_match": "triage",
    "absence_unconfirmed": "triage",
    "identity_mismatch_blocked": "escalate",
    "stale_attempt_blocked": "escalate",
    "journal_quarantined": "escalate",
}
#: #2675: the forcing lane's own mapping.  ``bind-reserved-job`` binds a held
#: forcing master on operator-verified sacct evidence, so the two held actions
#: whose job may well have run point at it; every other forcing action (a
#: foreign owner or comment collision) is ``escalate``.  The age rules are the
#: tables above, unchanged.  "No job found" is not a listing dimension (#2682):
#: the mapping and the command set stay as they are, and the help text and the
#: runbook route that branch to ``demote-reserved-job``.
HELD_RESERVATION_FORCING_BIND_ACTIONS = frozenset(("multiple_matches_blocked", "query_unavailable"))
#: The follow-up issue an operator is pointed at when no supported exit exists:
#: the legacy unversioned master (shape (c)).
HELD_RESERVATION_LEGACY_FOLLOW_UP = "#2674"
#: The held-reservation paragraph of ``LIST_OPERATOR_ACTIONS_HELP`` (the listing's
#: only online index), rendered from the constants above.
HELD_RESERVATION_HELP = (
    "Held reservations (#2668): a held reservation restart reconcile could not "
    f"resolve is listed as decision {HELD_RESERVATION_DECISION}, read from "
    "restart_reconcile.reserved_unbound.outcomes[] of the scanned passes and "
    "deduplicated by job_id; reason is the reconcile action and operator_command is "
    f"one of {', '.join(sorted(HELD_RESERVATION_OPERATOR_COMMANDS))}: "
    "ambiguous_fallback_match -> bind-reserved-job, always; legacy_unversioned_read_only "
    "and multiple_matches_blocked -> escalate, always; query_unavailable, "
    "fallback_no_match and absence_unconfirmed -> triage, and identity_mismatch_blocked, "
    "stale_attempt_blocked and journal_quarantined -> escalate, once "
    "submission_attempt_started_at is at least 6h older than the pass started_at or is "
    "unknown; bound, reservation_lost, absence_retry_permitted and "
    "identity_mismatch_released are never listed; any other action is listed with "
    "escalate. A forcing master (#2675, job id ending in a forcing stage) gets "
    f"bind-reserved-job for {' and '.join(sorted(HELD_RESERVATION_FORCING_BIND_ACTIONS))} "
    "(same age rules) and escalate for every other action; for a held forcing master, "
    "whatever the listed command, if sacct and squeue show no master carrying its attempt "
    "comment, the exit is demote-reserved-job (#2682), see "
    f"{HELD_FORCING_ABSENCE_RUNBOOK}; a job that is neither a "
    "forecast cohort master nor a forcing master always gets escalate. Only a legacy "
    f"unversioned master carries a follow_up_issue ({HELD_RESERVATION_LEGACY_FOLLOW_UP}). "
    "An entry is dropped once a newer pass whose restart-reconcile lane ran no longer "
    "reports that job held. (5) The fifth known boundary of exit 0: held entries come "
    "only from passes whose restart-reconcile lane ran; a pass whose lane was skipped, "
    "absent or recorded reserved_unbound_error is reported under "
    "restart_reconcile_unscanned_passes, changes no exit code, and exit 0 does not vouch "
    "for held rows visible only there (held rows persist, so the next reconciling pass "
    "shows them). "
)
_HELD_RETRY_SUFFIX_RE = re.compile(r"_retry_\d+$")


def _restart_reconcile_reserved_outcomes(payload: Mapping[str, Any]) -> tuple[Sequence[Any] | None, str | None]:
    """``(outcomes, None)`` when the pass's restart-reconcile reserved lane ran, else ``(None, reason)``.

    Positive and closed: only a ``restart_reconcile`` mapping that did not skip,
    recorded no ``reserved_unbound_error`` and carries an
    ``reserved_unbound.outcomes`` sequence answers (an empty sequence answers
    "nothing held").  The bounded compaction keeps exactly these keys.
    """

    block = payload.get("restart_reconcile")
    if not isinstance(block, Mapping):
        return None, "restart_reconcile_absent"
    if block.get("status") == "skipped":
        return None, "restart_reconcile_skipped"
    if block.get("reserved_unbound_error") not in (None, ""):
        return None, "reserved_unbound_error"
    lane = block.get("reserved_unbound")
    outcomes = lane.get("outcomes") if isinstance(lane, Mapping) else None
    if not isinstance(outcomes, Sequence) or isinstance(outcomes, str | bytes | bytearray):
        return None, "reserved_unbound_outcomes_absent"
    return outcomes, None


def _pass_held_reservations(outcomes: Sequence[Any], payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The held entries one lane-ran pass lists, per the closed per-action table."""

    started_text = payload.get("started_at")
    pass_started = _parse_utc(started_text) if isinstance(started_text, str) else None
    found: list[dict[str, Any]] = []
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            continue
        job_id = outcome.get("job_id")
        action = outcome.get("action")
        if not isinstance(job_id, str) or not job_id or not isinstance(action, str) or not action:
            continue
        if action in HELD_RESERVATION_NEVER_LISTED_ACTIONS:
            continue
        anchor_text = outcome.get("submission_attempt_started_at")
        anchor_text = anchor_text if isinstance(anchor_text, str) and anchor_text else None
        if action in HELD_RESERVATION_AGED_LISTED_ACTIONS:
            anchor = _parse_utc(anchor_text) if anchor_text is not None else None
            if anchor is not None and pass_started is not None and pass_started - anchor < HELD_RESERVATION_MIN_AGE:
                continue
            command = HELD_RESERVATION_AGED_LISTED_ACTIONS[action]
        else:
            # Always-listed actions, and any action outside the table: it fails visible.
            command = HELD_RESERVATION_ALWAYS_LISTED_ACTIONS.get(action, "escalate")
        forecast_master = _held_job_is_forecast_master(job_id)
        if not forecast_master:
            # The forcing lane's own mapping (#2675); any other non-forecast id escalates.
            forcing_bind = _held_job_is_forcing_master(job_id) and action in HELD_RESERVATION_FORCING_BIND_ACTIONS
            command = "bind-reserved-job" if forcing_bind else "escalate"
        source_id, cycle_time = _held_job_source_cycle(job_id)
        entry: dict[str, Any] = {
            "decision": HELD_RESERVATION_DECISION,
            "reason": action,
            "job_id": job_id,
            "source_id": source_id,
            "cycle_time": cycle_time,
            "submission_attempt_started_at": anchor_text,
            "operator_command": command,
            "recovery_runbook": HELD_RESERVATION_RUNBOOK,
        }
        if forecast_master and action == "legacy_unversioned_read_only":
            entry["follow_up_issue"] = HELD_RESERVATION_LEGACY_FOLLOW_UP
        found.append(entry)
    return found


def _held_job_is_forecast_master(job_id: str) -> bool:
    """Whether the job id names a forecast cohort master (``job_<run>_<forecast stage>[_retry_<n>]``).

    Only a forecast cohort master takes the per-action table as is; anything
    else -- the forcing lane (:func:`_held_job_is_forcing_master`), or an id this
    can not read -- is non-forecast.
    """

    return _held_job_stage_suffix_in(job_id, FORECAST_COHORT_STAGE_ALIASES)


def _held_job_is_forcing_master(job_id: str) -> bool:
    """#2675: whether the job id names a forcing master (``job_<run>_<forcing stage>[_retry_<n>]``)."""

    return _held_job_stage_suffix_in(job_id, FORCING_STAGE_ALIASES)


def _held_job_stage_suffix_in(job_id: str, aliases: frozenset[str]) -> bool:
    """The stage-suffix rule, shared by both lanes: a readable master id ending in ``_<alias>``."""

    if _held_job_source_cycle(job_id) == (None, None):
        return False
    base = _HELD_RETRY_SUFFIX_RE.sub("", job_id)
    return any(base.endswith(f"_{alias}") for alias in aliases)


def _held_job_source_cycle(job_id: str) -> tuple[str | None, str | None]:
    """``(source_id, cycle_time)`` via the journal's accepted-submit job-id parser, else ``(None, None)``.

    The same parser the bind/reconcile path uses, so a listing never reads a
    master id differently from the journal; an id it refuses is undeterminable.
    """

    try:
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
    except FileOrchestrationJournalError:
        return None, None
    return source_id, cycle_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str) -> datetime | None:
    """Parse one writer timestamp as an aware datetime, or ``None`` (compared as datetimes, never strings)."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
