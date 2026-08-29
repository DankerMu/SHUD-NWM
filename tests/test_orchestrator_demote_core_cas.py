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

from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    OPERATOR_VERIFIED_ABSENCE_DECISION,
    AcceptedSubmitTransition,
)
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
from tests.orchestrator_demote_reserved_job_helpers import (
    JOB_ID,
    STARTED_AT,
    _absence_row,
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


def test_operator_verified_absence_transition_rejects_identity_blocked_streak(
    tmp_path: Path,
) -> None:
    """The operator token never carries a non-zero blocked streak (task 1.1).

    ``operator_verified_absence`` sits outside the identity-mismatch streak
    set, so building its typed transition with ``identity_blocked_streak=1``
    raises the typed invariant error before any journal write; the
    constructor is a pure value seam, so the refusal precedes any write.
    """
    repository = _held_cohort_repository(tmp_path)
    before = _journal_bytes(repository.root)

    with pytest.raises(
        ValueError, match="identity blocked streak belongs to identity-mismatch transitions"
    ):
        AcceptedSubmitTransition.accounting(
            OPERATOR_VERIFIED_ABSENCE_DECISION,
            submit_outcome="submit_result_ambiguous",
            status="reservation_lost",
            identity_blocked_streak=1,
        )
    # The transition is never even built, so no journal byte can be touched.
    assert _journal_bytes(repository.root) == before


def test_operator_verified_absence_transition_accepts_zero_streak() -> None:
    """The same operator transition with the default zero streak builds fine."""
    transition = AcceptedSubmitTransition.accounting(
        OPERATOR_VERIFIED_ABSENCE_DECISION,
        submit_outcome="submit_result_ambiguous",
        status="reservation_lost",
    )
    assert transition.identity_blocked_streak == 0


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


# ---------------------------------------------------------------------------
# Reclaim CAS persisted-shape matrix: a still-demoted ``operator_verified_absence``
# row whose durable shape carries one forbidden axis (bound ``slurm_job_id``,
# non-null ``reconciliation_reason_class``, non-null ``matched_slurm_job_id``)
# must be refused with ``None``, byte-identical authority journal, and an
# unchanged current master.  The matrix is built on a FRESH real held row
# produced by the #1116 producer, demoted through the typed operator API, then
# re-read under a test-local lookup that substitutes exactly one persisted
# axis (the "controlled fixture construction" seam): the authority journal is
# the real clean demotion, so the reclaim refusal can only come from the
# persisted predicate, and no post-success ``reserved`` row is ever the
# negative subject (verified finding cand-te-02).
# ---------------------------------------------------------------------------
def _reclaim_request(demoted: dict[str, Any]) -> dict[str, Any]:
    return {
        **demoted,
        "expected_submission_attempt": demoted["submission_attempt"],
        "expected_submission_attempt_started_at": demoted["submission_attempt_started_at"],
        "status": "reserved",
        "submission_attempt": int(demoted["submission_attempt"]) + 1,
        "submission_attempt_started_at": STARTED_AT + timedelta(hours=5),
        "submit_outcome": None,
        "reconciliation_source": None,
        "reconciliation_decision": None,
        "matched_slurm_job_id": None,
    }


@pytest.mark.parametrize(
    ("axis", "axis_mutator"),
    [
        pytest.param("slurm_job_id", lambda row: row.update({"slurm_job_id": "17667"}), id="bound"),
        pytest.param(
            "reconciliation_reason_class",
            lambda row: row.update({"reconciliation_reason_class": "process_unavailable"}),
            id="reason_class",
        ),
        pytest.param(
            "matched_slurm_job_id",
            lambda row: row.update({"matched_slurm_job_id": "17667"}),
            id="matched",
        ),
    ],
)
def test_still_demoted_row_refuses_forbidden_persisted_axis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    axis: str,
    axis_mutator: Any,
) -> None:
    """task 2.1: a still-demoted row carrying a forbidden persisted axis refuses.

    Only the tested axis is substituted on the read surface; every other
    field stays the real clean demotion, so the refusal is caused by that
    axis alone (the project's lowest test seam reaching the reclaim CAS).
    """
    repository = _held_cohort_repository(tmp_path)
    demoted = _absence_row(repository, decision=OPERATOR_VERIFIED_ABSENCE_DECISION)
    assert demoted["status"] == "reservation_lost"
    assert demoted["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
    assert demoted[axis] is None

    mutated = dict(demoted)
    axis_mutator(mutated)
    assert mutated[axis] is not None  # the preloaded mismatch really exists
    original_lookup = repository._accepted_submit_job_for_id_unlocked

    def _substituted_lookup(
        pipeline_job_id: str, *, source_id: Any, cycle_time: Any
    ) -> dict[str, Any] | None:
        del source_id, cycle_time
        return dict(mutated) if str(pipeline_job_id) == JOB_ID else original_lookup(pipeline_job_id)

    repository._accepted_submit_job_for_id_unlocked = _substituted_lookup
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=5))
    before = _journal_bytes(repository.root)

    reclaimed = repository.reclaim_pipeline_job_reservation(_reclaim_request(demoted))

    assert reclaimed is None
    assert _journal_bytes(repository.root) == before
    repository._accepted_submit_job_for_id_unlocked = original_lookup
    current = _held_row(repository)
    assert current["status"] == "reservation_lost"
    assert current["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
    assert current == demoted  # current master unchanged


# ---------------------------------------------------------------------------
# 1.4 writer-authority closed world (Round 3): the operator decision is never
# written by a generic accepted-submit writer
# ---------------------------------------------------------------------------
def _held_key(repository: Any) -> str:
    return str(_held_row(repository)["idempotency_key"])


def test_operator_decision_rejected_by_submit_attempt_commit_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit writer rejects an accepted transition carrying the operator token.

    The accepted forgery carries ``operator_verified_absence``; the
    typed-authority error fires before any mutation, leaving the journal
    byte-identical and emitting zero events.
    """
    repository = _held_cohort_repository(tmp_path)
    row = _held_row(repository)
    key = _held_key(repository)
    forged = AcceptedSubmitTransition.accounting(
        OPERATOR_VERIFIED_ABSENCE_DECISION,
        submit_outcome="accepted",
        status="submitted",
    )
    before = _journal_bytes(repository.root)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))

    with pytest.raises(FileOrchestrationJournalError) as excinfo:
        repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=JOB_ID,
            expected_submission_attempt=int(row["submission_attempt"]),
            slurm_job_id="9999",
            transition=forged,
        )

    assert excinfo.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert _journal_bytes(repository.root) == before
    assert _durable_event_payloads(repository.root) == []
    assert _held_row(repository) == row


def test_operator_decision_rejected_by_defer_cohort_projection_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defer writer rejects a raw operator decision before any mutation."""
    repository = _held_cohort_repository(tmp_path)
    row = _held_row(repository)
    before = _journal_bytes(repository.root)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))

    with pytest.raises(FileOrchestrationJournalError) as excinfo:
        repository.defer_forecast_cohort_projection(
            JOB_ID,
            reconciliation_decision=OPERATOR_VERIFIED_ABSENCE_DECISION,
            reconciliation_reason_class=None,
            error_code="SLURM_MASTER_IDENTITY_MISMATCH",
            error_message="forged operator decision",
        )

    assert excinfo.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert _journal_bytes(repository.root) == before
    assert _durable_event_payloads(repository.root) == []
    assert _held_row(repository) == row


