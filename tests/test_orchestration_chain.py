from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from services.orchestrator.chain import M3_STAGES, ForecastOrchestrator, OrchestratorConfig, OrchestratorError
from services.orchestrator.retry import RetryConfig


class FakeCycleSlurmClient:
    def __init__(
        self,
        *,
        fail_stage: str | None = None,
        never_terminal_stage: str | None = None,
        array_results_by_stage: dict[str, list[str] | list[list[str]]] | None = None,
        failures_before_success_by_stage: dict[str, int] | None = None,
        error_code_by_stage: dict[str, str] | None = None,
    ) -> None:
        self.fail_stage = fail_stage
        self.never_terminal_stage = never_terminal_stage
        self.array_results_by_stage = array_results_by_stage or {}
        self.failures_before_success_by_stage = failures_before_success_by_stage or {}
        self.error_code_by_stage = error_code_by_stage or {}
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
        elif self.never_terminal_stage == job["stage"]:
            job["status"] = "running"
        else:
            remaining_failures = self.failures_before_success_by_stage.get(job["stage"], 0)
            failed = self.fail_stage == job["stage"] or job["stage_attempt"] < remaining_failures
            job["status"] = "failed" if failed else "succeeded"
            job["finished_at"] = _fmt(submitted_at + timedelta(minutes=2))
            job["exit_code"] = 1 if failed else 0
            if failed:
                job["error_code"] = self.error_code_by_stage.get(job["stage"], "FORCED_FAILURE")
                job["error_message"] = "forced failure"
        return dict(job)

    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        job = self.jobs[job_id]
        statuses = self.array_results_by_stage.get(job["stage"])
        task_count = len(job["payload"].get("tasks") or [])
        if statuses is None:
            statuses = ["succeeded"] * task_count
        elif statuses and isinstance(statuses[0], list):
            statuses = statuses[min(job["stage_attempt"], len(statuses) - 1)]
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
        stage_attempt = sum(1 for job in self.jobs.values() if job["stage"] == stage)
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
            "stage_attempt": stage_attempt,
        }
        self.submissions.append(payload)
        self.jobs[job_id] = job
        self.poll_counts[job_id] = 0
        return dict(job)


