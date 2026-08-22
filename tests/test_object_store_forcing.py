from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common import object_store_forcing
from packages.common.forecast_store import DEFAULT_STATION_SERIES_LIMIT, ForecastStoreError
from packages.common.object_store_forcing import (
    MAX_STATION_FORCING_CSV_BYTES,
    MAX_STATION_FORCING_CSV_LINE_BYTES,
    MAX_STATION_FORCING_CSV_ROWS,
    STATION_FORCING_CSV_READ_CHUNK_BYTES,
    PsycopgStationLookup,
    StationMetadata,
    _compute_cycle_compact,
    _normalize_source_id,
    _resolve_disk_path,
    raise_station_not_found,
    read_station_forcing_csv,
)
from packages.common.safe_fs import SafeFilesystemError

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


def _station(
    *,
    properties_json: dict[str, Any] | None = None,
    basin_version_id: str = "basins_heihe_vbasins",
) -> StationMetadata:
    if properties_json is None:
        properties_json = {
            "forcing_filename": FORCING_FILENAME,
            "source": "qhh.tsd.forc",
            "model_id": MODEL_ID,
        }
    return StationMetadata(
        station_id=STATION_ID,
        basin_version_id=basin_version_id,
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
        "expected_path": f"forcing/ifs/2026062012/basins_heihe_vbasins/{MODEL_ID}/shud/{FORCING_FILENAME}",
        "basin_version_id": "basins_heihe_vbasins",
        "source_id": "ifs",
        "cycle_time": "2026-06-20T12:00:00Z",
        "model_id": MODEL_ID,
    }
    assert str(tmp_path) not in error.value.message
    assert str(tmp_path) not in json.dumps(error.value.details)
    assert "OBJECT_STORE_ROOT" not in error.value.message


@pytest.mark.parametrize(
    ("source_id", "model_id", "expected_field"),
    [
        ("ifs/../gfs", MODEL_ID, "source_id"),
        (SOURCE_ID, "../other", "model_id"),
        (SOURCE_ID, "subdir/other", "model_id"),
        (SOURCE_ID, "/tmp/other", "model_id"),
    ],
)
def test_unsafe_api_path_segments_reject_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
    model_id: str,
    expected_field: str,
) -> None:
    root = tmp_path / "object-store"
    root.mkdir()
    open_calls: list[Path] = []

    def fail_open(path: Path, **_kwargs: Any) -> int:
        open_calls.append(path)
        raise AssertionError(f"reader must reject unsafe path before open: {path}")

    monkeypatch.setattr(object_store_forcing, "open_file_no_follow", fail_open)

    with pytest.raises(ForecastStoreError) as error:
        _read(root, source_id=source_id, model_id=model_id)

    assert error.value.status_code == 422
    assert error.value.code == "VALIDATION_ERROR"
    assert error.value.details["field"] == expected_field
    assert open_calls == []


@pytest.mark.parametrize(
    ("station", "reason_field"),
    [
        (_station(properties_json={"forcing_filename": "../x.csv"}), "forcing_filename"),
        (_station(properties_json={"forcing_filename": "subdir/x.csv"}), "forcing_filename"),
        (_station(properties_json={"forcing_filename": "/tmp/x.csv"}), "forcing_filename"),
        (_station(properties_json={"forcing_filename": "bad\\name.csv"}), "forcing_filename"),
        (_station(properties_json={"forcing_filename": "bad\x00name.csv"}), "forcing_filename"),
        (_station(properties_json={"forcing_filename": "."}), "forcing_filename"),
        (_station(properties_json={"forcing_filename": ".."}), "forcing_filename"),
        (_station(basin_version_id="../basins"), "station.basin_version_id"),
    ],
)
def test_unsafe_station_path_segments_reject_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    station: StationMetadata,
    reason_field: str,
) -> None:
    open_calls: list[Path] = []

    def fail_open(path: Path, **_kwargs: Any) -> int:
        open_calls.append(path)
        raise AssertionError(f"reader must reject unsafe path before open: {path}")

    monkeypatch.setattr(object_store_forcing, "open_file_no_follow", fail_open)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path, station_lookup=FakeStationLookup({STATION_ID: station}))

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert reason_field in error.value.details["parse_reason"]
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
    _write_csv(tmp_path, raw_content=raw_content)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert error.value.details["station_id"] == STATION_ID
    assert error.value.details["expected_path"] == (
        f"forcing/ifs/2026062012/basins_heihe_vbasins/{MODEL_ID}/shud/{FORCING_FILENAME}"
    )
    assert str(tmp_path) not in error.value.message
    assert str(tmp_path) not in json.dumps(error.value.details)
    assert "OBJECT_STORE_ROOT" not in error.value.message
    assert reason in error.value.details["parse_reason"]


