"""Reservation outcomes and journal transitions for reserved-unbound reconcile."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Mapping

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
    forecast_cohort_identity_is_valid,
)
from services.orchestrator.reconcile_identity import (
    _accepted_submit_versioned_job,
    _file_cohort_runtime_identity_matches,
    _is_forecast_cohort_job,
)
from services.orchestrator.reconcile_records import (
    IDENTITY_MISMATCH_RELEASED_ACTION,
    RESERVATION_LOST_STATUS,
    _as_utc_datetime,
    _callable_accepts_keyword,
    _strict_utc_datetime,
)


def _targeted_reserved_unbound_jobs(
    store: Any,
    job_id: str,
    *,
    target_submission_attempt: int | None = None,
    target_submission_attempt_started_at: datetime | str | None = None,
    target_slurm_comment: str | None = None,
) -> tuple[Any, ...]:
    """Read one current reserved row without enumerating the reconcile inventory."""

    job_id = str(job_id or "")
    if not job_id:
        return ()
    if target_submission_attempt is not None and (
        type(target_submission_attempt) is not int or target_submission_attempt < 1
    ):
        return ()
    expected_target_anchor = (
        _strict_utc_datetime(target_submission_attempt_started_at)
        if target_submission_attempt_started_at is not None
        else None
    )
    if target_submission_attempt_started_at is not None and expected_target_anchor is None:
        return ()

    direct_reader = getattr(store, "get_reserved_unbound_job", None)
    if callable(direct_reader):
        job = direct_reader(job_id)
    else:
        reader = getattr(store, "get_pipeline_job", None)
        if not callable(reader):
            return ()
        record = reader(job_id)
        job = SimpleNamespace(**dict(record)) if isinstance(record, Mapping) else record
    if (
        str(getattr(job, "job_id", "") or "") != job_id
        or str(getattr(job, "status", "") or "") != "reserved"
        or getattr(job, "slurm_job_id", None) not in (None, "")
        or not str(getattr(job, "idempotency_key", "") or "")
        or (
            target_submission_attempt is not None
            and _job_submission_attempt(job) != target_submission_attempt
        )
        or (
            expected_target_anchor is not None
            and _strict_utc_datetime(getattr(job, "submission_attempt_started_at", None))
            != expected_target_anchor
        )
        or (
            target_slurm_comment is not None
            and str(getattr(job, "slurm_comment", "") or "") != target_slurm_comment
        )
    ):
        return ()
    return (job,)


@dataclass(frozen=True)
class ReservationReconcileOutcome:
    job_id: str
    idempotency_key: str
    # "bound" | "reservation_lost" | "query_unavailable" | "absence_unconfirmed"
    # | "journal_quarantined".
    # "journal_quarantined": the FileOrchestrationJournalError branch; the only
    # producer of quarantine_reason / quarantine_field (see those fields below).
    # "absence_unconfirmed": accounting answered absent (query succeeded, no
    # matching comment row) but the reservation is younger than
    # RESERVATION_ABSENCE_GRACE, so the absence may merely be slurmdbd
    # propagation lag for a just-accepted sbatch. We keep the row reserved and
    # defer demotion to a later pass — NEVER mark reservation_lost here, which
    # would let the reserve gate reclaim+re-sbatch an in-flight job.
    action: str
    status: str
    slurm_job_id: str | None = None
    reconciliation_source: str | None = None
    reconciliation_decision: str | None = None
    matched_slurm_job_id: str | None = None
    match_count: int | None = None
    reconciliation_reason_class: str | None = None
    durable_write_kind: str | None = None
    durable_write_count: int = 0
    # Set only for "journal_quarantined": the journal error reason and the
    # offending file it named, so evidence says which cycle log is poisoned.
    quarantine_reason: str | None = None
    quarantine_field: str | None = None
    # Consecutive identity-mismatch passes recorded on the row, including this
    # one. Only the identity-mismatch outcomes carry it.
    identity_blocked_streak: int | None = None
    # #2655: pass-evidence-only name-window fallback match basis
    # (``submitline_exact`` | ``name_window_count``) on every fallback outcome
    # that classified at least one remaining master, including a bind. Never
    # persisted on the durable row.
    fallback_match_basis: str | None = None


def _job_submission_attempt(job: Any) -> int:
    try:
        return max(int(getattr(job, "submission_attempt", 1) or 1), 1)
    except (TypeError, ValueError):
        return 1


def _job_attempt_anchor(job: Any, *, accepted_submit_reconcile: bool) -> datetime | None:
    if accepted_submit_reconcile:
        # A versioned absence is authoritative only for the durable current
        # attempt. Never substitute created_at/updated_at for missing evidence.
        return _strict_utc_datetime(getattr(job, "submission_attempt_started_at", None))
    anchor = getattr(job, "updated_at", None)
    if anchor is None:
        anchor = getattr(job, "created_at", None)
    return _as_utc_datetime(anchor)


def _record_file_reconciliation(
    store: Any,
    job: Any,
    decision: str,
    *,
    reason_class: str | None = None,
    identity_blocked_streak: int = 0,
) -> int:
    return _transition_file_reconciliation(
        store,
        job,
        AcceptedSubmitTransition.accounting(
            decision,
            submit_outcome="submit_result_ambiguous",
            reconciliation_reason_class=reason_class,
            identity_blocked_streak=identity_blocked_streak,
        ),
    )


def _job_identity_blocked_streak(job: Any) -> int:
    try:
        return max(int(getattr(job, "identity_blocked_streak", 0) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _identity_blocked_release_anchor(job: Any) -> datetime | None:
    """Return the release grace anchor for one reserved-unbound row.

    The counter's own writes refresh ``updated_at`` every pass, so anchoring the
    release on ``updated_at`` would push the exit out forever. The attempt start
    is immutable for the attempt; ``created_at`` is the only fallback.
    """

    anchor = _strict_utc_datetime(getattr(job, "submission_attempt_started_at", None))
    if anchor is not None:
        return anchor
    return _as_utc_datetime(getattr(job, "created_at", None))


def _identity_blocked_outcome(
    store: Any,
    job: Any,
    *,
    idempotency_key: str,
    match_count: int | None = None,
    identity_blocked_streak_limit: int | None,
    grace: timedelta,
    accepted_submit_grace: timedelta | None,
    now: Callable[[], datetime],
) -> ReservationReconcileOutcome:
    """Record one identity-mismatch pass and release the row once it converges.

    Shared by all three reserved-unbound identity-mismatch writer sites so the
    counter, the threshold and the outcome shape have exactly one definition.
    Branch conditions and their order at the call sites are unchanged.
    """

    limit = identity_blocked_streak_limit
    exit_enabled = type(limit) is int and limit > 0
    current = _job_identity_blocked_streak(job)
    # Saturate: once the counter has reached the limit (or the exit is
    # disabled) further passes must not append unbounded journal growth.
    streak = min(current + 1, limit) if exit_enabled else current
    write_count = _record_file_reconciliation(
        store,
        job,
        "identity_mismatch_blocked",
        identity_blocked_streak=streak,
    )
    if exit_enabled and streak >= limit:
        anchor = _identity_blocked_release_anchor(job)
        release_grace = accepted_submit_grace if accepted_submit_grace is not None else grace
        if anchor is not None and now() - anchor >= release_grace:
            release_write_count = _release_identity_blocked_reservation(
                store,
                job,
                identity_blocked_streak=streak,
            )
            if release_write_count:
                return ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=idempotency_key,
                    action=IDENTITY_MISMATCH_RELEASED_ACTION,
                    status=RESERVATION_LOST_STATUS,
                    reconciliation_source="slurm_exact_comment",
                    reconciliation_decision=IDENTITY_MISMATCH_RELEASED_ACTION,
                    match_count=match_count,
                    durable_write_kind="identity_blocked_release",
                    durable_write_count=write_count + release_write_count,
                    identity_blocked_streak=streak,
                )
    return ReservationReconcileOutcome(
        job_id=job.job_id,
        idempotency_key=idempotency_key,
        action="identity_mismatch_blocked",
        status=str(job.status),
        reconciliation_source="slurm_exact_comment",
        reconciliation_decision="identity_mismatch_blocked",
        match_count=match_count,
        durable_write_kind="pipeline_job_reconciliation" if write_count else None,
        durable_write_count=write_count,
        identity_blocked_streak=streak,
    )


def _release_identity_blocked_reservation(store: Any, job: Any, *, identity_blocked_streak: int) -> int:
    releaser = getattr(store, "release_identity_blocked_reservation", None)
    if not callable(releaser):
        return 0
    for name in (
        "accepted_submit_contract_version",
        "expected_submission_attempt_started_at",
        "identity_blocked_streak",
    ):
        if not _callable_accepts_keyword(releaser, name):
            return 0
    return int(
        releaser(
            job.job_id,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=_job_submission_attempt(job),
            expected_submission_attempt_started_at=_strict_utc_datetime(
                getattr(job, "submission_attempt_started_at", None)
            ),
            expected_status=str(job.status),
            identity_blocked_streak=identity_blocked_streak,
        )
    )


def _transition_forcing_submit_ambiguity(store: Any, job: Any) -> int:
    """Persist a pre-outcome forcing reservation as unknown before querying it."""

    transitioner = getattr(store, "transition_pipeline_job_submit_evidence", None)
    if not callable(transitioner):
        return 0
    result = transitioner(
        job.job_id,
        AcceptedSubmitTransition.timeout(status=str(job.status)),
        expected_submission_attempt=_job_submission_attempt(job),
        expected_statuses=(str(job.status),),
        require_unbound=getattr(job, "slurm_job_id", None) in (None, ""),
    )
    return int(getattr(result, "wrote", False))


def _transition_file_reconciliation(
    store: Any,
    job: Any,
    transition: AcceptedSubmitTransition,
) -> int:
    transitioner = getattr(store, "transition_pipeline_job_submit_evidence", None)
    if callable(transitioner):
        transition_kwargs: dict[str, Any] = {
            "expected_submission_attempt": _job_submission_attempt(job),
            "expected_statuses": (str(job.status),),
            "require_unbound": getattr(job, "slurm_job_id", None) in (None, ""),
        }
        if _callable_accepts_keyword(transitioner, "accepted_submit_contract_version"):
            transition_kwargs["accepted_submit_contract_version"] = ACCEPTED_SUBMIT_CONTRACT_VERSION
        result = transitioner(
            job.job_id,
            transition,
            **transition_kwargs,
        )
        return int(getattr(result, "wrote", False))
    return 0


def _accepted_submit_reconcile_job(
    store: Any,
    job: Any,
    *,
    comment_capability: bool | None = None,
    comment_capability_provider: Callable[[], bool | None] | None = None,
) -> bool:
    identity = vars(job) if hasattr(job, "__dict__") else {}
    return bool(
        getattr(store, "supports_accepted_submit_reconcile", False)
        and _accepted_submit_versioned_job(job)
        and _is_forecast_cohort_job(job)
        and forecast_cohort_identity_is_valid(identity)
        and (
            # Fix 1: EVERY current-contract valid forecast row — pre-outcome
            # (submit_outcome is None) included — resolves the pass capability
            # lazily BEFORE the runtime lane is decided. An explicitly
            # comment-less cluster permits the fallback lane and evaluates
            # runtime later (held/streak-zero); True/unknown keeps the
            # ordinary exact-comment streak behavior. A pre-outcome row must
            # land in the same lane as an already-timeout row, so disposition
            # never depends on whether the timeout was already persisted.
            _accepted_submit_runtime_lane_eligible(
                store,
                identity,
                comment_capability=comment_capability,
                comment_capability_provider=comment_capability_provider,
            )
            is not False
        )
    )


def _accepted_submit_runtime_lane_eligible(
    store: Any,
    identity: Mapping[str, Any],
    *,
    comment_capability: bool | None = None,
    comment_capability_provider: Callable[[], bool | None] | None = None,
) -> bool | None:
    """Tri-state runtime-lane capability: comment-storing/unknown vs fallback.

    Returns ``True`` (the current-contract forecast reservation may proceed on
    its accounting lane), ``False`` (its durable runtime identity does not
    match on the exact-comment lane), or ``None`` (capability unknown: no lane
    may issue and the row stays held). The verdict is driven by the querier's
    typed capability verdict, which is the same source the name-window fallback
    gate uses (#1850 D2):

    - ``False`` (explicitly comment-less cluster): the fallback lane runs, so
      a runtime failure is evaluated later inside the fallback path and stays
      in the held-tuple family with streak zero.
    - ``True`` or unknown: the exact-comment lane runs, and its runtime
      identity gate stays the durable validator (pre-PR semantics).

    The verdict is resolved lazily through ``comment_capability_provider`` when
    the exact-comment validator would otherwise be consulted, so an empty or
    unversioned-only reserved inventory never triggers the capability probe
    (#1850 Fix D).
    """

    if comment_capability is None and callable(comment_capability_provider):
        comment_capability = comment_capability_provider()
    if comment_capability is False:
        # Explicitly comment-less: the name-window fallback MAY run regardless
        # of the durable runtime rows; a fallback runtime failure never enters
        # the durable identity-blocked streak.
        return True
    return _file_cohort_runtime_identity_matches(store, identity)
