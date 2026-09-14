"""Capture shipping explicit-cycle SQL and validate canonical identity.

The capture adapter invokes real ``PsycopgForecastStore.forecast_series`` with
q_down, include_analysis=False and run_types=['forecast']. SQL is never cloned.
Named UNION bindings, including repeated segment/network predicates, are
validated against frozen cycle/run/model/segment/window identity. Mapping
insertion order does not change the digest. Positional placeholders refuse.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from packages.common.forecast_store import (
    ForecastStoreError,
    PsycopgForecastStore,
    _timeseries_segment_id,
)
from packages.common.node27_pgdata_workload_types import PgdataWorkloadError, refuse

EXPLAIN_PREFIX = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
FORECAST_SERIES_PATH_TEMPLATE = "/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series"
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
PRIMARY_SQL_MARKERS = (
    "FROM hydro.river_timeseries",
    "h.run_type = 'forecast'",
    "h.cycle_time = %(issue_time)s",
)

NativeParameters = Mapping[str, Any]
CapturedParameters = dict[str, Any]

_PYFORMAT_REFERENCE_RE = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")
_UNSUPPORTED_PERCENT_RE = re.compile(r"%(?!s|\([A-Za-z_][A-Za-z0-9_]*\)s)")
_NAMED_EQUALS_RE = re.compile(
    r"\b(?P<predicate>(?:h|rt|rnv)\.[A-Za-z_][A-Za-z0-9_]*)\s*=\s*%\((?P<key>[A-Za-z_][A-Za-z0-9_]*)\)s",
    re.IGNORECASE,
)
_REQUIRED_EQUALS: tuple[tuple[str, str, str], ...] = (
    ("h.cycle_time", "issue_time", "QUERY_IDENTITY_UNBOUND"),
    ("h.run_id", "run_id", "QUERY_IDENTITY_UNBOUND"),
    ("h.model_id", "model_id", "QUERY_IDENTITY_UNBOUND"),
    ("rt.river_segment_id", "river_segment_id", "QUERY_SEGMENT_UNBOUND"),
    ("rt.river_network_version_id", "river_network_version_id", "QUERY_IDENTITY_UNBOUND"),
)
_REQUIRED_PRESENT_KEYS: tuple[str, ...] = (
    "basin_version_id",
    "river_segment_id",
    "river_network_version_id",
    "issue_time",
    "end_time",
    "scenario_tokens",
    "scenario_ids",
    "run_id",
    "model_id",
)


@dataclass(frozen=True)
class CanonicalExplicitCycleIdentity:
    issue_time: datetime
    run_id: str
    model_id: str
    timeseries_segment_id: str
    river_network_version_id: str
    basin_version_id: str
    source: str
    scenario: str
    window_end: datetime


class _RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, CapturedParameters]] = []

    def execute(self, statement: str, parameters: NativeParameters | Sequence[Any] | None = None) -> None:
        if parameters is None:
            bound: CapturedParameters = {}
        else:
            bound = _copy_named_parameters(parameters, error_code="QUERY_BINDING_SHAPE")
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
        refuse("source is not a shipping GFS/IFS lane", code="QUERY_SOURCE_INVALID", stage="query")
    return scenario


def parse_issue_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            refuse("issue_time is not a timezone-aware instant", code="QUERY_ISSUE_TIME_INVALID", stage="query")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        refuse("issue_time must be timezone-aware", code="QUERY_ISSUE_TIME_INVALID", stage="query")
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
            "run_types": "forecast",
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


def _copy_binding_value(value: Any, *, error_code: str) -> Any:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            refuse("curve SQL binding number is not finite", code=error_code, stage="query")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        refuse("curve SQL binding contains bytes", code=error_code, stage="query")
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                refuse("curve SQL binding mapping key is not text", code=error_code, stage="query")
            copied[key] = _copy_binding_value(item, error_code=error_code)
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_copy_binding_value(item, error_code=error_code) for item in value]
    refuse("curve SQL binding value is unsupported", code=error_code, stage="query")
    raise AssertionError("unreachable")


def _copy_named_parameters(parameters: object, *, error_code: str) -> CapturedParameters:
    if isinstance(parameters, Mapping):
        copied: dict[str, Any] = {}
        for key, value in parameters.items():
            if not isinstance(key, str):
                refuse("curve SQL binding mapping key is not text", code=error_code, stage="query")
            copied[key] = _copy_binding_value(value, error_code=error_code)
        return copied
    refuse("named SQL requires a mapping", code=error_code, stage="query")
    raise AssertionError("unreachable")


def _canonical_param(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"datetime": iso_utc(value)}
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            refuse("curve SQL binding number is not finite", code="QUERY_BINDING_INVALID", stage="query")
        return value
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                refuse("curve SQL binding mapping key is not text", code="QUERY_BINDING_INVALID", stage="query")
            canonical[key] = _canonical_param(value[key])
        return {"mapping": canonical}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return {"sequence": [_canonical_param(item) for item in value]}
    refuse("curve SQL binding value is unsupported", code="QUERY_BINDING_INVALID", stage="query")
    raise AssertionError("unreachable")


def query_digest(*, sql: str, parameters: NativeParameters) -> str:
    copied = _copy_named_parameters(parameters, error_code="QUERY_BINDING_INVALID")
    payload = {
        "sql": " ".join(sql.split()),
        "parameters": {"mapping": {key: _canonical_param(copied[key]) for key in sorted(copied)}},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _canonical_identity(expected: Mapping[str, Any] | CanonicalExplicitCycleIdentity) -> CanonicalExplicitCycleIdentity:
    if isinstance(expected, CanonicalExplicitCycleIdentity):
        return expected
    try:
        issue_time = parse_issue_time(expected["issue_time"])
        run_id = str(expected["run_id"] or "").strip()
        model_id = str(expected["model_id"] or "").strip()
        ts_segment = str(expected["timeseries_segment_id"] or "").strip()
        river_network_version_id = str(expected["river_network_version_id"] or "").strip()
        basin_version_id = str(expected["basin_version_id"] or "").strip()
        source = str(expected["source"] or "").strip().upper()
        scenario = str(expected.get("scenario") or scenario_for_source(source))
        window_end = (
            parse_issue_time(expected["window_end"])
            if "window_end" in expected
            else issue_time + timedelta(days=WINDOW_DAYS)
        )
    except (KeyError, TypeError, ValueError, PgdataWorkloadError):
        refuse("canonical explicit-cycle identity is malformed", code="QUERY_IDENTITY_MISSING", stage="query")
    if not run_id or not model_id or not ts_segment or not river_network_version_id or not basin_version_id:
        refuse("canonical explicit-cycle identity is incomplete", code="QUERY_IDENTITY_MISSING", stage="query")
    return CanonicalExplicitCycleIdentity(
        issue_time=issue_time,
        run_id=run_id,
        model_id=model_id,
        timeseries_segment_id=ts_segment,
        river_network_version_id=river_network_version_id,
        basin_version_id=basin_version_id,
        source=source,
        scenario=scenario,
        window_end=window_end,
    )


def _named_placeholders(sql: str) -> tuple[str, ...]:
    if "selected_cycles" in sql.lower() or "with selected_cycles" in sql.lower():
        refuse(
            "recorded SQL is the selected-cycles CTE branch, not explicit-cycle",
            code="QUERY_BRANCH_INVALID",
            stage="query",
        )
    if _UNSUPPORTED_PERCENT_RE.search(sql) is not None or "%s" in sql:
        refuse("recorded SQL mixes named and positional placeholders", code="QUERY_BINDING_SHAPE", stage="query")
    named = tuple(match.group(1) for match in _PYFORMAT_REFERENCE_RE.finditer(sql))
    if not named:
        refuse("recorded SQL has no supported placeholders", code="QUERY_BINDING_SHAPE", stage="query")
    return named


def _value_matches_expected(value: Any, expected: Any, *, issue_time: bool = False) -> bool:
    if issue_time:
        try:
            return parse_issue_time(value) == expected
        except PgdataWorkloadError:
            return False
    return value == expected


def _equals_keys(sql: str, predicate: str) -> tuple[str, ...]:
    keys: list[str] = []
    for match in _NAMED_EQUALS_RE.finditer(sql):
        if match.group("predicate").lower() == predicate.lower():
            keys.append(match.group("key"))
    return tuple(keys)


def validate_captured_explicit_cycle_query(
    sql: str,
    parameters: object,
    expected: Mapping[str, Any] | CanonicalExplicitCycleIdentity,
) -> CapturedParameters:
    """Fail closed unless captured named SQL binds the frozen explicit-cycle identity."""

    identity = _canonical_identity(expected)
    text = str(sql)
    referenced = _named_placeholders(text)
    copied = _copy_named_parameters(parameters, error_code="QUERY_BINDING_SHAPE")
    if set(copied) != set(referenced):
        refuse("named SQL mapping keys do not equal referenced placeholders", code="QUERY_BINDING_SHAPE", stage="query")
    missing = [key for key in _REQUIRED_PRESENT_KEYS if key not in copied]
    if missing:
        refuse("named SQL mapping is missing a required binding", code="QUERY_BINDING_SHAPE", stage="query")

    expected_equals = {
        "h.cycle_time": (identity.issue_time, True),
        "h.run_id": (identity.run_id, False),
        "h.model_id": (identity.model_id, False),
        "rt.river_segment_id": (identity.timeseries_segment_id, False),
        "rt.river_network_version_id": (identity.river_network_version_id, False),
    }
    for predicate, canonical_key, error_code in _REQUIRED_EQUALS:
        keys = _equals_keys(text, predicate)
        if not keys or any(key != canonical_key for key in keys):
            refuse("recorded SQL predicate references an unexpected binding key", code=error_code, stage="query")
        expected_value, is_issue_time = expected_equals[predicate]
        if not _value_matches_expected(copied.get(canonical_key), expected_value, issue_time=is_issue_time):
            refuse("recorded SQL binding does not match canonical identity", code=error_code, stage="query")

    if not _value_matches_expected(copied.get("basin_version_id"), identity.basin_version_id):
        refuse("recorded SQL binding does not match canonical identity", code="QUERY_IDENTITY_UNBOUND", stage="query")
    if not _value_matches_expected(copied.get("end_time"), identity.window_end, issue_time=True):
        refuse("recorded SQL binding does not match canonical identity", code="QUERY_IDENTITY_UNBOUND", stage="query")

    expected_tokens = [identity.scenario.lower()]
    expected_ids = [identity.scenario.lower()]
    tokens = copied.get("scenario_tokens")
    ids = copied.get("scenario_ids")
    if tokens != expected_tokens:
        refuse("recorded SQL binding does not match canonical identity", code="QUERY_IDENTITY_UNBOUND", stage="query")
    if ids != expected_ids:
        refuse("recorded SQL binding does not match canonical identity", code="QUERY_IDENTITY_UNBOUND", stage="query")
    return dict(copied)


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
    basin_token = str(basin_version_id or "").strip()
    network_token = str(river_network_version_id or "").strip()
    if not run_token or not model_token or not basin_token or not network_token:
        refuse("explicit-cycle recording requires complete identity", code="QUERY_IDENTITY_MISSING", stage="query")
    scenario = scenario_for_source(source)
    ts_segment = timeseries_segment_id(segment_id)
    window_end = parsed_issue + timedelta(days=WINDOW_DAYS)
    identity = CanonicalExplicitCycleIdentity(
        issue_time=parsed_issue,
        run_id=run_token,
        model_id=model_token,
        timeseries_segment_id=ts_segment,
        river_network_version_id=network_token,
        basin_version_id=basin_token,
        source=str(source).strip().upper(),
        scenario=scenario,
        window_end=window_end,
    )
    cursor = _RecordingCursor()
    try:
        _CaptureForecastStore(cursor).forecast_series(
            basin_version_id=basin_token,
            segment_id=segment_id,
            river_network_version_id=network_token,
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
            refuse("shipping curve owner refused recording", code="QUERY_RECORD_FAILED", stage="query")
    primary = [call for call in cursor.calls if all(marker in call[0] for marker in PRIMARY_SQL_MARKERS)]
    if len(primary) != 1:
        refuse(
            "shipping curve path did not yield exactly one primary SQL call",
            code="QUERY_PRIMARY_INVALID",
            stage="query",
        )
    query_text, captured_parameters = primary[0]
    parameters = validate_captured_explicit_cycle_query(query_text, captured_parameters, identity)
    digest = query_digest(sql=query_text, parameters=parameters)
    return {
        "sql": query_text,
        "explain_sql": f"{EXPLAIN_PREFIX}{query_text}",
        "parameters": parameters,
        "query_digest": digest,
        "timeseries_segment_id": ts_segment,
        "segment_id": str(segment_id),
        "basin_version_id": basin_token,
        "river_network_version_id": network_token,
        "run_id": run_token,
        "model_id": model_token,
        "source": identity.source,
        "scenario": scenario,
        "issue_time": issue_iso,
        "window_start": issue_iso,
        "window_end": iso_utc(window_end),
        "api_path": forecast_series_path(basin_version_id=basin_token, segment_id=segment_id),
        "api_query": forecast_series_query(
            river_network_version_id=network_token,
            run_id=run_token,
            model_id=model_token,
            issue_time=issue_iso,
            source=identity.source,
        ),
    }


def assert_query_identity(*, captured: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Re-validate frozen SQL/bindings before EXPLAIN or evidence acceptance."""

    sql = str(captured.get("sql") or "")
    parameters = captured.get("parameters")
    validate_captured_explicit_cycle_query(sql, parameters, expected)
    digest = query_digest(sql=sql, parameters=parameters)  # type: ignore[arg-type]
    frozen = str(captured.get("query_digest") or "")
    if digest != frozen:
        refuse("frozen query identity drifted", code="QUERY_IDENTITY_DRIFT", stage="query")
    if str(captured.get("explain_sql") or "") != f"{EXPLAIN_PREFIX}{sql}":
        refuse("EXPLAIN SQL drifted from the captured statement", code="QUERY_IDENTITY_DRIFT", stage="query")


