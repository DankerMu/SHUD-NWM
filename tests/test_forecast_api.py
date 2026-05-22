from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.routes.data_sources import get_data_source_store
from apps.api.routes.forecast import get_forecast_store
from packages.common.forecast_store import (
    ForecastStoreError,
    PsycopgForecastStore,
    _forecast_response_from_rows,
    _spliced_response_from_rows,
    analysis_window_for_issue_time,
)


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
        self.spliced_response = {
            "segments": [
                {
                    "scenario": "analysis_true_field",
                    "source": "ERA5",
                    "data": [{"valid_time": "2026-05-06T00:00:00Z", "value": 10.0}],
                },
                {
                    "scenario": "forecast_gfs_deterministic",
                    "source": "GFS",
                    "data": [{"valid_time": "2026-05-07T00:00:00Z", "value": 11.25}],
                },
            ],
            "issue_time": "2026-05-07T00:00:00Z",
            "river_segment_id": "seg_001",
            "variable": "discharge",
            "unit": "m3/s",
        }
        self.analysis_only_response = {
            "segments": [
                {
                    "scenario": "analysis_true_field",
                    "source": "ERA5",
                    "data": [{"valid_time": "2026-05-06T00:00:00Z", "value": 10.0}],
                }
            ],
            "issue_time": "2026-05-07T00:00:00Z",
            "river_segment_id": "analysis_only",
            "variable": "discharge",
            "unit": "m3/s",
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
        if kwargs.get("include_analysis") and kwargs["segment_id"] == "analysis_only":
            return self.analysis_only_response
        if kwargs.get("include_analysis"):
            return self.spliced_response
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


class InMemoryForecastSeriesStore(PsycopgForecastStore):
    def __init__(self) -> None:
        super().__init__("postgresql://test")
        self.latest_cycles = {
            "forecast_gfs_deterministic": _dt("2026-05-07T00:00:00Z"),
            "forecast_ifs_deterministic": _dt("2026-05-07T18:00:00Z"),
        }
        self.latest_analysis_issue_time: datetime | None = _dt("2026-05-07T18:00:00Z")
        self.forecast_fetches: list[dict[str, Any]] = []
        self.analysis_rows = [
            {
                "scenario_id": "analysis_true_field",
                "source_id": "ERA5",
                "valid_time": _dt("2026-05-06T18:00:00Z"),
                "value": 10.0,
                "unit": "m3/s",
            }
        ]
        self.forecast_rows = [
            {
                "scenario_id": "forecast_gfs_deterministic",
                "source_id": "GFS",
                "cycle_time": _dt("2026-05-07T00:00:00Z"),
                "valid_time": _dt("2026-05-07T00:00:00Z"),
                "value": 11.0,
                "unit": "m3/s",
            },
            {
                "scenario_id": "forecast_ifs_deterministic",
                "source_id": "IFS",
                "cycle_time": _dt("2026-05-07T18:00:00Z"),
                "valid_time": _dt("2026-05-07T18:00:00Z"),
                "value": 12.0,
                "unit": "m3/s",
            },
        ]

    def _transaction(self) -> Any:
        return _NullTransaction()

    def _validate_series_target(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        river_network_version_id: str,
    ) -> None:
        del cursor, basin_version_id, segment_id, river_network_version_id

    def _per_source_latest_cycles(self, cursor: Any, **_kwargs: Any) -> dict[str, datetime]:
        del cursor
        return dict(self.latest_cycles)

    def _latest_analysis_issue_time(self, cursor: Any, **_kwargs: Any) -> datetime | None:
        del cursor
        return self.latest_analysis_issue_time

    def _fetch_analysis_segment_rows(self, cursor: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        del cursor
        return list(self.analysis_rows)

    def _fetch_forecast_segment_rows(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        river_network_version_id: str,
        issue_time: datetime,
        scenario_filter: Any,
        cycle_times_by_scenario: dict[str, datetime] | None = None,
        end_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        del cursor, basin_version_id, segment_id, river_network_version_id, scenario_filter, end_time
        self.forecast_fetches.append(
            {
                "issue_time": issue_time,
                "cycle_times_by_scenario": cycle_times_by_scenario,
            }
        )
        if cycle_times_by_scenario is None:
            return [row for row in self.forecast_rows if row["cycle_time"] == issue_time]
        return [
            row
            for row in self.forecast_rows
            if cycle_times_by_scenario.get(str(row["scenario_id"])) == row["cycle_time"]
        ]


class SqlCaptureForecastStore(PsycopgForecastStore):
    def __init__(self, rows_by_statement: list[list[dict[str, Any]]] | None = None) -> None:
        super().__init__("postgresql://test")
        self.cursor = SqlCaptureCursor(rows_by_statement or [])

    def _transaction(self) -> Any:
        return _CursorTransaction(self.cursor)


class SqlCaptureCursor:
    def __init__(self, rows_by_statement: list[list[dict[str, Any]]]) -> None:
        self.rows_by_statement = rows_by_statement
        self.executions: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self.executions.append((statement, parameters))

    def fetchall(self) -> list[dict[str, Any]]:
        if not self.rows_by_statement:
            return []
        return self.rows_by_statement.pop(0)

    def fetchone(self) -> dict[str, Any]:
        rows = self.fetchall()
        return rows[0] if rows else {}


class _CursorTransaction:
    def __init__(self, cursor: SqlCaptureCursor) -> None:
        self.cursor = cursor

    def __enter__(self) -> SqlCaptureCursor:
        return self.cursor

    def __exit__(self, *_args: Any) -> bool:
        return False


class _NullTransaction:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: Any) -> bool:
        return False


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
        "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series?river_network_version_id=rnv_v1"
        "&issue_time=latest&variables=q_down&scenarios=GFS"
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
    assert fake_store.forecast_calls[-1]["river_network_version_id"] == "rnv_v1"
    assert fake_store.forecast_calls[-1]["include_analysis"] is False


@pytest.mark.asyncio
async def test_forecast_series_allows_null_frequency_thresholds(fake_store: FakeForecastStore) -> None:
    fake_store.response["frequency_thresholds"] = None

    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series?river_network_version_id=rnv_v1"
    )

    assert response.status_code == 200
    assert response.json()["frequency_thresholds"] is None


@pytest.mark.asyncio
async def test_forecast_series_include_analysis_true_returns_spliced_segments(fake_store: FakeForecastStore) -> None:
    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series?river_network_version_id=rnv_v1"
        "&issue_time=latest&variables=q_down&include_analysis=true"
    )

    assert response.status_code == 200
    data = response.json()
    assert "series" not in data
    assert data["variable"] == "discharge"
    assert data["river_segment_id"] == "seg_001"
    assert [segment["scenario"] for segment in data["segments"]] == [
        "analysis_true_field",
        "forecast_gfs_deterministic",
    ]
    assert [segment["source"] for segment in data["segments"]] == ["ERA5", "GFS"]
    assert fake_store.forecast_calls[-1]["include_analysis"] is True


