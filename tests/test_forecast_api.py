from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.routes.data_sources import get_data_source_store
from apps.api.routes.forecast import get_forecast_store
from packages.common.forecast_store import ForecastStoreError


class FakeForecastStore:
    def __init__(self) -> None:
        self.forecast_calls: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []
        issue_time = _dt("2026-05-07T00:00:00Z")
        self.response = {
            "segment_id": "seg_001",
            "issue_time": "2026-05-07T00:00:00Z",
            "unit": "m3/s",
            "series": [
                {
                    "scenario_id": "forecast_gfs_deterministic",
                    "segment_role": "future_7_days",
                    "points": [
                        [_timestamp_ms(issue_time), 11.25],
                        [_timestamp_ms(issue_time + timedelta(hours=3)), 12.5],
                    ],
                }
            ],
            "frequency_thresholds": {},
        }

    def forecast_series(self, **kwargs: Any) -> dict[str, Any]:
        self.forecast_calls.append(kwargs)
        if kwargs["segment_id"] == "missing":
            raise ForecastStoreError(
                status_code=404,
                code="SEGMENT_NOT_FOUND",
                message="River segment not found: missing",
                details={"segment_id": "missing"},
            )
        return self.response

    def get_run(self, run_id: str) -> dict[str, Any]:
        if run_id == "missing":
            raise ForecastStoreError(
                status_code=404,
                code="RUN_NOT_FOUND",
                message="Run not found: missing",
                details={"run_id": "missing"},
            )
        return {"run_id": run_id, "status": "parsed", "source": "gfs"}

    def list_runs(self, **kwargs: Any) -> dict[str, Any]:
        self.run_calls.append(kwargs)
        return {
            "total_count": 1,
            "items": [{"run_id": "run_001", "status": kwargs.get("status") or "parsed"}],
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    def list_data_sources(self, *, limit: int, offset: int) -> dict[str, Any]:
        return {
            "total_count": 1,
            "items": [{"source_id": "gfs", "provider": "NOAA/NCEP", "source": "gfs", "format": "GRIB2"}],
            "limit": limit,
            "offset": offset,
        }

    def list_cycles(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["source_id"] == "missing":
            raise ForecastStoreError(
                status_code=404,
                code="SOURCE_NOT_FOUND",
                message="Data source not found: missing",
                details={"source_id": "missing"},
            )
        return {
            "total_count": 1,
            "items": [{"cycle_id": "gfs_2026050700", "status": kwargs.get("status") or "raw_complete"}],
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    def list_met_stations(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["basin_version_id"] is None and kwargs["model_id"] is None:
            raise ForecastStoreError(
                status_code=422,
                code="MISSING_REQUIRED_FILTER",
                message="At least one of basin_version_id or model_id is required.",
                details={"required": ["basin_version_id", "model_id"]},
            )
        return {
            "total_count": 1,
            "items": [{"station_id": "sta_001", "name": "代站 1", "longitude": 110.0, "latitude": 30.0}],
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }


@pytest.fixture
def fake_store() -> FakeForecastStore:
    store = FakeForecastStore()
    app.dependency_overrides[get_forecast_store] = lambda: store
    app.dependency_overrides[get_data_source_store] = lambda: store
    return store


@pytest.fixture(autouse=True)
def clear_overrides() -> None:
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_forecast_series_returns_timestamp_value_tuples_and_q_down_filter(fake_store: FakeForecastStore) -> None:
    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series"
        "?issue_time=latest&variables=q_down&scenarios=GFS"
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"]
    data = response.json()
    assert data["unit"] == "m3/s"
    points = data["series"][0]["points"]
    assert points == fake_store.response["series"][0]["points"]
    assert all(isinstance(point, list) and len(point) == 2 for point in points)
    assert fake_store.forecast_calls[-1]["variables"] == ["q_down"]
    assert fake_store.forecast_calls[-1]["scenarios"] == ["GFS"]


@pytest.mark.asyncio
async def test_forecast_series_segment_not_found_uses_unified_error(fake_store: FakeForecastStore) -> None:
    response = await _get("/api/v1/basin-versions/basin_v1/river-segments/missing/forecast-series")

    assert fake_store is not None
    assert response.status_code == 404
    data = response.json()
    assert data["status"] == "error"
    assert data["request_id"]
    assert data["error"]["code"] == "SEGMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_run_list_uses_offset_limit_pagination_and_caps_limit(fake_store: FakeForecastStore) -> None:
    response = await _get("/api/v1/runs?basin_id=yangtze&source=gfs&status=parsed&limit=1000&offset=20")

    assert response.status_code == 200
    data = response.json()
    assert data["total_count"] == 1
    assert data["limit"] == 200
    assert data["offset"] == 20
    assert fake_store.run_calls[-1]["basin_id"] == "yangtze"
    assert fake_store.run_calls[-1]["source"] == "gfs"
    assert fake_store.run_calls[-1]["status"] == "parsed"


@pytest.mark.asyncio
async def test_data_source_cycles_not_found_error_code(fake_store: FakeForecastStore) -> None:
    response = await _get("/api/v1/data-sources/missing/cycles")

    assert fake_store is not None
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_met_stations_requires_basin_or_model_filter(fake_store: FakeForecastStore) -> None:
    response = await _get("/api/v1/met/stations")

    assert fake_store is not None
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MISSING_REQUIRED_FILTER"


async def _get(path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)
