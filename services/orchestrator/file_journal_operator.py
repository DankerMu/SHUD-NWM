"""Operator demote/bind cluster of the file orchestration journal, as a repository mixin.

The operator-verified writers for a held, unbound ``reserved`` pipeline job.
``demote_operator_verified_reserved_job`` turns an operator-verified dead
reservation into the reclaimable ``reservation_lost`` /
``operator_verified_absence`` terminal; ``bind_operator_verified_reserved_job``
binds a held master whose job did run to the operator-verified Slurm master.
Each re-reads the row under the cycle write lock, compares every durable field
and appends the master together with its operator audit event.  A held forcing
master is dispatched to ``_demote_operator_verified_forcing_locked`` /
``_bind_operator_verified_forcing_locked``, and ``_forcing_demote_input_refusal``
names the refusal a forcing row gives an invalid demote attestation.  The lock,
the row lookups and the writes are facade-class methods reached through
``self``.

The receipt and result dataclasses, the bind audit, the ``_OPERATOR_BIND_*``
constants and the journal's error type stay in the facade: the result types
are imported under ``TYPE_CHECKING`` for annotations and, with the others,
inside the methods that construct or raise them.  The facade-resident patched
clock is looked up on the facade module at call time; ``reconcile`` is imported
inside the methods because it imports the facade at load time.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
    OPERATOR_VERIFIED_ABSENCE_DECISION,
    AcceptedSubmitTransition,
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
    apply_accepted_submit_transition,
    is_forecast_cohort_stage_name,
    normalize_slurm_accounting_submitted_at,
)
from services.orchestrator.file_journal_candidates import (
    _accepted_submit_source_cycle_from_job_id,
    _forcing_submitline_array_spec,
)
from services.orchestrator.file_journal_paths import _optional_safe_identity
from services.orchestrator.file_journal_read_attribution import _install_public_read_attribution
from services.orchestrator.file_journal_retry_helpers import (
    _accepted_submit_attempt_anchor,
    _bounded_cohort_members,
    _journal_record_for_write,
    _operator_evidence_text,
    _operator_owner_evidence_text,
    _projection_error_reason,
    _projection_error_type,
    _redact_durable_error_message_fields,
    _strict_utc_datetime,
)
from services.orchestrator.forcing_submit_identity import forcing_submit_identity_is_complete, is_forcing_stage_name
from services.orchestrator.reservation import SLURM_COMMENT_PREFIX
from services.orchestrator.scheduler_state import _format_utc
from services.orchestrator.scheduler_state_types import ACTIVE_HYDRO_STATUSES

if TYPE_CHECKING:
    from services.orchestrator.file_orchestration_journal import (
        OperatorBindResult,
        OperatorDemoteReceipt,
        OperatorDemoteResult,
    )


@_install_public_read_attribution
class _FileJournalOperatorMixin:
    def demote_operator_verified_reserved_job(
        self,
        job_id: str,
        *,
        accepted_submit_contract_version: str | None,
        expected_submission_attempt: int,
        expected_submission_attempt_started_at: datetime,
        checked_by: str,
        checked_at: datetime,
        verification_note: str,
    ) -> OperatorDemoteReceipt | OperatorDemoteResult | None:
        """Atomically demote one operator-verified dead comment-unobservable reservation.

        File-journal-only (#1564).  The cluster does not store job comments
        (#1116), so no automatic reconcile pass can prove absence; an operator
        who independently verified the job dead with Slurm evidence uses this
        typed CAS to convert the narrowly-defined held shape
        (``reserved`` / ``submit_result_ambiguous`` /
        ``slurm_exact_comment`` / ``accounting_unavailable`` /
        ``comment_accounting_unproven``, unbound) into the reclaimable
        ``reservation_lost`` / ``operator_verified_absence`` terminal, with a
        durable audit event carrying the operator evidence.

        Everything is re-read under the cycle lock and every durable field is
        compared, so a concurrent bind / permit / release / demote / reclaim
        that advanced the row before this request obtained the lock loses the
        compare-and-swap and this method writes zero bytes.  Invalid input
        types/enums raise ``FileOrchestrationJournalError`` before any write;
        a stale or mismatched durable row returns ``None`` (never raises).

        On success the returned :class:`OperatorDemoteReceipt` carries the
        exact normalized, secret-redacted operator strings the audit event
        durably recorded, so a CLI can print the receipt without ever
        re-deriving (or leaking) the raw inputs.  ``written_record_count`` is
        the number of durable records appended (master + audit event + any
        active hydro projections).

        #2682: a held FORCING master (``is_forcing_stage_name`` on the row
        re-read under the cycle lock) is dispatched BEFORE the contract /
        row-kind checks, because forcing reservations carry no accepted-submit
        contract version; see :meth:`_demote_operator_verified_forcing_locked`.
        Only that branch returns an :class:`OperatorDemoteResult`, whose
        ``refusal`` is one of :data:`OPERATOR_DEMOTE_REFUSALS`; a forcing row
        also turns a blank attestation or an unreadable ``checked_at`` into a
        named refusal instead of the typed error.  Forecast rows and unknown
        job ids keep the contract above.
        """

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            FileOrchestrationJournalError,
            OperatorDemoteReceipt,
            OperatorDemoteResult,
            ProjectionWarning,
        )

        if accepted_submit_contract_version != ACCEPTED_SUBMIT_CONTRACT_VERSION:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid",
                field=ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
            )
        if type(expected_submission_attempt) is not int or expected_submission_attempt < 1:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_type_invalid", field="expected_submission_attempt"
            )
        if expected_submission_attempt_started_at is None:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_required", field="expected_submission_attempt_started_at"
            )
        try:
            normalized_checked_at = _accepted_submit_attempt_anchor(checked_at)
            checked_by_text = _operator_evidence_text(checked_by, field="checked_by")
            verification_note_text = _operator_evidence_text(verification_note, field="verification_note")
            expected_anchor = _accepted_submit_attempt_anchor(expected_submission_attempt_started_at)
        except FileOrchestrationJournalError:
            # #2682: only a forcing row names these; the lane is known only
            # from the locked read, and everything else keeps the typed error.
            refusal = self._forcing_demote_input_refusal(
                job_id,
                checked_by=checked_by,
                checked_at=checked_at,
                verification_note=verification_note,
            )
            if refusal is None:
                raise
            return OperatorDemoteResult(refusal)
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None:
                return None
            if is_forcing_stage_name(existing.get("stage"), existing.get("job_type")):
                # #2682: forcing rows carry no accepted-submit contract version,
                # so they branch BEFORE the contract / row-kind checks below.
                return self._demote_operator_verified_forcing_locked(
                    existing,
                    job_id=job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    expected_submission_attempt=expected_submission_attempt,
                    expected_anchor=expected_anchor,
                    checked_by_text=checked_by_text,
                    normalized_checked_at=normalized_checked_at,
                    verification_note_text=verification_note_text,
                )
            if (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
                or str(existing.get("status") or "") != "reserved"
                or existing.get("slurm_job_id") not in (None, "")
                or existing.get("matched_slurm_job_id") is not None
                or existing.get("submit_outcome") != "submit_result_ambiguous"
                or existing.get("reconciliation_source") != "slurm_exact_comment"
                or existing.get("reconciliation_decision") != "accounting_unavailable"
                or existing.get("reconciliation_reason_class") != "comment_accounting_unproven"
                or existing.get("submission_attempt") != expected_submission_attempt
                or _accepted_submit_attempt_anchor(existing.get("submission_attempt_started_at"))
                != expected_anchor
            ):
                return None
            # The operator's old anchor is only a CAS expectation, never a new
            # authority value: the post-state keeps the persisted attempt and
            # anchor untouched, so reclaim later mints attempt+1 under its own
            # lock-owned anchor.
            cohort_row = apply_accepted_submit_transition(
                existing,
                AcceptedSubmitTransition.accounting(
                    OPERATOR_VERIFIED_ABSENCE_DECISION,
                    submit_outcome="submit_result_ambiguous",
                    status="reservation_lost",
                ),
            )
            cohort_row["updated_at"] = _format_utc(file_orchestration_journal._utcnow())
            attempt = max(int(existing.get("submission_attempt") or 1), 1)
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
                        "error_message": "Operator verified the forecast submission absent; this attempt is retryable.",
                        "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                    }
                )
                payloads.append(("hydro_run", hydro_row, model_id))
                touched_models.add(model_id)
            payloads.append(("pipeline_job", cohort_row, _optional_safe_identity(cohort_row, "model_id")))
            event_id = self._next_accepted_submit_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
            )
            event = {
                "event_id": event_id,
                "entity_type": "pipeline_job",
                "entity_id": str(cohort_row["job_id"]),
                "event_type": "operator_verified_absence",
                "status_from": "reserved",
                "status_to": "reservation_lost",
                "message": "Operator verified the comment-unobservable reservation absent.",
                "details": {
                    "checked_by": checked_by_text,
                    "checked_at": normalized_checked_at,
                    "verification_note": verification_note_text,
                    "expected_submission_attempt": expected_submission_attempt,
                    "expected_submission_attempt_started_at": expected_anchor,
                    "prior_reconciliation_decision": str(
                        existing.get("reconciliation_decision") or ""
                    ),
                    "prior_reconciliation_reason_class": str(
                        existing.get("reconciliation_reason_class") or ""
                    ),
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
            # The append above is the authority commit point.  Direct/latest
            # files are derived projections that cannot be rolled back with the
            # journal: a failure here is contained to a bounded warning (the
            # operation is already committed), every remaining independent
            # projection is still attempted, and journal replay stays the
            # source of truth for later reads/writes.  Never raise a false
            # operation failure after authority state and audit evidence are
            # durable -- a repeated operator request then loses CAS without
            # appending a duplicate decision.
            warnings: list[ProjectionWarning] = []
            try:
                self._write_pipeline_job_direct_unlocked(cohort_row, records[-2])
            except Exception as error:
                warnings.append(
                    ProjectionWarning(
                        projection="pipeline_job_direct",
                        model_id=None,
                        error_type=_projection_error_type(error),
                        reason=_projection_error_reason(error),
                    )
                )
            for model_id in sorted(touched_models):
                try:
                    self._materialize_latest_unlocked(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        model_id=model_id,
                        include_direct_jobs=False,
                    )
                except Exception as error:
                    warnings.append(
                        ProjectionWarning(
                            projection="latest",
                            model_id=model_id,
                            error_type=_projection_error_type(error),
                            reason=_projection_error_reason(error),
                        )
                    )
            return OperatorDemoteReceipt(
                job_id=str(cohort_row["job_id"]),
                journal_root=str(self.root),
                status_from=str(existing.get("status") or ""),
                status_to=str(cohort_row.get("status") or ""),
                reconciliation_decision=str(cohort_row.get("reconciliation_decision") or ""),
                submission_attempt=attempt,
                submission_attempt_started_at=str(cohort_row.get("submission_attempt_started_at") or ""),
                checked_by=checked_by_text,
                checked_at=normalized_checked_at,
                verification_note=verification_note_text,
                written_record_count=len(records),
                warnings=tuple(warnings),
            )

    def _forcing_demote_input_refusal(
        self,
        job_id: str,
        *,
        checked_by: Any,
        checked_at: Any,
        verification_note: Any,
    ) -> str | None:
        """#2682: the named refusal a FORCING row gives an invalid attestation, else ``None``.

        Called only after the entry validation of
        :meth:`demote_operator_verified_reserved_job` failed.  The row is read
        under the cycle lock solely to learn the lane; nothing is written.
        ``None`` (no readable forcing row, or an input error this branch does
        not name, such as an oversized note) makes the caller re-raise its
        typed error, which is the forecast contract.
        """

        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        try:
            source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
            with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
                existing = self._accepted_submit_job_for_id_unlocked(
                    job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
        except FileOrchestrationJournalError:
            return None
        if existing is None or not is_forcing_stage_name(existing.get("stage"), existing.get("job_type")):
            return None
        if any(not isinstance(value, str) or not value.strip() for value in (checked_by, verification_note)):
            return "attestation_missing"
        if _strict_utc_datetime(checked_at) is None:
            return "verification_before_grace"
        return None

    def _demote_operator_verified_forcing_locked(
        self,
        existing: dict[str, Any],
        *,
        job_id: str,
        source_id: str,
        cycle_time: datetime,
        expected_submission_attempt: int,
        expected_anchor: str,
        checked_by_text: str,
        normalized_checked_at: str,
        verification_note_text: str,
    ) -> OperatorDemoteResult:
        """#2682: the forcing branch of the operator demotion; caller holds the cycle lock.

        The operator-verified absence exit for a held forcing master that no
        reconcile pass can release: ``query_unavailable`` is a reconcile
        answer, not a row field, so the operator's sacct/squeue verification
        replaces the reconcile-computed ``credible_absence``.

        CAS, every failure a named refusal with zero bytes: a complete forcing
        submit identity else ``identity_incomplete``; the held tuple of
        :meth:`_bind_operator_verified_forcing_locked` (``reserved``, no bound
        or matched id, ``submit_result_ambiguous``, no reconciliation decision)
        else ``not_held``; the expected attempt and anchor else
        ``stale_attempt``; ``checked_at`` not in the future and at least
        ``reconcile.RESERVATION_ABSENCE_GRACE`` after the anchor (the grace of
        reconcile's forcing ``credible_absence`` branch) else
        ``verification_before_grace``.

        Post-state: exactly the ``permit_forcing_submit_retry`` row
        (``reservation_lost`` / ``absence_retry_permitted``, the only decision
        forcing reclaim honours) plus one ``operator_verified_absence`` event,
        in one durable append.  The attestation lives only in that event, so
        reclaim cannot carry it into attempt+1.  Never an sbatch or scancel.
        """

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            FileOrchestrationJournalError,
            OperatorDemoteReceipt,
            OperatorDemoteResult,
            ProjectionWarning,
        )

        if not forcing_submit_identity_is_complete(existing):
            return OperatorDemoteResult("identity_incomplete")
        if (
            str(existing.get("status") or "") != "reserved"
            or existing.get("slurm_job_id") not in (None, "")
            or existing.get("matched_slurm_job_id") not in (None, "")
            or existing.get("submit_outcome") != "submit_result_ambiguous"
            or existing.get("reconciliation_decision") not in (None, "")
        ):
            return OperatorDemoteResult("not_held")
        try:
            current_anchor = _accepted_submit_attempt_anchor(existing.get("submission_attempt_started_at"))
        except FileOrchestrationJournalError:
            return OperatorDemoteResult("stale_attempt")
        if existing.get("submission_attempt") != expected_submission_attempt or current_anchor != expected_anchor:
            return OperatorDemoteResult("stale_attempt")
        # Lazy import: ``reconcile`` imports this module at load time.
        from services.orchestrator import reconcile

        anchor_instant = _strict_utc_datetime(current_anchor)
        checked_instant = _strict_utc_datetime(normalized_checked_at)
        # Datetimes, never strings.  Before the grace an absent job may merely
        # be slurmdbd propagation lag; a verification dated in the future
        # proves nothing about now.
        if (
            anchor_instant is None
            or checked_instant is None
            or checked_instant > file_orchestration_journal._utcnow()
            or checked_instant < anchor_instant + reconcile.RESERVATION_ABSENCE_GRACE
        ):
            return OperatorDemoteResult("verification_before_grace")
        # Exactly the ``permit_forcing_submit_retry`` post-state.
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
        row = {**_redact_durable_error_message_fields("pipeline_job", row), "source_id": source_id}
        model_id = _optional_safe_identity(row, "model_id")
        event = {
            "event_id": self._next_accepted_submit_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
            ),
            "entity_type": "pipeline_job",
            "entity_id": str(row["job_id"]),
            "event_type": "operator_verified_absence",
            "status_from": "reserved",
            "status_to": "reservation_lost",
            "message": "Operator verified that no Slurm job carries the held forcing attempt's comment.",
            "details": {
                "lane": "forcing",
                "checked_by": checked_by_text,
                "checked_at": normalized_checked_at,
                "verification_note": verification_note_text,
                "expected_submission_attempt": expected_submission_attempt,
                "expected_submission_attempt_started_at": expected_anchor,
                "slurm_comment": str(existing.get("slurm_comment") or ""),
                "prior_submit_outcome": str(existing.get("submit_outcome") or ""),
            },
            "created_at": _format_utc(file_orchestration_journal._utcnow()),
        }
        next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
        records: list[dict[str, Any]] = []
        for offset, (record_type, payload, record_model_id) in enumerate(
            (("pipeline_job", row, model_id), ("pipeline_event", event, None))
        ):
            record = _journal_record_for_write(
                record_type,
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=record_model_id,
                sequence=next_sequence + offset,
            )
            self._validate_outgoing_record(
                record,
                source_id=source_id,
                cycle_time=cycle_time,
                record_type=record_type,
                model_id=record_model_id,
            )
            records.append(record)
        self._append_journal_records_unlocked(
            source_id=source_id,
            cycle_time=cycle_time,
            records=records,
        )
        # The append above is the authority commit point; a derived
        # direct/latest projection failure is contained to a warning exactly
        # as on the forecast demotion, and journal replay stays authoritative.
        warnings: list[ProjectionWarning] = []
        try:
            self._write_pipeline_job_direct_unlocked(row, records[0])
        except Exception as error:
            warnings.append(
                ProjectionWarning(
                    projection="pipeline_job_direct",
                    model_id=None,
                    error_type=_projection_error_type(error),
                    reason=_projection_error_reason(error),
                )
            )
        if model_id is not None:
            try:
                self._materialize_latest_unlocked(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
            except Exception as error:
                warnings.append(
                    ProjectionWarning(
                        projection="latest",
                        model_id=model_id,
                        error_type=_projection_error_type(error),
                        reason=_projection_error_reason(error),
                    )
                )
        return OperatorDemoteResult(
            None,
            OperatorDemoteReceipt(
                job_id=str(row.get("job_id") or job_id),
                journal_root=str(self.root),
                status_from=str(existing.get("status") or ""),
                status_to=str(row.get("status") or ""),
                reconciliation_decision=str(row.get("reconciliation_decision") or ""),
                submission_attempt=expected_submission_attempt,
                submission_attempt_started_at=current_anchor,
                checked_by=checked_by_text,
                checked_at=normalized_checked_at,
                verification_note=verification_note_text,
                written_record_count=len(records),
                warnings=tuple(warnings),
                lane="forcing",
            ),
        )

    def bind_operator_verified_reserved_job(
        self,
        job_id: str,
        *,
        accepted_submit_contract_version: str | None,
        expected_submission_attempt: int,
        expected_submission_attempt_started_at: datetime | str,
        slurm_job_id: str,
        slurm_submit_time: datetime | str,
        submit_line: str,
        checked_by: str,
        checked_at: datetime | str,
        verification_note: str,
        slurm_user: str | None = None,
        slurm_account: str | None = None,
    ) -> OperatorBindResult:
        """Atomically bind one held forecast master to an operator-verified Slurm master (#2668).

        File-journal-only sibling of :meth:`demote_operator_verified_reserved_job`
        for a held row whose job DID run: the comment-less name-window fallback
        left it ``ambiguous_fallback_match`` or permanently
        ``query_unavailable``, and the operator matched its master in sacct.

        The post-state is exactly the #2655 automatic name-window bind tuple
        (``submitted`` / ``accepted`` / ``slurm_name_window_unique`` /
        ``matched_bound`` + the matched id and the canonical accounting
        Submit), written by the same typed-commit body under the same cycle
        lock, so no new durable token exists and a rollback stays safe.  The
        ``operator_verified_bind`` audit event lands in the same durable
        append.  Terminal projection stays with the next inflight reconcile.

        CAS, re-read under the cycle lock (design Decisions 2-3): current
        contract forecast cohort master; ``reserved``, unbound,
        ``submit_result_ambiguous`` with the exact held tuple
        ``slurm_exact_comment`` / ``accounting_unavailable`` /
        ``comment_accounting_unproven``; the expected attempt and anchor;
        ``anchor <= slurm_submit_time <= checked_at``; the SubmitLine's single
        ``--comment=`` value equals the row's own ``nhms_idem:<key>``; a bare
        canonical decimal Slurm id, compared by master part, that no other row
        of the same cycle (any stage/kind/contract: forcing, legacy, member task
        ``<id>_<n>``) binds or claims, nor any current accepted-submit forecast
        master of any other cycle, settled or active (strict: a recycled
        incarnation is refused too).

        #2675: a held FORCING master (``is_forcing_stage_name``) is dispatched
        under the same cycle lock BEFORE the contract / row-kind checks, because
        forcing reservations carry no accepted-submit contract version; see
        :meth:`_bind_operator_verified_forcing_locked`.  ``slurm_user`` /
        ``slurm_account`` (the sacct ``User`` / ``Account``) are read only by
        that branch; the forecast path ignores them.  Both lanes validate them
        at entry (:func:`_operator_owner_evidence_text`: bounded, sanitizer-clean,
        blank allowed), so an oversized or unsafe value raises before any read.

        Invalid input types raise ``FileOrchestrationJournalError`` before any
        read; every CAS failure returns a named refusal from
        :data:`OPERATOR_BIND_REFUSALS` and writes zero bytes.
        """

        from services.orchestrator.file_orchestration_journal import (
            _OPERATOR_BIND_COMMIT_REFUSALS,
            _OPERATOR_BIND_SLURM_ID_RE,
            FileOrchestrationJournalError,
            OperatorBindReceipt,
            OperatorBindResult,
            _OperatorBindAudit,
        )

        if accepted_submit_contract_version != ACCEPTED_SUBMIT_CONTRACT_VERSION:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid",
                field=ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
            )
        if type(expected_submission_attempt) is not int or expected_submission_attempt < 1:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_type_invalid", field="expected_submission_attempt"
            )
        if expected_submission_attempt_started_at is None:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_required", field="expected_submission_attempt_started_at"
            )
        if not isinstance(submit_line, str):
            raise FileOrchestrationJournalError("file_journal_evidence_type_invalid", field="submit_line")
        for owner_field, owner_value in (("slurm_user", slurm_user), ("slurm_account", slurm_account)):
            if owner_value is not None and not isinstance(owner_value, str):
                raise FileOrchestrationJournalError("file_journal_evidence_type_invalid", field=owner_field)
        normalized_checked_at = _accepted_submit_attempt_anchor(checked_at)
        checked_by_text = _operator_evidence_text(checked_by, field="checked_by")
        verification_note_text = _operator_evidence_text(verification_note, field="verification_note")
        slurm_user_text = _operator_owner_evidence_text(slurm_user, field="slurm_user")
        slurm_account_text = _operator_owner_evidence_text(slurm_account, field="slurm_account")
        expected_anchor = _accepted_submit_attempt_anchor(expected_submission_attempt_started_at)
        requested_id = slurm_job_id if isinstance(slurm_job_id, str) else ""
        if not _OPERATOR_BIND_SLURM_ID_RE.fullmatch(requested_id):
            # A canonical bare decimal MASTER id only: a leading zero
            # (``0123``), an array task (``123_4``), a step (``123.batch``),
            # or anything else is refused by name.
            return OperatorBindResult("slurm_id_invalid")
        canonical_submit = normalize_slurm_accounting_submitted_at(slurm_submit_time)
        if canonical_submit is None:
            return OperatorBindResult("slurm_submit_time_invalid")
        # The ONE SubmitLine key authority the automatic #2655 fallback uses.
        # Lazy import: ``reconcile`` imports this module at load time.
        from services.orchestrator.reconcile import _submitline_comment_key

        submitline_key = _submitline_comment_key(submit_line)
        try:
            source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        except FileOrchestrationJournalError:
            return OperatorBindResult("not_found")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None:
                return OperatorBindResult("not_found")
            if is_forcing_stage_name(existing.get("stage"), existing.get("job_type")):
                # #2675: forcing rows carry no accepted-submit contract version,
                # so they branch BEFORE the contract / row-kind checks below.
                return self._bind_operator_verified_forcing_locked(
                    existing,
                    job_id=job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    expected_submission_attempt=expected_submission_attempt,
                    expected_anchor=expected_anchor,
                    requested_id=requested_id,
                    canonical_submit=canonical_submit,
                    submitline_key=submitline_key,
                    submit_line=submit_line,
                    slurm_user=slurm_user_text,
                    slurm_account=slurm_account_text,
                    checked_by_text=checked_by_text,
                    normalized_checked_at=normalized_checked_at,
                    verification_note_text=verification_note_text,
                )
            forecast_cohort = is_forecast_cohort_stage_name(
                str(existing.get("stage") or ""), str(existing.get("job_type") or "")
            )
            if not accepted_submit_contract_is_current(existing):
                # Shape (c): the typed commit handles current-contract rows
                # only, so a legacy unversioned master is refused by name
                # (follow-up #2674), never bound through the legacy store path.
                if forecast_cohort and _bounded_cohort_members(existing.get("cohort_members")):
                    return OperatorBindResult("legacy_unversioned_unsupported")
                return OperatorBindResult("not_held")
            if accepted_submit_row_kind(existing) != "master" or not forecast_cohort:
                return OperatorBindResult("not_held")
            # Held shape first, so a repeated request after a successful bind is
            # ``not_held`` and a reclaimed-and-re-held row is ``stale_attempt``.
            if (
                str(existing.get("status") or "") != "reserved"
                or existing.get("slurm_job_id") not in (None, "")
                or existing.get("matched_slurm_job_id") not in (None, "")
                or existing.get("submit_outcome") != "submit_result_ambiguous"
                or existing.get("reconciliation_source") != "slurm_exact_comment"
                or existing.get("reconciliation_decision") != "accounting_unavailable"
                or existing.get("reconciliation_reason_class") != "comment_accounting_unproven"
            ):
                return OperatorBindResult("not_held")
            try:
                current_anchor = _accepted_submit_attempt_anchor(existing.get("submission_attempt_started_at"))
            except FileOrchestrationJournalError:
                return OperatorBindResult("stale_attempt")
            if existing.get("submission_attempt") != expected_submission_attempt or current_anchor != expected_anchor:
                return OperatorBindResult("stale_attempt")
            # The attempt window (design Decision 2): the key alone cannot tell
            # this attempt's master from a prior attempt's, because a released
            # attempt re-reserves under the same key.  Datetimes, never strings.
            anchor_instant = _strict_utc_datetime(current_anchor)
            submit_instant = _strict_utc_datetime(canonical_submit)
            checked_instant = _strict_utc_datetime(normalized_checked_at)
            if (
                anchor_instant is None
                or submit_instant is None
                or checked_instant is None
                or not anchor_instant <= submit_instant <= checked_instant
            ):
                return OperatorBindResult("submit_time_outside_attempt_window")
            own_key = f"{SLURM_COMMENT_PREFIX}{existing.get('idempotency_key') or ''}"
            if submitline_key is None or submitline_key != own_key:
                return OperatorBindResult("submitline_key_mismatch")
            audit = _OperatorBindAudit(
                details={
                    "checked_by": checked_by_text,
                    "checked_at": normalized_checked_at,
                    "verification_note": verification_note_text,
                    "submitline_key": submitline_key,
                    "slurm_job_id": requested_id,
                    "slurm_accounting_submitted_at": canonical_submit,
                    "expected_submission_attempt": expected_submission_attempt,
                    "expected_submission_attempt_started_at": expected_anchor,
                    "prior_status": str(existing.get("status") or ""),
                    "prior_submit_outcome": str(existing.get("submit_outcome") or ""),
                    "prior_reconciliation_source": str(existing.get("reconciliation_source") or ""),
                    "prior_reconciliation_decision": str(existing.get("reconciliation_decision") or ""),
                    "prior_reconciliation_reason_class": str(
                        existing.get("reconciliation_reason_class") or ""
                    ),
                }
            )
            try:
                result = self._commit_pipeline_job_submit_attempt_locked(
                    existing,
                    idempotency_key=str(existing.get("idempotency_key") or ""),
                    pipeline_job_id=job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    expected_submission_attempt=expected_submission_attempt,
                    requested_id=requested_id,
                    transition=AcceptedSubmitTransition.accounting(
                        "matched_bound",
                        submit_outcome="accepted",
                        matched_slurm_job_id=requested_id,
                        status="submitted",
                        reconciliation_source="slurm_name_window_unique",
                    ),
                    array_task_id=None,
                    submitted_at=None,
                    slurm_accounting_submitted_at=canonical_submit,
                    started_at=None,
                    finished_at=None,
                    exit_code=None,
                    error_code=None,
                    error_message=None,
                    log_uri=None,
                    fallback_submitline_key=submitline_key,
                    operator_bind=audit,
                )
            except FileOrchestrationJournalError as error:
                if error.reason == "file_journal_submit_instant_required":
                    return OperatorBindResult("slurm_submit_time_invalid")
                raise
            if result.outcome != "applied" or result.row is None:
                return OperatorBindResult(_OPERATOR_BIND_COMMIT_REFUSALS.get(result.outcome, "not_held"))
            bound = result.row
            return OperatorBindResult(
                None,
                OperatorBindReceipt(
                    job_id=str(bound.get("job_id") or job_id),
                    journal_root=str(self.root),
                    status_from=str(existing.get("status") or ""),
                    status_to=str(bound.get("status") or ""),
                    reconciliation_source=str(bound.get("reconciliation_source") or ""),
                    reconciliation_decision=str(bound.get("reconciliation_decision") or ""),
                    matched_slurm_job_id=str(bound.get("matched_slurm_job_id") or ""),
                    slurm_accounting_submitted_at=canonical_submit,
                    submitline_key=submitline_key,
                    submission_attempt=expected_submission_attempt,
                    submission_attempt_started_at=current_anchor,
                    checked_by=checked_by_text,
                    checked_at=normalized_checked_at,
                    verification_note=verification_note_text,
                    written_record_count=audit.written_record_count,
                    warnings=tuple(audit.warnings),
                ),
            )

    def _bind_operator_verified_forcing_locked(
        self,
        existing: dict[str, Any],
        *,
        job_id: str,
        source_id: str,
        cycle_time: datetime,
        expected_submission_attempt: int,
        expected_anchor: str,
        requested_id: str,
        canonical_submit: str,
        submitline_key: str | None,
        submit_line: str,
        slurm_user: str,
        slurm_account: str,
        checked_by_text: str,
        normalized_checked_at: str,
        verification_note_text: str,
    ) -> OperatorBindResult:
        """#2675: the forcing branch of the operator bind; caller holds the cycle lock.

        CAS (design Decision 2), every failure a named refusal with zero bytes:
        complete forcing submit identity and the held shape (``reserved``, no
        bound or matched id, ``submit_result_ambiguous``, no reconciliation
        decision) else ``not_held``; the expected attempt and anchor else
        ``stale_attempt``; ``floor(anchor) <= submit <= checked_at`` (the anchor
        floored to whole seconds: sacct ``Submit`` has whole-second precision)
        else ``submit_time_outside_attempt_window``; the SubmitLine's single
        ``--comment=`` value equals the durable attempt comment
        (``slurm_comment``) else ``submitline_key_mismatch``; exactly one
        ``--array=0-<n-1>[%k]`` token for the ``n`` cohort members else
        ``array_spec_mismatch``; the owner rule of inflight
        ``reconcile._forcing_accounting_identity_matches`` else
        ``slurm_owner_mismatch``; then, under the journal-global inventory
        lock, the #2668 bounded claimant exclusivity keyed by the forcing
        attempt comment (so no held forecast master is a window claimant)
        plus the same-cycle scan, else ``slurm_id_claimed``.

        Post-state: exactly the ``bind_forcing_submit_attempt`` tuple (no
        ``slurm_binding_source``, no ``slurm_accounting_submitted_at``) plus one
        ``operator_verified_bind`` event, in one durable append.  Never the
        accepted-submit typed commit: that path is forecast-master only and
        writes provenance the automatic forcing bind never writes.
        """

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            _OPERATOR_BIND_FORCING_ARRAY_SPEC_RE,
            FileOrchestrationJournalError,
            OperatorBindReceipt,
            OperatorBindResult,
            _OperatorBindAudit,
        )

        if not forcing_submit_identity_is_complete(existing):
            return OperatorBindResult("not_held")
        if (
            str(existing.get("status") or "") != "reserved"
            or existing.get("slurm_job_id") not in (None, "")
            or existing.get("matched_slurm_job_id") not in (None, "")
            or existing.get("submit_outcome") != "submit_result_ambiguous"
            or existing.get("reconciliation_decision") not in (None, "")
        ):
            return OperatorBindResult("not_held")
        try:
            current_anchor = _accepted_submit_attempt_anchor(existing.get("submission_attempt_started_at"))
        except FileOrchestrationJournalError:
            return OperatorBindResult("stale_attempt")
        if existing.get("submission_attempt") != expected_submission_attempt or current_anchor != expected_anchor:
            return OperatorBindResult("stale_attempt")
        anchor_instant = _strict_utc_datetime(current_anchor)
        submit_instant = _strict_utc_datetime(canonical_submit)
        checked_instant = _strict_utc_datetime(normalized_checked_at)
        # sacct ``Submit`` is whole-second while the durable anchor keeps
        # microseconds, so a same-second submit must not fall before it; the
        # attempt-comment check below still excludes every earlier attempt.
        if (
            anchor_instant is None
            or submit_instant is None
            or checked_instant is None
            or not anchor_instant.replace(microsecond=0) <= submit_instant <= checked_instant
        ):
            return OperatorBindResult("submit_time_outside_attempt_window")
        # ``forcing_submit_identity_is_complete`` already proved the durable
        # comment is this attempt's ``forcing_attempt_comment_for(key, attempt)``.
        attempt_comment = str(existing.get("slurm_comment") or "")
        if submitline_key is None or submitline_key != attempt_comment:
            return OperatorBindResult("submitline_key_mismatch")
        array_spec = _forcing_submitline_array_spec(submit_line)
        # Identity completeness proved ``cohort_members`` is a sequence whose
        # task ids are exactly ``0..n-1`` (unbounded: no 256-row display cap).
        member_count = len(existing["cohort_members"])
        array_match = (
            _OPERATOR_BIND_FORCING_ARRAY_SPEC_RE.fullmatch(array_spec) if array_spec is not None else None
        )
        if array_match is None or int(array_match.group(1)) + 1 != member_count:
            return OperatorBindResult("array_spec_mismatch")
        # The owner rule of inflight ``_forcing_accounting_identity_matches``,
        # verbatim, with the operator's sacct ``User`` / ``Account`` as the record.
        expected_user = str(existing.get("expected_slurm_user") or "")
        expected_account = str(existing.get("expected_slurm_account") or "")
        if bool(existing.get("slurm_ownership_required", False)) and (
            not expected_user or not expected_account or not slurm_user or not slurm_account
        ):
            return OperatorBindResult("slurm_owner_mismatch")
        if expected_user and slurm_user != expected_user:
            return OperatorBindResult("slurm_owner_mismatch")
        if expected_account and slurm_account != expected_account:
            return OperatorBindResult("slurm_owner_mismatch")
        audit = _OperatorBindAudit(
            details={
                "lane": "forcing",
                "checked_by": checked_by_text,
                "checked_at": normalized_checked_at,
                "verification_note": verification_note_text,
                "submitline_key": attempt_comment,
                "array_spec": array_spec,
                "slurm_job_id": requested_id,
                "slurm_submit_time": canonical_submit,
                "slurm_user": slurm_user,
                "slurm_account": slurm_account,
                "expected_submission_attempt": expected_submission_attempt,
                "expected_submission_attempt_started_at": expected_anchor,
                "prior_status": str(existing.get("status") or ""),
                "prior_submit_outcome": str(existing.get("submit_outcome") or ""),
                "prior_reconciliation_source": str(existing.get("reconciliation_source") or ""),
                "prior_reconciliation_decision": str(existing.get("reconciliation_decision") or ""),
                "prior_reconciliation_reason_class": str(existing.get("reconciliation_reason_class") or ""),
            }
        )
        with self._reconcile_inventory_file_lock_unlocked():
            # Cycle lock -> inventory lock, held through the write, so no
            # concurrent writer can bind this id between the scan and the append.
            entry_names = self._reconcile_inventory_entry_names_unlocked()
            other_masters, ambiguous = self._reconcile_inventory_jobs_matching_unlocked(
                entry_names,
                expected_user=expected_user,
                expected_account=expected_account,
                candidate_submit=submit_instant,
                active_slurm_job_id=requested_id,
                include_job_id=str(existing.get("job_id") or job_id),
                fallback_unique=True,
                submitline_key=attempt_comment,
                strict_slurm_id_exclusivity=True,
            )
            if ambiguous or other_masters:
                return OperatorBindResult("slurm_id_claimed")
            if self._operator_bind_slurm_id_claimed_in_cycle_unlocked(
                requested_id,
                include_job_id=str(existing.get("job_id") or job_id),
                source_id=source_id,
                cycle_time=cycle_time,
            ):
                return OperatorBindResult("slurm_id_claimed")
            # Exactly the ``bind_forcing_submit_attempt`` post-state.
            row = apply_accepted_submit_transition(existing, AcceptedSubmitTransition.accepted(status="submitted"))
            row.update(
                {
                    "slurm_job_id": requested_id,
                    "submitted_at": existing.get("submitted_at") or _format_utc(file_orchestration_journal._utcnow()),
                    "submit_outcome": "accepted",
                    "reconciliation_source": "slurm_exact_comment",
                    "reconciliation_decision": "matched_bound",
                    "matched_slurm_job_id": requested_id,
                    "reconciliation_reason_class": None,
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            result = self._write_operator_bind_unlocked(
                row,
                existing,
                audit,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=_optional_safe_identity(row, "model_id"),
            )
        bound = result.row or row
        return OperatorBindResult(
            None,
            OperatorBindReceipt(
                job_id=str(bound.get("job_id") or job_id),
                journal_root=str(self.root),
                status_from=str(existing.get("status") or ""),
                status_to=str(bound.get("status") or ""),
                reconciliation_source=str(bound.get("reconciliation_source") or ""),
                reconciliation_decision=str(bound.get("reconciliation_decision") or ""),
                matched_slurm_job_id=str(bound.get("matched_slurm_job_id") or ""),
                slurm_accounting_submitted_at=bound.get("slurm_accounting_submitted_at"),
                submitline_key=attempt_comment,
                submission_attempt=expected_submission_attempt,
                submission_attempt_started_at=current_anchor,
                checked_by=checked_by_text,
                checked_at=normalized_checked_at,
                verification_note=verification_note_text,
                written_record_count=audit.written_record_count,
                warnings=tuple(audit.warnings),
                lane="forcing",
                array_spec=array_spec,
                slurm_submit_time=canonical_submit,
            ),
        )