@pytest.mark.asyncio
async def test_forecast_series_include_analysis_false_keeps_m1_response(fake_store: FakeForecastStore) -> None:
    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series?river_network_version_id=rnv_v1"
        "&issue_time=latest&variables=q_down&include_analysis=false"
    )

    assert response.status_code == 200
    data = response.json()
    assert "series" in data
    assert "segments" not in data
    assert data["series"][0]["scenario_id"] == "forecast_gfs_deterministic"
    assert fake_store.forecast_calls[-1]["include_analysis"] is False


@pytest.mark.asyncio
async def test_forecast_series_include_analysis_supports_analysis_only(fake_store: FakeForecastStore) -> None:
    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/analysis_only/forecast-series?river_network_version_id=rnv_v1"
        "&issue_time=2026-05-07T00:00:00Z&variables=q_down&include_analysis=true"
    )

    assert response.status_code == 200
    data = response.json()
    assert [segment["scenario"] for segment in data["segments"]] == ["analysis_true_field"]
    assert data["segments"][0]["data"] == [{"valid_time": "2026-05-06T00:00:00Z", "value": 10.0}]


@pytest.mark.asyncio
async def test_forecast_series_multi_source_latest_returns_per_source_metadata() -> None:
    store = InMemoryForecastSeriesStore()
    app.dependency_overrides[get_forecast_store] = lambda: store

    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series?river_network_version_id=rnv_v1"
        "&issue_time=latest&variables=q_down&scenarios=GFS,IFS"
    )

    assert response.status_code == 200
    data = response.json()
    series_by_scenario = {series["scenario_id"]: series for series in data["series"]}
    assert data["issue_time"] == "2026-05-07T18:00:00Z"
    assert set(series_by_scenario) == {"forecast_gfs_deterministic", "forecast_ifs_deterministic"}
    assert series_by_scenario["forecast_gfs_deterministic"]["source_id"] == "GFS"
    assert series_by_scenario["forecast_gfs_deterministic"]["cycle_time"] == "2026-05-07T00:00:00Z"
    assert series_by_scenario["forecast_ifs_deterministic"]["source_id"] == "IFS"
    assert series_by_scenario["forecast_ifs_deterministic"]["cycle_time"] == "2026-05-07T18:00:00Z"
    assert series_by_scenario["forecast_ifs_deterministic"]["available_lead_hours"] == 144
    assert series_by_scenario["forecast_gfs_deterministic"]["points"] == [
        [_timestamp_ms(_dt("2026-05-07T00:00:00Z")), 11.0]
    ]
    assert store.forecast_fetches[-1]["cycle_times_by_scenario"] == store.latest_cycles


