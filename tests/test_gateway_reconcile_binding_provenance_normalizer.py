"""Binding provenance (#1850): the transition and validation surface.

Partition of ``tests/test_gateway_reconcile_binding_provenance.py`` (#3177):
the durable normalizer rejects invalid or non-canonical provenance, typed
commits refuse provenance-dirty inputs, clean reservation and reclaim start
clean, old rows stay readable and fail closed, and the scheduler attempt
evidence exposes the provenance additively. The shared field names and
projection/fallback helpers are in
``tests/gateway_reconcile_binding_provenance_helpers.py``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition, normalize_accepted_submit_evidence
from tests.gateway_reconcile_binding_provenance_helpers import (
    _ANCHOR,
    _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
    _SLURM_BINDING_SOURCE_FIELD,
)
from tests.gateway_reconcile_helpers import (
    _commit_name_window_fallback_bind,
    _file_cohort_repository,
    _versioned_master_reservation_record,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone

# ---------------------------------------------------------------------------
# Transition / validation surface (required matrix item 9)
# ---------------------------------------------------------------------------


def test_normalizer_rejects_invalid_binding_source() -> None:
    """A durable row carrying an unknown binding source is corrupt evidence."""
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitEvidenceError

    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "reconciliation_source": "slurm_name_window_unique",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72001",
                _SLURM_BINDING_SOURCE_FIELD(): "not_a_source",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "2026-07-12T01:00:00Z",
            }
        )
    assert error.value.field == _SLURM_BINDING_SOURCE_FIELD()


def test_normalizer_rejects_naive_noncanonical_accounting_timestamp() -> None:
    """A canonical accounting Submit must be a strict aware-UTC instant."""
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitEvidenceError

    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "reconciliation_source": "slurm_name_window_unique",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72001",
                _SLURM_BINDING_SOURCE_FIELD(): "slurm_name_window_unique",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "not-a-timestamp",
            }
        )
    assert error.value.field == _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()


def test_normalizer_rejects_name_window_source_without_canonical(tmp_path: Any) -> None:
    """An explicit name-window binding source without a strict canonical
    accounting Submit is not a legal fallback bind (#1850 round 2 issue 3)."""
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitEvidenceError

    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "slurm_job_id": "72001",
                "reconciliation_source": "slurm_name_window_unique",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72001",
                _SLURM_BINDING_SOURCE_FIELD(): "slurm_name_window_unique",
            }
        )
    assert error.value.field == _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()


def test_ordinary_accepted_with_canonical_input_rejected_at_typed_commit(
    tmp_path: Any,
) -> None:
    """The ordinary accepted lane cannot carry canonical accounting input.

    Binding provenance is not a transition field (removed in round 2); the
    typed commit derives it from the transition shape. The one forgeable input
    left at the commit boundary is the canonical ``slurm_accounting_submitted_at``
    keyword: an ordinary accepted commit carrying it is refused (the gateway
    lane may never mint canonical accounting Submit) and the journal is
    unchanged."""

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        current = repository.query_candidate_state(key)

        # Ordinary accepted carrying canonical accounting input is refused:
        # the gateway lane may never mint canonical accounting Submit.
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository.commit_pipeline_job_submit_attempt(
                key,
                pipeline_job_id=str(current["job_id"]),
                expected_submission_attempt=1,
                slurm_job_id="72001",
                slurm_accounting_submitted_at=_ANCHOR + timedelta(hours=1),
                transition=AcceptedSubmitTransition.accepted(status="submitted"),
            )
        assert error.value.reason == "file_journal_evidence_invariant_invalid"
        assert error.value.field == _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        # Journal unchanged.
        assert repository.get_pipeline_job(job_id)["status"] == "reserved"
        assert repository.get_pipeline_job(job_id)["slurm_job_id"] is None


def test_name_window_matched_bind_without_canonical_timestamp_fails_typed_commit(
    tmp_path: Any,
) -> None:
    """The name-window fallback bind REQUIRES canonical accounting Submit."""

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        current = repository.query_candidate_state(key)
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository.commit_pipeline_job_submit_attempt(
                key,
                pipeline_job_id=str(current["job_id"]),
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=_ANCHOR + timedelta(hours=1),
                transition=AcceptedSubmitTransition.accounting(
                    "matched_bound",
                    submit_outcome="accepted",
                    matched_slurm_job_id="72001",
                    status="submitted",
                    reconciliation_source="slurm_name_window_unique",
                ),
            )
        assert error.value.reason == "file_journal_submit_instant_required"
        assert error.value.field == _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()