def test_operator_decision_rejected_by_cohort_task_projection_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cohort task projection writer cannot synthesize the operator decision.

    ``project_forecast_cohort_tasks`` takes a raw ``reconciliation_decision``;
    its own evidence gate refuses anything but ``matched_bound``, so the
    operator token can never reach a durable row through this door.
    """
    repository = _held_cohort_repository(tmp_path)
    row = _held_row(repository)
    key = _held_key(repository)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))
    committed = repository.commit_pipeline_job_submit_attempt(
        key,
        pipeline_job_id=JOB_ID,
        expected_submission_attempt=int(row["submission_attempt"]),
        slurm_job_id="9999",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    assert committed.committed
    before = _journal_bytes(repository.root)

    with pytest.raises(FileOrchestrationJournalError) as excinfo:
        repository.project_forecast_cohort_tasks(
            JOB_ID,
            master_slurm_job_id="9999",
            projections=[],
            complete=True,
            master_status="failed",
            master_error_code=None,
            reconciliation_decision=OPERATOR_VERIFIED_ABSENCE_DECISION,
        )

    assert excinfo.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert _journal_bytes(repository.root) == before
    assert _durable_event_payloads(repository.root) == []
    current = _held_row(repository)
    assert current["status"] == "submitted"
    assert current["reconciliation_decision"] is None


def test_operator_decision_ignored_by_unbound_task_projection_defer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw operator token on a mismatched-ID master is refused at entry.

    The mismatch defer branch must never silently persist ``identity_mismatch_blocked``
    when the caller supplied ``operator_verified_absence``; the typed-authority
    refusal fires before any lock/mutation/event, as on the bound branch.
    """
    repository = _held_cohort_repository(tmp_path)
    row = _held_row(repository)
    before = _journal_bytes(repository.root)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))

    with pytest.raises(FileOrchestrationJournalError) as excinfo:
        repository.project_forecast_cohort_tasks(
            JOB_ID,
            master_slurm_job_id="9999",
            projections=[],
            complete=True,
            master_status="failed",
            master_error_code=None,
            reconciliation_decision=OPERATOR_VERIFIED_ABSENCE_DECISION,
        )

    assert excinfo.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert _journal_bytes(repository.root) == before
    assert _durable_event_payloads(repository.root) == []
    assert _held_row(repository) == row


