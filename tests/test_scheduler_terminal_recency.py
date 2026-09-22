"""#2401: completed-type terminal skips yield to a newer failure truth.

Two seams:

* real file journals driven through the REAL scheduler pass and the REAL
  forecast orchestrator + reservation path (only Slurm is faked) -- the §8.7
  quarantine loop, the ``terminal_run_manifest_missing`` sibling and the strict
  warm-start mismatch sibling;
* the public candidate-state decision (``scheduler._candidate_state_decision``)
  over explicit journal-shaped states -- the recency/tie/no-timestamp rules and
  the must-preserve shapes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import scheduler as scheduler_module

_RETRY_LIMIT = 2
_FORECAST_FAILURE = {"fail_stage": "forecast", "array_results_by_stage": {"forecast": ["failed"]}}


def _pass(tmp_path: Path, root: Path) -> tuple[Any, Any]:
    from tests.test_operator_reentry_confirmation import breaker_scheduler
    from tests.test_production_scheduler import FakeProductionOrchestrator

    orchestrator = FakeProductionOrchestrator()
    result = breaker_scheduler(tmp_path, root, orchestrator, retry_limit=_RETRY_LIMIT).run_once()
    return result, orchestrator


def _decisions(result: Any) -> list[tuple[str, str | None]]:
    rows: list[tuple[str, str | None]] = []
    for key in ("candidates", "blocked_candidates", "skipped_candidates"):
        for item in result.evidence.get(key) or []:
            rows.append((key, (item.get("state_evidence") or {}).get("decision")))
    return rows


def _drive_failing_reruns(
    tmp_path: Path, root: Path, passes: int
) -> tuple[int, list[list[tuple[str, str | None]]], list[list[str]]]:
    """Run ``passes`` scheduler passes; every dispatched handoff fails at ``forecast``.

    The handoff runs through the REAL orchestrator wired the way production's
    db-free scheduler wires it (``scheduler_core._db_free_file_retry_service_from_env``):
    a ``FileJournalRetryService`` owns the chain's inline ``_retry_<n>`` minting
    and the decline -> ``permanently_failed`` mark.  ``max_retries`` equals the
    scheduler's ``retry_limit``.  Slurm times are on the journal's clock.

    Returns REAL Slurm forecast submissions, per-pass decisions, per-pass blocked reasons.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.retry import RetryConfig
    from tests.test_operator_reentry_confirmation import real_rerun

    slurm_forecast = 0
    trace: list[list[tuple[str, str | None]]] = []
    blocked_reasons: list[list[str]] = []
    for _ in range(passes):
        result, orchestrator = _pass(tmp_path, root)
        trace.append(_decisions(result))
        blocked_reasons.append([str(item.get("reason")) for item in result.evidence.get("blocked_candidates") or []])
        if not orchestrator.calls:
            continue
        (call,) = orchestrator.calls
        client = _wallclock_slurm_client(**_FORECAST_FAILURE)
        retry_service = FileJournalRetryService(
            FileOrchestrationJournalRepository(root),
            RetryConfig(max_retries=_RETRY_LIMIT, backoff_schedule=[0]),
        )
        failed = real_rerun(
            tmp_path,
            root,
            [dict(basin) for basin in call["basins"]],
            slurm_client=client,
            retry_service=retry_service,
        )
        assert failed.status == "failed"
        slurm_forecast += sum(1 for item in client.submissions if item.get("stage") == "forecast")
    return slurm_forecast, trace, blocked_reasons


# ---------------------------------------------------------------------------
# Real-journal loops
# ---------------------------------------------------------------------------

_BUDGET_EXHAUSTED = [("blocked_candidates", "permanent_failure")]