@pytest.mark.asyncio
async def test_forecast_series_empty_store_path_returns_null_frequency_thresholds() -> None:
    store = InMemoryForecastSeriesStore()
    store.latest_cycles = {"forecast_gfs_deterministic": _dt("2026-05-07T00:00:00Z")}
    store.forecast_rows = []
    app.dependency_overrides[get_forecast_store] = lambda: store

    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series?river_network_version_id=rnv_v1"
        "&issue_time=latest&variables=q_down&scenarios=GFS"
    )

    assert response.status_code == 200
    data = response.json()
    assert data["series"] == []
    assert data["frequency_thresholds"] is None
    assert store.forecast_fetches[-1]["cycle_times_by_scenario"] == store.latest_cycles


@pytest.mark.asyncio
async def test_forecast_series_empty_no_latest_data_response_allows_null_issue_time() -> None:
    store = InMemoryForecastSeriesStore()
    store.latest_cycles = {}
    app.dependency_overrides[get_forecast_store] = lambda: store

    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series?river_network_version_id=rnv_v1"
        "&issue_time=latest&variables=q_down&scenarios=GFS"
    )

    assert response.status_code == 200
    data = response.json()
    assert data["issue_time"] is None
    assert data["series"] == []
    assert data["frequency_thresholds"] is None


@pytest.mark.asyncio
async def test_forecast_series_empty_spliced_no_latest_data_response_allows_null_issue_time() -> None:
    store = InMemoryForecastSeriesStore()
    store.latest_cycles = {}
    store.latest_analysis_issue_time = None
    app.dependency_overrides[get_forecast_store] = lambda: store

    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series?river_network_version_id=rnv_v1"
        "&issue_time=latest&variables=q_down&scenarios=GFS&include_analysis=true"
    )

    assert response.status_code == 200
    data = response.json()
    assert data["issue_time"] is None
    assert data["segments"] == []
    assert data["variable"] == "discharge"