def test_clean_reservation_rejects_binding_provenance_dirty_fields(tmp_path: Any) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    clean = FileOrchestrationJournalRepository(tmp_path / "dirty" / "journal")
    record = _versioned_master_reservation_record(
        member_count=1, expected_user="scheduler", expected_account="account"
    )
    record[_SLURM_BINDING_SOURCE_FIELD()] = "slurm_name_window_unique"
    record[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] = "2026-07-12T01:00:00Z"
    with pytest.raises(FileOrchestrationJournalError) as error:
        clean.reserve_pipeline_job(record)
    assert error.value.reason == "file_journal_clean_reservation_required"
    # The dirty-field set reports the lexicographically-first dirty field; both
    # binding-provenance fields are in the set, so either is the guard talking.
    assert error.value.field in {
        _SLURM_BINDING_SOURCE_FIELD(),
        _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(),
    }
    # Contamination control: the same shape without binding provenance reserves
    # cleanly, so the refusal is the dirty-field guard talking, not a malformed
    # record.
    legal = _versioned_master_reservation_record(
        member_count=1, expected_user="scheduler", expected_account="account"
    )
    assert clean.reserve_pipeline_job(legal) is not None


def test_reclaimable_row_cannot_carry_binding_provenance_and_reclaim_starts_clean(
    tmp_path: Any,
) -> None:
    """Binding provenance is impossible on a reclaimable (unbound) row, and a
    legal reclaim opens a fresh attempt with provenance cleared.

    The typed lifecycle only mints provenance on a successful bind (numeric
    Slurm id). Every path that produces a reclaimable ``reservation_lost`` row
    (operator demotion, permit-retry, release) runs on unbound reserved rows
    that never had provenance, and reclaim nulls the fields directly. So a
    reclaimable row carrying provenance is a durable impossible shape (rejected
    by the normalizer), and the reclaim's new attempt always starts with both
    provenance fields None.
    """

    from services.orchestrator.accepted_submit_identity import (
        OPERATOR_VERIFIED_ABSENCE_DECISION,
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        # The impossible shape: reservation_lost / operator_verified_absence /
        # unbound with explicit name-window provenance is rejected.
        impossible = {
            **_versioned_master_reservation_record(member_count=1),
            "status": "reservation_lost",
            "submit_outcome": "submit_result_ambiguous",
            "reconciliation_source": "slurm_exact_comment",
            "reconciliation_decision": OPERATOR_VERIFIED_ABSENCE_DECISION,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "slurm_job_id": None,
            _SLURM_BINDING_SOURCE_FIELD(): "slurm_name_window_unique",
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "2026-07-12T01:00:00Z",
        }
        with pytest.raises(AcceptedSubmitEvidenceError) as error:
            normalize_accepted_submit_evidence(impossible)
        assert error.value.field == "slurm_job_id"

        # The same impossible shape is refused at the durable write boundary.
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        current = repository.get_pipeline_job(job_id)
        impossible_row = dict(current)
        impossible_row.update(
            {
                "status": "reservation_lost",
                "submit_outcome": "submit_result_ambiguous",
                "reconciliation_source": "slurm_exact_comment",
                "reconciliation_decision": OPERATOR_VERIFIED_ABSENCE_DECISION,
                "reconciliation_reason_class": None,
                "matched_slurm_job_id": None,
                "slurm_job_id": None,
            }
        )
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository._write_pipeline_job_unlocked(
                impossible_row, exclusive_direct=False, model_id=None
            )
        assert error.value.reason == "file_journal_evidence_invariant_invalid"
        # The failed write left the bound row unchanged.
        assert repository.get_pipeline_job(job_id)["slurm_job_id"] == "72001"

        # A legal reclaim only ever applies to an unbound held row (which never
        # carried provenance). Drive the real held-cohort producer + operator
        # demotion door and reclaim it: the new attempt's provenance fields are
        # None and the demote-then-reclaim lifecycle stays intact.
        from tests.orchestrator_demote_reserved_job_helpers import (
            JOB_ID as HELD_JOB_ID,
        )  # noqa: I001 - kept with the held-reclaim helpers it drives.
        from tests.orchestrator_demote_reserved_job_helpers import (
            _demote_kwargs,
            _held_cohort_repository,
        )

        held = _held_cohort_repository(tmp_path / "held", member_count=1)
        held_job_id = HELD_JOB_ID
        held_row = held.get_accepted_submit_pipeline_job(held_job_id)
        assert held_row["status"] == "reserved"
        assert held_row[_SLURM_BINDING_SOURCE_FIELD()] is None
        demoted = held.demote_operator_verified_reserved_job(
            held_job_id,
            **_demote_kwargs(held_row, verification_note="round-3 reclaim provenance test"),
        )
        assert demoted is not None
        released = held.get_accepted_submit_pipeline_job(held_job_id)
        assert released["status"] == "reservation_lost"
        assert released["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
        assert released[_SLURM_BINDING_SOURCE_FIELD()] is None

        from tests.orchestrator_demote_reserved_job_helpers import (
            STARTED_AT as HELD_STARTED_AT,
        )

        record = _versioned_master_reservation_record(
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="gfs",
        )
        record.update(
            {
                **released,
                "expected_submission_attempt": released["submission_attempt"],
                "expected_submission_attempt_started_at": released[
                    "submission_attempt_started_at"
                ],
                "status": "reserved",
                "submission_attempt": int(released["submission_attempt"]) + 1,
                "submission_attempt_started_at": HELD_STARTED_AT + timedelta(hours=3),
                "submit_outcome": None,
                "reconciliation_source": None,
                "reconciliation_decision": None,
                "matched_slurm_job_id": None,
                "slurm_job_id": None,
            }
        )
        reclaimed = held.reclaim_pipeline_job_reservation(record)
        assert reclaimed is not None
        after = held.get_accepted_submit_pipeline_job(held_job_id)
        assert after[_SLURM_BINDING_SOURCE_FIELD()] is None
        assert after[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
        assert after["submitted_at"] is None
        assert after["submission_attempt"] == 2
        # Reopened journal agrees.
        reopened = FileOrchestrationJournalRepository(held.root)
        assert reopened.get_pipeline_job(held_job_id)[_SLURM_BINDING_SOURCE_FIELD()] is None


def test_old_row_missing_binding_fields_remains_readable_and_fail_closed() -> None:
    """A v1 current-contract row predating the additive fields stays readable;
    missing provenance never proves recycle (the settled-incarnation helper
    fails closed on it)."""

    from services.orchestrator.file_orchestration_journal import _settled_incarnation_matches_candidate

    row = {
        **_versioned_master_reservation_record(member_count=1),
        "status": "succeeded",
        "submit_outcome": "accepted",
        "reconciliation_source": "slurm_exact_comment",
        "reconciliation_decision": "matched_bound",
        "slurm_job_id": "72001",
        "matched_slurm_job_id": "72001",
        "submitted_at": "2026-07-12T01:00:00.123456Z",
    }
    normalized = normalize_accepted_submit_evidence(row)
    assert normalized[_SLURM_BINDING_SOURCE_FIELD()] is None
    assert normalized[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
    # Fail closed: no canonical provenance => same-id blocks (True), even though
    # the legacy submitted_at differs from the candidate.
    assert _settled_incarnation_matches_candidate(row, _ANCHOR + timedelta(hours=2)) is True


# ---------------------------------------------------------------------------
# Scheduler attempt evidence projection (required matrix item 10)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_scheduler_attempt_evidence_exposes_binding_provenance_additively(
    tmp_path: Any,
) -> None:
    """``_restart_reconcile_attempt_evidence`` exposes the additive binding
    provenance fields without changing any existing key/value."""

    from services.orchestrator.scheduler_runtime import _restart_reconcile_attempt_evidence

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        evidence = _restart_reconcile_attempt_evidence(repository, job_id)
        assert evidence["slurm_binding_source"] == "slurm_name_window_unique"
        assert evidence["slurm_accounting_submitted_at"] == "2026-07-12T01:00:00Z"
        # Pre-existing keys/values unchanged.
        assert evidence["reconciliation_source"] == "slurm_name_window_unique"
        assert evidence["reconciliation_decision"] == "matched_bound"
        assert evidence["matched_slurm_job_id"] == "72001"
        assert evidence["submit_outcome"] == "accepted"
        assert evidence["submission_attempt"] == 1


def test_ordinary_upsert_cannot_mutate_or_drop_binding_provenance(tmp_path: Any) -> None:
    """Binding provenance is closed master state: an ordinary upsert that tries
    to change or drop it on an existing bound row must be refused."""

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        public = repository.get_pipeline_job(job_id)
        assert public[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"

        # Attempt to mutate the binding source through the ordinary upsert.
        forged = {**public, _SLURM_BINDING_SOURCE_FIELD(): "slurm_exact_comment"}
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository.upsert_pipeline_job(forged)
        assert error.value.reason == "file_journal_evidence_invariant_invalid"
        assert error.value.field == _SLURM_BINDING_SOURCE_FIELD()
        assert repository.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"

        # Omitting the field is a silent keep (the merge copies only explicitly
        # carried keys), never a drop: the persisted provenance survives an
        # ordinary partial upsert that does not mention it.
        dropped = {k: v for k, v in public.items() if k != _SLURM_BINDING_SOURCE_FIELD()}
        kept = repository.upsert_pipeline_job(dropped)
        assert kept is not None
        assert repository.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"

        # Forging the canonical accounting Submit on the current row is refused
        # the same way.
        forged_submit = {**public, _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "2026-07-12T02:00:00Z"}
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository.upsert_pipeline_job(forged_submit)
        assert error.value.reason == "file_journal_evidence_invariant_invalid"
        assert error.value.field == _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        assert repository.get_pipeline_job(job_id)[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"