def test_failed_quarantine_rerun_reaches_the_failure_path_and_stops_at_the_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_operator_reentry_confirmation import seed_breaker_journal

    # Stale-token completed cycle; no operator confirmation; no completed stamped master.
    root = seed_breaker_journal(tmp_path, monkeypatch, breaker_engaged=False)

    slurm_forecast, trace, blocked = _drive_failing_reruns(tmp_path, root, _RETRY_LIMIT + 4)

    # One forced quarantine rerun, then ``retry_limit`` inline retries, then the
    # decline marks the rerun ``permanently_failed``.  Pre-fix: the old success
    # re-read it as terminal and every pass minted one more forced resubmission.
    assert slurm_forecast == 1 + _RETRY_LIMIT, (slurm_forecast, trace)
    assert trace[0] == [("candidates", "retry_journal_predecessor_identity_mismatch")], trace
    # Every later pass is the failure path's budget-exhausted block -- never a
    # completed-type skip rewritten into another quarantine retry.
    assert all(decisions == _BUDGET_EXHAUSTED for decisions in trace[1:]), trace
    assert all(reasons == ["retry_limit_exhausted"] for reasons in blocked[1:]), blocked


def test_terminal_run_manifest_missing_stops_refiring_after_a_newer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_operator_reentry_confirmation import seed_breaker_journal

    root = seed_breaker_journal(tmp_path, monkeypatch, breaker_engaged=False)
    (tmp_path / "object-store" / "runs" / "fcst_gfs_2026052100_model_a" / "input" / "manifest.json").unlink()

    slurm_forecast, trace, blocked = _drive_failing_reruns(tmp_path, root, _RETRY_LIMIT + 3)

    assert slurm_forecast == 1 + _RETRY_LIMIT, (slurm_forecast, trace)
    assert trace[0] == [("candidates", "retry_terminal_run_manifest_missing")], trace
    assert all(decisions == _BUDGET_EXHAUSTED for decisions in trace[1:]), trace
    assert all(reasons == ["retry_limit_exhausted"] for reasons in blocked[1:]), blocked


def test_failing_reruns_without_inline_retry_stay_within_the_forecast_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-E2: the #2401 loop with NO inline retry service; the scheduler owns every attempt.

    Same prefix throughout (``cycle_..._forecast_model_a``), so this is the
    bounded-by-construction shape: after the forced quarantine rerun fails, the
    failure lane's ``retry_failed`` restart (floor 0, not a forced-resubmit
    decision) finds that terminal failed row and resumes it instead of minting a
    new id -- no further Slurm forecast is ever submitted.  It does NOT reach a
    budget block: without an inline retry nothing charges a new attempt, so the
    candidate is re-selected as a no-op every pass.  Production's db-free
    scheduler always wires a ``FileJournalRetryService`` (the loop above).
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from tests.test_operator_reentry_confirmation import real_rerun, seed_breaker_journal

    root = seed_breaker_journal(tmp_path, monkeypatch, breaker_engaged=False)
    slurm_forecast = 0
    trace: list[list[tuple[str, str | None]]] = []
    for _ in range(_RETRY_LIMIT + 3):
        result, orchestrator = _pass(tmp_path, root)
        trace.append(_decisions(result))
        if not orchestrator.calls:
            continue
        (call,) = orchestrator.calls
        client = _wallclock_slurm_client(**_FORECAST_FAILURE)
        rerun = real_rerun(tmp_path, root, [dict(basin) for basin in call["basins"]], slurm_client=client)
        assert rerun.status == "failed"
        slurm_forecast += sum(1 for item in client.submissions if item.get("stage") == "forecast")

    assert slurm_forecast <= 1 + _RETRY_LIMIT, (slurm_forecast, trace)
    assert slurm_forecast == 1, trace
    assert trace[0] == [("candidates", "retry_journal_predecessor_identity_mismatch")], trace
    assert all(decisions == [("candidates", "retry_failed")] for decisions in trace[1:]), trace
    forecast_masters = [
        row["job_id"]
        for row in FileOrchestrationJournalRepository(root).query_pipeline_jobs_by_cycle("gfs_2026052100")
        if row.get("stage") == "forecast" and str(row["job_id"]).startswith("job_cycle_gfs_2026052100_forecast_")
    ]
    assert forecast_masters == ["job_cycle_gfs_2026052100_forecast_model_a_forecast"], forecast_masters