@pytest.mark.asyncio
async def test_forecast_series_include_analysis_multi_source_has_one_analysis_segment() -> None:
    store = InMemoryForecastSeriesStore()
    app.dependency_overrides[get_forecast_store] = lambda: store

    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series?river_network_version_id=rnv_v1"
        "&issue_time=latest&variables=q_down&scenarios=GFS,IFS&include_analysis=true"
    )

    assert response.status_code == 200
    data = response.json()
    analysis_segments = [segment for segment in data["segments"] if segment["scenario_id"] == "analysis_true_field"]
    forecast_segments = [segment for segment in data["segments"] if segment["scenario_id"] != "analysis_true_field"]
    assert len(analysis_segments) == 1
    assert analysis_segments[0]["segment_role"] == "past_7_days"
    assert "source_id" not in analysis_segments[0]
    assert "cycle_time" not in analysis_segments[0]
    assert {segment["scenario_id"] for segment in forecast_segments} == {
        "forecast_gfs_deterministic",
        "forecast_ifs_deterministic",
    }
    assert all(segment["segment_role"] == "future_7_days" for segment in forecast_segments)
    assert {segment["source_id"] for segment in forecast_segments} == {"GFS", "IFS"}
    assert store.forecast_fetches[-1]["cycle_times_by_scenario"] == store.latest_cycles


def test_forecast_response_groups_multi_source_rows_with_metadata_and_points() -> None:
    gfs_cycle = _dt("2026-05-07T00:00:00Z")
    ifs_cycle = _dt("2026-05-07T06:00:00Z")
    payload = _forecast_response_from_rows(
        segment_id="seg_001",
        issue_time=ifs_cycle,
        rows=[
            {
                "scenario_id": "forecast_gfs_deterministic",
                "source_id": "gfs",
                "cycle_time": gfs_cycle,
                "valid_time": gfs_cycle,
                "value": 11.0,
                "unit": "m3/s",
            },
            {
                "scenario_id": "forecast_ifs_deterministic",
                "source_id": "IFS",
                "cycle_time": ifs_cycle,
                "valid_time": ifs_cycle,
                "value": 12.0,
                "unit": "m3/s",
            },
        ],
    )

    series_by_scenario = {series["scenario_id"]: series for series in payload["series"]}
    assert series_by_scenario["forecast_gfs_deterministic"]["source_id"] == "GFS"
    assert series_by_scenario["forecast_gfs_deterministic"]["available_lead_hours"] == 168
    assert series_by_scenario["forecast_ifs_deterministic"]["cycle_time"] == "2026-05-07T06:00:00Z"
    assert series_by_scenario["forecast_ifs_deterministic"]["available_lead_hours"] == 144
    assert series_by_scenario["forecast_gfs_deterministic"]["points"] == [[_timestamp_ms(gfs_cycle), 11.0]]
    assert "segments" not in payload


def test_spliced_response_deduplicates_issue_time_boundary_and_uses_sources() -> None:
    issue_time = _dt("2026-05-07T00:00:00Z")
    payload = _spliced_response_from_rows(
        river_segment_id="seg_001",
        issue_time=issue_time,
        variable="discharge",
        analysis_rows=[
            {
                "scenario_id": "analysis_true_field",
                "source_id": "ERA5",
                "valid_time": issue_time - timedelta(days=1),
                "value": 10.0,
                "unit": "m3/s",
            },
            {
                "scenario_id": "analysis_true_field",
                "source_id": "ERA5",
                "valid_time": issue_time,
                "value": 10.5,
                "unit": "m3/s",
            },
        ],
        forecast_rows=[
            {
                "scenario_id": "forecast_gfs_deterministic",
                "source_id": "gfs",
                "valid_time": issue_time,
                "value": 11.0,
                "unit": "m3/s",
            }
        ],
    )

    assert payload["segments"][0]["source"] == "ERA5"
    assert payload["segments"][1]["source"] == "GFS"
    assert payload["segments"][0]["data"] == [{"valid_time": "2026-05-06T00:00:00Z", "value": 10.0}]
    assert payload["segments"][1]["data"] == [{"valid_time": "2026-05-07T00:00:00Z", "value": 11.0}]


