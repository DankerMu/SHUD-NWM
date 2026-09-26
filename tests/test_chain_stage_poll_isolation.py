"""A transient gateway or persistence failure inside a stage poll is governed (#2570 group A).

node-22 lost whole cohort chains to one exception escaping
``chain_stage_execution.poll_cycle_stage_until_terminal``: the gateway status query and the
two repository writes of the poll loop had no isolation, so ``orchestrate_cycle`` raised,
``scheduler_execution`` recorded every member ``submission_failed``, the remaining stages
never ran and the stage span committed ``basin_count=0``.  The requirement: a query failure
is retried until the job deadline and, if the deadline wins, ends ``reconcile_unverified`` /
``SLURM_STATUS_QUERY_UNAVAILABLE`` without any write; a status-write failure ends
``reconcile_unverified`` / ``STAGE_RUNTIME_STATUS_PERSIST_FAILED`` with no further write or
gateway call; an event-write failure is counted and polling goes on.  A bound Slurm job is
never submitted twice because of either failure, and the next pass resolves it.

Seams: the real ``orchestrate_cycle`` stage loop (``submit_and_wait_cycle_stage`` /
``resume_cycle_stage``) against a real ``FileOrchestrationJournalRepository``; only the Slurm
runtime is a double, and the repository faults are injected by a delegating proxy.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.chain import OrchestratorError
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from services.orchestrator.scheduler_candidate_quality import RECONCILIATION_PENDING_STATUSES
from services.orchestrator.scheduler_timing import SchedulerPassTiming, set_current_scheduler_pass_timing
from tests.test_orchestration_chain import FakeCycleSlurmClient, _forecast_cohort_basins, _orchestrator

_CYCLE = "2026050100"
_CYCLE_ID = f"gfs_{_CYCLE}"
_RUN_ID = "cycle_gfs_2026050100_forecast_cohort_fixture"
_CONVERT_JOB = f"job_{_RUN_ID}_convert"
_FORECAST_JOB = f"job_{_RUN_ID}_forecast"
_FULL_CHAIN = ["convert", "forcing", "forecast", "parse", "state_save_qc", "publish"]


def _convert_restart_basins() -> list[dict[str, Any]]:
    basins = _forecast_cohort_basins()
    for basin in basins:
        basin["restart_stage"] = "convert"
        basin["state_evidence"] = {"restart_stage": "convert"}
    return basins


def _rows(repository: Any) -> dict[str, dict[str, Any]]:
    reader = FileOrchestrationJournalRepository(repository.root)
    return {row["job_id"]: row for row in reader.query_pipeline_jobs_by_cycle(_CYCLE_ID)}


class _StatusQueryFaults(FakeCycleSlurmClient):
    """The Slurm runtime double whose status endpoint fails for one stage."""

    def __init__(self, *, stage: str, failures: int | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fault_stage = stage
        self.remaining_failures = failures  # ``None``: fail forever
        self.status_query_failures = 0

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        if self.jobs[job_id]["stage"] == self.fault_stage and self.remaining_failures != 0:
            if self.remaining_failures is not None:
                self.remaining_failures -= 1
            self.status_query_failures += 1
            time.sleep(0.01)
            raise ConnectionError("gateway status endpoint unreachable")
        return super().get_job_status(job_id)


def test_transient_status_query_failures_are_retried_and_the_chain_completes(tmp_path: Path) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _StatusQueryFaults(stage="convert", failures=2)
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", _CYCLE, _convert_restart_basins())

    assert client.status_query_failures == 2
    assert [(stage.stage, stage.status) for stage in result.stages] == [
        (stage, "succeeded") for stage in _FULL_CHAIN
    ]
    assert result.status == "complete"
    # One submission per stage: the query failures never caused a resubmit.
    assert [payload["stage"] for payload in client.submissions] == _FULL_CHAIN
    convert = result.stages[0]
    assert convert.accounting["poll_isolation"] == {
        "status_query_failures": 2,
        "status_query_last_error": "ConnectionError",
        "pipeline_event_write_failures": 0,
    }


def test_status_query_outage_until_the_deadline_is_reconcile_unverified_without_resubmit(tmp_path: Path) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _StatusQueryFaults(stage="convert", failures=None)
    orchestrator = _orchestrator(tmp_path, repository, client, job_timeout_seconds=0.2)

    result = orchestrator.orchestrate_cycle("gfs", _CYCLE, _convert_restart_basins())

    assert client.status_query_failures >= 1
    assert [(stage.stage, stage.status, stage.error_code) for stage in result.stages] == [
        ("convert", "reconcile_unverified", "SLURM_STATUS_QUERY_UNAVAILABLE")
    ]
    # ``reconciling`` is the scheduler's governed reconciliation-pending terminal, so the
    # unit's members are recorded pending -- never ``submission_failed``.
    assert result.status == "reconciling"
    assert result.status in RECONCILIATION_PENDING_STATUSES
    assert "failed" not in {outcome["status"] for outcome in result.candidate_outcomes}
    assert [payload["stage"] for payload in client.submissions] == ["convert"]
    row = _rows(repository)[_CONVERT_JOB]
    # No terminal (or any timeout) write: the row keeps its bound id in its runtime status.
    assert row["status"] == "pending"
    assert row["slurm_job_id"] == result.stages[0].slurm_job_id == "2001"
    assert row["error_code"] is None
    assert result.stages[0].accounting["poll_isolation"]["status_query_failures"] == client.status_query_failures


def test_deadline_without_query_failures_keeps_the_timeout_path(tmp_path: Path) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = FakeCycleSlurmClient(never_terminal_stage="convert")
    orchestrator = _orchestrator(tmp_path, repository, client, job_timeout_seconds=0.2)

    result = orchestrator.orchestrate_cycle("gfs", _CYCLE, _convert_restart_basins())

    convert = result.stages[0]
    assert (convert.stage, convert.status, convert.error_code) == ("convert", "failed", "SLURM_JOB_TIMEOUT")
    assert "poll_isolation" not in convert.accounting
    row = _rows(repository)[_CONVERT_JOB]
    assert (row["status"], row["error_code"]) == ("failed", "SLURM_JOB_TIMEOUT")


_WRITE_METHODS = frozenset(
    {
        "update_pipeline_job_status",
        "transition_pipeline_job_runtime_status",
        "insert_pipeline_event",
        "upsert_pipeline_job",
        "project_forecast_cohort_tasks",
        "defer_forecast_cohort_projection",
        "transition_pipeline_job_submit_evidence",
        "commit_pipeline_job_submit_attempt",
    }
)
_STATUS_WRITE_METHODS = frozenset({"update_pipeline_job_status", "transition_pipeline_job_runtime_status"})


class _FaultyJournal:
    """Delegates to a real journal; once tripped, every write of one job raises ``OSError``.

    The first runtime status write of ``job_id`` trips the fault.  Every later write that
    names the job is recorded in ``writes_after_fault`` before it raises, so a stray
    post-poll write shows up as evidence rather than only as an exception.
    """

    def __init__(self, inner: FileOrchestrationJournalRepository, *, job_id: str) -> None:
        self._inner = inner
        self._job_id = job_id
        self.armed = True
        self.tripped = False
        self.writes_after_fault: list[str] = []

    def _names_job(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        candidates = [*args[:1], kwargs.get("entity_id"), kwargs.get("job_id"), kwargs.get("pipeline_job_id")]
        if args and isinstance(args[0], dict):
            candidates.append(args[0].get("job_id"))
        return self._job_id in {str(value) for value in candidates if value is not None}

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if name not in _WRITE_METHODS or not callable(attribute):
            return attribute

        def _write(*args: Any, **kwargs: Any) -> Any:
            if self.armed and self._names_job(args, kwargs):
                if self.tripped:
                    self.writes_after_fault.append(name)
                    raise OSError(28, "No space left on device")
                if name in _STATUS_WRITE_METHODS:
                    self.tripped = True
                    raise OSError(28, "No space left on device")
            return attribute(*args, **kwargs)

        return _write


@pytest.mark.parametrize(
    ("stage", "job_id", "downstream"),
    [
        ("convert", _CONVERT_JOB, ["forcing", "forecast", "parse", "state_save_qc", "publish"]),
        ("forecast", _FORECAST_JOB, ["parse", "state_save_qc", "publish"]),
    ],
)
def test_status_persist_failure_is_governed_and_the_next_pass_resolves_the_bound_job(
    tmp_path: Path, stage: str, job_id: str, downstream: list[str]
) -> None:
    journal = FileOrchestrationJournalRepository(tmp_path / "journal")
    faulty = _FaultyJournal(journal, job_id=job_id)
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, faulty, client)
    timing = SchedulerPassTiming(pass_id="scheduler_test_2570persist01", level="stage")

    # Pass 1: the stage's first runtime status write fails, and so would every later one.
    with timing.pass_span(), set_current_scheduler_pass_timing(timing):
        first = orchestrator.orchestrate_cycle("gfs", _CYCLE, _convert_restart_basins())

    assert faulty.tripped is True
    # After the failed status write: no repository write and no gateway call for the stage.
    assert faulty.writes_after_fault == []
    bound_slurm_id = str(_rows(journal)[job_id]["slurm_job_id"])
    assert client.poll_counts[bound_slurm_id] == 1
    assert bound_slurm_id not in client.fetch_log_calls
    final = first.stages[-1]
    assert (final.stage, final.status, final.error_code) == (
        stage,
        "reconcile_unverified",
        "STAGE_RUNTIME_STATUS_PERSIST_FAILED",
    )
    assert final.slurm_job_id not in (None, "")
    assert first.status == "reconciling"
    assert first.status in RECONCILIATION_PENDING_STATUSES
    assert "failed" not in {outcome["status"] for outcome in first.candidate_outcomes}
    submissions_after_first_pass = [payload["stage"] for payload in client.submissions]
    assert submissions_after_first_pass[-1] == stage
    spans = {record["stage_name"]: record for record in timing.finalize_evidence(status=first.status)["stages"]}
    assert spans[stage]["basin_count"] == 2
    row = _rows(journal)[job_id]
    # The row stays in its pre-failure runtime status, still bound to its Slurm job.
    assert row["status"] == "pending"
    assert row["slurm_job_id"] == final.slurm_job_id

    # Another unit of the same pass (a different cycle on the same journal) is unaffected.
    other_client = FakeCycleSlurmClient()
    other_client.next_job = 5000
    other = _orchestrator(tmp_path / "other", FileOrchestrationJournalRepository(journal.root), other_client)
    other_basins = [
        {
            **basin,
            "run_id": basin["run_id"].replace(_CYCLE, "2026050106"),
            "candidate_id": basin["candidate_id"].replace("T00:00:00Z", "T06:00:00Z"),
            "orchestration_run_id": "cycle_gfs_2026050106_forecast_cohort_fixture",
        }
        for basin in _convert_restart_basins()
    ]
    assert other.orchestrate_cycle("gfs", "2026050106", other_basins).status == "complete"

    # Pass 2, persistence healed.  The scheduler's pass-start restart reconcile reads the
    # still-bound job's accounting (``reconcile_inflight_jobs`` never submits), then the
    # unit's chain resumes downstream; the bound stage is never submitted again.
    from services.orchestrator.reconcile import reconcile_inflight_jobs

    faulty.armed = False
    outcomes = reconcile_inflight_jobs(journal, sacct_query=_completed_sacct(client, journal))
    assert [(outcome.job_id, outcome.action) for outcome in outcomes] == [(job_id, "terminal")]
    assert _rows(journal)[job_id]["status"] == "succeeded"
    second = orchestrator.orchestrate_cycle("gfs", _CYCLE, _convert_restart_basins())

    assert second.status == "complete"
    assert [(result.stage, result.status) for result in second.stages][-len(downstream) - 1 :] == [
        (name, "succeeded") for name in [stage, *downstream]
    ]
    assert [payload["stage"] for payload in client.submissions] == [*submissions_after_first_pass, *downstream]


def _completed_sacct(client: FakeCycleSlurmClient, journal: FileOrchestrationJournalRepository) -> Any:
    """Slurm accounting for the double's bound jobs: each ran to COMPLETED."""

    from services.orchestrator.reconcile import SacctRecord

    rows = {str(row["slurm_job_id"]): row for row in _rows(journal).values() if row.get("slurm_job_id")}

    def _query(slurm_job_id: str) -> Any:
        row = rows[str(slurm_job_id)]
        # The runtime double's own view agrees: the job finished while nobody polled it.
        client.jobs[str(slurm_job_id)]["status"] = "succeeded"
        task_count = len(client.jobs[str(slurm_job_id)]["payload"].get("tasks") or [])
        tasks = tuple(
            SacctRecord(
                f"{slurm_job_id}_{index}",
                "COMPLETED",
                f"nhms_{row['stage']}",
                exit_code="0:0",
                comment=row.get("slurm_comment"),
                array_task_id=index,
            )
            for index in range(task_count)
        )
        return SacctRecord(
            str(slurm_job_id),
            "COMPLETED",
            f"nhms_{row['stage']}",
            exit_code="0:0",
            comment=row.get("slurm_comment"),
            array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
            array_task_records=tasks,
        )

    return _query