def test_mixed_outcome_cohort_splits_into_terminal_skip_and_the_failure_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-E1: one cohort array, model_a's task succeeds and model_b's fails.

    Each member's newest truth is its own reconciled task row: model_a's is a
    success (a terminal skip, neither ``None`` nor a retry), model_b's a failure
    newer than its old success (the ordinary failure path).
    """

    from tests.test_operator_reentry_confirmation import (
        breaker_scheduler,
        real_rerun,
        seed_breaker_journal,
        stale_token,
    )
    from tests.test_production_scheduler import FakeProductionOrchestrator

    models = ("model_a", "model_b")
    root = seed_breaker_journal(tmp_path, monkeypatch, model_ids=models, breaker_engaged=False)

    def _cohort_pass() -> tuple[Any, list[tuple[str, str | None, str | None, str | None]]]:
        orchestrator = FakeProductionOrchestrator()
        result = breaker_scheduler(tmp_path, root, orchestrator, model_ids=models, retry_limit=_RETRY_LIMIT).run_once()
        rows = [
            (key, item.get("model_id"), (item.get("state_evidence") or {}).get("decision"), item.get("reason"))
            for key in ("candidates", "blocked_candidates", "skipped_candidates")
            for item in result.evidence.get(key) or []
        ]
        return orchestrator, sorted(rows, key=lambda row: str(row[1]))

    orchestrator, rows = _cohort_pass()
    assert [row[2] for row in rows] == ["retry_journal_predecessor_identity_mismatch"] * 2
    (call,) = orchestrator.calls
    rerun = real_rerun(
        tmp_path,
        root,
        [dict(basin) for basin in call["basins"]],
        recorded_tokens={model_id: stale_token(model_id, lead_hours=6) for model_id in models},
        slurm_client=_wallclock_slurm_client(array_results_by_stage={"forecast": ["succeeded", "failed"]}),
    )
    assert rerun.status == "parsed_partial"

    _orchestrator, rows = _cohort_pass()
    assert rows == [
        ("skipped_candidates", "model_a", "skip_terminal", "terminal_hydro_success"),
        ("candidates", "model_b", "retry_failed", None),
    ]


def _completion_row(stage: str) -> dict[str, Any]:
    return {
        "job_id": f"job_fcst_gfs_2026052100_model_a_{stage}",
        "run_id": "fcst_gfs_2026052100_model_a",
        "cycle_id": "gfs_2026052100",
        "model_id": "model_a",
        "stage": stage,
        "job_type": {"parse": "parse_output_array", "publish": "publish"}[stage],
        "status": "succeeded",
        "retry_count": 0,
    }


@pytest.mark.parametrize("failure_is_newer", [True, False], ids=["newer_failure", "older_failure"])
def test_strict_warm_start_terminal_mismatch_leg_yields_to_a_newer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_is_newer: bool,
) -> None:
    """Strict lane, CONFLICT geometry; the terminal skip comes from the pipeline leg.

    The seeded ``hydro_run`` truth is older than every row written through the
    journal write path, so once a failure row exists the hydro leg is already
    non-terminal and only ``terminal_pipeline_success`` can feed the strict
    terminal mismatch rewrite.  Write order through ``upsert_pipeline_job``
    decides recency.
    """

    from tests.test_production_scheduler import (
        _budget_attempt_row,
        _budget_pass,
        _record_budget_attempt,
        _seed_budget_journal,
    )

    success = [_completion_row("parse"), _completion_row("publish")]
    failure = {**_budget_attempt_row(1, status="failed"), "error_code": "NODE_FAILURE"}
    root, scheduler = _seed_budget_journal(monkeypatch, tmp_path, [])
    for row in [*success, failure] if failure_is_newer else [failure, *success]:
        _record_budget_attempt(root, row)

    _selected, candidates, blocked, _skipped = _budget_pass(scheduler())

    assert blocked == []
    (candidate,) = candidates
    decision = candidate.state_evidence["decision"]
    if failure_is_newer:
        # The ordinary failure path (budget-read attempt 1 of 2), upgraded by the
        # strict lane's failed-retry manifest leg -- not the terminal mismatch leg.
        assert decision == "retry_strict_warm_start_retry_run_manifest_mismatch"
        assert candidate.state_evidence["retry_policy"]["attempt"] == 1
        assert candidate.state_evidence["retry_policy"]["retry_limit"] == 2
    else:
        # Old failure, newer success: still the terminal path.
        assert decision == "retry_strict_warm_start_terminal_init_state_mismatch"


# ---------------------------------------------------------------------------
# Decision-level rules
# ---------------------------------------------------------------------------


@pytest.fixture
def forcing_witness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The candidate's own forcing package, so the failure path is not a missing-forcing block."""

    from tests.test_production_scheduler import _seed_producer_forcing_sidecar

    object_store_root = tmp_path / "object-store"
    object_store_root.mkdir()
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_store_root))
    _seed_producer_forcing_sidecar(object_store_root, candidate=_candidate())


