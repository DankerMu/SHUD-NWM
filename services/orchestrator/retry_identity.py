"""Durable retry-attempt derivation from pipeline job identity.

The journal's clean-reservation invariant forces ``retry_count`` back to 0 on
master rows, so the only durable per-stage attempt record is the ``_retry_<n>``
suffix carried by the job id.  Production ids stack suffixes
(``..._retry_1_retry_2``), so the LAST suffix is authoritative.

This module is the single owner of that parsing; it deliberately has no
dependencies so both the DB-free journal and the scheduler read side can use it.
It also owns the manual-retry claim judgement (#1201), for the same reason: the
scheduler manifest minting point and the chain read side must share ONE
predicate, and neither may import the other's layer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

LOGGER = logging.getLogger(__name__)

RETRY_JOB_ID_MARKER = "_retry_"

MANUAL_RETRY_CLAIM_IGNORED_LOG_TOKEN = "MANUAL_RETRY_ATTEMPT_CLAIM_IGNORED"

#: Retry decision evidence field carrying the stage-scoped attempt the scheduler's
#: budget read has already charged, ``{"stage": <canonical stage>, "attempt": <n>}``
#: (#2404).  The chain mints that stage's retry at ``attempt + 1`` or later, so a
#: run_id prefix switch cannot restart the numbering.  Evidence-only: it never
#: becomes ``context.retry_attempt`` (#1201 / #2393).
RETRY_ATTEMPT_FLOOR_FIELD = "retry_attempt_floor"

#: Forecast cohort master field recording each floored member's
#: ``retry_attempt_floor`` at reservation, ``[{"model_id": str, "attempt": int}]``
#: (#2542).  First-write frozen like the other reservation provenance fields;
#: reconcile charges each member from it (:func:`member_charged_retry_attempt`).
RETRY_ATTEMPT_FLOORS_FIELD = "retry_attempt_floors"
#: Bounds of the recorded list.  Mirrors ``accepted_submit_cohort.
#: MAX_FORECAST_COHORT_MEMBERS`` and ``accepted_submit_identity.
#: MAX_ACCEPTED_SUBMIT_TEXT_LENGTH`` (kept literal so this module stays
#: dependency-free; ``tests/test_cohort_member_charge.py`` pins the member bound).
_MAX_RETRY_ATTEMPT_FLOOR_ENTRIES = 256
_MAX_RETRY_ATTEMPT_FLOOR_MODEL_ID_LENGTH = 256


def split_retry_job_identity(job_id: str | None) -> tuple[str, int]:
    """Split ``job_id`` into its retry base and the last ``_retry_<n>`` attempt.

    Ids without a parsable trailing suffix are returned unchanged with attempt 0.
    """

    text = str(job_id or "")
    if RETRY_JOB_ID_MARKER not in text:
        return text, 0
    base, attempt = text.rsplit(RETRY_JOB_ID_MARKER, maxsplit=1)
    try:
        return base, max(int(attempt), 0)
    except ValueError:
        return text, 0


def retry_suffix_attempt(job_id: str | None) -> int:
    """Return the attempt number encoded in the last ``_retry_<n>`` suffix."""

    return split_retry_job_identity(job_id)[1]


def effective_retry_attempt(job_id: str | None, recorded_count: Any = None) -> int:
    """Return the effective attempt for a job: recorded count or id suffix, whichever is higher."""

    return max(_coerce_attempt(recorded_count), retry_suffix_attempt(job_id))


def retry_attempt_floor(state_evidence: Any, stage: str) -> int | None:
    """Return the charged attempt ``state_evidence`` carries for ``stage``, or ``None``."""

    if not isinstance(state_evidence, Mapping):
        return None
    floor = state_evidence.get(RETRY_ATTEMPT_FLOOR_FIELD)
    if not isinstance(floor, Mapping) or floor.get("stage") != stage:
        return None
    attempt = floor.get("attempt")
    if type(attempt) is not int or attempt < 0:
        return None
    return attempt


def normalize_retry_attempt_floors(value: Any) -> list[dict[str, Any]]:
    """Return the durable per-member floor list: bounded, sorted and de-duplicated by ``model_id``.

    Absent / malformed input normalizes to ``[]``, which reads as "no recorded
    floors" -- the shared-charge direction, since masters written before #2542
    carry no such field.  Entries need a non-empty ``model_id`` string and a
    non-negative ``int`` (not ``bool``) ``attempt``; a repeated model keeps its
    highest floor.
    """

    if not isinstance(value, list | tuple):
        return []
    floors: dict[str, int] = {}
    for item in value[:_MAX_RETRY_ATTEMPT_FLOOR_ENTRIES]:
        if not isinstance(item, Mapping):
            continue
        model_id = item.get("model_id")
        attempt = item.get("attempt")
        if not isinstance(model_id, str) or type(attempt) is not int or attempt < 0:
            continue
        model_id = model_id.strip()[:_MAX_RETRY_ATTEMPT_FLOOR_MODEL_ID_LENGTH]
        if model_id:
            floors[model_id] = max(attempt, floors.get(model_id, 0))
    return [{"model_id": model_id, "attempt": floors[model_id]} for model_id in sorted(floors)]


def cohort_retry_attempt_floors(basins: Any, stage: str) -> list[dict[str, Any]]:
    """The normalized floors of the ``basins`` whose retry evidence carries one for ``stage``."""

    return normalize_retry_attempt_floors(
        [
            {"model_id": basin.get("model_id"), "attempt": floor}
            for basin in basins or ()
            if isinstance(basin, Mapping)
            and (floor := retry_attempt_floor(basin.get("state_evidence"), stage)) is not None
        ]
    )


def member_charged_retry_attempt(floors: Any, model_id: str, effective_attempt: int) -> int:
    """The stage attempt a cohort member is charged for its master's submission (#2542).

    ``effective_attempt`` is the master's (``effective_retry_attempt``).  With no
    recorded floors -- a legacy master, or a cohort without a floored member --
    every member is charged the shared attempt.  Otherwise the master was minted
    above the largest floor, so each member is charged its own next attempt plus
    every further attempt the master itself consumed (an inline retry of the same
    call, or an occupied id skipped forward):
    ``min(effective, own_base + (effective - (max(floors) + 1)))`` with
    ``own_base = own floor + 1`` for a recorded member and ``0`` for an unrecorded
    one.  A master that is not above its largest floor (an id reused without the
    floored minter) falls back to the shared attempt, fail-closed.
    """

    recorded = normalize_retry_attempt_floors(floors)
    if not recorded:
        return effective_attempt
    minted_floor = max(entry["attempt"] for entry in recorded) + 1
    if effective_attempt < minted_floor:
        return effective_attempt
    own_base = next((entry["attempt"] + 1 for entry in recorded if entry["model_id"] == model_id), 0)
    return min(effective_attempt, own_base + (effective_attempt - minted_floor))


def _coerce_attempt(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def has_active_manual_retry_decision(state_evidence: Any) -> bool:
    """True when this evidence's decision face carries an ACTIVE manual-retry decision.

    ``_manual_retry_state_evidence`` (the freshness-gated write point) stamps BOTH
    ``decision == "manual_retry"`` and ``reason == "manual_retry_requested"``; the
    evidence-owner face echoes the persisted ``manual_retry`` marker unconditionally
    into every decision's ``base_evidence`` and stamps neither.  Reading the decision
    face is therefore how a consumer tells an attempt claim backed by a live decision
    from a bare echo.

    The proposition is deliberately narrow: "no active manual-retry decision on THIS
    evidence", not "the marker is stale".  A higher-priority lane (e.g.
    ``resume_after_completed_stage``) returns before the manual-retry lane and carries
    the same echo, so a live marker can lawfully appear under another decision.

    Unjudgeable evidence (no ``decision``/``reason`` key) fails safe to False: dropping
    a claim degrades to the next free attempt and still submits, while honouring a
    wedged claim against an occupied terminal ``_retry_<n>`` row blocks the stage
    forever (#1201).
    """

    if not isinstance(state_evidence, Mapping):
        return False
    return state_evidence.get("decision") == "manual_retry" or state_evidence.get("reason") == "manual_retry_requested"


def log_ignored_manual_retry_attempt_claim(
    state_evidence: Mapping[str, Any],
    *,
    site: str,
    claimed_attempt: int,
    candidate_id: Any = None,
    basin_id: Any = None,
    cycle_id: Any = None,
) -> None:
    """Record a dropped manual-retry attempt claim (AC-4: never degrade silently).

    Both consumers of :func:`has_active_manual_retry_decision` emit through here so
    the two write points share one field schema.

    The message states only what the emitting site knows, and stops there.  Neither site
    can know what the stage then does: outside ``_FORCE_TERMINAL_RESUBMIT_DECISIONS`` a
    terminal failed row is RESUMED and nothing is targeted or submitted at all, and the
    chain site fires even on a pass whose direct operator field was honoured.  So the
    record says the claim is unused and says nothing about attempt targeting.
    """

    LOGGER.warning(
        "%s: manual_retry attempt claim ignored - no active manual-retry decision on this evidence; "
        "the marker-claimed attempt is not used "
        "(site=%s candidate_id=%s basin_id=%s cycle_id=%s claimed_attempt=%s decision=%s reason=%s)",
        MANUAL_RETRY_CLAIM_IGNORED_LOG_TOKEN,
        site,
        candidate_id,
        basin_id,
        cycle_id,
        claimed_attempt,
        state_evidence.get("decision"),
        state_evidence.get("reason"),
    )
