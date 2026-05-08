from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore
from services.orchestrator.chain import M3_STAGES, ForecastOrchestrator, OrchestratorConfig


class FakeCycleSlurmClient:
    def __init__(
        self,
        *,
        fail_stage: str | None = None,
        array_results_by_stage: dict[str, list[str]] | None = None,
    ) -> None:
        self.fail_stage = fail_stage
        self.array_results_by_stage = array_results_by_stage or {}
        self.submissions: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.poll_counts: dict[str, int] = {}
        self.next_job = 2000

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._submit(payload["manifest"]["stage"], payload["run_id"], payload["model_id"], payload["manifest"])

    def submit_job_array(
        self,
        job_type: str,
        *,
        cycle_id: str,
        stage_name: str,
        tasks: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        del job_type, cycle_id
        payload = {"stage": stage_name, "tasks": tasks, "manifest": manifest}
        return self._submit(stage_name, manifest["run_id"], manifest["model_id"], payload)

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
                job["error_message"] = "forced failure"
        return dict(job)

    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        job = self.jobs[job_id]
        statuses = self.array_results_by_stage.get(job["stage"])
        task_count = len(job["payload"].get("tasks") or [])
        if statuses is None:
            statuses = ["succeeded"] * task_count
        return [
            {
                "task_id": index,
                "job_id": f"{job_id}_{index}",
                "status": status,
                "exit_code": 0 if status == "succeeded" else 1,
            }
            for index, status in enumerate(statuses)
        ]

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        return {"job_id": job_id, "run_id": self.jobs[job_id]["run_id"], "complete": True, "logs": "ok"}

    def _submit(self, stage: str, run_id: str, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        active = [job for job in self.jobs.values() if job["status"] not in {"succeeded", "failed", "cancelled"}]
        if active:
            raise AssertionError("orchestrator submitted a stage before the previous stage reached terminal status")
        self.next_job += 1
        job_id = str(self.next_job)
        submitted_at = _dt("2026-05-01T00:00:00Z") + timedelta(minutes=len(self.submissions) * 5)
        job = {
            "job_id": job_id,
            "run_id": run_id,
            "model_id": model_id,
            "stage": stage,
            "status": "submitted",
            "submitted_at": _fmt(submitted_at),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "error_code": None,
            "error_message": None,
            "payload": payload,
        }
        self.submissions.append(payload)
        self.jobs[job_id] = job
        self.poll_counts[job_id] = 0
        return dict(job)


class FakeCycleRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.cycle_statuses: list[str] = []

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        return {"cycle_id": f"{source_id}_{cycle_time:%Y%m%d%H}", "status": "discovered"}

    def update_forecast_cycle_status(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        del source_id, cycle_time
        self.cycle_statuses.append(status)
        return {"status": status, "error_code": error_code, "error_message": error_message}

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
        log_uri: str | None = None,
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
                "log_uri": log_uri or self.jobs[job_id].get("log_uri"),
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

    def query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        return sorted(
            [dict(job) for job in self.jobs.values() if job["cycle_id"] == cycle_id],
            key=lambda job: str(job["submitted_at"]),
        )


def test_m3_cycle_orchestration_submits_all_seven_stages_lazily(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    assert result.status == "published"
    assert [submission["stage"] for submission in client.submissions] == [stage.stage for stage in M3_STAGES]
    assert [stage.status for stage in result.stages] == ["succeeded"] * 7
    assert repository.cycle_statuses[-1] == "published"
    assert {job["status"] for job in repository.jobs.values()} == {"succeeded"}


def test_stage_three_failure_blocks_downstream_stages(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(fail_stage="forcing", array_results_by_stage={"forcing": ["failed", "failed"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert repository.cycle_statuses[-1] == "failed_forcing"


def test_crash_recovery_resumes_after_last_completed_stage(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    run_id = "cycle_gfs_2026050100"
    for index, stage in enumerate(M3_STAGES[:4]):
        repository.jobs[f"job_{run_id}_{stage.stage}"] = {
            "job_id": f"job_{run_id}_{stage.stage}",
            "run_id": run_id,
            "cycle_id": cycle_id,
            "job_type": stage.job_type,
            "slurm_job_id": str(3000 + index),
            "model_id": None,
            "status": "succeeded",
            "stage": stage.stage,
            "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z") + timedelta(minutes=index)),
            "started_at": None,
            "finished_at": None,
            "exit_code": 0,
            "error_code": None,
            "error_message": None,
            "log_uri": None,
        }
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "published"
    assert [submission["stage"] for submission in client.submissions] == ["parse", "frequency", "publish"]


def test_partial_success_reindexes_downstream_and_keeps_partial_status(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["succeeded", "failed", "succeeded"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    forecast_submission = client.submissions[3]
    publish_submission = client.submissions[-1]
    assert result.status == "parsed_partial"
    assert [(task["task_id"], task["original_task_id"], task["model_id"]) for task in forecast_submission["tasks"]] == [
        (0, 0, "model_0"),
        (1, 2, "model_2"),
    ]
    assert publish_submission["metadata"] == {
        "total_basins": 3,
        "published_basins": 2,
        "excluded_basins": ["basin_1"],
    }
    assert repository.cycle_statuses[-1] == "parsed_partial"


def _orchestrator(
    tmp_path: Path,
    repository: FakeCycleRepository,
    client: FakeCycleSlurmClient,
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


def _basins(count: int) -> list[dict[str, Any]]:
    return [
        {
            "model_id": f"model_{index}",
            "basin_id": f"basin_{index}",
            "basin_version_id": f"basin_v{index}",
            "run_id": f"run_{index}",
        }
        for index in range(count)
    ]


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _fmt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