def _candidate() -> Any:
    from tests.test_production_scheduler import _scheduler_candidate_fixture

    return _scheduler_candidate_fixture()


def _identity() -> dict[str, str]:
    from tests.test_production_scheduler import _production_identity_fixture

    return _production_identity_fixture()


def _job(stage: str, status: str, updated_at: str | None) -> dict[str, Any]:
    identity = _identity()
    row: dict[str, Any] = {
        **identity,
        "job_id": f"job_{identity['run_id']}_{stage}_{status}",
        "stage": stage,
        "status": status,
        "error_code": "NODE_FAILURE" if status == "failed" else None,
    }
    if updated_at is not None:
        row["updated_at"] = updated_at
    return row


def _pipeline_state(success_at: str | None, failure_at: str) -> dict[str, Any]:
    failure = _job("forecast", "failed", failure_at)
    success = _job("publish", "succeeded", success_at)
    jobs = [failure, success] if success_at is None or success_at >= failure_at else [success, failure]
    return {
        **_identity(),
        "candidate_id": _candidate().candidate_id,
        "pipeline_status": jobs[-1]["status"],
        "pipeline_jobs": jobs,
    }


def _decide(state: dict[str, Any]) -> Any:
    decision = scheduler_module._candidate_state_decision(_candidate(), state)
    assert decision is not None
    return decision


_RECENCY_CASES = [
    ("2026-05-21T08:00:00Z", "2026-05-21T07:00:00Z", True),
    ("2026-05-21T07:00:00Z", "2026-05-21T07:00:00Z", True),
    (None, "2026-05-21T07:00:00Z", True),
    ("2026-05-21T07:00:00Z", "2026-05-21T08:00:00Z", False),
]
_RECENCY_IDS = ["older_failure", "tie", "success_without_timestamp", "newer_failure"]


@pytest.mark.usefixtures("forcing_witness")
@pytest.mark.parametrize(("success_at", "failure_at", "terminal"), _RECENCY_CASES, ids=_RECENCY_IDS)
def test_pipeline_leg_recency(success_at: str | None, failure_at: str, terminal: bool) -> None:
    expected = "terminal_pipeline_success" if terminal else "retry_failed_candidate"
    assert _decide(_pipeline_state(success_at, failure_at)).reason == expected


def _completed_cycle_state(copyback_at: str | None, failure_at: str) -> dict[str, Any]:
    candidate = _candidate()
    copyback: dict[str, Any] = {
        "stage": "copyback",
        "status": "succeeded",
        "source_id": "gfs",
        "cycle_id": candidate.cycle_id,
        "model_id": candidate.model_id,
        "run_id": candidate.run_id,
        "candidate_id": candidate.candidate_id,
    }
    if copyback_at is not None:
        copyback["updated_at"] = copyback_at
    return {
        **_identity(),
        "candidate_id": candidate.candidate_id,
        "pipeline_status": "failed",
        "forecast_cycle": {
            "cycle_id": candidate.cycle_id,
            "source_id": "gfs",
            "cycle_time": "2026-05-21T06:00:00Z",
            "status": "complete",
            # Cycle-scoped: deliberately newer than the failure, and NOT the success truth.
            "updated_at": "2026-05-21T09:00:00Z",
        },
        "copyback_evidence": copyback,
        "pipeline_jobs": [_job("forecast", "failed", failure_at)],
    }


@pytest.mark.usefixtures("forcing_witness")
@pytest.mark.parametrize(("copyback_at", "failure_at", "terminal"), _RECENCY_CASES, ids=_RECENCY_IDS)
def test_completed_cycle_leg_recency(copyback_at: str | None, failure_at: str, terminal: bool) -> None:
    expected = "terminal_completed_cycle" if terminal else "retry_failed_candidate"
    assert _decide(_completed_cycle_state(copyback_at, failure_at)).reason == expected


