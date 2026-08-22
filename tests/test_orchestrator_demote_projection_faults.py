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
