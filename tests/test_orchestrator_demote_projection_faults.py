"""#1564 demote-reserved-job: post-commit projection faults stay committed.

After the authority batch commits, a direct/latest projection fault must not
turn a durable success into a reported failure: the receipt stays committed
and carries bounded non-secret warnings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import OPERATOR_VERIFIED_ABSENCE_DECISION
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from tests.orchestrator_demote_reserved_job_helpers import (
    JOB_ID,
    _demote_kwargs,
    _durable_event_payloads,
    _durable_hydro_payloads,
    _held_cohort_repository,
    _held_row,
    _journal_bytes,
)


def test_demote_direct_projection_fault_returns_committed_receipt_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator.chain_types import OrchestratorError

    repository = _held_cohort_repository(tmp_path, member_count=2, active_hydro=True)
    root = repository.root
    held = _held_row(repository)
    before_hydro = {
        p["run_id"]: p
        for p in _durable_hydro_payloads(root)
        if p.get("run_id", "").startswith("fcst_gfs_2026071200_model_")
    }
    assert len(before_hydro) == 2

    latest_calls: list[str] = []
    original_materialize = repository._materialize_latest_unlocked

    def tracking_materialize(*, source_id: Any, cycle_time: Any, model_id: str, **kwargs: Any) -> None:
        latest_calls.append(model_id)
        original_materialize(source_id=source_id, cycle_time=cycle_time, model_id=model_id, **kwargs)

    def fail_direct(*_args: Any, **_kwargs: Any) -> None:
        raise OrchestratorError("FILE_JOURNAL_WRITE_FAILED", "injected direct projection failure")

    monkeypatch.setattr(repository, "_write_pipeline_job_direct_unlocked", fail_direct)
    monkeypatch.setattr(repository, "_materialize_latest_unlocked", tracking_materialize)

    # The real reserved-unbound reader sees the target BEFORE the demotion.
    assert [job.job_id for job in repository.query_reserved_unbound_jobs()] == [JOB_ID]

    receipt = repository.demote_operator_verified_reserved_job(JOB_ID, **_demote_kwargs(held))

    assert receipt is not None
    assert receipt.status_to == "reservation_lost"
    assert len(receipt.warnings) == 1
    warning = receipt.warnings[0]
    assert warning.projection == "pipeline_job_direct"
    assert warning.model_id is None
    # Even the exact trusted typed exception maps to the fixed non-secret
    # token: its constructor accepts an arbitrary code string that could be a
    # compact secret, so no error_type/reason is ever echoed.
    assert warning.error_type == "projection_fault"
    assert warning.reason == "projection_fault"
    # Every model's latest projection was still attempted after the direct fault.
    assert sorted(latest_calls) == ["model_0", "model_1"]
    # Authority batch is durable despite the projection fault.
    demoted = _held_row(repository)
    assert demoted["status"] == "reservation_lost"
    assert demoted["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
    assert len(_durable_event_payloads(root)) == 1
    for run_id in before_hydro:
        durable = [
            p
            for p in _durable_hydro_payloads(root)
            if p.get("run_id") == run_id and p.get("status") == "failed"
        ]
        assert durable, f"no failed hydro record for {run_id}"
    # A FRESH repository (no monkeypatches) sees the target GONE after the
    # direct projection fault: the authority batch committed, the target is no
    # longer a held reserved-unbound row, and journal replay stays
    # authoritative.  A restart reconcile's inventory iteration also no longer
    # blocks on it: the stale reconcile-inventory anchor is cleaned by the
    # demotion, so a fresh reader reports an empty held set (fixture isolation:
    # exactly zero rows).
    fresh = FileOrchestrationJournalRepository(root)
    assert fresh.query_reserved_unbound_jobs() == []
    stale_anchors = [
        path
        for path in sorted((root / "reconcile-inventory").glob("*.json"))
        if ".locks" not in path.parts
    ]
    assert stale_anchors == []
    # The authority master is still demoted, whatever the direct file says.
    assert _held_row(fresh)["status"] == "reservation_lost"
    assert _held_row(fresh)["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
    # Repeating the same request after a warning is still a zero-write CAS refusal.
    before_repeat = _journal_bytes(root)
    assert repository.demote_operator_verified_reserved_job(JOB_ID, **_demote_kwargs(demoted)) is None
    assert _journal_bytes(root) == before_repeat
    assert len(_durable_event_payloads(root)) == 1


def test_demote_single_latest_projection_fault_warns_that_model_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _held_cohort_repository(tmp_path, member_count=2, active_hydro=True)
    root = repository.root
    held = _held_row(repository)

    direct_calls: list[str] = []
    original_direct = repository._write_pipeline_job_direct_unlocked

    def tracking_direct(row: Any, record: Any) -> None:
        direct_calls.append(str(row.get("job_id") or ""))
        original_direct(row, record)

    latest_calls: list[str] = []
    original_materialize = repository._materialize_latest_unlocked

    def failing_materialize(*, source_id: Any, cycle_time: Any, model_id: str, **kwargs: Any) -> None:
        latest_calls.append(model_id)
        if model_id == "model_1":
            raise FileOrchestrationJournalError("file_journal_byte_limit_exceeded", field="latest")
        original_materialize(source_id=source_id, cycle_time=cycle_time, model_id=model_id, **kwargs)

    monkeypatch.setattr(repository, "_write_pipeline_job_direct_unlocked", tracking_direct)
    monkeypatch.setattr(repository, "_materialize_latest_unlocked", failing_materialize)

    receipt = repository.demote_operator_verified_reserved_job(JOB_ID, **_demote_kwargs(held))

    assert receipt is not None
    assert direct_calls == [JOB_ID]
    # Both models were attempted; only model_1's write failed.
    assert sorted(latest_calls) == ["model_0", "model_1"]
    assert len(receipt.warnings) == 1
    warning = receipt.warnings[0]
    assert warning.projection == "latest"
    assert warning.model_id == "model_1"
    # Every caught exception maps to the fixed non-secret token.
    assert warning.error_type == "projection_fault"
    assert warning.reason == "projection_fault"
    assert _held_row(repository)["status"] == "reservation_lost"
    assert len(_durable_event_payloads(root)) == 1


def test_demotion_success_receipt_warnings_empty(tmp_path: Path) -> None:
    repository = _held_cohort_repository(tmp_path, member_count=2, active_hydro=True)
    receipt = repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(_held_row(repository)),
    )
    assert receipt is not None
    assert receipt.warnings == ()


# ---------------------------------------------------------------------------
# 2.4 committed reclaim completion (Round 3, operator old-ID route only)
# ---------------------------------------------------------------------------
def _production_faithful_demoted(tmp_path: Path) -> Any:
    from tests.orchestrator_demote_reserved_job_helpers import (
        _production_faithful_held_cohort_repository,
    )

    repository = _production_faithful_held_cohort_repository(tmp_path)
    held = _held_row(repository)
    assert repository.demote_operator_verified_reserved_job(JOB_ID, **_demote_kwargs(held)) is not None
    return repository, _held_row(repository)


def _public_reclaim_request(demoted: dict[str, Any]) -> dict[str, Any]:
    """The old-ID public recovery shape: same job id / idempotency key."""
    return {
        **demoted,
        "expected_submission_attempt": demoted["submission_attempt"],
        "expected_submission_attempt_started_at": demoted["submission_attempt_started_at"],
        "status": "reserved",
        "submission_attempt": int(demoted["submission_attempt"]) + 1,
        "submission_attempt_started_at": demoted["submission_attempt_started_at"],
        "submit_outcome": None,
        "reconciliation_source": None,
        "reconciliation_decision": None,
        "matched_slurm_job_id": None,
    }


def test_reclaim_post_append_direct_fault_returns_committed_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task 2.4: a post-append direct projection fault on the operator old-ID
    reclaim returns a committed reserved row instead of raising and stranding a
    live pre-sbatch reservation."""
    from services.orchestrator.chain_types import OrchestratorError

    repository, demoted = _production_faithful_demoted(tmp_path)
    root = repository.root

    def fail_direct_once(*_args: Any, **_kwargs: Any) -> None:
        raise OrchestratorError("FILE_JOURNAL_WRITE_FAILED", "injected direct projection failure")

    monkeypatch.setattr(repository, "_write_pipeline_job_direct_unlocked", fail_direct_once)

    reclaimed = repository.reclaim_pipeline_job_reservation(_public_reclaim_request(demoted))

    # The authority append committed (attempt+1 reserved), and the post-append
    # projection fault was contained instead of escaping and wedging the
    # recovery.  The begin-attempt transition clears the accounting tuple, so
    # the committed row carries the null decision -- the operator marker lives
    # in the pre-append source row and is consumed by the reclaim.
    assert reclaimed is not None
    assert reclaimed["status"] == "reserved"
    assert int(reclaimed["submission_attempt"]) == int(demoted["submission_attempt"]) + 1
    assert reclaimed["idempotency_key"] == demoted["idempotency_key"]
    assert reclaimed["job_id"] == JOB_ID
    # A fresh repository replays the committed reserved attempt+1 row.
    fresh = FileOrchestrationJournalRepository(root)
    current = _held_row(fresh)
    assert current["status"] == "reserved"
    assert int(current["submission_attempt"]) == int(demoted["submission_attempt"]) + 1
    assert current["idempotency_key"] == demoted["idempotency_key"]
    assert current["reconciliation_decision"] is None
    # The pre-append fault control is still atomic: nothing durable on failure.
    # It uses a FRESH demoted row (attempt1 reservation_lost), because the
    # committed reclaim above already advanced the row to attempt2 reserved,
    # which the reclaim CAS correctly refuses -- the pre-append gate must never
    # be reachable against a live reservation.  Rebuild a second independent
    # held->demoted fixture so the control starts from the exact reclaimable
    # shape again.
    second_repository, demoted_again = _production_faithful_demoted(tmp_path / "control")
    before_control = _journal_bytes(second_repository.root)

    def fail_before(*_args: Any, **_kwargs: Any) -> None:
        raise FileOrchestrationJournalError("file_journal_append_failed", field="append")

    monkeypatch.setattr(second_repository, "_append_journal_record_unlocked", fail_before)
    with pytest.raises(FileOrchestrationJournalError):
        second_repository.reclaim_pipeline_job_reservation(_public_reclaim_request(demoted_again))
    assert _journal_bytes(second_repository.root) == before_control