class _EventWriteFailsOnce:
    def __init__(self, inner: FileOrchestrationJournalRepository, *, job_id: str) -> None:
        self._inner = inner
        self._job_id = job_id
        self.failures = 0

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if name != "insert_pipeline_event":
            return attribute

        def _insert(*args: Any, **kwargs: Any) -> Any:
            if (
                not self.failures
                and kwargs.get("entity_id") == self._job_id
                and kwargs.get("event_type") == "status_change"
            ):
                self.failures += 1
                raise OSError(5, "Input/output error")
            return attribute(*args, **kwargs)

        return _insert


def test_event_write_failure_is_counted_and_the_chain_continues(tmp_path: Path) -> None:
    journal = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository = _EventWriteFailsOnce(journal, job_id=_CONVERT_JOB)
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", _CYCLE, _convert_restart_basins())

    assert repository.failures == 1
    assert result.status == "complete"
    assert [(stage.stage, stage.status) for stage in result.stages] == [
        (stage, "succeeded") for stage in _FULL_CHAIN
    ]
    assert result.stages[0].accounting["poll_isolation"] == {
        "status_query_failures": 0,
        "status_query_last_error": None,
        "pipeline_event_write_failures": 1,
    }
    assert _rows(journal)[_CONVERT_JOB]["status"] == "succeeded"


def test_an_unexpected_stage_exception_still_propagates_with_the_real_span_basin_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    orchestrator = _orchestrator(tmp_path, repository, FakeCycleSlurmClient())
    after_terminal = orchestrator._after_cycle_stage_terminal

    def _explodes_after_forcing(stage: Any, *args: Any, **kwargs: Any) -> Any:
        if stage.stage == "forcing":
            raise OrchestratorError("UNEXPECTED_FIXTURE_FAULT", "post-terminal hook exploded")
        return after_terminal(stage, *args, **kwargs)

    monkeypatch.setattr(orchestrator, "_after_cycle_stage_terminal", _explodes_after_forcing)
    timing = SchedulerPassTiming(pass_id="scheduler_test_2570span0001", level="stage")

    with pytest.raises(OrchestratorError) as raised:
        with timing.pass_span(), set_current_scheduler_pass_timing(timing):
            orchestrator.orchestrate_cycle("gfs", _CYCLE, _convert_restart_basins())

    assert raised.value.error_code == "UNEXPECTED_FIXTURE_FAULT"
    spans = {record["stage_name"]: record for record in timing.finalize_evidence(status="failed")["stages"]}
    assert spans["convert"]["basin_count"] == 2
    assert spans["forcing"]["basin_count"] == 2
