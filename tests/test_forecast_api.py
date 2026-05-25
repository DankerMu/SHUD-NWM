from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType
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
    _PsycopgTransaction,
    _spliced_response_from_rows,
    analysis_window_for_issue_time,
)


class FakeForecastStore:
    def __init__(self) -> None:
        self.forecast_calls: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []
        self.station_series_calls: list[dict[str, Any]] = []
        self.latest_qhh_calls: list[str] = []
        self.latest_qhh_unavailable = False
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
        self.station_series_response = {
            "station_id": "qhh_stn_001",
            "station": {
                "station_id": "qhh_stn_001",
                "basin_version_id": "qhh_v2026",
                "station_name": "QHH Station 001",
                "name": "QHH Station 001",
                "longitude": 101.0,
                "latitude": 36.0,
                "elevation_m": 3200.0,
                "elevation": 3200.0,
                "station_role": "forcing_proxy",
                "active_flag": True,
                "properties_json": {"source": "fixture"},
            },
            "forcing_version_id": "forc_qhh_gfs_2026050700",
            "model_id": "qhh_shud_v1",
            "source_id": "GFS",
            "cycle_time": "2026-05-07T00:00:00Z",
            "valid_time_start": "2026-05-07T00:00:00Z",
            "valid_time_end": "2026-05-14T00:00:00Z",
            "limit": 2,
            "requested_from": "2026-05-07T00:00:00Z",
            "requested_to": "2026-05-07T03:00:00Z",
            "series": [
                {
                    "variable": "PRCP",
                    "unit": "mm/h",
                    "native_resolution": "1h",
                    "source_id": "GFS",
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "points": [
                        {
                            "valid_time": "2026-05-07T00:00:00Z",
                            "value": 1.0,
                            "quality_flag": "ok",
                            "source_id": "GFS",
                        },
                        {
                            "valid_time": "2026-05-07T01:00:00Z",
                            "value": 2.0,
                            "quality_flag": "warn",
                            "source_id": "GFS",
                        },
                    ],
                    "truncated": True,
                    "metadata": {
                        "limit": 2,
                        "returned_points": 2,
                        "requested_from": "2026-05-07T00:00:00Z",
                        "requested_to": "2026-05-07T03:00:00Z",
                        "returned_from": "2026-05-07T00:00:00Z",
                        "returned_to": "2026-05-07T01:00:00Z",
                        "truncated": True,
                    },
                },
                {
                    "variable": "TEMP",
                    "unit": "degC",
                    "native_resolution": "1h",
                    "source_id": "GFS",
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "points": [],
                    "truncated": False,
                    "metadata": {
                        "limit": 2,
                        "returned_points": 0,
                        "requested_from": "2026-05-07T00:00:00Z",
                        "requested_to": "2026-05-07T03:00:00Z",
                        "returned_from": None,
                        "returned_to": None,
                        "truncated": False,
                    },
                },
            ],
        }
        self.latest_qhh_response = {
            "basin_id": "basins_qhh",
            "model_id": "basins_qhh_shud",
            "basin_version_id": "basins_qhh_vbasins",
            "river_network_version_id": "basins_qhh_rivnet_vbasins",
            "source_id": "GFS",
            "cycle_time": "2026-05-07T00:00:00Z",
            "run_id": "qhh_gfs_2026050700",
            "forcing_version_id": "forc_qhh_gfs_2026050700_basins_qhh_shud",
            "station_count": 386,
            "expected_station_count": 386,
            "segment_count": 1633,
            "expected_segment_count": 1633,
            "status": "ready",
            "run_status": "frequency_done",
            "valid_time_start": "2026-05-07T00:00:00Z",
            "valid_time_end": "2026-05-14T00:00:00Z",
            "river_valid_time_start": "2026-05-07T00:00:00Z",
            "river_valid_time_end": "2026-05-14T00:00:00Z",
            "forcing_valid_time_start": "2026-05-07T00:00:00Z",
            "forcing_valid_time_end": "2026-05-14T00:00:00Z",
            "available_horizon_hours": 168,
            "expected_horizon_hours": 168,
            "shorter_horizon": False,
            "availability": {
                "ready": True,
                "unavailable_reasons": [],
                "quality_flags": [],
                "quality_notes": [],
            },
            "quality": {
                "station_sample_count": 1000,
                "river_sample_count": 2000,
                "required_station_variables": ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"],
                "station_variable_coverage": [],
                "candidate_limit": 100,
                "query_indexes": [],
            },
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

    def station_series(self, **kwargs: Any) -> dict[str, Any]:
        self.station_series_calls.append(kwargs)
        if kwargs["station_id"] == "missing":
            raise ForecastStoreError(
                status_code=404,
                code="STATION_NOT_FOUND",
                message="Station not found: missing",
                details={"station_id": "missing"},
            )
        if kwargs.get("forcing_version_id") == "missing":
            raise ForecastStoreError(
                status_code=404,
                code="FORCING_VERSION_NOT_FOUND",
                message="Forcing version not found: missing",
                details={"forcing_version_id": "missing"},
            )
        if kwargs.get("forcing_version_id") == "not_finalized":
            raise ForecastStoreError(
                status_code=409,
                code="FORCING_VERSION_NOT_FINALIZED",
                message="Forcing version is not finalized and cannot be used for station forcing reads.",
                details={"forcing_version_id": "not_finalized", "checksum_state": "pending"},
            )
        if kwargs.get("forcing_version_id") == "station_absent":
            raise ForecastStoreError(
                status_code=404,
                code="STATION_NOT_IN_FORCING_VERSION",
                message="Station has no finalized forcing samples for the selected forcing version.",
                details={"station_id": kwargs["station_id"], "forcing_version_id": "station_absent"},
            )
        if kwargs.get("forcing_version_id") and kwargs.get("source_id") == "IFS":
            raise ForecastStoreError(
                status_code=409,
                code="FORCING_VERSION_FILTER_CONFLICT",
                message="forcing_version_id conflicts with supplied model_id, source_id, or cycle_time.",
                details={
                    "forcing_version_id": kwargs["forcing_version_id"],
                    "conflicts": [{"field": "source_id", "supplied": "ifs", "selected": "gfs"}],
                },
            )
        if kwargs.get("model_id") == "ambiguous":
            raise ForecastStoreError(
                status_code=409,
                code="FORCING_VERSION_AMBIGUOUS",
                message="Multiple forcing versions match model_id, source_id, and cycle_time.",
                details={"candidates": [{"forcing_version_id": "forc_a"}, {"forcing_version_id": "forc_b"}]},
            )
        if kwargs.get("variables") and "unknown" in ",".join(str(value) for value in kwargs["variables"]):
            raise ForecastStoreError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="Invalid station forcing variable.",
                details={"field": "variables", "rejected_values": ["unknown"]},
            )
        if kwargs.get("from_time") and kwargs.get("to_time") and kwargs["from_time"] > kwargs["to_time"]:
            raise ForecastStoreError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="from must be earlier than or equal to to.",
                details={
                    "from": kwargs["from_time"].isoformat().replace("+00:00", "Z"),
                    "to": kwargs["to_time"].isoformat().replace("+00:00", "Z"),
                },
            )
        response = dict(self.station_series_response)
        if kwargs.get("model_id") and not kwargs.get("forcing_version_id"):
            response["forcing_version_id"] = "forc_resolved_from_tuple"
        if kwargs.get("variables") is None:
            response["series"] = [
                {"variable": variable, "unit": None, "native_resolution": None, "points": [], "truncated": False}
                for variable in ("PRCP", "TEMP", "RH", "wind", "Rn", "Press")
            ]
        return response

    def latest_qhh_display_product(self, source: str) -> dict[str, Any]:
        self.latest_qhh_calls.append(source)
        if str(source).strip().upper() not in {"GFS", "IFS"}:
            reflected_source = str(source)
            if len(reflected_source) > 64:
                reflected_source = f"{reflected_source[:61]}..."
            raise ForecastStoreError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="source must be GFS or IFS.",
                details={"field": "source", "rejected_value": reflected_source, "allowed_values": ["GFS", "IFS"]},
            )
        if self.latest_qhh_unavailable:
            raise ForecastStoreError(
                status_code=404,
                code="QHH_LATEST_PRODUCT_UNAVAILABLE",
                message="No usable latest QHH display product is available for source GFS.",
                details={
                    "source_id": str(source).strip().upper(),
                    "basin_id": "basins_qhh",
                    "status": "unavailable",
                    "unavailable_reasons": [{"code": "Q_DOWN_MISSING", "message": "No river q_down samples."}],
                },
            )
        response = dict(self.latest_qhh_response)
        response["source_id"] = str(source).strip().upper()
        if response["source_id"] == "IFS":
            response["available_horizon_hours"] = 144
            response["valid_time_end"] = "2026-05-13T00:00:00Z"
            response["river_valid_time_end"] = "2026-05-13T00:00:00Z"
            response["forcing_valid_time_end"] = "2026-05-13T00:00:00Z"
            response["shorter_horizon"] = True
            response["availability"] = {
                "ready": True,
                "unavailable_reasons": [],
                "quality_flags": ["shorter_horizon"],
                "quality_notes": [
                    {
                        "code": "SHORTER_HORIZON",
                        "message": "Available horizon is shorter than the default seven-day display window.",
                        "expected_horizon_hours": 168,
                        "available_horizon_hours": 144,
                        "available_end_time": "2026-05-13T00:00:00Z",
                    }
                ],
            }
        return response


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