def test_reclaim_post_append_direct_fault_emits_one_bounded_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """task 2.4 observability: a committed-reclaim projection fault emits exactly
    one bounded warning with a fixed code, stable projection name, and no
    exception text/class/path/secret-derived detail; the clean path emits none."""
    import logging

    repository, demoted = _production_faithful_demoted(tmp_path)

    class SecretShapedError(RuntimeError):
        pass

    def fail_direct(*_args: Any, **_kwargs: Any) -> None:
        error = SecretShapedError(
            "Authorization: Bearer abc.def.ghi password=supersecret /private/secret-path/token=tok_live_123"
        )
        error.reason = "supersecret"
        error.error_code = "tok_live_123"
        raise error

    monkeypatch.setattr(repository, "_write_pipeline_job_direct_unlocked", fail_direct)
    with caplog.at_level(logging.WARNING, logger="services.orchestrator.file_orchestration_journal"):
        reclaimed = repository.reclaim_pipeline_job_reservation(_public_reclaim_request(demoted))

    assert reclaimed is not None and reclaimed["status"] == "reserved"
    warnings = [
        record
        for record in caplog.records
        if record.name == "services.orchestrator.file_orchestration_journal"
        and record.levelno == logging.WARNING
        and "committed reclaim projection fault" in record.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "projection=pipeline_job_direct" in message
    assert "code=FILE_JOURNAL_RECLAIM_PROJECTION_FAULT" in message
    # Byte-identical fixed message for secret-shaped inputs: no exception text,
    # class name, path, error code/reason, or secret literal ever appears.
    for literal in (
        "abc.def.ghi",
        "supersecret",
        "secret-path",
        "tok_live_123",
        "SecretShapedError",
        "Bearer",
    ):
        assert literal not in message

    # The clean path emits no warning.
    clean_repository, clean_demoted = _production_faithful_demoted(tmp_path / "clean")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="services.orchestrator.file_orchestration_journal"):
        clean = clean_repository.reclaim_pipeline_job_reservation(_public_reclaim_request(clean_demoted))
    assert clean is not None and clean["status"] == "reserved"
    assert [
        record
        for record in caplog.records
        if record.name == "services.orchestrator.file_orchestration_journal"
        and record.levelno == logging.WARNING
        and "committed reclaim projection fault" in record.getMessage()
    ] == []


