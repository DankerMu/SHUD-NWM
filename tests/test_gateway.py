import asyncio
import threading

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from apps.api.errors import register_error_handlers
from apps.api.main import app
from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.gateway import create_gateway
from services.slurm_gateway.models import ResetRequest
from services.slurm_gateway.routes import slurm_gateway


@pytest.fixture(autouse=True)
def reset_mock_gateway():
    slurm_gateway.reset(ResetRequest(restore_defaults=True))
    yield
    slurm_gateway.reset(ResetRequest(restore_defaults=True))


@pytest.fixture
async def client():
    app.dependency_overrides[get_settings] = lambda: SlurmGatewaySettings(allow_internal_reset=True)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            yield async_client
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_health(client):
    response = await client.get("/api/v1/slurm/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["backend"] == "mock"
    assert payload["version"] == "0.1.0"
    assert payload["status"] == "ok"
    assert payload["error"] is None
    assert payload["healthy"] is True
    assert set(payload["binaries"]) == {"sbatch", "squeue", "sacct", "scancel"}
    assert all(probe["resolved"] and probe["executable"] for probe in payload["binaries"].values())


@pytest.mark.asyncio
async def test_reset_rejects_by_default():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        response = await async_client.post("/api/v1/slurm/internal/reset")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SLURM_INTERNAL_RESET_DISABLED"


@pytest.mark.asyncio
async def test_submit_job(client):
    response = await client.post(
        "/api/v1/slurm/jobs",
        json={"run_id": "run_001", "model_id": "model_001"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["job_id"] == "mock_1001"
    assert data["run_id"] == "run_001"
    assert data["model_id"] == "model_001"
    assert data["status"] == "submitted"


@pytest.mark.asyncio
async def test_submit_job_offloads_blocking_gateway_calls(client):
    class BlockingGateway:
        def __init__(self) -> None:
            self.barrier = threading.Barrier(2)
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def submit_job(self, request):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                self.barrier.wait(timeout=2)
            finally:
                with self.lock:
                    self.active -= 1
            return {
                "job_id": f"mock_{request.run_id}",
                "run_id": request.run_id,
                "model_id": request.model_id,
                "status": "submitted",
            }

        def reset(self, _request=None):
            return {"cleared": 0}

    fake_gateway = BlockingGateway()
    slurm_gateway._instance = fake_gateway
    try:
        responses = await asyncio.wait_for(
            asyncio.gather(
                client.post("/api/v1/slurm/jobs", json={"run_id": "run_a", "model_id": "model_001"}),
                client.post("/api/v1/slurm/jobs", json={"run_id": "run_b", "model_id": "model_001"}),
            ),
            timeout=3,
        )
    finally:
        slurm_gateway.reset_instance()

    assert [response.status_code for response in responses] == [201, 201]
    assert fake_gateway.max_active == 2


@pytest.mark.asyncio
async def test_submit_missing_fields(client):
    response = await client.post("/api/v1/slurm/jobs", json={"model_id": "model_001"})

    assert response.status_code == 422
    data = response.json()
    assert data["status"] == "error"
    assert data["error"]["code"] == "INVALID_MANIFEST"
    assert data["error"]["details"]["missing_fields"] == ["run_id"]


@pytest.mark.asyncio
async def test_duplicate_run_id(client):
    payload = {"run_id": "run_duplicate", "model_id": "model_001"}
    first = await client.post("/api/v1/slurm/jobs", json=payload)
    second = await client.post("/api/v1/slurm/jobs", json=payload)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DUPLICATE_RUN"


@pytest.mark.asyncio
async def test_get_job_status(client):
    submitted = await client.post(
        "/api/v1/slurm/jobs",
        json={"run_id": "run_status", "model_id": "model_001"},
    )

    response = await client.get(f"/api/v1/slurm/jobs/{submitted.json()['job_id']}")

    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == "mock_1001"
    assert data["run_id"] == "run_status"
    assert data["status"] == "submitted"
    assert data["submitted_at"]
    assert data["updated_at"]


@pytest.mark.asyncio
async def test_cancel_active_job(client):
    submitted = await client.post(
        "/api/v1/slurm/jobs",
        json={"run_id": "run_cancel", "model_id": "model_001"},
    )

    response = await client.delete(f"/api/v1/slurm/jobs/{submitted.json()['job_id']}")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "cancelled"
    assert data["finished_at"] is not None


@pytest.mark.asyncio
async def test_cancel_terminal_job(client):
    await client.post(
        "/api/v1/slurm/internal/reset",
        json={"delay_to_running_seconds": 0, "delay_to_succeeded_seconds": 0},
    )
    submitted = await client.post(
        "/api/v1/slurm/jobs",
        json={"run_id": "run_terminal", "model_id": "model_001"},
    )

    response = await client.delete(f"/api/v1/slurm/jobs/{submitted.json()['job_id']}")

    assert submitted.json()["status"] == "succeeded"
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "JOB_ALREADY_TERMINAL"


@pytest.mark.asyncio
async def test_cancel_not_found(client):
    response = await client.delete("/api/v1/slurm/jobs/mock_9999")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_list_jobs(client):
    await client.post("/api/v1/slurm/jobs", json={"run_id": "run_list_1", "model_id": "model_001"})
    await client.post("/api/v1/slurm/jobs", json={"run_id": "run_list_2", "model_id": "model_001"})

    response = await client.get("/api/v1/slurm/jobs")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert [job["job_id"] for job in data] == ["mock_1002", "mock_1001"]


@pytest.mark.asyncio
async def test_array_task_results(client):
    await client.post(
        "/api/v1/slurm/internal/reset",
        json={"delay_to_running_seconds": 0, "delay_to_succeeded_seconds": 0},
    )
    submitted = await client.post(
        "/api/v1/slurm/job-arrays",
        json={
            "job_type": "run_shud_forecast_array",
            "cycle_id": "gfs_2026050100",
            "stage_name": "forecast",
            "manifest": {"run_id": "run_array", "model_id": "model_001"},
            "tasks": [
                {"run_id": "run_0", "model_id": "model_001", "basin_version_id": "basin_0"},
                {"run_id": "run_1", "model_id": "model_001", "basin_version_id": "basin_1"},
            ],
        },
    )

    response = await client.get(f"/api/v1/slurm/jobs/{submitted.json()['job_id']}/array-tasks")

    assert response.status_code == 200
    assert [task["task_id"] for task in response.json()] == [0, 1]
    assert {task["status"] for task in response.json()} == {"succeeded"}


@pytest.mark.asyncio
async def test_fetch_logs_succeeded(client):
    await client.post(
        "/api/v1/slurm/internal/reset",
        json={"delay_to_running_seconds": 0, "delay_to_succeeded_seconds": 0},
    )
    submitted = await client.post(
        "/api/v1/slurm/jobs",
        json={"run_id": "run_logs", "model_id": "model_001"},
    )

    response = await client.get(f"/api/v1/slurm/jobs/{submitted.json()['job_id']}/logs")

    assert response.status_code == 200
    data = response.json()
    assert data["complete"] is True
    assert "exit code 0" in data["logs"]


@pytest.mark.asyncio
async def test_reset(client):
    await client.post("/api/v1/slurm/jobs", json={"run_id": "run_reset", "model_id": "model_001"})

    reset_response = await client.post("/api/v1/slurm/internal/reset")
    jobs_response = await client.get("/api/v1/slurm/jobs")

    assert reset_response.status_code == 200
    assert reset_response.json()["cleared"] == 1
    assert jobs_response.json() == []


@pytest.mark.asyncio
async def test_zero_delay_immediate_success(client):
    await client.post(
        "/api/v1/slurm/internal/reset",
        json={"delay_to_running_seconds": 0, "delay_to_succeeded_seconds": 0},
    )

    response = await client.post(
        "/api/v1/slurm/jobs",
        json={"run_id": "run_zero_delay", "model_id": "model_001"},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "succeeded"


@pytest.mark.asyncio
async def test_non_slurm_validation_error_envelope_survives_invalid_slurm_backend(monkeypatch):
    class ActivePayload(BaseModel):
        active: bool

    local_app = FastAPI()
    register_error_handlers(local_app)

    @local_app.put("/local/active")
    async def local_active(payload: ActivePayload):
        return {"active": payload.active}

    monkeypatch.setenv("SLURM_GATEWAY_BACKEND", "invalid")
    transport = ASGITransport(app=local_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        response = await async_client.put("/local/active", json={"active": {"not": "a-bool"}})

    assert response.status_code == 422
    data = response.json()
    assert data["status"] == "error"
    assert data["error"]["code"] == "VALIDATION_ERROR"
    assert data["error"]["details"][0]["field"] == "body.active"


@pytest.mark.asyncio
async def test_bare_slurm_router_validation_redacts_without_gateway_construction(monkeypatch):
    calls = {"count": 0}

    def fake_create_gateway(*args, **kwargs):
        del args, kwargs
        calls["count"] += 1
        return create_gateway(SlurmGatewaySettings(backend="mock"))

    monkeypatch.setattr("services.slurm_gateway.routes.create_gateway", fake_create_gateway)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        response = await async_client.get("/api/v1/slurm/jobs/bad-id")

    assert response.status_code == 422
    response_text = response.text
    assert "supersecret" not in response_text
    assert '"input"' not in response_text
    assert "rejected_value" not in response_text
    assert calls["count"] == 0
