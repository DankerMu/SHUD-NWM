"""Reservation-reclaim cluster of the file orchestration journal, as a repository mixin.

The in-lock reclaim of a reservation (``reclaim_pipeline_job_reservation``): a
keyed ``submission_failed`` / ``reservation_lost`` row, or a keyless ``pending``
row found by job id.  Under the cycle write lock it re-reads the persisted row
and returns ``None`` when the row is absent or its status, key or Slurm binding
does not admit a reclaim, or an identity, attempt, anchor or forcing-overlap
check fails; a legacy unversioned forecast cohort master raises.  Otherwise it
derives the new submission attempt from the persisted row and writes it back as
``reserved`` with committed-projection containment.  The lock, the row lookups
and the write are facade-class methods reached through ``self``.  The journal's
error type is imported inside the method; the facade-resident patched clock is
looked up on the facade module at call time.
"""

from __future__ import annotations

from typing import Any

from services.orchestrator.accepted_submit_identity import (
    OPERATOR_VERIFIED_ABSENCE_DECISION,
    SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
    SLURM_BINDING_SOURCE_FIELD,
    AcceptedSubmitTransition,
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
    apply_accepted_submit_transition,
)
from services.orchestrator.file_journal_candidates import _cycle_time_from_job, _source_id_from_job
from services.orchestrator.file_journal_paths import _optional_safe_identity
from services.orchestrator.file_journal_read_attribution import _install_public_read_attribution
from services.orchestrator.file_journal_retry_helpers import (
    _accepted_submit_attempt_anchor,
    _accepted_submit_master_identity,
    _is_legacy_unversioned_forecast_cohort_master,
)
from services.orchestrator.forcing_submit_identity import (
    forcing_attempt_comment_for,
    forcing_member_identity_is_complete,
)
from services.orchestrator.scheduler_state import _format_utc


