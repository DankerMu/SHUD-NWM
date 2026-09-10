"""Bounded read-only #1342 four-lane product-curve performance oracle."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from packages.common.node27_issue1895_lanes import LANE_NAMES, lane_kind, lane_source
from packages.common.node27_issue1895_percentiles import (
    ACCEPTED_SAMPLE_COUNT,
    API_P95_LIMIT_MS,
    P95_NEAREST_RANK_INDEX,
    SQL_P95_LIMIT_MS,
    WARMUP_COUNT,
    nearest_rank_p95,
)
from packages.common.node27_issue1895_sql import (
    PLAN_BUFFER_LIMIT,
    REPRESENTATIVE_HYPERTABLE,
    evaluate_explain_json_plan,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_payload, redact_text

ARTIFACT = "nhms-issue1895-performance-oracle"
SCHEMA_VERSION = "1.1"
HTTP_TIMEOUT_SECONDS = 5
HTTP_BODY_LIMIT_BYTES = 65536
PERCENTILE_METHOD = "nearest-rank"
FORECAST_SERIES_PATH_PREFIX = "/api/v1/basin-versions/"
FORECAST_SERIES_PATH_SUFFIX = "/forecast-series"
COMMIT_MARKER_SUFFIX = ".commit"


def _closed_failure(code: str, stage: str) -> dict[str, str]:
    return {"code": code, "stage": stage}


def _duration_ms(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Issue1895ReadinessError(
            "sample duration is not a finite number",
            code="SAMPLE_DURATION_INVALID",
            stage="performance",
        )
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise Issue1895ReadinessError(
            "sample duration is not a finite non-negative number",
            code="SAMPLE_DURATION_INVALID",
            stage="performance",
        )
    return number


def run_warmup_and_accepted(
    probe: Callable[[int], Mapping[str, Any]],
    *,
    warmup_count: int = WARMUP_COUNT,
    accepted_count: int = ACCEPTED_SAMPLE_COUNT,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Discard exactly one warmup, then accept exactly 20 serial samples."""

    if warmup_count != WARMUP_COUNT or accepted_count != ACCEPTED_SAMPLE_COUNT:
        raise Issue1895ReadinessError(
            "warmup/accepted counts drifted from the 1+20 contract",
            code="SAMPLE_COUNT_DRIFT",
            stage="performance",
        )
    warmup = dict(probe(0))
    warmup["index"] = 0
    warmup["discarded"] = True
    warmup["duration_ms"] = _duration_ms(warmup.get("duration_ms"))
    accepted: list[dict[str, Any]] = []
    for index in range(1, accepted_count + 1):
        sample = dict(probe(index))
        if sample.get("discarded") is True:
            raise Issue1895ReadinessError(
                "accepted sample was marked discarded",
                code="SAMPLE_DISCARDED",
                stage="performance",
            )
        sample["index"] = index
        sample["discarded"] = False
        sample["duration_ms"] = _duration_ms(sample.get("duration_ms"))
        accepted.append(sample)
    if len(accepted) != accepted_count:
        raise Issue1895ReadinessError(
            "accepted sample count is not 20",
            code="SAMPLE_COUNT_INVALID",
            stage="performance",
        )
    return warmup, tuple(accepted)


def assert_forecast_series_path(path: str) -> str:
    text = str(path or "")
    if not text.startswith(FORECAST_SERIES_PATH_PREFIX) or not text.endswith(FORECAST_SERIES_PATH_SUFFIX):
        raise Issue1895ReadinessError(
            "API path is not the shipping forecast-series contract",
            code="API_PATH_INVALID",
            stage="performance",
        )
    if "/layers/discharge/valid-times" in text:
        raise Issue1895ReadinessError(
            "API path is the invalid valid-times surface",
            code="API_PATH_INVALID",
            stage="performance",
        )
    return text


def _plan_from_sample(
    sample: Mapping[str, Any],
    *,
    candidates: Sequence[str],
    segment_id: str,
    window_start: str,
    window_end: str,
    lane_kind_name: str | None = None,
) -> dict[str, Any]:
    payload = sample.get("explain_json")
    if payload is None:
        raise Issue1895ReadinessError(
            "SQL sample is missing EXPLAIN JSON",
            code="PLAN_JSON_MISSING",
            stage="performance",
        )
    return evaluate_explain_json_plan(
        payload,
        candidate_chunk_names=candidates,
        segment_id=segment_id,
        window_start=window_start,
        window_end=window_end,
        require_segment_bound=True,
        allow_empty_rows=False,
        lane_kind=lane_kind_name,
    )