def test_psycopg_transaction_uses_readonly_repeatable_read_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    class FakeDatabaseError(Exception):
        pass

    class FakeRealDictCursor:
        pass

    class FakeConnection:
        def set_session(self, *, isolation_level: str, readonly: bool, autocommit: bool) -> None:
            calls.append(("set_session", isolation_level, readonly, autocommit))

        def cursor(self, *, cursor_factory: type[FakeRealDictCursor]) -> str:
            calls.append(("cursor", cursor_factory))
            return "fake-cursor"

        def commit(self) -> None:
            calls.append(("commit",))

        def rollback(self) -> None:
            calls.append(("rollback",))

        def close(self) -> None:
            calls.append(("close",))

    fake_connection = FakeConnection()
    fake_psycopg2 = ModuleType("psycopg2")
    fake_extras = ModuleType("psycopg2.extras")

    def connect(database_url: str) -> FakeConnection:
        calls.append(("connect", database_url))
        return fake_connection

    def register_default_json(*, conn_or_curs: FakeConnection) -> None:
        calls.append(("register_default_json", conn_or_curs))

    def register_default_jsonb(*, conn_or_curs: FakeConnection) -> None:
        calls.append(("register_default_jsonb", conn_or_curs))

    fake_psycopg2.connect = connect
    fake_psycopg2.Error = FakeDatabaseError
    fake_psycopg2.extras = fake_extras
    fake_extras.RealDictCursor = FakeRealDictCursor
    fake_extras.register_default_json = register_default_json
    fake_extras.register_default_jsonb = register_default_jsonb
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)

    with _PsycopgTransaction("postgresql://unit-test") as cursor:
        assert cursor == "fake-cursor"
        assert calls == [
            ("connect", "postgresql://unit-test"),
            ("set_session", "REPEATABLE READ", True, False),
            ("register_default_json", fake_connection),
            ("register_default_jsonb", fake_connection),
            ("cursor", FakeRealDictCursor),
        ]

    assert calls[-2:] == [("commit",), ("close",)]


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


def test_station_series_explicit_forcing_version_groups_rows_and_truncates_per_variable() -> None:
    from_time = _dt("2026-05-07T00:00:00Z")
    to_time = _dt("2026-05-07T03:00:00Z")
    store = SqlCaptureForecastStore(
        [
            [_station_row()],
            [_forcing_version_row()],
            [{"present": 1}],
            [
                _station_series_row("PRCP", from_time, 1.0, row_number=1, quality_flag="ok"),
                _station_series_row("PRCP", from_time + timedelta(hours=1), 2.0, row_number=2, quality_flag="warn"),
                _station_series_row("PRCP", from_time + timedelta(hours=2), 3.0, row_number=3),
                _station_series_row("TEMP", from_time, 11.0, row_number=1, unit="degC", native_resolution="3h"),
            ],
        ]
    )

    response = store.station_series(
        station_id="qhh_stn_001",
        forcing_version_id="forc_qhh_gfs_2026050700",
        variables=["PRCP,TEMP"],
        from_time=from_time,
        to_time=to_time,
        limit=2,
    )

    series_by_variable = {series["variable"]: series for series in response["series"]}
    assert response["station_id"] == "qhh_stn_001"
    assert response["forcing_version_id"] == "forc_qhh_gfs_2026050700"
    assert response["source_id"] == "GFS"
    assert response["cycle_time"] == "2026-05-07T00:00:00Z"
    assert list(series_by_variable) == ["PRCP", "TEMP"]
    assert series_by_variable["PRCP"]["unit"] == "mm/h"
    assert series_by_variable["PRCP"]["native_resolution"] == "1h"
    assert series_by_variable["PRCP"]["truncated"] is True
    assert series_by_variable["PRCP"]["points"] == [
        {"valid_time": "2026-05-07T00:00:00Z", "value": 1.0, "quality_flag": "ok", "source_id": "GFS"},
        {"valid_time": "2026-05-07T01:00:00Z", "value": 2.0, "quality_flag": "warn", "source_id": "GFS"},
    ]
    assert series_by_variable["PRCP"]["metadata"] == {
        "limit": 2,
        "returned_points": 2,
        "requested_from": "2026-05-07T00:00:00Z",
        "requested_to": "2026-05-07T03:00:00Z",
        "returned_from": "2026-05-07T00:00:00Z",
        "returned_to": "2026-05-07T01:00:00Z",
        "truncated": True,
    }
    assert series_by_variable["TEMP"]["unit"] == "degC"
    assert series_by_variable["TEMP"]["native_resolution"] == "3h"
    assert series_by_variable["TEMP"]["truncated"] is False
    membership_statement, membership_parameters = store.cursor.executions[2]
    assert "FROM met.forcing_station_timeseries" in membership_statement
    assert "LIMIT 1" in membership_statement
    assert membership_parameters == (
        "forc_qhh_gfs_2026050700",
        "qhh_stn_001",
        from_time,
        _dt("2026-05-14T00:00:00Z"),
    )
    statement, parameters = store.cursor.executions[3]
    assert "fst.forcing_version_id = %s" in statement
    assert "fst.station_id = %s" in statement
    assert "fst.variable = requested.variable" in statement
    assert "fst.valid_time >= %s" in statement
    assert "fst.valid_time <= %s" in statement
    assert parameters == (
        ["PRCP", "TEMP"],
        "forc_qhh_gfs_2026050700",
        "qhh_stn_001",
        from_time,
        _dt("2026-05-14T00:00:00Z"),
        from_time,
        to_time,
        3,
    )


def test_station_series_resolves_model_source_cycle_to_selected_forcing_version() -> None:
    cycle_time = _dt("2026-05-07T00:00:00Z")
    store = SqlCaptureForecastStore(
        [
            [_station_row()],
            [_forcing_version_row()],
            [{"present": 1}],
            [_station_series_row("RH", cycle_time, 78.0, row_number=1, unit="%")],
        ]
    )

    response = store.station_series(
        station_id="qhh_stn_001",
        model_id="qhh_shud_v1",
        source_id="gfs",
        cycle_time="2026-05-07T00:00:00Z",
        variables=["RH"],
        limit=10,
    )

    assert response["forcing_version_id"] == "forc_qhh_gfs_2026050700"
    assert response["series"][0]["points"] == [
        {"valid_time": "2026-05-07T00:00:00Z", "value": 78.0, "quality_flag": "ok", "source_id": "GFS"}
    ]
    statement, parameters = store.cursor.executions[1]
    assert "LOWER(source_id) = %s" in statement
    assert "LIMIT 2" in statement
    assert parameters == ("qhh_shud_v1", "gfs", cycle_time)


def test_station_series_accepts_string_variable_filter_without_character_splitting() -> None:
    cycle_time = _dt("2026-05-07T00:00:00Z")
    store = SqlCaptureForecastStore(
        [
            [_station_row()],
            [_forcing_version_row()],
            [{"present": 1}],
            [_station_series_row("PRCP", cycle_time, 5.0, row_number=1)],
        ]
    )

    response = store.station_series(
        station_id="qhh_stn_001",
        forcing_version_id="forc_qhh_gfs_2026050700",
        variables="PRCP",
    )

    assert [series["variable"] for series in response["series"]] == ["PRCP"]
    assert store.cursor.executions[3][1][:3] == (["PRCP"], "forc_qhh_gfs_2026050700", "qhh_stn_001")


@pytest.mark.parametrize(
    ("kwargs", "details_field"),
    [
        ({"variables": ["TEMP,unknown"]}, "variables"),
        ({"limit": 0}, "limit"),
        (
            {
                "from_time": "2026-05-08T00:00:00Z",
                "to_time": "2026-05-07T00:00:00Z",
            },
            None,
        ),
    ],
)
def test_station_series_validates_variables_limit_and_time_range(
    kwargs: dict[str, Any], details_field: str | None
) -> None:
    store = SqlCaptureForecastStore()

    with pytest.raises(ForecastStoreError) as error:
        store.station_series(
            station_id="qhh_stn_001",
            forcing_version_id="forc_qhh_gfs_2026050700",
            **kwargs,
        )

    assert error.value.status_code == 422
    assert error.value.code == "VALIDATION_ERROR"
    if details_field is not None:
        assert error.value.details["field"] == details_field
    assert store.cursor.executions == []


def test_station_series_raises_stable_errors_for_missing_station_and_forcing_version() -> None:
    missing_station_store = SqlCaptureForecastStore([[]])
    with pytest.raises(ForecastStoreError) as missing_station:
        missing_station_store.station_series(station_id="missing", forcing_version_id="forc_qhh_gfs_2026050700")
    assert missing_station.value.status_code == 404
    assert missing_station.value.code == "STATION_NOT_FOUND"

    missing_forcing_store = SqlCaptureForecastStore([[_station_row()], []])
    with pytest.raises(ForecastStoreError) as missing_forcing:
        missing_forcing_store.station_series(station_id="qhh_stn_001", forcing_version_id="missing")
    assert missing_forcing.value.status_code == 404
    assert missing_forcing.value.code == "FORCING_VERSION_NOT_FOUND"

    missing_resolved_forcing_store = SqlCaptureForecastStore([[_station_row()], []])
    with pytest.raises(ForecastStoreError) as missing_resolved_forcing:
        missing_resolved_forcing_store.station_series(
            station_id="qhh_stn_001",
            model_id="qhh_shud_v1",
            source_id="gfs",
            cycle_time="2026-05-07T00:00:00Z",
        )
    assert missing_resolved_forcing.value.status_code == 404
    assert missing_resolved_forcing.value.code == "FORCING_VERSION_NOT_FOUND"
    assert missing_resolved_forcing.value.details == {
        "model_id": "qhh_shud_v1",
        "source_id": "gfs",
        "cycle_time": "2026-05-07T00:00:00Z",
    }