class PublishFailureSlurmClient(FakeCycleSlurmClient):
    def get_job_status(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        if job["stage"] == "publish":
            self.poll_counts[job_id] += 1
            submitted_at = datetime.fromisoformat(job["submitted_at"].replace("Z", "+00:00"))
            job["status"] = "failed"
            job["finished_at"] = _fmt(submitted_at + timedelta(minutes=1))
            job["exit_code"] = 1
            job["error_code"] = "NO_PUBLISHABLE_PRODUCTS"
            job["error_message"] = "No publishable flood return-period products found for cycle_id=gfs_2026050100."
            return dict(job)
        return super().get_job_status(job_id)


class FakeCycleRepository:
    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.cycle_statuses: list[str] = []
        self.hydro_runs: dict[str, dict[str, Any]] = {}

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        del source_id, cycle_time
        return self.active

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

    def create_hydro_run_from_basin(self, basin: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        run_id = str(manifest["run_id"])
        existing = self.hydro_runs.get(run_id)
        if existing is not None and existing["status"] not in {"failed", "cancelled"}:
            return dict(existing)
        record = {
            "run_id": run_id,
            "status": "created",
            "model_id": manifest["model"]["model_id"],
            "basin_version_id": manifest["model"]["basin_version_id"],
            "source_id": manifest["source_id"],
            "cycle_time": manifest["cycle_time"],
            "run_manifest": dict(manifest),
            "basin": dict(basin),
        }
        self.hydro_runs[run_id] = record
        return dict(record)


class WriteFailingObjectStore(LocalObjectStore):
    def write_bytes_atomic(self, key_or_uri: str, content: bytes) -> str:
        if key_or_uri.endswith("/input/manifest.json"):
            raise OSError("permission denied")
        return super().write_bytes_atomic(key_or_uri, content)


class InvalidJsonForecastOrchestrator(ForecastOrchestrator):
    def _validate_forecast_runtime_manifest(
        self,
        manifest_path: Path,
        manifest: dict[str, Any],
        *,
        task_index: int,
    ) -> None:
        manifest_path.write_text("{bad json", encoding="utf-8")
        super()._validate_forecast_runtime_manifest(manifest_path, manifest, task_index=task_index)


class MissingManifestForecastOrchestrator(ForecastOrchestrator):
    def _validate_forecast_runtime_manifest(
        self,
        manifest_path: Path,
        manifest: dict[str, Any],
        *,
        task_index: int,
    ) -> None:
        manifest_path.unlink(missing_ok=True)
        super()._validate_forecast_runtime_manifest(manifest_path, manifest, task_index=task_index)


class UnreadableManifestForecastOrchestrator(ForecastOrchestrator):
    def _validate_forecast_runtime_manifest(
        self,
        manifest_path: Path,
        manifest: dict[str, Any],
        *,
        task_index: int,
    ) -> None:
        raise OrchestratorError(
            "RUNTIME_MANIFEST_READ_FAILED",
            f"Forecast runtime manifest cannot be read for task {task_index}: permission denied",
            {"manifest_path": str(manifest_path), "task_id": task_index},
        )


class FakeRetryService:
    def __init__(self, *, max_retries: int = 1) -> None:
        self.config = RetryConfig(max_retries=max_retries, backoff_schedule=[0])
        self.retry_counts: dict[str, int] = {}
        self.handled_job_ids: list[str] = []

    def handle_failed_job(self, job: Any) -> Any:
        self.handled_job_ids.append(job.job_id)
        retry_count = self.retry_counts.get(job.job_id, int(getattr(job, "retry_count", 0) or 0))
        if retry_count >= self.config.max_retries:
            job.status = "permanently_failed"
            return job
        retry_count += 1
        self.retry_counts[job.job_id] = retry_count
        job.retry_count = retry_count
        job.status = "pending"
        return job


def test_m3_cycle_orchestration_submits_all_seven_stages_lazily(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    assert result.status == "complete"
    assert [submission["stage"] for submission in client.submissions] == [stage.stage for stage in M3_STAGES]
    assert [stage.status for stage in result.stages] == ["succeeded"] * 7
    assert repository.cycle_statuses[-1] == "complete"
    assert {job["status"] for job in repository.jobs.values()} == {"succeeded"}


def test_forecast_stage_writes_runtime_manifests_and_manifest_index_paths(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    assert result.status == "complete"
    assert set(repository.hydro_runs) == {"run_0", "run_1"}
    for task in forecast_submission["tasks"]:
        manifest_path = Path(task["manifest_path"])
        assert manifest_path == tmp_path / "workspace" / "runs" / task["run_id"] / "input" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_id"] == task["run_id"]
        assert manifest["run_type"] == "forecast"
        assert manifest["scenario_id"] == "forecast_gfs_deterministic"
        assert manifest["source_id"] == "gfs"
        assert manifest["start_time"] == "2026-05-01T00:00:00Z"
        assert manifest["end_time"] == "2026-05-08T00:00:00Z"
        assert manifest["model"]["model_id"] == task["model_id"]
        assert manifest["model"]["basin_version_id"] == task["basin_version_id"]
        assert manifest["model"]["river_network_version_id"] == task["river_network_version_id"]
        assert manifest["forcing"]["forcing_uri"]
        assert manifest["outputs"]["run_manifest_uri"].endswith(f"runs/{task['run_id']}/input/manifest.json")
        assert repository.hydro_runs[task["run_id"]]["status"] == "created"

    index_path = Path(forecast_submission["manifest"]["manifest_index_path"])
    index_entries = json.loads(index_path.read_text(encoding="utf-8"))
    assert [entry["manifest_path"] for entry in index_entries] == [
        str(tmp_path / "workspace" / "runs" / "run_0" / "input" / "manifest.json"),
        str(tmp_path / "workspace" / "runs" / "run_1" / "input" / "manifest.json"),
    ]


def test_hydro_run_creation_is_idempotent_for_existing_created_status() -> None:
    repository = FakeCycleRepository()
    basin = _basins(1)[0]
    manifest = {
        "run_id": "run_0",
        "source_id": "gfs",
        "cycle_time": "2026-05-01T00:00:00Z",
        "model": {
            "model_id": "model_0",
            "basin_version_id": "basin_v0",
            "river_network_version_id": "model_0_river",
        },
    }

    first = repository.create_hydro_run_from_basin(basin, manifest)
    second = repository.create_hydro_run_from_basin(basin, manifest)

    assert first["run_id"] == second["run_id"] == "run_0"
    assert len(repository.hydro_runs) == 1
    assert repository.hydro_runs["run_0"]["status"] == "created"


def test_cycle_orchestration_rejects_db_active_duplicate(tmp_path: Path) -> None:
    repository = FakeCycleRepository(active=True)
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert exc_info.value.error_code == "PIPELINE_ALREADY_ACTIVE"
    assert client.submissions == []


def test_cycle_orchestration_rejects_unsafe_source_and_basin_ids(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    with pytest.raises(OrchestratorError) as source_exc:
        orchestrator.orchestrate_cycle("../gfs", "2026050100", _basins(1))

    unsafe_basins = _basins(1)
    unsafe_basins[0]["basin_id"] = "../basin"
    with pytest.raises(OrchestratorError) as basin_exc:
        orchestrator.orchestrate_cycle("gfs", "2026050100", unsafe_basins)

    assert source_exc.value.error_code == "UNSAFE_IDENTIFIER"
    assert basin_exc.value.error_code == "UNSAFE_IDENTIFIER"
    assert client.submissions == []


def test_stage_three_failure_blocks_downstream_stages(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(fail_stage="forcing", array_results_by_stage={"forcing": ["failed", "failed"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert repository.cycle_statuses[-1] == "failed_forcing"


def test_forecast_manifest_write_failure_marks_pipeline_failed(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(
        tmp_path,
        repository,
        client,
        object_store=WriteFailingObjectStore(tmp_path / "object-store", "s3://nhms"),
    )

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    job = repository.jobs["job_cycle_gfs_2026050100_forecast"]
    assert job["status"] == "submission_failed"
    assert job["error_code"] == "RUNTIME_MANIFEST_WRITE_FAILED"
    assert repository.cycle_statuses[-1] == "failed_run"


def test_invalid_forecast_runtime_manifest_blocks_publish(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client, orchestrator_cls=InvalidJsonForecastOrchestrator)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert repository.jobs["job_cycle_gfs_2026050100_forecast"]["status"] == "submission_failed"
    assert "publish" not in [submission["stage"] for submission in client.submissions]
    assert repository.cycle_statuses[-1] == "failed_run"


def test_missing_forecast_runtime_manifest_blocks_publish(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client, orchestrator_cls=MissingManifestForecastOrchestrator)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert repository.jobs["job_cycle_gfs_2026050100_forecast"]["status"] == "submission_failed"
    assert "publish" not in [submission["stage"] for submission in client.submissions]
    assert repository.cycle_statuses[-1] == "failed_run"


def test_unreadable_forecast_runtime_manifest_blocks_publish(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client, orchestrator_cls=UnreadableManifestForecastOrchestrator)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    job = repository.jobs["job_cycle_gfs_2026050100_forecast"]
    assert job["status"] == "submission_failed"
    assert job["error_code"] == "RUNTIME_MANIFEST_READ_FAILED"
    assert "publish" not in [submission["stage"] for submission in client.submissions]
    assert repository.cycle_statuses[-1] == "failed_run"


def test_permanently_failed_stage_blocks_downstream_stages(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    run_id = "cycle_gfs_2026050100"
    repository.jobs[f"job_{run_id}_download"] = {
        "job_id": f"job_{run_id}_download",
        "run_id": run_id,
        "cycle_id": cycle_id,
        "job_type": "download_source_cycle",
        "slurm_job_id": "3001",
        "model_id": None,
        "status": "permanently_failed",
        "stage": "download",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": None,
        "finished_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "exit_code": 1,
        "error_code": "SLURM_TIMEOUT",
        "error_message": "retry budget exhausted",
        "log_uri": None,
    }
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [stage.status for stage in result.stages] == ["permanently_failed"]
    assert client.submissions == []
    assert repository.cycle_statuses[-1] == "failed_download"


def test_failed_stage_auto_retries_before_downstream_stages(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(
        failures_before_success_by_stage={"download": 1},
        error_code_by_stage={"download": "SLURM_TIMEOUT"},
    )
    retry_service = FakeRetryService(max_retries=1)
    orchestrator = _orchestrator(tmp_path, repository, client, retry_service=retry_service)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "complete"
    assert [submission["stage"] for submission in client.submissions[:3]] == ["download", "download", "convert"]
    assert [stage.status for stage in result.stages] == ["succeeded"] * 7
    assert retry_service.handled_job_ids == ["job_cycle_gfs_2026050100_download"]


def test_poll_timeout_persists_failed_job_and_is_retry_eligible(monkeypatch, tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(never_terminal_stage="download")
    retry_service = FakeRetryService(max_retries=0)
    orchestrator = _orchestrator(tmp_path, repository, client, retry_service=retry_service)
    orchestrator.config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=tmp_path / "object-store",
        object_store_prefix="s3://nhms",
        poll_interval_seconds=1,
        job_timeout_seconds=1,
    )
    monotonic_values = iter([0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr("services.orchestrator.chain.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("services.orchestrator.chain.time.sleep", lambda _seconds: None)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    timed_out_job = repository.jobs["job_cycle_gfs_2026050100_download"]
    assert result.status == "failed"
    assert timed_out_job["status"] == "failed"
    assert timed_out_job["error_code"] == "SLURM_JOB_TIMEOUT"
    assert retry_service.handled_job_ids == ["job_cycle_gfs_2026050100_download"]
    assert any(
        event["event_type"] == "timeout"
        and event["entity_id"] == "job_cycle_gfs_2026050100_download"
        and event["status_to"] == "failed"
        and event["details"]["error_code"] == "SLURM_JOB_TIMEOUT"
        for event in repository.events
    )


def test_partial_array_retry_only_resubmits_failed_basin_tasks(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(
        array_results_by_stage={"forcing": [["succeeded", "failed", "succeeded"], ["succeeded"]]}
    )
    retry_service = FakeRetryService(max_retries=1)
    orchestrator = _orchestrator(tmp_path, repository, client, retry_service=retry_service)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    forcing_submissions = [submission for submission in client.submissions if submission["stage"] == "forcing"]
    assert result.status == "complete"
    assert [task["model_id"] for task in forcing_submissions[0]["tasks"]] == ["model_0", "model_1", "model_2"]
    assert [task["model_id"] for task in forcing_submissions[1]["tasks"]] == ["model_1"]
    assert [submission["stage"] for submission in client.submissions].count("forcing") == 2
    assert client.submissions[4]["stage"] == "forecast"
    assert [task["model_id"] for task in client.submissions[4]["tasks"]] == ["model_0", "model_1", "model_2"]


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

    assert result.status == "complete"
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


def test_publish_stage_failure_maps_to_failed_publish_cycle_status(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = PublishFailureSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert result.stages[-1].stage == "publish"
    assert result.stages[-1].status == "failed"
    assert result.stages[-1].error_code == "NO_PUBLISHABLE_PRODUCTS"
    assert repository.cycle_statuses[-1] == "failed_publish"


def _orchestrator(
    tmp_path: Path,
    repository: FakeCycleRepository,
    client: FakeCycleSlurmClient,
    *,
    retry_service: Any | None = None,
    object_store: LocalObjectStore | None = None,
    orchestrator_cls: type[ForecastOrchestrator] = ForecastOrchestrator,
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
    return orchestrator_cls(
        config=config,
        repository=repository,
        slurm_client=client,
        object_store=object_store or LocalObjectStore(object_root, "s3://nhms"),
        retry_service=retry_service,
    )


def _basins(count: int) -> list[dict[str, Any]]:
    return [
        {
            "model_id": f"model_{index}",
            "basin_id": f"basin_{index}",
            "basin_version_id": f"basin_v{index}",
            "river_network_version_id": f"river_v{index}",
            "run_id": f"run_{index}",
        }
        for index in range(count)
    ]


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _fmt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
