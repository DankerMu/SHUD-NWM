from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from services.orchestrator.chain import (
    STAGES,
    ForcingContext,
    ForecastOrchestrator,
    ModelContext,
    OrchestratorConfig,
    PipelineAlreadyActiveError,
)


class FakeSlurmClient:
    def __init__(self, *, fail_stage: str | None = None) -> None:
        self.fail_stage = fail_stage
        self.submissions: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.poll_counts: dict[str, int] = {}
        self.next_job = 1000

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        active = [job for job in self.jobs.values() if job["status"] not in {"succeeded", "failed", "cancelled"}]
        if active:
            raise AssertionError("orchestrator submitted a stage before the previous stage reached terminal status")
        self.next_job += 1
        job_id = f"mock_{self.next_job}"
        stage = payload["manifest"]["stage"]
        submitted_at = _dt("2026-05-01T00:00:00Z") + timedelta(minutes=len(self.submissions) * 3)
        job = {
            "job_id": job_id,
            "run_id": payload["run_id"],
            "model_id": payload["model_id"],
            "stage": stage,
            "status": "submitted",
            "submitted_at": _fmt(submitted_at),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "error_code": None,
            "error_message": None,
        }
        self.submissions.append(payload)
        self.jobs[job_id] = job
        self.poll_counts[job_id] = 0
        return dict(job)

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        self.poll_counts[job_id] += 1
        count = self.poll_counts[job_id]
        submitted_at = datetime.fromisoformat(job["submitted_at"].replace("Z", "+00:00"))
        if count == 1:
            job["status"] = "running"
            job["started_at"] = _fmt(submitted_at + timedelta(minutes=1))
        else:
            failed = self.fail_stage == job["stage"]
            job["status"] = "failed" if failed else "succeeded"
            job["finished_at"] = _fmt(submitted_at + timedelta(minutes=2))
            job["exit_code"] = 1 if failed else 0
            if failed:
                job["error_code"] = "FORCED_FAILURE"
                job["error_message"] = "forced test failure"
        return dict(job)

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        return {"job_id": job_id, "run_id": job["run_id"], "complete": True, "logs": f"log for {job['stage']}"}


