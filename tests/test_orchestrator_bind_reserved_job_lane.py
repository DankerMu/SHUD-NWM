"""#2668: a held forecast master bound by ``bind-reserved-job`` releases the source's lane.

Production shape (#2666 / #2667): the forecast array submit timed out at the
gateway while Slurm accepted it, so the master stays ``reserved`` /
``submit_result_ambiguous``.  On node-22 (comment-less accounting) restart
reconcile can only use the name-window fallback, and two shapes never resolve:

* (a) ``ambiguous_fallback_match`` -- two in-window masters, no provable key;
* (b) ``query_unavailable`` -- the ever-growing fallback window saturates.

#2667 then skips every member ``active_duplicate_pipeline`` and the cycle's
completion verdict stays ``gap``, holding the source's only backfill slot.

Seams, all real except the Slurm runtime and ``sacct`` stdout: the
``orchestrate_cycle`` stage loop writes the journal through a real
``FileOrchestrationJournalRepository``; ``reconcile_reserved_unbound_jobs``
writes the held tuple; the shipped CLI ``bind-reserved-job`` binds;
``reconcile_inflight_jobs`` projects accounting; the scheduler's
``_candidate_state_decision`` and ``_cycle_completion_status`` (which scores
through ``scheduler_discovery._cycle_completion_verdict``) judge the lane; and
``scripts/node22_manual_retry_failed_runs.py`` previews/marks the retry.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.node22_manual_retry_failed_runs import main as manual_retry_main
from services.orchestrator import cli
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from services.orchestrator.operator_action_listing_held import _held_job_is_forecast_master
from tests.test_gateway_reconcile_claimant_exclusivity import _fallback_querier, _fallback_row
from tests.test_orchestration_chain import _orchestrator
from tests.test_real_slurm_gateway import _pinned_local_timezone
from tests.test_scheduler_held_reservation_block import (
    _CYCLE,
    _HELD,
    _candidates,
    _cohort_basins,
    _cohort_run_id,
    _decision,
    _discovery,
    _forcing_witness,
    _master,
    _raw_models,
    _Runtime,
    _scheduler,
)

pytestmark = pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")

_MASTER_ID = "57553"
_OTHER_MASTER_ID = "57554"
_TERMINAL = ("skip", "terminal_hydro_success")


@pytest.fixture(autouse=True)
def _planner_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
    monkeypatch.delenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", raising=False)
    # sacct prints ``Submit`` in local time; the fixture cluster runs in UTC.
    with _pinned_local_timezone("UTC"):
        yield


def _sacct_local(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _held_production_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str, members: int = 2
) -> tuple[FileOrchestrationJournalRepository, list[dict[str, Any]], list[Any], datetime]:
    """Pass 1 times out on the forecast submit; restart reconcile leaves shape (a) or (b)."""

    from services.orchestrator import reconcile as reconcile_module

    raw_models = _raw_models(range(members))
    candidates = _candidates(raw_models)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    orchestrator = _orchestrator(tmp_path / "first-pass", repository, _Runtime("forecast"))
    # The fallback needs the owner the node-22 scheduler records on every master.
    orchestrator.config = dataclasses.replace(
        orchestrator.config, reconcile_slurm_user="scheduler", reconcile_slurm_account="account"
    )
    result = orchestrator.orchestrate_cycle("gfs", _CYCLE, _cohort_basins(candidates))
    assert [(stage.stage, stage.status) for stage in result.stages][-1] == ("forecast", "submit_result_ambiguous")
    _forcing_witness(monkeypatch, tmp_path, candidates)
    anchor = datetime.fromisoformat(
        str(_master(repository, "forecast")["submission_attempt_started_at"]).replace("Z", "+00:00")
    )
    query_end = anchor + timedelta(hours=1)
    if shape == "a":
        rows = _fallback_row(f"{_MASTER_ID}_0", submit=_sacct_local(anchor + timedelta(seconds=60))) + _fallback_row(
            f"{_OTHER_MASTER_ID}_0", submit=_sacct_local(anchor + timedelta(seconds=70))
        )
        query, _commands = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)
    else:

        def _saturated(_command: Any, **_kwargs: Any) -> str:
            raise reconcile_module.ReconcileQuerySaturated("rows")

        monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", _saturated)
        query = reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True, comment_storage_probe=lambda: False, now=lambda: query_end
        )
    (outcome,) = reconcile_module.reconcile_reserved_unbound_jobs(
        repository, comment_query=query, now=lambda: query_end
    )
    assert outcome.action == {"a": "ambiguous_fallback_match", "b": "query_unavailable"}[shape]
    held = _master(repository, "forecast")
    # The listing classifies this orchestrator-minted master id (and its retry
    # clone spelling) as a forecast master, so it is offered bind-reserved-job.
    assert _held_job_is_forecast_master(held["job_id"])
    assert _held_job_is_forecast_master(f"{held['job_id']}_retry_1")
    assert (held["status"], held["slurm_job_id"], held["reconciliation_reason_class"]) == (
        "reserved",
        None,
        "comment_accounting_unproven",
    )
    return repository, raw_models, candidates, anchor


def _verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: Any, raw_models: Sequence[Any]) -> str:
    scheduler = _scheduler(tmp_path, repository, raw_models, lookback_hours=0, cycle_lag_hours=0)
    monkeypatch.setattr(scheduler, "_strict_warm_start_for_candidate", lambda _candidate, _cycle: None)
    monkeypatch.setattr(
        scheduler,
        "_successor_warm_start_state_for_candidate",
        lambda _candidate, _cycle: {"ready": True, "status": "ready"},
    )
    models = [scheduler_module._coerce_registered_model(model) for model in raw_models]
    return scheduler._cycle_completion_status(_discovery(), models, horizon={})


def _bind(repository: Any, anchor: datetime, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    held = _master(repository, "forecast")
    args = [
        "bind-reserved-job",
        "--journal-root",
        str(repository.root),
        "--job-id",
        str(held["job_id"]),
        "--slurm-job-id",
        _MASTER_ID,
        "--slurm-submit-time",
        _iso(anchor + timedelta(seconds=60)),
        "--submit-line",
        f"/usr/bin/sbatch --array=0-1%15 --comment=nhms_idem:{held['idempotency_key']} /tmp/nhms_x.sbatch",
        "--expected-attempt",
        str(held["submission_attempt"]),
        "--expected-attempt-started-at",
        str(held["submission_attempt_started_at"]),
        "--checked-by",
        "operator-alice",
        "--checked-at",
        _iso(anchor + timedelta(hours=2)),
        "--verification-note",
        f"sacct shows {_MASTER_ID} carrying this key; {_OTHER_MASTER_ID} is another cohort",
        "--confirm",
    ]
    assert cli.main(args) in (0, None)
    return json.loads(capsys.readouterr().out)


def _project_accounting(repository: Any, members: int, *, failed_task: int | None = None) -> list[Any]:
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    tasks = tuple(
        SacctRecord(
            f"{_MASTER_ID}_{index}",
            "FAILED" if index == failed_task else "COMPLETED",
            "nhms_forecast",
            exit_code="1:0" if index == failed_task else "0:0",
            user="scheduler",
            account="account",
            array_task_id=index,
        )
        for index in range(members)
    )
    master = SacctRecord(
        slurm_job_id=_MASTER_ID,
        raw_state="FAILED" if failed_task is not None else "COMPLETED",
        job_name="nhms_forecast",
        exit_code="1:0" if failed_task is not None else "0:0",
        user="scheduler",
        account="account",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tasks,
    )
    return reconcile_inflight_jobs(repository, sacct_query=lambda job_id: master if str(job_id) == _MASTER_ID else None)


@pytest.mark.parametrize("shape", ["a", "b"])
def test_a_bound_held_master_projects_terminal_and_the_cycle_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], shape: str
) -> None:
    repository, raw_models, candidates, anchor = _held_production_shape(tmp_path, monkeypatch, shape)
    # Before: every member is held (the #2667 skip) and the cycle reads ``gap``.
    assert {(_decision(repository, c).action, _decision(repository, c).reason) for c in candidates} == {_HELD}
    assert _verdict(tmp_path, monkeypatch, repository, raw_models) == "gap"

    receipt = _bind(repository, anchor, capsys)

    assert (receipt["status"], receipt["matched_slurm_job_id"], receipt["written_record_count"]) == (
        "bound",
        _MASTER_ID,
        2,
    )
    bound = _master(repository, "forecast")
    assert (bound["status"], bound["reconciliation_decision"], bound["reconciliation_source"]) == (
        "submitted",
        "matched_bound",
        "slurm_name_window_unique",
    )
    # Terminal projection stays with the next pass's inflight reconcile.
    for candidate in candidates:
        assert repository._hydro_run_for(candidate.run_id)["status"] == "created"

    inflight = _project_accounting(repository, len(candidates))

    assert [(outcome.job_id, outcome.status) for outcome in inflight] == [(bound["job_id"], "succeeded")]
    for candidate in candidates:
        assert repository._hydro_run_for(candidate.run_id)["status"] == "succeeded"
        assert (_decision(repository, candidate).action, _decision(repository, candidate).reason) == _TERMINAL
    assert _verdict(tmp_path, monkeypatch, repository, raw_models) == "complete"


def test_issue_scenario_only_the_failed_tasks_member_needs_the_manual_retry_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Members already succeeded through a sibling attempt; the bound array has one FAILED task.

    The succeeded members stay terminal; only the failed task's member becomes
    ``permanent_failure_guard`` (the superseded-attempt ordering is the #2666
    basin-18 shape and out of scope), and its exit is the manual-retry marker on
    the cohort master, which re-runs only that member.
    """

    repository, raw_models, candidates, anchor = _held_production_shape(tmp_path, monkeypatch, "a")
    for candidate in candidates:
        repository.update_hydro_run_status(candidate.run_id, "succeeded")
    # The issue's freeze: terminal hydro truth, but the held row skips every member.
    assert {(_decision(repository, c).action, _decision(repository, c).reason) for c in candidates} == {_HELD}
    assert _verdict(tmp_path, monkeypatch, repository, raw_models) == "gap"

    _bind(repository, anchor, capsys)
    _project_accounting(repository, len(candidates), failed_task=0)

    failed, succeeded = candidates
    assert {repository._hydro_run_for(c.run_id)["status"] for c in candidates} == {"succeeded"}
    failed_decision = _decision(repository, failed)
    assert (failed_decision.action, failed_decision.reason) == ("blocked", "permanent_failure_guard")
    assert (_decision(repository, succeeded).action, _decision(repository, succeeded).reason) == _TERMINAL

    run_id = _cohort_run_id(candidates)
    base = ["--journal-root", str(repository.root), "--run-id", run_id, "--reason", "bound 57553 task 0 failed"]
    assert manual_retry_main([*base, "--requested-by", "operator-alice"]) == 0
    (preview,) = json.loads(capsys.readouterr().out)["runs"]
    assert preview["preview"]["decision"] == "would_mark"
    assert preview["preview"]["job_id"] == _master(repository, "forecast")["job_id"]

    assert manual_retry_main([*base, "--requested-by", "operator-alice", "--execute"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["outcome"] == "marked"
    rerun = _decision(repository, failed)
    assert (rerun.action, rerun.reason) == ("retry", "manual_retry_requested")
    assert (_decision(repository, succeeded).action, _decision(repository, succeeded).reason) == _TERMINAL
