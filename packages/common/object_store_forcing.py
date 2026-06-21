from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from packages.common.forecast_store import (
    DEFAULT_STATION_SERIES_LIMIT,
    MAX_STATION_SERIES_LIMIT,
    ForecastStoreError,
    _PsycopgTransaction,
    default_database_url,
)

FORCING_VARIABLES = ("PRCP", "TEMP", "RH", "wind", "Rn")
CSV_VARIABLE_COLUMNS = {
    "Precip": ("PRCP", "mm/day"),
    "Temp": ("TEMP", "degC"),
    "RH": ("RH", "0-1"),
    "Wind": ("wind", "m/s"),
    "RN": ("Rn", "W/m^2"),
}
CSV_COLUMNS = ("Time_Day", "Precip", "Temp", "RH", "Wind", "RN")
NATIVE_RESOLUTION = "3h"


@dataclass(frozen=True)
class StationMetadata:
    station_id: str
    basin_version_id: str
    station_name: str | None
    longitude: float | None
    latitude: float | None
    elevation_m: float | None
    station_role: str | None
    active_flag: bool | None
    properties_json: Mapping[str, Any]

    @property
    def forcing_filename(self) -> str | None:
        filename = self.properties_json.get("forcing_filename")
        if filename is None:
            return None
        normalized = str(filename).strip()
        return normalized or None

    def response(self) -> dict[str, Any]:
        station = {
            "station_id": self.station_id,
            "basin_version_id": self.basin_version_id,
            "station_name": self.station_name,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "elevation_m": self.elevation_m,
            "station_role": self.station_role,
            "active_flag": self.active_flag,
            "properties_json": dict(self.properties_json),
            "name": self.station_name,
            "elevation": self.elevation_m,
        }
        return station


@dataclass(frozen=True)
class ShudCsvHeader:
    nrow: int
    ncol: int
    start_date: str
    end_date: str


class StationLookup(Protocol):
    def lookup(self, station_id: str) -> StationMetadata:
        ...


class ObjectStoreForcingError(ForecastStoreError):
    pass


class StationForcingFilenameMissingError(ObjectStoreForcingError):
    def __init__(self, *, station_id: str) -> None:
        super().__init__(
            status_code=500,
            code="STATION_FORCING_FILENAME_MISSING",
            message=f"Station forcing filename is missing: {station_id}",
            details={"station_id": station_id},
        )


