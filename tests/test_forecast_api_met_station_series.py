from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.routes import data_sources
from apps.api.routes.data_sources import get_station_lookup
from packages.common.object_store_forcing import StationMetadata, raise_station_not_found

STATION_ID = "heihe_forc_001"
MODEL_ID = "basins_heihe_shud"
SOURCE_ID = "IFS"
CYCLE_TIME = "2026-06-20T12:00:00Z"
BASIN_VERSION_ID = "basins_heihe_vbasins"
FORCING_FILENAME = "X100.75Y37.65.csv"


def _station(*, properties_json: dict[str, Any] | None = None) -> StationMetadata:
    return StationMetadata(
        station_id=STATION_ID,
        basin_version_id=BASIN_VERSION_ID,
        station_name="HEIHE forcing station 001",
        longitude=100.75,
        latitude=37.65,
        elevation_m=0.0,
        station_role="forcing_grid",
        active_flag=True,
        properties_json=properties_json or {"forcing_filename": FORCING_FILENAME, "source": "fixture"},
    )


class FakeStationLookup:
    def __init__(self, stations: dict[str, StationMetadata] | None = None) -> None:
        self.stations = stations if stations is not None else {STATION_ID: _station()}
        self.calls: list[str] = []

    def lookup(self, station_id: str) -> StationMetadata:
        self.calls.append(station_id)
        station = self.stations.get(station_id)
        if station is None:
            raise_station_not_found(station_id)
        return station


def test_station_series_route_reads_disk_csv_and_returns_success_envelope(tmp_path: Path) -> None:
    _write_csv(tmp_path)
    lookup = FakeStationLookup()

    with _client(tmp_path, lookup) as client:
        response = client.get(
            f"/api/v1/met/stations/{STATION_ID}/series",
            params={
                "model_id": MODEL_ID,
                "source_id": SOURCE_ID,
                "cycle_time": CYCLE_TIME,
                "variables": "PRCP,TEMP",
                "limit": 3,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["request_id"]
    data = body["data"]
    assert lookup.calls == [STATION_ID]
    assert data["station_id"] == STATION_ID
    assert data["station"]["properties_json"]["forcing_filename"] == FORCING_FILENAME
    assert data["source_id"] == "IFS"
    assert data["cycle_time"] == CYCLE_TIME
    assert [series["variable"] for series in data["series"]] == ["PRCP"]
    assert data["series"][0]["unit"] == "mm/day"
    assert data["series"][0]["native_resolution"] == "3h"
    assert [point["valid_time"] for point in data["series"][0]["points"]] == [
        "2026-06-20T12:00:00Z",
        "2026-06-20T15:00:00Z",
        "2026-06-20T18:00:00Z",
    ]


def test_station_series_route_ignores_forcing_version_when_tuple_filters_are_present(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    with _client(tmp_path) as client:
        with_forcing_version = client.get(
            f"/api/v1/met/stations/{STATION_ID}/series",
            params={
                "forcing_version_id": "not_finalized",
                "model_id": MODEL_ID,
                "source_id": SOURCE_ID,
                "cycle_time": CYCLE_TIME,
                "variables": "PRCP",
            },
        )
        without_forcing_version = client.get(
            f"/api/v1/met/stations/{STATION_ID}/series",
            params={
                "model_id": MODEL_ID,
                "source_id": SOURCE_ID,
                "cycle_time": CYCLE_TIME,
                "variables": "PRCP",
            },
        )

    assert with_forcing_version.status_code == 200
    assert without_forcing_version.status_code == 200
    assert with_forcing_version.json()["data"] == without_forcing_version.json()["data"]


def test_station_series_route_forcing_version_alone_returns_missing_required_filter(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    with _client(tmp_path) as client:
        response = client.get(
            f"/api/v1/met/stations/{STATION_ID}/series",
            params={"forcing_version_id": "forc_qhh_gfs_2026050700"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MISSING_REQUIRED_FILTER"
    assert response.json()["error"]["details"] == {
        "required_alternatives": [
            ["forcing_version_id"],
            ["model_id", "source_id", "cycle_time"],
        ]
    }


@pytest.mark.parametrize(
    ("lookup", "write_file", "raw_content", "expected_status", "expected_code"),
    [
        (
            FakeStationLookup({}),
            True,
            None,
            404,
            "STATION_NOT_FOUND",
        ),
        (
            FakeStationLookup({STATION_ID: _station(properties_json={"source": "fixture"})}),
            True,
            None,
            500,
            "STATION_FORCING_FILENAME_MISSING",
        ),
        (
            FakeStationLookup(),
            False,
            None,
            404,
            "STATION_FORCING_FILE_NOT_FOUND",
        ),
        (
            FakeStationLookup(),
            True,
            "1\t6\t20260620\t20260627\nTime_Day\tPrecip\tTemp\tRH\tWind\tRN\nbad\n",
            500,
            "STATION_FORCING_FILE_MALFORMED",
        ),
    ],
)
def test_station_series_route_maps_reader_errors_to_api_envelope(
    tmp_path: Path,
    lookup: FakeStationLookup,
    write_file: bool,
    raw_content: str | None,
    expected_status: int,
    expected_code: str,
) -> None:
    if write_file:
        _write_csv(tmp_path, raw_content=raw_content)

    with _client(tmp_path, lookup) as client:
        response = client.get(
            f"/api/v1/met/stations/{STATION_ID}/series",
            params={
                "model_id": MODEL_ID,
                "source_id": SOURCE_ID,
                "cycle_time": CYCLE_TIME,
            },
        )

    assert response.status_code == expected_status
    body = response.json()
    assert body["status"] == "error"
    assert body["request_id"]
    assert body["error"]["code"] == expected_code
    assert body["error"]["details"] is not None


def test_station_series_route_does_not_call_db_store_or_finalize_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_csv(tmp_path)
    calls: list[str] = []

    def fail_station_series(*_args: Any, **_kwargs: Any) -> None:
        calls.append("station_series")
        raise AssertionError("station_series must not be called by the public route")

    monkeypatch.setattr(data_sources.PsycopgForecastStore, "station_series", fail_station_series)
    source = inspect.getsource(data_sources.get_met_station_series)
    assert ".station_series(" not in source
    assert "_ensure_forcing_version_finalized" not in source

    with _client(tmp_path) as client:
        response = client.get(
            f"/api/v1/met/stations/{STATION_ID}/series",
            params={
                "model_id": MODEL_ID,
                "source_id": SOURCE_ID,
                "cycle_time": CYCLE_TIME,
            },
        )

    assert response.status_code == 200
    assert calls == []


def _client(tmp_path: Path, lookup: FakeStationLookup | None = None) -> TestClient:
    api = create_app()
    api.state.object_store_root = tmp_path
    api.dependency_overrides[get_station_lookup] = lambda: lookup or FakeStationLookup()
    return TestClient(api)


def _write_csv(tmp_path: Path, *, raw_content: str | None = None) -> Path:
    path = (
        tmp_path
        / "forcing"
        / "ifs"
        / "2026062012"
        / BASIN_VERSION_ID
        / MODEL_ID
        / "shud"
        / FORCING_FILENAME
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    content = raw_content or "\n".join(
        [
            "3\t6\t20260620\t20260627",
            "Time_Day\tPrecip\tTemp\tRH\tWind\tRN",
            "0\t1.0\t271.0\t0.51\t4.0\t101.0",
            "0.125\t2.0\t272.0\t0.52\t5.0\t102.0",
            "0.25\t3.0\t273.0\t0.53\t6.0\t103.0",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
    return path
