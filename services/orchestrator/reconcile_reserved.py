"""Reserved-unbound reconcile (``reconcile_reserved_unbound_jobs``)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from services.orchestrator.accepted_submit_identity import forecast_cohort_identity_is_valid
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
from services.orchestrator.reconcile_comment_proof import _comment_query_capability
from services.orchestrator.reconcile_identity import (
    _expected_job_name_token,
    _fallback_comment_allowed,
    _file_cohort_runtime_identity_matches,
    _forcing_accounting_identity_matches,
    _identity_matches,
)
from services.orchestrator.reconcile_records import (
    JOURNAL_QUARANTINED_ACTION,
    RESERVATION_ABSENCE_GRACE,
    CommentSacctQuerier,
    SacctRecord,
)
from services.orchestrator.reconcile_reserved_phases import (
    _reserved_accounting_query_phase,
    _reserved_commit_bind_phase,
    _reserved_confirmed_absence_phase,
    _reserved_final_comment_guard_phase,
    _reserved_forcing_absence_phase,
    _reserved_lane_phase,
    _reserved_proof_kind_phase,
)
from services.orchestrator.reconcile_reserved_support import (
    ReservationReconcileOutcome,
    _identity_blocked_outcome,
    _record_file_reconciliation,
    _ReservedPassContext,
    _ReservedRowState,
    _targeted_reserved_unbound_jobs,
)
from services.orchestrator.reservation import idempotency_key_from_comment
from services.slurm_gateway.real_backend import SLURM_JOB_ID_RE

LOGGER = logging.getLogger("services.orchestrator.reconcile")


def reconcile_reserved_unbound_jobs(
    store,
    *,
    comment_query: CommentSacctQuerier,
    grace: timedelta = RESERVATION_ABSENCE_GRACE,
    accepted_submit_grace: timedelta | None = None,
    identity_blocked_streak_limit: int | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    target_job_id: str | None = None,
    target_submission_attempt: int | None = None,
    target_submission_attempt_started_at: datetime | str | None = None,
    target_slurm_comment: str | None = None,
    bind_only: bool = False,
) -> list[ReservationReconcileOutcome]:
    """Reconcile the submit-crash window: reserved rows with no slurm_job_id.

    A crash after ``sbatch`` accepted a job but before the durable bind leaves a
    ``status='reserved'`` row whose ``slurm_job_id`` is NULL. We recover the real
    ``slurm_job_id`` by querying accounting for the job sbatch recorded under
    ``--comment=nhms_idem:<idempotency_key>`` and bind it — never blindly
    re-submitting, so at most one job per idempotency_key enters submitted.

    If accounting *confirms* there is no job for that comment, sbatch did not
    actually take; we mark the reservation ``reservation_lost`` (typed), leaving
    a later pass free to reserve+submit again under the same idempotency_key.

    Tri-state, NOT binary: a transient query failure (``sacct`` timed out /
    exited non-zero, surfaced as ``ReconcileQueryUnavailable``) is NOT an
    absence. We must not read it as ``reservation_lost`` — that would free a
    later pass to re-reserve+re-sbatch a job that is in fact in flight (double
    submission). On a transient failure we keep the row ``reserved`` and let a
    later reconcile pass retry; only a *confirmed* absence marks
    ``reservation_lost``.

    Even a *confirmed* absence is gated by a minimum survival window
    (``grace``): sacct/slurmdbd accounting propagation lags sbatch acceptance,
    so a job just accepted can be momentarily invisible to the comment query
    (returncode 0, no matching row). Demoting such a young reservation to
    ``reservation_lost`` would let the reserve gate reclaim it and re-sbatch the
    very job that is in fact in flight — the same double-submit failure mode as
    the transient case. So a reserved-unbound row younger than ``grace`` whose
    comment query confirms absence is emitted ``absence_unconfirmed`` and left
    ``reserved`` for a later pass (where accounting will have caught up and
    either binds it or, past grace, demotes it). A row with no ``created_at`` to
    prove youth, or one already older than ``grace``, keeps the old behavior and
    demotes to ``reservation_lost`` so liveness never regresses. ``now`` is
    injectable for deterministic tests.

    ``target_job_id`` selects one current row through its direct reader instead
    of enumerating the reserved inventory. Its optional attempt, anchor, and
    comment pins must still match that row before any accounting query. With
    ``bind_only=True``, only a positive trusted forcing bind may write; absence
    stays reserved for the normal global reconciliation path.

    ``identity_blocked_streak_limit`` converges the third failure mode: a
    reserved-unbound row whose identity can never be verified was recorded
    ``identity_mismatch_blocked`` every pass while STAYING ``reserved``, so the
    cycle kept failing with ``PIPELINE_ALREADY_ACTIVE``. With a positive limit,
    the durable consecutive-blocked counter (reset by any other accounting
    transition) releases the row into ``reservation_lost`` once it reaches the
    limit and the attempt is past its grace. ``None``/``<= 0`` disables the
    exit and preserves today's behaviour exactly.
    """

    outcomes: list[ReservationReconcileOutcome] = []
    # #1850 D2 + Fix D: capability probing is LAZY. Select either the global
    # reserved snapshot or one direct current row first; the querier's cached
    # tri-state verdict is computed at most once only when a row needs lane
    # selection. An empty selection never triggers an accounting query. One
    # stable verdict serves the whole invocation.
    reserved_jobs = (
        list(store.query_reserved_unbound_jobs())
        if target_job_id is None
        else list(
            _targeted_reserved_unbound_jobs(
                store,
                target_job_id,
                target_submission_attempt=target_submission_attempt,
                target_submission_attempt_started_at=target_submission_attempt_started_at,
                target_slurm_comment=target_slurm_comment,
            )
        )
    )
    comment_capability: bool | None = None
    comment_capability_known = False

    def _pass_comment_capability() -> bool | None:
        nonlocal comment_capability, comment_capability_known
        if not comment_capability_known:
            comment_capability = _comment_query_capability(comment_query)
            comment_capability_known = True
        return comment_capability

    context = _ReservedPassContext(
        store=store,
        comment_query=comment_query,
        grace=grace,
        accepted_submit_grace=accepted_submit_grace,
        identity_blocked_streak_limit=identity_blocked_streak_limit,
        now=now,
        bind_only=bind_only,
        target_submission_attempt=target_submission_attempt,
        target_submission_attempt_started_at=target_submission_attempt_started_at,
        target_slurm_comment=target_slurm_comment,
        pass_comment_capability=_pass_comment_capability,
    )

    for job in reserved_jobs:
        idempotency_key = job.idempotency_key
        if not idempotency_key:
            continue
        # Journal capacity/integrity faults are per-cycle. Quarantine the
        # offending row with its evidence instead of letting one poisoned
        # cycle abort resolution of every other reserved row and the pass.
        # #2655: the match basis of a classified fallback also rides on a
        # journal quarantine raised after classification (commit-time scan);
        # the row state carries it out of the per-row call.
        row = _ReservedRowState(idempotency_key=idempotency_key)
        try:
            outcomes.append(_reconcile_one_reserved_unbound_job(job, context=context, row=row))
        except FileOrchestrationJournalError as error:
            LOGGER.warning(
                "reconcile reservation quarantined for idempotency_key=%s: %s (%s)",
                idempotency_key,
                error.reason,
                error.field,
            )
            # Every per-row exit returns exactly one outcome and a raise returns
            # none, so this is the row's only outcome (the monolith's guard
            # ``len(outcomes) == outcome_floor`` was always true here).
            outcomes.append(
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action=JOURNAL_QUARANTINED_ACTION,
                    status=str(job.status),
                    quarantine_reason=error.reason,
                    quarantine_field=error.field,
                    fallback_match_basis=row.fallback_basis,
                )
            )

    return outcomes


def _reconcile_one_reserved_unbound_job(
    job, *, context: _ReservedPassContext, row: _ReservedRowState
) -> ReservationReconcileOutcome:
    """Run one keyed reserved row through the ordered phases; the first outcome wins.

    Each phase returns the row's outcome or ``None`` to fall through, so a row
    yields exactly one outcome or raises. ``FileOrchestrationJournalError``
    reaches the caller's per-row quarantine; any other exception (the commit or
    bind API ``ReconcileQueryUnavailable`` included) aborts the whole pass.
    """

    for phase in (
        _reserved_lane_phase,
        _reserved_accounting_query_phase,
        _reserved_proof_kind_phase,
        _reserved_identity_gate_phase,
        _reserved_forcing_absence_phase,
        _reserved_confirmed_absence_phase,
        _reserved_final_comment_guard_phase,
    ):
        outcome = phase(job, context=context, row=row)
        if outcome is not None:
            return outcome
    return _reserved_commit_bind_phase(job, context=context, row=row)


def _reserved_identity_gate_phase(
    job, *, context: _ReservedPassContext, row: _ReservedRowState
) -> ReservationReconcileOutcome | None:
    """Record selection and identity gates (the ``_reserved_record_identity_matches`` seam caller)."""

    store = context.store
    grace = context.grace
    accepted_submit_grace = context.accepted_submit_grace
    identity_blocked_streak_limit = context.identity_blocked_streak_limit
    now = context.now
    idempotency_key = row.idempotency_key
    forcing_submit_reconcile = row.forcing_submit_reconcile
    accepted_submit_reconcile = row.accepted_submit_reconcile
    proof = row.proof
    forcing_reconciliation_source = row.forcing_reconciliation_source
    record = (
        proof.records[0]
        if proof.records
        and (
            proof.kind in {"owned_match", "fallback_unique"}
            or (forcing_submit_reconcile and proof.kind == "controller_match")
            or (not accepted_submit_reconcile and not forcing_submit_reconcile)
        )
        else None
    )
    fallback_unique = accepted_submit_reconcile and proof.kind == "fallback_unique"
    fallback_basis = row.fallback_basis = proof.fallback_match_basis if fallback_unique else None
    if (
        accepted_submit_reconcile
        and record is not None
        and not _reserved_record_identity_matches(
            store,
            record,
            job,
            str(idempotency_key),
            fallback_unique=fallback_unique,
        )
    ):
        # A unique name-window candidate that fails a remaining gate is
        # ``identity_mismatch_blocked`` with count 1 and NO
        # identity-mismatch durable transition: fallback failures never
        # enter the identity-blocked release ladder (#1565 D3).
        if fallback_unique:
            write_count = _record_file_reconciliation(
                store,
                job,
                "accounting_unavailable",
                reason_class="comment_accounting_unproven",
            )
            return (
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="identity_mismatch_blocked",
                    status=str(job.status),
                    reconciliation_source="slurm_exact_comment",
                    reconciliation_decision="accounting_unavailable",
                    reconciliation_reason_class="comment_accounting_unproven",
                    match_count=1,
                    durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                    durable_write_count=write_count,
                    fallback_match_basis=fallback_basis,
                )
            )
        return (
            _identity_blocked_outcome(
                store,
                job,
                idempotency_key=str(idempotency_key),
                match_count=1,
                identity_blocked_streak_limit=identity_blocked_streak_limit,
                grace=grace,
                accepted_submit_grace=accepted_submit_grace,
                now=now,
            )
        )
    if (
        forcing_submit_reconcile
        and record is not None
        and not _forcing_accounting_identity_matches(
            record,
            job,
            require_exact_comment=True,
            require_bound_master=False,
        )
    ):
        return (
            ReservationReconcileOutcome(
                job_id=job.job_id,
                idempotency_key=str(idempotency_key),
                action="identity_mismatch_blocked",
                status=str(job.status),
                reconciliation_source=forcing_reconciliation_source,
                match_count=1,
            )
        )
    row.record = record
    row.fallback_unique = fallback_unique
    return None


def _reserved_record_identity_matches(
    store: Any,
    record: SacctRecord,
    job: Any,
    idempotency_key: str,
    *,
    fallback_unique: bool = False,
) -> bool:
    identity = vars(job) if hasattr(job, "__dict__") else {}
    if not forecast_cohort_identity_is_valid(identity) or not _file_cohort_runtime_identity_matches(store, identity):
        return False
    if fallback_unique:
        # #1565: on the name-window fallback the accounting Comment was not
        # stored (explicitly comment-less cluster), so an empty comment is "not
        # stored", not "different job". A present-but-different comment stays
        # fatal exactly as on the exact-comment path.
        if not _fallback_comment_allowed(record.comment, getattr(job, "slurm_comment", None)):
            return False
    elif idempotency_key_from_comment(record.comment) != idempotency_key:
        return False
    if not SLURM_JOB_ID_RE.fullmatch(str(record.slurm_job_id)):
        return False
    expected_token = _expected_job_name_token(getattr(job, "stage", None), getattr(job, "job_type", None))
    if not _identity_matches(record, expected_token, job, require_durable_identity=False):
        return False
    for record_value, job_field in (
        (record.run_id, "run_id"),
        (record.stage, "stage"),
        (record.pipeline_job_id, "job_id"),
    ):
        expected = getattr(job, job_field, None)
        if record_value not in (None, "") and expected not in (None, "") and str(record_value) != str(expected):
            return False
    expected_user = str(getattr(job, "expected_slurm_user", None) or "")
    expected_account = str(getattr(job, "expected_slurm_account", None) or "")
    if bool(getattr(job, "slurm_ownership_required", False)) and (
        not expected_user or not expected_account or not record.user or not record.account
    ):
        return False
    if expected_user and record.user != expected_user:
        return False
    if expected_account and record.account != expected_account:
        return False
    return True