def test_legitimate_writers_still_apply_after_authority_closed_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legal accepted-submit decisions keep their writers unchanged."""
    repository = _held_cohort_repository(tmp_path)
    row = _held_row(repository)
    key = _held_key(repository)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))

    committed = repository.commit_pipeline_job_submit_attempt(
        key,
        pipeline_job_id=JOB_ID,
        expected_submission_attempt=int(row["submission_attempt"]),
        slurm_job_id="9999",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    assert committed.committed
    assert committed.row is not None
    assert committed.row["slurm_job_id"] == "9999"
    assert committed.row["status"] == "submitted"
    assert committed.row["reconciliation_decision"] is None


def test_legitimate_cohort_projection_decisions_still_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legal bound/mismatch projection decisions keep their writers unchanged.

    The operator-token refusal is narrowly scoped: ``matched_bound`` and the
    fixed ``identity_mismatch_blocked`` defer still apply via their writer.
    """
    repository = _held_cohort_repository(tmp_path)
    row = _held_row(repository)
    key = _held_key(repository)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))
    assert repository.commit_pipeline_job_submit_attempt(
        key,
        pipeline_job_id=JOB_ID,
        expected_submission_attempt=int(row["submission_attempt"]),
        slurm_job_id="9999",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    ).committed

    # Legal mismatch defer on the same bound master (wrong terminal id) writes
    # the fixed identity_mismatch_blocked decision.
    mismatch = repository.project_forecast_cohort_tasks(
        JOB_ID,
        master_slurm_job_id="8888",
        projections=[],
        complete=True,
        master_status="failed",
        master_error_code=None,
        reconciliation_decision="identity_mismatch_blocked",
    )
    assert mismatch == {"total": 1, "pipeline_status": 1, "pipeline_event": 0}
    current = _held_row(repository)
    assert current["reconciliation_decision"] == "identity_mismatch_blocked"
    assert current["status"] == "reconcile_unverified"

    # Legal matched_bound projection still applies on a bound master.
    members = journal_module._bounded_cohort_members(current.get("cohort_members"))
    matched = repository.project_forecast_cohort_tasks(
        JOB_ID,
        master_slurm_job_id="9999",
        projections=[
            {
                **member,
                "array_task_outcome": "failed",
                "native_shud_resubmitted": False,
                "task_slurm_job_id": f"9999_{member['array_task_id']}",
            }
            for member in members
        ],
        complete=True,
        master_status="failed",
        master_error_code="SLURM_ARRAY_TASK_FAILED",
        reconciliation_decision="matched_bound",
    )
    assert matched["pipeline_status"] >= len(members)
    projected = _held_row(repository)
    assert projected["status"] == "failed"
    assert projected["reconciliation_decision"] == "matched_bound"


# ---------------------------------------------------------------------------
# 1.4 upsert writer-authority (Phase 6.2): the operator decision is never
# persisted by ordinary pipeline-job upsert, including a marker-free legacy
# master upgraded to the current contract in one merge
# ---------------------------------------------------------------------------
def _legacy_marker_free_master_repository(tmp_path: Path) -> Any:
    """One marker-free legacy master as pre-contract reserves persist it.

    The current-contract marker is absent (historical compatibility data) and
    the row is structurally a forecast master — the legacy shape the ordinary
    upsert may legally upgrade to the current contract in one merge.
    """

    from tests.gateway_reconcile_helpers import _file_cohort_repository

    return _file_cohort_repository(tmp_path, member_count=2, versioned=False)


