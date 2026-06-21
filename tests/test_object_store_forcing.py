from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.forecast_store import DEFAULT_STATION_SERIES_LIMIT, ForecastStoreError
from packages.common.object_store_forcing import (
    PsycopgStationLookup,
    StationMetadata,
    _compute_cycle_compact,
    _normalize_source_id,
    _resolve_disk_path,
    raise_station_not_found,
    read_station_forcing_csv,
)

CYCLE_TIME = datetime(2026, 6, 20, 12, tzinfo=UTC)
MODEL_ID = "basins_heihe_shud"
SOURCE_ID = "IFS"
STATION_ID = "heihe_forc_001"
FORCING_FILENAME = "X100.75Y37.65.csv"
VARIABLE_ORDER = ["PRCP", "TEMP", "RH", "wind", "Rn"]


class FakeStationLookup:
    def __init__(self, stations: dict[str, StationMetadata] | None = None) -> None:
        self.stations = stations or {STATION_ID: _station()}

    def lookup(self, station_id: str) -> StationMetadata:
        station = self.stations.get(station_id)
        if station is None:
            raise_station_not_found(station_id)
        return station


def _station(*, properties_json: dict[str, Any] | None = None) -> StationMetadata:
    if properties_json is None:
        properties_json = {
            "forcing_filename": FORCING_FILENAME,
            "source": "qhh.tsd.forc",
            "model_id": MODEL_ID,
        }
    return StationMetadata(
        station_id=STATION_ID,
        basin_version_id="basins_heihe_vbasins",
        station_name="HEIHE forcing station 001",
        longitude=100.75,
        latitude=37.650000555388,
        elevation_m=0.0,
        station_role="forcing_grid",
        active_flag=True,
        properties_json=properties_json,
    )


