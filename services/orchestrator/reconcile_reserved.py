"""Reserved-unbound reconcile (``reconcile_reserved_unbound_jobs``)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
    forecast_cohort_identity_is_valid,
)
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
from services.orchestrator.forcing_submit_identity import is_current_forcing_attempt_comment
from services.orchestrator.reconcile_comment_proof import (
    _comment_query_capability,
    _effective_versioned_absence_coverage,
    _query_comment_accounting_proof,
)
from services.orchestrator.reconcile_identity import (
    _accepted_submit_versioned_job,
    _expected_job_name_token,
    _fallback_comment_allowed,
    _file_cohort_runtime_identity_matches,
    _forcing_accounting_identity_matches,
    _forcing_submit_reconcile_job,
    _identity_matches,
    _is_forecast_cohort_job,
)
from services.orchestrator.reconcile_records import (
    ABSENCE_UNCONFIRMED_ACTION,
    FALLBACK_BASIS_SUBMITLINE_EXACT,
    JOURNAL_QUARANTINED_ACTION,
    MAX_EXACT_COMMENT_MATCHES,
    RESERVATION_ABSENCE_GRACE,
    RESERVATION_LOST_STATUS,
    CommentSacctQuerier,
    ReconcileQueryUnavailable,
    SacctRecord,
    _callable_accepts_keyword,
    _ReconcileFallbackQueryUnavailable,
)
from services.orchestrator.reconcile_reserved_support import (
    ReservationReconcileOutcome,
    _accepted_submit_reconcile_job,
    _identity_blocked_outcome,
    _job_attempt_anchor,
    _job_submission_attempt,
    _record_file_reconciliation,
    _targeted_reserved_unbound_jobs,
    _transition_file_reconciliation,
    _transition_forcing_submit_ambiguity,
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

    for job in reserved_jobs:
        idempotency_key = job.idempotency_key
        if not idempotency_key:
            continue
        # Journal capacity/integrity faults are per-cycle. Quarantine the
        # offending row with its evidence instead of letting one poisoned
        # cycle abort resolution of every other reserved row and the pass.
        outcome_floor = len(outcomes)
        # #2655: the match basis of a classified fallback also rides on a
        # journal quarantine raised after classification (commit-time scan).
        fallback_basis: str | None = None
        try:
            forcing_submit_reconcile = _forcing_submit_reconcile_job(job)
            if bind_only and not forcing_submit_reconcile:
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action="query_unavailable",
                        status=str(job.status),
                        reconciliation_reason_class="comment_accounting_unproven",
                    )
                )
                continue
            accepted_submit_reconcile = _accepted_submit_reconcile_job(
                store,
                job,
                comment_capability_provider=_pass_comment_capability,
            )
            file_forecast_cohort = bool(
                getattr(store, "supports_accepted_submit_reconcile", False)
                and _accepted_submit_versioned_job(job)
                and _is_forecast_cohort_job(job)
                and isinstance(getattr(job, "cohort_members", None), Sequence)
                and not isinstance(getattr(job, "cohort_members", None), str | bytes)
                and getattr(job, "cohort_members", None)
            )
            unversioned_file_forecast = bool(
                getattr(store, "supports_accepted_submit_reconcile", False)
                and not _accepted_submit_versioned_job(job)
                and _is_forecast_cohort_job(job)
                and isinstance(getattr(job, "cohort_members", None), Sequence)
                and not isinstance(getattr(job, "cohort_members", None), str | bytes)
                and getattr(job, "cohort_members", None)
            )
            if unversioned_file_forecast:
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action="legacy_unversioned_read_only",
                        status=str(job.status),
                    )
                )
                continue
            if file_forecast_cohort and not accepted_submit_reconcile:
                outcomes.append(
                    _identity_blocked_outcome(
                        store,
                        job,
                        idempotency_key=str(idempotency_key),
                        identity_blocked_streak_limit=identity_blocked_streak_limit,
                        grace=grace,
                        accepted_submit_grace=accepted_submit_grace,
                        now=now,
                    )
                )
                continue

            if accepted_submit_reconcile and getattr(job, "submit_outcome", None) is None:
                _transition_file_reconciliation(
                    store,
                    job,
                    AcceptedSubmitTransition.timeout(status=str(job.status)),
                )
            elif forcing_submit_reconcile and getattr(job, "submit_outcome", None) is None:
                if bind_only:
                    outcomes.append(
                        ReservationReconcileOutcome(
                            job_id=job.job_id,
                            idempotency_key=str(idempotency_key),
                            action="query_unavailable",
                            status=str(job.status),
                            reconciliation_source="slurm_exact_comment",
                            reconciliation_reason_class="comment_accounting_unproven",
                        )
                    )
                    continue
                _transition_forcing_submit_ambiguity(store, job)
            trusted_submit_reconcile = accepted_submit_reconcile or forcing_submit_reconcile
            attempt_anchor = _job_attempt_anchor(
                job, accepted_submit_reconcile=trusted_submit_reconcile
            )
            try:
                expected_user = str(getattr(job, "expected_slurm_user", None) or "")
                expected_account = str(getattr(job, "expected_slurm_account", None) or "")
                forcing_members = getattr(job, "cohort_members", None)
                forcing_task_count = (
                    len(forcing_members)
                    if forcing_submit_reconcile
                    and isinstance(forcing_members, Sequence)
                    and not isinstance(forcing_members, str | bytes)
                    else None
                )
                proof = _query_comment_accounting_proof(
                    comment_query,
                    str(idempotency_key),
                    expected_user=expected_user if trusted_submit_reconcile else "",
                    expected_account=expected_account if trusted_submit_reconcile else "",
                    accepted_submit_contract_version=(
                        ACCEPTED_SUBMIT_CONTRACT_VERSION if accepted_submit_reconcile else None
                    ),
                    submission_attempt_started_at=(
                        attempt_anchor if trusted_submit_reconcile else None
                    ),
                    forcing_controller_identity=forcing_submit_reconcile,
                    forcing_stage=(
                        str(getattr(job, "stage", None) or getattr(job, "job_type", None) or "")
                        if forcing_submit_reconcile
                        else None
                    ),
                    forcing_task_count=forcing_task_count,
                    forcing_exact_comment=(
                        str(getattr(job, "slurm_comment", None) or "")
                        if forcing_submit_reconcile
                        else None
                    ),
                    forcing_current_attempt_comment=(
                        is_current_forcing_attempt_comment(vars(job))
                        if forcing_submit_reconcile
                        else False
                    ),
                )
            except ReconcileQueryUnavailable as error:
                # Transient failure: we did NOT confirm absence. Keep the row
                # reserved (do not touch status) and retry on a later pass. Never
                # mark reservation_lost here — that is what would let a double
                # submit slip through.
                LOGGER.warning(
                    "reconcile reservation query unavailable for idempotency_key=%s: %s",
                    idempotency_key,
                    error,
                )
                fallback_origin = isinstance(error, _ReconcileFallbackQueryUnavailable)
                # Pass evidence keeps the applicable transient reason; the
                # durable held tuple reason stays ``comment_accounting_unproven``
                # for fallback-origin failures so the #1564 demotion CAS remains
                # valid, while exact-comment failures keep their historical
                # durable reason behavior.
                durable_reason_class = (
                    "comment_accounting_unproven" if fallback_origin else error.reason_class
                )
                write_count = (
                    _record_file_reconciliation(
                        store,
                        job,
                        "accounting_unavailable",
                        reason_class=durable_reason_class,
                    )
                    if accepted_submit_reconcile
                    else 0
                )
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action="query_unavailable",
                        status=str(job.status),
                        reconciliation_source="slurm_exact_comment" if accepted_submit_reconcile else None,
                        reconciliation_decision="accounting_unavailable" if accepted_submit_reconcile else None,
                        reconciliation_reason_class=(error.reason_class if accepted_submit_reconcile else None),
                        durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                        durable_write_count=write_count,
                    )
                )
                continue
            forcing_reconciliation_source = (
                "slurm_controller_exact_comment"
                if forcing_submit_reconcile and proof.kind == "controller_match"
                else "slurm_exact_comment"
            )
            if (
                accepted_submit_reconcile
                and proof.kind == "global_absence"
                and not _effective_versioned_absence_coverage(proof, attempt_anchor=attempt_anchor)
            ):
                write_count = _record_file_reconciliation(
                    store,
                    job,
                    "accounting_unavailable",
                    reason_class="coverage_incomplete",
                )
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action="query_unavailable",
                        status=str(job.status),
                        reconciliation_source="slurm_exact_comment",
                        reconciliation_decision="accounting_unavailable",
                        reconciliation_reason_class="coverage_incomplete",
                        durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                        durable_write_count=write_count,
                    )
                )
                continue
            if (
                forcing_submit_reconcile
                and proof.kind == "global_absence"
                and not _effective_versioned_absence_coverage(
                    proof, attempt_anchor=attempt_anchor
                )
            ):
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action=ABSENCE_UNCONFIRMED_ACTION,
                        status=str(job.status),
                        reconciliation_source="slurm_exact_comment",
                        reconciliation_reason_class="coverage_incomplete",
                    )
                )
                continue
            if forcing_submit_reconcile and proof.kind not in {
                "owned_match",
                "controller_match",
                "global_absence",
            }:
                action = (
                    "multiple_matches_blocked"
                    if proof.kind == "ambiguous"
                    else "identity_mismatch_blocked"
                    if proof.kind == "foreign_collision"
                    else "query_unavailable"
                )
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action=action,
                        status=str(job.status),
                        reconciliation_source="slurm_exact_comment",
                        match_count=(
                            min(len(proof.records), MAX_EXACT_COMMENT_MATCHES + 1)
                            if proof.records
                            else 0
                        ),
                        reconciliation_reason_class=(
                            "comment_accounting_unproven"
                            if action == "query_unavailable"
                            else None
                        ),
                    )
                )
                continue
            if accepted_submit_reconcile and proof.kind == "ambiguous":
                write_count = _record_file_reconciliation(store, job, "multiple_matches_blocked")
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action="multiple_matches_blocked",
                        status=str(job.status),
                        reconciliation_source="slurm_exact_comment",
                        reconciliation_decision="multiple_matches_blocked",
                        match_count=min(len(proof.records), MAX_EXACT_COMMENT_MATCHES + 1),
                        durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                        durable_write_count=write_count,
                    )
                )
                continue
            if accepted_submit_reconcile and proof.kind == "foreign_collision":
                outcomes.append(
                    _identity_blocked_outcome(
                        store,
                        job,
                        idempotency_key=str(idempotency_key),
                        match_count=min(len(proof.records), MAX_EXACT_COMMENT_MATCHES + 1),
                        identity_blocked_streak_limit=identity_blocked_streak_limit,
                        grace=grace,
                        accepted_submit_grace=accepted_submit_grace,
                        now=now,
                    )
                )
                continue
            if accepted_submit_reconcile and proof.kind == "fallback_submit_unparsable":
                # #1565: malformed/missing Submit evidence on an otherwise
                # eligible name-window row is transient denial. Pass evidence
                # reports ``query_unavailable`` / ``fallback_submit_unparsable``
                # (pass-only); the durable held tuple stays reserved/unbound
                # under ``comment_accounting_unproven`` and is only established
                # on the first pass.
                write_count = _record_file_reconciliation(
                    store,
                    job,
                    "accounting_unavailable",
                    reason_class="comment_accounting_unproven",
                )
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action="query_unavailable",
                        status=str(job.status),
                        reconciliation_source="slurm_exact_comment",
                        reconciliation_decision="accounting_unavailable",
                        reconciliation_reason_class="fallback_submit_unparsable",
                        durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                        durable_write_count=write_count,
                    )
                )
                continue
            if accepted_submit_reconcile and proof.kind == "fallback_no_match":
                # #1565: zero eligible name-window masters is NOT an absence
                # proof. The row stays reserved and unbound; no retry
                # permission and no identity-mismatch transition is written.
                write_count = _record_file_reconciliation(
                    store,
                    job,
                    "accounting_unavailable",
                    reason_class="comment_accounting_unproven",
                )
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action="fallback_no_match",
                        status=str(job.status),
                        reconciliation_source="slurm_exact_comment",
                        reconciliation_decision="accounting_unavailable",
                        reconciliation_reason_class="comment_accounting_unproven",
                        match_count=0,
                        durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                        durable_write_count=write_count,
                    )
                )
                continue
            if accepted_submit_reconcile and proof.kind == "fallback_ambiguous":
                # #1565: two or more distinct owned in-window masters never
                # bind and never change disposal authority.
                fallback_basis = proof.fallback_match_basis
                write_count = _record_file_reconciliation(
                    store,
                    job,
                    "accounting_unavailable",
                    reason_class="comment_accounting_unproven",
                )
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action="ambiguous_fallback_match",
                        status=str(job.status),
                        reconciliation_source="slurm_exact_comment",
                        reconciliation_decision="accounting_unavailable",
                        reconciliation_reason_class="comment_accounting_unproven",
                        match_count=len(proof.records),
                        durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                        durable_write_count=write_count,
                        fallback_match_basis=fallback_basis,
                    )
                )
                continue
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
            fallback_basis = proof.fallback_match_basis if fallback_unique else None
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
                    outcomes.append(
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
                    continue
                outcomes.append(
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
                continue
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
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action="identity_mismatch_blocked",
                        status=str(job.status),
                        reconciliation_source=forcing_reconciliation_source,
                        match_count=1,
                    )
                )
                continue
            # Confirm the accounting row carries this reservation's current
            # durable comment and a valid Slurm id before binding — symmetric
            # with the identity guard in ``reconcile_inflight_jobs``. On the
            # name-window fallback an empty comment is expected because the
            # cluster does not store it; a present different comment is fatal.
            if forcing_submit_reconcile and record is None:
                if bind_only:
                    outcomes.append(
                        ReservationReconcileOutcome(
                            job_id=job.job_id,
                            idempotency_key=str(idempotency_key),
                            action=ABSENCE_UNCONFIRMED_ACTION,
                            status=str(job.status),
                            reconciliation_source="slurm_exact_comment",
                        )
                    )
                    continue
                credible_absence = (
                    proof.kind == "global_absence"
                    and _effective_versioned_absence_coverage(
                        proof, attempt_anchor=attempt_anchor
                    )
                )
                if credible_absence and attempt_anchor is not None:
                    anchor = attempt_anchor
                    if anchor.tzinfo is None:
                        anchor = anchor.replace(tzinfo=UTC)
                    if now() - anchor < grace:
                        outcomes.append(
                            ReservationReconcileOutcome(
                                job_id=job.job_id,
                                idempotency_key=str(idempotency_key),
                                action=ABSENCE_UNCONFIRMED_ACTION,
                                status=str(job.status),
                                reconciliation_source="slurm_exact_comment",
                            )
                        )
                        continue
                retry_permission = None
                if credible_absence:
                    permit_forcing_retry = getattr(
                        store, "permit_forcing_submit_retry", None
                    )
                    if callable(permit_forcing_retry):
                        retry_permission = permit_forcing_retry(
                            job.job_id,
                            expected_submission_attempt=_job_submission_attempt(job),
                            expected_submission_attempt_started_at=attempt_anchor,
                        )
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action=(
                            "absence_retry_permitted"
                            if retry_permission is not None
                            else ABSENCE_UNCONFIRMED_ACTION
                        ),
                        status=(
                            RESERVATION_LOST_STATUS
                            if retry_permission is not None
                            else str(job.status)
                        ),
                        reconciliation_source="slurm_exact_comment",
                        reconciliation_decision=(
                            "absence_retry_permitted"
                            if retry_permission is not None
                            else None
                        ),
                        reconciliation_reason_class=(
                            None if credible_absence else "coverage_incomplete"
                        ),
                        durable_write_kind=(
                            "forcing_retry_permission"
                            if retry_permission is not None
                            else None
                        ),
                        durable_write_count=1 if retry_permission is not None else 0,
                    )
                )
                continue
            if record is None or not SLURM_JOB_ID_RE.fullmatch(str(record.slurm_job_id)):
                if fallback_unique:
                    write_count = _record_file_reconciliation(
                        store,
                        job,
                        "accounting_unavailable",
                        reason_class="comment_accounting_unproven",
                    )
                    outcomes.append(
                        ReservationReconcileOutcome(
                            job_id=job.job_id,
                            idempotency_key=str(idempotency_key),
                            action="fallback_no_match",
                            status=str(job.status),
                            reconciliation_source="slurm_exact_comment",
                            reconciliation_decision="accounting_unavailable",
                            reconciliation_reason_class="comment_accounting_unproven",
                            match_count=0,
                            durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                            durable_write_count=write_count,
                        )
                    )
                    continue
                # Fall through to the confirmed-absent handling below.
            if (
                not fallback_unique
                and (
                    record is None
                    or (
                        forcing_submit_reconcile
                        and str(record.comment or "")
                        != str(getattr(job, "slurm_comment", None) or "")
                    )
                    or (
                        not forcing_submit_reconcile
                        and idempotency_key_from_comment(record.comment) != idempotency_key
                    )
                    or not SLURM_JOB_ID_RE.fullmatch(str(record.slurm_job_id))
                )
            ):
                # Confirmed-absent BUT possibly just slurmdbd propagation lag: if the
                # reservation's immutable current-attempt anchor is younger than
                # `grace`, defer
                # demotion to a later pass rather than risk demoting an in-flight job
                # into a reclaim+re-sbatch double-submit. Accepted-submit cohorts use
                # submission_attempt_started_at: reconciliation evidence may refresh
                # updated_at, but cannot extend this attempt's grace. Legacy rows keep
                # updated_at with created_at as their compatibility fallback.
                anchor = attempt_anchor
                if anchor is not None:
                    if anchor.tzinfo is None:
                        anchor = anchor.replace(tzinfo=UTC)
                    absence_grace = (
                        accepted_submit_grace
                        if accepted_submit_reconcile and accepted_submit_grace is not None
                        else grace
                    )
                    if now() - anchor < absence_grace:
                        write_count = (
                            _record_file_reconciliation(store, job, "absence_deferred")
                            if accepted_submit_reconcile
                            else 0
                        )
                        outcomes.append(
                            ReservationReconcileOutcome(
                                job_id=job.job_id,
                                idempotency_key=str(idempotency_key),
                                action=ABSENCE_UNCONFIRMED_ACTION,
                                status=str(job.status),
                                reconciliation_source="slurm_exact_comment" if accepted_submit_reconcile else None,
                                reconciliation_decision="absence_deferred" if accepted_submit_reconcile else None,
                                durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                                durable_write_count=write_count,
                            )
                        )
                        continue
                retry_permitted = False
                permit_retry = getattr(store, "permit_pipeline_job_retry", None)
                if accepted_submit_reconcile and callable(permit_retry):
                    retry_kwargs: dict[str, Any] = {
                        "expected_submission_attempt": _job_submission_attempt(job),
                        "expected_status": str(job.status),
                    }
                    retry_supports_anchor = _callable_accepts_keyword(
                        permit_retry, "expected_submission_attempt_started_at"
                    )
                    if retry_supports_anchor:
                        retry_kwargs["expected_submission_attempt_started_at"] = attempt_anchor
                    if _callable_accepts_keyword(permit_retry, "accepted_submit_contract_version"):
                        retry_kwargs["accepted_submit_contract_version"] = ACCEPTED_SUBMIT_CONTRACT_VERSION
                    retry_write_count = (
                        int(
                            permit_retry(
                                job.job_id,
                                **retry_kwargs,
                            )
                        )
                        if retry_supports_anchor
                        else 0
                    )
                    retry_permitted = retry_write_count > 0
                else:
                    store.update_job_status(
                        job.job_id,
                        RESERVATION_LOST_STATUS,
                        error_code="SLURM_RESERVATION_LOST",
                        error_message=(
                            "sbatch acceptance for reservation "
                            f"idempotency_key={idempotency_key} could not be confirmed in accounting."
                        ),
                    )
                    retry_permitted = True
                    retry_write_count = 1
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action=(
                            "absence_retry_permitted"
                            if accepted_submit_reconcile and retry_permitted
                            else "stale_attempt_blocked"
                            if accepted_submit_reconcile
                            else "reservation_lost"
                        ),
                        status=RESERVATION_LOST_STATUS if retry_permitted else str(job.status),
                        reconciliation_source="slurm_exact_comment" if accepted_submit_reconcile else None,
                        reconciliation_decision=(
                            "absence_retry_permitted" if accepted_submit_reconcile and retry_permitted else None
                        ),
                        durable_write_kind=(
                            "forecast_retry_permission"
                            if accepted_submit_reconcile and retry_write_count
                            else "pipeline_job_status"
                            if not accepted_submit_reconcile
                            else None
                        ),
                        durable_write_count=retry_write_count,
                    )
                )
                continue

            # Independent final comment guard (#1565 Fix 3): the reserved
            # identity gate above is one check, but the fallback bind must
            # refuse a present-but-different comment on its own even if that
            # earlier gate changes or is bypassed. The exact-comment path's
            # exact-key gate lives in the confirmed-absent guard above; the
            # fallback path allows empty or the same idempotency comment.
            if fallback_unique and not _fallback_comment_allowed(
                record.comment, getattr(job, "slurm_comment", None)
            ):
                write_count = _record_file_reconciliation(
                    store,
                    job,
                    "accounting_unavailable",
                    reason_class="comment_accounting_unproven",
                )
                outcomes.append(
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
                continue

            if accepted_submit_reconcile:
                committer = getattr(store, "commit_pipeline_job_submit_attempt", None)
                if not callable(committer):
                    raise ReconcileQueryUnavailable("accepted-submit commit API unavailable")
                commit_kwargs: dict[str, Any] = {
                    "expected_submission_attempt": _job_submission_attempt(job),
                    "slurm_job_id": record.slurm_job_id,
                    "transition": AcceptedSubmitTransition.accounting(
                        "matched_bound",
                        submit_outcome="accepted",
                        matched_slurm_job_id=record.slurm_job_id,
                        status="submitted",
                    ),
                }
                if (
                    fallback_unique
                    and record.submitted_at is not None
                    and _callable_accepts_keyword(committer, "slurm_accounting_submitted_at")
                ):
                    # #1850 Fix A (round 2): the parsed sacct ``Submit`` is the
                    # ONLY canonical accounting input. It is carried as
                    # ``slurm_accounting_submitted_at`` (the single incarnation/
                    # window authority) and is NEVER duplicated into the legacy
                    # ``submitted_at`` slot, which remains gateway/commit time
                    # and never participates in recycle/claimant proof.
                    commit_kwargs["slurm_accounting_submitted_at"] = record.submitted_at
                if fallback_unique:
                    # #1565 + Fix A: a successful name-window bind persists its
                    # own source AND its attempt-scoped immutable binding
                    # provenance; the typed commit derives the provenance from
                    # the transition shape + the canonical accounting keyword
                    # (no duplicate authority lives on the transition).
                    commit_kwargs["transition"] = AcceptedSubmitTransition.accounting(
                        "matched_bound",
                        submit_outcome="accepted",
                        matched_slurm_job_id=record.slurm_job_id,
                        status="submitted",
                        reconciliation_source="slurm_name_window_unique",
                    )
                if (
                    fallback_basis == FALLBACK_BASIS_SUBMITLINE_EXACT
                    and record.submitline_key
                    and _callable_accepts_keyword(committer, "fallback_submitline_key")
                ):
                    # #2655: the parsed SubmitLine key rides into the typed
                    # commit, which re-verifies it against the committing row
                    # and only then narrows durable claimants to same-key
                    # siblings. A store without the keyword keeps the
                    # window-overlap claimant rule (fail-closed).
                    commit_kwargs["fallback_submitline_key"] = record.submitline_key
                if _callable_accepts_keyword(committer, "pipeline_job_id"):
                    commit_kwargs["pipeline_job_id"] = job.job_id
                commit_result = committer(str(idempotency_key), **commit_kwargs)
                if not commit_result.committed:
                    # #1850 + Fix A: the typed commit refuses distinct
                    # non-committed outcomes for active-id occupancy
                    # (internal ``active_slurm_id_occupied`` /
                    # ``identity_mismatch_blocked``) vs multi-claimant
                    # ambiguity vs the ordinary stale CAS. Every refusal stays
                    # fail-closed under the held tuple with streak zero.
                    # Fix 3: the PUBLIC outcome vocabulary stays within the
                    # frozen spec — active occupancy maps to the specified
                    # ``identity_mismatch_blocked`` / count 1 action, never to
                    # the internal token.
                    if fallback_unique and commit_result.outcome in {
                        "ambiguous_fallback_match",
                        "identity_mismatch_blocked",
                        "active_slurm_id_occupied",
                    }:
                        internal = str(commit_result.outcome)
                        if internal == "ambiguous_fallback_match":
                            action = "ambiguous_fallback_match"
                            match_count = 2
                        else:
                            action = "identity_mismatch_blocked"
                            match_count = 1
                        write_count = _record_file_reconciliation(
                            store,
                            job,
                            "accounting_unavailable",
                            reason_class="comment_accounting_unproven",
                        )
                        outcomes.append(
                            ReservationReconcileOutcome(
                                job_id=job.job_id,
                                idempotency_key=str(idempotency_key),
                                action=action,
                                status=str(job.status),
                                reconciliation_source="slurm_exact_comment",
                                reconciliation_decision="accounting_unavailable",
                                reconciliation_reason_class="comment_accounting_unproven",
                                match_count=match_count,
                                durable_write_kind=(
                                    "pipeline_job_reconciliation" if write_count else None
                                ),
                                durable_write_count=write_count,
                                fallback_match_basis=fallback_basis,
                            )
                        )
                        continue
                    outcomes.append(
                        ReservationReconcileOutcome(
                            job_id=job.job_id,
                            idempotency_key=str(idempotency_key),
                            action="stale_attempt_blocked",
                            status=str(job.status),
                            reconciliation_source=(
                                "slurm_name_window_unique" if fallback_unique else "slurm_exact_comment"
                            ),
                            reconciliation_decision=None,
                            match_count=1,
                            fallback_match_basis=fallback_basis,
                        )
                    )
                    continue
                bound = commit_result.row
                write_count = int(commit_result.wrote)
            elif forcing_submit_reconcile:
                forcing_binder = getattr(store, "bind_forcing_submit_attempt", None)
                if not callable(forcing_binder):
                    raise ReconcileQueryUnavailable("forcing submit bind API unavailable")
                bind_kwargs: dict[str, Any] = {
                    "expected_submission_attempt": (
                        target_submission_attempt
                        if target_submission_attempt is not None
                        else _job_submission_attempt(job)
                    ),
                    "expected_submission_attempt_started_at": (
                        target_submission_attempt_started_at
                        if target_submission_attempt_started_at is not None
                        else attempt_anchor
                    ),
                    "slurm_job_id": record.slurm_job_id,
                    "reconciliation_source": forcing_reconciliation_source,
                }
                if target_slurm_comment is not None:
                    if not _callable_accepts_keyword(forcing_binder, "expected_slurm_comment"):
                        bound = None
                    else:
                        bind_kwargs["expected_slurm_comment"] = target_slurm_comment
                        bound = forcing_binder(str(idempotency_key), **bind_kwargs)
                else:
                    bound = forcing_binder(str(idempotency_key), **bind_kwargs)
                write_count = 1 if bound is not None else 0
            else:
                bound = store.bind_reservation(
                    str(idempotency_key),
                    slurm_job_id=record.slurm_job_id,
                    status="submitted",
                )
                write_count = 1 if bound is not None else 0
            bound_status = "submitted" if bound is not None else str(job.status)
            outcomes.append(
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="bound" if bound is not None else "stale_attempt_blocked",
                    status=bound_status,
                    slurm_job_id=record.slurm_job_id,
                    reconciliation_source=(
                        "slurm_name_window_unique"
                        if fallback_unique
                        else forcing_reconciliation_source
                        if forcing_submit_reconcile
                        else "slurm_exact_comment"
                    )
                    if accepted_submit_reconcile or forcing_submit_reconcile
                    else None,
                    reconciliation_decision=(
                        "matched_bound"
                        if accepted_submit_reconcile or forcing_submit_reconcile
                        else None
                    ),
                    matched_slurm_job_id=(
                        record.slurm_job_id
                        if accepted_submit_reconcile or forcing_submit_reconcile
                        else None
                    ),
                    match_count=1 if accepted_submit_reconcile or forcing_submit_reconcile else None,
                    durable_write_kind=(
                        "forcing_submit_bind"
                        if forcing_submit_reconcile and write_count
                        else "reservation_bind"
                        if write_count
                        else None
                    ),
                    durable_write_count=write_count,
                    fallback_match_basis=fallback_basis,
                )
            )
        except FileOrchestrationJournalError as error:
            LOGGER.warning(
                "reconcile reservation quarantined for idempotency_key=%s: %s (%s)",
                idempotency_key,
                error.reason,
                error.field,
            )
            if len(outcomes) == outcome_floor:
                outcomes.append(
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action=JOURNAL_QUARANTINED_ACTION,
                        status=str(job.status),
                        quarantine_reason=error.reason,
                        quarantine_field=error.field,
                        fallback_match_basis=fallback_basis,
                    )
                )
            continue

    return outcomes


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
