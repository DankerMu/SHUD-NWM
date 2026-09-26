"""A failed or unverified forecast cohort task restarts from forecast (#2559).

A forecast cohort that re-entered the chain at ``convert`` or ``forcing`` carries that
cohort-entry marker as each basin's top-level ``restart_stage``.  The terminal projection
used to copy it onto every failed/unverified task; ``normalize_candidate_projections``
rejects anything outside ``ACCEPTED_RESTART_STAGES`` (``forecast``/``state_save_qc``), so
an ordinary forecast failure was deferred as ``identity_mismatch_blocked`` /
``SLURM_TASK_IDENTITY_MISMATCH`` and every per-task outcome was lost.

Seam: the real ``orchestrate_cycle`` stage loop against a real
``FileOrchestrationJournalRepository`` (``supports_accepted_submit_reconcile``), so the
forecast terminal goes through ``record_cycle_stage_status_override`` ->
``project_forecast_cohort_tasks``; only the Slurm runtime is a double.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.orchestrator.accepted_submit_identity import ACCEPTED_RESTART_STAGES
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from tests.test_orchestration_chain import (
    _COHORT_JOB_ID,
    FakeCycleSlurmClient,
    _durable_cohort_master,
    _forecast_cohort_basins,
    _orchestrator,
)


def _restarted_cohort_basins(restart_stage: str) -> list[dict[str, object]]:
    basins = _forecast_cohort_basins()
    for basin in basins:
        basin["restart_stage"] = restart_stage
        basin["state_evidence"] = {"restart_stage": restart_stage}
    return basins


@pytest.mark.parametrize("entry_restart_stage", ["convert", "forcing"])
def test_failed_forecast_task_projection_restarts_from_forecast(tmp_path: Path, entry_restart_stage: str) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = FakeCycleSlurmClient(array_results_by_stage={"forecast": ["failed", "succeeded"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    orchestrator.orchestrate_cycle("gfs", "2026050100", _restarted_cohort_basins(entry_restart_stage))

    # The chain really re-entered at the cohort-entry stage before forecast.
    submitted_stages = [payload["stage"] for payload in client.submissions]
    assert submitted_stages[0] == entry_restart_stage
    assert "forecast" in submitted_stages
    master = _durable_cohort_master(repository.root)
    assert master["job_id"] == _COHORT_JOB_ID
    assert master["reconciliation_decision"] == "matched_bound"
    assert master["error_code"] != "SLURM_TASK_IDENTITY_MISMATCH"
    assert master["status"] == "partially_failed"
    assert [
        (item["array_task_id"], item["array_task_outcome"], item["restart_stage"])
        for item in master["candidate_projections"]
    ] == [(0, "failed", "forecast"), (1, "succeeded", "state_save_qc")]
    assert {item["restart_stage"] for item in master["candidate_projections"]} <= ACCEPTED_RESTART_STAGES


@pytest.mark.parametrize("entry_restart_stage", ["convert", "forcing"])
def test_failed_and_missing_forecast_tasks_defer_as_incomplete_accounting_not_identity_mismatch(
    tmp_path: Path, entry_restart_stage: str
) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    # Task 0 fails; task 1 has no accounting row at all (unverified).
    client = FakeCycleSlurmClient(array_results_by_stage={"forecast": ["failed"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _restarted_cohort_basins(entry_restart_stage))

    master = _durable_cohort_master(repository.root)
    # The projection passed identity validation; the governed incomplete-coverage
    # deferral (not the identity-mismatch one) owns the missing task.
    assert master["reconciliation_decision"] == "accounting_unavailable"
    assert master["reconciliation_reason_class"] == "coverage_incomplete"
    assert master["error_code"] == "SLURM_TASK_ACCOUNTING_INCOMPLETE"
    assert master["status"] == "reconcile_unverified"
    forecast = [stage for stage in result.stages if stage.stage == "forecast"]
    assert [(stage.status, stage.error_code) for stage in forecast] == [
        ("reconcile_unverified", "SLURM_TASK_ACCOUNTING_INCOMPLETE")
    ]


def test_all_succeeded_cohort_projection_still_restarts_from_state_save_qc(tmp_path: Path) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    orchestrator.orchestrate_cycle("gfs", "2026050100", _restarted_cohort_basins("convert"))

    master = _durable_cohort_master(repository.root)
    assert master["status"] == "succeeded"
    assert [item["restart_stage"] for item in master["candidate_projections"]] == [
        "state_save_qc",
        "state_save_qc",
    ]
