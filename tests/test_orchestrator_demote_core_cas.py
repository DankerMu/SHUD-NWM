"""#1564 demote-reserved-job: typed CAS refusals, atomicity, and post-commit faults.

Requirement-driven tests for the file-journal-only typed CAS that converts the
held comment-unobservable shape into a reclaimable ``reservation_lost`` /
``operator_verified_absence`` terminal.  These tests drive the real producer
(reserve + reconcile ``query_unavailable``), the typed demotion CAS, and the
atomicity/fault paths — no test hand-builds a post-state that bypasses the
typed transition.
"""

from __future__ import annotations

import json
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    OPERATOR_VERIFIED_ABSENCE_DECISION,
    AcceptedSubmitTransition,
)
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
from tests.orchestrator_demote_reserved_job_helpers import (
    JOB_ID,
    STARTED_AT,
    _axis_mismatch_field,
    _axis_repository,
    _demote_kwargs,
    _durable_event_payloads,
    _durable_hydro_payloads,
    _held_cohort_repository,
    _held_row,
    _journal_bytes,
)
from tests.test_file_orchestration_journal import _durable_pipeline_job_payloads


# ---------------------------------------------------------------------------
# 4.1 zero-write refusals (byte-identical journal after every refusal)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("override", "case"),
    [
        pytest.param({"job_id": "job_cycle_gfs_2026071200_forecast_fixture_other"}, "wrong_job_id", id="wrong_job_id"),
        pytest.param(
            {"expected_submission_attempt": 2},
            "wrong_attempt",
            id="wrong_attempt",
        ),
        pytest.param(
            {"expected_submission_attempt_started_at": STARTED_AT + timedelta(seconds=1)},
            "wrong_anchor",
            id="wrong_anchor",
        ),
    ],
)
def test_demotion_refuses_stale_or_invalid_input_with_zero_journal_change(
    tmp_path: Path,
    override: dict[str, Any],
    case: str,
) -> None:
    del case
    repository = _held_cohort_repository(tmp_path)
    before = _journal_bytes(repository.root)
    row = _held_row(repository)
    if override.get("job_id"):
        kwargs = _demote_kwargs(row, **{k: v for k, v in override.items() if k != "job_id"})
    else:
        kwargs = _demote_kwargs(row, **override)
    target_job_id = override.get("job_id", JOB_ID)

    receipt = repository.demote_operator_verified_reserved_job(target_job_id, **kwargs)

    assert receipt is None
    assert _journal_bytes(repository.root) == before
    assert _held_row(repository) == row


def test_demotion_rejects_wrong_contract_version_with_typed_error(tmp_path: Path) -> None:
    repository = _held_cohort_repository(tmp_path)
    before = _journal_bytes(repository.root)
    row = _held_row(repository)

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.demote_operator_verified_reserved_job(
            JOB_ID,
            **_demote_kwargs(row, accepted_submit_contract_version="nhms.accepted_submit.v2"),
        )
    assert error.value.reason == "file_journal_evidence_enum_invalid"
    assert _journal_bytes(repository.root) == before
    assert _held_row(repository) == row


@pytest.mark.parametrize(
    ("state_mutator", "case"),
    [
        pytest.param("bind", "bound", id="bound"),
        pytest.param("permits", "permitted", id="permitted"),
        pytest.param("released", "released", id="released"),
        pytest.param("demoted", "demoted", id="demoted"),
        pytest.param("reclaimed", "reclaimed", id="reclaimed"),
    ],
)
def test_demotion_refuses_a_row_that_left_the_exact_held_state(
    tmp_path: Path,
    state_mutator: str,
    case: str,
) -> None:
    del case
    repository = _held_cohort_repository(tmp_path)
    if state_mutator == "bind":
        assert repository.commit_pipeline_job_submit_attempt(
            str(_held_row(repository)["idempotency_key"]),
            pipeline_job_id=JOB_ID,
            expected_submission_attempt=1,
            slurm_job_id="17667",
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        ).committed
    elif state_mutator == "permits":
        assert repository.permit_pipeline_job_retry(
            JOB_ID,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_submission_attempt_started_at=_held_row(repository)["submission_attempt_started_at"],
        ) == 1
    elif state_mutator == "released":
        assert repository.release_identity_blocked_reservation(
            JOB_ID,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_submission_attempt_started_at=_held_row(repository)["submission_attempt_started_at"],
            identity_blocked_streak=3,
        ) == 1
    elif state_mutator == "demoted":
        assert repository.demote_operator_verified_reserved_job(
            JOB_ID,
            **_demote_kwargs(_held_row(repository)),
        ) is not None
    elif state_mutator == "reclaimed":
        assert repository.demote_operator_verified_reserved_job(
            JOB_ID,
            **_demote_kwargs(_held_row(repository)),
        ) is not None
        demoted = _held_row(repository)
        assert repository.reclaim_pipeline_job_reservation(
            {
                **demoted,
                "expected_submission_attempt": demoted["submission_attempt"],
                "expected_submission_attempt_started_at": demoted["submission_attempt_started_at"],
                "status": "reserved",
                "submission_attempt": int(demoted["submission_attempt"]) + 1,
                "submit_outcome": None,
                "reconciliation_source": None,
                "reconciliation_decision": None,
                "matched_slurm_job_id": None,
            }
        ) is not None
    before = _journal_bytes(repository.root)
    row = _held_row(repository)

    receipt = repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(row),
    )

    assert receipt is None
    assert _journal_bytes(repository.root) == before
    assert _held_row(repository) == row


