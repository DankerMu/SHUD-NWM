from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from services.orchestrator.chain import (
    M3_STAGES,
    ForecastOrchestrator,
    OrchestratorConfig,
    OrchestratorError,
    PsycopgOrchestratorRepository,
)
from services.orchestrator.retry import RetryConfig
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES


class FakeCycleSlurmClient:
    def __init__(
        self,
        *,
        fail_stage: str | None = None,
        never_terminal_stage: str | None = None,
        array_results_by_stage: dict[str, list[str] | list[list[str]]] | None = None,
        failures_before_success_by_stage: dict[str, int] | None = None,
        error_code_by_stage: dict[str, str] | None = None,
        accounting_by_stage: dict[str, dict[str, str]] | None = None,
        malformed_array_accounting_stages: set[str] | None = None,
    ) -> None:
        self.fail_stage = fail_stage
        self.never_terminal_stage = never_terminal_stage
        self.array_results_by_stage = array_results_by_stage or {}
        self.failures_before_success_by_stage = failures_before_success_by_stage or {}
        self.error_code_by_stage = error_code_by_stage or {}
        self.accounting_by_stage = accounting_by_stage or {}
        self.malformed_array_accounting_stages = malformed_array_accounting_stages or set()
        self.submissions: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.poll_counts: dict[str, int] = {}
        self.cancelled_jobs: list[str] = []
        self.next_job = 2000
        self.fail_next_array_submission_stage: str | None = None

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
        if self.fail_next_array_submission_stage == stage_name:
            raise RuntimeError(f"{stage_name} submission failed")
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
            job.update(self.accounting_by_stage.get(job["stage"], {}))
            if failed:
                job["error_code"] = self.error_code_by_stage.get(job["stage"], "FORCED_FAILURE")
                job["error_message"] = "forced failure"
        return dict(job)

    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        job = self.jobs[job_id]
        if job["stage"] in self.malformed_array_accounting_stages:
            return [{"task_id": "not-an-int", "status": "succeeded", "exit_code": 0}]
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
                "log_uri": f"s3://nhms/runs/{job['run_id']}/logs/{job_id}_{index}.out",
                "accounting": {
                    "elapsed": f"00:0{index + 1}:00",
                    "max_rss": f"{index + 1}024K",
                    "alloc_tres": "cpu=1,mem=2G",
                },
            }
            for index, status in enumerate(statuses)
        ]

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        return {"job_id": job_id, "run_id": self.jobs[job_id]["run_id"], "complete": True, "logs": "ok"}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        self.cancelled_jobs.append(job_id)
        job["status"] = "cancelled"
        job["exit_code"] = -1
        job["finished_at"] = _fmt(_dt("2026-05-01T00:30:00Z"))
        return dict(job)

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