def evaluate_sql_lane(
    *,
    warmup: Mapping[str, Any],
    accepted: Sequence[Mapping[str, Any]],
    candidate_chunk_names: Sequence[str],
    segment_id: str,
    window_start: str,
    window_end: str | None = None,
    hypertable: tuple[str, str] = REPRESENTATIVE_HYPERTABLE,
    lane_kind_name: str | None = None,
) -> dict[str, Any]:
    if warmup.get("index") != 0 or warmup.get("discarded") is not True:
        raise Issue1895ReadinessError(
            "SQL warmup is not the discarded index-0 sample",
            code="SQL_WARMUP_INVALID",
            stage="performance",
        )
    if len(accepted) != ACCEPTED_SAMPLE_COUNT:
        raise Issue1895ReadinessError(
            "SQL accepted count is not 20",
            code="SQL_SAMPLE_COUNT",
            stage="performance",
        )
    end = window_end or window_start
    buffers = 0
    plans: list[dict[str, Any]] = []
    for sample in accepted:
        plan = _plan_from_sample(
            sample,
            candidates=candidate_chunk_names,
            segment_id=segment_id,
            window_start=window_start,
            window_end=end,
            lane_kind_name=lane_kind_name,
        )
        if plan.get("seq_scan") is True:
            raise Issue1895ReadinessError(
                "SQL lane retained a Seq Scan",
                code="PLAN_SEQ_SCAN",
                stage="plan",
            )
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
        raise Issue1895ReadinessError(
            "SQL P95 exceeds 300 ms",
            code="SQL_P95_EXCEEDED",
            stage="performance",
        )
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
        "hypertable": f"{hypertable[0]}.{hypertable[1]}",
        "segment_id": segment_id,
        "window_start": window_start,
        "window_end": end,
        "candidate_count": len(tuple(dict.fromkeys(candidate_chunk_names))),
        "accepted_durations_ms": [float(sample["duration_ms"]) for sample in accepted],
        "plan_metrics": plans,
    }


def evaluate_api_lane(
    *,
    warmup: Mapping[str, Any],
    accepted: Sequence[Mapping[str, Any]],
    path: str,
) -> dict[str, Any]:
    path = assert_forecast_series_path(path)
    if warmup.get("index") != 0 or warmup.get("discarded") is not True:
        raise Issue1895ReadinessError(
            "API warmup is not the discarded index-0 sample",
            code="API_WARMUP_INVALID",
            stage="performance",
        )
    if len(accepted) != ACCEPTED_SAMPLE_COUNT:
        raise Issue1895ReadinessError(
            "API accepted count is not 20",
            code="API_SAMPLE_COUNT",
            stage="performance",
        )
    statuses: list[int] = []
    for sample in accepted:
        status = sample.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or status < 200 or status > 299:
            raise Issue1895ReadinessError(
                "API sample status is not 2xx",
                code="API_STATUS_INVALID",
                stage="performance",
            )
        if "body_len" not in sample:
            raise Issue1895ReadinessError(
                "API sample body_len is missing",
                code="API_BODY_MISSING",
                stage="performance",
            )
        body_len = sample.get("body_len")
        if not isinstance(body_len, int) or isinstance(body_len, bool) or body_len < 0:
            raise Issue1895ReadinessError(
                "API sample body_len is not a non-negative integer",
                code="API_BODY_INVALID",
                stage="performance",
            )
        if body_len > HTTP_BODY_LIMIT_BYTES:
            raise Issue1895ReadinessError(
                "API sample body exceeds the bounded limit",
                code="API_BODY_LIMIT",
                stage="performance",
            )
        statuses.append(status)
    p95 = nearest_rank_p95([float(sample["duration_ms"]) for sample in accepted])
    if p95 > API_P95_LIMIT_MS:
        raise Issue1895ReadinessError(
            "API P95 exceeds 500 ms",
            code="API_P95_EXCEEDED",
            stage="performance",
        )
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
    }