def test_upsert_operator_decision_rejected_even_on_marker_free_contract_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary upsert refuses a forged operator decision on an upgraded row.

    A marker-free legacy master upgraded in ONE ordinary merge is the path no
    other guard covers: the existing upsert shape gates fire only on an
    ALREADY-current master.  Only the dedicated typed demotion may persist
    ``operator_verified_absence``, so this merge is refused before any row
    construction, lock, durable mutation, or event — leaving the journal
    byte-identical and the durable/public legacy row untouched.
    """
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository = _legacy_marker_free_master_repository(tmp_path)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before = _journal_bytes(repository.root)
    legacy_row = repository.get_pipeline_job(job_id)
    assert legacy_row["reconciliation_decision"] is None
    assert legacy_row.get("accepted_submit_contract_version") is None
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))

    forged = dict(legacy_row)
    forged["accepted_submit_contract_version"] = ACCEPTED_SUBMIT_CONTRACT_VERSION
    forged["reconciliation_decision"] = OPERATOR_VERIFIED_ABSENCE_DECISION
    forged["reconciliation_source"] = "slurm_exact_comment"
    forged["reconciliation_reason_class"] = None
    forged["submit_outcome"] = "submit_result_ambiguous"

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job(forged)

    assert error.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert error.value.field == "reconciliation_decision"
    assert _journal_bytes(repository.root) == before
    assert _durable_event_payloads(repository.root) == []
    # The durable legacy row and its public view are unchanged.
    assert repository.get_pipeline_job(job_id)["reconciliation_decision"] is None
    assert repository.get_pipeline_job(job_id).get("accepted_submit_contract_version") is None
    assert _held_row(repository) == legacy_row


def test_upsert_operator_decision_rejected_on_current_contract_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-current master carrying the operator token is also refused."""
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository = _legacy_marker_free_master_repository(tmp_path)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    legacy_row = repository.get_pipeline_job(job_id)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))

    forged = dict(legacy_row)
    forged["accepted_submit_contract_version"] = ACCEPTED_SUBMIT_CONTRACT_VERSION
    forged["reconciliation_decision"] = OPERATOR_VERIFIED_ABSENCE_DECISION
    forged["reconciliation_source"] = "slurm_exact_comment"
    forged["reconciliation_reason_class"] = None
    forged["submit_outcome"] = "submit_result_ambiguous"

    # First upsert persists the CURRENT contract marker with the (legal)
    # automatic decision, so the row is now current-contract.
    automatic = dict(forged)
    automatic["reconciliation_decision"] = None
    automatic["reconciliation_reason_class"] = None
    automatic["reconciliation_source"] = None
    written = repository.upsert_pipeline_job(automatic)
    assert written.get("accepted_submit_contract_version") == ACCEPTED_SUBMIT_CONTRACT_VERSION
    current_before = _journal_bytes(repository.root)

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job(forged)

    assert error.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert error.value.field == "reconciliation_decision"
    assert _journal_bytes(repository.root) == current_before
    assert _durable_event_payloads(repository.root) == []
    current = repository.get_pipeline_job(job_id)
    assert current["reconciliation_decision"] is None
    assert current.get("accepted_submit_contract_version") == ACCEPTED_SUBMIT_CONTRACT_VERSION


def test_upsert_non_token_legacy_upgrade_still_applies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marker-free legacy upgrade WITHOUT the operator token still works.

    The ordinary upsert must keep accepting and persisting the non-token
    current-contract upgrade exactly as before the closed-world guard — the
    Phase 6.2 control proving the guard is narrowly scoped to the operator
    token.
    """
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository = _legacy_marker_free_master_repository(tmp_path)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    legacy_row = repository.get_pipeline_job(job_id)
    assert legacy_row.get("accepted_submit_contract_version") is None
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))

    upgraded = dict(legacy_row)
    upgraded["accepted_submit_contract_version"] = ACCEPTED_SUBMIT_CONTRACT_VERSION
    upgraded["submit_outcome"] = "submit_result_ambiguous"
    upgraded["reconciliation_decision"] = None
    upgraded["reconciliation_source"] = None
    upgraded["reconciliation_reason_class"] = None

    written = repository.upsert_pipeline_job(upgraded)

    assert written["status"] == legacy_row["status"]
    assert written["reconciliation_decision"] is None
    assert written.get("accepted_submit_contract_version") == ACCEPTED_SUBMIT_CONTRACT_VERSION
    current = repository.get_pipeline_job(job_id)
    assert current.get("accepted_submit_contract_version") == ACCEPTED_SUBMIT_CONTRACT_VERSION
    assert current["reconciliation_decision"] is None
    # The upgrade durably landed in the journal.
    durable = _durable_pipeline_job_payloads(repository.root, job_id)[-1]
    assert durable.get("accepted_submit_contract_version") == ACCEPTED_SUBMIT_CONTRACT_VERSION
    assert durable["reconciliation_decision"] is None


# ---------------------------------------------------------------------------
# #1805: legacy compatibility writers must not mint operator authority
# ---------------------------------------------------------------------------
def test_legacy_transition_pipeline_job_submit_evidence_rejects_forged_operator_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1805 1.1: the legacy accepted-submit transition cannot mint the token.

    A marker-free legacy row plus an ``operator_verified_absence`` transition is
    the compatibility surface the versioned gate does not cover: the legacy path
    has no decision whitelist, so it would apply the forged authority.  The
    typed-authority refusal must fire before the cycle lock, row construction,
    durable mutation, or event -- leaving the journal byte-identical and the
    legacy row untouched.
    """
    repository = _legacy_marker_free_master_repository(tmp_path)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before = _journal_bytes(repository.root)
    legacy_row = repository.get_pipeline_job(job_id)
    assert legacy_row.get("accepted_submit_contract_version") is None
    assert legacy_row["reconciliation_decision"] is None
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))

    forged = AcceptedSubmitTransition.accounting(
        OPERATOR_VERIFIED_ABSENCE_DECISION,
        submit_outcome="submit_result_ambiguous",
        status="reservation_lost",
    )
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.transition_pipeline_job_submit_evidence(
            job_id,
            forged,
            accepted_submit_contract_version=None,
        )

    assert error.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert error.value.field == "reconciliation_decision"
    assert _journal_bytes(repository.root) == before
    assert _durable_event_payloads(repository.root) == []
    assert repository.get_pipeline_job(job_id) == legacy_row


