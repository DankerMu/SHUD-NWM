from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.common.object_store_forcing import (
    PsycopgStationLookup,
    _compute_cycle_compact,
    _normalize_source_id,
    _resolve_disk_path,
)

pytestmark = [pytest.mark.e2e, pytest.mark.real_disk]

BASELINE_FIXTURE = Path(__file__).parent / "fixtures" / "station_series_baseline_heihe_ifs_2026060100.json"
LATEST_CYCLE = "2026-06-20T12:00:00Z"
MISSING_CYCLE = "2020-01-01T00:00:00Z"
COMBOS = [
    ("heihe_forc_001", "IFS", "basins_heihe_shud"),
    ("heihe_forc_001", "gfs", "basins_heihe_shud"),
    ("qhh_forc_001", "IFS", "basins_qhh_shud"),
    ("qhh_forc_001", "gfs", "basins_qhh_shud"),
]


def test_real_disk_latest_cycle_serves_all_currently_409_combinations() -> None:
    with _client() as client:
        for station_id, source_id, model_id in COMBOS:
            response = client.get(
                f"/api/v1/met/stations/{station_id}/series",
                params={"model_id": model_id, "source_id": source_id, "cycle_time": LATEST_CYCLE},
            )

            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["station_id"] == station_id
            assert [series["variable"] for series in data["series"]] == ["PRCP", "TEMP", "RH", "wind", "Rn"]
            assert {series["unit"] for series in data["series"]} == {"mm/day", "degC", "0-1", "m/s", "W/m^2"}
            assert all(series["points"] for series in data["series"])
            assert data["valid_time_start"] == LATEST_CYCLE


def test_real_disk_error_and_filter_scenarios() -> None:
    with _client() as client:
        missing_cycle = client.get(
            "/api/v1/met/stations/heihe_forc_001/series",
            params={"model_id": "basins_heihe_shud", "source_id": "IFS", "cycle_time": MISSING_CYCLE},
        )
        missing_station = client.get(
            "/api/v1/met/stations/bogus_forc_999/series",
            params={"model_id": "basins_heihe_shud", "source_id": "IFS", "cycle_time": LATEST_CYCLE},
        )
        variables = client.get(
            "/api/v1/met/stations/heihe_forc_001/series",
            params={
                "model_id": "basins_heihe_shud",
                "source_id": "IFS",
                "cycle_time": LATEST_CYCLE,
                "variables": "PRCP,TEMP",
            },
        )
        window = client.get(
            "/api/v1/met/stations/heihe_forc_001/series",
            params={
                "model_id": "basins_heihe_shud",
                "source_id": "IFS",
                "cycle_time": LATEST_CYCLE,
                "from": "2026-06-20T15:00:00Z",
                "to": "2026-06-20T18:00:00Z",
                "variables": "PRCP",
            },
        )
        zulu = client.get(
            "/api/v1/met/stations/heihe_forc_001/series",
            params={
                "model_id": "basins_heihe_shud",
                "source_id": "IFS",
                "cycle_time": LATEST_CYCLE,
                "variables": "PRCP",
            },
        )
        plus_eight = client.get(
            "/api/v1/met/stations/heihe_forc_001/series",
            params={
                "model_id": "basins_heihe_shud",
                "source_id": "IFS",
                "cycle_time": "2026-06-20T20:00:00+08:00",
                "variables": "PRCP",
            },
        )
        press_only = client.get(
            "/api/v1/met/stations/heihe_forc_001/series",
            params={
                "model_id": "basins_heihe_shud",
                "source_id": "IFS",
                "cycle_time": LATEST_CYCLE,
                "variables": "Press",
            },
        )
        prcp_press = client.get(
            "/api/v1/met/stations/heihe_forc_001/series",
            params={
                "model_id": "basins_heihe_shud",
                "source_id": "IFS",
                "cycle_time": LATEST_CYCLE,
                "variables": "PRCP,Press",
            },
        )

    assert missing_cycle.status_code == 404
    assert missing_cycle.json()["error"]["code"] == "STATION_FORCING_FILE_NOT_FOUND"
    assert "expected_path" in missing_cycle.json()["error"]["details"]
    assert missing_station.status_code == 404
    assert missing_station.json()["error"]["code"] == "STATION_NOT_FOUND"
    assert variables.status_code == 200
    assert [series["variable"] for series in variables.json()["data"]["series"]] == ["PRCP", "TEMP"]
    assert window.status_code == 200
    assert [point["valid_time"] for point in window.json()["data"]["series"][0]["points"]] == [
        "2026-06-20T15:00:00Z",
        "2026-06-20T18:00:00Z",
    ]
    assert zulu.status_code == 200
    assert plus_eight.status_code == 200
    assert zulu.json()["data"] == plus_eight.json()["data"]
    assert press_only.status_code == 200
    assert press_only.json()["data"]["series"] == []
    assert prcp_press.status_code == 200
    assert [series["variable"] for series in prcp_press.json()["data"]["series"]] == ["PRCP"]