def test_symlink_target_is_rejected_without_following(tmp_path: Path) -> None:
    path = _write_csv(tmp_path)
    outside = tmp_path / "outside.csv"
    outside.write_text(
        "1\t6\t20260620\t20260627\n"
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        "0\t99\t99\t99\t99\t99\n",
        encoding="utf-8",
    )
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert "symlink" in error.value.details["parse_reason"].lower()


def test_symlink_to_outside_root_is_malformed_not_not_found(tmp_path: Path) -> None:
    path = _write_csv(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.csv"
    outside.write_text(
        "1\t6\t20260620\t20260627\n"
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        "0\t99\t99\t99\t99\t99\n",
        encoding="utf-8",
    )
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert error.value.details["expected_path"] == (
        f"forcing/ifs/2026062012/basins_heihe_vbasins/{MODEL_ID}/shud/{FORCING_FILENAME}"
    )
    assert str(tmp_path) not in error.value.message
    assert str(tmp_path) not in json.dumps(error.value.details)
    assert "OBJECT_STORE_ROOT" not in error.value.message
    assert "symlink" in error.value.details["parse_reason"].lower()


@pytest.mark.parametrize(
    ("open_error", "reason"),
    [
        (PermissionError("permission denied"), "permission denied"),
        (OSError("open failed"), "open failed"),
    ],
)
def test_no_follow_open_errors_map_to_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    open_error: OSError,
    reason: str,
) -> None:
    _write_csv(tmp_path)

    def fail_open(_path: Path, **_kwargs: Any) -> int:
        raise open_error

    monkeypatch.setattr(object_store_forcing, "open_file_no_follow", fail_open)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert reason in error.value.details["parse_reason"]


def test_no_follow_read_oserror_maps_to_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_csv(tmp_path)

    def fail_read(_fd: int, _length: int) -> bytes:
        raise OSError("read failed")

    monkeypatch.setattr(object_store_forcing.os, "read", fail_read)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert "read failed" in error.value.details["parse_reason"]


def test_malformed_csv_extra_rows_reads_bounded_mismatch_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poison_tail = "poison\tthis\tline\tmust\tnot\tbe\tfully\tread\n" * (
        STATION_FORCING_CSV_READ_CHUNK_BYTES // 8
    )
    lines = [
        "1\t6\t20260620\t20260627\n",
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n",
        "0\t1\t2\t3\t4\t5\n",
        "0.125\t1\t2\t3\t4\t5\n",
        poison_tail,
    ]
    content = "".join(lines)
    _write_csv(tmp_path, raw_content=content)
    original_read = object_store_forcing.os.read
    bytes_read = 0
    requested_lengths: list[int] = []

    def spy_read(fd: int, length: int) -> bytes:
        nonlocal bytes_read
        requested_lengths.append(length)
        chunk = original_read(fd, length)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(object_store_forcing.os, "read", spy_read)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert "declared nrow 1 does not match data row count 2" in error.value.details["parse_reason"]
    assert max(requested_lengths) > 1
    assert bytes_read < len(content.encode("utf-8"))


