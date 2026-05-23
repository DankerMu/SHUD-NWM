import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app


@pytest.mark.asyncio
async def test_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "nhms-api"
    assert data["version"] == "0.1.0"


def test_api_errors_import_does_not_construct_slurm_gateway(monkeypatch):
    monkeypatch.setenv("SLURM_GATEWAY_BACKEND", "invalid")
    import apps.api.errors as errors

    assert errors.ApiError(
        status_code=400,
        code="BAD_REQUEST",
        message="bad request",
    ).code == "BAD_REQUEST"