def test_legacy_reconciliation_recorder_rejects_forged_operator_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1805 1.2: the legacy reconciliation recorder cannot mint the token.

    ``record_pipeline_job_reconciliation`` is the second compatibility writer:
    it accepts raw ``reconciliation_decision`` strings for marker-free legacy
    rows.  The forged operator decision must raise the typed-authority error
    before the cycle lock, row construction, durable mutation, or event, with
    a byte-identical journal and zero events.
    """
    repository = _legacy_marker_free_master_repository(tmp_path)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before = _journal_bytes(repository.root)
    legacy_row = repository.get_pipeline_job(job_id)
    assert legacy_row.get("accepted_submit_contract_version") is None
    assert legacy_row["reconciliation_decision"] is None
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.record_pipeline_job_reconciliation(
            job_id,
            submit_outcome="submit_result_ambiguous",
            reconciliation_decision=OPERATOR_VERIFIED_ABSENCE_DECISION,
            status="reservation_lost",
        )

    assert error.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert error.value.field == "reconciliation_decision"
    assert _journal_bytes(repository.root) == before
    assert _durable_event_payloads(repository.root) == []
    assert repository.get_pipeline_job(job_id) == legacy_row


def test_legacy_compatibility_writers_still_apply_legal_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1805 controls: legal legacy decisions keep both compatibility APIs.

    The new guard is scoped to exactly the operator token: an ordinary
    ``accounting_unavailable`` transition through the legacy submit-evidence
    API and an ``absence_deferred`` decision through the legacy reconciliation
    recorder must still apply durably.  This is the positive counterpart of the
    two negative tests above.
    """
    transition_repository = _legacy_marker_free_master_repository(tmp_path / "transition")
    record_repository = _legacy_marker_free_master_repository(tmp_path / "record")
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    monkeypatch.setattr(journal_module, "_utcnow", lambda: STARTED_AT + timedelta(hours=3))

    applied = transition_repository.transition_pipeline_job_submit_evidence(
        job_id,
        AcceptedSubmitTransition.accounting(
            "accounting_unavailable",
            submit_outcome="submit_result_ambiguous",
            reconciliation_reason_class="process_unavailable",
            status="reserved",
        ),
        accepted_submit_contract_version=None,
    )
    assert applied.outcome == "applied"
    transitioned = transition_repository.get_pipeline_job(job_id)
    assert transitioned["reconciliation_decision"] == "accounting_unavailable"
    assert transitioned["reconciliation_reason_class"] == "process_unavailable"
    transition_durable = _durable_pipeline_job_payloads(transition_repository.root, job_id)[-1]
    assert transition_durable["reconciliation_decision"] == "accounting_unavailable"

    recorded = record_repository.record_pipeline_job_reconciliation(
        job_id,
        submit_outcome="submit_result_ambiguous",
        reconciliation_decision="absence_deferred",
        status="reserved",
    )
    assert recorded is not None
    assert recorded["reconciliation_decision"] == "absence_deferred"
    recorded_row = record_repository.get_pipeline_job(job_id)
    assert recorded_row["reconciliation_decision"] == "absence_deferred"
    record_durable = _durable_pipeline_job_payloads(record_repository.root, job_id)[-1]
    assert record_durable["reconciliation_decision"] == "absence_deferred"