def test_chunked_csv_reader_handles_valid_lines_split_across_tiny_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_csv(tmp_path, time_days=[0.0, 0.125])
    tiny_chunk_size = 7
    original_read = object_store_forcing.os.read
    requested_lengths: list[int] = []
    read_sizes: list[int] = []

    def spy_read(fd: int, length: int) -> bytes:
        requested_lengths.append(length)
        chunk = original_read(fd, length)
        read_sizes.append(len(chunk))
        return chunk

    monkeypatch.setattr(object_store_forcing, "STATION_FORCING_CSV_READ_CHUNK_BYTES", tiny_chunk_size)
    monkeypatch.setattr(object_store_forcing.os, "read", spy_read)

    response = _read(tmp_path)

    assert _total_points(response) == 10
    assert [series["variable"] for series in response["series"]] == VARIABLE_ORDER
    assert [point["valid_time"] for point in response["series"][0]["points"]] == [
        "2026-06-20T12:00:00Z",
        "2026-06-20T15:00:00Z",
    ]
    assert len(requested_lengths) > 1
    assert max(requested_lengths) <= tiny_chunk_size
    assert all(size <= tiny_chunk_size for size in read_sizes)
    assert max(read_sizes) < path.stat().st_size


def test_malformed_csv_large_nrow_rejects_before_data_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    header = f"{MAX_STATION_FORCING_CSV_ROWS + 1}\t6\t20260620\t20260627\n"
    poison_tail = "0\t1\t2\t3\t4\t5\n" * (STATION_FORCING_CSV_READ_CHUNK_BYTES * 2)
    content = header + "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n" + poison_tail
    _write_csv(
        tmp_path,
        raw_content=content,
    )
    original_read = object_store_forcing.os.read
    bytes_read = 0
    requested_lengths: list[int] = []

    def spy_read(fd: int, length: int) -> bytes:
        nonlocal bytes_read
        requested_lengths.append(length)
        chunk = original_read(fd, length)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(object_store_forcing.os, "read", spy_read)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert f"nrow must not exceed {MAX_STATION_FORCING_CSV_ROWS}" in error.value.details["parse_reason"]
    assert max(requested_lengths) > 1
    assert bytes_read <= STATION_FORCING_CSV_READ_CHUNK_BYTES
    assert bytes_read < len(content.encode("utf-8"))


def test_malformed_csv_overlong_line_rejects_without_full_line_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_csv(tmp_path, raw_content=("1" * (MAX_STATION_FORCING_CSV_LINE_BYTES + 100)) + "\n")
    original_read = object_store_forcing.os.read
    bytes_read = 0

    def spy_read(fd: int, length: int) -> bytes:
        nonlocal bytes_read
        chunk = original_read(fd, length)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(object_store_forcing.os, "read", spy_read)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert f"header row exceeds {MAX_STATION_FORCING_CSV_LINE_BYTES} bytes" in error.value.details[
        "parse_reason"
    ]
    assert bytes_read == MAX_STATION_FORCING_CSV_LINE_BYTES + 1


def test_malformed_csv_oversized_file_rejects_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_csv(tmp_path)
    with path.open("r+b") as handle:
        handle.truncate(MAX_STATION_FORCING_CSV_BYTES + 1)

    def fail_read(_fd: int, _length: int) -> bytes:
        raise AssertionError("oversized file must be rejected from fstat before read")

    monkeypatch.setattr(object_store_forcing.os, "read", fail_read)

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert f"file exceeds {MAX_STATION_FORCING_CSV_BYTES} bytes" in error.value.details["parse_reason"]


def test_internal_blank_data_row_is_malformed_not_backfilled_by_extra_row(tmp_path: Path) -> None:
    _write_csv(
        tmp_path,
        raw_content=(
            "2\t6\t20260620\t20260627\n"
            "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
            "0\t1\t2\t3\t4\t5\n"
            "\n"
            "0.125\t1\t2\t3\t4\t5\n"
        ),
    )

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert "data row 2 is blank" in error.value.details["parse_reason"]


def test_trailing_blank_after_declared_section_is_ignored(tmp_path: Path) -> None:
    _write_csv(
        tmp_path,
        raw_content=(
            "1\t6\t20260620\t20260627\n"
            "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
            "0\t1\t2\t3\t4\t5\n"
            "\n"
        ),
    )

    response = _read(tmp_path, variables="PRCP")

    assert len(response["series"][0]["points"]) == 1