def test_analysis_window_for_issue_time_uses_open_end_seven_day_range() -> None:
    issue_time = _dt("2026-05-07T00:00:00Z")
    start_time, end_time = analysis_window_for_issue_time(issue_time)

    assert start_time == _dt("2026-04-30T00:00:00Z")
    assert end_time == issue_time


@pytest.mark.asyncio
async def test_forecast_series_segment_not_found_uses_unified_error(fake_store: FakeForecastStore) -> None:
    response = await _get(
        "/api/v1/basin-versions/basin_v1/river-segments/missing/forecast-series?river_network_version_id=rnv_v1"
    )

    assert fake_store is not None
    assert response.status_code == 404
    data = response.json()
    assert data["status"] == "error"
    assert data["request_id"]
    assert data["error"]["code"] == "SEGMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_forecast_series_requires_river_network_version_id(fake_store: FakeForecastStore) -> None:
    response = await _get("/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series")

    assert fake_store is not None
    assert response.status_code == 422
    assert fake_store.forecast_calls == []


def test_forecast_series_duplicate_segment_filters_forecast_analysis_and_latest_by_selected_network() -> None:
    issue_time = _dt("2026-05-07T00:00:00Z")
    selected_rows = [
        {
            "scenario_id": "forecast_gfs_deterministic",
            "model_id": "model_selected",
            "source_id": "GFS",
            "cycle_time": issue_time,
            "run_end_time": issue_time + timedelta(days=7),
            "lineage_json": {},
            "river_network_version_id": "rnv_selected",
            "valid_time": issue_time,
            "value": 11.0,
            "unit": "m3/s",
        }
    ]
    store = SqlCaptureForecastStore(
        [
            [{"basin_version_id": "basin_v1"}],
            [{"river_segment_id": "seg_001", "river_network_version_id": "rnv_selected", "properties_json": {}}],
            [{"scenario_id": "forecast_gfs_deterministic", "cycle_time": issue_time}],
            [],
            selected_rows,
            [],
        ]
    )

    response = store.forecast_series(
        basin_version_id="basin_v1",
        segment_id="seg_001",
        river_network_version_id="rnv_selected",
        issue_time="latest",
        variables=["q_down"],
        scenarios=["GFS"],
        include_analysis=True,
    )

    assert response["segments"] == [
        {
            "scenario": "forecast_gfs_deterministic",
            "scenario_id": "forecast_gfs_deterministic",
            "segment_role": "future_7_days",
            "source": "GFS",
            "source_id": "GFS",
            "cycle_time": "2026-05-07T00:00:00Z",
            "available_lead_hours": 168,
            "data": [{"valid_time": "2026-05-07T00:00:00Z", "value": 11.0}],
        }
    ]
    assert response["frequency_thresholds"] is None
    statements = [statement for statement, _parameters in store.cursor.executions]
    assert statements[1].count("rs.river_network_version_id = %s") == 1
    assert all(
        "rt.river_network_version_id = %s" in statement for statement in (statements[2], statements[3], statements[4])
    )
    assert all("rnv_selected" in parameters for _statement, parameters in store.cursor.executions[1:5])


def test_forecast_series_duplicate_segment_filters_hindcast_latest_and_rows_by_selected_network() -> None:
    end_time = _dt("1993-01-08T00:00:00Z")
    store = SqlCaptureForecastStore(
        [
            [{"basin_version_id": "basin_v1"}],
            [{"river_segment_id": "seg_001", "river_network_version_id": "rnv_selected", "properties_json": {}}],
            [{"valid_time": end_time}],
            [
                {
                    "scenario_id": "hindcast_replay",
                    "model_id": "model_selected",
                    "source_id": "ERA5",
                    "cycle_time": None,
                    "run_end_time": end_time,
                    "lineage_json": {},
                    "river_network_version_id": "rnv_selected",
                    "valid_time": end_time,
                    "value": 42.0,
                    "unit": "m3/s",
                }
            ],
            [],
        ]
    )

    response = store.forecast_series(
        basin_version_id="basin_v1",
        segment_id="seg_001",
        river_network_version_id="rnv_selected",
        issue_time="latest",
        variables=["q_down"],
        scenarios=["GFS"],
        run_types=["hindcast"],
    )

    assert response["series"][0]["scenario_id"] == "hindcast_replay"
    statements = [statement for statement, _parameters in store.cursor.executions]
    assert "rt.river_network_version_id = %s" in statements[2]
    assert "rt.river_network_version_id = %s" in statements[3]
    assert all("rnv_selected" in parameters for _statement, parameters in store.cursor.executions[1:4])