def test_real_disk_station_series_read_is_side_effect_free() -> None:
    root = _real_object_store_root()
    station = PsycopgStationLookup.from_env().lookup("heihe_forc_001")
    expected_path = _resolve_disk_path(
        root,
        _normalize_source_id("IFS"),
        _compute_cycle_compact(_dt(LATEST_CYCLE)),
        station.basin_version_id,
        "basins_heihe_shud",
        station.forcing_filename or "",
    )
    before = expected_path.stat().st_mtime_ns

    with _client() as client:
        responses = [
            client.get(
                "/api/v1/met/stations/heihe_forc_001/series",
                params={"model_id": "basins_heihe_shud", "source_id": "IFS", "cycle_time": LATEST_CYCLE},
            )
            for _ in range(3)
        ]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[0].json()["data"] == responses[1].json()["data"] == responses[2].json()["data"]
    assert expected_path.stat().st_mtime_ns == before


def test_real_disk_station_series_response_shape_matches_baseline_fixture() -> None:
    baseline = json.loads(BASELINE_FIXTURE.read_text(encoding="utf-8"))

    with _client() as client:
        response = client.get(
            "/api/v1/met/stations/heihe_forc_001/series",
            params={"model_id": "basins_heihe_shud", "source_id": "IFS", "cycle_time": LATEST_CYCLE},
        )

    assert response.status_code == 200, response.text
    _assert_station_series_shape(response.json(), baseline)


def _client() -> TestClient:
    root = _real_object_store_root()
    app = create_app(
        {
            "NHMS_REQUIRE_SERVICE_ROLE": "true",
            "NHMS_SERVICE_ROLE": "display_readonly",
            "OBJECT_STORE_ROOT": str(root),
        }
    )
    return TestClient(app)


def _real_object_store_root() -> Path:
    if os.getenv("NHMS_RUN_REAL_DISK", "").strip().lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("real-disk tests require NHMS_RUN_REAL_DISK=1")
    if not os.getenv("DATABASE_URL", "").strip():
        pytest.skip("real-disk tests require DATABASE_URL")
    raw_root = os.getenv("OBJECT_STORE_ROOT", "").strip()
    if not raw_root:
        pytest.skip("real-disk tests require OBJECT_STORE_ROOT")
    root = Path(raw_root).expanduser().resolve()
    if not root.is_dir():
        pytest.skip(f"real-disk OBJECT_STORE_ROOT is not a directory: {root}")
    return root


def _dt(value: str) -> Any:
    from datetime import UTC, datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _assert_station_series_shape(actual: dict[str, Any], baseline: dict[str, Any]) -> None:
    assert list(actual.keys()) == list(baseline.keys())
    assert isinstance(actual["request_id"], str)
    assert actual["status"] == baseline["status"] == "ok"

    actual_data = actual["data"]
    baseline_data = baseline["data"]
    assert list(actual_data.keys()) == list(baseline_data.keys())
    _assert_ordered_shape(actual_data["station"], baseline_data["station"], path="data.station")

    for key, actual_value in actual_data.items():
        if key in {"station", "series"}:
            continue
        assert _json_kind(actual_value) == _json_kind(baseline_data[key]), key

    actual_variables = [series["variable"] for series in actual_data["series"]]
    baseline_variables = [series["variable"] for series in baseline_data["series"]]
    assert actual_variables == [variable for variable in baseline_variables if variable in actual_variables]

    baseline_by_variable = {series["variable"]: series for series in baseline_data["series"]}
    for series in actual_data["series"]:
        baseline_series = baseline_by_variable[series["variable"]]
        _assert_series_shape(series, baseline_series)


def _assert_series_shape(actual: dict[str, Any], baseline: dict[str, Any]) -> None:
    assert list(actual.keys()) == list(baseline.keys())
    assert list(actual["metadata"].keys()) == list(baseline["metadata"].keys())
    _assert_ordered_shape(actual["metadata"], baseline["metadata"], path=f"series[{actual['variable']}].metadata")

    for key, actual_value in actual.items():
        if key in {"points", "metadata"}:
            continue
        assert _json_kind(actual_value) == _json_kind(baseline[key]), f"series[{actual['variable']}].{key}"

    assert actual["points"], f"series[{actual['variable']}].points must not be empty"
    valid_times = [point["valid_time"] for point in actual["points"]]
    assert valid_times == sorted(valid_times, key=_dt)

    baseline_point = baseline["points"][0]
    for index, point in enumerate(actual["points"]):
        assert list(point.keys()) == list(baseline_point.keys())
        for key, actual_value in point.items():
            assert _json_kind(actual_value) == _json_kind(baseline_point[key]), (
                f"series[{actual['variable']}].points[{index}].{key}"
            )


def _assert_ordered_shape(actual: dict[str, Any], baseline: dict[str, Any], *, path: str) -> None:
    assert list(actual.keys()) == list(baseline.keys()), path
    for key, actual_value in actual.items():
        baseline_value = baseline[key]
        current_path = f"{path}.{key}"
        assert _json_kind(actual_value) == _json_kind(baseline_value), current_path
        if isinstance(actual_value, dict):
            _assert_ordered_shape(actual_value, baseline_value, path=current_path)
        elif isinstance(actual_value, list):
            assert len(actual_value) == len(baseline_value), current_path
            for index, item in enumerate(actual_value):
                if isinstance(item, dict):
                    _assert_ordered_shape(item, baseline_value[index], path=f"{current_path}[{index}]")
                else:
                    assert _json_kind(item) == _json_kind(baseline_value[index]), f"{current_path}[{index}]"


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