@pytest.mark.parametrize(
    ("data_row", "reason"),
    [
        ("nan\t1\t2\t3\t4\t5", "Time_Day is not finite"),
        ("inf\t1\t2\t3\t4\t5", "Time_Day is not finite"),
        ("1e100\t1\t2\t3\t4\t5", "Time_Day overflows datetime range"),
        ("0\tnan\t2\t3\t4\t5", "column Precip is not finite"),
        ("0\t1e309\t2\t3\t4\t5", "column Precip is not finite"),
        ("0\t1\tinf\t3\t4\t5", "column Temp is not finite"),
    ],
)
def test_non_finite_numeric_values_are_malformed(
    tmp_path: Path, data_row: str, reason: str
) -> None:
    _write_csv(
        tmp_path,
        raw_content=(
            "1\t6\t20260620\t20260627\n"
            "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
            f"{data_row}\n"
        ),
    )

    with pytest.raises(ForecastStoreError) as error:
        _read(tmp_path)

    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert reason in error.value.details["parse_reason"]


def test_1_13g_variable_mapping_and_units(tmp_path: Path) -> None:
    _write_csv(tmp_path, time_days=[0.0])

    response = _read(tmp_path)
    json.dumps(response, allow_nan=False)

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


@pytest.mark.parametrize("variables", ["", "   ", " , ", ["", "   "]])
def test_blank_variables_filter_behaves_like_omitted_default_variables(
    tmp_path: Path, variables: str | list[str]
) -> None:
    _write_csv(tmp_path, time_days=[0.0, 0.125])

    omitted = _read(tmp_path, variables=None)
    response = _read(tmp_path, variables=variables)

    assert [series["variable"] for series in response["series"]] == VARIABLE_ORDER
    assert response["series"] == omitted["series"]
    assert _total_points(response) == 5 * 2


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


def test_limit_equal_first_variable_full_count_does_not_mark_prcp_truncated(tmp_path: Path) -> None:
    _write_csv(tmp_path, time_days=[index * 0.125 for index in range(53)])

    response = _read(tmp_path, limit=53)

    assert _total_points(response) == 53
    assert [series["variable"] for series in response["series"]] == ["PRCP"]
    assert len(response["series"][0]["points"]) == 53
    assert response["series"][0]["truncated"] is False
    assert response["series"][0]["metadata"]["truncated"] is False
    assert response["series"][0]["metadata"]["returned_points"] == 53


def test_partial_next_variable_is_marked_truncated_when_limit_cuts_inside_it(tmp_path: Path) -> None:
    _write_csv(tmp_path, time_days=[index * 0.125 for index in range(53)])

    response = _read(tmp_path, limit=54)

    assert _total_points(response) == 54
    assert [series["variable"] for series in response["series"]] == ["PRCP", "TEMP"]
    prcp, temp = response["series"]
    assert len(prcp["points"]) == 53
    assert prcp["truncated"] is False
    assert prcp["metadata"]["truncated"] is False
    assert len(temp["points"]) == 1
    assert temp["truncated"] is True
    assert temp["metadata"]["truncated"] is True
    assert temp["metadata"]["returned_points"] == 1


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
    original_open = object_store_forcing.open_file_no_follow
    open_calls: list[tuple[Path, Path | None]] = []

    def fail_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"reader must not mkdir: {self}")

    def spy_open(path: Path, *, containment_root: Path | None = None) -> int:
        open_calls.append((path, containment_root))
        return original_open(path, containment_root=containment_root)

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    monkeypatch.setattr(object_store_forcing, "open_file_no_follow", spy_open)

    responses = [_read(tmp_path) for _ in range(3)]

    assert responses[0] == responses[1] == responses[2]
    assert path.stat().st_mtime_ns == mtime_ns
    assert open_calls == [(path, tmp_path), (path, tmp_path), (path, tmp_path)]


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