@pytest.mark.asyncio
async def test_run_list_uses_offset_limit_pagination_and_caps_limit(fake_store: FakeForecastStore) -> None:
    response = await _get("/api/v1/runs?basin_id=yangtze&source=gfs&status=parsed&limit=1000&offset=20")

    assert response.status_code == 200
    envelope = response.json()
    assert set(envelope) == {"request_id", "status", "data"}
    assert envelope["status"] == "ok"
    data = envelope["data"]
    assert data["total"] == 1
    assert data["total_count"] == 1
    assert data["limit"] == 200
    assert data["offset"] == 20
    assert fake_store.run_calls[-1]["basin_id"] == "yangtze"
    assert fake_store.run_calls[-1]["source"] == "gfs"
    assert fake_store.run_calls[-1]["status"] == "parsed"


@pytest.mark.asyncio
async def test_run_list_forwards_flood_product_ready_filter(fake_store: FakeForecastStore) -> None:
    response = await _get("/api/v1/runs?status=frequency_done&flood_product_ready=true&limit=50")

    assert response.status_code == 200
    assert fake_store.run_calls[-1]["flood_product_ready"] is True


def test_list_runs_marks_and_filters_flood_product_readiness() -> None:
    ready_run = {
        "run_id": "run_ready",
        "status": "frequency_done",
        "cycle_time": _dt("2026-05-07T00:00:00Z"),
        "created_at": _dt("2026-05-07T01:00:00Z"),
        "flood_quality_max_over_window": True,
        "flood_result_rows": 2,
        "flood_return_period_rows": 2,
        "flood_warning_rows": 2,
    }
    warning_unavailable_run = {
        "run_id": "run_warning_unavailable",
        "status": "frequency_done",
        "cycle_time": _dt("2026-05-07T00:00:00Z"),
        "created_at": _dt("2026-05-07T01:00:00Z"),
        "flood_quality_max_over_window": True,
        "flood_result_rows": 2,
        "flood_return_period_rows": 2,
        "flood_warning_rows": 0,
    }
    store = SqlCaptureForecastStore(
        [[{"total_count": 1}], [ready_run], [{"total_count": 2}], [ready_run, warning_unavailable_run]]
    )

    ready_page = store.list_runs(
        basin_id=None,
        source=None,
        cycle_time=None,
        status="frequency_done",
        flood_product_ready=True,
        limit=50,
        offset=0,
    )
    unfiltered_page = store.list_runs(
        basin_id=None,
        source=None,
        cycle_time=None,
        status="frequency_done",
        limit=50,
        offset=0,
    )

    ready_sql = store.cursor.executions[0][0]
    assert "h.status IN ('frequency_done', 'published')" in ready_sql
    assert "return_period_result" in ready_sql
    assert ready_page["items"][0]["product_quality"]["flood_return_period"]["quality_state"] == "ready"
    qualities = {
        item["run_id"]: item["product_quality"]["flood_return_period"]
        for item in unfiltered_page["items"]
    }
    assert qualities["run_ready"]["quality_state"] == "ready"
    assert qualities["run_warning_unavailable"]["quality_state"] == "unavailable"
    assert qualities["run_warning_unavailable"]["unavailable_products"] == ["warning_thresholds"]


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