def test_station_series_raises_stable_error_for_ambiguous_model_source_cycle_resolution() -> None:
    cycle_time = _dt("2026-05-07T00:00:00Z")
    store = SqlCaptureForecastStore(
        [
            [_station_row()],
            [
                _forcing_version_row(forcing_version_id="forc_qhh_gfs_2026050700"),
                _forcing_version_row(forcing_version_id="forc_qhh_gfs_2026050700_rebuild"),
                _forcing_version_row(forcing_version_id="forc_qhh_gfs_2026050700_third"),
            ],
        ]
    )

    with pytest.raises(ForecastStoreError) as error:
        store.station_series(
            station_id="qhh_stn_001",
            model_id="qhh_shud_v1",
            source_id="gfs",
            cycle_time=cycle_time,
        )

    assert error.value.status_code == 409
    assert error.value.code == "FORCING_VERSION_AMBIGUOUS"
    assert error.value.details["candidates"] == [
        {"forcing_version_id": "forc_qhh_gfs_2026050700", "created_at": "2026-05-07T00:30:00Z"},
        {"forcing_version_id": "forc_qhh_gfs_2026050700_rebuild", "created_at": "2026-05-07T00:30:00Z"},
    ]
    assert len(error.value.details["candidates"]) == 2
    statement, _parameters = store.cursor.executions[1]
    assert "LIMIT 2" in statement


@pytest.mark.parametrize(
    ("kwargs", "expected_conflict"),
    [
        ({"source_id": "ifs"}, {"field": "source_id", "supplied": "ifs", "selected": "gfs"}),
        (
            {"cycle_time": "2026-05-07T06:00:00+06:00"},
            {
                "field": "cycle_time",
                "supplied": "2026-05-07T00:00:00Z",
                "selected": "2026-05-07T00:00:00Z",
            },
        ),
        (
            {"cycle_time": "2026-05-07T06:00:00Z"},
            {
                "field": "cycle_time",
                "supplied": "2026-05-07T06:00:00Z",
                "selected": "2026-05-07T00:00:00Z",
            },
        ),
    ],
)
def test_station_series_explicit_forcing_version_validates_redundant_tuple_filters(
    kwargs: dict[str, Any], expected_conflict: dict[str, Any]
) -> None:
    store = SqlCaptureForecastStore([[_station_row()], [_forcing_version_row()]])

    if expected_conflict["supplied"] == expected_conflict["selected"]:
        store.cursor.rows_by_statement.append([{"present": 1}])
        store.cursor.rows_by_statement.append([])
        response = store.station_series(
            station_id="qhh_stn_001",
            forcing_version_id="forc_qhh_gfs_2026050700",
            variables=["PRCP"],
            **kwargs,
        )
        assert response["series"][0]["points"] == []
        return

    with pytest.raises(ForecastStoreError) as error:
        store.station_series(
            station_id="qhh_stn_001",
            forcing_version_id="forc_qhh_gfs_2026050700",
            variables=["PRCP"],
            **kwargs,
        )

    assert error.value.status_code == 409
    assert error.value.code == "FORCING_VERSION_FILTER_CONFLICT"
    assert expected_conflict in error.value.details["conflicts"]
    assert len(store.cursor.executions) == 2


@pytest.mark.parametrize(
    ("checksum", "kwargs"),
    [
        (None, {"forcing_version_id": "forc_qhh_gfs_2026050700"}),
        (
            "pending",
            {
                "model_id": "qhh_shud_v1",
                "source_id": "GFS",
                "cycle_time": "2026-05-07T08:00:00+08:00",
            },
        ),
    ],
)
def test_station_series_rejects_not_finalized_forcing_versions(
    checksum: str | None, kwargs: dict[str, Any]
) -> None:
    forcing_rows = [_forcing_version_row(checksum=checksum)]
    store = SqlCaptureForecastStore([[_station_row()], forcing_rows])

    with pytest.raises(ForecastStoreError) as error:
        store.station_series(station_id="qhh_stn_001", variables=["PRCP"], **kwargs)

    assert error.value.status_code == 409
    assert error.value.code == "FORCING_VERSION_NOT_FINALIZED"
    assert error.value.details["forcing_version_id"] == "forc_qhh_gfs_2026050700"
    assert len(store.cursor.executions) == 2


def test_station_series_rejects_station_absent_from_selected_forcing_version() -> None:
    store = SqlCaptureForecastStore([[_station_row()], [_forcing_version_row()], []])

    with pytest.raises(ForecastStoreError) as error:
        store.station_series(
            station_id="qhh_stn_001",
            forcing_version_id="forc_qhh_gfs_2026050700",
            variables=["PRCP"],
        )

    assert error.value.status_code == 404
    assert error.value.code == "STATION_NOT_IN_FORCING_VERSION"
    assert error.value.details == {
        "station_id": "qhh_stn_001",
        "forcing_version_id": "forc_qhh_gfs_2026050700",
        "valid_time_start": "2026-05-07T00:00:00Z",
        "valid_time_end": "2026-05-14T00:00:00Z",
    }


def test_station_series_valid_station_with_time_filter_outside_rows_returns_empty_series() -> None:
    store = SqlCaptureForecastStore([[_station_row()], [_forcing_version_row()], [{"present": 1}], []])

    response = store.station_series(
        station_id="qhh_stn_001",
        forcing_version_id="forc_qhh_gfs_2026050700",
        variables=["PRCP"],
        from_time="2026-05-15T00:00:00Z",
        to_time="2026-05-15T01:00:00Z",
    )

    assert response["series"][0]["points"] == []
    assert response["series"][0]["metadata"]["requested_from"] == "2026-05-15T00:00:00Z"
    assert response["series"][0]["metadata"]["requested_to"] == "2026-05-15T01:00:00Z"


def test_station_series_excludes_out_of_window_rows_before_points_and_truncation() -> None:
    start_time = _dt("2026-05-07T00:00:00Z")
    end_time = _dt("2026-05-14T00:00:00Z")
    store = SqlCaptureForecastStore(
        [
            [_station_row()],
            [_forcing_version_row()],
            [{"present": 1}],
            [
                _station_series_row("PRCP", start_time - timedelta(hours=1), 99.0, row_number=1),
                _station_series_row("PRCP", start_time, 1.0, row_number=2),
                _station_series_row("PRCP", end_time + timedelta(hours=1), 100.0, row_number=3),
            ],
        ]
    )

    response = store.station_series(
        station_id="qhh_stn_001",
        forcing_version_id="forc_qhh_gfs_2026050700",
        variables=["PRCP"],
        limit=1,
    )

    series = response["series"][0]
    assert series["points"] == [
        {"valid_time": "2026-05-07T00:00:00Z", "value": 1.0, "quality_flag": "ok", "source_id": "GFS"}
    ]
    assert series["truncated"] is False
    assert series["metadata"]["returned_points"] == 1


def test_station_forcing_readiness_rejects_not_finalized_forcing_version() -> None:
    store = SqlCaptureForecastStore([[_forcing_version_row(checksum="pending")]])

    with pytest.raises(ForecastStoreError) as error:
        store.station_forcing_readiness(forcing_version_id="forc_qhh_gfs_2026050700")

    assert error.value.status_code == 409
    assert error.value.code == "FORCING_VERSION_NOT_FINALIZED"
    assert len(store.cursor.executions) == 1


def test_station_forcing_readiness_reports_qhh_like_coverage_and_index_outcome() -> None:
    store = SqlCaptureForecastStore(
        [
            [_forcing_version_row(station_count=386)],
            [
                {
                    "actual_station_count": 386,
                    "sample_count": 1200,
                    "valid_time_start": _dt("2026-05-07T00:00:00Z"),
                    "valid_time_end": _dt("2026-05-08T00:00:00Z"),
                }
            ],
            [
                _readiness_row("PRCP", station_count=386),
                _readiness_row("TEMP", station_count=386),
                _readiness_row("RH", station_count=386),
                _readiness_row("wind", station_count=386),
                _readiness_row("Rn", station_count=386, unit_count=0, missing_unit_samples=4),
            ],
        ]
    )

    response = store.station_forcing_readiness(
        forcing_version_id="forc_qhh_gfs_2026050700",
        expected_station_count=386,
    )

    coverage_by_variable = {item["variable"]: item for item in response["six_variable_coverage"]}
    reason_codes = {item["code"] for item in response["missing_data_reasons"]}
    assert response["forcing_version_id"] == "forc_qhh_gfs_2026050700"
    assert response["expected_station_count"] == 386
    assert response["actual_station_count"] == 386
    assert response["declared_station_count"] == 386
    assert response["required_variables"] == ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"]
    assert coverage_by_variable["PRCP"]["ready"] is True
    assert coverage_by_variable["Rn"]["missing_unit_samples"] == 4
    assert coverage_by_variable["Press"]["sample_count"] == 0
    assert {"UNIT_MISSING", "VARIABLE_MISSING"} <= reason_codes
    assert response["query_index"] == {
        "status": "covered_by_primary_key",
        "table": "met.forcing_station_timeseries",
        "index": "forcing_station_timeseries_pkey",
        "columns": ["forcing_version_id", "station_id", "variable", "valid_time"],
        "reason": (
            "Station-series reads constrain forcing_version_id and station_id before variable and valid_time, "
            "matching the source-of-truth primary key prefix; no additive index is required for #204."
        ),
    }
    assert response["ready"] is False