def test_reclaim_post_append_inventory_fault_keeps_reclaim_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """task 2.4 observability: a post-append reconcile-inventory fault is
    contained, warns exactly once, and journal replay stays authoritative.

    The fault is the SECOND ``_sync_reconcile_inventory_for_row_unlocked`` call
    for the old job inside the containment window (the first, pre-append anchor
    sync, and the direct JSON write both succeed first), raised exactly once.
    The forecast master is model-less, so the containment's latest projection
    is never attempted; the latest site is therefore not a fault case here.
    """
    import logging

    from services.orchestrator.chain_types import OrchestratorError

    repository, demoted = _production_faithful_demoted(tmp_path)
    root = repository.root
    original_sync_inventory = repository._sync_reconcile_inventory_for_row_unlocked
    original_materialize = repository._materialize_latest_unlocked
    latest_calls: list[str] = []
    faults_fired = 0
    inventory_syncs = [0]

    def tracking_materialize(*, source_id: Any, cycle_time: Any, model_id: str, **kwargs: Any) -> None:
        latest_calls.append(model_id)
        original_materialize(source_id=source_id, cycle_time=cycle_time, model_id=model_id, **kwargs)

    def failing_inventory_sync(row: Any) -> bool:
        nonlocal faults_fired
        if (
            str(row.get("job_id") or "") == JOB_ID
            and str(row.get("status") or "") == "reserved"
            and faults_fired == 0
        ):
            inventory_syncs[0] += 1
            if inventory_syncs[0] == 2:
                faults_fired += 1
                raise OrchestratorError("FILE_JOURNAL_WRITE_FAILED", "injected inventory projection failure")
        return original_sync_inventory(row)

    monkeypatch.setattr(repository, "_sync_reconcile_inventory_for_row_unlocked", failing_inventory_sync)
    monkeypatch.setattr(repository, "_materialize_latest_unlocked", tracking_materialize)
    with caplog.at_level(logging.WARNING, logger="services.orchestrator.file_orchestration_journal"):
        reclaimed = repository.reclaim_pipeline_job_reservation(_public_reclaim_request(demoted))

    assert reclaimed is not None and reclaimed["status"] == "reserved"
    assert int(reclaimed["submission_attempt"]) == int(demoted["submission_attempt"]) + 1
    assert faults_fired == 1
    assert inventory_syncs[0] == 2
    # A forecast master is model-less, so the containment's latest projection
    # guard (``model_id is not None``) never runs -- a boundary statement, not
    # a fault case; the inventory fault is the reachable post-append site and
    # it warns exactly once with the fixed bounded token.
    assert latest_calls == []
    warnings = [
        record
        for record in caplog.records
        if record.name == "services.orchestrator.file_orchestration_journal"
        and record.levelno == logging.WARNING
        and "committed reclaim projection fault" in record.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "projection=pipeline_job_direct" in message
    assert "code=FILE_JOURNAL_RECLAIM_PROJECTION_FAULT" in message
    assert "injected" not in message
    assert "FILE_JOURNAL_WRITE_FAILED" not in message
    assert "OrchestratorError" not in message
    # Journal replay stays authoritative: a fresh repository sees the committed
    # reserved attempt+1 row with the old id/key preserved.
    fresh = FileOrchestrationJournalRepository(root)
    current = _held_row(fresh)
    assert current["status"] == "reserved"
    assert int(current["submission_attempt"]) == int(demoted["submission_attempt"]) + 1
    assert current["idempotency_key"] == demoted["idempotency_key"]