class SubmitJobOnlyCycleSlurmClient:
    def __init__(self) -> None:
        self._delegate = FakeCycleSlurmClient()
        self.submissions = self._delegate.submissions
        self.submit_job_payloads: list[dict[str, Any]] = []

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.submit_job_payloads.append(payload)
        return self._delegate.submit_job(payload)

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        return self._delegate.get_job_status(job_id)

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        return self._delegate.fetch_logs(job_id)


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

    def update_hydro_run_status(
        self,
        run_id: str,
        status: str,
        *,
        slurm_job_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        record = self.hydro_runs[run_id]
        record["status"] = status
        if slurm_job_id is not None:
            record["slurm_job_id"] = slurm_job_id
        if error_code is not None:
            record["error_code"] = error_code
        if error_message is not None:
            record["error_message"] = error_message
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


def test_non_array_stage_submissions_carry_slurm_template_and_env_contract(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(
        tmp_path,
        repository,
        client,
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        slurm_env={"NHMS_PROFILE": "prod/gfs_00", "NHMS_RUN_LABEL": "prod_gfs_00"},
    )

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "complete"
    non_array_submissions = {
        submission["stage"]: submission
        for submission in client.submissions
        if submission["stage"] in {"download", "convert", "publish"}
    }
    assert set(non_array_submissions) == {"download", "convert", "publish"}
    for submission in non_array_submissions.values():
        assert submission["slurm_job_type_templates"] == dict(DEFAULT_JOB_TYPE_TEMPLATES)
        assert submission["slurm_env"] == {
            "NHMS_PROFILE": "prod/gfs_00",
            "NHMS_RUN_LABEL": "prod_gfs_00",
        }


def test_array_pipeline_jobs_are_persisted_as_cycle_level_rows(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "complete"
    for stage in ("forcing", "forecast", "parse", "frequency"):
        job = repository.jobs[f"job_cycle_gfs_2026050100_{stage}"]
        assert job["run_id"] == "cycle_gfs_2026050100"
        assert job["model_id"] is None


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


def test_forecast_runtime_manifest_write_rejects_symlink_target_without_submission(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    workspace = tmp_path / "workspace"
    target = workspace / "runs" / "run_1" / "input" / "manifest.json"
    target.parent.mkdir(parents=True)
    target.write_text("untouched", encoding="utf-8")
    link = workspace / "runs" / "run_0" / "input" / "manifest.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert target.read_text(encoding="utf-8") == "untouched"
    forecast_job = repository.jobs["job_cycle_gfs_2026050100_forecast"]
    assert forecast_job["status"] == "submission_failed"
    assert forecast_job["error_code"] == "RUNTIME_MANIFEST_WRITE_FAILED"


def test_cycle_manifest_index_write_rejects_symlink_target_without_stage_submission(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    workspace = tmp_path / "workspace"
    target = workspace / "runs" / "sibling" / "input" / "forcing_manifest_index.json"
    target.parent.mkdir(parents=True)
    target.write_text("untouched", encoding="utf-8")
    link = workspace / "runs" / "cycle_gfs_2026050100" / "input" / "forcing_manifest_index.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert"]
    assert target.read_text(encoding="utf-8") == "untouched"
    forcing_job = repository.jobs["job_cycle_gfs_2026050100_forcing"]
    assert forcing_job["status"] == "submission_failed"
    assert forcing_job["error_code"] == "CYCLE_MANIFEST_INDEX_WRITE_FAILED"


def test_cycle_manifest_index_rejects_task_count_over_limit_before_stage_submission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.common import manifest_index as manifest_index_module

    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_ENTRIES", 1)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert"]
    index_path = tmp_path / "workspace" / "runs" / "cycle_gfs_2026050100" / "input" / "forcing_manifest_index.json"
    assert not index_path.exists()
    forcing_job = repository.jobs["job_cycle_gfs_2026050100_forcing"]
    assert forcing_job["status"] == "submission_failed"
    assert forcing_job["error_code"] == "CYCLE_MANIFEST_INDEX_INVALID"


def test_cycle_manifest_index_rejects_serialized_size_over_limit_before_stage_submission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.common import manifest_index as manifest_index_module

    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_BYTES", 32)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert"]
    index_path = tmp_path / "workspace" / "runs" / "cycle_gfs_2026050100" / "input" / "forcing_manifest_index.json"
    assert not index_path.exists()
    forcing_job = repository.jobs["job_cycle_gfs_2026050100_forcing"]
    assert forcing_job["status"] == "submission_failed"
    assert forcing_job["error_code"] == "CYCLE_MANIFEST_INDEX_INVALID"


def test_model_run_identity_and_quality_contracts_propagate_to_worker_manifests(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "run_id": "fcst_gfs_2026050100_model_0",
        "forcing_version_id": "forc_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "station_count": 2,
        "segment_count": 3,
        "station_ids": ["sta_001", "sta_002"],
        "frequency_capabilities": {
            "return_periods": True,
            "curves_available": False,
            "warning_thresholds_available": False,
        },
        "display_capabilities": {"tiles": True, "optional_weather_available": False},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    parse_submission = next(submission for submission in client.submissions if submission["stage"] == "parse")
    frequency_submission = next(submission for submission in client.submissions if submission["stage"] == "frequency")
    publish_submission = next(submission for submission in client.submissions if submission["stage"] == "publish")
    task = forecast_submission["tasks"][0]
    runtime_manifest = json.loads(Path(task["manifest_path"]).read_text(encoding="utf-8"))

    assert result.status == "complete"
    assert runtime_manifest["identity"]["candidate_id"] == basin["candidate_id"]
    assert runtime_manifest["identity"]["run_id"] == basin["run_id"]
    assert runtime_manifest["identity"]["forcing_version_id"] == basin["forcing_version_id"]
    assert runtime_manifest["model"]["model_package_uri"] == basin["model_package_uri"]
    assert runtime_manifest["model"]["model_package_manifest_uri"] == "s3://nhms/models/model_0/v1/manifest.json"
    assert runtime_manifest["forcing"]["station_count"] == 2
    assert runtime_manifest["runtime"]["mode"] == "native_shud_project"
    assert runtime_manifest["runtime"]["output_river"]["river_network_version_id"] == "river_v0"
    assert runtime_manifest["outputs"]["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/"
    assert runtime_manifest["quality_states"]["frequency"]["unavailable_products"] == [
        "frequency_curves",
        "warning_thresholds",
    ]
    assert runtime_manifest["quality_states"]["display"]["unavailable_products"] == ["optional_weather_products"]
    assert {blocker["code"] for blocker in runtime_manifest["residual_blockers"]} == {
        "FREQUENCY_CURVES_UNAVAILABLE",
        "WARNING_THRESHOLDS_UNAVAILABLE",
        "OPTIONAL_WEATHER_PRODUCTS_UNAVAILABLE",
    }
    parse_entry = parse_submission["tasks"][0]
    frequency_entry = frequency_submission["tasks"][0]
    assert parse_entry["model_run_assembly"]["identity"]["candidate_id"] == basin["candidate_id"]
    assert frequency_entry["model_run_assembly"]["identity"]["run_id"] == basin["run_id"]
    assert frequency_submission["manifest"]["quality_states"][0]["quality_flag"] == "frequency_inputs_unavailable"
    assert publish_submission["metadata"]["residual_blockers"]
    assert publish_submission["metadata"]["quality_states"][0]["run_id"] == basin["run_id"]


def test_nested_forcing_station_metadata_reaches_runtime_manifest(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "resource_profile": {
            "forcing_station_metadata": {
                "station_count": 386,
                "station_ids": ["sta_001", "sta_002"],
                "source": "qhh_package_manifest",
                "quality_flag": "ok",
                "shud_station": "qhh.tsd.forc",
            }
        },
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    runtime_manifest = json.loads(Path(forecast_submission["tasks"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["forcing"]["station_metadata"] == {
        "schema_version": "nhms.forcing_station_metadata.v1",
        "state": "ready",
        "station_count": 386,
        "station_ids": ["sta_001", "sta_002"],
        "source": "qhh_package_manifest",
        "shud_station": "qhh.tsd.forc",
        "quality_flag": "ok",
    }
    assert runtime_manifest["forcing"]["shud_station"] == "qhh.tsd.forc"


def test_missing_station_forcing_is_quality_state_without_discarding_output_uri(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True, "optional_weather_available": False},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    publish_submission = next(submission for submission in client.submissions if submission["stage"] == "publish")
    quality = publish_submission["metadata"]["quality_states"][0]
    blockers = publish_submission["metadata"]["residual_blockers"]
    assert result.status == "complete"
    assert quality["quality_states"]["station_forcing"] == {
        "state": "unavailable",
        "quality_flag": "station_forcing_unavailable",
    }
    assert quality["output_uri"].endswith("runs/fcst_gfs_2026050100_model_0/output/")
    assert any(blocker["code"] == "STATION_FORCING_UNAVAILABLE" for blocker in blockers)
    assert any(blocker["code"] == "OPTIONAL_WEATHER_PRODUCTS_UNAVAILABLE" for blocker in blockers)


def test_missing_output_river_metadata_is_unavailable_not_fabricated_ready_segment(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "station_count": 2,
        "segment_count": None,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    runtime_manifest = json.loads(Path(forecast_submission["tasks"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["runtime"]["output_river"]["state"] == "unavailable"
    assert runtime_manifest["runtime"]["output_river"]["segment_count"] == 0
    assert runtime_manifest["identity"]["segment_count"] == 0
    assert runtime_manifest["quality_states"]["output_river"] == {
        "state": "unavailable",
        "quality_flag": "output_river_unavailable",
        "segment_count": 0,
    }
    assert any(blocker["code"] == "OUTPUT_RIVER_UNAVAILABLE" for blocker in runtime_manifest["residual_blockers"])


def test_valid_output_river_metadata_remains_ready(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "station_count": 2,
        "output_river": {
            "segment_count": 2,
            "river_segment_ids": ["seg_001", "seg_002"],
            "identity_source": "package_manifest",
        },
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    runtime_manifest = json.loads(Path(forecast_submission["tasks"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["runtime"]["output_river"]["state"] == "ready"
    assert runtime_manifest["runtime"]["output_river"]["segment_count"] == 2
    assert runtime_manifest["runtime"]["output_river"]["river_segment_ids"] == ["seg_001", "seg_002"]


def test_scheduler_style_relative_output_key_does_not_downgrade_runtime_output_uri(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_key": "runs/fcst_gfs_2026050100_model_0/output/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    task = forecast_submission["tasks"][0]
    runtime_manifest = json.loads(Path(task["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["outputs"]["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/"
    assert task["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/"


def test_relative_output_uri_falls_back_to_object_store_output_uri(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "runs/fcst_gfs_2026050100_model_0/output/",
        "log_uri": "runs/fcst_gfs_2026050100_model_0/logs/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    task = forecast_submission["tasks"][0]
    runtime_manifest = json.loads(Path(task["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["outputs"]["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/"
    assert runtime_manifest["outputs"]["log_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/logs/"


def test_legacy_explicit_absolute_output_uri_is_preserved_outside_production_candidate_scope(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "s3://nhms/custom/fcst_gfs_2026050100_model_0/output",
        "log_uri": "s3://nhms/custom/fcst_gfs_2026050100_model_0/logs",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    task = forecast_submission["tasks"][0]
    runtime_manifest = json.loads(Path(task["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["outputs"]["output_uri"] == "s3://nhms/custom/fcst_gfs_2026050100_model_0/output/"
    assert runtime_manifest["outputs"]["log_uri"] == "s3://nhms/custom/fcst_gfs_2026050100_model_0/logs/"


def test_production_candidate_wrong_absolute_output_uri_rejected_before_manifest_writes(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "s3://wrong-bucket/prod/runs/fcst_gfs_2026050100_model_0/output/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert exc_info.value.error_code == "CANDIDATE_IDENTITY_MISMATCH"
    assert exc_info.value.details["field"] == "output_uri"
    assert client.submissions == []
    assert repository.hydro_runs == {}
    assert not (tmp_path / "workspace" / "runs").exists()


def test_production_candidate_relative_output_uri_normalizes_to_canonical_uri(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "runs/fcst_gfs_2026050100_model_0/output",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    runtime_manifest = json.loads(Path(forecast_submission["tasks"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["outputs"]["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/"


@pytest.mark.parametrize("missing_field", ["basin_version_id", "river_network_version_id", "model_package_uri"])
def test_production_candidate_missing_package_or_version_metadata_rejected_before_side_effects(
    tmp_path: Path,
    missing_field: str,
) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }
    basin.pop(missing_field, None)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert exc_info.value.error_code == "PRODUCTION_CANDIDATE_METADATA_UNAVAILABLE"
    assert exc_info.value.details["missing_fields"] == [missing_field]
    assert client.submissions == []
    assert repository.hydro_runs == {}
    assert not (tmp_path / "workspace" / "runs").exists()


def test_legacy_manual_basin_without_production_candidate_identity_keeps_default_metadata_compatibility(
    tmp_path: Path,
) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {"model_id": "manual_model", "station_count": 1, "segment_count": 1}

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    runtime_manifest = json.loads(Path(forecast_submission["tasks"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["model"]["basin_version_id"] == "manual_model_basin"
    assert runtime_manifest["model"]["river_network_version_id"] == "manual_model_river"
    assert runtime_manifest["model"]["model_package_uri"] == "models/manual_model/"


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("candidate_id", "gfs:2026-05-01T00:00:00Z:model_0:wrong_scenario"),
        ("run_id", "fcst_gfs_2026050100_other_model"),
        ("forcing_version_id", "forc_gfs_2026050100_other_model"),
        ("cycle_id", "gfs_2026050106"),
        ("cycle_time", "2026050106"),
        ("scenario_id", "wrong_scenario"),
        ("output_uri", "s3://nhms/runs/fcst_gfs_2026050100_other_model/output/"),
    ],
)
def test_prefilled_identity_mismatch_rejected_before_manifest_writes(
    tmp_path: Path,
    field_name: str,
    field_value: str,
) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }
    basin[field_name] = field_value

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert exc_info.value.error_code == "CANDIDATE_IDENTITY_MISMATCH"
    assert client.submissions == []
    assert repository.hydro_runs == {}
    assert not (tmp_path / "workspace" / "runs").exists()


@pytest.mark.parametrize("field_name", ["candidate_id", "run_id", "run_manifest_uri", "output_uri", "model_id"])
def test_duplicate_sibling_identity_rejected_before_manifest_writes(tmp_path: Path, field_name: str) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basins = _basins(2)
    basins[0].update(
        {
            "run_id": "fcst_gfs_2026050100_model_0",
            "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
            "model_package_uri": "s3://nhms/models/model_0/v1/package/",
            "run_manifest_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_0/input/manifest.json",
            "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/",
        }
    )
    basins[1].update(
        {
            "run_id": "fcst_gfs_2026050100_model_1",
            "candidate_id": "gfs:2026-05-01T00:00:00Z:model_1:forecast_gfs_deterministic",
            "model_package_uri": "s3://nhms/models/model_1/v1/package/",
            "run_manifest_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_1/input/manifest.json",
            "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_1/output/",
        }
    )
    basins[1][field_name] = basins[0][field_name]

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", basins)

    assert exc_info.value.error_code == "DUPLICATE_CANDIDATE_IDENTITY"
    assert exc_info.value.details["field"] == field_name
    assert client.submissions == []
    assert repository.hydro_runs == {}
    assert not (tmp_path / "workspace" / "runs").exists()


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


def test_array_stage_requires_array_submit_contract_before_submission(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = SubmitJobOnlyCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    forcing_result = result.stages[-1]
    forcing_job = repository.jobs["job_cycle_gfs_2026050100_forcing"]
    assert result.status == "failed"
    assert forcing_result.stage == "forcing"
    assert forcing_result.status == "submission_failed"
    assert forcing_result.error_code == "SLURM_ARRAY_SUBMIT_UNSUPPORTED"
    assert forcing_job["status"] == "submission_failed"
    assert forcing_job["slurm_job_id"] is None
    assert forcing_job["error_code"] == "SLURM_ARRAY_SUBMIT_UNSUPPORTED"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert"]
    assert all(payload["job_type"] != "produce_forcing_array" for payload in client.submit_job_payloads)
    assert "forecast" not in [submission["stage"] for submission in client.submissions]
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


def test_forecast_submission_failure_marks_staged_hydro_runs_failed(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    client.fail_next_array_submission_stage = "forecast"
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert {run["status"] for run in repository.hydro_runs.values()} == {"failed"}
    assert {
        run["error_code"]
        for run in repository.hydro_runs.values()
    } == {"SBATCH_SUBMISSION_FAILED"}
    assert repository.jobs["job_cycle_gfs_2026050100_forecast"]["status"] == "submission_failed"


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
    client = FakeCycleSlurmClient(
        array_results_by_stage={
            "forcing": ["succeeded", "succeeded"],
            "forecast": ["succeeded", "succeeded"],
        }
    )
    client.jobs["3002"] = {
        "job_id": "3002",
        "run_id": run_id,
        "model_id": "model_0",
        "stage": "forcing",
        "status": "succeeded",
        "submitted_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "payload": {"tasks": [{}, {}]},
    }
    client.jobs["3003"] = {
        "job_id": "3003",
        "run_id": run_id,
        "model_id": "model_0",
        "stage": "forecast",
        "status": "succeeded",
        "submitted_at": _fmt(_dt("2026-05-01T00:03:00Z")),
        "payload": {"tasks": [{}, {}]},
    }
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
        "quality_states": publish_submission["metadata"]["quality_states"],
        "residual_blockers": publish_submission["metadata"]["residual_blockers"],
    }
    assert [item["model_id"] for item in publish_submission["basins"]] == ["model_0", "model_2"]
    assert [item["model_id"] for item in publish_submission["metadata"]["quality_states"]] == ["model_0", "model_2"]
    assert repository.cycle_statuses[-1] == "parsed_partial"


def test_array_partial_success_records_task_accounting_and_reduces_downstream_manifests(
    tmp_path: Path,
) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["succeeded", "failed", "succeeded"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    forcing_result = next(stage for stage in result.stages if stage.stage == "forcing")
    forcing_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_forcing"
        and event["status_to"] == "partially_failed"
    )
    task_results = forcing_event["details"]["task_results"]
    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    assert result.status == "parsed_partial"
    assert forcing_result.status == "partially_failed"
    assert forcing_result.task_results == tuple(task_results)
    assert task_results[0]["array_task_id"] == 0
    assert task_results[0]["slurm_job_id"].endswith("_0")
    assert task_results[0]["exit_code"] == 0
    assert task_results[0]["log_uri"].endswith("_0.out")
    assert task_results[0]["accounting"]["elapsed"] == "00:01:00"
    assert task_results[0]["resource_metrics"]["max_rss"] == "1024K"
    assert task_results[1]["status"] == "failed"
    assert task_results[1]["exit_code"] == 1
    assert [task["model_id"] for task in forecast_submission["tasks"]] == ["model_0", "model_2"]
    assert repository.cycle_statuses[-1] == "parsed_partial"


def test_slurm_accounting_available_is_recorded_in_pipeline_event_details(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(
        accounting_by_stage={"download": {"elapsed": "00:02:00", "max_rss": "2048K", "alloc_tres": "cpu=2,mem=4G"}}
    )
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    accounting_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_download"
        and event["event_type"] == "slurm_accounting"
    )
    slurm = accounting_event["details"]["slurm"]
    assert result.status == "complete"
    assert slurm["job_id"] == "2001"
    assert slurm["state"] == "succeeded"
    assert slurm["exit_code"] == 0
    assert slurm["log_uri"].endswith("/download.log")
    assert slurm["accounting"]["elapsed"] == "00:02:00"
    assert slurm["accounting"]["max_rss"] == "2048K"
    assert slurm["resource_metrics"]["alloc_tres"] == "cpu=2,mem=4G"


def test_malformed_array_accounting_records_gap_without_fabricating_metrics(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(malformed_array_accounting_stages={"forcing"})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    gap_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_forcing"
        and event["event_type"] == "slurm_accounting_gap"
    )
    forcing_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_forcing"
        and event["status_to"] == "failed"
    )
    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert gap_event["details"]["fabricated_metrics"] is False
    assert gap_event["details"]["gap"]["error"]
    assert forcing_event["details"]["task_results"] == ()


def test_cancel_active_cycle_jobs_calls_gateway_and_records_no_replacement(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    repository.jobs["job_cycle_gfs_2026050100_forcing"] = {
        "job_id": "job_cycle_gfs_2026050100_forcing",
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3001",
        "model_id": "model_0",
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": "s3://nhms/runs/cycle_gfs_2026050100/logs/forcing.log",
    }
    client = FakeCycleSlurmClient()
    client.jobs["3001"] = {
        "job_id": "3001",
        "run_id": "cycle_gfs_2026050100",
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "payload": {},
        "stage_attempt": 0,
    }
    orchestrator = _orchestrator(tmp_path, repository, client)

    cancelled = orchestrator.cancel_active_cycle_jobs(cycle_id, reason="scheduler_cancel_requested")

    cancel_event = repository.events[-1]
    assert client.cancelled_jobs == ["3001"]
    assert cancelled[0]["status"] == "cancelled"
    assert cancel_event["event_type"] == "cancel"
    assert cancel_event["details"]["replacement_submitted"] is False
    assert cancel_event["details"]["slurm"]["job_id"] == "3001"
    assert cancel_event["details"]["reason"] == "scheduler_cancel_requested"
    assert client.submissions == []


def test_cancel_active_cycle_jobs_conflict_records_gap_without_rewriting_cancelled(tmp_path: Path) -> None:
    class ConflictCancelClient(FakeCycleSlurmClient):
        def cancel_job(self, job_id: str) -> dict[str, Any]:
            from services.orchestrator.chain import SlurmClientError

            raise SlurmClientError(
                "JOB_ALREADY_TERMINAL",
                "Slurm Gateway returned HTTP 409.",
                {
                    "response": {
                        "error": {
                            "code": "JOB_ALREADY_TERMINAL",
                            "details": {"job_id": job_id, "status": "succeeded"},
                        }
                    }
                },
            )

    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    repository.jobs["job_cycle_gfs_2026050100_forcing"] = {
        "job_id": "job_cycle_gfs_2026050100_forcing",
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3001",
        "model_id": "model_0",
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": "s3://nhms/runs/cycle_gfs_2026050100/logs/forcing.log",
    }
    client = ConflictCancelClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    cancelled = orchestrator.cancel_active_cycle_jobs(cycle_id, reason="scheduler_cancel_requested")

    assert repository.jobs["job_cycle_gfs_2026050100_forcing"]["status"] == "running"
    assert cancelled[0]["status"] == "running"
    assert cancelled[0]["error_code"] == "JOB_ALREADY_TERMINAL"
    gap_event = repository.events[-1]
    assert gap_event["event_type"] == "slurm_cancellation_gap"
    assert gap_event["status_to"] == "blocked"
    assert gap_event["details"]["replacement_submitted"] is False
    assert gap_event["details"]["slurm"]["cancellation_proven"] is False


def test_psycopg_active_slurm_jobs_includes_cycle_run_array_job_for_filtered_model() -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            calls.append((statement, parameters))
            return [
                {
                    "job_id": "job_cycle_gfs_2026050100_forecast",
                    "run_id": "cycle_gfs_2026050100",
                    "cycle_id": "gfs_2026050100",
                    "job_type": "run_shud_forecast_array",
                    "slurm_job_id": "3001",
                    "model_id": "model_a",
                    "status": "running",
                    "stage": "forecast",
                }
            ]

    repository = CapturingRepository("postgresql://example")

    jobs = repository.active_slurm_jobs(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_b",
    )

    statement, parameters = calls[0]
    assert "OR pj.run_id = %s" in statement
    assert parameters == (
        "gfs_2026050100",
        "model_b",
        "model_b",
        "fcst_gfs_2026050100_model_b",
        "cycle_gfs_2026050100",
        "cycle_gfs_2026050100",
    )
    assert jobs[0]["run_id"] == "cycle_gfs_2026050100"
    assert jobs[0]["model_id"] == "model_a"


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
    client: Any,
    *,
    retry_service: Any | None = None,
    object_store: LocalObjectStore | None = None,
    orchestrator_cls: type[ForecastOrchestrator] = ForecastOrchestrator,
    slurm_job_type_templates: dict[str, str] | None = None,
    slurm_env: dict[str, str] | None = None,
) -> ForecastOrchestrator:
    workspace = tmp_path / "workspace"
    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=workspace,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        poll_interval_seconds=0,
        job_timeout_seconds=5,
        slurm_job_type_templates=slurm_job_type_templates or {},
        slurm_env=slurm_env or {},
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