def test_demotion_refuses_repeated_invocation_after_success(tmp_path: Path) -> None:
    repository = _held_cohort_repository(tmp_path)
    assert repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(_held_row(repository)),
    ) is not None
    demoted = _held_row(repository)
    assert demoted["status"] == "reservation_lost"
    before = _journal_bytes(repository.root)

    receipt = repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(demoted),
    )

    assert receipt is None
    assert _journal_bytes(repository.root) == before
    assert _held_row(repository) == demoted


def test_demotion_invalid_input_types_raise_typed_error(tmp_path: Path) -> None:
    repository = _held_cohort_repository(tmp_path)
    row = _held_row(repository)
    before = _journal_bytes(repository.root)

    for kwargs in (
        _demote_kwargs(row, expected_submission_attempt="1"),
        _demote_kwargs(row, expected_submission_attempt=0),
        _demote_kwargs(row, expected_submission_attempt_started_at="2026-07-12T00:00:00"),
        _demote_kwargs(row, expected_submission_attempt_started_at=None),
        _demote_kwargs(row, checked_at="2026-07-12T00:00:00"),
        _demote_kwargs(row, checked_at=None),
        _demote_kwargs(row, checked_by=None),
        _demote_kwargs(row, checked_by=""),
        _demote_kwargs(row, checked_by="x" * 257),
        _demote_kwargs(row, verification_note=None),
        _demote_kwargs(row, verification_note="   "),
        _demote_kwargs(row, verification_note="x" * 2049),
        _demote_kwargs(row, accepted_submit_contract_version=None),
    ):
        with pytest.raises(FileOrchestrationJournalError):
            repository.demote_operator_verified_reserved_job(JOB_ID, **kwargs)
    assert _journal_bytes(repository.root) == before
    assert _held_row(repository) == row


def test_demotion_is_file_journal_only_and_absent_from_generic_and_manual_surfaces(
    tmp_path: Path,
) -> None:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_RECONCILIATION_DECISIONS
    from services.orchestrator.retry import MANUAL_RETRY_SOURCE_STATUSES

    assert OPERATOR_VERIFIED_ABSENCE_DECISION in ACCEPTED_RECONCILIATION_DECISIONS
    # Generic versioned transitions reject the token before any write.
    repository = _held_cohort_repository(tmp_path)
    before = _journal_bytes(repository.root)
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.transition_pipeline_job_submit_evidence(
            JOB_ID,
            AcceptedSubmitTransition.accounting(
                OPERATOR_VERIFIED_ABSENCE_DECISION,
                submit_outcome="submit_result_ambiguous",
                status="reservation_lost",
            ),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )
    assert error.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert _journal_bytes(repository.root) == before
    # reserved stays outside the HTTP/manual retry source statuses.
    assert "reserved" not in MANUAL_RETRY_SOURCE_STATUSES