@pytest.mark.usefixtures("forcing_witness")
def test_completed_cycle_leg_without_row_time_falls_back_to_the_pipeline_success_time() -> None:
    state = _completed_cycle_state(None, "2026-05-21T08:00:00Z")
    state["pipeline_jobs"].append(_job("publish", "succeeded", "2026-05-21T07:00:00Z"))
    assert _decide(state).reason == "retry_failed_candidate"
    state["pipeline_jobs"][-1]["updated_at"] = "2026-05-21T09:00:00Z"
    assert _decide(state).reason == "terminal_completed_cycle"


@pytest.mark.usefixtures("forcing_witness")
def test_repaired_stage_failure_row_does_not_demote_a_completed_pipeline() -> None:
    state = _pipeline_state("2026-05-21T07:00:00Z", "2026-05-21T08:00:00Z")
    for job in state["pipeline_jobs"]:
        if job["status"] == "failed":
            job["repair_status"] = "repaired"
    state["pipeline_status"] = "failed"
    assert _decide(state).reason == "terminal_pipeline_success"


def _hydro_state(hydro_at: str, failure_at: str, failed_stage: str) -> dict[str, Any]:
    return {
        **_identity(),
        "candidate_id": _candidate().candidate_id,
        "hydro_status": "succeeded",
        "hydro_run": {**_identity(), "status": "succeeded", "updated_at": hydro_at},
        "pipeline_status": "failed",
        "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
        "pipeline_jobs": [_job(failed_stage, "failed", failure_at)],
    }


@pytest.mark.usefixtures("forcing_witness")
@pytest.mark.parametrize("hydro_at", ["2026-05-21T08:00:00Z", "2026-05-21T07:00:00Z"], ids=["newer", "tie"])
def test_terminal_hydro_success_is_unchanged(hydro_at: str) -> None:
    assert _decide(_hydro_state(hydro_at, "2026-05-21T07:00:00Z", "forecast")).reason == "terminal_hydro_success"


@pytest.mark.usefixtures("forcing_witness")
def test_newer_forecast_failure_does_not_reuse_an_older_durable_hydro_run() -> None:
    decision = _decide(_hydro_state("2026-05-21T07:00:00Z", "2026-05-21T08:00:00Z", "forecast"))
    assert decision.reason == "retry_failed_candidate"


@pytest.mark.usefixtures("forcing_witness")
def test_downstream_failure_after_durable_hydro_still_resumes_downstream() -> None:
    # The legitimate resume: SHUD output is durable and a LATER parse failed.
    decision = _decide(_hydro_state("2026-05-21T07:00:00Z", "2026-05-21T08:00:00Z", "parse"))
    assert decision.reason == "resume_downstream_after_durable_shud"
    assert decision.evidence["restart_stage"] == "parse"


@pytest.mark.usefixtures("forcing_witness")
def test_forecast_resume_with_an_untimed_hydro_run_keeps_the_pre_change_decision() -> None:
    state = _hydro_state("2026-05-21T07:00:00Z", "2026-05-21T08:00:00Z", "forecast")
    del state["hydro_run"]["updated_at"]
    decision = _decide(state)
    assert decision.reason == "resume_downstream_after_durable_shud"
    assert decision.evidence["restart_stage"] == "forecast"


# ---------------------------------------------------------------------------
# Permanence re-labels are not new failures (real write path)
# ---------------------------------------------------------------------------

_RERUN_MASTER = "job_cycle_gfs_2026052100_forecast_model_a_forecast"
_TERMINAL_REASONS = {"terminal_pipeline_success", "terminal_completed_cycle", "terminal_hydro_success"}


