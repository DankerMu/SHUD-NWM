"""Record the shipping M11 explicit-cycle river curve SQL and API target."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from packages.common.forecast_store import (
    ForecastStoreError,
    PsycopgForecastStore,
    _timeseries_segment_id,
)
from packages.common.node27_issue1895_http import bound_response_json
from packages.common.node27_issue1895_types import Issue1895ReadinessError

EXPLAIN_PREFIX = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
FORECAST_SERIES_PATH_TEMPLATE = (
    "/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series"
)
FORECAST_VARIABLE = "q_down"
INCLUDE_ANALYSIS = False
SCENARIO_BY_SOURCE: dict[str, str] = {
    "GFS": "forecast_gfs_deterministic",
    "IFS": "forecast_ifs_deterministic",
}
WINDOW_DAYS = 7
SERIES_INSPECTION_LIMIT = 8
POINT_INSPECTION_LIMIT = 480
UNIT_LIMIT = 32


class _RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, statement: str, parameters: Sequence[Any] | Mapping[str, Any] | None = None) -> None:
        if isinstance(parameters, Mapping):
            bound: tuple[Any, ...] = tuple(parameters.items())
        elif parameters is None:
            bound = ()
        else:
            bound = tuple(parameters)
        self.calls.append((str(statement), bound))

    def fetchall(self) -> list[dict[str, Any]]:
        return []

    def fetchone(self) -> None:
        return None


class _CaptureForecastStore(PsycopgForecastStore):
    """Recording adapter that exercises the public forecast-series owner."""

    def __init__(self, cursor: _RecordingCursor) -> None:
        super().__init__("recording-only")
        object.__setattr__(self, "_capture_cursor", cursor)

    @contextmanager
    def _transaction(self):  # type: ignore[no-untyped-def]
        yield self._capture_cursor

    def _validate_series_target(self, *args: Any, **kwargs: Any) -> None:
        return None


def scenario_for_source(source: str) -> str:
    token = str(source or "").strip().upper()
    scenario = SCENARIO_BY_SOURCE.get(token)
    if scenario is None:
        raise Issue1895ReadinessError(
            "source is not a shipping GFS/IFS lane",
            code="QUERY_SOURCE_INVALID",
            stage="query",
        )
    return scenario


def parse_issue_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise Issue1895ReadinessError(
                "issue_time is not a timezone-aware instant",
                code="QUERY_ISSUE_TIME_INVALID",
                stage="query",
            ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Issue1895ReadinessError(
            "issue_time must be timezone-aware",
            code="QUERY_ISSUE_TIME_INVALID",
            stage="query",
        )
    return parsed.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    return parse_issue_time(value).isoformat().replace("+00:00", "Z")


def timeseries_segment_id(segment_id: str) -> str:
    return _timeseries_segment_id(str(segment_id))


def forecast_series_path(*, basin_version_id: str, segment_id: str) -> str:
    return FORECAST_SERIES_PATH_TEMPLATE.format(
        basin_version_id=quote(str(basin_version_id), safe=""),
        segment_id=quote(str(segment_id), safe=""),
    )


def forecast_series_query(
    *,
    river_network_version_id: str,
    run_id: str,
    model_id: str,
    issue_time: str,
    source: str,
) -> str:
    return urlencode(
        {
            "river_network_version_id": river_network_version_id,
            "run_id": run_id,
            "model_id": model_id,
            "issue_time": issue_time,
            "variables": FORECAST_VARIABLE,
            "scenarios": scenario_for_source(source),
            "include_analysis": "false",
        }
    )


def forecast_series_url(
    *,
    origin: str,
    basin_version_id: str,
    segment_id: str,
    river_network_version_id: str,
    run_id: str,
    model_id: str,
    issue_time: str,
    source: str,
) -> str:
    path = forecast_series_path(basin_version_id=basin_version_id, segment_id=segment_id)
    query = forecast_series_query(
        river_network_version_id=river_network_version_id,
        run_id=run_id,
        model_id=model_id,
        issue_time=issue_time,
        source=source,
    )
    return f"{origin}{path}?{query}"


def _canonical_param(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, (bytes, bytearray)):
        raise Issue1895ReadinessError(
            "curve SQL binding contains bytes",
            code="QUERY_BINDING_INVALID",
            stage="query",
        )
    if isinstance(value, Mapping):
        return {str(key): _canonical_param(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_param(item) for item in value]
    return value


def query_digest(*, sql: str, parameters: Sequence[Any]) -> str:
    payload = {"sql": " ".join(sql.split()), "parameters": [_canonical_param(item) for item in parameters]}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_explicit_cycle_curve(
    *,
    basin_version_id: str,
    segment_id: str,
    river_network_version_id: str,
    issue_time: str | datetime,
    run_id: str,
    model_id: str,
    source: str,
) -> dict[str, Any]:
    """Record the unique M11 explicit-cycle ``hydro.river_timeseries`` statement."""

    parsed_issue = parse_issue_time(issue_time)
    issue_iso = iso_utc(parsed_issue)
    run_token = str(run_id or "").strip()
    model_token = str(model_id or "").strip()
    if not run_token or not model_token:
        raise Issue1895ReadinessError(
            "explicit-cycle recording requires run_id and model_id",
            code="QUERY_IDENTITY_MISSING",
            stage="query",
        )
    scenario = scenario_for_source(source)
    ts_segment = timeseries_segment_id(segment_id)
    cursor = _RecordingCursor()
    try:
        _CaptureForecastStore(cursor).forecast_series(
            basin_version_id=basin_version_id,
            segment_id=segment_id,
            river_network_version_id=river_network_version_id,
            issue_time=issue_iso,
            variables=[FORECAST_VARIABLE],
            scenarios=[scenario],
            include_analysis=INCLUDE_ANALYSIS,
            run_types=["forecast"],
            run_id=run_token,
            model_id=model_token,
        )
    except ForecastStoreError as error:
        if error.code != "RUN_NOT_PUBLISHED":
            raise Issue1895ReadinessError(
                "shipping curve owner refused recording",
                code="QUERY_RECORD_FAILED",
                stage="query",
            ) from None
    primary = [
        call
        for call in cursor.calls
        if "FROM hydro.river_timeseries rt" in call[0] and "h.run_type = 'forecast'" in call[0]
    ]
    if len(primary) != 1:
        raise Issue1895ReadinessError(
            "shipping curve path did not yield exactly one primary SQL call",
            code="QUERY_PRIMARY_INVALID",
            stage="query",
        )
    query_text, parameters = primary[0]
    if "selected_cycles" in query_text or "WITH selected_cycles" in query_text:
        raise Issue1895ReadinessError(
            "recorded SQL is the selected-cycles CTE branch, not explicit-cycle",
            code="QUERY_BRANCH_INVALID",
            stage="query",
        )
    if "h.cycle_time = %s" not in query_text:
        raise Issue1895ReadinessError(
            "recorded SQL is missing the explicit cycle equality",
            code="QUERY_BRANCH_INVALID",
            stage="query",
        )
    if query_text.count("%s") != len(parameters):
        raise Issue1895ReadinessError(
            "recorded SQL positional binding shape drifted",
            code="QUERY_BINDING_SHAPE",
            stage="query",
        )
    if run_token not in parameters or model_token not in parameters:
        raise Issue1895ReadinessError(
            "recorded SQL did not bind run_id and model_id",
            code="QUERY_IDENTITY_UNBOUND",
            stage="query",
        )
    if ts_segment not in parameters:
        raise Issue1895ReadinessError(
            "recorded SQL did not bind the timeseries segment id",
            code="QUERY_SEGMENT_UNBOUND",
            stage="query",
        )
    window_end = parsed_issue + timedelta(days=WINDOW_DAYS)
    return {
        "sql": query_text,
        "explain_sql": f"{EXPLAIN_PREFIX}{query_text}",
        "parameters": parameters,
        "query_digest": query_digest(sql=query_text, parameters=parameters),
        "timeseries_segment_id": ts_segment,
        "segment_id": str(segment_id),
        "basin_version_id": str(basin_version_id),
        "river_network_version_id": str(river_network_version_id),
        "run_id": run_token,
        "model_id": model_token,
        "source": str(source).strip().upper(),
        "scenario": scenario,
        "issue_time": issue_iso,
        "window_start": issue_iso,
        "window_end": iso_utc(window_end),
        "api_path": forecast_series_path(basin_version_id=basin_version_id, segment_id=segment_id),
        "api_query": forecast_series_query(
            river_network_version_id=river_network_version_id,
            run_id=run_token,
            model_id=model_token,
            issue_time=issue_iso,
            source=source,
        ),
    }


def _instant_ms(value: object) -> int:
    if isinstance(value, bool):
        raise Issue1895ReadinessError(
            "series point time is not a calendar instant",
            code="API_POINT_TIME_INVALID",
            stage="performance",
        )
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise Issue1895ReadinessError(
                "series point time is not finite",
                code="API_POINT_TIME_INVALID",
                stage="performance",
            )
        try:
            datetime.fromtimestamp(number / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            raise Issue1895ReadinessError(
                "series point time is not calendar-valid",
                code="API_POINT_TIME_INVALID",
                stage="performance",
            ) from None
        return int(number)
    return int(parse_issue_time(str(value)).timestamp() * 1000)


def _point_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Issue1895ReadinessError(
            "series point value is not a finite number",
            code="API_POINT_VALUE_INVALID",
            stage="performance",
        )
    number = float(value)
    if not math.isfinite(number):
        raise Issue1895ReadinessError(
            "series point value is not a finite number",
            code="API_POINT_VALUE_INVALID",
            stage="performance",
        )
    return number


def _parse_series_point(point: object) -> tuple[int, float]:
    if isinstance(point, Sequence) and not isinstance(point, (str, bytes)):
        if len(point) < 2:
            raise Issue1895ReadinessError(
                "series point is not a [time,value] pair",
                code="API_POINT_INVALID",
                stage="performance",
            )
        return _instant_ms(point[0]), _point_value(point[1])
    if isinstance(point, Mapping):
        return _instant_ms(point.get("valid_time", point.get("time"))), _point_value(point.get("value"))
    raise Issue1895ReadinessError(
        "series point is not a pair or object",
        code="API_POINT_INVALID",
        stage="performance",
    )


def _canonical_source(value: object) -> str:
    text = str(value or "").strip().upper()
    if text not in SCENARIO_BY_SOURCE:
        raise Issue1895ReadinessError(
            "series source_id is not canonical GFS/IFS",
            code="API_SOURCE_INVALID",
            stage="performance",
        )
    return text


def validate_river_series_response(
    payload: Any,
    *,
    segment_id: str,
    issue_time: str,
    scenario: str,
    source: str,
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    """Require a raw RiverSeriesResponse matching the requested lane identity."""

    bound_response_json(payload, code="API_JSON_TOO_COMPLEX", stage="performance")
    if not isinstance(payload, Mapping):
        raise Issue1895ReadinessError(
            "API body is not a RiverSeriesResponse object",
            code="API_BODY_INVALID",
            stage="performance",
        )
    if "data" in payload or payload.get("status") in {"ok", "ready"}:
        raise Issue1895ReadinessError(
            "API body is an envelope, not a raw RiverSeriesResponse",
            code="API_ENVELOPE_INVALID",
            stage="performance",
        )
    extra = set(payload) - {"segment_id", "river_segment_id", "issue_time", "unit", "variable", "series"}
    if extra:
        raise Issue1895ReadinessError(
            "RiverSeriesResponse has extra keys",
            code="API_BODY_KEYS_INVALID",
            stage="performance",
        )
    response_segment = payload.get("segment_id") or payload.get("river_segment_id")
    if response_segment != segment_id:
        raise Issue1895ReadinessError(
            "API segment_id does not match the frozen pin",
            code="API_SEGMENT_MISMATCH",
            stage="performance",
        )
    variable = payload.get("variable")
    if variable not in {None, FORECAST_VARIABLE}:
        raise Issue1895ReadinessError(
            "RiverSeriesResponse variable is not q_down",
            code="API_VARIABLE_INVALID",
            stage="performance",
        )
    unit = str(payload.get("unit") or "").strip()
    if not unit or len(unit) > UNIT_LIMIT:
        raise Issue1895ReadinessError(
            "RiverSeriesResponse unit is missing or oversized",
            code="API_UNIT_MISSING",
            stage="performance",
        )
    if payload.get("issue_time") in {None, ""}:
        raise Issue1895ReadinessError(
            "RiverSeriesResponse issue_time is missing",
            code="API_ISSUE_TIME_MISSING",
            stage="performance",
        )
    response_issue = iso_utc(parse_issue_time(str(payload.get("issue_time"))))
    expected_issue = iso_utc(parse_issue_time(issue_time))
    series = payload.get("series")
    if not isinstance(series, list) or not series:
        raise Issue1895ReadinessError(
            "RiverSeriesResponse series is empty",
            code="API_SERIES_EMPTY",
            stage="performance",
        )
    if len(series) > SERIES_INSPECTION_LIMIT:
        raise Issue1895ReadinessError(
            "RiverSeriesResponse series count exceeds the inspection bound",
            code="API_SERIES_BOUND",
            stage="performance",
        )
    expected_source = _canonical_source(source)
    window_start_ms = int(parse_issue_time(window_start).timestamp() * 1000)
    window_end_ms = int(parse_issue_time(window_end).timestamp() * 1000)
    matched: list[Mapping[str, Any]] = []
    total_points = 0
    for item in series:
        if not isinstance(item, Mapping):
            raise Issue1895ReadinessError(
                "RiverSeriesResponse series item is invalid",
                code="API_SERIES_INVALID",
                stage="performance",
            )
        scenario_id = item.get("scenario_id") or item.get("scenario")
        if scenario_id != scenario:
            continue
        matched.append(item)
        raw_points = item.get("points")
        if not isinstance(raw_points, list):
            raw_points = item.get("data")
        if not isinstance(raw_points, list) or not raw_points:
            raise Issue1895ReadinessError(
                "RiverSeriesResponse series has no points",
                code="API_SERIES_EMPTY",
                stage="performance",
            )
        if len(raw_points) > POINT_INSPECTION_LIMIT:
            raise Issue1895ReadinessError(
                "RiverSeriesResponse point count exceeds the inspection bound",
                code="API_POINTS_BOUND",
                stage="performance",
            )
        total_points += len(raw_points)
        if total_points > POINT_INSPECTION_LIMIT:
            raise Issue1895ReadinessError(
                "RiverSeriesResponse point count exceeds the inspection bound",
                code="API_POINTS_BOUND",
                stage="performance",
            )
        item_source = item.get("source_id") or item.get("source")
        if item_source in {None, ""}:
            raise Issue1895ReadinessError(
                "matching series is missing source_id",
                code="API_SOURCE_MISSING",
                stage="performance",
            )
        if _canonical_source(item_source) != expected_source:
            raise Issue1895ReadinessError(
                "matching series source_id does not equal the requested source",
                code="API_SOURCE_MISMATCH",
                stage="performance",
            )
        cycle_raw = item.get("cycle_time")
        cycle_iso = iso_utc(parse_issue_time(str(cycle_raw))) if cycle_raw not in {None, ""} else None
        if cycle_iso is None and response_issue != expected_issue:
            raise Issue1895ReadinessError(
                "matching series is missing cycle_time and issue_time does not bind the lane",
                code="API_CYCLE_MISSING",
                stage="performance",
            )
        if cycle_iso is not None and cycle_iso != expected_issue:
            raise Issue1895ReadinessError(
                "matching series cycle_time does not equal the requested cycle",
                code="API_CYCLE_MISMATCH",
                stage="performance",
            )
        for point in raw_points:
            instant_ms, _value = _parse_series_point(point)
            if instant_ms < window_start_ms or instant_ms > window_end_ms:
                raise Issue1895ReadinessError(
                    "series point is outside the frozen seven-day window",
                    code="API_POINT_OUT_OF_WINDOW",
                    stage="performance",
                )
    if not matched:
        raise Issue1895ReadinessError(
            "RiverSeriesResponse is missing the requested scenario",
            code="API_SCENARIO_MISSING",
            stage="performance",
        )
    if len(matched) != 1:
        raise Issue1895ReadinessError(
            "RiverSeriesResponse has duplicate matching series",
            code="API_SERIES_AMBIGUOUS",
            stage="performance",
        )
    if response_issue != expected_issue:
        cycle_raw = matched[0].get("cycle_time")
        if cycle_raw in {None, ""} or iso_utc(parse_issue_time(str(cycle_raw))) != expected_issue:
            raise Issue1895ReadinessError(
                "RiverSeriesResponse issue_time does not match the frozen pin",
                code="API_ISSUE_TIME_MISMATCH",
                stage="performance",
            )
    return {
        "segment_id": segment_id,
        "scenario": scenario,
        "source": expected_source,
        "point_count": total_points,
    }
