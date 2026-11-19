"""Pin the scheduler pass status for passes that never reach Slurm dispatch.

Both tests run the real ``ProductionScheduler.run_once()`` and assert on the
producer-emitted pass evidence, pinning two branches of
``services.orchestrator.scheduler_candidate_runtime._scheduler_pass_status_from_execution``:

1. a zero-dispatch ``reconciling`` pass falls through to the last-item status
   passthrough and reports ``reconciling`` (not ``submitted_partial``/``planned``);
2. a pass where every candidate failed before any Slurm job id was assigned
   takes the ``any blocked/failed`` branch and reports ``preflight_blocked``.

Re-homed by #3221 from the deleted readiness-lane test file, whose fixture
helpers were the only tests of these branches.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.contracts.time_normalization import cycle_id_for, format_cycle_time
from services.orchestrator.chain import PipelineResult, StageRunResult
from services.orchestrator.scheduler import ProductionScheduler
from tests.test_production_scheduler import (
    FakeAdapter,
    FakeProductionOrchestrator,
    FakeRegistry,
)
from tests.test_production_scheduler import (
    _AlwaysReadyCanonicalReadinessProvider as ReadyCanonicalReadinessProvider,
)
from tests.test_production_scheduler import (
    _config as _scheduler_config,
)
from tests.test_production_scheduler import (
    _dt as _scheduler_dt,
)
from tests.test_production_scheduler import (
    _model as _scheduler_model,
)


class _ZeroSubmissionReconcilingOrchestrator(FakeProductionOrchestrator):
    """Cycle result: a ``reconciling`` terminal with no Slurm dispatch anywhere.

    The cycle result carries no ``slurm_job_id`` on any stage or candidate
    outcome, so the real ``ProductionScheduler`` producer emits pass status
    ``reconciling``, ``submitted_count`` zero, and one reconciliation-pending
    model-run row that was execution-attempted but never submitted.
    """

    def orchestrate_cycle(
        self,
        source: str,
        cycle_time: datetime,
        basins: list[dict[str, Any]],
    ) -> PipelineResult:
        self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
        return PipelineResult(
            run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
            cycle_id=cycle_id_for(source, cycle_time),
            status="reconciling",
            stages=(
                StageRunResult(
                    stage="forecast",
                    job_type="run_shud_forecast_array",
                    pipeline_job_id="job_cycle_gfs_2026052106_forecast",
                    slurm_job_id="",
                    status="reconciling",
                ),
            ),
            candidate_outcomes=(
                {
                    "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "model_id": "model_a",
                    "status": "active",
                    "stage": "forecast",
                },
            ),
        )


class NoSubmissionFailedOrchestrator(FakeProductionOrchestrator):
    def orchestrate_cycle(
        self,
        source: str,
        cycle_time: datetime,
        basins: list[dict[str, object]],
    ) -> PipelineResult:
        self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
        candidate_outcomes = tuple(
            {
                "candidate_id": basin["candidate_id"],
                "run_id": basin["run_id"],
                "model_id": basin["model_id"],
                "status": "failed",
                "stage": "forcing",
                "reason": "submission_failed",
                "accounting": {},
            }
            for basin in basins
        )
        return PipelineResult(
            run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
            cycle_id=cycle_id_for(source, cycle_time),
            status="failed",
            stages=(
                StageRunResult(
                    stage="forcing",
                    job_type="produce_forcing_array",
                    pipeline_job_id="job_forcing",
                    slurm_job_id="",
                    status="submission_failed",
                    error_code="SBATCH_SUBMISSION_FAILED",
                    error_message="sbatch submission failed before a Slurm job id was assigned",
                ),
            ),
            candidate_outcomes=candidate_outcomes,
        )


def test_zero_dispatch_reconciling_pass_reports_reconciling_with_no_submission(tmp_path: Path) -> None:
    orchestrator = _ZeroSubmissionReconcilingOrchestrator()
    scheduler = ProductionScheduler(
        _scheduler_config(
            tmp_path / "scheduler-workspace",
            now=_scheduler_dt("2026-05-21T12:00:00Z"),
            dry_run=False,
        ),
        registry=FakeRegistry([_scheduler_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        canonical_readiness_provider=ReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "reconciling"
    assert result.evidence["status"] == "reconciling"
    assert result.evidence["counts"]["candidate_count"] == 1
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["counts"]["failed_count"] == 0
    assert result.evidence["counts"]["partial_count"] == 1
    assert len(result.evidence["model_run_evidence"]) == 1
    row = result.evidence["model_run_evidence"][0]
    assert row["status"] == "reconciling"
    assert row["submitted"] is False
    assert row["execution_attempted"] is True
    assert row["final_candidate_success"] is False
    assert row["candidate_outcome"]["status"] == "active"
    assert result.artifact_path is not None


def test_all_candidates_failed_before_slurm_reports_preflight_blocked(tmp_path: Path) -> None:
    orchestrator = NoSubmissionFailedOrchestrator()
    scheduler = ProductionScheduler(
        _scheduler_config(
            tmp_path / "scheduler-workspace-no-submission-failed",
            now=_scheduler_dt("2026-05-21T12:00:00Z"),
            dry_run=False,
        ),
        registry=FakeRegistry([_scheduler_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        canonical_readiness_provider=ReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert len(orchestrator.calls) == 1
    assert result.status == "preflight_blocked"
    assert result.evidence["status"] == "preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["counts"]["failed_count"] == 1
    assert result.evidence["counts"]["partial_count"] == 1
    model_run = result.evidence["model_run_evidence"][0]
    assert model_run["status"] == "failed"
    assert model_run["submitted"] is False
    assert model_run["execution_attempted"] is True
    assert model_run["stage_statuses"][0]["status"] == "submission_failed"
    assert result.artifact_path is not None
