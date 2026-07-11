"""B4 single-query inactive-row metadata leak fix.

Change: ``direct-grid-display-cutover`` — Epic #992 SUB-4 (§3.2 /
``object-store-station-series-read`` MODIFIED delta).

For an inactive (``active_flag=false``) / evidence-only station row the
single-station endpoint keeps the stable ``STATION_FORCING_FILE_NOT_FOUND``
code on a disk miss but desensitizes its ``details`` to at most
``{station_id}``. Active-station behavior is unchanged (full 6-field details).

The fix form is pinned to 404-details desensitization: the
``PsycopgStationLookup`` SQL SHALL NOT filter ``active_flag`` in the WHERE
clause — filtering would break the cross-requirement contract that an
inactive post-flip M0 legacy station whose pre-cutover file exists still
serves its series.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.forecast_store import ForecastStoreError
from packages.common.object_store_forcing import (
    PsycopgStationLookup,
    StationForcingFileNotFoundError,
    StationMetadata,
    _compute_cycle_compact,
    _normalize_source_id,
    _resolve_disk_path,
    raise_station_not_found,
    read_station_forcing_csv,
)

STATION_ID = "heihe_forc_001"
BASIN_VERSION_ID = "basins_heihe_vbasins"
SOURCE_ID = "ifs"
MODEL_ID = "basins_heihe_shud"
CYCLE_TIME = datetime(2026, 6, 20, 12, tzinfo=UTC)
FORCING_FILENAME = "X100.75Y37.65.csv"
EXPECTED_STORAGE_KEY = Path(
    f"forcing/{SOURCE_ID}/2026062012/{BASIN_VERSION_ID}/{MODEL_ID}/shud/{FORCING_FILENAME}"
)


class _FakeStationLookup:
    def __init__(self, station: StationMetadata) -> None:
        self._station = station

    def lookup(self, station_id: str) -> StationMetadata:
        if station_id != self._station.station_id:
            raise_station_not_found(station_id)
        return self._station


def _station(*, active_flag: bool | None) -> StationMetadata:
    return StationMetadata(
        station_id=STATION_ID,
        basin_version_id=BASIN_VERSION_ID,
        station_name="HEIHE forcing station 001",
        longitude=100.75,
        latitude=37.65,
        elevation_m=0.0,
        station_role="forcing_grid",
        active_flag=active_flag,
        properties_json={"forcing_filename": FORCING_FILENAME},
    )


def _write_csv_at(root: Path, *, station: StationMetadata) -> Path:
    path = _resolve_disk_path(
        root,
        _normalize_source_id(SOURCE_ID),
        _compute_cycle_compact(CYCLE_TIME),
        station.basin_version_id,
        MODEL_ID,
        station.forcing_filename or FORCING_FILENAME,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "1\t6\t20260620\t20260627\n"
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        "0\t1.000\t271.000\t0.510\t4.000\t101.000\n",
        encoding="utf-8",
    )
    return path


def test_inactive_row_disk_miss_details_desensitized_to_station_id_only(
    tmp_path: Path,
) -> None:
    """(negative) Inactive-row disk miss must not enumerate any 6-field tuple."""
    error = StationForcingFileNotFoundError(
        station_id=STATION_ID,
        expected_path=EXPECTED_STORAGE_KEY,
        basin_version_id=BASIN_VERSION_ID,
        source_id=SOURCE_ID,
        cycle_time=CYCLE_TIME,
        model_id=MODEL_ID,
        active_flag=False,
    )

    assert error.status_code == 404
    assert error.code == "STATION_FORCING_FILE_NOT_FOUND"
    # Exact-dict lock: no extra fields may leak details in the future.
    assert error.details == {"station_id": STATION_ID}
    for leak_field in (
        "expected_path",
        "basin_version_id",
        "source_id",
        "cycle_time",
        "model_id",
    ):
        assert leak_field not in error.details

    # Message desensitization: the object-store-relative storage key,
    # basin_version_id, source_id, model_id, and cycle_time must not appear.
    serialized_message = error.message
    for leak_token in (
        str(EXPECTED_STORAGE_KEY),
        BASIN_VERSION_ID,
        SOURCE_ID,
        MODEL_ID,
        "2026-06-20T12:00:00Z",
        "2026062012",
    ):
        assert leak_token not in serialized_message


def test_active_row_disk_miss_keeps_full_details_no_overfilter() -> None:
    """(positive) Active-station error contract preserves the 6 details fields."""
    error = StationForcingFileNotFoundError(
        station_id=STATION_ID,
        expected_path=EXPECTED_STORAGE_KEY,
        basin_version_id=BASIN_VERSION_ID,
        source_id=SOURCE_ID,
        cycle_time=CYCLE_TIME,
        model_id=MODEL_ID,
        active_flag=True,
    )

    assert error.status_code == 404
    assert error.code == "STATION_FORCING_FILE_NOT_FOUND"
    assert error.details == {
        "station_id": STATION_ID,
        "expected_path": str(EXPECTED_STORAGE_KEY),
        "basin_version_id": BASIN_VERSION_ID,
        "source_id": SOURCE_ID,
        "cycle_time": "2026-06-20T12:00:00Z",
        "model_id": MODEL_ID,
    }
    # Backward-compat: None (unknown flag) keeps the full active-shape too,
    # since active-station behavior is the reference and desensitization is
    # opt-in on explicit ``active_flag=False``.
    unknown_error = StationForcingFileNotFoundError(
        station_id=STATION_ID,
        expected_path=EXPECTED_STORAGE_KEY,
        basin_version_id=BASIN_VERSION_ID,
        source_id=SOURCE_ID,
        cycle_time=CYCLE_TIME,
        model_id=MODEL_ID,
    )
    assert set(unknown_error.details.keys()) == {
        "station_id",
        "expected_path",
        "basin_version_id",
        "source_id",
        "cycle_time",
        "model_id",
    }


def test_inactive_post_flip_m0_legacy_station_disk_hit_returns_series(
    tmp_path: Path,
) -> None:
    """(cross-requirement) Inactive-row disk HIT still returns the series.

    Locks the SUB-5 answerability contract (task 2.2): a post-flip
    ``active_flag=false`` M0 legacy station whose requested pre-cutover file
    exists still resolves through the lookup and returns its series. Uses the
    same ``StationLookup`` seam so the lookup still resolves inactive rows
    (proving the lookup SQL was NOT filtered by ``active_flag``).
    """
    inactive_station = _station(active_flag=False)
    _write_csv_at(tmp_path, station=inactive_station)

    response = read_station_forcing_csv(
        station_lookup=_FakeStationLookup(inactive_station),
        object_store_root=tmp_path,
        station_id=STATION_ID,
        source_id=SOURCE_ID,
        cycle_time=CYCLE_TIME,
        model_id=MODEL_ID,
    )

    assert response["station_id"] == STATION_ID
    assert response["station"]["active_flag"] is False
    # The series body itself is returned (at least one series with points).
    assert len(response["series"]) >= 1
    assert sum(len(item["points"]) for item in response["series"]) >= 1


def test_inactive_row_disk_miss_via_read_path_returns_desensitized_404(
    tmp_path: Path,
) -> None:
    """End-to-end read path: inactive-row disk MISS produces the desensitized 404.

    Confirms the plumbing from ``read_station_forcing_csv`` -> ``_read_csv_lines``
    -> ``StationForcingFileNotFoundError`` passes ``station.active_flag`` end
    to end. The file at the resolved path does not exist.
    """
    inactive_station = _station(active_flag=False)
    # Deliberately do not write the CSV — the resolved disk path must miss.

    with pytest.raises(ForecastStoreError) as excinfo:
        read_station_forcing_csv(
            station_lookup=_FakeStationLookup(inactive_station),
            object_store_root=tmp_path,
            station_id=STATION_ID,
            source_id=SOURCE_ID,
            cycle_time=CYCLE_TIME,
            model_id=MODEL_ID,
        )

    error = excinfo.value
    assert error.status_code == 404
    assert error.code == "STATION_FORCING_FILE_NOT_FOUND"
    assert error.details == {"station_id": STATION_ID}
    assert BASIN_VERSION_ID not in error.message
    assert MODEL_ID not in error.message
    assert str(tmp_path) not in error.message
    assert str(tmp_path) not in json.dumps(error.details)


def test_station_forcing_file_not_found_desensitized_message_omits_leaky_tokens(
    tmp_path: Path,
) -> None:
    """Message on inactive-row does not carry any leaky token."""
    object_store_root = tmp_path / "object-store"
    expected_path = (
        object_store_root
        / "forcing"
        / SOURCE_ID
        / "2026062012"
        / BASIN_VERSION_ID
        / MODEL_ID
        / "shud"
        / FORCING_FILENAME
    )
    error = StationForcingFileNotFoundError(
        station_id=STATION_ID,
        expected_path=expected_path,
        basin_version_id=BASIN_VERSION_ID,
        source_id=SOURCE_ID,
        cycle_time=CYCLE_TIME,
        model_id=MODEL_ID,
        active_flag=False,
    )

    # OBJECT_STORE_ROOT and all leaf identity tokens are absent from the message.
    assert str(object_store_root) not in error.message
    assert "OBJECT_STORE_ROOT" not in error.message
    for leak_token in (
        BASIN_VERSION_ID,
        MODEL_ID,
        SOURCE_ID,
        "2026-06-20T12:00:00Z",
        "2026062012",
        FORCING_FILENAME,
        "shud",
    ):
        assert leak_token not in error.message

    # The message still identifies which station was missed, for operator UX.
    assert STATION_ID in error.message


def test_psycopg_station_lookup_sql_has_no_active_flag_filter() -> None:
    """(regression) Static lock: no ``active_flag`` predicate in the WHERE clause.

    Filtering ``active_flag`` in the lookup would 404 post-flip historical M0
    reads whose files exist, breaking task 2.2's SUB-5 answerability contract,
    and would contradict the deployed ``STATION_NOT_FOUND`` trigger. This test
    is a byte-level regression lock.

    Precision: ``active_flag`` is allowed in the SELECT list (it is returned
    to the caller via ``StationMetadata.active_flag``); it is NOT allowed in
    the WHERE clause.
    """
    source = inspect.getsource(PsycopgStationLookup._lookup_with_cursor)

    # Sanity: the caller-facing SELECT includes ``active_flag``.
    assert "active_flag" in source, (
        "expected active_flag in SELECT list so callers receive it via "
        "StationMetadata"
    )

    # Extract the WHERE clause of the lookup SQL and prove active_flag does
    # not appear as a predicate. The lookup keeps the byte-unchanged
    # `WHERE station_id = %s` predicate.
    where_match = re.search(
        r"WHERE\s+(.+?)(?:\"\"\"|\Z)", source, re.DOTALL | re.IGNORECASE
    )
    assert where_match is not None, "lookup SQL must have a WHERE clause"
    where_clause = where_match.group(1)
    assert "active_flag" not in where_clause, (
        "PsycopgStationLookup._lookup_with_cursor must not filter on "
        "active_flag in the WHERE clause (Change direct-grid-display-cutover "
        "§3.2 non-goal)"
    )

    # Raw-file byte cross-check: the fix must not sneak an active_flag filter
    # in via string concatenation outside the inline SQL literal.
    module_path = Path(inspect.getfile(PsycopgStationLookup))
    module_source = module_path.read_text(encoding="utf-8")
    # Find the `_lookup_with_cursor` function span and inspect its SQL.
    fn_match = re.search(
        r"def _lookup_with_cursor\(.*?(?=\n    def |\nclass |\Z)",
        module_source,
        re.DOTALL,
    )
    assert fn_match is not None, "could not locate _lookup_with_cursor in module"
    fn_source = fn_match.group(0)
    where_match_file = re.search(
        r"WHERE\s+(.+?)(?:\"\"\"|\Z)", fn_source, re.DOTALL | re.IGNORECASE
    )
    assert where_match_file is not None
    assert "active_flag" not in where_match_file.group(1)


def test_inactive_row_disk_miss_details_is_json_serializable() -> None:
    """The desensitized details block round-trips through JSON cleanly."""
    error = StationForcingFileNotFoundError(
        station_id=STATION_ID,
        expected_path=EXPECTED_STORAGE_KEY,
        basin_version_id=BASIN_VERSION_ID,
        source_id=SOURCE_ID,
        cycle_time=CYCLE_TIME,
        model_id=MODEL_ID,
        active_flag=False,
    )
    payload: dict[str, Any] = json.loads(json.dumps(error.details))
    assert payload == {"station_id": STATION_ID}