def _wallclock_slurm_client(**kwargs: Any) -> Any:
    """``FakeCycleSlurmClient`` on the journal's clock.

    The stock fake stamps ``submitted_at`` / ``finished_at`` from a fixed
    2026-05-01 base while the journal stamps ``updated_at`` / ``created_at``
    from the wall clock; recency across those two clocks means nothing.  Here
    every Slurm-side time is the wall clock at the moment it is observed.
    """

    from tests.test_orchestration_chain import FakeCycleSlurmClient

    def _now() -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    class _WallClockSlurmClient(FakeCycleSlurmClient):
        def _submit(self, stage: str, run_id: str, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            job = super()._submit(stage, run_id, model_id, payload)
            self.jobs[job["job_id"]]["submitted_at"] = job["submitted_at"] = _now()
            return job

        def get_job_status(self, job_id: str) -> dict[str, Any]:
            super().get_job_status(job_id)
            job = self.jobs[job_id]
            job["started_at"] = job["started_at"] and _now()
            if job["status"] in {"succeeded", "failed"}:
                job["finished_at"] = _now()
            return dict(job)

    return _WallClockSlurmClient(**kwargs)


def _raw_decision_reasons(tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    from tests.test_operator_reentry_confirmation import _breaker_candidate_decisions

    reasons: list[str | None] = []
    decide = scheduler_module._candidate_state_decision

    def _spy(candidate: Any, raw_state: Any) -> Any:
        decision = decide(candidate, raw_state)
        reasons.append(decision.reason if decision is not None else None)
        return decision

    with monkeypatch.context() as patch:
        patch.setattr(scheduler_module, "_candidate_state_decision", _spy)
        _breaker_candidate_decisions(tmp_path, root)
    return reasons


def _quarantine_basins(tmp_path: Path, root: Path) -> list[dict[str, Any]]:
    result, orchestrator = _pass(tmp_path, root)
    assert _decisions(result) == [("candidates", "retry_journal_predecessor_identity_mismatch")]
    (call,) = orchestrator.calls
    return [dict(basin) for basin in call["basins"]]


def _mark_rerun_master_permanently_failed(root: Path) -> dict[str, Any]:
    """The production decline path: ``FileJournalRetryService.mark_permanently_failed``."""

    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        FileOrchestrationJournalRepository,
    )

    repository = FileOrchestrationJournalRepository(root)
    before = repository.get_pipeline_job(_RERUN_MASTER)
    assert before is not None and before["status"] == "failed"
    FileJournalRetryService(repository).mark_permanently_failed(before)
    after = FileOrchestrationJournalRepository(root).get_pipeline_job(_RERUN_MASTER)
    assert after is not None and after["status"] == "permanently_failed"
    # The mark rewrote both mark-side times; only the submission time is the attempt's.
    assert after["updated_at"] > before["updated_at"]
    assert after["finished_at"] > before["finished_at"]
    assert after["submitted_at"] == before["submitted_at"]
    return after


def test_old_failure_marked_permanent_after_a_newer_success_stays_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_operator_reentry_confirmation import real_rerun, seed_breaker_journal

    root = seed_breaker_journal(tmp_path, monkeypatch, breaker_engaged=False)
    basins = _quarantine_basins(tmp_path, root)
    # Failure F, then a forced replacement that SUCCEEDS, then F is re-labelled.
    assert real_rerun(tmp_path, root, basins, slurm_client=_wallclock_slurm_client(**_FORECAST_FAILURE)).status == (
        "failed"
    )
    assert real_rerun(tmp_path, root, basins, slurm_client=_wallclock_slurm_client()).status == "complete"
    _mark_rerun_master_permanently_failed(root)

    reasons = _raw_decision_reasons(tmp_path, root, monkeypatch)

    assert reasons and set(reasons) <= _TERMINAL_REASONS, reasons


def test_failed_rerun_marked_permanent_after_the_success_stays_non_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_operator_reentry_confirmation import real_rerun, seed_breaker_journal

    root = seed_breaker_journal(tmp_path, monkeypatch, breaker_engaged=False)
    basins = _quarantine_basins(tmp_path, root)
    # #2401 shape: the rerun fails AFTER the success, its retry is declined, it is marked permanent.
    assert real_rerun(tmp_path, root, basins, slurm_client=_wallclock_slurm_client(**_FORECAST_FAILURE)).status == (
        "failed"
    )
    marked = _mark_rerun_master_permanently_failed(root)

    reasons = _raw_decision_reasons(tmp_path, root, monkeypatch)

    assert reasons and not set(reasons) & _TERMINAL_REASONS, (reasons, marked["submitted_at"])
