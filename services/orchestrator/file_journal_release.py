"""Identity-blocked release/recover cluster of the file orchestration journal, as a repository mixin.

The typed release of a reservation whose submit identity stayed unverifiable
(``release_identity_blocked_reservation``), the operator-gated recovery of such
a released cohort (``recover_released_identity_blocked_reservation``), and the
in-window pipeline-job event append that writes the release's operator signal
(``_append_pipeline_job_event_unlocked``).  The facade core
(``_emit_committed_projection_fault_event``) also calls that append; the call
resolves through the MRO.  ``_record_identity_released_signal_failure``, the
degraded trace written when the operator signal fails, stays in the facade
class body under the LOGGER rule: it logs through the facade ``LOGGER``, and
every ``LOGGER`` user stays in the facade because tests filter on the facade's
logger name.  The journal's error type and the facade-defined signal constant,
recovery-command helper and attestation field are imported inside the methods
that use them.  The facade-resident patched clock is looked up on the facade
module at call time.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from packages.common.safe_fs import SafeFilesystemError
from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
    IDENTITY_MISMATCH_RELEASED_DECISION,
    AcceptedSubmitTransition,
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
    apply_accepted_submit_transition,
)
from services.orchestrator.chain_types import OrchestratorError
from services.orchestrator.file_journal_candidates import (
    _accepted_submit_source_cycle_from_job_id,
    _public_scheduler_row,
)
from services.orchestrator.file_journal_paths import _optional_safe_identity
from services.orchestrator.file_journal_primitives import _required_safe_identity
from services.orchestrator.file_journal_read_attribution import _install_public_read_attribution
from services.orchestrator.file_journal_retry_helpers import _accepted_submit_attempt_anchor
from services.orchestrator.scheduler_state import _format_utc


@_install_public_read_attribution
class _FileJournalReleaseMixin:
    def release_identity_blocked_reservation(
        self,
        job_id: str,
        *,
        accepted_submit_contract_version: str | None = None,
        expected_submission_attempt: int | None = None,
        expected_submission_attempt_started_at: datetime | None = None,
        expected_status: str = "reserved",
        identity_blocked_streak: int = 0,
    ) -> int:
        """Abandon one reservation whose identity stayed unverifiable for too long.

        Dedicated typed API on purpose: the generic evidence transition may not
        change master status and its decision whitelist stays closed. The CAS
        discipline mirrors :meth:`permit_pipeline_job_retry` (expected attempt,
        attempt anchor, expected status, unbound required), so a concurrently
        advanced attempt loses the race and the caller keeps its blocked outcome.

        The released row is a deliberate non-reclaimable terminal: reclaim
        accepts exactly two lower-level decisions -- ``absence_retry_permitted``
        and the #1564 ``operator_verified_absence`` -- and an
        ``identity_mismatch_released`` row matches neither, so this idempotency
        key is spent.  Liveness comes from the next attempt minting a
        retry-suffixed key.
        """

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            IDENTITY_RELEASED_OPERATOR_SIGNAL,
            FileOrchestrationJournalError,
            _released_reservation_recovery_command,
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
        if type(identity_blocked_streak) is not int or identity_blocked_streak < 1:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_type_invalid", field="identity_blocked_streak"
            )
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None or str(existing.get("status") or "") != expected_status:
                return 0
            if (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return 0
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
            if existing.get("submission_attempt") != expected_submission_attempt:
                return 0
            if existing.get("slurm_job_id") not in (None, ""):
                return 0
            # Released rows stay outside automatic retry BECAUSE this transition adds no
            # ``error_code``: stamping a transient one here (SLURM_RESERVATION_LOST is on
            # the transient list) would turn every release into an automatic duplicate
            # submission (spec: candidate-projection-stage-attempt-retention).
            row = apply_accepted_submit_transition(
                existing,
                AcceptedSubmitTransition.accounting(
                    IDENTITY_MISMATCH_RELEASED_DECISION,
                    submit_outcome="submit_result_ambiguous",
                    status="reservation_lost",
                    identity_blocked_streak=identity_blocked_streak,
                ),
            )
            row["updated_at"] = _format_utc(file_orchestration_journal._utcnow())
            model_id = _optional_safe_identity(row, "model_id")
            written = self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)
            if written is None:
                return 0
            # #1748: the released terminal is otherwise SILENT -- the row is
            # indistinguishable from an ordinary in-flight reservation until a
            # human happens to read the journal, and nothing automatic can
            # revive it (that is deliberate, see the error_code comment above).
            # This is the single release write point, so emitting here is
            # exactly-once per release and covers BOTH prior states (a fresh
            # reservation and a reclaim re-seed).  Emitting at the
            # ``reconcile.py`` caller instead would double-emit, since it is the
            # sole caller.  Gated on the successful write so a CAS refusal above
            # stays write-free and signal-free.
            # The release bytes are already appended and ``_locked_cycle_write``
            # has NO rollback, so a raise here would leave the row durably
            # released with no operator record -- permanently, because the
            # reconcile loop iterates ``query_reserved_unbound_jobs()`` and
            # never re-enters this door for a ``reservation_lost`` row.  Same
            # best-effort discipline as ``_record_permanent_failure_mark_failure``:
            # the evidence write exists to keep the terminal from being silent,
            # never to turn a correct durable release into a raise.  The catch
            # spans the filesystem/OS class too: ``_append_journal_record_unlocked``
            # and ``_materialize_latest_unlocked`` write through the safe-filesystem
            # layer and raise ``SafeFilesystemError``/``OSError``, which
            # ``reconcile.py``'s enclosing ``except FileOrchestrationJournalError``
            # would NOT catch -- it would abort the whole reconcile pass.
            try:
                self._append_pipeline_job_event_unlocked(
                    row,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    event_type=IDENTITY_RELEASED_OPERATOR_SIGNAL,
                    status_from=expected_status,
                    status_to="reservation_lost",
                    message=(
                        f"{IDENTITY_RELEASED_OPERATOR_SIGNAL} pipeline job {job_id} released an "
                        "identity-blocked reservation; the cohort is a permanent terminal until an "
                        "operator invokes recover_released_identity_blocked_reservation."
                    ),
                    details={
                        "pipeline_job_id": job_id,
                        "cohort_digest": row.get("cohort_digest"),
                        "identity_blocked_streak": identity_blocked_streak,
                        "recovery_api": "recover_released_identity_blocked_reservation",
                        # Both the API and the surface a human can actually
                        # reach: naming only the former is the round-3 defect.
                        "operator_command": _released_reservation_recovery_command(job_id),
                    },
                )
            except (OrchestratorError, FileOrchestrationJournalError, SafeFilesystemError, OSError) as error:
                self._record_identity_released_signal_failure(
                    job_id,
                    row,
                    error,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
            return 1

    def _append_pipeline_job_event_unlocked(
        self,
        row: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        message: str | None,
        details: Mapping[str, Any],
        materialize: bool = True,
    ) -> None:
        """Append one pipeline_job event from inside an open cycle write window.

        ``insert_pipeline_event`` cannot be reused here: it re-enters
        ``_locked_cycle_write``, whose ``finally`` clears ``_cycle_write_owner``
        for the still-open outer window and whose second ``flock`` on the same
        cycle lock file would block this thread against itself.
        """

        from services.orchestrator import file_orchestration_journal

        model_id = _optional_safe_identity(row, "model_id")
        event = {
            "event_id": self._next_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            ),
            "entity_type": "pipeline_job",
            "entity_id": _required_safe_identity(row, "job_id"),
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "message": message,
            "details": dict(details),
            "created_at": _format_utc(file_orchestration_journal._utcnow()),
        }
        self._append_validated_record_unlocked(
            "pipeline_event",
            event,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            materialize_model_id=model_id if materialize else None,
        )

    def recover_released_identity_blocked_reservation(
        self,
        job_id: str,
        *,
        accepted_submit_contract_version: str | None = None,
        expected_submission_attempt: int | None = None,
        expected_submission_attempt_started_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Operator-gated liveness for one released identity-blocked cohort (#1748).

        The released row is a permanent terminal for every AUTOMATIC arm: the
        release deliberately withholds ``error_code`` so ``should_auto_retry`` is
        false by construction, and lower-level reclaim accepts exactly two
        absence decisions -- ``absence_retry_permitted`` and the
        typed-demotion-only ``operator_verified_absence``.  This released row
        carries ``identity_mismatch_released``, matches neither, and reaches
        liveness only through the separate operator-attestation disjunct.  This
        door records a durable operator attestation ON THE RELEASED ROW and
        writes **no** successor pipeline-job row.

        Why a marker and not a row: the successor identity the ordinary retry
        path mints is exactly ``_next_current_master_retry_identity``'s, so
        pre-materializing it occupies that ``job_id``/idempotency key,
        ``_pipeline_job_conflicts_unlocked`` then refuses the ordinary reserve,
        reclaim refuses too (the row is ``pending``, not ``reservation_lost``),
        and the pass takes ``_skip_duplicate_submission`` on every later pass --
        permanently inert.  The recovery's only durable output must be an INPUT
        the ordinary submission path already consumes; here that consumer is the
        additive disjunct in
        ``chain_forecast_orchestrator_cycle._terminal_stage_needs_manual_retry``.

        This performs **no** Slurm-side liveness or absence check and must not be
        described as one.  On a cluster whose accounting does not store job
        comments absence is not provable; invoking this is an operator
        attestation, and the judgement deliberately sits with the operator.  For
        the same reason there is no automatic caller, and no automatic path can
        set the marker (it is outside ``_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS`` and
        rejected by the clean-reservation check).

        ``should_auto_retry`` is never consulted and no ``error_code`` is written.

        Returns the attested row, or ``None`` when the call is refused.  Refusal
        is write-free, and a repeat attestation is an idempotent no-op.
        """

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            OPERATOR_RECOVERY_ATTESTATION_FIELD,
            FileOrchestrationJournalError,
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
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None:
                return None
            if (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return None
            if str(existing.get("status") or "") != "reservation_lost":
                return None
            if existing.get("reconciliation_decision") != IDENTITY_MISMATCH_RELEASED_DECISION:
                return None
            if existing.get("slurm_job_id") not in (None, "") or existing.get(
                "matched_slurm_job_id"
            ) not in (None, ""):
                return None
            # CAS mirrors ``release_identity_blocked_reservation``: a
            # concurrently advanced attempt loses the race, write-free.
            if expected_submission_attempt_started_at is None:
                return None
            try:
                anchor_matches = _accepted_submit_attempt_anchor(
                    existing.get("submission_attempt_started_at")
                ) == _accepted_submit_attempt_anchor(expected_submission_attempt_started_at)
            except FileOrchestrationJournalError:
                return None
            if not anchor_matches:
                return None
            if existing.get("submission_attempt") != expected_submission_attempt:
                return None
            if existing.get(OPERATOR_RECOVERY_ATTESTATION_FIELD) not in (None, ""):
                # Idempotent: a second attestation leaves the row byte-identical
                # and emits nothing.  At most one recovered attempt still
                # follows, because the ordinary reservation path's own conflict
                # gate owns that exclusion, not this call.
                return _public_scheduler_row(existing)
            row = {
                **existing,
                OPERATOR_RECOVERY_ATTESTATION_FIELD: _format_utc(file_orchestration_journal._utcnow()),
                "updated_at": _format_utc(file_orchestration_journal._utcnow()),
            }
            return self._write_pipeline_job_unlocked(
                self._pipeline_job_row(row),
                exclusive_direct=False,
                model_id=_optional_safe_identity(row, "model_id"),
            )
