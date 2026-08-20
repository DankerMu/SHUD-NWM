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
