"""Record the shipping M11 explicit-cycle river curve SQL and API target."""

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


NativeParameters = Mapping[str, Any] | Sequence[Any]
CapturedParameters = dict[str, Any] | tuple[Any, ...]


@dataclass(frozen=True)
class _CanonicalExplicitCycleIdentity:
    issue_time: datetime
    run_id: str
    model_id: str
    timeseries_segment_id: str


class _RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, CapturedParameters]] = []

    def execute(self, statement: str, parameters: NativeParameters | None = None) -> None:
        if parameters is None:
            bound: CapturedParameters = ()
        else:
            bound = _copy_parameters(parameters, error_code="QUERY_BINDING_SHAPE")
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


_PYFORMAT_REFERENCE_RE = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")
_UNSUPPORTED_PERCENT_RE = re.compile(r"%(?!s|\([A-Za-z_][A-Za-z0-9_]*\)s)")
_REQUIRED_NAMED_BINDINGS: tuple[tuple[str, str, str], ...] = (
    ("h.cycle_time", "issue_time", "QUERY_IDENTITY_UNBOUND"),
    ("h.run_id", "run_id", "QUERY_IDENTITY_UNBOUND"),
    ("h.model_id", "model_id", "QUERY_IDENTITY_UNBOUND"),
)
_SEGMENT_PREDICATE_RE = re.compile(
    r"\brt\.(?:river_segment_id|river_segment_key)\s*=\s*(?P<placeholder>%s|%\([A-Za-z_][A-Za-z0-9_]*\)s)",
    re.IGNORECASE,
)


def _binding_error(message: str, *, code: str) -> Issue1895ReadinessError:
    return Issue1895ReadinessError(message, code=code, stage="query")


def _copy_binding_value(value: Any, *, error_code: str) -> Any:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _binding_error("curve SQL binding number is not finite", code=error_code)
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise _binding_error("curve SQL binding contains bytes", code=error_code)
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _binding_error("curve SQL binding mapping key is not text", code=error_code)
            copied[key] = _copy_binding_value(item, error_code=error_code)
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_copy_binding_value(item, error_code=error_code) for item in value]
    raise _binding_error("curve SQL binding value is unsupported", code=error_code)


def _copy_parameters(parameters: object, *, error_code: str) -> CapturedParameters:
    if isinstance(parameters, Mapping):
        copied: dict[str, Any] = {}
        for key, value in parameters.items():
            if not isinstance(key, str):
                raise _binding_error("curve SQL binding mapping key is not text", code=error_code)
            copied[key] = _copy_binding_value(value, error_code=error_code)
        return copied
    if isinstance(parameters, Sequence) and not isinstance(parameters, (str, bytes, bytearray, memoryview)):
        return tuple(_copy_binding_value(value, error_code=error_code) for value in parameters)
    raise _binding_error("curve SQL binding container is not a mapping or sequence", code=error_code)


def _canonical_param(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"datetime": iso_utc(value)}
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _binding_error("curve SQL binding number is not finite", code="QUERY_BINDING_INVALID")
        return value
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise _binding_error("curve SQL binding mapping key is not text", code="QUERY_BINDING_INVALID")
            canonical[key] = _canonical_param(value[key])
        return {"mapping": canonical}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return {"sequence": [_canonical_param(item) for item in value]}
    raise _binding_error("curve SQL binding value is unsupported", code="QUERY_BINDING_INVALID")


def query_digest(*, sql: str, parameters: NativeParameters) -> str:
    copied = _copy_parameters(parameters, error_code="QUERY_BINDING_INVALID")
    if isinstance(copied, Mapping):
        canonical_parameters = {"mapping": {key: _canonical_param(copied[key]) for key in sorted(copied)}}
    else:
        canonical_parameters = {"sequence": [_canonical_param(item) for item in copied]}
    payload = {"sql": " ".join(sql.split()), "parameters": canonical_parameters}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _canonical_explicit_cycle_identity(
    expected: Mapping[str, Any] | _CanonicalExplicitCycleIdentity,
) -> _CanonicalExplicitCycleIdentity:
    if isinstance(expected, _CanonicalExplicitCycleIdentity):
        return expected
    try:
        issue_time = parse_issue_time(expected["issue_time"])
        run_id = str(expected["run_id"] or "").strip()
        model_id = str(expected["model_id"] or "").strip()
        timeseries_segment_id = str(expected["timeseries_segment_id"] or "").strip()
    except (KeyError, TypeError, ValueError):
        raise _binding_error("canonical explicit-cycle identity is malformed", code="QUERY_IDENTITY_MISSING") from None
    if not run_id or not model_id or not timeseries_segment_id:
        raise _binding_error("canonical explicit-cycle identity is incomplete", code="QUERY_IDENTITY_MISSING")
    return _CanonicalExplicitCycleIdentity(
        issue_time=issue_time,
        run_id=run_id,
        model_id=model_id,
        timeseries_segment_id=timeseries_segment_id,
    )