# ---------------------------------------------------------------------------
# 1.3 / 2.1 success: one locked atomic append
# ---------------------------------------------------------------------------
def test_demotion_writes_master_hydro_fanout_and_audit_event_in_one_append(
    tmp_path: Path,
) -> None:
    repository = _held_cohort_repository(tmp_path, member_count=2)
    held = _held_row(repository)
    root = repository.root
    held_payload = _durable_pipeline_job_payloads(root, JOB_ID)[-1]

    receipt = repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(held),
    )

    # One pipeline_job + one audit event (hydro fan-out skips rows absent here).
    assert receipt is not None
    assert receipt.written_record_count == 2
    demoted = _held_row(repository)
    assert demoted["status"] == "reservation_lost"
    assert demoted["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
    assert demoted["reconciliation_source"] == "slurm_exact_comment"
    assert demoted["submit_outcome"] == "submit_result_ambiguous"
    assert demoted["matched_slurm_job_id"] is None
    assert demoted["reconciliation_reason_class"] is None
    # Immutable identity is preserved exactly.
    assert demoted["submission_attempt"] == held["submission_attempt"]
    assert demoted["submission_attempt_started_at"] == held["submission_attempt_started_at"]
    assert demoted["job_id"] == held["job_id"]
    assert demoted["idempotency_key"] == held["idempotency_key"]
    assert demoted["cohort_digest"] == held["cohort_digest"]
    assert demoted["cohort_members"] == held["cohort_members"]
    # The durable payload carries the same facts.
    durable = _durable_pipeline_job_payloads(root, JOB_ID)[-1]
    assert durable["status"] == "reservation_lost"
    assert durable["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
    assert durable["reconciliation_reason_class"] is None
    assert durable["submission_attempt"] == held_payload["submission_attempt"]
    assert durable["submission_attempt_started_at"] == held_payload["submission_attempt_started_at"]
    # The audit event carries bounded operator evidence and the prior blocker.
    events = _durable_event_payloads(root)
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "operator_verified_absence"
    assert event["status_from"] == "reserved"
    assert event["status_to"] == "reservation_lost"
    assert event["details"]["checked_by"] == "operator-alice"
    assert event["details"]["checked_at"] == (STARTED_AT + timedelta(hours=2)).astimezone(UTC).isoformat().replace(
        "+00:00", "Z"
    )
    assert "no matching nhms_forecast job" in event["details"]["verification_note"]
    assert event["details"]["expected_submission_attempt"] == held["submission_attempt"]
    assert event["details"]["expected_submission_attempt_started_at"] == held_payload[
        "submission_attempt_started_at"
    ]
    assert event["details"]["prior_reconciliation_decision"] == "accounting_unavailable"
    assert event["details"]["prior_reconciliation_reason_class"] == "comment_accounting_unproven"
    # The prior reason class survives ONLY in the audit details, never in the row.
    assert "comment_accounting_unproven" not in json.dumps(durable)


def test_demotion_fans_out_active_hydro_members_for_the_same_attempt(tmp_path: Path) -> None:
    repository = _held_cohort_repository(tmp_path, member_count=2, active_hydro=True)
    held = _held_row(repository)
    root = repository.root
    before_hydro = {
        p["run_id"]: p
        for p in _durable_hydro_payloads(root)
        if p.get("run_id", "").startswith("fcst_gfs_2026071200_model_")
    }
    assert len(before_hydro) == 2
    assert all(p.get("status") == "running" for p in before_hydro.values())

    receipt = repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(held),
    )

    assert receipt is not None
    assert receipt.written_record_count == 4  # two hydro + master + event
    for run_id in before_hydro:
        after = repository._hydro_run_for(run_id)
        assert after is not None
        assert after["status"] == "failed"
        assert after["error_code"] == "SLURM_RESERVATION_LOST"
        assert after["submission_attempt"] == 1
    # The LATEST hydro record per run is the demotion projection.
    for run_id in before_hydro:
        durable = [
            p
            for p in _durable_hydro_payloads(root)
            if p.get("run_id") == run_id and p.get("status") == "failed"
        ]
        assert durable, f"no failed hydro record for {run_id}"
        assert durable[-1]["error_code"] == "SLURM_RESERVATION_LOST"
    # The materialized latest view reflects the failed hydro rows.
    latest = json.loads((root / "latest/gfs/2026071200/model_0.json").read_text(encoding="utf-8"))
    assert latest["hydro_run"]["status"] == "failed"
    assert latest["hydro_run"]["error_code"] == "SLURM_RESERVATION_LOST"


# ---------------------------------------------------------------------------
# 4.2 concurrency and atomicity
# ---------------------------------------------------------------------------
def test_concurrent_successor_wins_and_stale_operator_request_writes_nothing(
    tmp_path: Path,
) -> None:
    repository = _held_cohort_repository(tmp_path)
    held = _held_row(repository)
    before = _journal_bytes(repository.root)
    # A concurrent bind wins before the operator obtains the cycle lock.
    assert repository.commit_pipeline_job_submit_attempt(
        str(held["idempotency_key"]),
        pipeline_job_id=JOB_ID,
        expected_submission_attempt=1,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    ).committed
    bound = _held_row(repository)

    receipt = repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(held),
    )

    assert receipt is None
    assert _journal_bytes(repository.root) != before  # the bind DID write, this request did not
    assert _held_row(repository) == bound