def _instant_ms(value: object) -> int:
    if isinstance(value, bool):
        refuse("series point time is not a calendar instant", code="API_POINT_TIME_INVALID", stage="performance")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            refuse("series point time is not finite", code="API_POINT_TIME_INVALID", stage="performance")
        try:
            datetime.fromtimestamp(number / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            refuse("series point time is not calendar-valid", code="API_POINT_TIME_INVALID", stage="performance")
        return int(number)
    return int(parse_issue_time(str(value)).timestamp() * 1000)


def _point_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        refuse("series point value is not a finite number", code="API_POINT_VALUE_INVALID", stage="performance")
    number = float(value)
    if not math.isfinite(number):
        refuse("series point value is not a finite number", code="API_POINT_VALUE_INVALID", stage="performance")
    return number


def _parse_series_point(point: object) -> tuple[int, float]:
    if isinstance(point, Sequence) and not isinstance(point, (str, bytes)):
        if len(point) < 2:
            refuse("series point is not a [time,value] pair", code="API_POINT_INVALID", stage="performance")
        return _instant_ms(point[0]), _point_value(point[1])
    if isinstance(point, Mapping):
        return _instant_ms(point.get("valid_time", point.get("time"))), _point_value(point.get("value"))
    refuse("series point is not a pair or object", code="API_POINT_INVALID", stage="performance")
    raise AssertionError("unreachable")


def _canonical_source(value: object) -> str:
    text = str(value or "").strip().upper()
    if text not in SCENARIO_BY_SOURCE:
        refuse("series source_id is not canonical GFS/IFS", code="API_SOURCE_INVALID", stage="performance")
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
    run_id: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Require a raw RiverSeriesResponse matching the requested identity.

    Shipping forecast-series JSON does not carry run_id/model_id. Those fields
    are proven by the captured named SQL plus a bounded hydro.hydro_run lookup
    in the measurement producer, not invented as response keys.
    """

    from packages.common.node27_pgdata_workload_http import bound_response_json

    bound_response_json(payload, code="API_JSON_TOO_COMPLEX", stage="performance")
    if not isinstance(payload, Mapping):
        refuse("API body is not a RiverSeriesResponse object", code="API_BODY_INVALID", stage="performance")
    if "data" in payload or payload.get("status") in {"ok", "ready"} or "segments" in payload:
        refuse(
            "API body is an envelope, not a raw RiverSeriesResponse", code="API_ENVELOPE_INVALID", stage="performance"
        )
    extra = set(payload) - {"segment_id", "issue_time", "unit", "series"}
    if extra:
        refuse("RiverSeriesResponse has extra keys", code="API_BODY_KEYS_INVALID", stage="performance")
    if payload.get("segment_id") != segment_id:
        refuse("API segment_id does not match the frozen pin", code="API_SEGMENT_MISMATCH", stage="performance")
    unit = str(payload.get("unit") or "").strip()
    if not unit or len(unit) > UNIT_LIMIT:
        refuse("RiverSeriesResponse unit is missing or oversized", code="API_UNIT_MISSING", stage="performance")
    if payload.get("issue_time") in {None, ""}:
        refuse("RiverSeriesResponse issue_time is missing", code="API_ISSUE_TIME_MISSING", stage="performance")
    response_issue = iso_utc(parse_issue_time(str(payload.get("issue_time"))))
    expected_issue = iso_utc(parse_issue_time(issue_time))
    series = payload.get("series")
    if not isinstance(series, list) or not series:
        refuse("RiverSeriesResponse series is empty", code="API_SERIES_EMPTY", stage="performance")
    if len(series) > SERIES_INSPECTION_LIMIT:
        refuse(
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
            refuse("RiverSeriesResponse series item is invalid", code="API_SERIES_INVALID", stage="performance")
        scenario_id = item.get("scenario_id") or item.get("scenario")
        if scenario_id != scenario:
            continue
        matched.append(item)
        variable = item.get("variable")
        if variable not in {None, FORECAST_VARIABLE}:
            refuse("RiverSeriesResponse variable is not q_down", code="API_VARIABLE_INVALID", stage="performance")
        raw_points = item.get("points")
        if not isinstance(raw_points, list) or not raw_points:
            refuse("RiverSeriesResponse series has no points", code="API_SERIES_EMPTY", stage="performance")
        if len(raw_points) > POINT_INSPECTION_LIMIT:
            refuse(
                "RiverSeriesResponse point count exceeds the inspection bound",
                code="API_POINTS_BOUND",
                stage="performance",
            )
        total_points += len(raw_points)
        if total_points > POINT_INSPECTION_LIMIT:
            refuse(
                "RiverSeriesResponse point count exceeds the inspection bound",
                code="API_POINTS_BOUND",
                stage="performance",
            )
        item_source = item.get("source_id") or item.get("source")
        if item_source in {None, ""}:
            refuse("matching series is missing source_id", code="API_SOURCE_MISSING", stage="performance")
        if _canonical_source(item_source) != expected_source:
            refuse(
                "matching series source_id does not equal the requested source",
                code="API_SOURCE_MISMATCH",
                stage="performance",
            )
        cycle_raw = item.get("cycle_time")
        cycle_iso = iso_utc(parse_issue_time(str(cycle_raw))) if cycle_raw not in {None, ""} else None
        if cycle_iso is None and response_issue != expected_issue:
            refuse(
                "matching series is missing cycle_time and issue_time does not bind the lane",
                code="API_CYCLE_MISSING",
                stage="performance",
            )
        if cycle_iso is not None and cycle_iso != expected_issue:
            refuse(
                "matching series cycle_time does not equal the requested cycle",
                code="API_CYCLE_MISMATCH",
                stage="performance",
            )
        if run_id is not None and item.get("run_id") not in {None, "", run_id}:
            refuse("API run_id does not match the frozen pin", code="API_RUN_MISMATCH", stage="performance")
        if model_id is not None and item.get("model_id") not in {None, "", model_id}:
            refuse("API model_id does not match the frozen pin", code="API_MODEL_MISMATCH", stage="performance")
        for point in raw_points:
            instant_ms, _value = _parse_series_point(point)
            if instant_ms < window_start_ms or instant_ms > window_end_ms:
                refuse(
                    "series point is outside the frozen seven-day window",
                    code="API_POINT_OUT_OF_WINDOW",
                    stage="performance",
                )
    if not matched:
        refuse(
            "RiverSeriesResponse is missing the requested scenario", code="API_SCENARIO_MISSING", stage="performance"
        )
    if len(matched) != 1:
        refuse("RiverSeriesResponse has duplicate matching series", code="API_SERIES_AMBIGUOUS", stage="performance")
    if response_issue != expected_issue:
        cycle_raw = matched[0].get("cycle_time")
        if cycle_raw in {None, ""} or iso_utc(parse_issue_time(str(cycle_raw))) != expected_issue:
            refuse(
                "RiverSeriesResponse issue_time does not match the frozen pin",
                code="API_ISSUE_TIME_MISMATCH",
                stage="performance",
            )
    return {
        "segment_id": segment_id,
        "scenario": scenario,
        "source": expected_source,
        "point_count": total_points,
        "content_digest": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest(),
    }