def _placeholder_style(sql: str) -> tuple[str, tuple[str, ...]]:
    if "selected_cycles" in sql.lower() or "with selected_cycles" in sql.lower():
        raise _binding_error(
            "recorded SQL is the selected-cycles CTE branch, not explicit-cycle", code="QUERY_BRANCH_INVALID"
        )
    if _UNSUPPORTED_PERCENT_RE.search(sql) is not None:
        raise _binding_error("recorded SQL contains unsupported placeholder syntax", code="QUERY_BINDING_SHAPE")
    named = tuple(match.group(1) for match in _PYFORMAT_REFERENCE_RE.finditer(sql))
    positional = sql.count("%s")
    if named and positional:
        raise _binding_error("recorded SQL mixes named and positional placeholders", code="QUERY_BINDING_SHAPE")
    if named:
        return "named", named
    if positional:
        return "positional", ()
    raise _binding_error("recorded SQL has no supported placeholders", code="QUERY_BINDING_SHAPE")


def _named_predicate_key(sql: str, predicate: str, *, identity_error: str) -> str:
    pattern = re.compile(
        rf"\b{re.escape(predicate)}\s*=\s*%\((?P<key>[A-Za-z_][A-Za-z0-9_]*)\)s",
        re.IGNORECASE,
    )
    matches = tuple(match.group("key") for match in pattern.finditer(sql))
    if len(matches) != 1:
        raise _binding_error("recorded SQL does not bind one required identity predicate", code=identity_error)
    return matches[0]


def _positional_predicate_ordinal(sql: str, predicate: str, *, identity_error: str) -> int:
    pattern = re.compile(rf"\b{re.escape(predicate)}\s*=\s*%s", re.IGNORECASE)
    matches = tuple(pattern.finditer(sql))
    if len(matches) != 1:
        raise _binding_error("recorded SQL does not bind one required identity predicate", code=identity_error)
    return sql[: matches[0].start()].count("%s")


def _segment_binding(sql: str, *, style: str) -> str | int:
    matches = tuple(_SEGMENT_PREDICATE_RE.finditer(sql))
    if len(matches) != 1:
        raise _binding_error(
            "recorded SQL does not bind one timeseries segment predicate", code="QUERY_SEGMENT_UNBOUND"
        )
    placeholder = matches[0].group("placeholder")
    if style == "named":
        named = _PYFORMAT_REFERENCE_RE.fullmatch(placeholder)
        if named is None:
            raise _binding_error("recorded SQL mixes placeholder styles", code="QUERY_BINDING_SHAPE")
        return named.group(1)
    if placeholder != "%s":
        raise _binding_error("recorded SQL mixes placeholder styles", code="QUERY_BINDING_SHAPE")
    return sql[: matches[0].start()].count("%s")


def _value_matches_expected(value: Any, expected: Any, *, issue_time: bool = False) -> bool:
    if issue_time:
        try:
            return parse_issue_time(value) == expected
        except Issue1895ReadinessError:
            return False
    return value == expected


def _validate_named_binding(
    sql: str,
    parameters: Mapping[str, Any],
    *,
    predicate: str,
    canonical_key: str,
    expected_value: Any,
    error_code: str,
    issue_time: bool = False,
) -> None:
    key = _named_predicate_key(sql, predicate, identity_error=error_code)
    if key != canonical_key:
        raise _binding_error("recorded SQL predicate references an unexpected binding key", code=error_code)
    if not _value_matches_expected(parameters.get(key), expected_value, issue_time=issue_time):
        raise _binding_error("recorded SQL binding does not match canonical identity", code=error_code)


