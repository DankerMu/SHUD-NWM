"""Durable retry-attempt derivation from pipeline job identity.

The journal's clean-reservation invariant forces ``retry_count`` back to 0 on
master rows, so the only durable per-stage attempt record is the ``_retry_<n>``
suffix carried by the job id.  Production ids stack suffixes
(``..._retry_1_retry_2``), so the LAST suffix is authoritative.

This module is the single owner of that parsing, and of the next-attempt
formulas built on it (stage-scoped, floored, inline, reservation, current-master,
non-master and manual paths) plus the cohort restart stage; it deliberately has
no dependencies so both the DB-free journal and the scheduler read side can use
it.  Its functions take plain data: callers keep their own topology (stage
matching, stage canonicalization) and their own occupancy probe (``is_occupied``).
It also owns the manual-retry claim judgement (#1201), for the same reason: the
scheduler manifest minting point and the chain read side must share ONE
predicate, and neither may import the other's layer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
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


def retry_job_id(base: str, attempt: int) -> str:
    """Format the retry job id ``<base>_retry_<n>`` with ``n = max(int(attempt), 1)``.

    ``base`` is used as given (not stripped), so a stacked base stays stacked.
    """

    return f"{base}{RETRY_JOB_ID_MARKER}{max(int(attempt), 1)}"


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


def next_stage_attempt(job_ids: Iterable[Any], base_job_id: str) -> int:
    """The next stage-scoped attempt: one past the largest positive suffix under ``base_job_id``.

    The LAST ``_retry_<n>`` suffix is the attempt (stacked producer ids, #2254).
    Suffix only, not ``retry_count``: an unparsable tail yields 0 and stays out of
    the numbering.
    """

    prefix = f"{base_job_id}{RETRY_JOB_ID_MARKER}"
    attempts: list[int] = []
    for job_id in job_ids:
        text = str(job_id or "")
        if not text.startswith(prefix):
            continue
        attempt = retry_suffix_attempt(text)
        if attempt > 0:
            attempts.append(attempt)
    return max(attempts, default=0) + 1


def mint_attempt(floors: Sequence[int], is_occupied: Callable[[int], bool]) -> int:
    """The floored mint: one past the largest floor, skipped forward past every occupied attempt.

    ``floors`` must be non-empty.  ``is_occupied`` is called with each candidate
    attempt in increasing order until it returns false (#1201: never reuse).
    """

    attempt = max(floors) + 1
    while is_occupied(attempt):
        attempt += 1
    return attempt


def inline_retry_attempt(retry_count: int, suffix_attempt: int, failure_number: int) -> int:
    """The inline (same call) auto-retry attempt of a current master."""

    return max(retry_count, suffix_attempt, failure_number - 1) + 1


def reservation_submission_attempt(pipeline_job_id: str, retry_attempt: Any) -> int:
    """The reservation's ``submission_attempt``: the claim (at least 1), raised past the id's last suffix.

    A non-int claim raises ``ValueError``; a non-int suffix tail is ignored.  The
    tail is parsed with ``rsplit`` + ``int``, not :func:`retry_suffix_attempt`
    (no 0 floor; unobservable through the outer ``max``).
    """

    submission_attempt = max(int(retry_attempt or 1), 1)
    if RETRY_JOB_ID_MARKER in pipeline_job_id:
        try:
            submission_attempt = max(
                submission_attempt,
                int(pipeline_job_id.rsplit(RETRY_JOB_ID_MARKER, maxsplit=1)[1]) + 1,
            )
        except ValueError:
            pass
    return submission_attempt


def next_effective_attempt(job_id: Any, recorded_count: Any) -> int:
    """One past :func:`effective_retry_attempt` (the current-master and manual paths)."""

    return effective_retry_attempt(job_id, recorded_count) + 1


def next_after_failure_master(job_id: Any, recorded_count: Any) -> tuple[str, int]:
    """The current master's next retry identity: the stripped base, at the next effective attempt."""

    text = str(job_id or "")
    base, _suffix_attempt = split_retry_job_identity(text)
    count = next_effective_attempt(text, recorded_count)
    return retry_job_id(base, count), count


def next_after_failure_non_master(job_id: Any, retry_count: Any) -> tuple[str, int]:
    """A non-master auto retry's identity: ``job_id`` unstripped (stacks), count unclamped.

    Deliberately not :func:`retry_job_id` / ``_coerce_attempt``: this path differs
    from the master path today (#3319 owns any behaviour change).
    """

    count = int(retry_count or 0) + 1
    return f"{job_id}{RETRY_JOB_ID_MARKER}{count}", count


def restart_stage_from_basins(restart_stages: Iterable[str | None], stage_order: Sequence[str]) -> str | None:
    """The earliest of the non-``None`` canonical ``restart_stages`` in ``stage_order``, or ``None``.

    ``min`` is intentional: the cohort starts at the EARLIEST claim among the
    members that carry a marker (run more, never less).  A stage missing from
    ``stage_order`` sorts last.
    """

    stages = [stage for stage in restart_stages if stage is not None]
    if not stages:
        return None
    order = {name: index for index, name in enumerate(stage_order)}
    return min(stages, key=lambda stage: order.get(stage, len(order)))


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
    ``min(effective, own floor + 1 + (effective - (max(floors) + 1)))``.  An
    unrecorded member is a forecast restart without a budget floor (manifest-missing,
    §8.7 quarantine, missing-output recompute) that may already carry forecast
    history, so it is charged the shared attempt, fail-closed.  So is every member
    of a master that is not above its largest floor (an id reused without the
    floored minter).
    """

    recorded = normalize_retry_attempt_floors(floors)
    if not recorded:
        return effective_attempt
    minted_floor = max(entry["attempt"] for entry in recorded) + 1
    own_floor = next((entry["attempt"] for entry in recorded if entry["model_id"] == model_id), None)
    if own_floor is None or effective_attempt < minted_floor:
        return effective_attempt
    return min(effective_attempt, own_floor + 1 + (effective_attempt - minted_floor))


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
