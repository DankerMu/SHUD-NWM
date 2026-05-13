from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.routes.forecast import get_forecast_store
from tests.test_monitoring_api import (
    _client,
    _create_job,
    _cycle_time,
    _insert_cycle,
    _MockGateway,
    _seed_monitoring_jobs,
    _store,
)
from workers.data_adapters.base import cycle_id_for

PIPELINE_JOB_KEYS = {
    "job_id",
    "run_id",
    "cycle_id",
    "run_type",
    "scenario",
    "job_type",
    "slurm_job_id",
    "model_id",
    "status",
    "stage",
    "submitted_at",
    "started_at",
    "finished_at",
    "exit_code",
    "retry_count",
    "error_code",
    "error_message",
    "log_uri",
    "duration_seconds",
}


def test_runs_contract_uses_success_envelope_and_paginated_data() -> None:
    app.dependency_overrides[get_forecast_store] = lambda: _RunStore()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/runs", params={"status": "frequency_done", "limit": 10, "offset": 0})
    finally:
        app.dependency_overrides.pop(get_forecast_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"items", "total_count", "limit", "offset", "total"}
    assert data["total"] == 1
    assert data["total_count"] == 1
    assert data["limit"] == 10
    assert data["offset"] == 0
    run = data["items"][0]
    assert run["run_id"] == "run_frequency_done"
    assert run["run_type"] == "forecast"
    assert run["status"] == "frequency_done"
    assert isinstance(run["start_time"], str)
    assert isinstance(run["end_time"], str)


def test_jobs_contract_uses_success_envelope_and_paginated_pipeline_jobs() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get(
                "/api/v1/jobs",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat(), "limit": 2, "offset": 0},
            )

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"items", "total", "limit", "offset"}
    assert data["total"] == 5
    assert data["limit"] == 2
    assert data["offset"] == 0
    assert len(data["items"]) == 2
    assert set(data["items"][0]) == PIPELINE_JOB_KEYS
    assert data["items"][0]["run_type"] is None
    assert data["items"][0]["scenario"] is None


def test_pipeline_status_contract_uses_success_envelope() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _insert_cycle(store, cycle_time=cycle_time, current_state="forecast_running")
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get(
                "/api/v1/pipeline/status",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"cycle_id", "source", "cycle_time", "current_state", "started_at", "updated_at", "job_counts"}
    assert data["cycle_id"] == cycle_id
    assert data["current_state"] == "forecast_running"
    assert data["job_counts"] == {"succeeded": 3, "failed": 1, "running": 1, "pending": 0}


def test_queue_depth_contract_uses_success_envelope() -> None:
    with _store() as store:
        with _client(store, _MockGateway(depth={"running": 2, "pending": 3, "idle": 1})) as client:
            response = client.get("/api/v1/queue/depth")

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"running", "pending", "idle"}
    assert data == {"running": 2, "pending": 3, "idle": 1}


def test_stage_duration_metrics_contract_uses_success_envelope() -> None:
    with _store() as store:
        cycle_id = cycle_id_for("GFS", _cycle_time())
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get("/api/v1/metrics/stage-duration", params={"days": 30})

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert isinstance(data, list)
    metric = next(row for row in data if row["stage"] == "download")
    assert set(metric) == {"date", "stage", "average_duration_seconds", "job_count"}
    assert isinstance(metric["date"], str)
    assert isinstance(metric["average_duration_seconds"], float)
    assert isinstance(metric["job_count"], int)


def test_retry_contract_documents_pipeline_job_and_execution_status_fields() -> None:
    with _store() as store:
        _create_job(store, job_id="job_retry_contract", run_id="run_retry_contract", status="failed")
        with _client(store) as client:
            response = client.post("/api/v1/runs/run_retry_contract/retry", headers={"X-User-Role": "operator"})

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {
        "job_id",
        "pipeline_job_id",
        "run_id",
        "retry_count",
        "status",
        "slurm_job_id",
        "execution_status",
    }
    assert data["pipeline_job_id"] == data["job_id"]
    assert data["run_id"] == "run_retry_contract"
    assert data["execution_status"] == "queued"
    assert data["slurm_job_id"] is None


def test_cancel_contract_documents_cancelled_jobs_and_slurm_failures() -> None:
    with _store() as store:
        gateway = _MockGateway()
        _create_job(
            store,
            job_id="job_cancel_contract",
            run_id="run_cancel_contract",
            status="running",
            slurm_job_id="slurm_cancel_contract",
        )
        with _client(store, gateway) as client:
            response = client.post("/api/v1/runs/run_cancel_contract/cancel", headers={"X-User-Role": "operator"})

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {
        "run_id",
        "cancelled_jobs",
        "cancelled",
        "failed_jobs",
        "slurm_failures",
        "partial_failure",
        "idempotent_jobs",
        "hydro_run",
        "forecast_cycle",
    }
    assert data["run_id"] == "run_cancel_contract"
    assert data["failed_jobs"] == []
    assert data["slurm_failures"] == []
    assert data["partial_failure"] is False
    assert data["cancelled"] == data["cancelled_jobs"]
    assert len(data["cancelled_jobs"]) == 1
    assert set(data["cancelled_jobs"][0]) == PIPELINE_JOB_KEYS
    assert data["cancelled_jobs"][0]["status"] == "cancelled"


def test_generated_frontend_types_match_openapi(tmp_path: Path) -> None:
    generated = tmp_path / "generated-types.ts"
    subprocess.run(
        [
            "npx",
            "openapi-typescript",
            "../../openapi/nhms.v1.yaml",
            "--output",
            str(generated),
        ],
        cwd=Path(__file__).resolve().parents[1] / "apps" / "frontend",
        check=True,
    )
    committed = Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    assert committed.read_text(encoding="utf-8") == generated.read_text(encoding="utf-8")


def _assert_success_envelope(body: dict[str, Any]) -> Any:
    assert set(body) == {"request_id", "status", "data"}
    assert body["request_id"]
    assert body["status"] == "ok"
    return body["data"]


class _RunStore:
    def list_runs(self, **kwargs: Any) -> dict[str, Any]:
        now = datetime(2026, 5, 3, tzinfo=UTC)
        return {
            "items": [
                {
                    "run_id": "run_frequency_done",
                    "run_type": "forecast",
                    "scenario_id": "forecast_gfs_deterministic",
                    "model_id": "model_1",
                    "basin_version_id": "basin_v1",
                    "forcing_version_id": None,
                    "init_state_id": None,
                    "source_id": "GFS",
                    "cycle_time": now.isoformat(),
                    "status": kwargs.get("status") or "frequency_done",
                    "slurm_job_id": None,
                    "start_time": now.isoformat(),
                    "end_time": (now + timedelta(days=7)).isoformat(),
                    "run_manifest_uri": "object://manifest",
                    "output_uri": None,
                    "log_uri": None,
                    "error_code": None,
                    "error_message": None,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            ],
            "total_count": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }
