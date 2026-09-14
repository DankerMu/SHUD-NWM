"""1+20 measurement, nearest-rank P95, native EXPLAIN and API probes."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from packages.common.node27_pgdata_workload_http import (
    HTTP_BODY_LIMIT_BYTES,
    HTTP_TIMEOUT_SECONDS,
    close_response,
    open_local_get,
    read_bounded_json_body,
)
from packages.common.node27_pgdata_workload_plan import (
    PLAN_BUFFER_LIMIT,
    evaluate_explain_json_plan,
    load_candidate_relations,
)
from packages.common.node27_pgdata_workload_query import (
    FORECAST_SERIES_PATH_TEMPLATE,
    assert_query_identity,
    validate_river_series_response,
)
from packages.common.node27_pgdata_workload_types import refuse

WARMUP_COUNT = 1
ACCEPTED_SAMPLE_COUNT = 20
P95_NEAREST_RANK_INDEX = 18
SQL_P95_LIMIT_MS = 300
API_P95_LIMIT_MS = 500
PERCENTILE_METHOD = "nearest-rank"
FORECAST_SERIES_PATH_PREFIX = "/api/v1/basin-versions/"
FORECAST_SERIES_PATH_SUFFIX = "/forecast-series"
HYDRO_RUN_IDENTITY_SQL = """
SELECT h.run_id, h.model_id, h.source_id, h.cycle_time, h.scenario_id
FROM hydro.hydro_run h
WHERE h.run_id = %(run_id)s
  AND h.model_id = %(model_id)s
  AND h.cycle_time = %(issue_time)s
  AND h.run_type = 'forecast'