def _validate_positional_binding(
    sql: str,
    parameters: Sequence[Any],
    *,
    predicate: str,
    expected_value: Any,
    error_code: str,
    issue_time: bool = False,
) -> None:
    ordinal = _positional_predicate_ordinal(sql, predicate, identity_error=error_code)
    if ordinal >= len(parameters) or not _value_matches_expected(
        parameters[ordinal], expected_value, issue_time=issue_time
    ):
        raise _binding_error("recorded SQL binding does not match canonical identity", code=error_code)


def _validate_captured_explicit_cycle_query(
    sql: str,
    parameters: object,
    expected: Mapping[str, Any] | _CanonicalExplicitCycleIdentity,
) -> CapturedParameters:
    """Fail closed unless one captured query binds the canonical explicit-cycle identity."""

    identity = _canonical_explicit_cycle_identity(expected)
    text = str(sql)
    style, referenced_names = _placeholder_style(text)
    copied = _copy_parameters(parameters, error_code="QUERY_BINDING_SHAPE")
    if style == "named":
        if not isinstance(copied, Mapping):
            raise _binding_error("named SQL requires a mapping", code="QUERY_BINDING_SHAPE")
        if set(copied) != set(referenced_names):
            raise _binding_error(
                "named SQL mapping keys do not equal referenced placeholders", code="QUERY_BINDING_SHAPE"
            )
        expected_values = {
            "h.cycle_time": (identity.issue_time, True),
            "h.run_id": (identity.run_id, False),
            "h.model_id": (identity.model_id, False),
        }
        for predicate, canonical_key, error_code in _REQUIRED_NAMED_BINDINGS:
            expected_value, is_issue_time = expected_values[predicate]
            _validate_named_binding(
                text,
                copied,
                predicate=predicate,
                canonical_key=canonical_key,
                expected_value=expected_value,
                error_code=error_code,
                issue_time=is_issue_time,
            )
        segment_key = _segment_binding(text, style=style)
        if segment_key != "river_segment_id":
            raise _binding_error(
                "recorded SQL predicate references an unexpected binding key",
                code="QUERY_SEGMENT_UNBOUND",
            )
        if not _value_matches_expected(copied.get(segment_key), identity.timeseries_segment_id):
            raise _binding_error("recorded SQL binding does not match canonical identity", code="QUERY_SEGMENT_UNBOUND")
        return dict(copied)

    if isinstance(copied, Mapping):
        raise _binding_error("positional SQL requires a sequence", code="QUERY_BINDING_SHAPE")
    placeholder_count = text.count("%s")
    if placeholder_count != len(copied):
        raise _binding_error("recorded SQL positional binding shape drifted", code="QUERY_BINDING_SHAPE")
    expected_values = {
        "h.cycle_time": (identity.issue_time, True),
        "h.run_id": (identity.run_id, False),
        "h.model_id": (identity.model_id, False),
    }
    for predicate, _canonical_key, error_code in _REQUIRED_NAMED_BINDINGS:
        expected_value, is_issue_time = expected_values[predicate]
        _validate_positional_binding(
            text,
            copied,
            predicate=predicate,
            expected_value=expected_value,
            error_code=error_code,
            issue_time=is_issue_time,
        )
    segment_ordinal = _segment_binding(text, style=style)
    if not isinstance(segment_ordinal, int) or segment_ordinal >= len(copied):
        raise _binding_error("recorded SQL binding does not match canonical identity", code="QUERY_SEGMENT_UNBOUND")
    if not _value_matches_expected(copied[segment_ordinal], identity.timeseries_segment_id):
        raise _binding_error("recorded SQL binding does not match canonical identity", code="QUERY_SEGMENT_UNBOUND")
    return tuple(copied)


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
    query_text, captured_parameters = primary[0]
    parameters = _validate_captured_explicit_cycle_query(
        query_text,
        captured_parameters,
        _CanonicalExplicitCycleIdentity(
            issue_time=parsed_issue,
            run_id=run_token,
            model_id=model_token,
            timeseries_segment_id=ts_segment,
        ),
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