class StationForcingFileNotFoundError(ObjectStoreForcingError):
    def __init__(
        self,
        *,
        station_id: str,
        expected_path: Path,
        basin_version_id: str,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> None:
        super().__init__(
            status_code=404,
            code="STATION_FORCING_FILE_NOT_FOUND",
            message=f"Station forcing file not found: {expected_path}",
            details={
                "station_id": station_id,
                "expected_path": str(expected_path),
                "basin_version_id": basin_version_id,
                "source_id": source_id,
                "cycle_time": _format_time(cycle_time),
                "model_id": model_id,
            },
        )


class StationForcingFileMalformedError(ObjectStoreForcingError):
    def __init__(
        self,
        *,
        station_id: str,
        expected_path: Path,
        parse_reason: str,
    ) -> None:
        super().__init__(
            status_code=500,
            code="STATION_FORCING_FILE_MALFORMED",
            message=f"Station forcing file is malformed: {expected_path}",
            details={
                "station_id": station_id,
                "expected_path": str(expected_path),
                "parse_reason": parse_reason,
            },
        )


class PsycopgStationLookup:
    def __init__(self, database_url: str | None = None, *, connection: Any | None = None) -> None:
        self.database_url = database_url
        self.connection = connection

    @classmethod
    def from_env(cls) -> "PsycopgStationLookup":
        return cls(default_database_url())

    def lookup(self, station_id: str) -> StationMetadata:
        station_id = _required_text(station_id, "station_id")
        if self.connection is not None:
            return self._lookup_with_connection(self.connection, station_id)
        database_url = self.database_url or default_database_url()
        with _PsycopgTransaction(database_url) as cursor:
            return self._lookup_with_cursor(cursor, station_id)

    def _lookup_with_connection(self, connection: Any, station_id: str) -> StationMetadata:
        cursor = connection.cursor()
        if hasattr(cursor, "__enter__"):
            with cursor as scoped_cursor:
                return self._lookup_with_cursor(scoped_cursor, station_id)
        try:
            return self._lookup_with_cursor(cursor, station_id)
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    def _lookup_with_cursor(self, cursor: Any, station_id: str) -> StationMetadata:
        cursor.execute(
            """
            SELECT
                station_id,
                basin_version_id,
                station_name,
                ST_X(geom) AS lon,
                ST_Y(geom) AS lat,
                elevation_m,
                station_role,
                active_flag,
                properties_json
            FROM met.met_station
            WHERE station_id = %s
            """,
            (station_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise_station_not_found(station_id)
        station = _station_metadata_from_row(row)
        if station.forcing_filename is None:
            raise StationForcingFilenameMissingError(station_id=station_id)
        return station


def _normalize_source_id(source_id: str) -> str:
    normalized = str(source_id or "").strip().lower()
    if not normalized:
        raise ValueError("source_id is required")
    return normalized


def _compute_cycle_compact(cycle_time: datetime) -> str:
    return _ensure_utc(cycle_time).strftime("%Y%m%d%H")


def _resolve_disk_path(
    object_store_root: Path,
    source_normalized: str,
    cycle_compact: str,
    basin_version_id: str,
    model_id: str,
    forcing_filename: str,
) -> Path:
    return (
        Path(object_store_root)
        / "forcing"
        / source_normalized
        / cycle_compact
        / basin_version_id
        / model_id
        / "shud"
        / forcing_filename
    )


def _parse_csv_header(line1: str) -> ShudCsvHeader:
    tokens = line1.split()
    if len(tokens) != 4:
        raise ValueError("header row must contain nrow ncol start_date end_date")
    try:
        nrow = int(tokens[0])
        ncol = int(tokens[1])
    except ValueError as error:
        raise ValueError("nrow and ncol must be integers") from error
    if nrow < 1:
        raise ValueError("nrow must be positive")
    if ncol != len(CSV_COLUMNS):
        raise ValueError(f"ncol must be {len(CSV_COLUMNS)}")
    return ShudCsvHeader(nrow=nrow, ncol=ncol, start_date=tokens[2], end_date=tokens[3])


def _parse_csv_data(rows: Iterable[str], cycle_time: datetime) -> Iterable[tuple[str, datetime, float]]:
    row_iter = iter(rows)
    try:
        header_columns = next(row_iter).split()
    except StopIteration as error:
        raise ValueError("column header row is missing") from error
    if set(header_columns) != set(CSV_COLUMNS) or len(header_columns) != len(CSV_COLUMNS):
        raise ValueError("column header must contain Time_Day Precip Temp RH Wind RN")

    indexes = {column: index for index, column in enumerate(header_columns)}
    cycle_time_utc = _ensure_utc(cycle_time)
    for row_number, line in enumerate(row_iter, start=1):
        values = line.split()
        if len(values) != len(header_columns):
            raise ValueError(f"data row {row_number} has {len(values)} columns; expected {len(header_columns)}")
        try:
            time_day = float(values[indexes["Time_Day"]])
        except ValueError as error:
            raise ValueError(f"data row {row_number} has non-numeric Time_Day") from error
        valid_time = cycle_time_utc + timedelta(seconds=int(round(time_day * 86400)))
        for column in CSV_COLUMNS[1:]:
            variable, _unit = CSV_VARIABLE_COLUMNS[column]
            try:
                value = float(values[indexes[column]])
            except ValueError as error:
                raise ValueError(f"data row {row_number} column {column} is non-numeric") from error
            yield (variable, valid_time, value)


def _apply_filters(
    tuples: Iterable[tuple[str, datetime, float]],
    variables: Sequence[str] | str | None,
    from_time: datetime | str | None,
    to_time: datetime | str | None,
    limit: int | None,
    *,
    apply_limit: bool = True,
) -> list[tuple[str, datetime, float]]:
    requested_variables = _station_variable_tokens(variables)
    selected_limit = _station_series_limit(limit) if apply_limit else None
    requested_from = _optional_datetime_filter(from_time, "from")
    requested_to = _optional_datetime_filter(to_time, "to")
    if requested_from is not None and requested_to is not None and requested_from > requested_to:
        return []

    filtered: list[tuple[str, datetime, float]] = []
    requested = set(requested_variables)
    for variable, valid_time, value in tuples:
        valid_time = _ensure_utc(valid_time)
        if variable not in requested:
            continue
        if requested_from is not None and valid_time < requested_from:
            continue
        if requested_to is not None and valid_time > requested_to:
            continue
        filtered.append((variable, valid_time, value))

    order = {variable: index for index, variable in enumerate(FORCING_VARIABLES)}
    filtered.sort(key=lambda item: (order[item[0]], item[1]))
    return filtered[:selected_limit] if selected_limit is not None else filtered


def read_station_forcing_csv(
    *,
    station_lookup: StationLookup,
    object_store_root: Path,
    station_id: str,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    variables: Sequence[str] | str | None = None,
    from_time: datetime | str | None = None,
    to_time: datetime | str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    _raise_missing_required_filter_if_needed(model_id=model_id, source_id=source_id, cycle_time=cycle_time)
    station_id = _required_text(station_id, "station_id")
    model_id = _required_text(model_id, "model_id")
    source_normalized = _normalize_source_id(source_id)
    cycle_time_utc = _ensure_utc(cycle_time)
    cycle_compact = _compute_cycle_compact(cycle_time_utc)
    selected_limit = _station_series_limit(limit)
    requested_from = _optional_datetime_filter(from_time, "from")
    requested_to = _optional_datetime_filter(to_time, "to")

    station = station_lookup.lookup(station_id)
    forcing_filename = station.forcing_filename
    if forcing_filename is None:
        raise StationForcingFilenameMissingError(station_id=station_id)

    expected_path = _resolve_disk_path(
        object_store_root=object_store_root,
        source_normalized=source_normalized,
        cycle_compact=cycle_compact,
        basin_version_id=station.basin_version_id,
        model_id=model_id,
        forcing_filename=forcing_filename,
    )
    _ensure_path_under_object_store_root(
        object_store_root=object_store_root,
        expected_path=expected_path,
        station_id=station_id,
        basin_version_id=station.basin_version_id,
        source_id=source_normalized,
        cycle_time=cycle_time_utc,
        model_id=model_id,
    )
    lines = _read_csv_lines(
        expected_path,
        station_id=station_id,
        basin_version_id=station.basin_version_id,
        source_id=source_normalized,
        cycle_time=cycle_time_utc,
        model_id=model_id,
    )
    parsed_tuples = _parse_station_csv(
        lines,
        station_id=station_id,
        expected_path=expected_path,
        cycle_time=cycle_time_utc,
    )
    response_tuples = _apply_filters(parsed_tuples, variables, requested_from, requested_to, selected_limit)
    requested_variables = _station_variable_tokens(variables)
    unbounded_tuples = _apply_filters(parsed_tuples, variables, requested_from, requested_to, None, apply_limit=False)

    return _station_series_response(
        station=station,
        model_id=model_id,
        source_id=source_normalized,
        cycle_time=cycle_time_utc,
        requested_variables=requested_variables,
        requested_from=requested_from,
        requested_to=requested_to,
        limit=selected_limit,
        rows=response_tuples,
        unbounded_rows=unbounded_tuples,
        window_rows=parsed_tuples,
    )


def raise_station_not_found(station_id: str) -> None:
    raise ForecastStoreError(
        status_code=404,
        code="STATION_NOT_FOUND",
        message=f"Station not found: {station_id}",
        details={"station_id": station_id},
    )


def _raise_missing_required_filter_if_needed(
    *,
    model_id: str | None,
    source_id: str | None,
    cycle_time: datetime | str | None,
) -> None:
    if str(model_id or "").strip() and str(source_id or "").strip() and cycle_time is not None:
        return
    raise ForecastStoreError(
        status_code=422,
        code="MISSING_REQUIRED_FILTER",
        message=(
            "forcing_version_id or model_id, source_id, and cycle_time are required "
            "for station series queries."
        ),
        details={
            "required_alternatives": [
                ["forcing_version_id"],
                ["model_id", "source_id", "cycle_time"],
            ]
        },
    )


def _read_csv_lines(
    expected_path: Path,
    *,
    station_id: str,
    basin_version_id: str,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> list[str]:
    try:
        with expected_path.open("r", encoding="utf-8", newline="") as handle:
            raw_header = handle.readline()
            if raw_header == "":
                raise ValueError("file is empty")
            header_line = raw_header.strip()
            header = _parse_csv_header(header_line)

            raw_column_header = handle.readline()
            lines = [header_line]
            if raw_column_header != "":
                lines.append(raw_column_header.strip())

            for _ in range(header.nrow):
                row = handle.readline()
                if row == "":
                    break
                normalized = row.strip()
                if normalized:
                    lines.append(normalized)

            extra_row = handle.readline()
            if extra_row != "":
                normalized = extra_row.strip()
                if normalized:
                    lines.append(normalized)

            return lines
    except FileNotFoundError as error:
        raise StationForcingFileNotFoundError(
            station_id=station_id,
            expected_path=expected_path,
            basin_version_id=basin_version_id,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        ) from error
    except (OSError, PermissionError) as error:
        raise StationForcingFileMalformedError(
            station_id=station_id,
            expected_path=expected_path,
            parse_reason=str(error),
        ) from error
    except ValueError as error:
        raise StationForcingFileMalformedError(
            station_id=station_id,
            expected_path=expected_path,
            parse_reason=str(error),
        ) from error


def _ensure_path_under_object_store_root(
    *,
    object_store_root: Path,
    expected_path: Path,
    station_id: str,
    basin_version_id: str,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> None:
    root = Path(object_store_root).resolve()
    resolved_path = expected_path.resolve(strict=False)
    try:
        resolved_path.relative_to(root)
    except ValueError as error:
        raise StationForcingFileNotFoundError(
            station_id=station_id,
            expected_path=resolved_path,
            basin_version_id=basin_version_id,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        ) from error


def _parse_station_csv(
    lines: Sequence[str],
    *,
    station_id: str,
    expected_path: Path,
    cycle_time: datetime,
) -> list[tuple[str, datetime, float]]:
    try:
        if not lines:
            raise ValueError("file is empty")
        header = _parse_csv_header(lines[0])
        data_lines = list(lines[2:])
        if len(lines) < 2:
            raise ValueError("column header row is missing")
        if len(data_lines) != header.nrow:
            raise ValueError(f"declared nrow {header.nrow} does not match data row count {len(data_lines)}")
        return list(_parse_csv_data(lines[1:], cycle_time))
    except ValueError as error:
        raise StationForcingFileMalformedError(
            station_id=station_id,
            expected_path=expected_path,
            parse_reason=str(error),
        ) from error


def _station_series_response(
    *,
    station: StationMetadata,
    model_id: str,
    source_id: str,
    cycle_time: datetime,
    requested_variables: Sequence[str],
    requested_from: datetime | None,
    requested_to: datetime | None,
    limit: int,
    rows: Sequence[tuple[str, datetime, float]],
    unbounded_rows: Sequence[tuple[str, datetime, float]],
    window_rows: Sequence[tuple[str, datetime, float]],
) -> dict[str, Any]:
    source_display = _display_source_id(source_id)
    cycle_time_formatted = _format_time(cycle_time)
    available_by_variable = _group_rows(unbounded_rows)
    returned_by_variable = _group_rows(rows)
    global_truncated = len(unbounded_rows) > len(rows)
    last_returned_variable = rows[-1][0] if global_truncated and rows else None
    series_items: list[dict[str, Any]] = []

    for variable in FORCING_VARIABLES:
        if variable not in requested_variables:
            continue
        returned_points = returned_by_variable.get(variable, [])
        if not returned_points:
            continue
        available_points = available_by_variable.get(variable, [])
        is_truncated = len(returned_points) < len(available_points) or variable == last_returned_variable
        point_payload = [
            {
                "valid_time": _format_time(valid_time),
                "value": float(value),
                "quality_flag": "ok",
                "source_id": source_display,
            }
            for valid_time, value in returned_points
        ]
        returned_from = point_payload[0]["valid_time"] if point_payload else None
        returned_to = point_payload[-1]["valid_time"] if point_payload else None
        unit = dict(CSV_VARIABLE_COLUMNS.values())[variable]
        series_items.append(
            {
                "variable": variable,
                "unit": unit,
                "native_resolution": NATIVE_RESOLUTION,
                "source_id": source_display,
                "cycle_time": cycle_time_formatted,
                "points": point_payload,
                "truncated": is_truncated,
                "metadata": {
                    "limit": limit,
                    "returned_points": len(point_payload),
                    "requested_from": _format_time(requested_from),
                    "requested_to": _format_time(requested_to),
                    "returned_from": returned_from,
                    "returned_to": returned_to,
                    "truncated": is_truncated,
                },
            }
        )

    valid_times = [valid_time for _variable, valid_time, _value in window_rows]
    return {
        "station_id": station.station_id,
        "station": station.response(),
        "forcing_version_id": f"forc_{source_id}_{_compute_cycle_compact(cycle_time)}_{model_id}",
        "model_id": model_id,
        "source_id": source_display,
        "cycle_time": cycle_time_formatted,
        "valid_time_start": _format_time(min(valid_times)) if valid_times else None,
        "valid_time_end": _format_time(max(valid_times)) if valid_times else None,
        "limit": limit,
        "requested_from": _format_time(requested_from),
        "requested_to": _format_time(requested_to),
        "series": series_items,
    }


def _group_rows(rows: Sequence[tuple[str, datetime, float]]) -> dict[str, list[tuple[datetime, float]]]:
    grouped: dict[str, list[tuple[datetime, float]]] = {}
    for variable, valid_time, value in rows:
        grouped.setdefault(variable, []).append((valid_time, value))
    for values in grouped.values():
        values.sort(key=lambda item: item[0])
    return grouped


def _station_metadata_from_row(row: Mapping[str, Any]) -> StationMetadata:
    properties = row.get("properties_json") or {}
    if not isinstance(properties, Mapping):
        properties = {}
    return StationMetadata(
        station_id=str(row["station_id"]),
        basin_version_id=str(row["basin_version_id"]),
        station_name=_optional_str(row.get("station_name")),
        longitude=_optional_float(row.get("longitude", row.get("lon"))),
        latitude=_optional_float(row.get("latitude", row.get("lat"))),
        elevation_m=_optional_float(row.get("elevation_m")),
        station_role=_optional_str(row.get("station_role")),
        active_flag=_optional_bool(row.get("active_flag")),
        properties_json=properties,
    )


def _station_variable_tokens(values: Sequence[str] | str | None) -> list[str]:
    if not values:
        return list(FORCING_VARIABLES)
    raw_values: Sequence[str]
    if isinstance(values, str):
        raw_values = [values]
    else:
        raw_values = values
    aliases = {variable.lower(): variable for variable in FORCING_VARIABLES}
    tokens: list[str] = []
    for value in raw_values:
        for token in str(value).split(","):
            canonical = aliases.get(token.strip().lower())
            if canonical is not None and canonical not in tokens:
                tokens.append(canonical)
    return tokens


def _station_series_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_STATION_SERIES_LIMIT
    try:
        limit = int(value)
    except (TypeError, ValueError) as error:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="limit must be an integer.",
            details={"field": "limit", "rejected_value": value},
        ) from error
    if limit < 1 or limit > MAX_STATION_SERIES_LIMIT:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"limit must be between 1 and {MAX_STATION_SERIES_LIMIT}.",
            details={"field": "limit", "rejected_value": value, "max": MAX_STATION_SERIES_LIMIT},
        )
    return limit


def _required_text(value: str | None, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"{field} is required.",
            details={"field": field},
        )
    return normalized


def _optional_datetime_filter(value: datetime | str | None, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    return _required_datetime_filter(value, field)


def _required_datetime_filter(value: datetime | str, field: str) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    try:
        return _ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError as error:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"{field} must be an ISO 8601 timestamp.",
            details={"field": field, "rejected_value": value},
        ) from error


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _display_source_id(source_id: str) -> str:
    normalized = source_id.strip()
    return normalized.upper() if normalized else "unknown"


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _optional_bool(value: Any) -> bool | None:
    return bool(value) if value is not None else None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None