LIMIT 2
"""


def nearest_rank_p95(samples: Sequence[float]) -> float:
    if len(samples) != ACCEPTED_SAMPLE_COUNT:
        refuse("P95 requires exactly 20 accepted samples", code="P95_SAMPLE_COUNT", stage="p95")
    values: list[float] = []
    for item in samples:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            refuse("P95 sample is not a finite number", code="P95_SAMPLE_INVALID", stage="p95")
        number = float(item)
        if not math.isfinite(number) or number < 0:
            refuse("P95 sample is not a finite non-negative number", code="P95_SAMPLE_INVALID", stage="p95")
        values.append(number)
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    if index != P95_NEAREST_RANK_INDEX:
        refuse("nearest-rank index drifted from the 20-sample contract", code="P95_INDEX_DRIFT", stage="p95")
    return ordered[index]


def _duration_ms(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        refuse("sample duration is not a finite number", code="SAMPLE_DURATION_INVALID", stage="performance")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        refuse(
            "sample duration is not a finite non-negative number", code="SAMPLE_DURATION_INVALID", stage="performance"
        )
    return number


def run_warmup_and_accepted(
    probe: Callable[[int], Mapping[str, Any]],
    *,
    warmup_count: int = WARMUP_COUNT,
    accepted_count: int = ACCEPTED_SAMPLE_COUNT,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    if warmup_count != WARMUP_COUNT or accepted_count != ACCEPTED_SAMPLE_COUNT:
        refuse("warmup/accepted counts drifted from the 1+20 contract", code="SAMPLE_COUNT_DRIFT", stage="performance")
    warmup = dict(probe(0))
    warmup["index"] = 0
    warmup["discarded"] = True
    warmup["duration_ms"] = _duration_ms(warmup.get("duration_ms"))
    accepted: list[dict[str, Any]] = []
    for index in range(1, accepted_count + 1):
        sample = dict(probe(index))
        if sample.get("discarded") is True:
            refuse("accepted sample was marked discarded", code="SAMPLE_DISCARDED", stage="performance")
        sample["index"] = index
        sample["discarded"] = False
        sample["duration_ms"] = _duration_ms(sample.get("duration_ms"))
        accepted.append(sample)
    if len(accepted) != accepted_count:
        refuse("accepted sample count is not 20", code="SAMPLE_COUNT_INVALID", stage="performance")
    return warmup, tuple(accepted)


def assert_forecast_series_path(path: str) -> str:
    text = str(path or "")
    if not text.startswith(FORECAST_SERIES_PATH_PREFIX) or not text.endswith(FORECAST_SERIES_PATH_SUFFIX):
        refuse("API path is not the shipping forecast-series contract", code="API_PATH_INVALID", stage="performance")
    if "/layers/discharge/valid-times" in text:
        refuse("API path is the invalid valid-times surface", code="API_PATH_INVALID", stage="performance")
    template_prefix = FORECAST_SERIES_PATH_TEMPLATE.split("{", 1)[0]
    if not text.startswith(template_prefix):
        refuse("API path is not the shipping forecast-series contract", code="API_PATH_INVALID", stage="performance")
    return text


def evaluate_sql_samples(
    *,
    warmup: Mapping[str, Any],
    accepted: Sequence[Mapping[str, Any]],
    candidate_chunk_names: Sequence[str],
    segment_id: str,
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    if warmup.get("index") != 0 or warmup.get("discarded") is not True:
        refuse("SQL warmup is not the discarded index-0 sample", code="SQL_WARMUP_INVALID", stage="performance")
    if len(accepted) != ACCEPTED_SAMPLE_COUNT:
        refuse("SQL accepted count is not 20", code="SQL_SAMPLE_COUNT", stage="performance")
    buffers = 0
    plans: list[dict[str, Any]] = []
    for sample in accepted:
        payload = sample.get("explain_json")
        if payload is None:
            refuse("SQL sample is missing EXPLAIN JSON", code="PLAN_JSON_MISSING", stage="performance")
        plan = evaluate_explain_json_plan(
            payload,
            candidate_chunk_names=candidate_chunk_names,
            segment_id=segment_id,
            window_start=window_start,
            window_end=window_end,
            require_segment_bound=True,
            allow_empty_rows=False,
        )
        if plan.get("seq_scan") is True:
            refuse("SQL lane retained a Seq Scan", code="PLAN_SEQ_SCAN", stage="plan")
        buffers = max(buffers, int(plan["shared_buffer_blocks"]))
        plans.append(
            {
                "index": sample["index"],
                "shared_hit_blocks": plan["shared_hit_blocks"],
                "shared_read_blocks": plan["shared_read_blocks"],
                "shared_buffer_blocks": plan["shared_buffer_blocks"],
                "decompressed_count": plan["decompressed_count"],
                "touched_count": plan["touched_count"],
                "candidate_count": plan["candidate_count"],
                "actual_rows": plan["actual_rows"],
            }
        )
    p95 = nearest_rank_p95([float(sample["duration_ms"]) for sample in accepted])
    if p95 > SQL_P95_LIMIT_MS:
        refuse("SQL P95 exceeds 300 ms", code="SQL_P95_EXCEEDED", stage="performance")
    return {
        "warmup_count": WARMUP_COUNT,
        "accepted_count": ACCEPTED_SAMPLE_COUNT,
        "percentile_method": PERCENTILE_METHOD,
        "p95_index": P95_NEAREST_RANK_INDEX,
        "p95_ms": p95,
        "p95_limit_ms": SQL_P95_LIMIT_MS,
        "buffers": buffers,
        "buffer_limit": PLAN_BUFFER_LIMIT,
        "seq_scan": False,
        "all_chunk_decompression": False,
        "segment_id": segment_id,
        "window_start": window_start,
        "window_end": window_end,
        "candidate_count": len(tuple(dict.fromkeys(candidate_chunk_names))),
        "accepted_durations_ms": [float(sample["duration_ms"]) for sample in accepted],
        "plan_metrics": plans,
    }


def evaluate_api_samples(
    *,
    warmup: Mapping[str, Any],
    accepted: Sequence[Mapping[str, Any]],
    path: str,
) -> dict[str, Any]:
    path = assert_forecast_series_path(path)
    if warmup.get("index") != 0 or warmup.get("discarded") is not True:
        refuse("API warmup is not the discarded index-0 sample", code="API_WARMUP_INVALID", stage="performance")
    if len(accepted) != ACCEPTED_SAMPLE_COUNT:
        refuse("API accepted count is not 20", code="API_SAMPLE_COUNT", stage="performance")
    statuses: list[int] = []
    digests: list[str] = []
    for sample in accepted:
        status = sample.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or status < 200 or status > 299:
            refuse("API sample status is not 2xx", code="API_STATUS_INVALID", stage="performance")
        if "body_len" not in sample:
            refuse("API sample body_len is missing", code="API_BODY_MISSING", stage="performance")
        body_len = sample.get("body_len")
        if not isinstance(body_len, int) or isinstance(body_len, bool) or body_len < 0:
            refuse("API sample body_len is not a non-negative integer", code="API_BODY_INVALID", stage="performance")
        if body_len > HTTP_BODY_LIMIT_BYTES:
            refuse("API sample body exceeds the bounded limit", code="API_BODY_LIMIT", stage="performance")
        statuses.append(status)
        digest = sample.get("content_digest")
        if not isinstance(digest, str) or not digest:
            refuse("API sample content digest is missing", code="API_CONTENT_MISSING", stage="performance")
        digests.append(digest)
    p95 = nearest_rank_p95([float(sample["duration_ms"]) for sample in accepted])
    if p95 > API_P95_LIMIT_MS:
        refuse("API P95 exceeds 500 ms", code="API_P95_EXCEEDED", stage="performance")
    return {
        "warmup_count": WARMUP_COUNT,
        "accepted_count": ACCEPTED_SAMPLE_COUNT,
        "percentile_method": PERCENTILE_METHOD,
        "p95_index": P95_NEAREST_RANK_INDEX,
        "p95_ms": p95,
        "p95_limit_ms": API_P95_LIMIT_MS,
        "path": path,
        "timeout_seconds": HTTP_TIMEOUT_SECONDS,
        "body_limit_bytes": HTTP_BODY_LIMIT_BYTES,
        "accepted_durations_ms": [float(sample["duration_ms"]) for sample in accepted],
        "accepted_statuses": statuses,
        "content_digests": digests,
    }


def _native_explain_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(parameters, Mapping):
        return dict(parameters)
    refuse("EXPLAIN parameters are not a mapping", code="SQL_EXPLAIN_FAILED", stage="performance")
    raise AssertionError("unreachable")


def execute_explain(connection: Any, *, sql: str, parameters: Mapping[str, Any]) -> Any:
    try:
        bound = _native_explain_parameters(parameters)
        with connection.cursor() as cursor:
            cursor.execute(sql, bound)
            row = cursor.fetchone()
    except Exception:
        refuse("EXPLAIN JSON query failed", code="SQL_EXPLAIN_FAILED", stage="performance")
    if row is None:
        refuse("EXPLAIN returned no plan", code="SQL_EXPLAIN_EMPTY", stage="performance")
    if isinstance(row, Mapping):
        payload = next(iter(row.values()))
    else:
        payload = row[0]
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            refuse("EXPLAIN JSON is not parseable", code="PLAN_JSON_INVALID", stage="plan")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            refuse("EXPLAIN JSON is not parseable", code="PLAN_JSON_INVALID", stage="plan")
    return payload


def make_sql_probe(
    connection: Any,
    *,
    captured: Mapping[str, Any],
    expected: Mapping[str, Any],
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[int], dict[str, Any]]:
    def probe(_index: int) -> dict[str, Any]:
        assert_query_identity(captured=captured, expected=expected)
        started = clock()
        payload = execute_explain(
            connection,
            sql=str(captured["explain_sql"]),
            parameters=captured["parameters"],  # type: ignore[arg-type]
        )
        duration_ms = (clock() - started) * 1000.0
        return {"duration_ms": duration_ms, "explain_json": payload}

    return probe


def fetch_local_api(
    *,
    origin: str,
    path: str,
    query: str,
    segment_id: str,
    issue_time: str,
    scenario: str,
    source: str,
    window_start: str,
    window_end: str,
    opener: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    body_limit: int = HTTP_BODY_LIMIT_BYTES,
    run_id: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    from packages.common.node27_pgdata_workload_io import LOCAL_ORIGIN_RE

    if LOCAL_ORIGIN_RE.fullmatch(origin) is None:
        refuse("API origin is not the local loopback contract", code="INPUT_ORIGIN_INVALID", stage="performance")
    path = assert_forecast_series_path(path)
    started = clock()
    _request, response = open_local_get(
        url=f"{origin}{path}?{query}",
        opener=opener,
        timeout_seconds=timeout_seconds,
        stage="performance",
    )
    try:
        status, body, payload = read_bounded_json_body(response, body_limit=body_limit, stage="performance")
        validated = validate_river_series_response(
            payload,
            segment_id=segment_id,
            issue_time=issue_time,
            scenario=scenario,
            source=source,
            window_start=window_start,
            window_end=window_end,
            run_id=run_id,
            model_id=model_id,
        )
        duration_ms = (clock() - started) * 1000.0
        return {
            "duration_ms": duration_ms,
            "status": status,
            "body_len": len(body),
            "point_count": validated["point_count"],
            "content_digest": validated["content_digest"],
        }
    finally:
        close_response(response)


def make_api_probe(
    *,
    origin: str,
    path: str,
    query: str,
    segment_id: str,
    issue_time: str,
    scenario: str,
    source: str,
    window_start: str,
    window_end: str,
    opener: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
    run_id: str | None = None,
    model_id: str | None = None,
) -> Callable[[int], dict[str, Any]]:
    def probe(_index: int) -> dict[str, Any]:
        return fetch_local_api(
            origin=origin,
            path=path,
            query=query,
            segment_id=segment_id,
            issue_time=issue_time,
            scenario=scenario,
            source=source,
            window_start=window_start,
            window_end=window_end,
            opener=opener,
            clock=clock,
            run_id=run_id,
            model_id=model_id,
        )

    return probe


def prove_authoritative_run_identity(connection: Any, *, captured: Mapping[str, Any]) -> dict[str, Any]:
    """SQL-side proof that the frozen run/model/cycle exists as one forecast run."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                HYDRO_RUN_IDENTITY_SQL,
                {
                    "run_id": captured["run_id"],
                    "model_id": captured["model_id"],
                    "issue_time": captured["parameters"]["issue_time"],
                },
            )
            rows = cursor.fetchall()
    except Exception:
        refuse("authoritative run identity lookup failed", code="SQL_IDENTITY_FAILED", stage="performance")
    if len(rows) != 1:
        refuse("authoritative run identity is missing or ambiguous", code="SQL_IDENTITY_MISSING", stage="performance")
    row = rows[0]
    if isinstance(row, Mapping):
        run_id = str(row.get("run_id") or "")
        model_id = str(row.get("model_id") or "")
        source_id = str(row.get("source_id") or "").upper()
        scenario_id = str(row.get("scenario_id") or "")
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        run_id = str(row[0])
        model_id = str(row[1])
        source_id = str(row[2]).upper()
        scenario_id = str(row[4])
    else:
        refuse("authoritative run identity row is malformed", code="SQL_IDENTITY_FAILED", stage="performance")
    if run_id != captured["run_id"] or model_id != captured["model_id"]:
        refuse("authoritative run identity drifted", code="SQL_IDENTITY_MISMATCH", stage="performance")
    if source_id and source_id != captured["source"]:
        refuse("authoritative source identity drifted", code="SQL_IDENTITY_MISMATCH", stage="performance")
    if scenario_id and scenario_id != captured["scenario"]:
        refuse("authoritative scenario identity drifted", code="SQL_IDENTITY_MISMATCH", stage="performance")
    return {"run_id": run_id, "model_id": model_id, "source": captured["source"]}


def load_window_candidates(connection: Any, *, window_start: str, window_end: str) -> dict[str, Any]:
    def execute(sql: str, params: object = None) -> list[Mapping[str, Any]]:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            fetched = cursor.fetchall() or ()
            names = None
            desc = getattr(cursor, "description", None)
            if desc is not None:
                names = tuple(
                    str(item[0] if not isinstance(item, Mapping) else item.get("name") or "") for item in desc
                )
            out: list[Mapping[str, Any]] = []
            for row in fetched:
                if isinstance(row, Mapping):
                    out.append(dict(row))
                    continue
                if names is None or any(not name for name in names):
                    refuse("cursor description is missing", code="SQL_DESCRIPTION_MISSING", stage="performance")
                if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                    refuse("discovery row is malformed", code="SQL_ROW_INVALID", stage="performance")
                if len(row) != len(names):
                    refuse("row width does not match cursor.description", code="SQL_ROW_WIDTH", stage="performance")
                out.append({name: row[index] for index, name in enumerate(names)})
            return out

    return load_candidate_relations(execute, window_start=window_start, window_end=window_end)