def test_station_forcing_readiness_without_expected_count_uses_declared_station_count() -> None:
    store = SqlCaptureForecastStore(
        [
            [_forcing_version_row(station_count=386)],
            [
                {
                    "actual_station_count": 385,
                    "sample_count": 1200,
                    "valid_time_start": _dt("2026-05-07T00:00:00Z"),
                    "valid_time_end": _dt("2026-05-08T00:00:00Z"),
                }
            ],
            [
                _readiness_row("PRCP", station_count=385),
                _readiness_row("TEMP", station_count=386),
                _readiness_row("RH", station_count=386),
                _readiness_row("wind", station_count=386),
                _readiness_row("Rn", station_count=386),
                _readiness_row("Press", station_count=386),
            ],
        ]
    )

    response = store.station_forcing_readiness(forcing_version_id="forc_qhh_gfs_2026050700")

    coverage_by_variable = {item["variable"]: item for item in response["six_variable_coverage"]}
    assert response["expected_station_count"] == 386
    assert response["actual_station_count"] == 385
    assert coverage_by_variable["PRCP"]["ready"] is False
    assert response["ready"] is False
    assert {
        ("STATION_COUNT_MISMATCH", None, 386, 385),
        ("VARIABLE_STATION_COUNT_MISMATCH", "PRCP", 386, 385),
    } <= {
        (reason["code"], reason.get("variable"), reason.get("expected"), reason.get("actual"))
        for reason in response["missing_data_reasons"]
    }


def test_station_forcing_readiness_missing_quality_flags_make_ready_false() -> None:
    store = SqlCaptureForecastStore(
        [
            [_forcing_version_row(station_count=386)],
            [
                {
                    "actual_station_count": 386,
                    "sample_count": 1200,
                    "valid_time_start": _dt("2026-05-07T00:00:00Z"),
                    "valid_time_end": _dt("2026-05-08T00:00:00Z"),
                }
            ],
            [
                _readiness_row("PRCP", station_count=386, quality_flag_count=1, missing_quality_flag_samples=3),
                _readiness_row("TEMP", station_count=386),
                _readiness_row("RH", station_count=386),
                _readiness_row("wind", station_count=386),
                _readiness_row("Rn", station_count=386),
                _readiness_row("Press", station_count=386),
            ],
        ]
    )

    response = store.station_forcing_readiness(forcing_version_id="forc_qhh_gfs_2026050700")

    coverage_by_variable = {item["variable"]: item for item in response["six_variable_coverage"]}
    assert coverage_by_variable["PRCP"]["ready"] is False
    assert response["ready"] is False
    assert {
        "code": "QUALITY_FLAG_MISSING",
        "variable": "PRCP",
        "missing_samples": 3,
    } in response["missing_data_reasons"]


def test_station_forcing_readiness_excludes_out_of_window_rows_from_sql_and_response() -> None:
    store = SqlCaptureForecastStore(
        [
            [_forcing_version_row(station_count=386)],
            [
                {
                    "actual_station_count": 386,
                    "sample_count": 600,
                    "valid_time_start": _dt("2026-05-07T00:00:00Z"),
                    "valid_time_end": _dt("2026-05-14T00:00:00Z"),
                }
            ],
            [
                _readiness_row(
                    "PRCP",
                    station_count=386,
                    valid_time_start=_dt("2026-05-07T00:00:00Z"),
                    valid_time_end=_dt("2026-05-14T00:00:00Z"),
                )
            ],
        ]
    )

    response = store.station_forcing_readiness(
        forcing_version_id="forc_qhh_gfs_2026050700",
        expected_station_count=386,
        required_variables=["PRCP"],
    )

    overall_statement, overall_parameters = store.cursor.executions[1]
    variable_statement, variable_parameters = store.cursor.executions[2]
    assert "valid_time >= %s" in overall_statement
    assert "valid_time <= %s" in overall_statement
    assert "valid_time >= %s" in variable_statement
    assert "valid_time <= %s" in variable_statement
    assert overall_parameters == (
        "forc_qhh_gfs_2026050700",
        _dt("2026-05-07T00:00:00Z"),
        _dt("2026-05-14T00:00:00Z"),
        ["PRCP"],
    )
    assert variable_parameters == overall_parameters
    assert response["valid_time_start"] == "2026-05-07T00:00:00Z"
    assert response["valid_time_end"] == "2026-05-14T00:00:00Z"
    assert response["ready"] is True


def test_latest_qhh_display_product_selects_ready_gfs_product_and_reports_identity_counts() -> None:
    store = SqlCaptureForecastStore(
        [[_qhh_candidate_row(cycle_time=_dt("2026-05-07T00:00:00Z"), source_id="gfs")]]
    )

    response = store.latest_qhh_display_product("gfs")

    assert {
        "basin_id": "basins_qhh",
        "model_id": "basins_qhh_shud",
        "basin_version_id": "basins_qhh_vbasins",
        "river_network_version_id": "basins_qhh_rivnet_vbasins",
        "source_id": "GFS",
        "cycle_time": "2026-05-07T00:00:00Z",
        "run_id": "qhh_gfs_2026050700",
        "forcing_version_id": "forc_qhh_gfs_2026050700_basins_qhh_shud",
        "station_count": 386,
        "expected_station_count": 386,
        "segment_count": 1633,
        "expected_segment_count": 1633,
        "status": "ready",
        "run_status": "frequency_done",
        "available_horizon_hours": 168,
        "expected_horizon_hours": 168,
        "shorter_horizon": False,
    }.items() <= response.items()
    assert response["availability"] == {
        "ready": True,
        "unavailable_reasons": [],
        "quality_flags": [],
        "quality_notes": [],
    }
    assert response["quality"]["required_station_variables"] == ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"]
    assert {item["index"] for item in response["quality"]["query_indexes"]} == {
        "hydro_run_qhh_latest_candidate_idx",
        "basin_version_qhh_latest_lookup_idx",
        "river_timeseries_qhh_latest_window_idx",
        "forcing_station_timeseries_qhh_latest_window_idx",
        "interp_weight_qhh_latest_membership_idx",
    }


def test_latest_qhh_display_product_selects_newest_ready_candidate_after_newer_unusable() -> None:
    newer_unusable = _qhh_candidate_row(
        run_id="qhh_gfs_2026050800",
        cycle_time=_dt("2026-05-08T00:00:00Z"),
        segment_count=0,
        river_sample_count=0,
        river_valid_time_start=None,
        river_valid_time_end=None,
    )
    older_ready = _qhh_candidate_row(
        run_id="qhh_gfs_2026050700",
        cycle_time=_dt("2026-05-07T00:00:00Z"),
    )
    store = SqlCaptureForecastStore([[newer_unusable, older_ready]])

    response = store.latest_qhh_display_product("GFS")

    assert response["run_id"] == "qhh_gfs_2026050700"
    assert response["cycle_time"] == "2026-05-07T00:00:00Z"
    assert response["status"] == "ready"


def test_latest_qhh_display_product_selects_ready_candidate_after_more_than_25_newer_unusable() -> None:
    unusable_candidates = [
        _qhh_candidate_row(
            run_id=f"qhh_gfs_202606{index + 1:02d}00",
            cycle_time=_dt("2026-06-01T00:00:00Z") - timedelta(hours=index),
            segment_count=0,
            river_sample_count=0,
            river_valid_time_start=None,
            river_valid_time_end=None,
        )
        for index in range(26)
    ]
    older_ready = _qhh_candidate_row(
        run_id="qhh_gfs_2026050700",
        cycle_time=_dt("2026-05-07T00:00:00Z"),
    )
    store = SqlCaptureForecastStore([[*unusable_candidates, older_ready]])

    response = store.latest_qhh_display_product("GFS")

    assert response["run_id"] == "qhh_gfs_2026050700"
    assert response["status"] == "ready"
    assert response["quality"]["candidate_limit"] == 100