def evaluate_named_lane(
    *,
    name: str,
    sql_warmup: Mapping[str, Any],
    sql_accepted: Sequence[Mapping[str, Any]],
    api_warmup: Mapping[str, Any],
    api_accepted: Sequence[Mapping[str, Any]],
    candidate_chunk_names: Sequence[str],
    segment_id: str,
    window_start: str,
    window_end: str,
    api_path: str,
) -> dict[str, Any]:
    if name not in LANE_NAMES:
        raise Issue1895ReadinessError(
            "lane name is closed",
            code="LANE_NAME_INVALID",
            stage="performance",
        )
    sql = evaluate_sql_lane(
        warmup=sql_warmup,
        accepted=sql_accepted,
        candidate_chunk_names=candidate_chunk_names,
        segment_id=segment_id,
        window_start=window_start,
        window_end=window_end,
        lane_kind_name=lane_kind(name),
    )
    api = evaluate_api_lane(warmup=api_warmup, accepted=api_accepted, path=api_path)
    return {
        "name": name,
        "kind": lane_kind(name),
        "source": lane_source(name),
        "state": "hot_uncompressed_source" if lane_kind(name) == "hot" else "cold_compressed_target",
        "sql": sql,
        "api": api,
        "run_id": "",
        "model_id": "",
        "cycle_time": "",
        "query_digest": "",
        "candidate_chunk_names": list(candidate_chunk_names),
    }


def build_performance_receipt(
    *,
    lanes: Mapping[str, Mapping[str, Any]],
    identity: Mapping[str, Any],
    frozen: Mapping[str, Any],
    status: str = "PASS",
    failure: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if status not in {"PASS", "FAIL", "BLOCKED"}:
        raise Issue1895ReadinessError(
            "performance receipt status is closed",
            code="RECEIPT_STATUS_INVALID",
            stage="performance",
        )
    if status == "PASS" and failure is not None:
        raise Issue1895ReadinessError(
            "PASS receipt cannot carry a failure",
            code="RECEIPT_STATUS_INVALID",
            stage="performance",
        )
    if status != "PASS" and failure is None:
        raise Issue1895ReadinessError(
            "non-PASS receipt requires a closed failure code",
            code="RECEIPT_FAILURE_MISSING",
            stage="performance",
        )
    if set(lanes) != set(LANE_NAMES):
        raise Issue1895ReadinessError(
            "performance receipt is missing the four closed lanes",
            code="RECEIPT_LANE_SET_INVALID",
            stage="performance",
        )
    readonly = identity.get("readonly") if isinstance(identity.get("readonly"), Mapping) else None
    if not isinstance(readonly, Mapping):
        raise Issue1895ReadinessError(
            "performance receipt is missing readonly proof",
            code="RECEIPT_READONLY_MISSING",
            stage="performance",
        )
    public_identity = {key: value for key, value in identity.items() if key != "readonly"}
    return {
        "artifact": ARTIFACT,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "failure": None if failure is None else _closed_failure(str(failure["code"]), str(failure["stage"])),
        "lanes": {name: dict(lanes[name]) for name in LANE_NAMES},
        "frozen": dict(frozen),
        "browser": {
            "lane": "test:e2e:live-river-click",
            "p95_limit_ms": 2000,
            "comparator": "strict_lt",
            "binder": "apps/frontend/scripts/river-click-receipt-binder.mjs",
        },
        "identity": public_identity,
        "readonly": {
            "transaction_read_only": bool(readonly.get("transaction_read_only") is True),
            "current_user": str(readonly.get("current_user") or ""),
        },
        "percentile_method": PERCENTILE_METHOD,
    }


def validate_performance_receipt(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed nested four-lane validator. Extra or missing nested keys fail."""

    from packages.common.node27_issue1895_receipt_validate import (
        validate_performance_receipt as _validate_nested,
    )

    return _validate_nested(document)


def refuse_secret_output(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Redact any accidental secret-shaped value before a receipt is published."""

    return redact_payload(payload)


def format_refusal(error: Issue1895ReadinessError) -> str:
    return redact_text(f"{error.code}: {error}")


def commit_marker_name(receipt_name: str) -> str:
    return f"{receipt_name}{COMMIT_MARKER_SUFFIX}"