def _write_csv(
    root: Path,
    *,
    station: StationMetadata | None = None,
    cycle_time: datetime = CYCLE_TIME,
    source_id: str = SOURCE_ID,
    model_id: str = MODEL_ID,
    time_days: list[float] | None = None,
    raw_content: str | None = None,
    declared_nrow: int | None = None,
) -> Path:
    station = station or _station()
    path = _resolve_disk_path(
        root,
        _normalize_source_id(source_id),
        _compute_cycle_compact(cycle_time),
        station.basin_version_id,
        model_id,
        station.forcing_filename or FORCING_FILENAME,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw_content is not None:
        path.write_text(raw_content, encoding="utf-8")
        return path

    time_days = [0.0, 0.125] if time_days is None else time_days
    nrow = len(time_days) if declared_nrow is None else declared_nrow
    lines = [
        f"{nrow}\t6\t20260620\t20260627",
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN",
    ]
    for index, time_day in enumerate(time_days, start=1):
        lines.append(
            "\t".join(
                [
                    f"{time_day:g}",
                    f"{index:.3f}",
                    f"{270 + index:.3f}",
                    f"{0.5 + index / 100:.3f}",
                    f"{3 + index:.3f}",
                    f"{100 + index:.3f}",
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _read(
    root: Path,
    *,
    station_lookup: Any | None = None,
    station_id: str = STATION_ID,
    source_id: str = SOURCE_ID,
    cycle_time: datetime = CYCLE_TIME,
    model_id: str = MODEL_ID,
    variables: str | list[str] | None = None,
    from_time: datetime | str | None = None,
    to_time: datetime | str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    return read_station_forcing_csv(
        station_lookup=station_lookup or FakeStationLookup(),
        object_store_root=root,
        station_id=station_id,
        source_id=source_id,
        cycle_time=cycle_time,
        model_id=model_id,
        variables=variables,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _total_points(response: dict[str, Any]) -> int:
    return sum(len(series["points"]) for series in response["series"])


def test_1_13a_path_resolution_heihe_ifs_happy_path(tmp_path: Path) -> None:
    station = _station()
    expected = (
        tmp_path
        / "forcing"
        / "ifs"
        / "2026062012"
        / "basins_heihe_vbasins"
        / MODEL_ID
        / "shud"
        / FORCING_FILENAME
    )

    resolved = _resolve_disk_path(
        tmp_path,
        _normalize_source_id("IFS"),
        _compute_cycle_compact(CYCLE_TIME),
        station.basin_version_id,
        MODEL_ID,
        FORCING_FILENAME,
    )

    assert resolved == expected
    _write_csv(tmp_path, station=station)
    response = _read(tmp_path, station_lookup=FakeStationLookup({STATION_ID: station}))
    assert response["station_id"] == STATION_ID
    assert response["source_id"] == "IFS"


def test_1_13b_cycle_utc_normalization_three_input_forms() -> None:
    assert _compute_cycle_compact(datetime(2026, 6, 20, 12)) == "2026062012"
    assert _compute_cycle_compact(_dt("2026-06-20T12:00:00Z")) == "2026062012"
    assert _compute_cycle_compact(_dt("2026-06-20T20:00:00+08:00")) == "2026062012"


def test_1_13c_station_not_found_reuses_existing_error_shape(tmp_path: Path) -> None:
    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path, station_lookup=FakeStationLookup({}), station_id="missing")

    assert error.value.status_code == 404
    assert error.value.code == "STATION_NOT_FOUND"
    assert error.value.details == {"station_id": "missing"}


def test_1_13d_forcing_filename_missing_returns_500(tmp_path: Path) -> None:
    station = _station(properties_json={"source": "fixture"})
    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path, station_lookup=FakeStationLookup({STATION_ID: station}))

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILENAME_MISSING"
    assert error.value.details == {"station_id": STATION_ID}


def test_1_13e_file_not_found_includes_operator_details(tmp_path: Path) -> None:
    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 404
    assert error.value.code == "STATION_FORCING_FILE_NOT_FOUND"
    assert error.value.details == {
        "station_id": STATION_ID,
        "expected_path": str(
            tmp_path
            / "forcing"
            / "ifs"
            / "2026062012"
            / "basins_heihe_vbasins"
            / MODEL_ID
            / "shud"
            / FORCING_FILENAME
        ),
        "basin_version_id": "basins_heihe_vbasins",
        "source_id": "ifs",
        "cycle_time": "2026-06-20T12:00:00Z",
        "model_id": MODEL_ID,
    }


@pytest.mark.parametrize("case", ["absolute_model_id", "relative_source_id"])
def test_path_containment_rejects_api_controlled_traversal_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    root = tmp_path / "object-store"
    root.mkdir()
    outside_model = tmp_path / "outside-model"
    if case == "absolute_model_id":
        model_id = str(outside_model)
        source_id = SOURCE_ID
        expected = outside_model / "shud" / FORCING_FILENAME
    else:
        model_id = MODEL_ID
        source_id = "../../outside-source"
        expected = (
            tmp_path
            / "outside-source"
            / "2026062012"
            / "basins_heihe_vbasins"
            / MODEL_ID
            / "shud"
            / FORCING_FILENAME
        )
    open_calls: list[Path] = []
    original_open = Path.open

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        open_calls.append(self)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)

    with pytest.raises(ForecastStoreError) as error:
        _read(root, source_id=source_id, model_id=model_id)

    assert error.value.status_code == 404
    assert error.value.code == "STATION_FORCING_FILE_NOT_FOUND"
    assert error.value.details["expected_path"] == str(expected.resolve(strict=False))
    assert error.value.details["model_id"] == model_id
    assert open_calls == []


@pytest.mark.parametrize(
    ("raw_content", "reason"),
    [
        ("Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n0\t1\t2\t3\t4\t5\n", "header row"),
        ("x\t6\t20260620\t20260627\nTime_Day\tPrecip\tTemp\tRH\tWind\tRN\n", "nrow"),
        ("1\t6\t20260620\t20260627\nTime_Day\tPrecip\tTemp\tRH\tWind\tRN\n0\t1\n", "columns"),
        ("1\t6\t20260620\t20260627\nTime_Day\tPrecip\tTemp\tRH\tWind\tRN\n0\tbad\t2\t3\t4\t5\n", "non-numeric"),
        ("", "file is empty"),
        ("2\t6\t20260620\t20260627\nTime_Day\tPrecip\tTemp\tRH\tWind\tRN\n0\t1\t2\t3\t4\t5\n", "nrow"),
    ],
)
def test_1_13f_malformed_csv_variants_return_stable_error(
    tmp_path: Path, raw_content: str, reason: str
) -> None:
    path = _write_csv(tmp_path, raw_content=raw_content)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert error.value.details["station_id"] == STATION_ID
    assert error.value.details["expected_path"] == str(path)
    assert reason in error.value.details["parse_reason"]


def test_malformed_csv_extra_rows_reads_only_one_mismatch_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BoundedHandle:
        def __init__(self) -> None:
            self.readline_calls = 0
            self.lines = [
                "1\t6\t20260620\t20260627\n",
                "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n",
                "0\t1\t2\t3\t4\t5\n",
                "0.125\t1\t2\t3\t4\t5\n",
                "poison\tthis\tline\tmust\tnot\tbe\tread\n",
            ]

        def __enter__(self) -> "BoundedHandle":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def readline(self) -> str:
            self.readline_calls += 1
            if self.readline_calls > 4:
                raise AssertionError("reader consumed beyond the single extra mismatch probe")
            return self.lines.pop(0)

    handle = BoundedHandle()

    def fake_open(self: Path, *args: Any, **kwargs: Any) -> BoundedHandle:
        return handle

    monkeypatch.setattr(Path, "open", fake_open)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert "declared nrow 1 does not match data row count 2" in error.value.details["parse_reason"]
    assert handle.readline_calls == 4


def test_1_13g_variable_mapping_and_units(tmp_path: Path) -> None:
    _write_csv(tmp_path, time_days=[0.0])

    response = _read(tmp_path)

    assert [series["variable"] for series in response["series"]] == VARIABLE_ORDER
    assert {series["variable"]: series["unit"] for series in response["series"]} == {
        "PRCP": "mm/day",
        "TEMP": "degC",
        "RH": "0-1",
        "wind": "m/s",
        "Rn": "W/m^2",
    }


def test_1_13h_valid_time_first_and_last_row_boundaries(tmp_path: Path) -> None:
    _write_csv(tmp_path, time_days=[0.0, 6.5])

    response = _read(tmp_path, variables="PRCP")

    points = response["series"][0]["points"]
    assert points[0]["valid_time"] == "2026-06-20T12:00:00Z"
    assert points[-1]["valid_time"] == "2026-06-27T00:00:00Z"


def test_1_13i_time_day_rounding_uses_round_not_truncation(tmp_path: Path) -> None:
    _write_csv(tmp_path, time_days=[0.041666])

    response = _read(tmp_path, variables="PRCP")

    assert response["series"][0]["points"][0]["valid_time"] == "2026-06-20T13:00:00Z"


def test_1_13j_variables_filter_single_variable(tmp_path: Path) -> None:
    _write_csv(tmp_path, time_days=[0.0, 0.125])

    response = _read(tmp_path, variables="PRCP")

    assert [series["variable"] for series in response["series"]] == ["PRCP"]
    assert len(response["series"][0]["points"]) == 2


def test_1_13k_press_request_is_silently_dropped(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    response = _read(tmp_path, variables="Press")

    assert response["series"] == []


def test_1_13l_prcp_press_request_returns_only_prcp(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    response = _read(tmp_path, variables="PRCP,Press")

    assert [series["variable"] for series in response["series"]] == ["PRCP"]


def test_1_13m_unknown_variable_is_silently_dropped(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    response = _read(tmp_path, variables="UnknownVariable")

    assert response["series"] == []


def test_1_13n_from_to_filter_is_inclusive(tmp_path: Path) -> None:
    _write_csv(tmp_path, time_days=[0.0, 0.125, 0.25, 0.375])

    response = _read(
        tmp_path,
        variables="PRCP",
        from_time="2026-06-20T15:00:00Z",
        to_time="2026-06-20T18:00:00Z",
    )

    assert [point["valid_time"] for point in response["series"][0]["points"]] == [
        "2026-06-20T15:00:00Z",
        "2026-06-20T18:00:00Z",
    ]


def test_1_13o_reversed_time_window_returns_empty_series(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    response = _read(
        tmp_path,
        variables="PRCP",
        from_time="2026-06-21T00:00:00Z",
        to_time="2026-06-20T00:00:00Z",
    )

    assert response["series"] == []


def test_1_13p_limit_truncates_total_tuple_stream_in_variable_order(tmp_path: Path) -> None:
    _write_csv(tmp_path, time_days=[index * 0.125 for index in range(53)])

    response = _read(tmp_path, limit=10)

    assert _total_points(response) == 10
    assert [series["variable"] for series in response["series"]] == ["PRCP"]
    assert response["series"][0]["truncated"] is True
    assert response["series"][0]["metadata"]["truncated"] is True
    assert [point["valid_time"] for point in response["series"][0]["points"]][:3] == [
        "2026-06-20T12:00:00Z",
        "2026-06-20T15:00:00Z",
        "2026-06-20T18:00:00Z",
    ]


@pytest.mark.parametrize("row_count", [1, 53, 56, 100])
def test_1_13q_default_tuples_are_five_times_declared_row_count(tmp_path: Path, row_count: int) -> None:
    _write_csv(tmp_path, time_days=[index * 0.125 for index in range(row_count)])

    response = _read(tmp_path)

    assert _total_points(response) == 5 * row_count
    assert [len(series["points"]) for series in response["series"]] == [row_count] * 5


def test_1_13r_response_shape_matches_baseline_fixture_for_emitted_variables(tmp_path: Path) -> None:
    baseline_path = Path("tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["data"]
    cycle_time = _dt("2026-06-01T00:00:00Z")
    _write_csv(tmp_path, cycle_time=cycle_time, time_days=[0.0, 0.125])

    response = _read(tmp_path, cycle_time=cycle_time)

    assert list(response.keys()) == list(baseline.keys())
    assert list(response["station"].keys()) == list(baseline["station"].keys())
    assert [series["variable"] for series in response["series"]] == VARIABLE_ORDER
    baseline_by_variable = {series["variable"]: series for series in baseline["series"]}
    for series in response["series"]:
        baseline_series = baseline_by_variable[series["variable"]]
        assert list(series.keys()) == list(baseline_series.keys())
        assert list(series["points"][0].keys()) == list(baseline_series["points"][0].keys())
        assert list(series["metadata"].keys()) == list(baseline_series["metadata"].keys())
        for key, value in series["points"][0].items():
            assert isinstance(value, type(baseline_series["points"][0][key]))


class _SpyCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> "_SpyCursor":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self.executions.append((statement, parameters))

    def fetchone(self) -> dict[str, Any]:
        return {
            "station_id": STATION_ID,
            "basin_version_id": "basins_heihe_vbasins",
            "station_name": "HEIHE forcing station 001",
            "lon": 100.75,
            "lat": 37.650000555388,
            "elevation_m": 0.0,
            "station_role": "forcing_grid",
            "active_flag": True,
            "properties_json": {"forcing_filename": FORCING_FILENAME},
        }


class _SpyConnection:
    def __init__(self, cursor: _SpyCursor) -> None:
        self.cursor_obj = cursor

    def cursor(self) -> _SpyCursor:
        return self.cursor_obj


def test_1_13s_psycopg_lookup_queries_only_met_station_for_complete_read(tmp_path: Path) -> None:
    _write_csv(tmp_path)
    cursor = _SpyCursor()
    lookup = PsycopgStationLookup(connection=_SpyConnection(cursor))

    response = _read(tmp_path, station_lookup=lookup)

    assert response["station_id"] == STATION_ID
    statements = [statement.lower() for statement, _params in cursor.executions]
    assert len(statements) == 1
    assert sum("select" in statement and "met.met_station" in statement for statement in statements) == 1
    assert sum("select" in statement and "met.forcing_version" in statement for statement in statements) == 0
    assert sum("select" in statement and "met.forcing_station_timeseries" in statement for statement in statements) == 0


def test_1_13t_reader_is_side_effect_free_for_repeated_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_csv(tmp_path)
    mtime_ns = path.stat().st_mtime_ns
    original_open = Path.open
    open_modes: list[str] = []

    def fail_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"reader must not mkdir: {self}")

    def spy_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        open_modes.append(mode)
        assert not any(flag in mode for flag in ("w", "a", "x", "+"))
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    monkeypatch.setattr(Path, "open", spy_open)

    responses = [_read(tmp_path) for _ in range(3)]

    assert responses[0] == responses[1] == responses[2]
    assert path.stat().st_mtime_ns == mtime_ns
    assert open_modes == ["r", "r", "r"]


def test_missing_required_filter_reuses_existing_details_shape(tmp_path: Path) -> None:
    with pytest.raises(ForecastStoreError) as error:
        read_station_forcing_csv(
            station_lookup=FakeStationLookup(),
            object_store_root=tmp_path,
            station_id=STATION_ID,
            source_id="",
            cycle_time=CYCLE_TIME,
            model_id=MODEL_ID,
        )

    assert error.value.status_code == 422
    assert error.value.code == "MISSING_REQUIRED_FILTER"
    assert error.value.details == {
        "required_alternatives": [
            ["forcing_version_id"],
            ["model_id", "source_id", "cycle_time"],
        ]
    }
    assert DEFAULT_STATION_SERIES_LIMIT == 500