def test_latest_qhh_display_product_normalizes_ifs_and_discloses_shorter_horizon() -> None:
    row = _qhh_candidate_row(
        run_id="qhh_ifs_2026050718",
        source_id="IFS",
        forcing_source_id="IFS",
        cycle_time=_dt("2026-05-07T18:00:00Z"),
        forcing_cycle_time=_dt("2026-05-07T18:00:00Z"),
        forcing_version_id="forc_qhh_ifs_2026050718_basins_qhh_shud",
        river_valid_time_start=_dt("2026-05-07T18:00:00Z"),
        river_valid_time_end=_dt("2026-05-13T18:00:00Z"),
        forcing_end_time=_dt("2026-05-13T18:00:00Z"),
        max_lead_time_hours=144,
    )
    store = SqlCaptureForecastStore([[row]])

    response = store.latest_qhh_display_product("ifs")

    assert response["source_id"] == "IFS"
    assert response["available_horizon_hours"] == 144
    assert response["expected_horizon_hours"] == 168
    assert response["shorter_horizon"] is True
    assert response["valid_time_end"] == "2026-05-13T18:00:00Z"
    assert response["availability"]["quality_flags"] == ["shorter_horizon"]
    assert response["availability"]["quality_notes"][0]["code"] == "SHORTER_HORIZON"


def test_latest_qhh_display_product_accepts_f003_shorter_common_window() -> None:
    cycle_time = _dt("2026-05-07T00:00:00Z")
    available_start = cycle_time + timedelta(hours=3)
    available_end = cycle_time + timedelta(hours=120)
    row = _qhh_candidate_row(
        cycle_time=cycle_time,
        forcing_start_time=available_start,
        station_valid_time_start=available_start,
        station_valid_time_end=available_end,
        station_variable_coverage=_qhh_variable_coverage(
            valid_time_start=available_start,
            valid_time_end=available_end,
        ),
        river_valid_time_start=available_start,
        river_valid_time_end=available_end,
        max_lead_time_hours=120,
    )
    store = SqlCaptureForecastStore([[row]])

    response = store.latest_qhh_display_product("GFS")

    assert response["status"] == "ready"
    assert response["valid_time_start"] == "2026-05-07T03:00:00Z"
    assert response["valid_time_end"] == "2026-05-12T00:00:00Z"
    assert response["available_horizon_hours"] == 120
    assert response["shorter_horizon"] is True
    assert response["availability"]["quality_flags"] == ["shorter_horizon"]


def test_latest_qhh_display_product_uses_station_later_common_start() -> None:
    cycle_time = _dt("2026-05-07T00:00:00Z")
    river_start = cycle_time + timedelta(hours=3)
    station_start = cycle_time + timedelta(hours=6)
    available_end = cycle_time + timedelta(hours=120)
    row = _qhh_candidate_row(
        cycle_time=cycle_time,
        forcing_start_time=river_start,
        station_valid_time_start=station_start,
        station_valid_time_end=available_end,
        station_variable_coverage=_qhh_variable_coverage(
            valid_time_start=station_start,
            valid_time_end=available_end,
        ),
        river_valid_time_start=river_start,
        river_valid_time_end=available_end,
        max_lead_time_hours=120,
    )
    store = SqlCaptureForecastStore([[row]])

    response = store.latest_qhh_display_product("GFS")

    assert response["status"] == "ready"
    assert response["valid_time_start"] == "2026-05-07T06:00:00Z"
    assert response["river_valid_time_start"] == "2026-05-07T03:00:00Z"


def test_latest_qhh_display_product_rejects_unsupported_source_before_sql() -> None:
    store = SqlCaptureForecastStore()

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("ECMWF")

    assert error.value.status_code == 422
    assert error.value.code == "VALIDATION_ERROR"
    assert error.value.details == {
        "field": "source",
        "rejected_value": "ECMWF",
        "allowed_values": ["GFS", "IFS"],
    }
    assert store.cursor.executions == []


def test_latest_qhh_display_product_bounds_reflected_unsupported_source() -> None:
    store = SqlCaptureForecastStore()
    source = "ECMWF-" + ("x" * 200)

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product(source)

    reflected = error.value.details["rejected_value"]
    assert reflected == f"{source[:61]}..."
    assert len(reflected) == 64
    assert store.cursor.executions == []