def test_append_fault_before_commit_leaves_no_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator.chain_types import OrchestratorError

    repository = _held_cohort_repository(tmp_path)
    held = _held_row(repository)
    before = _journal_bytes(repository.root)
    before_payloads = _durable_pipeline_job_payloads(repository.root, JOB_ID)
    before_hydro = _durable_hydro_payloads(repository.root)
    before_events = _durable_event_payloads(repository.root)

    def fail_batch_append(*_args: Any, **_kwargs: Any) -> None:
        raise OrchestratorError("FILE_JOURNAL_WRITE_FAILED", "injected batch append failure")

    monkeypatch.setattr(repository, "_append_journal_records_unlocked", fail_batch_append)

    with pytest.raises(OrchestratorError):
        repository.demote_operator_verified_reserved_job(
            JOB_ID,
            **_demote_kwargs(held),
        )

    assert _journal_bytes(repository.root) == before
    assert _durable_pipeline_job_payloads(repository.root, JOB_ID) == before_payloads
    assert _durable_hydro_payloads(repository.root) == before_hydro
    assert _durable_event_payloads(repository.root) == before_events
    assert _held_row(repository) == held


def test_event_validation_fault_blocks_the_whole_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _held_cohort_repository(tmp_path)
    held = _held_row(repository)
    before = _journal_bytes(repository.root)
    before_payloads = _durable_pipeline_job_payloads(repository.root, JOB_ID)
    before_events = _durable_event_payloads(repository.root)

    def fail_validate(*_args: Any, **_kwargs: Any) -> None:
        raise FileOrchestrationJournalError(
            "file_journal_invalid_identity",
            field="event_id",
            evidence={"expected": "integer", "actual": "poisoned"},
        )

    monkeypatch.setattr(repository, "_validate_outgoing_record", fail_validate)

    with pytest.raises(FileOrchestrationJournalError):
        repository.demote_operator_verified_reserved_job(
            JOB_ID,
            **_demote_kwargs(held),
        )

    assert _journal_bytes(repository.root) == before
    assert _durable_pipeline_job_payloads(repository.root, JOB_ID) == before_payloads
    assert _durable_event_payloads(repository.root) == before_events
    assert _held_row(repository) == held


# ---------------------------------------------------------------------------
# 4.4 isolated persisted-axis CAS refusals (byte-identical journal)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mutator", "expected_value"),
    [
        pytest.param("running", "running", id="status_running"),
        pytest.param("submitted", "submitted", id="status_submitted"),
        pytest.param("pending", "pending", id="status_pending"),
        pytest.param("submission_failed", "submission_failed", id="status_submission_failed"),
        # The reject transition writes the ``rejected`` outcome together with
        # ``submission_failed``; this case pins the outcome axis of that same
        # legitimate transition.
        pytest.param("rejected", "rejected", id="wrong_submit_outcome"),
        pytest.param("matched", "17667", id="matched_slurm_job_id"),
        pytest.param("pre_decision", None, id="wrong_reconciliation_source"),
        pytest.param("identity_mismatch", "identity_mismatch_blocked", id="wrong_reconciliation_decision"),
        pytest.param("process_unavailable", "process_unavailable", id="wrong_reconciliation_reason_class"),
        pytest.param("legacy", None, id="non_master_legacy"),
    ],
)
def test_demotion_refuses_every_isolated_persisted_axis_mismatch(
    tmp_path: Path,
    mutator: str,
    expected_value: object,
) -> None:
    repository = _axis_repository(tmp_path, mutator)
    before = _journal_bytes(repository.root)
    row = repository.get_accepted_submit_pipeline_job(JOB_ID)
    assert row is not None
    field, actual = _axis_mismatch_field(repository, mutator)
    # Demonstrate the persisted mismatch the CAS must refuse.
    assert actual == expected_value
    if mutator == "submission_failed":
        assert field == "status" and actual == "submission_failed"
    if mutator == "pre_decision":
        assert row["submit_outcome"] == "submit_result_ambiguous"
    if mutator == "legacy":
        assert row.get("accepted_submit_contract_version") is None

    receipt = repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(row),
    )

    assert receipt is None
    assert _journal_bytes(repository.root) == before
    assert repository.get_accepted_submit_pipeline_job(JOB_ID) == row