class FakeOrchestratorRepository:
    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.model = ModelContext(
            model_id="demo_model",
            basin_id="yangtze",
            basin_version_id="basin_v1",
            river_network_version_id="rivnet_v1",
            segment_count=2,
            model_package_uri="models/demo_model/package/",
        )
        self.forcing = ForcingContext(
            forcing_version_id="forc_gfs_2026050100_demo_model",
            forcing_package_uri="forcing/gfs/2026050100/basin_v1/demo_model/",
        )
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.cycle_statuses: list[str] = []
        self.hydro_statuses: list[str] = []
        self.created_runs: list[Any] = []

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        assert source_id == "gfs"
        assert cycle_time == _dt("2026-05-01T00:00:00Z")
        assert model_id == "demo_model"
        return self.active

    def load_model_context(self, model_id: str) -> ModelContext:
        assert model_id == "demo_model"
        return self.model

    def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
        assert source_id == "gfs"
        assert cycle_time == _dt("2026-05-01T00:00:00Z")
        assert model_id == "demo_model"
        return self.forcing

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        assert source_id == "gfs"
        return {"cycle_id": "gfs_2026050100", "cycle_time": cycle_time}

    def create_hydro_run(self, context: Any, manifest: dict[str, Any]) -> dict[str, Any]:
        self.created_runs.append((context, manifest))
        self.hydro_statuses.append("created")
        return {"run_id": context.run_id, "status": "created"}

    def update_hydro_run_status(
        self,
        run_id: str,
        status: str,
        *,
        slurm_job_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        assert run_id == "fcst_gfs_2026050100_demo_model"
        self.hydro_statuses.append(status)
        return {
            "run_id": run_id,
            "status": status,
            "slurm_job_id": slurm_job_id,
            "error_code": error_code,
            "error_message": error_message,
        }

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        self.jobs[record["job_id"]] = dict(record)
        return dict(record)

    def update_pipeline_job_status(
        self,
        job_id: str,
        status: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        previous = self.jobs[job_id]["status"]
        self.jobs[job_id].update(
            {
                "status": status,
                "started_at": started_at or self.jobs[job_id].get("started_at"),
                "finished_at": finished_at or self.jobs[job_id].get("finished_at"),
                "exit_code": exit_code,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        return previous, dict(self.jobs[job_id])

    def insert_pipeline_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "message": message,
            "details": details or {},
        }
        self.events.append(event)
        return event

    def update_forecast_cycle_status(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        assert source_id == "gfs"
        assert cycle_time == _dt("2026-05-01T00:00:00Z")
        self.cycle_statuses.append(status)
        return {"status": status, "error_code": error_code, "error_message": error_message}

    def list_stage_statuses(
        self,
        *,
        source_id: str | None,
        cycle_time: datetime,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        assert source_id == "gfs"
        assert cycle_time == _dt("2026-05-01T00:00:00Z")
        assert model_id == "demo_model"
        order = {stage.stage: index for index, stage in enumerate(STAGES)}
        return sorted(self.jobs.values(), key=lambda job: order[job["stage"]])


def test_lazy_chain_submits_all_stages_and_records_statuses(tmp_path: Path) -> None:
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    orchestrator = _build_orchestrator(tmp_path, repository, client)

    result = orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert result.status == "parsed"
    assert [payload["manifest"]["stage"] for payload in client.submissions] == [stage.stage for stage in STAGES]
    assert all(stage.status == "succeeded" for stage in result.stages)
    assert repository.hydro_statuses == ["created", "staged", "submitted", "succeeded", "parsed"]
    assert repository.cycle_statuses[-1] == "complete"
    assert {job["status"] for job in repository.jobs.values()} == {"succeeded"}
    assert len(repository.events) == 15
    run_manifest = tmp_path / "workspace" / "runs" / "fcst_gfs_2026050100_demo_model" / "input" / "manifest.json"
    assert run_manifest.exists()
    assert '"run_id": "fcst_gfs_2026050100_demo_model"' in run_manifest.read_text(encoding="utf-8")


def test_stage_failure_aborts_later_submissions_and_marks_run_failed(tmp_path: Path) -> None:
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient(fail_stage="convert_canonical")
    orchestrator = _build_orchestrator(tmp_path, repository, client)

    result = orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert result.status == "failed"
    assert [payload["manifest"]["stage"] for payload in client.submissions] == ["download_gfs", "convert_canonical"]
    assert repository.hydro_statuses[-1] == "failed"
    assert repository.cycle_statuses[-1] == "failed_convert"
    failed_events = [event for event in repository.events if event["status_to"] == "failed"]
    assert failed_events
    assert "FORCED_FAILURE" in failed_events[-1]["message"]


def test_duplicate_trigger_is_rejected_before_submission(tmp_path: Path) -> None:
    repository = FakeOrchestratorRepository(active=True)
    client = FakeSlurmClient()
    orchestrator = _build_orchestrator(tmp_path, repository, client)

    with pytest.raises(PipelineAlreadyActiveError):
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert client.submissions == []
    assert repository.created_runs == []


def test_stage_status_query_returns_ordered_stage_records(tmp_path: Path) -> None:
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    orchestrator = _build_orchestrator(tmp_path, repository, client)
    orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    statuses = orchestrator.stage_statuses(cycle_time="2026050100", source_id="gfs", model_id="demo_model")

    assert [status["stage"] for status in statuses] == [stage.stage for stage in STAGES]
    assert [status["status"] for status in statuses] == ["succeeded"] * 5


def test_sbatch_templates_are_five_linear_non_array_scripts() -> None:
    template_dir = Path("workers/sbatch_templates")
    names = sorted(path.name for path in template_dir.glob("*.sbatch"))

    assert names == sorted(stage.template_name for stage in STAGES)
    for path in template_dir.glob("*.sbatch"):
        content = path.read_text(encoding="utf-8")
        assert content.startswith("#!/bin/bash")
        assert "#SBATCH --job-name=" in content
        assert "#SBATCH --output=" in content
        assert "--dependency=afterok" not in content
        assert "#SBATCH --array" not in content


def _build_orchestrator(
    tmp_path: Path,
    repository: FakeOrchestratorRepository,
    client: FakeSlurmClient,
) -> ForecastOrchestrator:
    workspace = tmp_path / "workspace"
    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=workspace,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        poll_interval_seconds=0,
        job_timeout_seconds=5,
    )
    return ForecastOrchestrator(
        config=config,
        repository=repository,
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _fmt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