# --- #1660: concurrent atomic replacement inside the no-follow open window ---
#
# The producer publishes each shud CSV with os.replace, which swaps the target
# inode; open_file_no_follow refuses that swap with kind="identity_changed".
# These cases pin the bounded retry that absorbs it.  Note the namespace:
# object_store_forcing does `from packages.common.safe_fs import
# open_file_no_follow`, so the spy MUST be installed on
# packages.common.object_store_forcing — patching safe_fs would not bite.


def _read_lines(
    root: Path,
    *,
    attempts: int | None = None,
    station: StationMetadata | None = None,
) -> list[str]:
    """Drive _read_csv_lines directly so `attempts` can be injected.

    read_station_forcing_csv deliberately does not expose the knob (design D5),
    so the injecting cases bind to the private seam that owns the retry.
    """

    station = station or _station()
    path = _resolve_disk_path(
        root,
        _normalize_source_id(SOURCE_ID),
        _compute_cycle_compact(CYCLE_TIME),
        station.basin_version_id,
        MODEL_ID,
        station.forcing_filename or FORCING_FILENAME,
    )
    kwargs: dict[str, Any] = {
        "expected_storage_key": object_store_forcing._object_store_relative_path(root, path),
        "object_store_root": root,
        "station_id": station.station_id,
        "basin_version_id": station.basin_version_id,
        "source_id": _normalize_source_id(SOURCE_ID),
        "cycle_time": CYCLE_TIME,
        "model_id": MODEL_ID,
        "active_flag": station.active_flag,
    }
    if attempts is not None:
        kwargs["attempts"] = attempts
    return object_store_forcing._read_csv_lines(path, **kwargs)


def _install_open_spy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failures: int = 0,
    error_factory: Any = None,
) -> list[Path]:
    """Count open_file_no_follow calls; fail the first `failures` of them."""

    real_open = object_store_forcing.open_file_no_follow
    calls: list[Path] = []

    def spy(path: Path, **kwargs: Any) -> int:
        calls.append(Path(path))
        if error_factory is not None and len(calls) <= failures:
            raise error_factory(len(calls))
        return real_open(path, **kwargs)

    monkeypatch.setattr(object_store_forcing, "open_file_no_follow", spy)
    return calls


def _identity_changed(_attempt: int) -> SafeFilesystemError:
    return SafeFilesystemError("Target file changed while being opened: /x/y.csv", kind="identity_changed")


def test_concurrent_replace_retry_absorbs_identity_change_and_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.1 — a replacement that stops inside the bound is absorbed.

    attempts=4 is injected on purpose (!= the module constant 3): a helper that
    ignored the parameter and hard-used the constant would exhaust on attempt 3
    and never reach the successful open.
    """

    _write_csv(tmp_path, time_days=[0.0, 0.125])
    calls = _install_open_spy(monkeypatch, failures=3, error_factory=_identity_changed)

    lines = _read_lines(tmp_path, attempts=4)

    assert len(calls) == 4
    assert lines[0] == "2\t6\t20260620\t20260627"
    assert lines[1] == "Time_Day\tPrecip\tTemp\tRH\tWind\tRN"
    assert len(lines) == 4


def test_concurrent_replace_retry_exhausted_fails_closed_with_prefixed_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.2 — exhausting the bound keeps the pre-change failure contract.

    attempts=2 is injected on purpose (!= the module constant 3) so a helper
    that ignored the parameter would be caught by the call count.
    """

    _write_csv(tmp_path)
    calls = _install_open_spy(monkeypatch, failures=99, error_factory=_identity_changed)

    with pytest.raises(ForecastStoreError) as error:
        _read_lines(tmp_path, attempts=2)

    assert isinstance(error.value, object_store_forcing.StationForcingFileMalformedError)
    assert error.value.status_code == 500
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert error.value.details["parse_reason"].startswith("concurrent-replace")
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["unsafe", "io"])
def test_non_identity_refusals_are_not_retried_and_keep_verbatim_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """3.3 — default-deny: only identity_changed retries, and only it is relabelled."""

    _write_csv(tmp_path)
    raised = SafeFilesystemError(f"Target file must be a regular file: {tmp_path}/x.csv", kind=kind)
    calls = _install_open_spy(monkeypatch, failures=99, error_factory=lambda _attempt: raised)

    with pytest.raises(ForecastStoreError) as error:
        _read_lines(tmp_path)

    assert len(calls) == 1
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    reason = error.value.details["parse_reason"]
    assert "concurrent-replace" not in reason
    assert reason == object_store_forcing._public_error_reason(raised)