@pytest.mark.parametrize("status", ["failed", "cancelled", "pending", "created", "running", "parsed"])
def test_latest_qhh_display_product_rejects_failed_cancelled_pending_and_incomplete_runs(status: str) -> None:
    store = SqlCaptureForecastStore([[_qhh_candidate_row(status=status)]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    assert error.value.status_code == 404
    assert error.value.code == "QHH_LATEST_PRODUCT_UNAVAILABLE"
    reason_codes = {reason["code"] for reason in error.value.details["unavailable_reasons"]}
    assert "RUN_STATUS_NOT_READY" in reason_codes


@pytest.mark.parametrize(
    ("field", "reason_code"),
    [
        ("model_id", "MODEL_ID_MISSING"),
        ("basin_version_id", "BASIN_VERSION_ID_MISSING"),
        ("river_network_version_id", "RIVER_NETWORK_VERSION_ID_MISSING"),
        ("forcing_version_id", "FORCING_VERSION_ID_MISSING"),
        ("cycle_time", "CYCLE_TIME_MISSING"),
    ],
)
def test_latest_qhh_display_product_rejects_missing_identity_fields(field: str, reason_code: str) -> None:
    row = _qhh_candidate_row(**{field: None})
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    assert reason_code in {reason["code"] for reason in error.value.details["unavailable_reasons"]}


@pytest.mark.parametrize("checksum", [None, "pending"])
def test_latest_qhh_display_product_rejects_not_finalized_forcing(checksum: str | None) -> None:
    store = SqlCaptureForecastStore([[_qhh_candidate_row(forcing_checksum=checksum)]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    assert "FORCING_VERSION_NOT_FINALIZED" in {
        reason["code"] for reason in error.value.details["unavailable_reasons"]
    }


def test_latest_qhh_display_product_rejects_forcing_and_model_identity_mismatches() -> None:
    store = SqlCaptureForecastStore(
        [
            [
                _qhh_candidate_row(
                    forcing_model_id="different_model",
                    forcing_source_id="IFS",
                    forcing_cycle_time=_dt("2026-05-07T06:00:00Z"),
                    model_basin_version_id="other_basin_version",
                    river_network_basin_version_id="other_basin_version",
                )
            ]
        ]
    )

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    reason_codes = {reason["code"] for reason in error.value.details["unavailable_reasons"]}
    assert {
        "FORCING_MODEL_MISMATCH",
        "FORCING_SOURCE_MISMATCH",
        "FORCING_CYCLE_MISMATCH",
        "MODEL_BASIN_MISMATCH",
        "RIVER_NETWORK_BASIN_MISMATCH",
    } <= reason_codes


@pytest.mark.parametrize(
    ("field", "value", "reason_code"),
    [
        ("station_basin_version_id", "other_basin_version", "STATION_BASIN_MISMATCH"),
        ("station_source_id", "ifs", "STATION_SOURCE_MISMATCH"),
    ],
)
def test_latest_qhh_display_product_rejects_station_identity_mismatches(
    field: str,
    value: str,
    reason_code: str,
) -> None:
    row = _qhh_candidate_row(**{field: value})
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    assert reason_code in {reason["code"] for reason in error.value.details["unavailable_reasons"]}


def test_latest_qhh_display_product_rejects_missing_station_variable_and_q_down_coverage() -> None:
    row = _qhh_candidate_row(
        station_variable_coverage=_qhh_variable_coverage()[:-1],
        segment_count=0,
        river_sample_count=0,
        river_valid_time_start=None,
        river_valid_time_end=None,
    )
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    reason_codes = {reason["code"] for reason in error.value.details["unavailable_reasons"]}
    assert {"STATION_VARIABLE_MISSING", "Q_DOWN_MISSING", "Q_DOWN_VALID_TIME_MISSING"} <= reason_codes


def test_latest_qhh_display_product_rejects_missing_forcing_cycle() -> None:
    row = _qhh_candidate_row(forcing_cycle_time=None)
    row["forcing_cycle_time"] = None
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    assert "FORCING_CYCLE_MISSING" in {reason["code"] for reason in error.value.details["unavailable_reasons"]}


def test_latest_qhh_display_product_uses_station_end_as_shorter_available_end() -> None:
    cycle_time = _dt("2026-05-07T00:00:00Z")
    truncated_end = cycle_time + timedelta(hours=96)
    row = _qhh_candidate_row(
        station_valid_time_end=truncated_end,
        station_variable_coverage=_qhh_variable_coverage(valid_time_end=truncated_end),
        max_lead_time_hours=96,
    )
    store = SqlCaptureForecastStore([[row]])

    response = store.latest_qhh_display_product("GFS")

    assert response["status"] == "ready"
    assert response["valid_time_end"] == "2026-05-11T00:00:00Z"
    assert response["available_horizon_hours"] == 96
    assert response["shorter_horizon"] is True


def test_latest_qhh_display_product_rejects_sparse_station_one_timestep_coverage() -> None:
    valid_time = _dt("2026-05-07T03:00:00Z")
    row = _qhh_candidate_row(
        forcing_start_time=valid_time,
        station_valid_time_start=valid_time,
        station_valid_time_end=valid_time,
        station_variable_coverage=_qhh_variable_coverage(
            valid_time_start=valid_time,
            valid_time_end=valid_time,
            sample_count=386,
        ),
        river_valid_time_start=valid_time,
    )
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    reason_codes = {reason["code"] for reason in error.value.details["unavailable_reasons"]}
    assert {"STATION_VARIABLE_SINGLE_TIMESTEP", "DISPLAYABLE_WINDOW_NONPOSITIVE"} <= reason_codes


def test_latest_qhh_display_product_rejects_ragged_station_common_horizon() -> None:
    valid_time = _dt("2026-05-07T03:00:00Z")
    row = _qhh_candidate_row(
        forcing_start_time=valid_time,
        station_valid_time_start=valid_time,
        station_valid_time_end=valid_time,
        station_variable_coverage=_qhh_variable_coverage(
            valid_time_start=valid_time,
            valid_time_end=valid_time,
            sample_count=387,
        ),
        river_valid_time_start=valid_time,
    )
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    reason_codes = {reason["code"] for reason in error.value.details["unavailable_reasons"]}
    assert {"STATION_VARIABLE_COMMON_HORIZON_NONPOSITIVE", "DISPLAYABLE_WINDOW_NONPOSITIVE"} <= reason_codes


def test_latest_qhh_display_product_rejects_river_rows_outside_display_window() -> None:
    row = _qhh_candidate_row(
        segment_count=0,
        river_sample_count=0,
        river_valid_time_start=None,
        river_valid_time_end=None,
    )
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    reason_codes = {reason["code"] for reason in error.value.details["unavailable_reasons"]}
    assert {"Q_DOWN_MISSING", "Q_DOWN_VALID_TIME_MISSING"} <= reason_codes


def test_latest_qhh_display_product_rejects_sparse_river_one_timestep_coverage() -> None:
    valid_time = _dt("2026-05-07T03:00:00Z")
    row = _qhh_candidate_row(
        forcing_start_time=valid_time,
        station_valid_time_start=valid_time,
        station_variable_coverage=_qhh_variable_coverage(valid_time_start=valid_time),
        river_valid_time_start=valid_time,
        river_valid_time_end=valid_time,
        river_sample_count=1633,
    )
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    reason_codes = {reason["code"] for reason in error.value.details["unavailable_reasons"]}
    assert {"Q_DOWN_SINGLE_TIMESTEP", "DISPLAYABLE_WINDOW_NONPOSITIVE"} <= reason_codes


def test_latest_qhh_display_product_rejects_ragged_river_common_horizon() -> None:
    valid_time = _dt("2026-05-07T03:00:00Z")
    row = _qhh_candidate_row(
        forcing_start_time=valid_time,
        station_valid_time_start=valid_time,
        station_variable_coverage=_qhh_variable_coverage(valid_time_start=valid_time),
        river_valid_time_start=valid_time,
        river_valid_time_end=valid_time,
        river_sample_count=1634,
    )
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    reason_codes = {reason["code"] for reason in error.value.details["unavailable_reasons"]}
    assert {"Q_DOWN_COMMON_HORIZON_NONPOSITIVE", "DISPLAYABLE_WINDOW_NONPOSITIVE"} <= reason_codes


@pytest.mark.parametrize(
    ("river_valid_time_end", "max_lead_time_hours", "expected_reason"),
    [
        ("2026-05-07T00:00:00Z", 0, "Q_DOWN_HORIZON_NONPOSITIVE"),
        ("2026-05-07T01:00:00Z", 0, "Q_DOWN_LEAD_TIME_NONPOSITIVE"),
    ],
)
def test_latest_qhh_display_product_rejects_zero_or_nonpositive_q_down_horizon(
    river_valid_time_end: str,
    max_lead_time_hours: int,
    expected_reason: str,
) -> None:
    row = _qhh_candidate_row(
        river_valid_time_end=_dt(river_valid_time_end),
        max_lead_time_hours=max_lead_time_hours,
    )
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    assert expected_reason in {reason["code"] for reason in error.value.details["unavailable_reasons"]}


def test_latest_qhh_display_product_reports_station_and_segment_count_mismatches() -> None:
    row = _qhh_candidate_row(
        station_count=385,
        station_variable_coverage=_qhh_variable_coverage(station_count=385),
        segment_count=1600,
    )
    store = SqlCaptureForecastStore([[row]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    reason_codes = {reason["code"] for reason in error.value.details["unavailable_reasons"]}
    assert {
        "STATION_COUNT_MISMATCH",
        "STATION_VARIABLE_COUNT_MISMATCH",
        "SEGMENT_COUNT_MISMATCH",
    } <= reason_codes


def test_latest_qhh_display_product_candidate_discovery_sql_is_bounded_before_timeseries_aggregation() -> None:
    store = SqlCaptureForecastStore([[_qhh_candidate_row()]])

    response = store.latest_qhh_display_product("GFS")

    statement, parameters = store.cursor.executions[0]
    candidate_cte = statement[statement.index("WITH candidate_runs") : statement.index("station_sample_rows AS")]
    assert "bv.basin_id = %s" in candidate_cte
    assert "LOWER(h.source_id) = LOWER(%s)" in candidate_cte
    assert "h.status IN ('frequency_done', 'published')" in candidate_cte
    assert "h.run_type = 'forecast'" in candidate_cte
    assert "h.cycle_time IS NOT NULL" in candidate_cte
    assert "ORDER BY h.cycle_time DESC, h.run_id DESC" in candidate_cte
    assert "LIMIT %s" in candidate_cte
    assert "FROM met.forcing_station_timeseries" not in candidate_cte
    assert "FROM hydro.river_timeseries" not in candidate_cte
    station_cte = statement[statement.index("station_sample_rows AS") : statement.index("river_sample_rows AS")]
    hydro_cte = statement[statement.index("river_sample_rows AS") : statement.index("SELECT\n                cr.*")]
    assert "JOIN candidate_runs cr" in statement
    assert "fst.basin_version_id = cr.basin_version_id" in station_cte
    assert "LOWER(fst.source_id) = LOWER(cr.source_id)" in station_cte
    assert "FROM met.interp_weight iw" in station_cte
    assert "iw.model_id = cr.model_id" in station_cte
    assert "iw.station_id = fst.station_id" in station_cte
    assert "iw.variable = fst.variable" in station_cte
    assert "GREATEST(h.cycle_time, h.start_time, fv.start_time) AS display_start_time" in candidate_cte
    assert "fst.valid_time >= cr.display_start_time" in station_cte
    assert "fst.valid_time <= cr.display_end_time" in station_cte
    assert "station_identity_coverage AS" in station_cte
    assert "station_time_coverage AS" in station_cte
    assert "station_variable_complete_times AS" in station_cte
    assert "station_variable_common_times AS" in station_cte
    assert "station_all_variable_complete_times AS" in station_cte
    assert "GROUP BY\n                    forcing_version_id" in station_cte
    assert "variable,\n                    station_id" in station_cte
    assert "cr.expected_station_count" in station_cte
    assert "station_count = expected_station_count" in station_cte
    assert "COUNT(DISTINCT variable) AS complete_variable_count" in station_cte
    assert "HAVING COUNT(DISTINCT variable) = %s" in station_cte
    assert "MIN(valid_time) AS valid_time_start" in station_cte
    assert "MAX(valid_time) AS valid_time_end" in station_cte
    assert "MIN(valid_time) AS station_valid_time_start" in station_cte
    assert "MAX(valid_time) AS station_valid_time_end" in station_cte
    assert "MAX(valid_time_start) AS station_valid_time_start" not in station_cte
    assert "MIN(valid_time_end) AS station_valid_time_end" not in station_cte
    assert "cr.run_id = rt.run_id" in statement
    assert "rt.valid_time >= cr.display_start_time" in hydro_cte
    assert "rt.valid_time <= cr.display_end_time" in hydro_cte
    assert "river_identity_coverage AS" in hydro_cte
    assert "river_time_coverage AS" in hydro_cte
    assert "river_common_window AS" in hydro_cte
    assert "river_segment_id" in hydro_cte
    assert "cr.expected_segment_count" in hydro_cte
    assert "segment_count = expected_segment_count" in hydro_cte
    assert "MIN(valid_time) AS river_valid_time_start" in hydro_cte
    assert "MAX(valid_time) AS river_valid_time_end" in hydro_cte
    assert parameters == (
        "basins_qhh",
        "GFS",
        100,
        ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"],
        6,
    )
    assert response["quality"]["candidate_limit"] == 100


def test_latest_qhh_display_product_fetches_nonready_context_without_consuming_ready_candidate_window() -> None:
    nonready_context = _qhh_candidate_row(
        run_id="qhh_gfs_2026050800",
        cycle_time=_dt("2026-05-08T00:00:00Z"),
        status="pending",
        segment_count=0,
        river_sample_count=0,
        river_valid_time_start=None,
        river_valid_time_end=None,
    )
    store = SqlCaptureForecastStore([[], [nonready_context]])

    with pytest.raises(ForecastStoreError) as error:
        store.latest_qhh_display_product("GFS")

    ready_statement, ready_parameters = store.cursor.executions[0]
    context_statement, context_parameters = store.cursor.executions[1]
    assert "h.status IN ('frequency_done', 'published')" in ready_statement
    assert "FROM met.forcing_station_timeseries" in ready_statement
    assert "FROM hydro.river_timeseries" in ready_statement
    assert "h.status NOT IN ('frequency_done', 'published')" in context_statement
    assert "FROM met.forcing_station_timeseries" not in context_statement
    assert "FROM hydro.river_timeseries" not in context_statement
    assert ready_parameters == (
        "basins_qhh",
        "GFS",
        100,
        ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"],
        6,
    )
    assert context_parameters == ("basins_qhh", "GFS", 10)
    assert error.value.details["candidate_count"] == 1
    assert "RUN_STATUS_NOT_READY" in {
        reason["code"] for reason in error.value.details["unavailable_reasons"]
    }


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


@pytest.mark.asyncio
async def test_qhh_latest_product_success_envelope_accepts_source_case_and_needs_no_manual_ids(
    fake_store: FakeForecastStore,
) -> None:
    response = await _get("/api/v1/mvp/qhh/latest-product?source=ifs")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["request_id"]
    data = body["data"]
    assert data["source_id"] == "IFS"
    assert data["basin_id"] == "basins_qhh"
    assert data["model_id"] == "basins_qhh_shud"
    assert data["basin_version_id"] == "basins_qhh_vbasins"
    assert data["river_network_version_id"] == "basins_qhh_rivnet_vbasins"
    assert data["run_id"] == "qhh_gfs_2026050700"
    assert data["forcing_version_id"] == "forc_qhh_gfs_2026050700_basins_qhh_shud"
    assert data["station_count"] == 386
    assert data["segment_count"] == 1633
    assert data["status"] == "ready"
    assert data["shorter_horizon"] is True
    assert fake_store.latest_qhh_calls == ["ifs"]


@pytest.mark.asyncio
async def test_qhh_latest_product_unsupported_source_validation_error(fake_store: FakeForecastStore) -> None:
    response = await _get("/api/v1/mvp/qhh/latest-product?source=ecmwf")

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["field"] == "source"


@pytest.mark.asyncio
async def test_qhh_latest_product_bounds_long_unsupported_source_detail(fake_store: FakeForecastStore) -> None:
    source = "ECMWF-" + ("x" * 200)

    response = await _get(f"/api/v1/mvp/qhh/latest-product?source={source}")

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["rejected_value"] == f"{source[:61]}..."


@pytest.mark.asyncio
async def test_qhh_latest_product_no_usable_product_returns_typed_unavailable_reasons(
    fake_store: FakeForecastStore,
) -> None:
    fake_store.latest_qhh_unavailable = True

    response = await _get("/api/v1/mvp/qhh/latest-product?source=GFS")

    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "QHH_LATEST_PRODUCT_UNAVAILABLE"
    assert body["error"]["details"]["status"] == "unavailable"
    assert body["error"]["details"]["unavailable_reasons"][0]["code"] == "Q_DOWN_MISSING"


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


@pytest.mark.asyncio
async def test_met_station_series_explicit_forcing_version_uses_success_envelope_and_store_payload(
    fake_store: FakeForecastStore,
) -> None:
    response = await _get(
        "/api/v1/met/stations/qhh_stn_001/series"
        "?forcing_version_id=forc_qhh_gfs_2026050700&variables=PRCP,TEMP"
        "&from=2026-05-07T00:00:00Z&to=2026-05-07T03:00:00Z&limit=2"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["request_id"]
    data = body["data"]
    assert data["station_id"] == "qhh_stn_001"
    assert data["station"]["longitude"] == 101.0
    assert data["forcing_version_id"] == "forc_qhh_gfs_2026050700"
    assert data["model_id"] == "qhh_shud_v1"
    assert data["source_id"] == "GFS"
    assert data["cycle_time"] == "2026-05-07T00:00:00Z"
    assert data["limit"] == 2
    series_by_variable = {series["variable"]: series for series in data["series"]}
    assert list(series_by_variable) == ["PRCP", "TEMP"]
    assert series_by_variable["PRCP"]["unit"] == "mm/h"
    assert series_by_variable["PRCP"]["native_resolution"] == "1h"
    assert series_by_variable["PRCP"]["truncated"] is True
    assert series_by_variable["PRCP"]["metadata"] == {
        "limit": 2,
        "returned_points": 2,
        "requested_from": "2026-05-07T00:00:00Z",
        "requested_to": "2026-05-07T03:00:00Z",
        "returned_from": "2026-05-07T00:00:00Z",
        "returned_to": "2026-05-07T01:00:00Z",
        "truncated": True,
    }
    assert series_by_variable["PRCP"]["points"][0] == {
        "valid_time": "2026-05-07T00:00:00Z",
        "value": 1.0,
        "quality_flag": "ok",
        "source_id": "GFS",
    }
    assert fake_store.station_series_calls[-1] == {
        "station_id": "qhh_stn_001",
        "forcing_version_id": "forc_qhh_gfs_2026050700",
        "model_id": None,
        "source_id": None,
        "cycle_time": None,
        "variables": ["PRCP,TEMP"],
        "from_time": _dt("2026-05-07T00:00:00Z"),
        "to_time": _dt("2026-05-07T03:00:00Z"),
        "limit": 2,
    }


@pytest.mark.asyncio
async def test_met_station_series_tuple_resolution_and_repeated_variables_delegate_to_store(
    fake_store: FakeForecastStore,
) -> None:
    response = await _get(
        "/api/v1/met/stations/qhh_stn_001/series"
        "?model_id=qhh_shud_v1&source_id=GFS&cycle_time=2026-05-07T00:00:00Z"
        "&variables=PRCP&variables=TEMP"
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["forcing_version_id"] == "forc_resolved_from_tuple"
    assert fake_store.station_series_calls[-1]["forcing_version_id"] is None
    assert fake_store.station_series_calls[-1]["model_id"] == "qhh_shud_v1"
    assert fake_store.station_series_calls[-1]["source_id"] == "GFS"
    assert fake_store.station_series_calls[-1]["cycle_time"] == _dt("2026-05-07T00:00:00Z")
    assert fake_store.station_series_calls[-1]["variables"] == ["PRCP", "TEMP"]


@pytest.mark.asyncio
async def test_met_station_series_without_variables_defaults_through_store(
    fake_store: FakeForecastStore,
) -> None:
    response = await _get("/api/v1/met/stations/qhh_stn_001/series?forcing_version_id=forc_qhh_gfs_2026050700")

    assert response.status_code == 200
    assert fake_store.station_series_calls[-1]["variables"] is None
    assert [series["variable"] for series in response.json()["data"]["series"]] == [
        "PRCP",
        "TEMP",
        "RH",
        "wind",
        "Rn",
        "Press",
    ]


@pytest.mark.asyncio
async def test_met_station_series_empty_valid_filtered_range_returns_no_synthetic_points(
    fake_store: FakeForecastStore,
) -> None:
    fake_store.station_series_response["requested_from"] = "2026-05-15T00:00:00Z"
    fake_store.station_series_response["requested_to"] = "2026-05-15T01:00:00Z"
    fake_store.station_series_response["series"] = [
        {
            "variable": "PRCP",
            "unit": "mm/h",
            "native_resolution": "1h",
            "source_id": "GFS",
            "cycle_time": "2026-05-07T00:00:00Z",
            "points": [],
            "truncated": False,
            "metadata": {
                "limit": 2,
                "returned_points": 0,
                "requested_from": "2026-05-15T00:00:00Z",
                "requested_to": "2026-05-15T01:00:00Z",
                "returned_from": None,
                "returned_to": None,
                "truncated": False,
            },
        }
    ]

    response = await _get(
        "/api/v1/met/stations/qhh_stn_001/series"
        "?forcing_version_id=forc_qhh_gfs_2026050700&variables=PRCP"
        "&from=2026-05-15T00:00:00Z&to=2026-05-15T01:00:00Z&limit=2"
    )

    assert response.status_code == 200
    series = response.json()["data"]["series"][0]
    assert series["points"] == []
    assert series["metadata"] == {
        "limit": 2,
        "returned_points": 0,
        "requested_from": "2026-05-15T00:00:00Z",
        "requested_to": "2026-05-15T01:00:00Z",
        "returned_from": None,
        "returned_to": None,
        "truncated": False,
    }
    assert all("value" not in point for point in series["points"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_status", "expected_code"),
    [
        ("forcing_version_id=missing", 404, "FORCING_VERSION_NOT_FOUND"),
        ("forcing_version_id=not_finalized", 409, "FORCING_VERSION_NOT_FINALIZED"),
        ("forcing_version_id=station_absent", 404, "STATION_NOT_IN_FORCING_VERSION"),
        ("forcing_version_id=forc_qhh_gfs_2026050700&source_id=IFS", 409, "FORCING_VERSION_FILTER_CONFLICT"),
        ("model_id=ambiguous&source_id=GFS&cycle_time=2026-05-07T00:00:00Z", 409, "FORCING_VERSION_AMBIGUOUS"),
        ("forcing_version_id=forc_qhh_gfs_2026050700&variables=unknown", 422, "VALIDATION_ERROR"),
        (
            "forcing_version_id=forc_qhh_gfs_2026050700&from=2026-05-08T00:00:00Z&to=2026-05-07T00:00:00Z",
            422,
            "VALIDATION_ERROR",
        ),
    ],
)
async def test_met_station_series_preserves_store_error_envelope(
    fake_store: FakeForecastStore,
    query: str,
    expected_status: int,
    expected_code: str,
) -> None:
    response = await _get(f"/api/v1/met/stations/qhh_stn_001/series?{query}")

    assert response.status_code == expected_status
    body = response.json()
    assert body["status"] == "error"
    assert body["request_id"]
    assert body["error"]["code"] == expected_code
    assert body["error"]["details"] is not None


@pytest.mark.asyncio
async def test_met_station_series_missing_station_preserves_store_error(fake_store: FakeForecastStore) -> None:
    response = await _get("/api/v1/met/stations/missing/series?forcing_version_id=forc_qhh_gfs_2026050700")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "STATION_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["limit=0", "limit=10001", "cycle_time=not-a-time"])
async def test_met_station_series_fastapi_validation_uses_typed_error(
    fake_store: FakeForecastStore,
    query: str,
) -> None:
    response = await _get(f"/api/v1/met/stations/qhh_stn_001/series?forcing_version_id=forc&{query}")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake_store.station_series_calls == []


@pytest.mark.asyncio
async def test_met_station_series_invalid_limit_returns_documented_validation_detail_array(
    fake_store: FakeForecastStore,
) -> None:
    response = await _get("/api/v1/met/stations/qhh_stn_001/series?forcing_version_id=forc&limit=0")

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "Request validation failed."
    details = body["error"]["details"]
    assert isinstance(details, list)
    assert details[0]["field"] == "query.limit"
    assert details[0]["rejected_value"] == "0"
    assert isinstance(details[0]["reason"], str)
    assert fake_store.station_series_calls == []


async def _get(path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _station_row(station_id: str = "qhh_stn_001") -> dict[str, Any]:
    return {
        "station_id": station_id,
        "basin_version_id": "qhh_v2026",
        "station_name": "QHH Station 001",
        "longitude": 101.0,
        "latitude": 36.0,
        "elevation_m": 3200.0,
        "station_role": "forcing_proxy",
        "active_flag": True,
        "properties_json": {"source": "fixture"},
    }


def _forcing_version_row(
    forcing_version_id: str = "forc_qhh_gfs_2026050700",
    *,
    station_count: int = 386,
    checksum: str | None = "sha256:fixture",
) -> dict[str, Any]:
    return {
        "forcing_version_id": forcing_version_id,
        "model_id": "qhh_shud_v1",
        "source_id": "gfs",
        "cycle_time": _dt("2026-05-07T00:00:00Z"),
        "start_time": _dt("2026-05-07T00:00:00Z"),
        "end_time": _dt("2026-05-14T00:00:00Z"),
        "station_count": station_count,
        "forcing_package_uri": "s3://nhms/qhh/forcing.tar.gz",
        "checksum": checksum,
        "lineage_json": {"fixture": True},
        "created_at": _dt("2026-05-07T00:30:00Z"),
    }


def _station_series_row(
    variable: str,
    valid_time: datetime,
    value: float,
    *,
    row_number: int,
    unit: str = "mm/h",
    native_resolution: str = "1h",
    quality_flag: str = "ok",
) -> dict[str, Any]:
    return {
        "forcing_version_id": "forc_qhh_gfs_2026050700",
        "station_id": "qhh_stn_001",
        "variable": variable,
        "valid_time": valid_time,
        "value": value,
        "unit": unit,
        "native_resolution": native_resolution,
        "quality_flag": quality_flag,
        "source_id": "gfs",
        "row_number": row_number,
    }


def _readiness_row(
    variable: str,
    *,
    station_count: int,
    sample_count: int = 100,
    unit_count: int = 1,
    missing_unit_samples: int = 0,
    quality_flag_count: int = 1,
    missing_quality_flag_samples: int = 0,
    valid_time_start: datetime | None = None,
    valid_time_end: datetime | None = None,
) -> dict[str, Any]:
    return {
        "variable": variable,
        "station_count": station_count,
        "sample_count": sample_count,
        "unit_count": unit_count,
        "missing_unit_samples": missing_unit_samples,
        "quality_flag_count": quality_flag_count,
        "missing_quality_flag_samples": missing_quality_flag_samples,
        "valid_time_start": valid_time_start or _dt("2026-05-07T00:00:00Z"),
        "valid_time_end": valid_time_end or _dt("2026-05-08T00:00:00Z"),
    }


def _qhh_candidate_row(
    *,
    run_id: str = "qhh_gfs_2026050700",
    run_type: str = "forecast",
    scenario_id: str = "forecast_gfs_deterministic",
    model_id: str | None = "basins_qhh_shud",
    basin_version_id: str | None = "basins_qhh_vbasins",
    forcing_version_id: str | None = "forc_qhh_gfs_2026050700_basins_qhh_shud",
    source_id: str = "gfs",
    cycle_time: datetime | None = _dt("2026-05-07T00:00:00Z"),
    status: str = "frequency_done",
    river_network_version_id: str | None = "basins_qhh_rivnet_vbasins",
    model_basin_version_id: str | None = "basins_qhh_vbasins",
    river_network_basin_version_id: str | None = "basins_qhh_vbasins",
    forcing_model_id: str | None = "basins_qhh_shud",
    forcing_source_id: str | None = "gfs",
    forcing_cycle_time: datetime | None = None,
    run_start_time: datetime | None = None,
    run_end_time: datetime | None = None,
    forcing_start_time: datetime | None = None,
    forcing_end_time: datetime | None = None,
    forcing_checksum: str | None = "sha256:qhh-forcing",
    station_count: int = 386,
    expected_station_count: int = 386,
    station_sample_count: int = 12000,
    station_basin_version_id: str | None = "basins_qhh_vbasins",
    station_source_id: str | None = None,
    station_valid_time_start: datetime | None = None,
    station_valid_time_end: datetime | None = None,
    station_variable_coverage: list[dict[str, Any]] | None = None,
    segment_count: int = 1633,
    expected_segment_count: int = 1633,
    river_sample_count: int = 10000,
    river_valid_time_start: datetime | None = _dt("2026-05-07T00:00:00Z"),
    river_valid_time_end: datetime | None = _dt("2026-05-14T00:00:00Z"),
    max_lead_time_hours: int | None = 168,
) -> dict[str, Any]:
    default_cycle_time = _dt("2026-05-07T00:00:00Z")
    schedule_cycle_time = cycle_time or default_cycle_time
    selected_run_start = run_start_time or schedule_cycle_time
    selected_run_end = run_end_time or schedule_cycle_time + timedelta(days=7)
    selected_forcing_start = forcing_start_time or schedule_cycle_time
    selected_forcing_end = forcing_end_time or schedule_cycle_time + timedelta(days=7)
    selected_station_start = station_valid_time_start or selected_forcing_start
    selected_station_end = station_valid_time_end or selected_forcing_end
    selected_station_source_id = station_source_id if station_source_id is not None else source_id
    return {
        "run_id": run_id,
        "run_type": run_type,
        "scenario_id": scenario_id,
        "model_id": model_id,
        "basin_version_id": basin_version_id,
        "forcing_version_id": forcing_version_id,
        "source_id": source_id,
        "cycle_time": cycle_time,
        "run_start_time": selected_run_start,
        "run_end_time": selected_run_end,
        "status": status,
        "run_created_at": schedule_cycle_time,
        "run_updated_at": schedule_cycle_time,
        "river_network_version_id": river_network_version_id,
        "model_basin_version_id": model_basin_version_id,
        "basin_id": "basins_qhh",
        "river_network_basin_version_id": river_network_basin_version_id,
        "expected_segment_count": expected_segment_count,
        "fv_forcing_version_id": forcing_version_id,
        "forcing_model_id": forcing_model_id,
        "forcing_source_id": forcing_source_id,
        "forcing_cycle_time": forcing_cycle_time or schedule_cycle_time,
        "forcing_start_time": selected_forcing_start,
        "forcing_end_time": selected_forcing_end,
        "expected_station_count": expected_station_count,
        "forcing_checksum": forcing_checksum,
        "forcing_lineage_json": {},
        "display_start_time": max(schedule_cycle_time, selected_run_start, selected_forcing_start),
        "display_end_time": min(selected_run_end, selected_forcing_end),
        "station_count": station_count,
        "station_sample_count": station_sample_count,
        "station_basin_version_id": station_basin_version_id,
        "station_source_id": selected_station_source_id,
        "station_valid_time_start": selected_station_start,
        "station_valid_time_end": selected_station_end,
        "station_variable_coverage": station_variable_coverage
        or _qhh_variable_coverage(
            station_count=station_count,
            valid_time_start=selected_station_start,
            valid_time_end=selected_station_end,
        ),
        "segment_count": segment_count,
        "river_sample_count": river_sample_count,
        "river_valid_time_start": river_valid_time_start,
        "river_valid_time_end": river_valid_time_end,
        "min_lead_time_hours": 0,
        "max_lead_time_hours": max_lead_time_hours,
    }


def _qhh_variable_coverage(
    station_count: int = 386,
    *,
    valid_time_start: datetime = _dt("2026-05-07T00:00:00Z"),
    valid_time_end: datetime = _dt("2026-05-14T00:00:00Z"),
    sample_count: int = 1000,
) -> list[dict[str, Any]]:
    return [
        {
            "variable": variable,
            "station_count": station_count,
            "sample_count": sample_count,
            "unit_count": 1,
            "quality_flag_count": 1,
            "missing_unit_samples": 0,
            "missing_quality_flag_samples": 0,
            "valid_time_start": valid_time_start,
            "valid_time_end": valid_time_end,
        }
        for variable in ("PRCP", "TEMP", "RH", "wind", "Rn", "Press")
    ]
