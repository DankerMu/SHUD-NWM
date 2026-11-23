"""Submit-lifecycle cluster of the file orchestration journal, as a repository mixin.

The writers that carry one pipeline job through its accepted-submit lifecycle,
each under the cycle write lock.  ``bind_pipeline_job_reservation`` binds a
reserved row to its Slurm job; ``bind_forcing_submit_attempt`` binds one
current forcing ambiguity attempt and ``permit_forcing_submit_retry`` releases
a coverage-proven absent one.  ``commit_pipeline_job_submit_attempt`` and its
locked half ``_commit_pipeline_job_submit_attempt_locked`` bind and transition
one still-current reservation attempt; the operator-verified bind reuses the
locked half under the lock it already holds.
``transition_pipeline_job_submit_evidence`` and
``transition_pipeline_job_runtime_status`` advance one current master;
``request_pipeline_job_cancellation`` / ``complete_pipeline_job_cancellation``
persist a cancellation intent and its Gateway receipt;
``reject_pipeline_job_submit_attempt``, ``mark_pipeline_job_permanently_failed``,
``record_pipeline_job_reconciliation`` and ``permit_pipeline_job_retry`` settle
a rejected, permanently failed, reconciled or retryable attempt.  The lock, the
row lookups, the writes, the journal appends and the reconcile-inventory
helpers are facade-class methods reached through ``self``.

The journal's error type, the operator bind audit and the
``_GENERIC_VERSIONED_RECONCILIATION_DECISIONS`` / ``TERMINAL_PIPELINE_STATUSES``
/ ``_ACCEPTED_RUNTIME_TRANSITIONS`` / ``PERMANENT_FAILURE_SOURCE_STATUSES`` sets
stay in the facade: the bind audit is imported under ``TYPE_CHECKING`` for an
annotation and the others inside the methods that read them.  The
facade-resident patched clock is looked up on the facade module at call time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
    OPERATOR_VERIFIED_ABSENCE_DECISION,
    SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
    SLURM_BINDING_SOURCE_FIELD,
    AcceptedSubmitCommitResult,
    AcceptedSubmitEvidenceError,
    AcceptedSubmitTransition,
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
    apply_accepted_submit_transition,
    bind_replay_is_idempotent,
    binding_source_for_transition,
    normalize_candidate_projections,
    normalize_slurm_accounting_submitted_at,
)
from services.orchestrator.file_journal_candidates import (
    _accepted_submit_source_cycle_from_job_id,
    _cycle_time_from_job,
    _public_scheduler_row,
    _resolved_caller_evidence,
    _source_id_from_job,
)
from services.orchestrator.file_journal_paths import _optional_safe_identity
from services.orchestrator.file_journal_read_attribution import _install_public_read_attribution
from services.orchestrator.file_journal_retry_helpers import (
    _accepted_submit_attempt_anchor,
    _bounded_cohort_members,
    _durable_error_message,
    _journal_record_for_write,
    _optional_format_datetime,
    _strict_utc_datetime,
)
from services.orchestrator.forcing_submit_identity import (
    FORCING_EXACT_COMMENT_RECONCILIATION_SOURCES,
    forcing_submit_identity_is_complete,
)
from services.orchestrator.reservation import SLURM_COMMENT_PREFIX
from services.orchestrator.scheduler_state import _format_utc
from services.orchestrator.scheduler_state_types import ACTIVE_HYDRO_STATUSES

if TYPE_CHECKING:
    from services.orchestrator.file_orchestration_journal import _OperatorBindAudit


@_install_public_read_attribution
class _FileJournalSubmitMixin:
    def bind_pipeline_job_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> dict[str, Any] | None:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        initial = self._candidate_job_for_idempotency_unlocked(idempotency_key)
        if initial is None:
            return None
        if accepted_submit_contract_is_current(initial) and accepted_submit_row_kind(initial) == "master":
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="bind_reservation",
            )
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._candidate_job_for_idempotency_unlocked(idempotency_key)
            if existing is None or existing.get("slurm_job_id") not in (None, ""):
                return None
            if accepted_submit_contract_is_current(existing) and accepted_submit_row_kind(existing) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="bind_reservation",
                )
            row = apply_accepted_submit_transition(
                existing,
                AcceptedSubmitTransition.accepted(status=status),
            )
            row.update(
                {
                    "slurm_job_id": str(slurm_job_id),
                    "submitted_at": row.get("submitted_at") or _format_utc(file_orchestration_journal._utcnow()),
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            if array_task_id is not None:
                row["array_task_id"] = array_task_id
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def bind_forcing_submit_attempt(
        self,
        idempotency_key: str,
        *,
        expected_submission_attempt: int,
        expected_submission_attempt_started_at: datetime | str,
        slurm_job_id: str,
        reconciliation_source: str = "slurm_exact_comment",
        expected_slurm_comment: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically bind one trusted, current forcing ambiguity attempt."""

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        initial = self._candidate_job_for_idempotency_unlocked(idempotency_key)
        if initial is None or not forcing_submit_identity_is_complete(initial):
            return None
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        requested_id = str(slurm_job_id)
        requested_source = str(reconciliation_source or "")
        requested_comment = (
            str(expected_slurm_comment) if expected_slurm_comment is not None else None
        )
        if (
            not requested_id.isdigit()
            or requested_source not in FORCING_EXACT_COMMENT_RECONCILIATION_SOURCES
        ):
            return None
        try:
            expected_anchor = _optional_format_datetime(
                expected_submission_attempt_started_at,
                field="expected_submission_attempt_started_at",
            )
        except FileOrchestrationJournalError:
            return None
        if expected_anchor is None:
            return None
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._candidate_job_for_idempotency_unlocked(idempotency_key)
            if existing is None or not forcing_submit_identity_is_complete(existing):
                return None
            try:
                current_anchor = _optional_format_datetime(
                    existing.get("submission_attempt_started_at"),
                    field="submission_attempt_started_at",
                )
            except FileOrchestrationJournalError:
                return None
            if (
                current_anchor != expected_anchor
                or max(int(existing.get("submission_attempt") or 1), 1)
                != max(int(expected_submission_attempt), 1)
                or (
                    requested_comment is not None
                    and str(existing.get("slurm_comment") or "") != requested_comment
                )
            ):
                return None
            current_id = str(existing.get("slurm_job_id") or "")
            if current_id:
                if (
                    current_id == requested_id
                    and existing.get("submit_outcome") == "accepted"
                    and existing.get("reconciliation_source") == requested_source
                    and existing.get("reconciliation_decision") == "matched_bound"
                    and existing.get("matched_slurm_job_id") == requested_id
                ):
                    return dict(existing)
                return None
            if str(existing.get("status") or "") != "reserved":
                return None
            row = apply_accepted_submit_transition(
                existing, AcceptedSubmitTransition.accepted(status="submitted")
            )
            row.update(
                {
                    "slurm_job_id": requested_id,
                    "submitted_at": existing.get("submitted_at") or _format_utc(file_orchestration_journal._utcnow()),
                    "submit_outcome": "accepted",
                    "reconciliation_source": requested_source,
                    "reconciliation_decision": "matched_bound",
                    "matched_slurm_job_id": requested_id,
                    "reconciliation_reason_class": None,
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(
                row, exclusive_direct=False, model_id=model_id
            )

    def permit_forcing_submit_retry(
        self,
        job_id: str,
        *,
        expected_submission_attempt: int,
        expected_submission_attempt_started_at: datetime | str,
    ) -> dict[str, Any] | None:
        """Release only a coverage-proven absent forcing ambiguity attempt."""

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        initial = self._pipeline_job_for_id_unlocked(job_id)
        if initial is None or not forcing_submit_identity_is_complete(initial):
            return None
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        try:
            expected_anchor = _optional_format_datetime(
                expected_submission_attempt_started_at,
                field="expected_submission_attempt_started_at",
            )
        except FileOrchestrationJournalError:
            return None
        if expected_anchor is None:
            return None
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(job_id)
            if existing is None or not forcing_submit_identity_is_complete(existing):
                return None
            try:
                current_anchor = _optional_format_datetime(
                    existing.get("submission_attempt_started_at"),
                    field="submission_attempt_started_at",
                )
            except FileOrchestrationJournalError:
                return None
            if (
                str(existing.get("status") or "") != "reserved"
                or existing.get("slurm_job_id") not in (None, "")
                or max(int(existing.get("submission_attempt") or 1), 1)
                != max(int(expected_submission_attempt), 1)
                or current_anchor != expected_anchor
            ):
                return None
            row = dict(existing)
            row.update(
                {
                    "status": "reservation_lost",
                    "reconciliation_source": "slurm_exact_comment",
                    "reconciliation_decision": "absence_retry_permitted",
                    "reconciliation_reason_class": None,
                    "matched_slurm_job_id": None,
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(
                row, exclusive_direct=False, model_id=model_id
            )

    def commit_pipeline_job_submit_attempt(
        self,
        idempotency_key: str,
        *,
        pipeline_job_id: str | None = None,
        expected_submission_attempt: int,
        slurm_job_id: str,
        transition: AcceptedSubmitTransition,
        array_task_id: int | None = None,
        submitted_at: datetime | None = None,
        slurm_accounting_submitted_at: datetime | str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
        fallback_submitline_key: str | None = None,
    ) -> AcceptedSubmitCommitResult:
        """Bind and transition exactly one still-current reservation attempt.

        The attempt check, reserved-state check, unbound check, Slurm id bind,
        and complete accepted-submit evidence transition all happen under the
        same cycle lock and in one durable journal replacement.

        ``fallback_submitline_key`` (#2655) is the name-window fallback
        candidate's proven ``SubmitLine`` comment. It is legal only on a
        name-window ``matched_bound`` transition; a key that is not the
        committing row's own ``nhms_idem:<idempotency_key>`` comment is
        refused (``identity_mismatch_blocked``) with zero journal bytes. A
        verified key narrows durable claimants to same-key reserved siblings;
        active-owner and same-accounting-incarnation occupancy are unchanged.
        """

        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        if not isinstance(transition, AcceptedSubmitTransition):
            raise TypeError("transition must be AcceptedSubmitTransition")
        if transition.submit_outcome != "accepted":
            raise ValueError("submit-attempt commit requires an accepted transition")
        # Writer-authority closed world (#1564 D8): the operator decision is
        # durable provenance granted only by the dedicated typed demotion, never
        # by an accepted-submit writer, even when a caller forges an accepted
        # transition carrying the token.  Refuse before any mutation.
        if transition.reconciliation_decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="reconciliation_decision",
            )
        if type(transition.status) is not str or transition.status not in {
            "submitted",
            "pending",
            "queued",
            "running",
        }:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid", field="status"
            )
        requested_id = str(slurm_job_id)
        if not requested_id.isdigit():
            raise ValueError("submit-attempt commit requires a numeric Slurm master job id")
        if (
            transition.reconciliation_decision == "matched_bound"
            and transition.matched_slurm_job_id != requested_id
        ):
            raise ValueError("matched accounting id must equal the bound Slurm job id")
        if fallback_submitline_key is not None and not (
            transition.reconciliation_source == "slurm_name_window_unique"
            and transition.reconciliation_decision == "matched_bound"
        ):
            raise ValueError("a submit-line key is only valid for a name-window fallback bind")
        if pipeline_job_id is not None:
            source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(pipeline_job_id)
        else:
            initial = self._candidate_job_for_idempotency_unlocked(idempotency_key)
            if initial is None:
                return AcceptedSubmitCommitResult("missing")
            source_id = _source_id_from_job(initial)
            cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = (
                self._accepted_submit_job_for_id_unlocked(
                    pipeline_job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                if pipeline_job_id is not None
                else self._candidate_job_for_idempotency_unlocked(idempotency_key)
            )
            return self._commit_pipeline_job_submit_attempt_locked(
                existing,
                idempotency_key=idempotency_key,
                pipeline_job_id=pipeline_job_id,
                source_id=source_id,
                cycle_time=cycle_time,
                expected_submission_attempt=expected_submission_attempt,
                requested_id=requested_id,
                transition=transition,
                array_task_id=array_task_id,
                submitted_at=submitted_at,
                slurm_accounting_submitted_at=slurm_accounting_submitted_at,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=exit_code,
                error_code=error_code,
                error_message=error_message,
                log_uri=log_uri,
                fallback_submitline_key=fallback_submitline_key,
            )

    def _commit_pipeline_job_submit_attempt_locked(
        self,
        existing: dict[str, Any] | None,
        *,
        idempotency_key: str,
        pipeline_job_id: str | None,
        source_id: str,
        cycle_time: datetime,
        expected_submission_attempt: int,
        requested_id: str,
        transition: AcceptedSubmitTransition,
        array_task_id: int | None,
        submitted_at: datetime | None,
        slurm_accounting_submitted_at: datetime | str | None,
        started_at: datetime | None,
        finished_at: datetime | None,
        exit_code: int | None,
        error_code: str | None,
        error_message: str | None,
        log_uri: str | None,
        fallback_submitline_key: str | None,
        operator_bind: _OperatorBindAudit | None = None,
    ) -> AcceptedSubmitCommitResult:
        """The typed submit-attempt commit body; caller holds the cycle lock.

        Moved verbatim out of :meth:`commit_pipeline_job_submit_attempt` so the
        #2668 operator-verified bind runs the exact same claimant/occupancy
        scan and bind-row construction under the ONE cycle lock it already
        holds (the cycle lock is not reentrant).  ``operator_bind`` is
        ``None`` on every automatic path, which keeps their behaviour
        byte-for-byte; when set, the Slurm-id exclusivity is strict (a
        recycled id bound elsewhere is refused) and the write point appends
        the bind row and its audit event together.
        """

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        if existing is None:
            return AcceptedSubmitCommitResult("missing")
        if pipeline_job_id is not None and (
            not accepted_submit_contract_is_current(existing)
            or accepted_submit_row_kind(existing) != "master"
            or str(existing.get("idempotency_key") or "") != idempotency_key
        ):
            return AcceptedSubmitCommitResult("stale", dict(existing))
        if fallback_submitline_key is not None and fallback_submitline_key != (
            f"{SLURM_COMMENT_PREFIX}{existing.get('idempotency_key') or ''}"
        ):
            # #2655: the proven submit-line key must be this reservation's
            # own idempotency comment; anything else is another job.
            # Refused before any mutation -- zero journal bytes.
            return AcceptedSubmitCommitResult("identity_mismatch_blocked", dict(existing))
        current_id = str(existing.get("slurm_job_id") or "")
        current_attempt = max(int(existing.get("submission_attempt") or 1), 1)
        if current_attempt != max(int(expected_submission_attempt), 1):
            return AcceptedSubmitCommitResult("stale", dict(existing))
        # #1850 round 3 (Fix A): canonical normalization and derived bind
        # provenance are computed BEFORE the idempotent early-return. A
        # replay is idempotent ONLY when the full bind shape it WOULD write
        # -- the transition bind lane, the attempt-scoped derived binding
        # source, and (for the fallback) the canonical accounting Submit --
        # matches the durable bind exactly. A different canonical Submit on
        # the same tuple is a conflicting replay: non-committed, zero-write,
        # never reported as ``idempotent``/``bound``.
        fallback_unique = (
            transition.reconciliation_source == "slurm_name_window_unique"
            and transition.reconciliation_decision == "matched_bound"
        )
        # Canonical sacct ``Submit`` is the ONE authority for the fallback.
        # The candidate instant that drives the claimant/occupancy scan
        # comes ONLY from the explicit canonical accounting input, never
        # from the legacy gateway/commit ``submitted_at`` (which remains
        # acceptance/commit time and is never incarnation or window proof).
        # The fallback bind REQUIRES a strict canonical instant; every
        # non-fallback lane may not carry one.
        canonical_accounting_submit = normalize_slurm_accounting_submitted_at(
            slurm_accounting_submitted_at
        )
        if fallback_unique and canonical_accounting_submit is None:
            raise FileOrchestrationJournalError(
                "file_journal_submit_instant_required",
                field=SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
            )
        if not fallback_unique and slurm_accounting_submitted_at not in (None, ""):
            raise FileOrchestrationJournalError(
                "file_journal_evidence_invariant_invalid",
                field=SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
            )
        candidate_submit = _strict_utc_datetime(canonical_accounting_submit)
        if fallback_unique and candidate_submit is None:
            raise FileOrchestrationJournalError(
                "file_journal_submit_instant_required",
                field=SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
            )
        # The bind provenance this commit WOULD persist, derived centrally
        # from the transition shape (never from caller-forgeable fields).
        derived_binding_source = binding_source_for_transition(
            submit_outcome=str(transition.submit_outcome or ""),
            reconciliation_decision=transition.reconciliation_decision,
            reconciliation_source=str(transition.reconciliation_source or ""),
        )
        if derived_binding_source is None:
            # #1850 round 4 (Fix A): the typed commit accepts ONLY the three
            # legal bind shapes (ordinary accepted -> gateway_submit,
            # exact-comment matched -> slurm_exact_comment, name-window
            # matched -> slurm_name_window_unique). A transition that mints
            # no binding provenance (accepted + a held/defer/blocked
            # accounting decision, rejected, timeout, pre-outcome) is not a
            # bind and is refused with a stable error BEFORE any mutation or
            # occupancy scan -- otherwise a closed-world bind could be
            # forged with ``binding_source=None``.
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="transition",
            )
        if current_id:
            if current_id != requested_id:
                return AcceptedSubmitCommitResult("collision", dict(existing))
            same_lane = (
                existing.get("submit_outcome") == transition.submit_outcome
                and existing.get("reconciliation_source") == transition.reconciliation_source
                and existing.get("reconciliation_decision") == transition.reconciliation_decision
                and existing.get("matched_slurm_job_id") == transition.matched_slurm_job_id
            )
            # #1850 round 4 (Fix C): replay-equality is decided by ONE
            # centralized helper.  It keeps the pre-change v1 read
            # compatibility (a legacy row missing BOTH additive provenance
            # fields stays idempotent for an ordinary/exact-comment same-lane
            # replay, zero-write, never backfilled), while every new
            # provenance-carrying row still requires the exact derived
            # binding source and canonical accounting Submit.  Partial,
            # contradictory, or name-window-on-missing-fields shapes are
            # never idempotent and fall through to ``stale``.
            if bind_replay_is_idempotent(
                existing,
                derived_binding_source=derived_binding_source,
                canonical_accounting_submit=canonical_accounting_submit,
                same_lane=same_lane,
            ):
                return AcceptedSubmitCommitResult("idempotent", dict(existing))
            return AcceptedSubmitCommitResult("stale", dict(existing))
        if str(existing.get("status") or "") != "reserved":
            return AcceptedSubmitCommitResult("stale", dict(existing))
        with self._reconcile_inventory_file_lock_unlocked():
            entry_names = self._reconcile_inventory_entry_names_unlocked()
            other_masters, ambiguous = self._reconcile_inventory_jobs_matching_unlocked(
                entry_names,
                expected_user=str(existing.get("expected_slurm_user") or ""),
                expected_account=str(existing.get("expected_slurm_account") or ""),
                candidate_submit=candidate_submit,
                active_slurm_job_id=requested_id,
                include_job_id=str(existing.get("job_id") or ""),
                fallback_unique=fallback_unique,
                submitline_key=fallback_submitline_key,
                strict_slurm_id_exclusivity=operator_bind is not None,
            )
            if ambiguous:
                # More than one current reserved-unbound forecast master
                # claims this candidate window for the same owner: no
                # claimant may bind, regardless of reconcile iteration
                # order or concurrent source/cycle writers.
                return AcceptedSubmitCommitResult(
                    "ambiguous_fallback_match", dict(existing)
                )
            if any(
                str(master.get("slurm_job_id") or "") == requested_id
                for master in other_masters
            ):
                # The requested id is already bound to another active
                # current accepted-submit master in any source/cycle.
                return AcceptedSubmitCommitResult(
                    "active_slurm_id_occupied", dict(existing)
                )
            if operator_bind is not None and self._operator_bind_slurm_id_claimed_in_cycle_unlocked(
                requested_id,
                include_job_id=str(existing.get("job_id") or ""),
                source_id=source_id,
                cycle_time=cycle_time,
            ):
                # #2668 strict exclusivity, same cycle: ANY other row of this
                # source/cycle -- any stage or kind, forcing, legacy/
                # unversioned, downstream, member task, settled or active --
                # whose Slurm id has the requested master part claims it.
                # Other cycles are covered by the inventory scan above
                # (current accepted-submit masters only).
                return AcceptedSubmitCommitResult(
                    "active_slurm_id_occupied", dict(existing)
                )
            # The bind write happens INSIDE the journal-global inventory
            # lock, so no concurrent source/cycle writer (normal stage
            # submit, exact-comment commit, fallback commit) can enter the
            # scan-bind window or bind this id between the scan and the
            # commit point (cycle lock -> inventory lock order).
            row = apply_accepted_submit_transition(existing, transition)
            # #1850 Fix A (round 2): every successful typed bind records its
            # attempt-scoped binding provenance exactly once, derived
            # centrally from the transition SHAPE -- never from
            # caller-forgeable transition fields (removed) and never from
            # the legacy ``submitted_at``. The name-window fallback
            # persists the single canonical accounting Submit keyword; the
            # ordinary gateway submit and exact-comment matched recovery
            # persist no canonical evidence. ``binding_source_for_transition``
            # returns ``None`` for every non-bind shape, so a held/defer/
            # release/reject/timeout transition never mints provenance.
            derived_binding_source = binding_source_for_transition(
                submit_outcome=str(transition.submit_outcome or ""),
                reconciliation_decision=transition.reconciliation_decision,
                reconciliation_source=str(transition.reconciliation_source or ""),
            )
            if derived_binding_source is not None:
                row[SLURM_BINDING_SOURCE_FIELD] = derived_binding_source
                row[SLURM_ACCOUNTING_SUBMITTED_AT_FIELD] = (
                    canonical_accounting_submit
                    if derived_binding_source == "slurm_name_window_unique"
                    else None
                )
            # #1589 (design D3): unconditional writes of caller evidence,
            # so withheld resolves against the persisted row rather than
            # erasing it.  These ``durable=`` arguments are LOAD BEARING,
            # not uniformity: a ``reserved`` row is not necessarily
            # evidence-free.  ``reserve`` nulls the whole family, but
            # ``reclaim_pipeline_job_reservation`` nulls
            # ``exit_code``/``error_code``/``error_message`` and NOT
            # ``log_uri``, so a reclaimed reservation carries the
            # previous attempt's URI into this leg; and an unbound
            # submit-evidence transition writes evidence onto a row that
            # stays ``reserved``.  Neutralizing the ``log_uri``
            # resolution below turns exactly the
            # ``commit_pipeline_job_submit_attempt`` arm of the J20
            # replay table red -- do not "simplify" these away.
            row.update(
                {
                    "slurm_job_id": requested_id,
                    "submitted_at": _format_utc(submitted_at or file_orchestration_journal._utcnow()),
                    "started_at": (
                        _format_utc(started_at) if started_at is not None else None
                    ),
                    "finished_at": (
                        _format_utc(finished_at) if finished_at is not None else None
                    ),
                    "exit_code": _resolved_caller_evidence(
                        exit_code, durable=existing.get("exit_code")
                    ),
                    "error_code": _resolved_caller_evidence(
                        error_code, durable=existing.get("error_code")
                    ),
                    "error_message": _resolved_caller_evidence(
                        error_message, durable=existing.get("error_message")
                    ),
                    "log_uri": _resolved_caller_evidence(
                        log_uri, durable=existing.get("log_uri")
                    ),
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            if array_task_id is not None:
                row["array_task_id"] = array_task_id
            model_id = _optional_safe_identity(row, "model_id")
            if operator_bind is not None:
                # #2668: the operator-verified bind writes the SAME row plus
                # its audit event in one durable append (same inventory lock).
                return self._write_operator_bind_unlocked(
                    row,
                    existing,
                    operator_bind,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
            written = self._write_pipeline_job_unlocked(
                row, exclusive_direct=False, model_id=model_id
            )
            return AcceptedSubmitCommitResult("applied", written)

    def transition_pipeline_job_submit_evidence(
        self,
        job_id: str,
        transition: AcceptedSubmitTransition,
        *,
        accepted_submit_contract_version: str | None = None,
        expected_submission_attempt: int | None = None,
        expected_statuses: Sequence[str] | None = None,
        require_unbound: bool = False,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> AcceptedSubmitCommitResult:
        """Atomically replace submit outcome and the complete accounting tuple."""

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            _GENERIC_VERSIONED_RECONCILIATION_DECISIONS,
            FileOrchestrationJournalError,
        )

        if not isinstance(transition, AcceptedSubmitTransition):
            raise TypeError("transition must be AcceptedSubmitTransition")
        versioned = accepted_submit_contract_version == ACCEPTED_SUBMIT_CONTRACT_VERSION
        if accepted_submit_contract_version not in (None, ACCEPTED_SUBMIT_CONTRACT_VERSION):
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid",
                field=ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
            )
        if versioned:
            if (
                transition.submit_outcome != "submit_result_ambiguous"
                or transition.reconciliation_decision
                not in _GENERIC_VERSIONED_RECONCILIATION_DECISIONS
            ):
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="transition",
                )
            if (
                expected_submission_attempt is None
                or not expected_statuses
                or not require_unbound
            ):
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_cas",
                    field="expected_submission_attempt",
                )
            source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        else:
            # Writer-authority closed world (#1805): the legacy compatibility
            # path has no decision whitelist, so an ``operator_verified_absence``
            # transition would mint the typed operator authority without the
            # dedicated audited demotion.  Refuse before any read, lock
            # acquisition, row construction, durable mutation, or event.  The
            # versioned gate above keeps its own unchanged behavior.
            if transition.reconciliation_decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="reconciliation_decision",
                )
            initial = self._pipeline_job_for_id_unlocked(job_id)
            if initial is None:
                return AcceptedSubmitCommitResult("missing")
            if accepted_submit_contract_is_current(initial) and accepted_submit_row_kind(initial) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="accepted_submit_contract_version",
                )
            source_id = _source_id_from_job(initial)
            cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = (
                self._accepted_submit_job_for_id_unlocked(
                    job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                if versioned
                else self._pipeline_job_for_id_unlocked(job_id)
            )
            if existing is None:
                return AcceptedSubmitCommitResult("missing")
            if not versioned and accepted_submit_contract_is_current(existing) and accepted_submit_row_kind(
                existing
            ) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="accepted_submit_contract_version",
                )
            if versioned and (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if expected_submission_attempt is not None and max(
                int(existing.get("submission_attempt") or 1), 1
            ) != max(int(expected_submission_attempt), 1):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if expected_statuses is not None and str(existing.get("status") or "") not in set(expected_statuses):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if require_unbound and existing.get("slurm_job_id") not in (None, ""):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            row = apply_accepted_submit_transition(existing, transition)
            # #1589 (design D3): resolve BEFORE the guards, which puts it before
            # the ``changed_fields`` equality gate below too -- that ordering is
            # the whole fix.  The gate compares the row against durable state,
            # and durable state has been stripped by the write boundary, so a
            # raw placeholder here would make an identical replay differ from
            # the row forever: every pass "applied", every pass another record.
            started_at = _resolved_caller_evidence(started_at)
            finished_at = _resolved_caller_evidence(finished_at)
            exit_code = _resolved_caller_evidence(exit_code)
            error_code = _resolved_caller_evidence(error_code)
            error_message = _resolved_caller_evidence(error_message)
            log_uri = _resolved_caller_evidence(log_uri)
            if started_at is not None:
                row["started_at"] = _format_utc(started_at)
            if finished_at is not None:
                row["finished_at"] = _format_utc(finished_at)
            if exit_code is not None:
                row["exit_code"] = exit_code
            if error_code is not None:
                row["error_code"] = error_code
            if error_message is not None:
                row["error_message"] = _durable_error_message(error_message)
            if log_uri is not None:
                row["log_uri"] = log_uri
            changed_fields = (
                "submit_outcome",
                "reconciliation_source",
                "reconciliation_decision",
                "reconciliation_reason_class",
                "matched_slurm_job_id",
                # Without this the consecutive-blocked counter's increment would
                # be judged idempotent and silently never reach the journal.
                "identity_blocked_streak",
                "status",
                "started_at",
                "finished_at",
                "exit_code",
                "error_code",
                "error_message",
                "log_uri",
            )
            if all(row.get(field) == existing.get(field) for field in changed_fields):
                return AcceptedSubmitCommitResult("idempotent", dict(existing))
            row["updated_at"] = _format_utc(file_orchestration_journal._utcnow())
            model_id = _optional_safe_identity(row, "model_id")
            written = self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)
            return AcceptedSubmitCommitResult("applied", written)

    def transition_pipeline_job_runtime_status(
        self,
        job_id: str,
        status: str,
        *,
        expected_statuses: Sequence[str] | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> AcceptedSubmitCommitResult:
        """Advance one current master through non-terminal runtime states."""

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            _ACCEPTED_RUNTIME_TRANSITIONS,
            TERMINAL_PIPELINE_STATUSES,
            FileOrchestrationJournalError,
        )

        if status in TERMINAL_PIPELINE_STATUSES:
            raise FileOrchestrationJournalError(
                "file_journal_terminal_projection_required",
                field="status",
            )
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if (
                existing is None
                or not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing or {}))
            if (
                existing.get("submit_outcome") != "accepted"
                or not str(existing.get("slurm_job_id") or "").isdigit()
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if expected_statuses is not None and str(existing.get("status") or "") not in set(
                expected_statuses
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            current_status = str(existing.get("status") or "")
            if status not in _ACCEPTED_RUNTIME_TRANSITIONS.get(current_status, frozenset()):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            row = dict(existing)
            row["status"] = status
            # #1589 (design D3): same ordering requirement as the submit-evidence
            # leg -- resolve before the guards and therefore before the equality
            # gate.  ``running -> running`` is a legal self transition, so a raw
            # placeholder here is an unbounded append loop: nothing else in this
            # leg ever stops an identical poll from "changing" the row.
            started_at = _resolved_caller_evidence(started_at)
            finished_at = _resolved_caller_evidence(finished_at)
            exit_code = _resolved_caller_evidence(exit_code)
            error_code = _resolved_caller_evidence(error_code)
            error_message = _resolved_caller_evidence(error_message)
            log_uri = _resolved_caller_evidence(log_uri)
            if started_at is not None:
                row["started_at"] = _format_utc(started_at)
            if finished_at is not None:
                row["finished_at"] = _format_utc(finished_at)
            if exit_code is not None:
                row["exit_code"] = exit_code
            if error_code is not None:
                row["error_code"] = error_code
            if error_message is not None:
                row["error_message"] = _durable_error_message(error_message)
            if log_uri is not None:
                row["log_uri"] = log_uri
            if all(
                row.get(field) == existing.get(field)
                for field in (
                    "status",
                    "started_at",
                    "finished_at",
                    "exit_code",
                    "error_code",
                    "error_message",
                    "log_uri",
                )
            ):
                return AcceptedSubmitCommitResult("idempotent", dict(existing))
            row["updated_at"] = _format_utc(file_orchestration_journal._utcnow())
            written = self._write_pipeline_job_unlocked(
                row,
                exclusive_direct=False,
                model_id=None,
            )
            return AcceptedSubmitCommitResult("applied", written)

    def request_pipeline_job_cancellation(
        self,
        job_id: str,
        *,
        expected_statuses: Sequence[str],
        reason: str,
    ) -> AcceptedSubmitCommitResult:
        """Persist cancellation intent before the external Slurm side effect."""

        from services.orchestrator import file_orchestration_journal

        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if (
                existing is None
                or not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing or {}))
            if (
                existing.get("submit_outcome") != "accepted"
                or not str(existing.get("slurm_job_id") or "").isdigit()
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if str(existing.get("status") or "") == "cancellation_pending":
                return AcceptedSubmitCommitResult("idempotent", dict(existing))
            if (
                str(existing.get("status") or "") == "reconcile_unverified"
                and bool(existing.get("cancellation_receipt_recorded", False))
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if str(existing.get("status") or "") not in set(expected_statuses):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            row = dict(existing)
            row.update(
                {
                    "status": "cancellation_pending",
                    "error_code": "SLURM_CANCELLATION_REQUESTED",
                    "error_message": _durable_error_message(reason),
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            written = self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=None)
            return AcceptedSubmitCommitResult("applied", written)

    def complete_pipeline_job_cancellation(
        self,
        job_id: str,
        *,
        finished_at: datetime,
        exit_code: int | None,
        error_code: str | None,
        error_message: str | None,
        log_uri: str | None,
    ) -> AcceptedSubmitCommitResult:
        """Record the Gateway receipt while deferring terminal truth to task accounting."""

        from services.orchestrator import file_orchestration_journal

        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None:
                return AcceptedSubmitCommitResult("stale", dict(existing or {}))
            if (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
                or existing.get("submit_outcome") != "accepted"
                or not str(existing.get("slurm_job_id") or "").isdigit()
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            # #1589 (design D3): this leg writes its whole evidence family
            # UNCONDITIONALLY, so a stripped placeholder does not decline the
            # overwrite -- it destroys the value.  Withheld therefore resolves
            # against the persisted row.  Resolved ONCE, above the comparator,
            # so the value compared and the value persisted are literally the
            # same expression: otherwise the second Gateway receipt compares a
            # raw placeholder against erased durable state, answers ``stale``,
            # and ``chain_forecast_control.cancel_active_cycle_jobs`` drops the
            # cancellation event on an uncommitted result.
            resolved_exit_code = _resolved_caller_evidence(exit_code, durable=existing.get("exit_code"))
            resolved_error_message = _durable_error_message(
                _resolved_caller_evidence(error_message, durable=existing.get("error_message"))
            )
            resolved_log_uri = _resolved_caller_evidence(log_uri, durable=existing.get("log_uri"))
            desired_error_code = (
                _resolved_caller_evidence(error_code, durable=existing.get("error_code"))
                or "SLURM_JOB_CANCELLED"
            )
            if str(existing.get("status") or "") == "reconcile_unverified":
                desired = {
                    "finished_at": _format_utc(finished_at),
                    "exit_code": resolved_exit_code,
                    "error_code": desired_error_code,
                    "error_message": resolved_error_message,
                    "log_uri": resolved_log_uri,
                    "cancellation_receipt_recorded": True,
                }
                if all(existing.get(field) == value for field, value in desired.items()):
                    return AcceptedSubmitCommitResult("idempotent", dict(existing))
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if str(existing.get("status") or "") != "cancellation_pending":
                return AcceptedSubmitCommitResult("stale", dict(existing))
            row = dict(existing)
            row.update(
                {
                    "status": "reconcile_unverified",
                    "finished_at": _format_utc(finished_at),
                    "exit_code": resolved_exit_code,
                    "error_code": desired_error_code,
                    "error_message": resolved_error_message,
                    "log_uri": resolved_log_uri,
                    "cancellation_receipt_recorded": True,
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            written = self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=None)
            return AcceptedSubmitCommitResult("applied", written)

    def reject_pipeline_job_submit_attempt(
        self,
        idempotency_key: str,
        *,
        pipeline_job_id: str | None = None,
        expected_submission_attempt: int,
        finished_at: datetime,
        error_code: str,
        error_message: str,
        stage: str,
        job_type: str,
    ) -> AcceptedSubmitCommitResult:
        """Reject one current attempt and its staged hydro cohort atomically."""

        from services.orchestrator import file_orchestration_journal

        if pipeline_job_id is not None:
            source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(pipeline_job_id)
        else:
            initial = self._candidate_job_for_idempotency_unlocked(idempotency_key)
            if initial is None:
                return AcceptedSubmitCommitResult("missing")
            source_id = _source_id_from_job(initial)
            cycle_time = _cycle_time_from_job(initial)
        attempt = max(int(expected_submission_attempt), 1)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = (
                self._accepted_submit_job_for_id_unlocked(
                    pipeline_job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                if pipeline_job_id is not None
                else self._candidate_job_for_idempotency_unlocked(idempotency_key)
            )
            if existing is None:
                return AcceptedSubmitCommitResult("missing")
            if pipeline_job_id is not None and (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
                or str(existing.get("idempotency_key") or "") != idempotency_key
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if (
                max(int(existing.get("submission_attempt") or 1), 1) != attempt
                or str(existing.get("status") or "") != "reserved"
                or existing.get("slurm_job_id") not in (None, "")
            ):
                if (
                    max(int(existing.get("submission_attempt") or 1), 1) == attempt
                    and existing.get("submit_outcome") == "rejected"
                    and str(existing.get("status") or "") == "submission_failed"
                ):
                    return AcceptedSubmitCommitResult("idempotent", dict(existing))
                return AcceptedSubmitCommitResult("stale", dict(existing))

            safe_message = _durable_error_message(error_message)
            master = apply_accepted_submit_transition(existing, AcceptedSubmitTransition.rejected())
            master.update(
                {
                    "finished_at": _format_utc(finished_at),
                    "error_code": error_code,
                    "error_message": safe_message,
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            payloads: list[tuple[str, dict[str, Any], str | None]] = []
            touched_models: set[str] = set()
            members = _bounded_cohort_members(existing.get("cohort_members"))
            model_rows = self._cycle_rows_by_model_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_ids=(str(member.get("model_id") or "") for member in members),
            )
            for member in members:
                run_id = str(member.get("run_id") or "")
                model_id = str(member.get("model_id") or "")
                if not run_id or not model_id:
                    continue
                snapshot = model_rows.get(model_id)
                hydro = snapshot.hydro_run if snapshot is not None else None
                if isinstance(hydro, Mapping) and str(hydro.get("run_id") or "") != run_id:
                    hydro = None
                if (
                    hydro is None
                    or max(int(hydro.get("submission_attempt") or 1), 1) != attempt
                    or str(hydro.get("status") or "") not in ACTIVE_HYDRO_STATUSES
                ):
                    continue
                hydro_row = dict(hydro)
                hydro_row.update(
                    {
                        "status": "failed",
                        "error_code": error_code,
                        "error_message": safe_message,
                        "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                    }
                )
                payloads.append(("hydro_run", hydro_row, model_id))
                touched_models.add(model_id)
            payloads.append(("pipeline_job", master, None))
            event = {
                "event_id": self._next_accepted_submit_event_id_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                ),
                "entity_type": "pipeline_job",
                "entity_id": str(master["job_id"]),
                "event_type": "submission",
                "status_from": "reserved",
                "status_to": "submission_failed",
                "message": f"{stage} submission failed: {safe_message}",
                "details": {
                    "stage": stage,
                    "job_type": job_type,
                    "error": safe_message,
                    "submit_outcome": "rejected",
                    "submission_attempt": attempt,
                },
                "created_at": _format_utc(file_orchestration_journal._utcnow()),
            }
            payloads.append(("pipeline_event", event, None))

            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            self._write_pipeline_job_direct_unlocked(master, records[-2])
            for model_id in sorted(touched_models):
                self._materialize_latest_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    include_direct_jobs=False,
                )
            return AcceptedSubmitCommitResult("applied", _public_scheduler_row(master))

    def mark_pipeline_job_permanently_failed(
        self,
        job_id: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        finished_at: datetime | None = None,
        event_details: Mapping[str, Any] | None = None,
    ) -> AcceptedSubmitCommitResult:
        """Mark one current-contract cohort master permanently failed (#1312).

        ``update_pipeline_job_status`` refuses every master row on purpose, so
        the permanent-failure mark required for declined retries needs its own
        typed authority transition.  Only the WRITE SEQUENCE is borrowed from
        :meth:`reject_pipeline_job_submit_attempt`; none of its preconditions
        apply here (the target is already bound and already terminal) and its
        accounting replacement must NOT happen: a permanent-failure mark is a
        labelling transition, so the accepted-submit accounting tuple
        (reconciliation decision / submit outcome / matched Slurm job id) is
        preserved field-for-field by constructing the row in-lock.

        Idempotency reads the PERSISTED row, never the caller's snapshot, and
        the ``permanently_failed`` event is appended only with a real flip.
        """

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import PERMANENT_FAILURE_SOURCE_STATUSES

        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None:
                return AcceptedSubmitCommitResult("missing")
            if (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            status_from = str(existing.get("status") or "")
            if status_from == "permanently_failed":
                return AcceptedSubmitCommitResult("idempotent", dict(existing))
            if status_from not in PERMANENT_FAILURE_SOURCE_STATUSES:
                return AcceptedSubmitCommitResult("stale", dict(existing))
            master = dict(existing)
            master["status"] = "permanently_failed"
            if error_code not in (None, ""):
                master["error_code"] = error_code
            if error_message is not None:
                master["error_message"] = _durable_error_message(error_message)
            if finished_at is not None:
                master["finished_at"] = _format_utc(finished_at)
            master["updated_at"] = _format_utc(file_orchestration_journal._utcnow())
            event = {
                "event_id": self._next_accepted_submit_event_id_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                ),
                "entity_type": "pipeline_job",
                "entity_id": str(master["job_id"]),
                "event_type": "permanently_failed",
                "status_from": status_from or None,
                "status_to": "permanently_failed",
                "message": "automatic retry declined; job marked permanently failed",
                "details": dict(event_details or {}),
                "created_at": _format_utc(file_orchestration_journal._utcnow()),
            }
            payloads: list[tuple[str, dict[str, Any], str | None]] = [
                ("pipeline_job", master, None),
                ("pipeline_event", event, None),
            ]
            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            self._write_pipeline_job_direct_unlocked(master, records[0])
            return AcceptedSubmitCommitResult("applied", _public_scheduler_row(master))

    def record_pipeline_job_reconciliation(
        self,
        job_id: str,
        *,
        submit_outcome: str | None = None,
        reconciliation_decision: str | None = None,
        matched_slurm_job_id: str | None = None,
        candidate_projections: Sequence[Mapping[str, Any]] | None = None,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        """Compatibility/projection API backed by complete typed transitions."""

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        # Writer-authority closed world (#1805): the legacy reconciliation
        # recorder accepts raw decision strings, so it would mint the typed
        # operator authority without the dedicated audited demotion.  Refuse
        # before any read, lock acquisition, row construction, durable
        # mutation, or event; legal legacy decisions keep this API.
        if reconciliation_decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="reconciliation_decision",
            )
        initial = self._pipeline_job_for_id_unlocked(job_id)
        if initial is None:
            return None
        if accepted_submit_contract_is_current(initial) and accepted_submit_row_kind(initial) == "master":
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="pipeline_job_reconciliation",
            )
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(job_id)
            if existing is None:
                return None
            if accepted_submit_contract_is_current(existing) and accepted_submit_row_kind(existing) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="pipeline_job_reconciliation",
                )
            row = dict(existing)
            if reconciliation_decision is not None:
                outcome = submit_outcome or str(existing.get("submit_outcome") or "")
                row = apply_accepted_submit_transition(
                    row,
                    AcceptedSubmitTransition.accounting(
                        reconciliation_decision,
                        submit_outcome=outcome,
                        matched_slurm_job_id=matched_slurm_job_id,
                        status=status,
                    ),
                )
            elif submit_outcome is not None:
                row = apply_accepted_submit_transition(
                    row,
                    AcceptedSubmitTransition(submit_outcome=submit_outcome, status=status),
                )
            elif matched_slurm_job_id is not None:
                raise FileOrchestrationJournalError(
                    "file_journal_evidence_invariant_invalid", field="matched_slurm_job_id"
                )
            if candidate_projections is not None:
                try:
                    row["candidate_projections"] = normalize_candidate_projections(
                        candidate_projections,
                        cohort_members=existing.get("cohort_members"),
                    )
                except AcceptedSubmitEvidenceError as error:
                    raise FileOrchestrationJournalError(error.reason, field=error.field) from error
            if status is not None and submit_outcome is None and reconciliation_decision is None:
                row["status"] = status
            row["updated_at"] = _format_utc(file_orchestration_journal._utcnow())
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def permit_pipeline_job_retry(
        self,
        job_id: str,
        *,
        accepted_submit_contract_version: str | None = None,
        expected_submission_attempt: int | None = None,
        expected_submission_attempt_started_at: datetime | None = None,
        expected_status: str = "reserved",
    ) -> int:
        """Move one still-reserved cohort to retryable exactly once under its cycle lock."""

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        versioned = accepted_submit_contract_version == ACCEPTED_SUBMIT_CONTRACT_VERSION
        if versioned and (
            type(expected_submission_attempt) is not int or expected_submission_attempt < 1
        ):
            raise FileOrchestrationJournalError(
                "file_journal_evidence_type_invalid", field="expected_submission_attempt"
            )
        if versioned:
            source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        else:
            initial = self._pipeline_job_for_id_unlocked(job_id)
            if initial is None:
                return 0
            if accepted_submit_contract_is_current(initial) and accepted_submit_row_kind(initial) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="accepted_submit_contract_version",
                )
            source_id = _source_id_from_job(initial)
            cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = (
                self._accepted_submit_job_for_id_unlocked(
                    job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                if versioned
                else self._pipeline_job_for_id_unlocked(job_id)
            )
            if existing is None or str(existing.get("status") or "") != expected_status:
                return 0
            if not versioned and accepted_submit_contract_is_current(existing) and accepted_submit_row_kind(
                existing
            ) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="accepted_submit_contract_version",
                )
            if versioned and (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return 0
            if versioned:
                if expected_submission_attempt_started_at is None:
                    return 0
                try:
                    anchor_matches = _accepted_submit_attempt_anchor(
                        existing.get("submission_attempt_started_at")
                    ) == _accepted_submit_attempt_anchor(expected_submission_attempt_started_at)
                except FileOrchestrationJournalError:
                    return 0
                if not anchor_matches:
                    return 0
            if versioned and existing.get("submission_attempt") != expected_submission_attempt:
                return 0
            if not versioned and expected_submission_attempt is not None and max(
                int(existing.get("submission_attempt") or 1), 1
            ) != max(int(expected_submission_attempt), 1):
                return 0
            if existing.get("slurm_job_id") not in (None, ""):
                return 0
            # This write produces ``absence_retry_permitted``, one of the reclaim doors
            # the auto-retry isolation contract explicitly EXCLUDES: the row is meant to
            # be retried (``_verified_accepted_submit_forecast_retry`` and
            # ``reclaim_pipeline_job_reservation``'s precondition both key off this
            # shape).  The null ``error_code`` here is only the fact that the transition
            # introduces no new code; the isolation contract's subject is the
            # ``identity_mismatch_released`` sub-shape below (spec:
            # candidate-projection-stage-attempt-retention).
            cohort_row = apply_accepted_submit_transition(
                existing,
                AcceptedSubmitTransition.accounting(
                    "absence_retry_permitted",
                    submit_outcome="submit_result_ambiguous",
                    status="reservation_lost",
                ),
            )
            cohort_row.update(
                {
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            attempt = max(int(existing.get("submission_attempt") or 1), 1)
            source_id = _source_id_from_job(existing)
            cycle_time = _cycle_time_from_job(existing)
            payloads: list[tuple[str, dict[str, Any], str | None]] = []
            touched_models: set[str] = set()
            members = _bounded_cohort_members(existing.get("cohort_members"))
            model_rows = self._cycle_rows_by_model_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_ids=(str(member.get("model_id") or "") for member in members),
            )
            for member in members:
                run_id = str(member.get("run_id") or "")
                model_id = str(member.get("model_id") or "")
                if not run_id or not model_id:
                    continue
                snapshot = model_rows.get(model_id)
                hydro = snapshot.hydro_run if snapshot is not None else None
                if isinstance(hydro, Mapping) and str(hydro.get("run_id") or "") != run_id:
                    hydro = None
                if (
                    hydro is None
                    or int(hydro.get("submission_attempt") or 1) != attempt
                    or str(hydro.get("status") or "") not in ACTIVE_HYDRO_STATUSES
                ):
                    continue
                hydro_row = dict(hydro)
                hydro_row.update(
                    {
                        "status": "failed",
                        "error_code": "SLURM_RESERVATION_LOST",
                        "error_message": "Forecast submission was authoritatively absent; this attempt is retryable.",
                        "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                    }
                )
                payloads.append(("hydro_run", hydro_row, model_id))
                touched_models.add(model_id)
            payloads.append(("pipeline_job", cohort_row, _optional_safe_identity(cohort_row, "model_id")))
            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            self._write_pipeline_job_direct_unlocked(cohort_row, records[-1])
            for model_id in sorted(touched_models):
                self._materialize_latest_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    include_direct_jobs=False,
                )
            return len(records)