def test_retry_decision_follows_kind_not_message_text_for_unfamiliar_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.4a — identity_changed with wording unlike the primitive's still retries."""

    _write_csv(tmp_path, time_days=[0.0, 0.125])

    def alien_wording(_attempt: int) -> SafeFilesystemError:
        return SafeFilesystemError("inode moved", kind="identity_changed")

    calls = _install_open_spy(monkeypatch, failures=2, error_factory=alien_wording)

    lines = _read_lines(tmp_path, attempts=3)

    assert len(calls) == 3
    assert len(lines) == 4


def test_retry_decision_follows_kind_not_message_text_for_lookalike_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.4b — the primitive's identity wording under another kind does NOT retry."""

    _write_csv(tmp_path)
    lookalike = SafeFilesystemError("Target file changed while being opened: /x", kind="unsafe")
    calls = _install_open_spy(monkeypatch, failures=99, error_factory=lambda _attempt: lookalike)

    with pytest.raises(ForecastStoreError) as error:
        _read_lines(tmp_path, attempts=3)

    assert len(calls) == 1
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert "concurrent-replace" not in error.value.details["parse_reason"]


def test_parse_failure_on_opened_descriptor_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.5 — a content defect behind a successful open surfaces on first sight."""

    _write_csv(
        tmp_path,
        raw_content=(
            "2\t6\t20260620\t20260627\n"
            "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
            "0\t1.000\t271.000\t0.510\t4.000\t101.000\n"
            "\n"
        ),
    )
    calls = _install_open_spy(monkeypatch)

    with pytest.raises(ForecastStoreError) as error:
        _read_lines(tmp_path, attempts=3)

    assert len(calls) == 1
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
    assert "concurrent-replace" not in error.value.details["parse_reason"]


def test_missing_file_keeps_not_found_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.6 — os.replace leaves no missing-file window, so 404 keeps its shape."""

    path = _write_csv(tmp_path)
    path.unlink()
    calls = _install_open_spy(monkeypatch)

    with pytest.raises(ForecastStoreError) as error:
        _read_lines(tmp_path, attempts=3)

    assert isinstance(error.value, object_store_forcing.StationForcingFileNotFoundError)
    assert error.value.status_code == 404
    assert len(calls) == 1


def test_identity_retry_never_sleeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """3.7 — the bound is delivered without an interval knob (design D5).

    Asserted along a sequence that really retries: a clean read would exercise
    no retry at all and would stay green under a sleeping implementation.
    """

    _write_csv(tmp_path, time_days=[0.0, 0.125])
    calls = _install_open_spy(monkeypatch, failures=2, error_factory=_identity_changed)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    lines = _read_lines(tmp_path, attempts=3)

    assert len(calls) == 3
    assert len(lines) == 4
    assert sleeps == []


def test_identity_changed_raised_during_parse_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.8 — the retry's SCOPE is the open, not the parse.

    Without this case, widening the retry to wrap the whole open+parse block is
    an equivalent mutant: the selector is still the kind, and a normal parse
    raises only ValueError/OSError, which never matches it.
    """

    _write_csv(tmp_path)
    calls = _install_open_spy(monkeypatch)

    def boom(_self: Any, _line_label: str) -> str | None:
        raise SafeFilesystemError("Target file changed while being opened: /x", kind="identity_changed")

    monkeypatch.setattr(object_store_forcing._ChunkedBoundedCsvLineReader, "readline", boom)

    with pytest.raises(ForecastStoreError) as error:
        _read_lines(tmp_path, attempts=3)

    assert len(calls) == 1
    assert error.value.code == "STATION_FORCING_FILE_MALFORMED"