@_install_public_read_attribution
class _FileJournalReclaimMixin:
    def reclaim_pipeline_job_reservation(self, record: dict[str, Any]) -> dict[str, Any] | None:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        expected_current_attempt = record.get("expected_submission_attempt")
        expected_current_anchor = record.get("expected_submission_attempt_started_at")
        request_row = self._pipeline_job_row(record)
        idempotency_key = str(request_row["idempotency_key"])
        source_id = _source_id_from_job(request_row)
        cycle_time = _cycle_time_from_job(request_row)
        versioned_master = bool(
            accepted_submit_contract_is_current(request_row)
            and accepted_submit_row_kind(request_row) == "master"
        )
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = (
                self._accepted_submit_job_for_id_unlocked(
                    str(request_row["job_id"]),
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                if versioned_master
                else self._candidate_job_for_idempotency_unlocked(idempotency_key)
            )
            matched_by_key = bool(
                existing is not None
                and str(existing.get("idempotency_key") or "") == idempotency_key
            )
            if existing is None and request_row.get("job_id") not in (None, ""):
                existing = (
                    None
                    if versioned_master
                    else self._pipeline_job_for_id_unlocked(str(request_row["job_id"]))
                )
            if existing is None:
                return None
            forcing_reclaim = forcing_member_identity_is_complete(existing)
            if forcing_member_identity_is_complete(request_row):
                overlap_request = dict(request_row)
                overlap_request["job_id"] = str(existing.get("job_id") or "")
                if self._overlapping_unresolved_forcing_reservation_unlocked(overlap_request) is not None:
                    return None
            if (
                forcing_reclaim
                and str(existing.get("status") or "") == "reservation_lost"
                and existing.get("reconciliation_decision") != "absence_retry_permitted"
            ):
                return None
            existing_is_current_master = bool(
                accepted_submit_contract_is_current(existing)
                and accepted_submit_row_kind(existing) == "master"
            )
            if existing_is_current_master:
                if not versioned_master:
                    return None
                if (
                    str(existing.get("status") or "") != "reservation_lost"
                    or existing.get("slurm_job_id") not in (None, "")
                    or existing.get("submit_outcome") != "submit_result_ambiguous"
                    or existing.get("reconciliation_source") != "slurm_exact_comment"
                    or existing.get("reconciliation_decision")
                    not in {"absence_retry_permitted", OPERATOR_VERIFIED_ABSENCE_DECISION}
                    or existing.get("reconciliation_reason_class") is not None
                    or existing.get("matched_slurm_job_id") is not None
                    or expected_current_attempt is None
                    or expected_current_anchor is None
                ):
                    return None
                if max(int(existing.get("submission_attempt") or 1), 1) != max(
                    int(expected_current_attempt), 1
                ):
                    return None
                try:
                    if _accepted_submit_attempt_anchor(
                        existing.get("submission_attempt_started_at")
                    ) != _accepted_submit_attempt_anchor(expected_current_anchor):
                        return None
                except FileOrchestrationJournalError:
                    return None
            if versioned_master and (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
                or not matched_by_key
            ):
                return None
            if versioned_master:
                existing_identity = _accepted_submit_master_identity(existing)
                request_identity = _accepted_submit_master_identity(request_row)
                if any(
                    request_identity[field] != existing_identity[field]
                    for field in existing_identity
                    if field not in {"submission_attempt", "submission_attempt_started_at"}
                ):
                    return None
            existing_status = str(existing.get("status") or "")
            if matched_by_key:
                if existing.get("slurm_job_id") not in (None, "") or existing_status not in {
                    "submission_failed",
                    "reservation_lost",
                }:
                    return None
            else:
                if (
                    existing.get("idempotency_key") not in (None, "")
                    or existing.get("slurm_job_id") not in (None, "")
                    or existing_status != "pending"
                    or (
                        not versioned_master
                        and self._candidate_job_for_idempotency_unlocked(idempotency_key) is not None
                    )
                ):
                    return None
            # #2674: every reclaim that reaches this point writes ``reserved``.
            # Refuse on the EXISTING row's shape, not only on the row about to
            # be written: a request without ``cohort_members`` would backfill
            # ``[]`` below and commit a member-less ``reserved`` unversioned
            # forecast master, which reconcile would route to the generic
            # exact-comment lane.  No current caller reaches this (the only
            # forecast reclaim request is versioned).
            if _is_legacy_unversioned_forecast_cohort_master(existing):
                raise FileOrchestrationJournalError(
                    "file_journal_legacy_unversioned_reserved_forecast_master",
                    field="accepted_submit_contract_version",
                )
            # The new attempt's row is derived from the PERSISTED row, so the
            # init-state identity mapping it carries is the one captured at the
            # first reservation. That is the adjudicated semantics (#1188):
            # "stable from reservation onward" means from the FIRST reservation,
            # and a reclaim does not refresh the mapping even though it opens a
            # new submission attempt.
            row = (
                apply_accepted_submit_transition(
                    existing,
                    AcceptedSubmitTransition.begin_attempt(),
                )
                if accepted_submit_row_kind(existing) == "master" or forcing_reclaim
                else dict(existing)
            )
            row.update(
                {
                    "status": "reserved",
                    "slurm_job_id": None,
                    "array_task_id": None,
                    "submitted_at": None,
                    "started_at": None,
                    "finished_at": None,
                    "exit_code": None,
                    # Same auto-retry isolation contract as the reservation write
                    # in the facade's ``reserve_pipeline_job``: a reclaimed
                    # reservation that is later released must still classify as
                    # non-retriable (spec:
                    # candidate-projection-stage-attempt-retention).
                    "error_code": None,
                    "error_message": None,
                    "cancellation_receipt_recorded": False,
                    "idempotency_key": idempotency_key,
                    # #1850 Fix A: a reclaim opens a NEW attempt, so the
                    # previous attempt's binding provenance is cleared here,
                    # never inherited across attempts.
                    SLURM_BINDING_SOURCE_FIELD: None,
                    SLURM_ACCOUNTING_SUBMITTED_AT_FIELD: None,
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            # The successful lock holder owns the new attempt: for a versioned
            # master it is always the durable existing attempt + 1, exactly
            # once, never taken from the lock-external reclaim request.  (The
            # anchor below follows the same discipline.)  Legacy/non-versioned
            # rows keep the historical max() so their existing behaviour is
            # unchanged.
            if versioned_master:
                row["submission_attempt"] = int(existing.get("submission_attempt") or 1) + 1
            else:
                row["submission_attempt"] = max(
                    int(existing.get("submission_attempt") or 1) + 1,
                    int(request_row.get("submission_attempt") or 1),
                )
            # The successful lock holder owns a new attempt. Its immutable
            # authority anchor is captured here, never copied from a stale
            # lock-external reclaim request.
            row["submission_attempt_started_at"] = _format_utc(file_orchestration_journal._utcnow())
            if forcing_reclaim:
                # A reclaimed forcing reservation is a distinct execution
                # attempt. Mint its exact controller comment inside the lock
                # and persist the current request's validated task map so the
                # Gateway payload and durable recovery mapping agree.
                row["slurm_comment"] = forcing_attempt_comment_for(
                    idempotency_key,
                    row["submission_attempt"],
                )
                if forcing_member_identity_is_complete(request_row):
                    row["cohort_members"] = list(request_row["cohort_members"])
            if not versioned_master:
                # INIT_STATE_IDENTITY_FIELD is deliberately absent from this
                # backfill set (#1188): keeping it out is what makes the reclaim
                # keep-first, since a recomputed mapping on the lock-external
                # request row would otherwise overwrite the first attempt's
                # captured lineage. Same reason as the attempt anchor above —
                # durable authority state never comes from outside the lock.
                # Two halves, not one: for a versioned master the enclosing
                # ``if not versioned_master:`` never runs, so keep-first there
                # rests on the guard AND on this omission. Adding the field back
                # to the tuple alone would not change versioned-master behaviour,
                # and flipping the guard alone would not either; both must hold.
                keys = (
                    "run_id",
                    "cycle_id",
                    "model_id",
                    "stage",
                    "candidate_id",
                    "job_type",
                    "slurm_comment",
                    "cohort_members",
                    "cohort_digest",
                    "restart_stage",
                    "expected_slurm_user",
                    "expected_slurm_account",
                    "slurm_ownership_required",
                    "native_shud_resubmitted",
                )
                for key in keys:
                    if forcing_reclaim and key in {
                        "slurm_comment",
                        "cohort_members",
                        "restart_stage",
                        "expected_slurm_user",
                        "expected_slurm_account",
                        "slurm_ownership_required",
                    }:
                        continue
                    if key in request_row and request_row.get(key) not in (None, ""):
                        row[key] = request_row[key]
                for key in ("run_id", "cycle_id", "model_id", "stage", "candidate_id", "job_type"):
                    if row.get(key) in (None, "") and request_row.get(key) not in (None, ""):
                        row[key] = request_row[key]
            # #1564 D9 / #1796 committed reclaim completion: the authority append
            # inside ``_write_pipeline_job_unlocked`` is the commit point of the
            # new attempt.  A derived direct/inventory/latest projection failure
            # after that append must never be reported as an uncommitted failure
            # that strands a live pre-sbatch ``reserved`` row -- the identical
            # defect is reachable through every reclaim door (operator
            # ``operator_verified_absence`` and automatic
            # ``absence_retry_permitted``) plus plain reserve, so containment is
            # opt-in for every reservation/reclaim write at this boundary.
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(
                row,
                exclusive_direct=False,
                model_id=model_id,
                _committed_projection_containment=True,
            )
