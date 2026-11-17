"""Ordered phase helpers of the reserved-unbound reconcile (#3176).

``reconcile_reserved._reconcile_one_reserved_unbound_job`` runs these, in order,
for each keyed reserved row. Each body is a verbatim slice of the former
monolithic loop body of ``reconcile_reserved_unbound_jobs``: it unpacks the pass
context and the row state into the original local names, and returns the row's
outcome where the loop appended one and continued, or ``None`` to fall through
to the next phase. The identity-gate phase stays in ``reconcile_reserved``
because it calls the patched seam ``_reserved_record_identity_matches``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC
from typing import Any

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
)
from services.orchestrator.forcing_submit_identity import is_current_forcing_attempt_comment
from services.orchestrator.reconcile_comment_proof import (
    _effective_versioned_absence_coverage,
    _query_comment_accounting_proof,
)
from services.orchestrator.reconcile_identity import (
    _accepted_submit_versioned_job,
    _fallback_comment_allowed,
    _forcing_submit_reconcile_job,
    _is_forecast_cohort_job,
)
from services.orchestrator.reconcile_records import (
    ABSENCE_UNCONFIRMED_ACTION,
    FALLBACK_BASIS_SUBMITLINE_EXACT,
    MAX_EXACT_COMMENT_MATCHES,
    RESERVATION_LOST_STATUS,
    ReconcileQueryUnavailable,
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
    _ReservedPassContext,
    _ReservedRowState,
    _transition_file_reconciliation,
    _transition_forcing_submit_ambiguity,
)
from services.orchestrator.reservation import idempotency_key_from_comment
from services.slurm_gateway.real_backend import SLURM_JOB_ID_RE

LOGGER = logging.getLogger("services.orchestrator.reconcile")


def _reserved_lane_phase(
    job, *, context: _ReservedPassContext, row: _ReservedRowState
) -> ReservationReconcileOutcome | None:
    """Lane classification, the pre-query exits and the ``submit_outcome is None`` transitions."""

    store = context.store
    grace = context.grace
    accepted_submit_grace = context.accepted_submit_grace
    identity_blocked_streak_limit = context.identity_blocked_streak_limit
    now = context.now
    bind_only = context.bind_only
    _pass_comment_capability = context.pass_comment_capability
    idempotency_key = row.idempotency_key
    forcing_submit_reconcile = _forcing_submit_reconcile_job(job)
    if bind_only and not forcing_submit_reconcile:
        return (
            ReservationReconcileOutcome(
                job_id=job.job_id,
                idempotency_key=str(idempotency_key),
                action="query_unavailable",
                status=str(job.status),
                reconciliation_reason_class="comment_accounting_unproven",
            )
        )
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
        return (
            ReservationReconcileOutcome(
                job_id=job.job_id,
                idempotency_key=str(idempotency_key),
                action="legacy_unversioned_read_only",
                status=str(job.status),
            )
        )
    if file_forecast_cohort and not accepted_submit_reconcile:
        return (
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

    if accepted_submit_reconcile and getattr(job, "submit_outcome", None) is None:
        _transition_file_reconciliation(
            store,
            job,
            AcceptedSubmitTransition.timeout(status=str(job.status)),
        )
    elif forcing_submit_reconcile and getattr(job, "submit_outcome", None) is None:
        if bind_only:
            return (
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="query_unavailable",
                    status=str(job.status),
                    reconciliation_source="slurm_exact_comment",
                    reconciliation_reason_class="comment_accounting_unproven",
                )
            )
        _transition_forcing_submit_ambiguity(store, job)
    trusted_submit_reconcile = accepted_submit_reconcile or forcing_submit_reconcile
    attempt_anchor = _job_attempt_anchor(
        job, accepted_submit_reconcile=trusted_submit_reconcile
    )
    row.forcing_submit_reconcile = forcing_submit_reconcile
    row.accepted_submit_reconcile = accepted_submit_reconcile
    row.trusted_submit_reconcile = trusted_submit_reconcile
    row.attempt_anchor = attempt_anchor
    return None


def _reserved_accounting_query_phase(
    job, *, context: _ReservedPassContext, row: _ReservedRowState
) -> ReservationReconcileOutcome | None:
    """The comment-accounting query; only it is wrapped by the per-row transient ``except``."""

    store = context.store
    comment_query = context.comment_query
    idempotency_key = row.idempotency_key
    forcing_submit_reconcile = row.forcing_submit_reconcile
    accepted_submit_reconcile = row.accepted_submit_reconcile
    trusted_submit_reconcile = row.trusted_submit_reconcile
    attempt_anchor = row.attempt_anchor
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
        return (
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
    row.proof = proof
    return None


def _reserved_proof_kind_phase(
    job, *, context: _ReservedPassContext, row: _ReservedRowState
) -> ReservationReconcileOutcome | None:
    """Proof-kind dispatch: coverage, forcing non-match kinds and the accepted-submit fail-closed kinds."""

    store = context.store
    grace = context.grace
    accepted_submit_grace = context.accepted_submit_grace
    identity_blocked_streak_limit = context.identity_blocked_streak_limit
    now = context.now
    idempotency_key = row.idempotency_key
    forcing_submit_reconcile = row.forcing_submit_reconcile
    accepted_submit_reconcile = row.accepted_submit_reconcile
    attempt_anchor = row.attempt_anchor
    proof = row.proof
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
        return (
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
    if (
        forcing_submit_reconcile
        and proof.kind == "global_absence"
        and not _effective_versioned_absence_coverage(
            proof, attempt_anchor=attempt_anchor
        )
    ):
        return (
            ReservationReconcileOutcome(
                job_id=job.job_id,
                idempotency_key=str(idempotency_key),
                action=ABSENCE_UNCONFIRMED_ACTION,
                status=str(job.status),
                reconciliation_source="slurm_exact_comment",
                reconciliation_reason_class="coverage_incomplete",
            )
        )
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
        return (
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
    if accepted_submit_reconcile and proof.kind == "ambiguous":
        write_count = _record_file_reconciliation(store, job, "multiple_matches_blocked")
        return (
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
    if accepted_submit_reconcile and proof.kind == "foreign_collision":
        return (
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
        return (
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
        return (
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
    if accepted_submit_reconcile and proof.kind == "fallback_ambiguous":
        # #1565: two or more distinct owned in-window masters never
        # bind and never change disposal authority.
        fallback_basis = row.fallback_basis = proof.fallback_match_basis
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
    row.forcing_reconciliation_source = forcing_reconciliation_source
    return None


def _reserved_forcing_absence_phase(
    job, *, context: _ReservedPassContext, row: _ReservedRowState
) -> ReservationReconcileOutcome | None:
    """The forcing absence branch: bind-only hold, grace deferral and the forcing retry permission."""

    store = context.store
    grace = context.grace
    now = context.now
    bind_only = context.bind_only
    idempotency_key = row.idempotency_key
    forcing_submit_reconcile = row.forcing_submit_reconcile
    attempt_anchor = row.attempt_anchor
    proof = row.proof
    record = row.record
    # Confirm the accounting row carries this reservation's current
    # durable comment and a valid Slurm id before binding — symmetric
    # with the identity guard in ``reconcile_inflight_jobs``. On the
    # name-window fallback an empty comment is expected because the
    # cluster does not store it; a present different comment is fatal.
    if forcing_submit_reconcile and record is None:
        if bind_only:
            return (
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action=ABSENCE_UNCONFIRMED_ACTION,
                    status=str(job.status),
                    reconciliation_source="slurm_exact_comment",
                )
            )
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
                return (
                    ReservationReconcileOutcome(
                        job_id=job.job_id,
                        idempotency_key=str(idempotency_key),
                        action=ABSENCE_UNCONFIRMED_ACTION,
                        status=str(job.status),
                        reconciliation_source="slurm_exact_comment",
                    )
                )
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
        return (
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
    return None


def _reserved_confirmed_absence_phase(
    job, *, context: _ReservedPassContext, row: _ReservedRowState
) -> ReservationReconcileOutcome | None:
    """The fallback-unique no-record hold and the confirmed-absent deferral or demotion."""

    store = context.store
    grace = context.grace
    accepted_submit_grace = context.accepted_submit_grace
    now = context.now
    idempotency_key = row.idempotency_key
    forcing_submit_reconcile = row.forcing_submit_reconcile
    accepted_submit_reconcile = row.accepted_submit_reconcile
    attempt_anchor = row.attempt_anchor
    record = row.record
    fallback_unique = row.fallback_unique
    if record is None or not SLURM_JOB_ID_RE.fullmatch(str(record.slurm_job_id)):
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
                return (
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
        return (
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
    return None


def _reserved_final_comment_guard_phase(
    job, *, context: _ReservedPassContext, row: _ReservedRowState
) -> ReservationReconcileOutcome | None:
    """The independent final fallback comment guard (#1565 Fix 3)."""

    store = context.store
    idempotency_key = row.idempotency_key
    record = row.record
    fallback_unique = row.fallback_unique
    fallback_basis = row.fallback_basis
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
    return None


def _reserved_commit_bind_phase(
    job, *, context: _ReservedPassContext, row: _ReservedRowState
) -> ReservationReconcileOutcome:
    """The accepted-submit commit, the forcing bind or the legacy bind, and the bound outcome."""

    store = context.store
    target_submission_attempt = context.target_submission_attempt
    target_submission_attempt_started_at = context.target_submission_attempt_started_at
    target_slurm_comment = context.target_slurm_comment
    idempotency_key = row.idempotency_key
    forcing_submit_reconcile = row.forcing_submit_reconcile
    accepted_submit_reconcile = row.accepted_submit_reconcile
    attempt_anchor = row.attempt_anchor
    forcing_reconciliation_source = row.forcing_reconciliation_source
    record = row.record
    fallback_unique = row.fallback_unique
    fallback_basis = row.fallback_basis
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
                return (
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
            return (
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
    return (
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
